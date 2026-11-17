import os
import sys
import audio
import lookup
import tag
import hashlib
from pathlib import Path
from io import BytesIO
from itertools import combinations
from PIL import Image, ImageChops, ImageOps, ImageStat
import constants
import sort
import archive
from mutagen import File
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
import json
import tempfile

# Preserve artwork disagreements for interactive review
class ArtworkReviewRequired(ValueError):
    def __init__(self, candidates, release_ids):
        self.candidates = [
            {
                "path": item["source"],
                "sha256": hashlib.sha256(
                    item["data"]
                ).hexdigest()
            }
            for item in candidates
        ]
        self.release_ids = list(release_ids)

        super().__init__(
            "Supplied artwork does not match known covers."
        )

# Select verified artwork, prioritizing files supplied with the album
def select_album_art(album, release_ids, artwork_decision=None):
    def prepare_image(data):
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.format not in ("JPEG", "PNG"):
                raise ValueError("Unsupported artwork format.")
            mime = Image.MIME[image.format]
            pixels = image.width * image.height
            preview = ImageOps.fit(
                ImageOps.exif_transpose(image).convert("RGB"),
                (256, 256),
                method=Image.Resampling.LANCZOS
            )
            return preview, mime, pixels
    def difference(first, second):
        result = ImageChops.difference(first, second)
        return sum(ImageStat.Stat(result).mean) / 3

    # Locate supplied artwork using our existing sorter
    paths = [Path(t["path"]) for t in album["tracks"]]
    root = Path(os.path.commonpath([str(p.parent) for p in paths]))
    assets = sort.scan_import_assets(root)
    local_files = sorted(
        Path(asset["path"])
        for asset in assets
        if Path(asset["path"]).suffix.lower()
        in constants.album_art_types
    )

    # Prefer a root-level cover corroborated by a front scan
    primary_covers = [
        path for path in local_files
        if path.parent == root
        and path.name.casefold() in {
            "cover.jpg", "cover.png",
            "folder.jpg", "folder.png"
        }
    ]
    front_scans = [
        path for path in local_files
        if path.stem.casefold() == "front"
    ]
    if len(primary_covers) == 1 and len(front_scans) == 1:
        primary = primary_covers[0]
        front = front_scans[0]
        if any(
            path.stat().st_size > 20_000_000
            for path in (primary, front)
        ):
            raise ValueError("Local cover exceeds 20 MB.")
        data = primary.read_bytes()
        primary_preview, mime, _ = prepare_image(data)
        front_preview, _, _ = prepare_image(front.read_bytes())
        score = difference(primary_preview, front_preview)
        print(f"Local front-cover agreement: {score:.2f}")
        if score <= 6.0 and artwork_decision is None:
            print(f"Using corroborated local cover: {primary.name}")
            return {
                "mime": mime,
                "data": data,
                "source": str(primary)
            }

    # Trust matching local front-cover files before requesting artwork
    covers = {
        path.stem.casefold(): path
        for path in local_files
        if path.stem.casefold() in {"front", "folder", "cover"}
    }

    for first_name, second_name in (
        ("front", "folder"),
        ("front", "cover"),
        ("folder", "cover"),
    ):
        if first_name not in covers or second_name not in covers:
            continue

        first = covers[first_name]
        second = covers[second_name]

        if first.parent != second.parent:
            continue

        if any(
            path.stat().st_size > 20_000_000
            for path in (first, second)
        ):
            raise ValueError("Local cover exceeds 20 MB.")

        first_data = first.read_bytes()
        first_preview, mime, _ = prepare_image(first_data)
        second_preview, _, _ = prepare_image(second.read_bytes())

        score = difference(first_preview, second_preview)
        print(f"Local cover agreement: {score:.2f}")

        if score <= 6.0 and artwork_decision is None:
            print(f"Using corroborated local cover: {first.name}")
            return {
                "mime": mime,
                "data": first_data,
                "source": str(first)
            }

    # Exclude booklet, CD, sticker and packaging scans
    comparison_files = [
        path for path in local_files
        if path.stem.casefold()
        in {"front", "folder", "cover", "front+sticker"}
    ] or local_files


    # Decode each local cover only once
    prepared_files = []

    for path in comparison_files:
        if path.stat().st_size > 20_000_000:
            raise ValueError(f"Artwork too large: {path.name}")

        data = path.read_bytes()
        preview, mime, _ = prepare_image(data)

        prepared_files.append({
            "preview": preview,
            "mime": mime,
            "data": data,
            "source": str(path)
        })

    # Honor an explicitly approved local cover
    if artwork_decision is not None:
        if (
            artwork_decision.get("kind") != "local_artwork"
            or artwork_decision.get("status") != "pending_application"
            or sorted(artwork_decision.get("release_ids", []))
                != sorted(release_ids)
        ):
            raise ValueError("Artwork approval needs fresh review.")

        selected_path = Path(artwork_decision["path"]).resolve()

        approved = [
            item for item in prepared_files
            if (
                Path(item["source"]).resolve() == selected_path
                and hashlib.sha256(item["data"]).hexdigest()
                    == artwork_decision["sha256"]
            )
        ]

        if len(approved) != 1:
            raise ValueError(
                "Approved artwork changed or is no longer available."
            )

        selected = approved[0]
        print("  Using explicitly approved local artwork.")

        return {
            "mime": selected["mime"],
            "data": selected["data"],
            "source": selected["source"]
        }

    # Use consistent embedded covers when no separate art exists
    if not local_files:
        selected = None
        for track in album["tracks"]:
            audio = File(track["path"])
            if isinstance(audio, FLAC):
                pictures = audio.pictures
            elif isinstance(audio, MP3):
                pictures = (
                    audio.tags.getall("APIC")
                    if audio.tags else []
                )
            else:
                selected = None
                break
            fronts = [p for p in pictures if p.type == 3]
            if len(fronts) != 1:
                selected = None
                break
            picture = fronts[0]
            if (
                picture.mime not in ("image/jpeg", "image/png")
                or not picture.data
                or len(picture.data) > 20_000_000
            ):
                selected = None
                break

            # Validate actual image format, not just its tag
            _, mime, _ = prepare_image(picture.data)
            if mime != picture.mime:
                raise ValueError("Embedded artwork MIME mismatch.")
            if selected is None:
                selected = {
                    "mime": mime,
                    "data": picture.data,
                    "source": "Consistent embedded front cover"
                }
            elif (
                selected["mime"] != mime
                or selected["data"] != picture.data
            ):
                raise ValueError(
                    "Embedded covers disagree across tracks."
                )
        if selected is not None:
            print("  Using consistent embedded front artwork.")
            return selected

    # Compare cached previews against release references
    references = {}

    for release_id in release_ids[:5]:
        artwork = lookup.download_release_art(
            release_id, preview=True
        )
        if artwork is None:
            continue
        reference, _, _ = prepare_image(artwork["data"])
        references[release_id] = {"preview": reference}

        local_matches = []

        for candidate in prepared_files:
            score = difference(
                candidate["preview"],
                reference
            )

            print(
                f"  {Path(candidate['source']).name}: "
                f"difference {score:.2f}"
            )

            if score <= 6.0:
                local_matches.append({
                    "mime": candidate["mime"],
                    "data": candidate["data"],
                    "source": candidate["source"]
                })

        if local_matches:
            hashes = {
                hashlib.sha256(item["data"]).digest()
                for item in local_matches
            }
            if len(hashes) > 1:
                raise ValueError("Multiple local covers need review.")
            print("  Using validated local artwork.")
            return local_matches[0]
    if local_files:
        raise ArtworkReviewRequired(
            prepared_files, release_ids
        )
    if not references:
        raise ValueError("No reference artwork available.")

    # No local artwork: use online art only when releases agree
    if len(references) != len(release_ids):
        raise ValueError("Some release artwork is unavailable.")
    for first, second in combinations(references.values(), 2):
        if difference(first["preview"], second["preview"]) > 6.0:
            raise ValueError("Online cover variants need review.")

    # Download a full-quality image only when local art is absent
    selected_id = sorted(references)[0]
    selected = lookup.download_release_art(selected_id)
    if selected is None:
        raise ValueError("Selected online artwork is unavailable.")
    print("  No local cover found. Using online artwork.")
    return {
        **selected,
        "source": f"Cover Art Archive: {selected_id}"
    }

def preview_album(
    album, api_key, reviewed_decision=None, artwork_decision=None,
    manual_fields=None
):
    paths = sorted(track["path"] for track in album["tracks"])
    cache = {}
    print(f"\nProcessing album: {album['album']}")

    # Identify recordings without relying on existing tags
    print("\nInitial recording identification:")
    audits = audio.audit_tracks(paths, api_key, cache=cache)
    identified = [
        result["details"]
        for result in audits
        if result["status"] == "preliminary_match"
        and result["details"] is not None
    ]

    single_track = len(paths) == 1

    if single_track:
        if len(identified) != 1:
            raise ValueError(
                "Single-track recording identity needs review."
            )
    elif len(identified) < 2:
        raise ValueError(
            "Not enough independent recording matches."
        )


    # Discover candidate releases automatically

    if single_track:
        release_ids = {
            release["id"]
            for release in identified[0].get("release-list", [])
            if release.get("id")
        }
    else:
        release_ids = lookup.shared_release_ids(identified)

    releases = []
    for release_id in sorted(release_ids):
        tracklist = lookup.get_release_tracklist(release_id)
        if len(tracklist) != len(paths):
            continue
        signature = tuple(
            (str(t["disc"]), str(t["number"]), t["recording_id"])
            for t in tracklist
        )
        releases.append((release_id, tracklist, signature))
    if not releases:
        raise ValueError("No complete matching release found.")


    # Check which release editions actually match our audio
    compatible = []
    confirmed_ids = [
        result["details"]["id"]
        for result in audits
        if result["status"] == "preliminary_match"
        and result["details"] is not None
    ]
    for candidate_id, candidate_tracks, _ in releases:
        recording_ids = [
            track["recording_id"]
            for track in candidate_tracks
        ]

        # Independent recording matches must appear exactly once
        if any(recording_ids.count(rid) != 1 for rid in confirmed_ids):
            continue

        # Resolve uncertain tracks using this release
        checked = audio.audit_tracks(
            paths, api_key,
            release_id=candidate_id,
            cache=cache,
            quiet=True
        )
        matched, review, missing = lookup.match_album_to_release(
            checked, candidate_id
        )
        if review or missing or len(matched) != len(paths):
            continue
        positions = tuple(sorted(
            (str(path), disc, number)
            for (disc, number), path in matched.items()
        ))
        compatible.append(
            (candidate_id, candidate_tracks, matched, positions)
        )
    if not compatible:
        raise ValueError("No candidate release matches the complete album.")

    # Use complete existing numbering to narrow release candidates
    local_tracks = {
        Path(t["path"]).resolve(): t
        for t in album["tracks"]
    }
    if all(
        tag.parse_track_number(t["track_number"])[0]
        for t in album["tracks"]
    ):
        numbered_candidates = []
        for candidate in compatible:
            candidate_matched = candidate[2]
            agrees = True
            for (disc, number), path in candidate_matched.items():
                metadata = local_tracks[Path(path).resolve()]
                local_number, _ = tag.parse_track_number(
                    metadata["track_number"]
                )
                local_disc, _ = tag.parse_disc_number(
                    metadata["disc_number"]
                )
                try:
                    release_number = int(str(number))
                except (TypeError, ValueError):
                    # Vinyl-style positions such as A1 cannot
                    # be compared to ordinary numeric track tags.
                    agrees = False
                    break
                if int(local_number) != release_number:
                    agrees = False
                    break
                if local_disc is not None and local_disc != int(disc):
                    agrees = False
                    break
            if agrees:
                numbered_candidates.append(candidate)
        if not numbered_candidates:
            raise ValueError(
                "Existing numbering disagrees with all candidate releases."
            )
        compatible = numbered_candidates

    # Different file-to-track mappings still require review
    if len({candidate[3] for candidate in compatible}) != 1:
        raise ValueError("Compatible releases disagree on track positions.")
    release_id, tracklist, matched, _ = compatible[0]
    compatible_ids = [candidate[0] for candidate in compatible]
    common = lookup.common_release_metadata(compatible_ids)

    if single_track:
        if not (
            common.get("album")
            and common.get("album_artist")
        ):
            raise ValueError(
                "Single-track release editions disagree. "
                "Human review is required."
            )

        print(
            "Single-track identity corroborated by "
            "recording and release metadata."
        )

    proposals = tag.propose_release_numbering(matched, tracklist)
    proposals += tag.propose_common_metadata(
        matched, common, reviewed_decision=reviewed_decision
    )
    proposals += tag.propose_required_album_metadata(
        matched, common, manual_fields=manual_fields
    )

    # Select verified artwork for this album
    print("\nSelecting album artwork...")
    artwork = select_album_art(
        album, compatible_ids, artwork_decision=artwork_decision
    )
    print(f"Artwork selected: {artwork['source']}")

    # Report recording ambiguities resolved by album context
    initial_review = sum(
        result["status"] != "preliminary_match"
        for result in audits
    )

    print(f"\nInitial uncertainties resolved: {initial_review}")
    print("Remaining unmatched tracks: 0")

    print(f"\nAlbum matched: {len(matched)}/{len(paths)}")
    print(f"Compatible releases: {len(compatible_ids)}")
    print(f"Metadata corrections proposed: {len(proposals)}")

    return {
        "proposals": proposals,
        "artwork": artwork,
        "matched": matched,
        "compatible_ids": compatible_ids,
        "reviewed_decision": reviewed_decision,
        "artwork_decision": artwork_decision,
        "manual_fields": tag.validate_manual_metadata(manual_fields)
    }


# Compare existing embedded artwork with a verified cover
def artwork_difference(first_data, first_mime, second_data, second_mime):
    def preview(data, expected_mime):
        if not data or len(data) > 20_000_000:
            raise ValueError("Artwork is missing or too large.")

        with Image.open(BytesIO(data)) as image:
            image.load()

            if (
                image.format not in ("JPEG", "PNG")
                or Image.MIME.get(image.format) != expected_mime
            ):
                raise ValueError("Artwork format or MIME mismatch.")

            return ImageOps.fit(
                ImageOps.exif_transpose(image).convert("RGB"),
                (256, 256),
                method=Image.Resampling.LANCZOS
            )

    first = preview(first_data, first_mime)
    second = preview(second_data, second_mime)

    difference = ImageChops.difference(first, second)

    return sum(ImageStat.Stat(difference).mean) / 3

# Test an album's corrections on temporary copies only
def test_album_plan(plan, album, commit=False, auto=False):
    from shutil import copy2
    from tempfile import TemporaryDirectory
    from mutagen import File
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC

    originals = sorted({
        Path(path).resolve()
        for path in plan["matched"].values()
    })
    staging_root = tag.staging_folder.resolve()
    for source in originals:
        if not source.is_relative_to(staging_root):
            raise PermissionError("Source is outside staging.")
    # Preserve and verify originals before editing working copies
    backup_path = archive.backup_staged_import(album)
    print(f"Protected backup: {backup_path}")
    original_hashes = {
        path: hashlib.sha256(path.read_bytes()).digest()
        for path in originals
    }


    receipt = None
    with TemporaryDirectory(
        prefix=".mixie-work-",
        dir=tag.script_folder.parent
    ) as working_folder:
        copies = {}
        for index, source in enumerate(originals):
            target = Path(working_folder) / f"{index:03d}-{source.name}"
            copy2(source, target)
            copies[source] = target

        # Redirect every metadata correction to its working copy
        corrections = [
            {
                **proposal,
                "path": copies[Path(proposal["path"]).resolve()]
            }
            for proposal in plan["proposals"]
        ]
        writes = tag.apply_corrections(
            corrections, approved=True
        )

        # Apply user-approved metadata to temporary working copies
        reviewed = plan.get("reviewed_decision")

        if reviewed is not None:
            if reviewed.get("status") != "pending_application":
                raise ValueError("Metadata decision is not approved.")

            field = reviewed["field"]
            value = reviewed["value"]

            for target in copies.values():
                writes += tag.replace_reviewed_text_on_copy(
                    target, working_folder, field, value
                )

            # Verify every track received the selected value
            for target in copies.values():
                saved = tag.read_track_metadata(target)

                if saved is None or saved[field] != (value,):
                    raise RuntimeError(
                        f"Reviewed metadata mismatch: {target.name}"
                    )

            print(
                f"Reviewed {field} applied and verified "
                f"on {len(copies)} working copies."
            )


        # Apply explicitly reviewed date/genre choices ONLY to working
        # copies. They may legitimately replace conflicting source tags.
        for field, value in tag.validate_manual_metadata(
            plan.get("manual_fields")
        ).items():
            for target in copies.values():
                writes += tag.replace_reviewed_text_on_copy(
                    target, working_folder, field, value
                )

        # Embed or replace artwork on working copies only
        approved_artwork = plan.get("artwork_decision")

        for target in copies.values():
            if approved_artwork is not None:
                tag.replace_reviewed_front_art_on_copy(
                    target, working_folder, plan["artwork"]
                )
            else:
                tag.embed_front_art(target, plan["artwork"])

            audio_file = File(target)
            if isinstance(audio_file, MP3):
                pictures = audio_file.tags.getall("APIC")
            elif isinstance(audio_file, FLAC):
                pictures = audio_file.pictures
            else:
                raise ValueError(f"Unsupported format: {target.name}")

            fronts = [
                picture for picture in pictures
                if picture.type == 3
            ]

            verified = bool(fronts) and all(
                artwork_difference(
                    picture.data,
                    picture.mime,
                    plan["artwork"]["data"],
                    plan["artwork"]["mime"]
                ) <= 6.0
                for picture in fronts
            )

            if not verified:
                raise RuntimeError(
                    f"Artwork verification failed: {target.name}"
                )


        # Prepare library sorting from verified working copies
        print("\nPlanning library destinations...")
        positions = {
            Path(path).resolve(): (str(disc), str(number))
            for (disc, number), path in plan["matched"].items()
        }
        disc_count = len({
            disc for disc, _ in plan["matched"]
        })
        verified_tracks = []
        for source, target in copies.items():
            metadata = tag.read_track_metadata(target)
            if metadata is None:
                raise ValueError(f"Cannot read tags: {target.name}")
            def required(field):
                values = metadata[field]
                if len(values) != 1 or not values[0].strip():
                    raise ValueError(
                        f"Missing or ambiguous {field}: {target.name}"
                    )
                return values[0].strip()
            disc, number = positions[source]
            actual_number, _ = tag.parse_track_number(
                metadata["track_number"]
            )
            if actual_number is None or int(actual_number) != int(number):
                raise ValueError(
                    f"Track number verification failed: {target.name}"
                )
            existing_disc, _ = tag.parse_disc_number(
                metadata["disc_number"]
            )
            if existing_disc != int(disc):
                raise ValueError(f"Disc number verification failed: {target.name}")
            if tag.get_disc_totals(metadata) != {disc_count}:
                raise ValueError(f"Disc total verification failed: {target.name}")
            required("date")
            required("genre")
            disc_size = sum(1 for d, _ in plan["matched"] if str(d) == str(disc))
            totals = tag.get_track_totals(metadata)
            if totals not in ({disc_size}, {len(plan["matched"])}):
                raise ValueError(f"Track total verification failed: {target.name}")
            verified_tracks.append({
                "path": target,
                "album_artist": required("album_artist"),
                "album": required("album"),
                "title": required("title"),
                "track_number": required("track_number"),
                "disc_number": (
                    f"{disc}/{disc_count}" if disc_count > 1 else None
                )
            })

        # Preserve the validated artwork as a separate album cover
        artwork = plan["artwork"]

        if artwork["mime"] == "image/jpeg":
            cover_name = "cover.jpg"
        elif artwork["mime"] == "image/png":
            cover_name = "cover.png"
        else:
            raise ValueError("Unsupported album artwork format.")

        cover_path = Path(working_folder) / cover_name
        cover_path.write_bytes(artwork["data"])

        # Verify the temporary cover matches the selected image
        if cover_path.read_bytes() != artwork["data"]:
            raise RuntimeError("Artwork copy verification failed.")

        verified_assets = [{
            "path": cover_path,
            "relative_path": cover_name,
            "album_artist": verified_tracks[0]["album_artist"],
            "album": verified_tracks[0]["album"]
        }]

        # Preserve supplied booklet scans and other album artwork
        album_source = Path(os.path.commonpath(
            [str(path.parent) for path in originals]
        )).resolve()

        staging_root = tag.staging_folder.resolve()
        relative_source = album_source.relative_to(staging_root)
        import_root = staging_root / relative_source.parts[0]

        # Don't assign shared extras to the wrong album
        import_albums = [
            candidate
            for candidate in tag.group_staged_albums().values()
            if any(
                Path(t["path"]).resolve().is_relative_to(import_root)
                for t in candidate["tracks"]
            )
        ]

        if album_source == import_root and len(import_albums) > 1:
            print("Shared import extras require album assignment.")
            supplied_assets = []
        else:
            supplied_assets = sort.scan_import_assets(album_source)

        for asset in supplied_assets:
            source_asset = Path(asset["path"])
            relative = Path(asset["relative_path"])

            library_relative = relative

            if (
                len(relative.parts) == 1
                and relative.name.casefold() == cover_name.casefold()
            ):
                library_relative = Path("Original Artwork") / relative

            # Other file types remain protected by the original backup
            if source_asset.suffix.lower() not in (
                ".jpg", ".jpeg", ".png", ".pdf"
            ):
                continue

            target_asset = (
                Path(working_folder) / ".mixie-assets" / relative
            )
            target_asset.parent.mkdir(parents=True, exist_ok=True)
            copy2(source_asset, target_asset)

            if archive.file_hash(source_asset) != archive.file_hash(
                target_asset
            ):
                raise RuntimeError(
                    f"Extra asset verification failed: {relative}"
                )

            verified_assets.append({
                "path": target_asset,
                "relative_path": library_relative.as_posix(),
                "album_artist": verified_tracks[0]["album_artist"],
                "album": verified_tracks[0]["album"]
            })

        artwork_plan = sort.plan_album_assets(
            verified_assets, working_folder
        )

        # The planner may safely collapse identical artwork files
        # that resolve to the same library destination.
        if not artwork_plan or len(artwork_plan) > len(verified_assets):
            raise RuntimeError("Album artwork planning failed.")

        sorting_plan = sort.plan_library_sort(
            verified_tracks,
            approved=True,
            source_root=working_folder,
            check_existing=False
        )

        if len(sorting_plan) != len(verified_tracks):
            raise RuntimeError("Library sorting plan is incomplete.")
        print(f"Library destinations verified: {len(sorting_plan)}")
        print(
            f"Album artwork destination: "
            f"{artwork_plan[0]['destination']}"
        )

        # Confirm original staged files were not modified
        for source, expected_hash in original_hashes.items():
            current_hash = hashlib.sha256(
                source.read_bytes()
            ).digest()
            if current_hash != expected_hash:
                raise RuntimeError(
                    f"Original file changed: {source.name}"
                )

        # Optionally copy verified results into Music Library

        if commit:
            if auto:
                confirmed = True
            else:
                confirmation = input(
                    f"\nType IMPORT {album['album']} "
                    "to commit this album: "
                ).strip()
                confirmed = confirmation == f"IMPORT {album['album']}"

            if not confirmed:
                print("Library import cancelled.")
            else:
                created, already_present = sort.commit_verified_plan(
                    sorting_plan + artwork_plan
                )

                # Verify all committed tracks and artwork
                for item in sorting_plan + artwork_plan:
                    destination = Path(item["destination"])

                    if not destination.is_file():
                        raise RuntimeError(
                            f"Missing library file: {destination}"
                        )

                    if sort.file_checksum(item["source"]) != \
                            sort.file_checksum(destination):
                        raise RuntimeError(
                            f"Library verification failed: {destination}"
                        )

                print(
                    f"\nImported and verified "
                    f"{len(created)} new files, "
                    f"{len(already_present)} already verified."
                )
                print("Originals and backup preserved.")

                # Record every successfully verified library destination
                receipt = {
                    str(Path(item["destination"]).resolve()):
                    sort.file_checksum(item["destination"]).hex()
                    for item in sorting_plan + artwork_plan
                }

        print(f"\nWorking copies verified: {len(copies)}")
        print(f"Metadata frame writes: {writes}")
        print("Artwork verified on every working copy.")
        print("Original staged files unchanged.")
    print("Temporary copies cleaned up.")
    return receipt

# Identify the exact staged files belonging to a review
def review_fingerprint(issue):
    staging = tag.staging_folder.resolve()
    tracks = []

    for relative in sorted(issue["tracks"]):
        path = (staging / relative).resolve()

        if (
            not path.is_relative_to(staging)
            or not path.is_file()
        ):
            raise ValueError("Reviewed source files are unavailable.")

        tracks.append([
            relative,
            sort.file_checksum(path).hex()
        ])

    identity = {
        "import_name": issue["import_name"],
        "tracks": tracks
    }

    encoded = json.dumps(
        identity, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


# Read records of successfully completed reviews
def load_completed_reviews():
    path = tag.script_folder / ".completed_reviews.json"

    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("entries"), dict)
    ):
        raise ValueError("Invalid completed-review ledger.")

    return data["entries"]

# Verify a previously completed album before skipping it
def is_review_complete(issue, completed_entries):
    try:
        fingerprint = review_fingerprint(issue)
    except (OSError, ValueError):
        return False

    record = completed_entries.get(fingerprint)

    if not isinstance(record, dict):
        return False

    receipt = record.get("receipt")

    if (
        not isinstance(receipt, dict)
        or len(receipt) < len(issue["tracks"]) + 1
    ):
        return False

    for name in ("originals_archive", "library_archive"):
        archive_path = record.get(name)

        if (
            not isinstance(archive_path, str)
            or not Path(archive_path).is_file()
        ):
            return False

    library_root = Path(sort.music_library_folder).resolve()

    for destination, checksum in receipt.items():
        if (
            not isinstance(destination, str)
            or not isinstance(checksum, str)
        ):
            return False

        path = Path(destination)

        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(library_root)
            or sort.file_checksum(path).hex() != checksum
        ):
            return False

    return True

# Save a completed review after import and archive verification
def record_completed_review(issue, receipt, originals_zip, library_zip):
    if not receipt:
        raise ValueError("Cannot complete a review without a receipt.")

    if not Path(originals_zip).is_file():
        raise ValueError("Originals archive is missing.")

    if not Path(library_zip).is_file():
        raise ValueError("Music Library archive is missing.")

    # Verify both archives against their current source files
    library_root = Path(sort.music_library_folder).resolve()

    live_library = sort.file_manifest(library_root)
    archived_library = sort.zip_manifest(library_zip)

    if archived_library != live_library:
        raise RuntimeError(
            "Music Library archive does not match the library."
        )

    live_originals = sort.file_manifest(sort.inbox_folder)
    archived_originals = sort.zip_manifest(originals_zip)

    for name, checksum in live_originals.items():
        if archived_originals.get(name) != checksum:
            raise RuntimeError(
                f"Original download missing from archive: {name}"
            )

    # Every committed file must match its receipt and archive
    for destination, expected_hash in receipt.items():
        path = Path(destination)

        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(library_root)
        ):
            raise RuntimeError(
                f"Invalid completed file: {destination}"
            )

        relative = path.resolve().relative_to(
            library_root
        ).as_posix()

        if (
            live_library.get(relative) != expected_hash
            or archived_library.get(relative) != expected_hash
        ):
            raise RuntimeError(
                f"Completed file failed verification: {relative}"
            )

    entries = load_completed_reviews()
    identity = review_fingerprint(issue)

    entries[identity] = {
        "album": issue["album"],
        "receipt": receipt,
        "originals_archive": str(originals_zip),
        "library_archive": str(library_zip)
    }

    destination = tag.script_folder / ".completed_reviews.json"

    descriptor, temporary = tempfile.mkstemp(
        prefix=".mixie-completed-",
        suffix=".tmp",
        dir=tag.script_folder
    )

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(
                {"version": 1, "entries": entries},
                file, indent=2, ensure_ascii=False
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)

    return identity

# Preserve unresolved reviews and previously approved choices
def reconcile_review_queue(fresh_issues):
    previous_issues = load_review_queue()

    def identity(issue):
        return (
            issue["import_name"],
            tuple(sorted(issue["tracks"]))
        )

    previous_by_identity = {
        identity(issue): issue
        for issue in previous_issues
    }

    seen = set()

    for issue in fresh_issues:
        key = identity(issue)
        seen.add(key)
        previous = previous_by_identity.get(key)

        # Never carry approvals into changed review evidence
        if previous is None:
            continue

        old_details = previous.get("details", {})
        new_details = issue.get("details", {})
        approved_hash = previous.get("source_fingerprint")

        try:
            same_source = (
                isinstance(approved_hash, str)
                and approved_hash == review_fingerprint(issue)
            )
        except (OSError, ValueError):
            same_source = False

        # Preserve unchanged reviews. Older approvals without a
        # fingerprint remain manual-only.
        if old_details == new_details and (
            same_source or approved_hash is None
        ):
            for field in (
                "decision", "artwork_decision", "manual_fields",
                "artwork_details", "source_fingerprint"
            ):
                if field in previous:
                    issue[field] = previous[field]
            continue

        if not same_source:
            continue

        # Human-approved date/genre choices remain tied to these exact
        # staged audio bytes even if the next review finds artwork issues.
        if previous.get("manual_fields"):
            issue["manual_fields"] = previous["manual_fields"]
            issue["source_fingerprint"] = approved_hash

        old_kind = old_details.get("kind")
        new_kind = new_details.get("kind")

        # Metadata approval revealed an artwork disagreement.
        if old_kind == "metadata_conflict" and new_kind == "artwork_conflict":
            if previous.get("decision") is None:
                continue

            issue["details"] = old_details
            issue["artwork_details"] = new_details
            issue["decision"] = previous["decision"]
            issue["source_fingerprint"] = approved_hash

            if (
                previous.get("artwork_details") == new_details
                and previous.get("artwork_decision") is not None
            ):
                issue["artwork_decision"] = previous["artwork_decision"]

            issue["reason"] = (
                previous["reason"] + "\nAdditional artwork review: "
                + issue["reason"]
            )

        # Artwork approval revealed a metadata disagreement.
        elif old_kind == "artwork_conflict" and new_kind == "metadata_conflict":
            issue["artwork_details"] = old_details
            issue["source_fingerprint"] = approved_hash

            if previous.get("artwork_decision") is not None:
                issue["artwork_decision"] = previous["artwork_decision"]

            issue["reason"] += "\nExisting artwork review also applies."


    # Retain older unresolved reviews while their files remain staged
    staging_root = tag.staging_folder.resolve()

    for previous in previous_issues:
        key = identity(previous)

        if key in seen:
            continue

        tracks = [
            (staging_root / relative).resolve()
            for relative in previous["tracks"]
        ]

        if tracks and all(
            path.is_relative_to(staging_root) and path.is_file()
            for path in tracks
        ):
            fresh_issues.append(previous)
            seen.add(key)


    # Don't resurrect successfully completed albums
    completed = load_completed_reviews()
    library_root = Path(sort.music_library_folder).resolve()
    pending = []

    for issue in fresh_issues:
        try:
            fingerprint = review_fingerprint(issue)
        except (OSError, ValueError):
            pending.append(issue)
            continue

        entry = completed.get(fingerprint)
        receipt = (
            entry.get("receipt")
            if isinstance(entry, dict) else None
        )

        if not isinstance(receipt, dict) or not receipt:
            pending.append(issue)
            continue

        verified = True

        for destination, checksum in receipt.items():
            path = Path(destination)

            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(library_root)
                or sort.file_checksum(path).hex() != checksum
            ):
                verified = False
                break

        if verified:
            print(f"Previously completed: {issue['album']}")
        else:
            pending.append(issue)

    return pending


# Save unresolved albums for review after ingestion
def save_review_queue(issues):
    destination = tag.script_folder / ".needs_review.json"

    descriptor, temporary = tempfile.mkstemp(
        prefix=".mixie-review-",
        suffix=".tmp",
        dir=tag.script_folder
    )

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(
                {"version": 1, "issues": issues},
                file,
                indent=2,
                ensure_ascii=False
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)

    return destination


# Load unresolved albums from the persistent review queue
def load_review_queue():
    destination = tag.script_folder / ".needs_review.json"

    if not destination.exists():
        return []

    with destination.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("issues"), list)
    ):
        raise ValueError("Invalid review queue format.")

    return data["issues"]

# Ask the user to approve a local album cover
def choose_artwork_resolution(issue):
    details = (
        issue.get("artwork_details")
        or issue.get("details", {})
    )

    if details.get("kind") != "artwork_conflict":
        return False

    candidates = details.get("candidates", [])

    if not candidates:
        print("No artwork candidates available.")
        return False

    print("\nM.I.X.I.E. - Artwork Review")
    print("Local artwork could not be verified automatically.")

    for index, candidate in enumerate(candidates, 1):
        print(f"{index}. {candidate['path']}")

    print("0. Skip")

    try:
        choice = input("\nSelect artwork: ").strip()
    except (KeyboardInterrupt, EOFError):
        return False

    if choice == "0":
        return False

    if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
        print("Invalid selection.")
        return False

    candidate = candidates[int(choice) - 1]
    path = Path(candidate["path"]).resolve()

    if not path.is_file() or not path.is_relative_to(
        tag.staging_folder.resolve()
    ):
        raise ValueError("Artwork is no longer available.")

    data = path.read_bytes()

    if (
        len(data) > 20_000_000
        or hashlib.sha256(data).hexdigest() != candidate["sha256"]
    ):
        raise ValueError("Artwork changed since review.")

    issue["artwork_decision"] = {
        "kind": "local_artwork",
        "path": str(path),
        "sha256": candidate["sha256"],
        "release_ids": details["release_ids"],
        "status": "pending_application"
    }
    issue["source_fingerprint"] = review_fingerprint(issue)
    print("Artwork approval saved.")
    return True

# Ask for missing or disputed date/genre in one consolidated review.
def choose_required_metadata_resolution(issue):
    details = issue.get("details", {})
    if details.get("kind") != "required_metadata":
        return False
    needed = details.get("fields", [])
    if not needed or any(field not in ("date", "genre") for field in needed):
        raise ValueError("Unrecognized required metadata fields.")
    print("\nM.I.X.I.E. - Required Metadata Review")
    selected = dict(issue.get("manual_fields") or {})
    for field in needed:
        print(f"{field.title()} requires your choice; no value will be guessed.")
        try:
            value = input(f"Enter {field} (blank to postpone): ").strip()
        except (KeyboardInterrupt, EOFError):
            return False
        if not value:
            return False
        selected[field] = value
    try:
        selected = tag.validate_manual_metadata(selected)
    except (ValueError, TypeError) as error:
        print(f"Metadata choice not saved: {error}")
        return False
    issue["manual_fields"] = selected
    issue["source_fingerprint"] = review_fingerprint(issue)
    print("Approved metadata saved; staged originals unchanged.")
    return True

# Ask the user to resolve a metadata disagreement
def choose_metadata_resolution(issue):
    details = issue.get("details", {})

    if details.get("kind") != "metadata_conflict":
        print("\nThis issue does not have a metadata choice yet.")
        return False

    existing = details["existing_values"]
    reference = details["reference_value"]
    field = details["field"]

    if len(existing) != 1:
        print("\nMultiple existing values require further review.")
        return False

    print(f"\nMetadata conflict: {field.replace('_', ' ').title()}")
    print(f"1. Keep existing: {existing[0]}")
    print(f"2. Use MusicBrainz: {reference}")
    print("3. Enter a custom value")
    print("0. Skip for now")

    while True:
        try:
            choice = input("\nSelect an option: ").strip()
        except (KeyboardInterrupt, EOFError):
            return False

        if choice == "1":
            selected = existing[0]
        elif choice == "2":
            selected = reference
        elif choice == "3":
            selected = input("Enter your preferred value: ").strip()
            if not selected:
                print("The value cannot be empty.")
                continue
        elif choice == "0":
            return False
        else:
            print("Invalid selection.")
            continue

        issue["decision"] = {
            "field": field,
            "value": selected,
            "existing_value": existing[0],
            "reference_value": reference,
            "status": "pending_application"
        }
        issue["source_fingerprint"] = review_fingerprint(issue)
        print(f"\nChoice saved: {selected}")
        print("No audio files have been modified.")
        return True

# Test a saved review decision without committing to the library
def test_review_decision(issue, commit=False):
    decision = issue.get("decision")
    artwork_decision = issue.get("artwork_decision")
    manual_fields = issue.get("manual_fields")
    approved_source = issue.get("source_fingerprint")

    if (
        approved_source is not None
        and approved_source != review_fingerprint(issue)
    ):
        raise ValueError(
            "Original audio changed since approval. Review again."
        )

    if decision is not None:
        if decision.get("status") != "pending_application":
            raise ValueError("Metadata decision needs fresh approval.")
    if artwork_decision is not None:
        if artwork_decision.get("status") != "pending_application":
            raise ValueError("Artwork decision needs fresh approval.")

    staging_root = tag.staging_folder.resolve()
    expected_tracks = {
        (staging_root / relative).resolve()
        for relative in issue["tracks"]
    }

    if not expected_tracks or any(
        not path.is_relative_to(staging_root)
        for path in expected_tracks
    ):
        raise ValueError("Review contains invalid staging paths.")

    matches = [
        album
        for (import_name, _), album
        in tag.group_staged_albums().items()
        if import_name == issue["import_name"]
        and {
            Path(track["path"]).resolve()
            for track in album["tracks"]
        } == expected_tracks
    ]

    if len(matches) != 1:
        raise ValueError("Cannot uniquely identify the staged album.")

    api_key = get_acoustid_client_key()
    if not api_key:
        raise ValueError("AcoustID key is not configured.")

    album = matches[0]

    plan = preview_album(
        album,
        api_key,
        reviewed_decision=decision,
        artwork_decision=issue.get("artwork_decision"),
        manual_fields=manual_fields
    )

    receipt = test_album_plan(
        plan,
        album,
        commit=commit,
        auto=commit
    )

    if commit:
        if receipt is None:
            print("\nReviewed import was not completed.")
        else:
            print("\nReviewed album imported and verified.")
            print("Archive finalization is still required.")
    else:
        print("\nReviewed metadata passed working-copy verification.")
        print("Music Library and staged originals remain unchanged.")

    return receipt

# Import a reviewed album, verify archives, and retire its review
def finalize_reviewed_album(issue, issues):
    receipt = test_review_decision(issue, commit=True)

    if receipt is None:
        print("Import cancelled. Album remains in review.")
        return False

    print("\nFinalizing and verifying archives...")

    originals_zip = archive.update_original_downloads_archive()
    library_zip = archive.create_verified_zip(
        sort.music_library_folder, "MusicLibrary"
    )

    # Only mark complete after all archive and receipt checks pass
    record_completed_review(
        issue, receipt, originals_zip, library_zip
    )

    remaining = [
        entry for entry in issues
        if entry is not issue
    ]

    save_review_queue(remaining)
    issues[:] = remaining

    print(f"\nReview completed: {issue['album']}")
    print(f"Remaining pending reviews: {len(issues)}")
    quarantined = auto_quarantine_archived_completed(
        originals_zip, library_zip
    )
    auto_purge_verified_quarantines()
    if quarantined:
        print(
            f"Verified imports quarantined: "
            f"{', '.join(quarantined)}"
        )
    else:
        print("Unfinished imports remain safely staged.")

    return True

# Review albums sequentially, without returning to the list each time
def review_queue_menu(issues):
    selected = None

    while True:
        if not issues:
            print("\nMIXIE: All pending reviews completed!")
            return

        # Show the album list only when requested
        if selected is None:
            print("\n================================")
            print("  M.I.X.I.E. - Review Queue")
            print("================================")

            for number, entry in enumerate(issues, 1):
                print(f"{number}. {entry['album']}")

            print("0. Finish reviewing")

            try:
                choice = input(
                    "\nSelect album (Enter = first): "
                ).strip()
            except (KeyboardInterrupt, EOFError):
                return

            if choice == "0":
                print("Unresolved albums remain in staging.")
                return

            if not choice:
                selected = 0
            elif choice.isdigit() and 1 <= int(choice) <= len(issues):
                selected = int(choice) - 1
            else:
                print("Invalid selection.")
                continue

        selected %= len(issues)
        issue = issues[selected]
        kind = issue.get("details", {}).get("kind")

        print("\n================================")
        print(f"  Reviewing {selected + 1}/{len(issues)}")
        print("================================")
        print(f"Album: {issue['album']}")
        print(f"Import: {issue['import_name']}")
        print(f"Tracks: {len(issue['tracks'])}")
        print(f"\nReason:\n{issue['reason']}")

        # Collect a decision only if one hasn't been saved
        if kind == "metadata_conflict":
            if issue.get("decision"):
                decision = issue["decision"]
                print(
                    f"\nSaved metadata choice: "
                    f"{decision['field']} = {decision['value']}"
                )
            elif choose_metadata_resolution(issue):
                save_review_queue(issues)

        elif kind == "artwork_conflict":
            if issue.get("artwork_decision"):
                print("\nArtwork approval already saved.")
            elif choose_artwork_resolution(issue):
                save_review_queue(issues)

        if kind == "required_metadata":
            if issue.get("manual_fields") and all(
                field in issue["manual_fields"]
                for field in issue.get("details", {}).get("fields", [])
            ):
                print("\nRequired metadata choices already approved.")
            elif choose_required_metadata_resolution(issue):
                save_review_queue(issues)

        # Some albums have both metadata and artwork conflicts
        if (
            issue.get("artwork_details")
            and not issue.get("artwork_decision")
        ):
            if choose_artwork_resolution(issue):
                save_review_queue(issues)

        while True:
            try:
                command = input(
                    "\n[P]rocess  [C]hange choice  "
                    "[Enter] Next  [M]enu  [0] Exit: "
                ).strip().casefold()
            except (KeyboardInterrupt, EOFError):
                print("\nReview postponed.")
                return

            if command == "0":
                print("Unresolved albums remain in staging.")
                return

            if command == "m":
                selected = None
                break

            if command == "":
                selected = (selected + 1) % len(issues)
                break


            if command == "c":
                choices = {}

                if kind == "metadata_conflict":
                    choices["1"] = (
                        "Metadata", choose_metadata_resolution
                    )

                if (
                    kind == "artwork_conflict"
                    or issue.get("artwork_details", {}).get("kind")
                        == "artwork_conflict"
                ):
                    choices["2"] = (
                        "Artwork", choose_artwork_resolution
                    )

                if kind == "required_metadata":
                    choices["3"] = (
                        "Required metadata", choose_required_metadata_resolution
                    )

                if not choices:
                    print("No editable choices for this issue yet.")
                    continue

                if len(choices) == 1:
                    callback = next(iter(choices.values()))[1]
                else:
                    print("\nWhich decision would you like to change?")
                    for number, (label, _) in choices.items():
                        print(f"{number}. {label}")
                    try:
                        selection = input(
                            "Choice (Enter to cancel): "
                        ).strip()
                    except (KeyboardInterrupt, EOFError):
                        continue

                    if selection not in choices:
                        print("Choice change cancelled.")
                        continue

                    callback = choices[selection][1]

                try:
                    if callback(issue):
                        save_review_queue(issues)
                        print("Updated choice saved.")
                except Exception as error:
                    print(f"Review choice failed: {error}")

                continue

            if command in ("p", "process"):

                completed = False

                try:
                    completed = finalize_reviewed_album(
                        issue, issues
                    )

                except tag.MetadataConflictError as error:
                    if issue.get("details", {}).get("kind") == "artwork_conflict":
                        issue["artwork_details"] = issue["details"]

                    issue["details"] = {
                        "kind": "metadata_conflict",
                        "field": error.field,
                        "existing_values": error.existing,
                        "reference_value": error.expected,
                        "track": Path(error.path).name
                    }
                    issue["reason"] = str(error)
                    issue["error_type"] = type(error).__name__

                    save_review_queue(issues)
                    print("\nMetadata approval required.")

                    if choose_metadata_resolution(issue):
                        save_review_queue(issues)
                        print("Choice saved. Press P to process.")

                except ArtworkReviewRequired as error:
                    issue["artwork_details"] = {
                        "kind": "artwork_conflict",
                        "candidates": error.candidates,
                        "release_ids": error.release_ids
                    }
                    save_review_queue(issues)

                    print("\nAdditional artwork approval required.")

                    if choose_artwork_resolution(issue):
                        save_review_queue(issues)
                        print("Artwork saved. Retrying processing...")

                        try:
                            completed = finalize_reviewed_album(
                                issue, issues
                            )
                        except Exception as retry_error:
                            print(f"Processing stopped: {retry_error}")

                except (Exception, KeyboardInterrupt) as error:
                    print(f"\nProcessing stopped: {error}")
                    print("Album remains pending unless completed.")

                if completed:
                    # The completed album has been removed from issues.
                    # Its successor now occupies the same position.
                    if not issues:
                        print("\nAll reviews completed!")
                        return

                    selected %= len(issues)
                    break

                continue

            print("Invalid option.")

# Find safe, previously approved decisions for an album
def reusable_review_decisions(key, album, pending_issues):
    staging = tag.staging_folder.resolve()

    tracks = tuple(sorted(
        Path(track["path"]).resolve()
        .relative_to(staging).as_posix()
        for track in album["tracks"]
    ))

    matches = [
        issue for issue in pending_issues
        if issue.get("import_name") == key[0]
        and tuple(sorted(issue.get("tracks", []))) == tracks
    ]

    # An ambiguous or missing queue entry cannot authorize changes
    if len(matches) != 1:
        return None, None, None

    issue = matches[0]
    metadata = issue.get("decision")
    artwork = issue.get("artwork_decision")
    manual = issue.get("manual_fields")

    if metadata is None and artwork is None and not manual:
        return None, None, None

    # Older approvals without fingerprints require fresh review
    if issue.get("source_fingerprint") != review_fingerprint(issue):
        print(f"Saved approval needs fresh review: {album['album']}")
        return None, None, None

    if (
        metadata is not None
        and metadata.get("status") != "pending_application"
    ):
        return None, None, None

    if (
        artwork is not None
        and artwork.get("status") != "pending_application"
    ):
        return None, None, None

    print(f"Reusing approved decisions: {album['album']}")
    return metadata, artwork, tag.validate_manual_metadata(manual)

def auto_quarantine_archived_completed(originals_zip, library_zip):
    """Safely quarantine fully completed, archive-backed imports."""
    try:
        ledger = load_completed_reviews()
        staging = tag.staging_folder.resolve()
        completed = {}

        for key, album in tag.group_staged_albums().items():
            issue = {
                "import_name": key[0],
                "album": album["album"],
                "tracks": [
                    Path(track["path"]).resolve()
                    .relative_to(staging).as_posix()
                    for track in album["tracks"]
                ]
            }

            if is_review_complete(issue, ledger):
                fingerprint = review_fingerprint(issue)
                completed[key] = ledger[fingerprint]["receipt"]

        if not completed:
            return []

        # The sorter verifies archives, receipts and every
        # original file again before moving anything.
        # Run both cleanup routes independently. A failure in one
        # must not hide a successful quarantine in the other.
        moved = []

        for label, operation in (
            ("Archive-backed", sort.quarantine_archived_cleanup),
            ("Inbox-backed", sort.quarantine_inbox_cleanup),
        ):
            try:
                moved.extend(operation(
                    completed,
                    originals_zip,
                    library_zip,
                    approved=True
                ))
            except Exception as error:
                print(f"{label} cleanup deferred: {error}")

        return moved


    except Exception as error:
        print(f"\nAutomatic cleanup deferred: {error}")
        print("Originals and staged files retained for safety.")
        return []

def verify_archived_purge_candidates():
    """Verify quarantined imports before permitting any purge."""
    from collections import defaultdict
    import shutil
    import zipfile
    import constants

    backup = (
        Path(sort.script_folder)
        / constants.backup_folder_name
    ).resolve()
    root = backup / ".cleanup-quarantine"

    if not root.exists():
        return []

    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe quarantine directory.")

    ledger = load_completed_reviews()
    pending = {
        issue["import_name"]
        for issue in load_review_queue()
    }

    library = Path(sort.music_library_folder).resolve()
    live_files = sort.file_manifest(library)
    ready = []

    for folder in sorted(root.iterdir()):
        if not folder.name.startswith(".mixie-archived-"):
            continue

        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("Unsafe quarantine folder.")

        # Refuse unexpected files, including interrupted operations.
        if {p.name for p in folder.iterdir()} != {
            "recovery.json", "staging"
        }:
            raise RuntimeError(
                f"Unexpected quarantine contents: {folder}"
            )

        journal = folder / "recovery.json"
        held = folder / "staging"

        if journal.is_symlink() or held.is_symlink():
            raise ValueError("Unsafe quarantine contents.")

        with journal.open(encoding="utf-8") as file:
            record = json.load(file)

        name = record.get("import_name")

        if (
            record.get("version") != 1
            or not isinstance(name, str)
            or name in ("", ".", "..")
            or Path(name).name != name
            or "/" in name or "\\" in name
        ):
            raise ValueError("Invalid quarantine identity.")

        stage = Path(sort.staging_folder).resolve() / name

        if (
            record.get("original_staging_path") != str(stage)
            or stage.exists()
            or stage.is_symlink()
            or name in pending
        ):
            raise RuntimeError(
                f"Import not safe to purge: {name}"
            )

        staged = sort.file_manifest(held)

        if not staged or staged != record["staged_hashes"]:
            raise RuntimeError(
                f"Quarantine checksum mismatch: {name}"
            )

        # Verify the original ZIP still contains every staged file.
        bulk = Path(record["original_archive"])
        source_name = record["archived_source"]

        if (
            bulk.is_symlink()
            or bulk.resolve() != backup / "OriginalDownloads-Bulk.zip"
            or source_name != f"{name}.zip"
        ):
            raise RuntimeError(
                f"Invalid archived original: {name}"
            )

        archived = sort.zip_manifest(bulk)

        if archived.get(source_name) != record["archived_source_hash"]:
            raise RuntimeError(
                f"Original ZIP checksum mismatch: {name}"
            )

        with zipfile.ZipFile(bulk) as outer:
            with tempfile.TemporaryFile() as temporary:
                with outer.open(source_name) as source:
                    shutil.copyfileobj(
                        source, temporary, 1024 * 1024
                    )
                temporary.seek(0)
                original_files = sort.zip_manifest(temporary)

        if original_files != staged:
            raise RuntimeError(
                f"Archived originals differ: {name}"
            )

        snapshot = Path(record["library_snapshot"])

        if (
            snapshot.is_symlink()
            or snapshot.resolve().parent != backup
            or not snapshot.name.startswith("MusicLibrary-")
            or snapshot.suffix.lower() != ".zip"
        ):
            raise RuntimeError("Invalid library snapshot.")

        saved_files = sort.zip_manifest(snapshot)

        if saved_files != live_files:
            raise RuntimeError("Library snapshot differs from library.")

        # Reconstruct each album's original completion fingerprint.
        albums = defaultdict(list)

        for relative in staged:
            if Path(relative).suffix.lower() not in (
                constants.music_file_types
            ):
                continue

            track = tag.read_track_metadata(held / relative)

            if track is None:
                raise RuntimeError(f"Unreadable audio: {relative}")

            names = {
                value.strip()
                for value in track["album"]
                if value.strip()
            }

            if len(names) != 1:
                raise RuntimeError(
                    f"Ambiguous album metadata: {relative}"
                )

            album_key = " ".join(
                next(iter(names)).casefold().split()
            )
            albums[album_key].append(relative)

        if not albums:
            raise RuntimeError(f"No validated albums: {name}")

        for album_key, tracks in albums.items():
            identity = {
                "import_name": name,
                "tracks": sorted([
                    [f"{name}/{relative}", staged[relative]]
                    for relative in tracks
                ])
            }

            fingerprint = hashlib.sha256(
                json.dumps(
                    identity,
                    sort_keys=True,
                    ensure_ascii=False
                ).encode("utf-8")
            ).hexdigest()

            entry = ledger.get(fingerprint)

            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"Album not completed: {album_key}"
                )

            receipt = entry.get("receipt")

            if (
                not isinstance(receipt, dict)
                or len(receipt) < len(tracks) + 1
            ):
                raise RuntimeError(
                    f"Incomplete receipt: {album_key}"
                )

            for destination, digest in receipt.items():
                path = Path(destination)

                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(
                        f"Missing committed file: {destination}"
                    )

                relative = path.resolve().relative_to(
                    library
                ).as_posix()

                if (
                    live_files.get(relative) != digest
                    or saved_files.get(relative) != digest
                ):
                    raise RuntimeError(
                        f"Receipt mismatch: {relative}"
                    )

        ready.append({
            "name": name,
            "folder": folder,
            "files": len(staged),
            "albums": len(albums)
        })

        print(
            f"PURGE READY: {name} "
            f"({len(albums)} albums, {len(staged)} files)"
        )

    print("\nVERIFICATION ONLY: Nothing deleted.")
    return ready

def purge_verified_archived_quarantine(approved=False):
    """Purge archive-backed staging duplicates with restart recovery."""
    import os
    import shutil
    import tempfile
    import zipfile

    backup = Path(sort.script_folder) / constants.backup_folder_name
    root = backup / ".cleanup-quarantine"

    if backup.is_symlink() or root.is_symlink():
        raise RuntimeError("Unsafe backup path.")

    backup = backup.resolve()
    root = root.resolve()

    if not root.exists():
        return []

    if not root.is_dir():
        raise RuntimeError("Invalid quarantine directory.")

    def sync_dir(path):
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save_json(path, data):
        fd, temporary = tempfile.mkstemp(
            prefix=".mixie-write-", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_dir(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    folders = sorted(
        path for path in root.iterdir()
        if path.name.startswith(".mixie-archived-")
    )

    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise RuntimeError(f"Unsafe quarantine: {folder}")

    interrupted = [
        folder for folder in folders
        if (folder / "purge-state.json").exists()
        or (folder / "purge-state.json").is_symlink()
    ]

    if interrupted:
        selected = interrupted
        print("Interrupted purge found. Recovery verification required.")
    else:
        ready = verify_archived_purge_candidates()
        selected = [item["folder"] for item in ready]

    if not approved:
        print(f"Purge preview: {len(selected)} import(s)")
        print("Nothing deleted.")
        return selected

    completed = []

    for folder in selected:
        journal = folder / "recovery.json"
        state_path = folder / "purge-state.json"
        held = folder / "staging"
        resuming = state_path.exists()

        if (
            journal.is_symlink()
            or state_path.is_symlink()
            or held.is_symlink()
            or not journal.is_file()
        ):
            raise RuntimeError(f"Unsafe purge transaction: {folder}")

        allowed = {"recovery.json", "purge-state.json"}
        if held.exists():
            allowed.add("staging")

        if {p.name for p in folder.iterdir()} - allowed:
            raise RuntimeError("Unexpected files in purge transaction.")

        raw_journal = journal.read_bytes()
        journal_hash = hashlib.sha256(raw_journal).hexdigest()
        record = json.loads(raw_journal)

        name = record.get("import_name")

        if (
            record.get("version") != 1
            or not isinstance(name, str)
            or name in ("", ".", "..")
            or Path(name).name != name
            or "/" in name or "\\" in name
            or not isinstance(record.get("staged_hashes"), dict)
            or not record["staged_hashes"]
        ):
            raise RuntimeError("Invalid purge recovery journal.")

        stage_path = Path(sort.staging_folder).resolve() / name

        if (
            record.get("original_staging_path") != str(stage_path)
            or stage_path.exists()
            or stage_path.is_symlink()
        ):
            raise RuntimeError(f"Live staging conflict: {name}")

        ledger_path = tag.script_folder / ".completed_reviews.json"

        if ledger_path.is_symlink() or not ledger_path.is_file():
            raise RuntimeError("Completion ledger unavailable.")

        ledger_hash = hashlib.sha256(
            ledger_path.read_bytes()
        ).hexdigest()

        if resuming:
            state = json.loads(state_path.read_text(encoding="utf-8"))

            if (
                state.get("version") != 1
                or state.get("journal_sha256") != journal_hash
                or state.get("ledger_sha256") != ledger_hash
            ):
                raise RuntimeError(
                    f"Purge recovery evidence changed: {name}"
                )
        else:
            if not held.is_dir():
                raise RuntimeError("Staged quarantine missing.")

            if sort.file_manifest(held) != record["staged_hashes"]:
                raise RuntimeError("Staged checksum mismatch.")

            state = {
                "version": 1,
                "journal_sha256": journal_hash,
                "ledger_sha256": ledger_hash
            }

            # Persist recovery instructions BEFORE removing any file.
            save_json(state_path, state)

        # Independently verify recoverability from the original ZIP.
        bulk = Path(record["original_archive"])
        source_name = record["archived_source"]

        if (
            bulk.is_symlink()
            or bulk.resolve() != backup / "OriginalDownloads-Bulk.zip"
            or source_name != f"{name}.zip"
        ):
            raise RuntimeError("Unsafe archived-original reference.")

        outer_manifest = sort.zip_manifest(bulk)

        if outer_manifest.get(source_name) != (
            record["archived_source_hash"]
        ):
            raise RuntimeError("Archived original checksum mismatch.")

        with zipfile.ZipFile(bulk) as archive:
            with tempfile.TemporaryFile() as temporary:
                with archive.open(source_name) as source:
                    shutil.copyfileobj(source, temporary)
                temporary.seek(0)
                original_files = sort.zip_manifest(temporary)

        expected = record["staged_hashes"]

        if original_files != expected:
            raise RuntimeError("Original archive is not recoverable.")

        # The finished library and its independent backup must agree.
        snapshot = Path(record["library_snapshot"])

        if (
            snapshot.is_symlink()
            or snapshot.resolve().parent != backup
            or not snapshot.name.startswith("MusicLibrary-")
            or snapshot.suffix.lower() != ".zip"
        ):
            raise RuntimeError("Unsafe library backup reference.")

        if sort.zip_manifest(snapshot) != sort.file_manifest(
            sort.music_library_folder
        ):
            raise RuntimeError("Finished library backup mismatch.")

        if held.exists():
            if not held.is_dir():
                raise RuntimeError("Invalid remaining staging files.")

            remaining = sort.file_manifest(held)

            if any(
                expected.get(relative) != digest
                for relative, digest in remaining.items()
            ):
                raise RuntimeError("Unexpected or changed staging file.")
        else:
            if not resuming:
                raise RuntimeError("Missing staging before purge.")
            remaining = {}

        # Only delete filenames present in the verified journal.
        for relative in sorted(remaining):
            target = held / relative

            if (
                target.is_symlink()
                or not target.is_file()
                or sort.file_checksum(target).hex()
                    != expected[relative]
            ):
                raise RuntimeError(f"Changed file: {relative}")

            target.unlink()

        if held.exists():
            for directory in sorted(
                (p for p in held.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True
            ):
                if directory.is_symlink():
                    raise RuntimeError("Unexpected directory symlink.")
                directory.rmdir()

            held.rmdir()

        sync_dir(folder)

        # Preserve an audit receipt outside the deleted quarantine.
        receipt_dir = backup / ".cleanup-purge-receipts"

        if receipt_dir.is_symlink():
            raise RuntimeError("Unsafe purge receipt directory.")

        receipt_dir.mkdir(exist_ok=True)

        receipt_path = receipt_dir / f"{journal_hash}.json"
        receipt = {
            "version": 1,
            "import_name": name,
            "original_journal": record,
            "purge_state": state
        }

        if receipt_path.exists():
            if receipt_path.is_symlink():
                raise RuntimeError("Unsafe existing purge receipt.")
            with receipt_path.open(encoding="utf-8") as stream:
                if json.load(stream) != receipt:
                    raise RuntimeError("Conflicting purge receipt.")
        else:
            save_json(receipt_path, receipt)


        # Atomically move the completed transaction into the
        # permanent evidence directory. Keep its small journals.
        # A crash before this rename leaves a resumable purge;
        # a crash afterward leaves a completed audit record.
        if held.exists() or held.is_symlink():
            raise RuntimeError(
                "Cannot finalize purge while staged files remain."
            )

        evidence = receipt_dir / f"{journal_hash}.evidence"

        if evidence.exists() or evidence.is_symlink():
            raise RuntimeError(
                f"Conflicting completed purge evidence: {evidence}"
            )

        folder.rename(evidence)
        sync_dir(root)
        sync_dir(receipt_dir)


        completed.append(name)
        print(f"PURGED: {name} ({len(expected)} files)")

    return completed

def verify_inbox_purge_candidates():
    """Audit both quarantined copies before permanent Inbox cleanup."""
    from collections import defaultdict
    import constants

    backup = (
        Path(sort.script_folder)
        / constants.backup_folder_name
    )
    root = backup / ".cleanup-quarantine"

    if root.is_symlink():
        raise RuntimeError("Unsafe quarantine root.")

    if not root.exists():
        print("No Inbox-backed quarantines.")
        return []

    if not root.is_dir():
        raise RuntimeError("Invalid quarantine root.")

    folders = sorted(
        p for p in root.iterdir()
        if p.name.startswith(".mixie-inbox-")
    )

    if not folders:
        print("No Inbox-backed quarantines.")
        return []

    library = Path(sort.music_library_folder).resolve()
    live = sort.file_manifest(library)

    # Require a current, verified backup of the entire library.
    snapshots = sorted(
        backup.glob("MusicLibrary-*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    matching_snapshot = None
    for snapshot in snapshots:
        if snapshot.is_symlink() or not snapshot.is_file():
            continue
        if sort.zip_manifest(snapshot) == live:
            matching_snapshot = snapshot
            break

    if matching_snapshot is None:
        raise RuntimeError("No matching Music Library backup.")

    bulk = backup / "OriginalDownloads-Bulk.zip"
    if bulk.is_symlink() or not bulk.is_file():
        raise RuntimeError("Original download archive unavailable.")

    archived = sort.zip_manifest(bulk)
    ledger = load_completed_reviews()
    pending = {
        issue["import_name"] for issue in load_review_queue()
    }

    ready = []

    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise RuntimeError("Unsafe quarantine folder.")

        if {p.name for p in folder.iterdir()} != {
            "recovery.json", "staging", "original"
        }:
            raise RuntimeError(
                f"Incomplete Inbox quarantine: {folder}"
            )

        journal = folder / "recovery.json"
        held_stage = folder / "staging"
        held_source = folder / "original"

        if any(p.is_symlink() for p in (
            journal, held_stage, held_source
        )):
            raise RuntimeError("Unsafe quarantine contents.")

        record = json.loads(journal.read_text(encoding="utf-8"))

        name = record.get("import_name")
        source_name = record.get("source_name")

        def safe_name(value):
            return (
                isinstance(value, str)
                and value not in ("", ".", "..")
                and Path(value).name == value
                and "/" not in value
                and "\\" not in value
            )

        if (
            record.get("version") != 1
            or record.get("kind") != "inbox_cleanup"
            or not safe_name(name)
            or not safe_name(source_name)
            or type(record.get("source_is_dir")) is not bool
            or not isinstance(record.get("staged_hashes"), dict)
            or not isinstance(record.get("source_hashes"), dict)
            or not record["staged_hashes"]
        ):
            raise RuntimeError("Invalid Inbox recovery journal.")

        if name in pending:
            raise RuntimeError(f"Unresolved reviews: {name}")

        if (
            (Path(sort.staging_folder) / name).exists()
            or (Path(sort.inbox_folder) / source_name).exists()
        ):
            raise RuntimeError(f"Live import still exists: {name}")

        staged = sort.file_manifest(held_stage)
        if staged != record["staged_hashes"]:
            raise RuntimeError(f"Staged file mismatch: {name}")

        if record["source_is_dir"]:
            source_files = sort.file_manifest(held_source)
            prefix = source_name + "/"
            archived_source = {
                relative[len(prefix):]: digest
                for relative, digest in archived.items()
                if relative.startswith(prefix)
            }
        else:
            if not held_source.is_file():
                raise RuntimeError("Missing original download.")

            source_digest = sort.file_checksum(held_source).hex()

            if source_digest != record.get("source_sha256"):
                raise RuntimeError("Original download checksum mismatch.")

            if source_name.lower().endswith(".zip"):
                source_files = sort.zip_manifest(held_source)
            else:
                source_files = {source_name: source_digest}

            archived_source = source_files if (
                archived.get(source_name) == source_digest
            ) else {}

        if (
            source_files != record["source_hashes"]
            or archived_source != source_files
        ):
            raise RuntimeError(
                f"Original archive contents mismatch: {name}"
            )

        # Every staged audio file must belong to a completed album.
        albums = defaultdict(list)

        for relative in staged:
            if Path(relative).suffix.lower() not in (
                constants.music_file_types
            ):
                continue

            track = tag.read_track_metadata(
                held_stage / relative
            )
            if track is None:
                raise RuntimeError(f"Unreadable audio: {relative}")

            names = {
                value.strip()
                for value in track["album"]
                if value.strip()
            }
            if len(names) != 1:
                raise RuntimeError(
                    f"Ambiguous album metadata: {relative}"
                )

            key = " ".join(
                next(iter(names)).casefold().split()
            )
            albums[key].append(relative)

        if not albums:
            raise RuntimeError(f"No completed audio: {name}")

        for album_key, tracks in albums.items():
            identity = {
                "import_name": name,
                "tracks": sorted([
                    [f"{name}/{relative}", staged[relative]]
                    for relative in tracks
                ])
            }

            fingerprint = hashlib.sha256(
                json.dumps(
                    identity,
                    sort_keys=True,
                    ensure_ascii=False
                ).encode("utf-8")
            ).hexdigest()

            entry = ledger.get(fingerprint)

            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"Album completion missing: {album_key}"
                )

            receipt = entry.get("receipt")

            if (
                not isinstance(receipt, dict)
                or len(receipt) < len(tracks)
            ):
                raise RuntimeError(
                    f"Incomplete album receipt: {album_key}"
                )

            for destination, digest in receipt.items():
                path = Path(destination)
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(
                        f"Missing library file: {destination}"
                    )

                relative = path.resolve().relative_to(
                    library
                ).as_posix()

                if live.get(relative) != digest:
                    raise RuntimeError(
                        f"Library receipt mismatch: {relative}"
                    )

        ready.append({
            "name": name,
            "folder": folder,
            "albums": len(albums),
            "files": len(staged),
            "source_name": source_name
        })

        print(
            f"INBOX PURGE READY: {name} "
            f"({len(albums)} albums, {len(staged)} staged files)"
        )

    print(f"\nVerified Inbox purge candidates: {len(ready)}")
    print("VERIFICATION ONLY: Nothing deleted.")
    return ready

def purge_verified_inbox_quarantine(approved=False):
    """Purge verified Inbox/staging duplicates; resume interrupted purges."""
    import os
    import shutil
    import tempfile
    import zipfile
    import constants

    backup = Path(sort.script_folder) / constants.backup_folder_name
    root = backup / ".cleanup-quarantine"

    if backup.is_symlink() or root.is_symlink():
        raise RuntimeError("Unsafe backup or quarantine path.")

    if not root.exists():
        return []

    backup = backup.resolve()
    root = root.resolve()

    if not root.is_dir():
        raise RuntimeError("Invalid quarantine directory.")

    def sync_dir(path):
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save_json(path, value):
        fd, temporary = tempfile.mkstemp(
            prefix=".mixie-purge-", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_dir(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def safe_name(value):
        return (
            isinstance(value, str)
            and value not in ("", ".", "..")
            and Path(value).name == value
            and "/" not in value
            and "\\" not in value
        )

    folders = sorted(
        p for p in root.iterdir()
        if p.name.startswith(".mixie-inbox-")
    )

    seen_names = set()

    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise RuntimeError(f"Unsafe quarantine: {folder}")

        journal = folder / "recovery.json"

        if journal.is_symlink() or not journal.is_file():
            raise RuntimeError(f"Missing cleanup journal: {folder}")

        record = json.loads(journal.read_text(encoding="utf-8"))
        name = record.get("import_name")

        if not safe_name(name) or name in seen_names:
            raise RuntimeError("Unsafe or duplicate cleanup identity.")

        seen_names.add(name)

    interrupted = [
        p for p in folders
        if (p / "purge-state.json").exists()
        or (p / "purge-state.json").is_symlink()
    ]

    if interrupted:
        selected = interrupted
        print("Interrupted Inbox purge detected.")
    else:
        ready = verify_inbox_purge_candidates()
        selected = [item["folder"] for item in ready]

    if not approved:
        print(f"Inbox purge preview: {len(selected)} import(s)")
        print("Nothing deleted.")
        return selected

    purged = []

    for folder in selected:
        if folder.parent != root:
            raise RuntimeError("Unsafe purge candidate path.")

        journal = folder / "recovery.json"
        state_path = folder / "purge-state.json"
        held_stage = folder / "staging"
        held_source = folder / "original"

        if any(p.is_symlink() for p in (
            journal, state_path, held_stage, held_source
        )):
            raise RuntimeError("Unsafe purge transaction.")

        raw = journal.read_bytes()
        journal_hash = hashlib.sha256(raw).hexdigest()
        record = json.loads(raw)

        name = record.get("import_name")
        source_name = record.get("source_name")
        source_is_dir = record.get("source_is_dir")

        expected_stage = record.get("staged_hashes")
        expected_source = record.get("source_hashes")

        if (
            record.get("version") != 1
            or record.get("kind") != "inbox_cleanup"
            or not safe_name(name)
            or not safe_name(source_name)
            or type(source_is_dir) is not bool
            or not isinstance(expected_stage, dict)
            or not isinstance(expected_source, dict)
            or not expected_stage
            or expected_stage != expected_source
        ):
            raise RuntimeError(f"Invalid purge evidence: {folder}")

        # Both copies must represent exactly the same downloaded files.
        live_stage = Path(sort.staging_folder) / name
        live_source = Path(sort.inbox_folder) / source_name

        if any(
            p.exists() or p.is_symlink()
            for p in (live_stage, live_source)
        ):
            raise RuntimeError(f"Live import conflict: {name}")

        ledger_path = tag.script_folder / ".completed_reviews.json"

        if ledger_path.is_symlink() or not ledger_path.is_file():
            raise RuntimeError("Completion ledger unavailable.")

        ledger_hash = hashlib.sha256(
            ledger_path.read_bytes()
        ).hexdigest()

        resuming = state_path.exists()

        if resuming:
            state = json.loads(
                state_path.read_text(encoding="utf-8")
            )

            if (
                state.get("version") != 1
                or state.get("journal_sha256") != journal_hash
                or state.get("ledger_sha256") != ledger_hash
            ):
                raise RuntimeError(
                    f"Interrupted purge evidence changed: {name}"
                )
        else:
            state = None

        # Verify the archived original independently of quarantined files.
        bulk = backup / "OriginalDownloads-Bulk.zip"

        if bulk.is_symlink() or not bulk.is_file():
            raise RuntimeError("Original download archive missing.")

        bulk_hash = sort.file_checksum(bulk).hex()
        archived = sort.zip_manifest(bulk)

        if source_is_dir:
            prefix = source_name + "/"

            archived_source = {
                key[len(prefix):]: digest
                for key, digest in archived.items()
                if key.startswith(prefix)
            }

            if archived_source != expected_source:
                raise RuntimeError(
                    f"Original folder backup mismatch: {name}"
                )
        else:
            source_hash = record.get("source_sha256")

            if archived.get(source_name) != source_hash:
                raise RuntimeError(
                    f"Original download backup mismatch: {name}"
                )

            if source_name.lower().endswith(".zip"):
                with zipfile.ZipFile(bulk) as outer:
                    with tempfile.TemporaryFile() as temporary:
                        with outer.open(source_name) as stream:
                            shutil.copyfileobj(stream, temporary)
                        temporary.seek(0)

                        if sort.zip_manifest(temporary) != expected_source:
                            raise RuntimeError(
                                f"Original ZIP contents mismatch: {name}"
                            )
            elif expected_source != {source_name: source_hash}:
                raise RuntimeError(
                    f"Loose original file mismatch: {name}"
                )

        # Require an exact backup of the finished Music Library.
        library_files = sort.file_manifest(
            sort.music_library_folder
        )

        if resuming:
            snapshot = Path(state["library_snapshot"])

            if (
                snapshot.is_symlink()
                or snapshot.parent != backup
                or not snapshot.name.startswith("MusicLibrary-")
                or snapshot.suffix.lower() != ".zip"
                or not snapshot.is_file()
            ):
                raise RuntimeError("Unsafe saved library snapshot.")

            if sort.zip_manifest(snapshot) != library_files:
                raise RuntimeError("Library backup changed.")

            if (
                state.get("bulk_sha256") != bulk_hash
                or state.get("snapshot_sha256")
                    != sort.file_checksum(snapshot).hex()
            ):
                raise RuntimeError(
                    "Backup evidence changed during purge."
                )
        else:
            snapshot = None

            for candidate in sorted(
                backup.glob("MusicLibrary-*.zip")
            ):
                if candidate.is_symlink() or not candidate.is_file():
                    continue

                if sort.zip_manifest(candidate) == library_files:
                    snapshot = candidate
                    break

            if snapshot is None:
                raise RuntimeError(
                    "No verified Music Library snapshot."
                )

        def remaining_files(path, expected, must_be_dir):
            if not path.exists():
                if not resuming:
                    raise RuntimeError(
                        f"Missing copy before purge: {path}"
                    )
                return {}

            if path.is_symlink():
                raise RuntimeError(f"Unsafe symlink: {path}")

            if must_be_dir:
                if not path.is_dir():
                    raise RuntimeError(f"Expected directory: {path}")
                current = sort.file_manifest(path)
            else:
                if not path.is_file():
                    raise RuntimeError(f"Expected file: {path}")
                current = {
                    source_name: sort.file_checksum(path).hex()
                }

            if any(
                expected.get(relative) != digest
                for relative, digest in current.items()
            ):
                raise RuntimeError(
                    f"Unrecognized or changed file: {path}"
                )

            if not resuming and current != expected:
                raise RuntimeError(f"Incomplete original copy: {path}")

            return current

        staged_files = remaining_files(
            held_stage, expected_stage, True
        )
        original_files = remaining_files(
            held_source,
            expected_source if source_is_dir else {
                source_name: record.get("source_sha256")
            },
            source_is_dir
        )

        if not resuming:
            # This state must be durable before the first deletion.
            state = {
                "version": 1,
                "journal_sha256": journal_hash,
                "ledger_sha256": ledger_hash,
                "bulk_sha256": bulk_hash,
                "snapshot_sha256":
                    sort.file_checksum(snapshot).hex(),
                "library_snapshot": str(snapshot)
            }

        receipt_dir = backup / ".cleanup-purge-receipts"

        if receipt_dir.is_symlink():
            raise RuntimeError("Unsafe receipt directory.")

        receipt_dir.mkdir(exist_ok=True)

        evidence = receipt_dir / f"{journal_hash}.evidence"
        receipt_path = receipt_dir / f"{journal_hash}.json"

        if evidence.exists() or evidence.is_symlink():
            raise RuntimeError("Conflicting completed purge evidence.")

        receipt = {
            "version": 1,
            "import_name": name,
            "original_journal": record,
            "purge_state": state
        }

        if receipt_path.exists() or receipt_path.is_symlink():
            if receipt_path.is_symlink():
                raise RuntimeError("Unsafe purge receipt.")

            if json.loads(
                receipt_path.read_text(encoding="utf-8")
            ) != receipt:
                raise RuntimeError("Conflicting purge receipt.")

        if not resuming:
            save_json(state_path, state)

        def remove_tree(path, expected):
            if not path.exists():
                return

            actual = sort.file_manifest(path)

            if any(
                expected.get(relative) != digest
                for relative, digest in actual.items()
            ):
                raise RuntimeError("Unexpected file during deletion.")

            for relative in sorted(actual):
                target = path / relative

                if (
                    target.is_symlink()
                    or not target.is_file()
                    or sort.file_checksum(target).hex()
                        != expected[relative]
                ):
                    raise RuntimeError(
                        f"Changed file during purge: {relative}"
                    )

                target.unlink()

            directories = sorted(
                (p for p in path.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True
            )

            for directory in directories:
                if directory.is_symlink():
                    raise RuntimeError("Unsafe directory symlink.")
                directory.rmdir()

            path.rmdir()
            sync_dir(path.parent)

        # Delete only verified quarantine copies, never live music.
        remove_tree(held_stage, expected_stage)

        if source_is_dir:
            remove_tree(held_source, expected_source)
        elif held_source.exists():
            if (
                held_source.is_symlink()
                or not held_source.is_file()
                or sort.file_checksum(held_source).hex()
                    != record["source_sha256"]
            ):
                raise RuntimeError("Original download changed.")

            held_source.unlink()
            sync_dir(folder)

        if any(
            p.exists() or p.is_symlink()
            for p in (held_stage, held_source)
        ):
            raise RuntimeError("Quarantine copies remain.")

        if not receipt_path.exists():
            save_json(receipt_path, receipt)

        # Atomic finalization retains journals for future audits.
        folder.rename(evidence)
        sync_dir(root)
        sync_dir(receipt_dir)

        purged.append(name)
        print(f"INBOX PURGED: {name}")

    return purged

def auto_purge_verified_quarantines():
    """Purge completed imports; defer anything failing verification."""
    purged = []

    for label, operation in (
        ("Archive-backed", purge_verified_archived_quarantine),
        ("Inbox-backed", purge_verified_inbox_quarantine),
    ):
        try:
            results = operation(approved=True)
            purged.extend(results)

            if results:
                print(
                    f"{label} housekeeping complete: "
                    f"{len(results)} import(s)."
                )

        except Exception as error:
            print(
                f"{label} permanent cleanup deferred: {error}"
            )
            print(
                "Unverified files and recovery records "
                "have not been approved for removal."
            )

    return purged

def finish_prior_cleanup_before_ingest():
    """Resolve old quarantine BEFORE changing the bulk or library backups.

    This keeps restart journals compatible with the original immutable
    completion ledger and archive hashes needed for safe recovery.
    """
    backup = Path(sort.script_folder) / constants.backup_folder_name
    if backup.is_symlink():
        raise RuntimeError("Backup directory is a symbolic link.")
    root = backup / ".cleanup-quarantine"
    if root.is_symlink():
        raise RuntimeError("Quarantine root is a symbolic link.")
    if not root.exists():
        return
    if not root.is_dir():
        raise RuntimeError("Invalid quarantine root.")
    def outstanding():
        return sorted(
            p for p in root.iterdir()
            if p.name.startswith((".mixie-inbox-", ".mixie-archived-"))
        )
    if outstanding():
        print("Finishing previous verified cleanup before new imports...")
        auto_purge_verified_quarantines()
    if outstanding():
        raise RuntimeError(
            "Unfinished cleanup recovery remains; original evidence must "
            "be reconciled before starting another Ingest."
        )


def get_acoustid_client_key():
    """Use an explicit session key, otherwise a protected persistent key."""
    key = os.environ.get("ACOUSTID_CLIENT_KEY", "").strip()
    if key:
        return key

    saved = Path.home() / ".config" / "mixie" / "acoustid_key"
    if saved.is_symlink():
        raise RuntimeError("Refusing symlinked AcoustID configuration.")
    if not saved.exists():
        return None
    if not saved.is_file():
        raise RuntimeError("AcoustID configuration is not a regular file.")
    if saved.stat().st_mode & 0o077:
        raise PermissionError("AcoustID key must have 0600 permissions.")
    return saved.read_text(encoding="utf-8").strip() or None


def main():
    args = sys.argv[1:]
    target = None
    test_copies = "--test-copy" in args
    commit = "--commit" in args
    auto_ingest = False

    # Open the interactive menu when no arguments are supplied
    if not args:
        print("\n================================")
        print("  M.I.X.I.E. - Music Manager")
        print("================================")
        print("1. Ingest Music")
        print("2. Repair Library")
        print("3. Sync Device")
        print("4. Review Pending Albums")
        print("5. Exit")
        choice = input("\nSelect an option (1-5): ").strip()

        if choice == "1":
            auto_ingest = True
            print("\nAutomatically ingesting verified music...")
            try:
                finish_prior_cleanup_before_ingest()
            except Exception as error:
                print(f"Ingest stopped before staging: {error}")
                return
            sort.sort_inbox()
        elif choice == "2":
            print("\nAuditing Music Library...")
            tag.main(tag.library_folder)
            return
        elif choice == "3":
            print("\nDevice synchronization is not connected yet.")
            print("No files have been changed.")
            return
        elif choice == "4":
            try:
                review_queue_menu(load_review_queue())
            except (OSError, ValueError) as error:
                print(f"\nCannot load review queue: {error}")
            return
        elif choice == "5":
            print("\nGoodbye!")
            return

        else:
            print("\nInvalid option.")
            return

    # Preserve our existing command-line testing options
    elif "--stage-only" in args:
        sort.sort_inbox()
        return
    else:
        target = next(
            (arg.casefold() for arg in args if not arg.startswith("--")),
            None
        )

    # Identify and preview staged albums
    api_key = get_acoustid_client_key()
    if not api_key:
        print("AcoustID client key is not configured.")
        return
    albums = tag.group_staged_albums()
    if commit:
        matches = [
            album for album in albums.values()
            if target and album["album"].casefold() == target
        ]

        if len(matches) != 1:
            print("Commit requires exactly one named staged album.")
            return

    if not albums:
        print("No staged albums found.")
        if auto_ingest:
            print("Checking previously verified cleanup transactions...")
            auto_purge_verified_quarantines()
        return

    completed = {}
    review_queue = []
    pending_issues = (
        load_review_queue() if auto_ingest else []
    )
    completed_entries = (
        load_completed_reviews() if auto_ingest else {}
    )


    for key, album in albums.items():
        if target and album["album"].casefold() != target:
            continue

        try:
            if auto_ingest:
                source_issue = {
                    "import_name": key[0],
                    "tracks": [
                        str(
                            Path(track["path"]).resolve()
                            .relative_to(tag.staging_folder.resolve())
                        )
                        for track in album["tracks"]
                    ]
                }

                if is_review_complete(
                    source_issue, completed_entries
                ):
                    print(
                        f"Previously completed; skipping: "
                        f"{album['album']}"
                    )
                    continue

            reviewed = None
            approved_artwork = None
            approved_manual = None

            if auto_ingest:
                reviewed, approved_artwork, approved_manual = reusable_review_decisions(
                    key, album, pending_issues
                )

            plan = preview_album(
                album,
                api_key,
                reviewed_decision=reviewed,
                artwork_decision=approved_artwork,
                manual_fields=approved_manual
            )

            if test_copies or commit or auto_ingest:
                receipt = test_album_plan(
                    plan,
                    album,
                    commit=commit or auto_ingest,
                    auto=auto_ingest
                )

                if auto_ingest and receipt:
                    completed[key] = receipt

        except Exception as error:
            print(f"\nNeeds review: {album['album']}")
            print(f"Reason: {error}")

            details = {}

            if isinstance(error, tag.MetadataConflictError):
                details = {
                    "kind": "metadata_conflict",
                    "field": error.field,
                    "existing_values": error.existing,
                    "reference_value": error.expected,
                    "track": Path(error.path).name
                }

            elif isinstance(error, ArtworkReviewRequired):
                details = {
                    "kind": "artwork_conflict",
                    "candidates": error.candidates,
                    "release_ids": error.release_ids
                }
            elif isinstance(error, tag.RequiredMetadataReview):
                details = {
                    "kind": "required_metadata",
                    "fields": error.fields
                }

            review_queue.append({
                "import_name": key[0],
                "details": details,
                "album": album["album"],
                "status": "needs_review",
                "error_type": type(error).__name__,
                "reason": str(error),
                "tracks": [
                    str(Path(track["path"]).relative_to(
                        tag.staging_folder
                    ))
                    for track in album["tracks"]
                ]
            })

    # Archive and clean only after batch processing completes
    if auto_ingest:
        print("\nFinalizing ingestion archives...")

        try:
            review_queue = reconcile_review_queue(review_queue)
            review_path = save_review_queue(review_queue)
            print(
                f"\nReview queue saved: "
                f"{len(review_queue)} album(s)"
            )
            print(f"Review file: {review_path}")
            originals_zip = archive.update_original_downloads_archive()
            library_zip = archive.create_verified_zip(
                sort.music_library_folder,
                "MusicLibrary"
            )

            # Save completion evidence for automatically imported albums.
            # Record successfully imported albums after archive verification
            for key, receipt in completed.items():
                album = albums[key]

                completed_issue = {
                    "import_name": key[0],
                    "album": album["album"],
                    "tracks": [
                        Path(track["path"]).resolve()
                        .relative_to(tag.staging_folder.resolve())
                        .as_posix()
                        for track in album["tracks"]
                    ]
                }

                record_completed_review(
                    completed_issue,
                    receipt,
                    originals_zip,
                    library_zip
                )

            # Remove completed albums from pending reviews
            review_queue = reconcile_review_queue(review_queue)
            save_review_queue(review_queue)

            print(
                f"Pending after verified imports: "
                f"{len(review_queue)}"
            )

            # Clean completed imports using the verified ledger,
            # including albums finished during earlier sessions.
            cleaned = auto_quarantine_archived_completed(
                originals_zip,
                library_zip
            )
            purged = auto_purge_verified_quarantines()
            print(
                f"\nIngestion finalized: "
                f"{len(completed)} albums verified, "
                f"{len(purged)} source imports permanently cleaned, "
                f"{len(cleaned) - len(purged)} awaiting safe cleanup."
            )

        except Exception as error:
            print(f"\nArchiving or cleanup deferred: {error}")
            print("Unfinished imports have not been approved for cleanup.")

        # Offer review only after automatic ingestion finishes
        if review_queue:
            print(
                f"\nMIXIE: {len(review_queue)} album(s) "
                "need your attention."
            )

            try:
                choice = input(
                    "Would you like to review them now? (y/N): "
                ).strip().casefold()
            except (KeyboardInterrupt, EOFError):
                choice = "n"

            if choice in ("y", "yes"):
                review_queue_menu(review_queue)
            else:
                print("Review postponed. Files remain in staging.")

if __name__ == "__main__":
    main()
