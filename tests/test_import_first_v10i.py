"""Import-first receipts never replace mandatory album archive proof."""
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import mixie
import mixie_finalizer as final
from support import WorkspaceCase, make_audio
import mixie_library_merge
import shutil
from mutagen import File


class ImportFirstTests(WorkspaceCase):
    def _enable(self):
        (self.script / '.mixie-import-first.json').write_text(
            json.dumps({'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')

    def _receipt(self):
        source = self.track_path('Example/Album/01.flac', b'synthetic-original-audio-source')
        music = self.library / 'Artist' / 'Album' / '01 - Song.flac'
        cover = self.library / 'Artist' / 'Album' / 'cover.jpg'
        music.parent.mkdir(parents=True, exist_ok=True)
        music.write_bytes(b'synthetic-tagged-output-audio')
        cover.write_bytes(b'synthetic-cover')
        issue = {'import_name': 'Example', 'album': 'Album',
                 'tracks': ['Example/Album/01.flac']}
        manifest = mixie.completion_source_manifest(issue)
        checksum = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        receipt = mixie.CommitReceipt(
            {str(music): checksum(music), str(cover): checksum(cover)},
            input_map={'Example/Album/01.flac': {
                'source_sha256': checksum(source),
                'destination': str(music), 'destination_sha256': checksum(music)}},
            source_manifest=manifest, asset_outputs={str(cover): checksum(cover)},
            evidence={'provenance': 'independently_verified'})
        return issue, receipt, music, cover

    def test_library_import_persists_but_queue_waits_for_both_verified_archives(self):
        self._enable()
        issue, receipt, music, cover = self._receipt()
        identity = mixie.record_library_only_import(issue, receipt)
        self.assertTrue(mixie.is_library_import_verified(issue))
        self.assertIn(identity, mixie._verified_imports())
        # Import-only evidence must guard against future silent retagging too.
        self.assertIn(receipt[str(music)],
                      mixie._receipt_protected_destinations()[str(music)])
        self.assertFalse((self.backup / 'OriginalDownloads-Bulk.zip').exists())
        mixie.save_review_queue([{**issue, 'status': 'archive_pending', 'reason': 'old ZIP failed'}])
        pending = mixie.reconcile_review_queue([])
        self.assertEqual(1, len(pending))
        self.assertEqual('archive_pending', pending[0]['status'])
        self.assertEqual(issue['tracks'], pending[0]['tracks'])
        self.assertFalse(final.is_album_finalized(mixie, issue))
        # The local receipt is neither an archive nor cleanup authorization.
        self.assertEqual({}, mixie.load_completed_reviews())
        self.assertTrue(music.is_file() and cover.is_file())

    def test_changed_library_bytes_invalidate_import_only_receipt(self):
        self._enable()
        issue, receipt, music, cover = self._receipt()
        mixie.record_library_only_import(issue, receipt)
        music.write_bytes(b'manually-modified-library-output')
        self.assertFalse(mixie.is_library_import_verified(issue))
        with self.assertRaisesRegex(RuntimeError, 'Existing verified import receipt differs'):
            changed = mixie.CommitReceipt(
                {**receipt, str(music): hashlib.sha256(music.read_bytes()).hexdigest()},
                input_map={**receipt.input_map, 'Example/Album/01.flac': {
                    **receipt.input_map['Example/Album/01.flac'],
                    'destination_sha256': hashlib.sha256(music.read_bytes()).hexdigest()}},
                source_manifest=receipt.source_manifest, asset_outputs=receipt.asset_outputs)
            mixie.record_library_only_import(issue, changed)

    def test_source_payload_change_invalidates_receipt(self):
        self._enable()
        issue, receipt, music, cover = self._receipt()
        mixie.record_library_only_import(issue, receipt)
        (self.stage / issue['tracks'][0]).write_bytes(b'changed-original')
        self.assertFalse(mixie.is_library_import_verified(issue))

    def test_backup_action_is_manual_and_never_creates_original_bulk_zip(self):
        self._enable()
        self._receipt()
        with patch.object(mixie.sys, 'argv', ['mixie.py']):
            mixie._main('5')
        library_zip = list(self.backup.glob('MusicLibrary-*.zip'))
        self.assertEqual(len(library_zip), 1)
        self.assertFalse((self.backup / 'OriginalDownloads-Bulk.zip').exists())
        self.assertTrue((self.stage / 'Example/Album/01.flac').exists())

    def test_ingest_retains_archive_pending_without_provenance_and_never_reimports(self):
        self._enable()
        issue, receipt, music, cover = self._receipt()
        album = {'album': 'Album', 'tracks': [{'path': self.stage / issue['tracks'][0]}]}
        with patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(mixie.sort, 'sort_inbox'), \
             patch.object(mixie.tag, 'group_staged_albums',
                          return_value={('Example', 'Album'): album}), \
             patch.object(mixie, 'preview_album', return_value={'provenance': 'independently_verified'}) as preview, \
             patch.object(mixie, 'test_album_plan', return_value=receipt) as processor, \
             patch.object(mixie.archive, 'update_original_downloads_archive',
                          side_effect=AssertionError('Auto archive was called!')), \
             patch.object(mixie.archive, 'create_verified_zip',
                          side_effect=AssertionError('Auto library archive was called!')), \
             patch.object(mixie, 'auto_quarantine_archived_completed',
                          side_effect=AssertionError('Auto cleanup was called!')), \
             patch.object(mixie, 'auto_purge_verified_quarantines',
                          side_effect=AssertionError('Auto purge was called!')):
            mixie._main('1')
            self.assertTrue(mixie.is_library_import_verified(issue))
            self.assertFalse(final.is_album_finalized(mixie, issue))
            self.assertEqual(['archive_pending'], [row['status'] for row in mixie.load_review_queue()])
            mixie._main('1')
            self.assertEqual(['archive_pending'], [row['status'] for row in mixie.load_review_queue()])
            self.assertEqual(1, preview.call_count)
            self.assertEqual(1, processor.call_count)
        self.assertTrue(music.exists() and cover.exists())
        self.assertTrue((self.stage / issue['tracks'][0]).exists())
        self.assertFalse((self.backup / 'OriginalDownloads-Bulk.zip').exists())

    def test_approved_review_import_stays_archive_pending_without_finalization_proof(self):
        self._enable()
        issue, receipt, music, cover = self._receipt()
        pending = [issue]
        with patch.object(mixie.tag, 'group_staged_albums', return_value={
                ('Example', 'Album'): {'album': 'Album', 'tracks': [
                    {'path': self.stage / issue['tracks'][0]}]}}), \
             patch.object(mixie, 'test_review_decision', return_value=receipt), \
             patch.object(mixie.archive, 'update_original_downloads_archive',
                          side_effect=AssertionError('Unexpected archive')), \
             patch.object(mixie.archive, 'create_verified_zip',
                          side_effect=AssertionError('Unexpected ZIP')), \
             patch.object(mixie, 'auto_quarantine_archived_completed',
                          side_effect=AssertionError('Unexpected cleanup')):
            self.assertFalse(mixie.finalize_reviewed_album(issue, pending))
        self.assertEqual([issue], pending)
        self.assertEqual('archive_pending', issue['status'])
        saved = mixie.load_review_queue()
        self.assertEqual(1, len(saved))
        self.assertEqual('archive_pending', saved[0]['status'])
        self.assertEqual(issue['tracks'], saved[0]['tracks'])
        self.assertTrue(mixie.is_library_import_verified(issue))
        self.assertFalse(final.is_album_finalized(mixie, issue))
        self.assertTrue((self.stage / issue['tracks'][0]).exists())

    def test_stale_historical_receipt_never_rewrites_user_tags(self):
        self._enable()
        source = make_audio(self.stage / 'Example' / 'Album' / '01.flac',
                            title='Canonical', artist='Artist', album='Album')
        destination = self.library / 'Artist' / 'Album' / '01 - Canonical.flac'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        audio = File(destination, easy=True)
        audio['title'] = ['User corrected title']
        audio.save()
        old_bytes = destination.read_bytes()
        plan = [{'source': str(source), 'destination': str(destination)}]
        stale = {str(destination): {'0' * 64}}
        with self.assertRaisesRegex(RuntimeError, 'completed receipt disagrees'):
            mixie_library_merge.commit_verified_audio_plan(plan, [str(destination)], protected=stale)
        result = mixie_library_merge.commit_verified_audio_plan(
            plan, [str(destination)], protected=stale, allow_stale_receipt_reuse=True)
        self.assertIn(str(destination), result.reused_stream)
        self.assertEqual(destination.read_bytes(), old_bytes)
        self.assertEqual(File(destination, easy=True)['title'], ['User corrected title'])

    def test_old_installations_stay_on_original_mode(self):
        self.assertFalse(mixie._import_first_enabled())
        self.assertEqual({}, mixie._verified_imports())


if __name__ == '__main__':
    unittest.main()
