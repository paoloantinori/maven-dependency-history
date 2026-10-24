"""Tests for mdi.advisory_transport: total-deadline OSV request transport.

No test touches the network, starts an HTTP server, or runs Maven. The
parent side runs against fake workers: small Python scripts written into
a private temporary directory and injected through DeadlineTransport's
worker_command, each speaking the one-line JSON protocol. The real
worker is exercised only on refusal paths that end before any HTTP
(the destination is foreign or the request is malformed) plus in-process
runs with the executor's transport patched out, so the mapping of real
exception classes to envelopes is covered without any socket. The
slow, trickling, blocked and flooding workers make the deadline and
overflow behavior deterministic: every sleep is far longer than the
deadline under test, and every kill claim is verified against the
worker's recorded process group.
"""

import base64
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import warnings
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import advisory_executor, advisory_transport, operations, runstore
from mdi.advisories import OSV_QUERY_ENDPOINT
from mdi.advisory_transport import (
    SCHEMA,
    WORKER_FLAG,
    AdvisoryTransportError,
    DeadlineTransport,
    TransportDeadlineExceeded,
    TransportResponseTooLarge,
    default_worker_command,
)
from mdi.operations import ApprovalError

GROUP = "org.apache.logging.log4j"
ARTIFACT = "log4j-core"
VERSION = "2.14.1"
COORDINATE = f"{GROUP}:{ARTIFACT}"
CANONICAL_BODY = json.dumps(
    {"package": {"ecosystem": "Maven", "name": COORDINATE}, "version": VERSION},
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
EMPTY_PAGE = b"{}"

HOSTILE_ENV = {
    "PYTHONPATH": "/hostile/lib",
    "PYTHONSTARTUP": "/hostile/startup.py",
    "PYTHONHOME": "/hostile/pyhome",
    "http_proxy": "http://127.0.0.1:9",
    "https_proxy": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "ALL_PROXY": "socks5://127.0.0.1:9",
    "REQUESTS_CA_BUNDLE": "/hostile/ca.pem",
    "SSL_CERT_FILE": "/hostile/ca.pem",
    "CURL_CA_BUNDLE": "/hostile/ca.pem",
    "NETRC": "/hostile/.netrc",
    "HOME": "/hostile/home",
}


def resource_warnings(caught):
    return [w for w in caught if issubclass(w.category, ResourceWarning)]


class FakeStdin:
    """Binary stdin stand-in for in-process worker runs."""

    def __init__(self, data):
        self.buffer = _BytesReader(data)


class _BytesReader:
    def __init__(self, data):
        self._data = data

    def read(self, size=-1):
        data, self._data = self._data[:size], self._data[size:]
        return data


class FakeStdout:
    """Text stdout stand-in capturing the worker's single envelope line."""

    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    def flush(self):
        return None

    def value(self):
        return "".join(self.parts)


def envelope_line(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def ok_envelope(body):
    return {"schema": SCHEMA, "status": "ok", "body_b64": base64.b64encode(body).decode("ascii")}


def error_envelope(error_type, code, status_code=None):
    return {
        "schema": SCHEMA,
        "status": "error",
        "error_type": error_type,
        "status_code": status_code,
        "code": code,
    }


class WorkerScript:
    """A fake worker script in a private directory; command() builds argv."""

    def __init__(self, case, name, source):
        self.path = case.workers / f"{name}.py"
        self.path.write_text(source)

    def command(self, *args):
        return [sys.executable, "-I", str(self.path), WORKER_FLAG, *args]


class TransportCase(unittest.TestCase):
    """A private directory for fake workers plus small shared helpers."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mdi-advisory-transport-test-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.workers = self.base / "workers"
        self.workers.mkdir()

    def worker(self, name, source):
        return WorkerScript(self, name, source)

    def pid_path(self, name="worker.pid"):
        return self.base / name

    def read_pid(self, timeout=5.0):
        """Read the pid a worker recorded, tolerating start-up races."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return int(self.pid_path().read_text().strip())
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("worker never recorded its pid")

    def group_gone(self, pid, timeout=15.0):
        """True once the worker's group and its leader pid are fully gone.

        Both checks must clear: killpg(pid, 0) covers the whole group,
        and os.kill(pid, 0) covers the leader itself, because a killed
        but unreaped zombie leader keeps satisfying signal 0 either way,
        so a reap that failed must fail here rather than pass silently.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            group_clear = pid_clear = False
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                group_clear = True
            except PermissionError:
                pass
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid_clear = True
            except PermissionError:
                pass
            if group_clear and pid_clear:
                return True
            time.sleep(0.05)
        return False


# -- shared fake worker sources ------------------------------------------------

# Answers with the raw request bytes as the body: proves the exact
# parent-to-worker protocol the child received.
ECHO_SOURCE = """\
import base64, json, sys
raw = sys.stdin.buffer.read()
sys.stdout.write(json.dumps({
    "schema": "mdi.advisory-transport/1",
    "status": "ok",
    "body_b64": base64.b64encode(raw).decode("ascii"),
}) + "\\n")
sys.stdout.flush()
"""

# Answers with its own environment as the body.
ENV_SOURCE = """\
import base64, json, os, sys
sys.stdin.buffer.read()
body = json.dumps(dict(sorted(os.environ.items()))).encode("utf-8")
sys.stdout.write(json.dumps({
    "schema": "mdi.advisory-transport/1",
    "status": "ok",
    "body_b64": base64.b64encode(body).decode("ascii"),
}) + "\\n")
sys.stdout.flush()
"""

# Records the pid, then sleeps far beyond any deadline; never reads stdin.
BLOCKED_SOURCE = """\
import os, sys, time
with open(sys.argv[2], "w") as handle:
    handle.write(str(os.getpid()))
time.sleep(30)
"""

# Records the pid, then drips one stdout byte at a time for ~8 seconds.
TRICKLE_SOURCE = """\
import os, sys, time
with open(sys.argv[2], "w") as handle:
    handle.write(str(os.getpid()))
for _ in range(400):
    sys.stdout.write("x")
    sys.stdout.flush()
    time.sleep(0.02)
"""

# Records the pid, writes one protocol byte, then hangs; the interrupt
# test patches the parent's first stdout read to raise.
WRITE_THEN_HANG_SOURCE = """\
import os, sys, time
with open(sys.argv[2], "w") as handle:
    handle.write(str(os.getpid()))
sys.stdout.write("{")
sys.stdout.flush()
time.sleep(30)
"""

# Answers with a success envelope carrying exactly n bytes (argv[2]).
BODY_SOURCE = """\
import base64, json, sys
n = int(sys.argv[2])
sys.stdin.buffer.read()
sys.stdout.write(json.dumps({
    "schema": "mdi.advisory-transport/1",
    "status": "ok",
    "body_b64": base64.b64encode(b"z" * n).decode("ascii"),
}) + "\\n")
sys.stdout.flush()
"""

# Answers with the given error envelope fields (type, code, status argv 2..4).
FAIL_SOURCE = """\
import json, sys
sys.stdin.buffer.read()
status = int(sys.argv[4]) if sys.argv[4] != "-" else None
sys.stdout.write(json.dumps({
    "schema": "mdi.advisory-transport/1",
    "status": "error",
    "error_type": sys.argv[2],
    "code": sys.argv[3],
    "status_code": status,
}) + "\\n")
sys.stdout.flush()
"""

# Writes one raw payload verbatim (garbage, two lines, duplicates...);
# the payload travels base64-encoded so newlines survive verbatim.
RAW_SOURCE = """\
import base64, sys
sys.stdin.buffer.read()
sys.stdout.buffer.write(base64.b64decode({payload_b64!r}))
sys.stdout.buffer.flush()
"""

# Records each spawn in a counter file, then answers empty bytes.
COUNTING_BODY_SOURCE = """\
import base64, json, sys
with open(sys.argv[2], "a") as handle:
    handle.write("spawn\\n")
sys.stdin.buffer.read()
sys.stdout.write(json.dumps({
    "schema": "mdi.advisory-transport/1",
    "status": "ok",
    "body_b64": "",
}) + "\\n")
sys.stdout.flush()
"""


# -- parent-side argument validation (no subprocess is spawned) ----------------


class ParentValidationTests(TransportCase):
    def test_foreign_destination_refused_before_spawn(self):
        transport = DeadlineTransport(worker_command=["true"])
        for destination in (
            "https://api.osv.dev/v1/query/",
            "https://api.osv.dev:443/v1/query",
            "https://api.osv.dev/v1/query?x=1",
            "https://evil.example/v1/query",
            "http://api.osv.dev/v1/query",
        ):
            with self.assertRaises(AdvisoryTransportError) as caught:
                transport.post(destination, CANONICAL_BODY, 5, 1024)
            self.assertEqual(caught.exception.code, "destination-not-approved")

    def test_body_must_be_bounded_bytes(self):
        transport = DeadlineTransport(worker_command=["true"])
        for body in ("not bytes", None, 7):
            with self.assertRaises(AdvisoryTransportError) as caught:
                transport.post(OSV_QUERY_ENDPOINT, body, 5, 1024)
            self.assertEqual(caught.exception.code, "bad-body")
        with self.assertRaises(AdvisoryTransportError) as caught:
            transport.post(
                OSV_QUERY_ENDPOINT, b"x" * (advisory_transport._MAX_REQUEST_BODY_BYTES + 1), 5, 1024
            )
        self.assertEqual(caught.exception.code, "body-too-large")

    def test_timeout_must_be_positive_finite_number(self):
        transport = DeadlineTransport(worker_command=["true"])
        for bad in (0, -1, True, "5", float("inf"), float("nan")):
            with self.assertRaises(AdvisoryTransportError) as caught:
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, bad, 1024)
            self.assertEqual(caught.exception.code, "bad-timeout")

    def test_cap_must_be_positive_int(self):
        transport = DeadlineTransport(worker_command=["true"])
        for bad in (0, -1, True, 1.5, "1024"):
            with self.assertRaises(AdvisoryTransportError) as caught:
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, bad)
            self.assertEqual(caught.exception.code, "bad-cap")

    def test_worker_command_must_be_argv_list(self):
        for bad in ("python -I worker.py", [], ["", "x"], [7]):
            with self.assertRaises(AdvisoryTransportError) as caught:
                DeadlineTransport(worker_command=bad)
            self.assertEqual(caught.exception.code, "bad-worker-command")

    def test_default_worker_command_is_this_interpreter_isolated_this_file(self):
        self.assertEqual(
            default_worker_command(),
            [sys.executable, "-I", str(advisory_transport._SCRIPT_PATH), WORKER_FLAG],
        )

    def test_tool_identity_binds_this_source(self):
        import hashlib

        digest = hashlib.sha256(
            Path(advisory_transport.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(advisory_transport.TOOL_IDENTITY["source_sha256"], digest)
        self.assertEqual(advisory_transport.TOOL_IDENTITY["name"], "mdi.advisory_transport")


# -- request protocol and spawn discipline --------------------------------------


class RequestProtocolTests(TransportCase):
    def test_worker_receives_exactly_one_bounded_json_request_line(self):
        echo = self.worker("echo", ECHO_SOURCE)
        transport = DeadlineTransport(worker_command=echo.command())
        raw = transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 7, 65536)
        self.assertTrue(raw.endswith(b"\n"))
        request = json.loads(raw[:-1].decode("utf-8"))
        self.assertEqual(
            set(request),
            {"schema", "destination", "body_b64", "timeout_seconds", "response_cap_bytes"},
        )
        self.assertEqual(request["schema"], SCHEMA)
        self.assertEqual(request["destination"], OSV_QUERY_ENDPOINT)
        self.assertEqual(base64.b64decode(request["body_b64"], validate=True), CANONICAL_BODY)
        self.assertEqual(request["timeout_seconds"], 7.0)
        self.assertEqual(request["response_cap_bytes"], 65536)
        self.assertLessEqual(len(raw), advisory_transport._MAX_REQUEST_BYTES)

    def test_one_spawn_per_call_and_no_retry_after_error(self):
        counter = self.base / "spawns.txt"
        script = COUNTING_BODY_SOURCE.replace("sys.argv[2]", repr(str(counter)))
        counting = self.worker("counting", script)
        transport = DeadlineTransport(worker_command=counting.command())
        self.assertEqual(transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024), b"")
        fail = self.worker(
            "fail", FAIL_SOURCE
        ).command("HTTPError", "http-error", "500")
        failing = DeadlineTransport(worker_command=fail)
        with self.assertRaises(urllib.error.HTTPError):
            failing.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
        self.assertEqual(counter.read_text(), "spawn\n")
        self.assertEqual(transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024), b"")
        self.assertEqual(counter.read_text(), "spawn\nspawn\n")


# -- success, exact cap and error-envelope mapping ------------------------------


class EnvelopeMappingTests(TransportCase):
    def failing_transport(self, error_type, code, status_code):
        script = self.worker("fail", FAIL_SOURCE)
        argv = script.command(error_type, code, "-" if status_code is None else str(status_code))
        return DeadlineTransport(worker_command=argv)

    def test_body_at_exact_cap_passes_through_byte_equal(self):
        body = self.worker("body", BODY_SOURCE)
        transport = DeadlineTransport(worker_command=body.command(str(64)))
        self.assertEqual(
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 64), b"z" * 64
        )

    def test_body_over_cap_raises_response_too_large(self):
        body = self.worker("body", BODY_SOURCE)
        transport = DeadlineTransport(worker_command=body.command(str(65)))
        with self.assertRaises(TransportResponseTooLarge) as caught:
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 64)
        self.assertEqual(caught.exception.code, "response-too-large")
        self.assertIsInstance(caught.exception, advisory_transport.AdvisoryTransportError)

    def test_http_error_envelope_raises_closed_httperro_with_status_only(self):
        transport = self.failing_transport("HTTPError", "http-error", 500)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
            gc.collect()
        error = raised.exception
        self.assertEqual(error.code, 500)
        self.assertEqual(str(error), "HTTP Error 500: OSV query worker reported HTTP 500")
        self.assertTrue(error.fp.closed)
        self.assertEqual(resource_warnings(caught), [])

    def test_redirect_envelope_maps_to_executor_redirect_refusal(self):
        transport = self.failing_transport("HTTPError", "redirect-refused", 302)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
        error = raised.exception
        error.close()
        self.assertEqual(error.code, 302)
        self.assertEqual(
            advisory_executor._redirect_refusal(error),
            {"status_code": 302, "followed": False},
        )

    def test_url_error_envelope_raises_urlerror_with_fixed_reason(self):
        transport = self.failing_transport("URLError", "url-error", None)
        with self.assertRaises(urllib.error.URLError) as raised:
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
        self.assertNotIn("osv.dev", str(raised.exception))

    def test_deadline_and_socket_timeout_envelopes_raise_stable_timeout(self):
        for code in ("deadline-exceeded", "socket-timeout"):
            transport = self.failing_transport("TimeoutError", code, None)
            with self.assertRaises(TransportDeadlineExceeded) as raised:
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
            self.assertIsInstance(raised.exception, TimeoutError)
            self.assertEqual(raised.exception.code, "deadline-exceeded")

    def test_worker_reported_too_large_maps_to_response_too_large(self):
        transport = self.failing_transport("ResponseTooLarge", "response-too-large", None)
        with self.assertRaises(TransportResponseTooLarge):
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)

    def test_stable_worker_codes_map_to_advisory_transport_error(self):
        for code in ("bad-request", "bad-schema", "destination-not-approved", "unexpected-worker-error"):
            transport = self.failing_transport("WorkerBadRequest", code, None)
            with self.assertRaises(AdvisoryTransportError) as caught:
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("osv.dev/v1", str(caught.exception))


class ProtocolFailureTests(TransportCase):
    def post_raw(self, payload_text):
        encoded = base64.b64encode(payload_text.encode("utf-8")).decode("ascii")
        script = self.worker("raw", RAW_SOURCE.format(payload_b64=encoded))
        transport = DeadlineTransport(worker_command=script.command())
        with self.assertRaises(AdvisoryTransportError) as caught:
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
        return caught.exception

    def test_silent_worker_is_no_response(self):
        silent = self.worker(
            "silent", "import sys\nsys.stdin.buffer.read()\n"
        )
        transport = DeadlineTransport(worker_command=silent.command())
        with self.assertRaises(AdvisoryTransportError) as caught:
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
        self.assertEqual(caught.exception.code, "worker-no-response")

    def test_garbage_is_invalid_response(self):
        self.assertEqual(self.post_raw("not json\\n").code, "worker-invalid-response")

    def test_two_envelope_lines_are_invalid(self):
        self.assertEqual(
            self.post_raw(envelope_line(ok_envelope(b"{}")) + envelope_line(ok_envelope(b"{}"))).code,
            "worker-invalid-response",
        )

    def test_wrong_schema_is_invalid_response(self):
        wrong = ok_envelope(b"{}")
        wrong["schema"] = "mdi.someone-else/9"
        self.assertEqual(self.post_raw(envelope_line(wrong)).code, "worker-invalid-response")

    def test_duplicate_keys_are_invalid_response(self):
        self.assertEqual(
            self.post_raw('{"schema":"mdi.advisory-transport/1","schema":"x"}\\n').code,
            "worker-invalid-response",
        )

    def test_envelope_shape_is_strictly_validated(self):
        cases = [
            ok_envelope(b"x") | {"extra": 1},
            error_envelope("HTTPError", "http-error", "503"),
            error_envelope("HTTPError", "http-error", 99),
            error_envelope("HTTPError", "http-error", True),
            error_envelope("9bad", "http-error", None),
            error_envelope("HTTPError", "HTTP-ERROR", None),
            {"schema": SCHEMA, "status": "maybe"},
        ]
        for envelope in cases:
            self.assertEqual(
                self.post_raw(envelope_line(envelope)).code, "worker-invalid-response"
            )

    def test_bad_base64_body_is_invalid_response(self):
        envelope = ok_envelope(b"")
        envelope["body_b64"] = "not valid base64!!"
        self.assertEqual(self.post_raw(envelope_line(envelope)).code, "worker-invalid-response")


# -- deadline, overflow and kill discipline -------------------------------------


class DeadlineTests(TransportCase):
    """Slow, trickling, blocked and flooding workers; every kill verified."""

    def post_deadline(self, command, timeout=0.6, **over):
        transport = DeadlineTransport(worker_command=command)
        kwargs = {"timeout_seconds": timeout, "response_cap_bytes": 1024}
        kwargs.update(over)
        start = time.monotonic()
        with self.assertRaises(TransportDeadlineExceeded) as caught:
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, **kwargs)
        return caught.exception, time.monotonic() - start

    def assert_worker_group_dead(self, pid):
        self.assertTrue(
            self.group_gone(pid),
            f"worker process group {pid} still has members after post raised",
        )

    def test_slow_worker_killed_at_deadline_and_group_reaped(self):
        blocked = self.worker("blocked", BLOCKED_SOURCE)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            exception, elapsed = self.post_deadline(blocked.command(str(self.pid_path())))
            gc.collect()
        self.assertIsInstance(exception, TimeoutError)
        self.assertLess(elapsed, 8.0)
        self.assertGreaterEqual(elapsed, 0.6)
        self.assertEqual(resource_warnings(caught), [])
        self.assert_worker_group_dead(self.read_pid())

    def test_incremental_drip_cannot_outlive_the_deadline(self):
        trickle = self.worker("trickle", TRICKLE_SOURCE)
        exception, elapsed = self.post_deadline(trickle.command(str(self.pid_path())))
        self.assertIsInstance(exception, TimeoutError)
        # The worker drips for ~8 seconds; the exchange died at the deadline.
        self.assertLess(elapsed, 8.0)
        self.assert_worker_group_dead(self.read_pid())

    def test_blocked_worker_never_reading_stdin_cannot_wedge_the_request_write(self):
        blocked = self.worker("blocked", BLOCKED_SOURCE)
        # Big enough that the encoded request cannot fit a Linux pipe
        # buffer, so a blocking write would deadlock against the worker
        # that never reads; the pump must survive and hit the deadline.
        big_body = b'{"package":{"ecosystem":"Maven","name":"' + b"g:a," * 16000 + b'"}}'
        self.assertGreater(len(big_body), 48 * 1024)
        self.assertLessEqual(len(big_body), advisory_transport._MAX_REQUEST_BODY_BYTES)
        transport = DeadlineTransport(worker_command=blocked.command(str(self.pid_path())))
        start = time.monotonic()
        with self.assertRaises(TransportDeadlineExceeded):
            transport.post(OSV_QUERY_ENDPOINT, big_body, 0.6, 1024)
        self.assertLess(time.monotonic() - start, 8.0)
        self.assert_worker_group_dead(self.read_pid())

    def test_flooding_worker_is_cut_at_the_stdout_bound(self):
        pid = self.pid_path()
        flood = self.worker(
            "flood",
            "import os, sys\n"
            f"with open({str(pid)!r}, 'w') as handle: handle.write(str(os.getpid()))\n"
            "sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(b\"A\" * (4 * 1024 * 1024))\nsys.stdout.buffer.flush()\n"
        )
        transport = DeadlineTransport(worker_command=flood.command())
        start = time.monotonic()
        with self.assertRaises(AdvisoryTransportError) as caught:
            transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
        self.assertEqual(caught.exception.code, "worker-output-overflow")
        self.assertLess(time.monotonic() - start, 8.0)
        self.assert_worker_group_dead(self.read_pid())

    def test_keyboard_interrupt_kills_and_reaps_then_propagates(self):
        hang = self.worker("hang", WRITE_THEN_HANG_SOURCE)
        transport = DeadlineTransport(worker_command=hang.command(str(self.pid_path())))
        # The interrupt fires when the pump reads the worker's protocol
        # byte, so the child has certainly recorded its pid and is alive;
        # every other read (including Popen's exec-status pipe) passes
        # through to the real os.read.
        real_read = os.read

        def read_or_interrupt(fd, count):
            data = real_read(fd, count)
            if data == b"{":
                raise KeyboardInterrupt
            return data

        with mock.patch("os.read", read_or_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 1024)
        self.assert_worker_group_dead(self.read_pid())


# -- hostile inherited environment --------------------------------------------


class HostileEnvTests(TransportCase):
    def test_child_environment_is_exactly_the_allowlist(self):
        env = self.worker("env", ENV_SOURCE)
        transport = DeadlineTransport(worker_command=env.command())
        with mock.patch.dict(os.environ, HOSTILE_ENV):
            observed = json.loads(
                transport.post(OSV_QUERY_ENDPOINT, CANONICAL_BODY, 5, 65536)
            )
        self.assertEqual(set(observed), {"PATH", "HOME", "LC_ALL", "TZ"})
        self.assertEqual(observed["LC_ALL"], "C")
        self.assertEqual(observed["TZ"], "UTC")
        for part in observed["PATH"].split(os.pathsep):
            if part:
                self.assertTrue(os.path.isabs(part), f"relative PATH component {part!r}")
        self.assertNotEqual(observed["HOME"], "/hostile/home")
        for value in observed.values():
            self.assertNotIn("/hostile", value)
            self.assertNotIn("127.0.0.1", value)


# -- worker internals (in-process; the executor transport is patched out) ------


class WorkerUnitTests(TransportCase):
    def run_worker(self, request, argv=None):
        stdout = FakeStdout()
        code = advisory_transport._worker_main(
            argv if argv is not None else [WORKER_FLAG], FakeStdin(request), stdout
        )
        return code, stdout.value()

    def request_bytes(self, **over):
        fields = {
            "schema": SCHEMA,
            "destination": OSV_QUERY_ENDPOINT,
            "body_b64": base64.b64encode(CANONICAL_BODY).decode("ascii"),
            "timeout_seconds": 5.0,
            "response_cap_bytes": 1024,
        }
        fields.update(over)
        return json.dumps(fields).encode("utf-8") + b"\n"

    def parsed(self, text):
        self.assertTrue(text.endswith("\n"))
        return json.loads(text[:-1])

    def closed_httperro(self, code):
        error = urllib.error.HTTPError(OSV_QUERY_ENDPOINT, code, "upstream", None, None)
        error.close()
        return error

    def test_refuses_wrong_argv_without_any_output(self):
        code, text = self.run_worker(b"", argv=[])
        self.assertEqual(code, 2)
        self.assertEqual(text, "")

    def test_refuses_unparseable_or_oversized_request(self):
        for raw in (b"", b"not json\n", b"x" * (advisory_transport._MAX_REQUEST_BYTES + 1)):
            code, text = self.run_worker(raw)
            self.assertEqual(code, 0)
            self.assertEqual(self.parsed(text), error_envelope("WorkerBadRequest", "bad-request"))

    def test_refuses_foreign_destination_with_stable_code(self):
        code, text = self.run_worker(self.request_bytes(destination="https://evil.example/"))
        self.assertEqual(code, 0)
        self.assertEqual(
            self.parsed(text),
            error_envelope("WorkerBadRequest", "destination-not-approved"),
        )

    def test_refuses_malformed_request_fields(self):
        def body_of(payload):
            return base64.b64encode(payload).decode("ascii")

        bad = [
            self.request_bytes(schema="mdi.someone-else/9"),
            self.request_bytes(extra=1),
            json.dumps({"schema": SCHEMA}).encode() + b"\n",
            self.request_bytes(body_b64="not base64!!"),
            self.request_bytes(body_b64=body_of(b"")),  # empty body
            self.request_bytes(body_b64=body_of(b"not json")),
            self.request_bytes(body_b64=body_of(b'"just a string"')),
            self.request_bytes(body_b64=body_of(b'{"injected": true}')),
            self.request_bytes(timeout_seconds="5"),
            self.request_bytes(timeout_seconds=True),
            self.request_bytes(timeout_seconds=0),
            self.request_bytes(timeout_seconds=-3),
            self.request_bytes(response_cap_bytes=1.5),
            self.request_bytes(response_cap_bytes=True),
            self.request_bytes(response_cap_bytes=0),
        ]
        for raw in bad:
            code, text = self.run_worker(raw)
            self.assertEqual(self.parsed(text)["code"], "bad-schema", f"for request {raw!r}")

    def test_accepts_page_token_vocabulary_in_body(self):
        body = json.dumps(
            {"package": {"ecosystem": "Maven", "name": COORDINATE}, "version": VERSION,
             "page_token": "opaque-token"},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        recorded = {}

        class FakeUrllibTransport:
            def post(self, destination, request_body, timeout_seconds, response_cap_bytes):
                recorded["body"] = request_body
                return b"{}"

        with mock.patch.object(advisory_executor, "UrllibTransport", FakeUrllibTransport):
            code, text = self.run_worker(
                self.request_bytes(body_b64=base64.b64encode(body).decode())
            )
        self.assertEqual(self.parsed(text), ok_envelope(b"{}"))
        self.assertEqual(recorded["body"], body)

    def test_refuses_non_finite_timeout_constant_before_field_checks(self):
        # json.dumps spells infinity "Infinity", which the strict loader
        # rejects as a non-finite constant before any field validation.
        code, text = self.run_worker(self.request_bytes(timeout_seconds=float("inf")))
        self.assertEqual(self.parsed(text)["code"], "bad-request")

    def test_exhausted_budget_answers_deadline_without_network(self):
        with mock.patch(
            "mdi.advisory_transport.time.monotonic", side_effect=[0.0, 100.0]
        ):
            code, text = self.run_worker(self.request_bytes(timeout_seconds=5.0))
        self.assertEqual(code, 0)
        self.assertEqual(
            self.parsed(text), error_envelope("TimeoutError", "deadline-exceeded")
        )

    def post_with_fake_transport(self, exc=None, body=b'{"vulns": []}'):
        recorded = {}

        class FakeUrllibTransport:
            def post(self, destination, request_body, timeout_seconds, response_cap_bytes):
                recorded["call"] = (
                    destination,
                    request_body,
                    timeout_seconds,
                    response_cap_bytes,
                )
                if exc is not None:
                    raise exc
                return body

        with mock.patch.object(advisory_executor, "UrllibTransport", FakeUrllibTransport):
            code, text = self.run_worker(self.request_bytes())
        return code, self.parsed(text), recorded.get("call")

    def test_success_envelope_carries_body_and_transport_gets_exact_request(self):
        code, envelope, call = self.post_with_fake_transport()
        self.assertEqual(code, 0)
        self.assertEqual(envelope, ok_envelope(b'{"vulns": []}'))
        self.assertEqual(
            call,
            (OSV_QUERY_ENDPOINT, CANONICAL_BODY, mock.ANY, 1024),
        )
        self.assertTrue(0 < call[2] <= 5.0)

    def test_http_errors_map_to_status_only(self):
        for status, expected_code in ((302, "redirect-refused"), (500, "http-error")):
            code, envelope, _ = self.post_with_fake_transport(
                exc=self.closed_httperro(status)
            )
            self.assertEqual(code, 0)
            self.assertEqual(
                envelope,
                error_envelope("HTTPError", expected_code, status),
            )

    def test_url_and_socket_errors_map_to_stable_codes(self):
        cases = [
            (urllib.error.URLError("name or service not known: sekret"), "URLError", "url-error", None),
            (TimeoutError("timed out reading sekret"), "TimeoutError", "socket-timeout", None),
            (
                advisory_executor.ResponseTooLarge(129, 128),
                "ResponseTooLarge",
                "response-too-large",
                None,
            ),
            (ValueError("surprising detail: sekrit"), "WorkerInternalError", "unexpected-worker-error", None),
        ]
        for exc, error_type, code, status in cases:
            _, envelope, _ = self.post_with_fake_transport(exc=exc)
            self.assertEqual(
                envelope, error_envelope(error_type, code, status), f"for {type(exc).__name__}"
            )
            # No exception message text may cross the boundary.
            self.assertNotIn("sekret", json.dumps(envelope))
            self.assertNotIn("sekrit", json.dumps(envelope))

    def test_no_mdi_import_happens_before_the_request_validates(self):
        # A request that fails validation must answer without the executor
        # transport ever being imported by the worker path.
        with mock.patch.dict("sys.modules", {"mdi.advisory_executor": None}):
            code, text = self.run_worker(self.request_bytes(destination="https://evil.example/"))
        self.assertEqual(self.parsed(text)["code"], "destination-not-approved")


# -- the real worker as a subprocess (refusals only; no HTTP possible) ---------


class RealWorkerTests(TransportCase):
    def run_real_worker(self, request, argv=(WORKER_FLAG,), env=None):
        return subprocess.run(
            [sys.executable, "-I", str(advisory_transport._SCRIPT_PATH), *argv],
            input=request,
            capture_output=True,
            timeout=30,
            env=env,
        )

    def test_real_worker_refuses_foreign_destination(self):
        request = advisory_transport._build_request(
            "https://evil.example/v1/query", CANONICAL_BODY, 5, 1024
        )
        done = self.run_real_worker(request)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(
            json.loads(done.stdout),
            error_envelope("WorkerBadRequest", "destination-not-approved"),
        )

    def test_real_worker_refuses_garbage(self):
        done = self.run_real_worker(b"not json\n")
        self.assertEqual(json.loads(done.stdout)["code"], "bad-request")

    def test_real_worker_without_flag_does_nothing(self):
        done = self.run_real_worker(b"", argv=())
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, b"")

    def test_hostile_pythonpath_cannot_shadow_the_trusted_mdi_import(self):
        hostile = self.base / "hostile-site"
        (hostile / "mdi").mkdir(parents=True)
        (hostile / "mdi" / "__init__.py").write_text(
            'raise RuntimeError("hostile mdi package loaded")\n'
        )
        (hostile / "mdi" / "advisories.py").write_text(
            'raise RuntimeError("hostile mdi.advisories loaded")\n'
        )
        request = advisory_transport._build_request(
            "https://evil.example/v1/query", CANONICAL_BODY, 5, 1024
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(hostile)
        done = self.run_real_worker(request, env=env)
        self.assertEqual(done.returncode, 0)
        # The refusal came from the trusted module: the hostile package
        # never loaded, or its RuntimeError would have killed the child.
        self.assertEqual(
            json.loads(done.stdout),
            error_envelope("WorkerBadRequest", "destination-not-approved"),
        )
        self.assertNotIn(b"hostile", done.stderr)


# -- executor integration (read-only use of the existing executor) --------------


class ExecutorIntegrationTests(TransportCase):
    def setUp(self):
        super().setUp()
        store_dir = tempfile.mkdtemp(prefix="mdi-advisory-exec-", dir=str(self.base))
        self.store = runstore.RunStore.create(store_dir, query=f"{COORDINATE}:{VERSION}")
        proposal = advisory_executor.advisory_proposal(
            self.store, GROUP, ARTIFACT, VERSION
        )
        self.operation_id = proposal["id"]
        operations.propose(self.store, proposal, self.revision())
        self.approval = {
            "run_id": self.store.run_id,
            "operation_id": self.operation_id,
            "scope": proposal["scope"],
            "approved": True,
            "approved_by": "host-operator",
            "reference": "host chat test",
        }

    def revision(self):
        return self.store.load()["revision"]

    def transport(self, script, *args):
        return DeadlineTransport(worker_command=script.command(*args))

    def test_executor_executes_one_page_through_the_deadline_transport(self):
        body = self.worker("body", BODY_SOURCE.replace('b"z" * n', repr(EMPTY_PAGE)))
        outcome = advisory_executor.execute_advisory(
            self.store,
            self.operation_id,
            self.approval,
            expected_revision=self.revision(),
            transport=self.transport(body, "0"),
            timeout_seconds=5,
        )
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["result"]["status"], "no-known-matches")
        evidence = outcome["report"]["evidence"][-1]
        self.assertEqual(evidence["payload"]["response"]["bytes"], len(EMPTY_PAGE))

    def test_executor_records_deadline_failure_with_consent_spent(self):
        blocked = self.worker("blocked", BLOCKED_SOURCE)
        transport = DeadlineTransport(
            worker_command=blocked.command(str(self.pid_path()))
        )
        outcome = advisory_executor.execute_advisory(
            self.store,
            self.operation_id,
            self.approval,
            expected_revision=self.revision(),
            transport=transport,
            timeout_seconds=0.5,
        )
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(outcome["result"]["status"], "transport-error")
        self.assertEqual(outcome["result"]["error_type"], "TransportDeadlineExceeded")
        self.assertEqual(outcome["report"]["operations"][-1]["state"], "finished")
        replay = DeadlineTransport(
            worker_command=blocked.command(str(self.pid_path()))
        )
        with self.assertRaises(ApprovalError):
            advisory_executor.execute_advisory(
                self.store,
                self.operation_id,
                self.approval,
                expected_revision=self.revision(),
                transport=replay,
                timeout_seconds=0.5,
            )

    def test_executor_records_worker_too_large_with_stable_reason(self):
        fail = self.worker("fail", FAIL_SOURCE).command(
            "ResponseTooLarge", "response-too-large", "-"
        )
        outcome = advisory_executor.execute_advisory(
            self.store,
            self.operation_id,
            self.approval,
            expected_revision=self.revision(),
            transport=DeadlineTransport(worker_command=fail),
            timeout_seconds=5,
        )
        self.assertEqual(outcome["outcome"], "failed")
        failure = outcome["report"]["evidence"][-1]["payload"]["failure"]
        self.assertEqual(failure["error_type"], "TransportResponseTooLarge")
        self.assertEqual(failure["reason"], "response-too-large")


if __name__ == "__main__":
    unittest.main()
