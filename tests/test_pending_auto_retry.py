"""Pending-album-only retry safety; disposable data and mocked providers only."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from support import WorkspaceCase, metadata_record
import lookup
import mixie
import review
import tag

NAMES = (
    'Back From the Dead', 'The Strange Case of...', 'Witch',
    'Americana (ESCA 7393)', 'can u see me in the dark?',
)


class PendingAutoRetryTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.issues, self.grouped = [], {}
        for i, label in enumerate(NAMES):
            name = f'Import-{i}'
            filename = f'{name}/Album/01.flac'
            path = self.track_path(filename)
            issue = {'import_name': name, 'tracks': [filename], 'album': label,
                     'status': 'needs_review', 'reason': 'Historical synthetic review',
                     'details': {'kind': 'identity_release'}}
            self.issues.append(issue)
            self.grouped[(name, label)] = {'album': label, 'folder': path.parent,
                'tracks': [metadata_record(path)]}
        self.preview = self.stack.enter_context(patch.object(
            mixie, 'preview_album', return_value={'provenance': 'independently_verified'}))
        self.test_plan = self.stack.enter_context(patch.object(
            mixie, 'test_album_plan', return_value={'verified-library-file': 'hash'}))
        self.receipts = self.stack.enter_context(patch.object(mixie, 'record_library_only_import'))
        self.finalizer = self.stack.enter_context(patch.object(mixie, 'finalize_verified_imports'))
        self.saved = self.stack.enter_context(patch.object(mixie, 'save_review_queue'))
        self.stack.enter_context(patch.object(mixie, 'finish_prior_cleanup_before_ingest', return_value=[]))
        self.stack.enter_context(patch.object(mixie, '_import_first_enabled', return_value=True))
        self.stack.enter_context(patch.object(mixie, 'is_library_import_verified', return_value=False))
        self.stack.enter_context(patch.object(mixie, 'get_acoustid_client_key',
                                              return_value='DISPOSABLE-OFFLINE-TEST-KEY'))
        self.stack.enter_context(patch.object(mixie, 'review_fingerprint', return_value='current-source-pin'))
        self.stack.enter_context(patch.object(mixie, 'load_review_queue', side_effect=lambda: deepcopy(self.issues)))
        self.stack.enter_context(patch.object(tag, 'group_staged_albums', return_value=self.grouped))
        self.stack.enter_context(patch.object(mixie, 'reconcile_review_queue',
                                              side_effect=lambda fresh: fresh))

    def run_retry(self, *, preview=False):
        with redirect_stdout(StringIO()) as out:
            result = mixie.retry_pending_albums(preview_only=preview)
        return result, out.getvalue()

    def test_five_historical_issues_use_independent_auto_path_without_restaging(self):
        self.issues[0]['interpretation'] = {'status': 'draft', 'user_approved': False,
                                            'source_fingerprint': 'current-source-pin'}
        self.issues[0]['source_fingerprint'] = 'current-source-pin'
        result, output = self.run_retry()
        self.assertEqual({'attempted': 5, 'verified': 5, 'deferred': 0, 'protected': 0}, result)
        self.assertEqual(5, self.preview.call_count)
        self.assertEqual(5, self.test_plan.call_count)
        self.assertEqual(5, self.receipts.call_count)
        self.assertEqual(1, self.finalizer.call_count)
        self.assertFalse(self.saved.called)
        self.assertTrue(all(call.kwargs == {'auto_mode': True} for call in self.preview.call_args_list))
        self.assertTrue(all(call.kwargs['commit'] and call.kwargs['auto']
                            and call.kwargs['preserve_staged_backup'] is False
                            for call in self.test_plan.call_args_list))
        self.assertNotIn('source cleanup authorized', output.casefold())

    def test_preview_is_read_only_and_does_not_need_commit_receipt(self):
        self.test_plan.return_value = None
        result, output = self.run_retry(preview=True)
        self.assertEqual(5, result['verified'])
        self.assertTrue(all(not call.kwargs['commit'] and call.kwargs['auto']
                            and call.kwargs['preserve_staged_backup'] is False
                            for call in self.test_plan.call_args_list))
        self.receipts.assert_not_called()
        self.finalizer.assert_not_called()
        self.saved.assert_not_called()
        self.assertIn('no files imported', output)

    def test_saved_human_decision_is_not_automatically_applied_or_erased(self):
        self.issues[1]['manual_fields'] = {'genre': 'Rock'}
        self.issues[2]['interpretation'] = {'status': 'pending_application', 'user_approved': True}
        self.issues[3]['review_status'] = 'reviewed'
        result, output = self.run_retry()
        self.assertEqual(3, result['protected'])
        self.assertEqual(2, result['verified'])
        self.assertEqual(2, self.preview.call_count)
        self.saved.assert_not_called()
        self.assertIn('Saved user review choices', output)

    def test_stale_pinned_source_is_not_scanned_or_imported(self):
        self.issues[0]['source_fingerprint'] = 'old-source-pin'
        result, output = self.run_retry()
        self.assertEqual(1, result['deferred'])
        self.assertEqual(4, result['attempted'])
        self.assertIn('differs from saved review', output)
        self.assertEqual(4, self.test_plan.call_count)

    def test_missing_staged_album_is_preserved(self):
        del self.grouped[('Import-4', NAMES[4])]
        result, output = self.run_retry()
        self.assertEqual(4, result['attempted'])
        self.assertEqual(1, result['deferred'])
        self.assertIn('missing or ambiguous', output)

    def test_provider_outage_retains_diagnostic_and_never_invents_approval(self):
        self.issues[:] = self.issues[:1]
        self.preview.side_effect = mixie.ProviderUnavailable({'reason': 'Synthetic 503',
            'provider_errors': [{'retryable': True, 'http_status': 503}]})
        result, output = self.run_retry()
        self.assertEqual(1, result['deferred'])
        self.assertEqual(0, result['verified'])
        self.receipts.assert_not_called()
        queued = self.saved.call_args.args[0]
        self.assertEqual('provider_unavailable', queued[0]['details']['kind'])
        self.assertEqual('current-source-pin', queued[0]['source_fingerprint'])
        self.assertNotIn('user_approved', queued[0])
        self.assertIn('Still needs review', output)

    def test_source_change_mid_scan_refuses_commit(self):
        self.issues[:] = self.issues[:1]
        with patch.object(mixie, 'review_fingerprint', side_effect=['pin-1', 'pin-2', 'pin-2']):
            result, _ = self.run_retry()
        self.assertEqual(1, result['deferred'])
        self.test_plan.assert_not_called()
        self.assertEqual(1, self.saved.call_count)

    def test_catalog_lookup_is_discovery_only_and_rejects_unrelated_rows(self):
        mock_search = Mock(return_value={'release-list': [
            {'id': 'matching', 'title': 'Americana', 'artist-credit-phrase': 'The Offspring'},
            {'id': 'other-band', 'title': 'Americana', 'artist-credit-phrase': 'Somebody Else'},
            {'id': 'wrong-title', 'title': 'Another Album', 'artist-credit-phrase': 'The Offspring'},
            {'id': 'matching', 'title': 'Americana', 'artist-credit-phrase': 'The Offspring'},
        ]})
        with patch.object(lookup, '_musicbrainz_call', mock_search):
            result = lookup.find_catalog_number_releases('The Offspring', 'Americana', 'ESCA 7393')
        self.assertEqual(['matching'], result)
        self.assertEqual('ESCA 7393', mock_search.call_args.kwargs['catno'])
        self.assertEqual(25, mock_search.call_args.kwargs['limit'])

    def test_malformed_catalog_number_rejected_without_provider(self):
        with patch.object(lookup, '_musicbrainz_call', side_effect=AssertionError('No provider')):
            self.assertEqual([], lookup.find_catalog_number_releases('Artist','Album','../../passwd'))

    def test_cli_cleanup_is_opt_in_and_dry_check_remains_read_only(self):
        with patch.object(mixie, 'retry_pending_albums', return_value={}) as retry, \
             patch.object(mixie.sys, 'argv', ['mixie.py', '--retry-pending']), \
             redirect_stdout(StringIO()):
            mixie._main()
        retry.assert_called_once_with(preview_only=False, allow_source_cleanup=False)
        with patch.object(mixie, 'retry_pending_albums', return_value={}) as retry, \
             patch.object(mixie.sys, 'argv', ['mixie.py', '--retry-pending']):
            mixie._main(allow_source_cleanup=True)
        retry.assert_called_once_with(preview_only=False, allow_source_cleanup=True)
        with patch.object(mixie, 'retry_pending_albums', side_effect=AssertionError('must not run')), \
             patch.object(mixie.sys, 'argv', ['mixie.py', '--check-pending']):
            with self.assertRaisesRegex(ValueError, 'dry check cannot authorize source cleanup'):
                mixie._main(allow_source_cleanup=True)

    def test_opt_in_cleanup_reaches_verified_finalizer_after_imports(self):
        with redirect_stdout(StringIO()):
            result = mixie.retry_pending_albums(allow_source_cleanup=True)
        self.assertEqual(5, result['verified'])
        self.finalizer.assert_called_once_with(allow_source_cleanup=True)

    def test_error_diagnostic_checkpoint_does_not_rehash_completed_queue(self):
        self.issues[:] = self.issues[:1]
        self.issues[0]['review_evidence_history'] = [{'older': 'retained'}]
        self.preview.side_effect = mixie.ProviderUnavailable({'reason': 'Synthetic 503',
            'provider_errors': [{'retryable': True, 'http_status': 503}]})
        result, _output = self.run_retry()
        self.assertEqual(1, result['deferred'])
        self.assertFalse(mixie.reconcile_review_queue.called)
        checkpoint = self.saved.call_args.args[0][0]
        self.assertEqual('provider_unavailable', checkpoint['details']['kind'])
        self.assertEqual({'older': 'retained'}, checkpoint['review_evidence_history'][0])
        self.assertEqual({'kind': 'identity_release'},
                         checkpoint['review_evidence_history'][1]['details'])

    def test_diagnostic_checkpoint_refuses_new_human_approval(self):
        refresher = deepcopy(self.issues[0])
        refresher['details'] = {'kind': 'provider_unavailable'}
        self.issues[0]['manual_fields'] = {'genre': 'Rock'}
        with self.assertRaisesRegex(RuntimeError, 'Saved user review choices changed'):
            mixie._save_pending_retry_diagnostic(refresher)
        self.saved.assert_not_called()
