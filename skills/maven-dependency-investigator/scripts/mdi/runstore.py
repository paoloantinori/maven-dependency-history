"""On-disk run store: create, open, load, and commit MDI run revisions.

A run is a canonical UUID-named child of a caller-supplied existing base
directory, layout mode 0700, holding the marker ``.mdi-run.json``
(``{"format": 1, "run_id": "<uuid>"}``) and the revision-0 report
``report-00000000.json``, both mode 0600 and fsynced when written, with
the base directory fsynced once the child is published. Creation is
exclusive end to end (mkdir of the child, O_EXCL per file), so nothing
existing is ever overwritten, and an unrelated directory is never
adopted: open() accepts a path only when its leaf is a canonical UUID,
its marker validates, and its report chain validates with the same
run_id.

The report chain is the one source of truth: exactly the canonical
``report-NNNNNNNN.json`` names, contiguous from revision 0, at most
MAX_REVISIONS members, where every revision validates as a report, names
the run, holds its own index, and follows its predecessor under
validate_transition. open() validates the chain through latest, and
load(revision) validates it through the requested revision, so history
is never merged across runs and old revisions are immutable files that
no later call rewrites. Directory discovery tolerates exactly three
owned non-report names (the marker, the writer lock, and fresh
``.report-<uuid>.tmp`` temporaries); every other member, including
report-like malformed spellings, rejects the read. Reads ignore the
writer lock entirely, so readers always see either the old or the new
revision of a commit and never a partial file.

commit(draft, expected_revision) advances the run by exactly one
revision under a single-writer lock ``.writer.lock`` created with
O_CREAT|O_EXCL|O_NOFOLLOW at mode 0600 and holding a nonce+pid
ownership record that is bookkeeping, never authorization. The lock
coordinates cooperating writers only: the owner-only directory mode is
no isolation from a same-user adversary that ignores the lock, and the
identity and nonce checks at release are best-effort detection of
interference, not atomic adversarial protection. An existing lock,
including a stale one, means RunConflict: the store neither waits nor
clears it. The lock is acquired before the current generation is read
and released in a finally clause on every path (success, validation
error, injected fault, interrupt); release unlinks the lock only when
its directory identity and nonce are still the ones this call created,
so a lock replaced mid-commit is left untouched and reported as a
conflict. Publication serializes the validated successor with strict
JSON, writes it to an exclusive mode-0600 temporary, fsyncs the
temporary, and publishes it with os.link under the already-safe
directory descriptor with follow_symlinks disabled, so a destination
revision can never be overwritten; the directory is fsynced after the
link, and only a successful return means the new revision is durable.
A failure before the link leaves the previous revision current; cleanup
of the temporary is best-effort and applies only when this call can
still identify the file as its own by inode identity, so an interrupt
during the write, or a temporary whose ownership could not be
established, leaves an orphan that discovery tolerates and no later
call overwrites. A failure after the link raises CommitUncertain
carrying the published revision: the link is visible and the file
content was fsynced, but because a post-link step such as the directory
sync failed, power-loss survival of the directory entry is unknown; the
caller must re-read and reconcile, assuming neither rollback nor
durability, and the store neither deletes the report nor retries on
its own.

Every directory path, including base_dir at create time, is resolved by
walking the lexical absolute path one component at a time from the
filesystem root with O_DIRECTORY|O_NOFOLLOW; symlinked ancestors, a
symlinked base, a symlinked run directory, and symlinked or non-regular
marker/report files are all rejected, and ``..`` is normalized lexically
by abspath(), never resolved against symlinks. Member reads open with
O_NONBLOCK so a FIFO or device cannot block the open, are capped at
MAX_READ_BYTES, and reject duplicate JSON keys and non-finite JSON
constants. Marker inspection proves layout ownership, not adversary
authenticity.

Failure policy: create() never deletes anything. Revision 0 is
validated, serialized, and size-gated against MAX_READ_BYTES before the
child directory is created, so an invalid query or a revision too large
to ever read back creates nothing at all. Once the child mkdir
succeeds the call owns a fresh directory, but without locks its members
could already have been replaced by another actor by the time a failure
is noticed, and after an ambiguous durability failure deletion could
destroy committed data, so a failed create leaves its partial run in
place instead of rolling it back. The partial is identifiable as the
UUID-named child of the requested base with missing or invalid members;
recognizing and collecting partial runs belongs to a later slice.
Control-flow exceptions (KeyboardInterrupt, SystemExit) are never wrapped:
they propagate after the open descriptors are closed and the lock is
released, and the partial run stays. open() and load() never write, and
instances hold no file descriptors: every method opens and closes what
it needs.
"""

import copy
import json
import os
import re
import stat
import uuid

from mdi.report import ReportError, new_report, validate_report, validate_transition

MARKER_NAME = ".mdi-run.json"
REPORT_NAME = "report-00000000.json"
LOCK_NAME = ".writer.lock"
MAX_READ_BYTES = 32 * 1024 * 1024
MAX_REVISIONS = 1024
RUN_DIR_MODE = 0o700
FILE_MODE = 0o600

_REPORT_NAME_RE = re.compile(r"report-([0-9]{8})\.json")
_RAW_NAME_RE = re.compile(
    r"raw-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.bin"
)
_TEMP_NAME_RE = re.compile(
    r"\.report-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.tmp"
)


class RunStoreError(Exception):
    """Raised when a run cannot be created, opened, loaded, or committed safely."""


class RunConflict(RunStoreError):
    """Raised when another writer, a stale lock, or a destination blocks a commit."""


class CommitUncertain(RunStoreError):
    """Raised when a revision was published but a post-publication step failed.

    ``published_revision`` holds the revision whose link is visible and
    whose file content was fsynced. Because a post-link step such as the
    directory sync failed, power-loss survival of the directory entry is
    unknown: re-read the store to reconcile the outcome and assume
    neither rollback nor durability. The store never deletes the
    published report or retries by itself.
    """

    def __init__(self, message, published_revision):
        super().__init__(message)
        self.published_revision = published_revision


def _require_primitives():
    """Fail closed unless every flag and dir_fd capability relied on is present."""
    for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_EXCL"):
        if not getattr(os, name, 0):
            raise RunStoreError(f"os.{name} is unavailable on this platform")
    for func in (os.open, os.mkdir, os.link, os.unlink, os.lstat):
        if func not in os.supports_dir_fd:
            raise RunStoreError(f"os.{func.__name__} does not support dir_fd on this platform")
    if os.listdir not in os.supports_fd:
        raise RunStoreError("os.listdir does not accept a directory descriptor on this platform")
    if os.link not in os.supports_follow_symlinks:
        raise RunStoreError("os.link does not support follow_symlinks on this platform")


def _lexical_abs(path):
    try:
        text = os.fspath(path)
    except TypeError as exc:
        raise RunStoreError(f"path must be str or os.PathLike, got {type(path).__name__}") from exc
    if isinstance(text, bytes):
        raise RunStoreError("path must be text, not bytes")
    return os.path.abspath(text)


def _is_canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _open_dir_nofollow(abs_path):
    """Open abs_path by walking from the root, refusing every symlink component."""
    try:
        fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RunStoreError(f"cannot open filesystem root: {exc}") from exc
    walked = ""
    try:
        for part in abs_path.split(os.sep):
            if not part:
                continue
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise RunStoreError(f"cannot open run path component {walked + part!r}: {exc}") from exc
            os.close(fd)
            fd = nxt
            walked = walked + os.sep + part
        return fd
    except BaseException:
        os.close(fd)
        raise


def _reject_constant(token):
    raise RunStoreError(f"non-finite JSON constant {token!r}")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RunStoreError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json(dir_fd, name):
    """Read one member of dir_fd: non-symlink, regular, nonblocking, size-bounded, strict JSON."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise RunStoreError(f"cannot open {name}: {exc}") from exc
    try:
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RunStoreError(f"{name} is not a regular file")
            limit = MAX_READ_BYTES
            if info.st_size > limit:
                raise RunStoreError(f"{name} exceeds the {limit} byte read limit")
            data = b""
            while len(data) <= limit:
                chunk = os.read(fd, min(1 << 16, limit + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) > limit:
                raise RunStoreError(f"{name} exceeds the {limit} byte read limit")
        except OSError as exc:
            raise RunStoreError(f"cannot read {name}: {exc}") from exc
    finally:
        os.close(fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunStoreError(f"{name} is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
    except RunStoreError:
        raise
    except ValueError as exc:
        raise RunStoreError(f"{name} is not valid JSON: {exc}") from exc


def _validate_marker(marker, run_id):
    if not isinstance(marker, dict) or set(marker) != {"format", "run_id"}:
        raise RunStoreError("marker must hold exactly format and run_id")
    if type(marker["format"]) is not int or marker["format"] != 1:
        raise RunStoreError("marker format must be 1")
    if not _is_canonical_uuid(marker["run_id"]) or marker["run_id"] != run_id:
        raise RunStoreError("marker run_id does not match the run directory")


def _report_name(revision):
    return f"report-{revision:08d}.json"


def _read_report(dir_fd, revision, run_id):
    """Read and validate the report occupying one chain slot; return it fresh."""
    name = _report_name(revision)
    report = _read_json(dir_fd, name)
    try:
        validate_report(report)
    except ReportError as exc:
        raise RunStoreError(f"{name} is not a valid report: {exc}") from exc
    if report["revision"] != revision:
        raise RunStoreError(f"{name} must hold revision {revision}, got {report['revision']!r}")
    if report["run_id"] != run_id:
        raise RunStoreError(f"{name} run_id does not match the run")
    return report


def _started_audit(record):
    audit = record['audit'].get('started')
    return (isinstance(audit, dict) and set(audit) == {'at', 'approved_by', 'reference'}
            and all(isinstance(value, str) and value for value in audit.values()))


def _raw_authorizations(report, consumed_at):
    """Bind raw names to unique, actually paired consumption transitions."""
    from mdi.raw_capture_consent import validate_capture_record
    by_id = {op['id']: op for op in report['operations']}
    authorized = {}
    sources = set()
    for op in report['operations']:
        if op['scope'].get('kind') != 'raw-capture':
            continue
        try:
            validate_capture_record(op)
        except Exception as exc:
            raise RunStoreError('invalid raw capture registration') from exc
        scope = op['scope']
        name, source_id = scope['output_name'], scope['source_operation_id']
        if name in authorized or source_id in sources:
            raise RunStoreError('raw capture registration is not unique')
        sources.add(source_id)
        source = by_id.get(source_id)
        revision = consumed_at.get(op['id'])
        paired = (source is not None and source['scope'].get('kind') == scope['source_kind']
                  and _started_audit(op) and _started_audit(source)
                  and op['state'] in ('consumed', 'finished')
                  and source['state'] in ('consumed', 'finished')
                  and revision is not None and consumed_at.get(source_id) == revision)
        authorized[name] = (op, source, revision) if paired else None
    return authorized


def _check_raw_members(dir_fd, raw_names, report, consumed_at):
    """Judge exactly the raw names of one directory snapshot; a file created
    after the snapshot is invisible here and judged by the next read, so a
    concurrent authorized capture can never be rejected against a stale
    revision view."""
    authorized = _raw_authorizations(report, consumed_at)
    for name in raw_names:
        binding = authorized.get(name)
        if binding is None:
            raise RunStoreError('raw file lacks registered paired consumption')
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise RunStoreError('raw file identity could not be inspected') from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != FILE_MODE or info.st_nlink != 1
                or info.st_size > binding[0]['scope']['max_bytes']):
            raise RunStoreError('raw file violates ownership, mode or capture bound')
    return authorized


def _scan_names(names):
    """Validate one directory snapshot; return (latest revision, raw names).

    Revisions, unexpected members and raw membership all derive from this
    single listing, so no read can mix two directory views and judge a raw
    file against a stale revision range.
    """
    revisions = set()
    raw_names = []
    for name in names:
        if name in (MARKER_NAME, LOCK_NAME):
            continue
        match = _REPORT_NAME_RE.fullmatch(name)
        if match is not None:
            revisions.add(int(match.group(1)))
            continue
        if _TEMP_NAME_RE.fullmatch(name) is not None:
            continue
        if _RAW_NAME_RE.fullmatch(name) is not None:
            raw_names.append(name)
            continue
        raise RunStoreError(
            f"unexpected run member {name!r}; expected {MARKER_NAME!r}, canonical "
            f"report-NNNNNNNN.json files, or an owned lock/temporary name"
        )
    if not revisions:
        raise RunStoreError(f"the run holds no {REPORT_NAME}")
    latest = max(revisions)
    if len(revisions) != latest + 1:
        raise RunStoreError(
            f"report revisions are not contiguous from 0 to {latest}: found {sorted(revisions)}"
        )
    if latest >= MAX_REVISIONS:
        raise RunStoreError(f"latest revision {latest} meets the bound of {MAX_REVISIONS} revisions")
    return latest, raw_names


def _list_names(dir_fd):
    try:
        return os.listdir(dir_fd)
    except OSError as exc:
        raise RunStoreError(f"cannot list the run directory: {exc}") from exc


def _discover_latest(dir_fd):
    """Return the latest revision of the canonical contiguous report chain."""
    latest, _raw = _scan_names(_list_names(dir_fd))
    return latest


def _read_chain(dir_fd, run_id, revision=None, *, raw_bindings=None):
    """Validate the marker and the chain through the requested revision; return it.

    Every revision is checked for schema, run_id, and its own index, and
    each consecutive pair must pass validate_transition, so no foreign or
    reordered snapshot can enter the chain and no run merges with another.
    When raw artifacts exist, validate through the latest revision even for
    a historical read: later paired consent authorizes those artifacts, not
    the historical snapshot returned to the caller. One directory snapshot
    decides both the revision chain and raw membership, so a capture file
    created mid-read is never judged against a stale revision view.
    """
    latest, raw_names = _scan_names(_list_names(dir_fd))
    _validate_marker(_read_json(dir_fd, MARKER_NAME), run_id)
    if revision is None:
        target = latest
    else:
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise RunStoreError(f"revision must be an int, not bool, got {revision!r}")
        if revision < 0:
            raise RunStoreError(f"revision must not be negative, got {revision!r}")
        if revision > latest:
            raise RunStoreError(f"revision {revision} does not exist; latest is {latest}")
        target = revision
    through = latest if raw_names or raw_bindings is not None else target
    report = _read_report(dir_fd, 0, run_id)
    selected = report if target == 0 else None
    consumed_at = {}
    for index in range(1, through + 1):
        successor = _read_report(dir_fd, index, run_id)
        try:
            validate_transition(report, successor)
        except ReportError as exc:
            raise RunStoreError(
                f"{_report_name(index)} does not follow {_report_name(index - 1)}: {exc}"
            ) from exc
        previous_states = {op['id']: op['state'] for op in report['operations']}
        for op in successor['operations']:
            if (previous_states.get(op['id']) == 'proposed' and op['state'] == 'consumed'
                    and _started_audit(op)):
                consumed_at[op['id']] = index
        report = successor
        if index == target:
            selected = report
    bindings = _check_raw_members(dir_fd, raw_names, report, consumed_at)
    if raw_bindings is not None:
        raw_bindings.update(bindings)
    return selected


def _dumps(payload):
    return (json.dumps(payload, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _sync_write(fd, data):
    """Write one payload to an already-open exclusive file: chmod, full write, fsync.

    Ownership identity capture and descriptor lifetimes stay with the
    callers; only the byte-level write sequence is shared.
    """
    os.fchmod(fd, FILE_MODE)
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])
    os.fsync(fd)


def _write_new_file(dir_fd, name, data):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE, dir_fd=dir_fd)
    try:
        _sync_write(fd, data)
    finally:
        os.close(fd)


def _fd_identity(info):
    return (info.st_dev, info.st_ino)


def _acquire_writer_lock(dir_fd):
    """Create the single-writer lock; return (fd, (identity, nonce)).

    An existing lock, live or stale, is RunConflict: the store neither
    waits nor clears it. The nonce+pid record inside the lock is
    bookkeeping for release, never authorization.
    """
    nonce = str(uuid.uuid4())
    record = (json.dumps({"nonce": nonce, "pid": os.getpid()}, sort_keys=True) + "\n").encode("utf-8")
    try:
        lock_fd = os.open(
            LOCK_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE, dir_fd=dir_fd
        )
    except FileExistsError as exc:
        raise RunConflict(
            f"writer lock {LOCK_NAME!r} exists; another writer may be active or the lock "
            f"is stale: not waiting and not clearing it"
        ) from exc
    except OSError as exc:
        raise RunStoreError(f"cannot create writer lock {LOCK_NAME!r}: {exc}") from exc
    identity = None
    try:
        info = os.fstat(lock_fd)
        identity = _fd_identity(info)
        _sync_write(lock_fd, record)
    except BaseException:
        os.close(lock_fd)
        if identity is not None:
            _unlink_if_own(dir_fd, LOCK_NAME, identity)
        raise
    return lock_fd, (identity, nonce)


def _release_writer_lock(dir_fd, held):
    """Unlink the lock only if it is still exactly the file this call created.

    Returns None on success or a problem message when the lock went away
    or was replaced; a replacement is never touched or unlinked.
    """
    identity, nonce = held
    try:
        info = os.lstat(LOCK_NAME, dir_fd=dir_fd)
    except FileNotFoundError:
        return f"writer lock {LOCK_NAME!r} disappeared before release"
    except OSError as exc:
        return f"cannot inspect writer lock {LOCK_NAME!r} for release: {exc}"
    if _fd_identity(info) != identity:
        return f"writer lock {LOCK_NAME!r} was replaced during the commit and is left untouched"
    try:
        record = _read_json(dir_fd, LOCK_NAME)
    except RunStoreError as exc:
        return f"writer lock {LOCK_NAME!r} no longer holds our ownership record: {exc}"
    if not isinstance(record, dict) or record.get("nonce") != nonce:
        return f"writer lock {LOCK_NAME!r} no longer holds our ownership nonce and is left untouched"
    try:
        os.unlink(LOCK_NAME, dir_fd=dir_fd)
    except OSError as exc:
        return f"cannot remove writer lock {LOCK_NAME!r}: {exc}"
    return None


def _unlink_if_own(dir_fd, name, identity):
    """Remove one of this call's temporary files, and only that exact inode."""
    if identity is None:
        return
    try:
        info = os.lstat(name, dir_fd=dir_fd)
    except OSError:
        return
    if _fd_identity(info) != identity:
        return
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _publish(dir_fd, run_id, draft, expected_revision):
    """Validate and atomically publish the successor revision; return its deepcopy.

    Called only while the writer lock is held. The draft must sit at
    expected_revision, which must still be the latest on disk; storage
    advances the revision exactly once. Failures before the link leave
    the previous revision current, cleaning the temporary best-effort
    only when it is still identifiable as this call's own by inode
    identity; failures after the link raise CommitUncertain with the
    revision whose link is already visible.
    """
    latest_report = _read_chain(dir_fd, run_id)
    latest = latest_report["revision"]
    if latest != expected_revision:
        raise RunConflict(
            f"stale expected revision {expected_revision}; the run already holds revision {latest}"
        )
    try:
        validate_report(draft)
    except ReportError as exc:
        raise RunStoreError(f"draft is not a valid report: {exc}") from exc
    if draft["revision"] != expected_revision:
        raise RunStoreError(
            f"draft revision {draft['revision']!r} must equal expected_revision "
            f"{expected_revision!r}; storage advances the revision exactly once"
        )
    next_revision = expected_revision + 1
    if next_revision >= MAX_REVISIONS:
        raise RunStoreError(
            f"cannot store revision {next_revision}; the run is bounded to {MAX_REVISIONS} revisions"
        )
    persisted = copy.deepcopy(draft)
    persisted["revision"] = next_revision
    try:
        validate_transition(latest_report, persisted)
    except ReportError as exc:
        raise RunStoreError(f"draft is not a legal successor of revision {latest}: {exc}") from exc
    _raw_authorizations(persisted, {})
    data = _dumps(persisted)
    if len(data) > MAX_READ_BYTES:
        raise RunStoreError(
            f"serialized revision {next_revision} exceeds the {MAX_READ_BYTES} byte read limit"
        )
    temp_name = f".report-{uuid.uuid4()}.tmp"
    destination = _report_name(next_revision)
    identity = None
    published = False
    try:
        temp_fd = os.open(
            temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE, dir_fd=dir_fd
        )
        try:
            identity = _fd_identity(os.fstat(temp_fd))
            _sync_write(temp_fd, data)
        finally:
            os.close(temp_fd)
        os.link(temp_name, destination, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        published = True
        os.fsync(dir_fd)
    except FileExistsError as exc:
        _unlink_if_own(dir_fd, temp_name, identity)
        raise RunConflict(
            f"destination {destination!r} already exists; nothing was overwritten"
        ) from exc
    except OSError as exc:
        if published:
            _unlink_if_own(dir_fd, temp_name, identity)
            raise CommitUncertain(
                f"revision {next_revision} was published but a post-publication step failed: {exc}",
                next_revision,
            ) from exc
        _unlink_if_own(dir_fd, temp_name, identity)
        raise RunStoreError(f"cannot publish revision {next_revision}: {exc}") from exc
    _unlink_if_own(dir_fd, temp_name, identity)
    return persisted


class RunStore:
    """A located run directory; instances carry no open file descriptors."""

    def __init__(self, path, run_id):
        self.path = path
        self.run_id = run_id

    @classmethod
    def create(cls, base_dir, query=None):
        """Create a fresh run under the existing directory base_dir and return it.

        Revision 0 is validated, serialized, and size-gated against
        MAX_READ_BYTES before the child directory is created, so a
        rejected request creates nothing at all and nothing existing is
        ever overwritten.
        """
        _require_primitives()
        base = _lexical_abs(base_dir)
        base_fd = _open_dir_nofollow(base)
        run_id = str(uuid.uuid4())
        try:
            try:
                data = _dumps(new_report(run_id, query=query))
                if len(data) > MAX_READ_BYTES:
                    raise RunStoreError(
                        f"serialized revision 0 exceeds the {MAX_READ_BYTES} byte read limit"
                    )
            except RunStoreError:
                raise
            except Exception as exc:
                raise RunStoreError(f"cannot prepare revision 0 for run {run_id!r}: {exc}") from exc
            try:
                os.mkdir(run_id, RUN_DIR_MODE, dir_fd=base_fd)
            except OSError as exc:
                raise RunStoreError(f"cannot create run directory {run_id!r}: {exc}") from exc
            try:
                child_fd = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
                try:
                    os.fchmod(child_fd, RUN_DIR_MODE)
                    _write_new_file(child_fd, MARKER_NAME, _dumps({"format": 1, "run_id": run_id}))
                    _write_new_file(child_fd, REPORT_NAME, data)
                    os.fsync(child_fd)
                finally:
                    os.close(child_fd)
                os.fsync(base_fd)
            except Exception as exc:
                raise RunStoreError(
                    f"create failed for run {run_id!r}; its partial run is left in place "
                    f"for identification at {os.path.join(base, run_id)!r}"
                ) from exc
        finally:
            os.close(base_fd)
        return cls(os.path.join(base, run_id), run_id)

    @classmethod
    def open(cls, run_dir):
        """Validate an existing run directory and return its store; writes nothing.

        The marker, the canonical report chain, and every transition
        through the latest revision must validate for the same run_id.
        """
        _require_primitives()
        path = _lexical_abs(run_dir)
        run_id = path.rstrip(os.sep).rsplit(os.sep, 1)[-1]
        if not _is_canonical_uuid(run_id):
            raise RunStoreError(f"run directory leaf {run_id!r} is not a canonical UUID")
        fd = _open_dir_nofollow(path)
        try:
            _read_chain(fd, run_id)
        finally:
            os.close(fd)
        return cls(path, run_id)

    def load(self, revision=None):
        """Re-read and validate the chain; return the latest or an explicit revision.

        Every call independently checks discovery, the marker, each
        report's schema, run_id, index, and the consecutive transitions
        up to the requested revision, so old snapshots are only ever
        read, never re-derived or merged.
        """
        _require_primitives()
        fd = _open_dir_nofollow(self.path)
        try:
            return _read_chain(fd, self.run_id, revision)
        finally:
            os.close(fd)

    def commit(self, draft, expected_revision):
        """Advance the run by exactly one revision; return the persisted report.

        The draft must be a valid report sitting at expected_revision,
        which must still be the latest revision on disk when the writer
        lock is held; the successor that storage publishes carries
        expected_revision + 1 and must pass validate_transition against
        the current latest before anything is written. The lock is
        acquired before the generation is read and released on every
        exit path; a replaced lock is left intact and reported as a
        conflict after the (already durable) publication. Interrupts
        propagate unwrapped; one arriving between the temporary's
        creation and the link can leave that orphan temporary in place,
        which discovery tolerates and no later commit overwrites. A
        post-publication failure raises CommitUncertain rather than
        pretending nothing was committed.
        """
        _require_primitives()
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise RunStoreError(
                f"expected_revision must be an int, not bool, got {expected_revision!r}"
            )
        if expected_revision < 0:
            raise RunStoreError(f"expected_revision must not be negative, got {expected_revision!r}")
        if not isinstance(draft, dict):
            raise RunStoreError(f"draft must be a JSON object, got {type(draft).__name__}")
        dir_fd = _open_dir_nofollow(self.path)
        lock_fd = None
        held = None
        try:
            lock_fd, held = _acquire_writer_lock(dir_fd)
            result = _publish(dir_fd, self.run_id, draft, expected_revision)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            problem = _release_writer_lock(dir_fd, held) if held is not None else None
            os.close(dir_fd)
        if problem is not None:
            raise RunConflict(
                f"{problem}; revision {result['revision']} was already published durably"
            )
        return result
