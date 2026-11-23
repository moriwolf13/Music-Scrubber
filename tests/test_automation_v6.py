"""v6 regression targets: conservative automation, artwork, and real UI progress."""
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib

from PIL import Image

import audio
import lookup
import mixie
import mixie_artwork
import mixie_review_ui as ui
import mixie_terminal as terminal
import tag
from support import WorkspaceCase, make_audio, make_cover


class AutomationV6Tests(WorkspaceCase):
    def sample(self, *, count=2, disc=1):
        tracks = []
        for index in range(1, count + 1):
            path = make_audio(self.stage / 'Import' / f'{index:02}.flac', album='White Women',
                              albumartist='Chromeo', artist='Chromeo', title=f'Song {index}',
                              tracknumber=index, discnumber=disc, date='2014', genre='Dance')
            tracks.append(tag.read_track_metadata(path))
        album = {'album': 'White Women', 'folder': self.stage / 'Import', 'tracks': tracks}
        issue = {'album': 'White Women', 'import_name': 'Import',
                 'tracks': [t['path'].relative_to(self.stage).as_posix() for t in tracks],
                 'reason': 'Different release versions', 'details': {'kind': 'identity_release'}}
        issue['source_fingerprint'] = mixie.review_fingerprint(issue)
        issue['details']['source_fingerprint'] = issue['source_fingerprint']
        return album, issue

    def test_disc_layout_uses_stored_complete_musicbrainz_evidence(self):
        _album, issue = self.sample()
        mapping = ui._view(mixie, issue)
        for row in mapping.values():
            row['disc_number'] = ()
            row['disc_total'] = ()
        issue['details']['releases'] = [{
            'status': 'complete', 'tracklist_complete': True, 'errors': [],
            'tracklist': [{'disc': '1', 'number': str(i)} for i in (1, 2)],
            'matched': {'[1,1]': issue['tracks'][0], '[1,2]': issue['tracks'][1]}}]
        self.assertEqual('single', ui._disc_layout(mixie, issue, mapping))
        issue['details']['releases'].append({
            'status': 'complete', 'tracklist_complete': True, 'errors': [],
            'tracklist': [{'disc': '1', 'number': '1'}, {'disc': '2', 'number': '1'}],
            'matched': {'[1,1]': issue['tracks'][0], '[2,1]': issue['tracks'][1]}})
        self.assertEqual('auto', ui._disc_layout(mixie, issue, mapping))

    def test_auto_multiple_disc_uses_remote_positions_without_manual_numbering(self):
        _album, issue = self.sample()
        issue['details']['releases'] = [{
            'status': 'complete', 'tracklist_complete': True, 'errors': [],
            'tracklist': [{'disc': '1', 'number': '1'}, {'disc': '2', 'number': '1'}],
            'matched': {'[1,1]': issue['tracks'][0], '[2,1]': issue['tracks'][1]}}]
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual({1, 2}, {item['disc'] for item in decision['tracks'].values()})
        self.assertEqual({2}, {item['disc_total'] for item in decision['totals'].values()})
        self.assertEqual({1}, {item['track_total'] for item in decision['totals'].values()})

    def test_partial_musicbrainz_release_cannot_establish_single_disc(self):
        _album, issue = self.sample()
        mapping = ui._view(mixie, issue)
        for row in mapping.values():
            row['disc_number'] = ()
            row['disc_total'] = ()
        issue['details']['releases'] = [{
            'status': 'partial', 'tracklist_complete': False, 'errors': [],
            'tracklist': [{'disc': '1', 'number': '1'}],
            'matched': {'[1,1]': issue['tracks'][0]}}]
        self.assertEqual('auto', ui._disc_layout(mixie, issue, mapping))

    def test_webp_conversion_is_copy_only_and_rejects_oversized_dimensions(self):
        source = self.root / 'cover.webp'
        buffer = BytesIO()
        Image.new('RGB', (80, 80), (30, 200, 140)).save(buffer, format='WEBP')
        source.write_bytes(buffer.getvalue())
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        data, mime = mixie_artwork.normalized_image(source.read_bytes())
        self.assertEqual('image/jpeg', mime)
        self.assertEqual('JPEG', Image.open(BytesIO(data)).format)
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
        album, issue = self.sample()
        with patch('builtins.input', return_value=str(source)), redirect_stdout(StringIO()):
            ui._artwork(mixie, issue, [issue])
        snapshot = mixie._review_artwork_snapshot(issue['artwork_decision'])
        self.assertEqual('image/jpeg', snapshot['mime'])
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_approved_album_without_release_id_searches_exact_art(self):
        album, _issue = self.sample()
        cover = make_cover()
        with patch.object(mixie, 'album_asset_root', return_value=None), \
             patch.object(lookup, 'find_exact_album_releases', return_value=['release-a']) as discover, \
             patch.object(lookup, 'download_release_art', return_value=cover):
            with redirect_stdout(StringIO()):
                selected = mixie.select_album_art(album, [])
        discover.assert_called_once_with('Chromeo', 'White Women')
        self.assertEqual(cover['data'], selected['data'])

    def test_disagreeing_online_covers_are_snapshotted_and_selectable(self):
        album, issue = self.sample()
        first = make_cover()
        img = Image.open(BytesIO(first['data'])).convert('RGB')
        Image.new('RGB', img.size, (18, 225, 70)).save(self.root / 'second.png', 'PNG')
        second = {'mime': 'image/png', 'data': (self.root / 'second.png').read_bytes()}
        def art(release, preview=False):
            return first if release == 'release-a' else second
        with patch.object(mixie, 'album_asset_root', return_value=None), \
             patch.object(lookup, 'get_release_metadata', return_value={}), \
             patch.object(lookup, 'download_release_art', side_effect=art):
            with self.assertRaises(mixie.ArtworkReviewRequired) as caught:
                mixie.select_album_art(album, ['release-a', 'release-b'])
        candidates = caught.exception.candidates
        self.assertEqual(2, len(candidates))
        self.assertTrue(all(Path(x['path']).is_file() for x in candidates))
        issue['artwork_details'] = {'kind': 'artwork_conflict', 'candidates': candidates}
        with patch('builtins.input', return_value='2'), redirect_stdout(StringIO()):
            ui._artwork(mixie, issue, [issue])
        selected = mixie._review_artwork_snapshot(issue['artwork_decision'])
        self.assertEqual(second['data'], selected['data'])

    def test_artwork_remains_required_without_reference(self):
        album, _issue = self.sample()
        with patch.object(mixie, 'album_asset_root', return_value=None), \
             patch.object(lookup, 'find_exact_album_releases', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'No reference artwork'):
                mixie.select_album_art(album, [])

    def test_real_audio_scan_events_are_emitted_per_track(self):
        album, _issue = self.sample()
        messages = []
        def event(phase, index, total, path, result):
            messages.append((phase, index, total, Path(path).name))
        audits = [{'path': t['path'], 'status': 'preliminary_match', 'reason': 'verified', 'details': {}}
                  for t in album['tracks']]
        with patch.object(audio, '_audit_track_result', side_effect=audits), \
             audio.audit_progress(event), redirect_stdout(StringIO()):
            found = audio.audit_tracks([t['path'] for t in album['tracks']], 'key')
        self.assertEqual(2, len(found))
        self.assertEqual([('start', 1), ('complete', 1), ('start', 2), ('complete', 2)],
                         [(phase, index) for phase, index, _, _ in messages])

    def test_version_title_only_collapses_original_mix_label(self):
        self.assertEqual(mixie._edition_title_key('Clash'),
                         mixie._edition_title_key('Clash (Original Mix)'))
        self.assertNotEqual(mixie._edition_title_key('Song (Live)'),
                            mixie._edition_title_key('Song (Acoustic)'))
