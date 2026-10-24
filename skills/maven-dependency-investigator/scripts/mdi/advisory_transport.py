"""Total-deadline transport for the one approved OSV query request.

The worker's inner socket transport (`mdi.advisory_executor.UrllibTransport`)
bounds each blocking socket operation, not the whole exchange: a server
that drips one byte per interval keeps every single recv inside the socket
timeout and can extend one request indefinitely, and a cooperative clock
check cannot interrupt a blocked read at all. This module enforces a true
total deadline by moving the blocking HTTP call into a one-shot child
process the parent can kill: `DeadlineTransport.post` spawns a fresh
trusted worker per call, hands it one bounded JSON request on stdin, pumps
the exchange against a monotonic clock, and SIGKILLs the worker's whole
process group the moment the deadline, the stdout bound or an interruption
arrives. No thread is abandoned and the request is never retried; cleanup
always signals the worker's group while it can still have members and
reaps the leader with a bounded wait, so a stubborn worker's termination
is at worst unconfirmed (reported below) rather than silently abandoned.

Process start: `subprocess.Popen(..., start_new_session=True)` with an
explicit argv list, never a shell. Verified against the local CPython
3.14.7 source: with start_new_session the child calls setsid() (so the
parent can killpg the group) and Popen takes the _fork_exec path, which
runs no Python code between fork and exec; os.fork() is never called here
and no implicit fork happens in this module. The `-I` interpreter flag was
verified empirically on this interpreter (3.14.7): the child ignores
PYTHONPATH entirely and adds neither the script directory nor the caller's
cwd to sys.path; the worker then inserts the one trusted mdi parent
directory, derived from this file's own resolved location, at sys.path[0].
The worker environment is a fresh allowlist (PATH with absolute components
only, a fresh empty HOME, LC_ALL=C, TZ=UTC); nothing is inherited, so
hostile proxy, certificate-path or PYTHON* values from the caller's
environment cannot reach the child.

Protocol: one JSON request line on stdin (schema, destination, base64
body, timeout, response cap), one JSON response line on stdout. The parent
refuses a body above _MAX_REQUEST_BODY_BYTES and a request above
_MAX_REQUEST_BYTES before spawning, and bounds the worker's stdout at the
base64 worst case of the response cap plus envelope slack, so a stuck,
slow, trickling or flooding child can neither wedge the parent nor grow
memory without bound. The worker performs exactly one POST through
`mdi.advisory_executor.UrllibTransport` (no proxies, redirects refused,
default certificate verification, streaming byte cap preserved) and never
retries; it answers with the response bytes base64-encoded, or a minimal
error envelope. Worker stderr is devnull and no raw body is ever written
to disk: the body lives in pipes and memory only, and the parent
re-validates the decoded length against the cap before returning it.

Redaction: no child exception object, exception message, response header
or Location value ever crosses the process boundary. The envelope carries
only a stable error type name, a numeric HTTP status and a stable
machine-readable code; the parent reconstructs its own exceptions
(including a urllib HTTPError already closed, carrying just the numeric
status) from those fields alone. Unrecognized worker exceptions collapse
to the stable bucket "unexpected-worker-error" with no detail.

The deadline is anchored when Popen returns (exec succeeded), matching the
gitinspect/usage budget convention: it bounds the child's runtime while
parent-side fork/exec latency under load does not consume the budget. The
worker also passes its own remaining budget as the per-operation socket
timeout, a best-effort inner bound only; the parent's kill is the
deadline. Cleanup is total in what it attempts and honest about what it
cannot prove: on deadline, stdout overflow, malformed protocol, or any
BaseException (including KeyboardInterrupt and SystemExit), the worker's
process group receives SIGKILL while it can still have members and the
leader is reaped with a bounded (10 second) wait; if that wait expires,
termination and reaping stay unconfirmed, and nothing is claimed about
descendants that detached into their own session. Both pipes close on
every path, and a completed exchange is reaped before returning. The
worker's exit status is never trusted; only the strictly validated
envelope is.

`DeadlineTransport.post(destination, body, timeout_seconds,
response_cap_bytes)` matches the executor's transport contract, so it can
be injected as `execute_advisory(..., transport=...)` unchanged.
`TransportDeadlineExceeded` is a stable TimeoutError subclass for the
deadline itself. HTTP status failures surface as urllib's own HTTPError
and URLError, carrying a fixed parent-side message and a numeric status
only; worker and protocol failures are `AdvisoryError` subclasses
carrying a stable `code` the executor already records as the failure
reason. `TOOL_IDENTITY` follows the adapter identity convention for the
later executor integration slice.
"""

import base64
import binascii
import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

# The worker half of this module runs this very file as a script under
# `python -I`, where no mdi parent directory is on sys.path and none can
# be injected through the environment. Bootstrap the one trusted parent
# directory, derived from this file's own resolved location, before the
# mdi imports below; imported as mdi.advisory_transport the guard is
# inert because the package is already importable.
if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mdi.advisories import OSV_QUERY_ENDPOINT, AdvisoryError

SCHEMA = "mdi.advisory-transport/1"
WORKER_FLAG = "--mdi-osv-worker"

# Parent-side request bounds. The executor's canonical OSV query body is a
# few hundred bytes; 64 KiB is a hard ceiling, and the fully encoded JSON
# request line must fit 256 KiB. Both are checked before the spawn.
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 256 * 1024

# The worker's stdout is bounded at the base64 worst case of the response
# cap plus envelope slack; anything beyond is a flooding child.
_STDOUT_SLACK_BYTES = 4096
_B64_WORST_CASE_NUMERATOR, _B64_WORST_CASE_DENOMINATOR = 4, 3
_READ_CHUNK = 65536
_REAP_SECONDS = 10

_ERROR_TYPE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")

_SCRIPT_PATH = Path(__file__).resolve()
_SCRIPT_DIR = _SCRIPT_PATH.parent
_TRUSTED_PARENT_DIR = _SCRIPT_DIR.parent

TOOL_IDENTITY = {
    "name": "mdi.advisory_transport",
    "version": "1",
    "source_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
}


class TransportDeadlineExceeded(TimeoutError):
    """The total monotonic deadline expired; the worker was terminated.

    Stable TimeoutError subclass: callers may catch the builtin, and the
    executor records the type name without any child-controlled text.
    """

    code = "deadline-exceeded"


class AdvisoryTransportError(AdvisoryError):
    """Parent-side refusal or worker protocol failure; stable code only.

    `code` is a stable machine-readable reason (mirroring AdvisoryError)
    so callers never match on message text, and the message never
    contains anything the worker produced beyond fixed vocabulary and
    numbers.
    """


class TransportResponseTooLarge(AdvisoryTransportError):
    """The response exceeded the streaming cap (worker- or parent-detected)."""

    def __init__(self, cap):
        super().__init__(f"OSV response exceeded the {cap} byte streaming cap", "response-too-large")


def default_worker_command():
    """argv for the trusted worker: this interpreter, isolated, this file."""
    if not sys.executable:
        raise AdvisoryTransportError(
            "the running Python interpreter path is unavailable; cannot start the OSV worker",
            "worker-spawn-failed",
        )
    return [sys.executable, "-I", str(_SCRIPT_PATH), WORKER_FLAG]


def _sanitized_path(path_value):
    """Child-side PATH: absolute components only, or the trusted default."""
    absolute = [part for part in path_value.split(os.pathsep) if part and os.path.isabs(part)]
    return os.pathsep.join(absolute) or os.defpath


def _worker_env(home):
    """Fresh allowlist environment; nothing is inherited from the caller."""
    return {
        "PATH": _sanitized_path(os.environ.get("PATH") or os.defpath),
        "HOME": home,
        "LC_ALL": "C",
        "TZ": "UTC",
    }


def _require_posix_safety():
    missing = [name for name in ("setsid", "killpg", "set_blocking") if not hasattr(os, name)]
    if missing or not hasattr(signal, "SIGKILL") or os.name != "posix":
        raise AdvisoryTransportError(
            f"platform lacks process-control safety: {', '.join(missing)}",
            "platform-lacks-process-control",
        )


def _positive_number(value):
    """A finite positive non-bool number, or raise with the stable code."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdvisoryTransportError(f"timeout_seconds must be a number, got {value!r}", "bad-timeout")
    if not math.isfinite(value) or value <= 0:
        raise AdvisoryTransportError(f"timeout_seconds must be positive and finite, got {value!r}", "bad-timeout")
    return value


def _positive_cap(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdvisoryTransportError(
            f"response_cap_bytes must be int, got {type(value).__name__}", "bad-cap"
        )
    if value <= 0:
        raise AdvisoryTransportError(f"response_cap_bytes must be positive, got {value}", "bad-cap")
    return value


def _try_kill_group(proc):
    """SIGKILL the worker's process group; 'sent', 'absent' or 'failed'.

    Signal acceptance proves neither descendant exit nor leader reaping;
    the leader is reaped separately by the caller with a bounded wait.
    """
    try:
        pid = proc.pid
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            # pgid 1 becomes kill(-1): every process of the user. Never
            # signal an unvalidated group id.
            return "refused-invalid-pgid"
        os.killpg(pid, signal.SIGKILL)
        return "sent"
    except ProcessLookupError:
        return "absent"
    except OSError:
        return "failed"


def _terminate(proc):
    """Kill the group while it can still have members, then reap bounded.

    killpg runs only while the leader is unreaped (poll() is None): a
    reaped pid could have been reused for a different process group, and
    once the trusted one-shot leader is gone its group has no members.
    The reap wait is bounded; if it expires, termination and reaping stay
    unconfirmed and nothing further is attempted or claimed.
    """
    if proc.poll() is None:
        _try_kill_group(proc)
        try:
            proc.wait(timeout=_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _close_pipes(proc):
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _build_request(destination, body, timeout_seconds, response_cap_bytes):
    """Encode the one-line worker request; bounded by construction."""
    request = {
        "schema": SCHEMA,
        "destination": destination,
        "body_b64": base64.b64encode(body).decode("ascii"),
        "timeout_seconds": float(timeout_seconds),
        "response_cap_bytes": int(response_cap_bytes),
    }
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise AdvisoryTransportError(
            f"encoded request of {len(encoded)} bytes exceeds the {_MAX_REQUEST_BYTES} byte bound",
            "request-too-large",
        )
    return encoded


def _no_duplicate_keys(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate object key {key!r}")
        seen.add(key)
    return dict(pairs)


def _reject_constant(name):
    raise ValueError(f"non-finite number {name}")


def _strict_json_loads(text):
    """Strict decode: duplicate keys and non-finite constants rejected."""
    return json.loads(
        text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
    )


def _stdout_bound(response_cap_bytes):
    return (
        response_cap_bytes * _B64_WORST_CASE_NUMERATOR // _B64_WORST_CASE_DENOMINATOR
        + _STDOUT_SLACK_BYTES
    )


def _exchange(proc, request, deadline, stdout_bound):
    """Non-blocking pump: write the request, read stdout to EOF or bounds.

    Returns (raw_stdout, outcome) with outcome 'ok', 'timeout' or
    'overflow'. Both directions run through one selector under the
    monotonic deadline, so a worker that never reads stdin cannot wedge
    the write and a worker that never answers cannot outlive the
    deadline; stdout is bounded while reading, never
    read-to-EOF-then-check. stdin is closed once the request is written
    so the worker's read() sees EOF; a worker that exited early turns
    the write into BrokenPipeError, which ends the write side only.
    """
    stdin_fd = proc.stdin.fileno()
    stdout_fd = proc.stdout.fileno()
    os.set_blocking(stdin_fd, False)
    os.set_blocking(stdout_fd, False)
    pending = memoryview(request)
    chunks = []
    total = 0
    eof = False
    selector = selectors.DefaultSelector()
    try:
        if pending:
            selector.register(stdin_fd, selectors.EVENT_WRITE)
        else:
            proc.stdin.close()
        selector.register(stdout_fd, selectors.EVENT_READ)
        while selector.get_map() and not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b"", "timeout"
            for key, _mask in selector.select(remaining):
                fd = key.fd
                if fd == stdin_fd:
                    try:
                        written = os.write(fd, pending)
                    except BrokenPipeError:
                        selector.unregister(fd)
                        continue
                    except (BlockingIOError, InterruptedError):
                        # Spurious writability or a restarted call: the
                        # deadline loop reselects and retries.
                        continue
                    pending = pending[written:]
                    if not pending:
                        selector.unregister(fd)
                        proc.stdin.close()
                else:
                    allowance = stdout_bound + 1 - total
                    if allowance <= 0:
                        return b"", "overflow"
                    try:
                        chunk = os.read(fd, min(allowance, _READ_CHUNK))
                    except (BlockingIOError, InterruptedError):
                        continue
                    if chunk:
                        chunks.append(chunk)
                        total += len(chunk)
                    else:
                        selector.unregister(fd)
                        eof = True
            if total > stdout_bound:
                return b"", "overflow"
    finally:
        selector.close()
    return b"".join(chunks), "ok"


def _decode_envelope(raw):
    """Strictly validate the worker's single JSON response line."""
    if not raw:
        raise AdvisoryTransportError("worker exited without any response", "worker-no-response")
    data = raw[:-1] if raw.endswith(b"\n") else raw
    if not data or b"\n" in data:
        raise AdvisoryTransportError("worker response is not exactly one line", "worker-invalid-response")
    try:
        envelope = _strict_json_loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise AdvisoryTransportError(
            f"worker response is not strict JSON: {type(exc).__name__}",
            "worker-invalid-response",
        ) from None
    if not isinstance(envelope, dict) or envelope.get("schema") != SCHEMA:
        raise AdvisoryTransportError("worker response schema mismatch", "worker-invalid-response")
    status = envelope.get("status")
    if status == "ok":
        if set(envelope) != {"schema", "status", "body_b64"}:
            raise AdvisoryTransportError("worker success envelope keys mismatch", "worker-invalid-response")
        if not isinstance(envelope["body_b64"], str):
            raise AdvisoryTransportError("worker body_b64 must be a string", "worker-invalid-response")
        return envelope
    if status == "error":
        if set(envelope) != {"schema", "status", "error_type", "status_code", "code"}:
            raise AdvisoryTransportError("worker error envelope keys mismatch", "worker-invalid-response")
        error_type = envelope["error_type"]
        code = envelope["code"]
        status_code = envelope["status_code"]
        if not isinstance(error_type, str) or not _ERROR_TYPE_PATTERN.match(error_type):
            raise AdvisoryTransportError("worker error_type is not a stable name", "worker-invalid-response")
        if not isinstance(code, str) or not _CODE_PATTERN.match(code):
            raise AdvisoryTransportError("worker code is not a stable identifier", "worker-invalid-response")
        if status_code is not None and (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise AdvisoryTransportError("worker status_code must be null or 100..599", "worker-invalid-response")
        return envelope
    raise AdvisoryTransportError("worker response status must be ok or error", "worker-invalid-response")


def _envelope_result(envelope, destination, response_cap_bytes):
    """Turn a validated envelope into bytes or a parent-side exception."""
    if envelope["status"] == "ok":
        try:
            body = base64.b64decode(envelope["body_b64"].encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            raise AdvisoryTransportError("worker body is not valid base64", "worker-invalid-response") from None
        if len(body) > response_cap_bytes:
            raise TransportResponseTooLarge(response_cap_bytes)
        return body
    code = envelope["code"]
    if code in ("deadline-exceeded", "socket-timeout"):
        raise TransportDeadlineExceeded(
            "the OSV request exceeded the total monotonic deadline; the worker was terminated"
        )
    if code == "response-too-large":
        raise TransportResponseTooLarge(response_cap_bytes)
    if code in ("http-error", "redirect-refused") and envelope["status_code"] is not None:
        status = envelope["status_code"]
        error = urllib.error.HTTPError(
            destination,
            status,
            f"OSV query worker reported HTTP {status}",
            None,
            None,
        )
        # The error carries tempfile-backed machinery that warns at
        # deallocation unless close() ran; close it here (idempotent for
        # the executor's own close) before handing it up.
        error.close()
        raise error
    if code == "url-error":
        raise urllib.error.URLError("the OSV request to the approved endpoint failed")
    raise AdvisoryTransportError(
        f"the OSV request worker failed with the stable code {code}", code
    )


class DeadlineTransport:
    """Total-deadline OSV transport: one killable worker per POST.

    `post` has the executor's transport signature and semantics: exactly
    one request, no retry, the streaming response cap preserved, and
    failures raised as parent-side exceptions whose types and codes are
    stable. `worker_command` overrides the argv for the child (tests
    inject a fake worker this way); production uses
    `default_worker_command()`.
    """

    def __init__(self, worker_command=None):
        if worker_command is not None and isinstance(worker_command, str):
            raise AdvisoryTransportError(
                "worker_command must be a list of argv strings, never a shell command line",
                "bad-worker-command",
            )
        command = default_worker_command() if worker_command is None else list(worker_command)
        if not command or not all(isinstance(part, str) and part for part in command):
            raise AdvisoryTransportError(
                "worker_command must be a nonempty list of nonempty strings",
                "bad-worker-command",
            )
        self._worker_command = command

    @property
    def worker_command(self):
        return list(self._worker_command)

    def post(self, destination, body, timeout_seconds, response_cap_bytes):
        """One bounded POST through a one-shot worker under a total deadline."""
        if destination != OSV_QUERY_ENDPOINT:
            raise AdvisoryTransportError(
                f"destination {destination!r} is not the one approved endpoint {OSV_QUERY_ENDPOINT!r}",
                "destination-not-approved",
            )
        if not isinstance(body, bytes):
            raise AdvisoryTransportError(
                f"body must be bytes, got {type(body).__name__}", "bad-body"
            )
        if len(body) > _MAX_REQUEST_BODY_BYTES:
            raise AdvisoryTransportError(
                f"request body of {len(body)} bytes exceeds the {_MAX_REQUEST_BODY_BYTES} byte bound",
                "body-too-large",
            )
        timeout_seconds = _positive_number(timeout_seconds)
        response_cap_bytes = _positive_cap(response_cap_bytes)
        _require_posix_safety()
        request = _build_request(destination, body, timeout_seconds, response_cap_bytes)
        bound = _stdout_bound(response_cap_bytes)
        with tempfile.TemporaryDirectory(prefix="mdi-advisory-worker-home-") as home:
            try:
                proc = subprocess.Popen(
                    self._worker_command,
                    cwd=str(_TRUSTED_PARENT_DIR),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=_worker_env(home),
                    start_new_session=True,
                )
            except OSError as exc:
                raise AdvisoryTransportError(
                    f"cannot execute worker {self._worker_command[0]!r}: {exc.strerror or exc}",
                    "worker-spawn-failed",
                ) from exc
            # The total budget anchors after exec succeeds: it bounds the
            # child's runtime, and parent-side fork/exec latency under load
            # must not consume the request budget.
            deadline = time.monotonic() + timeout_seconds
            abnormal = False
            try:
                try:
                    raw, outcome = _exchange(proc, request, deadline, bound)
                    if outcome != "ok":
                        _terminate(proc)
                        if outcome == "timeout":
                            raise TransportDeadlineExceeded(
                                "the OSV request exceeded the total monotonic deadline; "
                                "the worker was terminated"
                            )
                        raise AdvisoryTransportError(
                            f"worker wrote more than the {bound} byte response bound",
                            "worker-output-overflow",
                        )
                    try:
                        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        _terminate(proc)
                    envelope = _decode_envelope(raw)
                    return _envelope_result(envelope, destination, response_cap_bytes)
                except BaseException:
                    abnormal = True
                    raise
            finally:
                # Termination runs on every abnormal path where the leader
                # is still unreaped; a normally finished worker was already
                # reaped above. Both pipes close on every path.
                if abnormal or proc.poll() is None:
                    _terminate(proc)
                _close_pipes(proc)


# -- worker (child side; runs only under the __main__ guard) -----------------


def _emit(stdout, envelope):
    stdout.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")
    stdout.flush()


def _emit_error(stdout, error_type, code, status_code=None):
    _emit(stdout, {
        "schema": SCHEMA,
        "status": "error",
        "error_type": error_type,
        "status_code": status_code,
        "code": code,
    })


def _safe_code(value):
    return value if isinstance(value, str) and _CODE_PATTERN.match(value) else None


def _classify_exception(exc):
    """Map one worker exception to (error_type, code, status_code).

    Only stable names, a numeric status and a stable code leave this
    mapping; message text, headers and everything else about the
    exception are dropped here, permanently.
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        status = code if isinstance(code, int) and not isinstance(code, bool) else None
        stable = "redirect-refused" if status is not None and 300 <= status < 400 else "http-error"
        return "HTTPError", stable, status
    if isinstance(exc, urllib.error.URLError):
        return "URLError", "url-error", None
    if isinstance(exc, TimeoutError):
        return "TimeoutError", "socket-timeout", None
    if isinstance(exc, AdvisoryError):
        return type(exc).__name__, _safe_code(getattr(exc, "code", None)), None
    return "WorkerInternalError", "unexpected-worker-error", None


def _validate_worker_request(envelope):
    """Strict child-side validation; returns (destination, body, timeout, cap).

    The worker refuses anything the parent should never have sent: the
    destination must still be the one approved endpoint, the body must
    decode to a JSON object over the build_query vocabulary within
    bounds, and timeout and cap must be positive and finite. Raises
    ValueError with the stable refusal code as the message.
    """
    if not isinstance(envelope, dict) or envelope.get("schema") != SCHEMA:
        raise ValueError("bad-schema")
    if set(envelope) != {"schema", "destination", "body_b64", "timeout_seconds", "response_cap_bytes"}:
        raise ValueError("bad-schema")
    destination = envelope["destination"]
    if destination != OSV_QUERY_ENDPOINT:
        raise ValueError("destination-not-approved")
    encoded = envelope["body_b64"]
    if not isinstance(encoded, str):
        raise ValueError("bad-schema")
    try:
        body = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        raise ValueError("bad-schema") from None
    if not body or len(body) > _MAX_REQUEST_BODY_BYTES:
        raise ValueError("bad-schema")
    try:
        decoded_body = _strict_json_loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("bad-schema") from None
    # The approved body is exactly what advisories.build_query can emit:
    # a JSON object over the package/version/page_token vocabulary.
    if not isinstance(decoded_body, dict) or not decoded_body:
        raise ValueError("bad-schema")
    if set(decoded_body) - {"package", "version", "page_token"}:
        raise ValueError("bad-schema")
    timeout = envelope["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("bad-schema")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("bad-schema")
    cap = envelope["response_cap_bytes"]
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ValueError("bad-schema")
    return destination, body, float(timeout), cap


def _worker_main(argv, stdin, stdout):
    """Child entry point: read one request, POST once, answer once.

    The trusted mdi parent directory is already on sys.path (the
    script-mode bootstrap at the top of this file put it there); running
    isolated, no environment or cwd path can precede it. The executor's
    transport is imported only once a request has passed validation and
    is actually going out, which also keeps this module free of any
    import cycle with the executor in the parent process.
    """
    if argv != [WORKER_FLAG]:
        return 2
    started = time.monotonic()
    try:
        raw = stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > _MAX_REQUEST_BYTES:
            _emit_error(stdout, "WorkerBadRequest", "bad-request")
            return 0
        data = raw[:-1] if raw.endswith(b"\n") else raw
        envelope = _strict_json_loads(data.decode("utf-8"))
        destination, body, timeout, cap = _validate_worker_request(envelope)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # The validator signals with a stable code as the message; strict
        # JSON failures carry arbitrary text and collapse to bad-request.
        code = exc.args[0] if exc.args and _CODE_PATTERN.match(str(exc.args[0])) else "bad-request"
        _emit_error(stdout, "WorkerBadRequest", code)
        return 0
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        _emit_error(stdout, "TimeoutError", "deadline-exceeded")
        return 0
    from mdi.advisory_executor import UrllibTransport

    try:
        # The socket timeout is the worker's remaining total budget, a
        # best-effort inner bound; the parent's kill enforces the deadline.
        response = UrllibTransport().post(destination, body, remaining, cap)
    except Exception as exc:  # noqa: BLE001 - mapped, never echoed
        error_type, code, status = _classify_exception(exc)
        _emit_error(stdout, error_type, code, status)
        return 0
    _emit(stdout, {
        "schema": SCHEMA,
        "status": "ok",
        "body_b64": base64.b64encode(response).decode("ascii"),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:], sys.stdin, sys.stdout))
