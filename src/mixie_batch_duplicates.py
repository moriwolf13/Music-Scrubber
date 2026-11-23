"""Ephemeral, same-run duplicate audio recognition for verified automatic imports.

Identical *encoded audio* is the only authority for reuse; similar names,
acoustic fingerprints, albums, or release metadata cannot establish it.
No source, original receipt, archive, or library file is changed here. Each
source gets an independent staged snapshot and ordinary commit/receipt path.
"""
from __future__ import annotations

from collections import defaultdict, Counter
from copy import deepcopy
from pathlib import Path

import mixie_library_merge
import sort
import tag


def _album_key(album):
    """A cheap candidate filter, never an assertion of duplicate identity."""
    tracks = album.get('tracks') or []
    name = ' '.join(str(album.get('album') or '').casefold().split())
    if not name or name in {'unknown album', 'unknown', 'untitled'} or len(tracks) < 2:
        return None
    extensions = tuple(sorted(Path(row['path']).suffix.casefold() for row in tracks))
    return name, len(tracks), extensions


def _similar_credits(left, right):
    """Refuse to reuse record-level evidence over materially different claims."""
    for field in ('title', 'artist', 'album'):
        l, r = left.get(field) or (), right.get(field) or ()
        if l and r and tuple(tag._normalize_label(str(v)) for v in l) != tuple(
                tag._normalize_label(str(v)) for v in r):
            return False
    return True


class BatchDuplicates:
    """Recognize staged repeat deliveries before any AcoustID/MusicBrainz scan.

    Only buckets with same normalized album name, count and formats are
    examined. Exact stream digests are expensive enough to avoid for all
    other albums but vastly cheaper than per-recording online identification.
    """
    def __init__(self, albums):
        self.representative = {}
        self.mappings = {}
        self.snapshots = {}
        self._index(albums)

    def _index(self, albums):
        groups = defaultdict(list)
        for key, album in albums.items():
            candidate = _album_key(album)
            if candidate is not None:
                groups[candidate].append((key, album))
        for group in groups.values():
            # The identical encoded streams have identical fidelity; choose the
            # better described local copy as the one full-scan representative.
            def local_quality(item):
                _key, album = item
                return sum(bool(row.get(field)) for row in album.get('tracks', ())
                           for field in ('artist', 'album_artist', 'title', 'track_number',
                                         'track_total', 'date', 'genre'))
            group.sort(key=lambda item: (-local_quality(item), str(item[0])))
            if len(group) < 2:
                continue
            signatures = {}
            for key, album in group:
                try:
                    pairs = []
                    hashes = {}
                    for row in album['tracks']:
                        path = Path(row['path']).absolute()
                        if path.is_symlink() or not path.is_file():
                            raise ValueError('Unsafe staged duplicate candidate.')
                        before = sort.file_checksum(path)
                        # Content identity, not a metadata- or artwork-dependent file SHA.
                        format_signature, content_hash = mixie_library_merge._stream_digest(path)
                        if sort.file_checksum(path) != before:
                            raise RuntimeError('Staged duplicate candidate changed during inspection.')
                        stream = (format_signature, content_hash.hex())
                        pairs.append((stream, path, row))
                        hashes[path] = before
                    # Ambiguous same-stream tracks cannot be reliably re-mapped.
                    if len({item[0] for item in pairs}) != len(pairs):
                        continue
                    signature = tuple(sorted(item[0] for item in pairs))
                    compatible = None
                    for owner, earlier in signatures.get(signature, []):
                        earlier_by_digest = {item[0]: item for item in earlier}
                        mapping = {earlier_by_digest[item[0]][1]: item[1] for item in pairs}
                        # A tag disagreement is not a different recording when
                        # the encoded streams are byte-identical. Selected
                        # metadata will come only from verified release evidence.
                        compatible = (owner, earlier, mapping)
                        break
                    if compatible is None:
                        signatures.setdefault(signature, []).append((key, pairs))
                        self.snapshots[key] = hashes
                    else:
                        owner, earlier, mapping = compatible
                        self.representative[key] = owner
                        self.mappings[key] = mapping
                        self.snapshots[key] = hashes
                        self.snapshots.setdefault(owner, {item[1]: sort.file_checksum(item[1]) for item in earlier})
                except (OSError, ValueError, RuntimeError, TypeError):
                    # Optional optimization fails closed: ordinary ingest still
                    # identifies and validates the album independently.
                    continue

    def source(self, key):
        return self.representative.get(key)

    def validated_mapping(self, key):
        if key not in self.mappings:
            return None
        reference = self.representative[key]
        for item in (key, reference):
            for path, expected in self.snapshots[item].items():
                if path.is_symlink() or not path.is_file() or sort.file_checksum(path) != expected:
                    raise RuntimeError('Previously indexed duplicate audio changed; do not reuse evidence.')
        return dict(self.mappings[key])


def ordered_deliveries(albums, duplicate_index):
    """Group preverified duplicate sources directly after their representative.

    Do not drop any delivery: every original still needs its own manifest,
    backup, working-copy verification and source-bound receipt.
    """
    dependents = defaultdict(list)
    for key in albums:
        owner = duplicate_index.source(key)
        if owner is not None:
            if owner not in albums or owner == key:
                raise ValueError('Duplicate preflight has an invalid representative.')
            dependents[owner].append(key)
    result = []
    for key in albums:
        if duplicate_index.source(key) is not None:
            continue
        result.append((key, albums[key]))
        result.extend((child, albums[child]) for child in dependents.pop(key, ()))
    if dependents or len(result) != len(albums) or {key for key, _ in result} != set(albums):
        raise ValueError('Duplicate ordering failed to preserve every original source.')
    return result


def _rebase(value, mapping):
    """Rewrite only exact, already verified source path identities in evidence."""
    if isinstance(value, Path):
        return mapping.get(value, value)
    if isinstance(value, str):
        return str(mapping[Path(value)]) if Path(value) in mapping else value
    if isinstance(value, dict):
        return {_rebase(key, mapping): _rebase(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebase(item, mapping) for item in value]
    if isinstance(value, tuple):
        return tuple(_rebase(item, mapping) for item in value)
    return deepcopy(value)


def reuse_verified_plan(service, original, album, mapping):
    """Rebind previously VERIFIED album evidence and re-propose NEW source tags.

    Reuse never manufactures release evidence. A first-pass plan must already
    have been independently verified and committed in the same run. The new
    files are separately backed up and reverified by test_album_plan().
    """
    if (original.get('provenance') not in ('independently_verified', 'independently_verified_partial')
            or original.get('interpretation') is not None
            or original.get('reviewed_decision') is not None
            or original.get('artwork_decision') is not None
            or original.get('manual_fields')
            or original.get('_batch_common') is None
            or not original.get('compatible_ids')
            or not original.get('identities')):
        raise ValueError('A duplicate album cannot reuse unapproved or incomplete evidence.')
    old_paths = {Path(row['path']).absolute() for row in original['audits']}
    new_paths = {Path(row['path']).absolute() for row in album['tracks']}
    if (set(mapping) != old_paths or set(mapping.values()) != new_paths
            or len(mapping) != len(new_paths)):
        raise ValueError('Duplicate proof does not cover every recording exactly once.')
    rebound = _rebase(original, mapping)
    # The independently verified release mapping is unchanged; do not silently
    # change the selected recording or album edition for a second copy.
    if set(rebound['identities']) != {str(p) for p in new_paths}:
        raise ValueError('Duplicate recording identity mapping is incomplete.')
    current = {Path(row['path']).absolute(): row for row in album['tracks']}
    for audit in rebound['audits']:
        path = Path(audit['path']).absolute()
        audit['existing'] = current[path]

    issue = service._album_issue(album)
    manifest = service.completion_source_manifest(issue)
    rebound['source_manifest'] = manifest
    rebound['source_fingerprint'] = service.completion_fingerprint(
        issue['import_name'], service._source_rows(issue, manifest), manifest)
    rebound['reviewed_decision'] = None
    rebound['artwork_decision'] = None
    rebound['manual_fields'] = {}
    rebound['tag_edits'] = service._validated_tag_edits(None, sorted(new_paths))
    rebound['album_overrides'] = {}

    matched, tracklist = rebound['matched'], rebound['release_tracks']
    common = rebound['_batch_common']
    proposal = service.tag.propose_release_numbering(
        matched, tracklist, allow_partial=rebound.get('partial', False),
        independently_verified=(not rebound.get('partial', False) and len(matched) >= 3
                                and rebound.get('provenance') == 'independently_verified'))
    proposal_common = {name: value for name, value in common.items()
                       if name not in ('album', 'album_artist', 'date')}
    proposal.extend(service.tag.propose_common_metadata(matched, proposal_common))
    proposal.extend(service.tag.propose_consistent_album_tags(matched, common, album['tracks']))
    if not common.get('album_artist') and not any(row.get('album_artist') for row in album['tracks']):
        primary = service._suggest_album_artist(album['tracks'])
        if primary:
            proposal.extend({'path': path, 'field': 'album_artist', 'value': primary,
                             'verified': False, 'source': 'unanimous_primary_track_artist'}
                            for path in matched.values())
    # Re-run the canonical field decision against the SECOND delivery's tags.
    # For example, a source carrying an independently plausible genre must not
    # inherit a first source's neutral "Unclassified" placeholder.
    album_artist = common.get('album_artist') or service._suggest_album_artist(album['tracks'])
    genre_hint = (service._catalog_genre_hint(album, album_artist)
                  if album_artist else None)
    selected_evaluation = rebound.get('_batch_evaluation')
    if not isinstance(selected_evaluation, dict):
        raise ValueError('Duplicate reuse lacks the chosen verified release evaluation.')
    fields = service._auto_album_metadata(album, common, selected_evaluation,
                                          artist_hint=album_artist, genre_hint=genre_hint)
    rebound['auto_album_fields'] = fields
    proposal.extend(service.tag.propose_required_album_metadata(
        matched, common, contextual_genre=genre_hint,
        allow_unclassified=True, canonical_genre=fields.get('genre')))
    rebound['proposals'] = proposal
    rebound.pop('_verified_changes', None)
    return rebound
