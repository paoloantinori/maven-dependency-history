"""File-backed archive traversal for the future isolated archive worker.

Input is a caller-owned read-only regular descriptor plus its expected complete
SHA256. Reads use pread through independent cursors, so neither caller ownership
nor offset changes. The expected digest is verified in bounded chunks before ZIP
parsing; source stat identity is checked again after traversal. Nested recursion
also verifies the final-write identity and complete digest before reading metadata,
so a changed spool cannot combine an earlier digest with later coordinates. This is tamper
evidence, not protection against concurrent same-user modification.

Nested container bytes stream to exclusive 0600 generated files in a caller-owned
0700 scratch directory. Nesting can be narrowed but never exceeds four levels,
so recursive traversal and simultaneously live nested descriptors stay bounded. Only this call's verified files are removed, after their
readers close. A replacement is retained and reported, never deleted by name.
The caller owns the scratch directory and its lifetime; a worker killed mid-call
cannot execute our finally blocks, so its parent must retain the directory until
termination is confirmed. This module never prunes existing scratch.

All deadlines are the SAME caller-supplied monotonic instant. Checks are
cooperative between kernel/decompressor operations, not a hard interrupt. The
future worker boundary must enforce hard time/memory limits. This module does
not change the existing CLI or in-memory archive reader's capabilities.

Narrow compatibility bridge: archive entry naming, properties parsing, metadata
preview and candidate identity reuse mdi.archives private helpers. The JSON
archives/entries/issues/coverage shape is compatible with _archive_candidates;
coverage adds explicit file-backed cleanup and limit observations.
"""

import fcntl
import hashlib
import io
import math
import os
import stat
import struct
import sys
import time
import uuid
import zipfile
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from mdi import archives
from mdi.discover import open_checkout_fd
from mdi.zip_preflight import PreflightError, preflight_zip


_CHUNK = 65536
_READ_ERRORS = (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError,
                EOFError, ValueError, struct.error, zlib.error)


@dataclass(frozen=True)
class FileArchiveLimits:
    max_input_bytes: int = 2 * 1024**3
    max_expanded_bytes: int = 2 * 1024**3
    max_entry_bytes: int = 256 * 1024**2
    max_entries: int = 20000
    max_depth: int = 4
    max_metadata_bytes: int = 4 * 1024**2
    max_metadata_entry_bytes: int = 1024**2

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if self.max_depth > 4:
            raise ValueError('max_depth may tighten but not exceed four nested levels')


def _identity(fd):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Expired(Exception):
    pass


class _Reader(io.RawIOBase):
    """Own one duplicated fd with an independent logical cursor."""
    def __init__(self, fd, check, max_read):
        super().__init__()
        self.fd = os.dup(fd)
        self.position = 0
        self.check = check
        self.max_read = max_read

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        self.check()
        base = (0 if whence == os.SEEK_SET else self.position if whence == os.SEEK_CUR
                else os.fstat(self.fd).st_size if whence == os.SEEK_END else None)
        if base is None or base + offset < 0:
            raise ValueError('invalid archive seek')
        self.position = base + offset
        return self.position

    def read(self, size=-1):
        self.check()
        if type(size) is not int or size < 0 or size > self.max_read:
            raise ValueError('unbounded archive read refused')
        # ZipFile requests the preflight-bounded central directory in one read.
        data = os.pread(self.fd, size, self.position)
        self.position += len(data)
        self.check()
        return data

    def close(self):
        primary = sys.exception()
        try:
            if not self.closed:
                os.close(self.fd)
        except OSError:
            if primary is None:
                raise
            primary.add_note('archive reader close failed while unwinding')
        finally:
            super().close()


class _State:
    def __init__(self, limits, deadline, scratch_fd, scratch_path):
        self.limits, self.deadline = limits, deadline
        self.scratch_fd, self.scratch_path = scratch_fd, scratch_path
        self.archives, self.entries, self.issues, self.leftovers = [], [], [], []
        self.expanded = 0
        self.deadline_hit = False

    def check(self):
        if time.monotonic() >= self.deadline:
            if not self.deadline_hit:
                self.issue(None, 'deadline', 'shared archive stage deadline exhausted')
            self.deadline_hit = True
            raise _Expired()

    def issue(self, archive_id, code, detail):
        self.issues.append({'archive_id': archive_id, 'code': code, 'detail': detail})


class _Nested:
    def __init__(self, state):
        self.state = state
        self.name = 'mdi-nested-' + str(uuid.uuid4()) + '.spool'
        self.fd = os.open(self.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                          | getattr(os, 'O_CLOEXEC', 0), 0o600, dir_fd=state.scratch_fd)
        try:
            self.identity = _identity(self.fd)
        except BaseException:
            os.close(self.fd)
            path = os.path.join(state.scratch_path, self.name)
            state.leftovers.append({'path': path, 'reason': 'nested-identity-unconfirmed'})
            state.issue(None, 'nested-identity-unconfirmed', path)
            raise

    def write(self, data):
        part = memoryview(data)
        try:
            while part:
                self.state.check()
                count = os.write(self.fd, part)
                if count <= 0 or count > len(part):
                    raise OSError('nested spool made invalid write progress')
                part = part[count:]
        finally:
            primary = sys.exception()
            try:
                self.identity = _identity(self.fd)
            except OSError:
                self.identity = None
                if primary is None:
                    raise
                primary.add_note('nested spool identity refresh failed; scratch retained')

    def close(self):
        removed = False
        try:
            current = os.stat(self.name, dir_fd=self.state.scratch_fd, follow_symlinks=False)
            current_identity = (current.st_dev, current.st_ino, current.st_size,
                                current.st_mtime_ns, current.st_ctime_ns)
            if not stat.S_ISREG(current.st_mode) or current_identity != self.identity:
                raise OSError('nested spool identity changed')
            if _identity(self.fd) != self.identity:
                raise OSError('nested descriptor identity changed')
            os.unlink(self.name, dir_fd=self.state.scratch_fd)
            removed = True
        except OSError:
            path = os.path.join(self.state.scratch_path, self.name)
            self.state.leftovers.append({'path': path, 'reason': 'nested-cleanup-unconfirmed'})
            self.state.issue(None, 'nested-cleanup-unconfirmed', path)
        finally:
            try:
                os.close(self.fd)
            except OSError:
                self.state.issue(None, 'nested-descriptor-close-unconfirmed',
                                 'nested descriptor close could not be confirmed')
                if not removed and not any(item['path'] == os.path.join(self.state.scratch_path, self.name)
                                           for item in self.state.leftovers):
                    self.state.leftovers.append({'path': os.path.join(self.state.scratch_path, self.name),
                                                 'reason': 'nested-cleanup-unconfirmed'})


def _read_entry(state, zf, info, nested, metadata):
    left = state.limits.max_expanded_bytes - state.expanded
    allowed = max(0, min(state.limits.max_entry_bytes, left))
    total = 0
    digest = hashlib.sha256()
    retained = bytearray() if metadata else None
    try:
        with zf.open(info) as stream:
            while total <= allowed:
                state.check()
                chunk = stream.read(min(_CHUNK, allowed + 1 - total))
                if not chunk:
                    break
                previous = total
                total += len(chunk)
                state.expanded += len(chunk)
                digest.update(chunk)
                # Never write the sentinel beyond the permitted byte count.
                if nested is not None:
                    nested.write(chunk[:max(0, allowed - previous)])
                if retained is not None:
                    if total <= state.limits.max_metadata_entry_bytes:
                        retained.extend(chunk)
                    else:
                        retained = None
    except _Expired:
        return 'skipped', total, None, None, False
    except _READ_ERRORS:
        return 'read-error', total, None, None, False
    if total > allowed:
        status = 'too-large' if state.limits.max_entry_bytes <= left else 'expanded-budget'
        return status, total, None, None, False
    if total != info.file_size:
        return 'size-mismatch', total, None, None, False
    return 'hashed', total, digest.hexdigest(), bytes(retained) if retained is not None else None, retained is not None


def _entry(state, zf, record, info, index):
    kind = archives._kind_of(info)
    item = {'archive_id': record['archive_id'], 'index': index, 'name': info.filename,
            'kind': kind, 'size_declared': info.file_size, 'size_actual': None,
            'sha256': None, 'status': None, 'compression': info.compress_type, 'is_archive': False}
    state.entries.append(item)
    violation = archives._name_violation(info)
    if violation:
        item.update(status='name-rejected', note=violation)
    elif kind == 'symlink':
        item['status'] = 'symlink'
    elif kind == 'directory':
        item.update(status='recorded', size_actual=0)
    elif info.flag_bits & 1:
        item['status'] = 'encrypted'
    elif info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        item['status'] = 'unsupported-compression'
    else:
        metadata = archives._POM_PROPERTIES_RE.match(info.filename) is not None
        is_archive = info.filename.lower().endswith(archives.ARCHIVE_SUFFIXES)
        item['is_archive'] = is_archive
        child_chain = record['entry_chain'] + [index]
        retain_child = is_archive and len(child_chain) <= state.limits.max_depth
        nested = None
        try:
            if retain_child:
                nested = _Nested(state)
            status, total, digest, raw, full_metadata = _read_entry(state, zf, info, nested, metadata)
            item.update(status=status, size_actual=total, sha256=digest)
            if metadata and status == 'hashed':
                if full_metadata:
                    archives._collect_evidence(record['identity']['evidence'], record['identity']['candidates'], item, raw)
                else:
                    record['inspection_status'] = 'partial'
                    state.issue(record['archive_id'], 'metadata-entry-too-large', f'index={index}')
            if status == 'hashed' and is_archive:
                if not retain_child:
                    item['status'] = 'depth-limit'
                elif total > state.limits.max_expanded_bytes - state.expanded:
                    record['inspection_status'] = 'partial'
                    state.issue(record['archive_id'], 'expanded-budget', 'nested container copy exceeds remaining budget')
                else:
                    # Preserve the original conservative double charge for containers.
                    state.expanded += total
                    child_start = len(state.archives)
                    _container(state, nested.fd, f"{record['archive_id']}[{index}]", record['path'],
                               record['display_path'] + '!' + info.filename, child_chain, digest, total,
                               expected_identity=nested.identity)
                    if state.archives[child_start]['sha256'] is None:
                        item.update(sha256=None, status='read-error')
        except OSError:
            item['status'] = 'read-error'
        finally:
            if nested is not None:
                primary = sys.exception()
                try:
                    nested.close()
                except OSError:
                    if primary is None:
                        raise
                    primary.add_note('nested spool cleanup failed while unwinding')
    if item['status'] not in ('hashed', 'recorded'):
        record['inspection_status'] = 'partial'
        state.issue(record['archive_id'], 'entry-' + item['status'], f'index={index} name={info.filename!r}')


def _matches_digest(state, fd, size, expected):
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        state.check()
        chunk = os.pread(fd, min(_CHUNK, size - offset), offset)
        if not chunk:
            return False
        offset += len(chunk)
        digest.update(chunk)
    state.check()
    return digest.hexdigest() == expected and os.fstat(fd).st_size == size


def _container(state, fd, archive_id, path, display, chain, digest, size, *, expected_identity=None):
    before = _identity(fd)
    archive_start, entry_start = len(state.archives), len(state.entries)
    record = {'archive_id': archive_id, 'path': path, 'display_path': display,
              'entry_chain': list(chain), 'size': size, 'sha256': digest,
              'identity': archives._identity([], []), 'inspection_status': 'complete'}
    state.archives.append(record)
    try:
        state.check()
        if expected_identity is not None:
            if before != expected_identity or not _matches_digest(state, fd, size, digest):
                state.issue(archive_id, 'input-changed', 'nested spool differs from captured container')
                record.update(sha256=None, inspection_status='partial')
                return
        meta = preflight_zip(fd, max_entries=state.limits.max_entries - len(state.entries),
                             max_metadata_bytes=state.limits.max_metadata_bytes, deadline=state.deadline)
        if not meta['count_matches']:
            state.issue(archive_id, 'entries-count-mismatch', 'end record and actual central count disagree')
            record['inspection_status'] = 'partial'
        with _Reader(fd, state.check, max(state.limits.max_metadata_bytes, 65557)) as source, zipfile.ZipFile(source) as zf:
            if len(zf.infolist()) != meta['entries_actual']:
                state.issue(archive_id, 'entries-count-mismatch', 'central directory changed after preflight')
                record['inspection_status'] = 'rejected'
                return
            for index, info in enumerate(zf.infolist()):
                state.check()
                if state.expanded > state.limits.max_expanded_bytes:
                    record['inspection_status'] = 'partial'
                    state.issue(archive_id, 'expanded-budget', 'shared expanded byte budget exhausted')
                    break
                if len(state.entries) >= state.limits.max_entries:
                    record['inspection_status'] = 'partial'
                    state.issue(archive_id, 'entries-limit', 'shared entry budget exhausted')
                    break
                _entry(state, zf, record, info, index)
    except _Expired:
        record['inspection_status'] = 'partial'
    except PreflightError as exc:
        if exc.code == 'deadline-exceeded':
            state.deadline_hit = True
        record['inspection_status'] = ('unsupported' if exc.code in
            ('multi-disk', 'stdlib-preflight-unavailable') else 'rejected')
        state.issue(archive_id, exc.code, str(exc))
    except _READ_ERRORS as exc:
        record['inspection_status'] = 'malformed'
        state.issue(archive_id, 'malformed-zip', type(exc).__name__)
    finally:
        record['identity'] = archives._identity(record['identity']['evidence'], record['identity']['candidates'])
        try:
            changed_during_inspection = _identity(fd) != before
        except OSError:
            changed_during_inspection = True
        if changed_during_inspection:
            state.issue(archive_id, 'input-changed', 'container identity changed or became unconfirmed during inspection')
            for changed in state.archives[archive_start:]:
                changed['sha256'] = None
                changed['identity'] = archives._identity([], [])
                changed['inspection_status'] = 'partial'
            for changed in state.entries[entry_start:]:
                changed['sha256'] = None


def inspect_archive_fd(source_fd, *, source_path, sha256, scratch_base, deadline,
                       limits=FileArchiveLimits(), observed_at=None):
    """Inspect immutable captured bytes through an open fd, preserving its offset.

    source_path is a logical locator, not reopened or verified as fd ownership.
    The caller must open/contain the source safely, retain its lifetime, and supply
    an existing private scratch directory outside the inspected checkout. This
    function checks scratch ancestor symlinks but has no checkout argument with
    which to establish containment. It never closes source_fd.
    """
    if not isinstance(limits, FileArchiveLimits):
        raise TypeError('limits must be FileArchiveLimits')
    if type(source_fd) is not int or source_fd < 0 or not hasattr(os, 'pread'):
        raise ValueError('source must be a regular descriptor on a pread platform')
    if not isinstance(source_path, str) or not source_path or '\x00' in source_path:
        raise ValueError('source_path must be a nonempty text locator')
    if not isinstance(sha256, str) or len(sha256) != 64 or any(x not in '0123456789abcdef' for x in sha256):
        raise ValueError('sha256 must be a complete lowercase SHA256')
    if isinstance(deadline, bool) or not isinstance(deadline, (float, int)) or not math.isfinite(deadline):
        raise ValueError('deadline must be a finite absolute monotonic instant')
    if fcntl.fcntl(source_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ValueError('source descriptor must be read-only')
    info = os.fstat(source_fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('source descriptor is not a regular file')
    before = _identity(source_fd)
    scratch_path = os.path.abspath(os.fspath(scratch_base))
    scratch_fd = open_checkout_fd(scratch_path)
    state = _State(limits, deadline, scratch_fd, scratch_path)
    verified = False
    try:
        scratch_info = os.fstat(scratch_fd)
        if scratch_info.st_mode & 0o077 or scratch_info.st_uid != os.getuid():
            raise ValueError('scratch directory must be caller-owned and owner-only')
        try:
            state.check()
            if info.st_size > limits.max_input_bytes:
                state.issue(None, 'input-exceeds-limit', 'captured file exceeds input byte cap')
            else:
                digest = hashlib.sha256()
                offset = 0
                while offset < info.st_size:
                    state.check()
                    chunk = os.pread(source_fd, min(_CHUNK, info.st_size - offset), offset)
                    if not chunk:
                        break
                    digest.update(chunk)
                    offset += len(chunk)
                state.check()
                if offset != info.st_size or _identity(source_fd) != before:
                    state.issue(None, 'input-changed', 'captured file changed during digest verification')
                elif digest.hexdigest() != sha256:
                    state.issue(None, 'input-digest-mismatch', 'captured file does not match expected digest')
                else:
                    verified = True
                    _container(state, source_fd, os.path.basename(source_path), source_path,
                               source_path, [], sha256, info.st_size)
        except _Expired:
            pass
        if _identity(source_fd) != before:
            verified = False
            state.issue(None, 'input-changed', 'source identity changed during traversal')
            for record in state.archives:
                record['inspection_status'] = 'partial'
                record['sha256'] = None
                record['identity'] = archives._identity([], [])
            for entry in state.entries:
                entry['sha256'] = None
        status = ('rejected' if not verified else 'partial' if state.issues or
                  any(a['inspection_status'] != 'complete' for a in state.archives) else 'inspected')
        return {'archives': state.archives, 'entries': state.entries, 'issues': state.issues,
                'coverage': {'status': status, 'input_bytes': info.st_size, 'input_truncated': False,
                    'outer_capture': {'path': source_path, 'complete': verified,
                        'sha256': sha256 if verified else None, 'reason': None if verified else 'input-unverified',
                        'nofollow': 'caller-descriptor', 'ancestors': 'caller-responsibility',
                        'observed_at': observed_at or datetime.now(timezone.utc).isoformat()},
                    'archives_total': len(state.archives),
                    'archives_complete': sum(a['inspection_status'] == 'complete' for a in state.archives),
                    'entries_recorded': len(state.entries),
                    'entries_hashed': sum(e['sha256'] is not None for e in state.entries),
                    'expanded_bytes_used': state.expanded, 'expanded_bytes_limit': limits.max_expanded_bytes,
                    'entries_budget_used': len(state.entries), 'entries_budget_limit': limits.max_entries,
                    'deadline_exceeded': state.deadline_hit, 'stopped_early': state.deadline_hit,
                    'limits': asdict(limits), 'cleanup': {'status': 'retained' if state.leftovers else 'complete',
                                                       'leftovers': state.leftovers}}}
    finally:
        primary = sys.exception()
        try:
            os.close(scratch_fd)
        except OSError:
            if primary is None:
                raise
            primary.add_note('scratch descriptor close failed while unwinding')
