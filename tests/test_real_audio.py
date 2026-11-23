"""Synthetic silence only; all paths are provided by the isolated WorkspaceCase."""
from copy import deepcopy
from pathlib import Path
import shutil
import zipfile

from mutagen import File
from mutagen.id3 import ID3

import tag
from support import WorkspaceCase, make_audio, make_cover


class RealAudioTests(WorkspaceCase):
    def working_folder(self, name):
        path = self.root / (".mixie-work-" + name)
        path.mkdir()
        return path

    def protected_copy(self, suffix, name, **tags):
        source = make_audio(self.stage / name / ("01" + suffix), **tags)
        inbox = self.inbox / (name + suffix)
        shutil.copy2(source, inbox)
        archived = self.backup / (name + ".zip")
        with zipfile.ZipFile(archived, "w") as archive:
            archive.write(source, source.name)
        snapshots = {path: path.read_bytes() for path in (source, inbox, archived)}
        working = self.working_folder(name)
        target = working / source.name
        shutil.copy2(source, target)
        return source, target, working, snapshots

    def assert_unchanged(self, snapshots):
        for path, expected in snapshots.items():
            self.assertEqual(path.read_bytes(), expected, f"Protected source changed: {path}")

    def identity(self, **changes):
        result = {"recording_id": "synthetic-recording", "title": "Hate Myself",
                  "artist": "Halestorm & I Prevail", "verified": True}
        result.update(changes)
        return result

    def test_verified_identity_corrects_wrong_title_only_on_copy_without_inventing_tags(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                source, target, working, snapshots = self.protected_copy(
                    suffix, "identity" + suffix, title="Wrong title", artist="Halestorm & I Prevail")
                identity = self.identity(date="2024", genre="Never invent this")
                original_identity = deepcopy(identity)
                self.assertEqual(tag.apply_verified_identity_on_copy(target, working, identity), 1)
                saved = tag.read_track_metadata(target)
                self.assertEqual(saved["title"], ("Hate Myself",))
                self.assertEqual(saved["artist"], ("Halestorm & I Prevail",))
                self.assertEqual((saved["date"], saved["genre"]), ((), ()))
                self.assertEqual(identity, original_identity)
                self.assertEqual(tag.apply_verified_identity_on_copy(target, working, identity), 0)
                self.assert_unchanged(snapshots)
                self.assertEqual(tag.read_track_metadata(source)["title"], ("Wrong title",))

    def test_full_verified_release_artist_overrides_bad_source_only_in_working_copy(self):
        for suffix in ('.flac', '.mp3'):
            with self.subTest(suffix=suffix):
                source, target, working, snapshots = self.protected_copy(
                    suffix, 'source-credit-' + suffix,
                    title="VAMPIRE'S DIET", artist='3OH!3, The Used_Bert McCracken',
                    album='NEED', albumartist='3OH!3',
                    tracknumber='11/11', discnumber='1/1')
                identity = {'recording_id': 'verified-rec-11',
                            'title': "VAMPIRE'S DIET", 'artist': '3OH!3',
                            'verified': True, 'user_approved': False}
                with self.assertRaises(tag.MetadataConflictError):
                    tag.apply_verified_identity_on_copy(target, working, identity)
                with self.assertRaisesRegex(ValueError, 'independently verified'):
                    tag.apply_verified_identity_on_copy(target, working,
                        {**identity, 'verified': False, 'user_approved': True},
                        authoritative_release=True)
                self.assertEqual(1, tag.apply_verified_identity_on_copy(
                    target, working, identity, authoritative_release=True))
                self.assertEqual(('3OH!3',), tag.read_track_metadata(target)['artist'])
                self.assertEqual(('3OH!3, The Used_Bert McCracken',),
                                 tag.read_track_metadata(source)['artist'])
                tag.remove_single_disc_tags_on_copy(target, working)
                metadata = tag.read_track_metadata(target)
                self.assertEqual((), metadata['disc_number'])
                self.assertEqual((), metadata['disc_total'])
                self.assertEqual(('11', 11), tag.parse_track_number(metadata['track_number']))
                self.assert_unchanged(snapshots)

    def test_equivalent_credit_typography_is_preserved_not_rewritten(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                _, target, working, snapshots = self.protected_copy(
                    suffix, "credit" + suffix, title="Wrong title", artist="Halestorm & I Prevail")
                tag.apply_verified_identity_on_copy(target, working, self.identity(artist="HALESTORM & I PREVAIL"))
                self.assertEqual(tag.read_track_metadata(target)["artist"], ("Halestorm & I Prevail",))
                self.assert_unchanged(snapshots)

    def test_material_artist_credit_conflicts_fail_before_changing_title(self):
        for suffix in (".flac", ".mp3"):
            for original, remote in (("SAMURAI", "SAMURAI (Refused)"),
                                     ("SAMURAI (Refused)", "SAMURAI"),
                                     ("Halestorm & I Prevail", "Halestorm")):
                with self.subTest(suffix=suffix, credit=original):
                    name = "conflict" + suffix + str(len(list(self.stage.iterdir())))
                    _, target, working, snapshots = self.protected_copy(suffix, name, title="Wrong title", artist=original)
                    before = target.read_bytes()
                    with self.assertRaises(tag.MetadataConflictError) as caught:
                        tag.apply_verified_identity_on_copy(target, working, self.identity(artist=remote))
                    self.assertEqual(caught.exception.field, "artist")
                    self.assertEqual(caught.exception.existing, [original])
                    self.assertEqual(caught.exception.expected, remote)
                    self.assertEqual(target.read_bytes(), before)
                    self.assert_unchanged(snapshots)

    def test_explicit_user_approved_identity_can_replace_credit_without_claiming_verification(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                _, target, working, snapshots = self.protected_copy(
                    suffix, "approved" + suffix, title="Wrong title", artist="SAMURAI")
                identity = self.identity(artist="SAMURAI (Refused)", title="Never Fade Away",
                                         verified=False, user_approved=True)
                self.assertEqual(tag.apply_verified_identity_on_copy(target, working, identity), 2)
                self.assertIs(identity["verified"], False)
                self.assertIs(identity["user_approved"], True)
                saved = tag.read_track_metadata(target)
                self.assertEqual((saved["title"], saved["artist"]), (("Never Fade Away",), ("SAMURAI (Refused)",)))
                self.assert_unchanged(snapshots)

    def test_unresolved_abba_identity_is_rejected_without_changes(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                _, target, working, snapshots = self.protected_copy(
                    suffix, "abba" + suffix, title="Unknown song", artist="ABBA")
                before = target.read_bytes()
                with self.assertRaises(ValueError):
                    tag.apply_verified_identity_on_copy(target, working, self.identity(
                        artist="ABBA", title="Dancing Queen", verified=False))
                self.assertEqual(target.read_bytes(), before)
                self.assert_unchanged(snapshots)

    def test_invalid_identity_is_preflighted_before_any_field_changes(self):
        _, target, working, _ = self.protected_copy(".flac", "invalid", title="Wrong title", artist="Artist")
        before = target.read_bytes()
        for changes in ({"artist": "Bad\nCredit"}, {"title": ""}, {"recording_id": ""},
                        {"verified": "true"}, {"verified": False, "user_approved": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                tag.apply_verified_identity_on_copy(target, working, self.identity(**changes))
            self.assertEqual(target.read_bytes(), before)

    def test_identity_cannot_write_source_symlink_or_hardlink(self):
        source, target, working, snapshots = self.protected_copy(
            ".flac", "boundary", title="Wrong title", artist="Halestorm & I Prevail")
        symlink = working / "symlink.flac"
        symlink.symlink_to(source)
        hardlink = working / "hardlink.flac"
        hardlink.hardlink_to(source)
        for path, folder in ((source, source.parent), (source, working), (symlink, working), (hardlink, working)):
            with self.subTest(path=path, folder=folder), self.assertRaises(PermissionError):
                tag.apply_verified_identity_on_copy(path, folder, self.identity())
        self.assert_unchanged(snapshots)
        self.assertEqual(tag.read_track_metadata(target)["title"], ("Wrong title",))

    def test_single_disc_numbering_and_art_round_trip_preserve_sources(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                source, target, working, snapshots = self.protected_copy(
                    suffix, "roundtrip" + suffix, title="Never Fade Away", artist="SAMURAI")
                matched = {("1", "1"): source}
                proposals = tag.propose_release_numbering(matched, [{"disc": 1, "number": "1"}])
                tag.apply_corrections([{**p, "path": target} for p in proposals], approved=True)
                cover = make_cover()
                self.assertTrue(tag.embed_front_art(target, cover))
                self.assertFalse(tag.embed_front_art(target, cover))
                saved = tag.read_track_metadata(target)
                self.assertEqual(tag.parse_track_number(saved["track_number"])[0], "01")
                self.assertEqual(tag.get_track_totals(saved), {1})
                self.assertEqual(saved["disc_number"], ())
                self.assertEqual(tag.get_disc_totals(saved), set())
                audio = File(target)
                pictures = audio.pictures if suffix == ".flac" else audio.tags.getall("APIC")
                self.assertEqual([(p.type, p.mime, p.data) for p in pictures], [(3, cover["mime"], cover["data"])])
                self.assert_unchanged(snapshots)

    def test_multi_disc_numbering_round_trip_uses_each_disc_size(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                working = self.working_folder("multi" + suffix)
                matched, release_tracks, snapshots, copies = {}, [], {}, {}
                for disc, number in ((1, 1), (1, 2), (2, 1)):
                    source = make_audio(self.stage / ("multi" + suffix) / f"Disc {disc}" / (str(number) + suffix),
                                        tracknumber=number, discnumber=disc, artist="Artist", title="Title")
                    snapshots[source] = source.read_bytes()
                    target = working / (f"{disc}-{number}" + suffix)
                    shutil.copy2(source, target)
                    matched[(str(disc), str(number))] = source
                    release_tracks.append({"disc": disc, "number": str(number)})
                    copies[source] = target
                proposals = tag.propose_release_numbering(matched, release_tracks)
                tag.apply_corrections([{**p, "path": copies[p["path"]]} for p in proposals], approved=True)
                for (disc, _), source in matched.items():
                    saved = tag.read_track_metadata(copies[source])
                    self.assertEqual(tag.get_track_totals(saved), {2 if disc == "1" else 1})
                    self.assertEqual(tag.get_disc_totals(saved), {2})
                self.assert_unchanged(snapshots)

    def test_reviewed_numbering_replaces_conflicting_totals_on_copy_only(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                source, target, working, snapshots = self.protected_copy(
                    suffix, "totals" + suffix, tracknumber="1/99", discnumber="1/9", title="Title", artist="Artist")
                if suffix == ".flac":
                    audio = File(target)
                    audio["tracktotal"] = ["98"]
                    audio["totaltracks"] = ["97"]
                    audio["disctotal"] = ["8"]
                    audio["totaldiscs"] = ["7"]
                    audio.save()
                decisions = []
                current = tag.read_track_metadata(target)
                for field, existing in (("track_total", tag.get_track_totals(current)),
                                        ("disc_total", tag.get_disc_totals(current))):
                    expected = "2" if field == "disc_total" else "1"
                    decisions.append({"status": "pending_application", "path": str(target), "field": field,
                                      "existing_value": [str(v) for v in sorted(existing)],
                                      "reference_value": expected, "value": expected})
                release = [{"disc": disc, "number": "1"} for disc in (1, 2)]
                proposals = tag.propose_release_numbering({("1", "1"): target}, release,
                                                         reviewed_decision=decisions, allow_partial=True)
                tag.apply_corrections(proposals, approved=True)
                saved = tag.read_track_metadata(target)
                self.assertEqual(tag.get_track_totals(saved), {1})
                self.assertEqual(tag.get_disc_totals(saved), {2})
                self.assertEqual(tag.parse_track_number(saved["track_number"])[0], "01")
                self.assertEqual(tag.parse_disc_number(saved["disc_number"])[0], 1)
                self.assertTrue(all(p["verified"] is False for p in proposals))
                self.assert_unchanged(snapshots)
                self.assertEqual(tag.get_track_totals(tag.read_track_metadata(source)), {99})

    def test_reviewed_number_and_total_replacements_share_id3_frames_safely(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                source, target, working, snapshots = self.protected_copy(
                    suffix, "positions" + suffix, tracknumber="9/99", discnumber="9/9")
                decisions = [{"status": "pending_application", "path": str(source), "field": field,
                              "existing_value": existing,
                              "reference_value": "2" if field == "disc_total" else "1",
                              "value": "2" if field == "disc_total" else "1"}
                             for field, existing in (("track_number", ["9/99"]), ("track_total", ["99"]),
                                                     ("disc_number", ["9/9"]), ("disc_total", ["9"]))]
                release = [{"disc": disc, "number": "1"} for disc in (1, 2)]
                proposals = tag.propose_release_numbering({("1", "1"): source}, release,
                                                         reviewed_decision=decisions, allow_partial=True)
                tag.apply_corrections([{**p, "path": target} for p in proposals], approved=True)
                saved = tag.read_track_metadata(target)
                self.assertEqual(tag.parse_track_number(saved["track_number"])[0], "01")
                self.assertEqual(tag.parse_disc_number(saved["disc_number"])[0], 1)
                self.assertEqual(tag.get_track_totals(saved), {1})
                self.assertEqual(tag.get_disc_totals(saved), {2})
                self.assert_unchanged(snapshots)

    def test_partial_numbering_writes_full_release_totals_only_to_working_copy(self):
        full_release = [{"disc": disc, "number": str(number)}
                        for disc, size in enumerate((4, 3, 1), 1) for number in range(1, size + 1)]
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                source, target, working, snapshots = self.protected_copy(
                    suffix, "partial" + suffix, tracknumber="2", discnumber="2", artist="SAMURAI")
                proposals = tag.propose_release_numbering({("2", "2"): source}, full_release, allow_partial=True)
                self.assertEqual({p["field"]: p["value"] for p in proposals}, {"track_total": 3, "disc_total": 3})
                tag.apply_corrections([{**p, "path": target} for p in proposals], approved=True)
                saved = tag.read_track_metadata(target)
                self.assertEqual(tag.parse_track_number(saved["track_number"])[0], "02")
                self.assertEqual(tag.parse_disc_number(saved["disc_number"])[0], 2)
                self.assertEqual(tag.get_track_totals(saved), {3})
                self.assertEqual(tag.get_disc_totals(saved), {3})
                self.assertEqual(saved["artist"], ("SAMURAI",))
                self.assertEqual((saved["date"], saved["genre"]), ((), ()))
                self.assert_unchanged(snapshots)

    def test_partial_reviewed_totals_preserve_snapshot_checks_and_originals(self):
        full_release = [{"disc": disc, "number": str(number)}
                        for disc, size in enumerate((4, 3, 1), 1) for number in range(1, size + 1)]
        for suffix in (".flac", ".mp3"):
            for stale in (False, True):
                with self.subTest(suffix=suffix, stale=stale):
                    source, target, working, snapshots = self.protected_copy(
                        suffix, "partial-review" + suffix + str(stale), tracknumber="2/99", discnumber="2/1")
                    decisions = [{"status": "pending_application", "path": str(source), "field": field,
                                  "existing_value": existing, "reference_value": "3", "value": "3"}
                                 for field, existing in (("track_total", ["99"]), ("disc_total", ["1"]))]
                    proposals = tag.propose_release_numbering({("2", "2"): source}, full_release,
                                                             reviewed_decision=decisions, allow_partial=True)
                    redirected = [{**p, "path": target} for p in proposals]
                    if stale:
                        audio = File(target, easy=True)
                        audio["tracknumber"] = ["3/99"]
                        audio.save()
                        before = target.read_bytes()
                        with self.assertRaises(ValueError):
                            tag.apply_corrections(redirected, approved=True)
                        self.assertEqual(target.read_bytes(), before)
                    else:
                        tag.apply_corrections(redirected, approved=True)
                        saved = tag.read_track_metadata(target)
                        self.assertEqual(tag.get_track_totals(saved), {3})
                        self.assertEqual(tag.get_disc_totals(saved), {3})
                        self.assertEqual(tag.parse_track_number(saved["track_number"])[0], "02")
                        self.assertEqual(tag.parse_disc_number(saved["disc_number"])[0], 2)
                    self.assertTrue(all(p["user_approved"] is True and p["verified"] is False for p in proposals))
                    self.assert_unchanged(snapshots)

    def test_multiple_common_decisions_are_applied_only_to_their_working_copy_paths(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                matched, decisions, copies, snapshots = {}, [], {}, {}
                for number, artist in enumerate(("SAMURAI", "Halestorm & I Prevail"), 1):
                    album = f"Original album {number}"
                    source, target, working, protected = self.protected_copy(
                        suffix, f"common-{number}" + suffix, album=album, albumartist=artist)
                    snapshots.update(protected)
                    matched[("1", str(number))] = source
                    copies[str(source)] = (target, working)
                    decisions.extend([
                        {"status": "pending_application", "path": str(source), "field": "album",
                         "existing_value": album, "reference_value": "Reference album", "value": f"Approved album {number}"},
                        {"status": "pending_application", "path": str(source), "field": "album_artist",
                         "existing_value": artist, "reference_value": "Reference artist", "value": artist},
                    ])
                common = {"album": "Reference album", "album_artist": "Reference artist"}
                self.assertEqual(tag.propose_common_metadata(matched, common, reviewed_decision=decisions), [])
                for decision in decisions:
                    target, working = copies[decision["path"]]
                    tag.replace_reviewed_text_on_copy(target, working, decision["field"], decision["value"])
                for (disc, number), source in matched.items():
                    saved = tag.read_track_metadata(copies[str(source)][0])
                    self.assertEqual(saved["album"], (f"Approved album {number}",))
                    expected_artist = "SAMURAI" if number == "1" else "Halestorm & I Prevail"
                    self.assertEqual(saved["album_artist"], (expected_artist,))
                    self.assertEqual((saved["date"], saved["genre"]), ((), ()))
                self.assert_unchanged(snapshots)

    def test_numbering_decision_is_rechecked_at_write_time(self):
        _, target, working, _ = self.protected_copy(".flac", "stale", tracknumber="1/99", discnumber="1/1")
        decision = {"status": "pending_application", "path": str(target), "field": "track_total",
                    "existing_value": ["99"], "reference_value": "1", "value": "1"}
        proposals = tag.propose_release_numbering({("1", "1"): target}, [{"disc": 1, "number": "1"}],
                                                 reviewed_decision=decision)
        audio = File(target)
        audio["tracknumber"] = ["1/98"]
        audio.save()
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            tag.apply_corrections(proposals, approved=True)
        self.assertEqual(target.read_bytes(), before)

    def test_reviewed_total_is_invalidated_by_changed_related_numbering(self):
        for suffix in (".flac", ".mp3"):
            for key, changed in (("tracknumber", "2/99"), ("discnumber", "2/1")):
                with self.subTest(suffix=suffix, key=key):
                    _, target, working, snapshots = self.protected_copy(
                        suffix, "context" + key + suffix, tracknumber="1/99", discnumber="1/1")
                    decision = {"status": "pending_application", "path": str(target), "field": "track_total",
                                "existing_value": ["99"], "reference_value": "1", "value": "1"}
                    proposals = tag.propose_release_numbering({("1", "1"): target}, [{"disc": 1, "number": "1"}],
                                                             reviewed_decision=decision)
                    audio = File(target, easy=True)
                    audio[key] = [changed]
                    audio.save()
                    before = target.read_bytes()
                    with self.assertRaises(ValueError):
                        tag.apply_corrections(proposals, approved=True)
                    self.assertEqual(target.read_bytes(), before)
                    self.assert_unchanged(snapshots)

    def test_reviewed_number_and_missing_totals_are_applied_together(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                source, target, working, snapshots = self.protected_copy(
                    suffix, "mixed" + suffix, tracknumber="9")
                decision = {"status": "pending_application", "path": str(source), "field": "track_number",
                            "existing_value": ["9"], "reference_value": "1", "value": "1"}
                proposals = tag.propose_release_numbering({("1", "1"): source}, [{"disc": 1, "number": "1"}],
                                                         reviewed_decision=decision)
                tag.apply_corrections([{**p, "path": target} for p in proposals], approved=True)
                saved = tag.read_track_metadata(target)
                self.assertEqual(tag.parse_track_number(saved["track_number"])[0], "01")
                self.assertEqual(tag.get_track_totals(saved), {1})
                self.assertEqual(saved["disc_number"], ())
                self.assertEqual(tag.get_disc_totals(saved), set())
                self.assert_unchanged(snapshots)

    def test_verified_identity_can_fill_blank_artist_without_inventing_album_metadata(self):
        for suffix in (".flac", ".mp3"):
            with self.subTest(suffix=suffix):
                _, target, working, snapshots = self.protected_copy(
                    suffix, "blank" + suffix, title="Wrong title", artist="   ")
                tag.apply_verified_identity_on_copy(target, working, self.identity())
                saved = tag.read_track_metadata(target)
                self.assertEqual(saved["title"], ("Hate Myself",))
                self.assertEqual(saved["artist"], ("Halestorm & I Prevail",))
                self.assertEqual((saved["album"], saved["date"], saved["genre"]), ((), (), ()))
                self.assert_unchanged(snapshots)

    def test_tagless_mp3_can_be_filled_on_copy(self):
        _, target, working, snapshots = self.protected_copy(".mp3", "tagless")
        File(target).delete()
        self.assertIsNone(File(target).tags)
        tag.apply_corrections([{"path": target, "field": "title", "value": "Title"}], approved=True)
        self.assertEqual(tag.read_track_metadata(target)["title"], ("Title",))
        self.assert_unchanged(snapshots)

    def test_wav_and_aiff_keep_id3_writer_and_verification_behavior(self):
        for suffix in (".wav", ".aiff"):
            with self.subTest(suffix=suffix):
                working = self.working_folder("id3" + suffix)
                target = make_audio(working / ("01" + suffix))
                proposals = [{"path": target, "field": field, "value": value}
                             for field, value in (("title", "Title"), ("artist", "Artist"),
                                                  ("track_number", 1), ("track_total", 1),
                                                  ("disc_number", 1), ("disc_total", 1))]
                tag.apply_corrections(proposals, approved=True)
                audio = File(target)
                self.assertIsInstance(audio.tags, ID3)
                self.assertEqual(list(audio.tags["TRCK"].text), ["01/1"])
                self.assertEqual(list(audio.tags["TPOS"].text), ["1/1"])
                self.assertEqual(tag.read_track_metadata(target)["title"], ("Title",))
