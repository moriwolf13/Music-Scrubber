"""User-requested primary-artist defaults and provider-outage local totals.

These are review hints, not independent recording evidence or cleanup permission.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import mixie
import mixie_review_ui as ui
import tag
from support import WorkspaceCase, metadata_record


class PrimaryArtistAndLocalTotalsTests(WorkspaceCase):
    def _review(self, numbers=range(1, 15), *, kind='provider_unavailable',
                artists=None, discs=None):
        tracks, rows = [], {}
        for position, number in enumerate(numbers):
            path = self.track_path(f'Panic/track-{position + 1}.flac', f'untouched audio {position}'.encode())
            relative = path.relative_to(self.stage).as_posix()
            tracks.append(relative)
            rows[path] = metadata_record(path, title=(f'Song {position + 1}',),
                artist=(artists[position] if artists else 'Caravan Palace',),
                album=('Panic',), album_artist=(), date=('2012',), genre=('Unknown',),
                track_number=(str(number),), track_total=(),
                disc_number=((str(discs[position]),) if discs else ()), disc_total=())
        issue = {'album': 'Panic', 'import_name': 'Panic', 'tracks': tracks,
                 'reason': 'Online verification was interrupted',
                 'details': {'kind': kind, 'releases': [], 'tracks': []}}
        self.stack.enter_context(patch.object(tag, 'read_track_metadata',
            side_effect=lambda path: deepcopy(rows[Path(path)])))
        return issue, rows

    def test_panic_primary_album_artist_and_local_contiguous_total_are_effective_defaults(self):
        issue, rows = self._review()
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual(summary['album_artist'], 'Caravan Palace')
        self.assertTrue(summary['album_artist_from_tracks'])
        self.assertEqual(summary['track_total'], '14')
        self.assertTrue(summary['track_total_from_local_sequence'])
        preflight = ui.preflight_issues(mixie, issue)
        self.assertFalse(any('Album Artist' in problem or 'Track Total missing' in problem
                             for problem in preflight))
        self.assertTrue(any('Online verification' in problem for problem in preflight))
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual(decision['album_artist'], 'Caravan Palace')
        self.assertEqual({t['track_total'] for t in decision['totals'].values()}, {14})
        self.assertFalse(decision['independently_verified'])
        self.assertTrue(all(not row['album_artist'] and not row['track_total'] for row in rows.values()))

    def test_offline_approval_discloses_provisional_total_and_is_not_an_import(self):
        from contextlib import redirect_stdout
        from io import StringIO
        issue, _ = self._review()
        with patch.object(ui, 'input', return_value='0'), redirect_stdout(StringIO()) as output:
            ui._show_album(mixie, issue, [issue])
        self.assertIn('local contiguous sequence; not release-verified', output.getvalue())
        self.assertIn('inferred from primary track artists', output.getvalue())
        with patch.object(ui, 'input', return_value='APPROVE'), \
             patch.object(mixie, 'save_review_queue'), redirect_stdout(StringIO()):
            success = ui._approve_review(mixie, issue, [issue])
        self.assertTrue(success)
        self.assertEqual('reviewed', issue['review_status'])
        self.assertFalse(issue['interpretation']['independently_verified'])
        self.assertEqual({}, mixie._verified_imports())

    def test_local_contiguous_total_is_provisional_even_with_identity_conflict(self):
        issue, _ = self._review(kind='identity_release')
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual('14', summary['track_total'])
        self.assertTrue(summary['track_total_from_local_sequence'])
        self.assertTrue(any('Release/recording evidence' in p for p in ui.preflight_issues(mixie, issue)))
        self.assertFalse(any('Track Total missing' in p for p in ui.preflight_issues(mixie, issue)))

    def test_local_file_count_never_becomes_proof_without_complete_sequence(self):
        for numbers, kind in (([1, 2, 4], 'provider_unavailable'),
                              ([1, 2, 2], 'provider_unavailable'),
                              ([2, 3, 4], 'provider_unavailable')):
            with self.subTest(numbers=list(numbers), kind=kind):
                issue, _ = self._review(numbers, kind=kind)
                summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
                self.assertEqual('Not confirmed', summary['track_total'])
                self.assertTrue(any('Track Total missing' in p for p in ui.preflight_issues(mixie, issue)))

    def test_full_sequence_overrides_stale_smaller_total_as_provisional(self):
        issue, rows = self._review(numbers=range(1, 15), kind='identity_release')
        for metadata in rows.values():
            metadata['track_total'] = ('13',)
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        # An inconsistent catalog/source total is not proof against 14 tracks.
        self.assertEqual('14', summary['track_total'])  # contradicting source tag does not suppress local default
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual({14}, {v['track_total'] for v in decision['totals'].values()})

    def test_multiple_disc_markers_never_infer_one_disc_total(self):
        issue, _ = self._review([1, 2, 3], discs=[1, 2, 2])
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertNotEqual('3', summary['track_total'])

    def test_mixed_primary_artists_still_require_album_artist_review(self):
        issue, _ = self._review([1, 2, 3, 4],
            artists=['A', 'B', 'A', 'B'], kind='provider_unavailable')
        summary = ui._album_summary(mixie, issue, ui._view(mixie, issue))
        self.assertEqual('Not set', summary['album_artist'])
        self.assertTrue(any('Album Artist' in problem for problem in ui.preflight_issues(mixie, issue)))
