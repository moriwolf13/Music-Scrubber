"""Final metadata contracts on actual disposable MP3/FLAC outputs.

Run only via tests/run_isolated.py. Audio is encoded silence; fingerprints and
provider transports are controlled doubles, NOT real-world identity acceptance.
Decode, evidence assessment, release matching, metadata, backups and library
publication are real. Named albums are regression scenarios, not fetched data.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
import hashlib
import json
import shutil
import uuid

from mutagen import File
from mutagen.flac import FLAC
from mutagen.id3 import TCON, TXXX

import audio
import lookup
import mixie
import sort
import tag
from support import WorkspaceCase, make_audio, make_cover


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_metadata(path):
    metadata = tag.read_track_metadata(path)
    if metadata is None:
        raise AssertionError(f'Expected readable audio metadata: {path}')
    return metadata


class _Response(BytesIO):
    def __init__(self, data, mime):
        super().__init__(data)
        self.headers = Message()
        self.headers['Content-Type'] = mime


class _ProviderCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.recordings, self.releases, self.tokens = {}, {}, {}
        self.cover = make_cover()
        self.clear_caches()
        self.addCleanup(self.clear_caches)
        self.stack.enter_context(patch.object(audio, 'script_folder', self.script))
        self.stack.enter_context(patch.object(audio, 'staging_folder', self.stage))
        self.stack.enter_context(patch.object(audio, 'DECODE_TIMEOUT', 10))
        self.stack.enter_context(patch.object(audio, 'fingerprint_audio', side_effect=self.fingerprint))
        self.stack.enter_context(patch.object(audio, 'identify_audio', side_effect=self.identify))
        self.stack.enter_context(patch.object(lookup, '_musicbrainz_call', side_effect=self.provider))
        self.stack.enter_context(patch.object(lookup, 'urlopen', side_effect=self.artwork))
        self.stack.enter_context(patch.object(audio, 'urlopen', side_effect=AssertionError('Unexpected provider')))
        self.stack.enter_context(patch('builtins.input', side_effect=AssertionError('Unexpected prompt')))

    def clear_caches(self):
        for function in (lookup.get_recording_details, lookup.get_release_tracklist,
                         lookup.get_release_metadata):
            getattr(function, 'cache_clear')()

    def identifier(self, token):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self.id() + '/' + token))

    def fingerprint(self, path):
        path = Path(path).resolve()
        self.assertTrue(path.is_relative_to(self.stage))
        self.assertIn(path.name, self.tokens)
        return {'duration': 0.15, 'fingerprint': 'fixture:' + self.tokens[path.name]}

    def identify(self, fingerprint, api_key):
        self.assertEqual('DISPOSABLE-OFFLINE-TEST-KEY', api_key)
        identifier = fingerprint['fingerprint'].removeprefix('fixture:')
        self.assertIn(identifier, self.recordings)
        return [{'score': 0.99, 'recordings': [{'id': identifier}]}]

    def provider(self, operation, *args, **kwargs):
        self.assertEqual(1, len(args))
        identifier = args[0]
        if operation.__name__ == 'get_recording_by_id' and identifier in self.recordings:
            return {'recording': deepcopy(self.recordings[identifier])}
        if operation.__name__ == 'get_release_by_id' and identifier in self.releases:
            return {'release': deepcopy(self.releases[identifier])}
        self.fail(f'Unexpected MusicBrainz transport: {operation.__name__} {identifier}')

    def artwork(self, request, *, timeout):
        url = urlsplit(request.full_url)
        self.assertEqual(('https', 'coverartarchive.org'), (url.scheme, url.netloc))
        identifier = url.path.removeprefix('/release/').removesuffix('/front')
        self.assertIn(identifier, self.releases)
        if url.path.endswith('/front'):
            return _Response(self.cover['data'], self.cover['mime'])
        image = f'https://coverartarchive.org/release/{identifier}/front'
        return _Response(json.dumps({'images': [{'front': True, 'approved': True,
            'image': image, 'thumbnails': {'250': image, '500': image}}]}).encode(),
            'application/json')

    def fixture(self, name, suffix, *, sizes=(3,), supplied=None, titles=None,
                artist='3OH!3', credits=None, date='2021-08-27', numbering='wrong',
                genres=None, source_date='2023', tagless=False):
        """Create managed sources with independently supplied provider positions."""
        release_id = self.identifier(name + suffix)
        remote, media = [], []
        for disc, size in enumerate(sizes, 1):
            entries = []
            for number in range(1, size + 1):
                position = (disc, number)
                identifier = self.identifier(f'{name}/{suffix}/{disc}/{number}')
                title = (titles or {}).get(position, f'{name} fixture song {disc}-{number}')
                credit = (credits or {}).get(position, artist)
                row = {'disc': disc, 'number': number, 'recording_id': identifier,
                       'title': title, 'artist': credit}
                remote.append(row)
                self.recordings[identifier] = {'id': identifier, 'title': title,
                    'artist-credit-phrase': credit, 'length': '150',
                    'artist-credit': [{'artist': {'id': self.identifier(credit), 'name': credit}}],
                    'release-count': '1', 'release-list': [{'id': release_id, 'title': name,
                        'release-group': {'primary-type': 'Album'}}]}
                entries.append({'id': self.identifier('track/' + identifier),
                    'number': str(number), 'position': str(number), 'title': title,
                    'length': '150', 'recording': {'id': identifier, 'title': title, 'length': '150'}})
            media.append({'position': str(disc), 'format': 'CD', 'track-count': str(size),
                          'track-list': entries})
        self.releases[release_id] = {'id': release_id, 'title': name,
            'artist-credit-phrase': artist, 'date': '2023-01-01',
            'release-group': {'id': self.identifier('group/' + name), 'primary-type': 'Album',
                              'first-release-date': date},
            'medium-count': str(len(media)), 'medium-list': media}
        chosen = [row for row in remote if supplied is None or (row['disc'], row['number']) in supplied]
        delivery_name = name.replace(' ', '-') + '-' + suffix
        inbox = self.inbox / delivery_name
        expected, originals = {}, {}
        for index, row in enumerate(chosen):
            # Filenames, titles and embedded numbers deliberately tell different stories.
            filename = f'{900 - index:03d}-misleading-{suffix}.{suffix}'
            relative = Path(name) / filename
            values = {'album': 'Wrong source album', 'albumartist': 'Wrong album credit',
                      'artist': artist, 'title': f'Wrong download title {index}',
                      'date': source_date}
            if numbering == 'wrong':
                values.update(tracknumber='88/99', discnumber='9/9')
            elif numbering == 'correct':
                values.update(tracknumber=row['number'], discnumber=row['disc'])
            genre = 'Electronic' if genres is None else genres[index]
            if genre is not None:
                values['genre'] = genre
            source = make_audio(inbox / relative, **values)
            if suffix == 'flac' and numbering == 'wrong':
                handle = File(source)
                for key, value in (('tracktotal', '98'), ('totaltracks', '97'),
                                   ('disctotal', '8'), ('totaldiscs', '7')):
                    handle[key] = [value]
                handle.save()
            if tagless:
                handle = File(source)
                if isinstance(handle, FLAC):
                    handle.clear()
                    handle.save()
                else:
                    handle.delete()
            originals[source] = sha(source)
            self.tokens[filename] = row['recording_id']
            expected[relative] = row
        with redirect_stdout(StringIO()):
            staged = sort.sort_inbox()
        stage = self.stage / sort.staging_import_name(delivery_name, 'directory')
        self.assertIn(stage, [Path(path) for path in staged])
        paths = {stage / relative: row for relative, row in expected.items()}
        originals.update({path: sha(path) for path in paths})
        album = {'album': name, 'folder': stage / name,
                 'tracks': [read_metadata(path) for path in reversed(paths)]}
        return album, paths, originals

    def preview(self, album, originals, **options):
        with redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, 'DISPOSABLE-OFFLINE-TEST-KEY', auto_mode=True, **options)
        for path, digest in originals.items():
            self.assertEqual(digest, sha(path))
        self.assertTrue(all(lookup._accepted_recording(row, independent=True) is not None
                            for row in plan['audits']))
        self.assertTrue(all(row['assessment']['complete'] for row in plan['audits']))
        return plan

    def commit(self, album, expected, originals, *, genres=None, **options):
        plan = self.preview(album, originals, **options)
        if plan['release_totals']['disc_total'] == 1:
            self.assertFalse(any(p['field'] in ('disc_number', 'disc_total') for p in plan['proposals']))
        with redirect_stdout(StringIO()):
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertTrue(receipt)
        self.assertEqual(len(expected), len(receipt.input_map))
        for index, (source, row) in enumerate(expected.items()):
            saved_row = receipt.input_map[source.relative_to(self.stage).as_posix()]
            output = Path(saved_row['destination'])
            self.assertTrue(output.is_relative_to(self.library))
            self.assertEqual(originals[source], saved_row['source_sha256'])
            self.assertEqual(sha(output), saved_row['destination_sha256'])
            metadata = read_metadata(output)
            # Expectations come from the supplied provider evidence, not the plan.
            lead, feature, guest = row['artist'].partition(' feat. ')
            credited_title = row['title'] + (f' (feat. {guest})' if feature else '')
            self.assertEqual((lead,), metadata['artist'])
            self.assertEqual((credited_title,), metadata['title'])
            # An independently verified one-track Single whose release title
            # equals its sole song follows the user's library naming policy.
            release = self.releases[plan['compatible_ids'][0]]
            source_album = album['album']
            if (len(expected) == 1
                    and release.get('release-group', {}).get('primary-type') == 'Single'
                    and tag._normalize_label(source_album) == tag._normalize_label(row['title'])):
                source_album = f"{row['title']} (Single)"
            self.assertEqual((source_album,), metadata['album'])
            self.assertEqual((self.releases[plan['compatible_ids'][0]]['artist-credit-phrase'],),
                             metadata['album_artist'])
            self.assertEqual((self.releases[plan['compatible_ids'][0]]['release-group']['first-release-date'],),
                             metadata['date'])
            self.assertEqual(f"{row['number']:02d}", tag.parse_track_number(metadata['track_number'])[0])
            medium = self.releases[plan['compatible_ids'][0]]['medium-list'][row['disc'] - 1]
            self.assertEqual({int(medium['track-count'])}, tag.get_track_totals(metadata))
            self.assertEqual(f"{row['number']:02d} - {sort.safe_path_part(credited_title)}.{source.suffix[1:]}",
                             output.name)
            handle = File(output)
            if plan['release_totals']['disc_total'] == 1:
                self.assertEqual((), metadata['disc_number'])
                self.assertEqual((), metadata['disc_total'])
                self.assertEqual(set(), tag.get_disc_totals(metadata))
                if isinstance(handle, FLAC):
                    self.assertFalse({'discnumber', 'disctotal', 'totaldiscs'} & set(handle.keys()))
                else:
                    self.assertEqual([], handle.tags.getall('TPOS'))
            else:
                self.assertEqual(row['disc'], tag.parse_disc_number(metadata['disc_number'])[0])
                self.assertEqual({plan['release_totals']['disc_total']}, tag.get_disc_totals(metadata))
                self.assertEqual(f"Disc {row['disc']}", output.parent.name)
                if isinstance(handle, FLAC):
                    self.assertEqual([str(row['disc'])], handle['discnumber'])
                    self.assertEqual([str(plan['release_totals']['disc_total'])], handle['disctotal'])
                    self.assertNotIn('totaldiscs', handle)
                    self.assertNotIn('totaltracks', handle)
                else:
                    self.assertEqual([f"{row['disc']}/{plan['release_totals']['disc_total']}"],
                                     list(handle.tags['TPOS'].text))
            expected_genre = ('Electronic',) if genres is None else genres[index]
            self.assertEqual(expected_genre, metadata['genre'])
        for path, digest in originals.items():
            self.assertEqual(digest, sha(path), f'Protected source changed: {path}')
        backups = list(self.backup.glob('*/manifest.json'))
        self.assertTrue(backups)
        matching = [path for path in backups if json.loads(path.read_text()).get('files') ==
                    receipt.source_manifest['payload_hashes']]
        self.assertTrue(matching)
        for manifest in matching:
            self.assertEqual(receipt.source_manifest['payload_hashes'],
                             sort.file_manifest(manifest.parent / 'original'))
        self.assertFalse(list(self.root.glob('.mixie-work-*')))
        return plan, receipt


class FinalMetadataOutputTests(_ProviderCase):
    def streets(self, suffix):
        album, paths, originals = self.fixture('Streets of Gold', suffix, sizes=(13,),
            date='2010-06-29', titles={(1, 1): 'Beaumont', (1, 3): 'My First Kiss'},
            credits={(1, 3): '3OH!3 feat. Ke$ha'})
        plan, receipt = self.commit(album, paths, originals)
        self.assertEqual({13}, set(plan['release_totals']['track_totals'].values()))
        featured = next(source for source, row in paths.items() if row['number'] == 3)
        output = receipt.input_map[featured.relative_to(self.stage).as_posix()]['destination']
        self.assertEqual(('My First Kiss (feat. Ke$ha)',), read_metadata(output)['title'])
        self.assertEqual(('3OH!3',), read_metadata(output)['artist'])

    def test_streets_of_gold_wrong_numbers_mp3_final_output(self):
        self.streets('mp3')

    def test_streets_of_gold_wrong_numbers_flac_final_output(self):
        self.streets('flac')

    def need(self, suffix):
        album, paths, originals = self.fixture('NEED', suffix, sizes=(11,), numbering='missing',
            tagless=True, titles={(1, 3): 'Lonely Machines', (1, 11): "VAMPIRE'S DIET"},
            credits={(1, 3): '3OH!3 and 100 gecs', (1, 11): '3OH!3 feat. Bert McCracken'})
        # An explicit reviewed genre is supported without turning it into audio evidence.
        plan, receipt = self.commit(album, paths, originals, manual_fields={'genre': 'Electronic'})
        self.assertEqual({'1': 11}, plan['release_totals']['track_totals'])
        for source, row in paths.items():
            output = receipt.input_map[source.relative_to(self.stage).as_posix()]['destination']
            metadata = read_metadata(output)
            if row['number'] == 3:
                self.assertEqual(('3OH!3 and 100 gecs',), metadata['artist'])
            elif row['number'] == 11:
                self.assertIn('Bert McCracken', metadata['title'][0])

    def test_need_missing_numbers_and_all_source_tags_mp3_final_output(self):
        self.need('mp3')

    def test_need_missing_numbers_and_all_source_tags_flac_final_output(self):
        self.need('flac')

    def test_two_disc_replacements_keep_actual_disc_numbers_totals_and_joint_credits(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                self.commit(*self.fixture('Joint two disc', suffix, sizes=(3, 2),
                    artist='Halestorm & I Prevail', titles={(2, 1): 'Hate Myself (Live)'}))

    def test_partial_missing_numbers_use_full_release_total_not_local_file_count(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                album, paths, originals = self.fixture('Partial full total ' + suffix, suffix, sizes=(7,),
                    supplied={(1, 2), (1, 4), (1, 7)}, numbering='missing')
                plan, receipt = self.commit(album, paths, originals)
                self.assertEqual('independently_verified_partial', plan['provenance'])
                self.assertEqual(7, plan['release_totals']['album_total'])
                self.assertTrue(all(Path(row['destination']).parent.name ==
                    album['album'] + ' (Incomplete, 3 of 7)' for row in receipt.input_map.values()))

    def test_partial_second_disc_retains_full_disc_layout_and_disc_track_total(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                album, paths, originals = self.fixture('Only second disc ' + suffix, suffix,
                    sizes=(3, 4), supplied={(2, 1), (2, 3), (2, 4)}, numbering='missing')
                plan, receipt = self.commit(album, paths, originals)
                self.assertEqual('independently_verified_partial', plan['provenance'])
                self.assertEqual({'disc_total': 2, 'album_total': 7,
                                  'track_totals': {'1': 3, '2': 4}}, plan['release_totals'])
                self.assertTrue(all(Path(row['destination']).parent.name == 'Disc 2'
                                    for row in receipt.input_map.values()))

    def test_partial_incorrect_numbers_use_verified_positions_and_full_release_totals(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                self.commit(*self.fixture('Partial incorrect ' + suffix, suffix, sizes=(7,),
                    supplied={(1, 2), (1, 4), (1, 7)}))

    def test_small_verified_releases_repair_incorrect_positions_without_a_three_track_floor(self):
        for suffix in ('mp3', 'flac'):
            for size in (1, 2):
                with self.subTest(format=suffix, tracks=size):
                    self.commit(*self.fixture(f'Small verified {size} {suffix}', suffix, sizes=(size,)))

    def test_placeholder_genres_are_removed_from_final_mp3_and_flac_outputs(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                self.commit(*self.fixture('Placeholder genre ' + suffix, suffix,
                    genres=['Unknown', ' unCLASSIFIED ', '']), genres=[()] * 3)

    def test_genuinely_missing_genre_reaches_final_outputs_without_unknown(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                self.commit(*self.fixture('No genre evidence', suffix, genres=[None] * 3),
                            genres=[()] * 3)

    def test_uncertain_mixed_genre_does_not_force_an_invented_album_value(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                self.commit(*self.fixture('Uncertain genre', suffix, genres=['Rock', 'Pop', None]),
                            genres=[('Rock',), ('Pop',), ()])

    def test_verified_date_is_not_replaced_by_unsupported_earlier_source_year(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                self.commit(*self.fixture('Date evidence', suffix, source_date='1990'))


class FinalTagPolicyTests(WorkspaceCase):
    def test_cleanup_removes_all_disc_aliases_on_copies_only_and_is_idempotent(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                source = make_audio(self.stage / suffix / ('source.' + suffix),
                    discnumber='9/9', tracknumber='2/13', title='Preserve me')
                handle = File(source)
                if isinstance(handle, FLAC):
                    handle['disctotal'] = ['8']
                    handle['totaldiscs'] = ['7']
                    handle['comment'] = ['Preserve unrelated metadata']
                else:
                    for name in ('DISCNUMBER', 'DiscTotal', 'totaldiscs'):
                        handle.tags.add(TXXX(encoding=3, desc=name, text=['9']))
                    handle.tags.add(TXXX(encoding=3, desc='catalog', text=['Preserve unrelated metadata']))
                handle.save()
                digest = sha(source)
                working = self.root / ('.mixie-work-disc-' + suffix)
                working.mkdir()
                target = working / source.name
                shutil.copy2(source, target)
                self.assertEqual(1, tag.remove_single_disc_tags_on_copy(target, working))
                self.assertEqual(0, tag.remove_single_disc_tags_on_copy(target, working))
                saved = File(target)
                if isinstance(saved, FLAC):
                    self.assertFalse({'discnumber', 'disctotal', 'totaldiscs'} & set(saved.keys()))
                    self.assertEqual(['Preserve unrelated metadata'], saved['comment'])
                else:
                    self.assertEqual([], saved.tags.getall('TPOS'))
                    self.assertEqual(['catalog'], [frame.desc for frame in saved.tags.getall('TXXX')])
                metadata = read_metadata(target)
                self.assertEqual(('02', 13), tag.parse_track_number(metadata['track_number']))
                self.assertEqual(('Preserve me',), metadata['title'])
                self.assertEqual(digest, sha(source))
                with redirect_stdout(StringIO()):
                    missing = tag.audit_metadata_presence([metadata])
                self.assertNotIn('disc_number', missing)
                self.assertNotIn('disc_total', missing)

    def test_cleanup_of_tagless_single_disc_copy_does_not_require_or_create_disc_tags(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                working = self.root / ('.mixie-work-tagless-' + suffix)
                working.mkdir()
                target = make_audio(working / ('source.' + suffix))
                handle = File(target)
                handle.delete()
                digest = sha(target)
                self.assertEqual(0, tag.remove_single_disc_tags_on_copy(target, working))
                self.assertEqual(digest, sha(target))
                metadata = read_metadata(target)
                self.assertEqual(((), ()), (metadata['disc_number'], metadata['disc_total']))

    def test_single_disc_proposals_ignore_bad_disc_family_instead_of_requiring_replacements(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                source = make_audio(self.stage / suffix / ('disc-tags.' + suffix),
                    tracknumber='1/1', discnumber='corrupt')
                digest = sha(source)
                self.assertEqual([], tag.propose_release_numbering(
                    {('1', '1'): source}, [{'disc': 1, 'number': 1}]))
                self.assertEqual(digest, sha(source))

    def test_unknown_genre_is_rejected_before_any_metadata_write(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                working = self.root / ('.mixie-work-genre-' + suffix)
                working.mkdir()
                target = make_audio(working / ('source.' + suffix))
                before = sha(target)
                for placeholder in ('Unknown', ' unknown ', 'Unclassified'):
                    with self.subTest(genre=placeholder):
                        proposals = [{'path': target, 'field': 'title', 'value': 'Must not be written'},
                                     {'path': target, 'field': 'genre', 'value': placeholder, 'verified': True}]
                        with self.assertRaisesRegex(ValueError, 'uncertain genre'):
                            tag.write_track_metadata(target, proposals)
                        with self.assertRaisesRegex(ValueError, 'uncertain genre'):
                            tag.apply_corrections(proposals, approved=True)
                        with self.assertRaisesRegex(ValueError, 'uncertain genre'):
                            tag.replace_reviewed_text_on_copy(target, working, 'genre', placeholder)
                        with self.assertRaises(ValueError):
                            tag.validate_manual_metadata({'genre': placeholder})
                        self.assertEqual(before, sha(target))

    def test_required_metadata_allows_missing_mixed_and_placeholder_genres_without_invention(self):
        for values in ((None, None, None), ('Unknown', None, 'Unclassified'), ('Rock', 'Pop', None)):
            with self.subTest(genres=values):
                paths = []
                for index, genre in enumerate(values):
                    values_to_write = {'date': '2021'}
                    if genre is not None:
                        values_to_write['genre'] = genre
                    path = make_audio(self.stage / str(len(list(self.stage.iterdir()))) / f'{index}.flac',
                                      **values_to_write)
                    paths.append(path)
                matched = {('1', str(index)): path for index, path in enumerate(paths, 1)}
                originals = {path: sha(path) for path in paths}
                self.assertEqual([], tag.propose_required_album_metadata(matched, {'date': '2021'}))
                self.assertEqual([], tag.propose_required_album_metadata(matched, {'date': '2021'},
                    allow_unclassified=True))
                self.assertEqual([], tag.propose_required_album_metadata(matched, {'date': '2021'},
                    contextual_genre='Unknown'))
                self.assertEqual([], tag.propose_required_album_metadata(matched, {'date': '2021'},
                    canonical_genre='Unknown'))
                self.assertEqual(originals, {path: sha(path) for path in paths})

    def test_noncontiguous_full_release_numbering_is_rejected_before_reading_sources(self):
        source = self.track_path()
        for tracks in ([{'disc': 1, 'number': '2'}],
                       [{'disc': 1, 'number': '1'}, {'disc': 1, 'number': '3'}],
                       [{'disc': 1, 'number': '1'}, {'disc': 2, 'number': '2'}]):
            with self.subTest(tracks=tracks), patch.object(tag, 'read_track_metadata') as reader:
                with self.assertRaisesRegex(ValueError, 'Full release track numbering'):
                    tag.propose_release_numbering({('1', '2' if len(tracks) == 1 else '1'): source},
                        tracks, allow_partial=True, independently_verified=True)
                reader.assert_not_called()


class UncertainGenreCleanupTests(WorkspaceCase):
    def genre_copy(self, suffix, values, name):
        source = make_audio(self.stage / name / ('source.' + suffix),
            title='Preserve title', artist='Artist & Guest', album='Preserve album',
            date='2021-08-27', tracknumber='2/13', discnumber='2/2')
        if values is not None:
            handle = File(source)
            if isinstance(handle, FLAC):
                handle['genre'] = list(values)
            else:
                handle.tags.setall('TCON', [TCON(encoding=1, text=list(values))])
            handle.save()
        working = self.root / ('.mixie-work-' + name + '-' + suffix)
        working.mkdir()
        target = working / source.name
        shutil.copy2(source, target)
        return source, target, working

    def assert_cleaned(self, source, target, working, expected, changed):
        source_hash = sha(source)
        before = {key: value for key, value in read_metadata(target).items()
                  if key not in ('path', 'genre')}
        self.assertEqual(changed, tag.remove_uncertain_genre_on_copy(target, working))
        saved = read_metadata(target)
        self.assertEqual(expected, saved['genre'])
        self.assertEqual(before, {key: value for key, value in saved.items()
                                  if key not in ('path', 'genre')})
        if not expected:
            raw = File(target)
            if isinstance(raw, FLAC):
                self.assertNotIn('genre', raw)
            else:
                self.assertEqual([], raw.tags.getall('TCON') if raw.tags is not None else [])
        cleaned_hash = sha(target)
        self.assertEqual(0, tag.remove_uncertain_genre_on_copy(target, working))
        self.assertEqual(cleaned_hash, sha(target))
        self.assertEqual(source_hash, sha(source))

    def test_placeholder_only_genre_tags_are_removed_and_verified_on_real_copies(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                self.assert_cleaned(*self.genre_copy(suffix,
                    ['Unknown', ' unCLASSIFIED ', '', '  '], 'only-placeholders'), (), 1)

    def test_mixed_genres_keep_real_values_order_duplicates_and_whitespace(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                self.assert_cleaned(*self.genre_copy(suffix,
                    ['Rock', 'Unknown', '', ' unclassified ', 'Unknown Pleasures', 'Rock', ' Pop '],
                    'mixed-genres'), ('Rock', 'Unknown Pleasures', 'Rock', ' Pop '), 1)

    def test_blank_entries_between_real_genres_are_removed_from_raw_tags(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                source, target, working = self.genre_copy(suffix, ['Rock', '', 'Pop'], 'blank-entry')
                raw = File(target)
                values = raw['genre'] if isinstance(raw, FLAC) else raw.tags['TCON'].text
                # Mutagen already normalizes empty ID3 entries on load; do not
                # demand a rewrite when the parsed file is already clean.
                self.assert_cleaned(source, target, working, ('Rock', 'Pop'), int('' in values))
                raw = File(target)
                values = raw['genre'] if isinstance(raw, FLAC) else raw.tags['TCON'].text
                self.assertEqual(['Rock', 'Pop'], list(values))

    def test_real_genres_are_a_byte_identical_noop_not_a_substring_match(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                values = ('Unknown Pleasures', 'Unclassified Sound', 'Rock')
                source, target, working = self.genre_copy(suffix, values, 'real-genres')
                before = sha(target)
                self.assert_cleaned(source, target, working, values, 0)
                self.assertEqual(before, sha(target))

    def test_absent_and_tagless_genres_do_not_create_tags_or_rewrite_files(self):
        for suffix in ('mp3', 'flac'):
            for tagless in (False, True):
                with self.subTest(format=suffix, tagless=tagless):
                    source, target, working = self.genre_copy(suffix, None, 'absent-' + str(tagless))
                    if tagless:
                        File(target).delete()
                    before = sha(target)
                    self.assert_cleaned(source, target, working, (), 0)
                    self.assertEqual(before, sha(target))

    def test_legacy_numeric_mp3_genres_keep_real_classifications_when_removing_unknown(self):
        source, target, working = self.genre_copy('mp3',
            ['(17)Unknown', '(13)', 'Unknown Pleasures'], 'numeric-genres')
        self.assertEqual(('Rock', 'Unknown', 'Pop', 'Unknown Pleasures'),
                         read_metadata(target)['genre'])
        self.assert_cleaned(source, target, working, ('Rock', 'Pop', 'Unknown Pleasures'), 1)

    def test_sources_links_nested_and_other_working_roots_are_rejected_before_open(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                source, target, working = self.genre_copy(suffix, ['Unknown'], 'boundaries')
                symlink = working / ('symlink.' + suffix)
                symlink.symlink_to(target)
                hardlink = working / ('hardlink.' + suffix)
                hardlink.hardlink_to(target)
                alias = self.root / ('.mixie-work-alias-' + suffix)
                alias.symlink_to(working, target_is_directory=True)
                other_working = self.root / ('.mixie-work-other-' + suffix)
                other_working.mkdir()
                other_copy = other_working / source.name
                shutil.copy2(source, other_copy)
                nested = working / 'nested' / source.name
                nested.parent.mkdir()
                shutil.copy2(source, nested)
                protected = [source, target, other_copy, nested]
                for folder in (self.inbox, self.library, self.backup):
                    path = folder / ('protected.' + suffix)
                    shutil.copy2(source, path)
                    protected.append(path)
                hashes = {path: sha(path) for path in protected}
                invalid = [(path, working) for path in protected]
                invalid += [(symlink, working), (hardlink, working),
                            (other_copy, working), (target, alias), (target, self.stage)]
                for path, root in invalid:
                    with self.subTest(path=path, root=root), patch.object(tag, 'File') as opener:
                        with self.assertRaises(PermissionError):
                            tag.remove_uncertain_genre_on_copy(path, root)
                        opener.assert_not_called()
                self.assertEqual(hashes, {path: sha(path) for path in protected})

    def test_unsupported_audio_is_rejected_without_writing(self):
        working = self.root / '.mixie-work-unsupported-genre'
        working.mkdir()
        target = make_audio(working / 'source.wav')
        digest = sha(target)
        with self.assertRaisesRegex(ValueError, 'Unsupported audio format'):
            tag.remove_uncertain_genre_on_copy(target, working)
        self.assertEqual(digest, sha(target))

    def test_save_that_does_not_remove_placeholders_fails_reopen_verification(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                source, target, working = self.genre_copy(suffix, ['Unknown'], 'failed-save')
                original, before = sha(source), sha(target)
                with (patch.object(type(File(target)), 'save', return_value=None),
                      self.assertRaisesRegex(RuntimeError, 'genre cleanup failed verification')):
                    tag.remove_uncertain_genre_on_copy(target, working)
                self.assertEqual(before, sha(target))
                self.assertEqual(original, sha(source))

    def test_failed_audio_reopen_is_an_explicit_verification_error(self):
        for suffix in ('mp3', 'flac'):
            with self.subTest(format=suffix):
                source, target, working = self.genre_copy(suffix, ['Unknown'], 'failed-reopen')
                original = sha(source)
                with (patch.object(tag, 'File', side_effect=[File(target), None]),
                      self.assertRaisesRegex(RuntimeError, 'genre cleanup failed verification')):
                    tag.remove_uncertain_genre_on_copy(target, working)
                self.assertEqual(original, sha(source))
