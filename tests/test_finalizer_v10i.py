"""Archive/cleanup finalization invariants on disposable sources only."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
import unittest
import zipfile

from support import WorkspaceCase, make_audio
import shutil
import subprocess
from mutagen import File
import tag
import mixie_batch_duplicates
import mixie
import mixie_finalizer as final
import sort


def assert_shared_originals(case, row, deliveries):
    """Check row pins against independently captured, complete fixture bytes."""
    originals = row['originals']
    case.assertEqual({'format', 'deliveries'}, set(originals))
    case.assertEqual('shared_originals_v1', originals['format'])
    case.assertEqual(set(deliveries), set(originals['deliveries']))
    case.assertEqual({name: sorted(f'{name}/{leaf}' for leaf in delivery['tracks'])
                      for name, delivery in deliveries.items()}, row['source_claims'])
    records = {}
    for name, delivery in deliveries.items():
        with case.subTest(shared_original=name):
            reference = originals['deliveries'][name]
            case.assertEqual({'archive', 'claimed_audio_sha256'}, set(reference))
            case.assertEqual({leaf: hashlib.sha256(delivery['payload'][leaf]).hexdigest()
                              for leaf in delivery['tracks']}, reference['claimed_audio_sha256'])
            record = reference['archive']
            case.assertEqual({'version', 'object_id', 'path', 'file_sha256', 'entries'}, set(record))
            case.assertIs(type(record['version']), int)
            case.assertEqual(1, record['version'])
            path = Path(record['path'])
            case.assertFalse(path.is_symlink())
            case.assertTrue(path.is_file())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            shared = case.backup / 'Originals' / 'Shared'
            case.assertEqual(shared / 'sha256' / digest[:2] / f'{digest}.zip', path)
            case.assertEqual(digest, record['file_sha256'])
            case.assertEqual(1, path.stat().st_nlink)
            files = ({f'files/{leaf}': data for leaf, data in delivery['payload'].items()}
                     if delivery['kind'] == 'directory' else
                     {'original.zip' if delivery['kind'] == 'zip' else 'original': delivery['raw']})
            descriptor = {'version': 1, 'type': 'shared_original_delivery',
                          'source_kind': delivery['kind'],
                          'payload_sha256': {leaf: hashlib.sha256(data).hexdigest()
                                             for leaf, data in delivery['payload'].items()},
                          'sha256': {leaf: hashlib.sha256(data).hexdigest() for leaf, data in files.items()}}
            manifest_bytes = json.dumps(descriptor, sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')
            object_id = hashlib.sha256(manifest_bytes).hexdigest()
            case.assertEqual(object_id, record['object_id'])
            entries = {**descriptor['sha256'], final.MANIFEST_NAME: object_id}
            case.assertEqual(entries, record['entries'])
            case.assertEqual(entries, sort.zip_manifest(path))
            with zipfile.ZipFile(path) as saved:
                case.assertIsNone(saved.testzip())
                case.assertEqual(sorted(entries), saved.namelist())
                case.assertEqual(manifest_bytes, saved.read(final.MANIFEST_NAME))
                case.assertEqual(files, {leaf: saved.read(leaf) for leaf in files})
            journal = shared / 'deliveries' / f'{record["object_id"]}.json'
            case.assertEqual({'version': 1, 'object_id': object_id, 'descriptor': descriptor,
                              'state': 'published', 'archive': record}, json.loads(journal.read_bytes()))
            case.assertFalse(journal.with_suffix('.partial').exists())
            records[name] = record
    library_relative = Path(row['library']['path']).relative_to(case.backup / 'Library')
    case.assertFalse((case.backup / 'Originals' / library_relative).exists())
    return records


class FinalizerTests(WorkspaceCase):
    def _fixture(self, *, extra_unresolved=False):
        (self.script / '.mixie-import-first.json').write_text(
            json.dumps({'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')
        inbox = self.inbox / 'ugly download NAME'
        inbox.mkdir()
        (inbox / '01.flac').write_bytes(b'unmodified-source-audio')
        (inbox / 'cover.jpg').write_bytes(b'unmodified-original-art')
        if extra_unresolved:
            (inbox / '02.flac').write_bytes(b'unresolved-other-album')
        sort.sort_inbox()
        name = sort.staging_import_name(inbox.name, 'directory')
        root = self.stage / name
        self.original_deliveries = {name: {'kind': 'directory', 'tracks': ['01.flac'],
            'payload': {path.relative_to(inbox).as_posix(): path.read_bytes()
                        for path in inbox.rglob('*') if path.is_file()}}}
        issue = {'album': 'NEED', 'import_name': name, 'tracks': [f'{name}/01.flac']}
        destination = self.library / '3OH!3' / 'NEED'
        destination.mkdir(parents=True)
        music = destination / '01 - Track.flac'
        cover = destination / 'cover.jpg'
        music.write_bytes(b'correctly-tagged-library-audio')
        cover.write_bytes(b'new-verified-artwork')
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        manifest = mixie.completion_source_manifest(issue)
        receipt = mixie.CommitReceipt({str(music): sha(music), str(cover): sha(cover)},
            input_map={f'{name}/01.flac': {'source_sha256': sha(root/'01.flac'),
                'destination': str(music), 'destination_sha256': sha(music)}},
            source_manifest=manifest, asset_outputs={str(cover): sha(cover)},
            evidence={'provenance': 'independently_verified'})
        mixie.record_library_only_import(issue, receipt)
        albums = {(name, 'NEED'): {'album': 'NEED',
                  'tracks': [{'path': root / '01.flac'}]}}
        return inbox, root, music, cover, albums

    def assert_originals(self):
        rows = final._state()['finalized']
        self.assertEqual(1, len(rows))
        records = assert_shared_originals(self, next(iter(rows.values())), self.original_deliveries)
        self.assertEqual({Path(record['path']) for record in records.values()},
                         set((self.backup / 'Originals').rglob('*.zip')))
        return records

    def test_both_verified_zip_types_then_remove_originals_and_stage(self):
        inbox, stage, music, cover, albums = self._fixture()
        result, errors = final.finish_verified_albums(mixie, albums, allow_source_cleanup=True)
        self.assertEqual((1, []), (result, errors))
        self.assertFalse(inbox.exists())
        self.assertFalse(stage.exists())
        self.assert_originals()
        library = self.backup / 'Library' / '3OH!3' / 'NEED.zip'
        self.assertTrue(library.is_file())
        with zipfile.ZipFile(library) as saved:
            self.assertEqual(music.read_bytes(), saved.read(music.name))
            self.assertEqual(cover.read_bytes(), saved.read(cover.name))
        ledger = json.loads((self.backup / final.LEDGER_NAME).read_text())
        self.assertIsNone(ledger['active'])
        self.assertEqual(1, len(ledger['completed']))
        self.assertEqual((0, []), final.finish_verified_albums(mixie, {}))

    def test_default_finalization_archives_but_does_not_remove_any_source_bytes(self):
        inbox, staged, _music, _cover, albums = self._fixture()
        before = sort.file_manifest(inbox), sort.file_manifest(staged)
        with patch.object(final.os, 'rename', side_effect=AssertionError('Unauthorized source rename')), \
             patch.object(final.shutil, 'rmtree', side_effect=AssertionError('Unauthorized source purge')):
            self.assertEqual((1, []), final.finish_verified_albums(mixie, albums))
            self.assertFalse(final.resume_cleanup())
        self.assertEqual(before, (sort.file_manifest(inbox), sort.file_manifest(staged)))
        self.assertTrue(final.is_album_finalized(mixie, mixie._album_issue(next(iter(albums.values())))))
        self.assertEqual({}, final._state()['completed'])

    def test_previous_verified_import_reconciles_without_second_scan(self):
        inbox, staged, _music, _cover, albums = self._fixture()
        with patch.object(mixie.sort, 'sort_inbox'), \
             patch.object(tag, 'group_staged_albums', return_value=albums), \
             patch.object(mixie, 'get_acoustid_client_key', return_value='OFFLINE-DISPOSABLE'), \
             patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(mixie, 'preview_album', side_effect=AssertionError('Rescan is forbidden')), \
             patch.object(mixie, 'input', return_value='n'), \
             redirect_stdout(StringIO()):
            mixie._main('1', allow_source_cleanup=True)
        self.assertFalse(inbox.exists())
        self.assertFalse(staged.exists())
        self.assert_originals()
        self.assertTrue((self.backup / 'Library' / '3OH!3' / 'NEED.zip').exists())

    def test_already_imported_review_finalizes_without_reimporting_or_grouping(self):
        inbox, staged, _music, _cover, albums = self._fixture()
        issue = mixie._album_issue(next(iter(albums.values())))
        pending = [issue]
        with patch.object(mixie, 'test_review_decision', side_effect=AssertionError('Duplicate import forbidden')), \
             patch.object(tag, 'group_staged_albums', side_effect=AssertionError('Metadata grouping forbidden')), \
             redirect_stdout(StringIO()):
            self.assertTrue(mixie.finalize_reviewed_album(issue, pending, allow_source_cleanup=True))
        self.assertEqual([], pending)
        self.assertFalse(inbox.exists() or staged.exists())
        self.assert_originals()
        self.assertTrue((self.backup / 'Library' / '3OH!3' / 'NEED.zip').exists())

    def test_archive_failure_never_removes_download_or_staged_source(self):
        inbox, stage, *_rest, albums = self._fixture()
        old = final._write_zip
        def fail_library(kind, *args):
            if kind == 'Library':
                raise OSError('simulated disk full')
            return old(kind, *args)
        with patch.object(final, '_write_zip', side_effect=fail_library):
            count, errors = final.finish_verified_albums(mixie, albums, allow_source_cleanup=True)
        self.assertEqual(count, 0)
        self.assertTrue(errors)
        self.assertTrue(inbox.exists() and stage.exists())
        self.assertIsNone(final._state()['active'])
        count, errors = final.finish_verified_albums(mixie, albums, allow_source_cleanup=True)
        self.assertEqual((1, []), (count, errors))
        self.assertFalse(inbox.exists() or stage.exists())

    def test_incomplete_shared_source_is_never_cleaned(self):
        inbox, stage, *_rest, albums = self._fixture(extra_unresolved=True)
        original, staged = sort.file_manifest(inbox), sort.file_manifest(stage)
        count, errors = final.finish_verified_albums(mixie, albums, allow_source_cleanup=True)
        self.assertEqual(1, count)
        self.assertTrue(any('Shared source' in value for value in errors))
        self.assertEqual(original, sort.file_manifest(inbox))
        self.assertEqual(staged, sort.file_manifest(stage))
        self.assert_originals()
        self.assertTrue((self.backup / 'Library' / '3OH!3' / 'NEED.zip').is_file())
        issue = mixie._album_issue(next(iter(albums.values())))
        self.assertTrue(final.is_album_finalized(mixie, issue))
        self.assertIsNone(final._state()['active'])
        self.assertEqual({}, final._state()['completed'])

    def test_interrupted_purge_can_resume_without_reimporting(self):
        inbox, stage, *_rest, albums = self._fixture()
        real_delete = final.shutil.rmtree
        count_calls = [0]
        def interrupted(*args, **kwargs):
            count_calls[0] += 1
            if count_calls[0] == 1:
                raise OSError('simulated power loss before source purge')
            return real_delete(*args, **kwargs)
        with patch.object(final.shutil, 'rmtree', side_effect=interrupted):
            count, errors = final.finish_verified_albums(mixie, albums, allow_source_cleanup=True)
        self.assertEqual(count, 0)
        self.assertTrue(errors)
        self.assertIsNotNone(final._state()['active'])
        self.assertFalse(inbox.exists() and stage.exists())
        self.assertFalse(final.resume_cleanup())
        self.assertIsNotNone(final._state()['active'])
        self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assertIsNone(final._state()['active'])
        self.assertEqual(1, len(final._state()['completed']))

    def test_exact_duplicate_deliveries_share_one_original_object_and_both_cleaned(self):
        first = self.inbox / 'NEED messy release'
        second = self.inbox / 'NEED copy'
        for source in (first, second):
            source.mkdir()
        for index, frequency in ((1, 440), (2, 880)):
            track = first / f'{index:02d}.flac'
            subprocess.run(['/usr/bin/ffmpeg', '-nostdin', '-hide_banner',
                            '-loglevel', 'error', '-f', 'lavfi', '-i',
                            f'sine=frequency={frequency}:duration=0.25',
                            '-n', str(track)], check=True, capture_output=True,
                           timeout=15)
            audio = File(track, easy=True)
            for key, value in {'album': 'NEED', 'albumartist': '3OH!3',
                               'artist': '3OH!3', 'title': f'Song {index}',
                               'tracknumber': str(index)}.items():
                audio[key] = [value]
            audio.save()
            shutil.copy2(track, second / track.name)
        sort.sort_inbox()
        names = [sort.staging_import_name(path.name, 'directory') for path in (first, second)]
        self.original_deliveries = {name: {'kind': 'directory', 'tracks': ['01.flac', '02.flac'],
            'payload': {path.name: path.read_bytes() for path in source.iterdir()}}
            for name, source in zip(names, (first, second))}
        albums = {}
        for name in names:
            paths = sorted((self.stage/name).glob('*.flac'))
            albums[(name, 'NEED')] = {'album': 'NEED',
                'tracks': [tag.read_track_metadata(path) for path in paths]}
        index = mixie_batch_duplicates.BatchDuplicates(albums)
        self.assertEqual(1, len(index.representative))
        owner = next(key for key in albums if index.source(key) is None)
        issue = mixie._album_issue(albums[owner])
        library = self.library / '3OH!3' / 'NEED'
        library.mkdir(parents=True)
        output = []
        for i in (1, 2):
            path = library / f'{i:02d} - Song {i}.flac'
            path.write_bytes(('corrected audio ' + str(i)).encode())
            output.append(path)
        cover = library / 'cover.jpg'
        cover.write_bytes(b'approved artwork')
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = mixie.completion_source_manifest(issue)
        sources = [self.stage / issue['import_name'] / f'{i:02d}.flac' for i in (1, 2)]
        receipt = mixie.CommitReceipt({str(p): sha(p) for p in (*output, cover)},
            input_map={f"{issue['import_name']}/{source.name}": {
                'source_sha256': sha(source), 'destination': str(target),
                'destination_sha256': sha(target)} for source, target in zip(sources, output)},
            source_manifest=manifest, asset_outputs={str(cover): sha(cover)},
            evidence={'provenance': 'independently_verified'})
        mixie.record_library_only_import(issue, receipt)
        count, problems = final.finish_verified_albums(mixie, albums, index, allow_source_cleanup=True)
        self.assertEqual((1, []), (count, problems))
        self.assertTrue(all(not (self.stage/name).exists() for name in names))
        self.assertTrue(all(not source.exists() for source in (first, second)))
        records = self.assert_originals()
        self.assertEqual(2, len(records))
        self.assertEqual(records[names[0]], records[names[1]])
        self.assertEqual(1, len(list((self.backup / 'Originals').rglob('*.zip'))))
        self.assertTrue((self.backup / 'Library' / '3OH!3' / 'NEED.zip').is_file())

    def test_library_mutation_blocks_cleanup_even_after_archive(self):
        inbox, stage, music, _cover, albums = self._fixture()
        music.write_bytes(b'something-new-and-unverified')
        count, errors = final.finish_verified_albums(mixie, albums, allow_source_cleanup=True)
        self.assertEqual(0, count)
        self.assertTrue(any('receipt' in error.casefold() for error in errors))
        # Changed library output is actionable recovery work, not silent success.
        self.assertTrue(inbox.exists() and stage.exists())


if __name__ == '__main__':
    unittest.main()
