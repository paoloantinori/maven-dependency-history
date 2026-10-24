"""Offline end-to-end acceptance tests for the TASK-23 CLI pipeline.

One immutable run drives the real copied skill end to end: the tests copy
``skills/maven-dependency-investigator`` into a private temporary root and
invoke ``scripts/investigate.py`` as a real subprocess for every verb
(discover, select, collect, import, report), threading ``--expect-revision``
from each document to the next. The fixtures are generated locally: an
A(=application) -> B(=b-lib) -> C(=lib-c) class chain compiled with the real
``javac``, a Boot-style fat JAR whose nested jar carries C's coordinates,
a recorded dependency-plugin 3.11.0 tree JSON produced by the fixture itself
(clearly caller-attested synthetic provenance, never a real Maven run), and
a local temporary Git history whose b-lib upgrade commit feeds the actual
history collector. The class-usage collect stage runs the real bundled
helper with the real ``java`` runtime over the copied skill's own default
vendor tree, verified by the CLI's SHA256SUMS and closure checks; the tests
set no MDI_TEST_DEPENDENCY_DIR because they never import
tests/java_support.py (the vendored jars are the helper's own verified
runtime closure, not test compile inputs).

Everything is strictly offline: no Maven, no network, no downloads and no
investigation-time bootstrap (the whole copied skill tree is digested before
and after the runs and must be byte-identical). The Git history assertions
cover the upgrade candidate only: the recorded declaration events are
candidate leads, never a resolved or shipped fact, and the tests assert that
no resolved transitive claim about C is invented anywhere.

Java, javac, jar and git are mandatory prerequisites: a missing tool raises
an actionable AssertionError naming the missing binary, never a skip.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from tests import child_env

REPO = Path(__file__).resolve().parents[1]
SKILL_SRC = REPO / "skills" / "maven-dependency-investigator"

_NS = "http://maven.apache.org/POM/4.0.0"
_TIMEOUT = 300
_JAVAC_RELEASE = "17"

_APP_SRC = """package com.example.app;
public class App {
    public String run() { return new com.example.b.B().label(); }
}
"""
_B_SRC = """package com.example.b;
public class B {
    public String label() { return new com.example.c.C().name(); }
}
"""
_C_SRC = """package com.example.c;
public class C {
    public String name() { return "c"; }
}
"""

# The recorded tree the fixture attests: C enters only as a child of B.
TREE_JSON = {
    "groupId": "org.example", "artifactId": "app", "version": "1.0.0",
    "type": "jar", "scope": "compile", "classifier": "", "optional": False,
    "children": [{
        "groupId": "org.example", "artifactId": "b-lib", "version": "2.0.0",
        "type": "jar", "scope": "compile", "classifier": "", "optional": False,
        "children": [{
            "groupId": "org.example", "artifactId": "lib-c", "version": "1.0.0",
            "type": "jar", "scope": "compile", "classifier": "", "optional": False,
            "children": [],
        }],
    }],
}


def _require_tool(name):
    found = shutil.which(name)
    if not found:
        raise AssertionError(
            f"BLOCKER: {name} is not available on PATH; these end-to-end "
            f"tests require a real JDK ({name}) and never skip on a missing "
            f"Java toolchain")
    return found


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jar_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _pom(dep_version):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<project xmlns="{_NS}">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>org.example</groupId>\n"
        "  <artifactId>app</artifactId>\n"
        "  <version>1.0.0</version>\n"
        "  <dependencies>\n"
        "    <dependency>\n"
        "      <groupId>org.example</groupId>\n"
        "      <artifactId>b-lib</artifactId>\n"
        f"      <version>{dep_version}</version>\n"
        "    </dependency>\n"
        "  </dependencies>\n"
        "</project>\n"
    )


def _compile_fixtures(work):
    """Compile A -> B -> C with the real javac and package the jars."""
    src = work / "src"
    for package in ("app", "b", "c"):
        (src / "com" / "example" / package).mkdir(parents=True)
    (src / "com/example/app/App.java").write_text(_APP_SRC, encoding="utf-8")
    (src / "com/example/b/B.java").write_text(_B_SRC, encoding="utf-8")
    (src / "com/example/c/C.java").write_text(_C_SRC, encoding="utf-8")
    classes = work / "classes"
    proc = subprocess.run(
        ["javac", "--release", _JAVAC_RELEASE, "-d", str(classes)]
        + sorted(str(p) for p in src.rglob("*.java")),
        capture_output=True, text=True, timeout=_TIMEOUT, env=child_env())
    if proc.returncode != 0:
        raise AssertionError(f"BLOCKER: fixture javac failed:\n{proc.stderr}")

    def jar_of(name, package, klass):
        stage = work / f"stage-{package}"
        target = stage / "com" / "example" / package
        target.mkdir(parents=True)
        shutil.copyfile(classes / "com" / "example" / package / f"{klass}.class",
                        target / f"{klass}.class")
        proc = subprocess.run(["jar", "--create", "--file", str(work / name),
                               "-C", str(stage), "."],
                              capture_output=True, text=True, timeout=_TIMEOUT,
                              env=child_env())
        if proc.returncode != 0:
            raise AssertionError(f"BLOCKER: fixture jar {name} failed:\n{proc.stderr}")

    jar_of("root.jar", "app", "App")
    jar_of("lib-b.jar", "b", "B")
    jar_of("lib-c.jar", "c", "C")
    # nested jar with real Maven pom.properties coordinates for C
    meta = work / "pc" / "META-INF" / "maven" / "org.example" / "lib-c"
    meta.mkdir(parents=True)
    (meta / "pom.properties").write_text(
        "groupId=org.example\nartifactId=lib-c\nversion=1.0.0\n", encoding="utf-8")
    proc = subprocess.run(
        ["jar", "--create", "--file", str(work / "lib-c-1.0.0.jar"),
         "-C", str(work / "stage-c"), ".", "-C", str(work / "pc"), "META-INF"],
        capture_output=True, text=True, timeout=_TIMEOUT, env=child_env())
    if proc.returncode != 0:
        raise AssertionError(f"BLOCKER: nested fixture jar failed:\n{proc.stderr}")


def _git_env(home):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Fixture Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.invalid",
    })
    return env


def _git(root, env, *args, date):
    stamp = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    done = subprocess.run(["git", "-C", str(root), *args], env={**env, **stamp},
                          capture_output=True, text=True, timeout=_TIMEOUT)
    if done.returncode != 0:
        raise AssertionError(
            f"BLOCKER: fixture git {args[0]} failed:\n{done.stderr}")


def make_project(root, env, fixtures):
    """Git project: POM declares only b-lib; history holds the 1.0.0 ->
    2.0.0 upgrade; the committed Boot-style fat JAR nests C's coordinates
    and the build/ jars feed the usage collect. C is declared nowhere."""
    root.mkdir(parents=True)
    (root / "pom.xml").write_text(_pom("1.0.0"), encoding="utf-8")
    _git(root, env, "init", "-q", date="2024-01-01T00:00:00+00:00")
    _git(root, env, "add", "-A", date="2024-01-01T00:00:00+00:00")
    _git(root, env, "commit", "-q", "-m", "declare b-lib 1.0.0",
         date="2024-01-01T00:00:00+00:00")
    (root / "pom.xml").write_text(_pom("2.0.0"), encoding="utf-8")
    _git(root, env, "commit", "-qam", "upgrade b-lib to 2.0.0",
         date="2024-02-01T00:00:00+00:00")
    fix = fixtures
    (root / "target").mkdir()
    (root / "target" / "app.jar").write_bytes(_jar_bytes({
        "BOOT-INF/lib/lib-c-1.0.0.jar": (fix / "lib-c-1.0.0.jar").read_bytes(),
    }))
    (root / "build").mkdir()
    for name in ("root.jar", "lib-b.jar", "lib-c.jar"):
        shutil.copyfile(fix / name, root / "build" / name)
    _git(root, env, "add", "-A", date="2024-03-01T00:00:00+00:00")
    _git(root, env, "commit", "-q", "-m", "ship boot fat jar and build jars",
         date="2024-03-01T00:00:00+00:00")
    return root


class EndToEndTestCase(unittest.TestCase):
    """Copied-skill subprocess harness shared by every acceptance test."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mdi-e2e-tests-"))
        cls.skill = cls.tmp / "skill"
        shutil.copytree(SKILL_SRC, cls.skill,
                        ignore=shutil.ignore_patterns("__pycache__"))
        cls.inv = [sys.executable, str(cls.skill / "scripts" / "investigate.py")]
        _require_tool("java")
        _require_tool("javac")
        _require_tool("jar")
        _require_tool("git")
        cls.fix = cls.tmp / "fixtures"
        cls.fix.mkdir()
        _compile_fixtures(cls.fix)
        cls.home = cls.tmp / "home"
        cls.home.mkdir()
        cls.git_env = _git_env(cls.home)
        cls.project = make_project(cls.tmp / "project", cls.git_env, cls.fix)
        # Tamper-evidence across the WHOLE module: the copied skill tree must
        # come out byte-identical, proving no investigation-time compilation,
        # bootstrap or download ever touched it.
        cls.skill_digests = cls._skill_tree_digests()

    @classmethod
    def tearDownClass(cls):
        # before the temp root goes: the whole copied skill tree must come
        # out byte-identical, proving no investigation-time compilation,
        # bootstrap or download ever touched it
        cls._assert_skill_tree_unchanged()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _skill_tree_digests(cls):
        return {str(p.relative_to(cls.skill)): _sha256_file(p)
                for p in sorted(cls.skill.rglob("*")) if p.is_file()}

    @classmethod
    def _assert_skill_tree_unchanged(cls):
        if cls._skill_tree_digests() != cls.skill_digests:
            raise AssertionError(
                "the copied skill tree changed during the test run: the CLI "
                "must never compile, bootstrap or download at investigation "
                "time")

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.tmp))
        self.state = self.work / "state"

    def run_cli(self, *args, env_extra=None):
        env = dict(os.environ)
        env["XDG_STATE_HOME"] = str(self.state)
        # never leave import bytecode inside the copied skill tree: the
        # tamper-evidence snapshot compares the whole tree after the run
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(env_extra or {})
        return subprocess.run(self.inv + [str(a) for a in args],
                              capture_output=True, text=True, timeout=_TIMEOUT,
                              env=env)

    def parse(self, proc):
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(proc.stderr, "",
                         "a successful invocation must not write to stderr")
        self.assertEqual(proc.stdout.count("\n"), 1,
                         "exactly one JSON document on stdout")
        return json.loads(proc.stdout)

    def discover(self, query, env_extra=None):
        return self.parse(self.run_cli(
            "discover", "--repo", self.project, "--query", query,
            "--state-dir", self.state, env_extra=env_extra))

    def select_first(self, doc):
        candidate_id = doc["candidates"][0]["id"]
        self.parse(self.run_cli(
            "select", "--run-dir", doc["run_dir"],
            "--expect-revision", doc["revision"], "--candidate", candidate_id))
        return candidate_id

    def collect_usage(self, run_dir, expect_revision, extra=(), env_extra=None):
        return self.run_cli(
            "collect", "--run-dir", run_dir,
            "--expect-revision", expect_revision,
            "--usage-root", self.project / "build" / "root.jar",
            "--usage-dependency", self.project / "build" / "lib-b.jar",
            "--usage-dependency", self.project / "build" / "lib-c.jar",
            *extra, env_extra=env_extra)

    def report(self, run_dir, validate=True):
        args = ["report", "--run-dir", run_dir]
        if validate:
            args.append("--validate")
        return self.parse(self.run_cli(*args))

    def stored(self, wrapper):
        return wrapper["report"]

    def usage_payload(self, report_doc):
        payloads = [record["payload"] for record in report_doc["evidence"]
                    if record["category"] == "class-usage"]
        return payloads

    def assert_transitive_edges(self, payload):
        """The helper's actual ownership map: B -> C is owned by lib-c.jar,
        A -> B by lib-b.jar, and no A -> C edge is ever fabricated."""
        refs = {(ref["from_class"], ref["to_class"]):
                [owner["source"] for owner in ref["owner_candidates"]]
                for ref in payload["source_refs"]}
        self.assertEqual(refs[("com.example.app.App", "com.example.b.B")],
                         [str(self.project / "build" / "lib-b.jar")])
        self.assertEqual(refs[("com.example.b.B", "com.example.c.C")],
                         [str(self.project / "build" / "lib-c.jar")])
        self.assertNotIn(("com.example.app.App", "com.example.c.C"), refs)
        self.assertEqual(payload["status"], "complete", msg=str(payload["issues"]))
        self.assertEqual(payload["issues"], [])
        self.assertEqual(payload["cleanup"], "complete")

    def assert_helper_is_vendored(self, payload):
        argv = payload["runner"]["run"]["argv"]
        classpath = next(arg for arg in argv if "helper.jar" in arg).split(os.pathsep)
        self.assertEqual(len(classpath), 5)
        for entry in classpath:
            self.assertTrue(entry.startswith(str(self.skill) + os.sep),
                            f"helper classpath outside the copied skill: {entry}")
        self.assertTrue(
            any(entry.endswith(os.path.join("vendor", "helper.jar"))
                for entry in classpath), classpath)


class TestFullPipelineOneRun(EndToEndTestCase):
    """discover -> select -> collect -> import -> collect -> report, one
    immutable run, revisions threaded forward only."""

    def test_end_to_end_transitive_candidate_acceptance(self):
        # C is absent from the application POM: the only declared dependency
        # is b-lib, so lib-c can enter the run solely as packaged evidence.
        pom_text = (self.project / "pom.xml").read_text(encoding="utf-8")
        self.assertIn("<artifactId>b-lib</artifactId>", pom_text)
        self.assertNotIn("lib-c", pom_text)

        # 1. discover: the Boot-style fat JAR is the only lib-c candidate and
        # it is a packaged copy with real nested pom.properties coordinates.
        doc = self.discover("org.example:lib-c:1.0.0")
        self.assertTrue(doc["ok"])
        self.assertFalse(doc["no_match"])
        self.assertEqual([c["kind"] for c in doc["candidates"]],
                         ["packaged-copy"])
        self.assertEqual(doc["candidates"][0]["coordinates"],
                         "org.example:lib-c:1.0.0")
        self.assertEqual(doc["issues"], [])
        run_dir, revision = doc["run_dir"], doc["revision"]

        # 2. select: immutable, single candidate.
        candidate_id = self.select_first(doc)
        revision += 1

        # 3. collect with real usage: actual java runs the vendored helper
        # over the A/B/C bytecode; history is honestly not_applicable because
        # the selected candidate is archive-only.
        collect = self.parse(self.collect_usage(run_dir, revision))
        revision += 1
        self.assertEqual(collect["usage_outcome"], "complete")
        self.assertEqual(collect["history_outcome"], "not_applicable")
        wrapper = self.report(run_dir)
        self.assertTrue(wrapper["validation"]["valid"], wrapper["validation"])
        report = self.stored(wrapper)
        self.assertEqual(report["revision"], revision)
        self.assertEqual([s["name"] for s in report["stages"]],
                         ["discover", "history", "usage"])
        first_usage = self.usage_payload(report)
        self.assertEqual(len(first_usage), 1)
        self.assert_transitive_edges(first_usage[0])
        self.assert_helper_is_vendored(first_usage[0])

        # 4. import the recorded tree with fully caller-attested provenance:
        # the report itself is a locally generated synthetic fixture and says
        # so in its own fields; producer/version are attested by the caller,
        # so nothing of the pinned grammar is assumed away silently.
        tree_file = self.work / "tree.json"
        tree_file.write_text(json.dumps(TREE_JSON), encoding="utf-8")
        provenance_file = self.work / "provenance.json"
        provenance_file.write_text(json.dumps({
            "producer": "maven-dependency-plugin",
            "version": "3.11.0",
            "format": "dependency-tree json",
            "generated_at": "2024-03-01T00:00:00+00:00",
            "revision": "synthetic-fixture",
            "module": ".",
            "profiles": None,
            "jdk": _JAVAC_RELEASE,
            "maven": "synthetic-local-fixture: no Maven ran",
            "settings_digest": None,
            "dirty": False,
            "working_tree_digest": _sha256_file(self.project / "pom.xml"),
        }), encoding="utf-8")
        imported = self.parse(self.run_cli(
            "import", "--run-dir", run_dir, "--expect-revision", revision,
            "--report", tree_file, "--kind", "tree-json",
            "--provenance", provenance_file,
            # the fixture is a locally generated nonverbose tree: the
            # authoritative reading is attested explicitly, never defaulted
            # (the conservative default is the diagnostic verbose reading,
            # and the generic provenance format string attests no mode)
            "--tree-mode", "nonverbose"))
        revision += 1
        self.assertEqual(imported["provenance_attestation"], "caller-attested")
        self.assertEqual(imported["tree_mode"], "nonverbose")
        self.assertEqual(imported["tree_mode_attestation"], "flag")
        self.assertIsNone(imported["assumed_grammar"])
        self.assertEqual(imported["supplied_provenance"]["maven"],
                         "synthetic-local-fixture: no Maven ran")

        # 5. a second collect on the same run: stages update in place, the
        # revision advances and the schema still validates.
        second = self.parse(self.collect_usage(run_dir, revision))
        revision += 1
        self.assertEqual(second["usage_outcome"], "complete")
        wrapper = self.report(run_dir)
        self.assertTrue(wrapper["validation"]["valid"], wrapper["validation"])
        report = self.stored(wrapper)
        self.assertEqual(report["revision"], revision)
        self.assertEqual([s["name"] for s in report["stages"]],
                         ["discover", "history", "usage", "import-tree-json"])
        usages = self.usage_payload(report)
        self.assertEqual(len(usages), 2)
        for payload in usages:
            self.assert_transitive_edges(payload)

        # the imported tree is stored as caller-attested evidence naming B as
        # C's parent, and the qualification says caller-attested, never
        # verified against a generating process
        imports = [record for record in report["evidence"]
                   if record["category"] == "imported-tree-json"]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0]["payload"]["import"]["mode"], "nonverbose")
        self.assertEqual(imports[0]["payload"]["import"]["inclusion_authority"],
                         "recorded-context")
        nodes = imports[0]["payload"]["import"]["evidence"]["nodes"]
        by_coords = {node["id"]: node for node in nodes}
        c_node = next(node for node in nodes
                      if node["coordinates"] == {
                          "groupId": "org.example", "artifactId": "lib-c",
                          "version": "1.0.0"})
        parent = by_coords[c_node["parent"]]
        self.assertEqual(parent["coordinates"], {
            "groupId": "org.example", "artifactId": "b-lib", "version": "2.0.0"})
        self.assertEqual(imports[0]["generation_context"]["attestation"],
                         "caller-attested")
        self.assertIn("not verified", imports[0]["generation_context"]["note"])
        qualifications = {q["kind"] for q in report["qualifications"]}
        self.assertEqual(qualifications, {"caller-attested-provenance"})


class TestHistoryUpgradeCandidate(EndToEndTestCase):
    """The actual history collector over the local Git upgrade history: the
    recorded events are candidate leads, never a resolved transitive fact."""

    def test_upgrade_event_recorded_without_resolved_claim(self):
        doc = self.discover("org.example:b-lib")
        declarations = [c for c in doc["candidates"] if c["kind"] == "declaration"]
        self.assertEqual(len(declarations), 1)
        candidate_id = declarations[0]["id"]
        self.parse(self.run_cli(
            "select", "--run-dir", doc["run_dir"],
            "--expect-revision", doc["revision"], "--candidate", candidate_id))
        collect = self.parse(self.run_cli(
            "collect", "--run-dir", doc["run_dir"],
            "--expect-revision", doc["revision"] + 1))
        self.assertEqual(collect["history_outcome"], "complete")
        self.assertEqual(collect["usage_outcome"], "unavailable")
        wrapper = self.report(doc["run_dir"])
        self.assertTrue(wrapper["validation"]["valid"], wrapper["validation"])
        history = [record for record in self.stored(wrapper)["evidence"]
                   if record["category"] == "declaration-history"]
        self.assertEqual(len(history), 1)
        payload = history[0]["payload"]
        upgrades = [event for event in payload["events"]
                    if event["kind"] == "version-change"]
        self.assertEqual(len(upgrades), 1)
        self.assertEqual(upgrades[0]["matched_coordinates"],
                         {"group_id": "org.example", "artifact_id": "b-lib"})
        self.assertEqual(upgrades[0]["old_version"], "1.0.0")
        self.assertEqual(upgrades[0]["new_version"], "2.0.0")
        # candidate evidence only: no resolved or shipped claim about C, and
        # the module's own limits say resolution never happened
        self.assertNotIn("lib-c", json.dumps(payload))
        self.assertTrue(any("no Maven resolution" in item
                            for item in payload["coverage"]["limitations"]))


class TestMissingJavaGraceful(EndToEndTestCase):
    """A broken java runtime is a recorded unavailable stage, exit 0."""

    def test_collect_reports_usage_unavailable_and_still_validates(self):
        doc = self.discover("org.example:lib-c:1.0.0")
        self.select_first(doc)
        # A PATH that still resolves git (the history collector needs it) but
        # no java runtime at all: no --java override, the trusted PATH
        # resolution simply finds nothing usable.
        restricted = self.work / "no-java-path"
        restricted.mkdir()
        os.symlink(shutil.which("git"), restricted / "git")
        proc = self.collect_usage(doc["run_dir"], doc["revision"] + 1,
                                  env_extra={"PATH": str(restricted)})
        doc_collect = self.parse(proc)
        self.assertEqual(doc_collect["usage_outcome"], "unavailable")
        self.assertEqual(doc_collect["history_outcome"], "not_applicable")
        wrapper = self.report(doc["run_dir"])
        self.assertTrue(wrapper["validation"]["valid"], wrapper["validation"])
        report = self.stored(wrapper)
        usage = [s for s in report["stages"] if s["name"] == "usage"][0]
        self.assertEqual(usage["outcome"], "unavailable")
        self.assertTrue(usage["reasons"])
        payloads = self.usage_payload(report)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["status"], "unavailable")
        self.assertEqual(payloads[0]["source_refs"], [])
        self.assertIn("java-unavailable",
                      [issue["code"] for issue in payloads[0]["issues"]])


if __name__ == "__main__":
    unittest.main()
