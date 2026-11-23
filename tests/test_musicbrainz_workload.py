"""Retain successful MusicBrainz subrequests without caching failed evidence."""
from copy import deepcopy
from unittest.mock import patch

from support import WorkspaceCase
import lookup


class MusicBrainzWorkloadTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.page_failures = 1
        self.recording = {"id": "recording", "title": "Song", "length": "200000", "release-count": 101,
                          "release-list": [{"id": "release-100"}]}
        for function in (lookup.get_recording_details, lookup.get_release_tracklist, lookup.get_release_metadata):
            function.cache_clear()
            self.addCleanup(function.cache_clear)
        self.stack.enter_context(patch.object(lookup, "_musicbrainz_call", side_effect=self.provider))

    def provider(self, operation, *args, **kwargs):
        self.calls.append((operation.__name__, args, kwargs))
        if operation.__name__ == "get_recording_by_id":
            return {"recording": deepcopy(self.recording)}
        if operation.__name__ == "browse_releases":
            offset = kwargs["offset"]
            if offset == 100 and self.page_failures:
                self.page_failures -= 1
                raise TimeoutError("synthetic membership page outage")
            return {"release-count": 101, "release-list": [
                {"id": f"release-{index}"} for index in range(offset, min(offset + 100, 101))]}
        if operation.__name__ == "get_release_by_id":
            return {"release": {"id": "release", "title": "Album", "artist-credit-phrase": "Artist",
                                "release-group": {"primary-type": "Single"}}}
        raise AssertionError("Unexpected synthetic request")

    def test_second_page_outage_retries_only_failed_subrequest_without_rehydrating_recording(self):
        with self.assertRaises(TimeoutError):
            lookup.get_recording_details("recording")
        details = lookup.get_recording_details("recording")
        self.assertTrue(details["release-list-complete"])
        self.assertEqual(101, len(details["release-list"]))
        self.assertEqual(1, sum(name == "get_recording_by_id" for name, _, _ in self.calls))
        self.assertEqual([0, 100, 100], [options["offset"] for name, _, options in self.calls if name == "browse_releases"])

    def test_cached_parts_and_final_details_are_defensive_and_clear_together(self):
        with self.assertRaises(TimeoutError):
            lookup.get_recording_details("recording")
        details = lookup.get_recording_details("recording")
        details["title"] = "caller edit"
        details["release-list"].clear()
        self.assertEqual("Song", lookup.get_recording_details("recording")["title"])
        lookup.get_recording_details.cache_clear()
        self.recording["title"] = "Fresh provider title"
        fresh = lookup.get_recording_details("recording")
        self.assertEqual("Fresh provider title", fresh["title"])
        self.assertEqual(2, sum(name == "get_recording_by_id" for name, _, _ in self.calls))
        self.assertEqual([0, 100, 100, 0, 100], [options["offset"] for name, _, options in self.calls if name == "browse_releases"])

    def test_malformed_page_is_not_cached_and_can_be_repaired_without_refetching_good_page(self):
        self.page_failures = 0
        provider = self.provider
        malformed = [True]

        def respond(operation, *args, **kwargs):
            result = provider(operation, *args, **kwargs)
            if operation.__name__ == "browse_releases" and kwargs["offset"] == 100 and malformed[0]:
                malformed[0] = False
                return {"release-count": 101, "release-list": [None]}
            return result

        with patch.object(lookup, "_musicbrainz_call", side_effect=respond):
            with self.assertRaises(ValueError):
                lookup.get_recording_details("recording")
            result = lookup.get_recording_details("recording")
        self.assertTrue(result["release-list-complete"])
        self.assertEqual([0, 100, 100], [options["offset"] for name, _, options in self.calls if name == "browse_releases"])

    def test_metadata_requests_release_type_once_instead_of_separate_type_lookup(self):
        metadata = lookup.get_release_metadata("release")
        self.assertEqual("Single", metadata["release_type"])
        self.assertEqual([("get_release_by_id", ("release",), {"includes": ["artists", "release-groups"]})], self.calls)
        metadata["release_type"] = "caller mutation"
        self.assertEqual("Single", lookup.get_release_metadata("release")["release_type"])
        self.assertEqual(1, len(self.calls))

    def test_unknown_membership_stays_unknown_even_when_reusing_good_subrequests(self):
        self.recording.pop("release-count")
        result = lookup.get_recording_details("recording")
        self.assertFalse(result["release-list-complete"])
        self.assertEqual(1, len(self.calls))

    def test_mismatched_recording_identity_is_never_cached_as_valid(self):
        self.recording["id"] = "wrong"
        with self.assertRaises(ValueError):
            lookup.get_recording_details("recording")
        self.recording["id"] = "recording"
        self.page_failures = 0
        result = lookup.get_recording_details("recording")
        self.assertTrue(result["release-list-complete"])
        self.assertEqual(2, sum(name == "get_recording_by_id" for name, _, _ in self.calls))
