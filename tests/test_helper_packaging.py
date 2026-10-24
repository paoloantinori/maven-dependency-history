"""Tests for the offline helper packaging slice (TASK-21.1).

Exercises helper/build_local.py: pinned-input verification before javac,
deterministic helper.jar bytes, output-collision refusal, pristine lib/ jars,
license text extracted from the actual analyzer jar (jar-entry kind) and
vendored under helper/LICENSES/ from official sources (vendored-file kind,
missing/tampered/unsafe vendored files refused before javac), and a real
``java -cp helper.jar:lib/* mdi.UsageHelper`` run on a tiny fixture.
No network, no Maven; everything runs in private temp dirs.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER_DIR = REPO / "skills" / "maven-dependency-investigator" / "helper"
BUILD_SCRIPT = HELPER_DIR / "build_local.py"
MANIFEST = HELPER_DIR / "DEPENDENCIES.json"
HELPER_SRC = HELPER_DIR / "src" / "main" / "java" / "mdi" / "UsageHelper.java"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import java_support

ANALYZER_LICENSE_SHA256 = \
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
ANALYZER_NOTICE_SHA256 = \
    "4e6fe9752f1fac810d2152ef2f6850ded2bad8ed1f46c39f60e98a7268bb98f9"

# Vendored license texts under helper/LICENSES/, pinned like the jars. The
# canonical Apache-2.0 file is shared by gson and error_prone_annotations
# (their POMs both declare the apache.org URL); ASM carries its own
# BSD-3-Clause text from the exact-tag official source. Missing or
# mismatched vendored files are a hard failure of the packaging contract.
VENDORED_DIR = HELPER_DIR / "LICENSES"
VENDORED_PINNED = {
    "LICENSES/apache-2.0.txt":
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "LICENSES/asm-9.10.1-BSD-3-Clause.txt":
        "293b6af371eee28b0ff16f0334ea19e20a3d5522143faa4b95b346855507879a",
}

FIXED_TIME = (1980, 1, 1, 0, 0, 0)
TIMEOUT = 300
SANITIZED_ENV_KEYS = ("PATH", "LANG", "LC_ALL")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_build(dep_dir, out_dir, javac=None, manifest=None):
    cmd = [sys.executable, str(BUILD_SCRIPT),
           "--dependency-dir", str(dep_dir), "--output-dir", str(out_dir)]
    if javac is not None:
        cmd += ["--javac", str(javac)]
    if manifest is not None:
        cmd += ["--manifest", str(manifest)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                           env=java_support.child_env())


def spy_javac(tmp, marker):
    """A javac wrapper that records that it ran, then execs the real javac."""
    real = shutil.which("javac")
    assert real, "javac is required for the packaging tests"
    script = tmp / "spy-javac.sh"
    script.write_text("#!/bin/sh\nprintf x >> '%s'\nexec %s \"$@\"\n"
                      % (marker, real), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class PackagingTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mandatory dependency availability: never skip, raise an actionable
        # RuntimeError naming the env var, manifest or failing jar.
        cls.dep_src = java_support.dependency_directory()
        cls.pins = java_support.verified_dependencies(cls.dep_src)
        if not BUILD_SCRIPT.is_file() or not MANIFEST.is_file():
            raise AssertionError(
                "missing %s or %s" % (BUILD_SCRIPT, MANIFEST))
        for rel, expected in VENDORED_PINNED.items():
            path = HELPER_DIR / rel
            if not path.is_file():
                raise AssertionError("vendored license file missing: %s" % path)
            actual = sha256(path)
            if actual != expected:
                raise AssertionError(
                    "vendored license %s has sha256 %s, expected %s"
                    % (path, actual, expected))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mdi-packaging-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dep_dir = self.tmp / "deps"
        self.dep_dir.mkdir()
        for name in self.pins:
            shutil.copyfile(self.dep_src / name, self.dep_dir / name)
        self.marker = self.tmp / "javac-ran"
        self.spy = spy_javac(self.tmp, self.marker)

    def out(self, name="out"):
        return self.tmp / name


class TestInputVerification(PackagingTestBase):
    def test_missing_dependency_input_fails_before_javac(self):
        (self.dep_dir / "gson-2.14.0.jar").unlink()
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gson-2.14.0.jar", result.stderr)
        self.assertFalse(out.exists())
        self.assertFalse(self.marker.exists(), "javac ran despite missing input")

    def test_hash_mismatch_fails_before_javac(self):
        target = self.dep_dir / "gson-2.14.0.jar"
        with open(target, "ab") as fh:
            fh.write(b"tampered")
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gson-2.14.0.jar", result.stderr)
        self.assertIn("sha256", result.stderr.lower())
        self.assertFalse(out.exists())
        self.assertFalse(self.marker.exists(), "javac ran despite hash mismatch")


class TestOutputCollision(PackagingTestBase):
    def test_existing_output_dir_refused_and_untouched(self):
        out = self.out()
        out.mkdir()
        sentinel = out / "sentinel.txt"
        sentinel.write_text("do-not-touch", encoding="utf-8")
        result = run_build(self.dep_dir, out, javac=self.spy)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exists", result.stderr)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["sentinel.txt"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-touch")
        self.assertFalse(self.marker.exists())

    def test_dangling_symlink_output_dir_refused_untouched(self):
        target = self.tmp / "never-created-target"
        out = self.tmp / "out-link"
        os.symlink(target, out)
        result = run_build(self.dep_dir, out, javac=self.spy)
        self.assertEqual(result.returncode, 2)
        self.assertIn("exists", result.stderr)
        self.assertIn("symlink", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(out.is_symlink(), "the dangling symlink was removed or replaced")
        self.assertEqual(os.readlink(out), str(target))
        self.assertFalse(target.exists(), "the symlink target was created through the link")
        self.assertFalse(self.marker.exists(), "javac ran despite symlink collision")


class TestDeterminism(PackagingTestBase):
    def test_two_builds_produce_identical_helper_jar(self):
        first, second = self.out("build-a"), self.out("build-b")
        for out in (first, second):
            result = run_build(self.dep_dir, out, javac=self.spy)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sha256(first / "helper.jar"), sha256(second / "helper.jar"))
        self.assertEqual((first / "SHA256SUMS").read_bytes(),
                         (second / "SHA256SUMS").read_bytes())
        with zipfile.ZipFile(first / "helper.jar") as zf:
            infos = zf.infolist()
        names = [i.filename for i in infos]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names[0], "META-INF/MANIFEST.MF")
        for info in infos:
            self.assertEqual(info.date_time, FIXED_TIME)


class TestVendoredLicenses(PackagingTestBase):
    """vendored-file license texts: packaged byte-identical, verified before
    javac, and unsafe manifest paths rejected without any build work."""

    def craft_manifest(self, name, mutate, with_licenses=True):
        """Write a mutated copy of DEPENDENCIES.json into a private dir.

        Returns the manifest path. The vendored LICENSES/ tree is copied
        alongside unless with_licenses is false, so vendored-file entries
        can be made missing, tampered or unsafe without touching the repo.
        """
        root = self.tmp / ("craft-" + name)
        root.mkdir()
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        mutate(data)
        manifest = root / "DEPENDENCIES.json"
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if with_licenses:
            shutil.copytree(VENDORED_DIR, root / "LICENSES")
        return manifest

    @staticmethod
    def gson_license(data):
        return next(d for d in data["dependencies"]
                    if d["filename"] == "gson-2.14.0.jar")["license"]["texts"]["license"]

    def test_vendored_text_included_and_byte_identical_per_output(self):
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Each packaged vendored LICENSE preserves the source bytes exactly.
        for stem, rel in (
                ("asm-9.10.1", "LICENSES/asm-9.10.1-BSD-3-Clause.txt"),
                ("gson-2.14.0", "LICENSES/apache-2.0.txt"),
                ("error_prone_annotations-2.48.0", "LICENSES/apache-2.0.txt")):
            packaged = out / "licenses" / stem / "LICENSE"
            self.assertEqual(packaged.read_bytes(),
                             (HELPER_DIR / rel).read_bytes(),
                             "packaged LICENSE for %s differs from %s" % (stem, rel))
        # BUILD-INFO records every vendored source with its pinned hash.
        info = json.loads((out / "BUILD-INFO.json").read_text(encoding="utf-8"))
        recorded = {(e["dependency"], e["role"], e["path"], e["sha256"])
                    for e in info["vendored_license_texts"]}
        self.assertEqual(recorded, {
            ("asm-9.10.1.jar", "license",
             "LICENSES/asm-9.10.1-BSD-3-Clause.txt",
             VENDORED_PINNED["LICENSES/asm-9.10.1-BSD-3-Clause.txt"]),
            ("gson-2.14.0.jar", "license", "LICENSES/apache-2.0.txt",
             VENDORED_PINNED["LICENSES/apache-2.0.txt"]),
            ("error_prone_annotations-2.48.0.jar", "license",
             "LICENSES/apache-2.0.txt",
             VENDORED_PINNED["LICENSES/apache-2.0.txt"])})
        self.assertEqual(info["license_blockers"], [])

    def test_missing_vendored_file_fails_before_javac(self):
        manifest = self.craft_manifest("missing", lambda data: None,
                                    with_licenses=False)
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy, manifest=manifest)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("vendored", result.stderr.lower())
        # asm is the first vendored-file dependency in manifest order.
        self.assertIn("asm-9.10.1-BSD-3-Clause.txt", result.stderr)
        self.assertFalse(out.exists(), "output directory created anyway")
        self.assertFalse(self.marker.exists(), "javac ran despite missing license")

    def test_tampered_vendored_file_fails_before_javac(self):
        manifest = self.craft_manifest("tamper", lambda data: None)
        target = manifest.parent / "LICENSES" / "asm-9.10.1-BSD-3-Clause.txt"
        with open(target, "ab") as fh:
            fh.write(b"tampered")
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy, manifest=manifest)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("asm-9.10.1-BSD-3-Clause.txt", result.stderr)
        self.assertIn("sha256", result.stderr.lower())
        self.assertFalse(out.exists(), "output directory created anyway")
        self.assertFalse(self.marker.exists(), "javac ran despite tampered license")

    def test_absolute_vendored_path_rejected(self):
        def mutate(data):
            self.gson_license(data)["path"] = "/etc/passwd"
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy,
                           manifest=self.craft_manifest("absolute", mutate))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("relative", result.stderr)
        self.assertFalse(out.exists())
        self.assertFalse(self.marker.exists())

    def test_traversal_vendored_path_rejected(self):
        def mutate(data):
            self.gson_license(data)["path"] = "LICENSES/../../../../etc/passwd"
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy,
                           manifest=self.craft_manifest("traversal", mutate))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("relative", result.stderr)
        self.assertFalse(out.exists())
        self.assertFalse(self.marker.exists())

    def test_symlinked_vendored_file_rejected(self):
        manifest = self.craft_manifest("symlink", lambda data: None)
        target = manifest.parent / "LICENSES" / "apache-2.0.txt"
        real = VENDORED_DIR / "apache-2.0.txt"
        target.unlink()
        os.symlink(real, target)
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy, manifest=manifest)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("symlink", result.stderr)
        self.assertFalse(out.exists())
        self.assertFalse(self.marker.exists())

    def test_unknown_text_kind_rejected(self):
        def mutate(data):
            self.gson_license(data)["kind"] = "download-url"
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy,
                           manifest=self.craft_manifest("unknown-kind", mutate))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("kind", result.stderr.lower())
        self.assertFalse(out.exists())
        self.assertFalse(self.marker.exists())


class TestBuildAndRun(PackagingTestBase):
    def test_package_layout_notices_and_real_helper_run(self):
        out = self.out()
        result = run_build(self.dep_dir, out, javac=self.spy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())

        # lib/ holds byte-pristine copies of every pinned jar.
        for name, expected in self.pins.items():
            self.assertEqual(sha256(out / "lib" / name), expected)

        # License text comes verbatim from the actual analyzer jar, and every
        # dependency now has a LICENSE in the package: all current licenses
        # are accounted for and no blocker remains.
        lic = out / "licenses" / "maven-dependency-analyzer-1.17.2"
        self.assertEqual(sha256(lic / "LICENSE"), ANALYZER_LICENSE_SHA256)
        self.assertEqual(sha256(lic / "NOTICE"), ANALYZER_NOTICE_SHA256)
        self.assertEqual(
            sorted(p.name for p in (out / "licenses").iterdir()),
            ["asm-9.10.1", "error_prone_annotations-2.48.0",
             "gson-2.14.0", "maven-dependency-analyzer-1.17.2"])
        for stem in ("asm-9.10.1", "error_prone_annotations-2.48.0",
                     "gson-2.14.0"):
            self.assertTrue((out / "licenses" / stem / "LICENSE").is_file(),
                            "missing packaged LICENSE for %s" % stem)

        info = json.loads((out / "BUILD-INFO.json").read_text(encoding="utf-8"))
        self.assertEqual(sha256(HELPER_SRC), info["helper_source"]["sha256"])
        self.assertTrue(info["javac"]["version_output"].startswith("javac "))
        self.assertEqual(info["python"], {
            "version": sys.version,
            "zlib_version": zlib.ZLIB_VERSION,
            "zlib_runtime_version": zlib.ZLIB_RUNTIME_VERSION})
        self.assertEqual(info["flags"], ["--release", "17", "-encoding", "UTF-8"])
        self.assertEqual(info["license_blockers"], [])

        # SHA256SUMS covers every packaged file and matches recomputation.
        for line in (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            recorded, rel = line.split("  ", 1)
            self.assertEqual(sha256(out / rel), recorded)

        self._run_real_helper(out)

    def _run_real_helper(self, out):
        # Mandatory Java integration: a missing tool is an honest failure, not
        # a skip.
        if shutil.which("java") is None:
            raise AssertionError(
                "BLOCKER: java not available on PATH; the packaged-helper "
                "runtime check is a mandatory integration test")
        src = self.tmp / "fixture"
        (src / "fix").mkdir(parents=True)
        (src / "fix" / "A.java").write_text(
            "package fix;\npublic class A { public String go() "
            "{ return new B().label(); } }\n", encoding="utf-8")
        (src / "fix" / "B.java").write_text(
            "package fix;\npublic class B { public String label() "
            "{ return \"b\"; } }\n", encoding="utf-8")
        classes = self.tmp / "fixture-classes"
        classes.mkdir()
        subprocess.run(
            [shutil.which("javac"), "--release", "17", "-d", str(classes),
             str(src / "fix" / "A.java"), str(src / "fix" / "B.java")],
            check=True, capture_output=True, timeout=TIMEOUT,
            env=java_support.child_env())
        classpath = os.pathsep.join([str(out / "helper.jar"),
                                     "%s/lib/*" % out])
        env = {k: os.environ[k] for k in SANITIZED_ENV_KEYS if k in os.environ}
        result = subprocess.run(
            [shutil.which("java"), "-cp", classpath, "mdi.UsageHelper",
             "--root", str(classes), "--scope", "production"],
            capture_output=True, text=True, timeout=TIMEOUT, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], "mdi.usage-helper/1")
        edges = {(r["from_class"], r["to_class"]) for r in report["refs"]}
        self.assertIn(("fix.A", "fix.B"), edges)


if __name__ == "__main__":
    unittest.main()
