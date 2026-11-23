import os
import sys
import audio
import mixie_terminal as terminal

# Unified terminal styles; redirected diagnostics remain unstyled.
print = terminal.themed_print
input = terminal.themed_input
import mixie_artwork
import lookup
import tag
import hashlib
from pathlib import Path
from io import BytesIO, StringIO
from itertools import combinations
from PIL import Image, ImageChops, ImageOps, ImageStat
import constants
import sort
import archive
from mutagen import File, MutagenError
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
import json
import tempfile
import review
import zipfile
import shutil
import stat
import re
import fcntl
from copy import deepcopy
from contextlib import contextmanager, redirect_stdout
from threading import RLock
from contextvars import ContextVar

_session_mutex = RLock()
_session_depth = 0
_review_checkpoint = ContextVar("mixie_review_checkpoint", default=False)


@contextmanager
def ingest_session():
    """Serialize user-visible ingest/review/cleanup, with crash-released locking."""
    global _session_depth
    with _session_mutex:
        descriptor = None
        if _session_depth == 0:
            root = Path(tag.script_folder)
            if root.is_symlink():
                raise ValueError("Unsafe ingest lock directory.")
            descriptor = os.open(root / ".mixie-ingest.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("Unsafe ingest lock file.")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(descriptor)
                raise
        _session_depth += 1
        try:
            yield
        finally:
            _session_depth -= 1
            if descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)


def _serialized(operation):
    from functools import wraps
    @wraps(operation)
    def run(*args, **kwargs):
        with ingest_session():
            return operation(*args, **kwargs)
    return run


class CleanupResults(list):
    def __init__(self, values=(), *, errors=()):
        super().__init__(values)
        self.errors = list(errors)


def _ingest_report(success=0, already=0, deferred=0, errors=0, quarantined=(), purged=(), *, awaiting_cleanup=None, provider_waiting=0, verified_library=0):
    if awaiting_cleanup is None:
        awaiting_cleanup = len(set(quarantined) - set(purged))
    print("\n=== Ingest result ===")
    for label, value in (("Success", success), ("Library imports verified (archive pending)", verified_library), ("Already verified", already), ("Deferred", deferred), ("Errors", errors),
                         ("Quarantined", len(quarantined)), ("Purged", len(purged)),
                         ("Awaiting safe cleanup", awaiting_cleanup), ("Waiting for online provider", provider_waiting)):
        print(f"{label}: {max(0, value)}")


class ProviderUnavailable(ValueError):
    """Retryable online failure, never permission to fill gaps or approve a match."""
    def __init__(self, details):
        self.details = review._json_safe({**details, "kind": "provider_unavailable", "retryable": True})
        super().__init__(self.details["reason"])


def _identity_review_error(audits, evaluations, reason, *, provider_errors=()):
    details = review.issue_details(audits, evaluations, reason)
    errors = list(provider_errors)
    for audit in audits:
        errors.extend((audit.get("evidence") or {}).get("errors", []))
    for evaluation in evaluations:
        errors.extend(evaluation.get("errors", []))
    retryable = [error for error in errors if isinstance(error, dict) and error.get("retryable") is True]
    if retryable:
        details.update(reason="Online verification was interrupted. Retry after the provider recovers; other uncertainties may still need review.",
                       provider_errors=retryable, uncertainty_reason=reason)
        return ProviderUnavailable(details)
    return review.AlbumReviewRequired(details)


def _online_album_call(operation, audits, evaluations, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (OSError, lookup.musicbrainzngs.MusicBrainzError) as error:
        evidence = lookup.provider_error_evidence("musicbrainz", error)
        if evidence["retryable"]:
            raise _identity_review_error(audits, evaluations, "Album metadata could not be checked.",
                                         provider_errors=[evidence]) from error
        raise


# Preserve artwork disagreements for interactive review
class ArtworkReviewRequired(ValueError):
    def __init__(self, candidates, release_ids):
        self.candidates = [
            {
                "path": item.get("path", item["source"]),
                "sha256": item.get("source_sha256") or hashlib.sha256(item["data"]).hexdigest(),
                "label": item.get("label", item["source"])
            }
            for item in candidates[:3]
        ]
        self.release_ids = list(release_ids)

        super().__init__(
            "Supplied artwork does not match known covers."
        )

def album_asset_root(album, *, matched=None):
    """Return a private staged asset root, or None for shared/ambiguous extras.

    A single disc's parent is eligible only when its anchored directory number is
    corroborated by source numbering or the confirmed mapping. Unmapped audio
    beneath an ancestor must also belong to that same structurally divided album.
    Neither album['folder'] nor the presence of a cover authorizes ascent.
    """
    staging = Path(tag.staging_folder).absolute()
    paths = {Path(track["path"]).absolute() for track in album["tracks"]}
    if not paths or staging.is_symlink():
        raise ValueError("A safe staged album inventory is required for assets.")
    imports = set()
    for path in paths:
        relative = path.relative_to(staging)
        if len(relative.parts) < 2:
            raise ValueError("Album assets must remain within a staged import.")
        imports.add(relative.parts[0])
        current = staging
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Symlinked album asset paths are unsafe.")
        if not path.is_file() or not path.resolve().is_relative_to(staging.resolve()):
            raise ValueError("Album asset source is unavailable or unsafe.")
    if len(imports) != 1:
        raise ValueError("Album assets cannot cross staged imports.")
    import_root = staging / next(iter(imports))
    root = Path(os.path.commonpath([str(path.parent) for path in paths]))
    metadata = {Path(track["path"]).absolute(): track for track in album["tracks"]}
    mapped = {Path(path).absolute(): int(disc) for (disc, _number), path in (matched or {}).items()}

    def disc_number(path):
        match = re.fullmatch(r"(?:disc|disk|cd)[ ._-]*0*([1-9][0-9]*)", path.name, re.IGNORECASE)
        return int(match[1]) if match else None

    def private(candidate):
        # file_manifest rejects links and special files before scan_import_assets
        # can follow anything; the complete audio inventory prevents shared extras.
        inventory = {candidate / relative for relative in sort.file_manifest(candidate)
                     if Path(relative).suffix.lower() in constants.music_file_types}
        if not paths <= inventory:
            raise ValueError("Album asset inventory changed.")
        if inventory == paths:
            return True
        labels = {tag._normalize_label(value) for track in metadata.values()
                  for value in track.get("album", ()) if value.strip()}
        credits = {tag._normalize_label(value) for track in metadata.values()
                   for value in track.get("album_artist", ()) if value.strip()}
        if len(labels) != 1 or len(credits) != 1:
            return False
        for path in inventory:
            relative = path.relative_to(candidate)
            if len(relative.parts) != 2 or disc_number(candidate / relative.parts[0]) is None:
                return False
            track = metadata.get(path) or tag.read_track_metadata(path)
            if track is None:
                return False
            if ({tag._normalize_label(value) for value in track["album"] if value.strip()} != labels
                    or {tag._normalize_label(value) for value in track["album_artist"] if value.strip()} != credits):
                return False
            number = mapped.get(path, tag.parse_disc_number(track["disc_number"])[0])
            if number != disc_number(path.parent):
                return False
        return True

    if not private(root):
        return None
    disc = disc_number(root)
    if (disc is not None and root != import_root and all(
            path.parent == root and mapped.get(path, tag.parse_disc_number(metadata[path]["disc_number"])[0]) == disc
            for path in paths) and private(root.parent)):
        return root.parent
    return root


# Select verified artwork, prioritizing files supplied with the album

# Human metadata edits are bound to one immutable staged inventory. They are
# user decisions, never independent recording/release verification.
def _validated_tag_edits(edits, paths):
    import mixie_review_ui as ui
    if edits is None:
        return {"album": {}, "tracks": {}}
    if not isinstance(edits, dict) or set(edits) - {"album", "tracks"}:
        raise ValueError("Invalid saved tag edits.")
    album, tracks = edits.get("album") or {}, edits.get("tracks") or {}
    if not isinstance(album, dict) or not isinstance(tracks, dict):
        raise ValueError("Invalid saved tag edits.")
    allowed_album = dict(ui.ALBUM_FIELDS)
    allowed_track = dict(ui.TRACK_FIELDS)
    root = Path(tag.staging_folder).resolve()
    inventory = {Path(path).resolve().relative_to(root).as_posix(): Path(path).resolve() for path in paths}
    inventory.update({str(path.resolve()): path.resolve() for path in paths})
    if set(album) - set(allowed_album) or set(tracks) - set(inventory):
        raise ValueError("Saved album tags refer to an unsupported field or song.")
    result = {"album": {}, "tracks": {}}
    for field, value in album.items():
        result["album"][field] = ui.clean_value(field, value)
    for relative, fields in tracks.items():
        if not isinstance(fields, dict) or set(fields) - set(allowed_track):
            raise ValueError("Invalid individual song tag edits.")
        result["tracks"][str(inventory[relative])] = {
            field: ui.clean_value(field, value) for field, value in fields.items()
        }
    return result


def _numbering_edit_approvals(matched, tracklist, edits, existing=(), *, numbering_totals=None):
    """Number edits cannot contradict release proof or explicit offline choices."""
    result = list(existing)
    has_number_edit = any(field in values for values in edits["tracks"].values()
                          for field in ("track_number", "disc_number"))
    if not has_number_edit and not any(field in edits["album"] for field in ("track_total", "disc_total")):
        return result  # Only explicit numbering edits touch this approval path.
    if numbering_totals is not None:
        # An offline tracklist represents supplied files, not the full release.
        for (disc, number), path in matched.items():
            path = str(Path(path).resolve())
            expected = {"track_number": int(number), "disc_number": int(disc),
                        **numbering_totals[path]}
            chosen = {**edits["album"], **edits["tracks"].get(path, {})}
            for field in tag._NUMBERING_FIELDS:
                if field in chosen and int(chosen[field]) != expected[field]:
                    raise ValueError(f"Edited {field.replace('_', ' ')} disagrees with the approved offline interpretation.")
        return result
    index = {(str(Path(d.get("path")).resolve()), d.get("field")) for d in result}
    totals = _release_totals(tracklist)
    album = edits["album"]
    if "track_total" in album and int(album["track_total"]) != totals["album_total"]:
        raise ValueError("Edited album track total disagrees with the selected full release.")
    if "disc_total" in album and int(album["disc_total"]) != totals["disc_total"]:
        raise ValueError("Edited disc total disagrees with the selected full release.")
    for (disc, number), path in matched.items():
        path = Path(path).resolve()
        fields = edits["tracks"].get(str(path), {})
        expected = {"track_number": int(number), "disc_number": int(disc),
                    "track_total": totals["track_totals"][str(disc)],
                    "disc_total": totals["disc_total"]}
        for field in ("track_number", "disc_number"):
            if field in fields and int(fields[field]) != expected[field]:
                raise ValueError(f"Edited {field.replace('_', ' ')} for {path.name} conflicts with the selected release. Choose the correct edition or position.")
        metadata = tag.read_track_metadata(path)
        if metadata is None:
            raise ValueError("Cannot read original numbering for a reviewed track.")
        for field in ("track_number", "disc_number", "track_total", "disc_total"):
            was_edited = field in fields or field in album
            if not was_edited:
                continue
            if field in fields and int(fields[field]) != expected[field]:
                raise ValueError("Edited numbering does not match the selected release.")
            if tag._numbering_agrees(metadata, field, expected[field],
                                      (expected[field], totals["album_total"]) if field == "track_total" else (expected[field],)):
                continue
            key = (str(path), field)
            if key in index:
                continue
            observed = tag._numbering_existing(metadata, field)
            if observed:
                result.append({"path": str(path), "field": field,
                               "existing_value": observed, "reference_value": str(expected[field]),
                               "value": str(expected[field]), "status": "pending_application"})
                index.add(key)
    return result


def _edited_identity(identities, edits):
    for path, identity in identities.items():
        song = edits["tracks"].get(path, {})
        album = edits["album"]
        changes = {field: song.get(field, album.get(field)) if field == "artist" else song.get(field)
                   for field in ("title", "artist")}
        changes = {field: value for field, value in changes.items() if value is not None}
        if changes:
            identity.update(changes, verified=False, user_approved=True)
            tag.validate_recording_identity(identity)


def _review_artwork_snapshot(choice):
    """Read an explicit cover from our owned snapshot, never an arbitrary stale path."""
    if not isinstance(choice, dict) or choice.get("kind") != "review_artwork_file" or choice.get("status") != "pending_application":
        raise ValueError("Invalid reviewed cover choice.")
    root = Path(tag.script_folder).resolve() / ".review_artwork"
    path = Path(choice.get("path", ""))
    if (root.is_symlink() or path.is_symlink() or not path.is_file()
            or path.parent != root or path.suffix.lower() not in (".jpg", ".png")):
        raise ValueError("Saved artwork is missing or outside the managed review folder.")
    if path.stat().st_size > 20_000_000:
        raise ValueError("Saved artwork is oversized.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 20_000_000:
            raise ValueError("Unsafe artwork snapshot.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(20_000_001)
    finally:
        os.close(descriptor)
    if (len(data) > 20_000_000 or hashlib.sha256(data).hexdigest() != choice.get("sha256")):
        raise ValueError("Saved cover bytes changed since approval.")
    with Image.open(BytesIO(data)) as image:
        image.load()
        if image.format not in ("JPEG", "PNG") or image.width * image.height > 60_000_000:
            raise ValueError("Saved cover is not a supported image.")
        mime = "image/jpeg" if image.format == "JPEG" else "image/png"
    if mime != choice.get("mime"):
        raise ValueError("Saved cover media type changed.")
    return {"mime": mime, "data": data, "source": str(path)}

def select_album_art(album, release_ids, artwork_decision=None, *, matched=None):
    if artwork_decision is not None and artwork_decision.get("kind") == "review_artwork_file":
        return _review_artwork_snapshot(artwork_decision)
    def prepare_image(data):
        normalized, mime = mixie_artwork.normalized_image(data)
        with Image.open(BytesIO(normalized)) as image:
            image.load()
            pixels = image.width * image.height
            preview = ImageOps.fit(
                ImageOps.exif_transpose(image).convert("RGB"),
                (256, 256), method=Image.Resampling.LANCZOS)
            return preview, mime, pixels
    def difference(first, second):
        result = ImageChops.difference(first, second)
        return sum(ImageStat.Stat(result).mean) / 3

    # Locate supplied artwork using our existing sorter
    root = album_asset_root(album, matched=matched)
    assets = sort.scan_import_assets(root) if root is not None else []
    local_files = sorted(
        Path(asset["path"])
        for asset in assets
        if Path(asset["path"]).suffix.lower()
        in constants.album_art_types
    )

    # Prefer a root-level cover corroborated by a front scan
    primary_covers = [
        path for path in local_files
        if path.parent == root
        and path.name.casefold() in {
            "cover.jpg", "cover.png", "cover.webp",
            "folder.jpg", "folder.png", "folder.webp"
        }
    ]
    front_scans = [
        path for path in local_files
        if path.stem.casefold() == "front"
    ]
    if len(primary_covers) == 1 and len(front_scans) == 1:
        primary = primary_covers[0]
        front = front_scans[0]
        if any(
            path.stat().st_size > 20_000_000
            for path in (primary, front)
        ):
            raise ValueError("Local cover exceeds 20 MB.")
        data, mime = mixie_artwork.normalized_image(primary.read_bytes())
        primary_preview, _, _ = prepare_image(data)
        front_preview, _, _ = prepare_image(front.read_bytes())
        score = difference(primary_preview, front_preview)
        print(f"Local front-cover image difference: {score:.2f} (0 = identical)")
        if score <= 6.0 and artwork_decision is None:
            print(f"Using corroborated local cover: {primary.name}")
            return {
                "mime": mime,
                "data": data,
                "source": str(primary)
            }

    # Trust matching local front-cover files before requesting artwork
    covers = {
        path.stem.casefold(): path
        for path in local_files
        if path.stem.casefold() in {"front", "folder", "cover"}
    }

    for first_name, second_name in (
        ("front", "folder"),
        ("front", "cover"),
        ("folder", "cover"),
    ):
        if first_name not in covers or second_name not in covers:
            continue

        first = covers[first_name]
        second = covers[second_name]

        if first.parent != second.parent:
            continue

        if any(
            path.stat().st_size > 20_000_000
            for path in (first, second)
        ):
            raise ValueError("Local cover exceeds 20 MB.")

        first_data, mime = mixie_artwork.normalized_image(first.read_bytes())
        first_preview, _, _ = prepare_image(first_data)
        second_preview, _, _ = prepare_image(second.read_bytes())

        score = difference(first_preview, second_preview)
        print(f"Local cover image difference: {score:.2f} (0 = identical)")

        if score <= 6.0 and artwork_decision is None:
            print(f"Using corroborated local cover: {first.name}")
            return {
                "mime": mime,
                "data": first_data,
                "source": str(first)
            }

    # Exclude booklet, CD, sticker and packaging scans
    comparison_files = [
        path for path in local_files
        if path.stem.casefold()
        in {"front", "folder", "cover", "front+sticker"}
    ] or local_files


    # Decode each local cover only once
    prepared_files = []

    for path in comparison_files:
        if path.stat().st_size > 20_000_000:
            raise ValueError(f"Artwork too large: {path.name}")

        raw = path.read_bytes()
        data, mime = mixie_artwork.normalized_image(raw)
        preview, _, _ = prepare_image(data)
        prepared_files.append({
            "preview": preview, "mime": mime, "data": data,
            "source": str(path), "source_sha256": hashlib.sha256(raw).hexdigest()
        })

    # Honor an explicitly approved local cover
    if artwork_decision is not None:
        if (
            artwork_decision.get("kind") != "local_artwork"
            or artwork_decision.get("status") != "pending_application"
            or sorted(artwork_decision.get("release_ids", []))
                != sorted(release_ids)
        ):
            raise ValueError("Artwork approval needs fresh review.")

        selected_path = Path(artwork_decision["path"]).resolve()

        approved = [
            item for item in prepared_files
            if (
                Path(item["source"]).resolve() == selected_path
                and item["source_sha256"] == artwork_decision["sha256"]
            )
        ]

        if len(approved) != 1:
            raise ValueError(
                "Approved artwork changed or is no longer available."
            )

        selected = approved[0]
        print("  Using explicitly approved local artwork.")

        return {
            "mime": selected["mime"],
            "data": selected["data"],
            "source": selected["source"]
        }

    # Use consistent embedded covers when no separate art exists
    if not local_files:
        selected = None
        for track in album["tracks"]:
            audio = File(track["path"])
            if isinstance(audio, FLAC):
                pictures = audio.pictures
            elif isinstance(audio, MP3):
                pictures = (
                    audio.tags.getall("APIC")
                    if audio.tags else []
                )
            else:
                selected = None
                break
            fronts = [p for p in pictures if p.type == 3]
            if len(fronts) != 1:
                selected = None
                break
            picture = fronts[0]
            if (
                picture.mime not in ("image/jpeg", "image/png")
                or not picture.data
                or len(picture.data) > 20_000_000
            ):
                selected = None
                break

            # Validate actual image format, not just its tag
            _, mime, _ = prepare_image(picture.data)
            if mime != picture.mime:
                raise ValueError("Embedded artwork MIME mismatch.")
            if selected is None:
                selected = {
                    "mime": mime,
                    "data": picture.data,
                    "source": "Consistent embedded front cover"
                }
            elif (
                selected["mime"] != mime
                or selected["data"] != picture.data
            ):
                raise ValueError(
                    "Embedded covers disagree across tracks."
                )
        if selected is not None:
            print("  Using consistent embedded front artwork.")
            return selected

    # Album-art lookup is an independent step from track identity. Explicitly
    # approved offline interpretations have no release ID; search by exact
    # album AND primary album artist rather than failing with no references.
    def candidates_by_album():
        title = album.get("album")
        artist = lookup.identify_artist(album.get("tracks") or [])
        if not artist or not title:
            return []
        return lookup.find_exact_album_releases(artist, title)

    candidate_ids = list(dict.fromkeys(release_ids))[:5]
    if not candidate_ids:
        candidate_ids = candidates_by_album()

    references = {}
    provider_failures = []
    for release_id in candidate_ids:
        artwork = None
        source = f"Cover Art Archive: {release_id}"
        try:
            artwork = lookup.download_release_art(release_id, preview=True)
            if artwork is None and re.fullmatch(r"[0-9a-fA-F-]{36}", str(release_id)):
                try:
                    metadata = lookup.get_release_metadata(release_id)
                    group_id = metadata.get("release_group_id")
                    if group_id:
                        artwork = lookup.download_release_group_art(group_id, preview=True)
                        source = f"Cover Art Archive release group: {group_id}"
                except (OSError, ValueError, lookup.musicbrainzngs.MusicBrainzError):
                    pass  # Missing group coverage does not contradict an available cover.
        except (OSError, ValueError, lookup.musicbrainzngs.MusicBrainzError) as error:
            provider_failures.append(type(error).__name__)
            continue
        if artwork is None:
            continue
        try:
            reference, _, _ = prepare_image(artwork["data"])
        except (OSError, ValueError):
            continue
        references[release_id] = {"preview": reference, "source": source,
                                  "group_id": source.rsplit(": ", 1)[-1] if "release group:" in source else None}
        local_matches = []
        for candidate in prepared_files:
            score = difference(candidate["preview"], reference)
            if os.environ.get("MIXIE_VERBOSE") == "1":
                print(f"  {Path(candidate['source']).name}: difference {score:.2f}")
            if score <= 6.0:
                local_matches.append(candidate)
        if local_matches:
            hashes = {hashlib.sha256(item["data"]).digest() for item in local_matches}
            if len(hashes) > 1:
                raise ArtworkReviewRequired(local_matches, candidate_ids)
            candidate = local_matches[0]
            print("  Using corroborated local artwork.")
            return {"mime": candidate["mime"], "data": candidate["data"],
                    "source": candidate["source"]}

    # Unmatched local art is not proof that the ALBUM cover is wrong. Use
    # independently supported online front art if it resolves unambiguously;
    # otherwise retain the old explicit-review safety behavior below.
    if not references:
        if local_files:
            raise ArtworkReviewRequired(prepared_files, candidate_ids)
        raise ValueError("No reference artwork available; verified cover required before import.")
    if len(references) < len(candidate_ids):
        print("  Some edition covers are unavailable; checking all available references.")
    if provider_failures and not references:
        raise ValueError("Artwork reference lookup was interrupted; retry when online providers recover.")

    # Compare up to five exact, album-matched edition candidates. If the art
    # differs, snapshot no more than three actual front covers for review.
    cover_groups = []
    for release_id, item in references.items():
        if not any(difference(item["preview"], earlier["preview"]) <= 6.0 for earlier in cover_groups):
            cover_groups.append({**item, "release_id": release_id})
    if len(cover_groups) > 1:
        # For retail editions with distinct front covers, prefer the shared
        # release-group album cover when all release IDs have the same known
        # group. Never confuse an unrelated recording's release-group cover.
        group_ids = set()
        try:
            for identifier in candidate_ids:
                group_id = lookup.get_release_metadata(identifier).get("release_group_id")
                if not group_id:
                    break
                group_ids.add(group_id)
            else:
                if len(group_ids) == 1:
                    art = lookup.download_release_group_art(next(iter(group_ids)))
                    if art is not None:
                        data, mime = mixie_artwork.normalized_image(art["data"])
                        print("  Using common MusicBrainz album-group cover (retail editions ignored).")
                        return {"mime": mime, "data": data, "source": "Cover Art Archive album group"}
        except (OSError, ValueError, lookup.musicbrainzngs.MusicBrainzError):
            pass
        options = []
        for item in cover_groups[:3]:
            try:
                if item["group_id"]:
                    chosen = lookup.download_release_group_art(item["group_id"])
                else:
                    chosen = lookup.download_release_art(item["release_id"])
                if chosen is None:
                    continue
                data, _ = mixie_artwork.normalized_image(chosen["data"])
                snapshot = mixie_artwork.snapshot_artwork(
                    data, root=Path(tag.script_folder).resolve() / ".review_artwork")
                options.append({"path": snapshot["path"], "data": data,
                                "source": item["source"], "label": item["source"]})
            except (OSError, ValueError, lookup.musicbrainzngs.MusicBrainzError):
                continue
        if options:
            raise ArtworkReviewRequired(options, candidate_ids)
        raise ValueError("Online cover variants need review, but none could be safely cached.")

    # A common supported online cover beats a single conflicting local cover;
    # the original stays preserved unchanged in the archive.
    if local_files:
        print("  Local artwork differs from known album covers; using verified online front art.")
    # All available reference covers agree. Download once in full quality.
    for release_id, item in references.items():
        if item["group_id"]:
            selected = lookup.download_release_group_art(item["group_id"])
        else:
            selected = lookup.download_release_art(release_id)
        if selected is None:
            continue
        data, mime = mixie_artwork.normalized_image(selected["data"])
        print("  Using matching Cover Art Archive artwork.")
        return {"mime": mime, "data": data, "source": item["source"]}
    raise ValueError("Selected online artwork is unavailable; artwork remains required.")


def _album_issue(album):
    staging = Path(tag.staging_folder).resolve()
    tracks = [Path(track["path"]).absolute().relative_to(staging).as_posix()
              for track in album["tracks"]]
    names = {Path(relative).parts[0] for relative in tracks}
    if not tracks or len(set(tracks)) != len(tracks) or len(names) != 1:
        raise ValueError("Album needs a unique full staged source inventory.")
    return {"import_name": next(iter(names)), "album": album["album"], "tracks": tracks}


def _decision_list(value) -> list[dict]:
    if value is None:
        return []
    values = [value] if isinstance(value, dict) else list(value)
    decisions: list[dict] = []
    for item in values:
        if not isinstance(item, dict) or item.get("status") != "pending_application":
            raise ValueError("Saved metadata decisions need fresh approval.")
        decisions.append(item)
    return decisions


def _effective_text_decisions(decisions, paths):
    """Resolve exact-source choices before field-wide defaults, regardless of order."""
    paths = {Path(path).resolve() for path in paths}
    scoped = {}
    for decision in _decision_list(decisions):
        field = decision.get("field")
        if field in tag._NUMBERING_FIELDS:
            continue
        if field not in ("title", "artist", "album", "album_artist", "date", "genre"):
            raise ValueError("Unsupported reviewed text field.")
        path = Path(decision["path"]).resolve() if decision.get("path") is not None else None
        if path is not None and path not in paths:
            raise ValueError("Reviewed text scope is outside the mapped sources.")
        key = (path, field)
        if key in scoped:
            raise ValueError("Duplicate reviewed text scope.")
        scoped[key] = decision
    result = {}
    for path in paths:
        for field in {key[1] for key in scoped}:
            decision = scoped.get((path, field), scoped.get((None, field)))
            if decision is not None:
                result[path, field] = decision
    return result


def _issue_decisions(issue) -> list[dict]:
    decisions = list(issue.get("decisions") or [])
    for old in _decision_list(issue.get("decision")):
        if not any((item.get("path"), item.get("field")) == (old.get("path"), old.get("field")) for item in decisions):
            decisions.append(old)
    return _decision_list(decisions)


def _release_totals(tracks):
    by_disc = {}
    for track in tracks:
        by_disc.setdefault(str(track["disc"]), set()).add(str(track["number"]))
    return {"disc_total": len(by_disc), "album_total": len(tracks),
            "track_totals": {disc: len(numbers) for disc, numbers in by_disc.items()}}


def _reviewed_numbering(matched, totals):
    proposals = []
    fields = ("track_number", "track_total", "disc_number", "disc_total")
    for (disc, number), path in matched.items():
        metadata = tag.read_track_metadata(path)
        if metadata is None:
            raise ValueError("Cannot read reviewed source numbering.")
        values = {"disc_number": int(disc), "track_number": int(number), **totals[str(Path(path).resolve())]}
        snapshot = {field: tag._numbering_existing(metadata, field) for field in fields}
        for field, expected in values.items():
            if tag._numbering_agrees(metadata, field, expected, (expected,)):
                continue
            proposal = {"path": path, "field": field, "value": expected, "verified": False,
                        "user_approved": True}
            if snapshot[field]:
                proposal.update(previous=snapshot[field], numbering_snapshot=snapshot)
            proposals.append(proposal)
    return proposals


def _primary_credit(value):
    """Normalize only explicit featured-artist suffixes, not band-name joins."""
    if not isinstance(value, str):
        return ""
    credit = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s+", value.strip(), maxsplit=1,
                      flags=re.IGNORECASE)[0]
    return tag._normalize_label(credit)


def _suggest_album_artist(tracks):
    """Conservative *hint* for the dominant lead artist, not release proof.

    A strict majority alone could mislabel compilations, split releases or
    soundtracks. Require >=80% of complete, explicit lead credits, at least
    four tracks for non-unanimous evidence, and no conflicting album-artist
    tags. Never overwrite track collaborations or an established album credit.
    """
    from collections import Counter
    if not tracks:
        return None
    explicit_album_artists = set()
    artist_rows = []
    for track in tracks:
        album_values = track.get("album_artist") or ()
        if len(album_values) > 1:
            return None
        if album_values and str(album_values[0]).strip():
            explicit_album_artists.add(tag._normalize_label(album_values[0]))
        credits = track.get("artist") or ()
        if len(credits) != 1 or not isinstance(credits[0], str) or not credits[0].strip():
            return None
        raw = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s+", credits[0].strip(),
                       maxsplit=1, flags=re.IGNORECASE)[0].strip()
        lead = _primary_credit(raw)
        if not lead or lead in {"various artists", "unknown artist"}:
            return None
        artist_rows.append((lead, raw))
    if len(explicit_album_artists) > 1:
        return None
    counts = Counter(lead for lead, _ in artist_rows)
    leader, count = counts.most_common(1)[0]
    if count != len(artist_rows) and (len(artist_rows) < 4 or count / len(artist_rows) < .80):
        return None
    if len(explicit_album_artists) == 1 and leader not in explicit_album_artists:
        return None
    return next(raw for normalized, raw in artist_rows if normalized == leader)


def _suggest_compilation_album_artist(tracks):
    """Show a Various Artists review hint; never grant write authority."""
    if len(tracks) < 4:
        return None
    lead = [_primary_credit(track["artist"][0]) for track in tracks
            if len(track.get("artist") or ()) == 1]
    if len(lead) != len(tracks) or not all(lead):
        return None
    from collections import Counter
    values = Counter(lead)
    return "Various Artists" if len(values) >= 3 and values.most_common(1)[0][1] < .6 * len(tracks) else None


def _catalog_genre_hint(album, album_artist):
    """Artist's modal genre: independent albums vote once, never per track."""
    from collections import Counter
    artist_key = _primary_credit(album_artist)
    if not artist_key: return None
    target = {Path(row['path']).resolve() for row in album['tracks']}
    # Genre is an optional hint, never an ingest prerequisite. One damaged
    # unrelated album must not crash a verified album's import preview.
    from mutagen import MutagenError
    catalog=[]
    for reader in (tag.group_library_albums, tag.group_staged_albums):
        try:
            groups=reader()
            catalog.extend(groups.values())
        except (OSError, ValueError, TypeError, MutagenError):
            continue
    sample={}
    for entry in catalog:
        rows=entry.get('tracks') or []
        if not rows or any(Path(row['path']).resolve() in target for row in rows): continue
        artists=set();names=set();genres=set()
        for row in rows:
            credit=row.get('album_artist') or row.get('artist') or ()
            name=row.get('album') or ()
            genre=row.get('genre') or ()
            if len(credit)!=1: artists.add(None)
            else: artists.add(_primary_credit(credit[0]))
            if len(name)==1:names.add(tag._normalize_label(name[0]))
            if len(genre)==1 and genre[0].strip() and genre[0].casefold()!='unclassified':
                genres.add(genre[0].strip())
        if artists!={artist_key} or len(genres)!=1 or len(names)!=1: continue
        name=next(iter(names));genre=next(iter(genres))
        if name in sample and sample[name]!=genre:sample[name]=None
        else:sample[name]=genre
    valid=[genre for genre in sample.values() if genre]
    if not valid:return None
    votes=Counter(genre.casefold() for genre in valid)
    winner,count=votes.most_common(1)[0]
    if count*4<len(valid)*3 or list(votes.values()).count(count)>1:return None
    choice=next(genre for genre in valid if genre.casefold()==winner)
    try:tag.validate_manual_metadata({'genre':choice})
    except (ValueError,TypeError):return None
    return choice


def _auto_album_metadata(album, common, evaluation, *, artist_hint=None, genre_hint=None):
    """Canonical tag proposals AFTER independently established recording mapping."""
    from collections import Counter
    fields={}
    title=common.get('album')
    if isinstance(title,str) and title.strip():
        fields['album']=title.strip()
        labels=[v.strip() for row in album['tracks'] for v in row.get('album',()) if v.strip()]
        # A single incorrectly tagged song must not downgrade an otherwise
        # corroborated deluxe album to the plain retail display title.
        if labels and len(evaluation.get('tracklist',[]))==len(album['tracks']):
            variants=Counter(tag._normalize_label(value) for value in labels)
            preferred,count=variants.most_common(1)[0]
            if count*4>=len(labels)*3:
                original=next(v for v in labels if tag._normalize_label(v)==preferred)
                suffix=re.fullmatch(r'(.+?)\s*\(\s*(Deluxe|Expanded)(?: Edition)?\s*\)',original,re.I)
                if suffix and tag._normalize_label(suffix[1])==tag._normalize_label(title):
                    # Refuse if the dissenters claim an unrelated album.
                    if all(tag._normalize_label(v) in (preferred,tag._normalize_label(title)) for v in labels):
                        fields['album']=f'{suffix[1].strip()} ({suffix[2].title()})'
    artist=common.get('album_artist') or artist_hint
    if isinstance(artist,str) and artist.strip():fields['album_artist']=artist.strip()
    known = common.get('date')
    if isinstance(known, str) and known.strip():
        fields['date'] = tag.validate_manual_metadata({'date': known})['date']
    # A source date is a fallback, not evidence against a verified release date.
    genres = [v.strip() for row in album['tracks'] for v in row.get('genre', ())
              if not tag.is_uncertain_genre(v)]
    if genres:
        votes=Counter(v.casefold() for v in genres);winner,count=votes.most_common(1)[0]
        if count>=3 and count*4>=len(genres)*3 and list(votes.values()).count(count)==1:
            fields['genre']=next(v for v in genres if v.casefold()==winner)
    elif genre_hint and not tag.is_uncertain_genre(genre_hint):
        fields['genre'] = genre_hint
    return fields


def _edition_title_key(title):
    """Treat explicitly unmodified/original mix labels as nonmaterial typography.

    Never collapse remixers, live/acoustic versions, radio edits, remasters,
    featuring credits beyond the existing normalization policy, or take a
    best-scoring candidate in place of missing evidence.
    """
    value = lookup.normalize_title(title)
    value = re.sub(r"(?:\s+-\s+|\s*\()(?:(?:original|album) (?:mix|version))\)?$", "", value).strip()
    # Equivalent square-bracket label punctuation, NOT equivalent audio versions.
    value = re.sub(r"\[([^\[\]]+)\]", r" - \1", value)
    # Editorial remaster labels alone cannot establish a different recording.
    # Live, remix, acoustic, edit and alternate takes remain material.
    value = re.sub(r"\s*(?:\(|\s+-\s*)(?:(?:\d{4}\s+)?(?:digital\s+)?remaster(?:ed)?)\)?$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _corroborated_release_type(album, common, release_ids, audits):
    expected = common.get("album")
    if not isinstance(expected, str):
        return None
    normalize = tag._normalize_label
    strip_single = lambda value: value.removesuffix(" - single")
    expected = normalize(expected)
    needs_type = any(normalize(value) != expected and strip_single(normalize(value)) == strip_single(expected)
                     for track in album["tracks"] for value in track.get("album", ()))
    if not needs_type:
        return None
    types = {identifier: set() for identifier in release_ids}
    for audit in audits:
        for release in (audit.get("details") or {}).get("release-list", []):
            if release.get("id") in types:
                group = release.get("release-group", {})
                kind = release.get("type") or group.get("primary-type") or group.get("type")
                if isinstance(kind, str):
                    types[release["id"]].add(kind.strip().casefold())
    for identifier, values in types.items():
        if values:
            continue
        try:
            metadata = lookup.get_release_metadata(identifier)
            # Metadata and corroborated primary type arrive together; do not
            # issue a redundant request or infer the type when it is absent.
            kind = metadata.get("release_type") or metadata.get("type")
            if isinstance(kind, str):
                values.add(kind.strip().casefold())
        except (OSError, ValueError, TypeError, KeyError, AttributeError, lookup.musicbrainzngs.MusicBrainzError) as error:
            # A type lookup outage cannot turn a storefront label into evidence.
            print(f"Release type remains uncorroborated for {identifier}: {error}")
    return "Single" if types and all(values == {"single"} for values in types.values()) else None


def _musical_release_signature(evaluation):
    """Canonical ordered (source, recording) sequence; ignore physical media."""
    tracks = {(int(row["disc"]), int(row["number"])): row
              for row in evaluation["tracklist"]}
    return tuple((str(Path(evaluation["matched"][position]).resolve()),
                  tracks[position]["recording_id"])
                 for position in sorted(evaluation["matched"]))


def _source_names_explicit_edition(title):
    """Protect intentional retail/bonus/pressing selections from US fallback."""
    text = str(title or "")
    return bool(re.search(r"\([A-Za-z]{2,10}[- ]\d{2,8}\)\s*$", text)
                or re.search(
                    r"\b(?:deluxe|expanded|extended|anniversary|bonus|special edition|remix|live|acoustic)\b",
                    text, flags=re.I))


def _preferred_music_release(compatible, tracks, *, standard_us_digital=False):
    """Choose a representation of the same independently verified music.

    Standard US digital is only an edition *tie-breaker* after complete audio
    mapping and musical-sequence agreement. Country or track numbers never
    serve as evidence that two different audio recordings are equivalent.
    """
    source_discs = set()
    for track in tracks:
        value = (track.get("disc_number") or ())
        number = tag.parse_disc_number(value)[0] if value else None
        if isinstance(number, int) and number > 0:
            source_discs.add(number)
    wanted = len(source_discs) if len(source_discs) > 1 else 1
    def score(item):
        media = len({int(row["disc"]) for row in item[1]["tracklist"]})
        if not standard_us_digital:
            return (media != wanted, media, str(item[0]))
        formats = {str(row.get("medium_format") or "").strip().casefold()
                   for row in item[1]["tracklist"]}
        digital = formats == {"digital media"}
        try:
            metadata = lookup.get_release_metadata(item[0])
        except AssertionError:
            raise  # Preserve the isolated test runner's provider safety guard.
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                lookup.musicbrainzngs.MusicBrainzError):
            metadata = {}
        country = str(metadata.get("country") or "").strip().upper()
        status = str(metadata.get("status") or "").strip().casefold()
        # Explicitly unofficial/bootleg releases are never preferred over
        # official or unknown-status equivalents.
        unofficial = bool(status) and status != "official"
        # Exact sequence length is filtered by caller; no deluxe/bonus content
        # can become complete merely by ranking it above a standard edition.
        digital_region = (0 if digital and country == "US" else
                          1 if digital and country in ("XW", "") else
                          2 if digital else
                          3 if country == "US" else 4)
        return (unofficial, digital_region, media != wanted, media, str(item[0]))
    return min(compatible, key=score)



def _overlapping_artist_credit(source, identified):
    """Use one shared lead artist when a joint credit overlaps a solo credit.

    Only explicit, whole artist names count. A substring like "Halo" must
    never match "Halestorm", and unrelated artists never get collapsed.
    """
    if not isinstance(source, str) or not isinstance(identified, str):
        return None
    def parts(value):
        return [part.strip() for part in re.split(r"\s+(?:&|and)\s+", value, flags=re.I)
                if part.strip()]
    local, remote = parts(source), parts(identified)
    if len(local) == len(remote) == 1:
        return None
    matched = {tag._normalize_label(part) for part in local} & {
        tag._normalize_label(part) for part in remote}
    if len(matched) != 1:
        return None
    value = next(iter(matched))
    return next(part for part in remote if tag._normalize_label(part) == value)


def _feature_aware_identity(details, release_title, title, existing):
    """Use documented guest credit in title; retain the lead artist for sorting.

    An unsupported source-only feature assertion is not identity proof; two
    different known guest names cannot silently be treated as the same take.
    Genuine joint artists ("and", "&") are never demoted by this rule.
    """
    remote_artist = details.get("artist-credit-phrase") or details.get("artist") or ""
    if not isinstance(remote_artist, str) or not remote_artist.strip():
        raise ValueError("Audio recording lacks supported performer credit.")
    feat = re.compile(r"(?:\(|\[)?\s*(?:feat\.?|ft\.?|featuring)\s+([^\])]+)", re.I)
    guest_match = feat.search(remote_artist)
    lead = remote_artist[:guest_match.start()].strip() if guest_match else remote_artist.strip()
    lead = lead.rstrip(" ([,-")
    if not lead:
        raise ValueError("Invalid lead artist credit.")
    source_title = (existing.get("title") or ("",))[0] if existing else ""
    source_artist = (existing.get("artist") or ("",))[0] if existing else ""
    # User preference: Halestorm vs Halestorm & I Prevail becomes Halestorm,
    # not a manual-review conflict. The recording's source remains unmodified.
    lead = _overlapping_artist_credit(source_artist, lead) or lead
    claimed = feat.search(source_title) or feat.search(source_artist)
    release_guest = feat.search(release_title or "")
    remote_guest = guest_match or release_guest
    # The source can name a different guest than the standard release. If the
    # source/release song titles agree after dropping feature-credit suffixes,
    # prefer the selected release's actual credits on the independent copy.
    # Do not turn a genuinely different song/live/remix title into a match.
    same_song = (isinstance(source_title, str) and bool(source_title.strip())
                 and isinstance(release_title, str) and bool(release_title.strip())
                 and lookup.normalize_title(source_title) == lookup.normalize_title(release_title))
    if claimed and remote_guest and tag._normalize_label(claimed[1]) != tag._normalize_label(remote_guest[1]):
        # "Rory Rodriguez of Dayseeker" and "Dayseeker" refer to the same
        # documented guest. Other disputed guests use release credit only when
        # the exact song name is corroborated by the matched release.
        expected_band = tag._normalize_label(claimed[1])
        verified_guest = tag._normalize_label(remote_guest[1])
        if not ((expected_band and verified_guest.endswith(" of " + expected_band)) or same_song):
            raise ValueError("Different featured performers are credited for different songs; review required.")
    if claimed and remote_guest is None and not same_song:
        raise ValueError("Source names a guest performer but the selected recording cannot corroborate it.")
    result_title = title
    if remote_guest and not feat.search(result_title or ""):
        if release_guest and isinstance(release_title, str):
            result_title = release_title
        elif (source_title and feat.search(source_title)
              and lookup.normalize_title(source_title) == lookup.normalize_title(result_title)
              and tag._normalize_label(feat.search(source_title)[1])
              == tag._normalize_label(remote_guest[1])):
            # Source may supply the same verified feature, but an unsupported
            # different guest must NEVER survive as the output title.
            result_title = source_title
        else:
            result_title = f"{result_title} (feat. {remote_guest[1].strip()})"
    return result_title, lead


def _confirmed_complete_single_disc(source_tracks, expected):
    """Corroborated source completeness for candidate *count* screening only.

    This never authenticates song names, sequence, release identity or audio.
    Duplicated/unknown numbers, conflicting totals and multidisc collections
    leave all candidates eligible.
    """
    if expected < 3 or len(source_tracks) != expected:
        return False
    numbers = set()
    for row in source_tracks:
        if not isinstance(row, dict):
            return False
        if tag.get_track_totals(row) != {expected}:
            return False
        number, embedded = tag.parse_track_number(row.get("track_number") or ())
        if number is None or (embedded is not None and embedded != expected):
            return False
        disc = row.get("disc_number") or ()
        if disc and (len(disc) != 1 or str(disc[0]).split("/")[0].strip() != "1"):
            return False
        if number in numbers:
            return False
        numbers.add(number)
    return numbers == {f"{number:02d}" for number in range(1, expected + 1)}


@lookup.musicbrainz_album()
def preview_album(
    album, api_key, reviewed_decision=None, artwork_decision=None,
    manual_fields=None, interpretation=None, tag_edits=None, *, auto_mode=False
):
    paths = sorted(Path(track["path"]).resolve() for track in album["tracks"])
    source_issue = _album_issue(album)
    source_manifest = completion_source_manifest(source_issue)
    # The US standard digital fallback applies only to a generic album name.
    # An explicit catalog/pressing reference must not be silently replaced.
    standard_album_preference = auto_mode and not _source_names_explicit_edition(
        album.get("album"))
    fingerprint = completion_fingerprint(source_issue["import_name"], _source_rows(source_issue, source_manifest), source_manifest)
    edits = _validated_tag_edits(tag_edits, paths)
    decisions = _decision_list(reviewed_decision)
    numbering = [item for item in decisions if item.get("field") in tag._NUMBERING_FIELDS]
    text = [item for item in decisions if item.get("field") not in tag._NUMBERING_FIELDS]
    common_decisions = [item for item in text if item.get("field") not in ("artist", "title")]
    terminal.header(f"Scanning: {album['album']}", f"{len(paths)} tracks")
    terminal.pass_status(1, "Audio fingerprint identification")
    evaluations, audits, prepared = [], [], {}
    overrides = {}
    if interpretation is not None:
        prepared = review.prepare_interpretation(album, interpretation, fingerprint)
        matched = prepared["matched"]
        tracklist = prepared["release_tracks"]
        identities = deepcopy(prepared["identities"])
        common = prepared["common"]
        compatible_ids = [interpretation["release_id"]] if interpretation.get("release_id") else []
        manual_fields = tag.validate_manual_metadata(manual_fields)
        if any(field in prepared["manual_fields"] and prepared["manual_fields"][field] != value
               for field, value in manual_fields.items()):
            raise ValueError("Saved manual metadata conflicts with the approved interpretation; review both choices.")
        manual_fields = {**manual_fields, **prepared["manual_fields"]}
        audits = [{"path": path, "status": "user_approved", "reason": "Explicit interpretation; not independently verified.",
                   "details": identities[str(path)], "existing": metadata}
                  for path, metadata in ((Path(track["path"]).resolve(), track) for track in album["tracks"])]
        if prepared["numbering_deferred"]:
            raise review.AlbumReviewRequired(review.issue_details(
                audits, [], "Approved interpretation still needs explicit full-release or offline totals."))
        selected_numbering = _numbering_edit_approvals(matched, tracklist, edits, numbering,
            numbering_totals=prepared["numbering_totals"] if not interpretation.get("release_id") else None)
        proposals = (tag.propose_release_numbering(matched, tracklist, reviewed_decision=selected_numbering or None,
                                                   allow_partial=True)
                     if interpretation.get("release_id") else _reviewed_numbering(matched, prepared["numbering_totals"]))
        overrides = dict(common)
    else:
        verbose = os.environ.get("MIXIE_VERBOSE", "0") == "1"
        if verbose:
            audits = audio.audit_tracks(paths, api_key, cache={})
        else:
            # Genuine per-track status without modifying the scanner's API.
            output = sys.stdout
            observed = [0]
            by_path = {Path(row["path"]).resolve(): row for row in album["tracks"]}
            def progress(phase, index, total, path, result):
                if phase == "complete":
                    observed[0] += 1
                terminal.scan_event(phase, index, total, path, result,
                                    metadata=by_path.get(Path(path).resolve()), stream=output)
            with audio.audit_progress(progress), redirect_stdout(StringIO()):
                audits = audio.audit_tracks(paths, api_key, cache={})
            if observed[0] == 0:
                terminal.track_summary(audits, album["tracks"], tag_service=tag)
        existing = {Path(track["path"]).resolve(): track for track in album["tracks"]}
        for audit in audits:
            audit["existing"] = existing.get(Path(audit["path"]).resolve())
        if len(audits) != len(paths) or {Path(row["path"]).resolve() for row in audits} != set(paths):
            raise ValueError("Audio audit did not account for the full album inventory.")
        compatible = []
        number_reasons = {f"{prefix}{field}{suffix}" for prefix in ("local_", "invalid_local_")
                          for field in tag._NUMBERING_FIELDS for suffix in (("_conflict",) if prefix == "local_" else ("",))}
        terminal.pass_status(2, "Album context and release comparison")
        terminal.album_stage("Looking up album release editions...")
        candidate_ids = lookup.discover_release_candidates(
            audits, local_album=album["album"],
            local_artist=_suggest_album_artist(album["tracks"]))
        # A catalog-number suffix is a filename/edition hint, not a distinct
        # musical album. Search the exact core title early, but never let this
        # text-only discovery authorize a recording without audio evaluation.
        if auto_mode and len(candidate_ids) > 24:
            title_hint = album.get("album")
            artist_hint = _suggest_album_artist(album["tracks"])
            if isinstance(title_hint, str) and artist_hint:
                core_title = re.sub(r"\s*\([A-Z]{2,}[- ]?\d{2,}\)\s*$", "", title_hint,
                                    flags=re.IGNORECASE).strip()
                if core_title != title_hint.strip():
                    try:
                        ranked = lookup.find_exact_album_releases(artist_hint, core_title)
                    except AssertionError:
                        raise
                    except (OSError, ValueError, TypeError, KeyError,
                            lookup.musicbrainzngs.MusicBrainzError):
                        ranked = []
                    if ranked:
                        candidate_ids = list(dict.fromkeys(ranked + candidate_ids))
                        terminal.album_stage(f"Catalog-suffix album search prioritized {len(ranked)} edition(s)")
        # Prioritize a named catalog pressing while staying within the same
        # 96-release budget. Search result order is never audio/edition proof.
        if auto_mode and len(candidate_ids) > 24:
            label = album.get("album")
            hint = _suggest_album_artist(album["tracks"])
            catalog = re.fullmatch(r"(.+?)\s*\(([A-Za-z]{2,10})[- ](\d{2,8})\)\s*", label or "")
            if catalog and hint:
                named, code = catalog.group(1).strip(), f"{catalog.group(2)} {catalog.group(3)}"
                prioritized = _online_album_call(
                    lookup.find_catalog_number_releases, audits, evaluations,
                    hint, named, code)
                if prioritized:
                    candidate_ids = list(dict.fromkeys(prioritized + candidate_ids))
                    terminal.album_stage(f"Catalog search nominated {len(prioritized)} edition(s) for audio verification")
        # Only a confirmed complete single-disc delivery may reject a retail
        # edition with independently indexed, incompatible audio-track count.
        # The fingerprint/recording gates stay mandatory after count screening.
        if (auto_mode and len(candidate_ids) > lookup.MAX_RELEASE_EVALUATIONS
                and _confirmed_complete_single_disc(album["tracks"], len(paths))):
            candidate_ids, excluded_counts = lookup.filter_release_ids_by_audio_track_count(
                candidate_ids, len(paths))
            if excluded_counts:
                terminal.album_stage(
                    f"Audio-track count excluded {len(excluded_counts)} incompatible edition(s); "
                    f"{len(candidate_ids)} candidate(s) retained")
        checked_ids = candidate_ids[:lookup.MAX_RELEASE_EVALUATIONS]
        # After a bounded first pass, search the *named album* itself rather
        # than checking an endless parade of compilation/retail releases.
        # This is discovery only; every returned release must still pass the
        # exact audio-to-tracklist evaluator and the normal import gates.
        targeted_done = False
        for release_index, release_id in enumerate(checked_ids, 1):
            if (auto_mode and not targeted_done and not compatible
                    and len(candidate_ids) > 24 and release_index == min(8, len(checked_ids))):
                targeted_done = True
                album_name = album.get("album")
                album_artist_hint = _suggest_album_artist(album["tracks"])
                if (isinstance(album_name, str) and album_name.strip()
                        and tag._normalize_label(album_name) not in ("unknown album", "unknown", "untitled")
                        and album_artist_hint):
                    try:
                        targeted = lookup.find_exact_album_releases(album_artist_hint, album_name)
                    except AssertionError:
                        raise  # offline test guard: never hide unexpected provider calls
                    except (OSError, ValueError, TypeError, KeyError,
                            lookup.musicbrainzngs.MusicBrainzError):
                        targeted = []  # provider failure is not negative album evidence
                    if targeted:
                        consumed = set(checked_ids[:release_index])
                        front = [identifier for identifier in targeted
                                 if identifier not in consumed and isinstance(identifier, str)]
                        # Include newly discovered IDs in the retained audit set.
                        for identifier in front:
                            if identifier not in candidate_ids:
                                candidate_ids.append(identifier)
                        unique = list(dict.fromkeys(checked_ids[:release_index] + front + candidate_ids))
                        checked_ids[:] = unique[:lookup.MAX_RELEASE_EVALUATIONS]
                        terminal.album_stage(f"Exact album search prioritized {len(front)} release candidate(s)")
            terminal.album_stage("Checking editions", release_index, len(checked_ids))
            evaluation = lookup.evaluate_release_candidate(audits, release_id, local_tracks=album["tracks"])
            evaluations.append(evaluation)
            mapping = evaluation["matched"]
            fully_accounted = (bool(mapping) and len(mapping) == len(paths)
                               and {Path(path).resolve() for path in mapping.values()} == set(paths)
                               and not evaluation.get("errors")
                               and getattr(evaluation["tracklist"], "complete", True))
            is_partial_album = len(mapping) < len(evaluation["tracklist"])
            only_numbering = set(evaluation["reasons"]) <= number_reasons | {
                "resolved_by_independent_anchors", "resolved_by_whole_album_fingerprints",
                "resolved_by_audio_verified_title", "resolved_by_album_context", "missing_release_tracks"}
            if not fully_accounted or not (evaluation["status"] == "complete" or only_numbering):
                continue
            # In automatic mode, a release can account for EVERY supplied audio
            # file without claiming that the user owns all of its tracks.
            # Single-song album inference is handled by a separate, stricter
            # album-identity policy below.
            if is_partial_album:
                independent_count = sum(lookup._accepted_recording(row, independent=True) is not None for row in audits)
                if (not auto_mode or independent_count < 3
                        or source_manifest.get("provenance") is None):
                    # Without several independent fingerprints and managed
                    # source provenance, keep the existing human approval path.
                    continue
            remote = {(int(track["disc"]), int(track["number"])): track for track in evaluation["tracklist"]}
            signature = tuple(sorted((str(Path(path).resolve()), int(disc), int(number),
                                      remote[(int(disc), int(number))]["recording_id"])
                                     for (disc, number), path in mapping.items()))
            compatible.append((release_id, evaluation, signature))
            # Stop early only when complete membership evidence has excluded
            # every unchecked competitor. A first good pressing alone cannot
            # settle recording versions or an alternative musical sequence.
            if auto_mode and not is_partial_album:
                remaining = [identifier for identifier in candidate_ids
                             if identifier not in {row['release_id'] for row in evaluations}]
                consensus = lookup.compare_release_candidate_reports(
                    audits, evaluations, unexamined_release_ids=remaining,
                    standard_album_preference=standard_album_preference)
                if consensus['status'] == 'complete':
                    break
        scanned_ids = {row.get("release_id") for row in evaluations}
        unexamined_ids = [identifier for identifier in candidate_ids if identifier not in scanned_ids]
        if unexamined_ids:
            evaluations.append({
                "release_id": None, "status": "partial", "matched": {}, "missing": [], "review": paths,
                "reasons": ["release_evaluation_budget_exhausted"], "tracklist": [], "errors": [],
                "unexamined_release_ids": unexamined_ids,
            })
            if not auto_mode or not compatible:
                raise _identity_review_error(
                    audits, evaluations, "Release evaluation budget exhausted; unexamined candidates require review.")
            # Full albums must pass the cross-release consensus gate below.
            # For a partially supplied album, require independent audio anchors,
            # managed provenance and a named parent before allowing the standard
            # baseline preference to proceed. Preserve all unexamined candidates
            # in the evidence instead of calling them disproven.
            partial_candidates = any(len(item[1]["tracklist"]) != len(paths)
                                     for item in compatible)
            if partial_candidates:
                anchors = sum(lookup._accepted_recording(row, independent=True) is not None
                              for row in audits)
                known_album = (isinstance(album.get("album"), str)
                               and tag._normalize_label(album["album"])
                               not in ("unknown album", "unknown", "untitled"))
                if (anchors < 3 or len(paths) < 3
                        or source_manifest.get("provenance") is None or not known_album):
                    raise _identity_review_error(
                        audits, evaluations, "Partial album still lacks sufficient evidence to choose a baseline.")
                terminal.album_stage(
                    f"Using audio-supported partial album; {len(unexamined_ids)} retail candidate(s) retained as unknown")
        if not compatible:
            raise _identity_review_error(
                audits, evaluations, "No complete, independently supported release mapping; review identity/release evidence.")
        if auto_mode:
            if any(len(item[1]['tracklist']) == len(paths) for item in compatible):
                consensus = lookup.compare_release_candidate_reports(
                    audits, evaluations, unexamined_release_ids=unexamined_ids,
                    standard_album_preference=standard_album_preference)
                if consensus['status'] != 'complete':
                    raise _identity_review_error(
                        audits, evaluations,
                        'Album recording/version evidence remains unresolved: '
                        + ', '.join(consensus['reasons']))
            anchors = sum(lookup._accepted_recording(row, independent=True) is not None for row in audits)
            cohort_supported = any(("resolved_by_whole_album_fingerprints" in
                                    item[1].get("reasons", ()) or
                                    "resolved_by_audio_verified_title" in item[1].get("reasons", ()) or
                                    "resolved_by_album_context" in item[1].get("reasons", ())) and
                                   len(item[1].get("matched", {})) == len(paths) == len(item[1].get("tracklist", ()))
                                   for item in compatible)
            if anchors < (1 if len(paths) == 1 else 2) and not cohort_supported:
                raise _identity_review_error(audits, evaluations,
                    "Album context lacks independent fingerprint anchors.")
            complete = [item for item in compatible if len(item[1]["tracklist"]) == len(paths)]
            pool = complete or compatible
            # Source-number agreement is NOT release evidence. Distinct full
            # musical orders/IDs require a real comparison, not guessing the
            # standard release by whichever edition preserves bad source tags.
            if complete and len({lookup._musical_song_signature(item[1]) for item in complete}) > 1:
                raise _identity_review_error(audits, evaluations,
                    "Verified releases disagree on audio sequence or recording identity; standard edition unclear.")
            # Whole-album cohort support must not silently choose between
            # incompatible complete musical sequences (e.g. clean/explicit).
            if anchors < 2 and len({lookup._musical_song_signature(item[1]) for item in pool}) > 1:
                raise _identity_review_error(audits, evaluations,
                    "Fingerprint-supported releases disagree on recording identities or order.")
            # A partially supplied collection needs a definite parent album
            # rather than arbitrarily choosing a larger bonus release.
            if not complete and album.get("album") in (None, "Unknown Album"):
                raise _identity_review_error(audits, evaluations,
                    "Untagged partial album has no unique parent album yet.")
            def rank(item):
                candidate = item[1]
                media = len({int(row["disc"]) for row in candidate["tracklist"]})
                # Never reward existing source track numbers: those are what
                # we are trying to REPAIR. Prefer a complete, simple musical
                # representation, then a stable release identifier.
                return (len(candidate["tracklist"]), media, str(item[0]))
            representative = min(pool, key=rank)
            selected_signature = lookup._musical_song_signature(representative[1])
            compatible = [row for row in pool if lookup._musical_song_signature(row[1]) == selected_signature]
            # When the source names no deliberate retail pressing or edition,
            # choose the user's US standard digital representation among the
            # *already audio-verified and musically equivalent* releases.
            if standard_album_preference:
                representative = _preferred_music_release(
                    compatible, album["tracks"], standard_us_digital=True)
            terminal.album_stage(f"Music-first match: {len(compatible)} equivalent release(s); "
                                 f"{len(paths)} of {len(representative[1]['tracklist'])} album tracks present")
        else:
            # Compare MUSIC, not retail media. CD, digital and vinyl may divide
            # identical ordered recordings among different physical discs.
            # The full, distinct audio identities must still agree; bonus editions
            # with absent tracks are never complete compatible matches.
            signatures = {_musical_release_signature(item[1]) for item in compatible}
            terminal.album_stage(f"Equivalent musical editions: {len(compatible)} releases / {len(signatures)} tracklist groups")
            if len(signatures) != 1:
                raise review.AlbumReviewRequired(review.issue_details(
                    audits, evaluations, "Compatible releases disagree on musical order or recording identities."))
            unknown_release_reasons = {"release_lookup_unavailable", "remote_tracklist_incomplete",
                                       "empty_release_tracklist", "invalid_release_positions_or_identity"}
            uncertain = [item for item in evaluations if item.get("errors")
                         or not getattr(item["tracklist"], "complete", True)
                         or unknown_release_reasons.intersection(item["reasons"])]
            # An unknown retail release is not negative evidence against an entire
            # independently anchored album. For a multi-song album, allow a fully
            # accounted compatible recording sequence to win if >=75% of its tracks
            # have independently verified audio IDs and no checked full competitor
            # disagrees. Never use the unknown release as metadata/numbering proof.
            # One-song releases and weakly anchored albums retain strict review.
            anchored = sum(lookup._accepted_recording(item, independent=True) is not None for item in audits)
            sufficient = len(paths) >= 3 and anchored * 4 >= len(paths) * 3
            chosen_mapping = compatible[0][1]
            chosen_remote = {(int(row["disc"]), int(row["number"])): row["recording_id"]
                             for row in chosen_mapping["tracklist"]}
            chosen_by_source = {Path(owner).resolve(): chosen_remote[position]
                                for position, owner in chosen_mapping["matched"].items()}
            known_conflict = False
            for partial in uncertain:
                if not isinstance(partial.get("matched"), dict):
                    known_conflict = True
                    break
                partial_remote = {(int(row["disc"]), int(row["number"])): row["recording_id"]
                                  for row in partial.get("tracklist", [])
                                  if isinstance(row, dict) and str(row.get("disc", "")).isdigit()
                                  and str(row.get("number", "")).isdigit()
                                  and isinstance(row.get("recording_id"), str)}
                for position, owner in partial["matched"].items():
                    if (position not in partial_remote or
                            partial_remote[position] != chosen_by_source.get(Path(owner).resolve())):
                        known_conflict = True
                        break
            if uncertain and (not sufficient or known_conflict):
                raise _identity_review_error(
                    audits, evaluations, "A competing release may change an unresolved recording; review its evidence.")
            if uncertain:
                terminal.album_stage(f"  {len(uncertain)} incomplete retail edition(s) ignored; "
                                     "complete audio-supported album sequence retained")
        # Prefer the normalized one-disc album if no explicit source evidence
        # calls for several discs. Never erase genuine multi-disc structure.
        release_id, evaluation, _unused = (_preferred_music_release(compatible, album["tracks"])
                                              if not auto_mode else representative)
        compatible_ids = [item[0] for item in compatible]
        automatic_partial = auto_mode and len(evaluation["tracklist"]) > len(paths)
        tracklist = [{**track, "disc": str(int(track["disc"])), "number": str(int(track["number"]))}
                     for track in evaluation["tracklist"]]
        matched = {(str(disc), str(number)): Path(path).resolve() for (disc, number), path in evaluation["matched"].items()}
        common = _online_album_call(lookup.common_release_metadata, audits, evaluations, compatible_ids)
        if len(paths) == 1 and not (common.get("album") and common.get("album_artist")):
            raise review.AlbumReviewRequired(review.issue_details(audits, evaluations, "Single-track release editions disagree."))
        # Personal library convention: an independently verified one-track
        # Single with a matching canonical song/album title is filed as
        # "Song Name (Single)".  Never relabel a real one-track album, EP,
        # distinct named single, or a release with unverified primary type.
        if auto_mode and len(paths) == len(tracklist) == 1 and compatible_ids:
            album_title = common.get("album")
            song_title = tracklist[0].get("title")
            if (isinstance(song_title, str) and song_title.strip()
                    and isinstance(album_title, str) and album_title.strip()):
                base_title = re.sub(r"\s*(?:\(single\)|[-–—]\s*single)\s*$",
                                    "", song_title.strip(), flags=re.I).strip()
                base_album = re.sub(r"\s*(?:\(single\)|[-–—]\s*single)\s*$",
                                    "", album_title.strip(), flags=re.I).strip()
                if (base_title and tag._normalize_label(base_title) == tag._normalize_label(base_album)):
                    kinds = []
                    for edition in compatible_ids:
                        metadata = _online_album_call(lookup.get_release_metadata, audits, evaluations, edition)
                        kinds.append(str(metadata.get("release_type") or "").strip().casefold())
                    if kinds and all(kind == "single" for kind in kinds):
                        common["album"] = f"{base_title} (Single)"
        identities = {}
        by_path = {Path(row["path"]).resolve(): row for row in audits}
        remote = {(str(track["disc"]), str(track["number"])): track for track in tracklist}
        for position, path in matched.items():
            audit = by_path[path]
            identifier = remote[position]["recording_id"]
            details = audit.get("details")
            if not details or details.get("id") != identifier:
                details = next((row.get("details") for row in audit.get("assessment", {}).get("candidates", [])
                                if row.get("recording_id") == identifier and row.get("status") == "verified"), None)
            if not details and auto_mode:
                details = (evaluation.get("title_matched_candidates") or {}).get(str(path))
            if not details and auto_mode and str(path) in (evaluation.get("album_context_paths") or ()):
                # The album/release and one-to-one song position are supported,
                # not a standalone AcoustID recording identification. Use the
                # catalog's song title and corroborated album artist without
                # claiming an independently verified recording version.
                lead = common.get("album_artist")
                if isinstance(lead, str) and lead.strip():
                    details = {"id": identifier,
                               "title": remote[position].get("recording_title") or remote[position]["title"],
                               "artist-credit-phrase": lead}
            if not details:
                raise review.AlbumReviewRequired(review.issue_details(audits, evaluations, "Mapped identity lacks confirmed recording details."))
            # A canonical recording title must not erase corroborated release versions.
            # Retail formats can assign the SAME song different physical disc
            # positions. Compare the title belonging to this actual source
            # recording in each mapping, not the chosen edition's position.
            release_titles = []
            for _edition_id, other, _signature in compatible:
                other_position = next((index for index, owner in other["matched"].items()
                                       if Path(owner).resolve() == path), None)
                if other_position is None:
                    raise ValueError("Equivalent album edition lost a mapped recording.")
                release_titles.append(next(track["title"] for track in other["tracklist"]
                                           if (int(track["disc"]), int(track["number"])) == other_position))
            if not auto_mode and len({_edition_title_key(title) for title in release_titles if title}) > 1:
                raise review.AlbumReviewRequired(review.issue_details(
                    audits, evaluations, "Compatible release version titles disagree; review the edition."))
            title = details.get("title")
            release_title = next((t["title"] for t in evaluation["tracklist"]
                                  if (str(t["disc"]), str(t["number"])) == position), release_titles[0])
            version = re.match(r"^(.*?)(?:\s*[\(\[]|\s+-\s+)(.+)$", release_title or "")
            if version and lookup.normalize_title(version[1]) == lookup.normalize_title(title or ""):
                title = release_title
            if auto_mode:
                try:
                    title, primary = _feature_aware_identity(details, release_title, title, by_path[path]["existing"])
                except ValueError as error:
                    raise _identity_review_error(
                        audits, evaluations, f'{path.name}: {error}') from error
                # A title-alias mapping may use a different genuine MBID. Keep
                # the audio-supported identity rather than claiming the file has
                # the release's distinct recording identifier.
                identity_details = {"recording_id": details.get("id", identifier), "title": title,
                                    "artist": primary, "verified": True, "user_approved": False}
            else:
                identity_details = {**details, "title": title, "verified": True, "user_approved": False}
            identities[str(path)] = tag.validate_recording_identity(identity_details)
        layout = edits["album"].get("disc_layout", "auto")
        actual_discs = len({int(row["disc"]) for row in tracklist})
        if layout == "single" and actual_discs != 1 or layout == "multiple" and actual_discs < 2:
            raise ValueError("Chosen Disc Layout disagrees with the independently identified release.")
        selected_numbering = _numbering_edit_approvals(matched, tracklist, edits, numbering)
        # Existing track positions may be shuffled or incorrectly tagged.
        # Only an independently mapped, complete musical edition can override
        # those tags without user approval. No filename or local number is
        # accepted as identity evidence for this choice.
        anchor_count = sum(lookup._accepted_recording(row, independent=True) is not None
                           for row in audits)
        cohort_positions = (any(reason in evaluation.get("reasons", ()) for reason in
                                ("resolved_by_whole_album_fingerprints", "resolved_by_audio_verified_title",
                                 "resolved_by_album_context"))
                            and len(matched) == len(paths) == len(tracklist))
        automatic_positions = (auto_mode and not selected_numbering
                               and ((anchor_count == len(paths))
                                    or (anchor_count >= 2 and anchor_count * 2 >= len(paths))
                                    or cohort_positions)
                               and (evaluation.get("status") == "complete" or
                                    set(evaluation.get("reasons", ())) <= number_reasons | {
                                        "resolved_by_independent_anchors", "resolved_by_whole_album_fingerprints",
                                        "resolved_by_audio_verified_title", "resolved_by_album_context", "missing_release_tracks"})
                               and len(matched) == len(paths)
                               and all(item.get("status") == "complete" or
                                       set(item.get("reasons", ())) <= number_reasons | {
                                           "resolved_by_independent_anchors", "resolved_by_whole_album_fingerprints",
                                           "resolved_by_audio_verified_title", "resolved_by_album_context", "missing_release_tracks"}
                                       for _, item, _ in compatible))
        proposals = tag.propose_release_numbering(matched, tracklist,
                         reviewed_decision=selected_numbering or None,
                         allow_partial=automatic_partial,
                         independently_verified=bool(automatic_positions))
        if auto_mode:
            corrected = sum(1 for item in proposals if item['field'] in ('track_number', 'disc_number')
                            and item.get('verified_release_position'))
            terminal.album_stage(f"Verified release order: {len(matched)} tracks; "
                                 f"{corrected} incorrect position tag(s) will be repaired")
        release_type = _corroborated_release_type(album, common, compatible_ids, audits)
        # An explicit date choice is applied after copy verification; it must
        # not be rejected by the release-date proposal before it can be used.
        # Existing exact metadata approvals still win and are never erased.
        proposal_common = dict(common)
        for edited_field in ("album", "album_artist"):
            if edited_field in edits["album"]:
                proposal_common.pop(edited_field, None)
        if manual_fields and "date" in manual_fields and not any(
                item.get("field") == "date" for item in common_decisions):
            proposal_common.pop("date", None)
        if auto_mode:
            approved={d.get('field') for d in common_decisions}
            for field in ('album','album_artist','date'):
                if field not in approved:proposal_common.pop(field,None)
        proposals += tag.propose_common_metadata(matched, proposal_common, reviewed_decision=common_decisions or None,
                                                 release_type=release_type)
        # Shared original album tags can fill gaps after the full verified
        # mapping, without using track names/artist credits as release proof.
        proposals += tag.propose_consistent_album_tags(matched, common, album["tracks"])
        # An entire independently mapped release can support a cautious missing
        # album-artist fill when its original track credits share one lead act.
        if not common.get("album_artist") and not any(track.get("album_artist") for track in album["tracks"]):
            inferred = _suggest_album_artist(album["tracks"])
            if inferred:
                proposals.extend({"path": path, "field": "album_artist", "value": inferred,
                                  "verified": False, "source": "unanimous_primary_track_artist"}
                                 for path in matched.values())
    _edited_identity(identities, edits)
    effective = _effective_text_decisions(text, paths)
    if interpretation is not None:
        # Two independent approvals cannot silently choose different output values.
        for (path, field), item in effective.items():
            metadata = tag.read_track_metadata(path)
            reference = (identities[str(path)].get(field) if field in ("title", "artist")
                         else manual_fields.get(field, common.get(field)))
            if (metadata is None or metadata[field] != (item.get("existing_value"),)
                    or reference is None or str(reference) != item.get("reference_value")
                    or str(reference) != item.get("value")):
                raise ValueError("Saved metadata approval conflicts with this interpretation or its reference; review both choices.")
    for (path, field), item in effective.items():
        if field in ("title", "artist"):
            metadata = tag.read_track_metadata(path)
            if (metadata is None or metadata[field] != (item.get("existing_value"),)
                    or item.get("reference_value") != identities[str(path)][field]):
                raise ValueError("Source or reference identity text changed since review.")
            identities[str(path)].update({field: item["value"], "verified": False, "user_approved": True})
    if interpretation is None:
        for metadata in album["tracks"]:
            path = Path(metadata["path"]).resolve()
            credit = identities[str(path)]["artist"]
            if (metadata.get("artist") and (path, "artist") not in effective
                    and "artist" not in edits["album"] and "artist" not in edits["tracks"].get(str(path), {})
                    and tuple(tag._normalize_label(value) for value in metadata["artist"])
                    != (tag._normalize_label(credit),)):
                if not (auto_mode and len(metadata['artist']) == 1 and
                        (tag.joint_credit_equivalent(metadata['artist'][0], credit) or
                         (_primary_credit(metadata['artist'][0]) == tag._normalize_label(credit)
                          and re.search(r'\b(?:feat\.?|ft\.?|featuring)\b', identities[str(path)]['title'], re.I)) or
                         # A verified full musical release can correct a source
                         # comma-separated guest credit to its matched primary
                         # recording artist. Other performers / joint acts remain
                         # guarded. This never changes the original download.
                         (automatic_positions and not automatic_partial
                          and identities[str(path)].get('verified') is True
                          and not identities[str(path)].get('user_approved')
                          and len(matched) == len(paths)
                          and len(tracklist) == len(paths)))):
                    conflict = tag.MetadataConflictError("artist", path, metadata["artist"], credit)
                    if auto_mode and automatic_positions and not automatic_partial:
                        # A genuine credit conflict must defer the whole import, but
                        # must not discard independently established album positions.
                        # This evidence is source-bound and ONLY supplies numbering
                        # hints to human review; it approves no artist or recording.
                        root = Path(tag.staging_folder).resolve()
                        positions = {Path(owner).resolve().relative_to(root).as_posix():
                                     [int(disc), int(number)]
                                     for (disc, number), owner in matched.items()}
                        totals = _release_totals(tracklist)
                        if (len(positions) == len(paths)
                                and totals["album_total"] == len(paths)):
                            conflict.release_numbering = {
                                "source_fingerprint": fingerprint,
                                "release_id": release_id,
                                "positions": positions,
                                "track_totals": totals["track_totals"],
                                # Album metadata is independently corroborated
                                # before the track-credit conflict; bind it to
                                # this same exact staged-source fingerprint.
                                "album_metadata": {field: common[field] for field in
                                                   ("album_artist", "date")
                                                   if isinstance(common.get(field), str)
                                                   and common[field].strip()},
                            }
                            conflict.release_audit = review.issue_details(
                                audits, evaluations,
                                "Independent album positions verified; artist credit needs review.")
                    raise conflict
    artist = common.get('album_artist') or _suggest_album_artist(album['tracks'])
    genre_hint = None  # Uncertain genre stays unset; catalog guesses add no authority.
    auto_album_fields = (_auto_album_metadata(album,common,evaluation,artist_hint=artist,genre_hint=genre_hint)
                         if auto_mode and interpretation is None else {})
    blocked=set(edits.get('album',()))|set(manual_fields or {})
    blocked.update(item.get('field') for item in common_decisions)
    for field in blocked:auto_album_fields.pop(field,None)
    proposals += tag.propose_required_album_metadata(matched, common, manual_fields=manual_fields,
                                                      contextual_genre=genre_hint,
                                                      allow_unclassified=auto_mode,
                                                      canonical_genre=auto_album_fields.get('genre'))

    terminal.pass_status(3, "Metadata corrections proposed (not written)")
    terminal.show_proposed_changes(album["tracks"], identities, proposals, edits,
                                   tag_service=tag, album=album['album'], artist=artist)
    # Select verified artwork for this album
    print("\nSelecting album artwork...")
    artwork = select_album_art(
        album, compatible_ids, artwork_decision=artwork_decision, matched=matched
    )
    if os.environ.get("MIXIE_VERBOSE", "0") == "1":
        print(f"Artwork selected: {artwork['source']}")
    if prepared.get("partial"):
        print(f"  User-approved interpretation: {len(prepared.get('missing', []))} release positions absent; not independently verified.")
    terminal.final_status(len(matched))
    if os.environ.get("MIXIE_VERBOSE", "0") == "1":
        print(f"Compatible releases: {len(compatible_ids)}")
        print(f"Metadata corrections proposed: {len(proposals)}")

    return {
        "proposals": proposals,
        "auto_album_fields": auto_album_fields,
        "artwork": artwork,
        "matched": matched,
        "compatible_ids": compatible_ids,
        "reviewed_decision": reviewed_decision,
        "artwork_decision": artwork_decision,
        "manual_fields": tag.validate_manual_metadata(manual_fields),
        "tag_edits": edits,
        "identities": identities, "audits": audits, "evaluations": evaluations,
        "release_tracks": tracklist, "release_totals": prepared.get("release_totals", _release_totals(tracklist)),
        "numbering_totals": prepared.get("numbering_totals", {}),
        "numbering_deferred": prepared.get("numbering_deferred", {}),
        "partial": prepared.get("partial", locals().get("automatic_partial", False)),
        "provenance": prepared.get("provenance", "independently_verified_partial" if locals().get("automatic_partial", False) else "independently_verified"),
        "interpretation": deepcopy(interpretation), "album_overrides": overrides,
        "source_fingerprint": fingerprint, "source_manifest": source_manifest,
        # Ephemeral evidence only, for same-run bit-identical duplicate albums.
        "_batch_common": deepcopy(common),
        "_batch_evaluation": deepcopy(locals().get("evaluation")),
        "_batch_genre_hint": locals().get("genre_hint"),
    }


# Compare existing embedded artwork with a verified cover
def artwork_difference(first_data, first_mime, second_data, second_mime):
    def preview(data, expected_mime):
        if not data or len(data) > 20_000_000:
            raise ValueError("Artwork is missing or too large.")

        with Image.open(BytesIO(data)) as image:
            image.load()

            if (
                image.format not in ("JPEG", "PNG")
                or Image.MIME.get(image.format) != expected_mime
            ):
                raise ValueError("Artwork format or MIME mismatch.")

            return ImageOps.fit(
                ImageOps.exif_transpose(image).convert("RGB"),
                (256, 256),
                method=Image.Resampling.LANCZOS
            )

    first = preview(first_data, first_mime)
    second = preview(second_data, second_mime)

    difference = ImageChops.difference(first, second)

    return sum(ImageStat.Stat(difference).mean) / 3

# Test an album's corrections on temporary copies only
@_serialized
def _receipt_protected_destinations(recent_receipts=()):
    """Never retag bytes already bound by a completed or same-batch receipt."""
    result = {}
    def add(receipt):
        if not isinstance(receipt, dict):
            raise ValueError("Malformed completed import receipt.")
        for path, expected in receipt.items():
            if not isinstance(path, str) or not isinstance(expected, str):
                raise ValueError("Invalid protected completion entry.")
            result.setdefault(str(Path(path).absolute()), set()).add(expected)
    for identity, entry in load_completed_reviews().items():
        # Retained history records are immutable audit evidence, not active
        # completion authority. A file may legitimately have multiple historical
        # hashes after receipt replacement. Enforcing every obsolete digest as
        # the current live digest would incorrectly reject another delivery of
        # the same encoded album. The active receipt remains protected below.
        if isinstance(identity, str) and identity.startswith('history:'):
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get('receipt'), dict):
            raise ValueError("Malformed existing completed import record.")
        add(entry['receipt'])
    if _import_first_enabled():
        for entry in _verified_imports().values():
            if not isinstance(entry, dict) or not isinstance(entry.get('receipt'), dict):
                raise ValueError('Malformed import-only receipt record.')
            # Even a now-stale import-only claim protects its library path from
            # silent replacement. The merge layer can reuse unchanged encoded
            # audio, but may not overwrite a file with changed history.
            add(entry['receipt'])
        # A retired claim is no longer valid for skipping scans or deletion,
        # but its historical library destination still needs overwrite safety.
        # Do not discard that protection merely to quiet active review work.
        for entry in _retired_import_receipts().values():
            if not isinstance(entry, dict) or not isinstance(entry.get('receipt'), dict):
                raise ValueError('Malformed retired import receipt record.')
            add(entry['receipt'])
    for receipt in recent_receipts:
        add(receipt)
    return result


def _import_first_enabled():
    """Opt-in state: an import receipt is independent of optional ZIP backups.

    The installer enables this mode on the user's working copy. Unmarked legacy
    installations and isolated historical tests keep their original behavior.
    """
    path = _state_path('.mixie-import-first.json')
    if not path.exists():
        return False
    with path.open('r', encoding='utf-8') as stream:
        state = json.load(stream)
    if state != {'version': 1, 'mode': 'verified_library_first'}:
        raise ValueError('Invalid MIXIE import-first configuration.')
    return True


def _verified_imports():
    """Load import-only receipts; never turn them into cleanup authorization."""
    path = _state_path('.verified_imports.json')
    if not path.exists():
        return {}
    with path.open('r', encoding='utf-8') as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('entries'), dict):
        raise ValueError('Invalid import-only receipt ledger.')
    return state['entries']




def _retired_import_receipts():
    path = _state_path('.retired_import_receipts.json')
    if not path.exists():
        return {}
    with path.open('r', encoding='utf-8') as stream:
        state = json.load(stream)
    if (not isinstance(state, dict) or state.get('version') != 1
            or not isinstance(state.get('entries'), dict)):
        raise ValueError('Invalid retired import receipt history.')
    return state['entries']


def _refresh_staged_import_receipts(albums):
    """Reconcile import-only *claims* against albums actually staged now.

    A prior receipt is neither an active review request nor permanent permission
    to skip a new scan. Rebase a receipt only after independently checking EVERY
    current source track and EVERY current library output against its original
    pinned hashes. Otherwise retire the now-inapplicable claim to immutable
    history before removing it from the active ledger. Neither outcome grants
    source cleanup, and finalized archive/cleanup journals are never edited.
    """
    if not _import_first_enabled():
        return (0, 0)
    records = _verified_imports()
    if not records:
        return (0, 0)
    staged = {}
    for album in albums.values():
        issue = _album_issue(album)
        key = (issue['import_name'], tuple(sorted(issue['tracks'])))
        if key in staged:
            raise RuntimeError('Duplicate staged album identity; cannot refresh receipts.')
        staged[key] = issue
    refreshed, retired = {}, {}
    for old_key, entry in list(records.items()):
        if (not isinstance(entry, dict) or not isinstance(entry.get('source_manifest'), dict)
                or not isinstance(entry.get('tracks'), list)
                or not isinstance(entry.get('input_map'), dict)
                or not isinstance(entry.get('asset_outputs'), dict)
                or not isinstance(entry.get('receipt'), dict)):
            raise ValueError('Malformed verified import receipt; refuse refresh.')
        issue = staged.get((entry['source_manifest'].get('import_name'),
                            tuple(sorted(entry['tracks']))))
        if issue is None:
            continue  # Receipts for unrelated deliveries are untouched.
        stage_root = Path(tag.staging_folder)
        current_stage = stage_root / issue['import_name']
        if stage_root.is_symlink() or current_stage.is_symlink():
            raise RuntimeError('Unsafe staged source; import receipt refresh refused.')
        if not current_stage.is_dir():
            continue  # Receipts for physically retired deliveries are untouched.
        if is_library_import_verified(issue, {old_key: entry}):
            continue
        candidate = None
        try:
            current = completion_source_manifest(issue)
            # Source ownership, all audio hashes, output hashes and asset hashes
            # must validate as a complete set, not just a common album name.
            trial = deepcopy(entry)
            trial['source_manifest'] = current
            _validate_library_only_entry(trial, issue, current)
            key = completion_fingerprint(issue['import_name'], _source_rows(issue, current), current)
            if key not in records and key not in refreshed:
                candidate = (key, trial)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass
        retired[old_key] = deepcopy(entry)
        if candidate is not None:
            refreshed[candidate[0]] = candidate[1]
    if not retired:
        return (0, 0)
    history_path = _state_path('.retired_import_receipts.json')
    history = {'version': 1, 'entries': _retired_import_receipts()}
    for key, row in retired.items():
        if key in history['entries'] and history['entries'][key] != row:
            raise RuntimeError('Historical receipt collision; refuse refresh.')
        history['entries'][key] = row
    # Failure here leaves current receipts active; the historical proof is
    # durable BEFORE a claim can be deactivated or replaced.
    _write_state(history_path, history)
    for key in retired:
        del records[key]
    records.update(refreshed)
    _write_state(_state_path('.verified_imports.json'),
                 {'version': 1, 'entries': records})
    if refreshed:
        print(f'  Reverified {len(refreshed)} import receipt(s) against current staged albums and library files.')
    dropped = len(retired) - len(refreshed)
    if dropped:
        print(f'  Retired {dropped} obsolete import receipt(s) to history; affected staged albums will be rescanned.')
    return (len(refreshed), dropped)

def _validate_library_only_entry(entry, issue, manifest):
    """Validate exact source/payload identity and every current output checksum.

    No original/archive/cleanup proof is implied by a successful result.
    """
    _validate_input_map(entry, issue, manifest)
    receipt = entry['receipt']
    raw_library = Path(sort.music_library_folder)
    if raw_library.is_symlink():
        raise RuntimeError('Music Library root is a symbolic link.')
    library = raw_library.resolve()
    if not library.is_dir():
        raise RuntimeError('Music Library root is not safe.')
    for destination, expected in receipt.items():
        if not isinstance(destination, str) or not isinstance(expected, str):
            raise RuntimeError('Invalid import-only file checksum.')
        path = Path(destination)
        # Reject links at any destination component, even if a parent symlink
        # happens to resolve back inside the Music Library tree.
        current_parent = path.parent
        unsafe_parent = False
        while current_parent != library and current_parent != current_parent.parent:
            if current_parent.is_symlink():
                unsafe_parent = True
                break
            current_parent = current_parent.parent
        if (unsafe_parent or path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(library)
                or sort.file_checksum(path).hex() != expected):
            raise RuntimeError(f'Verified library destination changed: {destination}')
    return True


def is_staged_album_already_imported(issue, entries=None):
    """Read-only album-level skip, independent of unrelated shared-folder files.

    A new sibling can change the full source manifest even when an album's
    own audio and imported library bytes have not changed. This proof ONLY
    avoids unnecessary provider calls; it never authorizes archive cleanup.
    Destructive finalization continues to require complete provenance.
    """
    try:
        name, tracks = issue['import_name'], sorted(issue['tracks'])
        if not sort._safe_name(name) or not tracks:
            return False
        stage = sort._safe_directory(Path(sort.staging_folder) / name)
        rows = []
        # Include old import receipts and durable finalized receipts, which may
        # outlive the separate import-only ledger on migrated installations.
        evidence = list((entries if entries is not None else _verified_imports()).values())
        import mixie_finalizer
        evidence.extend(row.get('import_receipt') for row in
                        mixie_finalizer._state()['finalized'].values())
        candidates = [entry for entry in evidence if isinstance(entry, dict)
                      and entry.get('tracks') == tracks
                      and (entry.get('source_manifest') or {}).get('import_name') == name]
        for entry in candidates:
            manifest = entry['source_manifest']
            try:
                source_rows = _source_rows(issue, manifest)
                if set(entry.get('input_map') or {}) != set(tracks):
                    continue
                for relative, expected in source_rows:
                    path = stage / Path(relative).relative_to(name)
                    sort._safe_directory(path.parent)
                    if (path.is_symlink() or not path.is_file()
                            or sort.file_checksum(path).hex() != expected):
                        raise RuntimeError('Album source audio changed.')
                _validate_library_only_entry(entry, issue, manifest)
                return True
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                continue
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        pass
    return False


def is_library_import_verified(issue, entries=None):
    """Skip only imports proven by immutable source and live library checks."""
    try:
        manifest = completion_source_manifest(issue)
        fingerprint = completion_fingerprint(issue['import_name'], _source_rows(issue, manifest), manifest)
        recorded = (entries if entries is not None else _verified_imports()).get(fingerprint)
        if not isinstance(recorded, dict):
            return False
        return _validate_library_only_entry(recorded, issue, manifest)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False


@_serialized
def record_library_only_import(issue, receipt):
    """Persist a successful import immediately, before required album archiving.

    Protected source backup already exists before test_album_plan commits audio;
    completion here does NOT entitle staging/Inbox cleanup.
    """
    if not isinstance(receipt, CommitReceipt) or not receipt:
        raise ValueError('Verified import requires a complete commit receipt.')
    manifest = completion_source_manifest(issue)
    if receipt.source_manifest != manifest:
        raise RuntimeError('Original staged source changed during import.')
    entry = {'album': issue['album'], 'tracks': sorted(issue['tracks']),
             'evidence_version': 2, 'source_manifest': deepcopy(receipt.source_manifest),
             'input_map': deepcopy(receipt.input_map), 'asset_outputs': deepcopy(receipt.asset_outputs),
             'receipt': dict(receipt), 'evidence': review._json_safe(receipt.evidence),
             'status': 'library_verified_archive_optional'}
    _validate_library_only_entry(entry, issue, manifest)
    if completion_source_manifest(issue) != manifest:
        raise RuntimeError('Source changed before import-only receipt publication.')
    identity = completion_fingerprint(issue['import_name'], _source_rows(issue, manifest), manifest)
    entries = _verified_imports()
    previous = entries.get(identity)
    if previous is not None and previous != entry:
        # Do not silently replace a pinned receipt: changed library bytes may
        # represent a user edit, another import, or corruption.
        raise RuntimeError('Existing verified import receipt differs; review required.')
    entries[identity] = entry
    _write_state(_state_path('.verified_imports.json'), {'version': 1, 'entries': entries})
    return identity


def _archive_pending(issue):
    return (issue.get('status') == 'archive_pending'
            or ((issue.get('details') or {}).get('kind') == 'completion'
                and str(issue.get('reason', '')).startswith('Library import verified; archive pending:')))


def _mark_recovery_required(issue):
    issue.update(status='recovery_required', details={'kind': 'recovery_required'},
                 reason='Import receipt cannot verify current source/library files; '
                        'recover missing or changed evidence before retrying.')


def _mark_archive_pending(issue, reason='required album archives not yet verified'):
    if not _archive_pending(issue):
        snapshot = {field: deepcopy(issue[field]) for field in
                    ('details', 'reason', 'error_type') if field in issue}
        if snapshot:
            issue.setdefault('review_evidence_history', []).append(snapshot)
    issue.update(status='archive_pending', details={'kind': 'completion'},
                 reason='Library import verified; archive pending: ' + reason)


def test_album_plan(plan, album, commit=False, auto=False, *, same_batch_receipts=(),
                    preserve_staged_backup=True):
    from shutil import copy2
    from tempfile import TemporaryDirectory
    from mutagen import File
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC

    if commit:
        finish_prior_cleanup_before_ingest()
    if plan.get("numbering_deferred"):
        raise ValueError("Reviewed numbering totals remain deferred; import is blocked.")
    originals = sorted({Path(path).resolve() for path in plan["matched"].values()})
    inventory = {Path(track["path"]).resolve() for track in album["tracks"]}
    if not originals or set(originals) != inventory or len(originals) != len(plan["matched"]):
        raise ValueError("Every source must be mapped exactly once; subset imports are forbidden.")
    source_issue = _album_issue(album)
    source_manifest = completion_source_manifest(source_issue)
    current_fingerprint = completion_fingerprint(source_issue["import_name"], _source_rows(source_issue, source_manifest), source_manifest)
    if plan.get("source_fingerprint", current_fingerprint) != current_fingerprint:
        raise ValueError("Source changed since preview or approval.")
    interpretation = plan.get("interpretation")
    if interpretation is not None:
        if (not isinstance(interpretation, dict) or interpretation.get("status") != "pending_application"
                or interpretation.get("user_approved") is not True
                or interpretation.get("independently_verified") is not False
                or interpretation.get("source_fingerprint") != current_fingerprint
                or plan.get("provenance") != "user_approved"):
            raise ValueError("Interpretation approval is stale or falsely labelled as independent verification.")
        if interpretation.get("release_id") is None and not plan.get("partial"):
            raise ValueError("An offline interpretation cannot claim complete commercial release coverage.")
        if any(identity.get("verified") is not False or identity.get("user_approved") is not True
               for identity in plan.get("identities", {}).values()):
            raise ValueError("Approved interpretation identities must remain user-approved, not verified.")
    for decision in _decision_list(plan.get("reviewed_decision")):
        if decision.get("source_fingerprint", current_fingerprint) != current_fingerprint:
            raise ValueError("Saved metadata/numbering source approval changed.")
    if plan.get("partial") and plan.get("provenance") != "independently_verified_partial" and not (
            isinstance(plan.get("interpretation"), dict)
            and plan["interpretation"].get("status") == "pending_application"
            and plan["interpretation"].get("user_approved") is True):
        raise ValueError("Partial release imports require verified evidence or explicit interpretation.")
    if plan.get("provenance") == "independently_verified_partial":
        if (plan.get("interpretation") is not None or not plan.get("partial")
                or len(plan.get("matched", {})) != len(originals)
                or not plan.get("compatible_ids")
                or not plan.get("release_tracks")
                or len(plan["release_tracks"]) <= len(originals)):
            raise ValueError("Invalid independent partial album evidence.")
    identities = plan.get("identities", {})
    for change in plan.get("proposals", ()):
        if not isinstance(change, dict) or change.get("verified_release_position") is not True:
            continue
        if (not auto or plan.get("provenance") not in
                ("independently_verified", "independently_verified_partial")
                or interpretation is not None or not plan.get("compatible_ids")
                or not plan.get("release_tracks")
                or len(plan.get("matched", {})) != len(originals)):
            raise ValueError("Automatic numbering replacement lacks independently verified album evidence.")
        owner = Path(change.get("path", "")).resolve()
        position = next((pos for pos, source in plan["matched"].items()
                         if Path(source).resolve() == owner), None)
        expected = (str(position[0]) if change.get("field") == "disc_number" else
                    str(position[1]) if change.get("field") == "track_number" else None) if position else None
        if owner not in originals or not isinstance(identities.get(str(owner)), dict) or identities[str(owner)].get("verified") is not True:
            raise ValueError("Automatic numbering replacement has no verified recording identity.")
        if expected is not None and str(change.get("value")) != expected:
            raise ValueError("Automatic numbering replacement contradicts verified release position.")
    if source_manifest["provenance"] is not None:
        required = {"identities", "source_fingerprint", "source_manifest", "audits",
                    "evaluations", "release_tracks", "compatible_ids"}
        if not required <= plan.keys() or plan["source_manifest"] != source_manifest:
            raise ValueError("Managed commits require the complete, snapshot-bound identification plan.")
        if (set(identities) != {str(path) for path in originals}
                or {Path(row["path"]).resolve() for row in plan["audits"]} != inventory
                or not plan["release_tracks"]):
            raise ValueError("Managed commit identity evidence is incomplete.")
        if interpretation is None and (not plan["evaluations"] or not plan["compatible_ids"]
                or plan.get("provenance") not in ("independently_verified", "independently_verified_partial")):
            raise ValueError("Managed automatic commits require corroborated release evidence.")
        for identity in identities.values():
            tag.validate_recording_identity(identity)
    if identities and set(identities) != {str(path) for path in originals}:
        raise ValueError("Identity evidence does not cover every source exactly once.")
    staging_root = tag.staging_folder.resolve()
    for source in originals:
        if not source.is_relative_to(staging_root):
            raise PermissionError("Source is outside staging.")
    # Old callers retain their legacy loose backup. For the explicit pending-
    # only import-first workflow the complete unmodified staged delivery stays
    # retained, and ZIP archival is a separate mandatory finalization gate.
    # This also keeps a verification-only retry free of redundant backup copies.
    if not preserve_staged_backup:
        if (not auto or not _import_first_enabled() or
                plan.get("provenance") not in
                ("independently_verified", "independently_verified_partial") or
                source_manifest.get("provenance") is None):
            raise ValueError("Skipping a loose backup requires managed independent import-first evidence.")
        print("Staged originals retained; no redundant loose backup created.")
    else:
        backup_path = archive.backup_staged_import(album)
        print(f"Protected backup: {backup_path}")
    original_hashes = {path: sort.file_checksum(path) for path in originals}
    import_root = staging_root / source_issue["import_name"]
    if any(digest.hex() != source_manifest["payload_hashes"].get(path.relative_to(import_root).as_posix())
           for path, digest in original_hashes.items()):
        raise RuntimeError("Source changed during backup; approved bytes cannot be copied.")


    receipt = None
    with TemporaryDirectory(
        prefix=".mixie-work-",
        dir=tag.script_folder.parent
    ) as working_folder:
        copies = {}
        for index, source in enumerate(originals):
            target = Path(working_folder) / f"{index:03d}-{source.name}"
            copy2(source, target)
            if target.is_symlink() or sort.file_checksum(target) != original_hashes[source]:
                raise RuntimeError("Initial working copy differs from the approved source bytes.")
            copies[source] = target

        # Display explicit phases; source files remain unchanged.
        terminal.pass_status(4, "Apply and verify working copies")
        # Redirect every metadata correction to its working copy
        corrections = [
            {
                **proposal,
                "path": copies[Path(proposal["path"]).resolve()]
            }
            for proposal in plan["proposals"]
        ]
        writes = tag.apply_corrections(
            corrections, approved=True
        )

        # Automatic album metadata is authorized only by a complete,
        # independently corroborated source-to-release mapping.
        fields=plan.get('auto_album_fields') or {}
        if fields:
            if (plan.get('provenance') not in ('independently_verified','independently_verified_partial')
                    or plan.get('interpretation') is not None or not plan.get('compatible_ids')
                    or set(fields)-{'album','album_artist','date','genre'}):
                raise ValueError('Album metadata has no independent verification authority.')
            for target in copies.values():
                for field,value in fields.items():
                    writes += tag.replace_reviewed_text_on_copy(target,working_folder,field,value)
                    saved=tag.read_track_metadata(target)
                    if saved is None or saved[field]!=(value,):
                        raise RuntimeError('Working-copy album metadata verification failed.')

        # Recording titles must actually reach the independent copies, not just logs.
        for source, target in copies.items():
            if str(source) in identities:
                try:
                    # Only a complete independently verified release mapping
                    # can replace a conflicting source Artist automatically.
                    # The copy is disposable until every output check passes.
                    verified_full_release = (
                        auto and plan.get('provenance') == 'independently_verified'
                        and not plan.get('partial') and plan.get('interpretation') is None
                        and plan.get('compatible_ids')
                        and len(plan.get('matched', {})) == len(originals)
                        and len(plan.get('release_tracks', [])) == len(originals)
                        and identities[str(source)].get('verified') is True
                        and not identities[str(source)].get('user_approved'))
                    writes += tag.apply_verified_identity_on_copy(
                        target, working_folder, identities[str(source)],
                        authoritative_release=bool(verified_full_release))
                except tag.MetadataConflictError as error:
                    raise tag.MetadataConflictError(error.field, source, error.existing, error.expected) from error
                saved = tag.read_track_metadata(target)
                identity = tag.validate_recording_identity(identities[str(source)])
                if saved is None or any(len(saved[field]) != 1 or
                        tag._normalize_label(saved[field][0]) != tag._normalize_label(identity[field])
                        for field in ("title", "artist")):
                    raise RuntimeError("Working-copy recording identity verification failed.")
        effective = _effective_text_decisions(plan.get("reviewed_decision"), originals)
        for (source, field), decision in effective.items():
            target, value = copies[source], decision["value"]
            writes += tag.replace_reviewed_text_on_copy(target, working_folder, field, value)
            saved = tag.read_track_metadata(target)
            if saved is None or saved[field] != (value,):
                raise RuntimeError(f"Reviewed metadata mismatch: {target.name}")
        for field, value in plan.get("album_overrides", {}).items():
            if field not in ("album", "album_artist") or plan.get("provenance") != "user_approved":
                raise ValueError("Album overrides require explicit interpretation approval.")
            for target in copies.values():
                writes += tag.replace_reviewed_text_on_copy(target, working_folder, field, value)
                saved = tag.read_track_metadata(target)
                if saved is None or saved[field] != (value,):
                    raise RuntimeError("Reviewed album override verification failed.")


        # Explicit tag choices are applied last on independent working copies.
        # They cannot create or verify a recording/release mapping on their own.
        edited = _validated_tag_edits(plan.get("tag_edits"), originals)
        for source, target in copies.items():
            fields = dict(edited["album"])
            fields.update(edited["tracks"].get(str(source), {}))
            for field in ("album", "album_artist", "artist", "title"):
                if field not in fields:
                    continue
                selected = fields[field]
                writes += tag.replace_reviewed_text_on_copy(target, working_folder, field, selected)
                saved = tag.read_track_metadata(target)
                if saved is None or saved[field] != (selected,):
                    raise RuntimeError(f"Human tag edit was not verified: {target.name} / {field}")

        # Apply explicitly reviewed date/genre choices ONLY to working
        # copies. They may legitimately replace conflicting source tags.
        for field, value in tag.validate_manual_metadata(plan.get("manual_fields")).items():
            for source, target in copies.items():
                if any(item["field"] == field and (item.get("path") is None or Path(item["path"]).resolve() == source)
                       for item in _decision_list(plan.get("reviewed_decision"))):
                    continue  # A path-scoped saved choice is not lost to an album-wide fallback.
                writes += tag.replace_reviewed_text_on_copy(target, working_folder, field, value)
                saved = tag.read_track_metadata(target)
                if saved is None or saved[field] != (value,):
                    raise RuntimeError("Manual metadata verification failed.")

        # Embed or replace artwork on working copies only
        approved_artwork = plan.get("artwork_decision")

        for target in copies.values():
            # Always replace stale front art on the temporary working copy;
            # leave every other image and the original audio untouched.
            tag.replace_reviewed_front_art_on_copy(target, working_folder, plan['artwork'])

            audio_file = File(target)
            if isinstance(audio_file, MP3):
                pictures = audio_file.tags.getall("APIC")
            elif isinstance(audio_file, FLAC):
                pictures = audio_file.pictures
            else:
                raise ValueError(f"Unsupported format: {target.name}")

            fronts = [
                picture for picture in pictures
                if picture.type == 3
            ]

            verified = bool(fronts) and all(
                artwork_difference(
                    picture.data,
                    picture.mime,
                    plan["artwork"]["data"],
                    plan["artwork"]["mime"]
                ) <= 6.0
                for picture in fronts
            )

            if not verified:
                raise RuntimeError(
                    f"Artwork verification failed: {target.name}"
                )


        # Disc count is determined by the selected full release or explicit
        # user-approved local interpretation, never by counting source folders.
        full_disc_count = (plan.get("release_totals") or {}).get("disc_total")
        if full_disc_count is None and plan.get("numbering_totals"):
            counts = {data.get("disc_total") for data in plan["numbering_totals"].values()}
            full_disc_count = next(iter(counts)) if len(counts) == 1 else None
        for target in copies.values():
            writes += tag.remove_uncertain_genre_on_copy(target, working_folder)
            if full_disc_count == 1:
                writes += tag.remove_single_disc_tags_on_copy(target, working_folder)
        plan['_verified_changes'] = terminal.show_verified_changes(
            album['tracks'], copies, tag_service=tag)

        # Prepare library sorting from verified working copies
        print("\nPlanning library destinations...")
        positions = {
            Path(path).resolve(): (str(disc), str(number))
            for (disc, number), path in plan["matched"].items()
        }
        full_totals = plan.get("release_totals")
        if full_totals is None and plan.get("release_tracks") and plan.get("provenance") != "user_approved":
            full_totals = _release_totals(plan["release_tracks"])
        if full_totals is None and not plan.get("partial") and interpretation is None:
            full_totals = _release_totals([{"disc": disc, "number": number} for disc, number in plan["matched"]])
        approved_totals = plan.get("numbering_totals", {})
        verified_tracks = []
        for source, target in copies.items():
            metadata = tag.read_track_metadata(target)
            if metadata is None:
                raise ValueError(f"Cannot read tags: {target.name}")
            def required(field, metadata=metadata, target=target):
                values = metadata[field]
                if len(values) != 1 or not values[0].strip():
                    raise ValueError(
                        f"Missing or ambiguous {field}: {target.name}"
                    )
                return values[0].strip()
            disc, number = positions[source]
            actual_number, _ = tag.parse_track_number(
                metadata["track_number"]
            )
            if actual_number is None or int(actual_number) != int(number):
                raise ValueError(
                    f"Track number verification failed: {target.name}"
                )
            existing_disc, _ = tag.parse_disc_number(
                metadata["disc_number"]
            )
            if disc_count_for_track := (full_totals or {}).get("disc_total", approved_totals.get(str(source), {}).get("disc_total")):
                if disc_count_for_track == 1:
                    if metadata["disc_number"] or metadata["disc_total"] or tag.get_disc_totals(metadata):
                        raise ValueError(f"Single-disc tag cleanup failed: {target.name}")
                elif existing_disc != int(disc):
                    raise ValueError(f"Disc number verification failed: {target.name}")
            else:
                raise ValueError("Full-release or explicitly reviewed disc total is required.")
            explicit = approved_totals.get(str(source), {})
            disc_count = (full_totals or {}).get("disc_total", explicit.get("disc_total"))
            if not isinstance(disc_count, int) or disc_count < int(disc):
                raise ValueError("Full-release or explicit reviewed disc total is unavailable.")
            if disc_count > 1 and tag.get_disc_totals(metadata) != {disc_count}:
                raise ValueError(f"Disc total verification failed: {target.name}")
            if not (plan.get("provenance") == "independently_verified_partial" and not metadata["date"]):
                required("date")
            if any(tag.is_uncertain_genre(value) for value in metadata['genre']):
                raise ValueError(f'Uncertain genre placeholder survived output cleanup: {target.name}')
            disc_size = (full_totals or {}).get("track_totals", {}).get(str(disc), explicit.get("track_total"))
            if not isinstance(disc_size, int) or disc_size < int(number):
                raise ValueError("Full-release or explicit reviewed track total is unavailable.")
            totals = tag.get_track_totals(metadata)
            allowed_totals = ({disc_size}, {(full_totals or {}).get("album_total", disc_size)})
            if totals not in allowed_totals:
                raise ValueError(f"Track total verification failed: {target.name}")
            verified_tracks.append({
                "path": target,
                "album_artist": required("album_artist"),
                "album": required("album"),
                "title": required("title"),
                "track_number": required("track_number"),
                "disc_number": (
                    f"{disc}/{disc_count}" if disc_count > 1 else None
                )
            })

        # Preserve the validated artwork as a separate album cover
        artwork = plan["artwork"]

        if artwork["mime"] == "image/jpeg":
            cover_name = "cover.jpg"
        elif artwork["mime"] == "image/png":
            cover_name = "cover.png"
        else:
            raise ValueError("Unsupported album artwork format.")

        cover_path = Path(working_folder) / cover_name
        cover_path.write_bytes(artwork["data"])

        # Verify the temporary cover matches the selected image
        if cover_path.read_bytes() != artwork["data"]:
            raise RuntimeError("Artwork copy verification failed.")

        verified_assets = [{
            "path": cover_path,
            "relative_path": cover_name,
            "album_artist": verified_tracks[0]["album_artist"],
            "album": verified_tracks[0]["album"]
        }]

        # Preserve supplied booklet scans and other album artwork
        album_source = album_asset_root(album, matched=plan["matched"])
        if album_source is None:
            print("Shared import extras require album assignment.")
            supplied_assets = []
        else:
            supplied_assets = sort.scan_import_assets(album_source)

        for asset in supplied_assets:
            source_asset = Path(asset["path"])
            relative = Path(asset["relative_path"])

            library_relative = relative

            if (
                len(relative.parts) == 1
                and relative.name.casefold() == cover_name.casefold()
            ):
                library_relative = Path("Original Artwork") / relative

            # Other file types remain protected by the original backup
            if source_asset.suffix.lower() not in (
                ".jpg", ".jpeg", ".png", ".pdf"
            ):
                continue

            target_asset = (
                Path(working_folder) / ".mixie-assets" / relative
            )
            target_asset.parent.mkdir(parents=True, exist_ok=True)
            copy2(source_asset, target_asset)

            if archive.file_hash(source_asset) != archive.file_hash(
                target_asset
            ):
                raise RuntimeError(
                    f"Extra asset verification failed: {relative}"
                )

            verified_assets.append({
                "path": target_asset,
                "relative_path": library_relative.as_posix(),
                "album_artist": verified_tracks[0]["album_artist"],
                "album": verified_tracks[0]["album"]
            })

        artwork_plan = sort.plan_album_assets(
            verified_assets, working_folder
        )

        # The planner may safely collapse identical artwork files
        # that resolve to the same library destination.
        if not artwork_plan or len(artwork_plan) > len(verified_assets):
            raise RuntimeError("Album artwork planning failed.")

        if plan.get("provenance") == "independently_verified_partial":
            total = (full_totals or {}).get("album_total")
            if not isinstance(total, int) or len(verified_tracks) >= total:
                raise ValueError("Partial album completion evidence is invalid.")
            label = f"{verified_tracks[0]['album']} (Incomplete, {len(verified_tracks)} of {total})"
            for row in verified_tracks:
                row["folder_album"] = label
            for asset in verified_assets:
                asset["folder_album"] = label
            # Never split an already published library album into a second,
            # differently numbered partial folder. Growth requires a separate
            # receipt-aware transaction; defer rather than breaking old proof.
            parent = Path(sort.music_library_folder) / sort.safe_path_part(verified_tracks[0]["album_artist"])
            if parent.is_dir() and any(p.is_dir() and (p.name == sort.safe_path_part(verified_tracks[0]["album"])
                    or re.fullmatch(re.escape(sort.safe_path_part(verified_tracks[0]["album"])) +
                                    r" \(Incomplete, [0-9]+ of [0-9]+\)", p.name))
                    for p in parent.iterdir()):
                raise ValueError("Existing album needs a receipt-safe partial merge; leave it for review.")
            artwork_plan = sort.plan_album_assets(verified_assets, working_folder)
        sorting_plan = sort.plan_library_sort(
            verified_tracks,
            approved=True,
            source_root=working_folder,
            check_existing=False
        )

        if len(sorting_plan) != len(verified_tracks):
            raise RuntimeError("Library sorting plan is incomplete.")
        print(f"Library destinations verified: {len(sorting_plan)}")
        print(
            f"Album artwork destination: "
            f"{artwork_plan[0]['destination']}"
        )

        # Confirm original staged files were not modified
        for source, expected_hash in original_hashes.items():
            current_hash = sort.file_checksum(source)
            if current_hash != expected_hash:
                raise RuntimeError(
                    f"Original file changed: {source.name}"
                )

        if completion_source_manifest(source_issue) != source_manifest:
            raise RuntimeError("Source payload, artwork, or provenance changed before publication.")
        # Optionally copy verified results into Music Library

        if commit:
            if auto:
                confirmed = True
            else:
                confirmation = input(
                    f"\nType IMPORT {album['album']} "
                    "to commit this album: "
                ).strip()
                confirmed = confirmation == f"IMPORT {album['album']}"

            if not confirmed:
                print("Library import cancelled.")
            else:
                merge_result = None
                try:
                    if (auto and plan.get('provenance') in
                            ('independently_verified', 'independently_verified_partial')
                            and plan.get('interpretation') is None):
                        import mixie_library_merge
                        protected = _receipt_protected_destinations(same_batch_receipts)
                        merge_options = {'protected': protected}
                        if _import_first_enabled():
                            merge_options['allow_stale_receipt_reuse'] = True
                        merge_result = mixie_library_merge.commit_verified_audio_plan(
                            sorting_plan + artwork_plan,
                            (item['destination'] for item in sorting_plan),
                            **merge_options)
                        created, already_present = merge_result.created, merge_result.already
                    else:
                        created, already_present = sort.commit_verified_plan(
                            sorting_plan + artwork_plan
                        )

                    # Verify all committed tracks and artwork
                    for item in sorting_plan + artwork_plan:
                        destination = Path(item["destination"])

                        if not destination.is_file():
                            raise RuntimeError(
                                f"Missing library file: {destination}"
                            )

                        if sort.file_checksum(item["source"]) != \
                                sort.file_checksum(destination):
                            if (merge_result is None or
                                    (str(destination) not in merge_result.reused_assets and
                                     (str(destination) not in merge_result.reused_stream or
                                      not mixie_library_merge.identical_encoded_audio(item["source"], destination)))):
                                raise RuntimeError(
                                    f"Library verification failed: {destination}"
                                )

                    print(
                        f"\nImported and verified "
                        f"{len(created)} new files, "
                        f"{len(already_present)} already verified."
                    )
                    print("Originals and backup preserved.")

                    # Pin explicit source -> output evidence; a file count is not proof.
                    files = {str(Path(item["destination"]).resolve()): sort.file_checksum(item["destination"]).hex()
                             for item in sorting_plan + artwork_plan}
                    by_copy = {target.resolve(): source for source, target in copies.items()}
                    input_map = {}
                    for item in sorting_plan:
                        source = by_copy[Path(item["source"]).resolve()]
                        relative = source.relative_to(staging_root).as_posix()
                        destination = str(Path(item["destination"]).resolve())
                        input_map[relative] = {"source_sha256": original_hashes[source].hex(),
                                               "destination": destination, "destination_sha256": files[destination],
                                               "identity": identities.get(str(source))}
                    if completion_source_manifest(source_issue) != source_manifest:
                        raise RuntimeError("Source payload or provenance changed during import; completion refused.")
                    evidence = {"provenance": plan.get("provenance", "legacy_plan"),
                                "interpretation": plan.get("interpretation"),
                                "decisions": plan.get("reviewed_decision"),
                                "source_fingerprint": current_fingerprint,
                                "partial": plan.get("partial", False), "release_totals": full_totals,
                                "numbering_totals": approved_totals}
                    if plan.get("audits"):
                        evidence["identity_release"] = review.issue_details(plan["audits"], plan.get("evaluations", []), "Committed interpretation evidence.")
                    receipt = CommitReceipt(files, input_map=input_map, source_manifest=source_manifest,
                        asset_outputs={str(Path(item["destination"]).resolve()): files[str(Path(item["destination"]).resolve())]
                                       for item in artwork_plan}, evidence=review._json_safe(evidence))
                except BaseException:
                    if merge_result is not None:
                        merge_result.rollback()
                    raise

        print(f"\nWorking copies verified: {len(copies)}")
        print(f"Metadata frame writes: {writes}")
        print("Artwork verified on every working copy.")
        print("Original staged files unchanged.")
    print("Temporary copies cleaned up.")
    return receipt

# Identify the exact staged files belonging to a review
class CommitReceipt(dict):
    """Legacy destination/hash dict plus explicit, snapshot-bound input evidence."""
    def __init__(self, files=(), *, input_map, source_manifest, asset_outputs=None, evidence=None):
        super().__init__(files)
        self.input_map = deepcopy(input_map)
        self.source_manifest = deepcopy(source_manifest)
        self.asset_outputs = deepcopy(asset_outputs or {})
        self.evidence = deepcopy(evidence or {})


def _safe_component(value):
    return (isinstance(value, str) and value not in ("", ".", "..")
            and Path(value).name == value and "/" not in value and "\\" not in value)


def _source_rows(issue, manifest):
    name = issue["import_name"]
    paths = issue["tracks"]
    if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths):
        raise ValueError("Invalid or duplicate source inventory.")
    rows = []
    for relative in sorted(paths):
        if (not isinstance(relative, str) or "\\" in relative
                or any(part in ("", ".", "..") for part in relative.split("/"))
                or Path(relative).is_absolute() or Path(relative).parts[0] != name):
            raise ValueError("Invalid staged source path.")
        leaf = Path(relative).relative_to(name).as_posix()
        digest = manifest["payload_hashes"].get(leaf)
        if digest is None or Path(leaf).suffix.lower() not in constants.music_file_types:
            raise ValueError("Reviewed source files are unavailable.")
        rows.append([relative, digest])
    return rows


def completion_source_manifest(issue):
    """Full import payload + committed delivery occurrence, not only album audio."""
    name = issue.get("import_name")
    if not _safe_component(name):
        raise ValueError("Invalid import identity.")
    root = Path(tag.staging_folder).absolute()
    stage = root / name
    if root.is_symlink() or stage.is_symlink() or not stage.is_dir():
        raise ValueError("Reviewed source payload is unavailable or unsafe.")
    # Exactly one SHA-256 inventory: validate the provenance record without
    # rehashing, then compare the freshly hashed payload with its pins below.
    # Never trust metadata alone or cache hashes between operations.
    provenance = sort.staging_provenance(name, verify_payload=False)
    payload = sort.file_manifest(stage)
    if not payload or (provenance is not None and payload != provenance["payload_hashes"]):
        raise ValueError("Source payload changed since staging.")
    result = {"import_name": name, "payload_hashes": payload, "provenance": provenance}
    _source_rows(issue, result)
    return result


def completion_fingerprint(import_name, tracks, source_manifest=None):
    """One canonical reconstruction for active and held views; None means v1."""
    identity = {"import_name": import_name, "tracks": sorted([list(row) for row in tracks])}
    if source_manifest is not None:
        identity.update(evidence_version=2, source_manifest=source_manifest)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def review_fingerprint(issue):
    manifest = completion_source_manifest(issue)
    return completion_fingerprint(issue["import_name"], _source_rows(issue, manifest), manifest)


def _state_path(filename):
    root = Path(tag.script_folder)
    destination = root / filename
    if root.is_symlink() or destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError("Unsafe review/completion state path.")
    return destination


def _write_state(destination, data):
    _state_path(destination.name)
    descriptor, temporary = tempfile.mkstemp(prefix=".mixie-state-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        _state_path(destination.name)
        os.replace(temporary, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _original_input_evidence(manifest, bulk, archived):
    name, payload, provenance = manifest["import_name"], manifest["payload_hashes"], manifest["provenance"]
    source_name = provenance["source_name"] if provenance else name
    kind = provenance["source_kind"] if provenance else "directory"
    if kind == "directory":
        prefix = source_name + "/"
        selected = {prefix + relative: digest for relative, digest in payload.items()}
        # Completion proves these original inputs, not that a cumulative archive
        # stopped growing. Purge still independently requires its full snapshots.
        if all(archived.get(key) == digest for key, digest in selected.items()):
            return selected
        if provenance:
            raise RuntimeError("Original folder archive differs from source payload.")
        source_name, kind = f"{name}.zip", "zip"  # Explicit legacy-only fallback.
    digest = archived.get(source_name)
    if digest is None or (provenance and digest != provenance["source_sha256"]):
        raise RuntimeError("Original download is missing or changed in the archive.")
    if kind == "zip":
        with zipfile.ZipFile(bulk) as outer, tempfile.TemporaryFile() as temporary:
            with outer.open(source_name) as stream:
                shutil.copyfileobj(stream, temporary, 1024 * 1024)
            temporary.seek(0)
            if sort.zip_manifest(temporary) != payload:
                raise RuntimeError("Archived original payload differs from completion inputs.")
    elif payload != {source_name: digest}:
        raise RuntimeError("Loose original differs from completion inputs.")
    return {source_name: digest}


def _validate_input_map(entry, issue, manifest):
    mapping, assets, receipt = entry.get("input_map"), entry.get("asset_outputs"), entry.get("receipt")
    if entry.get("source_manifest") != manifest or not isinstance(mapping, dict) or set(mapping) != set(issue["tracks"]):
        raise RuntimeError("Completion source/input mapping is incomplete or changed.")
    if not isinstance(assets, dict) or not assets or not isinstance(receipt, dict):
        raise RuntimeError("Completion asset receipt is incomplete.")
    destinations = set()
    rows = dict(_source_rows(issue, manifest))
    for relative, row in mapping.items():
        if not isinstance(row, dict):
            raise RuntimeError("Invalid completion input row.")
        destination = row.get("destination")
        if (row.get("source_sha256") != rows[relative] or not isinstance(destination, str)
                or destination in destinations or destination in assets
                or Path(destination).suffix.lower() not in constants.music_file_types
                or row.get("destination_sha256") != receipt.get(destination)):
            raise RuntimeError("Completion source/input destination evidence differs.")
        destinations.add(destination)
    if destinations | set(assets) != set(receipt) or any(receipt.get(path) != digest for path, digest in assets.items()):
        raise RuntimeError("Receipt contains unrelated or unaccounted outputs.")


def _validate_completed_entry(entry, issue, manifest):
    if not isinstance(entry, dict):
        raise RuntimeError("Album completion missing.")
    receipt = entry.get("receipt")
    if not isinstance(receipt, dict) or len(receipt) < len(issue["tracks"]) + 1:
        raise RuntimeError("Incomplete album receipt.")
    if entry.get("evidence_version") == 2:
        _validate_input_map(entry, issue, manifest)
    elif entry.get("evidence_version") is not None or manifest["provenance"] is not None:
        raise RuntimeError("Managed sources require version-2 completion input evidence.")
    library = Path(sort.music_library_folder).resolve()
    archives = []
    for name in ("originals_archive", "library_archive"):
        value = entry.get(name)
        if not isinstance(value, str) or Path(value).is_symlink() or not Path(value).is_file():
            raise RuntimeError("Completion archive unavailable or unsafe.")
        archives.append(Path(value))
    original_manifest, library_manifest = (sort.zip_manifest(path) for path in archives)
    original_inputs = _original_input_evidence(manifest, archives[0], original_manifest)
    if entry.get("evidence_version") == 2 and entry.get("originals_inputs") != original_inputs:
        raise RuntimeError("Completed original archive input evidence changed.")
    for destination, digest in receipt.items():
        if not isinstance(destination, str) or not isinstance(digest, str):
            raise RuntimeError("Invalid completion receipt.")
        path = Path(destination)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(library):
            raise RuntimeError("Unsafe completion destination.")
        relative = path.resolve().relative_to(library).as_posix()
        if sort.file_checksum(path).hex() != digest or library_manifest.get(relative) != digest:
            raise RuntimeError("Completion receipt or library archive changed.")
    return True


def _payload_completion_entry(ledger, name, tracks, payload, provenance) -> dict:
    manifest = {"import_name": name, "payload_hashes": payload, "provenance": provenance}
    issue = {"import_name": name, "tracks": [f"{name}/{relative}" for relative in tracks]}
    rows = _source_rows(issue, manifest)
    entry = ledger.get(completion_fingerprint(name, rows, manifest))
    if entry is not None:
        if not isinstance(entry, dict) or type(entry.get("evidence_version")) is not int or entry["evidence_version"] != 2:
            raise RuntimeError("Version-2 completion evidence cannot fall back to a legacy receipt.")
    elif provenance is None:
        entry = ledger.get(completion_fingerprint(name, rows))
        if isinstance(entry, dict) and any(field in entry for field in ("evidence_version", "input_map", "source_manifest", "asset_outputs")):
            raise RuntimeError("Invalid versioned evidence under a legacy completion identity.")
    if not isinstance(entry, dict):
        raise RuntimeError("Album completion missing.")
    _validate_completed_entry(entry, issue, manifest)
    return entry


def _quarantine_completion_gate(record, ledger):
    """Validate v2 inputs even after held files have been partly/fully deleted.

    Legacy resumptions remain bound to the original hash-pinned ledger/state;
    fresh legacy albums also pass the original per-album verifier below.
    """
    provenance = sort.validate_cleanup_provenance(record)
    name, payload = record["import_name"], record["staged_hashes"]
    manifest = {"import_name": name, "payload_hashes": payload, "provenance": provenance}
    expected = {f"{name}/{path}" for path in payload if Path(path).suffix.lower() in constants.music_file_types}
    covered = set()
    matching = [(key, entry) for key, entry in ledger.items() if isinstance(entry, dict)
                and entry.get("source_manifest") == manifest]
    for key, entry in matching:
        tracks = entry.get("tracks")
        if not isinstance(tracks, list):
            continue  # Malformed history grants nothing; missing coverage still fails closed.
        issue = {"import_name": name, "tracks": tracks}
        try:
            authority = completion_fingerprint(name, _source_rows(issue, manifest), manifest)
        except (ValueError, TypeError, KeyError):
            continue
        if key != authority:
            continue  # Validate authority only after excluding retained historical evidence.
        if type(entry.get("evidence_version")) is not int or entry["evidence_version"] != 2:
            raise RuntimeError("Version-2 quarantine evidence cannot be downgraded to a legacy receipt.")
        _validate_completed_entry(entry, issue, manifest)
        if covered.intersection(tracks):
            raise RuntimeError("Source input is accounted for more than once.")
        covered.update(tracks)
    if (provenance is not None or matching) and covered != expected:
        raise RuntimeError("Quarantined completion input coverage is incomplete.")
    return provenance


def _completed_payload_groups(record, ledger):
    """Use validated v2 input inventories even when original album tags needed repair."""
    name, payload = record["import_name"], record["staged_hashes"]
    provenance = sort.validate_cleanup_provenance(record)
    manifest = {"import_name": name, "payload_hashes": payload, "provenance": provenance}
    groups = {}
    for key, entry in ledger.items():
        if not isinstance(entry, dict) or entry.get("source_manifest") != manifest:
            continue
        tracks = entry.get("tracks")
        if not isinstance(tracks, list):
            continue
        issue = {"import_name": name, "tracks": tracks}
        if key == completion_fingerprint(name, _source_rows(issue, manifest), manifest):
            groups[key] = [Path(path).relative_to(name).as_posix() for path in tracks]
    return groups


# Read records of successfully completed reviews
def load_completed_reviews():
    path = _state_path(".completed_reviews.json")

    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("entries"), dict)
    ):
        raise ValueError("Invalid completed-review ledger.")

    return data["entries"]

# Verify a previously completed album before skipping it
def is_review_complete(issue, completed_entries):
    if not completed_entries:
        return False
    try:
        manifest = completion_source_manifest(issue)
        tracks = [Path(relative).relative_to(issue["import_name"]).as_posix() for relative in issue["tracks"]]
        _payload_completion_entry(completed_entries, issue["import_name"], tracks,
                                  manifest["payload_hashes"], manifest["provenance"])
        return True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile):
        return False

# Save a completed review after import and archive verification
@_serialized
def record_completed_review(issue, receipt, originals_zip, library_zip):
    if not isinstance(receipt, CommitReceipt) or not receipt:
        raise ValueError("Completion requires an explicit source/input receipt.")
    source_manifest = completion_source_manifest(issue)
    entry = {"album": issue["album"], "tracks": sorted(issue["tracks"]),
             "evidence_version": 2, "receipt": dict(receipt), "input_map": deepcopy(receipt.input_map),
             "source_manifest": deepcopy(receipt.source_manifest), "asset_outputs": deepcopy(receipt.asset_outputs),
             "evidence": review._json_safe(receipt.evidence),
             "originals_archive": str(originals_zip), "library_archive": str(library_zip)}
    _validate_input_map(entry, issue, source_manifest)

    if not Path(originals_zip).is_file():
        raise ValueError("Originals archive is missing.")

    if not Path(library_zip).is_file():
        raise ValueError("Music Library archive is missing.")

    # Verify both archives against their current source files
    library_root = Path(sort.music_library_folder).resolve()

    live_library = sort.file_manifest(library_root)
    archived_library = sort.zip_manifest(library_zip)

    if archived_library != live_library:
        raise RuntimeError(
            "Music Library archive does not match the library."
        )

    live_originals = sort.file_manifest(sort.inbox_folder)
    archived_originals = sort.zip_manifest(originals_zip)

    for name, checksum in live_originals.items():
        if archived_originals.get(name) != checksum:
            raise RuntimeError(
                f"Original download missing from archive: {name}"
            )

    # Every committed file must match its receipt and archive
    for destination, expected_hash in receipt.items():
        path = Path(destination)

        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(library_root)
        ):
            raise RuntimeError(
                f"Invalid completed file: {destination}"
            )

        relative = path.resolve().relative_to(
            library_root
        ).as_posix()

        if (
            live_library.get(relative) != expected_hash
            or archived_library.get(relative) != expected_hash
        ):
            raise RuntimeError(
                f"Completed file failed verification: {relative}"
            )

    entries = load_completed_reviews()
    identity = completion_fingerprint(issue["import_name"], _source_rows(issue, source_manifest), source_manifest)

    entry["originals_inputs"] = _original_input_evidence(source_manifest, originals_zip, archived_originals)
    _validate_completed_entry(entry, issue, source_manifest)
    if completion_source_manifest(issue) != source_manifest:
        raise RuntimeError("Completion inputs changed during archive verification.")
    previous = entries.get(identity)
    if previous is not None and previous != entry:
        history = hashlib.sha256(json.dumps(previous, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        entries.setdefault("history:" + history, previous)
    entries[identity] = entry
    _write_state(_state_path(".completed_reviews.json"), {"version": 1, "entries": entries})

    return identity

# Preserve unresolved reviews and previously approved choices
def _invalidate_stale_choices(issue):
    fields = ("decision", "decisions", "artwork_decision", "manual_fields", "interpretation", "artwork_details", "tag_edits", "review_status", "apply_problem")
    try:
        current = review_fingerprint(issue)
        same = issue.get("source_fingerprint") == current
    except (OSError, ValueError, RuntimeError):
        current, same = None, False
    if not same:
        retired = {field: deepcopy(issue[field]) for field in fields if field in issue}
        if retired:
            issue.setdefault('decision_history', []).append({
                'invalidated_source_fingerprint': issue.get('source_fingerprint'),
                'choices': retired})
        for field in fields:
            issue.pop(field, None)
        issue.pop("source_fingerprint", None)
    elif (issue.get("interpretation") or {}).get("status") == "draft":
        # Legacy reviewers could leave a reviewed label on a postponed draft.
        issue["review_status"] = "needs_review"
    return current, same


def reconcile_review_queue(fresh_issues, *, duplicate_owners=None, checkpoint_only=False, rescanned=()):
    checkpoint_only = checkpoint_only or _review_checkpoint.get()
    previous_issues = load_review_queue()
    rescanned = set(rescanned) if not checkpoint_only else set()
    duplicate_owners = duplicate_owners or {}
    if duplicate_owners:
        preserved = []
        for old in previous_issues:
            owner = duplicate_owners.get(old.get('import_name'))
            if owner is None:
                preserved.append(old)
                continue
            if any(old.get(field) for field in ('decisions', 'decision', 'manual_fields',
                            'artwork_decision', 'tag_edits', 'interpretation')) or old.get('review_status') == 'reviewed':
                raise RuntimeError('Duplicate has existing saved review choices; reconcile them before consolidation.')
            # Only stale, entirely unapproved duplicate diagnostics are retired.
        previous_issues = preserved
    identity = lambda issue: (issue["import_name"], tuple(sorted(issue["tracks"])))
    previous = {identity(issue): issue for issue in previous_issues}
    combined = [deepcopy(issue) for issue in fresh_issues]
    seen = {identity(issue) for issue in combined}
    for old in previous_issues:
        # A completed fresh scan replaces old diagnostic status. Historical
        # decisions are preserved below when bound to exactly the same source;
        # old failures do not reappear as newly confirmed problems.
        if identity(old) not in seen and identity(old) not in rescanned:
            combined.append(deepcopy(old))
            seen.add(identity(old))
    completed, pending = ({} if checkpoint_only else load_completed_reviews()), []
    import_only = (_verified_imports() if _import_first_enabled() and not checkpoint_only else {})
    for entry in import_only.values():
        manifest = entry.get('source_manifest') or {}
        name, tracks = manifest.get('import_name'), entry.get('tracks')
        if not sort._safe_name(name) or not isinstance(tracks, list) or not tracks:
            raise ValueError('Invalid source identity in import receipt; reconciliation stopped safely.')
        issue = {'import_name': name, 'album': entry['album'], 'tracks': list(tracks)}
        # Only a CURRENT verified import can create archive-pending work.
        # Stale/absent receipt evidence belongs to recovery logs and history,
        # never the human metadata review queue.
        if identity(issue) not in seen and is_library_import_verified(issue, import_only):
            _mark_archive_pending(issue)
            combined.append(issue)
            seen.add(identity(issue))
    for issue in combined:
        if import_only:
            import mixie_finalizer
            if mixie_finalizer.is_album_finalized(sys.modules[__name__], issue):
                print(f"Previously archived and verified: {issue['album']}")
                continue
            if is_library_import_verified(issue, import_only):
                _mark_archive_pending(issue)
                print(f"Previously imported; archive verification pending: {issue['album']}")
            # A stale receipt does NOT override the actual Pass-1/2/3 scan
            # result. It is an independent archive/library recovery issue.
        old = previous.get(identity(issue))
        if old is not None:
            # Historical diagnostics remain evidence even after choices go stale.
            history = deepcopy(old.get("review_evidence_history", []))
            if any(old.get(field) != issue.get(field) for field in ("details", "artwork_details")):
                snapshot = {field: deepcopy(old[field]) for field in
                            ("details", "artwork_details", "reason", "error_type", "apply_problem", "source_fingerprint")
                            if field in old}
                if snapshot and snapshot not in history:
                    history.append(snapshot)
            for snapshot in issue.get("review_evidence_history", []):
                if snapshot not in history:
                    history.append(deepcopy(snapshot))
            if history:
                issue["review_evidence_history"] = history
            prior = deepcopy(old)
            _current, same = _invalidate_stale_choices(prior)
            for retired in prior.get('decision_history', []):
                history = issue.setdefault('decision_history', [])
                if retired not in history:
                    history.append(deepcopy(retired))
            if same:
                for field in ("decision", "decisions", "artwork_decision", "manual_fields", "interpretation",
                              "artwork_details", "source_fingerprint", "tag_edits", "review_status", "apply_problem"):
                    if field in old:
                        issue[field] = deepcopy(old[field])
        _invalidate_stale_choices(issue)
        if checkpoint_only:
            # Preserve source-bound pending work during a batch without rehashing
            # immutable album archives. A full proof runs at the batch boundary.
            pending.append(issue)
        elif is_review_complete(issue, completed):
            print(f"Previously completed: {issue['album']}")
        else:
            pending.append(issue)
    return pending


def _queued_album_claims(issues):
    """Retain known sibling ownership without rescanning or trusting source tags."""
    root = Path(tag.staging_folder)
    albums = {}
    for index, issue in enumerate(issues):
        name, tracks = issue.get('import_name'), issue.get('tracks')
        if not sort._safe_name(name) or not isinstance(tracks, list) or not tracks:
            raise ValueError('Invalid queued source claim; archive recovery stopped safely.')
        if any(not isinstance(relative, str) or '\\' in relative
               or any(part in ('', '.', '..') for part in relative.split('/'))
               or Path(relative).is_absolute() or Path(relative).parts[0] != name
               for relative in tracks):
            raise ValueError('Queued source claim crosses its import boundary.')
        if (root / name).is_dir():
            albums[(name, f'queued:{index}')] = {
                'album': issue['album'], 'tracks': [{'path': root / relative} for relative in tracks]}
    return albums


@_serialized
def finalize_verified_imports(*, allow_source_cleanup=False):
    """Recover required album archives from receipts, without another ingestion."""
    import mixie_finalizer
    count, problems = mixie_finalizer.finish_verified_albums(
        sys.modules[__name__], _queued_album_claims(load_review_queue()),
        allow_source_cleanup=allow_source_cleanup)
    pending = reconcile_review_queue([])
    save_review_queue(pending)
    print(f'Newly verified album archive pairs: {count}')
    for problem in problems:
        print(f'Archive/source retention: {problem}')
    if not allow_source_cleanup:
        print('Source cleanup not authorized; Inbox and staging were retained.')
    print(f'Remaining pending albums: {len(pending)}')
    return count, problems


def _pending_retry_has_user_choices(issue):
    """Keep an owner's saved decisions out of unattended automatic retries.

    An unapproved *draft* is only a worklist, not an approval. Preserve it in
    review history, but permit fresh independent verification of the source.
    """
    if issue.get("review_status") == "reviewed":
        return True
    if any(issue.get(field) for field in (
        "decision", "decisions", "artwork_decision", "manual_fields", "tag_edits"
    )):
        return True
    interpretation = issue.get("interpretation") or {}
    return bool(interpretation and (
        interpretation.get("user_approved") is True or
        interpretation.get("status") not in (None, "draft")
    ))


def _pending_retry_diagnostic(issue, error, fingerprint):
    """Update only diagnostics; reconciliation preserves human decisions/history."""
    result = deepcopy(issue)
    details = deepcopy(error.details) if isinstance(
        error, (review.AlbumReviewRequired, ProviderUnavailable)
    ) else {}
    if isinstance(error, tag.MetadataConflictError):
        details = {
            "kind": "metadata_conflict", "field": error.field,
            "existing_values": error.existing, "reference_value": error.expected,
            "track": Path(error.path).name, "path": error.path,
        }
    elif isinstance(error, ArtworkReviewRequired):
        details = {"kind": "artwork_conflict", "candidates": error.candidates,
                   "release_ids": error.release_ids}
    elif isinstance(error, tag.RequiredMetadataReview):
        details = {"kind": "required_metadata", "fields": error.fields}
    elif not details:
        provider = lookup.provider_error_evidence("musicbrainz", error)
        if provider.get("retryable"):
            details = {"kind": "provider_unavailable", "retryable": True,
                       "provider_errors": [provider]}
        else:
            details = {"kind": "retry_error", "retryable": False}
    result.update(
        details=review._json_safe(details), reason=str(error),
        error_type=type(error).__name__,
        status="provider_unavailable" if details.get("retryable") else "needs_review",
        source_fingerprint=fingerprint,
        review_status="needs_review",
    )
    # The draft remains saved, never silently promoted to user approval.
    return result


def _save_pending_retry_diagnostic(refreshed):
    """Checkpoint one unchanged, unapproved pending issue; never audit the whole library.

    The authoritative finalizer still performs full source/receipt/archive
    verification before a queue entry is removed or a physical source purged.
    This fast diagnostic path cannot confer any new completion or cleanup proof.
    """
    key = (refreshed.get('import_name'), tuple(sorted(refreshed.get('tracks') or ())))
    queue = load_review_queue()
    positions = [i for i, previous in enumerate(queue)
                 if (previous.get('import_name'), tuple(sorted(previous.get('tracks') or ()))) == key]
    if len(positions) != 1:
        raise RuntimeError('Pending retry issue is missing or duplicated; refuse to overwrite review queue.')
    index = positions[0]
    prior = queue[index]
    if _pending_retry_has_user_choices(prior):
        raise RuntimeError('Saved user review choices changed; refuse to overwrite review queue.')
    history = deepcopy(prior.get('review_evidence_history') or [])
    if any(prior.get(field) != refreshed.get(field) for field in ('details', 'artwork_details')):
        snapshot = {field: deepcopy(prior[field]) for field in (
            'details', 'artwork_details', 'reason', 'error_type', 'apply_problem', 'source_fingerprint'
        ) if field in prior}
        if snapshot and snapshot not in history:
            history.append(snapshot)
    for snapshot in refreshed.get('review_evidence_history') or ():
        if snapshot not in history:
            history.append(deepcopy(snapshot))
    if history:
        refreshed = deepcopy(refreshed)
        refreshed['review_evidence_history'] = history
    queue[index] = refreshed
    save_review_queue(queue)


def retry_pending_albums(*, preview_only=False, allow_source_cleanup=False):
    """Recheck exact queued albums using the verified album-first automatic path.

    No Inbox restaging, source cleanup, silent human approval or broader catalog
    import. Missing/changed sources, edited reviews and uncertain versions defer.
    Successful imports use normal receipt/archive finalization; only verified
    archived albums leave the persistent queue.
    """
    if preview_only and allow_source_cleanup:
        raise ValueError("A pending-album dry check cannot authorize source cleanup.")
    finish_prior_cleanup_before_ingest(allow_source_cleanup=allow_source_cleanup)
    queue = load_review_queue()
    candidates = [issue for issue in queue if issue.get("status") != "archive_pending"
                  and not is_library_import_verified(issue)]
    print(f"Pending album recheck: {len(candidates)} album(s); "
          f"mode={'verify only' if preview_only else 'verify and import'}")
    if not candidates:
        if not preview_only:
            finalize_verified_imports(allow_source_cleanup=allow_source_cleanup)
        return {"attempted": 0, "verified": 0, "deferred": 0, "protected": 0}
    api_key = get_acoustid_client_key()
    if not api_key:
        raise ValueError("AcoustID client key is not configured; pending albums remain unchanged.")
    grouped = tag.group_staged_albums()
    by_identity = {}
    for (name, _), album in grouped.items():
        claim = _album_issue(album)
        identity = (name, tuple(sorted(claim["tracks"])))
        by_identity.setdefault(identity, []).append(album)

    stats = {"attempted": 0, "verified": 0, "deferred": 0, "protected": 0}
    # Retain every old row until normal finalizer proves import AND two archives.
    for ordinal, issue in enumerate(candidates, 1):
        label = str(issue.get("album", "Untitled"))
        print(f"\nPending {ordinal}/{len(candidates)}: {label}")
        if _pending_retry_has_user_choices(issue):
            stats["protected"] += 1
            print("  Saved user review choices exist; use Review Manager to apply or revise them.")
            continue
        identity = (issue.get("import_name"), tuple(sorted(issue.get("tracks") or ())))
        matches = by_identity.get(identity, [])
        if len(matches) != 1:
            stats["deferred"] += 1
            print("  Staged album missing or ambiguous. Existing review retained.")
            continue
        try:
            current = review_fingerprint(issue)
        except (OSError, ValueError, RuntimeError) as error:
            stats["deferred"] += 1
            print(f"  Source evidence unavailable: {error}; existing review retained.")
            continue
        if issue.get("source_fingerprint") not in (None, current):
            stats["deferred"] += 1
            print("  Source fingerprint differs from saved review; recovery/review required.")
            continue
        stats["attempted"] += 1
        album = matches[0]
        try:
            # Crucial: saved-review testing normally uses manual (non-auto)
            # mode. A fresh unattended retry must use the independent album
            # cohort/edition/credit checks from the actual Auto-Ingest path.
            plan = preview_album(album, api_key, auto_mode=True)
            if current != review_fingerprint(issue):
                raise ValueError("Staged album changed while it was being verified.")
            receipt = test_album_plan(plan, album, commit=not preview_only,
                                      auto=True, preserve_staged_backup=False)
            if not preview_only and not receipt:
                raise ValueError("Working-copy verification did not complete.")
            if not preview_only:
                if not _import_first_enabled():
                    raise ValueError("Pending retry requires verified import-first archival mode.")
                record_library_only_import(_album_issue(album), receipt)
            stats["verified"] += 1
            print("  Working copy verified" + ("; required archives still pending." if not preview_only else "; no files imported."))
        except (Exception, KeyboardInterrupt) as error:
            # User cancellation is saved by the runner's finally-style ledger;
            # unlike a generic error, interruption must stop the batch.
            stats["deferred"] += 1
            print(f"  Still needs review: {error}")
            if not preview_only:
                try:
                    stable = (current == review_fingerprint(issue))
                except (OSError, ValueError, RuntimeError):
                    stable = False
                if stable:
                    refreshed = _pending_retry_diagnostic(issue, error, current)
                else:
                    # Failed checks against changed bytes have NO authority.
                    # Preserve old review history, but don't pin provider or
                    # source interpretations to a new, unverified payload.
                    refreshed = deepcopy(issue)
                    refreshed.update(
                        details={"kind": "source_changed"},
                        reason="Source changed during automatic retry; inspect staging and re-review.",
                        status="needs_review", review_status="needs_review",
                        source_fingerprint=None)
                # Persist this exact existing issue without rehashing every
                # previously archived delivery after EACH unrelated failure.
                # The full verified archive/queue reconciliation still runs
                # once at batch completion; saved choices are never discarded.
                _save_pending_retry_diagnostic(refreshed)
            if isinstance(error, KeyboardInterrupt):
                print("  Interrupted; originals and previous decisions remain protected.")
                break

    if not preview_only:
        finalize_verified_imports(allow_source_cleanup=allow_source_cleanup)
    print("\nPending retry results: " + ", ".join(f"{name}={value}" for name, value in stats.items()))
    if allow_source_cleanup:
        print("Verified, fully accounted source deliveries may now be cleaned; unresolved shared deliveries remain protected.")
    else:
        print("No source cleanup was authorized. Unresolved albums remain in review.")
    return stats


def _retire_absent_review_issues(issues):
    """Retire queue entries whose entire audio inventory left active staging.

    Never interpret an absent source as permission to alter library, original
    backups or import receipts.  Preserve retired decisions in a separate,
    durable history.  A partial/malformed/symlinked inventory remains visible
    for recovery instead of being discarded.
    """
    if not issues:
        return issues
    root = Path(tag.staging_folder).absolute()
    try:
        imports = _verified_imports()
    except (OSError, ValueError, RuntimeError, TypeError):
        # An unreadable receipt ledger is a recovery problem, not proof that
        # a source was never imported. Fail closed on automatic retirement.
        return issues
    active, retired = [], []
    for issue in issues:
        name, tracks = issue.get('import_name'), issue.get('tracks')
        if (not sort._safe_name(name) or not isinstance(tracks, list) or not tracks
                or any(not isinstance(rel, str) or '\\' in rel
                       or Path(rel).is_absolute() or len(Path(rel).parts) < 2
                       or Path(rel).parts[0] != name
                       or any(part in ('', '.', '..') for part in Path(rel).parts)
                       for rel in tracks)):
            active.append(issue)
            continue
        # An import-only receipt does not make an absent audio album an active
        # metadata review. Its own ledger remains available to archive recovery.
        def present_or_unsafe(relative):
            current = root
            for part in Path(relative).parts:
                current = current / part
                if current.is_symlink():
                    return True  # Never retire an unsafe linked path silently.
            return os.path.lexists(current)
        if any(present_or_unsafe(rel) for rel in tracks):
            active.append(issue)
            continue
        retired.append(deepcopy(issue))
    if not retired:
        return active
    history_path = _state_path('.retired_reviews.json')
    if history_path.exists():
        with history_path.open('r', encoding='utf-8') as reader:
            history = json.load(reader)
        if (not isinstance(history, dict) or history.get('version') != 1
                or not isinstance(history.get('issues'), list)):
            raise ValueError('Retired review history is invalid; active queue retained.')
    else:
        history = {'version': 1, 'issues': []}
    known = {(row.get('import_name'), tuple(row.get('tracks') or ()))
             for row in history['issues'] if isinstance(row, dict)}
    for issue in retired:
        identity = (issue['import_name'], tuple(issue['tracks']))
        if identity not in known:
            history['issues'].append(issue)
            known.add(identity)
    # Persist the recovery copy before removing anything from the live queue.
    _write_state(history_path, history)
    print(f'  Retired {len(retired)} absent album review(s) from active queue; '
          'saved their history for recovery.')
    return active


# Save unresolved albums for review after ingestion
def save_review_queue(issues):
    destination = _state_path(".needs_review.json")
    _write_state(destination, {"version": 1, "issues": review._json_safe(issues)})
    return destination


# Load unresolved albums from the persistent review queue
def load_review_queue():
    destination = _state_path(".needs_review.json")

    if not destination.exists():
        return []

    with destination.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("issues"), list)
    ):
        raise ValueError("Invalid review queue format.")

    return data["issues"]

# Ask the user to approve a local album cover
def _approval_fingerprint(issue):
    current = review_fingerprint(issue)
    if issue.get("source_fingerprint") is not None and issue["source_fingerprint"] != current:
        _invalidate_stale_choices(issue)
        print("Source payload changed; previous approvals and drafts were invalidated.")
    return current


def choose_artwork_resolution(issue):
    details = (
        issue.get("artwork_details")
        or issue.get("details", {})
    )

    if details.get("kind") != "artwork_conflict":
        return False

    candidates = details.get("candidates", [])

    if not candidates:
        print("No artwork candidates available.")
        return False

    original_fingerprint = _approval_fingerprint(issue)
    print("\nM.I.X.I.E. - Artwork Review")
    print("Local artwork could not be verified automatically.")

    for index, candidate in enumerate(candidates, 1):
        candidate_path = Path(candidate["path"])
        try:
            label = candidate_path.relative_to(tag.staging_folder)
        except ValueError:
            label = candidate_path.name
        print(f"{index}. {review._shown(label)}")
    print("Choose the cover you recognize. No cover is recommended without your confirmation.")
    print("0. Postpone artwork choice")

    try:
        choice = input("\nSelect artwork: ").strip()
    except (KeyboardInterrupt, EOFError):
        return False

    if choice == "0":
        return False

    if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
        print("Invalid selection.")
        return False

    candidate = candidates[int(choice) - 1]
    path = Path(candidate["path"]).resolve()

    if not path.is_file() or not path.is_relative_to(
        tag.staging_folder.resolve()
    ):
        raise ValueError("Artwork is no longer available.")

    data = path.read_bytes()

    if (
        len(data) > 20_000_000
        or hashlib.sha256(data).hexdigest() != candidate["sha256"]
    ):
        raise ValueError("Artwork changed since review.")

    if review_fingerprint(issue) != original_fingerprint:
        raise ValueError("Source changed during artwork approval.")
    issue["artwork_decision"] = {
        "kind": "local_artwork",
        "path": str(path),
        "sha256": candidate["sha256"],
        "release_ids": details["release_ids"],
        "status": "pending_application"
    }
    issue["source_fingerprint"] = original_fingerprint
    print("Artwork approval saved.")
    return True

# Ask for missing or disputed date/genre in one consolidated review.
def choose_required_metadata_resolution(issue):
    details = issue.get("details", {})
    if details.get("kind") != "required_metadata":
        return False
    needed = details.get("fields", [])
    if not needed or any(field not in ("date", "genre") for field in needed):
        raise ValueError("Unrecognized required metadata fields.")
    original_fingerprint = _approval_fingerprint(issue)
    print("\nM.I.X.I.E. - Required Metadata Review")
    selected = dict(issue.get("manual_fields") or {})
    for field in needed:
        print(f"{field.title()} needs a known value; no value will be guessed.")
        print(f"1. Enter a {field} you know is correct")
        print("0. Postpone — recommended if you are unsure")
        try:
            value = input(f"Choose {field} action (0 = postpone): ").strip()
            if value == "1":
                value = input(f"Enter {field} (0 or blank = postpone): ").strip()
        except (KeyboardInterrupt, EOFError):
            return False
        if value in ("", "0") or value.casefold() == "postpone":
            return False
        selected[field] = value
    try:
        selected = tag.validate_manual_metadata(selected)
    except (ValueError, TypeError) as error:
        print(f"Metadata choice not saved: {error}")
        return False
    if review_fingerprint(issue) != original_fingerprint:
        raise ValueError("Source changed during required metadata approval.")
    issue["manual_fields"] = selected
    issue["source_fingerprint"] = original_fingerprint
    print("Approved metadata saved; staged originals unchanged.")
    return True

def _uniform_album_metadata_paths(issue, field, existing, selected_path):
    """Allow a field-wide choice only when every staged track has the same source value.

    Never widen an approval using just the current conflict's track. All album
    members must be independently present, inside staging, and uniform.
    """
    if field not in ("album", "album_artist") or len(existing) != 1:
        return ()
    relatives = issue.get("tracks") or []
    if not isinstance(relatives, (list, tuple)) or len(relatives) < 2:
        return ()
    root = Path(tag.staging_folder).resolve()
    paths = []
    try:
        for relative in relatives:
            if not isinstance(relative, str):
                return ()
            path = Path(relative)
            if (path.is_absolute() or len(path.parts) < 2
                    or path.parts[0] != issue.get("import_name")
                    or any(part in (".", "..") for part in path.parts)):
                return ()
            source = root / path
            if source.is_symlink() or not source.is_file():
                return ()
            resolved = source.resolve()
            if not resolved.is_relative_to(root):
                return ()
            # Reject symlinked ancestors even when they point back into staging.
            parent = source.parent
            while parent != root:
                if parent.is_symlink() or parent == parent.parent:
                    return ()
                parent = parent.parent
            metadata = tag.read_track_metadata(source)
            if metadata is None or tuple(metadata.get(field, ())) != (existing[0],):
                return ()
            paths.append(resolved)
    except (OSError, RuntimeError, ValueError, TypeError):
        return ()
    if len(set(paths)) != len(paths) or selected_path not in paths:
        return ()
    return tuple(paths)


# Ask the user to resolve a metadata disagreement
def choose_metadata_resolution(issue):
    details = issue.get("details", {})

    if details.get("kind") != "metadata_conflict":
        print("\nThis issue does not have a metadata choice yet.")
        return False

    existing = details["existing_values"]
    reference = details["reference_value"]
    field = details["field"]

    numbering = field in tag._NUMBERING_FIELDS
    if len(existing) != 1 and not numbering:
        print("\nMultiple existing values require interpretation review.")
        return False
    original_fingerprint = _approval_fingerprint(issue)
    path = details.get("path")
    if path is None and details.get("track"):
        candidates = [relative for relative in issue["tracks"]
                      if Path(relative).name == details["track"]]
        if len(candidates) == 1:
            path = str((Path(tag.staging_folder) / candidates[0]).resolve())
    source_path = Path(path).resolve() if path is not None else None
    bulk_paths = _uniform_album_metadata_paths(issue, field, existing, source_path)

    print(f"\nMetadata disagreement: {field.replace('_', ' ').title()}")
    if source_path is not None:
        print(f"Source track: {review._shown(source_path.name)}")
    current_label = "; ".join(review._shown(value) for value in existing) or "not set"
    print(f"1. Keep existing: {current_label}")
    print(f"2. Use matched release: {review._shown(reference)}")
    print("3. Enter a value you know is correct")
    print("0. Postpone this choice")
    if numbering:
        print("Recommended: 2 only if this is the right release; otherwise postpone or change the album mapping.")
    else:
        print("Recommended: compare the two values; postpone rather than guessing about an artist or edition.")

    while True:
        try:
            choice = input("\nSelect an option: ").strip()
        except (KeyboardInterrupt, EOFError):
            return False

        if choice == "1":
            if not existing:
                print("No existing value is available to keep.")
                continue
            selected = existing[0]
        elif choice == "2":
            selected = reference
        elif choice == "3":
            try:
                selected = input("Enter your preferred value (0 or blank = postpone): ").strip()
            except (KeyboardInterrupt, EOFError):
                return False
            if selected in ("", "0") or selected.casefold() == "postpone":
                return False
        elif choice == "0":
            return False
        else:
            print("Invalid selection.")
            continue

        if numbering and selected != reference:
            print("Numbering must agree with the chosen release mapping; use interpretation review to change that mapping.")
            return False
        # A full-album selection is a separate, explicit approval. An earlier
        # track-specific approval with a different value must never be overridden.
        decision_path = str(source_path) if source_path is not None else None
        if bulk_paths:
            earlier = [item for item in _issue_decisions(issue)
                       if item.get("field") == field and item.get("path") is not None]
            incompatible = any(
                (Path(item["path"]).resolve() not in bulk_paths
                 or item.get("value") != selected
                 or item.get("existing_value") != existing[0]
                 or item.get("reference_value") != reference)
                for item in earlier
            )
            if incompatible:
                print("Existing track-specific approvals differ. This choice stays track-specific;"
                      " change earlier choices explicitly before applying it across the album.")
            else:
                print(f"\nAll {len(bulk_paths)} album tracks have the same original {field.replace('_', ' ')} value.")
                print(f"1. Apply {review._shown(selected)} to all {len(bulk_paths)} tracks")
                print(f"2. Apply only to {review._shown(source_path.name)}")
                print("0. Postpone without saving this choice")
                try:
                    scope = input("Choose approval scope (0 = postpone): ").strip()
                except (KeyboardInterrupt, EOFError):
                    return False
                if scope == "0":
                    return False
                if scope == "1":
                    decision_path = None
                elif scope != "2":
                    print("Invalid scope. Choice not saved.")
                    return False
        elif source_path is not None and field in ("album", "album_artist"):
            print("This choice applies only to the shown track; other tracks may need review.")
        if review_fingerprint(issue) != original_fingerprint:
            raise ValueError("Source changed during metadata approval.")
        decision = {"field": field, "value": selected,
                    "existing_value": list(existing) if numbering else existing[0],
                    "reference_value": reference, "status": "pending_application",
                    "source_fingerprint": original_fingerprint}
        if decision_path is not None:
            decision["path"] = decision_path
        previous = _issue_decisions(issue)
        previous = [item for item in previous if (item.get("path"), item["field"]) != (decision.get("path"), field)]
        issue["decisions"] = previous + [decision]
        issue["decision"] = decision  # Keep old queue readers compatible.
        issue["source_fingerprint"] = original_fingerprint
        print(f"\nChoice saved: {review._shown(selected)}")
        if decision_path is None:
            if bulk_paths:
                print(f"Approval scope: all {len(bulk_paths)} album tracks.")
            else:
                print("Approval scope: field-wide legacy choice.")
        else:
            print(f"Approval scope: {review._shown(Path(decision_path).name)} only.")
        print("No audio files have been modified.")
        return True

# Test a saved review decision without committing to the library
@_serialized
def test_review_decision(issue, commit=False):
    decision = _issue_decisions(issue) or None
    interpretation = issue.get("interpretation")
    if isinstance(interpretation, dict) and interpretation.get("status") == "draft":
        interpretation = None  # Retain the draft, but retry automatic checks without treating it as approval.
    artwork_decision = issue.get("artwork_decision")
    manual_fields = issue.get("manual_fields")
    chosen_tag_edits = issue.get("tag_edits")
    approved_source = issue.get("source_fingerprint")
    current_source = review_fingerprint(issue)

    if (
        (decision is not None or artwork_decision is not None or manual_fields or chosen_tag_edits or interpretation is not None)
        and approved_source != current_source
    ):
        raise ValueError(
            "Original audio changed since approval. Review again."
        )

    if decision is not None:
        _decision_list(decision)
    if artwork_decision is not None:
        if artwork_decision.get("status") != "pending_application":
            raise ValueError("Artwork decision needs fresh approval.")

    staging_root = tag.staging_folder.resolve()
    expected_tracks = {
        (staging_root / relative).resolve()
        for relative in issue["tracks"]
    }

    if not expected_tracks or any(
        not path.is_relative_to(staging_root)
        for path in expected_tracks
    ):
        raise ValueError("Review contains invalid staging paths.")

    matches = [
        album
        for (import_name, _), album
        in tag.group_staged_albums().items()
        if import_name == issue["import_name"]
        and {
            Path(track["path"]).resolve()
            for track in album["tracks"]
        } == expected_tracks
    ]

    if len(matches) != 1:
        raise ValueError("Cannot uniquely identify the staged album.")

    api_key = get_acoustid_client_key() if interpretation is None else None
    if not api_key and interpretation is None:
        raise ValueError("AcoustID key is not configured.")

    album = matches[0]

    plan = preview_album(
        album,
        api_key,
        reviewed_decision=decision,
        artwork_decision=issue.get("artwork_decision"),
        manual_fields=manual_fields,
        interpretation=interpretation, tag_edits=chosen_tag_edits
    )
    if current_source != review_fingerprint(issue):
        raise ValueError("Source changed while revalidating review approval.")

    if commit:
        with terminal.compact_operation(f"Verifying and importing {album['album']}", root=tag.script_folder):
            receipt = test_album_plan(plan, album, commit=True, auto=True)
    else:
        receipt = test_album_plan(plan, album, commit=False, auto=False)

    if commit:
        if receipt is None:
            print("\nReviewed import was not completed.")
        else:
            print("\nReviewed album imported and verified.")
            print('Required album archive finalization is still pending; sources remain protected.')
    else:
        print("\nReviewed metadata passed working-copy verification.")
        print("Music Library and staged originals remain unchanged.")

    return receipt

# Import a reviewed album, verify archives, and retire its review
@_serialized
def finalize_reviewed_album(issue, issues, *, allow_source_cleanup=False):
    finish_prior_cleanup_before_ingest(allow_source_cleanup=allow_source_cleanup)
    import_first = _import_first_enabled()
    # A restart after a failed archive must not re-identify or import the album.
    already_imported = import_first and is_library_import_verified(issue)
    receipt = None if already_imported else test_review_decision(issue, commit=True)
    if not already_imported and receipt is None:
        print("Import cancelled. Album remains in review.")
        return False

    if import_first:
        if receipt is not None:
            record_library_only_import(issue, receipt)
        import mixie_finalizer
        _archived, problems = mixie_finalizer.finish_verified_albums(
            sys.modules[__name__], _queued_album_claims(issues),
            allow_source_cleanup=allow_source_cleanup)
        for problem in problems:
            print(f'Archive/source retention: {problem}')
        if not mixie_finalizer.is_album_finalized(sys.modules[__name__], issue):
            _mark_archive_pending(issue, '; '.join(problems) or 'finalization proof incomplete')
            save_review_queue(issues)
            print('Import is verified; review retained until both required archives verify.')
            return False
        remaining = [entry for entry in issues if entry is not issue]
        save_review_queue(remaining)
        issues[:] = remaining
        print(f"\nReview completed; import and album archives verified: {issue['album']}")
        print('Shared/unapproved source cleanup remains safely retained.')
        print(f'Remaining pending reviews: {len(issues)}')
        return True

    print("\nFinalizing and verifying archives...")

    with terminal.compact_operation("Verifying archive backups", root=tag.script_folder):
        originals_zip = archive.update_original_downloads_archive()
        library_zip = archive.create_verified_zip(
            sort.music_library_folder, "MusicLibrary"
        )

    # Only mark complete after all archive and receipt checks pass
    record_completed_review(
        issue, receipt, originals_zip, library_zip
    )

    remaining = [
        entry for entry in issues
        if entry is not issue
    ]

    save_review_queue(remaining)
    issues[:] = remaining

    print(f"\nReview completed: {issue['album']}")
    print(f"Remaining pending reviews: {len(issues)}")
    with terminal.compact_operation("Verifying safe cleanup", root=tag.script_folder):
        quarantined = auto_quarantine_archived_completed(
            originals_zip, library_zip, allow_source_cleanup=allow_source_cleanup
        )
        auto_purge_verified_quarantines(allow_source_cleanup=allow_source_cleanup)
    if quarantined:
        print(
            f"Verified imports quarantined: "
            f"{', '.join(quarantined)}"
        )
    else:
        print("Unfinished imports remain safely staged.")

    return True

def _choose_album_interpretation(issue, issues):
    """Expose full interpretation review without discarding the original conflict."""
    fingerprint = review_fingerprint(issue)
    working = deepcopy(issue)
    details = working.get("details", {})
    if ((details.get("kind") == "provider_unavailable" and details.get("tracks"))
            or (details.get("kind") == "metadata_conflict"
                and details.get("tracks") and details.get("releases"))):
        details["kind"] = "identity_release"
    elif working.get("details", {}).get("kind") != "identity_release":
        audits = []
        for relative in issue["tracks"]:
            path = Path(tag.staging_folder) / relative
            audits.append({"path": path, "status": "needs_review", "details": None,
                           "reason": issue["reason"], "existing": tag.read_track_metadata(path)})
        working["details"] = review.issue_details(audits, [], issue["reason"])

    def checkpoint(snapshot):
        if review_fingerprint(issue) != fingerprint:
            raise ValueError("Source changed while reviewing; fresh approval is required.")
        fields = ("interpretation", "source_fingerprint", "review_status", "apply_problem")
        previous = {field: deepcopy(issue[field]) for field in fields if field in issue}
        try:
            for field in ("interpretation", "source_fingerprint"):
                if field in snapshot:
                    issue[field] = deepcopy(snapshot[field])
            interpretation = issue.get("interpretation") or {}
            approved = (interpretation.get("status") == "pending_application"
                        and interpretation.get("user_approved") is True)
            issue["review_status"] = "reviewed" if approved else "needs_review"
            if approved:
                issue.pop("apply_problem", None)
            save_review_queue(issues)
        except BaseException:
            # A failed durable save cannot leave an in-memory approval behind.
            for field in fields:
                issue.pop(field, None)
            issue.update(previous)
            raise

    return review.choose_interpretation(working, fingerprint, save=checkpoint)


def _retire_metadata_choices(issue):
    """Retain old approvals as history only after an explicit reset request."""
    if input("Type CLEAR to retire earlier metadata choices, or Enter to keep them: ").strip() != "CLEAR":
        return False
    previous = {field: issue[field] for field in ("decision", "decisions", "manual_fields") if field in issue}
    if previous:
        issue.setdefault("decision_history", []).append(deepcopy(previous))
        for field in previous:
            issue.pop(field)
    return bool(previous)


def _offline_album_review_rows(issue):
    """Read every source tag once, without provider I/O or mutating staging."""
    root = Path(tag.staging_folder).resolve()
    name = issue.get("import_name")
    relatives = issue.get("tracks")
    if (not isinstance(name, str) or not name or not isinstance(relatives, list)
            or not relatives or len(set(relatives)) != len(relatives)):
        raise ValueError("Invalid album review inventory.")
    rows = []
    for relative in relatives:
        if not isinstance(relative, str):
            raise ValueError("Invalid source path in review inventory.")
        part = Path(relative)
        if (part.is_absolute() or len(part.parts) < 2 or part.parts[0] != name
                or any(component in (".", "..") for component in part.parts)):
            raise ValueError("Unsafe review source path.")
        source = root / part
        current = source
        while current != root:
            if current.is_symlink() or current == current.parent:
                raise ValueError("Symlink in review source path.")
            current = current.parent
        if not source.is_file() or not source.resolve().is_relative_to(root):
            raise ValueError("Review source is missing or outside staging.")
        metadata = tag.read_track_metadata(source)
        if metadata is None:
            raise ValueError("Review source metadata is unreadable.")
        rows.append(metadata)
    return rows


def _show_album_review_worklist(issue, rows=None):
    """Human-first, offline summary of issues known before final processing."""
    if rows is None:
        rows = _offline_album_review_rows(issue)
    print(f"\n=== Album checklist: {review._shown(issue['album'])} ===")
    print(f"Tracks: {len(rows)} | Source tags are read-only | No provider request")
    print("Shared original tags (blank fields can be filled only from unanimous values):")
    saved = issue.get("manual_fields") or {}
    for field, label in (("album", "Album"), ("album_artist", "Album artist"),
                         ("date", "Date"), ("genre", "Genre")):
        counts = {}
        missing = 0
        for row in rows:
            values = row.get(field) or ()
            if len(values) != 1 or not values[0].strip():
                missing += 1
            else:
                counts[values[0]] = counts.get(values[0], 0) + 1
        if field in saved:
            summary = f"USER CHOICE: {saved[field]} (all tracks on working copies)"
        elif len(counts) == 1:
            summary = f"{next(iter(counts))} ({missing} missing)"
        elif not counts:
            summary = f"not set on any track ({missing} missing)"
        else:
            summary = f"CONFLICT: {len(counts)} distinct values ({missing} missing)"
        print(f"  {label}: {review._shown(summary)}")
    if all(not row.get("album_artist") for row in rows):
        artist_hint = _suggest_album_artist(rows) or _suggest_compilation_album_artist(rows)
        if artist_hint:
            print(f"  Suggested album artist: {review._shown(artist_hint)} "
                  "[album context; not independently verified]")
    if all(not row.get("genre") for row in rows) and "genre" not in saved:
        artist_hint = _suggest_album_artist(rows)
        if artist_hint:
            genre_hint = _catalog_genre_hint({"tracks": rows}, artist_hint)
            if genre_hint:
                print(f"  Suggested genre: {review._shown(genre_hint)} "
                      "[consistent with other albums by this artist; not independently verified]")
    missing_totals = sum(not tag.get_track_totals(row) or not tag.get_disc_totals(row) for row in rows)
    print(f"  Track/disc totals incomplete in {missing_totals}/{len(rows)} tracks;")
    print("  final values require a full verified release or explicit approved totals.")
    details = issue.get("details") or {}
    kind = details.get("kind")
    if kind in ("identity_release", "provider_unavailable"):
        tracks = details.get("tracks") or []
        uncertain = [r for r in tracks if (r.get("assessment") or {}).get("status", r.get("status"))
                     not in ("verified", "preliminary_match")]
        print(f"  Recording identities needing attention: {len(uncertain)}")
        print(f"  Potential release editions recorded: {len(details.get('releases') or [])}")
    elif kind == "metadata_conflict":
        print(f"  Known metadata conflict: {review._shown(details.get('field', 'unknown'))}")
    elif kind == "artwork_conflict" or issue.get("artwork_details"):
        print("  Local artwork selection requires confirmation")
    elif kind == "required_metadata":
        pending = [field for field in details.get("fields", ()) if field not in saved]
        if pending:
            print("  Required values still missing: " + ", ".join(pending))
        else:
            print("  Saved required metadata choice; final processing still needs verification.")
    # A deferred album has not reached the normal proposal writer; make its
    # saved audio evidence visible without treating a suggestion as approval.
    if kind in ("identity_release", "provider_unavailable") and isinstance(details.get("tracks"), list):
        review.show_tag_suggestions(details["tracks"])
    elif kind == "metadata_conflict":
        print(f"  Current {review._shown(details.get('field', '?'))}: {review._shown(details.get('existing_values'))}")
        print(f"  Suggested {review._shown(details.get('field', '?'))}: {review._shown(details.get('reference_value'))}")
    for decision in _issue_decisions(issue):
        selected = decision.get("path")
        scope = (f"{review._shown(Path(selected).name)} only" if selected else "album-wide")
        print(f"  Saved metadata choice ({scope}): "
              f"{review._shown(decision.get('field', '?'))} = "
              f"{review._shown(decision.get('value', '?'))}")
    print("Known blockers are shown here; final validation may uncover additional ones.")
    print("All decisions persist until you explicitly choose final processing.")


def _edit_offline_shared_metadata(issue, issues):
    """Edit album-wide date/genre without re-identifying or processing tracks."""
    fingerprint = review_fingerprint(issue)
    while True:
        rows = _offline_album_review_rows(issue)
        _show_album_review_worklist(issue, rows)
        print("\n1. Set / correct release date for this album")
        print("2. Set / correct genre for this album")
        print("3. Resolve the known metadata disagreement (if any)")
        print("4. Select known artwork (if already flagged)")
        print("5. Review recording identities and release (saves draft, no processing)")
        print("6. Edit song titles, artists and track/disc numbers (guided)")
        print("7. Edit album title and album artist (guided)")
        print("0. Return to album menu with every decision saved")
        try:
            choice = input("Review checklist action (0 = back): ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if choice == "0":
            return
        if choice in ("1", "2"):
            field = "date" if choice == "1" else "genre"
            selected = dict(issue.get("manual_fields") or {})
            current = selected.get(field)
            print(f"Current manual choice: {review._shown(current) if current else 'none'}")
            print("Enter a known correct value; '-' removes the manual choice; blank keeps it.")
            try:
                value = input(f"{field.title()} (Enter = keep): ").strip()
            except (KeyboardInterrupt, EOFError):
                continue
            if not value:
                continue
            if value == "-":
                selected.pop(field, None)
            else:
                selected[field] = value
            try:
                selected = tag.validate_manual_metadata(selected)
            except (TypeError, ValueError) as error:
                print(f"Choice rejected: {error}")
                continue
            if review_fingerprint(issue) != fingerprint:
                raise ValueError("Source changed during review; choices were not saved.")
            issue["manual_fields"] = selected
            issue["source_fingerprint"] = fingerprint
            save_review_queue(issues)
            print("Saved as a user-approved album-wide choice; originals untouched.")
        elif choice == "3":
            if (issue.get("details") or {}).get("kind") != "metadata_conflict":
                print("No recorded metadata conflict to approve; no approval invented.")
            elif choose_metadata_resolution(issue):
                save_review_queue(issues)
        elif choice == "4":
            details = issue.get("artwork_details") or issue.get("details") or {}
            if details.get("kind") != "artwork_conflict":
                print("No verified artwork review choices have been prepared yet.")
            else:
                temporary = issue.get("details")
                approved = False
                try:
                    issue["details"] = details
                    approved = choose_artwork_resolution(issue)
                finally:
                    issue["details"] = temporary
                if approved:
                    save_review_queue(issues)
        elif choice in ("5", "6", "7"):
            print("Opening guided tag review. Choose 1 for songs or 3 for album details.")
            print("Save a draft or explicitly APPROVE; originals are never edited.")
            _choose_album_interpretation(issue, issues)
        else:
            print("Choose a numbered action or 0 to return.")


# Review albums sequentially, without returning to the list each time
@_serialized
def review_queue_menu(issues):
    selected = None

    while True:
        if not issues:
            print("\nMIXIE: All pending reviews completed!")
            return

        # Show the album list only when requested
        if selected is None:
            print("\n================================")
            print("  M.I.X.I.E. - Review Queue")
            print("================================")

            for number, entry in enumerate(issues, 1):
                waiting = " — waiting for online checks" if entry.get("details", {}).get("kind") == "provider_unavailable" else ""
                print(f"{number}. {review._shown(entry['album'])}{waiting}")

            print("0. Finish reviewing")

            try:
                choice = input(
                    "\nSelect album (Enter = first): "
                ).strip()
            except (KeyboardInterrupt, EOFError):
                return

            if choice == "0":
                print("Unresolved albums remain in staging.")
                return

            if not choice:
                selected = 0
            elif choice.isdigit() and 1 <= int(choice) <= len(issues):
                selected = int(choice) - 1
            else:
                print("Invalid selection.")
                continue

        selected %= len(issues)
        issue = issues[selected]
        _invalidate_stale_choices(issue)
        kind = issue.get("details", {}).get("kind")

        print("\n================================")
        print(f"  Reviewing {selected + 1}/{len(issues)}")
        print("================================")
        print(f"Album: {review._shown(issue['album'])}")
        print(f"Import: {review._shown(issue['import_name'])}")
        print(f"Tracks: {len(issue['tracks'])}")
        if kind == "provider_unavailable":
            print("\nOnline verification was interrupted; this is not a rejected match.")
            print("Recommended: choose 1 to retry online checks later, or 0 to postpone. Other uncertainties may remain.")
        else:
            print(f"\nWhy this needs attention: {review._shown(issue['reason'])}")
            print("Recommended: review the choices below; postpone if you are unsure.")

        # Review starts with one offline inventory, not an immediate question
        # or a full provider/identity re-run. Decisions are edited and saved
        # explicitly; processing is a separate menu action.
        try:
            _show_album_review_worklist(issue)
        except (OSError, ValueError, TypeError, MutagenError) as error:
            print(f"Offline checklist unavailable: {error}")
            print("Saved review evidence is retained; no processing was started.")

        while True:
            kind = issue.get("details", {}).get("kind")
            print("\nReview choices can be saved together under option 7 without reprocessing.")
            print("1. " + ("Retry online checks (recommended)" if kind == "provider_unavailable" else "Final processing / validate saved choices"))
            print("2. Review song identities and album choices")
            print("3. Change a saved choice")
            print("4. Postpone this album and see the next one")
            print("5. Return to the album list")
            print("6. Advanced technical details")
            print("7. View suggested tag edits / edit song and album tags OFFLINE")
            print("0. Finish reviewing — keep unresolved albums and drafts")
            try:
                command = input("\nChoose album action (0 = postpone and finish): ").strip().casefold()
                command = {"1": "p", "2": "i", "3": "c", "4": "", "5": "m", "6": "details", "7": "checklist"}.get(command, command)
            except (KeyboardInterrupt, EOFError):
                print("\nReview postponed.")
                return

            if command == "checklist":
                try:
                    _edit_offline_shared_metadata(issue, issues)
                except (ValueError, OSError) as error:
                    print(f"Cannot open album checklist: {error}")
                continue

            if command == "details":
                print(json.dumps(review._json_safe(issue.get("details", {})), ensure_ascii=True, indent=2))
                print("Advanced aliases: process / p, i (interpret), c (change), m (album list).")
                continue

            if command == "0":
                print("Unresolved albums remain in staging.")
                return

            if command == "m":
                selected = None
                break

            if command == "":
                selected = (selected + 1) % len(issues)
                break


            if command == "i":
                _choose_album_interpretation(issue, issues)
                continue

            if command == "c":
                choices = {
                    "4": ("Recording/release interpretation", lambda selected_issue: _choose_album_interpretation(selected_issue, issues)),
                    "5": ("Retire earlier metadata approvals", _retire_metadata_choices),
                }

                if kind == "metadata_conflict":
                    choices["1"] = (
                        "Metadata", choose_metadata_resolution
                    )

                if (
                    kind == "artwork_conflict"
                    or issue.get("artwork_details", {}).get("kind")
                        == "artwork_conflict"
                ):
                    choices["2"] = (
                        "Artwork", choose_artwork_resolution
                    )

                if kind == "required_metadata":
                    choices["3"] = (
                        "Required metadata", choose_required_metadata_resolution
                    )

                if not choices:
                    print("No editable choices for this issue yet.")
                    continue

                if len(choices) == 1:
                    callback = next(iter(choices.values()))[1]
                else:
                    print("\nWhich decision would you like to change?")
                    for number, (label, _) in choices.items():
                        print(f"{number}. {label}")
                    print("0. Postpone changing this choice")
                    try:
                        selection = input(
                            "Choice (0 or Enter to postpone): "
                        ).strip()
                    except (KeyboardInterrupt, EOFError):
                        continue

                    if selection not in choices:
                        print("Choice change cancelled.")
                        continue

                    callback = choices[selection][1]

                try:
                    if callback(issue):
                        save_review_queue(issues)
                        print("Updated choice saved.")
                except Exception as error:
                    print(f"Review choice failed: {error}")

                continue

            if command in ("p", "process"):

                completed = False

                try:
                    completed = finalize_reviewed_album(
                        issue, issues
                    )

                except (review.AlbumReviewRequired, ProviderUnavailable) as error:
                    issue["details"] = error.details
                    issue["reason"] = str(error)
                    issue["error_type"] = type(error).__name__
                    save_review_queue(issues)
                    if not isinstance(error, ProviderUnavailable):
                        _choose_album_interpretation(issue, issues)
                    else:
                        print("Online checks are still unavailable. Recommended: postpone and retry later.")

                except tag.RequiredMetadataReview as error:
                    issue["details"] = {"kind": "required_metadata", "fields": error.fields}
                    issue["reason"] = str(error)
                    save_review_queue(issues)
                    if choose_required_metadata_resolution(issue):
                        save_review_queue(issues)

                except tag.MetadataConflictError as error:
                    if issue.get("details", {}).get("kind") == "artwork_conflict":
                        issue["artwork_details"] = issue["details"]

                    issue["details"] = {
                        "kind": "metadata_conflict",
                        "field": error.field,
                        "existing_values": error.existing,
                        "reference_value": error.expected,
                        "track": Path(error.path).name,
                        "path": error.path
                    }
                    issue["reason"] = str(error)
                    issue["error_type"] = type(error).__name__

                    save_review_queue(issues)
                    print("\nMetadata approval required.")

                    if choose_metadata_resolution(issue):
                        save_review_queue(issues)
                        print("Choice saved. Choose 1 to process when ready.")

                except ArtworkReviewRequired as error:
                    issue["artwork_details"] = {
                        "kind": "artwork_conflict",
                        "candidates": error.candidates,
                        "release_ids": error.release_ids
                    }
                    save_review_queue(issues)

                    print("\nAdditional artwork approval required.")

                    if choose_artwork_resolution(issue):
                        save_review_queue(issues)
                        print("Artwork choice saved. Review any other known issues before final processing.")
                        print("No automatic reprocessing was started.")

                except (Exception, KeyboardInterrupt) as error:
                    print(f"\nProcessing stopped: {error}")
                    print("Album remains pending unless completed.")

                if completed:
                    # The completed album has been removed from issues.
                    # Its successor now occupies the same position.
                    if not issues:
                        print("\nAll reviews completed!")
                        return

                    selected %= len(issues)
                    break

                continue

            print("Invalid option.")

# Find safe, previously approved decisions for an album
def reusable_review_decisions(key, album, pending_issues):
    staging = tag.staging_folder.resolve()

    tracks = tuple(sorted(
        Path(track["path"]).resolve()
        .relative_to(staging).as_posix()
        for track in album["tracks"]
    ))

    matches = [
        issue for issue in pending_issues
        if issue.get("import_name") == key[0]
        and tuple(sorted(issue.get("tracks", []))) == tracks
    ]

    # An ambiguous or missing queue entry cannot authorize changes
    if len(matches) != 1:
        return None, None, None

    issue = matches[0]
    metadata = _issue_decisions(issue) or None
    artwork = issue.get("artwork_decision")
    manual = issue.get("manual_fields")

    if metadata is None and artwork is None and not manual:
        return None, None, None

    # Older approvals without fingerprints require fresh review
    if issue.get("source_fingerprint") != review_fingerprint(issue):
        print(f"Saved approval needs fresh review: {album['album']}")
        return None, None, None

    if (
        metadata is not None
        and any(item.get("status") != "pending_application" for item in metadata)
    ):
        return None, None, None

    if (
        artwork is not None
        and artwork.get("status") != "pending_application"
    ):
        return None, None, None

    print(f"Reusing approved decisions: {album['album']}")
    return metadata, artwork, tag.validate_manual_metadata(manual)

@_serialized
def auto_quarantine_archived_completed(originals_zip, library_zip, *, allow_source_cleanup=False):
    """Safely quarantine fully completed imports only with explicit authorization."""
    if type(allow_source_cleanup) is not bool:
        raise ValueError('Source cleanup authorization must be an explicit boolean.')
    if not allow_source_cleanup:
        return CleanupResults()
    try:
        ledger = load_completed_reviews()
        staging = tag.staging_folder.resolve()
        completed = {}

        for key, album in tag.group_staged_albums().items():
            issue = {
                "import_name": key[0],
                "album": album["album"],
                "tracks": [
                    Path(track["path"]).resolve()
                    .relative_to(staging).as_posix()
                    for track in album["tracks"]
                ]
            }

            if is_review_complete(issue, ledger):
                manifest = completion_source_manifest(issue)
                relatives = [Path(path).relative_to(issue["import_name"]).as_posix() for path in issue["tracks"]]
                completed[key] = _payload_completion_entry(ledger, issue["import_name"], relatives,
                    manifest["payload_hashes"], manifest["provenance"])["receipt"]

        if not completed:
            return []

        # The sorter verifies archives, receipts and every
        # original file again before moving anything.
        # Run both cleanup routes independently. A failure in one
        # must not hide a successful quarantine in the other.
        moved = CleanupResults()

        for label, operation in (
            ("Archive-backed", sort.quarantine_archived_cleanup),
            ("Inbox-backed", sort.quarantine_inbox_cleanup),
        ):
            try:
                moved.extend(operation(
                    completed,
                    originals_zip,
                    library_zip,
                    approved=True
                ))
            except Exception as error:
                moved.errors.append(str(error))
                print(f"{label} cleanup deferred: {error}")

        return moved


    except Exception as error:
        print(f"\nAutomatic cleanup deferred: {error}")
        print("Originals and staged files retained for safety.")
        return CleanupResults(errors=[str(error)])

def verify_archived_purge_candidates():
    """Verify quarantined imports before permitting any purge."""
    from collections import defaultdict
    import shutil
    import zipfile
    import constants

    backup = (
        Path(sort.script_folder)
        / constants.backup_folder_name
    ).resolve()
    root = backup / ".cleanup-quarantine"

    if not root.exists():
        return []

    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe quarantine directory.")

    ledger = load_completed_reviews()
    pending = {
        issue["import_name"]
        for issue in load_review_queue()
    }

    library = Path(sort.music_library_folder).resolve()
    live_files = sort.file_manifest(library)
    ready = []

    for folder in sorted(root.iterdir()):
        if not folder.name.startswith(".mixie-archived-"):
            continue

        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("Unsafe quarantine folder.")

        # Refuse unexpected files, including interrupted operations.
        if {p.name for p in folder.iterdir()} != {
            "recovery.json", "staging"
        }:
            raise RuntimeError(
                f"Unexpected quarantine contents: {folder}"
            )

        journal = folder / "recovery.json"
        held = folder / "staging"

        if journal.is_symlink() or held.is_symlink():
            raise ValueError("Unsafe quarantine contents.")

        with journal.open(encoding="utf-8") as file:
            record = json.load(file)

        provenance = sort.validate_cleanup_provenance(record)
        name = record.get("import_name")

        if (
            record.get("version") != 1
            or not isinstance(name, str)
            or name in ("", ".", "..")
            or Path(name).name != name
            or "/" in name or "\\" in name
        ):
            raise ValueError("Invalid quarantine identity.")

        stage = Path(sort.staging_folder).resolve() / name

        if (
            record.get("original_staging_path") != str(stage)
            or stage.exists()
            or stage.is_symlink()
            or name in pending
        ):
            raise RuntimeError(
                f"Import not safe to purge: {name}"
            )

        staged = sort.file_manifest(held)

        if not staged or staged != record["staged_hashes"]:
            raise RuntimeError(
                f"Quarantine checksum mismatch: {name}"
            )

        # Verify the original ZIP still contains every staged file.
        bulk = Path(record["original_archive"])
        source_name = record["archived_source"]

        if (
            bulk.is_symlink()
            or bulk.resolve() != backup / "OriginalDownloads-Bulk.zip"
            or not _safe_component(source_name)
            or (provenance is None and source_name != f"{name}.zip")
        ):
            raise RuntimeError(
                f"Invalid archived original: {name}"
            )

        archived = sort.zip_manifest(bulk)

        if archived.get(source_name) != record["archived_source_hash"]:
            raise RuntimeError(
                f"Original ZIP checksum mismatch: {name}"
            )

        with zipfile.ZipFile(bulk) as outer:
            with tempfile.TemporaryFile() as temporary:
                with outer.open(source_name) as source:
                    shutil.copyfileobj(
                        source, temporary, 1024 * 1024
                    )
                temporary.seek(0)
                original_files = sort.zip_manifest(temporary)

        if original_files != staged:
            raise RuntimeError(
                f"Archived originals differ: {name}"
            )

        snapshot = Path(record["library_snapshot"])

        if (
            snapshot.is_symlink()
            or snapshot.resolve().parent != backup
            or not snapshot.name.startswith("MusicLibrary-")
            or snapshot.suffix.lower() != ".zip"
        ):
            raise RuntimeError("Invalid library snapshot.")

        saved_files = sort.zip_manifest(snapshot)

        if saved_files != live_files:
            raise RuntimeError("Library snapshot differs from library.")

        # V2 has already proved exact source coverage; original tags are not authority.
        provenance = _quarantine_completion_gate(record, ledger)
        albums = defaultdict(list, _completed_payload_groups(record, ledger))

        for relative in (() if albums else staged):
            if Path(relative).suffix.lower() not in (
                constants.music_file_types
            ):
                continue

            track = tag.read_track_metadata(held / relative)

            if track is None:
                raise RuntimeError(f"Unreadable audio: {relative}")

            names = {
                value.strip()
                for value in track["album"]
                if value.strip()
            }

            if len(names) != 1:
                raise RuntimeError(
                    f"Ambiguous album metadata: {relative}"
                )

            album_key = " ".join(
                next(iter(names)).casefold().split()
            )
            albums[album_key].append(relative)

        if not albums:
            raise RuntimeError(f"No validated albums: {name}")

        for album_key, tracks in albums.items():
            entry = _payload_completion_entry(ledger, name, tracks, staged, provenance)

            receipt = entry.get("receipt")

            if (
                not isinstance(receipt, dict)
                or len(receipt) < len(tracks) + 1
            ):
                raise RuntimeError(
                    f"Incomplete receipt: {album_key}"
                )

            for destination, digest in receipt.items():
                path = Path(destination)

                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(
                        f"Missing committed file: {destination}"
                    )

                relative = path.resolve().relative_to(
                    library
                ).as_posix()

                if (
                    live_files.get(relative) != digest
                    or saved_files.get(relative) != digest
                ):
                    raise RuntimeError(
                        f"Receipt mismatch: {relative}"
                    )

        ready.append({
            "name": name,
            "folder": folder,
            "files": len(staged),
            "albums": len(albums)
        })

        print(
            f"PURGE READY: {name} "
            f"({len(albums)} albums, {len(staged)} files)"
        )

    print("\nVERIFICATION ONLY: Nothing deleted.")
    return ready

@_serialized
def purge_verified_archived_quarantine(approved=False):
    """Purge archive-backed staging duplicates with restart recovery."""
    import os
    import shutil
    import tempfile
    import zipfile

    backup = Path(sort.script_folder) / constants.backup_folder_name
    root = backup / ".cleanup-quarantine"

    if backup.is_symlink() or root.is_symlink():
        raise RuntimeError("Unsafe backup path.")

    backup = backup.resolve()
    root = root.resolve()

    if not root.exists():
        return []

    if not root.is_dir():
        raise RuntimeError("Invalid quarantine directory.")

    def sync_dir(path):
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save_json(path, data):
        fd, temporary = tempfile.mkstemp(
            prefix=".mixie-write-", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_dir(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    folders = sorted(
        path for path in root.iterdir()
        if path.name.startswith(".mixie-archived-")
    )

    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise RuntimeError(f"Unsafe quarantine: {folder}")

    interrupted = [
        folder for folder in folders
        if (folder / "purge-state.json").exists()
        or (folder / "purge-state.json").is_symlink()
    ]

    if interrupted:
        selected = interrupted
        print("Interrupted purge found. Recovery verification required.")
    else:
        ready = verify_archived_purge_candidates()
        selected = [item["folder"] for item in ready]

    if not approved:
        print(f"Purge preview: {len(selected)} import(s)")
        print("Nothing deleted.")
        return selected

    completed = []

    for folder in selected:
        journal = folder / "recovery.json"
        state_path = folder / "purge-state.json"
        held = folder / "staging"
        resuming = state_path.exists()

        if (
            journal.is_symlink()
            or state_path.is_symlink()
            or held.is_symlink()
            or not journal.is_file()
        ):
            raise RuntimeError(f"Unsafe purge transaction: {folder}")

        allowed = {"recovery.json", "purge-state.json"}
        if held.exists():
            allowed.add("staging")

        if {p.name for p in folder.iterdir()} - allowed:
            raise RuntimeError("Unexpected files in purge transaction.")

        raw_journal = journal.read_bytes()
        journal_hash = hashlib.sha256(raw_journal).hexdigest()
        record = json.loads(raw_journal)
        provenance = sort.validate_cleanup_provenance(record)

        name = record.get("import_name")

        if (
            record.get("version") != 1
            or not isinstance(name, str)
            or name in ("", ".", "..")
            or Path(name).name != name
            or "/" in name or "\\" in name
            or not isinstance(record.get("staged_hashes"), dict)
            or not record["staged_hashes"]
        ):
            raise RuntimeError("Invalid purge recovery journal.")

        stage_path = Path(sort.staging_folder).resolve() / name

        if (
            record.get("original_staging_path") != str(stage_path)
            or stage_path.exists()
            or stage_path.is_symlink()
        ):
            raise RuntimeError(f"Live staging conflict: {name}")

        ledger_path = tag.script_folder / ".completed_reviews.json"

        if ledger_path.is_symlink() or not ledger_path.is_file():
            raise RuntimeError("Completion ledger unavailable.")

        ledger_hash = hashlib.sha256(
            ledger_path.read_bytes()
        ).hexdigest()

        if resuming:
            state = json.loads(state_path.read_text(encoding="utf-8"))

            if (
                state.get("version") != 1
                or state.get("journal_sha256") != journal_hash
                or state.get("ledger_sha256") != ledger_hash
            ):
                raise RuntimeError(
                    f"Purge recovery evidence changed: {name}"
                )
        else:
            if not held.is_dir():
                raise RuntimeError("Staged quarantine missing.")

            if sort.file_manifest(held) != record["staged_hashes"]:
                raise RuntimeError("Staged checksum mismatch.")

            state = {
                "version": 1,
                "journal_sha256": journal_hash,
                "ledger_sha256": ledger_hash
            }

            # Persist recovery instructions BEFORE removing any file.
            save_json(state_path, state)

        # Independently verify recoverability from the original ZIP.
        bulk = Path(record["original_archive"])
        source_name = record["archived_source"]

        if (
            bulk.is_symlink()
            or bulk.resolve() != backup / "OriginalDownloads-Bulk.zip"
            or not _safe_component(source_name)
            or (provenance is None and source_name != f"{name}.zip")
        ):
            raise RuntimeError("Unsafe archived-original reference.")

        outer_manifest = sort.zip_manifest(bulk)

        if outer_manifest.get(source_name) != (
            record["archived_source_hash"]
        ):
            raise RuntimeError("Archived original checksum mismatch.")

        with zipfile.ZipFile(bulk) as archive:
            with tempfile.TemporaryFile() as temporary:
                with archive.open(source_name) as source:
                    shutil.copyfileobj(source, temporary)
                temporary.seek(0)
                original_files = sort.zip_manifest(temporary)

        expected = record["staged_hashes"]

        if original_files != expected:
            raise RuntimeError("Original archive is not recoverable.")

        # The finished library and its independent backup must agree.
        snapshot = Path(record["library_snapshot"])

        if (
            snapshot.is_symlink()
            or snapshot.resolve().parent != backup
            or not snapshot.name.startswith("MusicLibrary-")
            or snapshot.suffix.lower() != ".zip"
        ):
            raise RuntimeError("Unsafe library backup reference.")

        if sort.zip_manifest(snapshot) != sort.file_manifest(
            sort.music_library_folder
        ):
            raise RuntimeError("Finished library backup mismatch.")

        _quarantine_completion_gate(record, load_completed_reviews())
        if held.exists():
            if not held.is_dir():
                raise RuntimeError("Invalid remaining staging files.")

            remaining = sort.file_manifest(held)

            if any(
                expected.get(relative) != digest
                for relative, digest in remaining.items()
            ):
                raise RuntimeError("Unexpected or changed staging file.")
        else:
            if not resuming:
                raise RuntimeError("Missing staging before purge.")
            remaining = {}

        # Only delete filenames present in the verified journal.
        for relative in sorted(remaining):
            target = held / relative

            if (
                target.is_symlink()
                or not target.is_file()
                or sort.file_checksum(target).hex()
                    != expected[relative]
            ):
                raise RuntimeError(f"Changed file: {relative}")

            target.unlink()

        if held.exists():
            for directory in sorted(
                (p for p in held.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True
            ):
                if directory.is_symlink():
                    raise RuntimeError("Unexpected directory symlink.")
                directory.rmdir()

            held.rmdir()

        sync_dir(folder)

        # Preserve an audit receipt outside the deleted quarantine.
        receipt_dir = backup / ".cleanup-purge-receipts"

        if receipt_dir.is_symlink():
            raise RuntimeError("Unsafe purge receipt directory.")

        receipt_dir.mkdir(exist_ok=True)

        receipt_path = receipt_dir / f"{journal_hash}.json"
        receipt = {
            "version": 1,
            "import_name": name,
            "original_journal": record,
            "purge_state": state
        }

        if receipt_path.exists():
            if receipt_path.is_symlink():
                raise RuntimeError("Unsafe existing purge receipt.")
            with receipt_path.open(encoding="utf-8") as stream:
                if json.load(stream) != receipt:
                    raise RuntimeError("Conflicting purge receipt.")
        else:
            save_json(receipt_path, receipt)


        # Atomically move the completed transaction into the
        # permanent evidence directory. Keep its small journals.
        # A crash before this rename leaves a resumable purge;
        # a crash afterward leaves a completed audit record.
        if held.exists() or held.is_symlink():
            raise RuntimeError(
                "Cannot finalize purge while staged files remain."
            )

        evidence = receipt_dir / f"{journal_hash}.evidence"

        if evidence.exists() or evidence.is_symlink():
            raise RuntimeError(
                f"Conflicting completed purge evidence: {evidence}"
            )

        folder.rename(evidence)
        sync_dir(root)
        sync_dir(receipt_dir)


        completed.append(name)
        print(f"PURGED: {name} ({len(expected)} files)")

    return completed

def verify_inbox_purge_candidates():
    """Audit both quarantined copies before permanent Inbox cleanup."""
    from collections import defaultdict
    import constants

    backup = (
        Path(sort.script_folder)
        / constants.backup_folder_name
    )
    root = backup / ".cleanup-quarantine"

    if root.is_symlink():
        raise RuntimeError("Unsafe quarantine root.")

    if not root.exists():
        print("No Inbox-backed quarantines.")
        return []

    if not root.is_dir():
        raise RuntimeError("Invalid quarantine root.")

    folders = sorted(
        p for p in root.iterdir()
        if p.name.startswith(".mixie-inbox-")
    )

    if not folders:
        print("No Inbox-backed quarantines.")
        return []

    library = Path(sort.music_library_folder).resolve()
    live = sort.file_manifest(library)

    # Require a current, verified backup of the entire library.
    snapshots = sorted(
        backup.glob("MusicLibrary-*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    matching_snapshot = None
    for snapshot in snapshots:
        if snapshot.is_symlink() or not snapshot.is_file():
            continue
        if sort.zip_manifest(snapshot) == live:
            matching_snapshot = snapshot
            break

    if matching_snapshot is None:
        raise RuntimeError("No matching Music Library backup.")

    bulk = backup / "OriginalDownloads-Bulk.zip"
    if bulk.is_symlink() or not bulk.is_file():
        raise RuntimeError("Original download archive unavailable.")

    archived = sort.zip_manifest(bulk)
    ledger = load_completed_reviews()
    pending = {
        issue["import_name"] for issue in load_review_queue()
    }

    ready = []

    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise RuntimeError("Unsafe quarantine folder.")

        if {p.name for p in folder.iterdir()} != {
            "recovery.json", "staging", "original"
        }:
            raise RuntimeError(
                f"Incomplete Inbox quarantine: {folder}"
            )

        journal = folder / "recovery.json"
        held_stage = folder / "staging"
        held_source = folder / "original"

        if any(p.is_symlink() for p in (
            journal, held_stage, held_source
        )):
            raise RuntimeError("Unsafe quarantine contents.")

        record = json.loads(journal.read_text(encoding="utf-8"))
        provenance = sort.validate_cleanup_provenance(record)

        name = record.get("import_name")
        source_name = record.get("source_name")

        def safe_name(value):
            return (
                isinstance(value, str)
                and value not in ("", ".", "..")
                and Path(value).name == value
                and "/" not in value
                and "\\" not in value
            )

        if (
            record.get("version") != 1
            or record.get("kind") != "inbox_cleanup"
            or not safe_name(name)
            or not safe_name(source_name)
            or type(record.get("source_is_dir")) is not bool
            or not isinstance(record.get("staged_hashes"), dict)
            or not isinstance(record.get("source_hashes"), dict)
            or not record["staged_hashes"]
        ):
            raise RuntimeError("Invalid Inbox recovery journal.")

        if name in pending:
            raise RuntimeError(f"Unresolved reviews: {name}")

        if (
            (Path(sort.staging_folder) / name).exists()
            or (Path(sort.inbox_folder) / source_name).exists()
        ):
            raise RuntimeError(f"Live import still exists: {name}")

        staged = sort.file_manifest(held_stage)
        if staged != record["staged_hashes"]:
            raise RuntimeError(f"Staged file mismatch: {name}")

        if record["source_is_dir"]:
            source_files = sort.file_manifest(held_source)
            prefix = source_name + "/"
            archived_source = {
                relative[len(prefix):]: digest
                for relative, digest in archived.items()
                if relative.startswith(prefix)
            }
        else:
            if not held_source.is_file():
                raise RuntimeError("Missing original download.")

            source_digest = sort.file_checksum(held_source).hex()

            if source_digest != record.get("source_sha256"):
                raise RuntimeError("Original download checksum mismatch.")

            if source_name.lower().endswith(".zip"):
                source_files = sort.zip_manifest(held_source)
            else:
                source_files = {source_name: source_digest}

            archived_source = source_files if (
                archived.get(source_name) == source_digest
            ) else {}

        if (
            source_files != record["source_hashes"]
            or archived_source != source_files
        ):
            raise RuntimeError(
                f"Original archive contents mismatch: {name}"
            )

        # V2 has already proved exact source coverage; original tags are not authority.
        provenance = _quarantine_completion_gate(record, ledger)
        albums = defaultdict(list, _completed_payload_groups(record, ledger))

        for relative in (() if albums else staged):
            if Path(relative).suffix.lower() not in (
                constants.music_file_types
            ):
                continue

            track = tag.read_track_metadata(
                held_stage / relative
            )
            if track is None:
                raise RuntimeError(f"Unreadable audio: {relative}")

            names = {
                value.strip()
                for value in track["album"]
                if value.strip()
            }
            if len(names) != 1:
                raise RuntimeError(
                    f"Ambiguous album metadata: {relative}"
                )

            key = " ".join(
                next(iter(names)).casefold().split()
            )
            albums[key].append(relative)

        if not albums:
            raise RuntimeError(f"No completed audio: {name}")

        for album_key, tracks in albums.items():
            entry = _payload_completion_entry(ledger, name, tracks, staged, provenance)

            receipt = entry.get("receipt")

            if (
                not isinstance(receipt, dict)
                or len(receipt) < len(tracks)
            ):
                raise RuntimeError(
                    f"Incomplete album receipt: {album_key}"
                )

            for destination, digest in receipt.items():
                path = Path(destination)
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(
                        f"Missing library file: {destination}"
                    )

                relative = path.resolve().relative_to(
                    library
                ).as_posix()

                if live.get(relative) != digest:
                    raise RuntimeError(
                        f"Library receipt mismatch: {relative}"
                    )

        ready.append({
            "name": name,
            "folder": folder,
            "albums": len(albums),
            "files": len(staged),
            "source_name": source_name
        })

        print(
            f"INBOX PURGE READY: {name} "
            f"({len(albums)} albums, {len(staged)} staged files)"
        )

    print(f"\nVerified Inbox purge candidates: {len(ready)}")
    print("VERIFICATION ONLY: Nothing deleted.")
    return ready

@_serialized
def purge_verified_inbox_quarantine(approved=False):
    """Purge verified Inbox/staging duplicates; resume interrupted purges."""
    import os
    import shutil
    import tempfile
    import zipfile
    import constants

    backup = Path(sort.script_folder) / constants.backup_folder_name
    root = backup / ".cleanup-quarantine"

    if backup.is_symlink() or root.is_symlink():
        raise RuntimeError("Unsafe backup or quarantine path.")

    if not root.exists():
        return []

    backup = backup.resolve()
    root = root.resolve()

    if not root.is_dir():
        raise RuntimeError("Invalid quarantine directory.")

    def sync_dir(path):
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save_json(path, value):
        fd, temporary = tempfile.mkstemp(
            prefix=".mixie-purge-", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_dir(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def safe_name(value):
        return (
            isinstance(value, str)
            and value not in ("", ".", "..")
            and Path(value).name == value
            and "/" not in value
            and "\\" not in value
        )

    folders = sorted(
        p for p in root.iterdir()
        if p.name.startswith(".mixie-inbox-")
    )

    seen_names = set()

    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise RuntimeError(f"Unsafe quarantine: {folder}")

        journal = folder / "recovery.json"

        if journal.is_symlink() or not journal.is_file():
            raise RuntimeError(f"Missing cleanup journal: {folder}")

        record = json.loads(journal.read_text(encoding="utf-8"))
        name = record.get("import_name")

        if not safe_name(name) or name in seen_names:
            raise RuntimeError("Unsafe or duplicate cleanup identity.")

        seen_names.add(name)

    interrupted = [
        p for p in folders
        if (p / "purge-state.json").exists()
        or (p / "purge-state.json").is_symlink()
    ]

    if interrupted:
        selected = interrupted
        print("Interrupted Inbox purge detected.")
    else:
        ready = verify_inbox_purge_candidates()
        selected = [item["folder"] for item in ready]

    if not approved:
        print(f"Inbox purge preview: {len(selected)} import(s)")
        print("Nothing deleted.")
        return selected

    purged = []

    for folder in selected:
        if folder.parent != root:
            raise RuntimeError("Unsafe purge candidate path.")

        journal = folder / "recovery.json"
        state_path = folder / "purge-state.json"
        held_stage = folder / "staging"
        held_source = folder / "original"

        if any(p.is_symlink() for p in (
            journal, state_path, held_stage, held_source
        )):
            raise RuntimeError("Unsafe purge transaction.")

        raw = journal.read_bytes()
        journal_hash = hashlib.sha256(raw).hexdigest()
        record = json.loads(raw)
        provenance = sort.validate_cleanup_provenance(record)

        name = record.get("import_name")
        source_name = record.get("source_name")
        source_is_dir = record.get("source_is_dir")

        expected_stage = record.get("staged_hashes")
        expected_source = record.get("source_hashes")

        if (
            record.get("version") != 1
            or record.get("kind") != "inbox_cleanup"
            or not safe_name(name)
            or not safe_name(source_name)
            or type(source_is_dir) is not bool
            or not isinstance(expected_stage, dict)
            or not isinstance(expected_source, dict)
            or not expected_stage
            or expected_stage != expected_source
        ):
            raise RuntimeError(f"Invalid purge evidence: {folder}")

        # Both copies must represent exactly the same downloaded files.
        live_stage = Path(sort.staging_folder) / name
        live_source = Path(sort.inbox_folder) / source_name

        if any(
            p.exists() or p.is_symlink()
            for p in (live_stage, live_source)
        ):
            raise RuntimeError(f"Live import conflict: {name}")

        ledger_path = tag.script_folder / ".completed_reviews.json"

        if ledger_path.is_symlink() or not ledger_path.is_file():
            raise RuntimeError("Completion ledger unavailable.")

        ledger_hash = hashlib.sha256(
            ledger_path.read_bytes()
        ).hexdigest()

        resuming = state_path.exists()

        if resuming:
            state = json.loads(
                state_path.read_text(encoding="utf-8")
            )

            if (
                state.get("version") != 1
                or state.get("journal_sha256") != journal_hash
                or state.get("ledger_sha256") != ledger_hash
            ):
                raise RuntimeError(
                    f"Interrupted purge evidence changed: {name}"
                )
        else:
            state = None

        # Verify the archived original independently of quarantined files.
        bulk = backup / "OriginalDownloads-Bulk.zip"

        if bulk.is_symlink() or not bulk.is_file():
            raise RuntimeError("Original download archive missing.")

        bulk_hash = sort.file_checksum(bulk).hex()
        archived = sort.zip_manifest(bulk)

        if source_is_dir:
            prefix = source_name + "/"

            archived_source = {
                key[len(prefix):]: digest
                for key, digest in archived.items()
                if key.startswith(prefix)
            }

            if archived_source != expected_source:
                raise RuntimeError(
                    f"Original folder backup mismatch: {name}"
                )
        else:
            source_hash = record.get("source_sha256")

            if archived.get(source_name) != source_hash:
                raise RuntimeError(
                    f"Original download backup mismatch: {name}"
                )

            if source_name.lower().endswith(".zip"):
                with zipfile.ZipFile(bulk) as outer:
                    with tempfile.TemporaryFile() as temporary:
                        with outer.open(source_name) as stream:
                            shutil.copyfileobj(stream, temporary)
                        temporary.seek(0)

                        if sort.zip_manifest(temporary) != expected_source:
                            raise RuntimeError(
                                f"Original ZIP contents mismatch: {name}"
                            )
            elif expected_source != {source_name: source_hash}:
                raise RuntimeError(
                    f"Loose original file mismatch: {name}"
                )

        # Require an exact backup of the finished Music Library.
        library_files = sort.file_manifest(
            sort.music_library_folder
        )

        if resuming:
            if not isinstance(state, dict):
                raise RuntimeError("Invalid saved purge state.")
            snapshot = Path(state["library_snapshot"])

            if (
                snapshot.is_symlink()
                or snapshot.parent != backup
                or not snapshot.name.startswith("MusicLibrary-")
                or snapshot.suffix.lower() != ".zip"
                or not snapshot.is_file()
            ):
                raise RuntimeError("Unsafe saved library snapshot.")

            if sort.zip_manifest(snapshot) != library_files:
                raise RuntimeError("Library backup changed.")

            if (
                state.get("bulk_sha256") != bulk_hash
                or state.get("snapshot_sha256")
                    != sort.file_checksum(snapshot).hex()
            ):
                raise RuntimeError(
                    "Backup evidence changed during purge."
                )
        else:
            snapshot = None

            for candidate in sorted(
                backup.glob("MusicLibrary-*.zip")
            ):
                if candidate.is_symlink() or not candidate.is_file():
                    continue

                if sort.zip_manifest(candidate) == library_files:
                    snapshot = candidate
                    break

            if snapshot is None:
                raise RuntimeError(
                    "No verified Music Library snapshot."
                )

        def remaining_files(path, expected, must_be_dir):
            if not path.exists():
                if not resuming:
                    raise RuntimeError(
                        f"Missing copy before purge: {path}"
                    )
                return {}

            if path.is_symlink():
                raise RuntimeError(f"Unsafe symlink: {path}")

            if must_be_dir:
                if not path.is_dir():
                    raise RuntimeError(f"Expected directory: {path}")
                current = sort.file_manifest(path)
            else:
                if not path.is_file():
                    raise RuntimeError(f"Expected file: {path}")
                current = {
                    source_name: sort.file_checksum(path).hex()
                }

            if any(
                expected.get(relative) != digest
                for relative, digest in current.items()
            ):
                raise RuntimeError(
                    f"Unrecognized or changed file: {path}"
                )

            if not resuming and current != expected:
                raise RuntimeError(f"Incomplete original copy: {path}")

            return current

        _quarantine_completion_gate(record, load_completed_reviews())
        staged_files = remaining_files(
            held_stage, expected_stage, True
        )
        original_files = remaining_files(
            held_source,
            expected_source if source_is_dir else {
                source_name: record.get("source_sha256")
            },
            source_is_dir
        )

        if not resuming:
            # This state must be durable before the first deletion.
            state = {
                "version": 1,
                "journal_sha256": journal_hash,
                "ledger_sha256": ledger_hash,
                "bulk_sha256": bulk_hash,
                "snapshot_sha256":
                    sort.file_checksum(snapshot).hex(),
                "library_snapshot": str(snapshot)
            }

        receipt_dir = backup / ".cleanup-purge-receipts"

        if receipt_dir.is_symlink():
            raise RuntimeError("Unsafe receipt directory.")

        receipt_dir.mkdir(exist_ok=True)

        evidence = receipt_dir / f"{journal_hash}.evidence"
        receipt_path = receipt_dir / f"{journal_hash}.json"

        if evidence.exists() or evidence.is_symlink():
            raise RuntimeError("Conflicting completed purge evidence.")

        receipt = {
            "version": 1,
            "import_name": name,
            "original_journal": record,
            "purge_state": state
        }

        if receipt_path.exists() or receipt_path.is_symlink():
            if receipt_path.is_symlink():
                raise RuntimeError("Unsafe purge receipt.")

            if json.loads(
                receipt_path.read_text(encoding="utf-8")
            ) != receipt:
                raise RuntimeError("Conflicting purge receipt.")

        if not resuming:
            save_json(state_path, state)

        def remove_tree(path, expected):
            if not path.exists():
                return

            actual = sort.file_manifest(path)

            if any(
                expected.get(relative) != digest
                for relative, digest in actual.items()
            ):
                raise RuntimeError("Unexpected file during deletion.")

            for relative in sorted(actual):
                target = path / relative

                if (
                    target.is_symlink()
                    or not target.is_file()
                    or sort.file_checksum(target).hex()
                        != expected[relative]
                ):
                    raise RuntimeError(
                        f"Changed file during purge: {relative}"
                    )

                target.unlink()

            directories = sorted(
                (p for p in path.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True
            )

            for directory in directories:
                if directory.is_symlink():
                    raise RuntimeError("Unsafe directory symlink.")
                directory.rmdir()

            path.rmdir()
            sync_dir(path.parent)

        # Delete only verified quarantine copies, never live music.
        remove_tree(held_stage, expected_stage)

        if source_is_dir:
            remove_tree(held_source, expected_source)
        elif held_source.exists():
            if (
                held_source.is_symlink()
                or not held_source.is_file()
                or sort.file_checksum(held_source).hex()
                    != record["source_sha256"]
            ):
                raise RuntimeError("Original download changed.")

            held_source.unlink()
            sync_dir(folder)

        if any(
            p.exists() or p.is_symlink()
            for p in (held_stage, held_source)
        ):
            raise RuntimeError("Quarantine copies remain.")

        if not receipt_path.exists():
            save_json(receipt_path, receipt)

        # Atomic finalization retains journals for future audits.
        folder.rename(evidence)
        sync_dir(root)
        sync_dir(receipt_dir)

        purged.append(name)
        print(f"INBOX PURGED: {name}")

    return purged

def auto_purge_verified_quarantines(*, allow_source_cleanup=False):
    """Purge completed imports only when source cleanup was explicitly authorized."""
    if type(allow_source_cleanup) is not bool:
        raise ValueError('Source cleanup authorization must be an explicit boolean.')
    purged = CleanupResults()
    if not allow_source_cleanup:
        return purged

    for label, operation in (
        ("Archive-backed", purge_verified_archived_quarantine),
        ("Inbox-backed", purge_verified_inbox_quarantine),
    ):
        try:
            results = operation(approved=True)
            purged.extend(results)

            if results:
                print(
                    f"{label} housekeeping complete: "
                    f"{len(results)} import(s)."
                )

        except Exception as error:
            purged.errors.append(str(error))
            print(
                f"{label} permanent cleanup deferred: {error}"
            )
            print(
                "Unverified files and recovery records "
                "have not been approved for removal."
            )

    return purged

def finish_prior_cleanup_before_ingest(*, allow_source_cleanup=False):
    """Resolve old quarantine BEFORE changing the bulk or library backups.

    This keeps restart journals compatible with the original immutable
    completion ledger and archive hashes needed for safe recovery.
    """
    if _import_first_enabled():
        import mixie_finalizer
        mixie_finalizer.resume_cleanup(allow_source_cleanup=allow_source_cleanup)
        if mixie_finalizer._state()['active'] is not None:
            raise RuntimeError('Protected finalization recovery is pending; source cleanup requires explicit authorization.')
    backup = Path(sort.script_folder) / constants.backup_folder_name
    if backup.is_symlink():
        raise RuntimeError("Backup directory is a symbolic link.")
    root = backup / ".cleanup-quarantine"
    if root.is_symlink():
        raise RuntimeError("Quarantine root is a symbolic link.")
    if not root.exists():
        return
    if not root.is_dir():
        raise RuntimeError("Invalid quarantine root.")
    def outstanding():
        return sorted(
            p for p in root.iterdir()
            if p.name.startswith((".mixie-inbox-", ".mixie-archived-"))
        )
    resumed = CleanupResults()
    if outstanding():
        print("Finishing previous verified cleanup before new imports...")
        resumed = auto_purge_verified_quarantines(allow_source_cleanup=allow_source_cleanup)
    if outstanding():
        raise RuntimeError(
            "Unfinished cleanup recovery remains; original evidence must "
            "be reconciled before starting another Ingest."
        )
    return resumed


def get_acoustid_client_key():
    """Use an explicit session key, otherwise a protected persistent key."""
    key = os.environ.get("ACOUSTID_CLIENT_KEY", "").strip()
    if key:
        return key

    saved = Path.home() / ".config" / "mixie" / "acoustid_key"
    if saved.is_symlink():
        raise RuntimeError("Refusing symlinked AcoustID configuration.")
    if not saved.exists():
        return None
    if not saved.is_file():
        raise RuntimeError("AcoustID configuration is not a regular file.")
    if saved.stat().st_mode & 0o077:
        raise PermissionError("AcoustID key must have 0600 permissions.")
    return saved.read_text(encoding="utf-8").strip() or None


def review_manager_menu(issues):
    import mixie_review_ui
    return mixie_review_ui.review_manager_menu(sys.modules[__name__], issues)


def _main(selection=None, *, allow_source_cleanup=False):
    args = [argument for argument in sys.argv[1:] if argument != '--allow-source-cleanup']
    args = [] if args == ['--auto-ingest'] else args
    already_verified = error_count = recorded_count = 0
    recorded_keys = set()
    cleaned, purged, latest_purged = [], [], []
    preflight_key = None
    target = None
    test_copies = "--test-copy" in args
    commit = "--commit" in args
    auto_ingest = False
    explicitly_authorized_cleanup = allow_source_cleanup

    # Open the interactive menu when no arguments are supplied
    if not args:
        choice = selection if selection is not None else _main_choice()

        if choice == "1":
            auto_ingest = True
            # Auto-Ingest explicitly includes archive-backed source retirement.
            # The finalizer is the only destructive authority: it requires
            # verified original-delivery and corrected-library ZIPs, complete
            # physical source coverage, durable receipts and resumable
            # quarantine before removing anything. Unresolved shared delivery
            # siblings remain untouched, even if other albums are finalized.
            allow_source_cleanup = True
            print("\nAutomatically ingesting verified music...")
            print("  Archive-backed cleanup enabled for fully completed deliveries.")
            try:
                preflight_key = get_acoustid_client_key()
                if not preflight_key:
                    # Historical explicit --allow-source-cleanup can still recover
                    # a previously journaled transaction without an online key.
                    # Merely choosing Auto-Ingest never starts cleanup in this case.
                    if not explicitly_authorized_cleanup:
                        raise ValueError("AcoustID client key is not configured; staging, identification and automatic cleanup were not started.")
                resumed = finish_prior_cleanup_before_ingest(allow_source_cleanup=allow_source_cleanup) or []
                purged.extend(resumed)
                error_count += len(getattr(resumed, "errors", []))
                if not preflight_key:
                    raise ValueError("AcoustID client key is not configured; staging and identification were not started.")
                with terminal.compact_operation("Checking and staging Inbox", root=tag.script_folder):
                    sort.sort_inbox()
            except Exception as error:
                print(f"Ingest stopped before staging: {error}")
                _ingest_report(errors=error_count + 1, purged=purged)
                return
        elif choice == "2":
            print("\nAuditing Music Library...")
            tag.main(tag.library_folder)
            return
        elif choice == "3":
            print("\nDevice synchronization is not connected yet.")
            print("No files have been changed.")
            return
        elif choice == "4":
            try:
                pending = load_review_queue()
                current = _retire_absent_review_issues(pending)
                if len(current) != len(pending):
                    save_review_queue(current)
                review_manager_menu(current)
            except (OSError, ValueError) as error:
                print(f"\nCannot load review queue: {error}")
            return
        elif choice == "5" and _import_first_enabled():
            print('\nVerifying an optional Music Library ZIP backup...')
            try:
                with terminal.compact_operation('Verifying optional library ZIP', root=tag.script_folder):
                    saved = archive.create_verified_zip(sort.music_library_folder, 'MusicLibrary')
                print(f'Verified library ZIP: {saved}')
                print('This operation does not delete original downloads or staging files.')
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
                print(f'Optional library ZIP deferred: {error}')
            return
        elif choice == ("6" if _import_first_enabled() else "5"):
            print("\nGoodbye!")
            return

        else:
            print("\nInvalid option.")
            return

    elif args in (['--retry-pending'], ['--check-pending']):
        if args == ['--check-pending'] and allow_source_cleanup:
            raise ValueError("A pending-album dry check cannot authorize source cleanup.")
        return retry_pending_albums(preview_only=(args == ['--check-pending']),
                                    allow_source_cleanup=allow_source_cleanup)

    elif args == ['--finalize-verified']:
        return finalize_verified_imports(allow_source_cleanup=allow_source_cleanup)

    # Preserve our existing command-line testing options
    elif "--stage-only" in args:
        sort.sort_inbox()
        return
    else:
        target = next(
            (arg.casefold() for arg in args if not arg.startswith("--")),
            None
        )

    # Identify and preview staged albums using the already validated session key.
    api_key = preflight_key if auto_ingest else get_acoustid_client_key()
    if not api_key:
        print("AcoustID client key is not configured.")
        return
    albums = tag.group_staged_albums()
    if auto_ingest:
        prior = load_review_queue()
        current = _retire_absent_review_issues(prior)
        if len(current) != len(prior):
            save_review_queue(current)
    if auto_ingest:
        # Refresh only receipts for albums actually present. Old/changed source
        # and output claims are saved to immutable history, never silently
        # treated as a new Manual Review requirement or cleanup authorization.
        _refresh_staged_import_receipts(albums)
    # A stage-level duplicate index is built BEFORE any online identification.
    # Stream identity is mandatory; album names are only a cheap candidate filter.
    duplicate_index = None
    duplicate_plans = {}
    if auto_ingest and _import_first_enabled() and len(albums) > 1:
        import mixie_batch_duplicates
        duplicate_index = mixie_batch_duplicates.BatchDuplicates(albums)
        if duplicate_index.representative:
            print(f"  Early duplicate detection: {len(duplicate_index.representative)} "
                  "matching album delivery(s); online scans will be reused.")
    # Keep exact duplicate deliveries next to their owner so a verified scan is
    # reused immediately rather than scanning unrelated albums first.
    ordered_albums = (mixie_batch_duplicates.ordered_deliveries(albums, duplicate_index)
                      if duplicate_index is not None else list(albums.items()))
    unique_count = len(albums) - (len(duplicate_index.representative) if duplicate_index else 0)
    if auto_ingest and duplicate_index is not None:
        print(terminal.color(f'  Workload: {unique_count} unique album scans; '
                             f'{len(duplicate_index.representative)} exact-audio duplicate deliveries.', 'gold'))
    if commit:
        matches = [
            album for album in albums.values()
            if target and album["album"].casefold() == target
        ]

        if len(matches) != 1:
            print("Commit requires exactly one named staged album.")
            return

    if not albums:
        print("No staged albums found.")
        if auto_ingest:
            print("Checking previously verified cleanup transactions...")
            resumed = auto_purge_verified_quarantines(allow_source_cleanup=allow_source_cleanup)
            purged.extend(resumed)
            _ingest_report(errors=error_count + len(getattr(resumed, "errors", [])), purged=purged)
        return

    completed = {}
    review_queue = []
    interrupted = False
    pending_issues = (
        load_review_queue() if auto_ingest else []
    )
    completed_entries = (
        load_completed_reviews() if auto_ingest else {}
    )


    duplicate_owners = ({child[0]: owner[0] for child, owner in duplicate_index.representative.items()}
                        if duplicate_index is not None else {})
    # Before any import or archive/cleanup, protect previously saved human review
    # choices on an album that has just become a duplicate. Do not silently
    # discard or orphan those decisions when consolidating its review queue.
    if auto_ingest and duplicate_owners:
        for previous in pending_issues:
            if (previous.get('import_name') in duplicate_owners and
                    (any(previous.get(field) for field in
                         ('decisions', 'decision', 'manual_fields', 'artwork_decision',
                          'tag_edits', 'interpretation')) or
                     previous.get('review_status') == 'reviewed')):
                print('Duplicate has existing saved review choices. Consolidate those'
                      ' choices in Manual Review before this batch can be finalized.')
                return
    rescanned = set()
    unique_index = 0
    for album_index, (key, album) in enumerate(ordered_albums):
        if auto_ingest:
            owner = duplicate_index.source(key) if duplicate_index else None
            if owner is None:
                unique_index += 1
                print(terminal.color(f"\nAlbum {unique_index}/{unique_count}", "header"))
            else:
                print(terminal.color(f"\n  DUPLICATE DELIVERY  ·  {album['album']}", "gold"))
                print("  Verified encoded-audio match; no second online scan or duplicate review.")
        if target and album["album"].casefold() != target:
            continue
        if auto_ingest and not (Path(tag.staging_folder) / key[0]).is_dir():
            # An already finalized delivery may have disappeared during this
            # batch's archive/cleanup. Ignore a stale in-memory album snapshot;
            # never regenerate a review item for physically absent source data.
            print('  Source no longer staged; skipping outdated album snapshot.')
            continue
        if auto_ingest and owner is not None:
            # Do not create a second receipt or review for identical audio.
            # Full provenance/source checks still occur before any cleanup.
            duplicate_index.validated_mapping(key)
            print('  Grouped with first delivery; only the best representative is scanned.')
            continue

        evidence_fingerprint = None
        try:
            if auto_ingest:
                source_issue = {
                    "import_name": key[0],
                    "tracks": [
                        str(
                            Path(track["path"]).resolve()
                            .relative_to(tag.staging_folder.resolve())
                        )
                        for track in album["tracks"]
                    ]
                }

                if ((_import_first_enabled() and (
                            is_staged_album_already_imported(source_issue)
                            or is_library_import_verified(source_issue)))
                        or is_review_complete(source_issue, completed_entries)):
                    print(
                        f"Previously imported; identification skipped: "
                        f"{album['album']} (archive status checked at finalization)"
                    )
                    # A currently validated existing import is not unresolved
                    # metadata work, even if its older queue row says so.
                    rescanned.add((source_issue['import_name'], tuple(sorted(source_issue['tracks']))))
                    already_verified += 1
                    continue

            if auto_ingest:
                matched_pending = [entry for entry in pending_issues
                    if entry.get("import_name") == key[0]
                    and set(entry.get("tracks", ())) == set(_album_issue(album)["tracks"])]
                if len(matched_pending) == 1:
                    saved_issue = matched_pending[0]
                    _current, valid = _invalidate_stale_choices(saved_issue)
                    saved_interpretation = saved_issue.get("interpretation") or {}
                    has_approval = (saved_issue.get("review_status") == "reviewed"
                        or (saved_interpretation.get("status") == "pending_application"
                            and saved_interpretation.get("user_approved") is True))
                    if valid and has_approval:
                        # Older advanced approvals also belong in the Apply list.
                        saved_issue["review_status"] = "reviewed"
                        print(f"Saved review awaiting explicit Apply: {album['album']}")
                        review_queue.append(deepcopy(saved_issue))
                        continue
            # Bind diagnostics to the payload present BEFORE provider work.
            # A later error cannot authorize evidence for changed source bytes.
            evidence_fingerprint = review_fingerprint(_album_issue(album))
            # Automatic rechecks never replay human choices. Reconciliation keeps
            # unapproved drafts on failure; approved work is applied only by Review.
            reviewed = None
            approved_artwork = None
            approved_manual = None
            interpretation = None
            prior_key = duplicate_index.source(key) if duplicate_index else None
            mapping = (duplicate_index.validated_mapping(key)
                       if prior_key in duplicate_plans and not (reviewed or approved_artwork or approved_manual or interpretation)
                       else None)
            if mapping is not None:
                import mixie_batch_duplicates
                terminal.header(f"Scanning: {album['album']}", f"{len(album['tracks'])} tracks")
                print("  Exact duplicate audio verified; reusing prior album identification.")
                print("  Rechecking this delivery's metadata, artwork, backup and source manifest.")
                plan = mixie_batch_duplicates.reuse_verified_plan(
                    sys.modules[__name__], duplicate_plans[prior_key], album, mapping)
            else:
                plan = preview_album(
                    album, api_key, reviewed_decision=reviewed, artwork_decision=approved_artwork,
                    manual_fields=approved_manual, interpretation=interpretation,
                    auto_mode=auto_ingest
                )

            if test_copies or commit or auto_ingest:
                if auto_ingest:
                    with terminal.compact_operation(f"Verifying and importing {album['album']}", root=tag.script_folder):
                        receipt = test_album_plan(plan, album, commit=True, auto=True,
                                                  same_batch_receipts=tuple(completed.values()))
                else:
                    receipt = test_album_plan(plan, album, commit=commit, auto=False)

                if auto_ingest and receipt:
                    # Only VERIFIED successful imports supersede a prior review
                    # without a fresh failure diagnostic. A scan mock/no-op or
                    # interrupted commit must leave human drafts recoverable.
                    current_issue = _album_issue(album)
                    rescanned.add((current_issue['import_name'], tuple(sorted(current_issue['tracks']))))
                    if _import_first_enabled():
                        # Important ordering: prove the library first. ZIP creation
                        # never controls whether this import is successful.
                        record_library_only_import(_album_issue(album), receipt)
                        recorded_count += 1
                        recorded_keys.add(key)
                    terminal.show_confirmed_changes(plan.get("_verified_changes", []),
                                                    album=album['album'],
                                                    artist=(plan.get('_batch_common') or {}).get('album_artist'))
                    completed[key] = receipt
                    # An unmodified, independently verified plan can authorize
                    # same-run duplicate evidence reuse. The second delivery
                    # still receives an independent backup and commit receipt.
                    if (duplicate_index is not None and
                            plan.get('provenance') in ('independently_verified', 'independently_verified_partial')
                            and plan.get('interpretation') is None and plan.get('reviewed_decision') is None
                            and plan.get('artwork_decision') is None and not plan.get('manual_fields')):
                        duplicate_plans[key] = plan

        except (Exception, KeyboardInterrupt) as error:
            interrupted = isinstance(error, KeyboardInterrupt)
            print(f"\nNeeds review: {album['album']}")
            print("Reason: User interrupted; saving progress for a later retry."
                  if interrupted else f"Reason: {error}")

            details = {"kind": "interrupted"} if interrupted else {}

            if isinstance(error, (review.AlbumReviewRequired, ProviderUnavailable)):
                details = error.details
            elif isinstance(error, tag.MetadataConflictError):
                details = {
                    "kind": "metadata_conflict",
                    "field": error.field,
                    "existing_values": error.existing,
                    "reference_value": error.expected,
                    "track": Path(error.path).name,
                    "path": error.path
                }
                if getattr(error, "release_numbering", None):
                    # Keep the full candidate audit; the verified selection is
                    # source-bound numbering evidence, not identity approval.
                    snapshot = error.release_audit
                    details["tracks"] = snapshot["tracks"]
                    details["releases"] = snapshot["releases"]
                    details["verified_release_numbering"] = error.release_numbering

            elif isinstance(error, ArtworkReviewRequired):
                details = {
                    "kind": "artwork_conflict",
                    "candidates": error.candidates,
                    "release_ids": error.release_ids
                }
            elif isinstance(error, tag.RequiredMetadataReview):
                details = {
                    "kind": "required_metadata",
                    "fields": error.fields
                }
            elif not interrupted:
                provider_error = lookup.provider_error_evidence("musicbrainz", error)
                if provider_error["retryable"]:
                    details = {"kind": "provider_unavailable", "retryable": True,
                               "provider_errors": [provider_error]}
            if not interrupted and not isinstance(error, (ValueError, ArtworkReviewRequired)) and details.get("kind") != "provider_unavailable":
                error_count += 1

            details = deepcopy(details)
            stable = False
            if evidence_fingerprint is not None:
                try:
                    stable = review_fingerprint(_album_issue(album)) == evidence_fingerprint
                except (OSError, ValueError, RuntimeError):
                    pass
            if stable:
                # An existing pin (including None) belongs to the original
                # evidence; a failed retry cannot promote it to a new payload.
                details.setdefault('source_fingerprint', evidence_fingerprint)
            else:
                details['source_fingerprint'] = None
            review_queue.append({
                "import_name": key[0],
                "details": details,
                "album": album["album"],
                "status": "provider_unavailable" if details.get("kind") == "provider_unavailable" else "needs_review",
                "error_type": type(error).__name__,
                "reason": "User interrupted; retry later." if interrupted else str(error),
                "tracks": [
                    str(Path(track["path"]).relative_to(
                        tag.staging_folder
                    ))
                    for track in album["tracks"]
                ],
                **({'source_fingerprint': details['source_fingerprint']}
                   if details.get('source_fingerprint') else {})
            })
        # Checkpoint pending reviews before beginning the next album. The last
        # album is immediately followed by the existing batch-end atomic save.
        if auto_ingest and review_queue and (interrupted or album_index < len(ordered_albums) - 1):
            token = _review_checkpoint.set(True)
            try:
                # Save pending recovery data without repetitive full archive checks.
                save_review_queue(reconcile_review_queue(review_queue, duplicate_owners=duplicate_owners)
                                  if duplicate_owners else reconcile_review_queue(review_queue))
            finally:
                _review_checkpoint.reset(token)
        if interrupted:
            print("Ingestion interrupted. Completed review decisions remain saved.")
            break

    # Import-first plus mandatory verified ZIP archives and crash-safe source
    # cleanup. Never delete on a bare import-only receipt; the finalizer
    # verifies both ZIPs, their payloads, and the live library first.
    if auto_ingest and _import_first_enabled():
        import mixie_finalizer
        def _archive_progress(message):
            # compact_operation redirects stdout into the diagnostic log; keep
            # small live progress notices on stderr so large archives never
            # appear silently frozen. Strip terminal control characters.
            sys.stderr.write('  ' + terminal._safe(message) + '\n')
            sys.stderr.flush()

        with terminal.compact_operation('Archiving and finalizing verified albums', root=tag.script_folder):
            archived_count, finalization_errors = mixie_finalizer.finish_verified_albums(
                sys.modules[__name__], albums, duplicate_index,
                allow_source_cleanup=allow_source_cleanup, progress=_archive_progress)
        retention_suffix = ('Shared source still has unresolved album(s); '
                            'verified albums archived independently, full original and staging retained.')
        error_count += sum(not notice.endswith(retention_suffix) for notice in finalization_errors)
        for notice in finalization_errors:
            print(f'  Archive/cleanup deferred: {notice}')
        if archived_count:
            print(f'  Verified and archived {archived_count} album group(s).')
        if not allow_source_cleanup:
            print('  Source cleanup was not authorized; Inbox and staging remain intact.')
        # If interrupted, keep unprocessed older reviews; otherwise replace
        # diagnostics for every album this run actually rescanned.
        review_queue = (reconcile_review_queue(review_queue, duplicate_owners=duplicate_owners,
                                               rescanned=rescanned)
                        if duplicate_owners else reconcile_review_queue(review_queue, rescanned=rescanned))
        # Finalization may have physically purged a completed delivery after
        # the album snapshot was collected. Such absent source diagnostics are
        # historical recovery evidence, NOT fresh Manual Review tasks.
        review_queue = _retire_absent_review_issues(review_queue)
        review_path = save_review_queue(review_queue)
        print(f"\nReview queue saved: {len(review_queue)} album(s)")
        print(f"Review file: {review_path}")
        archive_pending = sum(_archive_pending(entry) for entry in review_queue)
        finalized_new = sum(mixie_finalizer.is_album_finalized(
            sys.modules[__name__], _album_issue(albums[key])) for key in recorded_keys)
        _ingest_report(finalized_new, already_verified,
                       len(review_queue) - archive_pending,
                       error_count, cleaned, purged,
                       verified_library=recorded_count - finalized_new,
                       provider_waiting=sum(entry.get('details', {}).get('kind') == 'provider_unavailable'
                                            for entry in review_queue))
        print('Library import successes are independently verified.')
        print('Completed albums require verified Originals/ and Library/ ZIPs before source cleanup.')
        if archive_pending:
            print(f'{archive_pending} import(s) await required album archive verification; no metadata reapproval needed.')
            print('Use Review Manager → Finish / Retry Required Archives, or --finalize-verified.')
        to_review = [entry for entry in review_queue
                     if entry.get('review_status') != 'reviewed' and not _archive_pending(entry)]
        if to_review:
            print(f"\nMIXIE: {len(to_review)} album(s) genuinely need your attention.")
            try:
                choice = input('Would you like to review them now? (y/N): ').strip().casefold()
            except (KeyboardInterrupt, EOFError):
                choice = 'n'
            if choice in ('y', 'yes'):
                review_manager_menu(review_queue)
        return

    # Legacy archive-backed ingestion is retained for unmarked installations
    # and the existing isolated safety/cleanup regression tests.
    if auto_ingest:
        print("\nFinalizing ingestion archives...")

        try:
            review_queue = reconcile_review_queue(review_queue)
            review_path = save_review_queue(review_queue)
            print(
                f"\nReview queue saved: "
                f"{len(review_queue)} album(s)"
            )
            print(f"Review file: {review_path}")
            # Always preserve original downloads, even when every album needs
            # review. Only the empty *library* archive is optional.
            library_root = Path(sort.music_library_folder)
            review_only_empty_library = (
                not completed and not already_verified
                and not any(p.is_file() for p in library_root.rglob("*"))
            )
            with terminal.compact_operation("Verifying ingestion archives", root=tag.script_folder):
                if review_only_empty_library:
                    try:
                        originals_zip = archive.update_original_downloads_archive()
                    except ValueError as archive_error:
                        # The only benign empty archive case.
                        if str(archive_error) != "No downloads to archive.":
                            raise
                        originals_zip = None
                    library_zip = None
                else:
                    originals_zip = archive.update_original_downloads_archive()
                    library_zip = archive.create_verified_zip(
                        sort.music_library_folder, "MusicLibrary"
                    )
            # This is an important review-only outcome, not low-level archive
            # diagnostics: keep it visible after the compact logging phase.
            if review_only_empty_library:
                print("No finished library files yet; library archive/cleanup deferred until verified import.")

            # Save completion evidence for automatically imported albums.
            # Record successfully imported albums after archive verification
            for key, receipt in completed.items():
                album = albums[key]

                completed_issue = {
                    "import_name": key[0],
                    "album": album["album"],
                    "tracks": [
                        Path(track["path"]).resolve()
                        .relative_to(tag.staging_folder.resolve())
                        .as_posix()
                        for track in album["tracks"]
                    ]
                }

                try:
                    record_completed_review(completed_issue, receipt, originals_zip, library_zip)
                    recorded_count += 1
                    recorded_keys.add(key)
                except Exception as error:
                    error_count += 1
                    review_queue.append({**completed_issue, "status": "needs_review", "details": {"kind": "completion"},
                                         "reason": str(error), "error_type": type(error).__name__})

            # Remove completed albums from pending reviews
            review_queue = reconcile_review_queue(review_queue)
            save_review_queue(review_queue)

            print(
                f"Pending after verified imports: "
                f"{len(review_queue)}"
            )

            # Clean completed imports using the verified ledger,
            # including albums finished during earlier sessions.
            if originals_zip is not None and library_zip is not None:
                with terminal.compact_operation("Verifying safe cleanup", root=tag.script_folder):
                    cleaned = auto_quarantine_archived_completed(
                        originals_zip, library_zip, allow_source_cleanup=allow_source_cleanup)
                    latest_purged = auto_purge_verified_quarantines(allow_source_cleanup=allow_source_cleanup)
                purged.extend(latest_purged)
                error_count += len(getattr(cleaned, "errors", [])) + len(getattr(latest_purged, "errors", []))

        except Exception as error:
            error_count += 1
            print(f"\nArchiving or cleanup deferred: {error}")
            print("Unfinished imports have not been approved for cleanup.")
            for key in completed:
                if key not in recorded_keys:
                    issue = _album_issue(albums[key])
                    stale=[entry for entry in review_queue if entry.get('import_name')==issue['import_name']
                           and set(entry.get('tracks',()))==set(issue['tracks'])]
                    latest={'status':'archive_pending','details':{'kind':'completion'},
                            'reason':'Library import verified; archive pending: '+str(error),
                            'error_type':type(error).__name__}
                    if stale:stale[0].update(latest)  # retain any human saved choices
                    else:review_queue.append({**issue,**latest})
            save_review_queue(review_queue)
        _ingest_report(recorded_count, already_verified,
                       sum(not _archive_pending(entry) for entry in review_queue), error_count, cleaned, purged,
                       awaiting_cleanup=len(set(cleaned) - set(latest_purged)),
                       verified_library=len([key for key in completed if key not in recorded_keys]),
                       provider_waiting=sum(issue.get("details", {}).get("kind") == "provider_unavailable" for issue in review_queue))

        # A deliberately reviewed batch is not another request for manual work.
        needing_attention = [entry for entry in review_queue
                             if entry.get("review_status") != "reviewed"
                             and not _archive_pending(entry)]
        archive_pending_count = sum(_archive_pending(entry) for entry in review_queue)
        if archive_pending_count:
            print(f"\nMIXIE: {archive_pending_count} album(s) imported but awaiting source archive verification; no metadata review needed.")
        reviewed_pending_count = sum(entry.get('review_status') == 'reviewed' and not _archive_pending(entry)
                                     for entry in review_queue)
        if reviewed_pending_count and not needing_attention:
            print(f"\nMIXIE: {reviewed_pending_count} reviewed album(s) are saved, not applied.")
            print("Use Review Manager → Apply Reviewed Albums when you decide to import them.")
        if needing_attention:
            print(
                f"\nMIXIE: {len(needing_attention)} album(s) "
                "need your attention."
            )

            try:
                choice = input(
                    "Would you like to review them now? (y/N): "
                ).strip().casefold()
            except (KeyboardInterrupt, EOFError):
                choice = "n"

            if choice in ("y", "yes"):
                review_manager_menu(review_queue)
            else:
                print("Review postponed. Files remain in staging.")

def _main_choice():
    terminal.header("Music Manager", "Offline-first music ingestion")
    if _import_first_enabled():
        print('1. Ingest Music\n2. Repair Library\n3. Sync Device\n4. Review Pending Albums\n5. Backup Music Library (Optional ZIP)\n6. Exit')
        return input('\nSelect an option (1-6): ').strip()
    print("1. Ingest Music\n2. Repair Library\n3. Sync Device\n4. Review Pending Albums\n5. Exit")
    return input("\nSelect an option (1-5): ").strip()


def main():
    arguments = [argument for argument in sys.argv[1:] if argument != '--allow-source-cleanup']
    allow_source_cleanup = '--allow-source-cleanup' in sys.argv[1:]
    auto = arguments == ['--auto-ingest']
    if arguments and not auto:
        with ingest_session():
            return _main(None, allow_source_cleanup=allow_source_cleanup)
    # TTY sessions return to the menu after ingest/review. Piped or automated
    # invocations remain one-shot to preserve batch and scripted callers.
    interactive = not auto and sys.stdin.isatty()
    while True:
        selection = '1' if auto else _main_choice()
        exit_choice = '6' if _import_first_enabled() else '5'
        if selection == exit_choice:
            return
        locked_selections = (None, '1', '4', '5') if _import_first_enabled() else (None, '1', '4')
        try:
            if selection in locked_selections:
                with ingest_session():
                    _main(selection, allow_source_cleanup=allow_source_cleanup)
            else:
                _main(selection, allow_source_cleanup=allow_source_cleanup)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Ingest/review stopped safely: {error}")
            if selection == '1':
                _ingest_report(errors=1)
        if not interactive:
            return


if __name__ == "__main__":
    main()
