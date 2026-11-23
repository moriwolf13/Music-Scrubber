"""Keep independently established release numbering when artist review blocks import.

All media and state are disposable. Run with tests/run_isolated.py only.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json

import audio
import lookup
import mixie
import mixie_review_ui as ui
import tag
from support import WorkspaceCase, make_audio, metadata_record


class VerifiedNumberingReviewTests(WorkspaceCase):
    def _album(self, *, weak=False, suffix="flac"):
        files = [make_audio(self.stage / 'Source' / f'{index}.{suffix}',
                            album='NEED', title=f'Song {index}', artist='3OH!3',
                            albumartist='3OH!3') for index in (1, 2, 3)]
        rows = {path: metadata_record(path, album=('NEED',), title=(f'Song {idx}',),
                  artist=('Other Act' if idx == 3 else '3OH!3',),
                  album_artist=('3OH!3',), date=('2024',), genre=('Rock',),
                  track_number=(), track_total=(), disc_number=(), disc_total=())
                for idx, path in enumerate(files, 1)}
        album = {'album': 'NEED', 'tracks': list(rows.values()), 'folder': files[0].parent}
        audits = [{'path': path, 'status': 'verified',
                   'details': {'id': f'id-{idx}', 'title': f'Song {idx}',
                               'artist-credit-phrase': '3OH!3'},
                   'assessment': {'status': 'verified', 'complete': True}}
                  for idx, path in enumerate(files, 1)]
        evaluation = {'release_id': 'album-release', 'status': 'complete',
                      'matched': {(1, idx): path for idx, path in enumerate(files, 1)},
                      'tracklist': [{'disc': '1', 'number': str(idx),
                                     'recording_id': f'id-{idx}', 'title': f'Song {idx}'}
                                    for idx in (1, 2, 3)],
                      'missing': [], 'review': [], 'errors': [], 'reasons': []}
        self.stack.enter_context(patch.object(tag, 'read_track_metadata',
            side_effect=lambda path: deepcopy(rows[Path(path)])))
        self.stack.enter_context(patch.object(audio, 'audit_tracks', return_value=deepcopy(audits)))
        self.stack.enter_context(patch.object(lookup, '_accepted_recording',
            side_effect=lambda audit, independent=False: None if weak else 'proven'))
        self.stack.enter_context(patch.object(lookup, 'discover_release_candidates', return_value=['album-release']))
        self.stack.enter_context(patch.object(lookup, 'evaluate_release_candidate', return_value=deepcopy(evaluation)))
        self.stack.enter_context(patch.object(lookup, 'common_release_metadata', return_value={
            'album':'NEED', 'album_artist':'3OH!3', 'date':'2024'}))
        return album, rows

    def _issue(self, *, weak=False, suffix="flac"):
        """A preserved pre-policy review; old decisions must remain inspectable.

        New verified full releases auto-replace Artist, but historic review
        records still need honest source-bound numbering, including MP3 cases.
        """
        album, rows = self._album(weak=weak, suffix=suffix)
        issue = mixie._album_issue(album)
        manifest = mixie.completion_source_manifest(issue)
        fingerprint = mixie.completion_fingerprint(
            issue['import_name'], mixie._source_rows(issue, manifest), manifest)
        issue['details'] = {'kind': 'metadata_conflict', 'field': 'artist',
            'existing_values': ('Other Act',), 'reference_value': '3OH!3',
            'track': '3.' + suffix,
            'verified_release_numbering': {
                'source_fingerprint': fingerprint, 'release_id': 'album-release',
                'positions': {path.relative_to(self.stage).as_posix(): [1, idx]
                              for idx, path in enumerate(rows, 1)},
                'track_totals': {'1': 3},
                'album_metadata': {'album_artist': '3OH!3', 'date': '2024'}},
            'tracks': [], 'releases': []}
        issue['source_fingerprint'] = fingerprint
        return issue, rows

    def test_conflict_keeps_source_bound_full_release_numbering_and_originals(self):
        issue, rows = self._issue()
        self.assertEqual(issue['details']['kind'], 'metadata_conflict')
        evidence = ui._release_disc_structure(issue, ui._view(mixie, issue))
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence[1], {1: 3})
        self.assertEqual(set(evidence[0]), set(issue['tracks']))
        self.assertEqual({pair[1] for pair in evidence[0].values()}, {1, 2, 3})
        blockers = ui.preflight_issues(mixie, issue)
        self.assertFalse(any('Track Number missing' in problem for problem in blockers))
        self.assertFalse(any('Track Total missing' in problem for problem in blockers))
        self.assertTrue(any('Metadata conflict' in problem for problem in blockers))
        with redirect_stdout(StringIO()) as output, patch.object(ui, 'input', return_value='0'):
            ui._show_tracks(mixie, issue, [issue])
        self.assertIn('Verified release:', output.getvalue())
        self.assertTrue(all(not tag.read_track_metadata(p)['track_number'] for p in rows))
        self.assertEqual(issue.get('review_status'), None)

    def test_mp3_review_uses_numbering_without_changing_guest_credit(self):
        issue, rows = self._issue(suffix='mp3')
        self.assertEqual(ui._release_disc_structure(issue, ui._view(mixie, issue))[1], {1: 3})
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual(sorted(t['number'] for t in decision['tracks'].values()), [1, 2, 3])
        self.assertEqual(next(t['artist'] for name, t in decision['tracks'].items()
                              if name.endswith('3.mp3')), 'Other Act')
        self.assertTrue(all(not tag.read_track_metadata(path)['track_number'] for path in rows))

    def test_review_build_uses_full_release_numbers_without_manually_typing_them(self):
        issue, rows = self._issue()
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual(sorted(v['number'] for v in decision['tracks'].values()), [1, 2, 3])
        self.assertTrue(all(v['disc'] == 1 for v in decision['tracks'].values()))
        self.assertTrue(all(v['track_total'] == 3 for v in decision['totals'].values()))
        guest = next(item for relative, item in decision['tracks'].items() if relative.endswith('3.flac'))
        self.assertEqual(guest['artist'], 'Other Act')
        self.assertFalse(decision['independently_verified'])
        self.assertEqual(issue.get('review_status'), None)

    def test_automatic_preview_now_resolves_matched_artist_without_review(self):
        album, rows = self._album()
        with patch.object(mixie, 'select_album_art', return_value={
                'mime': 'image/jpeg', 'data': b'verified-test-art', 'source': 'fixture'}), \
             redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, 'DISPOSABLE-TEST', auto_mode=True)
        self.assertEqual(3, len(plan['matched']))
        self.assertEqual('independently_verified', plan['provenance'])
        self.assertEqual({identity['artist'] for identity in plan['identities'].values()}, {'3OH!3'})
        self.assertEqual({str(item['value']) for item in plan['proposals']
                          if item['field'] == 'track_number'}, {'1', '2', '3'})
        self.assertTrue(all(not tag.read_track_metadata(path)['track_number'] for path in rows))

    def test_stale_and_incomplete_numbering_evidence_is_not_used(self):
        issue, _ = self._issue()
        mapping = ui._view(mixie, issue)
        original = deepcopy(issue['details']['verified_release_numbering'])
        for changed in ({'source_fingerprint': '0'*64},
                        {'positions': {issue['tracks'][0]: [1, 1]}},
                        {'track_totals': {'1': 1}},
                        {'positions': dict(original['positions'], **{issue['tracks'][0]: [1, 99]})}):
            with self.subTest(changed=changed):
                issue['details']['verified_release_numbering'] = {**original, **changed}
                self.assertIsNone(ui._release_disc_structure(issue, mapping))
        issue['details']['verified_release_numbering'] = original
        issue.pop('source_fingerprint')
        self.assertIsNone(ui._release_disc_structure(issue, mapping))

    def test_verified_full_release_uses_matched_lead_over_source_comma_credit(self):
        album, rows = self._album()
        third = next(row for row in album["tracks"] if Path(row["path"]).name == "3.flac")
        third["artist"] = ("3OH!3, The Used_Bert McCracken",)
        original = third["artist"]
        with patch.object(mixie, "select_album_art", return_value={
                 "mime": "image/jpeg", "data": b"verified-test-art", "source": "fixture"}), \
             redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, "DISPOSABLE-TEST", auto_mode=True)
        matched = plan["identities"][str(Path(third["path"]).resolve())]
        self.assertEqual(matched["artist"], "3OH!3")
        self.assertEqual(third["artist"], original)  # Stage and original untouched.
        self.assertEqual(plan["auto_album_fields"]["album_artist"], "3OH!3")

    def test_nonindependent_recordings_never_mint_numbering_evidence(self):
        # Reuse the same method but make a selection fail the auto safety floor.
        # The preview may raise an identity review before reaching artist checks.
        album, _ = self._album(weak=True)
        with self.assertRaises(Exception) as raised, redirect_stdout(StringIO()):
            mixie.preview_album(album, 'DISPOSABLE-TEST', auto_mode=True)
        self.assertFalse(getattr(raised.exception, 'release_numbering', None))


if __name__ == '__main__':
    import unittest
    unittest.main()
