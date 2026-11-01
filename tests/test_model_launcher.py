"""A terminal without hidden input must fail closed before accepting a key."""
import contextlib
import getpass
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import warnings

SPEC = importlib.util.spec_from_file_location('sixu_model_launcher', Path(__file__).resolve().parents[1] / 'scripts/start_model_local.py')
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


class ModelLauncherTests(unittest.TestCase):
    def run_cancelled(self, reader):
        output = io.StringIO()
        with patch.object(LAUNCHER.sys, 'argv', ['start_model_local.py']), patch.object(LAUNCHER.socket, 'socket'), patch.object(LAUNCHER.sys.stdin, 'isatty', return_value=True), patch.object(LAUNCHER.getpass, 'getpass', side_effect=reader), patch.object(LAUNCHER, 'run') as run, contextlib.redirect_stdout(output):
            self.assertEqual(LAUNCHER.main(), 1)
            run.assert_not_called()
        self.assertNotIn('test-secret-that-must-not-be-read', output.getvalue())

    def test_getpass_warning_cannot_fall_back_to_visible_input(self):
        def fallback(*args):
            warnings.warn('terminal echo unavailable', getpass.GetPassWarning)
            self.fail('GetPassWarning must abort before fallback input is read')
        self.run_cancelled(fallback)

    def test_eof_and_interrupt_cancel_before_service_start(self):
        for exception in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(exception).__name__):
                self.run_cancelled(exception)
