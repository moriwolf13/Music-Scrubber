"""Early same-run duplicate detection: audio proof, no online identity shortcuts."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import shutil
import subprocess
import unittest

from mutagen import File
from support import WorkspaceCase, make_cover
import mixie
import mixie_batch_duplicates as batch
import sort
import tag


class DuplicatePreflightTests(WorkspaceCase):
    def fixture(self):
        roots = (self.stage / 'Clash direct', self.stage / 'Clash discography')
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
        for i, frequency in ((1, 440), (2, 880)):
            source = roots[0] / f'{i:02d}.flac'
            subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error',
                            '-f','lavfi','-i',f'sine=frequency={frequency}:duration=0.25',
                            '-n',str(source)], check=True, capture_output=True, timeout=15)
            other = roots[1] / source.name
            shutil.copy2(source, other)
            for path, date in ((source, '2012'),(other, '2011')):
                audio = File(path, easy=True)
                if audio.tags is None: audio.add_tags()
                for key, value in {'album':'Clash', 'albumartist':'Caravan Palace',
                                   'artist':'Caravan Palace', 'title':f'Track {i}',
                                   'tracknumber':str(i), 'date':date, 'genre':'Electro Swing'}.items():
                    audio[key]=[value]
                audio.save()
        albums = {}
        for idx, root in enumerate(roots):
            tracks = [tag.read_track_metadata(root / f'{i:02d}.flac') for i in (1,2)]
            albums[(root.name, 'clash')] = {'album':'Clash', 'folder':root, 'tracks': tracks}
        return albums

    def test_duplicate_folders_share_one_scan_candidate_but_keep_each_source(self):
        albums = self.fixture()
        first, second = tuple(albums)
        self.assertNotEqual(sort.file_checksum(albums[first]['tracks'][0]['path']),
                            sort.file_checksum(albums[second]['tracks'][0]['path']))
        found = batch.BatchDuplicates(albums)
        self.assertEqual(first, found.source(second))
        mapping = found.validated_mapping(second)
        self.assertEqual(2, len(mapping))
        self.assertTrue(all(path.parent.name == first[0] for path in mapping))
        self.assertTrue(all(path.parent.name == second[0] for path in mapping.values()))

    def test_different_encoded_audio_does_not_share_identification(self):
        albums = self.fixture()
        second = list(albums)[1]
        target = Path(albums[second]['tracks'][0]['path'])
        target.unlink()
        subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error',
                        '-f','lavfi','-i','sine=frequency=1200:duration=0.25',
                        '-n',str(target)], check=True, capture_output=True, timeout=15)
        self.assertFalse(batch.BatchDuplicates(albums).representative)

    def test_different_tag_credit_still_groups_identical_encoded_audio(self):
        albums = self.fixture()
        second = list(albums)[1]
        other = Path(albums[second]['tracks'][0]['path'])
        audio = File(other, easy=True)
        audio['artist']=['A Different Performer']
        audio.save()
        albums[second]['tracks'][0] = tag.read_track_metadata(other)
        self.assertEqual(1, len(batch.BatchDuplicates(albums).representative))

    def test_two_exact_deliveries_create_only_one_review_and_one_scan(self):
        albums = self.fixture()
        (self.script / '.mixie-import-first.json').write_text(
            '{"version":1,"mode":"verified_library_first"}', encoding='utf-8')
        with patch.object(mixie.sort, 'sort_inbox'), \
             patch.object(tag, 'group_staged_albums', return_value=albums), \
             patch.object(mixie, 'get_acoustid_client_key', return_value='DISPOSABLE-TEST'), \
             patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(mixie, 'input', return_value='n'), \
             patch.object(mixie, 'preview_album', side_effect=ValueError('unresolved recording evidence')) as preview, \
             redirect_stdout(StringIO()):
            mixie._main('1')
        self.assertEqual(1, preview.call_count)
        queue = mixie.load_review_queue()
        self.assertEqual(1, len(queue))
        self.assertEqual('Clash', queue[0]['album'])
        self.assertEqual(2, len(tuple(self.stage.iterdir())))

    def test_saved_duplicate_review_choices_block_automatic_import_and_cleanup(self):
        albums = self.fixture()
        first, second = tuple(albums)
        (self.script / '.mixie-import-first.json').write_text(
            '{"version":1,"mode":"verified_library_first"}', encoding='utf-8')
        issue = mixie._album_issue(albums[second])
        issue.update({'status': 'needs_review', 'decisions': {'genre': 'Custom'}})
        mixie.save_review_queue([issue])
        with patch.object(mixie.sort, 'sort_inbox'), \
             patch.object(tag, 'group_staged_albums', return_value=albums), \
             patch.object(mixie, 'get_acoustid_client_key', return_value='DISPOSABLE-TEST'), \
             patch.object(mixie.sys, 'argv', ['mixie.py']), \
             patch.object(mixie, 'preview_album', side_effect=AssertionError('Should stop before importing')) as preview, \
             redirect_stdout(StringIO()) as transcript:
            mixie._main('1')
        self.assertEqual(0, preview.call_count)
        self.assertIn('saved review choices', transcript.getvalue())
        self.assertEqual(issue['decisions'], mixie.load_review_queue()[0]['decisions'])
        self.assertEqual(2, len(tuple(self.stage.iterdir())))

    def test_source_mutation_after_index_blocks_evidence_reuse(self):
        albums = self.fixture()
        second = list(albums)[1]
        found = batch.BatchDuplicates(albums)
        path = Path(albums[second]['tracks'][0]['path'])
        audio = File(path, easy=True)
        audio['genre']=['Other']
        audio.save()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            found.validated_mapping(second)

    def test_rebound_plan_revalidates_second_source_and_numbers(self):
        albums = self.fixture()
        first, second = tuple(albums)
        found = batch.BatchDuplicates(albums)
        mapping = found.validated_mapping(second)
        paths = [Path(track['path']).absolute() for track in albums[first]['tracks']]
        issues = mixie._album_issue(albums[first])
        manifest = mixie.completion_source_manifest(issues)
        common={'album':'Clash','album_artist':'Caravan Palace','date':'2012','genre':'Electro Swing'}
        plan={
            'provenance':'independently_verified', 'interpretation':None,
            'reviewed_decision':None, 'artwork_decision':None, 'manual_fields':{},
            'identities':{str(path):{'recording_id':f'rid-{i}', 'artist':'Caravan Palace',
                                   'title':f'Track {i}', 'verified':True, 'user_approved':False}
                          for i, path in enumerate(paths,1)},
            'audits':[{'path':p,'status':'preliminary_match','existing':row,'assessment':{}}
                      for p,row in zip(paths,albums[first]['tracks'])],
            'matched':{('1',str(i)):path for i,path in enumerate(paths,1)},
            'release_tracks':[{'disc':'1','number':str(i),'title':f'Track {i}',
                               'recording_id':f'rid-{i}'} for i in (1,2)],
            'evaluations':[{'matched':{(1,i):path for i,path in enumerate(paths,1)},
                            'status':'complete','reasons':[]}],
            'compatible_ids':['synthetic-release'], 'source_manifest':manifest,
            'source_fingerprint':mixie.completion_fingerprint(issues['import_name'],
                mixie._source_rows(issues,manifest),manifest),
            'auto_album_fields':{'album':'Clash','album_artist':'Caravan Palace',
                                 'date':'2012', 'genre':'Electro Swing'},
            'partial':False, '_batch_common':common,
            '_batch_evaluation': {'tracklist':[{'disc':'1','number':str(i),
                'title':f'Track {i}','recording_id':f'rid-{i}'} for i in (1,2)],
                'matched':{(1,i):path for i,path in enumerate(paths,1)}},
            '_batch_genre_hint':'Electro Swing',
        }
        plan['release_totals'] = mixie._release_totals(plan['release_tracks'])
        plan['numbering_deferred'] = {}
        rebound=batch.reuse_verified_plan(mixie,plan,albums[second],mapping)
        self.assertEqual({str(v) for v in mapping.values()},set(rebound['identities']))
        self.assertEqual('Clash discography',rebound['source_manifest']['import_name'])
        self.assertNotEqual(rebound['source_fingerprint'],plan['source_fingerprint'])
        self.assertTrue(all(Path(p['path']).parent == albums[second]['folder'] for p in rebound['proposals']))
        self.assertEqual(common,plan['_batch_common'])
        # Both deliveries must still execute the REAL commit and receipt path,
        # not simply mark the second source as finished because it shares music.
        plan['artwork'] = make_cover()
        plan['tag_edits'] = mixie._validated_tag_edits(None, paths)
        plan['proposals'] = tag.propose_release_numbering(plan['matched'],plan['release_tracks'])
        plan['release_totals'] = mixie._release_totals(plan['release_tracks'])
        plan['numbering_deferred'] = {}
        rebound['artwork'] = make_cover()
        with redirect_stdout(StringIO()):
            first_receipt = mixie.test_album_plan(plan, albums[first], commit=True, auto=True)
            second_receipt = mixie.test_album_plan(
                rebound, albums[second], commit=True, auto=True,
                same_batch_receipts=(first_receipt,))
        self.assertTrue(first_receipt)
        self.assertEqual(dict(first_receipt), dict(second_receipt))
        self.assertEqual(2, len(first_receipt.input_map))
        self.assertEqual(2, len(second_receipt.input_map))
        self.assertTrue(all(key.startswith(second[0]+'/') for key in second_receipt.input_map))
        self.assertNotEqual(first_receipt.source_manifest, second_receipt.source_manifest)

if __name__ == '__main__':
    unittest.main()
