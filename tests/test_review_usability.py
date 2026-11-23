"""Log-shaped review UI regressions; all evidence and files are synthetic.

Run ONLY: .venv/bin/python -I -B tests/run_isolated.py discover -s tests -p 'test_review*.py' -v
"""
from contextlib import redirect_stdout
from copy import deepcopy
import json
from unittest.mock import patch

from test_review import ReviewCase, SOURCE_HASH, recording, release_track
import review


class ReviewUsabilityTests(ReviewCase):
    def numbered_offline_answers(self):
        return ["2", "2", "3", "Local collection", "Local artist", "1", "1",
                "1", "1", "3", "My title", "My credit", "1", "1",
                "4", "2", "12", "3", "5", "APPROVE"]

    def log_issue(self):
        # Reproduce the supplied log's presentation, not live provider evidence.
        path = self.track(title=('Welcome to the Internet (From "Inside")',),
                          artist=("Bo Burnham",), track_number=("1/1",))
        first = recording("suggested-recording", "Welcome to the Internet", "Bo Burnham")
        first["length"] = "275999"
        candidates = [
            {"recording_id": first["id"], "score": 1.0, "details": first,
             "duration_difference": 0.0009999999999763531, "reasons": []},
            {"recording_id": "unavailable-recording", "score": 1.0, "details": None,
             "duration_difference": None, "reasons": ["recording_details_unavailable"]},
        ]
        issue = self.issue([path], candidates=candidates,
                           reasons=("recording_details_unavailable", "incomplete_evidence", "provider_error"))
        row = issue["details"]["tracks"][0]
        row["assessment"]["complete"] = False
        row["evidence"].update(
            fingerprint={"duration": 276.0, "fingerprint": "SYNTHETIC"},
            candidates={first["id"]: 1.0, "unavailable-recording": 1.0},
            recordings={first["id"]: first}, complete=False, lookup_state="partial",
            errors=[{"stage": "musicbrainz", "recording_id": "unavailable-recording",
                     "code": "provider_error", "type": "NetworkError",
                     "message": "HTTP Error 503: Service Temporarily Unavailable"}],
        )
        return issue

    def test_log_summary_uses_readable_hints_not_raw_evidence(self):
        issue = self.log_issue()
        before = deepcopy(issue["details"])
        saved = []
        self.assertFalse(self.choose(issue, ["postpone"], save=lambda value: saved.append(deepcopy(value))))
        output = self.output.getvalue()
        self.assertIn("Title: Welcome to the Internet", output)
        self.assertIn("Artist: Bo Burnham", output)
        for technical in ("{'title':", "assessment=", "errors=[", "provider_error",
                          "suggested-recording", "HTTP Error 503", "track N / release"):
            self.assertNotIn(technical, output)
        self.assertEqual(before, issue["details"])
        self.assertEqual(before, saved[-1]["details"])
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_log_entry_screen_has_numbered_actions_and_obvious_postpone(self):
        self.assertFalse(self.choose(self.log_issue(), ["postpone"]))
        output = self.output.getvalue()
        for action in ("1. Review track identities", "2. Choose album release", "3. Set album details",
                       "4. Set track/disc totals", "5. Preview and approve", "6. Advanced evidence",
                       "0. Postpone album"):
            self.assertIn(action, output)
        self.assertIn("NOT independently verified", output)

    def test_log_provider_outage_explains_uncertainty_and_recommends_postponing(self):
        self.assertFalse(self.choose(self.log_issue(), ["postpone"]))
        output = self.output.getvalue()
        self.assertIn("Recommended: 0", output)
        self.assertIn("temporary", output.lower())
        self.assertIn("retry later", output.lower())
        self.assertIn("not proof", output.lower())
        self.assertNotIn("NetworkError", output)

    def test_log_durations_handle_numeric_strings_missing_values_and_float_noise(self):
        self.assertFalse(self.choose(self.log_issue(), ["postpone"]))
        output = self.output.getvalue()
        self.assertIn("4:36", output)
        self.assertIn("276.0 seconds", output)
        self.assertIn("<0.1 seconds", output)
        self.assertIn("unavailable", output.lower())
        for confusing in ("275999s", "Nones", "0.0009999999999763531"):
            self.assertNotIn(confusing, output)

    def test_advanced_and_normal_durations_convert_numeric_string_milliseconds_without_changing_evidence(self):
        issue = self.log_issue()
        before = deepcopy(issue["details"])
        self.assertFalse(self.choose(issue, ["6", "0"]))
        normal, advanced = self.output.getvalue().split("\nTechnical evidence", 1)
        for section in (normal, advanced):
            self.assertIn("4:36 (276.0 seconds)", section)
            self.assertNotIn("275999s", section)
        self.assertIn("Raw MusicBrainz length values are milliseconds", advanced)
        self.assertIn('"length": "275999"', advanced)
        self.assertEqual(before, issue["details"])

    def test_linux_filename_characters_survive_draft_resume_approval_and_preparation(self):
        name = 'Import/Will Wood/"In case I make it,"/\tSong: "Title".flac  '
        path = self.track(name)
        original = path.read_bytes()
        issue, saved = self.issue([path]), []
        self.assertFalse(self.choose(issue, self.numbered_offline_answers()[:11] + ["0"],
                                     save=lambda value: saved.append(deepcopy(value))))
        restored = json.loads(json.dumps(saved[-1]))
        self.assertEqual([name], restored["tracks"])
        self.assertEqual(name, restored["details"]["tracks"][0]["path"])
        self.assertEqual([name], list(restored["interpretation"]["tracks"]))
        self.assertEqual("My title", restored["interpretation"]["tracks"][name]["title"])
        answers = ["1", "1", "3", "", "My credit", "1", "1", "4", "2", "12", "3", "5", "APPROVE"]
        self.assertTrue(self.choose(restored, answers))
        self.assertEqual([name], list(restored["interpretation"]["tracks"]))
        self.assertEqual([name], list(restored["interpretation"]["totals"]))
        plan = review.prepare_interpretation(self.album([path]), restored["interpretation"], SOURCE_HASH)
        self.assertEqual({("1", "1"): path}, plan["matched"])
        self.assertEqual({str(path): {"track_total": 12, "disc_total": 3}}, plan["numbering_totals"])
        self.assertFalse(plan["identities"][str(path)]["verified"])
        self.assertEqual(original, path.read_bytes())
        self.assertNotIn("\t", self.output.getvalue())
        self.assertIn('\\x09Song: "Title".flac  ', self.output.getvalue())
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_zero_postpones_immediately_and_checkpoints_unapproved_draft(self):
        issue, saved = self.log_issue(), []
        with patch("builtins.input", side_effect=["0", "postpone"]) as inputs, redirect_stdout(self.output):
            result = review.choose_interpretation(issue, SOURCE_HASH,
                                                  save=lambda value: saved.append(deepcopy(value)))
        self.assertFalse(result)
        self.assertEqual(1, inputs.call_count)
        self.assertIn("0", inputs.call_args.args[0])
        self.assertEqual("draft", saved[-1]["interpretation"]["status"])
        self.assertFalse(saved[-1]["interpretation"]["user_approved"])
        self.assertFalse(saved[-1]["interpretation"]["independently_verified"])

    def test_numbered_advanced_view_preserves_all_evidence_and_exposes_legacy_help(self):
        issue = self.log_issue()
        before = deepcopy(issue["details"])
        self.assertFalse(self.choose(issue, ["6", "postpone"]))
        output = self.output.getvalue()
        self.assertIn("Technical evidence", output)
        self.assertIn('"provider_error"', output)
        self.assertIn("HTTP Error 503", output)
        self.assertIn("suggested-recording", output)
        self.assertIn("Legacy text commands", output)
        self.assertIn("track N", output)
        self.assertEqual(before, issue["details"])
        self.assertEqual({}, issue["interpretation"]["tracks"])
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_numbered_offline_workflow_previews_readable_totals_and_preserves_safety(self):
        path = self.track()
        issue, saved = self.issue([path]), []
        before, original = deepcopy(issue["details"]), path.read_bytes()
        self.assertTrue(self.choose(issue, self.numbered_offline_answers(),
                                    save=lambda value: saved.append(deepcopy(value))))
        decision = issue["interpretation"]
        self.assertIsNone(decision["release_id"])
        self.assertEqual({"recording_id": None, "title": "My title", "artist": "My credit", "disc": 1, "number": 1},
                         decision["tracks"][issue["tracks"][0]])
        self.assertEqual({issue["tracks"][0]: {"track_total": 12, "disc_total": 3}}, decision["totals"])
        self.assertTrue(decision["user_approved"])
        self.assertFalse(decision["independently_verified"])
        self.assertTrue(decision["partial"])
        self.assertEqual("pending_application", saved[-1]["interpretation"]["status"])
        self.assertTrue(all(value["interpretation"]["status"] == "draft" for value in saved[:-1]))
        output = self.output.getvalue()
        self.assertIn("tracks on disc: 12; discs in album: 3", output)
        self.assertIn("does not write audio or bypass safety checks", output)
        self.assertIn("Type APPROVE", output)
        for raw in ("{'", "partial=True", "pending_application", "recording_id"):
            self.assertNotIn(raw, output)
        self.assertEqual(before, issue["details"])
        self.assertEqual(original, path.read_bytes())
        plan = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertEqual({}, plan["numbering_deferred"])
        self.assertFalse(plan["identities"][str(path)]["verified"])
        self.assertEqual([], plan["compatible_ids"])
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_numbered_online_workflow_keeps_explicit_positions_missing_tracks_and_full_totals(self):
        full = [release_track("missing-rec"), release_track("other-rec", "1", "2"),
                release_track("rec-a", "2", "2")]
        evaluation = {"release_id": "release-a", "title": "Chosen edition", "status": "partial",
                      "tracklist": full, "missing": full[:2], "review": [], "reasons": ["missing_release_tracks"]}
        path = self.track()
        issue = self.issue([path], evaluations=[evaluation])
        before = deepcopy(issue["details"])
        self.provide_release(full)
        answers = ["2", "1", "1", "1", "1", "", "", "2", "2",
                   "3", "", "", "1", "1", "4", "5", "APPROVE"]
        self.assertTrue(self.choose(issue, answers))
        decision = issue["interpretation"]
        self.assertEqual((2, 2), (decision["tracks"][issue["tracks"][0]]["disc"],
                                 decision["tracks"][issue["tracks"][0]]["number"]))
        self.assertNotIn("totals", decision)
        self.assertFalse(decision["independently_verified"])
        self.assertEqual(before, issue["details"])
        plan = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertEqual(full[:2], plan["missing"])
        self.assertTrue(plan["partial"])
        self.assertEqual({str(path): {"track_total": 2, "disc_total": 2}}, plan["numbering_totals"])
        output = self.output.getvalue()
        self.assertIn("3 tracks across 2 discs, including unimported tracks", output)
        self.assertIn("Online totals must come from the full selected release", output)
        self.assertIn("Missing tracklist positions: 2", output)
        self.assertNotIn("missing-rec", output)

    def test_numbered_recording_search_requires_explicit_candidate_selection(self):
        issue = self.issue()
        self.providers["search_recording"].side_effect = None
        self.providers["search_recording"].return_value = [
            recording("not-selected", "First suggestion"), recording("selected-rec", "Second suggestion")]
        answers = ["2", "2", "3", "Local", "Artist", "1", "1",
                   "1", "1", "2", "Query artist", "Query title", "2", "", "", "1", "1",
                   "4", "3", "5", "APPROVE"]
        self.assertTrue(self.choose(issue, answers))
        self.providers["search_recording"].assert_called_once_with("Query artist", "Query title")
        self.assertEqual("selected-rec", issue["interpretation"]["tracks"][issue["tracks"][0]]["recording_id"])
        self.assertFalse(issue["interpretation"]["independently_verified"])
        self.assertIn("Text search results are suggestions, not audio verification", self.output.getvalue())
        self.assertIn("Application must defer required numbering", self.output.getvalue())

    def test_numbered_album_search_requires_explicit_release_selection(self):
        issue = self.issue()
        before = deepcopy(issue["details"])
        self.providers["search_album"].side_effect = None
        self.providers["search_album"].return_value = [
            {"id": "not-selected", "title": "Wrong edition"},
            {"id": "chosen-release", "title": "Chosen edition", "country": "GB", "date": "2021"}]
        self.provide_release(identifier="chosen-release")
        answers = ["2", "1", "Query artist", "Query album", "2",
                   "1", "1", "1", "", "", "1", "5", "APPROVE"]
        self.assertTrue(self.choose(issue, answers))
        self.providers["search_album"].assert_called_once_with("Query artist", "Query album")
        self.providers["get_release_tracklist"].assert_called_once_with("chosen-release", refresh=True)
        self.assertEqual("chosen-release", issue["interpretation"]["release_id"])
        self.assertNotIn("date", issue["interpretation"])
        self.assertEqual(before, issue["details"])
        self.assertIn("Chosen edition (2021; GB)", self.output.getvalue())

    def test_zero_at_every_normal_prompt_saves_the_latest_draft_without_approval(self):
        answers = ["2", "2", "3", "Local", "Artist", "2", "2020-02-29", "2", "Rock",
                   "1", "1", "3", "My title", "My credit", "1", "1",
                   "4", "2", "12", "3", "5", "APPROVE"]
        for cutoff in range(len(answers)):
            with self.subTest(prompt=cutoff):
                issue, saved = self.issue(), []
                before = deepcopy(issue["details"])
                with patch("builtins.input", side_effect=answers[:cutoff] + ["0"]) as inputs, redirect_stdout(self.output):
                    result = review.choose_interpretation(issue, SOURCE_HASH,
                                                          save=lambda value: saved.append(deepcopy(value)))
                self.assertFalse(result)
                self.assertEqual(cutoff + 1, inputs.call_count)
                self.assertTrue(all("0 = postpone album" in call.args[0] for call in inputs.call_args_list))
                self.assertEqual("draft", saved[-1]["interpretation"]["status"])
                self.assertFalse(saved[-1]["interpretation"]["user_approved"])
                self.assertFalse(saved[-1]["interpretation"]["independently_verified"])
                self.assertEqual(before, saved[-1]["details"])
                if cutoff > 12:
                    self.assertEqual("My title", saved[-1]["interpretation"]["tracks"][issue["tracks"][0]]["title"])
                if cutoff > 18:
                    self.assertEqual(12, saved[-1]["interpretation"]["totals"][issue["tracks"][0]]["track_total"])
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_zero_at_search_and_advanced_id_prompts_never_starts_a_lookup(self):
        prefixes = (["1", "1", "2"], ["1", "1", "2", "Artist"], ["1", "1", "4"],
                    ["2", "1"], ["2", "1", "Artist"], ["2", "3"])
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                issue = self.issue()
                self.assertFalse(self.choose(issue, prefix + ["0"]))
                self.assertEqual({}, issue["interpretation"]["tracks"])
                self.assertNotIn("release_id", issue["interpretation"])
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_numbered_back_options_are_distinct_from_album_postponement(self):
        issue, saved = self.issue(), []
        self.assertFalse(self.choose(issue, ["2", "4", "1", "2", "2", "2", "4", "4", "0"],
                                     save=lambda value: saved.append(deepcopy(value))))
        self.assertIsNone(issue["interpretation"]["release_id"])
        self.assertEqual({}, issue["interpretation"]["tracks"])
        self.assertNotIn("retain_existing_totals", issue["interpretation"])
        self.assertEqual("draft", saved[-1]["interpretation"]["status"])

    def test_blank_invalid_or_recommended_action_never_implicitly_selects_or_approves(self):
        issue = self.issue()
        self.assertFalse(self.choose(issue, ["", "99", "5", "0"]))
        self.assertEqual({}, issue["interpretation"]["tracks"])
        self.assertNotIn("release_id", issue["interpretation"])
        self.assertFalse(issue["interpretation"]["user_approved"])
        self.assertIn("Cannot approve", self.output.getvalue())
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_numbered_optional_fields_validate_values_and_can_remove_a_manual_choice(self):
        issue = self.issue()
        answers = ["3", "Local", "Artist", "2", "2023-02-30", "2", "2023-02-28", "2", "Rock",
                   "3", "", "", "3", "1", "0"]
        self.assertFalse(self.choose(issue, answers))
        self.assertNotIn("date", issue["interpretation"])
        self.assertEqual("Rock", issue["interpretation"]["genre"])
        self.assertIn("does not erase existing source tags", self.output.getvalue())
        self.assertNotIn("2023-02-30", str(issue["interpretation"]))

    def test_legacy_optional_values_still_reject_leading_control_characters(self):
        issue = self.issue()
        self.assertFalse(self.choose(issue, ["fields", "Local", "Artist", "\n2020", "2021", "\x85Rock", "Jazz", "0"]))
        self.assertEqual("2021", issue["interpretation"]["date"])
        self.assertEqual("Jazz", issue["interpretation"]["genre"])

    def test_numbered_retain_and_defer_totals_preserve_explicit_policy(self):
        for choice in ("1", "3"):
            with self.subTest(choice=choice):
                path = self.track(track_number=("1/12",), disc_number=("1/3",))
                issue = self.issue([path])
                answers = self.numbered_offline_answers()[:14] + ["4", choice, "5", "APPROVE"]
                self.assertTrue(self.choose(issue, answers))
                plan = review.prepare_interpretation(self.album([path]), issue["interpretation"], SOURCE_HASH)
                if choice == "1":
                    self.assertTrue(issue["interpretation"]["retain_existing_totals"])
                    self.assertEqual({str(path): {"track_total": 12, "disc_total": 3}}, plan["numbering_totals"])
                else:
                    self.assertFalse(issue["interpretation"]["retain_existing_totals"])
                    self.assertEqual({}, plan["numbering_totals"])
                    self.assertEqual({str(path): ["track_total", "disc_total"]}, plan["numbering_deferred"])

    def test_numbered_multi_track_selector_never_maps_unselected_sources(self):
        first, second = self.track(), self.track("Import/Other/01.flac")
        issue = self.issue([first, second])
        before = deepcopy(issue["details"])
        self.assertFalse(self.choose(issue, ["1", "99", "2", "3", "Second song", "Credit", "1", "2", "5", "0"]))
        self.assertEqual({issue["tracks"][1]: {"recording_id": None, "title": "Second song",
                                             "artist": "Credit", "disc": 1, "number": 2}},
                         issue["interpretation"]["tracks"])
        self.assertFalse(issue["interpretation"]["user_approved"])
        self.assertEqual(before, issue["details"])
        self.assertIn("Choose a source number", self.output.getvalue())
        self.assertIn("Cannot approve", self.output.getvalue())
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_numbered_recommendations_follow_missing_choices_without_inferring_them(self):
        issue = self.issue()
        answers = ["2", "2", "1", "1", "3", "Song", "Credit", "1", "1",
                   "3", "Album", "Artist", "1", "1", "4", "3", "0"]
        self.assertFalse(self.choose(issue, answers))
        for action in (2, 1, 3, 4, 5):
            self.assertIn(f"Recommended: {action}", self.output.getvalue())
        self.assertFalse(issue["interpretation"]["user_approved"])

    def test_numbered_preview_still_requires_exact_approve_not_a_number_or_default(self):
        for confirmation in ("1", "approve", " APPROVE", "APPROVE ", ""):
            with self.subTest(confirmation=confirmation):
                issue = self.issue()
                answers = self.numbered_offline_answers()[:-1] + [confirmation]
                self.assertFalse(self.choose(issue, answers))
                self.assertEqual("draft", issue["interpretation"]["status"])
                self.assertFalse(issue["interpretation"]["user_approved"])

    def test_advanced_view_redacts_secret_fields_and_escapes_terminal_controls(self):
        issue = self.log_issue()
        issue["details"]["tracks"][0]["existing"]["title"] = ["Unsafe\x1b[31m title"]
        issue["details"]["tracks"][0]["evidence"]["api_key"] = "SYNTHETIC-SECRET"
        before = deepcopy(issue["details"])
        self.assertFalse(self.choose(issue, ["6", "0"]))
        output = self.output.getvalue()
        self.assertNotIn("SYNTHETIC-SECRET", output)
        self.assertNotIn("\x1b", output)
        self.assertIn("\\x1b", output)
        self.assertIn("\\u001b", output)
        self.assertEqual(before, issue["details"])
