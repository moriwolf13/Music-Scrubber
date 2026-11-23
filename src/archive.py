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
    import sort
    return sort.file_checksum(path).hex()

def snapshot_files(folder):
    import sort
    return sort.file_manifest(folder)


def _backup_root():
    import sort
    return sort._safe_directory(backup_folder, create=True)

# Preserve an untouched, verified copy of a staged import
def backup_staged_import(album):
    import sort
    staging = sort._safe_directory(tag.staging_folder)
    tracks = []
    for track in album["tracks"]:
        path = Path(track["path"])
        sort._safe_directory(path.parent)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Invalid staged track.")
        tracks.append(path.resolve())

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
    provenance = sort.staging_provenance(import_name, verify_payload=False)
    if provenance is not None and provenance["payload_hashes"] != originals:
        raise RuntimeError("Staged import differs from committed provenance.")
    manifest = {
        "import_name": import_name,
        "files": originals
    }
    if provenance is not None:
        manifest["staging_provenance"] = provenance

    def verify_source():
        if snapshot_files(source) != originals:
            raise RuntimeError("Staged source changed during backup.")
        if sort.staging_provenance(import_name, verify_payload=False) != provenance:
            raise RuntimeError("Staging provenance changed during backup.")

    encoded = json.dumps(
        manifest, sort_keys=True
    ).encode("utf-8")
    snapshot_id = hashlib.sha256(encoded).hexdigest()[:20]
    import_id = hashlib.sha256(
        import_name.encode("utf-8")
    ).hexdigest()[:12]
    backup = _backup_root()
    destination = backup / f"{import_id}-{snapshot_id}"

    # Payload equality alone does not verify the recovery/provenance manifest.
    if os.path.lexists(destination):
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("Unsafe existing backup.")
        record = destination / "manifest.json"
        if record.is_symlink() or not record.is_file():
            raise ValueError("Existing backup manifest is missing or unsafe.")
        if {path.name for path in destination.iterdir()} != {"original", "manifest.json"}:
            raise ValueError("Unexpected files in existing backup.")
        try:
            saved_manifest = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("Existing backup manifest is invalid.") from error
        if saved_manifest != manifest or snapshot_files(destination / "original") != originals:
            raise RuntimeError("Existing backup failed verification.")
        verify_source()
        print(f"Verified existing backup: {destination.name}")
        return destination
    temporary = Path(tempfile.mkdtemp(
        prefix=".mixie-backup-",
        dir=backup
    ))
    try:
        shutil.copytree(source, temporary / "original")
        saved = snapshot_files(temporary / "original")
        if saved != originals:
            raise RuntimeError("Backup checksum verification failed.")
        verify_source()
        sort._sync_payload(temporary / "original", originals)
        with (temporary / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        sort._sync_directory(temporary)
        verify_source()
        temporary.rename(destination)
        sort._sync_directory(backup)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise

    print(f"Backup created and verified: {destination.name}")
    print(f"Files protected: {len(originals)}")
    return destination

def create_verified_zip(source_folder, label):
    import sort
    source = sort._safe_directory(source_folder)

    if not label.isidentifier():
        raise ValueError("Invalid archive label.")

    manifest = snapshot_files(source)
    if not manifest:
        raise ValueError("Nothing to archive.")
    files = {name: (source / name, checksum) for name, checksum in manifest.items()}

    archive_id = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    backup = _backup_root()
    destination = backup / f"{label}-{archive_id}.zip"

    if destination.resolve().is_relative_to(source):
        raise ValueError("Archive cannot be inside its source.")

    def verify(zip_path):
        if sort.zip_manifest(zip_path) != manifest:
            raise RuntimeError("Archive contents or checksums are incomplete.")
        # Re-enumeration catches additions as well as changed or removed files.
        if snapshot_files(source) != manifest:
            raise RuntimeError("Source inventory changed during archiving.")

    if os.path.lexists(destination):
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("Unsafe existing archive.")
        verify(destination)
        print(f"Verified existing archive: {destination.name}")
        return destination
    descriptor, temporary = tempfile.mkstemp(
        prefix=".mixie-zip-",
        suffix=".tmp",
        dir=backup
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
        if snapshot_files(source) != manifest:
            raise RuntimeError("Source inventory changed before archive publication.")
        os.replace(temporary, destination)
        sort._sync_directory(backup)
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

    inbox = sort._safe_directory(sort.inbox_folder)
    backup = _backup_root()
    destination = backup / "OriginalDownloads-Bulk.zip"

    current = sort.file_manifest(inbox)

    if destination.is_symlink():
        raise ValueError("Bulk archive is a symbolic link.")
    if destination.exists() and not destination.is_file():
        raise ValueError("Invalid bulk archive.")

    previous_digest = file_hash(destination) if destination.exists() else None
    previous = sort.zip_manifest(destination) if destination.exists() else {}

    if not current and not previous:
        raise ValueError("No downloads to archive.")

    # Never replace a previously archived original with different bytes
    for name, checksum in current.items():
        if name in previous and previous[name] != checksum:
            raise ValueError(f"Original download changed: {name}")

    # A still-present directory cannot silently become a different historical
    # tree: its retained prefix would no longer describe this original import.
    for source in inbox.iterdir():
        if source.is_dir() and not source.is_symlink():
            prefix = source.name + "/"
            if any(name.startswith(prefix) and name not in current for name in previous):
                raise ValueError(f"Original download directory changed: {source.name}")

    combined = {**previous, **current}

    # This archive is a case-sensitive inventory, not an extraction plan.
    # Exact Linux names must survive independently; changed exact names still fail.

    if destination.exists() and combined == previous:
        if sort.file_manifest(inbox) != current:
            raise RuntimeError("Inbox changed during archive verification.")
        if file_hash(destination) != previous_digest:
            raise RuntimeError("Previous archive changed during verification.")
        print(f"Cumulative archive verified: {len(previous)} files")
        return destination

    descriptor, temporary = tempfile.mkstemp(
        prefix=".mixie-bulk-",
        suffix=".tmp",
        dir=backup
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

        if previous_digest is not None and (
            file_hash(destination) != previous_digest
            or sort.zip_manifest(destination) != previous
        ):
            raise RuntimeError("Previous archive changed during update.")

        with open(temporary, "rb") as saved:
            os.fsync(saved.fileno())

        # Publish only after complete verification
        os.replace(temporary, destination)
        sort._sync_directory(backup)

    finally:
        Path(temporary).unlink(missing_ok=True)

    print(f"Cumulative archive verified: {len(combined)} files")
    return destination
