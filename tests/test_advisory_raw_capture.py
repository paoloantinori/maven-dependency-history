"""Paired advisory capture uses local RunStore files and in-memory transport only."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from mdi import advisory_executor as executor, operations, raw_capture_consent, raw_capture_store
from mdi.runstore import RunStore


class AdvisoryRawCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunStore.create(self.tmp.name, query='example:lib:1')
        self.source = executor.advisory_proposal(self.store, 'example', 'lib', '1')
        operations.propose(self.store, self.source, 0)

    def approve(self, record):
        return dict(run_id=self.store.run_id, operation_id=record['id'],
                    scope=copy.deepcopy(record['scope']), approved=True,
                    approved_by='fixture', reference='synthetic test consent')

    def register(self, cap=1024, tool=None):
        self.capture = raw_capture_consent.build_capture_proposal(
            self.store.load(), self.source['id'], max_bytes=cap,
            tool_identity=tool or raw_capture_store.TOOL_IDENTITY)
        operations.propose(self.store, self.capture, self.store.load()['revision'])
        return Path(self.store.path) / self.capture['scope']['output_name']

    def execute(self, body=b'{}', error=None, **kwargs):
        case = self
        class Transport:
            def post(self, *args):
                case.calls += 1
                report = case.store.load()
                case.states = [op['state'] for op in report['operations']]
                if error is not None:
                    raise error
                return body
        self.calls = 0
        return executor.execute_advisory(
            self.store, self.source['id'], self.approve(self.source),
            expected_revision=self.store.load()['revision'], transport=Transport(), **kwargs)

    def paired(self, **kwargs):
        return self.execute(capture_operation_id=self.capture['id'],
                            capture_approval=self.approve(self.capture), **kwargs)

    def test_default_has_no_raw_file(self):
        self.execute()
        self.assertEqual(list(Path(self.store.path).glob('raw-*.bin')), [])

    def test_pair_consumed_before_post_and_both_finished(self):
        path = self.register()
        result = self.paired(body=b'{ "vulns": [] }')
        self.assertEqual(self.states, ['consumed', 'consumed'])
        self.assertEqual(path.read_bytes(), b'{ "vulns": [] }')
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(result['capture']['status'], 'complete')
        self.assertEqual(result['capture']['source_bytes'], 15)
        self.assertEqual([o['state'] for o in self.store.load()['operations']], ['finished', 'finished'])
        self.assertNotIn('{ "vulns": [] }', json.dumps(result))
        with self.assertRaises(operations.OperationsError):
            self.paired()
        self.assertEqual(self.calls, 0)

    def test_missing_denied_and_sibling_capture_approval_refuse_before_post(self):
        path = self.register()
        revision = self.store.load()['revision']
        for override in ({'capture_operation_id': self.capture['id']},
                         {'capture_approval': self.approve(self.capture)},
                         {'capture_operation_id': self.capture['id'],
                          'capture_approval': dict(self.approve(self.capture), approved=False)},
                         {'capture_operation_id': self.capture['id'],
                          'capture_approval': self.approve(self.source)}):
            with self.subTest(override=override):
                with self.assertRaises((executor.AdvisoryExecutionError, operations.OperationsError)):
                    self.execute(**override)
                self.assertFalse(path.exists())
                self.assertEqual(self.calls, 0)
                self.assertEqual(self.store.load()['revision'], revision)

    def test_wrong_capture_tool_refuses_before_consumption(self):
        path = self.register(tool={'name': 'other'})
        with self.assertRaises(operations.ApprovalError):
            self.paired()
        self.assertFalse(path.exists())
        self.assertEqual(self.store.load()['revision'], 2)
        self.assertEqual(self.calls, 0)

    def test_pending_capture_cannot_use_default_source_execution(self):
        path = self.register()
        with self.assertRaises(operations.ApprovalError):
            self.execute()
        self.assertFalse(path.exists())
        self.assertEqual(self.calls, 0)

    def test_capture_cap_retains_prefix_not_full_stream_digest(self):
        path = self.register(cap=2)
        result = self.paired(body=b'{ "vulns": [] }')
        self.assertEqual(path.read_bytes(), b'{ ')
        self.assertTrue(result['capture']['truncated'])
        self.assertEqual(result['capture']['status'], 'partial')
        self.assertIsNone(result['capture']['stream_sha256'])
        self.assertEqual(result['outcome'], 'succeeded')

    def test_invalid_json_retained_exactly_without_leaking_body(self):
        path = self.register()
        body = b'SECRET-invalid-response'
        result = self.paired(body=body)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(result['capture']['status'], 'complete')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['capture_outcome'], 'succeeded')

    def test_oversized_injected_body_is_captured_only_within_transport_bound(self):
        path = self.register()
        result = self.paired(body=b'x' * 40, limits=executor.AdvisoryLimits(response_bytes=16))
        self.assertEqual(path.read_bytes(), b'x' * 16)
        self.assertEqual(result['outcome'], 'failed')
        self.assertFalse(result['capture']['source_complete'])
        self.assertEqual(result['capture']['status'], 'partial')
        self.assertEqual(result['capture']['source_bytes'], 40)
        self.assertEqual(result['capture_outcome'], 'failed')

    def test_capture_bound_to_other_source_cannot_authorize_this_one(self):
        other = executor.advisory_proposal(self.store, 'example', 'other', '1')
        operations.propose(self.store, other, self.store.load()['revision'])
        capture = raw_capture_consent.build_capture_proposal(
            self.store.load(), other['id'], max_bytes=1024,
            tool_identity=raw_capture_store.TOOL_IDENTITY)
        operations.propose(self.store, capture, self.store.load()['revision'])
        before = self.store.load()
        with self.assertRaises(operations.ApprovalError):
            self.execute(capture_operation_id=capture['id'], capture_approval=self.approve(capture))
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.store.load(), before)

    def test_transport_failure_empty_capture_is_not_complete(self):
        path = self.register()
        result = self.paired(error=TimeoutError('SECRET'))
        self.assertEqual(path.read_bytes(), b'')
        self.assertEqual(result['capture']['status'], 'partial')
        self.assertFalse(result['capture']['source_complete'])
        self.assertEqual(result['capture_outcome'], 'failed')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_interrupt_preserved_sink_closed_and_consent_spent(self):
        path = self.register()
        original = KeyboardInterrupt('fixture')
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.paired(error=original)
        self.assertIs(caught.exception, original)
        self.assertEqual(original.raw_capture['status'], 'partial')
        self.assertEqual(path.read_bytes(), b'')
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())
        self.assertEqual([o['state'] for o in self.store.load()['operations']], ['consumed', 'consumed'])

    def test_interrupt_survives_secondary_sink_close_error(self):
        self.register()
        original = KeyboardInterrupt('primary')
        close = executor.RawCaptureSink.close
        def failed_close(sink, **kwargs):
            metadata = close(sink, **kwargs)
            raise raw_capture_store.RawCaptureError('secondary', metadata)
        with mock.patch.object(executor.RawCaptureSink, 'close', failed_close):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.paired(error=original)
        self.assertIs(caught.exception, original)
        self.assertIsNotNone(original.raw_capture)
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())

    def test_evidence_commit_failure_keeps_capture_metadata_and_spent_pair(self):
        path = self.register()
        commit = self.store.commit
        calls = []
        def fail_evidence(draft, revision):
            calls.append(revision)
            if len(calls) == 2:
                raise RuntimeError('publication failed')
            return commit(draft, revision)
        with mock.patch.object(self.store, 'commit', side_effect=fail_evidence):
            with self.assertRaises(RuntimeError) as caught:
                self.paired()
        self.assertEqual(caught.exception.raw_capture['path'], str(path))
        self.assertEqual([o['state'] for o in self.store.load()['operations']], ['consumed', 'consumed'])
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())

    def test_sink_open_failure_does_not_start_transport(self):
        self.register()
        with mock.patch.object(executor.RawCaptureSink, 'open', side_effect=raw_capture_store.RawCaptureError('SECRET')):
            result = self.paired()
        self.assertEqual(self.calls, 0)
        self.assertEqual(result['capture_outcome'], 'failed')
        self.assertEqual(result['outcome'], 'failed')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_sink_write_failure_keeps_received_advisory_and_continuation(self):
        self.register()
        body = json.dumps({'vulns': [{
            'id': 'TEST-LOCAL-1',
            'affected': [{'package': {'ecosystem': 'Maven', 'name': 'example:lib'},
                          'versions': ['1']}],
        }], 'next_page_token': 'next-page'}).encode()
        with mock.patch.object(executor.RawCaptureSink, 'write', side_effect=OSError('SECRET')):
            result = self.paired(body=body)
        self.assertEqual(self.calls, 1)
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(result['capture_outcome'], 'failed')
        self.assertEqual(result['next_page_token'], 'next-page')
        self.assertIn('TEST-LOCAL-1', json.dumps(result['result']))
        self.assertNotIn('transport-error', json.dumps(result['result']))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())

    def test_sink_write_failure_preserves_partial_and_unlocks(self):
        self.register()
        with mock.patch.object(executor.RawCaptureSink, 'write', side_effect=OSError('SECRET')):
            result = self.paired()
        self.assertEqual(result['capture_outcome'], 'failed')
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(result['result']['status'], 'no-known-matches')
        evidence = next(e for e in result['report']['evidence'] if e['id'] == result['evidence_id'])
        self.assertEqual(evidence['category'], 'advisory-page')
        self.assertNotIn('failure', evidence['payload'])
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())
        self.assertNotIn('SECRET', json.dumps(result))

    def test_sink_close_error_preserves_metadata_and_unlocks(self):
        self.register()
        close = executor.RawCaptureSink.close
        def failed_close(sink, **kwargs):
            metadata = close(sink, **kwargs)
            raise raw_capture_store.RawCaptureError('SECRET', metadata)
        with mock.patch.object(executor.RawCaptureSink, 'close', failed_close):
            result = self.paired()
        self.assertEqual(result['capture_outcome'], 'failed')
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(result['result']['status'], 'no-known-matches')
        evidence = next(e for e in result['report']['evidence'] if e['id'] == result['evidence_id'])
        self.assertEqual(evidence['category'], 'advisory-page')
        self.assertNotIn('failure', evidence['payload'])
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())
        self.assertNotIn('SECRET', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
