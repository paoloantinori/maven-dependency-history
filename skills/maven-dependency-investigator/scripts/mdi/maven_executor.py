"""Consent-gated bounded execution of one approved Maven operation.

`execute_maven` is the only public execution entry point: it takes a
RunStore, the id of a proposed maven operation, and the host approval
record, spawns the approved argv vector exactly once in the approved
working directory, and persists the run evidence plus the finished
operation. `maven_proposal` builds an inert proposal bound to this
module; `observe_executable` and `observe_inputs` are the identity
helpers both sides share. There is deliberately no entry point that
executes an already consumed operation or a free-standing authorization
descriptor: renewal after any outcome requires a new proposal with fresh
approval, and the descriptor `operations.consume_approval` returns
authorizes nothing by itself.

Policy checks run before any commit, so an operation failing one is left
proposed and its single-use consent is never spent: the scope must be
kind "maven" with an argv vector whose first item is an absolute path
(never a PATH name this module would have to resolve, never a shell
string), an absolute working directory that exists, at least one pinned
input identity (a Maven invocation always consults project files; an
empty list is refused where the pure-lookup advisory executor demands
one), a tool identity carrying exactly this module's binding plus the
pinned executable identity, and a disclosure holding this module's
unsandboxed-Maven statements plus a downloads-destinations line naming
either the explicit approved destinations or the explicit fact that they
cannot be enumerated. `maven_proposal` composes that disclosure from a
structured `network_destinations` argument; the executor re-derives the
declaration from the persisted disclosure with a strict grammar, so a
hand-edited clause never runs.

Observation before consumption: every pinned input is re-captured
bounded (symlink-final components refused, regular files only, digest
set only for complete captures) and the approved executable is
re-inspected and re-hashed without ever being executed, then both are
compared type-sensitively against the durable record inside
`operations.consume_approval`, which commits proposed->consumed plus the
started audit atomically before returning. A missing or changed input or
tool, a mistyped or sibling approval, a stale revision, and a replay of
a consumed, denied or finished operation therefore all raise before any
commit and launch nothing. After the commit the record is reloaded from
the store at the committed revision and argv, cwd and identities are
re-derived from that durable record alone, never from the in-memory
descriptor or the caller's arguments. These re-derivations are
qualifications, not isolation: a same-user process racing the filesystem
between observation and spawn is detected only as changed evidence after
the run, never prevented.

Execution is deliberately unglorified process control, mirroring the
reviewed `mdi.gitinspect._run`/`mdi.usage._run` semantics locally (a
shared public bounded-run helper remains the owning task's follow-up):
`subprocess.Popen` with `shell=False`, an absolute argv[0], stdin
devnull, and a fresh allowlist environment of exactly PATH (absolute
components only), HOME (a fresh owner-only temporary control directory,
removed after the run), LC_ALL and one executor-set option:
JAVA_TOOL_OPTIONS pins -Duser.home to that control directory, double
quoted because the JVM tokenizer honors quotes there (verified against
OpenJDK 21: a spaced path stays one literal token and option-shaped
directory text cannot inject options, while a quote-bearing temporary
base is refused before consent since an embedded quote would terminate
the quoted span). A JVM resolves user.home from the account database
and not from the HOME variable (verified against OpenJDK 21: HOME=temp
leaves user.home at the real home), so the pin, not the sanitized HOME,
is what keeps a compliant real Maven from reading the user's real
~/.m2/settings.xml and default ~/.m2/repository; the claim is exactly
that a JVM honoring executor options, absent later overrides, resolves
user.home inside the control HOME, and never a settings sandbox, since
an explicit -Duser.home in the approved command, a project
.mvn/jvm.config or any unsandboxed project or launcher code can
supersede the pin. Every inherited variable, including MAVEN_OPTS,
MAVEN_DEBUG_OPTS, MAVEN_ARGS, a hostile JAVA_TOOL_OPTIONS,
JDK_JAVA_OPTIONS, _JAVA_OPTIONS, CLASSPATH, MAVEN_CONFIG and any proxy
or credential variable, is dropped or overwritten rather than
forwarded, so inherited JVM or Maven option injection cannot reach the
child; the approved argv is never modified to achieve any of this. The
child starts a
new session; stdout and stderr share one bounded budget enforced while
reading (selectors plus os.read with a shrinking allowance, never
communicate-then-truncate); a finite monotonic deadline bounds total
wall time anchored after exec succeeds (NaN and infinities are refused
by validation before consent is spent, never reaching the deadline
arithmetic); and cleanup reasons one ownership lifecycle for the
leader: alive, exited-unreaped, or lost. The exit is observed with
waitid(P_PID, WEXITED | WNOHANG | WNOWAIT), which leaves the leader an
unreaped zombie whose allocated pid keeps the process-group id valid
(verified against the OS: /proc stays state Z, killpg on a group whose
only member is the leader zombie is accepted, and Popen.wait reaps
normally afterwards), the group is SIGKILLed on timeout, cap, abnormal
path or after a clean exit alike so a same-group descendant that
redirected its own pipes elsewhere cannot outlive the run, and only
then does Popen.wait reap the leader, so a group signal can never land
on a recycled unrelated group id. A lost leader (waitid raises ECHILD:
the child was reaped outside this executor) suppresses every signal,
reports kill_state 'unconfirmed', and is failed as identity-lost with
no exit status, because Popen.wait under ECHILD fabricates return code
0 (verified locally against a true exit of 7) and a released pid may
already name an unrelated group. Before consent is spent, a non-default
SIGCHLD disposition (SIG_IGN kernel auto-reap, a Python handler, or a
foreign handler getsignal reports as None) is refused outright;
reaping by unrelated threads is undetectable, unsupported, and the
tri-state backstop above still refuses to signal should it happen. The
platform gate fails closed where waitid/WNOWAIT is unavailable. Kill
states stay honest (sent, absent, failed, unconfirmed); no coverage is
claimed for a descendant that left the group entirely (its own
session). No total deadline covers Python-side post-processing. This module never probes a Maven version, resolves a
wrapper, bootstraps a distribution, or performs any network or download
action itself: everything Maven does happens inside the one approved
child, unsandboxed, and the approval is disclosure, not protection.
The disclosure the host approves states the sanitized-environment
consequences (the quoted executor-set JAVA_TOOL_OPTIONS pin and the
pickup notice the JVM prints, JAVA_HOME and every other inherited
variable dropped, any JVM resolved from the sanitized PATH, Maven's
user settings and default repository resolving inside the control HOME
for a JVM honoring executor options absent later overrides, an explicit
-Duser.home, a project .mvn/jvm.config or other unsandboxed code being
able to supersede the pin so no settings sandbox is claimed,
host-supplied -s/-gs/-Dmaven.repo.local still winning, the Maven
installation's own global settings still applying) and that input
coverage is host-chosen: unpinned files were not enumerated and stay
unknown, with no automatic completeness invented.

Privacy is structural: the persisted evidence repeats only facts the
operation record already carries (working directory, executable
identity, pinned inputs, disclosed destinations) plus run statuses,
counts and digests. Raw child output (which routinely carries secrets in
Maven diagnostics) is never persisted; the bounded combined bytes are
returned to the caller in memory (`output`) and the report records only
their size and, when the capture was not capped, their sha256, since
full digests require full captured content. No argv item and no output
text appears in any exception this module raises; refusals identify the
offending position (input index, argv[0], disclosure section) so the
operator can look the value up in the proposal they approved. Process
success means exit 0 under the timeout and output cap and nothing more:
no dependency resolution is derived here, no producer report is
imported (that offline import belongs to mdi.maven and the CLI), and a
failed run is recorded as failed, never as an empty result.

Transaction sequence across three RunStore commits, with honest crash
windows; no atomicity is claimed across them: (1) invocation, policy and
observation checks, nothing committed; (2) `consume_approval` commits
proposed->consumed with the started audit; every refusal inside it
raises before that commit; (3) the consumed record is re-read and only
then is the child spawned, exactly once, with no implicit retry; (4) the
evidence is committed, then `operations.finalize_operation` moves
consumed->finished referencing it, because finalize validates that
referenced evidence already exists; a crash between those two commits
leaves a consumed operation with unreferenced evidence, which is safe
because the consent is already spent and replay is refused. Interrupts
propagate uncaught after the process group is killed and the control
HOME removed; the operation stays consumed and is never retried here.
"""

import copy
import hashlib
import json
import math
import os
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mdi import fsprobe, operations, raw_capture_consent
from mdi.raw_capture_store import RawCaptureSink, TOOL_IDENTITY as CAPTURE_TOOL_IDENTITY
from mdi.capture import CaptureError, capture_file

TOOL_BINDING = {
    "name": "mdi.maven_executor",
    "version": "1",
    "source_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
}

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_INPUT_BYTES = 8 * 1024 * 1024
DEFAULT_OUTPUT_TARGET_BYTES = 8 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
MAX_DESTINATION_ITEMS = 32
MAX_DESTINATION_TEXT = 2048

DESTINATIONS_UNKNOWN = "unknown"
_DESTINATIONS_PREFIX = "downloads-destinations: "
_DESTINATIONS_EXPLICIT = "explicit: "
_DESTINATIONS_SEPARATOR = " | "

CONTROL_HOME_PREFIX = "mdi-maven-home-"

UNSANDBOXED_DISCLOSURE = (
    "Maven runs unsandboxed: the inspected project's code, build extensions and any wrapper "
    "script named by the command execute with full user authority and may write files or "
    "download dependencies; this approval is not protection against project-code side effects."
)
WRITES_DISCLOSURE = (
    "Anticipated writes: normal build outputs under the working directory, the child's fresh "
    "temporary control HOME (removed after the run), and any location the project's code, "
    "extensions, wrapper or settings direct Maven to write, including a local repository or "
    "download cache; no sandbox or network filter is applied."
)
ENVIRONMENT_DISCLOSURE = (
    "Child environment: a fresh allowlist of PATH (absolute entries only), a fresh temporary "
    "control HOME, LC_ALL=C, and exactly one executor-set JVM option: "
    'JAVA_TOOL_OPTIONS=-Duser.home="<control HOME>". The value is quoted, so a '
    "temporary-directory spelling containing spaces or option-shaped text stays one literal "
    "JVM token and cannot inject options, and a quote-bearing temporary base is refused "
    "before consent instead. A JVM resolves user.home from the account database rather than "
    "the HOME variable, so without that pin Maven would keep reading the user's real "
    "~/.m2/settings.xml and default ~/.m2/repository despite the sanitized HOME; with it, a "
    "JVM honoring executor options, absent later overrides (an explicit -Duser.home in the "
    "approved command, a project .mvn/jvm.config, or any unsandboxed project/launcher code), "
    "resolves user.home inside the control HOME: a default isolation posture, not a settings "
    "sandbox. An inherited JAVA_TOOL_OPTIONS value is overwritten, never merged, and the JVM "
    "prints a pickup notice in its output. JAVA_HOME and every other inherited variable are "
    "dropped and any JVM comes from the sanitized PATH. Host-supplied -s/--settings, "
    "-gs/--global-settings or -Dmaven.repo.local in the approved command still wins, and the "
    "Maven installation's own global settings under its install directory still apply."
)
COVERAGE_DISCLOSURE = (
    "Input coverage is host-chosen: the pinned inputs are the project, wrapper and settings "
    "files the host selected; unpinned files were not enumerated and their state is unknown "
    "to this approval, which does not claim completeness."
)

_RUN_OUTCOMES = ("ok", "timeout", "capped", "error")

_LIMITATIONS = (
    "process success means exit 0 under the timeout and output cap; it is not dependency "
    "resolution evidence, and no producer report is imported by this executor",
    "the bounded combined output is returned in memory; without separate paired capture consent "
    "it is not persisted. Raw-capture files may contain secrets; reports contain metadata only",
    "Maven executed unsandboxed with project, extension and wrapper code at full user authority; "
    "the approval discloses possible writes and downloads but is not a sandbox",
    "input and executable identities are re-derived before consumption and re-checked after the "
    "run; a same-user process racing the filesystem between those checks is detected only as "
    "changed evidence, never prevented",
    "the child environment is a fresh allowlist plus exactly one executor-set option "
    '(JAVA_TOOL_OPTIONS=-Duser.home="<control HOME>", quoted against token injection), so '
    "inherited JVM and Maven option values are dropped, and a JVM honoring executor options, "
    "absent later overrides such as an explicit -Duser.home in the approved command, a project "
    ".mvn/jvm.config or other unsandboxed project code, resolves Maven's user settings and "
    "default repository inside the control HOME; this is a default posture, not a settings "
    "sandbox, and host-supplied settings/repository flags still win",
    "the group kill reaches the child's process group, and cleanup signals the group only "
    "while the leader's identity is confirmed owned (alive or unreaped zombie); a leader "
    "reaped elsewhere is failed as identity-lost with all signals suppressed, and a "
    "descendant that starts its own session or migrates process groups is not covered and "
    "may outlive the run",
)


class MavenExecutionError(Exception):
    """Raised when this module refuses to build or run an operation as proposed."""


class _SafetyCapError(RuntimeError):
    """Raised when the platform cannot run subprocesses under control."""


# -- small shared helpers (mirroring the reviewed gitinspect/usage pattern) ---


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MavenExecutionError(f"{name} must be a positive number, got {value!r}")
    if not math.isfinite(value):
        # NaN compares False against <= 0 and infinity survives every bound
        # check, only to detonate inside the deadline and selector arithmetic
        # after consent is spent; non-finite budgets are refused up front.
        raise MavenExecutionError(f"{name} must be a finite positive number, got a non-finite value")
    if value <= 0:
        raise MavenExecutionError(f"{name} must be positive, got {value!r}")
    return value


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise MavenExecutionError(f"{name} must be int, got {value!r}")
    if value <= 0:
        raise MavenExecutionError(f"{name} must be positive, got {value!r}")
    return value


def _require_posix_safety():
    missing = [name for name in ("setsid", "killpg", "set_blocking") if not hasattr(os, name)]
    missing += [
        name
        for name in ("waitid", "P_PID", "WEXITED", "WNOWAIT", "WNOHANG")
        if not hasattr(os, name)
    ]
    if missing or not hasattr(signal, "SIGKILL") or os.name != "posix":
        raise _SafetyCapError(f"platform lacks process-control safety: {', '.join(missing)}")


# Grace bound for observing the leader's exit after a group SIGKILL: the
# signal cannot be blocked, so this only covers kernel scheduling latency.
_OBSERVE_GRACE_SECONDS = 10.0

# Leader ownership states, reasoned once for the whole lifecycle:
# - alive: the child is running; the pid is ours, signaling is safe;
# - exited-unreaped: the child is a zombie we have not reaped (observed
#   through waitid WNOWAIT); the pid and process-group id stay allocated,
#   signaling is safe;
# - lost: waitid raised ECHILD, so someone other than this executor reaped
#   the child (kernel auto-reap under SIGCHLD=SIG_IGN, a reaping handler,
#   or a concurrent thread); the pid may already be recycled, so no signal
#   may be attempted and no exit status exists (Popen.wait under ECHILD
#   fabricates return code 0, verified locally against a true exit of 7).
_LEADER_ALIVE = "alive"
_LEADER_ZOMBIE = "exited-unreaped"
_LEADER_LOST = "lost"

_IDENTITY_HELD = "held"
_IDENTITY_LOST = "lost"

_LOST_ISSUE = (
    "leader identity lost: the child was reaped outside this executor; group signals were "
    "suppressed and the exit status is unrecoverable"
)


def _probe_leader(proc):
    """Instant tri-state ownership probe that never reaps: waitid with
    WNOHANG|WNOWAIT reports the zombie without consuming it, returns None
    while the child runs, and raises ChildProcessError once the child was
    reaped elsewhere. The state is a real tri-state string precisely so no
    caller can collapse 'identity gone' into a boolean true."""
    try:
        result = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return _LEADER_LOST
    return _LEADER_ZOMBIE if result is not None else _LEADER_ALIVE


def _leader_state(proc, deadline):
    """Bounded tri-state wait for the leader's exit without reaping it.

    Returns exited-unreaped once the zombie exists, lost on ECHILD, or
    alive when the bound expires with the child still running. The
    unreaped zombie keeps the pid, and with it the process-group id,
    allocated, so no signal issued while it exists can land on a recycled
    unrelated group."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _LEADER_ALIVE
        state = _probe_leader(proc)
        if state is not _LEADER_ALIVE:
            return state
        time.sleep(min(0.02, remaining))


def _signal_group_if_owned(proc):
    """SIGKILL the leader's group only while ownership is confirmed (alive
    or unreaped zombie), probing immediately before the signal; 'lost'
    suppresses the signal and reports 'unconfirmed', because a released
    pid may already name an unrelated process group."""
    if _probe_leader(proc) == _LEADER_LOST:
        return "unconfirmed"
    return _try_kill_group(proc)


def _reap_quietly(proc):
    """Best-effort reap purely to close out Popen bookkeeping, whose __del__
    warns while returncode is None; the return code is deliberately never
    reported by callers, because under ECHILD Popen fabricates 0."""
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _lost(proc, kill_state="unconfirmed"):
    _reap_quietly(proc)
    return kill_state, _IDENTITY_LOST


def _cleanup_process(proc, leader, *, grace_seconds):
    """The complete signal/reap lifecycle in one place.

    `leader` is the last tri-state observation (None when unknown). Every
    signal goes through _signal_group_if_owned, which re-probes ownership
    immediately beforehand; a lost leader suppresses all signals, reports
    identity 'lost' (kill_state 'unconfirmed' when no signal was ever
    accepted, the honestly observed state otherwise) and is failed by the
    caller. A live leader is group-killed first, then its exit is observed
    under a short grace bound (still without reaping); an exited-unreaped
    leader is swept directly, with the zombie holding the pid and group id.
    Only after the sweep is the leader reaped through Popen.wait, so
    returncode decoding stays the stdlib's; on a lost identity the wait
    still runs to close out Popen state, but its fabricated rc is never
    reported."""
    if leader is None:
        leader = _probe_leader(proc)
    if leader == _LEADER_LOST:
        return _lost(proc)
    kill_state = None
    if leader == _LEADER_ALIVE:
        kill_state = _signal_group_if_owned(proc)
        if kill_state == "unconfirmed":
            return _lost(proc, kill_state)
        leader = _leader_state(proc, time.monotonic() + grace_seconds)
        if leader == _LEADER_LOST:
            return _lost(proc, kill_state)
    sweep = _signal_group_if_owned(proc)
    if sweep == "unconfirmed":
        return _lost(proc, kill_state or sweep)
    if kill_state is None:
        kill_state = sweep
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return "failed", _IDENTITY_HELD
    return kill_state, _IDENTITY_HELD


def _checked_child_reaping():
    """Refuse before consent when the leader's exit could be reaped by
    anyone other than this executor, because the unreaped-identity cleanup
    guarantee depends on sole ownership. SIGCHLD set to SIG_IGN makes the
    kernel auto-reap children (verified locally: waitid then raises ECHILD
    and Popen.wait fabricates return code 0 against a true exit of 7), and
    a Python handler, or a handler installed outside Python (getsignal
    returns None), may reap concurrently; every detectable non-default
    disposition is refused. Reaping by unrelated threads cannot be detected
    and is explicitly unsupported."""
    try:
        handler = signal.getsignal(signal.SIGCHLD)
    except ValueError as exc:
        raise MavenExecutionError(
            "cannot inspect the SIGCHLD disposition outside the main thread; refusing to run "
            "an operation whose cleanup guarantee cannot be verified"
        ) from exc
    if handler is signal.SIG_IGN:
        raise MavenExecutionError(
            "SIGCHLD is set to SIG_IGN, so the kernel auto-reaps children and the leader's "
            "unreaped identity cannot be held; restore the default disposition"
        )
    if handler is not signal.SIG_DFL:
        raise MavenExecutionError(
            "SIGCHLD carries a custom or foreign handler that may reap children concurrently; "
            "restore the default disposition before running Maven operations"
        )


def _sanitized_path(path_value):
    """Child-side PATH: absolute components only, so the child never resolves
    helper executables relative to the project directory it runs in."""
    absolute = [part for part in path_value.split(os.pathsep) if part and os.path.isabs(part)]
    return os.pathsep.join(absolute) or os.defpath


_UNSAFE_HOME_CHARS = ("\"", "'", "\n", "\t", "\r")


def _checked_temp_base():
    """Validate the base mkdtemp will use, before consent is spent.

    The control HOME is created under tempfile's base, which obeys TMPDIR
    and is therefore caller-influenced; a quote inside that spelling could
    break out of the JVM option's quoting and inject options, so such a
    base is refused up front (tabs and newlines for the same reason). The
    pinned user.home relocates Maven's local repository under this base, so
    a memory-backed base would turn every downloaded artifact into RAM; it
    is refused for the same reason the CLI refuses tmpfs state directories."""
    base = tempfile.gettempdir()
    hostile = [char for char in _UNSAFE_HOME_CHARS if char in base]
    if hostile:
        raise MavenExecutionError(
            "the temporary-directory base contains characters the JVM option tokenizer "
            f"cannot keep literal ({hostile!r}); point TMPDIR at a directory whose name "
            "carries no quotes, tabs or newlines"
        )
    try:
        memory_backed = fsprobe.is_memory_backed(base)
    except OSError as exc:
        raise MavenExecutionError(
            f"cannot inspect the filesystem holding the temporary-directory base "
            f"{base!r}: {exc.strerror}") from exc
    if memory_backed:
        raise MavenExecutionError(
            fsprobe.memory_backed_message("the temporary-directory base", base)
            + " Point TMPDIR at a disk-backed directory before proposing the operation.")
    return base


def _child_env(home):
    """Fresh allowlist environment; nothing is inherited, so hostile
    MAVEN_OPTS/MAVEN_ARGS/JVM option/CLASSPATH (and any other variable)
    values from the caller's environment cannot reach the child.

    JAVA_TOOL_OPTIONS is the one deliberate exception, set by the executor
    and never read from the caller: a JVM resolves user.home from the
    account database, not from the HOME variable (verified against OpenJDK
    21: HOME=temp leaves user.home at the real home), so pinning
    -Duser.home to the control HOME is what actually relocates Maven's
    ~/.m2/settings.xml and default local repository. The value is double
    quoted because the JVM tokenizer honors quotes there (verified
    against OpenJDK 21: a spaced path stays one literal token and
    option-shaped directory text cannot inject options); a quote-bearing
    base is refused by _checked_temp_base instead, since an embedded quote
    would terminate the quoted span. An inherited JAVA_TOOL_OPTIONS is
    overwritten, never merged, and the JVM prints a pickup notice. This is
    a default posture for a JVM honoring executor options absent later
    overrides: an explicit -Duser.home in the approved argv, a project
    .mvn/jvm.config or other unsandboxed project code can supersede it,
    and no settings sandbox is claimed."""
    return {
        "PATH": _sanitized_path(os.environ.get("PATH") or os.defpath),
        "HOME": home,
        "LC_ALL": "C",
        "JAVA_TOOL_OPTIONS": f'-Duser.home="{home}"',
    }


# -- bounded subprocess execution (reviewed semantics, local copy) ------------


@dataclass(frozen=True)
class _RunOutcome:
    """One bounded invocation; outcome is ok, timeout, capped or error,
    identity is held or lost (lost means the leader was reaped outside the
    executor, so no signal was sent and no exit status exists, and the
    outcome is never classified a success), and any non-ok outcome carries
    an honest issue stating only what the run control observed: no argv
    item and no output text ever enters it."""

    outcome: str
    rc: int | None
    stdout: bytes
    stderr: bytes
    kill_state: str | None
    issue: str | None
    identity: str = _IDENTITY_HELD


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
        detail = f"Maven operation timed out after {timeout_seconds}s"
    elif outcome == "capped":
        detail = f"Maven combined output exceeded max_output_bytes={max_output_bytes}"
    else:
        return None
    suffix = {
        "sent": "; SIGKILL sent to process group",
        "absent": "; process group already absent",
    }.get(kill_state, "; process group termination not confirmed")
    return detail + suffix


def _run(argv, cwd, env, *, timeout_seconds, max_output_bytes, capture_sink=None):
    """Run the approved command once with output bounded while reading, never
    communicate-then-truncate; wall-clock bounded from successful exec."""
    _require_posix_safety()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return _RunOutcome(
            "error", None, b"", b"", None,
            f"cannot execute the approved executable: {exc.strerror or exc}",
        )
    # The timeout budget anchors after exec succeeds: it bounds the child's
    # runtime, and parent-side fork/exec latency under load must not consume
    # the run budget.
    deadline = time.monotonic() + timeout_seconds
    outcome = "ok"
    abnormal = False
    leader = None
    identity = _IDENTITY_HELD
    buffers = {proc.stdout: [], proc.stderr: []}
    total = 0
    for stream in buffers:
        fd = stream.fileno()
        if isinstance(fd, bool) or not isinstance(fd, int) or fd <= 2:
            kill_state = _try_kill_group(proc)
            return _RunOutcome('error', None, b'', b'', kill_state,
                               f"child stream descriptor {fd!r} failed validation", _IDENTITY_HELD)
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
                            chunk = os.read(stream.fileno(), min(allowance, 65536))
                        except (BlockingIOError, InterruptedError):
                            continue
                        if chunk:
                            buffers[stream].append(chunk)
                            total += len(chunk)
                            if capture_sink is not None:
                                capture_sink.write(chunk)
                        else:
                            selector.unregister(stream.fileno())
                    if outcome != "ok":
                        break
                    if total > max_output_bytes:
                        outcome = "capped"
                        break
            finally:
                selector.close()
            if outcome == "ok":
                # Bounded wait for the leader's exit WITHOUT reaping it: no
                # poll() or wait() here, because either would reap the child
                # and free the pid for reuse before the group sweep below.
                leader = _leader_state(proc, deadline)
                if leader == _LEADER_ALIVE:
                    outcome = "timeout"
                # exited-unreaped proceeds to the sweep; lost is classified
                # by the cleanup below and can never stay a success.
        except BaseException:
            abnormal = True
            raise
    finally:
        # The whole signal/reap lifecycle lives in _cleanup_process: every
        # signal is ownership-probed first, a lost leader suppresses all
        # signals, and the leader is reaped only after the sweep. This runs
        # identically on the normal, timeout, cap and abnormal paths.
        kill_state, identity = _cleanup_process(
            proc, leader, grace_seconds=_OBSERVE_GRACE_SECONDS
        )
        if identity == _IDENTITY_LOST and outcome == "ok":
            outcome = "error"
        stdout = b"".join(buffers[proc.stdout])
        stderr = b"".join(buffers[proc.stderr])
        proc.stdout.close()
        proc.stderr.close()
    if identity == _IDENTITY_LOST:
        # Under ECHILD Popen.wait fabricates return code 0 (verified
        # locally against a true exit of 7); no status is reported at all.
        rc = None
        issue = _LOST_ISSUE
    else:
        rc = proc.returncode
        issue = _transport_issue(outcome, kill_state, timeout_seconds, max_output_bytes)
    return _RunOutcome(outcome, rc, stdout, stderr, kill_state, issue, identity)


# -- disclosure and destinations grammar --------------------------------------


def _validated_destinations(value):
    """Normalize the structured declaration: exactly 'unknown', or a nonempty
    bounded list of nonempty single-line items that survive the clause
    round trip. Anything else is refused rather than guessed at."""
    if isinstance(value, str) and value == DESTINATIONS_UNKNOWN:
        return value
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise MavenExecutionError(
            "network_destinations must be the string 'unknown' or a nonempty list of explicit "
            "destination descriptions"
        )
    items = list(value)
    if not items:
        raise MavenExecutionError(
            "network_destinations must not be empty; pass 'unknown' to disclose explicitly that "
            "download destinations cannot be enumerated"
        )
    if len(items) > MAX_DESTINATION_ITEMS:
        raise MavenExecutionError(
            f"network_destinations exceeds the {MAX_DESTINATION_ITEMS} item bound"
        )
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise MavenExecutionError(f"network_destinations[{index}]: must be a nonempty string")
        if len(item) > MAX_DESTINATION_TEXT:
            raise MavenExecutionError(
                f"network_destinations[{index}]: exceeds the {MAX_DESTINATION_TEXT} character bound"
            )
        if "\n" in item or _DESTINATIONS_SEPARATOR in item:
            raise MavenExecutionError(
                f"network_destinations[{index}]: must not contain newlines or the clause separator"
            )
    return items


def _destinations_clause(destinations):
    if destinations == DESTINATIONS_UNKNOWN:
        return _DESTINATIONS_PREFIX + DESTINATIONS_UNKNOWN
    return _DESTINATIONS_PREFIX + _DESTINATIONS_EXPLICIT + _DESTINATIONS_SEPARATOR.join(destinations)


def _destinations_from_disclosure(disclosure):
    """Re-derive the declaration from the persisted disclosure, strictly: the
    first downloads-destinations line must be exactly one this module could
    have generated, or the operation is refused rather than interpreted."""
    for line in disclosure.split("\n"):
        if not line.startswith(_DESTINATIONS_PREFIX):
            continue
        rest = line[len(_DESTINATIONS_PREFIX):]
        if rest == DESTINATIONS_UNKNOWN:
            return {"mode": "unknown"}
        if rest.startswith(_DESTINATIONS_EXPLICIT):
            items = rest[len(_DESTINATIONS_EXPLICIT):].split(_DESTINATIONS_SEPARATOR)
            if items and all(items) and _destinations_clause(items) == line:
                return {"mode": "explicit", "items": items}
        raise MavenExecutionError(
            "disclosure carries a downloads-destinations line this module did not generate; "
            "refusing to run the operation"
        )
    raise MavenExecutionError(
        "disclosure does not carry the required downloads-destinations line; refusing to run "
        "the operation"
    )


_DISCLOSURE_SENTENCES = (
    UNSANDBOXED_DISCLOSURE,
    WRITES_DISCLOSURE,
    ENVIRONMENT_DISCLOSURE,
    COVERAGE_DISCLOSURE,
)


def _compose_disclosure(argv, cwd, destinations, input_count):
    return "\n".join(
        [
            f"Run the approved Maven command ({len(argv)} argv items) with working directory {cwd}.",
            *_DISCLOSURE_SENTENCES,
            _destinations_clause(destinations),
            f"The executable identity and {input_count} captured input identities are pinned in "
            "this operation record; any observed change requires a new proposal and renewed "
            "approval.",
        ]
    )


# -- identity observation ------------------------------------------------------


def _oserror_reason(exc):
    """Static OS wording only; str(exc) embeds the offending path."""
    if exc.strerror:
        return exc.strerror
    if exc.errno is not None:
        return f"os error {exc.errno}"
    return "failed without an OS reason"


def _sha256_bounded(path, cap):
    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return digest.hexdigest()
                total += len(chunk)
                if total > cap:
                    raise MavenExecutionError(
                        f"approved executable exceeds the {cap} byte hashing bound; "
                        "refusing to pin it"
                    )
                digest.update(chunk)
    except OSError as exc:
        raise MavenExecutionError(
            f"cannot hash the approved executable (argv[0]): {_oserror_reason(exc)}"
        ) from exc


def observe_executable(path):
    """Pin the approved executable without executing it: absolute path, regular
    executable file, realpath plus stat identity plus bounded sha256. System
    launcher symlinks are followed deliberately and pinned by content; the
    observed value is what consent compares, so an executable that changed
    since the proposal is refused."""
    try:
        raw = os.fspath(path)
    except TypeError:
        raise MavenExecutionError("executable must be str or os.PathLike") from None
    if isinstance(raw, bytes) or not raw:
        raise MavenExecutionError("executable must be a nonempty text path")
    if not os.path.isabs(raw):
        raise MavenExecutionError(
            "approved executable (argv[0]) must be an absolute path; this module never resolves "
            "a PATH name and never probes a version"
        )
    try:
        info = os.stat(raw)
    except OSError as exc:
        raise MavenExecutionError(
            f"cannot inspect the approved executable (argv[0]): {_oserror_reason(exc)}"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise MavenExecutionError("approved executable (argv[0]) must be a regular file")
    if not os.access(raw, os.X_OK):
        raise MavenExecutionError("approved executable (argv[0]) is not executable")
    return {
        "path": raw,
        "realpath": os.path.realpath(raw),
        "sha256": _sha256_bounded(raw, MAX_EXECUTABLE_BYTES),
        "identity": {
            "dev": info.st_dev,
            "ino": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        },
    }


def _observed_tool_binding(argv0, tool=None):
    """The binding consume_approval compares: constant fields, freshly observed
    executable, the proposal's pinned input cap, and a freshly observed output
    prestate re-observed under the pinned output cap. A created, replaced,
    removed or changed output target since the proposal therefore refuses
    before consent is spent."""
    tool = tool or {}
    observed = dict(TOOL_BINDING, executable=observe_executable(argv0))
    pinned_input_cap = tool.get("input_max_bytes")
    if pinned_input_cap is not None:
        _positive_int(pinned_input_cap, "tool_identity.input_max_bytes")
        observed["input_max_bytes"] = pinned_input_cap
    output_binding = tool.get("output")
    if output_binding is not None:
        if not isinstance(output_binding, dict) \
                or output_binding.get("state") not in ("absent", "present") \
                or not isinstance(output_binding.get("path"), str):
            raise MavenExecutionError(
                "pinned output prestate has an unexpected shape; refusing to run")
        pinned_cap = output_binding.get("max_bytes", DEFAULT_OUTPUT_TARGET_BYTES)
        _positive_int(pinned_cap, "tool_identity.output.max_bytes")
        observed_output = observe_output_target(output_binding["path"], max_bytes=pinned_cap)
        observed_output["max_bytes"] = pinned_cap
        observed["output"] = observed_output
    return observed


def observe_inputs(identities, *, max_input_bytes=DEFAULT_INPUT_BYTES):
    """Re-capture every pinned input bounded and return the observed identity
    list in record order. A missing, symlinked-final, non-regular, truncated
    or mid-read-changed input raises: its content identity cannot be
    confirmed, so consent is not spent."""
    _positive_int(max_input_bytes, "max_input_bytes")
    if not isinstance(identities, list):
        raise MavenExecutionError("input_identities must be a list")
    if len(identities) > operations.MAX_IDENTITIES:
        raise MavenExecutionError(
            f"input_identities exceeds the {operations.MAX_IDENTITIES} identity bound"
        )
    observed = []
    for index, identity in enumerate(identities):
        where = f"input_identities[{index}]"
        if (
            not isinstance(identity, dict)
            or set(identity) != {"path", "sha256"}
            or not isinstance(identity["path"], str)
        ):
            raise MavenExecutionError(f"{where}: must hold exactly path and sha256")
        try:
            captured = capture_file(identity["path"], max_input_bytes)
        except (CaptureError, OSError) as exc:
            raise MavenExecutionError(f"{where}: {exc}") from exc
        if not captured.complete:
            raise MavenExecutionError(
                f"{where}: cannot confirm content identity ({captured.reason})"
            )
        observed.append({"path": captured.path, "sha256": captured.sha256})
    return observed


def observe_output_target(path, *, max_bytes=DEFAULT_OUTPUT_TARGET_BYTES):
    """Observe one proposed output target without creating or removing it.

    Returns the prestate record bound into the proposal's tool identity:
    ``{path, state: 'absent'}`` or ``{path, state: 'present', sha256, size}``.
    A symlinked or non-regular target is refused rather than followed, and a
    present file is re-captured bounded through capture_file (final component
    O_NOFOLLOW, stat identity before and after), so a swap during observation
    is detected. Absence is an observed fact with its own explicit state: it
    is never encoded as a fake digest, and the observation says nothing about
    ancestor components (best-effort same-user race detection, never atomic
    no-clobber). The observation proves nothing about the future: whether the
    command later creates, replaces or leaves the target is observed again
    after the run and recorded as evidence.
    """
    _positive_int(max_bytes, "max_bytes")
    try:
        text = os.fspath(path)
    except TypeError:
        raise MavenExecutionError("output target must be str or os.PathLike") from None
    if isinstance(text, bytes) or not text:
        raise MavenExecutionError("output target must be a nonempty text path")
    if not os.path.isabs(text):
        raise MavenExecutionError(
            "output target must be an absolute path; this module never resolves a "
            "relative output location")
    try:
        info = os.lstat(text)
    except FileNotFoundError:
        return {"path": text, "state": "absent"}
    except OSError as exc:
        raise MavenExecutionError(
            f"cannot inspect the output target: {_oserror_reason(exc)}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise MavenExecutionError(
            "output target is a symlink and is refused rather than followed")
    if not stat.S_ISREG(info.st_mode):
        raise MavenExecutionError("output target must be absent or a regular file")
    try:
        captured = capture_file(text, max_bytes)
    except (CaptureError, OSError) as exc:
        raise MavenExecutionError(f"cannot pin the output target: {exc}") from exc
    if not captured.complete:
        raise MavenExecutionError(
            f"cannot confirm the output target's content identity ({captured.reason})")
    return {"path": text, "state": "present", "sha256": captured.sha256,
            "size": info.st_size}


def _output_disclosure_line(prestate, overwrite):
    """The durable disclosure sentence for one observed output prestate."""
    where = prestate["path"]
    if prestate["state"] == "present":
        if not overwrite:
            raise MavenExecutionError(
                f"output target {where!r} already exists; refusing the proposal because "
                "overwriting a file the user did not create is never implicit: pass "
                "allow_overwrite only when the host explicitly approved replacing this "
                "observed file")
        return (
            f"Output target {where} exists (sha256 {prestate['sha256']}, "
            f"{prestate['size']} bytes) and the approved command may replace it: the "
            "overwrite of this observed file was explicitly approved for this proposal, "
            "and execution is refused if the observed prestate changed before the run. "
            "Prestate binding is best-effort same-user race detection, not atomic "
            "no-clobber.")
    return (
        f"Output target {where} is absent now and may be created by the command; "
        "execution is refused if the target's observed prestate changed before the run, "
        "and creation is an expectation, never a proven outcome until observed after "
        "the run. Prestate binding is best-effort same-user race detection, not "
        "atomic no-clobber.")


# -- proposal, policy and execution --------------------------------------------


def maven_proposal(store_or_run_id, argv, cwd, *, network_destinations,
                   input_paths=(), operation_id=None, max_input_bytes=DEFAULT_INPUT_BYTES,
                   output_path=None, allow_overwrite=False,
                   max_output_target_bytes=DEFAULT_OUTPUT_TARGET_BYTES):
    """Build a fresh proposed maven operation bound to this module; pure except
    for the input captures and executable hashing it must perform.

    Accepts a RunStore or a run_id string. The scope is the exact argv
    vector (first item an absolute executable path, never a shell string)
    plus the absolute working directory; network_destinations is either
    'unknown' (an explicit declaration that destinations cannot be
    enumerated) or a bounded list of explicit destination descriptions,
    embedded in the disclosure under a strict grammar the executor
    re-derives; input_paths are captured now and pinned as
    {path, sha256} identities.

    output_path, when given, captures the output target's prestate before
    the proposal exists and binds it immutably into the tool identity plus
    the disclosure: an existing target refuses the proposal unless
    allow_overwrite was explicitly set by the caller after host approval to
    replace that observed file (never an implicit overwrite from a generic
    proposal), and execute_maven re-observes the same prestate before
    consent is spent, so creation, replacement, removal or change of the
    target between proposal and execution refuses without spawning anything.
    """
    run_id = getattr(store_or_run_id, "run_id", store_or_run_id)
    if not isinstance(argv, list) or not argv:
        raise MavenExecutionError(
            "argv must be a nonempty vector of strings, never a shell string"
        )
    if not all(isinstance(item, str) and item for item in argv):
        raise MavenExecutionError("every argv item must be a nonempty string")
    if not isinstance(cwd, str) or not cwd:
        raise MavenExecutionError("cwd must be a nonempty text path")
    if not os.path.isabs(cwd):
        raise MavenExecutionError("cwd must be an absolute path")
    if not os.path.isdir(cwd):
        raise MavenExecutionError("cwd must be an existing directory at proposal time")
    if isinstance(input_paths, (str, bytes)):
        raise MavenExecutionError("input_paths must be a sequence of paths, not a single path")
    try:
        raw_paths = list(input_paths)
    except TypeError:
        raise MavenExecutionError("input_paths must be an iterable of paths") from None
    if not raw_paths:
        raise MavenExecutionError(
            "a maven operation must pin at least one captured project input"
        )
    paths = []
    for path in raw_paths:
        try:
            text = os.fspath(path)
        except TypeError:
            raise MavenExecutionError("input_paths must be text or os.PathLike paths") from None
        if isinstance(text, bytes) or not text:
            raise MavenExecutionError("input_paths must be nonempty text paths")
        paths.append(text)
    destinations = _validated_destinations(network_destinations)
    executable = observe_executable(argv[0])
    identities = observe_inputs(
        [{"path": path, "sha256": None} for path in paths],
        max_input_bytes=max_input_bytes,
    )
    _positive_int(max_input_bytes, "max_input_bytes")
    _positive_int(max_output_target_bytes, "max_output_target_bytes")
    output_prestate = None
    output_line = None
    if output_path is not None:
        output_prestate = observe_output_target(
            output_path, max_bytes=max_output_target_bytes)
        # Pin the cap so execution re-observes under the proposal's bound,
        # never a hardwired default that would refuse an unchanged target.
        output_prestate["max_bytes"] = max_output_target_bytes
        output_line = _output_disclosure_line(output_prestate, allow_overwrite)
    tool = dict(TOOL_BINDING, executable=executable,
                input_max_bytes=max_input_bytes)
    if output_prestate is not None:
        tool["output"] = output_prestate
    disclosure = _compose_disclosure(argv, cwd, destinations, len(identities))
    if output_line is not None:
        disclosure = disclosure + "\n" + output_line
    if len(disclosure) > operations.MAX_DISCLOSURE:
        # Refuse here, as this module's own error, before persistence ever
        # sees an overlong disclosure; the storage bound is a backstop.
        raise MavenExecutionError(
            f"composed disclosure exceeds the {operations.MAX_DISCLOSURE} character bound; "
            "shorten the destination descriptions"
        )
    return operations.build_proposal(
        "maven",
        {"kind": "maven", "argv": list(argv), "cwd": cwd},
        identities,
        tool,
        disclosure,
        run_id,
        operation_id,
    )


def _binds_this_executor(tool):
    """Type-faithful structural check against TOOL_BINDING: the constant
    fields must match exactly and a pinned executable identity must be
    present. Exact executable equality is enforced inside consume_approval
    against the freshly observed binding."""
    if not isinstance(tool, dict):
        return False
    for key, value in TOOL_BINDING.items():
        if type(tool.get(key)) is not type(value) or tool.get(key) != value:
            return False
    return isinstance(tool.get("executable"), dict)


def _validated_scope(record):
    """Return (argv, cwd, destinations) from one operation record, refusing
    anything this executor did not propose or cannot run as disclosed."""
    scope = record.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") != "maven":
        raise MavenExecutionError(
            "this executor runs maven operations only; refuse to execute anything else"
        )
    argv = scope.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise MavenExecutionError("maven scope argv must be a nonempty vector of nonempty strings")
    if not os.path.isabs(argv[0]):
        raise MavenExecutionError(
            "approved executable (argv[0]) must be absolute; this module never resolves a PATH "
            "name and never probes a version"
        )
    cwd = scope.get("cwd")
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        raise MavenExecutionError("maven scope cwd must be an absolute path")
    if not os.path.isdir(cwd):
        raise MavenExecutionError("maven scope cwd is not an existing directory")
    if record.get("input_identities") == []:
        raise MavenExecutionError(
            "a maven operation must pin at least one captured project input; an empty list is "
            "refused"
        )
    if not _binds_this_executor(record.get("tool_identity")):
        raise MavenExecutionError(
            "operation is not bound to this executor's tool identity; refusing to run it"
        )
    disclosure = record.get("disclosure")
    if not isinstance(disclosure, str) or not disclosure:
        raise MavenExecutionError("operation disclosure must be a nonempty string")
    for sentence in _DISCLOSURE_SENTENCES:
        if sentence not in disclosure:
            raise MavenExecutionError(
                "disclosure lacks the required unsandboxed-Maven statements; refusing to run it"
            )
    destinations = _destinations_from_disclosure(disclosure)
    return argv, cwd, destinations


def _check_invocation(runner, timeout_seconds, max_output_bytes, max_input_bytes):
    if runner is not None and not callable(runner):
        raise MavenExecutionError("runner must be callable")
    _positive_number(timeout_seconds, "timeout_seconds")
    _positive_int(max_output_bytes, "max_output_bytes")
    _positive_int(max_input_bytes, "max_input_bytes")


def _coerce_runner_result(result):
    """Validate the injected runner's outcome object; a caller-supplied
    stand-in that lies about its shape is a caller error recorded as a
    runner failure, never a crash."""
    needed = ("outcome", "rc", "stdout", "stderr", "kill_state", "issue", "identity")
    missing = [name for name in needed if not hasattr(result, name)]
    if missing:
        raise MavenExecutionError(f"runner result lacks {missing}")
    if result.outcome not in _RUN_OUTCOMES:
        raise MavenExecutionError(f"runner outcome must be one of {_RUN_OUTCOMES}")
    if result.identity not in (_IDENTITY_HELD, _IDENTITY_LOST):
        raise MavenExecutionError(
            f"runner identity must be {_IDENTITY_HELD!r} or {_IDENTITY_LOST!r}"
        )
    if not (result.rc is None or (isinstance(result.rc, int) and not isinstance(result.rc, bool))):
        raise MavenExecutionError("runner rc must be an int or None")
    if result.outcome == "ok" and result.identity != _IDENTITY_LOST and result.rc is None:
        # ok claims the child exited, so it carries a real exit status; a
        # null rc here is a stand-in that lied, not a run to classify.
        raise MavenExecutionError("runner outcome ok requires an integer rc")
    for name in ("stdout", "stderr"):
        if not isinstance(getattr(result, name), bytes):
            raise MavenExecutionError(f"runner {name} must be bytes")
    if not (result.kill_state is None or isinstance(result.kill_state, str)):
        raise MavenExecutionError("runner kill_state must be a string or None")
    if not (result.issue is None or isinstance(result.issue, str)):
        raise MavenExecutionError("runner issue must be a string or None")
    return result


def _failure_reason(run):
    if run.identity == _IDENTITY_LOST:
        return "identity-lost"
    if run.outcome == "timeout":
        return "timeout"
    if run.outcome == "capped":
        return "output-cap"
    if run.outcome == "error":
        return "exec-error"
    return "nonzero-exit"


def _recheck_inputs(identities, max_input_bytes):
    """Post-run drift observation: each pinned input is captured again and
    compared with its pinned digest; failures become changed_after_run None
    (cannot confirm), never a crash and never a hidden change."""
    checked = []
    for identity in identities:
        entry = {
            "path": identity["path"],
            "pinned_sha256": identity["sha256"],
            "observed_after_sha256": None,
            "changed_after_run": None,
        }
        try:
            captured = capture_file(identity["path"], max_input_bytes)
        except (CaptureError, OSError):
            checked.append(entry)
            continue
        if captured.complete:
            entry["observed_after_sha256"] = captured.sha256
            entry["changed_after_run"] = captured.sha256 != identity["sha256"]
        checked.append(entry)
    return checked


def _recheck_executable(argv0, pinned):
    """Post-run executable drift: True/False when observable, None otherwise."""
    try:
        observed = observe_executable(argv0)
    except MavenExecutionError:
        return None
    return observed != pinned


def execute_maven(
    store,
    operation_id,
    approval,
    *,
    expected_revision,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    max_input_bytes=DEFAULT_INPUT_BYTES,
    runner=None,
    capture_operation_id=None,
    capture_approval=None,
):
    """Execute one approved maven operation; return the persisted outcome.

    The return value holds operation_id, evidence_id, outcome, a process
    summary, the bounded combined output as raw bytes held in memory only
    (never persisted by default), its size/digest metadata, and the
    finalized report and revision. `runner` is a test injection point
    replacing the spawn; it is called exactly once with the durable argv,
    cwd, the sanitized child environment and the same budgets, and its
    result must carry the _RunOutcome fields. timeout_seconds is a finite
    total monotonic wall budget for the child measured from successful
    exec; max_output_bytes caps stdout and stderr combined while reading.

    Optional capture_operation_id and capture_approval must be supplied together.
    Both source and capture consent are then consumed atomically before opening
    the sink or spawning. The streaming sink receives chunks in pipe-read order,
    not stdout-then-stderr order; original stream interleaving is not reconstructed.
    Its independent byte cap never widens the process output cap. Reaching the
    capture cap does not stop the child, while a sink IO failure aborts the run
    without retry. Injected runners are unsupported on this capture path.

    The sink closes before evidence commits. Source and capture are finalized in
    separate revisions; a crash can leave either consumed and cannot be replayed.
    Interruptions retain bounded capture metadata on the exception and leave both
    approvals consumed. No affirmative approval is generated by this function.
    """
    _check_invocation(runner, timeout_seconds, max_output_bytes, max_input_bytes)
    if (capture_operation_id is None) != (capture_approval is None):
        raise MavenExecutionError('capture operation and approval must be supplied together')
    if capture_operation_id is not None and runner is not None:
        raise MavenExecutionError('raw capture requires the streaming process runner')
    _checked_temp_base()
    _checked_child_reaping()
    proposed = store.load()
    if capture_operation_id is not None:
        capture = operations._find_operation(proposed, capture_operation_id)
        if capture['scope'].get('source_operation_id') != operation_id:
            raise MavenExecutionError('capture is bound to a different source operation')
    record = operations._find_operation(proposed, operation_id)
    argv, cwd, destinations = _validated_scope(record)
    pinned_input_cap = record["tool_identity"].get("input_max_bytes")
    if pinned_input_cap is not None:
        # The proposal's pinned cap is the contract; a caller-side default
        # must not refuse an input the proposal already pinned as-is.
        _positive_int(pinned_input_cap, "tool_identity.input_max_bytes")
        max_input_bytes = pinned_input_cap
    observed_inputs = observe_inputs(record["input_identities"], max_input_bytes=max_input_bytes)
    observed_tool = _observed_tool_binding(argv[0], record["tool_identity"])
    if capture_operation_id is None:
        descriptor = operations.consume_approval(
            store, operation_id, approval, observed_inputs, observed_tool, expected_revision
        )
    else:
        descriptor = raw_capture_consent.consume_pair(
            store, capture_operation_id, approval, capture_approval,
            observed_source_inputs=observed_inputs, observed_source_tool=observed_tool,
            observed_capture_inputs=[], observed_capture_tool=CAPTURE_TOOL_IDENTITY,
            expected_revision=expected_revision,
        )
    try:
        return _execute_consumed(
            store,
            operation_id,
            descriptor,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_input_bytes=max_input_bytes,
            runner=runner,
            capture_operation_id=capture_operation_id,
        )
    except BaseException as exc:
        if capture_operation_id is not None and not hasattr(exc, 'raw_capture'):
            exc.raw_capture = {
                'path': os.path.join(store.path, capture['scope']['output_name']),
                'status': 'unconfirmed', 'retained': None,
                'capture_operation_id': capture_operation_id,
            }
        raise


def _run_captured(store, capture, revision, argv, cwd, env, timeout_seconds, max_output_bytes):
    """Close the capture's writer lock before any evidence publication."""
    sink = None
    primary = None
    run = _RunOutcome('error', None, b'', b'', None, None)
    failure = None
    metadata = {'path': None, 'status': 'unconfirmed', 'retained': False}
    try:
        sink = RawCaptureSink.open(store, capture['id'], expected_revision=revision)
        run = _run(argv, cwd, env, timeout_seconds=timeout_seconds,
                   max_output_bytes=max_output_bytes, capture_sink=sink)
    except BaseException as exc:
        failure = {'reason': 'raw-capture-failed'}
        expected_path = os.path.join(store.path, capture['scope']['output_name'])
        for attribute in ('raw_capture', 'capture'):
            observed = getattr(exc, attribute, None)
            if not isinstance(observed, dict):
                continue
            path = observed.get('path')
            retained = observed.get('retained')
            status = observed.get('status')
            if ((path is None or type(path) is str and path == expected_path)
                    and type(retained) is bool
                    and type(status) is str and status in ('partial', 'unconfirmed')
                    and (not retained or path == expected_path)):
                # Sink-open failures expose only bounded recovery metadata,
                # never arbitrary exception fields or an unrelated pathname.
                metadata = {'path': path, 'status': status, 'retained': retained}
                break
        else:
            metadata = {'path': expected_path, 'status': 'unconfirmed', 'retained': None}
        if not isinstance(exc, Exception):
            primary = exc
            raise
    finally:
        if sink is not None:
            try:
                metadata = sink.close(interrupted=primary is not None or failure is not None
                                      or run.outcome != 'ok' or run.rc != 0)
            except BaseException as exc:
                failure = {'reason': 'raw-capture-failed'}
                metadata = sink.result or getattr(exc, 'capture', None) or metadata
                metadata = dict(metadata, status='partial', durability='unconfirmed')
                if primary is not None:
                    primary.add_note('raw capture close failed; bytes may be retained')
                elif not isinstance(exc, Exception):
                    exc.raw_capture = metadata
                    raise
        metadata = dict(metadata, ordering='chunks in parent pipe-read order; stdout/stderr '
                        'interleaving and original write boundaries are not preserved',
                        source_stream_complete=bool(failure is None and primary is None
                            and run.outcome == 'ok' and run.rc == 0
                            and metadata.get('status') == 'complete'))
        if primary is not None:
            primary.raw_capture = metadata
    return run, failure, metadata


def _execute_consumed(store, operation_id, descriptor, *, timeout_seconds,
                      max_output_bytes, max_input_bytes, runner, capture_operation_id=None):
    """Drive the already consumed operation; private, never a public entry point."""
    persisted = store.load(descriptor["revision"])
    record = operations._find_operation(persisted, operation_id)
    if record["state"] != "consumed":
        raise MavenExecutionError(
            f"durable operation {operation_id!r} is {record['state']!r}, not consumed; "
            "refusing to spawn anything"
        )
    # argv, cwd and every identity used below come from this durable record,
    # never from the in-memory descriptor or the caller's arguments.
    argv, cwd, destinations = _validated_scope(record)
    capture = None
    capture_metadata = None
    if capture_operation_id is not None:
        capture = operations._find_operation(persisted, capture_operation_id)
        raw_capture_consent.validate_scope(capture['scope'])
        if (capture['state'] != 'consumed'
                or capture['scope']['source_operation_id'] != operation_id
                or capture['scope']['source_kind'] != 'maven'):
            raise MavenExecutionError('durable capture does not bind this consumed Maven operation')
        capture_metadata = {'path': None, 'status': 'unconfirmed', 'retained': False}
    home = tempfile.mkdtemp(prefix=CONTROL_HOME_PREFIX)
    failure = None
    started = time.monotonic()
    try:
        if any(char in home for char in _UNSAFE_HOME_CHARS):
            # Paranoia for a base swapped after the pre-consume check: a
            # quote-bearing home cannot be kept literal in the JVM option,
            # so the run is refused as an honest failure with consent spent,
            # never tokenized into options.
            run = _RunOutcome(
                "error", None, b"", b"", None,
                "control HOME spelling cannot be kept literal in the JVM option; run refused",
            )
            failure = {"reason": "control-home-unsafe"}
        else:
            env = _child_env(home)
            if capture is not None:
                run, failure, capture_metadata = _run_captured(
                    store, capture, descriptor['revision'], argv, cwd, env,
                    timeout_seconds, max_output_bytes)
            elif runner is None:
                run = _run(argv, cwd, env, timeout_seconds=timeout_seconds,
                           max_output_bytes=max_output_bytes)
            else:
                try:
                    result = runner(argv, cwd, env, timeout_seconds=timeout_seconds,
                                    max_output_bytes=max_output_bytes)
                    run = _coerce_runner_result(result)
                except Exception as exc:  # stand-in failed; consent is spent
                    run = _RunOutcome("error", None, b"", b"", None, None)
                    failure = {"reason": "runner-error", "error_type": type(exc).__name__}
    finally:
        shutil.rmtree(home, ignore_errors=True)
    wall_seconds = round(time.monotonic() - started, 3)

    # Post-run output-target observation: an observed fact about what exists
    # now, never a claim inferred from the exit status. Unobservable is
    # recorded as unknown, never guessed.
    output_binding = record["tool_identity"].get("output")
    output_after = None
    if output_binding is not None:
        try:
            observed_after = observe_output_target(
                output_binding["path"],
                max_bytes=output_binding.get("max_bytes", DEFAULT_OUTPUT_TARGET_BYTES))
        except MavenExecutionError:
            observed_after = None
        output_after = {"path": output_binding["path"],
                        "prestate": copy.deepcopy(output_binding),
                        "observed_after": observed_after}

    combined = run.stdout + run.stderr
    capped = run.outcome == "capped"
    preserved = combined[:max_output_bytes] if capped else combined
    output_digest = None if capped else hashlib.sha256(combined).hexdigest()
    if failure is None:
        if run.identity == _IDENTITY_LOST:
            # Identity first, before any rc inspection: a stand-in or a
            # Popen under ECHILD can fabricate return code 0, and a lost
            # leader means no signal was ever safely sendable and no exit
            # status exists.
            outcome = "failed"
            failure = {"reason": "identity-lost"}
        elif run.outcome == "ok" and run.rc == 0:
            outcome = "succeeded"
        else:
            outcome = "failed"
            failure = {"reason": _failure_reason(run)}
            if failure["reason"] == "nonzero-exit":
                failure["returncode"] = run.rc
    else:
        outcome = "failed"

    evidence = {
        "id": f"ev-{uuid.uuid4()}",
        "category": "maven-run" if outcome == "succeeded" else "maven-run-failure",
        "source_locator": cwd,
        "observed_at": _now_iso(),
        "producer": TOOL_BINDING["name"],
        "payload": {
            "command": {
                "argv_items": len(argv),
                "argv_sha256": hashlib.sha256(
                    json.dumps(argv).encode("utf-8")
                ).hexdigest(),
                "cwd": cwd,
                "executable": copy.deepcopy(record["tool_identity"]["executable"]),
            },
            "environment": {
                "mode": "allowlist",
                "keys": ["PATH", "HOME", "LC_ALL", "JAVA_TOOL_OPTIONS"],
                "inherited": (
                    "nothing; every other caller variable (MAVEN_OPTS, MAVEN_ARGS, JVM option "
                    "variables, CLASSPATH, proxy and credential variables) is dropped before "
                    "the child starts"
                ),
                "java_tool_options": (
                    'set by the executor to -Duser.home="<control HOME>" (quoted against '
                    "token injection) so a JVM honoring executor options, absent later "
                    "overrides such as an explicit -Duser.home in the approved command or a "
                    "project .mvn/jvm.config, resolves Maven's user settings and default "
                    "repository inside the control HOME; a default posture, not a settings "
                    "sandbox; the inherited value is overwritten, never merged"
                ),
                "home": "a fresh owner-only temporary control directory, removed after the run",
            },
            "process": {
                "outcome": run.outcome,
                "returncode": run.rc,
                "kill_state": run.kill_state,
                "issue": run.issue,
                "wall_seconds": wall_seconds,
            },
            "output": {
                "bytes": len(preserved),
                "read_bytes": len(combined),
                "sha256": output_digest,
                "capped": capped,
                "ordering": (
                    "stdout bytes then stderr bytes of the combined bounded capture; "
                    "interleaving is not preserved"
                ),
                "persistence": (
                    "not persisted by default; only this size and digest enter the report, "
                    "because Maven diagnostics can carry secrets"
                ),
            },
            "inputs": _recheck_inputs(record["input_identities"], max_input_bytes),
            "output_target": output_after,
            "executable_changed_after_run": _recheck_executable(
                argv[0], record["tool_identity"]["executable"]
            ),
            "destinations": destinations,
            "limitations": list(_LIMITATIONS),
        },
    }
    if failure is not None:
        evidence["payload"]["failure"] = failure

    persisted["evidence"].append(evidence)
    capture_evidence = None
    if capture is not None:
        capture_evidence = {
            'id': f'ev-{uuid.uuid4()}', 'category': 'raw-diagnostic-capture',
            'source_locator': capture_metadata.get('path') or store.path,
            'observed_at': _now_iso(), 'producer': CAPTURE_TOOL_IDENTITY['name'],
            'payload': {'source_operation_id': operation_id,
                        'capture_operation_id': capture_operation_id,
                        'source_evidence_id': evidence['id'], 'capture': capture_metadata},
        }
        persisted['evidence'].append(capture_evidence)
    try:
        persisted_evidence = store.commit(persisted, descriptor["revision"])
        finalized = operations.finalize_operation(
            store,
            operation_id,
            outcome,
            audit={
                "maven_run": {
                    "process_outcome": run.outcome,
                    "returncode": run.rc,
                    "kill_state": run.kill_state,
                    "output_bytes": len(preserved),
                    "output_capped": capped,
                }
            },
            evidence_ids=[evidence["id"]],
            expected_revision=persisted_evidence["revision"],
        )
        if capture_evidence is not None:
            finalized = operations.finalize_operation(
                store, capture_operation_id,
                'succeeded' if capture_metadata.get('status') == 'complete' else 'failed',
                evidence_ids=[capture_evidence['id']], expected_revision=finalized['revision'])
    except BaseException as exc:
        if capture_metadata is not None:
            exc.raw_capture = capture_metadata
        raise
    return {
        **({'capture': capture_metadata, 'capture_evidence_id': capture_evidence['id']}
           if capture_evidence is not None else {}),
        "operation_id": operation_id,
        "evidence_id": evidence["id"],
        "outcome": outcome,
        "process": {
            "outcome": run.outcome,
            "returncode": run.rc,
            "kill_state": run.kill_state,
            "issue": run.issue,
        },
        "output": preserved,
        "output_meta": {
            "bytes": len(preserved),
            "read_bytes": len(combined),
            "sha256": output_digest,
            "capped": capped,
        },
        "report": finalized,
        "revision": finalized["revision"],
    }
