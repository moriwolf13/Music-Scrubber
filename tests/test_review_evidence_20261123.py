"""Offline digest of the three problematic Nov 23 review reports (no audio/network).

The fixture stores only provider titles, versions, duration differences and
release sequences. It does not prove that the physical audio equals a specific
recording; it verifies matching decisions on the user's saved provider evidence.
"""
import json
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import lookup

FIXTURE = Path(__file__).parent / 'fixtures' / 'review_evidence_20261123.json'


class LiveReviewEvidenceDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = json.loads(FIXTURE.read_text())

    def rows(self, name):
        case = self.evidence[name]
        return deepcopy(case['audits']), deepcopy(case['releases'])

    def test_strange_case_music_first_finds_complete_standard_sequence(self):
        audits, releases = self.rows('The Strange Case of...')
        standard = next(rid for rid, tracks in releases.items() if len(tracks) == 12)
        with patch.object(lookup, 'get_release_tracklist', side_effect=lambda rid: releases[rid]):
            candidate = lookup.evaluate_release_candidate(audits, standard)
            evaluations = [lookup.evaluate_release_candidate(audits, rid) for rid in releases]
            consensus = lookup.compare_release_candidate_reports(
                audits, evaluations, standard_album_preference=True)
        self.assertEqual('complete', candidate['status'])
        self.assertEqual(12, len(candidate['matched']))
        self.assertEqual('complete', consensus['status'])
        self.assertGreaterEqual(len(consensus['compatible_release_ids']), 7)
        self.assertEqual([], consensus['reasons'])

    def test_strange_case_radio_edit_near_identical_would_stay_review(self):
        audits, releases = self.rows('The Strange Case of...')
        for audit in audits:
            if any(row['details']['title'] == 'Here’s to Us (radio edit)'
                   for row in audit['assessment']['candidates']):
                edit = next(row for row in audit['assessment']['candidates']
                            if row['details']['title'] == 'Here’s to Us (radio edit)')
                edit['duration_difference'] = .03
                break
        standard = next(rid for rid, tracks in releases.items() if len(tracks) == 12)
        with patch.object(lookup, 'get_release_tracklist', side_effect=lambda rid: releases[rid]):
            candidate = lookup.evaluate_release_candidate(audits, standard)
        self.assertNotEqual('complete', candidate['status'])
        self.assertTrue(candidate['review'])

    @staticmethod
    def with_strong_album_anchors(audits):
        """Restore independent hits omitted from the compact saved fixture.

        The real review evidence has 9/14 Americana and 11/12 Endless Summer
        independently identified tracks.  The deliberately stripped fixture
        also exercises the weaker no-anchor, must-defer behavior below.
        """
        for audit in audits:
            assessment = audit['assessment']
            verified = [row for row in assessment['candidates']
                        if row.get('status') == 'verified']
            if assessment.get('status') == 'verified' and len(verified) == 1:
                audit['status'] = 'preliminary_match'
                audit['details'] = deepcopy(verified[0]['details'])
        return audits

    def test_americana_whole_album_studio_context_resolves_live_decoys(self):
        audits, releases = self.rows('Americana')
        self.with_strong_album_anchors(audits)
        self.assertGreaterEqual(sum(lookup._accepted_recording(a, independent=True)
                                    is not None for a in audits), 9)
        ranked = lookup.discover_release_candidates(audits, local_album='Americana')
        with patch.object(lookup, 'get_release_tracklist', side_effect=lambda rid: releases[rid]):
            checked = [lookup.evaluate_release_candidate(audits, rid)
                       for rid in ranked[:5]]
        self.assertTrue(all(row['status'] == 'complete' for row in checked))
        self.assertTrue(all(len(row['matched']) == 14 for row in checked))
        consensus = lookup.compare_release_candidate_reports(
            audits, checked, unexamined_release_ids=ranked[5:],
            standard_album_preference=True)
        self.assertEqual('complete', consensus['status'])
        self.assertEqual([], consensus['reasons'])

    def test_endless_summer_standard_context_discards_off_release_instrumental(self):
        audits, releases = self.rows('Endless Summer')
        self.with_strong_album_anchors(audits)
        ranked = lookup.discover_release_candidates(audits, local_album='Endless Summer')
        with patch.object(lookup, 'get_release_tracklist', side_effect=lambda rid: releases[rid]):
            checked = [lookup.evaluate_release_candidate(audits, rid)
                       for rid in ranked[:3]]
        standard = next(row for row in checked if len(row['tracklist']) == 12)
        self.assertEqual('complete', standard['status'])
        self.assertEqual(12, len(standard['matched']))
        consensus = lookup.compare_release_candidate_reports(
            audits, checked, unexamined_release_ids=ranked[3:],
            standard_album_preference=True)
        self.assertEqual('complete', consensus['status'])
        self.assertEqual([], consensus['reasons'])

    def test_endless_summer_instrumental_on_selected_edition_still_blocks(self):
        audits, releases = self.rows('Endless Summer')
        self.with_strong_album_anchors(audits)
        standard = next(rid for rid, tracks in releases.items() if len(tracks) == 12)
        instrumental = next(row for a in audits for row in a['assessment']['candidates']
                            if isinstance(row.get('details'), dict)
                            and 'instrumental' in (row['details'].get('title') or '').lower())
        alternate = 'synthetic-instrumental-edition'
        releases[alternate] = deepcopy(releases[standard])
        releases[alternate][-1]['recording_id'] = instrumental['recording_id']
        releases[alternate][-1]['title'] = 'Memories (instrumental)'
        releases[alternate][-1]['recording_title'] = 'Memories (instrumental)'
        with patch.object(lookup, 'get_release_tracklist', side_effect=lambda rid: releases[rid]):
            reports = [lookup.evaluate_release_candidate(audits, rid)
                       for rid in (standard, alternate)]
        result = lookup.compare_release_candidate_reports(audits, reports)
        self.assertEqual('conflicting', result['status'])
        self.assertIn('competing_album_recording_sequences', result['reasons'])

    def test_americana_untimed_live_alternatives_still_defer(self):
        audits, releases = self.rows('Americana')
        standard = next(rid for rid, tracks in releases.items() if len(tracks) == 14)
        with patch.object(lookup, 'get_release_tracklist', side_effect=lambda rid: releases[rid]):
            candidate = lookup.evaluate_release_candidate(audits, standard)
        self.assertNotEqual('complete', candidate['status'])
        self.assertTrue(candidate['review'])

    def test_endless_summer_instrumental_competitor_still_defer(self):
        audits, releases = self.rows('Endless Summer')
        standard = next(rid for rid, tracks in releases.items() if len(tracks) == 12)
        with patch.object(lookup, 'get_release_tracklist', side_effect=lambda rid: releases[rid]):
            candidate = lookup.evaluate_release_candidate(audits, standard)
        self.assertNotEqual('complete', candidate['status'])
        self.assertTrue(candidate['review'])

if __name__ == '__main__':
    unittest.main()
