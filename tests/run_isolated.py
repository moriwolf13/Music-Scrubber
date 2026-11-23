"""Run allowlisted source and regression tests in a disposable offline workspace."""
from pathlib import Path
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


SOURCE_FILES = (
    "mixie.py", "audio.py", "lookup.py", "tag.py", "sort.py",
    "archive.py", "constants.py", "review.py", "mixie_review_ui.py", "mixie_library_merge.py", "mixie_terminal.py", "mixie_artwork.py", "mixie_batch_duplicates.py", "mixie_finalizer.py", "mixie_shared_originals.py",
)
LEGACY_TESTS = (
    "mixie_auto_ingest_integration_test.py", "mixie_inbox_purge_test.py",
)


def copy_source(path, destination):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f'Allowlisted source is not an independent regular file: {path.name}')
    shutil.copy2(path, destination)


def run_child(command, script, environment):
    with subprocess.Popen(command, cwd=script, env=environment,
                          stdin=subprocess.DEVNULL, start_new_session=True) as child:
        try:
            return child.wait(timeout=240)
        except subprocess.TimeoutExpired:
            # A killed test must not leave its ffmpeg running in the background.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            print('  ✗ Regression suite exceeded the 240-second safety limit; '
                  'see flushed active phase/test and watchdog stacks in the detailed log.', flush=True)
            return 124


def preserve_log(source, detailed):
    if not detailed.is_file() or detailed.is_symlink():
        print('  ✗ No regular detailed test log was produced; diagnostic setup failed.', flush=True)
        return False
    logs = source / '.mixie_logs'
    if logs.is_symlink():
        raise RuntimeError('Unsafe diagnostic directory')
    logs.mkdir(exist_ok=True)
    destination = logs / ('regression-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '.log')
    shutil.copyfile(detailed, destination)
    print('  Detailed diagnostic log:', destination, flush=True)
    return True


def run():
    started = time.monotonic()
    source = Path(__file__).resolve().parents[1]
    print('  Preparing disposable offline regression workspace (240s child deadline).', flush=True)
    # Ignore an inherited TMPDIR: test payloads must never be created in live data.
    with tempfile.TemporaryDirectory(prefix="mixie-v11-isolated-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        if root.parent != Path("/tmp").resolve():
            raise RuntimeError("Disposable test root was not independently verified.")
        script = root / "MIXIE-Lab" / "Scrubber"
        script.mkdir(parents=True)
        implementation = script / "src"
        implementation.mkdir()
        for name in SOURCE_FILES:
            copy_source(source / "src" / name, implementation / name)
        copy_source(source / "mixie.py", script / "mixie.py")
        for name in LEGACY_TESTS:
            copy_source(source / "tests" / "legacy" / name, script / name)
        tests = script / "tests"
        tests.mkdir()
        copy_source(source / "docs" / "stress_test.txt", tests / "stress_test.txt")
        for path in (source / "tests").glob("*.py"):
            copy_source(path, tests / path.name)
        # Version-pinned, public-catalog-only summary of the user's submitted
        # release evidence, kept out of production audio and review state.
        fixture = source / "tests" / "fixtures" / "review_evidence_20261123.json"
        if fixture.is_file() and not fixture.is_symlink():
            fixture_target = tests / "fixtures"
            fixture_target.mkdir()
            copy_source(fixture, fixture_target / fixture.name)
        home = root / "home"
        home.mkdir()
        detailed = root / 'regression-details.log'
        detailed.write_text('START child bootstrap (stack watchdog armed before guard/discovery)\n',
                            encoding='utf-8')
        print('  Live diagnostic log:', detailed, flush=True)
        print('  Starting child; flushed starts, 5s heartbeats, 15s blocked-phase stacks.', flush=True)
        environment = {
            'MIXIE_ISOLATED_DETAIL_LOG': str(detailed),
            "PATH": os.defpath,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "TMPDIR": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MIXIE_ISOLATED_TESTS": str(root),
            "ACOUSTID_CLIENT_KEY": "DISPOSABLE-OFFLINE-TEST-KEY",
        }
        # Arm the C-level watchdog before imports: discovery and guard setup can
        # block too. Its repeating dumps survive the parent's deadline kill.
        bootstrap = '''import faulthandler, os, signal, sys, traceback
trace = open(os.environ['MIXIE_ISOLATED_DETAIL_LOG'], 'a', encoding='utf-8', buffering=1)
faulthandler.enable(file=trace)
faulthandler.register(signal.SIGUSR1, file=trace, all_threads=True)
faulthandler.dump_traceback_later(15, repeat=True, file=trace)
guard_stats = None
try:
    trace.write('START importing / installing safety guard\\n')
    print('  Diagnostic child PID:', os.getpid(), '(SIGUSR1 dumps stacks)', flush=True)
    sys.path[:0] = ['tests', 'src', '.']
    import support
    guard_stats = support.install_guard(profile='--profile' in sys.argv[1:])
    trace.write('START importing presentation runner\\n')
    import pretty_runner
    exit_code = pretty_runner.main(watchdog=True)
except BaseException:
    traceback.print_exc(file=trace)
    raise
finally:
    if guard_stats is not None:
        trace.write('AUDIT PROFILE (explicit hook timing): ' + repr(guard_stats) + '\\n')
    faulthandler.cancel_dump_traceback_later()
    faulthandler.unregister(signal.SIGUSR1)
    trace.close()
sys.exit(exit_code)
'''
        exit_code = run_child(
            [sys.executable, '-I', '-B', '-c', bootstrap, *sys.argv[1:]], script, environment)
        # Copy verbose test details out before deleting disposable workspace.
        if not preserve_log(source, detailed):
            exit_code = exit_code or 1
        print(f'  Isolated runner wall time: {time.monotonic() - started:.3f}s', flush=True)
        return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
