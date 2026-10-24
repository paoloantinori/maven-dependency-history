"""Synthetic children only; local runner ownership never depends on Popen's ECHILD fallback.

The Popen doubles used here carry real integers and real fileno stubs: a bare
MagicMock coerces to 1 via __index__, and os.killpg(1) is the syscall
kill(-1, SIGKILL), which would signal every process of the user. The guards
under test exist so that can never happen again.
"""

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'))
from mdi import gitinspect, usage

MODULES = (gitinspect, usage)


class LocalRunnerIdentityTests(unittest.TestCase):
    def run_child(self, module, code='import sys; sys.exit(7)', timeout=2):
        with tempfile.TemporaryDirectory() as cwd:
            return module._run([sys.executable, '-c', code], cwd, dict(os.environ),
                               timeout_seconds=timeout, max_output_bytes=1024)

    def test_ignored_sigchld_refused_before_spawn(self):
        previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        try:
            for module in MODULES:
                with self.subTest(module=module.__name__):
                    with self.assertRaises(module._SafetyCapError):
                        self.run_child(module)
        finally:
            signal.signal(signal.SIGCHLD, previous)

    def test_default_disposition_preserves_real_exit_code(self):
        for module in MODULES:
            with self.subTest(module=module.__name__):
                result = self.run_child(module)
                self.assertEqual(result.rc, 7)
                self.assertEqual(result.outcome, 'ok')

    def test_custom_and_foreign_dispositions_refused_before_spawn(self):
        for handler in (lambda *_: None, None):
            for module in MODULES:
                with self.subTest(module=module.__name__, handler=handler):
                    with mock.patch.object(signal, 'getsignal', return_value=handler), \
                            mock.patch.object(module.subprocess, 'Popen',
                                              spec=subprocess.Popen) as spawn:
                        with self.assertRaises(module._SafetyCapError):
                            self.run_child(module)
                        spawn.assert_not_called()

    def test_invalid_pgid_is_never_signaled(self):
        for module in MODULES:
            for pid in (1, 0, -1, -999, True, mock.MagicMock()):
                with self.subTest(module=module.__name__, pid=repr(pid)):
                    with mock.patch.object(module.os, 'killpg') as kill:
                        state = module._try_kill_group(mock.Mock(pid=pid))
                    self.assertEqual(state, 'refused-invalid-pgid')
                    kill.assert_not_called()

    def test_invalid_pgid_is_never_signaled_in_the_other_engines(self):
        """The pgid guard exists in five copies; each must refuse alone."""
        from mdi import advisory_transport, archive_worker, maven_executor
        for engine, kill in ((maven_executor, maven_executor._try_kill_group),
                             (advisory_transport, advisory_transport._try_kill_group)):
            for pid in (1, 0, -1, True, mock.MagicMock()):
                with self.subTest(engine=engine.__name__, pid=repr(pid)):
                    with mock.patch.object(engine.os, 'killpg') as patched:
                        self.assertEqual(kill(mock.Mock(pid=pid)), 'refused-invalid-pgid')
                        patched.assert_not_called()
        for pid in (1, 0, -1, True, mock.MagicMock()):
            with self.subTest(engine='archive_worker', pid=repr(pid)):
                with mock.patch.object(archive_worker, '_probe', return_value='alive'), \
                        mock.patch.object(archive_worker.os, 'killpg') as patched:
                    result = archive_worker._cleanup(mock.Mock(pid=pid), grace=0.01)
                self.assertEqual(result['kill_state'], 'refused-invalid-pgid')
                patched.assert_not_called()

    def test_valid_pgid_is_signaled_normally(self):
        for module in MODULES:
            with self.subTest(module=module.__name__):
                with mock.patch.object(module.os, 'killpg') as kill:
                    state = module._try_kill_group(mock.Mock(pid=4242))
                self.assertEqual(state, 'sent')
                kill.assert_called_once_with(4242, signal.SIGKILL)

    def test_fabricated_stream_descriptors_refuse_before_selector(self):
        """A stdout/stderr whose fileno() coerces to a std fd must never reach
        the selector: epoll_ctl on a regular file (redirected log) raises
        EPERM into the kill path, which is how the desktop died."""
        for module in MODULES:
            with self.subTest(module=module.__name__):
                fake = mock.Mock(
                    pid=1,
                    stdout=mock.Mock(**{'fileno.return_value': 1}),
                    stderr=mock.Mock(**{'fileno.return_value': 2}),
                    poll=mock.Mock(return_value=0))
                with tempfile.TemporaryDirectory() as cwd:
                    with mock.patch.object(module.subprocess, 'Popen', return_value=fake), \
                            mock.patch.object(module.os, 'killpg') as kill:
                        result = module._run([sys.executable, '-c', 'pass'], cwd, {},
                                             timeout_seconds=2, max_output_bytes=1024)
                self.assertEqual(result.outcome, 'error')
                self.assertIn('failed validation', result.issue)
                kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
