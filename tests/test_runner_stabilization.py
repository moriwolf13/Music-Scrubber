"""Runner/guard regressions using disposable fixtures only, never live paths."""
import contextlib
import faulthandler
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, call, patch

import pretty_runner
import run_isolated
from support import FilesystemGuard, install_guard


class RunnerFixture(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('MIXIE_ISOLATED_TESTS'):
            raise RuntimeError('Run through tests/run_isolated.py only.')
        temporary = tempfile.TemporaryDirectory(prefix='runner-guard-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def descriptor(self, path):
        descriptor = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        return descriptor


class GuardTests(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.work = self.base / 'work'
        self.live = self.base / 'protected-fixture'
        self.dependencies = self.live / 'site-packages'
        for folder in (self.work, self.dependencies):
            folder.mkdir(parents=True)
        self.secret = self.live / 'state.json'
        self.secret.write_text('synthetic protected state', encoding='utf-8')
        self.package = self.dependencies / 'fixture.py'
        self.package.write_text('VALUE = 1\n', encoding='utf-8')
        self.guard = FilesystemGuard(self.work, (self.live,), (self.dependencies,))

    def test_component_boundaries_and_bytes_paths(self):
        self.guard.checked_path(os.fsencode(self.work / 'new'), True)
        self.guard.checked_path(self.work, True)
        self.guard.checked_path(self.base / 'protected-fixture-other' / 'file')
        with self.assertRaises(PermissionError):
            self.guard.checked_path(self.base / 'work-other' / 'file', True)
        with self.assertRaises(PermissionError):
            self.guard.checked_path(os.fsencode(self.secret))

    def test_direct_protected_spelling_is_denied_before_resolution(self):
        with patch('support.os.path.realpath', side_effect=AssertionError('must not probe protected paths')):
            with self.assertRaises(PermissionError):
                self.guard.checked_path(self.secret)

    def test_read_only_site_dependency_exception_is_preserved(self):
        self.guard.checked_path(self.package)
        alias = self.work / 'dependency'
        alias.symlink_to(self.dependencies, target_is_directory=True)
        self.guard.checked_path(alias / self.package.name)
        for path in (self.package, alias / self.package.name):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                self.guard.checked_path(path, True)
        with self.assertRaises(PermissionError):
            self.guard.checked_path(self.live / 'site-packages-other' / 'fixture.py')

    def test_replaced_leaf_is_resolved_again_on_read_and_write(self):
        path = self.work / 'file'
        path.write_bytes(b'fixture')
        self.guard.checked_path(path)
        self.guard.checked_path(path, True)
        path.unlink()
        path.symlink_to(self.secret)
        for writing in (False, True):
            with self.subTest(writing=writing), self.assertRaises(PermissionError):
                self.guard.checked_path(path, writing)

    def test_renamed_parent_replaced_by_symlink_is_not_cached(self):
        folder = self.work / 'folder'
        folder.mkdir()
        path = folder / self.secret.name
        self.guard.checked_path(path, True)
        folder.rename(self.work / 'retained')
        folder.symlink_to(self.live, target_is_directory=True)
        with self.assertRaises(PermissionError):
            self.guard.checked_path(path, True)

    def test_dependency_symlink_retargeting_does_not_keep_read_allowance(self):
        alias = self.work / 'alias'
        alias.symlink_to(self.dependencies, target_is_directory=True)
        self.guard.checked_path(alias / 'fixture.py')
        alias.unlink()
        alias.symlink_to(self.live, target_is_directory=True)
        with self.assertRaises(PermissionError):
            self.guard.checked_path(alias / 'fixture.py')

    def test_replacing_guard_root_cannot_reuse_its_old_resolution(self):
        self.guard.checked_path(self.work / 'file', True)
        self.work.rename(self.base / 'held-work')
        self.work.symlink_to(self.live, target_is_directory=True)
        with self.assertRaises(PermissionError):
            self.guard.checked_path(self.work / 'file', True)

    def test_symlink_dotdot_uses_kernel_order_not_lexical_normalization(self):
        child = self.live / 'child'
        child.mkdir()
        alias = self.work / 'alias'
        alias.symlink_to(child, target_is_directory=True)
        with self.assertRaises(PermissionError):
            self.guard.checked_path(alias / '..' / self.secret.name)

    def test_resolution_reuse_is_limited_to_one_audit_event(self):
        path = str(self.work / 'file')
        with patch('support.os.path.realpath', wraps=os.path.realpath) as resolve:
            self.guard.audit('os.rename', (path, path, -1, -1))
            self.assertEqual(1, resolve.call_count)
            self.guard.audit('open', (path, 'r', os.O_RDONLY))
            self.assertEqual(2, resolve.call_count)

    def test_all_write_modes_and_flags_remain_guarded(self):
        for mode, flags in (('w', 0), ('a', 0), ('x', 0), ('r+', 0),
                            (None, os.O_WRONLY), (None, os.O_RDWR),
                            (None, os.O_CREAT), (None, os.O_TRUNC), (None, os.O_APPEND)):
            with self.subTest(mode=mode, flags=flags), self.assertRaises(PermissionError):
                self.guard.audit('open', (str(self.package), mode, flags))
        self.guard.audit('open', (str(self.package), 'r', os.O_RDONLY))

    def test_mutations_use_actual_dir_fd_not_cwd(self):
        descriptor = self.descriptor(self.live)
        for event, args in (
            ('os.remove', ('file', descriptor)),
            ('os.rmdir', ('folder', descriptor)),
            ('os.mkdir', ('folder', 0o700, descriptor)),
            ('os.chmod', ('file', 0o600, descriptor)),
            ('os.utime', ('file', None, None, descriptor)),
            ('os.chown', ('file', -1, -1, descriptor)),
            ('os.rename', ('file', str(self.work / 'new'), descriptor, -1)),
            ('os.rename', (str(self.work / 'file'), 'new', -1, descriptor)),
            ('os.link', ('file', str(self.work / 'new'), descriptor, -1)),
            ('os.link', (str(self.work / 'file'), 'new', -1, descriptor)),
            ('os.symlink', ('target', 'link', descriptor)),
        ):
            with self.subTest(event=event, args=args), self.assertRaises(PermissionError):
                self.guard.audit(event, args)
        # Absolute paths ignore dir_fd, just as the real syscall does.
        self.guard.audit('os.remove', (str(self.work / 'file'), descriptor))

    def test_dir_fd_target_is_reverified_after_directory_is_moved(self):
        folder = self.work / 'folder'
        folder.mkdir()
        descriptor = self.descriptor(folder)
        self.guard.checked_path('file', True, descriptor)
        folder.rename(self.live / 'moved')
        with self.assertRaises(PermissionError):
            self.guard.checked_path('file', True, descriptor)

    def test_relative_symlink_target_is_based_at_destination(self):
        destination = self.work / 'nested' / 'alias'
        with self.assertRaises(PermissionError):
            self.guard.audit('os.symlink', ('../../protected-fixture/state.json', str(destination), -1))
        self.guard.audit('os.symlink', ('../safe', str(destination), -1))

    def test_descriptor_reads_listings_and_truncation_are_checked(self):
        file_descriptor = self.descriptor(self.secret)
        directory_descriptor = self.descriptor(self.live)
        for event, args in (
            ('open', (file_descriptor, 'r', os.O_RDONLY)),
            ('open', (file_descriptor, 'w', os.O_WRONLY)),
            ('os.listdir', (directory_descriptor,)),
            ('os.scandir', (directory_descriptor,)),
            ('os.chdir', (directory_descriptor,)),
            ('os.truncate', (file_descriptor, 0)),
        ):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                self.guard.audit(event, args)
        with self.assertRaises(PermissionError):
            self.guard.checked_path('file', True, file_descriptor)

    def test_installed_os_open_checks_dir_fd_without_creating_outside_files(self):
        descriptor = self.descriptor('/tmp')
        # No O_CREAT or O_TRUNC: even a broken guard must not create/change data.
        name = Path(os.environ['MIXIE_ISOLATED_TESTS']).name + '-no-create-probe'
        with self.assertRaises(PermissionError):
            unexpected = os.open(name, os.O_WRONLY, dir_fd=descriptor)
            os.close(unexpected)

    def test_guard_cannot_be_installed_without_verified_workspace(self):
        with patch.dict(os.environ, {'MIXIE_ISOLATED_TESTS': ''}):
            with self.assertRaises(RuntimeError):
                install_guard()
        with patch.dict(os.environ, {'MIXIE_ISOLATED_TESTS': str(self.work)}):
            with self.assertRaises(RuntimeError):
                install_guard()

    def test_network_and_shell_events_remain_forbidden(self):
        for event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                      'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg',
                      'urllib.Request', 'os.system', 'os.exec'):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                self.guard.audit(event, ())

    def test_ffmpeg_and_its_posix_spawn_path_remain_allowed_only_for_fixtures(self):
        command = ['/usr/bin/ffmpeg', '-nostdin', '-f', 'lavfi', '-i',
                   'anullsrc=r=44100:cl=mono', '-t', '0.15', '-n', str(self.work / 'silence.flac')]
        environment = {'PATH': os.defpath}
        self.guard.audit('subprocess.Popen', (command[0], command, None, environment))
        self.guard.audit('subprocess.Popen', (command[0], command, None, None))
        relative = ['ffmpeg', *command[1:]]
        self.guard.audit('subprocess.Popen', ('ffmpeg', relative, None, None))
        self.guard.audit('subprocess.Popen', ('ffmpeg', relative, None, {b'PATH': os.fsencode(os.defpath)}))
        self.guard.audit('os.posix_spawn', (command[0], command, environment))
        variants = [
            ('/bin/sh', command, environment),
            (str(self.work / 'ffmpeg'), command, environment),
            ('ffmpeg', ['ffmpeg', *command[1:]], {'PATH': str(self.work)}),
            ('ffmpeg', ['ffmpeg', *command[1:]], {b'PATH': os.fsencode(self.work)}),
            (command[0], [*command[:-1], str(self.secret)], environment),
            (command[0], [*command[:5], 'https://offline.invalid/audio', *command[6:]], environment),
            (command[0], [*command[:5], str(self.secret), *command[6:]], environment),
            (command[0], [arg for arg in command if arg != '-nostdin'], environment),
        ]
        for executable, arguments, env in variants:
            with self.subTest(arguments=arguments), self.assertRaises(PermissionError):
                self.guard.audit('subprocess.Popen', (executable, arguments, None, env))


class DiagnosticTests(RunnerFixture):
    def test_application_clock_mocks_cannot_make_diagnostics_negative(self):
        log, console = io.StringIO(), io.StringIO()
        diagnostics = pretty_runner.RunDiagnostics(log, console)
        with patch('time.monotonic', return_value=-1000000):
            diagnostics.phase('application clock patched')
            diagnostics.report()
        self.assertNotIn('elapsed=-', console.getvalue())
        self.assertNotIn('active=-', console.getvalue())
        self.assertIn('application clock patched', console.getvalue())

    def test_start_is_flushed_before_test_body_and_end_has_duration(self):
        class FlushedLog(io.StringIO):
            flushed = ''

            def flush(self):
                self.flushed = self.getvalue()
                super().flush()

        log, console = FlushedLog(), io.StringIO()
        diagnostics = pretty_runner.RunDiagnostics(log, console)

        def body():
            self.assertIn('START ' + case.id(), log.flushed)
            self.assertIn('elapsed=', log.flushed)

        case = unittest.FunctionTestCase(body)
        runner = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=lambda *args:
            pretty_runner.CompactResult(*args, total=1, console=console, diagnostics=diagnostics))
        result = runner.run(case)
        self.assertTrue(result.wasSuccessful(), log.getvalue())
        self.assertIn('END ' + case.id() + ' | duration=', log.getvalue())

    def test_started_test_is_not_presented_as_completed_and_tty_fits_one_row(self):
        log, console = io.StringIO(), io.StringIO()
        diagnostics = pretty_runner.RunDiagnostics(log, console)
        result = pretty_runner.CompactResult(log, True, 0, 1, console=console,
                                            interactive=True, diagnostics=diagnostics)
        case = unittest.FunctionTestCase(lambda: None)
        with patch('pretty_runner.shutil.get_terminal_size', return_value=os.terminal_size((80, 25))):
            result.startTestRun()
            result.startTest(case)
            self.assertEqual(1, result.testsRun)  # Native unittest count is untouched.
            self.assertEqual(0, result.completed)
            self.assertNotIn('100%', console.getvalue())
            result.addSuccess(case)
            result.stopTest(case)
        self.assertIn('100% 1/1', console.getvalue())
        for row in console.getvalue().split('\r\x1b[2K'):
            self.assertLessEqual(len(row), 79)
        result.total, result.completed = 818, 817
        console.truncate(0)
        console.seek(0)
        result._draw()
        self.assertIn('99%', console.getvalue())
        self.assertNotIn('100%', console.getvalue())

    def test_non_tty_heartbeat_identifies_active_test_and_elapsed(self):
        log, console = io.StringIO(), io.StringIO()
        diagnostics = pretty_runner.RunDiagnostics(log, console)
        result = pretty_runner.CompactResult(log, True, 0, 1, console=console,
                                            interactive=False, diagnostics=diagnostics)
        case = unittest.FunctionTestCase(lambda: None)
        result.startTestRun()
        result.startTest(case)
        diagnostics.report()
        self.assertIn('0/1 complete', console.getvalue())
        self.assertIn(case.id(), console.getvalue())
        self.assertIn('elapsed=', console.getvalue())
        self.assertIn('active=', console.getvalue())
        self.assertNotIn('\x1b', console.getvalue())
        self.assertIn('HEARTBEAT', log.getvalue())

    def test_watchdog_is_reset_at_phase_boundaries_not_heartbeats(self):
        log, console = io.StringIO(), io.StringIO()
        with patch('pretty_runner.faulthandler.dump_traceback_later') as arm, \
                patch('pretty_runner.faulthandler.cancel_dump_traceback_later') as cancel:
            with pretty_runner.RunDiagnostics(log, console, watchdog=True, interval=60) as diagnostics:
                diagnostics.phase('discovery')
                diagnostics.report()
                diagnostics.phase('module.Case.test_blocked')
                diagnostics.report()
            self.assertEqual([call(15.0, repeat=True, file=log)] * 2, arm.call_args_list)
            cancel.assert_called_once_with()

    def test_blocked_discovery_and_test_phases_produce_real_stack_traces(self):
        for phase in ('discovery / loading tests', 'synthetic.Case.test_blocked'):
            with self.subTest(phase=phase):
                path = self.base / ('discovery.log' if phase.startswith('discovery') else 'blocked.log')
                console = io.StringIO()
                with path.open('a', encoding='utf-8', buffering=1) as log:
                    with pretty_runner.RunDiagnostics(log, console, watchdog=True,
                                                     interval=0.01, stack_after=0.02) as diagnostics:
                        diagnostics.phase(phase)
                        # Stay blocked instead of polling the file through the
                        # audit hook while the C watchdog is walking our stack.
                        time.sleep(0.08)
                output = path.read_text()
                self.assertIn('START ' + phase, output)
                self.assertIn('Timeout', output)
                self.assertIn('test_blocked_discovery_and_test_phases_produce_real_stack_traces', output)
                self.assertIn(phase, console.getvalue())

    def test_discovery_failure_preserves_bootstrap_and_flushed_loading_diagnostics(self):
        detail = self.base / 'discovery.log'
        detail.write_text('bootstrap already armed\n', encoding='utf-8')
        console = io.StringIO()

        def fail_discovery(*args, **kwargs):
            self.assertIn('START discovery / loading tests', detail.read_text())
            raise RuntimeError('synthetic discovery obstruction')

        with patch.object(sys, 'argv', ['pretty_runner']), patch.object(sys, '__stdout__', console), \
                patch.dict(os.environ, {'MIXIE_ISOLATED_DETAIL_LOG': str(detail)}), \
                patch.object(unittest.TestLoader, 'discover', side_effect=fail_discovery):
            with self.assertRaisesRegex(RuntimeError, 'synthetic discovery obstruction'):
                pretty_runner.main()
        output = detail.read_text()
        self.assertIn('bootstrap already armed', output)
        self.assertIn('Traceback', output)
        self.assertIn('synthetic discovery obstruction', output)

    def test_slow_test_summary_includes_identifier_and_cleanup_duration(self):
        log, console = io.StringIO(), io.StringIO()
        result = pretty_runner.CompactResult(log, True, 0, 1, console=console, interactive=False)
        case = unittest.FunctionTestCase(lambda: None)
        result.startTest(case)
        result._test_started = time.monotonic() - 2.0
        result.addSuccess(case)
        result.stopTest(case)
        result.stopTestRun()
        self.assertIn('SLOWEST TESTS', console.getvalue())
        self.assertIn(case.id(), console.getvalue())
        self.assertGreaterEqual(result.timings[0][0], 2.0)

    def test_unittest_order_outcomes_subtests_and_expected_failures_are_native(self):
        def execute(compact):
            events = []

            class Outcomes(unittest.TestCase):
                def test_a_pass(self):
                    events.append('pass')

                def test_b_subtest_failure(self):
                    events.append('subtest')
                    with self.subTest(value=1):
                        self.fail('synthetic subtest')

                @unittest.expectedFailure
                def test_c_expected(self):
                    events.append('expected')
                    self.fail('expected')

                @unittest.skip('retained skip')
                def test_d_skip(self):
                    events.append('must not run')

                @unittest.expectedFailure
                def test_e_unexpected(self):
                    events.append('unexpected')

                def test_f_error(self):
                    events.append('error')
                    raise RuntimeError('synthetic error')

            options = {}
            display = io.StringIO()
            if compact:
                options['resultclass'] = lambda *args: pretty_runner.CompactResult(
                    *args, total=6, console=display, interactive=False)
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, **options).run(
                unittest.TestLoader().loadTestsFromTestCase(Outcomes))
            if compact:
                self.assertIn('test_b_subtest_failure', display.getvalue())
            return (events, result.testsRun, len(result.failures), len(result.errors),
                    len(result.skipped), len(result.expectedFailures),
                    len(result.unexpectedSuccesses), result.wasSuccessful())

        self.assertEqual(execute(False), execute(True))


class IsolatedSupervisorTests(RunnerFixture):
    def test_bootstrap_arms_stack_watchdog_before_guard_and_runner_imports(self):
        bootstrap = next(value for value in run_isolated.run.__code__.co_consts
                         if isinstance(value, str) and 'faulthandler.enable' in value)
        compile(bootstrap, '<disposable-bootstrap>', 'exec')
        self.assertLess(bootstrap.index('open('), bootstrap.index('dump_traceback_later(15'))
        self.assertLess(bootstrap.index('dump_traceback_later(15'), bootstrap.index('import support'))
        self.assertLess(bootstrap.index('support.install_guard'), bootstrap.index('import pretty_runner'))
        self.assertIn('repeat=True', bootstrap)
        self.assertIn('signal.SIGUSR1', bootstrap)

    def test_timeout_keeps_240s_deadline_kills_group_and_preserves_actual_trace(self):
        detail = self.base / 'detail.log'
        detail.write_text('START discovery / loading tests\n', encoding='utf-8')
        child = MagicMock()
        child.pid = 987654
        child.__enter__.return_value = child

        def wait(timeout=None):
            if timeout is not None:
                with detail.open('a', encoding='utf-8') as log:
                    faulthandler.dump_traceback(file=log)
                raise subprocess.TimeoutExpired('synthetic child', timeout)
            return -signal.SIGKILL

        child.wait.side_effect = wait
        with patch('run_isolated.subprocess.Popen', return_value=child) as spawn, \
                patch('run_isolated.os.killpg') as kill_group, contextlib.redirect_stdout(io.StringIO()):
            status = run_isolated.run_child(['synthetic-not-executed'], self.base, {})
            self.assertTrue(run_isolated.preserve_log(self.base, detail))
        self.assertEqual(124, status)
        self.assertEqual([call(timeout=240), call()], child.wait.call_args_list)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        kill_group.assert_called_once_with(child.pid, signal.SIGKILL)
        retained = list((self.base / '.mixie_logs').glob('regression-*.log'))
        self.assertEqual(1, len(retained))
        self.assertEqual(detail.read_bytes(), retained[0].read_bytes())
        self.assertIn('test_timeout_keeps_240s_deadline', retained[0].read_text())

    def test_supervisor_never_inherits_an_interactive_parent_terminal(self):
        """Automation tests must not repeatedly select Ingest Music on a Deck TTY."""
        child = MagicMock()
        child.__enter__.return_value = child
        child.wait.return_value = 0
        with patch('run_isolated.subprocess.Popen', return_value=child) as spawn, \
                patch('run_isolated.sys.stdin') as parent_terminal:
            parent_terminal.isatty.return_value = True
            self.assertEqual(0, run_isolated.run_child(['isolated-fixture'], self.base, {}))
        self.assertEqual(subprocess.DEVNULL, spawn.call_args.kwargs['stdin'])
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])

    def test_supervisor_preserves_nonzero_child_exit_status(self):
        child = MagicMock()
        child.__enter__.return_value = child
        child.wait.return_value = 7
        with patch('run_isolated.subprocess.Popen', return_value=child), \
                patch('run_isolated.os.killpg') as kill_group:
            self.assertEqual(7, run_isolated.run_child(['synthetic-not-executed'], self.base, {}))
        child.wait.assert_called_once_with(timeout=240)
        kill_group.assert_not_called()

    def test_source_copy_rejects_symlink_instead_of_reading_target(self):
        source = self.base / 'source.py'
        source.write_text('synthetic source\n', encoding='utf-8')
        alias = self.base / 'alias.py'
        alias.symlink_to(source)
        with patch('run_isolated.shutil.copy2') as copy:
            with self.assertRaises(RuntimeError):
                run_isolated.copy_source(alias, self.base / 'destination.py')
            copy.assert_not_called()
        destination = self.base / 'copied.py'
        run_isolated.copy_source(source, destination)
        self.assertEqual(source.read_bytes(), destination.read_bytes())

    def test_diagnostic_export_rejects_symlink_directory_and_missing_log(self):
        detail = self.base / 'detail.log'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(run_isolated.preserve_log(self.base, detail))
        detail.write_text('synthetic trace\n', encoding='utf-8')
        target = self.base / 'elsewhere'
        target.mkdir()
        (self.base / '.mixie_logs').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe diagnostic directory'):
            run_isolated.preserve_log(self.base, detail)
