"""Unhandled failures in a windowless build must remain diagnosable."""
import logging
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from halo import runtime_diagnostics as R


class RuntimeDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.logger = mock.Mock(spec=logging.Logger)
        self.callbacks = []
        self.faults = mock.Mock()
        for target, value in (
                ("faulthandler", self.faults),
                ("atexit", types.SimpleNamespace(register=self.callbacks.append))):
            patch = mock.patch.object(R, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        for target in ("excepthook",):
            patch = mock.patch.object(sys, target)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(threading, "excepthook")
        patch.start()
        self.addCleanup(patch.stop)
        R.install(self.logger, self.folder)
        self.addCleanup(lambda: self.callbacks[0]())

    def test_fatal_faults_write_to_an_open_file_even_without_stderr(self):
        self.faults.enable.assert_called_once()
        handle = self.faults.enable.call_args.kwargs["file"]
        self.assertFalse(handle.closed)
        self.assertTrue(handle.name.endswith("crash.log"))
        self.assertTrue(self.faults.enable.call_args.kwargs["all_threads"])

    def test_main_exception_keeps_the_traceback(self):
        try:
            raise RuntimeError("diagnostic test")
        except RuntimeError:
            failure = sys.exc_info()
        sys.excepthook(*failure)
        self.logger.critical.assert_called_once_with(
            "unhandled main-thread exception", exc_info=failure)

    def test_thread_exception_keeps_its_name_and_traceback(self):
        args = types.SimpleNamespace(
            exc_type=ValueError, exc_value=ValueError("worker"),
            exc_traceback=None, thread=types.SimpleNamespace(name="poll-worker"))
        threading.excepthook(args)
        self.logger.critical.assert_called_once_with(
            "unhandled exception in thread %s", "poll-worker",
            exc_info=(args.exc_type, args.exc_value, None))

    def test_a_thread_exiting_normally_is_not_a_crash(self):
        threading.excepthook(types.SimpleNamespace(exc_type=SystemExit))
        self.logger.critical.assert_not_called()

    def test_shutdown_is_recorded_and_releases_the_fault_file(self):
        handle = self.faults.enable.call_args.kwargs["file"]
        self.callbacks[0]()
        self.assertTrue(handle.closed)
        self.faults.disable.assert_called_once()
        self.assertTrue(any("interpreter shutdown" in c.args[0]
                            for c in self.logger.info.call_args_list))
        self.callbacks[0] = lambda: None

    def test_unwritable_fault_file_does_not_prevent_startup(self):
        extra = []
        with mock.patch.object(R, "atexit", types.SimpleNamespace(register=extra.append)), \
                mock.patch("builtins.open", side_effect=PermissionError("denied")):
            R.install(self.logger, self.folder)
        self.logger.warning.assert_called_once()
        self.assertEqual(len(extra), 1)
        extra[0]()


if __name__ == "__main__":
    unittest.main()
