"""v10j disposable source-bound finalization, cohort and review regressions."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import zipfile

from support import WorkspaceCase, metadata_record, make_audio
from test_finalizer_v10i import assert_shared_originals
import lookup
import mixie
import mixie_finalizer as final
import mixie_review_ui as ui
import sort
import tag
import shutil


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CohortTests(WorkspaceCase):
    def _fixture(self, competing=False):
        paths = [self.track_path(f'example/record-{i}.flac', bytes([i])) for i in range(1, 4)]
        candidates = []
        audits = []
        release = []
        for i, path in enumerate(paths, 1):
            choices = [{'recording_id': f'main-{i}', 'status': 'verified',
                        'details': {'id': f'main-{i}', 'title': f'Track {i}',
                                    'artist-credit-phrase': 'Synthetic Artist'}},
                       {'recording_id': f'other-{i}', 'status': 'verified',
                        'details': {'id': f'other-{i}', 'title': f'Track {i}',
                                    'artist-credit-phrase': 'Synthetic Artist'}}]
            if i == 2 and competing:
                choices.append({'recording_id': f'main-alt-{i}', 'status': 'verified',
                                'details': {'id': f'main-alt-{i}', 'title': f'Track {i}'}})
            audits.append({'path': path, 'status': 'needs_review', 'details': None,
                           'assessment': {'complete': True, 'status': 'ambiguous',
                                          'candidates': choices, 'errors': []}})
            release.append({'disc': '1', 'number': str(i),
                            'recording_id': f'main-{i}', 'title': f'Track {i}'})
        if competing:
            # Two on-release versions for the same track must NOT be selected.
            release[1]['recording_id'] = 'main-alt-2'
            release.append({'disc':'1','number':'4','recording_id':'main-2','title':'Track 2'})
        return paths, audits, release

    def test_full_verified_fingerprint_cohort_resolves_without_independent_anchors(self):
        paths, audits, release = self._fixture()
        with patch.object(lookup, 'get_release_tracklist', return_value=release):
            result = lookup.evaluate_release_candidate(audits, 'verified-release',
                local_tracks=[metadata_record(p, title=(f'Track {i}',), track_number=(str(i),))
                              for i,p in enumerate(paths,1)])
        self.assertEqual('complete', result['status'])
        self.assertEqual(3,len(result['matched']))
        self.assertIn('resolved_by_whole_album_fingerprints',result['reasons'])

    def test_probable_on_release_candidate_vetoes_cohort_resolution(self):
        paths,audits,release=self._fixture()
        audits[1]['assessment']['candidates'].append({'recording_id':'main-2','status':'probable','reasons':[]})
        with patch.object(lookup,'get_release_tracklist',return_value=release):
            result=lookup.evaluate_release_candidate(audits,'verified-release')
        self.assertNotEqual('complete',result['status'])

    def test_disagreeing_album_title_does_not_generate_an_audio_edge(self):
        paths,audits,release=self._fixture()
        release[1]['title']='A different song'
        with patch.object(lookup,'get_release_tracklist',return_value=release):
            result=lookup.evaluate_release_candidate(audits,'verified-release')
        self.assertNotEqual('complete',result['status'])

    def test_feature_credit_band_member_is_not_a_different_guest(self):
        details={'title':'I Want to Believe', 'artist-credit-phrase':'In This Moment'}
        actual_title='I Want to Believe (feat. Rory Rodriguez of Dayseeker)'
        title, artist = mixie._feature_aware_identity(details,actual_title,'I Want to Believe',
                {'title':('I Want to Believe (feat. Dayseeker)',),'artist':('In This Moment',)})
        self.assertEqual('In This Moment',artist)
        self.assertIn('Rory Rodriguez',title)
        title, artist = mixie._feature_aware_identity(details, actual_title, 'I Want to Believe',
                {'title': ('I Want to Believe (feat. Someone Else)',), 'artist': ('In This Moment',)})
        self.assertEqual('In This Moment', artist)
        self.assertIn('Rory Rodriguez', title)
        with self.assertRaisesRegex(ValueError, 'Different featured'):
            mixie._feature_aware_identity(details, actual_title, 'I Want to Believe',
                {'title': ('Different Song (feat. Someone Else)',), 'artist': ('In This Moment',)})

    def test_wrong_source_number_is_replaced_by_canonical_release_position(self):
        paths=[]
        for index, wrong in ((1,3),(2,1),(3,2)):
            path=make_audio(self.stage/'shuffle'/f'{index}.flac',
                artist='Synthetic Artist',album='Shuffled',title=f'Song {index}',
                tracknumber=wrong,tracktotal=3,discnumber='1',disctotal=1,
                date=2024,genre='Rock')
            paths.append(path)
        matched={('1',str(index)):path for index,path in enumerate(paths,1)}
        remote=[{'disc':'1','number':str(index),
                 'title':f'Song {index}', 'recording_id':f'verified-{index}'}
                for index in range(1,4)]
        proposals=tag.propose_release_numbering(matched,remote,independently_verified=True)
        corrected={(Path(row['path']).name,row['field']):row for row in proposals}
        self.assertEqual({('1.flac','track_number'),('2.flac','track_number'),('3.flac','track_number')},
                         {key for key in corrected if key[1]=='track_number'})
        self.assertEqual([1,2,3],[corrected[(f'{i}.flac','track_number')]['value'] for i in range(1,4)])
        copies=self.root/'.mixie-work-v10j';copies.mkdir()
        for index,path in enumerate(paths,1):
            target=copies/path.name
            shutil.copy2(path,target)
            tag.remove_single_disc_tags_on_copy(target,copies)
            row=tag.read_track_metadata(target)
            self.assertFalse(row['disc_number'])
            self.assertFalse(row['disc_total'])
            self.assertEqual(('1',),tag.read_track_metadata(path)['disc_number'])

    def test_single_disc_review_hides_disc_tag_editing(self):
        track=self.track_path('one/album/one.flac')
        issue={'import_name':'one','album':'Album','tracks':['one/album/one.flac']}
        with patch.object(mixie,'_offline_album_review_rows',return_value=[metadata_record(track)]),\
             patch.object(ui,'input',return_value='0'),redirect_stdout(StringIO()) as output:
            ui._show_tracks(mixie, issue, [issue])
        self.assertIn('Track Number',output.getvalue())
        self.assertNotIn('3. Disc Number:',output.getvalue())


class SharedDeliveryTests(WorkspaceCase):
    def _shared(self, only_one=False, zipped=False):
        inbox=self.inbox/'Shared collection'
        for album in ('First', 'Second'):
            folder=inbox/album;folder.mkdir(parents=True)
            (folder/'01.flac').write_bytes(('untouched '+album).encode())
            (folder/'art.png').write_bytes(('art '+album).encode())
        (inbox/'README.txt').write_bytes(b'untouched notes')
        self.delivery_bytes = {'kind': 'zip' if zipped else 'directory',
            'payload': {path.relative_to(inbox).as_posix(): path.read_bytes()
                        for path in inbox.rglob('*') if path.is_file()}}
        if zipped:
            saved=self.inbox/'Shared collection.zip'
            with zipfile.ZipFile(saved,'w') as output:
                for path in sorted(inbox.rglob('*')):
                    if path.is_file():
                        output.write(path,path.relative_to(inbox).as_posix())
            shutil.rmtree(inbox)
            inbox=saved
            self.delivery_bytes['raw'] = inbox.read_bytes()
        with redirect_stdout(StringIO()):sort.sort_inbox()
        name=sort.staging_import_name(inbox.name,'zip' if zipped else 'directory')
        albums={}
        for album in ('First','Second'):
            stage=self.stage/name/album/'01.flac'
            issue={'import_name':name,'album':album,'tracks':[f'{name}/{album}/01.flac']}
            library=self.library/'Synthetic Artist'/album
            library.mkdir(parents=True)
            music=library/'01 - One.flac'
            music.write_bytes(('tagged '+album).encode())
            cover=library/'cover.jpg'
            cover.write_bytes(('new cover '+album).encode())
            manifest=mixie.completion_source_manifest(issue)
            receipt=mixie.CommitReceipt({str(music):sha(music),str(cover):sha(cover)},
                input_map={f'{name}/{album}/01.flac':{'source_sha256':sha(stage),
                  'destination':str(music),'destination_sha256':sha(music)}},
                source_manifest=manifest,asset_outputs={str(cover):sha(cover)},
                evidence={'provenance':'independently_verified'})
            if album == 'First' or not only_one:
                mixie.record_library_only_import(issue, receipt)
            albums[(name,album)]={'album':album,'tracks':[{'path':stage}]}
        return inbox,self.stage/name,albums

    def test_review_fingerprint_checks_every_payload_hash_once(self):
        inbox, stage, albums = self._shared(only_one=True)
        name = stage.name
        issue = {'import_name': name, 'album': 'First',
                 'tracks': [f'{name}/First/01.flac']}
        with patch.object(sort, 'file_manifest', wraps=sort.file_manifest) as manifest:
            fingerprint = mixie.review_fingerprint(issue)
        self.assertEqual(1, manifest.call_count)
        self.assertEqual(64, len(fingerprint))
        # Detect a changed staged file in a sibling album as well, even though
        # this review refers only to the First album's audio.
        (stage/'Second'/'01.flac').write_bytes(b'tampered sibling audio')
        with self.assertRaisesRegex((RuntimeError, ValueError),
                                    'Source payload changed since staging'):
            mixie.review_fingerprint(issue)

    def assert_originals(self, albums):
        records = {}
        rows = final._state()['finalized']
        for (name, album), data in albums.items():
            issue = mixie._album_issue(data)
            matches = [row for row in rows.values()
                       if row['import_name'] == name and row['tracks'] == issue['tracks']]
            self.assertEqual(1, len(matches))
            records[album] = assert_shared_originals(self, matches[0], {
                name: {**self.delivery_bytes, 'tracks': [f'{album}/01.flac']}})[name]
        self.assertEqual({Path(record['path']) for record in records.values()},
                         set((self.backup / 'Originals').rglob('*.zip')))
        return records

    def test_two_verified_albums_share_one_original_before_shared_cleanup(self):
        inbox,stage,albums=self._shared()
        count,errors=final.finish_verified_albums(mixie,albums,allow_source_cleanup=True)
        self.assertEqual((2,[]),(count,errors))
        self.assertFalse(inbox.exists() or stage.exists())
        records = self.assert_originals(albums)
        self.assertEqual(records['First'], records['Second'])
        self.assertEqual(1, len(list((self.backup / 'Originals').rglob('*.zip'))))
        for album in ('First','Second'):
            library=self.backup/'Library'/'Synthetic Artist'/f'{album}.zip'
            self.assertTrue(library.is_file())
            with zipfile.ZipFile(library) as saved:
                self.assertEqual(('tagged '+album).encode(), saved.read('01 - One.flac'))
                self.assertEqual(('new cover '+album).encode(), saved.read('cover.jpg'))
        self.assertEqual(1,len(final._state()['completed']))
        self.assertFalse(final.resume_cleanup())

    def test_shared_zip_retains_exact_raw_bytes_in_one_object_for_both_album_refs(self):
        inbox,stage,albums=self._shared(zipped=True)
        original_bytes=inbox.read_bytes()
        count,errors=final.finish_verified_albums(mixie,albums,allow_source_cleanup=True)
        self.assertEqual((2,[]),(count,errors))
        self.assertFalse(inbox.exists() or stage.exists())
        records = self.assert_originals(albums)
        self.assertEqual(records['First'], records['Second'])
        self.assertEqual(1, len(list((self.backup / 'Originals').rglob('*.zip'))))
        with zipfile.ZipFile(records['First']['path']) as saved:
            self.assertEqual(original_bytes, saved.read('original.zip'))
        self.assertEqual(2, len(list((self.backup / 'Library').rglob('*.zip'))))

    def test_unresolved_sibling_never_deleted(self):
        inbox,stage,albums=self._shared(only_one=True)
        issue=mixie._album_issue(albums[(stage.name,'Second')])
        fingerprint=mixie.review_fingerprint(issue)
        original_inventory=sort.file_manifest(inbox)
        count,errors=final.finish_verified_albums(mixie,albums,allow_source_cleanup=True)
        self.assertEqual(1,count)
        self.assertTrue(errors and 'unresolved' in errors[0])
        self.assertTrue(inbox.exists() and stage.exists())
        self.assertEqual(original_inventory,sort.file_manifest(inbox))
        self.assertEqual(fingerprint,mixie.review_fingerprint(issue))
        self.assertIsNone(final._state()['active'])
        self.assertEqual(1,len(final._state()['finalized']))
        self.assert_originals({key: data for key, data in albums.items() if key[1] == 'First'})
        self.assertTrue((self.backup/'Library'/'Synthetic Artist'/'First.zip').is_file())
        self.assertFalse((self.backup/'Library'/'Synthetic Artist'/'Second.zip').exists())
        self.assertTrue(final.is_album_finalized(mixie,mixie._album_issue(albums[(stage.name,'First')])))

    def test_partial_purge_resume_verifies_all_shared_archives(self):
        inbox,stage,albums=self._shared()
        real=final.shutil.rmtree
        calls=[0]
        def stop_once(*args,**kwargs):
            calls[0]+=1
            if calls[0]==1:raise OSError('Synthetic stop before purge')
            return real(*args,**kwargs)
        with patch.object(final.shutil,'rmtree',side_effect=stop_once):
            count,errors=final.finish_verified_albums(mixie,albums,allow_source_cleanup=True)
        self.assertEqual(0,count)
        self.assertTrue(errors)
        self.assertIsNotNone(final._state()['active'])
        self.assertTrue(final.resume_cleanup(allow_source_cleanup=True))
        self.assertFalse(inbox.exists() or stage.exists())
        self.assertEqual(1,len(final._state()['completed']))
