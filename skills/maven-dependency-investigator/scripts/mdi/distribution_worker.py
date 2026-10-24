"""A one-shot, bounded process boundary for local tar and tar.gz inspection.

The caller supplies a read-only regular descriptor, an expected SHA256, an
existing checkout and a private scratch directory outside it. No source locator
is reopened. The child uses -B -I -S, never spawns descendants and never executes
member contents. OCI layouts use the separate oci_worker process boundary.

The absolute stage deadline covers startup and the parent's pipe exchange; it
is never reset. Native child decompression is interruptible by terminating the
owned process group. Parent filesystem operations, process creation and JSON
validation are not themselves hard-interruptible; their overrun is checked when
control returns. Cleanup has a separate two-second reap grace.

Process control reuses the reviewed archive_worker primitives through a narrow
private bridge. Default SIGCHLD and sole reaping ownership are required. Signals
precede reaping; lost ownership suppresses signals. Concurrent external reapers
are unsupported, and check-then-signal is not atomic against them. Permission to
release scratch rests on the trusted worker's no-descendants contract and actual
termination, not on a caller's approval or a successful-looking JSON response.

The supervisor never deletes caller scratch. Results and exceptions retain a
separate bounded cleanup ledger, even when identity evidence is rejected. If a
worker dies, overruns output, or cannot provide a valid ledger, the scratch root
is a recovery obligation and individual_paths_complete is false. That marker is
not authorization to recursively delete the directory. The caller must preserve
scratch until it can verify each cleanup target and worker termination.

Explicit worker_command is a trusted test seam. Only the default command runs
the shipped reader and resource guard. Protocol validation binds structural
identity and budgets; it is not authentication of an arbitrary supplied worker.
Raw worker exception messages and stderr never cross the boundary.
"""

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

if __name__ == '__main__' and __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mdi import archive_worker as boundary
from mdi.archive_files import FileArchiveLimits
from mdi.discover import open_checkout_fd
from mdi.distributions import DistributionLimits, inspect_distribution_fd

# These primitives deliberately keep the same process ownership, protocol and
# resource contracts as the ZIP worker; no generic process framework is added.
from mdi.archive_worker import (
    _BoundaryFailure, _cleanup, _digest, _exchange, _finite, _json,
    _platform, _scratch_info, _set_memory_limit, _source_info, _text,
)

SCHEMA = 'mdi.distribution-worker/1'
MAX_RESPONSE_BYTES = boundary.MAX_RESPONSE_BYTES
MAX_MEMORY_BYTES = boundary.MAX_MEMORY_BYTES
MAX_REQUEST_BYTES = boundary.MAX_REQUEST_BYTES
MAX_CLEANUP_PATHS = 32
_SCRIPT = Path(__file__).resolve()
_ERRORS = frozenset(('invalid-request', 'inspection-failed', 'memory-limit-unavailable',
    'memory-exhausted', 'deadline-exceeded', 'response-too-large',
    'scratch-identity-changed', 'checkout-identity-changed', 'source-changed'))
TOOL_IDENTITY = {'name': 'mdi.distribution_worker', 'version': '1',
                 'source_sha256': hashlib.sha256(_SCRIPT.read_bytes()).hexdigest(),
                 'process_boundary': boundary.TOOL_IDENTITY}


class DistributionWorkerError(ValueError):
    """Invalid invocation or unavailable process/memory safety primitives."""


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _recovery(scratch, reason):
    return {'leftovers': [{'path': scratch, 'reason': reason}],
            'individual_paths_complete': False}


def _add_recovery(ledger, scratch, reason):
    rows = list(ledger['leftovers'][:MAX_CLEANUP_PATHS - 1])
    rows.append({'path': scratch, 'reason': reason})
    return {'leftovers': rows, 'individual_paths_complete': False}


def _ledger(value, scratch):
    """Normalize a trusted-reader ledger before it can outlive its evidence."""
    if not isinstance(value, dict) or not isinstance(value.get('leftovers'), list):
        return _recovery(scratch, 'cleanup-details-unavailable')
    rows = value['leftovers']
    complete = value.get('individual_paths_complete', True)
    if type(complete) is not bool or len(rows) >= MAX_CLEANUP_PATHS:
        return _recovery(scratch, 'cleanup-details-incomplete')
    result = []
    for row in rows:
        if not isinstance(row, dict) or not _text(row.get('path')) or not _text(row.get('reason')):
            return _recovery(scratch, 'cleanup-details-invalid')
        path = row['path']
        if not os.path.isabs(path) or os.path.commonpath((scratch, os.path.normpath(path))) != scratch:
            return _recovery(scratch, 'cleanup-details-invalid')
        result.append({'path': path, 'reason': row['reason']})
    if not complete:
        result.append({'path': scratch, 'reason': 'cleanup-details-incomplete'})
    return {'leftovers': result, 'individual_paths_complete': complete}


def _validate_ledger(value, scratch):
    if not isinstance(value, dict) or set(value) != {'leftovers', 'individual_paths_complete'}:
        raise ValueError('invalid cleanup ledger')
    if type(value['individual_paths_complete']) is not bool or not isinstance(value['leftovers'], list):
        raise ValueError('invalid cleanup ledger fields')
    if len(value['leftovers']) > MAX_CLEANUP_PATHS:
        raise ValueError('cleanup ledger limit')
    for row in value['leftovers']:
        if not isinstance(row, dict) or set(row) != {'path', 'reason'} or not all(_text(row[k]) for k in row):
            raise ValueError('invalid cleanup entry')
        path = row['path']
        if not os.path.isabs(path) or os.path.normpath(path) != path or os.path.commonpath((scratch, path)) != scratch:
            raise ValueError('cleanup entry outside scratch')
    if not value['individual_paths_complete'] and not any(r['path'] == scratch for r in value['leftovers']):
        raise ValueError('missing recovery root')


def _structure(value):
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 2_000_000 or depth > 48:
            raise ValueError('result structure limit')
        if isinstance(item, dict):
            if len(item) > 2_000_000 - count or any(not isinstance(k, str) or len(k) > 65535 for k in item):
                raise ValueError('invalid result object')
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > 2_000_000 - count:
                raise ValueError('result list limit')
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > 65535:
                raise ValueError('result text limit')
            item.encode('utf-8')
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError('invalid result scalar')


def _validate_result(result, limits, path, digest, size, format, cleanup, scratch, *,
                     oci_whiteouts=False):
    _structure(result)
    if not isinstance(result, dict) or set(result) != {'source', 'members', 'archives', 'issues', 'coverage'}:
        raise ValueError('invalid inventory')
    source, coverage = result['source'], result['coverage']
    if not isinstance(source, dict) or set(source) != {'path', 'sha256', 'size', 'format', 'observed_at'}:
        raise ValueError('invalid source')
    if source['path'] != path or type(source['size']) is not int or source['size'] != size or source['format'] != format:
        raise ValueError('source binding mismatch')
    if source['sha256'] not in (None, digest) or not _text(source['observed_at']):
        raise ValueError('source digest mismatch')
    if not isinstance(coverage, dict) or coverage.get('status') not in ('complete', 'partial'):
        raise ValueError('invalid coverage')
    if coverage.get('limits') != asdict(limits) or any(type(v) is not int for v in coverage['limits'].values()):
        raise ValueError('limit mismatch')
    for key, maximum in (('expanded_bytes', limits.max_expanded_bytes + 1), ('zip_entries', limits.max_zip_entries)):
        if type(coverage.get(key)) is not int or not 0 <= coverage[key] <= maximum:
            raise ValueError('invalid coverage budget')
    members, archives, issues = result['members'], result['archives'], result['issues']
    if not all(isinstance(x, list) for x in (members, archives, issues)):
        raise ValueError('invalid inventory lists')
    if len(members) > limits.max_members or len(archives) > limits.max_archives or len(issues) > 4 * (limits.max_members + limits.max_archives + 1):
        raise ValueError('inventory count limit')
    if any(not isinstance(i, dict) or not _text(i.get('code')) for i in issues):
        raise ValueError('invalid issue')
    if not isinstance(coverage.get('cleanup'), dict) or _ledger(coverage['cleanup'], scratch) != cleanup:
        raise ValueError('cleanup ledger mismatch')
    if coverage.get('oci_whiteouts') is not oci_whiteouts or coverage.get('layer_changes_applied') is not False:
        raise ValueError('whiteout context mismatch')
    minimum_expanded = 512 * len(members)
    for index, member in enumerate(members):
        fields = {'index', 'name', 'name_truncated', 'type', 'size_declared', 'size_actual', 'sha256', 'status', 'role'}
        if oci_whiteouts and isinstance(member, dict) and 'whiteout' in member:
            fields.add('whiteout')
        if not isinstance(member, dict) or set(member) != fields:
            raise ValueError('invalid member')
        if type(member['index']) is not int or member['index'] != index or member['role'] != 'unknown':
            raise ValueError('invalid member identity')
        if not isinstance(member['name'], str) or len(member['name']) > limits.max_text or type(member['name_truncated']) is not bool:
            raise ValueError('invalid member name')
        if not isinstance(member['type'], str) or len(member['type']) != 2 or any(c not in '0123456789abcdef' for c in member['type']):
            raise ValueError('invalid member type')
        declared, actual = member['size_declared'], member['size_actual']
        if type(declared) is not int or type(actual) is not int or not 0 <= actual <= min(declared, limits.max_expanded_bytes):
            raise ValueError('invalid member size')
        if member['type'] not in ('00', '30') and declared != 0:
            raise ValueError('nonregular member has a payload')
        if member['status'] == 'recorded' and declared > limits.max_member_bytes:
            raise ValueError('recorded member exceeds size bound')
        statuses = ('recorded', 'name-rejected', 'unsupported-member-type', 'member-too-large', 'incomplete', 'source-changed')
        if oci_whiteouts:
            statuses += ('invalid-oci-whiteout',)
        if member['status'] not in statuses:
            raise ValueError('invalid member status')
        if 'whiteout' in member:
            name = member['name']
            parent, _, basename = name.rpartition('/')
            parts = name.split('/')
            if (member['type'] not in ('00', '30') or declared != 0 or actual != 0
                    or member['name_truncated'] or name.startswith('/') or '\\' in name
                    or any(part in ('', '.', '..') for part in parts)
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                    or not basename.startswith('.wh.')
                    or member['status'] not in ('recorded', 'source-changed')):
                raise ValueError('invalid whiteout member')
            target = basename[4:]
            if basename == '.wh..wh..opq':
                expected = {'kind': 'opaque', 'target': parent or '.'}
            elif target and target not in ('.', '..') and not target.startswith('.wh.'):
                expected = {'kind': 'remove', 'target': parent + '/' + target if parent else target}
            else:
                raise ValueError('invalid whiteout target')
            if member['whiteout'] != expected:
                raise ValueError('invalid whiteout metadata')
            if member['status'] == 'recorded' and member['sha256'] != hashlib.sha256(b'').hexdigest():
                raise ValueError('invalid whiteout digest')
        elif oci_whiteouts and member['name'].rsplit('/', 1)[-1].startswith('.wh.') and member['status'] == 'recorded':
            raise ValueError('missing whiteout metadata')
        if member['sha256'] is not None and (not _digest(member['sha256']) or actual != declared):
            raise ValueError('invalid member digest')
        if source['sha256'] is None and member['sha256'] is not None:
            raise ValueError('changed source retains member digest')
        minimum_expanded += actual
    seen = set()
    zip_entries = 0
    for observation in archives:
        if not isinstance(observation, dict) or set(observation) != {'member_index', 'member_name', 'inspection', 'role'}:
            raise ValueError('invalid archive observation')
        index = observation['member_index']
        if type(index) is not int or not 0 <= index < len(members) or index in seen:
            raise ValueError('invalid archive member reference')
        seen.add(index)
        member = members[index]
        if observation['role'] != 'unknown' or observation['member_name'] != member['name'] or member['sha256'] is None:
            raise ValueError('invalid archive identity')
        if member['type'] not in ('30', '00') or member['status'] != 'recorded' or member['size_actual'] > limits.max_member_bytes:
            raise ValueError('invalid Java member')
        inspected = observation['inspection']
        used_limits = inspected.get('coverage', {}).get('limits') if isinstance(inspected, dict) else None
        if not isinstance(used_limits, dict):
            raise ValueError('missing nested limits')
        nested_limits = FileArchiveLimits(**used_limits)
        expected_limits = dict(max_input_bytes=limits.max_member_bytes, max_entry_bytes=limits.max_member_bytes,
            max_depth=limits.max_depth, max_metadata_bytes=limits.max_metadata_bytes,
            max_metadata_entry_bytes=min(1024**2, limits.max_metadata_bytes))
        if any(used_limits[k] != v for k, v in expected_limits.items()) or not 0 < nested_limits.max_expanded_bytes <= limits.max_expanded_bytes or not 0 < nested_limits.max_entries <= limits.max_zip_entries - zip_entries:
            raise ValueError('nested budget mismatch')
        boundary._validate_result(inspected, nested_limits, path + '!' + member['name'], member['sha256'], member['size_actual'])
        zip_entries += len(inspected['entries'])
        minimum_expanded += inspected['coverage']['expanded_bytes_used']
        if minimum_expanded > limits.max_expanded_bytes + 1:
            raise ValueError('nested expansion exceeds distribution budget')
    if zip_entries > coverage['zip_entries'] or minimum_expanded > coverage['expanded_bytes']:
        raise ValueError('inventory exceeds accounted budget')
    if source['sha256'] is None and archives:
        raise ValueError('changed source retains archives')
    if coverage['status'] == 'complete':
        expected_archives = {m['index'] for m in members if m['type'] in ('00', '30')
                             and 'whiteout' not in m and m['name'].lower().endswith(('.jar', '.war', '.ear', '.zip'))}
        if (seen != expected_archives or source['sha256'] != digest or issues or cleanup['leftovers'] or not cleanup['individual_paths_complete']
                or size > limits.max_input_bytes or coverage['zip_entries'] != zip_entries
                or coverage['expanded_bytes'] < 1024 + minimum_expanded
                or any(m['status'] != 'recorded' or m['sha256'] is None for m in members)
                or any(a['inspection']['coverage']['status'] != 'inspected' for a in archives)):
            raise ValueError('inconsistent complete distribution')


def _decode(raw, limits, path, digest, size, format, scratch):
    envelope = _json(raw)
    if not isinstance(envelope, dict) or set(envelope) != {'schema', 'result', 'error', 'cleanup'} or envelope['schema'] != SCHEMA:
        raise ValueError('invalid envelope')
    _validate_ledger(envelope['cleanup'], scratch)
    if envelope['error'] is not None:
        if not isinstance(envelope['error'], str) or envelope['error'] not in _ERRORS or envelope['result'] is not None:
            raise ValueError('invalid error')
    else:
        _validate_result(envelope['result'], limits, path, digest, size, format, envelope['cleanup'], scratch)
    return envelope


def _close_all(handles, primary):
    failure = None
    for index in range(len(handles)):
        handle, handles[index] = handles[index], None
        if handle is None:
            continue
        try:
            handle.close() if hasattr(handle, 'close') else os.close(handle)
        except BaseException as exc:
            if failure is None:
                failure = exc
    if failure is not None and primary is not None:
        primary.add_note('distribution worker descriptor closure unconfirmed')
    return failure


def inspect_isolated(source_fd, *, source_path, sha256, checkout, scratch_base,
                     format, deadline, limits=DistributionLimits(),
                     max_response_bytes=MAX_RESPONSE_BYTES, memory_bytes=MAX_MEMORY_BYTES,
                     worker_command=None):
    """Inspect tar/tar.gz in a trusted worker; preserve caller fds and scratch.

    outcome=complete means a valid worker exchange, not complete distribution
    coverage. Always inspect result.coverage and the independent cleanup ledger.
    Errors return a scratch-root recovery marker; BaseException carries that same
    ledger plus observed lifecycle metadata in distribution_cleanup.
    """
    try:
        _platform()
    except (ValueError, OSError) as exc:
        raise DistributionWorkerError('required default-SIGCHLD process and memory controls unavailable') from exc
    if not _text(source_path) or not _digest(sha256) or not _finite(deadline) or format not in ('tar', 'tar.gz') or not isinstance(limits, DistributionLimits):
        raise DistributionWorkerError('invalid distribution identity, format, limits or deadline')
    for value, maximum in ((max_response_bytes, MAX_RESPONSE_BYTES), (memory_bytes, MAX_MEMORY_BYTES)):
        if type(value) is not int or not 0 < value <= maximum:
            raise DistributionWorkerError('worker bounds exceed supported range')
    scratch, root = os.path.abspath(os.fspath(scratch_base)), os.path.abspath(os.fspath(checkout))
    if not all(_text(p) for p in (scratch, root)):
        raise DistributionWorkerError('invalid checkout or scratch path')
    for first, second in ((root, scratch), (os.path.realpath(root), os.path.realpath(scratch))):
        if os.path.commonpath((first, second)) in (first, second):
            raise DistributionWorkerError('scratch and checkout must not overlap')
    command = [sys.executable, '-B', '-I', '-S', str(_SCRIPT), '--worker'] if worker_command is None else worker_command
    if not isinstance(command, (list, tuple)) or not command or not all(_text(x) for x in command) or not os.path.isabs(command[0]):
        raise DistributionWorkerError('absolute worker executable argv required')
    handles, proc = [], None
    cleanup_result = {'status': 'not-started', 'worker_stopped': True, 'leader_reaped': False,
        'kill_state': None, 'returncode': None, 'assumption': 'trusted-worker-no-descendants'}
    ledger = {'leftovers': [], 'individual_paths_complete': True}
    result = error = None
    try:
        info = _source_info(source_fd)
        original_identity = _identity(info)
        source_copy = os.dup(source_fd)
        handles.append(source_copy)
        scratch_fd = open_checkout_fd(scratch)
        handles.append(scratch_fd)
        scratch_info = _scratch_info(scratch_fd)
        root_fd = open_checkout_fd(root)
        handles.append(root_fd)
        root_info = os.fstat(root_fd)
        request = dict(schema=SCHEMA, source_fd=source_copy, source_path=source_path,
            sha256=sha256, source_identity=list(original_identity), scratch_fd=scratch_fd,
            scratch_base=scratch, scratch_identity=[scratch_info.st_dev, scratch_info.st_ino],
            checkout_fd=root_fd, checkout=root, checkout_identity=[root_info.st_dev, root_info.st_ino],
            format=format, deadline=deadline, limits=asdict(limits), memory_bytes=memory_bytes,
            response_bytes=max_response_bytes)
        data = (json.dumps(request, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(data) > MAX_REQUEST_BYTES:
            raise DistributionWorkerError('request exceeds byte cap')
        if time.monotonic() >= deadline:
            error = 'deadline-exceeded'
        else:
            # A spawn failure can still leave unknown child-side state. Never
            # lose recovery information while a response is unavailable.
            ledger = _recovery(scratch, 'worker-result-unavailable')
            try:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=str(_SCRIPT.parent.parent),
                    env={'PATH': os.defpath, 'HOME': scratch, 'TMPDIR': scratch, 'LC_ALL': 'C'},
                    pass_fds=(source_copy, scratch_fd, root_fd), start_new_session=True)
                cleanup_result = dict(status='unconfirmed', worker_stopped=False, leader_reaped=False,
                    kill_state='unconfirmed', returncode=None, assumption='trusted-worker-no-descendants')
                raw = _exchange(proc, data, deadline, max_response_bytes)
            except _BoundaryFailure as exc:
                error = exc.code
            except OSError:
                error = 'worker-spawn-or-io-failed'
            finally:
                if proc is not None:
                    primary = sys.exception()
                    cleanup_failure = None
                    try:
                        cleanup_result = _cleanup(proc, 2.0)
                    except BaseException as exc:
                        cleanup_failure = exc
                        if primary is not None:
                            primary.add_note('distribution worker termination unconfirmed')
                    close_error = _close_all([proc.stdin, proc.stdout], primary or cleanup_failure)
                    if primary is None:
                        if cleanup_failure is not None:
                            raise cleanup_failure
                        if close_error is not None:
                            raise close_error
            if not cleanup_result['worker_stopped']:
                error = 'worker-cleanup-unconfirmed'
            elif error is None and cleanup_result['returncode'] != 0:
                error = 'worker-failed'
            if error is None:
                try:
                    envelope = _decode(raw, limits, source_path, sha256, info.st_size, format, scratch)
                    ledger = envelope['cleanup']
                    result, error = envelope['result'], envelope['error']
                    if error is not None and not ledger['leftovers']:
                        ledger = _recovery(scratch, 'worker-error-cleanup-unconfirmed')
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
                    error = 'invalid-response'
                if _identity(os.fstat(source_fd)) != original_identity:
                    result, error = None, 'source-changed'
                    ledger = _add_recovery(ledger, scratch, 'source-changed-cleanup-unconfirmed')
                if time.monotonic() >= deadline:
                    result, error = None, 'deadline-exceeded'
                    ledger = _add_recovery(ledger, scratch, 'deadline-cleanup-unconfirmed')
    except BaseException as exc:
        exc.distribution_cleanup = dict(ledger, worker=cleanup_result)
        raise
    finally:
        primary = sys.exception()
        close_error = _close_all(handles, primary)
        if close_error is not None:
            ledger = _add_recovery(ledger, scratch, 'descriptor-close-unconfirmed')
            if primary is not None:
                primary.distribution_cleanup = dict(ledger, worker=cleanup_result)
            else:
                close_error.distribution_cleanup = dict(ledger, worker=cleanup_result)
                raise close_error
    return dict(outcome='failed' if error else 'complete', result=result, error=error,
        cleanup=ledger, cleanup_result=cleanup_result,
        worker_pid=proc.pid if proc is not None else None,
        execution={'memory_bytes': memory_bytes, 'response_bytes': max_response_bytes,
                   'deadline': deadline})


def _verify_directory(fd, path, expected, scratch=False):
    held = _scratch_info(fd) if scratch else os.fstat(fd)
    if not stat.S_ISDIR(held.st_mode) or expected != [held.st_dev, held.st_ino]:
        raise _BoundaryFailure('scratch-identity-changed' if scratch else 'checkout-identity-changed')
    current_fd = open_checkout_fd(path)
    try:
        current = _scratch_info(current_fd) if scratch else os.fstat(current_fd)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise _BoundaryFailure('scratch-identity-changed' if scratch else 'checkout-identity-changed')
    finally:
        os.close(current_fd)


def _worker():
    cap = MAX_RESPONSE_BYTES
    scratch = None
    ledger = None
    try:
        _platform()
        _set_memory_limit(MAX_MEMORY_BYTES)
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise _BoundaryFailure('invalid-request')
        request = _json(raw)
        expected = {'schema', 'source_fd', 'source_path', 'sha256', 'source_identity', 'scratch_fd',
                    'scratch_base', 'scratch_identity', 'checkout_fd', 'checkout', 'checkout_identity',
                    'format', 'deadline', 'limits', 'memory_bytes', 'response_bytes'}
        if not isinstance(request, dict) or set(request) != expected or request['schema'] != SCHEMA:
            raise _BoundaryFailure('invalid-request')
        scratch = request['scratch_base']
        if not _text(scratch) or not os.path.isabs(scratch) or os.path.normpath(scratch) != scratch:
            raise _BoundaryFailure('invalid-request')
        ledger = _recovery(scratch, 'worker-inspection-unconfirmed')
        cap, memory = request['response_bytes'], request['memory_bytes']
        if type(cap) is not int or not 0 < cap <= MAX_RESPONSE_BYTES or type(memory) is not int or not 0 < memory <= MAX_MEMORY_BYTES:
            raise _BoundaryFailure('invalid-request')
        _set_memory_limit(memory)
        if not _finite(request['deadline']) or not _text(request['source_path']) or not _digest(request['sha256']) or request['format'] not in ('tar', 'tar.gz'):
            raise _BoundaryFailure('invalid-request')
        if time.monotonic() >= request['deadline']:
            raise _BoundaryFailure('deadline-exceeded')
        _verify_directory(request['scratch_fd'], scratch, request['scratch_identity'], True)
        _verify_directory(request['checkout_fd'], request['checkout'], request['checkout_identity'])
        if list(_identity(_source_info(request['source_fd']))) != request['source_identity']:
            raise _BoundaryFailure('source-changed')
        limits = DistributionLimits(**request['limits'])
        result = inspect_distribution_fd(request['source_fd'], source_path=request['source_path'],
            checkout=request['checkout'], scratch_base=scratch, format=request['format'],
            deadline=request['deadline'], limits=limits)
        ledger = _ledger(result['coverage']['cleanup'], scratch)
        if result['source']['sha256'] not in (None, request['sha256']):
            raise _BoundaryFailure('source-changed')
        if list(_identity(_source_info(request['source_fd']))) != request['source_identity']:
            raise _BoundaryFailure('source-changed')
        if time.monotonic() >= request['deadline']:
            raise _BoundaryFailure('deadline-exceeded')
        boundary._emit(dict(schema=SCHEMA, result=result, error=None, cleanup=ledger), cap)
        return 0
    except BaseException as exc:
        if scratch is not None:
            lower = getattr(exc, 'distribution_cleanup', None)
            if lower is not None:
                ledger = _ledger(lower, scratch)
        code = exc.code if isinstance(exc, _BoundaryFailure) else ('memory-exhausted' if isinstance(exc, MemoryError) else 'inspection-failed')
        if code not in _ERRORS:
            code = 'inspection-failed'
    # A valid parent request always supplies scratch. Malformed direct invocations
    # can emit no ledger; the parent will reject it and retain its own root marker.
    if ledger is None:
        ledger = {'leftovers': [], 'individual_paths_complete': False}
    boundary._emit(dict(schema=SCHEMA, result=None, error=code, cleanup=ledger), MAX_RESPONSE_BYTES)
    return 0


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit(2)
    raise SystemExit(_worker())
