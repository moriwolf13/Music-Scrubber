from pathlib import Path
import hashlib
import json
import shutil
import tempfile
import constants
import tag
import zipfile
import os

backup_folder = tag.script_folder / constants.backup_folder_name

def file_hash(path):
    checksum = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()

def snapshot_files(folder):
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symbolic link in import: {path}")
        if path.is_file():
            relative = path.relative_to(folder).as_posix()
            files[relative] = file_hash(path)
    return files

# Preserve an untouched, verified copy of a staged import
def backup_staged_import(album):
    staging = tag.staging_folder.resolve()
    tracks = [
        Path(track["path"]).resolve()
        for track in album["tracks"]
    ]

    # All tracks must belong to the same staged import
    import_names = set()
    for track in tracks:
        relative = track.relative_to(staging)
        if len(relative.parts) < 2:
            raise ValueError("Unexpected staging structure.")
        import_names.add(relative.parts[0])
    if len(import_names) != 1:
        raise ValueError("Album spans multiple imports.")
    import_name = import_names.pop()
    source = staging / import_name
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Invalid staged import.")
    originals = snapshot_files(source)
    if not originals:
        raise ValueError("No files to back up.")
    manifest = {
        "import_name": import_name,
        "files": originals
    }
    encoded = json.dumps(
        manifest, sort_keys=True
    ).encode("utf-8")
    snapshot_id = hashlib.sha256(encoded).hexdigest()[:20]
    import_id = hashlib.sha256(
        import_name.encode("utf-8")
    ).hexdigest()[:12]
    backup_folder.mkdir(parents=True, exist_ok=True)
    destination = backup_folder / f"{import_id}-{snapshot_id}"

    # Reuse only an existing backup that passes verification
    if destination.exists():
        saved = snapshot_files(destination / "original")
        if saved != originals:
            raise RuntimeError("Existing backup failed verification.")
        print(f"Verified existing backup: {destination.name}")
        return destination
    temporary = Path(tempfile.mkdtemp(
        prefix=".mixie-backup-",
        dir=backup_folder
    ))
    try:
        shutil.copytree(source, temporary / "original")
        saved = snapshot_files(temporary / "original")
        if saved != originals:
            raise RuntimeError("Backup checksum verification failed.")
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8"
        )
        temporary.rename(destination)
    except Exception:
        shutil.rmtree(temporary)
        raise

    print(f"Backup created and verified: {destination.name}")
    print(f"Files protected: {len(originals)}")
    return destination

def create_verified_zip(source_folder, label):
    source = Path(source_folder).resolve()

    if not source.is_dir():
        raise FileNotFoundError(source)

    if not label.isidentifier():
        raise ValueError("Invalid archive label.")

    files = {}

    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink in source: {path}")

        if path.is_file():
            name = path.relative_to(source).as_posix()
            files[name] = (path, file_hash(path))

    if not files:
        raise ValueError("Nothing to archive.")

    manifest = {
        name: checksum
        for name, (_, checksum) in files.items()
    }

    archive_id = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    backup_folder.mkdir(parents=True, exist_ok=True)
    destination = backup_folder / f"{label}-{archive_id}.zip"

    if destination.resolve().is_relative_to(source):
        raise ValueError("Archive cannot be inside its source.")

    def verify(zip_path):
        with zipfile.ZipFile(zip_path) as saved:
            names = saved.namelist()
            if len(names) != len(files) or set(names) != set(files):
                raise RuntimeError("Archive contents are incomplete.")
            for name in names:
                digest = hashlib.sha256()
                with saved.open(name) as entry:
                    for chunk in iter(
                        lambda: entry.read(1024 * 1024), b""
                    ):
                        digest.update(chunk)
                if digest.hexdigest() != files[name][1]:
                    raise RuntimeError(
                        f"Archive verification failed: {name}"
                    )
        for path, expected in files.values():
            if file_hash(path) != expected:
                raise RuntimeError(
                    f"Source changed during archiving: {path}"
                )
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("Unsafe existing archive.")
        verify(destination)
        print(f"Verified existing archive: {destination.name}")
        return destination
    descriptor, temporary = tempfile.mkstemp(
        prefix=".mixie-zip-",
        suffix=".tmp",
        dir=backup_folder
    )
    os.close(descriptor)
    try:
        with zipfile.ZipFile(
            temporary, "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True
        ) as saved:
            for name, (path, _) in sorted(files.items()):
                saved.write(path, arcname=name)
        verify(temporary)
        with open(temporary, "rb") as completed:
            os.fsync(completed.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(
        f"Created and verified: {destination.name} "
        f"({len(files)} files)"
    )
    return destination

def update_original_downloads_archive():
    """Maintain a verified cumulative archive of untouched downloads."""
    import sort

    inbox = Path(sort.inbox_folder)
    backup_folder.mkdir(parents=True, exist_ok=True)
    destination = backup_folder / "OriginalDownloads-Bulk.zip"

    current = sort.file_manifest(inbox)

    if destination.is_symlink():
        raise ValueError("Bulk archive is a symbolic link.")
    if destination.exists() and not destination.is_file():
        raise ValueError("Invalid bulk archive.")

    previous = (
        sort.zip_manifest(destination)
        if destination.exists() else {}
    )

    if not current and not previous:
        raise ValueError("No downloads to archive.")

    # Never replace a previously archived original with different bytes
    for name, checksum in current.items():
        if name in previous and previous[name] != checksum:
            raise ValueError(f"Original download changed: {name}")

    combined = {**previous, **current}

    if len({name.casefold() for name in combined}) != len(combined):
        raise ValueError("Case-insensitive archive filename conflict.")

    if destination.exists() and combined == previous:
        print(f"Cumulative archive verified: {len(previous)} files")
        return destination

    descriptor, temporary = tempfile.mkstemp(
        prefix=".mixie-bulk-",
        suffix=".tmp",
        dir=backup_folder
    )
    os.close(descriptor)

    try:
        with zipfile.ZipFile(
            temporary, "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True
        ) as output:

            # Preserve all previously archived downloads
            if previous:
                with zipfile.ZipFile(destination) as old:
                    for name in sorted(previous):
                        with old.open(name) as source, output.open(
                            name, "w", force_zip64=True
                        ) as saved:
                            shutil.copyfileobj(
                                source, saved, length=1024 * 1024
                            )

            # Add only genuinely new downloads
            for name in sorted(current):
                if name in previous:
                    continue

                with open(inbox / name, "rb") as source, output.open(
                    name, "w", force_zip64=True
                ) as saved:
                    shutil.copyfileobj(
                        source, saved, length=1024 * 1024
                    )

        # Verify every archived file and detect source changes
        if sort.zip_manifest(temporary) != combined:
            raise RuntimeError("Bulk archive verification failed.")

        if sort.file_manifest(inbox) != current:
            raise RuntimeError("Inbox changed during archiving.")

        if previous and sort.zip_manifest(destination) != previous:
            raise RuntimeError("Previous archive changed during update.")

        with open(temporary, "rb") as saved:
            os.fsync(saved.fileno())

        # Publish only after complete verification
        os.replace(temporary, destination)

    finally:
        Path(temporary).unlink(missing_ok=True)

    print(f"Cumulative archive verified: {len(combined)} files")
    return destination
