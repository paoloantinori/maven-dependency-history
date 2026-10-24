"""Tests for mdi.advisory_executor: consent-gated one-page OSV execution.

Every network-facing test injects a transport; the default (the total-
deadline DeadlineTransport from mdi.advisory_transport) is exercised only
through a faked constructor and a synthetic slow child script, and the
urllib transport is injected explicitly against a faked opener, with
socket construction patched out, so no test ever touches the network. The
page fixture is a minimal factual extract of the saved 2026-09-10 OSV
probe response for org.apache.logging.log4j:log4j-core
(GHSA-jfh8-c2jp-5v3q, CVE-2021-44228): one affected entry of the real
page, copied verbatim.
"""

import copy
import gc
import hashlib
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import advisory_executor, advisory_transport, operations, runstore
from mdi.advisories import AdvisoryLimits, OSV_QUERY_ENDPOINT
from mdi.advisory_executor import (
    TOOL_IDENTITY,
    AdvisoryExecutionError,
    advisory_proposal,
    execute_advisory,
)
from mdi.advisory_transport import WORKER_FLAG
from mdi.operations import ApprovalError
from mdi.runstore import RunConflict

GROUP = "org.apache.logging.log4j"
ARTIFACT = "log4j-core"
VERSION = "2.14.1"
COORDINATE = f"{GROUP}:{ARTIFACT}"

CANONICAL_BODY = json.dumps(
    {"package": {"ecosystem": "Maven", "name": COORDINATE}, "version": VERSION},
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")

ADVISORY_URL = (
    "https://github.com/github/advisory-database/blob/main/advisories/github-reviewed/"
    "2021/12/GHSA-jfh8-c2jp-5v3q/GHSA-jfh8-c2jp-5v3q.json"
)

# Factual extract of the real OSV page: one branch interval and its
# version list, plus the database_specific source, copied verbatim.
PAGE = {
    "vulns": [
        {
            "id": "GHSA-jfh8-c2jp-5v3q",
            "aliases": ["CVE-2021-44228"],
            "published": "2021-12-10T00:40:56Z",
            "summary": "Remote code injection in Log4j",
            "severity": [
                {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H/E:H"}
            ],
            "references": [
                {"type": "ADVISORY", "url": "https://nvd.nist.gov/vuln/detail/CVE-2021-44228"},
            ],
            "affected": [
                {
                    "package": {
                        "name": COORDINATE,
                        "ecosystem": "Maven",
                        "purl": "pkg:maven/org.apache.logging.log4j/log4j-core",
                    },
                    "ranges": [
                        {"type": "ECOSYSTEM", "events": [{"introduced": "2.13.0"}, {"fixed": "2.15.0"}]}
                    ],
                    "versions": ["2.13.0", "2.13.1", "2.14.0", "2.14.1"],
                    "database_specific": {"source": ADVISORY_URL},
                },
                {
                    "package": {"name": "com.guicedee.services:log4j-core", "ecosystem": "Maven"},
                    "versions": ["1.0.10.0"],
                },
            ],
        }
    ]
}
PAGE_BYTES = json.dumps(PAGE).encode("utf-8")
EMPTY_PAGE_BYTES = b"{}"

# Synthetic slow child for the deadline integration test: records one
# spawn, reads the run store's latest report revision from disk itself and
# writes back the state it saw the operation in, then sleeps far beyond
# any deadline under test. The child sees the durable store exactly as a
# real worker would, so the test can prove the consent commit landed
# before the child ever ran. argv: [WORKER_FLAG, store_dir, operation_id,
# state_path, spawn_path]. No network, no Maven, no real HTTP anywhere.
SLOW_CHILD_SOURCE = """\
import json, os, sys, time
store_dir, operation_id, state_path, spawn_path = sys.argv[2:6]
with open(spawn_path, "a") as handle:
    handle.write("spawn\\n")
revisions = sorted(
    name for name in os.listdir(store_dir)
    if name.startswith("report-") and name.endswith(".json")
)
with open(os.path.join(store_dir, revisions[-1])) as handle:
    report = json.load(handle)
state = "missing"
for record in report["operations"]:
    if record["id"] == operation_id:
        state = record["state"]
with open(state_path, "w") as handle:
    handle.write(state)
time.sleep(30)
"""


class ClosableFP:
    """Minimal response fp recording closes; stands in for a real response.

    urllib's HTTPError wraps its fp in tempfile machinery whose deallocator
    emits "ResourceWarning: Implicitly cleaning up" unless close() ran, so
    every HTTPError this suite raises is closed by whoever owns it.
    """

    def __init__(self):
        self.closed = False
        self.close_count = 0

    def close(self):
        self.close_count += 1
        self.closed = True


def resource_warnings(caught):
    return [w for w in caught if issubclass(w.category, ResourceWarning)]


class FakeTransport:
    """In-memory transport recording every call; optionally inspects the store."""

    def __init__(self, pages=None, exc=None, store=None):
        self.pages = list(pages or [])
        self.exc = exc
        self.store = store
        self.calls = []

    def post(self, destination, body, timeout_seconds, response_cap_bytes):
        entry = {
            "destination": destination,
            "body": body,
            "timeout_seconds": timeout_seconds,
            "response_cap_bytes": response_cap_bytes,
        }
        if self.store is not None:
            report = self.store.load()
            entry["state_when_called"] = self._operation_state(report)
            entry["revision_when_called"] = report["revision"]
        self.calls.append(entry)
        if self.exc is not None:
            raise self.exc
        if self.pages:
            return self.pages.pop(0)
        return EMPTY_PAGE_BYTES

    def _operation_state(self, report):
        operation_id = self.operation_id
        for record in report["operations"]:
            if record["id"] == operation_id:
                return record["state"]
        return None


class ExecutorCase(unittest.TestCase):
    """A real RunStore with one proposed advisory operation and its approval."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = tmp.name
        self.store = runstore.RunStore.create(self.base, query=f"{COORDINATE}:{VERSION}")
        self.operation_id = self.propose_advisory()
        self.approval = self.build_approval()

    def propose_advisory(self, operation_id=None, record=None):
        if record is None:
            record = advisory_proposal(self.store, GROUP, ARTIFACT, VERSION, operation_id)
        operations.propose(self.store, record, self.revision())
        return record["id"]

    def build_approval(self, operation_id=None, **over):
        record = {
            "run_id": self.store.run_id,
            "operation_id": operation_id or self.operation_id,
            "scope": copy.deepcopy(self.operation(operation_id or self.operation_id)["scope"]),
            "approved": True,
            "approved_by": "host-operator",
            "reference": "host chat 2026-09-12 turn 31",
        }
        record.update(over)
        return record

    def execute(self, transport, **over):
        kwargs = {
            "expected_revision": self.revision(),
            "transport": transport,
        }
        kwargs.update(over)
        return execute_advisory(self.store, self.operation_id, self.approval, **kwargs)

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
        return [entry for entry in report["evidence"]]


class ContinuationExecutionTests(ExecutorCase):
    def prepare_continuation(self):
        record = advisory_proposal(
            self.store, GROUP, ARTIFACT, VERSION, page_token="cursor/+==")
        self.operation_id = self.propose_advisory(record=record)
        self.approval = self.build_approval()

    def test_continuation_payload_and_evidence_preserve_exact_token(self):
        self.prepare_continuation()
        transport = FakeTransport(pages=[EMPTY_PAGE_BYTES])
        outcome = self.execute(transport)
        body = json.loads(transport.calls[0]["body"])
        self.assertEqual(body["page_token"], "cursor/+==")
        self.assertEqual(outcome["result"]["query"], body)
        self.assertTrue(outcome["result"]["is_continuation"])
        self.assertEqual(outcome["result"]["page_scope"], "single-response-page")
        self.assertEqual(len(transport.calls), 1)

    def test_continuation_failure_preserves_requested_token(self):
        self.prepare_continuation()
        outcome = self.execute(FakeTransport(exc=TimeoutError("timed out")))
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["result"]["query"]["page_token"], "cursor/+==")
        self.assertTrue(outcome["result"]["is_continuation"])
        self.assertIsNone(outcome["result"]["more_pages"])

    def test_continuation_requires_matching_approval_before_transport(self):
        self.prepare_continuation()
        del self.approval["scope"]["query"]["page_token"]
        transport = FakeTransport()
        before = self.revision()
        with self.assertRaises(ApprovalError):
            self.execute(transport)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.revision(), before)


class SuccessfulExecutionTests(ExecutorCase):
    def test_transport_sees_durable_consumed_state_and_exact_request(self):
        transport = FakeTransport(pages=[PAGE_BYTES], store=self.store)
        transport.operation_id = self.operation_id
        before = self.revision()
        outcome = self.execute(transport)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["destination"], OSV_QUERY_ENDPOINT)
        self.assertEqual(call["body"], CANONICAL_BODY)
        self.assertEqual(call["timeout_seconds"], 20)
        self.assertEqual(call["response_cap_bytes"], AdvisoryLimits().response_bytes)
        # The consumption commit is durable before any external action.
        self.assertEqual(call["state_when_called"], "consumed")
        self.assertEqual(call["revision_when_called"], before + 1)
        self.assertEqual(outcome["outcome"], "succeeded")

    def test_operation_finished_and_evidence_recorded(self):
        transport = FakeTransport(pages=[PAGE_BYTES])
        outcome = self.execute(transport)
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(self.revision(), outcome["revision"])
        record = self.operation()
        self.assertEqual(record["state"], "finished")
        self.assertEqual(record["audit"]["outcome"]["result"], "succeeded")
        self.assertEqual(record["evidence_ids"], [outcome["evidence_id"]])
        self.assertEqual(len(outcome["report"]["evidence"]), 1)
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["id"], outcome["evidence_id"])
        self.assertEqual(evidence["source_locator"], OSV_QUERY_ENDPOINT)
        self.assertEqual(evidence["category"], "advisory-page")
        payload = evidence["payload"]
        self.assertEqual(
            payload["request"],
            {
                "url": OSV_QUERY_ENDPOINT,
                "body": {"package": {"ecosystem": "Maven", "name": COORDINATE}, "version": VERSION},
                "body_sha256": hashlib.sha256(CANONICAL_BODY).hexdigest(),
                "body_bytes": len(CANONICAL_BODY),
            },
        )
        self.assertEqual(
            payload["response"],
            {
                "sha256": hashlib.sha256(PAGE_BYTES).hexdigest(),
                "bytes": len(PAGE_BYTES),
            },
        )

    def test_mapped_result_preserves_range_fields(self):
        transport = FakeTransport(pages=[PAGE_BYTES])
        outcome = self.execute(transport)
        result = outcome["result"]
        self.assertEqual(result["status"], "matches")
        self.assertEqual(result["source_url"], OSV_QUERY_ENDPOINT)
        advisory = result["advisories"][0]
        self.assertEqual(advisory["id"], "GHSA-jfh8-c2jp-5v3q")
        self.assertEqual(advisory["aliases"], ["CVE-2021-44228"])
        affected = advisory["affected"]
        self.assertEqual(len(affected), 1)
        self.assertEqual(
            affected[0]["ranges"],
            [{"type": "ECOSYSTEM", "events": [{"introduced": "2.13.0"}, {"fixed": "2.15.0"}]}],
        )
        self.assertEqual(affected[0]["versions"], ["2.13.0", "2.13.1", "2.14.0", "2.14.1"])
        self.assertEqual(affected[0]["database_specific"], {"source": ADVISORY_URL})
        self.assertEqual(advisory["excluded_affected_count"], 1)

    def test_empty_page_reports_no_known_matches(self):
        transport = FakeTransport(pages=[EMPTY_PAGE_BYTES])
        outcome = self.execute(transport)
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["result"]["status"], "no-known-matches")

    def test_partial_page_returns_token_without_followup(self):
        page = json.dumps({"vulns": [], "next_page_token": "continuation-7"}).encode("utf-8")
        transport = FakeTransport(pages=[page])
        outcome = self.execute(transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["result"]["status"], "partial")
        self.assertEqual(outcome["result"]["more_pages"], True)
        self.assertEqual(outcome["next_page_token"], "continuation-7")
        record = self.operation()
        self.assertEqual(record["state"], "finished")


class RefusedBeforeConsumeTests(ExecutorCase):
    """Executor policy violations never commit and never reach a transport."""

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport(pages=[PAGE_BYTES])

    def propose_raw(self, scope, inputs=(), tool=None, operation_id="op-raw"):
        record = operations.build_proposal(
            "advisory",
            scope,
            list(inputs),
            TOOL_IDENTITY if tool is None else tool,
            "disclosure",
            self.store.run_id,
            operation_id,
        )
        operations.propose(self.store, record, self.revision())
        return record["id"]

    def refuse(self, operation_id, approval=None, **over):
        kwargs = {"expected_revision": self.revision(), "transport": self.transport}
        kwargs.update(over)
        with self.assertRaises(AdvisoryExecutionError):
            execute_advisory(
                self.store,
                operation_id,
                approval if approval is not None else self.approval,
                **kwargs,
            )
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.operation(operation_id)["state"], "proposed")

    def query_scope(self, **over):
        query = {"ecosystem": "Maven", "name": COORDINATE, "version": VERSION}
        query.update(over)
        return {"kind": "advisory", "destination": OSV_QUERY_ENDPOINT, "query": query}

    def test_port_alias_destination_refused(self):
        scope = self.query_scope()
        scope["destination"] = "https://api.osv.dev:443/v1/query"
        operation_id = self.propose_raw(scope)
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_other_destination_refused(self):
        scope = self.query_scope()
        scope["destination"] = "https://osv-mirror.example/v1/query"
        operation_id = self.propose_raw(scope)
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_wrong_ecosystem_refused(self):
        operation_id = self.propose_raw(self.query_scope(ecosystem="PyPI"))
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_name_without_exactly_one_colon_refused(self):
        for index, name in enumerate(("log4j-core", "a:b:c")):
            operation_id = self.propose_raw(self.query_scope(name=name), operation_id=f"op-raw-{index}")
            self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_local_input_identities_refused(self):
        sha = "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b"
        operation_id = self.propose_raw(
            self.query_scope(), inputs=[{"path": "/repo/pom.xml", "sha256": sha}]
        )
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_foreign_tool_identity_refused(self):
        operation_id = self.propose_raw(self.query_scope(), tool={"name": "other-adapter"})
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_modified_transport_identity_refused(self):
        # The proposal binds the transport's own source identity: when the
        # worker module changes, proposals naming the old transport are
        # refused rather than run against a transport the host never saw.
        tool = copy.deepcopy(TOOL_IDENTITY)
        tool["transport"]["source_sha256"] = "0" * 64
        operation_id = self.propose_raw(self.query_scope(), tool=tool)
        self.refuse(operation_id, approval=self.build_approval(operation_id))

    def test_maven_operation_refused(self):
        record = operations.build_proposal(
            "maven",
            {"kind": "maven", "argv": ["/usr/bin/mvn", "-q"], "cwd": "/repo"},
            [],
            {"name": "mvn"},
            "disclosure",
            self.store.run_id,
            "op-maven",
        )
        operations.propose(self.store, record, self.revision())
        self.refuse("op-maven", approval=self.build_approval("op-maven"))

    def test_bad_timeout_refused_before_consume(self):
        for bad in (0, -1, "20", True, None, float("nan"), float("inf"), float("-inf")):
            self.refuse(self.operation_id, timeout_seconds=bad)

    def test_transport_without_post_refused_before_consume(self):
        self.refuse(self.operation_id, transport=object())


class ApprovalRefusalTests(ExecutorCase):
    def test_altered_approval_scope_rejects_before_transport(self):
        self.approval["scope"]["query"]["version"] = "2.15.0"
        transport = FakeTransport(pages=[PAGE_BYTES])
        before = self.revision()
        with self.assertRaises(ApprovalError):
            self.execute(transport)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.revision(), before)
        self.assertEqual(self.operation()["state"], "proposed")

    def test_sibling_approval_rejects_before_transport(self):
        other_id = self.propose_advisory(operation_id="op-sibling")
        self.approval["operation_id"] = other_id
        self.approval["scope"] = copy.deepcopy(self.operation("op-sibling")["scope"])
        transport = FakeTransport(pages=[PAGE_BYTES])
        with self.assertRaises(ApprovalError):
            self.execute(transport)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.operation()["state"], "proposed")
        self.assertEqual(self.operation(other_id)["state"], "proposed")

    def test_stale_revision_conflicts_without_consuming(self):
        transport = FakeTransport(pages=[PAGE_BYTES])
        with self.assertRaises(RunConflict):
            self.execute(transport, expected_revision=self.revision() - 1)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.operation()["state"], "proposed")


class FailureRecordingTests(ExecutorCase):
    def test_concurrent_revision_after_consume_refuses_evidence_commit(self):
        store = self.store

        class ConcurrentTransport:
            def post(inner, *args):
                report = store.load()
                store.commit(report, report["revision"])
                return EMPTY_PAGE_BYTES

        with self.assertRaises(RunConflict):
            self.execute(ConcurrentTransport())
        self.assertEqual(self.operation()["state"], "consumed")
        self.assertEqual(self.evidence_records(), [])

    def test_transport_failure_records_failure_not_no_matches(self):
        transport = FakeTransport(exc=OSError("connection reset"))
        outcome = self.execute(transport)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["result"]["status"], "transport-error")
        self.assertEqual(outcome["result"]["error_type"], "OSError")
        self.assertEqual(outcome["next_page_token"], None)
        record = self.operation()
        self.assertEqual(record["state"], "finished")
        self.assertEqual(record["audit"]["outcome"]["result"], "failed")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["category"], "advisory-transport-failure")
        self.assertIsNone(evidence["payload"]["response"]["sha256"])
        self.assertEqual(evidence["payload"]["failure"]["error_type"], "OSError")

    def test_caught_http_error_response_closed_by_executor(self):
        fp = ClosableFP()
        error = urllib.error.HTTPError(OSV_QUERY_ENDPOINT, 503, "Service Unavailable", None, fp)
        transport = FakeTransport(exc=error)
        outcome = self.execute(transport)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["result"]["error_type"], "HTTPError")
        self.assertTrue(fp.closed)
        record = self.operation()
        self.assertEqual(record["state"], "finished")
        self.assertEqual(record["audit"]["outcome"]["result"], "failed")

    def test_http_error_close_is_idempotent(self):
        # The default transport closes an opener-raised HTTPError and the
        # executor closes it again on its failure path; the closer's
        # close_called guard must keep that to exactly one fp close.
        fp = ClosableFP()
        error = urllib.error.HTTPError(OSV_QUERY_ENDPOINT, 500, "boom", None, fp)
        error.close()
        error.close()
        self.assertEqual(fp.close_count, 1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            del error
            gc.collect()
        self.assertEqual(resource_warnings(caught), [])

    def test_failed_execution_cannot_replay(self):
        transport = FakeTransport(exc=OSError("connection reset"))
        self.execute(transport)
        with self.assertRaises(ApprovalError):
            self.execute(transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.operation()["state"], "finished")

    def test_invalid_json_records_failure_with_raw_hash(self):
        transport = FakeTransport(pages=[b"not json {"])
        outcome = self.execute(transport)
        self.assertEqual(outcome["outcome"], "failed")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "malformed-json")
        self.assertEqual(
            evidence["payload"]["response"]["sha256"], hashlib.sha256(b"not json {").hexdigest()
        )

    def test_page_for_other_package_records_failure(self):
        page = json.dumps({"vulns": [{"id": "GHSA-other", "affected": [
            {"package": {"name": "com.example:other", "ecosystem": "Maven"}}
        ]}]}).encode("utf-8")
        transport = FakeTransport(pages=[page])
        outcome = self.execute(transport)
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["result"]["status"], "transport-error")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "identity-not-in-advisory")

    def test_oversized_response_records_failure(self):
        limits = AdvisoryLimits(response_bytes=64)
        transport = FakeTransport(pages=[b"x" * 65])
        outcome = self.execute(transport, limits=limits)
        self.assertEqual(outcome["outcome"], "failed")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["payload"]["failure"]["reason"], "response-too-large")

    def test_http_error_provider_text_and_headers_never_persisted(self):
        # urllib's HTTPError spells str() as "HTTP Error <code>: <msg>"
        # with the provider's message verbatim, and its headers may carry
        # a Location with credentials; neither may reach the report.
        fp = ClosableFP()
        error = urllib.error.HTTPError(
            OSV_QUERY_ENDPOINT,
            503,
            "TOPSECRET",
            {"Location": "https://user:sekrit@evil.internal/v1/query?token=T"},
            fp,
        )
        outcome = self.execute(FakeTransport(exc=error))
        self.assertEqual(outcome["outcome"], "failed")
        report_text = json.dumps(self.store.load(), sort_keys=True)
        outcome_text = json.dumps(outcome, sort_keys=True)
        for text in (report_text, outcome_text):
            self.assertNotIn("TOPSECRET", text)
            self.assertNotIn("sekrit", text)
            self.assertNotIn("evil.internal", text)
        self.assertIn(OSV_QUERY_ENDPOINT, report_text)
        # Only the stable class name, the numeric status and executor-
        # authored text are recorded, on both the result and the evidence.
        failure = self.evidence_records()[0]["payload"]["failure"]
        self.assertEqual(failure["error_type"], "HTTPError")
        self.assertEqual(failure["status_code"], 503)
        self.assertEqual(outcome["result"]["error_type"], "HTTPError")
        self.assertEqual(outcome["result"]["status"], "transport-error")
        self.assertTrue(fp.closed)

    def test_url_error_reason_never_persisted(self):
        # A URLError str() is its reason verbatim; the reason can be a
        # string or a nested exception carrying the endpoint's URL with
        # userinfo credentials. Neither form may reach the report.
        reasons = (
            "connection to https://user:secret@evil.internal/v1/query failed",
            OSError("dial sekrit detail from https://evil.internal/x"),
        )
        for reason in reasons:
            with self.subTest(reason=type(reason).__name__):
                self.operation_id = self.propose_advisory()
                self.approval = self.build_approval()
                outcome = self.execute(FakeTransport(exc=urllib.error.URLError(reason)))
                self.assertEqual(outcome["outcome"], "failed")
                report_text = json.dumps(self.store.load(), sort_keys=True)
                outcome_text = json.dumps(outcome, sort_keys=True)
                for text in (report_text, outcome_text):
                    self.assertNotIn("user:secret", text)
                    self.assertNotIn("sekrit", text)
                    self.assertNotIn("evil.internal", text)
                failure = self.evidence_records()[-1]["payload"]["failure"]
                self.assertEqual(failure["error_type"], "URLError")
                self.assertIsNone(failure["reason"])
                self.assertEqual(outcome["result"]["error_type"], "URLError")

    def test_hostile_duplicate_key_snippet_never_persisted(self):
        # The mapper's duplicate-key refusal message quotes the hostile
        # key verbatim; the stable code, never that message, is recorded.
        transport = FakeTransport(pages=[b'{"TOPSECRET": 1, "TOPSECRET": 2}'])
        outcome = self.execute(transport)
        self.assertEqual(outcome["outcome"], "failed")
        failure = self.evidence_records()[0]["payload"]["failure"]
        self.assertEqual(failure["reason"], "duplicate-key")
        report_text = json.dumps(self.store.load(), sort_keys=True)
        outcome_text = json.dumps(outcome, sort_keys=True)
        for text in (report_text, outcome_text):
            self.assertNotIn("TOPSECRET", text)

    def test_hostile_unexpected_key_snippet_never_persisted(self):
        page = json.dumps({"vulns": [], "TOPSECRET": "x"}).encode("utf-8")
        transport = FakeTransport(pages=[page])
        outcome = self.execute(transport)
        self.assertEqual(outcome["outcome"], "failed")
        failure = self.evidence_records()[0]["payload"]["failure"]
        self.assertEqual(failure["reason"], "unexpected-key")
        report_text = json.dumps(self.store.load(), sort_keys=True)
        outcome_text = json.dumps(outcome, sort_keys=True)
        for text in (report_text, outcome_text):
            self.assertNotIn("TOPSECRET", text)

    def test_keyboard_interrupt_leaves_operation_consumed(self):
        transport = FakeTransport(exc=KeyboardInterrupt("operator stop"))
        before = self.revision()
        with self.assertRaises(KeyboardInterrupt):
            self.execute(transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.revision(), before + 1)
        record = self.operation()
        self.assertEqual(record["state"], "consumed")
        self.assertEqual(self.evidence_records(), [])
        with self.assertRaises(ApprovalError):
            self.execute(FakeTransport(pages=[PAGE_BYTES]))


class ProposalFactoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = runstore.RunStore.create(tmp.name, query=f"{COORDINATE}:{VERSION}")

    def test_proposal_shape_binds_tool_identity(self):
        record = advisory_proposal(self.store, GROUP, ARTIFACT, VERSION, "op-factory")
        self.assertEqual(record["state"], "proposed")
        self.assertEqual(record["run_id"], self.store.run_id)
        self.assertEqual(
            record["scope"],
            {
                "kind": "advisory",
                "destination": OSV_QUERY_ENDPOINT,
                "query": {"ecosystem": "Maven", "name": COORDINATE, "version": VERSION},
            },
        )
        self.assertEqual(record["input_identities"], [])
        self.assertEqual(record["tool_identity"], TOOL_IDENTITY)

    def test_accepts_run_id_string_and_generates_ids(self):
        first = advisory_proposal("run-x", GROUP, ARTIFACT, VERSION)
        second = advisory_proposal("run-x", GROUP, ARTIFACT, VERSION)
        self.assertEqual(first["run_id"], "run-x")
        self.assertNotEqual(first["id"], second["id"])

    def test_tool_identity_carries_source_digest(self):
        import mdi.advisory_executor as module

        digest = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        self.assertEqual(TOOL_IDENTITY["source_sha256"], digest)

    def test_tool_identity_binds_transport_source(self):
        # The executor identity carries the transport identity verbatim,
        # so editing the transport module invalidates every old proposal.
        self.assertEqual(TOOL_IDENTITY["transport"], advisory_transport.TOOL_IDENTITY)
        digest = hashlib.sha256(
            Path(advisory_transport.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(TOOL_IDENTITY["transport"]["source_sha256"], digest)
        self.assertEqual(TOOL_IDENTITY["transport"]["name"], "mdi.advisory_transport")

    def test_proposal_is_acceptable_to_operations(self):
        record = advisory_proposal(self.store, GROUP, ARTIFACT, VERSION)
        persisted = operations.propose(self.store, record, self.store.load()["revision"])
        self.assertEqual(persisted["operations"][0]["id"], record["id"])

    def test_blank_coordinate_rejected(self):
        with self.assertRaises(ValueError):
            advisory_proposal(self.store, GROUP, "", VERSION)


class FakeResponse:
    """Response stand-in accounting bytes read and closes, like urllib's."""

    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers if headers is not None else {}
        self.read_total = 0
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        if size is None or size < 0:
            data, self._body = self._body, b""
        else:
            data, self._body = self._body[:size], self._body[size:]
        self.read_total += len(data)
        return data

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    def __init__(self, status=200, body=EMPTY_PAGE_BYTES, exc=None, headers=None):
        self.status = status
        self.body = body
        self.exc = exc
        self.headers = headers if headers is not None else {}
        self.calls = []
        self.last_response = None

    def open(self, request, timeout=None):
        self.calls.append({"request": request, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        self.last_response = FakeResponse(self.status, self.body, dict(self.headers))
        return self.last_response


class DefaultSelectionTests(ExecutorCase):
    """With no injected transport, the executor selects DeadlineTransport.

    The selection is observed through a faked constructor, so the test
    proves only which class the executor builds and that the resulting
    request is the canonical one-page query; sockets are patched out, so
    not even an accidental network attempt is possible.
    """

    def test_no_transport_constructs_deadline_transport_once(self):
        created = []

        class RecordingDeadline:
            def __init__(self):
                created.append(self)

            def post(self, destination, body, timeout_seconds, response_cap_bytes):
                self.request = (destination, body, timeout_seconds, response_cap_bytes)
                return EMPTY_PAGE_BYTES

        guard = mock.patch("socket.socket")
        guard.start()
        self.addCleanup(guard.stop)
        selection = mock.patch.object(advisory_executor, "DeadlineTransport", RecordingDeadline)
        selection.start()
        self.addCleanup(selection.stop)
        outcome = execute_advisory(
            self.store, self.operation_id, self.approval,
            expected_revision=self.revision(),
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0].request,
            (OSV_QUERY_ENDPOINT, CANONICAL_BODY, 20, AdvisoryLimits().response_bytes),
        )
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["result"]["status"], "no-known-matches")
        record = self.operation()
        self.assertEqual(record["state"], "finished")


class DeadlineWorkerIntegrationTests(ExecutorCase):
    """The real DeadlineTransport against a synthetic slow child, offline.

    The child is a local script that records one spawn, reads the durable
    run store itself, notes the state it saw the operation in, then sleeps
    far beyond the deadline. This proves the ordering claim end to end:
    the consent commit is durable before any child exists, the timeout
    finalizes the operation failed, and no second child is ever spawned.
    """

    def setUp(self):
        super().setUp()
        self.workers = Path(self.base) / "workers"
        self.workers.mkdir()
        self.script = self.workers / "slow-child.py"
        self.script.write_text(SLOW_CHILD_SOURCE)
        self.state_path = self.workers / "observed-state.txt"
        self.spawn_path = self.workers / "spawns.txt"

    def slow_transport(self):
        command = [
            sys.executable,
            "-I",
            str(self.script),
            WORKER_FLAG,
            str(self.store.path),
            self.operation_id,
            str(self.state_path),
            str(self.spawn_path),
        ]
        return advisory_transport.DeadlineTransport(worker_command=command)

    def test_deadline_consumes_consent_before_child_and_finishes_failed_once(self):
        before = self.revision()
        outcome = self.execute(self.slow_transport(), timeout_seconds=0.5)
        self.assertEqual(outcome["outcome"], "failed")
        # The child read the durable report itself: the proposed->consumed
        # commit was already on disk when the worker ran, and exactly one
        # child ever existed; a timed-out request is never retried.
        self.assertEqual(self.state_path.read_text(), "consumed")
        self.assertEqual(self.spawn_path.read_text(), "spawn\n")
        self.assertEqual(outcome["result"]["status"], "transport-error")
        self.assertEqual(outcome["result"]["error_type"], "TransportDeadlineExceeded")
        evidence = self.evidence_records()[0]
        self.assertEqual(evidence["category"], "advisory-transport-failure")
        self.assertEqual(evidence["payload"]["failure"]["reason"], "deadline-exceeded")
        self.assertIsNone(evidence["payload"]["response"]["sha256"])
        record = self.operation()
        self.assertEqual(record["state"], "finished")
        self.assertEqual(record["audit"]["outcome"]["result"], "failed")
        # The spent consent refuses replay; the replay attempt spawns no
        # second child and touches no network.
        with self.assertRaises(ApprovalError):
            self.execute(self.slow_transport(), timeout_seconds=0.5)
        self.assertEqual(self.spawn_path.read_text(), "spawn\n")


class UrllibTransportTests(ExecutorCase):
    """The worker-side urllib transport, injected explicitly by these tests.

    UrllibTransport bounds each blocking socket operation only and no
    total deadline; the default executor path selects the deadline
    transport instead (see DefaultSelectionTests). Here the urllib
    transport is passed in directly, with a faked opener and sockets
    patched out, covering the opener contract the deadline worker runs
    inside.
    """

    def setUp(self):
        super().setUp()
        guard = mock.patch("socket.socket")
        guard.start()
        self.addCleanup(guard.stop)
        self.opener = FakeOpener(body=PAGE_BYTES)

    def execute_urllib(self, **over):
        kwargs = {
            "expected_revision": self.revision(),
            "transport": advisory_executor.UrllibTransport(opener=self.opener),
        }
        kwargs.update(over)
        return execute_advisory(self.store, self.operation_id, self.approval, **kwargs)

    def test_opener_construction_proxies_disabled_and_redirects_refused(self):
        builder = mock.patch("urllib.request.build_opener", return_value=self.opener)
        build_opener = builder.start()
        self.addCleanup(builder.stop)
        advisory_executor.UrllibTransport()
        build_opener.assert_called_once()
        handlers = build_opener.call_args[0]
        proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
        redirects = [
            h for h in handlers
            if isinstance(h, urllib.request.HTTPRedirectHandler)
            and type(h) is not urllib.request.HTTPRedirectHandler
        ]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {})
        self.assertEqual(len(redirects), 1)
        request = urllib.request.Request("https://api.osv.dev/v1/query")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            redirects[0].redirect_request(
                request, None, 302, "Found", {"location": "https://evil.example/"}, "https://evil.example/"
            )
        # This suite raised the error itself, so it owns closing it.
        self.assertFalse(caught.exception.fp.closed)
        caught.exception.close()
        self.assertTrue(caught.exception.fp.closed)

    def test_request_parameters_and_streamed_response(self):
        outcome = self.execute_urllib(timeout_seconds=7)
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["result"]["status"], "matches")
        self.assertEqual(len(self.opener.calls), 1)
        call = self.opener.calls[0]
        request = call["request"]
        self.assertEqual(request.full_url, OSV_QUERY_ENDPOINT)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, CANONICAL_BODY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(call["timeout"], 7)

    def test_redirect_status_refused_with_no_second_request(self):
        self.opener.status = 302
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            outcome = self.execute_urllib(timeout_seconds=7)
            gc.collect()
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["result"]["status"], "transport-error")
        self.assertEqual(outcome["result"]["error_type"], "HTTPError")
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(self.evidence_records()[0]["category"], "advisory-transport-failure")
        self.assertEqual(resource_warnings(caught), [])

    def test_redirect_refusal_persists_no_target_or_headers(self):
        self.opener.status = 302
        self.opener.headers = {
            "Location": "https://other.example/v1/query?token=TOPSECRET",
            "Authorization": "Bearer sekrit",
            "Cookie": "session=sekrit2",
        }
        outcome = self.execute_urllib()
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["result"]["error_type"], "HTTPError")
        evidence = self.evidence_records()[0]
        self.assertEqual(
            evidence["payload"]["failure"]["redirect"],
            {"status_code": 302, "followed": False},
        )
        # Nothing from the refused redirect's target or its headers may
        # reach the persisted report or the returned result, recursively;
        # the approved endpoint itself stays recorded.
        report_text = json.dumps(self.store.load(), sort_keys=True)
        outcome_text = json.dumps(outcome, sort_keys=True)
        for text in (report_text, outcome_text):
            self.assertNotIn("TOPSECRET", text)
            self.assertNotIn("sekrit", text)
            self.assertNotIn("other.example", text)
        self.assertIn(OSV_QUERY_ENDPOINT, report_text)

    def test_streaming_oversize_stops_at_cap_plus_one_and_closes(self):
        opener = FakeOpener(status=200, body=b"x" * 300)
        transport = advisory_executor.UrllibTransport(opener=opener)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(advisory_executor.ResponseTooLarge):
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 128)
            gc.collect()
        response = opener.last_response
        # The stream stops after consuming exactly cap + 1 bytes, never
        # buffering the whole oversized body, and the response closes.
        self.assertEqual(response.read_total, 129)
        self.assertTrue(response.closed)
        self.assertEqual(resource_warnings(caught), [])

    def test_streaming_body_at_exact_cap_passes(self):
        body = b"y" * 64
        opener = FakeOpener(status=200, body=body)
        transport = advisory_executor.UrllibTransport(opener=opener)
        data = transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 64)
        self.assertEqual(data, body)
        self.assertTrue(opener.last_response.closed)

    def test_opener_raised_http_error_closed_before_reraise(self):
        # opener.open can raise HTTPError before any response context manager
        # engages; the transport must release the response the error carries
        # before re-raising it.
        fp = ClosableFP()
        self.opener.exc = urllib.error.HTTPError(
            OSV_QUERY_ENDPOINT, 503, "Service Unavailable", None, fp
        )
        transport = advisory_executor.UrllibTransport(opener=self.opener)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(urllib.error.HTTPError):
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
            gc.collect()
        self.assertTrue(fp.closed)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(resource_warnings(caught), [])

    def test_execute_with_opener_raised_http_error_is_warning_free(self):
        fp = ClosableFP()
        self.opener.exc = urllib.error.HTTPError(
            OSV_QUERY_ENDPOINT, 500, "Internal Server Error", None, fp
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            outcome = self.execute_urllib()
            gc.collect()
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["result"]["error_type"], "HTTPError")
        self.assertTrue(fp.closed)
        self.assertEqual(resource_warnings(caught), [])


if __name__ == "__main__":
    unittest.main()
