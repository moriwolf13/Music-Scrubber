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
import uuid
import fcntl
from contextlib import contextmanager

#Set safety limits for ZIP archives
MAX_ZIP_UNCOMPRESSED_BYTES = 10 * 1024**3  # 10 GiB
MAX_ZIP_MEMBERS = 10_000

#File routing
script_folder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
drive_folder = os.path.dirname(script_folder)
inbox_folder = os.path.join(script_folder, constants.inbox_folder_name)
music_library_folder = os.path.join(drive_folder, constants.library_folder_name)
staging_folder = os.path.join(script_folder,constants.staging_folder_name)

def _zip_member_hash(archive, member):
    digest = hashlib.sha256()
    with archive.open(member) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _zip_members(archive, enforce_limits=False, *, case_sensitive=False):
    """Validate paths; extraction remains portable, inventories preserve Linux case.

    Only byte-identical repeated artwork entries may collapse to one payload
    file. The untouched ZIP remains the authority for every original occurrence.
    """
    members = archive.infolist()
    if enforce_limits and len(members) > MAX_ZIP_MEMBERS:
        raise ValueError("Too many files in ZIP.")
    seen = {}
    spellings = {}
    total = 0
    canonical = []
    for member in members:
        name = member.filename
        relative = name[:-1] if member.is_dir() else name
        parts = relative.split("/")
        mode = stat.S_IFMT(member.external_attr >> 16)
        if (
            not relative or "\\" in name or "\x00" in member.orig_filename
            or name.startswith("/") or ntpath.splitdrive(name)[0]
            or any(part in ("", ".", "..") for part in parts)
            or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
            or (mode == stat.S_IFDIR and not member.is_dir())
        ):
            raise ValueError(f"Unsafe ZIP entry: {name}")
        total += member.file_size
        if enforce_limits and total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ValueError("ZIP expands beyond the 10 GiB limit.")
        key = relative if case_sensitive else relative.casefold()
        if key in seen:
            previous = seen[key]
            if (name != previous.filename or member.is_dir() or previous.is_dir()
                    or Path(name).suffix.lower() not in (".jpg", ".jpeg", ".png", ".pdf")
                    or member.file_size != previous.file_size
                    or _zip_member_hash(archive, member) != _zip_member_hash(archive, previous)):
                raise ValueError(f"Conflicting or unsupported duplicate ZIP entry: {name}")
            continue
        seen[key] = member
        canonical.append(member)
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            prefix_key = prefix if case_sensitive else prefix.casefold()
            previous = spellings.setdefault(prefix_key, prefix)
            if previous != prefix:
                raise ValueError(f"Case-insensitive ZIP path conflict: {name}")
    for name in seen:
        parts = name.split("/")
        if any((ancestor := seen.get("/".join(parts[:index]))) is not None
               and not ancestor.is_dir() for index in range(1, len(parts))):
            raise ValueError(f"ZIP file/directory conflict: {name}")
    return canonical


def check_zip_safety(archive, temp_folder, filename):
    root = os.path.abspath(temp_folder)
    members = _zip_members(archive, enforce_limits=True)
    for member in members:
        destination = os.path.abspath(os.path.join(root, member.filename))
        if os.path.commonpath((root, destination)) != root:
            raise ValueError(f"Unsafe path in {filename}: {member.filename}")
    return members


def _safe_name(value):
    return (
        isinstance(value, str) and value not in ("", ".", "..")
        and Path(value).name == value and "/" not in value
        and "\\" not in value and not ntpath.splitdrive(value)[0]
        and "\x00" not in value
    )


def _is_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _validate_payload_manifest(hashes):
    if not isinstance(hashes, dict):
        raise ValueError("Invalid payload manifest.")
    spellings = {}
    files = set()
    for relative, checksum in hashes.items():
        if (not isinstance(relative, str) or "\\" in relative
                or relative.startswith("/") or ntpath.splitdrive(relative)[0]
                or any(part in ("", ".", "..") for part in relative.split("/"))
                or not _is_sha256(checksum)):
            raise ValueError("Noncanonical payload path or checksum.")
        parts = relative.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            if spellings.setdefault(prefix.casefold(), prefix) != prefix:
                raise ValueError("Case-insensitive payload path conflict.")
        files.add(relative.casefold())
    for relative in files:
        parts = relative.split("/")
        if any("/".join(parts[:index]) in files for index in range(1, len(parts))):
            raise ValueError("Payload file/directory conflict.")


def _safe_directory(path, create=False, missing_ok=False):
    path = Path(os.path.abspath(path))
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f"Symbolic link in directory path: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() and not (missing_ok and not path.exists()):
        raise ValueError(f"Unsafe directory: {path}")
    return path


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path, record):
    path = Path(path)
    _safe_directory(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Unsafe record path: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=".mixie-record-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def staging_import_name(source_name, source_kind):
    """Stable source identity, independent of package contents or Inbox order."""
    if not _safe_name(source_name) or source_kind not in ("directory", "zip", "file"):
        raise ValueError("Invalid staging source identity.")
    identity = json.dumps([source_kind, source_name], ensure_ascii=False).encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    stem = Path(source_name).stem if source_kind == "zip" else source_name
    return f"{safe_path_part(stem)[:76]}--mixie-{suffix}"


def _provenance_path(import_name, create=False):
    if not _safe_name(import_name):
        raise ValueError("Invalid import name.")
    root = _safe_directory(Path(script_folder) / ".staging-provenance",
                           create=create, missing_ok=not create)
    filename = hashlib.sha256(import_name.encode("utf-8")).hexdigest() + ".json"
    return root / filename


def _write_staging_provenance(import_name, record):
    path = _provenance_path(import_name, create=True)
    _atomic_json(path, record)
    _sync_directory(path.parent.parent)


def _legacy_adoption_path(import_name):
    return _provenance_path(import_name).with_suffix(".adopted.json")


def _legacy_adoption(import_name):
    path = _legacy_adoption_path(import_name)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Unsafe legacy adoption marker.")
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Invalid legacy adoption marker.") from error
    if (not isinstance(record, dict) or record.get("version") != 1
            or record.get("import_name") != import_name
            or not _safe_name(record.get("source_name"))
            or record.get("source_kind") not in ("directory", "zip", "file")):
        raise ValueError("Invalid legacy adoption identity.")
    return record


def staging_provenance(import_name, *, verify_payload=True):
    """Read committed provenance; None means legacy, never an unproven managed ID.

    Records stay outside payloads and survive quarantine/purge. Callers examining
    a held copy use verify_payload=False, then verify that copy against the record.
    """
    if not _safe_name(import_name) or import_name.startswith(".mixie-pending-"):
        raise ValueError("Pending or invalid staged import.")
    path = _provenance_path(import_name)
    adoption = _legacy_adoption(import_name)
    if path.is_symlink():
        raise ValueError("Unsafe staging provenance.")
    if not path.exists():
        if "--mixie-" in import_name or adoption is not None:
            raise ValueError("Missing provenance for managed or adopted legacy staged import.")
        return None
    if not path.is_file():
        raise ValueError("Invalid staging provenance path.")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Invalid staging provenance JSON.") from error
    if not isinstance(record, dict) or record.get("state") != "committed":
        raise ValueError("Uncommitted staged import requires recovery.")
    hashes = record.get("payload_hashes")
    kind = record.get("source_kind")
    source_name = record.get("source_name")
    transaction = record.get("transaction_id")
    checksum = record.get("source_sha256")

    legacy_name = Path(source_name).stem if isinstance(source_name, str) and kind == "zip" else source_name
    adopted = (record.get("adopted_legacy") is True and "--mixie-" not in import_name
               and legacy_name == import_name and adoption is not None
               and adoption["source_name"] == source_name and adoption["source_kind"] == kind)
    if (
        record.get("version") != 1 or record.get("kind") != "staged_import"
        or record.get("import_name") != import_name
        or not _safe_name(source_name) or kind not in ("directory", "zip", "file")
        or (staging_import_name(source_name, kind) != import_name and not adopted)
        or (adoption is not None and not adopted)
        or not isinstance(transaction, str) or len(transaction) != 32
        or any(character not in "0123456789abcdef" for character in transaction)
        or not isinstance(hashes, dict) or not hashes
        or (kind == "directory" and checksum is not None)
        or (kind != "directory" and not _is_sha256(checksum))
        or (kind == "zip" and Path(source_name).suffix.lower() != ".zip")
        or (kind == "file" and Path(source_name).suffix.lower() not in constants.music_file_types)
    ):
        raise ValueError("Invalid staged-import provenance.")
    _validate_payload_manifest(hashes)
    duplicates = record.get("duplicate_zip_entries", [])
    if (not isinstance(duplicates, list) or any(not isinstance(name, str) for name in duplicates)
            or duplicates != sorted(set(duplicates))
            or any(name not in hashes or Path(name).suffix.lower() not in (".jpg", ".jpeg", ".png", ".pdf")
                   for name in duplicates)
            or (duplicates and kind != "zip")):
        raise ValueError("Invalid duplicate ZIP provenance.")
    if verify_payload:
        stage = _safe_directory(Path(staging_folder) / import_name)
        if file_manifest(stage) != hashes:
            raise RuntimeError("Staged payload changed since commit.")
    return record


def is_committed_stage(path):
    """Strict scan helper: legacy/unproven, pending, symlinked or altered => False."""
    path = Path(os.path.abspath(path))
    try:
        root = _safe_directory(staging_folder)
        return path.parent == root and staging_provenance(path.name) is not None
    except (OSError, ValueError, RuntimeError):
        return False


def committed_staging_imports():
    root = _safe_directory(staging_folder)
    return [path for path in sorted(root.iterdir()) if is_committed_stage(path)]


def _source_snapshot(source):
    source = Path(source)
    _safe_directory(source.parent)
    if source.is_symlink():
        raise ValueError("Symbolic link in import source.")
    if source.is_dir():
        payload = file_manifest(_safe_directory(source))
        _validate_payload_manifest(payload)
        return "directory", None, payload
    if not source.is_file():
        raise ValueError("Unsupported import source.")
    checksum = file_checksum(source).hex()
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as saved:
            check_zip_safety(saved, staging_folder, source.name)
        payload = zip_manifest(source)
        _validate_payload_manifest(payload)
        if file_checksum(source).hex() != checksum:
            raise RuntimeError("Original ZIP changed during verification.")
        return "zip", checksum, payload
    if source.suffix.lower() in constants.music_file_types:
        payload = {source.name: checksum}
        _validate_payload_manifest(payload)
        return "file", checksum, payload
    raise ValueError("Unsupported import source.")


def _verify_provenance_source(record, source):
    kind, checksum, payload = _source_snapshot(source)
    if (
        Path(source).name != record["source_name"] or kind != record["source_kind"]
        or checksum != record["source_sha256"] or payload != record["payload_hashes"]
    ):
        raise RuntimeError("Original download changed since staging.")
    return payload


def _staging_record(source, name, kind, checksum, payload, *, adopted_legacy=False):
    record = {
        "version": 1, "kind": "staged_import", "state": "pending",
        "import_name": name, "transaction_id": uuid.uuid4().hex,
        "source_name": source.name, "source_kind": kind,
        "source_sha256": checksum, "payload_hashes": payload,
    }
    if adopted_legacy:
        record["adopted_legacy"] = True
    if kind == "zip":
        with zipfile.ZipFile(source) as saved:
            names = [member.filename for member in saved.infolist() if not member.is_dir()]
            seen, duplicates = set(), set()
            for relative in names:
                if relative in seen:
                    duplicates.add(relative)
                seen.add(relative)
        if duplicates:
            record["duplicate_zip_entries"] = sorted(duplicates)
    return record


def _adopt_legacy_stage(source, stage, kind, checksum, payload):
    # A full inventory comparison is new proof, not a claim about old provenance.
    if not payload or file_manifest(stage) != payload:
        raise ValueError("Unverified legacy staging differs from the full original; reconciliation required.")
    record = _staging_record(source, stage.name, kind, checksum, payload, adopted_legacy=True)
    _verify_provenance_source(record, source)
    _sync_payload(stage, payload)
    # A durable marker prevents missing provenance from downgrading this source
    # back to unmanaged legacy authority. Neither record is removed on failure.
    _provenance_path(stage.name, create=True)
    _atomic_json(_legacy_adoption_path(stage.name), {
        "version": 1, "import_name": stage.name,
        "source_name": source.name, "source_kind": kind,
    })
    _write_staging_provenance(stage.name, record)
    if file_manifest(stage) != payload:
        raise RuntimeError("Legacy staged payload changed during adoption.")
    _verify_provenance_source(record, source)
    _write_staging_provenance(stage.name, {**record, "state": "committed"})
    return str(stage)


def _sync_payload(root, hashes):
    for relative in hashes:
        path = root / relative
        _safe_directory(path.parent)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Unsupported staged file.")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()),
                            key=lambda path: len(path.parts), reverse=True):
        _sync_directory(directory)
    _sync_directory(root)


@contextmanager
def _staging_lock():
    root = _provenance_path("lock", create=True).parent
    descriptor = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Unsafe staging lock.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another staging operation is active.") from error
        yield
    finally:
        os.close(descriptor)

# Process new music imports from the Inbox
def sort_inbox():
    _safe_directory(inbox_folder, create=True)
    _safe_directory(staging_folder, create=True)
    with _staging_lock():
        return _sort_inbox_locked()


def _sort_inbox_locked():
    staging = _safe_directory(staging_folder)
    if any(path.name.startswith(".mixie-pending-") for path in staging.iterdir()):
        raise RuntimeError("Pending staging import requires recovery before ingestion.")
    backup = _safe_directory(Path(script_folder) / constants.backup_folder_name, missing_ok=True)
    # A prepared finalizer may already hold part of a delivery outside Inbox.
    # Do not create a new occurrence until explicit authorized recovery finishes.
    import mixie_finalizer
    if os.path.lexists(backup / mixie_finalizer.LEDGER_NAME) and mixie_finalizer._state()['active'] is not None:
        raise RuntimeError("Pending finalization cleanup requires authorized recovery before staging new imports.")
    quarantine = _safe_directory(backup / ".cleanup-quarantine", missing_ok=True)
    if quarantine.exists() and any(
        path.name.startswith((".mixie-inbox-", ".mixie-archived-"))
        for path in quarantine.iterdir()
    ):
        raise RuntimeError("Pending cleanup requires recovery before staging new imports.")

    staged_imports = []
    for source in sorted(Path(inbox_folder).iterdir()):
        if source.is_symlink():
            print(f"Skipping symbolic link: {source.name}")
            continue
        if not (source.is_dir() or (source.is_file() and
                source.suffix.lower() in (".zip", *constants.music_file_types))):
            print(f"Unsupported Inbox item: {source.name}")
            continue
        pending = None
        try:
            kind, checksum, payload = _source_snapshot(source)
            name = staging_import_name(source.name, kind)
            destination = staging / name
            legacy_name = source.stem if kind == "zip" else source.name
            legacy = staging / legacy_name
            if os.path.lexists(legacy):
                previous = staging_provenance(legacy_name)
                if previous is None:
                    if "--mixie-" in legacy_name or legacy_name.startswith(".mixie-"):
                        raise ValueError("Unproven managed staging destination requires recovery.")
                    staged_imports.append(_adopt_legacy_stage(source, legacy, kind, checksum, payload))
                    print(f"Verified and adopted unchanged legacy stage: {source.name} -> {legacy_name}")
                    continue
                if (previous["source_name"], previous["source_kind"]) == (source.name, kind):
                    _verify_provenance_source(previous, source)
                    print(f"Verified unchanged staged import: {source.name}")
                    continue
                # A proven different source (e.g. directory versus ZIP) gets its
                # own managed delivery, never somebody else's legacy authority.
            if os.path.lexists(destination):
                record = staging_provenance(name)
                _verify_provenance_source(record, source)
                print(f"Verified unchanged staged import: {source.name}")
                continue
            # An interrupted metadata commit must not be overwritten on retry.
            if os.path.lexists(_provenance_path(name)):
                staging_provenance(name, verify_payload=False)
            if not payload or not any(Path(relative).suffix.lower() in constants.music_file_types
                                      for relative in payload):
                raise ValueError("No supported music files found.")
            pending = Path(tempfile.mkdtemp(prefix=".mixie-pending-", dir=staging))
            if kind == "zip":
                with zipfile.ZipFile(source) as saved:
                    members = check_zip_safety(saved, pending, source.name)
                    saved.extractall(pending, members=members)
            elif kind == "directory":
                shutil.copytree(source, pending, dirs_exist_ok=True)
            else:
                shutil.copy2(source, pending / source.name)
            if file_manifest(pending) != payload:
                raise RuntimeError("Staged copy differs from original download.")
            record = _staging_record(source, name, kind, checksum, payload)
            _verify_provenance_source(record, source)
            _sync_payload(pending, payload)
            _write_staging_provenance(name, record)
            if os.path.lexists(destination):
                raise FileExistsError(destination)
            pending.rename(destination)
            _sync_directory(staging)
            if file_manifest(destination) != payload:
                raise RuntimeError("Staged payload changed before commit.")
            _verify_provenance_source(record, source)
            record = {**record, "state": "committed"}
            _write_staging_provenance(name, record)
            staged_imports.append(str(destination))
            print(f"Successfully staged: {source.name} -> {name}")
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            print(f"Import deferred: {source.name} - {error}")
        finally:
            # Never remove a published payload, even if its commit record failed.
            if pending is not None and pending.exists():
                shutil.rmtree(pending)
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

def _file_identity(attributes):
    return (attributes.st_dev, attributes.st_ino, attributes.st_mode,
            attributes.st_size, attributes.st_mtime_ns, attributes.st_ctime_ns)


# Calculate a stable regular file's checksum without following a leaf symlink.
def file_checksum(path):
    checksum = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Unsupported file: {path}")
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            checksum.update(chunk)
        if _file_identity(os.fstat(file.fileno())) != _file_identity(before):
            raise RuntimeError(f"File changed during hashing: {path}")
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
    except (OSError, ValueError, RuntimeError):

        # Mutation detection is a verification failure and must also roll back.
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
        folder_album = safe_path_part(asset.get("folder_album") or asset["album"])
        album_folder = os.path.join(
            music_library_folder,
            safe_path_part(asset["album_artist"]),
            folder_album
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
                safe_path_part(track.get("folder_album") or track["album"])
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
    root = _safe_directory(folder)
    manifest = {}
    identities = {}

    def visit(directory):
        before = directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError(f"Unsafe directory: {directory}")
        # Capture directory identity before enumeration, not after os.walk yields.
        identities[directory] = _file_identity(before)
        for path in sorted(directory.iterdir()):
            attributes = path.stat(follow_symlinks=False)
            if stat.S_ISDIR(attributes.st_mode):
                visit(path)
            elif stat.S_ISREG(attributes.st_mode):
                identities[path] = _file_identity(attributes)
                manifest[path.relative_to(root).as_posix()] = file_checksum(path).hex()
            else:
                raise ValueError(f"Symbolic link or unsupported file: {path}")

    visit(root)
    # This evidence is local to this call; it is never a persistent stat cache.
    for path, expected in identities.items():
        if _file_identity(path.stat(follow_symlinks=False)) != expected:
            raise RuntimeError(f"Inventory changed during hashing: {path}")
    return manifest


# Inspect ZIP contents using SHA-256, not just ZIP CRC
def zip_manifest(path):
    result = {}
    with zipfile.ZipFile(path) as saved:
        for member in _zip_members(saved, case_sensitive=True):
            if member.is_dir():
                continue
            digest = hashlib.sha256()
            with saved.open(member) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[member.filename] = digest.hexdigest()
    return result

# Find fully completed source imports
def _check_import_tracks(stage, albums, staged_files):
    actual = {relative for relative in staged_files
              if Path(relative).suffix.lower() in constants.music_file_types}
    discovered = {Path(track["path"]).relative_to(stage).as_posix()
                  for _, album in albums for track in album["tracks"]}
    if not actual or actual != discovered:
        raise ValueError("Unaccounted staged music.")
    unsupported = {".wav", ".aif", ".aiff", ".m4a", ".aac", ".ogg", ".opus",
                   ".wma", ".ape", ".wv", ".dsf", ".dff"}
    if any(Path(relative).suffix.lower() in unsupported for relative in staged_files):
        raise ValueError("Unsupported audio remains in staging.")


def _check_candidate_provenance(item, staged_hashes, source=None):
    record = staging_provenance(item["name"], verify_payload=False)
    if record != item.get("provenance"):
        raise RuntimeError("Staging provenance changed during cleanup.")
    if staged_hashes != item["staged_hashes"]:
        raise RuntimeError("Staging changed during cleanup.")
    if record is not None:
        if staged_hashes != record["payload_hashes"]:
            raise RuntimeError("Staging differs from committed provenance.")
        if source is not None:
            _verify_provenance_source(record, source)
    return record


def plan_ingest_cleanup(completed_keys, *, _quiet=False):
    import tag

    completed_keys = set(completed_keys)
    grouped = {}
    for key, album in tag.group_staged_albums().items():
        grouped.setdefault(key[0], []).append((key, album))
    inbox = _safe_directory(inbox_folder)
    legacy_sources = {}
    for source in inbox.iterdir():
        if source.is_symlink():
            continue
        if source.is_dir():
            name = source.name
        elif source.is_file() and source.suffix.lower() == ".zip":
            name = source.stem
        elif source.is_file() and source.suffix.lower() in constants.music_file_types:
            name = source.name
        else:
            continue
        legacy_sources.setdefault(name, []).append(source)
    ready = []
    for name, albums in grouped.items():
        try:
            record = staging_provenance(name, verify_payload=False)
            sources = ([inbox / record["source_name"]] if record is not None
                       else legacy_sources.get(name, []))
            if len(sources) != 1:
                raise ValueError("Ambiguous original source.")
            if not all(key in completed_keys for key, _ in albums):
                raise ValueError("Incomplete import.")
            source = sources[0]
            stage = Path(staging_folder) / name
            staged = file_manifest(stage)
            _check_import_tracks(stage, albums, staged)
            kind, checksum, original = _source_snapshot(source)
            if staged != original:
                raise ValueError("Staging differs from original download.")
            if record is not None and (
                original != record["payload_hashes"] or kind != record["source_kind"]
                or checksum != record["source_sha256"]
            ):
                raise ValueError("Original download differs from committed provenance.")
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
            if not _quiet:
                print(f"Cleanup blocked: {name}: {error}")
            continue
        ready.append({
            "name": name, "source": source, "stage": stage, "albums": albums,
            "original_hashes": original, "staged_hashes": staged,
            "source_sha256": checksum, "provenance": record,
        })
        if not _quiet:
            print(f"Cleanup eligible: {name} ({len(albums)} album(s))")
    return ready


# Preview cleanup of completed imports whose originals exist
# inside the verified cumulative archive, but not Inbox.
def plan_archived_cleanup(completed, originals_zip):
    import tag

    backup = _safe_directory(Path(script_folder) / constants.backup_folder_name)
    bulk = Path(originals_zip)
    expected = backup / "OriginalDownloads-Bulk.zip"
    if bulk.is_symlink() or bulk.resolve() != expected or not bulk.is_file():
        raise ValueError("Verified cumulative archive required.")

    # Verify the entire outer archive before trusting its entries
    archived = zip_manifest(bulk)

    grouped = {}
    for key, album in tag.group_staged_albums().items():
        grouped.setdefault(key[0], []).append((key, album))

    ready = []
    inbox = _safe_directory(inbox_folder)
    inbox_items = list(inbox.iterdir())

    with zipfile.ZipFile(bulk) as outer:
        for name, albums in sorted(grouped.items()):
            try:
                record = staging_provenance(name, verify_payload=False)
                if record is not None:
                    source = inbox / record["source_name"]
                    if source.exists() or source.is_symlink():
                        continue
                    if record["source_kind"] != "zip":
                        raise ValueError("Archived-only cleanup requires original ZIP provenance.")
                    archive_name = record["source_name"]
                    if archived.get(archive_name) != record["source_sha256"]:
                        raise ValueError("Archived original differs from committed provenance.")
                else:
                    if any(item.name == name or (item.suffix.lower() == ".zip" and item.stem == name)
                           for item in inbox_items):
                        continue
                    archive_name = f"{name}.zip"
                if not all(key in completed for key, _ in albums):
                    raise ValueError("Unfinished import.")
                if archive_name not in archived:
                    raise ValueError("No original ZIP in cumulative archive.")
                stage = Path(staging_folder) / name
                staged = file_manifest(stage)
                _check_import_tracks(stage, albums, staged)
                if record is not None and staged != record["payload_hashes"]:
                    raise ValueError("Staged payload differs from committed provenance.")

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
                "archived_source_hash": archived[archive_name],
                "staged_hashes": staged,
                "provenance": record,
                "albums": albums
            })

            print(f"Archived cleanup eligible: {name}")

    return ready

# Verify archived-source cleanup candidates without deleting anything
def verify_archived_cleanup(completed, originals_zip, library_zip):
    backup_root = _safe_directory(Path(script_folder) / constants.backup_folder_name)

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
    library_root = _safe_directory(music_library_folder)
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
        _check_candidate_provenance(item, staged_hashes)
        if archived_originals.get(item["archived_source"]) != item["archived_source_hash"]:
            raise RuntimeError("Original archive changed during cleanup verification.")

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

def verify_inbox_cleanup(completed, originals_zip, library_zip, *, _quiet=False):
    """Preview Inbox-backed cleanup with full archive verification."""
    backup_root = _safe_directory(Path(script_folder) / constants.backup_folder_name)

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

    library_root = _safe_directory(music_library_folder)
    current_library = file_manifest(library_root)
    archived_library = zip_manifest(snapshot)

    if current_library != archived_library:
        raise RuntimeError("Library backup is not current.")

    candidates = plan_ingest_cleanup(completed, _quiet=_quiet)
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

        _check_candidate_provenance(item, file_manifest(stage), source)
        if not source.is_dir() and digest != item["source_sha256"]:
            raise RuntimeError("Original package changed during cleanup verification.")

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
        if not _quiet:
            print(f"Inbox-backed cleanup verified: {item['name']}")

    if not _quiet:
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

    backup_root = _safe_directory(Path(script_folder) / constants.backup_folder_name)
    quarantine_root = backup_root / ".cleanup-quarantine"

    if quarantine_root.is_symlink():
        raise ValueError("Unsafe quarantine directory.")

    quarantine_root.mkdir(parents=True, exist_ok=True)

    inbox_root = _safe_directory(inbox_folder)
    staged_root = _safe_directory(staging_folder)

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
            _safe_directory(existing.parent)
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
            completed, originals_zip, library_zip, _quiet=True
        )
        matching = next((entry for entry in fresh if entry["name"] == name), None)
        fields = ("source", "stage", "provenance", "staged_hashes", "original_hashes", "source_sha256")
        if matching is None or any(matching.get(field) != item.get(field) for field in fields):
            raise RuntimeError(f"Cleanup eligibility changed: {name}")

        staged_hashes = file_manifest(stage)
        provenance = _check_candidate_provenance(item, staged_hashes, source)

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

        if original_hashes != item["original_hashes"] or (
            not source_is_dir and item.get("source_sha256") is not None
            and source_sha256 != item["source_sha256"]
        ):
            raise RuntimeError(f"Original download changed: {name}")

        quarantine = Path(tempfile.mkdtemp(
            prefix=".mixie-inbox-",
            dir=quarantine_root
        ))

        held_stage = quarantine / "staging"
        held_source = quarantine / "original"

        recovery = {
            "version": 1,
            "kind": "inbox_cleanup",
            "transaction_id": uuid.uuid4().hex,
            "staging_provenance": provenance,
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

def validate_cleanup_provenance(record):
    """Validate an active journal's immutable source binding, including on resume.

    Does not require a live stage or intact held copies: purge may be interrupted.
    This checks provenance only, not completion receipts or permission to delete.
    """
    if not isinstance(record, dict) or not _safe_name(record.get("import_name")):
        raise ValueError("Invalid cleanup provenance identity.")
    provenance = staging_provenance(record["import_name"], verify_payload=False)
    if provenance != record.get("staging_provenance"):
        raise RuntimeError("Recovery provenance changed or is missing.")
    if provenance is None:
        return None
    transaction = record.get("transaction_id")
    if (not isinstance(transaction, str) or len(transaction) != 32
            or any(character not in "0123456789abcdef" for character in transaction)):
        raise ValueError("Invalid cleanup transaction identity.")
    if provenance["payload_hashes"] != record.get("staged_hashes"):
        raise RuntimeError("Recovery payload differs from committed provenance.")
    if record.get("kind") == "inbox_cleanup":
        if (record.get("source_name") != provenance["source_name"]
                or type(record.get("source_is_dir")) is not bool
                or record["source_is_dir"] != (provenance["source_kind"] == "directory")
                or record.get("source_sha256") != provenance["source_sha256"]
                or record.get("source_hashes") != provenance["payload_hashes"]):
            raise RuntimeError("Inbox recovery source differs from committed provenance.")
    elif (provenance["source_kind"] != "zip"
            or record.get("archived_source") != provenance["source_name"]
            or record.get("archived_source_hash") != provenance["source_sha256"]):
        raise RuntimeError("Archived recovery source differs from committed provenance.")
    return provenance


def _retire_cleanup_transaction(folder, *, unpublished=False):
    """Move metadata-only recovery evidence aside; never delete its journal."""
    backup = _safe_directory(Path(script_folder) / constants.backup_folder_name)
    root = _safe_directory(backup / ".cleanup-quarantine")
    folder = _safe_directory(folder)
    if folder.parent != root:
        raise ValueError("Unsafe recovery retirement path.")
    allowed = {"recovery.json.tmp"} if unpublished else {"recovery.json"}
    for entry in folder.iterdir():
        if entry.name not in allowed or entry.is_symlink() or not entry.is_file():
            raise RuntimeError("Recovery retirement requires metadata-only transaction.")
    retired = _safe_directory(backup / ".cleanup-restored", create=True)
    _sync_directory(backup)
    destination = retired / f"{folder.name}-{uuid.uuid4().hex}.restored"
    _sync_directory(folder)
    folder.rename(destination)
    _sync_directory(root)
    _sync_directory(retired)


def restore_inbox_quarantine(approved=False):
    """Recover Inbox and staging files after interrupted cleanup."""
    backup = _safe_directory(Path(script_folder) / constants.backup_folder_name, missing_ok=True)
    root = backup / ".cleanup-quarantine"

    if not root.exists():
        print("No cleanup quarantine found.")
        return []

    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe quarantine directory.")

    inbox = _safe_directory(inbox_folder)
    staging = _safe_directory(staging_folder)

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
        purge_state = folder / "purge-state.json"
        if purge_state.exists() or purge_state.is_symlink():
            raise RuntimeError("Purge already started; resume purge instead of restoring.")

        if record_path.is_symlink():
            raise ValueError("Unsafe recovery journal.")

        if not record_path.is_file():
            if held_stage.exists() or held_source.exists():
                raise RuntimeError(
                    f"Quarantined files lack a recovery journal: {folder}"
                )
            if any(entry.name != "recovery.json.tmp" or entry.is_symlink() or not entry.is_file()
                   for entry in folder.iterdir()):
                raise RuntimeError("Unrecognized unpublished recovery files.")
            print(f"Unfinished empty journal: {folder.name}")
            if approved:
                _retire_cleanup_transaction(folder, unpublished=True)
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

        validate_cleanup_provenance(record)
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
        validate_cleanup_provenance(item["record"])
        staged_copy = item["held_stage"] if item["stage_held"] else item["stage"]
        original_copy = item["held_source"] if item["source_held"] else item["source"]
        if file_manifest(staged_copy) != item["record"]["staged_hashes"]:
            raise RuntimeError(f"Staged recovery copy changed: {name}")
        if original_copy.is_symlink():
            raise ValueError("Unsafe original recovery copy.")
        if item["record"]["source_is_dir"]:
            valid = file_manifest(original_copy) == item["record"]["source_hashes"]
        else:
            valid = file_checksum(original_copy).hex() == item["record"]["source_sha256"]
        if not valid:
            raise RuntimeError(f"Original recovery copy changed: {name}")

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

        validate_cleanup_provenance(item["record"])
        _retire_cleanup_transaction(item["folder"])

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

    backup_root = _safe_directory(Path(script_folder) / constants.backup_folder_name)

    quarantine_root = _safe_directory(backup_root / ".cleanup-quarantine", create=True)
    staged_root = _safe_directory(staging_folder)
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
        _check_candidate_provenance(item, file_manifest(stage))

        archived = zip_manifest(originals_zip)

        if archived.get(item["archived_source"]) != (
            item["archived_source_hash"]
        ):
            raise RuntimeError("Original archive changed.")

        current_library = file_manifest(music_library_folder)
        archived_library = zip_manifest(library_zip)
        if archived_library != current_library:
            raise RuntimeError("Library snapshot changed.")
        # Equality between two newly changed trees cannot replace the original
        # per-album completion proof verified at the start of this operation.
        library_root = _safe_directory(music_library_folder)
        for key, album in item["albums"]:
            receipt = completed.get(key)
            if not isinstance(receipt, dict) or len(receipt) < len(album["tracks"]) + 1:
                raise RuntimeError("Incomplete library receipt before quarantine.")
            for destination, checksum in receipt.items():
                path = Path(destination)
                if (path.is_symlink() or not path.is_file()
                        or not path.resolve().is_relative_to(library_root)):
                    raise RuntimeError("Unsafe library receipt before quarantine.")
                relative = path.resolve().relative_to(library_root).as_posix()
                if (current_library.get(relative) != checksum
                        or archived_library.get(relative) != checksum):
                    raise RuntimeError("Library receipt changed before quarantine.")

        quarantine = Path(tempfile.mkdtemp(
            prefix=".mixie-archived-",
            dir=quarantine_root
        ))

        held = quarantine / "staging"

        recovery = {
            "version": 1,
            "transaction_id": uuid.uuid4().hex,
            "staging_provenance": item.get("provenance"),
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
        _sync_directory(quarantine_root)
        _sync_directory(backup_root)

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
    backup_root = _safe_directory(Path(script_folder) / constants.backup_folder_name, missing_ok=True)
    quarantine_root = backup_root / ".cleanup-quarantine"
    stage_root = _safe_directory(staging_folder)

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
        purge_state = folder / "purge-state.json"
        if purge_state.exists() or purge_state.is_symlink():
            raise RuntimeError("Purge already started; resume purge instead of restoring.")

        if record_path.is_symlink() or held.is_symlink():
            raise ValueError(f"Unsafe recovery record: {folder}")

        if not record_path.is_file():
            if held.exists():
                raise RuntimeError(
                    f"Quarantined files lack recovery record: {folder}"
                )
            if any(entry.name != "recovery.json.tmp" or entry.is_symlink() or not entry.is_file()
                   for entry in folder.iterdir()):
                raise RuntimeError("Unrecognized unpublished recovery files.")
            print(f"Unfinished empty quarantine: {folder.name}")
            if approved:
                _retire_cleanup_transaction(folder, unpublished=True)
            continue

        with record_path.open(encoding="utf-8") as source:
            record = json.load(source)

        name = record.get("import_name")
        hashes = record.get("staged_hashes")

        if (
            record.get("version") != 1
            or not _safe_name(name)
            or not isinstance(hashes, dict)
            or not hashes
        ):
            raise ValueError(f"Invalid recovery record: {folder}")

        validate_cleanup_provenance(record)
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

            ready.append((name, held, stage, hashes, record))
            print(f"Recoverable: {name}")

        elif stage.is_dir() and not stage.is_symlink():
            if file_manifest(stage) != hashes:
                raise RuntimeError(
                    f"Existing staging differs: {name}"
                )
            ready.append((name, held, stage, hashes, record))
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

    for name, held, stage, hashes, record in ready:
        validate_cleanup_provenance(record)
        if held.exists():
            if stage.exists() or stage.is_symlink():
                raise RuntimeError(f"Recovery destination became occupied: {stage}")
            if file_manifest(held) != hashes:
                raise RuntimeError(f"Quarantine changed: {name}")
            held.rename(stage)
            _sync_directory(stage_root)
            _sync_directory(held.parent)
        if file_manifest(stage) != hashes:
            raise RuntimeError(
                f"Post-restore verification failed: {name}. "
                "Files retained in staging for inspection."
            )

        validate_cleanup_provenance(record)
        _retire_cleanup_transaction(held.parent)
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
