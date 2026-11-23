"""Retired completion history cannot veto a second verified delivery of the same album."""
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
import unittest

from support import WorkspaceCase, make_audio
import mixie
import mixie_library_merge as merge
import sort


class DuplicateAlbumHistoryTests(WorkspaceCase):
    def copies(self):
        first = self.stage / 'Clash from release A' / '01.flac'
        second = self.stage / 'Clash from discography' / '01.flac'
        destination = self.library / 'Caravan Palace' / 'Clash' / '01 - Clash.flac'
        make_audio(first, title='Clash', album='Clash', artist='Caravan Palace',
                   albumartist='Caravan Palace', date='2012', genre='Electro Swing', tracknumber='1')
        make_audio(second, title='Clash', album='Clash', artist='Caravan Palace',
                   albumartist='Caravan Palace', date='2011', genre='Electro Swing', tracknumber='1')
        destination.parent.mkdir(parents=True, exist_ok=True)
        make_audio(destination, title='Clash', album='Clash', artist='Caravan Palace',
                   albumartist='Caravan Palace', date='2012', genre='Electro Swing', tracknumber='1')
        self.assertTrue(merge.identical_encoded_audio(first, second))
        self.assertTrue(merge.identical_encoded_audio(first, destination))
        return first, second, destination

    def test_retired_receipt_hash_does_not_veto_active_receipt(self):
        first, second, destination = self.copies()
        good_hash = sort.file_checksum(destination).hex()
        historical_hash = '03' * 32
        ledger = {
            'history:old-claim': {'receipt': {str(destination): historical_hash}},
            'current-claim': {'receipt': {str(destination): good_hash}},
        }
        with patch.object(mixie, 'load_completed_reviews', return_value=ledger):
            protected = mixie._receipt_protected_destinations()
        self.assertEqual({good_hash}, protected[str(destination)])
        before = sort.file_checksum(destination)
        before_source = sort.file_checksum(second)
        with redirect_stdout(StringIO()):
            result = merge.commit_verified_audio_plan([
                {'source': str(second), 'destination': str(destination)}
            ], [destination], protected=protected)
        self.assertEqual(before, sort.file_checksum(destination))
        self.assertEqual(before_source, sort.file_checksum(second))
        self.assertIn(str(destination), result.reused_stream)
        self.assertFalse(result.upgraded)
        self.assertFalse(result.created)

    def test_active_receipt_mismatch_still_blocks_duplicate(self):
        _, source, destination = self.copies()
        ledger = {
            'history:retired': {'receipt': {str(destination): sort.file_checksum(destination).hex()}},
            'current': {'receipt': {str(destination): '03'*32}},
        }
        with patch.object(mixie, 'load_completed_reviews', return_value=ledger):
            protected = mixie._receipt_protected_destinations()
        before = sort.file_checksum(destination)
        with self.assertRaisesRegex(RuntimeError, 'completed receipt'):
            merge.commit_verified_audio_plan([
                {'source': str(source), 'destination': str(destination)}
            ], [destination], protected=protected)
        self.assertEqual(before, sort.file_checksum(destination))

    def test_conflicting_active_receipts_still_block(self):
        _, source, destination = self.copies()
        good_hash = sort.file_checksum(destination).hex()
        ledger = {
            'claim1': {'receipt': {str(destination): good_hash}},
            'claim2': {'receipt': {str(destination): '02'*32}},
            'history:retired': {'receipt': {str(destination): 'ff'*32}},
        }
        with patch.object(mixie, 'load_completed_reviews', return_value=ledger):
            protected = mixie._receipt_protected_destinations()
        self.assertEqual(2, len(protected[str(destination)]))
        with self.assertRaisesRegex(RuntimeError, 'completed receipt'):
            merge.commit_verified_audio_plan([
                {'source': str(source), 'destination': str(destination)}
            ], [destination], protected=protected)

    def test_all_retired_receipts_grant_no_completion_authority(self):
        _, source, destination = self.copies()
        ledger = {'history:old-only': {'receipt': {str(destination): '02'*32}}}
        with patch.object(mixie, 'load_completed_reviews', return_value=ledger):
            protected = mixie._receipt_protected_destinations()
        self.assertNotIn(str(destination), protected)
        before_source = sort.file_checksum(source)
        with redirect_stdout(StringIO()):
            result = merge.commit_verified_audio_plan([
                {'source': str(source), 'destination': str(destination)}
            ], [destination], protected=protected)
        self.assertEqual(before_source, sort.file_checksum(source))
        self.assertEqual(1, len(result.already) + len(result.upgraded))

if __name__ == '__main__':
    unittest.main()
