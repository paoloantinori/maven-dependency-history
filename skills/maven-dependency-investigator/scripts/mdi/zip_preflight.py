"""Bound ZIP central-directory allocation before constructing a ZipFile.

The input is an already captured, seekable binary file object or a caller-owned
regular-file descriptor. No pathname is opened and no member is decompressed.
Descriptor reads use pread, preserving both ownership and the shared file offset;
objects without fileno (such as BytesIO) are temporarily sought and restored.
Callers must not concurrently modify, seek, close or reuse the supplied source.

The stdlib end-record reader applies ZIP64 and prepended-data handling. Its I/O
is bounded here even if a future implementation requests an unbounded read.
A structural walk then advances by each central header and its three variable
lengths, never by counting signature bytes in filenames or comments. Declared
entry counts are observations, not allocation authority; actual records enforce
the caller's remaining entry budget before any ZipInfo is allocated.

This module verifies the post-ZIP64 location convention used by the local
Python 3.14 zipfile source (_handle_prepended_data). Missing private interfaces
fail closed rather than guessing an older runtime's ZIP64 offset convention.
The caller must retain the post-ZipFile count check and immutable captured input:
this preflight is not validation of member data or a filesystem snapshot. Deadline
checks are cooperative around I/O; use a killable worker for a hard stage deadline.
"""

import io
import math
import os
import stat
import struct
import time
import zipfile


_TAIL_CAP = 65535 + 22
_READ_CAP = _TAIL_CAP
_END_READ_BUDGET = _TAIL_CAP + 256


class PreflightError(ValueError):
    """Refused input or unsupported runtime, carrying a stable machine code."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise PreflightError("archive stage deadline exhausted", "deadline-exceeded")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Source:
    """Bounded cursor independent of a file descriptor's shared offset."""

    def __init__(self, source, deadline, budget):
        self.deadline = deadline
        self.remaining = budget
        self.position = 0
        self.fd = None
        self.saved_position = None
        self.file = None
        self.identity = None
        if isinstance(source, bool):
            raise PreflightError("source must be a regular fd or seekable binary file", "invalid-source")
        if isinstance(source, int):
            self.fd = source
        else:
            try:
                self.fd = source.fileno()
            except (AttributeError, io.UnsupportedOperation):
                self.file = source
        if self.fd is not None:
            if not hasattr(os, "pread"):
                raise PreflightError("pread is required for descriptor-safe inspection", "invalid-source")
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode):
                raise PreflightError("archive descriptor must refer to a regular file", "invalid-source")
            self.size = info.st_size
            self.identity = _identity(info)
        else:
            if not all(callable(getattr(source, name, None)) for name in ("seek", "tell", "read")):
                raise PreflightError("source must be a seekable binary file", "invalid-source")
            self.saved_position = source.tell()
            try:
                source.seek(0, os.SEEK_END)
                self.size = source.tell()
            finally:
                source.seek(self.saved_position)
            if type(self.size) is not int or self.size < 0:
                raise PreflightError("invalid source size", "invalid-source")

    def seek(self, offset, whence=os.SEEK_SET):
        _deadline(self.deadline)
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self.position + offset
        elif whence == os.SEEK_END:
            position = self.size + offset
        else:
            raise ValueError("invalid seek mode")
        if position < 0:
            raise OSError("negative archive seek")
        self.position = position
        return position

    def tell(self):
        return self.position

    def read(self, size=-1):
        _deadline(self.deadline)
        if type(size) is not int or size < 0 or size > _READ_CAP:
            raise PreflightError("unbounded stdlib preflight read refused", "unbounded-read")
        if size > self.remaining:
            raise PreflightError("preflight read budget exhausted", "read-budget-exceeded")
        # Charge requested bytes, not returned bytes: repeated short reads
        # cannot sidestep the allowance.
        self.remaining -= size
        if self.fd is not None:
            data = os.pread(self.fd, size, self.position)
        else:
            self.file.seek(self.position)
            data = self.file.read(size)
        _deadline(self.deadline)
        if not isinstance(data, bytes) or len(data) > size:
            raise PreflightError("source violated bounded binary read", "invalid-source")
        self.position += len(data)
        return data

    def finish(self):
        if self.fd is not None:
            if _identity(os.fstat(self.fd)) != self.identity:
                raise PreflightError("archive changed during preflight", "input-changed")
        elif self.saved_position is not None:
            self.file.seek(self.saved_position)


def _runtime():
    names = (
        "_EndRecData", "_handle_prepended_data", "structCentralDir", "sizeCentralDir",
        "stringCentralDir", "stringEndArchive", "stringEndArchive64",
        "_ECD_SIGNATURE", "_ECD_LOCATION", "_ECD_SIZE", "_ECD_OFFSET",
        "_ECD_DISK_NUMBER", "_ECD_DISK_START", "_ECD_ENTRIES_TOTAL",
        "_ECD_ENTRIES_THIS_DISK", "_ECD_COMMENT", "_ECD_COMMENT_SIZE",
        "_CD_SIGNATURE", "_CD_FILENAME_LENGTH", "_CD_EXTRA_FIELD_LENGTH",
        "_CD_COMMENT_LENGTH", "_CD_DISK_NUMBER_START",
    )
    if any(getattr(zipfile, name, None) is None for name in names):
        raise PreflightError("required zipfile preflight interfaces unavailable", "stdlib-preflight-unavailable")
    if not callable(zipfile._EndRecData) or not callable(zipfile._handle_prepended_data):
        raise PreflightError("required zipfile preflight readers unavailable", "stdlib-preflight-unavailable")
    if zipfile.sizeCentralDir != 46 or struct.calcsize(zipfile.structCentralDir) != 46:
        raise PreflightError("unsupported central-directory header layout", "stdlib-preflight-unavailable")


def _inspect(source, max_entries, max_metadata_bytes):
    if source.size < 22:
        raise PreflightError("no end-of-central-directory record", "not-a-zip")
    end = zipfile._EndRecData(source)
    if end is None:
        raise PreflightError("no end-of-central-directory record", "not-a-zip")
    signature = end[zipfile._ECD_SIGNATURE]
    if signature not in (zipfile.stringEndArchive, zipfile.stringEndArchive64):
        raise PreflightError("unsupported end record", "malformed-zip")
    if end[zipfile._ECD_DISK_NUMBER] or end[zipfile._ECD_DISK_START]:
        raise PreflightError("multidisk archives are unsupported", "multi-disk")
    if len(end[zipfile._ECD_COMMENT]) != end[zipfile._ECD_COMMENT_SIZE]:
        raise PreflightError("truncated archive comment", "malformed-zip")
    length = end[zipfile._ECD_SIZE]
    location = end[zipfile._ECD_LOCATION]
    if length > max_metadata_bytes:
        raise PreflightError("central directory exceeds metadata budget", "central-directory-too-large")
    start = location - length
    if start < 0 or location > source.size or start < end[zipfile._ECD_OFFSET]:
        raise PreflightError("central directory is outside the source", "central-directory-out-of-range")
    source.remaining = length
    source.seek(start)
    count = 0
    while source.tell() < location:
        _deadline(source.deadline)
        if location - source.tell() < zipfile.sizeCentralDir:
            raise PreflightError("truncated central-directory header", "malformed-central-directory")
        header = source.read(zipfile.sizeCentralDir)
        if len(header) != zipfile.sizeCentralDir:
            raise PreflightError("truncated central-directory header", "malformed-central-directory")
        values = struct.unpack(zipfile.structCentralDir, header)
        if values[zipfile._CD_SIGNATURE] != zipfile.stringCentralDir:
            raise PreflightError("invalid central-directory signature", "malformed-central-directory")
        if values[zipfile._CD_DISK_NUMBER_START]:
            raise PreflightError("multidisk member is unsupported", "multi-disk")
        variable_length = sum(values[index] for index in (
            zipfile._CD_FILENAME_LENGTH, zipfile._CD_EXTRA_FIELD_LENGTH, zipfile._CD_COMMENT_LENGTH))
        next_header = source.tell() + variable_length
        if next_header > location:
            raise PreflightError("central-directory lengths exceed span", "malformed-central-directory")
        count += 1
        if count > max_entries:
            raise PreflightError("central directory exceeds entry budget", "entries-limit")
        source.seek(next_header)
    _deadline(source.deadline)
    declared = end[zipfile._ECD_ENTRIES_TOTAL]
    disk_declared = end[zipfile._ECD_ENTRIES_THIS_DISK]
    return {
        "format": "zip64" if signature == zipfile.stringEndArchive64 else "zip",
        "file_size": source.size,
        "central_directory_offset": start,
        "central_directory_bytes": length,
        "prepended_bytes": start - end[zipfile._ECD_OFFSET],
        "entries_declared": declared,
        "entries_this_disk_declared": disk_declared,
        "entries_actual": count,
        "count_matches": count == declared == disk_declared,
    }


def preflight_zip(source, *, max_entries=20000, max_metadata_bytes=4 * 1024 * 1024,
                  deadline=None):
    """Return JSON-safe central metadata without constructing ZipFile/ZipInfo.

    `max_entries` is a remaining shared budget and may be zero for an empty
    archive. `deadline`, when supplied, is an absolute monotonic stage deadline,
    not a fresh duration per file. Errors have stable PreflightError.code values.
    A descriptor's offset and ownership are unchanged on success and failure;
    seekable file objects without a descriptor have their initial position
    restored. Neither kind of source is closed here. Only regular descriptors
    are accepted; generic binary objects are trusted caller-provided I/O seams.
    """
    for name, value, minimum in (("max_entries", max_entries, 0),
                                  ("max_metadata_bytes", max_metadata_bytes, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                                 or not math.isfinite(deadline)):
        raise ValueError("deadline must be a finite monotonic timestamp")
    _deadline(deadline)
    _runtime()
    reader = None
    try:
        reader = _Source(source, deadline, _END_READ_BUDGET)
        return _inspect(reader, max_entries, max_metadata_bytes)
    except PreflightError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, struct.error, zipfile.BadZipFile) as exc:
        raise PreflightError("ZIP metadata is malformed or unreadable", "malformed-zip") from exc
    finally:
        if reader is not None:
            try:
                reader.finish()
            except PreflightError:
                raise
            except (OSError, ValueError) as exc:
                raise PreflightError("source position or identity could not be restored", "invalid-source") from exc
