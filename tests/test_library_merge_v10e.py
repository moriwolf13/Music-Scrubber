"""Existing library audio is reusable without trusting tag-only file checksums.

These fixtures contain synthetically encoded silence and never contact providers.
"""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import subprocess
import unittest

from support import WorkspaceCase, make_audio
import mixie
import mixie_library_merge as merge
import mixie_review_ui as ui
import sort
import tag


class LibraryMergeV10ETests(WorkspaceCase):
    def audio_pair(self, *, ext='.flac'):
        source = self.stage / 'Import' / ('01' + ext)
        make_audio(source, title='Verified Album Title', album='Album',
                   artist='Artist', albumartist='Artist', date='2024', genre='Rock',
                   tracknumber='1')
        destination = self.library / 'Artist' / 'Album' / ('01' + ext)
        destination.parent.mkdir(parents=True, exist_ok=True)
        make_audio(destination, title='Old Title', album='Album',
                   artist='Artist', albumartist='Artist', date='2020', genre='Unclassified',
                   tracknumber='1')
        return source, destination

    def test_metadata_only_collision_updates_existing_without_touching_source(self):
        for ext in ('.flac', '.mp3'):
            with self.subTest(ext=ext):
                source, destination = self.audio_pair(ext=ext)
                original_source = sort.file_checksum(source)
                original_library = sort.file_checksum(destination)
                self.assertNotEqual(original_source, original_library)
                self.assertTrue(merge.identical_encoded_audio(source, destination))
                with redirect_stdout(StringIO()):
                    result = merge.commit_verified_audio_plan(
                        [{'source': str(source), 'destination': str(destination)}],
                        [destination])
                self.assertEqual([str(destination)], result.upgraded)
                self.assertEqual(sort.file_checksum(source), sort.file_checksum(destination))
                self.assertEqual(original_source, sort.file_checksum(source))
                self.assertIsNotNone(result.backup)
                self.assertEqual(original_library, sort.file_checksum(result._previous[str(destination)][0]))
                self.assertEqual(('Verified Album Title',), tag.read_track_metadata(destination)['title'])
                result.rollback()
                self.assertEqual(original_library, sort.file_checksum(destination))

    def test_completed_receipt_reuses_same_audio_without_invalidating_old_bytes(self):
        source, destination = self.audio_pair()
        previous = sort.file_checksum(destination)
        with redirect_stdout(StringIO()):
            result = merge.commit_verified_audio_plan(
                [{'source': str(source), 'destination': str(destination)}],
                [destination], protected={str(destination): {previous.hex()}})
        self.assertEqual({str(destination)}, result.reused_stream)
        self.assertFalse(result.upgraded)
        self.assertEqual(previous, sort.file_checksum(destination))
        self.assertIsNone(result.backup)

    def test_different_audio_remains_conflict_and_never_overwrites(self):
        source, destination = self.audio_pair()
        before = sort.file_checksum(destination)
        # Different actual encoded recording, not just different tags.
        destination.unlink()
        subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error',
                        '-f','lavfi','-i','sine=frequency=1000:duration=0.15',
                        '-n',str(destination)], check=True, capture_output=True, timeout=15)
        before = sort.file_checksum(destination)
        with self.assertRaisesRegex(FileExistsError, 'Different audio'):
            merge.commit_verified_audio_plan(
                [{'source': str(source), 'destination': str(destination)}], [destination])
        self.assertEqual(before, sort.file_checksum(destination))
        self.assertFalse(list(self.backup.glob('library-metadata-upgrade-*')))

    def test_different_booklet_is_preserved_while_missing_music_imports(self):
        source, dest = self.audio_pair()
        extrafile = self.library / 'Artist' / 'Album' / 'Booklet.pdf'
        extrafile.write_bytes(b'previous booklet')
        other = self.stage / 'new-booklet.pdf'
        other.write_bytes(b'new booklet')
        pending = self.library / 'Artist' / 'Album' / 'new.flac'
        with redirect_stdout(StringIO()):
            result = merge.commit_verified_audio_plan([
                {'source': str(source), 'destination': str(pending)},
                {'source': str(other), 'destination': str(extrafile)}
            ], [pending])
        self.assertTrue(pending.is_file())
        self.assertIn(str(extrafile), result.reused_assets)
        self.assertEqual(b'previous booklet', extrafile.read_bytes())

    def test_custom_replaygain_metadata_is_not_discarded_by_auto_upgrade(self):
        source, dest = self.audio_pair()
        from mutagen.flac import FLAC
        track = FLAC(dest)
        track['replaygain_track_gain'] = ['-4.2 dB']
        track.save()
        before = sort.file_checksum(dest)
        with redirect_stdout(StringIO()):
            result = merge.commit_verified_audio_plan(
                [{'source': str(source), 'destination': str(dest)}], [dest])
        self.assertEqual(before, sort.file_checksum(dest))
        self.assertEqual(('-4.2 dB',), tuple(FLAC(dest)['replaygain_track_gain']))
        self.assertIn(str(dest), result.reused_stream)
        self.assertFalse(result.upgraded)

    def test_wrong_protected_hash_blocks_even_if_same_audio(self):
        source, dest = self.audio_pair()
        with self.assertRaisesRegex(RuntimeError, 'completed receipt'):
            merge.commit_verified_audio_plan(
                [{'source': str(source), 'destination': str(dest)}], [dest],
                protected={str(dest): {'00' * 32}})

    def test_archive_pending_does_not_appear_as_review_task(self):
        issues = [{'album': 'EVERGREEN', 'tracks': [], 'import_name':'Import',
                   'status':'archive_pending', 'details':{'kind':'completion'},
                   'reason':'Library import verified; archive pending: original download moved'}]
        self.assertTrue(mixie._archive_pending(issues[0]))
        with patch.object(ui, 'input', side_effect=['0']), redirect_stdout(StringIO()) as out:
            ui.review_manager_menu(mixie, issues)
        self.assertIn('Albums Needing Review (0)', out.getvalue())
        self.assertIn('waiting for archive verification', out.getvalue())

    def test_live_review_queue_skip_visits_each_without_bouncing(self):
        issues = [{'album': value, 'tracks': [], 'import_name':value, 'reason':'test'}
                  for value in ('EVERGREEN', 'Chronologic', 'Clash', 'NEED')]
        called = []
        def fake_editor(_service, entry, _issues):
            called.append(entry['album'])
            return 'next'
        with patch.object(ui,'_album_editor',side_effect=fake_editor), \
             patch.object(ui,'input',side_effect=['2','0']), \
             redirect_stdout(StringIO()):
            ui._list(mixie, issues, False)
        self.assertEqual(['Chronologic', 'Clash', 'NEED', 'EVERGREEN'], called)

if __name__ == '__main__':
    unittest.main()
