"""Paired capture exercises real synthetic children, never Maven or network."""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'))
from mdi import maven_executor as maven, operations, raw_capture_consent
from mdi.raw_capture_store import RawCaptureSink, TOOL_IDENTITY
from mdi.runstore import RunStore, RunConflict


class MavenRawCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'project'
        self.repo.mkdir()
        self.pom = self.repo / 'pom.xml'
        self.pom.write_text('<project/>')
        self.store = RunStore.create(self.root)

    def propose(self, code="print('PRIVATE' + '-OUTPUT')", cap=4096, capture=True):
        self.source = maven.maven_proposal(self.store,
            [sys.executable, '-c', code], str(self.repo),
            network_destinations='unknown', input_paths=[self.pom], operation_id='source')
        report = operations.propose(self.store, self.source, self.store.load()['revision'])
        if capture:
            self.capture = raw_capture_consent.build_capture_proposal(report, 'source',
                max_bytes=cap, tool_identity=TOOL_IDENTITY, operation_id='capture')
            operations.propose(self.store, self.capture, report['revision'])
            self.path = Path(self.store.path, self.capture['scope']['output_name'])

    def approval(self, record):
        return dict(run_id=self.store.run_id, operation_id=record['id'],
            scope=copy.deepcopy(record['scope']), approved=True,
            approved_by='synthetic-host', reference='local fixture approval')

    def execute(self, **kwargs):
        return maven.execute_maven(self.store, 'source', self.approval(self.source),
            expected_revision=self.store.load()['revision'], capture_operation_id='capture',
            capture_approval=self.approval(self.capture), **kwargs)

    def test_default_creates_no_raw_file(self):
        self.propose(capture=False)
        result = maven.execute_maven(self.store, 'source', self.approval(self.source),
                                    expected_revision=self.store.load()['revision'])
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(list(Path(self.store.path).glob('raw-*.bin')), [])

    def test_pair_is_durable_before_spawn_and_file_private(self):
        code = "import json,pathlib; p=pathlib.Path(" + repr(self.store.path) + "); r=json.loads(sorted(p.glob('report-*.json'))[-1].read_text()); assert all(o['state']=='consumed' for o in r['operations']); print('PRIVATE'+'-OUTPUT')"
        self.propose(code)
        result = self.execute()
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(self.path.read_bytes(), b'PRIVATE-OUTPUT\n')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['capture']['stored_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertTrue(all(o['state'] == 'finished' for o in self.store.load()['operations']))
        self.assertNotIn('PRIVATE-OUTPUT', json.dumps(self.store.load()))
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())
        with self.assertRaises(operations.OperationsError):
            self.execute()

    def test_capture_is_written_while_child_is_still_running(self):
        code = (
            "import pathlib,time; p=pathlib.Path(" + repr(self.store.path) + "); "
            "print('streamed',flush=True); deadline=time.monotonic()+2\n"
            "while time.monotonic()<deadline and not any(f.stat().st_size for f in p.glob('raw-*.bin')): time.sleep(.01)\n"
            "assert any(f.stat().st_size for f in p.glob('raw-*.bin'))\n"
        )
        self.propose(code)
        result = self.execute()
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(self.path.read_bytes(), b'streamed\n')

    def test_capture_cap_does_not_stop_source(self):
        marker = self.root / 'done'
        self.propose("import pathlib; print('x'*10000); pathlib.Path(" + repr(str(marker)) + ").write_text('done')", cap=7)
        result = self.execute()
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertTrue(marker.exists())
        self.assertEqual(self.path.read_bytes(), b'x'*7)
        self.assertTrue(result['capture']['truncated'])
        self.assertEqual(result['capture']['status'], 'partial')

    def test_pending_capture_cannot_use_unpaired_executor(self):
        self.propose()
        before = self.store.load()
        with self.assertRaises(operations.ApprovalError):
            maven.execute_maven(self.store, 'source', self.approval(self.source), expected_revision=before['revision'])
        self.assertEqual(self.store.load(), before)
        self.assertFalse(self.path.exists())

    def test_either_capture_argument_missing_refuses_before_consume(self):
        self.propose()
        for args in ({'capture_operation_id':'capture'}, {'capture_approval':self.approval(self.capture)}):
            with self.assertRaises(maven.MavenExecutionError):
                maven.execute_maven(self.store, 'source', self.approval(self.source),
                    expected_revision=self.store.load()['revision'], **args)
            self.assertFalse(self.path.exists())
        self.assertTrue(all(o['state']=='proposed' for o in self.store.load()['operations']))

    def test_denied_approval_and_wrong_source_binding_refuse(self):
        self.propose()
        approval = self.approval(self.capture)
        approval['approved'] = False
        with self.assertRaises(operations.ApprovalError):
            maven.execute_maven(self.store, 'source', self.approval(self.source),
                expected_revision=self.store.load()['revision'], capture_operation_id='capture', capture_approval=approval)
        with self.assertRaises(maven.MavenExecutionError):
            maven.execute_maven(self.store, 'different', self.approval(self.source),
                expected_revision=self.store.load()['revision'], capture_operation_id='capture', capture_approval=self.approval(self.capture))
        self.assertFalse(self.path.exists())

    def test_timeout_retains_partial_and_spends_both(self):
        self.propose("import sys,time;print('partial',flush=True);time.sleep(30)")
        result = self.execute(timeout_seconds=.3)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(self.path.read_bytes(), b'partial\n')
        self.assertEqual(result['capture']['status'], 'partial')
        self.assertTrue(all(o['state']=='finished' for o in self.store.load()['operations']))

    def test_output_cap_retains_only_observed_prefix(self):
        self.propose("print('x'*10000)", cap=5000)
        result = self.execute(max_output_bytes=100)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(self.path.stat().st_size, 101)
        self.assertEqual(result['capture']['status'], 'partial')
        self.assertIsNone(result['capture']['stream_sha256'])

    def test_sink_open_failure_never_spawns_and_finalizes_pair(self):
        self.propose()
        with mock.patch.object(RawCaptureSink, 'open', side_effect=OSError('TOPSECRET-OPEN-481')), mock.patch.object(maven, '_run') as run:
            result = self.execute()
        run.assert_not_called()
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['capture']['status'], 'unconfirmed')
        self.assertTrue(all(o['state']=='finished' for o in self.store.load()['operations']))
        self.assertNotIn('TOPSECRET-OPEN-481', json.dumps(result['report']))

    def test_open_directory_fsync_interrupt_preserves_retained_file_metadata(self):
        self.propose()
        interrupt = KeyboardInterrupt('open interrupted after file creation')
        original_fsync = os.fsync
        calls = []
        def interrupt_after_creation(fd):
            if self.path.exists():
                calls.append(fd)
                raise interrupt
            return original_fsync(fd)
        before_fds = len(os.listdir('/proc/self/fd'))
        with mock.patch.object(os, 'fsync', side_effect=interrupt_after_creation), \
             mock.patch.object(maven, '_run') as run:
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.execute()
        self.assertIs(caught.exception, interrupt)
        self.assertEqual(len(calls), 1)
        run.assert_not_called()
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.path.stat().st_size, 0)
        self.assertEqual(interrupt.raw_capture['path'], str(self.path))
        self.assertIs(interrupt.raw_capture['retained'], True)
        self.assertEqual(interrupt.raw_capture['status'], 'unconfirmed')
        self.assertFalse(interrupt.raw_capture['source_stream_complete'])
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())
        self.assertEqual(len(os.listdir('/proc/self/fd')), before_fds)
        self.assertTrue(all(o['state']=='consumed' for o in self.store.load()['operations']))

    def test_open_exception_metadata_is_bounded_and_bound_to_approved_path(self):
        self.propose()
        interrupt = KeyboardInterrupt('untrusted metadata')
        interrupt.raw_capture = {'path': '/unapproved/TOPSECRET', 'retained': True,
                                 'status': 'TOPSECRET', 'extra': 'x' * 100000}
        with mock.patch.object(RawCaptureSink, 'open', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.execute()
        self.assertNotIn('TOPSECRET', json.dumps(interrupt.raw_capture))
        self.assertNotIn('extra', interrupt.raw_capture)
        self.assertLess(len(json.dumps(interrupt.raw_capture)), 1024)

    def test_write_failure_closes_sink_and_never_retries(self):
        self.propose("print('x'*1000)")
        with mock.patch.object(RawCaptureSink, 'write', side_effect=OSError('TOPSECRET-WRITE-482')) as write:
            result = self.execute()
        self.assertEqual(write.call_count, 1)
        self.assertEqual(result['outcome'], 'failed')
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())
        self.assertNotIn('TOPSECRET-WRITE-482', json.dumps(result['report']))

    def test_interrupt_preserved_with_metadata_and_consumed_pair(self):
        self.propose()
        interrupt = KeyboardInterrupt('stop')
        with mock.patch.object(RawCaptureSink, 'write', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.execute()
        self.assertIs(caught.exception, interrupt)
        self.assertEqual(interrupt.raw_capture['status'], 'partial')
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())
        self.assertTrue(all(o['state']=='consumed' for o in self.store.load()['operations']))

    def test_changed_capture_tool_refused_before_consumption(self):
        self.propose()
        with mock.patch.object(maven, 'CAPTURE_TOOL_IDENTITY', {'name':'different-sink'}):
            with self.assertRaises(operations.ApprovalError):
                self.execute()
        self.assertFalse(self.path.exists())
        self.assertTrue(all(o['state']=='proposed' for o in self.store.load()['operations']))

    def test_stale_revision_refuses_pair_without_raw_file(self):
        self.propose()
        with self.assertRaises(RunConflict):
            maven.execute_maven(self.store, 'source', self.approval(self.source),
                expected_revision=self.store.load()['revision'] - 1,
                capture_operation_id='capture', capture_approval=self.approval(self.capture))
        self.assertFalse(self.path.exists())
        self.assertTrue(all(o['state']=='proposed' for o in self.store.load()['operations']))

    def test_both_streams_are_teed_without_promising_write_order(self):
        self.propose("import os;os.write(1,b'OUT');os.write(2,b'ERR')")
        result = self.execute()
        self.assertIn(self.path.read_bytes(), (b'OUTERR', b'ERROUT'))
        self.assertEqual(self.path.stat().st_size, 6)
        self.assertIn('pipe-read order', result['capture']['ordering'])

    def test_publication_conflict_retains_capture_recovery_metadata(self):
        for failing_revision in (3, 4, 5):
            with self.subTest(failing_revision=failing_revision):
                self.store = RunStore.create(self.root)
                self.propose()
                original_commit = self.store.commit
                def conflict(draft, revision):
                    if revision == failing_revision:
                        raise RunConflict('synthetic publication conflict')
                    return original_commit(draft, revision)
                with mock.patch.object(self.store, 'commit', side_effect=conflict):
                    with self.assertRaises(RunConflict) as caught:
                        self.execute()
                metadata = caught.exception.raw_capture
                self.assertEqual(metadata['path'], str(self.path))
                self.assertTrue(metadata['retained'])
                self.assertEqual(metadata['status'], 'complete')
                self.assertEqual(metadata['stored_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
                self.assertFalse(Path(self.store.path, '.writer.lock').exists())
                report = self.store.load()
                self.assertEqual(report['revision'], failing_revision)
                self.assertEqual([o['state'] for o in report['operations']],
                                 ['finished' if failing_revision == 5 else 'consumed', 'consumed'])

    def test_capture_with_injected_runner_refused_before_consumption(self):
        self.propose()
        with self.assertRaises(maven.MavenExecutionError):
            self.execute(runner=lambda *args, **kwargs: None)
        self.assertFalse(self.path.exists())
        self.assertTrue(all(o['state']=='proposed' for o in self.store.load()['operations']))

    def test_close_error_publishes_partial_metadata_after_unlock(self):
        self.propose()
        original = RawCaptureSink.close
        def failing(sink, **kwargs):
            original(sink, **kwargs)
            raise OSError('private detail')
        with mock.patch.object(RawCaptureSink, 'close', failing):
            result = self.execute()
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['capture']['status'], 'partial')
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())


if __name__ == '__main__':
    unittest.main()
