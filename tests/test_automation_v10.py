"""v10 synthetic audio, music-first catalog and safe partial-album tests.

No providers or user music are accessed. Fixtures live in isolated temp folders.
"""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import unittest

import audio
import lookup
import mixie
import tag
import sort
from support import WorkspaceCase, make_audio, make_cover


class MusicFirstV10Tests(WorkspaceCase):
    def album(self, count=3, name='Album', missing_tags=False):
        tracks=[]
        for n in range(1,count+1):
            filename=f'{n:02d}.flac'
            path=make_audio(self.stage/'Import'/name/filename,
                album='' if missing_tags else name, artist='' if missing_tags else 'Artist',
                albumartist='' if missing_tags else 'Artist',title='' if missing_tags else f'Song {n}',
                tracknumber=0 if missing_tags else n,tracktotal=0 if missing_tags else count,
                discnumber=0 if missing_tags else 1,disctotal=0 if missing_tags else 1,
                date='' if missing_tags else '2020',genre='' if missing_tags else 'Electronic')
            if missing_tags:
                from mutagen import File
                handle=File(path,easy=True)
                for key in list(handle.keys()):
                    del handle[key]
                handle.save()
            tracks.append(tag.read_track_metadata(path))
        return {'album':'Unknown Album' if missing_tags else name,
                'folder':self.stage/'Import'/name,'tracks':tracks}

    def audits(self,album,guest=None):
        return [{'path':track['path'],'status':'preliminary_match',
                 'details':{'id':f'rec-{i}', 'title':f'Song {i}',
                    'artist-credit-phrase': f'Artist feat. {guest}' if guest and i==2 else 'Artist'}}
                for i,track in enumerate(album['tracks'],1)]

    def edition(self, album, rid='standard', full_count=None, reverse=False):
        count=full_count or len(album['tracks'])
        remote=[{'disc':'1','number':str(i),'recording_id':f'rec-{i}',
                 'title':f'Song {i}'} for i in range(1,count+1)]
        local={int(i):p['path'] for i,p in enumerate(album['tracks'],1)}
        positions=list(local)
        if reverse: positions=list(reversed(positions))
        mapping={(1,i): local[j] for i,j in zip(range(1,len(local)+1),positions)}
        return {'release_id':rid,'status':'partial' if count>len(local) else 'complete',
                'matched':mapping, 'missing':remote[len(local):],
                'review':[], 'reasons':['missing_release_tracks'] if count>len(local) else [],
                'tracklist':remote,'errors':[]}

    def preview(self,album,editions,audits=None,managed=False):
        audits=audits or self.audits(album)
        by_id={x['release_id']:x for x in editions}
        genuine_manifest = mixie.completion_source_manifest
        def manifest(issue):
            value = genuine_manifest(issue)
            if managed:
                value = {**value, 'provenance': {'synthetic': True}}
            return value
        with patch.object(mixie,'completion_source_manifest',side_effect=manifest), \
             patch.object(audio,'audit_tracks',return_value=audits), \
             patch.object(lookup,'discover_release_candidates',return_value=list(by_id)), \
             patch.object(lookup,'evaluate_release_candidate',side_effect=lambda *a,**k:by_id[a[1]]), \
             patch.object(lookup,'common_release_metadata',return_value={
                  'album':'Album','album_artist':'Artist','date':'2020'}), \
             patch.object(mixie,'select_album_art',return_value=make_cover()), \
             redirect_stdout(StringIO()):
            return mixie.preview_album(album,'synthetic',auto_mode=True)

    def test_one_exact_valid_album_beats_unexamined_retail_releases(self):
        album=self.album(4)
        complete=self.edition(album,'original')
        many=[complete]+[self.edition(album,f'retail-{i}',full_count=7) for i in range(100)]
        # These legacy audits have no exhaustive membership evidence: a first
        # complete pressing cannot disprove the unexamined musical competitors.
        with self.assertRaises(mixie.review.AlbumReviewRequired) as caught:
            self.preview(album,many)
        self.assertIn('album_competitors_unexamined_or_incomplete', caught.exception.details['reason'])
        unexamined=[row for row in caught.exception.details['releases']
                    if 'release_evaluation_budget_exhausted' in row.get('reasons',())]
        self.assertEqual(1,len(unexamined))
        self.assertTrue(unexamined[0]['unexamined_release_ids'])

    def test_standard_complete_ignores_deluxe_bonus_when_both_appear(self):
        album=self.album(3)
        plan=self.preview(album,[self.edition(album,'deluxe',full_count=5), self.edition(album,'standard')])
        self.assertEqual(3,len(plan['release_tracks']))
        self.assertEqual(['standard'],plan['compatible_ids'])

    def test_supplied_partial_album_retains_full_release_positions_and_totals(self):
        album=self.album(3)
        from mutagen import File
        for track in album['tracks']:
            h=File(track['path'],easy=True)
            for key in ('tracktotal','totaltracks'):
                if key in h: del h[key]
            h.save()
            track.update(tag.read_track_metadata(track['path']))
        editions=[self.edition(album,'standard',full_count=7)]
        plan=self.preview(album,editions,managed=True)
        self.assertTrue(plan['partial'])
        self.assertEqual('independently_verified_partial',plan['provenance'])
        self.assertEqual(7,plan['release_totals']['album_total'])
        self.assertEqual(3,len(plan['matched']))
        self.assertEqual(7,plan['release_totals']['track_totals']['1'])

    def test_tagless_full_album_gets_identity_without_source_tags(self):
        album=self.album(3,missing_tags=True)
        plan=self.preview(album,[self.edition(album)])
        self.assertEqual('Song 2',plan['identities'][str(album['tracks'][1]['path'].resolve())]['title'])
        self.assertFalse(plan['partial'])
        self.assertEqual(3,len(plan['matched']))

    def test_untagged_partial_album_not_assumed_from_ambiguous_release(self):
        album=self.album(3,missing_tags=True)
        with self.assertRaises(Exception) as caught:
            self.preview(album,[self.edition(album,'unproven',full_count=7)],managed=True)
        self.assertIn('parent album',str(caught.exception))

    def test_featured_performer_moves_into_title_without_changing_primary_artist(self):
        title,artist=mixie._feature_aware_identity({'artist-credit-phrase':'3OH!3 feat. Ke$ha'},
            'My First Kiss','My First Kiss',{'title':('My First Kiss',),'artist':('3OH!3',)})
        self.assertEqual('3OH!3',artist)
        self.assertEqual('My First Kiss (feat. Ke$ha)',title)

    def test_joint_artist_not_misclassified_as_feature(self):
        title,artist=mixie._feature_aware_identity({'artist-credit-phrase':'3OH!3 and 100 gecs'},
             'Lonely Machines','Lonely Machines',{'title':('Lonely Machines',),'artist':('3OH!3 and 100 gecs',)})
        self.assertEqual('3OH!3 and 100 gecs',artist)

    def test_wrong_source_guest_is_replaced_by_verified_release_credit_for_same_song(self):
        # The standard release settles a disputed guest, rather than forcing
        # a review or silently carrying the unverified source credit forward.
        title, artist = mixie._feature_aware_identity(
            {'artist-credit-phrase': '3OH!3 feat. Ke$ha'},
            'My First Kiss', 'My First Kiss',
            {'title': ('My First Kiss (feat. Katy Perry)',), 'artist': ('3OH!3',)})
        self.assertEqual(('My First Kiss (feat. Ke$ha)', '3OH!3'), (title, artist))
        # A different song identity is not approved merely by a guest alias.
        with self.assertRaisesRegex(ValueError, 'Different featured'):
            mixie._feature_aware_identity(
                {'artist-credit-phrase': '3OH!3 feat. Ke$ha'},
                'My First Kiss', 'My First Kiss',
                {'title': ('Another Song (feat. Katy Perry)',), 'artist': ('3OH!3',)})

    def test_existing_guest_credit_only_normalized_when_guest_preserved_in_title(self):
        folder=self.stage/'Import'/'copies'
        path=make_audio(folder/'test.flac',album='Album',artist='PVRIS feat. RAYE',
             albumartist='PVRIS',title='Thank You',tracknumber=1,tracktotal=1,
             discnumber=1,disctotal=1,date='2020',genre='Pop')
        identity={'recording_id':'r','title':'Thank You (feat. RAYE)', 'artist':'PVRIS',
                  'verified':True,'user_approved':False}
        with patch.object(tag,'_require_working_copy',return_value=path):
            tag.apply_verified_identity_on_copy(path,folder,identity)
        self.assertEqual(('PVRIS',),tag.read_track_metadata(path)['artist'])
        self.assertEqual(('Thank You (feat. RAYE)',),tag.read_track_metadata(path)['title'])

    def test_partial_album_path_is_folder_only_not_album_tag(self):
        audio_file=make_audio(self.stage/'Import'/'Album'/'02.flac',album='Album',artist='Artist',
             albumartist='Artist',title='Song 2',tracknumber=2,tracktotal=11,discnumber=1,
             disctotal=1,date='2020',genre='Electronic')
        with redirect_stdout(StringIO()):
            planned=sort.plan_library_sort([{'path':audio_file,'album_artist':'Artist',
                   'album':'Album','folder_album':'Album (Incomplete, 1 of 11)',
                   'title':'Song 2','track_number':'2','disc_number':None}],
                   approved=True,source_root=self.stage)
        self.assertEqual(1,len(planned))
        self.assertIn('Album (Incomplete, 1 of 11)',planned[0]['destination'])
        self.assertEqual('Album',tag.read_track_metadata(audio_file)['album'][0])

    def test_unclassified_genre_does_not_invent_a_date_or_genre(self):
        album=self.album(3,missing_tags=True)
        matched={(1,i):row['path'] for i,row in enumerate(album['tracks'],1)}
        proposals=tag.propose_required_album_metadata(matched,{},allow_unclassified=True)
        self.assertEqual([], proposals)
        self.assertTrue(all(not row['genre'] and not row['date'] for row in album['tracks']))


if __name__=='__main__':unittest.main()
