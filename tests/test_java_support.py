"""Tests for tests/java_support.py (TASK-21.5 shared dependency resolution).

Everything runs against crafted manifests and fake dependency directories in
private temp dirs: no network, no Maven, no real jar is downloaded or
required. The real repo manifest is parsed read-only to pin its schema,
entry shapes and ordering. The import-safety test proves the suite modules
import cleanly with the environment variable unset or bogus, so discovery
never fails and no eager validation exists.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import java_support

REAL_PINS = {
    "maven-dependency-analyzer-1.17.2.jar":
        "f4d83b15827ca61325718a5e80f57205da0567ab311bf0ec1646c8106f78255d",
    "asm-9.10.1.jar":
        "ed825d10ab1399c8c0cb669e688cf0c8c82629b4c8399b58352b68e92ca10fcb",
    "gson-2.14.0.jar":
        "2cbd119bf1961c28788310963dc80ba65f58cdeec1dd139c8bdb1240faa2c36f",
    "error_prone_annotations-2.48.0.jar":
        "b49c5c958316ed67a09c699dda9aa749caf434d51d863dea599ef36a49b9c855",
}


def craft_manifest(root, dependencies, schema=java_support.EXPECTED_SCHEMA):
    """Write a manifest into a private dir and return its path."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "DEPENDENCIES.json"
    manifest.write_text(
        json.dumps({"schema": schema, "dependencies": dependencies}, indent=2) + "\n",
        encoding="utf-8")
    return manifest


def pin_entry(name, content):
    return ({"filename": name, "sha256": hashlib.sha256(content).hexdigest()},
            content)


class JavaSupportTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mdi-java-support-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class DependencyDirectoryTest(JavaSupportTestCase):
    def test_missing_env_is_actionable_runtime_error_never_skip(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(java_support.DependencyError) as ctx:
                java_support.dependency_directory()
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertNotIsInstance(ctx.exception, unittest.SkipTest)
        message = str(ctx.exception)
        self.assertIn(java_support.ENV_VAR, message)
        self.assertIn(java_support.MANIFEST_PATH.name, message)
        self.assertIn("download", message.lower())

    def test_empty_env_value_is_treated_as_unset(self):
        for raw in ("", "   "):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {java_support.ENV_VAR: raw}):
                    with self.assertRaises(java_support.DependencyError):
                        java_support.dependency_directory()

    def test_env_value_returned_as_path(self):
        directory = self.tmp / "jars"
        directory.mkdir()
        with mock.patch.dict(os.environ, {java_support.ENV_VAR: str(directory)}):
            self.assertEqual(java_support.dependency_directory(), directory)

    def test_explicit_environ_mapping_is_respected(self):
        self.assertEqual(
            java_support.dependency_directory({java_support.ENV_VAR: "/x"}),
            Path("/x"))
        with self.assertRaises(java_support.DependencyError):
            java_support.dependency_directory({})


class ManifestPinsTest(JavaSupportTestCase):
    def test_real_manifest_pins_schema_order_and_shapes(self):
        pins = java_support.manifest_pins()
        raw = json.loads(java_support.MANIFEST_PATH.read_text(encoding="utf-8"))
        expected = {dep["filename"]: dep["sha256"] for dep in raw["dependencies"]}
        self.assertEqual(raw["schema"], java_support.EXPECTED_SCHEMA)
        # Manifest order is preserved exactly; no sorted or arbitrary order.
        self.assertEqual(list(pins), list(expected))
        self.assertEqual(pins, expected)
        self.assertEqual(pins, REAL_PINS)

    def test_foreign_schema_rejected(self):
        manifest = craft_manifest(self.tmp / "m", [], schema="mdi.helper-dependencies/1")
        with self.assertRaises(java_support.DependencyError) as ctx:
            java_support.manifest_pins(manifest)
        self.assertIn("schema", str(ctx.exception))

    def test_non_object_manifest_rejected(self):
        manifest = self.tmp / "list.json"
        manifest.write_text("[]\n", encoding="utf-8")
        with self.assertRaises(java_support.DependencyError):
            java_support.manifest_pins(manifest)

    def test_missing_manifest_is_actionable_dependency_error(self):
        missing = self.tmp / "absent" / "DEPENDENCIES.json"
        with self.assertRaises(java_support.DependencyError) as ctx:
            java_support.manifest_pins(missing)
        # The actionable error replaces the raw FileNotFoundError entirely.
        self.assertNotIsInstance(ctx.exception, FileNotFoundError)
        message = str(ctx.exception)
        self.assertIn(str(missing), message)
        self.assertIn("No such file", message)

    def test_unreadable_manifest_is_actionable_dependency_error(self):
        # A manifest path that cannot be opened (here: it is a directory)
        # surfaces as DependencyError, never as a raw OSError.
        directory = self.tmp / "manifest-is-a-directory"
        directory.mkdir()
        with self.assertRaises(java_support.DependencyError) as ctx:
            java_support.manifest_pins(directory)
        self.assertNotIsInstance(ctx.exception, IsADirectoryError)
        self.assertIn(str(directory), str(ctx.exception))

    def test_invalid_utf8_manifest_is_actionable_dependency_error(self):
        manifest = self.tmp / "not-utf8.json"
        manifest.write_bytes(b'{"schema": "mdi.helper-dependencies/2\xff\xfe"}')
        with self.assertRaises(java_support.DependencyError) as ctx:
            java_support.manifest_pins(manifest)
        # The actionable error replaces the raw UnicodeDecodeError entirely.
        self.assertNotIsInstance(ctx.exception, UnicodeDecodeError)
        message = str(ctx.exception)
        self.assertIn(str(manifest), message)
        self.assertIn("UTF-8", message)

    def test_invalid_json_rejected(self):
        manifest = self.tmp / "broken.json"
        manifest.write_text("{not json", encoding="utf-8")
        with self.assertRaises(java_support.DependencyError):
            java_support.manifest_pins(manifest)

    def test_empty_dependency_list_rejected(self):
        manifest = craft_manifest(self.tmp / "m", [])
        with self.assertRaises(java_support.DependencyError):
            java_support.manifest_pins(manifest)

    def test_unsafe_filenames_rejected_never_treated_as_paths(self):
        unsafe = ["../evil.jar", "/etc/evil.jar", "sub/dir/evil.jar",
                  "back\\slash.jar", "hidden/.x.jar", "no-extension",
                  "not-a.jar.txt", "", ".jar", "evil.jar\n"]
        for name in unsafe:
            with self.subTest(filename=name):
                manifest = craft_manifest(self.tmp / ("m-" + str(abs(hash(name)))),
                                          [{"filename": name, "sha256": "0" * 64}])
                with self.assertRaises(java_support.DependencyError) as ctx:
                    java_support.manifest_pins(manifest)
                message = str(ctx.exception)
                self.assertIn("filename", message)
                self.assertNotIsInstance(ctx.exception, unittest.SkipTest)

    def test_malformed_sha256_pins_rejected(self):
        bad = ["0" * 63, "0" * 65, "F" * 64, "g" * 64, "", None, 123]
        for digest in bad:
            with self.subTest(sha256=digest):
                manifest = craft_manifest(
                    self.tmp / ("d-" + str(abs(hash(str(digest))))),
                    [{"filename": "a.jar", "sha256": digest}])
                with self.assertRaises(java_support.DependencyError):
                    java_support.manifest_pins(manifest)

    def test_duplicate_filenames_rejected(self):
        entry, _ = pin_entry("dup.jar", b"x")
        manifest = craft_manifest(self.tmp / "m", [entry, dict(entry)])
        with self.assertRaises(java_support.DependencyError) as ctx:
            java_support.manifest_pins(manifest)
        self.assertIn("dup.jar", str(ctx.exception))

    def test_entry_without_filename_rejected(self):
        manifest = craft_manifest(self.tmp / "m", [{"sha256": "0" * 64}])
        with self.assertRaises(java_support.DependencyError):
            java_support.manifest_pins(manifest)


class VerifiedDependenciesTest(JavaSupportTestCase):
    def env_of(self, directory):
        return mock.patch.dict(os.environ, {java_support.ENV_VAR: str(directory)})

    def test_valid_pins_returned_in_manifest_order(self):
        second, second_bytes = pin_entry("z-second.jar", b"second-jar")
        first, first_bytes = pin_entry("a-first.jar", b"first-jar")
        manifest = craft_manifest(self.tmp / "m", [second, first])
        directory = self.tmp / "jars"
        directory.mkdir()
        (directory / "z-second.jar").write_bytes(second_bytes)
        (directory / "a-first.jar").write_bytes(first_bytes)
        with self.env_of(directory):
            pins = java_support.verified_dependencies(manifest_path=manifest)
        # Manifest order, not sorted order.
        self.assertEqual(list(pins), ["z-second.jar", "a-first.jar"])
        self.assertEqual(pins["a-first.jar"],
                         hashlib.sha256(first_bytes).hexdigest())

    def test_explicit_directory_bypasses_env(self):
        entry, content = pin_entry("only.jar", b"only")
        manifest = craft_manifest(self.tmp / "m", [entry])
        directory = self.tmp / "jars"
        directory.mkdir()
        (directory / "only.jar").write_bytes(content)
        with mock.patch.dict(os.environ, {}, clear=True):
            pins = java_support.verified_dependencies(directory=directory,
                                                      manifest_path=manifest)
        self.assertEqual(pins, {entry["filename"]: entry["sha256"]})

    def test_missing_env_surfaces_actionable_runtime_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(java_support.DependencyError) as ctx:
                java_support.verified_dependencies()
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertIn(java_support.ENV_VAR, str(ctx.exception))

    def test_missing_directory_rejected(self):
        missing = self.tmp / "not-there"
        with self.env_of(missing):
            with self.assertRaises(java_support.DependencyError) as ctx:
                java_support.verified_dependencies()
        message = str(ctx.exception)
        self.assertIn(str(missing), message)
        self.assertIn(java_support.MANIFEST_PATH.name, message)

    def test_absent_jar_rejected_against_real_manifest(self):
        # Directory exists but holds none of the pinned jars; no download is
        # attempted and the error names the jar, the directory and the manifest.
        directory = self.tmp / "empty-jars"
        directory.mkdir()
        with self.env_of(directory):
            with self.assertRaises(java_support.DependencyError) as ctx:
                java_support.verified_dependencies()
        message = str(ctx.exception)
        named = [name for name in REAL_PINS if name in message]
        self.assertEqual(len(named), 1, message)
        self.assertIn(str(directory), message)
        self.assertIn(java_support.MANIFEST_PATH.name, message)

    def test_tampered_jar_rejected_against_real_manifest(self):
        directory = self.tmp / "junk-jars"
        directory.mkdir()
        for name in REAL_PINS:
            (directory / name).write_bytes(b"tampered garbage bytes")
        with self.env_of(directory):
            with self.assertRaises(java_support.DependencyError) as ctx:
                java_support.verified_dependencies()
        message = str(ctx.exception)
        self.assertIn("sha256", message.lower())
        self.assertTrue(any(name in message for name in REAL_PINS), message)
        self.assertNotIsInstance(ctx.exception, unittest.SkipTest)

    def test_tampered_jar_rejected_with_crafted_pins(self):
        entry, content = pin_entry("pinned.jar", b"exact bytes")
        manifest = craft_manifest(self.tmp / "m", [entry])
        directory = self.tmp / "jars"
        directory.mkdir()
        (directory / "pinned.jar").write_bytes(content + b"extra")
        with self.env_of(directory):
            with self.assertRaises(java_support.DependencyError) as ctx:
                java_support.verified_dependencies(manifest_path=manifest)
        message = str(ctx.exception)
        self.assertIn("pinned.jar", message)
        self.assertIn(entry["sha256"], message)


class ImportSafetyTest(JavaSupportTestCase):
    def test_suite_modules_import_without_env_and_without_eager_validation(self):
        code = ("import tests.java_support as js, tests.test_usage_helper, "
                "tests.test_usage, tests.test_usage_pipeline, "
                "tests.test_helper_packaging; print('imported ' + js.ENV_VAR)")
        base = dict(os.environ)
        base.pop(java_support.ENV_VAR, None)
        bogus = str(REPO / "no" / "such" / "dependency" / "dir")
        for env_value in (None, bogus):
            with self.subTest(env=env_value):
                env = dict(base)
                if env_value is not None:
                    env[java_support.ENV_VAR] = env_value
                proc = subprocess.run([sys.executable, "-c", code],
                                      capture_output=True, text=True,
                                      cwd=str(REPO), env=env, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(),
                                 "imported " + java_support.ENV_VAR)

    def test_dependency_error_is_a_runtime_error_subclass(self):
        self.assertTrue(issubclass(java_support.DependencyError, RuntimeError))
        self.assertFalse(issubclass(java_support.DependencyError,
                                    unittest.SkipTest))


if __name__ == "__main__":
    unittest.main()
