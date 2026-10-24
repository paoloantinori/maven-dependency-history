"""End-to-end subprocess tests for the offline investigate.py CLI (TASK-23).

Every test copies the bundled skill into a private temporary root and
invokes ``scripts/investigate.py`` as a real subprocess, so the tests cover
the shipped entry point, exit codes and the one-JSON-document contract, not
in-process function calls. Fixtures are generated in the temporary root: a
small Maven project whose fat JAR carries dependency C only as a packaged
transitive candidate, a recorded dependency-plugin 3.11.0 tree JSON, and a
synthetic Git history for the collect verb. No Maven, no network, no
repository commits, no hardcoded temporary paths; nothing outside the
temporary root is touched (the default XDG location is exercised through
XDG_STATE_HOME overrides only).
"""

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
from tests import child_env

REPO = Path(__file__).resolve().parents[1]
SKILL_SRC = REPO / "skills" / "maven-dependency-investigator"

NS = "http://maven.apache.org/POM/4.0.0"
_TIMEOUT = 300


def _pom(deps):
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
        "  <dependencies>\n" + rows + "\n  </dependencies>\n"
        "</project>\n"
    )


def _jar_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_project(root, deps=(("org.example", "b-lib", "1.0.0"),)):
    """One-module Maven project plus a Boot-style fat JAR in target/.

    The fat JAR nests c-lib 2.0.0, which is declared nowhere: dependency C
    is only ever a packaged transitive candidate.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(_pom(deps), encoding="utf-8")
    target = root / "target"
    target.mkdir(exist_ok=True)
    c_lib = _jar_bytes({
        "META-INF/maven/org.example/c-lib/pom.properties":
            "groupId=org.example\nartifactId=c-lib\nversion=2.0.0\n",
    })
    with zipfile.ZipFile(target / "app.jar", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BOOT-INF/lib/c-lib-2.0.jar", c_lib)
    # an ordinary dependency jar for usage-prerequisite arguments
    with zipfile.ZipFile(target / "dep.jar", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README", "placeholder\n")
    return root


def git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=30,
                       env=child_env())
    except OSError:
        return False
    return True


def make_git_project(root, deps=(("org.example", "b-lib", "1.0.0"),)):
    root = make_project(root, deps)
    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True, timeout=60, env=child_env())
    git("init")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    git("add", "-A")
    git("commit", "-m", "fixture project")
    return root


TREE_JSON = {
    "groupId": "org.example", "artifactId": "app", "version": "1.0.0",
    "type": "jar", "scope": "compile", "classifier": "", "optional": False,
    "children": [{
        "groupId": "org.example", "artifactId": "b-lib", "version": "1.0.0",
        "type": "jar", "scope": "compile", "classifier": "", "optional": False,
        "children": [],
    }],
}


class CliTestCase(unittest.TestCase):
    """Shared copied-skill harness; every invocation is a real subprocess."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mdi-cli-tests-"))
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

    def run_cli(self, *args, xdg_state_home=None, cwd=None):
        env = dict(os.environ)
        env["XDG_STATE_HOME"] = xdg_state_home or str(self.state)
        return subprocess.run(self.inv + [str(a) for a in args],
                              capture_output=True, text=True, timeout=_TIMEOUT,
                              env=env, cwd=str(cwd) if cwd else None)

    def parse(self, proc):
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        return json.loads(proc.stdout)

    def discover(self, repo, query, module=None, artifact=None, state_dir=None):
        args = ["discover", "--repo", repo, "--query", query,
                "--state-dir", state_dir or self.state]
        if module:
            args += ["--module", module]
        if artifact:
            args += ["--artifact", artifact]
        return self.parse(self.run_cli(*args))

    def selected_run(self, repo, query, candidate_index=0):
        """discover + select; returns (doc, run_dir, candidate_id)."""
        doc = self.discover(repo, query)
        self.assertFalse(doc["no_match"], doc)
        candidate_id = doc["candidates"][candidate_index]["id"]
        proc = self.run_cli("select", "--run-dir", doc["run_dir"],
                            "--expect-revision", doc["revision"],
                            "--candidate", candidate_id)
        self.parse(proc)
        return doc, doc["run_dir"], candidate_id


class DiscoverSelectTests(CliTestCase):

    def test_discover_finds_archive_only_transitive_candidate(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "c-lib")
        self.assertTrue(doc["ok"])
        self.assertFalse(doc["no_match"])
        packaged = [c for c in doc["candidates"] if c["kind"] == "packaged-copy"]
        self.assertEqual(len(packaged), 1)
        self.assertEqual(packaged[0]["coordinates"], "org.example:c-lib:2.0.0")
        self.assertEqual(doc["stage"]["outcome"], "partial")  # not a git checkout
        # the report maps the copy honestly, with an archive digest
        report = self.parse(self.run_cli("report", "--run-dir", doc["run_dir"],
                                         "--validate"))
        self.assertTrue(report["validation"]["valid"])
        copies = [s for s in report["report"]["subjects"]
                  if s.get("packaged_copy")]
        self.assertEqual(len(copies), 1)
        self.assertTrue(copies[0]["packaged_copy"]["archive_digest"])
        self.assertEqual(copies[0]["realm"], "unknown")
        self.assertIn("BOOT-INF", copies[0]["packaged_copy"]["entry_path"])

    def test_discover_qualified_declaration_then_select_and_validate(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "org.example:b-lib:1.0.0")
        self.assertEqual(len(doc["candidates"]), 1)
        self.assertFalse(doc["selection_required"])
        proc = self.run_cli("select", "--run-dir", doc["run_dir"],
                            "--expect-revision", doc["revision"],
                            "--candidate", doc["candidates"][0]["id"])
        selected = self.parse(proc)
        self.assertEqual(selected["selection"]["mode"], "single")
        report = self.parse(self.run_cli("report", "--run-dir", doc["run_dir"]))
        self.assertEqual(report["revision"], 2)
        self.assertEqual(report["selection"],
                         {"candidate_ids": [doc["candidates"][0]["id"]],
                          "mode": "single"})

    def test_select_all_covers_every_query_match(self):
        project = make_project(
            self.work / "proj",
            deps=(("org.example", "b-lib", "1.0.0"),
                  ("org.example", "d-lib", "3.0.0")))
        doc = self.discover(project, "lib")
        # b-lib and d-lib declarations plus the packaged c-lib copy whose
        # pom.properties artifactId contains the bare query text
        self.assertEqual(len(doc["candidates"]), 3)
        selected = self.parse(self.run_cli(
            "select", "--run-dir", doc["run_dir"],
            "--expect-revision", doc["revision"], "--all"))
        self.assertEqual(selected["selection"]["mode"], "all")
        self.assertEqual(len(selected["selection"]["candidate_ids"]), 3)

    def test_select_unknown_candidate_is_invalid(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "org.example:b-lib:1.0.0")
        proc = self.run_cli("select", "--run-dir", doc["run_dir"],
                            "--expect-revision", doc["revision"],
                            "--candidate", "mdi-does-not-exist")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not one of the query-matching candidates",
                      json.loads(proc.stdout)["error"]["message"])

    def test_selection_is_immutable(self):
        project = make_project(
            self.work / "proj",
            deps=(("org.example", "b-lib", "1.0.0"),
                  ("org.example", "d-lib", "3.0.0")))
        doc = self.discover(project, "lib")
        first = self.run_cli("select", "--run-dir", doc["run_dir"],
                             "--expect-revision", doc["revision"], "--all")
        self.assertEqual(first.returncode, 0, first.stdout)
        second = self.run_cli("select", "--run-dir", doc["run_dir"],
                              "--expect-revision", doc["revision"] + 1, "--all")
        self.assertEqual(second.returncode, 2)
        self.assertIn("immutable", json.loads(second.stdout)["error"]["message"])

    def test_stale_expected_revision_is_conflict(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "org.example:b-lib:1.0.0")
        proc = self.run_cli("select", "--run-dir", doc["run_dir"],
                            "--expect-revision", doc["revision"] - 1,
                            "--all")
        self.assertEqual(proc.returncode, 3)
        body = json.loads(proc.stdout)
        self.assertEqual(body["error"]["exit_code"], 3)
        self.assertIn("stale expected revision", body["error"]["message"])

    def test_no_match_is_explicit_valid_result(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "totally-absent-artifact")
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["no_match"])
        self.assertEqual(doc["candidates"], [])
        proc = self.run_cli("select", "--run-dir", doc["run_dir"],
                            "--expect-revision", doc["revision"], "--all")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no query-matching candidates",
                      json.loads(proc.stdout)["error"]["message"])


class CollectTests(CliTestCase):

    def test_collect_without_selection_is_invalid(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "org.example:b-lib:1.0.0")
        proc = self.run_cli("collect", "--run-dir", doc["run_dir"],
                            "--expect-revision", doc["revision"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("requires a recorded selection",
                      json.loads(proc.stdout)["error"]["message"])

    def test_collect_usage_unavailable_without_prerequisites(self):
        project = make_project(self.work / "proj")
        _, run_dir, _ = self.selected_run(project, "org.example:b-lib:1.0.0")
        doc = self.parse(self.run_cli("collect", "--run-dir", run_dir,
                                      "--expect-revision", "2"))
        self.assertEqual(doc["usage_outcome"], "unavailable")
        report = self.parse(self.run_cli("report", "--run-dir", run_dir))
        usage = [s for s in report["stages"] if s["name"] == "usage"][0]
        self.assertEqual(usage["outcome"], "unavailable")
        self.assertTrue(usage["reasons"])
        self.assertFalse([e for e in report["evidence"]
                          if e["category"] == "class-usage"])

    def test_collect_usage_unavailable_when_helper_hash_fails(self):
        project = make_project(self.work / "proj")
        _, run_dir, _ = self.selected_run(project, "org.example:b-lib:1.0.0")
        vendor = self.work / "tampered-vendor"
        shutil.copytree(self.skill / "vendor", vendor)
        (vendor / "helper.jar").write_bytes(b"tampered bytes")
        doc = self.parse(self.run_cli(
            "collect", "--run-dir", run_dir, "--expect-revision", "2",
            "--usage-root", project / "target" / "app.jar",
            "--usage-dependency", project / "target" / "dep.jar",
            "--vendor-dir", vendor))
        self.assertEqual(doc["usage_outcome"], "unavailable")
        report = self.parse(self.run_cli("report", "--run-dir", run_dir))
        usage = [s for s in report["stages"] if s["name"] == "usage"][0]
        self.assertEqual(usage["outcome"], "unavailable")
        self.assertIn("does not match the pinned", usage["reasons"][0])

    def test_collect_history_on_generated_git_project(self):
        if not git_available():
            self.skipTest("git is not available on PATH")
        project = make_git_project(self.work / "proj")
        _, run_dir, _ = self.selected_run(project, "org.example:b-lib:1.0.0")
        doc = self.parse(self.run_cli("collect", "--run-dir", run_dir,
                                      "--expect-revision", "2"))
        self.assertEqual(doc["history_outcome"], "complete", doc)
        report = self.parse(self.run_cli("report", "--run-dir", run_dir,
                                         "--validate"))
        self.assertTrue(report["validation"]["valid"])
        history = [s for s in report["report"]["stages"]
                   if s["name"] == "history"][0]
        self.assertEqual(history["outcome"], "complete")
        evidence = [e for e in report["report"]["evidence"]
                    if e["category"] == "declaration-history"]
        self.assertEqual(len(evidence), 1)
        events = evidence[0]["payload"]["events"]
        self.assertEqual(events[0]["kind"], "addition")
        self.assertEqual(events[0]["matched_coordinates"]["artifact_id"], "b-lib")

    def test_collect_history_not_applicable_for_archive_only_selection(self):
        project = make_project(self.work / "proj")
        _, run_dir, _ = self.selected_run(project, "c-lib")
        doc = self.parse(self.run_cli("collect", "--run-dir", run_dir,
                                      "--expect-revision", "2"))
        self.assertEqual(doc["history_outcome"], "not_applicable")


class ImportTests(CliTestCase):

    def setUp(self):
        super().setUp()
        self.project = make_project(self.work / "proj")
        self.report_file = self.work / "tree.json"
        self.report_file.write_text(json.dumps(TREE_JSON), encoding="utf-8")
        run = self.discover(self.project, "org.example:b-lib:1.0.0")
        self.run_dir = run["run_dir"]
        self.revision = run["revision"]

    def test_import_without_provenance_labels_assumed_grammar(self):
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json"))
        self.assertEqual(doc["currency"], "unknown")
        self.assertEqual(doc["provenance_attestation"], "unknown")
        self.assertEqual(doc["assumed_grammar"],
                         {"producer": "maven-dependency-plugin",
                          "version": "3.11.0"})
        self.assertIsNone(doc["supplied_provenance"])
        report = self.parse(self.run_cli("report", "--run-dir", self.run_dir))
        evidence = [e for e in report["evidence"]
                    if e["category"] == "imported-tree-json"][0]
        # the assumed producer/version must never appear as naked observations
        self.assertNotIn("producer", evidence)
        self.assertNotIn("producer_version", evidence)
        context = evidence["generation_context"]
        self.assertEqual(context["attestation"], "unknown")
        self.assertEqual(context["assumed_grammar"],
                         {"producer": "maven-dependency-plugin",
                          "version": "3.11.0"})
        self.assertIsNone(context["supplied_provenance"])
        # the importer result sits under an explicitly labeled wrapper key
        self.assertEqual(evidence["payload"]["provenance_attestation"], "unknown")
        self.assertIn("import", evidence["payload"])
        self.assertEqual(evidence["payload"]["import"]["context"]["currency"],
                         "unknown")
        qualifications = [q for q in report["qualifications"]
                          if evidence["id"] in q["affected_evidence_ids"]]
        self.assertEqual(len(qualifications), 1)
        self.assertEqual(qualifications[0]["kind"], "assumed-producer-grammar")
        self.assertIn("remain unknown", qualifications[0]["reason"])
        stage = [s for s in report["stages"] if s["name"] == "import-tree-json"][0]
        self.assertEqual(stage["outcome"], "complete")
        self.assertIn("no current context was supplied",
                      stage["coverage"]["current_build_attribution"])

    def test_import_with_explicit_provenance_is_caller_attested(self):
        provenance = self.work / "prov.json"
        provenance.write_text(json.dumps({"revision": "deadbeef",
                                          "module": "app"}), encoding="utf-8")
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--provenance", provenance))
        self.assertEqual(doc["provenance_attestation"], "caller-attested")
        self.assertEqual(doc["supplied_provenance"],
                         {"revision": "deadbeef", "module": "app"})
        # producer/version were still not attested: only the caller's fields are
        self.assertEqual(doc["assumed_grammar"],
                         {"producer": "maven-dependency-plugin",
                          "version": "3.11.0"})
        report = self.parse(self.run_cli("report", "--run-dir", self.run_dir))
        evidence = [e for e in report["evidence"]
                    if e["category"] == "imported-tree-json"][0]
        self.assertNotIn("producer", evidence)
        self.assertEqual(evidence["generation_context"]["attestation"],
                         "caller-attested")
        self.assertEqual(evidence["generation_context"]["supplied_provenance"],
                         {"revision": "deadbeef", "module": "app"})
        qualifications = [q for q in report["qualifications"]
                          if evidence["id"] in q["affected_evidence_ids"]]
        self.assertEqual(qualifications[0]["kind"], "assumed-producer-grammar")
        self.assertIn("caller-attested", qualifications[0]["reason"])

    def test_import_with_attested_producer_records_observation(self):
        provenance = self.work / "full.json"
        provenance.write_text(json.dumps({
            "producer": "maven-dependency-plugin", "version": "3.11.0",
            "revision": "deadbeef"}), encoding="utf-8")
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--provenance", provenance))
        self.assertEqual(doc["provenance_attestation"], "caller-attested")
        self.assertIsNone(doc["assumed_grammar"])
        report = self.parse(self.run_cli("report", "--run-dir", self.run_dir))
        evidence = [e for e in report["evidence"]
                    if e["category"] == "imported-tree-json"][0]
        self.assertEqual(evidence["producer"], "maven-dependency-plugin")
        self.assertEqual(evidence["producer_version"], "3.11.0")
        qualifications = [q for q in report["qualifications"]
                          if evidence["id"] in q["affected_evidence_ids"]]
        self.assertEqual(qualifications[0]["kind"], "caller-attested-provenance")
        self.assertIn("not verified", qualifications[0]["reason"])

    def test_import_rejects_foreign_producer(self):
        provenance = self.work / "bad.json"
        provenance.write_text(json.dumps({"producer": "someone-elses-tool"}),
                              encoding="utf-8")
        proc = self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--provenance", provenance)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unsupported-producer",
                      json.loads(proc.stdout)["error"]["message"])

    def test_import_rejects_unknown_provenance_fields(self):
        provenance = self.work / "fields.json"
        provenance.write_text(json.dumps({"guessed": True}), encoding="utf-8")
        proc = self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--provenance", provenance)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown fields", json.loads(proc.stdout)["error"]["message"])

    def test_import_invalid_inputs(self):
        missing = self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.work / "absent.json", "--kind", "tree-json")
        self.assertEqual(missing.returncode, 2)
        malformed = self.work / "bad.json"
        malformed.write_text("{not json", encoding="utf-8")
        proc = self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", malformed, "--kind", "tree-json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("malformed-json",
                      json.loads(proc.stdout)["error"]["message"])

    def test_import_effective_pom_kind(self):
        pom_file = self.work / "effective.xml"
        pom_file.write_text(_pom((("org.example", "b-lib", "1.0.0"),)),
                            encoding="utf-8")
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", pom_file, "--kind", "effective-pom"))
        self.assertEqual(doc["assumed_grammar"]["producer"], "maven-help-plugin")
        self.assertEqual(doc["assumed_grammar"]["version"], "3.5.2")

    def test_import_stale_revision_is_conflict(self):
        proc = self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision + 5,
            "--report", self.report_file, "--kind", "tree-json")
        self.assertEqual(proc.returncode, 3)

    def test_import_tree_defaults_to_diagnostic_verbose(self):
        # absence of an attestation never selects the authoritative
        # non-verbose mode: the default is diagnostic verbose
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json"))
        self.assertEqual(doc["tree_mode"], "verbose")
        self.assertEqual(doc["tree_mode_attestation"], "default")
        report = self.parse(self.run_cli("report", "--run-dir", self.run_dir))
        evidence = [e for e in report["evidence"]
                    if e["category"] == "imported-tree-json"][-1]
        self.assertEqual(evidence["payload"]["import"]["mode"], "verbose")
        self.assertEqual(evidence["payload"]["import"]["inclusion_authority"],
                         "diagnostic-only")

    def test_import_tree_nonverbose_needs_explicit_attestation(self):
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--tree-mode", "nonverbose"))
        self.assertEqual(doc["tree_mode"], "nonverbose")
        self.assertEqual(doc["tree_mode_attestation"], "flag")
        report = self.parse(self.run_cli("report", "--run-dir", self.run_dir))
        evidence = [e for e in report["evidence"]
                    if e["category"] == "imported-tree-json"][-1]
        self.assertEqual(evidence["payload"]["import"]["mode"], "nonverbose")
        self.assertEqual(evidence["payload"]["import"]["inclusion_authority"],
                         "recorded-context")

    def test_import_tree_mode_attested_via_provenance_format(self):
        provenance = self.work / "format.json"
        provenance.write_text(json.dumps({"format": "nonverbose"}),
                              encoding="utf-8")
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--provenance", provenance))
        self.assertEqual(doc["tree_mode"], "nonverbose")
        self.assertEqual(doc["tree_mode_attestation"], "provenance")
        unrecognized = self.work / "weird.json"
        unrecognized.write_text(json.dumps({"format": "full-tree"}), encoding="utf-8")
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", doc["revision"],
            "--report", self.report_file, "--kind", "tree-json",
            "--provenance", unrecognized))
        self.assertEqual(doc["tree_mode"], "verbose")
        self.assertEqual(doc["tree_mode_attestation"], "default")

    def test_import_tree_mode_rejected_for_other_kinds(self):
        proc = self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--tree-mode", "nonverbose")
        self.assertEqual(proc.returncode, 0)
        pom_file = self.work / "effective.xml"
        pom_file.write_text(_pom((("org.example", "b-lib", "1.0.0"),))),
        proc = self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.parse(self.run_cli(
                "report", "--run-dir", self.run_dir))["revision"],
            "--report", pom_file, "--kind", "effective-pom",
            "--tree-mode", "nonverbose")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("tree-json only", json.loads(proc.stdout)["error"]["message"])

    def test_import_tree_mode_contradicting_provenance_is_refused(self):
        provenance = self.work / "verbose-attested.json"
        provenance.write_text(json.dumps({"format": "verbose"}), encoding="utf-8")
        proc = self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--tree-mode", "nonverbose", "--provenance", provenance)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("contradicts", json.loads(proc.stdout)["error"]["message"])
        # an agreeing attestation is fine and records the flag explicitly
        agreeing = self.work / "nonverbose-attested.json"
        agreeing.write_text(json.dumps({"format": "nonverbose"}), encoding="utf-8")
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--tree-mode", "nonverbose", "--provenance", agreeing))
        self.assertEqual(doc["tree_mode"], "nonverbose")
        self.assertEqual(doc["tree_mode_attestation"], "flag")

    def test_import_null_producer_version_stays_unknown(self):
        # explicit nulls behave exactly like omitted keys: the pinned grammar
        # parses, but the actual producer/version stay unknown and assumed
        provenance = self.work / "nulls.json"
        provenance.write_text(json.dumps({"producer": None, "version": None,
                                          "revision": "r1"}), encoding="utf-8")
        doc = self.parse(self.run_cli(
            "import", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--report", self.report_file, "--kind", "tree-json",
            "--provenance", provenance))
        self.assertEqual(doc["provenance_attestation"], "caller-attested")
        self.assertEqual(doc["assumed_grammar"],
                         {"producer": "maven-dependency-plugin",
                          "version": "3.11.0"})
        self.assertEqual(doc["supplied_provenance"]["producer"], None)
        report = self.parse(self.run_cli("report", "--run-dir", self.run_dir))
        evidence = [e for e in report["evidence"]
                    if e["category"] == "imported-tree-json"][-1]
        self.assertNotIn("producer", evidence)
        self.assertNotIn("producer_version", evidence)
        qualifications = [q for q in report["qualifications"]
                          if evidence["id"] in q["affected_evidence_ids"]]
        self.assertEqual(qualifications[-1]["kind"], "assumed-producer-grammar")


class VendorAndHardeningTests(CliTestCase):

    def setUp(self):
        super().setUp()
        self.project = make_project(self.work / "proj")
        _, self.run_dir, _ = self.selected_run(self.project,
                                               "org.example:b-lib:1.0.0")
        self.revision = 2

    def _write_sums(self, vendor, lines):
        (vendor / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _vendor_copy(self, name):
        vendor = self.work / name
        shutil.copytree(self.skill / "vendor", vendor)
        return vendor

    def _latest_revision(self, run_dir):
        return self.parse(self.run_cli("report", "--run-dir", run_dir))["revision"]

    def test_vendor_manifest_rejects_traversal_and_foreign_spellings(self):
        vendor = self._vendor_copy("vendor-traversal")
        real = (vendor / "helper.jar").read_bytes()
        import hashlib
        digest = hashlib.sha256(real).hexdigest()
        for line in (f"{digest}  ../evil/helper.jar",
                     f"{digest}  /etc/passwd",
                     f"{digest}  lib\\\\..\\\\helper.jar",
                     f"{digest}  C:/helper.jar",
                     f"{digest}  //server/share/helper.jar"):
            self._write_sums(vendor, [
                "ba8cbf5a9c6be2782681fe4e018c08b6a20a3129a2b1c7df88cf3a7f6343e906  BUILD-INFO.json",
                line,
            ])
            doc = self.parse(self.run_cli(
                "collect", "--run-dir", self.run_dir,
                "--expect-revision", self._latest_revision(self.run_dir),
                "--usage-root", self.project / "target" / "app.jar",
                "--usage-dependency", self.project / "target" / "dep.jar",
                "--vendor-dir", vendor))
            self.assertEqual(doc["usage_outcome"], "unavailable", line)
            report = self.parse(self.run_cli("report", "--run-dir", self.run_dir))
            usage = [s for s in report["stages"] if s["name"] == "usage"][-1]
            self.assertIn("unparsable", usage["reasons"][0], line)

    def test_vendor_closure_crosschecked_against_dependency_manifest(self):
        vendor = self._vendor_copy("vendor-closure")
        # Fully co-forge the package: swap the gson jar, update BUILD-INFO.json's
        # outputs and re-digest BUILD-INFO.json into SHA256SUMS, so manifest and
        # build info stay mutually consistent. Only the DEPENDENCIES.json pin
        # cross-check (outside the overridable vendor tree) can refuse this.
        import hashlib
        (vendor / "lib" / "gson-2.14.0.jar").write_bytes(b"not gson at all")
        forged = hashlib.sha256(
            (vendor / "lib" / "gson-2.14.0.jar").read_bytes()).hexdigest()
        build_info = json.loads(
            (vendor / "BUILD-INFO.json").read_text(encoding="utf-8"))
        build_info["outputs"]["lib"]["lib/gson-2.14.0.jar"] = forged
        (vendor / "BUILD-INFO.json").write_text(
            json.dumps(build_info, indent=2, sort_keys=True), encoding="utf-8")
        build_digest = hashlib.sha256(
            (vendor / "BUILD-INFO.json").read_bytes()).hexdigest()
        lines = []
        sums = self.skill / "vendor" / "SHA256SUMS"
        for line in sums.read_text(encoding="utf-8").splitlines():
            if line.endswith("lib/gson-2.14.0.jar"):
                line = f"{forged}  lib/gson-2.14.0.jar"
            elif line.endswith("BUILD-INFO.json"):
                line = f"{build_digest}  BUILD-INFO.json"
            lines.append(line)
        self._write_sums(vendor, lines)
        doc = self.parse(self.run_cli(
            "collect", "--run-dir", self.run_dir,
            "--expect-revision", self.revision,
            "--usage-root", self.project / "target" / "app.jar",
            "--usage-dependency", self.project / "target" / "dep.jar",
            "--vendor-dir", vendor))
        self.assertEqual(doc["usage_outcome"], "unavailable")
        report = self.parse(self.run_cli("report", "--run-dir", self.run_dir))
        usage = [s for s in report["stages"] if s["name"] == "usage"][-1]
        self.assertIn("DEPENDENCIES.json", usage["reasons"][0])

    def test_discover_records_artifact_path_matching_probe(self):
        # relative --artifact spellings resolve against the process cwd: the
        # recorded context path and the probed copy path must be the same
        # normalized absolute path
        doc = self.parse(self.run_cli(
            "discover", "--repo", str(self.project), "--query", "c-lib",
            "--artifact", "proj/target/app.jar", "--state-dir", self.state,
            cwd=str(self.work)))
        self.assertFalse(doc["no_match"])
        report = self.parse(self.run_cli("report", "--run-dir", doc["run_dir"]))
        context = [e for e in report["evidence"]
                   if e["category"] == "discovery-context"][0]["payload"]
        match = [e for e in report["evidence"]
                 if e["category"] == "discovery-match"][0]
        expected = os.path.abspath(os.path.join(str(self.work),
                                                "proj/target/app.jar"))
        self.assertEqual(context["artifact"], expected)
        self.assertEqual(match["captured_identity"]["path"], context["artifact"])

    def test_invalid_stored_report_schema_is_exit_2(self):
        report_path = Path(self.run_dir) / "report-00000001.json"
        stored = json.loads(report_path.read_text(encoding="utf-8"))
        stored["schema_version"] = 99
        report_path.write_text(json.dumps(stored), encoding="utf-8")
        proc = self.run_cli("report", "--run-dir", self.run_dir)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertIn("schema_version",
                      json.loads(proc.stdout)["error"]["message"])

    def test_revision_arguments_are_strict_ascii_decimals(self):
        for bad in ("1_0", "0x2", "+3", " 4", "٤"):
            proc = self.run_cli("select", "--run-dir", self.run_dir,
                                "--expect-revision", bad, "--all")
            self.assertEqual(proc.returncode, 2, bad)
            self.assertIn("invalid invocation",
                          json.loads(proc.stdout)["error"]["message"], bad)
            proc = self.run_cli("report", "--run-dir", self.run_dir,
                                "--revision", bad)
            self.assertEqual(proc.returncode, 2, bad)

    def test_malformed_build_info_outputs_is_unavailable_not_a_crash(self):
        if not git_available():
            self.skipTest("git is not available on PATH")
        project = make_git_project(self.work / "gitproj")
        doc = self.discover(project, "org.example:b-lib:1.0.0")
        candidate = doc["candidates"][0]["id"]
        self.parse(self.run_cli("select", "--run-dir", doc["run_dir"],
                                "--expect-revision", "1",
                                "--candidate", candidate))
        vendor = self.work / "broken-vendor"
        shutil.copytree(self.skill / "vendor", vendor)
        (vendor / "BUILD-INFO.json").write_text(json.dumps(
            {"schema": "mdi.helper-build-info/1", "outputs": ["invalid"]}),
            encoding="utf-8")
        import hashlib
        digest = hashlib.sha256(
            (vendor / "BUILD-INFO.json").read_bytes()).hexdigest()
        lines = [f"{digest}  BUILD-INFO.json" if line.endswith("BUILD-INFO.json")
                 else line
                 for line in (self.skill / "vendor" / "SHA256SUMS")
                 .read_text(encoding="utf-8").splitlines()]
        (vendor / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.parse(self.run_cli(
            "collect", "--run-dir", doc["run_dir"], "--expect-revision", "2",
            "--usage-root", project / "target" / "app.jar",
            "--usage-dependency", project / "target" / "dep.jar",
            "--vendor-dir", vendor))
        self.assertEqual(result["usage_outcome"], "unavailable")
        self.assertEqual(result["history_outcome"], "complete")
        report = self.parse(self.run_cli("report", "--run-dir", doc["run_dir"]))
        usage = [s for s in report["stages"] if s["name"] == "usage"][-1]
        self.assertEqual(usage["outcome"], "unavailable")
        self.assertIn("BUILD-INFO.json outputs", usage["reasons"][0])
        history = [s for s in report["stages"] if s["name"] == "history"][-1]
        self.assertEqual(history["outcome"], "complete")


class ReportAndStateTests(CliTestCase):

    def test_report_historical_revision(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "org.example:b-lib:1.0.0")
        self.run_cli("select", "--run-dir", doc["run_dir"],
                     "--expect-revision", doc["revision"],
                     "--candidate", doc["candidates"][0]["id"])
        old = self.parse(self.run_cli("report", "--run-dir", doc["run_dir"],
                                      "--revision", "1"))
        self.assertEqual(old["revision"], 1)
        self.assertIsNone(old["selection"])
        latest = self.parse(self.run_cli("report", "--run-dir", doc["run_dir"]))
        self.assertEqual(latest["revision"], 2)
        validated = self.parse(self.run_cli("report", "--run-dir",
                                            doc["run_dir"], "--validate"))
        self.assertTrue(validated["ok"])
        self.assertTrue(validated["validation"]["valid"])
        self.assertEqual(validated["report"]["revision"], 2)

    def test_report_nonexistent_revision_is_invalid(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "org.example:b-lib:1.0.0")
        proc = self.run_cli("report", "--run-dir", doc["run_dir"],
                            "--revision", "9")
        self.assertEqual(proc.returncode, 2)

    def test_report_rejects_non_run_directory(self):
        proc = self.run_cli("report", "--run-dir", self.work)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot open run directory",
                      json.loads(proc.stdout)["error"]["message"])

    def test_default_run_location_is_xdg_state_home(self):
        xdg = self.work / "xdg-state"
        project = make_project(self.work / "proj")
        proc = self.run_cli("discover", "--repo", str(project),
                            "--query", "org.example:b-lib:1.0.0",
                            xdg_state_home=str(xdg))
        doc = self.parse(proc)
        expected_base = xdg / "maven-dependency-investigator" / "runs"
        self.assertTrue(doc["run_dir"].startswith(str(expected_base) + os.sep))
        mode = stat.S_IMODE(os.stat(doc["run_dir"]).st_mode)
        self.assertEqual(mode, 0o700)

    def test_state_dir_refuses_files_and_symlinks(self):
        project = make_project(self.work / "proj")
        occupied = self.work / "occupied"
        occupied.write_text("not a directory\n", encoding="utf-8")
        proc = self.run_cli("discover", "--repo", str(project),
                            "--query", "b-lib", "--state-dir", occupied)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a plain directory",
                      json.loads(proc.stdout)["error"]["message"])
        linked = self.work / "linked"
        linked.symlink_to(self.work / "elsewhere", target_is_directory=True)
        proc = self.run_cli("discover", "--repo", str(project),
                            "--query", "b-lib", "--state-dir", linked)
        self.assertEqual(proc.returncode, 2)

    def test_state_dir_never_converts_existing_directory_into_a_run(self):
        project = make_project(self.work / "proj")
        existing = self.work / "already-there"
        existing.mkdir()
        doc = self.discover(project, "org.example:b-lib:1.0.0",
                            state_dir=existing)
        run_children = sorted(p.name for p in existing.iterdir())
        self.assertEqual(run_children, [doc["run_id"]])
        self.assertEqual(doc["run_dir"], str(existing / doc["run_id"]))

    def test_state_dir_inside_checkout_rejected_before_any_creation(self):
        project = make_project(self.work / "proj")
        inside = project / "runs"
        proc = self.run_cli("discover", "--repo", str(project),
                            "--query", "b-lib", "--state-dir", inside)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("overlaps the checkout",
                      json.loads(proc.stdout)["error"]["message"])
        self.assertFalse(inside.exists())
        # equal spelling is rejected the same way
        proc = self.run_cli("discover", "--repo", str(project),
                            "--query", "b-lib", "--state-dir", project)
        self.assertEqual(proc.returncode, 2)

    def test_state_dir_symlink_alias_into_checkout_rejected(self):
        project = make_project(self.work / "proj")
        alias = self.work / "alias"
        alias.symlink_to(project, target_is_directory=True)
        aliased = alias / "nested-state"  # lexically outside, really inside
        proc = self.run_cli("discover", "--repo", str(project),
                            "--query", "b-lib", "--state-dir", aliased)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("overlaps the checkout",
                      json.loads(proc.stdout)["error"]["message"])
        self.assertFalse((project / "nested-state").exists())
        self.assertFalse((alias / "nested-state").exists())
        self.assertTrue(project.joinpath("target").is_dir())

    def test_invalid_invocations_emit_json_and_exit_2(self):
        cases = [
            ["discover", "--repo", str(self.work)],  # missing --query
            ["discover", "--repo", str(self.work), "--query", "a:b:c:d",
             "--state-dir", str(self.state)],  # malformed qualified query
            ["select", "--run-dir", str(self.work)],  # missing args
            ["collect", "--run-dir", str(self.work), "--expect-revision", "x"],
        ]
        for case in cases:
            proc = self.run_cli(*case)
            self.assertEqual(proc.returncode, 2, case)
            body = json.loads(proc.stdout)
            self.assertFalse(body["ok"])
            self.assertTrue(body["error"]["message"])

    def test_unexpected_run_store_corruption_is_reported_without_traceback(self):
        project = make_project(self.work / "proj")
        doc = self.discover(project, "org.example:b-lib:1.0.0")
        (Path(doc["run_dir"]) / "stranger.txt").write_text("hostile member\n",
                                                           encoding="utf-8")
        proc = self.run_cli("report", "--run-dir", doc["run_dir"])
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)


class UnavailableVerbTests(CliTestCase):

    def test_bare_propose_and_execute_are_invalid_invocations(self):
        for command in ("propose", "execute"):
            proc = self.run_cli(command)
            self.assertEqual(proc.returncode, 2, proc.stdout)
            body = json.loads(proc.stdout)
            self.assertFalse(body["ok"])
            self.assertIn("invalid invocation", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
