"""Synthetic MP3/FLAC preview -> commit identity contracts; no live acceptance.

Run exclusively with .venv/bin/python -I -B tests/run_isolated.py.
Every audio file is 0.15 seconds of encoded silence. IDs, fingerprint matches,
recording timing and provider payloads are invented. Named track shapes do NOT
establish any identity/version/edition for user-owned audio. Only fingerprint
results and provider transports are doubled: decoding, evidence assessment,
discovery, release mapping/consensus, metadata, backups and publication are real.

Limits: two eligible IDs/file, <=13 files/album, <=110 retail nominations,
unchanged production 12-hydration / 3-membership-page / 96-release budgets.
Public-provider validation is deliberately NOT imported or retried here.
"""
import json
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import audio
import lookup
import mixie
import review
import sort
import tag
import test_final_metadata_stabilization as metadata_fixtures
from test_album_identity_stabilization import (
    AMERICANA_TITLES,
    BACK_FROM_DEAD_TITLES,
    STRANGE_CASE_TITLES,
    WITCH_TITLES,
)
from test_final_metadata_stabilization import _ProviderCase, read_metadata, sha


class AlbumIdentityOutputTests(_ProviderCase):
    def setUp(self):
        self.alternatives, self.memberships = {}, {}
        self.provider_calls, self.artwork_calls = [], []
        self.failed_releases, self.search_answers = {}, {}
        self.protected = {}
        super().setUp()
        self.addCleanup(self.assert_sources_preserved)

    def assert_sources_preserved(self):
        for path, digest in self.protected.items():
            self.assertEqual(digest, sha(path), f'Original or staged source changed: {path}')

    def identify(self, fingerprint, api_key):
        self.assertEqual('DISPOSABLE-OFFLINE-TEST-KEY', api_key)
        canonical = fingerprint['fingerprint'].removeprefix('fixture:')
        self.assertIn(canonical, self.recordings)
        identifiers = self.alternatives.get(canonical, (canonical,))
        self.assertLessEqual(len(identifiers), 2)
        return [{'score': .99, 'recordings': [{'id': rid} for rid in identifiers]}]

    def provider(self, operation, *args, **kwargs):
        name = operation.__name__
        self.provider_calls.append((name, args, deepcopy(kwargs)))
        if name == 'browse_releases':
            self.assertEqual((), args)
            self.assertEqual(lookup.RELEASE_MEMBERSHIP_PAGE_SIZE, kwargs['limit'])
            rows = self.memberships[kwargs['recording']]
            offset = kwargs['offset']
            self.assertIn(offset, (0, 100, 200))
            return {'release-count': str(len(rows)),
                    'release-list': deepcopy(rows[offset:offset + kwargs['limit']])}
        if name == 'search_releases':
            self.assertEqual((), args)
            key = (kwargs['artist'], kwargs['release'])
            self.assertIn(key, self.search_answers, 'Unexpected text-only discovery request')
            if 'catno' in kwargs:
                self.assertEqual('ESCA 7393', kwargs['catno'])
                self.assertEqual(25, kwargs['limit'])
            else:
                self.assertEqual(5, kwargs['limit'])
            return {'release-list': [deepcopy(self.releases[rid]) for rid in self.search_answers[key]]}
        if name == 'get_release_by_id' and args[0] in self.failed_releases:
            raise self.failed_releases[args[0]]
        return super().provider(operation, *args, **kwargs)

    def artwork(self, request, *, timeout):
        self.artwork_calls.append(request.full_url)
        return super().artwork(request, timeout=timeout)

    def make_named_album(self, name, suffix, titles, *, artist, date='2022-05-06', source_tags=None):
        self.assertIn(suffix, ('mp3', 'flac'))
        self.assertLessEqual(len(titles), 13)

        def create():
            return self.fixture(name, suffix, sizes=(len(titles),), artist=artist, date=date, source_date=date,
                                titles={(1, i): title for i, title in enumerate(titles, 1)})

        if source_tags:
            encode = metadata_fixtures.make_audio
            count = [0]

            def source_audio(path, **tags):
                count[0] += 1
                return encode(path, **{**tags, **source_tags.get(count[0], {})})

            # Control source tags BEFORE managed staging/provenance, never
            # modify an already approved source or a production write function.
            with patch.object(metadata_fixtures, 'make_audio', side_effect=source_audio):
                result = create()
        else:
            result = create()
        self.protected.update(result[2])
        return result

    def set_memberships(self, recording_id, rows):
        self.memberships[recording_id] = deepcopy(rows)
        self.recordings[recording_id]['release-count'] = str(len(rows))
        # Exercise actual bounded pagination, not an impossibly convenient
        # full embedded release list when nominations exceed the normal subset.
        self.recordings[recording_id]['release-list'] = deepcopy(rows[:25])

    def release_for(self, release_id, title, recordings, artist, *, kind='Single', template=None):
        result = deepcopy(template) if template else {}
        result.update(id=release_id, title=title, **{'artist-credit-phrase': artist,
            'release-group': {'id': self.identifier('group/' + release_id), 'primary-type': kind,
                              'first-release-date': '2022-05-06'},
            'medium-count': '1', 'medium-list': [{'position': '1', 'format': 'Digital Media',
                'track-count': str(len(recordings)), 'track-list': [{
                    'id': self.identifier('release-track/' + release_id + '/' + str(i)),
                    'number': str(i), 'position': str(i), 'title': self.recordings[rid]['title'],
                    'length': '150', 'recording': {key: self.recordings[rid][key]
                        for key in ('id', 'title', 'length', 'disambiguation') if key in self.recordings[rid]}}
                    for i, rid in enumerate(recordings, 1)]}]})
        self.releases[release_id] = result
        return result

    def add_private_aliases(self, expected):
        for row in expected.values():
            rid = row['recording_id']
            alias, release_id = self.identifier('alias/' + rid), self.identifier('alias-release/' + rid)
            self.recordings[alias] = {**deepcopy(self.recordings[rid]), 'id': alias}
            self.set_memberships(alias, [{'id': release_id, 'title': row['title'] + ' synthetic promo'}])
            self.release_for(release_id, row['title'] + ' synthetic promo', [alias], row['artist'])
            self.alternatives[rid] = (rid, alias)

    def preview(self, album, originals, **options):
        before = len(self.provider_calls)
        with redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, 'DISPOSABLE-OFFLINE-TEST-KEY', auto_mode=True, **options)
        self.assert_sources_preserved()
        self.assertIsNone(plan['interpretation'])
        self.assertIsNone(plan['reviewed_decision'])
        self.assertEqual('independently_verified', plan['provenance'])
        self.assertTrue(all(audit['assessment']['complete'] and not audit['assessment']['errors']
                            for audit in plan['audits']))
        for audit in plan['audits']:
            for candidate in audit['assessment']['candidates']:
                self.assertEqual('verified', candidate['status'])
                self.assertGreaterEqual(candidate['score'], .95)
                self.assertLessEqual(candidate['duration_difference'], 3)
                self.assertTrue(candidate['details']['release-list-complete'])
        self.assertTrue(all(identity['verified'] and not identity['user_approved']
                            for identity in plan['identities'].values()))
        self.preview_calls = self.provider_calls[before:]
        hydrations = Counter(args[0] for name, args, options in self.preview_calls if name == 'get_recording_by_id')
        self.assertLessEqual(sum(hydrations.values()), 2 * len(album['tracks']))
        self.assertTrue(all(count == 1 for count in hydrations.values()))
        tracklists = [call for call in self.preview_calls
                      if call[0] == 'get_release_by_id' and call[2]['includes'] == ['recordings']]
        self.assertLessEqual(len(tracklists), lookup.MAX_RELEASE_EVALUATIONS)
        pages = Counter(call[2]['recording'] for call in self.preview_calls if call[0] == 'browse_releases')
        self.assertTrue(all(count <= lookup.MAX_RELEASE_MEMBERSHIP_PAGES for count in pages.values()))
        return plan

    def commit_catalog_album(self, album, expected, originals):
        # The catalog-suffixed discovery hint is deliberately NOT the expected
        # album tag. Other cases can reuse _ProviderCase.commit unchanged.
        plan = self.preview(album, originals)
        first = next(iter(expected.values()))['recording_id']
        release = self.releases[self.recordings[first]['release-list'][0]['id']]
        self.assertNotEqual(album['album'], release['title'])
        with redirect_stdout(StringIO()):
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertTrue(receipt)
        self.assertEqual(len(expected), len(receipt.input_map))
        for source, row in expected.items():
            saved = receipt.input_map[source.relative_to(self.stage).as_posix()]
            output = Path(saved['destination'])
            self.assertTrue(output.is_relative_to(self.library))
            self.assertEqual(originals[source], saved['source_sha256'])
            self.assertEqual(sha(output), saved['destination_sha256'])
            metadata = read_metadata(output)
            self.assertEqual((row['title'],), metadata['title'])
            self.assertEqual((row['artist'],), metadata['artist'])
            self.assertEqual((release['title'],), metadata['album'])
            self.assertEqual((release['artist-credit-phrase'],), metadata['album_artist'])
            self.assertEqual((release['release-group']['first-release-date'],), metadata['date'])
            self.assertEqual(('Electronic',), metadata['genre'])
            self.assertEqual(f"{row['number']:02d}", tag.parse_track_number(metadata['track_number'])[0])
            self.assertEqual({len(expected)}, tag.get_track_totals(metadata))
            self.assertEqual((), metadata['disc_number'])
            self.assertEqual((), metadata['disc_total'])
            self.assertEqual(f"{row['number']:02d} - {sort.safe_path_part(row['title'])}{source.suffix}", output.name)
            self.assertEqual(release['title'], output.parent.name)
        self.assert_sources_preserved()
        backups = [path for path in self.backup.glob('*/manifest.json')
                   if json.loads(path.read_text()).get('files') == receipt.source_manifest['payload_hashes']]
        self.assertTrue(backups)
        for manifest in backups:
            self.assertEqual(receipt.source_manifest['payload_hashes'], sort.file_manifest(manifest.parent / 'original'))
        self.assertFalse(list(self.root.glob('.mixie-work-*')))
        return plan, receipt

    def assert_cohort(self, plan, expected):
        self.assertEqual(len(expected), len(plan['audits']))
        self.assertTrue(all(lookup._accepted_recording(audit, independent=True) is None for audit in plan['audits']))
        self.assertTrue(all(audit['assessment']['status'] == 'conflicting' and audit['details'] is None
                            and len(audit['assessment']['candidates']) == 2 for audit in plan['audits']))
        self.assertTrue(any('resolved_by_whole_album_fingerprints' in row['reasons']
                            for row in plan['evaluations']))
        self.assertEqual({('1', str(row['number'])): source for source, row in expected.items()}, plan['matched'])
        for source, row in expected.items():
            self.assertEqual(row['recording_id'], plan['identities'][str(source)]['recording_id'])
        changes = [row for row in plan['proposals'] if row['field'] == 'track_number']
        self.assertEqual(len(expected), len(changes))
        self.assertTrue(all(row.get('verified_release_position') is True for row in changes))

    def assert_one_release_fetch(self):
        self.assertEqual(1, sum(name == 'get_release_by_id' and options['includes'] == ['recordings']
                                for name, args, options in self.preview_calls))

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): sha(path)
                for path in self.root.rglob('*') if path.is_file()}

    def assert_review_without_writes(self, album, originals, exception) -> dict:
        before, art_before = self.snapshot(), len(self.artwork_calls)
        with redirect_stdout(StringIO()), self.assertRaises(exception) as caught:
            mixie.preview_album(album, 'DISPOSABLE-OFFLINE-TEST-KEY', auto_mode=True)
        self.assertEqual(before, self.snapshot(), 'A rejected preview wrote music, backup or application state')
        self.assert_sources_preserved()
        self.assertFalse(any(self.library.rglob('*')))
        self.assertFalse(any(self.backup.rglob('*')))
        self.assertFalse(list(self.root.glob('.mixie-work-*')))
        self.assertEqual(art_before, len(self.artwork_calls), 'Artwork must not authorize an unresolved identity')
        details = caught.exception.details
        if not isinstance(details, dict):
            self.fail('Review exception must carry structured identity evidence')
        self.assertEqual(len(album['tracks']), len(details['tracks']))
        for audit in details['tracks']:
            self.assertTrue(audit['assessment']['complete'])
            self.assertFalse(audit['assessment']['errors'])
            self.assertEqual(2, len(audit['assessment']['candidates']))
            self.assertTrue(all(row['status'] == 'verified' and row['details']['release-list-complete']
                                for row in audit['assessment']['candidates']))
        return details

    def test_back_from_dead_all_tracks_ambiguous_commit_canonical_mp3_and_flac(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                fixture = self.make_named_album('Back From the Dead', suffix, BACK_FROM_DEAD_TITLES, artist='Halestorm')
                self.add_private_aliases(fixture[1])
                plan, receipt = self.commit(*fixture)
                self.assert_cohort(plan, fixture[1])
                self.assert_one_release_fetch()
                self.assertEqual(11, len(receipt.input_map))
                first = next(source for source, row in fixture[1].items() if row['number'] == 1)
                output = Path(receipt.input_map[first.relative_to(self.stage).as_posix()]['destination'])
                self.assertEqual(f'01 - Back From the Dead.{suffix}', output.name)

    def test_strange_case_all_tracks_ambiguous_keep_explicit_evidence_in_final_outputs(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                fixture = self.make_named_album('The Strange Case of...', suffix, STRANGE_CASE_TITLES,
                                                artist='Halestorm', date='2012-04-10')
                for row in fixture[1].values():
                    if row['number'] in (4, 10, 12, 13):
                        self.recordings[row['recording_id']]['disambiguation'] = 'explicit'
                        release_id = self.recordings[row['recording_id']]['release-list'][0]['id']
                        self.releases[release_id]['medium-list'][0]['track-list'][row['number'] - 1]['recording']['disambiguation'] = 'explicit'
                self.add_private_aliases(fixture[1])
                plan, receipt = self.commit(*fixture)
                self.assert_cohort(plan, fixture[1])
                self.assert_one_release_fetch()
                self.assertEqual(13, len(receipt.input_map))
                source = next(source for source, row in fixture[1].items() if row['number'] == 4)
                output = Path(receipt.input_map[source.relative_to(self.stage).as_posix()]['destination'])
                self.assertEqual(f'04 - Freak Like Me.{suffix}', output.name)
                audit = next(audit for audit in plan['audits'] if audit['path'] == source)
                self.assertEqual({'explicit'}, {row['details']['disambiguation'] for row in audit['assessment']['candidates']})

    def test_witch_verified_featured_band_member_reaches_actual_output(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                fixture = self.make_named_album('Witch', suffix, WITCH_TITLES, artist='In This Moment',
                    source_tags={2: {'title': 'I Want to Believe (feat. Dayseeker)'}})
                source = next(source for source, row in fixture[1].items() if row['number'] == 2)
                row = fixture[1][source]
                guest = 'Rory Rodriguez of Dayseeker'
                row['artist'] = 'In This Moment feat. ' + guest
                self.recordings[row['recording_id']]['artist-credit-phrase'] = row['artist']
                release_id = self.recordings[row['recording_id']]['release-list'][0]['id']
                self.releases[release_id]['medium-list'][0]['track-list'][1]['title'] = f'I Want to Believe (feat. {guest})'
                self.add_private_aliases(fixture[1])
                plan, receipt = self.commit(*fixture)
                self.assert_cohort(plan, fixture[1])
                output = Path(receipt.input_map[source.relative_to(self.stage).as_posix()]['destination'])
                self.assertEqual(('In This Moment',), read_metadata(output)['artist'])
                self.assertEqual((f'I Want to Believe (feat. {guest})',), read_metadata(output)['title'])
                self.assertEqual(f'02 - I Want to Believe (feat. {guest}).{suffix}', output.name)
                self.assertEqual(('I Want to Believe (feat. Dayseeker)',), read_metadata(source)['title'])

    def test_americana_two_pass_weak_fingerprints_and_no_hit_commit_with_shuffled_numbers(self):
        # This is encoded synthetic silence, NOT a snapshot of the user's real audio.
        # Fake provider observations exercise the complete working-copy pipeline.
        titles = AMERICANA_TITLES
        source_tags = {i: {'track_number': str(14-i), 'track_total': '13'}
                       for i in range(1, 14)}
        fixture = self.make_named_album('Americana', 'flac', titles,
            artist='The Offspring', date='1998-11-17', source_tags=source_tags)
        sources, expected, originals = fixture
        by_number = {row['number']: row['recording_id'] for row in expected.values()}
        weak_ids = {by_number[i] for i in range(8, 13)}
        no_hit = by_number[13]
        observed = []
        ordinary_identify = self.identify

        def synthetic_identify(fingerprint, key):
            rid = fingerprint['fingerprint'].removeprefix('fixture:')
            observed.append(rid)
            if rid == no_hit:
                return []
            if rid in weak_ids:
                return [{'score': .808, 'recordings': [{'id': rid,
                         'title': self.recordings[rid]['title']}]}]
            return ordinary_identify(fingerprint, key)

        with patch.object(audio, 'identify_audio', side_effect=synthetic_identify):
            with redirect_stdout(StringIO()):
                plan = mixie.preview_album(sources, 'DISPOSABLE-OFFLINE-TEST-KEY', auto_mode=True)
            self.assertEqual(len(expected), len(set(observed)))
            self.assertEqual(13, len(plan['matched']))
            self.assertTrue(any('resolved_by_album_context' in report['reasons']
                                for report in plan['evaluations']))
            self.assertTrue(any(row['field'] == 'track_number' and row.get('verified_release_position')
                                for row in plan['proposals']))
            self.assertEqual('independently_verified', plan['provenance'])
            with redirect_stdout(StringIO()):
                receipt = mixie.test_album_plan(plan, sources, commit=True, auto=True)
        self.assertEqual(13, len(receipt.input_map))
        for source, row in expected.items():
            destination = Path(receipt.input_map[source.relative_to(self.stage).as_posix()]['destination'])
            self.assertEqual((row['title'],), read_metadata(destination)['title'])
            self.assertEqual(f'{row["number"]:02d}', tag.parse_track_number(
                read_metadata(destination)['track_number'])[0])
        self.assert_sources_preserved()

    def test_joint_halestorm_i_prevail_single_keeps_shared_lead_artist_automatically(self):
        title, credit = 'can u see me in the dark?', 'Halestorm & I Prevail'
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                fixture = self.make_named_album(title, suffix, (title,), artist=credit, date='2024-06-06',
                                                source_tags={1: {'artist': 'Halestorm'}})
                source, row = next(iter(fixture[1].items()))
                # User's preferred Artist is the overlapping real lead, while
                # Album Artist continues to describe the collaborative release.
                row['artist'] = 'Halestorm'
                rid = row['recording_id']
                self.recordings[rid]['artist-credit'] = [
                    {'artist': {'id': self.identifier('Halestorm'), 'name': 'Halestorm'}, 'joinphrase': ' & '},
                    {'artist': {'id': self.identifier('I Prevail'), 'name': 'I Prevail'}}]
                release_id = self.recordings[rid]['release-list'][0]['id']
                self.releases[release_id]['release-group']['primary-type'] = 'Single'
                self.recordings[rid]['release-list'][0]['release-group']['primary-type'] = 'Single'
                plan, receipt = self.commit(*fixture)
                self.assertIsNotNone(lookup._accepted_recording(plan['audits'][0], independent=True))
                output = Path(receipt.input_map[source.relative_to(self.stage).as_posix()]['destination'])
                self.assertEqual(('Halestorm',), read_metadata(output)['artist'])
                self.assertEqual((credit,), read_metadata(output)['album_artist'])
                self.assertEqual((title,), read_metadata(output)['title'])
                self.assertEqual((f'{title} (Single)',), read_metadata(output)['album'])
                self.assertEqual(credit, output.parent.parent.name)
                self.assertEqual(('Halestorm',), read_metadata(source)['artist'])

    def test_unknown_album_verified_single_receives_song_single_title(self):
        album, rows, originals = self.make_named_album(
            'Bad Dreams', 'flac', ('Bad Dreams',), artist='Conquer Divide')
        album['album'] = 'Unknown Album'
        rid = next(iter(rows.values()))['recording_id']
        release_id = self.recordings[rid]['release-list'][0]['id']
        self.releases[release_id]['release-group']['primary-type'] = 'Single'
        self.recordings[rid]['release-list'][0]['release-group']['primary-type'] = 'Single'
        plan = self.preview(album, originals)
        self.assertEqual('Bad Dreams (Single)', plan['auto_album_fields']['album'])
        with redirect_stdout(StringIO()):
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        source = next(iter(rows))
        output = Path(receipt.input_map[source.relative_to(self.stage).as_posix()]['destination'])
        self.assertEqual(('Bad Dreams (Single)',), read_metadata(output)['album'])
        self.assertEqual(('Bad Dreams',), read_metadata(output)['title'])
        self.assert_sources_preserved()

    def test_one_track_album_type_album_not_mislabeled_single(self):
        album, rows, originals = self.make_named_album(
            'One Track Work', 'flac', ('One Track Work',), artist='Synthetic Artist')
        rid = next(iter(rows.values()))['recording_id']
        release_id = self.recordings[rid]['release-list'][0]['id']
        self.releases[release_id]['release-group']['primary-type'] = 'Album'
        self.recordings[rid]['release-list'][0]['release-group']['primary-type'] = 'Album'
        plan = self.preview(album, originals)
        self.assertEqual('One Track Work', plan['auto_album_fields']['album'])
        self.assert_sources_preserved()

    def add_retail_nominations(self, expected, *, all_tracks=False):
        rows = list(expected.values())
        release_id = self.recordings[rows[0]['recording_id']]['release-list'][0]['id']
        decoys = [{'id': self.identifier(f'retail/{release_id}/{i}'), 'title': 'Synthetic compilation'} for i in range(110)]
        self.assertGreater(len(decoys), lookup.MAX_RELEASE_EVALUATIONS)
        self.assertLessEqual(len(decoys), 110)
        for row in rows if all_tracks else rows[:2]:
            rid = row['recording_id']
            for candidate in self.alternatives.get(rid, (rid,)):
                memberships = self.memberships.get(candidate, self.recordings[candidate]['release-list'])
                self.set_memberships(candidate, [*memberships, *decoys])
        return release_id, decoys

    def test_managed_pending_retry_preserves_staged_originals_without_loose_backup(self):
        fixture = self.make_named_album('Pending retry synthetic', 'mp3',
            ('First synthetic song', 'Second synthetic song', 'Third synthetic song'),
            artist='Synthetic Artist')
        album, expected, originals = fixture
        plan = self.preview(album, originals)
        with patch.object(mixie, '_import_first_enabled', return_value=True), \
             redirect_stdout(StringIO()):
            self.assertIsNone(mixie.test_album_plan(
                plan, album, commit=False, auto=True, preserve_staged_backup=False))
            self.assertFalse(list(self.backup.glob('*/manifest.json')))
            receipt = mixie.test_album_plan(
                plan, album, commit=True, auto=True, preserve_staged_backup=False)
        self.assertIsNotNone(receipt)
        self.assertEqual(len(expected), len(receipt.input_map))
        self.assertFalse(list(self.backup.glob('*/manifest.json')))
        self.assert_sources_preserved()
        self.assertTrue(all(Path(row['destination']).is_file()
                            for row in receipt.input_map.values()))

    def test_americana_catalog_core_and_exhaustive_memberships_skip_only_irrelevant_retails(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                fixture = self.make_named_album('Americana', suffix, AMERICANA_TITLES,
                    artist='The Offspring', date='1998-11-16', source_tags={
                        i: {'albumartist': 'The Offspring', 'album': 'Americana (ESCA 7393)'}
                        for i in range(1, len(AMERICANA_TITLES) + 1)})
                self.add_private_aliases(fixture[1])
                release_id, decoys = self.add_retail_nominations(fixture[1])
                fixture[0]['album'] = 'Americana (ESCA 7393)'
                self.search_answers[('The Offspring', 'Americana')] = [release_id]
                plan, receipt = self.commit_catalog_album(*fixture)
                self.assert_cohort(plan, fixture[1])
                self.assert_one_release_fetch()
                self.assertEqual([release_id], plan['compatible_ids'])
                self.assertEqual(13, len(receipt.input_map))
                ranked = lookup.discover_release_candidates(plan['audits'], local_album=fixture[0]['album'])
                self.assertEqual(release_id, ranked[0])
                self.assertGreater(len(ranked), lookup.MAX_RELEASE_EVALUATIONS)
                self.assertTrue(any(call[0] == 'browse_releases' for call in self.preview_calls))
                searches = [call[2] for call in self.preview_calls if call[0] == 'search_releases']
                self.assertEqual([
                    {'artist': 'The Offspring', 'release': 'Americana', 'limit': 5},
                    {'artist': 'The Offspring', 'release': 'Americana', 'catno': 'ESCA 7393', 'limit': 25},
                ], searches)
                unexamined = next(report['unexamined_release_ids'] for report in plan['evaluations']
                                  if 'release_evaluation_budget_exhausted' in report['reasons'])
                self.assertTrue({row['id'] for row in decoys}.issubset(unexamined))
                consensus = lookup.compare_release_candidate_reports(plan['audits'], plan['evaluations'],
                                                                      unexamined_release_ids=unexamined)
                self.assertEqual('complete', consensus['status'])
                self.assertEqual([], consensus['unresolved_release_ids'])

    def test_exhaustive_memberships_exclude_unexamined_retails_with_independent_anchors(self):
        # Positive counterpart to the one authorized legacy test policy change:
        # these rich audits really DO prove that retail nominations cannot cover
        # the complete supplied album, rather than assuming retail equivalence.
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                fixture = self.make_named_album('Exhaustive membership album', suffix,
                    ('First synthetic song', 'Second synthetic song', 'Third synthetic song'), artist='Synthetic Artist')
                release_id, decoys = self.add_retail_nominations(fixture[1])
                plan, receipt = self.commit(*fixture)
                self.assertTrue(all(lookup._accepted_recording(audit, independent=True) for audit in plan['audits']))
                self.assert_one_release_fetch()
                self.assertEqual([release_id], plan['compatible_ids'])
                self.assertEqual(3, len(receipt.input_map))
                retained = next(report['unexamined_release_ids'] for report in plan['evaluations']
                                if 'release_evaluation_budget_exhausted' in report['reasons'])
                self.assertEqual({row['id'] for row in decoys}, set(retained))

    def test_live_competitors_require_review_clean_explicit_catalogs_can_import(self):
        for version in ('live at a synthetic venue', 'clean', 'explicit'):
            for suffix in ('mp3', 'flac'):
                with self.subTest(format=suffix, version=version):
                    fixture = self.make_named_album('Unresolved ' + version, suffix,
                        ('First synthetic song', 'Second synthetic song', 'Third synthetic song'), artist='Synthetic Artist')
                    self.add_private_aliases(fixture[1])
                    selected = list(fixture[1].values())[1]['recording_id']
                    alias = self.alternatives[selected][1]
                    self.recordings[alias]['disambiguation'] = version
                    if version.startswith('live'):
                        details = self.assert_review_without_writes(
                            fixture[0], fixture[2], review.AlbumReviewRequired)
                        self.assertTrue(all(row['assessment']['complete'] for row in details['tracks']))
                        self.assertTrue(any('unresolved_recording_version' in row['reasons']
                                            for row in details['releases']))
                        self.assertEqual(2, len(details['tracks'][0]['assessment']['candidates']))
                    else:
                        plan, receipt = self.commit(*fixture)
                        self.assertEqual(3, len(receipt.input_map))
                        self.assertEqual(3, len(plan['matched']))
                        self.assert_sources_preserved()

    def test_unavailable_competing_album_requires_review_without_any_writes(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                fixture = self.make_named_album('Unavailable competitor', suffix,
                    ('First synthetic song', 'Second synthetic song', 'Third synthetic song'), artist='Synthetic Artist')
                self.add_private_aliases(fixture[1])
                canonical = next(iter(fixture[1].values()))['recording_id']
                release_id = self.recordings[canonical]['release-list'][0]['id']
                competitor = self.identifier('unavailable/' + release_id)
                aliases = [self.alternatives[row['recording_id']][1] for row in fixture[1].values()]
                for alias in aliases:
                    self.set_memberships(alias, [{'id': competitor, 'title': 'Synthetic alternate album'}])
                self.release_for(competitor, 'Synthetic alternate album', aliases, 'Synthetic Artist', kind='Album')
                self.failed_releases[competitor] = TimeoutError('Synthetic competing tracklist outage; never real network')
                details = self.assert_review_without_writes(fixture[0], fixture[2], mixie.ProviderUnavailable)
                self.assertTrue(details['retryable'])
                self.assertTrue(details['provider_errors'])
                self.assertIn('album_competitors_unexamined_or_incomplete', details['uncertainty_reason'])
                reports = {row['release_id']: row for row in details['releases']}
                self.assertEqual(3, len(reports[release_id]['matched']))
                self.assertIn('resolved_by_whole_album_fingerprints', reports[release_id]['reasons'])
                self.assertIn('release_lookup_unavailable', reports[competitor]['reasons'])
                self.assertTrue(reports[competitor]['retryable'])

    def test_americana_potentially_relevant_unexamined_retails_cannot_be_ignored(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                fixture = self.make_named_album('Americana', suffix,
                    ('Welcome', 'Have You Ever', 'Staring at the Sun'), artist='The Offspring', source_tags={
                        i: {'albumartist': 'The Offspring', 'album': 'Americana (ESCA 7393)'} for i in range(1, 4)})
                self.add_private_aliases(fixture[1])
                release_id, decoys = self.add_retail_nominations(fixture[1], all_tracks=True)
                recordings = [row['recording_id'] for row in fixture[1].values()]
                for decoy in decoys:
                    self.release_for(decoy['id'], decoy['title'], recordings, 'The Offspring', kind='Album',
                                     template=self.releases[release_id])
                fixture[0]['album'] = 'Americana (ESCA 7393)'
                self.search_answers[('The Offspring', 'Americana')] = [release_id]
                before = len(self.provider_calls)
                details = self.assert_review_without_writes(fixture[0], fixture[2], review.AlbumReviewRequired)
                calls = self.provider_calls[before:]
                self.assertEqual(lookup.MAX_RELEASE_EVALUATIONS,
                    sum(name == 'get_release_by_id' and options['includes'] == ['recordings']
                        for name, args, options in calls))
                self.assertIn('album_competitors_unexamined_or_incomplete', details['reason'])
                remainder = next(row['unexamined_release_ids'] for row in details['releases']
                                 if 'release_evaluation_budget_exhausted' in row['reasons'])
                self.assertTrue({row['id'] for row in decoys}.intersection(remainder))
