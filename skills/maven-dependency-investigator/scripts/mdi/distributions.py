"""Bounded local tar/tar.gz inventory, with Java ZIP members inspected from disk.

This module never extracts archive paths, follows member links, invokes project
code, or opens a source locator. The caller owns a read-only regular descriptor;
pread supplies an independent cursor. A source mutation invalidates all derived
member hashes and Java inventories. This is detection, not a same-user sandbox.

TarInfo.frombuf parses one fixed 512-byte header. We deliberately do not use
TarFile.next: its extension processors can read/allocate PAX or GNU long-name
payloads before application limits run. PAX, GNU long-name/long-link and sparse
extensions stop inspection explicitly; they are not silently interpreted as
ordinary entries. Nonregular members with nonzero sizes stop inspection before
any payload drain, since their framing cannot be treated as regular-file data.
USTAR prefix names are supported within the text bound.

Plain/gzip streams are read only in bounded chunks; all expanded framing,
padding and skipped member bytes count, as do bytes expanded inside Java ZIPs.
GzipFile.read(size) uses a bounded decompressor output request (verified against
local CPython 3.14.7 source). Deadline checks are cooperative between kernel and
native decompressor calls, NOT a hard process boundary. Run inside a controlled
worker before treating this as hard deadline isolation.

Java members use generated 0600 scratch files under an existing private,
caller-owned nofollow directory outside the checkout. The file-backed ZIP reader
is reused with remaining global budgets. Only unchanged owned files are removed;
uncertain scratch is retained. There is no automatic pruning or quota registry.
The inventory retention budget applies to member and Java inspection records,
not the final report envelope; it is checked incrementally before retention.
The ZIP reader still constructs one bounded inventory before that check. A
supervising worker must apply its memory and serialized-response limits too.
OCI image layering, tar sparse reconstruction and other compression are outside
this slice. The explicit oci_whiteouts option recognizes only safe, zero-length
regular OCI whiteout markers before archive-suffix classification; it never
applies deletions. Ordinary tar inspection leaves that option disabled and
handles those names as ordinary entries. Application/container attribution
remains unknown.
"""
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
import stat
import sys
import tarfile
import time
import uuid
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from mdi.archive_files import FileArchiveLimits, inspect_archive_fd
from mdi.discover import open_checkout_fd

_CHUNK = 65536
_BLOCK = 512
_MAX_NESTED_LEFTOVERS = 256
_MAX_CLEANUP_TEXT = 8192
_EXTENSIONS = (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
               tarfile.GNUTYPE_LONGLINK, tarfile.GNUTYPE_SPARSE)


@dataclass(frozen=True)
class DistributionLimits:
    max_input_bytes: int = 2 * 1024**3
    max_expanded_bytes: int = 2 * 1024**3
    max_member_bytes: int = 256 * 1024**2
    max_members: int = 20000
    max_archives: int = 32
    max_zip_entries: int = 20000
    max_depth: int = 4
    max_metadata_bytes: int = 4 * 1024**2
    max_text: int = 4096
    max_inventory_bytes: int = 24 * 1024**2

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError('distribution limits must be positive integers')
        if self.max_depth > 4:
            raise ValueError('nested ZIP depth cannot exceed four')


class DistributionError(ValueError):
    """Invalid invocation or an unsafe descriptor/scratch boundary."""


class _Stop(Exception):
    def __init__(self, code):
        self.code = code


def _identity(fd):
    s = os.fstat(fd)
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _close(fd):
    primary = sys.exception()
    try:
        os.close(fd)
    except BaseException:
        if primary is None:
            raise
        primary.add_note('distribution descriptor closure unconfirmed')


class _Source(io.RawIOBase):
    def __init__(self, fd, size, check):
        self.fd, self.size, self.check, self.offset = fd, size, check, 0
        super().__init__()

    def readable(self):
        return True

    def read(self, size=-1):
        self.check()
        if size < 0:
            raise ValueError('unbounded distribution source read refused')
        size = min(size, _CHUNK, max(0, self.size - self.offset))
        data = os.pread(self.fd, size, self.offset)
        self.offset += len(data)
        return data


class _Budget:
    def __init__(self, limits, deadline):
        self.limits, self.deadline = limits, deadline
        self.expanded = 0
        self.zip_entries = 0
        self.result_bytes = 0
        self.cleanup_paths_complete = True

    def retain(self, value):
        # Reserve fixed scalar growth (hash and final byte count) for a member
        # recorded before its body is read. Nested inventories are already final.
        self.result_bytes += 128
        for token in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(value):
            self.check()
            self.result_bytes += len(token)
            if self.result_bytes > self.limits.max_inventory_bytes:
                raise _Stop('result-limit')

    def check(self):
        if time.monotonic() >= self.deadline:
            raise _Stop('deadline-exceeded')

    def read(self, stream, size):
        self.check()
        if self.expanded > self.limits.max_expanded_bytes:
            raise _Stop('expanded-limit')
        allowed = min(size, _CHUNK, max(0, self.limits.max_expanded_bytes - self.expanded) + 1)
        data = stream.read(allowed)
        self.expanded += len(data)
        if self.expanded > self.limits.max_expanded_bytes:
            raise _Stop('expanded-limit')
        self.check()
        return data

    def exact(self, stream, size):
        parts = []
        while size:
            chunk = self.read(stream, size)
            if not chunk:
                raise _Stop('truncated-tar')
            parts.append(chunk)
            size -= len(chunk)
        return b''.join(parts)


def _name_ok(name, bound):
    return (isinstance(name, str) and bool(name) and len(name) <= bound
            and not name.startswith('/') and '\\' not in name
            and all(ord(c) >= 32 and ord(c) != 127 for c in name)
            and not any(p in ('', '.', '..') for p in name.split('/'))
            and not (len(name) >= 2 and name[1] == ':'))


class _MemberSpool:
    def __init__(self, base_fd, scratch, leftovers):
        self.base_fd, self.scratch = base_fd, scratch
        self.name = 'mdi-distribution-' + str(uuid.uuid4()) + '.spool'
        self.fd = None
        self.identity = None
        self.fd = os.open(self.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=base_fd)
        try:
            self.identity = _identity(self.fd)
        except BaseException:
            leftovers.append({'path': os.path.join(scratch, self.name),
                              'reason': 'distribution-spool-identity-unconfirmed'})
            fd, self.fd = self.fd, None
            _close(fd)
            raise

    def write(self, data, check):
        primary = None
        try:
            view = memoryview(data)
            while view:
                check()
                n = os.write(self.fd, view)
                if n <= 0 or n > len(view):
                    raise OSError('invalid spool write progress')
                view = view[n:]
        finally:
            primary = sys.exception()
            try:
                self.identity = _identity(self.fd)
            except BaseException:
                self.identity = None
                if primary is None:
                    raise
                primary.add_note('distribution spool identity unconfirmed')

    def reader(self):
        fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.base_fd)
        try:
            if self.identity is None or _identity(fd) != self.identity or _identity(self.fd) != self.identity:
                raise OSError('distribution spool changed')
            return fd
        except BaseException:
            _close(fd)
            raise

    def cleanup(self, leftovers):
        primary = sys.exception()
        removed = False
        try:
            s = os.stat(self.name, dir_fd=self.base_fd, follow_symlinks=False)
            named = s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
            if self.identity is not None and stat.S_ISREG(s.st_mode) and named == self.identity == _identity(self.fd):
                os.unlink(self.name, dir_fd=self.base_fd)
                removed = True
        except BaseException as exc:
            if primary is not None:
                primary.add_note('distribution spool cleanup interrupted')
            elif not isinstance(exc, OSError):
                raise
        finally:
            if not removed:
                leftovers.append({'path': os.path.join(self.scratch, self.name),
                                  'reason': 'distribution-spool-unconfirmed'})
            active = sys.exception() or primary
            fd, self.fd = self.fd, None
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as exc:
                    if active is not None:
                        active.add_note('distribution spool close unconfirmed')
                    elif not isinstance(exc, OSError):
                        raise
                    else:
                        leftovers.append({'path': os.path.join(self.scratch, self.name),
                                          'reason': 'distribution-spool-close-unconfirmed'})


def _retain_nested_cleanup(cleanup, leftovers, scratch):
    """Retain cleanup duties independently of the observation retention budget.

    The reader's ledger is bounded separately before copying. An unavailable or
    oversized ledger retains the scratch-directory obligation instead of losing
    knowledge that files may remain. Known entries are retained up to the bound,
    with a final root marker on saturation. Returns whether individual paths are
    complete; that flag must survive worker and OCI boundaries. No cleanup
    decision is made from this copy.
    """
    unknown = {'path': scratch, 'reason': 'nested-cleanup-ledger-unconfirmed'}

    def retain_unknown():
        if unknown not in leftovers:
            leftovers.append(unknown)

    if not isinstance(cleanup, dict) or not isinstance(cleanup.get('leftovers'), list):
        retain_unknown()
        return False
    records = cleanup['leftovers']
    complete = cleanup.get('individual_paths_complete', True) is True
    for index, record in enumerate(records):
        if index >= _MAX_NESTED_LEFTOVERS - 1 or len(leftovers) >= _MAX_NESTED_LEFTOVERS - 1:
            complete = False
            break
        if not isinstance(record, dict) or any(
                not isinstance(record.get(key), str) or not record[key]
                or len(record[key]) > _MAX_CLEANUP_TEXT
                for key in ('path', 'reason')):
            retain_unknown()
            complete = False
            continue
        copied = {'path': record['path'], 'reason': record['reason']}
        if copied not in leftovers:
            leftovers.append(copied)
    if not complete:
        retain_unknown()
    return complete


def _oci_whiteout(info, max_text):
    """Validate an OCI marker before any suffix-based archive inspection."""
    if (not _name_ok(info.name, max_text)
            or info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or info.size != 0):
        raise _Stop('invalid-oci-whiteout')
    parent, _, basename = info.name.rpartition('/')
    if basename == '.wh..wh..opq':
        return {'kind': 'opaque', 'target': parent or '.'}
    target = basename[len('.wh.'):]
    if not target or target in ('.', '..') or target.startswith('.wh.'):
        raise _Stop('invalid-oci-whiteout')
    return {'kind': 'remove', 'target': parent + '/' + target if parent else target}


def _inventory(stream, state, base_fd, scratch, source_path, members, archives, issues, leftovers,
               oci_whiteouts):
    header_bytes = 0
    while True:
        header = state.exact(stream, _BLOCK)
        if header == bytes(_BLOCK):
            if state.exact(stream, _BLOCK) != bytes(_BLOCK):
                raise _Stop('invalid-tar-terminator')
            # Consume gzip trailers and concatenated output; nonzero trailing
            # data is not silently treated as another distribution.
            while True:
                tail = state.read(stream, _CHUNK)
                if not tail:
                    return
                if any(tail):
                    raise _Stop('trailing-distribution-data')
        if len(members) >= state.limits.max_members:
            raise _Stop('member-limit')
        header_bytes += _BLOCK
        if header_bytes > state.limits.max_metadata_bytes:
            raise _Stop('metadata-limit')
        info = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
        if info.type in _EXTENSIONS:
            raise _Stop('unsupported-tar-extension')
        if info.size < 0:
            raise _Stop('invalid-member-size')
        # Nonregular tar types do not share regular-file payload semantics.
        # Never let a forged size consume the following member's header.
        if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) and info.size:
            raise _Stop('nonregular-member-size')
        status = ('name-rejected' if not _name_ok(info.name, state.limits.max_text)
                  else 'unsupported-member-type' if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                  else 'member-too-large' if info.size > state.limits.max_member_bytes else 'recorded')
        whiteout = None
        if oci_whiteouts and info.name.rsplit('/', 1)[-1].startswith('.wh.'):
            try:
                whiteout = _oci_whiteout(info, state.limits.max_text)
            except _Stop as exc:
                status = exc.code
        member = {'index': len(members), 'name': info.name[:state.limits.max_text],
                  'name_truncated': len(info.name) > state.limits.max_text,
                  'type': info.type.hex(), 'size_declared': info.size, 'size_actual': 0,
                  'sha256': None, 'status': status, 'role': 'unknown'}
        if whiteout is not None:
            member['whiteout'] = whiteout
        state.retain(member)
        members.append(member)
        if status != 'recorded':
            issues.append({'code': status, 'member_index': member['index']})
        is_java = (whiteout is None and status == 'recorded' and info.isfile()
                   and info.name.lower().endswith(('.jar', '.war', '.ear', '.zip')))
        if is_java and len(archives) >= state.limits.max_archives:
            raise _Stop('archive-limit')
        spool = None
        try:
            if is_java:
                spool = _MemberSpool(base_fd, scratch, leftovers)
            digest = hashlib.sha256()
            remaining = info.size
            while remaining:
                chunk = state.read(stream, remaining)
                if not chunk:
                    raise _Stop('truncated-tar')
                remaining -= len(chunk)
                member['size_actual'] += len(chunk)
                digest.update(chunk)
                if spool is not None:
                    spool.write(chunk, state.check)
            member['sha256'] = digest.hexdigest()
            state.exact(stream, (-info.size) % _BLOCK)
            if spool is not None:
                left = state.limits.max_expanded_bytes - state.expanded
                entries_left = state.limits.max_zip_entries - state.zip_entries
                if left <= 0 or entries_left <= 0:
                    raise _Stop('expanded-limit' if left <= 0 else 'zip-entry-limit')
                limits = FileArchiveLimits(max_input_bytes=state.limits.max_member_bytes,
                    max_entry_bytes=state.limits.max_member_bytes, max_expanded_bytes=left,
                    max_entries=entries_left, max_depth=state.limits.max_depth,
                    max_metadata_bytes=state.limits.max_metadata_bytes,
                    max_metadata_entry_bytes=min(1024**2, state.limits.max_metadata_bytes))
                fd = spool.reader()
                try:
                    try:
                        result = inspect_archive_fd(fd, source_path=source_path+'!'+info.name,
                            sha256=member['sha256'], scratch_base=scratch, deadline=state.deadline, limits=limits)
                    except BaseException as exc:
                        complete = _retain_nested_cleanup(getattr(exc, 'archive_cleanup', None), leftovers, scratch)
                        state.cleanup_paths_complete &= complete
                        raise
                    # Record duties before closing the reader or inspecting any
                    # ordinary result fields that could fail or exceed a budget.
                    coverage = result.get('coverage') if isinstance(result, dict) else None
                    cleanup = coverage.get('cleanup') if isinstance(coverage, dict) else None
                    complete = _retain_nested_cleanup(cleanup, leftovers, scratch)
                    state.cleanup_paths_complete &= complete
                finally:
                    _close(fd)
                state.expanded += result['coverage']['expanded_bytes_used']
                state.zip_entries += len(result['entries'])
                observation = {'member_index': member['index'], 'member_name': info.name,
                               'inspection': result, 'role': 'unknown'}
                state.retain(observation)
                archives.append(observation)
                if result['coverage']['status'] != 'inspected':
                    issues.append({'code': 'java-archive-partial', 'member_index': member['index']})
        finally:
            if member['size_actual'] != member['size_declared']:
                member['status'] = 'incomplete'
                member['sha256'] = None
            if spool is not None:
                spool.cleanup(leftovers)


def inspect_distribution_fd(source_fd, *, source_path, checkout, scratch_base,
                            format, deadline, limits=DistributionLimits(), oci_whiteouts=False):
    """Inventory one caller-owned regular descriptor; no path extraction or I/O lookup.

    Only tar/tar.gz are supported. oci_whiteouts=True records validated OCI
    markers without opening them as archives; no filesystem changes are applied.
    The default treats names such as .wh.library.jar as ordinary tar files.
    Scratch lifetime and single-writer ownership
    are caller responsibilities. Parent and nested decompression are cooperative;
    a supervising worker is required for hard cancellation.
    """
    if type(oci_whiteouts) is not bool:
        raise DistributionError('oci_whiteouts must be bool')
    if format not in ('tar', 'tar.gz') or not isinstance(limits, DistributionLimits):
        raise DistributionError('expected tar or tar.gz and DistributionLimits')
    try:
        finite_deadline = type(deadline) in (int, float) and math.isfinite(deadline)
    except OverflowError:
        finite_deadline = False
    if not finite_deadline:
        raise DistributionError('deadline must be finite')
    if type(source_fd) is not int or source_fd < 0 or not hasattr(os, 'pread'):
        raise DistributionError('read-only regular descriptor required')
    if not isinstance(source_path, str) or not source_path or len(source_path) > limits.max_text or '\x00' in source_path:
        raise DistributionError('bounded text source locator required')
    try:
        source_path.encode('utf-8')
    except UnicodeError:
        raise DistributionError('source locator must be UTF-8 text') from None
    if fcntl.fcntl(source_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise DistributionError('source descriptor must be read-only')
    initial = os.fstat(source_fd)
    if not stat.S_ISREG(initial.st_mode):
        raise DistributionError('source descriptor must be regular')
    before = (initial.st_dev, initial.st_ino, initial.st_size,
              initial.st_mtime_ns, initial.st_ctime_ns)
    root, scratch = os.path.abspath(os.fspath(checkout)), os.path.abspath(os.fspath(scratch_base))
    for a, b in ((root, scratch), (os.path.realpath(root), os.path.realpath(scratch))):
        if os.path.commonpath((a, b)) in (a, b):
            raise DistributionError('scratch and checkout must not overlap')
    base_fd = open_checkout_fd(scratch)
    members, archives, issues, leftovers = [], [], [], []
    state = _Budget(limits, deadline)
    source_digest = None
    raw = compressed = None
    try:
        s = os.fstat(base_fd)
        if s.st_uid != os.getuid() or s.st_mode & 0o077:
            raise DistributionError('scratch must be private and caller-owned')
        try:
            state.check()
            if initial.st_size > limits.max_input_bytes:
                raise _Stop('input-limit')
            digest, offset = hashlib.sha256(), 0
            while offset < initial.st_size:
                state.check()
                part = os.pread(source_fd, min(_CHUNK, initial.st_size-offset), offset)
                if not part:
                    raise _Stop('source-changed')
                digest.update(part)
                offset += len(part)
            if _identity(source_fd) != before:
                raise _Stop('source-changed')
            source_digest = digest.hexdigest()
            raw = _Source(source_fd, initial.st_size, state.check)
            compressed = gzip.GzipFile(fileobj=raw, mode='rb') if format == 'tar.gz' else None
            _inventory(compressed or raw, state, base_fd, scratch, source_path,
                       members, archives, issues, leftovers, oci_whiteouts)
        except _Stop as exc:
            issues.append({'code': exc.code})
        except (OSError, EOFError, tarfile.HeaderError, UnicodeError, zlib.error) as exc:
            issues.append({'code': 'invalid-distribution', 'error_type': type(exc).__name__})
        if _identity(source_fd) != before:
            source_digest = None
            archives.clear()
            for member in members:
                member['sha256'] = None
                member['status'] = 'source-changed'
            issues.append({'code': 'source-changed'})
        if leftovers:
            issues.append({'code': 'scratch-retained'})
        return {'source': {'path': source_path, 'sha256': source_digest, 'size': initial.st_size,
                           'format': format, 'observed_at': datetime.now(timezone.utc).isoformat()},
                'members': members, 'archives': archives, 'issues': issues,
                'coverage': {'status': 'partial' if issues else 'complete',
                    'oci_whiteouts': oci_whiteouts, 'layer_changes_applied': False,
                    'expanded_bytes': state.expanded, 'zip_entries': state.zip_entries,
                    'limits': asdict(limits), 'cleanup': {'leftovers': leftovers,
                        'individual_paths_complete': state.cleanup_paths_complete},
                    'limitations': ['tar headers parsed without PAX/GNU long-name/sparse extensions',
                        'deadlines are cooperative, including gzip and nested ZIP decompression',
                        'distribution member role and recorded component metadata are candidates only',
                        'source identity rechecks detect changes, not an atomic filesystem snapshot']}}
    except BaseException as exc:
        if leftovers:
            exc.distribution_cleanup = {'leftovers': list(leftovers),
                                        'individual_paths_complete': state.cleanup_paths_complete}
        raise
    finally:
        primary = sys.exception()
        for closer in ([compressed.close] if compressed is not None else []) + ([raw.close] if raw is not None else []) + [lambda: os.close(base_fd)]:
            try:
                closer()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note('distribution resource closure unconfirmed')
        if primary is not None and sys.exception() is None:
            raise primary
