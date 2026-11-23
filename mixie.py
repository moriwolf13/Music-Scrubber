"""MIXIE launcher. Runtime files live beside this file; implementation in src/."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    implementation = root / "src" / "mixie.py"
    if implementation.is_symlink() or not implementation.is_file():
        raise RuntimeError("MIXIE implementation missing or unsafe")
    sys.path.insert(0, str(root / "src"))
    runpy.run_path(str(implementation), run_name="__main__")
