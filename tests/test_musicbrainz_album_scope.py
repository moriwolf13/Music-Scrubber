"""Album-scope integration; synthetic HTTP responses in the isolated runner only."""
import json
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

import audio
import lookup
import mixie
import review
import tag
from support import WorkspaceCase, make_cover, metadata_record


RECORDING_ID = "11111111-1111-4111-8111-111111111111"
RELEASE_ID = "22222222-2222-4222-8222-222222222222"
ARTIST_ID = "33333333-3333-4333-8333-333333333333"
TRACK_ID = "44444444-4444-4444-8444-444444444444"
ALBUM = "Synthetic private album title"
ARTIST = "Synthetic artist"
TITLE = "Synthetic song"
KEY = "DISPOSABLE-OFFLINE-TEST-KEY"


def xml_response(content):
    return ('<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">'
            + content + '</metadata>').encode("utf-8")


CREDIT = (f'<artist-credit><name-credit><artist id="{ARTIST_ID}">'
          f'<name>{ARTIST}</name></artist></name-credit></artist-credit>')
RECORDING = (f'<recording id="{RECORDING_ID}"><title>{TITLE}</title>'
             f'<length>200000</length>{CREDIT}<release-list count="1">'
             f'<release id="{RELEASE_ID}"><title>{ALBUM}</title></release>'
             '</release-list></recording>')
RELEASE = (f'<release id="{RELEASE_ID}"><title>{ALBUM}</title>{CREDIT}'
           '<date>2020</date><release-group id="55555555-5555-4555-8555-555555555555" '
           'type="Album"><primary-type>Album</primary-type></release-group>'
           '<medium-list count="1"><medium><position>1</position><format>CD</format>'
           f'<track-list count="1"><track id="{TRACK_ID}"><position>1</position>'
           f'<number>1</number><title>{TITLE}</title><length>200000</length>'
           f'<recording id="{RECORDING_ID}"><title>{TITLE}</title><length>200000</length>'
           '</recording></track></track-list></medium></medium-list></release>')


class MusicBrainzAlbumScopeTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.assertIsNone(lookup.current_musicbrainz_album())
        self.clear_caches()
        self.addCleanup(self.clear_caches)
        self.path = self.track_path()
        self.metadata: dict = metadata_record(self.path, album=(ALBUM,), title=(TITLE,), artist=(ARTIST,),
                                        album_artist=(ARTIST,))
        self.album = {"album": ALBUM, "folder": self.path.parent, "tracks": [self.metadata]}
        self.stack.enter_context(patch.object(tag, "read_track_metadata",
                                             side_effect=lambda path: deepcopy(self.metadata)))
        details = review.issue_details([{"path": self.path, "status": "needs_review",
            "reason": "Synthetic unresolved identity", "existing": self.metadata}], [], "Explicit review required")
        self.issue = {"import_name": "Import", "album": ALBUM, "details": details,
                      "tracks": [self.path.relative_to(self.stage).as_posix()]}
        self.fingerprint = mixie.review_fingerprint(self.issue)
        self.observations, self.requests, self.saved = [], [], []
        self.failures = {}
        self.output, self.stderr = StringIO(), StringIO()
        self.clock, self.sleeps, self.request_times = 1000.0, [], []
        self.responses = {
            f"recording/{RECORDING_ID}": xml_response(RECORDING),
            f"release/{RELEASE_ID}": xml_response(RELEASE),
            "recording": xml_response('<recording-list count="1" offset="0">' + RECORDING + '</recording-list>'),
            "release": xml_response('<release-list count="1" offset="0">' + RELEASE + '</release-list>'),
        }
        self.transport = lookup.musicbrainzngs.musicbrainz
        self.stack.enter_context(patch.dict(lookup.os.environ, {
            "MIXIE_MB_DIAGNOSTICS": "1", "MIXIE_MB_INTERVAL_SECONDS": "2", "MIXIE_MB_MAX_ATTEMPTS": "3"}))
        self.stack.enter_context(redirect_stderr(self.stderr))
        self.stack.enter_context(patch.object(lookup, "_musicbrainz_cooldown_until", 0.0))
        self.stack.enter_context(patch.object(lookup, "_musicbrainz_cooldown_status", None))
        self.stack.enter_context(patch.object(lookup.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(lookup.time, "time", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(lookup.time, "sleep", side_effect=self.sleep))
        self.stack.enter_context(patch.object(self.transport._mb_request, "remaining_requests", None))
        self.stack.enter_context(patch.object(self.transport._mb_request, "last_call", 0.0))
        for name in ("limit_interval", "limit_requests", "do_rate_limit"):
            self.stack.enter_context(patch.object(self.transport, name, getattr(self.transport, name)))
        self.opener = Mock()
        self.opener.open.side_effect = self.respond
        self.stack.enter_context(patch.object(self.transport.compat, "build_opener", return_value=self.opener))
        self.audit = self.stack.enter_context(patch.object(audio, "audit_tracks", side_effect=self.audit_tracks))
        self.stack.enter_context(patch.object(mixie, "select_album_art", side_effect=self.select_art))
        self.stack.enter_context(patch.object(lookup, "urlopen", side_effect=AssertionError("No artwork provider")))
        self.before = self.tree_snapshot()
        self.addCleanup(self.assert_transient)

    @staticmethod
    def clear_caches():
        for operation in (lookup.get_recording_details, lookup.get_release_tracklist, lookup.get_release_metadata):
            getattr(operation, "cache_clear")()

    def tree_snapshot(self):
        return {(label, path.relative_to(root).as_posix()): path.read_bytes() if path.is_file() else None
                for label, root in (("case", self.root), ("application", Path.cwd()))
                for path in root.rglob("*")}

    def assert_transient(self):
        self.assertIsNone(lookup.current_musicbrainz_album())
        self.assertEqual(self.before, self.tree_snapshot(), "Request scopes/review must not write filesystem state")
        for private in (ALBUM, str(self.path), KEY, RECORDING_ID, RELEASE_ID, "Private query", "https://"):
            self.assertNotIn(private, self.stderr.getvalue())
        self.assertNotIn("MIXIE_MB ", self.output.getvalue())

    def sleep(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.sleeps.append(seconds)
        self.clock += seconds

    def summaries(self):
        return [event for line in self.stderr.getvalue().splitlines() if line.startswith("MIXIE_MB ")
                if (event := json.loads(line.removeprefix("MIXIE_MB ")))["event"] == "album_summary"]

    def observe(self, phase):
        stats = lookup.current_musicbrainz_album()
        if not isinstance(stats, dict):
            self.fail(f"No album context during {phase}")
        self.assertIs(type(stats["album_sequence"]), int)
        for field in ("actual_attempts", "http_failures", "retries", "categories", "failures"):
            self.assertIn(field, stats)
        serialized = json.dumps(stats)
        for private in (ALBUM, str(self.path), KEY, "Private query"):
            self.assertNotIn(private, serialized)
        self.observations.append((phase, deepcopy(stats)))
        return stats

    def respond(self, request, body=None, *, timeout):
        stats = self.observe("http")
        endpoint = urlsplit(request.full_url).path.removeprefix("/ws/2/").strip("/")
        self.requests.append((endpoint, deepcopy(stats)))
        self.request_times.append(self.clock)
        self.clock += 0.1
        self.assertGreater(timeout, 0)
        self.assertIn(endpoint, self.responses, "Unexpected raw provider request")
        failure = self.failures.get(endpoint)
        if isinstance(failure, list):
            failure = failure.pop(0) if failure else None
        if failure is not None:
            raise failure
        return BytesIO(self.responses[endpoint])

    def audit_tracks(self, paths, api_key, cache):
        self.observe("audit")
        self.assertEqual([self.path], paths)
        self.assertEqual(KEY, api_key)
        details = lookup.get_recording_details(RECORDING_ID)
        return [{"path": self.path, "status": "preliminary_match", "reason": "Synthetic accepted anchor",
                 "details": details}]

    def select_art(self, *args, **kwargs):
        self.observe("artwork")
        return make_cover()

    def save(self, issue):
        self.observe("save")
        self.saved.append(deepcopy(issue))
        serialized = json.dumps(issue)
        for field in ("album_sequence", "actual_attempts", "http_failures", "retries", "categories", "failures"):
            self.assertNotIn('"' + field + '"', serialized)

    def choose(self, answers, *, issue=None, save=None):
        issue = self.issue if issue is None else issue
        answers = iter(answers)

        def respond(prompt):
            self.observe("prompt")
            try:
                answer = next(answers)
            except StopIteration:
                raise AssertionError("Unexpected review prompt: " + prompt) from None
            if isinstance(answer, BaseException):
                raise answer
            return answer

        with patch("builtins.input", side_effect=respond), redirect_stdout(self.output):
            return review.choose_interpretation(issue, self.fingerprint, save=self.save if save is None else save)

    @staticmethod
    def offline_answers(approval="APPROVE"):
        return ["release", "offline", "fields", ALBUM, ARTIST, "1", "1",
                "track 1", "manual", TITLE, ARTIST, "1", "1", "totals", "enter", "12", "3",
                "approve", approval]

    def test_preview_opens_fresh_scope_even_when_all_lookups_are_cached(self):
        before = deepcopy(self.album)
        with redirect_stdout(self.output):
            first = mixie.preview_album(self.album, KEY)
            self.assertIsNone(lookup.current_musicbrainz_album())
            second = mixie.preview_album(self.album, KEY)
        audits = [stats for phase, stats in self.observations if phase == "audit"]
        ends = [stats for phase, stats in self.observations if phase == "artwork"]
        self.assertEqual(2, len(audits))
        self.assertEqual(audits[0]["album_sequence"] + 1, audits[1]["album_sequence"])
        self.assertEqual([0, 0], [stats["actual_attempts"] for stats in audits])
        self.assertEqual([3, 0], [stats["actual_attempts"] for stats in ends])
        self.assertEqual(3, len(self.requests))
        self.assertEqual({audits[0]["album_sequence"]}, {stats["album_sequence"] for _, stats in self.requests})
        self.assertEqual(first, second)
        self.assertTrue(first["identities"][str(self.path)]["verified"])
        self.assertNotIn("album_sequence", first)
        self.assertEqual(before, self.album)
        self.assertEqual([3, 0], [summary["actual_attempts"] for summary in self.summaries()])
        self.assertEqual([stats["album_sequence"] for stats in audits],
                         [summary["album_sequence"] for summary in self.summaries()])
        self.assertTrue(self.transport.do_rate_limit)
        self.assertTrue(self.sleeps)
        self.assertTrue(all(b - a >= 1.997 for a, b in zip(self.request_times, self.request_times[1:])))

    def test_preview_exceptions_and_release_lookup_ctrl_c_exit_scope_and_restore_transport(self):
        cases = ((f"recording/{RECORDING_ID}", RuntimeError("Synthetic lookup failure")),
                 (f"release/{RELEASE_ID}", KeyboardInterrupt()))
        for endpoint, failure in cases:
            with self.subTest(failure=type(failure).__name__):
                self.clear_caches()
                self.failures = {endpoint: failure}
                original = self.transport._safe_read
                before = len(self.summaries())
                with redirect_stdout(self.output), self.assertRaises(type(failure)) as caught:
                    mixie.preview_album(self.album, KEY)
                self.assertIs(failure, caught.exception)
                self.assertIsNone(lookup.current_musicbrainz_album())
                self.assertIs(original, self.transport._safe_read)
                summaries = self.summaries()
                self.assertEqual(before + 1, len(summaries))
                self.assertEqual("interrupted" if isinstance(failure, KeyboardInterrupt) else "failed",
                                 summaries[-1]["outcome"])
                self.assertEqual(2 if isinstance(failure, KeyboardInterrupt) else 1, summaries[-1]["actual_attempts"])

    def test_manual_recording_and_release_searches_and_id_lookups_share_one_scope(self):
        before = deepcopy(self.issue["details"])
        answers = ["track 1", "search", "Private query artist", "Private query song", "1", "", "", "1", "1",
                   "track 1", "id", RECORDING_ID, "", "", "1", "1",
                   "release", "search", "Private query artist", "Private query album", "1", "approve", "APPROVE"]
        self.assertTrue(self.choose(answers))
        self.assertEqual(["recording", f"recording/{RECORDING_ID}", "release",
                          f"release/{RELEASE_ID}", f"release/{RELEASE_ID}"], [endpoint for endpoint, _ in self.requests])
        self.assertEqual(1, len({stats["album_sequence"] for _, stats in self.observations}))
        self.assertEqual(5, self.observations[-1][1]["actual_attempts"])
        decision = self.issue["interpretation"]
        self.assertEqual(RELEASE_ID, decision["release_id"])
        self.assertTrue(decision["user_approved"])
        self.assertFalse(decision["independently_verified"])
        self.assertEqual(before, self.issue["details"])
        self.audit.assert_not_called()

    def test_failed_manual_searches_require_explicit_offline_choices_and_keep_unknown_totals_deferred(self):
        self.failures = {endpoint: HTTPError("https://musicbrainz.invalid/", 404, "Synthetic missing result", Message(), None)
                         for endpoint in ("recording", "release")}
        self.metadata.update(track_total=("12",), disc_total=("3",))
        before = deepcopy(self.issue["details"])
        answers = ["track 1", "search", "Private query artist", "Private query song", "back",
                   "release", "search", "Private query artist", "Private query album", "offline",
                   "fields", ALBUM, ARTIST, "1", "1", "track 1", "manual", TITLE, ARTIST, "1", "1",
                   "totals", "defer", "approve", "APPROVE"]
        self.assertTrue(self.choose(answers))
        stats = self.observations[-1][1]
        self.assertEqual((2, 2, 0), (stats["actual_attempts"], stats["http_failures"], stats["retries"]))
        decision = self.issue["interpretation"]
        self.assertTrue(decision["user_approved"])
        self.assertFalse(decision["independently_verified"])
        self.assertTrue(decision["partial"])
        self.assertIsNone(decision["release_id"])
        self.assertNotIn("date", decision)
        self.assertNotIn("genre", decision)
        self.assertEqual(before, self.issue["details"])
        plan = review.prepare_interpretation(self.album, decision, self.fingerprint)
        self.assertEqual({str(self.path): ["track_total", "disc_total"]}, plan["numbering_deferred"])
        self.assertIsNone(plan["release_totals"])
        self.assertEqual([], plan["compatible_ids"])
        self.assertFalse(plan["identities"][str(self.path)]["verified"])

    def test_offline_exact_approval_and_preview_remain_user_approved_not_verified_without_requests(self):
        for approval in ("approve", "", "APPROVE"):
            with self.subTest(approval=approval):
                issue = deepcopy(self.issue)
                self.assertEqual(approval == "APPROVE", self.choose(self.offline_answers(approval), issue=issue))
                self.assertIsNone(lookup.current_musicbrainz_album())
                decision = issue["interpretation"]
                self.assertIs(decision["user_approved"], approval == "APPROVE")
                self.assertIs(decision["independently_verified"], False)
                self.assertTrue(decision["partial"])
                if approval == "APPROVE":
                    with redirect_stdout(self.output):
                        plan = mixie.preview_album(self.album, None, interpretation=decision)
                    self.assertEqual("user_approved", plan["provenance"])
                    self.assertEqual([], plan["compatible_ids"])
                    self.assertIsNone(plan["release_totals"])
                    self.assertEqual({}, plan["numbering_deferred"])
                    self.assertEqual({str(self.path): {"track_total": 12, "disc_total": 3}}, plan["numbering_totals"])
                    self.assertFalse(plan["identities"][str(self.path)]["verified"])
                    self.assertTrue(plan["identities"][str(self.path)]["user_approved"])
        self.assertEqual([], self.requests)
        self.assertTrue(all(stats["actual_attempts"] == 0 for _, stats in self.observations))
        self.audit.assert_not_called()

    def test_manual_postpone_eof_and_ctrl_c_exit_scope_with_unapproved_draft(self):
        for answer in ("0", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__):
                issue = deepcopy(self.issue)
                self.assertFalse(self.choose([answer], issue=issue))
                self.assertIsNone(lookup.current_musicbrainz_album())
                self.assertEqual("draft", issue["interpretation"]["status"])
                self.assertFalse(issue["interpretation"]["user_approved"])
                self.assertFalse(issue["interpretation"]["independently_verified"])
        self.assertEqual([], self.requests)

    def test_manual_save_failures_and_uncaught_ctrl_c_exit_scope(self):
        for failure in (RuntimeError("Synthetic checkpoint failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                def fail_save(issue, error=failure):
                    self.observe("failed_save")
                    raise error

                issue = deepcopy(self.issue)
                before = len(self.summaries())
                with self.assertRaises(type(failure)) as caught:
                    self.choose([], issue=issue, save=fail_save)
                self.assertIs(failure, caught.exception)
                self.assertIsNone(lookup.current_musicbrainz_album())
                self.assertFalse(issue["interpretation"]["user_approved"])
                summaries = self.summaries()
                self.assertEqual(before + 1, len(summaries))
                self.assertEqual(0, summaries[-1]["actual_attempts"])
                self.assertEqual("interrupted" if isinstance(failure, KeyboardInterrupt) else "failed",
                                 summaries[-1]["outcome"])
        self.assertEqual([], self.requests)

    def test_nested_preview_and_manual_review_reuse_outer_album_without_ending_it(self):
        with redirect_stdout(self.output), lookup.musicbrainz_album() as outer:
            if not isinstance(outer, dict):
                self.fail("Album scope must yield its request stats")
            sequence = outer["album_sequence"]
            mixie.preview_album(self.album, KEY)
            self.assertEqual(sequence, self.observe("nested_preview")["album_sequence"])
            self.assertFalse(self.choose(["0"]))
            self.assertEqual(sequence, self.observe("nested_review")["album_sequence"])
            self.assertEqual(3, outer["actual_attempts"])
            self.assertEqual([], self.summaries(), "Nested public functions must not summarize/end the outer album")
        summaries = self.summaries()
        self.assertEqual(1, len(summaries))
        self.assertEqual(sequence, summaries[0]["album_sequence"])
        self.assertEqual(3, summaries[0]["actual_attempts"])
        self.assertEqual({sequence}, {stats["album_sequence"] for _, stats in self.observations})
        self.assertIsNone(lookup.current_musicbrainz_album())

    def http_error(self, status, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        error = HTTPError("https://musicbrainz.invalid/", status, "Synthetic outage", headers, BytesIO())
        self.addCleanup(error.close)
        return error

    def test_preview_retry_summary_counts_actual_attempts_without_disabling_native_pacing(self):
        self.failures = {f"recording/{RECORDING_ID}": [self.http_error(503, "3")]}
        with redirect_stdout(self.output):
            plan = mixie.preview_album(self.album, KEY)
        summaries = self.summaries()
        self.assertEqual(1, len(summaries))
        summary = summaries[0]
        self.assertEqual((4, 1, 1), (summary["actual_attempts"], summary["http_failures"], summary["retries"]))
        self.assertEqual({"http_503": 1}, summary["failures"])
        self.assertEqual("finished", summary["outcome"])
        self.assertTrue(plan["identities"][str(self.path)]["verified"])
        self.assertTrue(self.transport.do_rate_limit)
        self.assertEqual(2, self.transport.limit_interval)
        self.assertIn(3, self.sleeps)
        self.assertGreaterEqual(self.request_times[1] - self.request_times[0], 3)
        self.assertTrue(all(b - a >= 1.997 for a, b in zip(self.request_times, self.request_times[1:])))

    def test_preview_retry_exhaustion_stays_a_failure_and_summarizes_all_bounded_attempts(self):
        self.failures = {f"recording/{RECORDING_ID}": [self.http_error(503) for _ in range(3)]}
        original = self.transport._safe_read
        with redirect_stdout(self.output), self.assertRaises(lookup.musicbrainzngs.NetworkError):
            mixie.preview_album(self.album, KEY)
        self.assertIs(original, self.transport._safe_read)
        self.assertEqual(3, len(self.requests))
        self.assertEqual({f"recording/{RECORDING_ID}"}, {endpoint for endpoint, _ in self.requests})
        summaries = self.summaries()
        self.assertEqual(1, len(summaries))
        summary = summaries[0]
        self.assertEqual((3, 3, 2), (summary["actual_attempts"], summary["http_failures"], summary["retries"]))
        self.assertEqual("failed", summary["outcome"])
        self.assertEqual([2.0, 4.0], self.sleeps)
        self.assertTrue(self.transport.do_rate_limit)
        self.assertTrue(all(b - a >= 1.997 for a, b in zip(self.request_times, self.request_times[1:])))
        self.assertFalse(any(phase == "artwork" for phase, _ in self.observations))

    def complete_evaluation(self, audits, release_id, *, local_tracks):
        self.observe("evaluation")
        self.assertEqual([self.path], [audit["path"] for audit in audits])
        self.assertEqual(self.album["tracks"], local_tracks)
        return {"release_id": release_id, "status": "complete", "matched": {(1, 1): self.path},
                "missing": [], "review": [], "reasons": [], "errors": [],
                "tracklist": [{"disc": "1", "number": "1", "recording_id": RECORDING_ID, "title": TITLE}]}

    def test_release_evaluation_budget_retains_unexamined_candidates_even_when_examined_releases_are_complete(self):
        candidate_ids = [RELEASE_ID, "synthetic-release-b", "synthetic-release-c", "synthetic-release-d"]
        before = list(candidate_ids)
        with (
            patch.object(lookup, "MAX_RELEASE_EVALUATIONS", 2),
            patch.object(lookup, "discover_release_candidates", return_value=candidate_ids),
            patch.object(lookup, "evaluate_release_candidate", side_effect=self.complete_evaluation) as evaluate,
            patch.object(lookup, "common_release_metadata", side_effect=AssertionError("Cannot choose an edition")) as common,
            patch.object(mixie, "select_album_art", side_effect=AssertionError("Cannot approve artwork")) as artwork,
            patch.object(mixie, "_identity_review_error", wraps=mixie._identity_review_error) as identity_error,
            redirect_stdout(self.output),
            self.assertRaises(review.AlbumReviewRequired) as caught,
        ):
            mixie.preview_album(self.album, KEY)
        identity_error.assert_called_once()
        common.assert_not_called()
        artwork.assert_not_called()
        self.assertEqual(2, evaluate.call_count)
        self.assertEqual(candidate_ids[:2], [call.args[1] for call in evaluate.call_args_list])
        details = caught.exception.details
        if not isinstance(details, dict):
            self.fail("Budget exhaustion must retain structured review evidence")
        self.assertEqual("identity_release", details["kind"])
        evaluated, sentinel = details["releases"][:-1], details["releases"][-1]
        self.assertTrue(all(row["status"] == "complete" for row in evaluated))
        self.assertEqual(candidate_ids[:2], [row["release_id"] for row in evaluated])
        self.assertIsNone(sentinel["release_id"])
        self.assertEqual("partial", sentinel["status"])
        self.assertEqual(["release_evaluation_budget_exhausted"], sentinel["reasons"])
        self.assertEqual([], sentinel["tracklist"])
        self.assertEqual([], sentinel["errors"])
        self.assertEqual(self.issue["tracks"], sentinel["review"])
        self.assertEqual(candidate_ids[2:], sentinel["unexamined_release_ids"])
        self.assertEqual(candidate_ids, [row["release_id"] for row in evaluated] + sentinel["unexamined_release_ids"])
        self.assertEqual(before, candidate_ids)
        self.assertEqual("failed", self.summaries()[0]["outcome"])

    def test_zero_release_evaluation_budget_keeps_all_candidates_and_performs_no_evaluation(self):
        candidate_ids = [RELEASE_ID, "synthetic-release-b"]
        with (
            patch.object(lookup, "MAX_RELEASE_EVALUATIONS", 0),
            patch.object(lookup, "discover_release_candidates", return_value=candidate_ids),
            patch.object(lookup, "evaluate_release_candidate", side_effect=AssertionError("No evaluations allowed")) as evaluate,
            patch.object(mixie, "_identity_review_error", wraps=mixie._identity_review_error) as identity_error,
            redirect_stdout(self.output),
            self.assertRaises(review.AlbumReviewRequired) as caught,
        ):
            mixie.preview_album(self.album, KEY)
        evaluate.assert_not_called()
        identity_error.assert_called_once()
        details = caught.exception.details
        if not isinstance(details, dict):
            self.fail("Budget exhaustion must retain structured review evidence")
        self.assertEqual(1, len(details["releases"]))
        sentinel = details["releases"][0]
        self.assertEqual(candidate_ids, sentinel["unexamined_release_ids"])
        self.assertEqual(["release_evaluation_budget_exhausted"], sentinel["reasons"])
        self.assertEqual([], sentinel["errors"])
        self.assertFalse(any(phase == "artwork" for phase, _ in self.observations))

    def test_exact_release_evaluation_budget_still_allows_a_fully_checked_compatible_plan(self):
        candidate_ids = [RELEASE_ID, "synthetic-release-b"]
        with (
            patch.object(lookup, "MAX_RELEASE_EVALUATIONS", 2),
            patch.object(lookup, "discover_release_candidates", return_value=candidate_ids),
            patch.object(lookup, "evaluate_release_candidate", side_effect=self.complete_evaluation) as evaluate,
            patch.object(lookup, "common_release_metadata", return_value={
                "album": ALBUM, "album_artist": ARTIST, "date": "2020"}) as common,
            redirect_stdout(self.output),
        ):
            plan = mixie.preview_album(self.album, KEY)
        self.assertEqual(2, evaluate.call_count)
        self.assertEqual(candidate_ids, [call.args[1] for call in evaluate.call_args_list])
        common.assert_called_once_with(candidate_ids)
        self.assertEqual(candidate_ids, plan["compatible_ids"])
        self.assertEqual(2, len(plan["evaluations"]))
        self.assertTrue(all(row["status"] == "complete" for row in plan["evaluations"]))
        self.assertTrue(plan["identities"][str(self.path)]["verified"])
        self.assertEqual("finished", self.summaries()[0]["outcome"])
