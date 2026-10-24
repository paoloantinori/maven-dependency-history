"""Bounded, safe observation of one git checkout's immediate context.

checkout_context() reports the resolved HEAD revision and a dirty/clean flag
for an existing non-bare checkout root, plus nothing else; history search is a
later concern. Every probe runs the absolute system git (resolved once, never
a wrapper inside the inspected repository) under a fresh allowlist
environment, with pager, hooks, fsmonitor, gc, submodule recursion
and lazy fetch disabled, and with combined subprocess output bounded while it
is read (selectors plus os.read, monotonic deadline, process-group kill and
reap on cap or timeout; termination is attempted even when the leader has
already exited while descendants hold the pipes, and the reported claim
distinguishes signal sent, group already absent, and failure to confirm).
Paths containing symlinks are rejected, non-root inputs are rejected rather
than silently inspecting an ancestor repository, and any inability to run
safely is reported as unavailable, never retried with weaker settings. Probes
are read-only; with optional locks off the index is left byte-identical.
Working tree status is refused (dirty stays None) whenever any filter.*
driver is configured in the repository, because git executes clean and
process filter commands while hashing stat-dirty tracked files, even when
the bytes are unchanged; there is no git switch that disables this for
status.
"""

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

DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_MAX_OUTPUT_BYTES = 1 << 20

_GLOBAL_ARGS = (
    "--no-pager",
    "--no-optional-locks",
    "--no-lazy-fetch",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "gc.auto=0",
    "-c",
    "submodule.recurse=false",
)

_COVERAGE_NOTE = (
    "status ignores all submodule state (--ignore-submodules=all, no recursion); "
    "dirty is refused (fail closed) when any filter.* driver is configured; "
    "read-only probes, no network"
)


class _SafetyCapError(RuntimeError):
    """Raised when the platform cannot run subprocesses under control."""


def _resolve_git(path_value):
    """Absolute git executable from the trusted PATH, or None. The result is
    frozen lexically absolute in the parent's cwd before any wrapper check or
    child cwd switch, so a relative PATH component can never be re-resolved
    inside the inspected repository at exec time."""
    located = shutil.which("git", path=path_value)
    return os.path.abspath(located) if located is not None else None


def _sanitized_path(path_value):
    """Child-side PATH: absolute components only, so a child whose cwd is the
    inspected repository never resolves helper executables relative to it;
    falls back to the trusted system default when nothing qualifies."""
    absolute = [part for part in path_value.split(os.pathsep) if part and os.path.isabs(part)]
    return os.pathsep.join(absolute) or os.defpath


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


def _child_env(path_value, home):
    """Fresh allowlist environment; nothing is inherited, so hostile GIT_*
    and pager variables from the caller's environment cannot reach the child."""
    return {
        "PATH": _sanitized_path(path_value),
        "HOME": home,
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }


def _first_line(data):
    """Diagnostics only (stderr snippets and issue text); never used to parse
    protocol values, whose bytes must be decoded exactly."""
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line[:240]
    return ""


def _decode_stripped(data):
    """Decode one newline-terminated protocol value exactly: filesystem bytes
    via surrogateescape, then remove only the single trailing newline. Values
    may legitimately be longer than any diagnostic cap and may themselves
    contain newlines (paths), so no truncation or splitting happens here."""
    text = data.decode("utf-8", errors="surrogateescape")
    return text[:-1] if text.endswith("\n") else text


@dataclass(frozen=True)
class _RunOutcome:
    """Result of one bounded git invocation; outcome is ok, timeout, capped
    or error, and a non-ok outcome always carries an issue."""

    outcome: str
    rc: int | None
    stdout: bytes
    stderr: bytes
    issue: str | None


def _try_kill_group(proc):
    """Attempt SIGKILL of the child's whole process group. Returns 'sent' when
    the signal was accepted for the group, 'absent' when the group no longer
    had any member to signal, and 'failed' on any other error. Signal
    acceptance proves neither observed descendant exit nor leader reaping;
    the leader is reaped separately by the caller."""
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
    """Compose the transport issue after cleanup; the kill claim states only
    what was observed (signal sent, group already absent, or failure) and
    never asserts descendant exit, which only callers can verify."""
    if outcome == "timeout":
        detail = f"timed out after {timeout_seconds}s"
    elif outcome == "capped":
        detail = f"combined output exceeded max_output_bytes={max_output_bytes}"
    else:
        return None
    suffix = {
        "sent": "; SIGKILL sent to process group",
        "absent": "; process group already absent",
        "refused-invalid-pgid": "; process group id failed validation, no signal sent",
    }.get(kill_state, "; process group termination not confirmed")
    return detail + suffix


def _run(argv, cwd, env, *, timeout_seconds, max_output_bytes):
    """Run one git command with output bounded while reading, never
    communicate-then-truncate."""
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
        return _RunOutcome("error", None, b"", b"", f"cannot execute {argv[0]}: {exc.strerror or exc}")
    # The timeout budget anchors after exec succeeds: it bounds the child's
    # runtime, and parent-side fork/exec latency under load must not consume
    # the probe budget.
    deadline = time.monotonic() + timeout_seconds
    outcome = "ok"
    kill_state = None
    abnormal = False
    buffers = {proc.stdout: [], proc.stderr: []}
    total = 0
    # A fabricated or std stream descriptor must never reach the selector:
    # mock objects coerce to fd 1, and epoll_ctl on a regular file (stdout
    # redirected to a log) raises EPERM straight into the kill path.
    for stream in buffers:
        fd = stream.fileno()
        if isinstance(fd, bool) or not isinstance(fd, int) or fd <= 2:
            kill_state = _try_kill_group(proc)
            return _RunOutcome("error", None, b"", b"", 
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
    return _RunOutcome(outcome, proc.returncode, stdout, stderr, _transport_issue(
        outcome, kill_state, timeout_seconds, max_output_bytes
    ))


def _path_hygiene(path):
    """Preflight walk of every component of the absolute path; report the
    first missing ancestor (unavailable) or symlinked component (rejected),
    else (None, None). This is a preflight, not atomic adversarial isolation:
    a racing writer can swap components after they are inspected, and the
    probes themselves re-derive the root from git. The ascent stops at the
    dirname fixed point (dirname(head) == head, which POSIX reached via '//'
    reaches before '/'), so implementation-defined '//' spellings terminate
    without rewriting the claimed root identity."""
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
            return "unavailable", f"cannot inspect {candidate}: {exc.strerror or exc}"
        if stat.S_ISLNK(mode):
            return "rejected", f"refusing symlink in checkout path: {candidate}"
    return None, None


def _git_inside_repo(git_path, root):
    real_git = os.path.realpath(git_path)
    real_root = os.path.realpath(root)
    return real_git != real_root and real_git.startswith(real_root + os.sep)


def checkout_context(repo, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES):
    """JSON-safe snapshot of one checkout's revision and dirty state.

    status is complete (revision and dirty both observed), partial (some
    observed; an unborn HEAD reports revision None with an explicit issue, and
    a checkout with filter.* drivers configured refuses dirty),
    unavailable (no git, no repository, bare repository, unsupported flags, or
    any run going out of bounds), or rejected (symlinked path, or an input
    that is not the checkout root; ancestor repositories are never inspected
    silently). dirty is None whenever it was not observed. Path checks are
    preflight only; no atomic adversarial isolation is attempted, and the
    claimed repo spelling is never silently rewritten.
    """
    timeout_seconds = _positive_int(timeout_seconds, "timeout_seconds")
    max_output_bytes = _positive_int(max_output_bytes, "max_output_bytes")
    try:
        raw = os.fspath(repo)
    except TypeError:
        raise TypeError(f"repo must be str or os.PathLike, got {type(repo).__name__}") from None
    if isinstance(raw, bytes):
        raise TypeError("repo must be a text path, not bytes")
    path = os.path.abspath(raw)
    observed_at = datetime.now(timezone.utc).isoformat()
    issues = []
    revision = None
    dirty = None

    def result(status):
        return {
            "repo": path,
            "status": status,
            "revision": revision,
            "dirty": dirty,
            "issues": issues,
            "tool": tool,
            "coverage": _COVERAGE_NOTE,
            "observed_at": observed_at,
        }

    trusted_path = os.environ.get("PATH") or os.defpath
    git_path = _resolve_git(trusted_path)
    tool = {"name": "git", "path": git_path}

    try:
        _require_posix_safety()
    except _SafetyCapError as exc:
        issues.append(str(exc))
        return result("unavailable")
    if git_path is None:
        issues.append("git executable not found on PATH")
        return result("unavailable")
    if _git_inside_repo(git_path, path):
        issues.append(f"refusing git executable inside the inspected repository: {git_path}")
        return result("unavailable")

    hygiene_status, hygiene_issue = _path_hygiene(path)
    if hygiene_issue:
        issues.append(hygiene_issue)
        return result(hygiene_status)

    with tempfile.TemporaryDirectory(prefix="mdi-gitinspect-home-") as scratch_home:
        env = _child_env(trusted_path, scratch_home)

        def run(command):
            return _run(
                [git_path, *_GLOBAL_ARGS, *command],
                cwd=path,
                env=env,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )

        top = run(["rev-parse", "--show-toplevel"])
        if top.outcome != "ok" or top.rc != 0:
            detail = top.issue or f"exit {top.rc}: {_first_line(top.stderr)}"
            issues.append(f"cannot locate checkout root: {detail}")
            return result("unavailable")
        toplevel = _decode_stripped(top.stdout)
        if not toplevel:
            issues.append("cannot locate checkout root: git reported no toplevel")
            return result("unavailable")
        if toplevel != path:
            issues.append(f"input is not the checkout root; git reports {toplevel}")
            return result("rejected")

        head = run(["rev-parse", "--verify", "HEAD"])
        if head.outcome != "ok" or head.rc != 0:
            detail = head.issue or _first_line(head.stderr)
            issues.append(f"revision unavailable (HEAD unborn or unresolvable): {detail}")
        else:
            value = _decode_stripped(head.stdout)
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
                revision = value
            else:
                issues.append(f"revision has unexpected shape: {_first_line(head.stdout)!r}")

        # git hashes stat-dirty tracked files through their configured
        # filter.<driver>.clean or filter.<driver>.process command, executing
        # repo-configured code even when the bytes are unchanged (mtime-only
        # touch). No git switch disables this for status (verified against
        # git 2.55 man pages; process drivers take precedence over clean
        # ones), so any filter.* key fails closed instead of running status.
        filters = run(["config", "get", "--all", "--null", "--regexp", r"^filter\."])
        if filters.outcome != "ok":
            issues.append(f"working tree status unavailable: cannot inspect filter configuration: {filters.issue}")
        elif filters.rc == 0:
            issues.append(
                "working tree status unavailable: filter drivers are configured in this repository "
                "(filter.*.clean and filter.*.process execute commands during status content hashing); "
                "refusing to run status (fail closed)"
            )
        elif filters.rc != 1:
            issues.append(
                "working tree status unavailable: cannot inspect filter configuration: "
                f"exit {filters.rc}: {_first_line(filters.stderr)}"
            )
        else:
            work = run(["status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=all"])
            if work.outcome != "ok" or work.rc != 0:
                detail = work.issue or f"exit {work.rc}: {_first_line(work.stderr)}"
                issues.append(f"working tree status unavailable: {detail}")
            else:
                dirty = bool(work.stdout)

    if revision is not None and dirty is not None:
        return result("complete")
    if revision is not None or dirty is not None:
        return result("partial")
    return result("unavailable")
