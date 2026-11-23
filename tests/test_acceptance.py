"""Offline end-to-end acceptance: synthetic silence, actual ingest and recovery.

Run only with tests/run_isolated.py. The only application function doubles are
fingerprint/AcoustID/MusicBrainz/art transports. Filesystem obstructions exercise
archive and purge failures without replacing storage, ledger or cleanup code.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
import hashlib
import importlib
import json
import os
import shutil
import sys
import unittest
import uuid
import zipfile
from unittest.mock import patch

from mutagen import File
from mutagen.flac import FLAC

import audio
import archive
import lookup
import mixie
import review
import sort
import tag
from support import WorkspaceCase, make_audio, make_cover


class _ProviderResponse(BytesIO):
    def __init__(self, data, mime):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = mime


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class AcceptanceTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.releases = {}
        self.recordings = {}
        self.specs = {}
        self.unresolved = set()
        self.calls = []
        self.unexpected = []
        self.output = ""
        self.cover = make_cover()
        self.delivery_count = 0
        self.clear_provider_caches()
        self.addCleanup(self.clear_provider_caches)
        self.stack.enter_context(patch.object(audio, "script_folder", self.script))
        self.stack.enter_context(patch.object(audio, "staging_folder", self.stage))
        self.stack.enter_context(patch.object(audio, "DECODE_TIMEOUT", 10))
        self.stack.enter_context(patch.object(audio, "fingerprint_audio", side_effect=self.fingerprint))
        self.stack.enter_context(patch.object(audio, "identify_audio", side_effect=self.acoustid))
        self.stack.enter_context(patch.object(lookup, "_musicbrainz_call", side_effect=self.musicbrainz))
        self.stack.enter_context(patch.object(lookup, "urlopen", side_effect=self.art_transport))
        self.stack.enter_context(patch.object(audio, "urlopen", side_effect=lambda *a, **k: self.forbidden("audio.urlopen")))
        self.addCleanup(self.assert_no_surprise_providers)

    def clear_provider_caches(self):
        for function in (lookup.get_recording_details, lookup.get_release_tracklist, lookup.get_release_metadata):
            function.cache_clear()

    def assert_no_surprise_providers(self):
        self.assertEqual(self.unexpected, [], self.output)

    def forbidden(self, description):
        self.unexpected.append(description)
        raise AssertionError("Unexpected provider call: " + description)

    def identifier(self, value):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self.id() + "/" + value))

    def register_release(self, label, *, sizes=(2,), artist="Fixture Artist"):
        identifier = self.identifier("release/" + label)
        tracks, media = [], []
        for disc, size in enumerate(sizes, 1):
            remote = []
            for number in range(1, size + 1):
                recording_id = self.identifier(f"recording/{label}/{disc}/{number}")
                title = f"{label} Song {disc}-{number}"
                spec = {"recording_id": recording_id, "release_id": identifier, "title": title,
                        "artist": artist, "album": label, "disc": disc, "number": number,
                        "track_total": size, "disc_total": len(sizes)}
                tracks.append(spec)
                self.specs[recording_id] = spec
                self.recordings[recording_id] = {
                    "id": recording_id, "title": title, "artist-credit-phrase": artist,
                    "artist-credit": [{"artist": {"id": self.identifier("artist/" + artist), "name": artist}}],
                    "length": "150", "release-count": "1", "release-list": [
                        {"id": identifier, "title": label, "release-group": {"primary-type": "Album"}}],
                }
                remote.append({"id": self.identifier(f"track/{label}/{disc}/{number}"), "number": str(number),
                               "position": str(number), "title": title, "length": "150",
                               "recording": {"id": recording_id, "title": title, "length": "150"}})
            media.append({"position": str(disc), "format": "CD", "track-count": str(size), "track-list": remote})
        self.releases[identifier] = {
            "id": identifier, "title": label, "artist-credit-phrase": artist, "date": "2020",
            "release-group": {"primary-type": "Album"}, "medium-count": str(len(media)), "medium-list": media,
        }
        return {"id": identifier, "album": label, "artist": artist, "tracks": tracks}

    def fingerprint(self, path):
        path = Path(path)
        if not path.resolve().is_relative_to(self.stage) or path.stem not in self.specs:
            return self.forbidden("fingerprint outside known managed-stage fixture: " + str(path))
        self.calls.append(("fingerprint", path.stem))
        return {"duration": 0.15, "fingerprint": "offline-fixture:" + path.stem}

    def acoustid(self, fingerprint, api_key):
        self.assertEqual(api_key, "DISPOSABLE-OFFLINE-TEST-KEY")
        token = fingerprint.get("fingerprint", "")
        if not token.startswith("offline-fixture:") or token.split(":", 1)[1] not in self.specs:
            return self.forbidden("AcoustID unknown fingerprint")
        identifier = token.split(":", 1)[1]
        self.calls.append(("acoustid", identifier))
        return [] if identifier in self.unresolved else [{"score": 0.99, "recordings": [{"id": identifier}]}]

    def musicbrainz(self, provider, *args, **kwargs):
        name = provider.__name__
        if len(args) != 1:
            return self.forbidden("MusicBrainz argument inventory: " + name)
        identifier = args[0]
        self.calls.append((name, identifier))
        if name == "get_recording_by_id" and identifier in self.recordings:
            self.assertEqual(kwargs, {"includes": ["artist-credits", "releases"]})
            return {"recording": deepcopy(self.recordings[identifier])}
        if name == "get_release_by_id" and identifier in self.releases:
            if kwargs.get("includes") not in (["recordings"], ["artists", "release-groups"], ["release-groups"]):
                return self.forbidden("MusicBrainz unexpected release includes: " + repr(kwargs))
            return {"release": deepcopy(self.releases[identifier])}
        return self.forbidden(f"MusicBrainz {name}: {identifier}")

    def art_transport(self, request, *, timeout):
        url = urlsplit(request.full_url)
        self.calls.append(("art", request.full_url))
        if url.scheme == "https" and url.netloc == "coverartarchive.org":
            identifier = url.path.removeprefix("/release/")
            if identifier in self.releases and timeout == 20:
                image = f"https://coverartarchive.org/release/{identifier}/front"
                data = {"images": [{"front": True, "approved": True, "image": image,
                                    "thumbnails": {"250": image, "500": image}}]}
                return _ProviderResponse(json.dumps(data).encode(), "application/json")
        if url.scheme == "https" and url.netloc == "coverartarchive.org" and timeout in (8, 30):
            identifier = url.path.removeprefix("/release/").removesuffix("/front")
            if identifier in self.releases:
                return _ProviderResponse(self.cover["data"], self.cover["mime"])
        return self.forbidden("art URL/timeout: " + request.full_url)

    def delivery(self, name, tracks, *, kind="directory", wrong_titles=False, covers=True, filenames=None):
        self.delivery_count += 1
        build = self.root / "fixture-build" / str(self.delivery_count)
        build.mkdir(parents=True)
        sources, source_titles = {}, {}
        for index, spec in enumerate(tracks):
            suffix = (".flac", ".mp3")[index % 2]
            filename = (filenames or {}).get(spec["recording_id"], spec["recording_id"] + suffix)
            relative = f"{spec['album']}/Disc {spec['disc']}/{filename}"
            title = "Wrong source title" if wrong_titles else spec["title"]
            make_audio(build / relative, title=title, artist=spec["artist"], album=spec["album"],
                       albumartist=spec["artist"], tracknumber=spec["number"], discnumber=spec["disc"],
                       date="2020", genre="Rock")
            sources[relative] = spec
            source_titles[relative] = title
        for album in {spec["album"] for spec in tracks}:
            folder = build / album
            if covers:
                for filename in ("cover.png", "front.png"):
                    (folder / filename).write_bytes(self.cover["data"])
            (folder / "notes.txt").write_bytes(b"Synthetic original download notes; preserve verbatim.\n")
        payload = {path.relative_to(build).as_posix(): path.read_bytes() for path in build.rglob("*") if path.is_file()}
        source = self.inbox / name
        if kind == "directory":
            shutil.copytree(build, source)
            raw = {f"{name}/{relative}": data for relative, data in payload.items()}
        elif kind == "zip":
            with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as stream:
                for relative, data in sorted(payload.items()):
                    stream.writestr(relative, data)
            raw = {name: source.read_bytes()}
        else:
            self.fail("Unsupported fixture source kind")
        return {"name": name, "source": source, "kind": kind, "tracks": sources, "payload": payload,
                "hashes": {relative: _sha(data) for relative, data in payload.items()}, "raw": raw,
                "source_titles": source_titles, "import_name": sort.staging_import_name(name, kind)}

    def restart(self):
        # Reload orchestration and clear provider snapshots; durable files are not reconstructed.
        self.clear_provider_caches()
        importlib.reload(mixie)

    def run_main(self, *, stage_only=False):
        self.clear_provider_caches()
        def answer(prompt):
            if "Select an option" in prompt:
                return "1"
            if "Would you like to review them now?" in prompt:
                return "n"
            raise AssertionError("Unexpected interactive prompt: " + prompt)
        # These disposable cases deliberately exercise archive-gated purge.
        argv = (["mixie.py", "--stage-only"] if stage_only else
                ["mixie.py", "--allow-source-cleanup"])
        with patch.object(sys, "argv", argv), patch("builtins.input", side_effect=answer), redirect_stdout(StringIO()) as output:
            mixie.main()
        self.output = output.getvalue()
        self.assertEqual(self.unexpected, [], self.output)
        self.assertEqual(list(self.root.glob(".mixie-work-*")), [], self.output)
        return self.output

    def finalize(self, issue, issues):
        with patch("builtins.input", side_effect=AssertionError("Reviewed commit must not reprompt")), redirect_stdout(StringIO()) as output:
            try:
                return mixie.finalize_reviewed_album(issue, issues, allow_source_cleanup=True)
            except Exception as error:
                error.add_note("Full managed-stage album inventory: " + repr(issue["tracks"]))
                error.add_note("Captured real finalization output:\n" + output.getvalue())
                raise
            finally:
                self.output = output.getvalue()

    def assert_raw_archive(self, delivery, path=None):
        path = path or self.backup / "OriginalDownloads-Bulk.zip"
        with zipfile.ZipFile(path) as stream:
            self.assertIsNone(stream.testzip())
            for relative, data in delivery["raw"].items():
                self.assertEqual(stream.read(relative), data, relative)

    def assert_sources_retained(self, delivery):
        source = delivery["source"]
        self.assertTrue(source.exists(), self.output)
        if delivery["kind"] == "directory":
            self.assertEqual(sort.file_manifest(source), delivery["hashes"], self.output)
            for relative, data in delivery["payload"].items():
                self.assertEqual((source / relative).read_bytes(), data)
        else:
            self.assertEqual(source.read_bytes(), delivery["raw"][delivery["name"]])
        stage = self.stage / delivery["import_name"]
        self.assertTrue(sort.is_committed_stage(stage), self.output)
        self.assertEqual(sort.file_manifest(stage), delivery["hashes"], self.output)
        for relative, data in delivery["payload"].items():
            self.assertEqual((stage / relative).read_bytes(), data)

    def current_entry(self, delivery):
        provenance = sort.staging_provenance(delivery["import_name"], verify_payload=False)
        entries = [entry for entry in mixie.load_completed_reviews().values()
                   if entry["source_manifest"]["import_name"] == delivery["import_name"]
                   and entry["source_manifest"]["provenance"]["transaction_id"] == provenance["transaction_id"]]
        self.assertEqual(len(entries), 1, self.output)
        return entries[0]

    def assert_completed_entry(self, delivery, *, entry=None, decision=None, partial=False):
        entry = self.current_entry(delivery) if entry is None else entry
        manifest = entry["source_manifest"]
        self.assertEqual(entry["evidence_version"], 2)
        self.assertEqual(manifest["import_name"], delivery["import_name"])
        self.assertEqual(manifest["payload_hashes"], delivery["hashes"])
        self.assertEqual(manifest["provenance"]["source_name"], delivery["name"])
        self.assertEqual(manifest["provenance"]["source_kind"], delivery["kind"])
        self.assertEqual(manifest["provenance"]["state"], "committed")
        expected_inputs = {f"{delivery['import_name']}/{relative}" for relative in delivery["tracks"]}
        self.assertEqual(set(entry["input_map"]), expected_inputs)
        self.assertEqual(set(entry["tracks"]), expected_inputs)
        outputs = {row["destination"] for row in entry["input_map"].values()}
        self.assertTrue(entry["asset_outputs"])
        self.assertEqual(set(entry["receipt"]), outputs | set(entry["asset_outputs"]))
        self.assertEqual(entry["evidence"]["partial"], partial)
        self.assertEqual(entry["evidence"]["provenance"], "user_approved" if decision else "independently_verified")
        with zipfile.ZipFile(entry["library_archive"]) as archived:
            self.assertIsNone(archived.testzip())
            for destination, digest in entry["receipt"].items():
                path = Path(destination)
                self.assertTrue(path.is_relative_to(self.library))
                self.assertFalse(path.is_symlink())
                self.assertEqual(_sha(path.read_bytes()), digest)
                self.assertEqual(archived.read(path.relative_to(self.library).as_posix()), path.read_bytes())
        for relative, spec in delivery["tracks"].items():
            source = f"{delivery['import_name']}/{relative}"
            row = entry["input_map"][source]
            self.assertEqual(row["source_sha256"], _sha(delivery["payload"][relative]))
            self.assertEqual(row["destination_sha256"], entry["receipt"][row["destination"]])
            saved = tag.read_track_metadata(row["destination"])
            self.assertIsNotNone(saved)
            chosen = decision["tracks"][source] if decision else spec
            self.assertEqual(saved["title"], (chosen["title"],))
            self.assertEqual(saved["artist"], (chosen["artist"],))
            self.assertEqual(saved["album"], (decision["album"] if decision else spec["album"],))
            self.assertEqual(saved["album_artist"], (decision["album_artist"] if decision else spec["artist"],))
            self.assertEqual(saved["date"], (decision.get("date", "2020") if decision else "2020",))
            self.assertEqual(saved["genre"], (decision.get("genre", "Rock") if decision else "Rock",))
            self.assertEqual(tag.parse_track_number(saved["track_number"])[0], f"{chosen['number']:02}")
            totals = decision["totals"][source] if decision and decision["release_id"] is None else spec
            self.assertEqual(tag.get_track_totals(saved), {totals["track_total"]})
            if totals["disc_total"] == 1:
                # v5 Rockbox format: single-disc library outputs have no disc tags.
                self.assertEqual(saved["disc_number"], ())
                self.assertEqual(tag.get_disc_totals(saved), set())
            else:
                self.assertEqual(tag.parse_disc_number(saved["disc_number"])[0], chosen["disc"])
                self.assertEqual(tag.get_disc_totals(saved), {totals["disc_total"]})
            self.assertIs(row["identity"]["verified"], decision is None)
            self.assertIs(row["identity"]["user_approved"], decision is not None)
            audio_file = File(row["destination"])
            pictures = audio_file.pictures if isinstance(audio_file, FLAC) else audio_file.tags.getall("APIC")
            fronts = [picture for picture in pictures if picture.type == 3]
            self.assertEqual(len(fronts), 1)
            self.assertEqual((fronts[0].mime, fronts[0].data), (self.cover["mime"], self.cover["data"]))
        self.assert_raw_archive(delivery, Path(entry["originals_archive"]))
        backups = []
        for record in self.backup.glob("*/manifest.json"):
            data = json.loads(record.read_text())
            if (data.get("import_name") == delivery["import_name"]
                    and data.get("staging_provenance") == manifest["provenance"]):
                backups.append(record.parent / "original")
        self.assertEqual(len(backups), 1)
        self.assertEqual(sort.file_manifest(backups[0]), delivery["hashes"])
        for relative, data in delivery["payload"].items():
            self.assertEqual((backups[0] / relative).read_bytes(), data)
        return entry

    def audit_snapshot(self):
        root = self.backup / ".cleanup-purge-receipts"
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def storage_identity(self, root):
        # Ignore read-induced atime changes, but detect mutation, replacement or moves.
        snapshot = {}
        for path in (root, *root.rglob("*")):
            info = path.stat()
            snapshot[path.relative_to(root).as_posix()] = (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return snapshot

    def assert_purged(self, delivery, entry=None):
        entry = self.current_entry(delivery) if entry is None else entry
        self.assertFalse(delivery["source"].exists(), self.output)
        self.assertFalse((self.stage / delivery["import_name"]).exists(), self.output)
        receipt_root = self.backup / ".cleanup-purge-receipts"
        receipts = []
        for path in receipt_root.glob("*.json"):
            receipt = json.loads(path.read_text())
            if (receipt["import_name"] == delivery["import_name"] and
                    receipt["original_journal"]["staging_provenance"] == entry["source_manifest"]["provenance"]):
                receipts.append((path, receipt))
        self.assertEqual(len(receipts), 1, self.output)
        path, receipt = receipts[0]
        evidence = path.with_suffix(".evidence")
        self.assertEqual({p.name for p in evidence.iterdir()}, {"recovery.json", "purge-state.json"})
        self.assertEqual(json.loads((evidence / "recovery.json").read_text()), receipt["original_journal"])
        self.assertEqual(json.loads((evidence / "purge-state.json").read_text()), receipt["purge_state"])
        self.assertEqual(receipt["original_journal"]["staged_hashes"], delivery["hashes"])
        self.assertEqual(list((self.backup / ".cleanup-quarantine").iterdir()), [])
        self.assertEqual(mixie.load_review_queue(), [], self.output)

    def only_issue(self, delivery):
        issues = mixie.load_review_queue()
        self.assertEqual(len(issues), 1, self.output)
        issue = issues[0]
        self.assertEqual(issue["import_name"], delivery["import_name"])
        self.assertEqual(set(issue["tracks"]), {f"{delivery['import_name']}/{relative}" for relative in delivery["tracks"]})
        return issue, issues

    def approve(self, delivery, *, online, totals=None, manual=False):
        issue, issues = self.only_issue(delivery)
        self.assert_sources_retained(delivery)
        inventory = [album for (name, _), album in tag.group_staged_albums().items()
                     if name == issue["import_name"] and
                     {track["path"].relative_to(self.stage).as_posix() for track in album["tracks"]} == set(issue["tracks"])]
        self.assertEqual(len(inventory), 1)
        fingerprint = mixie.review_fingerprint(issue)
        first = next(iter(delivery["tracks"].values()))
        decision = {"status": "pending_application", "user_approved": True, "independently_verified": False,
                    "source_fingerprint": fingerprint, "release_id": first["release_id"] if online else None,
                    "album": "Hand-curated collection" if manual else first["album"],
                    "album_artist": "Explicit collection credit" if manual else first["artist"],
                    "partial": True, "tracks": {}}
        for relative, spec in delivery["tracks"].items():
            source = f"{delivery['import_name']}/{relative}"
            decision["tracks"][source] = {
                "recording_id": spec["recording_id"] if online else None,
                "title": "Explicit " + spec["title"] if manual else spec["title"],
                "artist": "Explicit track credit" if manual else spec["artist"],
                "disc": spec["disc"], "number": spec["number"],
            }
        if manual:
            decision.update(date="2024-02-29", genre="Synthpop")
        if totals is not None:
            decision["totals"] = {source: dict(totals) for source in issue["tracks"]}
        prepared = review.prepare_interpretation(inventory[0], decision, fingerprint)
        self.assertEqual(set(prepared["matched"].values()), {track["path"] for track in inventory[0]["tracks"]})
        self.assertEqual(prepared["provenance"], "user_approved")
        self.assertTrue(all(identity["verified"] is False and identity["user_approved"] is True
                            for identity in prepared["identities"].values()))
        self.assertEqual(mixie.review_fingerprint(issue), fingerprint)
        issue.update(source_fingerprint=fingerprint, interpretation=decision)
        mixie.save_review_queue(issues)
        return issue, issues, decision, prepared

    def mixed_unresolved_delivery(self):
        complete = self.register_release("Completed album")
        unresolved = self.register_release("Unresolved album")
        self.unresolved.update(track["recording_id"] for track in unresolved["tracks"])
        delivery = self.delivery("Multi-album source", complete["tracks"] + unresolved["tracks"])
        return delivery, complete, unresolved

    def test_v10e_archive_pending_reimport_reconciles_same_encoded_audio_and_repairs_tags(self):
        """A missing archive receipt must not turn a correct repeat into manual review."""
        release = self.register_release('Already Imported Album', sizes=(2,))
        delivered = self.delivery('Audio Redelivery', release['tracks'])
        with patch.object(archive, 'update_original_downloads_archive',
                          side_effect=RuntimeError('synthetic original archive unavailable')):
            first = self.run_main()
        self.assertIn('archive pending', first.lower())
        library_track = next(self.library.rglob('*.flac'))
        from mutagen import File
        previously_imported = File(library_track, easy=True)
        previously_imported['title'] = ['Incorrect older title']
        previously_imported.save()
        stale_hash = sort.file_checksum(library_track)
        second = self.run_main()
        self.assertIn('Verifying and importing Already Imported Album complete', second)
        self.assertEqual(('Already Imported Album Song 1-1',), tag.read_track_metadata(library_track)['title'])
        self.assertNotEqual(stale_hash, sort.file_checksum(library_track))
        self.assert_raw_archive(delivered)
        self.assertTrue(list(self.backup.glob('library-metadata-upgrade-*')))
        self.assertEqual([], mixie.load_review_queue(), second)
        self.current_entry(delivered)

    def test_complete_multiformat_import_uses_real_decode_commit_archives_and_purge(self):
        release = self.register_release("Complete mixed formats", artist="Halestorm & I Prevail")
        delivery = self.delivery("Mixed formats", release["tracks"], covers=False)
        self.run_main()
        self.assertEqual(mixie.load_review_queue(), [], self.output)
        self.assertEqual(len(mixie.load_completed_reviews()), 1, self.output)
        entry = self.assert_completed_entry(delivery)
        self.assertEqual({Path(row["destination"]).suffix for row in entry["input_map"].values()}, {".flac", ".mp3"})
        self.assertTrue(any(call[0] == "art" for call in self.calls))
        self.assertIn("Success: 1", self.output)
        self.assertIn("Errors: 0", self.output)
        self.assert_purged(delivery, entry)

    def test_wrong_titles_are_corrected_but_archived_raw_zip_and_backup_tags_are_untouched(self):
        release = self.register_release("Title repair", artist="SAMURAI")
        delivery = self.delivery("Title repair.zip", release["tracks"], kind="zip", wrong_titles=True)
        self.run_main()
        entry = self.assert_completed_entry(delivery)
        self.assert_purged(delivery, entry)
        with zipfile.ZipFile(entry["originals_archive"]) as bulk:
            raw_zip = bulk.read(delivery["name"])
        self.assertEqual(raw_zip, delivery["raw"][delivery["name"]])
        with zipfile.ZipFile(BytesIO(raw_zip)) as original:
            for relative in delivery["tracks"]:
                audio_file = File(BytesIO(original.read(relative)), easy=True)
                self.assertEqual(tuple(audio_file["title"]), ("Wrong source title",))
                self.assertEqual(tuple(audio_file["artist"]), ("SAMURAI",))

    def test_same_stem_directory_and_zip_have_distinct_managed_provenance_and_completion(self):
        directory_release = self.register_release("Directory album")
        zip_release = self.register_release("ZIP album")
        directory = self.delivery("Delivery", directory_release["tracks"])
        zipped = self.delivery("Delivery.zip", zip_release["tracks"], kind="zip")
        self.run_main()
        self.assertEqual(len(mixie.load_completed_reviews()), 2, self.output)
        entries = [self.assert_completed_entry(delivery) for delivery in (directory, zipped)]
        self.assertNotEqual(entries[0]["source_manifest"]["import_name"], entries[1]["source_manifest"]["import_name"])
        self.assertNotEqual(entries[0]["source_manifest"]["provenance"]["transaction_id"],
                            entries[1]["source_manifest"]["provenance"]["transaction_id"])
        for delivery, entry in zip((directory, zipped), entries):
            self.assert_purged(delivery, entry)

    def test_exact_legacy_stage_is_adopted_in_place_preserves_history_and_ingests_idempotently(self):
        historical = self.delivery("Historical synthetic delivery", self.register_release(
            "Historical synthetic album", sizes=(1,))["tracks"])
        self.run_main()
        self.assert_purged(historical, self.assert_completed_entry(historical))
        old_entries = deepcopy(mixie.load_completed_reviews())
        old_provenance = sort.staging_provenance(historical["import_name"], verify_payload=False)
        old_audit = self.audit_snapshot()

        release = self.register_release("Synthetic legacy album")
        delivery = self.delivery("Synthetic legacy.zip", release["tracks"], kind="zip")
        managed_name = delivery["import_name"]
        legacy = self.stage / delivery["source"].stem
        for relative, data in delivery["payload"].items():
            path = legacy / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        delivery["import_name"] = legacy.name
        untouched = self.storage_identity(legacy)
        self.assertIsNone(sort.staging_provenance(legacy.name))
        self.assertFalse(sort.is_committed_stage(legacy))
        calls = len(self.calls)

        self.run_main(stage_only=True)
        self.assert_sources_retained(delivery)
        provenance = sort.staging_provenance(legacy.name)
        self.assertIs(provenance["adopted_legacy"], True)
        self.assertEqual(provenance["import_name"], legacy.name)
        self.assertEqual(provenance["source_sha256"], _sha(delivery["raw"][delivery["name"]]))
        self.assertEqual(provenance["payload_hashes"], delivery["hashes"])
        self.assertEqual(self.storage_identity(legacy), untouched)
        self.assertEqual({path.name for path in self.stage.iterdir()}, {legacy.name})
        self.assertFalse((self.stage / managed_name).exists())
        self.assertEqual(mixie.load_completed_reviews(), old_entries)
        self.assertEqual(self.audit_snapshot(), old_audit)

        self.restart()
        self.run_main(stage_only=True)
        self.assert_sources_retained(delivery)
        self.assertEqual(sort.staging_provenance(legacy.name), provenance)
        self.assertEqual(self.storage_identity(legacy), untouched)
        self.assertEqual({path.name for path in self.stage.iterdir()}, {legacy.name})
        self.assertEqual(len(self.calls), calls)

        self.run_main()
        entry = self.assert_completed_entry(delivery)
        self.assertEqual(entry["source_manifest"]["provenance"], provenance)
        self.assert_purged(delivery, entry)
        entries = mixie.load_completed_reviews()
        self.assertEqual(len(entries), len(old_entries) + 1, self.output)
        for key, old_entry in old_entries.items():
            self.assertEqual(entries[key], old_entry)
        for path, data in old_audit.items():
            self.assertEqual(self.audit_snapshot()[path], data)
        self.assertEqual(sort.staging_provenance(historical["import_name"], verify_payload=False), old_provenance)

        ledger = (self.script / ".completed_reviews.json").read_bytes()
        library = sort.file_manifest(self.library)
        bulk = (self.backup / "OriginalDownloads-Bulk.zip").read_bytes()
        audit = self.audit_snapshot()
        calls = len(self.calls)
        self.restart()
        self.run_main()
        self.assertEqual((self.script / ".completed_reviews.json").read_bytes(), ledger)
        self.assertEqual(sort.file_manifest(self.library), library)
        self.assertEqual((self.backup / "OriginalDownloads-Bulk.zip").read_bytes(), bulk)
        self.assertEqual(self.audit_snapshot(), audit)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(sort.staging_provenance(legacy.name, verify_payload=False), provenance)

    def test_v10_independently_supported_partial_album_imports_with_full_totals_and_folder_label(self):
        release = self.register_release("Synthetic v10 partial album", sizes=(5,))
        delivery = self.delivery("V10 partial delivery", release["tracks"][:3])
        self.run_main()
        self.assertIn("Success: 1", self.output)
        self.assertIn("Errors: 0", self.output)
        self.assertEqual(mixie.load_review_queue(), [])
        records = mixie.load_completed_reviews()
        self.assertEqual(len(records), 1)
        record = next(iter(records.values()))
        self.assertEqual(record["evidence"]["provenance"], "independently_verified_partial")
        self.assertTrue(record["evidence"]["partial"])
        self.assertEqual(record["evidence"]["release_totals"]["album_total"], 5)
        folder = self.library / release["artist"] / f"{release['album']} (Incomplete, 3 of 5)"
        self.assertTrue(folder.is_dir(), self.output)
        audio_files = list(folder.rglob("*.flac")) + list(folder.rglob("*.mp3"))
        self.assertEqual(len(audio_files), 3)
        for path in audio_files:
            metadata = tag.read_track_metadata(path)
            self.assertEqual(metadata["album"], (release["album"],))
            self.assertEqual(tag.get_track_totals(metadata), {5})
        self.assert_sources_retained(delivery) if delivery["source"].exists() else self.assert_purged(delivery)

    def test_partial_legacy_stage_stays_blocked_without_replacing_payload_or_claiming_completion(self):
        release = self.register_release("Synthetic incomplete legacy stage")
        delivery = self.delivery("Partial synthetic legacy", release["tracks"])
        legacy = self.stage / delivery["name"]
        shutil.copytree(delivery["source"], legacy)
        missing = next(relative for relative in delivery["tracks"] if relative.endswith(".mp3"))
        (legacy / missing).unlink()
        staged_hashes = sort.file_manifest(legacy)
        untouched_stage = self.storage_identity(legacy)
        untouched_source = self.storage_identity(delivery["source"])
        self.assertNotEqual(staged_hashes, delivery["hashes"])

        for attempt in range(2):
            if attempt:
                self.restart()
            self.run_main()
            self.assertIn("reconciliation", self.output.lower())
            self.assertEqual(sort.file_manifest(legacy), staged_hashes)
            self.assertEqual(self.storage_identity(legacy), untouched_stage)
            self.assertEqual(sort.file_manifest(delivery["source"]), delivery["hashes"])
            self.assertEqual(self.storage_identity(delivery["source"]), untouched_source)
            self.assertEqual({path.name for path in self.stage.iterdir()}, {legacy.name})
            self.assertIsNone(sort.staging_provenance(legacy.name))
            self.assertFalse(sort.is_committed_stage(legacy))
            self.assertEqual(mixie.load_completed_reviews(), {})
            self.assertEqual(sort.file_manifest(self.library), {})
            self.assertEqual(self.audit_snapshot(), {})

    def test_identical_same_stem_directory_and_zip_share_outputs_but_archive_independent_sources(self):
        release = self.register_release("Synthetic identical album")
        directory = self.delivery("Synthetic twins", release["tracks"])
        name = directory["name"] + ".zip"
        source = self.inbox / name
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as saved:
            for relative, data in sorted(directory["payload"].items()):
                saved.writestr(relative, data)
        zipped = {**directory, "name": name, "source": source, "kind": "zip",
                  "raw": {name: source.read_bytes()}, "import_name": sort.staging_import_name(name, "zip")}
        self.run_main(stage_only=True)
        for delivery in (directory, zipped):
            self.assert_sources_retained(delivery)
        provenances = [sort.staging_provenance(delivery["import_name"]) for delivery in (directory, zipped)]
        self.assertEqual(provenances[0]["payload_hashes"], provenances[1]["payload_hashes"])
        self.assertNotEqual(provenances[0]["import_name"], provenances[1]["import_name"])
        self.assertNotEqual(provenances[0]["transaction_id"], provenances[1]["transaction_id"])

        self.run_main()
        self.assertEqual(len(mixie.load_completed_reviews()), 2, self.output)
        entries = [self.assert_completed_entry(delivery) for delivery in (directory, zipped)]
        self.assertEqual(entries[0]["receipt"], entries[1]["receipt"])
        self.assertTrue(set(entries[0]["input_map"]).isdisjoint(entries[1]["input_map"]))
        self.assertEqual(sort.file_manifest(self.library), {
            Path(path).relative_to(self.library).as_posix(): digest for path, digest in entries[0]["receipt"].items()})
        outputs = {row["destination"] for entry in entries for row in entry["input_map"].values()}
        self.assertEqual(len(outputs), len(release["tracks"]))
        for delivery, provenance, entry in zip((directory, zipped), provenances, entries):
            self.assertEqual(entry["source_manifest"]["provenance"], provenance)
            self.assert_raw_archive(delivery)
            self.assert_purged(delivery, entry)
        self.assertIn("Errors: 0", self.output)
        self.assertIn("Purged: 2", self.output)

        ledger = (self.script / ".completed_reviews.json").read_bytes()
        untouched_library = self.storage_identity(self.library)
        bulk = (self.backup / "OriginalDownloads-Bulk.zip").read_bytes()
        audit = self.audit_snapshot()
        calls = len(self.calls)
        self.restart()
        self.run_main()
        self.assertEqual((self.script / ".completed_reviews.json").read_bytes(), ledger)
        self.assertEqual(self.storage_identity(self.library), untouched_library)
        self.assertEqual((self.backup / "OriginalDownloads-Bulk.zip").read_bytes(), bulk)
        self.assertEqual(self.audit_snapshot(), audit)
        self.assertEqual(len(self.calls), calls)

    def test_linux_colon_album_and_tab_colon_filenames_complete_real_two_disc_mp3_flac_import(self):
        release = self.register_release("Synthetic: Two-disc signal", sizes=(1, 1))
        filenames = {spec["recording_id"]: filename for spec, filename in zip(release["tracks"], (
            "01.\tFirst: synthetic signal.flac", "01.\tSecond: synthetic signal.mp3"))}
        delivery = self.delivery("Synthetic: Linux delivery.zip", release["tracks"], kind="zip", filenames=filenames)
        self.assertTrue(all("\t" in Path(relative).name and ":" in Path(relative).name
                            for relative in delivery["tracks"]))
        self.run_main(stage_only=True)
        self.assert_sources_retained(delivery)
        by_title = {(spec["title"],): spec["recording_id"] for spec in release["tracks"]}

        def fingerprint_from_tags(path):
            path = Path(path)
            self.assertTrue(path.resolve().is_relative_to(self.stage))
            self.assertTrue(path.is_file())
            metadata = tag.read_track_metadata(path)
            self.assertIsNotNone(metadata)
            identifier = by_title.get(metadata["title"])
            if identifier is None:
                return self.forbidden("fingerprint of unknown synthetic tagged track: " + str(path))
            self.calls.append(("fingerprint", identifier))
            return {"duration": 0.15, "fingerprint": "offline-fixture:" + identifier}

        with patch.object(audio, "fingerprint_audio", side_effect=fingerprint_from_tags):
            self.run_main()
        entry = self.assert_completed_entry(delivery)
        self.assertEqual(entry["evidence"]["release_totals"], {
            "disc_total": 2, "album_total": 2, "track_totals": {"1": 1, "2": 1}})
        self.assertEqual({Path(row["destination"]).suffix for row in entry["input_map"].values()}, {".flac", ".mp3"})
        self.assertNotIn("Unsafe staged source path", self.output)
        self.assertNotIn("Invalid or unsafe source path", self.output)
        self.assert_purged(delivery, entry)
        self.assertIn("Success: 1", self.output)
        self.assertIn("Errors: 0", self.output)

    def test_one_unresolved_album_retains_the_entire_multi_album_source(self):
        delivery, complete, unresolved = self.mixed_unresolved_delivery()
        self.run_main()
        self.assert_sources_retained(delivery)
        self.assert_raw_archive(delivery)
        entries = list(mixie.load_completed_reviews().values())
        self.assertEqual(len(entries), 1, self.output)
        self.assertEqual(entries[0]["album"], complete["album"])
        self.assertEqual(entries[0]["source_manifest"]["payload_hashes"], delivery["hashes"])
        issues = mixie.load_review_queue()
        self.assertEqual([issue["album"] for issue in issues], [unresolved["album"]], self.output)
        self.assertEqual(issues[0]["details"]["kind"], "identity_release")
        self.assertEqual(len(issues[0]["details"]["tracks"]), len(unresolved["tracks"]))
        self.assertEqual(len([row for row in entries[0]["input_map"].values()
                              if Path(row["destination"]).is_file()]), len(complete["tracks"]))
        self.assertFalse(any(unresolved["album"] in path.parts for path in self.library.rglob("*")
                             if path.suffix.lower() in (".flac", ".mp3")))
        self.assertEqual(self.audit_snapshot(), {}, self.output)
        self.assertIn("Deferred: 1", self.output)
        self.assertIn("Purged: 0", self.output)

    def test_restart_revalidates_actual_completion_without_reidentifying_completed_album(self):
        delivery, complete, _ = self.mixed_unresolved_delivery()
        self.run_main()
        self.assertEqual(len(mixie.load_completed_reviews()), 1, self.output)
        ledger = (self.script / ".completed_reviews.json").read_bytes()
        library = sort.file_manifest(self.library)
        archive = (self.backup / "OriginalDownloads-Bulk.zip").read_bytes()
        start = len(self.calls)
        self.restart()
        self.run_main()
        identified = {call[1] for call in self.calls[start:] if call[0] == "fingerprint"}
        self.assertTrue(identified)
        self.assertTrue(identified.isdisjoint(track["recording_id"] for track in complete["tracks"]))
        self.assertEqual((self.script / ".completed_reviews.json").read_bytes(), ledger)
        self.assertEqual(sort.file_manifest(self.library), library)
        self.assertEqual((self.backup / "OriginalDownloads-Bulk.zip").read_bytes(), archive)
        self.assertIn("Already verified: 1", self.output)
        self.assert_sources_retained(delivery)

    def redelivery_case(self, kind):
        release = self.register_release("Identical redelivery " + kind)
        delivery = self.delivery("Repeated.zip" if kind == "zip" else "Repeated", release["tracks"], kind=kind)
        self.run_main()
        old_entry = deepcopy(self.assert_completed_entry(delivery))
        self.assert_purged(delivery, old_entry)
        old_ledger = deepcopy(mixie.load_completed_reviews())
        old_audit = self.audit_snapshot()
        self.assertTrue(old_audit)
        library = sort.file_manifest(self.library)
        bulk = (self.backup / "OriginalDownloads-Bulk.zip").read_bytes()
        if kind == "zip":
            delivery["source"].write_bytes(delivery["raw"][delivery["name"]])
        else:
            for relative, data in delivery["payload"].items():
                path = delivery["source"] / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        self.restart()
        self.run_main()
        new_entry = self.assert_completed_entry(delivery)
        self.assert_purged(delivery, new_entry)
        self.assertEqual(sort.file_manifest(self.library), library)
        self.assertEqual((self.backup / "OriginalDownloads-Bulk.zip").read_bytes(), bulk)
        self.assertEqual(len(mixie.load_completed_reviews()), len(old_ledger) + 1, self.output)
        for key, entry in old_ledger.items():
            self.assertEqual(mixie.load_completed_reviews()[key], entry)
        for path, data in old_audit.items():
            self.assertEqual(self.audit_snapshot()[path], data)
        self.assertNotEqual(old_entry["source_manifest"]["provenance"]["transaction_id"],
                            new_entry["source_manifest"]["provenance"]["transaction_id"])
        self.assertIn("Errors: 0", self.output)
        self.assertIn("Purged: 1", self.output)

    def test_identical_directory_redelivery_after_purge_preserves_audit_and_is_idempotent(self):
        self.redelivery_case("directory")

    def test_identical_zip_redelivery_after_purge_preserves_audit_and_is_idempotent(self):
        self.redelivery_case("zip")

    def test_main_archive_failure_retains_sources_then_restart_resumes_without_duplicate_outputs(self):
        release = self.register_release("Archive resume")
        delivery = self.delivery("Archive resume.zip", release["tracks"], kind="zip")
        obstruction = self.backup / "OriginalDownloads-Bulk.zip"
        obstruction.mkdir()
        self.run_main()
        self.assert_sources_retained(delivery)
        self.assertEqual(mixie.load_completed_reviews(), {})
        issue, _ = self.only_issue(delivery)
        self.assertEqual(issue["details"]["kind"], "completion", self.output)
        self.assertIn("Invalid bulk archive", issue["reason"])
        self.assertEqual(self.audit_snapshot(), {})
        library = sort.file_manifest(self.library)
        self.assertTrue(library)
        obstruction.rmdir()
        self.restart()
        self.run_main()
        entry = self.assert_completed_entry(delivery)
        self.assertEqual(sort.file_manifest(self.library), library)
        self.assert_purged(delivery, entry)

    def provider_outage_delivery(self, label, *, partial=False):
        release = self.register_release(label, sizes=(3,) if partial else (2,))
        delivery = self.delivery(label + ".zip", release["tracks"][:2], kind="zip")
        primary = release["tracks"][0]["recording_id"]
        unavailable = self.identifier("503 alternate/" + label)
        self.recordings[unavailable] = {**deepcopy(self.recordings[primary]), "id": unavailable, "length": "10000"}

        def candidates(fingerprint, api_key):
            matches = self.acoustid(fingerprint, api_key)
            if fingerprint["fingerprint"] == "offline-fixture:" + primary:
                matches[0]["recordings"].append({"id": unavailable})
            return matches

        def outage(provider, *args, **kwargs):
            if provider.__name__ == "get_recording_by_id" and args == (unavailable,):
                self.calls.append(("get_recording_by_id", unavailable))
                failure = HTTPError("https://synthetic.invalid/recording/" + unavailable,
                                    503, "Service Temporarily Unavailable", {}, None)
                self.addCleanup(failure.close)
                raise lookup.musicbrainzngs.NetworkError("Synthetic provider outage", failure)
            return self.musicbrainz(provider, *args, **kwargs)

        self.stack.enter_context(patch.object(audio, "identify_audio", side_effect=candidates))
        with patch.object(lookup, "_musicbrainz_call", side_effect=outage):
            self.run_main()
        issue, _ = self.only_issue(delivery)
        with self.subTest(phase="retryable provider outage classification"):
            self.assertEqual((issue["details"]["kind"], issue["details"].get("retryable")),
                             ("provider_unavailable", True), self.output)
        rows = [row for row in issue["details"]["tracks"] if row["evidence"]["lookup_state"] == "partial"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row["evidence"]["candidates"]), {primary, unavailable})
        self.assertEqual(set(row["evidence"]["recordings"]), {primary})
        self.assertFalse(row["evidence"]["complete"])
        self.assertIs(row["evidence"]["retryable"], True)
        self.assertEqual(row["assessment"]["status"], "probable")
        self.assertIs(row["assessment"]["retryable"], True)
        self.assertNotIn("multiple_eligible_recordings", row["assessment"]["reasons"])
        self.assertEqual(len(row["evidence"]["errors"]), 1)
        error = row["evidence"]["errors"][0]
        self.assertEqual((error["recording_id"], error["stage"], error["http_status"]),
                         (unavailable, "musicbrainz", 503))
        self.assertIs(error["retryable"], True)
        self.assert_sources_retained(delivery)
        self.assert_raw_archive(delivery)
        self.assertEqual(mixie.load_completed_reviews(), {})
        self.assertEqual(sort.file_manifest(self.library), {})
        self.assertEqual(self.audit_snapshot(), {})
        return delivery, release, unavailable

    def test_provider_503_partial_hydration_retries_after_restart_and_completes_fully_supported_album(self):
        delivery, release, unavailable = self.provider_outage_delivery("Synthetic provider recovery")
        provenance = sort.staging_provenance(delivery["import_name"])
        calls = len(self.calls)
        self.restart()
        self.run_main()
        self.assertIn(("get_recording_by_id", unavailable), self.calls[calls:])
        entry = self.assert_completed_entry(delivery)
        self.assertEqual(entry["source_manifest"]["provenance"], provenance)
        self.assertEqual(entry["evidence"]["release_totals"], {
            "disc_total": 1, "album_total": len(release["tracks"]), "track_totals": {"1": 2}})
        rows = entry["evidence"]["identity_release"]["tracks"]
        for row in rows:
            self.assertTrue(row["evidence"]["complete"])
            self.assertEqual(row["evidence"]["errors"], [])
            self.assertIs(row["assessment"]["retryable"], False)
        alternate = next(candidate for row in rows for candidate in row["assessment"]["candidates"]
                         if candidate["recording_id"] == unavailable)
        self.assertIn("duration_conflict", alternate["reasons"])
        self.assert_purged(delivery, entry)
        self.assertIn("Success: 1", self.output)
        self.assertIn("Errors: 0", self.output)

    def test_provider_recovery_does_not_auto_complete_genuinely_partial_release(self):
        delivery, _, unavailable = self.provider_outage_delivery("Synthetic provider partial release", partial=True)
        provenance = sort.staging_provenance(delivery["import_name"])
        untouched = self.storage_identity(self.stage / delivery["import_name"])
        bulk = (self.backup / "OriginalDownloads-Bulk.zip").read_bytes()
        calls = len(self.calls)
        self.restart()
        self.run_main()
        self.assertIn(("get_recording_by_id", unavailable), self.calls[calls:])
        issue, _ = self.only_issue(delivery)
        self.assertEqual(issue["details"]["kind"], "identity_release", self.output)
        self.assertIsNot(issue["details"].get("retryable"), True)
        self.assertTrue(any(evaluation["missing"] for evaluation in issue["details"]["releases"]))
        for row in issue["details"]["tracks"]:
            self.assertTrue(row["evidence"]["complete"])
            self.assertEqual(row["evidence"]["errors"], [])
            self.assertIs(row["assessment"]["retryable"], False)
        self.assert_sources_retained(delivery)
        self.assertEqual(sort.staging_provenance(delivery["import_name"]), provenance)
        self.assertEqual(self.storage_identity(self.stage / delivery["import_name"]), untouched)
        self.assertEqual((self.backup / "OriginalDownloads-Bulk.zip").read_bytes(), bulk)
        self.assertEqual(mixie.load_completed_reviews(), {})
        self.assertEqual(sort.file_manifest(self.library), {})
        self.assertEqual(self.audit_snapshot(), {})
        self.assertIn("Success: 0", self.output)
        self.assertIn("Purged: 0", self.output)

    def partial_delivery(self, label):
        release = self.register_release(label, sizes=(2, 3, 1))
        selected = [track for track in release["tracks"] if track["disc"] == 2 and track["number"] in (2, 3)]
        return self.delivery(label + ".zip", selected, kind="zip"), release

    def test_approved_online_partial_review_finalizes_full_release_totals_not_local_counts(self):
        delivery, _ = self.partial_delivery("Online partial")
        self.run_main()
        issue, issues, decision, prepared = self.approve(delivery, online=True)
        self.assertTrue(prepared["partial"])
        self.assertEqual(len(prepared["missing"]), 4)
        self.assertEqual(prepared["numbering_deferred"], {})
        calls = len(self.calls)
        self.assertTrue(self.finalize(issue, issues), self.output)
        self.assertFalse(any(call[0] in ("fingerprint", "acoustid") for call in self.calls[calls:]))
        entry = self.assert_completed_entry(delivery, decision=decision, partial=True)
        self.assertEqual(entry["evidence"]["release_totals"], {
            "disc_total": 3, "album_total": 6, "track_totals": {"1": 2, "2": 3, "3": 1}})
        self.assertEqual(issues, [])
        self.assert_purged(delivery, entry)

    def test_saved_online_partial_interpretation_survives_restart_until_explicit_apply(self):
        import mixie_review_ui
        delivery, _ = self.partial_delivery("Restarted partial")
        self.run_main()
        _, _, decision, _ = self.approve(delivery, online=True)
        calls = len(self.calls)
        self.restart()
        self.run_main()
        self.assertFalse(any(call[0] in ("fingerprint", "acoustid") for call in self.calls[calls:]))
        self.assertEqual({}, mixie.load_completed_reviews())
        self.assertEqual({}, sort.file_manifest(self.library))
        self.assert_sources_retained(delivery)
        self.assertIn("Saved review awaiting explicit Apply", self.output)
        issues = mixie.load_review_queue()
        self.assertEqual(decision, issues[0]["interpretation"])
        self.assertEqual("reviewed", issues[0]["review_status"])
        with patch("builtins.input", side_effect=["1", "APPLY"]), redirect_stdout(StringIO()) as output:
            mixie_review_ui._apply(mixie, issues)
        self.output = output.getvalue()
        self.assertFalse(any(call[0] in ("fingerprint", "acoustid") for call in self.calls[calls:]))
        self.assert_completed_entry(delivery, decision=decision, partial=True)
        # Metadata approval authorizes import, not destructive source cleanup.
        self.assert_sources_retained(delivery)
        self.assertEqual([], issues)
        self.assertIn("Applied and verified: 1/1", self.output)

    def test_import_first_complete_album_archives_cleans_and_skips_repeat(self):
        """Shared original and per-album Library ZIPs precede input removal."""
        import mixie_finalizer as final
        from test_finalizer_v10i import assert_shared_originals

        (self.script / '.mixie-import-first.json').write_text(json.dumps({
            'version': 1, 'mode': 'verified_library_first'}), encoding='utf-8')
        release = self.register_release('Import-first synthetic album')
        delivery = self.delivery('Import-first synthetic album.zip', release['tracks'], kind='zip')
        self.run_main()
        self.assertIn('Success: 1', self.output)
        self.assertIn('Verified and archived 1 album group(s)', self.output)
        self.assertFalse(delivery['source'].exists(), self.output)
        self.assertFalse((self.stage / delivery['import_name']).exists(), self.output)
        self.assertEqual({}, mixie.load_completed_reviews())
        self.assertEqual([], mixie.load_review_queue())

        entries = mixie._verified_imports()
        self.assertEqual(1, len(entries))
        entry = next(iter(entries.values()))
        self.assertEqual(delivery['hashes'], entry['source_manifest']['payload_hashes'])
        self.assertEqual('independently_verified', entry['evidence']['provenance'])
        expected_inputs = {f"{delivery['import_name']}/{path}" for path in delivery['tracks']}
        self.assertEqual(expected_inputs, set(entry['input_map']))
        for relative, spec in delivery['tracks'].items():
            row = entry['input_map'][f"{delivery['import_name']}/{relative}"]
            saved = tag.read_track_metadata(row['destination'])
            self.assertEqual((spec['title'],), saved['title'])
            self.assertEqual((spec['artist'],), saved['artist'])
            self.assertEqual({spec['track_total']}, tag.get_track_totals(saved))
            self.assertEqual(_sha(delivery['payload'][relative]), row['source_sha256'])
            self.assertEqual(entry['receipt'][row['destination']], row['destination_sha256'])
        for destination, digest in entry['receipt'].items():
            self.assertEqual(digest, _sha(Path(destination).read_bytes()))

        # Follow the durable row, not a guessed per-album Originals filename.
        finalized = final._state()['finalized']
        self.assertEqual(set(entries), set(finalized))
        row = finalized[next(iter(entries))]
        records = assert_shared_originals(self, row, {delivery['import_name']: {
            'kind': delivery['kind'], 'payload': delivery['payload'],
            'raw': delivery['raw'][delivery['name']], 'tracks': list(delivery['tracks'])}})
        originals = [Path(records[delivery['import_name']]['path'])]
        self.assertEqual(originals, list((self.backup / 'Originals').rglob('*.zip')))
        library_zips = list(self.backup.glob('Library/*/*.zip'))
        self.assertEqual(len(library_zips), 1, self.output)
        self.assertEqual(str(library_zips[0]), row['library']['path'])
        self.assertEqual(_sha(library_zips[0].read_bytes()), row['library']['file_sha256'])
        self.assertEqual(sort.zip_manifest(library_zips[0]), row['library']['entries'])
        shared = self.backup / 'Originals' / 'Shared'
        shared_before = self.storage_identity(shared)
        journal = shared / 'deliveries' / f'{records[delivery["import_name"]]["object_id"]}.json'
        journal_bytes = journal.read_bytes()
        with zipfile.ZipFile(library_zips[0]) as library_zip:
            self.assertIsNone(library_zip.testzip())
            manifest = json.loads(library_zip.read('mixie-archive-manifest.json'))
            self.assertEqual(manifest['type'], 'corrected_library')
            artist, album = library_zips[0].parent.name, library_zips[0].stem
            album_root = self.library / artist / album
            for destination, digest in entry['receipt'].items():
                path = Path(destination)
                self.assertEqual(library_zip.read(path.relative_to(album_root).as_posix()), path.read_bytes())
                self.assertEqual(_sha(path.read_bytes()), digest)

        # Restart must not recreate deleted deliveries or rescan online.
        before = sort.file_manifest(self.library)
        ledger = (self.script / '.verified_imports.json').read_bytes()
        archive_hashes = (_sha(originals[0].read_bytes()), _sha(library_zips[0].read_bytes()))
        calls = len(self.calls)
        self.restart()
        self.run_main()
        self.assertEqual([], self.calls[calls:])
        self.assertIn('No staged albums found.', self.output)
        self.assertEqual(before, sort.file_manifest(self.library))
        self.assertEqual(ledger, (self.script / '.verified_imports.json').read_bytes())
        self.assertEqual(archive_hashes, (_sha(originals[0].read_bytes()), _sha(library_zips[0].read_bytes())))
        self.assertEqual(shared_before, self.storage_identity(shared))
        self.assertEqual(journal_bytes, journal.read_bytes())
        self.assertFalse(delivery['source'].exists())
        self.assertFalse((self.stage / delivery['import_name']).exists())

    def offline_delivery(self, label):
        delivery, release = self.partial_delivery(label)
        self.unresolved.update(track["recording_id"] for track in release["tracks"])
        self.run_main()
        return delivery

    def test_offline_manual_interpretation_missing_totals_remains_deferred_without_provider_calls(self):
        delivery = self.offline_delivery("Deferred offline")
        issue, issues, decision, prepared = self.approve(delivery, online=False, manual=True)
        self.assertIsNone(prepared["release_totals"])
        self.assertEqual(prepared["numbering_totals"], {})
        self.assertEqual(prepared["numbering_deferred"], {
            str(self.stage / relative): ["track_total", "disc_total"] for relative in issue["tracks"]})
        calls = len(self.calls)
        with patch.dict(os.environ, {"ACOUSTID_CLIENT_KEY": ""}):
            with self.assertRaises(review.AlbumReviewRequired) as caught:
                self.finalize(issue, issues)
        self.assertIn("totals", caught.exception.details["reason"])
        self.assertEqual(self.calls[calls:], [])
        self.assertEqual(mixie.load_completed_reviews(), {})
        self.assertEqual(sort.file_manifest(self.library), {})
        self.assertEqual(self.audit_snapshot(), {})
        self.assertEqual(mixie.load_review_queue()[0]["interpretation"], decision)
        self.assert_sources_retained(delivery)

    def test_explicit_offline_totals_and_identity_finalize_without_key_or_provider_promotion(self):
        delivery = self.offline_delivery("Explicit offline")
        issue, issues, decision, prepared = self.approve(
            delivery, online=False, manual=True, totals={"track_total": 7, "disc_total": 3})
        self.assertEqual(prepared["numbering_deferred"], {})
        self.assertIsNone(prepared["release_totals"])
        calls = len(self.calls)
        with patch.dict(os.environ, {"ACOUSTID_CLIENT_KEY": ""}):
            self.assertTrue(self.finalize(issue, issues), self.output)
        self.assertEqual(self.calls[calls:], [])
        entry = self.assert_completed_entry(delivery, decision=decision, partial=True)
        self.assertIsNone(entry["evidence"]["release_totals"])
        self.assert_purged(delivery, entry)

    def test_review_archive_failure_keeps_approved_queue_and_real_commit_resumes(self):
        delivery = self.offline_delivery("Review archive resume")
        issue, issues, decision, _ = self.approve(
            delivery, online=False, manual=True, totals={"track_total": 7, "disc_total": 3})
        obstruction = self.backup / "OriginalDownloads-Bulk.zip"
        # The initial deferred batch already archived Inbox; retain its raw ZIP bytes.
        original_archive = obstruction.read_bytes()
        obstruction.unlink()
        obstruction.mkdir()
        with self.assertRaisesRegex(ValueError, "Invalid bulk archive"):
            self.finalize(issue, issues)
        self.assert_sources_retained(delivery)
        self.assertEqual(mixie.load_completed_reviews(), {})
        self.assertEqual(mixie.load_review_queue()[0]["interpretation"], decision)
        library = sort.file_manifest(self.library)
        self.assertTrue(library)
        self.assertEqual(self.audit_snapshot(), {})
        obstruction.rmdir()
        obstruction.write_bytes(original_archive)
        self.restart()
        issue, issues = self.only_issue(delivery)
        self.assertTrue(self.finalize(issue, issues), self.output)
        entry = self.assert_completed_entry(delivery, decision=decision, partial=True)
        self.assertEqual(sort.file_manifest(self.library), library)
        self.assert_purged(delivery, entry)

    def test_restart_finishes_real_quarantine_before_any_reidentification_or_new_staging(self):
        release = self.register_release("Quarantine restart")
        delivery = self.delivery("Quarantine restart", release["tracks"])
        obstruction = self.backup / ".cleanup-purge-receipts"
        obstruction.write_bytes(b"Synthetic filesystem obstruction; no purge receipt can be created.")
        self.run_main()
        entry = self.assert_completed_entry(delivery)
        ledger = (self.script / ".completed_reviews.json").read_bytes()
        library = sort.file_manifest(self.library)
        quarantines = list((self.backup / ".cleanup-quarantine").glob(".mixie-inbox-*"))
        self.assertEqual(len(quarantines), 1, self.output)
        held = quarantines[0]
        self.assertEqual(sort.file_manifest(held / "staging"), delivery["hashes"])
        self.assertEqual(sort.file_manifest(held / "original"), delivery["hashes"])
        self.assertFalse(delivery["source"].exists())
        self.assertFalse((self.stage / delivery["import_name"]).exists())
        self.assertFalse((held / "purge-state.json").exists())
        self.assertIn("Awaiting safe cleanup: 1", self.output)
        obstruction.unlink()
        calls = len(self.calls)
        self.restart()
        self.run_main()
        self.assertEqual(self.calls[calls:], [])
        self.assertEqual((self.script / ".completed_reviews.json").read_bytes(), ledger)
        self.assertEqual(sort.file_manifest(self.library), library)
        self.assertIn("Finishing previous verified cleanup", self.output)
        self.assertIn("Purged: 1", self.output)
        self.assert_purged(delivery, entry)


class LegacyLiveAcceptanceScripts(unittest.TestCase):
    @unittest.skip("mixie_real_audio_acceptance_test.py reads live music/state and uses real providers; equivalent offline coverage: complete multiformat import and quarantine restart above.")
    def test_mixie_real_audio_acceptance_test(self):
        pass

    @unittest.skip("mixie_real_audio_acceptance_test_fixed.py reads live music/state and uses real providers; equivalent offline coverage: raw ZIP preservation, redelivery, and archive resume above.")
    def test_mixie_real_audio_acceptance_test_fixed(self):
        pass

    @unittest.skip("mixie_real_metadata_repair_test.py reads live music/state and uses real providers; equivalent offline coverage: wrong-title correction and online/offline review finalization above.")
    def test_mixie_real_metadata_repair_test(self):
        pass
