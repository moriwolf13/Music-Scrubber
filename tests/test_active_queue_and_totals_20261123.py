"""Never review removed audio; show local contiguous totals without claiming release proof."""
import json
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import mixie
from support import WorkspaceCase


class ActiveQueueTests(WorkspaceCase):
    def issue(self, label, count=2):
        return {'import_name': 'The Offspring', 'album': label,
                'tracks': [f'The Offspring/{label}/track-{index}.flac'
                           for index in range(1, count + 1)],
                'reason': 'Review required',
                'details': {'kind': 'identity_release'},
                'manual_fields': {'date': '1998'}}

    def test_deleted_album_is_retired_without_erasing_active_sibling(self):
        old = self.issue('Americana (ESCA 7393)')
        active = self.issue('Americana 14-track')
        self.track_path(active['tracks'][0], b'audio')
        with redirect_stdout(StringIO()):
            result = mixie._retire_absent_review_issues([old, active])
        self.assertEqual([active], result)
        history = json.loads((self.script / '.retired_reviews.json').read_text())
        self.assertEqual([old], history['issues'])
        self.assertNotIn('import', old)
        self.assertFalse((self.script / '.needs_review.json').exists())

    def test_retirement_is_idempotent_and_preserves_saved_choices(self):
        old = self.issue('Removed')
        with redirect_stdout(StringIO()):
            mixie._retire_absent_review_issues([old])
            mixie._retire_absent_review_issues([old])
        history = json.loads((self.script / '.retired_reviews.json').read_text())
        self.assertEqual(1, len(history['issues']))
        self.assertEqual(old['manual_fields'], history['issues'][0]['manual_fields'])

    def test_partially_missing_album_is_retained_for_recovery(self):
        old = self.issue('Incomplete')
        self.track_path(old['tracks'][0], b'audio')
        self.assertEqual([old], mixie._retire_absent_review_issues([old]))
        self.assertFalse((self.script / '.retired_reviews.json').exists())

    def test_missing_staged_album_with_import_receipt_is_retired_from_active_not_history(self):
        old = self.issue('Imported')
        evidence = {'fake': {'source_manifest': {'import_name': old['import_name']},
                             'tracks': old['tracks']}}
        with patch.object(mixie, '_verified_imports', return_value=evidence):
            with redirect_stdout(StringIO()):
                self.assertEqual([], mixie._retire_absent_review_issues([old]))
        self.assertEqual([old], json.loads((self.script / '.retired_reviews.json').read_text())['issues'])

    def test_unreadable_receipts_and_retirement_history_fail_closed(self):
        old = self.issue('Unclear')
        with patch.object(mixie, '_verified_imports', side_effect=ValueError('corrupt')):
            self.assertEqual([old], mixie._retire_absent_review_issues([old]))
        (self.script / '.retired_reviews.json').write_text('not json')
        with self.assertRaises(ValueError):
            mixie._retire_absent_review_issues([old])
