import musicbrainzngs
import re
import tag
from collections import Counter
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from functools import lru_cache

# Identify MIXIE when contacting MusicBrainz
musicbrainzngs.set_useragent("MIXIE", "0.1")

# Respect MusicBrainz's API request limit
musicbrainzngs.set_rate_limit(1.0)

# Search MusicBrainz for possible album releases
def search_album(artist, album):
    results = musicbrainzngs.search_releases(
        artist=artist,
        release=album,
        limit=5
    )
    return results.get("release-list", [])


# Search for a recording using its artist and title
def search_recording(artist, title):
    results = musicbrainzngs.search_recordings(
        artist=artist,
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

# Retrieve the tracklist for a specific MusicBrainz release
@lru_cache(maxsize=256)
def get_release_tracklist(release_id):
    result = musicbrainzngs.get_release_by_id(
        release_id,
        includes=["recordings"]
    )
    release = result["release"]
    tracks = []

    # Read every disc belonging to this release
    for medium in release.get("medium-list", []):
        disc_number = medium.get("position", "1")
        for track in medium.get("track-list", []):
            tracks.append({
                "disc": disc_number,
                "number": track.get("number"),
                "title": track.get("title") or track["recording"]["title"],
                "recording_id": track["recording"].get("id")
            })
    return tracks

# Retrieve MusicBrainz details for an identified recording
@lru_cache(maxsize=256)
def get_recording_details(recording_id):
    result = musicbrainzngs.get_recording_by_id(
        recording_id,
        includes=["artist-credits", "releases"]
    )

    return result["recording"]

# Retrieve album metadata from a MusicBrainz release
@lru_cache(maxsize=256)
def get_release_metadata(release_id):
    result = musicbrainzngs.get_release_by_id(
        release_id,
        includes=["artists"]
    )

    release = result["release"]

    return {
        "release_id": release["id"],
        "album": release.get("title"),
        "album_artist": release.get("artist-credit-phrase"),
        "date": release.get("date"),
        "country": release.get("country"),
        "barcode": release.get("barcode")
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
            data = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    for artwork in data.get("images", []):
        if artwork.get("front") and artwork.get("approved"):
            return {
                "release_id": release_id,
                "image": artwork["image"],
                "preview": artwork.get("thumbnails", {}).get("500")
            }
    return None

# Download approved front-cover artwork
def download_release_art(release_id, preview=False):
    artwork = get_release_front_art(release_id)
    if artwork is None:
        return None
    url = (
        artwork["preview"]
        if preview and artwork.get("preview")
        else artwork["image"]
    ).replace(
        "http://coverartarchive.org/",
        "https://coverartarchive.org/",
        1
    )
    request = Request(url, headers={
        "User-Agent": "MIXIE/0.1"
    })
    with urlopen(request, timeout=8 if preview else 30) as response:
        mime = response.headers.get_content_type()
        data = response.read(20_000_001)
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError("Unsupported cover image format.")
    if len(data) > 20_000_000:
        raise ValueError("Cover artwork exceeds 20 MB.")
    return {"mime": mime, "data": data}

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

# Match audited audio to positions on a MusicBrainz release
def match_album_to_release(audits, release_id):
    tracklist = get_release_tracklist(release_id)
    matched = {}
    review = []
    for audit in audits:
        details = audit["details"]
        recording_id = details.get("id") if details else None
        positions = [
            track for track in tracklist
            if track["recording_id"] == recording_id
        ]
        if audit["status"] != "preliminary_match" or len(positions) != 1:
            review.append(audit["path"])
            continue
        position = (positions[0]["disc"], positions[0]["number"])
        if position in matched:
            review.append(audit["path"])
            continue
        matched[position] = audit["path"]
    missing = [
        track for track in tracklist
        if (track["disc"], track["number"]) not in matched
    ]
    return matched, review, missing

# Normalize formatting without removing version information
def normalize_title(title):
    # Standardize typographic hyphens and apostrophes
    title = title.translate(str.maketrans({
        "\u2010": "-",  # Hyphen
        "\u2011": "-",  # Nonbreaking hyphen
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
