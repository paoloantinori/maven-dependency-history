"""Inert operation proposals and durable single-use consent; no executor.

An operation record follows the report schema exactly: it enters a report
revision as state ``proposed`` with an empty audit and no evidence, and it
never describes how to run anything by itself. The scope carries the kind
and the exact semantic request the host approved: a Maven operation is an
argv vector plus an absolute working directory (never a shell string), and
an advisory operation is an HTTPS destination (no userinfo, query, or
fragment) plus a structured package query of exactly ecosystem, name and
version identifier fields, with an optional bounded opaque page_token.

Privacy is structural, not content detection. Unknown scope keys are
refused outright, so credential-shaped fields (api_key, token) and raw
request payloads cannot enter a persisted proposal. The module does not
and cannot detect secrets inside the free text the host approved: an
identifier, a disclosure, or a Maven argv element (which may legally carry
-Dpassword=...) is persisted exactly as the host provided it, and keeping
credentials out of that text is the approving host's responsibility. The
approved flag records authorization; it is not sanitization.

Raw-capture proposals bind a still-proposed Maven or advisory source and are
registered through this same closed dispatcher. They can only be consumed
atomically with their source using raw_capture_consent.consume_pair. The
single-operation path refuses a source with any non-denied capture registration;
a denied capture allows ordinary source execution without retaining raw data.

Consent is a host-provided approval record bound to one run, operation and
exact scope, compared with type-sensitive deep equality (1 is not True).
consume_approval persists proposed->consumed together with the started
audit entry in one RunStore commit and only then returns an execution
authorization descriptor; there is no executor in this module, so the
descriptor authorizes nothing by itself. Every refusal path (missing or
mistyped approval, wrong binding, changed observed inputs or tool, stale
revision, replay of a consumed or denied operation) raises before any
commit, and RunConflict from the store propagates unwrapped so callers can
distinguish concurrent writers. finalize_operation moves only
consumed->finished while appending audit keys and validated evidence
references; deny_proposal moves proposed->denied and is terminal. A crash
after consumption leaves the reloaded record consumed, and a second
consume attempt on it is refused: renewed approval requires a new proposal.
"""

import copy
import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

# TEMPORARY BRIDGE: report._json_equal is private, but re-implementing deep
# JSON equality risks a permissive copy (1 == True, 1 == 1.0). Import the
# exact helper the report transitions use until report exposes it publicly.
from mdi.report import _json_equal

KINDS = ("maven", "advisory", "raw-capture")
OUTCOMES = ("succeeded", "failed")
_TERMINAL_NOTE = "a new proposal with renewed approval is required"

OPERATION_FIELDS = frozenset(
    {"id", "run_id", "state", "scope", "input_identities",
     "tool_identity", "disclosure", "audit", "evidence_ids"}
)
APPROVAL_FIELDS = frozenset(
    {"run_id", "operation_id", "scope", "approved", "approved_by", "reference"}
)
_MAVEN_SCOPE_FIELDS = frozenset({"kind", "argv", "cwd"})
_ADVISORY_SCOPE_FIELDS = frozenset({"kind", "destination", "query"})
_QUERY_FIELDS = frozenset({"ecosystem", "name", "version"})
_IDENTITY_FIELDS = frozenset({"path", "sha256"})
_RESERVED_AUDIT_KEYS = frozenset({"started", "outcome", "denied"})

MAX_ARGV_ITEMS = 64
MAX_TEXT = 4096
MAX_DISCLOSURE = 8192
MAX_DESTINATION = 2048
MAX_SCOPE_BYTES = 64 * 1024
MAX_IDENTITIES = 256
MAX_AUDIT_KEYS = 32
MAX_AUDIT_BYTES = 16 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# Identifier whitelist for advisory query fields: no whitespace, no
# placeholder metacharacters, no quotes or escapes, no unicode tricks.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._+:/@-]+")


class OperationsError(Exception):
    """Raised when a proposal, approval, or state move violates its contract."""


class ProposalError(OperationsError):
    """Raised when a proposal record or its scope cannot be built or accepted."""


class ApprovalError(OperationsError):
    """Raised when an approval record does not authorize consuming an operation."""


class StateError(OperationsError):
    """Raised when an operation id or state does not allow the requested move."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value, bound, path, error=ProposalError):
    if not isinstance(value, str) or not value:
        raise error(f"{path}: must be a nonempty string")
    if len(value) > bound:
        raise error(f"{path}: exceeds the {bound} character bound")
    if "\x00" in value:
        raise error(f"{path}: must not contain NUL")


def _bounded_json(value, limit, path):
    try:
        data = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProposalError(f"{path}: is not JSON-safe ({exc})") from exc
    if len(data) > limit:
        raise ProposalError(f"{path}: exceeds the {limit} byte bound")


def _exact_fields(value, fields, path, error=ProposalError):
    if not isinstance(value, dict):
        raise error(f"{path}: must be a JSON object")
    unknown = sorted(set(value) - fields)
    if unknown:
        raise error(f"{path}: unknown fields {unknown}")
    missing = sorted(fields - set(value))
    if missing:
        raise error(f"{path}: missing fields {missing}")


def _check_maven_scope(scope, path):
    _exact_fields(scope, _MAVEN_SCOPE_FIELDS, path)
    argv = scope["argv"]
    if not isinstance(argv, list) or not argv:
        raise ProposalError(f"{path}.argv: must be a nonempty argv vector, never a shell string")
    if len(argv) > MAX_ARGV_ITEMS:
        raise ProposalError(f"{path}.argv: exceeds the {MAX_ARGV_ITEMS} item bound")
    for index, item in enumerate(argv):
        _text(item, MAX_TEXT, f"{path}.argv[{index}]")
    _text(scope["cwd"], MAX_TEXT, f"{path}.cwd")
    if not os.path.isabs(scope["cwd"]):
        raise ProposalError(f"{path}.cwd: must be an absolute path")


def _check_destination(destination, path):
    _text(destination, MAX_DESTINATION, f"{path}.destination")
    parsed = urlsplit(destination)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProposalError(f"{path}.destination: must be an HTTPS URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ProposalError(f"{path}.destination: must not carry userinfo credentials")
    if "?" in destination or "#" in destination:
        raise ProposalError(f"{path}.destination: must not carry a query string or fragment")


def _check_query(query, path):
    fields = _QUERY_FIELDS
    if isinstance(query, dict) and "page_token" in query:
        fields = fields | {"page_token"}
    _exact_fields(query, fields, f"{path}.query")
    if "page_token" in query:
        token = query["page_token"]
        _text(token, MAX_TEXT, f"{path}.query.page_token")
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
            raise ProposalError(f"{path}.query.page_token: whitespace and controls are forbidden")
    for field in sorted(_QUERY_FIELDS):
        value = query[field]
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            raise ProposalError(
                f"{path}.query.{field}: must be a nonempty identifier of letters, digits, "
                "dots, colons, slashes, or version characters; no whitespace or placeholders"
            )


def _check_advisory_scope(scope, path):
    _exact_fields(scope, _ADVISORY_SCOPE_FIELDS, path)
    _check_destination(scope["destination"], path)
    _check_query(scope["query"], path)


def _check_scope(scope, kind, path="scope"):
    if not isinstance(scope, dict):
        raise ProposalError(f"{path}: must be a JSON object")
    if not isinstance(scope.get("kind"), str) or scope["kind"] != kind:
        raise ProposalError(f"{path}.kind: must be exactly {kind!r}")
    if kind == "maven":
        _check_maven_scope(scope, path)
    elif kind == "advisory":
        _check_advisory_scope(scope, path)
    elif kind == "raw-capture":
        from mdi.raw_capture_consent import validate_scope
        validate_scope(scope, path)
    else:
        raise ProposalError(f"{path}.kind: must be one of {', '.join(KINDS)}")
    _bounded_json(scope, MAX_SCOPE_BYTES, path)


def _check_input_identities(identities, path="input_identities"):
    if not isinstance(identities, list):
        raise ProposalError(f"{path}: must be a list")
    if len(identities) > MAX_IDENTITIES:
        raise ProposalError(f"{path}: exceeds the {MAX_IDENTITIES} identity bound")
    for index, identity in enumerate(identities):
        where = f"{path}[{index}]"
        _exact_fields(identity, _IDENTITY_FIELDS, where)
        _text(identity["path"], MAX_TEXT, f"{where}.path")
        digest = identity["sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ProposalError(f"{where}.sha256: must be 64 lowercase hex characters")


def _check_tool_identity(tool, path="tool_identity"):
    if not isinstance(tool, dict) or not tool:
        raise ProposalError(f"{path}: must be a nonempty JSON object")
    _bounded_json(tool, MAX_SCOPE_BYTES, path)


def _validate_proposal_record(record, path="operation"):
    _exact_fields(record, OPERATION_FIELDS, path)
    _text(record["id"], 256, f"{path}.id")
    _text(record["run_id"], MAX_TEXT, f"{path}.run_id")
    if record["state"] != "proposed":
        raise ProposalError(f"{path}.state: a new operation must begin proposed")
    scope = record["scope"]
    _check_scope(scope, scope.get("kind") if isinstance(scope, dict) else None, f"{path}.scope")
    _check_input_identities(record["input_identities"], f"{path}.input_identities")
    _check_tool_identity(record["tool_identity"], f"{path}.tool_identity")
    _text(record["disclosure"], MAX_DISCLOSURE, f"{path}.disclosure")
    if record["audit"] != {}:
        raise ProposalError(f"{path}.audit: must start empty")
    if record["evidence_ids"] != []:
        raise ProposalError(f"{path}.evidence_ids: must start empty")
    if scope['kind'] == 'raw-capture':
        from mdi.raw_capture_consent import validate_capture_record
        validate_capture_record(record)


def build_proposal(kind, scope, input_identities, tool_identity, disclosure,
                   run_id, operation_id=None):
    """Return a fresh proposed operation record; pure, nothing is persisted."""
    if not isinstance(kind, str) or kind not in KINDS:
        raise ProposalError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    if not isinstance(run_id, str) or not run_id:
        raise ProposalError("run_id must be a nonempty string")
    if operation_id is None:
        operation_id = f"op-{uuid.uuid4()}"
    else:
        _text(operation_id, 256, "operation_id")
    scope = copy.deepcopy(scope)
    _check_scope(scope, kind)
    identities = copy.deepcopy(input_identities)
    _check_input_identities(identities)
    tool = copy.deepcopy(tool_identity)
    _check_tool_identity(tool)
    _text(disclosure, MAX_DISCLOSURE, "disclosure")
    return {
        "id": operation_id,
        "run_id": run_id,
        "state": "proposed",
        "scope": scope,
        "input_identities": identities,
        "tool_identity": tool,
        "disclosure": disclosure,
        "audit": {},
        "evidence_ids": [],
    }


def _find_operation(report, operation_id):
    if not isinstance(operation_id, str) or not operation_id:
        raise StateError("operation_id must be a nonempty string")
    for record in report["operations"]:
        if record["id"] == operation_id:
            return record
    raise StateError(f"unknown operation {operation_id!r}")


def propose(store, operation, expected_revision):
    """Append a proposed operation in one commit; return the persisted report."""
    _validate_proposal_record(operation)
    if operation["run_id"] != store.run_id:
        raise ProposalError(
            f"operation run_id {operation['run_id']!r} does not match the run {store.run_id!r}"
        )
    draft = store.load()
    if any(record["id"] == operation["id"] for record in draft["operations"]):
        raise ProposalError(f"duplicate operation {operation['id']!r}")
    if operation['scope']['kind'] == 'raw-capture':
        from mdi.raw_capture_consent import validate_registration
        validate_registration(draft, operation['scope'])
    draft["operations"].append(copy.deepcopy(operation))
    return store.commit(draft, expected_revision)


def _validate_approval(approval, store, operation_id, record):
    _exact_fields(approval, APPROVAL_FIELDS, "approval", error=ApprovalError)
    _text(approval["run_id"], MAX_TEXT, "approval.run_id", error=ApprovalError)
    _text(approval["operation_id"], 256, "approval.operation_id", error=ApprovalError)
    if approval["run_id"] != store.run_id:
        raise ApprovalError("approval run_id does not match this run")
    if approval["operation_id"] != operation_id:
        raise ApprovalError("approval is bound to a different operation")
    if not isinstance(approval["scope"], dict):
        raise ApprovalError("approval scope must be a JSON object")
    if not _json_equal(approval["scope"], record["scope"]):
        raise ApprovalError("approval scope differs from the proposed scope")
    if type(approval["approved"]) is not bool or approval["approved"] is not True:
        raise ApprovalError("approval must carry approved as the boolean True")
    _text(approval["approved_by"], MAX_TEXT, "approval.approved_by", error=ApprovalError)
    _text(approval["reference"], MAX_TEXT, "approval.reference", error=ApprovalError)


def consume_approval(store, operation_id, approval, observed_input_identities,
                     observed_tool_identity, expected_revision):
    """Persist proposed->consumed plus the started audit atomically, then authorize.

    Returns an execution authorization descriptor only after the RunStore
    commit that made the consumption durable. The descriptor authorizes
    nothing by itself: this module runs no process and touches no network.
    Every refusal raises ApprovalError (or StateError for an unknown id)
    before any commit; a stale expected_revision propagates RunConflict.
    """
    draft = store.load()
    record = _find_operation(draft, operation_id)
    if record["state"] != "proposed":
        raise ApprovalError(
            f"operation {operation_id!r} is {record['state']!r}, not proposed; {_TERMINAL_NOTE}"
        )
    if record['scope'].get('kind') == 'raw-capture':
        raise ApprovalError('raw capture requires atomic paired consumption')
    if any(other['scope'].get('kind') == 'raw-capture'
           and other['scope'].get('source_operation_id') == operation_id
           and other['state'] != 'denied' for other in draft['operations']):
        raise ApprovalError('source has a capture registration; paired consumption is required')
    _validate_approval(approval, store, operation_id, record)
    if not _json_equal(observed_input_identities, record["input_identities"]):
        raise ApprovalError("observed input identities differ from the proposal")
    if not _json_equal(observed_tool_identity, record["tool_identity"]):
        raise ApprovalError("observed tool identity differs from the proposal")
    consumed = record
    started = {
        "at": _now(),
        "approved_by": approval["approved_by"],
        "reference": approval["reference"],
    }
    consumed["state"] = "consumed"
    consumed["audit"]["started"] = started
    persisted = store.commit(draft, expected_revision)
    return {
        "operation_id": operation_id,
        "run_id": store.run_id,
        "revision": persisted["revision"],
        "scope": copy.deepcopy(record["scope"]),
        "started": copy.deepcopy(started),
    }


def _merge_audit(record, entries, path="audit"):
    if entries is None:
        entries = {}
    if not isinstance(entries, dict):
        raise OperationsError(f"{path}: must be a JSON object")
    if len(entries) > MAX_AUDIT_KEYS:
        raise OperationsError(f"{path}: exceeds the {MAX_AUDIT_KEYS} key bound")
    merged = copy.deepcopy(record["audit"])
    for key, value in entries.items():
        _text(key, 256, f"{path} key")
        if key in merged or key in _RESERVED_AUDIT_KEYS:
            raise OperationsError(f"{path}[{key!r}]: audit keys append, never rewrite")
        merged[key] = copy.deepcopy(value)
    _bounded_json(merged, MAX_AUDIT_BYTES, path)
    return merged


def finalize_operation(store, operation_id, outcome, audit=None, evidence_ids=None, *,
                       expected_revision):
    """Move consumed->finished, appending audit keys and evidence references."""
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise OperationsError(f"outcome must be one of {', '.join(OUTCOMES)}, got {outcome!r}")
    draft = store.load()
    record = _find_operation(draft, operation_id)
    if record["state"] != "consumed":
        raise StateError(
            f"operation {operation_id!r} is {record['state']!r}; only consumed finishes"
        )
    merged = _merge_audit(record, audit)
    merged["outcome"] = {"result": outcome, "at": _now()}
    if evidence_ids is None:
        evidence_ids = []
    if not isinstance(evidence_ids, list):
        raise OperationsError("evidence_ids: must be a list")
    known = {entry["id"] for entry in draft["evidence"]}
    for index, ref in enumerate(evidence_ids):
        _text(ref, 256, f"evidence_ids[{index}]")
        if ref not in known:
            raise OperationsError(f"evidence_ids[{index}]: unknown evidence reference {ref!r}")
    finished = record
    finished["state"] = "finished"
    finished["audit"] = merged
    finished["evidence_ids"] = finished["evidence_ids"] + list(evidence_ids)
    return store.commit(draft, expected_revision)


def deny_proposal(store, operation_id, reason, expected_revision):
    """Move proposed->denied explicitly; a denial is terminal and executes nothing."""
    _text(reason, MAX_TEXT, "reason")
    draft = store.load()
    record = _find_operation(draft, operation_id)
    if record["state"] != "proposed":
        raise StateError(
            f"operation {operation_id!r} is {record['state']!r}; only proposed can be denied"
        )
    denied = record
    denied["state"] = "denied"
    denied["audit"]["denied"] = {"reason": reason, "at": _now()}
    return store.commit(draft, expected_revision)
