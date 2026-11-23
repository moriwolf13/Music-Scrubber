"""Focused orchestration/UI regressions; synthetic evidence in isolated storage."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from support import WorkspaceCase, metadata_record
import archive
import audio
import lookup
import mixie
import review
import sort
import tag


class QueueWorkflowTests(WorkspaceCase):
    def issue(self, *, kind="provider_unavailable"):
        path = self.track_path("Import/Album/01.flac")
        return {"import_name": "Import", "album": "Album", "tracks": ["Import/Album/01.flac"],
                "reason": "Synthetic blocked verification", "details": {"kind": kind, "retryable": True,
                    "provider_errors": [{"stage": "musicbrainz", "http_status": 503, "retryable": True}]}}

    def queue(self, issue, answers):
        with patch("builtins.input", side_effect=answers), redirect_stdout(StringIO()) as output:
            mixie.review_queue_menu([issue])
        return output.getvalue()

    def test_provider_queue_uses_numbered_retry_recommendation_and_postpone_without_promoting(self):
        issue = self.issue()
        before = deepcopy(issue)
        with patch.object(mixie, "finalize_reviewed_album", side_effect=AssertionError("must not process")), \
             patch.object(review, "choose_interpretation", side_effect=AssertionError("must not prompt manual approval")):
            output = self.queue(issue, ["1", "0"])
        for expected in ("1. Retry online checks", "2. Review song identities", "6. Advanced technical details", "0. Finish reviewing", "not a rejected match"):
            self.assertIn(expected, output)
        self.assertNotIn("[P]rocess", output)
        self.assertNotIn("http_status", output)
        self.assertNotIn("interpretation", issue)
        self.assertEqual(before, issue)

    def test_provider_advanced_details_are_opt_in(self):
        output = self.queue(self.issue(), ["1", "6", "0"])
        self.assertIn('"http_status": 503', output)
        self.assertIn("Advanced aliases", output)

    def test_numbered_retry_processes_without_creating_user_approval(self):
        issue = self.issue()
        with patch.object(mixie, "finalize_reviewed_album", return_value=False) as process:
            self.queue(issue, ["1", "1", "0"])
        process.assert_called_once()
        self.assertNotIn("interpretation", issue)

    def test_provider_interpretation_preserves_real_candidates_without_changing_original_kind(self):
        issue = self.issue()
        path = self.stage / issue["tracks"][0]
        identity = {"id": "synthetic-recording", "title": "Song", "artist-credit-phrase": "Artist", "length": "1000"}
        details = review.issue_details([{"path": path, "status": "needs_review", "reason": "provider failure",
            "details": identity, "existing": metadata_record(path), "evidence": {"recordings": {identity["id"]: identity},
            "candidates": {identity["id"]: 1.0}, "errors": [{"retryable": True}]}}], [], "Synthetic outage")
        issue["details"].update(details, kind="provider_unavailable")

        def choose(working, fingerprint, save=None):
            self.assertEqual("identity_release", working["details"]["kind"])
            self.assertEqual(identity, working["details"]["tracks"][0]["details"])
            working["source_fingerprint"] = fingerprint
            working["interpretation"] = {"status": "draft", "user_approved": False}
            save(working)
            return False

        with patch.object(review, "choose_interpretation", side_effect=choose), \
             patch.object(mixie, "save_review_queue"):
            self.queue(issue, ["1", "2", "0"])
        self.assertEqual("provider_unavailable", issue["details"]["kind"])
        self.assertEqual("draft", issue["interpretation"]["status"])

    def test_metadata_custom_value_can_postpone_without_saving_zero_as_a_value(self):
        issue = self.issue(kind="metadata_conflict")
        issue["details"].update(field="album", existing_values=["Old"], reference_value="New")
        with patch("builtins.input", side_effect=["3", "0"]), redirect_stdout(StringIO()):
            self.assertFalse(mixie.choose_metadata_resolution(issue))
        self.assertNotIn("decision", issue)

    def test_postponed_draft_does_not_block_automatic_retry_or_act_as_approval(self):
        issue = self.issue()
        path = self.stage / issue["tracks"][0]
        issue["source_fingerprint"] = mixie.review_fingerprint(issue)
        issue["interpretation"] = {"status": "draft", "user_approved": False, "tracks": {}}
        original = deepcopy(issue)
        album = {"album": "Album", "folder": path.parent, "tracks": [metadata_record(path)]}
        with patch.object(tag, "group_staged_albums", return_value={("Import", "album"): album}), \
             patch.object(mixie, "get_acoustid_client_key", return_value="DISPOSABLE-OFFLINE-TEST-KEY"), \
             patch.object(mixie, "preview_album", return_value={}) as preview, \
             patch.object(mixie, "test_album_plan", return_value=None), redirect_stdout(StringIO()):
            mixie.test_review_decision(issue)
        self.assertIsNone(preview.call_args.kwargs["interpretation"])
        self.assertEqual("DISPOSABLE-OFFLINE-TEST-KEY", preview.call_args.args[1])
        self.assertEqual(original, issue)

    def test_missing_required_metadata_can_postpone_with_zero_without_saving_it(self):
        issue = self.issue(kind="required_metadata")
        issue["details"]["fields"] = ["genre"]
        with patch("builtins.input", return_value="0"), redirect_stdout(StringIO()):
            self.assertFalse(mixie.choose_required_metadata_resolution(issue))
        self.assertNotIn("manual_fields", issue)


class ProviderPlanTests(WorkspaceCase):
    def fixture(self, competing):
        path = self.track_path("Import/Album/01.flac")
        metadata = metadata_record(path)
        album = {"album": "Album", "folder": path.parent, "tracks": [metadata]}
        details = {"id": "synthetic-recording", "title": "Song", "artist-credit-phrase": "Artist"}
        audits = [{"path": path, "status": "preliminary_match", "reason": "Synthetic match", "details": details}]
        complete = {"status": "complete", "matched": {(1, 1): path}, "missing": [], "review": [], "reasons": [],
            "release_id": "supported", "tracklist": [{"disc": "1", "number": "1", "recording_id": details["id"], "title": "Song"}], "errors": []}
        competing = {"status": "partial", "matched": {}, "missing": [], "review": [path], "reasons": [],
                     "release_id": "competing", "tracklist": [], "errors": [], **competing}
        return album, audits, complete, competing

    def assert_blocked(self, competing, exception):
        album, audits, complete, other = self.fixture(competing)
        with patch.object(audio, "audit_tracks", return_value=audits), \
             patch.object(lookup, "discover_release_candidates", return_value=["supported", "competing"]), \
             patch.object(lookup, "evaluate_release_candidate", side_effect=[complete, other]), \
             patch.object(lookup, "common_release_metadata", side_effect=AssertionError("cannot choose an edition")), \
             redirect_stdout(StringIO()):
            with self.assertRaises(exception) as caught:
                mixie.preview_album(album, "DISPOSABLE-OFFLINE-TEST-KEY")
        self.assertEqual(2, len(caught.exception.details["releases"]))
        self.assertEqual({}, sort.file_manifest(self.library))
        return caught.exception

    def test_503_competing_release_cannot_disappear_when_another_maps_every_file(self):
        failure = {"stage": "release_tracklist", "type": "NetworkError", "http_status": 503, "retryable": True}
        error = self.assert_blocked({"errors": [failure], "retryable": True, "reasons": ["release_lookup_unavailable"]}, mixie.ProviderUnavailable)
        self.assertEqual("provider_unavailable", error.details["kind"])

    def test_malformed_competing_release_is_uncertainty_not_retryable_outage(self):
        failure = {"stage": "release_tracklist", "type": "ValueError", "retryable": False}
        error = self.assert_blocked({"errors": [failure], "reasons": ["release_lookup_unavailable"]}, review.AlbumReviewRequired)
        self.assertEqual("identity_release", error.details["kind"])

    def test_unknown_competing_tracklist_coverage_cannot_eliminate_competitor(self):
        self.assert_blocked({"reasons": ["remote_tracklist_incomplete"]}, review.AlbumReviewRequired)

    def test_album_metadata_outage_retains_prior_audit_evidence(self):
        album, audits, complete, _ = self.fixture({})
        failure = lookup.musicbrainzngs.NetworkError("Synthetic interruption", TimeoutError("synthetic timeout"))
        with patch.object(audio, "audit_tracks", return_value=audits), \
             patch.object(lookup, "discover_release_candidates", return_value=["supported"]), \
             patch.object(lookup, "evaluate_release_candidate", return_value=complete), \
             patch.object(lookup, "common_release_metadata", side_effect=failure), redirect_stdout(StringIO()):
            with self.assertRaises(mixie.ProviderUnavailable) as caught:
                mixie.preview_album(album, "DISPOSABLE-OFFLINE-TEST-KEY")
        self.assertTrue(caught.exception.details["retryable"])
        self.assertEqual(1, len(caught.exception.details["tracks"]))
        self.assertEqual(1, len(caught.exception.details["releases"]))


class AlbumWideMetadataScopeTests(WorkspaceCase):
    """A copied 11-track album can receive one explicit, validated album-wide choice."""

    def setUp(self):
        super().setUp()
        self.paths = [self.track_path(f"Import/Album/{number:02}.flac",
                                      data=f"unaltered-original-{number}".encode())
                      for number in range(1, 12)]
        self.source_bytes = {path: path.read_bytes() for path in self.paths}
        self.metadata = {path: metadata_record(path, album=("<I°_°I>",),
                                               album_artist=("Caravan Palace",))
                         for path in self.paths}
        self.stack.enter_context(patch.object(
            tag, "read_track_metadata", side_effect=lambda path: self.metadata[Path(path).resolve()]))
        self.issue = {
            "import_name": "Import", "album": "<I°_°I>",
            "reason": "Album title disagrees with the matched release.",
            "tracks": [path.relative_to(self.stage).as_posix() for path in self.paths],
            "details": {"kind": "metadata_conflict", "field": "album",
                        "existing_values": ["<I°_°I>"], "reference_value": "<|°_°|>",
                        "path": str(self.paths[0]), "track": self.paths[0].name},
        }

    def choose(self, answers):
        with patch("builtins.input", side_effect=answers), redirect_stdout(StringIO()) as output:
            result = mixie.choose_metadata_resolution(self.issue)
        return result, output.getvalue()

    def assert_unchanged(self):
        self.assertEqual(self.source_bytes, {path: path.read_bytes() for path in self.paths})

    def test_explicit_bulk_existing_value_resolves_all_eleven_without_rewriting_originals(self):
        saved, output = self.choose(["1", "1"])
        self.assertTrue(saved)
        self.assertIn("Source track: 01.flac", output)
        self.assertIn("all 11 tracks", output)
        self.assertNotIn("path", self.issue["decision"])
        matched = {("1", str(i)): path for i, path in enumerate(self.paths, 1)}
        self.assertEqual(tag.propose_common_metadata(matched, {"album": "<|°_°|>"},
                                                     reviewed_decision=self.issue["decisions"]), [])
        effective = mixie._effective_text_decisions(self.issue["decisions"], self.paths)
        self.assertEqual(11, len(effective))
        self.assertTrue(all(row["value"] == "<I°_°I>" for row in effective.values()))
        self.assert_unchanged()

    def test_bulk_choice_can_extend_identical_previously_saved_track_choice(self):
        self.assertTrue(self.choose(["1", "2"])[0])
        first = deepcopy(self.issue["decision"])
        self.issue["details"].update(path=str(self.paths[1]), track=self.paths[1].name)
        self.assertTrue(self.choose(["1", "1"])[0])
        self.assertEqual(2, len(self.issue["decisions"]))
        self.assertEqual(first, self.issue["decisions"][0])
        self.assertNotIn("path", self.issue["decisions"][1])
        matched = {("1", str(i)): path for i, path in enumerate(self.paths, 1)}
        self.assertEqual([], tag.propose_common_metadata(matched, {"album": "<|°_°|>"},
                                                          reviewed_decision=self.issue["decisions"]))
        self.assert_unchanged()

    def test_different_album_values_keep_choice_scoped_to_one_track(self):
        self.metadata[self.paths[-1]]["album"] = ("Different edition",)
        saved, output = self.choose(["1"])
        self.assertTrue(saved)
        self.assertEqual(str(self.paths[0]), self.issue["decision"]["path"])
        self.assertIn("only to the shown track", output)
        matched = {("1", str(i)): path for i, path in enumerate(self.paths, 1)}
        with self.assertRaises(tag.MetadataConflictError):
            tag.propose_common_metadata(matched, {"album": "<|°_°|>"},
                                        reviewed_decision=self.issue["decisions"])
        self.assert_unchanged()

    def test_differing_prior_approval_blocks_silent_album_wide_override(self):
        self.assertTrue(self.choose(["1", "2"])[0])
        before = deepcopy(self.issue["decisions"])
        self.issue["details"].update(path=str(self.paths[1]), track=self.paths[1].name)
        saved, output = self.choose(["2"])
        self.assertTrue(saved)
        self.assertIn("Existing track-specific approvals differ", output)
        self.assertEqual(before[0], self.issue["decisions"][0])
        self.assertEqual(str(self.paths[1]), self.issue["decision"]["path"])
        self.assert_unchanged()

    def test_postponing_bulk_scope_keeps_existing_saved_choice_untouched(self):
        self.assertTrue(self.choose(["1", "2"])[0])
        before = deepcopy(self.issue)
        self.issue["details"].update(path=str(self.paths[1]), track=self.paths[1].name)
        before = deepcopy(self.issue)
        saved, _ = self.choose(["1", "0"])
        self.assertFalse(saved)
        self.assertEqual(before, self.issue)
        self.assert_unchanged()

    def test_bulk_matched_value_explicitly_replaces_all_on_working_copies_only(self):
        saved, _ = self.choose(["2", "1"])
        self.assertTrue(saved)
        self.assertEqual("<|°_°|>", self.issue["decision"]["value"])
        self.assertNotIn("path", self.issue["decision"])
        matched = {("1", str(i)): path for i, path in enumerate(self.paths, 1)}
        self.assertEqual([], tag.propose_common_metadata(
            matched, {"album": "<|°_°|>"}, reviewed_decision=self.issue["decisions"]))
        effective = mixie._effective_text_decisions(self.issue["decisions"], self.paths)
        self.assertEqual({"<|°_°|>"}, {decision["value"] for decision in effective.values()})
        self.assert_unchanged()

    def test_review_queue_shows_saved_track_scope_clearly(self):
        self.assertTrue(self.choose(["1", "2"])[0])
        with patch("builtins.input", side_effect=["1", "0"]), \
             redirect_stdout(StringIO()) as output:
            mixie.review_queue_menu([self.issue])
        self.assertIn("Saved metadata choice (01.flac only)", output.getvalue())
        self.assertIn("album =", output.getvalue())
        self.assert_unchanged()

    def test_album_artist_uses_same_uniform_safety_gate(self):
        self.issue["details"].update(field="album_artist", existing_values=["Caravan Palace"],
                                     reference_value="Caravan Palace feat. Guest")
        saved, output = self.choose(["1", "1"])
        self.assertTrue(saved)
        self.assertIn("all 11 tracks", output)
        matched = {("1", str(i)): path for i, path in enumerate(self.paths, 1)}
        self.assertEqual([], tag.propose_common_metadata(
            matched, {"album_artist": "Caravan Palace feat. Guest"},
            reviewed_decision=self.issue["decisions"]))
        self.assert_unchanged()

    def test_source_mutation_during_scope_prompt_invalidates_choice(self):
        def reply(prompt):
            if "Select an option" in prompt:
                return "1"
            self.paths[-1].write_bytes(b"changed during approval")
            return "1"
        with patch("builtins.input", side_effect=reply), redirect_stdout(StringIO()):
            with self.assertRaises(ValueError):
                mixie.choose_metadata_resolution(self.issue)
        self.assertNotIn("decisions", self.issue)
        self.assertNotIn("decision", self.issue)
