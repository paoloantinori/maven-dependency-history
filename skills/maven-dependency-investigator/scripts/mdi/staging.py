"""Safe staging of usage-helper inputs copied out of a Maven checkout.

stage_inputs(checkout, root, dependencies, *, staging_base,
limits=StageLimits()) copies the reviewed root input and every dependency
input out of the checkout into one fresh exclusive UUID directory under a
caller-supplied staging_base and returns an immutable StagedInputs bundle
(root, dependencies tuple, observations, issues, complete, JSON-safe
summary). The staged_inputs(...) context manager yields that bundle and
removes the generated stage directory on exit, even when the body raises.
No helper is invoked here: staging only produces the trusted-immutable
inputs that mdi.usage.run_usage_helper's caller contract requires.

Reading policy: the checkout and staging_base are opened component by
component from the filesystem root with O_DIRECTORY|O_NOFOLLOW (any
symlinked component, including the roots themselves, refuses), every
source directory below the checkout is opened relative to a held
descriptor with the same flags, and every file is captured through
mdi.discover.capture_at (bounded read, stat identity before and after,
digest only for complete captures). No path is reopened after its
descriptor is held; a directory swapped or mutated mid-walk is detected by
fstat identity on the held descriptor and refused, and a child replaced by
a symlink fails the O_NOFOLLOW open instead of reading outside the
checkout. Sources must be lexically under the checkout, must be regular
*.jar files or plain directories (never *.jar directories), and duplicate
canonical inputs refuse. Containment (checkout versus staging_base in both
directions, and every input versus the checkout) is compared component-wise
on a spelling that collapses only the leading separator run, so a POSIX
'//' root spelling cannot dodge it while legitimate '//' bases keep
working; no symlink is ever followed to normalize. This detects
interference; it is not an atomic
snapshot of a concurrently changing checkout, and the owner-only stage
modes are no protection against a same-user adversary.

Writing policy: the stage directory is a new exclusive 0700 UUID directory
under staging_base (which must exist, be a directory, and lie outside the
checkout with no overlap in either direction); staged inputs get generated
names (input-0000.jar / input-0000) so they can never overlap originals;
every staged file is created O_EXCL at mode 0600 and every staged directory
mkdir'd 0700, both relative to held stage descriptors. Copies are written
from captured bytes (no hard links, no aliases); directories are copied as
every regular member file (multi-release META-INF/versions entries are
preserved and left for the helper to mark unsupported), with symlinks,
FIFOs, sockets and devices refused. File, directory, depth, byte, input,
count and wall-clock budgets are frozen positive ints enforced before any
descriptor is queued or byte is read; whole-directory identity is a
deterministic digest over sorted relative paths and full file hashes, not
directory stat.

Cleanup removes only entries this operation created, verified by inode
against a ledger before each unlink/rmdir and accessed exclusively through
held descriptors (no shutil.rmtree, no path walks, no symlink following);
an entry that no longer carries the created inode is interference: the
partial stage is left in place and StageError reports it, so a replaced
object is never deleted. The same rule applies before ownership is even
verified: when the freshly created stage directory's identity stat fails,
nothing is removed by name (the name could already hold someone else's
empty replacement), the identifiable partial is left for the caller, and
the descriptors are still closed on every path. A refusal that retained
anything reports it as structured evidence on the raised StageError:
cleanup_status ("not-created" before any directory exists, "complete" when
the abort removal verified everything away, "partial" when only some
created entries were removed, "failed" otherwise) plus at most one
StageLeftover record naming the retained stage directory with
identity_verified False, because ownership was never re-proven before the
refusal; absence of ownership information is never read as cleanliness.
Immutability of staged
inputs is a contract, not a filesystem guarantee: this module writes
0600/0700 owner-only copies and trusts that nothing mutates them between
staging and analysis.
"""

import contextlib
import errno
import os
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256

from mdi.capture import CaptureError, FileIdentity
from mdi.discover import capture_at, open_checkout_fd, open_module_dir

STAGING_SCHEMA = "mdi.staged-inputs/1"

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_CREATE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                      | getattr(os, "O_NOFOLLOW", 0))

# Test seams: patched in tests to inject drift deterministically.
_capture_at = capture_at
_monotonic = time.monotonic
_before_descend = lambda dir_fd, subject: None
_before_child_open = lambda dir_fd, name, subject: None


_LEFTOVER_REASON_MAX = 512


@dataclass(frozen=True)
class StageLeftover:
    """One remnant a refused staging deliberately left on disk: where it is,
    why it was left, and whether it was verified as an object this staging
    created (False whenever identity was not re-proven before the refusal).
    Evidence for the caller; never permission to delete."""

    path: str
    reason: str
    identity_verified: bool

    def __post_init__(self) -> None:
        if len(self.reason) > _LEFTOVER_REASON_MAX:
            object.__setattr__(self, "reason", self.reason[:_LEFTOVER_REASON_MAX])

    def as_dict(self) -> dict:
        return {"path": self.path, "reason": self.reason,
                "identity_verified": self.identity_verified}


class StageError(ValueError):
    """One staging refusal; `code` names the category and `observations`
    preserves what was already observed when the source changed mid-walk.
    `cleanup_status` states what the failure path left behind ("not-created"
    when no stage directory exists, "complete" when the abort removal
    verified everything it created away, "partial" when only some created
    entries were removed, "failed" otherwise, "unknown" when the raiser
    tracked nothing) and `leftovers` carries the retained locations as
    bounded StageLeftover records (staging itself records at most one, the
    stage directory). Absence of ownership information is never read as
    cleanliness."""

    def __init__(self, code, message, observations=(), cleanup_status="unknown",
                 leftovers=()):
        super().__init__(message)
        self.code = code
        self.message = message
        self.observations = tuple(observations)
        self.cleanup_status = cleanup_status
        self.leftovers = tuple(leftovers)


@dataclass(frozen=True)
class StageLimits:
    """Frozen positive budgets enforced before work, cumulative per call."""

    max_inputs: int = 64
    max_files: int = 10000
    max_dirs: int = 256
    max_depth: int = 16
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_seconds: int = 30

    def __post_init__(self) -> None:
        for name in ("max_inputs", "max_files", "max_dirs", "max_depth",
                     "max_file_bytes", "max_total_bytes", "max_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")


@dataclass(frozen=True)
class StagedInput:
    """One staged input: source locator, generated staged name, digests."""

    role: str
    source: str
    kind: str
    staged_name: str
    staged_path: str
    source_digest: str | None
    staged_digest: str | None
    file_count: int
    byte_count: int


class _Ownership:
    """Ledger and descriptors for cleanup of exactly what was created."""

    def __init__(self, base_fd, stage_fd, stage_name, stage_identity, stage_dir):
        self.base_fd = base_fd
        self.stage_fd = stage_fd
        self.stage_name = stage_name
        self.stage_identity = stage_identity
        self.stage_dir = stage_dir
        self.ledger = {}
        self.removed = 0
        self.done = False


@dataclass(frozen=True)
class StagedInputs:
    """Immutable staging result; cleanup() removes only the owned stage."""

    stage_dir: str
    stage_id: str
    root: StagedInput
    dependencies: tuple
    observations: tuple
    issues: tuple
    complete: bool
    summary: dict
    _own: _Ownership = field(default=None, repr=False, compare=False)

    def cleanup(self) -> None:
        _cleanup_owned(self._own)


def _lexical_abs(value, what) -> str:
    """Text-path validation plus lexical absolute spelling; never realpath."""
    try:
        raw = os.fspath(value)
    except TypeError:
        raise TypeError(f"{what} must be str or os.PathLike, got {type(value).__name__}") from None
    if isinstance(raw, bytes):
        raise TypeError(f"{what} must be a text path, not bytes")
    if not raw:
        raise StageError(f"invalid-{what}", f"empty path given for {what}")
    return os.path.abspath(raw)


def _comparable(abs_path: str) -> str:
    """Containment-comparison spelling of a lexical absolute path: only the
    leading separator run is collapsed, because POSIX leaves a leading '//'
    implementation-defined (on Linux it names the same root) while abspath
    preserves it, which would let a '//' spelling dodge a purely string
    prefix containment check. Purely lexical: no symlink is followed and no
    on-disk spelling is rewritten for opening or reporting."""
    while abs_path.startswith(os.sep + os.sep):
        abs_path = abs_path[1:]
    return abs_path


def _require_platform() -> None:
    needed = [fn for fn in (os.mkdir, os.rmdir, os.unlink, os.stat, os.open)
              if fn not in os.supports_dir_fd]
    if needed or os.scandir not in os.supports_fd or not getattr(os, "O_NOFOLLOW", 0):
        raise StageError("platform-unsupported",
                         "descriptor-relative staging primitives unavailable on this platform")


def _deadline(started, limits) -> None:
    if _monotonic() - started > limits.max_seconds:
        raise StageError("time-budget",
                         f"staging exceeded max_seconds={limits.max_seconds}")


def _require_utf8(name: str, subject: str) -> None:
    """Refuse names the filesystem returned as non-UTF-8 bytes (they arrive
    as lone surrogates); identity is rejected, never silently re-spelled."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StageError("non-utf8-name",
                         f"refusing source name that is not valid UTF-8: {subject!r}") from exc


def _read_dir_bounded(dir_fd: int, cap: int):
    """Bounded sorted entry read (name, is_symlink, is_dir), fd never taken."""
    records, capped = [], False
    scan_fd = os.dup(dir_fd)
    try:
        with os.scandir(scan_fd) as iterator:
            for entry in iterator:
                if len(records) >= cap:
                    capped = True
                    break
                records.append((entry.name, entry.is_symlink(),
                                entry.is_dir(follow_symlinks=False)))
    finally:
        os.close(scan_fd)
    records.sort(key=lambda record: record[0])
    return records, capped


def _write_staged(dir_fd: int, name: str, rel: str, data: bytes, own: _Ownership) -> None:
    fd = os.open(name, _FILE_CREATE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
    finally:
        os.close(fd)
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    own.ledger[rel] = (st.st_dev, st.st_ino, "file")


def _mkdir_staged(dir_fd: int, name: str, rel: str, own: _Ownership) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=dir_fd)
    except FileExistsError as exc:
        raise StageError("stage-interference",
                         f"staged path {rel!r} already exists; leaving stage untouched") from exc
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    own.ledger[rel] = (st.st_dev, st.st_ino, "dir")
    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    if os.fstat(fd).st_ino != st.st_ino:
        os.close(fd)
        raise StageError("stage-interference",
                         f"staged directory {rel!r} lost its created inode immediately")
    return fd


def _capture_member(dir_fd: int, name: str, subject: str, limits, counters) -> tuple:
    remaining = limits.max_total_bytes - counters["bytes"]
    if remaining <= 0:
        raise StageError("total-bytes",
                         f"cumulative staged bytes would exceed max_total_bytes="
                         f"{limits.max_total_bytes} at {subject}")
    effective = min(limits.max_file_bytes, remaining)
    cap = _capture_at(dir_fd, name, subject, effective)
    if not cap.complete:
        if "changed" in (cap.reason or ""):
            raise StageError("source-changed",
                             f"source changed during read: {subject}")
        if effective < limits.max_file_bytes:
            raise StageError("total-bytes",
                             f"{subject} would exceed remaining total budget "
                             f"{remaining} bytes")
        raise StageError("file-bytes",
                         f"{subject} exceeds max_file_bytes={limits.max_file_bytes}")
    counters["bytes"] += len(cap.data)
    counters["files"] += 1
    return cap


def _walk_tree(src_fd: int, staged_fd: int, subject: str, staged_prefix: str,
               input_prefix: str, depth: int, limits, counters, own, records,
               observations, started) -> None:
    _deadline(started, limits)
    counters["dirs"] += 1
    before = FileIdentity.from_stat(os.fstat(src_fd))
    _before_descend(src_fd, subject)
    entries, capped = _read_dir_bounded(src_fd, limits.max_files + limits.max_dirs + 1)
    if capped:
        raise StageError("file-count",
                         f"{subject} holds more entries than the combined "
                         f"max_files={limits.max_files} and max_dirs={limits.max_dirs} "
                         "budgets")
    for name, symlink, is_dir in entries:
        _deadline(started, limits)
        child_subject = f"{subject}/{name}"
        _require_utf8(name, child_subject)
        child_rel = f"{staged_prefix}/{name}"
        member_rel = child_rel[len(input_prefix) + 1:]
        if symlink:
            raise StageError("symlink-member",
                             f"refusing symlink member: {child_subject}")
        if is_dir:
            if depth + 1 > limits.max_depth:
                raise StageError("depth-limit",
                                 f"{child_subject} deeper than max_depth={limits.max_depth}")
            if counters["dirs"] + 1 > limits.max_dirs:
                raise StageError("dir-count",
                                 f"{child_subject} exceeds max_dirs={limits.max_dirs}")
            _before_child_open(src_fd, name, child_subject)
            try:
                child_src = os.open(name, _DIR_FLAGS, dir_fd=src_fd)
            except OSError as exc:
                raise StageError("source-rejected",
                                 f"cannot open {child_subject}: {exc.strerror or exc}") from exc
            try:
                child_staged = _mkdir_staged(staged_fd, name, child_rel, own)
                try:
                    _walk_tree(child_src, child_staged, child_subject, child_rel,
                               input_prefix, depth + 1, limits, counters, own,
                               records, observations, started)
                finally:
                    os.close(child_staged)
            finally:
                os.close(child_src)
            continue
        if counters["files"] + 1 > limits.max_files:
            raise StageError("file-count",
                             f"{child_subject} exceeds max_files={limits.max_files}")
        st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            raise StageError("nonregular-member",
                             f"refusing non-regular member: {child_subject}")
        cap = _capture_member(src_fd, name, child_subject, limits, counters)
        _write_staged(staged_fd, name, child_rel, cap.data, own)
        records.append((member_rel, cap.sha256))
        observations.append({
            "role": "member", "source": child_subject, "kind": "file",
            "staged_rel": child_rel, "bytes": len(cap.data),
            "source_sha256": cap.sha256, "staged_sha256": cap.sha256,
            "observed_at": cap.observed_at,
        })
    if before != FileIdentity.from_stat(os.fstat(src_fd)):
        raise StageError("source-changed",
                         f"directory changed during staging: {subject}")


def _tree_digest(records) -> str:
    ordered = "".join(f"{rel}\x00{digest}\n" for rel, digest in sorted(records))
    return sha256(ordered.encode("utf-8")).hexdigest()


def _stage_one(checkout_fd: int, role: str, source: str, parts, index: int,
               limits, counters, own, observations, started) -> StagedInput:
    _deadline(started, limits)
    staged_base = f"input-{index:04d}"
    if not parts:
        src_fd = os.dup(checkout_fd)
    else:
        try:
            parent_fd = open_module_dir(checkout_fd, parts[:-1], source)
        except OSError as exc:
            raise StageError("source-rejected",
                             f"cannot open parent of {source}: {exc.strerror or exc}") from exc
        leaf = parts[-1]
        try:
            try:
                st = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                label = "missing input" if exc.errno == errno.ENOENT else "cannot inspect"
                raise StageError("source-rejected",
                                 f"{label}: {source}: {exc.strerror or exc}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise StageError("symlink-input", f"refusing symlink input: {source}")
            if stat.S_ISREG(st.st_mode):
                if not leaf.endswith(".jar"):
                    raise StageError("input-kind",
                                     f"file input must be a .jar file: {source}")
                if counters["files"] + 1 > limits.max_files:
                    raise StageError("file-count",
                                     f"{source} exceeds max_files={limits.max_files}")
                cap = _capture_member(parent_fd, leaf, source, limits, counters)
                staged_name = staged_base + ".jar"
                _write_staged(own.stage_fd, staged_name, staged_name, cap.data, own)
                digest = cap.sha256
                observations.append({
                    "role": role, "source": source, "kind": "jar",
                    "staged_rel": staged_name, "bytes": len(cap.data),
                    "source_sha256": digest, "staged_sha256": digest,
                    "observed_at": cap.observed_at,
                })
                return StagedInput(role, source, "jar", staged_name,
                                   os.path.join(own.stage_dir, staged_name),
                                   digest, digest, 1, len(cap.data))
            if not stat.S_ISDIR(st.st_mode):
                raise StageError("nonregular-input",
                                 f"input must be a .jar file or directory: {source}")
            if leaf.endswith(".jar"):
                raise StageError("input-kind",
                                 f"directory input must not carry a .jar name: {source}")
            src_fd = os.open(leaf, _DIR_FLAGS, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    records = []
    bytes_before = counters["bytes"]
    try:
        if counters["dirs"] + 1 > limits.max_dirs:
            raise StageError("dir-count", f"{source} exceeds max_dirs={limits.max_dirs}")
        staged_fd = _mkdir_staged(own.stage_fd, staged_base, staged_base, own)
        try:
            _walk_tree(src_fd, staged_fd, source, staged_base, staged_base, 0,
                       limits, counters, own, records, observations, started)
        finally:
            os.close(staged_fd)
    finally:
        os.close(src_fd)
    digest = _tree_digest(records)
    byte_count = counters["bytes"] - bytes_before
    observations.append({
        "role": role, "source": source, "kind": "directory",
        "staged_rel": staged_base, "file_count": len(records),
        "byte_count": byte_count, "source_digest": digest,
        "staged_digest": digest, "observed_at": datetime.now(timezone.utc).isoformat(),
    })
    return StagedInput(role, source, "directory", staged_base,
                       os.path.join(own.stage_dir, staged_base),
                       digest, digest, len(records), byte_count)


def stage_inputs(checkout, root, dependencies, *, staging_base,
                 limits=StageLimits()) -> StagedInputs:
    """Stage root plus dependencies out of the checkout; see module docstring."""
    if not isinstance(limits, StageLimits):
        raise TypeError(f"limits must be StageLimits, got {type(limits).__name__}")
    _require_platform()
    if isinstance(dependencies, (str, bytes)):
        raise TypeError("dependencies must be an iterable of paths, not a single "
                        f"{type(dependencies).__name__}")
    dep_items = list(dependencies)
    checkout_fd = None
    base_fd = None
    own = None
    stage_created = False
    try:
        checkout_abs = _lexical_abs(checkout, "checkout")
        base_abs = _lexical_abs(staging_base, "staging_base")
        checkout_cmp = _comparable(checkout_abs)
        base_cmp = _comparable(base_abs)
        if base_cmp == checkout_cmp or base_cmp.startswith(checkout_cmp + os.sep):
            raise StageError("staging-base-inside-checkout",
                             f"staging_base {base_abs} must lie outside the checkout")
        if checkout_cmp.startswith(base_cmp + os.sep):
            raise StageError("staging-base-overlaps-checkout",
                             f"checkout {checkout_abs} must not lie inside staging_base")
        if 1 + len(dep_items) > limits.max_inputs:
            raise StageError("input-count",
                             f"1 root + {len(dep_items)} dependencies exceed "
                             f"max_inputs={limits.max_inputs}")
        jobs, seen = [], {}
        for role, value in [("root", root), *[("dependency", item) for item in dep_items]]:
            subject = _lexical_abs(value, role)
            subject_cmp = _comparable(subject)
            if not (subject_cmp == checkout_cmp
                    or subject_cmp.startswith(checkout_cmp + os.sep)):
                raise StageError("outside-checkout", f"{role} input outside checkout: {subject}")
            _require_utf8(subject, f"{role} input {subject}")
            parts = tuple(subject_cmp[len(checkout_cmp):].split(os.sep)[1:]) if subject_cmp != checkout_cmp else ()
            canonical = os.path.realpath(subject)
            if canonical in seen:
                raise StageError("duplicate-input",
                                 f"{role} input duplicates {seen[canonical]}: {subject}")
            seen[canonical] = role
            jobs.append((role, subject, parts))
        _require_utf8(checkout_abs, f"checkout {checkout_abs}")
        _require_utf8(base_abs, f"staging_base {base_abs}")
        stage_id = str(uuid.uuid4())
        counters = {"bytes": 0, "files": 0, "dirs": 0}
        observations = []
        try:
            checkout_fd = open_checkout_fd(checkout_abs)
        except OSError as exc:
            raise StageError("checkout-rejected",
                             f"cannot open checkout {checkout_abs}: {exc.strerror or exc}") from exc
        try:
            base_fd = open_checkout_fd(base_abs)
        except OSError as exc:
            raise StageError("staging-base-rejected",
                             f"cannot open staging_base {base_abs}: {exc.strerror or exc}") from exc
        try:
            os.mkdir(stage_id, 0o700, dir_fd=base_fd)
            stage_created = True
            st = os.stat(stage_id, dir_fd=base_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise StageError("stage-interference",
                                 f"stage directory name {stage_id!r} already exists;"
                                 " leaving it untouched") from exc
            label = "cannot create stage directory" if not stage_created \
                else ("cannot verify the created stage directory; the unverified"
                      f" stage was left in place at {os.path.join(base_abs, stage_id)}")
            raise StageError("stage-create-failed",
                             f"{label}: {exc.strerror or exc}") from exc
        stage_dir = os.path.join(base_abs, stage_id)
        own = _Ownership(base_fd, None, stage_id, (st.st_dev, st.st_ino), stage_dir)
        try:
            own.stage_fd = os.open(stage_id, _DIR_FLAGS, dir_fd=base_fd)
            started = _monotonic()
            staged = [_stage_one(checkout_fd, role, subject, parts, index, limits,
                                 counters, own, observations, started)
                      for index, (role, subject, parts) in enumerate(jobs)]
        except StageError as exc:
            if not exc.observations and observations:
                exc.observations = tuple(observations)
            raise
        except (OSError, CaptureError) as exc:
            raise StageError("stage-failed", f"staging failed: {exc}",
                             observations) from exc
    except BaseException as exc:
        # Every failure path (refusal, OSError, or an unexpected interrupt)
        # closes the held descriptors and removes exactly the entries this
        # operation created, then rethrows; nothing is swallowed. Before the
        # stage identity is verified nothing is removed by name: the fresh
        # name could by then hold someone else's empty replacement, so the
        # unverified partial stays and the raised error names it. A StageError
        # also carries the structured outcome of that removal, and the status
        # is never guessed: no ownership information is not cleanliness, a
        # created-but-unverifiable stage is a retained leftover, and a
        # retained stage is reported with identity unproven.
        if own is not None:
            outcome = _leave_or_clean(own)
        elif stage_created:
            outcome = ("failed", (StageLeftover(
                os.path.join(base_abs, stage_id),
                _leftover_reason(exc), False),))
        else:
            outcome = ("not-created", ())
        if isinstance(exc, StageError):
            exc.cleanup_status, exc.leftovers = outcome
        raise
    finally:
        if checkout_fd is not None:
            os.close(checkout_fd)
        if own is None and base_fd is not None:
            os.close(base_fd)
    summary = {
        "schema": STAGING_SCHEMA, "stage_id": stage_id, "stage_dir": stage_dir,
        "created_at": datetime.now(timezone.utc).isoformat(), "complete": True,
        "limits": {"max_inputs": limits.max_inputs, "max_files": limits.max_files,
                   "max_dirs": limits.max_dirs, "max_depth": limits.max_depth,
                   "max_file_bytes": limits.max_file_bytes,
                   "max_total_bytes": limits.max_total_bytes,
                   "max_seconds": limits.max_seconds},
        "counts": dict(counters),
        "inputs": [{"role": item.role, "source": item.source, "kind": item.kind,
                    "staged_rel": item.staged_name,
                    "source_digest": item.source_digest,
                    "staged_digest": item.staged_digest,
                    "file_count": item.file_count,
                    "byte_count": item.byte_count} for item in staged],
        "limitations": [
            "staged copies are immutable by contract only: owner-only 0600/0700 modes"
            " stop other users, not a same-user writer, and nothing re-verifies them"
            " between staging and analysis",
            "source capture detects mid-read and mid-walk identity changes but is not"
            " an atomic snapshot of a concurrently changing checkout",
            "whole-directory identity is a deterministic digest over sorted relative"
            " paths and full file hashes; directory stat times are not identity",
        ],
    }
    return StagedInputs(stage_dir, stage_id, staged[0], tuple(staged[1:]),
                        tuple(observations), (), True, summary, own)


def _leftover_reason(exc: BaseException) -> str:
    """Bounded, unparsed provenance for a retained leftover: the failing
    cleanup error's own code and message, carried verbatim (truncated) for
    the caller to read, never re-derived from its wording."""
    if isinstance(exc, StageError):
        return f"{exc.code}: {exc.message}"
    return f"oserror: {exc}"


def _leave_or_clean(own: _Ownership) -> tuple:
    """Best-effort removal of the owned stage; the staging error is re-raised
    by the caller, and interference during this cleanup keeps the partial.
    Returns the truthful outcome for the in-flight error: ("complete", ())
    when everything this staging created was removed, otherwise one
    unverified StageLeftover naming the retained stage directory (the
    interference or failure was detected before its identity could be
    re-proven, and nothing else is ever reported as removed or kept)."""
    try:
        _cleanup_owned(own)
    except (StageError, OSError) as exc:
        status = "partial" if own.removed else "failed"
        return status, (StageLeftover(own.stage_dir, _leftover_reason(exc),
                                      False),)
    return "complete", ()


def _cleanup_fs_failure(action: str, what: str, exc: OSError) -> StageError:
    """Truthful normalization of a cleanup filesystem failure: divergence
    from the owned ledger (missing, looped, or non-directory entries) is
    reported as interference, everything else as an honest cleanup failure.
    Owned entries are left exactly as they are either way."""
    divergence = exc.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR)
    code = "stage-interference" if divergence else "cleanup-failed"
    return StageError(code, f"cannot {action} {what!r} during cleanup: "
                            f"{exc.strerror or exc}; owned entries left untouched")


def _cleanup_owned(own: _Ownership) -> None:
    if own is None or own.done:
        return
    try:
        for rel in sorted(own.ledger, reverse=True):
            _remove_entry(own, rel)
            own.removed += 1
        try:
            st = os.stat(own.stage_name, dir_fd=own.base_fd, follow_symlinks=False)
        except OSError as exc:
            raise _cleanup_fs_failure("stat the stage directory", own.stage_name, exc) from exc
        if (st.st_dev, st.st_ino) != own.stage_identity:
            raise StageError("stage-interference",
                             f"stage directory {own.stage_name!r} no longer carries the"
                             " inode this staging created; leaving it untouched")
        try:
            os.rmdir(own.stage_name, dir_fd=own.base_fd)
        except OSError as exc:
            raise StageError("stage-interference",
                             f"stage directory {own.stage_name!r} could not be removed "
                             f"({exc.strerror or exc}); foreign member left untouched") from exc
    finally:
        own.done = True
        if own.stage_fd is not None:
            with contextlib.suppress(OSError):
                os.close(own.stage_fd)
        with contextlib.suppress(OSError):
            os.close(own.base_fd)


def _remove_entry(own: _Ownership, rel: str) -> None:
    parent_rel, _, name = rel.rpartition("/")
    fd = own.stage_fd
    opened = []
    try:
        walked = ""
        for part in parent_rel.split("/") if parent_rel else ():
            try:
                fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise _cleanup_fs_failure("open staged directory",
                                          f"{parent_rel or part}", exc) from exc
            opened.append(fd)
            walked = f"{walked}/{part}" if walked else part
            expect = own.ledger.get(walked)
            st_fd = os.fstat(fd)
            if expect is None or (st_fd.st_dev, st_fd.st_ino) != expect[:2]:
                raise StageError("stage-interference",
                                 f"staged directory {walked!r} was replaced; leaving"
                                 " the stage untouched")
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as exc:
            raise _cleanup_fs_failure("stat staged entry", rel, exc) from exc
        if (st.st_dev, st.st_ino) != own.ledger[rel][:2]:
            raise StageError("stage-interference",
                             f"staged entry {rel!r} was replaced; leaving the stage"
                             " untouched rather than deleting the replacement")
        try:
            if own.ledger[rel][2] == "dir":
                os.rmdir(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        except OSError as exc:
            raise _cleanup_fs_failure("remove staged entry", rel, exc) from exc
    finally:
        for opened_fd in opened:
            with contextlib.suppress(OSError):
                os.close(opened_fd)


@contextlib.contextmanager
def staged_inputs(checkout, root, dependencies, *, staging_base,
                  limits=StageLimits()):
    """stage_inputs plus guaranteed removal of the owned stage directory."""
    bundle = stage_inputs(checkout, root, dependencies,
                          staging_base=staging_base, limits=limits)
    try:
        yield bundle
    except BaseException as body_exc:
        try:
            bundle.cleanup()
        except (StageError, OSError) as cleanup_exc:
            # The body's exception is the one to surface; the cleanup failure
            # rides along as a note instead of replacing it.
            if hasattr(body_exc, "add_note"):
                body_exc.add_note(f"staging cleanup also failed: {cleanup_exc}")
        raise
    bundle.cleanup()
