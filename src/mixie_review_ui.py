"""MIXIE's simple, offline-first review manager (v4).

This module only saves *choices*. Source audio stays immutable; the original
preview/commit engine remains responsible for identity, release and archive proof.
"""
import hashlib
import os
import json
from pathlib import Path
import shlex
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from io import BytesIO

from PIL import Image
import mixie_artwork
import mixie_terminal as terminal

# Uniform violet/cyan headers, menu entries, and input prompts.
print = terminal.themed_print
input = terminal.themed_input


ALBUM_FIELDS = (("album", "Album"), ("artist", "Artist"),
                ("album_artist", "Album Artist"), ("genre", "Genre"),
                ("date", "Release Date"), ("track_total", "Track Total"),
                ("disc_layout", "Disc Layout"), ("disc_total", "Disc Total"))
TRACK_FIELDS = (("title", "Title"), ("track_number", "Track Number"),
                ("disc_number", "Disc Number"), ("artist", "Artist"))
SINGLE_DISC_FIELDS = TRACK_FIELDS


# One source manifest is enough for one READ-ONLY screen render. Never reuse
# this cache after the user can change files, submit a decision or initiate
# an import: all approval/commit paths still compute fresh SHA-256 evidence.
_render_fingerprints = ContextVar('mixie_review_display_fingerprints', default=None)


@contextmanager
def _review_render_scope():
    token = _render_fingerprints.set({})
    try:
        yield
    finally:
        _render_fingerprints.reset(token)


def _read_only_fingerprint(service, issue):
    cache = _render_fingerprints.get()
    if cache is None:
        return service.review_fingerprint(issue)
    key = (id(service), id(issue))
    if key not in cache:
        cache[key] = service.review_fingerprint(issue)
    return cache[key]


def clean_value(field, value):
    """Reject control characters and clearly invalid user-provided tag values."""
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("Enter a nonempty value no longer than 256 characters.")
    result = value.strip()
    if field in ("artist", "album_artist") and result.isascii() and result.isdecimal():
        raise ValueError("An artist credit cannot be only a number. Choose Track Number to edit numbering.")
    if field == "disc_layout":
        options = {"a": "auto", "auto": "auto", "s": "single", "single": "single",
                   "m": "multiple", "multiple": "multiple"}
        if result.casefold() not in options:
            raise ValueError("Choose Auto, Single, or Multiple.")
        return options[result.casefold()]
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in result):
        raise ValueError("Metadata cannot contain terminal control characters.")
    if field in ("track_total", "disc_total", "track_number", "disc_number"):
        if not (result.isascii() and result.isdecimal() and 1 <= int(result) <= 9999):
            raise ValueError("Enter a positive number (1–9999).")
        result = str(int(result))
    if field == "genre" and result.casefold() == "unknown":
        raise ValueError("Genre is optional; leave it unset rather than entering Unknown.")
    if field in ("genre", "date"):
        import tag
        result = tag.validate_manual_metadata({field: result})[field]
    return result


def tag_edits(issue):
    current = issue.get("tag_edits") or {}
    return {"album": dict(current.get("album") or {}),
            "tracks": {key: dict(value) for key, value in (current.get("tracks") or {}).items()}}


def reviewed(issue):
    return issue.get("review_status") == "reviewed"


def save_choice(service, issue, issues, *, field, value, relative=None):
    fingerprint = service.review_fingerprint(issue)
    if issue.get("source_fingerprint") not in (None, fingerprint):
        raise ValueError("Source changed; saved choices need fresh review before editing.")
    original = issue
    issue = deepcopy(issue)
    clearing_genre = field == "genre" and relative is None and value is None
    if not clearing_genre:
        value = clean_value(field, value)
    edits = tag_edits(issue)
    if relative is None:
        if field not in dict(ALBUM_FIELDS):
            raise ValueError("Not an album tag.")
        if field in ("date", "genre"):
            choices = dict(issue.get("manual_fields") or {})
            if clearing_genre:
                choices.pop("genre", None)
                edits["album"].pop("genre", None)
            else:
                choices[field] = value
            import tag
            issue["manual_fields"] = tag.validate_manual_metadata(choices)
        else:
            edits["album"][field] = value
    else:
        if field not in dict(TRACK_FIELDS) or relative not in issue.get("tracks", ()):
            raise ValueError("Unknown song or tag.")
        edits["tracks"].setdefault(relative, {})[field] = value
    # The latest explicit field edit supersedes older field approvals. Retain
    # superseded choices in history rather than allowing contradictory writers.
    retired = []
    for key in ("decision", "decisions"):
        older = issue.get(key)
        if not older:
            continue
        choices = [older] if isinstance(older, dict) else list(older)
        keep = []
        for item in choices:
            same_field = isinstance(item, dict) and item.get("field") == field
            old_path = item.get("path") if same_field else None
            overlapping = same_field and (relative is None or old_path is None
                or Path(str(old_path)).resolve() == (Path(service.tag.staging_folder) / relative).resolve())
            if overlapping:
                retired.append(deepcopy(item))
            else:
                keep.append(item)
        if keep:
            issue[key] = keep[0] if key == "decision" and len(keep) == 1 else keep
        else:
            issue.pop(key, None)
    if retired:
        issue.setdefault("decision_history", []).append({"superseded_by_field_edit": retired})
    issue["tag_edits"] = edits
    issue["source_fingerprint"] = fingerprint
    issue["review_status"] = "needs_review"  # An edit requires a fresh approval.
    draft = issue.get("interpretation")
    if draft is not None:
        if isinstance(draft, dict) and field == "genre" and relative is None:
            if clearing_genre:
                draft.pop("genre", None)
            else:
                draft["genre"] = value
        draft = service.review._validate_decision(draft,
            {path: Path(service.tag.staging_folder) / path for path in issue["tracks"]},
            fingerprint, approved=isinstance(draft, dict) and draft.get("status") == "pending_application")
        if relative is None:
            if field in ("album", "album_artist", "date", "genre"):
                if not clearing_genre:
                    draft[field] = value
            elif field == "artist":
                for path, track in draft["tracks"].items():
                    if "artist" not in edits["tracks"].get(path, {}):
                        track["artist"] = value
            elif field in ("track_total", "disc_total", "disc_layout"):
                if draft.get("release_id") is not None:
                    raise ValueError("Online release totals/layout need advanced review; recording choices were retained.")
                for path, track in draft["tracks"].items():
                    if field == "disc_layout":
                        if value == "single":
                            track["disc"] = 1
                            draft.setdefault("totals", {}).setdefault(path, {})["disc_total"] = 1
                    else:
                        draft.setdefault("totals", {}).setdefault(path, {})[field] = int(value)
                draft["retain_existing_totals"] = False
        elif relative in draft["tracks"]:
            key = {"track_number": "number", "disc_number": "disc"}.get(field, field)
            draft["tracks"][relative][key] = int(value) if key in ("disc", "number") else value
        draft.update(status="draft", user_approved=False)
        issue["interpretation"] = draft
    issue.pop("apply_problem", None)
    if service.review_fingerprint(original) != fingerprint:
        raise ValueError("Source changed during editing; choice not saved.")
    previous = deepcopy(original)
    original.clear()
    original.update(issue)
    try:
        service.save_review_queue(issues)
    except BaseException:
        original.clear()
        original.update(previous)
        raise


def _single(row, field):
    values = row.get(field) or ()
    if len(values) == 1:
        return values[0]
    if not values:
        return "Not set"
    return "Multiple values"


def _view(service, issue):
    rows = service._offline_album_review_rows(issue)
    root = Path(service.tag.staging_folder).resolve()
    values = {}
    for row in rows:
        relative = Path(row["path"]).resolve().relative_to(root).as_posix()
        values[relative] = row
    return values


def _display(service, value):
    return service.review._shown(value)


def _saved_interpretation(service, issue, mapping):
    saved = issue.get("interpretation")
    if not isinstance(saved, dict):
        return {}
    try:
        fingerprint = _read_only_fingerprint(service, issue)
        if issue.get("source_fingerprint") != fingerprint:
            return {}
        return service.review._validate_decision(saved,
            {path: Path(row["path"]) for path, row in mapping.items()}, fingerprint,
            approved=saved.get("status") == "pending_application")
    except (OSError, ValueError, TypeError):
        return {}


def _interpretation_totals(service, saved, mapping):
    if saved.get("release_id") is None:
        return saved.get("totals") or {}
    try:
        inventory = {relative: Path(row["path"]) for relative, row in mapping.items()}
        result = service.review._numbering_totals(inventory, saved, saved["release_tracks"], mapping)
        return {relative: result["numbering_totals"].get(str(path), {})
                for relative, path in inventory.items()}
    except (KeyError, TypeError, ValueError):
        return {}  # An unfinished mapping is still a draft, not missing-release proof.


def _review_edits(service, issue, mapping):
    edits = tag_edits(issue)
    saved = _saved_interpretation(service, issue, mapping)
    album = {field: saved[field] for field in ("album", "album_artist", "date", "genre") if field in saved}
    tracks = {}
    for path, track in saved.get("tracks", {}).items():
        tracks[path] = {target: str(track[source]) for source, target in
            (("title", "title"), ("artist", "artist"), ("disc", "disc_number"), ("number", "track_number"))
            if source in track}
    # Saved interpretation incorporates field edits; its later choices win.
    return {"album": {**edits["album"], **album}, "tracks": {
        path: {**edits["tracks"].get(path, {}), **tracks.get(path, {})}
        for path in set(edits["tracks"]) | set(tracks)}}


def _album_summary(service, issue, mapping):
    """Show saved user decisions instead of stale original-file tags."""
    edits = _review_edits(service, issue, mapping)
    rows = list(mapping.values())
    manual = issue.get("manual_fields") or {}
    summary = {}
    remote = _release_disc_structure(issue, mapping)
    for field, label in ALBUM_FIELDS:
        if field == "disc_layout":
            summary[field] = _review_disc_layout(service, issue, mapping)
        elif field in edits["album"]:
            summary[field] = edits["album"][field]
        elif field in manual:
            summary[field] = manual[field]
        elif field == "track_total":
            total = {next(iter(service.tag.get_track_totals(row))) for row in rows
                     if len(service.tag.get_track_totals(row)) == 1}
            if remote:
                summary[field] = (str(next(iter(remote[1].values()))) if len(remote[1]) == 1 else
                                  "; ".join(f"Disc {disc}: {count}" for disc, count in sorted(remote[1].items())))
            else:
                provisional = _allow_local_total_default(issue, _review_disc_layout(service, issue, mapping))
                inferred = _review_track_total(service, rows, local_default=provisional)
                summary[field] = str(inferred) if inferred is not None else "Not confirmed"
                summary["track_total_from_local_sequence"] = bool(
                    provisional and inferred is not None and (len(total) != 1
                    or inferred != next(iter(total))))
        elif field == "disc_total":
            total = {next(iter(service.tag.get_disc_totals(row))) for row in rows
                     if len(service.tag.get_disc_totals(row)) == 1}
            summary[field] = (str(len(remote[1])) if remote else
                              str(next(iter(total))) if len(total) == 1 else "Not confirmed")
        else:
            found = {_single(row, field) for row in rows}
            summary[field] = next(iter(found)) if len(found) == 1 else "Varies by track"
    saved = _saved_interpretation(service, issue, mapping)
    saved_totals = _interpretation_totals(service, saved, mapping)
    if saved_totals:
        counts = {}
        for relative, track in saved.get("tracks", {}).items():
            total = saved_totals.get(relative, {}).get("track_total")
            if total is not None and "disc" in track:
                counts.setdefault(track["disc"], set()).add(total)
        if counts and all(len(values) == 1 for values in counts.values()):
            summary["track_total"] = "; ".join(
                (f"Disc {disc}: " if len(counts) > 1 else "") + str(next(iter(values)))
                for disc, values in sorted(counts.items()))
        discs = {values["disc_total"] for values in saved_totals.values() if "disc_total" in values}
        if len(discs) == 1:
            summary["disc_total"] = str(next(iter(discs)))
    # An audio-supported release may have resolved album-level metadata
    # before a separate track-artist conflict blocked automatic import.
    # Reuse it only while the complete release-numbering evidence remains
    # source-bound and valid; user edits above always take precedence.
    verified = (issue.get("details") or {}).get("verified_release_numbering") or {}
    source_bound = remote is not None and isinstance(verified, dict)
    metadata = verified.get("album_metadata") if source_bound else None
    if isinstance(metadata, dict):
        for field in ("album", "album_artist", "date", "genre"):
            value = metadata.get(field)
            if (field not in manual and field not in edits["album"]
                    and isinstance(value, str) and value.strip()):
                if field in ("date", "genre"):
                    try:
                        value = clean_value(field, value)
                    except (ValueError, TypeError):
                        continue
                summary[field] = value
    # An unambiguous primary artist is an effective album-level default,
    # not a request to rewrite original files or erase collaboration credits.
    summary["album_artist_from_tracks"] = False
    if summary["album_artist"] == "Not set":
        supported = service._suggest_album_artist(rows)
        if supported:
            summary["album_artist"] = supported
            summary["album_artist_from_tracks"] = True
            summary["album_artist_hint"] = None
        else:
            summary["album_artist_hint"] = service._suggest_compilation_album_artist(rows)
    else:
        summary["album_artist_hint"] = None
    # Absence is uncertainty, not a verified genre named "Unknown".
    summary["genre_hint"] = None
    if str(summary["genre"]).strip().casefold() in ("unknown", "unclassified"):
        summary["genre"] = "Not set"
    return summary


def _fresh_release_evidence(issue):
    fingerprint = issue.get("source_fingerprint")
    if not isinstance(fingerprint, str):
        return False
    details = issue.get("details") or {}
    certificate = details.get("verified_release_numbering")
    evidence = certificate if certificate is not None else details
    if "source_fingerprint" in details and details["source_fingerprint"] != fingerprint:
        return False
    if not isinstance(evidence, dict) or evidence.get("source_fingerprint") != fingerprint:
        return False
    try:
        import mixie
        mixie.review._fingerprint(fingerprint)
        return _read_only_fingerprint(mixie, issue) == fingerprint
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def _release_disc_structure(issue, mapping):
    """Unanimous complete MusicBrainz positions and per-disc totals, if proven.

    The remote structure is a source-bound hint for HUMAN review, never a new
    acoustic identity. Missing/partial remote evaluations grant no authority.
    """
    if not mapping or not _fresh_release_evidence(issue):
        return None
    verified = (issue.get("details") or {}).get("verified_release_numbering")
    if verified is not None:
        # A recorded release certificate is authoritative only for the exact
        # source it came from. Invalid or stale proof must NEVER fall back to
        # alternative release mappings saved in the same issue.
        if not isinstance(verified, dict) or not isinstance(issue.get("source_fingerprint"), str):
            return None
        if verified.get("source_fingerprint") != issue["source_fingerprint"]:
            return None
        try:
            raw = verified.get("positions")
            totals = verified.get("track_totals")
            if isinstance(raw, dict) and set(raw) == set(mapping) and isinstance(totals, dict):
                positions = {}
                by_disc = {}
                for relative, pair in raw.items():
                    if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                            or any(type(value) is not int or value < 1 for value in pair)):
                        raise ValueError("Invalid certified numbering")
                    disc, number = pair
                    positions[relative] = (disc, number)
                    by_disc.setdefault(disc, set()).add(number)
                if (len(set(positions.values())) == len(mapping)
                        and sorted(by_disc) == list(range(1, len(by_disc) + 1))
                        and set(totals) == {str(disc) for disc in by_disc}
                        and all(type(totals[str(disc)]) is int
                                and by_disc[disc] == set(range(1, totals[str(disc)] + 1))
                                for disc in by_disc)):
                    return positions, {disc: len(numbers) for disc, numbers in by_disc.items()}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, OverflowError):
            pass
        return None
    releases = (issue.get("details") or {}).get("releases") or []
    # Incomplete/unexamined competitors cannot be silently discarded as
    # negative evidence. They may represent a longer or differently arranged
    # edition even when one fully mapped candidate looks perfect.
    if any(not isinstance(entry, dict) or entry.get("status") != "complete"
           or entry.get("tracklist_complete") is False or entry.get("errors")
           or entry.get("unexamined_release_ids") for entry in releases):
        return None
    signatures = set()
    candidates = []
    for entry in releases:
        if (not isinstance(entry, dict) or entry.get("status") != "complete"
                or entry.get("tracklist_complete") is False or entry.get("errors")):
            continue
        tracks = entry.get("tracklist") or []
        matched = entry.get("matched") or {}
        if (not isinstance(matched, dict) or len(tracks) != len(mapping)
                or len(matched) != len(mapping) or set(matched.values()) != set(mapping)):
            return None
        positions = {}
        per_disc = {}
        try:
            available = set()
            for track in tracks:
                disc, number = int(track["disc"]), int(track["number"])
                if disc < 1 or number < 1 or (disc, number) in available:
                    raise ValueError("Bad remote numbering")
                available.add((disc, number))
                per_disc[disc] = per_disc.get(disc, 0) + 1
            for encoded, relative in matched.items():
                pair = json.loads(encoded)
                if (not isinstance(pair, list) or len(pair) != 2
                        or type(pair[0]) not in (int, str) or type(pair[1]) not in (int, str)):
                    raise ValueError("Invalid source position")
                disc, number = int(pair[0]), int(pair[1])
                if (disc, number) not in available or relative in positions:
                    raise ValueError("Incomplete remote position")
                positions[relative] = (disc, number)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError, OverflowError):
            return None
        if set(positions) != set(mapping) or len(set(positions.values())) != len(mapping):
            return None
        if set(per_disc) != set(range(1, max(per_disc) + 1)):
            return None
        if any({number for other_disc, number in available if other_disc == disc}
               != set(range(1, count + 1)) for disc, count in per_disc.items()):
            return None
        signature = tuple(sorted(positions.items()))
        signatures.add(signature)
        candidates.append((positions, per_disc))
    if len(signatures) != 1 or not candidates:
        return None
    return candidates[0]


def _disc_layout(service, issue, mapping):
    """Infer only a unanimous disc layout from full MusicBrainz release evidence.

    A track's missing disc tags are not evidence of a single-disc release.
    Competing verified release editions may disagree on titles yet agree on the
    disc structure. Never use a partial/incomplete remote tracklist as proof.
    """
    edits = tag_edits(issue)
    selected = edits["album"].get("disc_layout", "auto")
    if selected != "auto":
        return selected
    saved = _saved_interpretation(service, issue, mapping)
    if (any(track.get("disc", 1) > 1 for track in saved.get("tracks", {}).values())
            or any(values.get("disc_total", 1) > 1 for values in (saved.get("totals") or {}).values())
            or any(int(track["disc"]) > 1 for track in saved.get("release_tracks") or [])):
        return "multiple"
    if (int(edits["album"].get("disc_total", 1)) > 1 or
            any(int(values.get("disc_number", 1)) > 1 for values in edits["tracks"].values())):
        return "multiple"
    remote = _release_disc_structure(issue, mapping)
    if remote is not None:
        return "single" if len(remote[1]) == 1 else "multiple"
    numbers = {service.tag.parse_disc_number(row["disc_number"])[0] for row in mapping.values()}
    totals = {total for row in mapping.values() for total in service.tag.get_disc_totals(row)}
    if any(number is not None and number > 1 for number in numbers) or any(total > 1 for total in totals):
        return "multiple"
    return "single" if numbers == {1} or (numbers <= {1, None} and totals == {1}) else "auto"


def _review_disc_layout(service, issue, mapping):
    """Use Single as the review *default*, never as verified release proof.

    Multi-disc source tags, a complete compatible MusicBrainz layout, or
    exclusively multi-disc fully mapped release candidates take priority.
    Unknown provider coverage leaves only the human-review default; it must
    not turn a partial/unexamined release into trusted online evidence.
    """
    known = _disc_layout(service, issue, mapping)
    if known in ("single", "multiple"):
        return known
    if not _fresh_release_evidence(issue):
        return "single"
    seen_multi = seen_single = False
    for release in (issue.get("details") or {}).get("releases") or ():
        if (not isinstance(release, dict) or release.get("status") != "complete"
                or release.get("tracklist_complete") is False or release.get("errors")):
            continue
        matches = release.get("matched") or {}
        if not isinstance(matches, dict):
            continue
        if len(matches) != len(mapping) or set(matches.values()) != set(mapping):
            continue
        tracklist = release.get("tracklist") or ()
        try:
            all_positions = {(int(track["disc"]), int(track["number"]))
                             for track in tracklist}
            positions = {tuple(map(int, json.loads(position)))
                         for position in matches}
        except (TypeError, ValueError, KeyError, OverflowError, json.JSONDecodeError):
            continue
        if len(all_positions) != len(tracklist) or not positions.issubset(all_positions):
            continue
        if any(disc > 1 for disc, number in positions):
            seen_multi = True
        else:
            seen_single = True
    if seen_multi and not seen_single:
        return "multiple"
    return "single"


def _remote_position_choices(issue, mapping):
    """Expose different complete edition positions without selecting between them."""
    choices = {relative: set() for relative in mapping}
    if not _fresh_release_evidence(issue):
        return choices
    for entry in (issue.get("details") or {}).get("releases") or []:
        if (not isinstance(entry, dict) or entry.get("status") != "complete"
                or entry.get("tracklist_complete") is False or entry.get("errors")):
            continue
        matched = entry.get("matched") or {}
        if set(matched.values()) != set(mapping) or len(matched) != len(mapping):
            continue
        for encoded, relative in matched.items():
            try:
                disc, number = json.loads(encoded)
                disc, number = int(disc), int(number)
                if disc > 0 and number > 0 and relative in choices:
                    choices[relative].add((disc, number))
            except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
                continue
    return choices


def _allow_local_total_default(issue, layout):
    """Allow an observed complete 1..N sequence as a provisional review total.

    Missing *edition* identity must not hide the local count. This is not
    positive release evidence, a recording approval or cleanup authority.
    The user must still approve a manual interpretation before writing audio.
    """
    # A complete, observed 1..N track sequence is provisional evidence
    # regardless of whether the *separate* import receipt became stale.
    # _review_track_total still refuses gaps, duplicates and multi-disc rows.
    return layout == "single"


def _review_track_total(service, rows, *, explicit=None, remote=None, local_default=False):
    """Prefer verified totals; allow a visibly provisional contiguous local sequence."""
    if explicit is not None:
        return int(clean_value("track_total", str(explicit)))
    if remote is not None:
        return remote
    existing = {number for row in rows for number in service.tag.get_track_totals(row)}
    if not local_default:
        return next(iter(existing)) if len(existing) == 1 else None
    if len(rows) < 2:
        return next(iter(existing)) if len(existing) == 1 else None
    raw_numbers = [service.tag.parse_track_number(row["track_number"])[0] for row in rows]
    numbers = [int(number) for number in raw_numbers if number is not None]
    # A gap, duplicate, ambiguous number or multi-disc marker forbids fallback.
    if (len(numbers) == len(rows)
            and set(numbers) == set(range(1, len(numbers) + 1))
            and all((service.tag.parse_disc_number(row["disc_number"])[0] or 1) == 1
                    for row in rows)):
        # A stale 13-of-14 total is not stronger than a complete 1..14
        # source sequence. Treat this as a provisional observed total.
        return len(rows)
    return next(iter(existing)) if len(existing) == 1 else None


def preflight_issues(service, issue):
    """Calculate all actionable blockers WITHOUT mutating state or contacting services."""
    mapping = _view(service, issue)
    edits = _review_edits(service, issue, mapping)
    album = _album_summary(service, issue, mapping)
    problems = []
    if (issue.get("source_fingerprint") is not None
            and _read_only_fingerprint(service, issue) != issue["source_fingerprint"]):
        problems.append("Staged source changed; saved choices and release evidence need fresh review")
    missing = ("Not set", "Multiple values", "Varies by track", "Not confirmed")
    if album.get("album") in missing:
        problems.append("Album title missing or inconsistent")
    if album.get("album_artist") in missing:
        if album.get("album_artist_hint"):
            problems.append("Album Artist absent in source (supported local suggestion available)")
        else:
            problems.append("Album Artist missing or inconsistent")
    if album.get("date") in missing:
        problems.append("Release Date missing")
    layout = _review_disc_layout(service, issue, mapping)
    remote = _release_disc_structure(issue, mapping) if edits["album"].get("disc_layout", "auto") == "auto" else None
    saved = _saved_interpretation(service, issue, mapping)
    numbering = _interpretation_totals(service, saved, mapping)
    positions, by_disc = {}, {}
    for relative, row in mapping.items():
        chosen = edits["tracks"].get(relative, {})
        for field, label in (("title", "Title"), ("artist", "Artist")):
            current = chosen.get(field, edits["album"].get(field, _single(row, field)))
            if current in missing or not str(current).strip():
                problems.append(f"{Path(relative).name}: {label} missing or inconsistent")
            elif field == "artist" and str(current).isascii() and str(current).isdecimal():
                problems.append(f"{Path(relative).name}: Artist looks like a track number ({current})")
        original_num = service.tag.parse_track_number(row["track_number"])[0]
        number = chosen.get("track_number") or (remote[0][relative][1] if remote else None) or original_num
        disc = (1 if layout == "single" else chosen.get("disc_number") or
                (remote[0][relative][0] if remote else None) or
                service.tag.parse_disc_number(row["disc_number"])[0])
        if layout == "single" and int(chosen.get("disc_number", 1)) != 1:
            problems.append(f"{Path(relative).name}: Disc Number conflicts with Single Disc Layout")
        if disc is not None:
            by_disc.setdefault(int(disc), []).append(relative)
        if number is None:
            possibilities = _remote_position_choices(issue, mapping).get(relative, set())
            choices = ", ".join(f"D{d}:T{n}" for d, n in sorted(possibilities)[:5])
            problems.append(f"{Path(relative).name}: Track Number missing" +
                            (f" (edition positions: {choices})" if choices else ""))
        if disc is None:
            problems.append(f"{Path(relative).name}: Disc Number missing")
        if number is not None and disc is not None:
            key = (int(disc), int(number))
            if key in positions:
                label = f"D{key[0]}:T{key[1]}" if layout == "multiple" else f"track {key[1]}"
                problems.append(f"Duplicate {label}: {Path(positions[key]).name} / {Path(relative).name}")
            positions[key] = relative
    for disc, relatives in sorted(by_disc.items()):
        saved_totals = {numbering.get(path, {}).get("track_total") for path in relatives}
        explicit = (next(iter(saved_totals)) if len(saved_totals) == 1 and None not in saved_totals else
                    edits["album"].get("track_total") if layout == "single" else None)
        total = _review_track_total(service, [mapping[path] for path in relatives],
            explicit=explicit,
            remote=remote[1].get(disc) if remote else None,
            local_default=_allow_local_total_default(issue, layout))
        if total is None:
            problems.append((f"Disc {disc}: " if layout == "multiple" else "") + "Track Total missing or conflicting; " +
                            ("set Track Total under Album Tags" if layout == "single" else
                             "use R advanced review for explicit per-disc totals"))
        elif any(number > total for other_disc, number in positions if other_disc == disc):
            problems.append((f"Disc {disc}: " if layout == "multiple" else "") +
                            "Track Total is smaller than a selected track number")
    if layout == "multiple":
        if "track_total" in edits["album"]:
            problems.append("Multi-disc Track Totals need per-disc choices in R advanced review")
        if album.get("disc_total") in missing and remote is None:
            problems.append("Disc Total needs release evidence or an explicit selection")
    if issue.get("apply_problem"):
        problems.append("Last Apply: " + str(issue["apply_problem"])[:180])
    # Historical diagnostics are audit evidence, not CURRENT actionable
    # blockers. Showing resolved old errors as outstanding issues makes the
    # review screen misleading even after a fresh scan has fixed them.
    diagnostics = [(issue, "")]
    for snapshot, prefix in diagnostics:
        details = snapshot.get("details") or {}
        if details.get("kind") == "metadata_conflict":
            field = str(details.get("field", "unknown")).replace("_", " ").title()
            existing = "; ".join(str(value) for value in details.get("existing_values", [])) or "not set"
            problems.append(prefix + f"Metadata conflict — {field}: {existing} "
                            f"vs {details.get('reference_value', 'unknown reference')}")
        if (details.get("kind") == "artwork_conflict" or
                (snapshot.get("artwork_details") or {}).get("kind") == "artwork_conflict"):
            problems.append(prefix + "Artwork conflict: cover selection needs Apply validation")
        if details.get("kind") in ("identity_release", "provider_unavailable"):
            resolved = (not prefix and details.get("kind") == "identity_release" and remote is not None
                        and all(_release_resolves_recording(issue, relative, remote) for relative in mapping))
            if not resolved:
                problems.append(prefix + "Release/recording evidence: " + str(snapshot.get("reason", "Unresolved"))[:180])
    # Historical diagnostics remain in the durable issue history, not preflight.
    return list(dict.fromkeys(problems))


def _autofill_supported(service, issue, issues):
    """Apply only consensual, supportable hints to the saved review, not original audio.

    Triggered by a deliberate review-menu action. Remote disagreement, unknown
    coverage or provider outages never authorize inferred release identity.
    """
    mapping = _view(service, issue)
    summary = _album_summary(service, issue, mapping)
    possible = {}
    if summary["album_artist"] == "Not set" and summary.get("album_artist_hint"):
        possible["album_artist"] = summary["album_artist_hint"]
    if summary["genre"] == "Not set" and summary.get("genre_hint"):
        possible["genre"] = summary["genre_hint"]
    # A complete mapping from each plausible edition establishes *positions*,
    # but only when every credible mapped edition agrees exactly.
    if _release_disc_structure(issue, mapping) is not None:
        print("Disc and track positions already resolve automatically from MusicBrainz.")
    release_ids = []
    for candidate in (issue.get("details") or {}).get("releases") or []:
        if (isinstance(candidate, dict) and candidate.get("status") == "complete"
                and candidate.get("tracklist_complete") is not False
                and not candidate.get("errors") and
                len(candidate.get("matched") or {}) == len(mapping) and
                set((candidate.get("matched") or {}).values()) == set(mapping)
                and candidate.get("release_id")):
            release_ids.append(candidate["release_id"])
    uncertain = any(isinstance(candidate, dict) and (
        candidate.get("errors") or candidate.get("tracklist_complete") is False or
        candidate.get("unexamined_release_ids") or
        "release_lookup_unavailable" in (candidate.get("reasons") or [])
    ) for candidate in (issue.get("details") or {}).get("releases") or [])
    if release_ids and not uncertain and _fresh_release_evidence(issue) and len(set(release_ids)) <= 8:
        import lookup
        terminal.pass_status(3, f"Reviewing shared metadata for {len(set(release_ids))} editions")
        try:
            common = lookup.common_release_metadata(sorted(set(release_ids)))
            # Only missing fields; require exact unanimous provider agreement.
            for field in ("album_artist", "date"):
                if summary.get(field) == "Not set" and isinstance(common.get(field), str) and common[field].strip():
                    possible[field] = common[field]
        except Exception as error:
            print(f"MusicBrainz metadata unavailable; existing choices remain: {type(error).__name__}")
    if len(set(release_ids)) > 8:
        print("Date/artist lookups postponed: too many editions to fetch interactively. Cached local evidence remains available.")
    if not possible:
        print("No new consensus-backed metadata fields to fill. Genuine conflicts still require review.")
        return 0
    print("Supported missing fields:")
    for field, value in possible.items():
        print(f"  {dict(ALBUM_FIELDS)[field]}: {_display(service, value)}")
    if input("Type FILL to save these choices (0 = postpone; Enter = cancel): ").strip() != "FILL":
        print("Nothing changed.")
        return 0
    count = 0
    for field, value in possible.items():
        try:
            save_choice(service, issue, issues, field=field, value=value)
            count += 1
        except (OSError, ValueError) as error:
            print(f"Could not fill {field}: {error}")
    print(f"Saved {count} supported field(s); all original audio unchanged.")
    return count


def _show_album(service, issue, issues):
    mapping = _view(service, issue)
    while True:
        rows = list(mapping.values())
        summary = _album_summary(service, issue, mapping)
        hint = summary["album_artist_hint"]
        genre_hint = summary["genre_hint"]
        multiple = _review_disc_layout(service, issue, mapping) == "multiple"
        shown_fields = tuple(item for item in ALBUM_FIELDS if multiple or
                             item[0] not in ("disc_layout", "disc_total"))
        print(f"\n=== Album Tags: {_display(service, issue['album'])} ===")
        for index, (field, label) in enumerate(shown_fields, 1):
            value = summary[field]
            if field == "disc_layout":
                value = f"{value.title()} (S=Single, M=Multiple)"
                if _disc_layout(service, issue, mapping) == "auto":
                    value += " [review default; not provider-verified]"
            elif field == "genre" and value in ("Not set", "Multiple values", "Varies by track"):
                value += " [optional; unresolved, no genre will be invented]"
            elif field == "album_artist" and summary.get("album_artist_from_tracks"):
                value += " [inferred from primary track artists]"
            elif field == "track_total" and summary.get("track_total_from_local_sequence"):
                value += " [local contiguous sequence; not release-verified]"
            annotation = (hint if field == "album_artist" else genre_hint if field == "genre" else None)
            annotation = (f" | Suggested: {_display(service, annotation)}" if annotation and
                          service.tag._normalize_label(str(summary[field])) != service.tag._normalize_label(annotation) else "")
            attention = "  [MISSING]" if value in ("Not set", "Not confirmed") else ""
            print(f"{index}. {label}: {_display(service, value)}{annotation}{attention}")
        if not multiple:
            print("M. Multiple-disc options (only if needed)")
        print("0. Back (all edits save immediately)")
        try:
            selection = input("Edit field: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if selection == "0":
            return
        if selection.casefold() == "m" and not multiple:
            try:
                save_choice(service, issue, issues, field="disc_layout", value="multiple")
            except (ValueError, OSError) as error:
                print(f"Not saved: {error}")
            continue
        if not selection.isdecimal() or not 1 <= int(selection) <= len(shown_fields):
            print("Select a field or 0 to return.")
            continue
        field, label = shown_fields[int(selection) - 1]
        if field == "artist" and len({_single(row, "artist") for row in rows}) > 1:
            print("This album has different track artists. Album-wide Artist would overwrite guest/compilation credits.")
            try:
                answer = input("Type ALL to replace every track artist (0 = postpone; Enter = cancel): ").strip()
            except (KeyboardInterrupt, EOFError):
                return
            if answer == "0":
                return
            if answer != "ALL":
                continue
        if field == "track_total" and _review_disc_layout(service, issue, mapping) == "multiple":
            print("Use R advanced interpretation review for explicit per-disc Track Totals, including partial multi-disc albums.")
            continue
        suggestion = hint if field == "album_artist" else genre_hint if field == "genre" else None
        print(f"Current: {_display(service, summary[field])}")
        if field == "disc_layout":
            print("Enter S for Single Disc, or M for Multiple Discs.")
        if field == "genre":
            print("Genre is optional. Enter - to remove the manual choice; existing source tags stay unchanged.")
        if suggestion:
            print(f"Suggested: {_display(service, suggestion)} (type S to use)")
        try:
            value = input(f"New {label} (0 = postpone; Enter = keep): ").strip()
            if value == "0":
                return
            if value.casefold() == "s" and suggestion:
                value = suggestion
        except (KeyboardInterrupt, EOFError):
            continue
        if not value:
            continue
        if field == "disc_layout" and value.casefold() not in ("s", "single", "m", "multiple"):
            print("Choose S for Single or M for Multiple. Auto is no longer a review option.")
            continue
        try:
            save_choice(service, issue, issues, field=field,
                        value=None if field == "genre" and value == "-" else value)
            print("Saved. Originals untouched.")
        except (ValueError, OSError) as error:
            print(f"Not saved: {error}")


def _evidence_by_path(service, issue):
    rows = (issue.get("details") or {}).get("tracks") or []
    source_root = Path(service.tag.staging_folder).resolve()
    result = {}
    for row in rows:
        try:
            path = Path(row["path"])
            resolved = (source_root / path).resolve() if not path.is_absolute() else path.resolve()
            if resolved.is_relative_to(source_root):
                result[resolved.relative_to(source_root).as_posix()] = row
        except (TypeError, ValueError, KeyError, OSError):
            continue
    return result


def _release_resolves_recording(issue, relative, structure):
    if structure is None:
        return False
    identifiers = set()
    for entry in (issue.get("details") or {}).get("releases") or []:
        if not isinstance(entry, dict) or entry.get("status") != "complete":
            return False
        position = structure[0][relative]
        targets = [track.get("recording_id") for track in entry.get("tracklist") or []
                   if (str(track.get("disc")), str(track.get("number"))) == tuple(map(str, position))]
        if len(targets) != 1 or not isinstance(targets[0], str) or not targets[0]:
            return False
        identifiers.add(targets[0])
    return len(identifiers) == 1


def _attention_paths(service, issue, row_map):
    evidence = _evidence_by_path(service, issue)
    structure = _release_disc_structure(issue, row_map)
    saved = _saved_interpretation(service, issue, row_map)
    flagged = []
    for relative in row_map:
        row = evidence.get(relative)
        if row is None:
            continue
        assessment = row.get("assessment") or {}
        status = assessment.get("status", row.get("status"))
        chosen = saved.get("tracks", {}).get(relative, {})
        if (status not in ("verified", "preliminary_match")
                and not _release_resolves_recording(issue, relative, structure)
                and not chosen.get("recording_id")):
            flagged.append(relative)
    return flagged


def _show_tracks(service, issue, issues):
    mapping = _view(service, issue)
    evidence = _evidence_by_path(service, issue)
    saved_tracks = _review_edits(service, issue, mapping)["tracks"]
    remote = _release_disc_structure(issue, mapping)
    paths = sorted(mapping, key=lambda p: (
        int(saved_tracks.get(p, {}).get("disc_number") or
            (remote[0][p][0] if remote else None) or
            service.tag.parse_disc_number(mapping[p]["disc_number"])[0] or 1),
        int(saved_tracks.get(p, {}).get("track_number") or
            (remote[0][p][1] if remote else None) or
            service.tag.parse_track_number(mapping[p]["track_number"])[0] or 9999), p))
    flagged = [path for path in paths if path in set(_attention_paths(service, issue, mapping))]
    filtered = bool(flagged)
    index = 0
    while True:
        shown_paths = flagged if filtered else paths
        if not shown_paths:
            filtered = False
            shown_paths = paths
        index = max(0, min(index, len(shown_paths) - 1))
        relative = shown_paths[index]
        row = mapping[relative]
        saved = _review_edits(service, issue, mapping)
        edits = saved["tracks"].get(relative, {})
        candidate = evidence.get(relative)
        suggested = (service.review.suggested_identity(candidate) or service.review.consensus_identity(candidate)) if candidate else None
        print(f"\n=== Track Tags: {index + 1}/{len(shown_paths)} ===")
        print(f"File: {_display(service, Path(relative).name)}")
        verified_numbering = (issue.get('details') or {}).get('verified_release_numbering')
        structure = remote if verified_numbering else None
        if structure and relative in structure[0]:
            disc, number = structure[0][relative]
            position = f"disc {disc}, " if len(structure[1]) > 1 else ""
            print(f"  Verified release: {position}track {number} / {structure[1][disc]}")
        # A single-disc album has no disc tags to edit. The track editor
        # offers them only for a genuinely multi-disc interpretation.
        fields = (TRACK_FIELDS if _review_disc_layout(service, issue, mapping) == "multiple"
                  else tuple(item for item in TRACK_FIELDS if item[0] != "disc_number"))
        for number, (field, label) in enumerate(fields, 1):
            fallback = saved["album"].get("artist", _single(row, field)) if field == "artist" else _single(row, field)
            current = edits.get(field, fallback)
            annotation = ""
            if remote and field in ("track_number", "disc_number") and field not in edits:
                disc, track_number = remote[0][relative]
                current = str(track_number if field == "track_number" else disc)
                annotation = " [release evidence; used on approval]"
            if field in ("title", "artist") and suggested and suggested.get(field) and service.tag._normalize_label(str(current)) != service.tag._normalize_label(suggested[field]):
                annotation = f" | Suggested: {_display(service, suggested[field])}"
            attention = "  [MISSING]" if current in ("Not set", "Not confirmed") else ""
            if field in ("track_number", "disc_number") and current == "Not set" and remote:
                disc, number = remote[0][relative]
                annotation += f" | Verified release: {number if field == 'track_number' else disc} (used on approval)"
            elif field == "track_number" and current == "Not set":
                options = _remote_position_choices(issue, mapping).get(relative, set())
                if options:
                    annotation += " | Edition positions: " + ", ".join(
                        f"D{d}:T{n}" for d,n in sorted(options)[:5])
            print(f"{number}. {label}: {_display(service, current)}{annotation}{attention}")
        selected = _saved_interpretation(service, issue, mapping).get("tracks", {}).get(relative, {})
        if selected.get("recording_id"):
            print("  Saved recording choice: " + _display(service, selected.get("title", "Title not set")) +
                  " [user choice; not independently verified]")
        if candidate:
            choices = (candidate.get("assessment") or {}).get("candidates") or []
            playable = [option for option in choices if isinstance(option, dict)
                        and option.get("status") in ("verified", "probable")
                        and isinstance(option.get("details"), dict)]
            if _release_resolves_recording(issue, relative, remote):
                print("  Recording resolved by source-bound album evidence; no recording-ID choice needed.")
            elif len(playable) > 1 and not selected.get("recording_id"):
                print(f"  {len(playable)} possible recordings; these are NOT interchangeable:")
                for position, option in enumerate(playable[:3], 1):
                    metadata = option["details"]
                    name = metadata.get("title") or "Title unknown"
                    credit = metadata.get("artist-credit-phrase") or "Artist unknown"
                    version = metadata.get("disambiguation") or ""
                    print(f"    {position}. {_display(service, name)} — {_display(service, credit)}"
                          + (f" ({_display(service, version)})" if version else ""))
                print("  Candidates need album-level fingerprint comparison; do not select by name alone.")
                print("  R opens Advanced Review when the automatic evidence remains genuinely ambiguous.")
        print("R. Choose recording / advanced review (if evidence remains unresolved)")
        print("N. Next  P. Previous  A. All/Unresolved tracks  0. Back")
        try:
            choice = input("Edit field or navigate: ").strip().casefold()
        except (KeyboardInterrupt, EOFError):
            return
        if choice == "0":
            return
        if choice == "r":
            print("Opening recording choices: choose the album release first, then use track review")
            print("and A batch-fill for album-resolved identities. Do not guess IDs from matching names.")
            if service._choose_album_interpretation(issue, issues):
                return "approved"
            continue
        if choice == "n":
            if index == len(shown_paths) - 1:
                print("Last track reached. Choose 0 to return to the album review.")
            else:
                index += 1
            continue
        if choice == "p":
            if index > 0:
                index -= 1
            continue
        if choice == "a":
            filtered = not filtered if flagged else False
            index = 0
            continue
        if not choice.isdecimal() or not 1 <= int(choice) <= len(fields):
            print("Select a tag or navigation key.")
            continue
        field, label = fields[int(choice) - 1]
        suggestion = (suggested or {}).get(field)
        if suggestion:
            print(f"MIXIE suggests: {_display(service, suggestion)} (S to accept)")
        try:
            value = input(f"New {label} (0 = postpone; Enter = keep): ").strip()
        except (KeyboardInterrupt, EOFError):
            continue
        if value == "0":
            return
        if value.casefold() == "s" and suggestion:
            value = suggestion
        if not value:
            continue
        if field == "artist":
            original = _single(row, "artist")
            guest_marker = any(marker in original.casefold() for marker in
                               ("feat.", "featuring", " ft.", " & ", " and ", ",", "/"))
            if guest_marker and service.tag._normalize_label(original) != service.tag._normalize_label(value):
                try:
                    answer = input("Guest/collaboration credit would change. Type REPLACE to confirm (0 = postpone): ").strip()
                except (KeyboardInterrupt, EOFError):
                    return
                if answer == "0":
                    return
                if answer != "REPLACE":
                    print("Artist edit postponed; existing credit preserved.")
                    continue
        try:
            save_choice(service, issue, issues, field=field, value=value, relative=relative)
            print("Track choice saved. Original file unchanged.")
        except (OSError, ValueError) as error:
            print(f"Not saved: {error}")


def _artwork(service, issue, issues):
    """Snapshot a user-selected JPEG/PNG and preserve its exact bytes for apply."""
    current = issue.get("artwork_decision") or {}
    print("\n=== Set Album Artwork ===")
    if current.get("kind") == "review_artwork_file":
        print(f"Saved cover: {_display(service, Path(current['path']).name)}")
    conflict = issue.get("artwork_details") or issue.get("details") or {}
    options = conflict.get("candidates", [])[:3] if conflict.get("kind") == "artwork_conflict" else []
    if options:
        print("Discovered covers (up to three). Open their source references if unsure:")
        for number, option in enumerate(options, 1):
            label = option.get("label") or Path(option.get("path", "unknown")).name
            print(f"{number}. {_display(service, label)}")
        print("Enter 1–3 to select a discovered cover, or supply your own file path.")
    print("Drag a JPEG, PNG or WebP into the terminal and press Enter.")
    print("0. Back")
    try:
        entry = input("Artwork file or option: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if entry in ("0", ""):
        return
    try:
        if options and entry.isascii() and entry.isdecimal() and 1 <= int(entry) <= len(options):
            candidate = options[int(entry) - 1]
            source = Path(candidate["path"]).absolute()
            if source.is_symlink() or not source.is_file():
                raise ValueError("Discovered artwork is no longer available.")
            if hashlib.sha256(source.read_bytes()).hexdigest() != candidate["sha256"]:
                raise ValueError("Discovered artwork changed; review it again.")
        else:
            tokens = shlex.split(entry)
            if len(tokens) != 1:
                raise ValueError("Enter one image path.")
            source = Path(tokens[0]).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise ValueError("Artwork must be an ordinary image file.")
        if source.stat().st_size > 20_000_000:
            raise ValueError("Artwork exceeds 20 MB.")
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if not __import__("stat").S_ISREG(metadata.st_mode) or metadata.st_size > 20_000_000:
                raise ValueError("Invalid image file.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(20_000_001)
        finally:
            os.close(descriptor)
        if len(data) > 20_000_000:
            raise ValueError("Artwork exceeds 20 MB.")
        # Normalize WebP to a Rockbox-compatible JPEG on the REVIEW copy only.
        data, mime = mixie_artwork.normalized_image(data)
        fingerprint = service._approval_fingerprint(issue)
        choice = mixie_artwork.snapshot_artwork(
            data, root=Path(service.tag.script_folder).resolve() / ".review_artwork")
        if service.review_fingerprint(issue) != fingerprint:
            raise ValueError("Source changed during artwork selection.")
        issue["artwork_decision"] = choice
        issue["source_fingerprint"] = fingerprint
        # Changing only cover art must not erase an already approved release
        # interpretation. Apply will still validate the snapshot and album.
        if not (issue.get("interpretation") or {}).get("user_approved"):
            issue["review_status"] = "needs_review"
        issue.pop("artwork_details", None)
        issue.pop("apply_problem", None)
        service.save_review_queue(issues)
        print("Artwork saved for the eventual import. Your image and music originals are untouched.")
    except (ValueError, OSError, SyntaxError) as error:
        print(f"Artwork not saved: {error}")


def _confirmed_text(value, label):
    if not isinstance(value, str) or value in ("Not set", "Multiple values", "Varies by track", "Not confirmed"):
        raise ValueError(f"Review {label} before approving this album.")
    return clean_value(label, value)


def _build_user_interpretation(service, issue):
    """Construct an explicitly human-authorized offline mapping of EVERY file.

    No provider IDs or completeness proof are invented. All positions must be
    present, unique, and checked; totals are stated during final confirmation.
    """
    current_fingerprint = service.review_fingerprint(issue)
    if issue.get("source_fingerprint") and issue["source_fingerprint"] != current_fingerprint:
        raise ValueError("Staged source changed since the saved edits; start a fresh review.")
    mapping = _view(service, issue)
    if not mapping or set(mapping) != set(issue.get("tracks", ())):
        raise ValueError("Album source inventory changed; review again.")
    edits = tag_edits(issue)
    album = _album_summary(service, issue, mapping)
    title = _confirmed_text(album["album"], "album")
    album_artist = album["album_artist"]
    if album_artist == "Not set" and album.get("album_artist_hint"):
        album_artist = album["album_artist_hint"]
    album_artist = _confirmed_text(album_artist, "album_artist")
    date = _confirmed_text(album["date"], "date")
    optional = {}
    if album["genre"] not in ("Not set", "Multiple values", "Varies by track", "Not confirmed"):
        optional["genre"] = _confirmed_text(album["genre"], "genre")
    service.tag.validate_manual_metadata({"date": date, **optional})
    layout = _review_disc_layout(service, issue, mapping)
    evidence = _release_disc_structure(issue, mapping) if edits["album"].get("disc_layout", "auto") == "auto" else None
    tracks, by_disc = {}, {}
    for relative, row in mapping.items():
        saved = edits["tracks"].get(relative, {})
        artist = saved.get("artist", edits["album"].get("artist", _single(row, "artist")))
        song_title = _confirmed_text(saved.get("title", _single(row, "title")), "title")
        artist = _confirmed_text(artist, "artist")
        raw_number = saved.get("track_number")
        raw_number = raw_number or (evidence[0][relative][1] if evidence else None)
        raw_number = raw_number or service.tag.parse_track_number(row["track_number"])[0]
        if raw_number is None:
            raise ValueError(f"Set the track number for {Path(relative).name} before approving.")
        number = int(clean_value("track_number", str(raw_number)))
        if layout == "single":
            if int(saved.get("disc_number", 1)) != 1:
                raise ValueError("Disc Number conflicts with Single Disc Layout; choose Multiple or use advanced review.")
            disc = 1
        else:
            raw_disc = saved.get("disc_number")
            raw_disc = raw_disc or (evidence[0][relative][0] if evidence else None)
            raw_disc = raw_disc or service.tag.parse_disc_number(row["disc_number"])[0]
            if raw_disc is None:
                raise ValueError(f"Set the disc number for {Path(relative).name} before approving.")
            disc = int(clean_value("disc_number", str(raw_disc)))
        position = (disc, number)
        if position in by_disc.get(disc, set()):
            raise ValueError(f"Duplicate track {number} on disc {disc}; review numbering.")
        by_disc.setdefault(disc, set()).add(position)
        tracks[relative] = {"recording_id": None, "title": song_title,
                            "artist": artist, "disc": disc, "number": number}
    discs = sorted(by_disc)
    if discs != list(range(1, len(discs) + 1)):
        raise ValueError("Disc numbers must start at 1 and contain no gaps; partial multi-disc albums need advanced review.")
    disc_count = len(discs) if layout == "single" else None
    if layout == "multiple":
        if "track_total" in edits["album"]:
            raise ValueError("Multi-disc Track Totals need explicit per-disc choices in advanced review.")
        if len(discs) < 2:
            raise ValueError("Multiple Discs requires at least two represented discs; partial albums need the advanced interpretation review.")
        saved_total = edits["album"].get("disc_total")
        if saved_total:
            disc_count = int(saved_total)
        elif evidence:
            disc_count = len(evidence[1])
        else:
            observed_totals = {n for row in mapping.values() for n in service.tag.get_disc_totals(row)}
            disc_count = next(iter(observed_totals)) if len(observed_totals) == 1 else None
        if disc_count is None or disc_count < max(discs):
            raise ValueError("Specify Disc Total under Album Tags before approving a multi-disc release.")
    totals = {}
    for relative, track in tracks.items():
        disc = track["disc"]
        positions = {number for _, number in by_disc[disc]}
        track_total = _review_track_total(service,
            [row for other, row in mapping.items() if tracks[other]["disc"] == disc],
            explicit=edits["album"].get("track_total") if layout == "single" else None,
            remote=evidence[1].get(disc) if evidence else None,
            local_default=_allow_local_total_default(issue, layout))
        if track_total is None:
            raise ValueError((f"Disc {disc}: " if layout == "multiple" else "") + "Track Total missing or conflicting. " +
                             ("Set Track Total under Album Tags or use advanced review." if layout == "single" else
                              "Use advanced review for explicit per-disc totals; local file counts are not totals."))
        if track_total < max(positions):
            raise ValueError(f"Track total for disc {disc} is smaller than a selected track number.")
        totals[relative] = {"track_total": track_total, "disc_total": disc_count}
    if service.review_fingerprint(issue) != current_fingerprint:
        raise ValueError("Source changed while building the interpretation; saved choices were not rebound.")
    decision = {"status": "pending_application", "user_approved": True,
                "independently_verified": False, "source_fingerprint": current_fingerprint,
                "release_id": None, "partial": True, "album": title,
                "album_artist": album_artist, "date": date, **optional,
                "tracks": tracks, "totals": totals}
    service.review._validate_decision(decision,
        {relative: (Path(service.tag.staging_folder) / relative).resolve() for relative in tracks},
        decision["source_fingerprint"], final=True, approved=True)
    return decision


def _approve_review(service, issue, issues):
    """Preview the effective choices and require a fresh, exact human confirmation."""
    try:
        mapping = _view(service, issue)
        inventory = {relative: Path(row["path"]).resolve() for relative, row in mapping.items()}
        saved = issue.get("interpretation")
        if saved is not None:
            if not isinstance(saved, dict):
                raise ValueError("Malformed saved interpretation; fresh advanced review is required.")
            if saved.get("status") == "draft":
                raise ValueError("Finish the advanced draft with APPROVE; it has not been approved.")
            fingerprint = service.review_fingerprint(issue)
            if issue.get("source_fingerprint") != fingerprint:
                raise ValueError("Source changed; the advanced interpretation needs fresh review.")
            decision = service.review._validate_decision(saved, inventory, fingerprint, final=True, approved=True)
        else:
            decision = _build_user_interpretation(service, issue)
        plan = service.review._plan(inventory, decision,
            service.review._draft_tracklist(issue.get("details") or {}, decision), mapping)
        if plan["numbering_deferred"]:
            raise ValueError("Track/Disc Totals still need explicit choices in advanced review before approval.")
    except (OSError, ValueError) as error:
        print(f"Not ready for approval: {error}")
        print("Your saved edits remain intact. Correct the indicated field, then retry.")
        return False
    print("\n=== Confirm Album for Import ===")
    artists = {track["artist"] for track in decision["tracks"].values()}
    print("Artist: " + _display(service, next(iter(artists)) if len(artists) == 1 else "Varies by track; see credits below"))
    disc_total = next(iter(plan["numbering_totals"].values()))["disc_total"]
    if disc_total > 1:
        print(f"Disc Layout: Multiple ({disc_total} discs)")
    service.review._show_plan(decision, plan)
    print("Originals, decoding and working-copy verification remain mandatory.")
    if service._import_first_enabled():
        print("Library ZIP backup is optional and separate from import.")
    else:
        print("Archive proof is checked during final application.")
    try:
        choice = input("Type APPROVE to save for later Apply (0 = postpone; anything else keeps editing): ")
    except (KeyboardInterrupt, EOFError):
        return False
    if choice != "APPROVE":
        print("Approval postponed; saved edits retained.")
        return False
    if service.review_fingerprint(issue) != decision["source_fingerprint"]:
        print("Source changed; approval was not saved.")
        return False
    previous = deepcopy(issue.get("interpretation"))
    had_interpretation = "interpretation" in issue
    saved = False
    try:
        issue["interpretation"] = decision
        saved = _finish(service, issue, issues)
        return saved
    finally:
        if not saved:
            if had_interpretation:
                issue["interpretation"] = previous
            else:
                issue.pop("interpretation", None)


def _finish(service, issue, issues):
    fields = ("source_fingerprint", "review_status", "apply_problem")
    previous = {field: deepcopy(issue[field]) for field in fields if field in issue}
    try:
        fingerprint = service.review_fingerprint(issue)
        if (issue.get("source_fingerprint") not in (None, fingerprint)
                or (issue.get("interpretation") or {}).get("source_fingerprint", fingerprint) != fingerprint
                or service.review_fingerprint(issue) != fingerprint):
            raise ValueError("Source changed during review; saved choices were retained without approval.")
        issue["source_fingerprint"] = fingerprint
        issue["review_status"] = "reviewed"
        issue.pop("apply_problem", None)
        service.save_review_queue(issues)
        print(f"Saved under Reviewed — Not Applied: {_display(service, issue['album'])}")
        print("No imports or audio edits were performed.")
        return True
    except BaseException as error:
        # Failed persistence must not expose a new in-memory reviewed state.
        for field in fields:
            issue.pop(field, None)
        issue.update(previous)
        if not isinstance(error, (OSError, ValueError)):
            raise
        print(f"Could not save review: {error}")
        return False


def _album_editor(service, issue, issues):
    while True:
        print(f"\n=== Review: {_display(service, issue['album'])} ===")
        print(f"{len(issue['tracks'])} tracks | {_display(service, issue.get('reason', 'Check metadata'))}")
        if issue.get("apply_problem"):
            print("Last apply needs attention: " + _display(service, issue["apply_problem"]))
        try:
            with _review_render_scope():
                mapping = _view(service, issue)
                attention = len(_attention_paths(service, issue, mapping))
                if attention:
                    print(f"{attention} song(s) have unresolved audio evidence; the rest are optional to inspect.")
                blockers = preflight_issues(service, issue)
                print("\nOutstanding issues:")
                if blockers:
                    for item in blockers[:16]:
                        print("  ! " + _display(service, item))
                    if len(blockers) > 16:
                        print(f"  ...and {len(blockers)-16} more; edit tracks to resolve them.")
                else:
                    print("  All local fields ready; final Apply still validates identity, artwork and import safety.")
                if not (issue.get("artwork_decision") or {}).get("kind") == "review_artwork_file":
                    print("  Artwork: automatic discovery on Apply; required before import.")
        except (OSError, ValueError) as error:
            print(f"Tag read unavailable: {error}")
        print("1. View / Update Track Tags")
        print("2. View / Update Album Tags")
        print("3. Set Album Artwork")
        print("4. Approve Album (save; import later)")
        print("5. Skip to Next Album")
        print("A. Auto-fill supported missing fields")
        print("R. Advanced recording / album interpretation (per-disc totals / partial albums)")
        print("0. Return to Review List")
        try:
            action = input("Album action: ").strip().casefold()
        except (KeyboardInterrupt, EOFError):
            return "list"
        try:
            if action == "1":
                if _show_tracks(service, issue, issues) == "approved":
                    return "list"
            elif action == "2":
                _show_album(service, issue, issues)
            elif action == "3":
                _artwork(service, issue, issues)
            elif action == "4":
                if _approve_review(service, issue, issues):
                    return "list"
            elif action == "5":
                return "next"
            elif action == "a":
                _autofill_supported(service, issue, issues)
            elif action == "0":
                return "list"
            elif action == "r":
                print("Opening advanced recording / album interpretation; no import is started.")
                if service._choose_album_interpretation(issue, issues):
                    return "list"
            else:
                print("Invalid choice.")
        except (OSError, ValueError, TypeError) as error:
            print(f"Review action could not finish: {error}")


def _needs_metadata_review(issue):
    return not (issue.get('status') == 'archive_pending'
                or ((issue.get('details') or {}).get('kind') == 'completion'
                    and str(issue.get('reason', '')).startswith('Library import verified; archive pending:')))


def _list(service, issues, want_reviewed):
    while True:
        entries = [issue for issue in issues if _needs_metadata_review(issue)
                   and reviewed(issue) == want_reviewed]
        print("\n=== " + ("Reviewed — Not Applied" if want_reviewed else "Albums Needing Review") + " ===")
        if not entries:
            print("No albums in this list.")
            return
        from collections import Counter
        duplicates=Counter(str(entry.get('album','')).casefold() for entry in entries)
        for index, issue in enumerate(entries, 1):
            suffix = " — apply needs attention" if issue.get("apply_problem") else ""
            if duplicates[str(issue.get('album','')).casefold()]>1 and issue.get('import_name'):
                suffix += "  [source: "+_display(service,issue['import_name'])+"]"
            if want_reviewed and not (issue.get("interpretation") or {}).get("user_approved"):
                suffix += " — approval incomplete (v4 review)"
            print(f"{index}. {_display(service, issue['album'])}{suffix}")
        print("0. Back to Review Manager")
        try:
            choice = input("Select album: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if choice == "0":
            return
        if not choice.isdecimal() or not 1 <= int(choice) <= len(entries):
            print("Choose a listed album or 0.")
            continue
        # Walk each review exactly once, beginning at the user-selected row.
        # The previous implementation removed only the current entry and then
        # restarted at row 1, so Skip alternated between the first two albums.
        # Hold this menu's initial identities: no approval/edit can silently
        # alter another album's position or make an item repeat forever.
        start_index = int(choice) - 1
        ordered = entries[start_index:] + entries[:start_index]
        for entry in ordered:
            if not any(item is entry for item in issues) or reviewed(entry) != want_reviewed or not _needs_metadata_review(entry):
                continue
            outcome = _album_editor(service, entry, issues)
            if outcome != "next":
                break
        # Return to selection after finished/skip; do not loop over reviewed items implicitly.


def _apply(service, issues):
    available = [issue for issue in issues if _needs_metadata_review(issue) and reviewed(issue) and
                 (issue.get("interpretation") or {}).get("user_approved") is True]
    if not available:
        print("No approved albums yet. Open an album and choose Approve Album first.")
        return
    print("\n=== Apply Reviewed Albums ===")
    print("A. Apply all approved albums")
    for index, issue in enumerate(available, 1):
        print(f"{index}. {_display(service, issue['album'])}")
    print("0. Back")
    try:
        choice = input("Choose album(s) to import: ").strip().casefold()
    except (KeyboardInterrupt, EOFError):
        return
    if choice == "0":
        return
    if choice == "a":
        chosen = available
    elif choice.isdecimal() and 1 <= int(choice) <= len(available):
        chosen = [available[int(choice)-1]]
    else:
        print("Invalid selection.")
        return
    print(f"Validate and import {len(chosen)} approved album(s)? Online approvals may contact MusicBrainz.")
    try:
        if input("Type APPLY to proceed: ").strip() != "APPLY":
            print("Cancelled. Nothing imported.")
            return
    except (KeyboardInterrupt, EOFError):
        return
    successes = 0
    for issue in list(chosen):
        if issue not in issues:
            continue
        print(f"\nApplying: {_display(service, issue['album'])}")
        snapshot = {key: deepcopy(issue[key]) for key in
                    ("details", "artwork_details", "reason", "error_type", "apply_problem", "source_fingerprint") if key in issue}
        source_fingerprint = None
        try:
            source_fingerprint = service.review_fingerprint(issue)
            if not reviewed(issue) or issue.get("source_fingerprint") not in (None, source_fingerprint):
                raise ValueError("Source changed; review choices need checking again.")
            if service.finalize_reviewed_album(issue, issues):
                successes += 1
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                print("Apply interrupted. Remaining albums and edits are retained.")
                return
            history = issue.setdefault("review_evidence_history", [])
            if snapshot and snapshot not in history:
                history.append(snapshot)
            details = None
            if isinstance(error, (service.review.AlbumReviewRequired, service.ProviderUnavailable)):
                details = deepcopy(error.details)
                issue["error_type"] = type(error).__name__
            elif isinstance(error, service.tag.RequiredMetadataReview):
                details = {"kind": "required_metadata", "fields": error.fields}
            elif isinstance(error, service.tag.MetadataConflictError):
                details = {"kind": "metadata_conflict", "field": error.field,
                           "existing_values": error.existing, "reference_value": error.expected,
                           "track": Path(error.path).name, "path": error.path}
                if getattr(error, "release_numbering", None):
                    details.update(tracks=deepcopy(error.release_audit["tracks"]),
                                   releases=deepcopy(error.release_audit["releases"]),
                                   verified_release_numbering=deepcopy(error.release_numbering))
            elif isinstance(error, service.ArtworkReviewRequired):
                issue["artwork_details"] = {"kind": "artwork_conflict", "candidates": error.candidates,
                                            "release_ids": error.release_ids}
            if details is not None:
                if (details.get("kind") in ("identity_release", "provider_unavailable")
                        or details.get("releases") or "verified_release_numbering" in details):
                    try:
                        stable = (source_fingerprint is not None
                                  and service.review_fingerprint(issue) == source_fingerprint)
                    except (OSError, ValueError, RuntimeError):
                        stable = False
                    if stable:
                        details.setdefault("source_fingerprint", source_fingerprint)
                    else:
                        # Keep diagnostics, but never bind an interrupted snapshot
                        # to the new source, even if it contains a certificate.
                        details["source_fingerprint"] = None
                issue["details"] = details
            issue["apply_problem"] = str(error)[:800]
            issue["reason"] = str(error)[:800]
            # Failure is not a completed import, and does not erase user edits.
            service.save_review_queue(issues)
            if _needs_metadata_review(issue):
                print("Apply did not complete; kept in Reviewed — Not Applied: " + _display(service, issue["apply_problem"]))
            else:
                print("Library import verified; required archives/finalization still pending: " +
                      _display(service, issue["apply_problem"]))
    print(f"\nApplied and verified: {successes}/{len(chosen)} album(s).")
    archive_pending = sum(not _needs_metadata_review(issue) for issue in chosen if issue in issues)
    if archive_pending:
        print(f"{archive_pending} existing import(s) await required archives; use Review Manager option 4.")
        print("No new metadata approval or recording selection is needed.")
    if successes + archive_pending < len(chosen):
        print("Unfinished albums remain available to edit and reapply.")


def _retry_archives(service, issues):
    print("\n=== Finish / Retry Required Archives ===")
    print("Existing verified imports only; no ingestion, provider lookup or metadata approval.")
    print("Source cleanup is not requested; physical sources stay retained by default.")
    for issue in issues:
        if not _needs_metadata_review(issue):
            print("  Pending archives: " + _display(service, issue["album"]))
    result = None
    try:
        # The parent menu already holds the ingest_session lock. Leave cleanup
        # authorization at the service's non-destructive default.
        result = service.finalize_verified_imports()
    except (KeyboardInterrupt, EOFError):
        print("Archive retry interrupted; reloading saved progress.")
    except (OSError, ValueError, RuntimeError) as error:
        print("Archive/finalization retry failed: " + _display(service, error))
    try:
        issues[:] = service.load_review_queue()
    except (OSError, ValueError, RuntimeError) as error:
        print("Could not reload the saved review queue: " + _display(service, error))
        print("Return to the main menu and reopen Review Manager before continuing.")
        return False
    if result is not None:
        count, problems = result
        retention_suffixes = (
            "Shared source has unaccounted audio; full original and staging retained.",
            "Shared source still has unresolved album(s); verified albums archived independently, full original and staging retained.",
        )
        retained = [problem for problem in problems if str(problem).endswith(retention_suffixes)]
        failures = [problem for problem in problems if not str(problem).endswith(retention_suffixes)]
        print(f"Required archives newly finalized: {count} album group(s).")
        if failures:
            print(f"Archive/finalization failures: {len(failures)}")
            for problem in failures:
                print("  ! " + _display(service, problem))
        else:
            print("No archive/finalization failures reported.")
        if retained:
            print("Shared-source retention (not an archive failure):")
            for problem in retained:
                print("  " + _display(service, problem))
    pending = sum(not _needs_metadata_review(issue) for issue in issues)
    print(f"Still awaiting required archives/finalization: {pending} album(s).")
    return True


def review_manager_menu(service, issues):
    """Main user-facing review entry; old programmatic legacy menu is preserved."""
    while True:
        # A stale source must never retain a reviewed/ready label.
        changed = False
        for issue in issues:
            if _needs_metadata_review(issue) and reviewed(issue):
                _before = issue.get("source_fingerprint")
                _current, valid = service._invalidate_stale_choices(issue)
                if not valid or not reviewed(issue):
                    issue["review_status"] = "needs_review"
                    issue.pop("apply_problem", None)
                    changed = True
        if changed:
            service.save_review_queue(issues)
        pending_count = sum(not reviewed(issue) for issue in issues if _needs_metadata_review(issue))
        reviewed_count = sum(reviewed(issue) for issue in issues if _needs_metadata_review(issue))
        archive_count = sum(not _needs_metadata_review(issue) for issue in issues)
        terminal.header("Review Manager", "Saved choices and pending decisions")
        print(f"1. Albums Needing Review ({pending_count})")
        print(f"2. Reviewed — Not Applied ({reviewed_count})")
        if archive_count:
            print(f"   {archive_count} library import(s) are waiting for archive verification (no review needed).")
        print("3. Apply Reviewed Albums")
        print(f"4. Finish / Retry Required Archives ({archive_count} pending)")
        print("0. Return to Main Menu")
        try:
            command = input("Choose an option: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if command == "0":
            return
        if command == "1":
            _list(service, issues, False)
        elif command == "2":
            _list(service, issues, True)
        elif command == "3":
            _apply(service, issues)
        elif command == "4":
            if not _retry_archives(service, issues):
                return
        else:
            print("Select 1, 2, 3, 4, or 0.")
