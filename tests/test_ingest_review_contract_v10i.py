"""Import-first review boundaries on disposable audio/state, never live data."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json

import mixie
import mixie_review_ui as ui
import review
import sort
import tag
from support import WorkspaceCase, make_audio


class IngestReviewContractTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        (self.script / '.mixie-import-first.json').write_text(json.dumps({
            'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')

    def album(self):
        rows = []
        for number in (1, 2):
            path = make_audio(self.stage / 'Delivery' / f'{number:02d}.flac',
                album='Album', albumartist='Artist', artist='Artist', title=f'Song {number}',
                tracknumber=number, tracktotal=2, discnumber=1, disctotal=1,
                date='2020', genre='Rock')
            rows.append(tag.read_track_metadata(path))
        album = {'album': 'Album', 'tracks': rows, 'folder': rows[0]['path'].parent}
        issue = {**mixie._album_issue(album), 'status': 'needs_review',
                 'reason': 'Synthetic unresolved evidence',
                 'details': {'kind': 'metadata_conflict', 'field': 'title',
                             'existing_values': ['Old'], 'reference_value': 'New'}}
        return album, issue

    def approve(self, issue):
        decision = ui._build_user_interpretation(mixie, issue)
        issue.update(interpretation=decision, source_fingerprint=decision['source_fingerprint'])
        return decision

    def ingest(self, album, *, preview_error=None):
        with patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(sort, 'sort_inbox'), \
             patch.object(tag, 'group_staged_albums', return_value={('Delivery', 'Album'): album}), \
             patch.object(mixie, 'preview_album', side_effect=preview_error, return_value={}) as preview, \
             patch.object(mixie, 'test_album_plan', return_value=None) as processor, \
             patch.object(mixie.archive, 'update_original_downloads_archive',
                          side_effect=AssertionError('Unexpected original archive')), \
             patch.object(mixie.archive, 'create_verified_zip',
                          side_effect=AssertionError('Unexpected library ZIP')), \
             patch('builtins.input', return_value='0'), redirect_stdout(StringIO()) as output:
            mixie._main('1')
        return preview, processor, output.getvalue()

    def test_advanced_approval_is_persisted_as_reviewed_without_importing(self):
        _album, issue = self.album()
        decision = ui._build_user_interpretation(mixie, issue)

        def choose(working, fingerprint, save):
            working.update(interpretation=decision, source_fingerprint=fingerprint)
            save(working)
            return True

        with patch.object(review, 'choose_interpretation', side_effect=choose):
            self.assertTrue(mixie._choose_album_interpretation(issue, [issue]))
        self.assertEqual('reviewed', mixie.load_review_queue()[0].get('review_status'))
        self.assertEqual(decision, issue['interpretation'])
        self.assertFalse(list(self.library.rglob('*')))
        self.assertFalse((self.script / '.verified_imports.json').exists())

    def test_reopening_advanced_approval_and_postponing_demotes_to_draft(self):
        _album, issue = self.album()
        self.approve(issue)
        issue['review_status'] = 'reviewed'
        with patch('builtins.input', return_value='0'), redirect_stdout(StringIO()):
            self.assertFalse(mixie._choose_album_interpretation(issue, [issue]))
        saved = mixie.load_review_queue()[0]
        self.assertEqual('draft', saved['interpretation']['status'])
        self.assertFalse(saved['interpretation']['user_approved'])
        self.assertEqual('needs_review', saved.get('review_status'))

    def test_failed_advanced_checkpoint_does_not_leave_an_in_memory_approval(self):
        _album, issue = self.album()
        before = deepcopy(issue)
        decision = ui._build_user_interpretation(mixie, issue)

        def choose(working, fingerprint, save):
            working.update(interpretation=decision, source_fingerprint=fingerprint)
            save(working)

        with patch.object(review, 'choose_interpretation', side_effect=choose), \
             patch.object(mixie, 'save_review_queue', side_effect=OSError('Synthetic full disk')):
            with self.assertRaisesRegex(OSError, 'full disk'):
                mixie._choose_album_interpretation(issue, [issue])
        self.assertEqual(before, issue)

    def test_legacy_advanced_approval_waits_for_explicit_apply(self):
        album, issue = self.album()
        self.approve(issue)
        mixie.save_review_queue([issue])
        preview, processor, output = self.ingest(album)
        preview.assert_not_called()
        processor.assert_not_called()
        self.assertIn('Saved review awaiting explicit Apply', output)
        saved = mixie.load_review_queue()[0]
        self.assertEqual('reviewed', saved.get('review_status'))
        self.assertEqual(issue['interpretation'], saved['interpretation'])
        self.assertFalse((self.script / '.verified_imports.json').exists())

    def test_tag_edit_draft_is_rechecked_without_replaying_it_and_survives_failure(self):
        album, issue = self.album()
        ui.save_choice(mixie, issue, [issue], field='title', value='Draft title', relative=issue['tracks'][0])
        original = deepcopy(issue['tag_edits'])
        error = review.AlbumReviewRequired({'kind': 'identity_release', 'reason': 'Still uncertain'})
        preview, _processor, _output = self.ingest(album, preview_error=error)
        preview.assert_called_once()
        for field in ('tag_edits', 'interpretation', 'reviewed_decision', 'manual_fields', 'artwork_decision'):
            self.assertFalse(preview.call_args.kwargs.get(field))
        saved = mixie.load_review_queue()[0]
        self.assertEqual(original, saved['tag_edits'])
        self.assertEqual('needs_review', saved['review_status'])
        self.assertNotIn('interpretation', saved)

    def test_manual_date_draft_is_not_an_automatic_override(self):
        album, issue = self.album()
        ui.save_choice(mixie, issue, [issue], field='date', value='2019')
        preview, _processor, _output = self.ingest(album)
        preview.assert_called_once()
        self.assertFalse(preview.call_args.kwargs.get('manual_fields'))
        self.assertEqual({'date': '2019'}, mixie.load_review_queue()[0]['manual_fields'])

    def test_saved_metadata_and_artwork_are_not_replayed_by_ingest(self):
        album, issue = self.album()
        issue.update(source_fingerprint=mixie.review_fingerprint(issue),
            decisions=[{'field': 'album', 'value': 'Human title', 'status': 'pending_application'}],
            artwork_decision={'kind': 'review_artwork_file', 'status': 'pending_application',
                              'path': str(self.root / 'not-applied.png')})
        mixie.save_review_queue([issue])
        preview, _processor, _output = self.ingest(album)
        preview.assert_called_once()
        self.assertIsNone(preview.call_args.kwargs.get('reviewed_decision'))
        self.assertIsNone(preview.call_args.kwargs.get('artwork_decision'))
        self.assertEqual(issue['artwork_decision'], mixie.load_review_queue()[0]['artwork_decision'])

    def test_stale_approval_cannot_skip_a_fresh_independent_recheck(self):
        album, issue = self.album()
        self.approve(issue)
        issue['review_status'] = 'reviewed'
        mixie.save_review_queue([issue])
        album['tracks'][0]['path'].write_bytes(b'synthetic changed source')
        preview, _processor, _output = self.ingest(album)
        preview.assert_called_once()
        self.assertIsNone(preview.call_args.kwargs.get('interpretation'))
        saved = mixie.load_review_queue()[0]
        self.assertNotIn('interpretation', saved)
        self.assertNotEqual('reviewed', saved.get('review_status'))

    def test_new_diagnostics_do_not_discard_earlier_review_evidence(self):
        _album, issue = self.album()
        issue['source_fingerprint'] = mixie.review_fingerprint(issue)
        issue['review_evidence_history'] = [{'details': {'kind': 'artwork_conflict', 'candidates': []},
                                            'reason': 'Earlier cover ambiguity'}]
        mixie.save_review_queue([issue])
        fresh = {key: deepcopy(value) for key, value in issue.items()
                 if key not in ('source_fingerprint', 'review_evidence_history')}
        fresh['details'] = {'kind': 'provider_unavailable'}
        reconciled = mixie.reconcile_review_queue([fresh])
        history = reconciled[0].get('review_evidence_history', [])
        self.assertIn(issue['review_evidence_history'][0], history)
        self.assertTrue(any(snapshot.get('details') == issue['details']
                            and snapshot.get('source_fingerprint') == issue['source_fingerprint']
                            for snapshot in history))
        self.assertEqual(fresh['details'], reconciled[0]['details'])

    def test_legacy_reviewed_label_cannot_make_a_draft_block_rechecks_after_restart(self):
        album, issue = self.album()
        decision = self.approve(issue)
        decision.update(status='draft', user_approved=False)
        issue['review_status'] = 'reviewed'
        mixie.save_review_queue([issue])
        for _attempt in range(2):
            preview, _processor, _output = self.ingest(album)
            preview.assert_called_once()
            self.assertIsNone(preview.call_args.kwargs.get('interpretation'))
            saved = mixie.load_review_queue()[0]
            self.assertEqual('needs_review', saved['review_status'])
            self.assertEqual(decision, saved['interpretation'])

    def test_primary_approval_save_failure_restores_previous_choices_and_status(self):
        _album, issue = self.album()
        ui.save_choice(mixie, issue, [issue], field='title', value='Saved draft', relative=issue['tracks'][0])
        issue['apply_problem'] = 'Earlier apply failure'
        before = deepcopy(issue)
        stored = (self.script / '.needs_review.json').read_bytes()
        with patch('builtins.input', return_value='APPROVE'), \
             patch.object(mixie, 'save_review_queue', side_effect=OSError('Synthetic full disk')), \
             redirect_stdout(StringIO()):
            self.assertFalse(ui._approve_review(mixie, issue, [issue]))
        self.assertEqual(before, issue)
        self.assertEqual(stored, (self.script / '.needs_review.json').read_bytes())
        self.assertNotIn('interpretation', issue)

    def test_explicit_offline_total_imports_but_waits_for_archive_proof(self):
        from support import make_cover
        album, issue = self.album()
        ui.save_choice(mixie, issue, [issue], field='track_total', value='12')
        before = {row['path']: row['path'].read_bytes() for row in album['tracks']}
        with patch('builtins.input', return_value='APPROVE'), redirect_stdout(StringIO()):
            self.assertTrue(ui._approve_review(mixie, issue, [issue]))
        pending = mixie.load_review_queue()
        issue = pending[0]
        with patch.object(tag, 'group_staged_albums', return_value={('Delivery', 'Album'): album}), \
             patch.object(mixie, 'select_album_art', return_value=make_cover()), \
             patch.object(mixie.archive, 'update_original_downloads_archive',
                          side_effect=AssertionError('Import must not require archives')), \
             patch.object(mixie.archive, 'create_verified_zip',
                          side_effect=AssertionError('Import must not require ZIP')), \
             patch.object(mixie, 'auto_quarantine_archived_completed',
                          side_effect=AssertionError('Import cannot authorize cleanup')), \
             redirect_stdout(StringIO()):
            self.assertFalse(mixie.finalize_reviewed_album(issue, pending))
        self.assertEqual([issue], pending)
        self.assertEqual('archive_pending', issue['status'])
        saved = mixie.load_review_queue()
        self.assertEqual(1, len(saved))
        self.assertEqual('archive_pending', saved[0]['status'])
        self.assertEqual(issue['tracks'], saved[0]['tracks'])
        self.assertTrue(mixie.is_library_import_verified(issue))
        entry = next(iter(mixie._verified_imports().values()))
        self.assertEqual('user_approved', entry['evidence']['provenance'])
        self.assertFalse(entry['evidence']['interpretation']['independently_verified'])
        self.assertTrue(entry['evidence']['interpretation']['partial'])
        for path, original in before.items():
            self.assertEqual(original, path.read_bytes())
            output = entry['input_map'][path.relative_to(self.stage).as_posix()]['destination']
            self.assertEqual({12}, tag.get_track_totals(tag.read_track_metadata(output)))
        self.assertFalse(mixie.load_completed_reviews())
        preview, processor, output = self.ingest(album)
        preview.assert_not_called()
        processor.assert_not_called()
        self.assertIn('Previously imported; identification skipped', output)
        with redirect_stdout(StringIO()):
            pending = mixie.reconcile_review_queue([issue])
        self.assertEqual(1, len(pending))
        self.assertEqual('archive_pending', pending[0]['status'])
        self.assertEqual(issue['tracks'], pending[0]['tracks'])

    def test_offline_edits_still_cannot_contradict_explicitly_approved_totals_or_positions(self):
        from support import make_cover
        album, issue = self.album()
        decision = self.approve(issue)
        for edits in ({'album': {'track_total': '12'}},
                      {'album': {'disc_total': '2'}},
                      {'tracks': {issue['tracks'][0]: {'track_number': '2'}}},
                      {'tracks': {issue['tracks'][0]: {'disc_number': '2'}}}):
            with self.subTest(edits=edits), \
                 patch.object(mixie, 'select_album_art', return_value=make_cover()), redirect_stdout(StringIO()):
                with self.assertRaises(ValueError):
                    mixie.preview_album(album, None, interpretation=decision, tag_edits=edits)

    def test_legacy_archive_pending_without_input_receipt_stays_pending(self):
        _album, issue = self.album()
        issue.update(status='archive_pending', details={'kind': 'completion'},
                     reason='Library import verified; archive pending: changed PVRIS')
        mixie.save_review_queue([issue])
        before = (self.script / '.needs_review.json').read_bytes()
        self.assertEqual([issue], mixie.reconcile_review_queue([]))
        self.assertEqual(before, (self.script / '.needs_review.json').read_bytes())
        self.assertFalse((self.script / '.verified_imports.json').exists())


class ImportReceiptBoundaryTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        (self.script / '.mixie-import-first.json').write_text(json.dumps({
            'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')
        source = self.track_path('Delivery/01.flac', b'synthetic original bytes')
        self.song = self.library / 'Artist' / 'Album' / '01.flac'
        self.song.parent.mkdir(parents=True)
        self.song.write_bytes(b'synthetic corrected output')
        cover = self.song.parent / 'cover.png'
        cover.write_bytes(b'synthetic artwork')
        self.issue = {'import_name': 'Delivery', 'album': 'Album', 'tracks': ['Delivery/01.flac']}
        hashes = {str(path): sort.file_checksum(path).hex() for path in (self.song, cover)}
        receipt = mixie.CommitReceipt(hashes,
            input_map={'Delivery/01.flac': {'source_sha256': sort.file_checksum(source).hex(),
                'destination': str(self.song), 'destination_sha256': hashes[str(self.song)]}},
            source_manifest=mixie.completion_source_manifest(self.issue),
            asset_outputs={str(cover): hashes[str(cover)]},
            evidence={'provenance': 'independently_verified'})
        self.identity = mixie.record_library_only_import(self.issue, receipt)

    def test_parent_symlink_inside_library_does_not_validate_receipt(self):
        original = self.song.parent
        held = original.with_name('Retained')
        original.rename(held)
        original.symlink_to(held, target_is_directory=True)
        self.assertFalse(mixie.is_library_import_verified(self.issue))
        self.assertEqual(b'synthetic corrected output', (held / '01.flac').read_bytes())

    def test_incomplete_or_wrong_source_mapping_never_validates(self):
        for change in ('missing', 'wrong_source', 'unaccounted_output'):
            with self.subTest(change=change):
                entries = deepcopy(mixie._verified_imports())
                entry = entries[self.identity]
                if change == 'missing':
                    entry['input_map'].clear()
                elif change == 'wrong_source':
                    entry['input_map']['Delivery/01.flac']['source_sha256'] = '0' * 64
                else:
                    entry['receipt'][str(self.library / 'unrelated.flac')] = '0' * 64
                self.assertFalse(mixie.is_library_import_verified(self.issue, entries))

    def test_optional_zip_failure_cannot_invalidate_success_or_retire_sources(self):
        before = (self.script / '.verified_imports.json').read_bytes()
        with patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(mixie.archive, 'create_verified_zip', side_effect=RuntimeError('Synthetic ZIP failure')), \
             patch.object(mixie, 'auto_purge_verified_quarantines',
                          side_effect=AssertionError('Optional ZIP cannot authorize purge')), \
             redirect_stdout(StringIO()) as output:
            mixie._main('5')
        self.assertIn('Optional library ZIP deferred', output.getvalue())
        self.assertEqual(before, (self.script / '.verified_imports.json').read_bytes())
        self.assertTrue(mixie.is_library_import_verified(self.issue))
        self.assertTrue((self.stage / 'Delivery/01.flac').is_file())
        self.assertFalse(mixie.load_completed_reviews())

    def test_import_only_proof_cannot_bypass_interrupted_cleanup_preflight(self):
        recovery = self.backup / '.cleanup-quarantine' / '.mixie-inbox-pending'
        recovery.mkdir(parents=True)
        with patch.object(mixie, 'auto_purge_verified_quarantines', return_value=mixie.CleanupResults()), \
             patch.object(sort, 'sort_inbox') as stage, \
             patch.object(mixie, 'preview_album') as preview, \
             patch.object(mixie.sys, 'argv', ['mixie.py']), redirect_stdout(StringIO()) as output:
            mixie._main('1')
        stage.assert_not_called()
        preview.assert_not_called()
        self.assertIn('Unfinished cleanup recovery remains', output.getvalue())
        self.assertTrue(recovery.is_dir())
        self.assertTrue(mixie.is_library_import_verified(self.issue))
        self.assertFalse(mixie.is_review_complete(self.issue, mixie.load_completed_reviews()))
