"""Consent-gated execution of one advisory operation page against OSV.

`execute_advisory` is the only public entry point: it takes a RunStore, the
id of a proposed advisory operation, and the host approval record, performs
the approved OSV query for exactly one response page, and persists both the
response evidence and the finished operation. Every request field is derived
from the durable operation record; the caller supplies no URL, no body and
no headers. There is deliberately no reusable entry point that executes an
already consumed operation: renewal after any outcome requires a new
proposal with fresh approval.

Policy checks run before any commit, so an operation failing one is left
proposed and its single-use consent is never spent: the scope must be kind
"advisory" with destination exactly `OSV_QUERY_ENDPOINT` (a lookalike
carrying a port, userinfo, a query string or a fragment is refused, never
normalized); the flat scope query must be ecosystem "Maven" with a name of
exactly "group:artifact" (one colon, both sides nonempty) and a version
passed verbatim to `advisories.build_query`, since this module never parses
versions; input identities must be empty, because a pure network lookup
claims no local files; and the proposal must carry exactly this module's
`TOOL_IDENTITY`, a constant binding of adapter name, version, the sha256
of this source file and this module's transport identity, so a proposal
naming any other tool is refused rather than echoed back as
authorization, and a proposal made before the transport module changed is
invalidated with it.

Without optional raw capture the transaction uses three RunStore commits,
with honest crash windows; no atomicity is claimed across them. Paired capture
consumes both approvals in the first commit and adds a separate final capture
operation commit after the source finishes; the bounded raw file is the sole
explicitly approved exception to structured-only persistence below:

1. argument and policy validation; nothing is committed;
2. `operations.consume_approval` commits proposed->consumed with the
   started audit (revision N to N+1). Every refusal inside it raises
   before that commit, so an invalid, stale, mistyped or replayed
   approval consumes nothing and no transport call happens;
3. the consumed record is re-read from the store at the committed
   revision; destination, query and identities come from that durable
   record, never from the in-memory descriptor alone;
4. exactly one transport POST is issued, the only external action. On
   interrupt (KeyboardInterrupt, SystemExit) the exception propagates
   with the operation left consumed and is never retried here;
5. any transport, size, decoding or mapping failure is recorded as
   explicit failure evidence with status "transport-error" and the
   operation is finalized with outcome "failed"; a failed fetch is never
   reported as "no-known-matches". The persisted failure carries only
   executor-authored text: a class name validated against a fixed
   pattern, a message from a fixed vocabulary, the stable machine
   code when the exception carries one, and the numeric HTTP status
   for an HTTPError. `str(exc)` is never persisted, so a provider
   message, a URLError reason carrying endpoint credentials, or a
   hostile response snippet quoted by a mapper refusal cannot enter
   the report or the returned result. On that failure path the
   response a caught HTTPError carries is closed (urllib wraps every
   HTTPError in temporary-file machinery that warns at deallocation
   unless close() ran), and the urllib transport closes an HTTPError
   raised out of opener.open before re-raising it, where no response
   context manager engaged. Successful responses are closed by their
   context manager. Exactly those closes are claimed and tested;
   nothing is claimed for interrupts, which propagate uncaught;
6. the evidence is committed first (N+1 to N+2), then
   `operations.finalize_operation` moves consumed->finished referencing
   it (N+2 to N+3), because finalize validates that referenced evidence
   already exists. A crash between those two commits leaves a consumed
   operation with unreferenced evidence; that state is safe because the
   consent is already spent and replay is refused.

A page carrying next_page_token finishes with outcome "succeeded" and the
token preserved in the result, the evidence and the return value. Automatic
continuation is never automatic: a follow-up page needs a new proposal and
approval bound to its exact page_token. Both successful and failed page
results retain the requested token and remain page-local evidence.

The default transport is `advisory_transport.DeadlineTransport`: it runs
the one POST through a short-lived worker child process the parent kills
at the total monotonic deadline, so a slow or trickling exchange cannot
extend the request indefinitely, and it never retries. That transport in
turn runs `UrllibTransport` inside the worker: a urllib opener with
`ProxyHandler({})` (no environment proxies), a redirect handler that
raises on every 3xx instead of following it, default certificate
verification, one fixed Content-Type header, the caller's timeout, and a
streaming read capped at `limits.response_bytes` while reading, before
the body is buffered. A caller-injected transport is a trusted seam: the
executor then enforces no deadline of its own and claims none, so a
seam that bounds each socket operation only admits an unbounded
exchange; deadline ownership belongs to whatever the caller injected.
`timeout_seconds` is passed to the transport: with the default it is the
total deadline; with `UrllibTransport` injected it is urllib's per-
operation socket timeout.
"""

import hashlib
import json
import math
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mdi import operations, raw_capture_consent
from mdi.raw_capture_store import RawCaptureSink
from mdi.raw_capture_store import TOOL_IDENTITY as _CAPTURE_TOOL_IDENTITY
from mdi.advisories import (
    MAVEN_ECOSYSTEM,
    OSV_QUERY_ENDPOINT,
    PROVIDER_NAME,
    AdvisoryError,
    AdvisoryLimits,
    build_query,
    map_response,
    transport_error_result,
)
from mdi.advisory_transport import TOOL_IDENTITY as _TRANSPORT_IDENTITY
from mdi.advisory_transport import DeadlineTransport

# The identity binds this module's own source and the transport module's
# identity, imported dynamically, so any edit to the killable worker
# invalidates every proposal that named the earlier transport. The
# transport imports only advisories at its top level and the worker
# imports this module lazily, so there is no import cycle.
TOOL_IDENTITY = {
    "name": "mdi.advisory_executor",
    "version": "2",
    "source_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
    "transport": dict(_TRANSPORT_IDENTITY),
}

_READ_CHUNK = 65536


class AdvisoryExecutionError(Exception):
    """Raised when the executor refuses to run an operation as proposed."""


class ResponseTooLarge(AdvisoryError):
    """Raised by the urllib transport when the streaming cap is exceeded."""

    def __init__(self, byte_count, cap):
        super().__init__(
            f"response of {byte_count} bytes exceeds the {cap} byte cap while streaming",
            "response-too-large",
        )


class _RefusedRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx surfaces as HTTPError, never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, f"redirect refused: HTTP {code} is not followed", headers, fp
        )


def _redirect_refusal(exc):
    """For a refused 3xx, return only its status and that it was not followed.

    The Location header is deliberately never persisted: a redirect
    target can carry signed tokens or credentials, and no header value
    enters the evidence. Only the numeric status is safe to record.
    """
    if not isinstance(exc, urllib.error.HTTPError):
        return None
    code = exc.code
    if not isinstance(code, int) or not 300 <= code < 400:
        return None
    return {"status_code": code, "followed": False}


def _close_error_response(exc):
    """Close the response an HTTPError carries; never mask the real error.

    HTTPError extends urllib's addbase, which wraps its fp in temporary
    file machinery whose deallocator emits a ResourceWarning unless
    close() ran, even when the error was built with fp None and a BytesIO
    was substituted. Closing is idempotent (the closer guards on
    close_called), so the transport and the executor may both close the
    same error. A close that itself fails must not displace the original
    transport failure, so it is ignored here.
    """
    if isinstance(exc, urllib.error.HTTPError):
        try:
            exc.close()
        except Exception:
            pass


# A persisted failure names only a class from this fixed shape (a Python
# identifier) and a code from this fixed shape (a lowercase token), so a
# hostile exception cannot smuggle text through either field.
_SAFE_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_SAFE_CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_UNSAFE_TYPE = "TransportFailure"

_FAILURE_TYPE_CACHE = {}


def _safe_type_name(exc):
    """The exception's class name, or a fixed bucket when it is unsafe."""
    name = type(exc).__name__
    return name if _SAFE_TYPE_PATTERN.fullmatch(name) else _UNSAFE_TYPE


def _stable_code(exc):
    """The exception's stable machine code, or None; never free text."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _SAFE_CODE_PATTERN.fullmatch(code):
        return code
    return None


def _safe_failure(exc):
    """(type_name, message, code, status) for persisting a transport failure.

    Every message comes from the fixed executor-authored vocabulary
    below: nothing derived from str(exc), the provider's HTTPError
    message, a URLError reason (which can carry endpoint credentials), a
    mapper refusal quoting a hostile response, or an exception header
    object is ever returned. The only provider-derived values are the
    validated class name and the numeric HTTP status. The failure still
    distinguishes deadline exhaustion from real HTTP statuses and keeps
    every stable AdvisoryError code.
    """
    code = _stable_code(exc)
    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            status = None
        message = (
            f"OSV query failed with HTTP status {status}"
            if status is not None
            else "OSV query failed with an HTTP error"
        )
        return "HTTPError", message, code, status
    if isinstance(exc, urllib.error.URLError):
        return "URLError", "the OSV request to the approved endpoint failed", code, None
    if isinstance(exc, TimeoutError):
        return _safe_type_name(exc), "the OSV request exceeded its time limit", code, None
    if isinstance(exc, AdvisoryError):
        message = (
            f"the OSV exchange failed with the stable code {code}"
            if code is not None
            else "the OSV exchange failed"
        )
        return _safe_type_name(exc), message, code, None
    return _safe_type_name(exc), "the OSV request failed unexpectedly", code, None


def _named_exception(name, message):
    """A carrier exception with the executor-chosen type name and message.

    `advisories.transport_error_result` derives its persisted error_type
    and error fields from the exception object it is handed; this hands
    it a carrier carrying only the normalized identity and text, so
    nothing from the original exception's message survives.
    """
    cls = _FAILURE_TYPE_CACHE.setdefault(
        name, type(name, (Exception,), {"__module__": "mdi.advisory_executor"})
    )
    return cls(message)


class UrllibTransport:
    """The worker-side inner transport: no proxies, no redirects, streaming cap.

    It bounds each blocking socket operation only and enforces no total
    deadline; the default executor path selects DeadlineTransport, which
    runs this transport inside its killable worker. Explicit injection
    here is a trusted test seam.
    """

    def __init__(self, opener=None):
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _RefusedRedirect()
            )
        self._opener = opener

    def post(self, destination, body, timeout_seconds, response_cap_bytes):
        request = urllib.request.Request(
            destination,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            response = self._opener.open(request, timeout=timeout_seconds)
        except urllib.error.HTTPError as exc:
            # No response context manager engaged for an error raised out
            # of open(); release the response the error carries, then let
            # the caller see the error unchanged.
            _close_error_response(exc)
            raise
        with response:
            code = response.getcode()
            if isinstance(code, int) and 300 <= code < 400:
                raise urllib.error.HTTPError(
                    destination,
                    code,
                    f"redirect refused: HTTP {code} is not followed",
                    getattr(response, "headers", None),
                    None,
                )
            chunks = []
            total = 0
            while total <= response_cap_bytes:
                chunk = response.read(min(_READ_CHUNK, response_cap_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > response_cap_bytes:
                raise ResponseTooLarge(total, response_cap_bytes)
            return b"".join(chunks)


def advisory_proposal(store_or_run_id, group_id, artifact_id, version, operation_id=None, *,
                      page_token=None):
    """Build a fresh proposed advisory operation bound to this adapter; pure.

    Accepts a RunStore or a run_id string. The destination is the one
    allowlisted OSV endpoint, the query is the flat scope spelling of one
    Maven coordinate, and the tool identity is this module's constant, so
    the record is executable here without any caller-supplied request
    fields. Coordinates are validated by `advisories.build_query`.
    """
    run_id = getattr(store_or_run_id, "run_id", store_or_run_id)
    build_query(group_id, artifact_id, version, page_token=page_token)
    scope = {
        "kind": "advisory",
        "destination": OSV_QUERY_ENDPOINT,
        "query": {
            "ecosystem": MAVEN_ECOSYSTEM,
            "name": f"{group_id}:{artifact_id}",
            "version": version,
        },
    }
    if page_token is not None:
        scope["query"]["page_token"] = page_token
    disclosure = (
        f"Query {PROVIDER_NAME} at {OSV_QUERY_ENDPOINT} for known advisories matching "
        f"Maven coordinate {group_id}:{artifact_id}:{version}; one page only, "
        "automatic follow-up pages are never taken."
    )
    if page_token is not None:
        disclosure += " This continuation sends the exact page_token shown in the approved scope."
    return operations.build_proposal(
        "advisory", scope, [], TOOL_IDENTITY, disclosure, run_id, operation_id
    )


def _binds_this_adapter(tool):
    """Type-faithful comparison against TOOL_IDENTITY (True is not 1)."""
    try:
        mine = json.dumps(TOOL_IDENTITY, sort_keys=True, allow_nan=False)
        theirs = json.dumps(tool, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return mine == theirs


def _validated_scope(record):
    """Return (destination, group, artifact, version) or refuse the operation."""
    scope = record.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") != "advisory":
        raise AdvisoryExecutionError(
            "this executor runs advisory operations only; refuse to execute anything else"
        )
    destination = scope.get("destination")
    if destination != OSV_QUERY_ENDPOINT:
        raise AdvisoryExecutionError(
            f"destination {destination!r} is not the one allowlisted endpoint {OSV_QUERY_ENDPOINT!r}"
        )
    query = scope.get("query")
    fields = {"ecosystem", "name", "version"}
    if not isinstance(query, dict) or set(query) not in (fields, fields | {"page_token"}):
        raise AdvisoryExecutionError(
            "advisory scope query requires ecosystem, name and version, with optional page_token"
        )
    operations._check_query(query, "scope")
    if query["ecosystem"] != MAVEN_ECOSYSTEM:
        raise AdvisoryExecutionError(
            f"advisory scope query ecosystem must be exactly {MAVEN_ECOSYSTEM!r}"
        )
    name = query["name"]
    group, separator, artifact = name.partition(":") if isinstance(name, str) else ("", "", "")
    if not separator or ":" in artifact or not group or not artifact:
        raise AdvisoryExecutionError(
            "advisory scope query name must be a group and an artifact joined by exactly one colon"
        )
    if not isinstance(query["version"], str) or not query["version"]:
        raise AdvisoryExecutionError("advisory scope query version must be a nonempty string")
    if record.get("input_identities") != []:
        raise AdvisoryExecutionError(
            "an advisory operation is a pure network lookup; local input identities must be empty"
        )
    if not _binds_this_adapter(record.get("tool_identity")):
        raise AdvisoryExecutionError(
            "operation is not bound to this adapter's exact tool identity; refusing to run it"
        )
    return destination, group, artifact, query["version"]


def _check_invocation(transport, limits, timeout_seconds):
    if transport is not None and not callable(getattr(transport, "post", None)):
        raise AdvisoryExecutionError("transport must provide a callable post method")
    if not isinstance(limits, AdvisoryLimits):
        raise AdvisoryExecutionError(
            f"limits must be an AdvisoryLimits, got {type(limits).__name__}"
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
        or not math.isfinite(timeout_seconds)
    ):
        raise AdvisoryExecutionError(f"timeout_seconds must be a positive number, got {timeout_seconds!r}")


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_body(query):
    return json.dumps(query, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def execute_advisory(
    store,
    operation_id,
    approval,
    *,
    expected_revision,
    transport=None,
    limits=AdvisoryLimits(),
    timeout_seconds=20,
    capture_operation_id=None,
    capture_approval=None,
):
    """Execute one approved advisory operation page; return the persisted outcome.

    The return value holds operation_id, evidence_id, outcome, the mapped
    page result, its next_page_token (None when the page is final or the
    attempt failed), the finalized report and its revision. Without an
    injected transport the request runs through the total-deadline
    DeadlineTransport and never retries; an injected transport is a
    trusted seam and owns its own deadline semantics, so no blanket
    total-deadline guarantee is claimed when the caller overrides.
    Failures persist only executor-authored text: a validated class
    name, a fixed message, the stable machine code and the numeric HTTP
    status; a refused 3xx is recorded as its status code and followed
    False only. The Location header, every other response header and
    str(exc) are never persisted, since redirect targets can carry
    signed tokens or credentials and provider or hostile text can carry
    secrets. `timeout_seconds` is the total deadline for the default
    transport and is passed through unchanged to an injected one.

    Optional capture_operation_id and capture_approval must be supplied together.
    The source and capture approvals are validated and consumed in one revision
    before opening the private run-local sink or making a request. The sink
    receives only the bounded returned response body, including malformed JSON;
    a transport failure exposes no partial body and leaves an empty partial
    capture. Injected oversized bodies are retained only through the transport
    byte bound and never labeled complete. A smaller approved capture cap keeps
    a prefix and marks it partial. The sink closes before any evidence commit.
    Source and capture finalization use separate commits; interruptions leave
    consumed consent and require a fresh pair, never a retry. No raw bytes enter
    the returned document or report. No file is created on the default path.
    """
    _check_invocation(transport, limits, timeout_seconds)
    current = store.load()
    proposed = operations._find_operation(current, operation_id)
    _validated_scope(proposed)
    if (capture_operation_id is None) != (capture_approval is None):
        raise AdvisoryExecutionError('capture operation and approval must be supplied together')
    if capture_operation_id is None:
        descriptor = operations.consume_approval(
            store, operation_id, approval, [], TOOL_IDENTITY, expected_revision
        )
    else:
        capture = operations._find_operation(current, capture_operation_id)
        if capture['scope'].get('source_operation_id') != operation_id:
            raise operations.ApprovalError('capture is bound to a different source operation')
        descriptor = raw_capture_consent.consume_pair(
            store, capture_operation_id, approval, capture_approval,
            observed_source_inputs=[], observed_source_tool=TOOL_IDENTITY,
            observed_capture_inputs=[], observed_capture_tool=_CAPTURE_TOOL_IDENTITY,
            expected_revision=expected_revision,
        )
    return _execute_consumed(store, operation_id, descriptor, transport, limits, timeout_seconds,
                             capture_operation_id=capture_operation_id)


def _execute_consumed(store, operation_id, descriptor, transport, limits, timeout_seconds, *,
                      capture_operation_id=None):
    """Drive the already consumed operation; private, never a public entry point."""
    persisted = store.load(descriptor["revision"])
    record = operations._find_operation(persisted, operation_id)
    if record["state"] != "consumed":
        raise AdvisoryExecutionError(
            f"durable operation {operation_id!r} is {record['state']!r}, not consumed; "
            "refusing to touch the network"
        )
    destination, group, artifact, version = _validated_scope(record)
    page_token = record["scope"]["query"].get("page_token")
    query = build_query(group, artifact, version, page_token=page_token, limits=limits)
    body = _canonical_body(query)
    body_digest = hashlib.sha256(body).hexdigest()
    if transport is None:
        transport = DeadlineTransport()
    raw = None
    stamp = None
    failure = None
    sink = None
    capture_metadata = None
    capture_failed = False
    body_complete = False
    source_bytes = None
    try:
        try:
            if capture_operation_id is not None:
                sink = RawCaptureSink.open(store, capture_operation_id,
                                           expected_revision=descriptor['revision'])
            raw = transport.post(destination, body, timeout_seconds, limits.response_bytes)
            if isinstance(raw, bytes):
                source_bytes = len(raw)
                body_complete = len(raw) <= limits.response_bytes
                if sink is not None:
                    try:
                        sink.write(raw[:limits.response_bytes])
                    except Exception:
                        # The response already arrived. Local retention failure
                        # must not discard its independently mappable evidence.
                        capture_failed = True
                    except BaseException:
                        capture_failed = True
                        raise
            stamp = _now_iso()
            result = map_response(
                raw, group, artifact, version, retrieved_at=stamp, limits=limits,
                request_page_token=page_token,
            )
            outcome = "succeeded"
        finally:
            # The sink holds the run writer lock. Release it before publishing
            # evidence even when mapping fails or the caller interrupts.
            if sink is not None:
                primary = sys.exception()
                try:
                    capture_metadata = sink.close(
                        interrupted=not body_complete or capture_failed
                        or primary is not None and not isinstance(primary, Exception))
                except BaseException as close_error:
                    capture_failed = True
                    capture_metadata = sink.result or getattr(close_error, 'capture', None)
                    if primary is not None:
                        primary.add_note('advisory raw capture close was not confirmed')
                    elif not isinstance(close_error, Exception):
                        close_error.raw_capture = capture_metadata
                        raise
                finally:
                    if primary is not None:
                        primary.raw_capture = capture_metadata
    except Exception as exc:
        if capture_operation_id is not None and sink is None:
            capture_failed = True
            capture_metadata = getattr(exc, 'capture', None) or {
                'path': None, 'status': 'unconfirmed', 'retained': False,
            }
        _close_error_response(exc)
        if stamp is None:
            stamp = _now_iso()
        outcome = "failed"
        error_type, message, code, status = _safe_failure(exc)
        failure = {
            "error_type": error_type,
            "error": message,
            "reason": code,
        }
        if status is not None:
            failure["status_code"] = status
        redirect = _redirect_refusal(exc)
        if redirect is not None:
            failure["redirect"] = redirect
        result = transport_error_result(
            group, artifact, version, _named_exception(error_type, message),
            retrieved_at=stamp, limits=limits,
            request_page_token=page_token,
        )
    evidence = {
        "id": f"ev-{uuid.uuid4()}",
        "category": "advisory-page" if outcome == "succeeded" else "advisory-transport-failure",
        "source_locator": destination,
        "observed_at": stamp,
        "producer": PROVIDER_NAME,
        "payload": {
            "request": {
                "url": destination,
                "body": query,
                "body_sha256": body_digest,
                "body_bytes": len(body),
            },
            "response": (
                {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                if isinstance(raw, bytes)
                else {"sha256": None, "bytes": None}
            ),
            "result": result,
        },
    }
    if failure is not None:
        evidence["payload"]["failure"] = failure
    persisted["evidence"].append(evidence)
    capture_evidence = None
    capture_outcome = None
    if capture_operation_id is not None:
        capture_metadata = dict(capture_metadata or {
            'path': None, 'status': 'unconfirmed', 'retained': False,
        }, source_complete=body_complete, source_bytes=source_bytes)
        if capture_failed or not body_complete:
            capture_metadata['status'] = 'partial'
        capture_outcome = ('succeeded' if not capture_failed and body_complete
                           and capture_metadata['status'] == 'complete' else 'failed')
        capture_evidence = {
            'id': f'ev-{uuid.uuid4()}', 'category': 'raw-capture',
            'source_locator': capture_metadata.get('path') or destination,
            'observed_at': stamp, 'producer': 'mdi.raw_capture_store',
            'payload': {'source_operation_id': operation_id,
                        'capture_operation_id': capture_operation_id,
                        'capture': capture_metadata},
        }
        persisted['evidence'].append(capture_evidence)
    try:
        persisted_evidence = store.commit(persisted, descriptor["revision"])
        finalized = operations.finalize_operation(
            store,
            operation_id,
            outcome,
            evidence_ids=[evidence["id"]],
            expected_revision=persisted_evidence["revision"],
        )
        if capture_evidence is not None:
            finalized = operations.finalize_operation(
                store, capture_operation_id, capture_outcome,
                evidence_ids=[capture_evidence['id']], expected_revision=finalized['revision'],
            )
    except BaseException as exc:
        if capture_metadata is not None:
            exc.raw_capture = capture_metadata
        raise
    return {
        **({'capture': capture_metadata, 'capture_outcome': capture_outcome,
            'capture_evidence_id': capture_evidence['id']} if capture_evidence is not None else {}),
        "operation_id": operation_id,
        "evidence_id": evidence["id"],
        "outcome": outcome,
        "result": result,
        "next_page_token": result.get("next_page_token"),
        "report": finalized,
        "revision": finalized["revision"],
    }
