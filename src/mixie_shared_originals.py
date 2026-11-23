"""Content-addressed physical deliveries; caller holds MIXIE's ingest/root lock.

Only ensure_archive writes, and only under backup_root. Published or sealed pins
are never repaired automatically. Journals remain after publication; an owned,
unpinned interrupted build may reuse its single partial in place. Provenance and
album claims belong to the caller, not to this content identity.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import shutil
import stat
import zipfile
from pathlib import Path

import constants
import sort

MANIFEST_NAME = 'mixie-archive-manifest.json'
_SOURCE_FIELDS = {'name', 'source_name', 'source_kind', 'source_hashes',
                  'stage_hashes', 'source', 'stage', 'provenance'}
_STAMP = (1980, 1, 1, 0, 0, 0)
_MODE = (stat.S_IFREG | 0o644) << 16


class _Corrupt(RuntimeError):
    """Invalid bytes, as distinct from an unsafe filesystem object or I/O error."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def _digest(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


def _path(value):
    try:
        text = os.fspath(value)
        path = Path(text)
    except (TypeError, ValueError) as error:
        raise RuntimeError('Invalid shared-originals path.') from error
    if (not path.is_absolute() or path.anchor != '/' or str(path) != text
            or '..' in path.parts or '\x00' in text):
        raise RuntimeError('Noncanonical absolute shared-originals path.')
    return path


def _directory(path, *, create=False, missing_ok=False):
    # Inspect every existing ancestor before mkdir, including dangling symlinks.
    path = _path(path)
    for parent in reversed((path, *path.parents)):
        if not os.path.lexists(parent):
            if create:
                parent.mkdir()
                sort._sync_directory(parent.parent)
            elif missing_ok:
                continue
            else:
                raise RuntimeError(f'Missing shared-originals directory: {parent}')
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise RuntimeError(f'Unsafe shared-originals directory: {parent}')
    return path


def _regular(path, peer=None):
    _directory(path.parent)
    try:
        attributes = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f'Missing pinned shared original; owner restoration required: {path}') from error
    if not stat.S_ISREG(attributes.st_mode):
        raise RuntimeError(f'Unsafe shared-originals file: {path}')
    if attributes.st_nlink != 1:
        other = peer.lstat() if peer is not None and os.path.lexists(peer) else None
        if (attributes.st_nlink != 2 or other is None or not stat.S_ISREG(other.st_mode)
                or other.st_nlink != 2 or (attributes.st_dev, attributes.st_ino)
                != (other.st_dev, other.st_ino)):
            raise RuntimeError(f'Unsafe hardlinked shared-originals file: {path}')
    return attributes


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or ntpath.splitdrive(value)[0] or value.startswith('/')
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise RuntimeError('Noncanonical shared-originals member path.')
    return value


def _hashes(value):
    if not isinstance(value, dict) or not value:
        raise RuntimeError('Missing shared-originals inventory.')
    for name, digest in value.items():
        _relative(name)
        if not sort._is_sha256(digest):
            raise RuntimeError('Invalid shared-originals SHA256.')
        parts = name.split('/')
        if any('/'.join(parts[:index]) in value for index in range(1, len(parts))):
            raise RuntimeError('Shared-originals file/directory conflict.')
    return dict(sorted(value.items()))


def delivery_descriptor(source):
    """Canonical v1 content identity, independent of directory/ZIP outer names."""
    if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
        raise RuntimeError('Invalid shared-originals source fields.')
    for key in ('name', 'source_name'):
        if not sort._safe_name(source[key]):
            raise RuntimeError('Invalid shared-originals delivery name.')
    original, stage = _path(source['source']), _path(source['stage'])
    if (original.parent != _path(sort.inbox_folder) or original.name != source['source_name']
            or stage.parent != _path(sort.staging_folder) or stage.name != source['name']
            or not isinstance(source['provenance'], dict)):
        raise RuntimeError('Shared-originals source/stage boundary mismatch.')
    _directory(original.parent, missing_ok=True)
    _directory(stage, missing_ok=True)
    payload, physical = _hashes(source['stage_hashes']), _hashes(source['source_hashes'])
    kind = source['source_kind']
    if kind == 'directory':
        if physical != payload:
            raise RuntimeError('Directory source and staged inventory differ.')
        entries = {f'files/{name}': digest for name, digest in physical.items()}
    elif kind in ('zip', 'file'):
        if set(physical) != {source['source_name']}:
            raise RuntimeError('Container source inventory must pin the original file.')
        if kind == 'zip' and original.suffix.lower() != '.zip':
            raise RuntimeError('ZIP source kind requires an original ZIP.')
        if kind == 'file' and (physical != payload or original.suffix.lower() not in constants.music_file_types):
            raise RuntimeError('Single-file source must match the staged audio inventory.')
        entries = {'original.zip' if kind == 'zip' else 'original': physical[source['source_name']]}
    else:
        raise RuntimeError('Unknown shared-originals source kind.')
    return {'version': 1, 'type': 'shared_original_delivery', 'source_kind': kind,
            'payload_sha256': payload, 'sha256': entries}


def _layout(backup_root, descriptor):
    root = _path(backup_root)
    _directory(root, missing_ok=True)
    if any(root.is_relative_to(Path(folder).resolve()) for folder in
           (sort.inbox_folder, sort.staging_folder, sort.music_library_folder)):
        raise RuntimeError('Archive storage cannot be inside Inbox, staging, or Music Library.')
    shared = root / 'Originals' / 'Shared'
    _directory(shared / 'deliveries', missing_ok=True)
    _directory(shared / 'sha256', missing_ok=True)
    identity = hashlib.sha256(_json(descriptor)).hexdigest()
    journal = shared / 'deliveries' / f'{identity}.json'
    partial = journal.with_suffix('.partial')
    temporary = journal.with_suffix('.json.tmp')
    if os.path.lexists(temporary):
        _regular(temporary)
    return shared, identity, journal, partial


def _entries(descriptor):
    return {**descriptor['sha256'], MANIFEST_NAME: hashlib.sha256(_json(descriptor)).hexdigest()}


def _record(shared, identity, descriptor, digest):
    return {'version': 1, 'object_id': identity,
            'path': str(shared / 'sha256' / digest[:2] / f'{digest}.zip'),
            'file_sha256': digest, 'entries': _entries(descriptor)}


def _check_record(shared, identity, descriptor, record):
    if (not isinstance(record, dict) or type(record.get('version')) is not int
            or not sort._is_sha256(record.get('file_sha256'))
            or record != _record(shared, identity, descriptor, record['file_sha256'])):
        raise RuntimeError('Shared-originals record identity/path/inventory mismatch.')
    return Path(record['path'])


def _read_journal(journal, shared, identity, descriptor):
    if not os.path.lexists(journal):
        return None
    _regular(journal)
    try:
        encoded = journal.read_bytes()
        row = json.loads(encoded)
        if journal.suffix == '.tmp' and encoded != _json(row):
            raise ValueError('Noncanonical journal temporary.')
    except (ValueError, UnicodeError) as error:
        raise RuntimeError('Corrupt shared-originals journal; owner restoration required.') from error
    if (not isinstance(row, dict) or set(row) != {'version', 'object_id', 'descriptor', 'state', 'archive'}
            or type(row['version']) is not int or row['version'] != 1 or row['object_id'] != identity
            or _json(row['descriptor']) != _json(descriptor)
            or row['state'] not in ('building', 'sealed', 'published')):
        raise RuntimeError('Conflicting shared-originals journal; owner restoration required.')
    if row['state'] == 'building':
        if row['archive'] is not None:
            raise RuntimeError('Unexpected pin in shared-originals build journal.')
    else:
        try:
            _check_record(shared, identity, descriptor, row['archive'])
        except RuntimeError as error:
            raise RuntimeError('Conflicting journal pin; owner restoration required.') from error
    return row


def _save_journal(path, row):
    temporary = path.with_suffix('.json.tmp')
    if not os.path.lexists(path) and os.path.lexists(temporary):
        raise RuntimeError('Unrecovered shared-originals journal temporary; owner restoration required.')
    if os.path.lexists(path):
        _regular(path)
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if os.path.lexists(temporary):
        _regular(temporary)
    else:
        flags |= os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(temporary, flags, 0o600), 'wb') as stream:
        before = _regular(temporary)
        if sort._file_identity(before) != sort._file_identity(os.fstat(stream.fileno())):
            raise RuntimeError('Shared-originals journal temporary changed.')
        stream.truncate(0)
        stream.write(_json(row))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sort._sync_directory(path.parent)


def _promote_journal(journal, row):
    temporary = journal.with_suffix('.json.tmp')
    before = _regular(temporary)
    with os.fdopen(os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        if (sort._file_identity(os.fstat(stream.fileno())) != sort._file_identity(before)
                or stream.read() != _json(row)
                or sort._file_identity(_regular(temporary)) != sort._file_identity(before)):
            raise RuntimeError('Journal temporary changed; owner restoration required.')
        os.fsync(stream.fileno())
    if os.path.lexists(journal):
        raise RuntimeError('Journal appeared during recovery; owner restoration required.')
    os.replace(temporary, journal)
    sort._sync_directory(journal.parent)


def _preflight_new_build(shared, descriptor):
    """Read CAS indexes/manifests only; never adopt an object or cache its hash."""
    objects = shared / 'sha256'
    if not objects.exists():
        return
    expected = _json(descriptor)
    for shard in sorted(objects.iterdir()):
        try:
            if len(shard.name) != 2 or any(c not in '0123456789abcdef' for c in shard.name):
                raise ValueError('Noncanonical CAS shard.')
            _directory(shard)
            for path in sorted(shard.iterdir()):
                if path.suffix != '.zip' or not sort._is_sha256(path.stem) or not path.stem.startswith(shard.name):
                    raise ValueError('Noncanonical CAS object.')
                before = _regular(path)
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as raw:
                    if sort._file_identity(os.fstat(raw.fileno())) != sort._file_identity(before):
                        raise RuntimeError('CAS object changed before metadata inspection.')
                    with zipfile.ZipFile(raw) as saved:
                        members = saved.infolist()
                        manifests = [m for m in members if m.filename == MANIFEST_NAME]
                        if len(manifests) != 1 or any(m.compress_type != zipfile.ZIP_STORED or m.flag_bits & 1
                                                     for m in members):
                            raise ValueError('Missing or unsafe CAS manifest.')
                        encoded = saved.read(manifests[0])
                        meta = json.loads(encoded)
                        if (not isinstance(meta, dict) or set(meta) != set(descriptor)
                                or type(meta['version']) is not int or meta['version'] != 1
                                or meta['type'] != 'shared_original_delivery' or encoded != _json(meta)):
                            raise ValueError('Unrecognized CAS delivery manifest.')
                        payload, physical = _hashes(meta['payload_sha256']), _hashes(meta['sha256'])
                        kind = meta['source_kind']
                        if kind == 'directory':
                            valid = physical == {f'files/{name}': digest for name, digest in payload.items()}
                        else:
                            key = 'original.zip' if kind == 'zip' else 'original'
                            valid = kind in ('zip', 'file') and set(physical) == {key}
                            if kind == 'file':
                                name = next(iter(payload))
                                valid = (valid and len(payload) == 1 and sort._safe_name(name)
                                         and Path(name).suffix.lower() in constants.music_file_types
                                         and list(payload.values()) == list(physical.values()))
                        if not valid or [m.filename for m in members] != sorted(_entries(meta)):
                            raise ValueError('Unbound CAS delivery inventory.')
                    if (sort._file_identity(os.fstat(raw.fileno())) != sort._file_identity(before)
                            or sort._file_identity(_regular(path)) != sort._file_identity(before)):
                        raise RuntimeError('CAS object changed during metadata inspection.')
                if encoded == expected:
                    raise RuntimeError('Unexplained published shared original; owner restoration required.')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, EOFError, zipfile.BadZipFile) as error:
            raise RuntimeError(f'Cannot bind CAS object metadata; owner restoration required: {shard}: {error}') from error


def _zip_hashes(saved):
    result = {}
    for member in sort._zip_members(saved, case_sensitive=True):
        if not member.is_dir():
            with saved.open(member) as stream:
                result[member.filename] = _digest(stream)
    return result


def _check_live(source, descriptor):
    path = _path(source['source'])
    _directory(path.parent)
    kind = descriptor['source_kind']
    if kind == 'directory':
        actual = sort.file_manifest(_directory(path))
    else:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError('Unsafe physical shared-originals source.')
        digest = sort.file_checksum(path).hex()
        if digest != source['source_hashes'][source['source_name']]:
            raise RuntimeError('Physical shared-originals source changed.')
        actual = sort.zip_manifest(path) if kind == 'zip' else {source['source_name']: digest}
        if sort.file_checksum(path).hex() != digest:
            raise RuntimeError('Physical shared-originals source changed during hashing.')
    if actual != descriptor['payload_sha256']:
        raise RuntimeError('Physical shared-originals inventory changed.')


def _verify(path, descriptor, digest=None, peer=None, *, durable=False):
    before = _regular(path, peer)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as raw:
        if sort._file_identity(os.fstat(raw.fileno())) != sort._file_identity(before):
            raise RuntimeError('Shared-originals file changed before verification.')
        actual = _digest(raw)
        if digest is not None and actual != digest:
            raise _Corrupt(f'Corrupt pinned shared original; owner restoration required: {path}')
        raw.seek(0)
        try:
            with zipfile.ZipFile(raw) as saved:
                members = saved.infolist()
                if (saved.comment or [m.filename for m in members] != sorted(_entries(descriptor))
                        or any(m.compress_type != zipfile.ZIP_STORED or m.date_time != _STAMP
                               or m.create_system != 3 or m.external_attr != _MODE for m in members)
                        or _zip_hashes(saved) != _entries(descriptor)
                        or saved.read(MANIFEST_NAME) != _json(descriptor)):
                    raise _Corrupt('Shared-originals manifest or full inventory mismatch.')
                if descriptor['source_kind'] == 'zip':
                    # Python 3.14 seeks stored/unencrypted members directly, without
                    # replaying prefixes. Seeking disables that stream's CRC check;
                    # the complete sequential member SHA256 above remains mandatory.
                    with saved.open('original.zip') as original, zipfile.ZipFile(original) as inner:
                        if _zip_hashes(inner) != descriptor['payload_sha256']:
                            raise _Corrupt('Original ZIP payload differs from pinned inventory.')
        except (zipfile.BadZipFile, ValueError, EOFError, KeyError, RuntimeError, NotImplementedError) as error:
            raise _Corrupt(f'Corrupt shared original; owner restoration required: {path}: {error}') from error
        if (sort._file_identity(os.fstat(raw.fileno())) != sort._file_identity(before)
                or sort._file_identity(_regular(path, peer)) != sort._file_identity(before)):
            raise RuntimeError('Shared-originals file changed during verification.')
        if durable:
            os.fsync(raw.fileno())
            sort._sync_directory(path.parent)
    return actual


def _info(member):
    info = zipfile.ZipInfo(member, _STAMP)
    info.create_system, info.external_attr = 3, _MODE
    info.compress_type = zipfile.ZIP_STORED
    return info


def _write_partial(partial, source, descriptor):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if os.path.lexists(partial):
        _regular(partial)  # Never truncate an unsafe or externally linked file.
    else:
        flags |= os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(partial, flags, 0o600), 'w+b') as raw:
        before = _regular(partial)
        if sort._file_identity(before) != sort._file_identity(os.fstat(raw.fileno())):
            raise RuntimeError('Shared-originals partial changed before writing.')
        raw.truncate(0)
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_STORED) as saved:
            for member in sorted(_entries(descriptor)):
                if member == MANIFEST_NAME:
                    saved.writestr(_info(member), _json(descriptor))
                    continue
                path = Path(source['source'])
                if descriptor['source_kind'] == 'directory':
                    path /= member[len('files/'):]
                _directory(path.parent)
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as original:
                    if not stat.S_ISREG(os.fstat(original.fileno()).st_mode):
                        raise RuntimeError('Unsafe shared-originals input file.')
                    with saved.open(_info(member), 'w', force_zip64=True) as target:
                        shutil.copyfileobj(original, target, 1024 * 1024)
        raw.flush()
        os.fsync(raw.fileno())
    sort._sync_directory(partial.parent)


def ensure_archive(backup_root, source):
    """Verify/reuse or durably publish one delivery; never modify source/stage."""
    descriptor = delivery_descriptor(source)
    shared, identity, journal, partial = _layout(backup_root, descriptor)
    row = _read_journal(journal, shared, identity, descriptor)
    recovered = row is None and os.path.lexists(journal.with_suffix('.json.tmp'))
    if recovered:
        row = _read_journal(journal.with_suffix('.json.tmp'), shared, identity, descriptor)
        if row is None:
            raise RuntimeError('Journal temporary disappeared; owner restoration required.')
    if row is None and os.path.lexists(partial):
        raise RuntimeError('Unknown shared-originals partial without durable intent.')
    if row is None or (recovered and row['state'] == 'building'):
        _preflight_new_build(shared, descriptor)
    _check_live(source, descriptor)
    if recovered and row is not None and row['state'] == 'building':
        _promote_journal(journal, row)  # Recover durable intent before touching its partial.
        recovered = False
    _directory(journal.parent, create=True)
    if row is None:
        row = {'version': 1, 'object_id': identity, 'descriptor': descriptor,
               'state': 'building', 'archive': None}
        _save_journal(journal, row)  # Intent must survive before any payload creation.
    if row['state'] == 'building':
        if os.path.lexists(partial):
            _regular(partial)
            try:
                digest = _verify(partial, descriptor, durable=True)
            except _Corrupt:
                _check_live(source, descriptor)
                _write_partial(partial, source, descriptor)
                digest = _verify(partial, descriptor, durable=True)
        else:
            _write_partial(partial, source, descriptor)
            digest = _verify(partial, descriptor, durable=True)
        _check_live(source, descriptor)
        record = _record(shared, identity, descriptor, digest)
        if os.path.lexists(record['path']):
            raise RuntimeError('Unexplained published shared original; restore its journal, never overwrite.')
        row.update(state='sealed', archive=record)
        _save_journal(journal, row)
    record = row['archive']
    blob = _check_record(shared, identity, descriptor, record)
    _directory(blob.parent, missing_ok=True)
    if not os.path.lexists(blob):
        if row['state'] == 'published':
            raise RuntimeError('Missing published shared original; owner restoration required.')
        _verify(partial, descriptor, record['file_sha256'])  # A sealed pin is not rebuild permission.
        _check_live(source, descriptor)
        _directory(blob.parent, create=True)
        os.link(partial, blob, follow_symlinks=False)  # No-clobber; retain evidence on any failure.
        sort._sync_directory(blob.parent)
    _verify(blob, descriptor, record['file_sha256'], peer=partial)
    sort._sync_directory(blob.parent)  # A previous link may have preceded a crash before fsync.
    if os.path.lexists(partial):
        _verify(partial, descriptor, record['file_sha256'], peer=blob)
    _check_live(source, descriptor)
    if recovered:
        _promote_journal(journal, row)  # Keep temporary pins intact until full verification succeeds.
    if row['state'] != 'published':
        row['state'] = 'published'
        _save_journal(journal, row)
    if os.path.lexists(partial):
        _regular(partial, blob)
        partial.unlink()
        sort._sync_directory(partial.parent)
    _check_live(source, descriptor)
    return record


def validate_archive(backup_root, source, record):
    """Read-only verification against source pins, including after source purge."""
    descriptor = delivery_descriptor(source)
    shared, identity, journal, partial = _layout(backup_root, descriptor)
    blob = _check_record(shared, identity, descriptor, record)
    row = _read_journal(journal, shared, identity, descriptor)
    if row is None or row['state'] != 'published' or _json(row['archive']) != _json(record):
        raise RuntimeError('Shared original lacks its matching published journal; restore before proceeding.')
    _verify(blob, descriptor, record['file_sha256'], peer=partial)
    if os.path.lexists(partial):
        _verify(partial, descriptor, record['file_sha256'], peer=blob)
    return descriptor
