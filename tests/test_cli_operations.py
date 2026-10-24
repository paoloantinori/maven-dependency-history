"""End-to-end subprocess tests for propose/execute wiring (TASK23 next slice).

Real subprocess invocations of the copied skill's investigate.py, covering
the inert proposal of Maven diagnostics and advisory lookups, the strict
approval-file execution path with a synthetic Maven executable fixture, and
every refusal path: denial, replay, stale revision, malformed or duplicate-
key approval files, unknown operations, missing tools, unselected or
unresolvable candidates, and the privacy guarantee that raw child output
never reaches stdout or the persisted report. No network, no real Maven: the
advisory happy path is deliberately not exercised (the default transport is
live), so advisory tests are refusal-only, and the Maven fixture is a shell
script standing in for the approved executable. Nothing outside the
temporary root is touched.
"""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SKILL_SRC = REPO / "skills" / "maven-dependency-investigator"

NS = "http://maven.apache.org/POM/4.0.0"
_TIMEOUT = 300

TREE_JSON = (
    '{"groupId":"org.example","artifactId":"app","version":"1.0.0",'
    '"type":"jar","scope":"compile","classifier":"","optional":false,'
    '"children":[{"groupId":"org.example","artifactId":"b-lib","version":"1.0.0",'
    '"type":"jar","scope":"compile","classifier":"","optional":false,"children":[]}]}'
)

FAKE_MVN = """#!/bin/sh
printf 'executed\\n' >> {log}
printf 'SECRET-OUTPUT-MARKER-stdout\\n'
printf 'SECRET-OUTPUT-MARKER-stderr\\n' >&2
out=""
for a in "$@"; do
  case "$a" in
    -DoutputFile=*) out=${{a#-DoutputFile=}} ;;
    -Doutput=*) out=${{a#-Doutput=}} ;;
    -Dmdep.outputFile=*) out=${{a#-Dmdep.outputFile=}} ;;
  esac
done
if [ -n "$out" ]; then printf '%s' '{tree}' > "$out"; fi
exit 0
"""


def _pom(deps, properties=""):
    rows = "".join(
        f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>"
        f"<version>{v}</version></dependency>"
        for g, a, v in deps
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<project xmlns="{NS}">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>org.example</groupId>\n"
        "  <artifactId>app</artifactId>\n"
        "  <version>1.0.0</version>\n"
        f"  <properties>{properties}</properties>\n"
        f"  <dependencies>\n{rows}\n  </dependencies>\n"
        "</project>\n"
    )


def make_project(root, deps=(("org.example", "b-lib", "1.0.0"),), properties=""):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(_pom(deps, properties), encoding="utf-8")
    return root


def make_multi_module_project(root):
    """Root aggregator plus module sub/ declaring d-lib; d-lib lives only in
    the submodule, so a proposal scoped to the selected subject must target
    sub/pom.xml, not the repository root."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<project xmlns="{NS}">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>org.example</groupId>\n"
        "  <artifactId>agg</artifactId>\n"
        "  <version>1.0.0</version>\n"
        "  <packaging>pom</packaging>\n"
        "  <modules><module>sub</module></modules>\n"
        "</project>\n", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "pom.xml").write_text(
        _pom((("org.example", "d-lib", "2.0.0"),)).replace(
            "<artifactId>app</artifactId>", "<artifactId>sub</artifactId>"),
        encoding="utf-8")
    return root


def make_plugin_project(root, plugin=("org.example", "p-lib", "1.1.0")):
    """Single-module project whose only p-lib occurrence is a build plugin."""
    root = make_project(root, deps=())
    pom = (root / "pom.xml").read_text(encoding="utf-8")
    pom = pom.replace(
        "</dependencies>",
        "</dependencies>\n  <build><plugins><plugin>"
        f"<groupId>{plugin[0]}</groupId><artifactId>{plugin[1]}</artifactId>"
        f"<version>{plugin[2]}</version>"
        "</plugin></plugins></build>")
    (root / "pom.xml").write_text(pom, encoding="utf-8")
    return root


def make_fake_maven(directory, name="fake-mvn.sh"):
    """Synthetic approved executable: logs its execution, prints secret-ish
    markers on both streams, and writes the -D output file like a pinned
    dependency plugin would."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "execution.log"
    script = directory / name
    script.write_text(FAKE_MVN.format(log=log, tree=TREE_JSON), encoding="utf-8")
    mode = script.stat().st_mode | stat.S_IXUSR
    script.chmod(mode)
    return script, log


class OperationsCliTestCase(unittest.TestCase):
    """Shared copied-skill harness; every invocation is a real subprocess."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mdi-cli-ops-tests-"))
        cls.skill = cls.tmp / "skill"
        shutil.copytree(SKILL_SRC, cls.skill,
                        ignore=shutil.ignore_patterns("__pycache__"))
        cls.inv = [sys.executable, str(cls.skill / "scripts" / "investigate.py")]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.tmp))
        self.state = self.work / "state"
        self.project = make_project(self.work / "proj")
        self.maven, self.log = make_fake_maven(self.work / "tools")
        self.output = self.work / "out" / "tree.json"
        self.output.parent.mkdir(parents=True, exist_ok=True)

    def run_cli(self, *args, cwd=None):
        env = dict(os.environ)
        env["XDG_STATE_HOME"] = str(self.state)
        return subprocess.run(self.inv + [str(a) for a in args],
                              capture_output=True, text=True, timeout=_TIMEOUT,
                              env=env, cwd=str(cwd) if cwd else None)

    def parse(self, proc):
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        return json.loads(proc.stdout)

    def error(self, proc):
        self.assertEqual(proc.returncode, 2,
                         f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        return json.loads(proc.stdout)["error"]["message"]

    def selected_run(self, query="org.example:b-lib:1.0.0"):
        proc = self.run_cli("discover", "--repo", self.project, "--query", query,
                            "--state-dir", self.state)
        doc = self.parse(proc)
        self.assertFalse(doc["no_match"], doc)
        candidate = doc["candidates"][0]["id"]
        self.parse(self.run_cli("select", "--run-dir", doc["run_dir"],
                                "--expect-revision", "1",
                                "--candidate", candidate))
        return doc["run_dir"], doc["run_id"], candidate

    def read_operation(self, run_dir):
        doc = self.parse(self.run_cli("report", "--run-dir", run_dir))
        self.assertEqual(len(doc["operations"]), 1)
        return doc["operations"][0]

    def propose_and_select(self, kind="tree"):
        run_dir, run_id, candidate = self.selected_run()
        if kind == "advisory":
            proc = self.run_cli("propose", "--run-dir", run_dir,
                                "--expect-revision", "2", "--kind", kind,
                                "--candidate", candidate)
        else:
            proc = self.run_cli("propose", "--run-dir", run_dir,
                                "--expect-revision", "2", "--kind", kind,
                                "--candidate", candidate,
                                "--maven", self.maven, "--output", self.output,
                                "--input", self.project / "pom.xml",
                                "--destinations-unknown")
        doc = self.parse(proc)
        return run_dir, doc["operation"]

    def write_approval(self, run_dir, operation, **overrides):
        approval = {
            "run_id": operation["run_id"],
            "operation_id": operation["id"],
            "scope": operation["scope"],
            "approved": True,
            "approved_by": "the host user",
            "reference": "interactive session 2026-09-12",
        }
        approval.update(overrides)
        path = self.work / f"approval-{operation['id']}.json"
        path.write_text(json.dumps(approval), encoding="utf-8")
        return path


class ProposeTests(OperationsCliTestCase):

    def test_propose_maven_tree_is_inert_and_exact(self):
        run_dir, _, candidate = self.selected_run()
        proc = self.run_cli(
            "propose", "--run-dir", run_dir, "--expect-revision", "2",
            "--kind", "tree", "--candidate", candidate,
            "--maven", self.maven, "--output", self.output,
            "--input", self.project / "pom.xml",
            "--destinations-unknown")
        doc = self.parse(proc)
        self.assertEqual(doc["revision"], 3)
        record = doc["operation"]
        self.assertEqual(record["kind"], "maven")
        self.assertEqual(record["state"], "proposed")
        self.assertEqual(record["scope"]["cwd"], str(self.project))
        self.assertEqual(record["scope"]["argv"][0], str(self.maven))
        self.assertIn("org.apache.maven.plugins:maven-dependency-plugin:3.11.0:tree",
                      record["scope"]["argv"])
        self.assertTrue(any(item.startswith("-DoutputFile=") for item
                            in record["scope"]["argv"]))
        self.assertIn("downloads-destinations: unknown", record["disclosure"])
        self.assertIn("never creates an approval file", doc["approval"])
        persisted = self.read_operation(run_dir)
        self.assertEqual(persisted["state"], "proposed")
        self.assertEqual(persisted["audit"], {})
        # inert: the executable was hashed and pinned, never executed
        self.assertFalse(self.log.exists())

    def test_propose_pins_explicit_destinations(self):
        run_dir, _, candidate = self.selected_run()
        proc = self.run_cli(
            "propose", "--run-dir", run_dir, "--expect-revision", "2",
            "--kind", "classpath", "--candidate", candidate,
            "--maven", self.maven, "--output", self.output,
            "--input", self.project / "pom.xml",
            "--destination", "repo.maven.apache.org (https)")
        doc = self.parse(proc)
        self.assertIn("downloads-destinations: explicit: repo.maven.apache.org (https)",
                      doc["operation"]["disclosure"])

    def test_propose_requires_exact_selected_candidate(self):
        # discover several candidates, select only the first, then attempt to
        # propose against a discovered-but-unselected one and an unknown id
        project = make_project(self.work / "multi",
                               deps=(("org.example", "b-lib", "1.0.0"),
                                     ("org.example", "d-lib", "3.0.0")))
        proc = self.run_cli("discover", "--repo", project, "--query", "lib",
                            "--state-dir", self.state)
        doc = self.parse(proc)
        candidates = [c["id"] for c in doc["candidates"]]
        self.assertGreater(len(candidates), 1)
        self.parse(self.run_cli("select", "--run-dir", doc["run_dir"],
                                "--expect-revision", "1",
                                "--candidate", candidates[0]))
        for bad in (candidates[1], "mdi-not-a-candidate"):
            proc = self.run_cli("propose", "--run-dir", doc["run_dir"],
                                "--expect-revision", "2", "--kind", "tree",
                                "--candidate", bad,
                                "--maven", self.maven, "--output", self.output,
                                "--input", self.project / "pom.xml",
                                "--destinations-unknown")
            self.assertIn("not part of the recorded selection", self.error(proc))

    def test_propose_without_selection_is_invalid(self):
        proc = self.run_cli("discover", "--repo", self.project,
                            "--query", "org.example:b-lib:1.0.0",
                            "--state-dir", self.state)
        run_dir = self.parse(proc)["run_dir"]
        proc = self.run_cli("propose", "--run-dir", run_dir,
                            "--expect-revision", "1", "--kind", "tree",
                            "--candidate", "mdi-x", "--maven", self.maven,
                            "--output", self.output,
                            "--input", self.project / "pom.xml",
                            "--destinations-unknown")
        self.assertIn("requires a recorded selection", self.error(proc))

    def test_propose_maven_requires_explicit_parameters(self):
        run_dir, _, candidate = self.selected_run()
        base = ["propose", "--run-dir", run_dir, "--expect-revision", "2",
                "--kind", "tree", "--candidate", candidate]
        missing_maven = self.run_cli(*base, "--output", self.output,
                                     "--input", self.project / "pom.xml",
                                     "--destinations-unknown")
        self.assertIn("--maven", self.error(missing_maven))
        relative_maven = self.run_cli(*base, "--maven", "mvn",
                                      "--output", self.output,
                                      "--input", self.project / "pom.xml",
                                      "--destinations-unknown")
        self.assertIn("exact absolute path", self.error(relative_maven))
        relative_output = self.run_cli(*base, "--maven", self.maven,
                                       "--output", "out/tree.json",
                                       "--input", self.project / "pom.xml",
                                       "--destinations-unknown")
        self.assertIn("--output", self.error(relative_output))
        no_inputs = self.run_cli(*base, "--maven", self.maven,
                                 "--output", self.output,
                                 "--destinations-unknown")
        self.assertIn("--input", self.error(no_inputs))
        no_destinations = self.run_cli(*base, "--maven", self.maven,
                                       "--output", self.output,
                                       "--input", self.project / "pom.xml")
        self.assertIn("never inferred from absent flags",
                      self.error(no_destinations))
        both_destinations = self.run_cli(*base, "--maven", self.maven,
                                         "--output", self.output,
                                         "--input", self.project / "pom.xml",
                                         "--destinations-unknown",
                                         "--destination", "example.org")
        self.assertIn("not both", self.error(both_destinations))

    def test_propose_stale_revision_is_conflict(self):
        run_dir, _, candidate = self.selected_run()
        proc = self.run_cli("propose", "--run-dir", run_dir,
                            "--expect-revision", "1", "--kind", "tree",
                            "--candidate", candidate, "--maven", self.maven,
                            "--output", self.output,
                            "--input", self.project / "pom.xml",
                            "--destinations-unknown")
        self.assertEqual(proc.returncode, 3)

    def test_propose_advisory_requires_exact_known_gav(self):
        run_dir, _, candidate = self.selected_run()
        proc = self.run_cli("propose", "--run-dir", run_dir,
                            "--expect-revision", "2", "--kind", "advisory",
                            "--candidate", candidate)
        doc = self.parse(proc)
        record = doc["operation"]
        self.assertEqual(record["kind"], "advisory")
        self.assertEqual(record["scope"]["query"]["name"], "org.example:b-lib")
        self.assertEqual(record["scope"]["query"]["version"], "1.0.0")
        self.assertTrue(record["scope"]["destination"].startswith("https://"))
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")

    def test_propose_advisory_refuses_unresolved_version(self):
        # a declaration whose version is a raw placeholder is not a resolved g:a:v
        self.project = make_project(
            self.work / "placeholder",
            deps=(("org.example", "b-lib", "${b-lib.version}"),),
            properties="<b-lib.version>1.0.0</b-lib.version>")
        proc = self.run_cli("discover", "--repo", self.project,
                            "--query", "org.example:b-lib",
                            "--state-dir", self.state)
        doc = self.parse(proc)
        run_dir, candidate = doc["run_dir"], doc["candidates"][0]["id"]
        self.parse(self.run_cli("select", "--run-dir", run_dir,
                                "--expect-revision", "1",
                                "--candidate", candidate))
        proc = self.run_cli("propose", "--run-dir", run_dir,
                            "--expect-revision", "2", "--kind", "advisory",
                            "--candidate", candidate)
        self.assertIn("lacks known", self.error(proc))

    def test_propose_advisory_invalid_known_version_is_rejected_input(self):
        for index, version in enumerate(('1.0 0', 'v' * 4104)):
            with self.subTest(length=len(version)):
                self.project = make_project(
                    self.work / f'invalid-version-{index}',
                    deps=(("org.example", "b-lib", version),))
                run_dir, _, candidate = self.selected_run('org.example:b-lib')
                proc = self.run_cli('propose', '--run-dir', run_dir,
                                    '--expect-revision', '2', '--kind', 'advisory',
                                    '--candidate', candidate)
                self.assertIn('advisory proposal rejected', self.error(proc))
                self.assertNotIn(version, proc.stdout)
                report = self.parse(self.run_cli('report', '--run-dir', run_dir))
                self.assertEqual(report['revision'], 2)
                self.assertEqual(report['operations'], [])
                self.assertFalse(self.log.exists())

    def test_propose_advisory_page_token_is_a_new_explicit_proposal(self):
        run_dir, _, candidate = self.selected_run()
        first = self.parse(self.run_cli(
            "propose", "--run-dir", run_dir, "--expect-revision", "2",
            "--kind", "advisory", "--candidate", candidate))
        second = self.parse(self.run_cli(
            "propose", "--run-dir", run_dir, "--expect-revision", "3",
            "--kind", "advisory", "--candidate", candidate,
            "--page-token", "page-two"))
        self.assertNotEqual(first["operation"]["id"], second["operation"]["id"])
        self.assertEqual(second["operation"]["scope"]["query"]["page_token"],
                         "page-two")
        self.assertIn("page_token", second["operation"]["disclosure"])

    def test_propose_advisory_rejects_maven_parameters(self):
        run_dir, _, candidate = self.selected_run()
        proc = self.run_cli("propose", "--run-dir", run_dir,
                            "--expect-revision", "2", "--kind", "advisory",
                            "--candidate", candidate,
                            "--maven", self.maven)
        self.assertIn("take no Maven parameters", self.error(proc))

    def test_bare_verbs_require_explicit_flags(self):
        for command in ("propose", "execute"):
            proc = self.run_cli(command)
            self.assertEqual(proc.returncode, 2, proc.stdout)
            body = json.loads(proc.stdout)
            self.assertFalse(body["ok"])
            self.assertIn("invalid invocation", body["error"]["message"])


class ModuleScopedProposalTests(OperationsCliTestCase):

    def propose_for(self, project, query, kind="tree"):
        proc = self.run_cli("discover", "--repo", project, "--query", query,
                            "--state-dir", self.state)
        doc = self.parse(proc)
        self.assertFalse(doc["no_match"], doc)
        candidate = doc["candidates"][0]
        self.parse(self.run_cli("select", "--run-dir", doc["run_dir"],
                                "--expect-revision", "1",
                                "--candidate", candidate["id"]))
        proc = self.run_cli("propose", "--run-dir", doc["run_dir"],
                            "--expect-revision", "2", "--kind", kind,
                            "--candidate", candidate["id"],
                            "--maven", self.maven, "--output", self.output,
                            "--input", project / "pom.xml",
                            "--destinations-unknown")
        return doc["run_dir"], candidate, self.parse(proc)

    def test_propose_scopes_to_selected_module_pom(self):
        project = make_multi_module_project(self.work / "multi")
        run_dir, candidate, doc = self.propose_for(
            project, "org.example:d-lib:2.0.0")
        record = doc["operation"]
        argv = record["scope"]["argv"]
        self.assertEqual(candidate["module"], "sub")
        self.assertIn("-N", argv)
        self.assertEqual(argv[argv.index("-f") + 1], str(project / "sub" / "pom.xml"))
        self.assertNotEqual(argv[argv.index("-f") + 1], str(project / "pom.xml"))
        # the selected module POM is pinned as an input even though the
        # caller never listed it; the selected module is never ignored
        pinned = {item["path"] for item in record["input_identities"]}
        self.assertIn(str(project / "sub" / "pom.xml"), pinned)
        self.assertEqual(doc["module"], {"id": "sub",
                                         "pom": str(project / "sub" / "pom.xml")})
        self.assertFalse(self.log.exists())

    def test_propose_root_module_scopes_to_root_pom(self):
        run_dir, candidate, doc = self.propose_for(
            self.project, "org.example:b-lib:1.0.0")
        argv = doc["operation"]["scope"]["argv"]
        self.assertEqual(candidate["module"], ".")
        self.assertEqual(argv[argv.index("-f") + 1], str(self.project / "pom.xml"))
        self.assertIn("-N", argv)

    def test_propose_effective_pom_carries_verbose_flag(self):
        # the pinned help plugin defaults verbose=false, which emits no
        # origin comments; the diagnostic must request them explicitly
        project = make_project(self.work / "single")
        run_dir, candidate, doc = self.propose_for(
            project, "org.example:b-lib:1.0.0", kind="effective-pom")
        argv = doc["operation"]["scope"]["argv"]
        self.assertIn("-Dverbose=true", argv)
        self.assertTrue(any(item.startswith("-Doutput=") for item in argv))
        self.assertIn("org.apache.maven.plugins:maven-help-plugin:3.5.2:effective-pom",
                      argv)

    def test_propose_refuses_build_plugin_realm(self):
        project = make_plugin_project(self.work / "pluginproj")
        proc = self.run_cli("discover", "--repo", project,
                            "--query", "org.example:p-lib",
                            "--state-dir", self.state)
        doc = self.parse(proc)
        self.assertEqual(doc["candidates"][0]["realm"], "build-plugin")
        self.parse(self.run_cli("select", "--run-dir", doc["run_dir"],
                                "--expect-revision", "1",
                                "--candidate", doc["candidates"][0]["id"]))
        proc = self.run_cli("propose", "--run-dir", doc["run_dir"],
                            "--expect-revision", "2", "--kind", "tree",
                            "--candidate", doc["candidates"][0]["id"],
                            "--maven", self.maven, "--output", self.output,
                            "--input", project / "pom.xml",
                            "--destinations-unknown")
        self.assertIn("build-plugin", self.error(proc))
        report = self.parse(self.run_cli("report", "--run-dir", doc["run_dir"]))
        self.assertEqual(report["operations"], [])

    def test_propose_refuses_missing_selected_module_pom(self):
        project = make_multi_module_project(self.work / "multi")
        proc = self.run_cli("discover", "--repo", project,
                            "--query", "org.example:d-lib:2.0.0",
                            "--state-dir", self.state)
        doc = self.parse(proc)
        candidate = doc["candidates"][0]
        self.parse(self.run_cli("select", "--run-dir", doc["run_dir"],
                                "--expect-revision", "1",
                                "--candidate", candidate["id"]))
        os.unlink(project / "sub" / "pom.xml")
        proc = self.run_cli("propose", "--run-dir", doc["run_dir"],
                            "--expect-revision", "2", "--kind", "tree",
                            "--candidate", candidate["id"],
                            "--maven", self.maven, "--output", self.output,
                            "--input", project / "pom.xml",
                            "--destinations-unknown")
        self.assertIn("no pom.xml", self.error(proc))


class StagingLocationTests(OperationsCliTestCase):

    def test_staging_base_inside_checkout_rejected_before_creation(self):
        run_dir, _, _ = self.selected_run()
        inside = self.project / "stage"
        proc = self.run_cli("collect", "--run-dir", run_dir,
                            "--expect-revision", "2",
                            "--usage-root", self.project / "target" / "app.jar",
                            "--usage-dependency", self.project / "target" / "dep.jar",
                            "--staging-base", inside)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("overlaps the checkout",
                      json.loads(proc.stdout)["error"]["message"])
        self.assertFalse(inside.exists())

    def test_staging_base_symlink_alias_rejected_untouched(self):
        run_dir, _, _ = self.selected_run()
        alias = self.work / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        aliased = alias / "staging"
        proc = self.run_cli("collect", "--run-dir", run_dir,
                            "--expect-revision", "2",
                            "--usage-root", self.project / "target" / "app.jar",
                            "--usage-dependency", self.project / "target" / "dep.jar",
                            "--staging-base", aliased)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("overlaps the checkout",
                      json.loads(proc.stdout)["error"]["message"])
        self.assertFalse((self.project / "staging").exists())
        self.assertFalse((alias / "staging").exists())


class ExecuteTests(OperationsCliTestCase):

    def test_execute_maven_happy_path_with_synthetic_fixture(self):
        run_dir, operation = self.propose_and_select("tree")
        approval = self.write_approval(run_dir, operation)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", approval)
        doc = self.parse(proc)
        self.assertEqual(doc["outcome"], "succeeded")
        self.assertEqual(doc["revision"], 6)  # consume, evidence, finalize
        self.assertTrue(doc["output_meta"]["sha256"])
        self.assertFalse(doc["output_meta"]["capped"])
        self.assertEqual(doc["output_report_path"], str(self.output))
        self.assertIn("not performed", doc["import"])
        report = self.parse(self.run_cli("report", "--run-dir", run_dir))
        finished = report["operations"][0]
        self.assertEqual(finished["state"], "finished")
        evidence = [e for e in report["evidence"] if e["id"] == doc["evidence_id"]][0]
        self.assertEqual(evidence["category"], "maven-run")
        self.assertTrue(self.log.exists())
        # privacy: the child printed secret markers on both streams
        self.assertNotIn("SECRET-OUTPUT-MARKER", proc.stdout)
        self.assertNotIn("SECRET-OUTPUT-MARKER", proc.stderr)
        self.assertNotIn("SECRET-OUTPUT-MARKER", json.dumps(report))
        # the produced report only becomes parsed evidence through explicit import
        self.parse(self.run_cli("import", "--run-dir", run_dir,
                                "--expect-revision", "6",
                                "--report", self.output, "--kind", "tree-json"))
        report = self.parse(self.run_cli("report", "--run-dir", run_dir))
        self.assertTrue([e for e in report["evidence"]
                         if e["category"] == "imported-tree-json"])

    def test_execute_denial_keeps_operation_proposed(self):
        run_dir, operation = self.propose_and_select("tree")
        approval = self.write_approval(run_dir, operation, approved=False)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", approval)
        message = self.error(proc)
        self.assertIn("approved", message)
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")
        self.assertFalse(self.log.exists())

    def test_execute_replay_after_success_is_refused(self):
        run_dir, operation = self.propose_and_select("tree")
        approval = self.write_approval(run_dir, operation)
        self.parse(self.run_cli("execute", "--run-dir", run_dir,
                                "--expect-revision", "3",
                                "--operation", operation["id"],
                                "--approval-file", approval))
        replay = self.run_cli("execute", "--run-dir", run_dir,
                              "--expect-revision", "6",
                              "--operation", operation["id"],
                              "--approval-file", approval)
        self.assertEqual(replay.returncode, 2)
        self.assertIn("finished", json.loads(replay.stdout)["error"]["message"])
        self.assertEqual(self.log.read_text(encoding="utf-8").count("executed"), 1)

    def test_execute_stale_revision_refused_before_consent(self):
        run_dir, operation = self.propose_and_select("tree")
        approval = self.write_approval(run_dir, operation)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "2",
                            "--operation", operation["id"],
                            "--approval-file", approval)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")
        self.assertFalse(self.log.exists())

    def test_execute_success_without_output_file_claims_no_parsed_tree(self):
        # the fixture exits 0 without writing the pinned output file: process
        # success is recorded, no tree is claimed, and the explicit import of
        # the absent file is refused instead of invented
        run_dir, operation = self.propose_and_select("tree")
        shutil.rmtree(self.output.parent)
        approval = self.write_approval(run_dir, operation)
        doc = self.parse(self.run_cli("execute", "--run-dir", run_dir,
                                      "--expect-revision", "3",
                                      "--operation", operation["id"],
                                      "--approval-file", approval))
        self.assertEqual(doc["outcome"], "succeeded")
        self.assertIn("not performed", doc["import"])
        report = self.parse(self.run_cli("report", "--run-dir", run_dir))
        self.assertFalse([e for e in report["evidence"]
                          if e["category"].startswith("imported")])
        missing = self.run_cli("import", "--run-dir", run_dir,
                               "--expect-revision", "6",
                               "--report", self.output, "--kind", "tree-json")
        self.assertEqual(missing.returncode, 2)

    def test_execute_malformed_approvals_are_refused(self):
        run_dir, operation = self.propose_and_select("tree")
        garbage = self.work / "garbage.json"
        garbage.write_text("{not json", encoding="utf-8")
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", garbage)
        self.assertIn("not valid strict JSON", self.error(proc))
        duplicate = self.work / "duplicate.json"
        duplicate.write_text(json.dumps(
            {"run_id": operation["run_id"], "operation_id": operation["id"],
             "scope": operation["scope"], "approved": True,
             "approved_by": "a", "reference": "b"})[:-1] +
            ', "approved": false}\n', encoding="utf-8")
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", duplicate)
        self.assertIn("duplicate JSON key", self.error(proc))
        wrong_run = self.write_approval(run_dir, operation, run_id="00000000-0000-0000-0000-000000000000")
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", wrong_run)
        self.assertIn("run_id does not match", self.error(proc))
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")
        self.assertFalse(self.log.exists())

    def test_execute_unknown_operation_is_invalid(self):
        run_dir, _ = self.propose_and_select("tree")
        approval = self.work / "any.json"
        approval.write_text("{}", encoding="utf-8")
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", "op-missing",
                            "--approval-file", approval)
        self.assertIn("unknown operation", self.error(proc))

    def test_execute_missing_tool_keeps_operation_proposed(self):
        run_dir, operation = self.propose_and_select("tree")
        os.unlink(self.maven)
        approval = self.write_approval(run_dir, operation)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", approval)
        message = self.error(proc)
        self.assertIn("execution refused", message)
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")

    def test_execute_advisory_denial_never_touches_network(self):
        run_dir, operation = self.propose_and_select("advisory")
        approval = self.write_approval(run_dir, operation, approved=False)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", approval)
        self.error(proc)
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")

    def test_execute_missing_required_flags_is_invalid(self):
        run_dir, operation = self.propose_and_select("tree")
        approval = self.write_approval(run_dir, operation)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"])
        self.assertIn("--approval-file", self.error(proc))
        proc = self.run_cli("execute", "--run-dir", run_dir)
        self.assertIn("--operation", self.error(proc))


class OutputPrestateTests(OperationsCliTestCase):
    """F1: the output target's prestate is captured before the proposal,
    bound immutably to the approval via tool identity and disclosure, and
    re-observed before consent is spent."""

    def _propose_tree(self, run_dir, candidate, overwrite=False):
        args = ["propose", "--run-dir", run_dir, "--expect-revision", "2",
                "--kind", "tree", "--candidate", candidate,
                "--maven", self.maven, "--output", self.output,
                "--input", self.project / "pom.xml", "--destinations-unknown"]
        if overwrite:
            args.append("--output-overwrite")
        return self.run_cli(*args)

    def test_propose_absent_output_binds_creation_expectation(self):
        run_dir, _, candidate = self.selected_run()
        doc = self.parse(self._propose_tree(run_dir, candidate))
        record = doc["operation"]
        self.assertEqual(record["tool_identity"]["output"],
                         {"path": str(self.output), "state": "absent",
                          "max_bytes": 8 * 1024**2})
        self.assertIn(f"Output target {self.output} is absent now",
                      record["disclosure"])
        self.assertIn("not atomic no-clobber", record["disclosure"])
        self.assertFalse(self.output.exists())

    def test_propose_existing_output_refused_without_explicit_overwrite(self):
        run_dir, _, candidate = self.selected_run()
        self.output.write_text("pre-existing user content\n", encoding="utf-8")
        proc = self._propose_tree(run_dir, candidate)
        self.assertIn("never implicit", self.error(proc))
        # nothing was written and the operation was never proposed
        self.assertEqual(self.output.read_text(encoding="utf-8"),
                         "pre-existing user content\n")
        report = self.parse(self.run_cli("report", "--run-dir", run_dir))
        self.assertEqual(report["operations"], [])

    def test_propose_existing_output_with_explicit_overwrite_discloses_prestate(self):
        run_dir, _, candidate = self.selected_run()
        self.output.write_text("pre-existing user content\n", encoding="utf-8")
        doc = self.parse(self._propose_tree(run_dir, candidate, overwrite=True))
        record = doc["operation"]
        self.assertEqual(record["tool_identity"]["output"]["state"], "present")
        self.assertIn("overwrite of this observed file was explicitly approved",
                      record["disclosure"])
        self.assertEqual(self.output.read_text(encoding="utf-8"),
                         "pre-existing user content\n")

    def test_execute_refuses_when_output_created_between_propose_and_execute(self):
        run_dir, operation = self.propose_and_select("tree")
        self.output.write_text("appeared after the proposal\n", encoding="utf-8")
        approval = self.write_approval(run_dir, operation)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", approval)
        self.assertIn("differs from the proposal",
                      json.loads(proc.stdout)["error"]["message"])
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")
        # the interfering file is untouched: nothing was spawned
        self.assertEqual(self.output.read_text(encoding="utf-8"),
                         "appeared after the proposal\n")
        self.assertFalse(self.log.exists())

    def test_execute_refuses_when_pinned_output_changed_or_removed(self):
        # changed content between propose (with explicit overwrite) and execute
        run_dir, _, candidate = self.selected_run()
        self.output.write_text("version one\n", encoding="utf-8")
        self.parse(self._propose_tree(run_dir, candidate, overwrite=True))
        operation = self.read_operation(run_dir)
        self.output.write_text("version two\n", encoding="utf-8")
        approval = self.write_approval(run_dir, operation)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", approval)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("differs from the proposal",
                      json.loads(proc.stdout)["error"]["message"])
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")

        # removed between propose and execute
        run_dir, _, candidate = self.selected_run()
        self.output.write_text("gone soon\n", encoding="utf-8")
        self.parse(self._propose_tree(run_dir, candidate, overwrite=True))
        operation = self.read_operation(run_dir)
        approval = self.write_approval(run_dir, operation)
        os.unlink(self.output)
        proc = self.run_cli("execute", "--run-dir", run_dir,
                            "--expect-revision", "3",
                            "--operation", operation["id"],
                            "--approval-file", approval)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("differs from the proposal",
                      json.loads(proc.stdout)["error"]["message"])
        self.assertEqual(self.read_operation(run_dir)["state"], "proposed")

    def test_execute_records_observed_output_target_after_run(self):
        run_dir, operation = self.propose_and_select("tree")
        approval = self.write_approval(run_dir, operation)
        doc = self.parse(self.run_cli("execute", "--run-dir", run_dir,
                                      "--expect-revision", "3",
                                      "--operation", operation["id"],
                                      "--approval-file", approval))
        self.assertEqual(doc["outcome"], "succeeded")
        report = self.parse(self.run_cli("report", "--run-dir", run_dir))
        evidence = [e for e in report["evidence"]
                    if e["id"] == doc["evidence_id"]][0]
        target = evidence["payload"]["output_target"]
        self.assertEqual(target["prestate"]["state"], "absent")
        observed = target["observed_after"]
        # creation is an observed fact with its own digest, never plugin speculation
        self.assertEqual(observed["state"], "present")
        self.assertEqual(len(observed["sha256"]), 64)
        self.assertEqual(self.output.read_text(encoding="utf-8"), TREE_JSON)


class AdvisoryInProcessTests(OperationsCliTestCase):
    """Mocked advisory happy path, no production seam: cli.main runs
    in-process with the real RunStore and execute_advisory durable flow, and
    only the default transport class is patched to an in-memory fake whose
    .post answers directly. Nothing opens a socket."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.scripts = cls.skill / "scripts"
        cls._saved_modules = {name: module for name, module in sys.modules.items()
                              if name == "mdi" or name.startswith("mdi.")}
        for name in cls._saved_modules:
            del sys.modules[name]
        sys.path.insert(0, str(cls.scripts))
        import mdi.advisory_executor
        import mdi.cli
        import mdi.runstore
        cls.advisory_executor = mdi.advisory_executor
        cls.cli = mdi.cli
        cls.runstore = mdi.runstore

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.scripts))
        for name in [n for n in sys.modules if n == "mdi" or n.startswith("mdi.")]:
            del sys.modules[name]
        sys.modules.update(cls._saved_modules)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.calls = []

    def run_main(self, *args):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.cli.main([str(a) for a in args])
        return code, json.loads(buffer.getvalue())

    def make_advisory_run(self):
        code, doc = self.run_main("discover", "--repo", self.project,
                                  "--query", "org.example:b-lib:1.0.0",
                                  "--state-dir", self.state)
        self.assertEqual(code, 0)
        candidate = doc["candidates"][0]["id"]
        self.run_main("select", "--run-dir", doc["run_dir"],
                      "--expect-revision", "1", "--candidate", candidate)
        code, proposed = self.run_main("propose", "--run-dir", doc["run_dir"],
                                       "--expect-revision", "2",
                                       "--kind", "advisory",
                                       "--candidate", candidate)
        self.assertEqual(code, 0)
        return doc["run_dir"], proposed["operation"]

    def _fake_transport(self, run_dir, operation_id, page):
        """Stand-in class patched over DeadlineTransport: one .post answering
        from memory, recording the durable operation state at call time."""
        test = self

        class FakeTransport:
            def __init__(self, opener=None):
                pass

            def post(self, destination, body, timeout_seconds, response_cap_bytes):
                store = test.runstore.RunStore.open(run_dir)
                record = next(op for op in store.load()["operations"]
                              if op["id"] == operation_id)
                test.calls.append({
                    "state_at_post": record["state"],
                    "destination": destination,
                    "body": json.loads(body.decode("utf-8")),
                    "timeout_seconds": timeout_seconds,
                    "response_cap_bytes": response_cap_bytes,
                })
                return json.dumps(page).encode("utf-8")
        return FakeTransport

    def test_advisory_execute_maps_evidence_and_consumes_before_post(self):
        run_dir, operation = self.make_advisory_run()
        approval = self.write_approval(run_dir, operation)
        with mock.patch.object(self.advisory_executor, "DeadlineTransport",
                               self._fake_transport(run_dir, operation["id"],
                                                    {"vulns": []})):
            code, executed = self.run_main(
                "execute", "--run-dir", run_dir, "--expect-revision", "3",
                "--operation", operation["id"], "--approval-file", approval)
        self.assertEqual(code, 0)
        self.assertEqual(executed["outcome"], "succeeded")
        self.assertIsNone(executed["next_page_token"])
        # one page, no retry, and consent was already spent when .post ran
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["state_at_post"], "consumed")
        self.assertEqual(self.calls[0]["destination"],
                         operation["scope"]["destination"])
        self.assertEqual(self.calls[0]["body"], {
            "package": {"name": "org.example:b-lib", "ecosystem": "Maven"},
            "version": "1.0.0",
        })
        code, report = self.run_main("report", "--run-dir", run_dir)
        self.assertEqual(code, 0)
        self.assertEqual(report["operations"][0]["state"], "finished")
        evidence = [e for e in report["evidence"]
                    if e["id"] == executed["evidence_id"]][0]
        self.assertEqual(evidence["category"], "advisory-page")
        payload = evidence["payload"]
        self.assertEqual(payload["result"]["status"], "no-known-matches")
        self.assertFalse(payload["result"]["more_pages"])
        self.assertEqual(payload["request"]["url"],
                         operation["scope"]["destination"])
        self.assertEqual(payload["request"]["body"], self.calls[0]["body"])
        self.assertTrue(payload["response"]["sha256"])
        # the emitted document mirrors the mapped result exactly
        self.assertEqual(executed["advisory_result"], payload["result"])

    def test_advisory_continuation_page_needs_fresh_approval(self):
        run_dir, operation = self.make_advisory_run()
        approval = self.write_approval(run_dir, operation)
        with mock.patch.object(self.advisory_executor, "DeadlineTransport",
                               self._fake_transport(run_dir, operation["id"],
                                                    {"vulns": [],
                                                     "next_page_token": "page-two"})):
            code, first = self.run_main(
                "execute", "--run-dir", run_dir, "--expect-revision", "3",
                "--operation", operation["id"], "--approval-file", approval)
        self.assertEqual(code, 0)
        self.assertEqual(first["outcome"], "succeeded")
        self.assertEqual(first["next_page_token"], "page-two")
        self.assertEqual(first["advisory_result"]["status"], "partial")
        # the first page's approval is bound to its own operation and cannot
        # authorize the continuation
        code, second = self.run_main(
            "propose", "--run-dir", run_dir, "--expect-revision", "6",
            "--kind", "advisory",
            "--candidate", self._selected_candidate_id(run_dir),
            "--page-token", "page-two")
        self.assertEqual(code, 0)
        continuation = second["operation"]
        self.assertNotEqual(continuation["id"], operation["id"])
        replay = self.run_cli("execute", "--run-dir", run_dir,
                              "--expect-revision", "7",
                              "--operation", continuation["id"],
                              "--approval-file", approval)
        self.assertEqual(replay.returncode, 2)
        self.assertIn("bound to a different operation",
                      json.loads(replay.stdout)["error"]["message"])
        fresh = self.write_approval(run_dir, continuation)
        with mock.patch.object(self.advisory_executor, "DeadlineTransport",
                               self._fake_transport(run_dir, continuation["id"],
                                                    {"vulns": []})):
            code, done = self.run_main(
                "execute", "--run-dir", run_dir, "--expect-revision", "7",
                "--operation", continuation["id"], "--approval-file", fresh)
        self.assertEqual(code, 0)
        self.assertEqual(done["outcome"], "succeeded")
        self.assertTrue(done["advisory_result"]["is_continuation"])
        self.assertEqual(self.calls[-1]["body"]["page_token"], "page-two")
        code, report = self.run_main("report", "--run-dir", run_dir)
        states = [op["state"] for op in report["operations"]]
        self.assertEqual(states, ["finished", "finished"])

    def _selected_candidate_id(self, run_dir):
        code, report = self.run_main("report", "--run-dir", run_dir)
        return report["selection"]["candidate_ids"][0]


if __name__ == "__main__":
    unittest.main()
