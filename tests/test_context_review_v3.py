"""Offline regression for explicit, album-context assisted review.

Album identity is established only by the selected complete remote tracklist
and independent audio evidence. Source text can order hints, never approve them.
"""
from contextlib import redirect_stdout
from io import StringIO
from unittest import TestCase
from unittest.mock import patch

import review


def candidate(identifier, title, artist='3OH!3', *, status='verified', score=.99, reasons=()):
    return {'recording_id': identifier, 'status': status, 'score': score,
            'reasons': list(reasons), 'details': {'id': identifier, 'title': title,
            'artist-credit-phrase': artist, 'length': 200000}}


def song(file, options, status='conflicting', *, errors=None):
    return {'path': 'Import/' + file, 'existing': {'title': (file[:-4],), 'artist': ('3OH!3',)},
            'assessment': {'status': status, 'complete': True, 'candidates': options,
                           'errors': errors or []}}


def remote(identifier, position, title):
    return {'recording_id': identifier, 'disc': '1', 'number': str(position), 'title': title}


def context_case():
    rows = [
        song('Starstrukk.mp3', [candidate('gaga', 'Stupid Love', 'Lady Gaga'),
                               candidate('audiogroove', 'Starstrukk', 'Audiogroove'),
                               candidate('starstrukk', 'Starstrukk', '3OH!3 feat. Katy Perry')]),
        song('Touchin on My.mp3', [candidate('touchin', 'Touchin on My')], 'verified'),
        song('Streets of Gold.mp3', [candidate('streets', 'Streets of Gold')], 'verified'),
        song('Double Vision.mp3', [candidate('double', 'Double Vision')], 'verified'),
        song('Dont Trust Me.mp3', [candidate('touchin', 'Touchin on My'),
                                   candidate('dont', "Don't Trust Me")]),
    ]
    state = {'release_id':'selected-release', 'release_tracks':[
        remote('starstrukk', 1, 'Starstrukk'), remote('touchin', 2, 'Touchin on My'),
        remote('streets', 3, 'Streets of Gold'), remote('double', 4, 'Double Vision'),
        remote('dont', 5, "Don't Trust Me")], 'tracks': {}}
    return rows, state


class ContextAssistedReviewTests(TestCase):
    def test_related_source_hint_is_ranked_above_spurious_artist_match(self):
        row = song('Starstrukk.mp3', [candidate('gaga', 'Stupid Love', 'Lady Gaga'),
                                      candidate('groove', 'Starstrukk', 'Audiogroove'),
                                      candidate('three', 'Starstrukk', '3OH!3 feat. Katy Perry')])
        original = [item['recording_id'] for item in row['assessment']['candidates']]
        result = review._ranked_display_hints(row)
        self.assertEqual('three', result[0]['recording_id'])
        self.assertEqual(original, [item['recording_id'] for item in row['assessment']['candidates']])
        self.assertIsNone(review.suggested_identity(row))
        with redirect_stdout(StringIO()) as capture:
            review.show_tag_suggestions([row])
        shown = capture.getvalue()
        self.assertLess(shown.index('Starstrukk — 3OH!3'), shown.index('Stupid Love'))
        self.assertIn('unverified source tags', shown)
        self.assertIn('no audio or source tags changed', shown)

    def test_complete_release_and_three_independent_anchors_resolve_spurious_candidates(self):
        rows, state = context_case()
        issue = {}
        captured = []
        with patch.object(review, '_ask', return_value='APPROVE BATCH'), redirect_stdout(StringIO()) as output:
            count = review._batch_supported_tracks(issue, {'tracks':rows}, state,
                                                    lambda _: captured.append(dict(state['tracks'])))
        self.assertEqual(5, count)
        self.assertEqual('starstrukk', state['tracks']['Import/Starstrukk.mp3']['recording_id'])
        self.assertEqual('dont', state['tracks']['Import/Dont Trust Me.mp3']['recording_id'])
        self.assertIn('album-context resolution', output.getvalue())
        self.assertEqual(1, len(captured))
        self.assertEqual(5, len(captured[0]))

    def test_competing_candidates_on_selected_release_do_not_auto_resolve(self):
        rows, state = context_case()
        state['release_tracks'].append(remote('gaga', 6, 'Stupid Love'))
        with patch.object(review, '_ask', return_value='APPROVE BATCH'), redirect_stdout(StringIO()):
            count = review._batch_supported_tracks({}, {'tracks': rows}, state, lambda _: None)
        self.assertEqual(4, count)
        self.assertNotIn('Import/Starstrukk.mp3', state['tracks'])

    def test_incomplete_evidence_cannot_resolve_spurious_candidates(self):
        rows, state = context_case()
        rows[0]['assessment']['errors'] = [{'stage':'musicbrainz', 'retryable':True}]
        rows[4]['assessment']['complete'] = False
        with patch.object(review, '_ask', return_value='APPROVE BATCH'), redirect_stdout(StringIO()):
            self.assertEqual(3, review._batch_supported_tracks({}, {'tracks':rows}, state, lambda _: None))
        self.assertNotIn('Import/Starstrukk.mp3', state['tracks'])
        self.assertNotIn('Import/Dont Trust Me.mp3', state['tracks'])

    def test_two_independent_anchors_required_before_context_resolution(self):
        rows, state = context_case()
        for row in rows[1:4]:
            if row['path'] != 'Import/Touchin on My.mp3':
                row['assessment']['status'] = 'conflicting'
        with patch.object(review, '_ask', return_value='APPROVE BATCH'), redirect_stdout(StringIO()):
            count = review._batch_supported_tracks({}, {'tracks':rows}, state, lambda _: None)
        self.assertEqual(0, count) if count == 0 else self.assertNotIn('Import/Starstrukk.mp3', state['tracks'])

    def test_reject_batch_does_not_change_drafts(self):
        rows, state = context_case()
        before = repr(state)
        with patch.object(review, '_ask', return_value='no'), redirect_stdout(StringIO()):
            self.assertEqual(0, review._batch_supported_tracks({}, {'tracks':rows}, state, lambda _: None))
        self.assertEqual(before, repr(state))


class MetadataContextTests(TestCase):
    def test_featured_guest_preserves_main_album_artist(self):
        import mixie
        rows = [{'artist': ('3OH!3',)}, {'artist': ('3OH!3 feat. Katy Perry',)},
                {'artist': ('3OH!3 ft. Ke$ha',)}]
        self.assertEqual('3OH!3', mixie._suggest_album_artist(rows))
        self.assertIsNone(mixie._suggest_compilation_album_artist(rows))

    def test_compilation_hint_never_claims_single_primary_artist(self):
        import mixie
        tracks = [{'artist': (name,)} for name in ('One', 'Two', 'Three', 'Four')]
        self.assertIsNone(mixie._suggest_album_artist(tracks))
        self.assertEqual('Various Artists', mixie._suggest_compilation_album_artist(tracks))

    def test_different_album_genre_only_suggested_on_unanimous_catalog(self):
        import mixie
        current = {'tracks':[{'path':'/tmp/Current.mp3','artist':('Caravan Palace',),'genre':()}]}
        album_a = {'tracks':[{'path':'/tmp/OtherA.mp3','album':('Other A',),'artist':('Caravan Palace',),'genre':('Electro Swing',)}]}
        album_b = {'tracks':[{'path':'/tmp/OtherB.mp3','album':('Other B',),'artist':('Caravan Palace',),'genre':('Electro Swing',)}]}
        with patch.object(mixie.tag, 'group_library_albums',return_value={'one':album_a}), \
             patch.object(mixie.tag, 'group_staged_albums',return_value={'two':album_b}):
            self.assertEqual('Electro Swing',mixie._catalog_genre_hint(current,'Caravan Palace'))
        album_b['tracks'][0]['genre'] = ('Dance',)
        with patch.object(mixie.tag, 'group_library_albums',return_value={'one':album_a}), \
             patch.object(mixie.tag, 'group_staged_albums',return_value={'two':album_b}):
            self.assertIsNone(mixie._catalog_genre_hint(current,'Caravan Palace'))

    def test_identical_ids_metadata_consensus_does_not_pick_recording(self):
        row = song('Over Your Shoulder.mp3',
            [candidate('a','Over Your Shoulder','Chromeo'),
             candidate('b','Over Your Shoulder','Chromeo'),
             candidate('c','Over Your Shoulder','Chromeo')])
        shared = review.consensus_identity(row)
        self.assertEqual(3, shared['count'])
        self.assertEqual('Over Your Shoulder',shared['title'])
        self.assertIsNone(review.suggested_identity(row))
        with redirect_stdout(StringIO()) as stream:
            review.show_tag_suggestions([row])
        self.assertIn('Metadata agrees across 3 audio candidates',stream.getvalue())
        row['assessment']['candidates'][1]['details']['title']='Remix'
        self.assertIsNone(review.consensus_identity(row))

    def test_catalog_context_does_not_override_existing_tag(self):
        import tag
        from pathlib import Path
        from unittest.mock import patch
        path = Path('/tmp/for-unit-test.mp3')
        with patch.object(tag, 'read_track_metadata',return_value={
                'date': ('2020',), 'genre': ('Indie',), 'path': path}):
            proposals = tag.propose_required_album_metadata(
                {('1','1'):path},{},contextual_genre='Electro Swing')
        self.assertEqual([],proposals)


class ArtworkCoverageTests(TestCase):
    def test_one_missing_release_cover_uses_agreeing_available_cover(self):
        import mixie
        from support import make_cover
        cover = make_cover()
        calls=[]
        def art(release_id, preview=False):
            calls.append((release_id, preview))
            if release_id=='unavailable':
                return None
            return cover
        from unittest.mock import patch
        with patch.object(mixie, 'album_asset_root',return_value=None), \
             patch.object(mixie.lookup, 'download_release_art',side_effect=art), \
             redirect_stdout(StringIO()) as output:
            result=mixie.select_album_art({'tracks':[]},['available','unavailable'])
        self.assertEqual(cover['data'],result['data'])
        self.assertIn('edition covers are unavailable',output.getvalue())

    def test_no_available_art_still_defers(self):
        import mixie
        from unittest.mock import patch
        with patch.object(mixie, 'album_asset_root',return_value=None), \
             patch.object(mixie.lookup, 'download_release_art',return_value=None):
            with self.assertRaisesRegex(ValueError,'No reference artwork'):
                mixie.select_album_art({'tracks':[]},['unavailable'])
