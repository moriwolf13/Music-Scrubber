from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import json
import shutil
import zipfile

import mixie
import sort
import tag
import constants


def test_case(is_folder, mode):
    kind = 'FOLDER' if is_folder else 'ZIP'
    with TemporaryDirectory(prefix='mixie-purge-fixture-') as tmp:
        root = Path(tmp)
        backup = root / constants.backup_folder_name
        inbox = root / 'Inbox'
        staging = root / '.staging'
        library = root / 'Music Library'
        for path in (backup, inbox, staging, library):
            path.mkdir(parents=True)

        name = 'Fixture Import'
        source_name = name if is_folder else name + '.zip'
        folder = backup / '.cleanup-quarantine' / '.mixie-inbox-fixture'
        held_stage = folder / 'staging'
        album = held_stage / 'Fixture Album'
        album.mkdir(parents=True)
        (album / '01.mp3').write_bytes(b'DUMMY ORIGINAL AUDIO')
        (album / 'cover.jpg').write_bytes(b'DUMMY ORIGINAL COVER')
        stage_hashes = sort.file_manifest(held_stage)

        held_source = folder / 'original'
        if is_folder:
            shutil.copytree(held_stage, held_source)
            source_hashes = sort.file_manifest(held_source)
            source_sha = None
        else:
            with zipfile.ZipFile(held_source, 'w') as archive:
                for relative in sorted(stage_hashes):
                    archive.write(held_stage / relative, relative)
            source_hashes = sort.zip_manifest(held_source)
            source_sha = sort.file_checksum(held_source).hex()

        bulk = backup / 'OriginalDownloads-Bulk.zip'
        with zipfile.ZipFile(bulk, 'w') as archive:
            if is_folder:
                for relative in sorted(source_hashes):
                    archive.write(held_source / relative, f'{source_name}/{relative}')
            else:
                archive.write(held_source, source_name)
        bulk_before = sort.file_checksum(bulk).hex()

        finished = library / 'Artist' / 'Fixture Album'
        finished.mkdir(parents=True)
        for filename in ('01.mp3', 'cover.jpg'):
            (finished / filename).write_bytes(b'FINISHED ' + filename.encode())
        receipt = {
            str(finished / filename): sort.file_checksum(finished / filename).hex()
            for filename in ('01.mp3', 'cover.jpg')
        }
        snapshot = backup / 'MusicLibrary-fixture.zip'
        with zipfile.ZipFile(snapshot, 'w') as archive:
            for relative in sorted(sort.file_manifest(library)):
                archive.write(library / relative, relative)
        snapshot_before = sort.file_checksum(snapshot).hex()

        identity = {
            'import_name': name,
            'tracks': [[f'{name}/Fixture Album/01.mp3', stage_hashes['Fixture Album/01.mp3']]]
        }
        fingerprint = hashlib.sha256(json.dumps(
            identity, sort_keys=True, ensure_ascii=False
        ).encode('utf-8')).hexdigest()
        (root / '.completed_reviews.json').write_text(json.dumps({
            'version': 1,
            'entries': {fingerprint: {
                'album': 'Fixture Album',
                'receipt': receipt,
                'originals_archive': str(bulk),
                'library_archive': str(snapshot)
            }}
        }), encoding='utf-8')
        (folder / 'recovery.json').write_text(json.dumps({
            'version': 1,
            'kind': 'inbox_cleanup',
            'import_name': name,
            'source_name': source_name,
            'source_is_dir': is_folder,
            'source_sha256': source_sha,
            'source_hashes': source_hashes,
            'staged_hashes': stage_hashes
        }), encoding='utf-8')

        with (
            patch.object(sort, 'script_folder', root),
            patch.object(sort, 'inbox_folder', inbox),
            patch.object(sort, 'staging_folder', staging),
            patch.object(sort, 'music_library_folder', library),
            patch.object(tag, 'script_folder', root),
            patch.object(mixie, 'load_review_queue', return_value=[]),
            patch.object(tag, 'read_track_metadata',
                         return_value={'album': ('Fixture Album',)})
        ):
            if mode == 'corrupt':
                with zipfile.ZipFile(bulk, 'w') as archive:
                    archive.writestr('unrelated.txt', b'CORRUPTED')
                try:
                    mixie.purge_verified_inbox_quarantine(approved=True)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('Corrupted backup was accepted')
                assert sort.file_manifest(held_stage) == stage_hashes
                assert held_source.exists()
                assert (folder / 'recovery.json').is_file()
                assert not (folder / 'purge-state.json').exists()
                print(f'PASS: {kind} corrupted archive refused; copies retained')
                return

            ready = mixie.verify_inbox_purge_candidates()
            assert len(ready) == 1 and ready[0]['name'] == name
            assert ready[0]['files'] == 2 and ready[0]['albums'] == 1
            preview = mixie.purge_verified_inbox_quarantine()
            assert preview == [folder]
            assert sort.file_manifest(held_stage) == stage_hashes

            if mode in ('stage_crash', 'original_crash'):
                if mode == 'stage_crash':
                    target = album / 'cover.jpg'
                elif is_folder:
                    target = held_source / 'Fixture Album' / 'cover.jpg'
                else:
                    target = held_source
                real_unlink = Path.unlink
                triggered = [False]

                def fail_once(self, *args, **kwargs):
                    if self == target and not triggered[0]:
                        triggered[0] = True
                        raise OSError('Simulated interruption')
                    return real_unlink(self, *args, **kwargs)

                with patch.object(Path, 'unlink', fail_once):
                    try:
                        mixie.purge_verified_inbox_quarantine(approved=True)
                    except OSError as exc:
                        assert str(exc) == 'Simulated interruption'
                    else:
                        raise AssertionError('Expected deletion interruption')
                assert triggered[0]
                assert (folder / 'purge-state.json').is_file()
                print(f'PASS: {kind} {mode} detected; resuming')

            elif mode == 'finalize_crash':
                real_rename = Path.rename
                triggered = [False]

                def fail_rename(self, target):
                    if self == folder and not triggered[0]:
                        triggered[0] = True
                        raise OSError('Simulated finalization crash')
                    return real_rename(self, target)

                with patch.object(Path, 'rename', fail_rename):
                    try:
                        mixie.purge_verified_inbox_quarantine(approved=True)
                    except OSError as exc:
                        assert str(exc) == 'Simulated finalization crash'
                    else:
                        raise AssertionError('Expected finalization interruption')
                assert triggered[0]
                assert not held_stage.exists() and not held_source.exists()
                assert (folder / 'purge-state.json').is_file()
                print(f'PASS: {kind} finalize crash detected; resuming')

            done = mixie.purge_verified_inbox_quarantine(approved=True)
            assert done == [name]
            assert not folder.exists()
            assert not (inbox / source_name).exists()
            assert not (staging / name).exists()
            assert sort.file_checksum(bulk).hex() == bulk_before
            assert sort.file_checksum(snapshot).hex() == snapshot_before
            evidence = list((backup / '.cleanup-purge-receipts').glob('*.evidence'))
            assert len(evidence) == 1
            assert (evidence[0] / 'recovery.json').is_file()
            assert (evidence[0] / 'purge-state.json').is_file()
            assert not (evidence[0] / 'original').exists()
            assert not (evidence[0] / 'staging').exists()
            assert mixie.purge_verified_inbox_quarantine() == []
            print(f'PASS: {kind} {mode}; backups intact, repeat run empty')


for is_folder in (False, True):
    for mode in ('normal', 'stage_crash', 'original_crash', 'finalize_crash', 'corrupt'):
        print(f'\nTEST: {"FOLDER" if is_folder else "ZIP"} {mode}')
        test_case(is_folder, mode)

print('\nALL TEN INBOX PURGE TESTS PASSED')
print('No real Inbox, staging, library, or backup files were touched.')
