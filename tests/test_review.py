"""Manual review regressions: synthetic evidence, disposable files, no providers.

Run ONLY: .venv/bin/python -I -B tests/run_isolated.py discover -s tests -p test_review.py -v
These are invented branch fixtures, not assertions about commercial recordings.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
from unittest.mock import patch

from support import WorkspaceCase, make_audio, make_cover, metadata_record
import audio
import lookup
import review
import tag


SOURCE_HASH = sha256(b"synthetic source inventory").hexdigest()
OTHER_HASH = sha256(b"changed synthetic source inventory").hexdigest()


def recording(identifier="rec-a", title="Chosen song", artist="Chosen artist"):
    return {"id": identifier, "title": title, "artist-credit-phrase": artist,
            "length": 200000, "release-list": [{"id": "release-a"}]}


def release_track(identifier="rec-a", number="1", disc="1", title="Chosen song"):
    return {"disc": disc, "number": number, "recording_id": identifier,
            "title": title, "length": 200000}


class ReviewCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.records = {}
        self.reader = tag.read_track_metadata
        self.metadata = self.stack.enter_context(patch.object(
            tag, "read_track_metadata", side_effect=lambda path: deepcopy(self.records.get(Path(path)))))
        self.providers = {}
        for name in ("search_recording", "search_album", "get_recording_details",
                     "get_release_tracklist", "get_release_metadata"):
            self.providers[name] = self.stack.enter_context(patch.object(
                lookup, name, side_effect=AssertionError(f"Unexpected provider: {name}")))
        for module, name in ((audio, "identify_audio"), (audio, "inspect_audio"),
                             (audio, "collect_recording_evidence"), (audio, "urlopen"),
                             (lookup, "_musicbrainz_call"), (lookup, "urlopen")):
            self.stack.enter_context(patch.object(
                module, name, side_effect=AssertionError(f"Forbidden provider: {name}")))
        self.output = StringIO()

    def track(self, name="Import/Album/01.flac", **fields):
        path = self.track_path(name)
        self.records[path] = metadata_record(path, **fields)
        return path

    def audit(self, path, *, status="probable", reasons=("low_confidence",), candidates=None):
        details = recording()
        candidates = candidates if candidates is not None else [{
            "recording_id": "rec-a", "score": 0.94, "duration_difference": 2.5,
            "details": details, "status": status, "reasons": list(reasons)}]
        return {"path": path, "status": "needs_review", "reason": "Needs a human choice",
                "details": details, "assessment": {"status": status, "complete": True,
                    "reason": "Needs a human choice", "reasons": list(reasons),
                    "candidates": candidates, "errors": []},
                "evidence": {"fingerprint": {"duration": 202.5, "fingerprint": "SYNTHETIC"},
                    "candidates": {"rec-a": 0.94}, "recordings": {"rec-a": details},
                    "matches": [], "errors": [], "complete": True, "lookup_state": "complete"}}

    def issue(self, paths=None, evaluations=(), **audit_options):
        paths = paths or [self.track()]
        details = review.issue_details([self.audit(path, **audit_options) for path in paths],
                                       evaluations, "Consolidated identity/release review")
        return {"import_name": "Import", "album": "Untrusted album hint", "details": details,
                "tracks": [path.relative_to(self.stage).as_posix() for path in paths]}

    def choose(self, issue, answers, save=None, fingerprint=SOURCE_HASH):
        answers = iter(answers)
        def respond(prompt):
            self.output.write(prompt + "\n")
            try:
                return next(answers)
            except StopIteration:
                raise AssertionError(f"Unexpected input prompt: {prompt}") from None
        with patch("builtins.input", side_effect=respond), redirect_stdout(self.output):
            return review.choose_interpretation(issue, fingerprint, save=save)

    def offline_answers(self, *, approve="APPROVE"):
        return ["release", "offline", "fields", "Local collection", "Local artist", "", "",
                "track 1", "manual", "My title", "My credit", "1", "1", "approve", approve]

    def decision(self, paths, *, release_id=None, **changes):
        result = {"status": "pending_application", "user_approved": True,
                  "independently_verified": False, "source_fingerprint": SOURCE_HASH,
                  "release_id": release_id, "album": "Chosen album", "album_artist": "Chosen artist",
                  "partial": release_id is None,
                  "tracks": {path.relative_to(self.stage).as_posix(): {
                      "recording_id": None if release_id is None else f"rec-{index}",
                      "title": f"Title {index}", "artist": "My explicit credit", "disc": 1, "number": index}
                      for index, path in enumerate(paths, 1)}}
        result.update(changes)
        return result

    def album(self, paths):
        return {"album": "Existing album", "folder": paths[0].parent,
                "tracks": [deepcopy(self.records[path]) for path in paths]}

    def provide_release(self, tracks=None, identifier="release-a"):
        tracks = tracks if tracks is not None else [release_track()]
        self.providers["get_release_tracklist"].side_effect = None
        self.providers["get_release_tracklist"].return_value = deepcopy(tracks)
        self.providers["get_release_metadata"].side_effect = None
        self.providers["get_release_metadata"].return_value = {
            "release_id": identifier, "album": "Chosen album", "album_artist": "Chosen artist"}


class EvidenceTests(ReviewCase):
    def test_public_apis_are_available(self):
        for name in ("issue_details", "choose_interpretation", "prepare_interpretation"):
            self.assertTrue(callable(getattr(review, name, None)), name)
        self.assertTrue(issubclass(review.AlbumReviewRequired, ValueError))

    def test_issue_details_keeps_all_tracks_release_diagnostics_and_metadata_hints(self):
        first, second = self.track(), self.track("Import/Album/02.flac", title=("Existing hint",))
        evaluation = {"release_id": "release-a", "status": "conflicting", "missing": [release_track()],
                      "review": [second], "reasons": ["local_track_number_conflict"],
                      "tracklist": [release_track()], "matched": {(1, 1): first}, "errors": []}
        audits = [self.audit(first), self.audit(second, status="conflicting", reasons=("duration_conflict",))]
        before = deepcopy((audits, evaluation))
        details = review.issue_details(audits, [evaluation], "Multiple review triggers")
        self.assertEqual("identity_release", details["kind"])
        self.assertEqual(["Import/Album/01.flac", "Import/Album/02.flac"], [t["path"] for t in details["tracks"]])
        self.assertEqual(["Existing hint"], details["tracks"][1]["existing"]["title"])
        self.assertEqual(["duration_conflict"], details["tracks"][1]["assessment"]["reasons"])
        self.assertEqual(["local_track_number_conflict"], details["releases"][0]["reasons"])
        self.assertEqual(before, (audits, evaluation))
        json.dumps(details, allow_nan=False)

    def test_exception_details_are_detached_json_safe_and_never_contain_api_keys(self):
        path = self.track()
        audit = self.audit(path)
        audit["evidence"]["api_key"] = "DO-NOT-PERSIST-ACOUSTIC-KEY"
        audit["evidence"]["nested"] = {"ACOUSTID_CLIENT_KEY": "ALSO-SECRET", "path": path}
        audit["assessment"]["candidates"][0]["score"] = float("nan")
        evaluation = {"release_id": "release-a", "status": "partial", "matched": {(1, 1): path},
                      "missing": [], "review": [path], "reasons": [], "tracklist": []}
        details = review.issue_details([audit], [evaluation], "Manual choice required")
        error = review.AlbumReviewRequired(details)
        payload = json.dumps(error.details, allow_nan=False)
        self.assertNotIn("DO-NOT-PERSIST", payload)
        self.assertNotIn("ALSO-SECRET", payload)
        self.assertIn("Manual choice required", str(error))
        self.assertEqual("Import/Album/01.flac", error.details["releases"][0]["matched"]["[1,1]"])
        details["tracks"].clear()
        self.assertEqual(1, len(error.details["tracks"]))

    def test_empty_duplicate_and_outside_audit_inventories_are_rejected(self):
        path = self.track()
        for audits in ([], [self.audit(path), self.audit(path)], [self.audit(self.home / "outside.flac")]):
            with self.subTest(audits=audits), self.assertRaises(ValueError):
                review.issue_details(audits, [], "Invalid inventory")

    def test_lookup_unavailable_keeps_errors_and_does_not_need_an_acoustic_key(self):
        path = self.track()
        audit = self.audit(path, status="unidentified", candidates=[])
        audit["evidence"].update(complete=False, lookup_state="unavailable",
                                 errors=[{"stage": "acoustid", "code": "provider_error", "message": "offline"}])
        with patch.dict(os.environ, {"ACOUSTID_CLIENT_KEY": ""}):
            details = review.issue_details([audit], [], "Offline review")
        self.assertEqual("offline", details["tracks"][0]["evidence"]["errors"][0]["message"])
        self.assertFalse(details["tracks"][0]["evidence"]["complete"])

    def test_positive_madilyn_and_unresolved_abba_keep_independent_assessment_status(self):
        # Labels only: synthetic scores/lengths, not captured commercial evidence.
        fingerprint = {"duration": 200}
        positive = audio.assess_recording_candidates(fingerprint, {"madilyn-rec": 0.99},
            {"madilyn-rec": recording("madilyn-rec", artist="Madilyn Bailey")})
        unresolved = audio.assess_recording_candidates(fingerprint, {"abba-a": 0.99, "abba-b": 0.98},
            {identifier: recording(identifier, artist="ABBA") for identifier in ("abba-a", "abba-b")})
        self.assertEqual("verified", positive["status"])
        self.assertEqual("conflicting", unresolved["status"])
        paths = [self.track("Import/Madilyn/01.flac"), self.track("Import/ABBA/01.flac")]
        audits = [dict(self.audit(path), assessment=assessment) for path, assessment in zip(paths, (positive, unresolved))]
        details = review.issue_details(audits, [], "Separate manual and automatic provenance")
        self.assertEqual(["verified", "conflicting"], [row["assessment"]["status"] for row in details["tracks"]])
        self.assertEqual(0.95, audio.MIN_RECORDING_SCORE)
        self.assertEqual(3, audio.MAX_DURATION_DIFFERENCE)


class SourcePathTests(ReviewCase):
    def test_colon_album_paths_survive_evidence_serialization_and_preparation(self):
        names = ["Import/Then: The Earlier Years/01.flac", "Import/Hawaii: Part II/01.flac",
                 "Import/Cyberpunk: Endgame/01.flac"]
        paths = [self.track(name) for name in names]
        evaluation = {"release_id": "release-a", "review": paths,
                      "matched": {(1, index): path for index, path in enumerate(paths, 1)}}
        issue = self.issue(paths, evaluations=[evaluation])
        restored = json.loads(json.dumps(issue))
        self.assertEqual(names, [row["path"] for row in restored["details"]["tracks"]])
        self.assertEqual(names, restored["details"]["releases"][0]["review"])
        self.assertEqual(names, list(restored["details"]["releases"][0]["matched"].values()))
        self.assertFalse(self.choose(restored, ["0"]))
        decision = self.decision(paths, totals={name: {"track_total": 12, "disc_total": 1} for name in names})
        before = deepcopy(decision)
        plan = review.prepare_interpretation(self.album(paths), decision, SOURCE_HASH)
        self.assertEqual({("1", str(index)): path for index, path in enumerate(paths, 1)}, plan["matched"])
        self.assertEqual({str(path): {"track_total": 12, "disc_total": 1} for path in paths}, plan["numbering_totals"])
        self.assertEqual(before, decision)
        self.assertTrue(all(not identity["verified"] for identity in plan["identities"].values()))
        self.assertTrue(all(not provider.called for provider in self.providers.values()))

    def test_source_paths_preserve_quotes_tabs_and_spaces_exactly(self):
        names = [' Import/ Album / "quoted": song.flac ', 'Import/Will Wood/\tSong.flac',
                 'Import/Album/.. song.flac', 'Import/Album/song..flac', 'Import/Album/ \t ']
        paths = [self.track_path(name) for name in names]
        for name, path in zip(names, paths):
            with self.subTest(name=name):
                self.assertEqual((name, path), review._source_path(name))
                self.assertEqual((name, path), review._source_path(Path(name)))
                self.assertEqual((name, path), review._source_path(path, allow_absolute=True))

    def test_source_paths_do_not_inherit_the_metadata_text_length_limit(self):
        name = "/".join(["Import"] + ["a" * 64] * 8 + ["song.flac"])
        self.assertGreater(len(name), 512)
        path = self.track_path(name)
        self.assertEqual((name, path), review._source_path(name))
        with self.assertRaises(ValueError):
            review._text("a" * 513, "title")

    def test_source_paths_reject_traversal_nul_backslashes_and_noncanonical_separators(self):
        path = self.track_path("Import/Album/song.flac")
        unsafe = (None, b"Import/Album/song.flac", "", ".", "..", "../song.flac",
                  str(path), "//" + str(path).lstrip("/"), "Import/Album/../Album/song.flac",
                  "Import/Then:/../Album/song.flac", "Import/Album/./song.flac",
                  "./Import/Album/song.flac", "Import//Album/song.flac", "Import/Album/song.flac/",
                  "Import\\Album\\song.flac", "Import/Album/song.flac\x00")
        for name in unsafe:
            with self.subTest(name=name), self.assertRaises(ValueError):
                review._source_path(name)
        for name in ("Import/Album/../Album/song.flac", "Import/Album/./song.flac"):
            with self.subTest(absolute=name), self.assertRaises(ValueError):
                review._source_path(str(self.stage) + "/" + name, allow_absolute=True)

    def test_absolute_inventory_input_stays_contained_and_saved_paths_stay_relative(self):
        name = 'Import/Then: The Earlier Years/\t"song".flac '
        path = self.track_path(name)
        self.assertEqual((name, path), review._source_path(path, allow_absolute=True))
        with self.assertRaises(ValueError):
            review._source_path(str(path))
        outside = self.home / "outside: song.flac "
        outside.write_bytes(b"disposable outside source")
        for source in (outside, self.stage.parent / ".." / outside.name):
            with self.subTest(source=source), self.assertRaises(ValueError):
                review._source_path(source, allow_absolute=True)

    def test_symlinks_remain_rejected_with_linux_filename_characters(self):
        path = self.track_path("Import/Album/song.flac")
        outside = self.home / "outside.flac"
        outside.write_bytes(b"disposable outside source")
        file_link = self.stage / 'Import/Album/\tlinked: "song".flac '
        file_link.symlink_to(outside)
        inside_link = self.stage / "Import/linked: album "
        inside_link.symlink_to(path.parent, target_is_directory=True)
        outside_link = self.stage / "Import/\toutside: album"
        outside_link.symlink_to(self.home, target_is_directory=True)
        for source in (file_link, inside_link / path.name, outside_link / outside.name):
            name = source.relative_to(self.stage).as_posix()
            with self.subTest(name=name), self.assertRaises(ValueError):
                review._source_path(name)
        root_link = self.script / "staging: link"
        root_link.symlink_to(self.stage, target_is_directory=True)
        with patch.object(tag, "staging_folder", root_link), self.assertRaises(ValueError):
            review._source_path("Import/Album/song.flac")

    def test_metadata_controls_stay_strict_independently_of_source_filename_rules(self):
        path = self.track()
        for control in ("\t", "\n", "\x00", "\x1b", "\x7f", "\x85"):
            for field in ("title", "artist", "album", "album_artist", "date", "genre"):
                with self.subTest(field=field, control=control), self.assertRaises(ValueError):
                    decision = self.decision([path])
                    target = decision["tracks"]["Import/Album/01.flac"] if field in ("title", "artist") else decision
                    target[field] = control + ("2021" if field == "date" else "Unsafe metadata")
                    review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertEqual('Title: "quoted"', review._text('  Title: "quoted"  ', "title"))


class InteractiveReviewTests(ReviewCase):
    def test_offline_exact_approval_schema_and_user_supplied_credit(self):
        issue = self.issue()
        snapshots = []
        with patch.dict(os.environ, {"ACOUSTID_CLIENT_KEY": ""}):
            self.assertTrue(self.choose(issue, ["advanced"] + self.offline_answers(),
                                        save=lambda value: snapshots.append(deepcopy(value))))
        decision = issue["interpretation"]
        self.assertEqual("pending_application", decision["status"])
        self.assertIs(decision["user_approved"], True)
        self.assertIs(decision["independently_verified"], False)
        self.assertEqual(SOURCE_HASH, decision["source_fingerprint"])
        self.assertEqual(SOURCE_HASH, issue["source_fingerprint"])
        self.assertIsNone(decision["release_id"])
        self.assertTrue(decision["partial"])
        self.assertNotIn("date", decision)
        self.assertNotIn("genre", decision)
        self.assertEqual({"recording_id": None, "title": "My title", "artist": "My credit", "disc": 1, "number": 1},
                         decision["tracks"][issue["tracks"][0]])
        self.assertGreaterEqual(len(snapshots), 6)
        self.assertEqual("draft", snapshots[0]["interpretation"]["status"])
        self.assertEqual("pending_application", snapshots[-1]["interpretation"]["status"])
        self.assertIn("OFFLINE", self.output.getvalue())
        self.assertIn("USER-APPROVED", self.output.getvalue())
        self.assertIn("NOT independently verified", self.output.getvalue())
        self.assertIn("0.94", self.output.getvalue())
        self.assertIn("202.5", self.output.getvalue())
        self.assertIn("2.5", self.output.getvalue())
        self.assertIn("low_confidence", self.output.getvalue())
        self.assertTrue(all(not mock.called for mock in self.providers.values()))

    def test_lowercase_approval_does_not_authorize_and_postpones_with_draft(self):
        issue = self.issue()
        self.assertFalse(self.choose(issue, self.offline_answers(approve="approve")))
        self.assertEqual("draft", issue["interpretation"]["status"])
        self.assertFalse(issue["interpretation"]["user_approved"])
        self.assertEqual("My title", issue["interpretation"]["tracks"][issue["tracks"][0]]["title"])

    def test_postpone_and_resume_keep_incremental_track_draft(self):
        issue = self.issue()
        saved = []
        self.assertFalse(self.choose(issue, ["track 1", "manual", "Draft title", "postpone"],
                                     save=lambda value: saved.append(deepcopy(value))))
        self.assertEqual("Draft title", issue["interpretation"]["tracks"][issue["tracks"][0]]["title"])
        self.assertEqual("Draft title", saved[-1]["interpretation"]["tracks"][issue["tracks"][0]]["title"])
        self.assertTrue(self.choose(issue, ["release", "offline", "fields", "My album", "My artist", "", "",
                                          "track 1", "manual", "", "My credit", "1", "1", "approve", "APPROVE"]))
        self.assertEqual("Draft title", issue["interpretation"]["tracks"][issue["tracks"][0]]["title"])

    def test_eof_and_keyboard_interrupt_save_drafts_without_approval(self):
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption):
                issue, saved = self.issue(), []
                with patch("builtins.input", side_effect=["track 1", "manual", "Saved title", interruption]), redirect_stdout(self.output):
                    result = review.choose_interpretation(issue, SOURCE_HASH, save=lambda value: saved.append(deepcopy(value)))
                self.assertFalse(result)
                self.assertEqual("Saved title", saved[-1]["interpretation"]["tracks"][issue["tracks"][0]]["title"])
                self.assertFalse(issue["interpretation"]["user_approved"])

    def test_per_track_candidates_and_explicit_multidisc_mapping(self):
        first, second = self.track(), self.track("Import/Album/02.flac")
        issue = self.issue([first, second])
        issue["details"]["tracks"][1]["assessment"]["candidates"] = [{
            "recording_id": "rec-b", "score": 0.98, "duration_difference": 0,
            "details": recording("rec-b", "Second song", "Second artist"),
            "status": "verified", "reasons": []}]
        self.provide_release([release_track("rec-a", "2"), release_track("rec-b", "1", "2", "Second song")])
        self.assertTrue(self.choose(issue, ["release", "id", "release-a",
            "fields", "", "", "", "", "track 1", "1", "", "", "1", "2",
            "track 2", "1", "", "", "2", "1", "approve", "APPROVE"]))
        tracks = issue["interpretation"]["tracks"]
        self.assertEqual((1, 2), (tracks[issue["tracks"][0]]["disc"], tracks[issue["tracks"][0]]["number"]))
        self.assertEqual("rec-b", tracks[issue["tracks"][1]]["recording_id"])
        self.assertFalse(issue["interpretation"]["partial"])
        self.assertEqual("probable", issue["details"]["tracks"][0]["assessment"]["status"])
        self.assertFalse(issue["interpretation"]["independently_verified"])

    def test_search_recording_and_album_use_lookup_and_require_explicit_selection(self):
        issue = self.issue(candidates=[])
        self.providers["search_recording"].side_effect = None
        self.providers["search_recording"].return_value = [{"id": "search-rec", "title": "Search song",
            "artist-credit": [{"artist": {"name": "Search artist"}}], "length": 201000, "ext:score": "98"}]
        self.providers["search_album"].side_effect = None
        self.providers["search_album"].return_value = [{"id": "search-release", "title": "Search album", "ext:score": "99"}]
        self.provide_release([release_track("search-rec")], identifier="search-release")
        self.assertTrue(self.choose(issue, ["track 1", "search", "Query artist", "Query title", "1", "", "", "1", "1",
            "release", "search", "Album artist query", "Album query", "1", "fields", "", "", "", "", "approve", "APPROVE"]))
        self.providers["search_recording"].assert_called_once_with("Query artist", "Query title")
        self.providers["search_album"].assert_called_once_with("Album artist query", "Album query")
        self.assertEqual("search-rec", issue["interpretation"]["tracks"][issue["tracks"][0]]["recording_id"])
        self.assertEqual("search-release", issue["interpretation"]["release_id"])
        self.assertIn("201", self.output.getvalue())
        self.assertIn("98", self.output.getvalue())

    def test_explicit_recording_id_is_hydrated_but_never_verified(self):
        issue = self.issue(candidates=[])
        self.providers["get_recording_details"].side_effect = None
        self.providers["get_recording_details"].return_value = recording("typed-rec")
        self.assertTrue(self.choose(issue, ["release", "offline", "fields", "My album", "My artist", "", "",
            "track 1", "id", "typed-rec", "Changed title", "Changed credit", "1", "1", "approve", "APPROVE"]))
        self.providers["get_recording_details"].assert_called_once_with("typed-rec")
        self.assertEqual("Changed credit", issue["interpretation"]["tracks"][issue["tracks"][0]]["artist"])
        self.assertFalse(issue["interpretation"]["independently_verified"])

    def test_release_candidates_display_reasons_missing_tracks_and_require_selection(self):
        evaluation = {"release_id": "release-a", "status": "partial", "missing": [release_track("rec-b", "2")],
            "review": [], "reasons": ["missing_release_tracks", "ambiguous_position_mapping"],
            "tracklist": [release_track(), release_track("rec-b", "2")], "matched": {}, "errors": []}
        issue = self.issue(evaluations=[evaluation])
        self.provide_release(evaluation["tracklist"])
        self.assertTrue(self.choose(issue, ["advanced", "release", "1", "fields", "", "", "", "",
            "track 1", "1", "", "", "1", "approve", "APPROVE"]))
        self.assertTrue(issue["interpretation"]["partial"])
        self.assertIn("missing_release_tracks", self.output.getvalue())
        self.assertIn("ambiguous_position_mapping", self.output.getvalue())
        self.assertIn("rec-b", self.output.getvalue())

    def test_provider_failures_allow_postpone_or_explicit_offline_fallback(self):
        issue = self.issue()
        self.providers["search_recording"].side_effect = TimeoutError("Synthetic recording timeout")
        self.providers["search_album"].side_effect = TimeoutError("Synthetic release timeout")
        self.assertTrue(self.choose(issue, ["track 1", "search", "Artist", "Title", "back",
            "release", "search", "Artist", "Album", "offline", "fields", "Local", "Artist", "", "",
            "track 1", "manual", "Local title", "Local credit", "1", "1", "approve", "APPROVE"]))
        self.assertIn("Synthetic recording timeout", self.output.getvalue())
        self.assertIn("Synthetic release timeout", self.output.getvalue())
        self.assertIsNone(issue["interpretation"]["release_id"])

    def test_release_failure_does_not_select_id_or_auto_map(self):
        issue = self.issue()
        self.providers["get_release_tracklist"].side_effect = TimeoutError("Synthetic full tracklist timeout")
        self.assertFalse(self.choose(issue, ["release", "id", "unavailable-release", "back", "postpone"]))
        self.assertNotIn("release_id", issue["interpretation"])
        self.assertEqual({}, issue["interpretation"]["tracks"])

    def test_duplicate_positions_and_invalid_numbers_are_reprompted(self):
        issue = self.issue([self.track(), self.track("Import/Album/02.flac")])
        self.assertTrue(self.choose(issue, ["release", "offline", "fields", "Local", "Artist", "", "",
            "track 1", "manual", "First", "Credit", "-1", "1", "bad", "1",
            "track 2", "manual", "Second", "Credit", "1", "1", "2", "approve", "APPROVE"]))
        self.assertEqual([1, 2], [t["number"] for t in issue["interpretation"]["tracks"].values()])
        self.assertIn("unique", self.output.getvalue().lower())

    def test_missing_inventory_cannot_be_approved(self):
        issue = self.issue([self.track(), self.track("Import/Album/02.flac")])
        answers = self.offline_answers()[:-1] + ["postpone"]
        self.assertFalse(self.choose(issue, answers))
        self.assertFalse(issue["interpretation"]["user_approved"])
        self.assertIn("inventory", self.output.getvalue().lower())

    def test_unsafe_text_and_invalid_date_are_reprompted_not_saved(self):
        issue = self.issue()
        self.assertTrue(self.choose(issue, ["release", "offline", "fields", "bad\x00album", "Local", "Artist",
            "2023-02-30", "2023-02-28", "Rock", "track 1", "manual", "", "Song", "Credit",
            "1", "1", "approve", "APPROVE"]))
        self.assertEqual("2023-02-28", issue["interpretation"]["date"])
        self.assertEqual("Rock", issue["interpretation"]["genre"])
        self.assertEqual("Song", issue["interpretation"]["tracks"][issue["tracks"][0]]["title"])

    def test_offline_totals_retention_is_an_explicit_choice_not_a_complete_release(self):
        issue = self.issue([self.track(track_total=("12",), disc_total=("2",))])
        answers = self.offline_answers()[:-2] + ["totals", "yes", "approve", "APPROVE"]
        self.assertTrue(self.choose(issue, answers))
        self.assertIs(issue["interpretation"]["retain_existing_totals"], True)
        self.assertTrue(issue["interpretation"]["partial"])
        self.assertIn("completeness", self.output.getvalue().lower())

    def test_multiple_triggers_share_one_draft_and_do_not_change_automatic_thresholds(self):
        issue = self.issue(evaluations=[{"release_id": "release-a", "status": "conflicting",
            "missing": [], "review": [], "reasons": ["local_track_total_conflict"], "tracklist": [], "matched": {}}])
        before = deepcopy(issue["details"])
        self.assertFalse(self.choose(issue, ["track 1", "1", "", "", "1", "1", "postpone"]))
        self.assertTrue(self.choose(issue, ["release", "offline", "fields", "Local", "Credit", "", "", "approve", "APPROVE"]))
        self.assertEqual(before, issue["details"])
        self.assertEqual(0.95, audio.MIN_RECORDING_SCORE)
        self.assertEqual(3, audio.MAX_DURATION_DIFFERENCE)
        self.assertFalse(issue["interpretation"]["independently_verified"])

    def test_stale_hash_and_malformed_drafts_do_not_mutate_saved_state(self):
        for broken in ({"status": "draft", "tracks": []},
                       {"status": "pending_application", "user_approved": False},
                       {"status": "draft", "source_fingerprint": OTHER_HASH, "tracks": {}}):
            issue = self.issue()
            issue["interpretation"] = broken
            before = deepcopy(issue)
            self.assertFalse(self.choose(issue, []))
            self.assertEqual(before, issue)
        issue = self.issue()
        issue["source_fingerprint"] = OTHER_HASH
        before = deepcopy(issue)
        self.assertFalse(self.choose(issue, []))
        self.assertEqual(before, issue)

    def test_outer_inventory_mismatch_and_changed_source_hash_reject_resumed_drafts(self):
        issue = self.issue()
        self.assertFalse(self.choose(issue, ["postpone"]))
        before = deepcopy(issue)
        self.assertFalse(self.choose(issue, [], fingerprint=OTHER_HASH))
        self.assertEqual(before, issue)
        issue["tracks"].append("Import/Album/not-a-source.flac")
        before = deepcopy(issue)
        self.assertFalse(self.choose(issue, []))
        self.assertEqual(before, issue)

    def test_save_callback_failure_is_not_misreported_as_persisted_approval(self):
        issue = self.issue()
        def save(value):
            if value["interpretation"]["status"] == "pending_application":
                raise OSError("Synthetic persistence failure")
        with self.assertRaisesRegex(OSError, "Synthetic persistence failure"):
            self.choose(issue, self.offline_answers(), save=save)
        self.assertEqual("draft", issue["interpretation"]["status"])
        self.assertFalse(issue["interpretation"]["user_approved"])

    def test_postpone_at_explicit_recording_id_prompt_is_not_swallowed_as_provider_error(self):
        issue = self.issue()
        self.assertFalse(self.choose(issue, ["track 1", "id", "postpone"]))
        self.assertEqual({}, issue["interpretation"]["tracks"])
        self.providers["get_recording_details"].assert_not_called()

    def test_malformed_nested_evidence_is_rejected_without_mutating_issue(self):
        cases = (("evidence", "fingerprint", []), ("evidence", "candidates", []),
                 ("evidence", "recordings", {"rec-a": []}),
                 ("assessment", "candidates", [None]),
                 ("assessment", "candidates", [{"recording_id": "rec-a", "details": []}]))
        for section, field, value in cases:
            with self.subTest(section=section, field=field):
                issue = self.issue()
                issue["details"]["tracks"][0][section][field] = value
                before = deepcopy(issue)
                self.assertFalse(self.choose(issue, []))
                self.assertEqual(before, issue)

    def test_bare_details_and_envelope_without_outer_inventory_are_supported(self):
        for bare in (False, True):
            with self.subTest(bare=bare):
                issue = self.issue()
                if bare:
                    issue = issue["details"]
                else:
                    issue.pop("tracks")
                self.assertTrue(self.choose(issue, self.offline_answers()))
                self.assertEqual("pending_application", issue["interpretation"]["status"])

    def test_reopening_pending_interpretation_requires_fresh_exact_approval(self):
        issue = self.issue()
        self.assertTrue(self.choose(issue, self.offline_answers()))
        choices = deepcopy(issue["interpretation"]["tracks"])
        self.assertFalse(self.choose(issue, ["postpone"]))
        self.assertEqual(choices, issue["interpretation"]["tracks"])
        self.assertEqual("draft", issue["interpretation"]["status"])
        self.assertFalse(issue["interpretation"]["user_approved"])


class PrepareInterpretationTests(ReviewCase):
    def test_offline_preparation_uses_explicit_mapping_and_never_fabricates_metadata(self):
        first, second = self.track(), self.track("Import/Album/02.flac")
        decision = self.decision([first, second])
        before = deepcopy(decision)
        result = review.prepare_interpretation(self.album([second, first]), decision, SOURCE_HASH)
        self.assertEqual({("1", "1"): first, ("1", "2"): second}, result["matched"])
        self.assertEqual({"album": "Chosen album", "album_artist": "Chosen artist"}, result["common"])
        self.assertEqual({}, result["manual_fields"])
        self.assertEqual([], result["compatible_ids"])
        self.assertTrue(result["partial"])
        self.assertEqual("user_approved", result["provenance"])
        self.assertEqual("user_supplied", result["release_tracks_provenance"])
        self.assertEqual(2, len(result["release_tracks"]))
        for identity in result["identities"].values():
            self.assertEqual("user-approved-local", identity["recording_id"])
            self.assertFalse(identity["verified"])
            self.assertTrue(identity["user_approved"])
            self.assertEqual(identity, tag.validate_recording_identity(identity))
        self.assertEqual(before, decision)
        self.assertTrue(all(not mock.called for mock in self.providers.values()))

    def test_selected_release_fetches_full_tracklist_preserves_missing_and_marks_partial(self):
        path = self.track()
        full = [release_track("rec-1"), release_track("rec-2", "2"), release_track("rec-3", "1", "2")]
        self.provide_release(full)
        decision = self.decision([path], release_id="release-a", partial=False)
        result = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.providers["get_release_tracklist"].assert_called_once_with("release-a", refresh=True)
        self.assertEqual(full, result["release_tracks"])
        self.assertEqual(full[1:], result["missing"])
        self.assertTrue(result["partial"])
        self.assertEqual([], result["compatible_ids"])
        self.assertEqual("user_approved", result["provenance"])
        self.assertFalse(result["identities"][str(path)]["verified"])

    def test_complete_release_mapping_is_not_order_dependent_or_automatically_remapped(self):
        first, second = self.track(), self.track("Import/Album/02.flac")
        full = [release_track("rec-2", "2"), release_track("rec-1")]
        self.provide_release(full)
        decision = self.decision([first, second], release_id="release-a")
        result = review.prepare_interpretation(self.album([second, first]), decision, SOURCE_HASH)
        self.assertFalse(result["partial"])
        self.assertEqual({("1", "1"): first, ("1", "2"): second}, result["matched"])
        self.assertEqual([], result["missing"])

    def test_repeated_recordings_require_explicit_distinct_positions(self):
        first, second = self.track(), self.track("Import/Album/02.flac")
        self.provide_release([release_track("same-rec"), release_track("same-rec", "2")])
        decision = self.decision([first, second], release_id="release-a")
        for track in decision["tracks"].values():
            track["recording_id"] = "same-rec"
        result = review.prepare_interpretation(self.album([first, second]), decision, SOURCE_HASH)
        self.assertEqual(2, len(result["matched"]))
        decision["tracks"][second.relative_to(self.stage).as_posix()]["number"] = 1
        with self.assertRaises(ValueError):
            review.prepare_interpretation(self.album([first, second]), decision, SOURCE_HASH)

    def test_release_position_and_recording_incompatibility_are_rejected_not_auto_mapped(self):
        path = self.track()
        self.provide_release([release_track("rec-1", "2")])
        decision = self.decision([path], release_id="release-a")
        with self.assertRaises(ValueError):
            review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        decision["tracks"][path.relative_to(self.stage).as_posix()]["number"] = 2
        decision["tracks"][path.relative_to(self.stage).as_posix()]["recording_id"] = "wrong-rec"
        with self.assertRaises(ValueError):
            review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)

    def test_unavailable_empty_malformed_and_incomplete_release_tracklists_are_rejected(self):
        path = self.track()
        decision = self.decision([path], release_id="release-a")
        incomplete = lookup.ReleaseTracklist()
        incomplete.extend([release_track("rec-1")])
        incomplete.complete = False
        for full in (None, [], [release_track("rec-1"), release_track("rec-2", "01")],
                     [{"disc": True, "number": "1", "recording_id": "rec-1"}], incomplete):
            with self.subTest(full=full):
                self.providers["get_release_tracklist"].side_effect = None
                self.providers["get_release_tracklist"].return_value = full
                with self.assertRaises(ValueError):
                    review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.providers["get_release_tracklist"].side_effect = TimeoutError("Synthetic unavailable release")
        with self.assertRaisesRegex(ValueError, "Synthetic unavailable release"):
            review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)

    def test_unapproved_draft_wrong_hash_and_false_verification_flags_are_rejected(self):
        path = self.track()
        for changes in ({"status": "draft"}, {"user_approved": False}, {"user_approved": 1},
                        {"independently_verified": True}, {"source_fingerprint": OTHER_HASH},
                        {"source_fingerprint": ""}, {"partial": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review.prepare_interpretation(self.album([path]), self.decision([path], **changes), SOURCE_HASH)

    def test_missing_extra_and_duplicate_inventory_paths_are_rejected(self):
        first, second = self.track(), self.track("Import/Album/02.flac")
        for paths, decision in (([first, second], self.decision([first])),
                                ([first], self.decision([first, second])),
                                ([first, first], self.decision([first])), ([], self.decision([first]))):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                review.prepare_interpretation({"tracks": [self.records[path] for path in paths]}, decision, SOURCE_HASH)

    def test_unsafe_relative_absolute_and_symlink_paths_are_rejected(self):
        path = self.track()
        decision = self.decision([path])
        value = next(iter(decision["tracks"].values()))
        for unsafe in ("../escape.flac", "/tmp/escape.flac", str(path), "Import/../Album/01.flac",
                       "Import\\Album\\01.flac", "Import/Album/01.flac\x00"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                review.prepare_interpretation(self.album([path]), {**decision, "tracks": {unsafe: value}}, SOURCE_HASH)
        outside = self.home / "outside.flac"
        outside.write_bytes(b"disposable outside target")
        link = self.stage / "Import/Album/link.flac"
        link.symlink_to(outside)
        self.records[link] = metadata_record(link)
        with self.assertRaises(ValueError):
            review.prepare_interpretation(self.album([link]), self.decision([link]), SOURCE_HASH)

    def test_removed_sources_and_changed_fingerprint_reject_old_approval(self):
        path = self.track()
        decision = self.decision([path])
        path.write_bytes(b"deliberately changed synthetic source")
        with self.assertRaises(ValueError):
            review.prepare_interpretation(self.album([path]), decision, OTHER_HASH)
        path.unlink()
        with self.assertRaises(ValueError):
            review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)

    def test_missing_unsafe_and_nonliteral_track_fields_are_rejected(self):
        path = self.track()
        decision = self.decision([path])
        key = path.relative_to(self.stage).as_posix()
        for field, bad in (("title", ""), ("artist", " \t"), ("title", "bad\x00title"),
                           ("artist", "bad\x85credit"), ("disc", True), ("number", "1"),
                           ("number", 0), ("recording_id", ""), ("recording_id", "bad\nid")):
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                broken = deepcopy(decision)
                broken["tracks"][key][field] = bad
                review.prepare_interpretation(self.album([path]), broken, SOURCE_HASH)
        for field in ("title", "artist", "disc", "number", "recording_id"):
            with self.subTest(missing=field), self.assertRaises(ValueError):
                broken = deepcopy(decision)
                del broken["tracks"][key][field]
                review.prepare_interpretation(self.album([path]), broken, SOURCE_HASH)

    def test_malformed_top_level_state_and_missing_album_fields_are_rejected(self):
        path = self.track()
        valid = self.decision([path])
        for decision in (None, [], {}, {**valid, "tracks": []}, {**valid, "album": ""},
                         {**valid, "album_artist": "bad\x00artist"}, {**valid, "release_id": 5},
                         {**valid, "retain_existing_totals": "yes"}, {**valid, "verified": True}):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        for missing in ("album", "album_artist", "release_id", "independently_verified", "partial"):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                broken = deepcopy(valid)
                del broken[missing]
                review.prepare_interpretation(self.album([path]), broken, SOURCE_HASH)

    def test_only_explicit_valid_date_and_genre_become_manual_fields(self):
        path = self.track(date=(), genre=())
        result = review.prepare_interpretation(self.album([path]),
            self.decision([path], date="2020-02-29", genre="Alternative"), SOURCE_HASH)
        self.assertEqual({"date": "2020-02-29", "genre": "Alternative"}, result["manual_fields"])
        for fields in ({"date": "2021-02-29"}, {"genre": ""}, {"date": None}, {"genre": "bad\x85genre"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                review.prepare_interpretation(self.album([path]), self.decision([path], **fields), SOURCE_HASH)

    def test_explicit_offline_tracklist_and_totals_policy_do_not_invent_release_identity(self):
        path = self.track(track_total=("12",), disc_total=("2",))
        full = [release_track(None), release_track(None, "2", title="User supplied missing song")]
        result = review.prepare_interpretation(self.album([path]), self.decision([path],
            release_tracks=full, retain_existing_totals=True), SOURCE_HASH)
        self.assertEqual(full, result["release_tracks"])
        self.assertEqual(full[1:], result["missing"])
        self.assertTrue(result["retain_existing_totals"])
        self.assertTrue(result["partial"])
        self.assertEqual([], result["compatible_ids"])
        self.assertNotIn("release_id", result["common"])
        self.assertNotIn("date", result["common"])
        self.assertEqual("user_supplied", result["release_tracks_provenance"])

    def test_real_synthetic_audio_cover_and_temporary_home_remain_untouched(self):
        path = make_audio(self.stage / "Import/Synthetic/song.flac", title="Synthetic title",
                          artist="Synthetic artist", album="Synthetic album")
        cover = path.parent / "cover.png"
        cover.write_bytes(make_cover()["data"])
        self.records[path] = self.reader(path)
        protected = {value: value.read_bytes() for value in (path, cover)}
        home_contents = sorted(self.home.rglob("*"))
        issue = self.issue([path])
        self.assertTrue(self.choose(issue, self.offline_answers()))
        result = review.prepare_interpretation(self.album([path]), issue["interpretation"], SOURCE_HASH)
        self.assertEqual({("1", "1"): path}, result["matched"])
        self.assertEqual(protected, {value: value.read_bytes() for value in protected})
        self.assertEqual(home_contents, sorted(self.home.rglob("*")))


class ReviewBoundaryTests(ReviewCase):
    def test_selecting_release_preserves_prior_automatic_evidence_and_saves_new_snapshot(self):
        evaluation = {"release_id": "release-a", "status": "conflicting", "missing": [], "review": [],
            "reasons": ["synthetic_prior_provider_error"], "tracklist": [release_track()], "matched": {},
            "errors": [{"stage": "musicbrainz", "message": "Synthetic earlier outage"}]}
        issue = self.issue(evaluations=[evaluation])
        before = deepcopy(issue["details"])
        full = [release_track(), release_track("other-rec", "2")]
        self.provide_release(full)
        self.assertTrue(self.choose(issue, ["release", "1", "fields", "", "", "", "",
            "track 1", "1", "", "", "1", "approve", "APPROVE"]))
        self.assertEqual(before, issue["details"])
        self.assertEqual(full, issue["interpretation"]["release_tracks"])
        self.assertTrue(issue["interpretation"]["partial"])

    def test_explicit_release_selection_does_not_append_unassessed_automatic_candidates(self):
        issue = self.issue()
        before = deepcopy(issue["details"])
        self.provide_release()
        self.assertFalse(self.choose(issue, ["release", "id", "release-a", "postpone"]))
        self.assertEqual(before, issue["details"])
        self.assertEqual([release_track()], issue["interpretation"]["release_tracks"])

    def test_online_snapshot_is_refetched_and_changed_coverage_requires_fresh_review(self):
        path = self.track()
        full = [release_track("rec-1")]
        decision = self.decision([path], release_id="release-a", release_tracks=full)
        self.provide_release(full)
        result = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertEqual(full, result["release_tracks"])
        self.providers["get_release_tracklist"].assert_called_once_with("release-a", refresh=True)
        self.providers["get_release_tracklist"].return_value = full + [release_track("new-rec", "2")]
        with self.assertRaisesRegex(ValueError, "[Tt]racklist changed|[Rr]elease.*changed"):
            review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)

    def test_switching_to_offline_drops_online_snapshot_without_relabeling_it_user_supplied(self):
        issue = self.issue()
        before = deepcopy(issue["details"])
        self.provide_release([release_track(), release_track("other-rec", "2")])
        self.assertTrue(self.choose(issue, ["release", "id", "release-a", "release", "offline",
            "fields", "Local", "Credit", "", "", "track 1", "manual", "Local song", "Local credit",
            "1", "1", "approve", "APPROVE"]))
        self.assertEqual(before, issue["details"])
        self.assertNotIn("release_tracks", issue["interpretation"])

    def test_interrupt_during_final_save_restores_draft_instead_of_resaving_pending_approval(self):
        issue = self.issue()
        def save(value):
            if value["interpretation"]["status"] == "pending_application":
                raise KeyboardInterrupt()
        self.assertFalse(self.choose(issue, self.offline_answers(), save=save))
        self.assertEqual("draft", issue["interpretation"]["status"])
        self.assertFalse(issue["interpretation"]["user_approved"])


class NumberingTotalsTests(ReviewCase):
    def test_partial_release_totals_cover_unimported_tracks_and_wholly_missing_discs(self):
        path = self.track()
        full = [release_track(f"disc1-{number}", str(number)) for number in range(1, 4)]
        full += [release_track("disc2-1", "1", "2"), release_track("rec-1", "2", "2")]
        self.provide_release(full)
        decision = self.decision([path], release_id="release-a", partial=True)
        decision["tracks"][path.relative_to(self.stage).as_posix()].update(disc=2, number=2)
        result = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertEqual({"disc_total": 2, "album_total": 5, "track_totals": {"1": 3, "2": 2}}, result["release_totals"])
        self.assertEqual({str(path): {"track_total": 2, "disc_total": 2}}, result["numbering_totals"])
        self.assertEqual({}, result["numbering_deferred"])
        self.assertEqual("release_tracklist", result["totals_provenance"])
        self.assertEqual(full[:-1], result["missing"])
        self.assertTrue(result["partial"])

    def test_offline_without_explicit_totals_defers_even_with_tags_and_user_tracklist(self):
        path = self.track(track_total=("12",), disc_total=("3",))
        decision = self.decision([path], release_tracks=[release_track(None)])
        result = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertIsNone(result["release_totals"])
        self.assertEqual({}, result["numbering_totals"])
        self.assertEqual({str(path): ["track_total", "disc_total"]}, result["numbering_deferred"])
        self.assertEqual("unavailable", result["totals_provenance"])
        self.assertEqual(1, len(result["release_tracks"]))

    def test_offline_explicit_totals_are_user_choices_not_local_counts_or_release_proof(self):
        path = self.track(track_total=(), disc_total=())
        relative = path.relative_to(self.stage).as_posix()
        decision = self.decision([path], totals={relative: {"track_total": 12, "disc_total": 3}})
        decision["tracks"][relative].update(disc=2, number=4)
        before = deepcopy(decision)
        result = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertEqual({str(path): {"track_total": 12, "disc_total": 3}}, result["numbering_totals"])
        self.assertEqual({}, result["numbering_deferred"])
        self.assertIsNone(result["release_totals"])
        self.assertEqual("user_approved", result["totals_provenance"])
        self.assertTrue(result["partial"])
        self.assertEqual(before, decision)
        self.assertEqual([], result["compatible_ids"])
        self.assertFalse(result["identities"][str(path)]["verified"])

    def test_offline_partly_known_totals_defer_only_missing_required_fields(self):
        first, second = self.track(), self.track("Import/Album/02.flac")
        relative = first.relative_to(self.stage).as_posix()
        decision = self.decision([first, second], totals={relative: {"track_total": 12}})
        result = review.prepare_interpretation(self.album([first, second]), decision, SOURCE_HASH)
        self.assertEqual({str(first): {"track_total": 12}}, result["numbering_totals"])
        self.assertEqual({str(first): ["disc_total"], str(second): ["track_total", "disc_total"]},
                         result["numbering_deferred"])

    def test_retaining_totals_requires_explicit_snapshot_and_current_metadata_agreement(self):
        path = self.track(track_number=("1/12",), track_total=("12",), disc_number=("1/3",), disc_total=("3",))
        relative = path.relative_to(self.stage).as_posix()
        decision = self.decision([path], retain_existing_totals=True,
                                 totals={relative: {"track_total": 12, "disc_total": 3}})
        result = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertEqual({str(path): {"track_total": 12, "disc_total": 3}}, result["numbering_totals"])
        self.assertEqual("existing_user_approved", result["totals_provenance"])
        self.records[path]["track_total"] = ("14",)
        with self.assertRaisesRegex(ValueError, "[Tt]otals.*changed|[Rr]etained.*changed"):
            review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)

    def test_legacy_retention_flag_alone_cannot_synthesize_totals_from_current_tags(self):
        path = self.track(track_total=("12",), disc_total=("3",))
        result = review.prepare_interpretation(self.album([path]),
            self.decision([path], retain_existing_totals=True), SOURCE_HASH)
        self.assertEqual({}, result["numbering_totals"])
        self.assertEqual({str(path): ["track_total", "disc_total"]}, result["numbering_deferred"])

    def test_malformed_unsafe_and_out_of_bounds_totals_are_rejected(self):
        path = self.track()
        relative = path.relative_to(self.stage).as_posix()
        bad_totals = ([], {relative: []}, {relative: {"track_total": True}}, {relative: {"disc_total": 0}},
                      {relative: {"track_total": "12"}}, {relative: {"album_total": 12}},
                      {"../outside.flac": {"track_total": 12}}, {str(path): {"track_total": 12}},
                      {"Import/Album/unknown.flac": {"track_total": 12}})
        for totals in bad_totals:
            with self.subTest(totals=totals), self.assertRaises(ValueError):
                review.prepare_interpretation(self.album([path]), self.decision([path], totals=totals), SOURCE_HASH)
        decision = self.decision([path], totals={relative: {"track_total": 1, "disc_total": 1}})
        decision["tracks"][relative].update(disc=2, number=2)
        with self.assertRaises(ValueError):
            review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)

    def test_conflicting_explicit_disc_or_same_disc_track_totals_need_fresh_choices(self):
        first, second = self.track(), self.track("Import/Album/02.flac")
        relatives = [path.relative_to(self.stage).as_posix() for path in (first, second)]
        for field in ("track_total", "disc_total"):
            totals = {relative: {"track_total": 12, "disc_total": 3} for relative in relatives}
            totals[relatives[1]][field] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                review.prepare_interpretation(self.album([first, second]),
                    self.decision([first, second], totals=totals), SOURCE_HASH)

    def test_online_totals_cannot_be_overridden_by_user_supplied_local_counts(self):
        path = self.track()
        self.provide_release([release_track("rec-1"), release_track("other-rec", "2")])
        decision = self.decision([path], release_id="release-a", totals={
            path.relative_to(self.stage).as_posix(): {"track_total": 1, "disc_total": 1}})
        with self.assertRaises(ValueError):
            review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)

    def test_unusual_full_release_positions_defer_incompatible_totals(self):
        path = self.track()
        self.provide_release([release_track("rec-1", "3", "2")])
        decision = self.decision([path], release_id="release-a")
        decision["tracks"][path.relative_to(self.stage).as_posix()].update(disc=2, number=3)
        result = review.prepare_interpretation(self.album([path]), decision, SOURCE_HASH)
        self.assertEqual({}, result["numbering_totals"])
        self.assertEqual({str(path): ["track_total", "disc_total"]}, result["numbering_deferred"])

    def test_ui_enter_totals_saves_values_and_shows_them_before_exact_approval(self):
        path = self.track(track_total=(), disc_total=())
        issue = self.issue([path])
        saved = []
        answers = self.offline_answers()[:-2] + ["totals", "enter", "12", "3", "approve", "APPROVE"]
        self.assertTrue(self.choose(issue, answers, save=lambda value: saved.append(deepcopy(value))))
        self.assertEqual({issue["tracks"][0]: {"track_total": 12, "disc_total": 3}}, issue["interpretation"]["totals"])
        self.assertFalse(issue["interpretation"]["retain_existing_totals"])
        self.assertIn("12", self.output.getvalue())
        self.assertIn("3", self.output.getvalue())
        self.assertEqual("pending_application", saved[-1]["interpretation"]["status"])
        result = review.prepare_interpretation(self.album([path]), issue["interpretation"], SOURCE_HASH)
        self.assertEqual({str(path): {"track_total": 12, "disc_total": 3}}, result["numbering_totals"])

    def test_ui_retain_snapshots_only_unambiguous_known_totals_not_mapping_counts(self):
        path = self.track(track_number=("1/12",), disc_number=("1/3",), track_total=(), disc_total=())
        issue = self.issue([path])
        answers = self.offline_answers()[:-2] + ["totals", "retain", "approve", "APPROVE"]
        self.assertTrue(self.choose(issue, answers))
        self.assertEqual({issue["tracks"][0]: {"track_total": 12, "disc_total": 3}}, issue["interpretation"]["totals"])
        result = review.prepare_interpretation(self.album([path]), issue["interpretation"], SOURCE_HASH)
        self.assertEqual({}, result["numbering_deferred"])

    def test_ui_conflicting_or_invalid_existing_totals_remain_deferred(self):
        for fields in ({"track_number": ("1/12",), "track_total": ("14",), "disc_total": ("3",)},
                       {"track_total": ("12", "bad"), "disc_total": ("3",)}):
            with self.subTest(fields=fields):
                path = self.track(**fields)
                issue = self.issue([path])
                answers = self.offline_answers()[:-2] + ["totals", "retain", "approve", "APPROVE"]
                self.assertTrue(self.choose(issue, answers))
                self.assertEqual({issue["tracks"][0]: {"disc_total": 3}}, issue["interpretation"]["totals"])
                result = review.prepare_interpretation(self.album([path]), issue["interpretation"], SOURCE_HASH)
                self.assertEqual({str(path): ["track_total"]}, result["numbering_deferred"])

    def test_ui_postpone_mid_totals_preserves_incremental_values_and_later_defers_unknowns(self):
        path = self.track()
        issue = self.issue([path])
        answers = self.offline_answers()[:-2] + ["totals", "enter", "12", "postpone"]
        self.assertFalse(self.choose(issue, answers))
        self.assertEqual({issue["tracks"][0]: {"track_total": 12}}, issue["interpretation"]["totals"])
        self.assertTrue(self.choose(issue, ["approve", "APPROVE"]))
        result = review.prepare_interpretation(self.album([path]), issue["interpretation"], SOURCE_HASH)
        self.assertEqual({str(path): ["disc_total"]}, result["numbering_deferred"])

    def test_ui_defer_removes_prior_totals_without_inventing_new_ones(self):
        path = self.track()
        issue = self.issue([path])
        answers = self.offline_answers()[:-2] + ["totals", "enter", "12", "3", "totals", "defer", "approve", "APPROVE"]
        self.assertTrue(self.choose(issue, answers))
        self.assertNotIn("totals", issue["interpretation"])
        result = review.prepare_interpretation(self.album([path]), issue["interpretation"], SOURCE_HASH)
        self.assertEqual({}, result["numbering_totals"])
        self.assertEqual({str(path): ["track_total", "disc_total"]}, result["numbering_deferred"])
