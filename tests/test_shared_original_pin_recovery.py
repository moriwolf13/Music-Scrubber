"""Cross-album published pins; disposable offline fixtures only."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import mixie
import mixie_finalizer as final
import mixie_shared_originals as store
import sort
import test_selective_finalization as selective
from support import WorkspaceCase


class SharedOriginalPinRecoveryTests(WorkspaceCase):
    # Alias helpers, not the TestCase: importing/inheriting it duplicates its suite.
    delivery = selective.SelectiveFinalizationTests.delivery
    finish = selective.SelectiveFinalizationTests.finish
    shared_journal_path = selective.SelectiveFinalizationTests.shared_journal_path

    def _publish_first(self, *, zipped=False, album_names=('First', 'Second')):
        self.delivery(zipped=zipped, album_names=album_names)
        self.assertEqual(1, self.finish(cleanup=False)[0])
        self.first_row = final._state()['finalized'][self.identities['First']]
        self.pinned_source, = self.first_row['sources']
        self.record = self.first_row['originals']['deliveries'][self.name]['archive']
        self.blob = Path(self.record['path'])
        self.journal = self.shared_journal_path(self.pinned_source)
        self.blob_bytes, self.journal_bytes = self.blob.read_bytes(), self.journal.read_bytes()
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))

    def _verify_second(self):
        mixie.record_library_only_import(self.issues['Second'], self.receipts['Second'])

    def _input_snapshot(self):
        return (sort.file_manifest(self.inbox), sort.file_manifest(self.stage),
                sort.file_manifest(self.library),
                sort.file_manifest(self.script / '.staging-provenance'),
                (self.script / '.verified_imports.json').read_bytes())

    def _backup_snapshot(self):
        return {path.relative_to(self.backup).as_posix():
                (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.backup.rglob('*') if path.is_file()}

    def _assert_new_work_blocked(self, *, albums=None):
        inputs, backups, state = self._input_snapshot(), self._backup_snapshot(), final._state()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        with (patch.object(store, '_write_partial', side_effect=AssertionError('Cannot rebuild a pinned payload')),
              patch.object(store, '_save_journal', side_effect=AssertionError('Cannot recreate a pinned journal')),
              patch.object(final, '_write_zip', side_effect=AssertionError('Cannot publish a new Library ZIP'))):
            count, errors = self.finish(cleanup=False, albums=albums)
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertEqual(state, final._state())
        self.assertIsNone(final._state()['active'])
        self.assertEqual(inputs, self._input_snapshot())
        self.assertEqual(backups, self._backup_snapshot())

    def _redelivery(self, *, outer='Renamed redelivery', album='Second', changed=False):
        original = self.inbox / outer
        for relative, data in self.original_files.items():
            target = original / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'different delivery notes' if changed and relative == 'README.txt' else data)
        with redirect_stdout(StringIO()):
            sort.sort_inbox()
        name = sort.staging_import_name(outer, 'directory')
        template = self.receipts[album]
        mapping = {f'{name}/{Path(relative).relative_to(self.name).as_posix()}': deepcopy(row)
                   for relative, row in template.input_map.items()}
        issue = {'import_name': name, 'album': album, 'tracks': sorted(mapping)}
        receipt = mixie.CommitReceipt(dict(template), input_map=mapping,
            source_manifest=mixie.completion_source_manifest(issue),
            asset_outputs=deepcopy(template.asset_outputs), evidence=deepcopy(template.evidence))
        fingerprint = mixie.record_library_only_import(issue, receipt)
        albums = {(name, album): {'album': album,
                  'tracks': [{'path': self.stage / relative} for relative in issue['tracks']]}}
        return issue, fingerprint, albums

    def test_missing_blob_and_journal_cannot_be_rebuilt_by_a_new_sibling(self):
        self._publish_first()
        self.blob.unlink()
        self.journal.unlink()
        self._verify_second()
        self._assert_new_work_blocked()
        self.assertFalse(self.blob.exists() or self.journal.exists())
        self.assertNotIn(self.identities['Second'], final._state()['finalized'])

    def test_corrupt_published_blob_cannot_be_repaired_by_a_new_sibling(self):
        self._publish_first()
        self.blob.write_bytes(b'corrupt published original')
        self._verify_second()
        self._assert_new_work_blocked()
        self.assertEqual(b'corrupt published original', self.blob.read_bytes())
        self.assertEqual(self.journal_bytes, self.journal.read_bytes())

    def test_missing_journal_with_blob_present_cannot_be_recreated_by_a_new_sibling(self):
        self._publish_first()
        self.journal.unlink()
        self._verify_second()
        self._assert_new_work_blocked()
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.blob_bytes, self.blob.read_bytes())

    def test_exact_owner_restoration_resumes_sibling_without_rebuilding_the_shared_object(self):
        self._publish_first(zipped=True)
        self.blob.unlink()
        self.journal.unlink()
        self._verify_second()
        self._assert_new_work_blocked()
        # Owner fixture restoration is explicit; the application may only verify/reuse it.
        self.blob.write_bytes(self.blob_bytes)
        self.journal.write_bytes(self.journal_bytes)
        inputs = self._input_snapshot()
        first_library = Path(self.first_row['library']['path'])
        old_files = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                     for path in (self.blob, self.journal, first_library)}
        with (patch.object(store, '_write_partial', side_effect=AssertionError('Cannot rebuild restored bytes')),
              patch.object(store, '_save_journal', side_effect=AssertionError('Cannot rewrite restored journal')),
              patch.object(final, '_write_zip', wraps=final._write_zip) as library_write):
            self.assertEqual((1, []), self.finish(cleanup=False))
        library_write.assert_called_once()
        self.assertEqual('Library', library_write.call_args.args[0])
        state = final._state()
        self.assertEqual(self.first_row, state['finalized'][self.identities['First']])
        second = state['finalized'][self.identities['Second']]
        self.assertEqual(self.record, second['originals']['deliveries'][self.name]['archive'])
        self.assertEqual([self.blob], sorted((self.backup / 'Originals').rglob('*.zip')))
        self.assertEqual(old_files, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in old_files})
        self.assertEqual(inputs, self._input_snapshot())
        self.assertTrue(all(final.is_album_finalized(mixie, issue) for issue in self.issues.values()))

    def test_same_content_renamed_delivery_honors_the_old_missing_pin(self):
        self._publish_first()
        self.blob.unlink()
        self.journal.unlink()
        issue, fingerprint, albums = self._redelivery()
        source = final._complete_source(issue['import_name'], issue['tracks'], allow_partial=True)
        self.assertNotEqual(self.name, source['name'])
        self.assertNotEqual(self.pinned_source['provenance'], source['provenance'])
        self.assertEqual(store.delivery_descriptor(self.pinned_source), store.delivery_descriptor(source))
        self._assert_new_work_blocked(albums=albums)
        self.assertNotIn(fingerprint, final._state()['finalized'])
        self.assertFalse(self.blob.exists() or self.journal.exists())

    def test_pin_only_in_completed_cleanup_receipt_blocks_a_renamed_redelivery_rebuild(self):
        self._publish_first(album_names=('First',))
        self.assertEqual((0, []), self.finish(cleanup=True))
        self.assertFalse(self.source.exists() or self.staged.exists())
        state = final._state()
        self.assertEqual(1, len(state['completed']))
        transaction, = state['completed'].values()
        self.assertIn(self.first_row, transaction['albums'])
        state['finalized'].clear()  # Synthetic loss of the index, not the independent cleanup receipt.
        final._write(state)
        self.blob.unlink()
        self.journal.unlink()
        issue, fingerprint, albums = self._redelivery(album='First')
        source = final._complete_source(issue['import_name'], issue['tracks'])
        self.assertEqual(store.delivery_descriptor(self.pinned_source), store.delivery_descriptor(source))
        self._assert_new_work_blocked(albums=albums)
        self.assertEqual({}, final._state()['finalized'])
        self.assertNotIn(fingerprint, final._state()['finalized'])
        self.assertEqual(state['completed'], final._state()['completed'])
        self.assertFalse(self.blob.exists() or self.journal.exists())

    def test_library_only_damage_and_unrelated_missing_pins_do_not_block_valid_new_archives(self):
        self._publish_first()
        Path(self.first_row['library']['path']).write_bytes(b'damaged first Library ZIP')
        self._verify_second()
        inputs = self._input_snapshot()
        shared_before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                         for path in (self.blob, self.journal)}
        with (patch.object(store, '_write_partial', side_effect=AssertionError('Cannot recopy valid Originals')),
              patch.object(store, '_save_journal', side_effect=AssertionError('Cannot rewrite valid journal'))):
            count, errors = self.finish(cleanup=False)
        self.assertEqual(1, count)
        self.assertTrue(errors)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertTrue(final.is_album_finalized(mixie, self.issues['Second']))
        second = final._state()['finalized'][self.identities['Second']]
        self.assertEqual(self.record, second['originals']['deliveries'][self.name]['archive'])
        self.assertEqual(shared_before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                        for path in shared_before})
        self.assertEqual(inputs, self._input_snapshot())

        self.blob.unlink()
        self.journal.unlink()
        issue, fingerprint, albums = self._redelivery(outer='Unrelated delivery', changed=True)
        source = final._complete_source(issue['import_name'], issue['tracks'], allow_partial=True)
        self.assertNotEqual(store.delivery_descriptor(self.pinned_source), store.delivery_descriptor(source))
        inputs = self._input_snapshot()
        with patch.object(store, '_write_partial', wraps=store._write_partial) as payload_write:
            count, errors = self.finish(cleanup=False, albums=albums)
        self.assertEqual(1, count)
        self.assertTrue(errors)
        payload_write.assert_called_once()
        self.assertEqual(self.shared_journal_path(source).with_suffix('.partial'), payload_write.call_args.args[0])
        row = final._state()['finalized'][fingerprint]
        archive = row['originals']['deliveries'][source['name']]['archive']
        self.assertNotEqual(self.record['object_id'], archive['object_id'])
        store.validate_archive(self.backup, source, archive)
        self.assertTrue(final.is_album_finalized(mixie, issue))
        self.assertFalse(self.blob.exists() or self.journal.exists())
        self.assertEqual(inputs, self._input_snapshot())

    def test_damaged_shared_format_or_reference_cannot_downgrade_the_existing_pin(self):
        self._publish_first()
        self._verify_second()
        baseline = final._state()
        self.blob.unlink()
        self.journal.unlink()
        for damage in ('missing_format', 'wrong_format', 'missing_delivery', 'missing_archive', 'legacy_archive_shape'):
            with self.subTest(damage=damage):
                state = deepcopy(baseline)
                originals = state['finalized'][self.identities['First']]['originals']
                if damage == 'missing_format':
                    originals.pop('format')
                elif damage == 'wrong_format':
                    originals['format'] = 'unknown_originals_v1'
                elif damage == 'missing_delivery':
                    originals['deliveries'].pop(self.name)
                elif damage == 'missing_archive':
                    originals['deliveries'][self.name].pop('archive')
                else:
                    originals['deliveries'][self.name]['archive'] = {
                        key: self.record[key] for key in ('path', 'file_sha256', 'entries')}
                final._write(state)
                self._assert_new_work_blocked()
                self.assertFalse(self.blob.exists() or self.journal.exists())
