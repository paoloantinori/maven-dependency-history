"""Offline process-boundary tests for local distribution inspection."""
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'maven-dependency-investigator' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from mdi import distribution_worker as worker
from mdi.distributions import DistributionLimits


class DistributionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.checkout = self.base / 'checkout'
        self.checkout.mkdir()
        self.scratch = self.base / 'scratch'
        self.scratch.mkdir(mode=0o700)
        self.source = self.checkout / 'dist.tar'
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, 'w') as z:
            z.writestr('META-INF/maven/example/lib/pom.properties', 'groupId=example\nartifactId=lib\nversion=1\n')
        with tarfile.open(self.source, 'w', format=tarfile.USTAR_FORMAT) as t:
            info = tarfile.TarInfo('lib/lib.jar')
            info.size = len(jar.getvalue())
            t.addfile(info, io.BytesIO(jar.getvalue()))
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.fd = os.open(self.source, os.O_RDONLY)
        self.addCleanup(os.close, self.fd)

    def run_worker(self, **over):
        options = dict(source_path=str(self.source), sha256=self.digest,
                       checkout=str(self.checkout), scratch_base=str(self.scratch),
                       format='tar', deadline=time.monotonic() + 10)
        options.update(over)
        return worker.inspect_isolated(self.fd, **options)

    def fake(self, code):
        path = self.base / ('fake-' + str(len(list(self.base.glob('fake-*')))) + '.py')
        path.write_text(code)
        return [sys.executable, '-B', '-I', '-S', str(path)]

    def assert_reaped(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_actual_nested_tar_preserves_caller_and_scratch(self):
        os.lseek(self.fd, 9, os.SEEK_SET)
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['result']['coverage']['status'], 'complete')
        self.assertEqual(result['result']['source']['sha256'], self.digest)
        self.assertEqual(result['result']['archives'][0]['member_name'], 'lib/lib.jar')
        self.assertEqual(result['cleanup']['leftovers'], [])
        self.assertTrue(result['cleanup_result']['worker_stopped'])
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), 9)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assert_reaped(result['worker_pid'])

    def test_actual_gzip(self):
        import gzip
        compressed = self.checkout / 'dist.tar.gz'
        compressed.write_bytes(gzip.compress(self.source.read_bytes()))
        fd = os.open(compressed, os.O_RDONLY)
        try:
            result = worker.inspect_isolated(fd, source_path=str(compressed),
                sha256=hashlib.sha256(compressed.read_bytes()).hexdigest(),
                checkout=str(self.checkout), scratch_base=str(self.scratch),
                format='tar.gz', deadline=time.monotonic() + 10)
        finally:
            os.close(fd)
        self.assertEqual(result['outcome'], 'complete', result)

    def test_digest_mismatch_discards_identity_and_preserves_recovery(self):
        result = self.run_worker(sha256='0' * 64)
        self.assertEqual(result['error'], 'source-changed')
        self.assertIsNone(result['result'])
        self.assertEqual(result['cleanup']['leftovers'][-1]['path'], str(self.scratch))

    def test_timeout_and_flood_retain_scratch_recovery(self):
        for code, cap, expected in [('import time; time.sleep(30)', 4096, 'deadline-exceeded'),
                ('import os\nwhile True: os.write(1,b"x"*65536)', 1024, 'response-too-large')]:
            with self.subTest(error=expected):
                start = time.monotonic()
                result = self.run_worker(worker_command=self.fake(code),
                                         deadline=start + .3, max_response_bytes=cap)
                self.assertEqual(result['error'], expected)
                self.assertEqual(result['cleanup']['leftovers'][0]['path'], str(self.scratch))
                self.assertFalse(result['cleanup']['individual_paths_complete'])
                self.assertTrue(result['cleanup_result']['leader_reaped'])
                self.assert_reaped(result['worker_pid'])
                self.assertLess(time.monotonic() - start, 4)

    def test_trickle_does_not_reset_deadline(self):
        result = self.run_worker(worker_command=self.fake('import os,time\nwhile True: os.write(1,b" ");time.sleep(.02)'),
                                 deadline=time.monotonic() + .25)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assert_reaped(result['worker_pid'])

    def test_expired_deadline_and_nondefault_sigchld_no_spawn(self):
        with mock.patch.object(worker.subprocess, 'Popen') as spawn:
            result = self.run_worker(deadline=time.monotonic() - 1)
            self.assertEqual(result['error'], 'deadline-exceeded')
            spawn.assert_not_called()
            with mock.patch.object(worker.signal, 'getsignal', return_value=signal.SIG_IGN):
                with self.assertRaises(worker.DistributionWorkerError):
                    self.run_worker()
            spawn.assert_not_called()

    def test_bad_protocol_never_leaks_text_and_keeps_root_obligation(self):
        for text in ('TOPSECRET', '{"schema":1,"schema":2}', '{"x":NaN}'):
            result = self.run_worker(worker_command=self.fake('print(' + repr(text) + ')'))
            self.assertEqual(result['error'], 'invalid-response')
            self.assertNotIn('TOPSECRET', json.dumps(result))
            self.assertEqual(result['cleanup']['leftovers'][0]['path'], str(self.scratch))

    def envelope_command(self, change):
        good = self.run_worker()
        result = good['result']
        change(result)
        envelope = {'schema': worker.SCHEMA, 'result': result, 'error': None,
                    'cleanup': good['cleanup']}
        return self.fake('print(' + repr(json.dumps(envelope)) + ')')

    def test_forged_member_and_nested_identity_rejected(self):
        def source(r): r['source']['sha256'] = '0' * 64
        def member(r): r['members'][0]['size_actual'] = 10**12
        def nested(r): r['archives'][0]['inspection']['archives'][0]['sha256'] = '0' * 64
        def missing(r): r['members'].clear()
        for change in (source, member, nested, missing):
            with self.subTest(change=change.__name__):
                result = self.run_worker(worker_command=self.envelope_command(change))
                self.assertEqual(result['error'], 'invalid-response', result)
                self.assertIsNone(result['result'])

    def test_partial_distribution_and_empty_tar_are_not_errors(self):
        self.source.write_bytes(b'not a tar')
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete')
        self.assertEqual(result['result']['coverage']['status'], 'partial')
        self.source.write_bytes(bytes(1024))
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.run_worker()
        self.assertEqual(result['result']['members'], [])
        self.assertEqual(result['result']['coverage']['status'], 'complete')

    def test_interrupt_preserved_with_lifecycle_and_recovery_metadata(self):
        error = KeyboardInterrupt('primary')
        with mock.patch.object(worker, '_exchange', side_effect=error):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_worker(worker_command=self.fake('import time;time.sleep(30)'))
        self.assertIs(caught.exception, error)
        self.assertTrue(error.distribution_cleanup['worker']['leader_reaped'])
        self.assertEqual(error.distribution_cleanup['leftovers'][0]['path'], str(self.scratch))

    def test_signal_before_reap_and_lost_identity_no_signal(self):
        original = os.killpg
        signals = []
        def signal_owned(pid, sig):
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            signals.append(pid)
            return original(pid, sig)
        with mock.patch.object(worker.os, 'killpg', side_effect=signal_owned):
            result = self.run_worker()
        self.assertTrue(signals)
        self.assertEqual(result['cleanup_result']['returncode'], 0)
        proc = mock.Mock(pid=987654)
        with mock.patch.object(worker.boundary.os, 'waitid', side_effect=ChildProcessError), \
                mock.patch.object(worker.boundary.os, 'killpg') as kill:
            cleanup = worker._cleanup(proc, .01)
        kill.assert_not_called()
        self.assertFalse(cleanup['worker_stopped'])
        self.assertIsNone(cleanup['returncode'])

    def test_valid_partial_result_keeps_retained_cleanup_metadata(self):
        retained = self.scratch / 'retained.spool'
        retained.write_bytes(b'caller-owned recovery fixture')
        good = self.run_worker()
        ledger = {'leftovers': [{'path': str(retained), 'reason': 'distribution-spool-unconfirmed'}],
                  'individual_paths_complete': True}
        result = good['result']
        result['coverage']['status'] = 'partial'
        result['coverage']['cleanup'] = {'leftovers': ledger['leftovers']}
        result['issues'].append({'code': 'scratch-retained'})
        envelope = dict(schema=worker.SCHEMA, result=result, error=None, cleanup=ledger)
        outcome = self.run_worker(worker_command=self.fake('print(' + repr(json.dumps(envelope)) + ')'))
        self.assertEqual(outcome['outcome'], 'complete', outcome)
        self.assertEqual(outcome['cleanup'], ledger)
        self.assertTrue(retained.exists())

    def test_close_failure_preserves_primary_interrupt_and_other_descriptors(self):
        original_close = os.close
        closed = []
        primary = KeyboardInterrupt('original')
        baseline = len(os.listdir('/proc/self/fd'))
        def owned(fd):
            # subprocess closes its own descriptors through os.close too (a
            # pidfd on Python 3.15); the worker-owned ones are exactly the
            # descriptors pointing under this test's private base, including
            # its dup of the source distribution.
            try:
                target = os.readlink(f'/proc/self/fd/{fd}')
            except OSError:
                return False
            return target.startswith(str(self.base))
        def release_then_fail(fd):
            is_owned = owned(fd)
            original_close(fd)
            if is_owned:
                closed.append(fd)
                if len(closed) == 1:
                    raise OSError('after release')
        def interrupted(*args):
            # Restrict the close injection to finalization after successful spawn.
            patch = mock.patch.object(worker.os, 'close', side_effect=release_then_fail)
            patch.start()
            self.addCleanup(patch.stop)
            raise primary
        with mock.patch.object(worker, '_exchange', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_worker(worker_command=self.fake('import time;time.sleep(30)'))
        self.assertIs(caught.exception, primary)
        self.assertEqual(len(closed), 3)
        self.assertEqual(len(set(closed)), 3)
        self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)
        os.fstat(self.fd)
        self.assertEqual(primary.distribution_cleanup['leftovers'][0]['path'], str(self.scratch))

    def test_worker_exception_preserves_lower_cleanup_ledger(self):
        retained = self.scratch / 'retained-member.spool'
        command = self.fake(
            'import sys,pathlib\n'
            f'sys.path.insert(0,{str(SCRIPTS)!r})\n'
            'from mdi import distribution_worker as w\n'
            'def failed(*args,**kwargs):\n'
            f' pathlib.Path({str(retained)!r}).write_bytes(b"partial bytes")\n'
            ' error=RuntimeError("TOPSECRET")\n'
            f' error.distribution_cleanup={{"leftovers":[{{"path":{str(retained)!r},"reason":"distribution-spool-unconfirmed"}}]}}\n'
            ' raise error\n'
            'w.inspect_distribution_fd=failed\n'
            'raise SystemExit(w._worker())\n')
        result = self.run_worker(worker_command=command)
        self.assertEqual(result['error'], 'inspection-failed')
        self.assertEqual(result['cleanup']['leftovers'][0]['path'], str(retained))
        self.assertTrue(retained.is_file())
        self.assertNotIn('TOPSECRET', json.dumps(result))

    def test_blocked_request_write_remains_deadline_bounded(self):
        command = self.fake('import time;time.sleep(30)')
        real_dumps = json.dumps
        # Valid request-size input, made larger than a small pipe's capacity;
        # the peer never reads stdin. This tests the shared selector path.
        def large_request(value, **kwargs):
            return real_dumps(value, **kwargs) + ' ' * 48000
        with mock.patch.object(worker.json, 'dumps', side_effect=large_request):
            result = self.run_worker(worker_command=command, deadline=time.monotonic()+.25)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assert_reaped(result['worker_pid'])

    def test_small_response_cap_returns_recovery_not_large_partial_json(self):
        result = self.run_worker(max_response_bytes=256)
        self.assertEqual(result['error'], 'response-too-large')
        self.assertFalse(result['cleanup']['individual_paths_complete'])
        self.assertTrue(result['cleanup_result']['worker_stopped'])

    def test_working_directory_and_checkout_descriptor_identity_bound(self):
        other = self.base / 'other'
        other.mkdir()
        def replace_then_spawn(*args, **kwargs):
            self.checkout.rename(other / 'moved')
            self.checkout.mkdir()
            return original(*args, **kwargs)
        original = subprocess.Popen
        with mock.patch.object(worker.subprocess, 'Popen', side_effect=replace_then_spawn):
            result = self.run_worker()
        self.assertEqual(result['error'], 'checkout-identity-changed')
        self.assertTrue(result['cleanup']['leftovers'])

    def test_startup_budget_is_not_reset(self):
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = original(*args, **kwargs)
            time.sleep(.15)
            return proc
        with mock.patch.object(worker.subprocess, 'Popen', side_effect=spawn):
            result = self.run_worker(worker_command=self.fake('import time;time.sleep(30)'),
                                     deadline=time.monotonic() + .1)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assert_reaped(result['worker_pid'])

    def test_repeated_actual_runs_do_not_leak_descriptors(self):
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(3): self.assertEqual(self.run_worker()['outcome'], 'complete')
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_real_worker_applies_requested_address_space_cap(self):
        marker = self.base / 'limit.json'
        command = self.fake(
            'import sys,json,resource\n'
            f'sys.path.insert(0,{str(SCRIPTS)!r})\n'
            'from mdi import distribution_worker as w\n'
            'original=w.inspect_distribution_fd\n'
            'def inspected(*args,**kwargs):\n'
            f' with open({str(marker)!r},"w") as out: json.dump(resource.getrlimit(resource.RLIMIT_AS),out)\n'
            ' return original(*args,**kwargs)\n'
            'w.inspect_distribution_fd=inspected\n'
            'raise SystemExit(w._worker())\n')
        cap = 256 * 1024**2
        result = self.run_worker(worker_command=command, memory_bytes=cap)
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(json.loads(marker.read_text()), [cap, cap])

    def test_default_worker_does_not_modify_copied_skill(self):
        import shutil
        copied = self.base / 'skill'
        shutil.copytree(SCRIPTS.parent, copied, ignore=shutil.ignore_patterns('__pycache__'))
        def snapshot():
            return {str(p.relative_to(copied)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in copied.rglob('*') if p.is_file()}
        before = snapshot()
        result = self.run_worker(worker_command=[sys.executable, '-B', '-I', '-S',
            str(copied / 'scripts' / 'mdi' / 'distribution_worker.py'), '--worker'])
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(snapshot(), before)

    def test_nonregular_writable_and_overlapping_inputs_refused(self):
        with self.assertRaises((ValueError, OSError)):
            self.run_worker(scratch_base=str(self.checkout))
        with mock.patch.object(worker.subprocess, 'Popen') as spawn:
            write_fd = os.open(self.source, os.O_RDWR)
            try:
                with self.assertRaises(ValueError):
                    worker.inspect_isolated(write_fd, source_path=str(self.source),
                        sha256=self.digest, checkout=str(self.checkout),
                        scratch_base=str(self.scratch), format='tar', deadline=time.monotonic()+10)
            finally:
                os.close(write_fd)
            spawn.assert_not_called()
        for option in ({'format':'oci'}, {'deadline':float('nan')}, {'memory_bytes':True}):
            with self.subTest(option=option), self.assertRaises(worker.DistributionWorkerError):
                self.run_worker(**option)

    def test_worker_has_memory_cap_and_ignores_hostile_environment(self):
        before = dict(os.environ)
        try:
            os.environ['PYTHONPATH'] = str(self.base)
            os.environ['TOPSECRET'] = 'never-child'
            os.environ['HTTP_PROXY'] = 'http://secret.invalid'
            (self.base / 'sitecustomize.py').write_text('raise RuntimeError("injected")')
            result = self.run_worker()
        finally:
            os.environ.clear(); os.environ.update(before)
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['execution']['memory_bytes'], worker.MAX_MEMORY_BYTES)


if __name__ == '__main__':
    unittest.main()
