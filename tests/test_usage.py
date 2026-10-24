"""Tests for mdi.usage run_usage_helper (TASK-21 Python runner slice).

The helper jar is built once per class from the existing UsageHelper.java plus
the verified pinned analyzer jars (no network, no Maven, no staging here: the
tests act as the trusted staging caller). The pinned jars come from
MDI_TEST_DEPENDENCY_DIR and are verified against the pin manifest by
tests/java_support.py in setUpClass. Fixtures are the mini A/B/C/Dup
class set. Fake-java shims cover the env allowlist, argv shape, output cap,
timeout with descendant cleanup, exit-code mapping, malformed output and
schema violations. Analyzed classes are never executed (sentinel static
initializer in A; the Java helper suite covers the property in depth).
"""

import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import usage

REPO = Path(__file__).resolve().parents[1]
HELPER_SRC = (REPO / "skills" / "maven-dependency-investigator" / "helper"
              / "src" / "main" / "java" / "mdi" / "UsageHelper.java")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import child_env, java_support
from tests.java_fixtures import (A_SRC, B_SRC, C_SRC, DUP_SRC, SENTINEL_NAME,
                                 sha256_file)

COMPILE_TIMEOUT = 240

# Fake java that records argv and environment, then prints a report built
# from its own argv (--root/--scope/--dependency). An injected python
# mutation snippet runs before printing.
_ARGV_SHIM = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
root = scope = None
deps = []
i = 0
while i < len(args):
    a = args[i]
    if a == "--root":
        root = args[i + 1]; i += 2
    elif a == "--scope":
        scope = args[i + 1]; i += 2
    elif a == "--dependency":
        deps.append(args[i + 1]); i += 2
    else:
        i += 1
info = {"argv": sys.argv, "env_keys": sorted(os.environ),
        "home": os.environ.get("HOME"),
        "home_is_dir": os.path.isdir(os.environ.get("HOME", "")),
        "lc_all": os.environ.get("LC_ALL"),
        "path": os.environ.get("PATH")}
with open(__DUMP__, "w") as fh:
    json.dump(info, fh)
inputs = [{"path": root, "role": scope, "kind": "jar", "size_bytes": 1,
           "class_count": 0, "classes": [], "analyzed": True}]
inputs += [{"path": d, "role": "upstream", "kind": "jar", "size_bytes": 1,
            "class_count": 0, "classes": [], "analyzed": True} for d in deps]
report = {"schema": "mdi.usage-helper/1", "scope": scope, "root": root,
          "inputs": inputs, "refs": [], "target_owner_candidates": [],
          "issues": []}
__MUTATION__
print(json.dumps(report))
"""


def _run_build(cmd, timeout=COMPILE_TIMEOUT):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          timeout=timeout, env=child_env())


class UsageRunnerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mandatory dependency availability: never skip, raise an actionable
        # RuntimeError naming the env var, manifest or failing jar.
        cls.dep_dir = java_support.dependency_directory()
        cls.pins = java_support.verified_dependencies(cls.dep_dir)
        if not HELPER_SRC.is_file():
            raise AssertionError(f"BLOCKER: helper source missing: {HELPER_SRC}")
        cls.java = shutil.which("java")
        if cls.java is None or shutil.which("javac") is None or shutil.which("jar") is None:
            raise AssertionError("BLOCKER: java/javac/jar not available on PATH")

        cls.build_dir = Path(tempfile.mkdtemp(prefix="mdi-usage-runner-build-"))
        cls.fix = Path(tempfile.mkdtemp(prefix="mdi-usage-runner-fix-"))
        cls.addClassCleanup(shutil.rmtree, str(cls.build_dir), True)
        cls.addClassCleanup(shutil.rmtree, str(cls.fix), True)
        cls._build_helper()
        cls._build_fixtures()
        cls.cp_paths = [str(cls.helper_jar)] + [str(cls.dep_dir / n) for n in cls.pins]
        cls.expected = {p: sha256_file(p) for p in cls.cp_paths}

    @classmethod
    def _build_helper(cls):
        proc = _run_build(["javac", "-cp",
                           os.pathsep.join(str(cls.dep_dir / n) for n in cls.pins),
                           "-d", cls.build_dir, HELPER_SRC])
        if proc.returncode != 0:
            raise AssertionError(f"BLOCKER: javac failed for UsageHelper:\n{proc.stderr}")
        cls.helper_jar = cls.build_dir / "helper.jar"
        proc = _run_build(["jar", "--create", "--file", cls.helper_jar,
                           "-C", cls.build_dir, "."])
        if proc.returncode != 0:
            raise AssertionError(f"BLOCKER: packaging helper.jar failed:\n{proc.stderr}")

    @classmethod
    def _build_fixtures(cls):
        fix = cls.fix
        src = fix / "src"
        (src / "com/example/dup").mkdir(parents=True)
        cls.sentinel = fix / SENTINEL_NAME
        (src / "com/example/A.java").write_text(A_SRC.format(sentinel=cls.sentinel))
        (src / "com/example/B.java").write_text(B_SRC)
        (src / "com/example/C.java").write_text(C_SRC)
        (src / "com/example/dup/Dup.java").write_text(DUP_SRC)
        classes = fix / "classes"
        proc = _run_build(["javac", "--release", "17", "-d", classes]
                          + sorted(p for p in src.rglob("*.java")))
        if proc.returncode != 0:
            raise AssertionError(f"BLOCKER: fixture javac failed:\n{proc.stderr}")

        def jar_of(out, rel):
            stage = fix / f"stage-{out}"
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(classes / rel, stage / rel)
            proc = _run_build(["jar", "--create", "--file", fix / out, "-C", stage, "."])
            if proc.returncode != 0:
                raise AssertionError(f"BLOCKER: fixture jar {out} failed:\n{proc.stderr}")

        jar_of("root.jar", "com/example/A.class")
        jar_of("dep-b.jar", "com/example/B.class")
        jar_of("dep-c.jar", "com/example/C.class")
        jar_of("dup1.jar", "com/example/dup/Dup.class")
        jar_of("dup2.jar", "com/example/dup/Dup.class")
        cls.root_jar = fix / "root.jar"
        cls.dep_b = fix / "dep-b.jar"
        cls.dep_c = fix / "dep-c.jar"
        cls.dup1 = fix / "dup1.jar"
        cls.dup2 = fix / "dup2.jar"
        cls.exploded = fix / "exploded-root"
        (cls.exploded / "com/example").mkdir(parents=True)
        shutil.copy(classes / "com/example/A.class",
                    cls.exploded / "com/example/A.class")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mdi-usage-runner-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    # -- call helpers -----------------------------------------------------

    @staticmethod
    def canon(path):
        return str(Path(path).resolve())

    def run_runner(self, root=None, deps=None, scope="production", java_path=None,
                   cp=None, expected=None, **kw):
        return usage.run_usage_helper(
            self.root_jar if root is None else root,
            [self.dep_b] if deps is None else list(deps),
            scope=scope,
            helper_classpath=self.cp_paths if cp is None else cp,
            expected_hashes=dict(self.expected) if expected is None else expected,
            java_path=java_path,
            **kw)

    def codes(self, result):
        return {i["code"] for i in result["issues"] if i["source"] == "runner"}

    def run_complete(self, **kw):
        result = self.run_runner(**kw)
        self.assertEqual(result["status"], "complete",
                         msg=str(result["issues"]))
        return result

    # -- shim helpers -------------------------------------------------------

    def make_script(self, body, name="shim-java"):
        path = self.tmp / name
        path.write_text(body)
        path.chmod(0o700)
        return path

    def marker_shim(self, marker):
        return self.make_script(f'#!/bin/sh\nprintf ran > "{marker}"\nexit 0\n')

    def payload_shim(self, payload):
        body = "#!/bin/sh\ncat <<'MDIEOF'\n" + payload + "\nMDIEOF\n"
        return self.make_script(body, "payload-java")

    def argv_shim(self, dump, mutation=""):
        body = (_ARGV_SHIM.replace("__DUMP__", repr(str(dump)))
                .replace("__MUTATION__", mutation))
        return self.make_script(body, "argv-java")

    def arm_pid_cleanup(self, pidfile):
        def cleanup():
            try:
                pid = int(Path(pidfile).read_text().split()[0])
            except (OSError, ValueError, IndexError):
                return
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        self.addCleanup(cleanup)

    def read_pid(self, pidfile, budget=5.0):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if Path(pidfile).exists():
                return int(Path(pidfile).read_text().strip())
            time.sleep(0.05)
        self.fail(f"pid file {pidfile} never appeared")

    def assert_dead(self, pid, budget=5.0):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"process {pid} still alive after kill budget")

    # -- real helper ------------------------------------------------------

    def test_complete_report_real_helper(self):
        result = self.run_complete(root=self.root_jar, deps=[self.dep_b, self.dep_c])
        report = result["report"]
        self.assertEqual(report["schema"], "mdi.usage-helper/1")
        self.assertEqual(result["schema"], "mdi.usage-helper-runner/1")
        self.assertEqual(result["root"], self.canon(self.root_jar))
        self.assertEqual(result["helper_classpath"], [self.canon(p) for p in self.cp_paths])
        pairs = {(r["from_class"], r["to_class"]) for r in result["refs"]}
        self.assertIn(("com.example.A", "com.example.B"), pairs)
        self.assertIn(("com.example.B", "com.example.C"), pairs)
        self.assertNotIn(("com.example.A", "com.example.C"), pairs)
        triples = {(r["from_class"], r["to_class"], r["origin_input"]) for r in result["refs"]}
        self.assertIn(("com.example.B", "com.example.C", self.canon(self.dep_b)), triples)
        roles = [e["role"] for e in result["inputs"]]
        self.assertEqual(roles, ["production", "upstream", "upstream"])
        self.assertEqual(result["run"]["returncode"], 0)
        self.assertEqual(result["run"]["outcome"], "ok")
        self.assertNotIn("staged-evidence-changed", self.codes(result))
        self.assertEqual(report["issues"], [])
        self.assertEqual(result["tool"]["origin"], "path")
        self.assertEqual(result["tool"]["realpath"], self.canon(self.java))
        self.assertEqual(result["tool"]["sha256"], sha256_file(Path(self.java).resolve()))
        self.assertFalse(self.sentinel.exists())

    def test_duplicate_owner_candidates_both_listed(self):
        result = self.run_complete(deps=[self.dup1, self.dup2])
        candidates = {c["to_class"]: c["owner_candidates"]
                      for c in result["target_owner_candidates"]}
        self.assertEqual(sorted(candidates["com.example.dup.Dup"]),
                         sorted([self.canon(self.dup1), self.canon(self.dup2)]))

    def test_scope_test_marks_root_role(self):
        result = self.run_complete(scope="test")
        self.assertEqual(result["inputs"][0]["role"], "test")
        self.assertEqual(result["report"]["scope"], "test")
        self.assertEqual(result["inputs"][1]["role"], "upstream")

    def test_exploded_directory_root(self):
        result = self.run_complete(root=self.exploded, deps=[self.dep_b])
        self.assertEqual(result["inputs"][0]["kind"], "directory")
        self.assertEqual(result["inputs"][0]["classes"], ["com.example.A"])

    def test_java_exit2_maps_rejected(self):
        bad = self.tmp / "badroot"
        (bad / "com/example").mkdir(parents=True)
        shutil.copy(self.exploded / "com/example/A.class", bad / "com/example/A.class")
        os.symlink(self.dep_b, bad / "com/example/stray.class")
        result = self.run_runner(root=bad, deps=[self.dep_b])
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["report"])
        self.assertEqual(result["run"]["returncode"], 2)
        self.assertIn("helper-rejected", self.codes(result))
        message = next(i["message"] for i in result["issues"]
                       if i.get("code") == "helper-rejected")
        self.assertIn("symlink inside directory input", message)

    # -- rejections before any execution -----------------------------------

    def assert_never_executed(self, result, marker, status="rejected"):
        self.assertEqual(result["status"], status, msg=str(result["issues"]))
        self.assertIsNone(result["run"])
        self.assertFalse(Path(marker).exists())

    def test_invalid_scope_rejected_no_execution(self):
        marker = self.tmp / "marker"
        result = self.run_runner(scope="dev", java_path=self.marker_shim(marker))
        self.assert_never_executed(result, marker)
        self.assertIn("invalid-scope", self.codes(result))

    def test_duplicate_inputs_rejected_no_execution(self):
        marker = self.tmp / "marker"
        shim = self.marker_shim(marker)
        for deps in ([self.dep_b, self.dep_b], [self.root_jar]):
            with self.subTest(deps=[str(d) for d in deps]):
                result = self.run_runner(deps=deps, java_path=shim)
                self.assert_never_executed(result, marker)
                self.assertIn("duplicate-input", self.codes(result))

    def test_bad_input_paths_rejected_no_execution(self):
        marker = self.tmp / "marker"
        shim = self.marker_shim(marker)
        txt = self.tmp / "notajar.txt"
        txt.write_text("hello")
        fake_jar_dir = self.tmp / "looks.jar"
        fake_jar_dir.mkdir()
        link = self.tmp / "link.jar"
        os.symlink(self.dep_b, link)
        real_dir = self.tmp / "real"
        real_dir.mkdir()
        (real_dir / "x.jar").write_bytes(b"")
        ancestor_link = self.tmp / "ancestor-link"
        os.symlink(real_dir, ancestor_link)
        cases = [
            (self.tmp / "missing.jar", "invalid-root"),
            (txt, "invalid-root"),
            (fake_jar_dir, "invalid-root"),
            (link, "invalid-root"),
            (ancestor_link / "x.jar", "invalid-root"),
        ]
        for root, code in cases:
            with self.subTest(root=str(root)):
                result = self.run_runner(root=root, java_path=shim)
                self.assert_never_executed(result, marker)
                self.assertIn(code, self.codes(result))
        result = self.run_runner(deps=[self.tmp / "gone.jar"], java_path=shim)
        self.assert_never_executed(result, marker)
        self.assertIn("invalid-dependency", self.codes(result))

    def test_classpath_components_rejected_no_execution(self):
        marker = self.tmp / "marker"
        shim = self.marker_shim(marker)
        txt = self.tmp / "plain.txt"
        txt.write_text("nope")
        link = self.tmp / "cp-link.jar"
        os.symlink(self.helper_jar, link)
        cases = [
            ([self.tmp / "missing.jar"], "invalid-classpath"),
            ([self.dep_dir], "invalid-classpath"),
            ([txt], "invalid-classpath"),
            ([link], "invalid-classpath"),
            ([""], "invalid-classpath"),
            ([self.helper_jar, self.helper_jar], "invalid-classpath"),
            ([], "invalid-classpath"),
        ]
        for cp, code in cases:
            with self.subTest(cp=[str(c) for c in cp]):
                result = self.run_runner(cp=cp, expected={}, java_path=shim)
                self.assert_never_executed(result, marker)
                self.assertIn(code, self.codes(result))

    def test_hash_gate_rejects_without_execution(self):
        marker = self.tmp / "marker"
        shim = self.marker_shim(marker)
        first = self.canon(self.cp_paths[0])
        wrong = dict(self.expected)
        wrong[first] = "0" * 64
        cases = [
            (wrong, "hash-mismatch"),
            ({**self.expected, first: self.expected[first].upper()}, "invalid-expected-hashes"),
            ({**self.expected, first: self.expected[first][:63]}, "invalid-expected-hashes"),
            ({k: v for k, v in self.expected.items() if k != first}, "missing-expected-hash"),
            ({**self.expected, str(self.tmp / "not-on-cp.jar"): "0" * 64},
             "unexpected-expected-hash"),
        ]
        for expected, code in cases:
            with self.subTest(code=code):
                result = self.run_runner(expected=expected, java_path=shim)
                self.assert_never_executed(result, marker)
                self.assertIn(code, self.codes(result))

    def test_argument_type_errors(self):
        with self.assertRaises(TypeError):
            self.run_runner(scope=3)
        with self.assertRaises(TypeError):
            usage.run_usage_helper(b"/bytes/root", [], scope="production",
                                   helper_classpath=self.cp_paths,
                                   expected_hashes=self.expected)
        with self.assertRaises(TypeError):
            usage.run_usage_helper(self.root_jar, "dep", scope="production",
                                   helper_classpath=self.cp_paths,
                                   expected_hashes=self.expected)
        with self.assertRaises(TypeError):
            self.run_runner(cp="a:b")
        with self.assertRaises(TypeError):
            self.run_runner(timeout_seconds="5")
        with self.assertRaises(ValueError):
            self.run_runner(timeout_seconds=0)
        with self.assertRaises(TypeError):
            self.run_runner(heap_mb=True)
        with self.assertRaises(ValueError):
            self.run_runner(max_output_bytes=0)
        with self.assertRaises(TypeError):
            self.run_runner(expected=[])

    # -- java executable boundary -------------------------------------------

    def test_java_not_found_unavailable(self):
        empty = self.tmp / "empty-bin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            result = self.run_runner()
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["tool"])
        self.assertIsNone(result["run"])
        self.assertIn("java-unavailable", self.codes(result))

    def test_java_path_constraints_rejected(self):
        marker = self.tmp / "marker"
        inside = self.exploded / "project-java"
        inside.write_text("#!/bin/sh\n")
        inside.chmod(0o700)
        plain = self.tmp / "not-executable"
        plain.write_text("#!/bin/sh\n")
        plain.chmod(0o644)
        cases = [
            ("relative-java", "java-rejected"),
            (self.tmp / "no-such-java", "java-rejected"),
            (self.tmp, "java-rejected"),
            (plain, "java-rejected"),
            (inside, "java-rejected"),
        ]
        for java_path, code in cases:
            with self.subTest(java_path=str(java_path)):
                result = self.run_runner(root=self.exploded, java_path=java_path)
                self.assertEqual(result["status"], "rejected", msg=str(result["issues"]))
                self.assertIsNone(result["run"])
                self.assertIn(code, self.codes(result))
        self.assertFalse(Path(marker).exists())

    # -- environment and argv ------------------------------------------------

    def test_child_environment_is_exact_allowlist(self):
        dump = self.tmp / "env.json"
        result = self.run_runner(java_path=self.argv_shim(dump))
        info = json.loads(dump.read_text())
        self.assertEqual(info["env_keys"], ["HOME", "LC_ALL", "PATH"])
        self.assertEqual(info["lc_all"], "C")
        self.assertTrue(info["home_is_dir"])
        self.assertNotEqual(info["home"], os.environ.get("HOME"))
        self.assertTrue(all(os.path.isabs(part)
                            for part in info["path"].split(os.pathsep) if part))
        # the shim printed a valid report built from argv, so this completed
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))

    def test_argv_is_exact_bounded_invocation(self):
        dump = self.tmp / "argv.json"
        deps = [self.dep_c, self.dep_b]
        self.run_runner(deps=deps, java_path=self.argv_shim(dump), heap_mb=96)
        argv = json.loads(dump.read_text())["argv"]
        self.assertEqual(argv[:7], [
            str(self.tmp / "argv-java"),
            "-Xmx96m",
            "-XX:+ExitOnOutOfMemoryError",
            "-cp",
            os.pathsep.join(self.canon(p) for p in self.cp_paths),
            "mdi.UsageHelper",
            "--root",
        ])
        self.assertEqual(argv[7:], [
            self.canon(self.root_jar),
            "--scope", "production",
            "--dependency", self.canon(self.dep_c),
            "--dependency", self.canon(self.dep_b),
        ])

    def test_inherited_jvm_option_env_stripped(self):
        poison = {
            "JAVA_TOOL_OPTIONS": "-javaagent:/nonexistent-agent.jar",
            "JDK_JAVA_OPTIONS": "-Xmx8m",
            "_JAVA_OPTIONS": "-Xmx8m",
            "CLASSPATH": "/nonexistent-classpath",
        }
        with mock.patch.dict(os.environ, poison):
            result = self.run_complete()
        self.assertFalse(self.sentinel.exists())

    # -- bounded execution failure paths --------------------------------------

    def test_output_cap_bounds_and_kills(self):
        pidfile = self.tmp / "pid"
        shim = self.make_script(
            f'#!/bin/sh\necho $$ > "{pidfile}"\n'
            "dd if=/dev/zero bs=1024 count=64 2>/dev/null\nsleep 30\n",
            "runaway-java")
        self.arm_pid_cleanup(pidfile)
        result = self.run_runner(java_path=shim, timeout_seconds=30,
                                 max_output_bytes=2048)
        self.assertEqual(result["status"], "failed")
        self.assertIn("helper-output-capped", self.codes(result))
        self.assertEqual(result["run"]["outcome"], "capped")
        self.assertLessEqual(result["run"]["stdout_bytes"], 2049)
        self.assert_dead(self.read_pid(pidfile))

    def test_timeout_kills_process_group_with_descendant(self):
        leader = self.tmp / "leader.pid"
        child = self.tmp / "child.pid"
        shim = self.make_script(
            f'#!/bin/sh\necho $$ > "{leader}"\nsleep 30 &\necho $! > "{child}"\nsleep 30\n',
            "hanging-java")
        self.arm_pid_cleanup(leader)
        self.arm_pid_cleanup(child)
        result = self.run_runner(java_path=shim, timeout_seconds=2)
        self.assertEqual(result["status"], "failed")
        self.assertIn("helper-timeout", self.codes(result))
        self.assertEqual(result["run"]["outcome"], "timeout")
        self.assertEqual(result["run"]["kill_state"], "sent")
        self.assert_dead(self.read_pid(leader))
        self.assert_dead(self.read_pid(child))

    def test_helper_exit1_maps_failed(self):
        shim = self.make_script("#!/bin/sh\necho boom >&2\nexit 1\n", "exit1-java")
        result = self.run_runner(java_path=shim)
        self.assertEqual(result["status"], "failed")
        self.assertIn("helper-failed", self.codes(result))
        self.assertEqual(result["run"]["returncode"], 1)
        self.assertIsNone(result["report"])

    # -- output validation ------------------------------------------------------

    def test_malformed_output_maps_failed(self):
        duplicate_keys = ('{"schema": "mdi.usage-helper/1", "schema": "mdi.usage-helper/1",'
                          ' "scope": "production", "root": "/x", "inputs": [], "refs": [],'
                          ' "target_owner_candidates": [], "issues": []}')
        foreign_schema = ('{"schema": "mdi.usage-helper/2", "scope": "production",'
                          ' "root": "/x", "inputs": [], "refs": [],'
                          ' "target_owner_candidates": [], "issues": []}')
        payloads = ["not json at all", duplicate_keys, foreign_schema, "[]", ""]
        for payload in payloads:
            with self.subTest(payload=payload[:40]):
                result = self.run_runner(java_path=self.payload_shim(payload))
                self.assertEqual(result["status"], "failed")
                self.assertIn("helper-output-invalid", self.codes(result))
                self.assertIsNone(result["report"])

    def test_report_schema_violations_map_failed(self):
        dump = self.tmp / "dump.json"
        mutations = {
            "foreign-ref-origin":
                'report["refs"] = [{"from_class": "x.A", "to_class": "x.B",'
                ' "origin_input": "/foreign.jar"}]',
            "unanalyzed-origin":
                'report["refs"] = [{"from_class": "x.A", "to_class": "x.B",'
                ' "origin_input": root}]\ninputs[0]["analyzed"] = False',
            "foreign-owner":
                'report["refs"] = [{"from_class": "x.A", "to_class": "x.B",'
                ' "origin_input": root}]\nreport["target_owner_candidates"] ='
                ' [{"to_class": "x.B", "owner_candidates": ["/foreign.jar"]}]',
            "owner-lacks-class":
                'report["refs"] = [{"from_class": "x.A", "to_class": "x.B",'
                ' "origin_input": root}]\nreport["target_owner_candidates"] ='
                ' [{"to_class": "x.B", "owner_candidates": [root]}]',
            "orphan-candidate":
                'report["target_owner_candidates"] = [{"to_class": "x.Z",'
                ' "owner_candidates": []}]',
            "extra-key": 'report["extra"] = 1',
            "wrong-type": 'inputs[0]["class_count"] = "0"',
            "bool-not-int": 'inputs[0]["size_bytes"] = True',
        }
        for name, mutation in mutations.items():
            with self.subTest(mutation=name):
                result = self.run_runner(java_path=self.argv_shim(dump, mutation))
                self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
                self.assertIn("helper-output-invalid", self.codes(result))
                self.assertIsNone(result["report"])

    def test_ref_from_class_must_be_in_origin_index(self):
        dump = self.tmp / "dump.json"
        mutation = ('report["refs"] = [{"from_class": "x.A", "to_class": "x.B",'
                    ' "origin_input": root}]')
        result = self.run_runner(java_path=self.argv_shim(dump, mutation))
        self.assertEqual(result["status"], "failed")
        message = next(i["message"] for i in result["issues"]
                       if i["code"] == "helper-output-invalid")
        self.assertIn("from_class", message)
        self.assertIn("class index", message)

    def test_malformed_origin_and_owner_types_fail_structured(self):
        dump = self.tmp / "dump.json"
        ref_origin_root = ('report["refs"] = [{"from_class": "x.A", "to_class": "x.B",'
                           ' "origin_input": root}]')
        owners = ('report["target_owner_candidates"] ='
                  ' [{"to_class": "x.B", "owner_candidates": %s}]')
        mutations = {
            "origin-list": ('report["refs"] = [{"from_class": "x.A",'
                            ' "to_class": "x.B", "origin_input": ["x"]}]'),
            "origin-dict": ('report["refs"] = [{"from_class": "x.A",'
                            ' "to_class": "x.B", "origin_input": {"p": 1}}]'),
            "origin-int": ('report["refs"] = [{"from_class": "x.A",'
                           ' "to_class": "x.B", "origin_input": 42}]'),
            "origin-none": ('report["refs"] = [{"from_class": "x.A",'
                            ' "to_class": "x.B", "origin_input": None}]'),
            "owners-list": ref_origin_root + "\n" + owners % '[["x"]]',
            "owners-dict": ref_origin_root + "\n" + owners % '[{"p": "x"}]',
            "owners-int": ref_origin_root + "\n" + owners % "[42]",
            "owners-none": ref_origin_root + "\n" + owners % "[None]",
            "issue-input-list": ('report["issues"] = [{"code": "c", "message": "m",'
                                 ' "input": ["x"]}]'),
            "refs-not-list": 'report["refs"] = "nope"',
            "owners-not-list": ('report["target_owner_candidates"] ='
                                ' [{"to_class": "x.B", "owner_candidates": "nope"}]'),
        }
        for name, mutation in mutations.items():
            with self.subTest(mutation=name):
                result = self.run_runner(java_path=self.argv_shim(dump, mutation))
                self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
                self.assertIn("helper-output-invalid", self.codes(result))
                self.assertIsNone(result["report"])

    def test_budget_bounds_rejected_before_expensive_loops(self):
        dump = self.tmp / "dump.json"
        ref_origin_root = ('report["refs"] = [{"from_class": "x.A", "to_class": "x.B",'
                           ' "origin_input": root}]')
        nine_inputs = ('report["inputs"] = [{**inputs[0], "path": f"/fake-{i}.jar",'
                       ' "classes": [f"c{j:05d}" for j in range(8192)],'
                       ' "class_count": 8192} for i in range(9)]')
        cases = {
            "inputs-over-64": (
                'report["inputs"] = [{**inputs[0], "path": f"/fake-{i}.jar"}'
                ' for i in range(65)]',
                "exceeds helper budget 64", "expected staged"),
            "classes-over-8192": (
                'inputs[0]["classes"] = [f"c{i:05d}" for i in range(8193)]\n'
                'inputs[0]["class_count"] = 8193',
                "exceeds helper budget 8192", "keys mismatch"),
            "total-over-65536": (
                nine_inputs, "exceeds helper budget 65536", "expected staged"),
            "refs-over-262144": (
                'report["refs"] = [{"garbage": i} for i in range(262145)]',
                "exceeds helper budget 262144", "keys mismatch"),
            "candidates-over-262144": (
                'report["target_owner_candidates"] = [{"garbage": i}'
                ' for i in range(262145)]',
                "exceeds helper budget 262144", "keys mismatch"),
            "owners-over-64": (
                ref_origin_root + '\nreport["target_owner_candidates"] ='
                ' [{"to_class": "x.B", "owner_candidates":'
                ' [f"/fake-{i}.jar" for i in range(65)]}]',
                "exceeds helper budget 64", "staged input"),
        }
        for name, (mutation, needle, absent) in cases.items():
            with self.subTest(budget=name):
                kwargs = {"max_output_bytes": 64 * 1024 * 1024} \
                    if "262144" in needle else {}
                result = self.run_runner(java_path=self.argv_shim(dump, mutation),
                                         **kwargs)
                self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
                message = next(i["message"] for i in result["issues"]
                               if i["code"] == "helper-output-invalid")
                self.assertIn(needle, message)
                self.assertNotIn(absent, message)

    def test_duplicate_target_candidates_rejected_no_silent_merge(self):
        dump = self.tmp / "dump.json"
        mutation = ('inputs[0]["classes"] = ["x.A"]\n'
                    'inputs[0]["class_count"] = 1\n'
                    'report["refs"] = [{"from_class": "x.A", "to_class": "x.A",'
                    ' "origin_input": root}]\nreport["target_owner_candidates"] ='
                    ' [{"to_class": "x.A", "owner_candidates": [root]},'
                    ' {"to_class": "x.A", "owner_candidates": []}]')
        result = self.run_runner(java_path=self.argv_shim(dump, mutation))
        self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
        message = next(i["message"] for i in result["issues"]
                       if i["code"] == "helper-output-invalid")
        self.assertIn("duplicate", message)

    def test_diagnostic_cap_accumulation_bounded(self):
        """Many invalid entries surface at most 20 detailed problems plus the
        single suppression summary, never an unbounded error list."""
        dump = self.tmp / "dump.json"
        for count in (60, 150):
            with self.subTest(invalid_entries=count):
                mutation = ('report["refs"] = [{"garbage": i} for i in range(%d)]'
                            % count)
                result = self.run_runner(java_path=self.argv_shim(dump, mutation))
                self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
                message = next(i["message"] for i in result["issues"]
                               if i["code"] == "helper-output-invalid")
                self.assertIn("ref 0 keys mismatch", message)
                # 20 capped problems + 1 suppression summary == exactly 21
                # surfaced entries; the count never grows with the payload.
                self.assertIn("(+20 more)", message)
                self.assertNotIn(f"(+{count - 1} more)", message)

    def test_validation_deadline_expiry_mid_loop_fails_structured(self):
        """A clock that advances one second per monotonic call expires the
        Python-side validation budget mid-loop (timeout_seconds=30 anchors
        the deadline at call k, so the trip point is invariant to how many
        calls the bounded run itself consumed); the public result is a
        structured failed with no report and a truthful partial message."""
        dump = self.tmp / "dump.json"
        mutation = ('inputs[0]["classes"] = ["x.A"]\n'
                    'inputs[0]["class_count"] = 1\n'
                    'report["refs"] = [{"from_class": "x.A", "to_class": "x.B",'
                    ' "origin_input": root} for _ in range(50)]')
        shim = self.argv_shim(dump, mutation)
        clock = itertools.count()
        with mock.patch.object(usage.time, "monotonic", side_effect=lambda: next(clock)):
            result = self.run_runner(java_path=shim)
        self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
        self.assertIsNone(result["report"])
        message = next(i["message"] for i in result["issues"]
                       if i["code"] == "helper-output-invalid")
        match = re.search(r"exhausted at ref (\d+) of 50; remaining refs unchecked",
                          message)
        self.assertIsNotNone(match, message)
        self.assertTrue(1 <= int(match.group(1)) < 50, message)

    def test_deeply_nested_payload_recursion_normalized(self):
        """A ~200KB payload of 100000 nested arrays trips this interpreter's
        JSON-decoder recursion limit (probed: 50000 parses, 100000 raises
        RecursionError); the public runner must return a structured failed
        result, not let RecursionError escape."""
        payload = "[" * 100_000 + "]" * 100_000
        result = self.run_runner(java_path=self.payload_shim(payload))
        self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
        self.assertIsNone(result["report"])
        message = next(i["message"] for i in result["issues"]
                       if i["code"] == "helper-output-invalid")
        self.assertIn("recursion depth", message)

    def test_issue_count_over_helper_ceiling_rejected_early(self):
        """UsageHelper.java emits at most one issue per input (inputs <= 64),
        so 65 issues is over the real generation ceiling and must be refused
        in the counting pre-pass, before any per-issue work."""
        dump = self.tmp / "dump.json"
        mutation = ('report["issues"] = [{"garbage": 0}] +'
                    ' [{"code": "c%d" % i, "message": "m", "input": root}'
                    ' for i in range(64)]')
        result = self.run_runner(java_path=self.argv_shim(dump, mutation))
        self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
        message = next(i["message"] for i in result["issues"]
                       if i["code"] == "helper-output-invalid")
        self.assertIn("exceeds helper budget 64", message)
        self.assertNotIn("keys mismatch", message)  # early: no per-issue work ran

    def test_validation_deadline_expires_mid_issues(self):
        """The same linear mock clock expires the validation budget inside
        the helper-issues loop; the result is failed with no report and a
        truthful partial message."""
        dump = self.tmp / "dump.json"
        mutation = ('report["issues"] = [{"code": "c", "message": "m",'
                    ' "input": root} for _ in range(50)]')
        clock = itertools.count()
        with mock.patch.object(usage.time, "monotonic", side_effect=lambda: next(clock)):
            result = self.run_runner(java_path=self.argv_shim(dump, mutation))
        self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
        self.assertIsNone(result["report"])
        message = next(i["message"] for i in result["issues"]
                       if i["code"] == "helper-output-invalid")
        match = re.search(r"exhausted at issue (\d+) of 50; remaining issues"
                          r" unchecked", message)
        self.assertIsNotNone(match, message)
        self.assertTrue(1 <= int(match.group(1)) < 50, message)

    def test_well_formed_fake_report_accepted(self):
        dump = self.tmp / "dump.json"
        result = self.run_runner(java_path=self.argv_shim(dump))
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))
        self.assertEqual(result["inputs"][0]["path"], self.canon(self.root_jar))

    def test_staged_evidence_change_flagged_after_run(self):
        dump = self.tmp / "dump.json"
        mutable = self.tmp / "helper-copy.jar"
        shutil.copy(self.helper_jar, mutable)
        cp = [str(mutable)] + self.cp_paths[1:]
        expected = {p: sha256_file(p) for p in cp}
        mutation = (f'open({str(mutable)!r}, "ab").write(b"x")\n'
                    f'import os as _os\n_os.utime(root)')
        result = self.run_runner(cp=cp, expected=expected,
                                 java_path=self.argv_shim(dump, mutation))
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))
        self.assertIn("staged-evidence-changed", self.codes(result))

    # -- classpath separator / wildcard refusal (JVM -cp split defense) -------

    def test_classpath_separator_and_wildcard_components_rejected(self):
        """A component whose filename (or an ancestor directory name)
        contains the classpath separator is hash-verified as ONE file but
        the JVM -cp would split it into unlisted, unhashed entries; '*' is
        the JVM wildcard. All are refused before any process."""
        marker = self.tmp / "marker"
        shim = self.marker_shim(marker)
        colon_file = self.tmp / "unlisted.jar:trailer.jar"
        shutil.copy(self.helper_jar, colon_file)
        colon_dir = self.tmp / "co:lon"
        colon_dir.mkdir()
        colon_ancestor = colon_dir / "helper.jar"
        shutil.copy(self.helper_jar, colon_ancestor)
        wildcard = self.tmp / "wild*.jar"
        shutil.copy(self.helper_jar, wildcard)
        for cp in ([str(colon_file)] + self.cp_paths[1:],
                   [str(colon_ancestor)] + self.cp_paths[1:],
                   [str(wildcard)] + self.cp_paths[1:]):
            with self.subTest(cp=cp):
                expected = {p: sha256_file(p) for p in cp}
                result = self.run_runner(cp=cp, expected=expected, java_path=shim)
                self.assert_never_executed(result, marker)
                self.assertIn("invalid-classpath", self.codes(result))
                message = next(i["message"] for i in result["issues"]
                               if i["code"] == "invalid-classpath")
                self.assertIn("classpath separator" if ":" in cp[0] else "'*'",
                              message)

    def test_classpath_separator_refused_by_option_preflight(self):
        refusal = usage.validate_usage_options(
            "production", ["/abs/unlisted.jar:trailer.jar"], {})
        self.assertEqual(refusal["code"], "invalid-classpath")
        self.assertIn("classpath separator", refusal["message"])
        refusal = usage.validate_usage_options("production", ["/abs/wild*.jar"], {})
        self.assertEqual(refusal["code"], "invalid-classpath")
        self.assertIn("'*'", refusal["message"])

    def test_real_jvm_colon_classpath_exploit_never_executes(self):
        """End-to-end exploit regression with the real JVM: the literal file
        a.jar:b.jar is hash-verified as one component, but an unlisted
        sibling a.jar containing a hostile mdi.UsageHelper would be loaded
        by the JVM's -cp split. The runner must refuse before any exec, so
        the hostile marker is never written."""
        marker = self.tmp / "pwned-marker"
        evil_src = self.tmp / "UsageHelper.java"
        evil_src.write_text(
            "package mdi;\n"
            "public class UsageHelper {\n"
            "    public static void main(String[] args) throws Exception {\n"
            f"        java.nio.file.Files.write(java.nio.file.Paths.get(\"{marker}\"),"
            " new byte[] {1});\n"
            "        System.out.println(\"PWNED\");\n"
            "    }\n"
            "}\n")
        build = self.tmp / "evil-build"
        proc = _run_build(["javac", "-d", build, evil_src])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = _run_build(["jar", "--create", "--file", self.tmp / "a.jar",
                           "-C", build, "."])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        colon_name = self.tmp / "a.jar:b.jar"
        shutil.copy(self.helper_jar, colon_name)
        cp = [str(colon_name)] + self.cp_paths[1:]
        expected = {p: sha256_file(p) for p in cp}
        result = self.run_runner(cp=cp, expected=expected)  # real JVM
        self.assertEqual(result["status"], "rejected", msg=str(result["issues"]))
        self.assertIn("invalid-classpath", self.codes(result))
        self.assertIsNone(result["run"])
        self.assertFalse(marker.exists())

    def test_malformed_kind_types_fail_structured(self):
        """Report input kind=[] / {} previously leaked TypeError at the
        _KINDS membership check; every non-string kind must fail
        structured."""
        dump = self.tmp / "dump.json"
        for bad in ("[]", "{}", "None", "42"):
            with self.subTest(kind=bad):
                mutation = f'inputs[0]["kind"] = {bad}'
                result = self.run_runner(java_path=self.argv_shim(dump, mutation))
                self.assertEqual(result["status"], "failed", msg=str(result["issues"]))
                self.assertIn("helper-output-invalid", self.codes(result))
                self.assertIsNone(result["report"])

    # -- shared option preflight (TASK-21.6) ----------------------------------

    def test_validate_usage_options_pure_shape_contract(self):
        # Pure shape contract: nonexistent classpath paths pass because the
        # preflight never touches the filesystem, and the one-to-one digest
        # mapping is deliberately NOT decided here (that needs canonical
        # component paths only the runner can derive).
        missing = "/nonexistent-preflight/does-not-exist.jar"
        digest = "0" * 64
        # Success returns the one materialized re-iterable component copy.
        self.assertEqual(usage.validate_usage_options(
            "production", [missing], {missing: digest}), [missing])
        # A well-formed extra entry is shape-valid: only the runner, which
        # canonicalizes components, can call it unexpected.
        self.assertEqual(usage.validate_usage_options(
            "production", [missing], {missing: digest, "/other.jar": digest}),
            [missing])
        cases = [
            (("dev", [missing], {missing: digest}), "invalid-scope"),
            (("production", [], {}), "invalid-classpath"),
            (("production", [""], {missing: digest}), "invalid-classpath"),
            (("production", [missing, ""], {missing: digest}),
             "invalid-classpath"),
            (("production", [missing], {missing: "g" * 64}),
             "invalid-expected-hashes"),
            (("production", [missing], {missing: digest[:63]}),
             "invalid-expected-hashes"),
            (("production", [missing], {missing: 1}),
             "invalid-expected-hashes"),
        ]
        for args, code in cases:
            with self.subTest(code=code):
                refusal = usage.validate_usage_options(*args)
                self.assertEqual(set(refusal), {"code", "message"})
                self.assertEqual(refusal["code"], code)
        refusal = usage.validate_usage_options(
            "production", [missing], {missing: digest}, java_path="relative-java")
        self.assertEqual(refusal["code"], "java-rejected")
        for kwargs, exc_type in [
                ({"scope": 3}, TypeError),
                ({"timeout_seconds": 0}, ValueError),
                ({"timeout_seconds": "5"}, TypeError),
                ({"heap_mb": True}, TypeError),
                ({"max_output_bytes": 0}, ValueError),
                ({"java_path": b"/bin/java"}, TypeError)]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(exc_type):
                    usage.validate_usage_options(
                        "production", [missing], {missing: digest}, **kwargs)
        for bad_cp in ("a:b", b"a:b", 5):
            with self.subTest(bad_cp=bad_cp):
                with self.assertRaises(TypeError):
                    usage.validate_usage_options(
                        "production", bad_cp, {missing: digest})
        with self.assertRaises(TypeError):
            usage.validate_usage_options("production", [missing], [])
        with self.assertRaises(TypeError):
            usage.validate_usage_options("production", [missing], {5: digest})

    def test_preflight_refusal_is_the_runners_own_refusal(self):
        # Single source of truth: the preflight returns exactly the code and
        # message the runner itself reports for the same invalid option.
        marker = self.tmp / "marker"
        shim = self.marker_shim(marker)
        runner_result = self.run_runner(scope="dev", java_path=shim)
        self.assert_never_executed(runner_result, marker)
        refusal = usage.validate_usage_options(
            "dev", self.cp_paths, self.expected)
        runner_issue = next(issue for issue in runner_result["issues"]
                            if issue["source"] == "runner")
        self.assertEqual((refusal["code"], refusal["message"]),
                         (runner_issue["code"], runner_issue["message"]))

    def test_passing_preflight_is_not_authorization(self):
        # A passing preflight on the original bytes authorizes nothing: the
        # runner re-hashes at execution time, so bytes changed afterwards
        # are still refused before any JVM start. The mutated copy lives in
        # the per-test tmp dir, so no shared fixture jar is touched.
        mutable = self.tmp / "helper-copy.jar"
        shutil.copy(self.cp_paths[0], mutable)
        cp = [str(mutable)] + self.cp_paths[1:]
        expected = {p: sha256_file(p) for p in cp}
        self.assertEqual(usage.validate_usage_options(
            "production", cp, expected), cp)
        with open(mutable, "ab") as fh:
            fh.write(b"changed-after-preflight")
        result = self.run_runner(cp=cp, expected=expected)
        self.assertEqual(result["status"], "rejected", msg=str(result["issues"]))
        self.assertIn("hash-mismatch", self.codes(result))
        self.assertIsNone(result["run"])

    def test_one_shot_classpath_iterable_still_runs_once(self):
        # The shared shape check materializes the components exactly once
        # and the runner validates that copy, so a one-shot iterable is not
        # exhausted between the preflight and the classpath validation.
        result = self.run_runner(cp=iter(self.cp_paths), expected=self.expected)
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))


if __name__ == "__main__":
    unittest.main()
