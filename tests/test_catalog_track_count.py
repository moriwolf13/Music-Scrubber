"""Audio-count discovery safeguards; synthetic provider responses only."""
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import lookup
import mixie


def ident(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def summary(identifier, count, form='CD'):
    return {'id': identifier, 'medium-count': '1', 'medium-list': [
        {'position': '1', 'format': form, 'track-count': str(count)}]}


class CatalogAudioTrackCountTests(TestCase):
    def tracks(self, count=13):
        return [{'track_number': [str(i)], 'track_total': [str(count)],
                 'disc_number': [], 'disc_total': []} for i in range(1, count + 1)]

    def test_americana_complete_thirteen_has_count_corrobation(self):
        self.assertTrue(mixie._confirmed_complete_single_disc(self.tracks(), 13))

    def test_source_is_not_complete_when_tags_conflict_duplicate_or_missing(self):
        for mutation in ('duplicate', 'badtotal', 'missing', 'multidisc'):
            with self.subTest(mutation=mutation):
                tracks = self.tracks()
                if mutation == 'duplicate':
                    tracks[0]['track_number'] = ['2']
                elif mutation == 'badtotal':
                    tracks[0]['track_total'] = ['14']
                elif mutation == 'missing':
                    tracks[0]['track_number'] = []
                else:
                    tracks[0]['disc_number'] = ['2']
                self.assertFalse(mixie._confirmed_complete_single_disc(tracks, 13))

    def test_indexed_batch_drops_only_confirmed_incompatible_non_enhanced_counts(self):
        ids = [ident(i) for i in range(1, 6)]
        seen = []
        def provider(operation, *args, **kwargs):
            self.assertEqual('search_releases', operation.__name__)
            self.assertEqual(5, kwargs['limit'])
            self.assertEqual(5, kwargs['query'].count('reid:'))
            seen.append(kwargs['query'])
            return {'release-list': [summary(ids[0], 13), summary(ids[1], 14),
                summary(ids[2], 23, 'Enhanced CD'), summary(ids[3], 2, 'Digital Media'),
                summary(ids[4], 13, 'Enhanced CD')]}
        with patch.object(lookup, '_musicbrainz_call', side_effect=provider):
            retained, dropped = lookup.filter_release_ids_by_audio_track_count(ids, 13)
        self.assertEqual([ids[0],ids[2],ids[4]], retained)
        self.assertEqual([ids[1],ids[3]], dropped)
        self.assertEqual(1, len(seen))

    def test_unknown_provider_counts_or_search_outage_never_exclude(self):
        ids = [ident(i) for i in range(1,4)]
        for payload in ({'release-list': [{'id': ids[0]}]},
                        {'release-list': [summary(ids[0], 23, 'Enhanced CD')]},
                        {'release-list': []}):
            with self.subTest(payload=payload):
                with patch.object(lookup, '_musicbrainz_call', return_value=payload):
                    self.assertEqual((ids, []), lookup.filter_release_ids_by_audio_track_count(ids, 13))
        with patch.object(lookup, '_musicbrainz_call', side_effect=OSError('Provider offline')):
            self.assertEqual((ids, []), lookup.filter_release_ids_by_audio_track_count(ids, 13))

    def test_multidisc_releases_cannot_be_excluded_by_aggregate_track_count(self):
        release = {'medium-count':'2','medium-list':[
            {'position':'1','format':'CD','track-count':'13'},
            {'position':'2','format':'CD','track-count':'8'}]}
        self.assertIsNone(lookup._search_hit_audio_count(release, 13))

    def test_unknown_or_partial_album_cannot_infer_release_from_count(self):
        self.assertFalse(mixie._confirmed_complete_single_disc(self.tracks()[:12], 12))
        self.assertFalse(mixie._confirmed_complete_single_disc(self.tracks(2), 2))
        self.assertIsNone(lookup._search_hit_audio_count({'medium-list': []},13))

    def test_malformed_or_synthetic_references_are_not_filtered(self):
        ids = ['made-up-edition', ident(1)]
        with patch.object(lookup, '_musicbrainz_call') as provider:
            self.assertEqual((ids, []), lookup.filter_release_ids_by_audio_track_count(ids, 13))
            provider.assert_not_called()

    def test_provider_results_outside_batch_cannot_remove_candidates(self):
        ids = [ident(i) for i in range(1,4)]
        with patch.object(lookup, '_musicbrainz_call', return_value={
            'release-list': [summary(ident(99), 2), summary(ids[1], 13)]}):
            self.assertEqual((ids, []), lookup.filter_release_ids_by_audio_track_count(ids, 13))

    def test_unbounded_candidate_sets_are_not_silently_shortened(self):
        ids = [ident(i) for i in range(1, lookup.MAX_COUNT_SEARCH_IDS+2)]
        with patch.object(lookup, '_musicbrainz_call') as provider:
            self.assertEqual((ids, []), lookup.filter_release_ids_by_audio_track_count(ids, 13))
            provider.assert_not_called()
