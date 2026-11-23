"""v7 review guidance, safe optional autofill, consistent UI, and compact logs."""
import hashlib
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import mixie
import mixie_review_ui as ui
import mixie_terminal as terminal
import lookup
import tag
from support import WorkspaceCase, make_audio


class AutomationV7Tests(WorkspaceCase):
    def example(self, *, missing=False):
        tracks=[]
        for i in (1,2):
            path=make_audio(self.stage/'Import'/f'{i}.flac', album='Album',
                            artist='Act feat. Guest' if i==2 else 'Act',
                            albumartist='Act', title=f'Song {i}',
                            tracknumber=i,discnumber=1,date='2020',genre='Electronic')
            tracks.append(tag.read_track_metadata(path))
        issue={'album':'Album','import_name':'Import',
               'tracks':[Path(t['path']).relative_to(self.stage).as_posix() for t in tracks],
               'details':{'kind':'identity_release'},'reason':'release editions disagree'}
        if missing:
            for row in tracks:
                row['album_artist']=()
                row['genre']=()
                row['date']=()
        return tracks,issue

    def test_missing_fields_all_shown_before_approval(self):
        tracks,issue=self.example()
        issue['tag_edits']={'album':{'album_artist':'Act','date':'2020'},'tracks':{}}
        issue['manual_fields']={'genre':'Electronic'}
        # This source already has all mandatory fields; only edition remains.
        report=ui.preflight_issues(mixie,issue)
        self.assertTrue(any('release editions disagree' in item for item in report))
        self.assertFalse(any('Release Date missing' in item for item in report))

    def test_preflight_identifies_every_missing_album_field_and_duplicates(self):
        tracks,issue=self.example()
        mapping=ui._view(mixie,issue)
        with patch.object(ui,'_view',return_value={key:{**value, 'album_artist':(), 'date':(), 'genre':()}
                                                       for key,value in mapping.items()}):
            issue['tag_edits']={'album':{},'tracks':{issue['tracks'][1]:{'track_number':'1'}}}
            report=ui.preflight_issues(mixie,issue)
        # A unanimous primary track artist is an effective album-artist default.
        self.assertFalse(any('Album Artist' in item for item in report))
        self.assertTrue(any('Release Date' in item for item in report))
        # Missing genre stays unresolved and is not an import blocker.
        self.assertFalse(any('Genre missing' in item for item in report))
        self.assertTrue(any('Duplicate track 1' in item for item in report))

    def test_artist_number_is_rejected_without_changing_source(self):
        tracks,issue=self.example()
        original={item['path']:hashlib.sha256(item['path'].read_bytes()).digest() for item in tracks}
        with self.assertRaisesRegex(ValueError,'cannot be only a number'):
            ui.save_choice(mixie,issue,[issue],field='artist',value='11',relative=issue['tracks'][1])
        self.assertEqual({},issue.get('tag_edits',{}))
        self.assertTrue(all(hashlib.sha256(path.read_bytes()).digest()==check for path,check in original.items()))

    def test_guest_credit_change_requires_explicit_confirmation(self):
        tracks,issue=self.example()
        with patch('builtins.input',side_effect=['n','3','Act','', '0']), redirect_stdout(StringIO()) as stream:
            # Single-disc field Artist=3; replacing guest credit should ask for REPLACE.
            ui._show_tracks(mixie,issue,[issue])
        self.assertNotIn('tracks',issue.get('tag_edits',{}))
        self.assertIn('preserved',stream.getvalue())

    def test_consensus_autofill_requires_explicit_fill(self):
        tracks,issue=self.example()
        view=ui._view(mixie,issue)
        missing={key:{**value, 'album_artist':(), 'date':(), 'genre':()} for key,value in view.items()}
        issue['source_fingerprint'] = mixie.review_fingerprint(issue)
        issue['details']['source_fingerprint'] = issue['source_fingerprint']
        # Provider evidence is a complete source-bound mapping, but an approval
        # isn't silently created by merely displaying the candidate list.
        issue['details']['releases']=[{'status':'complete','tracklist_complete':True,'errors':[],
            'release_id':'release-1','matched':{'[1,1]':issue['tracks'][0],'[1,2]':issue['tracks'][1]},
            'tracklist':[{'disc':'1','number':'1'},{'disc':'1','number':'2'}]}]
        with patch.object(ui,'_view',return_value=missing), \
             patch.object(lookup,'common_release_metadata',return_value={'album_artist':'Act','date':'2020'}), \
             patch('builtins.input',return_value=''), redirect_stdout(StringIO()):
            self.assertEqual(0,ui._autofill_supported(mixie,issue,[issue]))
        self.assertFalse(issue.get('tag_edits'))
        with patch.object(ui,'_view',return_value=missing), \
             patch.object(lookup,'common_release_metadata',return_value={'album_artist':'Act','date':'2020'}), \
             patch('builtins.input',return_value='FILL'),redirect_stdout(StringIO()), \
             patch.object(mixie,'save_review_queue'):
            self.assertEqual(1,ui._autofill_supported(mixie,issue,[issue]))
        # Primary album artist is already defaulted from the existing tracks;
        # only the date needs explicit online consensus filling.
        self.assertEqual('Act', ui._album_summary(mixie, issue, missing)['album_artist'])
        self.assertEqual('2020',issue['manual_fields']['date'])
        self.assertNotIn('interpretation',issue)

    def test_incomplete_competing_release_cannot_supply_auto_metadata(self):
        tracks,issue=self.example()
        view=ui._view(mixie,issue)
        missing={key:{**value, 'album_artist':(), 'date':(), 'genre':()} for key,value in view.items()}
        issue['details']['releases']=[{'status':'complete','tracklist_complete':True,'errors':[],
            'release_id':'good','matched':{'[1,1]':issue['tracks'][0],'[1,2]':issue['tracks'][1]},
            'tracklist':[{'disc':'1','number':'1'},{'disc':'1','number':'2'}]},
            {'status':'partial','tracklist_complete':False,'errors':[{'stage':'release_tracklist'}],
             'release_id':'unknown','matched':{},'tracklist':[]}]
        with patch.object(ui,'_view',return_value=missing), \
             patch.object(lookup,'common_release_metadata',side_effect=AssertionError('unsafe lookup')) as fetch, \
             patch('builtins.input',return_value=''), redirect_stdout(StringIO()):
            self.assertEqual(0,ui._autofill_supported(mixie,issue,[issue]))
        fetch.assert_not_called()
        self.assertFalse(issue.get('tag_edits'))

    def test_version_titles_collapse_brackets_only_and_not_recording_versions(self):
        self.assertEqual(mixie._edition_title_key('Song [Jupiter remix]'),
                         mixie._edition_title_key('Song (Jupiter remix)'))
        self.assertNotEqual(mixie._edition_title_key('Song [Live]'),
                            mixie._edition_title_key('Song (Acoustic)'))

    def test_compact_logs_preserve_output_and_do_not_mask_exception(self):
        out=StringIO()
        with redirect_stdout(out):
            with terminal.compact_operation('Test staging',root=self.root):
                print('some long staging output')
        text=out.getvalue()
        self.assertNotIn('some long staging output',text)
        self.assertIn('complete',text)
        logs=list((self.root/'.mixie_logs').glob('operation-*.log'))
        self.assertEqual(1,len(logs))
        self.assertIn('some long staging output',logs[0].read_text())
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(RuntimeError,'cannot proceed'):
                with terminal.compact_operation('Test failure',root=self.root):
                    raise RuntimeError('cannot proceed')

    def test_themed_output_is_plain_when_redirected(self):
        out=StringIO()
        with redirect_stdout(out):
            terminal.themed_print('=== Review: Album ===')
        self.assertEqual('=== Review: Album ===\n',out.getvalue())
