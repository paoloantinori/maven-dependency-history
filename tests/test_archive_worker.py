"""Offline tests of the archive worker process boundary."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'maven-dependency-investigator' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import archive_worker
from mdi.archive_files import FileArchiveLimits
from tests import child_env


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.scratch = self.base / 'scratch'
        self.scratch.mkdir(mode=0o700)
        self.archive = self.base / 'input.jar'
        with zipfile.ZipFile(self.archive, 'w') as zf:
            zf.writestr('META-INF/maven/example/lib/pom.properties',
                        'groupId=example\nartifactId=lib\nversion=1\n')
            zf.writestr('pkg/C.class', b'fixture bytes, never loaded')
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.fd = os.open(self.archive, os.O_RDONLY)
        self.addCleanup(os.close, self.fd)

    def run_worker(self, **kwargs):
        options = dict(source_path=str(self.archive), sha256=self.digest,
                       scratch_base=str(self.scratch), deadline=time.monotonic() + 10)
        options.update(kwargs)
        return archive_worker.inspect_isolated(self.fd, **options)

    def fake(self, body):
        script = self.base / ('worker-' + str(len(list(self.base.glob('worker-*')))) + '.py')
        script.write_text(body)
        return [sys.executable, '-I', '-S', str(script)]

    def assert_reaped(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_actual_archive_roundtrip_preserves_caller_fd_offset_and_scratch(self):
        os.lseek(self.fd, 7, os.SEEK_SET)
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['result']['coverage']['status'], 'inspected')
        self.assertEqual(len(result['result']['entries']), 2)
        self.assertEqual(result['result']['coverage']['outer_capture']['sha256'], self.digest)
        self.assertTrue(result['cleanup_result']['worker_stopped'])
        self.assertTrue(result['cleanup_result']['leader_reaped'])
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), 7)
        self.assertTrue(self.scratch.is_dir())
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assert_reaped(result['worker_pid'])

    def test_actual_nested_archive(self):
        import io
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as zf:
            zf.writestr('x.txt', b'content')
        with zipfile.ZipFile(self.archive, 'w') as zf:
            zf.writestr('BOOT-INF/lib/nested.jar', nested.getvalue())
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(len(result['result']['archives']), 2)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_actual_mismatch_is_inspection_rejection_not_transport_success_claim(self):
        result = self.run_worker(sha256='0' * 64)
        self.assertEqual(result['outcome'], 'complete')
        self.assertEqual(result['result']['coverage']['status'], 'rejected')
        self.assertFalse(result['result']['coverage']['outer_capture']['complete'])

    def test_deadline_kills_blocked_worker_and_never_retries(self):
        marker = self.base / 'started'
        command = self.fake(f"import pathlib,time\npathlib.Path({str(marker)!r}).write_text('started')\ntime.sleep(30)\n")
        with mock.patch.object(archive_worker.subprocess, 'Popen', wraps=subprocess.Popen) as spawn:
            start = time.monotonic()
            result = self.run_worker(worker_command=command, deadline=start + .3)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assertEqual(result['outcome'], 'failed')
        self.assertTrue(marker.exists())
        self.assertTrue(result['cleanup_result']['worker_stopped'])
        self.assert_reaped(result['worker_pid'])
        self.assertEqual(spawn.call_count, 1)
        self.assertLess(time.monotonic() - start, 3)

    def test_trickle_does_not_extend_absolute_deadline(self):
        command = self.fake("import os,time\nwhile True:\n os.write(1,b' ');time.sleep(.03)\n")
        start = time.monotonic()
        result = self.run_worker(worker_command=command, deadline=start + .3)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assert_reaped(result['worker_pid'])
        self.assertLess(time.monotonic() - start, 3)

    def test_startup_time_consumes_shared_budget(self):
        original = subprocess.Popen
        def slow_spawn(*args, **kwargs):
            proc = original(*args, **kwargs)
            time.sleep(.15)
            return proc
        command = self.fake('import time;time.sleep(30)')
        with mock.patch.object(archive_worker.subprocess, 'Popen', side_effect=slow_spawn):
            result = self.run_worker(worker_command=command, deadline=time.monotonic() + .1)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assertTrue(result['cleanup_result']['worker_stopped'])

    def test_expired_deadline_never_spawns(self):
        with mock.patch.object(archive_worker.subprocess, 'Popen') as spawn:
            result = self.run_worker(deadline=time.monotonic() - 1)
        spawn.assert_not_called()
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assertTrue(result['cleanup_result']['worker_stopped'])
        self.assertIsNone(result['worker_pid'])

    def test_stdout_flood_is_capped_and_stderr_not_retained(self):
        command = self.fake("import os\nos.write(2,b'TOPSECRET')\nwhile True:os.write(1,b'x'*65536)\n")
        result = self.run_worker(worker_command=command, max_response_bytes=1024)
        self.assertEqual(result['error'], 'response-too-large')
        self.assertNotIn('TOPSECRET', json.dumps(result))
        self.assert_reaped(result['worker_pid'])

    def test_malformed_response_is_fixed_error_without_body_leak(self):
        command = self.fake("print('TOPSECRET malformed')")
        result = self.run_worker(worker_command=command)
        self.assertEqual(result['error'], 'invalid-response')
        self.assertNotIn('TOPSECRET', json.dumps(result))

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        for text in ('{"schema":1,"schema":2}', '{"value":NaN}', '{"x":1e999}'):
            with self.subTest(text=text):
                result = self.run_worker(worker_command=self.fake('print(' + repr(text) + ')'))
                self.assertEqual(result['error'], 'invalid-response')

    def test_wrong_response_shape_or_unknown_error_is_rejected(self):
        for envelope in (
            {'schema': archive_worker.SCHEMA, 'result': {}, 'error': None},
            {'schema': archive_worker.SCHEMA, 'result': None, 'error': 'secret-token'},
            {'schema': archive_worker.SCHEMA, 'result': [], 'error': None},
        ):
            with self.subTest(envelope=envelope):
                result = self.run_worker(worker_command=self.fake('print(' + repr(json.dumps(envelope)) + ')'))
                self.assertEqual(result['error'], 'invalid-response')
                self.assertNotIn('secret-token', json.dumps(result))

    def test_lingering_worker_after_json_never_returns_success(self):
        command = self.fake("import os,time,json\nos.write(1,b'{}\\n');os.close(1);time.sleep(30)")
        result = self.run_worker(worker_command=command, deadline=time.monotonic() + .2)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assert_reaped(result['worker_pid'])

    def test_nonzero_exit_is_failed_even_with_envelope(self):
        command = self.fake("print('{}');raise SystemExit(7)")
        result = self.run_worker(worker_command=command)
        self.assertEqual(result['error'], 'worker-failed')

    def test_hostile_inherited_environment_not_passed(self):
        captured = {}
        original = subprocess.Popen
        def record(*args, **kwargs):
            captured.update(kwargs['env'])
            return original(*args, **kwargs)
        with mock.patch.dict(os.environ, {'PYTHONPATH': str(self.base), 'HTTP_PROXY': 'SECRET',
                                         'JAVA_TOOL_OPTIONS': 'SECRET', 'LD_PRELOAD': 'SECRET'}):
            with mock.patch.object(archive_worker.subprocess, 'Popen', side_effect=record):
                result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(set(captured), {'PATH', 'HOME', 'TMPDIR', 'LC_ALL'})
        self.assertNotIn('SECRET', json.dumps(captured))

    def test_real_worker_enforces_memory_limit(self):
        original = archive_worker._set_memory_limit
        infinity = (archive_worker.resource.RLIM_INFINITY,) * 2
        with mock.patch.object(archive_worker.resource, 'setrlimit') as setter:
            with mock.patch.object(archive_worker.resource, 'getrlimit',
                                   return_value=infinity):
                original(768 * 1024**2)
        setter.assert_called_once_with(archive_worker.resource.RLIMIT_AS,
                                      (768 * 1024**2, 768 * 1024**2))

    def test_missing_resource_support_refuses_before_spawn(self):
        with mock.patch.object(archive_worker, 'resource', None):
            with mock.patch.object(archive_worker.subprocess, 'Popen') as spawn:
                with self.assertRaises(archive_worker.ArchiveWorkerError):
                    self.run_worker()
        spawn.assert_not_called()

    def test_nondefault_sigchld_refuses_before_spawn(self):
        with mock.patch.object(archive_worker.signal, 'getsignal', return_value=signal.SIG_IGN):
            with mock.patch.object(archive_worker.subprocess, 'Popen') as spawn:
                with self.assertRaises(archive_worker.ArchiveWorkerError):
                    self.run_worker()
        spawn.assert_not_called()

    def test_interrupt_cleans_process_and_carries_cleanup_observation(self):
        proc_seen = []
        original = subprocess.Popen
        def record(*args, **kwargs):
            proc = original(*args, **kwargs)
            proc_seen.append(proc)
            return proc
        with mock.patch.object(archive_worker.subprocess, 'Popen', side_effect=record):
            with mock.patch.object(archive_worker, '_exchange', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.run_worker(worker_command=self.fake('import time;time.sleep(30)'))
        self.assertTrue(caught.exception.archive_cleanup['worker_stopped'])
        self.assert_reaped(proc_seen[0].pid)
        os.fstat(self.fd)

    def test_lost_identity_suppresses_signals_and_does_not_claim_cleanup(self):
        proc = mock.Mock(pid=12345, returncode=None)
        with mock.patch.object(archive_worker.os, 'waitid', side_effect=ChildProcessError):
            with mock.patch.object(archive_worker.os, 'killpg') as kill:
                result = archive_worker._cleanup(proc, .01)
        kill.assert_not_called()
        self.assertFalse(result['worker_stopped'])
        self.assertEqual(result['status'], 'unconfirmed')

    def test_cleanup_signals_before_reaping(self):
        events = []
        original_wait = subprocess.Popen.wait
        original_kill = os.killpg
        def wait(proc, *args, **kwargs):
            events.append('reap')
            return original_wait(proc, *args, **kwargs)
        def kill(pid, sig):
            events.append('signal')
            state = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            return original_kill(pid, sig)
        with mock.patch.object(archive_worker.subprocess.Popen, 'wait', wait):
            with mock.patch.object(archive_worker.os, 'killpg', side_effect=kill):
                result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertLess(events.index('signal'), events.index('reap'))
        self.assertNotIn('signal', events[events.index('reap'):])

    def test_wrong_scratch_permissions_and_symlink_refused(self):
        self.scratch.chmod(0o755)
        with self.assertRaises(archive_worker.ArchiveWorkerError):
            self.run_worker()
        self.scratch.chmod(0o700)
        alias = self.base / 'alias'
        alias.symlink_to(self.scratch, target_is_directory=True)
        with self.assertRaises(archive_worker.ArchiveWorkerError):
            self.run_worker(scratch_base=str(alias))

    def test_invalid_arguments_do_not_spawn(self):
        for over in ({'deadline': float('nan')}, {'deadline': True}, {'sha256': 'bad'},
                     {'max_response_bytes': 24 * 1024**2 + 1}, {'max_response_bytes': 0},
                     {'memory_bytes': 768 * 1024**2 + 1}, {'worker_command': 'shell command'},
                     {'source_path': 'x' * 8192}, {'limits': {}}):
            with self.subTest(over=over), mock.patch.object(archive_worker.subprocess, 'Popen') as spawn:
                with self.assertRaises(archive_worker.ArchiveWorkerError):
                    self.run_worker(**over)
                spawn.assert_not_called()

    def test_blocked_stdin_remains_under_shared_deadline(self):
        command = self.fake('import time;time.sleep(30)')
        start = time.monotonic()
        result = self.run_worker(source_path='\U0001f600' * 4000, worker_command=command,
                                 deadline=start + .2)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assert_reaped(result['worker_pid'])
        self.assertLess(time.monotonic() - start, 3)

    def test_forged_inventory_digest_and_counts_refused(self):
        good = self.run_worker()['result']
        import copy
        for mutate in (
            lambda r: r['coverage']['outer_capture'].update(sha256='0' * 64),
            lambda r: r['coverage'].update(entries_recorded=999999),
            lambda r: r['coverage'].update(expanded_bytes_used=-1),
            lambda r: r['coverage'].update(input_bytes=0),
            lambda r: r['entries'].append(dict(r['entries'][0])),
        ):
            value = copy.deepcopy(good)
            mutate(value)
            envelope = json.dumps({'schema': archive_worker.SCHEMA, 'result': value, 'error': None})
            result = self.run_worker(worker_command=self.fake('print(' + repr(envelope) + ')'))
            self.assertEqual(result['error'], 'invalid-response')

    def test_real_memory_limit_primitive(self):
        code = ('import sys,json,resource;sys.path.insert(0,' + repr(str(SCRIPTS)) + ');'
                'from mdi.archive_worker import _set_memory_limit;'
                '_set_memory_limit(768*1024**2);'
                'print(json.dumps(resource.getrlimit(resource.RLIMIT_AS)))')
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code],
                                capture_output=True, timeout=10, check=True,
                                env=child_env())
        limits = json.loads(result.stdout)
        self.assertGreater(limits[0], 0)
        self.assertLessEqual(limits[0], 768 * 1024**2)
        self.assertEqual(limits[0], limits[1])

    def test_fd_inventory_stable_over_repeated_success_and_failure(self):
        before = set(os.listdir('/proc/self/fd'))
        for _ in range(3):
            self.assertEqual(self.run_worker()['outcome'], 'complete')
            self.assertEqual(self.run_worker(worker_command=self.fake("print('bad')"))['error'],
                             'invalid-response')
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_scratch_replacement_before_worker_open_is_refused(self):
        original = subprocess.Popen
        moved = self.base / 'moved'
        def replace(*args, **kwargs):
            self.scratch.rename(moved)
            self.scratch.mkdir(mode=0o700)
            return original(*args, **kwargs)
        with mock.patch.object(archive_worker.subprocess, 'Popen', side_effect=replace):
            result = self.run_worker()
        self.assertEqual(result['error'], 'scratch-identity-changed')
        self.assertTrue(moved.is_dir())
        self.assertTrue(self.scratch.is_dir())

    def test_final_close_failure_preserves_interrupt_and_closes_other_owned_fd(self):
        before = set(os.listdir('/proc/self/fd'))
        owned = []
        original_spawn = subprocess.Popen
        original_close = os.close
        interrupt = KeyboardInterrupt('original interruption')
        closed = []

        def record_spawn(*args, **kwargs):
            owned.extend(kwargs['pass_fds'])
            return original_spawn(*args, **kwargs)

        def close_then_fail(fd):
            original_close(fd)
            if fd in owned:
                closed.append(fd)
                if fd == owned[1]:
                    raise OSError('close failed after release')

        with mock.patch.object(archive_worker.subprocess, 'Popen', side_effect=record_spawn):
            with mock.patch.object(archive_worker, '_exchange', side_effect=interrupt):
                with mock.patch.object(archive_worker.os, 'close', side_effect=close_then_fail):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        self.run_worker(worker_command=self.fake('import time;time.sleep(30)'))
        self.assertIs(caught.exception, interrupt)
        self.assertTrue(interrupt.archive_cleanup['worker_stopped'])
        self.assertCountEqual(closed, owned)
        self.assertEqual(len(closed), 2)
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)
        os.fstat(self.fd)

    def test_inventory_root_and_expansion_binding(self):
        import copy
        good = self.run_worker()['result']

        def empty_success(value):
            value['archives'] = []
            value['entries'] = []
            value['coverage'].update(archives_total=0, archives_complete=0,
                                     entries_recorded=0, entries_hashed=0,
                                     entries_budget_used=0, expanded_bytes_used=0)

        changes = (
            lambda r: r['archives'][0].update(sha256='0' * 64),
            lambda r: r['archives'][0].update(size=0),
            lambda r: r['entries'][0].update(size_actual=999999999999),
            lambda r: r['entries'][0].update(size_actual=1),
            lambda r: r['coverage'].update(expanded_bytes_used=0),
            lambda r: r['coverage'].update(entries_hashed=0),
            lambda r: r['coverage'].update(archives_complete=0),
            lambda r: r['coverage'].update(entries_budget_limit=1),
            lambda r: r['archives'][0].update(entry_chain=[0]),
            empty_success,
        )
        for change in changes:
            with self.subTest(change=change):
                value = copy.deepcopy(good)
                change(value)
                envelope = json.dumps({'schema': archive_worker.SCHEMA, 'result': value, 'error': None})
                result = self.run_worker(worker_command=self.fake('print(' + repr(envelope) + ')'))
                self.assertEqual(result['error'], 'invalid-response')

    def test_nested_hierarchy_binding_rejects_forgery(self):
        import copy
        import io
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as zf:
            zf.writestr('x', b'child')
        with zipfile.ZipFile(self.archive, 'w') as zf:
            zf.writestr('lib/child.jar', nested.getvalue())
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        good = self.run_worker()['result']
        for change in (
            lambda r: r['archives'][1].update(entry_chain=[999]),
            lambda r: r['archives'][1].update(sha256='0' * 64),
            lambda r: r['archives'][1].update(size=1),
            lambda r: r['archives'][1].update(archive_id='unbound-id'),
        ):
            value = copy.deepcopy(good)
            change(value)
            envelope = json.dumps({'schema': archive_worker.SCHEMA, 'result': value, 'error': None})
            result = self.run_worker(worker_command=self.fake('print(' + repr(envelope) + ')'))
            self.assertEqual(result['error'], 'invalid-response')

    def test_legitimate_partial_and_empty_archive_shapes_survive_validation(self):
        for limits in (FileArchiveLimits(max_entry_bytes=2),
                       FileArchiveLimits(max_expanded_bytes=2),
                       FileArchiveLimits(max_entries=1),
                       FileArchiveLimits(max_metadata_entry_bytes=2)):
            with self.subTest(limits=limits):
                result = self.run_worker(limits=limits)
                self.assertEqual(result['outcome'], 'complete', result)
                self.assertIn(result['result']['coverage']['status'], ('partial', 'rejected'))
        with zipfile.ZipFile(self.archive, 'w'):
            pass
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(len(result['result']['archives']), 1)
        self.assertEqual(result['result']['entries'], [])

    def test_partial_nested_invalidation_is_not_rejected_as_forgery(self):
        import io
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as zf:
            zf.writestr('inside', b'data')
        with zipfile.ZipFile(self.archive, 'w') as zf:
            zf.writestr('child.jar', nested.getvalue())
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        value = self.run_worker()['result']
        root, child = value['archives']
        child.update(sha256=None, inspection_status='partial',
                     identity={'status': 'unknown', 'conflict': False, 'candidates': [], 'evidence': []})
        value['entries'][0].update(sha256=None, status='read-error')
        value['entries'][1]['sha256'] = None
        root['inspection_status'] = 'partial'
        value['coverage'].update(status='partial', archives_complete=0, entries_hashed=0)
        value['issues'].append({'archive_id': child['archive_id'], 'code': 'input-changed',
                                'detail': 'nested source changed'})
        envelope = json.dumps({'schema': archive_worker.SCHEMA, 'result': value, 'error': None})
        result = self.run_worker(worker_command=self.fake('print(' + repr(envelope) + ')'))
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['result']['coverage']['status'], 'partial')

    def test_real_worker_leaves_copied_skill_tree_byte_identical(self):
        import shutil
        copied = self.base / 'copied-skill'
        shutil.copytree(SCRIPTS.parent, copied, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

        def inventory():
            return {str(path.relative_to(copied)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in copied.rglob('*') if path.is_file()}

        before = inventory()
        wrapper = self.base / 'invoke-copy.py'
        wrapper.write_text(
            'import json,os,sys,time\n'
            'sys.path.insert(0, ' + repr(str(copied / 'scripts')) + ')\n'
            'from mdi.archive_worker import inspect_isolated\n'
            'fd=os.open(sys.argv[1],os.O_RDONLY)\n'
            'try:\n'
            ' result=inspect_isolated(fd,source_path=sys.argv[1],sha256=sys.argv[2],'
            'scratch_base=sys.argv[3],deadline=time.monotonic()+10)\n'
            ' print(json.dumps(result))\n'
            'finally: os.close(fd)\n')
        completed = subprocess.run(
            [sys.executable, '-B', '-I', '-S', str(wrapper), str(self.archive),
             self.digest, str(self.scratch)], capture_output=True, check=True, timeout=15,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        result = json.loads(completed.stdout)
        self.assertEqual(result['outcome'], 'complete', result)
        after = inventory()
        changed = sorted(path for path in before.keys() | after.keys()
                         if before.get(path) != after.get(path))
        self.assertEqual(changed, [], 'worker modified copied skill files')

    def test_readwrite_source_refused(self):
        fd = os.open(self.archive, os.O_RDWR)
        try:
            with self.assertRaises(archive_worker.ArchiveWorkerError):
                archive_worker.inspect_isolated(fd, source_path=str(self.archive), sha256=self.digest,
                    scratch_base=str(self.scratch), deadline=time.monotonic() + 1)
        finally:
            os.close(fd)


if __name__ == '__main__':
    unittest.main()
