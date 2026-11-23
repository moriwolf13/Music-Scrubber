"""Run the full offline suite with one in-place TTY progress bar and full logs.

This changes presentation only: unittest owns pass/fail and executes every case.
No test is filtered, suppressed, replaced, or marked successful by this module.
"""
import argparse
import contextlib
import faulthandler
import os
import shutil
import sys
import threading
from time import monotonic as _clock
import unittest
from collections import defaultdict
from pathlib import Path

GROUPS = (
    ('AUDIO & CATALOG IDENTIFICATION', ('identification', 'candidate_workload', 'musicbrainz_workload', 'musicbrainz_transport', 'musicbrainz_album_scope', 'lookup_refresh')),
    ('METADATA & AUDIO TAGS', ('automation_v', 'context_review', 'metadata', 'real_audio')),
    ('ARCHIVE & DATA INTEGRITY', ('archive_safety', 'staging_safety', 'completion', 'stress_blockers', 'ingest_safety')),
    ('REVIEW & RECOVERY', ('review', 'queue_workflow', 'stabilization_patch')),
    ('END-TO-END ACCEPTANCE', ('acceptance', 'ingest_pipeline')),
)


def category(test):
    name = test.id()
    for label, stems in GROUPS:
        if any('test_' + stem in name for stem in stems):
            return label
    return 'OTHER REGRESSIONS'


def progress_bar(completed, total, width=26):
    """Pure formatting function used by both terminal output and unit tests."""
    percent = min(1.0, max(0.0, completed / total)) if total else 1.0
    filled = min(width, max(0, int(width * percent)))
    return '[' + '█' * filled + '░' * (width - filled) + ']'


def live_bar(completed, total, width=26, *, tty=False):
    """Frame and fill are single-cell glyphs. Gold fill matches MIXIE Rockbox.

    The fallback is fully printable; avoid ANSI when output is redirected or
    NO_COLOR is set. Never color the detailed regression log.
    """
    raw = progress_bar(completed, total, width)[1:-1]
    filled = raw.count('█')
    if not tty or 'NO_COLOR' in os.environ or os.environ.get('TERM') == 'dumb':
        return '┃' + raw + '┃'
    gold, violet, slate, reset = ('\x1b[38;2;220;177;80m',
                                 '\x1b[38;2;177;127;244m',
                                 '\x1b[38;2;76;87;111m', '\x1b[0m')
    return (violet + '┃' + gold + '█' * filled + slate + '░' * (width-filled)
            + violet + '┃' + reset)


def tint(text, shade, *, stream=None):
    """Use the MIXIE Rockbox palette on interactive terminals only."""
    stream = sys.__stdout__ if stream is None else stream
    if (not getattr(stream, 'isatty', lambda: False)()
            or 'NO_COLOR' in os.environ or os.environ.get('TERM') == 'dumb'):
        return str(text)
    tones = {'gold': '220;177;80', 'violet': '177;127;244',
             'cyan': '68;232;242', 'green': '78;219;161',
             'warning': '255;198;89', 'error': '255;102;125'}
    return f"\x1b[38;2;{tones[shade]}m{text}\x1b[0m"


class RunDiagnostics:
    """Keep progress visible independently of test output (including discovery)."""
    def __init__(self, log, console, *, watchdog=False, interval=5.0, stack_after=15.0):
        self.log = log
        self.console = console
        self.watchdog = watchdog
        self.interval = interval
        self.stack_after = stack_after
        self.started = _clock()
        self.phase_started = self.started
        self.active = 'initializing'
        self.draw = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    def phase(self, name):
        with self._lock:
            self.active = name
            self.phase_started = _clock()
            self.log.write(f'\nSTART {name} | elapsed={self.phase_started - self.started:.3f}s\n')
            self.log.flush()
            if self.watchdog:
                # Reset only at phase/test boundaries, never at a heartbeat: a
                # blocked import, fixture, cleanup or test must still dump stacks.
                faulthandler.dump_traceback_later(self.stack_after, repeat=True, file=self.log)

    def status(self):
        now = _clock()
        return f'elapsed={now - self.started:.1f}s active={now - self.phase_started:.1f}s {self.active}'

    def report(self):
        with self._lock:
            message = self.status()
            self.log.write(f'\nHEARTBEAT {message}\n')
            self.log.flush()
            if self.draw is not None:
                self.draw()
            else:
                self.console.write(f'  {message}\n')
                self.console.flush()

    def _watch(self):
        while not self._stop.wait(self.interval):
            self.report()

    def __enter__(self):
        if self.watchdog:
            self._thread = threading.Thread(target=self._watch, name='runner-heartbeat', daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self.watchdog:
            faulthandler.cancel_dump_traceback_later()


class CompactResult(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity, total, console=None, interactive=None,
                 diagnostics=None):
        super().__init__(stream, descriptions, verbosity)
        self.total = total
        self.completed = 0
        self.diagnostics = diagnostics
        self.timings = []
        self._test_started = None
        output = console if console is not None else sys.__stdout__
        if output is None:
            raise RuntimeError('No regression console stream available.')
        self.console = output
        self.interactive = (
            bool(getattr(self.console, 'isatty', lambda: False)())
            and os.environ.get('TERM', '') != 'dumb'
            if interactive is None else bool(interactive)
        )
        self.passed = 0
        self.skipped_count = 0
        self._active_category = 'INITIALIZING'
        self._group_results = defaultdict(lambda: {'done': 0, 'passed': 0, 'failed': 0, 'skipped': 0})
        self._failed_names = []

    def _draw(self):
        if not self.interactive:
            return
        columns = shutil.get_terminal_size(fallback=(100, 25)).columns
        width = (max(6, min(18, columns // 10)) if self.diagnostics is not None
                 else max(12, min(28, columns - 75)))
        pct = min(100, 100 * self.completed // self.total) if self.total else 100
        failed = len(self.failures) + len(self.errors) + len(self.unexpectedSuccesses)
        detail = self._active_category.title().replace(' & ', ' + ')
        colored = live_bar(self.completed, self.total, width,
                           tty=bool(getattr(self.console, 'isatty', lambda: False)()))
        line = (f'  {colored}  '
                f'{pct:3d}%  {self.completed}/{self.total}  '
                f'✓ {self.passed}  ✗ {failed}  ↷ {self.skipped_count}  {detail}')
        if self.diagnostics is not None:
            now = _clock()
            line = (f' {colored} {pct:3d}% {self.completed}/{self.total}'
                    f' ✓{self.passed} ✗{failed} ↷{self.skipped_count}'
                    f' {now - self.diagnostics.started:.0f}s/{now - self.diagnostics.phase_started:.0f}s ')
            # Keep a single terminal row; full identifiers are always in the log
            # and non-TTY heartbeat. Prefer the distinctive method-name suffix.
            visible = len(line) - len(colored) + width + 2
            available = max(0, columns - visible - 1)
            name = self.diagnostics.active
            if len(name) > available:
                name = ('…' + name[-(available - 1):]) if available > 1 else name[:available]
            line += name
        # Carriage return and erase-line update ONLY the current line; no terminal scroll.
        self.console.write('\r\x1b[2K' + line)
        self.console.flush()

    def _heartbeat(self):
        if self.diagnostics is None:
            return
        if self.interactive:
            self._draw()
        else:
            self.console.write(f'  {self.completed}/{self.total} complete | {self.diagnostics.status()}\n')
            self.console.flush()

    def startTestRun(self):
        super().startTestRun()
        if self.diagnostics is not None:
            self.diagnostics.draw = self._heartbeat
        self._draw()

    def startTest(self, test):
        self._active_category = category(test)
        self._test_started = _clock()
        if self.diagnostics is not None:
            self.diagnostics.phase(test.id())
        super().startTest(test)
        self.stream.flush()
        if self.diagnostics is not None:
            self._draw()

    def addSuccess(self, test):
        self.passed += 1
        self._group_results[category(test)]['passed'] += 1
        super().addSuccess(test)

    def addSkip(self, test, reason):
        self.skipped_count += 1
        self._group_results[category(test)]['skipped'] += 1
        super().addSkip(test, reason)

    def addFailure(self, test, err):
        self._group_results[category(test)]['failed'] += 1
        self._failed_names.append(('FAIL', test.id()))
        super().addFailure(test, err)

    def addError(self, test, err):
        self._group_results[category(test)]['failed'] += 1
        self._failed_names.append(('ERROR', test.id()))
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._group_results[category(test)]['failed'] += 1
            kind = 'FAIL' if err[0] is not None and issubclass(err[0], test.failureException) else 'ERROR'
            self._failed_names.append((kind, subtest.id()))
        super().addSubTest(test, subtest, err)

    def addUnexpectedSuccess(self, test):
        self._group_results[category(test)]['failed'] += 1
        self._failed_names.append(('UNEXPECTED SUCCESS', test.id()))
        super().addUnexpectedSuccess(test)

    def stopTest(self, test):
        elapsed = _clock() - self._test_started if self._test_started is not None else 0.0
        self.timings.append((elapsed, test.id()))
        self.completed += 1
        self._group_results[category(test)]['done'] += 1
        super().stopTest(test)
        if self.diagnostics is not None:
            self.stream.write(f'END {test.id()} | duration={elapsed:.3f}s\n')
        self.stream.flush()
        self._draw()

    def stopTestRun(self):
        if self.interactive:
            self.console.write('\n')
            self.console.flush()
        super().stopTestRun()
        if self.diagnostics is not None:
            self.diagnostics.draw = None
            self.diagnostics.phase('reporting results')
        self.show_group_results()
        slow = [(elapsed, name) for elapsed, name in self.timings if elapsed >= 1.0]
        if slow:
            self.console.write('\n  SLOWEST TESTS >= 1s (including setup/cleanup)\n')
            for elapsed, name in sorted(slow, reverse=True)[:10]:
                self.console.write(f'    {elapsed:7.3f}s  {name}\n')
            self.console.flush()

    def show_group_results(self):
        labels = [name for name, _ in GROUPS] + ['OTHER REGRESSIONS']
        self.console.write('\n  ' + tint('SUBSYSTEM RESULTS', 'violet', stream=self.console) + '\n')
        for label in labels:
            counts = self._group_results.get(label)
            if not counts or not counts['done']:
                continue
            mark = '✗ FAIL' if counts['failed'] else '↷ SKIP' if counts['skipped'] == counts['done'] else '✓ PASS'
            text = (f'    {mark:<8} {label.title():<34} '
                    f'{counts["done"]} tests')
            if counts['skipped']:
                text += f' ({counts["skipped"]} skipped)'
            shade = 'error' if counts['failed'] else 'warning' if counts['skipped'] == counts['done'] else 'green'
            self.console.write(tint(text[:12], shade, stream=self.console) +
                               tint(text[12:], 'cyan', stream=self.console) + '\n')
        for kind, name in self._failed_names:
            self.console.write(tint(f'    ✗ {kind}: {name}', 'error', stream=self.console) + '\n')
        self.console.flush()


def main(*, watchdog=False):
    parser = argparse.ArgumentParser(description='Run isolated unittest regressions with compact output.')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Detailed diagnostics are always verbose; console output stays compact.')
    parser.add_argument('--profile', action='store_true',
                        help='Record CPU profile and manifest/hash callers in the detailed log.')
    arguments = sys.argv[1:]
    # unittest's normal test discovery order is deliberately preserved.
    # Reordering tests may conceal order-dependent regression failures.
    if arguments[:1] == ['discover']:
        parser.add_argument('command', choices=('discover',))
        parser.add_argument('-s', '--start-directory', default='tests')
        parser.add_argument('-p', '--pattern', default='test_*.py')
        parser.add_argument('-t', '--top-level-directory')
        options = parser.parse_args(arguments)
    else:
        parser.add_argument('tests', nargs='*', metavar='test')
        options = parser.parse_args(arguments)
    raw_path = os.environ.get('MIXIE_ISOLATED_DETAIL_LOG', '')
    detail = Path(raw_path) if raw_path else None
    if detail is None or not detail.is_absolute() or not detail.parent.is_dir():
        raise RuntimeError('Missing isolated test log path; refusing to run without diagnostics.')
    console = sys.__stdout__
    with detail.open('a', encoding='utf-8', buffering=1) as log, RunDiagnostics(
            log, console, watchdog=watchdog) as diagnostics:
        profile = None
        if options.profile:
            import cProfile
            profile = cProfile.Profile()
            profile.enable()
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                diagnostics.phase('discovery / loading tests')
                diagnostics.report()
                loader = unittest.TestLoader()
                if arguments[:1] == ['discover']:
                    suite = loader.discover(options.start_directory, pattern=options.pattern,
                                            top_level_dir=options.top_level_directory)
                else:
                    suite = (loader.loadTestsFromNames(options.tests) if options.tests
                             else loader.discover('tests', pattern='test_*.py'))
                total = suite.countTestCases()
                if not total:
                    message = 'No tests matched; refusing to report an empty run as successful.'
                    log.write(message + '\n')
                    print(f'  ✗ {message}', file=console, flush=True)
                    return 1
                print(tint('\n  ╔════════════════════════════════════════════════╗'
                           '\n  ║  M.I.X.I.E.  //  REGRESSION DIAGNOSTICS        ║'
                           '\n  ╚════════════════════════════════════════════════╝',
                           'violet', stream=console), file=console, flush=True)
                print(tint(f'  {total} tests queued  •  isolated files  •  no network',
                           'cyan', stream=console), file=console, flush=True)
                runner = unittest.TextTestRunner(
                    stream=log, verbosity=2,
                    resultclass=lambda stream, descriptions, verbosity: CompactResult(
                        stream, descriptions, verbosity, total, console=console,
                        diagnostics=diagnostics),
                )
                result = runner.run(suite)
        except BaseException:
            import traceback
            traceback.print_exc(file=log)
            log.flush()
            raise
        finally:
            if profile is not None:
                profile.disable()
                import pstats
                stats = pstats.Stats(profile, stream=log).strip_dirs()
                log.write('\nCPU PROFILE (profiling adds overhead)\n')
                stats.sort_stats('cumulative').print_stats(35)
                stats.sort_stats('tottime').print_stats(25)
                stats.print_callers('file_manifest|file_checksum|sleep')
                log.flush()
        errors = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
        print(f'\n  RESULTS  ✓ {result.passed} passed  ·  ↷ {result.skipped_count} skipped'
              f'  ·  ✗ {errors} failed  ·  elapsed={_clock() - diagnostics.started:.3f}s',
              file=console, flush=True)
        return 0 if result.wasSuccessful() and result.testsRun == total else 1


if __name__ == '__main__':
    raise SystemExit(main())
