"""Bounded, execution-free inspection of one packaged archive copy (JAR/WAR
style ZIP plus nested archives), for Maven dependency evidence.

Contract highlights, so no caller has to guess:

- Only the stdlib zipfile module parses; this module writes no ZIP parser of
  its own. The one allocation zipfile performs before any caller-visible
  bound, the central-directory read in ZipFile._RealGetContents
  (``fp.read(endrec[_ECD_SIZE])`` plus one ZipInfo per entry, verified against
  the CPython 3.14.7 stdlib source used for the verification), is bounded
  BEFORE ZipFile is constructed: the end-of-central-directory record is
  preflighted through ``zipfile._EndRecData`` and the declared central
  directory size and entry count are checked against the shared budgets. If
  those private helpers are missing at some future Python version the archive
  is reported ``unsupported`` (``stdlib-preflight-unavailable``), never
  guessed; metadata counts are re-checked against the actual listed length
  after construction because end records can lie.
- Content is never executed and never extracted to disk. Nested archives are
  inspected from the bounded in-memory bytes of their entry, and the SHA256
  of those exact unmodified bytes identifies the copy even when inspection of
  its children is incomplete: identity completeness and inspection coverage
  are separate fields.
- Outer input comes through mdi.capture.capture_file: bounded to
  max_input_bytes (an initial bounded in-memory capture decision, not a final
  2 GiB product cap), final path component never a symlink, regular files
  only, ancestors the caller's responsibility. Unsafe outer capture raises
  mdi.capture.CaptureError; an oversized or mid-read-changed capture returns a
  clearly rejected result instead.
- Duplicate entry names are preserved as distinct records keyed by archive
  and index; reads always go through the ZipInfo object, never name lookup
  (which would silently pick the last duplicate).
- Honest limits: ZIP64 archives and multi-disk archives are declined, not
  inspected; only STORED and DEFLATED entries are read (others recorded with
  ``unsupported-compression``); the deadline is a monotonic clock checked
  between reads and entries, not a hard interrupt of one native decompressor
  call, and bounded inputs plus bounded decompression still leave CPU
  isolation to a future process boundary this module does not provide.
"""

import hashlib
import io
import os
import re
import stat
import struct
import time
import zipfile
import zlib
from dataclasses import dataclass

from mdi.capture import CapturedFile, capture_file

_CHUNK = 64 * 1024
_SUPPORTED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_ENCRYPTED_FLAG = 0x1  # general purpose bit 0; zipfile has no public constant
ARCHIVE_SUFFIXES = (".jar", ".war", ".zip", ".ear")
_RAW_EVIDENCE_CAP = 4096
_POM_PROPERTIES_RE = re.compile(r"(?:^|/)META-INF/maven/[^/]+/[^/]+/pom\.properties$")
_PROPERTY_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PARTIAL_ENTRY_STATUSES = frozenset(
    {"read-error", "size-mismatch", "too-large", "expanded-budget", "depth-limit", "skipped"}
)
# zipfile private preflight surface, names verified against the running stdlib
# (CPython 3.14 zipfile/__init__.py); every use is guarded by _endrec below.
_ZIPFILE_PREFLIGHT_ATTRS = (
    "_EndRecData",
    "_ECD_SIGNATURE",
    "_ECD_SIZE",
    "_ECD_ENTRIES_TOTAL",
    "_ECD_DISK_NUMBER",
    "_ECD_DISK_START",
    "_ECD_LOCATION",
    "stringEndArchive64",
)


def _monotonic() -> float:
    """Deadline clock hook; tests inject drift through it."""
    return time.monotonic()


@dataclass(frozen=True)
class ArchiveLimits:
    """Positive shared budgets for one inspect_archive call.

    max_input_bytes bounds the initial in-memory capture of the outer file
    (an explicit early bound, not the eventual full-product figure); the
    others bound the whole nested traversal cumulatively.
    """

    max_input_bytes: int = 64 * 1024 * 1024
    max_expanded_bytes: int = 128 * 1024 * 1024
    max_entry_bytes: int = 16 * 1024 * 1024
    max_entries: int = 20000
    max_depth: int = 4
    max_metadata_bytes: int = 4 * 1024 * 1024
    max_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in (
            "max_input_bytes",
            "max_expanded_bytes",
            "max_entry_bytes",
            "max_entries",
            "max_depth",
            "max_metadata_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        seconds = self.max_seconds
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError(f"max_seconds must be a number, got {type(seconds).__name__}")
        if seconds <= 0:
            raise ValueError(f"max_seconds must be positive, got {seconds}")


class _Abort(Exception):
    """Stop inspecting one archive; its record stays with the given status."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class _State:
    """Shared traversal state: budgets, records, and the deadline."""

    def __init__(self, limits: ArchiveLimits):
        self.limits = limits
        self.deadline = _monotonic() + limits.max_seconds
        self.stopped = False
        self.deadline_hit = False
        self.expanded_used = 0
        self.entries_used = 0
        self.archives: list[dict] = []
        self.entries: list[dict] = []
        self.issues: list[dict] = []

    def expired(self) -> bool:
        if self.stopped:
            return True
        if _monotonic() > self.deadline:
            self.stopped = True
            if not self.deadline_hit:
                self.deadline_hit = True
                self.issue(None, "deadline", f"time budget of {self.limits.max_seconds}s exhausted")
            return True
        return False

    def charge(self, count: int) -> None:
        self.expanded_used += count

    def issue(self, archive_id, code: str, detail: str) -> None:
        self.issues.append({"archive_id": archive_id, "code": code, "detail": detail})


def inspect_archive(path, *, limits: ArchiveLimits = ArchiveLimits()) -> dict:
    """Inspect one JAR/WAR file including nested archives; JSON-safe result.

    Returns ``{"archives": [...], "entries": [...], "issues": [...],
    "coverage": {...}}``. Unsafe outer capture (symlink final component,
    non-regular file, missing) raises mdi.capture.CaptureError; an oversized
    or mid-read-changed outer file yields a rejected result with an
    ``input-truncated``/``input-changed`` issue.
    """
    if not isinstance(limits, ArchiveLimits):
        raise TypeError(f"limits must be ArchiveLimits, got {type(limits).__name__}")
    captured = capture_file(path, limits.max_input_bytes)
    return inspect_captured_archive(captured, limits=limits)


def _rejected_result(captured, limits, code, detail, input_truncated):
    return {
        "archives": [],
        "entries": [],
        "issues": [{"archive_id": None, "code": code, "detail": detail}],
        "coverage": _coverage(
            limits=limits,
            captured=captured,
            archives=[],
            entries=[],
            expanded_used=0,
            deadline_hit=False,
            stopped=False,
            status="rejected",
            input_truncated=input_truncated,
        ),
    }


def inspect_captured_archive(captured, *, limits=ArchiveLimits()) -> dict:
    """Inspect already-captured archive bytes; same result shape as
    inspect_archive, which delegates here after its own capture.

    The captured object is validated, not trusted blindly: data larger than
    limits.max_input_bytes is rejected even when the caller captured with a
    bigger cap, and a complete capture whose stored digest does not match a
    recompute over data is rejected with input-digest-mismatch, so byte
    identity is never carried on an unverified claim. Incomplete captures
    keep the input-truncated/input-changed classification of inspect_archive.
    """
    if not isinstance(captured, CapturedFile):
        raise TypeError(f"captured must be CapturedFile, got {type(captured).__name__}")
    if not isinstance(limits, ArchiveLimits):
        raise TypeError(f"limits must be ArchiveLimits, got {type(limits).__name__}")
    if len(captured.data) > limits.max_input_bytes:
        return _rejected_result(
            captured, limits, "input-exceeds-limit",
            f"captured data is {len(captured.data)} bytes, over max_input_bytes="
            f"{limits.max_input_bytes}", input_truncated=False)
    if captured.complete:
        recompute = hashlib.sha256(captured.data).hexdigest()
        if captured.sha256 != recompute:
            return _rejected_result(
                captured, limits, "input-digest-mismatch",
                "stored digest does not match captured data; refusing to inspect",
                input_truncated=False)
    if not captured.complete:
        # Compatibility dependency, kept deliberately narrow: the truncated
        # vs changed distinction is read out of mdi.capture's diagnostic
        # wording ("truncated" appears only in the max_bytes reason). The
        # end-to-end tests in tests/test_archives.CaptureIntegrationTest run
        # the real capture_file, so rewording capture.py's reasons must fail
        # those tests instead of silently reclassifying rejections here.
        code = "input-truncated" if "truncated" in (captured.reason or "") else "input-changed"
        return _rejected_result(captured, limits, code, captured.reason,
                                input_truncated=code == "input-truncated")
    state = _State(limits)
    basename = os.path.basename(captured.path)
    _inspect_container(
        state,
        captured.data,
        basename,
        captured.path,
        captured.path,
        [],
        captured.sha256,
    )
    complete = sum(1 for a in state.archives if a["inspection_status"] == "complete")
    status = "inspected" if not state.stopped and complete == len(state.archives) else "partial"
    return {
        "archives": state.archives,
        "entries": state.entries,
        "issues": state.issues,
        "coverage": _coverage(
            limits=limits,
            captured=captured,
            archives=state.archives,
            entries=state.entries,
            expanded_used=state.expanded_used,
            deadline_hit=state.deadline_hit,
            stopped=state.stopped,
            status=status,
            input_truncated=False,
        ),
    }


def _inspect_container(state, data, archive_id, path, display_path, chain, digest):
    """Inspect one archive (outer file or nested bytes); returns its record."""
    record = {
        "archive_id": archive_id,
        "path": path,
        "display_path": display_path,
        "entry_chain": list(chain),
        "size": len(data),
        "sha256": digest,
        "identity": {"status": "unknown", "conflict": False, "candidates": [], "evidence": []},
        "inspection_status": "complete",
    }
    state.archives.append(record)
    if state.expired():
        record["inspection_status"] = "skipped"
        return record
    try:
        zf = _open_bounded(state, record, data)
    except _Abort as abort:
        record["inspection_status"] = abort.status
        return record
    partial = False
    evidence: list[dict] = []
    candidates: list[dict] = []
    try:
        for index, info in enumerate(zf.infolist()):
            if state.stopped:
                partial = True
                break
            # Re-check the shared budget before every entry: recursion into a
            # nested archive consumes the same pool, so a parent that passed
            # its preflight can still run out mid-list once children are
            # counted. Plain enforcement, no reservation, so nested archives
            # keep using whatever budget is genuinely left.
            if state.entries_used >= state.limits.max_entries:
                state.issue(
                    archive_id,
                    "entries-limit",
                    f"shared entry budget of {state.limits.max_entries} exhausted before "
                    f"entry index {index}",
                )
                partial = True
                break
            entry, retained = _record_entry(state, zf, record, info, index, evidence, candidates)
            if entry["status"] in _PARTIAL_ENTRY_STATUSES:
                partial = True
            if entry["status"] == "hashed" and entry["is_archive"]:
                child_chain = chain + [index]
                if len(child_chain) > state.limits.max_depth:
                    entry["status"] = "depth-limit"
                    state.issue(archive_id, "entry-depth-limit", f"index={index} name={entry['name']!r}")
                    partial = True
                else:
                    state.charge(len(retained))  # container bytes counted again, conservatively
                    _inspect_container(
                        state,
                        retained,
                        f"{archive_id}[{index}]",
                        path,
                        f"{display_path}!{entry['name']}",
                        child_chain,
                        entry["sha256"],
                    )
    finally:
        zf.close()
    record["identity"] = _identity(evidence, candidates)
    if partial:
        record["inspection_status"] = "partial"
    return record


def _open_bounded(state, record, data):
    """Preflight the end record, then construct ZipFile inside the budgets."""
    limits = state.limits
    archive_id = record["archive_id"]
    endrec = _endrec(state, archive_id, data)
    remaining = limits.max_entries - state.entries_used
    declared = endrec["entries_total"]
    if declared > remaining:
        state.issue(
            archive_id,
            "entries-limit",
            f"declares {declared} entries, shared budget allows {remaining} more",
        )
        raise _Abort("rejected")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except NotImplementedError as exc:
        state.issue(archive_id, "unsupported-zip-version", str(exc))
        raise _Abort("unsupported") from exc
    except (zipfile.BadZipFile, struct.error, ValueError, OSError, EOFError) as exc:
        state.issue(archive_id, "malformed-zip", f"{type(exc).__name__}: {exc}")
        raise _Abort("malformed") from exc
    try:
        actual = len(zf.infolist())
        if actual > remaining:
            state.issue(
                archive_id,
                "entries-limit",
                f"central directory lists {actual} entries, shared budget allows {remaining} more",
            )
            raise _Abort("rejected")
        if actual != declared:
            state.issue(
                archive_id,
                "entries-count-mismatch",
                f"end record claims {declared} entries, central directory lists {actual}",
            )
    except _Abort:
        zf.close()
        raise
    return zf


def _endrec(state, archive_id, data):
    """Bounded end-of-central-directory preflight; the reason ZipFile is safe."""
    helpers = {}
    for attr in _ZIPFILE_PREFLIGHT_ATTRS:
        value = getattr(zipfile, attr, None)
        if value is None:
            state.issue(
                archive_id,
                "stdlib-preflight-unavailable",
                f"zipfile.{attr} is missing at this Python version; refusing to parse unbounded",
            )
            raise _Abort("unsupported")
        helpers[attr] = value
    try:
        endrec = helpers["_EndRecData"](io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        state.issue(archive_id, "malformed-zip", f"end record unreadable: {exc}")
        raise _Abort("malformed") from exc
    except (OSError, struct.error, ValueError) as exc:
        state.issue(archive_id, "malformed-zip", f"{type(exc).__name__}: {exc}")
        raise _Abort("malformed") from exc
    if endrec is None:
        state.issue(archive_id, "not-a-zip", "no end-of-central-directory record found")
        raise _Abort("malformed")
    location = endrec[helpers["_ECD_LOCATION"]]
    # _EndRecData64 rewrites _ECD_LOCATION when it applies a ZIP64 record, so
    # the applied signature, not the location, is the ZIP64 evidence.
    if endrec[helpers["_ECD_SIGNATURE"]] == helpers["stringEndArchive64"]:
        state.issue(
            archive_id,
            "zip64-unsupported",
            "ZIP64 end-of-central-directory record applied; ZIP64 archives are declined",
        )
        raise _Abort("unsupported")
    if endrec[helpers["_ECD_DISK_NUMBER"]] != 0 or endrec[helpers["_ECD_DISK_START"]] != 0:
        state.issue(archive_id, "multi-disk", "archive spans multiple disks")
        raise _Abort("unsupported")
    size_cd = endrec[helpers["_ECD_SIZE"]]
    if size_cd > state.limits.max_metadata_bytes:
        state.issue(
            archive_id,
            "central-directory-too-large",
            f"declared central directory of {size_cd} bytes exceeds "
            f"max_metadata_bytes={state.limits.max_metadata_bytes}",
        )
        raise _Abort("rejected")
    if size_cd > len(data) or location < size_cd:
        state.issue(
            archive_id,
            "central-directory-out-of-range",
            f"declared central directory of {size_cd} bytes at location {location} "
            f"in a {len(data)}-byte archive",
        )
        raise _Abort("malformed")
    return {"entries_total": endrec[helpers["_ECD_ENTRIES_TOTAL"]], "size_cd": size_cd}


def _record_entry(state, zf, record, info, index, evidence, candidates):
    """Record one entry; reads go through the ZipInfo object, never a name."""
    archive_id = record["archive_id"]
    name = info.filename
    kind = _kind_of(info)
    entry = {
        "archive_id": archive_id,
        "index": index,
        "name": name,
        "kind": kind,
        "size_declared": info.file_size,
        "size_actual": None,
        "sha256": None,
        "status": None,
        "compression": info.compress_type,
        "is_archive": False,
    }
    retained = None
    violation = _name_violation(info)
    if violation is not None:
        entry["status"] = "name-rejected"
        entry["note"] = violation
    elif kind == "symlink":
        entry["status"] = "symlink"
    elif kind == "directory":
        entry["status"] = "recorded"
        entry["size_actual"] = 0
    elif info.flag_bits & _ENCRYPTED_FLAG:
        entry["status"] = "encrypted"
    elif info.compress_type not in _SUPPORTED_COMPRESSION:
        entry["status"] = "unsupported-compression"
    else:
        want_metadata = _POM_PROPERTIES_RE.match(name) is not None
        want_archive = name.lower().endswith(ARCHIVE_SUFFIXES)
        retained, status, size_actual, digest, note = _read_stream(
            state, zf, info, want_metadata=want_metadata, want_archive=want_archive
        )
        entry.update(
            status=status, size_actual=size_actual, sha256=digest, is_archive=want_archive
        )
        if note:
            entry["note"] = note
        if want_metadata and status == "hashed":
            _collect_evidence(evidence, candidates, entry, retained)
    if entry["status"] not in ("hashed", "recorded"):
        state.issue(archive_id, f"entry-{entry['status']}", f"index={index} name={name!r}")
    state.entries.append(entry)
    state.entries_used += 1
    return entry, retained


def _read_stream(state, zf, info, *, want_metadata, want_archive):
    """Read one entry in 64 KiB chunks under both byte budgets.

    Reads at most allowed+1 bytes so a sentinel byte proves the entry or
    budget was actually exceeded; the SHA256 is only reported for a clean
    read to end of stream. Only nested-archive containers and pom.properties
    evidence are retained, everything else is hashed and dropped.
    """
    limits = state.limits
    global_left = limits.max_expanded_bytes - state.expanded_used
    allowed = min(limits.max_entry_bytes, global_left)
    if allowed < 0:
        return None, "expanded-budget", 0, None, None
    hasher = hashlib.sha256()
    parts = bytearray() if (want_archive or want_metadata) else None
    total = 0
    eof = False
    try:
        with zf.open(info) as stream:
            while total <= allowed:
                if state.expired():
                    state.charge(total)
                    return None, "skipped", total, None, "deadline reached mid-entry"
                want = min(_CHUNK, allowed + 1 - total)
                chunk = stream.read(want)
                if not chunk:
                    eof = True
                    break
                total += len(chunk)
                hasher.update(chunk)
                if parts is not None:
                    parts += chunk
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, ValueError, zlib.error) as exc:
        state.charge(total)
        return None, "read-error", total, None, f"{type(exc).__name__}: {exc}"
    state.charge(total)
    retained = bytes(parts) if parts is not None else None
    if total > allowed:
        status = "too-large" if limits.max_entry_bytes <= global_left else "expanded-budget"
        return retained, status, total, None, None
    digest = hasher.hexdigest()
    status = "hashed" if total == info.file_size else "size-mismatch"
    return retained, status, total, digest, None


def _kind_of(info):
    if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
        return "symlink"
    if info.is_dir():
        return "directory"
    return "file"


def _name_violation(info):
    """Reason an entry name is refused, or None. ZipInfo already truncates
    names at the first NUL on read, so the raw/normalized comparison is the
    evidence a NUL was there."""
    if info.orig_filename != info.filename or "\x00" in info.orig_filename:
        return "NUL byte in entry name"
    name = info.filename
    if "\\" in name:
        return "backslash in entry name"
    if name.startswith("/"):
        return "absolute entry name"
    if any(part == ".." for part in name.split("/")):
        return "parent traversal in entry name"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return "control character in entry name"
    return None


def _parse_properties(raw):
    """Recognize only ordinary ASCII key=value properties; anything richer
    (escapes, continuations, non-ASCII, non-'=' separators, conflicting
    duplicates) is reported unsupported rather than half-parsed. Leading and
    trailing whitespace around values is trimmed; candidates stay advisory."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None, "non-ascii bytes"
    values: dict[str, str] = {}
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line.endswith("\\"):
            return None, "continuation line"
        if "\\" in line:
            return None, "escape sequence"
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in line):
            return None, "control character"
        if "=" not in line:
            return None, "line without '=' separator"
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not _PROPERTY_KEY_RE.match(key):
            return None, f"key is not a plain identifier: {key!r}"
        if key in values and values[key] != value:
            return None, f"conflicting duplicate key {key!r}"
        values[key] = value
    coords = {key: values.get(key) for key in ("groupId", "artifactId", "version")}
    if not all(coords.values()):
        return None, "missing groupId/artifactId/version keys"
    return coords, None


def _collect_evidence(evidence, candidates, entry, raw):
    # Analysis always sees the complete metadata bytes the entry budget
    # allowed to be read; only the human-facing preview below is capped, so a
    # conflict, escape, or value past the preview boundary can never be cut
    # into a fabricated or silenced candidate. If the bytes on hand are not
    # the whole entry for any reason, the outcome is truncated, never a
    # candidate.
    item = {
        "entry_index": entry["index"],
        "entry_name": entry["name"],
        "raw": raw[:_RAW_EVIDENCE_CAP].decode("ascii", "replace"),
        "raw_truncated": len(raw) > _RAW_EVIDENCE_CAP,
    }
    if len(raw) != entry["size_declared"]:
        item["parse"] = "truncated"
        item["reason"] = f"only {len(raw)} of {entry['size_declared']} metadata bytes captured"
    else:
        coords, reason = _parse_properties(raw)
        if coords is None:
            item["parse"] = "unsupported"
            item["reason"] = reason
        else:
            item["parse"] = "simple"
            candidates.append(
                {
                    "groupId": coords["groupId"],
                    "artifactId": coords["artifactId"],
                    "version": coords["version"],
                    "entry_index": entry["index"],
                    "entry_name": entry["name"],
                }
            )
    evidence.append(item)


def _identity(evidence, candidates):
    unique = {(c["groupId"], c["artifactId"], c["version"]) for c in candidates}
    return {
        "status": "candidate" if candidates else "unknown",
        "conflict": len(unique) > 1,
        "candidates": candidates,
        "evidence": evidence,
    }


def _coverage(*, limits, captured, archives, entries, expanded_used, deadline_hit,
              stopped, status, input_truncated):
    complete = sum(1 for a in archives if a["inspection_status"] == "complete")
    hashed = sum(1 for e in entries if e["status"] == "hashed")
    return {
        "status": status,
        "input_bytes": len(captured.data),
        "input_truncated": input_truncated,
        "outer_capture": {
            "path": captured.path,
            "complete": captured.complete,
            "reason": captured.reason,
            "sha256": captured.sha256,
            "nofollow": "final-component",
            "ancestors": "caller-responsibility",
            "observed_at": captured.observed_at,
        },
        "archives_total": len(archives),
        "archives_complete": complete,
        "entries_recorded": len(entries),
        "entries_hashed": hashed,
        "expanded_bytes_used": expanded_used,
        "expanded_bytes_limit": limits.max_expanded_bytes,
        "entries_budget_used": len(entries),
        "entries_budget_limit": limits.max_entries,
        "deadline_exceeded": deadline_hit,
        "stopped_early": stopped,
    }
