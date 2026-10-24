"""Isolated file-backed archive inspection with one shared absolute deadline.

The parent passes an already captured read-only descriptor, not archive bytes,
through pass_fds to a fresh isolated interpreter. Its explicit -B flag prevents
import bytecode writes even though -I ignores PYTHONDONTWRITEBYTECODE. The trusted
worker never spawns children. It applies an address-space limit before inspecting the file,
checks the inherited scratch directory against the supplied path, and runs
archive_files.inspect_archive_fd. The parent bounds both protocol directions
and kills the owned process group on deadline, overflow, interruption, and
normal completion, before reaping the leader.

The deadline is supplied by the stage owner and is not reset after spawn: any
startup time consumes it. Kernel process creation, filesystem operations and
parent-side JSON decoding cannot themselves be interrupted by this module;
an overrun is checked when control returns. Source capture performed before
this call remains the caller's responsibility and is not made interruptible
by putting subsequent inspection in a worker. Cleanup has its own bounded
reap grace, so total return latency can exceed the stage deadline by that grace.

A default SIGCHLD disposition and sole ownership of child reaping are required.
waitid(WNOWAIT) retains leader identity until group signaling finishes; ECHILD
suppresses signaling and reports unconfirmed cleanup. Concurrent external
reaping is unsupported: the ownership probe and signal are not atomic against
another reaper. Signals cannot cover descendants that detach; cleanup permission
rests on the trusted worker's no-descendants contract, not group signaling alone.

The parent never removes scratch files or closes caller-owned descriptors.
The caller must place scratch outside the checkout and may release captured
spools only when cleanup_result.worker_stopped is true AND all its own readers
are closed. That boolean is an observed lifecycle result, not user consent.
BaseException propagates after cleanup with an archive_cleanup attribute; if
cleanup cannot be confirmed, scratch must be retained. This is tamper evidence,
not isolation from hostile same-user filesystem mutation.

Explicit worker_command is a trusted test seam, not untrusted configuration.
Only the default command is covered by the no-descendants contract and resource
limit implementation. No raw worker exception text or stderr is returned.
Archive inventory names and metadata inside successful results are untrusted
evidence, not instructions or claims of secret-free content.
"""

import fcntl
import hashlib
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

try:
    import resource
except ImportError:
    resource = None

if __name__ == '__main__' and __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mdi.archive_files import FileArchiveLimits, inspect_archive_fd
from mdi.discover import open_checkout_fd

SCHEMA = 'mdi.archive-worker/1'
MAX_RESPONSE_BYTES = 24 * 1024**2
MAX_MEMORY_BYTES = 768 * 1024**2
MAX_REQUEST_BYTES = 64 * 1024
MAX_TEXT = 4096
_CHUNK = 65536
_REAP_SECONDS = 2.0
_SCRIPT = Path(__file__).resolve()
_ERRORS = frozenset(('invalid-request', 'inspection-failed', 'memory-limit-unavailable',
                     'memory-exhausted', 'deadline-exceeded', 'response-too-large',
                     'scratch-identity-changed'))
TOOL_IDENTITY = {'name': 'mdi.archive_worker', 'version': '1',
                 'source_sha256': hashlib.sha256(_SCRIPT.read_bytes()).hexdigest()}


class ArchiveWorkerError(ValueError):
    """Invalid invocation or unavailable required process/resource primitives."""


class _BoundaryFailure(Exception):
    def __init__(self, code):
        self.code = code


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT and '\x00' not in value


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _platform():
    if (os.name != 'posix' or resource is None or not hasattr(resource, 'RLIMIT_AS')
            or not all(hasattr(os, name) for name in
                       ('waitid', 'P_PID', 'WNOWAIT', 'WNOHANG', 'WEXITED', 'killpg', 'pread', 'set_blocking'))):
        raise ArchiveWorkerError('required process and memory controls unavailable')
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise ArchiveWorkerError('default SIGCHLD and sole child-reaper ownership required')


def _source_info(fd):
    if type(fd) is not int or fd < 0:
        raise ArchiveWorkerError('source must be a read-only regular descriptor')
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ArchiveWorkerError('source must be a read-only regular descriptor')
    return info


def _scratch_info(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ArchiveWorkerError('scratch must be a private caller-owned directory')
    return info


def _probe(proc):
    try:
        observed = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return 'lost'
    return 'exited' if observed is not None else 'alive'


def _cleanup(proc, grace):
    """Observe, signal while identity is held, then reap; never delete scratch."""
    result = {'status': 'unconfirmed', 'worker_stopped': False, 'leader_reaped': False,
              'kill_state': 'unconfirmed', 'returncode': None,
              'assumption': 'trusted-worker-no-descendants'}
    try:
        if _probe(proc) == 'lost':
            # Popen may synthesize rc=0 for ECHILD. Close its bookkeeping only;
            # never expose that value as an observed process result.
            proc.wait(timeout=0)
            return result
        pid = proc.pid
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            # pgid 1 becomes kill(-1): every process of the user. Never
            # signal an unvalidated group id.
            result['kill_state'] = 'refused-invalid-pgid'
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
                result['kill_state'] = 'sent'
            except ProcessLookupError:
                result['kill_state'] = 'absent'
            except OSError:
                result['kill_state'] = 'failed'
        until = time.monotonic() + grace
        while True:
            state = _probe(proc)
            if state == 'lost':
                proc.wait(timeout=0)
                return result
            if state == 'exited':
                result['returncode'] = proc.wait(timeout=0)
                result['leader_reaped'] = True
                # A failed or refused group signal does not confirm the
                # supported scope even if the leader exited by itself during
                # cleanup: no signal means the leader exit proves nothing
                # about descendants.
                if result['kill_state'] not in ('failed', 'refused-invalid-pgid'):
                    result['worker_stopped'] = True
                    result['status'] = 'confirmed'
                return result
            remaining = until - time.monotonic()
            if remaining <= 0:
                return result
            time.sleep(min(.01, remaining))
    except (OSError, subprocess.TimeoutExpired):
        return result


def _exchange(proc, request, deadline, cap):
    chunks, used, sent = [], 0, 0
    with selectors.DefaultSelector() as selector:
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE)
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BoundaryFailure('deadline-exceeded')
            for key, _events in selector.select(remaining):
                if time.monotonic() >= deadline:
                    raise _BoundaryFailure('deadline-exceeded')
                if key.fileobj is proc.stdin:
                    try:
                        count = os.write(proc.stdin.fileno(), request[sent:sent + _CHUNK])
                    except (BlockingIOError, InterruptedError):
                        continue
                    except BrokenPipeError:
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
                        continue
                    if count <= 0:
                        raise _BoundaryFailure('worker-protocol-failed')
                    sent += count
                    if sent == len(request):
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
                else:
                    try:
                        chunk = os.read(proc.stdout.fileno(), min(_CHUNK, cap + 1 - used))
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not chunk:
                        selector.unregister(proc.stdout)
                    else:
                        used += len(chunk)
                        if used > cap:
                            raise _BoundaryFailure('response-too-large')
                        chunks.append(chunk)
        while True:
            if time.monotonic() >= deadline:
                raise _BoundaryFailure('deadline-exceeded')
            state = _probe(proc)
            if state == 'lost':
                raise _BoundaryFailure('identity-lost')
            if state == 'exited':
                return b''.join(chunks)
            time.sleep(min(.01, max(0, deadline - time.monotonic())))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('nonfinite number')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('nonfinite number')
    return number


def _json(raw):
    if not isinstance(raw, bytes) or not raw.endswith(b'\n') or raw.count(b'\n') != 1:
        raise ValueError('expected one JSON line')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                      parse_constant=_constant, parse_float=_float)


def _validate_result(result, limits, source_path, expected_digest, source_size):
    """Validate the inventory boundary before returning worker-supplied data."""
    if not isinstance(result, dict) or set(result) != {'archives', 'entries', 'issues', 'coverage'}:
        raise ValueError('invalid inventory')
    containers, entries, issues = (result[k] for k in ('archives', 'entries', 'issues'))
    if not all(isinstance(x, list) for x in (containers, entries, issues)):
        raise ValueError('invalid inventory lists')
    if len(containers) > limits.max_entries + 1 or len(entries) > limits.max_entries or len(issues) > 4 * (limits.max_entries + 1):
        raise ValueError('inventory count cap')
    by_id = {}
    by_chain = {}
    for item in containers:
        if not isinstance(item, dict) or not _text(item.get('archive_id')) or item['archive_id'] in by_id:
            raise ValueError('invalid archive id')
        by_id[item['archive_id']] = item
        chain = item.get('entry_chain')
        if not isinstance(chain, list) or len(chain) > limits.max_depth or any(type(i) is not int or i < 0 for i in chain):
            raise ValueError('invalid entry chain')
        chain_key = tuple(chain)
        if chain_key in by_chain:
            raise ValueError('duplicate archive chain')
        by_chain[chain_key] = item
        if type(item.get('size')) is not int or item['size'] < 0:
            raise ValueError('invalid archive size')
        if chain and item['size'] > min(limits.max_entry_bytes, limits.max_expanded_bytes):
            raise ValueError('nested archive exceeds bounds')
        if item.get('inspection_status') not in ('complete', 'partial', 'rejected', 'unsupported', 'malformed'):
            raise ValueError('invalid archive status')
        if item.get('sha256') is not None and not _digest(item['sha256']):
            raise ValueError('invalid archive digest')
        if item.get('path') != source_path or not isinstance(item.get('display_path'), str):
            raise ValueError('invalid archive locator')
        identity = item.get('identity')
        if (not isinstance(identity, dict) or set(identity) != {'status', 'conflict', 'candidates', 'evidence'}
                or identity['status'] not in ('candidate', 'unknown')
                or type(identity['conflict']) is not bool
                or not isinstance(identity['candidates'], list)
                or not isinstance(identity['evidence'], list)):
            raise ValueError('invalid archive identity')
        for candidate in identity['candidates']:
            if not isinstance(candidate, dict) or any(
                    not isinstance(candidate.get(key), str) for key in ('groupId', 'artifactId', 'version')):
                raise ValueError('invalid candidate identity')
    entry_map = {}
    actual_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('archive_id') not in by_id:
            raise ValueError('invalid entry owner')
        index = entry.get('index')
        if type(index) is not int or index < 0 or (entry['archive_id'], index) in entry_map:
            raise ValueError('invalid entry index')
        if not isinstance(entry.get('name'), str) or len(entry['name']) > 65535:
            raise ValueError('invalid entry name')
        if entry.get('sha256') is not None and not _digest(entry['sha256']):
            raise ValueError('invalid entry digest')
        for field in ('size_declared', 'size_actual'):
            if entry.get(field) is not None and (type(entry[field]) is not int or entry[field] < 0):
                raise ValueError('invalid entry size')
        status = entry.get('status')
        if status not in ('hashed', 'recorded', 'name-rejected', 'symlink', 'encrypted',
                          'unsupported-compression', 'depth-limit', 'skipped', 'read-error',
                          'too-large', 'expanded-budget', 'size-mismatch'):
            raise ValueError('invalid entry status')
        size = entry.get('size_actual')
        if size is not None:
            if size > min(limits.max_entry_bytes, limits.max_expanded_bytes) + 1:
                raise ValueError('entry bytes exceed capture bound')
            actual_bytes += size
        if entry.get('sha256') is not None:
            if size is None or size != entry.get('size_declared') or size > limits.max_entry_bytes:
                raise ValueError('complete digest lacks complete bounded bytes')
        if status == 'hashed' and entry.get('sha256') is None:
            # A changed enclosing container deliberately clears its entries'
            # old digests; the affected archive must also lose its digest.
            if by_id[entry['archive_id']]['sha256'] is not None:
                raise ValueError('hashed entry lacks digest')
        if type(entry.get('is_archive')) is not bool:
            raise ValueError('invalid archive-entry flag')
        entry_map[(entry['archive_id'], index)] = entry
    if any(not isinstance(issue, dict) or not isinstance(issue.get('code'), str)
           or issue.get('archive_id') is not None and issue['archive_id'] not in by_id for issue in issues):
        raise ValueError('invalid issue')
    coverage = result['coverage']
    if not isinstance(coverage, dict) or coverage.get('status') not in ('inspected', 'partial', 'rejected'):
        raise ValueError('invalid coverage')
    outer = coverage.get('outer_capture')
    if not isinstance(outer, dict) or outer.get('path') != source_path or type(outer.get('complete')) is not bool:
        raise ValueError('invalid outer identity')
    if outer.get('sha256') is not None and not _digest(outer['sha256']):
        raise ValueError('invalid outer digest')
    if type(coverage.get('entries_recorded')) is not int or coverage['entries_recorded'] != len(entries):
        raise ValueError('invalid recorded count')
    if type(coverage.get('archives_total')) is not int or coverage['archives_total'] != len(containers):
        raise ValueError('invalid container count')
    if type(coverage.get('input_bytes')) is not int or coverage['input_bytes'] != source_size:
        raise ValueError('worker reported different input size')
    if outer['complete'] and outer.get('sha256') != expected_digest:
        raise ValueError('worker reported different input digest')
    expanded = coverage.get('expanded_bytes_used')
    if type(expanded) is not int or not 0 <= expanded <= limits.max_expanded_bytes + 1:
        raise ValueError('invalid expanded byte count')
    root = by_chain.get(())
    if containers and root is None:
        raise ValueError('missing outer root')
    if outer['complete'] and root is None:
        raise ValueError('complete capture lacks outer inventory')
    if root is not None:
        if root['archive_id'] != os.path.basename(source_path) or root['size'] != source_size:
            raise ValueError('outer root not bound to captured source')
        if root['sha256'] != (expected_digest if outer['complete'] else None):
            raise ValueError('outer root digest differs from capture')
        if root['display_path'] != source_path:
            raise ValueError('outer display locator mismatch')
    nested_bytes = 0
    for chain, item in by_chain.items():
        if not chain:
            continue
        parent = by_chain.get(chain[:-1])
        if parent is None:
            raise ValueError('missing parent archive')
        entry = entry_map.get((parent['archive_id'], chain[-1]))
        if entry is None or not entry['is_archive']:
            raise ValueError('nested archive lacks container entry')
        if item['archive_id'] != f"{parent['archive_id']}[{chain[-1]}]":
            raise ValueError('nested id does not match entry chain')
        if item['display_path'] != parent['display_path'] + '!' + entry['name']:
            raise ValueError('nested display locator mismatch')
        if item['size'] != entry['size_actual'] or item['sha256'] != entry['sha256']:
            raise ValueError('nested identity differs from captured entry')
        nested_bytes += item['size']
    # Nested containers are charged again when entered by the reader. For
    # partial reads with an IO exception before a size could be recorded the
    # total may be larger, but never smaller than the recorded evidence.
    measured = actual_bytes + nested_bytes
    if measured > expanded:
        raise ValueError('expanded total below recorded bytes')
    if coverage['status'] == 'inspected':
        if (not outer['complete'] or root is None or issues
                or any(a['inspection_status'] != 'complete' for a in containers)
                or any(e['status'] not in ('hashed', 'recorded') for e in entries)
                or measured != expanded):
            raise ValueError('inconsistent complete inspection')
    counts = {'archives_complete': sum(a['inspection_status'] == 'complete' for a in containers),
              'entries_hashed': sum(e['sha256'] is not None for e in entries),
              'entries_budget_used': len(entries), 'entries_budget_limit': limits.max_entries,
              'expanded_bytes_limit': limits.max_expanded_bytes}
    if any(type(coverage.get(key)) is not int or coverage[key] != value
           for key, value in counts.items()):
        raise ValueError('inconsistent coverage counts or limits')
    if coverage.get('limits') != asdict(limits) or any(
            type(value) is not int for value in coverage['limits'].values()):
        raise ValueError('worker used different limits')
    # Count/string/depth bounds also apply inside identities and other nested
    # provider-controlled metadata, not merely the top-level inventory lists.
    stack = [(result, 0)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if visited > 2_000_000 or depth > 40:
            raise ValueError('inventory structure exceeds bounds')
        if isinstance(value, dict):
            if any(not isinstance(key, str) or len(key) > 65535 for key in value):
                raise ValueError('invalid metadata key')
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str) and len(value) > 65535:
            raise ValueError('metadata string exceeds bound')


def _decode(raw, limits, source_path, expected_digest, source_size):
    envelope = _json(raw)
    if not isinstance(envelope, dict) or set(envelope) != {'schema', 'result', 'error'} or envelope['schema'] != SCHEMA:
        raise ValueError('invalid response envelope')
    error = envelope['error']
    if error is not None:
        if error not in _ERRORS or envelope['result'] is not None:
            raise ValueError('invalid worker error')
        return None, error
    _validate_result(envelope['result'], limits, source_path, expected_digest, source_size)
    return envelope['result'], None


def inspect_isolated(source_fd, *, source_path, sha256, scratch_base, deadline,
                     limits=FileArchiveLimits(), max_response_bytes=MAX_RESPONSE_BYTES,
                     memory_bytes=MAX_MEMORY_BYTES, worker_command=None):
    """Return outcome/result/error and a separate observed cleanup_result.

    outcome complete means the worker exchange completed, not that archive
    inspection had complete coverage. Inspect result.coverage separately.
    Invalid caller arguments raise ArchiveWorkerError before creating a child.
    Process/protocol failures return fixed error codes; BaseException propagates
    with archive_cleanup so the caller can retain scratch when necessary.
    """
    _platform()
    if not _text(source_path) or not _digest(sha256) or not _finite(deadline):
        raise ArchiveWorkerError('invalid source identity or absolute deadline')
    if not isinstance(limits, FileArchiveLimits):
        raise ArchiveWorkerError('limits must be FileArchiveLimits')
    for value, maximum in ((max_response_bytes, MAX_RESPONSE_BYTES), (memory_bytes, MAX_MEMORY_BYTES)):
        if type(value) is not int or not 0 < value <= maximum:
            raise ArchiveWorkerError('worker bounds must be positive integers within hard caps')
    command = [sys.executable, '-B', '-I', '-S', str(_SCRIPT), '--worker'] if worker_command is None else worker_command
    if not isinstance(command, (list, tuple)) or not command or not all(_text(x) for x in command) or not os.path.isabs(command[0]):
        raise ArchiveWorkerError('worker command must be an absolute executable argv')
    path = os.path.abspath(os.fspath(scratch_base))
    if not _text(path):
        raise ArchiveWorkerError('invalid scratch directory')
    source_copy = scratch_fd = None
    proc = None
    cleanup = {'status': 'not-started', 'worker_stopped': True, 'leader_reaped': False,
               'kill_state': None, 'returncode': None, 'assumption': 'trusted-worker-no-descendants'}
    error, result = None, None
    try:
        try:
            info = _source_info(source_fd)
            source_copy = os.dup(source_fd)
            scratch_fd = open_checkout_fd(path)
            scratch = _scratch_info(scratch_fd)
        except (OSError, ValueError) as exc:
            raise ArchiveWorkerError('source or scratch descriptor could not be verified') from exc
        request = {'schema': SCHEMA, 'source_fd': source_copy, 'scratch_fd': scratch_fd,
                   'scratch_base': path, 'scratch_identity': [scratch.st_dev, scratch.st_ino],
                   'source_path': source_path, 'sha256': sha256, 'deadline': deadline,
                   'limits': asdict(limits), 'memory_bytes': memory_bytes,
                   'response_bytes': max_response_bytes}
        raw_request = (json.dumps(request, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw_request) > MAX_REQUEST_BYTES:
            raise ArchiveWorkerError('request metadata exceeds byte cap')
        if time.monotonic() >= deadline:
            error = 'deadline-exceeded'
        else:
            try:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=str(_SCRIPT.parent.parent),
                    env={'PATH': os.defpath, 'HOME': path, 'TMPDIR': path, 'LC_ALL': 'C'},
                    pass_fds=(source_copy, scratch_fd), start_new_session=True)
                cleanup = {'status': 'unconfirmed', 'worker_stopped': False,
                           'leader_reaped': False, 'kill_state': 'unconfirmed',
                           'returncode': None, 'assumption': 'trusted-worker-no-descendants'}
                raw = _exchange(proc, raw_request, deadline, max_response_bytes)
            except _BoundaryFailure as exc:
                error = exc.code
            except OSError:
                error = 'worker-spawn-or-io-failed'
            finally:
                if proc is not None:
                    try:
                        cleanup = _cleanup(proc, _REAP_SECONDS)
                    finally:
                        for stream in (proc.stdin, proc.stdout):
                            if stream is not None:
                                stream.close()
            if not cleanup['worker_stopped']:
                error = 'worker-cleanup-unconfirmed'
            elif error is None and cleanup['returncode'] != 0:
                error = 'worker-failed'
            if error is None:
                try:
                    result, error = _decode(raw, limits, source_path, sha256, info.st_size)
                except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                    error = 'invalid-response'
                if time.monotonic() >= deadline:
                    result, error = None, 'deadline-exceeded'
    except BaseException as exc:
        # The inner finally already cleans a spawned child. Expose the
        # observation without replacing the caller's interrupt or exception.
        try:
            exc.archive_cleanup = cleanup
        except Exception:
            pass
        raise
    finally:
        primary = sys.exc_info()[1]
        close_failure = None
        handles = [scratch_fd, source_copy]
        scratch_fd = source_copy = None
        for index in range(len(handles)):
            fd, handles[index] = handles[index], None
            if fd is None:
                continue
            try:
                os.close(fd)
            except BaseException as exc:
                # close may already have released the fd. Never retry that
                # number, and always attempt the other independent handle.
                if close_failure is None:
                    close_failure = exc
        if close_failure is not None:
            if primary is not None:
                primary.add_note('archive descriptor close could not be confirmed')
            else:
                close_failure.archive_cleanup = cleanup
                raise close_failure
    return {'outcome': 'failed' if error else 'complete', 'error': error,
            'result': result, 'cleanup_result': cleanup,
            'worker_pid': proc.pid if proc is not None else None}


def _set_memory_limit(cap):
    if resource is None or not hasattr(resource, 'RLIMIT_AS'):
        raise _BoundaryFailure('memory-limit-unavailable')
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        target = min(cap, hard) if hard != resource.RLIM_INFINITY else cap
        resource.setrlimit(resource.RLIMIT_AS, (target, target))
    except (OSError, ValueError):
        raise _BoundaryFailure('memory-limit-unavailable') from None


def _emit(envelope, cap):
    chunks, count = [], 0
    for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(',', ':')).iterencode(envelope):
        chunk = part.encode('ascii')
        count += len(chunk)
        if count + 1 > cap:
            raise _BoundaryFailure('response-too-large')
        chunks.append(chunk)
    chunks.append(b'\n')
    sys.stdout.buffer.write(b''.join(chunks))
    sys.stdout.buffer.flush()


def _worker():
    cap = MAX_RESPONSE_BYTES
    try:
        _platform()
        # Establish the hard default before reading even bounded request metadata.
        _set_memory_limit(MAX_MEMORY_BYTES)
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise _BoundaryFailure('invalid-request')
        request = _json(raw)
        expected = {'schema', 'source_fd', 'scratch_fd', 'scratch_base', 'scratch_identity',
                    'source_path', 'sha256', 'deadline', 'limits', 'memory_bytes', 'response_bytes'}
        if not isinstance(request, dict) or set(request) != expected or request['schema'] != SCHEMA:
            raise _BoundaryFailure('invalid-request')
        cap = request['response_bytes']
        memory = request['memory_bytes']
        if type(cap) is not int or not 0 < cap <= MAX_RESPONSE_BYTES or type(memory) is not int or not 0 < memory <= MAX_MEMORY_BYTES:
            raise _BoundaryFailure('invalid-request')
        _set_memory_limit(memory)
        if not _finite(request['deadline']) or not _text(request['source_path']) or not _digest(request['sha256']):
            raise _BoundaryFailure('invalid-request')
        if time.monotonic() >= request['deadline']:
            raise _BoundaryFailure('deadline-exceeded')
        _source_info(request['source_fd'])
        scratch = _scratch_info(request['scratch_fd'])
        if request['scratch_identity'] != [scratch.st_dev, scratch.st_ino]:
            raise _BoundaryFailure('scratch-identity-changed')
        verified = open_checkout_fd(request['scratch_base'])
        try:
            current = _scratch_info(verified)
            if (current.st_dev, current.st_ino) != (scratch.st_dev, scratch.st_ino):
                raise _BoundaryFailure('scratch-identity-changed')
        finally:
            os.close(verified)
        limits = FileArchiveLimits(**request['limits'])
        result = inspect_archive_fd(request['source_fd'], source_path=request['source_path'],
            sha256=request['sha256'], scratch_base=request['scratch_base'],
            deadline=request['deadline'], limits=limits)
        if time.monotonic() >= request['deadline']:
            raise _BoundaryFailure('deadline-exceeded')
        _emit({'schema': SCHEMA, 'result': result, 'error': None}, cap)
        return 0
    except _BoundaryFailure as exc:
        code = exc.code
    except MemoryError:
        code = 'memory-exhausted'
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        code = 'invalid-request'
    except Exception:
        code = 'inspection-failed'
    # Fixed-size failure response uses the hard envelope cap even if a caller
    # requested too little room to encode it; the parent still enforces its cap.
    _emit({'schema': SCHEMA, 'result': None, 'error': code}, MAX_RESPONSE_BYTES)
    return 0


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit(2)
    raise SystemExit(_worker())
