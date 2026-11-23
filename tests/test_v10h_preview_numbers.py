"""Verify the automatic preview actually requests canonical positions."""
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path
import unittest

import audio
import lookup
import mixie
import tag
from support import WorkspaceCase, make_audio, make_cover


class PreviewPositionTests(WorkspaceCase):
    def test_fully_mapped_album_repairs_conflicting_source_track_order(self):
        sources = []
        for index, incorrect in enumerate((3, 1, 2), 1):
            source = make_audio(self.stage / 'Shuffled' / f'{index:02d}.flac',
                artist='Artist', album='Shuffled', title=f'Song {index}',
                tracknumber=str(incorrect), discnumber='1')
            sources.append(source)
        albums = {'album': 'Shuffled', 'folder': sources[0].parent,
                  'tracks': [tag.read_track_metadata(source) for source in sources]}
        audits = [{'path': path, 'status': 'preliminary_match', 'reason': 'fixture',
                   'details': {'id': f'identity-{i}', 'title': f'Song {i}',
                               'artist-credit-phrase': 'Artist',
                               'release-list': [{'id': 'release'}]}}
                  for i, path in enumerate(sources, 1)]
        tracks = [{'disc': '1', 'number': str(i), 'title': f'Song {i}',
                   'recording_id': f'identity-{i}'} for i in (1,2,3)]
        with patch.object(audio, 'audit_tracks', return_value=audits), \
             patch.object(lookup, 'discover_release_candidates', return_value=['release']), \
             patch.object(lookup, 'get_release_tracklist', return_value=tracks), \
             patch.object(lookup, 'common_release_metadata', return_value={
                'album':'Shuffled','album_artist':'Artist','date':'2010','genre':'Rock'}), \
             patch.object(mixie, 'select_album_art', return_value=make_cover()), \
             redirect_stdout(StringIO()):
            plan = mixie.preview_album(albums, 'fixture', auto_mode=True)
        changes = [p for p in plan['proposals']
                   if p['field']=='track_number' and p.get('verified_release_position')]
        self.assertEqual(3, len(changes))
        self.assertEqual({(str(p['path']), p['value']) for p in changes},
                         {(str(path), i) for i,path in enumerate(sources,1)})
        self.assertEqual('independently_verified', plan['provenance'])

if __name__ == '__main__': unittest.main()
