"""Receipt-aware, non-destructive reuse of the same *encoded audio* in the library.

Only independently verified automated plans may opt in.  This module never
uses acoustic fingerprints, names or metadata as proof of duplicate bytes.
An encoded stream comparison excludes artwork and tags while preserving the
actual audio packets; any uncertain comparison stays a collision requiring
review. All replaced library files receive a durable, checksum-verified backup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field

import archive
import constants
import sort
from mutagen import File


_ALLOWED_AUDIO = tuple(part.lower() for part in constants.music_file_types)


def _stream_digest(path):
    """Hash original encoded audio packets; do not decode, transcode or re-encode."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _ALLOWED_AUDIO:
        raise ValueError('Encoded audio comparison requires a regular supported audio file.')
    audio = File(path)
    if audio is None or getattr(audio, 'info', None) is None:
        raise ValueError('Unreadable audio stream.')
    info = audio.info
    signature = (type(audio).__name__, getattr(info, 'sample_rate', None),
                 getattr(info, 'channels', None))
    if not signature[1] or not signature[2]:
        raise ValueError('Incomplete audio stream format.')
    result = subprocess.run([
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror',
        '-protocol_whitelist', 'file,pipe', '-i', str(path.absolute()),
        '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'SHA256', '-'
    ], input=b'', stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
    if result.returncode or not re.fullmatch(rb'SHA256=[0-9a-fA-F]{64}\s*', result.stdout):
        raise ValueError('Encoded audio comparison could not be verified.')
    return signature, result.stdout.strip().split(b'=', 1)[1]



def _preserves_extra_tags(source, destination):
    """Never discard custom tags or non-front images during automatic repair.

    The canonical working copy already contains verified core fields, but a
    previously imported file may have user-added ReplayGain, lyrics or art.
    Those annotations are not ours to erase. Reuse the original when a full
    copy would lose or change them; a receipt-aware field merge is future work.
    """
    original, proposed = File(destination), File(source)
    if type(original) is not type(proposed):
        return False
    if original is None or proposed is None:
        return False
    managed = {'artist','albumartist','album','title','date','genre',
               'tracknumber','tracktotal','totaltracks','discnumber','disctotal','totaldiscs'}
    if original.__class__.__name__ == 'FLAC':
        if original.tags is not None:
            for key, value in original.tags.items():
                if key.casefold() not in managed and (
                        proposed.tags is None or proposed.tags.get(key) != value):
                    return False
        def other_pictures(item):
            return sorted((pic.type, pic.mime, hashlib.sha256(pic.data).hexdigest())
                          for pic in item.pictures if pic.type != 3)
        return other_pictures(original) == other_pictures(proposed)
    if original.__class__.__name__ == 'MP3':
        def custom_frames(item):
            result = []
            if item.tags is not None:
                for key, frame in item.tags.items():
                    if frame.FrameID in {'TPE1','TPE2','TALB','TIT2','TDRC','TCON','TRCK','TPOS'}:
                        continue
                    if frame.FrameID == 'APIC' and frame.type == 3:
                        continue
                    result.append((key, repr(frame)))
            return sorted(result)
        return custom_frames(original) == custom_frames(proposed)
    # No automatic replacement for a format whose additional tags we cannot inspect.
    return False


def identical_encoded_audio(a, b):
    """Require identical compressed audio and stream format, not just similar music."""
    a, b = Path(a), Path(b)
    if a.suffix.lower() != b.suffix.lower():
        return False
    before_a, before_b = sort.file_checksum(a), sort.file_checksum(b)
    left, right = _stream_digest(a), _stream_digest(b)
    if sort.file_checksum(a) != before_a or sort.file_checksum(b) != before_b:
        raise RuntimeError('Audio file changed during duplicate verification.')
    return left == right


def _atomic_restore(destination, source, expected):
    destination = Path(destination)
    fd, temp = tempfile.mkstemp(prefix='.mixie-restore-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as stream, Path(source).open('rb') as previous:
            shutil.copyfileobj(previous, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if sort.file_checksum(temp) != expected:
            raise RuntimeError('Restoration copy differs from verified backup.')
        os.replace(temp, destination)
        sort._sync_directory(destination.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


@dataclass
class MergeResult:
    created: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    reused_stream: set[str] = field(default_factory=set)
    reused_assets: set[str] = field(default_factory=set)
    upgraded: list[str] = field(default_factory=list)
    backup: Path | None = None
    _previous: dict[str, tuple[Path, bytes]] = field(default_factory=dict)
    _new_hashes: dict[str, bytes] = field(default_factory=dict)

    def rollback(self):
        # This is for failures *before* a completion receipt is constructed.
        for name, (original, digest) in reversed(list(self._previous.items())):
            if sort.file_checksum(original) != digest:
                raise RuntimeError('An upgrade recovery backup changed; manual recovery required.')
            current = sort.file_checksum(name)
            if current != digest:
                expected = self._new_hashes.get(name)
                if expected is None or current != expected:
                    raise RuntimeError('Library file changed after upgrade; verified backup retained for manual recovery.')
                _atomic_restore(name, original, digest)
        for created in reversed(self.created):
            if Path(created).is_file() and not Path(created).is_symlink():
                Path(created).unlink()


def _durable_backup(replacements):
    """Back up exact old library bytes, preserving the backups after success."""
    if not replacements:
        return None, {}
    root = archive._backup_root()
    scratch = Path(tempfile.mkdtemp(prefix='.mixie-upgrade-preparing-', dir=root))
    saved = {}
    try:
        for index, (item, oldhash) in enumerate(replacements):
            original = Path(item['destination'])
            target = scratch / f'{index:04d}.original'
            shutil.copy2(original, target, follow_symlinks=False)
            if target.is_symlink() or sort.file_checksum(target) != oldhash:
                raise RuntimeError('Library original changed during upgrade backup.')
            saved[str(original)] = (target, oldhash)
        manifest = {'version': 1, 'replaced_files': [
            {'destination': path, 'old_sha256': digest.hex(),
             'backup_name': original.name} for path, (original, digest) in saved.items()
        ]}
        record = scratch / 'manifest.json'
        with record.open('x', encoding='utf-8') as out:
            json.dump(manifest, out, indent=2, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        sort._sync_directory(scratch)
        final = root / ('library-metadata-upgrade-' + hashlib.sha256(record.read_bytes()).hexdigest()[:16])
        if final.exists() or final.is_symlink():
            if final.is_symlink() or not final.is_dir():
                raise RuntimeError('Unsafe existing metadata upgrade backup.')
            if (final / 'manifest.json').read_bytes() != record.read_bytes() or any(
                    sort.file_checksum(final / old.name) != digest
                    for old, digest in saved.values()):
                raise RuntimeError('Existing metadata upgrade backup failed verification.')
            shutil.rmtree(scratch)
        else:
            scratch.rename(final)
            sort._sync_directory(root)
        return final, {key: (final / old.name, digest) for key, (old, digest) in saved.items()}
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def commit_verified_audio_plan(plan, audio_destinations, protected=None,
                               *, allow_stale_receipt_reuse=False):
    """Commit verified work; harmless tag deltas no longer duplicate audio.

    protected: destination -> expected SHA256(s) from existing completion receipts.
    Files bound to any completion receipt are reused, never rewritten.  A new
    completion receipt records the *actual* reused file hash and source mapping.
    """
    if not plan:
        raise ValueError('Empty library plan.')
    audio_destinations = {str(Path(path).absolute()) for path in audio_destinations}
    protected = protected or {}
    library = Path(sort.music_library_folder).resolve()
    pending, identical, reuse_audio, reuse_assets, replacements = [], [], [], [], []
    seen = set()
    for item in plan:
        source = Path(item['source']).absolute()
        dest = Path(item['destination']).absolute()
        key = str(dest).casefold()
        if key in seen:
            raise ValueError(f'Duplicate destination: {dest}')
        seen.add(key)
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'Invalid source: {source}')
        parent = dest.parent.resolve()
        if not parent.is_relative_to(library) or source == dest:
            raise ValueError('Unsafe library destination.')
        if not os.path.lexists(dest):
            if dest.parent.exists() and any(n.casefold() == dest.name.casefold() for n in os.listdir(dest.parent)):
                raise FileExistsError(f'Filename conflict: {dest}')
            pending.append(item)
            continue
        if dest.is_symlink() or not dest.is_file():
            raise FileExistsError(f'Unsafe destination: {dest}')
        newhash, oldhash = sort.file_checksum(source), sort.file_checksum(dest)
        expected = set(protected.get(str(dest), ()))
        if expected and expected != {oldhash.hex()}:
            # Historical archive-backed receipts can lag user-edited tags.
            # Import-first mode may retain the existing file, but NEVER replace
            # mismatched receipt bytes or declare the old receipt current.
            if not allow_stale_receipt_reuse:
                raise RuntimeError(f'Existing completed receipt disagrees with library: {dest}')
            if str(dest) in audio_destinations:
                if not identical_encoded_audio(source, dest):
                    raise RuntimeError(f'Existing completed receipt disagrees with audio: {dest}')
                reuse_audio.append(str(dest))
            else:
                # Non-audio artwork/extras are preserved as-is. A new receipt
                # records their live checksums, not the stale historical value.
                reuse_assets.append(str(dest))
            continue
        if newhash == oldhash:
            identical.append(str(dest))
            continue
        if str(dest) in audio_destinations:
            if not identical_encoded_audio(source, dest):
                raise FileExistsError(f'Different audio already exists: {dest}')
            if expected or not _preserves_extra_tags(source, dest):
                reuse_audio.append(str(dest))
            else:
                replacements.append((item, oldhash))
        elif dest.name.casefold() in ('cover.jpg', 'cover.png'):
            if expected:
                reuse_assets.append(str(dest))
            else:
                replacements.append((item, oldhash))
        else:
            # Different supplementary booklets/scans are not evidence of a
            # different recording. Keep the existing file untouched; the new
            # supplied asset remains protected in its original-download archive.
            reuse_assets.append(str(dest))
    result = MergeResult(already=identical + reuse_audio + reuse_assets,
                         reused_stream=set(reuse_audio), reused_assets=set(reuse_assets))
    result.backup, result._previous = _durable_backup(replacements)
    try:
        # New files are still created exclusively by MIXIE's existing safe copier.
        result.created = sort.copy_sorting_plan(pending) if pending else []
        for item, oldhash in replacements:
            source, dest = Path(item['source']), Path(item['destination'])
            if sort.file_checksum(dest) != oldhash:
                raise RuntimeError('Existing library file changed during planned metadata upgrade.')
            canonical_hash = sort.file_checksum(source)
            _atomic_restore(dest, source, canonical_hash)
            result.upgraded.append(str(dest))
            result._new_hashes[str(dest)] = canonical_hash
            if sort.file_checksum(dest) != canonical_hash:
                raise RuntimeError('Library metadata upgrade failed verification.')
        print(f'Existing identical files: {len(identical)}')
        print(f'Existing verified audio reused: {len(reuse_audio)}')
        print(f'Existing artwork reused: {len(reuse_assets)}')
        print(f'Existing files with verified metadata/art upgrades: {len(replacements)}')
        print(f'New library files: {len(result.created)}')
        if result.backup:
            print(f'Original library bytes backed up: {result.backup}')
        return result
    except BaseException:
        result.rollback()
        raise
