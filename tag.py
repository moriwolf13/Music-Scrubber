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
import re
from datetime import date
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, TPOS, APIC

# Locate MIXIE's script directory & music library (one directory above)
script_folder = Path(__file__).resolve().parent
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
    if not track.isdigit():
        return None, None
    track = f"{int(track):02d}"
    total = None
    if len(parts) == 2 and parts[1].strip().isdigit():
        total = int(parts[1].strip())
    return track, total

# Collect all reported totals for a track
def get_track_totals(track):
    totals = set()
    # Check for a total embedded in the track number
    _, embedded_total = parse_track_number(track["track_number"])
    if embedded_total is not None:
        totals.add(embedded_total)
    # Check for separately stored totals
    for value in track["track_total"]:
        if value.strip().isdigit():
            totals.add(int(value.strip()))
    return totals

# Collect separate and embedded disc totals without discarding conflicts.
def get_disc_totals(track):
    totals = set()
    _, embedded = parse_disc_number(track["disc_number"])
    if embedded is not None:
        totals.add(embedded)
    for value in track["disc_total"]:
        if value.strip().isdigit():
            totals.add(int(value.strip()))
    return totals

# Extract the disc number and reported disc total
def parse_disc_number(value):
    if not value:
        return None, None
    parts = value[0].split("/")
    disc = parts[0].strip()
    if not disc.isdigit():
        return None, None
    disc = int(disc)
    total = None
    if len(parts) == 2 and parts[1].strip().isdigit():
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

    # Recognized audio without tags still belongs in the audit
    tags = audio.tags
    has_tags = tags is not None

    def values(key):
        if tags is None:
            return ()

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
        "path": track_path,
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
# Scan a folder and group music files by album
def scan_albums(scan_folder):
    albums = {}
    for track_path in scan_folder.rglob("*"):
        if not track_path.is_file():
            continue
        if track_path.suffix.lower() not in constants.music_file_types:
            continue

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
    for path in sorted(library_folder.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in constants.music_file_types:
            continue
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

# Apply standard metadata corrections to Vorbis-style tags
def write_vorbis_tags(track_path, corrections):
    audio = File(track_path)

    # Verify the actual audio format
    if not isinstance(audio, (FLAC, OggVorbis, OggOpus)):
        raise ValueError("This file does not use supported Vorbis-style tags.")
    if audio.tags is None:
        raise ValueError("Audio file has no editable metadata.")

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
    for correction in corrections:
        field = correction["field"]
        if field not in field_map:
            raise NotImplementedError(f"Unsupported metadata field: {field}")
        tag = field_map[field]
        if audio.get(tag):
            raise ValueError(f"Existing {tag} requires review.")

        # Totals can also be embedded in number tags
        if field == "track_total":
            _, total = parse_track_number(audio.get("tracknumber"))
            if total is not None or audio.get("totaltracks"):
                raise ValueError("Track total already exists.")
        if field == "disc_total":
            _, total = parse_disc_number(audio.get("discnumber"))
            if total is not None or audio.get("totaldiscs"):
                raise ValueError("Disc total already exists.")

    # Apply the corrections
    for correction in corrections:
        tag = field_map[correction["field"]]
        audio[tag] = [str(correction["value"])]
    if corrections:
        audio.save()
    return len(corrections)

# Apply metadata corrections to ID3 tags
def write_id3_tags(track_path, corrections):
    audio = File(track_path)

    # Verify the actual audio format
    if not isinstance(audio, (MP3, WAVE, AIFF)):
        raise ValueError("File does not use a supported ID3 container.")

    # Create an ID3 tag if one does not exist
    if audio.tags is None:
        audio.add_tags()
    if not isinstance(audio.tags, ID3):
        raise ValueError("File does not contain ID3 metadata.")

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
    for correction in corrections:
        field = correction["field"]
        value = str(correction["value"])

        # Track and disc totals share frames with their numbers
        if field in ("track_total", "disc_total"):
            frame_type = TRCK if field == "track_total" else TPOS
            existing = updates.get(frame_type.__name__) or audio.tags.get(frame_type.__name__)
            parser = parse_track_number if field == "track_total" else parse_disc_number
            number, total = parser(existing.text if existing else None)
            if number is None or total is not None:
                raise ValueError(f"Cannot safely add {field}.")
            value = f"{number}/{value}"
        else:
            frame_type = field_map.get(field)
            if frame_type is None:
                raise NotImplementedError(f"Unsupported field: {field}")
            if audio.tags.get(frame_type.__name__):
                raise ValueError(f"Existing {field} requires review.")
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
    audio = File(track_path, easy=True)

    # Verify the actual audio format
    if not isinstance(audio, EasyMP4):
        raise ValueError("File is not supported MP4 audio.")
    if audio.tags is None:
        audio.add_tags()

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
            key = field_map[field]
            if audio.tags.get(key) or key in updates:
                raise ValueError(f"Existing {field} requires review.")
            updates[key] = [value]

        # Handle track and disc numbers and totals
        elif field in ("track_number", "track_total",
                       "disc_number", "disc_total"):
            is_track = field.startswith("track_")
            key = "tracknumber" if is_track else "discnumber"
            parser = parse_track_number if is_track else parse_disc_number
            existing = updates.get(key, audio.tags.get(key, []))
            number, total = parser(existing)
            new_value = int(value)
            if not 1 <= new_value <= 65535:
                raise ValueError("Invalid numbering value.")
            if field.endswith("_number"):
                if number is not None:
                    raise ValueError(f"Existing {field} requires review.")
                number = new_value
            else:
                if total is not None:
                    raise ValueError(f"Existing {field} requires review.")
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

# Replace a reviewed text field on a temporary working copy only
def replace_reviewed_text_on_copy(track_path, working_folder, field, value):
    track_path = Path(track_path).resolve()
    working_folder = Path(working_folder).resolve()
    workspace = script_folder.parent.resolve()

    # Never permit this writer to touch staging or library originals
    if (
        working_folder.parent != workspace
        or not working_folder.name.startswith(".mixie-work-")
        or track_path.parent != working_folder
    ):
        raise PermissionError("Review replacement requires a working copy.")

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

    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
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
        if audio.tags is None:
            audio.add_tags()
        audio[allowed_fields[field]] = [value]

    elif family == "id3":
        audio = File(track_path)
        if not isinstance(audio, (MP3, WAVE, AIFF)):
            raise ValueError("Unexpected ID3 audio format.")
        if audio.tags is None:
            audio.add_tags()
        if not isinstance(audio.tags, ID3):
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
        if audio.tags is None:
            audio.add_tags()
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
    track_path = Path(track_path).resolve()
    working_folder = Path(working_folder).resolve()

    if (
        working_folder.parent != script_folder.parent.resolve()
        or not working_folder.name.startswith(".mixie-work-")
        or track_path.parent != working_folder
    ):
        raise PermissionError("Artwork replacement requires a working copy.")

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
        if audio.tags is None:
            audio.add_tags()

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
    else:
        pictures = saved.tags.getall("APIC")

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
    audio = File(track_path)
    if isinstance(audio, MP3):
        if audio.tags is None:
            audio.add_tags()
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

    for track in tracks:
        for field in fields:
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


# Propose corrections using an identified recording
def propose_identity_corrections(track, details, title_matches, artist_matches):
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
                "verified": False
            })

    return proposals

# Preview numbering corrections from a complete release match
def propose_release_numbering(matched, release_tracks):
    """Only fill numbering corroborated by a completely matched release.

    Existing contradictory tags are never silently overwritten.
    """
    positions = [(str(t["disc"]), str(t["number"])) for t in release_tracks]
    if len(positions) != len(set(positions)) or set(matched) != set(positions):
        raise ValueError("Album is not fully matched to the release.")

    discs = {int(disc) for disc, _ in positions}
    if not discs or discs != set(range(1, len(discs) + 1)):
        raise ValueError("Unusual disc numbering requires review.")
    disc_total = len(discs)
    album_total = len(positions)
    proposals = []

    for (disc, number), path in matched.items():
        metadata = read_track_metadata(path)
        if metadata is None:
            raise ValueError(f"Cannot read metadata: {path.name}")
        disc_number = int(disc)
        track_number = int(number)
        if track_number < 1:
            raise ValueError("Invalid release track number.")
        disc_tracks = sum(1 for d, _ in positions if int(d) == disc_number)

        current_number, _ = parse_track_number(metadata["track_number"])
        track_totals = get_track_totals(metadata)
        current_disc, _ = parse_disc_number(metadata["disc_number"])
        disc_totals = get_disc_totals(metadata)

        if current_number and int(current_number) != track_number:
            raise ValueError(f"Conflicting track number: {path.name}")
        valid_totals = ({disc_tracks}, {album_total}) if disc_total > 1 else ({disc_tracks},)
        if track_totals and track_totals not in valid_totals:
            raise ValueError(f"Conflicting track total: {path.name}")
        if current_disc is not None and current_disc != disc_number:
            raise ValueError(f"Conflicting disc number: {path.name}")
        if disc_totals and disc_totals != {disc_total}:
            raise ValueError(f"Conflicting disc total: {path.name}")

        for field, missing, value in (
            ("track_number", not current_number, track_number),
            ("track_total", not track_totals, disc_tracks),
            ("disc_number", current_disc is None, disc_number),
            ("disc_total", not disc_totals, disc_total),
        ):
            if missing:
                proposals.append({
                    "path": path, "field": field,
                    "value": value, "verified": False
                })

    return proposals

class MetadataConflictError(ValueError):
    def __init__(self, field, path, existing, expected):
        self.field = field
        self.path = str(path)
        self.existing = list(existing)
        self.expected = str(expected)

        super().__init__(
            f"Conflicting {field}: {path.name}\n"
            f"  Existing metadata: {tuple(existing)!r}\n"
            f"  Matched release: {(str(expected),)!r}"
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


def propose_required_album_metadata(matched, common, manual_fields=None):
    """Resolve date/genre conservatively, or defer without guessing.

    Explicitly reviewed overrides are applied later on temporary copies.
    Automatically reuse consistent existing tags; never select among
    conflicting originals or invent a missing genre.
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
            if value.strip()
        }
        # Album-level release date from all compatible editions takes
        # precedence; incompatible existing tags are handled separately.
        if field == "date" and common.get("date"):
            continue  # propose_common_metadata already handled this.
        if len(observed) != 1:
            unresolved.append(field)
            continue
        selected = next(iter(observed))
        for track in tracks:
            if not track[field]:
                proposals.append({
                    "path": track["path"], "field": field,
                    "value": selected, "verified": False
                })
    if unresolved:
        raise RequiredMetadataReview(unresolved)
    return proposals

# Preview shared album-level metadata corrections
def propose_common_metadata(
    matched, common_metadata, reviewed_decision=None
):
    proposals = []

    if reviewed_decision is not None:
        field = reviewed_decision.get("field")
        value = reviewed_decision.get("value")

        if (
            field not in ("album", "album_artist", "date", "genre")
            or reviewed_decision.get("status") != "pending_application"
            or not isinstance(value, str)
            or not value.strip()
            or reviewed_decision.get("reference_value")
                != str(common_metadata.get(field))
        ):
            raise ValueError(
                "Saved metadata decision needs fresh review."
            )

    for _, path in matched.items():
        metadata = read_track_metadata(path)
        if metadata is None:
            raise ValueError(f"Cannot read metadata: {path.name}")
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
                    def normalize_label(value):
                        value = str(value)
                        value = value.replace("’", "'").replace("‘", "'")
                        value = value.replace("“", '"').replace("”", '"')
                        return " ".join(value.casefold().split())

                    if normalize_label(current[0]) == normalize_label(expected):
                        continue

                # Treat "Vol. IV" and "Volume IV" as equivalent
                if field == "album" and len(current) == 1:
                    def normalize_volume_title(value):
                        value = " ".join(value.casefold().split())
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

                # A saved decision may resolve this exact conflict
                if (
                    reviewed_decision is not None
                    and reviewed_decision["field"] == field
                ):
                    if (
                        len(current) != 1
                        or current[0]
                            != reviewed_decision.get("existing_value")
                    ):
                        raise ValueError(
                            "Staged metadata changed since review."
                        )
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

    # Check every proposed file before making changes
    for proposal in proposals:
        track_path = Path(proposal["path"]).resolve()
        if not track_path.is_relative_to(lab_folder):
            raise PermissionError(f"File is outside MIXIE-Lab: {track_path}")

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
        corrections = grouped.setdefault(track_path, {})
        if field in corrections:
            raise ValueError(f"Duplicate correction: {field}")
        corrections[field] = proposal
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
