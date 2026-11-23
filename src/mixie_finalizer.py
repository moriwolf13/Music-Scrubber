"""Verified album ZIPs and resumable, source-bound finalization for import-first.

Never treat an import-only receipt as cleanup permission. Only an independently
validated output receipt, independently verified shared-original delivery ZIPs,
a per-album library ZIP, and a durable quarantine journal permit source removal.
One physically shared source must be entirely accounted for before it moves.
Albums can finish independently while that complete physical source is retained.
Source cleanup additionally requires explicit allow_source_cleanup=True.

All calls are made under MIXIE's existing process-wide ingest_session lock.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

import constants
import mixie_shared_originals
import sort
import tag


LEDGER_NAME = '.mixie-finalization.json'
MANIFEST_NAME = 'mixie-archive-manifest.json'
QUARANTINE_NAME = '.mixie-finalization-quarantine'
SHARED_ORIGINALS_FORMAT = 'shared_originals_v1'


def _sha(path):
    return sort.file_checksum(path).hex()


def _durable_json(path, state):
    sort._atomic_json(path, state)


def _root():
    from archive import _backup_root, backup_folder
    destination = Path(backup_folder).resolve()
    if any(destination.is_relative_to(Path(root).resolve()) for root in (
            sort.inbox_folder, sort.staging_folder, sort.music_library_folder)):
        raise RuntimeError('Archive storage cannot be inside Inbox, staging, or Music Library.')
    return _backup_root()


def _state_path():
    return _root() / LEDGER_NAME


def _state():
    path = _state_path()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError('Unsafe finalization journal.')
    if not path.exists():
        return {'version': 1, 'active': None, 'completed': {}, 'finalized': {}}
    result = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(result, dict) or result.get('version') != 1
            or not isinstance(result.get('completed'), dict)
            or not isinstance(result.get('finalized', {}), dict)
            or (result.get('active') is not None and not isinstance(result['active'], dict))):
        raise RuntimeError('Invalid finalization journal; refuse cleanup.')
    result.setdefault('finalized', {})  # Existing v1 whole-source journals remain recoverable.
    if any(not sort._is_sha256(identity) or not isinstance(row, dict)
           or row.get('fingerprint') != identity for identity, row in result['finalized'].items()):
        raise RuntimeError('Invalid finalized album receipt ledger.')
    return result


def _write(state):
    _durable_json(_state_path(), state)


def _safe_filename(name):
    return sort._safe_name(name) and not name.startswith('.')


def _relative_output(receipt):
    """Find the exact Artist/Album root from receipt audio, not download names."""
    root = sort._safe_directory(sort.music_library_folder)
    tracks = []
    for row in receipt.get('input_map', {}).values():
        target = row.get('destination')
        if not isinstance(target, str):
            raise RuntimeError('Incomplete verified import mapping.')
        path = Path(target).absolute()
        if path.is_symlink() or not path.is_file() or not path.is_relative_to(root):
            raise RuntimeError('Verified import is no longer present under Music Library.')
        rel = path.relative_to(root)
        if len(rel.parts) < 3 or not all(_safe_filename(value) for value in rel.parts[:2]):
            raise RuntimeError('Music Library album must be nested Artist/Album.')
        tracks.append(rel.parts[:2])
    if not tracks or len(set(tracks)) != 1:
        raise RuntimeError('Import receipt has inconsistent Artist/Album destinations.')
    artist, album = tracks[0]
    album_root = sort._safe_directory(root / artist / album)
    for target in receipt['receipt']:
        path = Path(target).absolute()
        if not path.is_relative_to(album_root) or path.is_symlink():
            raise RuntimeError('Album receipt crosses the verified album boundary.')
    return artist, album, album_root


def _claimed_audio(name, tracks, payload):
    if not isinstance(tracks, list) or not tracks or len(set(tracks)) != len(tracks):
        raise RuntimeError('Invalid or overlapping album source claims.')
    claimed = set()
    for track in tracks:
        if (not isinstance(track, str) or '\\' in track
                or any(part in ('', '.', '..') for part in track.split('/'))
                or Path(track).is_absolute() or Path(track).parts[0] != name):
            raise RuntimeError('Invalid album source claim.')
        relative = Path(track).relative_to(name).as_posix()
        if relative not in payload or Path(relative).suffix.casefold() not in constants.music_file_types:
            raise RuntimeError('Album claims missing or non-audio source files.')
        claimed.add(relative)
    return claimed


def _complete_source(name, tracks, *, allow_partial=False):
    """Verify the immutable full delivery, even when only some audio is claimed."""
    if not sort._safe_name(name):
        raise RuntimeError('Invalid staged import name.')
    root = sort._safe_directory(sort.staging_folder)
    stage = sort._safe_directory(root / name)
    provenance = sort.staging_provenance(name, verify_payload=False)
    if provenance is None:
        raise RuntimeError('Legacy stage needs explicit provenance reconciliation before cleanup.')
    inbox = sort._safe_directory(sort.inbox_folder)
    source = inbox / provenance['source_name']
    if source.is_symlink() or not source.exists() or not source.parent == inbox:
        raise RuntimeError('Original Inbox delivery is missing or unsafe.')
    sort._verify_provenance_source(provenance, source)
    payload = provenance['payload_hashes']
    present = {relative for relative in payload if Path(relative).suffix.casefold() in constants.music_file_types}
    claimed = _claimed_audio(name, tracks, payload)
    if not present or (not allow_partial and present != claimed):
        raise RuntimeError('Shared source contains additional or unaccounted audio; retain it staged.')
    if sort.file_manifest(stage) != payload:
        raise RuntimeError('Staged source payload changed.')
    # _verify_provenance_source already hashed the full original snapshot and
    # compared it to these pins. Do not immediately hash that same unit again;
    # archive publication and every cleanup phase independently revalidate it.
    original = (dict(payload) if provenance['source_kind'] == 'directory'
                else {provenance['source_name']: provenance['source_sha256']})
    return {
        'name': name, 'source_name': provenance['source_name'],
        'source_kind': provenance['source_kind'], 'source_hashes': original,
        'stage_hashes': payload, 'source': str(source), 'stage': str(stage),
        'provenance': provenance,
    }


def _archive_entries_original(sources):
    files = {}
    descriptions = []
    for index, source in enumerate(sorted(sources, key=lambda item: (item['source_name'], item['name']))):
        # Stable, descriptive delivery labels without relying on weird downloader names.
        label = f"{index + 1:02d}-{hashlib.sha256(source['name'].encode()).hexdigest()[:10]}"
        root = Path(source['source'])
        if source['source_kind'] == 'directory':
            for relative in sorted(source['source_hashes']):
                member = f'deliveries/{label}/{relative}'
                files[member] = (root / relative, source['source_hashes'][relative])
        else:
            member = f'deliveries/{label}/{root.name}'
            files[member] = (root, source['source_hashes'][root.name])
        descriptions.append({'label': label, 'original_name': source['source_name'],
                             'source_kind': source['source_kind'],
                             'import_name': source['name'],
                             'payload_sha256': source['stage_hashes']})
    return files, {'type': 'untouched_originals', 'deliveries': descriptions}


def _archive_entries_library(folder):
    contents = sort.file_manifest(folder)
    if not contents:
        raise RuntimeError('No Music Library files to archive.')
    return {key: (folder / key, value) for key, value in contents.items()}, {'type': 'corrected_library'}


def _zip_inventory(destination):
    if destination.is_symlink() or not destination.is_file() or not stat.S_ISREG(destination.stat().st_mode):
        raise RuntimeError('Unsafe or absent archive.')
    return sort.zip_manifest(destination)


def _write_zip(root, artist, album, files, descriptor):
    """Write once; verify every decompressed byte, reread sources, never overwrite."""
    backup = sort._safe_directory(_root() / root, create=True)
    folder = sort._safe_directory(backup / artist, create=True)
    manifest = {'version': 1, 'artist': artist, 'album': album, **descriptor,
                'sha256': {key: value for key, (_src, value) in sorted(files.items())}}
    manifest_bytes = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if MANIFEST_NAME in files:
        raise RuntimeError('Reserved backup manifest filename already exists.')
    expected = dict(manifest['sha256'])
    expected[MANIFEST_NAME] = hashlib.sha256(manifest_bytes).hexdigest()
    canonical = folder / (album + '.zip')
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    existing = canonical
    if os.path.lexists(existing):
        if _zip_inventory(existing) != expected:
            existing = folder / f'{album}--{digest[:12]}.zip'
            if os.path.lexists(existing):
                if _zip_inventory(existing) != expected:
                    raise RuntimeError('Backup archive name collision; refuse overwrite.')
    if not os.path.lexists(existing):
        descriptor_fd, temporary = tempfile.mkstemp(prefix='.mixie-final-zip-', suffix='.tmp', dir=folder)
        try:
            with os.fdopen(descriptor_fd, 'wb') as raw:
                with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zipped:
                    zipped.writestr(MANIFEST_NAME, manifest_bytes)
                    for member, (path, digest_expected) in sorted(files.items()):
                        if (path.is_symlink() or not path.is_file() or _sha(path) != digest_expected
                                or member.startswith('/') or any(part in ('', '.', '..') for part in member.split('/'))):
                            raise RuntimeError('Archive input changed or unsafe.')
                        zipped.write(path, arcname=member)
                raw.flush()
                os.fsync(raw.fileno())
            if _zip_inventory(Path(temporary)) != expected:
                raise RuntimeError('ZIP verification failed; originals retained.')
            for path, digest_expected in files.values():
                if _sha(path) != digest_expected:
                    raise RuntimeError('ZIP input changed during creation.')
            if os.path.lexists(existing):
                raise RuntimeError('Archive collision occurred before publication.')
            os.link(temporary, existing)  # no-clobber publication of durable bytes
            sort._sync_directory(folder)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if _zip_inventory(existing) != expected:
        raise RuntimeError('Published archive verification failed.')
    for path, digest_expected in files.values():
        if _sha(path) != digest_expected:
            raise RuntimeError('Archive input changed during verification.')
    return str(existing), _sha(existing), expected


def _validate_shared_originals(row, batch=None):
    """Bind each album's exact input claims to complete immutable deliveries."""
    if batch is None:
        batch = _SharedOriginalBatchProof()
    originals, sources, claims = row['originals'], row['sources'], row['source_claims']
    if (set(originals) != {'format', 'deliveries'}
            or originals['format'] != SHARED_ORIGINALS_FORMAT
            or not isinstance(sources, list) or not sources or not isinstance(claims, dict)
            or not isinstance(originals['deliveries'], dict)):
        raise RuntimeError('Invalid shared-originals album references.')
    by_name = {source['name']: source for source in sources}
    if (len(by_name) != len(sources) or set(by_name) != set(claims)
            or set(by_name) != set(originals['deliveries'])):
        raise RuntimeError('Shared-originals delivery references omit or add a source.')
    for name, source in by_name.items():
        _check_source_record(source)
        reference = originals['deliveries'][name]
        expected = {relative: source['stage_hashes'][relative]
                    for relative in _claimed_audio(name, claims[name], source['stage_hashes'])}
        if (not isinstance(reference, dict)
                or set(reference) != {'archive', 'claimed_audio_sha256'}
                or reference['claimed_audio_sha256'] != expected):
            raise RuntimeError('Shared-originals track inventory differs from album source claims.')
        batch.validate(source, reference['archive'])
    owner = by_name[row['import_name']]
    entry = row['import_receipt']
    if (row['tracks'] != sorted(claims[owner['name']])
            or entry.get('tracks') != row['tracks']
            or entry.get('source_manifest') != {'import_name': owner['name'],
                'payload_hashes': owner['stage_hashes'], 'provenance': owner['provenance']}
            or not isinstance(entry.get('input_map'), dict)
            or set(entry['input_map']) != set(row['tracks'])):
        raise RuntimeError('Shared-originals owner differs from the verified import receipt.')
    for relative, mapping in entry['input_map'].items():
        leaf = Path(relative).relative_to(owner['name']).as_posix()
        if (not isinstance(mapping, dict) or mapping.get('source_sha256') != owner['stage_hashes'].get(leaf)
                or mapping.get('destination_sha256') != row['receipt'].get(mapping.get('destination'))):
            raise RuntimeError('Shared-originals tracks differ from the verified input/output mapping.')


def _validate_archives(transaction, batch=None):
    # Every album's Library ZIP and every referenced original delivery must
    # verify before the complete physical source can move or resume purging.
    rows = transaction.get('albums') or [transaction]
    if not isinstance(rows, list) or not rows:
        raise RuntimeError('Invalid finalization archive rows.')
    # A new batch on each call preserves fresh verification at every
    # destructive cleanup / crash-recovery checkpoint.
    if batch is None:
        batch = _SharedOriginalBatchProof()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('receipt'), dict) or not row['receipt']:
            raise RuntimeError('Invalid finalization output receipt.')
        originals = row['originals']
        shared = isinstance(originals, dict) and 'format' in originals
        if shared:
            _validate_shared_originals(row, batch)
        archives = (('library', 'Library'),) if shared else (
            ('originals', 'Originals'), ('library', 'Library'))
        for key, category in archives:
            record = row[key]
            if (not isinstance(record, dict) or not isinstance(record.get('path'), str)
                    or not isinstance(record.get('entries'), dict) or not record['entries']
                    or not sort._is_sha256(record.get('file_sha256'))):
                raise RuntimeError('Invalid finalization archive record.')
            path = Path(record['path'])
            sort._safe_directory(path.parent)
            if (not path.is_absolute() or '..' in path.parts or not path.is_relative_to(_root() / category)
                    or path.suffix != '.zip' or path.is_symlink()
                    or _sha(path) != record['file_sha256'] or _zip_inventory(path) != record['entries']):
                raise RuntimeError(f'Finalization {key} archive no longer verifies.')
        if row.get('library_root') is not None:
            library_root = sort._safe_directory(row['library_root'])
            if (not library_root.is_relative_to(sort._safe_directory(sort.music_library_folder))
                    or sort.file_manifest(library_root) != row['library_hashes']):
                raise RuntimeError('Music Library album inventory changed; cleanup blocked.')
        for target, digest in row['receipt'].items():
            path = Path(target)
            sort._safe_directory(path.parent)
            if (not path.is_absolute() or '..' in path.parts
                    or not path.is_relative_to(sort._safe_directory(sort.music_library_folder))
                    or path.is_symlink() or not path.is_file() or _sha(path) != digest):
                raise RuntimeError('Verified Music Library file changed; cleanup blocked.')
        if 'fingerprint' in row:
            if row['receipt'] != row['import_receipt']['receipt'] or not row['sources']:
                raise RuntimeError('Finalized import mapping differs from its archive receipt.')
            library_desc = _archive_descriptor(row['library'], 'corrected_library')
            artist, album, library_root = _relative_output(row['import_receipt'])
            if (str(library_root) != row['library_root']
                    or library_desc['sha256'] != row['library_hashes']
                    or library_desc.get('artist') != artist or library_desc.get('album') != album):
                raise RuntimeError('Finalized Library ZIP differs from its import receipt.')
            if not shared:
                # Retain the exact validation rules for existing per-album ZIPs;
                # recognizing old proof must not rewrite or migrate it.
                original_desc = _originals_coverage([row], row['sources'])[0][1]
                claims = {source['name']: {relative: source['stage_hashes'][relative]
                          for relative in _claimed_audio(source['name'], row['source_claims'][source['name']],
                                                         source['stage_hashes'])} for source in row['sources']}
                if (original_desc.get('claimed_audio_sha256') != claims
                        or original_desc.get('artist') != artist or original_desc.get('album') != album):
                    raise RuntimeError('Finalized Originals ZIP differs from its source or library receipt.')


def _item_manifest(path, kind):
    if kind == 'directory':
        return sort.file_manifest(path)
    return {path.name: _sha(path)}


def _check_source_item(source, original, *, kind, purging=False):
    if source.is_symlink() or not source.exists() or (kind == 'directory' and not source.is_dir()) or (
            kind != 'directory' and not source.is_file()):
        raise RuntimeError('Source disappeared during finalization.')
    sort._safe_directory(source.parent)
    actual = _item_manifest(source, kind)
    # Only a durable purge marker permits files already deleted by an interrupted
    # rmtree to be absent. Every surviving byte still needs its original pin.
    if ((not purging and actual != original)
            or any(original.get(name) != digest for name, digest in actual.items())):
        raise RuntimeError('Source bytes changed since verified backup.')


def _quarantine_root(identifier):
    backup = sort._safe_directory(_root() / QUARANTINE_NAME, create=True)
    if not isinstance(identifier, str) or len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier):
        raise RuntimeError('Invalid transaction identifier.')
    return sort._safe_directory(backup / identifier, create=True)


def _check_source_record(source):
    """The journal cannot redefine a delivery's immutable staging provenance."""
    name, original_name = source['name'], source['source_name']
    if not sort._safe_name(name) or not sort._safe_name(original_name):
        raise RuntimeError('Invalid cleanup source name.')
    for field, root, leaf in (('source', sort.inbox_folder, original_name),
                              ('stage', sort.staging_folder, name)):
        parent = sort._safe_directory(root)
        if Path(source[field]) != parent / leaf:
            raise RuntimeError('Finalization source escaped the configured folder.')
    provenance = sort.staging_provenance(name, verify_payload=False)
    if (provenance is None or provenance != source['provenance']
            or provenance['source_name'] != original_name
            or provenance['source_kind'] != source['source_kind']
            or provenance['payload_hashes'] != source['stage_hashes']):
        raise RuntimeError('Finalization source provenance changed.')
    expected = (source['stage_hashes'] if source['source_kind'] == 'directory'
                else {original_name: provenance['source_sha256']})
    if source['source_hashes'] != expected:
        raise RuntimeError('Finalization original inventory differs from provenance.')


def _check_retained_source(source):
    _check_source_record(source)
    _check_source_item(Path(source['source']), source['source_hashes'], kind=source['source_kind'])
    _check_source_item(Path(source['stage']), source['stage_hashes'], kind='directory')


def _cleanup_items(txn, quarantined):
    items, names = [], {'inbox': set(), 'stage': set()}
    for source in txn['sources']:
        _check_source_record(source)
        for category, path_field, name_field, expected_field, kind in (
                ('inbox', 'source', 'source_name', 'source_hashes', source['source_kind']),
                ('stage', 'stage', 'name', 'stage_hashes', 'directory')):
            leaf = source[name_field]
            if leaf in names[category]:
                raise RuntimeError('Overlapping physical cleanup sources.')
            names[category].add(leaf)
            items.append((Path(source[path_field]), quarantined / category / leaf,
                          source[expected_field], kind))
    if {path.name for path in quarantined.iterdir()} - set(names):
        raise RuntimeError('Unexpected finalization quarantine contents.')
    for category, expected in names.items():
        folder = sort._safe_directory(quarantined / category, create=True)
        if {path.name for path in folder.iterdir()} - expected:
            raise RuntimeError('Unaccounted files in finalization quarantine.')
    return items


def _check_cleanup_items(items, *, purging):
    # Preflight EVERY item before moving or deleting any of its siblings.
    for live, held, expected, kind in items:
        if os.path.lexists(held):
            _check_source_item(held, expected, kind=kind, purging=purging)
            if os.path.lexists(live):
                raise RuntimeError('Both live and quarantined copies exist; cleanup stopped.')
        elif os.path.lexists(live):
            if purging:
                raise RuntimeError('Unexpected live source during purge.')
            _check_source_item(live, expected, kind=kind)
        elif not purging:
            raise RuntimeError('Missing source and quarantine copy; cleanup stopped.')


def _check_cleanup_claims(txn):
    rows = txn.get('albums') or [txn]
    if not any('source_claims' in row for row in rows):
        return  # Backwards-compatible recovery of already prepared v1 journals.
    claimed = defaultdict(set)
    for row in rows:
        for name, tracks in row['source_claims'].items():
            source = next((source for source in txn['sources'] if source['name'] == name), None)
            if source is None:
                raise RuntimeError('Cleanup claim has no physical source.')
            if 'sources' in row and not any(saved == source for saved in row['sources']):
                raise RuntimeError('Album backup and cleanup transaction pin different physical sources.')
            leaves = _claimed_audio(name, tracks, source['stage_hashes'])
            if claimed[name] & leaves:
                raise RuntimeError('Two finalized albums claim the same source audio.')
            claimed[name].update(leaves)
    for source in txn['sources']:
        audio = {name for name in source['stage_hashes']
                 if Path(name).suffix.casefold() in constants.music_file_types}
        if not audio or claimed[source['name']] != audio:
            raise RuntimeError('Shared source has unaccounted audio; cleanup blocked.')


def _resume_transaction(state, *, allow_source_cleanup=False):
    txn = state['active']
    if txn is None or not allow_source_cleanup:
        return False
    if txn.get('phase') not in ('prepared', 'purging'):
        raise RuntimeError('Unknown finalization cleanup phase.')
    _validate_archives(txn)
    _check_cleanup_claims(txn)
    quarantined = _quarantine_root(txn['id'])
    items = _cleanup_items(txn, quarantined)
    purging = txn['phase'] == 'purging'
    _check_cleanup_items(items, purging=purging)
    for live, held, expected, kind in items:
        if os.path.lexists(live):
            _check_source_item(live, expected, kind=kind)
            os.rename(live, held)
            sort._sync_directory(live.parent)
            sort._sync_directory(held.parent)
            _check_source_item(held, expected, kind=kind)
    # Recheck the entire held inventory, not just the last item moved.
    _check_cleanup_items(items, purging=purging)
    if not purging:
        txn['phase'] = 'purging'
        _write(state)
    _validate_archives(txn)
    _check_cleanup_items(items, purging=True)
    for _live, held, expected, kind in items:
        if os.path.lexists(held):
            _check_source_item(held, expected, kind=kind, purging=True)
            if kind == 'directory':
                shutil.rmtree(held)
            else:
                held.unlink()
            sort._sync_directory(held.parent)
    for category in ('inbox', 'stage'):
        (quarantined / category).rmdir()
    quarantined.rmdir()
    sort._sync_directory(quarantined.parent)
    fields = ('id', 'sources', 'albums') if txn.get('albums') else ('id', 'originals', 'library', 'receipt', 'sources')
    state['completed'][txn['id']] = {key: deepcopy(txn[key]) for key in fields}
    state['active'] = None
    _write(state)
    return True


def resume_cleanup(*, allow_source_cleanup=False):
    """Resume protected cleanup only with explicit source-mutation authorization."""
    if type(allow_source_cleanup) is not bool:
        raise ValueError('allow_source_cleanup must be an explicit boolean.')
    return _resume_transaction(_state(), allow_source_cleanup=allow_source_cleanup)


def _group_members(albums, index):
    grouped = defaultdict(list)
    for key in albums:
        root = key
        if index is not None:
            root = index.source(key) or key
        grouped[root].append(key)
    return list(grouped.values())




class _SharedOriginalBatchProof:
    """Reuse a complete shared-archive verification only during ONE locked run.

    This is an in-memory acceleration, not a new cleanup proof or a persistent
    trust flag. Every reuse rechecks the published journal, the pinned object
    identity and the file/directory safety constraints. A changed file stamp
    forces fresh SHA256 and ZIP-member verification. Destructive cleanup and
    recovery independently construct fresh proof sets at each transaction
    checkpoint, so cached ingest-time evidence never authorizes deletion.
    """

    def __init__(self):
        self._proofs = {}
        self._published = {}

    @staticmethod
    def _stamp(path, peer=None):
        metadata = mixie_shared_originals._regular(path, peer)
        return (metadata.st_dev, metadata.st_ino, metadata.st_mode,
                metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns,
                metadata.st_ctime_ns)

    def _current(self, source, record):
        store = mixie_shared_originals
        descriptor = store.delivery_descriptor(source)
        shared, identity, journal, partial = store._layout(_root(), descriptor)
        blob = store._check_record(shared, identity, descriptor, record)
        saved = store._read_journal(journal, shared, identity, descriptor)
        if (saved is None or saved['state'] != 'published'
                or store._json(saved['archive']) != store._json(record)):
            raise RuntimeError('Shared-originals journal no longer matches its published pin.')
        partial_present = os.path.lexists(partial)
        signatures = (self._stamp(blob, partial), self._stamp(journal),
                      self._stamp(partial, blob) if partial_present else None)
        key = (identity, store._json(record))
        return descriptor, key, signatures

    def validate(self, source, record):
        descriptor, key, signatures = self._current(source, record)
        if self._proofs.get(key) != signatures:
            # Includes full SHA256, complete ZIP inventory and (for original
            # ZIP deliveries) validation of the nested original ZIP payload.
            mixie_shared_originals.validate_archive(_root(), source, record)
            verified_descriptor, key, signatures = self._current(source, record)
            if verified_descriptor != descriptor:
                raise RuntimeError('Shared-originals descriptor changed during verification.')
            self._proofs[key] = signatures
        return descriptor

    def ensure(self, source):
        descriptor = mixie_shared_originals.delivery_descriptor(source)
        identity = hashlib.sha256(mixie_shared_originals._json(descriptor)).hexdigest()
        previous = self._published.get(identity)
        if previous is not None:
            # Cached archive bytes are NOT evidence about the live source.
            # Check the entire retained physical and staged inventory before
            # reusing any verified archive for another album claim.
            _check_retained_source(source)
            self.validate(source, previous)
            return previous
        result = mixie_shared_originals.ensure_archive(_root(), source)
        # ensure_archive already performed full SHA256/ZIP verification and
        # published its matching durable journal before returning.
        verified_descriptor, key, signatures = self._current(source, result)
        if verified_descriptor != descriptor:
            raise RuntimeError('Shared-originals descriptor changed during publication.')
        self._proofs[key] = signatures
        self._published[identity] = result
        return result


FINALIZATION_ERRORS = (ValueError, OSError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile)


def _fingerprint(service, issue, manifest):
    return service.completion_fingerprint(issue['import_name'], service._source_rows(issue, manifest), manifest)


def _verified_entry(service, issue, records):
    manifest = service.completion_source_manifest(issue)
    fingerprint = _fingerprint(service, issue, manifest)
    entry = records.get(fingerprint)
    if isinstance(entry, dict):
        try:
            # This manifest was freshly inventoried above. The library-only
            # validator checks its exact mapping and live outputs without a
            # second adjacent scan of every staged sibling.
            service._validate_library_only_entry(entry, issue, manifest)
            return fingerprint, entry
        except FINALIZATION_ERRORS as error:
            raise RuntimeError(f'Import receipt is stale; source/library output no longer verifies: {error}') from error
    if any(row['import_name'] == issue['import_name'] and row['tracks'] == sorted(issue['tracks'])
           for row in _state()['finalized'].values()):
        raise RuntimeError('Finalized album receipt is stale; source or output verification changed.')
    return None


def _check_shared_original_pins(state, source, batch=None):
    """A new album cannot rebuild an object already pinned by another receipt."""
    descriptor = mixie_shared_originals.delivery_descriptor(source)
    rows = list(state['finalized'].values())
    for transaction in state['completed'].values():
        rows.extend(transaction.get('albums') or [transaction])
    for row in rows:
        originals = row.get('originals')
        if isinstance(originals, dict) and set(originals) == {'path', 'file_sha256', 'entries'}:
            continue  # Existing per-album originals remain independently protected.
        for previous in row.get('sources', ()):
            if (previous.get('source_kind') != source['source_kind']
                    or previous.get('stage_hashes') != source['stage_hashes']):
                continue
            if mixie_shared_originals.delivery_descriptor(previous) != descriptor:
                continue
            if (not isinstance(originals, dict) or originals.get('format') != SHARED_ORIGINALS_FORMAT
                    or not isinstance(originals.get('deliveries'), dict)):
                raise RuntimeError('Published shared-original pin is damaged; owner restoration required.')
            reference = originals['deliveries'].get(previous['name'])
            if not isinstance(reference, dict) or 'archive' not in reference:
                raise RuntimeError('Published shared-original reference is missing; owner restoration required.')
            if batch is None:
                mixie_shared_originals.validate_archive(_root(), previous, reference['archive'])
            else:
                batch.validate(previous, reference['archive'])


def _archive_album(service, issue, fingerprint, entry, sources, claims, *, batch=None):
    """Publish a durable album receipt; this never authorizes physical cleanup.

    Complete physical deliveries are stored once in the shared content store.
    Album receipts pin those objects and exact audio ownership independently;
    unresolved siblings never require partitioning or mutating provenance.
    """
    if batch is None:
        batch = _SharedOriginalBatchProof()
    sources = sorted(sources, key=lambda source: (source['source_name'], source['name']))
    identity = {'fingerprint': fingerprint, 'import_name': issue['import_name'],
                'tracks': sorted(issue['tracks']), 'import_receipt': deepcopy(entry),
                'sources': deepcopy(sources), 'source_claims': deepcopy(claims),
                'source_cleanup': 'retained_until_whole_source_cleanup'}
    state = _state()
    if state['active'] is not None:
        raise RuntimeError('Previous cleanup transaction has not recovered.')
    previous = state['finalized'].get(fingerprint)
    if previous is not None:
        if any(previous.get(key) != value for key, value in identity.items()):
            raise RuntimeError('Finalized album receipt is stale; review required.')
        _validate_archives(previous, batch=batch)
        for source in sources:
            _check_retained_source(source)
        return previous, False
    artist, album_name, library_folder = _relative_output(entry)
    library_files, library_desc = _archive_entries_library(library_folder)
    for output, digest in entry['receipt'].items():
        relative = Path(output).relative_to(library_folder).as_posix()
        if library_files.get(relative, (None, None))[1] != digest:
            raise RuntimeError('Album archive candidates differ from verified import receipt.')
    originals = {'format': SHARED_ORIGINALS_FORMAT, 'deliveries': {}}
    for source in sources:
        name = source['name']
        _check_source_record(source)
        _check_shared_original_pins(state, source, batch=batch)
        originals['deliveries'][name] = {
            'archive': batch.ensure(source),
            'claimed_audio_sha256': {relative: source['stage_hashes'][relative]
                for relative in sorted(_claimed_audio(name, claims[name], source['stage_hashes']))}}
    library_path, library_sha, library_manifest = _write_zip(
        'Library', artist, album_name, library_files, library_desc)
    row = {**identity,
           'originals': originals,
           'library': {'path': library_path, 'file_sha256': library_sha, 'entries': library_manifest},
           'library_root': str(library_folder),
           'library_hashes': {name: digest for name, (_path, digest) in library_files.items()},
           'receipt': dict(entry['receipt'])}
    _validate_archives(row, batch=batch)
    for source in sources:
        _check_retained_source(source)  # Re-enumerate, catching added/missing sibling files too.
    if not service.is_library_import_verified(issue, {fingerprint: entry}):
        raise RuntimeError('Verified import changed during finalization.')
    state['finalized'][fingerprint] = row
    _write(state)  # Archive completion is durable even if later source cleanup fails.
    return row, True


def _archive_descriptor(record, kind):
    """Bind archive metadata to the already independently checked ZIP inventory."""
    if not isinstance(record, dict) or not isinstance(record.get('entries'), dict):
        raise RuntimeError('Invalid historical archive record.')
    with zipfile.ZipFile(record['path']) as saved:
        raw = saved.read(MANIFEST_NAME)
    descriptor = json.loads(raw)
    entries = {name: digest for name, digest in record['entries'].items() if name != MANIFEST_NAME}
    if (record['entries'].get(MANIFEST_NAME) != hashlib.sha256(raw).hexdigest()
            or not isinstance(descriptor, dict) or descriptor.get('version') != 1
            or descriptor.get('type') != kind or descriptor.get('sha256') != entries or not entries):
        raise RuntimeError('Historical archive manifest differs from its pinned inventory.')
    return descriptor


def _originals_coverage(rows, sources):
    """Verify directory partitions or untouched containers cover every source."""
    labels = {source['name']: (f'{index + 1:02d}-' + hashlib.sha256(source['name'].encode()).hexdigest()[:10])
              for index, source in enumerate(sorted(sources, key=lambda source: (source['source_name'], source['name'])))}
    by_name = {source['name']: source for source in sources}
    covered, partitions = {name: {} for name in by_name}, []
    for row in rows:
        descriptor = _archive_descriptor(row['originals'], 'untouched_originals')
        deliveries = descriptor.get('deliveries')
        if not isinstance(deliveries, list) or not deliveries:
            raise RuntimeError('Historical Originals ZIP has no delivery identity.')
        expected, parts = {}, {}
        for delivery in deliveries:
            if not isinstance(delivery, dict) or not sort._safe_name(delivery.get('import_name')):
                raise RuntimeError('Invalid historical delivery identity.')
            name = delivery['import_name']
            source = by_name.get(name)
            payload = delivery.get('payload_sha256')
            if (source is None or name in parts or delivery.get('label') != labels[name]
                    or delivery.get('original_name') != source['source_name']
                    or delivery.get('source_kind') != source['source_kind']
                    or not isinstance(payload, dict) or not payload
                    or any(source['stage_hashes'].get(key) != digest for key, digest in payload.items())):
                raise RuntimeError('Historical Originals ZIP differs from committed delivery provenance.')
            prefix = 'deliveries/' + labels[name] + '/'
            if source['source_kind'] == 'directory':
                original = payload
            else:
                if payload != source['stage_hashes']:
                    raise RuntimeError('Historical container payload provenance is incomplete.')
                original = source['source_hashes']
            expected.update({prefix + relative: digest for relative, digest in original.items()})
            covered[name].update(original)
            parts[name] = set(payload)
        if (set(parts) != set(by_name)
                or descriptor['sha256'] != expected):
            raise RuntimeError('Historical Originals ZIP has unaccounted or missing source bytes.')
        partitions.append((parts, descriptor))
    if any(covered[source['name']] != source['source_hashes'] for source in sources):
        raise RuntimeError('Historical Originals ZIPs omit part of the physical delivery.')
    return partitions


def _legacy_album_finalized(service, issue, state):
    """Read-only recognition of the two original v10j completed journal shapes.

    Missing payloads alone prove nothing. Require the old transaction identity,
    surviving committed provenance, exact independent import mappings, both ZIP
    inventories and current output bytes. Never migrate or repair history here.
    """
    if state['active'] is not None:
        return False
    records = service._verified_imports()
    for identifier, transaction in state['completed'].items():
        try:
            if not isinstance(transaction, dict) or not sort._is_sha256(identifier) or transaction.get('id') != identifier:
                continue
            sources = transaction['sources']
            if not isinstance(sources, list) or not sources:
                continue
            if set(transaction) == {'id', 'sources', 'albums'}:
                rows = transaction['albums']
                if not isinstance(rows, list) or len(rows) < 2 or len(sources) != 1:
                    continue
                identity = {'source': sources[0], 'albums': rows}
            elif set(transaction) == {'id', 'sources', 'originals', 'library', 'receipt'}:
                rows = [{key: transaction[key] for key in ('originals', 'library', 'receipt')}]
                identity = {'sources': sources, 'receipt': transaction['receipt'],
                            'original': transaction['originals']['file_sha256'],
                            'library': transaction['library']['file_sha256']}
            else:
                continue  # A damaged modern receipt cannot downgrade to legacy authority.
            if (any(not isinstance(row, dict) or set(row) != {'originals', 'library', 'receipt'} for row in rows)
                    or hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest() != identifier):
                continue
            for source in sources:
                _check_source_record(source)
                if any(os.path.lexists(source[field]) for field in ('source', 'stage')):
                    raise RuntimeError('Historical source has reappeared; do not assume it was purged.')
            by_name = {source['name']: source for source in sources}
            if len(by_name) != len(sources) or issue['import_name'] not in by_name:
                continue
            held_root = sort._safe_directory(_root() / QUARANTINE_NAME, missing_ok=True)
            if os.path.lexists(held_root / identifier):
                continue
            _validate_archives(transaction)
            partitions = _originals_coverage(rows, sources)
            claimed, matched = defaultdict(set), False
            for row, (parts, original_desc) in zip(rows, partitions):
                candidates = []
                for fingerprint, entry in records.items():
                    if not isinstance(entry, dict) or entry.get('receipt') != row['receipt']:
                        continue
                    manifest = entry.get('source_manifest')
                    if not isinstance(manifest, dict):
                        continue
                    source = by_name.get(manifest.get('import_name'))
                    if source is None or manifest != {'import_name': source['name'],
                            'payload_hashes': source['stage_hashes'], 'provenance': source['provenance']}:
                        continue
                    owned = {'import_name': source['name'], 'tracks': entry.get('tracks')}
                    if _fingerprint(service, owned, manifest) != fingerprint:
                        continue
                    service._validate_library_only_entry(entry, owned, manifest)
                    candidates.append((fingerprint, entry, owned))
                if len(candidates) != 1:
                    raise RuntimeError('Historical album mapping is missing or ambiguous.')
                fingerprint, entry, owned = candidates[0]
                name = owned['import_name']
                leaves = _claimed_audio(name, owned['tracks'], by_name[name]['stage_hashes'])
                original_audio = {relative for relative in parts[name]
                                  if Path(relative).suffix.casefold() in constants.music_file_types}
                if (not leaves <= original_audio or claimed[name] & leaves
                        or (by_name[name]['source_kind'] == 'directory' and leaves != original_audio)):
                    raise RuntimeError('Historical album source claims differ from its Originals ZIP.')
                claimed[name].update(leaves)
                artist, album, library_root = _relative_output(entry)
                library_desc = _archive_descriptor(row['library'], 'corrected_library')
                if (any(descriptor.get('artist') != artist or descriptor.get('album') != album
                        for descriptor in (original_desc, library_desc))
                        or library_desc['sha256'] != sort.file_manifest(library_root)):
                    raise RuntimeError('Historical Library ZIP differs from the current album inventory.')
                if name == issue['import_name'] and sorted(owned['tracks']) == sorted(issue['tracks']):
                    matched = _fingerprint(service, issue, entry['source_manifest']) == fingerprint
            # Shared old transactions had disjoint claims covering the entire
            # unit. Single-row duplicate transactions pin their owner's full album.
            if matched and all(claimed[name] == {relative for relative in by_name[name]['stage_hashes']
                    if Path(relative).suffix.casefold() in constants.music_file_types} for name in claimed):
                return True
        except FINALIZATION_ERRORS:
            continue
    return False


def is_album_finalized(service, issue):
    """True only for a live, validated archive pair and its exact source receipt.

    Retained shared sources count as finalized, not as physically purged. A stale
    archive, output inventory, source, or provenance record invalidates this proof.
    Callers may use this to remove archive-pending work without rescanning audio.
    """
    try:
        state = _state()
        for fingerprint, row in state['finalized'].items():
            if row['import_name'] != issue['import_name'] or row['tracks'] != sorted(issue['tracks']):
                continue
            entry = row['import_receipt']
            manifest = entry['source_manifest']
            if (row['receipt'] != entry['receipt'] or not row['sources']
                    or row['source_claims'].get(issue['import_name']) != sorted(issue['tracks'])
                    or set(row['source_claims']) != {source['name'] for source in row['sources']}):
                return False
            owner = next(source for source in row['sources'] if source['name'] == issue['import_name'])
            if owner['provenance'] != manifest['provenance'] or owner['stage_hashes'] != manifest['payload_hashes']:
                return False
            if os.path.lexists(Path(sort.staging_folder) / issue['import_name']):
                if service.completion_source_manifest(issue) != manifest:
                    return False
            if _fingerprint(service, issue, manifest) != fingerprint:
                return False
            service._validate_library_only_entry(entry, issue, manifest)
            _validate_archives(row)
            for source in row['sources']:
                _check_source_record(source)
            if all(os.path.lexists(source[field]) for source in row['sources'] for field in ('source', 'stage')):
                for source in row['sources']:
                    _check_retained_source(source)
                return True
            # After purge, the independent durable cleanup receipt must contain
            # this exact album record. A missing source alone is never success.
            return (all(not os.path.lexists(source[field]) for source in row['sources']
                        for field in ('source', 'stage'))
                    and any(row in transaction.get('albums', [])
                            and transaction['sources'] == row['sources']
                            for transaction in state['completed'].values()))
        return _legacy_album_finalized(service, issue, state)
    except FINALIZATION_ERRORS:
        return False


def _recover_verified_albums(service, albums, records):
    """Reconstruct receipt-owned work from staging, without ingestion or providers."""
    result = dict(albums)
    existing = [set(service._album_issue(album)['tracks']) for album in albums.values()]
    for fingerprint, entry in records.items():
        if not isinstance(entry, dict):
            continue
        manifest = entry.get('source_manifest')
        if not isinstance(manifest, dict) or not sort._is_sha256(fingerprint):
            continue
        name, tracks = manifest.get('import_name'), entry.get('tracks')
        try:
            if not sort._safe_name(name):
                continue
            _claimed_audio(name, tracks, manifest['payload_hashes'])
            if not (Path(sort.staging_folder) / name).is_dir() or any(set(tracks) & paths for paths in existing):
                continue
            key = (name, 'verified-recovery:' + fingerprint)
            result[key] = {'album': entry['album'],
                           'tracks': [{'path': Path(sort.staging_folder) / relative} for relative in tracks]}
            existing.append(set(tracks))
        except FINALIZATION_ERRORS:
            continue
    return result


def _cleanup_sources(sources, rows, *, allow_source_cleanup):
    if not allow_source_cleanup:
        return
    sources = sorted(sources, key=lambda source: (source['source_name'], source['name']))
    state = _state()
    if state['active'] is not None:
        raise RuntimeError('Previous cleanup transaction has not recovered.')
    identity = hashlib.sha256(json.dumps({'sources': sources, 'albums': rows},
                    sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    transaction = {'id': identity, 'phase': 'prepared', 'sources': sources, 'albums': rows}
    _check_cleanup_claims(transaction)
    _validate_archives(transaction)
    for source in sources:
        _check_retained_source(source)
    state['active'] = transaction
    _write(state)  # Durable journal BEFORE ANY source or staging rename.
    _resume_transaction(state, allow_source_cleanup=True)


def _finish_shared_source(service, keys, albums, records, *, allow_source_cleanup, batch=None, progress=None):
    issues = [service._album_issue(albums[key]) for key in keys]
    name = issues[0]['import_name']
    if any(issue['import_name'] != name for issue in issues):
        raise RuntimeError('Shared source has inconsistent import identities.')
    union = [relative for issue in issues for relative in issue['tracks']]
    if len(set(union)) != len(union):
        raise RuntimeError('Two albums claim the same source track.')
    verified, errors = [], []
    for issue in issues:
        try:
            verified.append(_verified_entry(service, issue, records))
        except FINALIZATION_ERRORS as error:
            verified.append(None)
            errors.append(f"{issue['album']}: {error}")
    if not any(verified):
        return 0, errors
    source = _complete_source(name, union, allow_partial=True)
    output_roots = [_relative_output(pair[1])[2] for pair in verified if pair is not None]
    if len(set(output_roots)) != len(output_roots):
        raise RuntimeError('Two album imports share one library destination.')
    rows, count = [], 0
    for position, (issue, pair) in enumerate(zip(issues, verified), 1):
        if pair is None:
            continue
        if progress is not None:
            progress(f"    Verifying album {position}/{len(issues)}: {issue['album']}")
        try:
            fingerprint, entry = pair
            row, created = _archive_album(service, issue, fingerprint, entry, [source],
                                          {name: sorted(issue['tracks'])}, batch=batch)
            rows.append(row)
            count += int(created)
        except FINALIZATION_ERRORS as error:
            errors.append(f"{issue['album']}: {error}")
    audio = {relative for relative in source['stage_hashes']
             if Path(relative).suffix.casefold() in constants.music_file_types}
    claimed = _claimed_audio(name, union, source['stage_hashes'])
    if claimed != audio:
        errors.append(f'{name}: Shared source has unaccounted audio; full original and staging retained.')
    elif len(rows) != len(issues):
        errors.append(f'{name}: Shared source still has unresolved album(s); '
                      'verified albums archived independently, full original and staging retained.')
    else:
        try:
            _cleanup_sources([source], rows, allow_source_cleanup=allow_source_cleanup)
        except FINALIZATION_ERRORS as error:
            errors.append(f'{name}: {error}')
            # Archive receipts remain durable, but do not report a failed cleanup
            # transaction as newly successful to legacy callers.
            return 0, errors
    return count, errors


def finish_verified_albums(service, albums, index=None, *, allow_source_cleanup=False, progress=None):
    """Archive verified albums independently; physical cleanup is opt-in.

    Return (newly finalized album groups, deferred archive/cleanup explanations).
    A finalized group can intentionally retain its complete shared physical source.
    Repeated calls validate durable per-album receipts without recreating archives.
    Existing .verified_imports entries can recover work even when albums is empty.
    All callers must hold the existing ingest_session lock.
    """
    resume_cleanup(allow_source_cleanup=allow_source_cleanup)
    if _state()['active'] is not None:
        return 0, ['Protected cleanup is pending; explicit allow_source_cleanup=True is required to resume.']
    records = service._verified_imports()
    if not records:
        return 0, []
    albums = _recover_verified_albums(service, albums, records)
    successes, errors = 0, []
    batch = _SharedOriginalBatchProof()
    by_import = defaultdict(list)
    for key in albums:
        by_import[service._album_issue(albums[key])['import_name']].append(key)
    # Preserve the existing exact-duplicate group policy for independent whole
    # deliveries. Shared physical deliveries require their own per-album receipts,
    # never a representative's receipt for an unresolved sibling.
    duplicate_groups = [members for members in _group_members(albums, index)
                        if len(members) > 1 and all(len(by_import[key[0]]) == 1 for key in members)]
    duplicate_keys = {key for members in duplicate_groups for key in members}
    pending_sources = [(name, keys) for name, keys in sorted(by_import.items())
                       if not any(key in duplicate_keys for key in keys)]
    for position, (import_name, keys) in enumerate(pending_sources, 1):
        if progress is not None:
            progress(f"Delivery {position}/{len(pending_sources)}: {import_name}")
        try:
            count, problems = _finish_shared_source(service, keys, albums, records,
                                                    allow_source_cleanup=allow_source_cleanup,
                                                    batch=batch, progress=progress)
            successes += count
            errors.extend(problems)
        except FINALIZATION_ERRORS as error:
            errors.append(f'{import_name}: {error}')
        if _state()['active'] is not None:
            return successes, errors
    for members in duplicate_groups:
        try:
            selected = None
            for key in members:
                issue = service._album_issue(albums[key])
                pair = _verified_entry(service, issue, records)
                if pair is not None:
                    selected = issue, pair
                    break
            if selected is None:
                continue
            issue, (fingerprint, entry) = selected
            sources, claims = [], {}
            for key in members:
                member_issue = service._album_issue(albums[key])
                name = member_issue['import_name']
                sources.append(_complete_source(name, member_issue['tracks']))
                claims[name] = sorted(member_issue['tracks'])
                if index is not None and index.source(key) is not None:
                    index.validated_mapping(key)
            row, created = _archive_album(service, issue, fingerprint, entry, sources, claims, batch=batch)
            _cleanup_sources(sources, [row], allow_source_cleanup=allow_source_cleanup)
            successes += int(created)
        except FINALIZATION_ERRORS as error:
            errors.append(f'{albums[members[0]]["album"]}: {error}')
            if _state()['active'] is not None:
                break
    return successes, errors
