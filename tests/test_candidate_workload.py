"""Offline synthetic workload regressions; no captured provider or music data.

Run only with: .venv/bin/python -I -B tests/run_isolated.py discover -s tests
              -p test_candidate_workload.py -v
"""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import call, patch
from urllib.error import HTTPError

import audio
import lookup


FINGERPRINT = {"duration": 200, "fingerprint": "SYNTHETIC-WORKLOAD-FINGERPRINT"}


def match(*identifiers, score=0.99):
    return {"id": "synthetic-acoustid", "score": score,
            "recordings": [{"id": identifier} for identifier in identifiers]}


def recording(identifier, *, length=200000, title="Synthetic provider title"):
    details = {"id": identifier, "title": title,
               "release-list": [{"id": "synthetic-release"}], "release-list-complete": True}
    if length is not None:
        details["length"] = length
    return details


class CandidateWorkloadCase(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.acoustid = self.stack.enter_context(patch.object(audio, "identify_audio"))
        self.recordings = self.stack.enter_context(patch.object(lookup, "get_recording_details"))
        for module, name in ((audio, "urlopen"), (lookup, "urlopen"),
                             (lookup.musicbrainzngs, "get_recording_by_id"),
                             (lookup.musicbrainzngs, "browse_releases")):
            self.stack.enter_context(patch.object(
                module, name, side_effect=AssertionError("Unexpected real provider access")))

    def provide(self, matches, recordings):
        self.acoustid.reset_mock()
        self.acoustid.side_effect = None
        self.acoustid.return_value = deepcopy(matches)
        self.recordings.reset_mock()

        def fetch(identifier):
            self.assertIn(identifier, recordings, "Hydrated a score-ineligible or unexpected candidate")
            value = recordings[identifier]
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)

        self.recordings.side_effect = fetch

    def collect(self, matches, recordings, **kwargs):
        self.provide(matches, recordings)
        return audio.collect_recording_evidence(FINGERPRINT, "synthetic-offline-key", **kwargs)

    def assess(self, evidence, **kwargs):
        return audio.assess_recording_candidates(
            evidence["fingerprint"], evidence["candidates"], evidence["recordings"],
            errors=evidence["errors"], complete=evidence["complete"], **kwargs)

    def assert_complete(self, evidence):
        self.assertEqual([], evidence["errors"])
        self.assertTrue(evidence["complete"])
        self.assertEqual("complete", evidence["lookup_state"])
        self.assertFalse(evidence["retryable"])

    def assert_skips(self, evidence, identifiers):
        expected = {identifier: "low_confidence" for identifier in identifiers}
        self.assertEqual(expected, evidence.get("hydration_skips", {}))
        rows = {row["recording_id"]: row for row in self.assess(evidence)["candidates"]}
        for identifier in identifiers:
            self.assertIn(identifier, evidence["candidates"])
            self.assertNotIn(identifier, evidence["recordings"])
            self.assertEqual(evidence["candidates"][identifier], rows[identifier]["score"])
            self.assertIn("low_confidence", rows[identifier]["reasons"])
            self.assertEqual("probable", rows[identifier]["status"])
            self.assertIsNone(rows[identifier]["details"])

    def test_exact_score_floor_is_hydrated(self):
        self.assertEqual(0.95, audio.MIN_RECORDING_SCORE)
        for score in (0.95, "0.95"):
            with self.subTest(score=score):
                evidence = self.collect([match("eligible", score=score)],
                                        {"eligible": recording("eligible")})
                self.assertEqual([call("eligible")], self.recordings.call_args_list)
                self.assertEqual({}, evidence.get("hydration_skips", {}))
                self.assert_complete(evidence)
                self.assertEqual("verified", self.assess(evidence)["status"])

    def test_strictly_below_score_floor_is_not_hydrated(self):
        for score in (0.949999, "0.949999", 0.0):
            with self.subTest(score=score):
                evidence = self.collect([match("low", score=score)], {})
                self.recordings.assert_not_called()
                self.assertEqual({"low": float(score)}, evidence["candidates"])
                self.assert_skips(evidence, ["low"])
                self.assert_complete(evidence)
                self.assertEqual("probable", self.assess(evidence)["status"])
                self.assertIsNone(self.assess(evidence)["details"])

    def test_thirty_low_scores_leave_all_twelve_eligible_slots_available(self):
        self.assertEqual(12, audio.MAX_RECORDING_LOOKUPS)
        eligible = {f"eligible-{index:02d}": recording(
            f"eligible-{index:02d}", length=200000 if index == 0 else 230000)
                    for index in range(12)}
        noise = [f"noise-{index:02d}" for index in range(30)]
        matches = ([match(identifier, score=0.1 + index / 100)
                    for index, identifier in enumerate(noise)]
                   + [match(identifier, score=0.95) for identifier in eligible])
        evidence = self.collect(matches, eligible)
        self.assertEqual([call(identifier) for identifier in eligible], self.recordings.call_args_list)
        self.assertEqual(42, len(evidence["matches"]))
        self.assertEqual(matches, evidence["matches"])
        self.assertEqual(42, len(evidence["candidates"]))
        self.assertEqual(12, len(evidence["recordings"]))
        self.assert_skips(evidence, noise)
        self.assert_complete(evidence)
        assessment = self.assess(evidence)
        self.assertEqual("verified", assessment["status"])
        self.assertEqual("eligible-00", assessment["details"]["id"])
        self.assertEqual(42, len(assessment["candidates"]))
        self.assertNotIn("candidate_budget_exhausted", assessment["reasons"])

    def test_low_scores_alone_do_not_exhaust_the_budget(self):
        noise = [f"noise-{index:02d}" for index in range(30)]
        matches = [match(*noise, score=0.949999)]
        evidence = self.collect(matches, {})
        self.recordings.assert_not_called()
        self.assertEqual(matches, evidence["matches"])
        self.assertEqual(30, len(evidence["candidates"]))
        self.assert_skips(evidence, noise)
        self.assert_complete(evidence)
        self.assertEqual("probable", self.assess(evidence)["status"])

    def test_thirteenth_eligible_candidate_still_exhausts_the_budget(self):
        eligible = {f"eligible-{index:02d}": recording(
            f"eligible-{index:02d}", length=200000 if index == 0 else 230000)
                    for index in range(13)}
        noise = [f"noise-{index:02d}" for index in range(30)]
        evidence = self.collect([match(*noise, score=0.2), match(*eligible)], eligible)
        self.assertEqual([call(identifier) for identifier in list(eligible)[:12]],
                         self.recordings.call_args_list)
        self.assertEqual(43, len(evidence["candidates"]))
        self.assertEqual(12, len(evidence["recordings"]))
        self.assert_skips(evidence, noise)
        self.assertNotIn("eligible-12", evidence["recordings"])
        self.assertFalse(evidence["complete"])
        self.assertEqual("partial", evidence["lookup_state"])
        self.assertFalse(evidence["retryable"])
        self.assertEqual(["candidate_budget_exhausted"],
                         [error["code"] for error in evidence["errors"]])
        assessment = self.assess(evidence)
        self.assertEqual("probable", assessment["status"])
        self.assertIn("candidate_budget_exhausted", assessment["reasons"])
        last = next(row for row in assessment["candidates"] if row["recording_id"] == "eligible-12")
        self.assertIn("recording_details_unavailable", last["reasons"])
        self.assertNotIn("low_confidence", last["reasons"])

    def test_custom_budget_counts_only_potentially_eligible_candidates(self):
        for eligible_count, complete in ((2, True), (3, False)):
            with self.subTest(eligible_count=eligible_count):
                eligible = {f"eligible-{index}": recording(f"eligible-{index}")
                            for index in range(eligible_count)}
                evidence = self.collect([match("noise", score=0.4), match(*eligible)],
                                        eligible, max_candidates=2)
                self.assertEqual([call("eligible-0"), call("eligible-1")],
                                 self.recordings.call_args_list)
                self.assert_skips(evidence, ["noise"])
                self.assertEqual(complete, evidence["complete"])
                self.assertEqual(not complete, any(error["code"] == "candidate_budget_exhausted"
                                                   for error in evidence["errors"]))

    def test_all_raw_matches_and_candidate_scores_survive_skipping(self):
        matches = [match("eligible", "low-a", score="0.2"),
                   match("low-b", score=0.949999), match("eligible", score=0.95),
                   match("low-a", score=0.3)]
        matches[0]["source-label"] = "Untrusted filename/title, not identity authority"
        matches[1]["recordings"][0]["title"] = "Raw provider text must survive"
        original = deepcopy(matches)
        evidence = self.collect(matches, {"eligible": recording("eligible")})
        self.assertEqual([call("eligible")], self.recordings.call_args_list)
        self.assertEqual(original, matches)
        self.assertEqual(original, evidence["matches"])
        self.assertEqual({"eligible": 0.95, "low-a": 0.3, "low-b": 0.949999}, evidence["candidates"])
        self.assertEqual(audio.collect_recording_candidates(original), evidence["candidates"])
        self.assert_skips(evidence, ["low-a", "low-b"])
        self.assert_complete(evidence)
        matches[0]["recordings"].clear()
        self.acoustid.return_value[1]["recordings"].clear()
        self.assertEqual(original, evidence["matches"])

    def test_duplicate_high_score_is_not_hidden_by_low_score_rows(self):
        for scores in ((0.2, 0.95), (0.95, 0.2)):
            with self.subTest(scores=scores):
                matches = [match("same-recording", score=score) for score in scores]
                evidence = self.collect(matches, {"same-recording": recording("same-recording")})
                self.assertEqual([call("same-recording")], self.recordings.call_args_list)
                self.assertEqual({"same-recording": 0.95}, evidence["candidates"])
                self.assertEqual({}, evidence.get("hydration_skips", {}))
                self.assertEqual(matches, evidence["matches"])
                self.assert_complete(evidence)
                self.assertEqual("verified", self.assess(evidence)["status"])

    def test_multiple_high_confidence_candidates_remain_ambiguous(self):
        matches = [match("higher", score=1.0), match("floor", score=0.95), match("noise", score=0.1)]
        evidence = self.collect(matches, {identifier: recording(identifier) for identifier in ("higher", "floor")})
        self.assertEqual([call("higher"), call("floor")], self.recordings.call_args_list)
        self.assert_skips(evidence, ["noise"])
        self.assert_complete(evidence)
        assessment = self.assess(evidence)
        self.assertEqual("conflicting", assessment["status"])
        self.assertIsNone(assessment["details"])
        self.assertIn("multiple_eligible_recordings", assessment["reasons"])

    def test_high_confidence_competitor_outage_remains_unverified(self):
        http_error = HTTPError("https://synthetic.invalid/recording", 503, "Synthetic outage", {}, None)
        self.addCleanup(http_error.close)
        failure = lookup.musicbrainzngs.NetworkError("retried 1 times", http_error)
        evidence = self.collect([match("chosen", score=1.0), match("competitor", score=0.95),
                                 match("noise", score=0.2)],
                                {"chosen": recording("chosen"), "competitor": failure})
        self.assertEqual([call("chosen"), call("competitor")], self.recordings.call_args_list)
        self.assert_skips(evidence, ["noise"])
        self.assertFalse(evidence["complete"])
        self.assertEqual("partial", evidence["lookup_state"])
        self.assertTrue(evidence["retryable"])
        self.assertEqual(1, len(evidence["errors"]))
        error = evidence["errors"][0]
        self.assertEqual("competitor", error["recording_id"])
        self.assertEqual("musicbrainz", error["stage"])
        self.assertEqual(503, error["http_status"])
        self.assertTrue(error["retryable"])
        assessment = self.assess(evidence)
        self.assertEqual("probable", assessment["status"])
        self.assertEqual("chosen", assessment["details"]["id"], "Details remain tentative, not authority")
        self.assertFalse(assessment["complete"])
        self.assertTrue(assessment["retryable"])
        self.assertIn("retryable_provider_error", assessment["reasons"])
        self.assertNotIn("multiple_eligible_recordings", assessment["reasons"])

    def test_high_confidence_competitor_missing_duration_remains_undecided(self):
        evidence = self.collect([match("chosen", "competitor", score=0.95), match("noise", score=0.1)],
                                {"chosen": recording("chosen"), "competitor": recording("competitor", length=None)})
        self.assertEqual([call("chosen"), call("competitor")], self.recordings.call_args_list)
        self.assert_skips(evidence, ["noise"])
        self.assert_complete(evidence)
        assessment = self.assess(evidence)
        self.assertEqual("probable", assessment["status"])
        self.assertEqual("chosen", assessment["details"]["id"])
        self.assertIn("recording_duration_unavailable", assessment["reasons"])

    def test_three_second_duration_gate_is_unchanged(self):
        self.assertEqual(3, audio.MAX_DURATION_DIFFERENCE)
        for length, status in ((203000, "verified"), (197000, "verified"),
                               (203001, "conflicting"), (196999, "conflicting")):
            with self.subTest(length=length):
                evidence = self.collect([match("eligible", score=0.95), match("noise", score=0.949999)],
                                        {"eligible": recording("eligible", length=length)})
                self.assertEqual([call("eligible")], self.recordings.call_args_list)
                self.assert_skips(evidence, ["noise"])
                self.assert_complete(evidence)
                assessment = self.assess(evidence)
                eligible = next(row for row in assessment["candidates"] if row["recording_id"] == "eligible")
                self.assertEqual(status, eligible["status"])
                self.assertEqual("verified" if status == "verified" else "probable", assessment["status"])
                self.assertEqual(status == "conflicting", "duration_conflict" in eligible["reasons"])

    def test_unknown_and_invalid_assessment_scores_are_not_eliminated(self):
        for score in (None, "invalid", float("nan"), float("inf"), -0.1, 1.01, True, [], {}):
            with self.subTest(score=score):
                assessment = audio.assess_recording_candidates(
                    FINGERPRINT, {"chosen": 0.99, "unknown": score},
                    {"chosen": recording("chosen"), "unknown": recording("unknown")})
                self.assertEqual("probable", assessment["status"])
                self.assertEqual("chosen", assessment["details"]["id"])
                unknown = next(row for row in assessment["candidates"] if row["recording_id"] == "unknown")
                self.assertIsNone(unknown["score"])
                self.assertEqual("probable", unknown["status"])
                self.assertIn("invalid_score", unknown["reasons"])
                self.assertNotIn("low_confidence", unknown["reasons"])

    def test_malformed_score_rows_remain_errors_not_optimization_skips(self):
        for score in (None, "invalid", float("nan"), float("inf"), -0.1, 1.01, True, [], {}):
            with self.subTest(score=score):
                matches = [match("chosen"), match("unknown", score=score), match("noise", score=0.1)]
                evidence = self.collect(matches, {"chosen": recording("chosen")})
                self.assertEqual([call("chosen")], self.recordings.call_args_list)
                self.assertEqual(matches, evidence["matches"])
                self.assertEqual({"chosen": 0.99, "noise": 0.1}, evidence["candidates"],
                                 "Keep the existing malformed-score parser contract")
                self.assert_skips(evidence, ["noise"])
                self.assertNotIn("unknown", evidence["hydration_skips"])
                self.assertFalse(evidence["complete"])
                self.assertFalse(evidence["retryable"])
                self.assertEqual(["malformed_acoustid_response"],
                                 [error["code"] for error in evidence["errors"]])
                self.assertEqual("probable", self.assess(evidence)["status"])

    def test_malformed_payloads_never_verify(self):
        for matches in (None, {}, [None], [match("chosen"), None],
                        [match("chosen"), {"score": 0.99, "recordings": {}}],
                        [match("chosen"), {"score": 0.99, "recordings": [None]}]):
            with self.subTest(matches=matches):
                evidence = self.collect(matches, {"chosen": recording("chosen")})
                self.assertEqual(matches, evidence["matches"])
                self.assertEqual({}, evidence["hydration_skips"])
                self.assertTrue(evidence["errors"])
                self.assertFalse(evidence["complete"])
                self.assertNotEqual("verified", self.assess(evidence)["status"])

    def test_malformed_competitor_details_never_verify(self):
        for details in (None, [], {"id": "wrong-id", "length": 200000}):
            with self.subTest(details=details):
                evidence = self.collect([match("chosen", "competitor"), match("noise", score=0.1)],
                                        {"chosen": recording("chosen"), "competitor": details})
                self.assertEqual([call("chosen"), call("competitor")], self.recordings.call_args_list)
                self.assert_skips(evidence, ["noise"])
                self.assertFalse(evidence["complete"])
                self.assertFalse(evidence["retryable"])
                self.assertEqual(1, len(evidence["errors"]))
                self.assertEqual("competitor", evidence["errors"][0]["recording_id"])
                self.assertEqual("probable", self.assess(evidence)["status"])

    def test_malformed_recording_lengths_never_verify(self):
        for length in (None, "invalid", float("nan"), float("inf"), -1, 0, True, [], {}):
            with self.subTest(length=length):
                evidence = self.collect([match("chosen"), match("noise", score=0.1)],
                                        {"chosen": recording("chosen", length=length)})
                self.assertEqual([call("chosen")], self.recordings.call_args_list)
                self.assert_skips(evidence, ["noise"])
                assessment = self.assess(evidence)
                self.assertEqual("probable", assessment["status"])
                self.assertIn("recording_duration_unavailable", assessment["reasons"])

    def test_source_text_neither_promotes_noise_nor_vetoes_audio(self):
        matches = [match("audio-id", score=0.95), match("source-named-id", score=0.949999)]
        matches[0]["recordings"][0]["title"] = "Does not match the claimed source title"
        matches[1]["recordings"][0]["title"] = "Exactly matches the claimed source title"
        evidence = self.collect(matches, {"audio-id": recording("audio-id", title="Different provider title")})
        self.assertEqual([call("audio-id")], self.recordings.call_args_list)
        self.assertEqual(matches, evidence["matches"])
        self.assert_skips(evidence, ["source-named-id"])
        self.assert_complete(evidence)
        assessment = self.assess(evidence)
        self.assertEqual("verified", assessment["status"])
        self.assertEqual("audio-id", assessment["details"]["id"])

    def test_legacy_tuple_keeps_raw_low_scores_without_hydrating_them(self):
        matches = [match("chosen", score=0.95), match("noise", score=0.949999)]
        self.provide(matches, {"chosen": recording("chosen")})
        raw, scores, details = audio.identify_recording(FINGERPRINT, "synthetic-offline-key")
        self.assertEqual([call("chosen")], self.recordings.call_args_list)
        self.assertEqual(matches, raw)
        self.assertEqual({"chosen": 0.95, "noise": 0.949999}, scores)
        self.assertEqual("chosen", details["id"])

    def test_skipped_evidence_is_cached_and_returned_defensively(self):
        self.provide([match("chosen"), match("noise", score=0.1)], {"chosen": recording("chosen")})
        inspector = self.stack.enter_context(patch.object(
            audio, "inspect_audio", return_value=(deepcopy(FINGERPRINT), None)))
        self.stack.enter_context(patch.object(audio, "_file_revision", return_value=(1, 2, 3, 4, 5)))
        cache = {}
        path = Path("synthetic-source-label.flac")
        first = audio.audit_tracks([path], "synthetic-offline-key", cache=cache, quiet=True)[0]
        self.assertEqual("preliminary_match", first["status"])
        self.assert_skips(first["evidence"], ["noise"])
        first["evidence"]["hydration_skips"].clear()
        first["evidence"]["matches"].clear()
        first["assessment"]["candidates"].clear()
        second = audio.audit_tracks([path], "synthetic-offline-key", cache=cache, quiet=True)[0]
        self.assertEqual("preliminary_match", second["status"])
        self.assert_skips(second["evidence"], ["noise"])
        self.assertEqual(2, len(second["evidence"]["matches"]))
        self.assertEqual(2, len(second["assessment"]["candidates"]))
        self.assertEqual(1, inspector.call_count)
        self.assertEqual(1, self.acoustid.call_count)
        self.assertEqual([call("chosen")], self.recordings.call_args_list)

    def test_acoustid_outage_makes_no_skip_decisions(self):
        self.acoustid.side_effect = TimeoutError("Synthetic AcoustID outage")
        evidence = audio.collect_recording_evidence(FINGERPRINT, "synthetic-offline-key")
        self.recordings.assert_not_called()
        self.assertEqual({}, evidence["hydration_skips"])
        self.assertEqual({}, evidence["candidates"])
        self.assertEqual([], evidence["matches"])
        self.assertFalse(evidence["complete"])
        self.assertEqual("unavailable", evidence["lookup_state"])
        self.assertTrue(evidence["retryable"])

    def test_inspection_failure_has_the_same_empty_skip_schema(self):
        result = audio._failed_audit(Path("synthetic-source.flac"), "inspection_error", ValueError("Synthetic failure"))
        self.assertEqual({}, result["evidence"]["hydration_skips"])
        self.assertNotEqual("preliminary_match", result["status"])
        self.assertFalse(result["assessment"]["complete"])
        self.acoustid.assert_not_called()
        self.recordings.assert_not_called()
