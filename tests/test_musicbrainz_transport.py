"""Real installed-client pacing with mocked wire responses; isolated runner only."""
from contextlib import redirect_stderr
from datetime import datetime, timezone
from email.message import Message
from email.utils import format_datetime
from io import BytesIO, StringIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from xml.parsers.expat import ExpatError
import json

from support import WorkspaceCase
import lookup


class Response(BytesIO):
    status = 200

    def getcode(self):
        return self.status


class MusicBrainzTransportTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        self.wire = []
        self.sleeps = []
        self.responses = []
        self.payload = {}
        self.stderr = StringIO()
        self.mb = lookup.musicbrainzngs.musicbrainz
        self.stack.enter_context(patch.dict(lookup.os.environ, {
            "MIXIE_MB_INTERVAL_SECONDS": "2", "MIXIE_MB_MAX_ATTEMPTS": "3", "MIXIE_MB_DIAGNOSTICS": "1"}))
        self.stack.enter_context(patch.object(lookup, "_musicbrainz_cooldown_until", 0.0))
        self.stack.enter_context(patch.object(lookup, "_musicbrainz_cooldown_status", None))
        self.stack.enter_context(patch.object(lookup.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(lookup.time, "time", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(lookup.time, "sleep", side_effect=self.sleep))
        self.stack.enter_context(patch.object(self.mb._mb_request, "remaining_requests", None))
        self.stack.enter_context(patch.object(self.mb._mb_request, "last_call", 0.0))
        for name in ("limit_interval", "limit_requests", "do_rate_limit"):
            self.stack.enter_context(patch.object(self.mb, name, getattr(self.mb, name)))
        self.opener = Mock()
        self.opener.open.side_effect = self.open
        self.stack.enter_context(patch.object(self.mb.compat, "build_opener", return_value=self.opener))
        self.stack.enter_context(patch.object(self.mb, "parser_fun", side_effect=lambda data: self.payload))
        self.stack.enter_context(redirect_stderr(self.stderr))
        for function in (lookup.get_recording_details, lookup.get_release_tracklist, lookup.get_release_metadata):
            function.cache_clear()
            self.addCleanup(function.cache_clear)

    def sleep(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.sleeps.append(seconds)
        self.clock += seconds

    def open(self, request, body=None, *, timeout):
        self.assertEqual(lookup.MUSICBRAINZ_TIMEOUT, timeout)
        self.wire.append((self.clock, request.full_url))
        self.clock += 0.1
        result = self.responses.pop(0) if self.responses else Response(b"synthetic response")
        if isinstance(result, BaseException):
            raise result
        return result

    def http_error(self, status, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        error = HTTPError("https://synthetic.invalid/SECRET-QUERY", status, "SECRET-ERROR", headers, BytesIO())
        self.addCleanup(error.close)
        return error

    def call(self):
        return lookup._musicbrainz_call(lookup.musicbrainzngs.get_recording_by_id, "synthetic-recording",
                                       includes=["artist-credits", "releases"])

    def events(self):
        return [json.loads(line.removeprefix("MIXIE_MB ")) for line in self.stderr.getvalue().splitlines()
                if line.startswith("MIXIE_MB ")]

    def test_503_recovers_with_a_coordinated_retry_and_no_following_request_burst(self):
        self.responses = [self.http_error(503), Response(b"ok"), Response(b"ok")]
        with lookup.musicbrainz_album() as stats:
            self.call()
            lookup._musicbrainz_call(lookup.musicbrainzngs.get_release_by_id, "release", includes=["recordings"])
        self.assertEqual(3, len(self.wire))
        self.assertTrue(all(b[0] - a[0] >= 1.997 for a, b in zip(self.wire, self.wire[1:])))
        self.assertEqual(3, stats["actual_attempts"])
        self.assertEqual(1, stats["failed_attempts"])
        self.assertEqual(1, stats["retries"])
        self.assertEqual({"recording_hydration": 2, "release_tracklist": 1}, stats["categories"])
        self.assertEqual({"http_503": 1}, stats["failures"])

    def test_retry_exhaustion_is_three_attempts_and_never_an_empty_success(self):
        self.responses = [self.http_error(503) for _ in range(3)]
        with lookup.musicbrainz_album() as stats:
            with self.assertRaises(lookup.musicbrainzngs.NetworkError):
                self.call()
        self.assertEqual(3, len(self.wire))
        self.assertEqual(2, stats["retries"])
        self.assertEqual(3, stats["http_failures"])
        waits = [row["delay_seconds"] for row in self.events() if row["event"] == "retry_wait"]
        self.assertEqual([2.0, 4.0], waits)

    def test_timeouts_and_transport_failures_are_separately_classified(self):
        for failure, kind in ((TimeoutError("SECRET timeout"), "timeout"),
                              (URLError(ConnectionResetError("SECRET transport")), "transport")):
            with self.subTest(kind=kind):
                self.responses = [failure, Response(b"ok")]
                with lookup.musicbrainz_album() as stats:
                    self.call()
                self.assertEqual({kind: 1}, stats["failures"])
                self.assertEqual(1, stats["retries"])

    def test_429_retry_after_is_respected_and_distinct_from_503(self):
        self.responses = [self.http_error(429, "7"), Response(b"ok")]
        with lookup.musicbrainz_album() as stats:
            self.call()
        self.assertGreaterEqual(self.wire[1][0] - self.wire[0][0], 7)
        self.assertEqual({"http_429": 1}, stats["failures"])
        self.assertTrue(any(row.get("delay_seconds") == 7 for row in self.events()))

    def test_retry_after_http_date_is_respected(self):
        date = format_datetime(datetime.fromtimestamp(self.clock + 9, timezone.utc), usegmt=True)
        self.responses = [self.http_error(503, date), Response(b"ok")]
        self.call()
        self.assertGreaterEqual(self.wire[1][0], 1009)

    def test_long_retry_after_defers_and_blocks_other_categories_without_early_requests(self):
        self.responses = [self.http_error(429, "120")]
        with self.assertRaises(lookup.musicbrainzngs.MusicBrainzError):
            self.call()
        with self.assertRaises(lookup.musicbrainzngs.MusicBrainzError):
            lookup._musicbrainz_call(lookup.musicbrainzngs.get_release_by_id, "release", includes=["recordings"])
        self.assertEqual(1, len(self.wire))
        self.assertFalse(any(delay > lookup.MAX_MUSICBRAINZ_RETRY_DELAY for delay in self.sleeps))

    def test_malformed_retry_after_does_not_leak_or_disable_bounded_backoff(self):
        self.responses = [self.http_error(503, "SECRET invalid header"), Response(b"ok")]
        self.call()
        self.assertEqual(2, len(self.wire))
        self.assertNotIn("SECRET", self.stderr.getvalue())

    def test_nontransient_http_errors_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                before = len(self.wire)
                self.responses = [self.http_error(status)]
                with self.assertRaises(lookup.musicbrainzngs.MusicBrainzError):
                    self.call()
                self.assertEqual(before + 1, len(self.wire))

    def test_malformed_xml_is_not_retried_or_called_a_transport_failure(self):
        self.mb.parser_fun.side_effect = ExpatError("SECRET malformed XML")
        with lookup.musicbrainz_album() as stats:
            with self.assertRaises(ExpatError):
                self.call()
        self.assertEqual(1, len(self.wire))
        self.assertEqual(0, stats["retries"])
        self.assertEqual({"malformed_data": 1}, stats["failures"])
        self.assertNotIn("SECRET", self.stderr.getvalue())

    def test_response_body_read_timeout_retries_and_closes_failed_response(self):
        failed = Response(b"unused")
        failed.read = Mock(side_effect=TimeoutError("synthetic body timeout"))
        self.responses = [failed, Response(b"ok")]
        self.call()
        self.assertTrue(failed.closed)
        self.assertEqual(2, len(self.wire))

    def test_response_size_limit_never_retries_or_succeeds(self):
        response = Response(b"x" * (lookup.MAX_MUSICBRAINZ_BYTES + 1))
        self.responses = [response]
        with self.assertRaises(ValueError):
            self.call()
        self.assertTrue(response.closed)
        self.assertEqual(1, len(self.wire))

    def test_mixed_api_paths_share_one_limiter_and_keep_request_categories(self):
        with lookup.musicbrainz_album() as stats:
            self.call()
            lookup._musicbrainz_call(lookup.musicbrainzngs.browse_releases, recording="recording", limit=100, offset=0)
            lookup._musicbrainz_call(lookup.musicbrainzngs.get_release_by_id, "release", includes=["recordings"])
            lookup._musicbrainz_call(lookup.musicbrainzngs.get_release_by_id, "release", includes=["artists"])
            lookup._musicbrainz_call(lookup.musicbrainzngs.get_release_by_id, "release", includes=["release-groups"])
            lookup._musicbrainz_call(lookup.musicbrainzngs.search_recordings, recording="SECRET-TRACK", limit=5)
            lookup._musicbrainz_call(lookup.musicbrainzngs.search_releases, release="SECRET-ALBUM", limit=5)
        self.assertEqual(7, stats["actual_attempts"])
        self.assertEqual(7, len(stats["categories"]))
        self.assertTrue(all(b[0] - a[0] >= 1.997 for a, b in zip(self.wire, self.wire[1:])))
        self.assertNotIn("SECRET", self.stderr.getvalue())

    def test_diagnostics_are_disabled_by_default_and_never_contain_request_secrets(self):
        with patch.dict(lookup.os.environ, {"MIXIE_MB_DIAGNOSTICS": "0"}):
            with lookup.musicbrainz_album():
                self.call()
        self.assertEqual("", self.stderr.getvalue())
        self.responses = [self.http_error(503), Response(b"ok")]
        with lookup.musicbrainz_album():
            lookup._musicbrainz_call(lookup.musicbrainzngs.search_recordings,
                                    artist="SECRET-ARTIST", recording="SECRET-TITLE", limit=5)
        output = self.stderr.getvalue()
        for forbidden in ("SECRET", "synthetic-recording", "https://", "Authorization", "ACOUSTID_CLIENT_KEY"):
            self.assertNotIn(forbidden, output)
        attempts = [row for row in self.events() if row["event"] == "http_attempt"]
        self.assertEqual(2, len(attempts))
        self.assertEqual([0, 1], [row["retry_number"] for row in attempts])
        self.assertEqual([503, 200], [row["http_status"] for row in attempts])
        self.assertTrue(all(row["duration_seconds"] >= 0 and "timestamp" in row for row in attempts))

    def test_album_scope_summarizes_interrupts_and_does_not_swallow_them(self):
        self.responses = [KeyboardInterrupt()]
        original = self.mb._safe_read
        with self.assertRaises(KeyboardInterrupt):
            with lookup.musicbrainz_album():
                self.call()
        self.assertIsNone(lookup.current_musicbrainz_album())
        self.assertIs(original, self.mb._safe_read)
        summaries = [row for row in self.events() if row["event"] == "album_summary"]
        self.assertEqual("interrupted", summaries[-1]["outcome"])
        self.assertEqual(1, summaries[-1]["actual_attempts"])

    def test_cached_success_counts_zero_new_http_attempts(self):
        self.payload = {"recording": {"id": "recording", "title": "Song", "release-count": 0, "release-list": []}}
        with lookup.musicbrainz_album() as first:
            lookup.get_recording_details("recording")
        with lookup.musicbrainz_album() as second:
            lookup.get_recording_details("recording")
        self.assertEqual(1, first["actual_attempts"])
        self.assertEqual(0, second["actual_attempts"])

    def test_adjustable_interval_and_attempt_limit_apply_without_a_second_limiter(self):
        with patch.dict(lookup.os.environ, {"MIXIE_MB_INTERVAL_SECONDS": "1", "MIXIE_MB_MAX_ATTEMPTS": "1"}):
            self.responses = [self.http_error(503), Response(b"ok")]
            with self.assertRaises(lookup.musicbrainzngs.NetworkError):
                self.call()
            self.call()
        self.assertEqual(2, len(self.wire))
        self.assertGreaterEqual(self.wire[1][0] - self.wire[0][0], 0.999)
        self.assertLess(self.wire[1][0] - self.wire[0][0], 1.01)

    def test_invalid_settings_fail_before_any_http_attempt_without_echoing_values(self):
        for name, value in (("MIXIE_MB_INTERVAL_SECONDS", "nan"), ("MIXIE_MB_INTERVAL_SECONDS", "0.5"),
                            ("MIXIE_MB_MAX_ATTEMPTS", "4"), ("MIXIE_MB_MAX_ATTEMPTS", "SECRET-invalid")):
            with self.subTest(name=name, value=value), patch.dict(lookup.os.environ, {name: value}):
                with self.assertRaises(ValueError) as caught:
                    self.call()
                self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual([], self.wire)
