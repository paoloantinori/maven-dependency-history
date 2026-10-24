"""Bounded, safe capture of file bytes for investigation evidence: at most
max_bytes + 1 bytes are read through a descriptor whose final path component
is never followed through a symlink (ancestors may be symlinks; checkout
containment and ancestor hygiene are the caller's responsibility, this
module guarantees nothing beyond the final component), only regular files
are accepted, and a capture qualifies as complete only when EOF was reached
and the source stat identity is unchanged across the read. A stat comparison
detects mid-read mutation but never proves an atomic snapshot. Captures are
immutable; comparison mutates nothing.
"""

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone


class CaptureError(ValueError):
    """Raised when a file cannot be captured safely."""


@dataclass(frozen=True)
class FileIdentity:
    """Immutable stat identity observed for one open descriptor."""

    dev: int
    ino: int
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, st) -> "FileIdentity":
        return cls(dev=st.st_dev, ino=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns)


@dataclass(frozen=True)
class CapturedFile:
    """One bounded byte snapshot; sha256 is set only for complete captures."""

    path: str
    data: bytes
    sha256: str | None
    complete: bool
    reason: str | None
    observed_at: str
    stat_before: FileIdentity
    stat_after: FileIdentity


@dataclass(frozen=True)
class CaptureChange:
    """How two captures of the same path differ, with both observations."""

    path: str
    reasons: tuple[str, ...]
    before_digest: str | None
    after_digest: str | None
    before_observed_at: str
    after_observed_at: str


def _fstat(fd: int) -> os.stat_result:
    """Single descriptor stat hook; tests inject stat drift through it."""
    return os.fstat(fd)


def _open_flags() -> int:
    """Safe-open flags, failing closed when the platform lacks either guard."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not no_follow or not nonblock:
        raise CaptureError(
            "safe file capture needs O_NOFOLLOW and O_NONBLOCK; unsupported on this platform"
        )
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow | nonblock


def _bounded_read(fd: int, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most max_bytes + 1 bytes; the extra byte is a sentinel and the
    boolean reports whether EOF was hit before the sentinel was filled."""
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            return b"".join(chunks), True
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks), False


def capture_file(path: str | os.PathLike, max_bytes: int) -> CapturedFile:
    """Capture up to max_bytes bytes of a regular file. Only the final path
    component is protected against symlink following; symlinked ancestors are
    traversed and containment of the captured path is the caller's
    responsibility. The subject is stored as the absolute spelling of `path`
    (never realpath). Relative and absolute spellings that normalize to the
    same lexical path compare as one subject; different symlinked-ancestor
    spellings remain distinct subjects. Truncated or mid-read-changed sources come back
    complete=False with a reason and never with a full-content digest.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError(f"max_bytes must be int, got {type(max_bytes).__name__}")
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")
    subject = os.path.abspath(os.fsdecode(os.fspath(path)))
    try:
        link_mode = os.lstat(subject).st_mode
    except OSError as exc:
        label = "missing file" if exc.errno == errno.ENOENT else f"cannot inspect {subject}"
        raise CaptureError(f"{label}: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(link_mode):
        raise CaptureError(f"refusing to follow symlink: {subject}")
    flags = _open_flags()
    try:
        fd = os.open(subject, flags)
    except OSError as exc:
        raise CaptureError(f"cannot open {subject}: {exc.strerror or exc}") from exc
    try:
        st_before_raw = _fstat(fd)
        if not stat.S_ISREG(st_before_raw.st_mode):
            raise CaptureError(f"not a regular file: {subject}")
        data, eof = _bounded_read(fd, max_bytes)
        stat_after = FileIdentity.from_stat(_fstat(fd))
    finally:
        os.close(fd)
    stat_before = FileIdentity.from_stat(st_before_raw)
    changed = stat_before != stat_after
    complete = eof and not changed
    notes = []
    if not eof:
        notes.append(f"truncated at max_bytes={max_bytes}")
    if changed:
        notes.append("source changed during read")
    if not eof:
        data = data[:max_bytes]
    return CapturedFile(
        path=subject,
        data=data,
        sha256=hashlib.sha256(data).hexdigest() if complete else None,
        complete=complete,
        reason="; ".join(notes) if notes else None,
        observed_at=datetime.now(timezone.utc).isoformat(),
        stat_before=stat_before,
        stat_after=stat_after,
    )


def compare_captures(before: CapturedFile, after: CapturedFile) -> CaptureChange | None:
    """Qualify how two captures of the same path differ; None means nothing
    observable changed. Neither capture object is mutated.
    """
    for capture in (before, after):
        if not isinstance(capture, CapturedFile):
            raise TypeError(f"captures must be CapturedFile, got {type(capture).__name__}")
    if before.path != after.path:
        raise ValueError(f"cannot compare different paths: {before.path!r} vs {after.path!r}")
    reasons = []
    if before.data != after.data:
        reasons.append("captured data differs")
    if before.complete != after.complete:
        reasons.append("completeness differs")
    if before.stat_after != after.stat_after:
        reasons.append("source identity differs")
    if not reasons:
        return None
    return CaptureChange(
        path=before.path,
        reasons=tuple(reasons),
        before_digest=before.sha256,
        after_digest=after.sha256,
        before_observed_at=before.observed_at,
        after_observed_at=after.observed_at,
    )
