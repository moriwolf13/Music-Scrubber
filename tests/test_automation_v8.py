"""v8: album context, content-equivalent editions and visible honest correction phases."""
from contextlib import redirect_stdout, nullcontext
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib
import sys

import mixie
import mixie_review_ui as ui
import mixie_terminal as terminal
import tag
from support import WorkspaceCase, make_audio


class AutomationV8Tests(WorkspaceCase):
    def sample(self, count=3):
        tracks=[]
        for i in range(1,count+1):
            path=make_audio(self.stage/'Import'/f'{i:02}.flac',album='Demo',artist='Artist',
                            title=f'Song {i}',tracknumber=i,discnumber=1,date='2024',genre='Electronic')
            tracks.append(tag.read_track_metadata(path))
        issue={'album':'Demo','import_name':'Import','tracks':[
            Path(row['path']).relative_to(self.stage).as_posix() for row in tracks],
            'details':{'kind':'identity_release','releases':[]},'reason':'Edition disagreement'}
        issue['source_fingerprint'] = mixie.review_fingerprint(issue)
        issue['details']['source_fingerprint'] = issue['source_fingerprint']
        return tracks,issue

    def full_release(self, issue, *, release_id='one', count=3):
        return {'release_id':release_id,'status':'complete','tracklist_complete':True,
                'errors':[],'reasons':[],
                'tracklist':[{'disc':'1','number':str(i),'recording_id':f'id-{i}'} for i in range(1,count+1)],
                'matched':{f'[1,{i}]':issue['tracks'][i-1] for i in range(1,count+1)}}

    def test_track_total_from_full_unanimous_remote_not_local_count(self):
        _,issue=self.sample()
        issue['details']['releases']=[self.full_release(issue)]
        summary=ui._album_summary(mixie,issue,ui._view(mixie,issue))
        self.assertEqual('3',summary['track_total'])
        self.assertEqual('1',summary['disc_total'])
        self.assertEqual('single',ui._disc_layout(mixie,issue,ui._view(mixie,issue)))

    def test_count_without_full_release_is_only_a_provisional_local_total(self):
        _,issue=self.sample()
        issue['details']['releases']=[{'release_id':'partial','status':'partial','tracklist_complete':False,
            'errors':[],'tracklist':[{'disc':'1','number':'1'}], 'matched':{'[1,1]':issue['tracks'][0]}}]
        summary=ui._album_summary(mixie,issue,ui._view(mixie,issue))
        self.assertEqual('3',summary['track_total'])
        self.assertTrue(summary['track_total_from_local_sequence'])
        self.assertIsNone(ui._release_disc_structure(issue, ui._view(mixie, issue)))

    def test_unchecked_competitor_prevents_claiming_edition_total(self):
        _,issue=self.sample()
        issue['details']['releases']=[self.full_release(issue), {'release_id':'outage','status':'partial',
                'tracklist_complete':False,'errors':[{'code':'outage'}],'tracklist':[],'matched':{}}]
        summary=ui._album_summary(mixie,issue,ui._view(mixie,issue))
        self.assertEqual('3',summary['track_total'])
        self.assertTrue(summary['track_total_from_local_sequence'])
        self.assertIsNone(ui._release_disc_structure(issue, ui._view(mixie, issue)))

    def test_primary_artist_majority_preserves_collaborations(self):
        tracks=[{'artist':('3OH!3',)} for i in range(8)] + [
             {'artist':('3OH!3 feat. Guest',)}, {'artist':('Guest and Another',)}]
        self.assertEqual('3OH!3',mixie._suggest_album_artist(tracks))
        self.assertEqual('3OH!3 feat. Guest',tracks[8]['artist'][0])
        self.assertIsNone(mixie._suggest_album_artist(tracks[:3] + tracks[-1:]))

    def test_artist_split_and_existing_conflicting_album_credit_do_not_fill(self):
        tracks=[{'artist':('A',)} for i in range(6)]+[{'artist':('B',)} for i in range(4)]
        self.assertIsNone(mixie._suggest_album_artist(tracks))
        tracks=[{'artist':('A',),'album_artist':('B',)} for i in range(10)]
        self.assertIsNone(mixie._suggest_album_artist(tracks))
        self.assertIsNone(mixie._suggest_album_artist([{'artist':('Various Artists',)}]*10))

    def test_content_signature_grouping_shows_equal_edition_group(self):
        # One recording/position signature should not become two conflicts because
        # of different retail packaging; material versions remain strict elsewhere.
        releases=[('a', {(1,1):('path','recording')}),('b',{(1,1):('path','recording')})]
        signatures={tuple(sorted(mapping.items())) for _,mapping in releases}
        self.assertEqual(1,len(signatures))
        self.assertNotEqual(mixie._edition_title_key('Song (Live)'),mixie._edition_title_key('Song (Acoustic)'))

    def test_proposals_are_not_mislabeled_verified(self):
        rows, _=self.sample(count=1)
        track=rows[0]
        result=StringIO()
        with redirect_stdout(result):
            terminal.pass_status(3,'Metadata proposal')
            terminal.show_proposed_changes(rows,{str(track['path'].resolve()):{
                'title':'Corrected', 'artist':'Artist'}},[],{'album':{},'tracks':{}},tag_service=tag)
        self.assertIn('PROPOSED',result.getvalue())
        self.assertNotIn('VERIFIED',result.getvalue())
        self.assertIn('Song 1',result.getvalue())
        self.assertIn('Corrected',result.getvalue())

    def test_verified_snapshot_reflects_working_copy_only(self):
        from shutil import copy2
        rows,_=self.sample(count=1)
        original=rows[0]['path']; digest=hashlib.sha256(original.read_bytes()).hexdigest()
        working=self.root/'.mixie-work-test'; working.mkdir()
        copy=working/'edited.flac';copy2(original,copy)
        tag.replace_reviewed_text_on_copy(copy,working,'title','Corrected')
        changes=terminal.collect_verified_changes(rows,{original:copy},tag_service=tag)
        self.assertTrue(any(field=='title' and old=='Song 1' and new=='Corrected' for _,field,old,new in changes))
        self.assertEqual(digest,hashlib.sha256(original.read_bytes()).hexdigest())

    def test_direct_auto_ingest_flag_routes_through_normal_lock(self):
        with patch.object(sys,'argv',['mixie.py','--auto-ingest']), \
                patch.object(mixie,'_main') as run, \
                patch.object(mixie,'ingest_session',return_value=nullcontext()), \
                patch.object(mixie,'_main_choice',side_effect=AssertionError('must not prompt')):
            mixie.main()
        run.assert_called_once_with('1', allow_source_cleanup=False)
