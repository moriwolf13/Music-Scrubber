"""Regression checks for sequential review navigation (no audio, no network)."""
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
import mixie
import mixie_review_ui as ui

class ReviewNavigationV10ETests(unittest.TestCase):
    def test_skip_visits_each_album_once_in_display_order(self):
        issues = [{"album": name, "import_name": name, "tracks": [], "reason": "test"}
                  for name in ("EVERGREEN", "Chronologic", "Clash", "Panic")]
        visited = []
        def editor(_service, entry, _issues):
            visited.append(entry["album"])
            return "next"
        with patch.object(ui, "_album_editor", side_effect=editor), \
             patch.object(ui, "input", side_effect=["2", "0"]), \
             redirect_stdout(StringIO()):
            ui._list(mixie, issues, False)
        self.assertEqual(visited, ["Chronologic", "Clash", "Panic", "EVERGREEN"])

    def test_two_item_list_does_not_toggle_forever(self):
        issues = [{"album": name, "import_name": name, "tracks": [], "reason": "test"}
                  for name in ("EVERGREEN", "Chronologic")]
        visited = []
        def editor(_service, entry, _issues):
            visited.append(entry["album"])
            return "next"
        with patch.object(ui, "_album_editor", side_effect=editor), \
             patch.object(ui, "input", side_effect=["1", "0"]), \
             redirect_stdout(StringIO()):
            ui._list(mixie, issues, False)
        self.assertEqual(visited, ["EVERGREEN", "Chronologic"])

    def test_album_return_to_list_does_not_implicitly_open_another(self):
        issues = [{"album": name, "import_name": name, "tracks": [], "reason": "test"}
                  for name in ("EVERGREEN", "Chronologic")]
        visited = []
        def editor(_service, entry, _issues):
            visited.append(entry["album"])
            return "list"
        with patch.object(ui, "_album_editor", side_effect=editor), \
             patch.object(ui, "input", side_effect=["1", "0"]), \
             redirect_stdout(StringIO()):
            ui._list(mixie, issues, False)
        self.assertEqual(visited, ["EVERGREEN"])

if __name__ == "__main__":
    unittest.main()
