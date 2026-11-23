"""Completion/input/provenance regressions. All files are disposable fixtures.

Use tests/run_isolated.py only. Provider calls and unexpected prompts fail closed.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
import hashlib
import json
import shutil
import zipfile
import sys
import fcntl
from unittest.mock import patch

import archive
import audio
import lookup
import mixie
import review
import sort
import tag
from support import WorkspaceCase, make_audio, make_cover, metadata_record


class CompletionTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        for module, names in ((lookup, ("get_recording_details", "get_release_tracklist", "get_release_metadata",
                                       "search_album", "search_recording", "urlopen")),
                              (audio, ("identify_audio", "urlopen"))):
            for name in names:
                self.stack.enter_context(patch.object(module, name, side_effect=AssertionError("Unexpected provider")))
        self.stack.enter_context(patch("builtins.input", side_effect=AssertionError("Unexpected prompt")))

    def fixture(self, *, managed=False, zipped=False):
        if managed:
            if zipped:
                source = self.inbox / "Delivery.zip"
                with zipfile.ZipFile(source, "w") as stream:
                    stream.writestr("01.flac", b"original audio")
                    stream.writestr("cover.png", b"original cover")
            else:
                source = self.inbox / "Delivery"
                source.mkdir()
                (source / "01.flac").write_bytes(b"original audio")
                (source / "cover.png").write_bytes(b"original cover")
            with redirect_stdout(StringIO()):
                staged = sort.sort_inbox()
            stage = Path(staged[0])
        else:
            stage = self.stage / "Import"
            stage.mkdir()
            (stage / "01.flac").write_bytes(b"original audio")
            (stage / "cover.png").write_bytes(b"original cover")
            source = self.inbox / "Import"
            shutil.copytree(stage, source)
        self.name, self.payload = stage.name, stage
        self.issue = {"import_name": stage.name, "album": "Album",
                      "tracks": [f"{stage.name}/01.flac"]}
        self.album = {"album": "Album", "folder": stage,
                      "tracks": [metadata_record(stage / "01.flac")]}
        destination = self.library / "Artist" / "Album"
        destination.mkdir(parents=True)
        self.song, self.cover = destination / "01.flac", destination / "cover.png"
        self.song.write_bytes(b"finished audio")
        self.cover.write_bytes(b"finished cover")
        self.files = {str(path): sort.file_checksum(path).hex() for path in (self.song, self.cover)}
        self.bulk = self.backup / "OriginalDownloads-Bulk.zip"
        self.snapshot = self.backup / "MusicLibrary-fixture.zip"
        self.write_zip(self.inbox, self.bulk)
        self.write_zip(self.library, self.snapshot)
        self.stack.enter_context(patch.object(tag, "group_staged_albums", return_value={(self.name, "album"): self.album}))
        self.stack.enter_context(patch.object(tag, "read_track_metadata", side_effect=lambda path: metadata_record(
            Path(path), album=("Album",))))

    def write_zip(self, root, target):
        with zipfile.ZipFile(target, "w") as stream:
            for relative in sort.file_manifest(root):
                stream.write(root / relative, relative)

    def receipt(self):
        return mixie.CommitReceipt(self.files, input_map={self.issue["tracks"][0]: {
            "source_sha256": sort.file_checksum(self.payload / "01.flac").hex(),
            "destination": str(self.song), "destination_sha256": self.files[str(self.song)]}},
            source_manifest=mixie.completion_source_manifest(self.issue),
            asset_outputs={str(self.cover): self.files[str(self.cover)]},
            evidence={"provenance": "independently_verified"})

    def record(self):
        return mixie.record_completed_review(self.issue, self.receipt(), self.bulk, self.snapshot)

    def legacy(self):
        identity = {"import_name": self.name, "tracks": [[self.issue["tracks"][0],
                    sort.file_checksum(self.payload / "01.flac").hex()]]}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {key: {"album": "Album", "receipt": self.files,
                      "originals_archive": str(self.bulk), "library_archive": str(self.snapshot)}}

    def quarantine(self, *, archived=False):
        self.record()
        receipt = self.receipt()
        if archived:
            (self.inbox / "Delivery.zip").unlink()
            operation = sort.quarantine_archived_cleanup
        else:
            operation = sort.quarantine_inbox_cleanup
        with redirect_stdout(StringIO()):
            moved = operation({(self.name, "album"): receipt}, self.bulk, self.snapshot, approved=True)
        self.assertEqual([self.name], moved)
        folders = list((self.backup / ".cleanup-quarantine").iterdir())
        self.assertEqual(1, len(folders))
        return folders[0]

    def test_legacy_completion_verifies_archive_contents_not_existence(self):
        self.fixture()
        ledger = self.legacy()
        self.assertTrue(mixie.is_review_complete(self.issue, ledger))
        with zipfile.ZipFile(self.snapshot, "w") as stream:
            stream.writestr("unrelated", b"not the receipt")
        self.assertFalse(mixie.is_review_complete(self.issue, ledger))

    def test_original_archive_corruption_invalidates_legacy_completion(self):
        self.fixture()
        ledger = self.legacy()
        with zipfile.ZipFile(self.bulk, "w") as stream:
            stream.writestr("unrelated", b"not the source")
        self.assertFalse(mixie.is_review_complete(self.issue, ledger))

    def test_asset_change_invalidates_source_fingerprint_and_approval(self):
        self.fixture()
        before = mixie.review_fingerprint(self.issue)
        (self.payload / "cover.png").write_bytes(b"changed artwork")
        self.assertNotEqual(before, mixie.review_fingerprint(self.issue))

    def test_managed_fingerprint_binds_transaction_occurrence(self):
        self.fixture(managed=True)
        before = mixie.review_fingerprint(self.issue)
        provenance = sort.staging_provenance(self.name)
        assert provenance is not None
        provenance["transaction_id"] = "a" * 32
        with patch.object(sort, "staging_provenance", return_value=provenance):
            self.assertNotEqual(before, mixie.review_fingerprint(self.issue))

    def test_record_rejects_plain_receipt_without_source_accounting(self):
        self.fixture()
        with self.assertRaisesRegex((ValueError, RuntimeError), "input|source|receipt"):
            mixie.record_completed_review(self.issue, dict(self.files), self.bulk, self.snapshot)

    def test_v2_receipt_is_legacy_dict_compatible_and_persisted(self):
        self.fixture()
        receipt = self.receipt()
        self.assertIsInstance(receipt, dict)
        self.assertEqual(self.files, dict(receipt))
        identity = mixie.record_completed_review(self.issue, receipt, self.bulk, self.snapshot)
        record = mixie.load_completed_reviews()[identity]
        self.assertEqual(2, record["evidence_version"])
        self.assertEqual(receipt.input_map, record["input_map"])
        self.assertEqual(receipt.source_manifest, record["source_manifest"])
        self.assertEqual(receipt.asset_outputs, record["asset_outputs"])
        self.assertTrue(mixie.is_review_complete(self.issue, {identity: record}))

    def test_unrelated_valid_output_count_is_not_input_proof(self):
        self.fixture()
        receipt = self.receipt()
        receipt.input_map = {}
        with self.assertRaisesRegex((ValueError, RuntimeError), "input|source|mapping"):
            mixie.record_completed_review(self.issue, receipt, self.bulk, self.snapshot)

    def test_wrong_input_hash_and_destination_are_refused(self):
        self.fixture()
        for field, value in (("source_sha256", "0" * 64), ("destination", str(self.cover))):
            with self.subTest(field=field):
                receipt = self.receipt()
                receipt.input_map[self.issue["tracks"][0]][field] = value
                with self.assertRaises((ValueError, RuntimeError)):
                    mixie.record_completed_review(self.issue, receipt, self.bulk, self.snapshot)

    def test_record_binds_full_source_payload_not_audio_subset(self):
        self.fixture()
        receipt = self.receipt()
        (self.payload / "extra.pdf").write_bytes(b"unaccounted source change")
        with self.assertRaises((ValueError, RuntimeError)):
            mixie.record_completed_review(self.issue, receipt, self.bulk, self.snapshot)

    def test_archive_growth_does_not_invalidate_prior_completion(self):
        self.fixture()
        self.record()
        with zipfile.ZipFile(self.bulk, "a") as stream:
            stream.writestr("later-download.zip", b"new independent download")
        extra = self.library / "Another" / "02.flac"
        extra.parent.mkdir()
        extra.write_bytes(b"later library file")
        self.assertTrue(mixie.is_review_complete(self.issue, mixie.load_completed_reviews()))

    def test_same_directory_archive_growth_preserves_prior_input_proof(self):
        self.fixture()
        self.record()
        with zipfile.ZipFile(self.bulk, "a") as stream:
            stream.writestr("Import/new-unrelated-file.txt", b"later delivery data")
        self.assertTrue(mixie.is_review_complete(self.issue, mixie.load_completed_reviews()))

    def test_changed_v2_input_map_cannot_hide_pending_review(self):
        self.fixture()
        identity = self.record()
        ledger = mixie.load_completed_reviews()
        ledger[identity]["input_map"].clear()
        with patch.object(mixie, "load_completed_reviews", return_value=ledger), \
             patch.object(mixie, "load_review_queue", return_value=[]):
            self.assertEqual([self.issue], mixie.reconcile_review_queue([deepcopy(self.issue)]))

    def test_read_and_write_state_refuse_symlinks(self):
        for filename, load, save in ((".needs_review.json", mixie.load_review_queue, lambda: mixie.save_review_queue([])),
                                     (".completed_reviews.json", mixie.load_completed_reviews, None)):
            with self.subTest(filename=filename):
                victim = self.root / "victim.json"
                victim.write_text('{"version":1,"issues":[],"entries":{}}')
                link = self.script / filename
                link.symlink_to(victim)
                with self.assertRaises((ValueError, RuntimeError, PermissionError)):
                    load()
                if save:
                    with self.assertRaises((ValueError, RuntimeError, PermissionError)):
                        save()
                link.unlink()
                self.assertEqual('{"version":1,"issues":[],"entries":{}}', victim.read_text())

    def test_reconcile_keeps_multiple_choices_and_draft_across_new_conflict(self):
        self.fixture()
        fingerprint = mixie.review_fingerprint(self.issue)
        prior = {**self.issue, "source_fingerprint": fingerprint,
                 "interpretation": {"status": "draft", "marker": "saved"},
                 "decisions": [{"field": "album", "value": "Chosen", "status": "pending_application"}],
                 "details": {"kind": "identity_release"}}
        fresh = {**self.issue, "details": {"kind": "metadata_conflict"}}
        with patch.object(mixie, "load_review_queue", return_value=[prior]), \
             patch.object(mixie, "load_completed_reviews", return_value={}):
            result = mixie.reconcile_review_queue([fresh])[0]
        self.assertEqual(prior["interpretation"], result["interpretation"])
        self.assertEqual(prior["decisions"], result["decisions"])

    def test_reconcile_removes_stale_saved_approvals_even_when_no_fresh_issue(self):
        self.fixture()
        prior = {**self.issue, "source_fingerprint": mixie.review_fingerprint(self.issue),
                 "interpretation": {"status": "pending_application"}, "decisions": [{"field": "album"}]}
        (self.payload / "cover.png").write_bytes(b"changed art")
        with patch.object(mixie, "load_review_queue", return_value=[prior]), \
             patch.object(mixie, "load_completed_reviews", return_value={}):
            result = mixie.reconcile_review_queue([])[0]
        self.assertNotIn("interpretation", result)
        self.assertNotIn("decisions", result)

    def test_managed_inbox_v2_completion_survives_quarantine(self):
        self.fixture(managed=True)
        folder = self.quarantine()
        with redirect_stdout(StringIO()):
            ready = mixie.verify_inbox_purge_candidates()
        self.assertEqual(self.name, ready[0]["name"])
        self.assertTrue((folder / "staging").is_dir())

    def test_managed_archive_exact_source_name_is_not_import_name_zip(self):
        self.fixture(managed=True, zipped=True)
        folder = self.quarantine(archived=True)
        self.assertNotEqual("Delivery.zip", self.name + ".zip")
        with redirect_stdout(StringIO()):
            ready = mixie.verify_archived_purge_candidates()
        self.assertEqual(self.name, ready[0]["name"])
        self.assertTrue((folder / "staging").is_dir())

    def test_tampered_v2_ledger_blocks_fresh_purge_before_deletion(self):
        self.fixture(managed=True)
        folder = self.quarantine()
        ledger_file = self.script / ".completed_reviews.json"
        data = json.loads(ledger_file.read_text())
        next(iter(data["entries"].values()))["input_map"].clear()
        ledger_file.write_text(json.dumps(data))
        with self.assertRaises((ValueError, RuntimeError)):
            mixie.purge_verified_inbox_quarantine(approved=True)
        self.assertTrue((folder / "staging" / "01.flac").is_file())
        self.assertFalse((folder / "purge-state.json").exists())

    def test_fresh_v2_purge_uses_validated_input_inventory_not_unrepaired_album_tags(self):
        for archived in (False, True):
            for original_albums in ((), ("Wrong album", "Other album")):
                with self.subTest(archived=archived, original_albums=original_albums):
                    case = CompletionTests("runTest")
                    case.setUp()
                    try:
                        case.fixture(managed=True, zipped=archived)
                        folder = case.quarantine(archived=archived)
                        operation = mixie.verify_archived_purge_candidates if archived else mixie.verify_inbox_purge_candidates
                        with patch.object(tag, "read_track_metadata", return_value=metadata_record(
                                folder / "staging" / "01.flac", album=original_albums)) as read, redirect_stdout(StringIO()):
                            ready = operation()
                        case.assertEqual([case.name], [item["name"] for item in ready])
                        read.assert_not_called()
                        case.assertTrue((folder / "staging" / "01.flac").is_file())
                    finally:
                        self.assertTrue(case.doCleanups())

    def test_malformed_retained_history_cannot_poison_current_v2_purge_authority(self):
        for archived in (False, True):
            for history_key in ("history:" + "a" * 64, "b" * 64):
                with self.subTest(archived=archived, history_key=history_key):
                    case = CompletionTests("runTest")
                    case.setUp()
                    try:
                        case.fixture(managed=True, zipped=archived)
                        folder = case.quarantine(archived=archived)
                        ledger_path = case.script / ".completed_reviews.json"
                        data = json.loads(ledger_path.read_text())
                        authoritative = next(iter(data["entries"].values()))
                        data["entries"][history_key] = {"source_manifest": authoritative["source_manifest"],
                                                       "evidence_version": "bad history", "tracks": None}
                        ledger_path.write_text(json.dumps(data))
                        operation = mixie.verify_archived_purge_candidates if archived else mixie.verify_inbox_purge_candidates
                        with redirect_stdout(StringIO()):
                            case.assertEqual([case.name], [row["name"] for row in operation()])
                        case.assertEqual(data, json.loads(ledger_path.read_text()))
                        case.assertTrue((folder / "staging").is_dir())
                    finally:
                        self.assertTrue(case.doCleanups())

    def test_malformed_current_v2_inventory_still_blocks_purge_despite_retained_history(self):
        self.fixture(managed=True)
        folder = self.quarantine()
        ledger_path = self.script / ".completed_reviews.json"
        data = json.loads(ledger_path.read_text())
        key, current = next(iter(data["entries"].items()))
        data["entries"]["history:" + "a" * 64] = deepcopy(current)
        data["entries"][key]["tracks"] = None
        ledger_path.write_text(json.dumps(data))
        with self.assertRaises((ValueError, RuntimeError)), redirect_stdout(StringIO()):
            mixie.verify_inbox_purge_candidates()
        self.assertTrue((folder / "staging" / "01.flac").is_file())
        self.assertFalse((folder / "purge-state.json").exists())

    def test_both_resumed_purge_routes_revalidate_provenance(self):
        for archived in (False, True):
            with self.subTest(archived=archived):
                case = CompletionTests("runTest")
                case.setUp()
                try:
                    case.fixture(managed=True, zipped=archived)
                    folder = case.quarantine(archived=archived)
                    journal = (folder / "recovery.json").read_bytes()
                    ledger = (case.script / ".completed_reviews.json").read_bytes()
                    (folder / "purge-state.json").write_text(json.dumps({
                        "version": 1, "journal_sha256": hashlib.sha256(journal).hexdigest(),
                        "ledger_sha256": hashlib.sha256(ledger).hexdigest()}))
                    operation = mixie.purge_verified_archived_quarantine if archived else mixie.purge_verified_inbox_quarantine
                    with patch.object(sort, "validate_cleanup_provenance", side_effect=RuntimeError("provenance changed")) as validate:
                        with case.assertRaisesRegex(RuntimeError, "provenance changed"):
                            operation(approved=True)
                        validate.assert_called()
                    case.assertTrue((folder / "staging" / "01.flac").is_file())
                finally:
                    case.doCleanups()

    def test_interpretation_menu_passes_fresh_hash_and_checkpoint_callback(self):
        self.fixture()
        issue = {**self.issue, "reason": "Identity unresolved", "details": {"kind": "identity_release"}}
        def choose(selected, fingerprint, save=None):
            self.assertEqual(mixie.review_fingerprint(issue), fingerprint)
            selected["source_fingerprint"] = fingerprint
            selected["interpretation"] = {"status": "draft", "marker": "checkpointed"}
            assert callable(save)
            save(selected)
            return False
        with patch.object(review, "choose_interpretation", side_effect=choose) as chooser, \
             patch.object(mixie, "save_review_queue") as saved, \
             patch("builtins.input", side_effect=["1", "2", "0"]), redirect_stdout(StringIO()):
            mixie.review_queue_menu([issue])
        # Review v2 never auto-enters interpretation; the user explicitly
        # selects it from the album checklist/menu before any processing.
        chooser.assert_called_once()
        self.assertEqual("checkpointed", issue["interpretation"]["marker"])
        saved.assert_called()

    def test_multiple_metadata_choices_are_retained_with_source_binding(self):
        self.fixture()
        issue = {**self.issue, "details": {"kind": "metadata_conflict", "field": "album",
                 "existing_values": ["Album"], "reference_value": "Chosen album", "path": str(self.payload / "01.flac")}}
        with patch("builtins.input", return_value="2"), redirect_stdout(StringIO()):
            self.assertTrue(mixie.choose_metadata_resolution(issue))
            issue["details"].update(field="album_artist", existing_values=["Artist"], reference_value="Chosen credit")
            self.assertTrue(mixie.choose_metadata_resolution(issue))
        self.assertEqual(["album", "album_artist"], [item["field"] for item in issue["decisions"]])
        self.assertTrue(all(item["source_fingerprint"] == mixie.review_fingerprint(issue) for item in issue["decisions"]))

    def test_source_change_during_date_or_art_approval_cannot_bind_new_bytes(self):
        for artwork in (False, True):
            with self.subTest(artwork=artwork):
                case = CompletionTests("runTest")
                case.setUp()
                try:
                    case.fixture()
                    issue = {**case.issue, "details": {"kind": "required_metadata", "fields": ["date"]}}
                    if artwork:
                        issue["details"] = {"kind": "artwork_conflict", "release_ids": [], "candidates": [
                            {"path": str(case.payload / "cover.png"), "sha256": sort.file_checksum(case.payload / "cover.png").hex()}]}
                    def respond(unused, case=case, artwork=artwork):
                        (case.payload / "01.flac").write_bytes(b"changed audio while choosing")
                        return "1" if artwork else "2021"
                    choose = mixie.choose_artwork_resolution if artwork else mixie.choose_required_metadata_resolution
                    with patch("builtins.input", side_effect=respond), redirect_stdout(StringIO()):
                        with case.assertRaises((ValueError, RuntimeError)):
                            choose(issue)
                    case.assertNotIn("artwork_decision" if artwork else "manual_fields", issue)
                finally:
                    case.doCleanups()

    def test_new_choice_does_not_rebind_older_approvals_after_source_change(self):
        self.fixture()
        issue = {**self.issue, "source_fingerprint": mixie.review_fingerprint(self.issue),
                 "decisions": [{"field": "album", "status": "pending_application", "value": "Old choice"}],
                 "details": {"kind": "required_metadata", "fields": ["date"]}}
        (self.payload / "cover.png").write_bytes(b"changed artifact")
        with patch("builtins.input", return_value="2021"), redirect_stdout(StringIO()):
            self.assertTrue(mixie.choose_required_metadata_resolution(issue))
        self.assertNotIn("decisions", issue)
        self.assertEqual({"date": "2021"}, issue["manual_fields"])
        self.assertEqual(mixie.review_fingerprint(issue), issue["source_fingerprint"])

    def test_v2_evidence_cannot_be_downgraded_to_legacy_receipt_validation(self):
        self.fixture()
        folder = self.quarantine()
        ledger_path = self.script / ".completed_reviews.json"
        ledger = json.loads(ledger_path.read_text())
        entry = next(iter(ledger["entries"].values()))
        entry.pop("evidence_version")
        entry["input_map"].clear()
        ledger_path.write_text(json.dumps(ledger))
        with self.assertRaises((ValueError, RuntimeError)), redirect_stdout(StringIO()):
            mixie.verify_inbox_purge_candidates()
        self.assertTrue((folder / "staging" / "01.flac").is_file())

    def test_managed_v2_purges_resume_after_partial_deletion_both_routes(self):
        for archived in (False, True):
            with self.subTest(archived=archived):
                case = CompletionTests("runTest")
                case.setUp()
                try:
                    case.fixture(managed=True, zipped=archived)
                    folder = case.quarantine(archived=archived)
                    snapshot_hash = sort.file_checksum(case.snapshot)
                    target = folder / "staging" / "cover.png"
                    unlink = Path.unlink
                    failed = []
                    def interrupt(path, *args, target=target, failed=failed, unlink=unlink, **kwargs):
                        if path == target and not failed:
                            failed.append(True)
                            raise OSError("synthetic restart boundary")
                        return unlink(path, *args, **kwargs)
                    operation = mixie.purge_verified_archived_quarantine if archived else mixie.purge_verified_inbox_quarantine
                    with patch.object(Path, "unlink", interrupt), redirect_stdout(StringIO()):
                        with case.assertRaisesRegex(OSError, "restart boundary"):
                            operation(approved=True)
                    case.assertTrue((folder / "purge-state.json").is_file())
                    with redirect_stdout(StringIO()):
                        case.assertEqual([case.name], operation(approved=True))
                    case.assertFalse(folder.exists())
                    case.assertEqual(snapshot_hash, sort.file_checksum(case.snapshot))
                finally:
                    case.doCleanups()

    def test_report_counts_purge_occurrences_not_only_reused_import_names(self):
        with redirect_stdout(StringIO()) as output:
            mixie._ingest_report(quarantined=["Delivery"], purged=["Delivery", "Delivery"])
        self.assertIn("Purged: 2", output.getvalue())
        self.assertNotIn("Awaiting safe cleanup: -", output.getvalue())

    def test_missing_key_allows_explicitly_authorized_prior_cleanup_but_never_stages(self):
        with patch.object(sys, "argv", ["mixie.py", "--allow-source-cleanup"]), patch("builtins.input", return_value="1"), \
             patch.object(mixie, "get_acoustid_client_key", return_value=None), \
             patch.object(mixie, "finish_prior_cleanup_before_ingest", return_value=["Recovered transaction"]) as recover, \
             patch.object(sort, "sort_inbox", side_effect=AssertionError("no key means no staging")) as stage, \
             redirect_stdout(StringIO()) as output:
            mixie.main()
        recover.assert_called_once_with(allow_source_cleanup=True)
        stage.assert_not_called()
        self.assertIn("Purged: 1", output.getvalue())

    def test_missing_key_does_not_start_automatic_cleanup(self):
        with patch.object(sys, "argv", ["mixie.py"]), patch("builtins.input", return_value="1"), \
             patch.object(mixie, "get_acoustid_client_key", return_value=None), \
             patch.object(mixie, "finish_prior_cleanup_before_ingest", return_value=[]) as recover, \
             patch.object(sort, "sort_inbox", side_effect=AssertionError("no key means no staging")) as stage, \
             redirect_stdout(StringIO()) as output:
            mixie.main()
        recover.assert_not_called()
        stage.assert_not_called()
        self.assertIn("Purged: 0", output.getvalue())

    def test_busy_ingest_lock_blocks_before_staging(self):
        lock = self.script / ".mixie-ingest.lock"
        with lock.open("w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sys, "argv", ["mixie.py"]), patch("builtins.input", return_value="1"), \
                 patch.object(sort, "sort_inbox", side_effect=AssertionError("must not stage")) as stage, redirect_stdout(StringIO()):
                mixie.main()
            stage.assert_not_called()

    def test_repair_and_sync_do_not_acquire_ingest_lock(self):
        for choice in ("2", "3"):
            with self.subTest(choice=choice), patch.object(sys, "argv", ["mixie.py"]), \
                 patch("builtins.input", return_value=choice), patch.object(tag, "main"), \
                 patch.object(mixie, "ingest_session", side_effect=AssertionError("unrelated mode lock")), redirect_stdout(StringIO()):
                mixie.main()


class WorkingPlanTests(WorkspaceCase):
    def album(self):
        path = make_audio(self.stage / "Import" / "01.flac", title="Wrong source title", artist="Artist",
                          album="Album", albumartist="Artist", tracknumber="1", tracktotal="12",
                          discnumber="2", disctotal="3", date="2020", genre="Rock")
        metadata = tag.read_track_metadata(path)
        assert metadata is not None
        return {"album": "Album", "folder": path.parent, "tracks": [metadata]}

    def test_working_identity_and_full_partial_totals_are_verified_without_source_changes(self):
        album = self.album()
        path = album["tracks"][0]["path"]
        before = sort.file_checksum(path)
        plan = {"matched": {("2", "1"): path}, "proposals": [], "artwork": make_cover(),
                "identities": {str(path.resolve()): {"recording_id": "approved-recording", "title": "Chosen title",
                    "artist": "Artist", "verified": False, "user_approved": True}},
                "interpretation": {"status": "pending_application", "user_approved": True, "independently_verified": False,
                    "source_fingerprint": mixie.review_fingerprint(mixie._album_issue(album))},
                "source_fingerprint": mixie.review_fingerprint(mixie._album_issue(album)), "partial": True,
                "provenance": "user_approved", "numbering_totals": {str(path.resolve()): {"track_total": 12, "disc_total": 3}},
                "release_totals": {"album_total": 30, "disc_total": 3, "track_totals": {"1": 10, "2": 12, "3": 8}}}
        with patch.object(archive, "backup_staged_import", return_value=self.backup), \
             patch.object(tag, "group_staged_albums", return_value={("Import", "album"): album}), redirect_stdout(StringIO()):
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertIsInstance(receipt, mixie.CommitReceipt)
        target = Path(receipt.input_map["Import/01.flac"]["destination"])
        saved = tag.read_track_metadata(target)
        assert saved is not None
        self.assertEqual(("Chosen title",), saved["title"])
        self.assertEqual({12}, tag.get_track_totals(saved))
        self.assertEqual({3}, tag.get_disc_totals(saved))
        self.assertEqual(before, sort.file_checksum(path))

    def test_source_change_during_backup_blocks_before_copy(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        plan = {"matched": {("2", "1"): source}, "proposals": [], "artwork": make_cover(),
                "source_fingerprint": mixie.review_fingerprint(mixie._album_issue(album))}
        def changed(unused):
            source.write_bytes(b"changed during backup fixture")
            return self.backup
        with patch.object(archive, "backup_staged_import", side_effect=changed), \
             patch.object(shutil, "copy2", side_effect=AssertionError("must reject changed source first")) as copy, redirect_stdout(StringIO()):
            with self.assertRaises((ValueError, RuntimeError)):
                mixie.test_album_plan(plan, album, commit=True, auto=True)
        copy.assert_not_called()

    def test_corrupted_initial_working_copy_is_not_given_to_tag_writers(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        plan = {"matched": {("2", "1"): source}, "proposals": [], "artwork": make_cover()}
        def corrupt(unused, target):
            Path(target).write_bytes(b"corrupted copy fixture")
        with patch.object(archive, "backup_staged_import", return_value=self.backup), \
             patch.object(shutil, "copy2", side_effect=corrupt), \
             patch.object(tag, "apply_corrections", side_effect=AssertionError("must validate copied bytes first")) as write, redirect_stdout(StringIO()):
            with self.assertRaises(RuntimeError):
                mixie.test_album_plan(plan, album, commit=True, auto=True)
        write.assert_not_called()

    def test_deferred_totals_block_before_backup_or_copy(self):
        album = self.album()
        path = album["tracks"][0]["path"]
        plan = {"matched": {("2", "1"): path}, "proposals": [], "artwork": make_cover(),
                "partial": True, "numbering_deferred": {str(path): ["track_total"]}}
        with patch.object(archive, "backup_staged_import", side_effect=AssertionError("Must defer before backup")):
            with self.assertRaises((ValueError, review.AlbumReviewRequired)):
                mixie.test_album_plan(plan, album, commit=True, auto=True)

    def decision(self, album, **changes):
        source = album["tracks"][0]["path"]
        decision = {"status": "pending_application", "user_approved": True, "independently_verified": False,
                    "source_fingerprint": mixie.review_fingerprint(mixie._album_issue(album)),
                    "release_id": None, "album": "Explicit album", "album_artist": "Explicit credit", "partial": True,
                    "tracks": {source.relative_to(self.stage).as_posix(): {"recording_id": None, "title": "Approved title",
                               "artist": "Artist", "disc": 2, "number": 1}},
                    "totals": {source.relative_to(self.stage).as_posix(): {"track_total": 12, "disc_total": 3}}}
        decision.update(changes)
        return decision

    def test_offline_interpretation_no_provider_identity_promotion_or_count_invention(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        before = sort.file_checksum(source)
        decision = self.decision(album)
        with patch.object(audio, "audit_tracks", side_effect=AssertionError("offline must not identify")), \
             patch.object(mixie, "select_album_art", return_value=make_cover()), \
             patch.object(archive, "backup_staged_import", return_value=self.backup), \
             patch.object(tag, "group_staged_albums", return_value={("Import", "album"): album}), redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, None, interpretation=decision)
            self.assertIsNone(plan["release_totals"])
            self.assertFalse(plan["identities"][str(source)]["verified"])
            self.assertTrue(plan["identities"][str(source)]["user_approved"])
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        target = Path(receipt.input_map["Import/01.flac"]["destination"])
        saved = tag.read_track_metadata(target)
        assert saved is not None
        self.assertEqual(("Explicit album",), saved["album"])
        self.assertEqual(("Explicit credit",), saved["album_artist"])
        self.assertEqual(("Approved title",), saved["title"])
        self.assertEqual({12}, tag.get_track_totals(saved))
        self.assertEqual({3}, tag.get_disc_totals(saved))
        self.assertEqual(before, sort.file_checksum(source))

    def test_offline_approval_without_explicit_totals_is_still_deferred(self):
        album = self.album()
        decision = self.decision(album)
        decision.pop("totals")
        with patch.object(audio, "audit_tracks", side_effect=AssertionError("offline provider")), \
             patch.object(mixie, "select_album_art", side_effect=AssertionError("must defer before artwork")):
            with self.assertRaises(review.AlbumReviewRequired):
                mixie.preview_album(album, None, interpretation=decision)

    def test_single_suffix_requires_corroborated_release_type(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        snapshot = metadata_record(source, album=("Song - Single",), title=("Song",),
                                   track_total=("1",), disc_total=("1",))
        album["tracks"] = [snapshot]
        audits = [{"path": source, "status": "preliminary_match", "reason": "synthetic",
                   "details": {"id": "recording", "title": "Song", "artist-credit-phrase": "Artist",
                               "release-list": [{"id": "release"}]}}]
        tracks = [{"disc": "1", "number": "1", "recording_id": "recording", "title": "Song"}]
        for kind in ("Single", "Album"):
            with self.subTest(kind=kind), patch.object(audio, "audit_tracks", return_value=audits), \
                 patch.object(lookup, "get_release_tracklist", return_value=tracks), \
                 patch.object(lookup, "common_release_metadata", return_value={"album": "Song", "album_artist": "Artist", "date": "2020"}), \
                 patch.object(lookup, "get_release_metadata", return_value={"release_type": kind}) as metadata, \
                 patch.object(tag, "read_track_metadata", return_value=snapshot), \
                 patch.object(tag, "propose_release_numbering", return_value=[]), \
                 patch.object(mixie, "select_album_art", return_value=make_cover()), redirect_stdout(StringIO()):
                if kind == "Single":
                    self.assertEqual(1, len(mixie.preview_album(album, "synthetic")["matched"]))
                else:
                    with self.assertRaises(tag.MetadataConflictError):
                        mixie.preview_album(album, "synthetic")
                metadata.assert_called_once_with("release")

    def test_complete_audio_mapping_reaches_numbering_conflict_review(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        snapshot = metadata_record(source, track_number=("9",), track_total=("9",))
        album["tracks"] = [snapshot]
        audits = [{"path": source, "status": "preliminary_match", "reason": "synthetic",
                   "details": {"id": "recording", "title": "Song", "artist-credit-phrase": "Artist",
                               "release-list": [{"id": "release"}]}}]
        with patch.object(audio, "audit_tracks", return_value=audits), \
             patch.object(lookup, "get_release_tracklist", return_value=[{"disc": "1", "number": "1", "recording_id": "recording", "title": "Song"}]), \
             patch.object(lookup, "common_release_metadata", return_value={"album": "Album", "album_artist": "Artist"}), \
             patch.object(tag, "read_track_metadata", return_value=snapshot), \
             patch.object(mixie, "select_album_art", side_effect=AssertionError("defer numbering first")), redirect_stdout(StringIO()):
            with self.assertRaises(tag.MetadataConflictError) as caught:
                mixie.preview_album(album, "synthetic")
        self.assertEqual("track_number", caught.exception.field)
        self.assertEqual(str(source), caught.exception.path)

    def test_offline_plan_cannot_claim_complete_commercial_coverage(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        plan = {"matched": {("2", "1"): source}, "proposals": [], "artwork": make_cover(),
                "interpretation": self.decision(album), "partial": False, "provenance": "user_approved",
                "numbering_totals": {str(source): {"track_total": 12, "disc_total": 3}}}
        with patch.object(archive, "backup_staged_import", side_effect=AssertionError("no false complete plan")):
            with self.assertRaises(ValueError):
                mixie.test_album_plan(plan, album, commit=True, auto=True)

    def test_unbound_or_mislabelled_interpretation_cannot_reach_backup(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        for changes in ({"source_fingerprint": "0" * 64}, {"independently_verified": True}):
            with self.subTest(changes=changes):
                interpretation = self.decision(album, **changes)
                plan = {"matched": {("2", "1"): source}, "proposals": [], "artwork": make_cover(),
                        "partial": True, "interpretation": interpretation, "provenance": "user_approved",
                        "numbering_totals": {str(source): {"track_total": 12, "disc_total": 3}}}
                with patch.object(archive, "backup_staged_import", side_effect=AssertionError("must refuse approval first")):
                    with self.assertRaises(ValueError):
                        mixie.test_album_plan(plan, album, commit=True, auto=True)
