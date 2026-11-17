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

# Locate staged music files
script_folder = Path(__file__).resolve().parent
staging_folder = script_folder / constants.staging_folder_name

# Check whether an audio file can be decoded
def validate_audio(path):
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-xerror",
            "-i", str(path),
            "-map", "0:a:0",
            "-f", "null",
            "-"
        ],
        capture_output=True,
        text=True
    )
    return result.returncode == 0, result.stderr.strip()

# Generate an acoustic fingerprint from an audio file
def fingerprint_audio(path):
    fpcalc = next((script_folder / ".tools").rglob("fpcalc"), None)
    if fpcalc is None:
        raise FileNotFoundError("Chromaprint fpcalc not found.")
    result = subprocess.run(
        [str(fpcalc), "-json", str(path)],
        capture_output=True,
        text=True,
        check=True
    )
    return json.loads(result.stdout)

# Validate audio before attempting identification
def inspect_audio(path):
    valid, error = validate_audio(path)
    if not valid:
        return None, error
    fingerprint = fingerprint_audio(path)
    if fingerprint["duration"] <= 0 or not fingerprint["fingerprint"]:
        return None, "Audio is empty or could not be fingerprinted."
    return fingerprint, None

# Identify audio using its acoustic fingerprint
def identify_audio(fingerprint, api_key):
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
    with urlopen(request, timeout=20) as response:
        results = json.load(response)
    if results.get("status") != "ok":
        raise ValueError("AcoustID lookup failed.")
    return results.get("results", [])

# Collect recording candidates and their strongest match scores
def collect_recording_candidates(matches):
    candidates = {}
    for match in matches:
        score = match.get("score", 0)
        for recording in match.get("recordings", []):
            recording_id = recording.get("id")
            if recording_id:
                candidates[recording_id] = max(
                    candidates.get(recording_id, 0),
                    score
                )
    return candidates

# Identify a recording using AcoustID and MusicBrainz
def identify_recording(fingerprint, api_key):
    matches = identify_audio(fingerprint, api_key)
    candidates = collect_recording_candidates(matches)
    if not candidates or len(candidates) > 5:
        return matches, candidates, None
    if len(candidates) == 1:
        recording_id = next(iter(candidates))
        details = lookup.get_recording_details(recording_id)
        return matches, candidates, details

    # Compare ambiguous recordings against actual audio duration
    duration_matches = []
    for recording_id in candidates:
        details = lookup.get_recording_details(recording_id)
        expected_ms = details.get("length")
        if expected_ms is None:
            continue
        difference = abs(
            fingerprint["duration"] - int(expected_ms) / 1000
        )
        if difference <= 3:
            duration_matches.append(details)
    # Only proceed when exactly one candidate fits
    if len(duration_matches) != 1:
        return matches, candidates, None
    return matches, candidates, duration_matches[0]

# Assess whether an identified recording is reliable enough to propose corrections
def assess_recording(fingerprint, candidates, details):
    if details is None:
        return False, "Recording identity is ambiguous or unknown."
    score = candidates.get(details.get("id"), 0)
    if score < 0.95:
        return False, "AcoustID confidence is too low."
    expected_ms = details.get("length")
    if not expected_ms:
        return False, "MusicBrainz duration is unavailable."
    difference = abs(fingerprint["duration"] - int(expected_ms) / 1000)
    if difference > 3:
        return False, "Recording duration needs review."
    return True, "Preliminary recording checks passed."

# Run preliminary audio identification for one track
def audit_track(path, api_key, release_id=None, cache=None):
    if cache is not None and path in cache:
        fingerprint, candidates, details = cache[path]
    else:
        try:
            fingerprint, error = inspect_audio(path)
        except (OSError, subprocess.CalledProcessError,
                ValueError, KeyError) as error:
            return "inspection_error", str(error), None
        if fingerprint is None:
            return "invalid_audio", error, None
        try:
            _, candidates, details = identify_recording(
                fingerprint, api_key
            )
        except (URLError, TimeoutError) as error:
            return "lookup_unavailable", str(error), None
        if cache is not None:
            cache[path] = (fingerprint, candidates, details)

    # Use independently established album context to resolve ambiguity
    if release_id:
        belongs_to_release = details is not None and any(
            release.get("id") == release_id
            for release in details.get("release-list", [])
        )
        if not belongs_to_release:
            release_matches = lookup.recordings_on_releases(
                candidates.keys(), {release_id}
            )
            details = (
                release_matches[0]
                if len(release_matches) == 1
                else None
            )
    eligible, reason = assess_recording(
        fingerprint, candidates, details
    )
    if not eligible:
        return "needs_review", reason, details
    return "preliminary_match", reason, details


# Audit multiple tracks without modifying them
def audit_tracks(paths, api_key, release_id=None, cache=None, quiet=False):
    results = []
    for path in paths:
        was_cached = cache is not None and path in cache
        try:
            status, reason, details = audit_track(
                path, api_key, release_id=release_id, cache=cache
            )
        except Exception as error:
            status = "audit_error"
            reason = f"{type(error).__name__}: {error}"
            details = None
        results.append({
            "path": path,
            "status": status,
            "reason": reason,
            "details": details
        })
        if not quiet:
            print(f"\n{path.name}")
            print(f"  Status: {status}")
            print(f"  Reason: {reason}")
            if details:
                print(f"  Artist: {details.get('artist-credit-phrase')}")
                print(f"  Title: {details.get('title')}")

        # Only pause after fresh identification work
        if not was_cached:
            time.sleep(1)

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
