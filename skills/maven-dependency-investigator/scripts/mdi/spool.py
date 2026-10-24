"""Bounded descriptor-to-file capture for archive inspection.

The caller supplies an already-open regular source descriptor. This module
never reopens its locator, never closes the caller's descriptor, and uses
pread on its own duplicate so the caller's offset is unchanged. The source
locator is evidence text, not proof that the descriptor belongs to a path;
source containment belongs to the descriptor-opening caller.

A fresh 0700 directory and one 0600 file are created under an existing safe
scratch base outside the checkout. Both roots are walked without following
symlink components. Bytes are streamed in 64 KiB chunks, with at most one
extra source byte consumed as a truncation sentinel; that byte is never
written. sha256 describes a complete unchanged source only; captured_sha256
always describes exactly the bytes successfully written to the spool.

The supplied absolute monotonic deadline is shared with the archive stage.
Checks are cooperative between file operations, including after fsync: they
cannot interrupt blocked kernel IO. The future worker enforces its own hard
execution bound. A stat comparison is not an atomic source snapshot.

A CapturedSpool owns directory descriptors, not a live worker. Before using
it in a worker, the caller must retain it until the worker is confirmed
stopped. cleanup(worker_stopped=True) is the caller's lifecycle assertion,
not user consent and not a process probe. The default cleanup retains the
scratch. Cleanup is terminal and only removes the one verified unchanged
file and its verified directory; unexpected entries or replaced paths are
retained as bounded metadata. No pruning or recovery-by-name is performed.
Modes and identity checks do not sandbox concurrent same-user mutation.
"""

import hashlib
import math
import os
import stat
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from mdi.capture import FileIdentity
from mdi.discover import open_checkout_fd


_CHUNK = 65536
_monotonic = time.monotonic
_pread = getattr(os, "pread", None)
_write = os.write


@dataclass(frozen=True)
class SpoolLeftover:
    path: str
    reason: str
    identity_verified: bool = False

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CleanupResult:
    status: str
    leftovers: tuple = ()

    def as_dict(self):
        return {"status": self.status, "leftovers": [item.as_dict() for item in self.leftovers]}


class SpoolError(ValueError):
    """Safe refusal with optional structured abort-cleanup evidence."""

    def __init__(self, code, message, cleanup=None):
        super().__init__(message)
        self.code = code
        self.cleanup = cleanup


class _Deadline(Exception):
    pass


def _path(value, label):
    path = os.fspath(value)
    if not isinstance(path, str) or not path or "\x00" in path:
        raise SpoolError("invalid-path", f"{label} must be a nonempty text path")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SpoolError("invalid-path", f"{label} must be UTF-8 representable") from exc
    return os.path.abspath(path)


def _comparison_path(path):
    return os.sep + path.lstrip(os.sep)


def _roots(checkout, base):
    left, right = _comparison_path(checkout), _comparison_path(base)
    if os.path.commonpath((left, right)) in (left, right):
        raise SpoolError("checkout-overlap", "scratch base and checkout must not overlap")
    # open_checkout_fd is the shared root-down O_DIRECTORY|O_NOFOLLOW walk.
    checkout_fd = open_checkout_fd(checkout)
    try:
        return open_checkout_fd(base)
    finally:
        os.close(checkout_fd)


def _require_support():
    if (os.name != "posix" or not callable(_pread)
            or not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_NONBLOCK", 0)
            or not getattr(os, "O_DIRECTORY", 0)):
        raise SpoolError("unsupported-platform", "descriptor spool needs POSIX safe-open and pread")


def _before_deadline(deadline):
    if _monotonic() >= deadline:
        raise _Deadline()


def _inode(st):
    return st.st_dev, st.st_ino


class _OwnedSpool:
    def __init__(self, base_fd, base, name):
        self.base_fd = base_fd
        self.name = name
        self.directory = os.path.join(base, name)
        self.dir_fd = None
        self.dir_identity = None
        self.filename = "archive.spool"
        self.file_identity = None
        self.created = False
        self.result = None

    @property
    def path(self):
        return os.path.join(self.directory, self.filename)

    def _directory_matches(self):
        if self.dir_fd is None or self.dir_identity is None:
            return False
        observed = os.stat(self.name, dir_fd=self.base_fd, follow_symlinks=False)
        held = os.fstat(self.dir_fd)
        return (stat.S_ISDIR(observed.st_mode)
                and _inode(observed) == self.dir_identity == _inode(held))

    def _file_matches(self):
        if self.file_identity is None:
            return False
        observed = os.stat(self.filename, dir_fd=self.dir_fd, follow_symlinks=False)
        return (stat.S_ISREG(observed.st_mode)
                and FileIdentity.from_stat(observed) == self.file_identity)

    def _entries(self):
        # Only one file is ours. Do not allocate an unbounded directory list
        # if another same-user writer has inserted unrelated entries.
        # A held directory description can have a stale enumeration view
        # after creating a child. Open '.' relative to it, not its pathname,
        # and check that the independent description still names our inode.
        fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                     | getattr(os, "O_CLOEXEC", 0), dir_fd=self.dir_fd)
        try:
            fresh = os.fstat(fd)
            if (not stat.S_ISDIR(fresh.st_mode)
                    or _inode(fresh) != self.dir_identity
                    or _inode(os.fstat(self.dir_fd)) != self.dir_identity):
                raise OSError("scratch scan directory identity differs")
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > 1:
                        break
            return names
        finally:
            active_error = sys.exception()
            try:
                os.close(fd)
            except BaseException:
                if active_error is None:
                    raise
                active_error.add_note("scratch scan descriptor close was not confirmed")

    def cleanup(self, worker_stopped, *, retain_reason="worker-termination-unconfirmed"):
        if type(worker_stopped) is not bool:
            raise TypeError("worker_stopped must be bool")
        if self.result is not None:
            return self.result
        removed_file = False
        try:
            if not self.created:
                self.result = CleanupResult("not-created")
            elif not worker_stopped:
                self.result = self._retained(retain_reason)
            elif not self._directory_matches():
                self.result = self._retained("directory-identity-unconfirmed")
            else:
                entries = self._entries()
                expected = [self.filename] if self.file_identity is not None else []
                if entries != expected or (entries and not self._file_matches()):
                    self.result = self._retained("scratch-contents-unconfirmed")
                else:
                    if entries:
                        os.unlink(self.filename, dir_fd=self.dir_fd)
                        removed_file = True
                    if not self._directory_matches():
                        self.result = self._retained("directory-identity-unconfirmed", removed_file)
                    else:
                        os.rmdir(self.name, dir_fd=self.base_fd)
                        self.result = CleanupResult("complete")
        except OSError:
            self.result = self._retained("cleanup-failed", removed_file)
        except BaseException:
            self.result = self._retained("cleanup-interrupted", removed_file)
            raise
        finally:
            # Release ownership before close: even an interrupted close must
            # never make a later cleanup act on a recycled descriptor number.
            active_error = sys.exception()
            close_error = None
            for name in ("dir_fd", "base_fd"):
                fd = getattr(self, name)
                setattr(self, name, None)
                if fd is not None:
                    try:
                        os.close(fd)
                    except BaseException as exc:
                        if close_error is None:
                            close_error = exc
            if close_error is not None:
                self.result = self._retained("descriptor-close-unconfirmed", removed_file)
                if active_error is not None:
                    active_error.add_note("spool descriptor close was not confirmed")
                elif not isinstance(close_error, OSError):
                    raise close_error
        return self.result

    def _retained(self, reason, partial=False):
        return CleanupResult("partial" if partial else "retained",
                             (SpoolLeftover(self.directory, reason),))

    def open_reader(self):
        if self.result is not None:
            raise SpoolError("spool-closed", "spool ownership was released")
        fd = None
        try:
            if not self._directory_matches() or not self._file_matches():
                raise SpoolError("spool-changed", "spool identity is no longer confirmed")
            fd = os.open(self.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | getattr(os, "O_CLOEXEC", 0), dir_fd=self.dir_fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or FileIdentity.from_stat(st) != self.file_identity:
                raise SpoolError("spool-changed", "opened spool identity differs from capture")
            return fd
        except BaseException as exc:
            if fd is not None:
                os.close(fd)
            if isinstance(exc, OSError):
                raise SpoolError("spool-unavailable", "cannot open verified spool") from exc
            raise


@dataclass(frozen=True)
class CapturedSpool:
    source_path: str
    path: str
    scratch_dir: str
    size: int
    bytes_read: int
    sha256: str | None
    captured_sha256: str
    complete: bool
    reasons: tuple
    observed_at: str
    stat_before: FileIdentity
    stat_after: FileIdentity
    _owner: _OwnedSpool = field(repr=False, compare=False)

    def as_dict(self):
        return {
            "source_path": self.source_path, "path": self.path,
            "scratch_dir": self.scratch_dir, "size": self.size,
            "bytes_read": self.bytes_read, "sha256": self.sha256,
            "captured_sha256": self.captured_sha256, "complete": self.complete,
            "reasons": list(self.reasons), "observed_at": self.observed_at,
            "stat_before": asdict(self.stat_before), "stat_after": asdict(self.stat_after),
        }

    def cleanup(self, *, worker_stopped=False):
        """Terminal cleanup; unconfirmed worker lifetime retains the scratch."""
        return self._owner.cleanup(worker_stopped)

    def open_reader(self):
        """Return a new owned read-only fd. Close it before confirming cleanup safe."""
        return self._owner.open_reader()


def capture_to_spool(source_fd, *, source_path, checkout, scratch_base,
                     max_bytes, deadline):
    """Capture from offset zero with pread, preserving the caller's fd/offset.

    deadline is an absolute finite monotonic instant, not a fresh per-file
    timeout. Incomplete capture retains bounded bytes and qualifications;
    operational failures raise SpoolError with abort cleanup metadata.
    The caller must eventually invoke cleanup with accurate worker state.
    """
    if type(source_fd) is not int or source_fd < 0:
        raise TypeError("source_fd must be a nonnegative int")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise ValueError("deadline must be a finite monotonic instant")
    try:
        finite = math.isfinite(deadline)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("deadline must be a finite monotonic instant")
    source_path = _path(source_path, "source locator")
    checkout = _path(checkout, "checkout")
    scratch_base = _path(scratch_base, "scratch base")
    _require_support()
    source = output = None
    owner = None
    captured = None
    primary = None
    primary_traceback = None
    try:
        _before_deadline(deadline)
        source = os.dup(source_fd)
        before_raw = os.fstat(source)
        if not stat.S_ISREG(before_raw.st_mode):
            raise SpoolError("not-regular", "source descriptor is not a regular file")
        before = FileIdentity.from_stat(before_raw)
        name = "mdi-spool-" + str(uuid.uuid4())
        base_fd = _roots(checkout, scratch_base)
        try:
            owner = _OwnedSpool(base_fd, scratch_base, name)
        except BaseException as exc:
            try:
                os.close(base_fd)
            except BaseException:
                exc.add_note("spool base descriptor close was not confirmed")
            raise
        _before_deadline(deadline)
        os.mkdir(owner.name, 0o700, dir_fd=base_fd)
        owner.created = True
        observed = os.stat(owner.name, dir_fd=base_fd, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            raise SpoolError("scratch-changed", "created scratch directory changed")
        owner.dir_identity = _inode(observed)
        owner.dir_fd = os.open(owner.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                               | getattr(os, "O_CLOEXEC", 0), dir_fd=base_fd)
        if _inode(os.fstat(owner.dir_fd)) != owner.dir_identity:
            raise SpoolError("scratch-changed", "opened scratch directory changed")
        output = os.open(owner.filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         0o600, dir_fd=owner.dir_fd)
        owner.file_identity = FileIdentity.from_stat(os.fstat(output))
        digest = hashlib.sha256()
        offset = written = 0
        eof = False
        reasons = []
        try:
            while offset <= max_bytes:
                _before_deadline(deadline)
                data = _pread(source, min(_CHUNK, max_bytes + 1 - offset), offset)
                if not data:
                    eof = True
                    break
                offset += len(data)
                part = memoryview(data)[:max(0, max_bytes - written)]
                while part:
                    _before_deadline(deadline)
                    count = _write(output, part)
                    if count <= 0 or count > len(part):
                        raise SpoolError("write-failed", "spool write made invalid progress")
                    digest.update(part[:count])
                    written += count
                    part = part[count:]
            _before_deadline(deadline)
            os.fsync(output)
            _before_deadline(deadline)
        except _Deadline:
            reasons.append("deadline-exceeded")
        after = FileIdentity.from_stat(os.fstat(source))
        owner.file_identity = FileIdentity.from_stat(os.fstat(output))
        if offset > max_bytes:
            reasons.append("input-truncated")
        if before != after:
            reasons.append("source-changed")
        complete = eof and not reasons
        captured_digest = digest.hexdigest()
        captured = CapturedSpool(
            source_path, owner.path, owner.directory, written, offset,
            captured_digest if complete else None, captured_digest, complete,
            tuple(reasons), datetime.now(timezone.utc).isoformat(), before, after, owner,
        )
    except BaseException as exc:
        primary, primary_traceback = exc, exc.__traceback__
        if output is not None:
            try:
                owner.file_identity = FileIdentity.from_stat(os.fstat(output))
            except BaseException:
                owner.file_identity = None
                primary.add_note("spool file identity was not confirmed during abort")

    # Ownership is transferred only after both local descriptors have been
    # closed. Invalidate before each attempt; a close can release an fd and
    # still raise, so neither that number nor the caller's fd may be retried.
    close_error = None
    for kind in ("output", "source"):
        if kind == "output":
            fd, output = output, None
        else:
            fd, source = source, None
        if fd is not None:
            try:
                os.close(fd)
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
    if primary is None and close_error is None:
        return captured
    if close_error is not None:
        if primary is None:
            primary, primary_traceback = close_error, close_error.__traceback__
        else:
            primary.add_note("spool capture descriptor close was not confirmed")
    try:
        # No worker was started, but an unconfirmed local close may leave a
        # writable spool descriptor alive. Retain rather than unlink in that
        # case, and always release the directory ownership separately.
        cleanup = (owner.cleanup(close_error is None, retain_reason="capture-close-unconfirmed")
                   if owner is not None else CleanupResult("not-created"))
    except BaseException:
        primary.add_note("spool abort cleanup was interrupted")
        cleanup = owner.result or owner._retained("cleanup-unconfirmed")
    if cleanup.leftovers:
        primary.add_note(f"scratch retained: {cleanup.leftovers[0].path}")
    if isinstance(primary, _Deadline):
        raise SpoolError("deadline-exceeded", "shared capture deadline expired", cleanup) from primary
    if isinstance(primary, SpoolError):
        primary.cleanup = cleanup
        raise primary.with_traceback(primary_traceback)
    if isinstance(primary, OSError):
        code = "capture-close-unconfirmed" if close_error is primary else "capture-io-error"
        raise SpoolError(code, "descriptor spool IO failed", cleanup) from primary
    raise primary.with_traceback(primary_traceback)
