"""Bounded local Git ancestry proofs for imported graph snapshots.

Only full lowercase storage-format commit IDs are accepted; refs, revision
expressions, abbreviations and option-shaped arguments are never resolved.
The installed Git manual documents merge-base --is-ancestor as exit 0 for
ancestor, 1 for not ancestor, other statuses for errors. Both objects are
first verified as local commits. SHA-1 and SHA-256 storage formats are read
from Git, not guessed from HEAD. Missing objects, shallow repositories and
legacy grafts yield unknown relationships, never negative ancestry.

The private gitinspect primitives are reused as a narrow bridge: absolute
trusted executable, sanitized environment, no hooks/fsmonitor/lazy fetch,
bounded process output and group cleanup. No checkout, status, textconv,
filters, fetch, Maven or project code executes here. Replacement refs and
commit-graph acceleration are disabled for these object-graph probes.
One monotonic deadline and aggregate output-byte budget cover all commands.
Filesystem preflight and process creation are not hard-interruptible; this
is not atomic isolation against same-user repository mutation. The default
SIGCHLD disposition is required before every Git call and rechecked before
using its result. Ignored, custom and foreign dispositions are refused:
Popen can otherwise substitute a zero status after kernel auto-reaping.
Concurrent external child reaping, or a disposition changed and restored
inside a Git call, is unsupported; these checks are not atomic isolation
from another reaper. No handler or process-wide signal policy is changed.

The caller supplies one run's snapshot evidence and a fresh run-local proof
ID. The result contains a report-compatible evidence record plus the exact
edge/reference shape consumed by graph_history. It proves local commit
ancestry only, not that a snapshot was actually produced by that commit.
"""

import math
import os
import re
import signal
import tempfile
import time
from datetime import datetime, timezone

from mdi.gitinspect import (
    _GLOBAL_ARGS, _child_env, _decode_stripped, _git_inside_repo,
    _path_hygiene, _require_posix_safety, _resolve_git, _run, _SafetyCapError,
)

_OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_LIMITATIONS = (
    'proofs describe the local Git object graph, not snapshot generation or shipped contents',
    'snapshot and proof IDs must belong to one run, enforced by the caller when persisting',
    'shallow or grafted history is not used for ancestry proofs; no objects are fetched',
    'replacement refs and commit-graph acceleration are disabled',
    'default SIGCHLD and exclusive child reaping are required; concurrent external reaping or transient signal-policy changes are unsupported',
    'checkout path checks and repository observations are not an atomic same-user snapshot',
    'deadline is shared across commands; parent metadata IO and process creation are not hard-interruptible',
)


class AncestryError(ValueError):
    """Invalid caller options or snapshot identifiers, before any Git execution."""


class _Unavailable(Exception):
    def __init__(self, code):
        self.code = code


def _text(value, label):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise AncestryError(f'{label} must be bounded nonempty text without controls')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise AncestryError(f'{label} must be UTF-8 text') from None
    return value


def _inputs(snapshots, evidence_id, max_snapshots):
    _text(evidence_id, 'evidence_id')
    if not isinstance(snapshots, (list, tuple)) or len(snapshots) > max_snapshots:
        raise AncestryError('snapshots must be a bounded sequence')
    rows, seen = [], {evidence_id}
    for record in snapshots:
        if not isinstance(record, dict):
            raise AncestryError('snapshot evidence must be an object')
        identifier = _text(record.get('id'), 'snapshot id')
        if identifier in seen:
            raise AncestryError('duplicate snapshot or proof evidence id')
        seen.add(identifier)
        payload = record.get('payload')
        imported = payload.get('import') if isinstance(payload, dict) else None
        provenance = imported.get('provenance') if isinstance(imported, dict) else None
        if (not isinstance(provenance, dict)
                or imported.get('kind') != 'dependency-tree'):
            raise AncestryError('snapshot must wrap dependency-tree import provenance')
        revision = provenance.get('revision')
        if revision is not None and (not isinstance(revision, str) or not _OID.fullmatch(revision)):
            raise AncestryError('revision must be a full lowercase commit ID or null')
        rows.append({'id': identifier, 'revision': revision})
    return rows


def _check_child_reaping():
    try:
        disposition = signal.getsignal(signal.SIGCHLD)
    except (AttributeError, OSError, ValueError):
        raise _Unavailable('unsupported-child-reaping') from None
    if type(disposition) not in (int, signal.Handlers) or disposition != signal.SIG_DFL:
        raise _Unavailable('unsupported-child-reaping')


class _Git:
    def __init__(self, executable, repo, env, deadline, output_bytes):
        self.executable, self.repo, self.env = executable, repo, env
        self.deadline, self.remaining = deadline, output_bytes
        self.commands = 0

    def run(self, *command):
        _check_child_reaping()
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise _Unavailable('deadline-exceeded')
        if self.remaining <= 0:
            raise _Unavailable('output-budget-exhausted')
        result = _run([self.executable, *_GLOBAL_ARGS, '-c', 'core.commitGraph=false',
                       '--no-replace-objects', *command], cwd=self.repo, env=self.env,
                      timeout_seconds=left, max_output_bytes=self.remaining)
        self.commands += 1
        self.remaining -= len(result.stdout) + len(result.stderr)
        _check_child_reaping()
        if type(result.rc) is not int:
            raise _Unavailable('git-exit-status-unavailable')
        if time.monotonic() >= self.deadline:
            raise _Unavailable('deadline-exceeded')
        if self.remaining < 0 or result.outcome == 'capped':
            raise _Unavailable('output-budget-exhausted')
        if result.outcome != 'ok':
            raise _Unavailable('git-command-unavailable')
        return result

    def value(self, *command):
        result = self.run(*command)
        if result.rc != 0:
            raise _Unavailable('git-command-unavailable')
        return _decode_stripped(result.stdout)


def _history_available(git):
    shallow = git.value('rev-parse', '--is-shallow-repository')
    if shallow == 'true':
        raise _Unavailable('shallow-history')
    if shallow != 'false':
        raise _Unavailable('invalid-git-response')
    grafts = git.value('rev-parse', '--git-path', 'info/grafts')
    if not grafts or '\x00' in grafts:
        raise _Unavailable('invalid-git-response')
    if not os.path.isabs(grafts):
        grafts = os.path.join(git.repo, grafts)
    # Even an empty or symlinked graft file is refused, without opening it.
    if os.path.lexists(grafts):
        raise _Unavailable('grafts-present')


def collect_ancestry(repo, snapshots, *, evidence_id, max_snapshots=128,
                     timeout_seconds=10, max_output_bytes=1024 * 1024):
    """Return {evidence, ancestry, proof_evidence_ids, coverage, issues}.

    Evidence IDs name snapshot records, not commits; multiple snapshots of the
    same commit remain unordered. An unrelated pair is recorded only after
    both directional Git probes exit 1 in a nonshallow local history. Any
    failed probe leaves that pair unknown. Positive edges include the fresh
    caller-chosen proof ID; results never reference unknown supplied IDs.
    """
    if type(max_snapshots) is not int or not 1 <= max_snapshots <= 128:
        raise AncestryError('max_snapshots must be an integer from 1 through 128')
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 8 * 1024 * 1024:
        raise AncestryError('max_output_bytes must be a positive integer at most 8 MiB')
    try:
        finite = type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
    except OverflowError:
        finite = False
    if not finite or not 0 < timeout_seconds <= 3600:
        raise AncestryError('timeout_seconds must be finite, positive and at most 3600')
    rows = _inputs(snapshots, evidence_id, max_snapshots)
    try:
        path = os.path.abspath(_text(os.fspath(repo), 'repo'))
    except TypeError:
        raise AncestryError('repo must be a text path') from None
    deadline = time.monotonic() + timeout_seconds
    observed = datetime.now(timezone.utc).isoformat()
    issues, edges, pairs, commits = [], [], [], {}
    coverage = {'status': 'partial', 'snapshots': len(rows), 'pairs_checked': 0,
                'object_format': None, 'commands': 0, 'output_bytes': 0,
                'limits': {'max_snapshots': max_snapshots, 'timeout_seconds': timeout_seconds,
                           'max_output_bytes': max_output_bytes},
                'limitations': list(_LIMITATIONS)}
    trusted_path = os.environ.get('PATH') or os.defpath
    executable = _resolve_git(trusted_path)
    git = None

    def issue(code, snapshot_id=None):
        item = {'code': code, 'snapshot_id': snapshot_id}
        if item not in issues:
            issues.append(item)

    def invalidate_proofs():
        edges.clear()
        for pair in pairs:
            pair['relation'] = 'unknown'
            pair.pop('before', None)
            pair.pop('after', None)
        coverage['pairs_checked'] = 0

    try:
        _check_child_reaping()
        _require_posix_safety()
        hygiene, _detail = _path_hygiene(path)
        if hygiene or executable is None or _git_inside_repo(executable, path):
            raise _Unavailable('checkout-unavailable')
        with tempfile.TemporaryDirectory(prefix='mdi-ancestry-home-') as home:
            env = _child_env(trusted_path, home)
            env['GIT_NO_REPLACE_OBJECTS'] = '1'
            git = _Git(executable, path, env, deadline, max_output_bytes)
            if git.value('rev-parse', '--show-toplevel') != path:
                raise _Unavailable('checkout-not-root')
            fmt = git.value('rev-parse', '--show-object-format')
            if fmt not in ('sha1', 'sha256'):
                raise _Unavailable('unsupported-object-format')
            coverage['object_format'] = fmt
            _history_available(git)
            width = 40 if fmt == 'sha1' else 64
            for row in rows:
                revision = row['revision']
                if revision is None:
                    issue('revision-unknown', row['id'])
                    continue
                if len(revision) != width:
                    issue('revision-format-mismatch', row['id'])
                    continue
                if revision not in commits:
                    check = git.run('cat-file', '-t', revision)
                    commits[revision] = ('commit' if check.rc == 0 and check.stdout == b'commit\n'
                                         else 'not-a-commit' if check.rc == 0 else 'revision-unavailable')
                if commits[revision] != 'commit':
                    issue(commits[revision], row['id'])
            for index, first in enumerate(rows):
                for second in rows[index + 1:]:
                    pair = {'first': first['id'], 'second': second['id'], 'relation': 'unknown'}
                    pairs.append(pair)
                    a, b = first['revision'], second['revision']
                    if commits.get(a) != 'commit' or commits.get(b) != 'commit':
                        continue
                    if a == b:
                        pair['relation'] = 'same-revision'
                        issue('same-revision-unordered')
                        continue
                    forward = git.run('merge-base', '--is-ancestor', a, b)
                    if forward.stdout or forward.rc not in (0, 1):
                        issue('ancestry-query-unavailable')
                        continue
                    if forward.rc == 0:
                        before, after = first, second
                    else:
                        backward = git.run('merge-base', '--is-ancestor', b, a)
                        if backward.stdout or backward.rc not in (0, 1):
                            issue('ancestry-query-unavailable')
                            continue
                        if backward.rc == 1:
                            pair['relation'] = 'unrelated'
                            coverage['pairs_checked'] += 1
                            continue
                        before, after = second, first
                    pair.update(relation='ancestor', before=before['id'], after=after['id'])
                    edges.append({'before': before['id'], 'after': after['id'],
                                  'evidence_ids': [evidence_id], 'verified': True})
                    coverage['pairs_checked'] += 1
            # A change to shallow/graft policy during collection invalidates all
            # positive proofs; no old observations are silently refreshed.
            _history_available(git)
    except _Unavailable as exc:
        issue(exc.code)
        # An incomplete final policy recheck cannot attest that the collected
        # relationships used the same nonshallow, nongrafted graph throughout.
        invalidate_proofs()
        if git is None or coverage['object_format'] is None:
            coverage['status'] = 'unavailable'
    except (OSError, ValueError, _SafetyCapError):
        issue('local-git-unavailable')
        invalidate_proofs()
        coverage['status'] = 'unavailable' if git is None else 'partial'
    if len(rows) < 2:
        issue('insufficient-snapshots')
    if not issues:
        coverage['status'] = 'complete'
    if git is not None:
        coverage['commands'] = git.commands
        coverage['output_bytes'] = max_output_bytes - max(0, git.remaining)
    record = {'id': evidence_id, 'category': 'git-ancestry', 'source_locator': path,
              'observed_at': observed, 'producer': 'git',
              'payload': {'snapshots': rows, 'pairs': pairs, 'issues': issues,
                          'coverage': coverage, 'tool': {'path': executable}}}
    return {'evidence': [record], 'ancestry': edges, 'proof_evidence_ids': [evidence_id],
            'coverage': coverage, 'issues': issues}
