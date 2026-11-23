"""Synthetic named 29-album archival matrix, NOT acceptance of live music/state.

Tiny byte fixtures stand in for already verified imports, not audio identification.
The labels/counts mirror the requested six shared deliveries plus five independent
albums. All source, receipt, library, ZIP and cleanup writes stay in WorkspaceCase.
"""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import zipfile

import audio
import lookup
import mixie
import mixie_finalizer as final
import mixie_shared_originals as store
import sort
import tag
from support import WorkspaceCase
from test_finalizer_v10i import assert_shared_originals


SHARED = (
    ('PVRIS', 'zip', ('Use Me (Deluxe)', 'EVERGREEN'), ()),
    ('Halestorm', 'directory', (
        'MistressChristmas', 'ReAniMateEP', 'Halestorm', 'IntoWildLifeDeluxe',
        'Everest', 'Vicious', 'Reanimate3EP'), ('BackFromDead', 'StrangeCase', 'canuseeme')),
    ('InThisMoment', 'zip', ('Ritual', 'Godmode'), ('Witch',)),
    ('Offspring', 'directory', ('RiseFallRageGrace', 'LetBadTimesDeluxe'), ('Americana',)),
    ('CaravanPalace', 'directory', ('Clash', 'CaravanPalace', 'Panic'), ()),
    ('RareAmericans', 'zip', ('BrittleBonesNicky3', "You'reNotBadPerson", 'RareAmericans2'), ()),
)
INDEPENDENT = (
    ('StreetsOfGold', 'zip'), ('NEED', 'directory'), ('DirtyMind', 'directory'),
    ('ChromeoWhiteWomen', 'zip'), ('CaravanChronologic', 'directory'),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Named29AlbumFinalizationAcceptanceTests(WorkspaceCase):
    def build_matrix(self):
        (self.script / '.mixie-import-first.json').write_text(json.dumps({
            'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')
        self.units, self.albums = {}, {}
        for label, kind, verified, pending in (*SHARED, *(
                (label, kind, (label,), ()) for label, kind in INDEPENDENT)):
            payload = {'README.txt': ('synthetic untouched delivery ' + label).encode()}
            unit = {'label': label, 'kind': kind, 'verified': verified, 'pending': pending,
                    'payload': payload, 'albums': []}
            # Artist/album labels here are fixture routing, not catalog evidence.
            for number, album in enumerate((*verified, *pending), 1):
                leaf = f'{number:02d}-{album}/01.flac'
                payload[leaf] = f'synthetic original audio | {label} | {album}'.encode()
                payload[f'{number:02d}-{album}/art.png'] = f'synthetic original art | {label} | {album}'.encode()
                key = label, album
                row = {'key': key, 'unit': unit, 'album': album, 'leaf': leaf,
                       'initially_verified': album in verified,
                       'library_bytes': {
                           '01 - Fixture.flac': f'synthetic corrected audio | {label} | {album}'.encode(),
                           'cover.jpg': f'synthetic verified cover | {label} | {album}'.encode()}}
                self.albums[key] = row
                unit['albums'].append(row)
            source = self.inbox / ('synthetic-' + label + ('.zip' if kind == 'zip' else ''))
            if kind == 'zip':
                with zipfile.ZipFile(source, 'w') as saved:
                    for name, data in payload.items():
                        saved.writestr(name, data)
                unit['zip_bytes'] = source.read_bytes()
            else:
                for name, data in payload.items():
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            unit['source'] = source
            self.units[label] = unit
        with redirect_stdout(StringIO()):
            self.assertEqual(11, len(sort.sort_inbox()))
        queue = []
        for unit in self.units.values():
            name = sort.staging_import_name(unit['source'].name, unit['kind'])
            unit.update(name=name, stage=self.stage / name,
                        provenance=sort.staging_provenance(name, verify_payload=False))
            for row in unit['albums']:
                issue = {'import_name': name, 'album': row['album'],
                         'tracks': [f"{name}/{row['leaf']}"],
                         'status': 'archive_pending' if row['initially_verified'] else 'needs_review',
                         'details': {'kind': 'completion' if row['initially_verified'] else 'identity_release'},
                         'reason': ('Library import verified; archive pending: synthetic fixture'
                                    if row['initially_verified'] else 'Synthetic unresolved sibling')}
                issue['source_fingerprint'] = mixie.review_fingerprint(issue)
                row['issue'] = issue
                if row['initially_verified']:
                    self.record_receipt(row)
                queue.append(deepcopy(issue))
        mixie.save_review_queue(queue)
        self.pending_keys = {key for key, row in self.albums.items() if not row['initially_verified']}
        self.verified_keys = set(self.albums) - self.pending_keys
        self.partial_units = {label for label, unit in self.units.items() if unit['pending']}

    def record_receipt(self, row):
        unit, issue = row['unit'], row['issue']
        folder = self.library / unit['label'] / row['album']
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in row['library_bytes'].items():
            (folder / name).write_bytes(data)
        music, cover = folder / '01 - Fixture.flac', folder / 'cover.jpg'
        receipt = mixie.CommitReceipt({str(path): digest(path.read_bytes()) for path in (music, cover)},
            input_map={issue['tracks'][0]: {
                'source_sha256': digest(unit['payload'][row['leaf']]),
                'destination': str(music), 'destination_sha256': digest(row['library_bytes'][music.name])}},
            source_manifest=mixie.completion_source_manifest(issue),
            asset_outputs={str(cover): digest(row['library_bytes'][cover.name])},
            evidence={'provenance': 'independently_verified', 'fixture': 'synthetic archival matrix only'})
        row['identity'] = mixie.record_library_only_import(issue, receipt)

    def assert_sources(self, retained, phase):
        for label, unit in self.units.items():
            with self.subTest(phase=phase, delivery=label, kind=unit['kind']):
                self.assertEqual(label in retained, unit['source'].exists())
                self.assertEqual(label in retained, unit['stage'].exists())
                self.assertEqual(unit['provenance'], sort.staging_provenance(unit['name'], verify_payload=False))
                if label not in retained:
                    continue
                expected = {name: digest(data) for name, data in unit['payload'].items()}
                self.assertEqual(expected, sort.file_manifest(unit['stage']))
                if unit['kind'] == 'zip':
                    self.assertEqual(unit['zip_bytes'], unit['source'].read_bytes())
                else:
                    self.assertEqual(expected, sort.file_manifest(unit['source']))

    def assert_queue(self, expected_keys, phase):
        queue = mixie.load_review_queue()
        expected = {(self.albums[key]['issue']['import_name'], tuple(self.albums[key]['issue']['tracks'])):
                    self.albums[key]['album'] for key in expected_keys}
        actual = {(issue['import_name'], tuple(issue['tracks'])): issue['album'] for issue in queue}
        with self.subTest(phase=phase, boundary='exact queue claims'):
            self.assertEqual(len(expected_keys), len(queue))
            self.assertEqual(expected, actual)
        for issue in queue:
            with self.subTest(phase=phase, pending=issue['album']):
                self.assertEqual('needs_review', issue['status'])
                self.assertEqual('identity_release', issue['details']['kind'])
                self.assertEqual(issue['source_fingerprint'], mixie.review_fingerprint(issue))

    def archive_snapshot(self):
        return {path.relative_to(self.backup).as_posix(): (digest(path.read_bytes()), path.stat().st_mtime_ns)
                for category in ('Originals', 'Library')
                for path in (self.backup / category).rglob('*.zip')}

    def shared_snapshot(self):
        root = self.backup / 'Originals' / 'Shared'
        return {path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob('*') if path.is_file()}

    def assert_archives(self, keys, phase):
        state = final._state()
        by_delivery = {}
        for key in sorted(keys):
            row = self.albums[key]
            unit = row['unit']
            with self.subTest(phase=phase, delivery=unit['label'], album=row['album']):
                record = state['finalized'][row['identity']]
                self.assertEqual({unit['name']: row['issue']['tracks']}, record['source_claims'])
                delivery = {'kind': unit['kind'], 'payload': unit['payload'], 'tracks': [row['leaf']]}
                if unit['kind'] == 'zip':
                    delivery['raw'] = unit['zip_bytes']
                original = assert_shared_originals(self, record, {unit['name']: delivery})[unit['name']]
                previous = by_delivery.setdefault(unit['label'], original)
                self.assertEqual(previous, original, 'Sibling albums must pin the same complete delivery object')
                files = row['library_bytes']
                path = self.backup / 'Library' / unit['label'] / (row['album'] + '.zip')
                saved_record = record['library']
                self.assertEqual(str(path), saved_record['path'])
                self.assertEqual(digest(path.read_bytes()), saved_record['file_sha256'])
                self.assertEqual(sort.zip_manifest(path), saved_record['entries'])
                with zipfile.ZipFile(path) as saved:
                    self.assertEqual(set(files) | {final.MANIFEST_NAME}, set(saved.namelist()))
                    self.assertEqual(files, {name: saved.read(name) for name in files})
                    manifest = json.loads(saved.read(final.MANIFEST_NAME))
                self.assertEqual('corrected_library', manifest['type'])
                self.assertEqual({name: digest(data) for name, data in files.items()}, manifest['sha256'])
        with self.subTest(phase=phase, boundary='11 physical delivery objects, not per-album copies'):
            self.assertEqual(set(self.units), set(by_delivery))
            paths = {Path(record['path']) for record in by_delivery.values()}
            self.assertEqual(11, len(paths))
            self.assertEqual(paths, set((self.backup / 'Originals').rglob('*.zip')))
            self.assertEqual(11, len({(path.stat().st_dev, path.stat().st_ino) for path in paths}))
            journals = list((self.backup / 'Originals' / 'Shared' / 'deliveries').glob('*.json'))
            self.assertEqual(11, len(journals))
            self.assertEqual(len(keys), len(list((self.backup / 'Library').rglob('*.zip'))))

    def test_named_six_shared_plus_five_independent_receipt_only_finalization(self):
        self.build_matrix()
        with self.subTest(phase='requested matrix counts'):
            self.assertEqual({'PVRIS': (2, 0), 'Halestorm': (7, 3), 'InThisMoment': (2, 1),
                              'Offspring': (2, 1), 'CaravanPalace': (3, 0), 'RareAmericans': (3, 0)},
                             {label: (len(verified), len(pending)) for label, _kind, verified, pending in SHARED})
            self.assertEqual(19, sum(len(verified) for _label, _kind, verified, _pending in SHARED))
            self.assertEqual((29, 24, 5), (len(self.albums), len(self.verified_keys), len(self.pending_keys)))
            self.assertEqual({'Halestorm', 'InThisMoment', 'Offspring'}, self.partial_units)
            self.assertEqual(24, len(mixie._verified_imports()))

        # These files deliberately have no readable music tags. Recovery must
        # use persisted exact claims, never rediscover albums through metadata.
        with ExitStack() as stack:
            for module, name in (
                    (sort, 'sort_inbox'), (tag, 'group_staged_albums'), (tag, 'read_track_metadata'),
                    (mixie, 'preview_album'), (mixie, 'test_album_plan'), (mixie, 'test_review_decision'),
                    (mixie, 'get_acoustid_client_key'), (audio, 'audit_tracks'),
                    (lookup, 'get_release_tracklist'), (lookup, 'get_recording_details')):
                stack.enter_context(patch.object(module, name,
                    side_effect=AssertionError('Receipt-only recovery called forbidden operation: ' + name)))
            stack.enter_context(patch('builtins.input', side_effect=AssertionError('Recovery must not prompt')))
            stack.enter_context(redirect_stdout(StringIO()))

            count, problems = mixie.finalize_verified_imports()
            self.assertEqual(24, count)
            self.assertEqual(24, len(final._state()['finalized']))
            self.assertEqual({}, final._state()['completed'])
            self.assertIsNone(final._state()['active'])
            self.assert_sources(set(self.units), 'default recovery retains all 11 sources')
            self.assert_queue(self.pending_keys, '24 archived, five unresolved')
            self.assert_archives(self.verified_keys, '24 Library ZIPs reference 11 complete delivery objects')
            original_archives = self.archive_snapshot()
            original_shared = self.shared_snapshot()
            self.assertEqual(35, len(original_archives))
            for label in sorted(self.partial_units):
                with self.subTest(phase='known unresolved, not unknown audio', delivery=label):
                    notices = [problem for problem in problems if self.units[label]['name'] in problem]
                    self.assertEqual(1, len(notices))
                    self.assertIn('unresolved', notices[0])
                    self.assertNotIn('unaccounted', notices[0])

            # Explicit permission may purge the eight wholly resolved deliveries,
            # but not one file from the three shared units with pending siblings.
            count, _problems = mixie.finalize_verified_imports(allow_source_cleanup=True)
            self.assertEqual(0, count)
            self.assertEqual(8, len(final._state()['completed']))
            self.assert_sources(self.partial_units, 'authorized cleanup retains three partial units intact')
            self.assert_queue(self.pending_keys, 'cleanup must not retire unresolved siblings')
            self.assertEqual(original_archives, self.archive_snapshot())
            self.assertEqual(original_shared, self.shared_snapshot())
            for key in self.pending_keys:
                with self.subTest(phase='no archive for unresolved album', album=key):
                    row = self.albums[key]
                    for category in ('Originals', 'Library'):
                        self.assertFalse((self.backup / category / row['unit']['label'] / (row['album'] + '.zip')).exists())

            # A new call reloads persisted receipts/journal, as on restart. No
            # second archive or finalized receipt publication is allowed.
            journal = (self.backup / final.LEDGER_NAME).read_bytes()
            with patch.object(final, '_write_zip', side_effect=AssertionError('Duplicate Library archive creation')), \
                 patch.object(store, '_write_partial', side_effect=AssertionError('Duplicate delivery object creation')), \
                 patch.object(final, '_write', side_effect=AssertionError('Duplicate finalized receipt')):
                count, _problems = mixie.finalize_verified_imports()
            self.assertEqual(0, count)
            self.assertEqual(journal, (self.backup / final.LEDGER_NAME).read_bytes())
            self.assertEqual(original_archives, self.archive_snapshot())
            self.assertEqual(original_shared, self.shared_snapshot())
            self.assert_sources(self.partial_units, 'restart leaves shared ZIP and sibling fingerprints intact')
            self.assert_queue(self.pending_keys, 'restart does not resurrect 24 completed reviews')

            # Resolve the five pending albums by installing synthetic, independently
            # pinned library receipts; never identify, regroup, or reimport them.
            for key in sorted(self.pending_keys):
                self.record_receipt(self.albums[key])
            count, problems = mixie.finalize_verified_imports()
            self.assertEqual((5, []), (count, problems))
            self.assertEqual(29, len(mixie._verified_imports()))
            self.assertEqual(29, len(final._state()['finalized']))
            self.assert_sources(self.partial_units, 'all 29 archived, cleanup still defaults off')
            self.assert_queue(set(), 'last five retire only after both ZIPs verify')
            self.assert_archives(set(self.albums), '29 Library ZIPs still share exactly 11 delivery objects')
            all_archives = self.archive_snapshot()
            self.assertEqual(40, len(all_archives))
            self.assertEqual(original_shared, self.shared_snapshot())
            self.assertEqual(original_archives, {name: all_archives[name] for name in original_archives})

            with patch.object(final, '_write_zip', side_effect=AssertionError('Cleanup must reuse all 29 Library ZIPs')), \
                 patch.object(store, '_write_partial', side_effect=AssertionError('Cleanup must reuse 11 delivery objects')):
                self.assertEqual((0, []), mixie.finalize_verified_imports(allow_source_cleanup=True))
            self.assert_sources(set(), 'only explicit authorization purges final three sources')
            self.assertEqual(11, len(final._state()['completed']))
            self.assertEqual(all_archives, self.archive_snapshot())
            self.assertEqual(original_shared, self.shared_snapshot())
            self.assert_queue(set(), 'all synthetic reviews remain retired')
            with patch.object(final, '_write_zip', side_effect=AssertionError('Final restart cannot recreate Library ZIPs')), \
                 patch.object(store, '_write_partial', side_effect=AssertionError('Final restart cannot recreate delivery objects')), \
                 patch.object(final, '_write', side_effect=AssertionError('Final restart cannot rewrite receipts')):
                self.assertEqual((0, []), mixie.finalize_verified_imports())
            self.assertEqual(all_archives, self.archive_snapshot())
            self.assertEqual(original_shared, self.shared_snapshot())
