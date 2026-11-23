from pathlib import Path
import subprocess
import constants
import lookup
import tag
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError
import sys
import time
import math
import tempfile
from copy import deepcopy
from threading import Lock
from contextvars import ContextVar
from contextlib import contextmanager

# Locate staged music files
script_folder = Path(__file__).resolve().parent.parent
staging_folder = script_folder / constants.staging_folder_name

MIN_RECORDING_SCORE = 0.95
MAX_DURATION_DIFFERENCE = 3
MAX_RECORDING_LOOKUPS = 12
DECODE_TIMEOUT = 120
FINGERPRINT_TIMEOUT = 45
LOOKUP_TIMEOUT = 20
MAX_PROVIDER_BYTES = 2_000_000
MAX_FINGERPRINT_BYTES = 1_000_000
_acoustid_lock = Lock()
_last_acoustid_request = None


def _finite_number(value, *, positive=False, maximum=None):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _validated_fingerprint(value):
    if not isinstance(value, dict):
        raise ValueError("Fingerprint provider returned a non-object response.")
    duration = _finite_number(value.get("duration"), positive=True)
    fingerprint = value.get("fingerprint")
    if (duration is None or not isinstance(fingerprint, str) or not fingerprint.strip()
            or len(fingerprint.encode("utf-8")) > MAX_FINGERPRINT_BYTES):
        raise ValueError("Fingerprint duration or fingerprint data is invalid.")
    return {"duration": duration, "fingerprint": fingerprint}


def _run_audio_process(arguments, timeout):
    # Spool tool output instead of accumulating arbitrary executable output in RAM.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(arguments, input=b"",
                                stdout=output, stderr=errors, timeout=timeout)
        output.seek(0)
        data = output.read(MAX_FINGERPRINT_BYTES + 1)
        if len(data) > MAX_FINGERPRINT_BYTES:
            raise ValueError("Audio tool output exceeds 1 MB.")
        errors.seek(0)
        diagnostic = errors.read(65_537)
        suffix = " [diagnostic truncated]" if len(diagnostic) > 65_536 else ""
        return subprocess.CompletedProcess(
            arguments, result.returncode, data.decode("utf-8"),
            diagnostic[:65_536].decode("utf-8", errors="replace") + suffix)


# Check whether an audio file can be decoded without following network protocols.
def validate_audio(path):
    result = _run_audio_process([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-protocol_whitelist", "file,pipe", "-i", str(Path(path).absolute()),
        "-map", "0:a:0", "-f", "null", "-"
    ], DECODE_TIMEOUT)
    return result.returncode == 0, result.stderr.strip()


def fingerprint_audio(path):
    fpcalc = next((script_folder / ".tools").rglob("fpcalc"), None)
    if fpcalc is None:
        raise FileNotFoundError("Chromaprint fpcalc not found.")
    result = _run_audio_process(
        [str(fpcalc), "-json", str(Path(path).absolute())], FINGERPRINT_TIMEOUT)
    result.check_returncode()
    return _validated_fingerprint(json.loads(result.stdout))


def inspect_audio(path):
    valid, error = validate_audio(path)
    if not valid:
        return None, error
    return _validated_fingerprint(fingerprint_audio(path)), None

# Identify audio using its acoustic fingerprint
def identify_audio(fingerprint, api_key):
    fingerprint = _validated_fingerprint(fingerprint)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("An AcoustID application key is required.")
    data = urlencode({
        "client": api_key,
        "duration": round(fingerprint["duration"]),
        "fingerprint": fingerprint["fingerprint"],
        "meta": "recordings",
        "format": "json"
    }).encode("utf-8")
    request = Request(
        "https://api.acoustid.org/v2/lookup",
        data=data,
        headers={"User-Agent": "MIXIE/0.1"}
    )
    global _last_acoustid_request
    with _acoustid_lock:
        now = time.monotonic()
        if _last_acoustid_request is not None:
            delay = max(0, 1.0 - (now - _last_acoustid_request))
            if delay:
                time.sleep(delay)
        _last_acoustid_request = time.monotonic()
    with urlopen(request, timeout=LOOKUP_TIMEOUT) as response:
        data = response.read(MAX_PROVIDER_BYTES + 1)
    if len(data) > MAX_PROVIDER_BYTES:
        raise ValueError("AcoustID response exceeds 2 MB.")
    results = json.loads(data)
    if not isinstance(results, dict) or results.get("status") != "ok":
        raise ValueError("AcoustID lookup failed or returned malformed data.")
    matches = results.get("results", [])
    if not isinstance(matches, list):
        raise ValueError("AcoustID results must be a list.")
    return matches

def _evidence_error(stage, error, *, recording_id=None, code="provider_error"):
    return lookup.provider_error_evidence(stage, error, recording_id=recording_id, code=code)


def _parse_candidates(matches):
    candidates, errors = {}, []
    if not isinstance(matches, list):
        return {}, [_evidence_error("acoustid", ValueError("Results must be a list."),
                                    code="malformed_acoustid_response")]
    for match in matches:
        if not isinstance(match, dict):
            errors.append(_evidence_error("acoustid", ValueError("Invalid match object."),
                                          code="malformed_acoustid_response"))
            continue
        score = _finite_number(match.get("score"), maximum=1)
        recordings = match.get("recordings", [])
        if score is None or not isinstance(recordings, list):
            errors.append(_evidence_error("acoustid", ValueError("Invalid score or recordings."),
                                          code="malformed_acoustid_response"))
            continue
        for recording in recordings:
            identifier = recording.get("id") if isinstance(recording, dict) else None
            if not isinstance(identifier, str) or not identifier.strip():
                errors.append(_evidence_error("acoustid", ValueError("Invalid recording ID."),
                                              code="malformed_acoustid_response"))
                continue
            candidates[identifier] = max(candidates.get(identifier, 0), score)
    return candidates, errors


def collect_recording_candidates(matches):
    """Compatibility score map; rich collection also retains malformed-row errors."""
    return _parse_candidates(matches)[0]


def collect_recording_evidence(fingerprint, api_key, *, max_candidates=MAX_RECORDING_LOOKUPS):
    """Collect bounded provider snapshots, including errors and unhydrated IDs.

    Returns fingerprint, matches (raw AcoustID response rows), candidates (ID to
    score), recordings (ID to MusicBrainz details), hydration_skips (ID to reason),
    errors, complete, lookup_state, retryable. States are 'complete', 'partial',
    'unavailable'. Errors retain stage, recording_id, code, type, message, retryable
    and an HTTP status when known; candidate IDs survive hydration errors/budget
    limits and intentional skips. Only valid scores strictly below
    MIN_RECORDING_SCORE are skipped, with reason 'low_confidence'. These candidates
    cannot qualify for automatic use and consume no hydration budget. Intentional
    skips do not make evidence incomplete; unknown/invalid score errors still do.
    This contains no application key. A partial/unavailable snapshot must never
    be interpreted as an exhaustive negative lookup.
    """
    fingerprint = _validated_fingerprint(fingerprint)
    if (isinstance(max_candidates, bool) or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= MAX_RECORDING_LOOKUPS):
        raise ValueError("Recording lookup budget is outside the supported bounds.")
    evidence = {"fingerprint": fingerprint, "matches": [], "candidates": {},
                "recordings": {}, "hydration_skips": {}, "errors": [],
                "complete": False, "lookup_state": "unavailable", "retryable": False}
    try:
        evidence["matches"] = deepcopy(identify_audio(fingerprint, api_key))
    except Exception as error:
        if isinstance(error, AssertionError):
            raise
        failure = _evidence_error("acoustid", error)
        evidence["errors"].append(failure)
        evidence["retryable"] = failure["retryable"]
        return evidence
    candidates, errors = _parse_candidates(evidence["matches"])
    evidence["candidates"], evidence["errors"] = candidates, errors
    ordered = sorted(candidates, key=lambda identifier: (-candidates[identifier], identifier))
    to_hydrate = []
    for identifier in ordered:
        score = _finite_number(candidates[identifier], maximum=1)
        if score is not None and score < MIN_RECORDING_SCORE:
            evidence["hydration_skips"][identifier] = "low_confidence"
        else:
            to_hydrate.append(identifier)
    for identifier in to_hydrate[:max_candidates]:
        try:
            details = lookup.get_recording_details(identifier)
            if not isinstance(details, dict) or details.get("id") != identifier:
                raise ValueError("MusicBrainz returned a missing or mismatched recording ID.")
            evidence["recordings"][identifier] = deepcopy(details)
        except Exception as error:
            if isinstance(error, AssertionError):
                raise
            evidence["errors"].append(_evidence_error("musicbrainz", error, recording_id=identifier))
    if len(to_hydrate) > max_candidates:
        evidence["errors"].append(_evidence_error(
            "budget", ValueError("Recording candidate hydration budget exhausted."),
            code="candidate_budget_exhausted"))
    evidence["complete"] = not evidence["errors"]
    evidence["lookup_state"] = "complete" if evidence["complete"] else "partial"
    evidence["retryable"] = any(error.get("retryable") is True for error in evidence["errors"])
    return evidence


def assess_recording_candidates(fingerprint, candidates, recordings, *, release_id=None,
                                errors=(), complete=True):
    """Pure recording assessment; titles/tags never select or veto an identity.

    Returns status ('verified', 'probable', 'conflicting', 'unidentified'), reason,
    reasons (codes), details (possibly tentative), candidates (evidence rows),
    errors, complete, release_id, retryable. Only status == 'verified' authorizes preliminary
    recording use; a non-None details object alone does not. Candidate rows retain
    score/duration/release evidence even when no recording can be selected.
    ``verified`` is recording scope, not release/edition approval.
    A release_id is a caller-established independent context, never a tagged hint.
    """
    duration = _finite_number(fingerprint.get("duration"), positive=True) if isinstance(fingerprint, dict) else None
    rows = []
    errors = deepcopy(list(errors))
    complete = bool(complete) and not errors
    for identifier, raw_score in sorted(candidates.items()):
        score = _finite_number(raw_score, maximum=1)
        details = recordings.get(identifier)
        valid_details = isinstance(details, dict) and details.get("id") == identifier
        expected = _finite_number(details.get("length"), positive=True) if valid_details else None
        delta = abs(duration - expected / 1000) if duration is not None and expected is not None else None
        release_list = details.get("release-list", []) if valid_details else []
        release_ids = sorted({item["id"] for item in release_list if isinstance(item, dict)
                              and isinstance(item.get("id"), str)}) if isinstance(release_list, list) else []
        reasons = []
        if score is None:
            reasons.append("invalid_score")
        elif score < MIN_RECORDING_SCORE:
            reasons.append("low_confidence")
        if duration is None:
            reasons.append("invalid_audio_duration")
        if not valid_details:
            reasons.append("recording_details_unavailable")
        elif expected is None:
            reasons.append("recording_duration_unavailable")
        if delta is not None and delta > MAX_DURATION_DIFFERENCE:
            reasons.append("duration_conflict")
        if release_id and release_id not in release_ids:
            membership_complete = (valid_details and isinstance(release_list, list)
                                   and details.get("release-list-complete") is True)
            reasons.append("recording_not_on_release" if membership_complete else "release_membership_unavailable")
        status = "verified" if not reasons else "probable"
        if "duration_conflict" in reasons or "recording_not_on_release" in reasons:
            status = "conflicting"
        rows.append({"recording_id": identifier, "score": score, "details": deepcopy(details),
                     "duration_difference": delta, "release_ids": release_ids,
                     "status": status, "reasons": reasons})
    eligible = [row for row in rows if row["status"] == "verified"]
    # A high-confidence candidate with missing timing/details is not eliminated.
    undecided = any(row["status"] == "probable" and
                    (row["score"] is None or row["score"] >= MIN_RECORDING_SCORE) for row in rows)
    details = None
    reasons = list(dict.fromkeys(reason for row in rows for reason in row["reasons"]))
    if len(eligible) > 1 and lookup.clean_explicit_catalog_equivalent(
            [row["details"] for row in eligible]):
        # The catalog labels are interchangeable for ordinary song metadata.
        # Prefer the explicit catalog entry without asserting an audio lyric test.
        preferred = min(eligible, key=lambda row: (
            0 if lookup._catalog_explicit(row["details"]) else 1,
            row["recording_id"]))
        details = deepcopy(preferred["details"])
        status = "verified" if complete and not undecided else "probable"
        reason = ("Equivalent clean/explicit catalog entries; explicit metadata preferred."
                  if status == "verified" else "Catalog evidence remains incomplete.")
        reasons.append("clean_explicit_catalog_equivalent")
    elif len(eligible) > 1:
        status, reason = "conflicting", "Multiple eligible recording identities need review."
        reasons.append("multiple_eligible_recordings")
    elif len(eligible) == 1:
        details = deepcopy(eligible[0]["details"])
        status = "verified" if complete and not undecided else "probable"
        reason = "Preliminary recording checks passed." if status == "verified" else "Recording evidence is incomplete."
        if lookup.unresolved_recording_version(details, rows):
            status, details = "conflicting", None
            reason = "Acoustically plausible recording versions need review."
            reasons.append("unresolved_recording_version")
    elif rows:
        status = "probable" if any(row["status"] == "probable" for row in rows) else "conflicting"
        reason = "Recording identity needs additional evidence."
        if len(rows) == 1:
            details = deepcopy(rows[0]["details"])
    else:
        status, reason = "unidentified", "Recording identity is ambiguous or unknown."
        reasons.append("no_recording_candidates")
    if not complete:
        reasons.append("incomplete_evidence")
    reasons.extend(error.get("code", "provider_error") for error in errors)
    retryable = any(error.get("retryable") is True for error in errors)
    if retryable:
        reasons.append("retryable_provider_error")
        provider = "MusicBrainz" if any(error.get("stage") == "musicbrainz" and error.get("retryable")
                                       for error in errors) else "AcoustID"
        reason = f"{provider} evidence is incomplete after a retryable provider failure; retry identification."
    return {"status": status, "reason": reason, "reasons": list(dict.fromkeys(reasons)),
            "details": details, "candidates": rows, "errors": errors, "complete": complete,
            "release_id": release_id, "retryable": retryable}


def identify_recording(fingerprint, api_key):
    """Legacy (matches, score-map, details) API; use rich evidence for new callers."""
    evidence = collect_recording_evidence(fingerprint, api_key)
    if evidence["lookup_state"] == "unavailable":
        raise URLError(evidence["errors"][0]["message"])
    assessment = assess_recording_candidates(
        fingerprint, evidence["candidates"], evidence["recordings"],
        errors=evidence["errors"], complete=evidence["complete"])
    details = assessment["details"] if assessment["status"] == "verified" else None
    return evidence["matches"], evidence["candidates"], details


def assess_recording(fingerprint, candidates, details):
    if not isinstance(details, dict) or not details.get("id"):
        return False, "Recording identity is ambiguous or unknown."
    identifier = details["id"]
    result = assess_recording_candidates(
        fingerprint, {identifier: candidates.get(identifier)}, {identifier: details})
    if result["status"] == "verified":
        return True, "Preliminary recording checks passed."
    messages = {"low_confidence": "AcoustID confidence is too low.",
                "recording_duration_unavailable": "MusicBrainz duration is unavailable.",
                "duration_conflict": "Recording duration needs review."}
    return False, next((messages[code] for code in result["reasons"] if code in messages),
                       "Recording evidence is invalid or incomplete.")


def _file_revision(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _failed_audit(path, status, error, *, stage="inspection", code="provider_error"):
    evidence = {"fingerprint": None, "matches": [], "candidates": {}, "recordings": {},
                "hydration_skips": {}, "errors": [_evidence_error(stage, error, code=code)],
                "complete": False, "lookup_state": "unavailable", "retryable": False}
    assessment = assess_recording_candidates({}, {}, {}, errors=evidence["errors"], complete=False)
    return {"path": path, "status": status, "reason": str(error), "details": None,
            "assessment": assessment, "evidence": evidence}


def _audit_track_result(path, api_key, release_id=None, cache=None):
    path = Path(path)
    try:
        revision = _file_revision(path)
        entry = cache.get(path) if cache is not None else None
        if not isinstance(entry, dict) or entry.get("revision") != revision or entry.get("version") != 1:
            fingerprint, error = inspect_audio(path)
            if _file_revision(path) != revision:
                if cache is not None:
                    cache.pop(path, None)
                return _failed_audit(path, "inspection_error", ValueError("Source changed during inspection."), code="source_changed")
            if fingerprint is None:
                return _failed_audit(path, "invalid_audio", ValueError(error or "Audio decoding failed."))
            fingerprint = _validated_fingerprint(fingerprint)
            entry = {"version": 1, "revision": revision, "fingerprint": fingerprint, "evidence": None}
            if cache is not None:
                cache[path] = entry
        evidence = entry.get("evidence")
        if evidence is None or not evidence["complete"]:
            evidence = collect_recording_evidence(entry["fingerprint"], api_key)
            entry["evidence"] = deepcopy(evidence)
        if _file_revision(path) != revision:
            if cache is not None:
                cache.pop(path, None)
            return _failed_audit(path, "inspection_error", ValueError("Source changed during lookup."), code="source_changed")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as error:
        return _failed_audit(path, "inspection_error", error)
    assessment = assess_recording_candidates(
        entry["fingerprint"], evidence["candidates"], evidence["recordings"],
        release_id=release_id, errors=evidence["errors"], complete=evidence["complete"])
    status = "preliminary_match" if assessment["status"] == "verified" else "needs_review"
    reason = assessment["reason"]
    if evidence["lookup_state"] == "unavailable":
        status = "lookup_unavailable"
        reason = "AcoustID lookup unavailable: " + evidence["errors"][0]["message"]
        if evidence.get("retryable"):
            reason += " Retry identification after the provider recovers."
    return deepcopy({"path": path, "status": status, "reason": reason,
                     "details": assessment["details"], "assessment": assessment, "evidence": evidence})


def audit_track(path, api_key, release_id=None, cache=None):
    """Legacy (status, reason, details) API; audit_tracks adds assessment/evidence."""
    result = _audit_track_result(path, api_key, release_id=release_id, cache=cache)
    return result["status"], result["reason"], result["details"]


_progress_hook = ContextVar("mixie_audio_progress_hook", default=None)


@contextmanager
def audit_progress(callback):
    """Optional UI-only event handler. Existing audit_tracks callers remain unchanged."""
    token = _progress_hook.set(callback)
    try:
        yield
    finally:
        _progress_hook.reset(token)


def audit_tracks(paths, api_key, release_id=None, cache=None, quiet=False):
    """Read-only audits with legacy keys plus independent assessment/evidence dicts.

    Cache entries are opaque revision-aware snapshots, not the old tuple format.
    Provider pacing occurs at transport time, including standalone lookup calls.
    """
    results = []
    for index, path in enumerate(paths, 1):
        callback = _progress_hook.get()
        if callback is not None:
            callback("start", index, len(paths), path, None)
        try:
            result = _audit_track_result(path, api_key, release_id=release_id, cache=cache)
        except Exception as error:
            result = _failed_audit(Path(path), "audit_error", error, stage="audit")
        results.append(result)
        if callback is not None:
            callback("complete", index, len(paths), path, result)
        if not quiet:
            print(f"\n{result['path'].name}")
            print(f"  Status: {result['status']}")
            print(f"  Reason: {result['reason']}")
            if result["details"]:
                print(f"  Artist: {result['details'].get('artist-credit-phrase')}")
                print(f"  Title: {result['details'].get('title')}")
    return results


# Test validation against one staged audio file
if __name__ == "__main__":
    tracks = sorted(
        path for path in staging_folder.rglob("*")
        if path.is_file()
        and path.suffix.lower() in constants.music_file_types
    )

    if not tracks:
        print("No staged audio files found.")
    else:
        index = int(sys.argv[1]) if len(sys.argv) > 1 else 0

        if not 0 <= index < len(tracks):
            raise SystemExit(f"Choose a track between 0 and {len(tracks) - 1}.")

        track = tracks[index]
        fingerprint, error = inspect_audio(track)

        print(f"Checking: {track.name}")

        if fingerprint is not None:
            print("Audio validation passed")
            print(f"Duration: {fingerprint['duration']} seconds")
            print(f"Fingerprint: {fingerprint['fingerprint'][:50]}...")

            api_key = os.environ.get("ACOUSTID_CLIENT_KEY")

            if api_key:
                try:
                    matches, recording_ids, details = identify_recording(fingerprint, api_key)
                except (URLError, TimeoutError) as error:
                    raise SystemExit(f"AcoustID unavailable: {error}")
                print(f"AcoustID matches: {len(matches)}")

                for match in matches[:3]:
                    print(f"\nMatch score: {match['score']:.3f}")

                    for recording in match.get("recordings", []):
                        print(f"  Title: {recording.get('title')}")
                        print(f"  Recording ID: {recording.get('id')}")

                print(f"Distinct recording candidates: {len(recording_ids)}")

                # Test MusicBrainz identification
                if details is not None:

                    print("\nMusicBrainz identification:")
                    print("Title:", details.get("title"))
                    print("Artist:", details.get("artist-credit-phrase"))

                    # Compare audio duration against MusicBrainz
                    expected_ms = details.get("length")

                    if expected_ms:
                        expected_seconds = int(expected_ms) / 1000
                        difference = abs(fingerprint["duration"] - expected_seconds)

                        print(f"Duration difference: {difference:.2f} seconds")
                        print(
                            "Duration:",
                            "CONSISTENT" if difference <= 3 else "NEEDS REVIEW"
                        )
                    else:
                        print("Duration: UNVERIFIED (MusicBrainz has no duration)")

                    print("Releases:", [
                        release.get("title")
                        for release in details.get("release-list", [])
                    ])

                    # Compare existing tags against the identified recording
                    metadata = tag.read_track_metadata(track)

                    if metadata is None:
                        print("Could not read existing metadata.")
                    else:
                        tagged_title = metadata["title"][0] if metadata["title"] else ""
                        tagged_artist = metadata["artist"][0] if metadata["artist"] else ""

                        title_matches = (
                            bool(tagged_title)
                            and lookup.normalize_title(tagged_title)
                            == lookup.normalize_title(details.get("title", ""))
                        )

                        artist_matches = (
                            bool(tagged_artist)
                            and lookup.recording_credits_artist(details, tagged_artist)
                        )

                        print("\nMetadata comparison:")
                        print("Title:", "MATCH" if title_matches else "NEEDS REVIEW")
                        print("Artist:", "MATCH" if artist_matches else "NEEDS REVIEW")

                        # Assess recording confidence before proposing corrections
                        eligible, reason = assess_recording(fingerprint, recording_ids, details)

                        print(f"\nRecording assessment: {reason}")

                        if eligible:
                            proposals = tag.propose_identity_corrections(
                                metadata, details, title_matches, artist_matches
                            )
                        else:
                            proposals = []

                        print("\nProposed corrections (not applied):")

                        if not proposals:
                            print("  None")
                        else:
                            for proposal in proposals:
                                print(
                                    f"  {proposal['field']}: "
                                    f"{proposal['previous'] or '(missing)'} "
                                    f"-> {proposal['value']}"
                                )

            else:
                print("AcoustID application key not configured.")
        else:
            print(f"Decode failed: {error}")
