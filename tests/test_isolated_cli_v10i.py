"""Synthetic runner CLI regressions; execute only through tests/run_isolated.py."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pretty_runner


class IsolatedCliTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('MIXIE_ISOLATED_TESTS'):
            raise RuntimeError('Run through tests/run_isolated.py only.')
        temporary = tempfile.TemporaryDirectory(prefix='mixie-cli-v10i-')
        self.addCleanup(temporary.cleanup)
        self.addCleanup(self.clear_modules)
        self.root = Path(temporary.name)
        self.tests = self.root / 'tests'
        self.tests.mkdir()
        self.invocations = 0
        self.write_module('cli_v10i_events', 'EVENTS = []\n')
        self.write_module('test_cli_v10i_alpha', '''import unittest
from cli_v10i_events import EVENTS

class OrderedTests(unittest.TestCase):
    def test_b_second(self):
        EVENTS.append('alpha.second')
        print('synthetic detail-only output')

    def test_a_first(self):
        EVENTS.append('alpha.first')

    @unittest.skip('synthetic retained skip')
    def test_c_skipped(self):
        raise AssertionError('A skipped test must not run')
''')
        self.write_module('test_cli_v10i_beta', '''import unittest
from cli_v10i_events import EVENTS

class OtherTests(unittest.TestCase):
    def test_other(self):
        EVENTS.append('beta.other')
''')
        self.write_module('testcli_v10i_excluded', '''import unittest

class ExcludedTests(unittest.TestCase):
    def test_not_in_default_pattern(self):
        raise AssertionError('The original test_*.py default must be retained')
''')
        self.write_module('cli_v10i_outcomes', '''import unittest

class FailureTests(unittest.TestCase):
    def test_failure(self):
        self.fail('synthetic assertion failure')

class ErrorTests(unittest.TestCase):
    def test_error(self):
        raise RuntimeError('synthetic test error')

class UnexpectedSuccessTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_unexpected_success(self):
        pass
''')

    @staticmethod
    def clear_modules():
        for name in list(sys.modules):
            if name.startswith(('cli_v10i_', 'test_cli_v10i_', 'testcli_v10i_')):
                sys.modules.pop(name, None)

    def write_module(self, name, source):
        (self.tests / (name + '.py')).write_text(source, encoding='utf-8')

    def invoke(self, *arguments):
        self.clear_modules()
        self.invocations += 1
        detail = self.root / f'details-{self.invocations}.log'
        console = io.StringIO()
        with (
            contextlib.chdir(self.root),
            patch.object(sys, 'argv', [pretty_runner.__file__, *arguments]),
            patch.object(sys, 'path', [str(self.tests), str(self.root), *sys.path]),
            patch.object(sys, '__stdout__', console),
            contextlib.redirect_stdout(console),
            contextlib.redirect_stderr(console),
            patch.dict(os.environ, {'MIXIE_ISOLATED_DETAIL_LOG': str(detail)}),
            patch.object(unittest, 'defaultTestLoader', unittest.TestLoader()),
        ):
            try:
                status = pretty_runner.main()
            except SystemExit as error:
                status = error.code
        events = list(getattr(sys.modules.get('cli_v10i_events'), 'EVENTS', []))
        logged = detail.read_text(encoding='utf-8') if detail.is_file() else ''
        return status, console.getvalue(), logged, events

    def test_discovery_options_select_pattern_preserve_order_and_skips(self):
        status, console, logged, events = self.invoke(
            'discover', '-s', 'tests', '-p', 'test_cli_v10i_alpha.py', '-v')
        self.assertEqual(0, status, console + logged)
        self.assertEqual(['alpha.first', 'alpha.second'], events)
        self.assertIn('3 tests queued', console)
        self.assertIn('✓ 2 passed', console)
        self.assertIn('↷ 1 skipped', console)
        self.assertIn('✗ 0 failed', console)
        self.assertIn('Ran 3 tests', logged)
        self.assertIn("skipped 'synthetic retained skip'", logged)
        self.assertIn('synthetic detail-only output', logged)
        self.assertNotIn('synthetic detail-only output', console)
        self.assertNotIn('test_a_first', console)
        self.assertNotIn('_FailedTest', logged)
        self.assertNotIn('\x1b', console + logged)

    def test_default_discovers_all_original_pattern_without_reordering(self):
        status, console, logged, events = self.invoke()
        self.assertEqual(0, status, console + logged)
        self.assertEqual(['alpha.first', 'alpha.second', 'beta.other'], events)
        self.assertIn('4 tests queued', console)
        self.assertIn('Ran 4 tests', logged)
        self.assertIn('↷ 1 skipped', console)
        self.assertNotIn('ExcludedTests', logged)

    def test_discovery_respects_start_and_top_level_directories(self):
        package = self.root / 'cli_v10i_package'
        package.mkdir()
        (package / '__init__.py').write_text('', encoding='utf-8')
        (package / 'test_cli_v10i_nested.py').write_text('''import unittest
from cli_v10i_events import EVENTS

class NestedTests(unittest.TestCase):
    def test_nested(self):
        EVENTS.append('package.nested')
''', encoding='utf-8')
        status, console, logged, events = self.invoke(
            'discover', '-s', 'cli_v10i_package', '-p', 'test_cli_v10i_nested.py',
            '-t', '.', '-v')
        self.assertEqual(0, status, console + logged)
        self.assertEqual(['package.nested'], events)
        self.assertIn('cli_v10i_package.test_cli_v10i_nested.NestedTests', logged)
        self.assertIn('Ran 1 test', logged)

    def test_named_module_class_and_method_targets_remain_supported(self):
        cases = (
            ('test_cli_v10i_alpha', ['alpha.first', 'alpha.second'], 3),
            ('test_cli_v10i_alpha.OrderedTests', ['alpha.first', 'alpha.second'], 3),
            ('test_cli_v10i_alpha.OrderedTests.test_b_second', ['alpha.second'], 1),
        )
        for target, expected, count in cases:
            with self.subTest(target=target):
                status, console, logged, events = self.invoke(target, '-v')
                self.assertEqual(0, status, console + logged)
                self.assertEqual(expected, events)
                self.assertIn(f'Ran {count} test', logged)
                self.assertNotIn('_FailedTest', logged)

    def test_multiple_named_targets_keep_requested_order(self):
        status, console, logged, events = self.invoke(
            'test_cli_v10i_beta.OtherTests', 'test_cli_v10i_alpha.OrderedTests')
        self.assertEqual(0, status, console + logged)
        self.assertEqual(['beta.other', 'alpha.first', 'alpha.second'], events)
        self.assertIn('Ran 4 tests', logged)

    def test_failures_errors_and_unexpected_successes_propagate(self):
        cases = (
            ('FailureTests', 'FAIL', 'synthetic assertion failure'),
            ('ErrorTests', 'ERROR', 'synthetic test error'),
            ('UnexpectedSuccessTests', 'UNEXPECTED SUCCESS', 'unexpected success'),
        )
        for name, marker, diagnostic in cases:
            with self.subTest(outcome=name):
                status, console, logged, _ = self.invoke('cli_v10i_outcomes.' + name)
                self.assertEqual(1, status, console + logged)
                self.assertIn('Ran 1 test', logged)
                self.assertIn(diagnostic, logged)
                self.assertIn(marker, console)
                self.assertIn('✗ 1 failed', console)
        status, console, logged, _ = self.invoke(
            'discover', '-s', 'tests', '-p', 'cli_v10i_outcomes.py', '-v')
        self.assertEqual(1, status, console + logged)
        self.assertIn('Ran 3 tests', logged)
        self.assertIn('✗ 3 failed', console)

    def test_missing_named_target_remains_a_logged_loader_failure(self):
        status, console, logged, events = self.invoke('cli_v10i_missing')
        self.assertEqual(1, status, console + logged)
        self.assertEqual([], events)
        self.assertIn('ModuleNotFoundError', logged)
        self.assertIn('cli_v10i_missing', logged)
        self.assertIn('✗ 1 failed', console)

    def test_malformed_arguments_are_rejected_before_any_tests_run(self):
        cases = (
            ('discover', '--unknown-option'),
            ('discover', '-s'),
            ('discover', '-p'),
            ('discover', '-t'),
            ('--unknown-option',),
            ('discover', '-s', 'tests', 'unexpected-extra-argument'),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                status, console, logged, events = self.invoke(*arguments)
                self.assertEqual(2, status, console + logged)
                self.assertIn('usage:', console.lower())
                self.assertEqual([], events)
                self.assertNotIn('Ran ', logged)
                self.assertNotIn('RESULTS', console)

    def test_empty_discovery_fails_closed_with_diagnostics(self):
        status, console, logged, events = self.invoke(
            'discover', '-s', 'tests', '-p', 'test_cli_v10i_missing*.py')
        self.assertEqual(1, status, console + logged)
        self.assertEqual([], events)
        self.assertIn('No tests matched', console)
        self.assertIn('No tests matched', logged)
        self.assertNotIn('_FailedTest', logged)
