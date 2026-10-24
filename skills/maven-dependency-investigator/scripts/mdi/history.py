"""Bounded local history analysis of one dependency's POM declarations.

inspect_history(repo, query, pom_paths=...) walks the first-parent history of
the checkout HEAD, restricted to the caller-supplied relative POM paths (never
a repository-wide scan), lists the commits that touched those paths, and reads
the full POM blob of each touching commit and of its first parent: actual
snapshot objects, never diff fragments. Declarations are extracted with the
namespace-exact walker from mdi.discover, matched with the g:a[:v] query
semantics from mdi.candidates (known strings only; raw ${...} placeholder
versions are preserved as written). Changes are compared per declaration
identity (the raw groupId/artifactId plus realm kind, management context,
profile locator, type and classifier), so artifacts that share a bare-query
substring never collide; each event names its actual changed subject and is
classified as addition, removal, reintroduction, version-change,
property-change, management-change or declaration-change (a raw scope or
optional value change with the version unchanged; no defaults are resolved
and no usage is claimed). Indistinguishable duplicate
declarations are compared as value multisets, reported with an explicit
ambiguity marker, and never paired arbitrarily.
Optional introducing dependencies add candidate-introduction events: leads for
transitive queries, never a resolved or shipped fact. Every pathspec-bearing
command runs with --literal-pathspecs, so POM paths containing glob
characters are matched as literal filenames and a requested scan can never
broaden into a pattern match.

Safety: the checkout root is confirmed with mdi.gitinspect.checkout_context
(a partial context is acceptable; history needs no working-tree status, and
raw blob reads run no clean filters). Every git invocation reuses that
module's proven bounded runner: absolute trusted git outside the repo, a fresh
allowlist environment, optional locks, lazy fetch, fsmonitor, hooks,
attributes, gc and submodule recursion disabled, plus GIT_NO_REPLACE_OBJECTS
so replaced objects cannot rewrite the observed history. Output is bounded
while read. Timing has two separately bounded phases: the context phase
(checkout_context, each of its commands capped at timeout_seconds) and the
analysis phase (one aggregate timeout_seconds deadline shared by every
history command); the whole call is therefore not bounded by a single
timeout. Commit ids and blob oids are validated as hex before use; a missing
POM path is distinguished from an error by an explicit ls-tree enumeration (an
exit-128 object read is never read as absence, and a failed HEAD enumeration
is reported as an issue, never silently suppressed). The root commit's
pre-history state is empty by definition, while a shallow graft boundary has
unknown parentage and stays unclassified with an issue; neither truncation nor
shallowness lets the oldest observed event pose as the first ever. Analyses
are read-only: no config change, network fetch, Maven resolution, or
working-tree or index writes.

Private-helper bridges (deliberate and documented): this module imports
mdi.gitinspect._run/_GLOBAL_ARGS/_child_env/_resolve_git/_first_line/
_positive_int, mdi.discover._extract_project/_root_problem and
mdi.candidates._parse_query/_coords_match instead of duplicating bounded
execution, extraction or query semantics; exposing small public wrappers is
deferred to a later shared task. No gitinspect, discover or candidates file is
modified by this slice, and tests pin the bridged behavior.
"""

import os
import re
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone

from mdi.candidates import _coords_match, _parse_query
from mdi.discover import _extract_project, _root_problem
from mdi.gitinspect import (
    _GLOBAL_ARGS,
    _child_env,
    _first_line,
    _positive_int,
    _resolve_git,
    _run,
    checkout_context,
)
from mdi.limits import Limits
from mdi.xmlsafe import UnsafeXML, parse_xml

DEFAULT_MAX_COMMITS = 64
DEFAULT_MAX_OUTPUT_BYTES = 1 << 20
DEFAULT_TIMEOUT_SECONDS = 10

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]*)\}")
_LOG_FORMAT = "%H%n%P%n%an%n%ae%n%cn%n%ce%n%aI%n%cI%n%s"
_MATCHED_KINDS = frozenset({"dependency", "managed-dependency", "bom-import"})
_RAW_DECL_FIELDS = ("scope", "type", "classifier", "optional")
_TEXT_CAP = 200

_LAST_MOD_NOTE = (
    "latest observed change within the analyzed window; not a git blame and not an "
    "original-introduction claim; with truncated or shallow history the oldest observed "
    "event is not necessarily the first ever"
)
_CANDIDATE_NOTE = (
    "introducing dependency changed in the analyzed POM paths while the queried dependency "
    "is not named there; candidate lead only, not a resolved or shipped fact"
)
_LIMITATIONS = (
    "first-parent linear history from HEAD only; commits reachable only through merge second parents are attributed to the merge commit against its first parent, not individually",
    "declarations only: no Maven resolution, property interpolation, profile activation, inheritance or packaging; raw ${...} placeholder versions are preserved as written",
    "candidate-introduction events are leads from explicitly supplied introducing dependencies only; never a resolved or shipped fact",
    "events are per declaration identity (raw groupId/artifactId plus realm, management, profile, type, classifier); indistinguishable duplicates are compared as value multisets and flagged ambiguous, never silently paired or dropped",
    "reintroduction detection is scoped per POM path: an identity first seen in another queried module does not turn a later first addition here into a reintroduction",
    "last_modification reports the latest observed change within the analyzed window; it is not a git blame and not an original-introduction claim",
    "when truncated or shallow, the oldest observed event is not necessarily the first ever",
    "non-version declaration values (scope, optional) are raw POM text with no default resolution; a change in them alone classifies as declaration-change, while a change combined with a version change reports the version-bearing kind with both raw field sets visible",
    "type and classifier are part of declaration identity, so changing either replaces one declared subject with another and reports a removal plus an addition of the two distinct identities",
    "profile-scoped moves of an otherwise unchanged declaration are not classified as events",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_text(data: bytes) -> str:
    """Decode one protocol field exactly, then keep it UTF-8 JSON safe.

    surrogateescape preserves the observed bytes; lone surrogates (non-UTF-8
    identities in hostile repositories) are replaced with '?' so downstream
    utf-8 encoders cannot fail. The result is bounded to _TEXT_CAP characters.
    """
    text = data.decode("utf-8", errors="surrogateescape")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", errors="replace").decode("utf-8")
    return text[:_TEXT_CAP]


class _BudgetExhausted(RuntimeError):
    """Raised when the aggregate deadline leaves no budget for another command."""


class _Runner:
    """Bounded git runner: absolute trusted git, sanitized env, one aggregate
    wall-clock deadline and one output cap shared by every invocation.
    --literal-pathspecs makes every pathspec (log and ls-tree alike) match
    byte-for-byte, so glob characters in POM path inputs stay literal
    filenames and a requested scan can never broaden into a pattern match."""

    def __init__(self, git_path, cwd, env, timeout_seconds, max_output_bytes):
        self.git_path = git_path
        self.cwd = cwd
        self.env = env
        self.max_output_bytes = max_output_bytes
        self.deadline = time.monotonic() + timeout_seconds

    def run(self, command):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise _BudgetExhausted("aggregate timeout budget exhausted")
        return _run([self.git_path, *_GLOBAL_ARGS, "--literal-pathspecs", *command],
                    cwd=self.cwd, env=self.env,
                    timeout_seconds=left, max_output_bytes=self.max_output_bytes)


def _validate_pom_paths(pom_paths) -> tuple:
    """Relative POSIX POM paths only: plain components, no escapes, no magic."""
    if isinstance(pom_paths, (str, bytes)):
        raise TypeError("pom_paths must be a sequence of relative path strings, not one path")
    paths = []
    for raw in pom_paths:
        if not isinstance(raw, str):
            raise TypeError(f"pom path must be str, got {type(raw).__name__}")
        if not raw or raw.startswith(("/", ":")) or "\x00" in raw:
            raise ValueError(f"pom path {raw!r} must be a nonempty relative POSIX path")
        for part in raw.split("/"):
            if part in ("", ".", ".."):
                raise ValueError(f"pom path {raw!r} must not contain empty, '.' or '..' components")
        if raw not in paths:
            paths.append(raw)
    if not paths:
        raise ValueError("pom_paths must name at least one relative POM path")
    return tuple(paths)


def _parse_introducing(introducing_dependencies) -> list:
    if isinstance(introducing_dependencies, (str, bytes)):
        raise TypeError("introducing_dependencies must be a sequence of query strings")
    parsed = []
    for item in introducing_dependencies:
        if not isinstance(item, str):
            raise TypeError(f"introducing query must be str, got {type(item).__name__}")
        parsed.append(_parse_query(item))
    return parsed


class _ProtocolError(RuntimeError):
    """Raised when git log output does not match the pinned field protocol."""


def _parse_log(data: bytes) -> list:
    """Parse the -z nine-field log protocol; every id is validated hex."""
    entries = []
    for chunk in data.split(b"\0"):
        if not chunk:
            continue
        fields = chunk.split(b"\n")
        if len(fields) != 9:
            raise _ProtocolError(f"log entry carries {len(fields)} fields, expected 9")
        commit_id = _json_text(fields[0])
        if not _SHA_RE.fullmatch(commit_id):
            raise _ProtocolError(f"commit id has unexpected shape: {commit_id[:_TEXT_CAP]!r}")
        first_parent = None
        if fields[1]:
            candidate = _json_text(fields[1]).split(" ")[0]
            if not _SHA_RE.fullmatch(candidate):
                raise _ProtocolError(f"parent id has unexpected shape: {candidate[:_TEXT_CAP]!r}")
            first_parent = candidate
        entries.append({
            "id": commit_id,
            "first_parent": first_parent,
            "author": {"name": _json_text(fields[2]), "email": _json_text(fields[3])},
            "committer": {"name": _json_text(fields[4]), "email": _json_text(fields[5])},
            "author_time": _json_text(fields[6]),
            "commit_time": _json_text(fields[7]),
            "subject": _json_text(fields[8]),
        })
    return entries


def _summary(entry) -> dict:
    return {
        "id": entry["id"],
        "subject": entry["subject"],
        "author": dict(entry["author"]),
        "committer": dict(entry["committer"]),
        "author_time": entry["author_time"],
        "commit_time": entry["commit_time"],
    }


def _matched_rows(query, snapshot) -> list:
    """Matched declaration rows in one snapshot (absent: []). Each row keeps
    its full subject identity (raw groupId/artifactId plus realm kind,
    management context, profile locator, type and classifier, exactly as
    written, with no default resolution) separate from its mutable values
    (raw version, scope, optional), so different artifacts matched by one bare
    query never collide and non-version changes stay observable."""
    if snapshot["status"] != "read":
        return []
    rows = []
    for declaration in snapshot["declarations"]:
        if declaration["kind"] not in _MATCHED_KINDS:
            continue
        coordinates = declaration["coordinates"]
        if not _coords_match(query, coordinates):
            continue
        profile = declaration["profile"]
        rows.append({
            "identity": {
                "group_id": coordinates.get("groupId"),
                "artifact_id": coordinates.get("artifactId"),
                "kind": declaration["kind"],
                "management_only": bool(declaration["management_only"]),
                "profile": dict(profile) if profile else None,
                "type": coordinates.get("type"),
                "classifier": coordinates.get("classifier"),
            },
            "values": {
                "version": coordinates.get("version") or "",
                "scope": coordinates.get("scope"),
                "optional": coordinates.get("optional"),
            },
        })
    return rows


def _identity_key(row) -> tuple:
    """Hashable identity for grouping; profile flattens to its locator."""
    identity = row["identity"]
    profile = identity["profile"] or {}
    return (identity["group_id"], identity["artifact_id"], identity["kind"],
            identity["management_only"], profile.get("id"), profile.get("source"),
            identity["type"], identity["classifier"])


def _value_key(row) -> tuple:
    values = row["values"]
    return (values["version"], values["scope"], values["optional"])


def _identity_sort(key) -> tuple:
    return tuple(str(part) if part is not None else "" for part in key)


def _value_sort(value_key) -> tuple:
    """Deterministic order over raw value triples without coercing evidence:
    None sorts before any string, and the stored value keeps its raw None."""
    return tuple((part is not None, part if part is not None else "") for part in value_key)


def _classify_pair(old_row, new_row):
    """Kind for one identity whose single old and new rows differ in values.
    When the version changes together with scope or optional, the
    version-bearing kind wins and both raw field sets stay in the event."""
    old_version, new_version = old_row["values"]["version"], new_row["values"]["version"]
    if ("${" in old_version) != ("${" in new_version):
        return "property-change"
    if old_row["identity"]["management_only"] and old_version != new_version:
        return "management-change"
    if old_version != new_version:
        return "version-change"
    if old_row["values"] != new_row["values"]:
        return "declaration-change"
    return None


def _declaration_transitions(old_rows, new_rows, seen) -> list:
    """One transition per declaration identity between two snapshots:
    (kind, old row, new row, ambiguous, removed values, added values).

    `seen` is the set of identities observed earlier for THIS POM path, so an
    identity that first appeared in a different module never turns a genuine
    addition into a reintroduction, while removal and return inside one POM
    still does. Rows sharing an identity are indistinguishable duplicates:
    their value multisets drive the verdict (Counter difference), the event is
    marked ambiguous, and value fields are reported only when exactly one
    distinct value was removed or added, so no arbitrary row pairing is ever
    presented. The old/new rows carry identity only for those events."""
    old_groups, new_groups = {}, {}
    for row in old_rows:
        old_groups.setdefault(_identity_key(row), []).append(row)
    for row in new_rows:
        new_groups.setdefault(_identity_key(row), []).append(row)
    transitions = []
    for key in sorted(set(old_groups) | set(new_groups), key=_identity_sort):
        olds, news = old_groups.get(key, []), new_groups.get(key, [])
        removed = Counter(map(_value_key, olds)) - Counter(map(_value_key, news))
        added = Counter(map(_value_key, news)) - Counter(map(_value_key, olds))
        if not removed and not added:
            continue
        ambiguous = len(olds) > 1 or len(news) > 1
        dropped = tuple(sorted(removed.elements(), key=_value_sort))
        gained = tuple(sorted(added.elements(), key=_value_sort))
        if not olds:
            kind = "reintroduction" if key in seen else "addition"
            transitions.append((kind, None, news[0], ambiguous, (), gained))
        elif not news:
            transitions.append(("removal", olds[0], None, ambiguous, dropped, ()))
        elif not ambiguous:
            kind = _classify_pair(olds[0], news[0])
            if kind is not None:
                transitions.append((kind, olds[0], news[0], False, (), ()))
        elif not removed:
            transitions.append(("addition", olds[0], news[0], True, (), gained))
        elif not added:
            transitions.append(("removal", olds[0], news[0], True, dropped, ()))
        else:
            transitions.append(("declaration-change", olds[0], news[0], True, dropped, gained))
    return transitions


def _single_value(value_keys, index):
    """The one distinct member at `index` among unpairable value keys, else None."""
    distinct = {value_key[index] for value_key in value_keys}
    return distinct.pop() if len(distinct) == 1 else None


def _row_fields(row, values=None):
    """Event-facing view of one matched declaration row (absent row: Nones).
    `values`, a version/scope/optional triple, overrides the row's own values
    for ambiguous events, where value fields must come from the observed
    multiset difference instead of an arbitrary representative row."""
    if not row:
        return {"version": None, "management_only": None, "profile": None,
                "scope": None, "optional": None, "type": None, "classifier": None}
    identity = row["identity"]
    source = row["values"] if values is None else dict(zip(("version", "scope", "optional"), values))
    return {
        "version": source["version"],
        "management_only": identity["management_only"],
        "profile": dict(identity["profile"]) if identity["profile"] else None,
        "scope": source["scope"],
        "optional": source["optional"],
        "type": identity["type"],
        "classifier": identity["classifier"],
    }


def _property_values(old_row, new_row, old_snapshot, new_snapshot) -> dict:
    """Literal values of referenced properties, observed in the same snapshots."""
    values = {}
    for row, snapshot in ((new_row, new_snapshot), (old_row, old_snapshot)):
        if not row or not row["values"]["version"] or snapshot["status"] != "read":
            continue
        for name in _PLACEHOLDER_RE.findall(row["values"]["version"]):
            if not name or name in values:
                continue
            observed = snapshot["meta"]["properties"].get(name)
            if observed is not None:
                values[name] = observed
    return values


def _read_snapshot(git, rev, path, add_issue) -> dict:
    """One POM snapshot at a revision: absent, read, or error (never guessed)."""
    tree = git.run(["ls-tree", "-z", "--full-name", rev, "--", path])
    if tree.outcome != "ok" or tree.rc != 0:
        detail = tree.issue or f"exit {tree.rc}: {_first_line(tree.stderr)}"
        add_issue("git-tree-read", f"cannot enumerate {path} at {rev}: {detail}", commit=rev, pom=path)
        return {"status": "error"}
    if not tree.stdout:
        return {"status": "absent"}
    entry = tree.stdout.split(b"\0")[0]
    meta_part, separator, _name = entry.partition(b"\t")
    bits = meta_part.split(b" ")
    if not separator or len(bits) != 3:
        add_issue("git-tree-read", f"unparsable ls-tree entry for {path} at {rev}", commit=rev, pom=path)
        return {"status": "error"}
    mode, otype, oid = (bit.decode("ascii", errors="replace") for bit in bits)
    if otype != "blob" or mode not in ("100644", "100755"):
        add_issue("pom-entry-not-regular", f"{path} at {rev} is {otype} mode {mode}, not a regular file",
                  commit=rev, pom=path)
        return {"status": "error"}
    if not _SHA_RE.fullmatch(oid):
        add_issue("git-tree-read", f"object id for {path} at {rev} has unexpected shape", commit=rev, pom=path)
        return {"status": "error"}
    blob = git.run(["cat-file", "blob", oid])
    if blob.outcome != "ok" or blob.rc != 0:
        detail = blob.issue or f"exit {blob.rc}: {_first_line(blob.stderr)}"
        add_issue("git-blob-read", f"cannot read {path} at {rev}: {detail}", commit=rev, pom=path)
        return {"status": "error"}
    try:
        elem = parse_xml(blob.stdout, limits=Limits(xml_bytes=git.max_output_bytes))
    except UnsafeXML as exc:
        add_issue("malformed-pom", f"{path} at {rev}: {exc}", commit=rev, pom=path)
        return {"status": "error"}
    problem = _root_problem(elem)
    if problem is not None:
        add_issue("nonproject-root", f"{path} at {rev}: {problem}", commit=rev, pom=path)
        return {"status": "error"}
    declarations, _refs, meta = _extract_project(elem, path, path)
    return {"status": "read", "declarations": declarations, "meta": meta}


def inspect_history(repo, query, *, pom_paths, introducing_dependencies=(),
                    max_commits=DEFAULT_MAX_COMMITS,
                    max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                    timeout_seconds=DEFAULT_TIMEOUT_SECONDS) -> dict:
    """JSON-safe history of one dependency's declarations in given POM paths.

    Returns {'query', 'revision', 'events', 'last_modification', 'coverage',
    'issues'}: the parsed g:a[:v] query, the analyzed HEAD revision, events
    ordered oldest analyzed commit first, the latest declaration-changing and
    file-touching commits reported separately, explicit coverage limits, and
    issues for everything not observed. No Maven resolution, property
    interpolation, profile activation or network access happens; placeholders
    stay raw and transitive introductions stay candidates.
    """
    for label, value in (("max_commits", max_commits), ("max_output_bytes", max_output_bytes),
                         ("timeout_seconds", timeout_seconds)):
        _positive_int(value, label)
    parsed_query = _parse_query(query)
    paths = _validate_pom_paths(pom_paths)
    introducing = _parse_introducing(introducing_dependencies)

    started_at = _now()
    issues = []

    def add_issue(code, detail, commit=None, pom=None):
        issues.append({"code": code, "detail": detail, "commit": commit, "pom": pom})

    coverage = {
        "scope": "declaration-history",
        "status": None,
        "order": "events are ordered oldest analyzed commit first",
        "first_parent_only": True,
        "truncated": False,
        "shallow": False,
        "commits_touching": 0,
        "paths": list(paths),
        "introducing_dependencies": introducing,
        "limits": {"max_commits": max_commits, "max_output_bytes": max_output_bytes,
                   "timeout_seconds": timeout_seconds, "text_chars": _TEXT_CAP,
                   "timeout_note": (
                       "two bounded phases, not one whole-call timeout: context phase runs first "
                       "(each checkout_context command separately bounded by timeout_seconds), "
                       "then the analysis phase enforces one aggregate timeout_seconds deadline "
                       "shared by all history commands; the whole call is therefore not bounded "
                       "by a single timeout")},
        "started_at": started_at,
        "finished_at": None,
        "limitations": list(_LIMITATIONS),
    }
    events = []
    result = {
        "query": parsed_query,
        "revision": None,
        "events": events,
        "last_modification": {"declaration": None, "file": None, "note": _LAST_MOD_NOTE},
        "coverage": coverage,
        "issues": issues,
    }

    def finish(status):
        return _finish(result, status)

    context = checkout_context(repo, timeout_seconds=timeout_seconds,
                               max_output_bytes=max_output_bytes)
    coverage["checkout_status"] = context["status"]
    revision = context["revision"]
    if revision is None:
        issues.extend({"code": "checkout-context", "detail": text, "commit": None, "pom": None}
                      for text in context["issues"])
        return finish(context["status"])
    result["revision"] = revision
    if context["status"] != "complete":
        add_issue("checkout-context", "; ".join(context["issues"]) or "checkout context partial")

    trusted_path = os.environ.get("PATH") or os.defpath
    git_path = _resolve_git(trusted_path)
    if git_path is None:
        add_issue("git-missing", "git executable not found on PATH")
        return finish("unavailable")

    with tempfile.TemporaryDirectory(prefix="mdi-history-home-") as home:
        env = dict(_child_env(trusted_path, home))
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        git = _Runner(git_path, context["repo"], env, timeout_seconds, max_output_bytes)
        try:
            return _analyze(git, result, revision, add_issue)
        except _BudgetExhausted:
            add_issue("time-budget-exhausted",
                      f"aggregate timeout_seconds={timeout_seconds} budget exhausted before completion")
            return finish("partial")


def _analyze(git, result, revision, add_issue) -> dict:
    coverage = result["coverage"]
    paths = tuple(coverage["paths"])
    events = result["events"]
    issues = result["issues"]
    introducing = coverage["introducing_dependencies"]
    max_commits = coverage["limits"]["max_commits"]
    queries = [("query", result["query"])] + [("introducing", item) for item in introducing]

    shallow_probe = git.run(["rev-parse", "--is-shallow-repository"])
    shallow = (shallow_probe.outcome == "ok" and shallow_probe.rc == 0
               and _json_text(shallow_probe.stdout).strip() == "true")
    coverage["shallow"] = shallow
    if shallow_probe.outcome != "ok" or shallow_probe.rc != 0:
        add_issue("shallow-unknown", "cannot determine whether history is shallow: "
                  f"{shallow_probe.issue or f'exit {shallow_probe.rc}: {_first_line(shallow_probe.stderr)}'}")

    observed = set()
    seed_failed = set()
    for path in paths:
        seeded = git.run(["ls-tree", "-z", "--full-name", revision, "--", path])
        if seeded.outcome != "ok" or seeded.rc != 0:
            seed_failed.add(path)
            detail = seeded.issue or f"exit {seeded.rc}: {_first_line(seeded.stderr)}"
            add_issue("head-tree-read",
                      f"cannot enumerate {path} at HEAD {revision}: {detail}", commit=revision, pom=path)
        elif seeded.stdout:
            observed.add(path)

    listing = git.run(["log", "--first-parent", "-z", f"--format={_LOG_FORMAT}",
                       f"--max-count={max_commits + 1}", revision, "--", *paths])
    if listing.outcome != "ok" or listing.rc != 0:
        detail = listing.issue or f"exit {listing.rc}: {_first_line(listing.stderr)}"
        add_issue("git-history-listing", f"cannot list first-parent history for {list(paths)}: {detail}")
        return _finish(result, "partial")
    try:
        commits = _parse_log(listing.stdout)
    except _ProtocolError as exc:
        add_issue("git-history-protocol", f"unparsable history listing: {exc}")
        return _finish(result, "partial")
    coverage["commits_touching"] = len(commits)
    truncated = len(commits) > max_commits
    coverage["truncated"] = truncated
    if truncated:
        commits = commits[:max_commits]

    cache = {}

    def snapshot(rev, path):
        key = (rev, path)
        if key not in cache:
            cache[key] = _read_snapshot(git, rev, path, add_issue)
            if cache[key]["status"] == "read":
                observed.add(path)
        return cache[key]

    seen_before = {}
    newest = commits[0] if commits else None

    for entry in reversed(commits):
        for path in paths:
            # Reintroduction detection is scoped per POM path: an identity
            # seen earlier in a different module stays an addition here.
            path_seen = seen_before.setdefault(path, set())
            new_snapshot = snapshot(entry["id"], path)
            if entry["first_parent"] is None:
                if shallow:
                    add_issue("shallow-boundary",
                              f"commit {entry['id']} is a shallow boundary; its parent state is "
                              "unavailable so changes are not classified", commit=entry["id"], pom=path)
                    continue
                old_snapshot = {"status": "absent"}
            else:
                old_snapshot = snapshot(entry["first_parent"], path)
            if new_snapshot["status"] == "error" or old_snapshot["status"] == "error":
                continue
            for tag, query in queries:
                old_rows = _matched_rows(query, old_snapshot)
                new_rows = _matched_rows(query, new_snapshot)
                transitions = _declaration_transitions(old_rows, new_rows, path_seen)
                for row in old_rows + new_rows:
                    path_seen.add(_identity_key(row))
                for kind, old, new, ambiguous, removed, added in transitions:
                    if ambiguous:
                        old_fields = _row_fields(old, tuple(_single_value(removed, i) for i in range(3)))
                        new_fields = _row_fields(new, tuple(_single_value(added, i) for i in range(3)))
                    else:
                        old_fields, new_fields = _row_fields(old), _row_fields(new)
                    coordinates = (new or old)["identity"]
                    event = {
                        "kind": kind,
                        "pom": path,
                        "commit": _summary(entry),
                        "matched_coordinates": {
                            "group_id": coordinates["group_id"],
                            "artifact_id": coordinates["artifact_id"],
                        },
                        "old_version": old_fields["version"],
                        "new_version": new_fields["version"],
                        "old_management_only": old_fields["management_only"],
                        "new_management_only": new_fields["management_only"],
                        "old_profile": old_fields["profile"],
                        "new_profile": new_fields["profile"],
                        "old_scope": old_fields["scope"],
                        "new_scope": new_fields["scope"],
                        "old_type": old_fields["type"],
                        "new_type": new_fields["type"],
                        "old_classifier": old_fields["classifier"],
                        "new_classifier": new_fields["classifier"],
                        "old_optional": old_fields["optional"],
                        "new_optional": new_fields["optional"],
                    }
                    if ambiguous:
                        event["ambiguous"] = True
                        event["observed_values"] = {
                            "removed": [dict(zip(("version", "scope", "optional"), value))
                                        for value in removed],
                            "added": [dict(zip(("version", "scope", "optional"), value))
                                      for value in added],
                        }
                    if tag == "query":
                        if kind == "property-change":
                            event["property_values"] = _property_values(
                                old, new, old_snapshot, new_snapshot)
                        events.append(event)
                    elif kind != "removal":
                        events.append({
                            "kind": "candidate-introduction",
                            "pom": path,
                            "commit": _summary(entry),
                            "matched_coordinates": {
                                "group_id": coordinates["group_id"],
                                "artifact_id": coordinates["artifact_id"],
                            },
                            "introducing": {
                                "query": query["raw"],
                                "change_kind": kind,
                                "old_version": old_fields["version"],
                                "new_version": new_fields["version"],
                            },
                            "evidence": _CANDIDATE_NOTE,
                        })

    for path in paths:
        if path not in observed and path not in seed_failed:
            add_issue("pom-path-unobserved",
                      f"{path} was not observed in any analyzed snapshot or at HEAD")

    direct = [event for event in events if event["kind"] != "candidate-introduction"]
    last = result["last_modification"]
    if direct:
        last["declaration"] = {"commit": direct[-1]["commit"], "kind": direct[-1]["kind"]}
    if newest is not None:
        last["file"] = {"commit": _summary(newest)}
    status = "complete" if not issues and not truncated and not shallow else "partial"
    return _finish(result, status)


def _finish(result, status) -> dict:
    result["coverage"]["status"] = status
    result["coverage"]["finished_at"] = _now()
    return result
