"""Archive verification regressions; run only through run_isolated.py."""
from pathlib import Path
from unittest.mock import patch
import json
import shutil
import zipfile

from support import WorkspaceCase
import archive
import sort


class ArchiveSafetyTests(WorkspaceCase):
    def staged_album(self):
        track = self.track_path("Legacy Import/Album/01.flac", b"untouched original")
        (track.parent / "cover.png").write_bytes(b"untouched artwork")
        return {"tracks": [{"path": track}]}, track.parents[1]

    def library_file(self):
        path = self.library / "Artist/Album/01.flac"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"finished fixture")
        return path

    def test_reused_backup_requires_manifest(self):
        album, _ = self.staged_album()
        backup = archive.backup_staged_import(album)
        (backup / "manifest.json").unlink()
        with self.assertRaises((RuntimeError, ValueError)):
            archive.backup_staged_import(album)
        self.assertTrue((backup / "original/Album/01.flac").is_file())

    def test_reused_backup_requires_exact_manifest(self):
        album, _ = self.staged_album()
        backup = archive.backup_staged_import(album)
        record = json.loads((backup / "manifest.json").read_text())
        record["import_name"] = "different provenance"
        (backup / "manifest.json").write_text(json.dumps(record))
        with self.assertRaises((RuntimeError, ValueError)):
            archive.backup_staged_import(album)

    def test_reused_backup_rejects_symlink_manifest(self):
        album, _ = self.staged_album()
        backup = archive.backup_staged_import(album)
        manifest = backup / "manifest.json"
        other = self.root / "manifest.json"
        manifest.rename(other)
        manifest.symlink_to(other)
        with self.assertRaises((RuntimeError, ValueError)):
            archive.backup_staged_import(album)

    def test_reused_backup_rejects_symlink_original_root(self):
        album, _ = self.staged_album()
        backup = archive.backup_staged_import(album)
        other = self.root / "saved-original"
        (backup / "original").rename(other)
        (backup / "original").symlink_to(other, target_is_directory=True)
        with self.assertRaises((RuntimeError, ValueError)):
            archive.backup_staged_import(album)

    def test_reused_backup_verifies_payload(self):
        album, _ = self.staged_album()
        backup = archive.backup_staged_import(album)
        (backup / "original/Album/01.flac").write_bytes(b"corruption")
        with self.assertRaises(RuntimeError):
            archive.backup_staged_import(album)

    def test_valid_backup_reuse_does_not_rewrite_manifest(self):
        album, _ = self.staged_album()
        backup = archive.backup_staged_import(album)
        manifest = backup / "manifest.json"
        before = manifest.read_bytes(), manifest.stat().st_mtime_ns
        self.assertEqual(archive.backup_staged_import(album), backup)
        self.assertEqual((manifest.read_bytes(), manifest.stat().st_mtime_ns), before)

    def test_staged_source_mutation_during_backup_is_rejected(self):
        album, source = self.staged_album()
        real_copy = shutil.copytree

        def mutate(src, dst, *args, **kwargs):
            result = real_copy(src, dst, *args, **kwargs)
            if Path(src) == source:
                (source / "late.txt").write_bytes(b"late arrival")
            return result

        with patch.object(archive.shutil, "copytree", mutate):
            with self.assertRaises(RuntimeError):
                archive.backup_staged_import(album)
        self.assertEqual(list(self.backup.iterdir()), [])
        self.assertTrue((source / "Album/01.flac").is_file())

    def test_backup_root_symlink_is_rejected(self):
        album, _ = self.staged_album()
        target = self.root / "other-backups"
        self.backup.rename(target)
        self.backup.symlink_to(target, target_is_directory=True)
        with self.assertRaises((RuntimeError, ValueError)):
            archive.backup_staged_import(album)
        self.assertEqual(list(target.iterdir()), [])

    def test_new_file_during_zip_creation_is_not_omitted(self):
        track = self.library_file()
        real_write = zipfile.ZipFile.write

        def mutate(saved, *args, **kwargs):
            result = real_write(saved, *args, **kwargs)
            (track.parent / "late.txt").write_bytes(b"late arrival")
            return result

        with patch.object(zipfile.ZipFile, "write", mutate):
            with self.assertRaises(RuntimeError):
                archive.create_verified_zip(self.library, "MusicLibrary")
        self.assertEqual(list(self.backup.iterdir()), [])
        self.assertTrue(track.is_file())

    def test_new_file_during_existing_zip_verification_is_detected(self):
        track = self.library_file()
        saved = archive.create_verified_zip(self.library, "MusicLibrary")
        before = saved.read_bytes()
        real_open = zipfile.ZipFile.open
        changed = []

        def mutate(opened, name, *args, **kwargs):
            result = real_open(opened, name, *args, **kwargs)
            if Path(opened.filename) == saved and not changed:
                changed.append(True)
                (track.parent / "late.txt").write_bytes(b"late arrival")
            return result

        with patch.object(zipfile.ZipFile, "open", mutate):
            with self.assertRaises(RuntimeError):
                archive.create_verified_zip(self.library, "MusicLibrary")
        self.assertEqual(saved.read_bytes(), before)

    def test_changed_file_during_zip_creation_still_refused(self):
        track = self.library_file()
        real_write = zipfile.ZipFile.write

        def mutate(saved, *args, **kwargs):
            result = real_write(saved, *args, **kwargs)
            track.write_bytes(b"changed after archiving")
            return result

        with patch.object(zipfile.ZipFile, "write", mutate):
            with self.assertRaises(RuntimeError):
                archive.create_verified_zip(self.library, "MusicLibrary")
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_symlink_source_root_is_rejected(self):
        self.library_file()
        alias = self.root / "library-alias"
        alias.symlink_to(self.library, target_is_directory=True)
        with self.assertRaises((RuntimeError, ValueError)):
            archive.create_verified_zip(alias, "MusicLibrary")

    def test_symlink_inside_source_is_rejected(self):
        track = self.library_file()
        (track.parent / "alias.flac").symlink_to(track)
        with self.assertRaises(ValueError):
            archive.create_verified_zip(self.library, "MusicLibrary")

    def test_stable_library_archive_reuse_is_exact_and_nonwriting(self):
        self.library_file()
        saved = archive.create_verified_zip(self.library, "MusicLibrary")
        before = saved.read_bytes(), saved.stat().st_mtime_ns
        self.assertEqual(archive.create_verified_zip(self.library, "MusicLibrary"), saved)
        self.assertEqual((saved.read_bytes(), saved.stat().st_mtime_ns), before)
        self.assertEqual(sort.zip_manifest(saved), sort.file_manifest(self.library))

    def test_changed_original_filename_never_replaces_archived_bytes(self):
        source = self.inbox / "Delivery.zip"
        source.write_bytes(b"first package")
        saved = archive.update_original_downloads_archive()
        before = saved.read_bytes()
        source.write_bytes(b"new package under same filename")
        with self.assertRaisesRegex(ValueError, "changed"):
            archive.update_original_downloads_archive()
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(source.read_bytes(), b"new package under same filename")

    def test_unchanged_bulk_branch_rechecks_source_generation(self):
        source = self.inbox / "Delivery.zip"
        source.write_bytes(b"first package")
        saved = archive.update_original_downloads_archive()
        before = saved.read_bytes()
        real_manifest = sort.file_manifest
        calls = []

        def mutate(folder):
            result = real_manifest(folder)
            if Path(folder) == self.inbox and not calls:
                calls.append(True)
                source.write_bytes(b"changed while verifying unchanged branch")
            return result

        with patch.object(sort, "file_manifest", mutate):
            with self.assertRaises(RuntimeError):
                archive.update_original_downloads_archive()
        self.assertEqual(saved.read_bytes(), before)

    def test_changed_directory_inventory_defers_without_replacing_bulk(self):
        source = self.inbox / "Delivery"
        source.mkdir()
        (source / "01.flac").write_bytes(b"one")
        (source / "02.flac").write_bytes(b"two")
        saved = archive.update_original_downloads_archive()
        before = saved.read_bytes()
        (source / "02.flac").unlink()
        with self.assertRaisesRegex(ValueError, "changed"):
            archive.update_original_downloads_archive()
        self.assertEqual(saved.read_bytes(), before)

    def test_inventory_addition_inside_final_hash_pass_is_detected(self):
        track = self.library_file()
        real_checksum = sort.file_checksum
        calls = []

        def mutate(path):
            result = real_checksum(path)
            if Path(path) == track:
                calls.append(True)
                if len(calls) == 3:
                    (track.parent / "late.txt").write_bytes(b"arrived inside final scan")
            return result

        with patch.object(sort, "file_checksum", mutate):
            with self.assertRaises(RuntimeError):
                archive.create_verified_zip(self.library, "MusicLibrary")
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_file_mutation_inside_final_hash_pass_is_detected(self):
        track = self.library_file()
        real_checksum = sort.file_checksum
        calls = []

        def mutate(path):
            result = real_checksum(path)
            if Path(path) == track:
                calls.append(True)
                if len(calls) == 3:
                    track.write_bytes(b"changed inside final scan")
            return result

        with patch.object(sort, "file_checksum", mutate):
            with self.assertRaises(RuntimeError):
                archive.create_verified_zip(self.library, "MusicLibrary")
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_absent_old_sources_remain_preserved_in_cumulative_archive(self):
        source = self.inbox / "First.zip"
        source.write_bytes(b"old original")
        saved = archive.update_original_downloads_archive()
        first = sort.zip_manifest(saved)
        source.unlink()
        (self.inbox / "Second.zip").write_bytes(b"new original")
        archive.update_original_downloads_archive()
        current = sort.zip_manifest(saved)
        self.assertEqual(current["First.zip"], first["First.zip"])
        self.assertIn("Second.zip", current)
