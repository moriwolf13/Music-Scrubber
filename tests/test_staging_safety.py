"""Staging/provenance regressions; run only through run_isolated.py."""
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
import json
import shutil
import stat
import zipfile

from support import WorkspaceCase, make_audio, metadata_record
import archive
import sort
import tag


class StagingSafetyTests(WorkspaceCase):
    def test_copy_mutation_rolls_back_only_files_created_by_this_plan(self):
        existing = self.library / "existing.flac"
        existing.write_bytes(b"already verified user file")
        plan = []
        for index in range(2):
            source = self.root / f"copy-source-{index}.flac"
            source.write_bytes(b"independent fixture " + bytes([index]))
            plan.append({"source": str(source), "destination": str(self.library / f"new-{index}.flac")})
        checksum = sort.file_checksum

        def changing(path):
            if Path(path) == Path(plan[-1]["destination"]):
                raise RuntimeError("Synthetic mutation during verification")
            return checksum(path)

        with patch.object(sort, "file_checksum", side_effect=changing), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(RuntimeError, "mutation"):
                sort.copy_sorting_plan(plan)
        self.assertEqual(b"already verified user file", existing.read_bytes())
        self.assertTrue(all(not Path(item["destination"]).exists() for item in plan))
        self.assertTrue(all(Path(item["source"]).is_file() for item in plan))

    def folder_source(self, name="The Offspring", albums=("Album",)):
        source = self.inbox / name
        for album in albums:
            path = source / album / "01.flac"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(album.encode() + b" original")
        return source

    def zip_source(self, name="The Offspring.zip", members=None):
        source = self.inbox / name
        with zipfile.ZipFile(source, "w") as output:
            for relative, data in (members or {"Album/01.flac": b"ZIP original"}).items():
                output.writestr(relative, data)
        return source

    def ingest(self):
        output = StringIO()
        with redirect_stdout(output):
            staged = [Path(path) for path in sort.sort_inbox()]
        return staged, output.getvalue()

    def stage_one(self, source=None):
        if source is None:
            source = self.folder_source()
        staged, output = self.ingest()
        self.assertEqual(len(staged), 1, output)
        return source, staged[0]

    def grouped(self, stage):
        result = {}
        for path in sorted(stage.rglob("*.flac")):
            album = path.parent.name
            key = (stage.name, album.casefold())
            result.setdefault(key, {"album": album, "tracks": []})["tracks"].append(
                metadata_record(path, album=(album,)))
        return result

    def recovery_fixture(self, archived=False, stage_held=True, source_held=True):
        name = "Recovery Import"
        source = self.folder_source(name)
        stage = self.stage / name
        shutil.copytree(source, stage)
        hashes = sort.file_manifest(stage)
        root = self.backup / ".cleanup-quarantine"
        folder = root / (".mixie-archived-fixture" if archived else ".mixie-inbox-fixture")
        folder.mkdir(parents=True)
        record = {
            "version": 1, "import_name": name, "staged_hashes": hashes,
            "transaction_id": "fixture-transaction",
        }
        if archived:
            record.update(original_staging_path=str(stage), original_archive="unused",
                          archived_source=name + ".zip", archived_source_hash="unused",
                          library_snapshot="unused")
        else:
            record.update(kind="inbox_cleanup", source_name=name, source_is_dir=True,
                          source_sha256=None, source_hashes=hashes)
        (folder / "recovery.json").write_text(json.dumps(record), encoding="utf-8")
        if stage_held:
            stage.rename(folder / "staging")
        if source_held and not archived:
            source.rename(folder / "original")
        return folder, stage, source, record

    def cleanup_fixture(self, is_folder=False):
        source = self.folder_source("Delivery") if is_folder else self.zip_source("Delivery.ZIP")
        _, stage = self.stage_one(source)
        grouped = self.grouped(stage)
        completed = {}
        for key, album in grouped.items():
            destination = self.library / "Artist" / album["album"]
            destination.mkdir(parents=True)
            for filename in ("01.flac", "cover.png"):
                (destination / filename).write_bytes(b"finished " + filename.encode())
            completed[key] = {str(path): sort.file_checksum(path).hex()
                              for path in destination.iterdir()}
        bulk = archive.update_original_downloads_archive()
        snapshot = archive.create_verified_zip(self.library, "MusicLibrary")
        return source, stage, grouped, completed, bulk, snapshot

    def test_real_inbox_quarantine_restore_keeps_managed_provenance(self):
        source, stage, grouped, completed, bulk, snapshot = self.cleanup_fixture()
        provenance = sort.staging_provenance(stage.name)
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            self.assertEqual(sort.quarantine_inbox_cleanup(
                completed, bulk, snapshot, approved=True), [stage.name])
        folder = next((self.backup / ".cleanup-quarantine").iterdir())
        record = json.loads((folder / "recovery.json").read_text())
        self.assertEqual(record["staging_provenance"], provenance)
        self.assertEqual(sort.validate_cleanup_provenance(record), provenance)
        self.assertFalse(source.exists())
        self.assertFalse(stage.exists())
        sort.restore_inbox_quarantine(approved=True)
        self.assertTrue(source.is_file())
        self.assertTrue(sort.is_committed_stage(stage))

    def test_real_directory_quarantine_restore_keeps_managed_provenance(self):
        source, stage, grouped, completed, bulk, snapshot = self.cleanup_fixture(is_folder=True)
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            self.assertEqual(sort.quarantine_inbox_cleanup(
                completed, bulk, snapshot, approved=True), [stage.name])
        sort.restore_inbox_quarantine(approved=True)
        self.assertEqual(sort.file_manifest(source), sort.file_manifest(stage))
        self.assertTrue(sort.is_committed_stage(stage))

    def test_real_archived_quarantine_restore_preserves_exact_source_name(self):
        source, stage, grouped, completed, bulk, snapshot = self.cleanup_fixture()
        source.unlink()
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            self.assertEqual(sort.quarantine_archived_cleanup(
                completed, bulk, snapshot, approved=True), [stage.name])
        folder = next((self.backup / ".cleanup-quarantine").iterdir())
        record = json.loads((folder / "recovery.json").read_text())
        self.assertEqual(record["archived_source"], "Delivery.ZIP")
        self.assertEqual(sort.validate_cleanup_provenance(record), record["staging_provenance"])
        sort.restore_archived_quarantine(approved=True)
        self.assertTrue(sort.is_committed_stage(stage))
        self.assertFalse(folder.exists())

    def test_managed_quarantine_crash_between_moves_can_restore(self):
        source, stage, grouped, completed, bulk, snapshot = self.cleanup_fixture()
        real_rename = Path.rename

        def fail_source(path, target):
            if path == source:
                raise OSError("simulated source-move crash")
            return real_rename(path, target)

        with patch.object(tag, "group_staged_albums", return_value=grouped):
            with patch.object(Path, "rename", fail_source):
                with self.assertRaisesRegex(OSError, "source-move crash"):
                    sort.quarantine_inbox_cleanup(completed, bulk, snapshot, approved=True)
        self.assertTrue(source.is_file())
        self.assertFalse(stage.exists())
        sort.restore_inbox_quarantine(approved=True)
        self.assertTrue(sort.is_committed_stage(stage))

    def test_archived_quarantine_rechecks_receipts_not_just_snapshot_equality(self):
        source, stage, grouped, completed, bulk, snapshot = self.cleanup_fixture()
        source.unlink()
        real_verify = sort.verify_archived_cleanup

        def change_verified_outputs(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            next(self.library.rglob("01.flac")).write_bytes(b"changed library output")
            with zipfile.ZipFile(snapshot, "w") as saved:
                for relative in sort.file_manifest(self.library):
                    saved.write(self.library / relative, relative)
            return result

        with patch.object(tag, "group_staged_albums", return_value=grouped):
            with patch.object(sort, "verify_archived_cleanup", change_verified_outputs):
                with self.assertRaises(RuntimeError):
                    sort.quarantine_archived_cleanup(completed, bulk, snapshot, approved=True)
        self.assertTrue(stage.is_dir())
        self.assertFalse(any((self.backup / ".cleanup-quarantine").glob("*/staging")))

    def test_inbox_verification_rejects_symlinked_backup_ancestor(self):
        _, _, grouped, completed, bulk, snapshot = self.cleanup_fixture()
        other = self.root / "backup-target"
        self.backup.rename(other)
        self.backup.symlink_to(other, target_is_directory=True)
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            with self.assertRaises(ValueError):
                sort.verify_inbox_cleanup(completed, bulk, snapshot)

    def test_folder_backslash_name_is_rejected_before_publication(self):
        source = self.inbox / "Backslash"
        source.mkdir()
        (source / "Album\\01.flac").write_bytes(b"original")
        staged, _ = self.ingest()
        self.assertEqual(staged, [])
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertTrue((source / "Album\\01.flac").is_file())

    def test_folder_case_ancestor_conflict_is_rejected_before_publication(self):
        source = self.folder_source("Case conflict")
        (source / "album").mkdir()
        (source / "album/02.flac").write_bytes(b"second original")
        staged, _ = self.ingest()
        self.assertEqual(staged, [])
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_cleanup_provenance_rejects_source_alias_and_generation_change(self):
        _, stage, grouped, completed, bulk, snapshot = self.cleanup_fixture()
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            sort.quarantine_inbox_cleanup(completed, bulk, snapshot, approved=True)
        journal = next((self.backup / ".cleanup-quarantine").glob("*/recovery.json"))
        record = json.loads(journal.read_text())
        with self.assertRaises(RuntimeError):
            sort.validate_cleanup_provenance({**record, "source_name": "different.ZIP"})
        changed = {**record["staging_provenance"], "transaction_id": "f" * 32}
        sort._write_staging_provenance(stage.name, changed)
        with self.assertRaises(RuntimeError):
            sort.validate_cleanup_provenance(record)
        self.assertTrue(journal.is_file())

    def test_missing_provenance_does_not_fall_back_after_source_rename(self):
        source, stage = self.stage_one()
        sort._provenance_path(stage.name).unlink()
        source.rename(self.inbox / stage.name)
        grouped = self.grouped(stage)
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            self.assertEqual(sort.plan_ingest_cleanup(grouped), [])
        self.assertFalse(sort.is_committed_stage(stage))

    def test_restore_crash_after_payload_move_can_finish_retirement(self):
        folder, stage, _, _ = self.recovery_fixture(archived=True)
        with patch.object(sort, "_retire_cleanup_transaction", side_effect=OSError("retirement crash")):
            with self.assertRaisesRegex(OSError, "retirement crash"):
                sort.restore_archived_quarantine(approved=True)
        self.assertTrue(stage.is_dir())
        self.assertTrue((folder / "recovery.json").exists())
        self.assertEqual(sort.restore_archived_quarantine(approved=True), [stage.name])
        self.assertFalse(folder.exists())

    def test_same_stem_directory_and_zip_have_distinct_provenance(self):
        folder = self.folder_source()
        zipped = self.zip_source()
        staged, output = self.ingest()
        self.assertEqual(len(staged), 2, output)
        self.assertEqual(len({path.name.casefold() for path in staged}), 2)
        records = [sort.staging_provenance(path.name) for path in staged]
        self.assertEqual({item["source_name"] for item in records}, {folder.name, zipped.name})
        self.assertEqual({item["source_kind"] for item in records}, {"directory", "zip"})
        self.assertEqual({item["state"] for item in records}, {"committed"})

    def test_unchanged_repeat_reuses_committed_stage_and_record(self):
        source, stage = self.stage_one()
        before = sort.staging_provenance(stage.name)
        hashes = sort.file_manifest(stage)
        staged, output = self.ingest()
        self.assertEqual(staged, [], output)
        self.assertIn("verified unchanged", output.lower())
        self.assertEqual(sort.staging_provenance(stage.name), before)
        self.assertEqual(sort.file_manifest(stage), hashes)
        self.assertEqual(sort.file_manifest(source), hashes)

    def test_changed_source_is_not_silently_reused(self):
        source, stage = self.stage_one()
        before = sort.file_manifest(stage)
        (source / "Album/01.flac").write_bytes(b"changed delivery")
        staged, output = self.ingest()
        self.assertEqual(staged, [])
        self.assertIn("changed", output.lower())
        self.assertEqual(sort.file_manifest(stage), before)

    def test_provenance_is_outside_untouched_payload(self):
        source, stage = self.stage_one()
        record = sort.staging_provenance(stage.name)
        self.assertEqual(sort.file_manifest(stage), sort.file_manifest(source))
        self.assertEqual(record["payload_hashes"], sort.file_manifest(source))
        self.assertTrue(record["transaction_id"])
        self.assertTrue(sort.is_committed_stage(stage))
        self.assertEqual(sort.committed_staging_imports(), [stage])

    def test_partial_legacy_destination_requires_explicit_reconciliation(self):
        source = self.folder_source(albums=("Album", "Second"))
        legacy = self.stage / source.name
        (legacy / "Album").mkdir(parents=True)
        shutil.copy2(source / "Album/01.flac", legacy / "Album/01.flac")
        staged, output = self.ingest()
        self.assertEqual(staged, [])
        self.assertIn("unverified", output.lower())
        self.assertFalse(sort.is_committed_stage(legacy))

    def test_pending_import_blocks_new_staging_without_deleting_it(self):
        self.folder_source()
        pending = self.track_path(".mixie-pending-crashed/Album/01.flac")
        with self.assertRaisesRegex(RuntimeError, "[Pp]ending"):
            sort.sort_inbox()
        self.assertTrue(pending.is_file())
        self.assertFalse(sort.is_committed_stage(pending.parents[1]))

    def test_staged_payload_mutation_invalidates_commit(self):
        _, stage = self.stage_one()
        (stage / "Album/01.flac").write_bytes(b"partial replacement")
        self.assertFalse(sort.is_committed_stage(stage))
        staged, output = self.ingest()
        self.assertEqual(staged, [])
        self.assertIn("changed", output.lower())

    def test_directory_mutation_during_copy_is_not_published(self):
        source = self.folder_source()
        real_copy = shutil.copytree

        def changing_copy(src, dst, *args, **kwargs):
            result = real_copy(src, dst, *args, **kwargs)
            (source / "late.txt").write_bytes(b"arrived during copy")
            return result

        with patch.object(sort.shutil, "copytree", changing_copy):
            staged, _ = self.ingest()
        self.assertEqual(staged, [])
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertTrue((source / "late.txt").exists())

    def test_corrupted_loose_copy_is_not_published(self):
        source = self.inbox / "track.flac"
        source.write_bytes(b"complete original")
        real_copy = shutil.copy2

        def corrupt_copy(src, dst, *args, **kwargs):
            result = real_copy(src, dst, *args, **kwargs)
            Path(dst).write_bytes(b"partial")
            return result

        with patch.object(sort.shutil, "copy2", corrupt_copy):
            staged, _ = self.ingest()
        self.assertEqual(staged, [])
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertEqual(source.read_bytes(), b"complete original")

    def test_zip_container_mutation_during_extraction_is_not_published(self):
        source = self.zip_source()
        real_extract = zipfile.ZipFile.extractall

        def changing_extract(saved, *args, **kwargs):
            result = real_extract(saved, *args, **kwargs)
            with source.open("ab") as stream:
                stream.write(b"changed package bytes")
            return result

        with patch.object(zipfile.ZipFile, "extractall", changing_extract):
            staged, _ = self.ingest()
        self.assertEqual(staged, [])
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_commit_record_crash_retains_uncommitted_payload(self):
        self.folder_source()
        writer = sort._write_staging_provenance

        def fail_commit(name, record):
            if record["state"] == "committed":
                raise OSError("simulated commit-record crash")
            return writer(name, record)

        with patch.object(sort, "_write_staging_provenance", fail_commit):
            staged, _ = self.ingest()
        self.assertEqual(staged, [])
        retained = list(self.stage.iterdir())
        self.assertEqual(len(retained), 1)
        self.assertFalse(sort.is_committed_stage(retained[0]))
        staged, output = self.ingest()
        self.assertEqual(staged, [])
        self.assertIn("uncommitted", output.lower())

    def test_symlinked_staging_root_is_rejected(self):
        self.folder_source()
        target = self.root / "other-stage"
        self.stage.rename(target)
        self.stage.symlink_to(target, target_is_directory=True)
        with self.assertRaises((ValueError, RuntimeError)):
            sort.sort_inbox()
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_source_and_nested_link_are_not_staged(self):
        source = self.folder_source()
        (self.inbox / "Alias").symlink_to(source, target_is_directory=True)
        (source / "Album/link.flac").symlink_to(source / "Album/01.flac")
        staged, _ = self.ingest()
        self.assertEqual(staged, [])
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_noncanonical_zip_paths_are_rejected(self):
        for index, member in enumerate(("Album\\01.flac", "Album/./01.flac", "Album//01.flac")):
            with self.subTest(member=member):
                source = self.zip_source(f"bad-{index}.zip", {member: b"original"})
                staged, _ = self.ingest()
                self.assertEqual(staged, [])
                source.unlink()
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_zip_case_ancestor_conflict_is_rejected(self):
        self.zip_source(members={"Album/01.flac": b"one", "album/02.flac": b"two"})
        staged, _ = self.ingest()
        self.assertEqual(staged, [])

    def test_zip_manifest_and_extraction_share_canonical_names(self):
        source = self.inbox / "normal.ZIP"
        with zipfile.ZipFile(source, "w") as saved:
            saved.writestr("Album/", b"")
            saved.writestr("Album/01.flac", b"original")
            saved.writestr("Album/Artwork/cover.png", b"original artwork")
        _, stage = self.stage_one(source)
        self.assertEqual(sort.file_manifest(stage), sort.zip_manifest(source))

    def test_zip_traversal_duplicate_and_symlink_remain_rejected(self):
        for members in (("../01.flac",), ("01.flac", "01.flac"), ("C:/01.flac",)):
            with self.subTest(members=members):
                data = BytesIO()
                with zipfile.ZipFile(data, "w") as saved:
                    for name in members:
                        saved.writestr(name, b"fixture")
                data.seek(0)
                with zipfile.ZipFile(data) as saved, self.assertRaises(ValueError):
                    sort.check_zip_safety(saved, self.stage, "unsafe.zip")
        data = BytesIO()
        with zipfile.ZipFile(data, "w") as saved:
            entry = zipfile.ZipInfo("01.flac")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            saved.writestr(entry, "elsewhere")
        data.seek(0)
        with zipfile.ZipFile(data) as saved, self.assertRaises(ValueError):
            sort.check_zip_safety(saved, self.stage, "link.zip")

    def test_real_generated_flac_survives_staging_untouched(self):
        source = make_audio(self.inbox / "Synthetic/Album/01.flac",
                            artist="Artist", album="Album", title="Silence", tracknumber="1")
        original = sort.file_checksum(source)
        _, stage = self.stage_one(source.parents[1])
        self.assertEqual(sort.file_checksum(stage / "Album/01.flac"), original)
        self.assertEqual(tag.read_track_metadata(stage / "Album/01.flac")["album"], ("Album",))

    def test_cleanup_resolves_same_stem_sources_from_provenance(self):
        self.folder_source()
        self.zip_source()
        stages, output = self.ingest()
        self.assertEqual(len(stages), 2, output)
        grouped = {}
        for stage in stages:
            grouped.update(self.grouped(stage))
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            ready = sort.plan_ingest_cleanup(grouped)
        self.assertEqual({item["source"].name for item in ready}, {"The Offspring", "The Offspring.zip"})
        self.assertTrue(all(item["provenance"]["state"] == "committed" for item in ready))

    def test_cleanup_cannot_infer_missing_managed_provenance(self):
        name = "Album--mixie-0123456789abcdef"
        source = self.folder_source(name)
        stage = self.stage / name
        shutil.copytree(source, stage)
        grouped = self.grouped(stage)
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            self.assertEqual(sort.plan_ingest_cleanup(grouped), [])

    def test_cleanup_rejects_payload_and_source_changed_together(self):
        source, stage = self.stage_one()
        for root in (source, stage):
            (root / "Album/01.flac").write_bytes(b"matching but not committed")
        grouped = self.grouped(stage)
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            self.assertEqual(sort.plan_ingest_cleanup(grouped), [])

    def test_multi_album_cleanup_requires_all_albums_completed(self):
        _, stage = self.stage_one(self.folder_source(albums=("First", "Second")))
        grouped = self.grouped(stage)
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            self.assertEqual(sort.plan_ingest_cleanup([next(iter(grouped))]), [])
            self.assertEqual(len(sort.plan_ingest_cleanup(grouped)), 1)

    def test_archived_cleanup_uses_exact_provenance_zip_name(self):
        source, stage = self.stage_one(self.zip_source("Delivery.ZIP"))
        bulk = archive.update_original_downloads_archive()
        source.unlink()
        grouped = self.grouped(stage)
        completed = {key: {"track": "hash", "cover": "hash"} for key in grouped}
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            ready = sort.plan_archived_cleanup(completed, bulk)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["archived_source"], "Delivery.ZIP")

    def test_archived_cleanup_cannot_infer_missing_managed_provenance(self):
        name = "Album--mixie-0123456789abcdef"
        path = self.track_path(name + "/Album/01.flac")
        stage = path.parents[1]
        inner = BytesIO()
        with zipfile.ZipFile(inner, "w") as saved:
            saved.write(path, "Album/01.flac")
        bulk = self.backup / "OriginalDownloads-Bulk.zip"
        with zipfile.ZipFile(bulk, "w") as saved:
            saved.writestr(name + ".zip", inner.getvalue())
        grouped = self.grouped(stage)
        completed = {key: {"track": "hash", "cover": "hash"} for key in grouped}
        with patch.object(tag, "group_staged_albums", return_value=grouped):
            self.assertEqual(sort.plan_archived_cleanup(completed, bulk), [])

    def test_inbox_restore_retires_journal_without_discarding_evidence(self):
        folder, stage, source, record = self.recovery_fixture()
        self.assertEqual(sort.restore_inbox_quarantine(approved=True), [stage.name])
        self.assertEqual(sort.file_manifest(stage), record["staged_hashes"])
        self.assertEqual(sort.file_manifest(source), record["source_hashes"])
        self.assertFalse(folder.exists())
        records = list((self.backup / ".cleanup-restored").glob("*/recovery.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text()), record)

    def test_inbox_restore_recovers_crash_between_moves(self):
        folder, stage, source, record = self.recovery_fixture(source_held=False)
        self.assertEqual(sort.restore_inbox_quarantine(approved=True), [stage.name])
        self.assertTrue(source.is_dir())
        self.assertEqual(sort.file_manifest(stage), record["staged_hashes"])
        self.assertFalse(folder.exists())

    def test_restore_refuses_purge_started_state_before_any_move(self):
        for archived in (False, True):
            with self.subTest(archived=archived):
                folder, stage, _, _ = self.recovery_fixture(archived=archived)
                (folder / "purge-state.json").write_text("{}")
                operation = sort.restore_archived_quarantine if archived else sort.restore_inbox_quarantine
                with self.assertRaisesRegex(RuntimeError, "[Pp]urge"):
                    operation(approved=True)
                self.assertTrue((folder / "recovery.json").is_file())
                self.assertTrue((folder / "staging").is_dir())
                self.assertFalse(stage.exists())
                shutil.rmtree(folder)

    def test_archived_restore_retires_journal(self):
        folder, stage, _, record = self.recovery_fixture(archived=True)
        self.assertEqual(sort.restore_archived_quarantine(approved=True), [stage.name])
        self.assertEqual(sort.file_manifest(stage), record["staged_hashes"])
        self.assertFalse(folder.exists())
        self.assertEqual(len(list((self.backup / ".cleanup-restored").glob("*/recovery.json"))), 1)

    def test_archived_restore_retires_already_restored_crash(self):
        folder, stage, _, _ = self.recovery_fixture(archived=True, stage_held=False)
        self.assertEqual(sort.restore_archived_quarantine(approved=True), [stage.name])
        self.assertTrue(stage.is_dir())
        self.assertFalse(folder.exists())

    def test_empty_and_temporary_journals_retire_only_with_approval(self):
        root = self.backup / ".cleanup-quarantine"
        for suffix in ("empty", "temporary"):
            folder = root / (".mixie-inbox-" + suffix)
            folder.mkdir(parents=True)
            if suffix == "temporary":
                (folder / "recovery.json.tmp").write_bytes(b"interrupted metadata")
        sort.restore_inbox_quarantine()
        self.assertEqual(len(list(root.iterdir())), 2)
        sort.restore_inbox_quarantine(approved=True)
        self.assertEqual(list(root.iterdir()), [])
        retired = list((self.backup / ".cleanup-restored").iterdir())
        self.assertEqual(len(retired), 2)
        self.assertEqual(next(path / "recovery.json.tmp" for path in retired
                              if (path / "recovery.json.tmp").exists()).read_bytes(), b"interrupted metadata")

    def test_restore_conflict_does_not_overwrite_live_data(self):
        folder, stage, _, _ = self.recovery_fixture()
        stage.mkdir()
        (stage / "keep.txt").write_bytes(b"new data")
        with self.assertRaises(RuntimeError):
            sort.restore_inbox_quarantine(approved=True)
        self.assertEqual((stage / "keep.txt").read_bytes(), b"new data")
        self.assertTrue((folder / "staging").is_dir())

    def test_quarantine_gets_new_transaction_identity_after_restore(self):
        source = self.folder_source("Legacy")
        stage = self.stage / source.name
        shutil.copytree(source, stage)
        hashes = sort.file_manifest(stage)
        item = {"name": stage.name, "stage": stage, "source": source, "albums": [],
                "staged_hashes": hashes, "original_hashes": hashes}
        journals = []
        for _ in range(2):
            with patch.object(sort, "verify_inbox_cleanup", return_value=[item]):
                sort.quarantine_inbox_cleanup({}, "unused", "unused", approved=True)
            record = next((self.backup / ".cleanup-quarantine").glob("*/recovery.json"))
            journals.append(json.loads(record.read_text()))
            sort.restore_inbox_quarantine(approved=True)
        self.assertTrue(journals[0].get("transaction_id"))
        self.assertNotEqual(journals[0]["transaction_id"], journals[1]["transaction_id"])

    def test_archived_quarantine_refuses_symlink_root(self):
        target = self.backup / "elsewhere"
        target.mkdir()
        (self.backup / ".cleanup-quarantine").symlink_to(target, target_is_directory=True)
        with patch.object(sort, "verify_archived_cleanup", return_value=[]):
            with self.assertRaises(ValueError):
                sort.quarantine_archived_cleanup({}, "unused", "unused", approved=True)
        self.assertEqual(list(target.iterdir()), [])
