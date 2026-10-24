"""One-shot process isolation for inventories of explicitly supplied OCI layouts.

A caller-owned read-only layout directory is inherited by a trusted -B -I -S
worker. Its logical pathname is never reopened: manifest and blob observations
stay anchored to that directory. Checkout and private scratch directories are
opened nofollow and bound by descriptor identity; the lower reader still uses
these two paths, so their same-user mutation checks are not a sandbox.

The absolute deadline is shared with the caller and includes process startup.
Child decompression is bounded by a killable process and an address-space cap.
Parent filesystem IO, Popen and JSON validation are cooperative, not themselves
hard-interruptible. Cleanup has a separate two-second grace. Default SIGCHLD and
sole child-reaping ownership are required; external reapers are unsupported.

Only fixed failure codes cross the boundary. A bounded cleanup ledger survives
rejected responses, timeouts and interruptions (oci_cleanup on exceptions).
Unconfirmed worker termination never grants permission to remove scratch. This
module never deletes scratch: even a recovery root is an obligation to inspect,
not authority to delete. The trusted worker must never spawn descendants.

Inventories describe layer blobs only, never an applied image filesystem or
application/container attribution. Whiteouts are observed, not applied; config
commands and environment are never emitted or executed. No pull or lookup occurs.
Explicit worker_command is a trusted test seam, not an untrusted plugin API.
Protocol validation checks internal identity and budget consistency, not the
cryptographic authenticity of arbitrary test-worker claims.
"""

import fcntl
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
from mdi import distribution_worker as distribution_boundary
from mdi.discover import open_checkout_fd
from mdi.distributions import DistributionLimits
from mdi.oci_layout import CONFIG, MANIFEST, OCILimits, _LAYER_FORMATS, inspect_oci_layout_fd
from mdi.archive_worker import (
    _BoundaryFailure, _cleanup, _digest, _exchange, _finite, _json,
    _platform, _scratch_info, _set_memory_limit, _text,
)
from mdi.distribution_worker import (
    _add_recovery, _close_all, _recovery, _structure, _validate_ledger,
    _verify_directory,
)

SCHEMA = 'mdi.oci-worker/1'
MAX_RESPONSE_BYTES = boundary.MAX_RESPONSE_BYTES
MAX_MEMORY_BYTES = boundary.MAX_MEMORY_BYTES
MAX_REQUEST_BYTES = boundary.MAX_REQUEST_BYTES
_SCRIPT = Path(__file__).resolve()
_ERRORS = frozenset(('invalid-request', 'inspection-failed', 'memory-limit-unavailable',
    'memory-exhausted', 'deadline-exceeded', 'response-too-large',
    'scratch-identity-changed', 'checkout-identity-changed', 'layout-identity-changed'))
TOOL_IDENTITY = {'name': 'mdi.oci_worker', 'version': '1',
                 'source_sha256': hashlib.sha256(_SCRIPT.read_bytes()).hexdigest(),
                 'process_boundary': boundary.TOOL_IDENTITY}


class OCIWorkerError(ValueError):
    """Invalid invocation or unavailable required process safety."""


def _layout_info(fd):
    if type(fd) is not int or fd < 0:
        raise OCIWorkerError('layout must be an open read-only directory descriptor')
    try:
        info = os.fstat(fd)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except (OSError, ValueError, OverflowError):
        raise OCIWorkerError('layout descriptor unavailable') from None
    if (not stat.S_ISDIR(info.st_mode) or flags & os.O_ACCMODE != os.O_RDONLY
            or flags & getattr(os, 'O_PATH', 0)):
        raise OCIWorkerError('layout must be an open read-only directory descriptor')
    return info


def _layout_identity(info):
    # A pathname rename changes ctime, not the descriptor-anchored directory.
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _path(value):
    try:
        path = os.path.abspath(os.fspath(value))
        if not _text(path):
            raise ValueError()
        path.encode('utf-8')
        return path
    except (TypeError, ValueError, UnicodeError):
        raise OCIWorkerError('paths must be bounded UTF-8 text') from None


def _no_overlap(scratch, path):
    for first, second in ((scratch, path), (os.path.realpath(scratch), os.path.realpath(path))):
        first, second = '/' + first.lstrip('/'), '/' + second.lstrip('/')
        if os.path.commonpath((first, second)) in (first, second):
            raise OCIWorkerError('scratch must not overlap checkout or layout')


def _oci_digest(value):
    return isinstance(value, str) and value.startswith('sha256:') and _digest(value[7:])


def _descriptor(value, media=None):
    if not isinstance(value, dict) or not {'mediaType', 'digest', 'size'} <= set(value) or set(value) - {'mediaType', 'digest', 'size', 'platform'}:
        raise ValueError('invalid descriptor')
    if (not _text(value['mediaType']) or media is not None and value['mediaType'] != media
            or not _oci_digest(value['digest']) or type(value['size']) is not int or value['size'] < 0):
        raise ValueError('invalid descriptor values')
    if 'platform' in value:
        _platform_values(value['platform'])


def _platform_values(value):
    if (not isinstance(value, dict) or not {'architecture', 'os'} <= set(value)
            or set(value) - {'architecture', 'os', 'variant', 'os.version'}
            or any(not _text(v) for v in value.values())):
        raise ValueError('invalid platform')


def _ledger(value, scratch):
    """Preserve all 32 lower ledger slots, including its recovery marker."""
    if not isinstance(value, dict) or set(value) != {'status', 'leftovers', 'individual_paths_complete'}:
        raise ValueError('invalid OCI cleanup ledger')
    rows = value['leftovers']
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError('invalid cleanup count')
    if value['status'] != ('retained' if rows else 'complete'):
        raise ValueError('invalid cleanup status')
    clean = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'reason', 'identity_verified'} or row['identity_verified'] is not False:
            raise ValueError('invalid cleanup observation')
        clean.append({'path': row['path'], 'reason': row['reason']})
    ledger = dict(leftovers=clean, individual_paths_complete=value['individual_paths_complete'])
    _validate_ledger(ledger, scratch)
    return ledger


def _validate_result(result, limits, path, selected, cleanup, scratch):
    _structure(result)
    if not isinstance(result, dict) or set(result) != {'source', 'manifests', 'selected_manifest', 'config', 'layers', 'issues', 'coverage', 'cleanup'}:
        raise ValueError('invalid OCI inventory')
    source, coverage = result['source'], result['coverage']
    if (not isinstance(source, dict) or not {'path', 'observed_at'} <= set(source)
            or set(source) - {'path', 'observed_at', 'binding', 'captures', 'unchanged'}
            or source['path'] != path or not _text(source['observed_at'])
            or source.get('binding') not in (None, 'caller-opened-layout-descriptor')):
        raise ValueError('invalid source binding')
    if 'unchanged' in source and type(source['unchanged']) is not bool:
        raise ValueError('invalid source observation')
    if (not isinstance(coverage, dict) or coverage.get('status') != 'partial'
            or coverage.get('scope') != 'layer-inventory-only'
            or coverage.get('merged_filesystem') is not False
            or coverage.get('format_support') != 'provisional-synthetic-fixtures'
            or coverage.get('limits') != asdict(limits)
            or any(type(v) is not int for v in coverage['limits'].values())):
        raise ValueError('invalid coverage')
    for key, maximum in (('input_bytes', limits.max_total_blob_bytes),
                         ('expanded_bytes', limits.max_expanded_bytes + 1)):
        if type(coverage.get(key)) is not int or not 0 <= coverage[key] <= maximum:
            raise ValueError('invalid coverage budget')
    if not isinstance(coverage.get('limitations'), list) or not all(_text(x) for x in coverage['limitations']):
        raise ValueError('invalid limitations')
    if _ledger(result['cleanup'], scratch) != cleanup:
        raise ValueError('cleanup mismatch')
    manifests, layers, issues = result['manifests'], result['layers'], result['issues']
    if (not all(isinstance(v, list) for v in (manifests, layers, issues))
            or len(manifests) > limits.max_descriptors or len(layers) > limits.max_layers
            or len(issues) > 4 * (limits.max_layers + limits.max_descriptors + 1)):
        raise ValueError('invalid inventory lists')
    for issue in issues:
        if (not isinstance(issue, dict) or not _text(issue.get('code'))
                or set(issue) - {'code', 'media_type', 'layer_index'}
                or 'media_type' in issue and not _text(issue['media_type'])
                or 'layer_index' in issue and (type(issue['layer_index']) is not int
                    or not 0 <= issue['layer_index'] < limits.max_layers)):
            raise ValueError('invalid inventory issue')
    capture_rows = source.get('captures', [])
    if not isinstance(capture_rows, list) or len(capture_rows) > limits.max_descriptors + 2:
        raise ValueError('invalid capture list')
    captures = {}
    capture_bytes = 0
    for capture in capture_rows:
        if (not isinstance(capture, dict) or set(capture) != {'path', 'sha256', 'size'}
                or not _digest(capture['sha256']) or type(capture['size']) is not int or capture['size'] < 0):
            raise ValueError('invalid captured blob')
        name = capture['path']
        if name not in ('oci-layout', 'index.json'):
            if not isinstance(name, str) or not name.startswith('blobs/sha256/') or not _digest(name[len('blobs/sha256/'):]) or name[len('blobs/sha256/'):] != capture['sha256']:
                raise ValueError('invalid content-addressed capture')
        elif capture['size'] > limits.max_json_bytes:
            raise ValueError('metadata capture too large')
        if name in captures or capture['size'] > max(limits.max_blob_bytes, limits.max_json_bytes):
            raise ValueError('duplicate or oversized capture')
        captures[name] = capture
        capture_bytes += capture['size']
    if len(captures) > limits.max_descriptors + 2 or capture_bytes > coverage['input_bytes']:
        raise ValueError('captured input budget mismatch')

    def captured(descriptor, metadata=False):
        key = 'blobs/sha256/' + descriptor['digest'][7:]
        entry = captures.get(key)
        if entry is None or entry['size'] != descriptor['size'] or entry['sha256'] != descriptor['digest'][7:]:
            raise ValueError('uncaptured descriptor')
        if metadata and entry['size'] > limits.max_json_bytes:
            raise ValueError('metadata descriptor too large')

    known = {}
    routes = set()
    for row in manifests:
        if not isinstance(row, dict) or set(row) != {'descriptor', 'routes'}:
            raise ValueError('invalid manifest observation')
        desc = row['descriptor']
        _descriptor(desc, MANIFEST)
        if desc['digest'] in known or not isinstance(row['routes'], list) or not row['routes']:
            raise ValueError('duplicate or unrouted manifest')
        for route in row['routes']:
            if not isinstance(route, list) or not route or len(route) > 2 * limits.max_index_depth + 1 or len(route) % 2 != 1:
                raise ValueError('invalid manifest route')
            for index, value in enumerate(route):
                if index % 2 == 0:
                    if type(value) is not int or value < 0:
                        raise ValueError('invalid route index')
                elif not _oci_digest(value):
                    raise ValueError('invalid route digest')
            key = tuple(route)
            if key in routes or len(routes) >= limits.max_descriptors:
                raise ValueError('duplicate route or route cap')
            routes.add(key)
        known[desc['digest']] = desc
    chosen = result['selected_manifest']
    if chosen is not None:
        if (not _oci_digest(chosen) or chosen not in known
                or selected is not None and chosen != selected
                or selected is None and len(known) != 1):
            raise ValueError('manifest selection mismatch')
        if source.get('unchanged') is not True or source.get('binding') != 'caller-opened-layout-descriptor':
            raise ValueError('selected image lacks source identity')
        if not {'oci-layout', 'index.json'} <= set(captures):
            raise ValueError('missing layout metadata')
        captured(known[chosen], True)
        config = result['config']
        _descriptor(config, CONFIG)
        if 'platform' not in config:
            raise ValueError('missing config platform')
        captured(config, True)
        if any(config['platform'].get(k) != v for k, v in known[chosen].get('platform', {}).items()):
            raise ValueError('platform mismatch')
    elif result['config'] is not None or layers or not issues:
        raise ValueError('unselected inventory has layers or no reason')
    if source.get('unchanged') is False and (chosen is not None or manifests or layers):
        raise ValueError('changed source retains identity')
    expanded = members = archives = zip_entries = 0
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or set(layer) != {'index', 'mediaType', 'digest', 'size', 'realm', 'distribution', 'whiteouts', 'presence_scope'}:
            raise ValueError('invalid layer')
        desc = {k: layer[k] for k in ('mediaType', 'digest', 'size')}
        _descriptor(desc)
        format = _LAYER_FORMATS.get(desc['mediaType'])
        if type(layer['index']) is not int or layer['index'] != index or format is None or layer['realm'] != 'unknown' or layer['presence_scope'] != 'layer-blob-only':
            raise ValueError('invalid layer identity')
        captured(desc)
        distribution_limits = DistributionLimits(max_input_bytes=limits.max_blob_bytes,
            max_member_bytes=limits.max_member_bytes, max_inventory_bytes=limits.max_inventory_bytes,
            max_text=limits.max_text, max_expanded_bytes=limits.max_expanded_bytes-expanded,
            max_members=limits.max_members-members, max_archives=limits.max_archives-archives,
            max_zip_entries=limits.max_zip_entries-zip_entries)
        distribution = layer['distribution']
        lower_cleanup = distribution_boundary._ledger(distribution.get('coverage', {}).get('cleanup'), scratch) if isinstance(distribution, dict) else None
        distribution_boundary._validate_result(distribution, distribution_limits,
            os.path.join(path, 'blobs', 'sha256', desc['digest'][7:]), desc['digest'][7:],
            desc['size'], format, lower_cleanup, scratch, oci_whiteouts=True)
        expanded += distribution['coverage']['expanded_bytes']
        members += len(distribution['members'])
        archives += len(distribution['archives'])
        zip_entries += distribution['coverage']['zip_entries']
        if cleanup['individual_paths_complete'] and any(row not in cleanup['leftovers'] for row in lower_cleanup['leftovers']):
            raise ValueError('lost layer cleanup obligation')
        expected_whiteouts = []
        for member in distribution['members']:
            if 'whiteout' in member:
                expected_whiteouts.append(dict(member_index=member['index'], name=member['name'],
                    **member['whiteout']))
        if layer['whiteouts'] != expected_whiteouts:
            raise ValueError('whiteout observation mismatch')
    if expanded > coverage['expanded_bytes']:
        raise ValueError('layer expansion accounting mismatch')
    if layers and not issues:
        raise ValueError('layer composition limitation missing')


def _decode(raw, limits, path, selected, scratch):
    envelope = _json(raw)
    if not isinstance(envelope, dict) or set(envelope) != {'schema', 'result', 'error', 'cleanup'} or envelope['schema'] != SCHEMA:
        raise ValueError('invalid envelope')
    _validate_ledger(envelope['cleanup'], scratch)
    if envelope['error'] is not None:
        if not isinstance(envelope['error'], str) or envelope['error'] not in _ERRORS or envelope['result'] is not None:
            raise ValueError('invalid failure')
    else:
        _validate_result(envelope['result'], limits, path, selected, envelope['cleanup'], scratch)
    return envelope


def inspect_isolated(layout_fd, *, layout_path, checkout, scratch_base, deadline,
                     manifest_digest=None, limits=OCILimits(),
                     max_response_bytes=MAX_RESPONSE_BYTES, memory_bytes=MAX_MEMORY_BYTES,
                     worker_command=None):
    """Return a validated layer inventory and independent cleanup obligations.

    outcome=complete means a valid exchange, not an applied or complete image.
    Consult result.coverage and cleanup; exceptions carry oci_cleanup with worker
    lifecycle metadata. Caller layout descriptors and scratch are never removed.
    """
    try:
        _platform()
    except (ValueError, OSError) as exc:
        raise OCIWorkerError('default-SIGCHLD process and memory controls required') from exc
    if not _finite(deadline) or not isinstance(limits, OCILimits) or manifest_digest is not None and not _oci_digest(manifest_digest):
        raise OCIWorkerError('invalid limits, deadline or manifest digest')
    for value, maximum in ((max_response_bytes, MAX_RESPONSE_BYTES), (memory_bytes, MAX_MEMORY_BYTES)):
        if type(value) is not int or not 0 < value <= maximum:
            raise OCIWorkerError('worker bounds exceed supported range')
    path, root, scratch = map(_path, (layout_path, checkout, scratch_base))
    _no_overlap(scratch, path)
    _no_overlap(scratch, root)
    _layout_info(layout_fd)
    command = [sys.executable, '-B', '-I', '-S', str(_SCRIPT), '--worker'] if worker_command is None else worker_command
    if not isinstance(command, (list, tuple)) or not command or not all(_text(v) for v in command) or not os.path.isabs(command[0]):
        raise OCIWorkerError('absolute worker executable argv required')
    handles, proc = [], None
    lifecycle = dict(status='not-started', worker_stopped=True, leader_reaped=False,
                     kill_state=None, returncode=None, assumption='trusted-worker-no-descendants')
    ledger = dict(leftovers=[], individual_paths_complete=True)
    result = error = None
    try:
        source_copy = os.dup(layout_fd)
        handles.append(source_copy)
        identity = _layout_identity(_layout_info(source_copy))
        scratch_fd = open_checkout_fd(scratch)
        handles.append(scratch_fd)
        scratch_info = _scratch_info(scratch_fd)
        root_fd = open_checkout_fd(root)
        handles.append(root_fd)
        root_info = os.fstat(root_fd)
        request = dict(schema=SCHEMA, layout_fd=source_copy, layout_path=path,
            layout_identity=identity, scratch_fd=scratch_fd, scratch_base=scratch,
            scratch_identity=[scratch_info.st_dev, scratch_info.st_ino],
            checkout_fd=root_fd, checkout=root, checkout_identity=[root_info.st_dev, root_info.st_ino],
            deadline=deadline, manifest_digest=manifest_digest, limits=asdict(limits),
            memory_bytes=memory_bytes, response_bytes=max_response_bytes)
        data = (json.dumps(request, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(data) > MAX_REQUEST_BYTES:
            raise OCIWorkerError('request exceeds byte cap')
        if time.monotonic() >= deadline:
            error = 'deadline-exceeded'
        else:
            ledger = _recovery(scratch, 'worker-result-unavailable')
            try:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=str(_SCRIPT.parent.parent),
                    env={'PATH': os.defpath, 'HOME': scratch, 'TMPDIR': scratch, 'LC_ALL': 'C'},
                    pass_fds=(source_copy, scratch_fd, root_fd), start_new_session=True)
                lifecycle = dict(status='unconfirmed', worker_stopped=False, leader_reaped=False,
                    kill_state='unconfirmed', returncode=None, assumption='trusted-worker-no-descendants')
                raw = _exchange(proc, data, deadline, max_response_bytes)
            except _BoundaryFailure as exc:
                error = exc.code
            except OSError:
                error = 'worker-spawn-or-io-failed'
            finally:
                if proc is not None:
                    primary, cleanup_failure = sys.exception(), None
                    try:
                        lifecycle = _cleanup(proc, 2.0)
                    except BaseException as exc:
                        cleanup_failure = exc
                        if primary is not None:
                            primary.add_note('OCI worker termination unconfirmed')
                    close_error = _close_all([proc.stdin, proc.stdout], primary or cleanup_failure)
                    if primary is None:
                        if cleanup_failure is not None:
                            raise cleanup_failure
                        if close_error is not None:
                            raise close_error
            if not lifecycle['worker_stopped']:
                error = 'worker-cleanup-unconfirmed'
            elif error is None and lifecycle['returncode'] != 0:
                error = 'worker-failed'
            if error is None:
                try:
                    envelope = _decode(raw, limits, path, manifest_digest, scratch)
                    ledger = envelope['cleanup']
                    result, error = envelope['result'], envelope['error']
                    if error is not None and not ledger['leftovers']:
                        ledger = _recovery(scratch, 'worker-error-cleanup-unconfirmed')
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
                    error = 'invalid-response'
                if _layout_identity(_layout_info(source_copy)) != identity:
                    result, error = None, 'layout-identity-changed'
                    ledger = _add_recovery(ledger, scratch, 'layout-changed-cleanup-unconfirmed')
                if time.monotonic() >= deadline:
                    result, error = None, 'deadline-exceeded'
                    ledger = _add_recovery(ledger, scratch, 'deadline-cleanup-unconfirmed')
    except BaseException as exc:
        exc.oci_cleanup = dict(ledger, worker=lifecycle)
        raise
    finally:
        primary = sys.exception()
        failure = _close_all(handles, primary)
        if failure is not None:
            ledger = _add_recovery(ledger, scratch, 'descriptor-close-unconfirmed')
            if primary is not None:
                primary.oci_cleanup = dict(ledger, worker=lifecycle)
            else:
                failure.oci_cleanup = dict(ledger, worker=lifecycle)
                raise failure
    return dict(outcome='failed' if error else 'complete', result=result, error=error,
        cleanup=ledger, cleanup_result=lifecycle, worker_pid=proc.pid if proc is not None else None,
        execution=dict(memory_bytes=memory_bytes, response_bytes=max_response_bytes, deadline=deadline))


def _worker():
    cap, scratch, ledger = MAX_RESPONSE_BYTES, None, None
    try:
        _platform()
        _set_memory_limit(MAX_MEMORY_BYTES)
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise _BoundaryFailure('invalid-request')
        request = _json(raw)
        expected = {'schema', 'layout_fd', 'layout_path', 'layout_identity', 'scratch_fd',
            'scratch_base', 'scratch_identity', 'checkout_fd', 'checkout', 'checkout_identity',
            'deadline', 'manifest_digest', 'limits', 'memory_bytes', 'response_bytes'}
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
        if (not _finite(request['deadline']) or not _text(request['layout_path'])
                or request['manifest_digest'] is not None and not _oci_digest(request['manifest_digest'])):
            raise _BoundaryFailure('invalid-request')
        if time.monotonic() >= request['deadline']:
            raise _BoundaryFailure('deadline-exceeded')
        _verify_directory(request['scratch_fd'], scratch, request['scratch_identity'], True)
        _verify_directory(request['checkout_fd'], request['checkout'], request['checkout_identity'])
        if _layout_identity(_layout_info(request['layout_fd'])) != request['layout_identity']:
            raise _BoundaryFailure('layout-identity-changed')
        limits = OCILimits(**request['limits'])
        result = inspect_oci_layout_fd(request['layout_fd'], layout_path=request['layout_path'],
            checkout=request['checkout'], scratch_base=scratch, deadline=request['deadline'],
            manifest_digest=request['manifest_digest'], limits=limits)
        ledger = _ledger(result['cleanup'], scratch)
        if _layout_identity(_layout_info(request['layout_fd'])) != request['layout_identity']:
            raise _BoundaryFailure('layout-identity-changed')
        if time.monotonic() >= request['deadline']:
            raise _BoundaryFailure('deadline-exceeded')
        boundary._emit(dict(schema=SCHEMA, result=result, error=None, cleanup=ledger), cap)
        return 0
    except BaseException as exc:
        if scratch is not None:
            lower = getattr(exc, 'oci_cleanup', None)
            if lower is not None:
                try:
                    ledger = _ledger(lower, scratch)
                except (ValueError, TypeError, KeyError):
                    ledger = _recovery(scratch, 'cleanup-details-unavailable')
        code = exc.code if isinstance(exc, _BoundaryFailure) else ('memory-exhausted' if isinstance(exc, MemoryError) else 'inspection-failed')
        if code not in _ERRORS:
            code = 'inspection-failed'
    if ledger is None:
        ledger = dict(leftovers=[], individual_paths_complete=False)
    boundary._emit(dict(schema=SCHEMA, result=None, error=code, cleanup=ledger), MAX_RESPONSE_BYTES)
    return 0


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit(2)
    raise SystemExit(_worker())
