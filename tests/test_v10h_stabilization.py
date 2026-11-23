"""v10h numbering and terminal presentation regressions (isolated fixtures)."""
import io
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from support import WorkspaceCase, make_audio
import mixie_batch_duplicates as duplicates
import mixie_terminal as terminal
import pretty_runner
import tag


class NumberingRepairTests(WorkspaceCase):
    def test_verified_shuffled_release_reorders_tags_on_copies_for_flac_and_mp3(self):
        for suffix in ('.flac', '.mp3'):
            with self.subTest(suffix=suffix):
                album = f'shuffle-{suffix}'
                sources = []
                working = self.root / ('.mixie-work-' + suffix.lstrip('.'))
                working.mkdir()
                # Deliberately wrong source track numbers: 3, 1, 2.
                for index, incorrect in enumerate((3, 1, 2), 1):
                    source = make_audio(self.stage / album / f'{index:02d}{suffix}',
                                        tracknumber=f'{incorrect}/3', discnumber='1/1',
                                        artist='3OH!3', title=f'Song {index}')
                    sources.append(source)
                matched = {('1', str(i)): source for i, source in enumerate(sources, 1)}
                reference = [{'disc': '1', 'number': str(i)} for i in range(1, 4)]
                original = {source: source.read_bytes() for source in sources}
                with self.assertRaises(tag.MetadataConflictError):
                    tag.propose_release_numbering(matched, reference)
                proposed = tag.propose_release_numbering(
                    matched, reference, independently_verified=True)
                wrong = [p for p in proposed if p['field'] == 'track_number']
                self.assertEqual(3, len(wrong))
                self.assertTrue(all(p['verified_release_position'] is True for p in wrong))
                copies = {}
                for source in sources:
                    dest = working / source.name
                    shutil.copy2(source, dest)
                    copies[source] = dest
                tag.apply_corrections([{**p, 'path': copies[p['path']]} for p in proposed], approved=True)
                for i, source in enumerate(sources, 1):
                    metadata = tag.read_track_metadata(copies[source])
                    self.assertEqual(int(tag.parse_track_number(metadata['track_number'])[0]), i)
                    self.assertEqual(tag.get_track_totals(metadata), {3})
                    self.assertEqual(source.read_bytes(), original[source])

    def test_stale_snapshot_refuses_automatic_position_replacement(self):
        source = make_audio(self.stage / 'stale' / 'song.flac', tracknumber='9/10', discnumber='1/1')
        proposals = tag.propose_release_numbering(
            {('1', '1'): source}, [{'disc': '1', 'number': '1'}], independently_verified=True)
        working = self.root / '.mixie-work-stale'
        working.mkdir()
        target = working / source.name
        shutil.copy2(source, target)
        audio = tag.File(target, easy=True)
        audio['tracknumber'] = ['8/10']
        audio.save()
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed since'):
            tag.apply_corrections([{**p, 'path': target} for p in proposals], approved=True)
        self.assertEqual(target.read_bytes(), before)

    def test_fake_or_mixed_numbering_evidence_fails_closed(self):
        source = make_audio(self.stage / 'bad' / 'song.flac', tracknumber='9')
        match = {('1', '1'): source}
        release = [{'disc': 1, 'number': '1'}]
        with self.assertRaises(ValueError):
            tag.propose_release_numbering(match, release, independently_verified=True,
                reviewed_decision={'field': 'track_number'})
        with self.assertRaises(ValueError):
            tag.propose_release_numbering(match, release, independently_verified='yes')


class PresentationTests(unittest.TestCase):
    def test_bar_frame_matches_length_and_uses_rockbox_gold(self):
        normal = pretty_runner.live_bar(5, 10, 10)
        self.assertEqual('┃' + '█'*5 + '░'*5 + '┃', normal)
        self.assertEqual(len(normal), 12)
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}, clear=False):
            styled = pretty_runner.live_bar(5, 10, 10, tty=True)
            self.assertIn('38;2;220;177;80m', styled)
            self.assertIn('38;2;177;127;244m', styled)
        with patch.dict(os.environ, {'NO_COLOR': '1'}, clear=False):
            self.assertNotIn('\x1b', pretty_runner.live_bar(5, 10, 10, tty=True))

    def test_edition_progress_redraws_same_line_on_tty(self):
        class TTY(io.StringIO):
            def isatty(self): return True
        screen = TTY()
        try:
            with patch.object(terminal.sys, 'stdout', screen), \
                 patch.dict(os.environ, {'TERM': 'xterm-256color'}, clear=False):
                terminal.album_stage('Checking editions', 1, 4)
                terminal.album_stage('Checking editions', 2, 4)
                terminal.album_stage('Checking editions', 3, 4)
                terminal.album_stage('Checking editions', 4, 4)
        finally:
            terminal._finish_edition_progress()
        screen_output = screen.getvalue()
        self.assertEqual(screen_output.count('\x1b[2K'), 4)
        self.assertEqual(screen_output.count('\n'), 1)
        self.assertIn('Checking editions', screen_output)
        self.assertIn('38;2;220;177;80m', screen_output)

    def test_duplicate_sources_grouped_without_deleting_any(self):
        keys = [('Other', 'x'), ('Clash A', 'clash'), ('Later', 'y'), ('Clash B', 'clash')]
        data = {key: {'album': key[1], 'tracks': []} for key in keys}
        class Index:
            def source(self, key):
                return keys[1] if key == keys[3] else None
        output = [key for key, _ in duplicates.ordered_deliveries(data, Index())]
        self.assertEqual([keys[0], keys[1], keys[3], keys[2]], output)
        self.assertEqual(set(keys), set(output))


if __name__ == '__main__':
    unittest.main()
