"""Descriptor capture and isolated inspection under one archive-stage deadline.

The caller opens and contains a read-only regular source descriptor. Its locator
is evidence text, never reopened here. Capture preserves the caller's offset;
only a complete unchanged captured spool is passed to the worker. The same
absolute monotonic deadline is used for quota inspection, capture and the worker.
Parent filesystem IO and process creation remain cooperatively bounded, not
hard-interruptible; the worker supplies the hard child-decompression boundary.

Scratch is an existing private base outside the checkout, dedicated to this
pipeline. A bounded two-level inventory accepts only generated spool directory
and file spellings, never follows symlinks, and totals logical file sizes. It
also reserves the maximum possible new outer and nested spool sizes before
capture. Unknown contents, excessive inventory, unavailable accounting or a
quota overrun refuse new work, without deleting anything. Names and modes are
not authentication: this is a trusted-base, sole-writer contract, not a registry
or a concurrency-safe filesystem quota. Concurrent changes remain unconfirmed.

After inspection the parent closes its reader before releasing the captured
spool. Cleanup is allowed only if no worker was invoked or the trusted worker
reported confirmed termination/reaping. Unknown worker state, unknown reader
closure or changed scratch contents retain bounded leftover metadata. No old
scratch is pruned. Interruptions propagate with archive_pipeline_cleanup attached;
secondary cleanup errors never replace the primary exception.

Explicit worker injection is a trusted test seam. The injected callable must
honor the no-descendants contract and report actual lifecycle observations, not
merely assert that cleanup is safe. Inventory in result is the worker's exact
archive result, untouched for candidate adapters; capture and cleanup are
separate qualifications, not new archive identity claims.
"""

import fcntl
import math
import os
import stat
import sys
import time
import uuid

from mdi.archive_files import FileArchiveLimits
from mdi.archive_worker import inspect_isolated
from mdi.discover import open_checkout_fd
from mdi.spool import SpoolError, capture_to_spool


DEFAULT_QUOTA_BYTES = 8 * 1024**3
MAX_QUOTA_ENTRIES = 10000
MAX_LEFTOVERS = 16
MAX_PATH = 4096
_LIMITATIONS = (
    'parent capture and metadata IO are cooperative deadline checks, not hard IO interruption',
    'one absolute stage deadline is shared with the isolated worker and never reset',
    'source_path is a logical locator; descriptor containment is the caller responsibility',
    'scratch quota is a bounded logical-size inventory requiring a dedicated sole-writer base, '
    'not atomic protection against concurrent same-user mutation',
    'cleanup depends on the trusted worker no-descendants contract and observed termination',
)


class ArchivePipelineError(ValueError):
    """Invalid caller options or an unsafe source/scratch boundary."""


class _QuotaRefusal(Exception):
    def __init__(self, code):
        self.code = code


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise _QuotaRefusal('deadline-exceeded')


def _path(value):
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or '\x00' in raw:
            raise ValueError()
        result = os.path.abspath(raw)
        result.encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise ArchivePipelineError('paths must be nonempty UTF-8 text paths') from None
    if len(result) > MAX_PATH:
        raise ArchivePipelineError('path exceeds metadata bound')
    return result


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _directory_identity(info):
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _private_directory(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ArchivePipelineError('scratch base must be a private caller-owned directory')
    return info


def _validate(source_fd, source_path, checkout, scratch_base, deadline, limits,
              quota_bytes, max_quota_entries, worker):
    if not isinstance(limits, FileArchiveLimits):
        raise ArchivePipelineError('limits must be FileArchiveLimits')
    if not _finite(deadline):
        raise ArchivePipelineError('deadline must be a finite absolute monotonic instant')
    if type(quota_bytes) is not int or not 0 < quota_bytes <= DEFAULT_QUOTA_BYTES:
        raise ArchivePipelineError('quota must be positive and cannot exceed 8 GiB')
    if type(max_quota_entries) is not int or not 0 < max_quota_entries <= MAX_QUOTA_ENTRIES:
        raise ArchivePipelineError('quota inventory bound must be within its hard cap')
    if worker is not None and not callable(worker):
        raise ArchivePipelineError('worker must be callable')
    if type(source_fd) is not int or source_fd < 0:
        raise ArchivePipelineError('source must be a read-only regular descriptor')
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode) or fcntl.fcntl(source_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
            raise ArchivePipelineError('source must be a read-only regular descriptor')
    except OSError:
        raise ArchivePipelineError('source descriptor unavailable') from None
    source_path, checkout, scratch_base = map(_path, (source_path, checkout, scratch_base))
    for left, right in ((checkout, scratch_base), (os.path.realpath(checkout), os.path.realpath(scratch_base))):
        left, right = os.sep + left.lstrip(os.sep), os.sep + right.lstrip(os.sep)
        if os.path.commonpath((left, right)) in (left, right):
            raise ArchivePipelineError('scratch base and checkout must not overlap')
    return source_path, checkout, scratch_base


def _generated(name, prefix, suffix=''):
    if not name.startswith(prefix) or (suffix and not name.endswith(suffix)):
        return False
    text = name[len(prefix):len(name) - len(suffix) if suffix else None]
    try:
        return str(uuid.UUID(text)) == text
    except (ValueError, AttributeError):
        return False


class _QuotaHandles:
    """Own quota descriptors; release numeric identities before attempting close."""

    def __init__(self):
        self.root = self.base = self.child = None
        self.unconfirmed = False

    def close(self, *names):
        primary = sys.exception()
        first_error = None
        for name in names:
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as exc:
                    self.unconfirmed = True
                    if first_error is None or isinstance(first_error, Exception):
                        first_error = exc
        if first_error is not None:
            if primary is not None:
                primary.add_note('quota descriptor close unconfirmed')
            elif not isinstance(first_error, Exception):
                raise first_error


def _quota_inventory(checkout, base, deadline, quota_bytes, reserve, max_entries):
    result = {'status': 'unavailable', 'limit_bytes': quota_bytes, 'reserved_bytes': reserve,
              'bytes_observed': 0, 'entries_checked': 0, 'retained_paths': [],
              'paths_truncated': False, 'cleanup_status': 'complete'}
    handles = _QuotaHandles()
    refusal = None
    try:
        _check_deadline(deadline)
        handles.root = open_checkout_fd(checkout)
        handles.base = open_checkout_fd(base)
        base_fd = handles.base
        initial = _private_directory(base_fd)

        def count():
            _check_deadline(deadline)
            result['entries_checked'] += 1
            if result['entries_checked'] > max_entries:
                raise _QuotaRefusal('scratch-quota-unavailable')

        with os.scandir(base_fd) as entries:
            for entry in entries:
                count()
                if not _generated(entry.name, 'mdi-spool-'):
                    raise _QuotaRefusal('scratch-quota-unavailable')
                observed = entry.stat(follow_symlinks=False)
                if not stat.S_ISDIR(observed.st_mode):
                    raise _QuotaRefusal('scratch-quota-unavailable')
                handles.child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                        | getattr(os, 'O_CLOEXEC', 0), dir_fd=base_fd)
                child_fd = handles.child
                try:
                    held = _private_directory(child_fd)
                    if _directory_identity(held) != _directory_identity(observed):
                        raise _QuotaRefusal('scratch-quota-unavailable')
                    path = os.path.join(base, entry.name)
                    if len(result['retained_paths']) < MAX_LEFTOVERS:
                        result['retained_paths'].append(path[:MAX_PATH])
                    else:
                        result['paths_truncated'] = True
                    with os.scandir(child_fd) as files:
                        for item in files:
                            count()
                            if item.name != 'archive.spool' and not _generated(item.name, 'mdi-nested-', '.spool'):
                                raise _QuotaRefusal('scratch-quota-unavailable')
                            before = item.stat(follow_symlinks=False)
                            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                                    or before.st_mode & 0o077):
                                raise _QuotaRefusal('scratch-quota-unavailable')
                            result['bytes_observed'] += before.st_size
                            after = os.stat(item.name, dir_fd=child_fd, follow_symlinks=False)
                            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                                raise _QuotaRefusal('scratch-quota-unavailable')
                            if result['bytes_observed'] + reserve > quota_bytes:
                                raise _QuotaRefusal('scratch-quota-exceeded')
                    if _directory_identity(os.fstat(child_fd)) != _directory_identity(held):
                        raise _QuotaRefusal('scratch-quota-unavailable')
                    after = os.stat(entry.name, dir_fd=base_fd, follow_symlinks=False)
                    if _directory_identity(after) != _directory_identity(held):
                        raise _QuotaRefusal('scratch-quota-unavailable')
                finally:
                    handles.close('child')
                if handles.unconfirmed:
                    raise _QuotaRefusal('scratch-quota-unavailable')
        if _directory_identity(os.fstat(base_fd)) != _directory_identity(initial):
            raise _QuotaRefusal('scratch-quota-unavailable')
        _check_deadline(deadline)
        if result['bytes_observed'] + reserve > quota_bytes:
            raise _QuotaRefusal('scratch-quota-exceeded')
        result['status'] = 'checked'
    except _QuotaRefusal as exc:
        refusal = exc.code
    except (OSError, ValueError):
        refusal = 'scratch-quota-unavailable'
    finally:
        handles.close('child', 'base', 'root')
    if handles.unconfirmed:
        result['status'] = 'unavailable'
        result['cleanup_status'] = 'unconfirmed'
        refusal = 'scratch-quota-unavailable'
    return result, refusal


def _stopped(observation, worker_pid):
    if not isinstance(observation, dict) or observation.get('worker_stopped') is not True:
        return False
    if observation.get('leader_reaped') is True:
        return True
    return observation.get('status') == 'not-started' and worker_pid is None


def _retained(capture, code):
    return {'status': 'retained', 'leftovers': [{'path': capture.scratch_dir,
            'reason': code, 'identity_verified': False}]}


def inspect_archive(source_fd, *, source_path, checkout, scratch_base, deadline,
                    limits=FileArchiveLimits(), quota_bytes=DEFAULT_QUOTA_BYTES,
                    max_quota_entries=MAX_QUOTA_ENTRIES, worker=None):
    """Return result inventory plus separate capture, quota and cleanup evidence.

    outcome is complete only for a finished exchange with inspected coverage and
    confirmed scratch cleanup. Partial capture never starts a worker. Transport
    failures return failed with stable issues; BaseException remains an exception.
    """
    source_path, checkout, scratch_base = _validate(
        source_fd, source_path, checkout, scratch_base, deadline, limits,
        quota_bytes, max_quota_entries, worker)
    # Source size may grow before capture. Reserve the capture's full enforced
    # bound, not a mutable stat observation, plus the nested expansion budget.
    reserve = limits.max_input_bytes + limits.max_expanded_bytes
    quota, refusal = _quota_inventory(checkout, scratch_base, deadline, quota_bytes,
                                      reserve, max_quota_entries)
    response = {'outcome': 'failed', 'result': None, 'capture': None,
                'worker_cleanup': None, 'cleanup_result': {'status': 'not-created', 'leftovers': []},
                'quota': quota, 'issues': [], 'limitations': list(_LIMITATIONS)}
    if refusal:
        response['issues'].append({'code': refusal})
        return response
    capture = None
    reader = None
    worker_invoked = False
    stopped = True
    primary = None
    cleanup = response['cleanup_result']
    try:
        capture = capture_to_spool(source_fd, source_path=source_path, checkout=checkout,
                                   scratch_base=scratch_base, max_bytes=limits.max_input_bytes,
                                   deadline=deadline)
        response['capture'] = capture.as_dict()
        if not capture.complete:
            response['outcome'] = 'partial'
            response['issues'].extend({'code': code} for code in capture.reasons)
        else:
            reader = capture.open_reader()
            # Mark uncertainty before entering the callable: an exception can
            # arrive after it spawned a child but before it returned metadata.
            worker_invoked, stopped = True, False
            exchange = (inspect_isolated if worker is None else worker)(
                reader, source_path=source_path, sha256=capture.sha256,
                scratch_base=capture.scratch_dir, deadline=deadline, limits=limits)
            if not isinstance(exchange, dict):
                raise ValueError('invalid worker exchange')
            observation = exchange.get('cleanup_result')
            stopped = _stopped(observation, exchange.get('worker_pid'))
            response['worker_cleanup'] = observation if isinstance(observation, dict) else None
            if not stopped:
                response['issues'].append({'code': 'worker-cleanup-unconfirmed'})
            elif exchange.get('outcome') != 'complete' or not isinstance(exchange.get('result'), dict):
                code = exchange.get('error')
                response['issues'].append({'code': code if isinstance(code, str) and
                    code in ('deadline-exceeded', 'memory-exhausted', 'response-too-large',
                             'worker-failed', 'worker-cleanup-unconfirmed', 'invalid-response')
                    else 'worker-failed'})
            else:
                response['result'] = exchange['result']
                response['outcome'] = ('complete' if exchange['result'].get('coverage', {}).get('status')
                                       == 'inspected' else 'partial')
    except BaseException as exc:
        if worker_invoked:
            observation = getattr(exc, 'archive_cleanup', None)
            stopped = _stopped(observation, object())
            response['worker_cleanup'] = observation if isinstance(observation, dict) else None
        if isinstance(exc, Exception):
            code = exc.code if isinstance(exc, SpoolError) else 'archive-pipeline-failed'
            response['issues'].append({'code': code})
            if isinstance(exc, SpoolError) and exc.cleanup is not None:
                cleanup = exc.cleanup.as_dict()
        else:
            if capture is None:
                cleanup = {'status': 'unconfirmed', 'leftovers': [
                    {'path': scratch_base, 'reason': 'capture-interrupted',
                     'identity_verified': False}]}
            primary = exc
            raise
    finally:
        if reader is not None:
            fd, reader = reader, None
            try:
                os.close(fd)
            except BaseException as exc:
                stopped = False
                response['issues'].append({'code': 'reader-close-unconfirmed'})
                if primary is not None:
                    primary.add_note('archive reader close unconfirmed')
                elif not isinstance(exc, Exception):
                    primary = exc
        if capture is not None:
            try:
                cleanup = capture.cleanup(worker_stopped=stopped).as_dict()
            except BaseException as exc:
                cleanup = _retained(capture, 'cleanup-interrupted')
                if primary is not None:
                    primary.add_note('archive scratch cleanup interrupted')
                elif not isinstance(exc, Exception):
                    primary = exc
        response['cleanup_result'] = cleanup
        if primary is not None:
            primary.archive_pipeline_cleanup = cleanup
            if sys.exception() is not primary:
                raise primary
    if cleanup['status'] not in ('complete', 'not-created'):
        response['issues'].append({'code': 'scratch-retained'})
        if response['outcome'] == 'complete':
            response['outcome'] = 'partial'
    if time.monotonic() >= deadline:
        response['issues'].append({'code': 'deadline-exceeded'})
        if response['outcome'] == 'complete':
            response['outcome'] = 'partial'
    return response
