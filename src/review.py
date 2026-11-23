"""Manual recording/release decisions; approval never becomes audio verification.

This module reads staged metadata and requested lookup snapshots, but never writes
music or queues. The caller supplies a freshly computed source fingerprint and an
optional save(issue) callback, and applies the returned plan on working copies.
"""
from copy import deepcopy
import json
import math
from pathlib import Path, PurePosixPath
import re

import lookup
import tag
import mixie_terminal as terminal

# Shared theme for nested review/repair menus.
print = terminal.themed_print
input = terminal.themed_input


_TEXT_FIELDS = ("title", "artist", "album", "album_artist", "track_number", "track_total",
                "disc_number", "disc_total", "date", "genre")
_DECISION_FIELDS = {"status", "user_approved", "independently_verified", "source_fingerprint",
                    "release_id", "tracks", "album", "album_artist", "date", "genre", "partial",
                    "release_tracks", "retain_existing_totals", "totals"}
_TRACK_FIELDS = {"recording_id", "title", "artist", "disc", "number"}
_TOTAL_FIELDS = ("track_total", "disc_total")
_SECRET_FIELDS = {"api_key", "acoustid_client_key", "acoustid_key", "acoustic_key", "client_key",
                  "authorization", "access_token", "token", "secret", "password"}


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).casefold().replace("-", "_") in _SECRET_FIELDS:
                continue
            if isinstance(key, tuple):
                key = json.dumps(_json_safe(key), ensure_ascii=False, separators=(",", ":"))
            result[str(key)] = _json_safe(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"Unsupported review evidence type: {type(value).__name__}")


class AlbumReviewRequired(ValueError):
    """The automatic planner must stop; details are detached, JSON-safe evidence."""
    def __init__(self, details):
        if not isinstance(details, dict) or details.get("kind") != "identity_release":
            raise ValueError("Structured identity_release evidence is required.")
        self.details = _json_safe(details)
        super().__init__(str(self.details.get("reason", "Album interpretation needs review.")))


def _text(value, field):
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise ValueError(f"Invalid or unsafe {field}.")
    return value.strip()


def _fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("A current SHA-256 source fingerprint is required.")
    return value


def _source_path(value, *, allow_absolute=False):
    if not isinstance(value, (str, Path)):
        raise ValueError("A relative staged source path is required.")
    # Linux filenames are not metadata text: preserve punctuation and whitespace.
    # Backslashes remain excluded by the current source/archive portability policy.
    text = str(value)
    if not text or "\x00" in text or "\\" in text:
        raise ValueError("Unsafe staged source path.")
    parts = text.split("/")
    if text.startswith("/"):
        parts = parts[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Unsafe staged source path.")
    root = Path(tag.staging_folder).absolute()
    relative = PurePosixPath(text)
    if relative.is_absolute():
        if not allow_absolute:
            raise ValueError("Saved source paths must be relative to staging.")
        try:
            relative = Path(text).relative_to(root)
        except ValueError:
            raise ValueError("Source path is outside staging.") from None
    path = root
    if path.is_symlink():
        raise ValueError("Symlinked staging paths are not reviewable.")
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symlinked source paths are not reviewable.")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Reviewed staged source is unavailable or unsafe.")
    return relative.as_posix(), path


def _inventory(rows):
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("A nonempty full source inventory is required.")
    inventory = {}
    for row in rows:
        if not isinstance(row, dict) or "path" not in row:
            raise ValueError("Malformed source inventory.")
        relative, path = _source_path(row["path"], allow_absolute=True)
        if relative in inventory:
            raise ValueError("Duplicate source inventory path.")
        inventory[relative] = path
    return inventory


def issue_details(audits, evaluations, reason):
    """Serialize every audit/evaluation without selecting or weakening evidence.

    Paths on tracks, review lists and matched values are staging-relative.
    Tuple mapping keys use JSON array strings, e.g. '[1,2]'; metadata is hints only.
    No provider calls or acoustic application key are needed here.
    """
    audits = list(audits)
    inventory = _inventory(audits)
    tracks = []
    for audit, (relative, path) in zip(audits, inventory.items()):
        row = {key: audit[key] for key in ("status", "reason", "details", "assessment", "evidence") if key in audit}
        row["path"] = relative
        existing = audit.get("existing", audit.get("metadata"))
        if existing is None:
            try:
                existing = tag.read_track_metadata(path)
            except (OSError, ValueError):
                existing = None
        if isinstance(existing, dict):
            row["existing"] = {field: existing[field] for field in _TEXT_FIELDS if field in existing}
        tracks.append(_json_safe(row))
    releases = []
    for evaluation in evaluations:
        if not isinstance(evaluation, dict):
            raise ValueError("Malformed release evaluation.")
        row = dict(evaluation)
        row.setdefault("release_id", None)
        row.setdefault("status", "unassessed")
        for key in ("missing", "review", "reasons", "tracklist"):
            row.setdefault(key, [])
        row["review"] = [_source_path(path, allow_absolute=True)[0] for path in row["review"]]
        if "matched" in row:
            if not isinstance(row["matched"], dict):
                raise ValueError("Malformed release mapping evidence.")
            row["matched"] = {key: _source_path(path, allow_absolute=True)[0]
                              for key, path in row["matched"].items()}
        row["tracklist_complete"] = (getattr(row["tracklist"], "complete", True) is True
                                     and "remote_tracklist_incomplete" not in row["reasons"])
        releases.append(_json_safe(row))
    return {"kind": "identity_release", "tracks": tracks, "releases": releases,
            "reason": _text(reason, "review reason")}


def _positive(value, *, remote=False):
    if remote and isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or value <= 0:
        raise ValueError("Disc and track numbers must be positive integers.")
    return value


def _tracklist(tracks, *, online):
    if not isinstance(tracks, list) or not tracks or getattr(tracks, "complete", True) is not True:
        raise ValueError("A complete nonempty full tracklist is required; use OFFLINE if unavailable.")
    result, positions = [], set()
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("Malformed release tracklist.")
        disc = _positive(track.get("disc"), remote=True)
        number = _positive(track.get("number"), remote=True)
        if (disc, number) in positions:
            raise ValueError("Duplicate release disc/track positions.")
        positions.add((disc, number))
        identifier = track.get("recording_id")
        if online or identifier is not None:
            _text(identifier, "release recording ID")
        row = deepcopy(track)
        row.update(disc=str(disc), number=str(number))
        result.append(row)
    return result


def _fetch_tracklist(identifier):
    try:
        tracks = lookup.get_release_tracklist(identifier, refresh=True)
        # _tracklist checks the remote .complete flag before producing a plain list.
        return _tracklist(tracks, online=True)
    except AssertionError:
        raise
    except Exception as error:
        raise ValueError(f"Full release tracklist unavailable: {error}") from error


def _validate_decision(decision, inventory, fingerprint, *, final=False, approved=False):
    if not isinstance(decision, dict) or set(decision) - _DECISION_FIELDS:
        raise ValueError("Malformed saved interpretation; fresh review is required.")
    expected_status = "pending_application" if approved else "draft"
    if (decision.get("status") != expected_status
            or decision.get("user_approved") is not approved
            or decision.get("independently_verified") is not False
            or decision.get("source_fingerprint") != fingerprint):
        raise ValueError("Saved approval/draft is invalid or its source fingerprint changed.")
    if not isinstance(decision.get("tracks"), dict):
        raise ValueError("Malformed interpretation track inventory.")
    if type(decision.get("partial")) is not bool:
        raise ValueError("The partial interpretation flag must be a boolean.")
    cleaned = deepcopy(decision)
    if "release_id" in cleaned and cleaned["release_id"] is not None:
        cleaned["release_id"] = _text(cleaned["release_id"], "release ID")
    for field in ("album", "album_artist"):
        if field in cleaned:
            cleaned[field] = _text(cleaned[field], field)
    manual = {field: _text(cleaned[field], field) for field in ("date", "genre") if field in cleaned}
    if manual.get("genre", "").casefold() == "unknown":
        raise ValueError("Genre is unresolved; omit it rather than approving Unknown as metadata.")
    cleaned.update(tag.validate_manual_metadata(manual))
    if "retain_existing_totals" in cleaned:
        if type(cleaned["retain_existing_totals"]) is not bool:
            raise ValueError("Totals retention needs an explicit boolean choice.")
        if cleaned["retain_existing_totals"] and cleaned.get("release_id") is not None:
            raise ValueError("Existing totals retention is only an OFFLINE policy.")
    if "release_tracks" in cleaned:
        cleaned["release_tracks"] = _tracklist(cleaned["release_tracks"], online=cleaned.get("release_id") is not None)
    positions = set()
    for relative, track in cleaned["tracks"].items():
        _source_path(relative)
        if relative not in inventory or not isinstance(track, dict) or set(track) - _TRACK_FIELDS:
            raise ValueError("Saved track does not belong to the full source inventory.")
        for field in ("title", "artist"):
            if field in track:
                track[field] = _text(track[field], field)
        if "recording_id" in track and track["recording_id"] is not None:
            track["recording_id"] = _text(track["recording_id"], "recording ID")
        for field in ("disc", "number"):
            if field in track:
                _positive(track[field])
        if "disc" in track and "number" in track:
            position = (track["disc"], track["number"])
            if position in positions and (final or approved):
                raise ValueError("Every source must have a unique disc/track position.")
            positions.add(position)
        if final and set(track) != _TRACK_FIELDS:
            raise ValueError("Every source inventory entry needs recording, title, artist, disc and number choices.")
    if "totals" in cleaned:
        if cleaned.get("release_id") is not None or not isinstance(cleaned["totals"], dict):
            raise ValueError("Explicit totals are an OFFLINE source-to-totals mapping only.")
        disc_totals, track_totals = set(), {}
        for relative, totals in cleaned["totals"].items():
            _source_path(relative)
            if relative not in inventory or not isinstance(totals, dict) or set(totals) - set(_TOTAL_FIELDS):
                raise ValueError("Invalid or unknown source in reviewed totals.")
            for field, value in totals.items():
                _positive(value)
            if final and relative in cleaned["tracks"]:
                track = cleaned["tracks"][relative]
                for field, position in (("track_total", "number"), ("disc_total", "disc")):
                    if field in totals and totals[field] < track.get(position, 1):
                        raise ValueError("Reviewed totals cannot be smaller than explicit mapped positions.")
                if "disc_total" in totals:
                    disc_totals.add(totals["disc_total"])
                if "track_total" in totals:
                    track_totals.setdefault(track.get("disc"), set()).add(totals["track_total"])
        if len(disc_totals) > 1 or any(len(values) > 1 for values in track_totals.values()):
            raise ValueError("Conflicting explicit totals need fresh choices for this album/disc.")
    if final:
        required = {"release_id", "album", "album_artist", "partial"}
        if not required <= set(cleaned) or set(cleaned["tracks"]) != set(inventory):
            raise ValueError("Interpretation must cover the full source inventory and required album fields.")
        if cleaned["release_id"] is None and not cleaned["partial"]:
            raise ValueError("OFFLINE mapping cannot establish commercial release completeness.")
    return cleaned


def _known_totals(metadata):
    if not isinstance(metadata, dict):
        return {}
    known = {}
    for prefix in ("track", "disc"):
        observed, invalid = set(), False
        values, numbers = metadata.get(prefix + "_total", ()), metadata.get(prefix + "_number", ())
        if not isinstance(values, (list, tuple)) or not isinstance(numbers, (list, tuple)):
            continue
        try:
            observed.update(_positive(value, remote=True) for value in values)
            for value in numbers:
                if isinstance(value, str) and "/" in value:
                    parts = value.split("/")
                    if len(parts) != 2:
                        raise ValueError("Invalid embedded total.")
                    _positive(parts[0], remote=True)
                    observed.add(_positive(parts[1], remote=True))
        except ValueError:
            invalid = True
        if not invalid and len(observed) == 1:
            known[prefix + "_total"] = observed.pop()
    return known


def _numbering_totals(inventory, decision, release_tracks, metadata):
    selected, release_totals = {}, None
    if decision["release_id"] is not None:
        by_disc = {}
        for track in release_tracks:
            by_disc.setdefault(int(track["disc"]), set()).add(int(track["number"]))
        disc_total = len(by_disc)
        release_totals = {"disc_total": disc_total, "album_total": len(release_tracks),
                          "track_totals": {str(disc): len(numbers) for disc, numbers in sorted(by_disc.items())}}
        for relative, track in decision["tracks"].items():
            values, numbers = {}, by_disc[track["disc"]]
            if numbers == set(range(1, len(numbers) + 1)):
                values["track_total"] = len(numbers)
            if set(by_disc) == set(range(1, disc_total + 1)):
                values["disc_total"] = disc_total
            selected[relative] = values
        provenance = "release_tracklist"
    else:
        selected = decision.get("totals", {})
        provenance = "unavailable"
        if any(selected.values()):
            provenance = "existing_user_approved" if decision.get("retain_existing_totals") else "user_approved"
        if decision.get("retain_existing_totals"):
            for relative, values in selected.items():
                record = metadata.get(relative) if metadata is not None else tag.read_track_metadata(inventory[relative])
                current = _known_totals(record)
                if any(current.get(field) != value for field, value in values.items()):
                    raise ValueError("Retained existing totals changed; fresh review is required.")
    totals, deferred = {}, {}
    for relative, path in inventory.items():
        values = selected.get(relative, {})
        if values:
            totals[str(path)] = deepcopy(values)
        missing = [field for field in _TOTAL_FIELDS if field not in values]
        if missing:
            deferred[str(path)] = missing
    return {"release_totals": release_totals, "numbering_totals": totals,
            "numbering_deferred": deferred, "totals_provenance": provenance}


def _plan(inventory, decision, release_tracks=None, metadata=None):
    online = decision["release_id"] is not None
    if not online:
        release_tracks = decision.get("release_tracks")
        if release_tracks is None:
            release_tracks = [{"disc": str(track["disc"]), "number": str(track["number"]),
                               "title": track["title"], "recording_id": track["recording_id"]}
                              for track in decision["tracks"].values()]
        release_tracks = _tracklist(release_tracks, online=False)
    remote = {(int(track["disc"]), int(track["number"])): track for track in release_tracks}
    matched, identities = {}, {}
    for relative, track in decision["tracks"].items():
        position = (track["disc"], track["number"])
        target = remote.get(position)
        if target is None:
            raise ValueError(f"Explicit mapping position is absent from the tracklist: {relative}")
        identifier = track["recording_id"]
        if (online and identifier is None) or (target.get("recording_id") is not None
                                               and identifier != target["recording_id"]):
            raise ValueError(f"Selected recording ID is incompatible with the explicit mapping: {relative}")
        path = inventory[relative]
        matched[(str(position[0]), str(position[1]))] = path
        identities[str(path)] = tag.validate_recording_identity({
            "recording_id": identifier if identifier is not None else "user-approved-local",
            "title": track["title"], "artist": track["artist"],
            "verified": False, "user_approved": True})
    missing = [deepcopy(track) for position, track in remote.items()
               if (str(position[0]), str(position[1])) not in matched]
    result = {"matched": matched, "release_tracks": deepcopy(release_tracks), "compatible_ids": [],
            "identities": identities, "common": {field: decision[field] for field in ("album", "album_artist")},
            "manual_fields": {field: decision[field] for field in ("date", "genre") if field in decision},
            "partial": decision["partial"] or bool(missing) or not online, "provenance": "user_approved",
            "missing": missing, "release_tracks_provenance": "musicbrainz" if online else "user_supplied",
            "retain_existing_totals": decision.get("retain_existing_totals", False)}
    result.update(_numbering_totals(inventory, decision, release_tracks, metadata))
    return result


def prepare_interpretation(album, decision, source_fingerprint):
    """Validate a pending decision, never infer/remap identities or source positions.

    album['tracks'] must be the complete current metadata inventory. The caller
    must recompute source_fingerprint now, not replay the stored hash. Online
    selections require a fresh full tracklist; absent remote tracks stay missing.
    Saved online release_tracks are review snapshots, not a lookup bypass: changed
    snapshots require fresh review and never overwrite the original evaluations.
    Offline release_tracks are user-supplied positions, NOT edition/total evidence.
    numbering_totals maps absolute source strings to approved track_total/disc_total
    integers. Online release_totals covers the FULL release, including missing
    tracks/discs. Offline totals must be explicitly supplied in decision['totals']
    under relative source paths; retaining tags requires that approved snapshot.
    A nonempty numbering_deferred maps sources to missing required total fields:
    the parent must defer numbering/application, never fall back to local counts.
    """
    fingerprint = _fingerprint(source_fingerprint)
    if not isinstance(album, dict):
        raise ValueError("A current album source inventory is required.")
    inventory = _inventory(album.get("tracks"))
    decision = _validate_decision(decision, inventory, fingerprint, final=True, approved=True)
    tracks = _fetch_tracklist(decision["release_id"]) if decision["release_id"] is not None else None
    if tracks is not None and "release_tracks" in decision:
        saved = {(track["disc"], track["number"]): track for track in decision["release_tracks"]}
        current = {(track["disc"], track["number"]): track for track in tracks}
        if saved != current:
            raise ValueError("Selected release tracklist changed; fresh review is required.")
    metadata = dict(zip(inventory, album["tracks"]))
    return _plan(inventory, decision, tracks, metadata)


# UI helpers deliberately operate on one incremental draft, not automatic evidence.
class _Postpone(Exception):
    pass


def _ask(prompt):
    # Reserve zero even in free-text/advanced prompts so there is always a safe exit.
    prompt = prompt.rstrip().removesuffix(":")
    value = input(prompt + " [0 = postpone album; save draft]: ")
    if value.strip().casefold() in ("0", "postpone", "p"):
        raise _Postpone()
    return value


def _shown(value):
    return "".join(f"\\x{ord(char):02x}" if ord(char) < 32 or 127 <= ord(char) <= 159 else char
                   for char in str(value))


def _persist(issue, save):
    if save is not None:
        save(_json_safe(issue))


def _credit(details):
    value = details.get("artist-credit-phrase")
    if value:
        return value
    parts = []
    credits = details.get("artist-credit", [])
    if not isinstance(credits, list):
        return ""
    for credit in credits:
        if isinstance(credit, str):
            parts.append(credit)
        elif isinstance(credit, dict):
            artist = credit.get("artist")
            name = credit.get("name") or (artist.get("name", "") if isinstance(artist, dict) else "")
            joinphrase = credit.get("joinphrase", "")
            if isinstance(name, str) and isinstance(joinphrase, str):
                parts.append(name + joinphrase)
    return "".join(parts).strip()


def _candidates(row):
    evidence = row.get("evidence") or {}
    candidates = deepcopy((row.get("assessment") or {}).get("candidates", []))
    known = {item.get("recording_id") for item in candidates}
    for identifier, score in evidence.get("candidates", {}).items():
        if identifier not in known:
            candidates.append({"recording_id": identifier, "score": score})
    for candidate in candidates:
        if not isinstance(candidate.get("details"), dict):
            candidate["details"] = deepcopy(evidence.get("recordings", {}).get(candidate.get("recording_id")))
    return candidates



def _ranked_display_hints(row):
    """Sort visible hints by source-text relevance; NEVER use ranking as proof.

    The full, unchanged AcoustID candidate set remains in evidence and in the
    numbered recording selector. A source tag is a search/display hint only.
    """
    options = _candidates(row)
    current = row.get("existing") or {}
    title = current.get("title") or ()
    artist = current.get("artist") or current.get("album_artist") or ()
    source_title = lookup.normalize_title(title[0]) if len(title) == 1 and isinstance(title[0], str) else ""
    source_artist = artist[0].casefold().strip() if len(artist) == 1 and isinstance(artist[0], str) else ""

    def rank(candidate):
        details = candidate.get("details") or {}
        title_value = details.get("title")
        remote_title = lookup.normalize_title(title_value) if isinstance(title_value, str) else ""
        credit = _credit(details).casefold().strip()
        title_match = bool(source_title and remote_title and (
            source_title == remote_title or source_title.startswith(remote_title + " (")
            or source_title.startswith(remote_title + " - ")))
        artist_match = bool(source_artist and credit and (
            source_artist == credit or credit.startswith(source_artist + " feat")
            or credit.startswith(source_artist + " and ")))
        score = candidate.get("score")
        score = score if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score) else -1
        return (title_match and artist_match, title_match, artist_match,
                candidate.get("status") == "verified", score)

    return sorted(options, key=rank, reverse=True)

def suggested_identity(row):
    """Display hint only: one verified candidate, never a write authorization."""
    assessment = row.get("assessment") or {}
    if assessment.get("status") != "verified" or assessment.get("complete") is False:
        return None
    candidates = [c for c in _candidates(row)
                  if c.get("status") == "verified" and isinstance(c.get("details"), dict)
                  and c["details"].get("id") == c.get("recording_id")]
    if len(candidates) != 1:
        return None
    details = candidates[0]["details"]
    try:
        return {"title": _text(details.get("title"), "title"),
                "artist": _text(_credit(details), "artist")}
    except ValueError:
        return None


def consensus_identity(row):
    """Non-authoritative shared metadata across distinct eligible recording IDs.

    This is useful for names like Chromeo's 'Over Your Shoulder'. Different
    IDs still need release resolution; no single ID is silently selected.
    """
    assessment = row.get("assessment") or {}
    if assessment.get("complete") is not True or assessment.get("errors"):
        return None
    candidates = [option for option in _candidates(row)
                  if option.get("status") == "verified"
                  and isinstance(option.get("details"), dict)
                  and option["details"].get("id") == option.get("recording_id")]
    if len(candidates) < 2:
        return None
    try:
        titles = {_text(option["details"].get("title"), "title") for option in candidates}
        artists = {_text(_credit(option["details"]), "artist") for option in candidates}
    except ValueError:
        return None
    if (len({lookup.normalize_title(title) for title in titles}) != 1
            or len({artist.casefold() for artist in artists}) != 1):
        return None
    return {"title": sorted(titles)[0], "artist": sorted(artists)[0], "count": len(candidates)}


def show_tag_suggestions(rows):
    """Render source -> suggested edits without changing staged media or evidence."""
    print("\nSong tag suggestions (read-only; not yet approved):")
    changes = 0
    for index, row in enumerate(rows, 1):
        old = row.get("existing") or {}
        hint = suggested_identity(row)
        print(f"  {index}. {_shown(Path(str(row.get('path', 'unknown'))).name)}")
        if hint is None:
            consensus = consensus_identity(row)
            if consensus:
                print(f"     Metadata agrees across {consensus['count']} audio candidates: "
                      f"{_shown(consensus['title'])} — {_shown(consensus['artist'])} ")
                print("     Recording IDs still need an album position; no identity approved.")
                for field, label in (("title", "Title"), ("artist", "Artist")):
                    old_value = _hint(old, field)
                    if old_value != consensus[field]:
                        print(f"     {label}: {_shown(old_value)} -> {_shown(consensus[field])} "
                              "[candidate consensus; not verified]")
                continue
            candidates = _ranked_display_hints(row)
            print("     No unique verified identity; options below are ranked using unverified source tags.")
            for candidate in candidates[:3]:
                details = candidate.get("details") or {}
                title, artist = details.get("title"), _credit(details)
                if isinstance(title, str) and title.strip() and artist:
                    print(f"     Possible: {_shown(title)} — {_shown(artist)} (not approved)")
            if len(candidates) > 3:
                print(f"     +{len(candidates) - 3} more possible recordings in song review")
            continue
        for field, label in (("title", "Title"), ("artist", "Artist")):
            old_value, new_value = _hint(old, field), hint[field]
            if old_value != new_value:
                print(f"     {label}: {_shown(old_value)} -> {_shown(new_value)} [verified audio hint]")
                changes += 1
    print(f"{changes} possible changes displayed; no audio or source tags changed.")
    print("Edit or accept these in the guided song/album review, then explicitly approve.")


def _label(value, fallback="not available"):
    return _shown(value) if isinstance(value, str) and value.strip() else fallback


def _hint(metadata, field):
    values = metadata.get(field, ())
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return "not set"
    return "; ".join(_shown(value) for value in values if isinstance(value, str) and value.strip()) or "not set"


def _display_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _duration(value, *, milliseconds=False, difference=False):
    number = _display_number(value)
    if number is None:
        return "unavailable"
    if milliseconds:
        number /= 1000
    if difference:
        return "<0.1 seconds" if 0 < number < 0.1 else f"{number:.1f} seconds"
    minutes, seconds = divmod(round(number), 60)
    return f"{minutes}:{seconds:02d} ({number:.1f} seconds)"


def _explanation(codes):
    messages = {
        "low_confidence": "The audio match score is below the automatic safety threshold.",
        "invalid_score": "The audio match score is unavailable.",
        "invalid_audio_duration": "The source audio duration is unavailable.",
        "recording_details_unavailable": "A suggested recording is missing song or artist details.",
        "recording_duration_unavailable": "A suggested recording has no usable duration.",
        "duration_conflict": "The source and suggested recording have conflicting durations.",
        "multiple_eligible_recordings": "More than one recording could fit; none was chosen for you.",
        "no_recording_candidates": "No recording suggestion is available.",
        "recording_not_on_release": "The recording does not match this album release.",
        "release_membership_unavailable": "The recording's album membership could not be checked.",
        "incomplete_evidence": "Some identification evidence is missing.",
        "provider_error": "An online lookup did not finish.",
        "release_lookup_unavailable": "The album release could not be looked up.",
        "remote_tracklist_incomplete": "The online album tracklist is incomplete.",
        "missing_release_tracks": "Some album tracks have no matched source file.",
        "ambiguous_position_mapping": "The disc/track positions need explicit choices.",
        "unresolved_recording_identity": "At least one song identity still needs review.",
        "local_track_number_conflict": "Existing track numbers disagree with the album release.",
        "local_disc_number_conflict": "Existing disc numbers disagree with the album release.",
        "local_track_total_conflict": "Existing track totals disagree with the album release.",
        "local_disc_total_conflict": "Existing disc totals disagree with the album release.",
        "text_search_not_audio_verification": "Text search results are suggestions, not audio verification.",
    }
    explanations = []
    for code in codes if isinstance(codes, list) else []:
        message = messages.get(code) if isinstance(code, str) else None
        message = message or "Additional automatic checks need review; see advanced evidence."
        if message not in explanations:
            explanations.append(message)
    return " ".join(explanations)


def _lookup_incomplete(details):
    for row in details["tracks"]:
        assessment, evidence = row.get("assessment") or {}, row.get("evidence") or {}
        if (assessment.get("complete") is False or evidence.get("complete") is False
                or assessment.get("errors") or evidence.get("errors")
                or evidence.get("lookup_state") in ("partial", "unavailable")
                or any(code in (assessment.get("reasons") or []) for code in
                       ("provider_error", "incomplete_evidence", "recording_details_unavailable"))):
            return True
    return any(row.get("errors") or row.get("tracklist_complete") is False
               or "release_lookup_unavailable" in (row.get("reasons") or []) for row in details["releases"])


def _show_recordings(candidates, *, numbered=True):
    if not candidates:
        print("  No recording suggestions. Search online, enter details yourself, or postpone.")
    for index, candidate in enumerate(candidates, 1):
        details = candidate.get("details") or {}
        prefix = f"{index}." if numbered else f"Suggestion {index}:"
        print(f"  {prefix} {_label(details.get('title'), 'Title unavailable')}"
              f" — {_label(_credit(details), 'Artist unavailable')}")
        if details.get("disambiguation"):
            print(f"     Version: {_label(details['disambiguation'])}")
        print(f"     Duration: {_duration(details.get('length'), milliseconds=True)};"
              f" difference from source: {_duration(candidate.get('duration_difference'), difference=True)}.")
        score = _display_number(candidate.get("score"))
        if "text_search_not_audio_verification" in (candidate.get("reasons") or []):
            print(f"     Text search score: {f'{score:g}' if score is not None else 'unavailable'}"
                  " (not audio verification).")
        else:
            print(f"     Audio match score: {f'{score * 100:g}%' if score is not None and score <= 1 else 'unavailable'}"
                  " (not an approval).")
        explanation = _explanation(candidate.get("reasons", []))
        if explanation:
            print(f"     Caution: {explanation}")


def _show_release_tracks(tracks):
    multiple = any(str(track.get("disc", "1")) != "1" for track in tracks if isinstance(track, dict))
    for track in tracks:
        if isinstance(track, dict):
            prefix = f"Disc {_shown(track.get('disc', '?'))}, " if multiple else ""
            print(f"    {prefix}track {_shown(track.get('number', '?'))}:"
                  f" {_label(track.get('title'), 'Title unavailable')}")


def _release_label(release):
    identifier = release.get("release_id") or release.get("id")
    fallback = (f"Unlabeled release [{identifier[:8]}]"
                if isinstance(identifier, str) and identifier else "Unlabeled release")
    title = _label(release.get("title") or release.get("album"), fallback)
    extra = [_label(release[field]) for field in ("date", "country", "disambiguation") if release.get(field)]
    artist = _credit(release)
    return title + (f" — {_label(artist)}" if artist else "") + (f" ({'; '.join(extra)})" if extra else "")


def _show_compact_evidence(details):
    """A legible overview of large albums; never removes underlying evidence."""
    rows = details["tracks"]
    pending = [row for row in rows if (row.get("assessment") or {}).get(
        "status", row.get("status")) not in ("verified", "preliminary_match")]
    print(f"\nAlbum overview: {len(rows)} tracks; {len(pending)} recording identities need attention.")
    print(f"Release candidates recorded: {len(details['releases'])} (none chosen automatically).")
    print("Existing tags are hints, not independently verified identities.")
    print("Review one item at a time using option 1; choose an edition with option 2.")
    print("Complete evidence and all candidates remain available with option 6.")
    if _lookup_incomplete(details):
        print("Online evidence is incomplete or temporarily unavailable; retry later if unsure.")
        print("Missing provider evidence is not proof of a particular recording or release.")
    print("\nRecording checklist:")
    for index, row in enumerate(rows, 1):
        existing = row.get("existing") or {}
        title = _hint(existing, "title")
        if title == "not set":
            title = str(row.get("path", "Unknown source")).rsplit("/", 1)[-1]
        status = (row.get("assessment") or {}).get("status", row.get("status"))
        label = "review" if row in pending else "audio supported / release undecided"
        print(f"  {index:>2}. {_shown(title)} — {label}")
        if status not in ("verified", "preliminary_match"):
            candidates = _ranked_display_hints(row)
            names = []
            for option in candidates[:3]:
                candidate = option.get("details") or {}
                name = candidate.get("title")
                version = candidate.get("disambiguation")
                if isinstance(name, str) and name.strip():
                    names.append(name + (f" [{version}]" if isinstance(version, str) and version.strip() else ""))
            if names:
                print("      Suggestions (not approved): " + " | ".join(_shown(x) for x in names))
            if len(candidates) > 3:
                print(f"      +{len(candidates) - 3} more in the track's details")
    if len(details["releases"]) > 8:
        print("\nRelease list is paginated in option 2; unrelated candidates won't flood this screen.")


def _show_evidence(details):
    print("\nManual album review — USER-APPROVED choices are NOT independently verified")
    print("Automatic checks could not safely settle the song identities, album release or track positions.")
    print("Nothing here changes audio files. Choose a numbered action; 0 saves your draft and postpones.")
    if len(details["tracks"]) > 3:
        _show_compact_evidence(details)
        return
    if _lookup_incomplete(details):
        print("Online evidence is incomplete or unavailable. This may be a temporary service problem;")
        print("postpone and retry later if unsure. Missing evidence is not proof that a suggestion is right or wrong.")
    statuses = {"verified": "Automatic recording checks passed; the album release still needs review.",
                "probable": "Suggested match only; automatic recording checks are not conclusive.",
                "conflicting": "Recording evidence conflicts; do not approve on score alone.",
                "unidentified": "No confirmed recording identity."}
    for index, row in enumerate(details["tracks"], 1):
        print(f"\nSource {index}: {_shown(row['path'])}")
        existing = row.get("existing", {})
        print("  Existing tags are hints only, not verification:")
        multiple = (_known_totals(existing).get("disc_total", 1) > 1
                    or (tag.parse_disc_number(existing.get("disc_number", ()))[0] or 1) > 1)
        for field, label in (("title", "Title"), ("artist", "Artist"), ("album", "Album"),
                             ("album_artist", "Album artist"), ("track_number", "Track number"),
                             ("track_total", "Tracks on disc"), ("disc_number", "Disc number"),
                             ("disc_total", "Discs in album"), ("date", "Date"), ("genre", "Genre")):
            if field in ("disc_number", "disc_total") and not multiple:
                continue
            print(f"    {label}: {_hint(existing, field)}")
        assessment, evidence = row.get("assessment") or {}, row.get("evidence") or {}
        print("  " + statuses.get(assessment.get("status", row.get("status")), "Song identity needs review."))
        explanation = _explanation(assessment.get("reasons", []))
        if explanation:
            print("  " + explanation)
        print(f"  Source duration: {_duration((evidence.get('fingerprint') or {}).get('duration'))}")
        _show_recordings(_candidates(row), numbered=False)
    for index, release in enumerate(details["releases"], 1):
        print(f"\nAlbum release suggestion {index}: {_release_label(release)}")
        print(f"  {len(release.get('tracklist', []))} listed tracks;"
              f" {len(release.get('missing', []))} positions unmatched by automatic checks.")
        print("  " + _explanation(release.get("reasons", [])))
    print("\nFull IDs, provider errors and original evidence are available under 6. Advanced evidence.")


def _show_advanced(details, state):
    print("\nTechnical evidence (read-only; original automatic assessments are unchanged)")
    print("Raw MusicBrainz length values are milliseconds; formatted durations below are seconds.")
    for index, row in enumerate(details["tracks"], 1):
        print(f"\nSource {index}: {_shown(row['path'])}")
        _show_recordings(_candidates(row))
    payload = json.dumps(_json_safe({"evidence": details, "draft": state}), ensure_ascii=False, indent=2)
    for line in payload.split("\n"):
        print(_shown(line))
    print("Legacy text commands: track N, release, fields, totals, approve, advanced, postpone (p).")
    print("Candidate menus also accept search, id, manual (recording), offline (release), back.")
    print("Totals aliases: retain/yes, enter, defer/no, back. Optional fields: blank keeps, '-' removes the choice.")
    print("0 postpones at every prompt. Only exact APPROVE saves a manual approval.")


def _show_menu(details, state):
    ready = sum(_TRACK_FIELDS <= set(track) for track in state["tracks"].values())
    release = ("not chosen" if "release_id" not in state else
               "manual / no online release" if state["release_id"] is None else "online release chosen")
    print(f"\nDraft: {ready}/{len(details['tracks'])} source tracks mapped; album release: {release}.")
    print("1. Review track identities and disc/track positions")
    print("2. Choose album release (online or manual/offline)")
    print("3. Set album details (album, album artist, optional date/genre)")
    print("4. Set track/disc totals (manual/offline only)")
    print("5. Preview and approve choices (does not process audio)")
    print("6. Advanced evidence and text-command help")
    print("0. Postpone album — save draft without approval")
    if _lookup_incomplete(details) and "release_id" not in state:
        print("Recommended: 0 — postpone and retry later; online evidence is incomplete.")
    elif "release_id" not in state:
        print("Recommended: 2 — choose an album release, or deliberately choose manual/offline.")
    elif ready != len(details["tracks"]):
        print("Recommended: 1 — explicitly review every source track; no mapping is chosen for you.")
    elif any(field not in state for field in ("album", "album_artist")):
        print("Recommended: 3 — supply the album title and artist.")
    elif state["release_id"] is None and "retain_existing_totals" not in state:
        print("Recommended: 4 — choose known totals or deliberately leave them deferred.")
    else:
        print("Recommended: 5 — inspect the complete preview before deciding whether to approve.")


def _batch_supported_tracks(issue, details, state, save):
    """Offer one explicitly approved batch of recording/release mapping choices.

    Independently verified audio anchors and a COMPLETE user-selected release
    may resolve ambiguous recordings when exactly one eligible, unoccupied
    release position remains. No filenames/tags supply identities or positions.
    Never hide conflicting on-release evidence or network incompleteness.
    These are user-approved drafts, not automatic or independently verified
    choices; final processing still refreshes the chosen release and verifies
    the working copies before commit.
    """
    if state.get("release_id") is None or "release_tracks" not in state:
        print("Choose a complete online album release first; no positions will be guessed.")
        return 0
    try:
        tracklist = _tracklist(state["release_tracks"], online=True)
    except (ValueError, TypeError) as error:
        print(f"Cannot offer batch choices: {_shown(error)}")
        return 0
    by_recording = {}
    for remote in tracklist:
        by_recording.setdefault(remote["recording_id"], []).append(remote)
    occupied = {(track.get("disc"), track.get("number"))
                for track in state["tracks"].values()
                if "recording_id" in track and track.get("disc") is not None and track.get("number") is not None}

    # Only independent, uniquely established recording identities contribute
    # context. Colliding anchor IDs or positions are not treated as proof.
    anchors_by_identity = {}
    for row in details["tracks"]:
        assessment = row.get("assessment") or {}
        if (assessment.get("status") != "verified" or assessment.get("complete") is not True
                or assessment.get("release_id") or assessment.get("errors")):
            continue
        verified = [candidate for candidate in _candidates(row)
                    if candidate.get("status") == "verified"
                    and isinstance(candidate.get("details"), dict)
                    and candidate["details"].get("id") == candidate.get("recording_id")]
        if len(verified) != 1:
            continue
        identifier = verified[0]["recording_id"]
        remote = by_recording.get(identifier, [])
        if len(remote) != 1:
            continue
        position = (int(remote[0]["disc"]), int(remote[0]["number"]))
        anchors_by_identity.setdefault(identifier, []).append((row["path"], position))

    unique_anchors = {identifier: values[0] for identifier, values in anchors_by_identity.items()
                      if len(values) == 1}
    position_counts = {}
    for _, position in unique_anchors.values():
        position_counts[position] = position_counts.get(position, 0) + 1
    unique_anchors = {identifier: pair for identifier, pair in unique_anchors.items()
                      if position_counts[pair[1]] == 1}
    anchored_positions = {position for _, position in unique_anchors.values()}

    proposed = []
    for row in details["tracks"]:
        relative = row["path"]
        previous = state["tracks"].get(relative, {})
        if "recording_id" in previous:
            continue
        assessment = row.get("assessment") or {}
        if (assessment.get("complete") is not True or assessment.get("errors")
                or assessment.get("release_id") or (row.get("evidence") or {}).get("errors")):
            continue
        options = _candidates(row)
        # Any eligible recording on this release with unknown duration/details
        # makes the proposed identity undecidable, not negative evidence.
        uncertain = any(candidate.get("status") == "probable"
                        and candidate.get("recording_id") in by_recording
                        and "low_confidence" not in (candidate.get("reasons") or [])
                        for candidate in options)
        if uncertain:
            continue
        possible = []
        for candidate in options:
            if candidate.get("status") != "verified":
                continue
            identifier = candidate.get("recording_id")
            details_row = candidate.get("details")
            if not isinstance(details_row, dict) or details_row.get("id") != identifier:
                continue
            positions = by_recording.get(identifier, [])
            if len(positions) != 1:
                continue
            position = (int(positions[0]["disc"]), int(positions[0]["number"]))
            if (position in occupied or any(previous.get(field, value) != value
                    for field, value in (("disc", position[0]), ("number", position[1])))):
                continue
            # Other independently verified source tracks already claim these
            # positions, so this file must not displace their identity.
            if position in anchored_positions and unique_anchors.get(identifier, (None,))[0] != relative:
                continue
            possible.append((identifier, position, details_row))
        if len(possible) != 1:
            continue
        identifier, position, details_row = possible[0]
        # A disagreeing candidate set is ambiguous even if an older audit
        # incorrectly labelled its top-level status 'verified'.
        verified_count = sum(candidate.get("status") == "verified" for candidate in options)
        # An ambiguous audio result needs at least two OTHER independent
        # recording anchors, distinct from this source/identity.
        if assessment.get("status") != "verified" or verified_count != 1:
            independent_others = {key for key, (path, _) in unique_anchors.items()
                                  if path != relative and key != identifier}
            if len(independent_others) < 2:
                continue
        try:
            title = _text(details_row.get("title"), "title")
            artist = _text(_credit(details_row), "artist")
        except ValueError:
            continue
        proposed.append((relative, {
            "recording_id": identifier,
            "title": title, "artist": artist,
            "disc": position[0], "number": position[1], **previous
        }, assessment.get("status") != "verified"))
        occupied.add(position)
    if not proposed:
        print("No uniquely supported, unmapped tracks can be filled from this release.")
        return 0
    context_count = sum(context for _, _, context in proposed)
    print(f"\n{len(proposed)} unmapped tracks have a unique audio-supported position on the selected release"
          f" ({context_count} resolved by independent album context):")
    for relative, track, context in proposed:
        name = relative.rsplit("/", 1)[-1]
        label = " [album-context resolution]" if context else ""
        print(f"  Disc {track['disc']}, #{track['number']}: {_shown(name)} -> "
              f"{_shown(track['title'])} — {_shown(track['artist'])}{label}")
    print("This is YOUR approval, not automatic independent verification.")
    print("Remaining ambiguous clean/explicit/remix identities and unknown positions need individual review.")
    print("Source audio is untouched; final processing will revalidate the release.")
    answer = _ask("Type APPROVE BATCH to save these proposed mappings (anything else cancels)")
    if answer != "APPROVE BATCH":
        print("Batch choice cancelled; existing draft retained.")
        return 0
    for relative, track, _ in proposed:
        state["tracks"][relative] = track
    _persist(issue, save)
    print(f"Saved {len(proposed)} draft track mappings; no processing started.")
    return len(proposed)


def _review_tracks(issue, details, state, save):
    print("\nChoose a source track to review (existing tag names are hints only):")
    for index, row in enumerate(details["tracks"], 1):
        existing = _hint(row.get("existing") or {}, "title")
        hint = suggested_identity(row)
        suggestion = f" -> {hint['title']}" if hint and hint["title"] != existing else ""
        saved = " [draft saved]" if row["path"] in state["tracks"] else ""
        print(f"{index}. {_shown(row['path'])} | {_shown(existing)}{_shown(suggestion)}{saved}")
    back = str(len(details["tracks"]) + 1)
    print(f"{back}. Back to album review")
    print("A. Batch-fill audio-supported tracks using the selected release (explicit approval)")
    print("0. Postpone album — save draft")
    while True:
        choice = _ask("Choose source track").strip().casefold()
        if choice in (back, "back"):
            return
        if choice in ("a", "batch"):
            _batch_supported_tracks(issue, details, state, save)
            return
        if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(details["tracks"]):
            _review_track(issue, details["tracks"][int(choice) - 1], state, save)
            return
        print("Choose a source number, the back option, or 0 to postpone.")


def _prompt_text(prompt, current=None):
    while True:
        value = _ask(prompt + (f" [{_shown(current)}; Enter = keep]" if current else " (type a value)"))
        try:
            return _text(value if value else current, prompt)
        except ValueError as error:
            print(error)


def _prompt_number(prompt, current=None):
    while True:
        value = _ask(prompt + (f" [{current}; Enter = keep]" if current else " (type a positive whole number)"))
        try:
            return _positive(value if value else current, remote=True)
        except ValueError as error:
            print(error)


def _single_disc_review(state, rows=()):
    """Hide disc prompts only when release/tag evidence actually settles them."""
    if any(track.get("disc", 1) != 1 for track in state.get("tracks", {}).values()):
        return False
    totals = list((state.get("totals") or {}).values())
    if any(values.get("disc_total", 1) != 1 for values in totals):
        return False
    tracks = state.get("release_tracks")
    if state.get("release_id") and tracks:
        return all(str(track.get("disc")) == "1" for track in tracks)
    known = [_known_totals(row).get("disc_total") for row in rows]
    return bool(known) and set(known) == {1}


def _review_track(issue, row, state, save):
    relative = row["path"]
    options = _candidates(row)
    print(f"\nReviewing source: {_shown(relative)}")
    print("Confirm the song and credited artist yourself; an audio score alone is not approval.")
    while True:
        _show_recordings(options)
        actions = {str(len(options) + index): action
                   for index, action in enumerate(("search", "manual", "id", "back"), 1)}
        for number, label in zip(actions, ("Search by artist and song title (online)",
                                           "Enter song title and artist yourself (manual)",
                                           "Advanced: enter a MusicBrainz recording ID", "Back to album review")):
            print(f"{number}. {label}")
        print("E. Edit existing draft title, artist and disc/track number")
        print("0. Postpone album — save draft")
        print("Recommended: 0 if unsure; compare the song, artist, version and duration before choosing.")
        choice = _ask("Choose recording action").strip()
        choice = actions.get(choice, choice)
        details, identifier = {}, None
        if choice.casefold() == "back":
            return
        if choice.casefold() in ("e", "edit"):
            if relative not in state["tracks"]:
                print("Choose a candidate or manual recording first; no saved draft for this song.")
                continue
            track = state["tracks"][relative]
            for field in ("title", "artist"):
                track[field] = _prompt_text(field.capitalize(), track.get(field))
                _persist(issue, save)
            disc = (1 if _single_disc_review(state, [row.get("existing") or {}]) else
                    _prompt_number("Disc number", track.get("disc")))
            if disc != track.get("disc"):
                track.pop("number", None)
            track["disc"] = disc
            _persist(issue, save)
            while True:
                number = _prompt_number("Track number on this disc", track.get("number"))
                if any(key != relative and other.get("disc") == disc and other.get("number") == number
                       for key, other in state["tracks"].items()):
                    print("Each song needs a unique disc/track position.")
                    continue
                track["number"] = number
                _persist(issue, save)
                return
        if choice.casefold() == "search":
            artist = _prompt_text("Recording artist")
            title = _prompt_text("Recording title")
            try:
                results = lookup.search_recording(artist, title)
                if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
                    raise ValueError("Malformed recording search results.")
                options = [{"recording_id": item.get("id"), "details": item,
                            "score": item.get("ext:score", item.get("score")),
                            "reasons": ["text_search_not_audio_verification"]} for item in results]
            except AssertionError:
                raise
            except Exception as error:
                print(f"Recording search unavailable: {_shown(error)}")
            continue
        try:
            if choice.casefold() == "id":
                identifier = _prompt_text("Recording ID")
                details = lookup.get_recording_details(identifier)
                if not isinstance(details, dict) or details.get("id") != identifier:
                    raise ValueError("Mismatched recording details.")
            elif choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(options):
                candidate = options[int(choice) - 1]
                identifier = _text(candidate.get("recording_id"), "recording ID")
                details = candidate.get("details")
                if not isinstance(details, dict):
                    details = lookup.get_recording_details(identifier)
                if not isinstance(details, dict) or details.get("id") != identifier:
                    raise ValueError("Mismatched recording details.")
            elif choice.casefold() != "manual":
                print("Choose a numbered recording or action, or 0 to postpone.")
                continue
        except (AssertionError, _Postpone):
            raise
        except Exception as error:
            print(f"Recording selection unavailable: {_shown(error)}")
            continue
        track = state["tracks"].setdefault(relative, {})
        track["recording_id"] = identifier
        for field, hint in (("title", details.get("title")), ("artist", _credit(details))):
            if hint:
                try:
                    track.setdefault(field, _text(hint, field))
                except ValueError:
                    print(f"Unsafe provider {field} ignored; enter it explicitly.")
        _persist(issue, save)
        for field in ("title", "artist"):
            track[field] = _prompt_text(field.capitalize(), track.get(field))
            _persist(issue, save)
        # Suggest only a unique position from the release the user SELECTED.
        if identifier and state.get("release_id") and isinstance(state.get("release_tracks"), list):
            positions = [remote for remote in state["release_tracks"]
                         if remote.get("recording_id") == identifier]
            if len(positions) == 1:
                try:
                    disc_hint = _positive(positions[0].get("disc"), remote=True)
                    number_hint = _positive(positions[0].get("number"), remote=True)
                    if not any(key != relative and other.get("disc") == disc_hint
                               and other.get("number") == number_hint
                               for key, other in state["tracks"].items()):
                        prefix = f"disc {disc_hint}, " if not _single_disc_review(state) else ""
                        print(f"Selected release suggests {prefix}track {number_hint}.")
                        track.setdefault("disc", disc_hint)
                        track.setdefault("number", number_hint)
                except ValueError:
                    pass
        single = _single_disc_review(state, [row.get("existing") or {}])
        print("Confirm the track number; the suggestion is NOT automatically approved." if single else
              "Confirm the disc and track number; the suggestion is NOT automatically approved.")
        disc = 1 if single else _prompt_number("Disc number", track.get("disc"))
        if disc != track.get("disc"):
            track.pop("number", None)
        track["disc"] = disc
        _persist(issue, save)
        while True:
            number = _prompt_number("Track number on this disc", track.get("number"))
            if any(key != relative and other.get("disc") == disc and other.get("number") == number
                   for key, other in state["tracks"].items()):
                print("Every source needs a unique disc/track position.")
                continue
            track["number"] = number
            _persist(issue, save)
            return


def _review_release(issue, details, state, save):
    options = details["releases"]
    print("\nChoose the album release/edition, not just a similar album title.")
    page = 0
    page_size = 8
    while True:
        total_pages = max(1, (len(options) + page_size - 1) // page_size)
        page = min(page, total_pages - 1)
        start = page * page_size
        print(f"\nRelease suggestions: {len(options)} — page {page + 1}/{total_pages}")
        for index in range(start, min(len(options), start + page_size)):
            option = options[index]
            print(f"  {index + 1}. {_release_label(option)}")
            print(f"     {len(option.get('tracklist') or [])} listed tracks; "
                  f"{len(option.get('missing') or [])} unmatched; "
                  f"status: {_label(option.get('status'), 'not assessed')}")
        if not options:
            print("No album release suggestions are available.")
        elif total_pages > 1:
            print("  n. Next page     b. Previous page     (a release number works from any page)")
        actions = {str(len(options) + index): action
                   for index, action in enumerate(("search", "offline", "id", "back"), 1)}
        for number, label in zip(actions, ("Search by album artist and title (online)",
                                           "Use manual album details without an online release (OFFLINE)",
                                           "Advanced: enter a MusicBrainz release ID", "Back to album review")):
            print(f"{number}. {label}")
        print("0. Postpone album — save draft")
        print("Recommended: 0 if the edition is uncertain; manual/offline is only for choices you can supply yourself.")
        choice = _ask("Choose album release action").strip()
        if choice.casefold() in ("n", "next"):
            page = min(total_pages - 1, page + 1)
            continue
        if choice.casefold() in ("b", "previous"):
            page = max(0, page - 1)
            continue
        choice = actions.get(choice, choice)
        if choice.casefold() == "back":
            return
        if choice.casefold() == "offline":
            if state.get("release_id") is not None:
                state.pop("release_tracks", None)
            state["release_id"] = None
            state["partial"] = True
            _persist(issue, save)
            print("OFFLINE manual interpretation: USER-APPROVED, NOT independently verified.")
            print("User-supplied mapping does not establish commercial release completeness, edition, date or genre.")
            return
        if choice.casefold() == "search":
            artist = _prompt_text("Album artist query")
            album = _prompt_text("Album query")
            try:
                results = lookup.search_album(artist, album)
                if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
                    raise ValueError("Malformed release search results.")
                options = results
            except AssertionError:
                raise
            except Exception as error:
                print(f"Release search unavailable: {_shown(error)}")
            continue
        if choice.casefold() == "id":
            identifier = _prompt_text("Release ID")
        elif choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(options):
            identifier = options[int(choice) - 1].get("release_id", options[int(choice) - 1].get("id"))
        else:
            print("Choose a numbered album release or action, or 0 to postpone.")
            continue
        try:
            identifier = _text(identifier, "release ID")
            tracks = _fetch_tracklist(identifier)
        except ValueError as error:
            print(_shown(error))
            continue
        metadata = {}
        try:
            metadata = lookup.get_release_metadata(identifier)
            if not isinstance(metadata, dict) or metadata.get("release_id") != identifier:
                raise ValueError("Mismatched release metadata.")
        except AssertionError:
            raise
        except Exception as error:
            print(f"Release metadata unavailable; enter fields explicitly: {_shown(error)}")
            metadata = {}
        state["release_id"] = identifier
        state["release_tracks"] = _json_safe(tracks)
        state.pop("retain_existing_totals", None)
        state.pop("totals", None)
        # Coverage is recomputed at approval, not inferred from a release ID.
        state["partial"] = False
        for field in ("album", "album_artist", "date", "genre"):
            if field not in state and metadata.get(field):
                try:
                    value = _text(metadata[field], field)
                    if field in ("date", "genre"):
                        if field == "genre" and value.casefold() == "unknown":
                            continue
                        value = tag.validate_manual_metadata({field: value})[field]
                    state[field] = value
                except ValueError:
                    pass
        _persist(issue, save)
        print("Online album release selected. Map EVERY source explicitly; no automatic remapping.")
        _show_release_tracks(tracks)
        print("Known album fields filled from the selected release; final APPROVE is still required.")
        return


def _review_fields(issue, state, save):
    for field, prompt in (("album", "Album"), ("album_artist", "Album artist")):
        state[field] = _prompt_text(prompt, state.get(field))
        _persist(issue, save)
    for field in ("date", "genre"):
        while True:
            print(f"\nOptional {field}: {_label(state.get(field), 'no manual choice')}")
            print("1. Keep this choice / leave unset (recommended if unknown)")
            print(f"2. Enter a {field}" + (" (YYYY, YYYY-MM or YYYY-MM-DD)" if field == "date" else ""))
            print("3. Remove this manual choice (does not erase existing source tags)")
            print("0. Postpone album — save draft")
            answer = _ask(f"Choose {field} action")
            choice = answer.strip()
            if choice in ("1", ""):
                value = ""
            elif choice in ("3", "-"):
                state.pop(field, None)
                value = ""
            elif choice == "2":
                value = _prompt_text(f"Enter {field}", state.get(field))
            else:
                # Validate the original text, including any unsafe control characters.
                value = answer
            if value:
                try:
                    value = _text(value, field)
                    if field == "genre" and value.casefold() == "unknown":
                        raise ValueError("Genre is optional; leave it unset instead of entering Unknown.")
                    state.update(tag.validate_manual_metadata({field: value}))
                except ValueError as error:
                    print(error)
                    continue
            _persist(issue, save)
            break


def _review_totals(issue, inventory, state, save):
    print("OFFLINE totals are explicit user choices, not commercial release completeness evidence.")
    while True:
        print("1. Keep unambiguous existing tag totals (explicitly approved hints, not release proof)")
        print("2. Enter known full-disc track totals and album disc totals yourself")
        print("3. Leave totals unknown / deferred (recommended if unsure; processing must wait)")
        print("4. Back to album review")
        print("0. Postpone album — save draft")
        choice = _ask("Choose totals action").strip().casefold()
        choice = {"1": "retain", "2": "enter", "3": "defer", "4": "back"}.get(choice, choice)
        if choice == "back":
            return
        if choice in ("defer", "no"):
            state.pop("totals", None)
            state["retain_existing_totals"] = False
            _persist(issue, save)
            print("Required totals deferred; no totals will be inferred from the local mapping.")
            return
        if choice in ("retain", "yes"):
            totals = {}
            for relative, path in inventory.items():
                known = _known_totals(tag.read_track_metadata(path))
                if known:
                    totals[relative] = known
                print(f"  {_shown(relative)}: tracks on disc: {known.get('track_total', 'unknown')};"
                      f" discs in album: {known.get('disc_total', 'unknown')}. Other totals deferred.")
            state.update(totals=totals, retain_existing_totals=True)
            _persist(issue, save)
            return
        if choice == "enter":
            single = _single_disc_review(state, [tag.read_track_metadata(path) for path in inventory.values()])
            state.setdefault("totals", {})
            state["retain_existing_totals"] = False
            _persist(issue, save)
            for relative in inventory:
                print(f"User-supplied totals for {_shown(relative)}; Enter leaves that required total unknown.")
                for field, label in (("track_total", "Tracks on this disc"), ("disc_total", "Discs in the album")):
                    if single and field == "disc_total":
                        state["totals"].setdefault(relative, {})[field] = 1
                        _persist(issue, save)
                        continue
                    while True:
                        answer = _ask(f"{label} (positive whole number; Enter = defer)").strip()
                        if not answer:
                            values = state["totals"].get(relative, {})
                            values.pop(field, None)
                            if not values:
                                state["totals"].pop(relative, None)
                        else:
                            try:
                                value = _positive(answer, remote=True)
                            except ValueError as error:
                                print(error)
                                continue
                            state["totals"].setdefault(relative, {})[field] = value
                        _persist(issue, save)
                        break
            return
        print("Choose a numbered totals action, or 0 to postpone.")


def _issue_inventory(issue):
    if not isinstance(issue, dict):
        raise ValueError("A structured review issue is required.")
    details = issue.get("details", issue)
    if not isinstance(details, dict) or details.get("kind") != "identity_release":
        raise ValueError("An identity_release issue is required.")
    inventory = _inventory(details.get("tracks"))
    if not isinstance(details.get("releases"), list) or any(not isinstance(row, dict) for row in details["releases"]):
        raise ValueError("Malformed release evidence.")
    for row in details["tracks"]:
        _source_path(row["path"])
        for key in ("assessment", "evidence", "existing"):
            if key in row and not isinstance(row[key], dict):
                raise ValueError("Malformed recording evidence.")
        if not isinstance((row.get("assessment") or {}).get("candidates", []), list):
            raise ValueError("Malformed recording candidates.")
        if any(not isinstance(candidate, dict) for candidate in (row.get("assessment") or {}).get("candidates", [])):
            raise ValueError("Malformed recording candidate.")
        evidence = row.get("evidence") or {}
        if evidence.get("fingerprint") is not None and not isinstance(evidence["fingerprint"], dict):
            raise ValueError("Malformed fingerprint evidence.")
        for field in ("candidates", "recordings"):
            if not isinstance(evidence.get(field, {}), dict):
                raise ValueError("Malformed recording evidence map.")
        for candidate in (row.get("assessment") or {}).get("candidates", []):
            if candidate.get("details") is not None and not isinstance(candidate["details"], dict):
                raise ValueError("Malformed recording details.")
        if any(not isinstance(value, dict) for value in evidence.get("recordings", {}).values()):
            raise ValueError("Malformed recording details map.")
    for release in details["releases"]:
        for field in ("missing", "review", "reasons", "tracklist"):
            if not isinstance(release.get(field, []), list):
                raise ValueError("Malformed release evidence list.")
    _json_safe(details)
    if details is not issue and "tracks" in issue:
        outer = issue.get("tracks")
        if (not isinstance(outer, list) or any(not isinstance(path, str) for path in outer)
                or len(outer) != len(inventory) or set(outer) != set(inventory)):
            raise ValueError("Issue and evidence must contain the same full source inventory.")
    return details, inventory


def _draft_tracklist(details, state):
    if state["release_id"] is None:
        return None
    if "release_tracks" in state:
        return _tracklist(state["release_tracks"], online=True)
    selected = next((row for row in details["releases"] if row.get("release_id") == state["release_id"]), None)
    if selected is None or selected.get("tracklist_complete", True) is not True:
        raise ValueError("Select a release with a complete full tracklist, or choose OFFLINE.")
    return _tracklist(selected.get("tracklist"), online=True)


def _show_plan(state, plan):
    print("\nUSER-APPROVED interpretation preview — NOT independently verified")
    print("Approval saves your choices for later processing; it does not write audio or bypass safety checks.")
    print(f"Album: {_shown(state['album'])}; album artist: {_shown(state['album_artist'])}")
    print("Release: " + ("online release selected" if state["release_id"] is not None else "OFFLINE / no release"))
    print("Album coverage: " + ("partial / completeness not established" if plan["partial"] else "all release positions mapped"))
    for field in ("date", "genre"):
        unset = ("not set / unresolved if absent; no genre inferred; existing tags unchanged" if field == "genre"
                 else "no manual change; existing tags stay unchanged")
        print(f"{field.capitalize()}: {_label(plan['manual_fields'].get(field), unset)}")
    totals_values = list(plan["numbering_totals"].values())
    single = (all(track["disc"] == 1 for track in state["tracks"].values())
              and len(totals_values) == len(state["tracks"])
              and all(values.get("disc_total") == 1 for values in totals_values))
    labels = ({"track_total": "tracks"} if single else
              {"track_total": "tracks on disc", "disc_total": "discs in album"})
    for relative, track in state["tracks"].items():
        prefix = "" if single else f"disc {track['disc']}, "
        print(f"  {_shown(relative)} -> {prefix}track {track['number']}: {_shown(track['title'])}"
              f" / {_shown(track['artist'])}")
        path = str(Path(tag.staging_folder).absolute() / relative)
        totals = plan["numbering_totals"].get(path, {})
        print("    Totals: " + "; ".join(f"{label}: {totals.get(field, 'unknown — deferred')}"
                                        for field, label in labels.items()))
    full = plan["release_totals"]
    if full is None:
        print("Full release totals: not established by an online release.")
    else:
        if full["disc_total"] == 1:
            print(f"Full release total: {full['album_total']} tracks, including unimported tracks.")
        else:
            print(f"Full release totals: {full['album_total']} tracks across {full['disc_total']} discs, including unimported tracks.")
            for disc, total in full["track_totals"].items():
                print(f"  Disc {_shown(disc)}: {total} tracks")
    provenance = {"release_tracklist": "full online release tracklist", "existing_user_approved": "existing tags explicitly retained",
                  "user_approved": "explicit user-supplied totals", "unavailable": "not available"}
    print(f"Numbering totals source: {provenance[plan['totals_provenance']]}")
    print(f"Required numbering deferred: {len(plan['numbering_deferred'])} source track(s)")
    if plan["numbering_deferred"]:
        print("Application must defer required numbering; totals are NEVER inferred from local file counts.")
    print(f"Missing tracklist positions: {len(plan['missing'])}")
    _show_release_tracks(plan["missing"])
    if state["release_id"] is None:
        print("OFFLINE positions are user-supplied, not proof of commercial release completeness.")
        print("Retain existing known totals: " + ("yes" if plan["retain_existing_totals"] else "no"))


@lookup.musicbrainz_album()
def choose_interpretation(issue, source_fingerprint, save=None):
    """Edit issue['interpretation']; save(issue) checkpoints drafts and final choice.

    Returns True ONLY after exact APPROVE. Any other final response, postpone,
    EOF or Ctrl-C returns False with the incremental draft intact. Invalid/stale
    saved state is left untouched. Persistence errors propagate, and a failed
    final save restores draft status. Accepts a queue envelope or bare details.
    Normal actions are numbered (1 tracks, 2 release, 3 fields, 4 totals, 5 preview /
    approval, 6 advanced evidence). Zero postpones at EVERY prompt. Legacy text
    commands remain accepted but are only advertised in the advanced view.
    """
    try:
        fingerprint = _fingerprint(source_fingerprint)
        details, inventory = _issue_inventory(issue)
        if save is not None and not callable(save):
            raise ValueError("save must be a callback accepting the updated issue.")
        if "source_fingerprint" in issue and issue["source_fingerprint"] != fingerprint:
            raise ValueError("Source fingerprint changed; fresh review is required.")
        if "interpretation" in issue:
            previous = issue["interpretation"]
            approved = isinstance(previous, dict) and previous.get("status") == "pending_application"
            state = _validate_decision(previous, inventory, fingerprint, final=approved, approved=approved)
            state.update(status="draft", user_approved=False)
        else:
            state = {"status": "draft", "user_approved": False, "independently_verified": False,
                     "source_fingerprint": fingerprint, "tracks": {}, "partial": True}
            edits = issue.get("tag_edits") or {}
            if (not isinstance(edits, dict) or not isinstance(edits.get("album", {}), dict)
                    or not isinstance(edits.get("tracks", {}), dict)
                    or any(not isinstance(choices, dict) for choices in edits.get("tracks", {}).values())):
                raise ValueError("Malformed saved field edits; fresh review is required.")
            shared = edits.get("album") or {}
            manual = tag.validate_manual_metadata(issue.get("manual_fields"))
            for field in ("album", "album_artist", "date", "genre"):
                if field in manual or field in shared:
                    state[field] = manual.get(field, shared.get(field))
            for relative, choices in (edits.get("tracks") or {}).items():
                track = {target: _positive(choices[source], remote=True) if target in ("disc", "number")
                         else choices[source] for source, target in
                         (("title", "title"), ("artist", "artist"), ("disc_number", "disc"), ("track_number", "number"))
                         if source in choices}
                if "artist" in shared and "artist" not in track:
                    track["artist"] = shared["artist"]
                if track:
                    state["tracks"][relative] = track
            totals = {}
            for relative in inventory:
                if shared.get("disc_layout") == "single":
                    state["tracks"].setdefault(relative, {})["disc"] = 1
                values = {field: _positive(shared[field], remote=True) for field in _TOTAL_FIELDS
                          if field in shared and (field != "track_total" or shared.get("disc_layout") != "multiple")}
                if values:
                    if shared.get("disc_layout") == "single":
                        values.setdefault("disc_total", 1)
                    totals[relative] = values
            if totals:
                state.update(totals=totals, retain_existing_totals=False)
            state = _validate_decision(state, inventory, fingerprint)
    except (OSError, ValueError, TypeError) as error:
        print(f"Interpretation needs fresh review: {_shown(error)}")
        return False
    issue["interpretation"] = state
    issue["source_fingerprint"] = fingerprint
    _persist(issue, save)
    _show_evidence(details)
    try:
        while True:
            _show_menu(details, state)
            command = _ask("Choose review action").strip().casefold()
            command = {"1": "tracks", "2": "release", "3": "fields", "4": "totals",
                       "5": "approve", "6": "advanced"}.get(command, command)
            match = re.fullmatch(r"track ([0-9]+)", command)
            if command == "tracks":
                _review_tracks(issue, details, state, save)
            elif match:
                index = int(match.group(1))
                if not 1 <= index <= len(details["tracks"]):
                    print("Select a source inventory number shown above.")
                    continue
                _review_track(issue, details["tracks"][index - 1], state, save)
            elif command == "release":
                _review_release(issue, details, state, save)
            elif command == "fields":
                _review_fields(issue, state, save)
            elif command == "advanced":
                _show_advanced(details, state)
            elif command == "totals":
                if "release_id" not in state or state["release_id"] is not None:
                    print("Choose a manual/OFFLINE release first (option 2) to set totals yourself.")
                    print("Online totals must come from the full selected release, never local file counts.")
                    continue
                _review_totals(issue, inventory, state, save)
            elif command == "approve":
                try:
                    cleaned = _validate_decision(state, inventory, fingerprint, final=True)
                    plan = _plan(inventory, cleaned, _draft_tracklist(details, cleaned))
                except (OSError, ValueError) as error:
                    print(f"Cannot approve interpretation: {_shown(error)}")
                    continue
                state["partial"] = plan["partial"]
                _persist(issue, save)
                _show_plan(state, plan)
                if _ask("Type APPROVE to save your manual approval for later processing (anything else postpones)") != "APPROVE":
                    print("Postponed; draft retained, no approval or audio writes.")
                    return False
                approved = deepcopy(cleaned)
                approved.update(status="pending_application", user_approved=True, partial=plan["partial"])
                issue["interpretation"] = approved
                try:
                    _persist(issue, save)
                except BaseException:
                    # Interruptions must roll back pending status before draft persistence.
                    issue["interpretation"] = state
                    raise
                print("Interpretation approval saved; staged originals unchanged. NOT independently verified.")
                return True
            else:
                print("Choose a numbered action above, or 0 to save the draft and postpone.")
    except (_Postpone, EOFError, KeyboardInterrupt):
        _persist(issue, save)
        print("Postponed; incremental draft retained, no audio files modified.")
        return False
