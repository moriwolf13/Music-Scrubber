"""Queue/finalizer/authorization integration on disposable offline data only."""
from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import zipfile

import archive
import mixie
import mixie_finalizer as final
import mixie_shared_originals as store
import sort
import tag
from support import WorkspaceCase, metadata_record
from test_finalizer_v10i import assert_shared_originals


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FinalizationIntegrationTests(WorkspaceCase):
    def delivery(self, *, verified=True, shared=False):
        (self.script / '.mixie-import-first.json').write_text(json.dumps({
            'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')
        self.source = self.inbox / 'Synthetic delivery'
        names = ('First', 'Second') if shared else ('Album',)
        for album in names:
            folder = self.source / album
            folder.mkdir(parents=True)
            (folder / '01.flac').write_bytes(('untouched source ' + album).encode())
            (folder / 'art.png').write_bytes(('untouched art ' + album).encode())
        (self.source / 'README.txt').write_bytes(b'untouched shared notes')
        self.payload = {path.relative_to(self.source).as_posix(): path.read_bytes()
                        for path in self.source.rglob('*') if path.is_file()}
        with redirect_stdout(StringIO()):
            sort.sort_inbox()
        self.name = sort.staging_import_name(self.source.name, 'directory')
        self.staged = self.stage / self.name
        self.albums, self.issues, self.receipts = {}, {}, {}
        for album in names:
            relative = f'{self.name}/{album}/01.flac'
            issue = {'import_name': self.name, 'album': album, 'tracks': [relative],
                     'status': 'needs_review', 'reason': 'Synthetic identity needs review',
                     'details': {'kind': 'identity_release'}}
            folder = self.library / 'Synthetic Artist' / album
            folder.mkdir(parents=True)
            music, cover = folder / '01 - Song.flac', folder / 'cover.jpg'
            music.write_bytes(('corrected audio ' + album).encode())
            cover.write_bytes(('verified artwork ' + album).encode())
            receipt = mixie.CommitReceipt({str(music): sha(music), str(cover): sha(cover)},
                input_map={relative: {'source_sha256': sha(self.stage / relative),
                    'destination': str(music), 'destination_sha256': sha(music)}},
                source_manifest=mixie.completion_source_manifest(issue),
                asset_outputs={str(cover): sha(cover)},
                evidence={'provenance': 'independently_verified'})
            self.issues[album], self.receipts[album] = issue, receipt
            self.albums[(self.name, album)] = {'album': album,
                'tracks': [{'path': self.stage / relative}]}
            if verified and album == names[0]:
                mixie.record_library_only_import(issue, receipt)
        self.issue = self.issues[names[0]]
        self.receipt = self.receipts[names[0]]
        self.inbox_before, self.stage_before = sort.file_manifest(self.inbox), sort.file_manifest(self.stage)
        self.provenance = sort.staging_provenance(self.name)

    @contextmanager
    def offline_recovery(self, *, forbid_review=True):
        with ExitStack() as stack:
            forbidden = [(sort, 'sort_inbox'), (tag, 'group_staged_albums'),
                         (mixie, 'preview_album'), (mixie, 'test_album_plan'),
                         (mixie, 'get_acoustid_client_key')]
            if forbid_review:
                forbidden.append((mixie, 'test_review_decision'))
            for module, name in forbidden:
                stack.enter_context(patch.object(module, name,
                    side_effect=AssertionError('Recovery must not call ' + name)))
            stack.enter_context(patch('builtins.input', side_effect=AssertionError('Recovery must not prompt')))
            stack.enter_context(redirect_stdout(StringIO()))
            yield

    def assert_retained(self):
        self.assertEqual(self.inbox_before, sort.file_manifest(self.inbox))
        self.assertEqual(self.stage_before, sort.file_manifest(self.stage))
        self.assertEqual(self.provenance, sort.staging_provenance(self.name))

    def assert_pair(self, album='Album'):
        library = self.backup / 'Library' / 'Synthetic Artist' / f'{album}.zip'
        identity = mixie.completion_fingerprint(self.name,
            mixie._source_rows(self.issues[album], self.receipts[album].source_manifest),
            self.receipts[album].source_manifest)
        row = final._state()['finalized'][identity]
        records = assert_shared_originals(self, row, {self.name: {
            'kind': 'directory', 'payload': self.payload, 'tracks': [f'{album}/01.flac']}})
        self.assertEqual({Path(records[self.name]['path'])},
                         set((self.backup / 'Originals').rglob('*.zip')))
        self.assertEqual(str(library), row['library']['path'])
        self.assertEqual(sha(library), row['library']['file_sha256'])
        self.assertEqual(sort.zip_manifest(library), row['library']['entries'])
        folder = self.library / 'Synthetic Artist' / album
        with zipfile.ZipFile(library) as saved:
            actual = {name: saved.read(name) for name in saved.namelist() if name != final.MANIFEST_NAME}
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, actual)
        return records[self.name]

    def pending(self, *, all_albums=False):
        issues = list(self.issues.values()) if all_albums else [self.issue]
        pending = deepcopy(issues)
        mixie.save_review_queue(pending)
        return pending

    def fail_library(self):
        real = final._write_zip
        def write(kind, *args):
            if kind == 'Library':
                raise OSError('synthetic required Library ZIP failure')
            return real(kind, *args)
        return patch.object(final, '_write_zip', side_effect=write)

    def test_auto_ingest_archives_both_copies_then_cleans_complete_delivery(self):
        """Menu Auto-Ingest owns explicit cleanup, not an unverified import."""
        self.delivery()
        output = StringIO()
        with (patch.object(mixie.sys, 'argv', ['mixie.py']),
              patch.object(mixie, 'get_acoustid_client_key', return_value='isolated-key'),
              patch.object(sort, 'sort_inbox'),
              patch.object(tag, 'group_staged_albums', return_value=self.albums),
              patch.object(mixie, 'is_staged_album_already_imported', return_value=True),
              patch.object(mixie, 'review_manager_menu', side_effect=AssertionError('unexpected review')),
              patch('builtins.input', return_value='n'), redirect_stdout(output)):
            mixie._main('1')
        self.assertFalse(self.source.exists(), output.getvalue())
        self.assertFalse(self.staged.exists())
        self.assert_pair()
        self.assertTrue(final.is_album_finalized(mixie, self.issue))
        self.assertEqual([], mixie.load_review_queue())

    def test_auto_ingest_archive_failure_never_removes_originals(self):
        self.delivery()
        output = StringIO()
        with (patch.object(mixie.sys, 'argv', ['mixie.py']),
              patch.object(mixie, 'get_acoustid_client_key', return_value='isolated-key'),
              patch.object(sort, 'sort_inbox'),
              patch.object(tag, 'group_staged_albums', return_value=self.albums),
              patch.object(mixie, 'is_staged_album_already_imported', return_value=True),
              self.fail_library(), patch('builtins.input', return_value='n'),
              redirect_stdout(output)):
            mixie._main('1')
        self.assert_retained()
        self.assertFalse(final.is_album_finalized(mixie, self.issue))

    def test_auto_ingest_shared_incomplete_delivery_keeps_both_sources(self):
        self.delivery(shared=True)
        # One album has a durable verified import; the other is unreviewed.
        # The first may receive both ZIP archives, but neither original source
        # nor the shared stage may be partially deleted with the v1 provenance.
        from review import AlbumReviewRequired
        def preview(album, *args, **kwargs):
            if album['album'] == 'Second':
                raise AlbumReviewRequired({'kind': 'identity_release',
                    'reason': 'Unresolved second album', 'tracks': [], 'releases': []})
            raise AssertionError('Already imported First must not scan again.')
        output = StringIO()
        with (patch.object(mixie.sys, 'argv', ['mixie.py']),
              patch.object(mixie, 'get_acoustid_client_key', return_value='isolated-key'),
              patch.object(sort, 'sort_inbox'),
              patch.object(tag, 'group_staged_albums', return_value=self.albums),
              patch.object(mixie, 'is_staged_album_already_imported',
                  side_effect=lambda issue: 'First/' in issue['tracks'][0]),
              patch.object(mixie, 'preview_album', side_effect=preview),
              patch('builtins.input', return_value='n'), redirect_stdout(output)):
            mixie._main('1')
        self.assert_retained()
        self.assertIn('First', output.getvalue())
        self.assert_pair('First')
        self.assertTrue((self.staged/'Second'/'01.flac').exists())

    def test_receipt_only_recovery_command_never_ingests_and_keeps_sources_by_default(self):
        self.delivery()
        with self.offline_recovery(), patch.object(mixie.sys, 'argv', ['mixie.py', '--finalize-verified']):
            count, problems = mixie.main()
        self.assertEqual((1, []), (count, problems))
        self.assertEqual([], mixie.load_review_queue())
        self.assert_retained()
        self.assert_pair()
        archives = {path: (path.stat().st_mtime_ns, sha(path)) for path in self.backup.rglob('*.zip')}
        with self.offline_recovery():
            self.assertEqual((0, []), mixie.finalize_verified_imports())
        self.assertEqual(archives, {path: (path.stat().st_mtime_ns, sha(path)) for path in archives})

    def test_receipt_only_command_requires_separate_explicit_cleanup_authorization(self):
        self.delivery()
        with self.offline_recovery(), patch.object(mixie.sys, 'argv', [
                'mixie.py', '--finalize-verified', '--allow-source-cleanup']):
            self.assertEqual((1, []), mixie.main())
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertTrue(final.is_album_finalized(mixie, self.issue))
        self.assertEqual([], mixie.load_review_queue())

    def test_failed_recovery_command_persists_pending_work_then_retries_without_reimport(self):
        self.delivery()
        with self.offline_recovery(), self.fail_library():
            count, problems = mixie.finalize_verified_imports()
        self.assertEqual(0, count)
        self.assertTrue(problems)
        self.assertEqual('archive_pending', mixie.load_review_queue()[0]['status'])
        with self.offline_recovery():
            self.assertEqual((1, []), mixie.finalize_verified_imports())
        self.assertEqual([], mixie.load_review_queue())
        self.assert_retained()

    def test_source_change_invalidates_choices_but_preserves_their_audit_history(self):
        self.delivery(verified=False)
        pending = self.pending()
        pending[0].update(source_fingerprint=mixie.review_fingerprint(self.issue),
                          manual_fields={'genre': 'Jazz'})
        mixie.save_review_queue(pending)
        (self.stage / self.issue['tracks'][0]).write_bytes(b'changed fixture')
        fresh = {**self.issue, 'reason': 'fresh evidence'}
        with self.offline_recovery():
            result = mixie.reconcile_review_queue([fresh])
        self.assertEqual(1, len(result))
        self.assertNotIn('manual_fields', result[0])
        self.assertEqual({'genre': 'Jazz'}, result[0]['decision_history'][0]['choices']['manual_fields'])

    def test_missing_review_source_is_retained_as_unresolved_not_silently_removed(self):
        self.delivery(verified=False)
        self.pending()
        (self.stage / self.issue['tracks'][0]).unlink()
        with self.offline_recovery():
            result = mixie.reconcile_review_queue([])
        self.assertEqual(1, len(result))
        self.assertEqual(self.issue['tracks'], result[0]['tracks'])
        self.assertFalse(final.is_album_finalized(mixie, result[0]))

    def test_library_only_receipt_keeps_existing_queue_archive_pending(self):
        self.delivery()
        self.pending()
        with self.offline_recovery():
            pending = mixie.reconcile_review_queue([])
        self.assertEqual(1, len(pending))
        self.assertEqual('archive_pending', pending[0]['status'])
        self.assertTrue(mixie.is_library_import_verified(self.issue))
        self.assertFalse(final.is_album_finalized(mixie, self.issue))
        self.assert_retained()

    def test_library_only_receipt_reconstructs_missing_queue_without_fresh_diagnostics(self):
        self.delivery()
        with self.offline_recovery():
            pending = mixie.reconcile_review_queue([])
        self.assertEqual(1, len(pending))
        self.assertEqual('archive_pending', pending[0]['status'])
        self.assertEqual(self.issue['tracks'], pending[0]['tracks'])
        self.assert_retained()

    def test_existing_receipt_apply_requires_both_archives_and_does_not_reimport(self):
        self.delivery()
        pending = self.pending()
        with self.offline_recovery(), self.fail_library():
            self.assertFalse(mixie.finalize_reviewed_album(pending[0], pending))
        self.assertEqual(1, len(pending))
        self.assertEqual('archive_pending', pending[0]['status'])
        self.assertEqual('archive_pending', mixie.load_review_queue()[0]['status'])
        self.assertTrue(mixie.is_library_import_verified(self.issue))
        self.assertFalse(final.is_album_finalized(mixie, self.issue))
        self.assert_retained()
        receipt_bytes = (self.script / '.verified_imports.json').read_bytes()
        self.assertEqual({}, final._state()['finalized'])
        journals = list((self.backup / 'Originals' / 'Shared' / 'deliveries').glob('*.json'))
        self.assertEqual(1, len(journals))
        journal_bytes, journal_mtime = journals[0].read_bytes(), journals[0].stat().st_mtime_ns
        journal = json.loads(journal_bytes)
        self.assertEqual('published', journal['state'])
        original = Path(journal['archive']['path'])
        self.assertEqual(sha(original), journal['archive']['file_sha256'])
        original_bytes, original_mtime = original.read_bytes(), original.stat().st_mtime_ns
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
        self.assertEqual([], pending)
        self.assertEqual([], mixie.load_review_queue())
        self.assertEqual(receipt_bytes, (self.script / '.verified_imports.json').read_bytes())
        self.assertEqual((original_bytes, original_mtime), (original.read_bytes(), original.stat().st_mtime_ns))
        self.assertEqual((journal_bytes, journal_mtime), (journals[0].read_bytes(), journals[0].stat().st_mtime_ns))
        self.assertTrue(final.is_album_finalized(mixie, self.issue))
        self.assert_retained()
        self.assert_pair()

    def test_new_review_import_checkpoints_receipt_before_archive_failure_and_retries_once(self):
        self.delivery(verified=False)
        pending = self.pending()
        review_issue = pending[0]
        with self.offline_recovery(forbid_review=False), \
             patch.object(mixie, 'test_review_decision', return_value=self.receipt) as importer:
            with self.fail_library():
                self.assertFalse(mixie.finalize_reviewed_album(pending[0], pending))
            self.assertTrue(mixie.is_library_import_verified(self.issue))
            self.assertEqual(1, len(pending))
            self.assertEqual('archive_pending', pending[0]['status'])
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
        importer.assert_called_once_with(review_issue, commit=True)
        self.assertEqual([], mixie.load_review_queue())
        self.assert_retained()
        self.assert_pair()

    def test_review_finalization_default_retains_sources_even_after_queue_retirement(self):
        self.delivery()
        pending = self.pending()
        with self.offline_recovery(), \
             patch.object(final.os, 'rename', side_effect=AssertionError('Unauthorized source rename')), \
             patch.object(final.shutil, 'rmtree', side_effect=AssertionError('Unauthorized source deletion')):
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
        self.assertEqual([], pending)
        self.assert_retained()
        self.assert_pair()
        self.assertEqual({}, final._state()['completed'])

    def test_review_finalization_explicit_authorization_cleans_complete_source(self):
        self.delivery()
        pending = self.pending()
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending, allow_source_cleanup=True))
        self.assertEqual([], pending)
        self.assertEqual([], mixie.load_review_queue())
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertTrue(final.is_album_finalized(mixie, self.issue))
        self.assertEqual(1, len(final._state()['completed']))

    def test_finalized_review_does_not_resurrect_from_retained_stage_or_old_diagnostics(self):
        self.delivery()
        pending = self.pending()
        old = deepcopy(pending[0])
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
            self.assertEqual([], mixie.reconcile_review_queue([old]))
            self.assertEqual([], mixie.reconcile_review_queue([]))
        self.assertEqual([], mixie.load_review_queue())
        self.assert_retained()

    def test_stale_persisted_queue_cannot_resurrect_finalized_retained_album(self):
        self.delivery()
        pending = self.pending()
        old = deepcopy(pending)
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
            mixie.save_review_queue(old)
            self.assertEqual([], mixie.reconcile_review_queue([]))
        self.assert_retained()

    def test_missing_required_originals_archive_keeps_queue_despite_library_receipt(self):
        self.delivery()
        pending = self.pending()
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
        record = self.assert_pair()
        original = Path(record['path'])
        journal = self.backup / 'Originals' / 'Shared' / 'deliveries' / f'{record["object_id"]}.json'
        journal_bytes = journal.read_bytes()
        original.unlink()
        with self.offline_recovery(), patch.object(store, '_write_partial',
                side_effect=AssertionError('Missing pinned original must not be rebuilt')):
            pending = mixie.reconcile_review_queue([deepcopy(self.issue)])
            self.assertEqual(['archive_pending'], [row['status'] for row in pending])
            count, problems = mixie.finalize_verified_imports(allow_source_cleanup=True)
        self.assertEqual(0, count)
        self.assertTrue(problems)
        self.assertEqual(['archive_pending'], [row['status'] for row in mixie.load_review_queue()])
        self.assertFalse(final.is_album_finalized(mixie, self.issue))
        self.assertFalse(original.exists())
        self.assertEqual([], list((self.backup / 'Originals').rglob('*.zip')))
        self.assertEqual(journal_bytes, journal.read_bytes())
        self.assertEqual({}, final._state()['completed'])
        self.assert_retained()

    def test_corrupt_referenced_original_blocks_queue_and_cleanup_without_alternate_archive(self):
        self.delivery()
        pending = self.pending()
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
        record = self.assert_pair()
        original = Path(record['path'])
        journal = self.backup / 'Originals' / 'Shared' / 'deliveries' / f'{record["object_id"]}.json'
        journal_bytes = journal.read_bytes()
        original.write_bytes(b'corrupt pinned untouched delivery')
        pending = self.pending()
        with self.offline_recovery(), patch.object(store, '_write_partial',
                side_effect=AssertionError('Corrupt pinned original must not be rebuilt')):
            self.assertFalse(mixie.finalize_reviewed_album(pending[0], pending, allow_source_cleanup=True))
        self.assertEqual(['archive_pending'], [row['status'] for row in pending])
        self.assertEqual(pending, mixie.load_review_queue())
        self.assertFalse(final.is_album_finalized(mixie, self.issue))
        self.assertEqual(b'corrupt pinned untouched delivery', original.read_bytes())
        self.assertEqual([original], list((self.backup / 'Originals').rglob('*.zip')))
        self.assertEqual(journal_bytes, journal.read_bytes())
        self.assertEqual({}, final._state()['completed'])
        self.assert_retained()

    def test_missing_shared_original_journal_cannot_be_recreated_to_retire_queue_or_clean_source(self):
        self.delivery()
        pending = self.pending()
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
        record = self.assert_pair()
        original = Path(record['path'])
        original_before = original.read_bytes(), original.stat().st_mtime_ns
        journal = self.backup / 'Originals' / 'Shared' / 'deliveries' / f'{record["object_id"]}.json'
        journal.unlink()
        pending = self.pending()
        with self.offline_recovery():
            self.assertFalse(mixie.finalize_reviewed_album(pending[0], pending, allow_source_cleanup=True))
        self.assertEqual(['archive_pending'], [row['status'] for row in pending])
        self.assertEqual(pending, mixie.load_review_queue())
        self.assertFalse(final.is_album_finalized(mixie, self.issue))
        self.assertFalse(journal.exists())
        self.assertEqual(original_before, (original.read_bytes(), original.stat().st_mtime_ns))
        self.assertEqual([original], list((self.backup / 'Originals').rglob('*.zip')))
        self.assertEqual({}, final._state()['completed'])
        self.assert_retained()

    def test_corrupt_required_library_archive_keeps_review_pending_without_reimport(self):
        self.delivery()
        pending = self.pending()
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending))
        archive_path = self.backup / 'Library' / 'Synthetic Artist' / 'Album.zip'
        archive_path.write_bytes(b'corrupted required archive')
        pending = self.pending()
        with self.offline_recovery():
            self.assertFalse(mixie.finalize_reviewed_album(pending[0], pending))
        self.assertEqual(['archive_pending'], [row['status'] for row in pending])
        self.assertEqual(b'corrupted required archive', archive_path.read_bytes())
        self.assert_retained()

    def test_selective_review_retirement_preserves_unresolved_sibling_and_fingerprint(self):
        self.delivery(shared=True)
        pending = self.pending(all_albums=True)
        sibling = deepcopy(pending[1])
        fingerprint = mixie.review_fingerprint(sibling)
        with self.offline_recovery():
            self.assertTrue(mixie.finalize_reviewed_album(pending[0], pending, allow_source_cleanup=True))
        self.assertEqual([sibling], pending)
        self.assertEqual([sibling], mixie.load_review_queue())
        self.assertEqual(fingerprint, mixie.review_fingerprint(sibling))
        self.assert_retained()
        self.assert_pair('First')
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))
        self.assertFalse(final.is_album_finalized(mixie, sibling))

    def test_main_archive_failure_keeps_queue_then_retries_without_second_import(self):
        self.delivery(verified=False)
        with patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(sort, 'sort_inbox'), \
             patch.object(tag, 'group_staged_albums', return_value=self.albums), \
             patch.object(mixie, 'preview_album', return_value={'provenance': 'independently_verified'}) as preview, \
             patch.object(mixie, 'test_album_plan', return_value=self.receipt) as processor, \
             patch('builtins.input', return_value='n'), redirect_stdout(StringIO()):
            with self.fail_library():
                mixie._main('1')
            pending = mixie.load_review_queue()
            receipt_bytes = (self.script / '.verified_imports.json').read_bytes()
            self.assertEqual(['archive_pending'], [row['status'] for row in pending])
            self.assertFalse(final.is_album_finalized(mixie, self.issue))
            mixie._main('1')
            self.assertEqual([], mixie.load_review_queue())
            mixie._main('1')
            self.assertEqual([], mixie.load_review_queue())
        self.assertEqual(1, preview.call_count)
        self.assertEqual(1, processor.call_count)
        self.assertEqual(receipt_bytes, (self.script / '.verified_imports.json').read_bytes())
        self.assertTrue(final.is_album_finalized(mixie, self.issue))
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assert_pair()

    def test_main_does_not_report_bare_library_import_as_completed_success(self):
        self.delivery(verified=False)
        with patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(sort, 'sort_inbox'), \
             patch.object(tag, 'group_staged_albums', return_value=self.albums), \
             patch.object(mixie, 'preview_album', return_value={'provenance': 'independently_verified'}), \
             patch.object(mixie, 'test_album_plan', return_value=self.receipt), \
             patch('builtins.input', return_value='n'), self.fail_library(), \
             redirect_stdout(StringIO()) as output:
            mixie._main('1')
        self.assertIn('Success: 0', output.getvalue())
        self.assertIn('Library imports verified (archive pending): 1', output.getvalue())
        self.assert_retained()

    def test_main_explicit_authorization_only_cleans_after_both_archives_verify(self):
        self.delivery()
        with patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(sort, 'sort_inbox'), \
             patch.object(tag, 'group_staged_albums', return_value=self.albums), \
             patch.object(mixie, 'preview_album', side_effect=AssertionError('No duplicate provider scan')), \
             patch.object(mixie, 'test_album_plan', side_effect=AssertionError('No duplicate import')), \
             patch('builtins.input', return_value='n'), redirect_stdout(StringIO()):
            mixie._main('1', allow_source_cleanup=True)
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual([], mixie.load_review_queue())
        self.assertTrue(final.is_album_finalized(mixie, self.issue))

    def test_interrupted_finalizer_recovery_default_blocks_ingestion_without_mutating_sources(self):
        self.delivery()
        self.pending()
        with patch.object(final.os, 'rename', side_effect=OSError('synthetic quarantine interruption')):
            count, errors = final.finish_verified_albums(mixie, {}, allow_source_cleanup=True)
        self.assertEqual(0, count)
        self.assertTrue(errors)
        journal = (self.backup / final.LEDGER_NAME).read_bytes()
        queue = (self.script / '.needs_review.json').read_bytes()
        with self.offline_recovery():
            with self.assertRaisesRegex(RuntimeError, 'explicit authorization'):
                mixie.finish_prior_cleanup_before_ingest()
        self.assertEqual(journal, (self.backup / final.LEDGER_NAME).read_bytes())
        self.assertEqual(queue, (self.script / '.needs_review.json').read_bytes())
        self.assert_retained()
        with self.offline_recovery():
            mixie.finish_prior_cleanup_before_ingest(allow_source_cleanup=True)
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertTrue(final.is_album_finalized(mixie, self.issue))


class LegacyCleanupAuthorizationTests(WorkspaceCase):
    def prepare(self):
        self.source = self.inbox / 'Legacy cleanup fixture'
        self.source.mkdir()
        (self.source / '01.flac').write_bytes(b'untouched legacy fixture audio')
        (self.source / 'cover.png').write_bytes(b'untouched legacy fixture art')
        with redirect_stdout(StringIO()):
            sort.sort_inbox()
        self.name = sort.staging_import_name(self.source.name, 'directory')
        self.staged = self.stage / self.name
        music = self.library / 'Artist' / 'Album' / '01.flac'
        music.parent.mkdir(parents=True)
        music.write_bytes(b'verified corrected legacy fixture audio')
        cover = music.parent / 'cover.jpg'
        cover.write_bytes(b'verified corrected legacy fixture art')
        self.issue = {'import_name': self.name, 'album': 'Album', 'tracks': [f'{self.name}/01.flac']}
        self.album = {'album': 'Album', 'folder': self.staged,
                      'tracks': [metadata_record(self.staged / '01.flac')]}
        receipt = mixie.CommitReceipt({str(music): sha(music), str(cover): sha(cover)},
            input_map={self.issue['tracks'][0]: {'source_sha256': sha(self.staged / '01.flac'),
                'destination': str(music), 'destination_sha256': sha(music)}},
            source_manifest=mixie.completion_source_manifest(self.issue),
            asset_outputs={str(cover): sha(cover)}, evidence={'provenance': 'independently_verified'})
        self.stack.enter_context(patch.object(tag, 'group_staged_albums',
            return_value={(self.name, 'Album'): self.album}))
        self.stack.enter_context(patch.object(tag, 'read_track_metadata',
            side_effect=lambda path: metadata_record(Path(path), album=('Album',))))
        with redirect_stdout(StringIO()):
            self.originals = archive.update_original_downloads_archive()
            self.library_zip = archive.create_verified_zip(self.library, 'MusicLibrary')
            mixie.record_completed_review(self.issue, receipt, self.originals, self.library_zip)
        self.archive_bytes = self.originals.read_bytes(), self.library_zip.read_bytes()
        self.raw_before = sort.file_manifest(self.inbox), sort.file_manifest(self.stage)

    def quarantine(self):
        with redirect_stdout(StringIO()):
            moved = mixie.auto_quarantine_archived_completed(self.originals, self.library_zip,
                                                            allow_source_cleanup=True)
        self.assertEqual([self.name], moved)
        self.assertFalse(getattr(moved, 'errors', []))
        return self.backup / '.cleanup-quarantine'

    def assert_archives_unchanged(self):
        self.assertEqual(self.archive_bytes, (self.originals.read_bytes(), self.library_zip.read_bytes()))

    def test_legacy_auto_quarantine_default_is_read_only_even_with_full_completion_proof(self):
        self.prepare()
        with patch.object(sort, 'quarantine_archived_cleanup', side_effect=AssertionError('Unauthorized quarantine')), \
             patch.object(sort, 'quarantine_inbox_cleanup', side_effect=AssertionError('Unauthorized quarantine')):
            self.assertEqual([], mixie.auto_quarantine_archived_completed(self.originals, self.library_zip))
        self.assertEqual(self.raw_before, (sort.file_manifest(self.inbox), sort.file_manifest(self.stage)))
        self.assert_archives_unchanged()

    def test_legacy_auto_purge_default_retains_all_held_bytes_then_explicitly_authorized_purge_works(self):
        self.prepare()
        root = self.quarantine()
        before = sort.file_manifest(root)
        with patch.object(mixie, 'purge_verified_archived_quarantine', side_effect=AssertionError('Unauthorized purge')), \
             patch.object(mixie, 'purge_verified_inbox_quarantine', side_effect=AssertionError('Unauthorized purge')):
            self.assertEqual([], mixie.auto_purge_verified_quarantines())
        self.assertEqual(before, sort.file_manifest(root))
        with redirect_stdout(StringIO()):
            purged = mixie.auto_purge_verified_quarantines(allow_source_cleanup=True)
        self.assertEqual([self.name], purged)
        self.assertFalse(getattr(purged, 'errors', []))
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertFalse(list(root.glob('.mixie-inbox-*')))
        self.assert_archives_unchanged()

    def test_legacy_pre_ingest_recovery_default_blocks_instead_of_silently_deleting(self):
        self.prepare()
        root = self.quarantine()
        before = sort.file_manifest(root)
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'Unfinished cleanup recovery'):
                mixie.finish_prior_cleanup_before_ingest()
        self.assertEqual(before, sort.file_manifest(root))
        with redirect_stdout(StringIO()):
            self.assertEqual([self.name], mixie.finish_prior_cleanup_before_ingest(allow_source_cleanup=True))
        self.assert_archives_unchanged()

    def test_legacy_cleanup_rejects_truthy_non_boolean_authorization(self):
        self.prepare()
        for value in (1, 'yes', None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    mixie.auto_quarantine_archived_completed(self.originals, self.library_zip,
                                                            allow_source_cleanup=value)
                with self.assertRaises(ValueError):
                    mixie.auto_purge_verified_quarantines(allow_source_cleanup=value)
        self.assertEqual(self.raw_before, (sort.file_manifest(self.inbox), sort.file_manifest(self.stage)))
        self.assert_archives_unchanged()
