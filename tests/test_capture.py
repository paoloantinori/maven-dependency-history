"""Tests for mdi.capture bounded file capture and comparison."""

import contextlib
import hashlib
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.capture import CaptureError, capture_file, compare_captures


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, name, data):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    @contextlib.contextmanager
    def cwd_at(self, path):
        previous = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)


class LimitValidationTest(CaptureTestCase):
    def test_non_positive_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                capture_file(self.tmp / "x", bad)

    def test_bool_and_non_int_rejected(self):
        for bad in (True, False, "16", 1.0, None):
            with self.assertRaises(TypeError, msg=repr(bad)):
                capture_file(self.tmp / "x", bad)


class RejectionTest(CaptureTestCase):
    def test_missing_file_fails_clearly(self):
        with self.assertRaises(CaptureError) as ctx:
            capture_file(self.tmp / "nope.bin", 16)
        self.assertIn("missing", str(ctx.exception))

    def test_symlink_rejected_not_followed(self):
        target = self.write("target.bin", b"real payload")
        link = self.tmp / "link.bin"
        link.symlink_to(target)
        with self.assertRaises(CaptureError) as ctx:
            capture_file(link, 16)
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(target.read_bytes(), b"real payload")

    def test_dangling_symlink_rejected(self):
        link = self.tmp / "dangling.bin"
        link.symlink_to(self.tmp / "gone.bin")
        with self.assertRaises(CaptureError) as ctx:
            capture_file(link, 16)
        self.assertIn("symlink", str(ctx.exception))

    def test_directory_rejected(self):
        with self.assertRaises(CaptureError) as ctx:
            capture_file(self.tmp, 16)
        self.assertIn("not a regular file", str(ctx.exception))

    def test_fifo_rejected_without_blocking(self):
        fifo = self.tmp / "pipe.fifo"
        os.mkfifo(fifo)
        with self.assertRaises(CaptureError) as ctx:
            capture_file(fifo, 16)
        self.assertIn("not a regular file", str(ctx.exception))

    def test_symlinked_parent_followed_final_component_only(self):
        real = self.tmp / "realdir"
        real.mkdir()
        (real / "leaf.bin").write_bytes(b"leaf payload")
        link = self.tmp / "linkdir"
        link.symlink_to(real)
        cap = capture_file(link / "leaf.bin", 64)
        self.assertTrue(cap.complete)
        self.assertEqual(cap.data, b"leaf payload")
        self.assertEqual(cap.path, str(link / "leaf.bin"))


class CaptureTest(CaptureTestCase):
    def test_empty_file_complete(self):
        cap = capture_file(self.write("empty.bin", b""), 16)
        self.assertEqual(cap.data, b"")
        self.assertTrue(cap.complete)
        self.assertIsNone(cap.reason)
        self.assertEqual(cap.sha256, hashlib.sha256(b"").hexdigest())
        self.assertEqual(cap.stat_after.size, 0)

    def test_binary_and_unicode_digest_matches_content(self):
        content = "café 中文 \U0001f680\n".encode() + b"\x00\xff\xfe"
        cap = capture_file(self.write("mixed.bin", content), 1024)
        self.assertEqual(cap.data, content)
        self.assertTrue(cap.complete)
        self.assertEqual(cap.sha256, hashlib.sha256(content).hexdigest())

    def test_exact_limit_complete(self):
        content = bytes(range(32))
        cap = capture_file(self.write("exact.bin", content), len(content))
        self.assertTrue(cap.complete)
        self.assertEqual(cap.data, content)
        self.assertEqual(cap.sha256, hashlib.sha256(content).hexdigest())

    def test_overflow_truncated_without_digest(self):
        content = b"0123456789ABCDEF"
        cap = capture_file(self.write("big.bin", content), 8)
        self.assertFalse(cap.complete)
        self.assertEqual(cap.data, content[:8])
        self.assertIsNone(cap.sha256)
        self.assertIn("truncated", cap.reason)

    def test_sparse_read_is_bounded(self):
        sparse = self.tmp / "sparse.bin"
        sparse.touch()
        os.truncate(sparse, 4 * 1024 * 1024)
        reads = []
        real_read = os.read

        def spy(fd, size):
            chunk = real_read(fd, size)
            reads.append(len(chunk))
            return chunk

        with mock.patch("os.read", side_effect=spy):
            cap = capture_file(sparse, 1024)
        self.assertFalse(cap.complete)
        self.assertEqual(len(cap.data), 1024)
        self.assertEqual(sum(reads), 1025)
        self.assertEqual(cap.stat_after.size, 4 * 1024 * 1024)

    def test_stat_drift_during_read_marks_incomplete(self):
        path = self.write("stable.bin", b"stable bytes")
        st = os.stat(path)
        drifted = SimpleNamespace(st_mode=st.st_mode, st_dev=st.st_dev, st_ino=st.st_ino,
                                  st_size=st.st_size, st_mtime_ns=st.st_mtime_ns + 1)
        with mock.patch("mdi.capture._fstat", side_effect=[st, drifted]):
            cap = capture_file(path, 64)
        self.assertFalse(cap.complete)
        self.assertIn("changed during read", cap.reason)
        self.assertIsNone(cap.sha256)
        self.assertEqual(cap.data, b"stable bytes")
        self.assertNotEqual(cap.stat_before, cap.stat_after)

    def test_observed_at_is_utc_iso8601(self):
        cap = capture_file(self.write("when.bin", b"x"), 16)
        moment = datetime.fromisoformat(cap.observed_at)
        self.assertIsNotNone(moment.tzinfo)
        self.assertEqual(moment.utcoffset(), timedelta(0))
        self.assertLess(abs(moment - datetime.now(timezone.utc)), timedelta(minutes=5))

    def test_captures_are_frozen(self):
        cap = capture_file(self.write("frozen.bin", b"x"), 16)
        with self.assertRaises(FrozenInstanceError):
            cap.data = b"rewritten"
        with self.assertRaises(FrozenInstanceError):
            cap.stat_after.size = 0


class CompareTest(CaptureTestCase):
    def test_unchanged_returns_none(self):
        path = self.write("same.bin", b"unchanged")
        first = capture_file(path, 1024)
        second = capture_file(path, 1024)
        self.assertIsNone(compare_captures(first, second))

    def test_modified_preserves_both_and_reports_change(self):
        path = self.write("evolve.bin", b"version one")
        before = capture_file(path, 1024)
        path.write_bytes(b"version two, longer")
        after = capture_file(path, 1024)
        self.assertEqual(before.data, b"version one")
        self.assertEqual(after.data, b"version two, longer")
        change = compare_captures(before, after)
        self.assertIsNotNone(change)
        self.assertEqual(change.path, str(path))
        self.assertIn("captured data differs", change.reasons)
        self.assertEqual(change.before_digest, hashlib.sha256(b"version one").hexdigest())
        self.assertEqual(change.after_digest, hashlib.sha256(b"version two, longer").hexdigest())
        self.assertEqual(change.before_observed_at, before.observed_at)
        self.assertEqual(change.after_observed_at, after.observed_at)
        self.assertEqual(before.data, b"version one")

    def test_cross_subject_compare_rejected(self):
        one = capture_file(self.write("a.bin", b"aaa"), 1024)
        two = capture_file(self.write("b.bin", b"bbb"), 1024)
        with self.assertRaises(ValueError):
            compare_captures(one, two)

    def test_relative_and_absolute_spelling_compare(self):
        self.write("same.bin", b"unchanged")
        with self.cwd_at(self.tmp):
            first = capture_file("same.bin", 1024)
        second = capture_file(self.tmp / "same.bin", 1024)
        self.assertEqual(first.path, str(self.tmp / "same.bin"))
        self.assertIsNone(compare_captures(first, second))

    def test_same_relative_spelling_other_cwd_is_cross_subject(self):
        self.write("same.bin", b"top file")
        sub = self.tmp / "sub"
        sub.mkdir()
        (sub / "same.bin").write_bytes(b"sub file")
        with self.cwd_at(self.tmp):
            top = capture_file("same.bin", 1024)
        with self.cwd_at(sub):
            inner = capture_file("same.bin", 1024)
        self.assertNotEqual(top.path, inner.path)
        with self.assertRaises(ValueError):
            compare_captures(top, inner)


class SafeOpenFlagTest(CaptureTestCase):
    def test_zero_flag_fails_closed(self):
        path = self.write("guarded.bin", b"payload")
        for flag in ("O_NOFOLLOW", "O_NONBLOCK"):
            with mock.patch.object(os, flag, 0), self.assertRaises(CaptureError) as ctx:
                capture_file(path, 16)
            self.assertIn("unsupported", str(ctx.exception))

    def test_absent_flag_fails_closed(self):
        path = self.write("guarded2.bin", b"payload")
        saved = os.O_NOFOLLOW
        del os.O_NOFOLLOW
        try:
            with self.assertRaises(CaptureError):
                capture_file(path, 16)
        finally:
            os.O_NOFOLLOW = saved
