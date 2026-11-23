"""Standard US digital representation fallback; synthetic, offline, no live audio."""
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import lookup
import mixie


class StandardUSEditionTests(TestCase):
    TITLES = (
        'Back From the Dead', 'Wicked Ways', 'Strange Girl', 'Brightside',
        'The Steeple', 'Terrible Things', 'My Redemption', 'Bombshell',
        'I Come First', 'Psycho Crazy', 'Raise Your Horns',
    )

    def fixtures(self, titles=None, release_ids=('standard-us',)):
        titles = tuple(titles or self.TITLES)
        memberships = [{'id': release_id} for release_id in release_ids]
        audit, tracks, local = [], [], []
        for i, title in enumerate(titles, 1):
            name = Path(f'fictional-audio-{i}.flac')
            canonical = f'edition-{i}'
            aliases = [
                {'recording_id': canonical, 'status': 'verified', 'reasons': [],
                 'details': {'id': canonical, 'title': title,
                             'disambiguation': '', 'release-list': deepcopy(memberships),
                             'release-list-complete': True}},
                {'recording_id': f'alias-{i}', 'status': 'verified', 'reasons': [],
                 'details': {'id': f'alias-{i}', 'title': title,
                             'disambiguation': '', 'release-list': deepcopy(memberships),
                             'release-list-complete': True}},
            ]
            if i == len(titles):
                # In the real saved Back From the Dead evidence, an unrelated
                # duration-unknown result was only probable, not audio-verified.
                aliases.append({'recording_id': 'unrelated', 'status': 'probable',
                                'reasons': ['recording_duration_unavailable'],
                                'details': {'id': 'unrelated', 'title': 'Unrelated Song'}})
            audit.append({'path': name, 'status': 'needs_review', 'details': None,
                          'assessment': {'status': 'conflicting', 'complete': True,
                                         'errors': [], 'candidates': aliases}})
            tracks.append({'disc': '1', 'number': str(i), 'recording_id': canonical,
                           'title': title, 'recording_title': title,
                           'medium_format': 'Digital Media'})
            # Existing numbers are untrusted and deliberately shuffled.
            local.append({'path': name, 'track_number': (str(len(titles)-i+1),),
                          'track_total': (str(len(titles)),),
                          'disc_number': (), 'disc_total': ()})
        return audit, tracks, local

    def evaluate(self, audit, tracks, local, release_id='standard-us'):
        with patch.object(lookup, 'get_release_tracklist', return_value=tracks):
            return lookup.evaluate_release_candidate(audit, release_id, local_tracks=local)

    def test_ambiguous_on_release_ids_map_all_11_audio_titles_and_repair_order(self):
        audits, tracks, local = self.fixtures()
        result = self.evaluate(audits, tracks, local)
        self.assertEqual(11, len(result['matched']))
        self.assertIn('resolved_by_audio_verified_title', result['reasons'])
        self.assertEqual({(1, i): Path(f'fictional-audio-{i}.flac') for i in range(1, 12)}, result['matched'])
        self.assertTrue(any(reason.startswith('local_track_number') for reason in result['reasons']))
        self.assertEqual('complete', lookup.compare_release_candidate_reports(
            audits, [result], standard_album_preference=True)['status'])

    def test_twelve_distinct_ambiguous_audio_titles_map_without_guessing_ids(self):
        titles = tuple(f'Song {i}' for i in range(1, 13))
        audits, tracks, local = self.fixtures(titles)
        result = self.evaluate(audits, tracks, local)
        self.assertEqual(12, len(result['matched']))
        self.assertEqual('complete', lookup.compare_release_candidate_reports(
            audits, [result], standard_album_preference=True)['status'])

    def test_standard_prefers_official_us_digital_over_cd_or_worldwide(self):
        audits, tracks, local = self.fixtures(('First', 'Second', 'Third'))
        def case(ident, form):
            return (ident, {'tracklist': [{**row, 'medium_format': form} for row in tracks]}, ())
        choices = [case('a-us-cd', 'CD'), case('b-worldwide-digital', 'Digital Media'),
                   case('c-us-digital', 'Digital Media')]
        metadata = {'a-us-cd': {'country': 'US', 'status': 'Official'},
                    'b-worldwide-digital': {'country': 'XW', 'status': 'Official'},
                    'c-us-digital': {'country': 'US', 'status': 'Official'}}
        with patch.object(lookup, 'get_release_metadata', side_effect=metadata.__getitem__):
            self.assertEqual('c-us-digital', mixie._preferred_music_release(
                choices, local, standard_us_digital=True)[0])
        self.assertEqual('a-us-cd', mixie._preferred_music_release(choices, local)[0])

    def test_deluxe_prefix_same_songs_is_not_a_competing_music_version(self):
        audits, tracks, local = self.fixtures(release_ids=('standard-us', 'deluxe'))
        standard = self.evaluate(audits, tracks, local)
        deluxe_tracks = deepcopy(tracks) + [
            {'disc': '1', 'number': '12', 'recording_id': 'bonus',
             'title': 'New Bonus Track', 'recording_title': 'New Bonus Track'}]
        deluxe = self.evaluate(audits, deluxe_tracks, local, release_id='deluxe')
        self.assertEqual('partial', lookup.compare_release_candidate_reports(
            audits, [standard, deluxe])['status'])
        self.assertEqual('complete', lookup.compare_release_candidate_reports(
            audits, [standard, deluxe], standard_album_preference=True)['status'])

    def test_different_song_order_and_live_version_cannot_be_suppressed(self):
        for kind in ('reorder', 'live'):
            audits, tracks, local = self.fixtures(release_ids=('standard-us', kind))
            standard = self.evaluate(audits, tracks, local)
            wrong = deepcopy(tracks)
            if kind == 'reorder':
                wrong[0], wrong[1] = wrong[1], wrong[0]
            else:
                wrong[0]['title'] = 'Back From the Dead (live)'
            wrong.append({'disc': '1', 'number': '12', 'recording_id': 'bonus',
                          'title': 'Bonus', 'recording_title': 'Bonus'})
            alternative = self.evaluate(audits, wrong, local, release_id=kind)
            result = lookup.compare_release_candidate_reports(
                audits, [standard, alternative], standard_album_preference=True)
            with self.subTest(kind=kind):
                self.assertNotEqual('complete', result['status'])

    def test_unexamined_retail_stays_unsafe_even_with_preferred_edition(self):
        audits, tracks, local = self.fixtures(release_ids=('standard-us', 'unknown-retail'))
        standard = self.evaluate(audits, tracks, local)
        result = lookup.compare_release_candidate_reports(
            audits, [standard], unexamined_release_ids=['unknown-retail'],
            standard_album_preference=True)
        self.assertNotEqual('complete', result['status'])

    def test_song_title_live_verb_is_not_live_version(self):
        self.assertEqual(frozenset(), lookup._material_recording_version(
            {'title': 'Long Live Rock'}))
        self.assertEqual(frozenset(), lookup._material_recording_version(
            {'title': 'Can You Live Forever'}))
        self.assertEqual(frozenset({'live'}), lookup._material_recording_version(
            {'title': 'Long Live Rock (Live at Brixton)'}))

    def test_explicit_source_edition_never_uses_generic_us_tie_break(self):
        for title in ('The Strange Case of... (Deluxe Edition)',
                      'Americana (ESCA 7393)', 'Album (Live)', 'Album — Acoustic'):
            with self.subTest(title=title):
                self.assertTrue(mixie._source_names_explicit_edition(title))
        self.assertFalse(mixie._source_names_explicit_edition('Back From the Dead'))

    def test_verified_different_recording_versions_remain_unresolved(self):
        audits, tracks, local = self.fixtures()
        # A second AcoustID/MB hit claiming a live take cannot be decided by
        # the standard US digital tracklist, regardless of catalog preference.
        audits[3]['assessment']['candidates'][1]['details']['disambiguation'] = 'live'
        result = self.evaluate(audits, tracks, local)
        self.assertNotEqual('complete', result['status'])

    def test_single_verified_and_unknown_timed_competitor_still_requires_review(self):
        audits, tracks, local = self.fixtures()
        audits[-1]['assessment']['candidates'] = [
            audits[-1]['assessment']['candidates'][0],
            audits[-1]['assessment']['candidates'][2],
        ]
        result = self.evaluate(audits, tracks, local)
        self.assertNotEqual('complete', result['status'])
