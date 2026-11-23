"""Manual-review stabilization, disposable sources/state only; use run_isolated.py."""
import hashlib
import json
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import audio
import lookup
import mixie
import mixie_review_ui as ui
import review
import sort
import tag
from support import WorkspaceCase, make_audio, make_cover, metadata_record


class ManualReviewStabilizationTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.rows = {}
        self.originals = {}
        self.stack.enter_context(patch.object(tag, "read_track_metadata",
            side_effect=lambda path: deepcopy(self.rows[Path(path)])))
        for module, name in ((audio, "identify_audio"), (lookup, "_musicbrainz_call")):
            self.stack.enter_context(patch.object(module, name,
                side_effect=AssertionError("Review must not identify or contact providers")))
        self.addCleanup(self.assert_sources_unchanged)

    def assert_sources_unchanged(self):
        for path, original in self.originals.items():
            self.assertEqual(original, path.read_bytes())

    def issue(self, sizes=(2,), *, certificate=True, genre=()):
        relatives, tracks, positions, matched, remote = [], [], {}, {}, []
        for disc, size in enumerate(sizes, 1):
            for number in range(1, size + 1):
                relative = f"Delivery/{disc}-{number}.flac"
                path = self.track_path(relative, f"synthetic {disc}-{number}".encode())
                self.originals[path] = path.read_bytes()
                self.rows[path] = metadata_record(path, title=(f"Source {disc}-{number}",),
                    album=(), album_artist=(), artist=("Act",), date=(), genre=genre,
                    track_number=(), disc_number=(), track_total=(), disc_total=())
                identifier = f"recording-{disc}-{number}"
                candidates = [{"recording_id": candidate, "status": "verified", "score": .99,
                    "details": {"id": candidate, "title": f"Chosen {disc}-{number}",
                                "artist-credit-phrase": "Act", "length": 200000}}
                    for candidate in (identifier, identifier + "-alternate")]
                tracks.append({"path": relative, "existing": deepcopy(self.rows[path]),
                    "assessment": {"status": "conflicting", "complete": True, "errors": [],
                                   "candidates": candidates}})
                positions[relative] = [disc, number]
                matched[json.dumps([disc, number])] = relative
                remote.append({"disc": str(disc), "number": str(number),
                    "recording_id": identifier, "title": f"Chosen {disc}-{number}"})
                relatives.append(relative)
        issue = {"album": "Queued album", "import_name": "Delivery", "tracks": relatives,
            "reason": "Artist credit needs a human decision", "details": {
                "kind": "metadata_conflict", "field": "artist", "existing_values": ["Act"],
                "reference_value": "Act feat. Guest", "tracks": tracks, "releases": [{
                    "release_id": "release-one", "status": "complete", "tracklist_complete": True,
                    "errors": [], "reasons": [], "missing": [], "review": [],
                    "matched": matched, "tracklist": remote}]}}
        issue["source_fingerprint"] = mixie.review_fingerprint(issue)
        issue["details"]["source_fingerprint"] = issue["source_fingerprint"]
        if certificate:
            issue["details"]["verified_release_numbering"] = {
                "source_fingerprint": issue["source_fingerprint"], "release_id": "release-one",
                "positions": positions, "track_totals": {str(disc): size for disc, size in enumerate(sizes, 1)},
                "album_metadata": {"album": "Known album", "album_artist": "Act", "date": "2024-02-03"}}
        return issue

    def bind(self, issue):
        issue["source_fingerprint"] = mixie.review_fingerprint(issue)
        issue["details"]["source_fingerprint"] = issue["source_fingerprint"]
        proof = issue["details"].get("verified_release_numbering")
        if proof is not None:
            proof["source_fingerprint"] = issue["source_fingerprint"]

    def local_fields(self, issue):
        for number, relative in enumerate(issue["tracks"], 1):
            self.rows[self.stage / relative].update(album=("Local album",), album_artist=("Act",),
                date=("2020",), track_number=(str(number),), track_total=(str(len(issue["tracks"])),))
        self.bind(issue)

    def test_single_disc_screens_and_real_confirmation_have_no_disc_fields_or_warnings(self):
        issue = self.issue(certificate=False)
        issue["details"]["releases"] = []
        self.local_fields(issue)
        before = deepcopy(issue)
        with patch.object(ui, "input", return_value="0"), redirect_stdout(StringIO()) as stream:
            ui._show_album(mixie, issue, [issue])
            ui._show_tracks(mixie, issue, [issue])
            self.assertFalse(ui._approve_review(mixie, issue, [issue]))
        output = stream.getvalue()
        for unnecessary in ("Disc Number:", "Disc Total:", "Disc Layout:", "disc 1,",
                            "discs in album:", "Disc 1:", "Disc structure uncertain"):
            self.assertNotIn(unnecessary, output)
        self.assertIn("Genre: Not set [optional; unresolved", output)
        self.assertNotIn("Genre: Unknown", output)
        self.assertIn("track 1: Source 1-1", output)
        self.assertFalse(any("Disc" in item or "Genre missing" in item
                             for item in ui.preflight_issues(mixie, issue)))
        self.assertEqual(before, issue)

    def test_verified_album_fields_and_numbers_autopopulate_without_becoming_approval(self):
        issue = self.issue()
        proof = issue["details"]["verified_release_numbering"]
        proof["album_metadata"]["genre"] = "Electronic"
        before = deepcopy(issue)
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual(("Known album", "Act", "2024-02-03", "Electronic", "2"),
            tuple(summary[field] for field in ("album", "album_artist", "date", "genre", "track_total")))
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual([1, 2], [track["number"] for track in decision["tracks"].values()])
        self.assertEqual({2}, {value["track_total"] for value in decision["totals"].values()})
        report = "\n".join(ui.preflight_issues(mixie, issue))
        for missing in ("Track Number missing", "Track Total missing", "Release Date missing", "Album title missing"):
            self.assertNotIn(missing, report)
        self.assertIn("Metadata conflict", report)
        self.assertFalse(decision["independently_verified"])
        self.assertEqual(before, issue)

    def test_verified_totals_override_stale_existing_totals_not_explicit_choices(self):
        issue = self.issue()
        for relative in issue["tracks"]:
            self.rows[self.stage / relative]["track_total"] = ("99",)
            self.rows[self.stage / relative]["disc_total"] = ("7",)
        self.bind(issue)
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual("2", summary["track_total"])
        self.assertEqual("1", summary["disc_total"])
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual({2}, {value["track_total"] for value in decision["totals"].values()})
        ui.save_choice(mixie, issue, [issue], field="track_total", value="12")
        self.assertEqual({12}, {value["track_total"] for value in
                               ui._build_user_interpretation(mixie, issue)["totals"].values()})

    def test_verified_album_metadata_beats_untrusted_source_but_not_explicit_edits(self):
        issue = self.issue()
        for relative in issue["tracks"]:
            self.rows[self.stage / relative].update(album=("Wrong source album",),
                album_artist=("Wrong source credit",), date=("1999",))
        self.bind(issue)
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual("Known album", summary["album"])
        self.assertEqual("Act", summary["album_artist"])
        self.assertEqual("2024-02-03", summary["date"])
        ui.save_choice(mixie, issue, [issue], field="date", value="2022")
        ui.save_choice(mixie, issue, [issue], field="album", value="My chosen album")
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual("2022", decision["date"])
        self.assertEqual("My chosen album", decision["album"])

    def test_multidisc_unequal_totals_are_visible_and_used_by_real_approval(self):
        issue = self.issue((2, 3))
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual("Disc 1: 2; Disc 2: 3", summary["track_total"])
        with patch.object(ui, "input", return_value="APPROVE"), redirect_stdout(StringIO()) as stream:
            self.assertTrue(ui._approve_review(mixie, issue, [issue]))
        decision = mixie.load_review_queue()[0]["interpretation"]
        for relative, track in decision["tracks"].items():
            self.assertEqual({"track_total": 2 if track["disc"] == 1 else 3, "disc_total": 2},
                             decision["totals"][relative])
        self.assertIn("Disc Layout: Multiple (2 discs)", stream.getvalue())
        self.assertIn("tracks on disc: 3; discs in album: 2", stream.getvalue())
        self.assertFalse(any("Total missing" in item for item in ui.preflight_issues(mixie, issue)))

    def test_fresh_saved_online_snapshot_supplies_review_totals_after_restart(self):
        issue = self.issue((2, 3))
        decision = ui._build_user_interpretation(mixie, issue)
        decision.update(release_id="release-one", release_tracks=deepcopy(issue["details"]["releases"][0]["tracklist"]))
        decision.pop("totals")
        for relative, track in decision["tracks"].items():
            track["recording_id"] = f"recording-{track['disc']}-{track['number']}"
        issue.update(interpretation=decision, review_status="reviewed", details={"kind": "identity_release", "releases": []})
        mixie.save_review_queue([issue])
        restored = mixie.load_review_queue()[0]
        summary = ui._album_summary(mixie, restored, ui._view(mixie, restored))
        self.assertEqual("Disc 1: 2; Disc 2: 3", summary["track_total"])
        self.assertEqual("2", summary["disc_total"])
        self.assertEqual("multiple", summary["disc_layout"])
        self.assertFalse(any("Total missing" in item or "Disc Total needs" in item
                             for item in ui.preflight_issues(mixie, restored)))
        with patch.object(ui, "input", return_value="APPROVE"), \
             patch.object(lookup, "get_release_tracklist", side_effect=AssertionError("No lookup during UI preview")), \
             redirect_stdout(StringIO()):
            self.assertTrue(ui._approve_review(mixie, restored, [restored]))
        self.assertEqual(decision, mixie.load_review_queue()[0]["interpretation"])

    def test_stale_certificate_never_falls_back_to_other_cached_release_evidence(self):
        issue = self.issue()
        issue["details"]["verified_release_numbering"]["source_fingerprint"] = "0" * 64
        before = deepcopy(issue)
        self.assertIsNone(ui._release_disc_structure(issue, ui._view(mixie, issue)))
        self.assertEqual("Not set", ui._album_summary(mixie, issue, ui._view(mixie, issue))["date"])
        with self.assertRaises(ValueError):
            ui._build_user_interpretation(mixie, issue)
        self.assertEqual(before, issue)

    def test_cached_release_mapping_requires_a_current_source_fingerprint(self):
        issue = self.issue(certificate=False)
        mapping = ui._view(mixie, issue)
        self.assertIsNotNone(ui._release_disc_structure(issue, mapping))
        for fingerprint in (None, "0" * 64, "not-a-sha256"):
            with self.subTest(fingerprint=fingerprint):
                issue["source_fingerprint"] = fingerprint
                self.assertIsNone(ui._release_disc_structure(issue, mapping))
        self.assertFalse(any(ui._remote_position_choices(issue, mapping).values()))

    def test_saving_a_field_cannot_rebind_unbound_cached_release_evidence(self):
        issue = self.issue(certificate=False)
        issue.pop("source_fingerprint")
        issue["details"].pop("source_fingerprint")
        mapping = ui._view(mixie, issue)
        self.assertIsNone(ui._release_disc_structure(issue, mapping))
        ui.save_choice(mixie, issue, [issue], field="genre", value="Jazz")
        self.assertEqual(mixie.review_fingerprint(issue), issue["source_fingerprint"])
        self.assertIsNone(ui._release_disc_structure(issue, mapping))
        self.assertFalse(any(ui._remote_position_choices(issue, mapping).values()))

    def test_changed_source_is_rejected_without_erasing_saved_edits(self):
        issue = self.issue()
        ui.save_choice(mixie, issue, [issue], field="title", value="Saved title", relative=issue["tracks"][0])
        before = deepcopy(issue)
        path = self.stage / issue["tracks"][0]
        path.write_bytes(b"different disposable payload")
        self.originals[path] = path.read_bytes()
        self.assertIsNone(ui._release_disc_structure(issue, ui._view(mixie, issue)))
        with self.assertRaisesRegex(ValueError, "changed"):
            ui._build_user_interpretation(mixie, issue)
        with self.assertRaisesRegex(ValueError, "changed"):
            ui.save_choice(mixie, issue, [issue], field="genre", value="Jazz")
        self.assertEqual(before, issue)

    def test_source_change_during_build_cannot_rebind_old_evidence(self):
        issue = self.issue()
        before = deepcopy(issue)
        original = ui._album_summary
        def change_after_summary(service, selected, mapping):
            result = original(service, selected, mapping)
            path = self.stage / issue["tracks"][0]
            path.write_bytes(b"changed during review")
            self.originals[path] = path.read_bytes()
            return result
        with patch.object(ui, "_album_summary", side_effect=change_after_summary):
            with self.assertRaises(ValueError):
                ui._build_user_interpretation(mixie, issue)
        self.assertEqual(before, issue)

    def test_noncontiguous_or_duplicate_remote_positions_are_not_full_totals(self):
        issue = self.issue(certificate=False)
        release = issue["details"]["releases"][0]
        release["tracklist"][1]["number"] = "3"
        self.assertIsNone(ui._release_disc_structure(issue, ui._view(mixie, issue)))
        release["tracklist"][1]["number"] = "1"
        self.assertIsNone(ui._release_disc_structure(issue, ui._view(mixie, issue)))

    def test_absent_and_placeholder_genres_remain_optional_and_unverified(self):
        for genre in ((), ("Unknown",), (" unknown ",), ("Jazz", "Rock")):
            with self.subTest(genre=genre):
                issue = self.issue(genre=genre)
                decision = ui._build_user_interpretation(mixie, issue)
                self.assertNotIn("genre", decision)
                plan = review.prepare_interpretation({"tracks": list(ui._view(mixie, issue).values())},
                    decision, mixie.review_fingerprint(issue))
                self.assertNotIn("genre", plan["manual_fields"])
                self.assertFalse(any("Genre missing" in item for item in ui.preflight_issues(mixie, issue)))

    def test_unknown_is_not_accepted_as_a_new_genre_or_replayed_as_an_approval(self):
        issue = self.issue()
        before = deepcopy(issue)
        with self.assertRaisesRegex(ValueError, "optional"):
            ui.save_choice(mixie, issue, [issue], field="genre", value="Unknown")
        self.assertEqual(before, issue)
        decision = ui._build_user_interpretation(mixie, issue)
        decision["genre"] = "Unknown"
        with self.assertRaisesRegex(ValueError, "unresolved"):
            review.prepare_interpretation({"tracks": list(ui._view(mixie, issue).values())},
                decision, decision["source_fingerprint"])

    def test_optional_genre_can_clear_a_legacy_unknown_approval_without_losing_recordings(self):
        issue = self.issue()
        decision = ui._build_user_interpretation(mixie, issue)
        relative = issue["tracks"][0]
        decision["genre"] = "Unknown"
        decision["tracks"][relative]["recording_id"] = "saved-recording"
        issue.update(interpretation=decision, manual_fields={"genre": "Unknown"}, review_status="reviewed")
        with patch.object(ui, "input", side_effect=["4", "-", "0"]), redirect_stdout(StringIO()) as stream:
            ui._show_album(mixie, issue, [issue])
        restored = mixie.load_review_queue()[0]
        self.assertNotIn("genre", restored["manual_fields"])
        self.assertNotIn("genre", restored["interpretation"])
        self.assertEqual("saved-recording", restored["interpretation"]["tracks"][relative]["recording_id"])
        self.assertEqual("draft", restored["interpretation"]["status"])
        self.assertIn("Genre is optional. Enter -", stream.getvalue())

    def test_unknown_manual_genre_can_be_replaced_with_a_real_choice(self):
        issue = self.issue()
        decision = ui._build_user_interpretation(mixie, issue)
        decision["genre"] = "Unknown"
        issue.update(interpretation=decision, manual_fields={"genre": "Unknown"})
        ui.save_choice(mixie, issue, [issue], field="genre", value="Jazz")
        restored = mixie.load_review_queue()[0]
        self.assertEqual("Jazz", restored["interpretation"]["genre"])
        self.assertEqual("Jazz", restored["manual_fields"]["genre"])
        self.assertFalse(restored["interpretation"]["user_approved"])

    def test_album_evidence_resolves_recordings_without_showing_an_id_chooser(self):
        issue = self.issue()
        with patch.object(ui, "input", return_value="0"), redirect_stdout(StringIO()) as stream:
            ui._show_tracks(mixie, issue, [issue])
        output = stream.getvalue()
        self.assertIn("no recording-ID choice needed", output)
        self.assertNotIn("possible recordings;", output)
        self.assertIn("2. Track Number: 1 [release evidence; used on approval]", output)
        self.assertNotIn("[MISSING]", output)
        self.assertEqual([], ui._attention_paths(mixie, issue, ui._view(mixie, issue)))

    def test_recording_choice_reopens_from_track_screen_and_survives_real_queue_sessions(self):
        issue = self.issue((1,), certificate=False)
        issue["details"]["releases"] = []
        issue["details"]["kind"] = "identity_release"
        with patch.object(ui, "input", side_effect=["r", "0"]), \
             patch.object(review, "input", side_effect=["2", "offline", "track 1", "2", "", "", "1", "1", "0"]), \
             redirect_stdout(StringIO()) as stream:
            ui._show_tracks(mixie, issue, [issue])
        restored = mixie.load_review_queue()[0]
        relative = restored["tracks"][0]
        chosen = restored["interpretation"]["tracks"][relative]
        self.assertEqual("recording-1-1-alternate", chosen["recording_id"])
        self.assertEqual("draft", restored["interpretation"]["status"])
        self.assertIn("Opening recording choices", stream.getvalue())
        with patch.object(ui, "input", side_effect=["1", "r"]) as navigation, \
             patch.object(review, "input", side_effect=["3", "Saved album", "Act", "2", "2024", "1",
                                                        "4", "2", "1", "1", "5", "APPROVE"]), \
             redirect_stdout(StringIO()):
            self.assertEqual("list", ui._album_editor(mixie, restored, [restored]))
        self.assertEqual(2, navigation.call_count)
        saved = mixie.load_review_queue()[0]
        self.assertTrue(ui.reviewed(saved))
        self.assertEqual(chosen, saved["interpretation"]["tracks"][relative])
        self.assertFalse(saved["interpretation"]["independently_verified"])

    def test_later_tag_edit_preserves_recording_choice_as_a_resumable_draft(self):
        issue = self.issue()
        decision = ui._build_user_interpretation(mixie, issue)
        relative = issue["tracks"][0]
        decision["tracks"][relative]["recording_id"] = "saved-recording-choice"
        issue.update(interpretation=decision, review_status="reviewed")
        ui.save_choice(mixie, issue, [issue], field="title", value="Revised title", relative=relative)
        restored = mixie.load_review_queue()[0]
        self.assertEqual("saved-recording-choice", restored["interpretation"]["tracks"][relative]["recording_id"])
        self.assertEqual("draft", restored["interpretation"]["status"])
        self.assertFalse(restored["interpretation"]["user_approved"])
        with patch.object(review, "input", return_value="0"), redirect_stdout(StringIO()):
            self.assertFalse(mixie._choose_album_interpretation(restored, [restored]))
        self.assertEqual("Revised title", mixie.load_review_queue()[0]["interpretation"]["tracks"][relative]["title"])
        with patch.object(ui, "input", return_value="0"), redirect_stdout(StringIO()) as stream:
            ui._show_tracks(mixie, restored, [restored])
        self.assertIn("Title: Revised title", stream.getvalue())

    def test_entering_advanced_review_keeps_preexisting_manual_fields_and_track_edits(self):
        issue = self.issue()
        relative = issue["tracks"][0]
        ui.save_choice(mixie, issue, [issue], field="title", value="Saved title", relative=relative)
        ui.save_choice(mixie, issue, [issue], field="date", value="2022-03-04")
        ui.save_choice(mixie, issue, [issue], field="genre", value="Jazz")
        with patch.object(review, "input", return_value="0"), redirect_stdout(StringIO()):
            self.assertFalse(mixie._choose_album_interpretation(issue, [issue]))
        restored = mixie.load_review_queue()[0]["interpretation"]
        self.assertEqual("Saved title", restored["tracks"][relative]["title"])
        self.assertEqual("2022-03-04", restored["date"])
        self.assertEqual("Jazz", restored["genre"])
        self.assertFalse(restored["user_approved"])

    def test_advanced_entry_preserves_explicit_offline_totals_without_inferring_file_counts(self):
        issue = self.issue()
        ui.save_choice(mixie, issue, [issue], field="disc_layout", value="single")
        ui.save_choice(mixie, issue, [issue], field="track_total", value="12")
        with patch.object(review, "input", return_value="0"), redirect_stdout(StringIO()):
            self.assertFalse(mixie._choose_album_interpretation(issue, [issue]))
        restored = mixie.load_review_queue()[0]["interpretation"]
        self.assertNotIn("release_id", restored)
        self.assertEqual({12}, {values["track_total"] for values in restored["totals"].values()})
        self.assertEqual({1}, {values["disc_total"] for values in restored["totals"].values()})
        self.assertFalse(restored["user_approved"])

    def test_album_supported_batch_fills_ids_without_erasing_existing_tag_edit_drafts(self):
        issue = self.issue((1,))
        row = issue["details"]["tracks"][0]
        row["assessment"].update(status="verified", candidates=row["assessment"]["candidates"][:1])
        relative = row["path"]
        state = {"release_id": "release-one", "release_tracks": issue["details"]["releases"][0]["tracklist"],
                 "tracks": {relative: {"title": "My saved title", "disc": 1}}}
        with patch.object(review, "input", return_value="APPROVE BATCH"), redirect_stdout(StringIO()):
            self.assertEqual(1, review._batch_supported_tracks(issue, issue["details"], state, None))
        self.assertEqual("recording-1-1", state["tracks"][relative]["recording_id"])
        self.assertEqual("My saved title", state["tracks"][relative]["title"])
        self.assertEqual(1, state["tracks"][relative]["number"])

    def test_draft_can_retain_temporarily_duplicate_numbers_until_explicit_final_validation(self):
        issue = self.issue()
        decision = ui._build_user_interpretation(mixie, issue)
        issue.update(interpretation=decision, review_status="reviewed")
        first, second = issue["tracks"]
        ui.save_choice(mixie, issue, [issue], field="track_number", value="2", relative=first)
        with patch.object(review, "input", return_value="0"), redirect_stdout(StringIO()):
            self.assertFalse(mixie._choose_album_interpretation(issue, [issue]))
        self.assertEqual({2}, {track["number"] for track in issue["interpretation"]["tracks"].values()})
        with self.assertRaisesRegex(ValueError, "unique"):
            review._validate_decision(issue["interpretation"],
                {path: self.stage / path for path in issue["tracks"]}, issue["source_fingerprint"], final=True)
        ui.save_choice(mixie, issue, [issue], field="track_number", value="1", relative=second)
        self.assertEqual([2, 1], [track["number"] for track in issue["interpretation"]["tracks"].values()])

    def test_selected_release_fills_valid_date_but_never_a_placeholder_genre(self):
        issue = self.issue((1,))
        state = {"tracks": {}, "source_fingerprint": issue["source_fingerprint"]}
        remote = issue["details"]["releases"][0]["tracklist"]
        with patch.object(review, "input", return_value="1"), \
             patch.object(lookup, "get_release_tracklist", return_value=remote) as fetch, \
             patch.object(lookup, "get_release_metadata", return_value={"release_id": "release-one",
                 "album": "Online album", "album_artist": "Act", "date": "2024-02-03", "genre": "Unknown"}), \
             redirect_stdout(StringIO()):
            review._review_release(issue, issue["details"], state, None)
        fetch.assert_called_once_with("release-one", refresh=True)
        self.assertEqual("2024-02-03", state["date"])
        self.assertEqual("Online album", state["album"])
        self.assertNotIn("genre", state)
        self.assertEqual(remote, state["release_tracks"])

    def test_known_single_disc_advanced_recording_selection_does_not_prompt_for_disc(self):
        issue = self.issue((1,))
        row = issue["details"]["tracks"][0]
        state = {"tracks": {}, "release_id": "release-one",
                 "release_tracks": issue["details"]["releases"][0]["tracklist"]}
        with patch.object(review, "input", side_effect=["1", "", "", "1"]) as prompts, \
             redirect_stdout(StringIO()) as stream:
            review._review_track(issue, row, state, None)
        self.assertFalse(any("Disc number" in call.args[0] for call in prompts.call_args_list))
        self.assertNotIn("disc 1", stream.getvalue())
        self.assertEqual(1, state["tracks"][row["path"]]["disc"])

    def test_finish_cannot_rebind_a_saved_interpretation_to_changed_source(self):
        issue = self.issue()
        issue["interpretation"] = ui._build_user_interpretation(mixie, issue)
        before = deepcopy(issue)
        with patch.object(mixie, "review_fingerprint", return_value="1" * 64), \
             patch.object(mixie, "save_review_queue") as save, redirect_stdout(StringIO()):
            self.assertFalse(ui._finish(mixie, issue, [issue]))
        save.assert_not_called()
        self.assertEqual(before, issue)

    def test_failed_field_persistence_restores_previous_choices_in_memory_and_on_disk(self):
        issue = self.issue()
        mixie.save_review_queue([issue])
        before = deepcopy(issue)
        persisted = (self.script / ".needs_review.json").read_bytes()
        with patch.object(mixie, "save_review_queue", side_effect=OSError("fixture full disk")):
            with self.assertRaises(OSError):
                ui.save_choice(mixie, issue, [issue], field="genre", value="Jazz")
        self.assertEqual(before, issue)
        self.assertEqual(persisted, (self.script / ".needs_review.json").read_bytes())

    def test_apply_delegates_completion_without_removing_drafts_or_pending_finalization(self):
        issue = self.issue()
        decision = ui._build_user_interpretation(mixie, issue)
        issue.update(interpretation=decision, review_status="reviewed")
        draft = {"album": "Unresolved", "tracks": [], "review_status": "needs_review",
                 "interpretation": {"status": "draft", "user_approved": False}}
        for result in (False, True):
            with self.subTest(parent_result=result):
                issues = [issue, draft]
                before = deepcopy(issues)
                with patch.object(ui, "input", side_effect=["1", "APPLY"]), \
                     patch.object(mixie, "finalize_reviewed_album", return_value=result) as finalize, \
                     redirect_stdout(StringIO()):
                    ui._apply(mixie, issues)
                finalize.assert_called_once_with(issue, issues)
                self.assertEqual(before, issues)

    def test_apply_failure_binds_fresh_provider_evidence_to_the_pre_action_source(self):
        for exception in (review.AlbumReviewRequired, mixie.ProviderUnavailable):
            with self.subTest(exception=exception.__name__):
                issue = self.issue()
                issue.update(interpretation=ui._build_user_interpretation(mixie, issue), review_status="reviewed")
                fingerprint = issue["source_fingerprint"]
                details = deepcopy(issue["details"])
                details.update(kind="identity_release", reason="Fresh lookup needs a decision")
                details.pop("source_fingerprint")
                details.pop("verified_release_numbering")
                error = exception(details)
                original_error = deepcopy(error.details)
                with patch.object(ui, "input", side_effect=["1", "APPLY"]), \
                     patch.object(mixie, "finalize_reviewed_album", side_effect=error), redirect_stdout(StringIO()):
                    ui._apply(mixie, [issue])
                restored = mixie.load_review_queue()[0]
                self.assertEqual(fingerprint, restored["source_fingerprint"])
                self.assertEqual(fingerprint, restored["details"]["source_fingerprint"])
                self.assertIsNotNone(ui._release_disc_structure(restored, ui._view(mixie, restored)))
                self.assertEqual(original_error, error.details)
                self.assertEqual(issue["interpretation"], restored["interpretation"])

    def test_source_change_during_apply_failure_cannot_authorize_rebound_certificate(self):
        issue = self.issue()
        issue.update(interpretation=ui._build_user_interpretation(mixie, issue), review_status="reviewed")
        original = deepcopy(issue)
        details = deepcopy(issue["details"])
        details.update(kind="identity_release", reason="Snapshot predates the source change")
        error = review.AlbumReviewRequired(details)
        incoming = error.details
        assert isinstance(incoming, dict)
        certificate = incoming["verified_release_numbering"]
        assert isinstance(certificate, dict)
        def fail_after_source_change(selected, _issues):
            path = self.stage / selected["tracks"][0]
            path.write_bytes(b"fixture source changed during failed Apply")
            self.originals[path] = path.read_bytes()
            current = mixie.review_fingerprint(selected)
            # Model a downstream failure handler incorrectly stamping old
            # evidence with the post-mutation source. The UI must reject it.
            selected["source_fingerprint"] = current
            selected["details"] = {"kind": "intermediate_failure"}
            incoming["source_fingerprint"] = current
            certificate["source_fingerprint"] = current
            raise error
        with patch.object(ui, "input", side_effect=["1", "APPLY"]), \
             patch.object(mixie, "finalize_reviewed_album", side_effect=fail_after_source_change), \
             redirect_stdout(StringIO()):
            ui._apply(mixie, [issue])
        restored = mixie.load_review_queue()[0]
        self.assertIsNone(restored["details"]["source_fingerprint"])
        self.assertIsNotNone(incoming["source_fingerprint"])
        self.assertIsNone(ui._release_disc_structure(restored, ui._view(mixie, restored)))
        self.assertTrue(any(entry.get("details") == review._json_safe(original["details"]) and
                            entry.get("source_fingerprint") == original["source_fingerprint"]
                            for entry in restored["review_evidence_history"]))
        self.assertEqual(original["interpretation"], restored["interpretation"])
        restored.pop("interpretation")  # Explicitly begin new choices for the changed source.
        ui.save_choice(mixie, restored, [restored], field="genre", value="Jazz")
        self.assertIsNone(ui._release_disc_structure(restored, ui._view(mixie, restored)))

    def test_apply_failure_does_not_refresh_an_already_stale_snapshot_binding(self):
        issue = self.issue()
        issue.update(interpretation=ui._build_user_interpretation(mixie, issue), review_status="reviewed")
        details = deepcopy(issue["details"])
        details.update(kind="identity_release", reason="Old cached evidence", source_fingerprint="0" * 64)
        details.pop("verified_release_numbering")
        error = review.AlbumReviewRequired(details)
        with patch.object(ui, "input", side_effect=["1", "APPLY"]), \
             patch.object(mixie, "finalize_reviewed_album", side_effect=error), redirect_stdout(StringIO()):
            ui._apply(mixie, [issue])
        restored = mixie.load_review_queue()[0]
        self.assertEqual("0" * 64, restored["details"]["source_fingerprint"])
        self.assertIsNone(ui._release_disc_structure(restored, ui._view(mixie, restored)))

    def test_apply_failure_without_a_readable_post_action_source_remains_unbound(self):
        issue = self.issue()
        issue.update(interpretation=ui._build_user_interpretation(mixie, issue), review_status="reviewed")
        details = deepcopy(issue["details"])
        details.update(kind="identity_release", reason="Source became unavailable")
        error = review.AlbumReviewRequired(details)
        with patch.object(ui, "input", side_effect=["1", "APPLY"]), \
             patch.object(mixie, "review_fingerprint", side_effect=[issue["source_fingerprint"], OSError("Unavailable")]), \
             patch.object(mixie, "finalize_reviewed_album", side_effect=error), redirect_stdout(StringIO()):
            ui._apply(mixie, [issue])
        restored = mixie.load_review_queue()[0]
        self.assertIsNone(restored["details"]["source_fingerprint"])
        self.assertIsNone(ui._release_disc_structure(restored, ui._view(mixie, restored)))

    def test_apply_retains_source_bound_numbering_when_a_new_credit_conflict_needs_review(self):
        issue = self.issue()
        issue.update(interpretation=ui._build_user_interpretation(mixie, issue), review_status="reviewed")
        error = tag.MetadataConflictError("artist", self.stage / issue["tracks"][0], ["Act"], "Act feat. Guest")
        proof = deepcopy(issue["details"]["verified_release_numbering"])
        setattr(error, "release_audit", deepcopy(issue["details"]))
        setattr(error, "release_numbering", proof)
        with patch.object(ui, "input", side_effect=["1", "APPLY"]), \
             patch.object(mixie, "finalize_reviewed_album", side_effect=error), redirect_stdout(StringIO()):
            ui._apply(mixie, [issue])
        restored = mixie.load_review_queue()[0]
        self.assertEqual(proof, restored["details"]["verified_release_numbering"])
        self.assertEqual(issue["source_fingerprint"], restored["details"]["source_fingerprint"])
        self.assertIsNotNone(ui._release_disc_structure(restored, ui._view(mixie, restored)))

    def test_apply_archive_pending_result_points_to_retry_not_reapproval(self):
        issue = self.issue()
        issue.update(interpretation=ui._build_user_interpretation(mixie, issue), review_status="reviewed")
        def pending_archives(selected, issues):
            mixie._mark_archive_pending(selected)
            mixie.save_review_queue(issues)
            return False
        with patch.object(ui, "input", side_effect=["1", "APPLY"]), \
             patch.object(mixie, "finalize_reviewed_album", side_effect=pending_archives), \
             redirect_stdout(StringIO()) as stream:
            ui._apply(mixie, [issue])
        output = stream.getvalue()
        self.assertIn("use Review Manager option 4", output)
        self.assertIn("No new metadata approval or recording selection is needed", output)
        self.assertNotIn("available to edit and reapply", output)
        self.assertEqual("archive_pending", mixie.load_review_queue()[0]["status"])

    def test_back_navigation_preserves_unresolved_queue_and_never_starts_apply(self):
        issue = self.issue()
        before = deepcopy(issue)
        with patch.object(ui, "input", side_effect=["1", "1", "1", "0", "0", "0", "0"]), \
             patch.object(mixie, "finalize_reviewed_album", side_effect=AssertionError("No Apply requested")), \
             redirect_stdout(StringIO()) as stream:
            ui.review_manager_menu(mixie, [issue])
        for screen in ("Review Manager", "Albums Needing Review", "=== Review:", "Track Tags:"):
            self.assertIn(screen, stream.getvalue())
        self.assertEqual(before, issue)


class ArchiveRetryMenuTests(WorkspaceCase):
    def pending(self, album="Imported"):
        return {"album": album, "import_name": "Delivery", "tracks": [f"Delivery/{album}/01.flac"],
            "status": "archive_pending", "review_status": "reviewed", "source_fingerprint": "old source",
            "interpretation": {"user_approved": True}, "details": {"kind": "completion"},
            "reason": "Library import verified; archive pending: required ZIPs"}

    def unresolved(self):
        return {"album": "Unresolved sibling", "import_name": "Delivery", "tracks": [],
                "review_status": "needs_review", "tag_edits": {"album": {"album": "Saved draft"}},
                "details": {"kind": "identity_release"}, "reason": "Recording needs review"}

    def forbid_import_work(self):
        for module, name in ((sort, "sort_inbox"), (tag, "group_staged_albums"),
                (audio, "audit_tracks"), (lookup, "_musicbrainz_call"), (mixie, "_main"),
                (mixie, "preview_album"), (mixie, "test_album_plan"), (mixie, "get_acoustid_client_key"),
                (mixie, "test_review_decision"), (mixie, "finalize_reviewed_album"),
                (mixie, "_choose_album_interpretation"), (ui, "_approve_review"), (ui, "_apply")):
            self.stack.enter_context(patch.object(module, name,
                side_effect=AssertionError("Archive retry must not call " + name)))

    def test_numbered_retry_runs_under_menu_lock_saves_and_reloads_without_approval(self):
        self.forbid_import_work()
        pending, draft = self.pending(), self.unresolved()
        issues = [pending, draft]
        mixie.save_review_queue(issues)
        def finalize(*, allow_source_cleanup=False):
            self.assertFalse(allow_source_cleanup)
            self.assertGreater(mixie._session_depth, 0)
            mixie.save_review_queue([draft])
            return 1, []
        with mixie.ingest_session(), \
             patch.object(mixie, "finalize_verified_imports", side_effect=finalize, create=True) as retry, \
             patch.object(mixie, "save_review_queue", wraps=mixie.save_review_queue) as save, \
             patch.object(mixie, "load_review_queue", wraps=mixie.load_review_queue) as load, \
             patch.object(mixie, "_invalidate_stale_choices", side_effect=AssertionError("No metadata review for existing import")), \
             patch.object(ui, "input", side_effect=["4", "0"]) as inputs, redirect_stdout(StringIO()) as stream:
            ui.review_manager_menu(mixie, issues)
        retry.assert_called_once_with()
        load.assert_called_once_with()
        save.assert_called_once_with([draft])
        self.assertEqual(2, inputs.call_count)
        self.assertEqual([draft], issues)
        self.assertEqual([draft], mixie.load_review_queue())
        output = stream.getvalue()
        self.assertIn("4. Finish / Retry Required Archives (1 pending)", output)
        self.assertIn("4. Finish / Retry Required Archives (0 pending)", output)
        self.assertIn("1. Albums Needing Review (1)", output)
        self.assertIn("2. Reviewed — Not Applied (0)", output)
        self.assertIn("Required archives newly finalized: 1", output)
        self.assertIn("No archive/finalization failures reported", output)
        self.assertNotIn("Type APPROVE", output)
        self.assertNotIn("Choose recording", output)

    def test_archive_only_queue_is_visible_and_back_does_not_run_a_retry(self):
        self.forbid_import_work()
        issue = self.pending()
        issues = [issue]
        before = deepcopy(issues)
        with patch.object(mixie, "finalize_verified_imports", create=True) as retry, \
             patch.object(mixie, "_invalidate_stale_choices", side_effect=AssertionError("Imported source is not an approval prompt")), \
             patch.object(ui, "input", return_value="0"), redirect_stdout(StringIO()) as stream:
            ui.review_manager_menu(mixie, issues)
        retry.assert_not_called()
        output = stream.getvalue()
        self.assertIn("1. Albums Needing Review (0)", output)
        self.assertIn("2. Reviewed — Not Applied (0)", output)
        self.assertIn("4. Finish / Retry Required Archives (1 pending)", output)
        self.assertEqual(before, issues)

    def test_retry_reports_shared_retention_separately_from_real_archive_failures(self):
        self.forbid_import_work()
        complete, failed, draft = self.pending("Finished"), self.pending("Failed"), self.unresolved()
        issues = [complete, failed, draft]
        mixie.save_review_queue(issues)
        problems = [
            "Delivery: Shared source has unaccounted audio; full original and staging retained.",
            "Delivery: Shared source still has unresolved album(s); verified albums archived independently, full original and staging retained.",
            "Failed: Library ZIP verification failed; originals retained.",
        ]
        def finalize():
            mixie.save_review_queue([failed, draft])
            return 1, problems
        with patch.object(mixie, "finalize_verified_imports", side_effect=finalize, create=True), \
             patch.object(ui, "input", side_effect=["4", "0"]), redirect_stdout(StringIO()) as stream:
            ui.review_manager_menu(mixie, issues)
        output = stream.getvalue()
        self.assertIn("Archive/finalization failures: 1", output)
        self.assertNotIn("Archive/finalization failures: 3", output)
        self.assertIn("Shared-source retention (not an archive failure)", output)
        self.assertIn("Library ZIP verification failed; originals retained", output)
        self.assertIn("Still awaiting required archives/finalization: 1", output)
        self.assertEqual([failed, draft], issues)
        self.assertEqual([failed, draft], mixie.load_review_queue())

    def test_retained_shared_sources_without_archive_failures_are_not_reported_as_errors(self):
        self.forbid_import_work()
        issues = [self.pending()]
        mixie.save_review_queue(issues)
        def finalize():
            mixie.save_review_queue([])
            return 1, ["Delivery: Shared source has unaccounted audio; full original and staging retained."]
        with patch.object(mixie, "finalize_verified_imports", side_effect=finalize, create=True), \
             patch.object(ui, "input", side_effect=["4", "0"]), redirect_stdout(StringIO()) as stream:
            ui.review_manager_menu(mixie, issues)
        self.assertEqual([], issues)
        self.assertIn("No archive/finalization failures reported", stream.getvalue())
        self.assertNotIn("Archive/finalization failures: 1", stream.getvalue())
        self.assertIn("physical sources stay retained by default", stream.getvalue())

    def test_failed_or_interrupted_retry_reloads_durably_saved_progress(self):
        self.forbid_import_work()
        for error in (OSError("Fixture archive disk failure"), KeyboardInterrupt(), EOFError()):
            with self.subTest(error=type(error).__name__):
                pending, draft = self.pending(), self.unresolved()
                issues = [pending, draft]
                mixie.save_review_queue(issues)
                def finalize(draft=draft, error=error):
                    mixie.save_review_queue([draft])
                    raise error
                with patch.object(mixie, "finalize_verified_imports", side_effect=finalize, create=True), \
                     patch.object(ui, "input", side_effect=["4", "0"]), redirect_stdout(StringIO()) as stream:
                    ui.review_manager_menu(mixie, issues)
                self.assertEqual([draft], issues)
                self.assertEqual([draft], mixie.load_review_queue())
                self.assertIn("Still awaiting required archives/finalization: 0", stream.getvalue())
                self.assertNotIn("Required archives newly finalized: 0", stream.getvalue())

    def test_failed_queue_reload_returns_to_main_without_overwriting_saved_progress(self):
        self.forbid_import_work()
        issues = [self.pending()]
        before = deepcopy(issues)
        def finalize():
            mixie.save_review_queue([])
            return 1, []
        with patch.object(mixie, "finalize_verified_imports", side_effect=finalize, create=True), \
             patch.object(mixie, "load_review_queue", side_effect=OSError("Fixture read failure")), \
             patch.object(ui, "input", side_effect=["4"]) as inputs, redirect_stdout(StringIO()) as stream:
            ui.review_manager_menu(mixie, issues)
        self.assertEqual(1, inputs.call_count)
        self.assertEqual(before, issues)
        self.assertEqual([], mixie.load_review_queue())
        self.assertIn("reopen Review Manager before continuing", stream.getvalue())

    def test_numbered_retry_works_without_an_existing_pending_queue_entry(self):
        self.forbid_import_work()
        discovered = self.pending("Recovered from receipt")
        issues = []
        def finalize():
            mixie.save_review_queue([discovered])
            return 0, ["Recovered from receipt: Required originals archive is unavailable."]
        with patch.object(mixie, "finalize_verified_imports", side_effect=finalize, create=True) as retry, \
             patch.object(ui, "input", side_effect=["4", "0"]), redirect_stdout(StringIO()) as stream:
            ui.review_manager_menu(mixie, issues)
        retry.assert_called_once_with()
        self.assertEqual([discovered], issues)
        self.assertIn("4. Finish / Retry Required Archives (1 pending)", stream.getvalue())

    def test_menu_retry_finalizes_real_existing_receipt_and_retains_the_shared_source(self):
        import mixie_finalizer as final
        from test_finalizer_v10i import assert_shared_originals

        (self.script / ".mixie-import-first.json").write_text(json.dumps({
            "version": 1, "mode": "verified_library_first"}), encoding="utf-8")
        source = self.inbox / "Shared delivery"
        for name in ("Imported", "Unresolved"):
            folder = source / name
            folder.mkdir(parents=True)
            (folder / "01.flac").write_bytes(("untouched fixture " + name).encode())
        with redirect_stdout(StringIO()):
            sort.sort_inbox()
        name = sort.staging_import_name(source.name, "directory")
        relative = f"{name}/Imported/01.flac"
        issue = {"import_name": name, "album": "Imported", "tracks": [relative], "details": {"kind": "completion"}}
        destination = self.library / "Artist" / "Imported" / "01 - Song.flac"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"verified synthetic output")
        cover = destination.parent / "cover.jpg"
        cover.write_bytes(b"verified synthetic artwork")
        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = mixie.CommitReceipt({str(destination): digest(destination), str(cover): digest(cover)}, input_map={relative: {
            "source_sha256": digest(self.stage / relative), "destination": str(destination),
            "destination_sha256": digest(destination)}}, source_manifest=mixie.completion_source_manifest(issue),
            asset_outputs={str(cover): digest(cover)}, evidence={"provenance": "independently_verified"})
        identity = mixie.record_library_only_import(issue, receipt)
        mixie._mark_archive_pending(issue)
        draft = {"import_name": name, "album": "Unresolved", "tracks": [f"{name}/Unresolved/01.flac"],
                 "details": {"kind": "identity_release"}, "reason": "Unresolved recording", "tag_edits": {"album": {"genre": "Jazz"}}}
        draft['source_fingerprint'] = mixie.review_fingerprint(draft)
        issues = [issue, draft]
        mixie.save_review_queue(issues)
        inbox_before, stage_before = sort.file_manifest(self.inbox), sort.file_manifest(self.stage)
        self.forbid_import_work()
        operation = getattr(mixie, "finalize_verified_imports")
        with mixie.ingest_session(), \
             patch.object(mixie, "finalize_verified_imports", wraps=operation) as retry, \
             patch.object(ui, "input", side_effect=["4", "0"]), redirect_stdout(StringIO()) as stream:
            ui.review_manager_menu(mixie, issues)
        retry.assert_called_once_with()
        self.assertEqual(1, len(issues))
        self.assertEqual(draft["tracks"], issues[0]["tracks"])
        self.assertEqual(draft["tag_edits"], issues[0]["tag_edits"])
        self.assertEqual(issues, mixie.load_review_queue())
        records = assert_shared_originals(self, final._state()['finalized'][identity], {name: {
            'kind': 'directory', 'tracks': ['Imported/01.flac'],
            'payload': {f'{album}/01.flac': ('untouched fixture ' + album).encode()
                        for album in ('Imported', 'Unresolved')}}})
        self.assertEqual([Path(records[name]['path'])], list((self.backup / 'Originals').rglob('*.zip')))
        self.assertTrue((self.backup / "Library" / "Artist" / "Imported.zip").is_file())
        self.assertEqual(inbox_before, sort.file_manifest(self.inbox))
        self.assertEqual(stage_before, sort.file_manifest(self.stage))
        self.assertIn("No archive/finalization failures reported", stream.getvalue())
        self.assertIn("4. Finish / Retry Required Archives (0 pending)", stream.getvalue())


class ManualReviewOutputTests(WorkspaceCase):
    def test_verified_multidisc_totals_reach_actual_working_copy_output(self):
        rows, originals, tracklist, matched = [], {}, [], {}
        for disc, total in ((1, 2), (2, 3)):
            for number in range(1, total + 1):
                path = make_audio(self.stage / "Delivery" / f"{disc}-{number}.flac", album="Album",
                    albumartist="Act", artist="Act", title=f"Song {disc}-{number}", date="2024", genre="Rock")
                rows.append(tag.read_track_metadata(path))
                originals[path] = path.read_bytes()
                matched[json.dumps([disc, number])] = path.relative_to(self.stage).as_posix()
                tracklist.append({"disc": str(disc), "number": str(number),
                    "recording_id": f"rec-{disc}-{number}", "title": f"Song {disc}-{number}"})
        album = {"album": "Album", "folder": rows[0]["path"].parent, "tracks": rows}
        issue = mixie._album_issue(album)
        issue.update(source_fingerprint=mixie.review_fingerprint(issue), reason="Fixture review", details={
            "kind": "identity_release", "source_fingerprint": mixie.review_fingerprint(issue), "releases": [{"release_id": "release-one", "status": "complete",
                "tracklist_complete": True, "errors": [], "matched": matched, "tracklist": tracklist}]})
        with patch.object(ui, "input", return_value="APPROVE"), redirect_stdout(StringIO()):
            self.assertTrue(ui._approve_review(mixie, issue, [issue]))
        with patch.object(audio, "audit_tracks", side_effect=AssertionError("No identity replay")), \
             patch.object(mixie, "select_album_art", return_value=make_cover()), redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, None, interpretation=mixie.load_review_queue()[0]["interpretation"])
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertIsNotNone(receipt)
        for path, original in originals.items():
            relative = path.relative_to(self.stage).as_posix()
            output = tag.read_track_metadata(receipt.input_map[relative]["destination"])
            assert output is not None, "Committed working-copy tags must be readable"
            disc = tag.parse_disc_number(output["disc_number"])[0]
            self.assertEqual({2 if disc == 1 else 3}, tag.get_track_totals(output))
            self.assertEqual({2}, tag.get_disc_totals(output))
            self.assertEqual(("Rock",), output["genre"])
            self.assertEqual(original, path.read_bytes())
        self.assertEqual("user_approved", plan["provenance"])
        self.assertEqual({}, mixie.load_completed_reviews())
        self.assertEqual(1, len(mixie.load_review_queue()))

    def test_optional_genre_and_single_disc_choices_reach_actual_working_copy_output(self):
        path = make_audio(self.stage / "Delivery" / "01.flac", album="Album", albumartist="Act",
            artist="Act", title="Song", tracknumber=1, tracktotal=12, date="2024")
        original = path.read_bytes()
        album = {"album": "Album", "folder": path.parent, "tracks": [tag.read_track_metadata(path)]}
        issue = mixie._album_issue(album)
        issue.update(reason="Manual fixture", details={"kind": "identity_release"})
        with patch.object(ui, "input", return_value="APPROVE"), redirect_stdout(StringIO()):
            self.assertTrue(ui._approve_review(mixie, issue, [issue]))
        restored = mixie.load_review_queue()[0]
        with patch.object(audio, "audit_tracks", side_effect=AssertionError("No automatic identity replay")), \
             patch.object(mixie, "select_album_art", return_value=make_cover()), redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, None, interpretation=restored["interpretation"])
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertIsNotNone(receipt)
        destination = receipt.input_map[path.relative_to(self.stage).as_posix()]["destination"]
        output = tag.read_track_metadata(destination)
        assert output is not None, "Committed working-copy tags must be readable"
        self.assertEqual((), output["genre"])
        self.assertEqual({12}, tag.get_track_totals(output))
        number = tag.parse_track_number(output["track_number"])[0]
        assert number is not None, "A reviewed track number must reach the final output"
        self.assertEqual(1, int(number))
        self.assertEqual(original, path.read_bytes())
        self.assertEqual("user_approved", plan["provenance"])
        self.assertFalse(plan["identities"][str(path)]["verified"])
        self.assertEqual({}, mixie.load_completed_reviews())
        self.assertEqual(1, len(mixie.load_review_queue()))
