"""Orchestration regressions; no live music, accounts, or provider calls."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import runpy
import sys
import zipfile
from unittest.mock import patch

import archive
import audio
import lookup
import mixie
import review
import sort
import tag
from support import WorkspaceCase, make_audio, make_cover, metadata_record


class IngestPipelineTests(WorkspaceCase):
    def album(self, title="Album", count=1):
        tracks = [metadata_record(self.track_path(f"Import/{index:02d}.flac"),
                    title=(f"Song {index}",), track_number=(str(index),),
                    track_total=(str(count),), disc_total=("1",))
                  for index in range(1, count + 1)]
        return {"album": title, "tracks": tracks, "folder": tracks[0]["path"].parent}

    def audits(self, album, releases=("release",)):
        return [{"path": track["path"], "status": "preliminary_match", "reason": "fixture",
                 "details": {"id": f"rec-{index}", "title": f"Song {index}",
                     "artist-credit-phrase": "Artist", "length": "150000",
                     "release-list": [{"id": value} for value in releases]}}
                for index, track in enumerate(album["tracks"], 1)]

    def prepare_preview(self, album, *, extra=0, audits=None):
        audits = self.audits(album) if audits is None else audits
        tracks = [{"disc": "1", "number": str(index), "title": f"Song {index}",
                   "recording_id": f"rec-{index}"}
                  for index in range(1, len(album["tracks"]) + extra + 1)]
        self.stack.enter_context(patch.object(audio, "audit_tracks", return_value=audits))
        self.stack.enter_context(patch.object(lookup, "get_release_tracklist", return_value=tracks))
        self.stack.enter_context(patch.object(lookup, "common_release_metadata", return_value={
            "album": album["album"], "album_artist": "Artist", "date": "2020"}))
        for name in ("propose_release_numbering", "propose_common_metadata", "propose_required_album_metadata"):
            self.stack.enter_context(patch.object(tag, name, return_value=[]))
        self.stack.enter_context(patch.object(mixie, "select_album_art", return_value=make_cover()))
        return audits

    def test_partial_release_preserves_missing_track_evidence(self):
        album = self.album("Then: The Earlier Years")
        self.prepare_preview(album, extra=3)
        with self.assertRaises(review.AlbumReviewRequired) as caught:
            mixie.preview_album(album, "fixture")
        self.assertEqual(caught.exception.details["kind"], "identity_release")
        self.assertTrue(caught.exception.details["releases"][0]["missing"])

    def test_unidentified_tracks_have_actionable_evidence(self):
        album = self.album("Tapeworms - Single")
        self.prepare_preview(album, audits=[{"path": album["tracks"][0]["path"],
            "status": "needs_review", "details": None, "reason": "No candidate"}])
        with self.assertRaises(review.AlbumReviewRequired) as caught:
            mixie.preview_album(album, "fixture")
        self.assertEqual(len(caught.exception.details["tracks"]), 1)

    def test_correct_identity_is_in_plan_despite_wrong_source_title(self):
        album = self.album("A Thousand Songs About It All: Act 1")
        album["tracks"][0]["title"] = ("My Only Sense Of Purpose",)
        audits = self.audits(album)
        audits[0]["details"]["title"] = "One Man Circus"
        audits[0]["details"]["artist-credit-phrase"] = "Madilyn Mei"
        album["tracks"][0]["artist"] = ("Madilyn Mei",)
        self.prepare_preview(album, audits=audits)
        plan = mixie.preview_album(album, "fixture")
        identity = plan["identities"][str(album["tracks"][0]["path"].resolve())]
        self.assertEqual(identity["title"], "One Man Circus")
        self.assertIs(identity["verified"], True)

    def test_abba_conflicting_identity_is_not_promoted(self):
        album = self.album("The Singles (The First Fifty Years)")
        row = {"path": album["tracks"][0]["path"], "status": "needs_review",
               "details": {"id": "when", "title": "When You Danced With Me"},
               "reason": "Multiple eligible recordings", "assessment": {
                   "status": "conflicting", "complete": True, "candidates": []}}
        self.prepare_preview(album, audits=[row])
        with self.assertRaises(review.AlbumReviewRequired):
            mixie.preview_album(album, "fixture")

    def test_candidate_release_union_does_not_lose_incomplete_relationships(self):
        album = self.album("Marvin's Marvelous Mechanical Museum", 2)
        rows = self.audits(album)
        rows[1]["details"]["release-list"] = []
        self.prepare_preview(album, audits=rows)
        plan = mixie.preview_album(album, "fixture")
        self.assertEqual(len(plan["matched"]), 2)

    def test_source_change_invalidates_approved_interpretation(self):
        album = self.album()
        issue = {"import_name": "Import", "tracks": [str(t["path"].relative_to(self.stage)) for t in album["tracks"]]}
        old = mixie.review_fingerprint(issue)
        issue["interpretation"] = {"status": "pending_application", "source_fingerprint": old}
        issue["source_fingerprint"] = old
        album["tracks"][0]["path"].write_bytes(b"changed bytes")
        with self.assertRaisesRegex(ValueError, "changed|approval|review"):
            mixie.test_review_decision(issue)

    def test_reconciliation_does_not_hide_missing_archive(self):
        album = self.album()
        path = self.library / "Artist" / "Album" / "01.flac"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"library fixture")
        cover = path.parent / "cover.png"
        cover.write_bytes(b"cover fixture")
        issue = {"import_name": "Import", "album": "Album", "tracks": ["Import/01.flac"]}
        entry = {"receipt": {str(p): sort.file_checksum(p).hex() for p in (path, cover)},
                 "originals_archive": str(self.backup / "missing.zip"),
                 "library_archive": str(self.backup / "missing-library.zip")}
        self.stack.enter_context(patch.object(mixie, "load_review_queue", return_value=[]))
        self.stack.enter_context(patch.object(mixie, "load_completed_reviews", return_value={mixie.review_fingerprint(issue): entry}))
        self.assertEqual(mixie.reconcile_review_queue([issue]), [issue])

    def test_reconciliation_retains_interpretation_when_evidence_changes_but_source_does_not(self):
        album = self.album()
        issue = {"import_name": "Import", "album": "Album", "tracks": ["Import/01.flac"],
                 "details": {"kind": "identity_release"}, "reason": "old"}
        issue["source_fingerprint"] = mixie.review_fingerprint(issue)
        issue["interpretation"] = {"status": "pending_application", "marker": "saved"}
        fresh = {**issue, "details": {"kind": "artwork_conflict"}, "reason": "new"}
        fresh.pop("interpretation")
        self.stack.enter_context(patch.object(mixie, "load_review_queue", return_value=[issue]))
        self.stack.enter_context(patch.object(mixie, "load_completed_reviews", return_value={}))
        result = mixie.reconcile_review_queue([fresh])
        self.assertEqual(result[0]["interpretation"]["marker"], "saved")

    def test_review_and_completed_json_refuse_symlink_targets(self):
        victim = self.root / "unrelated.json"
        victim.write_text("original")
        (self.script / ".needs_review.json").symlink_to(victim)
        with self.assertRaises((ValueError, PermissionError, RuntimeError)):
            mixie.save_review_queue([])
        self.assertEqual(victim.read_text(), "original")

    def test_persistent_key_fallback_is_isolated_and_permission_checked(self):
        key = self.home / ".config" / "mixie" / "acoustid_key"
        key.parent.mkdir(parents=True)
        key.write_text("FAKE-ISOLATED-KEY")
        key.chmod(0o600)
        with patch.dict("os.environ", {"ACOUSTID_CLIENT_KEY": ""}):
            self.assertEqual(mixie.get_acoustid_client_key(), "FAKE-ISOLATED-KEY")
            key.chmod(0o644)
            with self.assertRaises(PermissionError):
                mixie.get_acoustid_client_key()

    def test_guard_rejects_live_data_and_network(self):
        with self.assertRaises(PermissionError):
            Path("/home/deck/MIXIE-Lab/Scrubber/.needs_review.json").read_bytes()
        from urllib.request import urlopen
        with self.assertRaises(PermissionError):
            urlopen("https://example.invalid", timeout=1)

    def test_batch_finishes_before_review_and_reports_all_outcomes(self):
        albums = {(f"Import-{i}", f"Album-{i}"): {"album": f"Album-{i}",
            "tracks": [metadata_record(self.track_path(f"Import-{i}/01.flac"))]}
            for i in range(120)}
        events = []
        def preview(album, *args, **kwargs):
            events.append(album["album"])
            if int(album["album"].split("-")[-1]) % 3:
                raise review.AlbumReviewRequired({"kind": "identity_release", "reason": "fixture",
                    "tracks": [], "releases": []})
            return {}
        def prompt(message):
            if "Select an option" in message:
                return "1"
            self.assertEqual(len(events), 120)
            return "n"
        replacements = [(sort, "sort_inbox", lambda: []),
            (tag, "group_staged_albums", lambda: albums),
            (mixie, "finish_prior_cleanup_before_ingest", lambda *, allow_source_cleanup=False: None),
            (mixie, "load_review_queue", lambda: []),
            (mixie, "load_completed_reviews", lambda: {}),
            (mixie, "preview_album", preview),
            (mixie, "test_album_plan", lambda *args, **kwargs: {"fixture": "digest"}),
            (mixie, "reconcile_review_queue", lambda items: items),
            (mixie, "save_review_queue", lambda items: self.script / "queue.json"),
            (archive, "update_original_downloads_archive", lambda: self.backup / "originals.zip"),
            (archive, "create_verified_zip", lambda *args: self.backup / "library.zip"),
            (mixie, "record_completed_review", lambda *args: None),
            (mixie, "auto_quarantine_archived_completed", lambda *args, allow_source_cleanup=False: []),
            (mixie, "auto_purge_verified_quarantines", lambda *, allow_source_cleanup=False: [])]
        for module, name, value in replacements:
            self.stack.enter_context(patch.object(module, name, value))
        with patch.object(sys, "argv", ["mixie.py"]), patch("builtins.input", side_effect=prompt), redirect_stdout(StringIO()) as output:
            mixie.main()
        self.assertEqual(len(events), 120)
        self.assertIn("Already verified:", output.getvalue())
        self.assertIn("Deferred:", output.getvalue())


class LegacyRegressionTests(WorkspaceCase):
    def test_existing_six_orchestration_cases(self):
        with patch.object(mixie, "finish_prior_cleanup_before_ingest", return_value=None), redirect_stdout(StringIO()):
            runpy.run_path(str(Path.cwd() / "mixie_auto_ingest_integration_test.py"), run_name="__main__")

    def test_existing_purge_recovery_cases(self):
        with redirect_stdout(StringIO()):
            runpy.run_path(str(Path.cwd() / "mixie_inbox_purge_test.py"), run_name="__main__")
