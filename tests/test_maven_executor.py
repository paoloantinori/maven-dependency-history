"""Tests for mdi.maven_executor: consent-gated bounded Maven execution.

Every execution test runs synthetic local child processes only: this
interpreter through ``-c`` scripts, plus one tiny synthetic executable
script. No Maven binary, project wrapper, network request or download is
ever performed by this suite. Real-child tests prove durable consumed
state before spawn, sanitized environment, bounded combined output, total
timeout, process-group cleanup after leader exit, and that raw child
output never reaches the persisted report; injected-runner tests prove
consent ordering, refusal and no-retry behavior without spawning.
"""

import copy
import hashlib
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
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import maven_executor, operations, runstore
from mdi.maven_executor import (
    CONTROL_HOME_PREFIX,
    COVERAGE_DISCLOSURE,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    DESTINATIONS_UNKNOWN,
    ENVIRONMENT_DISCLOSURE,
    TOOL_BINDING,
    UNSANDBOXED_DISCLOSURE,
    WRITES_DISCLOSURE,
    MavenExecutionError,
    execute_maven,
    maven_proposal,
    observe_executable,
)
from mdi.operations import ApprovalError, ProposalError, StateError
from mdi.runstore import RunConflict

PYTHON = sys.executable
JAVA = shutil.which("java")
SECRET = "SECRET-mdi-child-output"
DESTINATIONS = ["Maven Central mirror https://repo.maven.apache.org/maven2"]
DESTINATIONS_CLAUSE = "downloads-destinations: explicit: " + " | ".join(DESTINATIONS)

POLLUTED_ENV = {
    "MAVEN_OPTS": "-Xmx1g -javaagent:evil-agent.jar",
    "MAVEN_DEBUG_OPTS": "-Xdebug",
    "MAVEN_ARGS": "-Devil=true",
    "JAVA_TOOL_OPTIONS": "-javaagent:evil-agent.jar",
    "JDK_JAVA_OPTIONS": "-javaagent:evil-agent.jar",
    "_JAVA_OPTIONS": "-Xmx2g",
    "CLASSPATH": "/evil:/also-evil",
    "MAVEN_CONFIG": "/evil/.mavenrc",
    "MDI_PARENT_CANARY": "canary-value",
}

OK_RESULT = SimpleNamespace(outcome="ok", rc=0, stdout=b"", stderr=b"", kill_state=None,
                            issue=None, identity="held")


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_disclosure(clause=DESTINATIONS_CLAUSE, argv_items=2, cwd="/repo") -> str:
    return "\n".join(
        [
            f"Run the approved Maven command ({argv_items} argv items) with working directory {cwd}.",
            UNSANDBOXED_DISCLOSURE,
            WRITES_DISCLOSURE,
            ENVIRONMENT_DISCLOSURE,
            COVERAGE_DISCLOSURE,
            clause,
            "The executable identity and 1 captured input identities are pinned in this "
            "operation record; any observed change requires a new proposal and renewed approval.",
        ]
    )


def wait_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.05)
    return False


_UNSET = object()


class FakeRunner:
    """In-memory spawn hook recording every call; never starts a process."""

    def __init__(self, result=_UNSET, exc=None, store=None, operation_id=None):
        self.calls = []
        self.result = OK_RESULT if result is _UNSET else result
        self.exc = exc
        self.store = store
        self.operation_id = operation_id

    def __call__(self, argv, cwd, env, *, timeout_seconds, max_output_bytes):
        entry = {
            "argv": list(argv),
            "cwd": cwd,
            "env": dict(env),
            "timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
        }
        if self.store is not None:
            report = self.store.load()
            entry["state_when_called"] = next(
                (op["state"] for op in report["operations"] if op["id"] == self.operation_id),
                None,
            )
            entry["revision_when_called"] = report["revision"]
        self.calls.append(entry)
        if self.exc is not None:
            raise self.exc
        return self.result


class ExecutorCase(unittest.TestCase):
    """A real RunStore, a synthetic project, one proposed operation and approval."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = tmp.name
        self.project = Path(self.base) / "project"
        self.project.mkdir()
        self.pom = self.project / "pom.xml"
        self.pom.write_text("<project></project>\n")
        self.store = runstore.RunStore.create(self.base, query="commons-lang3:3.12.0")
        self.operation_id = self.propose()
        self.approval = self.build_approval()

    def use_tempdir(self, path):
        """Point tempfile's cache-cleared base at path for one test."""
        old_env = os.environ.get("TMPDIR")
        old_cache = tempfile.tempdir
        os.environ["TMPDIR"] = str(path)
        tempfile.tempdir = None

        def restore():
            tempfile.tempdir = old_cache
            if old_env is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = old_env

        self.addCleanup(restore)

    def child_argv(self, code, *args):
        return [PYTHON, "-c", code, *[str(arg) for arg in args]]

    def propose(self, argv=None, operation_id="op-1", destinations=None,
                input_paths=None, cwd=None):
        # The default child prints its marker in two runtime-joined halves so
        # the contiguous output line can never appear inside the approved
        # argv text that operations.py legitimately persists in the scope.
        record = maven_proposal(
            self.store,
            self.child_argv("print('BUILD' + ' SUCCESS (mdi synthetic)')") if argv is None else argv,
            str(self.project if cwd is None else cwd),
            network_destinations=destinations if destinations is not None else DESTINATIONS,
            input_paths=[self.pom] if input_paths is None else input_paths,
            operation_id=operation_id,
        )
        operations.propose(self.store, record, self.revision())
        return record["id"]

    def propose_raw(self, scope=None, inputs=None, tool=None, disclosure=None,
                    operation_id="op-raw", kind="maven"):
        record = operations.build_proposal(
            kind,
            scope if scope is not None else {
                "kind": "maven",
                "argv": self.child_argv("print('x')"),
                "cwd": str(self.project),
            },
            inputs if inputs is not None else [
                {"path": str(self.pom), "sha256": sha_of(self.pom.read_bytes())}
            ],
            tool if tool is not None else dict(
                TOOL_BINDING, executable=observe_executable(PYTHON)
            ),
            disclosure if disclosure is not None else valid_disclosure(),
            self.store.run_id,
            operation_id,
        )
        operations.propose(self.store, record, self.revision())
        return record["id"]

    def build_approval(self, operation_id=None, **over):
        record = {
            "run_id": self.store.run_id,
            "operation_id": operation_id or self.operation_id,
            "scope": copy.deepcopy(self.operation(operation_id or self.operation_id)["scope"]),
            "approved": True,
            "approved_by": "host-operator",
            "reference": "host chat 2026-09-12 turn 47",
        }
        record.update(over)
        return record

    def execute(self, **over):
        kwargs = {"expected_revision": self.revision()}
        kwargs.update(over)
        return execute_maven(self.store, self.operation_id, self.approval, **kwargs)

    def revision(self):
        return self.store.load()["revision"]

    def operation(self, operation_id=None):
        wanted = operation_id or self.operation_id
        for record in self.store.load()["operations"]:
            if record["id"] == wanted:
                return record
        self.fail(f"operation {wanted!r} not found")

    def evidence_records(self, report=None):
        report = report if report is not None else self.store.load()
        return list(report["evidence"])

    def synthetic_executable(self, marker=None):
        """A tiny executable script standing in for a Maven launcher."""
        script = Path(self.base) / "mvn-like"
        if marker is None:
            script.write_text("#!/bin/sh\nexit 0\n")
        else:
            script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        script.chmod(0o755)
        return script


class ProposalFactoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = tmp.name
        self.store = runstore.RunStore.create(self.base, query="q")
        self.project = Path(self.base) / "project"
        self.project.mkdir()
        self.pom = self.project / "pom.xml"
        self.pom.write_text("<project></project>\n")

    def propose(self, **over):
        kwargs = {
            "argv": [PYTHON, "-q"],
            "cwd": str(self.project),
            "network_destinations": DESTINATIONS,
            "input_paths": [self.pom],
        }
        kwargs.update(over)
        return maven_proposal(self.store, **kwargs)

    def test_proposal_shape_disclosure_and_bindings(self):
        record = self.propose()
        self.assertEqual(record["state"], "proposed")
        self.assertEqual(record["run_id"], self.store.run_id)
        self.assertEqual(record["scope"]["kind"], "maven")
        self.assertEqual(record["scope"]["cwd"], str(self.project))
        self.assertEqual(
            record["input_identities"],
            [{"path": str(self.pom), "sha256": sha_of(self.pom.read_bytes())}],
        )
        self.assertEqual(record["tool_identity"]["name"], TOOL_BINDING["name"])
        self.assertEqual(record["tool_identity"]["source_sha256"], TOOL_BINDING["source_sha256"])
        self.assertEqual(
            record["tool_identity"]["executable"]["sha256"],
            sha_of(Path(PYTHON).read_bytes()),
        )
        disclosure = record["disclosure"]
        self.assertIn(UNSANDBOXED_DISCLOSURE, disclosure)
        self.assertIn(WRITES_DISCLOSURE, disclosure)
        self.assertIn(ENVIRONMENT_DISCLOSURE, disclosure)
        self.assertIn(COVERAGE_DISCLOSURE, disclosure)
        self.assertIn(DESTINATIONS_CLAUSE, disclosure)

    def test_environment_disclosure_names_dropped_java_home_and_real_settings(self):
        record = self.propose()
        self.assertIn(ENVIRONMENT_DISCLOSURE, record["disclosure"])
        self.assertIn(COVERAGE_DISCLOSURE, record["disclosure"])
        # The approved text must state what the sanitized environment means
        # for a Maven launcher: JAVA_HOME dropped, any JVM resolved from the
        # sanitized PATH, real-HOME settings absent.
        self.assertIn("JAVA_HOME", ENVIRONMENT_DISCLOSURE)
        self.assertIn("PATH", ENVIRONMENT_DISCLOSURE)
        self.assertIn("~/.m2/settings.xml", ENVIRONMENT_DISCLOSURE)
        # Coverage is host-chosen; unlisted files are unknown, never
        # automatically complete.
        self.assertIn("host", COVERAGE_DISCLOSURE)
        self.assertIn("unknown", COVERAGE_DISCLOSURE)

    def test_unknown_destinations_accepted_and_disclosed(self):
        record = self.propose(network_destinations=DESTINATIONS_UNKNOWN)
        self.assertIn("downloads-destinations: unknown", record["disclosure"])

    def test_bad_destinations_refused(self):
        bad = [
            [],
            [""],
            ["a\nb"],
            ["a | b"],
            "maybe",
            7,
            ["ok"] * 33,
            ["x" * 2049],
        ]
        for value in bad:
            with self.assertRaises(MavenExecutionError, msg=repr(value)):
                self.propose(network_destinations=value)

    def test_overlong_composed_disclosure_refused_before_persistence(self):
        # Four maximal destination descriptions push the composed disclosure
        # past the persistence bound; the refusal must come from this module
        # as MavenExecutionError, never as a storage-layer ProposalError
        # after the fact.
        with self.assertRaises(MavenExecutionError):
            self.propose(network_destinations=["d" * 2048] * 4)

    def test_relative_executable_refused(self):
        with self.assertRaises(MavenExecutionError):
            self.propose(argv=["mvn", "-q"])

    def test_shell_string_argv_refused(self):
        with self.assertRaises(MavenExecutionError):
            self.propose(argv="/usr/bin/mvn -q dependency:tree")

    def test_relative_cwd_refused_by_operations(self):
        with self.assertRaises((ProposalError, MavenExecutionError)):
            self.propose(cwd="project")

    def test_missing_cwd_refused(self):
        with self.assertRaises(MavenExecutionError):
            self.propose(cwd=str(self.project / "nope"))

    def test_empty_inputs_refused(self):
        with self.assertRaises(MavenExecutionError):
            self.propose(input_paths=[])

    def test_oversized_input_capture_refused(self):
        big = self.project / "big.settings"
        big.write_bytes(b"x" * 16)
        with self.assertRaises(MavenExecutionError):
            self.propose(input_paths=[big], max_input_bytes=8)

    def test_accepts_run_id_string_and_generates_ids(self):
        first = maven_proposal(
            "run-x", [PYTHON, "-q"], str(self.project),
            network_destinations=DESTINATIONS_UNKNOWN, input_paths=[self.pom],
        )
        second = maven_proposal(
            "run-x", [PYTHON, "-q"], str(self.project),
            network_destinations=DESTINATIONS_UNKNOWN, input_paths=[self.pom],
        )
        self.assertEqual(first["run_id"], "run-x")
        self.assertNotEqual(first["id"], second["id"])

    def test_proposal_is_acceptable_to_operations(self):
        record = self.propose()
        persisted = operations.propose(self.store, record, self.store.load()["revision"])
        self.assertEqual(persisted["operations"][0]["id"], record["id"])

    def test_tool_binding_carries_source_digest(self):
        digest = hashlib.sha256(Path(maven_executor.__file__).read_bytes()).hexdigest()
        self.assertEqual(TOOL_BINDING["source_sha256"], digest)

    def test_defaults_follow_plan(self):
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 900)
        self.assertEqual(DEFAULT_MAX_OUTPUT_BYTES, 8 * 1024 * 1024)

    def test_observe_executable_never_executes_it(self):
        marker = Path(self.base) / "ran-marker"
        script = Path(self.base) / "mvn-like"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        script.chmod(0o755)
        identity = observe_executable(str(script))
        self.assertEqual(identity["sha256"], sha_of(script.read_bytes()))
        self.assertEqual(identity["realpath"], str(script.resolve()))
        self.assertFalse(marker.exists())

    def test_observe_executable_refuses_relative_and_missing(self):
        with self.assertRaises(MavenExecutionError):
            observe_executable("mvn")
        missing = str(Path(self.base) / "absent")
        with self.assertRaises(MavenExecutionError) as caught:
            observe_executable(missing)
        self.assertNotIn(missing, str(caught.exception))

    def test_observe_executable_oserror_fallback_never_leaks_the_path(self):
        # An OSError without strerror must fall back to static wording, not
        # to str(exc), whose text embeds the offending path.
        hostile = OSError("/secret/launcher/path vanished")
        self.assertIsNone(hostile.strerror)
        with mock.patch.object(maven_executor.os, "stat", side_effect=hostile):
            with self.assertRaises(MavenExecutionError) as caught:
                observe_executable("/secret/launcher/path")
        message = str(caught.exception)
        self.assertNotIn("/secret", message)
        self.assertNotIn("vanished", message)


class SuccessfulRealChildTests(ExecutorCase):
    def test_concurrent_revision_after_consume_refuses_evidence_commit(self):
        def concurrent_runner(*args, **kwargs):
            report = self.store.load()
            self.store.commit(report, report["revision"])
            return FakeRunner()(*args, **kwargs)

        with self.assertRaises(RunConflict):
            self.execute(runner=concurrent_runner)
        self.assertEqual(self.operation()["state"], "consumed")
        self.assertEqual(self.store.load()["evidence"], [])

    def test_exit_zero_succeeds_with_digest_only_evidence(self):
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertIn(b"BUILD SUCCESS (mdi synthetic)", outcome["output"])
        record = self.operation()
        self.assertEqual(record["state"], "finished")
        self.assertEqual(record["audit"]["outcome"]["result"], "succeeded")
        self.assertEqual(record["evidence_ids"], [outcome["evidence_id"]])
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["category"], "maven-run")
        self.assertEqual(evidence["source_locator"], str(self.project))
        payload = evidence["payload"]
        self.assertEqual(payload["process"]["outcome"], "ok")
        self.assertEqual(payload["process"]["returncode"], 0)
        self.assertEqual(payload["output"]["bytes"], len(outcome["output"]))
        self.assertEqual(payload["output"]["sha256"], sha_of(outcome["output"]))
        self.assertFalse(payload["output"]["capped"])
        self.assertEqual(payload["destinations"], {"mode": "explicit", "items": DESTINATIONS})
        self.assertEqual(payload["command"]["argv_items"], len(record["scope"]["argv"]))
        clean_input = payload["inputs"][0]
        self.assertEqual(clean_input["pinned_sha256"], clean_input["observed_after_sha256"])
        self.assertFalse(clean_input["changed_after_run"])
        # The group-kill claim must state its own limit: a descendant that
        # starts its own session escapes the process group.
        self.assertTrue(
            any("own session" in item for item in payload["limitations"]),
            payload["limitations"],
        )
        # Raw child output never reaches the persisted report.
        self.assertNotIn("BUILD SUCCESS", json.dumps(self.store.load()))

    def test_argv_sha256_covers_exact_approved_vector(self):
        outcome = self.execute()
        argv = self.operation()["scope"]["argv"]
        expected = hashlib.sha256(json.dumps(argv).encode("utf-8")).hexdigest()
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["command"]["argv_sha256"], expected)

    def test_child_sees_exact_argv_cwd_stdin_and_sanitized_env(self):
        for key, value in POLLUTED_ENV.items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        code = (
            "import json, os, sys\n"
            "for key in sorted(os.environ):\n"
            "    print(f'{key}={os.environ[key]}')\n"
            "print('CWD=' + os.getcwd())\n"
            "print('ARGV=' + json.dumps(sys.argv))\n"
            "print('STDIN=' + repr(sys.stdin.read()))\n"
        )
        argv = self.child_argv(code, "one", "two")
        self.propose(argv=argv, operation_id="op-env")
        self.operation_id = "op-env"
        self.approval = self.build_approval()
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        lines = dict(
            line.split("=", 1) for line in outcome["output"].decode().splitlines() if "=" in line
        )
        self.assertEqual(
            set(lines) - {"CWD", "ARGV", "STDIN"},
            {"PATH", "HOME", "LC_ALL", "JAVA_TOOL_OPTIONS"},
        )
        # Every inherited variable is gone; JAVA_TOOL_OPTIONS exists only as
        # the executor's own trusted injection, never the inherited value.
        for banned in POLLUTED_ENV:
            if banned == "JAVA_TOOL_OPTIONS":
                continue
            self.assertNotIn(banned, lines)
        self.assertNotIn("MDI_PARENT_CANARY", lines)
        injected = lines["JAVA_TOOL_OPTIONS"]
        self.assertTrue(injected.startswith('-Duser.home="'), injected)
        self.assertNotIn("evil", injected)
        self.assertEqual(lines["LC_ALL"], "C")
        self.assertNotEqual(lines["HOME"], os.environ.get("HOME") or "")
        self.assertTrue(all(os.path.isabs(p) and p for p in lines["PATH"].split(os.pathsep)))
        self.assertEqual(lines["CWD"], str(self.project))
        # python -c CODE args... exposes ['-c', args...] as sys.argv.
        self.assertEqual(json.loads(lines["ARGV"]), ["-c"] + argv[3:])
        self.assertEqual(lines["STDIN"], "''")

    def test_durable_consumed_state_visible_to_real_child_at_spawn(self):
        code = (
            "import glob, json, sys\n"
            "latest = max(glob.glob(sys.argv[1] + '/report-*.json'))\n"
            "with open(latest) as fh:\n"
            "    doc = json.load(fh)\n"
            "for op in doc['operations']:\n"
            "    print(op['id'] + '=' + op['state'])\n"
            "print('REV=' + str(doc['revision']))\n"
        )
        argv = self.child_argv(code, self.store.path)
        self.propose(argv=argv, operation_id="op-order")
        self.operation_id = "op-order"
        self.approval = self.build_approval()
        revision_before = self.revision()
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        text = outcome["output"].decode()
        self.assertIn("op-order=consumed", text)
        self.assertIn(f"REV={revision_before + 1}", text)

    def test_input_drift_during_run_is_flagged_not_hidden(self):
        code = (
            "import sys\n"
            "with open(sys.argv[1], 'a') as fh:\n"
            "    fh.write('drift')\n"
        )
        argv = self.child_argv(code, self.pom)
        self.propose(argv=argv, operation_id="op-drift")
        self.operation_id = "op-drift"
        self.approval = self.build_approval()
        pinned = sha_of(self.pom.read_bytes())
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        evidence = self.evidence_records()[0]
        entry = evidence["payload"]["inputs"][0]
        self.assertEqual(entry["pinned_sha256"], pinned)
        self.assertEqual(entry["observed_after_sha256"], sha_of(self.pom.read_bytes()))
        self.assertTrue(entry["changed_after_run"])
        self.assertNotEqual(entry["observed_after_sha256"], pinned)


class RefusedBeforeConsumeTests(ExecutorCase):
    """Policy and observation refusals commit nothing and spawn nothing."""

    def setUp(self):
        super().setUp()
        self.runner = FakeRunner(store=self.store, operation_id=self.operation_id)
        self.runner.operation_id = self.operation_id

    def refuse(self, operation_id=None, approval=None, **over):
        kwargs = {"expected_revision": self.revision(), "runner": self.runner}
        kwargs.update(over)
        before = self.revision()
        with self.assertRaises((MavenExecutionError, ApprovalError)):
            execute_maven(
                self.store,
                operation_id or self.operation_id,
                approval if approval is not None else self.approval,
                **kwargs,
            )
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.operation(operation_id or self.operation_id)["state"], "proposed")
        self.assertEqual(self.revision(), before)

    def test_relative_executable_refused(self):
        scope = {
            "kind": "maven",
            "argv": ["mvn", "-q"],
            "cwd": str(self.project),
        }
        tool = dict(TOOL_BINDING, executable={"path": "mvn"})
        operation_id = self.propose_raw(scope=scope, tool=tool)
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_missing_executable_refused(self):
        script = self.synthetic_executable()
        self.propose(argv=[str(script)], operation_id="op-tool")
        script.unlink()
        self.operation_id = "op-tool"
        self.approval = self.build_approval()
        self.refuse()

    def test_changed_executable_refused(self):
        script = self.synthetic_executable()
        self.propose(argv=[str(script)], operation_id="op-tool")
        script.write_text("#!/bin/sh\nexit 0\n# changed\n")
        self.operation_id = "op-tool"
        self.approval = self.build_approval()
        self.refuse()

    def test_changed_input_refused(self):
        self.pom.write_text("<project>changed</project>\n")
        self.refuse()

    def test_missing_input_refused(self):
        self.pom.unlink()
        self.refuse()

    def test_symlinked_input_refused(self):
        replacement = self.project / "real-pom.xml"
        replacement.write_text("<project></project>\n")
        self.pom.unlink()
        self.pom.symlink_to(replacement)
        self.refuse()

    def test_missing_cwd_refused(self):
        scope = {
            "kind": "maven",
            "argv": self.child_argv("print('x')"),
            "cwd": str(self.project / "gone"),
        }
        operation_id = self.propose_raw(scope=scope)
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_empty_input_identities_refused(self):
        operation_id = self.propose_raw(inputs=[])
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_foreign_tool_identity_refused(self):
        operation_id = self.propose_raw(tool={"name": "other-executor"})
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_tool_identity_pinning_another_executable_refused(self):
        # The binding names this executor but pins a different executable
        # than the scope's argv[0]: the observation at consume time must
        # disagree with the record and refuse.
        other = self.synthetic_executable()
        tool = dict(TOOL_BINDING, executable=observe_executable(str(other)))
        scope = {
            "kind": "maven",
            "argv": self.child_argv("print('x')"),
            "cwd": str(self.project),
        }
        operation_id = self.propose_raw(scope=scope, tool=tool)
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_advisory_operation_refused(self):
        operation_id = self.propose_raw(
            kind="advisory",
            scope={
                "kind": "advisory",
                "destination": "https://api.osv.dev/v1/query",
                "query": {"ecosystem": "Maven", "name": "g:a", "version": "1"},
            },
            inputs=[],
        )
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_disclosure_without_unsandboxed_statement_refused(self):
        disclosure = valid_disclosure()
        disclosure = disclosure.replace(UNSANDBOXED_DISCLOSURE, "Maven may write files.")
        operation_id = self.propose_raw(disclosure=disclosure)
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_disclosure_without_environment_or_coverage_statement_refused(self):
        for sentence in (ENVIRONMENT_DISCLOSURE, COVERAGE_DISCLOSURE):
            disclosure = valid_disclosure().replace(sentence, "Environment is fine.")
            operation_id = self.propose_raw(
                disclosure=disclosure, operation_id=f"op-env-{len(sentence)}"
            )
            self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_disclosure_without_destinations_line_refused(self):
        disclosure = "\n".join(
            line for line in valid_disclosure().split("\n") if not line.startswith("downloads-destinations:")
        )
        operation_id = self.propose_raw(disclosure=disclosure)
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_disclosure_with_foreign_destinations_clause_refused(self):
        for clause in (
            "downloads-destinations: maybe",
            "downloads-destinations: explicit: ",
            "downloads-destinations: unknown ",
        ):
            operation_id = self.propose_raw(
                disclosure=valid_disclosure(clause=clause),
                operation_id=f"op-clause-{clause.strip()[-6:]}",
            )
            self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_stale_revision_conflicts_without_consuming(self):
        with self.assertRaises(RunConflict):
            self.execute(runner=self.runner, expected_revision=self.revision() - 1)
        self.assertEqual(self.runner.calls, [])

    def test_sibling_approval_rejected_before_spawn(self):
        other_id = self.propose(operation_id="op-sibling")
        # The approval is bound to the sibling operation, not the one executed.
        sibling_approval = self.build_approval(other_id)
        self.refuse(approval=sibling_approval)
        self.assertEqual(self.operation(other_id)["state"], "proposed")

    def test_altered_approval_scope_rejected_before_spawn(self):
        self.approval["scope"]["argv"] = self.approval["scope"]["argv"] + ["-Dextra=true"]
        self.refuse()

    def test_denied_operation_never_runs(self):
        operations.deny_proposal(self.store, self.operation_id, "host said no", self.revision())
        with self.assertRaises(ApprovalError):
            self.execute(runner=self.runner)
        self.assertEqual(self.runner.calls, [])

    def test_unknown_operation_id_raises_state_error(self):
        with self.assertRaises(StateError):
            execute_maven(self.store, "op-absent", self.approval,
                          expected_revision=self.revision(), runner=self.runner)

    def test_bad_invocation_arguments_refused_before_consume(self):
        for bad in (0, -1, "5", True):
            self.refuse(timeout_seconds=bad)
        # Non-finite timeouts must never reach the deadline arithmetic: they
        # once compared False against <= 0, consumed consent, spawned the
        # child and only then blew up inside the selector/wait math.
        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            self.refuse(timeout_seconds=nonfinite)
        self.refuse(max_output_bytes=0)
        self.refuse(max_input_bytes=-2)
        self.refuse(runner=object())
        self.assertEqual(self.revision(), 1)

    def test_quote_bearing_temp_base_refused_before_consume(self):
        # A quote inside the temporary base would let a crafted path break
        # out of the JVM option's quoting and inject options, so the base is
        # validated before consent is spent, not discovered after spawn.
        hostile = Path(self.base) / 'bad"quote'
        hostile.mkdir()
        self.use_tempdir(hostile)
        self.refuse()

    def test_memory_backed_temp_base_refused_before_consume(self):
        # The pinned user.home puts Maven's local repository under the temp
        # base, so a tmpfs base turns every downloaded artifact into RAM; the
        # same pre-consent choke point refuses it.
        from mdi import fsprobe
        with mock.patch.object(fsprobe, "fs_magic", return_value=0x01021994):
            with self.assertRaises(maven_executor.MavenExecutionError) as caught:
                maven_executor._checked_temp_base()
        self.assertIn("memory-backed", str(caught.exception))
        with mock.patch.object(fsprobe, "fs_magic", return_value=0x01021994):
            self.refuse()

    def test_nondefault_sigchld_disposition_refused_before_consume(self):
        # SIGCHLD=SIG_IGN makes the kernel auto-reap the child (observed
        # locally: waitid then raises ECHILD and Popen.wait fabricates
        # return code 0), and a custom handler may reap concurrently; either
        # destroys the unreaped-identity cleanup guarantee, so both are
        # refused before consent is spent.
        real = signal.getsignal(signal.SIGCHLD)
        try:
            for hostile in (signal.SIG_IGN, lambda *_args: None):
                signal.signal(signal.SIGCHLD, hostile)
                self.refuse()
        finally:
            signal.signal(signal.SIGCHLD, real)


class PinnedCapTests(ExecutorCase):
    """Caps raised at proposal time are pinned into the record and honored at
    re-observation; a caller-side default must never make an unchanged target
    or input unexecutable (A2)."""

    def test_pinned_output_cap_survives_reobservation(self):
        target = Path(self.base) / "big-target.json"
        target.write_bytes(b"x" * (10 * 1024**2))
        record = maven_proposal(
            self.store, self.child_argv("print('done')"), str(self.project),
            network_destinations=DESTINATIONS, input_paths=[self.pom],
            output_path=str(target), allow_overwrite=True,
            max_output_target_bytes=16 * 1024**2)
        operations.propose(self.store, record, self.revision())
        self.operation_id = record["id"]
        self.approval = self.build_approval()
        result = self.execute()
        self.assertEqual(result["outcome"], "succeeded", result.get("output"))

    def test_pinned_input_cap_survives_reobservation(self):
        big = Path(self.base) / "big-input.bin"
        big.write_bytes(b"y" * (10 * 1024**2))
        record = maven_proposal(
            self.store, self.child_argv("print('done')"), str(self.project),
            network_destinations=DESTINATIONS, input_paths=[self.pom, big],
            max_input_bytes=16 * 1024**2)
        operations.propose(self.store, record, self.revision())
        self.operation_id = record["id"]
        self.approval = self.build_approval()
        result = self.execute()
        self.assertEqual(result["outcome"], "succeeded", result.get("output"))

    def test_post_run_observation_uses_pinned_output_cap(self):
        target = Path(self.base) / "grown-target.json"
        record = maven_proposal(
            self.store,
            self.child_argv(f"open({str(target)!r}, 'wb').write(b'z' * (10 * 1024**2))"),
            str(self.project),
            network_destinations=DESTINATIONS, input_paths=[self.pom],
            output_path=str(target),
            max_output_target_bytes=16 * 1024**2)
        operations.propose(self.store, record, self.revision())
        self.operation_id = record["id"]
        self.approval = self.build_approval()
        result = self.execute()
        self.assertEqual(result["outcome"], "succeeded", result.get("output"))
        evidence = next(e for e in result["report"]["evidence"]
                        if e["id"] == result["evidence_id"])
        observed = evidence["payload"]["output_target"]["observed_after"]
        self.assertEqual(observed["state"], "present")
        self.assertEqual(observed["size"], 10 * 1024**2)

class InjectedRunnerTests(ExecutorCase):
    """A fake spawn hook proves ordering, retries and failure accounting."""

    def runner(self, **over):
        runner = FakeRunner(store=self.store, operation_id=self.operation_id, **over)
        runner.operation_id = self.operation_id
        return runner

    def test_runner_sees_durable_consumed_state_and_sanitized_env(self):
        captured = {}

        class RecordingRunner(FakeRunner):
            def __call__(self, argv, cwd, env, **kwargs):
                captured["env"] = dict(env)
                home = env["JAVA_TOOL_OPTIONS"][len("-Duser.home="):].strip('"')
                # Observed at call time: the control HOME still exists while
                # the spawn hook runs and is removed only after cleanup.
                captured["home_alive"] = Path(home).is_dir()
                return super().__call__(argv, cwd, env, **kwargs)

        runner = RecordingRunner(store=self.store, operation_id=self.operation_id)
        runner.operation_id = self.operation_id
        before = self.revision()
        outcome = self.execute(runner=runner)
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(len(runner.calls), 1)
        call = runner.calls[0]
        self.assertEqual(call["state_when_called"], "consumed")
        self.assertEqual(call["revision_when_called"], before + 1)
        self.assertEqual(call["argv"], self.operation()["scope"]["argv"])
        self.assertEqual(call["cwd"], str(self.project))
        self.assertEqual(set(call["env"]), {"PATH", "HOME", "LC_ALL", "JAVA_TOOL_OPTIONS"})
        # The one trusted injection points the JVM's user.home at the live
        # control HOME for the duration of the spawn hook.
        injected = call["env"]["JAVA_TOOL_OPTIONS"]
        self.assertTrue(injected.startswith("-Duser.home="), injected)
        control_home = injected[len("-Duser.home="):]
        self.assertTrue(Path(control_home).name.startswith(CONTROL_HOME_PREFIX))
        self.assertTrue(captured["home_alive"])
        self.assertEqual(call["timeout_seconds"], 900)
        self.assertEqual(call["max_output_bytes"], DEFAULT_MAX_OUTPUT_BYTES)

    def test_success_cannot_be_replayed(self):
        runner = self.runner()
        self.execute(runner=runner)
        with self.assertRaises(ApprovalError):
            self.execute(runner=FakeRunner())
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.operation()["state"], "finished")

    def test_failure_runs_exactly_once_with_no_retry(self):
        runner = self.runner(result=SimpleNamespace(
            outcome="ok", rc=3, stdout=b"", stderr=b"", kill_state=None, issue=None,
            identity="held"))
        outcome = self.execute(runner=runner)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(len(runner.calls), 1)
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["category"], "maven-run-failure")
        self.assertEqual(evidence["payload"]["failure"], {"reason": "nonzero-exit", "returncode": 3})
        with self.assertRaises(ApprovalError):
            self.execute(runner=FakeRunner())
        self.assertEqual(len(runner.calls), 1)

    def test_timeout_result_recorded_with_kill_state(self):
        runner = self.runner(result=SimpleNamespace(
            outcome="timeout", rc=None, stdout=b"", stderr=b"", kill_state="sent", issue=None,
            identity="held"))
        outcome = self.execute(runner=runner)
        self.assertEqual(outcome["outcome"], "failed")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "timeout")
        self.assertEqual(evidence["payload"]["process"]["kill_state"], "sent")

    def test_runner_exception_recorded_without_message_leakage(self):
        runner = self.runner(exc=RuntimeError(f"boom {SECRET}"))
        outcome = self.execute(runner=runner)
        self.assertEqual(outcome["outcome"], "failed")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "runner-error")
        self.assertEqual(evidence["payload"]["failure"]["error_type"], "RuntimeError")
        self.assertNotIn(SECRET, json.dumps(self.store.load()))

    def test_runner_returning_garbage_recorded_as_failure(self):
        runner = self.runner(result=None)
        outcome = self.execute(runner=runner)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(self.evidence_records()[0]["payload"]["failure"]["reason"], "runner-error")

    def test_runner_returning_wrong_types_recorded_as_failure(self):
        runner = self.runner(result=SimpleNamespace(
            outcome="ok", rc=0, stdout="not-bytes", stderr=b"", kill_state=None, issue=None,
            identity="held"))
        outcome = self.execute(runner=runner)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(self.evidence_records()[0]["payload"]["failure"]["reason"], "runner-error")

    def test_runner_ok_without_rc_is_an_invalid_result(self):
        # outcome ok with rc None is not a successful or nonzero run; it is a
        # stand-in that lied about its shape, and must be classified
        # runner-error, never nonzero-exit with a null returncode.
        runner = self.runner(result=SimpleNamespace(
            outcome="ok", rc=None, stdout=b"", stderr=b"", kill_state=None, issue=None,
            identity="held"))
        outcome = self.execute(runner=runner)
        self.assertEqual(outcome["outcome"], "failed")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "runner-error")
        self.assertNotIn("returncode", evidence["payload"]["failure"])

    def test_identity_lost_runner_result_never_succeeds(self):
        # A stand-in reporting a fabricated rc 0 together with a lost
        # identity must be classified failed with the identity-lost reason,
        # never successful.
        runner = self.runner(result=SimpleNamespace(
            outcome="ok", rc=0, stdout=b"", stderr=b"",
            kill_state="unconfirmed", issue=None, identity="lost"))
        outcome = self.execute(runner=runner)
        self.assertEqual(outcome["outcome"], "failed")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "identity-lost")
        self.assertEqual(evidence["payload"]["process"]["kill_state"], "unconfirmed")
        self.assertEqual(self.operation()["audit"]["outcome"]["result"], "failed")


class SignalBeforeReapTests(ExecutorCase):
    """Group signals must never target a recycled group id.

    The reviewer's repro showed the sweep firing after Popen.wait had
    reaped the leader (returncode already 0 at signal time), when the pid
    and its use as the process-group id may already have been reassigned
    to an unrelated process. The regression traces one real successful
    run: every killpg must land while a non-reaping waitid can still
    observe the leader (zombie holding the identity), and the reap must
    come after the signals.
    """

    def test_group_signal_precedes_reap_with_leader_identity_held(self):
        events = []
        real_waitid = os.waitid
        real_killpg = os.killpg
        real_wait = subprocess.Popen.wait

        def traced_waitid(idtype, pid, options):
            events.append(("observe", pid))
            return real_waitid(idtype, pid, options)

        def traced_killpg(pgid, sig):
            held = True
            try:
                # Non-reaping probe: succeeds only while the leader is still
                # an unreaped child, zombie or live.
                real_waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                held = False
            events.append(("signal", pgid, held))
            return real_killpg(pgid, sig)

        def traced_wait(self, timeout=None):
            events.append(("reap", self.pid))
            return real_wait(self, timeout=timeout)

        with mock.patch.object(os, "waitid", side_effect=traced_waitid), \
                mock.patch.object(os, "killpg", side_effect=traced_killpg), \
                mock.patch.object(subprocess.Popen, "wait", autospec=True) as wait_patch:
            wait_patch.side_effect = traced_wait
            outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["process"]["returncode"], 0)
        signals = [event for event in events if event[0] == "signal"]
        reaps = [event for event in events if event[0] == "reap"]
        self.assertTrue(signals, events)
        self.assertTrue(reaps, events)
        for _pgid, held in [(event[1], event[2]) for event in signals]:
            self.assertTrue(held, "group signal fired after the leader was reaped")
        leader = reaps[0][1]
        for event in signals:
            self.assertEqual(event[1], leader)
        first_signal = events.index(signals[0])
        last_signal = events.index(signals[-1])
        first_reap = events.index(reaps[0])
        self.assertLess(first_signal, first_reap, events)
        self.assertLess(last_signal, events.index(reaps[-1]), events)


class LeaderIdentityLostTests(ExecutorCase):
    """The identity-lost backstop, exercised without any real signal.

    When the leader is reaped by anyone other than the executor (kernel
    auto-reap under SIGCHLD=SIG_IGN, a reaping handler, or a concurrent
    thread; the first two are refused pre-consume, the third is
    unsupported), every group signal must be suppressed: the released pid
    may already name an unrelated process group, and Popen.wait under
    ECHILD fabricates return code 0, so no success may be claimed either.
    """

    def test_lost_leader_suppresses_all_signals_and_claims_no_success(self):
        kill_calls = []
        with mock.patch.object(
            os, "killpg", side_effect=lambda pgid, sig: kill_calls.append((pgid, sig))
        ), mock.patch.object(os, "waitid", side_effect=ChildProcessError("no child")):
            run = maven_executor._run(
                [PYTHON, "-c", "print('lost-child')"],
                str(self.project),
                {"PATH": os.environ.get("PATH") or os.defpath},
                timeout_seconds=5,
                max_output_bytes=4096,
            )
        self.assertEqual(run.outcome, "error")
        self.assertEqual(run.identity, "lost")
        self.assertEqual(run.kill_state, "unconfirmed")
        self.assertIsNone(run.rc)
        self.assertIn("identity lost", run.issue)
        # The mocked signal hook proves no signal was ever attempted, so no
        # destructive PID-reuse stress is needed to establish the guarantee.
        self.assertEqual(kill_calls, [])


class RealChildFailureTests(ExecutorCase):
    def test_nonzero_exit_fails_without_output_leakage(self):
        # The secret is assembled at runtime so the contiguous value exists
        # only in the child's output, never in the approved argv text.
        argv = self.child_argv("print('SECRET-' + 'mdi-child-output')\nraise SystemExit(3)\n")
        self.propose(argv=argv, operation_id="op-fail")
        self.operation_id = "op-fail"
        self.approval = self.build_approval()
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "failed")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["category"], "maven-run-failure")
        self.assertEqual(evidence["payload"]["failure"], {"reason": "nonzero-exit", "returncode": 3})
        self.assertIn(SECRET.encode(), outcome["output"])
        self.assertNotIn(SECRET, json.dumps(self.store.load()))

    def test_total_timeout_kills_child_before_it_finishes(self):
        marker = Path(self.base) / "finished-marker"
        code = (
            "import sys, time\n"
            "time.sleep(30)\n"
            "with open(sys.argv[1], 'w') as fh:\n"
            "    fh.write('finished')\n"
        )
        argv = self.child_argv(code, marker)
        self.propose(argv=argv, operation_id="op-slow")
        self.operation_id = "op-slow"
        self.approval = self.build_approval()
        started = time.monotonic()
        outcome = self.execute(timeout_seconds=1)
        elapsed = time.monotonic() - started
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["process"]["outcome"], "timeout")
        self.assertIn(outcome["process"]["kill_state"], ("sent", "absent"))
        self.assertIsInstance(outcome["process"]["returncode"], int)
        self.assertLess(outcome["process"]["returncode"], 0)
        self.assertFalse(marker.exists())
        self.assertLess(elapsed, 15)
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "timeout")
        self.assertEqual(evidence["payload"]["process"]["kill_state"],
                         outcome["process"]["kill_state"])

    def test_output_reads_use_bounded_chunks(self):
        original_read = os.read
        requested = []

        def recording_read(fd, size):
            requested.append(size)
            return original_read(fd, size)

        with mock.patch.object(maven_executor.os, "read", side_effect=recording_read):
            result = maven_executor._run(
                self.child_argv("print('x' * 200000)"), str(self.project),
                {"PATH": os.defpath}, timeout_seconds=10, max_output_bytes=1024 * 1024,
            )
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(len(result.stdout), 200001)
        self.assertTrue(requested)
        self.assertLessEqual(max(requested), 65536)

    def test_output_overflow_capped_while_read(self):
        code = "print('x' * 200000)\n"
        argv = self.child_argv(code)
        self.propose(argv=argv, operation_id="op-loud")
        self.operation_id = "op-loud"
        self.approval = self.build_approval()
        outcome = self.execute(max_output_bytes=4096)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["process"]["outcome"], "capped")
        self.assertEqual(len(outcome["output"]), 4096)
        meta = outcome["output_meta"]
        self.assertTrue(meta["capped"])
        self.assertIsNone(meta["sha256"])
        self.assertEqual(meta["bytes"], len(outcome["output"]))
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "output-cap")
        self.assertEqual(evidence["payload"]["output"]["sha256"], None)
        self.assertEqual(evidence["payload"]["output"]["capped"], True)

    def test_group_kill_reaches_descendants_after_leader_exit(self):
        marker = Path(self.base) / "grandchild-finished"
        pidfile = Path(self.base) / "grandchild-pid"
        grandchild = (
            "import os, sys, time\n"
            "with open(sys.argv[1], 'w') as fh:\n"
            "    fh.write(str(os.getpid()))\n"
            "sys.stdout.write('grandchild started\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
            "with open(sys.argv[2], 'w') as fh:\n"
            "    fh.write('grandchild finished after sleep')\n"
        )
        leader = (
            "import os, subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(grandchild) +
            ", sys.argv[1], sys.argv[2]], env={'PATH': os.environ.get('PATH', '')})\n"
        )
        argv = self.child_argv(leader, pidfile, marker)
        self.propose(argv=argv, operation_id="op-group")
        self.operation_id = "op-group"
        self.approval = self.build_approval()
        started = time.monotonic()
        outcome = self.execute(timeout_seconds=2)
        elapsed = time.monotonic() - started
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["process"]["outcome"], "timeout")
        # The leader itself exited 0; the timeout came from the descendants.
        self.assertEqual(outcome["process"]["returncode"], 0)
        self.assertEqual(outcome["process"]["kill_state"], "sent")
        self.assertFalse(marker.exists())
        self.assertTrue(pidfile.exists())
        self.assertTrue(wait_gone(int(pidfile.read_text().strip())))
        self.assertLess(elapsed, 15)

    def test_devnull_straggler_swept_after_successful_leader_exit(self):
        # A same-group descendant that redirects its own pipes to DEVNULL
        # holds nothing open: the leader exits 0, every pipe closes and the
        # run looks successful while the straggler keeps running. Cleanup
        # must signal the group while holding the leader unreaped. The
        # leader records the straggler's pid, because the sweep can overtake
        # the straggler's own startup.
        marker = Path(self.base) / "straggler-finished"
        pidfile = Path(self.base) / "straggler-pid"
        straggler = (
            "import time\n"
            "time.sleep(30)\n"
            "with open(sys.argv[2], 'w') as fh:\n"
            "    fh.write('straggler outlived the leader')\n"
        )
        leader = (
            "import os, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', " + repr(straggler) +
            ", 'x', sys.argv[2]], stdin=subprocess.DEVNULL,"
            " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
            " env={'PATH': os.environ.get('PATH', '')})\n"
            "with open(sys.argv[1], 'w') as fh:\n"
            "    fh.write(str(child.pid))\n"
        )
        argv = self.child_argv(leader, pidfile, marker)
        self.propose(argv=argv, operation_id="op-sweep")
        self.operation_id = "op-sweep"
        self.approval = self.build_approval()
        outcome = self.execute(timeout_seconds=30)
        # The leader itself exited 0 with all pipes closed: honest success.
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["process"]["returncode"], 0)
        # And the straggler was still swept, not left sleeping behind a
        # successful run.
        self.assertTrue(pidfile.exists())
        self.assertTrue(wait_gone(int(pidfile.read_text().strip())))
        self.assertFalse(marker.exists())

    def test_spawn_failure_after_consume_consumes_consent(self):
        # The launcher exists, is regular and executable, and its digest pins,
        # but its interpreter does not: exec fails only at spawn time, after
        # the consume commit has already spent the single-use approval.
        script = self.synthetic_executable()
        script.write_text("#!/no/such/interpreter\nexit 0\n")
        argv = [str(script), "-q"]
        self.propose(argv=argv, operation_id="op-spawn")
        self.operation_id = "op-spawn"
        self.approval = self.build_approval()
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["process"]["outcome"], "error")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "exec-error")
        record = self.operation()
        self.assertEqual(record["state"], "finished")
        self.assertEqual(record["audit"]["outcome"]["result"], "failed")
        with self.assertRaises(ApprovalError):
            self.execute()


@unittest.skipUnless(JAVA, "the user.home isolation regression needs a real JVM on PATH")
class RealJvmIsolationTests(ExecutorCase):
    """A real JVM proves the mechanism a real Maven launcher relies on.

    OpenJDK resolves user.home from the account database, not from the HOME
    variable (verified: HOME=temp leaves user.home at the real home), so a
    sanitized HOME alone never relocates Maven's ~/.m2/settings.xml or the
    default local repository. These tests run java only; no Maven binary,
    wrapper or network is involved.
    """

    def user_home_from_output(self, outcome):
        text = outcome["output"].decode()
        match = re.search(r"^\s*user\.home = (.+)$", text, re.MULTILINE)
        self.assertIsNotNone(match, text)
        return match.group(1).strip()

    def test_real_jvm_resolves_user_home_inside_control_home(self):
        # Poison the parent value first: the inherited JAVA_TOOL_OPTIONS
        # must be overwritten by the executor's own trusted option, never
        # merged with the hostile one.
        os.environ["JAVA_TOOL_OPTIONS"] = "-javaagent:evil-agent.jar"
        self.addCleanup(os.environ.pop, "JAVA_TOOL_OPTIONS", None)
        argv = [JAVA, "-XshowSettings:properties", "-version"]
        self.propose(argv=argv, operation_id="op-jvm")
        self.operation_id = "op-jvm"
        self.approval = self.build_approval()
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        value = self.user_home_from_output(outcome)
        self.assertNotEqual(value, os.path.expanduser("~"))
        self.assertIn(CONTROL_HOME_PREFIX, value)
        text = outcome["output"].decode()
        self.assertIn("Picked up JAVA_TOOL_OPTIONS: -Duser.home=", text)
        self.assertNotIn("evil", text)

    def test_fake_maven_launcher_resolves_user_home_inside_control_home(self):
        # A shell launcher that execs the JVM from PATH, the way a Maven
        # wrapper script does: the relocation must survive the indirection.
        launcher = Path(self.base) / "fake-mvn"
        launcher.write_text("#!/bin/sh\nexec java -XshowSettings:properties -version\n")
        launcher.chmod(0o755)
        argv = [str(launcher)]
        self.propose(argv=argv, operation_id="op-fake-mvn")
        self.operation_id = "op-fake-mvn"
        self.approval = self.build_approval()
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        value = self.user_home_from_output(outcome)
        self.assertNotEqual(value, os.path.expanduser("~"))
        self.assertIn(CONTROL_HOME_PREFIX, value)

    def test_spaced_control_home_stays_one_jvm_token(self):
        # TemporaryDirectory and mkdtemp obey TMPDIR, and a temporary base
        # with spaces is legal: the injected option must keep the whole path
        # as one literal JVM token instead of splitting into options.
        spaced_root = Path(self.base) / "tmp dir with spaces"
        spaced_root.mkdir()
        self.use_tempdir(spaced_root)
        argv = [JAVA, "-XshowSettings:properties", "-version"]
        self.propose(argv=argv, operation_id="op-space")
        self.operation_id = "op-space"
        self.approval = self.build_approval()
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        text = outcome["output"].decode()
        self.assertNotIn("Unrecognized", text)
        value = self.user_home_from_output(outcome)
        self.assertTrue(value.startswith(str(spaced_root) + "/"), value)
        self.assertIn(CONTROL_HOME_PREFIX, value)
        self.assertNotEqual(value, os.path.expanduser("~"))

    def test_option_shaped_temp_base_cannot_inject_jvm_tokens(self):
        # The temporary base is caller-influenced, so its spelling is not
        # trusted: option-shaped text in the directory name must remain a
        # literal path (had it tokenized, the bogus option would abort the
        # JVM with an Unrecognized error instead of a clean run).
        hostile_root = Path(self.base) / "evil -XX:+NotARealOption"
        hostile_root.mkdir()
        self.use_tempdir(hostile_root)
        argv = [JAVA, "-XshowSettings:properties", "-version"]
        self.propose(argv=argv, operation_id="op-inject")
        self.operation_id = "op-inject"
        self.approval = self.build_approval()
        outcome = self.execute()
        self.assertEqual(outcome["outcome"], "succeeded")
        text = outcome["output"].decode()
        self.assertNotIn("Unrecognized", text)
        value = self.user_home_from_output(outcome)
        self.assertTrue(value.startswith(str(hostile_root) + "/"), value)


if __name__ == "__main__":
    unittest.main()
