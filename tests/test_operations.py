"""Tests for mdi.operations: inert proposals, single-use consent, no executor."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import operations, runstore
from mdi.operations import (
    ApprovalError,
    OperationsError,
    ProposalError,
    StateError,
)
from mdi.runstore import RunConflict

SHA = "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b"
ARGV = ["/usr/bin/mvn", "-f", "/repo/pom.xml", "-q", "dependency:tree", "-DoutputFile=/tmp/tree.json"]
DISCLOSURE = "Run Maven dependency:tree in the inspected repository and import its JSON output."


def maven_scope(argv=None, cwd="/repo"):
    return {"kind": "maven", "argv": list(ARGV) if argv is None else argv, "cwd": cwd}


ADVISORY_QUERY = {
    "ecosystem": "Maven",
    "name": "org.apache.commons:commons-lang3",
    "version": "3.12.0",
}


def advisory_scope(destination="https://api.osv.dev/v1/query", query=None):
    return {
        "kind": "advisory",
        "destination": destination,
        "query": ADVISORY_QUERY if query is None else query,
    }


class StoreCase(unittest.TestCase):
    """A real RunStore in a temporary directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = tmp.name
        self.store = runstore.RunStore.create(self.base, query="commons-lang3:3.12.0")

    def proposal(self, operation_id="op-1", kind="maven", scope=None, inputs=None,
                 tool=None, run_id=None):
        return operations.build_proposal(
            kind,
            scope if scope is not None else maven_scope(),
            [] if inputs is None else inputs,
            tool if tool is not None else {"name": "mvn", "version": "3.9.6"},
            DISCLOSURE,
            self.store.run_id if run_id is None else run_id,
            operation_id,
        )

    def propose(self, **kwargs):
        return operations.propose(self.store, self.proposal(**kwargs), self.revision())

    def propose_advisory(self, operation_id="op-net"):
        return operations.propose(
            self.store,
            self.proposal(
                operation_id=operation_id,
                kind="advisory",
                scope=advisory_scope(),
                tool={"name": "osv-adapter"},
            ),
            self.revision(),
        )

    def revision(self):
        return self.store.load()["revision"]

    def operation(self, operation_id="op-1"):
        for record in self.store.load()["operations"]:
            if record["id"] == operation_id:
                return record
        self.fail(f"operation {operation_id!r} not found")

    def approval(self, operation_id="op-1", approved=True, scope=None, run_id=None, **over):
        record = {
            "run_id": self.store.run_id if run_id is None else run_id,
            "operation_id": operation_id,
            "scope": copy.deepcopy(scope if scope is not None else self.operation(operation_id)["scope"]),
            "approved": approved,
            "approved_by": "host-operator",
            "reference": "host chat 2026-09-12 turn 17",
        }
        record.update(over)
        return record

    def assertNoNewRevision(self, before):
        self.assertEqual(self.store.load()["revision"], before)


class BuildProposalTests(unittest.TestCase):
    def test_proposed_record_shape(self):
        record = operations.build_proposal(
            "maven", maven_scope(), [{"path": "/repo/pom.xml", "sha256": SHA}],
            {"name": "mvn"}, DISCLOSURE, "run-2026-09-12", "op-7",
        )
        self.assertEqual(
            sorted(record),
            ["audit", "disclosure", "evidence_ids", "id", "input_identities",
             "run_id", "scope", "state", "tool_identity"],
        )
        self.assertEqual(record["state"], "proposed")
        self.assertEqual(record["audit"], {})
        self.assertEqual(record["evidence_ids"], [])
        self.assertEqual(record["run_id"], "run-2026-09-12")

    def test_generated_operation_id(self):
        first = operations.build_proposal("maven", maven_scope(), [], {"n": "mvn"}, DISCLOSURE, "r")
        second = operations.build_proposal("maven", maven_scope(), [], {"n": "mvn"}, DISCLOSURE, "r")
        self.assertTrue(first["id"] and isinstance(first["id"], str))
        self.assertNotEqual(first["id"], second["id"])

    def test_inputs_copied_not_aliased(self):
        scope = maven_scope()
        inputs = [{"path": "/repo/pom.xml", "sha256": SHA}]
        record = operations.build_proposal("maven", scope, inputs, {"n": "mvn"}, DISCLOSURE, "r")
        scope["argv"].append("injected")
        inputs[0]["path"] = "mutated"
        self.assertEqual(record["scope"]["argv"], ARGV)
        self.assertEqual(record["input_identities"][0]["path"], "/repo/pom.xml")
        self.assertEqual(scope["argv"], ARGV + ["injected"])

    def test_unknown_kind_rejected(self):
        for kind in ("tree", "", "maven ", None, 1):
            with self.assertRaises(ProposalError):
                operations.build_proposal(kind, maven_scope(), [], {"n": "mvn"}, DISCLOSURE, "r")

    def test_scope_kind_must_match(self):
        with self.assertRaises(ProposalError):
            operations.build_proposal(
                "maven", advisory_scope(), [], {"n": "mvn"}, DISCLOSURE, "r")

    def test_maven_scope_shape(self):
        for scope in (
            {"kind": "maven", "argv": "mvn dependency:tree", "cwd": "/repo"},
            {"kind": "maven", "argv": [], "cwd": "/repo"},
            {"kind": "maven", "argv": ["mvn", ""] , "cwd": "/repo"},
            {"kind": "maven", "argv": ["mvn"], "cwd": "relative/path"},
            {"kind": "maven", "argv": ["mvn"], "cwd": "/repo", "extra": 1},
            {"kind": "maven", "argv": ["mvn\x00bad"], "cwd": "/repo"},
            {"kind": "maven", "argv": ["mvn"], "cwd": ""},
        ):
            with self.assertRaises(ProposalError):
                operations.build_proposal("maven", scope, [], {"n": "mvn"}, DISCLOSURE, "r")

    def test_advisory_scope_shape(self):
        bad_destinations = (
            "http://api.osv.dev/v1/query",
            "https://user@api.osv.dev/v1/query",
            "https://user:pass@api.osv.dev/v1/query",
            "https://api.osv.dev/v1/query?token=abc",
            "https://api.osv.dev/v1/query?",
            "https://api.osv.dev/v1/query#section",
            "https://api.osv.dev/v1/query#",
            "https:///v1/query",
            "not-a-url",
            "",
        )
        for destination in bad_destinations:
            with self.assertRaises(ProposalError):
                operations.build_proposal(
                    "advisory", advisory_scope(destination=destination),
                    [], {"n": "osv"}, DISCLOSURE, "r")

    def test_advisory_query_structured_identity_only(self):
        for query in (
            {"ecosystem": "Maven", "name": "org.apache.commons:commons-lang3"},
            {"ecosystem": "Maven", "name": "a:b", "version": "1.0", "api_key": "k"},
            {"ecosystem": "Maven", "name": "a:b", "version": "1.0", "token": "t"},
            {"ecosystem": " Maven", "name": "a:b", "version": "1.0"},
            {"ecosystem": "Maven", "name": "a:b:c", "version": "1 0"},
            {"ecosystem": "Maven", "name": "${artifactId}", "version": "1.0"},
            {"ecosystem": "Maven", "name": "a:b", "version": "%VERSION%"},
            {"ecosystem": "Maven", "name": "a:b", "version": 10},
            {"ecosystem": "Maven", "name": "a:b", "version": True},
            {"ecosystem": "", "name": "a:b", "version": "1.0"},
        ):
            with self.assertRaises(ProposalError):
                operations.build_proposal(
                    "advisory", advisory_scope(query=query), [], {"n": "osv"}, DISCLOSURE, "r")

    def test_advisory_continuation_token_is_opaque_and_bound(self):
        query = dict(ADVISORY_QUERY, page_token="cursor/+==:v2")  # notsecret
        record = operations.build_proposal(
            "advisory", advisory_scope(query=query), [], {"n": "osv"}, DISCLOSURE, "r")
        self.assertEqual(record["scope"]["query"], query)
        query["page_token"] = "changed"
        self.assertEqual(record["scope"]["query"]["page_token"], "cursor/+==:v2")

    def test_advisory_continuation_token_rejects_invalid_values(self):
        for token in (None, True, 1, [], "", "a b", "a\n", "a\x00b", "x" * 4097):
            with self.subTest(token=repr(token)[:40]), self.assertRaises(ProposalError):
                operations.build_proposal(
                    "advisory", advisory_scope(query=dict(ADVISORY_QUERY, page_token=token)),
                    [], {"n": "osv"}, DISCLOSURE, "r")

    def test_bad_input_identities(self):
        for inputs in (
            [{"path": "pom.xml"}],
            [{"sha256": SHA}],
            [{"path": "pom.xml", "sha256": SHA.upper()}],
            [{"path": "pom.xml", "sha256": SHA[:-1] + "g"}],
            [{"path": "", "sha256": SHA}],
            [{"path": "pom.xml", "sha256": SHA, "extra": 1}],
            ["pom.xml"],
            [{"path": "pom.xml", "sha256": SHA}] * 300,
        ):
            with self.assertRaises(ProposalError):
                operations.build_proposal("maven", maven_scope(), inputs, {"n": "mvn"}, DISCLOSURE, "r")

    def test_bad_tool_identity_disclosure_run_id(self):
        for tool in ([{}], {}, "mvn", None):
            with self.assertRaises(ProposalError):
                operations.build_proposal("maven", maven_scope(), [], tool, DISCLOSURE, "r")
        for disclosure in ("", 7, "x" * 9000):
            with self.assertRaises(ProposalError):
                operations.build_proposal("maven", maven_scope(), [], {"n": "mvn"}, disclosure, "r")
        for run_id in ("", None):
            with self.assertRaises(ProposalError):
                operations.build_proposal("maven", maven_scope(), [], {"n": "mvn"}, DISCLOSURE, run_id)

    def test_bounds(self):
        with self.assertRaises(ProposalError):
            operations.build_proposal(
                "maven", maven_scope(argv=["a"] * 65), [], {"n": "mvn"}, DISCLOSURE, "r")
        with self.assertRaises(ProposalError):
            operations.build_proposal(
                "maven", maven_scope(argv=["x" * 5000]), [], {"n": "mvn"}, DISCLOSURE, "r")

    def test_proposal_validates_as_report_operation(self):
        from mdi.report import validate_report
        record = operations.build_proposal(
            "maven", maven_scope(), [{"path": "/repo/pom.xml", "sha256": SHA}],
            {"name": "mvn"}, DISCLOSURE, "run-x", "op-1")
        report = {
            "schema_version": 1, "run_id": "run-x", "revision": 0, "query": None,
            "selection": None, "subjects": [], "evidence": [], "analyses": [],
            "stages": [], "operations": [record], "qualifications": [],
        }
        validate_report(report)


class ProposeTests(StoreCase):
    def test_persists_proposed(self):
        result = self.propose()
        self.assertEqual(result["revision"], 1)
        stored = self.operation()
        self.assertEqual(stored["state"], "proposed")
        self.assertEqual(stored["scope"], maven_scope())
        self.assertEqual(stored["audit"], {})

    def test_propose_is_draft_safe(self):
        proposal = self.proposal()
        before = copy.deepcopy(proposal)
        operations.propose(self.store, proposal, self.revision())
        self.assertEqual(proposal, before)
        proposal["scope"]["argv"].append("later")
        self.assertEqual(self.operation()["scope"]["argv"], ARGV)

    def test_duplicate_id_refuses(self):
        self.propose()
        before = self.revision()
        with self.assertRaises(OperationsError):
            self.propose(operation_id="op-1")
        self.assertNoNewRevision(before)

    def test_run_id_binding(self):
        with self.assertRaises(OperationsError):
            self.propose(run_id="other-run")
        self.assertNoNewRevision(0)

    def test_stale_expected_revision(self):
        proposal = self.proposal()
        self.store.commit(_bump(self.store), 0)
        with self.assertRaises(RunConflict):
            operations.propose(self.store, proposal, 0)
        self.assertNotIn("op-1", [op["id"] for op in self.store.load()["operations"]])

    def test_credential_scope_keys_never_persist(self):
        for extra in ({"api_key": "k"}, {"token": "t"}, {"payload": {"body": "x"}}):
            scope = advisory_scope()
            scope.update(extra)
            with self.assertRaises(OperationsError):
                operations.propose(self.store, self.proposal(kind="advisory", scope=scope), 0)
        self.assertEqual(self.store.load()["revision"], 0)
        self.assertEqual(self.store.load()["operations"], [])

    def test_structured_advisory_query_persists(self):
        operations.propose(
            self.store, self.proposal(kind="advisory", scope=advisory_scope()), 0)
        stored = self.operation()
        self.assertEqual(stored["scope"]["query"], ADVISORY_QUERY)
        self.assertEqual(stored["state"], "proposed")


class ConsumeTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.propose()
        self.propose(operation_id="op-2")

    def consume(self, operation_id="op-1", approval=None, inputs=None, tool=None, revision=None):
        return operations.consume_approval(
            self.store,
            operation_id,
            approval if approval is not None else self.approval(operation_id),
            self.operation(operation_id)["input_identities"] if inputs is None else inputs,
            self.operation(operation_id)["tool_identity"] if tool is None else tool,
            self.revision() if revision is None else revision,
        )

    def test_consumed_persisted_before_return(self):
        descriptor = self.consume()
        stored = self.operation()
        self.assertEqual(stored["state"], "consumed")
        self.assertEqual(stored["audit"]["started"]["approved_by"], "host-operator")
        self.assertEqual(descriptor["operation_id"], "op-1")
        self.assertEqual(descriptor["run_id"], self.store.run_id)
        self.assertEqual(descriptor["scope"], self.operation()["scope"])

    def test_continuation_approval_requires_exact_token_and_cannot_replay(self):
        query = dict(ADVISORY_QUERY, page_token="cursor/+==")
        self.propose(operation_id="page-2", kind="advisory", scope=advisory_scope(query=query))
        for replacement in (None, "different-cursor"):
            approval = self.approval("page-2")
            if replacement is None:
                del approval["scope"]["query"]["page_token"]
            else:
                approval["scope"]["query"]["page_token"] = replacement
            before = self.revision()
            with self.assertRaises(ApprovalError):
                self.consume("page-2", approval=approval)
            self.assertNoNewRevision(before)
        self.consume("page-2")
        self.assertEqual(self.operation("page-2")["state"], "consumed")
        with self.assertRaises(ApprovalError):
            self.consume("page-2")

    def test_started_audit_bounded_keys(self):
        self.consume()
        started = self.operation()["audit"]["started"]
        self.assertEqual(set(started), {"at", "approved_by", "reference"})
        self.assertNotIn("argv", json.dumps(started))
        self.assertNotIn("payload", json.dumps(started))

    def test_reload_stays_consumed_and_replay_refused(self):
        self.consume()
        reloaded = runstore.RunStore(self.store.path, self.store.run_id).load()
        record = next(op for op in reloaded["operations"] if op["id"] == "op-1")
        self.assertEqual(record["state"], "consumed")
        before = self.revision()
        with self.assertRaises(ApprovalError):
            operations.consume_approval(
                reloaded_store(self.store), "op-1", descriptor_stale_approval(self.store, record),
                record["input_identities"], record["tool_identity"], before)
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation()["state"], "consumed")

    def test_false_bool_and_string_true_refused(self):
        for approved in (False, "true", 1, None):
            before = self.revision()
            with self.assertRaises(ApprovalError):
                self.consume(approval=self.approval(approved=approved))
            self.assertNoNewRevision(before)
            self.assertEqual(self.operation()["state"], "proposed")

    def test_wrong_binding_refused(self):
        for approval in (
            self.approval(run_id="some-other-run"),
            self.approval(operation_id="op-99", scope=maven_scope()),
            self.approval(scope=maven_scope(cwd="/elsewhere")),
            {"run_id": self.store.run_id, "operation_id": "op-1",
             "scope": self.operation()["scope"], "approved": True},
            self.approval(approved_by=""),
            self.approval(reference=""),
        ):
            before = self.revision()
            with self.assertRaises(ApprovalError):
                self.consume(approval=approval)
            self.assertNoNewRevision(before)

    def test_scope_type_sensitivity(self):
        self.propose_advisory()
        approval = self.approval(operation_id="op-net")
        approval["scope"]["query"]["version"] = True
        before = self.revision()
        with self.assertRaises(ApprovalError):
            self.consume(operation_id="op-net", approval=approval)
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation("op-net")["state"], "proposed")

    def test_tool_identity_type_sensitivity(self):
        self.propose(operation_id="op-tool", tool={"name": "osv-adapter", "attempts": 1})
        before = self.revision()
        with self.assertRaises(ApprovalError):
            self.consume(operation_id="op-tool", tool={"name": "osv-adapter", "attempts": True})
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation("op-tool")["state"], "proposed")

    def test_changed_observed_inputs_refused(self):
        changed = [{"path": "/repo/pom.xml", "sha256": "b" * 64}]
        before = self.revision()
        with self.assertRaises(ApprovalError):
            self.consume(inputs=changed)
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation()["state"], "proposed")

    def test_extra_observed_input_refused(self):
        inputs = [{"path": "/repo/pom.xml", "sha256": SHA}, {"path": "/repo/build.gradle", "sha256": SHA}]
        before = self.revision()
        with self.assertRaises(ApprovalError):
            self.consume(inputs=inputs)
        self.assertNoNewRevision(before)

    def test_changed_tool_identity_refused(self):
        before = self.revision()
        with self.assertRaises(ApprovalError):
            self.consume(tool={"name": "mvn", "version": "3.8.1"})
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation()["state"], "proposed")

    def test_sibling_cannot_use_approval(self):
        before = self.revision()
        with self.assertRaises(ApprovalError):
            self.consume(operation_id="op-2", approval=self.approval(operation_id="op-1"))
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation("op-2")["state"], "proposed")
        self.assertEqual(self.operation()["state"], "proposed")

    def test_stale_expected_revision_refused(self):
        before = self.revision()
        with self.assertRaises(RunConflict):
            self.consume(revision=before - 1)
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation()["state"], "proposed")

    def test_unknown_operation_refused(self):
        stored = self.operation()
        with self.assertRaises(OperationsError):
            self.consume(
                operation_id="op-none", approval=self.approval(),
                inputs=stored["input_identities"], tool=stored["tool_identity"])


def _bump(store):
    draft = copy.deepcopy(store.load())
    draft["stages"].append({
        "name": "noop", "outcome": "not_applicable", "coverage": {},
        "reasons": [], "evidence_ids": [],
    })
    return draft


def reloaded_store(store):
    return runstore.RunStore(store.path, store.run_id)


def descriptor_stale_approval(store, record):
    """An approval still bound to a consumed operation, as a replay would carry it."""
    return {
        "run_id": store.run_id,
        "operation_id": record["id"],
        "scope": copy.deepcopy(record["scope"]),
        "approved": True,
        "approved_by": "host-operator",
        "reference": "host chat 2026-09-12 turn 17",
    }


class FinalizeTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.propose()
        operations.consume_approval(
            self.store, "op-1", self.approval(),
            self.operation()["input_identities"], self.operation()["tool_identity"],
            self.revision())

    def finalize(self, outcome="succeeded", audit=None, evidence_ids=None, revision=None, op="op-1"):
        return operations.finalize_operation(
            self.store, op, outcome, audit, evidence_ids,
            expected_revision=self.revision() if revision is None else revision)

    def test_finished_after_success(self):
        result = self.finalize(audit={"exit_code": 0})
        self.assertEqual(result["revision"], 3)
        stored = self.operation()
        self.assertEqual(stored["state"], "finished")
        self.assertEqual(stored["audit"]["outcome"]["result"], "succeeded")
        self.assertEqual(stored["audit"]["exit_code"], 0)

    def test_failure_still_finalizes_and_consumes_approval(self):
        self.finalize(outcome="failed", audit={"error": "spawn refused"})
        stored = self.operation()
        self.assertEqual(stored["state"], "finished")
        self.assertEqual(stored["audit"]["outcome"]["result"], "failed")
        before = self.revision()
        with self.assertRaises(ApprovalError):
            operations.consume_approval(
                self.store, "op-1", descriptor_stale_approval(self.store, stored),
                stored["input_identities"], stored["tool_identity"], before)
        self.assertNoNewRevision(before)

    def test_started_audit_preserved(self):
        started_before = copy.deepcopy(self.operation()["audit"]["started"])
        self.finalize(audit={"exit_code": 1})
        self.assertEqual(self.operation()["audit"]["started"], started_before)

    def test_audit_cannot_rewrite_reserved_keys(self):
        before = self.revision()
        with self.assertRaises(OperationsError):
            self.finalize(audit={"started": {"at": "forged"}})
        with self.assertRaises(OperationsError):
            self.finalize(audit={"outcome": "overwritten"})
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation()["state"], "consumed")

    def test_only_consumed_moves_to_finished(self):
        self.propose(operation_id="op-2")
        before = self.revision()
        with self.assertRaises(StateError):
            self.finalize(op="op-2")
        with self.assertRaises(StateError):
            self.finalize(op="op-none")
        self.assertNoNewRevision(before)

    def test_finished_is_terminal(self):
        self.finalize()
        before = self.revision()
        with self.assertRaises(StateError):
            self.finalize()
        self.assertNoNewRevision(before)

    def test_crash_before_finalize_leaves_consumed(self):
        reloaded = reloaded_store(self.store).load()
        record = next(op for op in reloaded["operations"] if op["id"] == "op-1")
        self.assertEqual(record["state"], "consumed")

    def test_evidence_refs_validated(self):
        before = self.revision()
        with self.assertRaises(OperationsError):
            self.finalize(evidence_ids=["E-nope"])
        self.assertNoNewRevision(before)

    def test_evidence_refs_accepted_when_present(self):
        self.store.commit(_evidence_draft(self.store), self.revision())
        self.finalize(evidence_ids=["E1"])
        self.assertEqual(self.operation()["evidence_ids"], ["E1"])

    def test_bad_outcome_rejected(self):
        before = self.revision()
        for outcome in ("", "crashed", None, True):
            with self.assertRaises(OperationsError):
                self.finalize(outcome=outcome)
        self.assertNoNewRevision(before)

    def test_stale_revision_refused(self):
        before = self.revision()
        with self.assertRaises(RunConflict):
            self.finalize(revision=before - 1)
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation()["state"], "consumed")


class DenyTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.propose()

    def test_deny_persists_and_is_terminal(self):
        result = operations.deny_proposal(
            self.store, "op-1", "host declined network access", self.revision())
        self.assertEqual(result["revision"], 2)
        stored = self.operation()
        self.assertEqual(stored["state"], "denied")
        self.assertEqual(stored["audit"]["denied"]["reason"], "host declined network access")
        reloaded = reloaded_store(self.store).load()
        self.assertEqual(reloaded["operations"][0]["state"], "denied")

    def test_denied_cannot_be_consumed(self):
        operations.deny_proposal(self.store, "op-1", "no", self.revision())
        before = self.revision()
        with self.assertRaises(ApprovalError):
            operations.consume_approval(
                self.store, "op-1", self.approval(),
                self.operation()["input_identities"], self.operation()["tool_identity"], before)
        self.assertNoNewRevision(before)
        self.assertEqual(self.operation()["state"], "denied")

    def test_deny_only_from_proposed(self):
        operations.deny_proposal(self.store, "op-1", "no", self.revision())
        before = self.revision()
        with self.assertRaises(StateError):
            operations.deny_proposal(self.store, "op-1", "again", before)
        self.assertNoNewRevision(before)

    def test_deny_unknown_or_bad_reason(self):
        before = self.revision()
        with self.assertRaises(OperationsError):
            operations.deny_proposal(self.store, "op-none", "no", before)
        with self.assertRaises(OperationsError):
            operations.deny_proposal(self.store, "op-1", "", before)
        self.assertNoNewRevision(before)


class NoSideEffectsTests(StoreCase):
    def test_full_cycle_makes_no_subprocess_or_network_calls(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("operations must not execute processes or touch the network")

        patches = [
            ("subprocess.Popen", forbidden), ("subprocess.run", forbidden),
            ("subprocess.call", forbidden), ("subprocess.check_output", forbidden),
            ("socket.create_connection", forbidden), ("socket.socket", forbidden),
            ("urllib.request.urlopen", forbidden),
        ]
        for target, repl in patches:
            patcher = mock.patch(target, side_effect=repl)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.propose()
        operations.consume_approval(
            self.store, "op-1", self.approval(),
            self.operation()["input_identities"], self.operation()["tool_identity"],
            self.revision())
        operations.finalize_operation(
            self.store, "op-1", "succeeded", None, None, expected_revision=self.revision())
        self.assertEqual(self.operation()["state"], "finished")

    def test_module_does_not_import_executors(self):
        import mdi.operations as ops_module
        source = Path(ops_module.__file__).read_text()
        for banned in ("import subprocess", "import socket", "urllib.request", "os.system", "popen"):
            self.assertNotIn(banned, source.lower())


def _evidence_draft(store):
    draft = copy.deepcopy(store.load())
    draft["evidence"].append({
        "id": "E1", "category": "tree-json",
        "source_locator": "file:///tmp/tree.json", "observed_at": "2026-09-12T00:00:00Z",
        "payload": {"root": "org.apache.commons"},
    })
    return draft


if __name__ == "__main__":
    unittest.main()
