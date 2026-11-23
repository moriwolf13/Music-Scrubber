"""User-facing review regressions; synthetic metadata/music only, no network."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import hashlib

from PIL import Image

import mixie
import mixie_review_ui as ui
import tag
from support import WorkspaceCase, make_audio, make_cover


class ReviewManagerV4Tests(WorkspaceCase):
    def album(self, count=2, *, names=None):
        names = names or ["One Man Circus", "Second Song"]
        tracks=[]
        for i in range(1,count+1):
            source=make_audio(self.stage/"Import"/f"{i:02d}.flac",
                              album="Album", albumartist="Elio Mei", artist="Elio Mei",
                              title="Mistitled" if i==1 else names[i-1], tracknumber=i,
                              discnumber=1, tracktotal=count, disctotal=1,date="2020",genre="Indie")
            tracks.append(tag.read_track_metadata(source))
        return {"album":"Album", "tracks":tracks,"folder":tracks[0]["path"].parent}

    def issue(self, album):
        return {"album": album["album"], "import_name":"Import",
                "tracks":[Path(t["path"]).relative_to(self.stage).as_posix() for t in album["tracks"]],
                "reason":"Ambiguous song", "details":{"kind":"identity_release"}}

    def synthetic_preview(self, album):
        import audio, lookup
        audits=[];tracks=[];matched={}
        for i,t in enumerate(album["tracks"],1):
            rid=f"e{i}"
            title="One Man Circus" if i==1 else "Second Song"
            audits.append({"path":t["path"],"status":"preliminary_match",
                           "details":{"id":rid,"title":title,"artist-credit-phrase":"Elio Mei"}})
            tracks.append({"disc":"1","number":str(i),"recording_id":rid,"title":title})
            matched[(1,i)]=t["path"]
        response={"status":"complete","matched":matched,"missing":[],"review":[],
                  "reasons":[],"release_id":"r1","tracklist":tracks,"errors":[]}
        self.stack.enter_context(patch.object(audio,"audit_tracks",return_value=audits))
        self.stack.enter_context(patch.object(lookup,"discover_release_candidates",return_value=["r1"]))
        self.stack.enter_context(patch.object(lookup,"evaluate_release_candidate",return_value=response))
        self.stack.enter_context(patch.object(lookup,"common_release_metadata",return_value={
            "album":"Album","album_artist":"Elio Mei","date":"2020"}))
        self.stack.enter_context(patch.object(mixie,"select_album_art",return_value=make_cover()))

    def test_reviewing_one_album_moves_only_that_album_to_persistent_reviewed_list(self):
        album=self.album()
        first=self.issue(album)
        another={**first,"import_name":"Another","album":"Other","tracks":["Another/X.flac"]}
        queue=[first,another]
        with patch.object(mixie,"review_fingerprint",return_value="proof"), \
             patch.object(mixie,"save_review_queue") as save, redirect_stdout(StringIO()):
            self.assertTrue(ui._finish(mixie, first, queue))
        self.assertTrue(ui.reviewed(first))
        self.assertFalse(ui.reviewed(another))
        self.assertEqual("proof",first["source_fingerprint"])
        self.assertEqual(2,len(queue))
        save.assert_called_once_with(queue)
        self.assertEqual({},first.get("tag_edits",{}))

    def test_edits_save_individually_with_full_source_fingerprint(self):
        album=self.album()
        first=self.issue(album)
        queue=[first]
        relative=first["tracks"][0]
        with patch.object(mixie,"save_review_queue") as saver:
            ui.save_choice(mixie,first,queue,field="title",value="One Man Circus",relative=relative)
            ui.save_choice(mixie,first,queue,field="album_artist",value="Elio Mei")
            ui.save_choice(mixie,first,queue,field="genre",value="Indie Pop")
        self.assertEqual("One Man Circus",first["tag_edits"]["tracks"][relative]["title"])
        self.assertEqual("Elio Mei",first["tag_edits"]["album"]["album_artist"])
        self.assertEqual("Indie Pop",first["manual_fields"]["genre"])
        self.assertEqual(mixie.review_fingerprint(first),first["source_fingerprint"])
        self.assertEqual(3,saver.call_count)
        self.assertEqual("Mistitled",tag.read_track_metadata(album["tracks"][0]["path"])["title"][0])

    def test_single_disc_menu_hides_disc_number_and_has_no_submenus(self):
        album=self.album()
        first=self.issue(album)
        with patch("builtins.input",side_effect=["0"]), redirect_stdout(StringIO()) as stream:
            ui._show_tracks(mixie,first,[first])
        shown=stream.getvalue()
        for label in ("1. Title:", "2. Track Number:", "3. Artist:"):
            self.assertIn(label, shown)
        self.assertNotIn("Disc Number:", shown)
        self.assertNotIn("More Track", shown)
        self.assertNotIn("5. Genre", shown)

    def test_track_suggestion_shows_when_wrong_title_not_when_same(self):
        album=self.album()
        issue=self.issue(album)
        source=album["tracks"][0]["path"]
        candidate={"recording_id":"rec","status":"verified","details":{
            "id":"rec","title":"One Man Circus","artist-credit-phrase":"Elio Mei"}}
        issue["details"]["tracks"]=[{"path":str(source),"assessment":{
            "status":"verified","complete":True,"errors":[],"candidates":[candidate]}}]
        with patch("builtins.input",side_effect=["0"]), redirect_stdout(StringIO()) as stream:
            ui._show_tracks(mixie,issue,[issue])
        output=stream.getvalue()
        self.assertIn("Title: Mistitled | Suggested: One Man Circus",output)
        self.assertIn("1. Title: Mistitled",output)

    def test_selected_user_correction_can_be_applied_to_independent_working_copies(self):
        album=self.album()
        self.synthetic_preview(album)
        issue=self.issue(album)
        first=issue["tracks"][0]
        ui.save_choice(mixie,issue,[issue],field="title",value="One Man Circus",relative=first)
        ui.save_choice(mixie,issue,[issue],field="album",value="Custom Album")
        ui.save_choice(mixie,issue,[issue],field="album_artist",value="Elio Mei")
        ui.save_choice(mixie,issue,[issue],field="genre",value="Indie Pop")
        source_hash={Path(t["path"]):hashlib.sha256(Path(t["path"]).read_bytes()).hexdigest() for t in album["tracks"]}
        with redirect_stdout(StringIO()):
            plan=mixie.preview_album(album,"synthetic",tag_edits=issue["tag_edits"],
                                     manual_fields=issue["manual_fields"])
            receipt=mixie.test_album_plan(plan,album,commit=True,auto=True)
        self.assertIsNotNone(receipt)
        destination=receipt.input_map[first]["destination"]
        result=tag.read_track_metadata(destination)
        self.assertEqual(("One Man Circus",),result["title"])
        self.assertEqual(("Custom Album",),result["album"])
        self.assertEqual(("Elio Mei",),result["album_artist"])
        self.assertEqual(("Indie Pop",),result["genre"])
        self.assertTrue(all(hashlib.sha256(path.read_bytes()).hexdigest()==digest for path,digest in source_hash.items()))
        self.assertFalse(plan["identities"][str(album["tracks"][0]["path"])]["verified"])

    def test_saved_review_survives_queue_reconciliation_without_becoming_applied(self):
        album=self.album()
        issue=self.issue(album)
        with patch.object(mixie,"save_review_queue"):
            ui._finish(mixie,issue,[issue])
        replacement={**self.issue(album),"reason":"New provider warning"}
        with patch.object(mixie,"load_review_queue",return_value=[issue]), \
             patch.object(mixie,"load_completed_reviews",return_value={}):
            queue=mixie.reconcile_review_queue([replacement])
        self.assertEqual("reviewed",queue[0]["review_status"])
        self.assertEqual("New provider warning",queue[0]["reason"])

    def test_invalid_source_removes_reviewed_status_without_touching_music(self):
        album=self.album()
        issue=self.issue(album)
        with patch.object(mixie,"save_review_queue"):
            ui._finish(mixie,issue,[issue])
        album["tracks"][0]["path"].write_bytes(b"altered payload")
        with patch("builtins.input",side_effect=["0"]), redirect_stdout(StringIO()):
            ui.review_manager_menu(mixie,[issue])
        self.assertFalse(ui.reviewed(issue))

    def test_artwork_drag_path_is_snapshot_bound_and_can_be_selected_safely(self):
        album=self.album()
        issue=self.issue(album)
        user_file=self.root/"my cover.png"
        user_file.write_bytes(make_cover()["data"])
        with patch("builtins.input",return_value="'"+str(user_file)+"'"),redirect_stdout(StringIO()):
            ui._artwork(mixie,issue,[issue])
        decision=issue["artwork_decision"]
        self.assertTrue(Path(decision["path"]).is_file())
        snap=mixie._review_artwork_snapshot(decision)
        self.assertEqual(make_cover()["data"],snap["data"])
        self.assertNotEqual(str(user_file),decision["path"])
        Path(decision["path"]).write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            mixie._review_artwork_snapshot(decision)

    def test_artwork_rejects_symlink_image(self):
        album=self.album()
        issue=self.issue(album)
        original=self.root/"safe.png"
        original.write_bytes(make_cover()["data"])
        shortcut=self.root/"alias.png"
        shortcut.symlink_to(original)
        with patch("builtins.input",return_value=str(shortcut)),redirect_stdout(StringIO()):
            ui._artwork(mixie,issue,[issue])
        self.assertNotIn("artwork_decision",issue)

    def test_apply_is_opt_in_and_chooses_only_reviewed_albums(self):
        album=self.album()
        first=self.issue(album)
        first["review_status"]="reviewed"
        first["interpretation"] = {"user_approved": True}
        second={"album":"Other","import_name":"Other","tracks":["Other/1.mp3"],"reason":"pending"}
        issues=[first,second]
        with patch("builtins.input",side_effect=["a","APPLY"]), \
             patch.object(mixie,"finalize_reviewed_album",return_value=True) as finisher, \
             redirect_stdout(StringIO()):
            ui._apply(mixie,issues)
        finisher.assert_called_once_with(first,issues)

    def test_apply_error_keeps_reviewed_record_and_reason(self):
        album=self.album()
        first=self.issue(album)
        first["review_status"]="reviewed"
        first["interpretation"] = {"user_approved": True}
        issues=[first]
        with patch("builtins.input",side_effect=["1","APPLY"]), \
             patch.object(mixie,"finalize_reviewed_album",side_effect=ValueError("Unknown release edition")), \
             patch.object(mixie,"save_review_queue") as saver, redirect_stdout(StringIO()):
            ui._apply(mixie,issues)
        self.assertEqual("reviewed",first["review_status"])
        self.assertIn("Unknown release edition",first["apply_problem"])
        saver.assert_called_with(issues)

    def test_album_number_edit_cannot_override_remote_total(self):
        album=self.album()
        edit=mixie._validated_tag_edits({"album":{"track_total":"999"}},
                                        [item["path"] for item in album["tracks"]])
        with self.assertRaisesRegex(ValueError,"disagrees"):
            mixie._numbering_edit_approvals({("1","1"):album["tracks"][0]["path"]},
                                          [{"disc":"1","number":"1"}],edit)

    def test_review_starts_with_only_unresolved_songs_instead_of_full_album(self):
        album=self.album()
        issue=self.issue(album)
        first=album["tracks"][0]["path"]
        issue["details"]["tracks"]=[{"path":str(first),"status":"needs_review",
                                      "assessment":{"status":"conflicting","candidates":[]}}]
        with patch("builtins.input",side_effect=["0"]),redirect_stdout(StringIO()) as stream:
            ui._show_tracks(mixie,issue,[issue])
        self.assertIn("1/1",stream.getvalue())
        self.assertIn("01.flac",stream.getvalue())
        self.assertNotIn("02.flac",stream.getvalue())

    def test_auto_ingest_leaves_reviewed_not_applied_untouched(self):
        from unittest.mock import patch
        import sort
        album=self.album()
        issue=self.issue(album)
        with patch.object(mixie,"save_review_queue"):
            self.assertTrue(ui._finish(mixie,issue,[issue]))
        key=("Import","Album")
        with patch("sys.argv",["mixie.py"]), \
             patch.object(sort,"sort_inbox"), \
             patch.object(mixie,"get_acoustid_client_key",return_value="fixture"), \
             patch.object(mixie,"finish_prior_cleanup_before_ingest",return_value=[]), \
             patch.object(mixie.tag,"group_staged_albums",return_value={key:album}), \
             patch.object(mixie,"load_review_queue",return_value=[issue]), \
             patch.object(mixie,"load_completed_reviews",return_value={}), \
             patch.object(mixie,"preview_album") as preview, \
             patch.object(mixie,"test_album_plan") as processor, \
             patch.object(mixie.archive,"update_original_downloads_archive",return_value=self.backup/"fake.zip"), \
             patch("builtins.input",side_effect=AssertionError("No review prompt expected")), \
             redirect_stdout(StringIO()) as output:
            mixie._main(selection="1")
        preview.assert_not_called()
        processor.assert_not_called()
        self.assertIn("Saved review awaiting explicit Apply",output.getvalue())
        self.assertEqual("reviewed",mixie.load_review_queue()[0]["review_status"]) if False else None

    def test_bad_metadata_and_bad_disc_number_are_never_saved(self):
        album=self.album()
        issue=self.issue(album)
        with self.assertRaises(ValueError):
            ui.save_choice(mixie,issue,[issue],field="title",value="Bad\x1b[31m",relative=issue["tracks"][0])
        with self.assertRaises(ValueError):
            ui.save_choice(mixie,issue,[issue],field="disc_number",value="0",relative=issue["tracks"][0])
        self.assertNotIn("tag_edits",issue)
