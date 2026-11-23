"""Synthetic identification regressions, NOT captured AcoustID/MusicBrainz responses.

Album labels come from stress_test.txt. IDs, durations and provider payloads below
are deliberately invented branch fixtures, not claims about the commercial audio.
Run only with: .venv/bin/python -I -B tests/run_isolated.py discover -s tests
              -p test_identification.py -v
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO

import subprocess

from unittest.mock import Mock, patch
from urllib.error import HTTPError

from support import WorkspaceCase, make_audio, metadata_record
import audio
import lookup


FINGERPRINT = {"duration": 200, "fingerprint": "SYNTHETIC-NOT-A-REAL-FINGERPRINT"}


def recording(identifier="rec-a", title="Synthetic song", length=200000,
              releases=("release-a",), artist="Synthetic artist"):
    result = {"id": identifier, "title": title, "artist-credit-phrase": artist,
              "artist-credit": [{"artist": {"id": "synthetic-artist", "name": artist}}],
              "release-list": [{"id": value} for value in releases],
              "release-list-complete": True}
    if length is not None:
        result["length"] = length
    return result


def acoustic(*ids, score=0.99):
    return [{"id": "synthetic-acoustid", "score": score,
             "recordings": [{"id": value} for value in ids]}]


def release_track(identifier="rec-a", number="1", disc="1", title="Synthetic song"):
    return {"disc": disc, "number": number, "title": title,
            "recording_id": identifier, "track_id": f"synthetic-{disc}-{number}"}


def fake_process(stdout="", stderr="", returncode=0):
    def run(arguments, **kwargs):
        for name, value in (("stdout", stdout), ("stderr", stderr)):
            stream = kwargs.get(name)
            if hasattr(stream, "write"):
                stream.write(value.encode("utf-8"))
        return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)
    return run


def accepted(path, details=None):
    return {"path": path, "status": "preliminary_match", "reason": "synthetic accepted anchor",
            "details": details or recording()}


class IdentificationCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.originals = {}
        self.unexpected = []
        self.addCleanup(self.assert_no_unexpected_providers)
        self.addCleanup(self.assert_sources_unchanged)
        self.stack.enter_context(patch.object(audio, "script_folder", self.script))
        self.stack.enter_context(patch.object(audio, "staging_folder", self.stage))
        self.stack.enter_context(patch.object(audio.time, "sleep"))
        self.inspector = self.stack.enter_context(patch.object(
            audio, "inspect_audio", return_value=(deepcopy(FINGERPRINT), None)))
        self.acoustid = self.stack.enter_context(patch.object(
            audio, "identify_audio", side_effect=self.denied("AcoustID")))
        self.recordings = self.stack.enter_context(patch.object(
            lookup, "get_recording_details", side_effect=self.denied("recording details")))
        self.tracklists = self.stack.enter_context(patch.object(
            lookup, "get_release_tracklist", side_effect=self.denied("release tracklist")))
        for module, name in ((lookup, "search_album"), (lookup, "search_recording"),
                             (audio, "urlopen"), (lookup, "urlopen")):
            self.stack.enter_context(patch.object(module, name, side_effect=self.denied(name)))
        for name in ("search_releases", "search_recordings", "get_release_by_id", "get_recording_by_id",
                     "browse_releases"):
            self.stack.enter_context(patch.object(
                lookup.musicbrainzngs, name, side_effect=self.denied(name)))

    def denied(self, name):
        def reject(*args, **kwargs):
            self.unexpected.append(name)
            raise AssertionError(f"Unexpected provider: {name}")
        return reject

    def assert_no_unexpected_providers(self):
        self.assertEqual([], self.unexpected)

    def assert_sources_unchanged(self):
        for path, digest in self.originals.items():
            self.assertEqual(digest, sha256(path.read_bytes()).hexdigest(), str(path))

    def track(self, name="Import/Album/track.flac"):
        path = self.track_path(name)
        self.originals[path] = sha256(path.read_bytes()).hexdigest()
        return path

    def provide(self, records, matches=None):
        self.acoustid.side_effect = None
        self.acoustid.return_value = deepcopy(matches if matches is not None else acoustic(*records))
        def fetch(identifier):
            if identifier not in records:
                return self.denied(f"recording {identifier}")()
            value = records[identifier]
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)
        self.recordings.side_effect = fetch

    def audit(self, path=None, **kwargs):
        return audio.audit_tracks([path or self.track()], "synthetic-key", quiet=True, **kwargs)[0]

    def assess(self, scores, records, **kwargs):
        return audio.assess_recording_candidates(FINGERPRINT, scores, records, **kwargs)

    def evaluate(self, audits, tracks, **kwargs):
        self.tracklists.side_effect = None
        self.tracklists.return_value = deepcopy(tracks)
        return lookup.evaluate_release_candidate(audits, "release-a", **kwargs)

    def test_rich_assessment_preserves_provenance(self):
        self.provide({"rec-a": recording()})
        result = self.audit()
        self.assertEqual("preliminary_match", result["status"])
        self.assertEqual("verified", result["assessment"]["status"])
        self.assertEqual(acoustic("rec-a"), result["evidence"]["matches"])
        candidate = result["assessment"]["candidates"][0]
        self.assertEqual("rec-a", candidate["recording_id"])
        self.assertEqual(0.99, candidate["score"])
        self.assertEqual(0, candidate["duration_difference"])

    def test_score_and_duration_floors_are_unchanged(self):
        for score, length, eligible in ((0.95, 203000, True), (0.949999, 200000, False),
                                        (0.95, 203001, False), (0.95, 197000, True)):
            with self.subTest(score=score, length=length):
                assessment = self.assess({"rec-a": score}, {"rec-a": recording(length=length)})
                self.assertEqual(eligible, assessment["status"] == "verified")
                self.assertEqual(eligible, audio.assess_recording(
                    FINGERPRINT, {"rec-a": score}, recording(length=length))[0])

    def test_candidate_counts_have_no_five_candidate_cliff(self):
        for count in (1, 2, 5, 6, 9):
            with self.subTest(count=count):
                records = {f"rec-{index}": recording(f"rec-{index}", length=200000 if index == 0 else 230000)
                           for index in range(count)}
                self.provide(records)
                result = self.audit(self.track(f"Counts/{count}/track.flac"))
                self.assertEqual("verified", result["assessment"]["status"])
                self.assertEqual(count, len(result["assessment"]["candidates"]))
                self.assertEqual("rec-0", result["details"]["id"])

    def test_low_score_duration_alternative_does_not_hide_evidence(self):
        records = {"rec-a": recording(), "rec-noise": recording("rec-noise")}
        assessment = self.assess({"rec-a": 0.99, "rec-noise": 0.1}, records)
        self.assertEqual("verified", assessment["status"])
        self.assertEqual(2, len(assessment["candidates"]))
        self.assertEqual("rec-a", assessment["details"]["id"])

    def test_multiple_eligible_recordings_are_conflicting_not_highest_wins(self):
        result = self.assess({"rec-a": 0.99, "rec-b": 0.98},
                             {"rec-a": recording(), "rec-b": recording("rec-b")})
        self.assertEqual("conflicting", result["status"])
        self.assertIsNone(result["details"])
        self.assertIn("multiple_eligible_recordings", result["reasons"])

    def test_probable_missing_duration_and_low_confidence(self):
        for score, length in ((0.99, None), (0.94, 200000)):
            with self.subTest(score=score, length=length):
                result = self.assess({"rec-a": score}, {"rec-a": recording(length=length)})
                self.assertEqual("probable", result["status"])

    def test_duration_disagreement_is_conflicting(self):
        result = self.assess({"rec-a": 0.99}, {"rec-a": recording(length=250000)})
        self.assertEqual("conflicting", result["status"])

    def test_no_candidates_is_unidentified(self):
        self.assertEqual("unidentified", self.assess({}, {})["status"])

    def test_nonfinite_and_invalid_numbers_never_verify(self):
        for score, duration, length in ((float("nan"), 200, 200000), (float("inf"), 200, 200000),
                                        (0.99, float("nan"), 200000), (0.99, 0, 200000),
                                        (0.99, 200, "not-a-duration")):
            with self.subTest(score=score, duration=duration, length=length):
                result = audio.assess_recording_candidates(
                    {"duration": duration}, {"rec-a": score}, {"rec-a": recording(length=length)})
                self.assertNotEqual("verified", result["status"])
                self.assertFalse(audio.assess_recording(
                    {"duration": duration}, {"rec-a": score}, recording(length=length))[0])

    def test_candidate_lookup_failure_retains_other_candidates_and_error(self):
        self.provide({"rec-a": recording(), "rec-b": TimeoutError("synthetic MB timeout")})
        result = self.audit()
        self.assertNotEqual("preliminary_match", result["status"])
        self.assertEqual("probable", result["assessment"]["status"])
        self.assertEqual(2, len(result["assessment"]["candidates"]))
        self.assertEqual("rec-b", result["evidence"]["errors"][0]["recording_id"])
        self.assertFalse(result["evidence"]["complete"])

    def test_partial_503_hydration_is_retryable_not_a_verified_identity(self):
        http_error = HTTPError(
            "https://synthetic.invalid/recording", 503, "Service Temporarily Unavailable", {}, None)
        self.addCleanup(http_error.close)
        failure = lookup.musicbrainzngs.NetworkError("retried 1 times", http_error)
        self.provide({"rec-a": recording(), "rec-b": failure}, acoustic("rec-a", "rec-b", score=1.0))
        result = self.audit()
        self.assertEqual("needs_review", result["status"])
        self.assertEqual("probable", result["assessment"]["status"])
        self.assertFalse(result["assessment"]["complete"])
        self.assertEqual("rec-a", result["details"]["id"], "details remain tentative, not authority")
        self.assertEqual(2, len(result["assessment"]["candidates"]))
        error = result["evidence"]["errors"][0]
        self.assertEqual("rec-b", error["recording_id"])
        self.assertEqual(503, error.get("http_status"))
        self.assertTrue(error.get("retryable"))
        self.assertTrue(result["evidence"].get("retryable"))
        self.assertTrue(result["assessment"].get("retryable"))
        self.assertIn("retryable_provider_error", result["assessment"]["reasons"])
        self.assertIn("MusicBrainz", result["reason"])
        self.assertIn("retry", result["reason"].casefold())
        self.assertNotIn("multiple_eligible_recordings", result["assessment"]["reasons"])

    def test_provider_retryability_distinguishes_outages_from_bad_data_and_requests(self):
        rate_limited = HTTPError("https://synthetic.invalid", 429, "rate limit", {}, None)
        self.addCleanup(rate_limited.close)
        forbidden = HTTPError("https://synthetic.invalid", 403, "forbidden", {}, None)
        self.addCleanup(forbidden.close)
        for failure, retryable in ((TimeoutError("synthetic timeout"), True),
                                   (ValueError("synthetic malformed data"), False),
                                   (rate_limited, True), (forbidden, False)):
            with self.subTest(failure=str(failure)):
                self.provide({"rec-a": failure})
                result = self.audit()
                self.assertNotEqual("preliminary_match", result["status"])
                self.assertEqual(retryable, result["evidence"]["errors"][0].get("retryable"))
                self.assertEqual(retryable, result["assessment"].get("retryable"))
        genuine = self.assess({"rec-a": 0.99, "rec-b": 0.99},
                              {"rec-a": recording(), "rec-b": recording("rec-b")})
        self.assertEqual("conflicting", genuine["status"])
        self.assertFalse(genuine.get("retryable", True))
        self.assertNotIn("retryable_provider_error", genuine["reasons"])

    def test_partial_hydration_can_retry_without_reinspecting_source(self):
        path, cache = self.track(), {}
        self.provide({"rec-a": recording(), "rec-b": TimeoutError("synthetic outage")})
        first = self.audit(path, cache=cache)
        self.assertEqual("probable", first["assessment"]["status"])
        self.provide({"rec-a": recording(), "rec-b": recording("rec-b", length=230000)})
        second = self.audit(path, cache=cache)
        self.assertEqual("verified", second["assessment"]["status"])
        self.assertFalse(second["assessment"].get("retryable", True))
        self.assertEqual(1, self.inspector.call_count)
        self.assertEqual(2, self.acoustid.call_count)

    def test_candidate_budget_retains_unhydrated_ids(self):
        records = {f"rec-{index:02d}": recording(f"rec-{index:02d}") for index in range(30)}
        self.provide(records)
        result = self.audit()
        self.assertEqual(30, len(result["assessment"]["candidates"]))
        self.assertLessEqual(self.recordings.call_count, audio.MAX_RECORDING_LOOKUPS)
        self.assertNotEqual("preliminary_match", result["status"])
        self.assertIn("candidate_budget_exhausted", result["assessment"]["reasons"])

    def test_lookup_outage_is_not_cached_as_empty_identity(self):
        path, cache = self.track(), {}
        self.acoustid.side_effect = TimeoutError("synthetic outage")
        first = self.audit(path, cache=cache)
        self.assertEqual("lookup_unavailable", first["status"])
        self.assertTrue(first["evidence"]["errors"])
        self.provide({"rec-a": recording()})
        second = self.audit(path, cache=cache)
        self.assertEqual("preliminary_match", second["status"])
        self.assertEqual(1, self.inspector.call_count, "successful local inspection must survive outage")

    def test_revision_cache_invalidates_same_path_replacement(self):
        path, cache = self.track(), {}
        self.provide({"rec-a": recording()})
        self.audit(path, cache=cache)
        self.audit(path, cache=cache)
        self.assertEqual(1, self.inspector.call_count)
        path.write_bytes(b"deliberately replaced synthetic source with different size")
        self.originals[path] = sha256(path.read_bytes()).hexdigest()
        self.audit(path, cache=cache)
        self.assertEqual(2, self.inspector.call_count)
        self.assertEqual(2, self.acoustid.call_count)

    def test_context_decision_does_not_poison_raw_cache(self):
        path, cache = self.track(), {}
        self.provide({"rec-a": recording(releases=("release-a",)),
                      "rec-b": recording("rec-b", releases=("release-b",))})
        self.assertEqual("conflicting", self.audit(path, cache=cache)["assessment"]["status"])
        contextual = self.audit(path, cache=cache, release_id="release-a")
        self.assertEqual("rec-a", contextual["details"]["id"])
        self.assertEqual("verified", contextual["assessment"]["status"])
        self.assertEqual("conflicting", self.audit(path, cache=cache)["assessment"]["status"])
        self.assertEqual(1, self.acoustid.call_count)

    def test_returned_evidence_cannot_mutate_cached_evidence(self):
        path, cache = self.track(), {}
        self.provide({"rec-a": recording()})
        result = self.audit(path, cache=cache)
        result["details"]["title"] = "mutated caller copy"
        result["evidence"]["matches"].clear()
        again = self.audit(path, cache=cache)
        self.assertEqual("Synthetic song", again["details"]["title"])
        self.assertEqual(1, len(again["evidence"]["matches"]))

    def test_legacy_recording_and_audit_tuple_apis(self):
        self.provide({"rec-a": recording()})
        matches, scores, details = audio.identify_recording(FINGERPRINT, "synthetic-key")
        self.assertEqual("rec-a", details["id"])
        self.assertEqual({"rec-a": 0.99}, scores)
        self.assertEqual(acoustic("rec-a"), matches)
        self.assertEqual(3, len(audio.audit_track(self.track(), "synthetic-key")))

    def test_subprocesses_have_finite_timeouts(self):
        fpcalc = self.script / ".tools" / "fpcalc"
        fpcalc.parent.mkdir()
        fpcalc.write_bytes(b"synthetic, never executed")
        with patch.object(audio.subprocess, "run", side_effect=fake_process(
                stdout='{"duration": 200, "fingerprint": "synthetic"}')) as run:
            audio.validate_audio(self.track())
            audio.fingerprint_audio(self.track("Import/Album/second.flac"))
        for call in run.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 120)

    def test_subprocess_timeout_has_structured_inspection_error(self):
        self.inspector.side_effect = subprocess.TimeoutExpired("ffmpeg", 60)
        result = self.audit()
        self.assertEqual("inspection_error", result["status"])
        self.assertEqual("unidentified", result["assessment"]["status"])
        self.assertEqual("inspection", result["evidence"]["errors"][0]["stage"])

    def test_actual_synthetic_audio_decode_preserves_source(self):
        path = self.stage / "Synthetic" / "silence.flac"
        make_audio(path, title="Synthetic silence", artist="Synthetic artist")
        self.originals[path] = sha256(path.read_bytes()).hexdigest()
        self.assertEqual((True, ""), audio.validate_audio(path))

    def test_acoustid_transport_is_bounded_and_rate_limited(self):
        clock = [10.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(audio, "identify_audio", ORIGINAL_IDENTIFY_AUDIO), \
             patch.object(audio, "urlopen", return_value=BytesIO(b'{"status":"ok","results":[]}')) as opened, \
             patch.object(audio.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(audio.time, "sleep", side_effect=sleep) as paused, \
             patch.object(audio, "_last_acoustid_request", None, create=True):
            audio.identify_audio(FINGERPRINT, "synthetic-key")
            opened.return_value = BytesIO(b'{"status":"ok","results":[]}')
            audio.identify_audio(FINGERPRINT, "synthetic-key")
            self.assertTrue(paused.called, "back-to-back real transport calls must be paced")
            self.assertGreaterEqual(clock[0], 11.0)
            self.assertTrue(all(0 < call.kwargs["timeout"] <= 30 for call in opened.call_args_list))

    def test_musicbrainz_transport_timeout_and_retry_bound_are_scoped(self):
        transport = lookup.musicbrainzngs.musicbrainz
        original = transport._safe_read
        opener = Mock()
        opener.open.return_value = BytesIO(b"synthetic response")
        def provider():
            return transport._safe_read(opener, "synthetic request")
        result = lookup._musicbrainz_call(provider)
        self.assertEqual(b"synthetic response", result)
        self.assertIs(original, transport._safe_read)
        self.assertEqual(1, opener.open.call_count)
        self.assertGreater(opener.open.call_args.kwargs["timeout"], 0)
        self.assertTrue(lookup.musicbrainzngs.musicbrainz.do_rate_limit)
        opener.open.side_effect = TimeoutError("synthetic transport timeout")
        with self.assertRaises(Exception):
            lookup._musicbrainz_call(provider)
        self.assertIs(original, transport._safe_read)
        self.assertEqual(4, opener.open.call_count, "initial success plus three bounded timeout attempts; no hidden eight-retry loop")

    def test_madilyn_positive_with_wrong_source_title_and_track_total_conflict(self):
        path = self.track("Elio Mei/A Thousand Songs About It All Act 1/My Only Sense Of Purpose.flac")
        self.provide({"rec-circus": recording("rec-circus", "One Man Circus", artist="Madilyn Mei")})
        result = self.audit(path)
        self.assertEqual("verified", result["assessment"]["status"])
        self.assertEqual("One Man Circus", result["details"]["title"])
        report = self.evaluate([result], [release_track("rec-circus", title="One Man Circus")],
                               local_tracks=[metadata_record(path, title=("My Only Sense Of Purpose",),
                                                             track_total=("12",))])
        self.assertEqual("conflicting", report["status"])
        self.assertIn("local_track_total_conflict", report["reasons"])
        self.assertEqual({(1, 1): path}, report["matched"])

    def test_abba_unresolved_synthetic_ambiguity_not_a_false_positive_oracle(self):
        path = self.track("ABBA/The Singles (The First Fifty Years)/No Doubt About It.flac")
        self.provide({"rec-danced": recording("rec-danced", "When You Danced With Me", artist="ABBA"),
                      "rec-doubt": recording("rec-doubt", "No Doubt About It", artist="ABBA")})
        result = self.audit(path)
        self.assertEqual("needs_review", result["status"])
        self.assertEqual("conflicting", result["assessment"]["status"])
        report = self.evaluate([result], [release_track("rec-danced", title="When You Danced With Me")])
        self.assertNotEqual("complete", report["status"])
        self.assertEqual([path], report["review"])

    def test_source_title_alone_neither_accepts_nor_rejects(self):
        for filename, score, expected in (("Wrong title.flac", 0.99, "verified"),
                                           ("Synthetic song.flac", 0.5, "probable")):
            with self.subTest(filename=filename):
                self.provide({"rec-a": recording()}, acoustic("rec-a", score=score))
                result = self.audit(self.track(f"Title/{filename}"))
                self.assertEqual(expected, result["assessment"]["status"])

    def test_discovery_is_ranked_union_not_intersection(self):
        audits = [accepted(self.track("Discovery/a.flac"), recording("rec-a", releases=("r-a", "r-common"))),
                  accepted(self.track("Discovery/b.flac"), recording("rec-b", releases=("r-b", "r-common"))),
                  accepted(self.track("Discovery/outlier.flac"), recording("rec-c", releases=("r-outlier",)))]
        self.assertEqual(["r-common", "r-a", "r-b", "r-outlier"],
                         lookup.discover_release_candidates(audits, "Wrong tagged album", "Wrong tagged artist"))

    def test_discovery_ignores_unaccepted_and_duplicate_anchor_votes(self):
        a = accepted(self.track(), recording(releases=("r-z",)))
        duplicate = accepted(self.track("Discovery/duplicate.flac"), recording(releases=("r-z",)))
        b = accepted(self.track("Discovery/b.flac"), recording("rec-b", releases=("r-a",)))
        weak = {"path": self.track("Discovery/weak.flac"), "status": "needs_review",
                "details": recording("weak", releases=("hint-only",))}
        self.assertEqual(["r-a", "r-z"], lookup.discover_release_candidates([a, duplicate, b, weak]))
        self.assertEqual([], lookup.discover_release_candidates([weak], "Tagged album", "Tagged artist"))

    def test_discovery_includes_audio_supported_alternatives_without_promoting_anchors(self):
        self.provide({"rec-a": recording(releases=("release-a",)),
                      "rec-b": recording("rec-b", releases=("release-b",))})
        ambiguous = self.audit()
        calls = self.recordings.call_count
        self.assertEqual("conflicting", ambiguous["assessment"]["status"])
        self.assertEqual(["release-a", "release-b"], lookup.discover_release_candidates(
            [ambiguous], "Wrong source album", "Wrong source artist"))
        self.assertEqual(calls, self.recordings.call_count, "discovery must remain provider-free")
        result = self.evaluate([ambiguous], [release_track()])
        self.assertEqual("partial", result["status"], "candidate discovery cannot manufacture an anchor")
        self.assertEqual({}, result["matched"])

    def test_independent_discovery_support_outranks_candidate_only_votes(self):
        anchors = [accepted(self.track("DiscoveryPriority/anchor.flac"), recording(releases=("r-z-anchor",)))]
        uncertain = []
        for index in range(2):
            self.provide({f"rec-{index}": recording(f"rec-{index}", releases=("r-a-candidate",)),
                          f"other-{index}": recording(f"other-{index}", releases=("r-other",))})
            uncertain.append(self.audit(self.track(f"DiscoveryPriority/{index}.flac")))
        self.assertEqual(["r-z-anchor", "r-a-candidate", "r-other"],
                         lookup.discover_release_candidates(anchors + uncertain))
        self.provide({"weak": recording("weak", releases=("r-weak",))}, acoustic("weak", score=0.949999))
        self.assertEqual([], lookup.discover_release_candidates([self.audit()]))

    def test_partial_candidate_discovery_retains_outage_without_context_promotion(self):
        self.provide({"rec-c": recording("rec-c"), "rec-other": TimeoutError("synthetic 503 outage")})
        uncertain = self.audit(self.track("Partial/uncertain.flac"))
        self.assertEqual(["release-a"], lookup.discover_release_candidates([uncertain]))
        anchors = [accepted(self.track("Partial/a.flac"), recording("rec-a")),
                   accepted(self.track("Partial/b.flac"), recording("rec-b"))]
        tracks = [release_track("rec-a"), release_track("rec-b", "2"), release_track("rec-c", "3")]
        result = self.evaluate(anchors + [uncertain], tracks)
        self.assertEqual("partial", result["status"])
        self.assertNotIn(uncertain["path"], result["matched"].values())
        self.assertTrue(result.get("retryable"))
        self.assertIn("retryable_provider_error", result["reasons"])
        self.assertEqual("rec-other", result["errors"][0]["recording_id"])

    def test_complete_release_and_legacy_raw_position_tuple(self):
        path = self.track()
        audits, tracks = [accepted(path)], [release_track()]
        report = self.evaluate(audits, tracks)
        self.assertEqual("complete", report["status"])
        self.assertEqual({(1, 1): path}, report["matched"])
        self.assertEqual([], report["missing"])
        self.assertEqual([], report["review"])
        self.assertEqual("release-a", report["release_id"])
        self.assertEqual(tracks, report["tracklist"])
        self.assertEqual(({("1", "1"): path}, [], []), lookup.match_album_to_release(audits, "release-a"))

    def test_partial_release_keeps_verified_mapping_and_missing(self):
        path = self.track()
        report = self.evaluate([accepted(path)], [release_track(), release_track("rec-b", "2")])
        self.assertEqual("partial", report["status"])
        self.assertEqual({(1, 1): path}, report["matched"])
        self.assertEqual(1, len(report["missing"]))
        self.assertIn("missing_release_tracks", report["reasons"])

    def test_duplicate_local_claims_are_order_independent(self):
        paths = [self.track("Duplicate/a.flac"), self.track("Duplicate/b.flac")]
        for order in (paths, list(reversed(paths))):
            with self.subTest(order=order):
                report = self.evaluate([accepted(path) for path in order], [release_track()])
                self.assertEqual("conflicting", report["status"])
                self.assertEqual({}, report["matched"])
                self.assertEqual(set(paths), set(report["review"]))

    def test_same_path_cannot_fill_two_positions(self):
        path = self.track()
        report = self.evaluate([accepted(path), accepted(path)],
                               [release_track(), release_track(number="2")])
        self.assertEqual("conflicting", report["status"])
        self.assertEqual({}, report["matched"])

    def test_repeated_remote_recording_requires_unambiguous_mapping(self):
        paths = [self.track("Repeated/a.flac"), self.track("Repeated/b.flac")]
        audits = [accepted(path) for path in paths]
        tracks = [release_track(), release_track(number="2")]
        report = self.evaluate(audits, tracks)
        self.assertEqual("partial", report["status"])
        self.assertEqual({}, report["matched"])
        mapped = self.evaluate(audits, tracks, manual_mapping={paths[0]: (1, 1)})
        self.assertEqual("complete", mapped["status"])
        self.assertEqual({(1, 1): paths[0], (1, 2): paths[1]}, mapped["matched"])

    def test_manual_mapping_cannot_override_missing_audio_identity(self):
        path = self.track()
        weak = {"path": path, "status": "needs_review", "details": recording()}
        result = self.evaluate([weak], [release_track()], manual_mapping={path: (1, 1)})
        self.assertNotEqual("complete", result["status"])
        self.assertEqual({}, result["matched"])

    def test_invalid_manual_mapping_is_conflicting(self):
        path = self.track()
        report = self.evaluate([accepted(path)], [release_track()], manual_mapping={path: (9, 9)})
        self.assertEqual("conflicting", report["status"])
        self.assertIn("invalid_manual_mapping", report["reasons"])

    def test_invalid_release_positions_and_empty_tracklists_never_complete(self):
        for tracks in ([], [release_track(number="A1")], [release_track(number="0")],
                       [release_track(), release_track("rec-b")]):
            with self.subTest(tracks=tracks):
                result = self.evaluate([accepted(self.track())], tracks)
                self.assertNotEqual("complete", result["status"])
                self.assertTrue(result["reasons"])

    def test_provider_failure_returns_partial_report(self):
        path = self.track()
        self.tracklists.side_effect = TimeoutError("synthetic release timeout")
        result = lookup.evaluate_release_candidate([accepted(path)], "release-a")
        self.assertEqual("partial", result["status"])
        self.assertTrue(result["errors"])
        self.assertEqual([path], result["review"])

    def test_competing_context_release_outage_cannot_be_treated_as_negative_evidence(self):
        self.provide({"rec-x": recording("rec-x", releases=("release-a",)),
                      "rec-y": recording("rec-y", releases=("release-b",))})
        uncertain = self.audit(self.track("CompetingEditions/uncertain.flac"))
        anchors = [accepted(self.track(f"CompetingEditions/{identifier}.flac"),
                            recording(identifier, releases=("release-a", "release-b")))
                   for identifier in ("rec-a", "rec-b")]
        audits = anchors + [uncertain]
        choices = {release: [release_track("rec-a"), release_track("rec-b", "2"),
                            release_track(identifier, "3")]
                   for release, identifier in (("release-a", "rec-x"), ("release-b", "rec-y"))}
        http_error = HTTPError(
            "https://synthetic.invalid/release", 503, "Service Temporarily Unavailable", {}, None)
        self.addCleanup(http_error.close)
        failure = lookup.musicbrainzngs.NetworkError("retried 1 times", http_error)
        def fetch(identifier):
            if identifier == "release-b":
                raise failure
            return deepcopy(choices[identifier])
        self.tracklists.side_effect = fetch
        self.assertEqual(["release-a", "release-b"], lookup.discover_release_candidates(audits))
        first = lookup.evaluate_release_candidate(audits, "release-a")
        interrupted = lookup.evaluate_release_candidate(audits, "release-b")
        self.assertEqual("complete", first["status"])
        self.assertEqual("partial", interrupted["status"])
        self.assertTrue(interrupted["retryable"])
        self.assertFalse(interrupted["tracklist"], "failed lookup proves no absence or incompatible edition")
        self.tracklists.side_effect = lambda identifier: deepcopy(choices[identifier])
        second = lookup.evaluate_release_candidate(audits, "release-b")
        self.assertEqual("complete", second["status"])
        self.assertEqual(first["matched"], second["matched"])
        self.assertNotEqual(first["tracklist"][2]["recording_id"], second["tracklist"][2]["recording_id"])
        self.assertEqual("conflicting", uncertain["assessment"]["status"])

    def test_context_cannot_promote_candidate_hydration_budget_exhaustion(self):
        records = {f"rec-{index:02d}": recording(f"rec-{index:02d}", length=200000 if index == 0 else 230000)
                   for index in range(audio.MAX_RECORDING_LOOKUPS + 1)}
        self.provide(records)
        uncertain = self.audit(self.track("ContextBudget/uncertain.flac"))
        anchors = [accepted(self.track(f"ContextBudget/{identifier}.flac"), recording(identifier))
                   for identifier in ("anchor-a", "anchor-b")]
        tracks = [release_track("anchor-a"), release_track("anchor-b", "2"), release_track("rec-00", "3")]
        result = self.evaluate(anchors + [uncertain], tracks)
        self.assertEqual("partial", result["status"])
        self.assertNotIn(uncertain["path"], result["matched"].values())
        self.assertEqual("candidate_budget_exhausted", result["errors"][0]["code"])
        self.assertFalse(result["retryable"], "local budget exhaustion is not a provider outage")

    def test_release_outage_report_retains_http_status_and_retryability(self):
        path = self.track()
        http_error = HTTPError(
            "https://synthetic.invalid/release", 503, "Service Temporarily Unavailable", {}, None)
        self.addCleanup(http_error.close)
        self.tracklists.side_effect = lookup.musicbrainzngs.NetworkError("retried 1 times", http_error)
        report = lookup.evaluate_release_candidate([accepted(path)], "release-a")
        self.assertEqual("partial", report["status"])
        self.assertTrue(report["retryable"])
        self.assertIn("retryable_provider_error", report["reasons"])
        self.assertEqual(503, report["errors"][0]["http_status"])
        self.assertEqual({}, report["matched"])

    def test_release_context_requires_independent_anchors(self):
        paths = [self.track(f"Context/{index}.flac") for index in range(3)]
        self.provide({"rec-c": recording("rec-c"),
                      "rec-other": recording("rec-other", releases=("release-other",))})
        uncertain = self.audit(paths[2])
        anchors = [accepted(paths[0], recording("rec-a")), accepted(paths[1], recording("rec-b"))]
        tracks = [release_track("rec-a"), release_track("rec-b", "2"), release_track("rec-c", "3")]
        self.assertNotEqual("complete", self.evaluate([uncertain], tracks)["status"])
        result = self.evaluate(anchors + [uncertain], tracks)
        self.assertEqual("complete", result["status"])
        self.assertIn("resolved_by_independent_anchors", result["reasons"])
        duplicate_anchors = [anchors[0], accepted(paths[1], recording("rec-a"))]
        self.assertNotEqual("complete", self.evaluate(duplicate_anchors + [uncertain], tracks)["status"])

    def test_album_context_preserves_score_and_duration_boundaries(self):
        for score, length, complete in ((0.95, 203000, True), (0.949999, 200000, False),
                                        (0.95, 203001, False)):
            with self.subTest(score=score, length=length):
                records = {"rec-c": recording("rec-c", length=length),
                           "rec-other": recording("rec-other"), "rec-third": recording("rec-third")}
                self.provide(records, acoustic("rec-c", score=score) + acoustic("rec-other", "rec-third"))
                uncertain = self.audit(self.track(f"ContextFloors/{score}-{length}.flac"))
                anchors = [accepted(self.track(f"ContextFloors/{identifier}.flac"), recording(identifier))
                           for identifier in ("rec-a", "rec-b")]
                tracks = [release_track(identifier, str(index))
                          for index, identifier in enumerate(("rec-a", "rec-b", "rec-c"), 1)]
                report = self.evaluate(anchors + [uncertain], tracks)
                self.assertEqual(complete, report["status"] == "complete")

    def test_context_cannot_discard_an_on_release_candidate_with_unknown_duration(self):
        self.provide({"rec-c": recording("rec-c"), "rec-d": recording("rec-d", length=None)})
        uncertain = self.audit(self.track("ContextTiming/uncertain.flac"))
        anchors = [accepted(self.track(f"ContextTiming/{identifier}.flac"), recording(identifier))
                   for identifier in ("rec-a", "rec-b", "rec-d")]
        tracks = [release_track(identifier, str(index))
                  for index, identifier in enumerate(("rec-a", "rec-b", "rec-c", "rec-d"), 1)]
        result = self.evaluate(anchors + [uncertain], tracks)
        self.assertEqual("partial", result["status"])
        self.assertNotIn(uncertain["path"], result["matched"].values())
        self.assertIn("unresolved_recording_identity", result["reasons"])

    def test_incomplete_tracklist_cannot_eliminate_a_context_competitor(self):
        self.provide({"rec-c": recording("rec-c"), "rec-other": recording("rec-other")})
        uncertain = self.audit(self.track("ContextCoverage/uncertain.flac"))
        anchors = [accepted(self.track(f"ContextCoverage/{identifier}.flac"), recording(identifier))
                   for identifier in ("rec-a", "rec-b")]
        tracks = lookup.ReleaseTracklist()
        tracks.extend(release_track(identifier, str(index))
                      for index, identifier in enumerate(("rec-a", "rec-b", "rec-c"), 1))
        tracks.complete = False
        result = self.evaluate(anchors + [uncertain], tracks)
        self.assertEqual("partial", result["status"])
        self.assertNotIn(uncertain["path"], result["matched"].values())
        self.assertNotIn("resolved_by_independent_anchors", result["reasons"])

    def test_complete_tracklist_can_supply_context_despite_incomplete_recording_membership(self):
        records = {identifier: recording(identifier, releases=()) for identifier in ("rec-c", "rec-other")}
        for details in records.values():
            details["release-list-complete"] = False
        self.provide(records)
        uncertain = self.audit(self.track("ContextMembership/uncertain.flac"))
        anchors = [accepted(self.track(f"ContextMembership/{identifier}.flac"), recording(identifier))
                   for identifier in ("rec-a", "rec-b")]
        tracks = [release_track(identifier, str(index))
                  for index, identifier in enumerate(("rec-a", "rec-b", "rec-c"), 1)]
        result = self.evaluate(anchors + [uncertain], tracks)
        self.assertEqual("complete", result["status"])
        self.assertEqual(uncertain["path"], result["matched"][(1, 3)])
        self.assertEqual("conflicting", uncertain["assessment"]["status"], "raw evidence stays context-free")

    def test_remix_live_and_edit_recording_ids_cannot_be_substituted_by_title(self):
        for title in ("Song (live)", "Song (alt version)", "Song (Nicole Moudaber remix)", "Song (2024 edit)"):
            with self.subTest(title=title):
                path = self.track()
                result = self.evaluate([accepted(path, recording("version-id", title))],
                                       [release_track("studio-id", title="Song")])
                self.assertEqual("conflicting", result["status"])
                self.assertEqual({}, result["matched"])
        self.assertEqual(lookup.normalize_title("Song (Lynn mix)"), lookup.normalize_title("Song - Lynn mix"))
        self.assertNotEqual(lookup.normalize_title("Song (live)"), lookup.normalize_title("Song"))

    def test_local_numbering_conflicts_do_not_erase_identity_mapping(self):
        path = self.track()
        result = self.evaluate([accepted(path)], [release_track()],
                               local_tracks=[metadata_record(path, track_number=("2",))])
        self.assertEqual("conflicting", result["status"])
        self.assertEqual({(1, 1): path}, result["matched"])
        self.assertIn("local_track_number_conflict", result["reasons"])

    def test_malformed_acoustid_payloads_never_verify(self):
        malformed = [None, {}, [None], [{"score": "invalid", "recordings": [{"id": "rec-a"}]}],
                     [{"score": 0.99, "recordings": [None]}],
                     [{"score": float("nan"), "recordings": [{"id": "rec-a"}]}]]
        for index, payload in enumerate(malformed):
            with self.subTest(payload=payload):
                self.acoustid.side_effect = None
                self.acoustid.return_value = payload
                result = self.audit(self.track(f"Malformed/{index}.flac"))
                self.assertNotEqual("preliminary_match", result["status"])
                self.assertTrue(result["evidence"]["errors"])
                self.assertFalse(result["evidence"]["complete"])

    def test_malformed_recording_details_are_retained_as_errors(self):
        for value in (None, [], {"id": "different-recording", "length": 200000}):
            with self.subTest(value=value):
                self.provide({"rec-a": value})
                result = self.audit()
                self.assertNotEqual("preliminary_match", result["status"])
                self.assertTrue(result["evidence"]["errors"])

    def test_invalid_fingerprint_provider_output_never_reaches_network(self):
        for fingerprint in ({}, [], {"duration": 0, "fingerprint": "x"},
                            {"duration": float("nan"), "fingerprint": "x"},
                            {"duration": 200, "fingerprint": []}):
            with self.subTest(fingerprint=fingerprint):
                self.inspector.return_value = (fingerprint, None)
                result = self.audit()
                self.assertEqual("inspection_error", result["status"])
                self.assertTrue(result["evidence"]["errors"])

    def test_fpcalc_malformed_or_oversized_output_is_rejected(self):
        fpcalc = self.script / ".tools" / "fpcalc"
        fpcalc.parent.mkdir()
        fpcalc.write_bytes(b"synthetic, never executed")
        for output in ("not-json", "[]", '{}', '{"duration":NaN,"fingerprint":"x"}',
                       '{"duration":200,"fingerprint":[]}', " " * 2_000_000):
            with self.subTest(output=output[:50]):
                with patch.object(audio.subprocess, "run", side_effect=fake_process(stdout=output)):
                    with self.assertRaises(ValueError):
                        audio.fingerprint_audio(self.track())

    def test_missing_and_failing_executables_are_inspection_errors(self):
        with patch.object(audio, "inspect_audio", ORIGINAL_INSPECT_AUDIO):
            for failure in (FileNotFoundError("synthetic missing ffmpeg"),
                            subprocess.CalledProcessError(1, "ffmpeg"),
                            subprocess.TimeoutExpired("ffmpeg", 60)):
                with self.subTest(failure=type(failure).__name__):
                    with patch.object(audio.subprocess, "run", side_effect=failure):
                        result = self.audit()
                    self.assertEqual("inspection_error", result["status"])
                    self.assertEqual("inspection", result["evidence"]["errors"][0]["stage"])

    def test_failed_decode_is_invalid_audio_without_fingerprinting(self):
        with patch.object(audio, "inspect_audio", ORIGINAL_INSPECT_AUDIO), \
             patch.object(audio.subprocess, "run", side_effect=fake_process(stderr="synthetic decode failure", returncode=1)):
            result = self.audit()
        self.assertEqual("invalid_audio", result["status"])
        self.assertEqual("unidentified", result["assessment"]["status"])

    def test_acoustid_malformed_or_oversized_transport_response(self):
        for payload in (b"not-json", b"[]", b'{"status":"ok","results":{}}', b" " * 3_000_000):
            with self.subTest(payload=payload[:50]):
                with patch.object(audio, "identify_audio", ORIGINAL_IDENTIFY_AUDIO), \
                     patch.object(audio, "urlopen", return_value=BytesIO(payload)), \
                     patch.object(audio, "_last_acoustid_request", None, create=True):
                    with self.assertRaises(ValueError):
                        audio.identify_audio(FINGERPRINT, "synthetic-key")

    def test_source_change_during_inspection_never_verifies(self):
        path, cache = self.track(), {}
        def changing_source(unused):
            path.write_bytes(b"synthetic source changed during inspection")
            self.originals[path] = sha256(path.read_bytes()).hexdigest()
            return deepcopy(FINGERPRINT), None
        self.inspector.side_effect = changing_source
        result = self.audit(path, cache=cache)
        self.assertEqual("inspection_error", result["status"])
        self.assertIn("source_changed", result["assessment"]["reasons"])
        self.assertNotIn(path, cache)

    def test_invalid_identity_and_boolean_positions_never_complete(self):
        path = self.track()
        for tracks in ([release_track(identifier=None)], [release_track(disc=True)],
                       [release_track(), release_track("rec-b", number="01")]):
            with self.subTest(tracks=tracks):
                result = self.evaluate([accepted(path)], tracks)
                self.assertEqual("conflicting", result["status"])
                self.assertEqual({}, result["matched"])

    def test_legacy_api_cannot_promote_partial_candidate_evidence(self):
        records = {f"rec-{index:02d}": recording(f"rec-{index:02d}", length=200000 if index == 0 else 230000)
                   for index in range(30)}
        self.provide(records)
        matches, scores, details = audio.identify_recording(FINGERPRINT, "synthetic-key")
        self.assertEqual(30, len(scores))
        self.assertIsNone(details, "legacy callers must not mistake a partial candidate for an accepted identity")

    def test_incomplete_release_membership_does_not_eliminate_competitor(self):
        other = recording("rec-b", releases=("release-other",))
        other["release-list-complete"] = False
        result = self.assess({"rec-a": 0.99, "rec-b": 0.99},
                             {"rec-a": recording(), "rec-b": other}, release_id="release-a")
        self.assertNotEqual("verified", result["status"])
        self.assertIn("release_membership_unavailable", result["reasons"])

    def test_unknown_release_membership_cannot_eliminate_a_competitor(self):
        other = recording("rec-b", releases=("release-other",))
        other.pop("release-list-complete")
        result = self.assess({"rec-a": 0.99, "rec-b": 0.99},
                             {"rec-a": recording(), "rec-b": other}, release_id="release-a")
        self.assertNotEqual("verified", result["status"])
        self.assertIn("release_membership_unavailable", result["reasons"])

    def test_context_verified_tracks_do_not_become_independent_discovery_anchors(self):
        path = self.track()
        self.provide({"rec-a": recording()})
        audit = self.audit(path, release_id="release-a")
        self.assertEqual([], lookup.discover_release_candidates([audit]))

    def test_local_inventory_without_audit_cannot_complete(self):
        path, extra = self.track(), self.track("Inventory/uninspected.flac")
        result = self.evaluate([accepted(path)], [release_track()],
                               local_tracks=[metadata_record(path), metadata_record(extra)])
        self.assertEqual("partial", result["status"])
        self.assertIn(extra, result["review"])
        self.assertIn("missing_audio_audit", result["reasons"])

    def test_malformed_unicode_and_huge_positions_are_conflicting_not_exceptions(self):
        for number in ("²", "1" * 5000, 1.0, -1):
            with self.subTest(number=str(number)[:20]):
                result = self.evaluate([accepted(self.track())], [release_track(number=number)])
                self.assertEqual("conflicting", result["status"])

    def test_musicbrainz_oversized_response_is_bounded_and_transport_restored(self):
        transport = lookup.musicbrainzngs.musicbrainz
        original = transport._safe_read
        response = BytesIO(b"x" * 3_000_000)
        opener = Mock()
        opener.open.return_value = response
        with self.assertRaises(ValueError):
            lookup._musicbrainz_call(lambda: transport._safe_read(opener, "synthetic request"))
        self.assertTrue(response.closed)
        self.assertIs(original, transport._safe_read)

    def test_fpcalc_failures_and_timeouts_have_structured_errors(self):
        fpcalc = self.script / ".tools" / "fpcalc"
        fpcalc.parent.mkdir()
        fpcalc.write_bytes(b"synthetic, never executed")
        with patch.object(audio, "inspect_audio", ORIGINAL_INSPECT_AUDIO), \
             patch.object(audio, "validate_audio", return_value=(True, "")):
            for failure in (FileNotFoundError("synthetic missing fpcalc"),
                            subprocess.CalledProcessError(1, "fpcalc"),
                            subprocess.TimeoutExpired("fpcalc", 45)):
                with self.subTest(failure=type(failure).__name__):
                    with patch.object(audio.subprocess, "run", side_effect=failure) as invoked:
                        result = self.audit()
                    self.assertEqual("inspection_error", result["status"])
                    self.assertEqual(type(failure).__name__, result["evidence"]["errors"][0]["type"])
                    self.assertEqual(45, invoked.call_args.kwargs["timeout"])

    def test_cover_metadata_is_bounded_and_malformed_input_is_rejected(self):
        for payload in (b"[]", b'{"images":{}}', b'{"images":[null]}',
                        b'{"images":[{"front":true,"approved":true}]}',
                        b'{"images":[],"padding":"' + b"x" * 3_000_000 + b'"}'):
            with self.subTest(payload=payload[:50]):
                with patch.object(lookup, "urlopen", return_value=BytesIO(payload)):
                    with self.assertRaises(ValueError):
                        lookup.get_release_front_art("synthetic-release")

    def test_all_named_stress_albums_have_explicit_synthetic_cases(self):
        # Each label is a small branch fixture, not a reconstructed real tracklist.
        complete = ["You're Not A Bad Person, it's Just A Bad World", "Rare Americans 2", "Sweat",
                    "No Love In LA / Punching Bag", "Let The Bad Times Roll (Deluxe Edition)", "EVERGREEN",
                    "Use Me  (Deluxe)", "Ritual", "Mistress for Christmas", "Scripted", "Dirty Mind"]
        partial = ["Upright, Love", "Tapeworms - Single", "let's blow this popsicle stand!", '"In case I make it,"',
                   "Ta-Dah", "Fish in a Birdcage - EP", "Touch-Tone Telephone - Single",
                   "Marvin's Marvelous Mechanical Museum", "Americana (ESCA 7393)", "Sex, Death & The Infinite Void",
                   "For One Night Only (Dead as Disco Soundtrack Vol 1)", "89.7 Growl FM", "Lent - Single",
                   "Charlie's Inferno - Single", "Then: The Earlier Years",
                   'Welcome to the Internet (From "Inside") - Single']
        unknown = ["Live", "The Red Means I Love You", "Daisy Bell - Single", "Panic", "Chippin' in",
                   "A Human's Touch (feat. McKenna Rae) - Single", "Hawaii: Part II",
                   "All Eyes On Me (Song Only) - Single", "Cyberpunk: Endgame", "The Normal Album (2023 Edit)"]
        metadata_only = ["Rule #34 - Single", "Lover Without A Heart - Single", "can u see me in the dark?",
                         "Never Fade Away", "Archangel", "Black Dog", "A Like Supreme", "The Ballad of Buck Ravers"]
        external_or_numbering = ["Rise And Fall, Rage And Grace", "The One Who's Running the Show - Single"]
        special = ["The Singles (The First Fifty Years)", "A Thousand Songs About It All: Act 1"]
        skipped = ["<I°_°I>", "Brittle Bones Nicky 3", "Under The Covers, Vol. IV", "Under the Covers Volume II",
                   "Under The Covers", "Under the Covers, Vol. III"]
        self.assertEqual(49, len(complete + partial + unknown + metadata_only + external_or_numbering + special))
        self.assertEqual(55, len(set(complete + partial + unknown + metadata_only + external_or_numbering + special + skipped)))
        for index, label in enumerate(complete + partial + unknown + metadata_only + external_or_numbering + special + skipped):
            with self.subTest(album=label, payload="synthetic"):
                path = self.track(f"Stress/{index}/track.flac")
                audits = [accepted(path)]
                tracks = [release_track()]
                expected = "complete"
                local = ()
                if label in partial:
                    tracks.append(release_track("rec-missing", "2"))
                    expected = "partial"
                elif label in unknown or label == special[0]:
                    audits = [{"path": path, "status": "needs_review", "details": None}]
                    expected = "partial"
                elif label in (external_or_numbering[1], special[1]):
                    local = [metadata_record(path, track_number=("9",))]
                    expected = "conflicting"
                report = self.evaluate(audits, tracks, local_tracks=local)
                self.assertEqual(expected, report["status"])
                # Artwork/album-artist/album-name policy is intentionally the caller's gate.


ORIGINAL_IDENTIFY_AUDIO = audio.identify_audio
ORIGINAL_INSPECT_AUDIO = audio.inspect_audio
ORIGINAL_BROWSE_RELEASES = lookup.musicbrainzngs.browse_releases


class MusicBrainzSnapshotTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        lookup.get_release_tracklist.cache_clear()
        lookup.get_recording_details.cache_clear()
        self.addCleanup(lookup.get_release_tracklist.cache_clear)
        self.addCleanup(lookup.get_recording_details.cache_clear)
        self.stack.enter_context(patch.object(lookup.musicbrainzngs, "browse_releases",
                                              side_effect=AssertionError("Unexpected membership browse")))

    def test_tracklist_keeps_release_specific_length_and_track_id(self):
        payload = {"release": {"medium-list": [{"position": "1", "format": "CD", "track-count": 1,
                   "track-list": [{"id": "release-track", "number": "1", "length": "201000",
                                   "recording": {"id": "rec-a", "title": "Song", "length": "200000"}}]}]}}
        with patch.object(lookup.musicbrainzngs, "get_release_by_id", return_value=payload):
            tracks = lookup.get_release_tracklist("synthetic-release")
        self.assertEqual("release-track", tracks[0]["track_id"])
        self.assertEqual("201000", tracks[0]["length"])
        self.assertEqual("200000", tracks[0]["recording_length"])
        self.assertEqual("CD", tracks[0]["medium_format"])

    def test_remote_counts_must_prove_completeness_before_context_resolution(self):
        paths = [self.track_path(f"RemoteCounts/{identifier}.flac") for identifier in ("rec-a", "rec-b", "rec-c")]
        audits = [accepted(path, recording(identifier)) for path, identifier in zip(paths[:2], ("rec-a", "rec-b"))]
        assessment = audio.assess_recording_candidates(FINGERPRINT, {"rec-c": 0.99, "rec-d": 0.99},
                                                       {"rec-c": recording("rec-c"), "rec-d": recording("rec-d")})
        audits.append({"path": paths[2], "status": "needs_review", "details": None, "assessment": assessment})
        for missing in ("medium-count", "track-count", None):
            with self.subTest(missing=missing):
                lookup.get_release_tracklist.cache_clear()
                medium = {"position": "1", "track-count": 3, "track-list": [
                    {"number": str(index), "recording": {"id": identifier, "title": identifier}}
                    for index, identifier in enumerate(("rec-a", "rec-b", "rec-c"), 1)]}
                release = {"medium-count": 1, "medium-list": [medium]}
                if missing == "medium-count":
                    release.pop(missing)
                elif missing:
                    medium.pop(missing)
                with patch.object(lookup.musicbrainzngs, "get_release_by_id", return_value={"release": release}):
                    report = lookup.evaluate_release_candidate(audits, "release-a")
                self.assertEqual(missing is None, report["tracklist"].complete)
                self.assertEqual(missing is None, report["status"] == "complete")
                if missing:
                    self.assertNotIn(paths[2], report["matched"].values())
                    self.assertIn("remote_tracklist_incomplete", report["reasons"])

    def test_incomplete_medium_cannot_complete_release(self):
        payload = {"release": {"medium-list": [{"position": "1", "track-count": 2,
                   "track-list": [{"number": "1", "recording": {"id": "rec-a", "title": "Song"}}]}]}}
        with patch.object(lookup.musicbrainzngs, "get_release_by_id", return_value=payload):
            result = lookup.evaluate_release_candidate([accepted(self.track_path())], "synthetic-release")
        self.assertEqual("partial", result["status"])
        self.assertIn("remote_tracklist_incomplete", result["reasons"])

    def test_truncated_recording_membership_is_completed_by_bounded_browse(self):
        payload = {"recording": {**recording(), "release-count": 2}}
        full = {"release-count": 2, "release-list": [{"id": "release-a"}, {"id": "release-discovered"}]}
        with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value=payload), \
             patch.object(lookup.musicbrainzngs, "browse_releases", return_value=full) as browse:
            details = lookup.get_recording_details("rec-a")
        self.assertTrue(details["release-list-complete"])
        self.assertEqual({"release-a", "release-discovered"}, {item["id"] for item in details["release-list"]})
        browse.assert_called_once_with(recording="rec-a", limit=100, offset=0)
        self.assertEqual(["release-a", "release-discovered"], lookup.discover_release_candidates(
            [accepted(self.track_path(), details)]))

    def test_membership_browse_uses_the_installed_client_api_without_transport(self):
        payload = {"recording": {**recording(releases=("release-100",)), "release-count": 101}}
        pages = [{"release-count": 101, "release-list": [{"id": f"release-{i}"} for i in range(100)]},
                 {"release-count": 101, "release-list": [{"id": "release-100"}]}]
        with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value=payload), \
             patch.object(lookup.musicbrainzngs, "browse_releases", ORIGINAL_BROWSE_RELEASES), \
             patch.object(lookup.musicbrainzngs.musicbrainz, "_do_mb_query", side_effect=pages) as query:
            details = lookup.get_recording_details("rec-a")
        self.assertTrue(details["release-list-complete"])
        self.assertEqual(2, query.call_count)
        self.assertEqual(("release", "", [], {"recording": "rec-a", "limit": 100}), query.call_args_list[0].args)
        self.assertEqual(("release", "", [], {"recording": "rec-a", "limit": 100, "offset": 100}),
                         query.call_args_list[1].args)

    def test_complete_membership_pages_preserve_offsets_and_defensive_cache(self):
        payload = {"recording": {**recording(releases=("release-100",)), "release-count": 101}}
        pages = [{"release-count": 101, "release-list": [{"id": f"release-{i}"} for i in range(100)]},
                 {"release-count": 101, "release-list": [{"id": "release-100"}]}]
        with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value=payload), \
             patch.object(lookup.musicbrainzngs, "browse_releases", side_effect=pages) as browse:
            details = lookup.get_recording_details("rec-a")
            self.assertTrue(details["release-list-complete"])
            self.assertEqual(101, len(details["release-list"]))
            details["release-list"].clear()
            again = lookup.get_recording_details("rec-a")
        self.assertEqual(101, len(again["release-list"]))
        self.assertEqual([0, 100], [call.kwargs["offset"] for call in browse.call_args_list])

    def test_malformed_membership_page_never_becomes_an_exhaustive_negative(self):
        payload = {"recording": {**recording(), "release-count": 2}}
        for page in ({"release-count": 2, "release-list": [None]},
                     {"release-count": 2, "release-list": [{"id": ""}]},
                     {"release-count": 2, "release-list": {}}, None):
            with self.subTest(page=page):
                lookup.get_recording_details.cache_clear()
                with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value=payload), \
                     patch.object(lookup.musicbrainzngs, "browse_releases", return_value=page):
                    with self.assertRaises(ValueError):
                        lookup.get_recording_details("rec-a")

    def test_recording_membership_without_count_is_not_an_exhaustive_negative(self):
        with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value={"recording": recording()}), \
             patch.object(lookup.musicbrainzngs, "browse_releases", side_effect=AssertionError("No declared pagination")):
            details = lookup.get_recording_details("rec-a")
        self.assertFalse(details["release-list-complete"])
        self.assertEqual([{"id": "release-a"}], details["release-list"])

    def test_recording_membership_page_budget_preserves_positive_memberships(self):
        payload = {"recording": {**recording(releases=("release-000",)), "release-count": 400}}
        def page(**options):
            return {"release-count": 400, "release-list": [
                {"id": f"release-{index:03d}"} for index in range(options["offset"], options["offset"] + 100)]}
        with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value=payload), \
             patch.object(lookup.musicbrainzngs, "browse_releases", side_effect=page) as browse:
            details = lookup.get_recording_details("rec-a")
        self.assertEqual(3, browse.call_count)
        self.assertEqual([0, 100, 200], [call.kwargs["offset"] for call in browse.call_args_list])
        self.assertFalse(details["release-list-complete"])
        self.assertEqual(300, len(details["release-list"]))
        self.assertIn({"id": "release-299"}, details["release-list"])

    def test_duplicate_or_inconsistent_membership_pages_never_prove_absence(self):
        for inconsistent in (False, True):
            with self.subTest(inconsistent=inconsistent):
                lookup.get_recording_details.cache_clear()
                payload = {"recording": {**recording(releases=("release-0",)), "release-count": 101}}
                pages = [{"release-count": 101, "release-list": [{"id": f"release-{i}"} for i in range(100)]},
                         {"release-count": 102 if inconsistent else 101,
                          "release-list": [{"id": "release-100" if inconsistent else "release-0"}]}]
                with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value=payload), \
                     patch.object(lookup.musicbrainzngs, "browse_releases", side_effect=pages) as browse:
                    details = lookup.get_recording_details("rec-a")
                self.assertEqual(2, browse.call_count)
                self.assertFalse(details["release-list-complete"])

    def test_membership_browse_outage_is_not_cached_as_complete_or_empty(self):
        payload = {"recording": {**recording(), "release-count": 2}}
        full = {"release-count": 2, "release-list": [{"id": "release-a"}, {"id": "release-b"}]}
        with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value=payload), \
             patch.object(lookup.musicbrainzngs, "browse_releases", side_effect=[TimeoutError("synthetic 503"), full]) as browse:
            with self.assertRaises(TimeoutError):
                lookup.get_recording_details("rec-a")
            details = lookup.get_recording_details("rec-a")
        self.assertEqual(2, browse.call_count)
        self.assertTrue(details["release-list-complete"])

    def test_cached_recording_and_tracklist_are_defensive_snapshots(self):
        with patch.object(lookup.musicbrainzngs, "get_recording_by_id", return_value={"recording": recording()}):
            first = lookup.get_recording_details("rec-a")
            first["title"] = "caller mutation"
            self.assertEqual("Synthetic song", lookup.get_recording_details("rec-a")["title"])
        payload = {"release": {"medium-list": [{"position": "1", "track-list": [
            {"number": "1", "recording": {"id": "rec-a", "title": "Song"}}]}]}}
        with patch.object(lookup.musicbrainzngs, "get_release_by_id", return_value=payload):
            first = lookup.get_release_tracklist("synthetic-release")
            first.clear()
            self.assertEqual(1, len(lookup.get_release_tracklist("synthetic-release")))
