"""Guard physical workspace roots after moving application modules into src/."""
from pathlib import Path
import unittest
import audio
import sort
import tag

class ProjectLayoutTests(unittest.TestCase):
    def test_app_modules_keep_the_scrubber_runtime_root(self):
        script = Path.cwd().resolve()
        self.assertEqual(script, Path(tag.script_folder).resolve())
        self.assertEqual(script, Path(sort.script_folder).resolve())
        self.assertEqual(script, Path(audio.script_folder).resolve())

    def test_music_library_stays_sibling_of_scrubber(self):
        script = Path.cwd().resolve()
        self.assertEqual(script.parent / 'Music Library', Path(sort.music_library_folder))
        self.assertEqual(script.parent / 'Music Library', Path(tag.library_folder))

    def test_staging_and_backups_keep_original_paths(self):
        script = Path.cwd().resolve()
        self.assertEqual(script / '.staging', Path(sort.staging_folder))
        self.assertEqual(script / '.staging', Path(tag.staging_folder))
        self.assertEqual(script / '.staging', Path(audio.staging_folder))
