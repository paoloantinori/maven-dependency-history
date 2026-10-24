"""Raw capture files require durable paired consent, including on historical loads."""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from mdi import operations, raw_capture_consent
from mdi.runstore import RunStore, RunStoreError
from mdi.raw_capture_store import RawCaptureSink, RawCaptureError, TOOL_IDENTITY


class RawCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunStore.create(self.tmp.name, query='example:lib')
        self.source = operations.build_proposal('maven',
            {'kind': 'maven', 'argv': ['/synthetic/mvn'], 'cwd': '/synthetic'},
            [], {'name': 'synthetic'}, 'No execution in this test.', self.store.run_id, 'source')
        report = operations.propose(self.store, self.source, 0)
        self.capture = raw_capture_consent.build_capture_proposal(report, 'source',
            max_bytes=4, tool_identity=TOOL_IDENTITY, operation_id='capture')
        self.report = operations.propose(self.store, self.capture, report['revision'])
        self.path = Path(self.store.path, self.capture['scope']['output_name'])

    def approve(self, op):
        return dict(run_id=self.store.run_id, operation_id=op['id'], scope=op['scope'],
                    approved=True, approved_by='synthetic-host', reference='local test')

    def consume(self):
        return raw_capture_consent.consume_pair(self.store, 'capture', self.approve(self.source),
            self.approve(self.capture), observed_source_inputs=[],
            observed_source_tool=self.source['tool_identity'], observed_capture_inputs=[],
            observed_capture_tool=TOOL_IDENTITY, expected_revision=self.report['revision'])

    def test_pending_and_denied_create_nothing(self):
        for state in ('pending', 'denied'):
            if state == 'denied':
                self.report = operations.deny_proposal(self.store, 'capture', 'no', self.report['revision'])
            with self.assertRaises(RawCaptureError):
                RawCaptureSink.open(self.store, 'capture', expected_revision=self.report['revision'])
            self.assertFalse(self.path.exists())

    def test_bounded_capture_load_latest_history_and_no_replay(self):
        consumed = self.consume()
        with RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision']) as sink:
            self.assertEqual(sink.write(b'ab'), 2)
            self.assertEqual(sink.write(b'cdef'), 2)
            self.assertEqual(self.store.load()['revision'], consumed['revision'])
            self.assertEqual(self.store.load(0)['revision'], 0)
        result = sink.result
        self.assertEqual(self.path.read_bytes(), b'abcd')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['stored_sha256'], hashlib.sha256(b'abcd').hexdigest())
        self.assertTrue(result['truncated'])
        self.assertIsNone(result['stream_sha256'])
        self.assertEqual(RunStore.open(self.store.path).load()['revision'], consumed['revision'])
        with self.assertRaises(RawCaptureError):
            RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision'])
        self.assertEqual(self.path.read_bytes(), b'abcd')

    def test_exact_cap_and_finished_retention(self):
        consumed = self.consume()
        with RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision']) as sink:
            sink.write(b'abcd')
        self.assertFalse(sink.result['truncated'])
        report = operations.finalize_operation(self.store, 'source', 'succeeded',
                                              expected_revision=consumed['revision'])
        report = operations.finalize_operation(self.store, 'capture', 'succeeded',
                                              expected_revision=report['revision'])
        self.assertEqual(self.store.load(0)['revision'], 0)
        with self.assertRaises(RawCaptureError):
            RawCaptureSink.open(self.store, 'capture', expected_revision=report['revision'])

    def test_missing_output_after_consumption_is_valid(self):
        consumed = self.consume()
        self.assertEqual(self.store.load()['revision'], consumed['revision'])
        self.assertFalse(self.path.exists())

    def test_unregistered_or_pending_raw_file_rejected(self):
        self.path.write_bytes(b'x'); self.path.chmod(0o600)
        with self.assertRaises(RunStoreError): self.store.load()
        with self.assertRaises(RunStoreError): self.store.load(0)

    def test_different_consumption_revisions_rejected_even_same_timestamp(self):
        draft = self.store.load()
        draft['operations'][0]['state'] = 'consumed'
        started = dict(at='2026-09-12T00:00:00Z', approved_by='synthetic', reference='test')
        draft['operations'][0]['audit']['started'] = started
        report = self.store.commit(draft, draft['revision'])
        report['operations'][1]['state'] = 'consumed'
        report['operations'][1]['audit']['started'] = started
        self.store.commit(report, report['revision'])
        self.path.write_bytes(b'x'); self.path.chmod(0o600)
        with self.assertRaises(RunStoreError): self.store.load()

    def test_symlink_fifo_hardlink_modes_and_overcap_rejected_without_read(self):
        self.consume()
        for kind in ('symlink', 'fifo', 'hardlink', 'mode', 'oversized'):
            with self.subTest(kind=kind):
                outside = Path(self.tmp.name, 'outside')
                outside.write_bytes(b'x'); outside.chmod(0o600)
                if kind == 'symlink': self.path.symlink_to(outside)
                elif kind == 'fifo': os.mkfifo(self.path, 0o600)
                elif kind == 'hardlink': os.link(outside, self.path)
                else:
                    self.path.write_bytes(b'12345' if kind == 'oversized' else b'x')
                    self.path.chmod(0o644 if kind == 'mode' else 0o600)
                with self.assertRaises(RunStoreError): self.store.load()
                self.path.unlink()

    def test_interrupt_retains_bytes_and_closes_owned_handles(self):
        consumed = self.consume()
        original = KeyboardInterrupt('synthetic')
        with self.assertRaises(KeyboardInterrupt) as caught:
            with RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision']) as sink:
                sink.write(b'ab')
                raise original
        self.assertIs(caught.exception, original)
        self.assertEqual(sink.result['status'], 'partial')
        self.assertEqual(self.path.read_bytes(), b'ab')
        self.store.load()
        with self.assertRaises(RawCaptureError): sink.write(b'c')

    def test_short_writes_and_failure_preserve_primary(self):
        consumed = self.consume()
        real_write = os.write
        with RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision']) as sink:
            with mock.patch('mdi.raw_capture_store.os.write', side_effect=lambda fd, data: real_write(fd, data[:1])):
                sink.write(b'abcd')
        self.assertEqual(self.path.read_bytes(), b'abcd')

    def test_load_does_not_open_raw_contents(self):
        consumed = self.consume()
        with RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision']) as sink:
            sink.write(b'ab')
        real_open = os.open
        def open_guard(name, *args, **kwargs):
            if name == self.path.name:
                raise AssertionError('raw bytes must not be opened by load')
            return real_open(name, *args, **kwargs)
        with mock.patch('mdi.runstore.os.open', open_guard), mock.patch.object(
                os, 'supports_dir_fd', os.supports_dir_fd | {open_guard}):
            self.store.load(0)
            self.store.load()

    def test_fsync_failure_reports_retention_and_no_replay(self):
        consumed = self.consume()
        sink = RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision'])
        sink.write(b'ab')
        with mock.patch('mdi.raw_capture_store.os.fsync', side_effect=OSError('synthetic')):
            with self.assertRaises(RawCaptureError) as caught: sink.close()
        self.assertTrue(caught.exception.capture['retained'])
        self.assertEqual(caught.exception.capture['durability'], 'unconfirmed')
        self.assertEqual(self.path.read_bytes(), b'ab')
        self.store.load()
        with self.assertRaises(RawCaptureError):
            RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision'])

    def test_started_audits_cannot_be_added_after_consumption(self):
        report = self.store.load()
        for op in report['operations']: op['state'] = 'consumed'
        report = self.store.commit(report, report['revision'])
        for op in report['operations']:
            op['audit']['started'] = dict(at='same', approved_by='synthetic', reference='test')
        self.store.commit(report, report['revision'])
        self.path.write_bytes(b'x'); self.path.chmod(0o600)
        with self.assertRaises(RunStoreError): self.store.load()

    def test_wrong_tool_refuses_without_file(self):
        consumed = self.consume()
        with mock.patch('mdi.raw_capture_store.TOOL_IDENTITY', {'name': 'other'}):
            with self.assertRaises(RawCaptureError):
                RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision'])
        self.assertFalse(self.path.exists())

    def test_replacement_is_not_written_or_deleted(self):
        consumed = self.consume()
        sink = RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision'])
        old = Path(self.tmp.name, 'original')
        self.path.rename(old)
        self.path.write_bytes(b'new'); self.path.chmod(0o600)
        with self.assertRaises(RawCaptureError): sink.write(b'X')
        with self.assertRaises(RawCaptureError): sink.close()
        self.assertEqual(self.path.read_bytes(), b'new')
        self.assertEqual(old.read_bytes(), b'')
        self.assertIsNone(sink.result['stored_sha256'])

    def test_same_size_external_rewrite_invalidates_digest(self):
        consumed = self.consume()
        sink = RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision'])
        sink.write(b'ab')
        self.path.write_bytes(b'cd')
        with self.assertRaises(RawCaptureError): sink.close()
        self.assertIsNone(sink.result['stored_sha256'])
        self.assertEqual(self.path.read_bytes(), b'cd')

    def test_primary_interrupt_survives_fsync_failure(self):
        consumed = self.consume()
        original = KeyboardInterrupt('original')
        with self.assertRaises(KeyboardInterrupt) as caught:
            with RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision']) as sink:
                sink.write(b'a')
                with mock.patch('mdi.raw_capture_store.os.fsync', side_effect=OSError('synthetic')):
                    try:
                        raise original
                    finally:
                        sink.close(interrupted=True)
        self.assertIs(caught.exception, original)
        self.assertEqual(sink.result['durability'], 'unconfirmed')
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())

    def test_output_close_failure_does_not_retry_recycled_descriptor(self):
        consumed = self.consume()
        sink = RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision'])
        sink.write(b'ab')
        original_close = os.close
        target = sink.output_fd
        replacement = []
        def close(fd):
            original_close(fd)
            if fd == target and not replacement:
                new = os.open(os.devnull, os.O_RDONLY)
                if new != target:
                    os.dup2(new, target); original_close(new)
                replacement.append(target)
                raise OSError('closed then failed')
        try:
            with mock.patch('mdi.raw_capture_store.os.close', side_effect=close):
                with self.assertRaises(RawCaptureError): sink.close()
            os.fstat(target)
            self.assertEqual(sink.close()['status'], 'partial')
            os.fstat(target)
        finally:
            if replacement: original_close(target)
        self.assertFalse(Path(self.store.path, '.writer.lock').exists())

    def test_sink_holds_writer_lock_until_close(self):
        consumed = self.consume()
        with RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision']):
            from mdi.runstore import RunConflict
            report = self.store.load()
            with self.assertRaises(RunConflict): self.store.commit(report, report['revision'])
        report = self.store.load()
        self.store.commit(report, report['revision'])

    def test_historical_read_with_raw_validates_latest_chain(self):
        consumed = self.consume()
        with RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision']):
            pass
        Path(self.store.path, f'report-{consumed["revision"]:08d}.json').write_text('broken')
        with self.assertRaises(RunStoreError): self.store.load(0)

    def test_unknown_raw_file_rejected_even_with_valid_pair(self):
        self.consume()
        other = Path(self.store.path, 'raw-12345678-1234-1234-1234-123456789abc.bin')
        other.write_bytes(b'a'); other.chmod(0o600)
        with self.assertRaises(RunStoreError): self.store.load()

    def test_stale_revision_does_not_create_file(self):
        consumed = self.consume()
        with self.assertRaises(RawCaptureError):
            RawCaptureSink.open(self.store, 'capture', expected_revision=consumed['revision'] - 1)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
