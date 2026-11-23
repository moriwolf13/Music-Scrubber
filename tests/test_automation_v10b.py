"""Bounded, audio-verified release discovery tests. No live providers or user music."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import audio
import lookup
import mixie
import review
from support import WorkspaceCase, make_audio, make_cover
import tag


class BoundedDiscoveryTests(WorkspaceCase):
    def make_album(self, count=3, name="Album"):
        paths = [make_audio(self.stage / 'Import' / name / f'{i:02d}.flac',
                    album=name, artist='Artist', albumartist='Artist', title=f'Song {i}',
                    tracknumber=i, tracktotal=count, discnumber=1, disctotal=1,
                    date='2020', genre='Electronic') for i in range(1, count+1)]
        metadata = [tag.read_track_metadata(path) for path in paths]
        return {'album': name, 'tracks': metadata, 'folder': paths[0].parent}

    def accepted(self, album):
        return [{'path': row['path'], 'status': 'preliminary_match',
                 'details': {'id': f'rec-{i}', 'title': f'Song {i}',
                             'artist-credit-phrase': 'Artist'}}
                for i, row in enumerate(album['tracks'], 1)]

    def valid(self, album, release, total=None):
        total = total or len(album['tracks'])
        remote = [{'disc':'1', 'number':str(i), 'recording_id':f'rec-{i}',
                   'title':f'Song {i}'} for i in range(1, total+1)]
        return {'release_id': release,
                'status': 'complete' if total == len(album['tracks']) else 'partial',
                'matched': {(1,i): row['path'] for i,row in enumerate(album['tracks'],1)},
                'missing': remote[len(album['tracks']):], 'review':[],
                'reasons': [] if total == len(album['tracks']) else ['missing_release_tracks'],
                'tracklist':remote,'errors':[]}

    @staticmethod
    def useless(release):
        return {'release_id': release, 'status':'partial', 'matched':{}, 'missing':[],
                'review':[], 'reasons':['empty_release_tracklist'], 'tracklist':[], 'errors':[]}

    def test_exact_named_release_prioritized_when_audio_support_equal(self):
        audits = [{'path': self.stage/'a.flac', 'status':'preliminary_match',
                   'details': {'id':'rec', 'release-list': [
                        {'id':'retail-z', 'title':'Greatest Hits'},
                        {'id':'retail-a', 'title':'Album'}]}}]
        self.assertEqual(['retail-a','retail-z'],
                         lookup.discover_release_candidates(audits,local_album='Album'))
        self.assertEqual(['retail-a','retail-z'],
                         lookup.discover_release_candidates(audits,local_album='Unknown Album'))

    def test_targeted_catalog_search_prioritizes_audio_tested_album_with_bounded_work(self):
        album = self.make_album()
        candidates = [f'compilation-{i:03d}' for i in range(101)]
        checked = []
        def evaluate(audits, rid, **kwargs):
            checked.append(rid)
            return self.valid(album, rid) if rid == 'exact-album' else self.useless(rid)
        audits = self.accepted(album)
        for audit in audits:
            audit['details'].update({'release-list-complete': True,
                                     'release-list': [{'id': 'exact-album'}]})
        with patch.object(audio, 'audit_tracks', return_value=audits), \
             patch.object(lookup,'discover_release_candidates',return_value=candidates), \
             patch.object(lookup,'find_exact_album_releases',return_value=['exact-album']) as search, \
             patch.object(lookup,'evaluate_release_candidate',side_effect=evaluate), \
             patch.object(lookup,'common_release_metadata',return_value={'album':'Album','album_artist':'Artist','date':'2020'}), \
             patch.object(mixie,'select_album_art',return_value=make_cover()), \
             redirect_stdout(StringIO()) as output:
            plan = mixie.preview_album(album, 'test-key', auto_mode=True)
        search.assert_called_once_with('Artist','Album')
        self.assertIn('Exact album search prioritized 1',output.getvalue())
        self.assertEqual('exact-album',plan['compatible_ids'][0])
        self.assertEqual(9,len(checked))
        self.assertEqual('exact-album',checked[8])

    def test_strong_managed_partial_album_survives_unexamined_retail_candidates(self):
        album = self.make_album()
        from mutagen import File
        for row in album['tracks']:
            h = File(row['path'], easy=True)
            for key in ('tracktotal', 'totaltracks'):
                if key in h: del h[key]
            h.save()
            row.update(tag.read_track_metadata(row['path']))
        candidate_ids = ['standard','retail-a','retail-b','retail-c','retail-d']
        original_manifest = mixie.completion_source_manifest
        def manifest(issue):
            value=original_manifest(issue)
            return {**value, 'provenance':{'test':True}}
        def evaluate(audits,rid,**kw):
            return self.valid(album,rid,total=5) if rid=='standard' else self.useless(rid)
        with patch.object(mixie,'completion_source_manifest',side_effect=manifest), \
             patch.object(audio, 'audit_tracks', return_value=self.accepted(album)), \
             patch.object(lookup,'MAX_RELEASE_EVALUATIONS',3), \
             patch.object(lookup,'discover_release_candidates',return_value=candidate_ids), \
             patch.object(lookup,'evaluate_release_candidate',side_effect=evaluate), \
             patch.object(lookup,'common_release_metadata',return_value={'album':'Album','album_artist':'Artist','date':'2020'}), \
             patch.object(mixie,'select_album_art',return_value=make_cover()), \
             redirect_stdout(StringIO()) as output:
            plan=mixie.preview_album(album,'test-key',auto_mode=True)
        self.assertTrue(plan['partial'])
        self.assertEqual(5,plan['release_totals']['album_total'])
        self.assertIn('retail candidate(s) retained as unknown',output.getvalue())
        self.assertEqual(['retail-c','retail-d'],
                         plan['evaluations'][-1]['unexamined_release_ids'])

    def test_unchecked_candidates_do_not_authorize_unmapped_album(self):
        album = self.make_album()
        with patch.object(audio, 'audit_tracks', return_value=self.accepted(album)), \
             patch.object(lookup,'MAX_RELEASE_EVALUATIONS',2), \
             patch.object(lookup,'discover_release_candidates',return_value=['a','b','c']), \
             patch.object(lookup,'evaluate_release_candidate',side_effect=lambda a,rid,**k:self.useless(rid)), \
             redirect_stdout(StringIO()), \
             self.assertRaises(review.AlbumReviewRequired) as issue:
            mixie.preview_album(album,'test-key',auto_mode=True)
        self.assertIn('budget exhausted',str(issue.exception).lower())

    def test_unmanaged_partial_album_still_blocked(self):
        album = self.make_album()
        def evaluate(audits,rid,**kw):
            return self.valid(album,rid,total=5) if rid=='standard' else self.useless(rid)
        with patch.object(audio, 'audit_tracks', return_value=self.accepted(album)), \
             patch.object(lookup,'MAX_RELEASE_EVALUATIONS',2), \
             patch.object(lookup,'discover_release_candidates',return_value=['standard','retail-a','retail-b']), \
             patch.object(lookup,'evaluate_release_candidate',side_effect=evaluate), \
             redirect_stdout(StringIO()), \
             self.assertRaises(review.AlbumReviewRequired) as issue:
            mixie.preview_album(album,'test-key',auto_mode=True)
        self.assertIn('budget exhausted',str(issue.exception).lower())
