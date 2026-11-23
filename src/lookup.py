import musicbrainzngs
import re
import tag
from collections import Counter, defaultdict
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from functools import lru_cache, wraps
from copy import deepcopy
from pathlib import Path
from threading import RLock
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from itertools import count
import math
import os
import sys
import time
from xml.parsers.expat import ExpatError

# Identify MIXIE when contacting MusicBrainz
musicbrainzngs.set_useragent("MIXIE", "0.1")

# Respect MusicBrainz's API request limit
musicbrainzngs.set_rate_limit(1.0)

MUSICBRAINZ_TIMEOUT = 20
MAX_MUSICBRAINZ_BYTES = 2_000_000
RELEASE_MEMBERSHIP_PAGE_SIZE = 100
MAX_RELEASE_MEMBERSHIP_PAGES = 3
MAX_RELEASE_EVALUATIONS = 96
MAX_MUSICBRAINZ_RETRY_DELAY = 30.0
_musicbrainz_lock = RLock()
_album_requests = ContextVar("musicbrainz_album_requests", default=None)
_album_sequence = count(1)
_musicbrainz_cooldown_until = 0.0
_musicbrainz_cooldown_status = None


def _musicbrainz_policy():
    try:
        interval = float(os.environ.get("MIXIE_MB_INTERVAL_SECONDS", "2"))
        attempts = int(os.environ.get("MIXIE_MB_MAX_ATTEMPTS", "3"))
    except (ValueError, OverflowError):
        raise ValueError("MusicBrainz interval/attempt settings must be valid numbers.") from None
    if not math.isfinite(interval) or not 1 <= interval <= 10:
        raise ValueError("MIXIE_MB_INTERVAL_SECONDS must be between 1 and 10.")
    if not 1 <= attempts <= 3:
        raise ValueError("MIXIE_MB_MAX_ATTEMPTS must be between 1 and 3.")
    diagnostics = os.environ.get("MIXIE_MB_DIAGNOSTICS", "0").lower() in ("1", "true", "yes")
    return {"interval_seconds": interval, "max_attempts": attempts, "diagnostics": diagnostics}


def current_musicbrainz_album():
    """Detached, nonidentifying counters; never request arguments or credentials."""
    stats = _album_requests.get()
    return deepcopy(stats) if stats is not None else None


def _musicbrainz_diagnostic(event, policy, **fields):
    if not policy["diagnostics"]:
        return
    allowed = {"album_sequence", "request_number", "category", "attempt", "retry_number",
               "http_status", "failure_kind", "duration_seconds", "pacing_seconds", "delay_seconds",
               "interval_seconds", "actual_attempts", "failed_attempts", "http_failures", "retries",
               "logical_requests", "categories", "failures", "outcome", "reason", "elapsed_seconds"}
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event}
    record.update({key: value for key, value in fields.items() if key in allowed})
    try:
        print("MIXIE_MB " + json.dumps(record, sort_keys=True, allow_nan=False), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # Optional diagnostics must never authorize, invalidate, or interrupt a match.
        pass


@contextmanager
def musicbrainz_album():
    """Group one album's actual attempts; nested review/planning shares the scope."""
    if _album_requests.get() is not None:
        yield _album_requests.get()
        return
    policy = _musicbrainz_policy()
    stats = {"album_sequence": next(_album_sequence), "actual_attempts": 0, "failed_attempts": 0,
             "http_failures": 0, "retries": 0, "logical_requests": 0, "categories": {}, "failures": {}}
    token = _album_requests.set(stats)
    started = time.monotonic()
    outcome = "finished"
    try:
        yield stats
    except (KeyboardInterrupt, SystemExit):
        outcome = "interrupted"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        _musicbrainz_diagnostic("album_summary", policy, **stats, outcome=outcome,
                                elapsed_seconds=round(time.monotonic() - started, 6))
        _album_requests.reset(token)


def _musicbrainz_category(provider, options):
    name = getattr(provider, "__name__", "")
    fixed = {"get_recording_by_id": "recording_hydration", "browse_releases": "membership_browse",
             "search_recordings": "recording_search", "search_releases": "release_search"}
    if name in fixed:
        return fixed[name]
    if name == "get_release_by_id":
        includes = options.get("includes", [])
        if "recordings" in includes:
            return "release_tracklist"
        return "release_metadata" if "artists" in includes else "release_type"
    return "other"


def _exception_chain(error):
    pending, seen, result = [error], set(), []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        for name in ("cause", "__cause__", "reason"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return result


def _retry_after(error):
    for current in _exception_chain(error):
        headers = getattr(current, "headers", None)
        value = headers.get("Retry-After") if isinstance(current, HTTPError) and headers is not None else None
        if not isinstance(value, str):
            continue
        try:
            delay = float(value)
        except (ValueError, OverflowError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = date.timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return None


def _musicbrainz_failure(error):
    chain = _exception_chain(error)
    status = next((current.code for current in chain if isinstance(current, HTTPError)
                   and type(current.code) is int), None)
    if status is None:
        status = next((current.http_status for current in chain
                       if type(getattr(current, "http_status", None)) is int), None)
    if isinstance(error, (ValueError, TypeError, KeyError, ExpatError)):
        kind, retryable = "malformed_data", False
    elif status is not None:
        kind = f"http_{status}" if status in (408, 429, 503) else "http_5xx" if 500 <= status <= 599 else "http_error"
        retryable = status in (408, 429) or 500 <= status <= 599
    elif any(isinstance(current, TimeoutError) for current in chain):
        kind, retryable = "timeout", True
    elif any(isinstance(current, (URLError, ConnectionError, HTTPException, musicbrainzngs.NetworkError))
             for current in chain):
        kind, retryable = "transport", True
    elif any(isinstance(current, (ValueError, TypeError, KeyError, ExpatError)) for current in chain):
        kind, retryable = "malformed_data", False
    else:
        kind, retryable = "provider_error", False
    return {"failure_kind": kind, "http_status": status, "retryable": retryable}


class MusicBrainzCooldown(musicbrainzngs.NetworkError):
    def __init__(self, status):
        self.http_status = status
        super().__init__("MusicBrainz requested a longer cooldown; postpone online verification.")


def _wait_musicbrainz_cooldown(policy, category):
    delay = max(0.0, _musicbrainz_cooldown_until - time.monotonic())
    if delay > MAX_MUSICBRAINZ_RETRY_DELAY:
        _musicbrainz_diagnostic("cooldown", policy, category=category, delay_seconds=round(delay, 6),
                                http_status=_musicbrainz_cooldown_status, reason="server_wait_exceeds_bound")
        raise MusicBrainzCooldown(_musicbrainz_cooldown_status)
    if delay:
        _musicbrainz_diagnostic("provider_wait", policy, category=category, delay_seconds=round(delay, 6))
        time.sleep(delay)


def _musicbrainz_call(provider, *args, **kwargs):
    """Bound wire attempts; each retry re-enters the client's ONE shared limiter.

    The library's internal retry loop is deliberately kept at one attempt. Its
    limiter surrounds a logical request, so enabling internal retries can burst
    a retry and the following request together. Never log URLs, arguments, headers,
    response bodies, exception messages, identifiers, titles or credentials here.
    """
    global _musicbrainz_cooldown_until, _musicbrainz_cooldown_status
    policy = _musicbrainz_policy()
    category = _musicbrainz_category(provider, kwargs)
    stats = _album_requests.get()
    if stats is not None:
        stats["logical_requests"] += 1
    transport = musicbrainzngs.musicbrainz
    with _musicbrainz_lock:
        musicbrainzngs.set_rate_limit(policy["interval_seconds"], new_requests=1)
        original = transport._safe_read
        state = {}

        class BoundedResponse:
            def __init__(self, response):
                self.response = response

            def read(self):
                try:
                    data = self.response.read(MAX_MUSICBRAINZ_BYTES + 1)
                    if len(data) > MAX_MUSICBRAINZ_BYTES:
                        raise ValueError("MusicBrainz response exceeds 2 MB.")
                    return data
                except BaseException as error:
                    state["wire_error"] = error
                    raise
                finally:
                    self.response.close()
                    state["wire_end"] = time.monotonic()

        class BoundedOpener:
            def __init__(self, opener):
                self.opener = opener

            def open(self, request, body=None):
                state["wire_start"] = time.monotonic()
                if stats is not None:
                    stats["actual_attempts"] += 1
                    stats["categories"][category] = stats["categories"].get(category, 0) + 1
                    stats["retries"] += state["attempt"] > 1
                _musicbrainz_diagnostic("http_start", policy, category=category,
                    album_sequence=stats["album_sequence"] if stats else None,
                    request_number=stats["actual_attempts"] if stats else None,
                    attempt=state["attempt"], retry_number=state["attempt"] - 1)
                try:
                    response = self.opener.open(request, body, timeout=MUSICBRAINZ_TIMEOUT)
                    status = getattr(response, "status", None)
                    state["http_status"] = status if type(status) is int else None
                    return BoundedResponse(response)
                except BaseException as error:
                    state["wire_error"] = error
                    state["wire_end"] = time.monotonic()
                    raise

        def read(opener, request, body=None):
            return original(BoundedOpener(opener), request, body,
                            max_retries=1, retry_delay_delta=0)

        transport._safe_read = read
        try:
            for attempt in range(1, policy["max_attempts"] + 1):
                _wait_musicbrainz_cooldown(policy, category)
                state.clear()
                state.update(attempt=attempt, started=time.monotonic(), http_status=None)
                failure, retry_delay = None, None
                try:
                    return provider(*args, **kwargs)
                except Exception as error:
                    evidence = _musicbrainz_failure(error)
                    failure = evidence["failure_kind"]
                    state["http_status"] = evidence["http_status"] or state["http_status"]
                    if stats is not None and "wire_start" in state:
                        stats["failed_attempts"] += 1
                        stats["http_failures"] += evidence["http_status"] is not None and evidence["http_status"] >= 400
                        stats["failures"][failure] = stats["failures"].get(failure, 0) + 1
                    server_delay = _retry_after(error)
                    if server_delay is not None and "wire_start" in state:
                        _musicbrainz_cooldown_until = max(_musicbrainz_cooldown_until, time.monotonic() + server_delay)
                        _musicbrainz_cooldown_status = evidence["http_status"]
                    can_retry = evidence["retryable"] and "wire_start" in state and attempt < policy["max_attempts"]
                    if can_retry:
                        retry_delay = max(2.0 ** attempt, server_delay or 0.0)
                        can_retry = retry_delay <= MAX_MUSICBRAINZ_RETRY_DELAY
                    if not can_retry:
                        if evidence["retryable"] and "wire_start" in state:
                            final = musicbrainzngs.NetworkError(f"MusicBrainz request failed after {attempt} HTTP attempt(s).", error)
                            final.musicbrainz_attempts = attempt
                            raise final from error
                        raise
                except BaseException:
                    failure = "interrupted"
                    raise
                finally:
                    if "wire_start" in state:
                        _musicbrainz_diagnostic("http_attempt", policy, category=category,
                            album_sequence=stats["album_sequence"] if stats else None,
                            request_number=stats["actual_attempts"] if stats else None,
                            attempt=attempt, retry_number=attempt - 1, http_status=state["http_status"],
                            failure_kind=failure, interval_seconds=policy["interval_seconds"],
                            duration_seconds=round(max(0.0, state.get("wire_end", time.monotonic()) - state["wire_start"]), 6),
                            pacing_seconds=round(max(0.0, state["wire_start"] - state["started"]), 6))
                    for error in _exception_chain(state["wire_error"]) if "wire_error" in state else ():
                        if isinstance(error, HTTPError):
                            error.close()
                _musicbrainz_diagnostic("retry_wait", policy, category=category, retry_number=attempt,
                                        delay_seconds=retry_delay, reason=failure)
                time.sleep(retry_delay)
        finally:
            transport._safe_read = original


def provider_error_evidence(stage, error, *, recording_id=None, code="provider_error"):
    """Retain provider failures without confusing transient outages with ambiguity.

    MusicBrainz wraps HTTP errors in ``cause``; urllib can wrap timeouts in
    ``reason``. Inspect exception objects, not message text or source metadata.
    Retryability is diagnostic only and never authorizes an identity.
    """
    pending, seen = [error], set()
    http_status, retryable = None, False
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, HTTPError) and type(current.code) is int and http_status is None:
            http_status = current.code
        if isinstance(current, (TimeoutError, ConnectionError, URLError, musicbrainzngs.NetworkError)):
            retryable = True
        for name in ("cause", "__cause__", "reason"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    if http_status is not None:
        retryable = http_status in (408, 429) or 500 <= http_status <= 599
    retryable = retryable and code == "provider_error" and stage in (
        "acoustid", "musicbrainz", "release_tracklist")
    evidence = {"stage": stage, "recording_id": recording_id, "code": code,
                "type": type(error).__name__, "message": str(error), "retryable": retryable}
    if http_status is not None:
        evidence["http_status"] = http_status
    return evidence


def _snapshot_cache(function):
    @lru_cache(maxsize=256)
    def cached(*args, **kwargs):
        return deepcopy(function(*args, **kwargs))

    @wraps(function)
    def snapshot(*args, **kwargs):
        return deepcopy(cached(*args, **kwargs))

    snapshot.cache_clear = cached.cache_clear
    snapshot.cache_info = cached.cache_info
    return snapshot


class ReleaseTracklist(list):
    """List-compatible track snapshot carrying remote coverage completeness."""
    def __init__(self):
        super().__init__()
        self.complete = True


def _count_matches(value, actual):
    if value is None:
        return False
    try:
        return not isinstance(value, bool) and str(value).isdigit() and int(value) == actual
    except ValueError:
        return False

# Search MusicBrainz for possible album releases
def search_album(artist, album):
    results = _musicbrainz_call(
        musicbrainzngs.search_releases, artist=artist,
        release=album,
        limit=5
    )
    return results.get("release-list", [])


def find_exact_album_releases(artist, album):
    """Conservative album/artist identity fallback, not recording verification.

    The search is deliberately limited by search_album (five results). An
    approximate title match, compilation or unrelated artist cannot authorize
    downloading an unrelated cover.
    """
    if not isinstance(artist, str) or not artist.strip() or not isinstance(album, str) or not album.strip():
        return []
    releases = search_album(artist, album)
    found = []
    for item in releases:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if (not isinstance(identifier, str) or not identifier
                or not isinstance(item.get("title"), str)
                or normalize_title(item["title"]) != normalize_title(album)):
            continue
        credit = item.get("artist-credit-phrase")
        if not isinstance(credit, str) or tag._normalize_label(credit) != tag._normalize_label(artist):
            continue
        if identifier not in found:
            found.append(identifier)
    return found[:5]


def find_catalog_number_releases(artist, album, catalog_number):
    """MusicBrainz catalog-number search: DISCOVERY ONLY, never edition proof.

    A labeled import such as Americana (ESCA 7393) can otherwise get buried
    behind 96 unrelated retail/compilation memberships. Exact catalog, album
    and artist matches are nominated FIRST, but the normal audio/tracklist
    evaluator and competing-release gates still decide whether it is safe.
    """
    if not all(isinstance(value, str) and value.strip()
               for value in (artist, album, catalog_number)):
        return []
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{2,39}", catalog_number):
        return []
    data = _musicbrainz_call(musicbrainzngs.search_releases,
                             artist=artist, release=album,
                             catno=catalog_number, limit=25)
    result = []
    for item in data.get("release-list", []):
        if not isinstance(item, dict):
            continue
        identifier, title, credit = (item.get("id"), item.get("title"),
                                     item.get("artist-credit-phrase"))
        if (isinstance(identifier, str) and identifier and
                isinstance(title, str) and normalize_title(title) == normalize_title(album)
                and isinstance(credit, str)
                and tag._normalize_label(credit) == tag._normalize_label(artist)):
            result.append(identifier)
    return list(dict.fromkeys(result))[:25]


# Indexed MusicBrainz count metadata can remove incompatible edition candidates
# without using any source title as a recording identity.
_COUNT_RELEASE_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
COUNT_SEARCH_BATCH = 12
MAX_COUNT_SEARCH_IDS = 220


def _search_hit_audio_count(release, expected):
    """Known playable-audio track count; None when the data may be ambiguous.

    A 13-song Enhanced CD can appear as 23 tracks when multimedia content is
    included. Never reject such an edition on its indexed *total* alone.
    """
    media = release.get("medium-list") if isinstance(release, dict) else None
    if not isinstance(media, list) or len(media) != 1:
        # A delivery can be one disc of a multidisc box. Total tracks across
        # media cannot disprove that partial-disc edition.
        return None
    medium_count = release.get("medium-count")
    if medium_count is not None and not _count_matches(medium_count, len(media)):
        return None
    total = 0
    for medium in media:
        if not isinstance(medium, dict):
            return None
        raw = medium.get("track-count", medium.get("medium-track-count"))
        if isinstance(raw, bool) or raw is None or not str(raw).isdigit():
            return None
        count = int(raw)
        if count < 1:
            return None
        fmt = str(medium.get("format") or "").casefold()
        if (count != expected and
                ("enhanced" in fmt or "data" in fmt or
                 "data-track-list" in medium or "data-track-count" in medium or
                 "data-tracks" in medium)):
            return None
        total += count
    return total


def filter_release_ids_by_audio_track_count(release_ids, expected):
    """Prune confirmed nonmatching counts before full recording evaluation.

    Searches only provider-indexed medium summaries in bounded batches.
    Missing hits, malformed/incomplete data, unknown multimedia counts and
    provider failures remain candidate UNKNOWNs, never false exclusions.
    A matching count is not a fingerprint, song mapping or edition approval.
    """
    identifiers = list(dict.fromkeys(release_ids))
    if (isinstance(expected, bool) or not isinstance(expected, int) or expected < 3
            or len(identifiers) > MAX_COUNT_SEARCH_IDS):
        return identifiers, []
    counts = {}
    for begin in range(0, len(identifiers), COUNT_SEARCH_BATCH):
        batch = identifiers[begin:begin + COUNT_SEARCH_BATCH]
        if not all(isinstance(identifier, str) and _COUNT_RELEASE_ID.fullmatch(identifier)
                   for identifier in batch):
            continue  # synthetic/invalid IDs have no usable catalog evidence
        query = " OR ".join("reid:" + identifier for identifier in batch)
        try:
            result = _musicbrainz_call(musicbrainzngs.search_releases,
                                       query=query, limit=len(batch))
        except AssertionError:
            raise  # A failed isolated-network guard is never hidden.
        except (OSError, ValueError, TypeError, KeyError, musicbrainzngs.MusicBrainzError):
            continue
        rows = result.get("release-list", []) if isinstance(result, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("id") not in batch:
                continue
            count = _search_hit_audio_count(row, expected)
            if count is not None:
                counts[row["id"]] = count
    rejected = [identifier for identifier in identifiers
                if identifier in counts and counts[identifier] != expected]
    rejected_set = set(rejected)
    return [identifier for identifier in identifiers if identifier not in rejected_set], rejected


# Search for a recording using its artist and title
def search_recording(artist, title):
    results = _musicbrainz_call(
        musicbrainzngs.search_recordings, artist=artist,
        recording=title,
        limit=5
    )
    return results.get("recording-list", [])

# Check whether a recording credits the expected artist
def recording_credits_artist(recording, artist):
    target = artist.casefold().strip()
    for credit in recording.get("artist-credit", []):
        if not isinstance(credit, dict):
            continue
        name = credit.get("artist", {}).get("name", "")
        if name.casefold().strip() == target:
            return True
    return False

# Find possible releases containing a particular song
def find_track_releases(artist, title):
    release_ids = set()

    for recording in search_recording(artist, title):
        if not recording_credits_artist(recording, artist):
            continue

        if normalize_title(recording.get("title", "")) != normalize_title(title):
            continue

        for release in recording.get("release-list", []):
            release_id = release.get("id")

            if release_id:
                release_ids.add(release_id)

    return release_ids

# Fetch every audio position without discarding remote completeness evidence.
def _fetch_release_tracklist(release_id):
    result = _musicbrainz_call(musicbrainzngs.get_release_by_id, release_id, includes=["recordings"])
    release = result.get("release") if isinstance(result, dict) else None
    if (not isinstance(release, dict) or not isinstance(release.get("medium-list", []), list)
            or ("id" in release and release["id"] != release_id)):
        raise ValueError("Malformed or mismatched MusicBrainz release response.")
    tracks = ReleaseTracklist()
    media = release.get("medium-list", [])
    tracks.complete = _count_matches(release.get("medium-count"), len(media))
    for medium in media:
        if not isinstance(medium, dict) or not isinstance(medium.get("track-list", []), list):
            raise ValueError("Malformed MusicBrainz medium response.")
        remote = medium.get("track-list", [])
        tracks.complete = tracks.complete and _count_matches(medium.get("track-count"), len(remote))
        for track in remote:
            recording = track.get("recording") if isinstance(track, dict) else None
            if not isinstance(recording, dict):
                raise ValueError("Malformed MusicBrainz release track.")
            tracks.append({
                "disc": medium.get("position", "1"),
                # MusicBrainz's position is ordinal; printed numbers may be A1,
                # B1, etc. Never infer positions from filenames or local tags.
                "number": track.get("position") if "position" in track else track.get("number"),
                "printed_number": track.get("number"),
                "title": track.get("title") or recording.get("title"),
                "recording_title": recording.get("title"),
                "recording_disambiguation": recording.get("disambiguation"),
                "recording_id": recording.get("id"), "track_id": track.get("id"),
                "length": track.get("length"), "recording_length": recording.get("length"),
                "medium_format": medium.get("format"), "position": track.get("position")
            })
    return tracks


_cached_release_tracklist = _snapshot_cache(_fetch_release_tracklist)


def get_release_tracklist(release_id, *, refresh=False):
    """Return a defensive batch snapshot, or explicitly fetch fresh evidence.

    refresh=True bypasses the batch cache and uses the same bounded,
    rate-limited MusicBrainz transport. It neither reads nor changes frozen
    batch snapshots, including unrelated entries. Fresh-fetch errors propagate
    instead of falling back to a cached success. ReleaseTracklist.complete is
    retained so review can reject incomplete data before normalizing the list.
    """
    if type(refresh) is not bool:
        raise ValueError("refresh must be a literal boolean.")
    if refresh:
        return deepcopy(_fetch_release_tracklist(release_id))
    return _cached_release_tracklist(release_id)


get_release_tracklist.cache_clear = _cached_release_tracklist.cache_clear
get_release_tracklist.cache_info = _cached_release_tracklist.cache_info


@_snapshot_cache
def _recording_header(recording_id):
    """Cache only a validated recording response, even if later paging fails."""
    result = _musicbrainz_call(musicbrainzngs.get_recording_by_id, recording_id,
                              includes=["artist-credits", "releases"])
    recording = result.get("recording") if isinstance(result, dict) else None
    if not isinstance(recording, dict) or recording.get("id") != recording_id:
        raise ValueError("Malformed or mismatched MusicBrainz recording response.")
    return recording


@_snapshot_cache
def _membership_page(recording_id, offset):
    """Cache validated browse pages individually; never cache a failed page."""
    result = _musicbrainz_call(
        musicbrainzngs.browse_releases, recording=recording_id,
        limit=RELEASE_MEMBERSHIP_PAGE_SIZE, offset=offset)
    rows = result.get("release-list") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) > RELEASE_MEMBERSHIP_PAGE_SIZE:
        raise ValueError("Malformed MusicBrainz release membership page.")
    for release in rows:
        identifier = release.get("id") if isinstance(release, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Malformed MusicBrainz release membership ID.")
    return result


def _complete_recording_membership(recording):
    # Embedded releases are often truncated. Browse from zero: the embedded
    # subset has no guaranteed ordering compatible with browse offsets.
    releases = recording.get("release-list", [])
    if not isinstance(releases, list):
        raise ValueError("Malformed MusicBrainz recording releases.")
    known = {}
    for release in releases:
        identifier = release.get("id") if isinstance(release, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Malformed MusicBrainz recording release ID.")
        known[identifier] = deepcopy(release)
    total = recording.get("release-count")
    complete = (total is not None and _count_matches(total, len(releases))
                and len(known) == len(releases))
    expected = _positive_integer(total)
    if not complete and expected is not None and expected > len(known):
        seen, consistent = set(), len(known) == len(releases)
        for page in range(MAX_RELEASE_MEMBERSHIP_PAGES):
            offset = page * RELEASE_MEMBERSHIP_PAGE_SIZE
            result = _membership_page(recording["id"], offset)
            rows = result["release-list"]
            page_ids = set()
            for release in rows:
                identifier = release["id"]
                page_ids.add(identifier)
                known[identifier] = deepcopy(release)
            consistent = (consistent and result.get("release-count") is not None
                          and _count_matches(result["release-count"], expected)
                          and len(page_ids) == len(rows) and not seen.intersection(page_ids))
            seen.update(page_ids)
            if len(rows) < RELEASE_MEMBERSHIP_PAGE_SIZE or offset + len(rows) >= expected:
                break
        complete = consistent and len(seen) == expected and set(known) == seen
    recording["release-list"] = list(known.values())
    recording["release-list-complete"] = complete
    return recording


@_snapshot_cache
def get_recording_details(recording_id):
    """Return defensive details; retain good subrequests across paging outages.

    Failed or malformed pages are never cached as evidence of release absence.
    A future retry reuses only validated work, without rehydrating the recording.
    """
    return _complete_recording_membership(_recording_header(recording_id))


_original_recording_cache_clear = get_recording_details.cache_clear


def _clear_recording_caches():
    # Existing callers expect one clear to reset ALL recording-related snapshots.
    _original_recording_cache_clear()
    _recording_header.cache_clear()
    _membership_page.cache_clear()


get_recording_details.cache_clear = _clear_recording_caches

# Retrieve album metadata from a MusicBrainz release
@_snapshot_cache
def get_release_metadata(release_id):
    # Ask for primary release type in the SAME request as artist metadata.
    # An absent release group is unknown, never evidence that it is a Single.
    result = _musicbrainz_call(
        musicbrainzngs.get_release_by_id, release_id,
        includes=["artists", "release-groups"]
    )
    release = result.get("release") if isinstance(result, dict) else None
    if not isinstance(release, dict) or release.get("id") != release_id:
        raise ValueError("Malformed or mismatched MusicBrainz release metadata.")
    group = release.get("release-group")
    if group is not None and not isinstance(group, dict):
        raise ValueError("Malformed MusicBrainz release group.")
    kind = (group or {}).get("primary-type")
    if kind is not None and not isinstance(kind, str):
        raise ValueError("Malformed MusicBrainz release type.")
    return {
        "release_id": release_id,
        "album": release.get("title"),
        "album_artist": release.get("artist-credit-phrase"),
        "date": release.get("date"),
        "country": release.get("country"),
        "status": release.get("status"),
        "barcode": release.get("barcode"),
        "release_type": kind,
        "release_group_id": (group or {}).get("id") if isinstance((group or {}).get("id"), str) else None,
        "original_release_date": (group or {}).get("first-release-date")
    }

# Find metadata shared by all candidate album releases
def common_release_metadata(release_ids):
    if not release_ids:
        return {}
    releases = [
        get_release_metadata(release_id)
        for release_id in sorted(release_ids)
    ]
    common = {}
    for field in ("album", "album_artist", "date"):
        values = [release.get(field) for release in releases]
        if all(values) and len(set(values)) == 1:
            common[field] = values[0]
    # Prefer the independently reported original album-group date, even when
    # every candidate happens to share a later retail pressing date.
    # Different/unknown groups or inconsistent original dates prove nothing.
    groups = {row.get("release_group_id") for row in releases}
    original_dates = {row.get("original_release_date") for row in releases}
    if len(groups) == len(original_dates) == 1 and None not in groups:
        original_date = next(iter(original_dates))
        if isinstance(original_date, str) and original_date.strip():
            try:
                import tag
                tag.validate_manual_metadata({"date": original_date})
            except (ValueError, TypeError):
                pass
            else:
                common["date"] = original_date
    return common

# Find approved front-cover artwork for a release
def get_release_front_art(release_id):
    url = f"https://coverartarchive.org/release/{release_id}"
    request = Request(url, headers={
        "User-Agent": "MIXIE/0.1",
        "Accept": "application/json"
    })
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read(MAX_MUSICBRAINZ_BYTES + 1)
        if len(payload) > MAX_MUSICBRAINZ_BYTES:
            raise ValueError("Cover metadata response exceeds 2 MB.")
        data = json.loads(payload)
        if not isinstance(data, dict) or not isinstance(data.get("images", []), list):
            raise ValueError("Malformed cover metadata response.")
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    for artwork in data.get("images", []):
        if not isinstance(artwork, dict):
            raise ValueError("Malformed cover image metadata.")
        if artwork.get("front") is True and artwork.get("approved") is True:
            image = artwork.get("image")
            thumbnails = artwork.get("thumbnails", {})
            if not isinstance(image, str) or not image or not isinstance(thumbnails, dict):
                raise ValueError("Malformed approved cover image URL.")
            preview = thumbnails.get("500")
            if preview is not None and not isinstance(preview, str):
                raise ValueError("Malformed cover preview URL.")
            return {"release_id": release_id, "image": image, "preview": preview}
    return None

def get_release_group_front_art(group_id):
    """Query only Cover Art Archive release-group front artwork."""
    if not isinstance(group_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", group_id):
        return None
    url = f"https://coverartarchive.org/release-group/{group_id}"
    request = Request(url, headers={"User-Agent": "MIXIE/0.1", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read(MAX_MUSICBRAINZ_BYTES + 1)
        if len(payload) > MAX_MUSICBRAINZ_BYTES:
            raise ValueError("Cover metadata response exceeds 2 MB.")
        result = json.loads(payload)
        if not isinstance(result, dict) or not isinstance(result.get("images", []), list):
            raise ValueError("Invalid release-group cover metadata.")
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    for image in result.get("images", []):
        if not isinstance(image, dict):
            raise ValueError("Malformed release-group image metadata.")
        if image.get("front") is True and image.get("approved") is True:
            original = image.get("image")
            thumb = image.get("thumbnails", {})
            if not isinstance(original, str) or not isinstance(thumb, dict):
                raise ValueError("Invalid release-group image URL.")
            preview = thumb.get("500")
            if preview is not None and not isinstance(preview, str):
                raise ValueError("Invalid cover preview URL.")
            return {"release_id": group_id, "image": original, "preview": preview}
    return None


def _fetch_cover_image(artwork, preview):
    if artwork is None:
        return None
    url = artwork["preview"] if preview and artwork.get("preview") else artwork["image"]
    url = url.replace("http://coverartarchive.org/", "https://coverartarchive.org/", 1)
    # Cover Art Archive directs image downloads to the Internet Archive.
    from urllib.parse import urlsplit
    target = urlsplit(url)
    if target.scheme != "https" or target.hostname not in ("coverartarchive.org", "archive.org") or target.username or target.password:
        raise ValueError("Cover image URL is not a trusted artwork host.")
    request = Request(url, headers={"User-Agent": "MIXIE/0.1"})
    with urlopen(request, timeout=8 if preview else 30) as response:
        mime = response.headers.get_content_type()
        data = response.read(20_000_001)
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError("Unsupported cover image format.")
    if not data or len(data) > 20_000_000:
        raise ValueError("Cover artwork size is invalid.")
    return {"mime": mime, "data": data}


def download_release_group_art(group_id, preview=False):
    return _fetch_cover_image(get_release_group_front_art(group_id), preview)


# Download approved front-cover artwork
def download_release_art(release_id, preview=False):
    return _fetch_cover_image(get_release_front_art(release_id), preview)


# Count how many tracks support each possible album release
def find_album_candidates(artist, tracks):
    candidates = Counter()

    for track in tracks:
        titles = track["title"]

        if not titles or not titles[0].strip():
            print(f"Missing title: {track['path'].name}")
            return candidates

        releases = find_track_releases(artist, titles[0])
        candidates.update(releases)

    return candidates

# Find releases shared by independently identified recordings
def shared_release_ids(recordings):
    release_sets = []
    for recording in recordings:
        if recording is None:
            continue
        releases = {
            release["id"]
            for release in recording.get("release-list", [])
            if release.get("id")
        }
        release_sets.append(releases)
    if len(release_sets) < 2:
        return set()
    return set.intersection(*release_sets)

# Find recording candidates belonging to independently matched releases
def recordings_on_releases(candidate_ids, release_ids):
    matching = []
    for recording_id in candidate_ids:
        details = get_recording_details(recording_id)
        recording_releases = {
            release["id"]
            for release in details.get("release-list", [])
            if release.get("id")
        }
        if recording_releases & release_ids:
            matching.append(details)
    return matching

def _accepted_recording(audit, *, independent=False):
    if audit.get("status") != "preliminary_match":
        return None
    assessment = audit.get("assessment")
    if assessment is not None:
        if assessment.get("status") != "verified" or not assessment.get("complete", False):
            return None
        if independent and assessment.get("release_id"):
            return None
    details = audit.get("details")
    if isinstance(details, dict) and isinstance(details.get("id"), str) and details["id"]:
        return details
    return None


def discover_release_candidates(audits, local_album=None, local_artist=None):
    """Nominate releases, ranking independent support before candidate support.

    Audio-supported alternatives can nominate a release even when identity is
    ambiguous or another candidate failed hydration. They never become anchors.
    Tagged hints and context-derived selections supply no votes. Discovery does
    no provider I/O and grants no identity, coverage or edition authorization.
    """
    support, alternatives = defaultdict(set), defaultdict(set)
    # Titles are soft ordering hints ONLY: never votes, identity evidence, or
    # permission to map a recording. They make an exact parent album likely
    # to be checked before hundreds of compilations and retail appearances.
    named_releases = defaultdict(set)
    album_hint = _discovery_album_key(local_album)
    if album_hint in ("unknown album", "unknown", "untitled"):
        album_hint = ""
    for audit in audits:
        assessment = audit.get("assessment") or {}
        if assessment.get("release_id"):
            continue
        details = _accepted_recording(audit, independent=True)
        nominations = [(details, support)] if details is not None else []
        for row in assessment.get("candidates", []):
            candidate = row.get("details")
            if (row.get("status") == "verified" and isinstance(candidate, dict)
                    and candidate.get("id") == row.get("recording_id")
                    and (details is None or candidate["id"] != details["id"])):
                nominations.append((candidate, alternatives))
        for candidate, votes in nominations:
            releases = candidate.get("release-list", [])
            for release in releases if isinstance(releases, list) else ():
                identifier = release.get("id") if isinstance(release, dict) else None
                if isinstance(identifier, str) and identifier:
                    # Several alternative IDs for ONE file are not several
                    # independent songs. Only accepted identities supply anchors.
                    votes[identifier].add(candidate["id"] if votes is support else Path(audit["path"]))
                    release_title = release.get("title")
                    if isinstance(release_title, str) and release_title.strip():
                        named_releases[identifier].add(_discovery_album_key(release_title))
    return sorted(set(support) | set(alternatives), key=lambda identifier: (
        -len(support[identifier]), -len(alternatives[identifier]),
        0 if album_hint and album_hint in named_releases[identifier] else 1, identifier))


def _discovery_album_key(value):
    if not isinstance(value, str):
        return ""
    # A catalog suffix is only a discovery hint, never exact-edition evidence.
    value = re.sub(r"\s*\([a-z]{2,}[- ]?\d{2,}\)\s*$", "", value, flags=re.IGNORECASE)
    return normalize_title(value)


def _positive_integer(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    try:
        number = int(text) if text.isdigit() else 0
    except ValueError:
        return None
    return number if number > 0 else None


def _position(disc, number):
    disc, number = _positive_integer(disc), _positive_integer(number)
    return (disc, number) if disc is not None and number is not None else None


def _number_tag(values):
    if not values:
        return None, None, False
    value = values[0] if isinstance(values, (tuple, list)) else values
    parts = str(value).split("/")
    number = _positive_integer(parts[0])
    total = _positive_integer(parts[1]) if len(parts) == 2 else None
    invalid = number is None or len(parts) > 2 or (len(parts) == 2 and total is None)
    return number, total, invalid


def _local_number_conflicts(metadata, position, remote):
    disc, number = position
    disc_count = len({key[0] for key in remote})
    track_count = sum(key[0] == disc for key in remote)
    reasons = []
    for field, expected, total_expected, total_field in (
            ("track_number", number, track_count, "track_total"),
            ("disc_number", disc, disc_count, "disc_total")):
        value, embedded_total, invalid = _number_tag(metadata.get(field, ()))
        if invalid:
            reasons.append(f"invalid_local_{field}")
        elif value is not None and value != expected:
            reasons.append(f"local_{field}_conflict")
        explicit_total, unused, invalid_total = _number_tag(metadata.get(total_field, ()))
        if invalid_total:
            reasons.append(f"invalid_local_{total_field}")
        if any(total is not None and total != total_expected for total in (embedded_total, explicit_total)):
            reasons.append(f"local_{total_field}_conflict")
    return reasons


def _recording_version_key(details):
    """Conservative provider version evidence, never source tags or filenames."""
    if not isinstance(details, dict):
        return frozenset()
    markers = r"\b(?:live|remix|mix|edit|version|clean|explicit|censored|uncensored|instrumental|acoustic|demo|remaster(?:ed)?|mono|stereo|karaoke|re[- ]?record(?:ed|ing)?|extended)\b"
    title = details.get("title")
    qualifiers = re.findall(r"\(([^()]*)\)|\[([^\[\]]*)\]|\s[-–—]\s(.+)$", title) if isinstance(title, str) else ()
    versions = {normalize_title(part) for parts in qualifiers for part in parts
                if part and re.search(markers, part, flags=re.IGNORECASE)}
    disambiguation = details.get("disambiguation")
    if isinstance(disambiguation, str) and disambiguation.strip():
        versions.add(normalize_title(disambiguation))
    return frozenset(versions)


def unresolved_recording_version(selected, candidates):
    """True when another audio-viable ID carries different provider version evidence.

    Release absence cannot distinguish an acoustically plausible live/remix/edit
    or clean/explicit recording. Only failed audio gates eliminate that version;
    a matching local title, album membership or slightly higher score cannot.
    """
    if not isinstance(selected, dict):
        return False
    # Clean and explicit are catalog choices, not different song mappings.
    # Retain all material live/remix/edit/arrangement qualifiers as blockers.
    version = _material_recording_version(selected)
    for row in candidates:
        if not isinstance(row, dict) or row.get("recording_id") == selected.get("id"):
            continue
        if {"low_confidence", "duration_conflict"}.intersection(row.get("reasons") or ()):
            continue
        other = row.get("details")
        if (isinstance(other, dict) and other.get("id") == row.get("recording_id")
                and _material_recording_version(other) != version):
            return True
    return False


def _release_version_unconfirmed(selected, positions, remote):
    version = _material_recording_version(selected)
    for position in positions:
        track = remote[position]
        claim = _material_recording_version({"title": track.get("title"),
                                             "disambiguation": track.get("recording_disambiguation")})
        if claim and claim != version:
            return True
    return False


def _context_candidates(assessment, by_recording):
    if (assessment.get("complete") is not True or assessment.get("errors")
            or assessment.get("release_id")):
        return set()
    rows = assessment.get("candidates") or ()
    # Unknown timing/details are not disproven competitors, even off-release.
    if any(isinstance(row, dict) and row.get("status") == "probable"
           and "low_confidence" not in (row.get("reasons") or ()) for row in rows):
        return set()
    return {row["recording_id"] for row in rows if isinstance(row, dict)
            and row.get("status") == "verified" and row.get("recording_id") in by_recording
            and isinstance(row.get("details"), dict)
            and row["details"].get("id") == row["recording_id"]}


def _whole_album_fingerprint_cohort(audits, paths, remote, by_recording):
    """Prove a unique full assignment by singleton elimination, not permutations.

    With equally many files and positions, a unique perfect bipartite matching
    must have a forced file edge. Removing forced edges either accounts for the
    entire album or leaves ambiguity/contradiction. No arbitrary first matching,
    exponential search, provider requests, local order or numbering is used.
    """
    edges = {}
    for audit, path in zip(audits, paths):
        assessment = audit.get("assessment") or {}
        accepted = _accepted_recording(audit, independent=True)
        identifiers = {accepted["id"]} if accepted else _context_candidates(assessment, by_recording)
        positions = set()
        for identifier in identifiers:
            targets = by_recording.get(identifier, ())
            if len(targets) != 1:
                return {}  # Repeated recordings need explicit position evidence.
            position = next(iter(targets))
            selected = accepted or next(row["details"] for row in assessment["candidates"]
                                        if row.get("recording_id") == identifier and row.get("status") == "verified")
            canonical_title = remote[position].get("recording_title") or remote[position].get("title")
            if (not isinstance(selected.get("title"), str) or not isinstance(canonical_title, str)
                    or not selected["title"].strip()
                    or normalize_title(selected["title"]) != normalize_title(canonical_title)):
                return {}
            positions.add(position)
        if not positions:
            return {}
        edges[path] = positions
    assigned = {}
    while edges:
        forced = {path: next(iter(options)) for path, options in edges.items() if len(options) == 1}
        if not forced or len(set(forced.values())) != len(forced):
            return {}
        assigned.update(forced)
        consumed = set(forced.values())
        edges = {path: options - consumed for path, options in edges.items() if path not in forced}
        if any(not options for options in edges.values()):
            return {}
    return {path: remote[position]["recording_id"] for path, position in assigned.items()}



def _material_recording_version(details):
    """Only distinguish versions that materially change the recording.

    Catalog disambiguations like "album version", "2012 release", and
    different MusicBrainz IDs do not establish different audible songs.
    """
    if not isinstance(details, dict):
        return frozenset()
    title = details.get("title") if isinstance(details.get("title"), str) else ""
    disambiguation = (details.get("disambiguation")
                      if isinstance(details.get("disambiguation"), str) else "")
    # Version words in the *base song title* are not necessarily version tags:
    # "Long Live Rock" is not a live recording.  Read parenthesized/bracketed
    # suffixes, dash suffixes, and provider disambiguations instead.
    qualifiers = re.findall(r"\(([^()]*)\)|\[([^\[\]]*)\]|\s[-–—]\s(.+)$", title)
    value = " ".join([disambiguation] + [part for group in qualifiers for part in group if part]).casefold()
    markers = {
        "live": r"\blive\b", "remix": r"\bremix\b",
        # Clean/explicit metadata does not distinguish the canonical musical
        # composition or track numbering. We must not claim to know lyrics.
        "acoustic": r"\bacoustic\b", "instrumental": r"\binstrumental\b",
        "demo": r"\bdemo\b", "radio edit": r"\bradio edit\b",
        "edit": r"\bedit\b", "mix": r"\bmix\b", "extended": r"\bextended\b",
        "remaster": r"\bremaster(?:ed)?\b", "mono": r"\bmono\b",
        "stereo": r"\bstereo\b", "karaoke": r"\bkaraoke\b",
        "re-recorded": r"\bre[- ]record(?:ed|ing)?\b",
    }
    return frozenset(name for name, pattern in markers.items() if re.search(pattern, value))


def _verified_title_album_cohort(audits, remote):
    """Map a whole album by unique canonical song names backed by acoustic hits.

    Some MusicBrainz releases use different *recording IDs* for the same song.
    This fallback ignores that identifier mismatch, not the fingerprint gate:
    EVERY audio file must have a verified candidate naming exactly one unique
    release track, and the complete release must have no duplicate titles.
    Local track numbers, local order, and source-title guesses are never used.
    """
    if len(remote) < 2 or len(remote) != len(audits):
        return {}
    titles = {}
    for position, row in remote.items():
        title = row.get("recording_title") or row.get("title")
        if not isinstance(title, str) or not title.strip():
            return {}
        key = _catalog_song_key(title)
        if not key or key in titles:
            return {}
        titles[key] = position
    resolved = {}
    on_release_ids = {row["recording_id"] for row in remote.values()}
    # Album-first context can rule out an unrelated live/instrumental *catalog*
    # appearance only when the remaining music already strongly identifies this
    # full edition.  It never creates a fingerprint match or overrides a
    # competing version listed on the very edition being tested.
    independent = sum(_accepted_recording(audit, independent=True) is not None
                      for audit in audits)
    strong_album_context = (len(remote) >= 4 and
                            independent >= max(3, math.ceil(len(remote) * .60)))
    for audit in audits:
        assessment = audit.get("assessment") or {}
        if assessment.get("complete") is not True or assessment.get("errors"):
            return {}
        # An on-release candidate which failed the normal fingerprint gates
        # is negative evidence: a different same-titled MBID cannot mask it.
        if any(isinstance(row, dict) and row.get("recording_id") in on_release_ids
               and row.get("status") != "verified"
               for row in assessment.get("candidates") or ()):
            return {}
        viable = []
        for row in assessment.get("candidates") or ():
            if (not isinstance(row, dict) or row.get("status") != "verified"
                    or (row.get("reasons") and
                        {"low_confidence", "duration_conflict"}.intersection(row["reasons"]))):
                continue
            candidate = row.get("details")
            if (not isinstance(candidate, dict) or candidate.get("id") != row.get("recording_id")
                    or not isinstance(candidate.get("title"), str)):
                continue
            position = titles.get(_catalog_song_key(candidate["title"]))
            if position is None:
                continue
            remote_row = remote[position]
            remote_version = _material_recording_version({
                "title": remote_row.get("title"),
                "disambiguation": remote_row.get("recording_disambiguation")})
            # Explicitly distinct live/clean/remix/etc. takes remain different
            # music, even if their displayed base titles match.
            if _material_recording_version(candidate) != remote_version:
                continue
            viable.append((position, candidate))
        positions = {position for position, _ in viable}
        if len(positions) != 1:
            return {}
        position = next(iter(positions))
        # A canonical album title does not identify which *version* of the
        # audio is present.  A live/clean/explicit/remix candidate remains a
        # veto even if a different on-release MBID also has a verified hit.
        # In particular this protects the real Strange Case clean/explicit
        # variants, not just the short synthetic regression cases.
        canonical_version = _material_recording_version({
            "title": remote[position].get("title"),
            "disambiguation": remote[position].get("recording_disambiguation")})
        # A different materially labeled version remains a real competitor.
        # The exception is narrow: a near-sample-exact, strongly fingerprinted
        # album take (<50ms) can exclude a different labeled take only when that
        # competitor is at least 250ms away.  Both must belong to the SAME
        # acoustically identified song.  Matching filenames or tag numbers do
        # not count, and close-timed instrumental/live alternatives still veto.
        same_version = [row for row in assessment.get("candidates") or ()
                        if isinstance(row, dict) and row.get("status") == "verified"
                        and isinstance(row.get("details"), dict)
                        and _material_recording_version(row["details"]) == canonical_version
                        and _catalog_song_key(row["details"].get("title")) in titles]
        near_exact = any(isinstance(row.get("duration_difference"), (int, float))
                         and abs(row["duration_difference"]) <= .05 for row in same_version)
        edition_supported = (strong_album_context and
            any(row.get("recording_id") in on_release_ids
                and isinstance(row.get("score"), (int, float)) and row["score"] >= .95
                and isinstance(row.get("duration_difference"), (int, float))
                and math.isfinite(row["duration_difference"])
                and abs(row["duration_difference"]) <= .20
                for row in same_version))
        for candidate_row in assessment.get("candidates") or ():
            if not isinstance(candidate_row, dict):
                continue
            if {"low_confidence", "duration_conflict"}.intersection(candidate_row.get("reasons") or ()):
                continue
            if candidate_row.get("status") not in {"verified", "probable"}:
                continue
            candidate_details = candidate_row.get("details")
            if not isinstance(candidate_details, dict):
                continue
            if _material_recording_version(candidate_details) != canonical_version:
                # A live/instrumental alternative elsewhere in the catalog
                # cannot veto an acoustically coherent studio album.  The
                # edition must have a strong independent majority and its own
                # exact-version recording must have a verified close timing.
                # Same-edition alternative takes still require human review.
                if (edition_supported and candidate_row.get("recording_id")
                        not in on_release_ids):
                    continue
                delta = candidate_row.get("duration_difference")
                # The version suffix itself does not change the underlying
                # song title.  Exclude only a clearly longer/shorter *same-song*
                # candidate, never a distinct composition or unknown take.
                other_title = candidate_details.get("title") or ""
                other_base = re.sub(r"\s*(?:\((?:radio )?edit\)|\(live\)|"
                                    r"\(instrumental\)|\(acoustic\)|\(remix\))\s*$",
                                    "", other_title, flags=re.I)
                canonical_title = remote[position].get("recording_title") or remote[position].get("title")
                if (near_exact and isinstance(delta, (int, float))
                        and math.isfinite(delta) and abs(delta) >= .25
                        and _catalog_song_key(other_base) == _catalog_song_key(canonical_title)):
                    continue
                return {}
        # An unmeasured but otherwise plausible recording cannot be disproved
        # from edition membership alone.  A complete album may nevertheless
        # choose its standard *song mapping* when TWO distinct, duration-
        # verified IDs independently corroborate that one song and version.
        # One verified ID + an untimed competitor remains unresolved.
        viable_ids = {details.get("id") for pos, details in viable if pos == position}
        if (any(isinstance(candidate_row, dict)
                and candidate_row.get("status") == "probable"
                and not {"low_confidence", "duration_conflict"}.intersection(
                    candidate_row.get("reasons") or ())
                and (not edition_supported or
                     candidate_row.get("recording_id") in on_release_ids)
                for candidate_row in assessment.get("candidates") or ())
                and len(viable_ids) < 2):
            return {}
        # Multiple verified MBIDs for the same named song are one mapping.
        selected = min((details for pos, details in viable if pos == position),
                       key=lambda details: (
                           0 if strong_album_context and details["id"] in on_release_ids else 1,
                           0 if re.search(r"\bexplicit\b|\buncensored\b",
                                          str(details.get("disambiguation") or ""), re.I) else 1,
                           str(details["id"])))
        path = Path(audit["path"])
        if path in resolved:
            return {}
        resolved[path] = (position, selected)
    if set(position for position, _ in resolved.values()) != set(remote):
        return {}
    return resolved


def _catalog_explicit(details):
    if not isinstance(details, dict):
        return False
    return bool(re.search(r"\bexplicit\b|\buncensored\b", " ".join(
        str(details.get(name) or "") for name in ("title", "disambiguation")), re.I))


def clean_explicit_catalog_equivalent(recordings):
    """True only for sound-compatible identical songs/credits labeled clean/explicit.

    An ordinary alternative MusicBrainz ID without an explicitness label is
    *not* silently collapsed; neither are edits, mixes or live versions.
    """
    if len(recordings) < 2 or not all(isinstance(item, dict) for item in recordings):
        return False
    titles = {_catalog_song_key(item.get("title")) for item in recordings}
    artists = {normalize_title(item.get("artist-credit-phrase") or "") for item in recordings}
    if len(titles) != 1 or "" in titles or len(artists) != 1 or "" in artists:
        return False
    if len({_material_recording_version(item) for item in recordings}) != 1:
        return False
    markers = set()
    for item in recordings:
        value = " ".join(str(item.get(name) or "") for name in ("title", "disambiguation"))
        explicit = bool(re.search(r"\bexplicit\b|\buncensored\b", value, re.I))
        clean = bool(re.search(r"\bclean\b|\bcensored\b", value, re.I))
        if explicit == clean:
            return False
        markers.add("explicit" if explicit else "clean")
    return markers == {"clean", "explicit"}


# A 0.95 AcoustID result remains the only *independent* recording identity.
# Weaker hits may corroborate one position in an already well-anchored album.
MIN_ALBUM_CONTEXT_SCORE = 0.75
MAX_ORPHAN_DURATION_DELTA = 0.5  # no fingerprint hit: unusually close timing needed


def _catalog_song_key(title):
    """Ignore only clean/explicit catalog suffixes, not audible edits or mixes."""
    if not isinstance(title, str):
        return ""
    value = re.sub(r"\s*(?:\((?:clean|explicit|censored|uncensored)\)|"
                   r"\[(?:clean|explicit|censored|uncensored)\]|"
                   r"[-–—]\s*(?:clean|explicit|censored|uncensored))\s*$",
                   "", title, flags=re.IGNORECASE)
    # Terminal song punctuation is not a distinct musical version: a release
    # can call it "Why Don't You Get a Job" or "Why Don't You Get a Job?".
    # Do NOT remove internal punctuation or audible version qualifiers.
    return normalize_title(value).rstrip(".?! ")


def _remote_audio_seconds(track):
    for field in ("length", "recording_length"):
        value = track.get(field)
        if isinstance(value, bool):
            continue
        try:
            milliseconds = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(milliseconds) and milliseconds > 0:
            return milliseconds / 1000
    return None


def _album_assisted_cohort(audits, remote, by_recording):
    """Second-pass one-to-one song assignment from independent album anchors.

    Uses no source title, filename, order, track/disc numbers or provider I/O.
    An otherwise unidentifiable *single* remainder can be placed only after
    every other song is acoustically accounted for and its time is within
    half a second of the sole remaining canonical track. All provider lookups
    must be complete. This maps a musical song, not an independently proven MBID.
    """
    count = len(audits)
    if count < 3 or count != len(remote) or len(by_recording) != count:
        return {}
    titles = {}
    for pos, item in remote.items():
        key = _catalog_song_key(item.get("recording_title") or item.get("title"))
        if not key or key in titles:
            return {}  # Repeated song names need direct position evidence.
        titles[key] = pos
    anchor_count = sum(_accepted_recording(a, independent=True) is not None
                       for a in audits)
    if anchor_count < max(2, math.ceil(count * 0.4)):
        return {}

    edges, missing, durations = {}, [], {}
    for audit in audits:
        path = Path(audit["path"])
        assessment = audit.get("assessment") or {}
        evidence = audit.get("evidence") or {}
        if (assessment.get("complete") is not True or assessment.get("errors")
                or assessment.get("release_id") or path in edges):
            return {}
        fingerprint = evidence.get("fingerprint") or {}
        seconds = fingerprint.get("duration")
        try:
            seconds = float(seconds)
        except (TypeError, ValueError, OverflowError):
            return {}
        if not math.isfinite(seconds) or seconds <= 0:
            return {}
        durations[path] = seconds
        candidates = assessment.get("candidates") or ()
        viable = set()
        # First use fully hydrated, audio-viable candidates (regardless of
        # which MBID the chosen pressing happens to use).
        for row in candidates:
            if not isinstance(row, dict):
                return {}
            score = row.get("score")
            if row.get("status") == "probable" and "low_confidence" not in (row.get("reasons") or ()):
                return {}  # An untimed high-score competitor cannot be dismissed.
            if row.get("status") != "verified":
                continue
            details = row.get("details")
            if not isinstance(details, dict) or details.get("id") != row.get("recording_id"):
                return {}
            pos = titles.get(_catalog_song_key(details.get("title")))
            if pos is None and row.get("recording_id") in by_recording:
                positions = by_recording[row["recording_id"]]
                pos = next(iter(positions)) if len(positions) == 1 else None
            if pos is not None:
                expected = _remote_audio_seconds(remote[pos])
                actual_version = _material_recording_version(details)
                canonical_version = _material_recording_version({
                    "title": remote[pos].get("title"),
                    "disambiguation": remote[pos].get("recording_disambiguation")})
                if actual_version != canonical_version:
                    return {}  # Live/radio/remix conflicts stay in review.
                if expected is not None and abs(seconds - expected) <= 3:
                    viable.add(pos)
            elif _material_recording_version(details):
                # If the provider advertises a plausible different version,
                # don't turn its absence from this pressing into negative audio evidence.
                return {}
        if not viable:
            # AcoustID can supply title and MBID even for scores below .95.
            # The first-pass evidence is retained; no second network scan.
            raw_titles = {}
            for match in evidence.get("matches") or ():
                if not isinstance(match, dict):
                    continue
                raw_score = match.get("score")
                if not isinstance(raw_score, (int, float)) or not math.isfinite(raw_score):
                    continue
                if not MIN_ALBUM_CONTEXT_SCORE <= raw_score < 0.95:
                    continue
                for recording in match.get("recordings") or ():
                    if isinstance(recording, dict) and recording.get("id"):
                        raw_titles.setdefault(recording["id"], set()).add(
                            _catalog_song_key(recording.get("title")))
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                score = row.get("score")
                if not isinstance(score, (int, float)) or not MIN_ALBUM_CONTEXT_SCORE <= score < 0.95:
                    continue
                identifier = row.get("recording_id")
                positions = set(by_recording.get(identifier, ()))
                positions.update(titles[name] for name in raw_titles.get(identifier, ()) if name in titles)
                for pos in positions:
                    expected = _remote_audio_seconds(remote[pos])
                    if expected is not None and abs(seconds - expected) <= 3:
                        viable.add(pos)
        if viable:
            edges[path] = viable
        else:
            # No usable candidate is eligible only as the *single* remaining
            # audio item. Strong contradictory candidates must never be guessed.
            if any(isinstance(row, dict) and isinstance(row.get("score"), (int, float))
                   and row["score"] >= MIN_ALBUM_CONTEXT_SCORE for row in candidates):
                return {}
            missing.append(path)
    if len(missing) > 1:
        return {}
    assigned = {}
    while edges:
        singles = {path: next(iter(options)) for path, options in edges.items() if len(options) == 1}
        if not singles or len(set(singles.values())) != len(singles):
            return {}
        assigned.update(singles)
        consumed = set(singles.values())
        edges = {path: options - consumed for path, options in edges.items() if path not in singles}
        if any(not options for options in edges.values()):
            return {}
    remaining = set(remote) - set(assigned.values())
    if missing:
        if len(remaining) != 1:
            return {}
        path, pos = missing[0], next(iter(remaining))
        expected = _remote_audio_seconds(remote[pos])
        if expected is None or abs(durations[path] - expected) > MAX_ORPHAN_DURATION_DELTA:
            return {}
        assigned[path] = pos
    if set(assigned) != {Path(a["path"]) for a in audits} or set(assigned.values()) != set(remote):
        return {}
    return {path: (pos, remote[pos]["recording_id"]) for path, pos in assigned.items()}

def _musical_song_signature(report):
    """Ordered musical sequence, ignoring provider IDs and physical disc splits."""
    by_position = {_position(row.get("disc"), row.get("number")): row
                   for row in report.get("tracklist") or () if isinstance(row, dict)}
    order = sorted((pos, path) for pos, path in (report.get("matched") or {}).items()
                   if pos in by_position)
    return tuple((index, str(Path(path)), _catalog_song_key(by_position[pos].get("title") or ""),
                  tuple(sorted(_material_recording_version({
                      "title": by_position[pos].get("title"),
                      "disambiguation": by_position[pos].get("recording_disambiguation")}))))
                 for index, (pos, path) in enumerate(order))


def evaluate_release_candidate(audits, release_id, *, local_tracks=(), manual_mapping=None):
    """Assess musical coverage, never approve a library import or exact edition.

    ``matched`` keys are positive integer (disc, number) pairs. ``manual_mapping``
    maps Path -> (disc, number), restricting existing audio-supported edges only;
    it cannot supply identity, relax thresholds, or approve metadata conflicts.
    Returns status ('complete', 'partial', 'conflicting'), matched, missing (remote
    track dicts), review (Paths), reasons (diagnostic codes), release_id, tracklist,
    and errors (provider-error dicts). Partial/conflicting reports preserve safe
    matches but never authorize import. local_tracks is an optional full metadata
    inventory: missing audits and existing numbering/total conflicts block complete.
    Album/artist naming, artwork, exact edition and approval remain caller gates.

    Legacy preliminary_match audits are trusted at their existing API boundary.
    Ambiguous audits normally need two other distinct, context-free anchors.
    A fully accounted album can instead use a unique full assignment of provider-
    verified fingerprint candidates for EVERY song; canonical recording titles
    corroborate those acoustic edges but never create them. Unresolved probable
    candidates and audio-viable different versions veto automatic mapping.
    A complete report proves compatibility with ONE release, not absence of a
    competing musical sequence: callers must compare other candidate reports.
    ``retryable`` and per-error retryability diagnose outages, not permission.
    """
    audits = list(audits)
    paths = [Path(audit["path"]) for audit in audits]
    report = {"status": "partial", "matched": {}, "missing": [], "review": [],
              "reasons": [], "release_id": release_id, "tracklist": [], "errors": [], "retryable": False}
    reasons, review = report["reasons"], set()
    for audit, path in zip(audits, paths):
        errors = (audit.get("assessment") or {}).get("errors") or (audit.get("evidence") or {}).get("errors", [])
        if errors:
            review.add(path)
            reasons.append("recording_lookup_incomplete")
            report["errors"].extend({**deepcopy(error), "path": str(path)} for error in errors)
    report["retryable"] = any(error.get("retryable") is True for error in report["errors"])
    if report["retryable"]:
        reasons.append("retryable_provider_error")
    try:
        tracklist = get_release_tracklist(release_id)
        if not isinstance(tracklist, list):
            raise ValueError("Release tracklist must be a list.")
        report["tracklist"] = deepcopy(tracklist)
    except Exception as error:
        if isinstance(error, AssertionError):
            raise
        failure = provider_error_evidence("release_tracklist", error)
        report["errors"].append(failure)
        report["retryable"] = report["retryable"] or failure["retryable"]
        report["review"] = sorted(set(paths), key=str)
        reasons.append("release_lookup_unavailable")
        if failure["retryable"] and "retryable_provider_error" not in reasons:
            reasons.append("retryable_provider_error")
        return report
    remote, by_recording = {}, defaultdict(set)
    invalid = False
    for track in tracklist:
        position = _position(track.get("disc"), track.get("number")) if isinstance(track, dict) else None
        identifier = track.get("recording_id") if isinstance(track, dict) else None
        if position is None or position in remote or not isinstance(identifier, str) or not identifier:
            invalid = True
            continue
        remote[position] = track
        by_recording[identifier].add(position)
    if invalid or len(set(paths)) != len(paths):
        report.update(status="conflicting", missing=deepcopy(tracklist), review=sorted(set(paths), key=str))
        reasons.append("invalid_release_positions_or_identity" if invalid else "duplicate_local_path")
        return report
    if not tracklist:
        report["review"] = sorted(set(paths), key=str)
        reasons.append("empty_release_tracklist")
        return report
    remote_complete = getattr(tracklist, "complete", True) is True
    if not remote_complete:
        reasons.append("remote_tracklist_incomplete")
        review.update(paths)
    independent = {}
    for audit, path in zip(audits, paths):
        details = _accepted_recording(audit, independent=True)
        if details and details["id"] in by_recording:
            independent[path] = details["id"]

    cohort = {}
    # Complete album identity can also come from verified fingerprint hits
    # with the same *song titles* as a release, even if MB assigned distinct IDs.
    # The fallback is for off-release MBID aliases, not a way around the
    # existing on-release audio/version constraints or an incomplete audit.
    needs_alias = any(not (any(isinstance(row, dict) and row.get("status") == "verified"
                               and row.get("recording_id") in by_recording
                               for row in (audit.get("assessment") or {}).get("candidates") or ())
                           or (_accepted_recording(audit) or {}).get("id") in by_recording)
                      for audit in audits)
    # A fully verified album can still have *ambiguous* AcoustID/MB recording
    # identities even when every song has at least one on-release candidate.
    # The old needs_alias gate wrongly skipped the verified-title matcher in
    # precisely that case, leaving an 11/11 cohort at zero matched files.
    # Permit it only for full albums; the matcher itself requires one unique
    # acoustically VERIFIED song-title edge for every file, identical material
    # version evidence, and a complete one-to-one assignment. Text tags alone
    # can never create a mapping.
    all_ambiguous_with_on_release_hits = bool(audits) and all(
        _accepted_recording(audit, independent=True) is None and
        any(isinstance(candidate, dict) and candidate.get("status") == "verified"
            and candidate.get("recording_id") in by_recording
            for candidate in (audit.get("assessment") or {}).get("candidates") or ())
        for audit in audits)
    # Full acoustically mapped song sequences deserve evaluation even when
    # some MB IDs belong directly to the release.  The old needs_alias gate
    # incorrectly skipped this path for mixed matched/ambiguous albums.
    title_cohort = (_verified_title_album_cohort(audits, remote)
                    if remote_complete and len(paths) == len(remote) and not report["errors"] else {})
    if remote_complete and len(paths) >= 3 and len(paths) == len(remote) and not report["errors"]:
        cohort = _whole_album_fingerprint_cohort(audits, paths, remote, by_recording)
        # A full combinatorial assignment is not acoustic version evidence.
        # Veto the entire self-supporting cohort if any selected ID has a viable
        # different version; the other uncertain files cannot anchor each other.
        for audit, path in zip(audits, paths):
            identifier = cohort.get(path)
            if identifier and _accepted_recording(audit, independent=True) is None:
                options = audit["assessment"]["candidates"]
                selected = next(row["details"] for row in options if row.get("recording_id") == identifier
                                and row.get("status") == "verified")
                if (unresolved_recording_version(selected, options)
                        or _release_version_unconfirmed(selected, by_recording[identifier], remote)):
                    # Title-cohort has already checked the selected pressing's
                    # recording/version compatibility in its own album context.
                    # Preserve the older cohort diagnostic only if that stricter
                    # complete mapping could not be established.
                    if not title_cohort:
                        reasons.append("unresolved_recording_version")
                    cohort = {}
                    break
    album_context = (_album_assisted_cohort(audits, remote, by_recording)
                     if remote_complete and len(paths) == len(remote) and not report["errors"]
                     and not cohort and not title_cohort else {})
    manual = {}
    conflicting = False
    if manual_mapping is not None:
        if not isinstance(manual_mapping, dict):
            conflicting = True
            reasons.append("invalid_manual_mapping")
        else:
            for path, value in manual_mapping.items():
                try:
                    path = Path(path)
                    position = _position(*value) if isinstance(value, (tuple, list)) and len(value) == 2 else None
                except (TypeError, ValueError):
                    position = None
                if path not in paths or position not in remote:
                    conflicting = True
                    reasons.append("invalid_manual_mapping")
                    if path in paths:
                        review.add(path)
                else:
                    manual[path] = position
    edges, identities = {}, {}
    for audit, path in zip(audits, paths):
        details = _accepted_recording(audit)
        identifier = details["id"] if details else None
        assessment = audit.get("assessment") or {}
        # Prefer exact MusicBrainz identity, but do not require its ID to be
        # identical when a verified acoustic hit maps to one unique song title.
        if path in title_cohort and identifier not in by_recording and path not in cohort:
            position, supported = title_cohort[path]
            identifier = remote[position]["recording_id"]
            report.setdefault("title_matched_candidates", {})[str(path)] = supported
            reasons.append("resolved_by_audio_verified_title")
        if path in album_context and identifier not in by_recording:
            position, representative_id = album_context[path]
            identifier = representative_id
            report.setdefault("album_context_paths", []).append(str(path))
            reasons.append("resolved_by_album_context")
        if identifier is None:
            # The target track cannot vote for its own release context.
            anchors = {value for anchor, value in independent.items() if anchor != path}
            if len(anchors) >= 2 and remote_complete:
                eligible = _context_candidates(assessment, by_recording)
                if len(eligible) == 1:
                    proposed = next(iter(eligible))
                    candidates = assessment["candidates"]
                    selected = next(row["details"] for row in candidates if row.get("recording_id") == proposed
                                    and row.get("status") == "verified")
                    if (unresolved_recording_version(selected, candidates)
                            or _release_version_unconfirmed(selected, by_recording[proposed], remote)):
                        reasons.append("unresolved_recording_version")
                    else:
                        identifier = proposed
                        reasons.append("resolved_by_independent_anchors")
            if identifier is None and path in cohort:
                identifier = cohort[path]
                reasons.append("resolved_by_whole_album_fingerprints")
            if identifier is None and path in album_context:
                position, identifier = album_context[path]
                report.setdefault("album_context_paths", []).append(str(path))
                reasons.append("resolved_by_album_context")
            if identifier is None:
                review.add(path)
                reasons.append("unresolved_recording_identity")
                continue
        positions = set(by_recording.get(identifier, ()))
        if not positions:
            conflicting = True
            reasons.append("recording_not_on_release")
            review.add(path)
            continue
        if path in album_context:
            supported_position = album_context[path][0]
            if supported_position not in positions:
                conflicting = True
                reasons.append("album_context_position_conflict")
                review.add(path)
                continue
            positions = {supported_position}
        if path in manual:
            if manual[path] not in positions:
                conflicting = True
                reasons.append("invalid_manual_mapping")
                review.add(path)
                continue
            positions = {manual[path]}
        edges[path], identities[path] = positions, identifier
    # Reject over-subscribed recording groups without choosing the first file.
    for identifier in set(identities.values()):
        owners = [path for path, value in identities.items() if value == identifier]
        if len(owners) > len(by_recording[identifier]):
            conflicting = True
            reasons.append("duplicate_local_recording_claim")
            for path in owners:
                review.add(path)
                edges.pop(path, None)
    matched = report["matched"]
    while edges:
        singles = defaultdict(list)
        for path, positions in edges.items():
            if len(positions) == 1:
                singles[next(iter(positions))].append(path)
        if not singles:
            break
        consumed = set(singles)
        for position, owners in singles.items():
            if len(owners) > 1:
                conflicting = True
                reasons.append("duplicate_position_claim")
                review.update(owners)
            else:
                matched[position] = owners[0]
            for path in owners:
                edges.pop(path)
        for path in list(edges):
            edges[path] -= consumed
            if not edges[path]:
                conflicting = True
                reasons.append("duplicate_position_claim")
                review.add(path)
                del edges[path]
    if edges:
        review.update(edges)
        reasons.append("ambiguous_position_mapping")
    local_tracks = list(local_tracks)
    local = {Path(track["path"]): track for track in local_tracks}
    if len(local) != len(local_tracks):
        conflicting = True
        reasons.append("duplicate_local_metadata")
        review.update(local)
    unaudited = set(local) - set(paths)
    if unaudited:
        review.update(unaudited)
        reasons.append("missing_audio_audit")
    for position, path in matched.items():
        conflicts = _local_number_conflicts(local[path], position, remote) if path in local else []
        if conflicts:
            conflicting = True
            reasons.extend(conflicts)
            review.add(path)
    report["missing"] = [deepcopy(track) for position, track in remote.items() if position not in matched]
    if report["missing"]:
        reasons.append("missing_release_tracks")
    complete = (bool(paths) and len(matched) == len(paths) == len(remote)
                and not review and remote_complete and not report["errors"])
    report["status"] = "conflicting" if conflicting else "complete" if complete else "partial"
    report["review"] = sorted(review, key=str)
    report["reasons"] = list(dict.fromkeys(reasons))
    return report


def _release_may_cover_album(audits, release_id):
    """Only exhaustive candidate memberships may rule out an unchecked release."""
    for audit in audits:
        accepted = _accepted_recording(audit, independent=True)
        assessment = audit.get("assessment") or {}
        if accepted is not None:
            candidates = [accepted]
        elif assessment.get("complete") is True and not assessment.get("errors") and not assessment.get("release_id"):
            candidates = [row.get("details") for row in assessment.get("candidates", ())
                          if isinstance(row, dict) and not {"low_confidence", "duration_conflict"}.intersection(row.get("reasons") or ())]
        else:
            continue  # Unknown evidence cannot establish absence.
        if candidates and all(isinstance(candidate, dict) and candidate.get("release-list-complete") is True
                              and isinstance(candidate.get("release-list"), list)
                              and not any(isinstance(row, dict) and row.get("id") == release_id
                                          for row in candidate["release-list"]) for candidate in candidates):
            return False
    return True


def _verified_standard_superset(candidate, complete):
    """Known bonus/retail edition adds no contradictory *music* to a full album.

    A tracklist can be ignored as a competing edition ONLY if its opening
    musical sequence exactly matches an already complete, acoustically mapped
    reference, including live/remix/clean/explicit version qualifiers. No
    source tag, fuzzy title, missing provider response, or partially understood
    alternate mix can qualify. The candidate contributes no metadata itself.
    """
    tracks = candidate.get("tracklist") or ()
    reference = complete.get("tracklist") or ()
    if (not reference or len(tracks) < len(reference)
            or candidate.get("errors") or getattr(tracks, "complete", True) is not True):
        return False
    # The reference already established the complete musical version for all
    # supplied files. A bonus edition with the same musical prefix cannot veto
    # that reference merely because its own partial evaluator re-encountered
    # an off-release alternate MB recording for the final source track.
    unsafe = {"recording_not_on_release",
              "duplicate_local_recording_claim", "invalid_manual_mapping",
              "retryable_provider_error", "release_lookup_unavailable",
              "remote_tracklist_incomplete"}
    if unsafe.intersection(candidate.get("reasons") or ()):
        return False

    def song(track):
        if not isinstance(track, dict):
            return None
        name = _catalog_song_key(track.get("title") or track.get("recording_title") or "")
        if not name:
            return None
        version = tuple(sorted(_material_recording_version({
            "title": track.get("title"),
            "disambiguation": track.get("recording_disambiguation")})))
        return name, version

    parent = tuple(song(track) for track in reference)
    prefix = tuple(song(track) for track in tracks[:len(reference)])
    if (None in parent or parent != prefix or
            len(set(parent)) != len(parent)):
        return False
    # If the retailer's report did map any files, they cannot contradict the
    # reference's audio-backed source-to-song assignment.
    original_remote = {_position(track.get("disc"), track.get("number")): song(track)
                       for track in reference if isinstance(track, dict)}
    original_by_path = {Path(path): original_remote.get(position)
                        for position, path in (complete.get("matched") or {}).items()}
    other_remote = {_position(track.get("disc"), track.get("number")): song(track)
                    for track in tracks if isinstance(track, dict)}
    for position, path in (candidate.get("matched") or {}).items():
        if (Path(path) not in original_by_path or
                original_by_path[Path(path)] != other_remote.get(position)):
            return False
    return True


def compare_release_candidate_reports(audits, evaluations, *, unexamined_release_ids=(),
                                      standard_album_preference=False):
    """Pure full-album consensus gate; never choose the first compatible release.

    Use AFTER bounded evaluation and BEFORE selecting a full automatic album.
    Disc boundaries and source numbers supply no identity votes. Different
    recording IDs, file assignments, ordered songs or provider version evidence
    conflict. Unknown competitors remain blockers unless complete memberships
    independently prove they cannot cover every supplied audio file. Partial
    album/edition/import policy remains the caller's responsibility.
    """
    audits, evaluations = list(audits), list(evaluations)
    paths = {Path(audit["path"]) for audit in audits}
    signatures, errors = {}, []
    # Retain nominated competitors even if a caller forgets to pass its budget
    # remainder. Discovery is provider-free and grants no positive authority.
    unresolved = set(unexamined_release_ids) | set(discover_release_candidates(audits))
    membership_unknown = [Path(audit["path"]) for audit in audits
                          if _accepted_recording(audit, independent=True) is None
                          and any(isinstance(row, dict) and row.get("status") == "verified"
                                  and (not isinstance(row.get("details"), dict)
                                       or row["details"].get("release-list-complete") is not True)
                                  for row in (audit.get("assessment") or {}).get("candidates", ()))]
    numbering = {f"local_{field}_conflict" for field in ("track_number", "track_total", "disc_number", "disc_total")}
    numbering.update(f"invalid_local_{field}" for field in ("track_number", "track_total", "disc_number", "disc_total"))
    allowed = numbering | {"resolved_by_independent_anchors", "resolved_by_whole_album_fingerprints",
                           "resolved_by_audio_verified_title", "resolved_by_album_context"}
    unknown_reasons = {"release_lookup_unavailable", "remote_tracklist_incomplete", "empty_release_tracklist",
                       "invalid_release_positions_or_identity", "unresolved_recording_identity",
                       "unresolved_recording_version", "ambiguous_position_mapping"}
    # A standard US digital preference must still have a complete, uniquely
    # verified album to anchor any consideration of bonus-track supersets.
    # Multi-edition disagreements cannot be erased by a format preference.
    complete_reference = None
    if standard_album_preference and len(paths) >= 3:
        for item in evaluations:
            records = item.get("tracklist") or ()
            if ((item.get("status") == "complete" or
                 set(item.get("reasons") or ()) <= allowed)
                    and len(records) == len(paths)
                    and len(item.get("matched") or {}) == len(paths)
                    and getattr(records, "complete", True) is True
                    and not item.get("errors")):
                complete_reference = item
                break
    for report in evaluations:
        identifier = report.get("release_id")
        unresolved.update(report.get("unexamined_release_ids") or ())
        errors.extend(deepcopy(report.get("errors") or ()))
        if not identifier:
            continue
        unresolved.discard(identifier)
        tracks, matched = report.get("tracklist") or (), report.get("matched") or {}
        remote = {_position(row.get("disc"), row.get("number")): row for row in tracks if isinstance(row, dict)}
        full = (bool(paths) and len(paths) == len(audits) == len(tracks) == len(matched) == len(remote)
                and None not in remote and set(matched) == set(remote)
                and {Path(path) for path in matched.values()} == paths
                and getattr(tracks, "complete", True) is True and not report.get("errors")
                and (report.get("status") == "complete" or set(report.get("reasons") or ()) <= allowed))
        if full:
            # Different MusicBrainz recording IDs are not different songs when
            # a complete, audio-backed release maps the same song to each file.
            # Materially different version markers and song/order changes still
            # make the reports incompatible.
            signatures[identifier] = _musical_song_signature(report)
        elif (report.get("errors") or getattr(tracks, "complete", True) is not True
              or unknown_reasons.intersection(report.get("reasons") or ())):
            if (complete_reference is not None and
                    _verified_standard_superset(report, complete_reference)):
                unresolved.discard(identifier)
            else:
                unresolved.add(identifier)
    unresolved = {identifier for identifier in unresolved if _release_may_cover_album(audits, identifier)}
    reasons = []
    if len(set(signatures.values())) > 1:
        reasons.append("competing_album_recording_sequences")
    if unresolved:
        reasons.append("album_competitors_unexamined_or_incomplete")
    if membership_unknown:
        reasons.append("recording_membership_incomplete")
    if not signatures:
        reasons.append("no_complete_album_mapping")
    return {"status": "conflicting" if len(set(signatures.values())) > 1 else
                      "complete" if signatures and not unresolved and not membership_unknown else "partial",
            "compatible_release_ids": sorted(signatures), "signatures": signatures,
            "unresolved_release_ids": sorted(unresolved), "membership_incomplete_paths": sorted(membership_unknown, key=str),
            "reasons": reasons, "errors": errors,
            "retryable": any(error.get("retryable") is True for error in errors)}


def match_album_to_release(audits, release_id):
    """Legacy tuple API retaining the release's original position key types."""
    report = evaluate_release_candidate(audits, release_id)
    matched = {}
    for track in report["tracklist"]:
        if not isinstance(track, dict):
            continue
        position = _position(track.get("disc"), track.get("number"))
        if position in report["matched"]:
            matched[(track["disc"], track["number"])] = report["matched"][position]
    review = list(report["review"])
    if report["status"] != "complete" and not review and not report["missing"]:
        review = list(report["matched"].values())
    return matched, review, report["missing"]

# Normalize formatting without removing version information
def normalize_title(title):
    # Standardize typographic hyphens and apostrophes
    title = title.translate(str.maketrans({
        "\u2010": "-",  # Hyphen
        "\u2011": "-",  # Nonbreaking hyphen
        "\u2013": "-",  # En dash
        "\u2014": "-",  # Em dash
        "\u2026": "...",  # Typographic ellipsis
        "\u2018": "'",
        "\u2019": "'",
    }))

    title = " ".join(title.casefold().split())

    # Preserve our existing featured-credit comparison rule
    title = re.sub(
        r"\s*\(feat\.?\s+[^)]*\)",
        "",
        title,
        flags=re.IGNORECASE
    )

    # Treat "(name remix)" and " - name remix" consistently.
    # Keep the full version label so different mixes stay distinct.
    title = re.sub(
        r"\s+\(([^()]+\b(?:remix|mix|version))\)$",
        r" - \1",
        title
    )

    return " ".join(title.split())


# Find an artist to use when searching MusicBrainz
def identify_artist(tracks):
    album_artists = {
        track["album_artist"][0].strip()
        for track in tracks
        if track["album_artist"] and track["album_artist"][0].strip()
    }

    # Prefer a consistent album artist
    if len(album_artists) == 1:
        return next(iter(album_artists))

    # Conflicting album artists require review
    if len(album_artists) > 1:
        return None

    # Fall back to individual track artists
    artists = [
        track["artist"][0].strip()
        for track in tracks
        if track["artist"] and track["artist"][0].strip()
    ]
    if not artists:
        return None
    if len(set(artists)) == 1:
        return artists[0]

    # A strict majority can provide an unverified search hint
    artist_counts = Counter(artists)
    candidate, count = artist_counts.most_common(1)[0]
    if count > len(tracks) / 2:
        print(f"Using search hint: {candidate} (unverified)")
        return candidate
    return None


# Identify an album using its existing metadata
def identify_album(tracks):
    albums = {
        track["album"][0].strip()
        for track in tracks
        if track["album"] and track["album"][0].strip()
    }

    # The album name must be consistent
    if len(albums) != 1:
        return None
    artist = identify_artist(tracks)
    if artist is None:
        return None
    return artist, next(iter(albums))

# Use the shared grouping maintained by tag.py
def group_staged_albums():
    return tag.group_staged_albums()

# Compare title collections without verifying the release or track order
def compare_track_titles(local_tracks, release_tracks):
    local_titles = []
    for track in local_tracks:
        titles = track.get("title", ())
        if not titles or not titles[0].strip():
            return "Needs review: missing local track title."
        local_titles.append(normalize_title(titles[0]))
    remote_titles = []
    for track in release_tracks:
        title = track.get("title")
        if not title or not title.strip():
            return "Needs review: missing MusicBrainz track title."
        remote_titles.append(normalize_title(title))
    local_counts = Counter(local_titles)
    remote_counts = Counter(remote_titles)
    if local_counts == remote_counts:
        return "Title collection matches; order and edition unverified."
    local_only = local_counts - remote_counts
    release_only = remote_counts - local_counts
    lines = [
        f"{sum(local_only.values())} local-only / "
        f"{sum(release_only.values())} release-only"
    ]

    for label, differences in (
        ("Local titles", local_only),
        ("MusicBrainz titles", release_only),
    ):
        if not differences:
            continue
        lines.append(f"{label}:")
        for title, count in sorted(differences.items()):
            suffix = f" ×{count}" if count > 1 else ""
            lines.append(f"  {title}{suffix}")
    return "\n".join(lines)

# Compare titles at their numbered positions
def compare_track_positions(local_tracks, release_tracks):
    remote = {}

    for track in release_tracks:
        try:
            position = (int(track["disc"]), int(track["number"]))
        except (ValueError, TypeError, KeyError):
            return "Positions need review: nonnumeric release numbering."
        if min(position) < 1 or position in remote:
            return "Positions need review: invalid or duplicate release position."
        remote[position] = normalize_title(track["title"])
    if not remote:
        return "Positions need review: empty release tracklist."
    remote_discs = {disc for disc, number in remote}
    local = {}
    assumed_disc = False

    for track in local_tracks:
        number, _ = tag.parse_track_number(track["track_number"])
        disc, _ = tag.parse_disc_number(track["disc_number"])
        if number is None:
            return "Positions unchecked: local track numbers missing."
        if disc is None:
            if remote_discs != {1}:
                return "Positions unchecked: local disc numbers missing."
            disc = 1
            assumed_disc = True
        position = (disc, int(number))
        if min(position) < 1 or position in local:
            return "Positions need review: invalid or duplicate local position."
        titles = track.get("title", ())
        if not titles or not titles[0].strip():
            return "Positions unchecked: local title missing."
        local[position] = normalize_title(titles[0])
    if local.keys() != remote.keys():
        return "Disc/track positions differ."
    differences = sum(
        local[position] != remote[position]
        for position in local
    )
    if differences:
        return f"Titles differ at {differences} numbered position(s)."
    if assumed_disc:
        return "Positions match assuming untagged discs are disc 1."

    return "Disc/track positions and titles match."

# Preview missing numbers when matching candidates agree
def preview_missing_numbers(local_tracks, candidates):
    missing = [
        track for track in local_tracks
        if not track["track_number"]
    ]
    if not missing:
        return []

    matching = [
        candidate for candidate in candidates
        if candidate["same_album"]
        and candidate["titles_match"]
        and candidate.get("artist_name_matches", False)
    ]

    if not matching:
        print("\n  Numbering preview: no matching candidates.")
        return []
    candidate_maps = []
    for candidate in matching:
        positions = {}
        used_positions = set()
        for track in candidate["tracks"]:
            title = normalize_title(track["title"])
            try:
                position = (int(track["disc"]), int(track["number"]))
            except (ValueError, TypeError, KeyError):
                print("\n  Numbering preview needs review: release numbering.")
                return []
            if (
                min(position) < 1
                or title in positions
                or position in used_positions
            ):
                print("\n  Numbering preview needs review: ambiguous tracklist.")
                return []
            positions[title] = position
            used_positions.add(position)
        candidate_maps.append(positions)
    print(
        f"\n  Missing-number preview "
        f"({len(matching)} title-matching candidates):"
    )

    proposals = []

    for track in missing:
        title = normalize_title(track["title"][0])
        positions = {
            mapping.get(title) for mapping in candidate_maps
        }
        if None in positions or len(positions) != 1:
            print(f"    {track['path'].name}: candidates disagree")
            continue
        disc, number = positions.pop()
        existing_disc, _ = tag.parse_disc_number(track["disc_number"])
        if track["disc_number"] and existing_disc != disc:
            print(f"    {track['path'].name}: existing disc needs review")
            continue

        proposals.append({
            "path": track["path"],
            "field": "track_number",
            "value": f"{number:02d}",
            "verified": False,
            "source": "musicbrainz_candidate_consensus",
            "release_ids": [
                candidate["release_id"] for candidate in matching
            ],
        })

        print(
            f"    {track['path'].name}: "
            f"missing → track {number:02d} (candidate disc {disc})"
        )

    print("  Preview only; artist identity and exact edition still need review.")
    return proposals

# Test MusicBrainz searching on grouped staged albums
if __name__ == "__main__":
    albums = group_staged_albums()
    for (import_name, album_key), album_data in albums.items():
        tracks = album_data["tracks"]
        print(f"\nImport: {import_name}")
        print(f"Album: {album_data['album']}")
        print(f"Local tracks: {len(tracks)}")

        # Summarize local numbering without assuming missing disc tags
        local_discs = {}
        for track in tracks:
            disc, _ = tag.parse_disc_number(track["disc_number"])
            number, _ = tag.parse_track_number(track["track_number"])
            disc_label = str(disc) if disc is not None else "untagged"
            local_discs.setdefault(disc_label, []).append(
                int(number) if number is not None else None
            )

        for disc_label, numbers in local_discs.items():
            known = sorted(n for n in numbers if n is not None)
            missing = numbers.count(None)
            sequence = ", ".join(map(str, known)) or "none"
            print(f"  Local disc {disc_label}: tracks {sequence}")
            if missing:
                print(f"    Missing track numbers: {missing}")

        identity = identify_album(tracks)
        if identity is None:
            print("Album identity needs review.")
            for track in tracks:
                print(f"  File: {track['path'].name}")
                print(f"    Artist: {track['artist']}")
                print(f"    Album artist: {track['album_artist']}")
                print(f"    Album: {track['album']}")
            continue

        artist, album = identity


        # Temporarily test full-album discovery
        if album == "NEED":
            candidates = find_album_candidates(artist, tracks)

            print("\nAlbum discovery candidates:")
            for release_id, count in candidates.most_common(5):
                print(f"  {count}/{len(tracks)} tracks: {release_id}")


        print(f"Searching: {artist} - {album}")
        matches = search_album(artist, album)
        print(f"Found {len(matches)} possible releases.")
        debug = False
        candidate_results = []
        for index, match in enumerate(matches, start=1):
            release_id = match["id"]
            release_tracks = get_release_tracklist(release_id)
            title = match.get("title", "Unknown")
            artist_credit = match.get("artist-credit-phrase")
            if not artist_credit:
                artist_credit = "Unknown artist"

            artist_name_matches = (
                " ".join(artist_credit.casefold().split())
                == " ".join(artist.casefold().split())
            )

            date = match.get("date") or "Undated"
            country = match.get("country") or "Unknown region"
            disc_count = len({
                track["disc"] for track in release_tracks
            })
            comparison = compare_track_titles(tracks, release_tracks)
            summary, *details = comparison.splitlines()

            if summary.startswith("Title collection matches"):
                summary = "Titles match"
            print(
                f"\n  {index}. {artist_credit} — {title} | {date} | {country}\n"
                f"     {len(release_tracks)} tracks · "
                f"{disc_count} disc(s) | {summary}"
            )

            # Show details for close candidates, or all in debug mode
            same_album = (
                " ".join(title.casefold().split())
                == " ".join(album.casefold().split())
            )
            same_count = len(tracks) == len(release_tracks)

            if debug or (same_album and same_count):
                for line in details:
                    print(f"     {line}")
            titles_match = summary == "Titles match"
            position_result = None

            if titles_match:
                position_result = compare_track_positions(
                    tracks, release_tracks
                )
                print(f"     {position_result}")

            # Retain evidence for later candidate review and proposals
            candidate_results.append({
                "release_id": release_id,
                "title": title,
                "same_album": same_album,
                "tracks": release_tracks,
                "titles_match": titles_match,
                "position_result": position_result,
                "artist_credit": artist_credit,
                "artist_name_matches": artist_name_matches,
            })

            if debug:
                print(f"     Release ID: {release_id}")

        numbering_proposals = preview_missing_numbers(
            tracks, candidate_results
        )
        print("\n  Comparisons only; exact edition remains unverified.")
