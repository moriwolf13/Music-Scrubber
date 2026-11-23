"""Offline, disposable proof: staged inputs define active reviews, not old receipts."""
from contextlib import redirect_stdout
from io import StringIO
from copy import deepcopy
from pathlib import Path
import json
import hashlib
from unittest.mock import patch

import mixie
import mixie_review_ui as ui
import sort
from support import WorkspaceCase, metadata_record


def sha(path):
    return sort.file_checksum(path).hex()


class ReceiptRefreshTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        (self.script / '.mixie-import-first.json').write_text(json.dumps(
            {'version': 1, 'mode': 'verified_library_first'}))
        self.name = 'SyntheticSource'
        self.source_audio = self.track_path('SyntheticSource/Album/01.flac', b'original synthetic music')
        self.extra = self.stage / 'SyntheticSource' / 'Album' / 'booklet.txt'
        self.folder = self.library / 'Artist' / 'Album'
        self.folder.mkdir(parents=True)
        self.output = self.folder / '01 - Song.flac'
        self.output.write_bytes(b'corrected synthetic music')
        self.cover = self.folder / 'cover.jpg'
        self.cover.write_bytes(b'verified cover')
        self.album = {'album': 'Album', 'tracks': [metadata_record(self.source_audio,
            title=('Song',), artist=('Artist',), album=('Album',),
            track_number=('1',), track_total=())]}
        self.issue = mixie._album_issue(self.album)
        self.receipt = mixie.CommitReceipt({str(self.output): sha(self.output), str(self.cover): sha(self.cover)},
            input_map={self.issue['tracks'][0]: {'source_sha256': sha(self.source_audio),
                'destination': str(self.output), 'destination_sha256': sha(self.output)}},
            source_manifest=mixie.completion_source_manifest(self.issue),
            asset_outputs={str(self.cover): sha(self.cover)}, evidence={'provenance': 'independently_verified'})
        mixie.record_library_only_import(self.issue, self.receipt)
        self.key = next(iter(mixie._verified_imports()))
        self.albums = {(self.name, 'Album'): self.album}

    def test_current_verified_receipt_not_reset_or_rewritten(self):
        before = (self.script / '.verified_imports.json').read_bytes()
        self.assertEqual((0, 0), mixie._refresh_staged_import_receipts(self.albums))
        self.assertEqual(before, (self.script / '.verified_imports.json').read_bytes())
        self.assertFalse((self.script / '.retired_import_receipts.json').exists())

    def test_non_audio_sibling_change_rebases_only_after_full_source_and_output_proof(self):
        # Unmanaged staging fixture deliberately has no immutable original
        # delivery provenance. The album's original track remains identical.
        self.extra.write_bytes(b'new unrelated booklet')
        self.assertFalse(mixie.is_library_import_verified(self.issue))
        with redirect_stdout(StringIO()):
            self.assertEqual((1, 0), mixie._refresh_staged_import_receipts(self.albums))
        updated = mixie._verified_imports()
        self.assertEqual(1, len(updated))
        self.assertNotIn(self.key, updated)
        self.assertTrue(mixie.is_library_import_verified(self.issue))
        history = json.loads((self.script / '.retired_import_receipts.json').read_text())
        self.assertEqual(self.receipt.source_manifest, history['entries'][self.key]['source_manifest'])
        self.assertEqual(sha(self.source_audio), updated[next(iter(updated))]['input_map'][self.issue['tracks'][0]]['source_sha256'])
        self.assertTrue(self.source_audio.exists() and self.output.exists())

    def test_changed_source_audio_retires_claim_without_touching_music(self):
        original_library = self.output.read_bytes()
        self.source_audio.write_bytes(b'new unrelated audio data')
        with redirect_stdout(StringIO()):
            self.assertEqual((0, 1), mixie._refresh_staged_import_receipts(self.albums))
        self.assertEqual({}, mixie._verified_imports())
        self.assertEqual(original_library, self.output.read_bytes())
        self.assertTrue(self.source_audio.exists())
        self.assertIn(self.key, json.loads((self.script / '.retired_import_receipts.json').read_text())['entries'])

    def test_changed_library_output_retires_claim_but_never_overwrites_user_edit(self):
        self.output.write_bytes(b'user edited library music')
        with redirect_stdout(StringIO()):
            self.assertEqual((0, 1), mixie._refresh_staged_import_receipts(self.albums))
        self.assertEqual({}, mixie._verified_imports())
        self.assertEqual(b'user edited library music', self.output.read_bytes())
        protected = mixie._receipt_protected_destinations()
        self.assertIn(self.receipt[str(self.output)], protected[str(self.output)])

    def test_corrupt_retired_history_never_resets_live_receipt(self):
        self.source_audio.write_bytes(b'changed input')
        (self.script / '.retired_import_receipts.json').write_text('invalid json')
        before = (self.script / '.verified_imports.json').read_bytes()
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            mixie._refresh_staged_import_receipts(self.albums)
        self.assertEqual(before, (self.script / '.verified_imports.json').read_bytes())

    def test_import_receipt_from_other_delivery_is_not_touched(self):
        before = (self.script / '.verified_imports.json').read_bytes()
        self.assertEqual((0, 0), mixie._refresh_staged_import_receipts({}))
        self.assertEqual(before, (self.script / '.verified_imports.json').read_bytes())

    def test_fresh_scan_replaces_old_problem_without_reintroducing_receipt_review(self):
        old = {**self.issue, 'status': 'recovery_required', 'reason': 'old receipt failure',
               'details': {'kind': 'recovery_required'}}
        mixie.save_review_queue([old])
        self.output.write_bytes(b'user changed output')
        with redirect_stdout(StringIO()):
            mixie._refresh_staged_import_receipts(self.albums)
            pending = mixie.reconcile_review_queue([], rescanned={
                (self.issue['import_name'], tuple(sorted(self.issue['tracks'])))} )
        self.assertEqual([], pending)
        self.assertEqual(b'user changed output', self.output.read_bytes())

    def test_valid_receipt_produces_archive_pending_not_metadata_review(self):
        with redirect_stdout(StringIO()):
            pending = mixie.reconcile_review_queue([])
        self.assertEqual(1, len(pending))
        self.assertEqual('archive_pending', pending[0]['status'])
        self.assertFalse(ui._needs_metadata_review(pending[0]))

    def test_source_bound_missing_total_is_derived_despite_recovery_status(self):
        row1 = metadata_record(self.source_audio, title=('Song',), artist=('Artist',),
                               album=('Album',), track_number=('1',), track_total=())
        second = self.track_path('SyntheticSource/Album/02.flac', b'other song')
        row2 = metadata_record(second, title=('Song 2',), artist=('Artist',),
                               album=('Album',), track_number=('2',), track_total=())
        issue = {**self.issue, 'details': {'kind': 'recovery_required'}, 'status': 'recovery_required',
                 'tracks': [self.issue['tracks'][0], 'SyntheticSource/Album/02.flac']}
        with patch.object(ui, '_view', return_value={issue['tracks'][0]:row1,issue['tracks'][1]:row2}):
            issues = ui.preflight_issues(mixie, issue)
        self.assertFalse(any('Track Total missing' in line for line in issues), issues)
