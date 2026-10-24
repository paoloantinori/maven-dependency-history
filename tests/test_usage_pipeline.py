"""Tests for mdi.usage_pipeline inspect_usage (TASK-21.4 integration slice).

End-to-end over real artifacts only: the helper package is built once per
class by helper/build_local.py from the existing pinned jars (no network, no
Maven; the jars come from MDI_TEST_DEPENDENCY_DIR and are verified against
the pin manifest by tests/java_support.py in setUpClass), the A/B/C/Dup/MR
fixtures are compiled once with javac --release 17,
and every call stages real jars out of a private checkout into a private
staging base and runs the real Java helper over the staged copies. Checkout
originals must stay untouched, the JVM must never see original paths, and
staged evidence must map back to original source locators and digests even
after the stage directory is deleted.
"""

import errno
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import staging
from mdi import usage
from mdi import usage_pipeline

REPO = Path(__file__).resolve().parents[1]
HELPER_DIR = (REPO / "skills" / "maven-dependency-investigator" / "helper")
BUILD_SCRIPT = HELPER_DIR / "build_local.py"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import java_support
from tests.java_fixtures import (A_SRC, B_SRC, C_SRC, DUP_SRC, MR_BASE_SRC,
                                 MR_V_SRC, SENTINEL_NAME, sha256_file)

COMPILE_TIMEOUT = 240
BUILD_TIMEOUT = 600
RUN_TIMEOUT = 120


def _run(cmd, timeout=COMPILE_TIMEOUT):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          timeout=timeout, env=java_support.child_env())


class PipelineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mandatory dependency availability: never skip, raise an actionable
        # RuntimeError naming the env var, manifest or failing jar.
        cls.dep_dir = java_support.dependency_directory()
        cls.pins = java_support.verified_dependencies(cls.dep_dir)
        if not BUILD_SCRIPT.is_file():
            raise AssertionError(f"BLOCKER: helper build script missing: {BUILD_SCRIPT}")
        for tool in ("java", "javac", "jar"):
            if shutil.which(tool) is None:
                raise AssertionError(f"BLOCKER: {tool} not available on PATH")
        cls._build_helper_package()
        cls._build_class_fixtures()

    @classmethod
    def _build_helper_package(cls):
        """Package the helper with the real build entry (CLI) into a private
        temp dir; licenses must be included with no blockers."""
        parent = Path(tempfile.mkdtemp(prefix="mdi-pipeline-pkg-"))
        cls.addClassCleanup(shutil.rmtree, str(parent), True)
        cls.pkg = parent / "pkg"
        proc = _run([sys.executable, BUILD_SCRIPT,
                     "--dependency-dir", cls.dep_dir, "--output-dir", cls.pkg],
                    timeout=BUILD_TIMEOUT)
        if proc.returncode != 0:
            raise AssertionError(f"BLOCKER: helper packaging failed:\n{proc.stderr}")
        info = json.loads((cls.pkg / "BUILD-INFO.json").read_text(encoding="utf-8"))
        if info["license_blockers"]:
            raise AssertionError(f"BLOCKER: license blockers in helper package:"
                                 f" {info['license_blockers']}")
        for name in cls.pins:
            license_file = cls.pkg / "licenses" / name.removesuffix(".jar") / "LICENSE"
            if not license_file.is_file():
                raise AssertionError(f"BLOCKER: packaged LICENSE missing for {name}")
        cls.cp_paths = [str(cls.pkg / "helper.jar")]
        cls.cp_paths += [str(cls.pkg / "lib" / name) for name in cls.pins]
        # lib/ copies are verified against the pinned digests (a build that
        # shipped an altered jar must fail, not be self-consistently accepted);
        # only helper.jar, which this build produces, is hashed fresh.
        cls.expected = {str(cls.pkg / "helper.jar"): sha256_file(cls.pkg / "helper.jar")}
        cls.expected.update({str(cls.pkg / "lib" / name): pinned
                             for name, pinned in cls.pins.items()})

    @classmethod
    def _build_class_fixtures(cls):
        fix = Path(tempfile.mkdtemp(prefix="mdi-pipeline-fix-"))
        cls.addClassCleanup(shutil.rmtree, str(fix), True)
        cls.fix = fix
        cls.sentinel = fix / SENTINEL_NAME
        src = fix / "src"
        (src / "com/example/dup").mkdir(parents=True)
        (src / "com/example/mr").mkdir(parents=True)
        (src / "com/example/A.java").write_text(A_SRC.format(sentinel=cls.sentinel))
        (src / "com/example/B.java").write_text(B_SRC)
        (src / "com/example/C.java").write_text(C_SRC)
        (src / "com/example/dup/Dup.java").write_text(DUP_SRC)
        (src / "com/example/mr/Base.java").write_text(MR_BASE_SRC)
        (src / "com/example/mr/V.java").write_text(MR_V_SRC)
        classes = fix / "classes"
        proc = _run(["javac", "--release", "17", "-d", classes]
                    + sorted(path for path in src.rglob("*.java")))
        if proc.returncode != 0:
            raise AssertionError(f"BLOCKER: fixture javac failed:\n{proc.stderr}")

        def jar_of(out, rel, manifest=None):
            stage = fix / f"stage-{out}"
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(classes / rel, stage / rel)
            cmd = ["jar", "--create", "--file", fix / out]
            if manifest is not None:
                cmd += ["--manifest", manifest]
            cmd += ["-C", stage, "."]
            proc = _run(cmd)
            if proc.returncode != 0:
                raise AssertionError(f"BLOCKER: fixture jar {out} failed:\n{proc.stderr}")

        jar_of("root.jar", "com/example/A.class")
        jar_of("dep-b.jar", "com/example/B.class")
        jar_of("dep-c.jar", "com/example/C.class")
        jar_of("dup1.jar", "com/example/dup/Dup.class")
        jar_of("dup2.jar", "com/example/dup/Dup.class")

        mr_stage = fix / "stage-mr"
        (mr_stage / "com/example/mr").mkdir(parents=True)
        shutil.copy(classes / "com/example/mr/Base.class",
                    mr_stage / "com/example/mr/Base.class")
        v17 = mr_stage / "META-INF/versions/17/com/example/mr"
        v17.mkdir(parents=True)
        shutil.copy(classes / "com/example/mr/V.class", v17 / "V.class")
        mr_manifest = fix / "mr.mf"
        mr_manifest.write_text("Multi-Release: true\n")
        jar_of("mr.jar", "com/example/mr/Base.class", mr_manifest)

    def setUp(self):
        checkout = tempfile.TemporaryDirectory(prefix="mdi-pipeline-checkout-")
        base = tempfile.TemporaryDirectory(prefix="mdi-pipeline-base-")
        self.addCleanup(base.cleanup)
        self.addCleanup(checkout.cleanup)
        self.checkout = Path(checkout.name).resolve()
        self.base = Path(base.name).resolve()
        app = self.checkout / "app"
        app.mkdir()
        for name in ("root.jar", "dep-b.jar", "dep-c.jar", "dup1.jar", "dup2.jar",
                     "mr.jar"):
            shutil.copyfile(self.fix / name, app / name)

    # -- call helpers -----------------------------------------------------

    def inspect(self, root="app/root.jar", deps=("app/dep-b.jar", "app/dep-c.jar"),
                scope="production", staging_base=None,
                timeout_seconds=RUN_TIMEOUT, helper_classpath=None,
                expected=None, **kw):
        return usage_pipeline.inspect_usage(
            self.checkout, self.checkout / root,
            [self.checkout / dep for dep in deps],
            scope=scope, staging_base=self.base if staging_base is None else staging_base,
            helper_classpath=self.cp_paths if helper_classpath is None
            else helper_classpath,
            expected_hashes=dict(self.expected) if expected is None else expected,
            timeout_seconds=timeout_seconds, **kw)

    def checkout_snapshot(self):
        snapshot = {}
        for path in sorted(self.checkout.rglob("*")):
            if path.is_file():
                st = path.stat()
                snapshot[str(path)] = (st.st_size, st.st_mtime_ns, sha256_file(path))
        return snapshot

    @staticmethod
    def tree_digest(root):
        """Independent recomputation of staging's directory digest: sha256
        over sorted 'rel\\0file-sha256\\n' records, relative to the input."""
        records = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                records.append((path.relative_to(root).as_posix(), sha256_file(path)))
        joined = "".join(f"{rel}\x00{digest}\n" for rel, digest in sorted(records))
        return hashlib.sha256(joined.encode()).hexdigest()

    def base_entries(self):
        return sorted(p.name for p in self.base.iterdir())

    def ref(self, result, pair):
        return next(r for r in result["source_refs"]
                    if (r["from_class"], r["to_class"]) == pair)

    # -- happy path --------------------------------------------------------

    def test_complete_run_maps_evidence_to_original_sources(self):
        before = self.checkout_snapshot()
        result = self.inspect()
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["cleanup"], "complete")
        self.assertIsNone(result["cleanup_error"])
        self.assertTrue(result["staging"]["complete"])
        self.assertTrue(result["staging"]["stage_dir"].startswith(str(self.base) + os.sep))

        # The JVM saw staged copies only; original checkout paths never appear
        # in the invocation or in the validated report.
        runner = result["runner"]
        self.assertTrue(runner["root"].startswith(str(self.base) + os.sep))
        self.assertNotIn(str(self.checkout), " ".join(runner["run"]["argv"]))
        for ref in runner["refs"]:
            self.assertTrue(ref["origin_input"].startswith(str(self.base) + os.sep),
                            ref["origin_input"])

        # Source refs carry ORIGINAL locators and digests, mapped exactly from
        # the staged paths the helper reported.
        digests = {name: sha256_file(self.checkout / "app" / name)
                   for name in ("root.jar", "dep-b.jar", "dep-c.jar")}
        ab = self.ref(result, ("com.example.A", "com.example.B"))
        self.assertEqual(ab["origin"]["source"], str(self.checkout / "app/root.jar"))
        self.assertEqual(ab["origin"]["role"], "root")
        self.assertEqual(ab["origin"]["source_digest"], digests["root.jar"])
        self.assertEqual(ab["scope"], "production")
        self.assertEqual(ab["stage_id"], result["staging"]["stage_id"])
        bc = self.ref(result, ("com.example.B", "com.example.C"))
        self.assertEqual(bc["origin"]["source"], str(self.checkout / "app/dep-b.jar"))
        self.assertEqual(bc["origin"]["source_digest"], digests["dep-b.jar"])
        self.assertEqual([owner["source"] for owner in bc["owner_candidates"]],
                         [str(self.checkout / "app/dep-c.jar")])
        # No application-to-C reference: transitive edges are not invented.
        self.assertIsNone(next((r for r in result["source_refs"]
                                if (r["from_class"], r["to_class"])
                                == ("com.example.A", "com.example.C")), None))

        # After cleanup the stage is gone yet the evidence remains: originals
        # still on disk, digests still matching, staged names historical.
        self.assertFalse(Path(result["staging"]["stage_dir"]).exists())
        for entry in result["staging"]["inputs"]:
            original = Path(entry["source"])
            self.assertTrue(original.is_file(), entry["source"])
            if entry["kind"] == "jar":
                self.assertEqual(digests[original.name], entry["source_digest"])
            self.assertIn(entry["staged_rel"], ("input-0000.jar", "input-0001.jar",
                                                "input-0002.jar"))

        # Analyzed classes are never executed, and a successful run leaves the
        # checkout originals untouched too, not only the failure paths.
        self.assertFalse(self.sentinel.exists())
        self.assertEqual(self.checkout_snapshot(), before)
        # The result is JSON-safe and honest about capture-time evidence.
        json.loads(json.dumps(result))
        self.assertTrue(any("mutate" in item or "capture" in item
                            for item in result["limitations"]))

    def test_duplicate_owner_candidates_all_retained(self):
        result = self.inspect(deps=("app/dep-b.jar", "app/dup1.jar", "app/dup2.jar"))
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))
        dup_refs = [r for r in result["source_refs"]
                    if r["to_class"] == "com.example.dup.Dup"]
        self.assertTrue(dup_refs)
        expected = sorted([str(self.checkout / "app/dup1.jar"),
                           str(self.checkout / "app/dup2.jar")])
        for entry in dup_refs:
            # Every reference to the duplicated class keeps BOTH candidate
            # owners; a sole owner is never picked when several exist.
            self.assertEqual(
                sorted(owner["source"] for owner in entry["owner_candidates"]),
                expected)

    def test_scope_production_and_test_root_roles(self):
        for scope in ("production", "test"):
            with self.subTest(scope=scope):
                result = self.inspect(scope=scope, deps=("app/dep-b.jar",))
                self.assertEqual(result["status"], "complete",
                                 msg=str(result["issues"]))
                self.assertEqual(result["scope"], scope)
                self.assertEqual(result["runner"]["report"]["inputs"][0]["role"], scope)
                self.assertEqual(self.ref(result, ("com.example.A",
                                                   "com.example.B"))["scope"], scope)

    def test_unknown_owner_retained_empty(self):
        result = self.inspect(deps=("app/dep-b.jar",))
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))
        bc = self.ref(result, ("com.example.B", "com.example.C"))
        self.assertEqual(bc["owner_candidates"], [])

    def test_exploded_directory_root_staged_and_mapped(self):
        exploded = self.checkout / "exploded"
        (exploded / "com/example").mkdir(parents=True)
        shutil.copy(self.fix / "classes/com/example/A.class",
                    exploded / "com/example/A.class")
        result = self.inspect(root="exploded", deps=("app/dep-b.jar",))
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))
        ab = self.ref(result, ("com.example.A", "com.example.B"))
        self.assertEqual(ab["origin"]["kind"], "directory")
        self.assertEqual(ab["origin"]["source"], str(exploded))
        # The directory digest is recomputed independently from the fixture
        # bytes, not self-compared against another bundle-derived value.
        self.assertEqual(ab["origin"]["source_digest"], self.tree_digest(exploded))
        root_entry = result["staging"]["inputs"][0]
        self.assertEqual(root_entry["source_digest"], self.tree_digest(exploded))
        self.assertEqual(root_entry["kind"], "directory")

    # -- partial outcomes ---------------------------------------------------

    def test_mr_dependency_partial_with_surfaced_helper_issue(self):
        result = self.inspect(deps=("app/dep-b.jar", "app/mr.jar"))
        self.assertEqual(result["status"], "partial")
        # The raw runner result stays complete-but-flagged; the helper issue
        # is surfaced at pipeline level with the ORIGINAL source attached.
        self.assertEqual(result["runner"]["status"], "complete")
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("unsupported-multi-release-jar", codes)
        self.assertIn("inputs-not-analyzed", codes)
        surfaced = next(issue for issue in result["issues"]
                        if issue["code"] == "unsupported-multi-release-jar")
        self.assertEqual(surfaced["input_source"], str(self.checkout / "app/mr.jar"))
        mr_entry = next(entry for entry in result["runner"]["report"]["inputs"]
                        if entry["path"] == surfaced["input"])
        self.assertFalse(mr_entry["analyzed"])
        self.assertTrue(any("app/mr.jar" in issue["message"]
                            for issue in result["issues"]
                            if issue["code"] == "inputs-not-analyzed"))

    # -- staging refusals ---------------------------------------------------

    def test_staging_refusal_never_runs_helper(self):
        inside = self.checkout / "inside-stage"
        inside.mkdir()
        boom = mock.Mock(side_effect=AssertionError("helper must not run"))
        with mock.patch.object(usage, "run_usage_helper", boom):
            result = self.inspect(staging_base=inside)
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["runner"])
        self.assertEqual(result["source_refs"], [])
        self.assertFalse(result["staging"]["complete"])
        self.assertEqual(result["staging"]["refusal"]["code"],
                         "staging-base-inside-checkout")
        # The refusal view is a pipeline-defined refusal record, not a staging
        # summary wearing the staging schema with a different key set.
        self.assertEqual(set(result["staging"]),
                         {"stage_id", "stage_dir", "complete", "refusal",
                          "observations", "cleanup_status", "leftovers"})
        self.assertEqual(result["cleanup"], "not-reached")
        self.assertEqual(result["staging"]["cleanup_status"], "not-created")
        self.assertEqual(result["staging"]["leftovers"], [])
        self.assertEqual([issue["code"] for issue in result["issues"]],
                         ["staging-base-inside-checkout"])
        boom.assert_not_called()

    def test_staging_refusal_with_retained_stage_surfaces_leftovers(self):
        # Staging refuses AFTER creating its private stage and the abort
        # cleanup meets interference: the pipeline result must carry the
        # StageError's structured leftovers, never a not-reached cleanup
        # claim, and never a location scraped out of the message prose.
        real_capture = staging._capture_at
        calls = itertools.count()
        box = {}

        def sabotage(dir_fd, name, subject, max_bytes):
            if next(calls) >= 1:
                victim = next(self.base.rglob("input-0000.jar"))
                victim.unlink()
                victim.write_bytes(b"foreign-replacement")
                box["victim"] = victim
                raise OSError(errno.EIO, "injected read failure")
            return real_capture(dir_fd, name, subject, max_bytes)

        boom = mock.Mock(side_effect=AssertionError("helper must not run"))
        with mock.patch.object(staging, "_capture_at", sabotage), \
                mock.patch.object(usage, "run_usage_helper", boom):
            result = self.inspect()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["staging"]["refusal"]["code"], "stage-failed")
        # Remnants are known, so the cleanup field must not say not-reached.
        self.assertEqual(result["cleanup"], "failed")
        self.assertEqual(result["staging"]["cleanup_status"], "failed")
        self.assertIsNone(result["staging"]["stage_dir"])
        leftovers = result["staging"]["leftovers"]
        self.assertEqual(len(leftovers), 1)
        record = leftovers[0]
        self.assertEqual(set(record), {"path", "reason", "identity_verified"})
        self.assertIs(record["identity_verified"], False)
        retained = Path(record["path"])
        self.assertTrue(str(retained).startswith(str(self.base) + os.sep))
        self.assertFalse(str(retained).startswith(str(self.checkout) + os.sep))
        self.assertTrue(retained.is_dir())
        self.assertEqual(self.base_entries(), [retained.name])
        self.assertTrue(record["reason"])
        # Structured evidence, not prose: the refusal message names no path.
        self.assertNotIn(str(self.base), result["staging"]["refusal"]["message"])
        self.assertEqual(box["victim"].read_bytes(), b"foreign-replacement")
        json.loads(json.dumps(result))
        boom.assert_not_called()

    def test_post_creation_refusal_with_verified_cleanup_reports_complete(self):
        # Staging refuses after creating the stage (missing dependency) and
        # its abort cleanup removes everything: the rejection is real, and
        # the cleanup field states the verified removal, not not-reached.
        result = self.inspect(deps=("app/dep-b.jar", "app/missing.jar"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["staging"]["refusal"]["code"], "source-rejected")
        self.assertEqual(result["staging"]["cleanup_status"], "complete")
        self.assertEqual(result["staging"]["leftovers"], [])
        self.assertEqual(result["cleanup"], "complete")
        self.assertEqual(self.base_entries(), [])

    def test_untracked_refusal_status_surfaces_raw_and_reports_failed(self):
        # A StageError whose raiser tracked nothing (constructor default
        # unknown), one carrying an unrecognized status, and the inconsistent
        # not-created-with-records combo must never be laundered into a
        # not-reached claim: the conservative outer value is failed, the raw
        # cleanup_status stays visible in the refusal record, and no JVM runs.
        cases = [
            ("default-unknown", staging.StageError(
                "synthetic-refusal", "refusal that tracked nothing")),
            ("unrecognized-status", staging.StageError(
                "synthetic-refusal", "odd raiser",
                cleanup_status="garbage-status")),
            ("records-contradict-status", staging.StageError(
                "synthetic-refusal", "not-created but records exist",
                cleanup_status="not-created",
                leftovers=[staging.StageLeftover("/retained", "recorded",
                                                  False)])),
        ]
        for label, synthetic in cases:
            with self.subTest(label=label):
                boom = mock.Mock(side_effect=AssertionError("helper must not run"))
                with mock.patch.object(staging, "staged_inputs",
                                       side_effect=synthetic), \
                        mock.patch.object(usage, "run_usage_helper", boom):
                    result = self.inspect()
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["cleanup"], "failed")
                self.assertEqual(result["staging"]["cleanup_status"],
                                 synthetic.cleanup_status)
                self.assertEqual(result["staging"]["leftovers"],
                                 [record.as_dict()
                                  for record in synthetic.leftovers])
                self.assertEqual(result["staging"]["refusal"]["code"],
                                 "synthetic-refusal")
                self.assertIsNone(result["runner"])
                json.loads(json.dumps(result))
                boom.assert_not_called()

    def test_duplicate_input_refusal_rejected_without_runner(self):
        result = self.inspect(deps=("app/root.jar",))
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["runner"])
        self.assertEqual(result["staging"]["refusal"]["code"], "duplicate-input")
        self.assertEqual(result["staging"]["cleanup_status"], "not-created")
        self.assertEqual(result["staging"]["leftovers"], [])
        self.assertEqual(result["cleanup"], "not-reached")

    # -- runner failures ----------------------------------------------------

    def test_runner_validation_error_cleans_stage_leaves_sources(self):
        before = self.checkout_snapshot()
        with self.assertRaises(ValueError):
            self.inspect(timeout_seconds=0)
        self.assertEqual(self.base_entries(), [])
        self.assertEqual(self.checkout_snapshot(), before)

    def test_helper_failure_cleans_stage_leaves_sources(self):
        broken = self.checkout / "broken"
        (broken / "com/example").mkdir(parents=True)
        (broken / "com/example/X.class").write_bytes(b"")
        before = self.checkout_snapshot()
        result = self.inspect(deps=("app/dep-b.jar", "broken"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["runner"]["status"], "failed")
        self.assertIn("helper-failed",
                      {issue["code"] for issue in result["runner"]["issues"]})
        self.assertEqual(result["cleanup"], "complete")
        self.assertEqual(self.base_entries(), [])
        self.assertEqual(self.checkout_snapshot(), before)
        self.assertEqual(result["source_refs"], [])

    def test_missing_java_reports_unavailable(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.base)}):
            result = self.inspect()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["runner"]["status"], "unavailable")
        self.assertIsNone(result["runner"]["run"])
        self.assertEqual(result["cleanup"], "complete")
        self.assertEqual(self.base_entries(), [])

    # -- cleanup interference -------------------------------------------------

    def test_cleanup_interference_noted_replacement_preserved(self):
        real = usage.run_usage_helper
        victim_box = {}

        def sabotage(root, deps, **kw):
            # Replace one staged copy with a fresh inode holding the same
            # bytes: analysis proceeds, but cleanup must refuse to delete the
            # replacement and the refusal must not lose the analysis result.
            victim = Path(deps[0])
            data = victim.read_bytes()
            victim.unlink()
            victim.write_bytes(data)
            victim_box.update(path=victim, data=data)
            return real(root, deps, **kw)

        with mock.patch.object(usage, "run_usage_helper", sabotage):
            result = self.inspect()
        self.assertEqual(result["cleanup"], "failed")
        self.assertEqual(result["cleanup_error"]["code"], "stage-interference")
        self.assertIn("stage-interference",
                      {issue["code"] for issue in result["issues"]})
        self.assertEqual(result["status"], "partial")
        # The original analysis result survives the cleanup failure.
        self.assertEqual(result["runner"]["status"], "complete")
        self.assertTrue(result["source_refs"])
        self.assertTrue(self.ref(result, ("com.example.A", "com.example.B")))
        # The foreign replacement is preserved, the stage is left in place.
        self.assertTrue(victim_box["path"].exists())
        self.assertEqual(victim_box["path"].read_bytes(), victim_box["data"])
        self.assertTrue(Path(result["staging"]["stage_dir"]).exists())

    def test_non_stageerror_cleanup_failure_preserves_result(self):
        # A raw OSError escaping the normal-exit cleanup must ride along on
        # the finished result, not crash the caller and discard the analysis.
        with mock.patch.object(staging.StagedInputs, "cleanup",
                               side_effect=OSError("device busy")):
            result = self.inspect()
        self.assertEqual(result["cleanup"], "failed")
        self.assertEqual(result["cleanup_error"]["code"], "cleanup-failed")
        self.assertIn("device busy", result["cleanup_error"]["message"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["runner"]["status"], "complete")
        self.assertTrue(result["source_refs"])
        self.assertTrue(Path(result["staging"]["stage_dir"]).exists())

    def test_body_stageerror_is_not_reinterpreted_as_refusal(self):
        synthetic = staging.StageError("synthetic", "body failure")
        with mock.patch.object(usage, "run_usage_helper", side_effect=synthetic):
            with self.assertRaises(staging.StageError) as ctx:
                self.inspect()
        # The body's own exception propagates unchanged; no fabricated
        # rejection result is returned for a run whose staging succeeded.
        self.assertIs(ctx.exception, synthetic)
        self.assertEqual(ctx.exception.cleanup_status, "unknown")
        self.assertEqual(ctx.exception.leftovers, ())
        self.assertEqual(self.base_entries(), [])

    def test_double_slash_staging_base_still_maps_completely(self):
        # POSIX abspath preserves a leading "//" while the runner canonicalizes
        # with realpath, so the join must index both spellings of each staged
        # input instead of silently dropping every ref while claiming complete.
        doubled = "//" + str(self.base)[1:]
        result = self.inspect(staging_base=doubled)
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))
        self.assertEqual(result["issues"], [])
        ab = self.ref(result, ("com.example.A", "com.example.B"))
        self.assertEqual(ab["origin"]["source"], str(self.checkout / "app/root.jar"))
        self.assertFalse(Path(result["staging"]["stage_dir"]).exists())

    def test_double_slash_base_mr_issue_keeps_original_sources(self):
        # Same noncanonical spelling, but through every original-coordinate
        # surface: the surfaced helper issue must carry input_source, the
        # inputs-not-analyzed message must name the ORIGINAL source, and no
        # deleted-stage path may be labeled as an original source. The raw
        # runner result keeps its staged spellings unchanged.
        doubled = "//" + str(self.base)[1:]
        result = self.inspect(deps=("app/dep-b.jar", "app/mr.jar"),
                              staging_base=doubled)
        self.assertEqual(result["status"], "partial")
        mr_original = str(self.checkout / "app/mr.jar")
        surfaced = next(issue for issue in result["issues"]
                        if issue["code"] == "unsupported-multi-release-jar")
        self.assertEqual(surfaced["input_source"], mr_original)
        self.assertTrue(surfaced["input"].startswith(str(self.base) + os.sep))
        skipped = next(issue for issue in result["issues"]
                       if issue["code"] == "inputs-not-analyzed")
        self.assertIn(mr_original, skipped["message"])
        self.assertNotIn("input-", skipped["message"])
        self.assertNotIn(str(self.base), skipped["message"])
        # Every ref still maps; nothing was dropped as unmapped.
        self.assertEqual(len(result["source_refs"]), len(result["runner"]["refs"]))
        self.assertNotIn("unmapped-staged-path",
                         {issue["code"] for issue in result["issues"]})
        # The raw runner result is untouched: staged spellings, no input_source.
        raw_mr_issue = next(issue for issue in result["runner"]["issues"]
                            if issue["code"] == "unsupported-multi-release-jar")
        self.assertNotIn("input_source", raw_mr_issue)
        self.assertTrue(raw_mr_issue["input"].startswith(str(self.base) + os.sep))

    def test_unmapped_report_path_downgrades_to_partial(self):
        # If a report path cannot be matched to a staged input, the dropped
        # ref is an issue, and no issue may coexist with a complete verdict.
        real = usage.run_usage_helper

        def distort(root, deps, **kw):
            outcome = real(root, deps, **kw)
            refs = outcome["refs"]
            if refs:
                outcome = dict(outcome)
                outcome["refs"] = [dict(refs[0], origin_input="/nonexistent-input"),
                                   *refs[1:]]
            return outcome

        with mock.patch.object(usage, "run_usage_helper", distort):
            result = self.inspect()
        self.assertEqual(result["status"], "partial")
        self.assertIn("unmapped-staged-path",
                      {issue["code"] for issue in result["issues"]})
        self.assertTrue(self.ref(result, ("com.example.A", "com.example.B")))
        self.assertEqual(len(result["source_refs"]), len(result["runner"]["refs"]) - 1)

    # -- runner-option preflight (TASK-21.6) ----------------------------------

    def test_invalid_options_rejected_before_staging(self):
        # Clearly invalid option configuration never reaches staging: no
        # copying is attempted, no stage exists (cleanup not-reached), no
        # synthetic runner result claims a run, and the refusal keeps the
        # runner's underlying code visible inside runner-option-refused.
        first = self.cp_paths[0]
        cases = [
            ("invalid-scope", {"scope": "dev"}),
            ("invalid-classpath", {"helper_classpath": []}),
            ("invalid-expected-hashes",
             {"expected": {**self.expected, first: "0" * 63}}),
        ]
        for code, override in cases:
            with self.subTest(code=code):
                boom = mock.Mock(side_effect=AssertionError("staging must not run"))
                with mock.patch.object(staging, "staged_inputs", boom):
                    result = self.inspect(**override)
                self.assertEqual(result["status"], "rejected")
                self.assertIsNone(result["runner"])
                self.assertEqual(result["source_refs"], [])
                self.assertEqual(result["cleanup"], "not-reached")
                # Same refusal-record shape as a staging refusal, but no
                # staging summary: no bundle was ever produced.
                self.assertEqual(set(result["staging"]),
                                 {"stage_id", "stage_dir", "complete", "refusal",
                                  "observations", "cleanup_status", "leftovers"})
                self.assertEqual(result["staging"]["stage_id"], None)
                self.assertEqual(result["staging"]["stage_dir"], None)
                self.assertFalse(result["staging"]["complete"])
                self.assertEqual(result["staging"]["cleanup_status"], "not-created")
                self.assertEqual(result["staging"]["leftovers"], [])
                self.assertEqual(result["staging"]["observations"], [])
                self.assertEqual(result["staging"]["refusal"]["code"],
                                 "runner-option-refused")
                self.assertIn(code, result["staging"]["refusal"]["message"])
                self.assertEqual([issue["code"] for issue in result["issues"]],
                                 ["runner-option-refused"])
                self.assertEqual(result["issues"][0]["source"], "runner")
                self.assertIn(code, result["issues"][0]["message"])
                self.assertEqual(self.base_entries(), [])
                json.loads(json.dumps(result))
                boom.assert_not_called()

    def test_invalid_budget_propagates_before_staging(self):
        # Numeric argument errors keep the runner's TypeError/ValueError
        # contract (programming errors never become structured results);
        # they now surface before staging instead of after it.
        boom = mock.Mock(side_effect=AssertionError("staging must not run"))
        cases = [({"timeout_seconds": 0}, ValueError),
                 ({"heap_mb": True}, TypeError),
                 ({"max_output_bytes": "big"}, TypeError)]
        for override, exc_type in cases:
            with self.subTest(override=override):
                with mock.patch.object(staging, "staged_inputs", boom), \
                        self.assertRaises(exc_type):
                    self.inspect(**override)
        boom.assert_not_called()
        self.assertEqual(self.base_entries(), [])

    def test_helper_bytes_changed_after_preflight_still_rejected(self):
        # The preflight is a shape check, not authorization: helper bytes
        # changed after it passed still fail the runner's own post-staging
        # hash gate, with staging having really happened and cleaned up. The
        # class-scoped helper jar is restored in a finally, so nothing is
        # permanently mutated for the other tests sharing the package.
        helper = Path(self.cp_paths[0])
        original = helper.read_bytes()
        seen = []
        real_preflight = usage.validate_usage_options

        def mutate_after_pass(scope, helper_classpath, expected_hashes, **kw):
            options = real_preflight(scope, helper_classpath, expected_hashes,
                                     **kw)
            seen.append(options)
            if not isinstance(options, dict):
                with open(helper, "ab") as fh:
                    fh.write(b"changed-after-preflight")
            return options

        try:
            with mock.patch.object(usage, "validate_usage_options",
                                   mutate_after_pass):
                result = self.inspect()
        finally:
            helper.write_bytes(original)
        # The preflight itself passed on the original bytes.
        self.assertEqual(seen, [self.cp_paths])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["runner"]["status"], "rejected")
        self.assertIn("hash-mismatch",
                      {issue["code"] for issue in result["issues"]})
        # Staging genuinely ran and cleaned; the rejection came from the
        # runner's hash gate, not from a skipped pipeline.
        self.assertTrue(result["staging"]["complete"])
        self.assertEqual(result["cleanup"], "complete")
        self.assertEqual(self.base_entries(), [])
        self.assertEqual(sha256_file(helper), self.expected[str(helper)])

    def test_one_shot_classpath_iterable_completes_real_pipeline(self):
        # The pipeline re-uses the preflight's materialized classpath copy,
        # so a one-shot iterable is not exhausted between the preflight and
        # the runner consuming it inside the staging context.
        result = usage_pipeline.inspect_usage(
            self.checkout, self.checkout / "app/root.jar",
            [self.checkout / "app/dep-b.jar"], scope="production",
            staging_base=self.base, helper_classpath=iter(self.cp_paths),
            expected_hashes=dict(self.expected))
        self.assertEqual(result["status"], "complete", msg=str(result["issues"]))


if __name__ == "__main__":
    unittest.main()
