"""Bounded Python runner for the Java usage helper (TASK-21 adapter slice).

run_usage_helper(root, dependencies, *, scope, helper_classpath,
expected_hashes, java_path=None, timeout_seconds=30,
max_output_bytes=16 MiB, heap_mb=128) executes mdi.UsageHelper exactly once
over PRE-STAGED inputs and returns a JSON-safe structured result. It never
stages, compiles, downloads, or invents paths: every path comes from the
caller, and staging itself is a separate, not-yet-implemented slice.

Trusted caller contract (guaranteed by the caller; this module cannot enforce
it): root, every dependency, and every helper classpath component was staged
by trusted code as an immutable regular .jar file or plain directory for the
whole duration of the run, expected_hashes are digests the caller actually
verified against its own staging, and java_path (when supplied) is a trusted
system executable rather than a user or project wrapper.

Public option preflight: validate_usage_options(scope, helper_classpath,
expected_hashes, timeout_seconds=30, max_output_bytes=16 MiB, heap_mb=128,
java_path=None) is the shared, side-effect-free shape check for exactly
those option arguments, the same code run_usage_helper executes first (one
implementation, not a copy). It checks types and values only (positive int
budgets, str scope in production|test, a non-string non-empty classpath
container of non-empty text components, a dict of str keys to lowercase
64-hex sha256 digests, and a supplied java_path that is a text path and
absolute), with no filesystem access, no hashing and no process, so a
caller can refuse clearly invalid configuration before doing expensive
work. It is a shape check and never authorization or an immutability
proof: every path, hash and identity fact is derived again by the runner
at execution time, so helper bytes that changed after a passing preflight
still fail the runner's hash gate, and argument-type errors keep raising
TypeError and ValueError exactly as the runner always did.

Enforced validation, all BEFORE any JVM start (status "rejected", no process):
text paths only; scope exactly production or test; every staged input exists,
has no symlinked component anywhere on its absolute path, and is a regular
.jar file or a directory not named *.jar; no duplicate input after
canonicalization; helper_classpath is an explicit non-empty ordered sequence
of absolute-able regular non-symlink *.jar components with no duplicates,
used verbatim as -cp (no shell, no defaults, no /tmp fallback, no additions);
no component's path, raw or canonical, may contain the classpath separator
or '*' (the JVM would split or wildcard-expand one pinned component into
unlisted, unhashed entries), refused in the pure shape preflight and again
on the canonical path;
expected_hashes maps exactly those components one-to-one to lowercase 64-hex
sha256 digests, and every digest must match the file bytes; a supplied
java_path is absolute, a regular executable file, and outside every staged
input (a java resolved from PATH keeps system symlink aliasing but is held to
the same outside-staged-inputs rule and has its realpath pinned). The java
identity is snapshotted (realpath, stat identity, sha256); --version is never
executed.

Execution containment: fresh allowlist environment with exactly PATH
(absolute components only), HOME=<fresh temp control dir>, LC_ALL=C, and cwd
set to that same control dir; JAVA_TOOL_OPTIONS, JDK_JAVA_OPTIONS,
_JAVA_OPTIONS, CLASSPATH and every other inherited variable are dropped
rather than forwarded. argv is exactly
java -Xmx{heap}m -XX:+ExitOnOutOfMemoryError -cp <validated list>
mdi.UsageHelper --root <canonical> --scope <scope> --dependency <canonical>...
stdout and stderr are bounded WHILE being read (selectors plus os.read with a
shrinking allowance), a monotonic deadline bounds wall time, and the child's
whole process group is SIGKILLed and reaped on timeout, cap, or abnormal
exit, with the reported kill state distinguishing signal sent, group already
absent, and failure to confirm. This run loop mirrors the reviewed
mdi.gitinspect._run semantics locally rather than importing that private
symbol; extracting one shared public bounded-run helper is a deliberate
follow-up for the owning task, not an oversight.

Result: status "complete" (helper exit 0 AND strict report validation
passed), "rejected" (validation refusal above, or helper exit 2),
"failed" (helper exit 1 or any other exit, timeout, output cap, or output
that fails validation), or "unavailable" (no usable java executable, cannot
execute, or platform without process-control safety). The helper report is
accepted only after strict mdi.usage-helper/1 validation: exact key sets at
every level, exact types and limits, inputs matching the validated staged
list in order with the expected roles, refs originating only from analyzed
known inputs with a from_class present in the origin input's class index,
and target owner candidates referencing only analyzed inputs whose class
list contains the target, with duplicate target entries refused rather than
silently merged; malformed JSON, duplicate object keys, unhashable values
in reference positions, over-budget counts, or any foreign shape fail
closed and are never passed through raw. Depth and size are explicit
contracts: payload bytes are bounded by max_output_bytes while read, and
payload nesting depth is bounded by the interpreter's JSON-decoder
recursion limit, with a RecursionError at the decoder or inside validation
normalized to a structured failed result (never a crash, never a
permissive fallback, and nothing beyond RecursionError is caught there).
Helper issues are capped at the helper's own generation ceiling of one
issue per input (64; inspected from UsageHelper.java, not guessed) and the
aggregated public result enforces the same ceiling. Validation work is
linear and count-bounded (mirrored helper budgets checked before any
cross-referencing loop, set-based class indexes, diagnostics capped at 20
problems) and runs under its own Python-side wall budget of
timeout_seconds; the JVM deadline never covers, and is never claimed to
cover, this post-processing. Staged
input identities and classpath digests are re-checked after the run and
changes are flagged as issues; this detects evidence drift, it does not
protect against a same-user adversary racing the run.
"""

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
DEFAULT_HEAP_MB = 128

HELPER_MAIN = "mdi.UsageHelper"
HELPER_SCHEMA = "mdi.usage-helper/1"
RUNNER_SCHEMA = "mdi.usage-helper-runner/1"
SCOPES = ("production", "test")

_REPORT_KEYS = {"schema", "scope", "root", "inputs", "refs",
                "target_owner_candidates", "issues"}
_INPUT_KEYS = {"path", "role", "kind", "size_bytes", "class_count",
               "classes", "analyzed"}
# Optional, present only for multi-release inputs analyzed through derived
# baseline/version views by the helper.
_MR_INPUT_KEYS = {"multi_release", "multi_release_versions", "versioned_classes"}
_REF_KEYS = {"from_class", "to_class", "origin_input"}
_CANDIDATE_KEYS = {"to_class", "owner_candidates"}
_ISSUE_KEYS = {"code", "message", "input"}
_KINDS = {"jar", "directory"}

_DIGEST = re.compile(r"[0-9a-f]{64}")
_CLASS_NAME = re.compile(r"[^\s/\[\];<>]+")

# Budgets mirrored from the helper contract (UsageHelper.java); enforced on
# the parsed payload in a counting pre-pass before any cross-referencing
# loop, so an over-budget claim fails structured and cheap.
_MAX_INPUTS = 64
_MAX_CLASSES_PER_INPUT = 8_192
_MAX_TOTAL_CLASSES = 65_536
_MAX_REFS = 262_144
_MAX_PROBLEMS = 20
_EMPTY_SET = frozenset()

_LIMITATIONS = (
    "staging is not implemented here: a trusted caller pre-stages immutable jars or"
    " directories and passes exact paths plus verified digests; this adapter validates"
    " and runs, it never stages, compiles, or downloads anything",
    "preflight validation plus a post-run recheck detect staged-evidence changes; they"
    " do not protect against a same-user adversary swapping files during the run",
    "java resolution from PATH trusts the caller's PATH; the executable must live"
    " outside staged inputs and its identity (realpath, stat, sha256) is snapshotted"
    " without ever executing --version",
    "the helper classpath is explicit and fully hash-pinned; there is no default"
    " classpath, no /tmp fallback, and no environment-injected CLASSPATH",
    "evidence is static class-usage analysis only: reflection, service loading, and"
    " other runtime-only usage stay invisible (helper-level limitation)",
    "helper-output validation happens in Python after the JVM exits: it is linear"
    " and count-bounded with mirrored budgets and its own timeout_seconds wall"
    " budget; the JVM deadline does not cover this post-processing",
    "exit mapping: helper exit 2 -> rejected, exit 1 or any other exit -> failed,"
    " absent or unusable java -> unavailable, timeout or output cap -> failed",
)


class _Reject(Exception):
    """One refusal reason; always becomes status 'rejected' with an issue."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class _JavaUnavailable(Exception):
    """No usable java executable could be resolved."""


class _SafetyCapError(RuntimeError):
    """Raised when the platform cannot run subprocesses under control."""


# -- small shared helpers (mirroring the reviewed gitinspect pattern) --------


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _require_posix_safety():
    missing = [name for name in ("setsid", "killpg", "set_blocking") if not hasattr(os, name)]
    if missing or not hasattr(signal, "SIGKILL") or os.name != "posix":
        raise _SafetyCapError(f"platform lacks process-control safety: {', '.join(missing)}")
    # Sole-reaper allowlist: SIG_IGN makes the kernel auto-reap (Popen then
    # fabricates rc 0 for real failures), a Python callable may reap
    # concurrently, and None means a foreign handler we cannot inspect.
    if signal.getsignal(signal.SIGCHLD) is not signal.SIG_DFL:
        raise _SafetyCapError(
            "SIGCHLD disposition is not SIG_DFL; concurrent or automatic "
            "reaping would fabricate exit identities and break group "
            "cleanup ownership, so the child is not spawned")


def _sanitized_path(path_value):
    """Child-side PATH: absolute components only, so a child whose cwd is the
    control directory never resolves helper executables relative to it."""
    absolute = [part for part in path_value.split(os.pathsep) if part and os.path.isabs(part)]
    return os.pathsep.join(absolute) or os.defpath


def _first_line(data):
    """Diagnostics only (stderr snippets); never used to parse the report,
    whose bytes must be decoded and validated exactly."""
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line[:240]
    return ""


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


# -- bounded subprocess execution (reviewed semantics, local copy) ----------


@dataclass(frozen=True)
class _RunOutcome:
    """One bounded invocation; outcome is ok, timeout, capped or error, and
    any non-ok outcome carries an honest transport issue, including what the
    group kill actually achieved."""

    outcome: str
    rc: int | None
    stdout: bytes
    stderr: bytes
    kill_state: str | None
    issue: str | None


def _try_kill_group(proc):
    """Attempt SIGKILL of the child's whole process group: 'sent' when the
    signal was accepted, 'absent' when the group had no member left,
    'failed' otherwise. Acceptance proves neither descendant exit nor leader
    reaping; the leader is reaped separately by the caller."""
    pid = proc.pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        # pgid 1 becomes kill(-1): every process of the user. Never signal
        # an unvalidated group id.
        return "refused-invalid-pgid"
    try:
        os.killpg(pid, signal.SIGKILL)
        return "sent"
    except ProcessLookupError:
        return "absent"
    except OSError:
        return "failed"


def _transport_issue(outcome, kill_state, timeout_seconds, max_output_bytes):
    if outcome == "timeout":
        detail = f"helper timed out after {timeout_seconds}s"
    elif outcome == "capped":
        detail = f"helper combined output exceeded max_output_bytes={max_output_bytes}"
    else:
        return None
    suffix = {
        "sent": "; SIGKILL sent to process group",
        "absent": "; process group already absent",
        "refused-invalid-pgid": "; process group id failed validation, no signal sent",
    }.get(kill_state, "; process group termination not confirmed")
    return detail + suffix


def _run(argv, cwd, env, *, timeout_seconds, max_output_bytes):
    """Run the helper once with output bounded while reading, never
    communicate-then-truncate; wall-clock bounded from successful exec."""
    _require_posix_safety()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return _RunOutcome("error", None, b"", b"", None,
                           f"cannot execute {argv[0]}: {exc.strerror or exc}")
    # The timeout budget anchors after exec succeeds: it bounds the child's
    # runtime, and parent-side fork/exec latency under load must not consume
    # the run budget.
    deadline = time.monotonic() + timeout_seconds
    outcome = "ok"
    kill_state = None
    abnormal = False
    buffers = {proc.stdout: [], proc.stderr: []}
    total = 0
    for stream in buffers:
        fd = stream.fileno()
        if isinstance(fd, bool) or not isinstance(fd, int) or fd <= 2:
            kill_state = _try_kill_group(proc)
            return _RunOutcome("error", None, b"", b"", None,
                               f"child stream descriptor {fd!r} failed validation; "
                               f"group termination {kill_state}")
    try:
        try:
            selector = selectors.DefaultSelector()
            try:
                for stream in buffers:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream.fileno(), selectors.EVENT_READ, stream)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        outcome = "timeout"
                        break
                    for key, _mask in selector.select(remaining):
                        stream = key.data
                        allowance = max_output_bytes + 1 - total
                        if allowance <= 0:
                            outcome = "capped"
                            break
                        try:
                            chunk = os.read(stream.fileno(), allowance)
                        except (BlockingIOError, InterruptedError):
                            continue
                        if chunk:
                            buffers[stream].append(chunk)
                            total += len(chunk)
                        else:
                            selector.unregister(stream.fileno())
                    if outcome != "ok":
                        break
                    if total > max_output_bytes:
                        outcome = "capped"
                        break
            finally:
                selector.close()
            if outcome == "ok" and proc.poll() is None:
                try:
                    proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    outcome = "timeout"
        except BaseException:
            abnormal = True
            raise
    finally:
        # Termination is attempted on every abnormal exit path and on timeout
        # or cap even when the leader has already exited, because descendants
        # may still hold the pipes and belong to the group.
        if abnormal or outcome != "ok" or proc.poll() is None:
            kill_state = _try_kill_group(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            kill_state = "failed"
        stdout = b"".join(buffers[proc.stdout])
        stderr = b"".join(buffers[proc.stderr])
        proc.stdout.close()
        proc.stderr.close()
    return _RunOutcome(outcome, proc.returncode, stdout, stderr, kill_state,
                       _transport_issue(outcome, kill_state, timeout_seconds,
                                        max_output_bytes))


# -- staged input and classpath validation -----------------------------------


def _path_hygiene(path):
    """Lexical walk of every component of the absolute path with lstat; the
    first missing or symlinked component is reported, else None. A preflight,
    not atomic adversarial isolation: a racing writer can swap components
    after inspection."""
    stack = []
    head = path
    while True:
        stack.append(head)
        parent = os.path.dirname(head)
        if parent == head:
            break
        head = parent
    for candidate in reversed(stack):
        try:
            mode = os.lstat(candidate).st_mode
        except OSError as exc:
            return f"cannot inspect {candidate}: {exc.strerror or exc}"
        if stat.S_ISLNK(mode):
            return f"refusing symlink in staged path: {candidate}"
    return None


def _text_path(value, what):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise TypeError(f"{what} must be str or os.PathLike, got {type(value).__name__}") from None
    if isinstance(raw, bytes):
        raise TypeError(f"{what} must be a text path, not bytes")
    if not raw:
        raise _Reject(f"invalid-{what}", f"empty path given for {what}")
    return raw


def _classify_staged_input(value, what):
    """Validate one staged input and return (canonical, kind) or refuse."""
    raw = _text_path(value, what)
    path = os.path.abspath(raw)
    problem = _path_hygiene(path)
    if problem:
        raise _Reject(f"invalid-{what}", problem)
    name = os.path.basename(path)
    mode = os.lstat(path).st_mode
    if stat.S_ISREG(mode):
        if not name.endswith(".jar"):
            raise _Reject(f"invalid-{what}",
                          f"input must be a .jar regular file or a plain directory: {path}")
        kind = "jar"
    elif stat.S_ISDIR(mode):
        if name.endswith(".jar"):
            raise _Reject(f"invalid-{what}",
                          f"directory input must not carry a .jar name: {path}")
        kind = "directory"
    else:
        raise _Reject(f"invalid-{what}",
                      f"input must be a .jar regular file or a plain directory: {path}")
    return os.path.realpath(path), kind


def _classpath_delimiter_problem(path):
    """JVM -cp semantics, not path-character taste: one component whose
    path text carries the classpath separator is hash-verified as a single
    file but the JVM splits it into multiple entries, so an unlisted,
    unhashed sibling jar would execute (probed on OpenJDK 21: -cp
    "/x.jar:a.jar" loads a.jar even when the literal combined name is the
    only file that was pinned); a '*' component lets the JVM expand to
    every jar in a directory, again unpinned. Exactly these two characters
    are refused, each for that stated reason, and nothing else."""
    if os.pathsep in path:
        return ("classpath component contains the classpath separator"
                f" {os.pathsep!r}: the JVM -cp would split it into unlisted,"
                f" unhashed entries: {path}")
    if "*" in path:
        return (f"classpath component contains '*' (JVM classpath wildcard"
                f" expansion would load unpinned jars): {path}")
    return None


def _classpath_item_shape(item):
    """Per-component shape checks with no filesystem access."""
    try:
        raw = os.fspath(item)
    except TypeError:
        raise TypeError("classpath components must be str or os.PathLike, got"
                        f" {type(item).__name__}") from None
    if isinstance(raw, bytes):
        raise TypeError("classpath components must be text paths, not bytes")
    if not raw.strip():
        raise _Reject("invalid-classpath", "classpath component is empty")
    problem = _classpath_delimiter_problem(raw)
    if problem:
        raise _Reject("invalid-classpath", problem)


def _classpath_shape(helper_classpath):
    """Container and per-component shape checks with no filesystem access;
    the regular-file, suffix, hygiene and canonical-duplicate rules stay
    with _classpath_components, which inspects the staged filesystem."""
    if isinstance(helper_classpath, (str, bytes)):
        raise TypeError("helper_classpath must be a sequence of paths, not a single"
                        f" {type(helper_classpath).__name__}; pass the explicit"
                        " component list")
    try:
        items = list(helper_classpath)
    except TypeError:
        raise TypeError("helper_classpath must be an iterable of paths, got"
                        f" {type(helper_classpath).__name__}") from None
    if not items:
        raise _Reject("invalid-classpath", "helper_classpath must not be empty")
    for item in items:
        _classpath_item_shape(item)
    return items


def _digests_shape(expected_hashes):
    """Type and digest-format checks with no filesystem access; the
    one-to-one mapping onto the validated classpath stays with
    _expected_digests, which canonicalizes component paths."""
    if not isinstance(expected_hashes, dict):
        raise TypeError("expected_hashes must be dict, got"
                        f" {type(expected_hashes).__name__}")
    for key, digest in expected_hashes.items():
        if not isinstance(key, str):
            raise TypeError("expected_hashes keys must be str, got"
                            f" {type(key).__name__}")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise _Reject("invalid-expected-hashes",
                          f"expected digest for {key} must be a lowercase 64-hex"
                          " sha256 string")


def _java_path_shape(java_path):
    """Pure text/shape checks for a supplied java_path: type, emptiness and
    absoluteness only; existence, regularity, executability and the
    outside-staged-inputs rule stay with _resolve_java at execution time."""
    raw = _text_path(java_path, "java_path")
    if not os.path.isabs(raw):
        raise _Reject("java-rejected",
                      f"java_path must be absolute, got: {raw}")
    return raw


def _validate_option_shapes(scope, helper_classpath, expected_hashes,
                            timeout_seconds, max_output_bytes, heap_mb,
                            java_path):
    """The one shared option-shape validation, executed first by the public
    preflight and by run_usage_helper alike: pure argument checks with no
    filesystem access, no hashing and no process. Returns the materialized
    helper_classpath items, the one re-iterable copy, so a one-shot
    iterable argument is consumed exactly here and never twice."""
    _positive_int(timeout_seconds, "timeout_seconds")
    _positive_int(max_output_bytes, "max_output_bytes")
    _positive_int(heap_mb, "heap_mb")
    if isinstance(scope, bytes) or not isinstance(scope, str):
        raise TypeError(f"scope must be str, got {type(scope).__name__}")
    if scope not in SCOPES:
        raise _Reject("invalid-scope",
                      f"scope must be one of {'|'.join(SCOPES)}, got {scope!r}")
    items = _classpath_shape(helper_classpath)
    _digests_shape(expected_hashes)
    if java_path is not None:
        _java_path_shape(java_path)
    return items


def _classpath_components(helper_classpath):
    """Exact ordered list of canonical .jar components; nothing is added,
    deduplicated by forgiveness, or defaulted."""
    components = []
    seen = set()
    for item in _classpath_shape(helper_classpath):
        path = os.path.abspath(os.fspath(item))
        problem = _path_hygiene(path)
        if problem:
            raise _Reject("invalid-classpath", problem)
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode):
            raise _Reject("invalid-classpath",
                          f"classpath component must be a regular file: {path}")
        if not os.path.basename(path).endswith(".jar"):
            raise _Reject("invalid-classpath",
                          f"classpath components must be .jar files: {path}")
        canonical = os.path.realpath(path)
        # Re-checked on the canonical spelling: canonicalization must never
        # be able to introduce a separator or wildcard the shape preflight
        # refused in the raw argument.
        problem = _classpath_delimiter_problem(canonical)
        if problem:
            raise _Reject("invalid-classpath", problem)
        if canonical in seen:
            raise _Reject("invalid-classpath",
                          f"duplicate classpath component: {canonical}")
        seen.add(canonical)
        components.append(canonical)
    return components


def _expected_digests(expected_hashes, components):
    """One-to-one lowercase sha256 digests for exactly the validated
    components; missing, unexpected, malformed, or ambiguous entries refuse."""
    _digests_shape(expected_hashes)
    by_real = {}
    for key, digest in expected_hashes.items():
        real = os.path.realpath(key)
        if real in by_real:
            raise _Reject("invalid-expected-hashes",
                          f"two expected_hashes entries resolve to one component: {real}")
        by_real[real] = digest
    component_set = set(components)
    for component in components:
        if component not in by_real:
            raise _Reject("missing-expected-hash",
                          f"no expected digest for classpath component: {component}")
    for real in sorted(by_real):
        if real not in component_set:
            raise _Reject("unexpected-expected-hash",
                          f"expected digest for a path not on the helper classpath:"
                          f" {real}")
    return {component: by_real[component] for component in components}


def _resolve_java(java_path, input_paths):
    """Absolute java executable from the trusted PATH or the supplied
    argument, constrained to live outside every staged input. Returns
    (path, realpath, origin)."""
    if java_path is None:
        trusted_path = os.environ.get("PATH") or os.defpath
        located = shutil.which("java", path=trusted_path)
        if located is None:
            raise _JavaUnavailable("java executable not found on PATH")
        path = os.path.abspath(located)
        origin = "path"
    else:
        path = _java_path_shape(java_path)
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise _Reject("java-rejected",
                          f"java_path is not usable: {path}: {exc.strerror or exc}") from None
        if not stat.S_ISREG(mode):
            raise _Reject("java-rejected",
                          f"java_path must be a regular executable file: {path}")
        if not os.access(path, os.X_OK):
            raise _Reject("java-rejected",
                          f"java_path is not executable: {path}")
        origin = "argument"
    real = os.path.realpath(path)
    for staged in input_paths:
        if real == staged or real.startswith(staged + os.sep):
            raise _Reject("java-rejected",
                          "refusing java executable inside staged inputs:"
                          f" {real} under {staged}")
    return path, real, origin


def _tool_snapshot(path, real, origin):
    """Identity of the trusted executable, recorded without executing it.
    System java is commonly an alternatives symlink, so symlinks are allowed
    here (unlike staged inputs) and identity is pinned by realpath plus stat
    plus sha256."""
    snapshot = {"name": "java", "path": path, "realpath": real, "origin": origin,
                "sha256": None, "identity": None}
    try:
        st = os.stat(path)
        snapshot["identity"] = {"dev": st.st_dev, "ino": st.st_ino,
                                "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError:
        return snapshot
    try:
        snapshot["sha256"] = _sha256_file(path)
    except OSError:
        pass
    return snapshot


def _identity(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _child_env(home):
    """Fresh allowlist environment; nothing is inherited, so hostile
    JAVA_TOOL_OPTIONS, JDK_JAVA_OPTIONS, _JAVA_OPTIONS and CLASSPATH values
    (and any other variable) from the caller's environment cannot reach the
    JVM."""
    return {
        "PATH": _sanitized_path(os.environ.get("PATH") or os.defpath),
        "HOME": home,
        "LC_ALL": "C",
    }


# -- strict report validation -------------------------------------------------


def _no_duplicate_keys(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        seen.add(key)
    return dict(pairs)


def _validate_report(report, scope, root, input_paths, deadline):
    """All problems found in a parsed helper payload against the exact
    mdi.usage-helper/1 shape and the validated staged inputs; empty means
    valid. Unknown shapes are never tolerated speculatively.

    Work is count-bounded and linear: the helper's own budgets (input count,
    per-input and total class counts, refs, candidates, per-candidate owner
    counts) are checked in a counting pre-pass that returns before any
    cross-referencing loop when exceeded; class indexes are set-based and
    built once per input; every origin, owner, and issue input is validated
    to be a string BEFORE any dict or set membership, so unhashable or
    malformed values fail structured instead of raising TypeError; the
    diagnostic list is capped; and a monotonic deadline bounds the item
    loops, so a hostile payload cannot turn validation into unbounded
    post-processing. The deadline is a fresh Python-side budget supplied by
    the caller; it is not, and never covers, the JVM wall deadline."""
    problems = []
    suppressed = 0

    def note(message):
        nonlocal suppressed
        if len(problems) < _MAX_PROBLEMS:
            problems.append(message)
        else:
            suppressed += 1

    def done():
        if suppressed:
            problems.append(f"{suppressed} further problems suppressed"
                            f" (diagnostics capped at {_MAX_PROBLEMS})")
        return problems

    if not isinstance(report, dict):
        return [f"top level is {type(report).__name__}, not a JSON object"]
    keys = set(report)
    if keys != _REPORT_KEYS:
        return [f"report keys mismatch (missing={sorted(_REPORT_KEYS - keys)},"
                f" unexpected={sorted(keys - _REPORT_KEYS)})"]
    if report["schema"] != HELPER_SCHEMA:
        note(f"schema is {report['schema']!r}, expected {HELPER_SCHEMA!r}")
    if report["scope"] != scope:
        note(f"scope is {report['scope']!r}, expected {scope!r}")
    if report["root"] != root:
        note(f"root is {report['root']!r}, expected {root!r}")

    inputs = report["inputs"]
    refs = report["refs"]
    candidates = report["target_owner_candidates"]
    helper_issues = report["issues"]

    # Counting pre-pass: mirrored helper budgets, before any expensive loop.
    budget_exceeded = False
    if isinstance(inputs, list):
        if len(inputs) > _MAX_INPUTS:
            note(f"inputs count {len(inputs)} exceeds helper budget {_MAX_INPUTS}")
            budget_exceeded = True
        total_classes = 0
        for entry in inputs:
            names = entry.get("classes") if isinstance(entry, dict) else None
            if not isinstance(names, list):
                continue
            if len(names) > _MAX_CLASSES_PER_INPUT:
                note(f"input class count {len(names)} exceeds helper budget"
                     f" {_MAX_CLASSES_PER_INPUT}")
                budget_exceeded = True
            total_classes += len(names)
        if total_classes > _MAX_TOTAL_CLASSES:
            note(f"total class count {total_classes} exceeds helper budget"
                 f" {_MAX_TOTAL_CLASSES}")
            budget_exceeded = True
    if isinstance(refs, list) and len(refs) > _MAX_REFS:
        note(f"refs count {len(refs)} exceeds helper budget {_MAX_REFS}")
        budget_exceeded = True
    if isinstance(candidates, list):
        if len(candidates) > _MAX_REFS:
            note(f"target_owner_candidates count {len(candidates)} exceeds helper"
                 f" budget {_MAX_REFS}")
            budget_exceeded = True
        for cand in candidates:
            owners = cand.get("owner_candidates") if isinstance(cand, dict) else None
            if isinstance(owners, list) and len(owners) > _MAX_INPUTS:
                note(f"candidate owner count {len(owners)} exceeds helper budget"
                     f" {_MAX_INPUTS}")
                budget_exceeded = True
    # Inspected, not guessed: UsageHelper.java emits at most one issue per
    # input (the multi-release unsupported issue, inside the per-input loop),
    # and inputs are already capped at _MAX_INPUTS, so that is the actual
    # issue-generation ceiling.
    if isinstance(helper_issues, list) and len(helper_issues) > _MAX_INPUTS:
        note(f"issues count {len(helper_issues)} exceeds helper budget"
             f" {_MAX_INPUTS}")
        budget_exceeded = True
    if budget_exceeded:
        return done()

    analyzed = {}
    classes_by_input = {}
    if not isinstance(inputs, list):
        note(f"inputs is {type(inputs).__name__}, not a list")
    else:
        if len(inputs) != len(input_paths):
            note(f"expected {len(input_paths)} inputs, got {len(inputs)}")
        for i, entry in enumerate(inputs):
            if time.monotonic() > deadline:
                note(f"validation wall budget exhausted at input {i} of"
                     f" {len(inputs)}; remaining inputs unchecked")
                return done()
            if not isinstance(entry, dict):
                note(f"input {i} is {type(entry).__name__}, not an object")
                continue
            extra = set(entry) - _INPUT_KEYS
            missing = _INPUT_KEYS - set(entry)
            if missing or (extra - _MR_INPUT_KEYS):
                note(f"input {i} keys mismatch: {sorted(entry)}")
                continue
            if extra and entry.get("multi_release") is not True:
                note(f"input {i} carries multi-release fields without"
                     f" multi_release true")
                continue
            if "multi_release_versions" in entry:
                versions = entry["multi_release_versions"]
                if (not isinstance(versions, list)
                        or not all(type(v) is int and v >= 6 for v in versions)
                        or versions != sorted(set(versions))):
                    note(f"input {i} multi_release_versions is not a sorted"
                         f" list of distinct version ints >= 6")
            if "versioned_classes" in entry:
                versioned = entry["versioned_classes"]
                versions = entry.get("multi_release_versions")
                if (not isinstance(versioned, dict)
                        or sorted(versioned) != sorted(str(v) for v in (versions or []))):
                    note(f"input {i} versioned_classes keys do not match"
                         f" multi_release_versions")
                else:
                    union = set(entry.get("classes") or [])
                    for key, names in versioned.items():
                        if (not isinstance(names, list)
                                or not all(isinstance(n, str)
                                           and _CLASS_NAME.fullmatch(n)
                                           for n in names)
                                or names != sorted(set(names))):
                            note(f"input {i} versioned_classes[{key!r}] is"
                                 f" not a sorted-unique list of class names")
                            break
                        if not set(names) <= union:
                            note(f"input {i} versioned_classes[{key!r}] lists"
                                 f" classes outside the input's class union")
                            break
            expected_role = scope if i == 0 else "upstream"
            if entry["role"] != expected_role:
                note(f"input {i} role is {entry['role']!r},"
                     f" expected {expected_role!r}")
            if not isinstance(entry["kind"], str) or entry["kind"] not in _KINDS:
                note(f"input {i} kind is {entry['kind']!r},"
                     f" expected one of {sorted(_KINDS)}")
            if type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0:
                note(f"input {i} size_bytes is not a non-negative int")
            if type(entry["analyzed"]) is not bool:
                note(f"input {i} analyzed is not a bool")
            names = entry["classes"]
            if (not isinstance(names, list)
                    or not all(isinstance(n, str) and _CLASS_NAME.fullmatch(n)
                               for n in names)):
                note(f"input {i} classes is not a list of class names")
                names = None
            elif names != sorted(set(names)):
                note(f"input {i} classes is not sorted-unique")
            elif type(entry["class_count"]) is not int or entry["class_count"] != len(names):
                note(f"input {i} class_count does not match its classes")
            path = entry["path"]
            if not isinstance(path, str):
                note(f"input {i} path is not a string")
            elif i < len(input_paths):
                if path != input_paths[i]:
                    note(f"input {i} path is {path!r},"
                         f" expected staged {input_paths[i]!r}")
                analyzed[input_paths[i]] = entry["analyzed"]
                if names is not None:
                    classes_by_input[input_paths[i]] = set(names)
                else:
                    classes_by_input[input_paths[i]] = _EMPTY_SET

    if not isinstance(refs, list):
        note(f"refs is {type(refs).__name__}, not a list")
        refs = []
    ref_targets = set()
    for i, ref in enumerate(refs):
        if time.monotonic() > deadline:
            note(f"validation wall budget exhausted at ref {i} of {len(refs)};"
                 " remaining refs unchecked")
            return done()
        if not isinstance(ref, dict) or set(ref) != _REF_KEYS:
            note(f"ref {i} keys mismatch")
            continue
        from_class = ref["from_class"]
        to_class = ref["to_class"]
        for field, value in (("from_class", from_class), ("to_class", to_class)):
            if not isinstance(value, str) or not _CLASS_NAME.fullmatch(value):
                note(f"ref {i} {field} is not a class name")
        if isinstance(to_class, str):
            ref_targets.add(to_class)
        origin = ref["origin_input"]
        if not isinstance(origin, str):
            note(f"ref {i} origin_input is not a string: {origin!r}")
            continue
        if origin not in analyzed:
            note(f"ref {i} origin_input {origin!r} is not a staged input")
            continue
        if analyzed[origin] is not True:
            note(f"ref {i} origin_input {origin!r} is not an analyzed input")
            continue
        if (isinstance(from_class, str)
                and from_class not in classes_by_input.get(origin, _EMPTY_SET)):
            note(f"ref {i} from_class {from_class!r} is not in the class index"
                 f" of its origin input {origin!r}")

    if not isinstance(candidates, list):
        note("target_owner_candidates is"
             f" {type(candidates).__name__}, not a list")
        candidates = []
    candidate_targets = set()
    seen_targets = set()
    for i, cand in enumerate(candidates):
        if time.monotonic() > deadline:
            note(f"validation wall budget exhausted at candidate {i} of"
                 f" {len(candidates)}; remaining candidates unchecked")
            return done()
        if not isinstance(cand, dict) or set(cand) != _CANDIDATE_KEYS:
            note(f"candidate {i} keys mismatch")
            continue
        target = cand["to_class"]
        if not isinstance(target, str) or not _CLASS_NAME.fullmatch(target):
            note(f"candidate {i} to_class is not a class name")
            continue
        if target in seen_targets:
            note(f"duplicate target_owner_candidates entries for {target!r};"
                 " the two owner lists would be ambiguous, refusing to merge"
                 " or drop either silently")
        seen_targets.add(target)
        candidate_targets.add(target)
        owners = cand["owner_candidates"]
        if not isinstance(owners, list):
            note(f"candidate {i} owner_candidates is not a list")
            continue
        owner_strings = [owner for owner in owners if isinstance(owner, str)]
        if len(owner_strings) != len(owners):
            note(f"candidate {i} owner_candidates contains non-string entries")
        if len(owner_strings) != len(set(owner_strings)):
            note(f"candidate {i} owner_candidates has duplicates")
        for owner in owner_strings:
            if owner not in analyzed:
                note(f"candidate {i} owner {owner!r} is not a staged input")
            elif analyzed[owner] is not True:
                note(f"candidate {i} owner {owner!r} is not an analyzed input")
            elif target not in classes_by_input.get(owner, _EMPTY_SET):
                note(f"candidate {i} owner {owner!r} does not contain class"
                     f" {target!r}")
    if candidate_targets != ref_targets:
        note("candidate targets do not match ref targets"
             f" (only-in-candidates={sorted(candidate_targets - ref_targets)},"
             f" only-in-refs={sorted(ref_targets - candidate_targets)})")

    helper_issues = report["issues"]
    if not isinstance(helper_issues, list):
        note(f"issues is {type(helper_issues).__name__}, not a list")
    else:
        for i, entry in enumerate(helper_issues):
            if time.monotonic() > deadline:
                note(f"validation wall budget exhausted at issue {i} of"
                     f" {len(helper_issues)}; remaining issues unchecked")
                return done()
            if not isinstance(entry, dict) or set(entry) != _ISSUE_KEYS:
                note(f"helper issue {i} keys mismatch")
                continue
            if not isinstance(entry["code"], str) or not entry["code"]:
                note(f"helper issue {i} code is not a non-empty string")
            if not isinstance(entry["message"], str):
                note(f"helper issue {i} message is not a string")
            issue_input = entry["input"]
            if not isinstance(issue_input, str):
                note(f"helper issue {i} input is not a string: {issue_input!r}")
            elif issue_input not in analyzed:
                note(f"helper issue {i} input {issue_input!r} is not a staged input")
    return done()


def _recheck_evidence(input_paths, before, components, expected_digests):
    """Post-run re-derivation of staged evidence; returns change notices."""
    changed = []
    for path in input_paths:
        after = _identity(path)
        if after is None:
            changed.append(f"staged input no longer statable after run: {path}")
        elif after != before[path]:
            changed.append(f"staged input identity changed after run: {path}")
    for component in components:
        try:
            digest = _sha256_file(component)
        except OSError:
            changed.append(f"classpath component no longer readable after run:"
                           f" {component}")
            continue
        if digest != expected_digests[component]:
            changed.append(f"classpath component hash changed after run: {component}")
    return changed


# -- public entry point -------------------------------------------------------


def validate_usage_options(scope, helper_classpath, expected_hashes, *,
                           timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                           max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                           heap_mb=DEFAULT_HEAP_MB, java_path=None):
    """No-staged-path option preflight, the shape half of the runner's own
    validation (TASK-21.6).

    Runs exactly the option-shape validation run_usage_helper executes
    first: pure types and values with no filesystem access, no hashing and
    no process, so nonexistent classpath paths are fine here (the runner
    derives every path, hash and identity fact again at execution time).
    Returns the materialized helper_classpath components (the one
    re-iterable copy; a one-shot iterable argument is consumed exactly
    here) when every option is shape-valid, so a caller re-using the
    argument never holds an exhausted iterator, or the first refusal as
    {"code", "message"} carrying the runner's own codes and messages.
    TypeError and ValueError for argument-type errors propagate unchanged,
    exactly as run_usage_helper raises them. A passing result is a shape
    check, never authorization: it proves nothing about file contents,
    which may change before the runner reads them.
    """
    try:
        return _validate_option_shapes(scope, helper_classpath, expected_hashes,
                                       timeout_seconds, max_output_bytes,
                                       heap_mb, java_path)
    except _Reject as exc:
        return {"code": exc.code, "message": exc.message}


def run_usage_helper(root, dependencies, *, scope, helper_classpath,
                     expected_hashes, java_path=None,
                     timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                     max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                     heap_mb=DEFAULT_HEAP_MB):
    """Run mdi.UsageHelper once over pre-staged trusted-immutable inputs.

    Returns a JSON-safe dict with schema, status, scope, root, dependencies,
    helper_classpath, tool, run, report/inputs/refs/target_owner_candidates
    (set only when a report passed strict validation), issues (runner and
    helper, each tagged with its source), limitations and observed_at.
    See the module docstring for the enforced validation and the trusted
    caller contract this deliberately does not replace. The shared option
    preflight (validate_usage_options) runs first, so option-shape
    refusals precede every staged-path check.
    """
    observed_at = _now()
    issues = []

    def finish(status, root_display, dep_canonical, cp_canonical, tool, run_info,
               report=None):
        result = {
            "schema": RUNNER_SCHEMA,
            "status": status,
            "scope": scope,
            "root": root_display,
            "dependencies": dep_canonical,
            "helper_classpath": cp_canonical,
            "tool": tool,
            "run": run_info,
            "report": report,
            "inputs": report["inputs"] if report is not None else None,
            "refs": report["refs"] if report is not None else None,
            "target_owner_candidates": (report["target_owner_candidates"]
                                        if report is not None else None),
            "issues": issues,
            "limitations": _LIMITATIONS,
            "observed_at": observed_at,
        }
        if report is not None:
            helper_entries = report["issues"]
            issues.extend({"source": "helper", **entry}
                          for entry in helper_entries[:_MAX_INPUTS])
            if len(helper_entries) > _MAX_INPUTS:
                # Unreachable after validation (over-ceiling reports fail);
                # enforced here too so the aggregated public result can never
                # grow past the helper's own generation ceiling.
                issues.append({"source": "runner",
                               "code": "helper-issues-truncated",
                               "message": f"{len(helper_entries) - _MAX_INPUTS}"
                                          f" helper issues dropped at the"
                                          f" {_MAX_INPUTS}-entry ceiling"})
        return result

    def refuse(exc, root_display, dep_canonical, cp_canonical):
        issues.append({"source": "runner", "code": exc.code, "message": exc.message})
        return finish("rejected", root_display, dep_canonical, cp_canonical, None, None)

    root_display = None
    dep_canonical = []
    cp_canonical = []
    # Shared option preflight first (the public validate_usage_options is
    # the same check): TypeErrors for argument types propagate exactly as
    # they always did, and a _Reject refuses with no path read at all.
    try:
        cp_items = _validate_option_shapes(scope, helper_classpath,
                                           expected_hashes, timeout_seconds,
                                           max_output_bytes, heap_mb, java_path)
    except _Reject as exc:
        return refuse(exc, None, [], [])
    if isinstance(dependencies, (str, bytes)):
        raise TypeError("dependencies must be an iterable of paths, not a single"
                        f" {type(dependencies).__name__}")
    try:
        dependency_items = list(dependencies)
    except TypeError:
        raise TypeError("dependencies must be an iterable of paths, got"
                        f" {type(dependencies).__name__}") from None

    try:
        root_canonical, _root_kind = _classify_staged_input(root, "root")
    except TypeError:
        raise
    except _Reject as exc:
        return refuse(exc, root_display, dep_canonical, cp_canonical)
    root_display = root_canonical
    for item in dependency_items:
        try:
            canonical, _kind = _classify_staged_input(item, "dependency")
        except TypeError:
            raise
        except _Reject as exc:
            return refuse(exc, root_display, dep_canonical, cp_canonical)
        dep_canonical.append(canonical)
    input_paths = [root_canonical, *dep_canonical]
    if len(set(input_paths)) != len(input_paths):
        duplicates = sorted({p for p in input_paths if input_paths.count(p) > 1})
        return refuse(_Reject("duplicate-input",
                              "staged input path supplied more than once:"
                              f" {', '.join(duplicates)}"),
                      root_display, dep_canonical, cp_canonical)

    try:
        cp_canonical = _classpath_components(cp_items)
    except TypeError:
        raise
    except _Reject as exc:
        return refuse(exc, root_display, dep_canonical, cp_canonical)
    try:
        expected_digests = _expected_digests(expected_hashes, cp_canonical)
    except TypeError:
        raise
    except _Reject as exc:
        return refuse(exc, root_display, dep_canonical, cp_canonical)
    for component in cp_canonical:
        try:
            actual = _sha256_file(component)
        except OSError as exc:
            return refuse(_Reject("invalid-classpath",
                                  f"cannot hash classpath component {component}:"
                                  f" {exc.strerror or exc}"),
                          root_display, dep_canonical, cp_canonical)
        if actual != expected_digests[component]:
            return refuse(_Reject("hash-mismatch",
                                  f"classpath component sha256 {actual} != expected"
                                  f" digest for {component}"),
                          root_display, dep_canonical, cp_canonical)

    try:
        java_path_resolved, java_real, origin = _resolve_java(java_path, input_paths)
    except _JavaUnavailable as exc:
        issues.append({"source": "runner", "code": "java-unavailable", "message": str(exc)})
        return finish("unavailable", root_display, dep_canonical, cp_canonical,
                      None, None)
    except _Reject as exc:
        return refuse(exc, root_display, dep_canonical, cp_canonical)
    tool = _tool_snapshot(java_path_resolved, java_real, origin)

    argv = [java_path_resolved, f"-Xmx{heap_mb}m", "-XX:+ExitOnOutOfMemoryError",
            "-cp", os.pathsep.join(cp_canonical), HELPER_MAIN,
            "--root", root_canonical, "--scope", scope]
    for dep in dep_canonical:
        argv += ["--dependency", dep]

    before = {path: _identity(path) for path in input_paths}
    with tempfile.TemporaryDirectory(prefix="mdi-usage-control-") as control_dir:
        env = _child_env(control_dir)
        try:
            outcome = _run(argv, cwd=control_dir, env=env,
                           timeout_seconds=timeout_seconds,
                           max_output_bytes=max_output_bytes)
        except _SafetyCapError as exc:
            issues.append({"source": "runner", "code": "platform-unsupported",
                           "message": str(exc)})
            return finish("unavailable", root_display, dep_canonical, cp_canonical,
                          tool, None)
    run_info = {
        "argv": argv,
        "returncode": outcome.rc,
        "outcome": outcome.outcome,
        "kill_state": outcome.kill_state,
        "stdout_bytes": len(outcome.stdout),
        "stderr_bytes": len(outcome.stderr),
    }

    report = None
    if outcome.outcome == "error":
        issues.append({"source": "runner", "code": "java-exec-failed",
                       "message": outcome.issue})
        status = "unavailable"
    elif outcome.outcome == "timeout":
        issues.append({"source": "runner", "code": "helper-timeout",
                       "message": outcome.issue})
        status = "failed"
    elif outcome.outcome == "capped":
        issues.append({"source": "runner", "code": "helper-output-capped",
                       "message": outcome.issue})
        status = "failed"
    elif outcome.rc == 0:
        try:
            text = outcome.stdout.decode("utf-8")
            report = json.loads(text, object_pairs_hook=_no_duplicate_keys)
        except (UnicodeDecodeError, ValueError) as exc:
            issues.append({"source": "runner", "code": "helper-output-invalid",
                           "message": "helper output is not strict UTF-8 JSON with"
                                      f" unique keys: {exc}"})
            report = None
        except RecursionError:
            # Size is bounded by max_output_bytes; depth is bounded by the
            # interpreter's JSON-decoder recursion limit, and crossing it is
            # a structured refusal, never a crash or a permissive fallback.
            issues.append({"source": "runner", "code": "helper-output-invalid",
                           "message": "helper output nesting exceeds safe"
                                      " recursion depth; refusing the payload"})
            report = None
        if report is not None:
            validation_deadline = time.monotonic() + timeout_seconds
            try:
                problems = _validate_report(report, scope, root_canonical,
                                            input_paths, validation_deadline)
            except RecursionError:
                problems = ["payload nesting exceeded safe recursion depth"
                            " during validation; refusing the payload"]
            if problems:
                detail = problems[0]
                if len(problems) > 1:
                    detail += f" (+{len(problems) - 1} more)"
                issues.append({"source": "runner", "code": "helper-output-invalid",
                               "message": "helper output failed"
                                          f" {HELPER_SCHEMA} validation: {detail}"})
                report = None
        status = "complete" if report is not None else "failed"
    elif outcome.rc == 2:
        issues.append({"source": "runner", "code": "helper-rejected",
                       "message": "helper refused the inputs (exit 2):"
                                  f" {_first_line(outcome.stderr)}"})
        status = "rejected"
    else:
        issues.append({"source": "runner", "code": "helper-failed",
                       "message": f"helper exited unexpectedly with {outcome.rc}:"
                                  f" {_first_line(outcome.stderr)}"})
        status = "failed"

    changed = _recheck_evidence(input_paths, before, cp_canonical, expected_digests)
    if changed:
        issues.append({"source": "runner", "code": "staged-evidence-changed",
                       "message": "; ".join(changed)})
    return finish(status, root_display, dep_canonical, cp_canonical, tool,
                  run_info, report)
