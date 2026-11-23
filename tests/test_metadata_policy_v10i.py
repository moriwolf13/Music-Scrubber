"""Supported release dates and explicitly unresolved genre in bound reviews."""
from copy import deepcopy
from unittest.mock import patch

import lookup
import mixie
import mixie_review_ui as ui
import tag
from test_verified_numbering_review_v10i import VerifiedNumberingReviewTests


class MetadataPolicyTests(VerifiedNumberingReviewTests):
    def test_original_release_group_date_wins_over_identical_later_retail_dates(self):
        retail = {'CD': {'release_group_id': 'same',
                         'original_release_date': '2021-08-27',
                         'date': '2023-01-01', 'album': 'NEED',
                         'album_artist': '3OH!3'},
                  'Digital': {'release_group_id': 'same',
                              'original_release_date': '2021-08-27',
                              'date': '2023-01-01', 'album': 'NEED',
                              'album_artist': '3OH!3'}}
        with patch.object(lookup, 'get_release_metadata', side_effect=lambda key: retail[key]):
            common = lookup.common_release_metadata(['CD', 'Digital'])
        self.assertEqual('2021-08-27', common['date'])

    def test_different_original_group_dates_are_not_guessed(self):
        retail = {'A': {'release_group_id': 'same', 'original_release_date': '2021',
                        'date': '2023-01-01', 'album': 'NEED', 'album_artist': '3OH!3'},
                  'B': {'release_group_id': 'same', 'original_release_date': '2022',
                        'date': '2024-01-01', 'album': 'NEED', 'album_artist': '3OH!3'}}
        with patch.object(lookup, 'get_release_metadata', side_effect=lambda key: retail[key]):
            common = lookup.common_release_metadata(['A', 'B'])
        self.assertNotIn('date', common)

    def test_missing_auto_genre_remains_unset(self):
        album = {'tracks': [{'date': (), 'genre': (), 'album': ('NEED',)}]}
        fields = mixie._auto_album_metadata(album, {'album': 'NEED'}, {'tracklist': [{}]})
        self.assertNotIn('genre', fields)

    def test_review_recovers_matched_album_metadata_without_inventing_genre(self):
        issue, rows = self._issue()
        stored = issue['details']['verified_release_numbering']['album_metadata']
        self.assertEqual({'album_artist': '3OH!3', 'date': '2024'}, stored)
        for row in rows.values():
            row['album_artist'] = ()
            row['date'] = ()
            row['genre'] = ()
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual('3OH!3', summary['album_artist'])
        self.assertEqual('2024', summary['date'])
        self.assertEqual('Not set', summary['genre'])
        problems = ui.preflight_issues(mixie, issue)
        self.assertFalse(any('Release Date missing' in item or 'Genre missing' in item for item in problems))
        self.assertTrue(any('Metadata conflict' in item for item in problems))
        reviewed = ui._build_user_interpretation(mixie, issue)
        self.assertEqual('2024', reviewed['date'])
        self.assertNotIn('genre', reviewed)
        self.assertEqual('3OH!3', reviewed['album_artist'])
        guest = next(track for name, track in reviewed['tracks'].items() if name.endswith('3.flac'))
        self.assertEqual('Other Act', guest['artist'])

    def test_stale_album_metadata_evidence_cannot_autofill(self):
        issue, rows = self._issue()
        for row in rows.values():
            row['album_artist'] = ()
            row['date'] = ()
            row['genre'] = ()
        issue['details']['verified_release_numbering']['source_fingerprint'] = 'f' * 64
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual('Not set', summary['date'])
        self.assertEqual('Not set', summary['genre'])

    def test_explicit_date_choice_wins_over_matched_metadata(self):
        issue, rows = self._issue()
        for row in rows.values():
            row['date'] = ()
        issue['manual_fields'] = {'date': '2020-01-02'}
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual('2020-01-02', summary['date'])
