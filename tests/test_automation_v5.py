"""MIXIE v5: user-approved import, Rockbox disc semantics, and compact UI."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib
import shutil

import mixie
import mixie_review_ui as ui
import mixie_terminal as terminal
import tag
from support import WorkspaceCase, make_audio, make_cover


class AutomationV5Tests(WorkspaceCase):
    def sample(self, *, multi=False):
        count = 3 if multi else 2
        rows = []
        for index in range(count):
            disc, number, total = (1, index + 1, 2) if multi and index < 2 else ((2, 1, 1) if multi else (1, index + 1, 2))
            file = self.stage / "Import" / f"{index:02}.flac"
            make_audio(file, album="Album", albumartist="Elio Mei", artist="Elio Mei",
                       title="Wrong Name" if index == 0 else f"Song {index}",
                       tracknumber=number, tracktotal=total, discnumber=disc,
                       disctotal=(2 if multi else 1), date="2020", genre="Indie")
            rows.append(tag.read_track_metadata(file))
        album = {"album": "Album", "folder": rows[0]["path"].parent, "tracks": rows}
        issue = {"album": "Album", "import_name": "Import", "tracks": [
            Path(t["path"]).relative_to(self.stage).as_posix() for t in rows],
            "reason": "Competing recordings", "details": {"kind": "identity_release"}}
        return album, issue

    def test_source_single_disc_tags_are_removed_only_from_working_copy(self):
        for extension in (".flac", ".mp3"):
            with self.subTest(extension=extension):
                original = make_audio(self.stage / "source" / f"song{extension}",
                                      album="Album", albumartist="Artist", artist="Artist",
                                      title="Title", tracknumber="1/1", discnumber="1/1",
                                      date="2020", genre="Rock")
                digest = hashlib.sha256(original.read_bytes()).hexdigest()
                working = self.root / ".mixie-work-testdisc"
                working.mkdir(exist_ok=True)
                target = working / f"copy{extension}"
                shutil.copy2(original, target)
                self.assertEqual(1, tag.remove_single_disc_tags_on_copy(target, working))
                saved = tag.read_track_metadata(target)
                self.assertEqual((), saved["disc_number"])
                self.assertEqual(set(), tag.get_disc_totals(saved))
                self.assertEqual({1}, tag.get_track_totals(saved))
                self.assertEqual(digest, hashlib.sha256(original.read_bytes()).hexdigest())
                self.assertTrue(tag.read_track_metadata(original)["disc_number"])

    def test_disc_layout_auto_multi_and_single_fields(self):
        album, issue = self.sample()
        mapping = ui._view(mixie, issue)
        self.assertEqual("single", ui._disc_layout(mixie, issue, mapping))
        self.assertEqual(4, len(ui.SINGLE_DISC_FIELDS))
        album2, issue2 = self.sample(multi=True)
        mapping2 = ui._view(mixie, issue2)
        self.assertEqual("multiple", ui._disc_layout(mixie, issue2, mapping2))
        self.assertEqual(4, len(ui.TRACK_FIELDS))

    def test_user_approval_is_explicit_not_provider_verification(self):
        album, issue = self.sample()
        relative = issue["tracks"][0]
        with patch.object(mixie, "save_review_queue"):
            ui.save_choice(mixie, issue, [issue], field="title", value="One Man Circus", relative=relative)
            ui.save_choice(mixie, issue, [issue], field="disc_layout", value="Single")
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertIsNone(decision["release_id"])
        self.assertTrue(decision["user_approved"])
        self.assertFalse(decision["independently_verified"])
        self.assertEqual("One Man Circus", decision["tracks"][relative]["title"])
        self.assertEqual(1, decision["totals"][relative]["disc_total"])
        with patch("builtins.input", return_value="APPROVE"), patch.object(mixie, "save_review_queue"), redirect_stdout(StringIO()):
            self.assertTrue(ui._approve_review(mixie, issue, [issue]))
        self.assertEqual("reviewed", issue["review_status"])
        self.assertEqual(decision, issue["interpretation"])

    def test_user_approved_album_can_reach_verified_import_without_provider(self):
        album, issue = self.sample()
        with patch.object(mixie, "select_album_art", return_value=make_cover()):
            decision = ui._build_user_interpretation(mixie, issue)
            original_hashes = {row["path"]: hashlib.sha256(row["path"].read_bytes()).hexdigest()
                               for row in album["tracks"]}
            with redirect_stdout(StringIO()):
                plan = mixie.preview_album(album, None, interpretation=decision)
                receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        self.assertIsNotNone(receipt)
        self.assertEqual("user_approved", plan["provenance"])
        for source in original_hashes:
            self.assertEqual(original_hashes[source], hashlib.sha256(source.read_bytes()).hexdigest())
            destination = receipt.input_map[source.relative_to(self.stage).as_posix()]["destination"]
            saved = tag.read_track_metadata(destination)
            self.assertEqual((), saved["disc_number"])
            self.assertEqual(set(), tag.get_disc_totals(saved))

    def test_user_approved_multi_disc_keeps_correct_numbers_and_totals(self):
        album, issue = self.sample(multi=True)
        decision = ui._build_user_interpretation(mixie, issue)
        self.assertEqual({1, 2}, {row["disc"] for row in decision["tracks"].values()})
        with patch.object(mixie, "select_album_art", return_value=make_cover()), redirect_stdout(StringIO()):
            plan = mixie.preview_album(album, None, interpretation=decision)
            receipt = mixie.test_album_plan(plan, album, commit=True, auto=True)
        for relative, track in decision["tracks"].items():
            destination = receipt.input_map[relative]["destination"]
            saved = tag.read_track_metadata(destination)
            self.assertEqual(track["disc"], tag.parse_disc_number(saved["disc_number"])[0])
            self.assertEqual({2}, tag.get_disc_totals(saved))

    def test_duplicate_manual_positions_cannot_be_approved(self):
        album, issue = self.sample()
        with patch.object(mixie, "save_review_queue"):
            ui.save_choice(mixie, issue, [issue], field="track_number", value="1", relative=issue["tracks"][1])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            ui._build_user_interpretation(mixie, issue)

    def test_compact_scan_labels_preliminary_matches_as_matched_not_ok(self):
        album, issue = self.sample()
        audits = [{"path": album["tracks"][0]["path"], "status": "preliminary_match", "details": {"title": "One Man Circus", "artist-credit-phrase": "Elio Mei"}},
                  {"path": album["tracks"][1]["path"], "status": "needs_review", "details": None}]
        with redirect_stdout(StringIO()) as out:
            terminal.track_summary(audits, album["tracks"], tag_service=tag)
        self.assertIn("MATCHED", out.getvalue())
        self.assertIn("NEEDS REVIEW", out.getvalue())
        self.assertNotIn(" OK", out.getvalue())

    def test_compact_scan_uses_existing_audit_signature_and_hides_diagnostics(self):
        album, _issue = self.sample()
        seen = []

        def legacy_audit(paths, api_key, cache):
            seen.append((list(paths), api_key, cache))
            print("LEGACY PER-TRACK DEBUG")
            return [{"path": path, "status": "needs_review", "reason": "Synthetic ambiguity", "details": {}}
                    for path in paths]

        with patch.object(mixie.audio, "audit_tracks", side_effect=legacy_audit), \
                patch.object(mixie.lookup, "discover_release_candidates", return_value=[]), \
                redirect_stdout(StringIO()) as out:
            with self.assertRaises(mixie.review.AlbumReviewRequired):
                mixie.preview_album(album, "synthetic-offline-key")
        self.assertEqual(1, len(seen))
        self.assertEqual({}, seen[0][2])
        self.assertIn("NEEDS REVIEW", out.getvalue())
        self.assertNotIn("LEGACY PER-TRACK DEBUG", out.getvalue())
