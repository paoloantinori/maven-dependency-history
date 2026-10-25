"""Tests for the Java mdi.UsageHelper (TASK-21 bounded usage-evidence slice).

The helper is compiled once per test class against the verified analyzer jars
and fixtures (tiny A/B/C classes) are built once with javac --release 17 into a
temp dir. The jars come from MDI_TEST_DEPENDENCY_DIR and are verified against
the pin manifest by tests/java_support.py in setUpClass; nothing is
downloaded. No network, no Maven, and analyzed classes are never executed
(the sentinel test proves no class loading).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER_SRC = (REPO / "skills" / "maven-dependency-investigator" / "helper"
              / "src" / "main" / "java" / "mdi" / "UsageHelper.java")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import java_support
from tests.java_fixtures import (A_SRC, B_SRC, C_SRC, DUP_SRC, MR_BASE_SRC,
                                 MR_V_SRC, SENTINEL_NAME, sha256_file)

RUN_TIMEOUT = 120
COMPILE_TIMEOUT = 240
MAX_STDOUT_BYTES = 8 * 1024 * 1024


def _run(cmd, timeout=COMPILE_TIMEOUT, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                          env=java_support.child_env())


class UsageHelperTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mandatory dependency availability: never skip, raise an actionable
        # RuntimeError naming the env var, manifest or failing jar.
        cls.dep_dir = java_support.dependency_directory()
        cls.pins = java_support.verified_dependencies(cls.dep_dir)
        cls.jar_cp = os.pathsep.join(str(cls.dep_dir / name) for name in cls.pins)
        if not HELPER_SRC.is_file():
            raise AssertionError(f"BLOCKER: helper source missing (RED phase): {HELPER_SRC}")

        cls.build_dir = Path(tempfile.mkdtemp(prefix="mdi-usage-build-"))
        cls.fix = Path(tempfile.mkdtemp(prefix="mdi-usage-fix-"))
        cls.addClassCleanup(shutil.rmtree, str(cls.build_dir), True)
        cls.addClassCleanup(shutil.rmtree, str(cls.fix), True)
        cls._compile_helper()
        cls._build_fixtures()

    # -- one-time builds -------------------------------------------------

    @classmethod
    def _compile_helper(cls):
        proc = _run(["javac", "-cp", cls.jar_cp, "-d", str(cls.build_dir), str(HELPER_SRC)])
        if proc.returncode != 0:
            raise AssertionError(f"BLOCKER: javac failed for UsageHelper:\n{proc.stderr}")

    @classmethod
    def _build_fixtures(cls):
        fix = cls.fix
        src = fix / "src"
        (src / "com/example").mkdir(parents=True)
        (src / "com/example/dup").mkdir()
        (src / "com/example/mr").mkdir()
        cls.sentinel = fix / SENTINEL_NAME
        (src / "com/example/A.java").write_text(A_SRC.format(sentinel=cls.sentinel))
        (src / "com/example/B.java").write_text(B_SRC)
        (src / "com/example/C.java").write_text(C_SRC)
        (src / "com/example/dup/Dup.java").write_text(DUP_SRC)
        (src / "com/example/mr/Base.java").write_text(MR_BASE_SRC)
        (src / "com/example/mr/V.java").write_text(MR_V_SRC)
        classes = fix / "classes"
        proc = _run(["javac", "--release", "17", "-d", str(classes)] +
                    [str(p) for p in sorted(src.rglob("*.java"))])
        if proc.returncode != 0:
            raise AssertionError(f"BLOCKER: fixture javac failed:\n{proc.stderr}")

        def stage(rel, into):
            dst = fix / "stage" / into / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(classes / rel, dst)
            return fix / "stage" / into

        def jar(out, stage_dir, manifest=None):
            cmd = ["jar", "--create", "--file", str(fix / out)]
            if manifest:
                cmd += ["--manifest", str(manifest)]
            cmd += ["-C", str(stage_dir), "."]
            proc = _run(cmd)
            if proc.returncode != 0:
                raise AssertionError(f"BLOCKER: fixture jar {out} failed:\n{proc.stderr}")

        jar("root.jar", stage("com/example/A.class", "root"))
        jar("dep-b.jar", stage("com/example/B.class", "depb"))
        jar("dep-c.jar", stage("com/example/C.class", "depc"))
        jar("dup1.jar", stage("com/example/dup/Dup.class", "dup"))
        jar("dup2.jar", stage("com/example/dup/Dup.class", "dup"))
        cls.exploded = fix / "exploded"
        shutil.copytree(fix / "stage/root", cls.exploded)

        mr_stage = fix / "stage/mr"
        (mr_stage / "com/example/mr").mkdir(parents=True)
        shutil.copy(classes / "com/example/mr/Base.class", mr_stage / "com/example/mr/Base.class")
        v17 = mr_stage / "META-INF/versions/17/com/example/mr"
        v17.mkdir(parents=True)
        shutil.copy(classes / "com/example/mr/V.class", v17 / "V.class")
        mr_manifest = fix / "mr.mf"
        mr_manifest.write_text("Multi-Release: true\n")
        jar("mr.jar", mr_stage, mr_manifest)

        mm_stage = fix / "stage/mr-manifest-only"
        (mm_stage / "com/example/mr").mkdir(parents=True)
        shutil.copy(classes / "com/example/mr/Base.class",
                    mm_stage / "com/example/mr/Base.class")
        mm_manifest = fix / "mr-manifest.mf"
        mm_manifest.write_text("Multi-Release: true\n")
        jar("mr-manifest-only.jar", mm_stage, mm_manifest)
        cls.mr_manifest_jar = fix / "mr-manifest-only.jar"

        cls.mr_exploded = fix / "exploded-mr"
        (cls.mr_exploded / "com/example/mr").mkdir(parents=True)
        shutil.copy(classes / "com/example/mr/Base.class",
                    cls.mr_exploded / "com/example/mr/Base.class")
        v17dir = cls.mr_exploded / "META-INF/versions/17/com/example/mr"
        v17dir.mkdir(parents=True)
        shutil.copy(classes / "com/example/mr/V.class", v17dir / "V.class")

        cls.root_jar = fix / "root.jar"
        cls.dep_b = fix / "dep-b.jar"
        cls.dep_c = fix / "dep-c.jar"
        cls.dup1 = fix / "dup1.jar"
        cls.dup2 = fix / "dup2.jar"
        cls.mr_jar = fix / "mr.jar"

    # -- subprocess helpers ----------------------------------------------

    def run_helper(self, *args, cwd=None, props=()):
        cp = os.pathsep.join([str(self.build_dir), self.jar_cp])
        cmd = ["java", *[f"-D{p}" for p in props], "-cp", cp,
               "mdi.UsageHelper", *[str(a) for a in args]]
        proc = _run(cmd, timeout=RUN_TIMEOUT, cwd=cwd)
        self.assertLessEqual(len(proc.stdout.encode()), MAX_STDOUT_BYTES)
        return proc

    def run_ok(self, *args, cwd=None, props=()):
        proc = self.run_helper(*args, cwd=cwd, props=props)
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        self.assertEqual(proc.stderr, "")
        return json.loads(proc.stdout)

    def run_err(self, *args, cwd=None, props=()):
        proc = self.run_helper(*args, cwd=cwd, props=props)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("usage-helper:"), proc.stderr)
        return proc.stderr

    def fake_class_dir(self, count):
        """Unique temp dir with `count` empty .class files; the analyzer
        cannot parse them, so any run that reaches analysis fails loudly."""
        fake = Path(tempfile.mkdtemp(prefix="fake-classes-", dir=self.fix))
        for i in range(count):
            (fake / f"X{i}.class").touch()
        return fake

    @staticmethod
    def pairs(out):
        return {(r["from_class"], r["to_class"]) for r in out["refs"]}

    @staticmethod
    def candidates(out):
        return {c["to_class"]: c["owner_candidates"] for c in out["target_owner_candidates"]}

    # -- happy path ------------------------------------------------------

    def test_success_schema_and_stream_hygiene(self):
        out = self.run_ok("--root", self.root_jar, "--scope", "production",
                          "--dependency", self.dep_b, "--dependency", self.dep_c)
        self.assertEqual(out["schema"], "mdi.usage-helper/1")
        self.assertEqual(out["scope"], "production")
        self.assertEqual(out["root"], str(self.root_jar.resolve()))
        root_entry = out["inputs"][0]
        self.assertTrue(root_entry["analyzed"])
        self.assertEqual(root_entry["classes"], ["com.example.A"])
        for entry in out["inputs"]:
            self.assertEqual(set(entry), {"path", "role", "kind", "size_bytes",
                                          "class_count", "classes", "analyzed"})
        for ref in out["refs"]:
            self.assertEqual(set(ref), {"from_class", "to_class", "origin_input"})
        for cand in out["target_owner_candidates"]:
            self.assertEqual(set(cand), {"to_class", "owner_candidates"})

    def test_b_references_c_and_a_does_not(self):
        out = self.run_ok("--root", self.root_jar, "--scope", "production",
                          "--dependency", self.dep_b, "--dependency", self.dep_c)
        pairs = self.pairs(out)
        self.assertIn(("com.example.A", "com.example.B"), pairs)
        self.assertIn(("com.example.B", "com.example.C"), pairs)
        self.assertNotIn(("com.example.A", "com.example.C"), pairs)
        # the B->C edge originates from the B jar, never from the root
        triples = {(r["from_class"], r["to_class"], r["origin_input"]) for r in out["refs"]}
        self.assertIn(("com.example.B", "com.example.C", str(self.dep_b.resolve())), triples)

    def test_duplicate_owners_all_listed_no_first_wins(self):
        out = self.run_ok("--root", self.root_jar, "--scope", "production",
                          "--dependency", self.dup1, "--dependency", self.dup2)
        owners = self.candidates(out)["com.example.dup.Dup"]
        self.assertEqual(sorted(owners),
                         sorted([str(self.dup1.resolve()), str(self.dup2.resolve())]))
        self.assertEqual(len(owners), 2)

    def test_unowned_target_retained(self):
        out = self.run_ok("--root", self.root_jar, "--scope", "production",
                          "--dependency", self.dep_b)  # dep-c intentionally absent
        self.assertIn(("com.example.B", "com.example.C"), self.pairs(out))
        self.assertEqual(self.candidates(out).get("com.example.C"), [])

    def test_scope_distinguishes_root_role(self):
        for scope in ("production", "test"):
            out = self.run_ok("--root", self.root_jar, "--scope", scope,
                              "--dependency", self.dep_b)
            roles = {e["path"]: e["role"] for e in out["inputs"]}
            self.assertEqual(roles[str(self.root_jar.resolve())], scope)
            self.assertEqual(roles[str(self.dep_b.resolve())], "upstream")

    def test_exploded_directory_root(self):
        out = self.run_ok("--root", self.exploded, "--scope", "production",
                          "--dependency", self.dep_b)
        root_entry = out["inputs"][0]
        self.assertEqual(root_entry["kind"], "directory")
        self.assertEqual(root_entry["classes"], ["com.example.A"])
        self.assertIn(("com.example.A", "com.example.B"), self.pairs(out))

    def test_relative_argument_reported_as_canonical_path(self):
        out = self.run_ok("--root", "root.jar", "--scope", "production",
                          "--dependency", "./dep-b.jar", cwd=self.fix)
        self.assertEqual(out["inputs"][0]["path"], str(self.root_jar.resolve()))
        self.assertEqual(out["inputs"][1]["path"], str(self.dep_b.resolve()))

    # -- multi-release jars ------------------------------------------------

    def test_multi_release_dependency_analyzed_with_real_names_and_versions(self):
        out = self.run_ok("--root", self.root_jar, "--scope", "production",
                          "--dependency", self.dep_b, "--dependency", self.mr_jar)
        mr_path = str(self.mr_jar.resolve())
        mr_entry = next(e for e in out["inputs"] if e["path"] == mr_path)
        self.assertTrue(mr_entry["analyzed"])
        self.assertTrue(mr_entry["multi_release"])
        self.assertEqual(mr_entry["multi_release_versions"], [17])
        self.assertIn("17", mr_entry["versioned_classes"])
        versioned = mr_entry["versioned_classes"]["17"]
        self.assertTrue(any(c.endswith(".V") for c in versioned))
        self.assertIn(("com.example.A", "com.example.B"), self.pairs(out))
        names = [c for e in out["inputs"] for c in e["classes"]]
        names += [r[k] for r in out["refs"] for k in ("from_class", "to_class")]
        names += list(self.candidates(out))
        self.assertFalse([n for n in names if "META-INF" in n or "versions/17" in n])

    def test_multi_release_root_also_analyzed(self):
        out = self.run_ok("--root", self.mr_jar, "--scope", "production")
        self.assertTrue(out["inputs"][0]["analyzed"])
        self.assertTrue(out["inputs"][0]["multi_release"])
        self.assertNotIn("unsupported-multi-release-jar",
                         [i["code"] for i in out["issues"]])

    def test_manifest_only_multi_release_jar_analyzed_normally(self):
        out = self.run_ok("--root", self.root_jar, "--scope", "production",
                          "--dependency", self.dep_b, "--dependency", self.mr_manifest_jar)
        mm_path = str(self.mr_manifest_jar.resolve())
        mm_entry = next(e for e in out["inputs"] if e["path"] == mm_path)
        self.assertTrue(mm_entry["analyzed"])
        self.assertTrue(mm_entry["multi_release"])
        self.assertEqual(mm_entry["multi_release_versions"], [])
        self.assertEqual(mm_entry["versioned_classes"], {})
        self.assertNotIn("unsupported-multi-release-jar",
                         [i["code"] for i in out["issues"]])
        self.assertIn(("com.example.A", "com.example.B"), self.pairs(out))

    def test_multi_release_exploded_directory_unsupported(self):
        out = self.run_ok("--root", self.mr_exploded, "--scope", "production",
                          "--dependency", self.dep_b)
        mr_path = str(self.mr_exploded.resolve())
        self.assertTrue(any(i["code"] == "unsupported-multi-release-directory"
                            and i["input"] == mr_path for i in out["issues"]))
        root_entry = out["inputs"][0]
        self.assertFalse(root_entry["analyzed"])
        self.assertEqual(root_entry["classes"], [])
        self.assertEqual(root_entry["class_count"], 0)
        self.assertEqual(root_entry["kind"], "directory")
        self.assertEqual(root_entry["role"], "production")
        self.assertGreater(root_entry["size_bytes"], 0)  # metadata retained
        names = [c for e in out["inputs"] for c in e["classes"]]
        names += [r[k] for r in out["refs"] for k in ("from_class", "to_class")]
        names += list(self.candidates(out))
        self.assertFalse([n for n in names if "META-INF" in n or "versions/17" in n])
        self.assertTrue(out["refs"])  # other inputs are still analyzed

    def test_total_class_budget_enforced_preflight_before_analysis(self):
        fake = self.fake_class_dir(4)
        # Over budget with EMPTY .class files: the analyzer would fail on
        # them, so a clean exit-2 budget error proves the throw happened
        # before any analyzer call.
        err = self.run_err("--root", self.exploded, "--scope", "production",
                           "--dependency", fake, props=["mdi.maxTotalClasses=3"])
        self.assertIn("total preflight class count", err)
        self.assertIn("exceeds budget 3", err)
        # Exactly at budget: two real one-class directory inputs analyze fine.
        out = self.run_ok("--root", self.exploded, "--scope", "production",
                          "--dependency", self.fix / "stage/depb",
                          props=["mdi.maxTotalClasses=2"])
        self.assertEqual([e["class_count"] for e in out["inputs"]], [1, 1])
        self.assertTrue(out["refs"])
        # An unparseable override is a clean validation error.
        err = self.run_err("--root", self.exploded, "--scope", "production",
                           props=["mdi.maxTotalClasses=abc"])
        self.assertIn("invalid mdi.maxTotalClasses", err)

    def test_total_class_budget_override_can_only_narrow(self):
        fake = self.fake_class_dir(2)
        # Zero, negative, above the hard maximum, out-of-int-range, and
        # non-integer overrides are all clean validation failures BEFORE any
        # analyzer call; with these values accepted the analyzer would run
        # on the empty .class files and fail as exit 1 instead.
        for bad in ("0", "-4", "65537", "2147483647", "9999999999999", "abc"):
            with self.subTest(value=bad):
                err = self.run_err("--root", self.exploded, "--scope", "production",
                                   "--dependency", fake,
                                   props=[f"mdi.maxTotalClasses={bad}"])
                self.assertIn("invalid mdi.maxTotalClasses", err)
                self.assertIn("between 1 and 65536", err)
        # The hard maximum itself is an allowed (non-widening) value.
        out = self.run_ok("--root", self.exploded, "--scope", "production",
                          "--dependency", self.fix / "stage/depb",
                          props=["mdi.maxTotalClasses=65536"])
        self.assertEqual([e["class_count"] for e in out["inputs"]], [1, 1])
        # Positive narrowing keeps working (behavior preserved from before);
        # cap 2 is below the total regardless of how many fake files exist.
        err = self.run_err("--root", self.exploded, "--scope", "production",
                           "--dependency", fake, props=["mdi.maxTotalClasses=2"])
        self.assertIn("total preflight class count", err)

    # -- validation failures -------------------------------------------------

    def test_malformed_args_and_unsafe_paths_fail_cleanly(self):
        txt = self.fix / "notajar.txt"
        txt.write_text("hello")
        link = self.fix / "link-to-b.jar"
        os.symlink(self.dep_b, link)
        big = self.fix / "big.jar"
        with open(big, "wb") as fh:
            fh.truncate(70 * 1024 * 1024)
        baddir = self.fix / "baddir"
        (baddir / "com/example").mkdir(parents=True)
        shutil.copy(self.exploded / "com/example/A.class", baddir / "com/example/A.class")
        os.symlink(self.dep_b, baddir / "com/example/stray.class")
        ok = ["--scope", "production"]
        cases = [
            ([], "--root"),
            (["--scope", "production"], "--root"),
            (["--root", self.root_jar], "--scope"),
            (["--root", self.root_jar, "--scope", "dev"], "scope"),
            (["--wat"], "unknown argument"),
            (["--root", self.root_jar] + ok + ["--dependency"], "--dependency"),
            (["--root", self.fix / "nope.jar"] + ok, "no such"),
            (["--root", txt] + ok, "must be a .jar"),
            (["--root", self.root_jar] + ok + ["--dependency", link], "symlink"),
            (["--root", baddir] + ok, "symlink"),
            (["--root", self.root_jar] + ok + ["--dependency", self.dep_b,
                                               "--dependency", self.dep_b], "duplicate input"),
            (["--root", self.root_jar] + ok + ["--dependency", self.root_jar], "duplicate input"),
            (["--root", self.root_jar] + ok + ["--dependency", big], "exceeds"),
        ]
        for args, needle in cases:
            with self.subTest(args=[str(a) for a in args]):
                self.assertIn(needle, self.run_err(*args))

    # -- safety properties ---------------------------------------------------

    def test_inputs_unchanged_by_analysis(self):
        def snap():
            paths = [self.root_jar, self.dep_b, self.dep_c, self.dup1, self.dup2,
                     self.mr_jar, *self.exploded.rglob("*")]
            result = {}
            for p in paths:
                if p.is_file():
                    st = p.stat()
                    result[str(p)] = (st.st_size, st.st_mtime_ns, sha256_file(p))
            return result

        before = snap()
        self.run_ok("--root", self.root_jar, "--scope", "production",
                    "--dependency", self.dep_b, "--dependency", self.dep_c,
                    "--dependency", self.dup1, "--dependency", self.mr_jar)
        self.assertEqual(snap(), before)

    def test_analyzed_classes_are_never_loaded(self):
        self.assertFalse(self.sentinel.exists())
        self.run_ok("--root", self.root_jar, "--scope", "test", "--dependency", self.dep_b)
        self.assertFalse(self.sentinel.exists())


if __name__ == "__main__":
    unittest.main()
