"""Source-bound ingest failures and receipt-only recovery in isolated fixtures."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import shutil

import audio
import lookup
import mixie
import mixie_finalizer as final
import mixie_review_ui as ui
import review
import sort
import tag
import test_finalization_integration_stabilization as integration_fixtures
import test_selective_finalization as historical_fixtures
from support import WorkspaceCase, metadata_record


_ABSENT = object()


class IngestFailureEvidenceTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        (self.script / '.mixie-import-first.json').write_text(json.dumps({
            'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')
        self.rows, self.payload = {}, {}
        for number in (1, 2):
            relative = f'Delivery/Album/{number:02d}.flac'
            data = f'untouched synthetic source {number}'.encode()
            path = self.track_path(relative, data)
            self.payload[path] = data
            self.rows[relative] = metadata_record(path, title=(f'Source song {number}',),
                track_number=(str(number),), track_total=('2',))
        self.album = {'album': 'Album', 'tracks': list(self.rows.values())}
        self.issue = mixie._album_issue(self.album)
        self.fingerprint = mixie.review_fingerprint(self.issue)
        self.positions = {relative: (1, number)
                          for number, relative in enumerate(self.rows, 1)}
        for module, name in ((audio, 'audit_tracks'), (lookup, '_musicbrainz_call')):
            self.stack.enter_context(patch.object(module, name,
                side_effect=AssertionError('Unexpected provider/audio work')))

    def failure(self, exception, pin=_ABSENT):
        details = {'kind': 'identity_release', 'reason': 'Synthetic provider snapshot needs review',
            'tracks': [{'path': relative, 'status': 'preliminary_match',
                        'details': {'id': f'recording-{number}', 'title': f'Provider song {number}',
                                    'artist-credit-phrase': 'Artist'}}
                       for number, relative in enumerate(self.rows, 1)],
            'releases': [{'release_id': 'cached-release', 'status': 'complete',
                'tracklist_complete': True, 'errors': [], 'reasons': [], 'missing': [], 'review': [],
                'matched': {json.dumps(list(position)): relative
                            for relative, position in self.positions.items()},
                'tracklist': [{'disc': '1', 'number': str(number),
                               'recording_id': f'recording-{number}', 'title': f'Provider song {number}'}
                              for number in (1, 2)]}]}
        if pin is not _ABSENT:
            details['source_fingerprint'] = pin
        return exception(details)

    def ingest_failure(self, error, *, change_source=None):
        for path, data in self.payload.items():
            path.write_bytes(data)
        mixie.save_review_queue([])
        raised = []

        def fail(*args, **kwargs):
            self.assertEqual(self.fingerprint, mixie.review_fingerprint(self.issue))
            if change_source is not None:
                change_source(error)
            raised.append(deepcopy(error.details))
            raise error

        with mixie.ingest_session(), \
             patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(sort, 'sort_inbox') as stage, \
             patch.object(tag, 'group_staged_albums', return_value={('Delivery', 'Album'): self.album}), \
             patch.object(mixie, 'get_acoustid_client_key', return_value='DISPOSABLE-OFFLINE-TEST-KEY'), \
             patch.object(mixie, 'preview_album', side_effect=fail) as preview, \
             patch.object(mixie, 'test_album_plan', side_effect=AssertionError('Failed scan cannot import')) as commit, \
             patch('builtins.input', return_value='n'), redirect_stdout(StringIO()):
            mixie._main('1')
        stage.assert_called_once_with()
        preview.assert_called_once()
        commit.assert_not_called()
        self.assertEqual([error.details], raised, 'Failure capture must not mutate the exception snapshot')
        pending = mixie.load_review_queue()
        self.assertEqual(1, len(pending))
        saved = pending[0]
        for field in ('tracks', 'releases', 'verified_release_numbering'):
            self.assertEqual(raised[0].get(field), saved['details'].get(field))
        self.assertNotIn('interpretation', saved)
        self.assertEqual({}, sort.file_manifest(self.library))
        self.assertFalse((self.script / '.verified_imports.json').exists())
        return saved

    def assert_nonauthorizing(self, issue):
        self.assertFalse(ui._fresh_release_evidence(issue))
        self.assertIsNone(ui._release_disc_structure(issue, self.rows))

    def test_stable_source_preserves_incoming_old_hash_without_rebinding(self):
        old_hash = '0' * 64
        self.assertNotEqual(old_hash, self.fingerprint)
        for exception in (review.AlbumReviewRequired, mixie.ProviderUnavailable):
            with self.subTest(exception=exception.__name__):
                saved = self.ingest_failure(self.failure(exception, old_hash))
                self.assertEqual(old_hash, saved['details']['source_fingerprint'])
                self.assertNotEqual(self.fingerprint, saved.get('source_fingerprint'))
                self.assert_nonauthorizing(saved)

    def test_stable_source_preserves_explicit_none_as_nonauthorizing(self):
        for exception in (review.AlbumReviewRequired, mixie.ProviderUnavailable):
            with self.subTest(exception=exception.__name__):
                saved = self.ingest_failure(self.failure(exception, None))
                self.assertIn('source_fingerprint', saved['details'])
                self.assertIsNone(saved['details']['source_fingerprint'])
                self.assert_nonauthorizing(saved)

    def test_stable_source_binds_a_truly_unbound_fresh_failure(self):
        for exception in (review.AlbumReviewRequired, mixie.ProviderUnavailable):
            with self.subTest(exception=exception.__name__):
                error = self.failure(exception)
                self.assertNotIn('source_fingerprint', error.details)
                saved = self.ingest_failure(error)
                self.assertEqual(self.fingerprint, saved['source_fingerprint'])
                self.assertEqual(self.fingerprint, saved['details']['source_fingerprint'])
                self.assertTrue(ui._fresh_release_evidence(saved))
                self.assertEqual((self.positions, {1: 2}), ui._release_disc_structure(saved, self.rows))

    def test_source_change_during_provider_failure_keeps_snapshot_nonauthorizing(self):
        for claims_current_source in (False, True):
            with self.subTest(claims_current_source=claims_current_source):
                error = self.failure(mixie.ProviderUnavailable)
                if claims_current_source:
                    error.details['verified_release_numbering'] = {
                        'source_fingerprint': self.fingerprint, 'release_id': 'cached-release',
                        'positions': {relative: list(position) for relative, position in self.positions.items()},
                        'track_totals': {'1': 2}}

                def change(incoming):
                    next(iter(self.payload)).write_bytes(b'changed synthetic source during provider failure')
                    if claims_current_source:
                        # Model an upstream handler incorrectly repinning the old snapshot.
                        current = mixie.review_fingerprint(self.issue)
                        incoming.details['source_fingerprint'] = current
                        incoming.details['verified_release_numbering']['source_fingerprint'] = current

                saved = self.ingest_failure(error, change_source=change)
                current = mixie.review_fingerprint(self.issue)
                self.assertNotEqual(self.fingerprint, current)
                self.assertNotEqual(current, saved.get('source_fingerprint'))
                self.assertNotEqual(current, saved['details'].get('source_fingerprint'))
                self.assert_nonauthorizing(saved)


class ReceiptRecoveryVisibilityTests(WorkspaceCase):
    # Reuse fixture setup only, not the other case's regression methods.
    delivery = integration_fixtures.FinalizationIntegrationTests.delivery
    offline_recovery = integration_fixtures.FinalizationIntegrationTests.offline_recovery

    def setUp(self):
        super().setUp()
        self.delivery()
        self.assertEqual([], mixie.load_review_queue())
        self.assertTrue(mixie.is_library_import_verified(self.issue))
        self.assertFalse(final.is_album_finalized(mixie, self.issue))

    def retained_state(self):
        return ([sort.file_manifest(root) for root in
                 (self.inbox, self.stage, self.library, self.backup)],
                sort.staging_provenance(self.name, verify_payload=False),
                (self.script / '.verified_imports.json').read_bytes())

    def assert_recovery_separate_from_metadata(self):
        # A stale receipt belongs to the immutable import/finalization ledgers,
        # never to the user's list of albums requiring tag decisions.
        self.assertEqual([], mixie.load_review_queue())
        with patch.object(ui, 'input', return_value='0'), redirect_stdout(StringIO()) as output:
            ui.review_manager_menu(mixie, [])
        self.assertIn('1. Albums Needing Review (0)', output.getvalue())
        self.assertIn('4. Finish / Retry Required Archives (0 pending)', output.getvalue())

    def reconcile_missing_queue(self):
        self.assertEqual([], mixie.load_review_queue())
        before = self.retained_state()
        with self.offline_recovery():
            pending = mixie.reconcile_review_queue([])
        self.assertEqual([], pending)
        self.assert_recovery_separate_from_metadata()
        self.assertEqual(before, self.retained_state())

    def test_bare_receipt_with_changed_output_is_not_a_manual_tag_task(self):
        destination = Path(next(iter(self.receipt.input_map.values()))['destination'])
        destination.write_bytes(b'changed synthetic library output')
        self.assertFalse(mixie.is_library_import_verified(self.issue))
        self.reconcile_missing_queue()

    def test_bare_receipt_with_changed_source_is_not_a_manual_tag_task(self):
        (self.stage / self.issue['tracks'][0]).write_bytes(b'changed synthetic staged source')
        self.assertFalse(mixie.is_library_import_verified(self.issue))
        self.reconcile_missing_queue()

    def test_bare_receipt_with_missing_stage_is_not_a_manual_tag_task(self):
        shutil.rmtree(self.staged)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(mixie.is_library_import_verified(self.issue))
        self.reconcile_missing_queue()
        self.assertFalse(self.staged.exists())

    def test_invalid_available_receipt_reports_archive_failure_without_manual_tag_task(self):
        destination = Path(next(iter(self.receipt.input_map.values()))['destination'])
        destination.unlink()
        before = self.retained_state()
        with self.offline_recovery(), mixie.ingest_session():
            with self.assertRaisesRegex(RuntimeError, r'(?i)(receipt|destination|output|library|changed|invalid)'):
                final._verified_entry(mixie, self.issue, mixie._verified_imports())
            count, problems = mixie.finalize_verified_imports()
        self.assertEqual(0, count)
        self.assertTrue(problems, 'Invalid receipt must remain visible in archive diagnostics')
        self.assertTrue(any(str(problem).strip() for problem in problems))
        self.assert_recovery_separate_from_metadata()
        self.assertEqual(before, self.retained_state())


class HistoricalCompletionRecoveryTests(WorkspaceCase):
    delivery = historical_fixtures.SelectiveFinalizationTests.delivery
    legacy_completed = historical_fixtures.SelectiveFinalizationTests.legacy_completed
    repin_legacy = historical_fixtures.SelectiveFinalizationTests.repin_legacy
    offline_recovery = integration_fixtures.FinalizationIntegrationTests.offline_recovery

    def test_absent_source_with_validated_historical_completion_does_not_resurrect(self):
        self.legacy_completed()
        issue = self.issues['First']
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual({}, final._state()['finalized'])
        self.assertTrue(final.is_album_finalized(mixie, issue))
        self.assertEqual([], mixie.load_review_queue())
        before = (sort.file_manifest(self.backup), sort.file_manifest(self.library),
                  (self.script / '.verified_imports.json').read_bytes())
        with self.offline_recovery(), mixie.ingest_session():
            self.assertEqual([], mixie.reconcile_review_queue([]))
            self.assertEqual([], mixie.reconcile_review_queue([deepcopy(issue)]))
            self.assertEqual((0, []), mixie.finalize_verified_imports())
        self.assertEqual([], mixie.load_review_queue())
        self.assertTrue(final.is_album_finalized(mixie, issue))
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual(before, (sort.file_manifest(self.backup), sort.file_manifest(self.library),
                                 (self.script / '.verified_imports.json').read_bytes()))
