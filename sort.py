import os
import shutil
import tempfile
import zipfile
import stat
import ntpath
import constants
import hashlib
from pathlib import Path
import json

#Set safety limits for ZIP archives
MAX_ZIP_UNCOMPRESSED_BYTES = 10 * 1024**3  # 10 GiB
MAX_ZIP_MEMBERS = 10_000

#File routing
script_folder = os.path.dirname(os.path.abspath(__file__))
drive_folder = os.path.dirname(script_folder)
inbox_folder = os.path.join(script_folder, constants.inbox_folder_name)
music_library_folder = os.path.join(drive_folder, constants.library_folder_name)
staging_folder = os.path.join(script_folder,constants.staging_folder_name)

#Reject unsafe ZIP paths and archives that exceed the safety limits
def check_zip_safety(archive, temp_folder, filename):
    members = archive.infolist()
    seen_names = set()
    if len(members) > MAX_ZIP_MEMBERS:
        raise ValueError(f"Too many files in ZIP: {filename}")
    safe_root = os.path.abspath(temp_folder)
    total_uncompressed_size = 0
    for member in members:
        member_name = member.filename.replace("\\", "/")
        drive, _ = ntpath.splitdrive(member_name)
        path_parts = member_name.split("/")

        # Reject duplicate paths that could overwrite one another
        normalized_name = os.path.normpath(member_name).casefold()
        if normalized_name in seen_names:
            raise ValueError(f"Duplicate ZIP entry: {member_name}")
        seen_names.add(normalized_name)

        # Reject symbolic links stored inside ZIP archives
        unix_mode = member.external_attr >> 16
        if stat.S_ISLNK(unix_mode):
            raise ValueError(f"ZIP contains a symbolic link: {member_name}")
        if (
            not member_name
            or drive
            or member_name.startswith("/")
            or ".." in path_parts
        ):
            raise ValueError(f"Unsafe path in {filename}: {member.filename}")
        destination = os.path.abspath(os.path.join(safe_root, *path_parts))
        try:
            stays_inside_temp = (
                os.path.commonpath((safe_root, destination)) == safe_root
            )
        except ValueError:
            stays_inside_temp = False
        if not stays_inside_temp:
            raise ValueError(f"Unsafe path in {filename}: {member.filename}")
        total_uncompressed_size += member.file_size
        if total_uncompressed_size > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ValueError(f"ZIP expands beyond the 10 GiB limit: {filename}")

# Process new music imports from the Inbox
def sort_inbox():

    # Never restage an Inbox source while its previous cleanup
    # transaction is incomplete.
    quarantine_root = (
        Path(script_folder)
        / constants.backup_folder_name
        / ".cleanup-quarantine"
    )

    if quarantine_root.is_symlink():
        raise RuntimeError("Unsafe cleanup quarantine path.")

    if quarantine_root.exists():
        if not quarantine_root.is_dir():
            raise RuntimeError("Invalid cleanup quarantine.")

        for folder in quarantine_root.iterdir():
            if not folder.name.startswith(".mixie-inbox-"):
                continue

            if folder.is_symlink() or not folder.is_dir():
                raise RuntimeError(
                    f"Unsafe cleanup transaction: {folder}"
                )

            journal = folder / "recovery.json"
            held_stage = folder / "staging"
            held_source = folder / "original"

            if (
                journal.is_symlink()
                or held_stage.is_symlink()
                or held_source.is_symlink()
            ):
                raise RuntimeError("Unsafe cleanup recovery paths.")

            if not journal.is_file():
                if any(folder.iterdir()):
                    raise RuntimeError(
                        f"Incomplete cleanup journal: {folder}"
                    )
                continue

            # Both held means fully quarantined.
            # Anything else requires recovery before ingesting.
            if not (
                held_stage.exists()
                and held_source.exists()
            ):
                raise RuntimeError(
                    "Interrupted Inbox cleanup detected. "
                    "Restore the pending transaction before ingestion."
                )

    # Prepare the directories used for importing
    os.makedirs(inbox_folder, exist_ok=True)
    os.makedirs(staging_folder, exist_ok=True)
    staged_imports = []
    for filename in sorted(os.listdir(inbox_folder)):
        source_path = os.path.join(inbox_folder, filename)

        # Never follow symbolic links from Inbox
        if os.path.islink(source_path):
            print(f"Skipping symbolic link: {filename}")
            continue
        is_folder = os.path.isdir(source_path)
        is_zip = (
            os.path.isfile(source_path)
            and filename.lower().endswith(".zip")
        )
        is_music_file = (
            os.path.isfile(source_path)
            and filename.lower().endswith(constants.music_file_types)
        )
        if not (is_folder or is_zip or is_music_file):
            print(f"Unsupported Inbox item: {filename}")
            continue

        # Each import gets its own staging folder
        import_name = os.path.splitext(filename)[0] if is_zip else filename
        import_folder = os.path.join(staging_folder, import_name)
        if os.path.lexists(import_folder):
            print(f"Already staged, skipping: {filename}")
            continue

        # Work in a temporary folder until the import is complete
        pending_folder = tempfile.mkdtemp(
            prefix=".mixie-pending-",
            dir=staging_folder
        )
        try:
            if is_zip:
                with zipfile.ZipFile(source_path, "r") as archive:
                    check_zip_safety(archive, pending_folder, filename)
                    bad_file = archive.testzip()
                    if bad_file is not None:
                        raise ValueError(f"Damaged ZIP entry: {bad_file}")
                    archive.extractall(pending_folder)
            elif is_folder:
                # Do not follow links hidden inside folders
                for root, directories, files in os.walk(source_path):
                    for name in directories + files:
                        item_path = os.path.join(root, name)

                        if os.path.islink(item_path):
                            raise ValueError(
                                f"Symbolic link found: {item_path}"
                            )
                shutil.copytree(
                    source_path,
                    pending_folder,
                    dirs_exist_ok=True
                )
            else:
                # Stage a loose audio file without modifying it
                shutil.copy2(
                    source_path,
                    os.path.join(pending_folder, filename)
                )

            # Check for at least one supported audio file
            music_found = any(
                name.lower().endswith(constants.music_file_types)
                for _, _, names in os.walk(pending_folder)
                for name in names
            )
            if not music_found:
                raise ValueError("No supported music files found")

            # Never replace an existing staging folder
            if os.path.lexists(import_folder):
                raise FileExistsError(import_folder)

            # Commit the completed import
            os.rename(pending_folder, import_folder)
            staged_imports.append(import_folder)
            print(f"Successfully staged: {filename}")
        except ( OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            print(f"Import failed: {filename} - {error}")
        finally:

            # Remove any incomplete temporary import
            if os.path.exists(pending_folder):
                shutil.rmtree(pending_folder)

    print(f"Staging complete: {len(staged_imports)} imports")
    return staged_imports

# Make artist, album, and track names safe for filenames
def safe_path_part(value):
    name = str(value).strip()

    # Replace characters forbidden on common filesystems
    for character in '<>:"/\\|?*':
        name = name.replace(character, "_")

    # Remove control characters
    name = "".join(
        "_" if ord(character) < 32 else character
        for character in name
    )
    name = name.strip(" .")
    if not name:
        raise ValueError("Empty filename component")

    # Avoid Windows reserved names
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{i}" for i in range(1, 10))
    reserved.update(f"LPT{i}" for i in range(1, 10))
    if name.split(".")[0].upper() in reserved:
        name = "_" + name
    return name[:100].rstrip(" .")

# Read disc numbers such as 1, "1", or "1/2"
def parse_disc_info(value):
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    if value is None or str(value).strip() == "":
        return None, None
    parts = str(value).split("/", 1)
    disc = int(parts[0])
    total = int(parts[1]) if len(parts) == 2 else None
    if disc < 1 or (total is not None and total < disc):
        raise ValueError("Invalid disc information")
    return disc, total

# Verify that a track is safe to copy
def validate_source_track(track_path, source_root):
    root = os.path.realpath(source_root)
    source = os.path.realpath(track_path)

    # Prevent reading files outside the permitted folder
    if os.path.commonpath((root, source)) != root:
        raise ValueError("Track is outside the source folder")
    if not os.path.isfile(source):
        raise FileNotFoundError(f"Track not found: {source}")
    if not source.lower().endswith(constants.music_file_types):
        raise ValueError("Unsupported audio format")
    return source

# Calculate a file's checksum
def file_checksum(path):
    checksum = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.digest()

# Copy an approved sorting plan into the library
def copy_sorting_plan(planned_tracks):
    copied_files = []
    library_root = os.path.realpath(music_library_folder)

    try:
        for track in planned_tracks:
            source = track["source"]
            destination = os.path.abspath(track["destination"])
            destination_folder = os.path.dirname(destination)

            # Prevent copying outside Music Library
            resolved_folder = os.path.realpath(destination_folder)
            if os.path.commonpath(
                (library_root, resolved_folder)
            ) != library_root:
                raise ValueError("Destination outside Music Library")
            os.makedirs(destination_folder, exist_ok=True)

            # Exclusive creation prevents overwriting files
            with open(source, "rb") as original:
                with open(destination, "xb") as copied:
                    copied_files.append(destination)
                    shutil.copyfileobj(original, copied)

            # Verify that both files have identical contents
            if file_checksum(source) != file_checksum(destination):
                raise IOError(f"Copy verification failed: {destination}")

            print(f"Copied and verified: {destination}")
    except (OSError, ValueError):

        # Remove only files created during this operation
        for path in reversed(copied_files):
            try:
                os.remove(path)
            except OSError as error:
                print(f"Rollback failed for {path}: {error}")
        raise
    return copied_files

def commit_verified_plan(plan):
    """Copy missing files, accept identical files, reject conflicts."""
    if not plan:
        raise ValueError("Empty library commit plan.")

    library_root = os.path.realpath(music_library_folder)
    pending = []
    already_present = []
    destinations = set()

    # Check the entire album before writing anything
    for item in plan:
        source = os.path.abspath(item["source"])
        destination = os.path.abspath(item["destination"])
        key = os.path.normpath(destination).casefold()

        if key in destinations:
            raise ValueError(f"Duplicate destination: {destination}")
        destinations.add(key)

        if not os.path.isfile(source) or os.path.islink(source):
            raise ValueError(f"Invalid source: {source}")

        parent = os.path.realpath(os.path.dirname(destination))
        if os.path.commonpath((library_root, parent)) != library_root:
            raise PermissionError("Destination outside Music Library.")

        if source == destination:
            raise ValueError("Source and destination are identical.")

        if os.path.lexists(destination):
            if os.path.islink(destination) or not os.path.isfile(destination):
                raise FileExistsError(f"Unsafe destination: {destination}")

            if file_checksum(source) != file_checksum(destination):
                raise FileExistsError(
                    f"Different file already exists: {destination}"
                )

            already_present.append(destination)
            continue

        # Also detect case-insensitive filename collisions
        directory = os.path.dirname(destination)
        if os.path.isdir(directory):
            filename = os.path.basename(destination).casefold()
            if any(name.casefold() == filename
                   for name in os.listdir(directory)):
                raise FileExistsError(
                    f"Filename conflict: {destination}"
                )

        pending.append(item)

    print(f"Already verified: {len(already_present)} files")
    print(f"New files to copy: {len(pending)}")

    # Existing copier verifies checksums and rolls back copy errors
    created = copy_sorting_plan(pending) if pending else []

    return created, already_present

# Discover artwork and extras inside a staged import
def scan_import_assets(import_folder):
    assets = []
    root = os.path.realpath(import_folder)
    staging_root = os.path.realpath(staging_folder)
    if (
        root == staging_root
        or os.path.commonpath((staging_root, root)) != staging_root
    ):
        raise ValueError("Import must be inside Staging")
    if not os.path.isdir(root):
        raise FileNotFoundError(root)
    for folder_path, directories, filenames in os.walk(root):

        # Reject symbolic links
        for name in directories + filenames:
            path = os.path.join(folder_path, name)
            if os.path.islink(path):
                raise ValueError(f"Symbolic link found: {path}")
        for filename in filenames:

            # Audio files are handled separately
            if filename.lower().endswith(constants.music_file_types):
                continue
            path = os.path.join(folder_path, filename)
            assets.append({
                "path": path,
                "relative_path": os.path.relpath(path, root)
            })

    return assets

# Plan where verified album artwork and extras belong
def plan_album_assets(verified_assets, source_root):
    planned_assets = []
    seen_destination_sources = {}
    root = os.path.realpath(source_root)
    primary_covers = {
        "cover.jpg", "cover.png",
        "folder.jpg", "folder.png",
        "front.jpg", "front.png"
    }
    for asset in verified_assets:
        source = os.path.realpath(asset["path"])
        if os.path.commonpath((root, source)) != root:
            raise ValueError("Asset is outside the source folder")
        if not os.path.isfile(source):
            raise FileNotFoundError(source)

        # Preserve the original subfolder structure
        relative_path = asset["relative_path"].replace("\\", "/")
        parts = relative_path.split("/")

        # Reject absolute paths
        if relative_path.startswith("/") or ntpath.splitdrive(relative_path)[0]:
            raise ValueError("Absolute asset path")

        # Ensure the filename matches the actual source
        if parts[-1].casefold() != os.path.basename(source).casefold():
            raise ValueError("Asset filename mismatch")

        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("Unsafe asset path")
        album = safe_path_part(asset["album"])
        album_folder = os.path.join(
            music_library_folder,
            safe_path_part(asset["album_artist"]),
            album
        )

        # Source may already be organized in a library extras folder.
        # Strip precisely one redundant album extras prefix.
        extras_name = f"{album} Extras"
        if len(parts) > 1 and parts[0].casefold() == extras_name.casefold():
            parts = parts[1:]

        # Put primary artwork in the album folder
        if len(parts) == 1 and parts[0].casefold() in primary_covers:
            destination = os.path.join(
                album_folder,
                safe_path_part(parts[0])
            )
        else:
            destination = os.path.join(
                album_folder,
                f"{album} Extras",
                *(safe_path_part(part) for part in parts)
            )
        # Identical source covers can map to the same normalized path.
        # Preserve one copy; conflicting bytes must stop the import.
        destination_key = os.path.normpath(destination).casefold()
        previous_source = seen_destination_sources.get(destination_key)
        if previous_source is not None:
            if file_checksum(previous_source) != file_checksum(source):
                raise ValueError(
                    f"Different artwork files share one destination: {destination}"
                )
            continue
        seen_destination_sources[destination_key] = source
        planned_assets.append({
            "source": source,
            "destination": destination
        })

    return planned_assets

# Organize verified music into the main library
def plan_library_sort(verified_tracks, approved=False, source_root=None, check_existing=True):

    if source_root is None:
        source_root = staging_folder

    # Never modify the library without approval
    if not approved:
        print("Library sorting requires approval.")
        return []

    # Identify albums that contain multiple discs
    multi_disc_albums = set()
    for track in verified_tracks:
        try:
            disc, total = parse_disc_info(
                track.get("disc_number")
            )
        except (ValueError, TypeError):
            continue
        if (disc is not None and disc > 1) or (
            total is not None and total > 1
        ):
            album_key = (
                str(track.get("album_artist", "")).casefold(),
                str(track.get("album", "")).casefold()
            )
            multi_disc_albums.add(album_key)

    planned_tracks = []

    # Track destinations we've already planned
    planned_destinations = set()
    for track in verified_tracks:

        # Information required to organize a track
        required_fields = (
            "path",
            "album_artist",
            "album",
            "title",
            "track_number"
        )
        missing_fields = [
            field for field in required_fields
            if not track.get(field)
        ]
        if missing_fields:
            print(f"Incomplete track metadata: {missing_fields}")
            continue

        try:
            # Verify the source file before planning its destination
            source_path = validate_source_track(
                track["path"],
                source_root
            )

            # Accept track numbers like 4, "04", or "04/11"
            number = track["track_number"]
            if isinstance(number, (tuple, list)):
                number = number[0]
            number = int(str(number).split("/", 1)[0])
            if number < 1:
                raise ValueError("Invalid track number")

            # Preserve the original audio format
            extension = os.path.splitext(str(track["path"]))[1].lower()
            if extension not in constants.music_file_types:
                raise ValueError("Unsupported audio format")

            # Build the final library destination
            destination_folder = os.path.join(
                drive_folder,
                constants.library_folder_name,
                safe_path_part(track["album_artist"]),
                safe_path_part(track["album"])
            )

            # Check whether this album needs disc folders
            album_key = (
                str(track["album_artist"]).casefold(),
                str(track["album"]).casefold()
            )
            disc, total = parse_disc_info(
                track.get("disc_number")
            )
            if album_key in multi_disc_albums:
                if disc is None:
                    raise ValueError(
                        "Missing disc number on multi-disc album"
                    )
                destination_folder = os.path.join(
                    destination_folder,
                    f"Disc {disc}"
                )
            filename = (
                f"{number:02d} - "
                f"{safe_path_part(track['title'])}{extension}"
            )
            destination = os.path.join(destination_folder, filename)
        except (OSError, ValueError, TypeError, IndexError) as error:
            print(f"Cannot organize track: {error}")
            continue

        # Compare paths without case sensitivity
        destination_key = os.path.normpath(destination).casefold()

        # Check for collisions within the current sorting plan
        if destination_key in planned_destinations:
            print(f"Duplicate destination: {destination}")
            continue

        # Check for files already in the library
        if check_existing and os.path.isdir(destination_folder):
            existing_files = {
                name.casefold()
                for name in os.listdir(destination_folder)
            }
            if filename.casefold() in existing_files:
                print(f"Already exists in library: {destination}")
                continue

        # Reserve this destination
        planned_destinations.add(destination_key)
        planned_tracks.append({
            **track,
            "source": source_path,
            "destination": destination
        })
        print(f"Planned: {destination}")

    # Never approve an incomplete sorting plan
    if len(planned_tracks) != len(verified_tracks):
        print("Sorting canceled: incomplete or conflicting tracks.")
        return []

    # Return the complete plan without copying anything
    return planned_tracks

# Copy verified new music into Music Library
def sort_library(
    verified_tracks, approved=False, source_root=None, verified_assets=None):
    if not approved:
        print("Library sorting requires approval.")
        return []

    # Require album assets to be reviewed before sorting
    if verified_assets is None:
        print("Album assets have not been reviewed.")
        return []

    if not verified_tracks:
        return []

    # Ensure every asset belongs to an album being sorted
    track_albums = {
        (
            str(track["album_artist"]).strip().casefold(),
            str(track["album"]).strip().casefold()
        )
        for track in verified_tracks
    }

    for asset in verified_assets:
        asset_album = (
            str(asset.get("album_artist", "")).strip().casefold(),
            str(asset.get("album", "")).strip().casefold()
        )

        if asset_album not in track_albums:
            print(
                f"Asset does not belong to this sorting batch: "
                f"{asset.get('path')}"
            )
            return []

    plan = plan_library_sort(
        verified_tracks,
        approved=True,
        source_root=source_root
    )
    if len(plan) != len(verified_tracks):
        return []

    # Plan the album artwork and extras
    asset_plan = []

    if verified_assets:
        try:
            asset_plan = plan_album_assets(
                verified_assets,
                source_root or staging_folder
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Cannot organize album assets: {error}")
            return []

    # Combine audio and assets into one copying operation
    complete_plan = plan + asset_plan

    # Check every destination before copying anything
    destinations = set()
    for item in complete_plan:
        destination = item["destination"]
        destination_key = os.path.normpath(destination).casefold()
        parent = os.path.dirname(destination)
        filename = os.path.basename(destination).casefold()

        if destination_key in destinations:
            print(f"Duplicate destination: {destination}")
            return []

        if os.path.lexists(destination):
            print(f"File already exists: {destination}")
            return []

        if os.path.isdir(parent):
            if filename in {name.casefold() for name in os.listdir(parent)}:
                print(f"Filename conflict: {destination}")
                return []
        destinations.add(destination_key)

    try:
        copied_files = copy_sorting_plan(complete_plan)
    except (OSError, ValueError) as error:
        print(f"Library sorting failed: {error}")
        return []

    print(
        f"Library sorting complete: {len(plan)} tracks, "
        f"{len(asset_plan)} assets"
    )
    return plan

# Plan repairs to the existing library structure
def plan_library_reorganization(verified_tracks):
    plan = plan_library_sort(
        verified_tracks,
        approved=True,
        source_root=music_library_folder,
        check_existing=False
    )
    if len(plan) != len(verified_tracks):
        return None
    changes = []
    for track in plan:
        source = track["source"]
        destination = track["destination"]

        # Leave correctly organized music alone
        if os.path.abspath(source) == os.path.abspath(destination):
            print(f"Already organized: {destination}")
            continue

        # Never overwrite another existing file
        if os.path.lexists(destination):
            print(f"Destination occupied: {destination}")
            return None
        changes.append(track)
        print(f"Would reorganize: {source} -> {destination}")
    print(f"Reorganization plan: {len(changes)} tracks")
    return changes

# Plan repairs to existing library artwork and extras
def plan_library_asset_reorganization(verified_assets):
    plan = plan_album_assets(
        verified_assets,
        music_library_folder
    )
    changes = []
    for asset in plan:
        source = os.path.abspath(asset["source"])
        destination = os.path.abspath(asset["destination"])

        # Artwork already in the correct location
        if source == destination:
            print(f"Already organized: {destination}")
            continue

        # Never overwrite existing artwork
        if os.path.lexists(destination):
            raise FileExistsError(
                f"Asset destination occupied: {destination}"
            )
        changes.append(asset)
        print(f"Would reorganize asset: {source} -> {destination}")
    return changes


# Reorganize existing library music and associated assets
def reorganize_library(
    verified_tracks, approved=False, verified_assets=None
):
    if not approved:
        print("Library reorganization requires approval.")
        return []
    if verified_assets is None:
        print("Album assets have not been reviewed.")
        return []
    if not verified_tracks and not verified_assets:
        print("Nothing supplied for reorganization.")
        return []

    # Plan track reorganization
    changes = (
        plan_library_reorganization(verified_tracks)
        if verified_tracks else []
    )
    if changes is None:
        print("Track reorganization plan failed.")
        return []

    # Plan artwork and extras reorganization
    try:
        asset_changes = plan_library_asset_reorganization(
            verified_assets
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Asset reorganization failed: {error}")
        return []
    complete_plan = changes + asset_changes
    if not complete_plan:
        print("Everything is already organized.")
        return []

    # Check for conflicting destinations
    destinations = set()
    for item in complete_plan:
        destination = item["destination"]
        key = os.path.normpath(destination).casefold()
        if key in destinations or os.path.lexists(destination):
            print(f"Destination conflict: {destination}")
            return []
        parent = os.path.dirname(destination)
        if os.path.isdir(parent):
            existing = {
                name.casefold() for name in os.listdir(parent)
            }
            if os.path.basename(destination).casefold() in existing:
                print(f"Filename conflict: {destination}")
                return []
        destinations.add(key)

    # Copy and verify the entire batch
    try:
        copied_files = copy_sorting_plan(complete_plan)
    except (OSError, ValueError) as error:
        print(f"Library reorganization failed: {error}")
        return []
    print(
        f"Reorganization complete: {len(changes)} tracks, "
        f"{len(asset_changes)} assets."
    )
    print("Original files preserved pending archiving.")
    return complete_plan


# Inventory files without following symbolic links
def file_manifest(folder):
    root = Path(folder)

    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Unsafe directory: {root}")

    manifest = {}

    for current, directories, filenames in os.walk(root):
        for name in directories + filenames:
            path = Path(current) / name

            if path.is_symlink():
                raise ValueError(f"Symbolic link: {path}")

        for name in filenames:
            path = Path(current) / name

            if not path.is_file():
                raise ValueError(f"Unsupported file: {path}")

            relative = path.relative_to(root).as_posix()
            manifest[relative] = file_checksum(path).hex()

    return manifest


# Inspect ZIP contents using SHA-256, not just ZIP CRC
def zip_manifest(path):
    result = {}
    seen = set()

    with zipfile.ZipFile(path) as saved:
        for member in saved.infolist():
            name = member.filename.replace("\\", "/")

            if member.is_dir():
                continue

            parts = name.split("/")

            if (
                not name
                or name.startswith("/")
                or ntpath.splitdrive(name)[0]
                or any(part in ("", ".", "..") for part in parts)
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError(f"Unsafe ZIP entry: {name}")

            if name.casefold() in seen:
                raise ValueError(f"Duplicate ZIP entry: {name}")

            seen.add(name.casefold())
            digest = hashlib.sha256()

            with saved.open(member) as source:
                for chunk in iter(
                    lambda: source.read(1024 * 1024), b""
                ):
                    digest.update(chunk)

            result[name] = digest.hexdigest()

    return result

# Find fully completed source imports
def plan_ingest_cleanup(completed_keys):
    import tag

    completed_keys = set(completed_keys)
    grouped = {}

    for key, album in tag.group_staged_albums().items():
        grouped.setdefault(key[0], []).append((key, album))

    inbox_sources = {}

    for source in Path(inbox_folder).iterdir():
        if source.is_symlink():
            continue

        if source.is_dir():
            name = source.name
        elif source.is_file() and source.suffix.lower() == ".zip":
            name = source.stem
        elif (
            source.is_file()
            and source.suffix.lower() in constants.music_file_types
        ):
            name = source.name
        else:
            continue

        inbox_sources.setdefault(name, []).append(source)

    ready = []

    for name, albums in grouped.items():
        sources = inbox_sources.get(name, [])

        if len(sources) != 1:
            print(f"Cleanup deferred, ambiguous source: {name}")
            continue

        if not all(key in completed_keys for key, _ in albums):
            print(f"Cleanup deferred, incomplete import: {name}")
            continue

        source = sources[0]
        stage = Path(staging_folder) / name

        try:
            staged_files = file_manifest(stage)

            # All supported tracks must be accounted for
            actual_music = {
                filename
                for filename in staged_files
                if Path(filename).suffix.lower()
                in constants.music_file_types
            }

            discovered_music = {
                Path(track["path"]).relative_to(stage).as_posix()
                for _, album in albums
                for track in album["tracks"]
            }

            if not actual_music or actual_music != discovered_music:
                raise ValueError("Unaccounted staged music.")

            # Don't discard formats MIXIE cannot ingest yet
            unsupported_audio = {
                ".wav", ".aif", ".aiff", ".m4a", ".aac",
                ".ogg", ".opus", ".wma", ".ape", ".wv",
                ".dsf", ".dff"
            }

            if any(
                Path(filename).suffix.lower() in unsupported_audio
                for filename in staged_files
            ):
                raise ValueError("Unsupported audio remains in staging.")

            # Verify staging still matches the original download
            if source.is_dir():
                original_files = file_manifest(source)
            elif source.suffix.lower() == ".zip":
                original_files = zip_manifest(source)
            else:
                original_files = {
                    source.name: file_checksum(source).hex()
                }

            if staged_files != original_files:
                raise ValueError("Staging differs from original download.")

        except (OSError, ValueError, zipfile.BadZipFile) as error:
            print(f"Cleanup blocked: {name}: {error}")
            continue

        ready.append({
            "name": name,
            "source": source,
            "stage": stage,
            "albums": albums,
            "original_hashes": original_files,
            "staged_hashes": staged_files
        })

        print(f"Cleanup eligible: {name} ({len(albums)} album(s))")

    return ready


# Preview cleanup of completed imports whose originals exist
# inside the verified cumulative archive, but not Inbox.
def plan_archived_cleanup(completed, originals_zip):
    import tag

    bulk = Path(originals_zip).resolve()
    expected = (
        Path(script_folder)
        / constants.backup_folder_name
        / "OriginalDownloads-Bulk.zip"
    ).resolve()

    if bulk != expected or not bulk.is_file():
        raise ValueError("Verified cumulative archive required.")

    # Verify the entire outer archive before trusting its entries
    archived = zip_manifest(bulk)

    grouped = {}
    for key, album in tag.group_staged_albums().items():
        grouped.setdefault(key[0], []).append((key, album))

    ready = []
    inbox = Path(inbox_folder)

    with zipfile.ZipFile(bulk) as outer:
        for name, albums in sorted(grouped.items()):
            # Imports with a current Inbox source use the old planner
            if any(
                item.name == name
                or (
                    item.suffix.lower() == ".zip"
                    and item.stem == name
                )
                for item in inbox.iterdir()
            ):
                continue

            if not all(key in completed for key, _ in albums):
                print(f"Archived cleanup deferred: {name} (unfinished)")
                continue

            archive_name = f"{name}.zip"
            if archive_name not in archived:
                print(f"Archived cleanup deferred: {name} (no ZIP)")
                continue

            stage = Path(staging_folder) / name

            try:
                staged = file_manifest(stage)
                if not staged:
                    raise ValueError("Empty staged import")

                actual_music = {
                    relative for relative in staged
                    if Path(relative).suffix.lower()
                    in constants.music_file_types
                }

                discovered_music = {
                    Path(track["path"]).relative_to(stage).as_posix()
                    for _, album in albums
                    for track in album["tracks"]
                }

                if actual_music != discovered_music:
                    raise ValueError("Unaccounted staged music")

                unsupported_audio = {
                    ".wav", ".aif", ".aiff", ".m4a",
                    ".aac", ".ogg", ".opus", ".wma",
                    ".ape", ".wv", ".dsf", ".dff"
                }

                if any(
                    Path(relative).suffix.lower() in unsupported_audio
                    for relative in staged
                ):
                    raise ValueError("Unsupported audio remains")

                with tempfile.TemporaryFile() as temporary:
                    with outer.open(archive_name) as source:
                        shutil.copyfileobj(
                            source, temporary, 1024 * 1024
                        )

                    temporary.seek(0)
                    with zipfile.ZipFile(temporary) as inner:
                        check_zip_safety(
                            inner, str(stage), archive_name
                        )

                    temporary.seek(0)
                    original = zip_manifest(temporary)

                if original != staged:
                    raise ValueError("Archived original differs from staging")

                for key, album in albums:
                    receipt = completed.get(key)
                    if (
                        not isinstance(receipt, dict)
                        or len(receipt) < len(album["tracks"]) + 1
                    ):
                        raise ValueError("Incomplete import receipt")

            except (OSError, ValueError, RuntimeError,
                    zipfile.BadZipFile) as error:
                print(f"Archived cleanup blocked: {name}: {error}")
                continue

            ready.append({
                "name": name,
                "stage": stage,
                "archived_source": archive_name,
                "albums": albums
            })

            print(f"Archived cleanup eligible: {name}")

    return ready

# Verify archived-source cleanup candidates without deleting anything
def verify_archived_cleanup(completed, originals_zip, library_zip):
    backup_root = (
        Path(script_folder) / constants.backup_folder_name
    ).resolve()

    bulk = Path(originals_zip)
    snapshot = Path(library_zip)

    if (
        bulk.is_symlink()
        or not bulk.is_file()
        or bulk.resolve() != backup_root / "OriginalDownloads-Bulk.zip"
    ):
        raise ValueError("Invalid original-download archive.")

    if (
        snapshot.is_symlink()
        or not snapshot.is_file()
        or snapshot.parent.resolve() != backup_root
        or not snapshot.name.startswith("MusicLibrary-")
        or snapshot.suffix.lower() != ".zip"
    ):
        raise ValueError("Invalid library snapshot.")

    # Verify originals still present in Inbox are archived
    archived_originals = zip_manifest(bulk)
    current_originals = file_manifest(inbox_folder)

    for name, checksum in current_originals.items():
        if archived_originals.get(name) != checksum:
            raise RuntimeError(
                f"Original download is not archived: {name}"
            )

    # The snapshot must contain the current Music Library exactly
    library_root = Path(music_library_folder).resolve()
    current_library = file_manifest(library_root)
    archived_library = zip_manifest(snapshot)

    if current_library != archived_library:
        raise RuntimeError("Music Library snapshot is out of date.")

    candidates = plan_archived_cleanup(completed, bulk)
    verified = []

    for item in candidates:
        stage = Path(item["stage"])

        if stage.is_symlink() or not stage.is_dir():
            raise ValueError("Invalid staged import.")

        staged_hashes = file_manifest(stage)

        for key, album in item["albums"]:
            receipt = completed.get(key)

            if (
                not isinstance(receipt, dict)
                or len(receipt) < len(album["tracks"]) + 1
            ):
                raise RuntimeError(
                    f"Incomplete library receipt: {key}"
                )

            for destination, checksum in receipt.items():
                path = Path(destination)

                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(library_root)
                ):
                    raise RuntimeError(
                        f"Unsafe library destination: {destination}"
                    )

                relative = path.resolve().relative_to(
                    library_root
                ).as_posix()

                if (
                    current_library.get(relative) != checksum
                    or archived_library.get(relative) != checksum
                ):
                    raise RuntimeError(
                        f"Library receipt mismatch: {relative}"
                    )

        verified.append({
            **item,
            "staged_hashes": staged_hashes,
            "archived_source_hash": archived_originals[
                item["archived_source"]
            ]
        })

        print(f"Fully verified for cleanup: {item['name']}")

    print(f"\nVerified cleanup candidates: {len(verified)}")
    print("PREVIEW ONLY: No files removed.")
    return verified

def verify_inbox_cleanup(completed, originals_zip, library_zip):
    """Preview Inbox-backed cleanup with full archive verification."""
    backup_root = (
        Path(script_folder) / constants.backup_folder_name
    ).resolve()

    bulk = Path(originals_zip)
    snapshot = Path(library_zip)

    if (
        bulk.is_symlink()
        or not bulk.is_file()
        or bulk.resolve() != backup_root / "OriginalDownloads-Bulk.zip"
    ):
        raise ValueError("Invalid original-download archive.")

    if (
        snapshot.is_symlink()
        or not snapshot.is_file()
        or snapshot.parent.resolve() != backup_root
        or not snapshot.name.startswith("MusicLibrary-")
        or snapshot.suffix.lower() != ".zip"
    ):
        raise ValueError("Invalid library snapshot.")

    archived_originals = zip_manifest(bulk)
    current_inbox = file_manifest(inbox_folder)

    # All remaining Inbox files must be in the bulk archive.
    for relative, digest in current_inbox.items():
        if archived_originals.get(relative) != digest:
            raise RuntimeError(
                f"Inbox file is not safely archived: {relative}"
            )

    library_root = Path(music_library_folder).resolve()
    current_library = file_manifest(library_root)
    archived_library = zip_manifest(snapshot)

    if current_library != archived_library:
        raise RuntimeError("Library backup is not current.")

    candidates = plan_ingest_cleanup(completed)
    ready = []

    for item in candidates:
        source = Path(item["source"])
        stage = Path(item["stage"])

        if (
            source.is_symlink()
            or stage.is_symlink()
            or source.parent.resolve() != Path(inbox_folder).resolve()
            or stage.parent.resolve() != Path(staging_folder).resolve()
        ):
            raise RuntimeError("Unsafe import source path.")

        # Verify the actual original source is represented in bulk.
        if source.is_dir():
            originals = file_manifest(source)

            if originals != item["original_hashes"]:
                raise RuntimeError("Original folder changed.")

            for relative, digest in originals.items():
                archived_name = f"{source.name}/{relative}"
                if archived_originals.get(archived_name) != digest:
                    raise RuntimeError(
                        f"Missing archived original: {archived_name}"
                    )
        else:
            digest = file_checksum(source).hex()

            if archived_originals.get(source.name) != digest:
                raise RuntimeError(
                    f"Original file not archived: {source.name}"
                )

            originals = (
                zip_manifest(source)
                if source.suffix.lower() == ".zip"
                else {source.name: digest}
            )

            if originals != item["original_hashes"]:
                raise RuntimeError("Original file contents changed.")

        if file_manifest(stage) != item["staged_hashes"]:
            raise RuntimeError("Staging changed.")

        # Every completed album must have valid library receipts.
        for key, album in item["albums"]:
            receipt = completed.get(key)

            if (
                not isinstance(receipt, dict)
                or len(receipt) < len(album["tracks"]) + 1
            ):
                raise RuntimeError(
                    f"Incomplete library receipt: {key}"
                )

            for destination, digest in receipt.items():
                path = Path(destination)

                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(library_root)
                ):
                    raise RuntimeError(
                        f"Unsafe library file: {destination}"
                    )

                relative = path.resolve().relative_to(
                    library_root
                ).as_posix()

                if (
                    current_library.get(relative) != digest
                    or archived_library.get(relative) != digest
                ):
                    raise RuntimeError(
                        f"Library receipt mismatch: {relative}"
                    )

        ready.append(item)
        print(f"Inbox-backed cleanup verified: {item['name']}")

    print(f"\nVerified Inbox-backed imports: {len(ready)}")
    print("PREVIEW ONLY: Nothing moved or deleted.")
    return ready

def quarantine_inbox_cleanup(
    completed, originals_zip, library_zip, approved=False
):
    """Quarantine verified Inbox originals and staged copies."""
    ready = verify_inbox_cleanup(
        completed, originals_zip, library_zip
    )

    if not approved:
        print("Inbox quarantine preview only. Nothing moved.")
        return ready

    if not ready:
        return []

    backup_root = (
        Path(script_folder) / constants.backup_folder_name
    ).resolve()
    quarantine_root = backup_root / ".cleanup-quarantine"

    if quarantine_root.is_symlink():
        raise ValueError("Unsafe quarantine directory.")

    quarantine_root.mkdir(parents=True, exist_ok=True)

    inbox_root = Path(inbox_folder).resolve()
    staged_root = Path(staging_folder).resolve()

    def sync_directory(directory):
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    moved = []

    for item in ready:
        source = Path(item["source"])
        stage = Path(item["stage"])
        name = item["name"]

        if (
            source.is_symlink()
            or stage.is_symlink()
            or not source.exists()
            or not stage.is_dir()
            or source.parent.resolve() != inbox_root
            or stage.parent.resolve() != staged_root
            or stage.name != name
        ):
            raise RuntimeError(f"Unsafe cleanup candidate: {name}")

        # Never start another cleanup over an unresolved journal.
        for existing in quarantine_root.glob(
            ".mixie-inbox-*/recovery.json"
        ):
            if existing.is_symlink():
                raise RuntimeError("Unsafe recovery journal.")

            with existing.open(encoding="utf-8") as file:
                previous = json.load(file)

            if previous.get("import_name") == name:
                raise RuntimeError(
                    f"Existing cleanup requires recovery: {name}"
                )

        # Reverify eligibility immediately before the move.
        fresh = verify_inbox_cleanup(
            completed, originals_zip, library_zip
        )

        if name not in {entry["name"] for entry in fresh}:
            raise RuntimeError(
                f"Cleanup eligibility changed: {name}"
            )

        staged_hashes = file_manifest(stage)

        if staged_hashes != item["staged_hashes"]:
            raise RuntimeError(f"Staging changed: {name}")

        source_is_dir = source.is_dir()

        if source_is_dir:
            original_hashes = file_manifest(source)
            source_sha256 = None
        else:
            source_sha256 = file_checksum(source).hex()
            if source.suffix.lower() == ".zip":
                original_hashes = zip_manifest(source)
            else:
                original_hashes = {
                    source.name: source_sha256
                }

        if original_hashes != item["original_hashes"]:
            raise RuntimeError(
                f"Original download changed: {name}"
            )

        quarantine = Path(tempfile.mkdtemp(
            prefix=".mixie-inbox-",
            dir=quarantine_root
        ))

        held_stage = quarantine / "staging"
        held_source = quarantine / "original"

        recovery = {
            "version": 1,
            "kind": "inbox_cleanup",
            "import_name": name,
            "source_name": source.name,
            "source_is_dir": source_is_dir,
            "source_sha256": source_sha256,
            "source_hashes": original_hashes,
            "staged_hashes": staged_hashes
        }

        temporary_record = quarantine / "recovery.json.tmp"
        recovery_path = quarantine / "recovery.json"

        # Persist recovery instructions before moving files.
        with temporary_record.open("x", encoding="utf-8") as file:
            json.dump(recovery, file, indent=2)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_record, recovery_path)
        sync_directory(quarantine)
        sync_directory(quarantine_root)

        # The journal remains available if either move fails
        # or power is lost between the two moves.
        stage.rename(held_stage)
        sync_directory(staged_root)
        sync_directory(quarantine)

        source.rename(held_source)
        sync_directory(inbox_root)
        sync_directory(quarantine)

        if file_manifest(held_stage) != staged_hashes:
            raise RuntimeError(
                f"Quarantined staging checksum mismatch: {name}"
            )

        if source_is_dir:
            verified_source = file_manifest(held_source)
        else:
            if file_checksum(held_source).hex() != source_sha256:
                raise RuntimeError(
                    f"Quarantined original changed: {name}"
                )
            verified_source = (
                zip_manifest(held_source)
                if source.suffix.lower() == ".zip"
                else {source.name: source_sha256}
            )

        if verified_source != original_hashes:
            raise RuntimeError(
                f"Quarantined original verification failed: {name}"
            )

        moved.append(name)
        print(f"Safely quarantined Inbox import: {name}")

    print(f"\nInbox imports quarantined: {len(moved)}")
    print("All quarantined files retained for recovery.")
    return moved

def restore_inbox_quarantine(approved=False):
    """Recover Inbox and staging files after interrupted cleanup."""
    backup = (
        Path(script_folder) / constants.backup_folder_name
    ).resolve()
    root = backup / ".cleanup-quarantine"

    if not root.exists():
        print("No cleanup quarantine found.")
        return []

    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe quarantine directory.")

    inbox = Path(inbox_folder)
    staging = Path(staging_folder)

    if (
        inbox.is_symlink() or not inbox.is_dir()
        or staging.is_symlink() or not staging.is_dir()
    ):
        raise ValueError("Unsafe Inbox or staging directory.")

    def sync_directory(path):
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    ready = []

    for folder in sorted(root.iterdir()):
        if not folder.name.startswith(".mixie-inbox-"):
            continue

        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f"Unsafe recovery folder: {folder}")

        record_path = folder / "recovery.json"
        held_stage = folder / "staging"
        held_source = folder / "original"

        if record_path.is_symlink():
            raise ValueError("Unsafe recovery journal.")

        if not record_path.is_file():
            if held_stage.exists() or held_source.exists():
                raise RuntimeError(
                    f"Quarantined files lack a recovery journal: {folder}"
                )
            print(f"Unfinished empty journal: {folder.name}")
            continue

        with record_path.open(encoding="utf-8") as file:
            record = json.load(file)

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
            or not isinstance(record.get("source_hashes"), dict)
            or not isinstance(record.get("staged_hashes"), dict)
            or not record["staged_hashes"]
        ):
            raise ValueError(f"Invalid recovery journal: {folder}")

        source_is_dir = record["source_is_dir"]
        source = inbox / source_name
        stage = staging / name

        for path in (source, stage, held_source, held_stage):
            if path.is_symlink():
                raise ValueError(f"Unsafe recovery path: {path}")

        # Exactly one copy must exist at each location.
        stage_held = held_stage.is_dir()
        stage_live = stage.is_dir()

        source_held = held_source.exists()
        source_live = source.exists()

        if stage_held == stage_live:
            raise RuntimeError(
                f"Missing or conflicting staged copies: {name}"
            )

        if source_held == source_live:
            raise RuntimeError(
                f"Missing or conflicting originals: {name}"
            )

        staged_copy = held_stage if stage_held else stage
        original_copy = held_source if source_held else source

        if (
            sort_file_hashes := file_manifest(staged_copy)
        ) != record["staged_hashes"]:
            raise RuntimeError(
                f"Staged checksum mismatch: {name}"
            )

        if source_is_dir:
            if not original_copy.is_dir():
                raise RuntimeError(
                    f"Original folder missing: {name}"
                )
            original_hashes = file_manifest(original_copy)
        else:
            if not original_copy.is_file():
                raise RuntimeError(
                    f"Original download missing: {name}"
                )

            checksum = file_checksum(original_copy).hex()

            if checksum != record.get("source_sha256"):
                raise RuntimeError(
                    f"Original checksum mismatch: {name}"
                )

            if source_name.lower().endswith(".zip"):
                original_hashes = zip_manifest(original_copy)
            else:
                original_hashes = {source_name: checksum}

        if original_hashes != record["source_hashes"]:
            raise RuntimeError(
                f"Original download contents changed: {name}"
            )

        ready.append({
            "name": name,
            "folder": folder,
            "record": record,
            "stage": stage,
            "source": source,
            "held_stage": held_stage,
            "held_source": held_source,
            "stage_held": stage_held,
            "source_held": source_held
        })

        print(
            f"Recoverable: {name} "
            f"(staging held: {stage_held}, "
            f"original held: {source_held})"
        )

    if not approved:
        print(f"\nRecovery preview: {len(ready)} import(s)")
        print("Nothing moved.")
        return ready

    restored = []

    for item in ready:
        name = item["name"]

        # Restore original download first.
        if item["source_held"]:
            if item["source"].exists() or item["source"].is_symlink():
                raise RuntimeError(
                    f"Inbox destination occupied: {name}"
                )

            item["held_source"].rename(item["source"])
            sync_directory(inbox)
            sync_directory(item["folder"])

        # Then restore the staging folder.
        if item["stage_held"]:
            if item["stage"].exists() or item["stage"].is_symlink():
                raise RuntimeError(
                    f"Staging destination occupied: {name}"
                )

            item["held_stage"].rename(item["stage"])
            sync_directory(staging)
            sync_directory(item["folder"])

        if file_manifest(item["stage"]) != (
            item["record"]["staged_hashes"]
        ):
            raise RuntimeError(
                f"Restored staging verification failed: {name}"
            )

        if item["record"]["source_is_dir"]:
            valid_original = (
                file_manifest(item["source"])
                == item["record"]["source_hashes"]
            )
        else:
            valid_original = (
                file_checksum(item["source"]).hex()
                == item["record"]["source_sha256"]
            )

        if not valid_original:
            raise RuntimeError(
                f"Restored original verification failed: {name}"
            )

        # Remove only the recovery journal, never music.
        item["folder"].joinpath("recovery.json").unlink()
        sync_directory(item["folder"])

        try:
            item["folder"].rmdir()
        except OSError:
            print(
                f"Recovery folder retained for inspection: "
                f"{item['folder']}"
            )

        restored.append(name)
        print(f"Restored Inbox and staging: {name}")

    return restored

# Move fully verified staged imports into recoverable quarantine
def quarantine_archived_cleanup(
    completed, originals_zip, library_zip, approved=False
):
    ready = verify_archived_cleanup(
        completed, originals_zip, library_zip
    )

    if not approved:
        print("Quarantine preview only. No files moved.")
        return ready

    backup_root = (
        Path(script_folder) / constants.backup_folder_name
    ).resolve()

    quarantine_root = backup_root / ".cleanup-quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)

    staged_root = Path(staging_folder).resolve()
    moved = []

    for item in ready:
        stage = Path(item["stage"])

        if (
            stage.is_symlink()
            or not stage.is_dir()
            or stage.parent.resolve() != staged_root
        ):
            raise ValueError("Unsafe staged import.")

        # Recheck immediately before moving anything
        if file_manifest(stage) != item["staged_hashes"]:
            raise RuntimeError("Staged files changed.")

        archived = zip_manifest(originals_zip)

        if archived.get(item["archived_source"]) != (
            item["archived_source_hash"]
        ):
            raise RuntimeError("Original archive changed.")

        if zip_manifest(library_zip) != file_manifest(
            music_library_folder
        ):
            raise RuntimeError("Library snapshot changed.")

        quarantine = Path(tempfile.mkdtemp(
            prefix=".mixie-archived-",
            dir=quarantine_root
        ))

        held = quarantine / "staging"

        recovery = {
            "version": 1,
            "import_name": item["name"],
            "original_staging_path": str(stage),
            "staged_hashes": item["staged_hashes"],
            "original_archive": str(originals_zip),
            "archived_source": item["archived_source"],
            "archived_source_hash": item["archived_source_hash"],
            "library_snapshot": str(library_zip)
        }


        # Save recovery instructions atomically before moving files.
        recovery_path = quarantine / "recovery.json"
        temporary_record = quarantine / "recovery.json.tmp"

        with temporary_record.open("x", encoding="utf-8") as file:
            json.dump(recovery, file, indent=2)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_record, recovery_path)

        # Persist the recovery record's directory entry.
        directory_fd = os.open(
            quarantine,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


        try:
            stage.rename(held)

            # Persist both sides of the staging-to-quarantine move.
            for directory in (staged_root, quarantine):
                directory_fd = os.open(
                    directory,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

            if file_manifest(held) != item["staged_hashes"]:
                raise RuntimeError(
                    "Quarantined files failed verification."
                )

        except Exception:
            if held.exists() and not stage.exists():
                held.rename(stage)
            raise

        moved.append(item["name"])
        print(f"Safely quarantined: {item['name']}")

    print(f"\nQuarantined imports: {len(moved)}")
    print("Quarantined files retained for recovery.")
    return moved

# Preview or restore staged imports from recoverable quarantine.
def restore_archived_quarantine(approved=False):
    backup_root = (
        Path(script_folder) / constants.backup_folder_name
    ).resolve()
    quarantine_root = backup_root / ".cleanup-quarantine"
    stage_root = Path(staging_folder).resolve()

    if not quarantine_root.exists():
        print("No cleanup quarantine exists.")
        return []

    if (
        quarantine_root.is_symlink()
        or not quarantine_root.is_dir()
        or stage_root.is_symlink()
        or not stage_root.is_dir()
    ):
        raise ValueError("Unsafe recovery directory.")

    ready = []

    for folder in sorted(quarantine_root.iterdir()):
        if folder.is_symlink():
            raise ValueError(f"Unsafe quarantine entry: {folder}")

        if not folder.is_dir():
            continue

        # Only handle records created by our new quarantine method.
        if not folder.name.startswith(".mixie-archived-"):
            continue

        record_path = folder / "recovery.json"
        held = folder / "staging"

        if record_path.is_symlink() or held.is_symlink():
            raise ValueError(f"Unsafe recovery record: {folder}")

        if not record_path.is_file():
            if held.exists():
                raise RuntimeError(
                    f"Quarantined files lack recovery record: {folder}"
                )
            print(f"Unfinished empty quarantine: {folder.name}")
            continue

        with record_path.open(encoding="utf-8") as source:
            record = json.load(source)

        name = record.get("import_name")
        hashes = record.get("staged_hashes")

        if (
            record.get("version") != 1
            or not isinstance(name, str)
            or name in ("", ".", "..")
            or Path(name).name != name
            or not isinstance(hashes, dict)
            or not hashes
        ):
            raise ValueError(f"Invalid recovery record: {folder}")

        stage = stage_root / name

        if Path(record.get("original_staging_path", "")) != stage:
            raise ValueError("Recovery destination does not match.")

        if held.is_dir():
            if stage.exists() or stage.is_symlink():
                raise RuntimeError(
                    f"Recovery blocked: destination exists: {stage}"
                )

            if file_manifest(held) != hashes:
                raise RuntimeError(
                    f"Recovery checksum mismatch: {name}"
                )

            ready.append((name, held, stage, hashes))
            print(f"Recoverable: {name}")

        elif stage.is_dir() and not stage.is_symlink():
            if file_manifest(stage) != hashes:
                raise RuntimeError(
                    f"Existing staging differs: {name}"
                )
            print(f"Already in staging: {name}")

        else:
            raise RuntimeError(
                f"Recovery files missing: {name}"
            )

    if not approved:
        print(f"\nRecovery preview: {len(ready)} import(s)")
        print("Nothing moved.")
        return ready

    restored = []

    for name, held, stage, hashes in ready:
        if stage.exists() or stage.is_symlink():
            raise RuntimeError(
                f"Recovery destination became occupied: {stage}"
            )

        if file_manifest(held) != hashes:
            raise RuntimeError(f"Quarantine changed: {name}")

        held.rename(stage)

        if file_manifest(stage) != hashes:
            raise RuntimeError(
                f"Post-restore verification failed: {name}. "
                "Files retained in staging for inspection."
            )

        restored.append(name)
        print(f"Restored to staging: {name}")

    return restored

# Remove imports only after verifying both archives and library copies
def cleanup_verified_imports(
    completed, originals_zip, library_zip, approved=False
):

    if approved:
        print(
            "Legacy immediate-delete cleanup disabled. "
            "Files remain protected."
        )
        plan_ingest_cleanup(completed)
        return []

    ready = plan_ingest_cleanup(completed)

    if not approved:
        print("Cleanup preview only. No files removed.")
        return ready

    if not ready:
        return []

    backup_root = (
        Path(script_folder) / constants.backup_folder_name
    ).resolve()

    original_archive = Path(originals_zip).resolve()
    library_archive = Path(library_zip).resolve()

    # Only a cumulative original-download archive permits cleanup
    if original_archive != (
        backup_root / "OriginalDownloads-Bulk.zip"
    ):
        raise ValueError("Cumulative originals archive required.")

    if (
        library_archive.parent != backup_root
        or not library_archive.name.startswith("MusicLibrary-")
        or library_archive.suffix.lower() != ".zip"
    ):
        raise ValueError("Verified library snapshot required.")

    # The bulk archive must contain every current Inbox file
    archived_originals = zip_manifest(original_archive)
    current_originals = file_manifest(inbox_folder)

    for name, checksum in current_originals.items():
        if archived_originals.get(name) != checksum:
            raise RuntimeError(
                f"Original missing from archive: {name}"
            )

    # The library snapshot must exactly match the current library
    archived_library = zip_manifest(library_archive)
    current_library = file_manifest(music_library_folder)

    if archived_library != current_library:
        raise RuntimeError("Music Library archive is not current.")

    # Verify the per-album receipts from MIXIE's successful commits
    for item in ready:
        for key, album in item["albums"]:
            receipt = completed.get(key)

            if not isinstance(receipt, dict):
                raise RuntimeError(f"Missing commit receipt: {key}")

            if len(receipt) < len(album["tracks"]) + 1:
                raise RuntimeError(f"Incomplete commit receipt: {key}")

            for destination, expected_hash in receipt.items():
                path = Path(destination)

                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(
                        Path(music_library_folder).resolve()
                    )
                ):
                    raise RuntimeError(
                        f"Invalid committed file: {destination}"
                    )

                if file_checksum(path).hex() != expected_hash:
                    raise RuntimeError(
                        f"Committed file changed: {destination}"
                    )

    print("\nBoth archives and all commit receipts verified.")

    quarantine_root = backup_root / ".cleanup-quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    cleaned = []

    for item in ready:
        source = item["source"]
        stage = item["stage"]

        # Confirm neither input changed during archive verification
        if file_manifest(stage) != item["staged_hashes"]:
            raise RuntimeError(f"Staging changed: {item['name']}")

        if source.is_dir():
            current_source = file_manifest(source)
        elif source.suffix.lower() == ".zip":
            current_source = zip_manifest(source)
        else:
            current_source = {
                source.name: file_checksum(source).hex()
            }

        if current_source != item["original_hashes"]:
            raise RuntimeError(f"Original changed: {item['name']}")

        quarantine = Path(tempfile.mkdtemp(
            prefix=".mixie-clean-",
            dir=quarantine_root
        ))

        # Move out of Inbox/Staging before final disposal
        try:
            stage.rename(quarantine / "staging")
            source.rename(quarantine / "original")
        except OSError:
            if (quarantine / "original").exists():
                (quarantine / "original").rename(source)
            if (quarantine / "staging").exists():
                (quarantine / "staging").rename(stage)
            quarantine.rmdir()
            raise

        # Retain recovery copies during the first cleanup rollout
        print(f"Recovery copies preserved: {quarantine}")

        cleaned.append(item["name"])
        print(f"Cleaned verified import: {item['name']}")

    print(f"\nCleanup complete: {len(cleaned)} imports")
    return cleaned

# Run the sorter only when launched directly
if __name__ == "__main__":
    sort_inbox()
