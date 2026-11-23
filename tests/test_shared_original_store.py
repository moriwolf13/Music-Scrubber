"""Low-level physical-delivery fixtures; run through tests/run_isolated.py only."""
import hashlib
import json
import os
import shutil
import stat
import warnings
import zipfile
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import mixie_shared_originals as store
import sort
from support import WorkspaceCase


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SharedOriginalStoreTests(WorkspaceCase):
    def fixture(self, kind='directory', outer=None, payload=None):
        number = len(list(self.stage.iterdir()))
        outer = outer or {'directory': f'download-{number}', 'zip': f'download-{number}.zip',
                          'file': f'track-{number}.flac'}[kind]
        payload = payload if payload is not None else {
            'Album A/01.flac': b'original album one',
            'Album B/02.flac': b'original album two',
            'Cover.JPG': b'upper-case art', 'cover.jpg': b'lower-case art',
            'extras/notes.txt': b'complete delivery notes',
            store.MANIFEST_NAME: b'original vendor manifest, not the wrapper manifest',
        }
        original, staged = self.inbox / outer, self.stage / f'import-{number}'
        staged.mkdir()
        if kind == 'file':
            payload = {outer: b'untouched single audio file'}
            original.write_bytes(payload[outer])
        elif kind == 'directory':
            original.mkdir()
            for relative, data in payload.items():
                target = original / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        else:
            with zipfile.ZipFile(original, 'w', compression=zipfile.ZIP_DEFLATED) as saved:
                saved.comment = b'raw ZIP metadata must survive, too'
                saved.writestr(zipfile.ZipInfo('empty-directory/', (2001, 2, 3, 4, 5, 6)), b'')
                for relative, data in payload.items():
                    info = zipfile.ZipInfo(relative, (2001, 2, 3, 4, 5, 6))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    saved.writestr(info, data)
        for relative, data in payload.items():
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        hashes = {relative: sha(data) for relative, data in payload.items()}
        return {'name': staged.name, 'source_name': outer, 'source_kind': kind,
                'source_hashes': hashes.copy() if kind == 'directory' else {outer: sha(original.read_bytes())},
                'stage_hashes': hashes, 'source': str(original), 'stage': str(staged),
                'provenance': {'transaction_id': f'volatile-transaction-{number}',
                               'artist': 'volatile artist', 'album': 'volatile album'}}

    def paths(self, source, root=None):
        identity = sha(canonical(store.delivery_descriptor(source)))
        shared = (root or self.backup) / 'Originals' / 'Shared'
        journal = shared / 'deliveries' / f'{identity}.json'
        return journal, journal.with_suffix('.partial')

    def journal(self, source):
        return json.loads(self.paths(source)[0].read_bytes())

    def blobs(self):
        return sorted((self.backup / 'Originals' / 'Shared').rglob('*.zip'))

    def backup_inventory(self):
        result = {}
        for path in (self.backup, *sorted(self.backup.rglob('*'))):
            attributes = path.lstat()
            contents = (path.read_bytes() if stat.S_ISREG(attributes.st_mode)
                        else os.readlink(path) if stat.S_ISLNK(attributes.st_mode) else None)
            result[path.relative_to(self.backup).as_posix()] = (
                attributes.st_mode, attributes.st_ino, attributes.st_nlink, attributes.st_mtime_ns, contents)
        return result

    def assert_clean(self, source, record):
        journal, partial = self.paths(source)
        self.assertEqual([Path(record['path'])], self.blobs())
        self.assertFalse(partial.exists())
        self.assertFalse(journal.with_suffix('.json.tmp').exists())
        self.assertEqual('published', self.journal(source)['state'])
        self.assertEqual(record, self.journal(source)['archive'])
        self.assertEqual(1, len(list(journal.parent.glob('*.json'))))

    def stop_before_write(self, source):
        with (patch.object(store, '_write_partial', side_effect=OSError('interrupted before write')),
              self.assertRaisesRegex(OSError, 'interrupted')):
            store.ensure_archive(self.backup, source)
        self.assertEqual('building', self.journal(source)['state'])

    def stop_after_seal(self, source):
        with (patch.object(store.os, 'link', side_effect=OSError('interrupted before publication')),
              self.assertRaisesRegex(OSError, 'interrupted')):
            store.ensure_archive(self.backup, source)
        self.assertEqual('sealed', self.journal(source)['state'])

    def forged_archive(self, source, descriptor, *, files=None, manifest=None, extra=None):
        """Independently write a canonical-looking blob, receipt, and published index."""
        identity = sha(canonical(descriptor))
        temporary = self.backup / 'fixture-forged.zip'
        if files is None:
            if source['source_kind'] == 'directory':
                files = {f'files/{name}': (Path(source['source']) / name).read_bytes()
                         for name in source['source_hashes']}
            else:
                files = {'original.zip' if source['source_kind'] == 'zip' else 'original':
                         Path(source['source']).read_bytes()}
        contents = {**files, store.MANIFEST_NAME: canonical(manifest or descriptor), **(extra or {})}
        with zipfile.ZipFile(temporary, 'w') as saved:
            for name, data in sorted(contents.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                saved.writestr(info, data)
        digest = sha(temporary.read_bytes())
        shared = self.backup / 'Originals' / 'Shared'
        destination = shared / 'sha256' / digest[:2] / f'{digest}.zip'
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(destination)
        # Pins deliberately remain the expected inventory, not whatever the ZIP says.
        entries = {**descriptor['sha256'], store.MANIFEST_NAME: sha(canonical(descriptor))}
        record = {'version': 1, 'object_id': identity, 'path': str(destination),
                  'file_sha256': digest, 'entries': entries}
        journal = shared / 'deliveries' / f'{identity}.json'
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_bytes(canonical({'version': 1, 'object_id': identity, 'descriptor': descriptor,
                                      'state': 'published', 'archive': record}))
        return record

    def test_actual_zip_bytes_address_the_blob_and_record_inventory(self):
        source = self.fixture()
        record = store.ensure_archive(self.backup, source)
        descriptor = store.delivery_descriptor(source)
        blob = Path(record['path'])
        digest = sha(blob.read_bytes())
        self.assertEqual({'version', 'object_id', 'path', 'file_sha256', 'entries'}, set(record))
        self.assertEqual(1, record['version'])
        self.assertEqual(sha(canonical(descriptor)), record['object_id'])
        self.assertEqual(digest, record['file_sha256'])
        self.assertEqual(self.backup / 'Originals/Shared/sha256' / digest[:2] / f'{digest}.zip', blob)
        with zipfile.ZipFile(blob) as saved:
            self.assertEqual(record['entries'], {name: sha(saved.read(name)) for name in saved.namelist()})
            self.assertEqual(canonical(descriptor), saved.read(store.MANIFEST_NAME))
        self.assertEqual(descriptor, store.validate_archive(self.backup, source, record))
        self.assert_clean(source, record)

    def test_complete_directory_case_extras_and_reserved_vendor_name_survive(self):
        source = self.fixture()
        before = sort.file_manifest(source['source']), sort.file_manifest(source['stage'])
        record = store.ensure_archive(self.backup, source)
        with zipfile.ZipFile(record['path']) as saved:
            self.assertEqual(sorted(record['entries']), saved.namelist())
            for relative in source['source_hashes']:
                self.assertEqual((Path(source['source']) / relative).read_bytes(), saved.read(f'files/{relative}'))
            for member in saved.infolist():
                self.assertEqual(zipfile.ZIP_STORED, member.compress_type)
                self.assertEqual((1980, 1, 1, 0, 0, 0), member.date_time)
                self.assertEqual((stat.S_IFREG | 0o644) << 16, member.external_attr)
        self.assertEqual(before, (sort.file_manifest(source['source']), sort.file_manifest(source['stage'])))

    def test_zip_preserves_complete_raw_container_including_metadata_and_trailing_bytes(self):
        source = self.fixture('zip')
        original = Path(source['source'])
        with original.open('ab') as stream:
            stream.write(b'non-extracted raw vendor trailer')
        source['source_hashes'][source['source_name']] = sha(original.read_bytes())
        record = store.ensure_archive(self.backup, source)
        with zipfile.ZipFile(record['path']) as saved:
            self.assertEqual([store.MANIFEST_NAME, 'original.zip'], saved.namelist())
            self.assertEqual(original.read_bytes(), saved.read('original.zip'))
        self.assertEqual(source['stage_hashes'], store.validate_archive(self.backup, source, record)['payload_sha256'])
        self.assert_clean(source, record)

    def test_zip_retains_known_identical_duplicate_artwork_occurrences(self):
        payload = {'01.flac': b'audio', 'cover.jpg': b'art', 'booklet.pdf': b'booklet'}
        source = self.fixture('zip', payload=payload)
        original = Path(source['source'])
        # Staging collapses identical artwork, but the raw archive keeps every occurrence.
        with warnings.catch_warnings(), zipfile.ZipFile(original, 'a') as saved:
            warnings.simplefilter('ignore', UserWarning)
            for _ in range(3):
                for name in ('cover.jpg', 'booklet.pdf'):
                    saved.writestr(zipfile.ZipInfo(name, (2001, 2, 3, 4, 5, 6)), payload[name])
        raw = original.read_bytes()
        source['source_hashes'][source['source_name']] = sha(raw)
        record = store.ensure_archive(self.backup, source)
        with zipfile.ZipFile(record['path']) as saved:
            archived = saved.read('original.zip')
            self.assertEqual(raw, archived)
            self.assertEqual(sha(raw), record['entries']['original.zip'])
            with zipfile.ZipFile(BytesIO(archived)) as inner:
                for name in ('cover.jpg', 'booklet.pdf'):
                    occurrences = [member for member in inner.infolist() if member.filename == name]
                    self.assertEqual(4, len(occurrences))
                    self.assertTrue(all(inner.read(member) == payload[name] for member in occurrences))
        original.unlink()
        shutil.rmtree(source['stage'])
        self.assertEqual(source['stage_hashes'],
                         store.validate_archive(self.backup, source, record)['payload_sha256'])
        self.assert_clean(source, record)

    def test_conflicting_artwork_or_unsupported_audio_duplicates_are_blocked(self):
        for name, duplicate in (('cover.jpg', b'different art'), ('01.flac', b'audio')):
            with self.subTest(duplicate=name):
                source = self.fixture('zip', payload={'01.flac': b'audio', 'cover.jpg': b'art'})
                original = Path(source['source'])
                with warnings.catch_warnings(), zipfile.ZipFile(original, 'a') as saved:
                    warnings.simplefilter('ignore', UserWarning)
                    saved.writestr(name, duplicate)
                raw = original.read_bytes()
                source['source_hashes'][source['source_name']] = sha(raw)
                with self.assertRaisesRegex(ValueError, 'duplicate ZIP entry'):
                    store.ensure_archive(self.backup, source)
                journal, partial = self.paths(source)
                self.assertFalse(journal.exists() or partial.exists())
                # Even a self-consistent wrapper/index cannot bless the unsafe raw ZIP.
                record = self.forged_archive(source, store.delivery_descriptor(source))
                before = journal.read_bytes(), Path(record['path']).read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'duplicate ZIP entry'):
                    store.validate_archive(self.backup, source, record)
                self.assertEqual(before, (journal.read_bytes(), Path(record['path']).read_bytes()))
                self.assertEqual(raw, original.read_bytes())

    def test_stored_wrapper_nested_zip_seeks_have_linear_read_volume(self):
        payload = {f'{number:03d}.flac': hashlib.shake_256(str(number).encode()).digest(32768)
                   for number in range(48)}
        source = self.fixture('zip', payload=payload)
        original = Path(source['source'])
        with zipfile.ZipFile(original, 'a') as saved:
            # Index order is legal but opposite to physical member offsets: every
            # nested member needs a backward seek, exposing prefix-replay behavior.
            saved.filelist.reverse()
            saved.comment = b'reverse physical traversal for the bounded seek probe'
        raw_size = original.stat().st_size
        source['source_hashes'][source['source_name']] = sha(original.read_bytes())
        record = store.ensure_archive(self.backup, source)
        read2 = vars(zipfile.ZipExtFile)['_read2']
        bytes_read = 0
        def measured_read(stream, count):
            nonlocal bytes_read
            data = read2(stream, count)
            if stream.name == 'original.zip':
                bytes_read += len(data)
            return data
        with patch.object(zipfile.ZipExtFile, '_read2', measured_read):
            store.validate_archive(self.backup, source, record)
        # Includes the full sequential raw-member SHA256 pass AND inner ZIP reads.
        # No timing assumptions and no multi-gigabyte allocation are needed.
        self.assertGreater(bytes_read, raw_size)
        self.assertLess(bytes_read, raw_size * 4, 'Nested ZIP seeks replayed whole prefixes.')
        print(f'Stored-wrapper seek probe: {bytes_read} bytes read for {raw_size} raw ZIP bytes '
              f'({bytes_read / raw_size:.2f}x), 48 members in reverse physical order')

    def test_single_file_uses_original_member_and_strict_payload(self):
        source = self.fixture('file')
        record = store.ensure_archive(self.backup, source)
        with zipfile.ZipFile(record['path']) as saved:
            self.assertEqual(Path(source['source']).read_bytes(), saved.read('original'))
        self.assertEqual({'original': source['stage_hashes'][source['source_name']]},
                         store.validate_archive(self.backup, source, record)['sha256'])

    def test_repeated_album_calls_reuse_one_blob_without_rewriting_records(self):
        source = self.fixture()
        first = store.ensure_archive(self.backup, source)
        journal, _ = self.paths(source)
        before = journal.read_bytes(), journal.stat().st_mtime_ns, Path(first['path']).stat().st_mtime_ns
        albums = {}
        with (patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')),
              patch.object(store, '_save_journal', side_effect=AssertionError('rewritten index')),
              patch.object(store, '_preflight_new_build', side_effect=AssertionError('known-journal scan'))):
            for album in ('one', 'two', 'three'):
                albums[album] = store.ensure_archive(self.backup, source)
        self.assertTrue(all(record == first for record in albums.values()))
        self.assertEqual(before, (journal.read_bytes(), journal.stat().st_mtime_ns,
                                  Path(first['path']).stat().st_mtime_ns))
        self.assert_clean(source, first)

    def test_independently_renamed_directory_and_zip_deliveries_deduplicate(self):
        for kind in ('directory', 'zip'):
            with self.subTest(kind=kind):
                suffix = '.zip' if kind == 'zip' else ''
                first = self.fixture(kind, outer=f'artist-album first{suffix}')
                second = self.fixture(kind, outer=f'unrelated redelivery{suffix}')
                self.assertNotEqual(first['name'], second['name'])
                self.assertNotEqual(first['provenance'], second['provenance'])
                one = store.ensure_archive(self.backup, first)
                two = store.ensure_archive(self.backup, second)
                self.assertEqual(one, two)
                descriptor = store.validate_archive(self.backup, second, one)
                encoded = canonical(descriptor)
                for volatile in (first['source_name'], second['source_name'], first['name'],
                                 first['source'], 'volatile artist', 'volatile album', 'volatile-transaction'):
                    self.assertNotIn(volatile.encode(), encoded)
        self.assertEqual(2, len(self.blobs()))

    def test_changed_byte_added_file_or_case_inventory_has_new_identity(self):
        original = {'Folder/01.flac': b'one', 'cover.jpg': b'art'}
        variants = [original, {**original, 'Folder/01.flac': b'two'},
                    {**original, 'extras.txt': b'extra'}, {'folder/01.flac': b'one', 'cover.jpg': b'art'}]
        records = []
        for payload in variants:
            source = self.fixture(payload=payload)
            records.append((source, store.ensure_archive(self.backup, source)))
        self.assertEqual(4, len({record['object_id'] for _, record in records}))
        self.assertEqual(4, len({record['file_sha256'] for _, record in records}))
        for source, record in records:
            store.validate_archive(self.backup, source, record)
        self.assertEqual(4, len(self.blobs()))

    def test_changed_raw_zip_metadata_changes_identity_even_if_payload_is_identical(self):
        source = self.fixture('zip')
        one = store.ensure_archive(self.backup, source)
        with zipfile.ZipFile(source['source'], 'a') as saved:
            saved.comment = b'a different complete physical delivery'
        source['source_hashes'][source['source_name']] = sha(Path(source['source']).read_bytes())
        two = store.ensure_archive(self.backup, source)
        self.assertNotEqual(one['object_id'], two['object_id'])
        self.assertNotEqual(one['file_sha256'], two['file_sha256'])
        self.assertTrue(Path(one['path']).exists())

    def test_mtime_and_permissions_do_not_change_built_zip_bytes(self):
        source = self.fixture()
        first = store.ensure_archive(self.backup, source)
        for path in Path(source['source']).rglob('*'):
            if path.is_file():
                os.utime(path, (1000000000, 1000000000))
                path.chmod(0o600)
        other_root = self.script / '.independent-backup'
        second = store.ensure_archive(other_root, source)
        self.assertEqual(first['object_id'], second['object_id'])
        self.assertEqual(first['file_sha256'], second['file_sha256'])
        self.assertEqual(Path(first['path']).read_bytes(), Path(second['path']).read_bytes())

    def test_read_only_validation_after_original_and_stage_purge(self):
        for kind in ('directory', 'zip', 'file'):
            with self.subTest(kind=kind):
                source = self.fixture(kind)
                record = store.ensure_archive(self.backup, source)
                original = Path(source['source'])
                shutil.rmtree(original) if original.is_dir() else original.unlink()
                shutil.rmtree(source['stage'])
                before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.backup.rglob('*') if path.is_file()}
                with (patch.object(store, '_save_journal', side_effect=AssertionError('validation wrote')),
                      patch.object(store, '_write_partial', side_effect=AssertionError('validation built'))):
                    self.assertEqual(store.delivery_descriptor(source),
                                     store.validate_archive(self.backup, source, record))
                self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                          for path in self.backup.rglob('*') if path.is_file()})

    def test_invalid_source_schema_paths_and_pins_are_rejected_before_mkdir(self):
        source = self.fixture()
        mutations = [lambda row: row.pop('provenance'),
                     lambda row: row.update(source_kind='rar'),
                     lambda row: row.update(stage_hashes={'../escape': 'a' * 64}),
                     lambda row: row.update(stage_hashes={'/absolute': 'a' * 64}),
                     lambda row: row.update(stage_hashes={'a//b': 'a' * 64}),
                     lambda row: row.update(stage_hashes={'a\\b': 'a' * 64}),
                     lambda row: row.update(source_hashes={'other.flac': 'a' * 64}),
                     lambda row: row.update(source=str(self.inbox / 'wrong-name')),
                     lambda row: row.update(source=str(self.inbox / 'other' / '..' / row['source_name'])),
                     lambda row: row.update(stage=str(self.library / row['name'])),
                     lambda row: row.update(stage_hashes={'a': 'A' * 64}),
                     lambda row: row.update(stage_hashes={'a': 'a' * 64, 'a/b': 'a' * 64})]
        destination = self.script / 'not-created' / 'backup'
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = deepcopy(source)
                mutate(bad)
                with self.assertRaises(RuntimeError):
                    store.ensure_archive(destination, bad)
                self.assertFalse(destination.parent.exists())

    def test_zip_and_file_descriptor_pin_strictness(self):
        for kind in ('zip', 'file'):
            source = self.fixture(kind)
            bad = deepcopy(source)
            bad['source_hashes'] = {'different.flac': 'a' * 64}
            with self.assertRaises(RuntimeError):
                store.delivery_descriptor(bad)
            if kind == 'file':
                source['stage_hashes']['extra.txt'] = 'b' * 64
                with self.assertRaisesRegex(RuntimeError, 'Single-file'):
                    store.delivery_descriptor(source)

    def test_bad_record_identity_path_and_inventory_cannot_authorize_validation(self):
        source = self.fixture()
        record = store.ensure_archive(self.backup, source)
        for changes in ({'version': 2}, {'version': True}, {'object_id': 'a' * 64},
                        {'path': 'relative.zip'}, {'path': str(self.library / 'wrong.zip')},
                        {'path': str(Path(record['path']).parent / '..' / Path(record['path']).name)},
                        {'file_sha256': 'b' * 64}, {'entries': {}}, {'unexpected': 'field'}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                store.validate_archive(self.backup, source, {**record, **changes})

    def test_self_consistent_different_delivery_record_is_not_expected_source(self):
        source, other = self.fixture(), self.fixture(payload={'other.flac': b'different'})
        record = store.ensure_archive(self.backup, other)
        with self.assertRaisesRegex(RuntimeError, 'identity/path/inventory'):
            store.validate_archive(self.backup, source, record)

    def test_manifest_forgery_with_actual_file_digest_and_published_index_fails(self):
        source = self.fixture()
        descriptor = store.delivery_descriptor(source)
        forged = deepcopy(descriptor)
        forged['payload_sha256'] = {'other.flac': 'a' * 64}
        record = self.forged_archive(source, descriptor, manifest=forged)
        self.assertEqual(record['file_sha256'], sha(Path(record['path']).read_bytes()))
        with self.assertRaisesRegex(RuntimeError, 'manifest|inventory'):
            store.validate_archive(self.backup, source, record)

    def test_matching_forged_zip_manifest_cannot_lie_about_raw_zip_payload(self):
        source = self.fixture('zip')
        source['stage_hashes'] = {'invented.flac': sha(b'invented')}
        descriptor = store.delivery_descriptor(source)
        record = self.forged_archive(source, descriptor)
        with self.assertRaisesRegex(RuntimeError, 'ZIP payload'):
            store.validate_archive(self.backup, source, record)
        with self.assertRaisesRegex(RuntimeError, 'inventory changed'):
            store.ensure_archive(self.backup, source)

    def test_unexpected_or_traversing_wrapper_members_fail_full_inventory(self):
        source = self.fixture()
        for name in ('unclaimed.txt', '../escape.txt', '/absolute.txt'):
            with self.subTest(name=name):
                record = self.forged_archive(source, store.delivery_descriptor(source), extra={name: b'extra'})
                with self.assertRaises(RuntimeError):
                    store.validate_archive(self.backup, source, record)

    def test_corrupt_published_blob_never_gets_an_alternate_or_automatic_repair(self):
        source = self.fixture()
        record = store.ensure_archive(self.backup, source)
        blob, journal = Path(record['path']), self.paths(source)[0]
        original_journal = journal.read_bytes()
        blob.write_bytes(b'damaged published object')
        before = set(self.backup.rglob('*'))
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'Corrupt pinned'):
                store.ensure_archive(self.backup, source)
            with self.assertRaises(RuntimeError):
                store.validate_archive(self.backup, source, record)
        self.assertEqual(before, set(self.backup.rglob('*')))
        self.assertEqual(b'damaged published object', blob.read_bytes())
        self.assertEqual(original_journal, journal.read_bytes())

    def test_missing_published_object_fails_even_with_a_complete_owned_partial(self):
        source = self.fixture()
        record = store.ensure_archive(self.backup, source)
        journal, partial = self.paths(source)
        shutil.copyfile(record['path'], partial)
        Path(record['path']).unlink()
        before = journal.read_bytes(), partial.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Missing published'):
            store.ensure_archive(self.backup, source)
        with self.assertRaises(RuntimeError):
            store.validate_archive(self.backup, source, record)
        self.assertEqual(before, (journal.read_bytes(), partial.read_bytes()))
        self.assertEqual([], self.blobs())

    def test_missing_journal_cannot_be_reconstructed_from_an_unexplained_blob(self):
        source = self.fixture()
        record = store.ensure_archive(self.backup, source)
        journal, partial = self.paths(source)
        journal.unlink()  # Owner damage, never a store operation.
        before = self.backup_inventory()
        with self.assertRaisesRegex(RuntimeError, 'matching published journal'):
            store.validate_archive(self.backup, source, record)
        with (patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')),
              patch.object(store, '_save_journal', side_effect=AssertionError('replacement intent')),
              self.assertRaisesRegex(RuntimeError, 'Unexplained published')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, self.backup_inventory())
        self.assertFalse(journal.exists() or partial.exists())
        self.assertEqual([Path(record['path'])], self.blobs())

    def test_renamed_orphan_preflight_reads_only_bounded_zip_metadata(self):
        payload = {'01.flac': hashlib.shake_256(b'orphan seek probe').digest(2 * 1024 * 1024)}
        fdopen = store.os.fdopen
        for kind in ('directory', 'zip'):
            with self.subTest(kind=kind):
                suffix = '.zip' if kind == 'zip' else ''
                first = self.fixture(kind, outer=f'first delivery{suffix}', payload=payload)
                renamed = self.fixture(kind, outer=f'renamed delivery{suffix}', payload=payload)
                record = store.ensure_archive(self.backup, first)
                journal, partial = self.paths(first)
                journal.unlink()
                self.assertEqual(self.paths(first), self.paths(renamed))
                before = self.backup_inventory()
                bytes_read = 0
                class Meter:
                    def __init__(self, stream):
                        self.stream = stream
                    def __getattr__(self, name):
                        return getattr(self.stream, name)
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        return self.stream.__exit__(*args)
                    def read(self, size=-1):
                        nonlocal bytes_read
                        data = self.stream.read(size)
                        bytes_read += len(data)
                        return data
                def measured_fdopen(*args, **kwargs):
                    return Meter(fdopen(*args, **kwargs))
                for _ in range(2):
                    with (patch.object(store.os, 'fdopen', side_effect=measured_fdopen),
                          patch.object(store, '_digest', side_effect=AssertionError('full archive hash')),
                          patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')),
                          patch.object(store, '_save_journal', side_effect=AssertionError('replacement intent')),
                          self.assertRaisesRegex(RuntimeError, 'Unexplained published')):
                        store.ensure_archive(self.backup, renamed)
                self.assertGreater(bytes_read, 0)
                self.assertLess(bytes_read, 2 * len(self.blobs()) * 128 * 1024)
                self.assertEqual(before, self.backup_inventory())
                self.assertFalse(journal.exists() or partial.exists())
                self.assertTrue(Path(record['path']).is_file())
                print(f'Orphan preflight ({kind}): {bytes_read} metadata bytes across two retries; '
                      f'{len(self.blobs())} CAS objects, no payload hash/write')

    def test_unreadable_orphan_metadata_blocks_build_without_any_storage_change(self):
        source = self.fixture()
        record = store.ensure_archive(self.backup, source)
        self.paths(source)[0].unlink()
        Path(record['path']).write_bytes(b'damaged ZIP index; delivery binding unknown')
        before = self.backup_inventory()
        with (patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')),
              patch.object(store, '_save_journal', side_effect=AssertionError('replacement intent')),
              self.assertRaisesRegex(RuntimeError, 'Cannot bind CAS object metadata; owner restoration')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, self.backup_inventory())

    def test_unrecognized_orphan_manifest_is_preserved_and_blocks_build(self):
        source = self.fixture()
        self.forged_archive(source, store.delivery_descriptor(source), manifest={'unknown': 'manifest'})
        self.paths(source)[0].unlink()
        before = self.backup_inventory()
        with (patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')),
              self.assertRaisesRegex(RuntimeError, 'Cannot bind CAS object metadata; owner restoration')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, self.backup_inventory())

    def test_orphan_preflight_rejects_symlinked_object_without_touching_evidence(self):
        source = self.fixture()
        record = store.ensure_archive(self.backup, source)
        blob = Path(record['path'])
        moved = self.script / 'owner-retained-orphan.zip'
        blob.rename(moved)
        blob.symlink_to(moved)
        self.paths(source)[0].unlink()
        before = self.backup_inventory()
        retained = moved.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'owner restoration required'):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, self.backup_inventory())
        self.assertEqual(retained, moved.read_bytes())

    def test_initial_temporary_only_intent_is_promoted_before_payload_creation(self):
        source = self.fixture()
        journal, _ = self.paths(source)
        temporary = journal.with_suffix('.json.tmp')
        with (patch.object(store.os, 'replace', side_effect=OSError('interrupted initial intent replace')),
              self.assertRaisesRegex(OSError, 'initial intent')):
            store.ensure_archive(self.backup, source)
        self.assertFalse(journal.exists())
        self.assertEqual('building', json.loads(temporary.read_bytes())['state'])
        write = store._write_partial
        def require_durable_main(*args):
            self.assertTrue(journal.is_file())
            self.assertFalse(temporary.exists())
            self.assertEqual('building', self.journal(source)['state'])
            write(*args)
        with patch.object(store, '_write_partial', side_effect=require_durable_main):
            record = store.ensure_archive(self.backup, source)
        self.assert_clean(source, record)

    def test_building_temporary_only_intent_can_reuse_owned_incomplete_partial(self):
        source = self.fixture()
        self.stop_before_write(source)
        journal, partial = self.paths(source)
        journal.rename(journal.with_suffix('.json.tmp'))
        partial.write_bytes(b'interrupted owned build')
        inode = partial.stat().st_ino
        write = store._write_partial
        def rebuild(*args):
            self.assertEqual(inode, partial.stat().st_ino)
            write(*args)
            self.assertEqual(inode, partial.stat().st_ino)
        with patch.object(store, '_write_partial', side_effect=rebuild):
            record = store.ensure_archive(self.backup, source)
        self.assert_clean(source, record)

    def test_building_temporary_cannot_hide_matching_orphan_or_allocate_payload(self):
        source = self.fixture()
        store.ensure_archive(self.backup, source)
        journal, partial = self.paths(source)
        row = self.journal(source)
        row.update(state='building', archive=None)
        journal.with_suffix('.json.tmp').write_bytes(canonical(row))
        journal.unlink()
        before = self.backup_inventory()
        with (patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')),
              self.assertRaisesRegex(RuntimeError, 'Unexplained published')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, self.backup_inventory())
        self.assertFalse(journal.exists() or partial.exists())

    def test_sealed_temporary_only_pin_publishes_verified_partial_without_rebuild(self):
        source = self.fixture()
        self.stop_after_seal(source)
        journal, _ = self.paths(source)
        pinned = self.journal(source)['archive']
        journal.rename(journal.with_suffix('.json.tmp'))
        with (patch.object(store, '_preflight_new_build', side_effect=AssertionError('pinned scan')),
              patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild'))):
            self.assertEqual(pinned, store.ensure_archive(self.backup, source))
        self.assert_clean(source, pinned)

    def test_sealed_temporary_only_pin_recovers_owned_publication_link(self):
        source = self.fixture()
        link = store.os.link
        def interrupt(*args, **kwargs):
            link(*args, **kwargs)
            raise OSError('interrupted after publication link')
        with patch.object(store.os, 'link', side_effect=interrupt), self.assertRaises(OSError):
            store.ensure_archive(self.backup, source)
        journal, partial = self.paths(source)
        pinned = self.journal(source)['archive']
        journal.rename(journal.with_suffix('.json.tmp'))
        self.assertEqual(2, partial.stat().st_nlink)
        with patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild')):
            self.assertEqual(pinned, store.ensure_archive(self.backup, source))
        self.assert_clean(source, pinned)
        self.assertEqual(1, Path(pinned['path']).stat().st_nlink)

    def test_published_temporary_only_pin_promotes_after_full_read_only_verification(self):
        source = self.fixture()
        pinned = store.ensure_archive(self.backup, source)
        journal, _ = self.paths(source)
        temporary = journal.with_suffix('.json.tmp')
        saved = journal.read_bytes()
        journal.rename(temporary)
        before = self.backup_inventory()
        with self.assertRaisesRegex(RuntimeError, 'matching published journal'):
            store.validate_archive(self.backup, source, pinned)
        self.assertEqual(before, self.backup_inventory())
        with (patch.object(store, '_preflight_new_build', side_effect=AssertionError('pinned scan')),
              patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild')),
              patch.object(store, '_save_journal', side_effect=AssertionError('pin overwritten'))):
            self.assertEqual(pinned, store.ensure_archive(self.backup, source))
        self.assertEqual(saved, journal.read_bytes())
        self.assert_clean(source, pinned)

    def test_sealed_temporary_pin_missing_or_corrupt_partial_is_preserved(self):
        for damage in ('missing', 'corrupt'):
            with self.subTest(damage=damage):
                source = self.fixture(payload={'01.flac': f'{damage} temporary-only delivery'.encode()})
                self.stop_after_seal(source)
                journal, partial = self.paths(source)
                journal.rename(journal.with_suffix('.json.tmp'))
                partial.unlink() if damage == 'missing' else partial.write_bytes(b'corrupt sealed partial')
                before = self.backup_inventory()
                with (patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild')),
                      patch.object(store, '_save_journal', side_effect=AssertionError('pin overwritten')),
                      self.assertRaisesRegex(RuntimeError, 'owner restoration required')):
                    store.ensure_archive(self.backup, source)
                self.assertEqual(before, self.backup_inventory())
                self.assertFalse(journal.exists())

    def test_published_temporary_pin_with_missing_blob_never_uses_complete_partial(self):
        source = self.fixture()
        pinned = store.ensure_archive(self.backup, source)
        journal, partial = self.paths(source)
        journal.rename(journal.with_suffix('.json.tmp'))
        Path(pinned['path']).rename(partial)
        before = self.backup_inventory()
        with (patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild')),
              patch.object(store, '_save_journal', side_effect=AssertionError('pin overwritten')),
              self.assertRaisesRegex(RuntimeError, 'Missing published shared original; owner restoration')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, self.backup_inventory())
        self.assertFalse(journal.exists())

    def test_corrupt_published_blob_cannot_be_repaired_from_temporary_only_pin(self):
        source = self.fixture()
        pinned = store.ensure_archive(self.backup, source)
        journal, _ = self.paths(source)
        journal.rename(journal.with_suffix('.json.tmp'))
        Path(pinned['path']).write_bytes(b'corrupt published blob')
        before = self.backup_inventory()
        with (patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild')),
              self.assertRaisesRegex(RuntimeError, 'Corrupt pinned.*owner restoration')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, self.backup_inventory())
        self.assertFalse(journal.exists())

    def test_malformed_or_conflicting_temporary_only_metadata_is_preserved(self):
        source = self.fixture()
        self.stop_before_write(source)
        journal, partial = self.paths(source)
        row = self.journal(source)
        temporary = journal.with_suffix('.json.tmp')
        journal.rename(temporary)
        bad_pin = {'version': 1, 'object_id': row['object_id'], 'path': '../escape.zip',
                   'file_sha256': 'a' * 64, 'entries': {}}
        damages = [b'not JSON', canonical({**row, 'unexpected': True}),
                   canonical({**row, 'object_id': 'a' * 64}),
                   canonical({**row, 'descriptor': {'other': 'delivery'}}),
                   canonical({**row, 'state': 'published', 'archive': bad_pin}),
                   json.dumps(row, indent=2).encode()]
        for damage in damages:
            with self.subTest(damage=damage):
                temporary.write_bytes(damage)
                before = self.backup_inventory()
                with (patch.object(store, '_write_partial', side_effect=AssertionError('new payload')),
                      patch.object(store, '_save_journal', side_effect=AssertionError('discarded metadata')),
                      self.assertRaisesRegex(RuntimeError, 'owner restoration required')):
                    store.ensure_archive(self.backup, source)
                self.assertEqual(before, self.backup_inventory())
                self.assertFalse(journal.exists() or partial.exists())

    def test_temporary_only_symlink_or_hardlink_metadata_cannot_be_promoted(self):
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                source = self.fixture(payload={'01.flac': f'{kind} temporary-only delivery'.encode()})
                self.stop_before_write(source)
                journal, _ = self.paths(source)
                temporary = journal.with_suffix('.json.tmp')
                victim = self.script / f'{kind}-temporary-owner-record'
                journal.rename(victim)
                temporary.symlink_to(victim) if kind == 'symlink' else os.link(victim, temporary)
                before = self.backup_inventory()
                saved = victim.read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                    store.ensure_archive(self.backup, source)
                self.assertEqual(before, self.backup_inventory())
                self.assertEqual(saved, victim.read_bytes())
                self.assertFalse(journal.exists())

    def test_save_cannot_truncate_unrecovered_temporary_pin(self):
        source = self.fixture()
        store.ensure_archive(self.backup, source)
        journal, _ = self.paths(source)
        building = self.journal(source)
        building.update(state='building', archive=None)
        journal.rename(journal.with_suffix('.json.tmp'))
        before = self.backup_inventory()
        with self.assertRaisesRegex(RuntimeError, 'Unrecovered.*owner restoration'):
            store._save_journal(journal, building)
        self.assertEqual(before, self.backup_inventory())

    def test_temporary_only_promotion_failure_retains_pin_and_can_resume(self):
        source = self.fixture()
        pinned = store.ensure_archive(self.backup, source)
        journal, _ = self.paths(source)
        journal.rename(journal.with_suffix('.json.tmp'))
        before = self.backup_inventory()
        with (patch.object(store.os, 'replace', side_effect=OSError('interrupted journal promotion')),
              patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild')),
              self.assertRaisesRegex(OSError, 'journal promotion')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, self.backup_inventory())
        self.assertFalse(journal.exists())
        self.assertEqual(pinned, store.ensure_archive(self.backup, source))
        self.assert_clean(source, pinned)

    def test_durable_intent_precedes_any_partial_creation_and_missing_partial_resumes(self):
        source = self.fixture()
        self.stop_before_write(source)
        journal, partial = self.paths(source)
        self.assertTrue(journal.exists())
        self.assertFalse(partial.exists())
        record = store.ensure_archive(self.backup, source)
        self.assert_clean(source, record)

    def test_interrupted_incomplete_build_reuses_one_partial_inode(self):
        source = self.fixture()
        def interrupt(original, target, length):
            target.write(original.read(3))
            raise OSError('interrupted payload write')
        journal, partial = self.paths(source)
        inodes = []
        for _ in range(3):
            with (patch.object(store.shutil, 'copyfileobj', side_effect=interrupt),
                  self.assertRaisesRegex(OSError, 'interrupted payload')):
                store.ensure_archive(self.backup, source)
            inodes.append(partial.stat().st_ino)
        inode = partial.stat().st_ino
        self.assertEqual([inode] * 3, inodes)
        self.assertEqual('building', self.journal(source)['state'])
        self.assertEqual([partial], list(journal.parent.glob('*.partial')))
        write = store._write_partial
        seen = []
        def rebuild(*args):
            seen.append(partial.stat().st_ino)
            write(*args)
            self.assertEqual(inode, partial.stat().st_ino)
        with patch.object(store, '_write_partial', side_effect=rebuild):
            record = store.ensure_archive(self.backup, source)
        self.assertEqual([inode], seen)
        self.assert_clean(source, record)

    def test_complete_unsealed_partial_resumes_without_recopying_payload(self):
        source = self.fixture()
        save = store._save_journal
        def interrupt(path, row):
            if row['state'] == 'sealed':
                raise OSError('interrupted before seal')
            save(path, row)
        with (patch.object(store, '_save_journal', side_effect=interrupt),
              self.assertRaisesRegex(OSError, 'before seal')):
            store.ensure_archive(self.backup, source)
        self.assertEqual('building', self.journal(source)['state'])
        with patch.object(store, '_write_partial', side_effect=AssertionError('unnecessary rebuild')):
            record = store.ensure_archive(self.backup, source)
        self.assert_clean(source, record)

    def test_sealed_partial_resumes_after_failure_before_publication(self):
        source = self.fixture()
        self.stop_after_seal(source)
        pinned = self.journal(source)['archive']
        with patch.object(store, '_write_partial', side_effect=AssertionError('sealed rebuild')):
            self.assertEqual(pinned, store.ensure_archive(self.backup, source))
        self.assert_clean(source, pinned)

    def test_sealed_partial_missing_or_corrupt_is_a_restoration_blocker(self):
        for damage in ('missing', 'corrupt'):
            with self.subTest(damage=damage):
                # Names alone deduplicate: independent failure cases need distinct bytes.
                source = self.fixture(payload={'01.flac': f'{damage} pinned delivery'.encode()})
                self.stop_after_seal(source)
                journal, partial = self.paths(source)
                before = journal.read_bytes()
                partial.unlink() if damage == 'missing' else partial.write_bytes(b'incomplete now')
                with (patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild')),
                      self.assertRaisesRegex(RuntimeError, 'restoration required')):
                    store.ensure_archive(self.backup, source)
                self.assertEqual(before, journal.read_bytes())
                self.assertEqual([], self.blobs())

    def test_crash_immediately_after_hardlink_resumes_and_unlinks_only_owned_partial(self):
        source = self.fixture()
        link = store.os.link
        def interrupt(*args, **kwargs):
            link(*args, **kwargs)
            raise OSError('interrupted after link')
        with (patch.object(store.os, 'link', side_effect=interrupt),
              self.assertRaisesRegex(OSError, 'after link')):
            store.ensure_archive(self.backup, source)
        _, partial = self.paths(source)
        record = self.journal(source)['archive']
        blob = Path(record['path'])
        self.assertEqual(2, blob.stat().st_nlink)
        self.assertEqual(blob.stat().st_ino, partial.stat().st_ino)
        with patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')):
            self.assertEqual(record, store.ensure_archive(self.backup, source))
        self.assertEqual(1, blob.stat().st_nlink)
        self.assert_clean(source, record)

    def test_crash_before_published_index_update_resumes_without_duplicate_payload(self):
        source = self.fixture()
        save = store._save_journal
        def interrupt(path, row):
            if row['state'] == 'published':
                raise OSError('interrupted index')
            save(path, row)
        with (patch.object(store, '_save_journal', side_effect=interrupt),
              self.assertRaisesRegex(OSError, 'interrupted index')):
            store.ensure_archive(self.backup, source)
        self.assertEqual('sealed', self.journal(source)['state'])
        self.assertEqual(1, len(self.blobs()))
        with patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')):
            record = store.ensure_archive(self.backup, source)
        self.assert_clean(source, record)

    def test_crash_during_index_replace_reuses_deterministic_metadata_temporary(self):
        source = self.fixture()
        replace = store.os.replace
        def interrupt(src, dst):
            if json.loads(Path(src).read_bytes())['state'] == 'published':
                raise OSError('interrupted index replace')
            replace(src, dst)
        with (patch.object(store.os, 'replace', side_effect=interrupt),
              self.assertRaisesRegex(OSError, 'index replace')):
            store.ensure_archive(self.backup, source)
        journal, _ = self.paths(source)
        self.assertTrue(journal.with_suffix('.json.tmp').is_file())
        with patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')):
            record = store.ensure_archive(self.backup, source)
        self.assert_clean(source, record)

    def test_crash_after_published_index_before_partial_unlink_resumes(self):
        source = self.fixture()
        _, partial = self.paths(source)
        unlink = Path.unlink
        def interrupt(path, *args, **kwargs):
            if path == partial:
                raise OSError('interrupted owned-partial unlink')
            return unlink(path, *args, **kwargs)
        with (patch.object(Path, 'unlink', interrupt),
              self.assertRaisesRegex(OSError, 'owned-partial unlink')):
            store.ensure_archive(self.backup, source)
        record = self.journal(source)['archive']
        self.assertEqual('published', self.journal(source)['state'])
        # Read-only validation accepts the exact publication link but leaves it alone.
        store.validate_archive(self.backup, source, record)
        self.assertTrue(partial.exists())
        with patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')):
            self.assertEqual(record, store.ensure_archive(self.backup, source))
        self.assert_clean(source, record)

    def test_sealed_journal_can_finish_with_verified_blob_and_missing_partial(self):
        source = self.fixture()
        link = store.os.link
        def interrupt(*args, **kwargs):
            link(*args, **kwargs)
            raise OSError('interrupted publication')
        with patch.object(store.os, 'link', side_effect=interrupt), self.assertRaises(OSError):
            store.ensure_archive(self.backup, source)
        self.paths(source)[1].unlink()
        record = store.ensure_archive(self.backup, source)
        self.assert_clean(source, record)

    def test_publication_collision_is_no_clobber_and_retains_both_evidence_paths(self):
        source = self.fixture()
        link = store.os.link
        def collide(src, dst, **kwargs):
            Path(dst).write_bytes(b'unexplained object at the pinned address')
            link(src, dst, **kwargs)
        with patch.object(store.os, 'link', side_effect=collide), self.assertRaises(FileExistsError):
            store.ensure_archive(self.backup, source)
        row = self.journal(source)
        self.assertEqual('sealed', row['state'])
        blob = Path(row['archive']['path'])
        self.assertEqual(b'unexplained object at the pinned address', blob.read_bytes())
        self.assertTrue(self.paths(source)[1].is_file())
        with self.assertRaisesRegex(RuntimeError, 'Corrupt pinned'):
            store.ensure_archive(self.backup, source)
        self.assertEqual([blob], self.blobs())
        self.assertEqual(row, self.journal(source))

    def test_complete_recovered_partial_is_fsynced_before_seal(self):
        source = self.fixture()
        save = store._save_journal
        def interrupt(path, row):
            if row['state'] == 'sealed':
                raise OSError('interrupted before seal')
            save(path, row)
        with patch.object(store, '_save_journal', side_effect=interrupt), self.assertRaises(OSError):
            store.ensure_archive(self.backup, source)
        journal, partial = self.paths(source)
        before = journal.read_bytes(), partial.read_bytes()
        with (patch.object(store.os, 'fsync', side_effect=OSError('partial data fsync failed')),
              patch.object(store, '_write_partial', side_effect=AssertionError('duplicate payload')),
              patch.object(store, '_save_journal', side_effect=AssertionError('seal before data fsync')),
              self.assertRaisesRegex(OSError, 'partial data fsync')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, (journal.read_bytes(), partial.read_bytes()))
        self.assertFalse(journal.with_suffix('.json.tmp').exists())
        self.assert_clean(source, store.ensure_archive(self.backup, source))

    def test_recovered_publication_directory_is_fsynced_before_published_index(self):
        source = self.fixture()
        link = store.os.link
        def interrupt(*args, **kwargs):
            link(*args, **kwargs)
            raise OSError('interrupted before publication directory fsync')
        with patch.object(store.os, 'link', side_effect=interrupt), self.assertRaises(OSError):
            store.ensure_archive(self.backup, source)
        record = self.journal(source)['archive']
        sync = sort._sync_directory
        def fail_publication_sync(path):
            if Path(path) == Path(record['path']).parent:
                raise OSError('publication directory fsync failed')
            sync(path)
        with (patch.object(sort, '_sync_directory', side_effect=fail_publication_sync),
              patch.object(store, '_save_journal', side_effect=AssertionError('index before directory fsync')),
              self.assertRaisesRegex(OSError, 'publication directory fsync')):
            store.ensure_archive(self.backup, source)
        self.assertEqual('sealed', self.journal(source)['state'])
        self.assertTrue(self.paths(source)[1].exists())
        self.assert_clean(source, store.ensure_archive(self.backup, source))

    def test_unknown_partial_without_intent_is_never_truncated_or_claimed(self):
        source = self.fixture()
        journal, partial = self.paths(source)
        journal.parent.mkdir(parents=True)
        partial.write_bytes(b'unowned recovery evidence')
        with self.assertRaisesRegex(RuntimeError, 'without durable intent'):
            store.ensure_archive(self.backup, source)
        self.assertEqual(b'unowned recovery evidence', partial.read_bytes())
        self.assertFalse(journal.exists())

    def test_symlink_and_hardlink_build_partials_are_never_written_or_removed(self):
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                source = self.fixture(payload={'01.flac': f'{kind} partial delivery'.encode()})
                self.stop_before_write(source)
                _, partial = self.paths(source)
                target = self.script / f'{kind}-victim'
                target.write_bytes(b'leave this recovery evidence alone')
                if kind == 'symlink':
                    partial.symlink_to(target)
                else:
                    os.link(target, partial)
                with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                    store.ensure_archive(self.backup, source)
                self.assertTrue(os.path.lexists(partial))
                self.assertEqual(b'leave this recovery evidence alone', target.read_bytes())

    def test_external_third_link_is_not_mistaken_for_the_owned_publication_pair(self):
        source = self.fixture()
        self.stop_after_seal(source)
        _, partial = self.paths(source)
        record = self.journal(source)['archive']
        blob = Path(record['path'])
        blob.parent.mkdir(parents=True, exist_ok=True)
        os.link(partial, blob)
        other = self.script / 'external-link'
        os.link(partial, other)
        with self.assertRaisesRegex(RuntimeError, 'hardlinked'):
            store.ensure_archive(self.backup, source)
        self.assertEqual(3, blob.stat().st_nlink)
        self.assertTrue(partial.exists() and other.exists())

    def test_unsafe_journal_and_metadata_temporary_links_are_blocked(self):
        for target_kind in ('journal', 'temporary'):
            for link_kind in ('symlink', 'hardlink'):
                with self.subTest(target=target_kind, kind=link_kind):
                    source = self.fixture(payload={'01.flac': f'{target_kind}-{link_kind} delivery'.encode()})
                    self.stop_before_write(source)
                    journal, _ = self.paths(source)
                    victim = self.script / f'{target_kind}-{link_kind}-victim'
                    victim.write_bytes(journal.read_bytes())
                    target = journal if target_kind == 'journal' else journal.with_suffix('.json.tmp')
                    if target.exists():
                        target.unlink()
                    target.symlink_to(victim) if link_kind == 'symlink' else os.link(victim, target)
                    before = victim.read_bytes()
                    with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                        store.ensure_archive(self.backup, source)
                    self.assertEqual(before, victim.read_bytes())
                    self.assertTrue(os.path.lexists(target))

    def test_nested_backup_roots_are_rejected_before_any_directory_creation(self):
        source = self.fixture()
        for forbidden in (self.inbox, self.stage, self.library):
            with self.subTest(root=forbidden):
                destination = forbidden / 'must-not-exist' / 'backup'
                with self.assertRaisesRegex(RuntimeError, 'cannot be inside'):
                    store.ensure_archive(destination, source)
                self.assertFalse(destination.parent.exists())

    def test_noncanonical_backup_root_cannot_bypass_inbox_boundary_checks(self):
        source = self.fixture()
        destination = self.inbox / 'not-created' / 'backup'
        for spelling in ('/' + str(destination), str(destination.parent) + '//backup',
                         str(destination.parent) + '/./backup', str(destination.parent) + '/../backup',
                         'relative-backup'):
            with self.subTest(root=spelling), self.assertRaisesRegex(RuntimeError, 'Noncanonical'):
                store.ensure_archive(spelling, source)
            self.assertFalse(destination.parent.exists())

    def test_backup_root_and_shared_parents_cannot_be_symlinks_or_files(self):
        source = self.fixture()
        actual = self.script / 'actual-backup'
        actual.mkdir()
        linked = self.script / 'linked-backup'
        linked.symlink_to(actual, target_is_directory=True)
        regular = self.script / 'not-a-directory'
        regular.write_bytes(b'not a directory')
        for destination in (linked / 'new', regular / 'new'):
            with self.subTest(root=destination), self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                store.ensure_archive(destination, source)
        originals = self.backup / 'Originals'
        originals.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            store.ensure_archive(self.backup, source)
        self.assertEqual([], list(actual.iterdir()))

    def test_symlinked_published_object_parent_is_rejected(self):
        source = self.fixture()
        record = store.ensure_archive(self.backup, source)
        shard = Path(record['path']).parent
        moved = shard.with_name(shard.name + '-owner-evidence')
        shard.rename(moved)
        shard.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            store.ensure_archive(self.backup, source)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            store.validate_archive(self.backup, source, record)
        self.assertTrue((moved / Path(record['path']).name).exists())

    def test_conflicting_or_corrupt_journal_is_retained_without_payload_rebuild(self):
        source = self.fixture()
        self.stop_before_write(source)
        journal, partial = self.paths(source)
        original = journal.read_bytes()
        for damage in (b'not json', canonical({**json.loads(original), 'object_id': 'a' * 64}),
                       canonical({**json.loads(original), 'descriptor': {'other': 'delivery'}})):
            journal.write_bytes(damage)
            with self.assertRaisesRegex(RuntimeError, 'journal'):
                store.ensure_archive(self.backup, source)
            self.assertEqual(damage, journal.read_bytes())
            self.assertFalse(partial.exists())

    def test_changed_physical_source_is_rejected_before_intent_or_payload_creation(self):
        source = self.fixture()
        (Path(source['source']) / 'unaccounted.txt').write_bytes(b'late arrival')
        with self.assertRaisesRegex(RuntimeError, 'inventory changed'):
            store.ensure_archive(self.backup, source)
        self.assertEqual([], list(self.backup.iterdir()))

    def test_physical_source_is_checked_again_after_payload_write(self):
        source = self.fixture()
        write = store._write_partial
        def mutate(*args):
            write(*args)
            (Path(source['source']) / 'late.txt').write_bytes(b'changed during build')
        with (patch.object(store, '_write_partial', side_effect=mutate),
              self.assertRaisesRegex(RuntimeError, 'inventory changed')):
            store.ensure_archive(self.backup, source)
        self.assertEqual('building', self.journal(source)['state'])
        self.assertTrue(self.paths(source)[1].exists())
        self.assertEqual([], self.blobs())

    def test_physical_source_is_checked_again_after_publication_before_index(self):
        source = self.fixture()
        link = store.os.link
        def mutate(*args, **kwargs):
            link(*args, **kwargs)
            (Path(source['source']) / 'late.txt').write_bytes(b'changed during publication')
        with (patch.object(store.os, 'link', side_effect=mutate),
              self.assertRaisesRegex(RuntimeError, 'inventory changed')):
            store.ensure_archive(self.backup, source)
        self.assertEqual('sealed', self.journal(source)['state'])
        self.assertEqual(1, len(self.blobs()))
        self.assertTrue(self.paths(source)[1].exists())

    def test_damaged_blob_after_link_blocks_resume_without_discarding_partial_or_pin(self):
        source = self.fixture()
        link = store.os.link
        def interrupt(*args, **kwargs):
            link(*args, **kwargs)
            raise OSError('interrupted publication')
        with patch.object(store.os, 'link', side_effect=interrupt), self.assertRaises(OSError):
            store.ensure_archive(self.backup, source)
        row = self.journal(source)
        Path(row['archive']['path']).write_bytes(b'corrupt linked object and partial')
        journal, partial = self.paths(source)
        before = journal.read_bytes(), partial.read_bytes()
        with (patch.object(store, '_write_partial', side_effect=AssertionError('pinned rebuild')),
              self.assertRaisesRegex(RuntimeError, 'Corrupt pinned')):
            store.ensure_archive(self.backup, source)
        self.assertEqual(before, (journal.read_bytes(), partial.read_bytes()))
        self.assertEqual(1, len(self.blobs()))
