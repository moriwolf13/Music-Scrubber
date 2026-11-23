"""Selective archive/finalization recovery; disposable isolated fixtures only."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import os
import shutil
import zipfile

from support import WorkspaceCase
import mixie
import mixie_finalizer as final
import mixie_shared_originals as store
import sort
import tag


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


class SelectiveFinalizationTests(WorkspaceCase):
    def delivery(self, *, verified=('First',), zipped=False, unclaimed=False,
                 album_names=('First', 'Second')):
        source = self.inbox / 'Shared delivery'
        for album in album_names:
            folder = source / album
            folder.mkdir(parents=True)
            for number in (1, 2):
                (folder / f'{number:02d}.flac').write_bytes(f'untouched {album} {number}'.encode())
            (folder / 'art.png').write_bytes(f'original art {album}'.encode())
        (source / 'README.txt').write_bytes(b'original delivery notes')
        if unclaimed:
            (source / 'unclaimed.flac').write_bytes(b'unknown album audio')
        self.original_files = {path.relative_to(source).as_posix(): path.read_bytes()
                               for path in source.rglob('*') if path.is_file()}
        self.original_zip = None
        if zipped:
            saved = self.inbox / 'Shared delivery.zip'
            with zipfile.ZipFile(saved, 'w') as output:
                for relative, data in sorted(self.original_files.items()):
                    output.writestr(relative, data)
            shutil.rmtree(source)
            source = saved
            self.original_zip = source.read_bytes()
        with redirect_stdout(StringIO()):
            sort.sort_inbox()
        name = sort.staging_import_name(source.name, 'zip' if zipped else 'directory')
        self.source, self.staged = source, self.stage / name
        self.name = name
        self.albums, self.issues, self.receipts, self.identities = {}, {}, {}, {}
        for album in album_names:
            tracks = [f'{name}/{album}/{number:02d}.flac' for number in (1, 2)]
            issue = {'import_name': name, 'album': album, 'tracks': tracks}
            folder = self.library / 'Synthetic Artist' / album
            folder.mkdir(parents=True)
            music = [folder / f'{number:02d} - Song.flac' for number in (1, 2)]
            for number, path in enumerate(music, 1):
                path.write_bytes(f'corrected {album} {number}'.encode())
            cover = folder / 'cover.jpg'
            cover.write_bytes(f'verified cover {album}'.encode())
            manifest = mixie.completion_source_manifest(issue)
            receipt = mixie.CommitReceipt({str(path): sha(path) for path in [*music, cover]},
                input_map={relative: {'source_sha256': sha(self.stage / relative),
                           'destination': str(output), 'destination_sha256': sha(output)}
                           for relative, output in zip(tracks, music)},
                source_manifest=manifest, asset_outputs={str(cover): sha(cover)},
                evidence={'provenance': 'independently_verified'})
            self.issues[album], self.receipts[album] = issue, receipt
            self.identities[album] = mixie.review_fingerprint(issue)
            self.albums[(name, album)] = {'album': album,
                'tracks': [{'path': self.stage / relative} for relative in tracks]}
            if album in verified:
                mixie.record_library_only_import(issue, receipt)
        self.inbox_before = sort.file_manifest(self.inbox)
        self.stage_before = sort.file_manifest(self.stage)
        self.provenance_before = sort.staging_provenance(name)

    def finish(self, *, cleanup=True, albums=None):
        return final.finish_verified_albums(mixie, self.albums if albums is None else albums,
                                            allow_source_cleanup=cleanup)

    def assert_retained(self):
        self.assertEqual(self.inbox_before, sort.file_manifest(self.inbox))
        self.assertEqual(self.stage_before, sort.file_manifest(self.stage))
        self.assertEqual(self.provenance_before, sort.staging_provenance(self.name))

    def shared_journal_path(self, source):
        identity = hashlib.sha256(canonical(store.delivery_descriptor(source))).hexdigest()
        return self.backup / 'Originals' / 'Shared' / 'deliveries' / f'{identity}.json'

    def archive_path(self, kind, album='First'):
        if kind != 'Originals':
            return self.backup / kind / 'Synthetic Artist' / (album + '.zip')
        row = final._state()['finalized'].get(self.identities[album])
        if row is not None:
            originals = row['originals']
            if originals.get('format') == 'shared_originals_v1':
                return Path(originals['deliveries'][self.name]['archive']['path'])
            return Path(originals['path'])
        if hasattr(self, 'legacy_rows'):
            return Path(self.legacy_rows[list(self.issues).index(album)]['originals']['path'])
        # A published delivery can outlive failure of either the Library ZIP or
        # the finalized-row write. Its retained journal, not an album name, pins it.
        source = final._complete_source(self.name, self.issues[album]['tracks'], allow_partial=True)
        journal = json.loads(self.shared_journal_path(source).read_bytes())
        self.assertEqual('published', journal['state'])
        store.validate_archive(self.backup, source, journal['archive'])
        return Path(journal['archive']['path'])

    def assert_archived_bytes(self, album='First'):
        state = final._state()
        row = state['finalized'][self.identities[album]]
        originals = row['originals']
        self.assertEqual({'format', 'deliveries'}, set(originals))
        self.assertEqual('shared_originals_v1', originals['format'])
        self.assertEqual({self.name}, set(originals['deliveries']))
        reference = originals['deliveries'][self.name]
        self.assertEqual({'archive', 'claimed_audio_sha256'}, set(reference))
        claims = {relative: self.provenance_before['payload_hashes'][relative]
                  for relative in (f'{album}/01.flac', f'{album}/02.flac')}
        self.assertEqual(claims, reference['claimed_audio_sha256'])
        self.assertEqual({self.name: sorted(self.issues[album]['tracks'])}, row['source_claims'])
        record = reference['archive']
        self.assertEqual({'version', 'object_id', 'path', 'file_sha256', 'entries'}, set(record))
        self.assertEqual(1, record['version'])
        original = self.archive_path('Originals', album)
        digest = sha(original)
        self.assertEqual(digest, record['file_sha256'])
        self.assertEqual(self.backup / 'Originals' / 'Shared' / 'sha256' / digest[:2] / f'{digest}.zip',
                         original)
        self.assertEqual(sort.zip_manifest(original), record['entries'])
        if self.original_zip is not None:
            expected = {'original.zip': self.original_zip}
        else:
            expected = {f'files/{relative}': data for relative, data in self.original_files.items()}
        descriptor = {'version': 1, 'type': 'shared_original_delivery',
                      'source_kind': 'zip' if self.original_zip is not None else 'directory',
                      'payload_sha256': {relative: hashlib.sha256(data).hexdigest()
                                         for relative, data in self.original_files.items()},
                      'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in expected.items()}}
        self.assertEqual(hashlib.sha256(canonical(descriptor)).hexdigest(), record['object_id'])
        source, = row['sources']
        self.assertEqual(self.name, source['name'])
        self.assertEqual(descriptor, store.validate_archive(self.backup, source, record))
        with zipfile.ZipFile(original) as saved:
            self.assertEqual(canonical(descriptor), saved.read(store.MANIFEST_NAME))
            self.assertEqual(set(expected) | {store.MANIFEST_NAME}, set(saved.namelist()))
            self.assertEqual(expected, {name: saved.read(name) for name in expected})
        journal_path = self.shared_journal_path(source)
        self.assertEqual({'version': 1, 'object_id': record['object_id'], 'descriptor': descriptor,
                          'state': 'published', 'archive': record}, json.loads(journal_path.read_bytes()))
        self.assertEqual([journal_path], sorted(journal_path.parent.glob('*.json')))
        self.assertFalse(journal_path.with_suffix('.partial').exists())
        self.assertFalse(journal_path.with_suffix('.json.tmp').exists())
        # All rows for this physical unit pin one object, never per-album raw copies.
        self.assertEqual([original], sorted((self.backup / 'Originals').rglob('*.zip')))
        self.assertFalse((self.backup / 'Originals' / 'Synthetic Artist').exists())
        for sibling_album, fingerprint in self.identities.items():
            sibling = state['finalized'].get(fingerprint)
            if sibling is None:
                continue
            sibling_ref = sibling['originals']['deliveries'][self.name]
            self.assertEqual(record, sibling_ref['archive'])
            self.assertEqual({relative: self.provenance_before['payload_hashes'][relative]
                              for relative in (f'{sibling_album}/01.flac', f'{sibling_album}/02.flac')},
                             sibling_ref['claimed_audio_sha256'])
        library = self.archive_path('Library', album)
        self.assertEqual(str(library), row['library']['path'])
        self.assertEqual(sha(library), row['library']['file_sha256'])
        self.assertEqual(sort.zip_manifest(library), row['library']['entries'])
        folder = self.library / 'Synthetic Artist' / album
        expected = {path.name: path.read_bytes() for path in folder.iterdir()}
        with zipfile.ZipFile(library) as saved:
            self.assertEqual(set(expected) | {final.MANIFEST_NAME}, set(saved.namelist()))
            self.assertEqual(expected, {name: saved.read(name) for name in expected})

    def interrupt_prepared(self, *, after_moves=0):
        real = final.os.rename
        calls = []
        def interrupt(source, destination):
            calls.append(source)
            if len(calls) > after_moves:
                raise OSError('synthetic interrupted quarantine')
            return real(source, destination)
        with patch.object(final.os, 'rename', side_effect=interrupt):
            count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual('prepared', final._state()['active']['phase'])

    def interrupt_purge(self, *, partial=False):
        def interrupt(folder, *args, **kwargs):
            if partial:
                next(Path(folder).rglob('*.flac')).unlink()
            raise OSError('synthetic interrupted purge')
        with patch.object(final.shutil, 'rmtree', side_effect=interrupt):
            count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        state = final._state()
        self.assertEqual('purging', state['active']['phase'])
        return self.backup / final.QUARANTINE_NAME / state['active']['id']

    def legacy_completed(self, *, shared=False, zipped=False):
        """Build the ORIGINAL v10j records/ZIP layouts, not downgraded new rows."""
        names = ('First', 'Second') if shared else ('First',)
        self.delivery(verified=names, zipped=zipped, album_names=names)
        (self.script / '.mixie-import-first.json').write_text(json.dumps({
            'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')
        tracks = [track for issue in self.issues.values() for track in issue['tracks']]
        source = final._complete_source(self.name, tracks)
        rows = []
        for album in names:
            if shared and not zipped:
                part = {relative: digest for relative, digest in source['source_hashes'].items()
                        if relative.split('/')[0] == album or (album == names[0]
                            and relative.split('/')[0] not in names)}
                label = '01-' + hashlib.sha256(self.name.encode()).hexdigest()[:10]
                files = {f'deliveries/{label}/{relative}': (self.source / relative, digest)
                         for relative, digest in part.items()}
                descriptor = {'type': 'untouched_originals', 'deliveries': [{
                    'label': label, 'original_name': source['source_name'],
                    'source_kind': 'directory', 'import_name': self.name, 'payload_sha256': part}]}
            else:
                files, descriptor = final._archive_entries_original([source])
            original, original_sha, original_entries = final._write_zip(
                'Originals', 'Synthetic Artist', album, files, descriptor)
            files, descriptor = final._archive_entries_library(self.library / 'Synthetic Artist' / album)
            library, library_sha, library_entries = final._write_zip(
                'Library', 'Synthetic Artist', album, files, descriptor)
            rows.append({'originals': {'path': original, 'file_sha256': original_sha, 'entries': original_entries},
                         'library': {'path': library, 'file_sha256': library_sha, 'entries': library_entries},
                         'receipt': dict(self.receipts[album])})
        self.legacy_rows = rows
        self.legacy_transaction = ({'sources': [source], 'albums': rows} if shared
                                   else {'sources': [source], **rows[0]})
        self.repin_legacy()
        final._validate_archives(self.legacy_transaction)
        if zipped:
            self.source.unlink()
        else:
            shutil.rmtree(self.source)
        shutil.rmtree(self.staged)
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual(self.provenance_before, sort.staging_provenance(self.name, verify_payload=False))

    def repin_legacy(self):
        txn = self.legacy_transaction
        if 'albums' in txn:
            identity = {'source': txn['sources'][0], 'albums': txn['albums']}
        else:
            identity = {'sources': txn['sources'], 'receipt': txn['receipt'],
                        'original': txn['originals']['file_sha256'], 'library': txn['library']['file_sha256']}
        self.legacy_id = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        txn['id'] = self.legacy_id
        final._write({'version': 1, 'active': None, 'completed': {self.legacy_id: txn}})

    def rewrite_legacy_archive(self, kind, *, row=0, change_bytes=False, remove_audio=False, wrong_identity=False):
        """Create self-consistent ZIP/journal pins that still lack semantic proof."""
        record = self.legacy_rows[row][kind]
        path = Path(record['path'])
        with zipfile.ZipFile(path) as saved:
            descriptor = json.loads(saved.read(final.MANIFEST_NAME))
            files = {name: saved.read(name) for name in saved.namelist() if name != final.MANIFEST_NAME}
        if change_bytes or remove_audio:
            name = next(name for name in files if name.endswith(('.flac', '.zip')))
            if remove_audio:
                del files[name]
            else:
                files[name] = b'unrelated unverified archived audio'
            if kind == 'originals' and descriptor['deliveries'][0]['source_kind'] == 'directory':
                delivery = descriptor['deliveries'][0]
                relative = name.removeprefix('deliveries/' + delivery['label'] + '/')
                if remove_audio:
                    del delivery['payload_sha256'][relative]
                else:
                    delivery['payload_sha256'][relative] = hashlib.sha256(files[name]).hexdigest()
        if wrong_identity:
            if kind == 'originals':
                descriptor['deliveries'][0]['original_name'] = 'Different physical delivery'
            else:
                descriptor['artist'] = 'Different Artist'
        descriptor['sha256'] = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        with zipfile.ZipFile(path, 'w') as saved:
            saved.writestr(final.MANIFEST_NAME, json.dumps(descriptor, sort_keys=True, ensure_ascii=False,
                                                        separators=(',', ':')).encode())
            for name, data in files.items():
                saved.writestr(name, data)
        record.update(file_sha256=sha(path), entries=sort.zip_manifest(path))
        self.repin_legacy()
        final._validate_archives(self.legacy_transaction)

    def test_partial_directory_finalizes_album_and_keeps_sibling_fingerprint(self):
        self.delivery()
        sibling = mixie.review_fingerprint(self.issues['Second'])
        count, errors = self.finish()
        self.assertEqual(1, count)
        self.assertTrue(any('unresolved' in error for error in errors))
        self.assert_retained()
        self.assertEqual(sibling, mixie.review_fingerprint(self.issues['Second']))
        self.assert_archived_bytes()
        self.assertNotIn(self.identities['Second'], final._state()['finalized'])
        self.assertTrue(self.archive_path('Originals').exists())
        self.assertFalse(self.archive_path('Library', 'Second').exists())
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))
        self.assertFalse(final.is_album_finalized(mixie, self.issues['Second']))
        self.assertIsNone(final._state()['active'])
        self.assertEqual({}, final._state()['completed'])

    def test_partial_zip_finalizes_album_and_retains_exact_shared_zip(self):
        self.delivery(zipped=True)
        self.assertEqual(1, self.finish()[0])
        self.assert_retained()
        self.assert_archived_bytes()
        self.assertEqual(self.original_zip, self.source.read_bytes())
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))

    def test_source_cleanup_is_non_destructive_by_default_even_when_all_verified(self):
        self.delivery(verified=('First', 'Second'))
        self.assertEqual((2, []), final.finish_verified_albums(mixie, self.albums))
        self.assert_retained()
        self.assertEqual(2, len(final._state()['finalized']))
        self.assertEqual({}, final._state()['completed'])
        self.assertFalse(final.resume_cleanup())
        for album in self.issues:
            self.assertTrue(final.is_album_finalized(mixie, self.issues[album]))
            self.assert_archived_bytes(album)

    def test_retained_receipts_restart_without_duplicate_archive_or_ledger_writes(self):
        self.delivery()
        self.finish()
        ledger = (self.backup / final.LEDGER_NAME).read_bytes()
        with patch.object(final, '_write_zip', side_effect=AssertionError('duplicate archive work')), \
             patch.object(store, '_write_partial', side_effect=AssertionError('duplicate original payload')), \
             patch.object(store, '_save_journal', side_effect=AssertionError('duplicate delivery journal work')), \
             patch.object(final, '_write', side_effect=AssertionError('duplicate receipt work')):
            count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual(ledger, (self.backup / final.LEDGER_NAME).read_bytes())
        self.assert_retained()
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))

    def test_later_sibling_completion_reuses_first_pair_then_purges_whole_directory(self):
        self.delivery()
        self.finish()
        original = self.archive_path('Originals')
        library = self.archive_path('Library')
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (original, library)]
        mixie.record_library_only_import(self.issues['Second'], self.receipts['Second'])
        with patch.object(final, '_write_zip', wraps=final._write_zip) as write, \
             patch.object(store, '_write_partial', side_effect=AssertionError('duplicate shared payload')), \
             patch.object(store, '_save_journal', side_effect=AssertionError('duplicate delivery journal')):
            self.assertEqual((1, []), self.finish())
        self.assertEqual(1, write.call_count)
        self.assertEqual('Library', write.call_args.args[0])
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in (original, library)])
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual(2, len(final._state()['finalized']))
        self.assertEqual(1, len(final._state()['completed']))
        for album in self.issues:
            self.assertTrue(final.is_album_finalized(mixie, self.issues[album]))
            self.assert_archived_bytes(album)
        self.assertEqual((0, []), self.finish(albums={}))

    def test_later_sibling_completion_purges_whole_zip_only_after_both_pairs(self):
        self.delivery(zipped=True)
        self.finish()
        mixie.record_library_only_import(self.issues['Second'], self.receipts['Second'])
        self.assertEqual((1, []), self.finish())
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assert_archived_bytes('First')
        self.assert_archived_bytes('Second')

    def test_already_verified_receipts_recover_without_new_ingestion_or_grouping(self):
        self.delivery(verified=('First', 'Second'))
        with patch.object(sort, 'sort_inbox', side_effect=AssertionError('new ingestion forbidden')), \
             patch.object(tag, 'group_staged_albums', side_effect=AssertionError('metadata scan forbidden')), \
             patch.object(mixie, 'preview_album', side_effect=AssertionError('provider scan forbidden')):
            self.assertEqual((2, []), self.finish(albums={}))
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assert_archived_bytes('First')
        self.assert_archived_bytes('Second')

    def test_partial_receipt_recovery_with_no_album_scan_retains_unaccounted_siblings(self):
        self.delivery()
        count, errors = self.finish(albums={})
        self.assertEqual(1, count)
        self.assertTrue(any('unaccounted' in error for error in errors))
        self.assert_retained()
        self.assert_archived_bytes()
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))

    def test_omitted_finalized_album_recovered_for_later_whole_source_cleanup(self):
        self.delivery()
        self.finish()
        mixie.record_library_only_import(self.issues['Second'], self.receipts['Second'])
        remaining = {(self.name, 'Second'): self.albums[(self.name, 'Second')]}
        self.assertEqual((1, []), self.finish(albums=remaining))
        self.assertFalse(self.source.exists() or self.staged.exists())

    def test_overlapping_album_claims_never_authorize_finalization(self):
        self.delivery(verified=('First', 'Second'))
        self.albums[(self.name, 'Second')]['tracks'] = self.albums[(self.name, 'First')]['tracks']
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(any('same source track' in error for error in errors))
        self.assert_retained()
        self.assertFalse((self.backup / 'Originals').exists())

    def test_missing_claim_never_authorizes_finalization(self):
        self.delivery()
        self.albums[(self.name, 'First')]['tracks'].append({'path': self.staged / 'First' / 'missing.flac'})
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assert_retained()
        self.assertFalse((self.backup / 'Originals').exists())

    def test_unaccounted_original_audio_prevents_cleanup_but_not_verified_archives(self):
        self.delivery(verified=('First', 'Second'), unclaimed=True)
        count, errors = self.finish()
        self.assertEqual(2, count)
        self.assertTrue(any('unaccounted' in error for error in errors))
        self.assert_retained()
        self.assert_archived_bytes('First')
        self.assert_archived_bytes('Second')
        self.assertIsNone(final._state()['active'])

    def changed_source(self, where, change):
        self.delivery(verified=('First', 'Second'))
        folder = self.source if where == 'original' else self.staged
        track = folder / 'Second' / '01.flac'
        if change == 'missing':
            track.unlink()
        elif change == 'extra':
            (folder / 'unexpected.flac').write_bytes(b'new unknown audio')
        else:
            track.write_bytes(b'changed original audio')
        inbox, stage = sort.file_manifest(self.inbox), sort.file_manifest(self.stage)
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual(inbox, sort.file_manifest(self.inbox))
        self.assertEqual(stage, sort.file_manifest(self.stage))
        self.assertFalse((self.backup / 'Originals').exists())
        self.assertIsNone(final._state()['active'])

    def test_missing_original_audio_blocks_all_mutations(self):
        self.changed_source('original', 'missing')

    def test_extra_original_audio_blocks_all_mutations(self):
        self.changed_source('original', 'extra')

    def test_changed_original_audio_blocks_all_mutations(self):
        self.changed_source('original', 'changed')

    def test_missing_staged_sibling_blocks_all_mutations(self):
        self.changed_source('stage', 'missing')

    def test_extra_staged_sibling_blocks_all_mutations(self):
        self.changed_source('stage', 'extra')

    def test_changed_staged_sibling_blocks_all_mutations(self):
        self.changed_source('stage', 'changed')

    def test_changed_original_zip_blocks_all_mutations(self):
        self.delivery(verified=('First', 'Second'), zipped=True)
        with zipfile.ZipFile(self.source, 'a') as saved:
            saved.writestr('unexpected.flac', b'extra original audio')
        before = self.source.read_bytes()
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(self.stage_before, sort.file_manifest(self.stage))
        self.assertFalse((self.backup / 'Originals').exists())

    def test_stale_library_receipt_cannot_authorize_shared_cleanup(self):
        self.delivery(verified=('First', 'Second'))
        (self.library / 'Synthetic Artist' / 'First' / '01 - Song.flac').write_bytes(b'changed library audio')
        count, errors = self.finish()
        self.assertEqual(1, count)
        self.assertTrue(any('unresolved' in error for error in errors))
        self.assert_retained()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertTrue(final.is_album_finalized(mixie, self.issues['Second']))

    def test_completed_receipt_rejects_later_library_inventory_addition(self):
        self.delivery()
        self.finish()
        (self.library / 'Synthetic Artist' / 'First' / 'extra.flac').write_bytes(b'unarchived extra audio')
        before = self.archive_path('Library').read_bytes()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual(before, self.archive_path('Library').read_bytes())
        self.assert_retained()

    def test_completed_receipt_rejects_later_source_and_provenance_changes(self):
        self.delivery()
        self.finish()
        provenance = deepcopy(self.provenance_before)
        provenance['transaction_id'] = '0' * 32
        sort._write_staging_provenance(self.name, provenance)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual(self.inbox_before, sort.file_manifest(self.inbox))
        self.assertEqual(self.stage_before, sort.file_manifest(self.stage))

    def corrupt_archive(self, kind, *, remove=False):
        self.delivery(verified=('First', 'Second'))
        self.assertEqual((2, []), self.finish(cleanup=False))
        path = self.archive_path(kind)
        if remove:
            path.unlink()
        else:
            path.write_bytes(b'not a valid archive')
        before = sort.file_manifest(self.backup)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        if kind == 'Originals':
            self.assertFalse(final.is_album_finalized(mixie, self.issues['Second']))
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assert_retained()
        self.assertIsNone(final._state()['active'])
        self.assertEqual(not remove, path.exists())
        self.assertEqual(before, sort.file_manifest(self.backup))

    def test_corrupt_completed_originals_archive_blocks_cleanup_without_overwrite(self):
        self.corrupt_archive('Originals')

    def test_corrupt_completed_library_archive_blocks_cleanup_without_overwrite(self):
        self.corrupt_archive('Library')

    def test_missing_completed_archive_blocks_cleanup_without_recreation(self):
        self.corrupt_archive('Originals', remove=True)

    def test_interrupted_archive_pair_reuses_original_zip_before_finalized_receipt(self):
        self.delivery()
        real = final._write_zip
        def fail_library(kind, *args):
            if kind == 'Library':
                raise OSError('synthetic archive disk full')
            return real(kind, *args)
        with patch.object(final, '_write_zip', side_effect=fail_library):
            count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual({}, final._state()['finalized'])
        self.assert_retained()
        original = self.archive_path('Originals')
        before = original.read_bytes(), original.stat().st_mtime_ns
        with patch.object(store, '_write_partial', side_effect=AssertionError('duplicate shared payload')), \
             patch.object(store, '_save_journal', side_effect=AssertionError('duplicate delivery journal')):
            self.assertEqual(1, self.finish()[0])
        self.assertEqual(before, (original.read_bytes(), original.stat().st_mtime_ns))
        self.assert_archived_bytes()

    def test_interrupted_finalized_receipt_reuses_both_published_archives(self):
        self.delivery()
        with patch.object(final, '_write', side_effect=OSError('synthetic journal failure')):
            count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual({}, final._state()['finalized'])
        paths = [self.archive_path(kind) for kind in ('Originals', 'Library')]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        with patch.object(store, '_write_partial', side_effect=AssertionError('duplicate shared payload')), \
             patch.object(store, '_save_journal', side_effect=AssertionError('duplicate delivery journal')):
            self.assertEqual(1, self.finish()[0])
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths])
        self.assert_archived_bytes()
        self.assert_retained()

    def test_prepared_cleanup_requires_explicit_authorization_again_on_restart(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared()
        self.assert_retained()
        before = (self.backup / final.LEDGER_NAME).read_bytes()
        with patch.object(final.os, 'rename', side_effect=AssertionError('unauthorized source rename')), \
             patch.object(final.shutil, 'rmtree', side_effect=AssertionError('unauthorized source purge')):
            self.assertFalse(final.resume_cleanup())
            count, errors = final.finish_verified_albums(mixie, self.albums)
        self.assertEqual(0, count)
        self.assertTrue(any('authorization' in error or 'required' in error for error in errors))
        self.assertEqual(before, (self.backup / final.LEDGER_NAME).read_bytes())
        self.assert_retained()
        self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assertFalse(self.source.exists() or self.staged.exists())

    def test_quarantine_interruption_between_original_and_stage_moves_resumes(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared(after_moves=1)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.staged.exists())
        self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual(1, len(final._state()['completed']))

    def test_prepared_cleanup_rechecks_both_archive_pairs_before_any_move(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared()
        self.archive_path('Library', 'Second').write_bytes(b'corrupt second archive')
        with self.assertRaises((RuntimeError, ValueError, OSError, zipfile.BadZipFile)):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assert_retained()
        self.assertEqual('prepared', final._state()['active']['phase'])

    def test_prepared_cleanup_preflights_missing_stage_before_moving_original(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared()
        (self.staged / 'Second' / '02.flac').unlink()
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertTrue(self.source.is_dir())
        self.assertEqual(self.inbox_before, sort.file_manifest(self.inbox))

    def test_partial_file_tree_purge_resumes_from_immutable_manifest(self):
        self.delivery(verified=('First', 'Second'))
        quarantine = self.interrupt_purge(partial=True)
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual(3, len(list((quarantine / 'inbox').rglob('*.flac'))))
        self.assert_archived_bytes('First')
        self.assert_archived_bytes('Second')
        self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assertIsNone(final._state()['active'])
        self.assertEqual(1, len(final._state()['completed']))
        self.assertFalse(quarantine.exists())

    def test_zip_source_purged_before_interrupted_stage_tree_resumes(self):
        self.delivery(verified=('First', 'Second'), zipped=True)
        quarantine = self.interrupt_purge(partial=True)
        self.assertEqual([], list((quarantine / 'inbox').iterdir()))
        self.assertEqual(3, len(list((quarantine / 'stage').rglob('*.flac'))))
        self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assert_archived_bytes('First')
        self.assert_archived_bytes('Second')

    def test_changed_remaining_quarantined_audio_blocks_every_further_purge(self):
        self.delivery(verified=('First', 'Second'))
        quarantine = self.interrupt_purge(partial=True)
        remaining = next((quarantine / 'stage').rglob('*.flac'))
        remaining.write_bytes(b'changed held audio')
        before = sort.file_manifest(quarantine)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, sort.file_manifest(quarantine))
        self.assertIsNotNone(final._state()['active'])

    def test_unexpected_quarantined_audio_blocks_every_further_purge(self):
        self.delivery(verified=('First', 'Second'))
        quarantine = self.interrupt_purge(partial=True)
        (quarantine / 'inbox' / self.source.name / 'unexpected.flac').write_bytes(b'new unknown held audio')
        before = sort.file_manifest(quarantine)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, sort.file_manifest(quarantine))

    def test_unknown_quarantine_sibling_is_not_deleted(self):
        self.delivery(verified=('First', 'Second'))
        quarantine = self.interrupt_purge()
        (quarantine / 'stage' / 'unaccounted.flac').write_bytes(b'unknown quarantine sibling')
        before = sort.file_manifest(quarantine)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, sort.file_manifest(quarantine))

    def test_live_source_reappearance_during_purge_is_never_deleted(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_purge()
        self.source.mkdir()
        (self.source / 'new.flac').write_bytes(b'new delivery under reused name')
        before = sort.file_manifest(self.inbox)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, sort.file_manifest(self.inbox))

    def test_post_purge_receipt_interruption_recovers_without_rearchiving(self):
        self.delivery(verified=('First', 'Second'))
        real = final._write
        def interrupt(state):
            if state['completed'] and state['active'] is None:
                raise OSError('synthetic crash after purge before completion receipt')
            return real(state)
        with patch.object(final, '_write', side_effect=interrupt):
            count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual('purging', final._state()['active']['phase'])
        with patch.object(final, '_write_zip', side_effect=AssertionError('must not archive again')), \
             patch.object(store, 'ensure_archive', side_effect=AssertionError('must not rebuild shared originals')):
            self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))
        self.assertTrue(final.is_album_finalized(mixie, self.issues['Second']))
        self.assertFalse(final.resume_cleanup(allow_source_cleanup=True))

    def test_v1_prepared_whole_source_journal_still_recovers(self):
        self.legacy_completed(shared=True)
        for root in (self.source, self.staged):
            for relative, data in self.original_files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        active = {**deepcopy(self.legacy_transaction), 'phase': 'prepared'}
        final._write({'version': 1, 'active': active, 'completed': {}})
        self.assertNotIn('finalized', json.loads((self.backup / final.LEDGER_NAME).read_bytes()))
        self.assertFalse((self.backup / 'Originals' / 'Shared').exists())
        with patch.object(final, '_write_zip', side_effect=AssertionError('Legacy recovery cannot rearchive')), \
             patch.object(store, 'ensure_archive', side_effect=AssertionError('Legacy recovery cannot migrate')):
            self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual({self.legacy_id: self.legacy_transaction}, final._state()['completed'])
        self.assertEqual({}, final._state()['finalized'])
        self.assertFalse((self.backup / 'Originals' / 'Shared').exists())
        self.assertTrue(all(final.is_album_finalized(mixie, issue) for issue in self.issues.values()))

    def test_truthy_non_boolean_cleanup_values_never_grant_authorization(self):
        self.delivery(verified=('First', 'Second'))
        for value in (1, 'yes', None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    final.finish_verified_albums(mixie, self.albums, allow_source_cleanup=value)
                with self.assertRaises(ValueError):
                    final.resume_cleanup(allow_source_cleanup=value)
        self.assert_retained()
        self.assertFalse((self.backup / 'Originals').exists())

    def test_all_previously_finalized_albums_need_no_archive_work_for_authorized_cleanup(self):
        self.delivery(verified=('First', 'Second'))
        self.assertEqual((2, []), self.finish(cleanup=False))
        with patch.object(final, '_write_zip', side_effect=AssertionError('duplicate archive work')), \
             patch.object(store, 'ensure_archive', side_effect=AssertionError('duplicate shared archive work')):
            self.assertEqual((0, []), self.finish())
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))
        self.assertTrue(final.is_album_finalized(mixie, self.issues['Second']))

    def test_stale_completed_sibling_does_not_block_a_new_verified_album_archive(self):
        self.delivery()
        self.finish()
        (self.library / 'Synthetic Artist' / 'First' / '01 - Song.flac').write_bytes(b'stale first output')
        mixie.record_library_only_import(self.issues['Second'], self.receipts['Second'])
        count, errors = self.finish()
        self.assertEqual(1, count)
        self.assertTrue(any('stale' in error for error in errors))
        self.assert_retained()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertTrue(final.is_album_finalized(mixie, self.issues['Second']))
        self.assert_archived_bytes('Second')
        self.assertIsNone(final._state()['active'])

    def test_stale_completed_source_claim_is_not_success_or_cleanup_authority(self):
        self.delivery(verified=('First', 'Second'))
        self.finish(cleanup=False)
        state = final._state()
        state['finalized'][self.identities['First']]['source_claims'][self.name].pop()
        final._write(state)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(any('stale' in error for error in errors))
        self.assert_retained()
        self.assertIsNone(final._state()['active'])

    def test_malformed_completed_receipt_fails_closed_without_source_mutation(self):
        self.delivery()
        self.finish()
        state = final._state()
        state['finalized'][self.identities['First']] = None
        final._write(state)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        with self.assertRaises(RuntimeError):
            self.finish()
        self.assert_retained()

    def test_pending_finalization_blocks_new_staging_until_authorized_recovery(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared()
        extra = self.inbox / 'Unrelated new delivery'
        extra.mkdir()
        (extra / '01.flac').write_bytes(b'new independent source')
        with self.assertRaisesRegex(RuntimeError, 'Pending finalization'):
            sort.sort_inbox()
        self.assertFalse((self.stage / sort.staging_import_name(extra.name, 'directory')).exists())
        self.assertEqual(self.stage_before, sort.file_manifest(self.stage))
        self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        with redirect_stdout(StringIO()):
            sort.sort_inbox()
        self.assertTrue((self.stage / sort.staging_import_name(extra.name, 'directory')).is_dir())
        self.assertEqual(b'new independent source', (extra / '01.flac').read_bytes())

    def test_retained_shared_source_is_not_restaged_or_reprovenanced(self):
        self.delivery()
        self.finish()
        with patch.object(sort, '_staging_record', side_effect=AssertionError('restaging forbidden')), \
             redirect_stdout(StringIO()):
            self.assertEqual([], sort.sort_inbox())
        self.assert_retained()
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))

    def test_default_resume_during_partial_purge_leaves_quarantine_unchanged(self):
        self.delivery(verified=('First', 'Second'))
        quarantine = self.interrupt_purge(partial=True)
        before = sort.file_manifest(quarantine)
        journal = (self.backup / final.LEDGER_NAME).read_bytes()
        self.assertFalse(final.resume_cleanup())
        self.assertEqual(before, sort.file_manifest(quarantine))
        self.assertEqual(journal, (self.backup / final.LEDGER_NAME).read_bytes())

    def test_independent_sibling_archive_survives_another_album_archive_failure(self):
        self.delivery(verified=('First', 'Second'))
        real = final._write_zip
        def fail_first_library(kind, artist, album, *args):
            if kind == 'Library' and album == 'First':
                raise OSError('synthetic first album archive failure')
            return real(kind, artist, album, *args)
        with patch.object(final, '_write_zip', side_effect=fail_first_library):
            count, errors = self.finish()
        self.assertEqual(1, count)
        self.assertTrue(errors)
        self.assert_retained()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertTrue(final.is_album_finalized(mixie, self.issues['Second']))
        self.assert_archived_bytes('Second')
        self.assertEqual((1, []), self.finish())
        self.assertFalse(self.source.exists() or self.staged.exists())

    def test_changed_zip_container_bytes_with_identical_payload_are_not_cleanup_authority(self):
        self.delivery(verified=('First', 'Second'), zipped=True)
        payload = sort.zip_manifest(self.source)
        with zipfile.ZipFile(self.source, 'a') as saved:
            saved.comment = b'different original container generation'
        self.assertEqual(payload, sort.zip_manifest(self.source))
        self.assertNotEqual(self.original_zip, self.source.read_bytes())
        current = self.source.read_bytes()
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual(current, self.source.read_bytes())
        self.assertEqual(self.stage_before, sort.file_manifest(self.stage))

    def test_source_addition_between_archives_prevents_finalized_receipt_and_cleanup(self):
        self.delivery()
        real = final._write_zip
        def add_audio(kind, *args):
            result = real(kind, *args)
            if kind == 'Library':
                (self.source / 'unexpected.flac').write_bytes(b'arrived during archiving')
            return result
        with patch.object(final, '_write_zip', side_effect=add_audio):
            count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual({}, final._state()['finalized'])
        self.assertIsNone(final._state()['active'])
        self.assertTrue(self.source.is_dir() and self.staged.is_dir())
        self.assertEqual(self.stage_before, sort.file_manifest(self.stage))
        self.assertEqual(b'arrived during archiving', (self.source / 'unexpected.flac').read_bytes())

    def test_library_addition_between_archives_prevents_finalized_receipt_and_cleanup(self):
        self.delivery()
        real = final._write_zip
        def add_audio(kind, *args):
            result = real(kind, *args)
            if kind == 'Library':
                (self.library / 'Synthetic Artist' / 'First' / 'late.flac').write_bytes(b'unarchived output')
            return result
        with patch.object(final, '_write_zip', side_effect=add_audio):
            count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assertEqual({}, final._state()['finalized'])
        self.assertIsNone(final._state()['active'])
        self.assert_retained()

    def test_missing_archive_after_partial_quarantine_blocks_stage_move_and_purge(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared(after_moves=1)
        self.archive_path('Originals', 'Second').unlink()
        before = sort.file_manifest(self.backup / final.QUARANTINE_NAME)
        with self.assertRaises((RuntimeError, OSError)):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, sort.file_manifest(self.backup / final.QUARANTINE_NAME))
        self.assertEqual(self.stage_before, sort.file_manifest(self.stage))
        self.assertTrue(self.staged.is_dir())

    def test_changed_library_output_during_purge_blocks_every_remaining_delete(self):
        self.delivery(verified=('First', 'Second'))
        quarantine = self.interrupt_purge(partial=True)
        output = self.library / 'Synthetic Artist' / 'Second' / '01 - Song.flac'
        output.write_bytes(b'changed corrected output after purge began')
        before = sort.file_manifest(quarantine)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, sort.file_manifest(quarantine))

    def test_legacy_retained_finalized_rows_reuse_old_archives_without_migration(self):
        self.delivery(verified=('First', 'Second'))
        tracks = [track for issue in self.issues.values() for track in issue['tracks']]
        source = final._complete_source(self.name, tracks)
        rows = {}
        for album, issue in self.issues.items():
            fingerprint = self.identities[album]
            entry = mixie._verified_imports()[fingerprint]
            claims = {self.name: sorted(issue['tracks'])}
            files, descriptor = final._archive_entries_original([source])
            descriptor['claimed_audio_sha256'] = {self.name: {
                relative: source['stage_hashes'][relative]
                for relative in (f'{album}/01.flac', f'{album}/02.flac')}}
            original, original_sha, original_entries = final._write_zip(
                'Originals', 'Synthetic Artist', album, files, descriptor)
            library_root = self.library / 'Synthetic Artist' / album
            files, descriptor = final._archive_entries_library(library_root)
            library, library_sha, library_entries = final._write_zip(
                'Library', 'Synthetic Artist', album, files, descriptor)
            row = {'fingerprint': fingerprint, 'import_name': self.name,
                   'tracks': sorted(issue['tracks']), 'import_receipt': deepcopy(entry),
                   'sources': [deepcopy(source)], 'source_claims': claims,
                   'source_cleanup': 'retained_until_whole_source_cleanup',
                   'originals': {'path': original, 'file_sha256': original_sha, 'entries': original_entries},
                   'library': {'path': library, 'file_sha256': library_sha, 'entries': library_entries},
                   'library_root': str(library_root), 'library_hashes': sort.file_manifest(library_root),
                   'receipt': dict(entry['receipt'])}
            final._validate_archives(row)
            rows[fingerprint] = row
        final._write({'version': 1, 'active': None, 'completed': {}, 'finalized': rows})
        journal = (self.backup / final.LEDGER_NAME).read_bytes()
        archives = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in self.backup.rglob('*.zip')}
        with patch.object(final, '_write_zip', side_effect=AssertionError('Cannot recreate old archives')), \
             patch.object(store, 'ensure_archive', side_effect=AssertionError('Cannot migrate old archives')):
            self.assertTrue(all(final.is_album_finalized(mixie, issue) for issue in self.issues.values()))
            self.assertEqual((0, []), self.finish(cleanup=False))
            self.assert_retained()
            self.assertEqual(journal, (self.backup / final.LEDGER_NAME).read_bytes())
            self.assertEqual((0, []), self.finish())
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual(rows, final._state()['finalized'])
        self.assertTrue(all(final.is_album_finalized(mixie, issue) for issue in self.issues.values()))
        self.assertFalse((self.backup / 'Originals' / 'Shared').exists())
        self.assertEqual(archives, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                    for path in self.backup.rglob('*.zip')})

    def test_legacy_single_completed_transaction_is_recognized_without_migration(self):
        self.legacy_completed()
        journal = (self.backup / final.LEDGER_NAME).read_bytes()
        receipts = (self.script / '.verified_imports.json').read_bytes()
        self.assertNotIn('finalized', json.loads(journal))
        self.assertFalse(mixie.is_library_import_verified(self.issues['First']))
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))
        self.assertEqual(journal, (self.backup / final.LEDGER_NAME).read_bytes())
        self.assertEqual(receipts, (self.script / '.verified_imports.json').read_bytes())
        self.assertEqual({}, final._state()['finalized'])

    def test_legacy_duplicate_source_transaction_recognizes_its_independently_mapped_owner(self):
        self.delivery(album_names=('First',))
        (self.script / '.mixie-import-first.json').write_text(json.dumps({
            'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')
        first = final._complete_source(self.name, self.issues['First']['tracks'])
        duplicate = self.inbox / 'Duplicate delivery'
        shutil.copytree(self.source, duplicate)
        with redirect_stdout(StringIO()):
            sort.sort_inbox()
        duplicate_name = sort.staging_import_name(duplicate.name, 'directory')
        tracks = [f'{duplicate_name}/First/{number:02d}.flac' for number in (1, 2)]
        second = final._complete_source(duplicate_name, tracks)
        files, descriptor = final._archive_entries_original([first, second])
        original, original_sha, original_entries = final._write_zip(
            'Originals', 'Synthetic Artist', 'First', files, descriptor)
        files, descriptor = final._archive_entries_library(self.library / 'Synthetic Artist' / 'First')
        library, library_sha, library_entries = final._write_zip(
            'Library', 'Synthetic Artist', 'First', files, descriptor)
        row = {'originals': {'path': original, 'file_sha256': original_sha, 'entries': original_entries},
               'library': {'path': library, 'file_sha256': library_sha, 'entries': library_entries},
               'receipt': dict(self.receipts['First'])}
        self.legacy_rows = [row]
        self.legacy_transaction = {'sources': [first, second], **row}
        self.repin_legacy()
        for source in (first, second):
            shutil.rmtree(source['source'])
            shutil.rmtree(source['stage'])
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))
        self.assertFalse(final.is_album_finalized(mixie, {'import_name': duplicate_name, 'tracks': tracks}))
        self.assertEqual((0, []), final.finish_verified_albums(mixie, {}))
        with zipfile.ZipFile(original) as saved:
            members = [name for name in saved.namelist() if name.endswith('.flac')]
            self.assertEqual(4, len(members))

    def test_legacy_single_original_zip_completed_transaction_is_recognized(self):
        self.legacy_completed(zipped=True)
        self.assertTrue(final.is_album_finalized(mixie, self.issues['First']))
        with zipfile.ZipFile(self.archive_path('Originals')) as saved:
            member = next(name for name in saved.namelist() if name.endswith('/Shared delivery.zip'))
            self.assertEqual(self.original_zip, saved.read(member))

    def test_legacy_shared_directory_partitions_are_recognized_for_both_albums(self):
        self.legacy_completed(shared=True)
        for album in self.issues:
            self.assertTrue(final.is_album_finalized(mixie, self.issues[album]))
        with zipfile.ZipFile(self.archive_path('Originals', 'Second')) as saved:
            self.assertFalse(any('/First/' in name or name.endswith('/README.txt') for name in saved.namelist()))
            for number in (1, 2):
                member = next(name for name in saved.namelist() if name.endswith(f'/Second/{number:02d}.flac'))
                self.assertEqual(f'untouched Second {number}'.encode(), saved.read(member))

    def test_legacy_shared_zip_archives_retain_and_verify_exact_container_bytes(self):
        self.legacy_completed(shared=True, zipped=True)
        for album in self.issues:
            self.assertTrue(final.is_album_finalized(mixie, self.issues[album]))
            with zipfile.ZipFile(self.archive_path('Originals', album)) as saved:
                member = next(name for name in saved.namelist() if name.endswith('/Shared delivery.zip'))
                self.assertEqual(self.original_zip, saved.read(member))

    def test_legacy_stale_diagnostics_retire_without_rearchive_reimport_or_ingestion(self):
        self.legacy_completed(shared=True)
        stale = [{**deepcopy(issue), 'status': 'needs_review', 'reason': 'old interrupted diagnostics'}
                 for issue in self.issues.values()]
        mixie.save_review_queue(stale)
        journal = (self.backup / final.LEDGER_NAME).read_bytes()
        protected = sort.file_manifest(self.backup)
        with patch.object(final, '_write_zip', side_effect=AssertionError('Duplicate archive forbidden')), \
             patch.object(sort, 'sort_inbox', side_effect=AssertionError('New ingestion forbidden')), \
             patch.object(tag, 'group_staged_albums', side_effect=AssertionError('Metadata grouping forbidden')), \
             patch.object(mixie, 'preview_album', side_effect=AssertionError('Providers forbidden')), \
             patch.object(mixie, 'test_album_plan', side_effect=AssertionError('Duplicate import forbidden')), \
             patch.object(mixie, 'test_review_decision', side_effect=AssertionError('Duplicate review forbidden')), \
             patch.object(mixie, 'get_acoustid_client_key', side_effect=AssertionError('Provider key forbidden')), \
             redirect_stdout(StringIO()):
            self.assertEqual([], mixie.reconcile_review_queue(stale))
            self.assertEqual((0, []), final.finish_verified_albums(mixie, {}))
            self.assertEqual((0, []), mixie.finalize_verified_imports())
            self.assertEqual([], mixie.load_review_queue())
            self.assertEqual([], mixie.reconcile_review_queue(stale))
        self.assertEqual(journal, (self.backup / final.LEDGER_NAME).read_bytes())
        self.assertEqual(protected, sort.file_manifest(self.backup))

    def test_legacy_missing_originals_zip_is_not_finalized(self):
        self.legacy_completed()
        self.archive_path('Originals').unlink()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_corrupt_originals_zip_is_not_finalized(self):
        self.legacy_completed()
        self.archive_path('Originals').write_bytes(b'corrupted historical Originals ZIP')
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_missing_library_zip_is_not_finalized(self):
        self.legacy_completed()
        self.archive_path('Library').unlink()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_corrupt_library_zip_is_not_finalized(self):
        self.legacy_completed()
        self.archive_path('Library').write_bytes(b'corrupted historical Library ZIP')
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_shared_sibling_archive_failure_invalidates_whole_historical_proof(self):
        self.legacy_completed(shared=True)
        self.archive_path('Originals', 'Second').unlink()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertFalse(final.is_album_finalized(mixie, self.issues['Second']))

    def test_legacy_changed_library_output_is_not_finalized(self):
        self.legacy_completed()
        path = self.library / 'Synthetic Artist' / 'First' / '01 - Song.flac'
        path.write_bytes(b'user changed corrected output')
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertEqual(b'user changed corrected output', path.read_bytes())

    def test_legacy_missing_library_output_is_not_finalized(self):
        self.legacy_completed()
        (self.library / 'Synthetic Artist' / 'First' / '01 - Song.flac').unlink()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_unarchived_library_audio_is_not_finalized(self):
        self.legacy_completed()
        (self.library / 'Synthetic Artist' / 'First' / 'unexpected.flac').write_bytes(b'unarchived new audio')
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_symlink_output_cannot_validate_historical_proof(self):
        self.legacy_completed()
        output = self.library / 'Synthetic Artist' / 'First' / '01 - Song.flac'
        held = self.root / 'retained output.flac'
        output.rename(held)
        output.symlink_to(held)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_missing_committed_provenance_is_not_finalized(self):
        self.legacy_completed()
        sort._provenance_path(self.name).unlink()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_changed_committed_provenance_is_not_finalized(self):
        self.legacy_completed()
        provenance = deepcopy(self.provenance_before)
        provenance['transaction_id'] = '0' * 32
        sort._write_staging_provenance(self.name, provenance)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_missing_independent_import_receipt_is_not_finalized(self):
        self.legacy_completed()
        (self.script / '.verified_imports.json').unlink()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_wrong_input_mapping_is_not_finalized(self):
        self.legacy_completed()
        path = self.script / '.verified_imports.json'
        state = json.loads(path.read_text())
        entry = state['entries'][self.identities['First']]
        entry['input_map'][self.issues['First']['tracks'][0]]['source_sha256'] = '0' * 64
        path.write_text(json.dumps(state))
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_wrong_import_fingerprint_is_not_finalized(self):
        self.legacy_completed()
        path = self.script / '.verified_imports.json'
        state = json.loads(path.read_text())
        entry = state['entries'].pop(self.identities['First'])
        state['entries']['0' * 64] = entry
        path.write_text(json.dumps(state))
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_import_only_receipt_and_missing_sources_do_not_prove_purge(self):
        self.legacy_completed()
        final._write({'version': 1, 'active': None, 'completed': {}})
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_wrong_completed_transaction_identity_is_not_finalized(self):
        self.legacy_completed()
        self.legacy_transaction['id'] = '0' * 64
        final._write({'version': 1, 'active': None, 'completed': {'0' * 64: self.legacy_transaction}})
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_reappeared_source_is_not_assumed_purged(self):
        self.legacy_completed()
        self.source.mkdir()
        (self.source / 'new.flac').write_bytes(b'new source generation')
        before = sort.file_manifest(self.inbox)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertEqual(before, sort.file_manifest(self.inbox))

    def test_legacy_reappeared_stage_is_not_assumed_purged(self):
        self.legacy_completed()
        self.staged.mkdir()
        (self.staged / 'new.flac').write_bytes(b'new staged generation')
        before = sort.file_manifest(self.stage)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertEqual(before, sort.file_manifest(self.stage))

    def test_legacy_leftover_quarantine_cannot_masquerade_as_completed_purge(self):
        self.legacy_completed()
        held = self.backup / final.QUARANTINE_NAME / self.legacy_id
        held.mkdir(parents=True)
        (held / 'unpurged.flac').write_bytes(b'held recovery audio')
        before = sort.file_manifest(held)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertEqual(before, sort.file_manifest(held))

    def test_real_legacy_prepared_shape_resumes_and_preserves_historical_recognition(self):
        self.legacy_completed(shared=True)
        for root in (self.source, self.staged):
            for relative, data in self.original_files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        active = {**deepcopy(self.legacy_transaction), 'phase': 'prepared'}
        final._write({'version': 1, 'active': active, 'completed': {}})
        before = sort.file_manifest(self.inbox), sort.file_manifest(self.stage)
        self.assertFalse(final.resume_cleanup())
        self.assertEqual(before, (sort.file_manifest(self.inbox), sort.file_manifest(self.stage)))
        with patch.object(final, '_write_zip', side_effect=AssertionError('Recovery cannot recreate archives')):
            self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual({}, final._state()['finalized'])
        self.assertEqual(self.legacy_transaction, final._state()['completed'][self.legacy_id])
        self.assertTrue(all(final.is_album_finalized(mixie, issue) for issue in self.issues.values()))

    def test_legacy_active_transaction_does_not_authorize_historical_success(self):
        self.legacy_completed()
        state = final._state()
        state['active'] = {'id': self.legacy_id, 'phase': 'purging', **self.legacy_transaction}
        final._write(state)
        before = (self.backup / final.LEDGER_NAME).read_bytes()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertEqual(before, (self.backup / final.LEDGER_NAME).read_bytes())

    def test_legacy_self_consistent_original_zip_with_wrong_bytes_is_not_proof(self):
        self.legacy_completed()
        self.rewrite_legacy_archive('originals', change_bytes=True)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_self_consistent_library_zip_with_wrong_bytes_is_not_proof(self):
        self.legacy_completed()
        self.rewrite_legacy_archive('library', change_bytes=True)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_partition_missing_sibling_audio_is_not_complete_historical_proof(self):
        self.legacy_completed(shared=True)
        self.rewrite_legacy_archive('originals', row=1, remove_audio=True)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertFalse(final.is_album_finalized(mixie, self.issues['Second']))

    def test_legacy_original_zip_wrong_delivery_identity_is_not_proof(self):
        self.legacy_completed()
        self.rewrite_legacy_archive('originals', wrong_identity=True)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_library_zip_wrong_album_identity_is_not_proof(self):
        self.legacy_completed()
        self.rewrite_legacy_archive('library', wrong_identity=True)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_legacy_noncanonical_archive_path_cannot_alias_valid_historical_proof(self):
        self.legacy_completed()
        record = self.legacy_rows[0]['originals']
        path = Path(record['path'])
        record['path'] = str(path.parent / '..' / path.parent.name / path.name)
        self.repin_legacy()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def test_damaged_modern_completed_receipt_cannot_downgrade_to_legacy_proof(self):
        self.delivery(verified=('First', 'Second'))
        self.assertEqual((2, []), self.finish())
        state = final._state()
        state.pop('finalized')
        final._write(state)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        self.assertFalse(final.is_album_finalized(mixie, self.issues['Second']))

    def test_legacy_partial_or_duplicate_issue_tracks_cannot_alias_completed_album(self):
        self.legacy_completed()
        issue = self.issues['First']
        for tracks in (issue['tracks'][:1], [issue['tracks'][0], issue['tracks'][0]]):
            with self.subTest(tracks=tracks):
                self.assertFalse(final.is_album_finalized(mixie, {**issue, 'tracks': tracks}))

    def test_malformed_legacy_archive_receipt_returns_false_not_success(self):
        self.legacy_completed()
        self.legacy_transaction['receipt'] = []
        self.repin_legacy()
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))

    def forged_modern_archive(self, kind, *, repin_descriptor=True):
        self.delivery()
        self.assertEqual(1, self.finish(cleanup=False)[0])
        state = final._state()
        row = state['finalized'][self.identities['First']]
        if kind == 'originals':
            reference = row['originals']['deliveries'][self.name]
            record = reference['archive']
        else:
            record = row['library']
        path = Path(record['path'])
        with zipfile.ZipFile(path) as saved:
            members = saved.infolist()
            descriptor = json.loads(saved.read(final.MANIFEST_NAME))
            contents = {member.filename: saved.read(member) for member in members}
        member = next(name for name in contents if name.endswith('.flac'))
        contents[member] = b'unrelated unverified archived audio'
        digest = hashlib.sha256(contents[member]).hexdigest()
        if repin_descriptor:
            descriptor['sha256'][member] = digest
            if kind == 'originals':
                descriptor['payload_sha256'][member.removeprefix('files/')] = digest
        contents[final.MANIFEST_NAME] = canonical(descriptor)
        forged = self.backup / 'forged-shared-original.zip' if kind == 'originals' else path
        with zipfile.ZipFile(forged, 'w') as saved:
            for info in members:
                saved.writestr(info, contents[info.filename])
        if kind == 'originals':
            digest = sha(forged)
            identity = hashlib.sha256(canonical(descriptor)).hexdigest()
            destination = self.backup / 'Originals' / 'Shared' / 'sha256' / digest[:2] / f'{digest}.zip'
            destination.parent.mkdir(parents=True, exist_ok=True)
            forged.rename(destination)
            record = {'version': 1, 'object_id': identity, 'path': str(destination),
                      'file_sha256': digest, 'entries': {**descriptor['sha256'],
                      store.MANIFEST_NAME: hashlib.sha256(canonical(descriptor)).hexdigest()}}
            reference['archive'] = record
            journal = self.backup / 'Originals' / 'Shared' / 'deliveries' / f'{identity}.json'
            journal.write_bytes(canonical({'version': 1, 'object_id': identity, 'descriptor': descriptor,
                                          'state': 'published', 'archive': record}))
            source, = row['sources']
            if repin_descriptor:
                # This is a valid CAS object for different bytes, not a broken ZIP.
                # The album must still reject its reference against immutable provenance.
                forged_source = deepcopy(source)
                forged_source['source_hashes'] = dict(descriptor['payload_sha256'])
                forged_source['stage_hashes'] = dict(descriptor['payload_sha256'])
                self.assertEqual(descriptor, store.validate_archive(self.backup, forged_source, record))
                self.assertEqual(record['entries'], sort.zip_manifest(destination))
            with self.assertRaises(RuntimeError):
                store.validate_archive(self.backup, source, record)
        else:
            record.update(file_sha256=sha(path), entries=sort.zip_manifest(path))
            self.assertEqual(descriptor, final._archive_descriptor(record, 'corrected_library'))
        final._write(state)
        before = sort.file_manifest(self.backup)
        self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
        count, errors = self.finish()
        self.assertEqual(0, count)
        self.assertTrue(errors)
        self.assert_retained()
        self.assertIsNone(final._state()['active'])
        self.assertEqual(before, sort.file_manifest(self.backup))

    def test_modern_original_zip_cannot_self_consistently_claim_unrelated_source_bytes(self):
        self.forged_modern_archive('originals')

    def test_modern_library_zip_cannot_self_consistently_claim_unrelated_output_bytes(self):
        self.forged_modern_archive('library')

    def test_modern_shared_original_manifest_pins_cannot_hide_unrelated_payload(self):
        self.forged_modern_archive('originals', repin_descriptor=False)

    def test_shared_original_claim_hashes_are_exact_and_cannot_alias_a_sibling_album(self):
        self.delivery(verified=('First', 'Second'))
        self.assertEqual((2, []), self.finish(cleanup=False))
        baseline = final._state()
        for damage in ('sibling_claims', 'wrong_sha256', 'missing_track'):
            with self.subTest(damage=damage):
                state = deepcopy(baseline)
                first = state['finalized'][self.identities['First']]['originals']['deliveries'][self.name]
                second = state['finalized'][self.identities['Second']]['originals']['deliveries'][self.name]
                if damage == 'sibling_claims':
                    first['claimed_audio_sha256'] = deepcopy(second['claimed_audio_sha256'])
                elif damage == 'wrong_sha256':
                    first['claimed_audio_sha256']['First/01.flac'] = '0' * 64
                else:
                    first['claimed_audio_sha256'].pop('First/01.flac')
                final._write(state)
                source, = state['finalized'][self.identities['First']]['sources']
                store.validate_archive(self.backup, source, first['archive'])
                before = sort.file_manifest(self.backup)
                self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
                self.assertTrue(final.is_album_finalized(mixie, self.issues['Second']))
                count, errors = self.finish()
                self.assertEqual(0, count)
                self.assertTrue(errors)
                self.assert_retained()
                self.assertIsNone(final._state()['active'])
                self.assertEqual(before, sort.file_manifest(self.backup))

    def test_shared_original_reference_requires_exact_deliveries_and_record_pins(self):
        self.delivery()
        self.assertEqual(1, self.finish(cleanup=False)[0])
        baseline = final._state()
        for damage in ('missing_delivery', 'extra_delivery', 'object_id', 'path', 'file_sha256', 'entries'):
            with self.subTest(damage=damage):
                state = deepcopy(baseline)
                originals = state['finalized'][self.identities['First']]['originals']
                reference = originals['deliveries'][self.name]
                record = reference['archive']
                if damage == 'missing_delivery':
                    originals['deliveries'].pop(self.name)
                elif damage == 'extra_delivery':
                    originals['deliveries']['unrelated-import'] = deepcopy(reference)
                elif damage == 'path':
                    path = Path(record['path'])
                    record['path'] = str(path.parent / '..' / path.parent.name / path.name)
                elif damage == 'entries':
                    record['entries'].pop(next(name for name in record['entries'] if name.endswith('.flac')))
                else:
                    record[damage] = '0' * 64
                final._write(state)
                before = sort.file_manifest(self.backup)
                self.assertFalse(final.is_album_finalized(mixie, self.issues['First']))
                count, errors = self.finish()
                self.assertEqual(0, count)
                self.assertTrue(errors)
                self.assert_retained()
                self.assertIsNone(final._state()['active'])
                self.assertEqual(before, sort.file_manifest(self.backup))

    def test_shared_original_journal_loss_or_tampering_blocks_cleanup_without_repair(self):
        self.delivery(verified=('First', 'Second'))
        self.assertEqual((2, []), self.finish(cleanup=False))
        row = final._state()['finalized'][self.identities['First']]
        source, = row['sources']
        record = row['originals']['deliveries'][self.name]['archive']
        journal = self.shared_journal_path(source)
        original = journal.read_bytes()
        for damage in ('missing', 'descriptor', 'archive_pin'):
            with self.subTest(damage=damage):
                if damage == 'missing':
                    journal.unlink()
                else:
                    changed = json.loads(original)
                    if damage == 'descriptor':
                        changed['descriptor']['payload_sha256']['First/01.flac'] = '0' * 64
                    else:
                        changed['archive']['file_sha256'] = '0' * 64
                    journal.write_bytes(canonical(changed))
                before = sort.file_manifest(self.backup)
                with self.assertRaises(RuntimeError):
                    store.validate_archive(self.backup, source, record)
                with patch.object(final, '_write_zip', side_effect=AssertionError('Cannot repair archives')), \
                     patch.object(store, '_save_journal', side_effect=AssertionError('Cannot repair journals')):
                    for issue in self.issues.values():
                        self.assertFalse(final.is_album_finalized(mixie, issue))
                    count, errors = self.finish()
                self.assertEqual(0, count)
                self.assertTrue(errors)
                self.assert_retained()
                self.assertIsNone(final._state()['active'])
                self.assertEqual(before, sort.file_manifest(self.backup))
                journal.write_bytes(original)
        self.assertTrue(all(final.is_album_finalized(mixie, issue) for issue in self.issues.values()))

    def test_archive_storage_inside_inputs_is_rejected_before_creating_any_directory(self):
        import archive
        self.delivery()
        library = sort.file_manifest(self.library)
        for root in (self.source, self.staged, self.library / 'Synthetic Artist' / 'First'):
            destination = root / 'must-not-create-archives'
            with self.subTest(root=root), patch.object(archive, 'backup_folder', destination):
                with self.assertRaisesRegex(RuntimeError, 'Archive storage cannot be inside'):
                    self.finish()
                self.assertFalse(destination.exists())
            self.assert_retained()
            self.assertEqual(library, sort.file_manifest(self.library))

    def test_complete_source_hashes_each_physical_snapshot_once_per_verification(self):
        self.delivery(verified=('First', 'Second'))
        tracks = [track for issue in self.issues.values() for track in issue['tracks']]
        with patch.object(sort, 'file_manifest', wraps=sort.file_manifest) as manifest:
            source = final._complete_source(self.name, tracks)
        calls = [Path(call.args[0]) for call in manifest.call_args_list]
        self.assertEqual(1, calls.count(self.source))
        self.assertEqual(1, calls.count(self.staged))
        self.assertEqual(self.provenance_before['payload_hashes'], source['stage_hashes'])
        self.assertEqual(self.provenance_before['payload_hashes'], source['source_hashes'])

    def test_verified_entry_does_not_rescan_all_staged_siblings_twice(self):
        self.delivery()
        with patch.object(mixie, 'completion_source_manifest', wraps=mixie.completion_source_manifest) as manifest:
            pair = final._verified_entry(mixie, self.issues['First'], mixie._verified_imports())
        self.assertEqual(1, manifest.call_count)
        self.assertEqual(self.identities['First'], pair[0])
        self.assertEqual(dict(self.receipts['First']), pair[1]['receipt'])
        output = self.library / 'Synthetic Artist' / 'First' / '01 - Song.flac'
        output.write_bytes(b'changed library output')
        with self.assertRaisesRegex(RuntimeError, 'receipt is stale'):
            final._verified_entry(mixie, self.issues['First'], mixie._verified_imports())

    def test_nineteen_shared_receipts_recover_selectively_then_clean_only_when_all_resolve(self):
        names = tuple(f'Album {number:02d}' for number in range(1, 20))
        verified = names[:10]
        self.delivery(verified=verified, album_names=names)
        with patch.object(sort, 'sort_inbox', side_effect=AssertionError('Ingestion forbidden')), \
             patch.object(tag, 'group_staged_albums', side_effect=AssertionError('Grouping forbidden')), \
             patch.object(mixie, 'preview_album', side_effect=AssertionError('Providers forbidden')):
            count, errors = self.finish(albums={})
        self.assertEqual(10, count)
        self.assertTrue(any('unaccounted' in error for error in errors))
        self.assert_retained()
        for album in verified:
            self.assertTrue(final.is_album_finalized(mixie, self.issues[album]))
            self.assert_archived_bytes(album)
        for album in names[10:]:
            self.assertNotIn(self.identities[album], final._state()['finalized'])
            self.assertFalse((self.backup / 'Originals' / 'Synthetic Artist' / (album + '.zip')).exists())
            self.assertFalse(self.archive_path('Library', album).exists())
            mixie.record_library_only_import(self.issues[album], self.receipts[album])
        self.assertEqual((9, []), self.finish(albums={}))
        self.assertFalse(self.source.exists() or self.staged.exists())
        self.assertEqual(19, len(final._state()['finalized']))
        self.assertEqual(1, len(final._state()['completed']))
        self.assertTrue(all(final.is_album_finalized(mixie, self.issues[album]) for album in names))
        self.assert_archived_bytes(names[-1])

    def test_prepared_cleanup_detects_late_extra_audio_before_any_source_move(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared()
        (self.source / 'late.flac').write_bytes(b'late unaccounted source audio')
        before = sort.file_manifest(self.inbox), sort.file_manifest(self.stage)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, (sort.file_manifest(self.inbox), sort.file_manifest(self.stage)))

    def test_prepared_held_audio_change_blocks_remaining_stage_move(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared(after_moves=1)
        txn = final._state()['active']
        held = self.backup / final.QUARANTINE_NAME / txn['id']
        next((held / 'inbox').rglob('*.flac')).write_bytes(b'changed held original audio')
        before = sort.file_manifest(held), sort.file_manifest(self.stage)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, (sort.file_manifest(held), sort.file_manifest(self.stage)))

    def test_unknown_active_phase_fails_before_any_source_or_quarantine_mutation(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared()
        state = final._state()
        state['active']['phase'] = 'unrecognized-phase'
        final._write(state)
        before = sort.file_manifest(self.backup), sort.file_manifest(self.inbox), sort.file_manifest(self.stage)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assertEqual(before, (sort.file_manifest(self.backup), sort.file_manifest(self.inbox),
                                  sort.file_manifest(self.stage)))

    def test_incomplete_active_audio_claims_block_cleanup_before_any_move(self):
        self.delivery(verified=('First', 'Second'))
        self.interrupt_prepared()
        state = final._state()
        state['active']['albums'][0]['source_claims'][self.name].pop()
        final._write(state)
        with self.assertRaises(RuntimeError):
            final.resume_cleanup(allow_source_cleanup=True)
        self.assert_retained()
