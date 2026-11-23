"""Post-restart integration regressions; synthetic audio, isolated runner only.

Provider doubles below are explicitly synthetic. The acceptance fixture is composed,
not inherited/imported as a TestCase, so its tests are never rediscovered here.
"""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import archive
import audio
import lookup
import mixie
import review
import sort
import tag
import test_acceptance
from support import WorkspaceCase, make_audio, make_cover, metadata_record


class IngestSafetyTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        for module, names in ((audio, ("audit_tracks", "identify_audio", "urlopen")),
                              (lookup, ("_musicbrainz_call", "urlopen", "download_release_art"))):
            for name in names:
                self.stack.enter_context(patch.object(module, name, side_effect=AssertionError("Unexpected provider: " + name)))
        self.stack.enter_context(patch("builtins.input", side_effect=AssertionError("Unexpected prompt")))

    def test_retiring_conflicting_choices_requires_explicit_confirmation(self):
        issue = {"decisions": [{"field": "date", "value": "2018"}],
                 "interpretation": {"status": "pending_application"}}
        before = deepcopy(issue)
        with patch("builtins.input", return_value=""):
            self.assertFalse(mixie._retire_metadata_choices(issue))
        self.assertEqual(before, issue)

    def test_retired_choices_remain_in_history_without_losing_interpretation(self):
        issue = {"decision": {"field": "date", "value": "2018"},
                 "decisions": [{"field": "date", "value": "2018"}],
                 "manual_fields": {"genre": "Rock"},
                 "interpretation": {"status": "pending_application"}}
        previous = {key: deepcopy(issue[key]) for key in ("decision", "decisions", "manual_fields")}
        with patch("builtins.input", return_value="CLEAR"):
            self.assertTrue(mixie._retire_metadata_choices(issue))
        self.assertEqual([previous], issue["decision_history"])
        self.assertTrue(all(key not in issue for key in previous))
        self.assertEqual({"status": "pending_application"}, issue["interpretation"])

    def album(self, *, count=1, folder="Import/Album", artist="Artist", disc=1):
        tracks = []
        for number in range(1, count + 1):
            path = make_audio(self.stage / folder / f"{number:02}.flac", title=f"Song {number}",
                              artist=artist, album="Album", albumartist="Artist", date="2020", genre="Rock",
                              tracknumber=number, tracktotal=count, discnumber=disc, disctotal=max(1, disc))
            tracks.append(tag.read_track_metadata(path))
        return {"album": "Album", "tracks": tracks, "folder": tracks[0]["path"].parent}

    def legacy_plan(self, album):
        return {"matched": {(str(tag.parse_disc_number(track["disc_number"])[0]), str(index)): track["path"]
                            for index, track in enumerate(album["tracks"], 1)},
                "proposals": [], "artwork": make_cover()}

    def decision(self, album, field, value, *, path=None, reference="2024", existing="2020"):
        result = {"status": "pending_application", "field": field, "value": value,
                  "existing_value": existing, "reference_value": reference,
                  "source_fingerprint": mixie.review_fingerprint(mixie._album_issue(album))}
        if path is not None:
            result["path"] = str(path)
        return result

    def interpretation(self, album, **changes):
        result = {"status": "pending_application", "user_approved": True, "independently_verified": False,
                  "source_fingerprint": mixie.review_fingerprint(mixie._album_issue(album)),
                  "release_id": None, "album": "Album", "album_artist": "Artist", "partial": True,
                  "tracks": {}, "totals": {}, "date": "2024"}
        for index, track in enumerate(album["tracks"], 1):
            relative = track["path"].relative_to(self.stage).as_posix()
            disc = tag.parse_disc_number(track["disc_number"])[0]
            result["tracks"][relative] = {"recording_id": None, "title": f"Song {index}", "artist": "Artist",
                                           "disc": disc, "number": index}
            result["totals"][relative] = {"track_total": len(album["tracks"]), "disc_total": max(1, disc)}
        result.update(changes)
        return result

    @contextmanager
    def synthetic_preview(self, album, *, recording_title=None, release_title=None, artist="Artist", editions=None, date="2020"):
        audits, tracks, matched = [], [], {}
        for index, track in enumerate(album["tracks"], 1):
            identifier = f"synthetic-recording-{index}"
            details = {"id": identifier, "title": recording_title or f"Song {index}", "artist-credit-phrase": artist}
            audits.append({"path": track["path"], "status": "preliminary_match", "reason": "Synthetic regression evidence",
                           "details": details})
            tracks.append({"disc": "1", "number": str(index), "recording_id": identifier,
                           "title": release_title or details["title"]})
            matched[(1, index)] = track["path"]
        editions = editions or {"synthetic-release": tracks}
        def evaluate(audits, identifier, **unused):
            return {"status": "complete", "matched": matched, "missing": [], "review": [], "reasons": [],
                    "release_id": identifier, "tracklist": deepcopy(editions[identifier])}
        with patch.object(audio, "audit_tracks", return_value=audits), \
             patch.object(lookup, "discover_release_candidates", return_value=list(editions)), \
             patch.object(lookup, "evaluate_release_candidate", side_effect=evaluate), \
             patch.object(lookup, "common_release_metadata", return_value={"album": "Album", "album_artist": "Artist", "date": date}), \
             patch.object(mixie, "select_album_art", return_value=make_cover()), redirect_stdout(StringIO()):
            yield

    def test_commit_backend_preflights_blocked_cleanup_before_backup_or_copy(self):
        album = self.album()
        pending = self.backup / ".cleanup-quarantine" / ".mixie-inbox-blocked"
        pending.mkdir(parents=True)
        with patch.object(archive, "backup_staged_import", return_value=self.backup) as backup, redirect_stdout(StringIO()):
            with self.assertRaisesRegex((ValueError, RuntimeError), "cleanup|recovery"):
                mixie.test_album_plan(self.legacy_plan(album), album, commit=True, auto=True)
        backup.assert_not_called()
        self.assertEqual({}, sort.file_manifest(self.library))

    def test_cli_commit_cannot_publish_during_interrupted_cleanup(self):
        album = self.album()
        pending = self.backup / ".cleanup-quarantine" / ".mixie-inbox-blocked"
        pending.mkdir(parents=True)
        with patch.object(sys, "argv", ["mixie.py", "--commit", "Album"]), \
             patch.object(tag, "group_staged_albums", return_value={("Import", "album"): album}), \
             patch.object(mixie, "preview_album", return_value=self.legacy_plan(album)), \
             patch.object(archive, "backup_staged_import", return_value=self.backup) as backup, \
             patch("builtins.input", return_value="IMPORT Album"), redirect_stdout(StringIO()) as output:
            mixie.main()
        backup.assert_not_called()
        self.assertEqual({}, sort.file_manifest(self.library))
        self.assertIn("cleanup", output.getvalue().lower())

    def test_title_only_approval_never_authorizes_material_artist_replacement(self):
        album = self.album(artist="Halestorm & I Prevail")
        source = album["tracks"][0]["path"]
        before = sort.file_checksum(source)
        choice = self.decision(album, "title", "Approved title", path=source,
                               existing="Song 1", reference="Song 1")
        with self.synthetic_preview(album, artist="Halestorm"):
            with self.assertRaises(tag.MetadataConflictError) as caught:
                plan = mixie.preview_album(album, "synthetic", reviewed_decision=choice)
                mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertEqual("artist", caught.exception.field)
        self.assertEqual(before, sort.file_checksum(source))
        self.assertEqual({}, sort.file_manifest(self.library))

    def test_artist_specific_approval_can_authorize_only_its_material_credit_change(self):
        album = self.album(artist="Halestorm & I Prevail")
        source = album["tracks"][0]["path"]
        choice = self.decision(album, "artist", "Halestorm", path=source,
                               existing="Halestorm & I Prevail", reference="Halestorm")
        with self.synthetic_preview(album, artist="Halestorm"):
            plan = mixie.preview_album(album, "synthetic", reviewed_decision=choice)
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        saved = tag.read_track_metadata(receipt.input_map[source.relative_to(self.stage).as_posix()]["destination"])
        self.assertEqual(("Halestorm",), saved["artist"])
        self.assertEqual(("Halestorm & I Prevail",), tag.read_track_metadata(source)["artist"])

    def test_full_interpretation_explicitly_authorizes_material_artist_change(self):
        album = self.album(artist="Halestorm & I Prevail")
        source = album["tracks"][0]["path"]
        with patch.object(mixie, "select_album_art", return_value=make_cover()), redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, None, interpretation=self.interpretation(album))
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        row = receipt.input_map[source.relative_to(self.stage).as_posix()]
        self.assertEqual(("Artist",), tag.read_track_metadata(row["destination"])["artist"])
        self.assertFalse(row["identity"]["verified"])
        self.assertTrue(row["identity"]["user_approved"])

    def test_exact_source_choice_beats_album_choice_in_both_orders_and_final_tags(self):
        album = self.album(count=2)
        first, second = [track["path"] for track in album["tracks"]]
        fallback = self.decision(album, "date", "2018")
        scoped = self.decision(album, "date", "2021", path=first)
        for choices in ([fallback, scoped], [scoped, fallback]):
            with self.subTest(order=[bool(item.get("path")) for item in choices]), self.synthetic_preview(album, date="2024"):
                plan = mixie.preview_album(album, "synthetic", reviewed_decision=choices)
                receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
                for path, expected in ((first, "2021"), (second, "2018")):
                    saved = tag.read_track_metadata(receipt.input_map[path.relative_to(self.stage).as_posix()]["destination"])
                    self.assertEqual((expected,), saved["date"])
                for path in (first, second):
                    self.assertEqual(("2020",), tag.read_track_metadata(path)["date"])

    def test_interpretation_rechecks_all_saved_choices_instead_of_silently_mixing(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        cases = {"stale existing": {"existing": "1999", "reference": "2024"},
                 "stale reference": {"existing": "2020", "reference": "2019"},
                 "outside source scope": {"path": self.stage / "Elsewhere" / "01.flac", "reference": "2024"},
                 "older contradictory approval": {"existing": "2020", "reference": "2020"}}
        for label, changes in cases.items():
            with self.subTest(label=label), patch.object(mixie, "select_album_art", return_value=make_cover()), redirect_stdout(StringIO()):
                choice = self.decision(album, "date", "2018", path=source, **{key: value for key, value in changes.items() if key != "path"})
                if "path" in changes:
                    choice["path"] = str(changes["path"])
                retained = deepcopy(choice)
                with self.assertRaises((ValueError, review.AlbumReviewRequired)):
                    mixie.preview_album(album, None, reviewed_decision=choice, interpretation=self.interpretation(album))
                self.assertEqual(retained, choice)
                self.assertEqual({}, sort.file_manifest(self.library))

    def test_interpretation_does_not_silently_overwrite_saved_album_or_manual_choice(self):
        album = self.album()
        choice = self.decision(album, "album", "Older approved album", existing="Album", reference="Album")
        for kwargs in ({"reviewed_decision": choice}, {"manual_fields": {"date": "2018"}}):
            with self.subTest(kwargs=kwargs), patch.object(mixie, "select_album_art", return_value=make_cover()), redirect_stdout(StringIO()):
                with self.assertRaises((ValueError, review.AlbumReviewRequired)):
                    mixie.preview_album(album, None, interpretation=self.interpretation(album), **kwargs)

    def test_release_specific_live_title_is_preserved_without_source_title_authority(self):
        album = self.album()
        source = album["tracks"][0]["path"]
        with self.synthetic_preview(album, recording_title="Song 1", release_title="Song 1 (Live)"):
            plan = mixie.preview_album(album, "synthetic")
        self.assertEqual("Song 1 (Live)", plan["identities"][str(source)]["title"])
        self.assertTrue(plan["identities"][str(source)]["verified"])

    def test_compatible_release_version_disagreements_require_structured_review(self):
        album = self.album()
        editions = {"synthetic-live": [{"disc": "1", "number": "1", "recording_id": "synthetic-recording-1", "title": "Song 1 (Live)"}],
                    "synthetic-acoustic": [{"disc": "1", "number": "1", "recording_id": "synthetic-recording-1", "title": "Song 1 (Acoustic)"}]}
        with self.synthetic_preview(album, editions=editions):
            with self.assertRaises(review.AlbumReviewRequired) as caught:
                mixie.preview_album(album, "synthetic")
        self.assertEqual(2, len(caught.exception.details["releases"]))
        self.assertIn("version", caught.exception.details["reason"].lower())

    def test_arbitrary_release_title_does_not_erase_known_correct_madilyn_identity(self):
        album = self.album(artist="Madilyn Mei")
        source = album["tracks"][0]["path"]
        with self.synthetic_preview(album, recording_title="One Man Circus", release_title="Song 1", artist="Madilyn Mei"):
            plan = mixie.preview_album(album, "synthetic")
        self.assertEqual("One Man Circus", plan["identities"][str(source)]["title"])

    def test_disc_asset_root_is_shared_by_selection_and_extra_preservation(self):
        album = self.album(folder="Import/Album/Disc 2", disc=2)
        root = self.stage / "Import" / "Album"
        for name in ("cover.png", "front.png"):
            (root / name).write_bytes(make_cover()["data"])
        (root / "booklet.pdf").write_bytes(b"Synthetic original booklet")
        source = album["tracks"][0]["path"]
        with redirect_stdout(StringIO()):
            artwork = mixie.select_album_art(album, [])
            plan = self.legacy_plan(album)
            plan["artwork"] = artwork
            plan["release_totals"] = {"disc_total": 2, "album_total": 2, "track_totals": {"1": 1, "2": 1}}
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertEqual(root / "cover.png", Path(artwork["source"]))
        self.assertTrue(any(Path(path).name == "booklet.pdf" for path in receipt.asset_outputs))
        self.assertTrue(any(Path(path).parts[-2:] == ("Original Artwork", "cover.png") for path in receipt.asset_outputs))
        self.assertEqual(("Song 1",), tag.read_track_metadata(source)["title"])

    def test_asset_root_never_ascends_arbitrary_or_uncorroborated_disc_directories(self):
        for folder, disc in (("bonus", 1), ("FLAC", 1), ("Disc 2", 1)):
            with self.subTest(folder=folder):
                album = self.album(folder="Import/" + folder, disc=disc)
                (self.stage / "Import" / "cover.png").write_bytes(make_cover()["data"])
                (self.stage / "Import" / "front.png").write_bytes(make_cover()["data"])
                with self.assertRaises((ValueError, AssertionError)), redirect_stdout(StringIO()):
                    mixie.select_album_art(album, [])

    def test_disc_root_requires_no_unrelated_album_audio_before_shared_extras(self):
        album = self.album(folder="Import/Disc 2", disc=2)
        make_audio(self.stage / "Import" / "Disc 1" / "other.flac", title="Other", artist="Other", album="Other album", discnumber=1)
        for name in ("cover.png", "front.png"):
            (self.stage / "Import" / name).write_bytes(make_cover()["data"])
        with self.assertRaises((ValueError, AssertionError)), redirect_stdout(StringIO()):
            mixie.select_album_art(album, [])

    def test_confirmed_mapping_can_corroborate_repaired_disc_directory_for_assets(self):
        album = self.album(folder="Import/Album/Disc 2", disc=1)
        root = self.stage / "Import" / "Album"
        for name in ("cover.png", "front.png"):
            (root / name).write_bytes(make_cover()["data"])
        source = album["tracks"][0]["path"]
        with redirect_stdout(StringIO()):
            selected = mixie.select_album_art(album, [], matched={("2", "1"): source})
        self.assertEqual(root / "cover.png", Path(selected["source"]))

    def test_interpretation_route_checkpoints_real_draft_for_all_unresolved_kinds(self):
        source = self.track_path("Import/Album/01.flac")
        issue_base = {"import_name": "Import", "album": "Album", "tracks": ["Import/Album/01.flac"], "reason": "Synthetic unresolved issue"}
        conflicts = [{"kind": "metadata_conflict", "field": "artist", "existing_values": ["A", "B"], "reference_value": "C"},
                     {"kind": "metadata_conflict", "field": "track_total", "existing_values": ["2"], "reference_value": "3"},
                     {"kind": "required_metadata", "fields": ["date"]}]
        for details in conflicts:
            for change_menu in (False, True):
                with self.subTest(details=details, change_menu=change_menu):
                    issue = {**deepcopy(issue_base), "details": details}
                    fingerprint = mixie.review_fingerprint(issue)
                    issue.update(source_fingerprint=fingerprint, decisions=[{"field": "genre", "value": "Rock", "status": "pending_application"}])
                    commands = iter(["c", "0"] if change_menu else ["i", "0"])
                    def answer(prompt):
                        if "Select album" in prompt:
                            return "1"
                        if "Choice (" in prompt:
                            return "4"
                        if "Choose review action" in prompt:
                            return "0"
                        if "Choose album action" in prompt:
                            return next(commands)
                        raise AssertionError("Unexpected prompt: " + prompt)
                    with patch.object(tag, "read_track_metadata", return_value=metadata_record(source)), \
                         patch.object(mixie, "choose_metadata_resolution", return_value=False), \
                         patch.object(mixie, "choose_required_metadata_resolution", return_value=False), \
                         patch("builtins.input", side_effect=answer), redirect_stdout(StringIO()):
                        mixie.review_queue_menu([issue])
                    self.assertEqual("draft", issue.get("interpretation", {}).get("status"))
                    saved = mixie.load_review_queue()[0]
                    self.assertEqual(issue["interpretation"], saved["interpretation"])
                    self.assertEqual(issue["decisions"], saved["decisions"])
                    self.assertEqual(details, saved["details"])
                    self.assertEqual(fingerprint, saved["source_fingerprint"])


class ManagedAuthorityTests(unittest.TestCase):
    @contextmanager
    def acceptance(self):
        fixture = test_acceptance.AcceptanceTests("runTest")
        fixture.setUp()
        try:
            yield fixture
        finally:
            try:
                fixture.assert_no_surprise_providers()
            finally:
                self.assertTrue(fixture.doCleanups(), "Acceptance fixture cleanup failed")

    def test_managed_commit_rejects_missing_explicit_authority_before_any_backup(self):
        with self.acceptance() as fixture:
            release = fixture.register_release("Managed authority")
            fixture.delivery("Managed authority", release["tracks"])
            fixture.run_main(stage_only=True)
            album = next(iter(tag.group_staged_albums().values()))
            with redirect_stdout(StringIO()):
                valid = mixie.preview_album(album, "DISPOSABLE-OFFLINE-TEST-KEY")
            for missing in ("identities", "source_fingerprint", "source_manifest", "audits", "evaluations", "release_tracks", "compatible_ids"):
                with self.subTest(missing=missing):
                    plan = deepcopy(valid)
                    plan.pop(missing)
                    with patch.object(archive, "backup_staged_import", side_effect=AssertionError("Managed authority must be checked before backup")), redirect_stdout(StringIO()):
                        with self.assertRaises((ValueError, review.AlbumReviewRequired)):
                            mixie.test_album_plan(plan, album, commit=True, auto=True)
                    self.assertEqual({}, sort.file_manifest(fixture.library))

    def test_managed_commit_rejects_legacy_shaped_plan_even_with_readable_tags(self):
        with self.acceptance() as fixture:
            release = fixture.register_release("Legacy shaped managed input")
            fixture.delivery("Legacy shape", release["tracks"])
            fixture.run_main(stage_only=True)
            album = next(iter(tag.group_staged_albums().values()))
            matched = {("1", str(index)): track["path"] for index, track in enumerate(album["tracks"], 1)}
            plan = {"matched": matched, "proposals": [], "artwork": make_cover()}
            with patch.object(archive, "backup_staged_import", side_effect=AssertionError("Cannot mint new authority from legacy plan")), redirect_stdout(StringIO()):
                with self.assertRaises((ValueError, review.AlbumReviewRequired)):
                    mixie.test_album_plan(plan, album, commit=True, auto=True)
            self.assertEqual({}, sort.file_manifest(fixture.library))
