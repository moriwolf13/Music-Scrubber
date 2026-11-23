"""A real three-MP3 canonical artist, numbering and library commit check."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib

import audio
import lookup
import mixie
import tag
from support import WorkspaceCase, make_audio, make_cover


class FullMatchImportTests(WorkspaceCase):
    def test_matched_release_imports_actual_mp3_numbers_and_canonical_artist(self):
        source = self.stage / 'NEED'
        paths = []
        for index, title in enumerate(('FIRST', 'SECOND', "VAMPIRE'S DIET"), 1):
            path = make_audio(source / f'{index:02d}.mp3',
                              album='NEED', title=title,
                              artist=('3OH!3, The Used_Bert McCracken' if index == 3 else '3OH!3'),
                              albumartist='3OH!3', date='2021', genre='Unknown')
            paths.append(path)
        original_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        album = {'album': 'NEED', 'folder': source,
                 'tracks': [tag.read_track_metadata(p) for p in paths]}
        audits = [{'path': path, 'status': 'verified',
                   'details': {'id': f'id-{index}', 'title': path.stem if index < 3 else "VAMPIRE'S DIET",
                               'artist-credit-phrase': '3OH!3'},
                   'assessment': {'status': 'verified', 'complete': True}}
                  for index, path in enumerate(paths, 1)]
        # Match the exact source titles, not the filename formatting.
        for index, audit in enumerate(audits, 1):
            audit['details']['title'] = album['tracks'][index - 1]['title'][0]
        evaluation = {'release_id': 'exact-need', 'status': 'complete',
                      'matched': {(1, index): path for index, path in enumerate(paths, 1)},
                      'tracklist': [{'disc': '1', 'number': str(index),
                                    'recording_id': f'id-{index}', 'title': album['tracks'][index - 1]['title'][0]}
                                    for index in (1, 2, 3)],
                      'missing': [], 'review': [], 'errors': [], 'reasons': []}
        with patch.object(audio, 'audit_tracks', return_value=audits), \
             patch.object(lookup, '_accepted_recording', return_value='independent'), \
             patch.object(lookup, 'discover_release_candidates', return_value=['exact-need']), \
             patch.object(lookup, 'evaluate_release_candidate', return_value=evaluation), \
             patch.object(lookup, 'common_release_metadata', return_value={
                 'album': 'NEED', 'album_artist': '3OH!3', 'date': '2021'}), \
             patch.object(lookup, 'get_release_metadata', return_value={'release_type': 'album'}), \
             patch.object(mixie, 'select_album_art', return_value=make_cover()), \
             redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, 'OFFLINE-TEST', auto_mode=True)
            self.assertEqual('independently_verified', plan['provenance'])
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertTrue(receipt)
        for index, source in enumerate(paths, 1):
            destination = Path(receipt.input_map[source.relative_to(self.stage).as_posix()]['destination'])
            metadata = tag.read_track_metadata(destination)
            self.assertEqual(('3OH!3',), metadata['artist'])
            self.assertEqual(index, int(tag.parse_track_number(metadata['track_number'])[0]))
            self.assertEqual(3, tag.parse_track_number(metadata['track_number'])[1])
            self.assertEqual((), metadata['disc_number'])
            self.assertEqual((), metadata['disc_total'])
            self.assertEqual(original_hashes[source], hashlib.sha256(source.read_bytes()).hexdigest())

