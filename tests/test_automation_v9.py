"""v9 focused regressions: music-first release equivalence and artwork fallback.

All test audio/art is synthetic; source audio and user folders are never read.
"""
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import audio
import lookup
import mixie
import review
import sort
import tag
from support import WorkspaceCase, make_audio, make_cover


class MusicFirstV9Tests(WorkspaceCase):
    def album(self, count=3):
        tracks=[]
        for n in range(1,count+1):
            path=make_audio(self.stage/'Import'/'Album'/f'{n:02}.flac',album='Album',artist='Artist',
                albumartist='Artist',title=f'Song {n}',tracknumber=n,tracktotal=count,
                discnumber=1,disctotal=1,date='2020',genre='Electronic')
            tracks.append(tag.read_track_metadata(path))
        return {'album':'Album','folder':self.stage/'Import'/'Album','tracks':tracks}

    def accepted(self, album):
        return [{'path':track['path'],'status':'preliminary_match',
                 'details':{'id':f'rec-{i}','title':f'Song {i}',
                            'artist-credit-phrase':'Artist'}}
                for i,track in enumerate(album['tracks'],1)]

    def release(self, album, name='digital', discs=None, missing=0):
        paths=[row['path'] for row in album['tracks']]
        discs = discs or [(1,n) for n in range(1,len(paths)+1)]
        tracks=[{'disc':str(disc),'number':str(n),'recording_id':f'rec-{i}',
                 'title':f'Song {i}'}
                for i,(disc,n) in enumerate(discs,1)]
        tracks += [{'disc':'1','number':str(len(paths)+n),'recording_id':f'bonus-{n}',
                    'title':f'Bonus {n}'} for n in range(1,missing+1)]
        return {'release_id':name, 'status':'partial' if missing else 'complete',
                'matched':{pos:path for pos,path in zip(discs,paths)},
                'missing':tracks[-missing:] if missing else [], 'review':[],
                'reasons':['missing_release_tracks'] if missing else [],
                'tracklist':tracks,'errors':[]}

    def plan(self, album, candidates, audits=None):
        audits=audits if audits is not None else self.accepted(album)
        releases={release['release_id']:release for release in candidates}
        with patch.object(audio,'audit_tracks',return_value=audits), \
             patch.object(lookup,'discover_release_candidates',return_value=list(releases)), \
             patch.object(lookup,'evaluate_release_candidate',side_effect=lambda *a,**kw: releases[a[1]]), \
             patch.object(lookup,'common_release_metadata',return_value={'album':'Album',
                        'album_artist':'Artist','date':'2020'}), \
             patch.object(mixie,'select_album_art',return_value=make_cover()), \
             redirect_stdout(StringIO()) as out:
            result=mixie.preview_album(album,'synthetic')
        return result,out.getvalue()

    def test_complete_standard_wins_over_deluxe_with_unprovided_bonus(self):
        album=self.album()
        plan,_=self.plan(album,[self.release(album,'standard'),self.release(album,'deluxe',missing=2)])
        self.assertEqual(['standard'],plan['compatible_ids'])
        self.assertEqual(3,len(plan['release_tracks']))

    def test_digital_and_vinyl_divisions_are_one_music_group(self):
        album=self.album()
        digital=self.release(album,'digital')
        vinyl=self.release(album,'vinyl',discs=[(1,1),(1,2),(2,1)])
        plan,out=self.plan(album,[vinyl,digital])
        self.assertEqual(1,len({mixie._musical_release_signature(x) for x in [digital,vinyl]}))
        self.assertEqual(['vinyl','digital'],plan['compatible_ids'])
        self.assertEqual(1,len({int(track['disc']) for track in plan['release_tracks']}))
        self.assertIn('1 tracklist groups',out)

    def test_strong_multi_song_album_is_not_vetoed_by_unavailable_retail_edition(self):
        album=self.album(4)
        good=self.release(album,'good')
        outage={'release_id':'missing','status':'partial','matched':{},'missing':[],
                'review':[],'reasons':['release_lookup_unavailable'], 'tracklist':[],
                'errors':[{'retryable':True,'stage':'release_tracklist'}]}
        plan,out=self.plan(album,[good,outage])
        self.assertEqual(['good'],plan['compatible_ids'])
        self.assertIn('incomplete retail edition(s) ignored',out)

    def test_partially_checked_competitor_with_known_different_recording_still_blocks(self):
        album=self.album(4)
        good=self.release(album,'good')
        uncertain={'release_id':'suspect','status':'partial',
                   'matched':{(1,1):album['tracks'][0]['path']},
                   'missing':[],'review':[],
                   'reasons':['remote_tracklist_incomplete'],
                   'tracklist':[{'disc':'1','number':'1','recording_id':'different-recording'}],
                   'errors':[]}
        with self.assertRaises(review.AlbumReviewRequired):
            self.plan(album,[good,uncertain])

    def test_single_song_outage_still_blocks(self):
        album=self.album(1)
        good=self.release(album,'good')
        outage={'release_id':'unknown','status':'partial','matched':{},'missing':[],
                'review':[], 'reasons':['release_lookup_unavailable'],'tracklist':[],
                'errors':[{'retryable':True,'stage':'release_tracklist'}]}
        with self.assertRaises(mixie.ProviderUnavailable):
            self.plan(album,[good,outage])

    def test_unverified_album_context_cannot_dismiss_incomplete_edition(self):
        album=self.album(4)
        weak=self.accepted(album)
        weak[2]['status']='needs_review'
        weak[3]['status']='needs_review'
        good=self.release(album,'good')
        outage={'release_id':'unknown','status':'partial','matched':{},'missing':[],
                'review':[], 'reasons':['remote_tracklist_incomplete'],'tracklist':[], 'errors':[]}
        with self.assertRaises(review.AlbumReviewRequired):
            self.plan(album,[good,outage],audits=weak)

    def test_same_audio_different_order_remains_conflicting(self):
        a=self.release(self.album(),'one')
        b=dict(a,matched={(1,1):a['matched'][(1,2)],(1,2):a['matched'][(1,1)],
                          (1,3):a['matched'][(1,3)]})
        self.assertNotEqual(mixie._musical_release_signature(a),mixie._musical_release_signature(b))

    def test_remaster_packaging_not_treated_as_different_song(self):
        self.assertEqual(mixie._edition_title_key('Song'),mixie._edition_title_key('Song (2019 Remaster)'))
        self.assertNotEqual(mixie._edition_title_key('Song (Live)'),mixie._edition_title_key('Song (Acoustic)'))

    def covers(self, name, color):
        path=self.stage/'Import'/'Album'/name
        path.parent.mkdir(parents=True,exist_ok=True)
        image=BytesIO()
        Image.new('RGB',(64,64),color).save(image,'PNG')
        path.write_bytes(image.getvalue())
        return path, {'mime':'image/png','data':image.getvalue()}

    def test_local_mismatch_uses_agreeing_online_album_front(self):
        path,local=self.covers('Folder.png','red')
        _other,remote=self.covers('online.png','blue')
        with patch.object(mixie,'album_asset_root',return_value=path.parent), \
             patch.object(sort,'scan_import_assets',return_value=[{'path':path}]), \
             patch.object(lookup,'download_release_art',return_value=remote), \
             redirect_stdout(StringIO()) as output:
            selected=mixie.select_album_art({'tracks':[]},['release-a','release-b'])
        self.assertEqual(remote['data'],selected['data'])
        self.assertEqual(local['data'],path.read_bytes())
        self.assertIn('differs',output.getvalue())

    def test_local_mismatch_without_reference_still_needs_review(self):
        path,_=self.covers('Folder.png','red')
        with patch.object(mixie,'album_asset_root',return_value=path.parent), \
             patch.object(sort,'scan_import_assets',return_value=[{'path':path}]), \
             patch.object(lookup,'download_release_art',return_value=None):
            with self.assertRaises(mixie.ArtworkReviewRequired):
                mixie.select_album_art({'tracks':[]},['release-a'])

    def test_different_retail_covers_same_group_use_album_group_art(self):
        _one,a=self.covers('one.png','blue')
        _two,b=self.covers('two.png','red')
        _three,group=self.covers('group.png','purple')
        def art(identifier,preview=False):
            return a if identifier=='first' else b
        with patch.object(mixie,'album_asset_root',return_value=None), \
             patch.object(lookup,'download_release_art',side_effect=art), \
             patch.object(lookup,'get_release_metadata',return_value={'release_group_id':'group-1'}), \
             patch.object(lookup,'download_release_group_art',return_value=group) as lookup_group, \
             redirect_stdout(StringIO()):
            selected=mixie.select_album_art({'tracks':[]},['first','second'])
        self.assertEqual(group['data'],selected['data'])
        lookup_group.assert_called_once_with('group-1')

    def test_original_album_date_not_retail_print_date(self):
        rows={
            'CD': {'release_group_id':'same','original_release_date':'2019-03-01',
                   'date':'2020-01-01','album':'Album','album_artist':'Artist'},
            'Digital': {'release_group_id':'same','original_release_date':'2019-03-01',
                        'date':'2019-03-01','album':'Album','album_artist':'Artist'}}
        with patch.object(lookup,'get_release_metadata',side_effect=lambda rid:rows[rid]):
            common=lookup.common_release_metadata(['CD','Digital'])
        self.assertEqual('2019-03-01',common['date'])
