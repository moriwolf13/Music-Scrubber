"""Focused review UI contract checks; synthetic sources in the protected runner only."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import mixie
import mixie_review_ui as ui
import review
import tag
from support import WorkspaceCase, metadata_record


class ReviewContractV10ITests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.rows = {}
        self.original_bytes = {}
        self.stack.enter_context(patch.object(tag, "read_track_metadata",
            side_effect=lambda path: deepcopy(self.rows[Path(path)])))
        for name in ("preview_album", "test_album_plan", "finalize_reviewed_album",
                     "get_acoustid_client_key"):
            self.stack.enter_context(patch.object(mixie, name,
                side_effect=AssertionError("UI must not identify or import: " + name)))
        self.addCleanup(self.assert_sources_unchanged)

    def assert_sources_unchanged(self):
        for path, data in self.original_bytes.items():
            self.assertEqual(data, path.read_bytes())

    def issue(self, *, totals=True, multiple=False):
        relatives = []
        for index in (1, 2):
            path = self.track_path(f"Import/{index:02}.flac", f"synthetic source {index}".encode())
            self.original_bytes[path] = path.read_bytes()
            self.rows[path] = metadata_record(path, title=(f"Source Song {index}",),
                artist=("Source Artist",), album_artist=("Source Album Artist",),
                track_number=(str(1 if multiple else index),),
                disc_number=(str(index if multiple else 1),),
                track_total=(("1" if multiple else "2",) if totals else ()),
                disc_total=("2" if multiple else "1",))
            relatives.append(path.relative_to(self.stage).as_posix())
        return {"album": "Album", "import_name": "Import", "tracks": relatives,
                "reason": "Synthetic review", "details": {"kind": "identity_release",
                    "tracks": [], "releases": []}}

    def advanced(self, issue, *, online=False):
        fingerprint = mixie.review_fingerprint(issue)
        tracks = {relative: {"recording_id": f"chosen-{index}" if online else None,
            "title": f"Chosen Song {index} (live)", "artist": f"Chosen Artist {index}",
            "disc": 1 if online else 2, "number": index if online else index + 3}
            for index, relative in enumerate(issue["tracks"], 1)}
        decision = {"status": "pending_application", "user_approved": True,
            "independently_verified": False, "source_fingerprint": fingerprint,
            "release_id": "chosen-release" if online else None, "partial": True,
            "album": "Chosen Album", "album_artist": "Chosen Album Artist",
            "date": "2019-02-03", "genre": "Jazz", "tracks": tracks}
        if online:
            decision["release_tracks"] = [{"disc": "1", "number": str(index),
                "recording_id": f"chosen-{index}", "title": f"Chosen Song {index} (live)"}
                for index in (1, 2, 3)]
        else:
            decision["totals"] = {relative: {"track_total": 12, "disc_total": 3}
                                  for relative in tracks}
        issue.update(source_fingerprint=fingerprint, interpretation=decision)
        return deepcopy(decision)

    def approve(self, issue, answer):
        with patch.object(ui, "input", return_value=answer), redirect_stdout(StringIO()) as output:
            result = ui._approve_review(mixie, issue, [issue])
        return result, output.getvalue()

    def test_primary_approval_requires_exact_case_sensitive_approve(self):
        for answer in ("a", "A", "approve", "Approve", " APPROVE", "APPROVE ", "", "0"):
            with self.subTest(answer=answer):
                issue = self.issue()
                before = deepcopy(issue)
                with patch.object(mixie, "save_review_queue") as save:
                    result, _ = self.approve(issue, answer)
                self.assertFalse(result)
                self.assertEqual(before, issue)
                save.assert_not_called()

    def test_exact_approval_saves_reviewed_not_applied_and_previews_album_fields(self):
        issue = self.issue()
        with patch.object(ui, "input", return_value="APPROVE") as confirm, \
             patch.object(mixie, "_import_first_enabled", return_value=True), redirect_stdout(StringIO()) as stream:
            result = ui._approve_review(mixie, issue, [issue])
        output = stream.getvalue()
        self.assertIn("Type APPROVE", confirm.call_args.args[0])
        self.assertTrue(result)
        self.assertTrue(ui.reviewed(issue))
        self.assertEqual(issue, mixie.load_review_queue()[0])
        for text in ("Album", "Source Album Artist", "2020", "Rock", "track 1",
                     "Source Song 1", "Source Artist", "tracks: 2",
                     "NOT independently verified", "Reviewed — Not Applied",
                     "Artist: Source Artist", "Library ZIP backup is optional"):
            self.assertIn(text, output)
        self.assertNotIn("archival proof remain mandatory", output)
        self.assertNotIn("Disc Layout:", output)
        self.assertNotIn("discs in album:", output)

    def test_preview_uses_effective_track_edits_album_artist_fallback_and_totals(self):
        issue = self.issue()
        first, second = issue["tracks"]
        for field, value, relative in (("artist", "Shared Artist", None),
                ("title", "Edited One (live)", first), ("track_number", "2", first),
                ("track_number", "1", second), ("artist", "Specific Artist", second),
                ("date", "2024-01-02", None), ("genre", "Electronic", None)):
            ui.save_choice(mixie, issue, [issue], field=field, value=value, relative=relative)
        result, output = self.approve(issue, "APPROVE")
        self.assertTrue(result)
        self.assertIn("track 2: Edited One (live) / Shared Artist", output)
        self.assertIn("track 1: Source Song 2 / Specific Artist", output)
        self.assertIn("2024-01-02", output)
        self.assertIn("Electronic", output)
        self.assertIn("tracks: 2", output)

    def test_primary_preserves_fresh_approved_advanced_offline_choices(self):
        issue = self.issue()
        decision = self.advanced(issue)
        result, output = self.approve(issue, "APPROVE")
        self.assertTrue(result)
        self.assertEqual(decision, issue["interpretation"])
        self.assertEqual(decision, mixie.load_review_queue()[0]["interpretation"])
        for text in ("Chosen Album", "Chosen Album Artist", "2019-02-03", "Jazz",
                     "disc 2, track 4: Chosen Song 1 (live) / Chosen Artist 1",
                     "tracks on disc: 12", "discs in album: 3"):
            self.assertIn(text, output)
        self.assertNotIn("Source Song 1", output)

    def test_primary_preserves_online_snapshot_without_provider_calls(self):
        issue = self.issue()
        decision = self.advanced(issue, online=True)
        with patch.object(review, "_fetch_tracklist", side_effect=AssertionError("No lookup during preview")):
            result, output = self.approve(issue, "APPROVE")
        self.assertTrue(result)
        self.assertEqual(decision, issue["interpretation"])
        self.assertIn("tracks: 3", output)
        self.assertIn("Missing tracklist positions: 1", output)

    def test_stale_advanced_approval_cannot_be_rebound_to_current_source(self):
        issue = self.issue()
        self.advanced(issue)
        issue["interpretation"]["source_fingerprint"] = "e" * 64
        before = deepcopy(issue)
        with patch.object(mixie, "save_review_queue") as save:
            result, _ = self.approve(issue, "APPROVE")
        self.assertFalse(result)
        self.assertEqual(before, issue)
        save.assert_not_called()

    def test_advanced_draft_is_not_silently_replaced_or_promoted(self):
        issue = self.issue()
        self.advanced(issue)
        issue["interpretation"].update(status="draft", user_approved=False)
        before = deepcopy(issue)
        with patch.object(mixie, "save_review_queue") as save:
            result, _ = self.approve(issue, "APPROVE")
        self.assertFalse(result)
        self.assertEqual(before, issue)
        save.assert_not_called()

    def test_approval_interrupt_keeps_saved_choices_without_approval(self):
        for interrupt in (EOFError, KeyboardInterrupt):
            with self.subTest(interrupt=interrupt):
                issue = self.issue()
                before = deepcopy(issue)
                with patch.object(ui, "input", side_effect=interrupt), redirect_stdout(StringIO()):
                    self.assertFalse(ui._approve_review(mixie, issue, [issue]))
                self.assertEqual(before, issue)

    def test_single_disc_track_menu_hides_irrelevant_disc_number(self):
        issue = self.issue()
        with patch.object(ui, "input", return_value="0"), redirect_stdout(StringIO()) as output:
            ui._show_tracks(mixie, issue, [issue])
        for label in ("1. Title:", "2. Track Number:", "3. Artist:"):
            self.assertIn(label, output.getvalue())
        self.assertNotIn("Disc Number:", output.getvalue())
        self.assertNotIn("More Track", output.getvalue())

    def test_multidisc_track_menu_keeps_explicit_disc_number(self):
        issue = self.issue(multiple=True)
        with patch.object(ui, "input", return_value="0"), redirect_stdout(StringIO()) as output:
            ui._show_tracks(mixie, issue, [issue])
        for label in ("1. Title:", "2. Track Number:", "3. Disc Number:", "4. Artist:"):
            self.assertIn(label, output.getvalue())

    def test_track_menu_shows_album_artist_field_fallback_and_track_override(self):
        issue = self.issue()
        first, second = issue["tracks"]
        issue["tag_edits"] = {"album": {"artist": "Shared Artist"},
                              "tracks": {second: {"artist": "Specific Artist"}}}
        with patch.object(ui, "input", side_effect=["n", "0"]), redirect_stdout(StringIO()) as output:
            ui._show_tracks(mixie, issue, [issue])
        self.assertIn("3. Artist: Shared Artist", output.getvalue())
        self.assertIn("3. Artist: Specific Artist", output.getvalue())
        self.assertNotIn("Artist: Source Artist", output.getvalue())

    def test_direct_disc_number_edit_is_not_silently_forced_back_to_one(self):
        issue = self.issue()
        ui.save_choice(mixie, issue, [issue], field="disc_number", value="2", relative=issue["tracks"][0])
        ui.save_choice(mixie, issue, [issue], field="disc_total", value="2")
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual(2, decision["tracks"][issue["tracks"][0]]["disc"])
        self.assertEqual({2}, {value["disc_total"] for value in decision["totals"].values()})

    def test_explicit_single_layout_rejects_a_conflicting_disc_edit(self):
        issue = self.issue()
        ui.save_choice(mixie, issue, [issue], field="disc_layout", value="single")
        ui.save_choice(mixie, issue, [issue], field="disc_number", value="2", relative=issue["tracks"][0])
        with self.assertRaisesRegex(ValueError, "[Dd]isc.*[Ll]ayout|[Ss]ingle.*[Dd]isc"):
            ui._build_user_interpretation(mixie, issue)
        self.assertTrue(any("Disc" in problem for problem in ui.preflight_issues(mixie, issue)))

    def test_zero_at_track_text_prompts_postpones_without_saving(self):
        for field in (1, 2, 3, 4):
            with self.subTest(field=field):
                # Single-disc reviews have three fields; multi-disc has four.
                issue = self.issue(multiple=(field == 4))
                before = deepcopy(issue)
                with patch.object(ui, "input", side_effect=[str(field), "0", "0"]) as inputs, \
                     patch.object(mixie, "save_review_queue") as save, redirect_stdout(StringIO()):
                    ui._show_tracks(mixie, issue, [issue])
                self.assertEqual(before, issue)
                self.assertEqual(2, inputs.call_count)
                self.assertIn("0", inputs.call_args.args[0])
                save.assert_not_called()

    def test_zero_at_album_text_prompts_postpones_without_saving(self):
        for field in range(1, 9):
            with self.subTest(field=field):
                issue = self.issue(multiple=True)
                before = deepcopy(issue)
                with patch.object(ui, "input", side_effect=[str(field), "0", "0"]) as inputs, \
                     patch.object(mixie, "save_review_queue") as save, redirect_stdout(StringIO()):
                    ui._show_album(mixie, issue, [issue])
                self.assertEqual(before, issue)
                self.assertEqual(2, inputs.call_count)
                save.assert_not_called()

    def test_zero_at_album_artist_overwrite_confirmation_preserves_credits(self):
        issue = self.issue()
        self.rows[self.stage / issue["tracks"][1]]["artist"] = ("Different Artist",)
        before = deepcopy(issue)
        with patch.object(ui, "input", side_effect=["2", "0", "0"]) as inputs, redirect_stdout(StringIO()):
            ui._show_album(mixie, issue, [issue])
        self.assertEqual(before, issue)
        self.assertEqual(2, inputs.call_count)

    def test_zero_at_guest_replace_confirmation_preserves_guest(self):
        issue = self.issue()
        self.rows[self.stage / issue["tracks"][0]]["artist"] = ("Lead feat. Guest",)
        before = deepcopy(issue)
        with patch.object(ui, "input", side_effect=["3", "Lead", "0", "0"]) as inputs, redirect_stdout(StringIO()):
            ui._show_tracks(mixie, issue, [issue])
        self.assertEqual(before, issue)
        self.assertEqual(3, inputs.call_count)
        self.assertIn("REPLACE", inputs.call_args.args[0])
        self.assertIn("0", inputs.call_args.args[0])

    def test_missing_track_totals_have_provisional_contiguous_local_default(self):
        issue = self.issue(totals=False)
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual({2}, {value['track_total'] for value in decision['totals'].values()})
        self.assertFalse(decision['independently_verified'])
        self.assertIsNone(decision['release_id'])
        self.assertFalse(any('Track Total missing' in problem for problem in ui.preflight_issues(mixie, issue)))

    def test_explicit_offline_total_can_exceed_represented_file_count(self):
        issue = self.issue(totals=False)
        ui.save_choice(mixie, issue, [issue], field="track_total", value="12")
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual({12}, {value["track_total"] for value in decision["totals"].values()})
        self.assertTrue(decision["partial"])
        self.assertIsNone(decision["release_id"])
        self.assertFalse(decision["independently_verified"])
        self.assertFalse(any("Track Total" in problem for problem in ui.preflight_issues(mixie, issue)))

    def test_complete_unanimous_release_can_supply_missing_totals(self):
        issue = self.issue(totals=False)
        issue["source_fingerprint"] = mixie.review_fingerprint(issue)
        issue["details"]["source_fingerprint"] = issue["source_fingerprint"]
        issue["details"]["releases"] = [{"status": "complete", "tracklist_complete": True,
            "errors": [], "matched": {f"[1,{index}]": relative
                for index, relative in enumerate(issue["tracks"], 1)},
            "tracklist": [{"disc": "1", "number": str(index)} for index in (1, 2)]}]
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual({2}, {value["track_total"] for value in decision["totals"].values()})
        self.assertFalse(any("Track Total" in problem for problem in ui.preflight_issues(mixie, issue)))

    def test_multidisc_missing_total_requires_advanced_per_disc_choices(self):
        issue = self.issue(multiple=True)
        self.rows[self.stage / issue["tracks"][1]]["track_total"] = ()
        with self.assertRaisesRegex(ValueError, "[Aa]dvanced"):
            ui._build_user_interpretation(mixie, issue)
        report = "\n".join(ui.preflight_issues(mixie, issue))
        self.assertIn("Track Total", report)
        self.assertIn("2", report)
        self.assertIn("advanced", report.lower())

    def test_multidisc_album_wide_track_total_is_not_silently_ignored(self):
        issue = self.issue(multiple=True)
        with patch.object(ui, "input", side_effect=["6", "0", "0"]), redirect_stdout(StringIO()) as output:
            ui._show_album(mixie, issue, [issue])
        self.assertNotIn("tag_edits", issue)
        self.assertIn("advanced", output.getvalue().lower())
        self.assertIn("per-disc", output.getvalue().lower())

    def test_preflight_lists_known_metadata_and_artwork_conflicts_together(self):
        issue = self.issue()
        issue["details"] = {"kind": "metadata_conflict", "field": "album",
            "existing_values": ["Album"], "reference_value": "Other edition"}
        issue["artwork_details"] = {"kind": "artwork_conflict", "candidates": []}
        report = "\n".join(ui.preflight_issues(mixie, issue))
        for text in ("Metadata conflict", "Album", "Other edition", "Artwork"):
            self.assertIn(text, report)
        with patch.object(ui, "input", return_value="0"), redirect_stdout(StringIO()) as output:
            ui._album_editor(mixie, issue, [issue])
        self.assertNotIn("All local fields ready", output.getvalue())

    def test_preflight_also_lists_artwork_conflict_in_primary_details(self):
        issue = self.issue()
        issue["details"] = {"kind": "artwork_conflict", "candidates": []}
        self.assertTrue(any("Artwork" in problem for problem in ui.preflight_issues(mixie, issue)))

    def test_preflight_reports_totals_smaller_than_selected_positions(self):
        issue = self.issue()
        ui.save_choice(mixie, issue, [issue], field="track_number", value="3", relative=issue["tracks"][1])
        self.assertTrue(any("Track Total" in problem for problem in ui.preflight_issues(mixie, issue)))
        with self.assertRaisesRegex(ValueError, "[Tt]rack total.*smaller"):
            ui._build_user_interpretation(mixie, issue)

    def test_subsequent_apply_errors_retain_original_and_intermediate_evidence(self):
        issue = self.issue()
        original = deepcopy(issue["details"])
        original["tracks"] = [{"path": issue["tracks"][0], "assessment": {
            "status": "conflicting", "candidates": [{"recording_id": "original-candidate"}]}}]
        issue["details"] = deepcopy(original)
        self.advanced(issue)
        issue["review_status"] = "reviewed"
        original_art = {"kind": "artwork_conflict", "candidates": [{"label": "Original cover"}]}
        issue["artwork_details"] = deepcopy(original_art)
        interpretation = deepcopy(issue["interpretation"])
        errors = [tag.RequiredMetadataReview(["date"]),
                  tag.MetadataConflictError("artist", self.stage / issue["tracks"][0], ["Old"], "New"),
                  mixie.ProviderUnavailable({"reason": "Synthetic outage", "tracks": [], "releases": []})]
        for error in errors:
            with patch.object(ui, "input", side_effect=["1", "APPLY"]), \
                 patch.object(mixie, "finalize_reviewed_album", side_effect=error), redirect_stdout(StringIO()):
                ui._apply(mixie, [issue])
        history = issue.get("review_evidence_history", [])
        self.assertTrue(any(entry.get("details") == original for entry in history))
        self.assertTrue(any((entry.get("details") or {}).get("kind") == "required_metadata" for entry in history))
        self.assertTrue(any((entry.get("details") or {}).get("field") == "artist" for entry in history))
        self.assertTrue(any(entry.get("artwork_details") == original_art for entry in history))
        self.assertEqual(interpretation, issue["interpretation"])
        self.assertTrue(ui.reviewed(issue))
        self.assertEqual(history, mixie.load_review_queue()[0]["review_evidence_history"])
        self.assertEqual("provider_unavailable", issue["details"]["kind"])
        report = "\n".join(ui.preflight_issues(mixie, issue))
        # Earlier diagnostics remain in history, but must not be mistaken for
        # a new actionable review blocker after the latest provider response.
        self.assertIn('Synthetic outage', report)
        for text in ('Earlier diagnostic', 'Metadata conflict — Artist', 'Old vs New'):
            self.assertNotIn(text, report)
        # Current unsolved artwork still belongs in the active blocker list.
        self.assertIn('Artwork conflict', report)

    def test_advanced_approval_returns_to_list_without_an_extra_confirmation(self):
        issue = self.issue()
        def choose(selected, _issues):
            self.advanced(selected)
            selected["review_status"] = "reviewed"  # Parent-owned checkpoint contract.
            return True
        with patch.object(ui, "input", side_effect=["r", "0"]) as inputs, \
             patch.object(mixie, "_choose_album_interpretation", side_effect=choose), redirect_stdout(StringIO()):
            self.assertEqual("list", ui._album_editor(mixie, issue, [issue]))
        self.assertEqual(1, inputs.call_count)
        self.assertTrue(ui.reviewed(issue))
