"""Offline regressions for review suggestions and non-destructive ingestion progress."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import sys
import mixie
import review
import sort
import tag
from support import WorkspaceCase, metadata_record


class SuggestionTests(WorkspaceCase):
    def test_source_to_suggestion_is_visible_but_never_written(self):
        path = self.track_path("Delivery/Songs/01.flac")
        existing = metadata_record(path, title=("Wrong Title",), artist=("Old Artist",),
                                   track_total=(), disc_total=())
        details = {"kind": "identity_release", "releases": [], "tracks": [
            {"path": "Delivery/Songs/01.flac", "existing": existing,
             "assessment": {"status": "verified", "complete": True, "candidates": [
                 {"recording_id": "rec", "status": "verified", "details": {
                     "id": "rec", "title": "Right Title", "artist-credit-phrase": "New Artist"}}]}}]}
        issue = {"import_name": "Delivery", "album": "Songs",
                 "tracks": ["Delivery/Songs/01.flac"], "details": details}
        before = path.read_bytes()
        output = StringIO()
        with patch.object(mixie, "_offline_album_review_rows", return_value=[existing]), redirect_stdout(output):
            mixie._show_album_review_worklist(issue)
        self.assertIn("Title: Wrong Title -> Right Title", output.getvalue())
        self.assertIn("Artist: Old Artist -> New Artist", output.getvalue())
        self.assertEqual(before, path.read_bytes())

    def test_incomplete_or_ambiguous_evidence_never_becomes_a_verified_suggestion(self):
        candidate = lambda n: {"recording_id": n, "status": "verified", "details": {
            "id": n, "title": "Title", "artist-credit-phrase": "Artist"}}
        self.assertIsNone(review.suggested_identity({"assessment": {"status": "verified", "complete": False,
                                              "candidates": [candidate("one")]}}))
        self.assertIsNone(review.suggested_identity({"assessment": {"status": "verified", "complete": True,
                                              "candidates": [candidate("one"), candidate("two")]}}))

    def test_editor_enters_guided_tag_review_without_processing(self):
        path = self.track_path("Delivery/Songs/01.flac")
        issue = {"album": "Songs", "import_name": "Delivery", "tracks": ["Delivery/Songs/01.flac"],
                 "details": {"kind": "required_metadata", "fields": ["genre"]}}
        before = path.read_bytes()
        with patch.object(mixie, "review_fingerprint", return_value="synthetic"), \
             patch.object(mixie, "_offline_album_review_rows", return_value=[metadata_record(path)]), \
             patch.object(mixie, "_choose_album_interpretation", return_value=False) as editor, \
             patch.object(mixie, "finalize_reviewed_album", side_effect=AssertionError("Should not process")), \
             patch("builtins.input", side_effect=["6", "0"]), redirect_stdout(StringIO()):
            mixie._edit_offline_shared_metadata(issue, [issue])
        editor.assert_called_once()
        self.assertEqual(before, path.read_bytes())


class BatchTests(WorkspaceCase):
    def _run(self, count, effects):
        albums = {}
        for index in range(count):
            path = self.track_path(f"Delivery/Album {index}/01.flac")
            albums[("Delivery", f"Album {index}")] = {
                "album": f"Album {index}", "folder": path.parent,
                "tracks": [metadata_record(path)]}
        output = StringIO()
        with patch.object(sys, "argv", ["mixie.py"]), \
             patch.object(sort, "sort_inbox", return_value=[]), \
             patch.object(tag, "group_staged_albums", return_value=albums), \
             patch.object(mixie, "preview_album", side_effect=effects), \
             patch.object(mixie, "get_acoustid_client_key", return_value="offline-test"), \
             patch.object(mixie, "finish_prior_cleanup_before_ingest", return_value=[]), \
             patch.object(mixie.archive, "update_original_downloads_archive", return_value=self.backup / "OriginalDownloads-Bulk.zip") as originals, \
             patch.object(mixie.archive, "create_verified_zip", side_effect=AssertionError("Empty library must not be archived")), \
             patch("builtins.input", return_value="n"), redirect_stdout(output):
            mixie._main(selection="1")
            originals.assert_called_once_with()
        queue = json.loads((self.script / ".needs_review.json").read_text())
        return output.getvalue(), queue["issues"]

    def test_review_only_run_is_not_a_false_archive_error(self):
        error = review.AlbumReviewRequired({"kind": "identity_release", "reason": "Need edition"})
        output, issues = self._run(1, error)
        self.assertIn("Errors: 0", output)
        self.assertIn("No finished library files yet", output)
        self.assertEqual(1, len(issues))

    def test_interrupt_keeps_all_prior_albums_in_queue(self):
        error = review.AlbumReviewRequired({"kind": "identity_release", "reason": "Need edition"})
        output, issues = self._run(2, [error, KeyboardInterrupt()])
        self.assertIn("Errors: 0", output)
        self.assertIn("Ingestion interrupted", output)
        self.assertEqual(2, len(issues))
        self.assertEqual("interrupted", issues[1]["details"]["kind"])


class OriginalArchiveBoundaryTests(WorkspaceCase):
    def run_review_only(self, archive_error):
        path = self.track_path("Delivery/Album/01.flac")
        albums = {("Delivery", "Album"): {"album": "Album", "folder": path.parent,
                                        "tracks": [metadata_record(path)]}}
        output = StringIO()
        with patch.object(sys, "argv", ["mixie.py"]), \
             patch.object(sort, "sort_inbox", return_value=[]), \
             patch.object(tag, "group_staged_albums", return_value=albums), \
             patch.object(mixie, "preview_album", side_effect=review.AlbumReviewRequired({"kind":"identity_release"})), \
             patch.object(mixie, "get_acoustid_client_key", return_value="offline-test"), \
             patch.object(mixie, "finish_prior_cleanup_before_ingest", return_value=[]), \
             patch.object(mixie.archive, "update_original_downloads_archive", side_effect=archive_error) as originals, \
             patch.object(mixie.archive, "create_verified_zip", side_effect=AssertionError("No finished music")) as library, \
             patch("builtins.input", return_value="n"), redirect_stdout(output):
            mixie._main(selection="1")
            originals.assert_called_once()
            library.assert_not_called()
        return output.getvalue()

    def test_no_original_downloads_is_only_benign_empty_archive_case(self):
        output = self.run_review_only(ValueError("No downloads to archive."))
        self.assertIn("Errors: 0", output)
        self.assertIn("Deferred: 1", output)

    def test_original_archival_failure_is_never_hidden(self):
        output = self.run_review_only(ValueError("Original download changed: Album.zip"))
        self.assertIn("Errors: 1", output)
        self.assertIn("Original download changed", output)
