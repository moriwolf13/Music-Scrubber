"""Offline regressions for album consensus and consolidated, readable review."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import mixie
import review
import tag


class AlbumConsensusTests(TestCase):
    def test_shared_original_album_artist_fills_only_missing_tracks(self):
        a, b, c = (Path('/tmp/Song-One.flac'), Path('/tmp/Song-Two.flac'), Path('/tmp/Song-Three.flac'))
        metadata = {a: {'path': a, 'album': ('My Album',), 'album_artist': ('My Artist',)},
                    b: {'path': b, 'album': ('My Album',), 'album_artist': ()},
                    c: {'path': c, 'album': (), 'album_artist': ()}}
        with patch.object(tag, 'read_track_metadata', side_effect=lambda path: metadata[Path(path)]):
            proposals = tag.propose_consistent_album_tags({('1','1'): a, ('1','2'): b, ('1','3'): c}, {})
        self.assertEqual({(str(p['path']), p['field'], p['value']) for p in proposals}, {
            (str(b), 'album_artist', 'My Artist'),
            (str(c), 'album_artist', 'My Artist'), (str(c), 'album', 'My Album')})
        self.assertTrue(all(p['verified'] is False for p in proposals))

    def test_conflicting_values_and_remote_common_never_silently_override(self):
        a, b = Path('/tmp/Artist-A.flac'), Path('/tmp/Artist-B.flac')
        metadata = {a: {'path': a, 'album': ('Edition A',), 'album_artist': ('A',)},
                    b: {'path': b, 'album': ('Edition B',), 'album_artist': ()}}
        with patch.object(tag, 'read_track_metadata', side_effect=lambda path: metadata[Path(path)]):
            proposals = tag.propose_consistent_album_tags({('1','1'): a, ('1','2'): b}, {'album_artist':'Remote Credit'})
        self.assertEqual([], proposals)

    def test_missing_everywhere_does_not_invent_album_artist_from_track_artist(self):
        a = Path('/tmp/Song.flac')
        metadata = {'path': a, 'album': (), 'album_artist': (), 'artist': ('Singer',)}
        with patch.object(tag, 'read_track_metadata', return_value=metadata):
            self.assertEqual([], tag.propose_consistent_album_tags({('1','1'): a}, {}))


class CompactReviewTests(TestCase):
    def test_large_16_track_65_release_evidence_is_compact_and_not_discarded(self):
        rows = []
        for n in range(16):
            status = 'conflicting' if n < 6 else 'verified'
            rows.append({'path': f'Import/Streets/{n:02d}.mp3',
                         'existing': {'title': (f'Song {n}',), 'artist': ('3OH!3',)},
                         'assessment': {'status': status},
                         'evidence': {},
                         'candidates': []})
        details = {'tracks': rows,
                   'releases': [{'release_id': f'R-{n:03d}', 'tracklist': [], 'reasons': []} for n in range(65)]}
        before = repr(details)
        stream = StringIO()
        with redirect_stdout(stream):
            review._show_evidence(details)
        out = stream.getvalue()
        self.assertIn('16 tracks; 6 recording identities need attention', out)
        self.assertIn('65', out)
        self.assertLess(len(out.splitlines()), 55)
        self.assertNotIn('Album release suggestion 65:', out)
        self.assertEqual(before, repr(details))

    def test_release_navigation_shows_only_eight_at_once_and_keeps_all_selectable(self):
        details = {'releases': [{'release_id': f'known-release-{n:03d}', 'tracklist': [],
                                 'missing': [], 'status': 'partial'} for n in range(65)]}
        issue = {'details': details}
        out = StringIO()
        with patch.object(review, '_ask', side_effect=['n', 'b', 'back']), redirect_stdout(out):
            review._review_release(issue, details, {}, lambda _: None)
        output = out.getvalue()
        self.assertIn('page 1/9', output)
        self.assertIn('page 2/9', output)
        self.assertIn('9. Unlabeled release', output)
        self.assertNotIn('30. Unlabeled release', output)

    def test_offline_editor_saves_genre_without_processing_or_network(self):
        issue = {'album':'My Album', 'tracks':['MyImport/song.flac'], 'import_name':'MyImport',
                 'details': {'kind':'required_metadata', 'fields':['genre']}}
        row = {'path': Path('/tmp/song.flac'), 'album': ('My Album',),
               'album_artist': ('My Artist',), 'date': ('2020',), 'genre': (),
               'track_number': ('1',), 'track_total': ('1',),
               'disc_number': ('1',), 'disc_total': ('1',)}
        captured = []
        with patch.object(mixie, 'review_fingerprint', return_value='SYNTHETIC-HASH'), \
             patch.object(mixie, '_offline_album_review_rows', return_value=[row]), \
             patch.object(mixie, 'save_review_queue', side_effect=lambda issues: captured.append(issues[0]['manual_fields'].copy())), \
             patch.object(mixie, 'finalize_reviewed_album', side_effect=AssertionError('Must not process')), \
             patch('builtins.input', side_effect=['2', 'Electro Swing', '0']), \
             redirect_stdout(StringIO()):
            mixie._edit_offline_shared_metadata(issue, [issue])
        self.assertEqual(issue['manual_fields'], {'genre': 'Electro Swing'})
        self.assertEqual(issue['source_fingerprint'], 'SYNTHETIC-HASH')
        self.assertEqual(captured, [{'genre':'Electro Swing'}])

    def test_offline_editor_refuses_stale_source_before_saving(self):
        issue = {'album':'My Album', 'tracks':['MyImport/song.flac'], 'import_name':'MyImport', 'details':{}}
        row = {'path': Path('/tmp/song.flac'), 'album':(), 'album_artist':(), 'date':(), 'genre':(),
               'track_number':(), 'track_total':(), 'disc_number':(), 'disc_total':()}
        with patch.object(mixie, 'review_fingerprint', side_effect=['FIRST','CHANGED']), \
             patch.object(mixie, '_offline_album_review_rows', return_value=[row]), \
             patch.object(mixie, 'save_review_queue', side_effect=AssertionError('Must not save')), \
             patch('builtins.input', side_effect=['2', 'Electronic']), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                mixie._edit_offline_shared_metadata(issue, [issue])
        self.assertNotIn('manual_fields', issue)

class BatchSupportedReviewTests(TestCase):
    def test_batch_accepts_only_unique_verified_matches_at_unique_release_positions(self):
        def row(name, identifier, status='verified', alternate=False):
            candidate = {'recording_id': identifier, 'status': 'verified',
                         'details': {'id': identifier, 'title': name, 'artist-credit-phrase': '3OH!3'}}
            options = [candidate]
            if alternate:
                options.append({'recording_id': identifier + '-clean', 'status': 'verified',
                                'details': {'id': identifier + '-clean', 'title': name + ' (clean)',
                                            'artist-credit-phrase': '3OH!3'}})
            return {'path': f'Import/{name}.mp3', 'assessment': {'status': status, 'complete':True,
                                                                'candidates': options}}
        rows = [row('Beaumont', 'rec-1'), row('House Party', 'rec-2', alternate=True),
                row('Déjà Vu', 'rec-3', status='conflicting')]
        state = {'release_id': 'release-1', 'release_tracks': [
            {'recording_id':'rec-1', 'disc':'1', 'number':'1', 'title':'Beaumont'},
            {'recording_id':'rec-2', 'disc':'1', 'number':'2', 'title':'House Party'},
            {'recording_id':'rec-3', 'disc':'1', 'number':'3', 'title':'Déjà Vu'}],
            'tracks': {}}
        issue, saves = {'interpretation':state}, []
        with patch.object(review, '_ask', return_value='APPROVE BATCH'), redirect_stdout(StringIO()):
            result = review._batch_supported_tracks(issue, {'tracks':rows}, state,
                                                  lambda _: saves.append(dict(state['tracks'])))
        self.assertEqual(result, 1)
        self.assertEqual(set(state['tracks']), {'Import/Beaumont.mp3'})
        self.assertEqual(state['tracks']['Import/Beaumont.mp3']['number'], 1)
        self.assertEqual(len(saves), 1)

    def test_batch_cannot_override_existing_or_start_without_release(self):
        row = {'path':'Import/Beaumont.mp3', 'assessment':{'status':'verified', 'candidates':[
            {'recording_id':'rec-1', 'status':'verified',
             'details':{'id':'rec-1','title':'Beaumont','artist-credit-phrase':'3OH!3'}}]}}
        state = {'tracks': {}}
        with patch.object(review, '_ask', side_effect=AssertionError('Must not prompt')), redirect_stdout(StringIO()):
            self.assertEqual(0, review._batch_supported_tracks({}, {'tracks':[row]}, state, lambda _: None))
        self.assertFalse(state['tracks'])
