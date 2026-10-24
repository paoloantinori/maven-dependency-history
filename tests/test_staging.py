"""Tests for mdi.staging safe input staging."""

import contextlib
import errno
import hashlib
import itertools
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dataclasses

import mdi.discover
import mdi.staging
from mdi.staging import (StageError, StageLimits, StagedInput, StagedInputs,
                         stage_inputs, staged_inputs)


class StagingTestCase(unittest.TestCase):
    def setUp(self):
        checkout = tempfile.TemporaryDirectory()
        base = tempfile.TemporaryDirectory()
        self.addCleanup(checkout.cleanup)
        self.addCleanup(base.cleanup)
        self.checkout = Path(checkout.name).resolve()
        self.base = Path(base.name).resolve()

    def write(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def jar(self, name: str, data: bytes = b"PK\x03\x04jar-bytes") -> Path:
        return self.write(self.checkout / name, data)

    def tree(self, name: str, files: dict) -> Path:
        root = self.checkout / name
        root.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            self.write(root / rel, data)
        return root

    def stage(self, root, deps=(), limits=None, base=None):
        kwargs = {} if limits is None else {"limits": limits}
        return stage_inputs(self.checkout, root, list(deps),
                            staging_base=base or self.base, **kwargs)

    def assertStageError(self, code, callable_obj, *args, **kwargs):
        with self.assertRaises(StageError) as ctx:
            callable_obj(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code,
                         msg=f"expected {code}, got {ctx.exception.code}:"
                             f" {ctx.exception.message}")
        return ctx.exception

    def base_children(self):
        return sorted(p.name for p in self.base.iterdir())


class LimitsTest(StagingTestCase):
    def test_defaults(self):
        limits = StageLimits()
        self.assertEqual(limits.max_inputs, 64)
        self.assertEqual(limits.max_files, 10000)
        self.assertEqual(limits.max_dirs, 256)
        self.assertEqual(limits.max_depth, 16)
        self.assertEqual(limits.max_file_bytes, 16 * 1024 * 1024)
        self.assertEqual(limits.max_total_bytes, 64 * 1024 * 1024)
        self.assertEqual(limits.max_seconds, 30)

    def test_bool_rejected(self):
        for index, name in enumerate(("max_inputs", "max_files", "max_dirs",
                                      "max_depth", "max_file_bytes",
                                      "max_total_bytes", "max_seconds")):
            with self.assertRaises(TypeError, msg=name):
                StageLimits(**{name: True})

    def test_non_positive_and_non_int_rejected(self):
        for name in ("max_inputs", "max_files", "max_dirs", "max_depth",
                     "max_file_bytes", "max_total_bytes", "max_seconds"):
            for bad in (0, -1):
                with self.assertRaises(ValueError, msg=f"{name}={bad}"):
                    StageLimits(**{name: bad})
            for bad in ("5", 1.0, None):
                with self.assertRaises(TypeError, msg=f"{name}={bad!r}"):
                    StageLimits(**{name: bad})

    def test_non_limits_object_rejected(self):
        with self.assertRaises(TypeError):
            self.stage(self.jar("a.jar"), limits={"max_inputs": 4})


class JarStagingTest(StagingTestCase):
    def test_jar_staged_byte_identical_new_inode(self):
        payload = b"PK\x03\x04payload-\x00\xff"
        source = self.jar("root.jar", payload)
        dep = self.jar("dep.jar", b"dep-bytes")
        bundle = self.stage(source, [dep])
        self.assertTrue(bundle.complete)
        self.assertEqual(bundle.issues, ())
        self.assertEqual(bundle.root.role, "root")
        self.assertEqual(bundle.root.kind, "jar")
        self.assertEqual(bundle.root.staged_name, "input-0000.jar")
        self.assertEqual(bundle.dependencies[0].staged_name, "input-0001.jar")
        staged_root = Path(bundle.root.staged_path)
        staged_dep = Path(bundle.dependencies[0].staged_path)
        self.assertEqual(staged_root.read_bytes(), payload)
        self.assertEqual(staged_dep.read_bytes(), b"dep-bytes")
        self.assertNotEqual(os.stat(source).st_ino, os.stat(staged_root).st_ino)
        self.assertEqual(os.stat(staged_root).st_nlink, 1)
        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual(bundle.root.source_digest, digest)
        self.assertEqual(bundle.root.staged_digest, digest)

    def test_permissions_and_layout(self):
        source = self.jar("root.jar")
        bundle = self.stage(source)
        self.assertTrue(bundle.stage_dir.startswith(str(self.base) + os.sep))
        stage = Path(bundle.stage_dir)
        self.assertEqual(stat.S_IMODE(os.stat(stage).st_mode), 0o700)
        self.assertEqual(os.stat(bundle.root.staged_path).st_mode & 0o777, 0o600)
        self.assertEqual(stage.name, bundle.stage_id)
        self.assertEqual(sorted(p.name for p in stage.iterdir()),
                         ["input-0000.jar"])

    def test_metadata_json_safe(self):
        bundle = self.stage(self.jar("root.jar"))
        json.dumps(bundle.summary)
        json.dumps(list(bundle.observations))
        self.assertEqual(bundle.summary["schema"], "mdi.staged-inputs/1")
        self.assertEqual(bundle.summary["counts"]["files"], 1)
        self.assertTrue(bundle.summary["limitations"])
        self.assertEqual(json.loads(json.dumps(bundle.summary))["stage_id"],
                         bundle.stage_id)

    def test_dependencies_immutable_tuple(self):
        deps = [self.jar("a.jar"), self.jar("b.jar")]
        bundle = self.stage(self.jar("root.jar"), deps)
        self.assertIsInstance(bundle.dependencies, tuple)
        with self.assertRaises((AttributeError, TypeError)):
            bundle.dependencies.append(deps[0])

    def test_observations_carry_locator_and_mapping(self):
        source = self.jar("root.jar", b"xyz")
        bundle = self.stage(source)
        obs = [o for o in bundle.observations if o["role"] == "root"]
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["source"], str(source))
        self.assertEqual(obs[0]["staged_rel"], "input-0000.jar")
        self.assertEqual(obs[0]["source_sha256"], obs[0]["staged_sha256"])


class DirectoryStagingTest(StagingTestCase):
    FILES = {"com/example/App.class": b"\xca\xfe\xba\xbeapp",
             "com/example/inner/Helper.class": b"helper",
             "META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\n",
             "META-INF/versions/9/com/example/App.class": b"mr-class",
             "config/resource.xml": b"<config/>"}

    def test_tree_staged_identical_new_inodes(self):
        source = self.tree("classes", self.FILES)
        bundle = self.stage(source)
        self.assertEqual(bundle.root.kind, "directory")
        self.assertEqual(bundle.root.staged_name, "input-0000")
        staged = Path(bundle.root.staged_path)
        self.assertTrue(staged.is_dir())
        for rel, data in self.FILES.items():
            staged_file = staged / rel
            self.assertEqual(staged_file.read_bytes(), data, msg=rel)
            original = source / rel
            self.assertNotEqual(os.stat(original).st_ino,
                                os.stat(staged_file).st_ino, msg=rel)
            self.assertEqual(os.stat(staged_file).st_mode & 0o777, 0o600, msg=rel)
        for rel in ("com", "com/example", "com/example/inner", "META-INF",
                    "META-INF/versions/9", "config"):
            self.assertEqual(os.stat(staged / rel).st_mode & 0o777, 0o700, msg=rel)

    def test_tree_digest_deterministic_and_order_independent(self):
        source_a = self.tree("classes", self.FILES)
        source_b = self.tree("other", dict(reversed(list(self.FILES.items()))))
        bundle_a = self.stage(source_a)
        bundle_b = self.stage(source_b)
        ordered = "".join(f"{rel}\x00{hashlib.sha256(data).hexdigest()}\n"
                          for rel, data in sorted(self.FILES.items()))
        expected = hashlib.sha256(ordered.encode()).hexdigest()
        self.assertEqual(bundle_a.root.source_digest, expected)
        self.assertEqual(bundle_b.root.source_digest, expected)
        self.assertEqual(bundle_a.root.source_digest,
                         bundle_a.root.staged_digest)
        changed = self.tree("changed", dict(self.FILES,
                                            **{"config/resource.xml": b"<other/>"}))
        self.assertNotEqual(self.stage(changed).root.source_digest, expected)

    def test_mixed_root_and_dependency_roles_ordered(self):
        classes = self.tree("classes", {"A.class": b"a"})
        jar = self.jar("lib.jar", b"lib")
        bundle = self.stage(classes, [jar])
        self.assertEqual(bundle.root.staged_name, "input-0000")
        self.assertEqual(bundle.dependencies[0].staged_name, "input-0001.jar")
        self.assertEqual(bundle.root.file_count, 1)
        self.assertEqual(bundle.dependencies[0].file_count, 1)
        member_obs = [o for o in bundle.observations if o["role"] == "member"]
        self.assertEqual(len(member_obs), 1)
        self.assertEqual(member_obs[0]["staged_rel"],
                         "input-0000/A.class")


class CleanupTest(StagingTestCase):
    def test_context_cleans_on_success(self):
        with staged_inputs(self.checkout, self.jar("root.jar"), [],
                           staging_base=self.base) as bundle:
            self.assertTrue(Path(bundle.stage_dir).is_dir())
        self.assertEqual(self.base_children(), [])

    def test_context_cleans_on_body_error(self):
        with self.assertRaises(RuntimeError):
            with staged_inputs(self.checkout, self.jar("root.jar"), [],
                               staging_base=self.base):
                raise RuntimeError("helper blew up")
        self.assertEqual(self.base_children(), [])

    def test_failure_leaves_base_empty(self):
        with self.assertRaises(StageError):
            self.stage(self.checkout / "missing.jar")
        self.assertEqual(self.base_children(), [])

    def test_cleanup_interference_leaves_replacement(self):
        bundle = self.stage(self.jar("root.jar"))
        staged = Path(bundle.root.staged_path)
        staged.unlink()
        staged.write_bytes(b"replacement")
        self.assertStageError("stage-interference", bundle.cleanup)
        self.assertEqual(staged.read_bytes(), b"replacement")
        self.assertTrue(Path(bundle.stage_dir).is_dir())

    def test_checkout_untouched_by_staging(self):
        jar = self.jar("root.jar", b"root")
        classes = self.tree("classes", {"a/A.class": b"A"})
        sentinel = self.write(self.checkout / "sentinel/canary.txt", b"canary")
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.checkout.rglob("*") if p.is_file()}
        with staged_inputs(self.checkout, jar, [classes], staging_base=self.base):
            pass
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.checkout.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(sentinel.read_bytes(), b"canary")
        self.assertEqual(self.base_children(), [])


class SourceRejectionTest(StagingTestCase):
    def test_outside_checkout_rejected(self):
        outside = self.write(self.base / "elsewhere.jar", b"x")
        self.assertStageError("outside-checkout", self.stage, outside)
        self.assertStageError("outside-checkout", self.stage,
                              str(self.checkout.parent / "escape.jar"))

    def test_lexical_escape_rejected(self):
        spelled = self.checkout / "sub" / ".." / "escape.jar"
        self.assertStageError("source-rejected", self.stage, spelled)
        escaping = self.checkout / ".." / "escape.jar"
        self.assertStageError("outside-checkout", self.stage, escaping)

    def test_missing_input_rejected(self):
        self.assertStageError("source-rejected", self.stage,
                              self.checkout / "nope.jar")

    def test_symlink_input_rejected(self):
        target = self.jar("real.jar")
        link = self.checkout / "link.jar"
        link.symlink_to(target)
        self.assertStageError("symlink-input", self.stage, link)
        self.assertEqual(target.read_bytes(), b"PK\x03\x04jar-bytes")

    def test_symlinked_component_rejected(self):
        real = self.tree("realdir", {"x.jar": b"x"})
        link = self.checkout / "linkdir"
        link.symlink_to(real)
        self.assertStageError("source-rejected", self.stage, link / "x.jar")

    def test_fifo_input_rejected(self):
        fifo = self.checkout / "pipe.jar"
        os.mkfifo(fifo)
        self.assertStageError("nonregular-input", self.stage, fifo)

    def test_plain_file_not_jar_rejected(self):
        self.assertStageError("input-kind", self.stage,
                              self.write(self.checkout / "plain.txt", b"t"))

    def test_directory_with_jar_name_rejected(self):
        self.tree("weird.jar", {"a.class": b"a"})
        self.assertStageError("input-kind", self.stage, self.checkout / "weird.jar")

    def test_symlink_member_rejected(self):
        source = self.tree("classes", {"A.class": b"A"})
        (source / "link.class").symlink_to(source / "A.class")
        self.assertStageError("symlink-member", self.stage, source)

    def test_fifo_member_rejected(self):
        source = self.tree("classes", {"A.class": b"A"})
        os.mkfifo(source / "pipe")
        self.assertStageError("nonregular-member", self.stage, source)
        self.assertEqual(self.base_children(), [])

    def test_duplicate_inputs_rejected(self):
        jar = self.jar("same.jar", b"d")
        self.assertStageError("duplicate-input", self.stage, jar, [jar])
        respelled = self.checkout / "sub" / ".." / "same.jar"
        self.assertStageError("duplicate-input", self.stage, jar, [respelled])
        other = self.jar("other.jar", b"o")
        self.assertStageError("duplicate-input", self.stage, jar, [other, jar])


class BaseRejectionTest(StagingTestCase):
    def test_symlink_base_rejected(self):
        link = self.base / "linkbase"
        link.symlink_to(self.base)
        self.assertStageError("staging-base-rejected", self.stage,
                              self.jar("root.jar"), base=link)

    def test_base_inside_checkout_rejected(self):
        inner = self.checkout / "stage"
        inner.mkdir()
        self.assertStageError("staging-base-inside-checkout", self.stage,
                              self.jar("root.jar"), base=inner)

    def test_base_overlapping_checkout_rejected(self):
        nested = self.tree("deep/proj", {"pom.xml": b"<project/>"})
        self.assertStageError("staging-base-overlaps-checkout",
                              stage_inputs, nested, nested / "pom.xml", [],
                              staging_base=self.checkout / "deep")

    def test_missing_base_rejected(self):
        self.assertStageError("staging-base-rejected", self.stage,
                              self.jar("root.jar"),
                              base=self.base / "missing")

    def test_base_file_rejected(self):
        target = self.write(self.base / "file.txt", b"f")
        self.assertStageError("staging-base-rejected", self.stage,
                              self.jar("root.jar"), base=target)


class BudgetTest(StagingTestCase):
    def test_file_bytes(self):
        jar = self.jar("big.jar", b"x" * 100)
        limits = StageLimits(max_file_bytes=10)
        self.assertStageError("file-bytes", self.stage, jar, limits=limits)

    def test_total_bytes_cumulative(self):
        first = self.jar("a.jar", b"a" * 60)
        second = self.jar("b.jar", b"b" * 60)
        limits = StageLimits(max_total_bytes=100)
        self.assertStageError("total-bytes", self.stage, first, [second],
                              limits=limits)

    def test_file_count_cumulative(self):
        root = self.jar("root.jar", b"r")
        dep = self.tree("classes", {"a.class": b"a", "b.class": b"b"})
        limits = StageLimits(max_files=2)
        self.assertStageError("file-count", self.stage, root, [dep],
                              limits=limits)

    def test_dir_count(self):
        dep = self.tree("classes", {"a/x.class": b"a", "b/y.class": b"b"})
        limits = StageLimits(max_dirs=2)
        self.assertStageError("dir-count", self.stage, self.jar("r.jar"), [dep],
                              limits=limits)

    def test_depth(self):
        dep = self.tree("classes", {"a/b/c/deep.class": b"d"})
        limits = StageLimits(max_depth=2)
        self.assertStageError("depth-limit", self.stage, self.jar("r.jar"), [dep],
                              limits=limits)

    def test_input_count(self):
        deps = [self.jar(f"d{i}.jar", bytes([i])) for i in range(3)]
        limits = StageLimits(max_inputs=2)
        self.assertStageError("input-count", self.stage, self.jar("r.jar"),
                              deps, limits=limits)

    def test_time_budget(self):
        ticking = itertools.count(step=1000).__next__
        with mock.patch.object(mdi.staging, "_monotonic", ticking):
            self.assertStageError("time-budget", self.stage,
                                  self.jar("r.jar", b"r"),
                                  limits=StageLimits(max_seconds=30))

    def test_fds_released_on_early_failure(self):
        dep = self.tree("classes", {"a/x.class": b"a", "b/y.class": b"b"})
        before = len(os.listdir("/proc/self/fd"))
        with self.assertRaises(StageError):
            self.stage(self.jar("r.jar"), [dep], limits=StageLimits(max_dirs=2))
        after = len(os.listdir("/proc/self/fd"))
        self.assertEqual(before, after)
        self.assertEqual(self.base_children(), [])


class InterferenceTest(StagingTestCase):
    def test_child_swapped_for_symlink_never_reads_outside(self):
        outside = self.write(self.base / "outside" / "secret.class", b"secret")
        dep = self.tree("classes", {"pkg/inner/A.class": b"A"})

        def swap(dir_fd, name, subject):
            if name == "pkg":
                os.rename(name, name + ".orig", src_dir_fd=dir_fd,
                          dst_dir_fd=dir_fd)
                os.symlink(str(outside.parent), name, dir_fd=dir_fd)

        with mock.patch.object(mdi.staging, "_before_child_open", swap):
            exc = self.assertStageError("source-rejected", self.stage,
                                        self.jar("r.jar"), [dep])
        self.assertIn("cannot open", exc.message)
        survivors = [p for p in self.base.rglob("secret.class")]
        self.assertEqual(survivors, [outside])
        self.assertEqual(self.base_children(), ["outside"])

    def test_directory_mutated_during_walk_refused(self):
        dep = self.tree("classes", {"A.class": b"A"})

        def touch(dir_fd, subject):
            fd = os.open("extra.txt", os.O_WRONLY | os.O_CREAT, 0o600,
                         dir_fd=dir_fd)
            os.write(fd, b"extra")
            os.close(fd)

        with mock.patch.object(mdi.staging, "_before_descend", touch):
            exc = self.assertStageError("source-changed", self.stage,
                                        self.jar("r.jar"), [dep])
        self.assertTrue(exc.observations)
        self.assertTrue(any(o["source"].endswith("classes/A.class")
                            for o in exc.observations))
        self.assertEqual(self.base_children(), [])

    def test_file_changed_during_read_refused(self):
        jar = self.jar("drift.jar", b"payload")
        real_fstat = os.fstat
        seen = {}

        def drifting(fd):
            st = real_fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return st
            seen[fd] = seen.get(fd, 0) + 1
            if seen[fd] > 1:
                return SimpleNamespace(st_dev=st.st_dev, st_ino=st.st_ino,
                                       st_size=st.st_size,
                                       st_mtime_ns=st.st_mtime_ns + 1,
                                       st_mode=st.st_mode)
            return st

        with mock.patch.object(mdi.discover.os, "fstat", drifting):
            self.assertStageError("source-changed", self.stage, jar)
        self.assertEqual(self.base_children(), [])


class ComplianceFixTest(StagingTestCase):
    """Regressions for the compliance review: non-UTF-8 names, guarded
    resource lifecycle, and observation retention on every failure path."""

    def fd_count(self):
        return len(os.listdir("/proc/self/fd"))

    def write_raw_member(self, directory, raw_name: bytes, data: bytes) -> None:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = os.open(raw_name, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=dir_fd)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)

    def test_non_utf8_member_rejected_with_observations_and_cleanup(self):
        dep = self.tree("classes", {"A.class": b"A"})
        self.write_raw_member(dep, b"bad-\xe9.class", b"raw")
        before = self.fd_count()
        exc = self.assertStageError("non-utf8-name", self.stage,
                                    self.jar("r.jar"), [dep])
        self.assertTrue(any(o["source"].endswith("classes/A.class")
                            for o in exc.observations))
        self.assertEqual(self.base_children(), [])
        self.assertEqual(self.fd_count(), before)

    def test_non_utf8_input_path_rejected(self):
        dep = self.tree("classes", {"A.class": b"A"})
        self.write_raw_member(dep, b"bad-\xe9.class", b"raw")
        raw = os.path.join(os.fsencode(str(dep)), b"bad-\xe9.class")
        with self.assertRaises(StageError) as ctx:
            stage_inputs(self.checkout, os.fsdecode(raw), [],
                         staging_base=self.base)
        self.assertEqual(ctx.exception.code, "non-utf8-name")
        self.assertEqual(self.base_children(), [])

    def test_stage_identity_stat_failure_closes_fds_leaves_unverified_stage(self):
        jar = self.jar("root.jar")
        real_stat = os.stat
        failing = itertools.count()

        def first_call_fails(*args, **kwargs):
            if next(failing) == 0:
                raise OSError(errno.EIO, "injected stat failure")
            return real_stat(*args, **kwargs)

        before = self.fd_count()
        # The wrapper must also look dir_fd-capable to the platform gate,
        # which tests membership in os.supports_dir_fd rather than calling.
        supported = os.supports_dir_fd | {first_call_fails}
        with mock.patch.object(mdi.staging.os, "stat", first_call_fails), \
                mock.patch.object(mdi.staging.os, "supports_dir_fd", supported):
            exc = self.assertStageError("stage-create-failed", self.stage, jar)
        self.assertIn("left in place", exc.message)
        children = self.base_children()
        self.assertEqual(len(children), 1, "unverified partial must remain")
        self.assertTrue((self.base / children[0]).is_dir())
        self.assertEqual(self.fd_count(), before)

    def test_empty_replacement_survives_identity_stat_failure(self):
        jar = self.jar("root.jar")
        real_mkdir, real_rmdir, real_stat = os.mkdir, os.rmdir, os.stat
        stat_calls = itertools.count()

        def swapping_mkdir(path, mode=0o777, *, dir_fd=None):
            real_mkdir(path, mode, dir_fd=dir_fd)
            # Same-user swap right after creation: remove ours, plant an
            # empty replacement under the same fresh name (mode 0755 marks it).
            real_rmdir(path, dir_fd=dir_fd)
            real_mkdir(path, 0o755, dir_fd=dir_fd)

        def failing_stat(*args, **kwargs):
            if next(stat_calls) == 0:
                raise OSError(errno.EIO, "injected stat failure")
            return real_stat(*args, **kwargs)

        before = self.fd_count()
        supported = os.supports_dir_fd | {swapping_mkdir, failing_stat}
        with mock.patch.object(mdi.staging.os, "mkdir", swapping_mkdir), \
                mock.patch.object(mdi.staging.os, "stat", failing_stat), \
                mock.patch.object(mdi.staging.os, "supports_dir_fd", supported):
            self.assertStageError("stage-create-failed", self.stage, jar)
        children = self.base_children()
        self.assertEqual(len(children), 1, "the replacement must be left intact")
        replacement = self.base / children[0]
        self.assertTrue(replacement.is_dir())
        self.assertEqual(list(replacement.iterdir()), [])
        self.assertEqual(stat.S_IMODE(os.stat(replacement).st_mode), 0o755)
        self.assertEqual(self.fd_count(), before)

    def test_read_oserror_retains_observations_and_cleans(self):
        dep = self.tree("classes", {"a.class": b"a", "b.class": b"b"})
        real_capture = mdi.staging._capture_at
        calls = itertools.count()

        def fail_after_first(dir_fd, name, subject, max_bytes):
            if next(calls) >= 1:
                raise OSError(errno.EIO, "injected read failure")
            return real_capture(dir_fd, name, subject, max_bytes)

        before = self.fd_count()
        with mock.patch.object(mdi.staging, "_capture_at", fail_after_first):
            exc = self.assertStageError("stage-failed", self.stage,
                                        self.jar("root.jar"), [dep])
        self.assertTrue(exc.observations)
        self.assertEqual(self.base_children(), [])
        self.assertEqual(self.fd_count(), before)

    def test_runtime_error_cleans_stage_and_propagates(self):
        dep = self.tree("classes", {"A.class": b"A"})

        def boom(dir_fd, subject):
            raise RuntimeError("unexpected")

        before = self.fd_count()
        with mock.patch.object(mdi.staging, "_before_descend", boom):
            with self.assertRaises(RuntimeError):
                self.stage(self.jar("r.jar"), [dep])
        self.assertEqual(self.base_children(), [])
        self.assertEqual(self.fd_count(), before)

    def test_keyboard_interrupt_cleans_stage_and_propagates(self):
        dep = self.tree("classes", {"A.class": b"A"})

        def interrupting(dir_fd, name, subject, max_bytes):
            raise KeyboardInterrupt

        before = self.fd_count()
        with mock.patch.object(mdi.staging, "_capture_at", interrupting):
            with self.assertRaises(KeyboardInterrupt):
                self.stage(self.jar("r.jar"), [dep])
        self.assertEqual(self.base_children(), [])
        self.assertEqual(self.fd_count(), before)


class CleanupQualityTest(StagingTestCase):
    """Cleanup failures are normalized StageErrors, never raw OSErrors that
    mask the caller's exception, and no source fd leaks when staged-side
    mkdir fails after the source open."""

    def fd_count(self):
        return len(os.listdir("/proc/self/fd"))

    def test_cleanup_of_deleted_ledgered_entry_raises_stage_error(self):
        bundle = self.stage(self.jar("root.jar"))
        os.unlink(bundle.root.staged_path)
        with self.assertRaises(StageError) as ctx:
            bundle.cleanup()
        self.assertEqual(ctx.exception.code, "stage-interference")

    def test_context_body_exception_survives_cleanup_failure(self):
        with self.assertRaises(RuntimeError) as ctx:
            with staged_inputs(self.checkout, self.jar("root.jar"), [],
                               staging_base=self.base) as bundle:
                os.unlink(bundle.root.staged_path)
                raise RuntimeError("body failure")
        self.assertIsInstance(ctx.exception, RuntimeError)
        notes = getattr(ctx.exception, "__notes__", [])
        self.assertTrue(any("cleanup" in note.lower() for note in notes),
                        msg=f"notes were {notes}")

    def test_walk_child_mkdir_failure_leaks_no_source_fd(self):
        dep = self.tree("classes", {"pkg/inner/A.class": b"A"})
        real_mkdir_staged = mdi.staging._mkdir_staged
        calls = itertools.count()

        def failing_after_first(dir_fd, name, rel, own):
            if next(calls) >= 1:
                raise OSError(errno.EIO, "injected staged-mkdir failure")
            return real_mkdir_staged(dir_fd, name, rel, own)

        before = self.fd_count()
        with mock.patch.object(mdi.staging, "_mkdir_staged", failing_after_first):
            self.assertStageError("stage-failed", self.stage,
                                  self.jar("r.jar"), [dep])
        self.assertEqual(self.base_children(), [])
        self.assertEqual(self.fd_count(), before)

    def test_stage_one_mkdir_failure_leaks_no_source_fd(self):
        classes = self.tree("classes", {"A.class": b"A"})

        def failing(dir_fd, name, rel, own):
            raise OSError(errno.EIO, "injected staged-mkdir failure")

        before = self.fd_count()
        with mock.patch.object(mdi.staging, "_mkdir_staged", failing):
            self.assertStageError("stage-failed", self.stage, classes)
        self.assertEqual(self.base_children(), [])
        self.assertEqual(self.fd_count(), before)


class LeftoverEvidenceTest(StagingTestCase):
    """Structured leftovers on refused stagings: the retained location is
    carried as evidence on the StageError, never inferred clean from absent
    ownership, never discovered by parsing the message prose."""

    def sabotage_after_first_capture(self, replace, call_index=1):
        """Wrap _capture_at so that call #call_index first applies `replace`
        to an already staged object, then fails the read."""
        real_capture = mdi.staging._capture_at
        calls = itertools.count()

        def wrapper(dir_fd, name, subject, max_bytes):
            if next(calls) >= call_index:
                replace()
                raise OSError(errno.EIO, "injected read failure")
            return real_capture(dir_fd, name, subject, max_bytes)

        return wrapper

    def replace_staged(self, rel, data):
        def apply():
            victim = next(self.base.rglob(rel))
            victim.unlink()
            victim.write_bytes(data)
        return apply

    def test_stage_error_defaults_are_unknown_and_empty(self):
        exc = StageError("some-code", "some message")
        self.assertEqual(exc.cleanup_status, "unknown")
        self.assertEqual(exc.leftovers, ())
        self.assertIsInstance(exc.leftovers, tuple)
        positional = StageError("c", "m", ({"role": "root"},))
        self.assertEqual(positional.observations, ({"role": "root"},))

    def test_leftover_records_are_frozen_and_bounded(self):
        record = mdi.staging.StageLeftover("/some/path", "r" * 5000, False)
        self.assertLessEqual(len(record.reason), 512)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.path = "/other"
        exc = StageError("c", "m", cleanup_status="failed",
                         leftovers=[record])
        self.assertIsInstance(exc.leftovers, tuple)
        self.assertIs(exc.leftovers[0], record)
        self.assertEqual(record.as_dict(),
                         {"path": "/some/path", "reason": record.reason,
                          "identity_verified": False})

    def test_identity_stat_failure_records_unverified_leftover(self):
        jar = self.jar("root.jar")
        real_stat = os.stat
        failing = itertools.count()

        def first_call_fails(*args, **kwargs):
            if next(failing) == 0:
                raise OSError(errno.EIO, "injected stat failure")
            return real_stat(*args, **kwargs)

        supported = os.supports_dir_fd | {first_call_fails}
        with mock.patch.object(mdi.staging.os, "stat", first_call_fails), \
                mock.patch.object(mdi.staging.os, "supports_dir_fd", supported):
            exc = self.assertStageError("stage-create-failed", self.stage, jar)
        children = self.base_children()
        self.assertEqual(len(children), 1)
        self.assertEqual(exc.cleanup_status, "failed")
        self.assertEqual(len(exc.leftovers), 1)
        record = exc.leftovers[0]
        self.assertEqual(record.path, str(self.base / children[0]))
        self.assertTrue(Path(record.path).is_dir())
        self.assertIs(record.identity_verified, False)
        self.assertTrue(record.reason)
        # The retained location is the stage, never a checkout source path.
        self.assertTrue(record.path.startswith(str(self.base) + os.sep))
        self.assertFalse(record.path.startswith(str(self.checkout) + os.sep))

    def test_abort_interference_preserves_replacement_and_records_it(self):
        # A jar dependency keeps the ledger at exactly input-0000.jar when
        # the dep read fails, so the replaced root copy is the first and
        # only removal attempt and nothing of ours was removed.
        dep = self.jar("dep.jar", b"d")
        sabotage = self.sabotage_after_first_capture(
            self.replace_staged("input-0000.jar", b"foreign-replacement"))
        with mock.patch.object(mdi.staging, "_capture_at", sabotage):
            exc = self.assertStageError("stage-failed", self.stage,
                                        self.jar("r.jar"), [dep])
        self.assertEqual(exc.cleanup_status, "failed")
        self.assertEqual(len(exc.leftovers), 1)
        record = exc.leftovers[0]
        self.assertIs(record.identity_verified, False)
        self.assertIn("interference", record.reason)
        # The foreign replacement is preserved untouched inside the retained
        # stage, and the structured record names that stage, not a source.
        children = self.base_children()
        self.assertEqual(len(children), 1)
        self.assertEqual(record.path, str(self.base / children[0]))
        replaced = self.base / children[0] / "input-0000.jar"
        self.assertEqual(replaced.read_bytes(), b"foreign-replacement")
        self.assertTrue(Path(record.path).is_dir())
        # The prose message names no location: the record is the evidence.
        self.assertNotIn(str(self.base), exc.message)

    def test_partial_removal_reports_partial_with_one_record(self):
        dep = self.tree("classes", {"a.class": b"a", "b.class": b"b",
                                    "c.class": b"c"})
        # a.class and b.class are already staged when c.class's capture
        # fails; replacing a.class lets cleanup remove b.class first and
        # then refuse on the replacement, so the outcome is partial.
        sabotage = self.sabotage_after_first_capture(
            self.replace_staged("input-0001/a.class", b"foreign-a"), 3)
        with mock.patch.object(mdi.staging, "_capture_at", sabotage):
            exc = self.assertStageError("stage-failed", self.stage,
                                        self.jar("r.jar"), [dep])
        self.assertEqual(exc.cleanup_status, "partial")
        records = exc.leftovers
        self.assertEqual(len(records), 1)
        self.assertIs(records[0].identity_verified, False)
        children = self.base_children()
        self.assertEqual(len(children), 1)
        stage = self.base / children[0]
        self.assertEqual(records[0].path, str(stage))
        self.assertEqual((stage / "input-0001" / "b.class").exists(), False)
        self.assertEqual((stage / "input-0001" / "a.class").read_bytes(),
                         b"foreign-a")
        self.assertTrue((stage / "input-0000.jar").is_file())

    def test_pre_creation_refusal_reports_not_created(self):
        # Only refusals raised before the stage directory exists are
        # not-created; source-kind refusals happen after creation and are
        # covered by the verified-cleanup test below.
        elsewhere = self.write(self.base / "elsewhere.jar", b"x")
        (self.checkout / "inner").mkdir()
        jar = self.jar("same.jar", b"d")
        for code, call in (
                ("outside-checkout", lambda: self.stage(elsewhere)),
                ("duplicate-input", lambda: self.stage(jar, [jar])),
                ("staging-base-inside-checkout",
                 lambda: self.stage(self.jar("r.jar"),
                                    base=self.checkout / "inner"))):
            with self.subTest(code=code):
                exc = self.assertStageError(code, call)
                self.assertEqual(exc.cleanup_status, "not-created")
                self.assertEqual(exc.leftovers, ())
                self.assertEqual(self.base_children(), ["elsewhere.jar"])

    def test_verified_abort_cleanup_reports_complete(self):
        for code, call in (
                ("source-rejected",
                 lambda: self.stage(self.jar("r.jar"),
                                    [self.checkout / "missing.jar"])),
                ("file-bytes",
                 lambda: self.stage(self.jar("big.jar", b"x" * 100),
                                    limits=StageLimits(max_file_bytes=10)))):
            with self.subTest(code=code):
                exc = self.assertStageError(code, call)
                self.assertEqual(exc.cleanup_status, "complete")
                self.assertEqual(exc.leftovers, ())
                self.assertEqual(self.base_children(), [])


class DoubleSlashContainmentTest(StagingTestCase):
    """POSIX allows a leading '//' spelling; on Linux it names the same root,
    so containment comparisons must not be fooled by it."""

    def test_double_slash_base_inside_checkout_rejected(self):
        (self.checkout / "stages").mkdir()
        inside = "//" + str(self.checkout / "stages")[1:]
        self.assertStageError("staging-base-inside-checkout",
                              stage_inputs, self.checkout, self.jar("root.jar"),
                              [], staging_base=inside)
        self.assertEqual(self.base_children(), [])

    def test_double_slash_base_containing_checkout_rejected(self):
        parent = "//" + str(self.checkout.parent)[1:]
        self.assertStageError("staging-base-overlaps-checkout",
                              stage_inputs, self.checkout, self.jar("root.jar"),
                              [], staging_base=parent)
        self.assertEqual(self.base_children(), [])

    def test_double_slash_external_base_still_works(self):
        external = "//" + str(self.base)[1:]
        bundle = self.stage(self.jar("root.jar"), base=external)
        self.assertTrue(Path(bundle.root.staged_path).is_file())
        bundle.cleanup()
        self.assertEqual(self.base_children(), [])

    def test_double_slash_input_inside_checkout_staged(self):
        spelled = "//" + str(self.checkout / "lib.jar")[1:]
        jar = self.jar("lib.jar")
        bundle = self.stage(self.jar("root.jar"), [spelled])
        self.assertEqual(bundle.dependencies[0].source, spelled)
        self.assertTrue(Path(bundle.dependencies[0].staged_path).is_file())
        bundle.cleanup()


class DirBudgetGuardTest(StagingTestCase):
    """The max_dirs budget check must sit inside the fd guard so a refused
    second directory input cannot leak its already-opened source fd."""

    def fd_count(self):
        return len(os.listdir("/proc/self/fd"))

    def test_second_dir_input_over_dir_budget_leaks_no_fd(self):
        first = self.tree("first", {})
        second = self.tree("second", {})
        before = self.fd_count()
        exc = self.assertStageError("dir-count", self.stage, self.jar("r.jar"),
                                    [first, second], limits=StageLimits(max_dirs=1))
        self.assertEqual(self.base_children(), [])
        self.assertEqual(self.fd_count(), before)
        self.assertEqual(exc.cleanup_status, "complete")


class OutputShapeTest(StagingTestCase):
    def test_bundle_shape_matches_trusted_contract(self):
        classes = self.tree("classes", {"a/A.class": b"a"})
        jar = self.jar("lib.jar", b"lib")
        bundle = self.stage(classes, [jar])
        self.assertIsInstance(bundle, StagedInputs)
        self.assertIsInstance(bundle.root, StagedInput)
        self.assertIsInstance(bundle.dependencies, tuple)
        self.assertIsInstance(bundle.observations, tuple)
        self.assertTrue(bundle.complete)
        root = bundle.root
        # The staged paths satisfy the run_usage_helper caller contract:
        # root first, dependencies after, absolute, .jar file / plain dir.
        self.assertTrue(os.path.isabs(root.staged_path))
        self.assertTrue(root.staged_path.endswith("input-0000"))
        self.assertNotIn(".jar", root.staged_path)
        self.assertTrue(bundle.dependencies[0].staged_path.endswith(".jar"))
        self.assertEqual(os.path.realpath(root.staged_path), root.staged_path)
        for item in (root, *bundle.dependencies):
            mode = os.stat(item.staged_path).st_mode
            if item.kind == "jar":
                self.assertTrue(stat.S_ISREG(mode))
            else:
                self.assertTrue(stat.S_ISDIR(mode))
        bundle.cleanup()
        self.assertEqual(self.base_children(), [])


if __name__ == "__main__":
    unittest.main()
