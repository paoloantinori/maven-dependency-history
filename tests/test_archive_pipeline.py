"""Offline lifecycle tests for descriptor capture plus isolated archive inspection."""
import hashlib
import io
import os
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'maven-dependency-investigator' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import archive_pipeline, archive_worker, spool
from mdi.archive_files import FileArchiveLimits


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.checkout = self.base / 'checkout'
        self.scratch = self.base / 'scratch'
        self.checkout.mkdir()
        self.scratch.mkdir(mode=0o700)
        self.archive = self.checkout / 'app.jar'
        child = io.BytesIO()
        with zipfile.ZipFile(child, 'w') as zf:
            zf.writestr('META-INF/maven/example/lib/pom.properties',
                        'groupId=example\nartifactId=lib\nversion=1\n')
        with zipfile.ZipFile(self.archive, 'w') as zf:
            zf.writestr('BOOT-INF/lib/lib.jar', child.getvalue())
        self.fd = os.open(self.archive, os.O_RDONLY)
        self.addCleanup(os.close, self.fd)

    def run_pipeline(self, **overrides):
        args = dict(source_path=str(self.archive), checkout=str(self.checkout),
                    scratch_base=str(self.scratch), deadline=time.monotonic() + 10)
        args.update(overrides)
        return archive_pipeline.inspect_archive(self.fd, **args)

    def retained(self, size=32):
        directory = self.scratch / ('mdi-spool-' + str(uuid.uuid4()))
        directory.mkdir(mode=0o700)
        path = directory / 'archive.spool'
        with path.open('wb') as stream:
            stream.truncate(size)
        path.chmod(0o600)
        return directory, path

    def test_real_nested_roundtrip_preserves_original_and_caller_offset(self):
        original = self.archive.read_bytes()
        os.lseek(self.fd, 9, os.SEEK_SET)
        result = self.run_pipeline()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['result']['coverage']['status'], 'inspected')
        self.assertEqual(len(result['result']['archives']), 2)
        self.assertEqual(result['capture']['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(result['result']['coverage']['outer_capture']['path'], str(self.archive))
        self.assertEqual(result['cleanup_result']['status'], 'complete')
        self.assertTrue(result['worker_cleanup']['worker_stopped'])
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), 9)
        self.assertEqual(self.archive.read_bytes(), original)

    def test_same_deadline_capture_reader_closed_before_cleanup(self):
        deadline = time.monotonic() + 10
        original_capture = spool.capture_to_spool
        recorded = {}
        def capture(*args, **kwargs):
            recorded['capture_deadline'] = kwargs['deadline']
            value = original_capture(*args, **kwargs)
            recorded['spool'] = value
            return value
        def worker(fd, **kwargs):
            recorded['reader'] = fd
            recorded['worker_deadline'] = kwargs['deadline']
            self.assertEqual(kwargs['source_path'], str(self.archive))
            self.assertNotEqual(fd, self.fd)
            return archive_worker.inspect_isolated(fd, **kwargs)
        original_cleanup = spool.CapturedSpool.cleanup
        def cleanup(value, **kwargs):
            with self.assertRaises(OSError):
                os.fstat(recorded['reader'])
            return original_cleanup(value, **kwargs)
        with mock.patch.object(archive_pipeline, 'capture_to_spool', capture), \
             mock.patch.object(spool.CapturedSpool, 'cleanup', cleanup):
            result = self.run_pipeline(deadline=deadline, worker=worker)
        self.assertEqual(result['outcome'], 'complete')
        self.assertEqual(recorded['capture_deadline'], deadline)
        self.assertEqual(recorded['worker_deadline'], deadline)

    def test_incomplete_capture_never_starts_worker(self):
        worker = mock.Mock()
        result = self.run_pipeline(limits=FileArchiveLimits(max_input_bytes=32), worker=worker)
        self.assertEqual(result['outcome'], 'partial')
        self.assertFalse(result['capture']['complete'])
        self.assertIsNone(result['capture']['sha256'])
        self.assertIsNone(result['result'])
        worker.assert_not_called()
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_expired_budget_creates_nothing(self):
        worker = mock.Mock()
        result = self.run_pipeline(deadline=time.monotonic() - 1, worker=worker)
        self.assertEqual(result['outcome'], 'failed')
        self.assertIn('deadline-exceeded', [x['code'] for x in result['issues']])
        self.assertEqual(list(self.scratch.iterdir()), [])
        worker.assert_not_called()

    def test_unknown_worker_cleanup_retains_spool(self):
        for cleanup in ({}, {'worker_stopped': True},
                        {'worker_stopped': True, 'leader_reaped': False},
                        {'worker_stopped': 1, 'leader_reaped': True}):
            with self.subTest(cleanup=cleanup):
                def worker(*args, **kwargs):
                    return {'outcome': 'failed', 'result': None, 'error': 'test',
                            'cleanup_result': cleanup}
                result = self.run_pipeline(worker=worker)
                self.assertEqual(result['cleanup_result']['status'], 'retained')
                path = Path(result['capture']['path'])
                self.assertTrue(path.exists())
                self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(len(list(self.scratch.iterdir())), 4)

    def test_verified_not_started_failure_can_clean(self):
        def worker(*args, **kwargs):
            return {'outcome': 'failed', 'result': None, 'error': 'deadline-exceeded',
                    'worker_pid': None, 'cleanup_result': {'status': 'not-started',
                    'worker_stopped': True, 'leader_reaped': False}}
        result = self.run_pipeline(worker=worker)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['cleanup_result']['status'], 'complete')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_worker_exception_without_lifecycle_proof_retains_and_does_not_leak_text(self):
        def worker(*args, **kwargs):
            raise ValueError('SECRET-provider-text')
        result = self.run_pipeline(worker=worker)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['cleanup_result']['status'], 'retained')
        self.assertNotIn('SECRET', repr(result))

    def test_interrupt_propagates_with_retained_cleanup(self):
        original = KeyboardInterrupt('interrupt')
        def worker(*args, **kwargs):
            raise original
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_pipeline(worker=worker)
        self.assertIs(caught.exception, original)
        self.assertEqual(original.archive_pipeline_cleanup['status'], 'retained')
        self.assertEqual(len(list(self.scratch.iterdir())), 1)

    def test_confirmed_stopped_interrupt_cleans(self):
        original = KeyboardInterrupt()
        original.archive_cleanup = {'status': 'complete', 'worker_stopped': True,
                                    'leader_reaped': True}
        def worker(*args, **kwargs):
            raise original
        with self.assertRaises(KeyboardInterrupt):
            self.run_pipeline(worker=worker)
        self.assertEqual(original.archive_pipeline_cleanup['status'], 'complete')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_failed_reader_open_has_no_worker_and_cleans(self):
        with mock.patch.object(spool.CapturedSpool, 'open_reader', side_effect=OSError('SECRET')):
            result = self.run_pipeline()
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['cleanup_result']['status'], 'complete')
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertNotIn('SECRET', repr(result))

    def test_quota_reservation_refuses_without_deleting_old_scratch(self):
        directory, path = self.retained(32)
        limit = FileArchiveLimits(max_input_bytes=1024, max_expanded_bytes=1024)
        result = self.run_pipeline(limits=limit, quota_bytes=40)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['issues'][0]['code'], 'scratch-quota-exceeded')
        self.assertEqual(list(self.scratch.iterdir()), [directory])
        self.assertEqual(path.stat().st_size, 32)

    def test_source_growth_before_capture_cannot_exceed_quota_reservation(self):
        with zipfile.ZipFile(self.archive, 'w'):
            pass
        self.assertEqual(self.archive.stat().st_size, 22)
        real_inventory = archive_pipeline._quota_inventory
        real_capture = archive_pipeline.capture_to_spool
        grew = []

        def inventory_after_growth(*args, **kwargs):
            # _validate already observed the 22-byte file. Mutate that same
            # inode before quota admission/capture, without replacing the fd.
            with zipfile.ZipFile(self.archive, 'w') as archive:
                archive.comment = b'x' * 2078
            grew.append(self.archive.stat().st_size)
            return real_inventory(*args, **kwargs)

        limits = FileArchiveLimits(max_input_bytes=4096, max_expanded_bytes=1024)
        worker = mock.Mock()
        with mock.patch.object(archive_pipeline, '_quota_inventory', inventory_after_growth), \
             mock.patch.object(archive_pipeline, 'capture_to_spool', wraps=real_capture) as capture:
            result = self.run_pipeline(limits=limits, quota_bytes=1500, worker=worker)
        self.assertEqual(grew, [2100])
        self.assertEqual(result['issues'][0]['code'], 'scratch-quota-exceeded')
        self.assertEqual(result['quota']['reserved_bytes'], 5120)
        self.assertIsNone(result['capture'])
        capture.assert_not_called()
        worker.assert_not_called()
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_source_growth_with_full_reservation_can_be_captured(self):
        with zipfile.ZipFile(self.archive, 'w'):
            pass
        real_capture = archive_pipeline.capture_to_spool

        def capture_after_growth(*args, **kwargs):
            with zipfile.ZipFile(self.archive, 'w') as archive:
                archive.comment = b'x' * 2078
            return real_capture(*args, **kwargs)

        limits = FileArchiveLimits(max_input_bytes=4096, max_expanded_bytes=1024)
        with mock.patch.object(archive_pipeline, 'capture_to_spool', capture_after_growth):
            result = self.run_pipeline(limits=limits, quota_bytes=5120)
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['capture']['size'], 2100)
        self.assertEqual(result['quota']['reserved_bytes'], 5120)
        self.assertLessEqual(result['capture']['size'] + limits.max_expanded_bytes,
                             result['quota']['reserved_bytes'])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_quota_unknown_directory_refuses_no_traversal_or_delete(self):
        directory = self.scratch / 'unrelated'
        directory.mkdir()
        marker = directory / 'keep'
        marker.write_text('unchanged')
        result = self.run_pipeline()
        self.assertEqual(result['issues'][0]['code'], 'scratch-quota-unavailable')
        self.assertEqual(marker.read_text(), 'unchanged')
        self.assertEqual(list(self.scratch.iterdir()), [directory])

    def test_quota_symlink_refuses_without_following(self):
        link = self.scratch / ('mdi-spool-' + str(uuid.uuid4()))
        link.symlink_to(self.checkout, target_is_directory=True)
        result = self.run_pipeline()
        self.assertEqual(result['issues'][0]['code'], 'scratch-quota-unavailable')
        self.assertTrue(link.is_symlink())

    def test_quota_entry_scan_bounded(self):
        self.retained()
        self.retained()
        result = self.run_pipeline(max_quota_entries=1)
        self.assertEqual(result['issues'][0]['code'], 'scratch-quota-unavailable')
        self.assertEqual(len(list(self.scratch.iterdir())), 2)

    def test_quota_close_failure_releases_other_handles_without_retry(self):
        real_open = archive_pipeline.open_checkout_fd
        real_close = os.close
        roots, replacement, closed, recycled_at = {}, [], [], []

        def opening(path):
            fd = real_open(path)
            roots[str(path)] = fd
            return fd

        def closing(fd):
            closed.append(fd)
            real_close(fd)
            if fd == roots.get(str(self.scratch)) and not replacement:
                while not replacement or replacement[-1] < fd:
                    replacement.append(os.open(os.devnull, os.O_RDONLY))
                self.assertEqual(replacement[-1], fd)
                recycled_at.append(len(closed))
                raise OSError('close outcome uncertain')

        worker = mock.Mock()
        try:
            with mock.patch.object(archive_pipeline, 'open_checkout_fd', opening), \
                 mock.patch.object(archive_pipeline.os, 'close', closing):
                result = self.run_pipeline(worker=worker)
            self.assertEqual(result['issues'][0]['code'], 'scratch-quota-unavailable')
            self.assertEqual(result['quota']['cleanup_status'], 'unconfirmed')
            worker.assert_not_called()
            os.fstat(replacement[-1])
            self.assertNotIn(replacement[-1], closed[recycled_at[0]:])
            with self.assertRaises(OSError):
                os.fstat(roots[str(self.checkout)])
            self.assertEqual(list(self.scratch.iterdir()), [])
        finally:
            for fd in replacement:
                real_close(fd)
            root = roots.get(str(self.checkout))
            if root is not None:
                try:
                    os.fstat(root)
                except OSError:
                    pass
                else:
                    real_close(root)

    def test_quota_child_close_error_preserves_original_interrupt(self):
        directory, _path = self.retained()
        real_scandir, real_close = os.scandir, os.close
        identity = (directory.stat().st_dev, directory.stat().st_ino)
        interrupted = KeyboardInterrupt('quota scan interrupted')
        child, replacement, closed = [], [], []
        before = len(list(Path('/proc/self/fd').iterdir())) if Path('/proc/self/fd').exists() else None

        def scanning(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == identity:
                child.append(fd)
                raise interrupted
            return real_scandir(fd)

        def closing(fd):
            closed.append(fd)
            real_close(fd)
            if child and fd == child[0] and not replacement:
                replacement.append(os.open(os.devnull, os.O_RDONLY))
                self.assertEqual(replacement[0], fd)
                raise OSError('secondary close failure')

        try:
            with mock.patch.object(archive_pipeline.os, 'scandir', scanning), \
                 mock.patch.object(archive_pipeline.os, 'close', closing):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.run_pipeline()
            self.assertIs(caught.exception, interrupted)
            self.assertTrue(any('quota descriptor close' in note for note in interrupted.__notes__))
            os.fstat(replacement[0])
            self.assertEqual(closed.count(replacement[0]), 1)
        finally:
            for fd in replacement:
                real_close(fd)
        if before is not None:
            self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)
        self.assertTrue(directory.is_dir())

    def test_outside_checkout_validated_before_creation(self):
        inside = self.checkout / 'scratch'
        with self.assertRaises(archive_pipeline.ArchivePipelineError):
            self.run_pipeline(scratch_base=inside)
        self.assertFalse(inside.exists())

    def test_writable_source_refused(self):
        fd = os.open(self.archive, os.O_RDWR)
        try:
            with self.assertRaises(archive_pipeline.ArchivePipelineError):
                archive_pipeline.inspect_archive(fd, source_path=str(self.archive),
                    checkout=str(self.checkout), scratch_base=str(self.scratch),
                    deadline=time.monotonic() + 10)
        finally:
            os.close(fd)

    def test_real_timeout_retains_no_running_worker(self):
        script = self.base / 'slow.py'
        script.write_text('import time\ntime.sleep(30)\n')
        def worker(fd, **kwargs):
            return archive_worker.inspect_isolated(fd, **kwargs,
                worker_command=[sys.executable, '-I', '-S', str(script)])
        result = self.run_pipeline(worker=worker, deadline=time.monotonic() + .2)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['worker_cleanup']['worker_stopped'], True)
        self.assertEqual(result['cleanup_result']['status'], 'complete')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_capture_interrupt_without_result_reports_unknown_cleanup(self):
        original = KeyboardInterrupt()
        with mock.patch.object(archive_pipeline, 'capture_to_spool', side_effect=original):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_pipeline()
        self.assertIs(caught.exception, original)
        self.assertEqual(original.archive_pipeline_cleanup['status'], 'unconfirmed')

    def test_cleanup_error_cannot_mask_worker_interrupt(self):
        original = KeyboardInterrupt('original')
        def worker(*args, **kwargs):
            raise original
        real_cleanup = spool.CapturedSpool.cleanup
        def interrupted_cleanup(value, **kwargs):
            real_cleanup(value, **kwargs)
            raise OSError('secondary')
        with mock.patch.object(spool.CapturedSpool, 'cleanup', interrupted_cleanup):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_pipeline(worker=worker)
        self.assertIs(caught.exception, original)
        self.assertEqual(original.archive_pipeline_cleanup['status'], 'retained')
        self.assertTrue(list(self.scratch.iterdir()))

    def test_empty_paths_rejected_before_work(self):
        for key in ('source_path', 'checkout', 'scratch_base'):
            with self.subTest(key=key), self.assertRaises(archive_pipeline.ArchivePipelineError):
                self.run_pipeline(**{key: ''})
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_repeated_roundtrips_do_not_leak_descriptors(self):
        fd_dir = Path('/proc/self/fd')
        if not fd_dir.exists():
            self.skipTest('descriptor inventory requires procfs')
        before = len(list(fd_dir.iterdir()))
        for _ in range(3):
            self.assertEqual(self.run_pipeline()['outcome'], 'complete')
        self.assertEqual(len(list(fd_dir.iterdir())), before)


if __name__ == '__main__':
    unittest.main()
