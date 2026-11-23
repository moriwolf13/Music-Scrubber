"""New stress-log regressions, always in the disposable isolated runner."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import shutil
import warnings
import zipfile

from support import WorkspaceCase
import archive
import sort
import tag


class StressBlockerTests(WorkspaceCase):
    def ingest(self):
        with redirect_stdout(StringIO()) as output:
            staged = [Path(path) for path in sort.sort_inbox()]
        return staged, output.getvalue()

    def legacy_fixture(self, kind="zip"):
        legacy = self.track_path("Legacy/Album/01.flac", b"untouched recording").parents[1]
        (legacy / "Album/cover.jpg").write_bytes(b"untouched cover")
        if kind == "zip":
            source = self.inbox / "Legacy.zip"
            with zipfile.ZipFile(source, "w") as saved:
                for name in sort.file_manifest(legacy):
                    saved.write(legacy / name, name)
        else:
            source = self.inbox / "Legacy"
            shutil.copytree(legacy, source)
        return source, legacy

    def test_exact_legacy_zip_is_adopted_in_place_without_discarding_records(self):
        source, legacy = self.legacy_fixture()
        before = sort.file_manifest(legacy)
        identity = legacy.stat().st_ino
        evidence = self.backup / "old-recovery.json"
        evidence.write_text('{"historical": true}')
        staged, output = self.ingest()
        self.assertEqual([legacy], staged, output)
        self.assertEqual(identity, legacy.stat().st_ino)
        self.assertEqual(before, sort.file_manifest(legacy))
        self.assertTrue(sort.is_committed_stage(legacy))
        record = sort.staging_provenance(legacy.name)
        self.assertIs(record["adopted_legacy"], True)
        self.assertEqual(source.name, record["source_name"])
        self.assertEqual(before, record["payload_hashes"])
        self.assertEqual('{"historical": true}', evidence.read_text())
        staged, output = self.ingest()
        self.assertEqual([], staged, output)
        self.assertEqual(record, sort.staging_provenance(legacy.name))

    def test_exact_legacy_directory_is_adopted_in_place(self):
        source, legacy = self.legacy_fixture("directory")
        staged, output = self.ingest()
        self.assertEqual([legacy], staged, output)
        self.assertEqual(sort.file_manifest(source), sort.file_manifest(legacy))
        self.assertEqual("directory", sort.staging_provenance(legacy.name)["source_kind"])

    def test_partial_legacy_is_not_adopted_or_replaced(self):
        source, legacy = self.legacy_fixture("directory")
        (source / "Album/02.flac").write_bytes(b"not staged yet")
        before = sort.file_manifest(legacy)
        staged, output = self.ingest()
        self.assertEqual([], staged, output)
        self.assertEqual(before, sort.file_manifest(legacy))
        self.assertIsNone(sort.staging_provenance(legacy.name))
        self.assertIn("reconciliation", output.lower())

    def test_legacy_commit_failure_preserves_payload_and_blocks_automatic_readoption(self):
        _, legacy = self.legacy_fixture()
        before = sort.file_manifest(legacy)
        writer = sort._write_staging_provenance

        def fail_commit(name, record):
            if record["state"] == "committed":
                raise OSError("simulated adoption commit crash")
            return writer(name, record)

        with patch.object(sort, "_write_staging_provenance", side_effect=fail_commit):
            staged, _ = self.ingest()
        self.assertEqual([], staged)
        self.assertEqual(before, sort.file_manifest(legacy))
        self.assertFalse(sort.is_committed_stage(legacy))
        staged, output = self.ingest()
        self.assertEqual([], staged, output)
        self.assertIn("uncommitted", output.lower())

    def test_adopted_legacy_payload_mutation_is_not_scanned(self):
        _, legacy = self.legacy_fixture()
        self.ingest()
        (legacy / "Album/01.flac").write_bytes(b"changed payload")
        with patch.object(tag, "read_track_metadata", side_effect=AssertionError("must prune altered stage")):
            with redirect_stdout(StringIO()):
                self.assertEqual({}, tag.scan_albums(legacy))

    def test_same_stem_zip_does_not_claim_adopted_directory_provenance(self):
        source, legacy = self.legacy_fixture("directory")
        zipped = self.inbox / "Legacy.zip"
        with zipfile.ZipFile(zipped, "w") as saved:
            for name in sort.file_manifest(source):
                saved.write(source / name, name)
        staged, output = self.ingest()
        self.assertEqual(2, len(staged), output)
        records = [sort.staging_provenance(path.name) for path in staged]
        self.assertEqual({source.name, zipped.name}, {row["source_name"] for row in records})
        self.assertEqual(2, len({row["transaction_id"] for row in records}))
        self.assertEqual(source.name, sort.staging_provenance(legacy.name)["source_name"])

    def test_missing_adopted_legacy_provenance_never_falls_back_to_unmanaged(self):
        _, legacy = self.legacy_fixture()
        staged, output = self.ingest()
        self.assertEqual([legacy], staged, output)
        sort._provenance_path(legacy.name).unlink()
        with self.assertRaisesRegex(ValueError, "[Mm]issing.*provenance"):
            sort.staging_provenance(legacy.name)
        self.assertFalse(sort.is_committed_stage(legacy))
        with patch.object(tag, "read_track_metadata", side_effect=AssertionError("must prune missing authority")):
            with redirect_stdout(StringIO()):
                self.assertEqual({}, tag.scan_albums(legacy))
        staged, output = self.ingest()
        self.assertEqual([], staged, output)
        self.assertIn("provenance", output.lower())
        self.assertTrue((legacy / "Album/01.flac").is_file())

    def test_duplicate_artwork_alias_case_and_symlink_remain_rejected(self):
        for name, mode in (("Album/COVER.jpg", None), ("Album/cover.jpg", 0o120777)):
            with self.subTest(name=name, mode=mode):
                source = self.inbox / "Aliases.zip"
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(source, "w") as saved:
                        saved.writestr("Album/01.flac", b"recording")
                        saved.writestr("Album/cover.jpg", b"same")
                        entry = zipfile.ZipInfo(name)
                        if mode is not None:
                            entry.external_attr = mode << 16
                        saved.writestr(entry, b"same")
                staged, output = self.ingest()
                self.assertEqual([], staged, output)
                self.assertEqual([], list(self.stage.iterdir()))
                source.unlink()

    def test_duplicate_zip_entries_all_count_toward_expansion_limits(self):
        self.duplicate_zip()
        with patch.object(sort, "MAX_ZIP_UNCOMPRESSED_BYTES", 25):
            staged, output = self.ingest()
        self.assertEqual([], staged, output)
        self.assertIn("limit", output)
        self.assertEqual([], list(self.stage.iterdir()))

    def test_legacy_adoption_cannot_authorize_unrelated_import_alias(self):
        _, legacy = self.legacy_fixture()
        staged, output = self.ingest()
        self.assertEqual([legacy], staged, output)
        record = sort.staging_provenance(legacy.name)
        sort._write_staging_provenance("Unrelated", {**record, "import_name": "Unrelated"})
        with self.assertRaises(ValueError):
            sort.staging_provenance("Unrelated", verify_payload=False)

    def duplicate_zip(self, second=b"same artwork", name="cover.jpg"):
        source = self.inbox / "Duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(source, "w") as saved:
                saved.writestr("Album/01.flac", b"untouched recording")
                saved.writestr("Album/" + name, b"same artwork")
                saved.writestr("Album/" + name, second)
        return source

    def test_byte_identical_duplicate_artwork_zip_members_stage_once_and_archive_raw(self):
        source = self.duplicate_zip()
        raw = source.read_bytes()
        staged, output = self.ingest()
        self.assertEqual(1, len(staged), output)
        self.assertEqual(2, len(sort.file_manifest(staged[0])))
        self.assertEqual(b"same artwork", (staged[0] / "Album/cover.jpg").read_bytes())
        record = sort.staging_provenance(staged[0].name)
        self.assertEqual(["Album/cover.jpg"], record["duplicate_zip_entries"])
        saved = archive.update_original_downloads_archive()
        with zipfile.ZipFile(saved) as bulk:
            self.assertEqual(raw, bulk.read(source.name))
        self.assertEqual(raw, source.read_bytes())

    def test_different_duplicate_artwork_entries_remain_deferred(self):
        source = self.duplicate_zip(b"different artwork")
        raw = source.read_bytes()
        staged, output = self.ingest()
        self.assertEqual([], staged, output)
        self.assertEqual([], list(self.stage.iterdir()))
        self.assertEqual(raw, source.read_bytes())
        self.assertIn("duplicate", output.lower())

    def test_case_distinct_linux_library_paths_archive_without_merging(self):
        files = {"3OH!3/Dirty Mind/01.flac": b"first", "3Oh!3/Dirty Mind/02.flac": b"second"}
        for relative, data in files.items():
            path = self.library / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        saved = archive.create_verified_zip(self.library, "MusicLibrary")
        self.assertEqual(sort.file_manifest(self.library), sort.zip_manifest(saved))
        with zipfile.ZipFile(saved) as contents:
            self.assertEqual(set(files), set(contents.namelist()))
            for relative, data in files.items():
                self.assertEqual(data, contents.read(relative))
        self.assertEqual(saved, archive.create_verified_zip(self.library, "MusicLibrary"))

    def test_case_distinct_original_download_names_remain_independent(self):
        (self.inbox / "Artist.zip").write_bytes(b"one original")
        (self.inbox / "artist.zip").write_bytes(b"another original")
        saved = archive.update_original_downloads_archive()
        self.assertEqual(sort.file_manifest(self.inbox), sort.zip_manifest(saved))
        with zipfile.ZipFile(saved) as contents:
            self.assertEqual(b"one original", contents.read("Artist.zip"))
            self.assertEqual(b"another original", contents.read("artist.zip"))
