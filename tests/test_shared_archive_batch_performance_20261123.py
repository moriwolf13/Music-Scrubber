"""Shared-original archive batching, tamper detection and fresh deletion proofs.

All source material is disposable fixture data; none of these tests inspect music.
"""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json

import mixie
import mixie_finalizer as final
import mixie_shared_originals as store
import test_selective_finalization as selective
from support import WorkspaceCase


class SharedArchiveBatchProofTests(WorkspaceCase):
    delivery = selective.SelectiveFinalizationTests.delivery
    finish = selective.SelectiveFinalizationTests.finish
    assert_retained = selective.SelectiveFinalizationTests.assert_retained

    def _get_pinned(self):
        record = next(iter(final._state()['finalized'].values()))
        source, = record['sources']
        return source, record['originals']['deliveries'][source['name']]['archive']

    def test_two_albums_one_shared_delivery_ensure_archives_only_once(self):
        self.delivery(verified=('First', 'Second'))
        with patch.object(store, 'ensure_archive', wraps=store.ensure_archive) as ensure, \
             patch.object(store, 'validate_archive', wraps=store.validate_archive) as verify:
            self.assertEqual((2, []), self.finish(cleanup=False))
        ensure.assert_called_once()
        # The publish operation itself verifies the full shared archive;
        # the second album may reuse that proof while hashes/journal stay fixed.
        self.assertLessEqual(verify.call_count, 1)
        self.assert_retained()
        state = final._state()
        self.assertEqual(2, len(state['finalized']))
        self.assertEqual(1, len(list((self.backup / 'Originals').rglob('*.zip'))))
        # Independently re-verify persisted bytes with a fresh proof set.
        for row in state['finalized'].values():
            final._validate_archives(row)

    def test_same_delivery_proof_is_reused_but_live_files_are_rechecked(self):
        self.delivery(verified=('First', 'Second'))
        original = final._check_retained_source
        calls = []
        def observe(source):
            calls.append(source['name'])
            return original(source)
        with patch.object(final, '_check_retained_source', side_effect=observe):
            self.assertEqual((2, []), self.finish(cleanup=False))
        self.assertGreaterEqual(len(calls), 2)
        self.assert_retained()

    def test_content_change_invalidates_verified_session(self):
        self.delivery()
        self.assertEqual(1, self.finish(cleanup=False)[0])
        source, record = self._get_pinned()
        session = final._SharedOriginalBatchProof()
        session.validate(source, record)
        path = Path(record['path'])
        path.write_bytes(b'corrupt shared archive')
        with self.assertRaises(RuntimeError):
            session.validate(source, record)

    def test_journal_change_invalidates_verified_session(self):
        self.delivery()
        self.assertEqual(1, self.finish(cleanup=False)[0])
        source, record = self._get_pinned()
        session = final._SharedOriginalBatchProof()
        session.validate(source, record)
        shared, identity, journal, partial = store._layout(self.backup, store.delivery_descriptor(source))
        row = json.loads(journal.read_text())
        row['state'] = 'sealed'
        journal.write_text(json.dumps(row))
        with self.assertRaises(RuntimeError):
            session.validate(source, record)

    def test_destructive_cleanup_uses_fresh_full_checks(self):
        self.delivery(verified=('First', 'Second'))
        with patch.object(store, 'validate_archive', wraps=store.validate_archive) as verify:
            self.assertEqual((2, []), self.finish(cleanup=True))
        # Post-publication cleanup validates before preparing, when resuming,
        # and again before purging; none use the ingest-time session.
        self.assertGreaterEqual(verify.call_count, 3)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.staged.exists())
        state = final._state()
        self.assertIsNone(state['active'])
        self.assertEqual(1, len(state['completed']))

    def test_intervening_archive_corruption_prevents_second_published_album(self):
        self.delivery(verified=('First', 'Second'))
        archive = final._archive_album
        counter = [0]
        def wrapped(*args, **kwargs):
            row, created = archive(*args, **kwargs)
            counter[0] += 1
            if counter[0] == 1:
                blob = Path(next(iter(row['originals']['deliveries'].values()))['archive']['path'])
                blob.write_bytes(b'corrupted after first album publication')
            return row, created
        with patch.object(final, '_archive_album', side_effect=wrapped):
            count, errors = self.finish(cleanup=False)
        self.assertEqual(1, count)
        self.assertTrue(errors)
        self.assertEqual(1, len(final._state()['finalized']))
        self.assert_retained()

    def test_archive_progress_reports_each_delivery_and_album(self):
        self.delivery(verified=('First', 'Second'))
        messages = []
        self.assertEqual((2, []), final.finish_verified_albums(
            mixie, self.albums, allow_source_cleanup=False, progress=messages.append))
        self.assertTrue(any('Delivery 1/1' in line for line in messages))
        self.assertTrue(any('Verifying album 1/2' in line for line in messages))
        self.assertTrue(any('Verifying album 2/2' in line for line in messages))
