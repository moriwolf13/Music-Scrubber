"""v8b: default single in manual review, without inventing release proof."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import mixie
import mixie_review_ui as ui
import tag
from support import WorkspaceCase, make_audio


class V8BDiscReviewTests(WorkspaceCase):
    def sample(self, *, disc_tags=None):
        tracks = []
        for i in (1, 2):
            kwargs = dict(album='Sample', artist='Artist', albumartist='Artist',
                          title=f'Song {i}', tracknumber=i, tracktotal=2,
                          date='2024', genre='Pop')
            if disc_tags is not None:
                kwargs.update(discnumber=disc_tags[i-1], disctotal=max(disc_tags))
            p = make_audio(self.stage/'Album'/f'{i:02}.flac', **kwargs)
            tracks.append(tag.read_track_metadata(p))
        issue = dict(album='Sample', import_name='Album',
                     tracks=[row['path'].relative_to(self.stage).as_posix() for row in tracks],
                     details={'kind':'identity_release', 'releases':[]}, reason='Release incomplete')
        issue['source_fingerprint'] = mixie.review_fingerprint(issue)
        issue['details']['source_fingerprint'] = issue['source_fingerprint']
        return issue

    def test_unknown_layout_defaults_single_only_for_review(self):
        issue=self.sample()
        mapping=ui._view(mixie,issue)
        self.assertEqual('auto',ui._disc_layout(mixie,issue,mapping))
        self.assertEqual('single',ui._review_disc_layout(mixie,issue,mapping))
        self.assertEqual('single',ui._album_summary(mixie,issue,mapping)['disc_layout'])
        self.assertFalse(any('Disc Number missing' in x or 'Disc structure uncertain' in x
                             for x in ui.preflight_issues(mixie,issue)))
        decision=ui._build_user_interpretation(mixie,issue)
        self.assertEqual({1},{row['disc'] for row in decision['tracks'].values()})
        self.assertEqual({1},{row['disc_total'] for row in decision['totals'].values()})
        self.assertTrue(decision['user_approved'])
        self.assertFalse(decision['independently_verified'])

    def test_incomplete_musicbrainz_does_not_become_online_verification(self):
        issue=self.sample()
        issue['details']['releases']=[{'status':'partial','tracklist_complete':False,
                                      'matched':{'[1,1]':issue['tracks'][0]},
                                      'tracklist':[{'disc':'1','number':'1'}]}]
        mapping=ui._view(mixie,issue)
        self.assertIsNone(ui._release_disc_structure(issue,mapping))
        self.assertEqual('single',ui._review_disc_layout(mixie,issue,mapping))
        self.assertEqual('Not confirmed', ui._album_summary(mixie,issue,mapping)['disc_total'])

    def test_explicit_local_multidisc_stays_multiple(self):
        issue=self.sample(disc_tags=(1,2))
        mapping=ui._view(mixie,issue)
        self.assertEqual('multiple',ui._review_disc_layout(mixie,issue,mapping))

    def test_complete_multidisc_remote_evidence_is_used_despite_unknown_competitor(self):
        issue=self.sample()
        issue['details']['releases']=[{
            'status':'complete', 'tracklist_complete':True, 'errors':[],
            'matched': {'[1,1]':issue['tracks'][0], '[2,1]':issue['tracks'][1]},
            'tracklist':[{'disc':'1','number':'1'},{'disc':'2','number':'1'}]
        }, {'status':'partial','tracklist_complete':False,'matched':{},'tracklist':[]}]
        mapping=ui._view(mixie,issue)
        self.assertIsNone(ui._release_disc_structure(issue,mapping))
        self.assertEqual('auto',ui._disc_layout(mixie,issue,mapping))
        self.assertEqual('multiple',ui._review_disc_layout(mixie,issue,mapping))

    def test_single_and_multiple_are_only_visible_editor_choices(self):
        issue=self.sample()
        with patch('builtins.input', side_effect=['m','7','A','0']), redirect_stdout(StringIO()) as buf:
            ui._show_album(mixie,issue,[issue])
        output=buf.getvalue()
        self.assertIn('M. Multiple-disc options (only if needed)', output)
        self.assertIn('Disc Layout: Multiple (S=Single, M=Multiple)', output)
        self.assertNotIn('A=Auto',output)
        self.assertIn('Auto is no longer a review option.',output)
        self.assertEqual('multiple', issue['tag_edits']['album']['disc_layout'])

    def test_legacy_auto_saved_selection_still_works_as_single_default(self):
        issue=self.sample()
        issue['tag_edits']={'album':{'disc_layout':'auto'},'tracks':{}}
        mapping=ui._view(mixie,issue)
        self.assertEqual('single',ui._review_disc_layout(mixie,issue,mapping))
        self.assertTrue(ui._build_user_interpretation(mixie,issue)['user_approved'])
