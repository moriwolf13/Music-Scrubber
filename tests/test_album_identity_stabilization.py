"""Synthetic identity regressions, NOT live fingerprint acceptance.

Named songs/albums identify the motivating cases, not captured audio or provider
responses. Provider boundaries return invented IDs and timing; collection,
assessment, discovery and album mapping below are the actual algorithms.
Run exclusively through tests/run_isolated.py. Online public metadata validation
is a separate, explicitly invoked provider_metadata_validation.py program.
"""
from copy import deepcopy

from unittest.mock import patch

from support import WorkspaceCase, metadata_record
import audio
import lookup


FINGERPRINT = {"duration": 200, "fingerprint": "SYNTHETIC-ALBUM-IDENTITY-NOT-AUDIO"}
# Shared named shapes only: output tests still synthesize every audio byte, ID,
# fingerprint and timing value, without importing captured provider responses.
BACK_FROM_DEAD_TITLES = ("Back From the Dead", "Wicked Ways", "Strange Girl", "Brightside", "The Steeple",
                         "Terrible Things", "My Redemption", "Bombshell", "I Come First", "Psycho Crazy",
                         "Raise Your Horns")
STRANGE_CASE_TITLES = ("Love Bites (So Do I)", "Mz. Hyde", "I Miss the Misery", "Freak Like Me",
                       "Beautiful With You", "In Your Room", "Break In", "Rock Show", "Daughters of Darkness",
                       "You Call Me a Bitch Like It’s a Bad Thing", "American Boys", "Here’s to Us",
                       "Here’s to Us (guest version)")
AMERICANA_TITLES = ("Welcome", "Have You Ever", "Staring at the Sun", "Pretty Fly (for a White Guy)",
                   "The Kids Aren’t Alright", "Feelings", "She’s Got Issues", "Walla Walla", "The End of the Line",
                   "No Brakes", "Why Don’t You Get a Job?", "Americana", "Pay the Man / Pretty Fly (reprise)")
WITCH_TITLES = ("Shame", "I Want to Believe", "Crawl", "Into the Dark", "Sleeping With the Enemy",
                "Wrapped Around Your Finger", "Heretic", "Father", "Without Me There’s No You",
                "Another Brick in the Wall", "Something I Can Never Have")


def recording(identifier, title, releases=(), *, length=200000, version=""):
    return {"id": identifier, "title": title, "length": length,
            "disambiguation": version, "artist-credit-phrase": "Synthetic Artist",
            "release-count": len(releases), "release-list-complete": True,
            "release-list": [{"id": rid, "title": album} for rid, album in releases]}


def track(identifier, title, number, disc=1):
    return {"disc": str(disc), "number": str(number), "title": title,
            "recording_id": identifier, "track_id": f"synthetic-{disc}-{number}"}


class AlbumIdentityStabilizationTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.records, self.releases, self.calls = {}, {}, []
        for function in (lookup.get_recording_details, lookup.get_release_tracklist):
            function.cache_clear()
            self.addCleanup(function.cache_clear)
        self.stack.enter_context(patch.object(lookup, "_musicbrainz_call", side_effect=self.provider))
        self.acoustid = self.stack.enter_context(patch.object(audio, "identify_audio"))
        for module in (audio, lookup):
            self.stack.enter_context(patch.object(module, "urlopen",
                side_effect=AssertionError("No online providers in synthetic identity tests")))

    def provider(self, operation, *args, **kwargs):
        self.calls.append((operation.__name__, args, deepcopy(kwargs)))
        if operation.__name__ == "get_recording_by_id":
            value = self.records[args[0]]
            if isinstance(value, Exception):
                raise value
            return {"recording": deepcopy(value)}
        if operation.__name__ == "get_release_by_id":
            value = self.releases[args[0]]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, dict):
                return deepcopy(value)
            return {"release": {"id": args[0], "medium-count": 1, "medium-list": [
                {"position": "1", "track-count": len(value), "track-list": [
                    {"id": row["track_id"], "position": row["number"], "number": row["number"],
                     "title": row["title"], "recording": {
                         "id": row["recording_id"], "title": row.get("recording_title", row["title"]),
                         "disambiguation": row.get("recording_disambiguation", ""), "length": "200000"}}
                    for row in value]}]}}
        raise AssertionError(f"Unexpected provider operation: {operation.__name__}")

    def audit(self, path, records, *, scores=None):
        # Some adversarial scenarios replace a synthetic provider observation.
        lookup.get_recording_details.cache_clear()
        self.records.update(deepcopy(records))
        self.acoustid.return_value = [{"score": (scores or {}).get(rid, .99),
            "recordings": [{"id": rid}]} for rid in records]
        evidence = audio.collect_recording_evidence(FINGERPRINT, "DISPOSABLE-SYNTHETIC-KEY")
        assessment = audio.assess_recording_candidates(FINGERPRINT, evidence["candidates"],
            evidence["recordings"], errors=evidence["errors"], complete=evidence["complete"])
        return {"path": path, "status": "preliminary_match" if assessment["status"] == "verified" else "needs_review",
                "details": assessment["details"], "assessment": assessment, "evidence": evidence}

    def album(self, name="Synthetic Album", titles=("One", "Two", "Three")):
        audits, rows = [], []
        for index, title in enumerate(titles, 1):
            rid, other = f"main-{index}", f"other-{index}"
            path = self.track_path(f"synthetic/{name}/shuffled-{len(titles)-index}.flac")
            audits.append(self.audit(path, {
                rid: recording(rid, title, (("album", name),)),
                other: recording(other, title, (("other-edition", name),))}))
            rows.append(track(rid, title, index))
        self.releases["album"] = rows
        return audits, rows

    def evaluate(self, audits, release="album", **kwargs):
        return lookup.evaluate_release_candidate(audits, release, **kwargs)

    def test_back_from_the_dead_all_tracks_multiple_ids_use_canonical_membership(self):
        audits, rows = self.album("Back From the Dead", ("Back From the Dead", "Wicked Ways", "Strange Girl"))
        self.assertTrue(all(a["assessment"]["status"] == "conflicting" for a in audits))
        self.assertTrue(all(len(a["assessment"]["candidates"]) == 2 for a in audits))
        expected = {(1, i): a["path"] for i, a in enumerate(audits, 1)}
        for order in (audits, list(reversed(audits))):
            with self.subTest(order=[a["path"].name for a in order]):
                result = self.evaluate(order)
                self.assertEqual("complete", result["status"])
                self.assertEqual(expected, result["matched"])
                self.assertIn("resolved_by_whole_album_fingerprints", result["reasons"])
        self.assertEqual(1, sum(name == "get_release_by_id" for name, _, _ in self.calls))
        self.assertEqual(rows[0]["recording_id"], result["tracklist"][0]["recording_id"])

    def test_strange_case_wrong_source_positions_never_choose_between_sequences(self):
        audits, rows = self.album("The Strange Case of...", ("Love Bites (So Do I)", "Mz. Hyde", "I Miss the Misery"))
        wrong = [metadata_record(a["path"], track_number=(str(4-i),),
                                 disc_number=("9",), track_total=("99",))
                 for i, a in enumerate(audits, 1)]
        baseline = self.evaluate(audits)
        tagged = self.evaluate(audits, local_tracks=wrong)
        self.assertEqual("complete", baseline["status"])
        self.assertEqual("conflicting", tagged["status"])
        self.assertEqual(baseline["matched"], tagged["matched"])
        self.assertIn("local_track_number_conflict", tagged["reasons"])
        self.releases["reordered"] = [dict(row, number=str(4-i)) for i, row in enumerate(rows, 1)]
        other = self.evaluate(audits, "reordered", local_tracks=wrong)
        self.assertNotEqual(baseline["matched"], other["matched"])
        self.assertEqual(set(baseline["matched"].values()), set(other["matched"].values()))

    def test_unique_global_assignment_can_resolve_overlapping_on_release_ids(self):
        # Every file has multiple eligible IDs; even two ON-release edges on the
        # first file are safe only when the entire release has one assignment.
        audits, rows = self.album()
        title = rows[0]["title"]
        rows[1]["title"] = title
        audits[0] = self.audit(audits[0]["path"], {
            "main-1": recording("main-1", title, (("album", "Synthetic Album"),)),
            "main-2": recording("main-2", title, (("album", "Synthetic Album"),))})
        audits[1] = self.audit(audits[1]["path"], {
            "main-2": recording("main-2", title, (("album", "Synthetic Album"),)),
            "off-release": recording("off-release", title)})
        result = self.evaluate(list(reversed(audits)))
        self.assertEqual("complete", result["status"])
        self.assertEqual({(1, i): a["path"] for i, a in enumerate(audits, 1)}, result["matched"])

    def test_multiple_album_assignments_never_use_file_order_or_local_numbers(self):
        audits, rows = self.album(titles=("Same song", "Same song", "Third song"))
        for i in range(2):
            audits[i] = self.audit(audits[i]["path"], {
                rid: recording(rid, "Same song", (("album", "Synthetic Album"),))
                for rid in ("main-1", "main-2")})
        for order in (audits, list(reversed(audits))):
            result = self.evaluate(order, local_tracks=[metadata_record(a["path"],
                track_number=(str(i),)) for i, a in enumerate(audits, 1)])
            self.assertNotEqual("complete", result["status"])
            self.assertEqual({}, result["matched"])

    def test_oversubscribed_album_cannot_manufacture_unique_assignment(self):
        audits, rows = self.album(titles=("Same song", "Same song", "Third song"))
        for i in range(2):
            audits[i] = self.audit(audits[i]["path"], {
                "main-1": recording("main-1", "Same song", (("album", "Synthetic Album"),)),
                f"outside-{i}": recording(f"outside-{i}", "Same song")})
        self.assertNotEqual("complete", self.evaluate(audits)["status"])

    def test_material_versions_veto_but_clean_explicit_catalogs_are_equivalent(self):
        for version in ("live at Brixton, 2024", "Nicole Moudaber remix", "radio edit", "clean", "explicit",
                        "acoustic version", "instrumental", "re-recording, 2024"):
            for location in ("title", "disambiguation"):
                with self.subTest(version=version, location=location):
                    lookup.get_release_tracklist.cache_clear()
                    lookup.get_recording_details.cache_clear()
                    audits, rows = self.album()
                    base = rows[1]["title"]
                    competitor = recording("version-competitor", base if location == "disambiguation" else f"{base} ({version})",
                        (("different-version", "Synthetic Album"),), version=version if location == "disambiguation" else "")
                    audits[1] = self.audit(audits[1]["path"], {
                        "main-2": recording("main-2", base, (("album", "Synthetic Album"),)),
                        "version-competitor": competitor})
                    result = self.evaluate(audits)
                    if version in ("clean", "explicit"):
                        self.assertEqual("complete", result["status"])
                        self.assertIn(audits[1]["path"], result["matched"].values())
                    else:
                        self.assertNotEqual("complete", result["status"])
                        self.assertIn("unresolved_recording_version", result["reasons"])
                        self.assertNotIn(audits[1]["path"], result["matched"].values())

    def test_release_version_label_alone_cannot_discriminate_fingerprint_aliases(self):
        for qualifier in ("live", "radio edit", "clean", "explicit"):
            with self.subTest(qualifier=qualifier):
                lookup.get_release_tracklist.cache_clear()
                audits, rows = self.album()
                rows[1]["recording_title"] = "Two"
                rows[1]["title"] = f"Two ({qualifier})"
                result = self.evaluate(audits)
                if qualifier in ("clean", "explicit"):
                    self.assertEqual("complete", result["status"])
                else:
                    self.assertNotEqual("complete", result["status"])
                    self.assertIn("unresolved_recording_version", result["reasons"])

    def test_release_and_recording_corroborated_version_still_allows_cohort(self):
        audits, rows = self.album()
        rows[1]["recording_disambiguation"] = "explicit"
        audits[1] = self.audit(audits[1]["path"], {
            "main-2": recording("main-2", "Two", version="explicit"),
            "other-2": recording("other-2", "Two", version="explicit")})
        self.assertEqual("complete", self.evaluate(audits)["status"])

    def test_anchor_context_accepts_clean_explicit_equivalent_song(self):
        audits, rows = self.album()
        for i in (0, 2):
            rid = f"main-{i+1}"
            audits[i] = self.audit(audits[i]["path"], {rid: recording(rid, rows[i]["title"])})
        audits[1] = self.audit(audits[1]["path"], {
            "main-2": recording("main-2", "Two", version="explicit"),
            "clean-2": recording("clean-2", "Two", version="clean")})
        result = self.evaluate(audits)
        self.assertEqual("complete", result["status"])
        self.assertEqual(3, len(result["matched"]))

    def test_version_with_failing_audio_gate_is_not_an_unresolved_competitor(self):
        for score, length in ((.949999, 200000), (.99, 203001)):
            with self.subTest(score=score, length=length):
                lookup.get_recording_details.cache_clear()
                lookup.get_release_tracklist.cache_clear()
                audits, rows = self.album()
                audits[1] = self.audit(audits[1]["path"], {
                    "main-2": recording("main-2", "Two"),
                    "unknown-alias": recording("unknown-alias", "Two"),
                    "clean": recording("clean", "Two", version="clean", length=length)}, scores={"clean": score})
                self.assertEqual("complete", self.evaluate(audits)["status"])

    def test_unresolved_timing_off_release_is_not_negative_identity_evidence(self):
        audits, rows = self.album()
        audits[1] = self.audit(audits[1]["path"], {
            "main-2": recording("main-2", "Two"),
            "untimed": recording("untimed", "Two", length=None)})
        self.assertNotEqual("complete", self.evaluate(audits)["status"])

    def test_cohort_retains_provider_failures_and_candidate_budget_exhaustion(self):
        for failure in (TimeoutError("synthetic metadata outage"), ValueError("synthetic malformed response"), "budget"):
            with self.subTest(failure=str(failure)):
                lookup.get_recording_details.cache_clear()
                audits, rows = self.album()
                records = {"main-2": recording("main-2", "Two"), "other-2": failure}
                if failure == "budget":
                    records = {f"candidate-{i:02}": recording(f"candidate-{i:02}", "Two") for i in range(13)}
                audits[1] = self.audit(audits[1]["path"], records)
                result = self.evaluate(audits)
                self.assertNotEqual("complete", result["status"])
                self.assertTrue(result["errors"])
                self.assertEqual(isinstance(failure, TimeoutError), result["retryable"])

    def test_incomplete_tracklist_and_partial_local_album_cannot_establish_cohort(self):
        audits, rows = self.album()
        self.releases["album"] = {"release": {"id": "album", "medium-count": 1,
            "medium-list": [{"position": "1", "track-count": 4, "track-list": [
                {"number": row["number"], "recording": {"id": row["recording_id"], "title": row["title"]}}
                for row in rows]}]}}
        result = self.evaluate(audits)
        self.assertNotEqual("complete", result["status"])
        self.assertIn("remote_tracklist_incomplete", result["reasons"])
        lookup.get_release_tracklist.cache_clear()
        self.releases["album"] = rows
        self.assertNotEqual("complete", self.evaluate(audits[:2])["status"])

    def test_repeated_remote_recording_never_uses_source_positions_as_evidence(self):
        audits, rows = self.album(titles=("Same song", "Same song", "Third song"))
        rows[1]["recording_id"] = "main-1"
        audits[1] = deepcopy(audits[0]) | {"path": audits[1]["path"]}
        result = self.evaluate(audits, local_tracks=[metadata_record(a["path"], track_number=(str(i),))
                                                   for i, a in enumerate(audits, 1)])
        self.assertNotEqual("complete", result["status"])

    def test_in_this_moment_witch_whole_album_aliases_and_feature_display_titles(self):
        audits, rows = self.album("Witch", WITCH_TITLES)
        rows[1]["recording_title"] = "I Want to Believe"
        rows[1]["title"] = "I Want to Believe (feat. Rory Rodriguez of Dayseeker)"
        before = deepcopy(audits)
        result = self.evaluate(list(reversed(audits)))
        self.assertEqual("complete", result["status"])
        self.assertEqual(11, len(result["matched"]))
        self.assertEqual({(1, i): audit["path"] for i, audit in enumerate(audits, 1)}, result["matched"])
        self.assertEqual(before, audits, "canonical mapping must not rewrite context-free fingerprint evidence")

    def test_in_this_moment_witch_single_delivery_cannot_prove_the_full_album(self):
        audits, _ = self.album("Witch", WITCH_TITLES)
        self.assertNotEqual("complete", self.evaluate(audits[:1])["status"])
        self.assertEqual({}, self.evaluate(audits[:1])["matched"])

    def test_strange_case_freak_like_me_equivalent_clean_explicit_catalogs(self):
        audits, rows = self.album("The Strange Case of...", ("Freak Like Me", "Mz. Hyde", "I Miss the Misery"))
        rows[0]["recording_disambiguation"] = "explicit"
        audits[0] = self.audit(audits[0]["path"], {
            "main-1": recording("main-1", "Freak Like Me", (("album", "The Strange Case of…"),), version="explicit"),
            "clean-freak": recording("clean-freak", "Freak Like Me", (("clean-edition", "The Strange Case of…"),), version="clean")})
        result = self.evaluate(audits)
        self.assertEqual("complete", result["status"])
        self.assertEqual(3, len(result["matched"]))

    def test_can_u_see_me_in_the_dark_joint_single_requires_audio_discrimination(self):
        title = "can u see me in the dark?"
        path = self.track_path("Halestorm & I Prevail/can u see me in the dark/single.flac")
        records = {"explicit": recording("explicit", title, (("album", title),), version="explicit"),
                   "clean": recording("clean", title, (("clean-single", title),), version="clean")}
        for value in records.values():
            value["artist-credit-phrase"] = "Halestorm & I Prevail"
        self.releases["album"] = [track("explicit", title, 1)]
        ambiguous = self.audit(path, records)
        self.assertEqual("complete", self.evaluate([ambiguous])["status"])
        self.assertEqual("explicit", ambiguous["details"]["id"])
        lookup.get_recording_details.cache_clear()
        records["clean"]["length"] = 204000
        discriminated = self.audit(path, records)
        self.assertEqual("complete", self.evaluate([discriminated])["status"])
        self.assertEqual("Halestorm & I Prevail", discriminated["details"]["artist-credit-phrase"])

    def test_independent_release_context_accepts_clean_explicit_preference(self):
        records = {"explicit": recording("explicit", "Song", (("release", "Album"),), version="explicit"),
                   "clean": recording("clean", "Song", (("other", "Album"),), version="clean")}
        result = audio.assess_recording_candidates(FINGERPRINT, {rid: .99 for rid in records},
                                                   records, release_id="release")
        self.assertEqual("verified", result["status"])
        self.assertNotIn("unresolved_recording_version", result["reasons"])

    def test_two_full_albums_with_distinct_unlabelled_ids_remain_distinct_reports(self):
        audits, rows = self.album()
        self.releases["other-edition"] = [dict(row, recording_id=f"other-{i}")
                                         for i, row in enumerate(rows, 1)]
        first, second = self.evaluate(audits), self.evaluate(audits, "other-edition")
        self.assertEqual("complete", first["status"])
        self.assertEqual("complete", second["status"])
        self.assertNotEqual([t["recording_id"] for t in first["tracklist"]],
                            [t["recording_id"] for t in second["tracklist"]])
        self.assertTrue(all(a["details"] is None for a in audits), "evaluation must not rewrite raw identities")

    def test_americana_catalog_suffix_ranks_audio_coverage_before_96_call_exhaustion(self):
        audits, _ = self.album("Americana", ("Welcome", "Have You Ever", "Staring at the Sun"))
        decoys = [(f"compilation-{i:03}", f"Compilation {i}") for i in range(110)]
        for audit in audits:
            for row in audit["assessment"]["candidates"]:
                row["details"]["release-list"].extend({"id": rid, "title": title} for rid, title in decoys)
                for member in row["details"]["release-list"]:
                    if member["id"] == "album":
                        member["id"] = "zz-esca7393"
        for hint in ("Americana (ESCA 7393)", "Americana (ESCA7393)"):
            with self.subTest(hint=hint):
                before = len(self.calls)
                ranked = lookup.discover_release_candidates(audits, local_album=hint)
                self.assertEqual({"other-edition", "zz-esca7393"}, set(ranked[:2]))
                self.assertEqual(112, len(ranked))
                self.assertEqual(before, len(self.calls), "ranking must not perform provider requests")
                self.assertIn("zz-esca7393", ranked[:lookup.MAX_RELEASE_EVALUATIONS])

    def test_americana_full_synthetic_album_resolves_with_one_bounded_release_fetch(self):
        titles = AMERICANA_TITLES
        audits, rows = self.album("Americana", titles)
        self.releases["zz-esca7393"] = rows
        decoys = [{"id": f"compilation-{i:03}", "title": "Compilation"} for i in range(110)]
        for index, audit in enumerate(audits):
            for candidate in audit["assessment"]["candidates"]:
                details = candidate["details"]
                details["release-list"] = ([{"id": "zz-esca7393", "title": "Americana"}]
                    if candidate["recording_id"].startswith("main-") else
                    [{"id": f"single-{index}", "title": titles[index]}])
                if index < 2:
                    details["release-list"].extend(deepcopy(decoys))
                details["release-count"] = len(details["release-list"])
        before = len(self.calls)
        ranked = lookup.discover_release_candidates(audits, local_album="Americana (ESCA 7393)")
        self.assertGreater(len(ranked), lookup.MAX_RELEASE_EVALUATIONS)
        self.assertEqual("zz-esca7393", ranked[0])
        report = self.evaluate(list(reversed(audits)), ranked[0])
        consensus = lookup.compare_release_candidate_reports(audits, [report], unexamined_release_ids=ranked[1:])
        self.assertEqual("complete", report["status"])
        self.assertEqual("complete", consensus["status"])
        self.assertEqual([], consensus["unresolved_release_ids"])
        self.assertEqual({(1, i): audit["path"] for i, audit in enumerate(audits, 1)}, report["matched"])
        self.assertEqual(before + 1, len(self.calls), "one release fetch, no speculative provider searches")

    def test_candidate_id_multiplicity_cannot_outrank_distinct_audio_file_coverage(self):
        audits, _ = self.album()
        for i, audit in enumerate(audits):
            for row in audit["assessment"]["candidates"]:
                row["details"]["release-list"] = [{"id": "full-coverage", "title": "Unhinted"}]
            if i == 0:
                title = "One"
                audit["assessment"]["candidates"].extend({"recording_id": f"many-{j}", "status": "verified",
                    "details": recording(f"many-{j}", title, (("one-file", "Hinted"),))} for j in range(10))
        ranked = lookup.discover_release_candidates(audits, local_album="Hinted")
        self.assertEqual(["full-coverage", "one-file"], ranked)

    def test_incorrect_album_hint_never_outranks_independent_audio_support(self):
        audits, _ = self.album()
        anchor = self.audit(self.track_path("independent/file.flac"), {
            "independent": recording("independent", "Song", (("anchor-release", "Actual"),))})
        self.assertEqual("anchor-release", lookup.discover_release_candidates(
            [anchor] + audits, local_album="Synthetic Album")[0])

    def test_typographic_ellipsis_in_strange_case_is_only_a_discovery_hint(self):
        audit = self.audit(self.track_path(), {"rec": recording("rec", "Song", (
            ("a-unrelated", "Compilation"), ("z-strange-case", "The Strange Case of…")))})
        self.assertEqual("z-strange-case", lookup.discover_release_candidates(
            [audit], local_album="The Strange Case of...")[0])

    def test_remote_positions_not_printed_vinyl_numbers_define_canonical_sequence(self):
        audits, rows = self.album()
        payload = self.provider(lookup.musicbrainzngs.get_release_by_id, "album")
        for i, row in enumerate(payload["release"]["medium-list"][0]["track-list"], 1):
            row["number"] = f"A{i}"
        self.releases["album"] = payload
        result = self.evaluate(audits)
        self.assertEqual("complete", result["status"])
        self.assertEqual({(1, i): a["path"] for i, a in enumerate(audits, 1)}, result["matched"])
        self.assertEqual(["A1", "A2", "A3"], [row["printed_number"] for row in result["tracklist"]])

    def test_mismatched_release_response_is_not_positive_membership_evidence(self):
        audits, rows = self.album()
        payload = self.provider(lookup.musicbrainzngs.get_release_by_id, "album")
        payload["release"]["id"] = "wrong-provider-release"
        self.releases["album"] = payload
        result = self.evaluate(audits)
        self.assertNotEqual("complete", result["status"])
        self.assertTrue(result["errors"])

    def test_album_consensus_accepts_distinct_recording_ids_for_the_same_anchored_songs(self):
        # Two verified releases may have distinct MBIDs without containing
        # different music. Match the named songs to source files and their
        # canonical sequence, not opaque recording identifiers.
        audits, rows = self.album()
        self.releases["other-edition"] = [dict(row, recording_id=f"other-{i}")
                                          for i, row in enumerate(rows, 1)]
        reports = [self.evaluate(audits), self.evaluate(audits, "other-edition")]
        result = lookup.compare_release_candidate_reports(audits, reports)
        self.assertEqual("complete", result["status"])
        self.assertEqual(["album", "other-edition"], result["compatible_release_ids"])
        self.assertEqual(1, len(set(result["signatures"].values())))

    def test_album_consensus_never_treats_a_provider_outage_as_an_eliminated_competitor(self):
        audits, rows = self.album()
        self.releases["other-edition"] = TimeoutError("synthetic competing edition outage")
        reports = [self.evaluate(audits), self.evaluate(audits, "other-edition")]
        result = lookup.compare_release_candidate_reports(audits, reports)
        self.assertEqual("partial", result["status"])
        self.assertEqual(["other-edition"], result["unresolved_release_ids"])
        self.assertTrue(result["retryable"])
        self.assertTrue(result["errors"])

    def test_album_consensus_retains_nominated_and_explicitly_unexamined_candidates(self):
        audits, rows = self.album()
        before = len(self.calls)
        result = lookup.compare_release_candidate_reports(audits, [self.evaluate(audits)])
        self.assertEqual("partial", result["status"])
        self.assertEqual(["other-edition"], result["unresolved_release_ids"])
        self.assertEqual(before + 1, len(self.calls), "comparison must make no provider requests")
        for audit in audits:
            for row in audit["assessment"]["candidates"]:
                row["details"]["release-list-complete"] = False
        result = lookup.compare_release_candidate_reports(audits, [self.evaluate(audits)],
                                                          unexamined_release_ids=["new-unknown"])
        self.assertEqual("partial", result["status"])
        self.assertIn("new-unknown", result["unresolved_release_ids"])
        self.assertIn("recording_membership_incomplete", result["reasons"])

    def test_album_consensus_disc_boundaries_and_source_numbers_cannot_break_ties(self):
        audits, rows = self.album()
        # Same ordered recording IDs can be divided over physical discs.
        self.releases["other-edition"] = {"release": {"id": "other-edition", "medium-count": 2,
            "medium-list": [{"position": str(disc), "track-count": len(indices), "track-list": [
                {"number": str(i), "recording": {"id": rows[index]["recording_id"], "title": rows[index]["title"]}}
                for i, index in enumerate(indices, 1)]} for disc, indices in ((1, (0, 1)), (2, (2,)))]}}
        reports = [self.evaluate(audits), self.evaluate(audits, "other-edition")]
        self.assertEqual("complete", lookup.compare_release_candidate_reports(audits, reports)["status"])
        self.releases["reordered"] = [dict(row, number=str(4-i)) for i, row in enumerate(rows, 1)]
        report = self.evaluate(audits, "reordered", local_tracks=[metadata_record(a["path"], track_number=(str(4-i),))
                                                                 for i, a in enumerate(audits, 1)])
        conflict = lookup.compare_release_candidate_reports(audits, reports + [report])
        self.assertEqual("conflicting", conflict["status"])

    def test_album_consensus_requires_explicit_evidence_to_ignore_unchecked_release(self):
        audits, rows = self.album()
        # Positive whole-album membership excludes the alias candidates, while
        # complete memberships prove the unexamined alias-only release cannot
        # contain the third supplied song. No network absence is fabricated.
        audits[2]["assessment"]["candidates"][1]["details"]["release-list"] = []
        result = lookup.compare_release_candidate_reports(audits, [self.evaluate(audits)],
                                                          unexamined_release_ids=["not-a-member"])
        self.assertEqual("complete", result["status"])
        self.assertEqual([], result["unresolved_release_ids"])
        for audit in audits:
            for row in audit["assessment"]["candidates"]:
                row["details"]["release-list-complete"] = False
        uncertain = lookup.compare_release_candidate_reports(audits, [self.evaluate(audits)],
                                                             unexamined_release_ids=["not-a-member"])
        self.assertEqual("partial", uncertain["status"])
        self.assertIn("not-a-member", uncertain["unresolved_release_ids"])

    def test_same_recording_ids_with_clean_release_labels_are_equivalent(self):
        audits, rows = self.album()
        # Independent audio IDs do not make contradictory provider version
        # claims equivalent. Both actual evaluator reports remain available.
        for i, audit in enumerate(audits, 1):
            rid = f"main-{i}"
            audits[i-1] = self.audit(audit["path"], {rid: recording(rid, rows[i-1]["title"], (
                ("album", "Synthetic Album"), ("other-edition", "Synthetic Album")))})
        self.releases["other-edition"] = deepcopy(rows)
        self.releases["other-edition"][1]["title"] = "Two (clean)"
        result = lookup.compare_release_candidate_reports(audits,
            [self.evaluate(audits), self.evaluate(audits, "other-edition")])
        self.assertEqual("complete", result["status"])

    def _context_album(self, *, weak_score=.808, orphan_delta=0, anchors=7, version_conflict=False):
        """Thirteen independent synthetic sources; no original tags establish identity."""
        titles = tuple(f"Song {i}" for i in range(1, 14))
        audits, rows = self.album("Americana", titles)
        for i, item in enumerate(audits):
            rid, title = f"main-{i+1}", rows[i]["title"]
            records = {rid: recording(rid, title, (("album", "Americana"),))}
            if i < anchors:
                audits[i] = self.audit(item["path"], records)
            elif i < 12:
                if version_conflict and i == 7:
                    records["live"] = recording("live", title, version="live")
                    audits[i] = self.audit(item["path"], records, scores={rid: .99, "live": .99})
                else:
                    audits[i] = self.audit(item["path"], records, scores={rid: weak_score})
            else:
                audits[i] = self.audit(item["path"], {})
                audits[i]["evidence"]["fingerprint"]["duration"] += orphan_delta
        metadata = [metadata_record(a["path"], track_number=(str(14-i),),
                    track_total=("13",), disc_number=("1",))
                    for i, a in enumerate(audits, 1)]
        return audits, rows, metadata

    def test_americana_album_context_recovers_weak_hits_and_unique_unmatched_pay_the_man(self):
        audits, rows, local = self._context_album()
        result = self.evaluate(list(reversed(audits)), local_tracks=local)
        self.assertEqual(13, len(result["matched"]))
        self.assertEqual({(1, i): audit["path"] for i, audit in enumerate(audits, 1)}, result["matched"])
        self.assertIn("resolved_by_album_context", result["reasons"])
        self.assertEqual(6, len(result["album_context_paths"]))
        self.assertIn("local_track_number_conflict", result["reasons"])
        # The original, standalone evidence remains unmodified.
        self.assertEqual("needs_review", audits[-1]["status"])
        self.assertEqual("needs_review", audits[8]["status"])
        self.assertEqual("complete", lookup.compare_release_candidate_reports(
            audits, [result])["status"])

    def test_americana_orphan_duration_within_eighty_milliseconds_is_supported(self):
        audits, _, _ = self._context_album(orphan_delta=.08)
        self.assertEqual(13, len(self.evaluate(audits)["matched"]))

    def test_americana_orphan_is_not_inferred_from_a_one_second_mismatch(self):
        audits, _, _ = self._context_album(orphan_delta=1)
        self.assertNotEqual(13, len(self.evaluate(audits)["matched"]))

    def test_americana_weak_hit_below_context_floor_does_not_get_promoted(self):
        audits, _, _ = self._context_album(weak_score=.6)
        self.assertNotEqual(13, len(self.evaluate(audits)["matched"]))

    def test_americana_context_requires_multiple_independent_album_anchors(self):
        audits, _, _ = self._context_album(anchors=1)
        self.assertNotEqual(13, len(self.evaluate(audits)["matched"]))

    def test_americana_context_retains_real_live_version_conflict(self):
        audits, _, _ = self._context_album(version_conflict=True)
        result = self.evaluate(audits)
        self.assertNotEqual(13, len(result["matched"]))
        self.assertTrue("unresolved_recording_version" in result["reasons"] or
                        "unresolved_recording_identity" in result["reasons"])

    def test_clean_explicit_catalog_prefers_explicit_without_claiming_lyric_detection(self):
        records = {
            "clean": recording("clean", "Song 1", version="clean"),
            "explicit": recording("explicit", "Song 1", version="explicit")}
        result = audio.assess_recording_candidates(FINGERPRINT,
            {"clean": .99, "explicit": .99}, records)
        self.assertEqual("verified", result["status"])
        self.assertEqual("explicit", result["details"]["id"])
        self.assertIn("clean_explicit_catalog_equivalent", result["reasons"])
        # A real alternate take is not a catalog alias, even with matching time.
        records["live"] = recording("live", "Song 1", version="live")
        blocked = audio.assess_recording_candidates(FINGERPRINT,
            {"clean": .99, "explicit": .99, "live": .99}, records)
        self.assertNotEqual("verified", blocked["status"])

    def test_provider_validation_module_has_no_import_time_network_or_test_entry_point(self):
        import provider_metadata_validation as probe
        self.assertFalse(probe.__file__.rsplit("/", 1)[-1].startswith("test_"))
        self.assertNotIn("audio.py", probe.SOURCE_FILES)
        self.assertEqual(40, probe.MAX_HTTP_CALLS)
        self.assertEqual(180, probe.MAX_SECONDS)

    def test_album_consensus_preserves_incomplete_competitor_tracklist(self):
        audits, rows = self.album()
        partial = lookup.ReleaseTracklist()
        partial.extend(rows)
        partial.complete = False
        with patch.object(lookup, "get_release_tracklist", return_value=partial):
            incomplete = self.evaluate(audits, "other-edition")
        result = lookup.compare_release_candidate_reports(audits, [self.evaluate(audits), incomplete])
        self.assertEqual("partial", result["status"])
        self.assertEqual(["other-edition"], result["unresolved_release_ids"])

    def test_cohort_score_and_three_second_duration_boundaries_remain_exact(self):
        for score, length, expected in ((.95, 203000, "complete"), (.95, 197000, "complete"),
                                        (.949999, 200000, "conflicting"), (.99, 203001, "conflicting")):
            with self.subTest(score=score, length=length):
                audits, rows = self.album()
                audits[1] = self.audit(audits[1]["path"], {
                    "main-2": recording("main-2", "Two", length=length),
                    "other-2": recording("other-2", "Two")}, scores={"main-2": score})
                self.assertEqual(expected, self.evaluate(audits)["status"])

    def test_large_cyclic_assignment_stays_ambiguous_without_permutation_search(self):
        count = 80
        audits, rows = self.album(titles=tuple("Same song" for _ in range(count)))
        for index, audit in enumerate(audits):
            audit["assessment"] = audio.assess_recording_candidates(FINGERPRINT,
                {f"main-{index+1}": .99, f"main-{(index+1) % count+1}": .99},
                {rid: recording(rid, "Same song", (("album", "Synthetic Album"),))
                 for rid in (f"main-{index+1}", f"main-{(index+1) % count+1}")})
        before = len(self.calls)
        result = self.evaluate(audits)
        self.assertEqual("partial", result["status"])
        self.assertEqual({}, result["matched"])
        self.assertEqual(before + 1, len(self.calls), "album matching must not hydrate extra recordings")

    def test_canonical_recording_title_and_disambiguation_survive_tracklist_fetch(self):
        audits, rows = self.album()
        rows[0]["recording_title"] = "Canonical recording title"
        rows[0]["recording_disambiguation"] = "album version w/ spoken outro"
        snapshot = lookup.get_release_tracklist("album")
        self.assertEqual("Canonical recording title", snapshot[0]["recording_title"])
        self.assertEqual("album version w/ spoken outro", snapshot[0]["recording_disambiguation"])
        snapshot[0]["recording_disambiguation"] = "caller mutation"
        self.assertNotEqual("caller mutation", lookup.get_release_tracklist("album")[0]["recording_disambiguation"])
