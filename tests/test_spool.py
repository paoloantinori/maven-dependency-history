"""Descriptor-spool capture tests; no archive execution or network."""

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import spool


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / "checkout"
        self.base = self.root / "scratch"
        self.checkout.mkdir()
        self.base.mkdir()
        self.source = self.checkout / "app.jar"
        self.source.write_bytes(b"archive bytes")
        self.fd = os.open(self.source, os.O_RDONLY)
        self.addCleanup(os.close, self.fd)

    def capture(self, **over):
        args = dict(source_path=str(self.source), checkout=self.checkout,
                    scratch_base=self.base, max_bytes=128,
                    deadline=time.monotonic() + 30)
        args.update(over)
        result = spool.capture_to_spool(self.fd, **args)
        self.addCleanup(result.cleanup, worker_stopped=True)
        return result

    def test_regular_source_private_copy_and_json_metadata(self):
        result = self.capture()
        expected = hashlib.sha256(b"archive bytes").hexdigest()
        self.assertTrue(result.complete)
        self.assertEqual(result.sha256, expected)
        self.assertEqual(result.captured_sha256, expected)
        self.assertEqual(result.size, 13)
        self.assertEqual(Path(result.path).read_bytes(), self.source.read_bytes())
        self.assertEqual(stat.S_IMODE(os.stat(result.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(result.scratch_dir).st_mode), 0o700)
        self.assertNotEqual(os.stat(result.path).st_ino, os.fstat(self.fd).st_ino)
        metadata = result.as_dict()
        self.assertNotIn("data", metadata)
        json.dumps(metadata, allow_nan=False)
        cleanup = result.cleanup(worker_stopped=True)
        self.assertEqual(cleanup.status, "complete")
        self.assertFalse(Path(result.scratch_dir).exists())
        self.assertEqual(os.pread(self.fd, 13, 0), b"archive bytes")

    def test_caller_offset_and_descriptor_remain_untouched(self):
        os.lseek(self.fd, 5, os.SEEK_SET)
        result = self.capture()
        self.assertEqual(Path(result.path).read_bytes(), b"archive bytes")
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), 5)
        os.fstat(self.fd)

    def test_source_path_is_a_locator_never_reopened(self):
        self.source.rename(self.checkout / "moved.jar")
        self.source.symlink_to(self.checkout / "missing.jar")
        result = self.capture()
        self.assertEqual(Path(result.path).read_bytes(), b"archive bytes")
        self.assertTrue(result.complete)

    def test_exact_limit_checks_eof(self):
        result = self.capture(max_bytes=13)
        self.assertTrue(result.complete)
        self.assertEqual(result.bytes_read, 13)

    def test_limit_plus_one_is_only_read_as_sentinel(self):
        reads = []
        actual = os.pread
        def read(fd, size, offset):
            data = actual(fd, size, offset)
            reads.append(len(data))
            return data
        with mock.patch.object(spool, "_pread", side_effect=read):
            result = self.capture(max_bytes=5)
        self.assertEqual(sum(reads), 6)
        self.assertEqual(result.bytes_read, 6)
        self.assertEqual(Path(result.path).read_bytes(), b"archi")
        self.assertFalse(result.complete)
        self.assertIsNone(result.sha256)
        self.assertIn("input-truncated", result.reasons)
        self.assertEqual(result.captured_sha256, hashlib.sha256(b"archi").hexdigest())

    def test_reads_are_chunk_bounded(self):
        self.source.write_bytes(b"x" * (200000))
        requests = []
        actual = os.pread
        def read(fd, size, offset):
            requests.append(size)
            return actual(fd, size, offset)
        with mock.patch.object(spool, "_pread", side_effect=read):
            result = self.capture(max_bytes=300000)
        self.assertTrue(result.complete)
        self.assertLessEqual(max(requests), 65536)

    def test_source_mutation_is_qualified(self):
        actual = os.pread
        changed = False
        def read(fd, size, offset):
            nonlocal changed
            data = actual(fd, size, offset)
            if not changed:
                changed = True
                with self.source.open("ab") as stream:
                    stream.write(b" changed")
            return data
        with mock.patch.object(spool, "_pread", side_effect=read):
            result = self.capture()
        self.assertFalse(result.complete)
        self.assertIsNone(result.sha256)
        self.assertIn("source-changed", result.reasons)
        self.assertNotEqual(result.stat_before, result.stat_after)
        self.assertEqual(result.captured_sha256, hashlib.sha256(Path(result.path).read_bytes()).hexdigest())

    def test_expired_deadline_creates_nothing(self):
        with self.assertRaises(spool.SpoolError) as caught:
            self.capture(deadline=time.monotonic() - 1)
        self.assertEqual(caught.exception.code, "deadline-exceeded")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_deadline_during_read_preserves_partial_evidence(self):
        actual = os.pread
        expired = False
        now = time.monotonic()
        def read(fd, size, offset):
            nonlocal expired
            data = actual(fd, size, offset)
            expired = True
            return data
        with mock.patch.object(spool, "_pread", side_effect=read), \
                mock.patch.object(spool, "_monotonic", side_effect=lambda: now + 20 if expired else now):
            result = self.capture(deadline=now + 10)
        self.assertFalse(result.complete)
        self.assertIn("deadline-exceeded", result.reasons)
        self.assertIsNone(result.sha256)
        self.assertLessEqual(result.size, 128)

    def test_invalid_limits_do_not_create_scratch(self):
        for bad in (True, 0, -1, 1.5):
            with self.subTest(max_bytes=bad), self.assertRaises((TypeError, ValueError)):
                self.capture(max_bytes=bad)
        for bad in (True, None, float("nan"), float("inf")):
            with self.subTest(deadline=bad), self.assertRaises((TypeError, ValueError)):
                self.capture(deadline=bad)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_nonregular_source_refused_without_closing_caller(self):
        readfd, writefd = os.pipe()
        try:
            with self.assertRaises(spool.SpoolError) as caught:
                spool.capture_to_spool(readfd, source_path="pipe", checkout=self.checkout,
                                       scratch_base=self.base, max_bytes=10,
                                       deadline=time.monotonic() + 1)
            self.assertEqual(caught.exception.code, "not-regular")
            os.fstat(readfd)
            self.assertEqual(list(self.base.iterdir()), [])
        finally:
            os.close(readfd)
            os.close(writefd)

    def test_checkout_overlap_refused_before_creation(self):
        for base in (self.checkout, self.checkout / "nested", self.root):
            with self.subTest(base=base), self.assertRaises(spool.SpoolError):
                self.capture(scratch_base=base)
        self.assertFalse((self.checkout / "nested").exists())

    def test_symlink_base_and_ancestor_refused(self):
        alias = self.root / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        for base in (alias, alias / "child"):
            with self.subTest(base=base), self.assertRaises(spool.SpoolError):
                self.capture(scratch_base=base)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_symlink_checkout_refused(self):
        alias = self.root / "checkout-alias"
        alias.symlink_to(self.checkout, target_is_directory=True)
        with self.assertRaises(spool.SpoolError):
            self.capture(checkout=alias)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_unknown_worker_termination_retains_scratch(self):
        result = self.capture()
        cleanup = result.cleanup()
        self.assertEqual(cleanup.status, "retained")
        self.assertEqual(len(cleanup.leftovers), 1)
        self.assertEqual(cleanup.leftovers[0].reason, "worker-termination-unconfirmed")
        self.assertTrue(Path(result.path).exists())
        self.assertEqual(result.cleanup(worker_stopped=True), cleanup)
        json.dumps(cleanup.as_dict())
        with self.assertRaises(spool.SpoolError):
            result.open_reader()

    def test_reader_owned_by_caller_is_read_only(self):
        result = self.capture()
        fd = result.open_reader()
        try:
            self.assertEqual(os.read(fd, 100), b"archive bytes")
            with self.assertRaises(OSError):
                os.write(fd, b"x")
        finally:
            os.close(fd)

    def test_replaced_file_is_never_deleted_or_opened(self):
        result = self.capture()
        path = Path(result.path)
        path.rename(path.with_name("original"))
        path.write_bytes(b"someone else")
        with self.assertRaises(spool.SpoolError):
            result.open_reader()
        cleanup = result.cleanup(worker_stopped=True)
        self.assertEqual(cleanup.status, "retained")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"someone else")

    def test_missing_spool_is_not_claimed_as_cleaned(self):
        result = self.capture()
        Path(result.path).unlink()
        cleanup = result.cleanup(worker_stopped=True)
        self.assertEqual(cleanup.status, "retained")
        self.assertTrue(Path(result.scratch_dir).exists())
        self.assertFalse(cleanup.leftovers[0].identity_verified)

    def test_modified_same_inode_is_retained(self):
        result = self.capture()
        Path(result.path).write_bytes(b"changed")
        self.assertEqual(result.cleanup(worker_stopped=True).status, "retained")
        self.assertEqual(Path(result.path).read_bytes(), b"changed")

    def test_cleanup_scans_fresh_description_not_stale_held_directory(self):
        result = self.capture()
        held = result._owner.dir_fd
        actual_scan = os.scandir
        scanned = []
        class EmptyScan:
            def __enter__(self):
                return iter(())
            def __exit__(self, *args):
                pass
        def scan(fd):
            scanned.append(fd)
            return EmptyScan() if fd == held else actual_scan(fd)
        with mock.patch.object(spool.os, "scandir", side_effect=scan):
            cleanup = result.cleanup(worker_stopped=True)
        self.assertEqual(cleanup.status, "complete")
        self.assertTrue(scanned)
        self.assertNotIn(held, scanned)
        self.assertFalse(Path(result.scratch_dir).exists())

    def test_fresh_scan_preserves_primary_error_and_closes_descriptor(self):
        result = self.capture()
        owner = result._owner
        actual_open, actual_close = os.open, os.close
        opened = []
        primary = RuntimeError("scan failed")
        def open_directory(path, *args, **kwargs):
            fd = actual_open(path, *args, **kwargs)
            if path == ".":
                opened.append(fd)
            return fd
        def close(fd):
            actual_close(fd)
            if fd in opened:
                raise OSError("close confirmation failed")
        with mock.patch.object(spool.os, "open", side_effect=open_directory), \
                mock.patch.object(spool.os, "scandir", side_effect=primary), \
                mock.patch.object(spool.os, "close", side_effect=close):
            with self.assertRaises(RuntimeError) as caught:
                owner._entries()
        self.assertIs(caught.exception, primary)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        os.fstat(owner.dir_fd)
        self.assertTrue(Path(result.path).exists())

    def test_fresh_scan_rejects_different_inode_without_deleting(self):
        result = self.capture()
        actual_open = os.open
        def open_directory(path, *args, **kwargs):
            if path == ".":
                return actual_open(self.base, os.O_RDONLY | os.O_DIRECTORY)
            return actual_open(path, *args, **kwargs)
        with mock.patch.object(spool.os, "open", side_effect=open_directory):
            cleanup = result.cleanup(worker_stopped=True)
        self.assertEqual(cleanup.status, "retained")
        self.assertEqual(cleanup.leftovers[0].reason, "cleanup-failed")
        self.assertTrue(Path(result.path).exists())

    def test_cleanup_on_explicit_filesystem_base(self):
        # Optional cross-filesystem coverage uses the same portable mechanism
        # as TemporaryDirectory(dir=...), never a machine-specific home path.
        parent = os.environ.get("MDI_TEST_SPOOL_BASE")
        if parent is None:
            parent = self.root
        with tempfile.TemporaryDirectory(dir=parent) as base:
            result = self.capture(scratch_base=base)
            self.assertTrue(Path(result.path).exists())
            self.assertEqual(result.cleanup(worker_stopped=True).status, "complete")
            self.assertEqual(list(Path(base).iterdir()), [])

    def test_unknown_child_is_never_deleted(self):
        result = self.capture()
        extra = Path(result.scratch_dir) / "user-file"
        extra.write_bytes(b"preserve")
        self.assertEqual(result.cleanup(worker_stopped=True).status, "retained")
        self.assertTrue(extra.exists())
        self.assertTrue(Path(result.path).exists())

    def test_replaced_directory_is_retained(self):
        result = self.capture()
        directory = Path(result.scratch_dir)
        moved = directory.with_name(directory.name + "-moved")
        directory.rename(moved)
        directory.mkdir()
        cleanup = result.cleanup(worker_stopped=True)
        self.assertEqual(cleanup.status, "retained")
        self.assertTrue(directory.exists())
        self.assertTrue(moved.joinpath(Path(result.path).name).exists())

    def test_terminal_cleanup_requires_strict_boolean(self):
        result = self.capture()
        with self.assertRaises(TypeError):
            result.cleanup(worker_stopped=1)
        self.assertTrue(Path(result.path).exists())
        self.assertEqual(result.cleanup(worker_stopped=True).status, "complete")

    def test_uuid_collision_never_adopts_existing_directory(self):
        existing = self.base / "mdi-spool-fixed"
        existing.mkdir()
        marker = existing / "sentinel"
        marker.write_bytes(b"keep")
        with mock.patch.object(spool.uuid, "uuid4", return_value="fixed"):
            with self.assertRaises(spool.SpoolError) as caught:
                self.capture()
        self.assertEqual(caught.exception.cleanup.status, "not-created")
        self.assertEqual(marker.read_bytes(), b"keep")

    def test_missing_initial_directory_identity_retains_partial(self):
        actual = os.stat
        def failed(name, *args, **kwargs):
            if isinstance(name, str) and name.startswith("mdi-spool-"):
                raise OSError("cannot observe ownership")
            return actual(name, *args, **kwargs)
        with mock.patch.object(spool.os, "stat", side_effect=failed):
            with self.assertRaises(spool.SpoolError) as caught:
                self.capture()
        self.assertEqual(caught.exception.cleanup.status, "retained")
        self.assertEqual(len(caught.exception.cleanup.leftovers), 1)
        self.assertEqual(len(list(self.base.iterdir())), 1)

    def test_unlink_failure_is_structured_and_closes_ownership(self):
        result = self.capture()
        with mock.patch.object(spool.os, "unlink", side_effect=PermissionError("refused")):
            cleanup = result.cleanup(worker_stopped=True)
        self.assertEqual(cleanup.status, "retained")
        self.assertEqual(cleanup.leftovers[0].reason, "cleanup-failed")
        self.assertTrue(Path(result.path).exists())
        with self.assertRaises(spool.SpoolError):
            result.open_reader()

    def test_source_read_failure_cleans_only_own_partial(self):
        sentinel = self.base / "existing"
        sentinel.write_bytes(b"unrelated")
        with mock.patch.object(spool, "_pread", side_effect=OSError("read failed")):
            with self.assertRaises(spool.SpoolError) as caught:
                self.capture()
        self.assertEqual(caught.exception.cleanup.status, "complete")
        self.assertEqual(list(self.base.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"unrelated")

    def test_no_safe_platform_support_refuses_before_writes(self):
        with mock.patch.object(spool, "_pread", None):
            with self.assertRaises(spool.SpoolError) as caught:
                self.capture()
        self.assertEqual(caught.exception.code, "unsupported-platform")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_double_slash_spelling_cannot_hide_checkout_overlap(self):
        with self.assertRaises(spool.SpoolError) as caught:
            self.capture(scratch_base="/" + str(self.checkout))
        self.assertEqual(caught.exception.code, "checkout-overlap")

    def test_interrupted_cleanup_is_terminal_after_fd_reuse(self):
        result = self.capture()
        owner = result._owner
        released = owner.base_fd
        interruption = KeyboardInterrupt("cleanup interrupted")
        with mock.patch.object(owner, "_entries", side_effect=interruption):
            with self.assertRaises(KeyboardInterrupt) as caught:
                result.cleanup(worker_stopped=True)
        self.assertIs(caught.exception, interruption)
        replacements = []
        while not replacements or replacements[-1] < released:
            replacements.append(os.open(os.devnull, os.O_RDONLY))
        replacement = replacements[-1]
        try:
            self.assertEqual(replacement, released, "fixture must actually reuse the released fd")
            cleanup = result.cleanup(worker_stopped=True)
            os.fstat(replacement)
            self.assertEqual(cleanup.status, "retained")
            self.assertEqual(cleanup.leftovers[0].reason, "cleanup-interrupted")
            self.assertIsNone(owner.base_fd)
            self.assertIsNone(owner.dir_fd)
            self.assertTrue(Path(result.path).exists())
        finally:
            # The pre-fix implementation closes this unrelated descriptor.
            for fd in replacements:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if owner.result is None:
                owner.result = spool.CleanupResult("retained")

    def test_cleanup_close_failure_preserves_original_interrupt(self):
        result = self.capture()
        owner = result._owner
        directory_fd = owner.dir_fd
        interruption = KeyboardInterrupt("original interrupt")
        actual_close = os.close

        def close(fd):
            actual_close(fd)
            if fd == directory_fd:
                raise OSError("close confirmation unavailable")

        with mock.patch.object(owner, "_entries", side_effect=interruption), \
                mock.patch.object(spool.os, "close", side_effect=close):
            with self.assertRaises(KeyboardInterrupt) as caught:
                result.cleanup(worker_stopped=True)
        self.assertIs(caught.exception, interruption)
        self.assertIsNone(owner.base_fd)
        self.assertIsNone(owner.dir_fd)
        cleanup = result.cleanup(worker_stopped=True)
        self.assertEqual(cleanup.status, "retained")
        self.assertEqual(cleanup.leftovers[0].reason, "descriptor-close-unconfirmed")
        self.assertTrue(Path(result.path).exists())

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'fd inventory needs procfs')
    def test_name_or_owner_creation_failure_does_not_leak_descriptors(self):
        for target in ("uuid", "owner"):
            with self.subTest(target=target):
                opened = []
                roots = spool._roots
                def record_roots(*args):
                    fd = roots(*args)
                    opened.append(fd)
                    return fd
                failure = RuntimeError("construction failed")
                patch = (mock.patch.object(spool.uuid, "uuid4", side_effect=failure)
                         if target == "uuid" else
                         mock.patch.object(spool, "_OwnedSpool", side_effect=failure))
                before = len(list(Path('/proc/self/fd').iterdir()))
                try:
                    with mock.patch.object(spool, "_roots", side_effect=record_roots), patch:
                        with self.assertRaises(RuntimeError) as caught:
                            self.capture()
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)
                    self.assertEqual(list(self.base.iterdir()), [])
                    os.fstat(self.fd)
                finally:
                    # Clean the deliberately reproduced pre-fix leak, if any.
                    for fd in opened:
                        try:
                            os.close(fd)
                        except OSError:
                            pass

    def _close_after_release_fixture(self):
        actual_open, actual_close = os.open, os.close
        targets, recycled = [], []
        def open_file(path, *args, **kwargs):
            fd = actual_open(path, *args, **kwargs)
            if path == "archive.spool":
                targets.append(fd)
            return fd
        def close(fd):
            if targets and fd == targets[0] and not recycled:
                actual_close(fd)
                replacement = actual_open(os.devnull, os.O_RDONLY)
                recycled.append(replacement)
                if replacement != fd:
                    raise AssertionError("fixture did not reuse released descriptor")
                raise OSError("close raised after release")
            actual_close(fd)
        return open_file, close, recycled

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'fd inventory needs procfs')
    def test_capture_close_after_release_retains_accurate_metadata_without_fd_retry(self):
        before = len(list(Path('/proc/self/fd').iterdir()))
        open_file, close, recycled = self._close_after_release_fixture()
        try:
            with mock.patch.object(spool.os, "open", side_effect=open_file), \
                    mock.patch.object(spool.os, "close", side_effect=close):
                with self.assertRaises(spool.SpoolError) as caught:
                    self.capture()
            self.assertEqual(caught.exception.code, "capture-close-unconfirmed")
            self.assertEqual(caught.exception.cleanup.status, "retained")
            self.assertEqual(len(caught.exception.cleanup.leftovers), 1)
            self.assertEqual(caught.exception.cleanup.leftovers[0].reason, "capture-close-unconfirmed")
            self.assertTrue(Path(caught.exception.cleanup.leftovers[0].path).exists())
            os.fstat(recycled[0])
            os.fstat(self.fd)
            self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before + 1)
        finally:
            for fd in recycled:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_source_duplicate_close_error_does_not_lose_spool_cleanup(self):
        actual_dup, actual_close = os.dup, os.close
        duplicated, closed = [], []
        def dup(fd):
            result = actual_dup(fd)
            duplicated.append(result)
            return result
        def close(fd):
            actual_close(fd)
            closed.append(fd)
            if duplicated and fd == duplicated[0]:
                raise OSError("source duplicate close failed after release")
        with mock.patch.object(spool.os, "dup", side_effect=dup), \
                mock.patch.object(spool.os, "close", side_effect=close):
            with self.assertRaises(spool.SpoolError) as caught:
                self.capture()
        self.assertEqual(caught.exception.code, "capture-close-unconfirmed")
        self.assertEqual(caught.exception.cleanup.status, "retained")
        self.assertEqual(closed.count(duplicated[0]), 1)
        self.assertTrue(Path(caught.exception.cleanup.leftovers[0].path).exists())
        os.fstat(self.fd)

    def test_capture_close_error_preserves_primary_read_exception(self):
        open_file, close, recycled = self._close_after_release_fixture()
        primary = KeyboardInterrupt("read interrupted")
        try:
            with mock.patch.object(spool.os, "open", side_effect=open_file), \
                    mock.patch.object(spool.os, "close", side_effect=close), \
                    mock.patch.object(spool, "_pread", side_effect=primary):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.capture()
            self.assertIs(caught.exception, primary)
            os.fstat(recycled[0])
            self.assertTrue(any("close" in note for note in primary.__notes__))
            self.assertTrue(any("scratch retained" in note for note in primary.__notes__))
        finally:
            for fd in recycled:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_pipeline_records_capture_close_retention_without_starting_worker(self):
        from mdi import archive_pipeline
        from mdi.archive_files import FileArchiveLimits
        self.base.chmod(0o700)
        open_file, close, recycled = self._close_after_release_fixture()
        worker = mock.Mock(side_effect=AssertionError("worker must not run"))
        try:
            with mock.patch.object(spool.os, "open", side_effect=open_file), \
                    mock.patch.object(spool.os, "close", side_effect=close):
                result = archive_pipeline.inspect_archive(
                    self.fd, source_path=str(self.source), checkout=self.checkout,
                    scratch_base=self.base, deadline=time.monotonic() + 10,
                    limits=FileArchiveLimits(max_input_bytes=128, max_expanded_bytes=256),
                    worker=worker,
                )
            self.assertEqual(result["outcome"], "failed")
            self.assertIsNone(result["capture"])
            self.assertEqual(result["cleanup_result"]["status"], "retained")
            self.assertEqual(len(result["cleanup_result"]["leftovers"]), 1)
            self.assertIn({"code": "capture-close-unconfirmed"}, result["issues"])
            worker.assert_not_called()
            os.fstat(recycled[0])
        finally:
            for fd in recycled:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_short_writes_preserve_all_bytes(self):
        actual = os.write
        def write(fd, data):
            return actual(fd, data[:2])
        with mock.patch.object(spool, "_write", side_effect=write):
            result = self.capture()
        self.assertTrue(result.complete)
        self.assertEqual(Path(result.path).read_bytes(), b"archive bytes")

    def test_write_failure_removes_verified_owned_partial(self):
        with mock.patch.object(spool, "_write", side_effect=OSError("write failed")):
            with self.assertRaises(spool.SpoolError) as caught:
                self.capture()
        self.assertEqual(caught.exception.cleanup.status, "complete")
        self.assertEqual(list(self.base.iterdir()), [])
        os.fstat(self.fd)

    def test_interrupt_closes_descriptors_and_cleans_partial(self):
        with mock.patch.object(spool, "_pread", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.capture()
        self.assertEqual(list(self.base.iterdir()), [])
        os.fstat(self.fd)

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'fd inventory needs procfs')
    def test_repeated_cleanup_and_retention_do_not_leak_fds(self):
        before = len(list(Path('/proc/self/fd').iterdir()))
        for _ in range(12):
            result = self.capture()
            result.cleanup(worker_stopped=True)
            result.cleanup(worker_stopped=True)
        for _ in range(3):
            self.capture().cleanup(worker_stopped=False)
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)


if __name__ == '__main__':
    unittest.main()
