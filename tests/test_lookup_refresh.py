"""Real lookup cache + manual review, with only the MB transport replaced.

Run through tests/run_isolated.py only. Audio, managed staging/provenance,
metadata reads, source fingerprints and review planning are actual fixtures.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import lookup
import mixie
import review
import sort
import tag
from support import WorkspaceCase, make_audio


class LookupRefreshTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.clear_caches()
        self.addCleanup(self.clear_caches)
        self.responses = {"release-a": self.payload("release-a"), "release-other": self.payload("release-other")}
        self.calls = []
        self.transport = self.stack.enter_context(patch.object(lookup, "_musicbrainz_call", side_effect=self.respond))
        self.original = make_audio(self.inbox / "Delivery/Album/01.flac", title="Song", artist="Artist",
                                   album="Album", albumartist="Artist", tracknumber=1, discnumber=1,
                                   date="2020", genre="Rock")
        with redirect_stdout(StringIO()):
            stages = sort.sort_inbox()
        self.assertEqual(len(stages), 1)
        self.stage_root = Path(stages[0])
        self.assertTrue(sort.is_committed_stage(self.stage_root))
        self.path = self.stage_root / "Album/01.flac"
        metadata = tag.read_track_metadata(self.path)
        self.assertIsNotNone(metadata)
        self.album = {"album": "Album", "folder": self.path.parent, "tracks": [metadata]}
        details = review.issue_details([{"path": self.path, "status": "needs_review", "details": None,
                                         "reason": "Synthetic manual-release choice"}], [], "Choose an interpretation")
        self.issue = {"import_name": self.stage_root.name, "album": "Album", "details": details,
                      "tracks": [self.path.relative_to(self.stage).as_posix()]}
        self.fingerprint = mixie.review_fingerprint(self.issue)
        self.draft = {"status": "draft", "user_approved": False, "independently_verified": False,
                      "source_fingerprint": self.fingerprint, "partial": True,
                      "album": "Album", "album_artist": "Artist", "tracks": {
                          self.issue["tracks"][0]: {"recording_id": "rec-a", "title": "Song", "artist": "Artist",
                                                    "disc": 1, "number": 1}}}
        self.original_bytes = {path: path.read_bytes() for path in (
            self.original, self.path, sort._provenance_path(self.stage_root.name))}
        self.addCleanup(self.assert_sources_unchanged)

    def clear_caches(self):
        for function in (lookup.get_release_tracklist, lookup.get_release_metadata, lookup.get_recording_details):
            function.cache_clear()

    def assert_sources_unchanged(self):
        for path, data in self.original_bytes.items():
            self.assertEqual(path.read_bytes(), data, f"Review/cache operation changed {path}")
        self.assertEqual(mixie.review_fingerprint(self.issue), self.fingerprint)

    @staticmethod
    def payload(identifier, recordings=("rec-a",), *, track_count=None, medium_count=1):
        rows = [{"id": f"track-{identifier}-{number}", "position": str(number), "number": str(number),
                 "title": "Song" if number == 1 else f"Song {number}", "length": "150",
                 "recording": {"id": recording, "title": "Song", "length": "150"}}
                for number, recording in enumerate(recordings, 1)]
        return {"release": {"id": identifier, "title": "Album", "artist-credit-phrase": "Artist", "date": "2020",
                            "medium-count": medium_count, "medium-list": [{
                                "position": "1", "format": "CD", "track-count": len(rows) if track_count is None else track_count,
                                "track-list": rows}]}}

    def respond(self, provider, identifier, **options):
        self.calls.append((provider.__name__, identifier, deepcopy(options)))
        if provider is lookup.musicbrainzngs.get_release_by_id and identifier in self.responses:
            self.assertIn(options, ({"includes": ["recordings"]}, {"includes": ["artists", "release-groups"]}))
            response = self.responses[identifier]
            if isinstance(response, Exception):
                raise response
            return deepcopy(response)
        if provider is lookup.musicbrainzngs.get_recording_by_id and identifier == "rec-a":
            self.assertEqual(options, {"includes": ["artist-credits", "releases"]})
            return {"recording": {"id": identifier, "title": "Song", "artist-credit-phrase": "Artist",
                                  "release-count": 1, "release-list": [{"id": "release-a"}]}}
        raise AssertionError(f"Unexpected MusicBrainz transport request: {provider.__name__} {identifier} {options}")

    def tracklist_calls(self, identifier="release-a"):
        return [call for call in self.calls if call == ("get_release_by_id", identifier, {"includes": ["recordings"]})]

    def choose(self, answers):
        self.issue.update(source_fingerprint=self.fingerprint, interpretation=deepcopy(self.draft))
        answers = iter(answers)
        def respond(prompt):
            try:
                return next(answers)
            except StopIteration:
                raise AssertionError("Unexpected review prompt: " + prompt) from None
        with patch("builtins.input", side_effect=respond), redirect_stdout(StringIO()):
            return review.choose_interpretation(self.issue, mixie.review_fingerprint(self.issue))

    def select_snapshot_a(self):
        # A normal batch lookup genuinely populates the real cache before review selection.
        lookup.get_release_tracklist("release-a")
        self.assertTrue(self.choose(["release", "id", "release-a", "approve", "APPROVE"]))
        decision = self.issue["interpretation"]
        self.assertEqual(decision["status"], "pending_application")
        self.assertFalse(decision["independently_verified"])
        self.assertEqual(decision["release_tracks"][0]["recording_id"], "rec-a")
        return decision

    def test_default_batch_cache_keeps_defensive_snapshots_and_cache_api(self):
        first = lookup.get_release_tracklist("release-a")
        self.assertIsInstance(first, lookup.ReleaseTracklist)
        self.assertTrue(first.complete)
        first[0]["title"] = "Caller mutation"
        first.complete = False
        second = lookup.get_release_tracklist("release-a")
        self.assertEqual(second[0]["title"], "Song")
        self.assertTrue(second.complete)
        second.clear()
        self.assertEqual(len(lookup.get_release_tracklist("release-a")), 1)
        self.assertEqual(len(self.tracklist_calls()), 1)
        info = lookup.get_release_tracklist.cache_info()
        self.assertEqual((info.hits, info.misses, info.maxsize, info.currsize), (2, 1, 256, 1))
        lookup.get_release_tracklist.cache_clear()
        self.assertEqual(lookup.get_release_tracklist.cache_info().currsize, 0)
        lookup.get_release_tracklist("release-a")
        self.assertEqual(len(self.tracklist_calls()), 2)

    def test_refresh_bypasses_a_warm_snapshot_for_each_explicit_request(self):
        old = lookup.get_release_tracklist("release-a")
        self.responses["release-a"] = self.payload("release-a", ("rec-version-b",))
        fresh = lookup.get_release_tracklist("release-a", refresh=True)
        self.assertEqual(fresh[0]["recording_id"], "rec-version-b")
        self.assertEqual(old[0]["recording_id"], "rec-a")
        fresh[0]["recording_id"] = "Caller mutation"
        fresh.complete = False
        again = lookup.get_release_tracklist("release-a", refresh=True)
        self.assertEqual(again[0]["recording_id"], "rec-version-b")
        self.assertTrue(again.complete)
        self.assertEqual(len(self.tracklist_calls()), 3)

    def test_refresh_does_not_clear_unrelated_release_metadata_or_recording_caches(self):
        other = lookup.get_release_tracklist("release-other")
        metadata = lookup.get_release_metadata("release-a")
        recording = lookup.get_recording_details("rec-a")
        lookup.get_release_tracklist("release-a")
        lookup.get_release_tracklist("release-a", refresh=True)
        calls = deepcopy(self.calls)
        self.assertEqual(lookup.get_release_tracklist("release-other"), other)
        self.assertEqual(lookup.get_release_metadata("release-a"), metadata)
        self.assertEqual(lookup.get_recording_details("rec-a"), recording)
        self.assertEqual(self.calls, calls)

    def test_nonliteral_refresh_flags_are_rejected_before_transport(self):
        for flag in (None, 1, "true", [], {}):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                lookup.get_release_tracklist("release-a", refresh=flag)
        self.assertEqual(self.calls, [])
        lookup.get_release_tracklist("release-a", refresh=False)
        lookup.get_release_tracklist("release-a", refresh=False)
        self.assertEqual(len(self.tracklist_calls()), 1)

    def test_failed_fresh_request_never_returns_a_warm_success(self):
        lookup.get_release_tracklist("release-a")
        self.responses["release-a"] = TimeoutError("Synthetic fresh transport outage")
        with self.assertRaisesRegex(TimeoutError, "fresh transport outage"):
            lookup.get_release_tracklist("release-a", refresh=True)
        self.assertEqual(len(self.tracklist_calls()), 2)

    def test_fresh_tracklist_retains_false_remote_complete_before_normalization(self):
        lookup.get_release_tracklist("release-a")
        for counts in ({"track_count": 2}, {"medium_count": 2}):
            with self.subTest(counts=counts):
                self.responses["release-a"] = self.payload("release-a", **counts)
                fresh = lookup.get_release_tracklist("release-a", refresh=True)
                self.assertIsInstance(fresh, lookup.ReleaseTracklist)
                self.assertFalse(fresh.complete)
                self.assertEqual(len(fresh), 1)
                fresh.complete = True
                self.assertFalse(lookup.get_release_tracklist("release-a", refresh=True).complete)

    def test_real_selection_then_changed_recording_same_process_requires_fresh_review(self):
        decision = self.select_snapshot_a()
        saved = deepcopy(decision)
        before = len(self.tracklist_calls())
        self.responses["release-a"] = self.payload("release-a", ("rec-version-b",))
        with self.assertRaisesRegex(ValueError, "tracklist changed|Release.*changed"):
            review.prepare_interpretation(self.album, decision, mixie.review_fingerprint(self.issue))
        self.assertEqual(len(self.tracklist_calls()), before + 1)
        self.assertEqual(decision, saved)

    def test_real_selection_then_added_track_same_process_does_not_reuse_old_totals(self):
        decision = self.select_snapshot_a()
        saved = deepcopy(decision)
        self.responses["release-a"] = self.payload("release-a", ("rec-a", "rec-added"))
        with self.assertRaisesRegex(ValueError, "tracklist changed|Release.*changed"):
            review.prepare_interpretation(self.album, decision, mixie.review_fingerprint(self.issue))
        self.assertEqual(decision, saved)

    def test_selection_itself_bypasses_an_older_batch_snapshot(self):
        lookup.get_release_tracklist("release-a")
        self.responses["release-a"] = self.payload("release-a", ("rec-version-b",))
        self.assertFalse(self.choose(["release", "id", "release-a", "postpone"]))
        draft = self.issue["interpretation"]
        self.assertEqual(draft["release_tracks"][0]["recording_id"], "rec-version-b")
        # Choosing a release must not silently remap the existing recording choice.
        self.assertEqual(draft["tracks"][self.issue["tracks"][0]]["recording_id"], "rec-a")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(len(self.tracklist_calls()), 2)

    def test_incomplete_fresh_selection_cannot_be_normalized_into_an_approved_release(self):
        lookup.get_release_tracklist("release-a")
        self.responses["release-a"] = self.payload("release-a", track_count=2)
        self.assertFalse(self.choose(["release", "id", "release-a", "back", "postpone"]))
        self.assertNotIn("release_id", self.issue["interpretation"])
        self.assertNotIn("release_tracks", self.issue["interpretation"])
        self.assertIs(self.issue["interpretation"]["user_approved"], False)

    def test_incomplete_fresh_prepare_is_rejected_even_when_every_normalized_row_matches(self):
        decision = self.select_snapshot_a()
        saved = deepcopy(decision)
        for counts in ({"track_count": 2}, {"medium_count": 2}):
            with self.subTest(counts=counts):
                self.responses["release-a"] = self.payload("release-a", **counts)
                with self.assertRaisesRegex(ValueError, "complete"):
                    review.prepare_interpretation(self.album, decision, mixie.review_fingerprint(self.issue))
                self.assertEqual(decision, saved)

    def test_incomplete_fresh_prepare_without_saved_snapshot_cannot_claim_complete(self):
        decision = deepcopy(self.select_snapshot_a())
        decision.pop("release_tracks")
        self.responses["release-a"] = self.payload("release-a", medium_count=2)
        with self.assertRaisesRegex(ValueError, "complete"):
            review.prepare_interpretation(self.album, decision, mixie.review_fingerprint(self.issue))

    def test_prepare_outage_never_falls_back_to_the_selected_cached_snapshot(self):
        decision = self.select_snapshot_a()
        self.responses["release-a"] = TimeoutError("Synthetic revalidation outage")
        with self.assertRaisesRegex(ValueError, "revalidation outage"):
            review.prepare_interpretation(self.album, decision, mixie.review_fingerprint(self.issue))

    def test_offline_preparation_still_makes_no_provider_request(self):
        decision = {**deepcopy(self.draft), "status": "pending_application", "user_approved": True,
                    "release_id": None, "totals": {self.issue["tracks"][0]: {"track_total": 1, "disc_total": 1}}}
        decision["tracks"][self.issue["tracks"][0]]["recording_id"] = None
        result = review.prepare_interpretation(self.album, decision, mixie.review_fingerprint(self.issue))
        self.assertEqual(result["numbering_deferred"], {})
        self.assertIsNone(result["release_totals"])
        self.assertEqual(self.calls, [])
