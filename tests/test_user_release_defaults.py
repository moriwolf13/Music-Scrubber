"""Requested album-first defaults. All audio and provider observations synthetic."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib

from support import WorkspaceCase, make_audio, make_cover
import lookup
import mixie
import audio
import tag


class UserReleaseDefaultsTests(WorkspaceCase):
    def test_one_overlapping_artist_from_joint_credit(self):
        self.assertEqual('Halestorm', mixie._overlapping_artist_credit('Halestorm', 'Halestorm & I Prevail'))
        self.assertEqual('Halestorm', mixie._overlapping_artist_credit('Halestorm & I Prevail', 'Halestorm'))
        self.assertEqual('Halestorm', mixie._overlapping_artist_credit('I Prevail & Halestorm', 'Halestorm & Someone Else'))
        self.assertIsNone(mixie._overlapping_artist_credit('Halo', 'Halestorm & I Prevail'))
        self.assertIsNone(mixie._overlapping_artist_credit('ABBA', 'Halestorm & I Prevail'))

    def test_joint_single_artist_output_needs_no_manual_choice(self):
        details = {'title': 'can u see me in the dark?',
                   'artist-credit-phrase': 'Halestorm & I Prevail'}
        title, artist = mixie._feature_aware_identity(details, 'can u see me in the dark?',
                       'can u see me in the dark?',
                       {'title': ('can u see me in the dark?',), 'artist': ('Halestorm',)})
        self.assertEqual(('can u see me in the dark?', 'Halestorm'), (title, artist))

    def test_three_wrongly_numbered_audio_title_aliases_follow_canonical_album_order(self):
        song_titles = ('Opening', 'Middle Song', 'Finale')
        paths=[]
        # File basenames and existing numbers intentionally disagree with the release.
        for i, title in enumerate(song_titles):
            path=make_audio(self.stage/'Shuffled Album'/f'Wrong {3-i}.mp3',
                            title=title, artist='Synthetic Group', albumartist='Synthetic Group',
                            album='Shuffled Album', tracknumber=f"{3-i}/3",
                            date='2024', genre='Rock')
            paths.append(path)
        original={path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        audits=[]
        for i, path in enumerate(paths):
            title=song_titles[i]
            real_id=f'fingerprint-verified-{i}'
            details={'id':real_id, 'title':title, 'artist-credit-phrase':'Synthetic Group',
                     'release-list':[], 'release-list-complete':True, 'disambiguation':''}
            audits.append({'path':path, 'status':'needs_review', 'details':None,
                          'assessment':{'status':'conflicting','complete':True,'errors':[],
                                        'candidates':[{'recording_id':real_id, 'status':'verified',
                                                       'details':details, 'reasons':[]}]}})
        canonical=[{'disc':'1','number':str(i+1),'recording_id':f'standard-release-id-{i}',
                    'title':title,'recording_title':title} for i,title in enumerate(song_titles)]
        with patch.object(lookup,'get_release_tracklist',return_value=canonical):
            report=lookup.evaluate_release_candidate(audits,'standard-release',
                     local_tracks=[tag.read_track_metadata(path) for path in paths])
        self.assertEqual({(1,i+1):paths[i] for i in range(3)}, report['matched'])
        self.assertIn('resolved_by_audio_verified_title',report['reasons'])
        self.assertTrue(all(report['title_matched_candidates'][str(p)]['id'].startswith('fingerprint-verified-') for p in paths))
        album={'album':'Shuffled Album', 'folder':self.stage/'Shuffled Album',
               'tracks':[tag.read_track_metadata(path) for path in paths]}
        with patch.object(audio,'audit_tracks',return_value=audits), \
             patch.object(lookup,'discover_release_candidates',return_value=['standard-release']), \
             patch.object(lookup,'evaluate_release_candidate',return_value=report), \
             patch.object(lookup,'common_release_metadata',return_value={
                 'album':'Shuffled Album','album_artist':'Synthetic Group','date':'2024'}), \
             patch.object(lookup,'get_release_metadata',return_value={'release_type':'album'}), \
             patch.object(mixie,'select_album_art',return_value=make_cover()), \
             redirect_stdout(StringIO()):
            plan=mixie.preview_album(album,'DISPOSABLE-TEST-KEY',auto_mode=True)
            self.assertEqual('independently_verified',plan['provenance'])
            self.assertEqual({(str(1),str(i+1)):paths[i] for i in range(3)},plan['matched'])
            receipt=mixie.test_album_plan(plan,album,commit=True,auto=True)
        self.assertTrue(receipt)
        for i,path in enumerate(paths):
            output=Path(receipt.input_map[path.relative_to(self.stage).as_posix()]['destination'])
            saved=tag.read_track_metadata(output)
            self.assertTrue(output.name.startswith(f'{i+1:02d}'), output.name)
            self.assertEqual(i+1, int(tag.parse_track_number(saved['track_number'])[0]))
            self.assertEqual((),saved['disc_number'])
            self.assertEqual(original[path],hashlib.sha256(path.read_bytes()).hexdigest())

    def test_title_alias_refuses_real_version_mismatch(self):
        names=['Opening','Middle Song','Finale']
        paths=[self.track_path(f'Album/{i}.flac') for i in range(3)]
        audits=[]
        for i,path in enumerate(paths):
            details={'id':f'alias-{i}', 'title':names[i],
                     'disambiguation':'live at Brixton' if i==1 else ''}
            audits.append({'path':path,'status':'needs_review','details':None,
                           'assessment':{'status':'conflicting','complete':True,'errors':[],
                                         'candidates':[{'recording_id':details['id'],'status':'verified',
                                                        'details':details,'reasons':[]}]}})
        remote={(1,i+1):{'recording_id':f'album-{i}','title':title,'disc':'1','number':str(i+1)}
                for i,title in enumerate(names)}
        self.assertEqual({},lookup._verified_title_album_cohort(audits,remote))

    def test_title_alias_requires_complete_unique_audio_assignment(self):
        paths=[self.track_path(f'Album/{i}.flac') for i in range(3)]
        audits=[]
        for i,path in enumerate(paths):
            details={'id':f'alias-{i}','title':('Same Song' if i==2 else f'Song {i}')}
            audits.append({'path':path,'status':'needs_review','details':None,
                           'assessment':{'status':'conflicting','complete':True,'errors':[],
                                         'candidates':[{'recording_id':details['id'],'status':'verified',
                                                        'details':details,'reasons':[]}]}})
        remote={(1,i+1):{'recording_id':f'album-{i}','title':('Same Song' if i>=1 else 'Song 0')}
                for i in range(3)}
        self.assertEqual({},lookup._verified_title_album_cohort(audits,remote))

    def test_review_screen_only_reuses_fingerprint_until_next_action(self):
        import mixie_review_ui as ui
        issue = {'album': 'Synthetic', 'import_name': 'Synthetic', 'tracks': ['Synthetic/x.flac']}
        with patch.object(mixie, 'review_fingerprint', return_value='a' * 64) as hashed:
            with ui._review_render_scope():
                self.assertEqual('a' * 64, ui._read_only_fingerprint(mixie, issue))
                self.assertEqual('a' * 64, ui._read_only_fingerprint(mixie, issue))
                self.assertEqual('a' * 64, ui._read_only_fingerprint(mixie, issue))
                self.assertEqual(1, hashed.call_count)
            # Outside the read-only render, no hashes survive into an approval.
            self.assertEqual('a' * 64, ui._read_only_fingerprint(mixie, issue))
            self.assertEqual(2, hashed.call_count)
            with ui._review_render_scope():
                self.assertEqual('a' * 64, ui._read_only_fingerprint(mixie, issue))
                self.assertEqual(3, hashed.call_count)

    def test_same_song_uses_matched_release_guest_even_if_source_differs(self):
        details = {'title': 'I Want to Believe', 'artist-credit-phrase':
                   'In This Moment feat. Rory Rodriguez of Dayseeker'}
        release_title = 'I Want to Believe (feat. Rory Rodriguez of Dayseeker)'
        title, artist = mixie._feature_aware_identity(details, release_title, 'I Want to Believe',
                        {'title': ('I Want to Believe (feat. Someone Else)',),
                         'artist': ('In This Moment',)})
        self.assertEqual(('In This Moment', release_title), (artist, title))
        with self.assertRaisesRegex(ValueError, 'Different featured'):
            mixie._feature_aware_identity(details, release_title, 'I Want to Believe',
                         {'title': ('Different Song (feat. Someone Else)',),
                          'artist': ('In This Moment',)})
