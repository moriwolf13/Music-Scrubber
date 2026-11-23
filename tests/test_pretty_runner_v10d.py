"""v10d terminal display safety and result semantics, independent of hardware."""
import io
import os
import unittest
from unittest.mock import patch

import pretty_runner


class RegressionDashboardTests(unittest.TestCase):
    def _runner(self, interactive):
        stream = io.StringIO()
        display = io.StringIO()
        result = pretty_runner.CompactResult(stream, True, 0, 3,
                                             console=display, interactive=interactive)
        return result, display

    def test_bar_fills_correctly_without_overshoot_or_early_completion(self):
        self.assertEqual(pretty_runner.progress_bar(0, 4, 8), '[░░░░░░░░]')
        self.assertEqual(pretty_runner.progress_bar(1, 4, 8), '[██░░░░░░]')
        self.assertEqual(pretty_runner.progress_bar(2, 4, 8), '[████░░░░]')
        self.assertEqual(pretty_runner.progress_bar(4, 4, 8), '[████████]')
        self.assertEqual(pretty_runner.progress_bar(9, 4, 8), '[████████]')

    def test_tty_updates_one_line_per_finished_test_without_printing_bars(self):
        result, display = self._runner(True)
        result.startTestRun()
        for _ in range(3):
            case = unittest.FunctionTestCase(lambda: None)
            result.startTest(case)
            result.addSuccess(case)
            result.stopTest(case)
        # Only carriage returns before the one completed-group summary.
        self.assertEqual(display.getvalue().count('\n'), 0)
        self.assertEqual(display.getvalue().count('\x1b[2K'), 4)
        self.assertIn('100%  3/3', display.getvalue())
        result.stopTestRun()
        self.assertIn('Other Regressions', display.getvalue())

    def test_non_tty_has_no_escape_codes_or_repeated_progress_lines(self):
        result, display = self._runner(False)
        case = unittest.FunctionTestCase(lambda: None)
        result.startTestRun()
        result.startTest(case)
        result.addSuccess(case)
        result.stopTest(case)
        result.stopTestRun()
        output = display.getvalue()
        self.assertNotIn('\x1b', output)
        self.assertNotIn('░', output)
        self.assertIn('SUBSYSTEM RESULTS', output)

    def test_failed_case_is_visible_in_summary_and_marks_result_unsuccessful(self):
        result, display = self._runner(False)
        case = unittest.FunctionTestCase(lambda: None)
        result.startTest(case)
        try:
            raise AssertionError('synthetic failure')
        except AssertionError:
            import sys
            result.addFailure(case, sys.exc_info())
        result.stopTest(case)
        result.stopTestRun()
        self.assertFalse(result.wasSuccessful())
        self.assertIn('FAIL:', display.getvalue())
        self.assertIn('✗ FAIL', display.getvalue())

    def test_known_subsystems_are_classified_and_unrecognized_tests_not_dropped(self):
        class _Dummy:
            def __init__(self, name):
                self.name = name
            def id(self):
                return self.name
        self.assertEqual(pretty_runner.category(_Dummy('test_ingest_pipeline.Foo.test_bar')),
                         'END-TO-END ACCEPTANCE')
        self.assertEqual(pretty_runner.category(_Dummy('test_something_new.Foo.test_bar')),
                         'OTHER REGRESSIONS')


if __name__ == '__main__':
    unittest.main()

class GenreHintResilienceTests(unittest.TestCase):
    def test_malformed_unrelated_staged_flac_does_not_crash_genre_hint(self):
        """Catalog genre hints must not become an audio-validation bypass/blocker."""
        import tempfile
        from pathlib import Path
        from mutagen.flac import FLACNoHeaderError
        import mixie
        with tempfile.TemporaryDirectory() as temp:
            catalog={}
            for i in range(3):
                catalog[str(i)]={"tracks":[{
                    "path":Path(temp)/f"known-{i}.flac",
                    "album_artist":("Caravan Palace",),
                    "artist":("Caravan Palace",),
                    "album":(f"Known Album {i}",),
                    "genre":("Electro Swing",),
                }]}
            target={"tracks":[{"path":Path(temp)/"candidate.flac"}]}
            with patch.object(mixie.tag,"group_library_albums",return_value=catalog), \
                 patch.object(mixie.tag,"group_staged_albums",side_effect=FLACNoHeaderError("bad other FLAC")):
                self.assertEqual(mixie._catalog_genre_hint(target,"Caravan Palace"),"Electro Swing")
