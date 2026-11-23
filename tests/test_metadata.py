"""Offline metadata regressions; run only through tests/run_isolated.py."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import sort
import tag
from support import WorkspaceCase, metadata_record


class CommonMetadataTests(WorkspaceCase):
    def proposals(self, record, common, **options):
        with patch.object(tag, "read_track_metadata", return_value=record):
            return tag.propose_common_metadata({("1", "1"): record["path"]}, common, **options)

    def test_equivalent_case_spacing_and_typography_preserve_original_credit(self):
        for original, expected in (
            ("SAMURAI", "samurai"),
            ("Halestorm & I Prevail", "HALESTORM & I PREVAIL"),
            ("Guns N’ Roses", "guns n' roses"),
            ("Artist – Guest", "artist - guest"),
        ):
            with self.subTest(credit=original):
                record = metadata_record(self.track_path(), album_artist=(original,))
                self.assertEqual(self.proposals(record, {"album_artist": expected}), [])
                self.assertEqual(record["album_artist"], (original,))

    def test_meaningful_parentheticals_and_collaborations_require_review(self):
        for original, expected in (
            ("SAMURAI", "SAMURAI (Refused)"),
            ("SAMURAI (Refused)", "SAMURAI"),
            ("Halestorm & I Prevail", "Halestorm"),
            ("Halestorm & I Prevail", "I Prevail"),
        ):
            with self.subTest(credit=original, reference=expected):
                record = metadata_record(self.track_path(), album_artist=(original,))
                with self.assertRaises(tag.MetadataConflictError) as caught:
                    self.proposals(record, {"album_artist": expected})
                self.assertEqual(caught.exception.field, "album_artist")
                self.assertEqual(caught.exception.existing, [original])
                self.assertEqual(caught.exception.expected, expected)

    def test_single_suffix_equivalence_requires_explicit_release_type(self):
        record = metadata_record(self.track_path(), album=("Hate Myself - Single",))
        for proof in (None, "Album", "EP"):
            with self.subTest(proof=proof):
                options = {} if proof is None else {"release_type": proof}
                with self.assertRaises(tag.MetadataConflictError):
                    self.proposals(record, {"album": "Hate Myself"}, **options)
        self.assertEqual(self.proposals(record, {"album": "Hate Myself"}, release_type="Single"), [])
        reverse = metadata_record(record["path"], album=("Hate Myself",))
        self.assertEqual(self.proposals(reverse, {"album": "Hate Myself - Single"}, release_type="single"), [])

    def test_single_proof_does_not_erase_other_release_qualifiers(self):
        for original in ("Hate Myself (Live)", "Hate Myself (Remastered) - Single"):
            with self.subTest(album=original):
                record = metadata_record(self.track_path(), album=(original,))
                with self.assertRaises(tag.MetadataConflictError):
                    self.proposals(record, {"album": "Hate Myself"}, release_type="Single")

    def test_volume_and_year_equivalence_remain_backwards_compatible(self):
        record = metadata_record(self.track_path(), album=("Collection, Vol. IV",), date=("2020",))
        self.assertEqual(self.proposals(record, {"album": "Collection Volume IV", "date": "2020-02-03"}), [])


class CommonMetadataDecisionTests(WorkspaceCase):
    def records(self, albums=("Original A", "Original B")):
        return {path: metadata_record(path, album=(album,), album_artist=("Original artist",), date=("2019",))
                for index, album in enumerate(albums, 1)
                for path in (self.track_path(f"Import/Album/{index:02}.flac"),)}

    def decision(self, field, existing, reference, path=None, value=None):
        result = {"status": "pending_application", "field": field, "existing_value": existing,
                  "reference_value": reference, "value": existing if value is None else value}
        if path is not None:
            result["path"] = str(path)
        return result

    def propose(self, records, common, decisions):
        matched = {("1", str(index)): path for index, path in enumerate(records, 1)}
        with patch.object(tag, "read_track_metadata", side_effect=records.__getitem__):
            return tag.propose_common_metadata(matched, common, reviewed_decision=decisions)

    def test_list_and_tuple_preserve_several_fields_and_paths_without_mutating_decisions(self):
        records = self.records()
        common = {"album": "Reference album", "album_artist": "Reference artist", "date": "2020"}
        decisions = [self.decision(field, record[field][0], reference, path)
                     for path, record in records.items() for field, reference in common.items()]
        before = deepcopy(decisions)
        for collection in (decisions, tuple(decisions)):
            with self.subTest(collection=type(collection).__name__):
                self.assertEqual(self.propose(records, common, collection), [])
        self.assertEqual(decisions, before)
        self.assertEqual([r["album"] for r in records.values()], [("Original A",), ("Original B",)])

    def test_path_specific_dict_does_not_resolve_a_sibling_track_conflict(self):
        records = self.records(("Original", "Original"))
        first, second = records
        decision = self.decision("album", "Original", "Reference", first)
        with self.assertRaises(tag.MetadataConflictError) as caught:
            self.propose(records, {"album": "Reference"}, decision)
        self.assertEqual(caught.exception.path, str(second))
        self.assertEqual(caught.exception.existing, ["Original"])

    def test_path_specific_decision_can_override_a_legacy_field_wide_decision(self):
        records = self.records()
        first, second = records
        decisions = [self.decision("album", "Original A", "Reference"),
                     self.decision("album", "Original B", "Reference", second)]
        self.assertEqual(self.propose(records, {"album": "Reference"}, decisions), [])

    def test_legacy_dict_and_empty_collections_keep_missing_field_proposals(self):
        records = self.records(("Original", "Original"))
        second = list(records)[1]
        records[second]["album"] = ()
        decision = self.decision("album", "Original", "Reference")
        proposals = self.propose(records, {"album": "Reference"}, decision)
        self.assertEqual([(p["path"], p["value"]) for p in proposals], [(second, "Reference")])
        for empty in ([], ()):
            with self.subTest(collection=empty):
                self.assertEqual(self.propose({second: records[second]}, {"album": "Reference"}, empty), proposals)

    def test_stale_and_unapproved_decisions_are_rejected_even_if_current_tag_matches_reference(self):
        records = self.records(("Reference",))
        path = next(iter(records))
        valid = self.decision("album", "Reference", "Reference", path)
        for change in ({"existing_value": "Old value"}, {"reference_value": "Old reference"},
                       {"status": "unreviewed"}, {"value": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.propose(records, {"album": "Reference"}, [{**valid, **change}])

    def test_duplicate_scopes_invalid_members_and_unknown_paths_require_fresh_review(self):
        records = self.records(("Original",))
        path = next(iter(records))
        scoped = self.decision("album", "Original", "Reference", path)
        global_decision = self.decision("album", "Original", "Reference")
        invalid = ([scoped, dict(scoped)], [global_decision, dict(global_decision)], [None], "not decisions",
                   [{**scoped, "path": str(self.stage / "Other/01.flac")}], [{**scoped, "path": 42}])
        for decisions in invalid:
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                self.propose(records, {"album": "Reference"}, decisions)


class ReleaseNumberingTests(WorkspaceCase):
    def release(self, sizes):
        matched, release_tracks, records = {}, [], {}
        for disc, size in enumerate(sizes, 1):
            for number in range(1, size + 1):
                path = self.track_path(f"Import/Album/Disc {disc}/{number:02}.flac")
                position = (str(disc), str(number))
                matched[position] = path
                release_tracks.append({"disc": disc, "number": str(number)})
                records[path] = metadata_record(path, track_number=(str(number),), disc_number=(str(disc),))
        return matched, release_tracks, records

    def propose(self, matched, release_tracks, records, **options):
        with patch.object(tag, "read_track_metadata", side_effect=lambda path: records[Path(path)]):
            return tag.propose_release_numbering(matched, release_tracks, **options)

    def decision(self, path, field, existing, expected):
        return {"status": "pending_application", "path": str(path), "field": field,
                "existing_value": list(existing), "reference_value": str(expected), "value": str(expected)}

    def test_single_disc_missing_totals_use_full_release_coverage(self):
        matched, release_tracks, records = self.release([3])
        proposals = self.propose(matched, release_tracks, records)
        self.assertEqual(len(proposals), 3)
        for proposal in proposals:
            self.assertEqual(proposal["field"], "track_total")
            self.assertEqual(proposal["value"], 3)
            self.assertIs(proposal["verified"], False)

    def test_multi_disc_missing_totals_are_per_disc_and_disc_count(self):
        matched, release_tracks, records = self.release([2, 1])
        proposals = self.propose(matched, release_tracks, records)
        for proposal in proposals:
            disc = next(int(d) for (d, _), path in matched.items() if path == proposal["path"])
            expected = (2 if disc == 1 else 1) if proposal["field"] == "track_total" else 2
            self.assertEqual(proposal["value"], expected)
        self.assertEqual(len(proposals), 6)

    def test_existing_album_wide_multi_disc_totals_are_preserved(self):
        matched, release_tracks, records = self.release([2, 1])
        for record in records.values():
            record["track_total"] = ("3",)
            record["disc_total"] = ("2",)
        self.assertEqual(self.propose(matched, release_tracks, records), [])

    def test_numbering_conflicts_are_structured_with_actual_values(self):
        cases = (
            ("track_number", ("9",), ["9"], "1"),
            ("track_total", ("99",), ["99"], "2"),
            ("disc_number", ("9",), ["9"], "1"),
            ("disc_total", ("99",), ["99"], "2"),
            ("track_number", ("unknown",), ["unknown"], "1"),
            ("track_total", ("unknown",), ["unknown"], "2"),
        )
        for field, values, existing, expected in cases:
            with self.subTest(field=field, values=values):
                sizes = [2, 1] if field.startswith("disc_") else [2]
                matched, release_tracks, records = self.release(sizes)
                path = matched[("1", "1")]
                records[path][field] = values
                with self.assertRaises(tag.MetadataConflictError) as caught:
                    self.propose(matched, release_tracks, records)
                conflict = caught.exception
                self.assertEqual((conflict.field, conflict.path), (field, str(path)))
                self.assertEqual(conflict.existing, existing)
                self.assertEqual(conflict.expected, expected)

    def test_conflicting_embedded_and_alias_totals_are_not_discarded(self):
        matched, release_tracks, records = self.release([2])
        path = matched[("1", "1")]
        records[path].update(track_number=("1/2",), track_total=("99", "2"))
        with self.assertRaises(tag.MetadataConflictError) as caught:
            self.propose(matched, release_tracks, records)
        self.assertEqual(caught.exception.field, "track_total")
        self.assertEqual(caught.exception.existing, ["2", "99"])
        self.assertEqual(caught.exception.expected, "2")

    def test_all_embedded_totals_remain_visible(self):
        record = metadata_record(self.track_path(), track_number=("1/2", "1/99"), disc_number=("1/1", "1/9"))
        self.assertEqual(tag.get_track_totals(record), {2, 99})
        self.assertEqual(tag.get_disc_totals(record), {1, 9})

    def test_nondecimal_digit_tags_are_reviewed_without_parser_crashes(self):
        for parser in (tag.parse_track_number, tag.parse_disc_number):
            with self.subTest(parser=parser.__name__):
                self.assertEqual(parser(("²",)), (None, None))
        record = metadata_record(self.track_path(), track_total=("²",), disc_total=("²",))
        for getter in (tag.get_track_totals, tag.get_disc_totals):
            with self.subTest(getter=getter.__name__):
                self.assertEqual(getter(record), set())
        for field in ("track_number", "disc_number"):
            with self.subTest(field=field):
                matched, tracks, records = self.release([1, 1] if field == "disc_number" else [1])
                path = matched[("1", "1")]
                records[path][field] = ("²",)
                with self.assertRaises(tag.MetadataConflictError) as caught:
                    self.propose(matched, tracks, records)
                self.assertEqual(caught.exception.existing, ["²"])
                self.assertEqual(caught.exception.expected, "1")

    def test_partial_extra_duplicate_positions_and_reused_files_rejected(self):
        matched, release_tracks, records = self.release([2])
        invalid = (
            ({("1", "1"): matched[("1", "1")]}, release_tracks),
            ({**matched, ("1", "3"): self.track_path("Import/Album/03.flac")}, release_tracks),
            (matched, release_tracks + [release_tracks[0]]),
            ({position: matched[("1", "1")] for position in matched}, release_tracks),
        )
        for mapping, tracks in invalid:
            with self.subTest(mapping=mapping, tracks=tracks):
                with self.assertRaises(ValueError):
                    self.propose(mapping, tracks, records)

    def test_partial_single_disc_mapping_uses_full_release_numbers_and_totals(self):
        matched, tracks, records = self.release([5])
        path = matched[("1", "4")]
        records[path].update(track_number=(), disc_number=())
        proposals = self.propose({("1", "4"): path}, tracks, records, allow_partial=True)
        self.assertEqual({p["field"]: p["value"] for p in proposals}, {
            "track_number": 4, "track_total": 5,
        })
        self.assertTrue(all(p["path"] == path and p["verified"] is False for p in proposals))

    def test_partial_multi_disc_mapping_counts_absent_discs_and_full_disc_sizes(self):
        matched, tracks, records = self.release([3, 2, 4])
        subset = {position: matched[position] for position in (("2", "2"), ("3", "1"))}
        proposals = self.propose(subset, tracks, records, allow_partial=True)
        self.assertEqual({(p["path"], p["field"]): p["value"] for p in proposals}, {
            (matched[("2", "2")], "track_total"): 2, (matched[("2", "2")], "disc_total"): 3,
            (matched[("3", "1")], "track_total"): 4, (matched[("3", "1")], "disc_total"): 3,
        })
        self.assertEqual(records[matched[("2", "2")]]["track_number"], ("2",))

    def test_partial_mode_requires_literal_true_and_is_never_inferred_from_review(self):
        matched, tracks, records = self.release([2])
        path = matched[("1", "1")]
        records[path]["track_total"] = ("99",)
        decision = self.decision(path, "track_total", ["99"], 2)
        with self.assertRaises(ValueError):
            self.propose({("1", "1"): path}, tracks, records, reviewed_decision=decision)
        for flag in (False, 1, "true", None, [], {}):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                self.propose({("1", "1"): path}, tracks, records, allow_partial=flag, reviewed_decision=decision)

    def test_partial_mode_rejects_outside_empty_duplicate_and_invalid_full_release_positions(self):
        matched, tracks, records = self.release([2])
        path = matched[("1", "1")]
        invalid = (
            ({}, tracks),
            ({("1", "3"): path}, tracks),
            ({("2", "1"): path}, tracks),
            ({position: path for position in matched}, tracks),
            ({("1", "1"): path}, tracks + [tracks[0]]),
            ({("1", "1"): path}, tracks + [{"disc": "01", "number": "01"}]),
            ({("1", "1"): path}, tracks + [{"disc": 3, "number": "1"}]),
            ({("1", "1"): path}, tracks + [{"disc": 2, "number": "A1"}]),
        )
        for subset, full_tracks in invalid:
            with self.subTest(subset=subset, tracks=full_tracks):
                with patch.object(tag, "read_track_metadata") as reader:
                    with self.assertRaises(ValueError):
                        tag.propose_release_numbering(subset, full_tracks, allow_partial=True)
                    reader.assert_not_called()

    def test_partial_conflicts_reference_full_release_counts_and_preserve_review_snapshots(self):
        matched, tracks, records = self.release([4, 3, 1])
        path = matched[("2", "2")]
        subset = {("2", "2"): path}
        records[path].update(track_total=("1",), disc_total=("1",))
        with self.assertRaises(tag.MetadataConflictError) as caught:
            self.propose(subset, tracks, records, allow_partial=True)
        self.assertEqual((caught.exception.field, caught.exception.existing, caught.exception.expected),
                         ("track_total", ["1"], "3"))
        decisions = [self.decision(path, field, ["1"], 3) for field in ("track_total", "disc_total")]
        proposals = self.propose(subset, tracks, records, allow_partial=True, reviewed_decision=decisions)
        self.assertEqual({p["field"]: p["value"] for p in proposals}, {"track_total": 3, "disc_total": 3})
        self.assertTrue(all(p["user_approved"] is True and p["verified"] is False for p in proposals))
        self.assertEqual(proposals[0]["numbering_snapshot"], {
            "track_number": ["2"], "track_total": ["1"], "disc_number": ["2"], "disc_total": ["1"],
        })
        decisions[0]["reference_value"] = "1"
        with self.assertRaises(ValueError):
            self.propose(subset, tracks, records, allow_partial=True, reviewed_decision=decisions)

    def test_partial_mode_preserves_valid_album_wide_totals_and_full_match_compatibility(self):
        matched, tracks, records = self.release([3, 2])
        self.assertEqual(self.propose(matched, tracks, records, allow_partial=True),
                         self.propose(matched, tracks, records))
        path = matched[("2", "2")]
        records[path].update(track_total=("5",), disc_total=("2",))
        self.assertEqual(self.propose({("2", "2"): path}, tracks, records, allow_partial=True), [])

    def test_reviewed_numbering_is_exact_scoped_and_not_called_verified(self):
        matched, release_tracks, records = self.release([2])
        path = matched[("1", "1")]
        records[path]["track_total"] = ("99",)
        decision = self.decision(path, "track_total", ["99"], 2)
        proposals = self.propose(matched, release_tracks, records, reviewed_decision=decision)
        replacement = next(p for p in proposals if p["path"] == path and p["field"] == "track_total")
        self.assertEqual(replacement["previous"], ["99"])
        self.assertEqual(replacement["numbering_snapshot"], {
            "track_number": ["1"], "track_total": ["99"],
            "disc_number": ["1"], "disc_total": [],
        })
        self.assertEqual(replacement["value"], 2)
        self.assertIs(replacement["user_approved"], True)
        self.assertIs(replacement["verified"], False)
        self.assertEqual(records[path]["track_total"], ("99",))

    def test_reviewed_numbering_can_cover_multiple_explicit_conflicts(self):
        matched, release_tracks, records = self.release([1, 1])
        path = matched[("1", "1")]
        records[path].update(track_total=("99",), disc_total=("9",))
        decisions = [self.decision(path, "track_total", ["99"], 1),
                     self.decision(path, "disc_total", ["9"], 2)]
        proposals = self.propose(matched, release_tracks, records, reviewed_decision=decisions)
        self.assertEqual({p["field"] for p in proposals if p.get("user_approved")}, {"track_total", "disc_total"})

    def test_stale_wrong_path_unapproved_or_non_release_numbering_decisions_rejected(self):
        matched, release_tracks, records = self.release([2])
        path = matched[("1", "1")]
        records[path]["track_total"] = ("99",)
        valid = self.decision(path, "track_total", ["99"], 2)
        for change in ({"existing_value": ["98"]}, {"reference_value": "3"}, {"value": "3"},
                       {"path": str(matched[("1", "2")])}, {"status": "unreviewed"}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.propose(matched, release_tracks, records, reviewed_decision={**valid, **change})


class RequiredMetadataTests(WorkspaceCase):
    def test_absent_date_requires_review_without_inventing_optional_genre(self):
        path = self.track_path()
        record = metadata_record(path, date=(), genre=())
        with patch.object(tag, "read_track_metadata", return_value=record):
            with self.assertRaises(tag.RequiredMetadataReview) as caught:
                tag.propose_required_album_metadata({("1", "1"): path}, {})
        self.assertEqual(caught.exception.fields, ["date"])
        self.assertEqual((record["date"], record["genre"]), ((), ()))

    def test_consistent_existing_metadata_can_fill_only_missing_tags(self):
        paths = [self.track_path(f"Import/Album/{n}.flac") for n in (1, 2)]
        records = {paths[0]: metadata_record(paths[0]), paths[1]: metadata_record(paths[1], date=(), genre=())}
        with patch.object(tag, "read_track_metadata", side_effect=records.__getitem__):
            proposals = tag.propose_required_album_metadata(dict(zip((("1", "1"), ("1", "2")), paths)), {})
        self.assertEqual({p["field"]: p["value"] for p in proposals}, {"date": "2020", "genre": "Rock"})
        self.assertTrue(all(p["verified"] is False for p in proposals))

    def test_explicit_date_genre_values_are_validated_not_guessed(self):
        self.assertEqual(tag.validate_manual_metadata({"date": "2024-02-29", "genre": " Alternative "}),
                         {"date": "2024-02-29", "genre": "Alternative"})
        for manual in ({"date": "2023-02-29"}, {"genre": ""}, {"date": "tomorrow"}, {"title": "Wrong scope"}):
            with self.subTest(manual=manual), self.assertRaises(ValueError):
                tag.validate_manual_metadata(manual)


class IdentityProposalTests(WorkspaceCase):
    def test_legacy_identity_proposals_remain_unverified_by_default(self):
        record = metadata_record(self.track_path(), title=("Wrong title",))
        proposals = tag.propose_identity_corrections(record, {"title": "Right title"}, False, True)
        self.assertEqual(proposals[0]["previous"], "Wrong title")
        self.assertIs(proposals[0]["verified"], False)

    def test_verification_and_user_approval_are_distinct_provenance(self):
        record = metadata_record(self.track_path(), title=("Wrong title",))
        for options, verified, approved in (({"verified": True}, True, False),
                                            ({"user_approved": True}, False, True)):
            with self.subTest(options=options):
                proposals = tag.propose_identity_corrections(record, {"title": "Right title"}, False, True, **options)
                self.assertIs(proposals[0]["verified"], verified)
                self.assertEqual(proposals[0].get("user_approved", False), approved)

    def test_unresolved_abba_identity_cannot_authorize_replacement(self):
        working = self.root / ".mixie-work-identity"
        working.mkdir()
        path = working / "01.flac"
        path.write_bytes(b"synthetic placeholder")
        identity = {"recording_id": "unresolved-abba", "title": "Dancing Queen", "artist": "ABBA",
                    "verified": False, "user_approved": False}
        before = deepcopy(identity)
        with patch.object(tag, "File") as opener:
            with self.assertRaises(ValueError):
                tag.apply_verified_identity_on_copy(path, working, identity)
            opener.assert_not_called()
        self.assertEqual(identity, before)


class WorkingCopyBoundaryTests(WorkspaceCase):
    def test_all_public_writers_reject_source_and_archive_files_before_opening(self):
        paths = [self.track_path(), self.inbox / "source.mp3", self.library / "original.flac",
                 self.backup / "archived.mp3"]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b"source fixture")
            for writer in (tag.write_track_metadata, tag.write_vorbis_tags, tag.write_id3_tags, tag.write_mp4_tags):
                with self.subTest(path=path, writer=writer.__name__), patch.object(tag, "File") as opener:
                    with self.assertRaises(PermissionError):
                        writer(path, [{"field": "title", "value": "Never write here"}])
                    opener.assert_not_called()
            with self.subTest(path=path, writer="embed_front_art"), patch.object(tag, "File") as opener:
                with self.assertRaises(PermissionError):
                    tag.embed_front_art(path, {"mime": "image/png", "data": b"placeholder"})
                opener.assert_not_called()

    def test_apply_corrections_preflights_all_paths_before_any_write(self):
        working = self.root / ".mixie-work-boundary"
        working.mkdir()
        copy = working / "01.flac"
        copy.write_bytes(b"copy fixture")
        source = self.track_path()
        proposals = [{"path": path, "field": "title", "value": "Title"} for path in (copy, source)]
        with patch.object(tag, "write_track_metadata") as writer:
            with self.assertRaises(PermissionError):
                tag.apply_corrections(proposals, approved=True)
            writer.assert_not_called()

    def test_unapproved_corrections_are_noop(self):
        with patch.object(tag, "File") as opener:
            self.assertEqual(tag.apply_corrections([{"path": self.track_path()}]), 0)
            opener.assert_not_called()

    def test_none_audio_and_failed_tag_creation_are_explicit_errors(self):
        working = self.root / ".mixie-work-tags"
        working.mkdir()
        for suffix, writer, audio_type in ((".flac", tag.write_vorbis_tags, tag.FLAC),
                                           (".mp3", tag.write_id3_tags, tag.MP3),
                                           (".m4a", tag.write_mp4_tags, tag.EasyMP4)):
            path = working / ("01" + suffix)
            path.write_bytes(b"fixture")
            with self.subTest(suffix=suffix, case="unrecognized"), patch.object(tag, "File", return_value=None):
                with self.assertRaises(ValueError):
                    writer(path, [{"field": "title", "value": "Title"}])
            audio = Mock(spec=audio_type)
            audio.tags = None
            with self.subTest(suffix=suffix, case="no tag handle"), patch.object(tag, "File", return_value=audio):
                with self.assertRaises(ValueError):
                    writer(path, [{"field": "title", "value": "Title"}])
                audio.save.assert_not_called()

    def test_artwork_reopen_without_audio_or_tags_has_explicit_verification_error(self):
        working = self.root / ".mixie-work-art"
        working.mkdir()
        path = working / "01.mp3"
        path.write_bytes(b"fixture")
        missing_tags = Mock(spec=tag.MP3)
        missing_tags.tags = None
        for saved in (None, missing_tags):
            with self.subTest(saved=saved):
                audio = Mock(spec=tag.MP3)
                audio.tags = tag.ID3()
                with patch.object(tag, "File", side_effect=[audio, saved]):
                    with self.assertRaises(RuntimeError):
                        tag.replace_reviewed_front_art_on_copy(path, working, {"mime": "image/png", "data": b"front"})

    def test_vorbis_can_create_a_missing_tag_handle(self):
        working = self.root / ".mixie-work-vorbis"
        working.mkdir()
        path = working / "01.flac"
        path.write_bytes(b"fixture")
        audio = Mock(spec=tag.FLAC)
        audio.tags = None
        audio.add_tags.side_effect = lambda: setattr(audio, "tags", {})
        with patch.object(tag, "File", return_value=audio):
            self.assertEqual(tag.write_vorbis_tags(path, [{"field": "title", "value": "Title"}]), 1)
        self.assertEqual(audio.tags["title"], ["Title"])
        audio.save.assert_called_once_with()


class ManagedStageScanTests(WorkspaceCase):
    def committed_fixture(self):
        source = self.inbox / "Delivery/Album/01.flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"synthetic staged fixture")
        with redirect_stdout(StringIO()):
            staged = sort.sort_inbox()
        self.assertEqual(len(staged), 1)
        stage = Path(staged[0])
        self.assertTrue(sort.is_committed_stage(stage))
        return source, stage, stage / "Album/01.flac"

    def test_unproven_managed_stage_is_pruned_for_staging_ancestor_and_nested_scans(self):
        name = sort.staging_import_name("Uncommitted", "directory")
        track = self.track_path(name + "/Album/01.flac")
        stage = self.stage / name
        for root in (self.script, self.stage, stage, track.parent):
            with self.subTest(root=root), redirect_stdout(StringIO()) as output:
                with patch.object(sort, "is_committed_stage", wraps=sort.is_committed_stage) as committed:
                    with patch.object(tag, "read_track_metadata") as reader:
                        self.assertEqual(tag.scan_albums(root), {})
                        reader.assert_not_called()
                    committed.assert_called_once_with(stage)
                self.assertRegex(output.getvalue().lower(), "review|recover")
        self.assertEqual(track.read_bytes(), b"disposable fixture")

    def test_committed_stage_is_scanned_but_altered_payload_is_not_opened(self):
        source, stage, track = self.committed_fixture()
        source_bytes = source.read_bytes()
        with patch.object(sort, "is_committed_stage", wraps=sort.is_committed_stage) as committed:
            with patch.object(tag, "read_track_metadata", side_effect=metadata_record) as reader:
                self.assertEqual(set(tag.scan_albums(self.stage)), {track.parent})
                reader.assert_called_once_with(track)
            committed.assert_called_once_with(stage)
        track.write_bytes(b"altered synthetic fixture")
        for root in (self.stage, track.parent):
            with self.subTest(root=root), redirect_stdout(StringIO()):
                with patch.object(sort, "is_committed_stage", wraps=sort.is_committed_stage) as committed:
                    with patch.object(tag, "read_track_metadata") as reader:
                        self.assertEqual(tag.scan_albums(root), {})
                        reader.assert_not_called()
                    committed.assert_called_once_with(stage)
        self.assertEqual(track.read_bytes(), b"altered synthetic fixture")
        self.assertEqual(source.read_bytes(), source_bytes)

    def test_prepared_managed_stage_is_not_scanned_or_deleted(self):
        source, stage, track = self.committed_fixture()
        record = sort.staging_provenance(stage.name)
        sort._write_staging_provenance(stage.name, {**record, "state": "prepared"})
        before = {path: path.read_bytes() for path in (source, track, sort._provenance_path(stage.name))}
        with redirect_stdout(StringIO()), patch.object(tag, "read_track_metadata") as reader:
            self.assertEqual(tag.scan_albums(self.stage), {})
            reader.assert_not_called()
        for path, expected in before.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_legacy_tracks_remain_visible_for_conservative_review(self):
        track = self.track_path("Legacy/Album/01.flac")
        with patch.object(sort, "is_committed_stage", return_value=False):
            with patch.object(tag, "read_track_metadata", side_effect=metadata_record) as reader:
                self.assertEqual(set(tag.scan_albums(self.stage)), {track.parent})
                reader.assert_called_once_with(track)
        self.assertEqual(track.read_bytes(), b"disposable fixture")

    def test_managed_looking_names_outside_configured_staging_are_ordinary_folders(self):
        name = sort.staging_import_name("Ordinary album", "directory")
        roots = (self.library, self.root / "Elsewhere/.staging")
        for root in roots:
            track = root / name / "Album/01.flac"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"ordinary outside staging")
            with self.subTest(root=root):
                with patch.object(sort, "is_committed_stage", side_effect=AssertionError("Not staging")):
                    with patch.object(tag, "read_track_metadata", side_effect=metadata_record) as reader:
                        self.assertEqual(set(tag.scan_albums(root)), {track.parent})
                        reader.assert_called_once_with(track)


class PendingScanTests(WorkspaceCase):
    def test_pending_directories_are_never_read_at_any_depth(self):
        good = self.track_path("Import/Album/01.flac")
        pending = [self.track_path(".mixie-pending-import/Album/02.flac"),
                   self.track_path("Import/.mixie-pending-copy/Disc 1/03.mp3")]
        with patch.object(tag, "read_track_metadata", side_effect=lambda path: metadata_record(path)) as reader:
            albums = tag.scan_albums(self.stage)
        self.assertEqual(set(albums), {good.parent})
        self.assertEqual([call.args[0] for call in reader.call_args_list], [good])
        self.assertTrue(all(path.exists() for path in pending))

    def test_scanning_pending_root_is_empty_but_similarly_named_file_is_not_hidden(self):
        pending = self.track_path(".mixie-pending-import/Album/01.flac")
        ordinary = self.track_path("Import/Album/.mixie-pending-song.flac")
        with patch.object(tag, "read_track_metadata", side_effect=lambda path: metadata_record(path)) as reader:
            self.assertEqual(tag.scan_albums(pending.parents[1]), {})
            reader.assert_not_called()
            self.assertEqual(set(tag.scan_albums(ordinary.parent)), {ordinary.parent})
