"""Offline regression guards for read-only album skipping and cleaner output."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import hashlib

import mixie
import mixie_finalizer
import mixie_terminal
import sort


class StressLogHousekeepingTests(TestCase):
    def test_unchanged_album_skip_survives_unrelated_sibling_change(self):
        with TemporaryDirectory() as temp:
            stage = Path(temp) / 'staging'
            artist = stage / 'artist'
            (artist / 'Album A').mkdir(parents=True)
            (artist / 'Album B').mkdir(parents=True)
            song = artist / 'Album A' / 'song.flac'
            song.write_bytes(b'original audio')
            sibling = artist / 'Album B' / 'different.flac'
            sibling.write_bytes(b'changed sibling')
            leaf = 'Album A/song.flac'
            relative = 'artist/' + leaf
            issue = {'import_name':'artist', 'tracks':[relative]}
            expected = hashlib.sha256(song.read_bytes()).hexdigest()
            manifest = {'import_name':'artist', 'payload_hashes':{
                leaf:expected, 'Album B/different.flac':'a'*64}, 'provenance': {'v':1}}
            entry = {'tracks':[relative], 'source_manifest':manifest,
                     'input_map':{relative:{'source_sha256':expected}}}
            with patch.object(sort, 'staging_folder', str(stage)), \
                 patch.object(mixie_finalizer, '_state', return_value={'finalized':{}}), \
                 patch.object(mixie, '_validate_library_only_entry', return_value=True) as validator:
                self.assertTrue(mixie.is_staged_album_already_imported(issue, {'old':entry}))
                validator.assert_called_once_with(entry, issue, manifest)
                song.write_bytes(b'changed audio')
                validator.reset_mock()
                self.assertFalse(mixie.is_staged_album_already_imported(issue, {'old':entry}))
                validator.assert_not_called()

    def test_skip_refuses_source_symlink(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'real').mkdir()
            (root/'staging').mkdir()
            (root/'staging'/'artist').symlink_to(root/'real', target_is_directory=True)
            with patch.object(sort, 'staging_folder', str(root/'staging')):
                self.assertFalse(mixie.is_staged_album_already_imported({
                    'import_name':'artist','tracks':['artist/track.flac']}))

    def test_intermediate_review_checkpoint_never_checks_archive_history(self):
        issue={'album':'Album B','import_name':'artist','tracks':['artist/B.flac']}
        with patch.object(mixie,'load_review_queue',return_value=[]), \
             patch.object(mixie,'_invalidate_stale_choices',return_value=(None,True)), \
             patch.object(mixie,'load_completed_reviews',side_effect=AssertionError('must not inspect completions')), \
             patch.object(mixie,'_verified_imports',side_effect=AssertionError('must not read imports')), \
             patch.object(mixie,'is_review_complete',side_effect=AssertionError('must not verify archives')):
            self.assertEqual([issue],mixie.reconcile_review_queue([issue],checkpoint_only=True))

    def test_track_grouped_output_one_status_per_file(self):
        changes=[('VUKOVI - Fall Better - 01 17359.flac','album_artist','VUKOVI','Vukovi'),
                 ('VUKOVI - Fall Better - 01 17359.flac','date','2020','2020-01-24'),
                 ('VUKOVI - Fall Better - 01 17359.flac','track_total','[missing]','12')]
        output=StringIO()
        with redirect_stdout(output):
            mixie_terminal.show_confirmed_changes(changes, artist='VUKOVI', album='Fall Better')
        shown=output.getvalue()
        self.assertIn('VUKOVI | Fall Better',shown)
        self.assertIn('01. 17359.flac:',shown)
        self.assertIn('Album Artist: VUKOVI → Vukovi',shown)
        self.assertIn('Date: 2020 → 2020-01-24',shown)
        self.assertIn('Track Total: [missing] → 12',shown)
        self.assertEqual(1,shown.count('Status: VERIFIED'))
