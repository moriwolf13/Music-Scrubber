"""MIXIE automatic-ingest orchestration tests. No real disk writes or deletion.

Uses temporary dummy staged paths and mocks every production-writing operation.
This tests call order and failure handling; it does not substitute for the
already-passed real purge mechanics or future full end-to-end ingestion test.
"""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import sys

import mixie
import sort
import tag
import archive


def check_main(mode):
    events = []
    recorded = []
    saved_queues = []

    with TemporaryDirectory(prefix="mixie-auto-integration-") as tmp:
        root = Path(tmp)
        stage = root / ".staging"
        stage.mkdir()

        ready_path = stage / "Completed Download" / "Album One" / "01.mp3"
        pending_path = stage / "Unfinished Download" / "Album Two" / "01.mp3"
        for path in (ready_path, pending_path):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"SAFE TEST FIXTURE")

        ready_album = {
            "album": "Album One",
            "tracks": [{"path": ready_path}],
        }
        pending_album = {
            "album": "Album Two",
            "tracks": [{"path": pending_path}],
        }

        albums = {
            ("Completed Download", "Album One"): ready_album,
            ("Unfinished Download", "Album Two"): pending_album,
        }
        if mode == "unresolved_only":
            albums = {("Unfinished Download", "Album Two"): pending_album}
        elif mode == "empty_staging":
            albums = {}

        def preview(album, api_key, **kwargs):
            events.append("preview:" + album["album"])
            if album["album"] == "Album Two":
                raise ValueError("Release needs manual review")
            return {"verified": True}

        def commit(plan, album, **kwargs):
            events.append("commit:" + album["album"])
            assert kwargs.get("commit") is True
            assert kwargs.get("auto") is True
            return {str(root / "Music Library" / "Album One" / "01.mp3"): "dummy-digest"}

        def save_queue(issues):
            saved_queues.append([dict(item) for item in issues])
            events.append("save_queue")
            return root / ".needs_review.json"

        def original_archive():
            events.append("originals_archive")
            return root / "OriginalDownloads-Bulk.zip"

        def library_archive(*args, **kwargs):
            events.append("library_archive")
            if mode == "backup_failure":
                raise OSError("SIMULATED: library backup unavailable")
            return root / "MusicLibrary-fixture.zip"

        def record(issue, receipt, originals_zip, library_zip):
            assert issue["album"] == "Album One"
            assert issue["import_name"] == "Completed Download"
            assert issue["tracks"] == ["Completed Download/Album One/01.mp3"]
            assert originals_zip.name == "OriginalDownloads-Bulk.zip"
            assert library_zip.name == "MusicLibrary-fixture.zip"
            assert receipt
            recorded.append(issue)
            events.append("record_completion")

        def quarantine(originals_zip, library_zip, *, allow_source_cleanup=False):
            assert allow_source_cleanup is True
            # A completed import must be recorded before it can quarantine.
            if mode == "normal":
                assert len(recorded) == 1
            events.append("quarantine")
            return ["Completed Download"] if recorded else []

        def purge(*, allow_source_cleanup=False):
            assert allow_source_cleanup is True
            events.append("purge")
            return ["Completed Download"] if recorded else []

        def is_complete(issue, entries):
            return mode == "already_completed" and issue["import_name"] == "Completed Download"

        with ExitStack() as stack:
            for obj, attribute, replacement in (
                (tag, "staging_folder", stage),
                (sort, "sort_inbox", lambda: events.append("sort_inbox")),
                (tag, "group_staged_albums", lambda: albums),
                (mixie, "load_review_queue", lambda: []),
                (mixie, "load_completed_reviews", lambda: {}),
                (mixie, "is_review_complete", is_complete),
                (mixie, "preview_album", preview),
                (mixie, "test_album_plan", commit),
                (mixie, "reconcile_review_queue", lambda issues: issues),
                (mixie, "save_review_queue", save_queue),
                (archive, "update_original_downloads_archive", original_archive),
                (archive, "create_verified_zip", library_archive),
                (mixie, "record_completed_review", record),
                (mixie, "auto_quarantine_archived_completed", quarantine),
                (mixie, "auto_purge_verified_quarantines", purge),
            ):
                stack.enter_context(patch.object(obj, attribute, replacement))
            stack.enter_context(patch.object(sys, "argv", ["mixie.py", "--allow-source-cleanup"]))
            stack.enter_context(patch.dict(os.environ, {"ACOUSTID_CLIENT_KEY": "DUMMY-TEST-KEY"}))
            stack.enter_context(patch("builtins.input", side_effect=["1", "n"]))
            mixie.main()

        assert events[0] == "sort_inbox", events
        assert not any("Unfinished Download" == item["import_name"] for item in recorded)
        if mode == "normal":
            assert len(recorded) == 1
            assert len(saved_queues) == 2
            assert len(saved_queues[-1]) == 1
            assert saved_queues[-1][0]["album"] == "Album Two"
            assert events.index("library_archive") < events.index("record_completion")
            assert events.index("record_completion") < events.index("quarantine")
            assert events.index("quarantine") < events.index("purge")
        elif mode == "backup_failure":
            assert not recorded
            assert "quarantine" not in events and "purge" not in events
            assert saved_queues and saved_queues[-1][0]["album"] == "Album Two"
        elif mode == "unresolved_only":
            assert not recorded
            assert saved_queues and saved_queues[-1][0]["album"] == "Album Two"
            # Empty library with no verified outputs is not an archive/cleanup
            # error or an eligible cleanup transaction; earlier recovery is
            # already handled in finish_prior_cleanup_before_ingest.
            assert "quarantine" not in events and "purge" not in events
        elif mode == "already_completed":
            assert not recorded
            assert "preview:Album One" not in events
            assert "commit:Album One" not in events
            assert "quarantine" in events and "purge" in events
        elif mode == "empty_staging":
            assert "preview:Album One" not in events
            assert "purge" in events, (
                "When there are no staged albums, Ingest must still resume "
                "a previously verified purge; see empty-staging fix."
            )

        assert ready_path.is_file() and pending_path.is_file()
        print("PASS:", mode, "| dummy files unchanged")


def check_route_isolation():
    called = []
    def broken_archive(*, approved=False):
        assert approved is True
        called.append("archive")
        raise RuntimeError("SIMULATED archive route failure")
    def safe_inbox(*, approved=False):
        assert approved is True
        called.append("inbox")
        return ["Safe Fixture"]

    with (
        patch.object(mixie, "purge_verified_archived_quarantine", broken_archive),
        patch.object(mixie, "purge_verified_inbox_quarantine", safe_inbox)
    ):
        assert mixie.auto_purge_verified_quarantines() == []
        assert called == [], called
        result = mixie.auto_purge_verified_quarantines(allow_source_cleanup=True)

    assert called == ["archive", "inbox"], called
    assert result == ["Safe Fixture"], result
    print("PASS: route isolation | Inbox still runs after archive failure")


for mode in ("normal", "backup_failure", "unresolved_only", "already_completed", "empty_staging"):
    print("\nTEST:", mode)
    check_main(mode)
print("\nTEST: independent cleanup routes")
check_route_isolation()
print("\nALL SIX AUTOMATIC INGEST INTEGRATION TESTS PASSED")
print("No real Inbox, staging, library, or backups touched.")
