from pathlib import Path
from mutagen import File
import constants
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from mutagen.aiff import AIFF
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, TPOS
from mutagen.easymp4 import EasyMP4
import os
import re
import unicodedata
import mixie_terminal as terminal

# Shared theme for nested review/repair menus.
print = terminal.themed_print
input = terminal.themed_input
from datetime import date
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, TPOS, APIC

# Locate MIXIE's script directory & music library (one directory above)
script_folder = Path(__file__).resolve().parent.parent
staging_folder = script_folder / constants.staging_folder_name
library_folder = script_folder.parent / constants.library_folder_name

# Start Function Block
# --------------------

# Ask the user which directory to scan
def choose_scan_folder():
    print("MIXIE - Metadata Manager")
    print("1. Staging")
    print("2. Music Library")
    choice = input("Select a folder (1 or 2): ")

    if choice == "1":
        return staging_folder
    elif choice == "2":
        return library_folder
    else:
        raise SystemExit("Invalid selection. Please choose 1 or 2.")

# Extract and normalize track numbering
def parse_track_number(value):
    if not value:
        return None, None
    parts = value[0].split("/")
    track = parts[0].strip()
    if not track.isdecimal():
        return None, None
    track = f"{int(track):02d}"
    total = None
    if len(parts) == 2 and parts[1].strip().isdecimal():
        total = int(parts[1].strip())
    return track, total

# Collect all reported totals for a track
def get_track_totals(track):
    totals = set()
    # Check for a total embedded in the track number
    for value in track["track_number"]:
        _, embedded_total = parse_track_number((value,))
        if embedded_total is not None:
            totals.add(embedded_total)
    # Check for separately stored totals
    for value in track["track_total"]:
        if value.strip().isdecimal():
            totals.add(int(value.strip()))
    return totals

# Collect separate and embedded disc totals without discarding conflicts.
def get_disc_totals(track):
    totals = set()
    for value in track["disc_number"]:
        _, embedded = parse_disc_number((value,))
        if embedded is not None:
            totals.add(embedded)
    for value in track["disc_total"]:
        if value.strip().isdecimal():
            totals.add(int(value.strip()))
    return totals

# Extract the disc number and reported disc total
def parse_disc_number(value):
    if not value:
        return None, None
    parts = value[0].split("/")
    disc = parts[0].strip()
    if not disc.isdecimal():
        return None, None
    disc = int(disc)
    total = None
    if len(parts) == 2 and parts[1].strip().isdecimal():
        total = int(parts[1].strip())
    return disc, total

# Check whether tracks form a complete numbering sequence
def validate_track_sequence(tracks):
    track_numbers = []
    for track in tracks:
        number, _ = parse_track_number(track["track_number"])
        if number is not None:
            track_numbers.append(int(number))
    expected = set(range(1, len(tracks) + 1))
    return (
        len(track_numbers) == len(tracks)
        and set(track_numbers) == expected
    )

# Read existing metadata without changing or discarding values
def read_track_metadata(track_path):
    track_path = Path(track_path)
    audio = File(track_path, easy=True)

    if audio is None:
        return None

    return _metadata_from_audio(track_path, audio)


def _metadata_from_audio(track_path, audio):
    # WAV/AIFF expose raw ID3 even when File was opened with easy=True.
    id3_fields = {
        "artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
        "title": "TIT2", "genre": "TCON", "date": "TDRC",
        "tracknumber": "TRCK", "discnumber": "TPOS",
    }
    # Recognized audio without tags still belongs in the audit
    tags = audio.tags
    has_tags = tags is not None

    def values(key):
        if tags is None:
            return ()

        if isinstance(tags, ID3):
            frame = tags.get(id3_fields.get(key, ""))
            value = frame.text if frame is not None else None
        else:
            value = tags.get(key)
        if value is None:
            return ()
        if isinstance(value, (str, int)):
            value = (value,)

        return tuple(str(item) for item in value)

    # Preserve both aliases so conflicting totals remain visible
    track_total_sources = {
        key: values(key)
        for key in ("tracktotal", "totaltracks")
    }
    disc_total_sources = {
        key: values(key)
        for key in ("disctotal", "totaldiscs")
    }

    return {
        "path": Path(track_path),
        "has_tags": has_tags,
        "artist": values("artist"),
        "album_artist": values("albumartist"),
        "album": values("album"),
        "title": values("title"),
        "track_number": values("tracknumber"),
        "track_total": tuple(
            value
            for entries in track_total_sources.values()
            for value in entries
        ),
        "disc_number": values("discnumber"),
        "disc_total": tuple(
            value
            for entries in disc_total_sources.values()
            for value in entries
        ),
        "date": values("date"),
        "genre": values("genre"),
        "track_total_sources": track_total_sources,
        "disc_total_sources": disc_total_sources,
    }


def _scan_music_paths(scan_folder):
    scan_folder = Path(scan_folder)
    if any(part.startswith(".mixie-pending-") for part in scan_folder.parts):
        return
    staging_root = Path(os.path.abspath(staging_folder))
    checked_stages = {}

    def include_stage(path):
        try:
            relative = Path(os.path.abspath(path)).relative_to(staging_root)
        except ValueError:
            return True
        if not relative.parts:
            return True
        import sort
        name = relative.parts[0]
        if ("--mixie-" not in name and not os.path.lexists(sort._provenance_path(name))
                and not os.path.lexists(sort._legacy_adoption_path(name))):
            return True  # Unadopted legacy tracks remain visible for conservative review.
        stage = staging_root / name
        if stage not in checked_stages:
            checked_stages[stage] = sort.is_committed_stage(stage)
            if not checked_stages[stage]:
                print(f"Skipping staged import {stage.name}: committed payload needs recovery/review.")
        return checked_stages[stage]

    if not include_stage(scan_folder):
        return
    for folder, directories, filenames in os.walk(scan_folder, followlinks=False):
        directories[:] = [
            name for name in directories
            if not name.startswith(".mixie-pending-") and include_stage(Path(folder) / name)
        ]
        for name in filenames:
            path = Path(folder) / name
            if path.suffix.lower() in constants.music_file_types and path.is_file():
                yield path


# Scan a folder and group music files by album
def scan_albums(scan_folder):
    albums = {}
    for track_path in _scan_music_paths(scan_folder):
        # Read the existing metadata
        track_data = read_track_metadata(track_path)
        if track_data is None:
            print(f"Could not read tags: {track_path.name}")
            continue

        # Group tracks by their album folder
        album_folder = track_path.parent
        if album_folder not in albums:
            albums[album_folder] = []
        albums[album_folder].append(track_data)
    return albums

# Group staged tracks by import and album, across disc folders
def group_staged_albums():
    folders = scan_albums(staging_folder)
    albums = {}
    for folder, tracks in folders.items():
        relative = folder.relative_to(staging_folder)
        import_name = relative.parts[0] if relative.parts else "."
        for track in tracks:
            album_tags = track["album"]
            if album_tags and album_tags[0].strip():
                album_name = album_tags[0].strip()
                album_key = " ".join(album_name.casefold().split())
            else:
                album_name = "Unknown Album"
                album_key = ("unknown", str(folder))
            key = (import_name, album_key)
            if key not in albums:
                albums[key] = {
                    "album": album_name,
                    "folder": folder,
                    "tracks": [],
                }
            albums[key]["tracks"].append(track)
    return albums

# Discover albums already present in the Music Library
def group_library_albums():
    albums = {}
    for path in sorted(_scan_music_paths(library_folder)):
        metadata = read_track_metadata(path)
        if metadata is None:
            print(f"Could not read metadata: {path}")
            continue
        def single_value(field):
            values = [
                value.strip()
                for value in metadata[field]
                if value.strip()
            ]
            return values[0] if len(values) == 1 else None
        album_name = single_value("album")
        album_artist = single_value("album_artist")
        track_artist = single_value("artist")

        # Recognize standard multi-disc folder structures
        folder = path.parent
        if re.fullmatch(r"(?:disc|cd)[\s_-]*\d+", folder.name, re.I):
            folder = folder.parent

        # Keep independent album folders separate.
        # For loose files at library root, distinguish albums by tags.
        artist_key = (
            album_artist or track_artist or ""
            if folder == library_folder else ""
        )

        # Avoid accidentally merging unrelated untagged files
        key = (
            folder,
            (album_name or str(path)).casefold(),
            artist_key.casefold()
        )
        if key not in albums:
            albums[key] = {
                "album": album_name or "Unknown Album",
                "folder": folder,
                "tracks": [],
                "source_root": library_folder,
                "identity_needs_review": not bool(album_name)
            }
        albums[key]["tracks"].append(metadata)
    return albums

# Identify the metadata system used by an audio file
def get_tag_family(track_path):
    extension = Path(track_path).suffix.lower()

    # Vorbis-style metadata
    if extension in (".flac", ".ogg", ".oga", ".opus"):
        return "vorbis"
    # ID3 metadata
    if extension in (".mp3", ".wav", ".aif", ".aiff"):
        return "id3"
    # MP4 metadata
    if extension in (".m4a"):
        return "mp4"
    # Unknown or unsupported format
    return None

def _require_working_copy(track_path, working_folder=None):
    """Reject originals, symlinks and hardlinks before opening a tag handle."""
    original_path = Path(track_path)
    original_folder = Path(working_folder) if working_folder is not None else original_path.parent
    track_path = original_path.resolve()
    working_folder = original_folder.resolve()
    workspace = script_folder.parent.resolve()
    if (
        workspace.name != "MIXIE-Lab"
        or working_folder.parent != workspace
        or not working_folder.name.startswith(".mixie-work-")
        or track_path.parent != working_folder
        or original_path.is_symlink()
        or original_folder.is_symlink()
    ):
        raise PermissionError("Metadata writes require an independent working copy.")
    if not track_path.is_file() or track_path.stat().st_nlink != 1:
        raise PermissionError("Metadata writes require a regular, unlinked working copy.")
    return track_path


def _editable_tags(audio):
    if audio.tags is None:
        audio.add_tags()
    if audio.tags is None:
        raise ValueError("Audio file has no editable metadata after tag creation.")
    return audio.tags


def is_uncertain_genre(value):
    """An absent classification is not a musical genre or verified evidence."""
    return (not isinstance(value, str) or not value.strip()
            or value.strip().casefold() in {"unknown", "unclassified"})


def _validate_genre_correction(field, value):
    if field == "genre" and is_uncertain_genre(value):
        raise ValueError("An uncertain genre must be left unset, not written as a placeholder.")


def _positive_number(value):
    text = str(value)
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", text) or int(text) < 1:
        raise ValueError("Numbering values must be positive integers.")
    return int(text)


# Apply standard metadata corrections to Vorbis-style tags
def write_vorbis_tags(track_path, corrections):
    track_path = _require_working_copy(track_path)
    audio = File(track_path)

    # Verify the actual audio format
    if not isinstance(audio, (FLAC, OggVorbis, OggOpus)):
        raise ValueError("This file does not use supported Vorbis-style tags.")
    tags = _editable_tags(audio)
    metadata = _metadata_from_audio(track_path, audio)

    # Translate MIXIE's field names into Vorbis comment names
    field_map = {
        "artist": "artist",
        "album_artist": "albumartist",
        "album": "album",
        "title": "title",
        "genre": "genre",
        "date": "date",
        "track_number": "tracknumber",
        "track_total": "tracktotal",
        "disc_number": "discnumber",
        "disc_total": "disctotal"
    }

    # Validate all requested corrections before changing anything
    seen = set()
    for correction in corrections:
        field = correction["field"]
        if field not in field_map:
            raise NotImplementedError(f"Unsupported metadata field: {field}")
        _validate_genre_correction(field, correction["value"])
        if field in seen:
            raise ValueError(f"Duplicate correction: {field}")
        seen.add(field)
        if field in _NUMBERING_FIELDS:
            _positive_number(correction["value"])
            existing = _numbering_existing(metadata, field)
        else:
            existing = metadata[field]
        if existing:
            raise MetadataConflictError(field, track_path, existing, correction["value"])

    # Apply the corrections
    for correction in corrections:
        tag = field_map[correction["field"]]
        tags[tag] = [str(correction["value"])]
    if corrections:
        audio.save()
    return len(corrections)

# Apply metadata corrections to ID3 tags
def write_id3_tags(track_path, corrections):
    track_path = _require_working_copy(track_path)
    audio = File(track_path)

    # Verify the actual audio format
    if not isinstance(audio, (MP3, WAVE, AIFF)):
        raise ValueError("File does not use a supported ID3 container.")

    # Create an ID3 tag if one does not exist
    if not isinstance(_editable_tags(audio), ID3):
        raise ValueError("File does not contain ID3 metadata.")
    metadata = _metadata_from_audio(track_path, audio)

    # Translate MIXIE fields into ID3 frame types
    field_map = {
        "artist": TPE1,
        "album_artist": TPE2,
        "album": TALB,
        "title": TIT2,
        "genre": TCON,
        "date": TDRC,
        "track_number": TRCK,
        "disc_number": TPOS
    }

    # Prepare corrections before saving
    updates = {}
    seen = set()
    for correction in corrections:
        field = correction["field"]
        value = str(correction["value"])
        _validate_genre_correction(field, correction["value"])
        if field in seen:
            raise ValueError(f"Duplicate correction: {field}")
        seen.add(field)
        if field in _NUMBERING_FIELDS:
            _positive_number(value)
            existing_values = _numbering_existing(metadata, field)
            if existing_values:
                raise MetadataConflictError(field, track_path, existing_values, value)

        # Track and disc totals share frames with their numbers
        if field in ("track_total", "disc_total"):
            frame_type = TRCK if field == "track_total" else TPOS
            existing = updates.get(frame_type.__name__) or audio.tags.get(frame_type.__name__)
            parser = parse_track_number if field == "track_total" else parse_disc_number
            number, total = parser(existing.text if existing else None)
            if number is None:
                raise ValueError(f"Cannot add {field} without its number.")
            if total is not None:
                raise MetadataConflictError(field, track_path, [str(total)], value)
            value = f"{number}/{value}"
        else:
            frame_type = field_map.get(field)
            if frame_type is None:
                raise NotImplementedError(f"Unsupported field: {field}")
            if audio.tags.get(frame_type.__name__):
                raise MetadataConflictError(field, track_path, metadata[field], value)
        frame_id = frame_type.__name__
        if frame_id in updates and field not in ("track_total", "disc_total"):
            raise ValueError(f"Conflicting corrections for {frame_id}.")
        updates[frame_id] = frame_type(encoding=3, text=[value])

    # Save all prepared changes together
    for frame_id, frame in updates.items():
        audio.tags.setall(frame_id, [frame])
    if updates:
        audio.save()
    return len(updates)

# Apply metadata corrections to MP4-family audio
def write_mp4_tags(track_path, corrections):
    track_path = _require_working_copy(track_path)
    audio = File(track_path, easy=True)

    # Verify the actual audio format
    if not isinstance(audio, EasyMP4):
        raise ValueError("File is not supported MP4 audio.")
    _editable_tags(audio)
    metadata = _metadata_from_audio(track_path, audio)

    # Translate MIXIE's fields into MP4 metadata keys
    field_map = {
        "artist": "artist",
        "album_artist": "albumartist",
        "album": "album",
        "title": "title",
        "genre": "genre",
        "date": "date"
    }

    # Prepare changes before saving
    updates = {}
    for correction in corrections:
        field = correction["field"]
        value = str(correction["value"])

        # Handle ordinary text fields
        if field in field_map:
            _validate_genre_correction(field, correction["value"])
            key = field_map[field]
            if audio.tags.get(key) or key in updates:
                raise MetadataConflictError(field, track_path, metadata[field], value)
            updates[key] = [value]

        # Handle track and disc numbers and totals
        elif field in ("track_number", "track_total",
                       "disc_number", "disc_total"):
            is_track = field.startswith("track_")
            key = "tracknumber" if is_track else "discnumber"
            parser = parse_track_number if is_track else parse_disc_number
            existing = updates.get(key, audio.tags.get(key, []))
            number, total = parser(existing)
            new_value = _positive_number(value)
            existing_values = _numbering_existing(metadata, field)
            if existing_values:
                raise MetadataConflictError(field, track_path, existing_values, new_value)
            if not 1 <= new_value <= 65535:
                raise ValueError("Invalid numbering value.")
            if field.endswith("_number"):
                if number is not None:
                    raise MetadataConflictError(field, track_path, [str(number)], new_value)
                number = new_value
            else:
                if total is not None:
                    raise MetadataConflictError(field, track_path, [str(total)], new_value)
                total = new_value
            if number is None:
                raise ValueError("Cannot set total without a number.")
            updates[key] = [
                f"{number}/{total}" if total is not None else str(number)
            ]
        else:
            raise NotImplementedError(f"Unsupported field: {field}")

    # Save all validated corrections together
    for key, values in updates.items():
        audio.tags[key] = values
    if updates:
        audio.save()
    return len(corrections)

# Route metadata corrections to the appropriate writer
def write_track_metadata(track_path, corrections):
    track_path = _require_working_copy(track_path)
    tag_family = get_tag_family(track_path)

    # Reject unsupported audio formats
    if tag_family is None:
        raise ValueError(f"Unsupported audio format: {track_path}")

    # Select the appropriate metadata writer
    if tag_family == "vorbis":
        return write_vorbis_tags(track_path, corrections)
    elif tag_family == "id3":
        return write_id3_tags(track_path, corrections)
    elif tag_family == "mp4":
        return write_mp4_tags(track_path, corrections)
    else:
        raise ValueError(f"Unknown metadata family: {tag_family}")

# Rockbox displays redundant DISCNUMBER=1 / DISCTOTAL=1 incorrectly.
# Remove the entire disc family from verified single-disc working copies only.
def remove_single_disc_tags_on_copy(track_path, working_folder):
    track_path = _require_working_copy(track_path, working_folder)
    family = get_tag_family(track_path)
    audio = File(track_path, easy=True) if family == "mp4" else File(track_path)
    supported = {"vorbis": (FLAC, OggVorbis, OggOpus), "id3": (MP3, WAVE, AIFF),
                 "mp4": (EasyMP4,)}
    if family not in supported or not isinstance(audio, supported[family]):
        raise ValueError("Unsupported audio format for disc tag cleanup.")
    tags = _editable_tags(audio)
    if family == "id3":
        if not isinstance(tags, ID3):
            raise ValueError("Invalid ID3 tags.")
        changed = bool(tags.getall("TPOS"))
        tags.delall("TPOS")
        # Some encoders also leave disc aliases in user-defined ID3 frames.
        for frame in tuple(tags.getall("TXXX")):
            if frame.desc.strip().casefold() in {"discnumber", "disctotal", "totaldiscs"}:
                tags.delall(frame.HashKey)
                changed = True
    else:
        changed = False
        for key in ("discnumber", "disctotal", "totaldiscs"):
            if key in tags:
                del tags[key]
                changed = True
    if changed:
        audio.save()
    saved = read_track_metadata(track_path)
    if saved is None or saved["disc_number"] or saved["disc_total"] or get_disc_totals(saved):
        raise RuntimeError("Rockbox single-disc tag cleanup failed verification.")
    if family == "id3":
        reopened = File(track_path)
        if (not isinstance(reopened, (MP3, WAVE, AIFF))
                or (reopened.tags is not None and (
                    reopened.tags.getall("TPOS")
                    or any(frame.desc.strip().casefold() in {"discnumber", "disctotal", "totaldiscs"}
                           for frame in reopened.tags.getall("TXXX"))))):
            raise RuntimeError("Rockbox single-disc ID3 cleanup failed verification.")
    return int(changed)


def remove_uncertain_genre_on_copy(path, working_root):
    """Remove only placeholder genres from an independent MP3/FLAC working copy.

    Preserve real genre values in order, including duplicates. Return 1 if a
    genre tag changed, otherwise 0; always reopen and verify the retained values.
    """
    path = _require_working_copy(path, working_root)
    audio = File(path)
    if not isinstance(audio, (MP3, FLAC)):
        raise ValueError("Unsupported audio format for uncertain genre cleanup.")
    if isinstance(audio, MP3) and audio.tags is not None and not isinstance(audio.tags, ID3):
        raise ValueError("Invalid ID3 tags.")

    def genre_state(container):
        if container.tags is None:
            return (), False, False
        if isinstance(container, MP3):
            frames = container.tags.getall("TCON")
            # Decoded genres resolve numeric IDs but hide blank raw entries.
            values = tuple(value for frame in frames for value in frame.genres)
            raw_placeholders = any(is_uncertain_genre(value) for frame in frames for value in frame.text)
            return values, bool(frames), raw_placeholders
        return tuple(container.tags.get("genre", ())), "genre" in container.tags, False

    current, present, raw_placeholders = genre_state(audio)
    retained = tuple(value for value in current if not is_uncertain_genre(value))
    changed = present and (retained != current or not retained or raw_placeholders)
    if changed and audio.tags is not None:
        if isinstance(audio, MP3):
            if retained:
                frame = audio.tags.getall("TCON")[0]
                frame.genres = list(retained)
                audio.tags.setall("TCON", [frame])
            else:
                audio.tags.delall("TCON")
        elif retained:
            audio.tags["genre"] = list(retained)
        else:
            del audio.tags["genre"]
        audio.save()

    saved = File(path)
    if (not isinstance(saved, type(audio))
            or (isinstance(saved, MP3) and saved.tags is not None and not isinstance(saved.tags, ID3))):
        raise RuntimeError("Working-copy uncertain genre cleanup failed verification.")
    saved_values, saved_present, saved_placeholders = genre_state(saved)
    if saved_values != retained or saved_present != bool(retained) or saved_placeholders:
        raise RuntimeError("Working-copy uncertain genre cleanup failed verification.")
    return int(changed)


# Replace a reviewed text field on a temporary working copy only
def replace_reviewed_text_on_copy(track_path, working_folder, field, value):
    track_path = _require_working_copy(track_path, working_folder)

    allowed_fields = {
        "album": "album",
        "album_artist": "albumartist",
        "artist": "artist",
        "title": "title",
        "date": "date",
        "genre": "genre"
    }

    if field not in allowed_fields:
        raise ValueError(f"Unsupported reviewed field: {field}")
    _validate_genre_correction(field, value)

    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
    ):
        raise ValueError("Invalid reviewed metadata value.")

    existing = read_track_metadata(track_path)
    if existing is None:
        raise ValueError("Cannot read working-copy metadata.")

    # Preserve already-correct tags without rewriting them
    if existing[field] == (value,):
        return 0

    family = get_tag_family(track_path)

    if family == "vorbis":
        audio = File(track_path)
        if not isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            raise ValueError("Unexpected Vorbis audio format.")
        _editable_tags(audio)
        audio[allowed_fields[field]] = [value]

    elif family == "id3":
        audio = File(track_path)
        if not isinstance(audio, (MP3, WAVE, AIFF)):
            raise ValueError("Unexpected ID3 audio format.")
        if not isinstance(_editable_tags(audio), ID3):
            raise ValueError("Invalid ID3 tags.")

        frames = {
            "album": TALB,
            "album_artist": TPE2,
            "artist": TPE1,
            "title": TIT2,
            "date": TDRC,
            "genre": TCON
        }
        frame = frames[field]
        audio.tags.setall(
            frame.__name__,
            [frame(encoding=3, text=[value])]
        )

    elif family == "mp4":
        audio = File(track_path, easy=True)
        if not isinstance(audio, EasyMP4):
            raise ValueError("Unexpected MP4 audio format.")
        _editable_tags(audio)
        audio[allowed_fields[field]] = [value]

    else:
        raise ValueError("Unsupported audio format for review.")

    audio.save()

    # Verify the written value immediately
    saved = read_track_metadata(track_path)
    if saved is None or saved[field] != (value,):
        raise RuntimeError(
            f"Reviewed metadata verification failed: {track_path.name}"
        )

    return 1

# Replace front artwork only on a temporary working copy
def replace_reviewed_front_art_on_copy(track_path, working_folder, artwork):
    track_path = _require_working_copy(track_path, working_folder)

    mime = artwork.get("mime")
    data = artwork.get("data")

    if (
        mime not in ("image/jpeg", "image/png")
        or not isinstance(data, bytes)
        or not data
        or len(data) > 20_000_000
    ):
        raise ValueError("Invalid approved artwork.")

    audio = File(track_path)

    if isinstance(audio, FLAC):
        # Preserve booklet images, back covers, and other pictures
        other_pictures = [
            picture for picture in audio.pictures
            if picture.type != 3
        ]

        audio.clear_pictures()

        for picture in other_pictures:
            audio.add_picture(picture)

        front = Picture()
        front.type = 3
        front.mime = mime
        front.data = data
        audio.add_picture(front)

    elif isinstance(audio, MP3):
        if not isinstance(_editable_tags(audio), ID3):
            raise ValueError("Invalid ID3 tags.")

        other_pictures = [
            picture for picture in audio.tags.getall("APIC")
            if picture.type != 3
        ]

        audio.tags.delall("APIC")

        for picture in other_pictures:
            audio.tags.add(picture)

        audio.tags.add(APIC(
            encoding=3,
            mime=mime,
            type=3,
            desc="MIXIE Front Cover",
            data=data
        ))

    else:
        raise ValueError("Unsupported audio format for artwork replacement.")

    audio.save()

    # Verify the front cover was replaced correctly
    saved = File(track_path)

    if isinstance(saved, FLAC):
        pictures = saved.pictures
    elif isinstance(saved, MP3) and isinstance(saved.tags, ID3):
        pictures = saved.tags.getall("APIC")
    else:
        raise RuntimeError("Cannot reopen working-copy artwork for verification.")

    fronts = [picture for picture in pictures if picture.type == 3]

    if (
        len(fronts) != 1
        or fronts[0].mime != mime
        or fronts[0].data != data
    ):
        raise RuntimeError("Approved artwork replacement failed verification.")

    return 1

# Embed front-cover artwork without replacing existing images
def embed_front_art(track_path, artwork):
    track_path = _require_working_copy(track_path)
    audio = File(track_path)
    if isinstance(audio, MP3):
        if not isinstance(_editable_tags(audio), ID3):
            raise ValueError("Invalid ID3 tags.")
        if audio.tags.getall("APIC"):
            return False
        audio.tags.add(APIC(
            encoding=3,
            mime=artwork["mime"],
            type=3,
            desc="Front cover",
            data=artwork["data"]
        ))
    elif isinstance(audio, FLAC):
        if audio.pictures:
            return False
        picture = Picture()
        picture.type = 3
        picture.mime = artwork["mime"]
        picture.data = artwork["data"]
        audio.add_picture(picture)
    else:
        raise NotImplementedError(
            "Artwork embedding is not supported for this format yet."
        )
    audio.save()
    return True

# Report missing or blank metadata without proposing replacements
def audit_metadata_presence(tracks):
    fields = (
        "artist", "album_artist", "album", "title",
        "track_number", "track_total",
        "disc_number", "disc_total",
        "date", "genre",
    )
    findings = {field: [] for field in fields}
    multi_disc = any(
        (parse_disc_number(track.get("disc_number", ()))[0] or 1) > 1
        or any(total > 1 for total in get_disc_totals(track))
        for track in tracks
    )

    for track in tracks:
        for field in fields:
            if field in ("disc_number", "disc_total") and not multi_disc:
                continue
            values = track.get(field, ())
            present = any(str(value).strip() for value in values)

            # Totals may be embedded in number tags
            if field == "track_total":
                _, embedded = parse_track_number(track["track_number"])
                present = present or embedded is not None
            elif field == "disc_total":
                _, embedded = parse_disc_number(track["disc_number"])
                present = present or embedded is not None

            if not present:
                findings[field].append(track["path"])

    missing = {
        field: paths for field, paths in findings.items() if paths
    }

    if missing:
        print("  Metadata absent or blank:")
        for field, paths in missing.items():
            label = field.replace("_", " ").capitalize()
            print(f"    {label}: {len(paths)}/{len(tracks)} tracks")
    else:
        print("  All audited text/number fields are populated.")

    return missing

# Inspect existing text values without rewriting them
def audit_text_fields(tracks):
    fields = (
        "artist", "album_artist", "album",
        "title", "date", "genre",
    )
    findings = []

    for track in tracks:
        for field in fields:
            for value in track.get(field, ()):
                issues = []

                if not value.strip():
                    issues.append("blank value")
                else:
                    if value != value.strip():
                        issues.append("leading/trailing whitespace")
                    if re.search(r"\s{2,}", value):
                        issues.append("repeated whitespace")

                if any(
                    ord(character) < 32 or 127 <= ord(character) <= 159
                    for character in value
                ):
                    issues.append("control character")

                if issues:
                    findings.append({
                        "path": track["path"],
                        "field": field,
                        "value": value,
                        "issues": issues,
                    })

    if findings:
        grouped = {}

        for finding in findings:
            key = (
                finding["field"],
                finding["value"],
                tuple(finding["issues"]),
            )
            grouped.setdefault(key, []).append(finding["path"])

        print("  Text formatting needs review:")

        for (field, value, issues), paths in grouped.items():
            label = field.replace("_", " ").capitalize()
            print(
                f"    {label}: {', '.join(issues)} "
                f"({len(paths)} track(s))"
            )
            print(f"      Current value: {value!r}")

            if len(paths) == 1:
                print(f"      File: {paths[0].name}")

    return findings

# Check common metadata date formats without guessing replacements
def audit_dates(tracks):
    findings = {}

    for track in tracks:
        for raw_value in track.get("date", ()):
            value = raw_value.strip()
            if not value:
                continue  # Already covered by the presence audit

            issue = None

            if not re.fullmatch(
                r"[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?",
                value,
            ):
                issue = "format needs review; expected YYYY, YYYY-MM, or YYYY-MM-DD"
            else:
                parts = [int(part) for part in value.split("-")]
                year = parts[0]
                month = parts[1] if len(parts) > 1 else 1
                day = parts[2] if len(parts) > 2 else 1

                try:
                    date(year, month, day)
                except ValueError:
                    issue = "invalid calendar date"

            if issue:
                key = (raw_value, issue)
                findings.setdefault(key, []).append(track["path"])

    if findings:
        print("  Dates need review:")
        for (value, issue), paths in findings.items():
            print(f"    {value!r}: {issue} ({len(paths)} track(s))")

    return findings

# Flag differing album-level values without choosing a winner
def audit_album_consistency(tracks):
    findings = {}

    for field in ("album", "album_artist", "date"):
        variants = {}

        for track in tracks:
            values = track.get(field, ())
            populated = tuple(
                value for value in values if value.strip()
            )
            if not populated:
                continue  # Missing values are reported separately

            # Ignore spacing/case only for this comparison.
            # Original values remain available for review.
            key = tuple(
                " ".join(value.casefold().split())
                for value in populated
            )

            if key not in variants:
                variants[key] = {
                    "values": populated,
                    "paths": [],
                }

            variants[key]["paths"].append(track["path"])

        if len(variants) > 1:
            findings[field] = list(variants.values())

    if findings:
        print("  Album-level differences need review:")

        for field, variants in findings.items():
            print(f"    {field.replace('_', ' ').capitalize()}:")

            for variant in variants:
                display = " / ".join(
                    repr(value) for value in variant["values"]
                )
                print(
                    f"      {display} "
                    f"({len(variant['paths'])} track(s))"
                )

    return findings

# Analyze an album and prepare metadata corrections
def analyze_album(album_folder, tracks, proposals, album_name=None):

    # Count how many tracks belong to each disc
    disc_track_counts = {}

    for track in tracks:
        disc_number, _ = parse_disc_number(track["disc_number"])
        disc_id = str(disc_number) if disc_number is not None else "1"

        disc_track_counts[disc_id] = disc_track_counts.get(disc_id, 0) + 1

    # Display a clean summary of the album
    print(f"\nAlbum: {album_name or album_folder.name}")
    print(f"Tracks: {len(tracks)} | Discs: {len(disc_track_counts)}")
    audit_metadata_presence(tracks)
    audit_text_fields(tracks)
    audit_dates(tracks)
    audit_album_consistency(tracks)

    # Prepare a collection for checking existing track totals
    reported_totals = set()
    if len(disc_track_counts) == 1:

        # Collect totals from every track
        for track in tracks:
            reported_totals.update(get_track_totals(track))

        # Check for conflicting totals after collecting everything
        if reported_totals and reported_totals != {len(tracks)}:
            print(f"  Warning: Reported totals {sorted(reported_totals)}")
            print(f"  Local track count: {len(tracks)} (needs review)")

            # Identify the individual files causing the conflict
            for track in tracks:
                totals = get_track_totals(track)
                if totals and totals != {len(tracks)}:
                    print(f"    {track['path'].name}: Reported {sorted(totals)}")

    # Count tracks with no reported total
    missing_totals = 0
    for track in tracks:
        if not get_track_totals(track):
            missing_totals += 1

    # Suggest a total for single-disc albums
    if missing_totals > 0:
        print(f"  Missing totals: {missing_totals}")

        # Only suggest corrections when existing totals agree
        if len(disc_track_counts) == 1 and (
            not reported_totals or reported_totals == {len(tracks)}
        ):

            # Check whether the album's track numbering is complete
            if validate_track_sequence(tracks):
                print(f"  Suggested total: {len(tracks)} (sequence complete, release unverified)")

                # Prepare corrections for tracks with missing totals
                for track in tracks:
                    if not get_track_totals(track):
                        proposals.append({
                            "path": track["path"],
                            "field": "track_total",
                            "value": len(tracks),
                            "verified": False
                        })
            else:
                print("  Warning: Missing or duplicate track numbers")

    # Validate track numbering separately for each disc
    if len(disc_track_counts) > 1:
        for disc_id in sorted(disc_track_counts, key=int):
            disc_tracks = []

            # Collect tracks belonging to this disc
            for track in tracks:
                disc_number, _ = parse_disc_number(track["disc_number"])
                track_disc = str(disc_number) if disc_number is not None else "1"
                if track_disc == disc_id:
                    disc_tracks.append(track)

            # Validate the collected tracks
            count = len(disc_tracks)
            if validate_track_sequence(disc_tracks):
                print(f"  Disc {disc_id}: {count} tracks | Numbering OK")
            else:
                print(f"  Disc {disc_id}: {count} tracks | Numbering needs review")

            # Compare reported totals with this disc's local track count
            totals_by_value = {}

            for track in disc_tracks:
                for total in get_track_totals(track):
                    totals_by_value.setdefault(total, []).append(
                        track["path"]
                    )

            for total, paths in sorted(totals_by_value.items()):
                if total != count:
                    print(
                        f"    Track total {total} on {len(paths)} track(s); "
                        f"this disc contains {count} local tracks "
                        f"(needs review)"
                    )


def _normalize_label(value):
    value = unicodedata.normalize("NFC", str(value)).translate(str.maketrans({
        "’": "'", "‘": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "‑": "-",
    }))
    return " ".join(value.casefold().split())


def validate_recording_identity(identity):
    """Validate supplied evidence, without treating approval as verification.

    Requires recording_id, title, artist and either verified=True or
    user_approved=True. MusicBrainz's id/artist-credit-phrase aliases are
    accepted. Other recording details do not authorize date/genre writes.
    """
    if not isinstance(identity, dict):
        raise ValueError("A supplied recording identity is required.")
    verified = identity.get("verified", False)
    approved = identity.get("user_approved", False)
    if type(verified) is not bool or type(approved) is not bool or not (verified or approved):
        raise ValueError("Recording identity needs verification or explicit user approval.")
    cleaned = {"verified": verified, "user_approved": approved}
    for field, alias in (("recording_id", "id"), ("title", "title"),
                         ("artist", "artist-credit-phrase")):
        value = identity.get(field, identity.get(alias))
        if (not isinstance(value, str) or not value.strip()
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
            raise ValueError(f"Invalid recording identity {field}.")
        if field != alias and field in identity and alias in identity and identity[alias] != value:
            raise ValueError(f"Conflicting recording identity {field} aliases.")
        cleaned[field] = value.strip()
    return cleaned


def joint_credit_equivalent(existing, reference):
    """Match two real joint artists with an underscore, ampersand or `and`."""
    if not isinstance(existing,str) or not isinstance(reference,str):return False
    def split(value):
        for sep in (r'\s+and\s+',r'\s*&\s*',r'\s*_\s*'):
            parts=re.split(sep,value.strip(),maxsplit=1,flags=re.I)
            if len(parts)==2 and all(p.strip() for p in parts):
                return tuple(_normalize_label(part) for part in parts)
        return None
    first,second=split(existing),split(reference)
    return first is not None and first==second


def apply_verified_identity_on_copy(track_path, working_folder, identity, *, authoritative_release=False):
    """Replace title/artist on an independent working copy; return field writes.

    A materially different existing artist credit requires user_approved=True,
    even for a verified recording. All values/conflicts are checked before any
    replacement; equivalent credit typography is preserved byte-for-byte.
    """
    track_path = _require_working_copy(track_path, working_folder)
    identity = validate_recording_identity(identity)
    if type(authoritative_release) is not bool or (authoritative_release and
            (not identity["verified"] or identity["user_approved"])):
        raise ValueError("Canonical artist override requires independently verified recording identity.")
    metadata = read_track_metadata(track_path)
    if metadata is None:
        raise ValueError("Cannot read working-copy metadata.")
    changes = []
    for field in ("title", "artist"):
        current = metadata[field]
        expected = identity[field]
        if len(current) == 1 and _normalize_label(current[0]) == _normalize_label(expected):
            continue
        if (field == "artist" and any(value.strip() for value in current)
                and not identity["user_approved"] and not authoritative_release):
            # A suffix explicitly credited as featuring is not a second lead
            # artist. Only normalize when that guest remains in the verified
            # title; joint performers without a feat suffix stay protected.
            guest = re.search(r"\s+(?:feat\.?|ft\.?|featuring)\s+(.+)$", current[0], re.I) if len(current) == 1 else None
            featured_title = re.search(r"(?:feat\.?|ft\.?|featuring)\s+([^\])]+)", identity["title"], re.I)
            if not joint_credit_equivalent(current[0] if len(current)==1 else '',expected) and (
                    guest is None or featured_title is None
                    or _normalize_label(current[0][:guest.start()]) != _normalize_label(expected)
                    or _normalize_label(guest[1]) != _normalize_label(featured_title[1])):
                raise MetadataConflictError(field, track_path, current, expected)
        changes.append((field, expected))
    return sum(
        replace_reviewed_text_on_copy(track_path, working_folder, field, value)
        for field, value in changes
    )


# Propose corrections using an identified recording
def propose_identity_corrections(
    track, details, title_matches, artist_matches, *, verified=False, user_approved=False
):
    if type(verified) is not bool or type(user_approved) is not bool:
        raise ValueError("Identity provenance flags must be booleans.")
    proposals = []

    for field, matches, value in (
        ("title", title_matches, details.get("title")),
        ("artist", artist_matches, details.get("artist-credit-phrase")),
    ):
        if not matches and value:
            proposals.append({
                "path": track["path"],
                "field": field,
                "value": value,
                "previous": track[field][0] if track[field] else "",
                "verified": verified,
                **({"user_approved": True} if user_approved else {}),
            })

    return proposals

_NUMBERING_FIELDS = ("track_number", "track_total", "disc_number", "disc_total")


def _numbering_existing(metadata, field):
    if field.endswith("_number"):
        return list(metadata[field])
    prefix = field.split("_")[0]
    numeric, invalid = set(), []
    for value in metadata[field]:
        if re.fullmatch(r"[0-9]+", value.strip()):
            numeric.add(int(value.strip()))
        else:
            invalid.append(value)
    for value in metadata[prefix + "_number"]:
        _, separator, total = value.partition("/")
        if separator:
            if re.fullmatch(r"[0-9]+", total.strip()):
                numeric.add(int(total.strip()))
            else:
                invalid.append(total)
    return [str(value) for value in sorted(numeric)] + sorted(set(invalid))


def _numbering_agrees(metadata, field, expected, allowed_totals):
    existing = _numbering_existing(metadata, field)
    if field.endswith("_total"):
        return any(existing == [str(value)] for value in allowed_totals)
    if len(existing) != 1:
        return False
    parser = parse_track_number if field == "track_number" else parse_disc_number
    number, _ = parser(existing)
    return number is not None and int(number) == expected


# Preview numbering corrections corroborated by the full release tracklist
def propose_release_numbering(matched, release_tracks, reviewed_decision=None, *, allow_partial=False, independently_verified=False):
    """Fill missing numbering, or propose an exact user-reviewed replacement.

    The default requires complete coverage. Only pass allow_partial=True after
    the parent has an explicitly reviewed interpretation of a partial import.
    Even then, release_tracks must be the FULL release, not the local subset:
    every matched position and every proposed total is checked against it.

    reviewed_decision may be one dict or a list of dicts, each containing:
    status='pending_application', path, field, existing_value (list of
    strings), reference_value and value (both str(expected)). Decisions are
    bound to the supplied full release and must all resolve current conflicts.
    Replacement proposals carry previous/user_approved/numbering_snapshot
    and work with apply_corrections after only their paths are redirected
    to working copies. Keep these provenance/snapshot fields intact.
    Single-disc releases never require or propose disc fields; the parent
    removes that family with remove_single_disc_tags_on_copy before sorting.
    """
    if type(allow_partial) is not bool:
        raise ValueError("allow_partial must be a literal boolean backed by explicit review.")
    if type(independently_verified) is not bool:
        raise ValueError("independently_verified must be a literal boolean.")
    if independently_verified and reviewed_decision is not None:
        raise ValueError("Independent numbering and saved user numbering decisions cannot be mixed.")
    positions = [(str(t["disc"]), str(t["number"])) for t in release_tracks]
    release_positions = set(positions)
    matched_positions = set(matched)
    coverage = (matched_positions <= release_positions if allow_partial
                else matched_positions == release_positions)
    if (not matched or len(positions) != len(release_positions) or not coverage
            or len({Path(path).resolve() for path in matched.values()}) != len(matched)):
        raise ValueError("Album mapping does not match the supplied full release coverage.")
    numeric_positions = [(_positive_number(disc), _positive_number(number))
                         for disc, number in positions]
    if len(numeric_positions) != len(set(numeric_positions)):
        raise ValueError("Duplicate numeric release positions require review.")
    discs = {disc for disc, _ in numeric_positions}
    if not discs or discs != set(range(1, len(discs) + 1)):
        raise ValueError("Unusual disc numbering requires review.")
    disc_total = len(discs)
    for disc in discs:
        numbers = {number for current_disc, number in numeric_positions if current_disc == disc}
        if numbers != set(range(1, len(numbers) + 1)):
            raise ValueError("Full release track numbering is incomplete or unusual; review required.")
    album_total = len(positions)
    if reviewed_decision is None:
        decisions = []
    elif isinstance(reviewed_decision, dict):
        decisions = [reviewed_decision]
    elif isinstance(reviewed_decision, (list, tuple)):
        decisions = list(reviewed_decision)
    else:
        raise ValueError("Invalid numbering review decisions.")
    indexed = {}
    for decision in decisions:
        if (not isinstance(decision, dict)
                or decision.get("status") != "pending_application"
                or decision.get("field") not in _NUMBERING_FIELDS
                or not isinstance(decision.get("path"), (str, Path))
                or not isinstance(decision.get("existing_value"), list)
                or not decision["existing_value"]
                or not all(isinstance(value, str) for value in decision["existing_value"])):
            raise ValueError("Saved numbering decision needs fresh review.")
        key = (Path(decision["path"]).resolve(), decision["field"])
        if key in indexed:
            raise ValueError("Duplicate numbering review decision.")
        indexed[key] = decision
    used = set()
    proposals = []
    for (disc, number), path in matched.items():
        path = Path(path)
        metadata = read_track_metadata(path)
        if metadata is None:
            raise ValueError(f"Cannot read metadata: {path.name}")
        disc_number, track_number = int(disc), int(number)
        disc_tracks = sum(1 for d, _ in numeric_positions if d == disc_number)
        for field, expected in (
            ("track_number", track_number), ("track_total", disc_tracks),
            ("disc_number", disc_number), ("disc_total", disc_total),
        ):
            if disc_total == 1 and field in ("disc_number", "disc_total"):
                continue
            existing = _numbering_existing(metadata, field)
            allowed = (disc_tracks, album_total) if field == "track_total" else (expected,)
            replacement = {}
            if existing:
                if _numbering_agrees(metadata, field, expected, allowed):
                    continue
                key = (path.resolve(), field)
                decision = indexed.get(key)
                if decision is None and not independently_verified:
                    raise MetadataConflictError(field, path, existing, expected)
                if decision is not None:
                    if (decision["existing_value"] != existing
                            or decision.get("reference_value") != str(expected)
                            or decision.get("value") != str(expected)):
                        raise ValueError("Saved numbering decision needs fresh review.")
                    used.add(key)
                # A verified recording-to-release-position map is independent
                # evidence, not a user approval. Snapshot the exact original
                # tags so that neither subsequent editing nor a stale plan can
                # silently replace a different value.
                replacement = {
                    "previous": existing,
                    "numbering_snapshot": {
                        name: _numbering_existing(metadata, name) for name in _NUMBERING_FIELDS
                    },
                    **({"user_approved": True} if decision is not None else
                       {"verified_release_position": True}),
                }
            proposals.append({
                "path": path, "field": field, "value": expected,
                "verified": False, **replacement,
            })
    if used != set(indexed):
        raise ValueError("Saved numbering decision no longer resolves a conflict.")
    return proposals

class MetadataConflictError(ValueError):
    def __init__(self, field, path, existing, expected):
        self.field = field
        self.path = str(path)
        self.existing = [str(value) for value in existing]
        self.expected = str(expected)

        super().__init__(
            f"Conflicting {field}: {Path(path).name}\n"
            f"  Existing metadata: {tuple(self.existing)!r}\n"
            f"  Matched release: {(self.expected,)!r}"
        )

class RequiredMetadataReview(ValueError):
    """Metadata needs a human choice instead of an invented value."""
    def __init__(self, fields):
        self.fields = sorted(set(fields))
        super().__init__(
            "A reliable value is needed for: " + ", ".join(self.fields)
        )


def validate_manual_metadata(fields):
    """Validate explicit review values before use on working copies."""
    import datetime
    if fields is None:
        return {}
    if not isinstance(fields, dict) or set(fields) - {"genre", "date"}:
        raise ValueError("Invalid reviewed metadata fields.")
    cleaned = {}
    for key, value in fields.items():
        if (not isinstance(value, str) or not value.strip() or len(value) > 96
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError(f"Invalid reviewed {key}.")
        value = value.strip()
        _validate_genre_correction(key, value)
        if key == "date":
            parts = value.split("-")
            if (len(parts) not in (1, 2, 3)
                    or not all(part.isdecimal() for part in parts)
                    or len(parts[0]) != 4):
                raise ValueError("Date must be YYYY, YYYY-MM or YYYY-MM-DD.")
            year = int(parts[0])
            if not (1000 <= year <= 9999):
                raise ValueError("Invalid release year.")
            if len(parts) >= 2 and (len(parts[1]) != 2 or not 1 <= int(parts[1]) <= 12):
                raise ValueError("Invalid release month.")
            if len(parts) == 3:
                if len(parts[2]) != 2:
                    raise ValueError("Invalid release day.")
                datetime.date(year, int(parts[1]), int(parts[2]))
        cleaned[key] = value
    return cleaned


def propose_consistent_album_tags(matched, common_metadata, source_tracks=None):
    """Fill missing shared album tags after a complete, corroborated mapping.

    Never replace nonempty source tags, infer totals, or use track-artist
    credits as album-artist evidence. Only identical nonblank original album
    values already present on this release may supply missing values, and
    an independent common release value always takes precedence.
    """
    paths = [Path(path).resolve() for path in matched.values()]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Shared metadata requires distinct mapped album tracks.")
    if source_tracks is None:
        tracks = [read_track_metadata(path) for path in paths]
    else:
        original = {}
        for track in source_tracks:
            if not isinstance(track, dict) or "path" not in track:
                raise ValueError("Invalid original album metadata inventory.")
            path = Path(track["path"]).resolve()
            if path in original:
                raise ValueError("Duplicate original album metadata path.")
            original[path] = track
        if set(original) != set(paths):
            raise ValueError("Source metadata inventory does not match mapped tracks.")
        tracks = [original[path] for path in paths]
    if any(track is None for track in tracks):
        raise ValueError("Cannot read album source metadata.")
    proposals = []
    for field in ("album", "album_artist"):
        if common_metadata.get(field):
            continue  # Independently supported common release metadata owns this field.
        nonempty = {tuple(track[field]) for track in tracks if track[field]}
        if len(nonempty) != 1:
            continue  # Mixed/unknown values require review rather than a guess.
        value_tuple = next(iter(nonempty))
        if len(value_tuple) != 1 or not value_tuple[0].strip():
            continue
        for track in tracks:
            if not track[field]:
                proposals.append({"path": track["path"], "field": field,
                                  "value": value_tuple[0], "verified": False,
                                  "source": "consistent_existing_album_tags"})
    return proposals


def propose_required_album_metadata(matched, common, manual_fields=None, *, contextual_genre=None,
                                    allow_unclassified=False, canonical_genre=None):
    """Resolve supported date/genre values without inventing a classification.

    Explicitly reviewed overrides are applied later on temporary copies.
    Consistent existing or contextual genres can fill gaps, but are unverified.
    Missing/uncertain genre is optional, including literal placeholder values;
    mixed real genres remain untouched rather than choosing a winner.
    """
    manual = validate_manual_metadata(manual_fields)
    tracks = [read_track_metadata(path) for path in matched.values()]
    if any(track is None for track in tracks):
        raise ValueError("Cannot read staged metadata.")
    unresolved = []
    proposals = []
    for field in ("date", "genre"):
        if field in manual:
            continue
        observed = {
            value.strip() for track in tracks for value in track[field]
            if value.strip() and (field != "genre" or not is_uncertain_genre(value))
        }
        # Album-level release date from all compatible editions takes
        # precedence; incompatible existing tags are handled separately.
        if field == "date" and common.get("date"):
            continue  # propose_common_metadata already handled this.
        # Only use catalog consensus for a completely blank album genre.
        # This is contextual metadata, never independent audio verification.
        if field == "genre" and canonical_genre is not None and not is_uncertain_genre(canonical_genre):
            continue  # copy-only canonicalization is handled after staging
        if (field == "genre" and not observed and isinstance(contextual_genre, str)
                and not is_uncertain_genre(contextual_genre)):
            selected = contextual_genre.strip()
            source = "consistent_artist_catalog"
        elif len(observed) == 1:
            selected = next(iter(observed))
            source = "consistent_existing_album_tags"
        elif field == "genre":
            continue  # No classification is better than an invented or disputed one.
        elif allow_unclassified and field == "date" and not observed:
            # An unknown date is never fabricated solely to permit import.
            continue
        else:
            unresolved.append(field)
            continue
        for track in tracks:
            if not any(str(value).strip() for value in track[field]):
                proposals.append({
                    "path": track["path"], "field": field,
                    "value": selected, "verified": False, "source": source
                })
    if unresolved:
        raise RequiredMetadataReview(unresolved)
    return proposals

# Preview shared album-level metadata corrections
def propose_common_metadata(
    matched, common_metadata, reviewed_decision=None, *, release_type=None
):
    """Preserve equivalent originals; '- Single' needs release_type='Single'.

    reviewed_decision accepts None, a dict, or a list/tuple of dicts. Each
    retains the existing field/value/existing_value/reference_value/status
    contract. Optional path scopes a decision to one matched source file;
    pathless legacy decisions apply to the field album-wide. An exact path
    takes precedence over a pathless decision; duplicate scopes are rejected.
    Approved replacements are still applied separately on working copies.
    """
    proposals = []
    if reviewed_decision is None:
        decisions = []
    elif isinstance(reviewed_decision, dict):
        decisions = [reviewed_decision]
    elif isinstance(reviewed_decision, (list, tuple)):
        decisions = list(reviewed_decision)
    else:
        raise ValueError("Invalid metadata review decisions.")
    target_paths = {Path(path).resolve() for path in matched.values()}
    indexed = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("Saved metadata decision needs fresh review.")
        field, value = decision.get("field"), decision.get("value")
        if (field not in ("album", "album_artist", "date", "genre")
                or decision.get("status") != "pending_application"
                or not isinstance(value, str) or not value.strip()
                or not isinstance(decision.get("existing_value"), str)
                or decision.get("reference_value") != str(common_metadata.get(field))):
            raise ValueError("Saved metadata decision needs fresh review.")
        path = decision.get("path")
        if path is not None:
            if not isinstance(path, (str, Path)) or Path(path).resolve() not in target_paths:
                raise ValueError("Saved metadata decision is outside the matched source paths.")
            path = Path(path).resolve()
        key = (path, field)
        if key in indexed:
            raise ValueError("Duplicate metadata review scope.")
        indexed[key] = decision

    for _, path in matched.items():
        path = Path(path)
        metadata = read_track_metadata(path)
        if metadata is None:
            raise ValueError(f"Cannot read metadata: {path.name}")
        effective = {}
        for field in ("album", "album_artist", "date", "genre"):
            decision = indexed.get((path.resolve(), field), indexed.get((None, field)))
            if decision is not None:
                current = metadata[field]
                if ((current or decision.get("path") is not None)
                        and current != (decision["existing_value"],)):
                    raise ValueError("Staged metadata changed since review.")
                effective[field] = decision
        for field in ("album", "album_artist", "date"):
            expected = common_metadata.get(field)
            if not expected:
                continue
            current = metadata[field]
            if current:
                if current == (str(expected),):
                    continue

                # Accept equivalent capitalization, spacing, and typography
                if field in ("album", "album_artist") and len(current) == 1:
                    if _normalize_label(current[0]) == _normalize_label(expected):
                        continue

                # Only a corroborated release type makes this storefront suffix safe.
                if (field == "album" and len(current) == 1
                        and isinstance(release_type, str)
                        and release_type.strip().casefold() == "single"):
                    existing_album = re.sub(r" - single$", "", _normalize_label(current[0]))
                    expected_album = re.sub(r" - single$", "", _normalize_label(expected))
                    if existing_album and existing_album == expected_album:
                        continue

                # Treat "Vol. IV" and "Volume IV" as equivalent
                if field == "album" and len(current) == 1:
                    def normalize_volume_title(value):
                        value = _normalize_label(value)
                        return re.sub(
                            r",?\s+\bvol(?:ume)?\.?\s+(?=[ivxlcdm0-9]+\b)",
                            " volume ",
                            value
                        ).strip()

                    if (
                        normalize_volume_title(current[0])
                        == normalize_volume_title(str(expected))
                    ):
                        continue

                # A year-only date can agree with a full release date
                if field == "date" and len(current) == 1:
                    existing = current[0].strip()
                    expected_date = str(expected).strip()
                    if (
                        len(existing) == 4
                        and existing.isdigit()
                        and expected_date.startswith(existing + "-")
                    ):
                        continue

                # Only this path's effective, freshly validated decision applies.
                if field in effective:
                    continue

                raise MetadataConflictError(
                    field, path, current, expected
                )

            proposals.append({
                "path": path,
                "field": field,
                "value": expected,
                "verified": False
            })
    return proposals

def _prepare_reviewed_numbering(metadata, corrections):
    updates = {}
    for correction in corrections:
        field = correction["field"]
        if field not in _NUMBERING_FIELDS or field in updates:
            raise ValueError("Invalid or duplicate numbering correction.")
        existing = _numbering_existing(metadata, field)
        if (correction.get("user_approved") is True or
                correction.get("verified_release_position") is True) and "previous" in correction:
            if correction.get("user_approved") is True and correction.get("verified_release_position") is True:
                raise ValueError("Numbering replacement has ambiguous provenance.")
            snapshot = {name: _numbering_existing(metadata, name) for name in _NUMBERING_FIELDS}
            if (correction.get("numbering_snapshot") != snapshot
                    or not existing or correction["previous"] != existing):
                raise ValueError("Working-copy numbering changed since verified release mapping.")
        elif existing:
            raise MetadataConflictError(field, metadata["path"], existing, correction["value"])
        elif "previous" in correction:
            raise ValueError("Numbering replacement needs explicit user approval.")
        updates[field] = _positive_number(correction["value"])

    states = {}
    for prefix, parser in (("track", parse_track_number), ("disc", parse_disc_number)):
        number_field, total_field = prefix + "_number", prefix + "_total"
        if number_field not in updates and total_field not in updates:
            continue
        number, _ = parser(metadata[number_field])
        number = updates.get(number_field, int(number) if number is not None else None)
        totals = _numbering_existing(metadata, total_field)
        if total_field in updates:
            total = updates[total_field]
        elif not totals:
            total = None
        elif len(totals) == 1 and re.fullmatch(r"[0-9]+", totals[0]) and int(totals[0]) > 0:
            total = int(totals[0])
        else:
            raise RequiredMetadataReview([total_field])
        if number is None or number < 1:
            raise RequiredMetadataReview([number_field])
        states[prefix] = (number, total)
    return updates, states


def _apply_reviewed_numbering_on_copy(track_path, corrections):
    track_path = _require_working_copy(track_path)
    family = get_tag_family(track_path)
    audio = File(track_path, easy=True) if family == "mp4" else File(track_path)
    supported = {"vorbis": (FLAC, OggVorbis, OggOpus), "id3": (MP3, WAVE, AIFF),
                 "mp4": (EasyMP4,)}
    if family not in supported or not isinstance(audio, supported[family]):
        raise ValueError("Unsupported audio format for numbering review.")
    tags = _editable_tags(audio)
    if family == "id3" and not isinstance(tags, ID3):
        raise ValueError("Invalid ID3 tags.")
    metadata = _metadata_from_audio(track_path, audio)
    updates, states = _prepare_reviewed_numbering(metadata, corrections)
    if family == "mp4" and any(value > 65535 for value in updates.values()):
        raise ValueError("Invalid MP4 numbering value.")

    for prefix, (number, total) in states.items():
        number_field, total_field = prefix + "_number", prefix + "_total"
        key = prefix + "number"
        if family == "vorbis":
            if total_field in updates:
                # Clear every old alias and embedded total, not just the first.
                tags[key] = [str(number)]
                for alias in (prefix + "total", "total" + prefix + "s"):
                    if alias in tags:
                        del tags[alias]
                tags[prefix + "total"] = [str(total)]
            else:
                embedded = any("/" in value for value in metadata[number_field])
                tags[key] = [f"{number}/{total}" if embedded and total is not None else str(number)]
        else:
            number_text = f"{number:02d}" if prefix == "track" and family == "id3" else str(number)
            value = f"{number_text}/{total}" if total is not None else number_text
            if family == "id3":
                frame = TRCK if prefix == "track" else TPOS
                tags.setall(frame.__name__, [frame(encoding=3, text=[value])])
            else:
                tags[key] = [value]
    if updates:
        audio.save()
    return len(updates)


# Apply approved metadata corrections
def apply_corrections(proposals, approved=False):

    # Never modify files without explicit approval
    if not approved:
        print("Corrections not approved. No files modified.")
        return 0

    # Restrict writing tests to our development workspace
    lab_folder = script_folder.parent.resolve()
    if lab_folder.name != "MIXIE-Lab":
        raise PermissionError("Writing is disabled outside MIXIE-Lab.")

    # Check every proposed file before making changes, including aliases/links.
    proposals = list(proposals)
    for proposal in proposals:
        _require_working_copy(proposal["path"])

    # Group corrections by file
    grouped = {}

    for proposal in proposals:
        track_path = Path(proposal["path"]).resolve()
        field = proposal["field"]
        if field not in (
            "artist", "album_artist", "album", "title",
            "date", "genre", "track_number", "track_total",
            "disc_number", "disc_total"
        ):
            raise NotImplementedError(f"Unsupported correction: {field}")
        _validate_genre_correction(field, proposal["value"])
        corrections = grouped.setdefault(track_path, {})
        if field in corrections:
            raise ValueError(f"Duplicate correction: {field}")
        corrections[field] = proposal

    reviewed_tracks = set()
    for track_path, corrections in grouped.items():
        if any(field in _NUMBERING_FIELDS and
               (correction.get("user_approved") is True or correction.get("verified_release_position") is True)
               and "previous" in correction for field, correction in corrections.items()):
            metadata = read_track_metadata(track_path)
            if metadata is None:
                raise ValueError("Cannot read working-copy numbering.")
            _prepare_reviewed_numbering(metadata, [corrections[field] for field in _NUMBERING_FIELDS
                                                 if field in corrections])
            reviewed_tracks.add(track_path)
    applied = 0

    # Write each track's corrections together
    for track_path, corrections in grouped.items():
        ordered = [
            corrections[field]
            for field in (
                "artist", "album_artist", "album", "title",
                "date", "genre", "track_number", "track_total",
                "disc_number", "disc_total"
            )
            if field in corrections
        ]
        if track_path in reviewed_tracks:
            ordinary = [correction for correction in ordered if correction["field"] not in _NUMBERING_FIELDS]
            numbering = [correction for correction in ordered if correction["field"] in _NUMBERING_FIELDS]
            changes = write_track_metadata(track_path, ordinary) if ordinary else 0
            changes += _apply_reviewed_numbering_on_copy(track_path, numbering)
        else:
            changes = write_track_metadata(track_path, ordered)

        # Reopen the file and verify every correction
        saved = read_track_metadata(track_path)
        if saved is None:
            raise RuntimeError(f"Could not verify: {track_path.name}")
        for correction in ordered:
            field = correction["field"]
            value = correction["value"]
            if field == "track_number":
                expected = int(value)
                number, _ = parse_track_number(saved["track_number"])
                valid = number is not None and int(number) == expected
            elif field == "track_total":
                expected = int(value)
                valid = get_track_totals(saved) == {expected}
            elif field == "disc_number":
                number, _ = parse_disc_number(saved["disc_number"])
                valid = number == int(value)
            elif field == "disc_total":
                valid = get_disc_totals(saved) == {int(value)}
            else:
                valid = saved[field] == (str(value),)
            if not valid:
                raise RuntimeError(
                    f"Verification failed: {track_path.name}"
                )
        applied += changes
    return applied

# -----------------
# End Function Block

# Run MIXIE's metadata manager
def main(scan_folder=None):

    # Ask for a folder only if one wasn't provided
    if scan_folder is None:
        scan_folder = choose_scan_folder()

    print(f"Selected directory: {scan_folder}")

    # Store proposed metadata corrections without applying them
    proposals = []

    if Path(scan_folder).resolve() == staging_folder.resolve():
        albums = group_staged_albums()

        for album_data in albums.values():
            analyze_album(
                album_data["folder"],
                album_data["tracks"],
                proposals,
                album_name=album_data["album"],
            )
    else:
        albums = scan_albums(scan_folder)

        for album_folder, tracks in albums.items():
            analyze_album(album_folder, tracks, proposals)

    # Display all proposed metadata corrections
    print(f"\nProposed corrections: {len(proposals)} (not applied)")
    for proposal in proposals:
        file_path = proposal["path"].relative_to(scan_folder)
        status = "Verified" if proposal["verified"] else "Unverified"
        print(f"  {file_path}")
        print(f"    {proposal['field']}: Missing -> {proposal['value']} ({status})")

    # Return proposed corrections for other MIXIE components
    return proposals

# Run MIXIE when this script is launched directly
if __name__ == "__main__":
    main()
