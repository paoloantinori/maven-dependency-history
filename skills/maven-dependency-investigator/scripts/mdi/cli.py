"""Offline CLI orchestration for the Maven dependency investigator.

Entry point ``scripts/investigate.py``; every invocation prints exactly one
JSON document on stdout and exits 0 (valid result, including partial
evidence and explicit no-match), 2 (invalid invocation, schema or rejected
input), 3 (stale expected revision or concurrent writer: ``RunConflict``),
or 4 (unexpected internal failure, sanitized message, never a traceback).

Verbs implement the plan contract lines 81-118. The offline verbs
``discover``/``select``/``collect``/``import``/``report`` never invoke
Maven, a wrapper, a version probe, project code or the network.
``propose`` appends an inert proposed operation: a Maven diagnostic (tree,
effective-pom or classpath, each spelled with its pinned plugin goal and an
explicit output flag path) is built only from the caller's exact absolute
``--maven`` executable, explicit ``--input`` pins and an explicit
destinations declaration (``--destinations-unknown`` is a declaration, never
inferred from absent flags), and an advisory operation requires one exact
known ``groupId:artifactId:version`` from the selected candidate (raw
placeholders are not resolved values); a continuation page is a new
proposal carrying an explicit ``--page-token`` and needs fresh approval.
``execute`` routes the stored operation kind to its executor with the
caller's approval file: a bounded, strictly parsed JSON record this CLI
only ever reads and never creates (no ambient approvals, nothing inferred).
The executed Maven command runs unsandboxed under the executor's
disclosure; its raw output bytes never reach stdout or the report (only
size and digest), and process success is never presented as a parsed
report: importing the produced output stays an explicit ``import`` with
its own file capture and provenance. The Maven output target's prestate
(absent or present with digest) is captured before the proposal, bound
immutably into the operation's tool identity and disclosure, and
re-observed before consent is spent, so a target created, replaced,
removed or changed between proposal and execution refuses without
spawning anything; an existing target refuses the proposal outright
unless ``--output-overwrite`` records explicit approval to replace that
observed file (best-effort same-user race detection, never atomic
no-clobber). Large-run storage failures surface through the existing
RunStore error mapping (exit 3 conflict, exit 4 uncertain) with no
silent truncation.
The ``collect`` class-usage helper is used only after
verifying the vendored ``SHA256SUMS`` manifest byte for byte and
cross-checking the verified jar closure against the installed
``helper/DEPENDENCIES.json`` pins and the packaged ``BUILD-INFO.json``
digests; a missing or inconsistent helper is a recorded unavailable stage,
never an investigation-time bootstrap. Trust model: the default vendor tree
is part of the trusted skill installation and these checks are
tamper-evidence, not same-user adversary resistance; ``--vendor-dir`` is an
explicit operator override that substitutes a different tree for that
installation trust.

Run state lives outside the checkout. ``discover`` probes the checkout
read-only first and only then creates a fresh UUID run under
``--state-dir`` (default
``${XDG_STATE_HOME:-~/.local/state}/maven-dependency-investigator/runs``,
created private 0700, refusing to adopt or overwrite an existing file,
symlink or unrelated directory) and commits its findings as revision 1
through ``RunStore``, whose exclusive creation, no-symlink path walk,
single-writer lock and one-revision-per-commit checks carry the storage
guarantees. Continuation commands take ``--expect-revision``; a mismatch
against the run's latest revision is exit 3 before any work starts.

Continuation context (repo, parsed query, module/artifact filters, git
context, coverage, issues and the query-matching candidate ids) is
recorded at discover time as a ``discovery-context`` evidence payload, so
``select``/``collect`` resume from run state alone without inventing new
schema fields. Selection may name only candidates from that recorded set,
never contextual subjects, and it is immutable once recorded.

``import --kind runtime-observations`` reads the internal recorded-workload
schema only; it never instruments or executes a workload. Embedded provenance
is caller-attested, not authenticated. Per-observation candidate associations
compare coordinates, module and realm with the recorded selection; unknown,
unselected, ambiguous or mismatched observations remain partial and are never
attached as verified subject evidence. The workload scope is retained, including
empty traces that cannot prove non-use. No separate Maven provenance is accepted.

Graph history is an explicit ``collect --graph-history`` opt-in. It compares
same-run tree imports through local ancestry proofs; each underlying ancestry
or declaration collector has its own 10-second/1-MiB budget, with at most 64
declaration commits, not a combined hard deadline. Maven report ``--realm``
is caller-attested and defaults to unknown; it is never inferred from selection.

Shaded correspondence is opt-in through repeatable ``collect --reference-jar``
(at most eight references and eight selected packaged copies). A shared 120-second
cooperative parent budget is passed to isolated archive readers. Each selected
nested entry chain and captured digest must still match discovery; comparisons
are fresh observations, not a claim that the checkout built the enclosing archive.
Only entry-content correspondence is emitted. Existing matching graph/model records
and captured selected-module shade configuration are candidate leads. Literal indexed
entry paths may match declared relocation prefixes, but profile activation, execution,
plugin configuration semantics and binary relocation remain unverified. Missing POM
configuration leaves attribution unresolved without discarding archive comparisons.
Full bounded inspection payloads may reach the run-store report cap;
that commit is refused explicitly, never silently truncated.

Stage outcomes mirror library coverage honestly: no-match and unavailable
tools (missing git, missing java, unusable vendored helper) are valid
exit-0 results with explicit reasons. Nothing missing is ever invented as
evidence, and no removal or usage is claimed without observed proof.
"""

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from mdi import fsprobe, operations, raw_capture_consent, report as report_module
from mdi.raw_capture_store import TOOL_IDENTITY as _CAPTURE_TOOL_IDENTITY
from mdi.advisories import AdvisoryError
from mdi.advisory_executor import (AdvisoryExecutionError, advisory_proposal,
                                   execute_advisory)
from mdi.candidates import discover_candidates
from mdi.archive_files import FileArchiveLimits
from mdi.archive_pipeline import (inspect_archive as inspect_archive_pipeline,
                                  _quota_inventory, DEFAULT_QUOTA_BYTES, MAX_QUOTA_ENTRIES)
from mdi.distribution_worker import inspect_isolated as inspect_distribution_isolated
from mdi.oci_worker import inspect_isolated as inspect_oci_isolated
from mdi.distributions import DistributionLimits
from mdi.oci_visibility import VisibilityError, compose_visibility
from mdi.oci_layout import OCILimits
from mdi.shaded import ShadedError, match_archives
from mdi.discover import open_checkout_fd, open_module_dir, capture_at
from mdi.shade_config import ConfigError, ConfigLimits, inspect_shade_config
from mdi.capture import CaptureError, capture_file
from mdi.history import inspect_history
from mdi.history_pipeline import HistoryPipelineError, collect_graph_history
from mdi.maven import (PROVENANCE_FIELDS, MavenImportError, import_classpath,
                       import_dependency_tree, import_effective_pom)
from mdi.maven_executor import (MavenExecutionError, execute_maven,
                                maven_proposal)
from mdi.runstore import CommitUncertain, RunConflict, RunStore, RunStoreError
from mdi.usage_pipeline import inspect_usage
from mdi.runtime_evidence import RuntimeEvidenceError, import_runtime_observations
from mdi.forge_evidence import ForgeEvidenceError, import_forge_evidence

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_CONFLICT = 3
EXIT_UNEXPECTED = 4

_DISCOVERY_CONTEXT = "discovery-context"
_DISCOVERY_MATCH = "discovery-match"
_HISTORY_CATEGORY = "declaration-history"
_USAGE_CATEGORY = "class-usage"

_IMPORT_CATEGORIES = {
    "tree-json": "imported-tree-json",
    "effective-pom": "imported-effective-pom",
    "classpath": "imported-classpath",
}
_IMPORT_MAX_BYTES = 8 * 1024 * 1024
_PROVENANCE_MAX_BYTES = 1 << 20
_APPROVAL_MAX_BYTES = 1 << 20
_VENDOR_JARS = ("helper.jar",)
_MAVEN_PLUGIN_GOALS = {
    "tree": ("org.apache.maven.plugins:maven-dependency-plugin:3.11.0:tree",
             ("-DoutputType=json", "-DoutputFile={output}")),
    "effective-pom": ("org.apache.maven.plugins:maven-help-plugin:3.5.2:effective-pom",
                      ("-Dverbose=true", "-Doutput={output}")),
    "classpath": ("org.apache.maven.plugins:maven-dependency-plugin:3.11.0:build-classpath",
                  ("-Dmdep.outputFile={output}",)),
}
_OUTPUT_FLAG_PREFIXES = ("-DoutputFile=", "-Doutput=", "-Dmdep.outputFile=")
_MESSAGE_CAP = 500
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


class CliError(Exception):
    """A command-level failure carrying its exit code and a clean message."""

    def __init__(self, message, exit_code=EXIT_INVALID):
        super().__init__(message)
        self.exit_code = exit_code


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(message):
    return " ".join(str(message).split())[:_MESSAGE_CAP]


def _emit(doc, code=EXIT_OK):
    sys.stdout.write(json.dumps(doc, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


def _emit_error(exc, *, source=None):
    document = {
        "ok": False,
        "error": {"message": _clean(exc), "exit_code": exc.exit_code},
    }
    owner = source if source is not None else exc
    if hasattr(owner, 'collection_cleanup'):
        document['cleanup'] = owner.collection_cleanup
    if hasattr(owner, 'raw_capture'):
        document['capture'] = owner.raw_capture
    return _emit(document, exc.exit_code)


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse's default prints usage to stderr
        raise CliError(f"invalid invocation: {message}")


def _int_arg(text):
    # strict ASCII decimal: int() would silently accept "1_0" and non-ASCII digits
    if not re.fullmatch(r"[0-9]+", text):
        raise argparse.ArgumentTypeError(
            f"expected a nonnegative decimal integer, got {text!r}")
    return int(text)


def _build_parser():
    parser = _Parser(prog="investigate.py",
                     description="Offline Maven dependency investigation CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="probe a checkout for query-matching candidates")
    discover.add_argument("--repo", required=True)
    discover.add_argument("--query", required=True)
    discover.add_argument("--module")
    discover.add_argument("--artifact")
    discover.add_argument("--state-dir")

    select = sub.add_parser("select", help="record the immutable candidate selection")
    select.add_argument("--run-dir", required=True)
    select.add_argument("--expect-revision", required=True, type=_int_arg)
    group = select.add_mutually_exclusive_group(required=True)
    group.add_argument("--candidate")
    group.add_argument("--all", action="store_true", dest="select_all")

    collect = sub.add_parser("collect", help="collect selected history and optional usage")
    collect.add_argument("--run-dir", required=True)
    collect.add_argument("--expect-revision", required=True, type=_int_arg)
    collect.add_argument("--usage-root")
    collect.add_argument("--usage-dependency", action="append", dest="usage_dependencies")
    collect.add_argument("--usage-scope", choices=("production", "test"), default="production")
    collect.add_argument("--java")
    collect.add_argument("--vendor-dir")
    collect.add_argument("--staging-base")
    distribution = collect.add_mutually_exclusive_group()
    distribution.add_argument("--distribution", help="one existing local tar or tar.gz distribution")
    distribution.add_argument("--oci-layout", help="one existing local OCI image layout; no pull")
    collect.add_argument("--distribution-format", choices=("tar", "tar.gz"))
    collect.add_argument("--oci-manifest", help="explicit sha256 manifest digest in the local layout")
    collect.add_argument("--reference-jar", action="append", dest="reference_jars",
                         help="compare selected packaged copies with explicit local reference JARs; at most eight")
    collect.add_argument("--graph-history", action="store_true",
                         help="compare same-run imported trees using bounded local Git ancestry; "
                              "each ancestry/declaration collector has its own 10-second and 1-MiB cap")

    importer = sub.add_parser("import", help="import a recorded producer report")
    importer.add_argument("--run-dir", required=True)
    importer.add_argument("--expect-revision", required=True, type=_int_arg)
    importer.add_argument("--report", required=True)
    importer.add_argument("--kind", required=True,
                          choices=("tree-json", "effective-pom", "classpath", "runtime-observations", "forge-evidence"))
    importer.add_argument("--provenance")
    importer.add_argument("--realm", choices=("project", "build-plugin", "unknown"),
                          help="caller-attested realm for a Maven report; omitted remains unknown")
    importer.add_argument("--tree-mode", choices=("nonverbose", "verbose"),
                          help="attest the recorded tree kind; the default is "
                               "diagnostic verbose and omission never selects "
                               "the authoritative nonverbose mode")

    reporter = sub.add_parser("report", help="emit a stored report revision")
    reporter.add_argument("--run-dir", required=True)
    reporter.add_argument("--revision", type=_int_arg)
    reporter.add_argument("--validate", action="store_true")

    proposal = sub.add_parser("propose", help="append an inert proposed operation")
    proposal.add_argument("--run-dir", required=True)
    proposal.add_argument("--expect-revision", required=True, type=_int_arg)
    proposal.add_argument("--kind", required=True,
                          choices=tuple(_MAVEN_PLUGIN_GOALS) + ("advisory", "raw-capture"))
    proposal.add_argument("--candidate")
    proposal.add_argument("--source-operation")
    proposal.add_argument("--max-capture-bytes", type=_int_arg)
    proposal.add_argument("--maven")
    proposal.add_argument("--output")
    proposal.add_argument("--output-overwrite", action="store_true",
                          dest="output_overwrite")
    proposal.add_argument("--input", action="append", dest="inputs")
    proposal.add_argument("--destination", action="append", dest="destinations")
    proposal.add_argument("--destinations-unknown", action="store_true",
                          dest="destinations_unknown")
    proposal.add_argument("--page-token")

    execution = sub.add_parser("execute", help="execute one operation")
    execution.add_argument("--run-dir", required=True)
    execution.add_argument("--expect-revision", required=True, type=_int_arg)
    execution.add_argument("--operation", required=True)
    execution.add_argument("--approval-file", required=False,
                           help="optional host approval; without it, diagnostic "
                                "operations run under the standing policy")

    timeline_cmd = sub.add_parser("timeline",
                                  help="when a dependency entered scope, who added it, "
                                       "and every version change across Git history")
    timeline_cmd.add_argument("--repo", required=True)
    timeline_cmd.add_argument("--query", required=True,
                              help="groupId:artifactId of the dependency to trace")

    releases_cmd = sub.add_parser("releases",
                                  help="which tagged releases carry the dependency "
                                       "declaration, at what version")
    releases_cmd.add_argument("--repo", required=True)
    releases_cmd.add_argument("--query", required=True,
                              help="groupId:artifactId of the dependency to check")

    chain_cmd = sub.add_parser("chain",
                               help="every inclusion path from the tree root to a "
                                    "coordinate, traced from a tree JSON file")
    chain_cmd.add_argument("--tree", required=True,
                           help="path to the tree JSON produced by dependency:tree")
    chain_cmd.add_argument("--query", required=True,
                           help="groupId:artifactId of the dependency to trace")

    security_cmd = sub.add_parser("security",
                                  help="known OSV vulnerabilities for one exact coordinate")
    security_cmd.add_argument("--query", required=True,
                              help="groupId:artifactId:version of the dependency to check")

    conflicts_cmd = sub.add_parser("conflicts",
                                   help="version mediations in a resolved tree")
    conflicts_cmd.add_argument("--tree", required=True,
                               help="path to the tree JSON produced by dependency:tree")

    versions_cmd = sub.add_parser("versions",
                                  help="per-GA version landscape across all module POMs")
    versions_cmd.add_argument("--repo", required=True)
    versions_cmd.add_argument("--query",
                              help="optional groupId:artifactId to filter")

    diff_cmd = sub.add_parser("diff",
                              help="what changed in the dependency graph between two trees")
    diff_cmd.add_argument("--tree-a", required=True)
    diff_cmd.add_argument("--tree-b", required=True)
    diff_cmd.add_argument("--label-a", default="A")
    diff_cmd.add_argument("--label-b", default="B")

    usage_cmd = sub.add_parser("usage",
                               help="project-class to library-class references")
    usage_cmd.add_argument("--jar", required=True)
    usage_cmd.add_argument("--classes", required=True)
    usage_cmd.add_argument("--scope", default="production",
                           choices=("production", "test"))

    orphans_cmd = sub.add_parser("orphans",
                                 help="declared dependencies with zero static references")
    orphans_cmd.add_argument("--repo", required=True)
    orphans_cmd.add_argument("--classes", required=True)
    orphans_cmd.add_argument("--maven-repo",
                             help="optional path to the local Maven repository")
    execution.add_argument("--capture-operation")
    execution.add_argument("--capture-approval-file")
    return parser


def _contains_path(inner, outer):
    """Component-wise containment: True only when inner is inside or equal
    to outer, on normalized spellings."""
    try:
        return os.path.commonpath([os.path.normpath(inner), os.path.normpath(outer)]) \
            == os.path.normpath(outer)
    except ValueError:
        return False


def _reject_checkout_overlap(location, checkout, what, *, both_directions=False):
    """Refuse run state or staging locations inside the inspected checkout,
    before any directory is created, on both the lexical spelling and the
    realpath spelling, so a symlink alias into the checkout cannot smuggle
    a destination past the check (makedirs would happily create through it).
    """
    pairs = {(os.path.abspath(location), os.path.abspath(checkout))}
    pairs.add((os.path.realpath(location), os.path.realpath(checkout)))
    for candidate_loc, candidate_chk in pairs:
        inside = _contains_path(candidate_loc, candidate_chk)
        overlaps = inside or (both_directions and _contains_path(candidate_chk, candidate_loc))
        if overlaps:
            raise CliError(
                f"{what} {location!r} overlaps the checkout {checkout!r} (lexical or "
                f"realpath spelling) and is refused: run state and staging directories "
                f"never live in the inspected checkout")


def _require_disk_backed(path, what):
    """Refuse memory-backed filesystems: spool and scratch bytes become RAM."""
    try:
        memory_backed = fsprobe.is_memory_backed(path)
    except OSError as exc:
        raise CliError(
            f"cannot inspect the filesystem holding {path!r}: {exc.strerror}") from exc
    if memory_backed:
        raise CliError(fsprobe.memory_backed_message(what, path))


def _ensure_private_dir(path, what):
    """Return an existing plain directory, or create missing parents 0700.

    An existing symlink or non-directory is never adopted or replaced; the
    directory itself is only ever created, never overwritten. RunStore then
    enforces its own no-symlink walk inside it.
    """
    target = os.path.abspath(path)
    if os.path.lexists(target):
        if os.path.islink(target) or not os.path.isdir(target):
            raise CliError(f"{what} {target!r} exists and is not a plain directory")
        _require_disk_backed(target, what)
        return target
    ancestor = target
    while not os.path.lexists(ancestor):
        ancestor = os.path.dirname(ancestor)
    # statfs needs an existing path; the nearest existing ancestor decides
    # which filesystem makedirs will populate.
    _require_disk_backed(ancestor, what)
    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise CliError(f"cannot create {what} {target!r}: {exc.strerror or exc}") from exc
    # makedirs(exist_ok=True) accepts a final component that raced into
    # existence as a symlink to a directory; only this recheck refuses it.
    if os.path.islink(target) or not os.path.isdir(target):
        raise CliError(f"{what} {target!r} did not become a plain directory")
    return target


def _default_state_base():
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(state_home, "maven-dependency-investigator", "runs")


def _open_run(run_dir):
    _require_disk_backed(os.path.abspath(run_dir), "run directory")
    try:
        return RunStore.open(run_dir)
    except RunStoreError as exc:
        raise CliError(f"cannot open run directory: {_clean(exc)}") from exc


def _checked_load(store, expected_revision):
    """Load the latest report or raise the conflict exit code on mismatch."""
    if expected_revision < 0:
        raise CliError(f"--expect-revision must not be negative, got {expected_revision}")
    current = store.load()
    latest = current["revision"]
    if latest != expected_revision:
        raise CliError(
            f"stale expected revision {expected_revision}: run {store.run_id} is at "
            f"revision {latest}; re-read the run and retry with the current revision",
            EXIT_CONFLICT)
    return current


def _find_context(current):
    for record in reversed(current["evidence"]):
        if record["category"] == _DISCOVERY_CONTEXT:
            return record
    raise CliError("run holds no discovery context; only runs created by 'discover' "
                   "can be continued")


def _next_evidence_id(current):
    return f"E{len(current['evidence']) + 1}"


def _next_qualification_id(current):
    return f"Q{len(current['qualifications']) + 1}"


def _json_safe(value):
    """Normalize a collector result to strict JSON-safe data (tuples to lists,
    non-finite rejected). Library results are documented JSON-safe dicts, but
    some embed tuple constants; the report schema rejects tuples, so the
    payload is normalized exactly once, losslessly for JSON types."""
    return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))


def _coordinate_string(coords):
    if not isinstance(coords, dict):
        return None
    joined = ":".join(coords[name] for name in ("groupId", "artifactId", "version")
                      if coords.get(name))
    return joined or None


def _outcome_reasons(issues, extra=()):
    reasons = [f"{issue.get('code', 'issue')}: {_clean(issue.get('detail') or issue)}"
               for issue in issues]
    reasons.extend(_clean(item) for item in extra)
    return reasons


def _append_evidence(draft, record):
    draft["evidence"].append(record)
    return record["id"]


def _append_stage(draft, name, outcome, coverage, reasons, evidence_ids):
    """Record a stage result; a stage name is unique per report, so a later
    collect on the same run updates the prior stage in place (stages are the
    one updatable record kind; the prior revision snapshot keeps history)."""
    record = {
        "name": name,
        "outcome": outcome,
        "coverage": coverage,
        "reasons": reasons,
        "evidence_ids": evidence_ids,
    }
    for index, stage in enumerate(draft["stages"]):
        if stage["name"] == name:
            draft["stages"][index] = record
            return
    draft["stages"].append(record)


# ---------------------------------------------------------------------------
# discover


def _subject_record(candidate):
    subject = {"id": candidate["id"], "realm": candidate["realm"]}
    subject["module"] = candidate["module"] or "."
    coords = _coordinate_string(candidate.get("coordinates"))
    if coords:
        subject["coordinates"] = coords
    plugin = _coordinate_string(candidate.get("owning_plugin"))
    if plugin:
        subject["owning_plugin"] = plugin
    if candidate["kind"] == "packaged-copy" and candidate["copies"]:
        copy = candidate["copies"][0]
        subject["packaged_copy"] = {
            "archive_digest": copy.get("sha256"),
            "entry_path": copy.get("display_path"),
        }
    subject["build_context"] = {
        "kind": candidate["kind"],
        "coordinate_status": candidate["coordinate_status"],
        "inclusion_evidence": candidate["inclusion_evidence"],
    }
    return subject


def _candidate_locator(candidate, repo):
    for declaration in candidate["declarations"]:
        source = declaration.get("source") or {}
        if source.get("pom"):
            return source["pom"]
    for copy in candidate["copies"]:
        if copy.get("path"):
            return copy["path"]
    return repo


@contextmanager
def _archive_scratch(base):
    """Hold a nonblocking base-directory flock for the entire scratch lifetime.

    Locking the held base inode keeps the lock outside the quota inventory and
    avoids lock-file unlink/recreation races. Independent discoveries using the
    same base refuse before scratch creation or inspection; the final base-fd
    close releases the lock only after scratch cleanup. No stale lock is stolen.
    This coordinates cooperating local callers, not hostile directory renames
    or filesystems that do not implement flock exclusion.
    """
    name = "archive-scratch"
    base_fd = open_checkout_fd(base)
    created = False
    scratch_fd = None
    identity = None
    try:
        try:
            fcntl.flock(base_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunConflict("archive scratch is busy in another discovery") from None
        except OSError as exc:
            raise CliError("archive scratch lifetime locking is unavailable") from exc
        _require_disk_backed(base, "archive scratch base")
        try:
            os.mkdir(name, mode=0o700, dir_fd=base_fd)
            created = True
        except FileExistsError:
            pass
        scratch_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=base_fd)
        info = os.fstat(scratch_fd)
        identity = info.st_dev, info.st_ino
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CliError("archive scratch must be a private caller-owned directory")
        yield os.path.join(base, name)
    finally:
        primary = sys.exception()
        close_error = None
        if scratch_fd is not None:
            fd, scratch_fd = scratch_fd, None
            try:
                os.close(fd)
            except BaseException as exc:
                close_error = exc
        if created and identity is not None and close_error is None:
            try:
                current = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
                if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                    os.rmdir(name, dir_fd=base_fd)
            except OSError:
                # Nonempty or replaced scratch is retained, never recursively removed.
                pass
            except BaseException as exc:
                close_error = exc
        fd, base_fd = base_fd, None
        try:
            os.close(fd)
        except BaseException as exc:
            if close_error is None:
                close_error = exc
        if close_error is not None:
            if primary is not None:
                primary.add_note("archive scratch directory close unconfirmed")
            else:
                raise close_error


def _cmd_discover(args):
    repo = os.path.abspath(args.repo)
    if not args.query.strip():
        raise CliError("--query must be a nonempty string")
    artifact = os.path.abspath(args.artifact) if args.artifact else None
    base_arg = args.state_dir or _default_state_base()
    _reject_checkout_overlap(base_arg, repo, "state directory")
    base = _ensure_private_dir(base_arg, "state directory")
    try:
        with _archive_scratch(base) as scratch:
            result = discover_candidates(repo, args.query, module=args.module,
                                         artifact=args.artifact, archive_scratch_base=scratch)
    except (OSError, TypeError, ValueError) as exc:
        raise CliError(f"discovery rejected: {_clean(exc)}") from exc

    store = RunStore.create(base, query=result["query"])
    current = store.load()
    draft = copy.deepcopy(current)
    now = _now()

    context_payload = {
        "repo": repo,
        "query": result["query"],
        "module": args.module,
        "artifact": artifact,
        "candidate_ids": [candidate["id"] for candidate in result["candidates"]],
        "distinct_subjects": result["distinct_subjects"],
        "context": result["context"],
        "coverage": result["coverage"],
        "issues": result["issues"],
    }
    context_id = _append_evidence(draft, {
        "id": _next_evidence_id(draft),
        "category": _DISCOVERY_CONTEXT,
        "source_locator": repo,
        "observed_at": now,
        "payload": context_payload,
    })

    for candidate in result["candidates"]:
        subject = _subject_record(candidate)
        draft["subjects"].append(subject)
        record = {
            "id": _next_evidence_id(draft),
            "category": _DISCOVERY_MATCH,
            "subject_id": subject["id"],
            "source_locator": _candidate_locator(candidate, repo),
            "observed_at": now,
            "payload": {"candidate": candidate},
        }
        if candidate["copies"]:
            record["captured_identity"] = {
                "path": candidate["copies"][0]["path"],
                "sha256": candidate["copies"][0].get("sha256"),
            }
        _append_evidence(draft, record)

    issues = list(result["issues"])
    truncated = result["coverage"]["archives"]["truncated"]
    git_status = result["context"]["status"]
    extra = []
    if truncated:
        extra.append(f"archive coverage truncated: {', '.join(truncated)}")
    if git_status != "complete":
        extra.append(f"git checkout context is {git_status}")
    outcome = "complete" if not issues and not extra else "partial"
    _append_stage(draft, "discover", outcome,
                  {"candidates": len(result["candidates"]),
                   "distinct_subjects": result["distinct_subjects"],
                   "archives_captured": result["coverage"]["archives"]["captured"],
                   "git_context": git_status,
                   "no_match": not result["candidates"]},
                  _outcome_reasons(issues, extra) if outcome == "partial" else [],
                  [context_id])

    persisted = store.commit(draft, current["revision"])
    return _emit({
        "ok": True,
        "command": "discover",
        "run_id": store.run_id,
        "run_dir": store.path,
        "state_base": base,
        "revision": persisted["revision"],
        "query": args.query,
        "no_match": not result["candidates"],
        "selection_required": result["selection_required"],
        "distinct_subjects": result["distinct_subjects"],
        "candidates": [{"id": candidate["id"], "kind": candidate["kind"],
                        "realm": candidate["realm"], "module": candidate["module"],
                        "coordinates": _coordinate_string(candidate.get("coordinates"))}
                       for candidate in result["candidates"]],
        "stage": {"name": "discover", "outcome": outcome},
        "issues": result["issues"],
    })


# ---------------------------------------------------------------------------
# select


def _cmd_select(args):
    store = _open_run(args.run_dir)
    current = _checked_load(store, args.expect_revision)
    context = _find_context(current)
    if current["selection"] is not None:
        raise CliError("selection is already recorded and immutable; investigating "
                       "other candidates requires a new run")
    candidate_ids = context["payload"]["candidate_ids"]
    if args.select_all:
        if not candidate_ids:
            raise CliError("no query-matching candidates were discovered; --all has "
                           "nothing to select")
        chosen, mode = list(candidate_ids), "all"
    else:
        if args.candidate not in candidate_ids:
            raise CliError(
                f"candidate {args.candidate!r} is not one of the query-matching "
                f"candidates {candidate_ids}; no implicit choice is made")
        chosen, mode = [args.candidate], "single"
    draft = copy.deepcopy(current)
    draft["selection"] = {"candidate_ids": chosen, "mode": mode}
    persisted = store.commit(draft, current["revision"])
    return _emit({
        "ok": True,
        "command": "select",
        "run_id": store.run_id,
        "run_dir": store.path,
        "revision": persisted["revision"],
        "selection": persisted["selection"],
    })


# ---------------------------------------------------------------------------
# collect


def _vendor_classpath(vendor_dir):
    """Verify the vendored helper package; return (classpath, hashes) or a reason.

    Trust model, stated plainly: the default vendor tree is part of the
    trusted skill installation. The checks here provide tamper-evidence and
    internal consistency, never same-user adversary resistance. The manifest
    itself is not an anchor (a co-modified manifest, jar and BUILD-INFO stay
    mutually consistent), so the classpath closure is additionally
    cross-checked against the installed ``helper/DEPENDENCIES.json`` pins and
    the packaged ``BUILD-INFO.json`` output digests; a mismatch of any kind
    makes the usage stage unavailable. ``--vendor-dir`` is an explicit
    operator override that substitutes a different tree for that installation
    trust; it is verified by the same rules and never bootstraps, builds or
    downloads anything.

    Manifest paths are canonical relative ``/`` spellings only: backslashes,
    drive letters, UNC prefixes, absolute paths and ``.``/``..`` components
    are refused before any join. Every ``SHA256SUMS`` entry is hashed and
    compared, not just the classpath jars. Nothing is ever built or
    downloaded here.
    """
    base = os.path.abspath(vendor_dir) if vendor_dir else os.path.join(_SKILL_ROOT, "vendor")
    sums_path = os.path.join(base, "SHA256SUMS")
    if not os.path.isfile(sums_path):
        return None, None, (f"vendored helper unavailable: no SHA256SUMS manifest at "
                            f"{sums_path!r}; investigation-time bootstrap is not permitted")
    pinned = {}
    try:
        with open(sums_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                digest, _, rel = line.partition(" ")
                rel = rel.lstrip("* ").strip()
                if not _DIGEST_RE.fullmatch(digest) or not _valid_manifest_rel(rel):
                    return None, None, (f"vendored helper unavailable: unparsable "
                                        f"SHA256SUMS line {line!r}; expected a digest "
                                        f"and a canonical relative '/' path")
                pinned[rel] = digest
        verified = {}
        for rel, digest in pinned.items():
            path = os.path.join(base, *rel.split("/"))
            hasher = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    hasher.update(chunk)
            actual = hasher.hexdigest()
            if actual != digest:
                return None, None, (f"vendored helper unavailable: {rel!r} sha256 "
                                    f"{actual} does not match the pinned {digest}")
            verified[rel] = path
    except OSError as exc:
        return None, None, f"vendored helper unavailable: {exc.strerror or exc}"
    problem = _verify_helper_closure(base, verified, pinned)
    if problem is not None:
        return None, None, problem
    jar_rels = [rel for rel in sorted(pinned)
                if rel == "helper.jar" or (rel.startswith("lib/") and rel.endswith(".jar"))]
    if "helper.jar" not in verified or not jar_rels:
        return None, None, ("vendored helper unavailable: SHA256SUMS lists no verified "
                            "helper.jar or lib/*.jar entries")
    classpath = [verified[rel] for rel in jar_rels]
    hashes = {verified[rel]: pinned[rel] for rel in jar_rels}
    return classpath, hashes, None


def _valid_manifest_rel(rel):
    """Canonical relative '/' path only: no backslash, drive, UNC, absolute,
    empty, '.' or '..' components, decided before any join."""
    if not rel or "\\" in rel or "\x00" in rel:
        return False
    if rel.startswith("/") or os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel):
        return False
    parts = rel.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _read_manifest_json(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read(_PROVENANCE_MAX_BYTES).decode("utf-8"))


def _verify_helper_closure(base, verified, pinned):
    """Cross-check the verified vendor tree against its two identity manifests.

    BUILD-INFO.json (itself manifest-verified) must name the packaged
    helper.jar and every lib jar digest, and every verified lib jar must be
    pinned by the installed helper/DEPENDENCIES.json, whose full dependency
    closure must be present. The installed DEPENDENCIES.json is the anchor
    because it lives in the trusted skill tree outside the overridable
    vendor directory.
    """
    try:
        build_info = _read_manifest_json(os.path.join(base, "BUILD-INFO.json"))
    except (OSError, ValueError):
        return ("vendored helper unavailable: BUILD-INFO.json is missing or not "
                "valid JSON")
    if not isinstance(build_info, dict) \
            or build_info.get("schema") != "mdi.helper-build-info/1":
        return "vendored helper unavailable: BUILD-INFO.json has an unexpected schema"
    outputs = build_info.get("outputs")
    if not isinstance(outputs, dict):
        return "vendored helper unavailable: BUILD-INFO.json outputs is not a JSON object"
    if outputs.get("helper.jar") != pinned.get("helper.jar"):
        return ("vendored helper unavailable: BUILD-INFO.json helper.jar digest does "
                "not match the verified package")
    lib_out = outputs.get("lib")
    if not isinstance(lib_out, dict):
        return "vendored helper unavailable: BUILD-INFO.json outputs.lib is not a JSON object"
    lib_rels = sorted(rel for rel in verified
                      if rel.startswith("lib/") and rel.endswith(".jar"))
    for rel in lib_rels:
        if lib_out.get(rel) != pinned.get(rel):
            return (f"vendored helper unavailable: BUILD-INFO.json digest for {rel!r} "
                    f"does not match the verified package")
    manifest_path = os.path.join(_SKILL_ROOT, "helper", "DEPENDENCIES.json")
    try:
        dependencies = _read_manifest_json(manifest_path)
    except (OSError, ValueError):
        return ("vendored helper unavailable: the installed DEPENDENCIES.json could "
                "not be read for the closure cross-check")
    if not isinstance(dependencies, dict):
        return ("vendored helper unavailable: the installed DEPENDENCIES.json is not "
                "a JSON object")
    dependency_list = dependencies.get("dependencies")
    if not isinstance(dependency_list, list):
        return ("vendored helper unavailable: the installed DEPENDENCIES.json "
                "dependencies entry is not a list")
    pins = {}
    for dep in dependency_list:
        if (not isinstance(dep, dict)
                or not isinstance(dep.get("filename"), str)
                or not isinstance(dep.get("sha256"), str)):
            return ("vendored helper unavailable: the installed DEPENDENCIES.json "
                    "carries a malformed dependency entry")
        pins[dep["filename"]] = dep["sha256"]
    seen = {}
    for rel in lib_rels:
        filename = rel.split("/")[-1]
        if pins.get(filename) != pinned.get(rel):
            return (f"vendored helper unavailable: {rel!r} is not pinned by the "
                    f"installed DEPENDENCIES.json")
        seen[filename] = rel
    missing = sorted(set(pins) - set(seen))
    if missing:
        return ("vendored helper unavailable: the DEPENDENCIES.json closure is "
                f"incomplete in the package; missing {missing}")
    return None


def _selected_pom_paths(current, repo):
    selected = set(current["selection"]["candidate_ids"])
    paths = []
    for record in current["evidence"]:
        if record["category"] != _DISCOVERY_MATCH or record.get("subject_id") not in selected:
            continue
        for declaration in record["payload"]["candidate"].get("declarations", []):
            pom = (declaration.get("source") or {}).get("pom")
            if not pom:
                continue
            rel = os.path.relpath(pom, repo)
            if rel.startswith("..") or os.path.isabs(rel) or rel in paths:
                continue
            paths.append(rel)
    return paths


def _history_stage(draft, repo, query):
    paths = _selected_pom_paths(draft, repo)
    if not paths:
        _append_stage(draft, "history", "not_applicable", {"paths": []},
                      ["no selected subject carries POM declaration paths; archive-only "
                       "candidates have no declaration history"], [])
        return "not_applicable"
    try:
        history = inspect_history(repo, query, pom_paths=paths)
    except Exception as exc:  # collector failure is evidence, not a crash
        _append_stage(draft, "history", "failed", {"paths": paths},
                      [f"history collection failed: {_clean(exc)}"], [])
        return "failed"
    status = history["coverage"]["status"]
    outcome = status if status in ("complete", "partial", "unavailable") else "partial"
    reasons = _outcome_reasons(history["issues"])
    if outcome != "complete" and not reasons:
        reasons = [f"history coverage status is {status!r}"]
    evidence_id = _append_evidence(draft, {
        "id": _next_evidence_id(draft),
        "category": _HISTORY_CATEGORY,
        "source_locator": repo,
        "observed_at": history["coverage"]["finished_at"] or _now(),
        "payload": _json_safe(history),
    })
    _append_stage(draft, "history", outcome,
                  {"status": status, "paths": paths,
                   "commits_touching": history["coverage"]["commits_touching"],
                   "truncated": history["coverage"]["truncated"],
                   "shallow": history["coverage"]["shallow"],
                   "revision": history["revision"],
                   "events": len(history["events"])},
                  reasons, [evidence_id])
    return outcome


def _usage_stage(draft, repo, args, run_dir):
    if bool(args.usage_root) != bool(args.usage_dependencies):
        raise CliError("usage collection requires both --usage-root and at least one "
                       "--usage-dependency")
    if not args.usage_root:
        _append_stage(draft, "usage", "unavailable", {},
                      ["usage prerequisites were not supplied; pass --usage-root and "
                       "--usage-dependency to collect actual class usage"], [])
        return "unavailable"
    classpath, hashes, problem = _vendor_classpath(args.vendor_dir)
    if problem is not None:
        _append_stage(draft, "usage", "unavailable", {}, [problem], [])
        return "unavailable"
    staging_base_arg = args.staging_base or os.path.join(
        os.path.dirname(os.path.abspath(run_dir)), "staging")
    _reject_checkout_overlap(staging_base_arg, repo, "staging base directory",
                             both_directions=True)
    staging_base = _ensure_private_dir(staging_base_arg, "staging base directory")
    try:
        usage = inspect_usage(
            repo, args.usage_root, args.usage_dependencies, scope=args.usage_scope,
            staging_base=staging_base, helper_classpath=classpath,
            expected_hashes=hashes, java_path=args.java)
    except (TypeError, ValueError) as exc:
        raise CliError(f"usage collection rejected: {_clean(exc)}") from exc
    except Exception as exc:  # collector failure is evidence, not a crash
        _append_stage(draft, "usage", "failed", {}, [f"usage collection failed: {_clean(exc)}"], [])
        return "failed"
    outcome = {"complete": "complete", "partial": "partial", "unavailable": "unavailable",
               "rejected": "failed", "failed": "failed"}.get(usage["status"], "partial")
    reasons = _outcome_reasons(
        [{"code": issue.get("code", "issue"), "detail": issue.get("message", "")}
         for issue in usage["issues"]])
    if outcome != "complete" and not reasons:
        reasons = [f"usage pipeline status is {usage['status']!r}"]
    evidence_id = _append_evidence(draft, {
        "id": _next_evidence_id(draft),
        "category": _USAGE_CATEGORY,
        "source_locator": repo,
        "observed_at": usage["observed_at"],
        "payload": _json_safe(usage),
    })
    _append_stage(draft, "usage", outcome,
                  {"status": usage["status"], "scope": usage["scope"],
                   "cleanup": usage["cleanup"],
                   "source_refs": len(usage["source_refs"])},
                  reasons, [evidence_id])
    return outcome


def _graph_history_stage(draft, repo, query):
    """Opt-in local graph history; collector budgets are separate, not a hard total.

    Only declaration paths already attached to selected subjects are supplied.
    Archive-only selections can compare imported trees without inventing POMs.
    """
    paths = _selected_pom_paths(draft, repo)
    used = {record["id"] for field in
            ("subjects", "evidence", "analyses", "operations", "qualifications")
            for record in draft[field]}
    identifiers = []
    index = len(draft["evidence"]) + 1
    while len(identifiers) < (3 if paths else 2):
        candidate = f"E{index}"
        if candidate not in used:
            identifiers.append(candidate)
            used.add(candidate)
        index += 1
    try:
        result = collect_graph_history(
            draft, repo, query, ancestry_evidence_id=identifiers[0],
            graph_evidence_id=identifiers[1],
            declaration_evidence_id=identifiers[2] if paths else None,
            pom_paths=paths, timeout_seconds=10, max_output_bytes=1024 * 1024,
            max_commits=64,
        )
    except HistoryPipelineError as exc:
        raise CliError(f"graph history rejected: {_clean(exc)}") from exc
    return result["draft"], result["stage"]["outcome"]


def _fresh_record_id(draft, prefix):
    used = {record['id'] for kind in ('subjects', 'evidence', 'analyses', 'operations', 'qualifications')
            for record in draft[kind]}
    index = 1
    while f'{prefix}{index}' in used:
        index += 1
    return f'{prefix}{index}'


def _inspect_shaded_archive(draft, path, repo, scratch, deadline):
    """Open the explicit file through no-follow ancestors; never reopen after validation."""
    directory = source = None
    try:
        directory = open_checkout_fd(os.path.dirname(path))
        source = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
        exchange = inspect_archive_pipeline(
            source, source_path=path, checkout=repo, scratch_base=scratch,
            deadline=deadline, limits=FileArchiveLimits())
    except (OSError, ValueError) as exc:
        exchange = {'outcome': 'failed', 'result': None,
                    'issues': [{'code': 'archive-input-unavailable'}]}
    finally:
        primary = sys.exception()
        close_error = None
        for fd in (source, directory):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as exc:
                    close_error = close_error or exc
        if close_error is not None:
            if primary is not None:
                primary.add_note('shaded input descriptor close unconfirmed')
            else:
                raise close_error
    evidence_id = _fresh_record_id(draft, 'E')
    _append_evidence(draft, {
        'id': evidence_id, 'category': 'shaded-archive-inspection',
        'source_locator': path, 'observed_at': _now(), 'payload': exchange,
    })
    if exchange.get('outcome') != 'complete':
        draft['qualifications'].append({
            'id': _fresh_record_id(draft, 'Q'), 'affected_evidence_ids': [evidence_id],
            'kind': 'incomplete-archive-inspection',
            'reason': 'archive capture, inspection or cleanup was incomplete; identity and coverage remain qualified',
            'observed_at': _now(),
        })
    return evidence_id, exchange


def _capture_shade_pom(repo, module):
    """Capture only the selected module's POM through held no-follow descriptors."""
    if not isinstance(module, str) or not module:
        return None, None, 'shade-module-unknown'
    parts = [] if module == '.' else module.split('/')
    path = os.path.join(repo, *parts, 'pom.xml')
    root = directory = None
    try:
        root = open_checkout_fd(repo)
        directory = open_module_dir(root, parts, path)
        captured = capture_at(directory, 'pom.xml', path, ConfigLimits().max_bytes)
        if not captured.complete:
            return path, captured, 'shade-pom-capture-incomplete'
        return path, captured, None
    except (OSError, ValueError, CaptureError):
        return path, None, 'shade-pom-unavailable'
    finally:
        primary = sys.exception()
        close_error = None
        for fd in (directory, root):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as exc:
                    close_error = close_error or exc
        if close_error is not None:
            if primary is not None:
                primary.add_note('shade POM descriptor close unconfirmed')
            else:
                raise close_error


def _shade_path_pairs(comparison):
    """Bound literal path candidates; each locator comes from an indexed inventory."""
    pairs, locators = [], []
    limit = ConfigLimits().max_paths
    surplus = {}
    for entry in comparison['surplus']:
        name = entry['entry_name']
        if entry['sha256'] is not None and entry['size_actual']:
            surplus.setdefault(name.rsplit('/', 1)[-1], []).append(entry)
    truncated = False
    for row in comparison['matches']:
        original = row['reference']
        if original['sha256'] is None or not original['size_actual']:
            continue
        packaged = [row['container']] if row['container'] is not None else surplus.get(
            original['entry_name'].rsplit('/', 1)[-1], [])
        for entry in packaged:
            if entry['sha256'] is None or not entry['size_actual']:
                continue
            if len(pairs) >= limit:
                truncated = True
                break
            pairs.append({'original': original['entry_name'], 'packaged': entry['entry_name']})
            locators.append({'original': original, 'packaged': entry,
                             'basis': 'observed-path-pair-candidate', 'relocation_proven': False})
        if truncated:
            break
    return pairs, locators, truncated


def _observe_shade_configuration(draft, captured_pom, module, comparison, source_ids):
    path, captured, issue = captured_pom
    observation = None
    pairs, locators, truncated = _shade_path_pairs(comparison)
    if issue is None:
        try:
            observation = inspect_shade_config(
                captured.data, source_path=path, module=module, observed_at=_now(),
                observed_paths=pairs)
        except ConfigError as exc:
            issue = f'shade-pom-rejected-{exc.code}'
    evidence_id = _fresh_record_id(draft, 'E')
    record = {
        'id': evidence_id, 'category': 'observed-shade-configuration',
        'source_locator': path or 'selected-module:unknown', 'observed_at': _now(),
        'payload': {'observation': observation, 'issue': issue,
                    'source_evidence_ids': list(source_ids),
                    'path_pair_observations': locators, 'path_pairs_truncated': truncated},
    }
    if captured is not None and captured.complete:
        record['captured_identity'] = {'path': captured.path, 'sha256': captured.sha256}
    inherited = [q for q in draft['qualifications']
                 if set(q['affected_evidence_ids']).intersection(source_ids)]
    record['payload']['limitations'] = [report_module._qualification_text(q) for q in inherited]
    _append_evidence(draft, record)
    for qualifier in inherited:
        draft['qualifications'].append(dict(
            qualifier, id=_fresh_record_id(draft, 'Q'), affected_evidence_ids=[evidence_id]))
    configured = observation is not None and bool(observation['configurations'])
    draft['qualifications'].append({
        'id': _fresh_record_id(draft, 'Q'), 'affected_evidence_ids': [evidence_id],
        'kind': 'unverified-shade-configuration',
        'reason': 'captured POM configuration and literal path pairs are observations only; '
                  'profile activation, management application, execution and enclosing build provenance '
                  'are unknown; no binary relocation is proven'
                  + (f'; {issue}' if issue else '')
                  + ('; path-pair coverage truncated' if truncated else ''),
        'observed_at': _now(),
    })
    return evidence_id, configured, issue


def _shaded_attribution(draft, candidate):
    """Link existing observations as leads only, without promoting model text to build proof."""
    coordinates = candidate.get('coordinates') or {}
    module, realm = candidate.get('module') or '.', candidate.get('realm', 'unknown')
    graphs, models = [], []
    for record in draft['evidence']:
        imported = record.get('payload', {}).get('import', {})
        if not isinstance(imported, dict):
            continue
        provenance = imported.get('provenance', {})
        if provenance.get('module') != module:
            continue
        imported_realm = imported.get('subject_realm', 'unknown')
        if realm != 'unknown' and imported_realm != realm:
            continue
        if record['category'] == 'imported-tree-json':
            if any(all(node.get('coordinates', {}).get(key) == coordinates.get(key)
                       for key in ('groupId', 'artifactId', 'version'))
                   for node in imported.get('evidence', {}).get('nodes', []) if isinstance(node, dict)):
                graphs.append(record['id'])
        elif record['category'] == 'imported-effective-pom':
            models.append(record['id'])
    return {
        'inclusion_graph': {'status': 'candidate-only' if graphs else 'unresolved', 'evidence_ids': graphs},
        'packaging_configuration': {
            'status': 'unresolved', 'model_evidence_ids': models,
            'reason': 'effective-model observations are leads; no shade configuration or enclosing build provenance is verified',
        },
        'limitations': ['graph and model links are candidate leads, never proof that this checkout produced the archive'],
    }


def _shaded_stage(draft, repo, references, run_dir):
    selected = set(draft['selection']['candidate_ids'])
    targets = []
    for evidence in draft['evidence']:
        candidate = evidence.get('payload', {}).get('candidate', {})
        if evidence['category'] != _DISCOVERY_MATCH or candidate.get('id') not in selected:
            continue
        if candidate.get('kind') != 'packaged-copy':
            continue
        for captured in candidate.get('copies', []):
            targets.append((evidence, candidate, captured))
    if len(targets) > 8:
        raise CliError('shaded collection supports at most eight selected packaged copies')
    if not targets:
        _append_stage(draft, 'shaded', 'not_applicable', {'comparisons': 0},
                      ['selected subjects have no observed packaged copies'], [])
        return 'not_applicable'
    reasons, evidence_ids, inspections = [], [], {}
    pom_captures = {}
    deadline = time.monotonic() + 120
    base = os.path.dirname(os.path.abspath(run_dir))
    _reject_checkout_overlap(base, repo, 'archive scratch base')
    with _archive_scratch(base) as scratch:
        paths = list(dict.fromkeys([os.path.abspath(copy['path']) for _, _, copy in targets] + references))
        for path in paths:
            if time.monotonic() >= deadline:
                reasons.append('shaded-stage-deadline-exceeded')
                break
            inspections[path] = _inspect_shaded_archive(draft, path, repo, scratch, deadline)
            evidence_ids.append(inspections[path][0])
            if inspections[path][1].get('outcome') != 'complete':
                reasons.append('archive-inspection-incomplete')
        comparisons = 0
        for origin, candidate, captured in targets:
            path = os.path.abspath(captured['path'])
            observation = inspections.get(path)
            if observation is None or not isinstance(observation[1].get('result'), dict):
                continue
            inventory = observation[1]['result']
            chain = captured.get('entry_chain')
            matches = [archive for archive in inventory['archives'] if archive['entry_chain'] == chain]
            if len(matches) != 1 or not captured.get('sha256') or matches[0].get('sha256') != captured['sha256']:
                reasons.append('selected-copy-changed')
                continue
            container = matches[0]
            for reference in references:
                ref_observation = inspections.get(reference)
                if ref_observation is None or not isinstance(ref_observation[1].get('result'), dict):
                    continue
                if time.monotonic() >= deadline:
                    reasons.append('shaded-stage-deadline-exceeded')
                    break
                try:
                    comparison = match_archives(inventory, ref_observation[1]['result'],
                                                container_archive_id=container['archive_id'])
                except ShadedError:
                    reasons.append('shaded-matcher-refused')
                    continue
                comparisons += 1
                if (comparison['verdict'] != 'complete-entry-content-correspondence'
                        or comparison['container_identity']['conflict'] or comparison['reference_identity']['conflict']):
                    reasons.append('entry-correspondence-incomplete-or-conflicting')
                module = candidate.get('module')
                if module not in pom_captures:
                    pom_captures[module] = _capture_shade_pom(repo, module)
                config_id, configured, config_issue = _observe_shade_configuration(
                    draft, pom_captures[module], module, comparison,
                    [origin['id'], observation[0], ref_observation[0]])
                evidence_ids.append(config_id)
                if config_issue:
                    reasons.append(config_issue)
                attribution = _shaded_attribution(draft, candidate)
                attribution['packaging_configuration'].update(
                    status='candidate-only' if configured else 'unresolved',
                    evidence_ids=[config_id], enclosing_build_verified=False,
                    reason='selected-module POM configuration is observed, not resolved or proven executed; '
                           'literal path pairs support candidate attribution only' if configured else
                           'no usable selected-module shade configuration was observed; attribution unresolved')
                sources = [origin['id'], observation[0], ref_observation[0], config_id,
                           *attribution['inclusion_graph']['evidence_ids'],
                           *attribution['packaging_configuration']['model_evidence_ids']]
                limitations = list(comparison['limitations']) + attribution['limitations']
                inherited = [q for q in draft['qualifications']
                             if set(q['affected_evidence_ids']).intersection(sources)]
                if inherited:
                    reasons.append('source-evidence-qualified')
                for qualifier in inherited:
                    limitations.append(report_module._qualification_text(qualifier))
                evidence_id = _fresh_record_id(draft, 'E')
                _append_evidence(draft, {
                    'id': evidence_id, 'category': 'shaded-entry-correspondence',
                    'subject_id': candidate['id'], 'source_locator': container['display_path'],
                    'observed_at': _now(), 'payload': {
                        'comparison': comparison, 'attribution': attribution,
                        'source_evidence_ids': sources, 'inspection_evidence_ids': [observation[0], ref_observation[0]],
                        'limitations': limitations,
                    },
                })
                evidence_ids.append(evidence_id)
                qualification = {
                    'id': _fresh_record_id(draft, 'Q'), 'affected_evidence_ids': [evidence_id],
                    'kind': 'candidate-correspondence',
                    'reason': 'entry-content correspondence is not artifact identity, proven relocation or build attribution',
                    'observed_at': _now(),
                }
                draft['qualifications'].append(qualification)
                for qualifier in inherited:
                    draft['qualifications'].append(dict(
                        qualifier, id=_fresh_record_id(draft, 'Q'), affected_evidence_ids=[evidence_id]))
    if comparisons == 0:
        reasons.append('no-comparable-archive-pairs')
    outcome = 'partial' if reasons else 'complete'
    _append_stage(draft, 'shaded', outcome,
                  {'comparisons': comparisons, 'target_copies': len(targets), 'references': len(references),
                   'deadline_seconds': 120, 'attribution': 'candidate-only-or-unresolved'},
                  list(dict.fromkeys(reasons)), evidence_ids)
    return outcome


def _distribution_options(args):
    requested = args.distribution or args.oci_layout
    if args.distribution and args.distribution_format is None:
        raise CliError('--distribution requires --distribution-format tar or tar.gz')
    if args.distribution_format and not args.distribution:
        raise CliError('--distribution-format requires --distribution')
    if args.oci_manifest and not args.oci_layout:
        raise CliError('--oci-manifest requires --oci-layout')
    if args.oci_manifest and not re.fullmatch(r'sha256:[0-9a-f]{64}', args.oci_manifest):
        raise CliError('--oci-manifest must be a lowercase sha256 digest')
    return os.path.abspath(requested) if requested else None


def _distribution_associations(draft, inventory, oci):
    """Metadata resemblance only; layer presence never establishes final image presence."""
    selected = set(draft['selection']['candidate_ids'])
    subjects = [(s['id'], (s.get('coordinates') or '').split(':'))
                for s in draft['subjects'] if s['id'] in selected]
    rows = []
    comparisons = 0
    layers = inventory.get('layers', []) if oci else [dict(distribution=inventory)]
    for layer in layers:
        for member in layer.get('distribution', {}).get('archives', []):
            for archive in member['inspection']['archives']:
                for coordinates in archive.get('identity', {}).get('candidates', []):
                    matches = []
                    observed = [coordinates.get(k) for k in ('groupId', 'artifactId', 'version')]
                    known = all(isinstance(value, str) and value and '${' not in value
                                and not any(char.isspace() for char in value) for value in observed)
                    for subject_id, parts in subjects:
                        comparisons += 1
                        if comparisons > 100000:
                            return rows, 'partial', 'association-limit'
                        if known and len(parts) == 3 and parts == observed:
                            matches.append(subject_id)
                    if len(rows) >= 10000:
                        return rows, 'partial', 'association-limit'
                    rows.append(dict(layer_index=layer.get('index'), member_index=member['member_index'],
                        archive_id=archive['archive_id'], entry_chain=archive['entry_chain'],
                        coordinates={k: coordinates[k] for k in ('groupId', 'artifactId', 'version')},
                        candidate_ids=matches, realm='unknown', basis='metadata-candidate-only',
                        metadata_conflict=archive['identity'].get('conflict') is True))
    status = 'partial' if not rows or any(len(row['candidate_ids']) != 1 or row['metadata_conflict']
                                        for row in rows) else 'complete'
    return rows, status, None


def _oci_visibility_evidence(draft, source, inventory):
    """Project supported layer visibility without changing raw observations."""
    visibility = None
    issues = []
    if inventory is None:
        issues.append({'code': 'oci-visibility-unavailable'})
    else:
        try:
            visibility = compose_visibility(inventory)
        except VisibilityError:
            issues.append({'code': 'oci-visibility-invalid-inventory'})
    associations = []
    association_status = 'unavailable'
    if visibility is not None:
        issues.extend(visibility['issues'])
        if visibility['coverage']['status'] == 'complete-within-recorded-layers':
            selected = set(draft['selection']['candidate_ids'])
            subjects = [(subject['id'], (subject.get('coordinates') or '').split(':'))
                        for subject in draft['subjects'] if subject['id'] in selected]
            candidates = visibility['java_candidates']
            if len(candidates) > 10000 or len(candidates) * len(subjects) > 100000:
                issues.append({'code': 'association-limit'})
                association_status = 'partial'
            else:
                for candidate in candidates:
                    observed = [candidate['coordinates'].get(key)
                                for key in ('groupId', 'artifactId', 'version')]
                    known = all(isinstance(value, str) and value and '${' not in value
                                and not any(char.isspace() for char in value) for value in observed)
                    matches = [subject_id for subject_id, parts in subjects
                               if known and parts == observed]
                    associations.append(dict(path=candidate['path'], layer_index=candidate['layer_index'],
                        member_index=candidate['member_index'], archive_id=candidate['archive_id'],
                        entry_chain=copy.deepcopy(candidate['entry_chain']),
                        coordinates={key: candidate['coordinates'][key]
                                     for key in ('groupId', 'artifactId', 'version')},
                        candidate_ids=matches, realm='unknown',
                        basis='visible-member-metadata-candidate-only'))
                association_status = ('complete' if associations and all(
                    len(row['candidate_ids']) == 1 for row in associations) else 'partial')
        else:
            association_status = 'partial'
    evidence_id = _fresh_record_id(draft, 'E')
    inherited = [q for q in draft['qualifications'] if source['id'] in q['affected_evidence_ids']]
    limitations = [report_module._qualification_text(q) for q in inherited]
    reason = ('visibility is derived only from the recorded supported layer inventories; '
              'unsupported changes keep visibility uncertain, and metadata matches do not '
              'prove component binary identity, application or container realm, project '
              'inclusion, definitive absence or a runnable merged filesystem')
    qualifier = dict(id=_fresh_record_id(draft, 'Q'), affected_evidence_ids=[evidence_id],
                    kind='recorded-layer-visibility', reason=reason, observed_at=_now())
    limitations.append(report_module._qualification_text(qualifier))
    _append_evidence(draft, dict(id=evidence_id, category='oci-visibility',
        source_locator=source['source_locator'], observed_at=_now(), payload=dict(
            source_evidence_ids=[source['id']], visibility=visibility, issues=issues,
            associations=associations, association_status=association_status,
            merged_filesystem=False, limitations=limitations)))
    draft['qualifications'].append(qualifier)
    for original in inherited:
        draft['qualifications'].append(dict(id=_fresh_record_id(draft, 'Q'),
            affected_evidence_ids=[evidence_id], kind=original['kind'],
            reason=original['reason'], observed_at=original['observed_at']))
    return evidence_id, visibility, association_status, issues


def _distribution_stage(draft, repo, args, run_dir, path):
    """Inspect one explicit local input; retain independent cleanup obligations."""
    oci = args.oci_layout is not None
    deadline = time.monotonic() + 120
    base = os.path.dirname(os.path.abspath(run_dir))
    _reject_checkout_overlap(base, repo, 'distribution state base', both_directions=True)
    if oci:
        _reject_checkout_overlap(base, path, 'OCI state base', both_directions=True)
    handles = {}
    exchange = None
    quota = None
    scratch_path = None
    scratch_identity = None
    stopped = True
    removed = False
    cleanup = {'leftovers': [], 'individual_paths_complete': True}
    issues = []
    source_identity = None
    close_failed = False
    try:
        # Descriptor-safe opening prevents a source symlink from redirecting reads.
        try:
            if oci:
                handles['source'] = open_checkout_fd(path)
            else:
                handles['directory'] = open_checkout_fd(os.path.dirname(path))
                handles['source'] = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                            dir_fd=handles['directory'])
                initial = os.fstat(handles['source'])
                if not stat.S_ISREG(initial.st_mode):
                    raise ValueError('not a regular source')
        except (OSError, ValueError):
            raise CliError('distribution source must be an existing no-follow regular file or OCI directory') from None
        with _archive_scratch(base) as scratch:
            quota, refusal = _quota_inventory(repo, scratch, deadline, DEFAULT_QUOTA_BYTES,
                                              4 * 1024**3, MAX_QUOTA_ENTRIES)
            if refusal:
                issues.append({'code': refusal})
                cleanup = {'leftovers': [{'path': scratch, 'reason': refusal}],
                           'individual_paths_complete': False}
            else:
                handles['scratch_base'] = open_checkout_fd(scratch)
                name = 'mdi-spool-' + str(uuid.uuid4())
                scratch_path = os.path.join(scratch, name)
                os.mkdir(name, mode=0o700, dir_fd=handles['scratch_base'])
                handles['scratch'] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                             dir_fd=handles['scratch_base'])
                st = os.fstat(handles['scratch'])
                scratch_identity = (st.st_dev, st.st_ino)
                try:
                    if not oci:
                        # Hash the held source with bounded reads; the worker verifies it again.
                        limit = DistributionLimits().max_input_bytes
                        if initial.st_size > limit:
                            raise ValueError('input-limit')
                        digest, offset = hashlib.sha256(), 0
                        while offset <= limit:
                            if time.monotonic() >= deadline:
                                raise TimeoutError()
                            chunk = os.pread(handles['source'], min(65536, limit + 1 - offset), offset)
                            if not chunk:
                                break
                            digest.update(chunk)
                            offset += len(chunk)
                        final = os.fstat(handles['source'])
                        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                        if offset > limit or offset != initial.st_size or identity(initial) != identity(final):
                            raise ValueError('source-changed-or-over-limit')
                        source_identity = {'path': path, 'sha256': digest.hexdigest(), 'size': offset}
                    stopped = False
                    if oci:
                        exchange = inspect_oci_isolated(handles['source'], layout_path=path,
                            checkout=repo, scratch_base=scratch_path, deadline=deadline,
                            manifest_digest=args.oci_manifest, limits=OCILimits())
                    else:
                        exchange = inspect_distribution_isolated(handles['source'], source_path=path,
                            sha256=source_identity['sha256'], checkout=repo, scratch_base=scratch_path,
                            format=args.distribution_format, deadline=deadline, limits=DistributionLimits())
                    cleanup = exchange['cleanup']
                    lifecycle = exchange['cleanup_result']
                    stopped = (lifecycle.get('worker_stopped') is True and lifecycle.get('leader_reaped') is True
                               and lifecycle.get('status') == 'confirmed')
                    if exchange.get('error'):
                        issues.append({'code': exchange['error']})
                except BaseException as exc:
                    metadata = getattr(exc, 'oci_cleanup' if oci else 'distribution_cleanup', None)
                    if isinstance(metadata, dict):
                        cleanup = metadata
                        lifecycle = metadata.get('worker', {})
                        stopped = lifecycle.get('worker_stopped') is True and lifecycle.get('leader_reaped') is True
                    if not isinstance(exc, Exception):
                        raise
                    issues.append({'code': 'distribution-inspection-failed'})
                finally:
                    primary = sys.exception()
                    cleanup_error = None
                    # Only the empty, unchanged directory owned here may be removed.
                    if stopped and not cleanup.get('leftovers'):
                        try:
                            held = os.fstat(handles['scratch'])
                            named = os.stat(name, dir_fd=handles['scratch_base'], follow_symlinks=False)
                            if (held.st_dev, held.st_ino) == scratch_identity == (named.st_dev, named.st_ino):
                                os.rmdir(name, dir_fd=handles['scratch_base'])
                                removed = True
                        except BaseException as exc:
                            # Removal may have happened before the exception. Never
                            # retry it; retain uncertainty without masking the worker.
                            cleanup_error = exc
                            if primary is not None:
                                primary.add_note('distribution scratch cleanup unconfirmed')
                    if not removed:
                        cleanup = dict(cleanup, leftovers=list(cleanup.get('leftovers', [])))
                        if len(cleanup['leftovers']) < 32:
                            cleanup['leftovers'].append({'path': scratch_path, 'reason': 'distribution-scratch-retained'})
                        cleanup['individual_paths_complete'] = False
                        cleanup['recovery_root'] = scratch_path
                    if primary is not None:
                        primary.collection_cleanup = cleanup
                    elif cleanup_error is not None and not isinstance(cleanup_error, Exception):
                        cleanup_error.collection_cleanup = cleanup
                        raise cleanup_error
    except (OSError, ValueError):
        if scratch_path is None:
            raise
        issues.append({'code': 'distribution-scratch-unconfirmed'})
        if not removed:
            cleanup = {'leftovers': [{'path': scratch_path, 'reason': 'distribution-scratch-unconfirmed'}],
                       'individual_paths_complete': False}
    finally:
        primary = sys.exception()
        for key in list(handles):
            fd = handles.pop(key)
            try:
                os.close(fd)
            except BaseException as exc:
                close_failed = True
                if primary is not None:
                    primary.add_note('distribution input descriptor close unconfirmed')
                elif not isinstance(exc, Exception):
                    exc.collection_cleanup = cleanup
                    primary = exc
        if primary is not None:
            if scratch_path and not removed:
                primary.collection_cleanup = dict(cleanup, recovery_root=scratch_path)
            if sys.exception() is None:
                raise primary
    if close_failed:
        issues.append({'code': 'distribution-descriptor-close-unconfirmed'})
    inventory = exchange.get('result') if isinstance(exchange, dict) else None
    associations, association_status, association_issue = _distribution_associations(draft, inventory or {}, oci)
    if association_issue:
        issues.append({'code': association_issue})
    if inventory:
        issues.extend(inventory.get('issues', []))
    if oci:
        issues.append({'code': 'layer-observations-not-final-filesystem'})
    if association_status != 'complete':
        issues.append({'code': 'component-association-unresolved'})
    if cleanup.get('leftovers'):
        issues.append({'code': 'distribution-scratch-retained'})
    complete = (not oci and exchange is not None and exchange.get('outcome') == 'complete'
                and inventory is not None and inventory.get('coverage', {}).get('status') == 'complete'
                and not issues)
    outcome = 'complete' if complete else 'partial'
    evidence_id = _fresh_record_id(draft, 'E')
    record = dict(id=evidence_id, category='distribution-inspection', source_locator=path,
        observed_at=_now(), payload=dict(kind='oci-layout' if oci else args.distribution_format,
            exchange=exchange, quota=quota, cleanup=cleanup, associations=associations,
            association_status=association_status, merged_filesystem=False,
            association_scope='recorded-layer-members' if oci else 'recorded-distribution-members',
            limitations=['caller-supplied local inventory, not authenticated build provenance',
                         'metadata association does not prove component binary identity or project realm',
                         'layer-observations-not-final-filesystem' if oci else 'distribution contents are not checkout resolution',
                         'parent hashing and filesystem IO are cooperative; child processing is deadline-bounded']))
    if source_identity:
        record['captured_identity'] = {'path': path, 'sha256': source_identity['sha256']}
    _append_evidence(draft, record)
    draft['qualifications'].append(dict(id=_fresh_record_id(draft, 'Q'),
        affected_evidence_ids=[evidence_id], kind='unverified-distribution-attribution',
        reason='local distribution or layer inventory does not prove project inclusion, component binary identity or a merged image filesystem',
        observed_at=_now()))
    evidence_ids = [evidence_id]
    visibility_status = None
    visible_association_status = None
    if oci:
        try:
            visibility_id, visibility, visible_association_status, visibility_issues = _oci_visibility_evidence(
                draft, record, inventory if exchange is not None and exchange.get('outcome') == 'complete' else None)
        except BaseException as exc:
            exc.collection_cleanup = cleanup
            raise
        evidence_ids.append(visibility_id)
        visibility_status = visibility['coverage']['status'] if visibility is not None else 'unavailable'
        issues.extend(visibility_issues)
    _append_stage(draft, 'distribution', outcome,
        dict(kind='oci-layout' if oci else args.distribution_format, merged_filesystem=False,
             association_status=association_status, cleanup=cleanup,
             visibility_status=visibility_status, visible_association_status=visible_association_status),
        list(dict.fromkeys(issue['code'] for issue in issues)), evidence_ids)
    return outcome


def _cmd_collect(args):
    store = _open_run(args.run_dir)
    current = _checked_load(store, args.expect_revision)
    if current["selection"] is None:
        raise CliError("collect requires a recorded selection; run 'select' first")
    context = _find_context(current)
    payload = context["payload"]
    repo = payload["repo"]
    query = payload["query"]["raw"]
    distribution_path = _distribution_options(args)
    references = args.reference_jars
    if references and len(references) > 8:
        raise CliError('shaded collection supports at most eight reference JARs')
    references = list(dict.fromkeys(os.path.abspath(path) for path in references)) if references else []
    draft = copy.deepcopy(current)
    if distribution_path:
        distribution_outcome = _distribution_stage(draft, repo, args, store.path, distribution_path)
    try:
        if references:
            shaded_outcome = _shaded_stage(draft, repo, references, store.path)
        history_outcome = _history_stage(draft, repo, query)
        usage_outcome = _usage_stage(draft, repo, args, store.path)
        if args.graph_history:
            draft, graph_outcome = _graph_history_stage(draft, repo, query)
        persisted = store.commit(draft, current["revision"])
    except BaseException as exc:
        if distribution_path:
            evidence = next(e for e in reversed(draft['evidence']) if e['category'] == 'distribution-inspection')
            exc.collection_cleanup = evidence['payload']['cleanup']
        raise
    result = {
        "ok": True,
        "command": "collect",
        "run_id": store.run_id,
        "run_dir": store.path,
        "revision": persisted["revision"],
        "query": query,
        "stages": [{"name": stage["name"], "outcome": stage["outcome"]}
                   for stage in persisted["stages"]
                   if stage["name"] in (("history", "usage", "graph-history")
                                        if args.graph_history else ("history", "usage"))],
        "history_outcome": history_outcome,
        "usage_outcome": usage_outcome,
    }
    if distribution_path:
        result['distribution_outcome'] = distribution_outcome
        result['stages'].extend({'name': s['name'], 'outcome': s['outcome']}
                                for s in persisted['stages'] if s['name'] == 'distribution')
    if args.graph_history:
        result["graph_history_outcome"] = graph_outcome
    if references:
        result['shaded_outcome'] = shaded_outcome
        result['stages'].extend({'name': s['name'], 'outcome': s['outcome']}
                                for s in persisted['stages'] if s['name'] == 'shaded')
    return _emit(result)


# ---------------------------------------------------------------------------
# import


def _default_provenance(kind):
    """The pinned parser grammar for `kind`, spelled as a provenance dict.

    These values are the grammar the pinned importer accepts, NOT an
    observation about the report: the caller supplies real provenance via
    --provenance, and anything left unattested stays explicitly labeled as
    an assumption in the evidence (see _cmd_import).
    """
    pinned = {
        "tree-json": ("maven-dependency-plugin", "3.11.0"),
        "effective-pom": ("maven-help-plugin", "3.5.2"),
        "classpath": ("maven-dependency-plugin", "3.11.0"),
    }
    producer, version = pinned[kind]
    provenance = {name: None for name in PROVENANCE_FIELDS}
    provenance["producer"] = producer
    provenance["version"] = version
    return provenance


def _load_provenance(path):
    try:
        captured = capture_file(path, _PROVENANCE_MAX_BYTES)
    except CaptureError as exc:
        raise CliError(f"cannot capture provenance file: {_clean(exc)}") from exc
    if not captured.complete:
        raise CliError(f"provenance file was not captured completely: "
                       f"{captured.reason or 'incomplete capture'}")
    try:
        supplied = json.loads(captured.data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CliError(f"provenance file is not valid JSON: {_clean(exc)}") from exc
    if not isinstance(supplied, dict):
        raise CliError("provenance file must hold a JSON object")
    unknown = sorted(set(supplied) - set(PROVENANCE_FIELDS))
    if unknown:
        raise CliError(f"provenance file has unknown fields {unknown}; the accepted "
                       f"fields are {sorted(PROVENANCE_FIELDS)}")
    return supplied


def _runtime_associations(current, imported):
    selected = set((current["selection"] or {}).get("candidate_ids", []))
    subjects = [subject for subject in current["subjects"] if subject["id"] in selected]
    if len(subjects) * len(imported["observations"]) > 100000:
        raise CliError("runtime subject comparison exceeds 100000 pairs; narrow the selection "
                       "or import a smaller observation document")
    discovered = {record["subject_id"]: record["payload"]["candidate"]
                  for record in current["evidence"] if record["category"] == _DISCOVERY_MATCH
                  and record.get("subject_id") in selected}
    for subject in subjects:
        if subject["id"] not in discovered:
            raise CliError("selected runtime subject has no discovery evidence")

    def known(value):
        return isinstance(value, str) and bool(value) and "${" not in value

    associations = []
    for observation in imported["observations"]:
        claim = observation["claim"]
        observed = claim["subject"]
        candidates, uncertain = [], []
        for subject in subjects:
            expected = discovered[subject["id"]].get("coordinates") or {}
            actual = observed["coordinates"] or {}
            pairs = [(observed["module"], subject.get("module")),
                     (None if observed["realm"] == "unknown" else observed["realm"],
                      None if subject["realm"] == "unknown" else subject["realm"])]
            pairs.extend((actual.get(key), expected.get(key))
                         for key in ("groupId", "artifactId", "version"))
            if any(known(left) and known(right) and left != right for left, right in pairs):
                continue
            if all(known(left) and known(right) for left, right in pairs):
                candidates.append(subject["id"])
            else:
                uncertain.append(subject["id"])
        if not subjects:
            status = "unselected"
        elif len(candidates) == 1 and not uncertain:
            status = "candidate-match"
        elif candidates:
            status = "ambiguous"
        elif uncertain:
            status = "unknown"
        else:
            status = "mismatch"
        associations.append({"observation_id": claim["id"], "status": status,
                             "candidate_ids": candidates, "possible_candidate_ids": uncertain,
                             "basis": "caller-attested runtime identity compared with discovery "
                                      "metadata; not verified binary or build identity"})
    return associations


def _import_runtime(args, store, current, captured):
    if args.tree_mode is not None or args.provenance is not None:
        raise CliError("runtime-observations carries its own caller-attested provenance; "
                       "--provenance and --tree-mode are not applicable")
    try:
        imported = import_runtime_observations(captured.data)
    except RuntimeEvidenceError as exc:
        raise CliError(f"runtime import rejected ({exc.code})") from exc
    associations = _runtime_associations(current, imported)
    reasons = [issue["code"] for issue in imported["issues"]]
    reasons.extend(f"runtime-subject-{status}" for status in sorted({
        association["status"] for association in associations
        if association["status"] != "candidate-match"
    }))
    outcome = "partial" if reasons else "complete"
    draft = copy.deepcopy(current)
    evidence_id = _next_evidence_id(draft)
    now = _now()
    _append_evidence(draft, {
        "id": evidence_id, "category": "imported-runtime-observations",
        "source_locator": captured.path, "observed_at": now,
        "captured_identity": {"path": captured.path, "sha256": imported["input"]["sha256"]},
        "generation_context": {"attestation": "caller-attested",
                               "workload": imported["document"]["workload"],
                               "scope": imported["document"]["scope"]},
        "payload": {"import": imported, "associations": associations},
    })
    reason = "; ".join(imported["coverage"]["limitations"])
    reason += "; subject associations are metadata candidates, not verified binary identity"
    if reasons:
        reason += "; missing or unmatched coverage: " + ", ".join(reasons)
    draft["qualifications"].append({
        "id": _next_qualification_id(draft), "affected_evidence_ids": [evidence_id],
        "kind": "caller-attested-runtime", "reason": reason, "observed_at": now,
    })
    coverage = dict(imported["coverage"],
                    scope=imported["document"]["scope"],
                    selection_associations=associations,
                    provenance_attestation="caller-attested")
    _append_stage(draft, "import-runtime-observations", outcome, coverage, reasons, [evidence_id])
    persisted = store.commit(draft, current["revision"])
    return _emit({
        "ok": True, "command": "import", "kind": args.kind,
        "run_id": store.run_id, "run_dir": store.path, "revision": persisted["revision"],
        "evidence_id": evidence_id, "provenance_attestation": "caller-attested",
        "input": imported["input"], "stage": {"name": "import-runtime-observations",
                                               "outcome": outcome},
        "issues": reasons,
    })


def _import_forge(args, store, current, captured):
    """Retain a caller-exported discussion without inferring subject membership."""
    if args.provenance is not None or args.tree_mode is not None or args.realm is not None:
        raise CliError("forge-evidence carries its own caller-attested provenance; "
                       "--provenance, --tree-mode and --realm are not applicable")
    try:
        imported = import_forge_evidence(captured.data)
    except ForgeEvidenceError as exc:
        raise CliError(f"forge import rejected ({exc.code})") from exc
    reasons = [issue["code"] for issue in imported["issues"]]
    outcome = "partial" if reasons else "complete"
    draft = copy.deepcopy(current)
    evidence_id = _next_evidence_id(draft)
    now = _now()
    _append_evidence(draft, {
        "id": evidence_id, "category": "imported-forge-evidence",
        "source_locator": captured.path, "observed_at": now,
        "captured_identity": {"path": captured.path, "sha256": imported["input"]["sha256"]},
        "generation_context": {"attestation": "caller-attested",
                               "origin": imported["origin"],
                               "repository": imported["repository"],
                               "retrieval": imported["retrieval"]},
        "payload": {"import": imported, "repository_membership_verified": False,
                    "subject_associations": []},
    })
    reason = "; ".join(imported["limitations"])
    reason += "; repository membership and links to selected dependencies were not verified"
    draft["qualifications"].append({
        "id": _next_qualification_id(draft), "affected_evidence_ids": [evidence_id],
        "kind": "caller-attested-forge", "reason": reason, "observed_at": now,
    })
    coverage = dict(imported["coverage"], provenance_attestation="caller-attested",
                    repository_membership_verified=False, subject_associations=[])
    _append_stage(draft, "import-forge-evidence", outcome, coverage, reasons, [evidence_id])
    persisted = store.commit(draft, current["revision"])
    return _emit({
        "ok": True, "command": "import", "kind": args.kind,
        "run_id": store.run_id, "run_dir": store.path, "revision": persisted["revision"],
        "evidence_id": evidence_id, "provenance_attestation": "caller-attested",
        "input": imported["input"],
        "stage": {"name": "import-forge-evidence", "outcome": outcome}, "issues": reasons,
    })


def _cmd_import(args):
    store = _open_run(args.run_dir)
    current = _checked_load(store, args.expect_revision)
    try:
        captured = capture_file(args.report, _IMPORT_MAX_BYTES)
    except CaptureError as exc:
        raise CliError(f"cannot capture report file: {_clean(exc)}") from exc
    if not captured.complete:
        raise CliError(f"report file was not captured completely: "
                       f"{captured.reason or 'incomplete capture'}")
    if args.kind == "forge-evidence":
        return _import_forge(args, store, current, captured)
    if args.kind == "runtime-observations":
        if args.realm is not None:
            raise CliError("--realm attests a Maven report; runtime scope belongs in the observation document")
        return _import_runtime(args, store, current, captured)
    supplied = _load_provenance(args.provenance) if args.provenance else None
    if args.tree_mode is not None and args.kind != "tree-json":
        raise CliError("--tree-mode attests a recorded dependency tree and applies "
                       "to --kind tree-json only")
    if supplied is not None and args.tree_mode is not None:
        attested_format = supplied.get("format")
        if attested_format in ("verbose", "nonverbose") \
                and attested_format != args.tree_mode:
            raise CliError(
                f"--tree-mode {args.tree_mode!r} contradicts the provenance format "
                f"{attested_format!r}: refusing to resolve the conflict silently; fix "
                f"the attestation or the provenance file")
    # Conservative tree mode: the diagnostic verbose reading is the default,
    # and the authoritative nonverbose mode requires an explicit attestation
    # through the flag or a recognized provenance 'format' value; omitting
    # the flag never silently selects inclusion authority, and a flag never
    # silently overrides a contradicting attestation.
    tree_mode, tree_attestation = "verbose", "default"
    if args.tree_mode is not None:
        tree_mode, tree_attestation = args.tree_mode, "flag"
    elif supplied is not None and supplied.get("format") in ("verbose", "nonverbose"):
        tree_mode, tree_attestation = supplied["format"], "provenance"
    grammar = _default_provenance(args.kind)
    provenance = dict(grammar)
    if supplied:
        # explicit null producer/version behave exactly like omitted keys: the
        # pinned grammar still parses, while the attestation logic sees them as
        # unknown and the caller's nulls stay visible in supplied_provenance
        provenance.update({key: value for key, value in supplied.items()
                           if not (key in ("producer", "version") and value is None)})
    # Grammar pin vs attested fact: producer/version the caller did not attest
    # are assumptions that select the pinned parser and are never presented as
    # observations; everything the caller did supply stays caller-attested and
    # unverified.
    assumed_grammar = {name: grammar[name] for name in ("producer", "version")
                       if supplied is None or supplied.get(name) is None}
    attestation = "caller-attested" if supplied else "unknown"
    try:
        if args.kind == "tree-json":
            imported = import_dependency_tree(captured.data, provenance=provenance,
                                              verbose=tree_mode == "verbose", realm=args.realm)
        elif args.kind == "effective-pom":
            imported = import_effective_pom(captured.data, provenance=provenance,
                                            pom_locator=captured.path, realm=args.realm)
        else:
            imported = import_classpath(captured.data, provenance=provenance, realm=args.realm)
    except MavenImportError as exc:
        raise CliError(f"import rejected ({exc.code}): {_clean(exc)}") from exc
    except (TypeError, ValueError) as exc:
        raise CliError(f"import rejected: {_clean(exc)}") from exc

    generation_context = {
        "attestation": attestation,
        "supplied_provenance": copy.deepcopy(supplied),
        "assumed_grammar": assumed_grammar or None,
        **({"realm_attestation": "caller-attested", "realm": args.realm}
           if args.realm is not None else {}),
        "note": "producer/version values under assumed_grammar were assumed from the "
                "pinned parser grammar, not observed; supplied_provenance fields are "
                "caller-attested and were not verified against the generating process",
    }
    draft = copy.deepcopy(current)
    evidence_id = _next_evidence_id(draft)
    record = {
        "id": evidence_id,
        "category": _IMPORT_CATEGORIES[args.kind],
        "source_locator": captured.path,
        "observed_at": imported["imported_at"],
        "captured_identity": {"path": captured.path,
                              "sha256": imported["input"]["sha256"]},
        "generation_context": generation_context,
        # labeled wrapper, never a naked assumed observation nested as fact
        "payload": {"import": imported,
                    "provenance_attestation": attestation,
                    "assumed_grammar": assumed_grammar or None},
    }
    if supplied is not None and supplied.get("producer") is not None:
        record["producer"] = supplied["producer"]
    if supplied is not None and supplied.get("version") is not None:
        record["producer_version"] = supplied["version"]
    _append_evidence(draft, record)
    if assumed_grammar:
        kind, reason = "assumed-producer-grammar", (
            f"producer/version {sorted(assumed_grammar.values())} were not attested for "
            f"this report; the pinned grammar was assumed so the pinned parser could "
            f"read it, and the actual generating producer and version remain unknown")
    else:
        kind, reason = "caller-attested-provenance", (
            "the supplied provenance is caller-attested only and was not verified "
            "against the generating process")
    if supplied is not None and assumed_grammar:
        reason += "; the supplied provenance fields are caller-attested, not verified"
    if args.realm is not None:
        reason += "; the report realm is caller-attested, not independently verified"
    draft["qualifications"].append({
        "id": _next_qualification_id(draft),
        "affected_evidence_ids": [evidence_id],
        "kind": kind,
        "reason": reason,
        "observed_at": _now(),
    })
    _append_stage(draft, f"import-{args.kind}", "complete",
                  {"currency": imported["context"]["currency"],
                   "provenance_attestation": attestation,
                   "assumed_grammar": assumed_grammar or None,
                   **({"tree_mode": tree_mode,
                       "tree_mode_attestation": tree_attestation}
                      if args.kind == "tree-json" else {}),
                   "current_build_attribution": "none: no current context was supplied; "
                   "the evidence is authoritative only for the context its provenance "
                   "records"},
                  [], [evidence_id])
    persisted = store.commit(draft, current["revision"])
    return _emit({
        "ok": True,
        "command": "import",
        "run_id": store.run_id,
        "run_dir": store.path,
        "revision": persisted["revision"],
        "kind": args.kind,
        "evidence_id": evidence_id,
        "provenance_attestation": attestation,
        "assumed_grammar": assumed_grammar or None,
        "supplied_provenance": supplied,
        **({"tree_mode": tree_mode, "tree_mode_attestation": tree_attestation}
           if args.kind == "tree-json" else {}),
        "currency": imported["context"]["currency"],
        "input": imported["input"],
    })


# ---------------------------------------------------------------------------
# report and unavailable verbs


def _cmd_report(args):
    store = _open_run(args.run_dir)
    if args.revision is not None and args.revision < 0:
        raise CliError(f"--revision must not be negative, got {args.revision}")
    try:
        stored = store.load(args.revision)
    except RunStoreError as exc:
        raise CliError(f"cannot load report revision: {_clean(exc)}") from exc
    if not args.validate:
        return _emit(stored)
    try:
        report_module.validate_report(stored)
        problem = None
    except report_module.ReportError as exc:
        problem = _clean(exc)
    document = {
        "ok": problem is None,
        "command": "report",
        "run_dir": store.path,
        "validation": {"valid": problem is None, "problem": problem,
                       "revision": stored["revision"], "run_id": stored["run_id"],
                       "checks": ["schema", "references", "unique-ids", "transition-chain"]},
        "report": stored,
    }
    return _emit(document, EXIT_OK if problem is None else EXIT_UNEXPECTED)


# ---------------------------------------------------------------------------
# propose and execute


def _selected_candidate(current, candidate_id):
    """Return the selected candidate id after exact validation, else exit 2."""
    if current["selection"] is None:
        raise CliError("propose requires a recorded selection; run 'select' first")
    selected = current["selection"]["candidate_ids"]
    if candidate_id not in selected:
        raise CliError(
            f"candidate {candidate_id!r} is not part of the recorded selection "
            f"{selected}; an operation may only target the selected candidate")
    return candidate_id


def _candidate_record(current, candidate_id):
    """The discovery candidate record behind one subject id."""
    for record in current["evidence"]:
        if record["category"] == _DISCOVERY_MATCH and record.get("subject_id") == candidate_id:
            return record["payload"]["candidate"]
    raise CliError(f"candidate {candidate_id!r} has no discovery evidence")


def _candidate_coordinates(current, candidate_id):
    """Known coordinates of one candidate, as observed at discovery time."""
    return _candidate_record(current, candidate_id).get("coordinates") or {}


def _require_absolute(value, flag):
    if not os.path.isabs(value):
        raise CliError(f"{flag} must be an exact absolute path; it is never resolved "
                       f"from PATH or made relative, got {value!r}")
    return value


def _cmd_propose(args):
    store = _open_run(args.run_dir)
    current = _checked_load(store, args.expect_revision)
    if args.kind == "raw-capture":
        missing = [flag for flag, value in (
            ("--source-operation", args.source_operation),
            ("--max-capture-bytes", args.max_capture_bytes)) if value is None]
        if missing:
            raise CliError(f"a raw-capture proposal requires {', '.join(missing)}")
        source_only = [flag for flag, value in (
            ("--candidate", args.candidate), ("--maven", args.maven),
            ("--output", args.output), ("--input", args.inputs),
            ("--destination", args.destinations), ("--page-token", args.page_token))
            if value is not None]
        if args.output_overwrite:
            source_only.append("--output-overwrite")
        if args.destinations_unknown:
            source_only.append("--destinations-unknown")
        if source_only:
            raise CliError(f"raw-capture proposals take no source parameters "
                           f"({', '.join(source_only)})")
        operation = raw_capture_consent.build_capture_proposal(
            current, args.source_operation, max_bytes=args.max_capture_bytes,
            tool_identity=_CAPTURE_TOOL_IDENTITY)
    else:
        if args.candidate is None:
            raise CliError(f"a {args.kind} proposal requires --candidate")
        if args.source_operation is not None or args.max_capture_bytes is not None:
            raise CliError("--source-operation and --max-capture-bytes are only "
                           "valid for raw-capture proposals")
        _selected_candidate(current, args.candidate)
    if args.kind == "advisory":
        maven_only = [flag for flag, value in (("--maven", args.maven),
                                               ("--output", args.output),
                                               ("--input", args.inputs),
                                               ("--destination", args.destinations),
                                               ("--destinations-unknown", args.destinations_unknown))
                      if value]
        if maven_only:
            raise CliError(f"advisory proposals take no Maven parameters "
                           f"({', '.join(maven_only)})")
        if args.page_token is not None and not args.page_token:
            raise CliError("--page-token must be a nonempty string when supplied")
        coordinates = _candidate_coordinates(current, args.candidate)
        parts = {name: coordinates.get(name) for name in ("groupId", "artifactId", "version")}
        unknown = [name for name, value in parts.items() if not value]
        if unknown:
            raise CliError(
                f"an advisory operation queries one exact resolved coordinate; "
                f"candidate {args.candidate!r} lacks known {unknown} as observed at "
                f"discovery time (raw placeholders are not resolved values)")
        try:
            operation = advisory_proposal(store, parts["groupId"], parts["artifactId"],
                                          parts["version"], page_token=args.page_token)
        except (AdvisoryError, operations.OperationsError) as exc:
            raise CliError(f"advisory proposal rejected: {_clean(exc)}") from exc
    elif args.kind in _MAVEN_PLUGIN_GOALS:
        missing = [flag for flag, value in (("--maven", args.maven),
                                            ("--output", args.output),
                                            ("--input", args.inputs))
                   if not value]
        if missing:
            raise CliError(f"a {args.kind} diagnostic proposal requires explicit "
                           f"{', '.join(missing)}")
        if args.destinations_unknown and args.destinations:
            raise CliError("pass either --destinations-unknown or explicit "
                           "--destination items, not both")
        if not args.destinations_unknown and not args.destinations:
            raise CliError("download destinations must be declared explicitly: pass "
                           "--destinations-unknown or at least one --destination; "
                           "unknown is never inferred from absent flags")
        destinations = ("unknown" if args.destinations_unknown
                        else list(args.destinations))
        executable = _require_absolute(args.maven, "--maven")
        output = _require_absolute(args.output, "--output")
        repo = _find_context(current)["payload"]["repo"]
        candidate = _candidate_record(current, args.candidate)
        if candidate.get("realm") == "build-plugin":
            raise CliError(
                f"the selected candidate {args.candidate!r} is realm build-plugin: a "
                f"project dependency-tree/effective-pom/classpath diagnostic cannot "
                f"explain plugin-realm inclusion, so the proposal is refused instead "
                f"of running an unscoped goal that would ignore the selection")
        module_id = candidate.get("module") or "."
        module_root = repo if module_id == "." else os.path.join(repo, *module_id.split("/"))
        module_pom = os.path.join(module_root, "pom.xml")
        if not os.path.isfile(module_pom):
            raise CliError(
                f"selected module {module_id!r} has no pom.xml at {module_pom!r}: the "
                f"module POM is a required pinned input of every Maven diagnostic, so "
                f"a missing POM is a clear refusal, never an ignored module")
        pinned = [module_pom]
        for path in args.inputs:
            absolute = _require_absolute(path, "--input")
            if os.path.abspath(absolute) not in {os.path.normpath(p) for p in pinned}:
                pinned.append(absolute)
        goal, output_flags = _MAVEN_PLUGIN_GOALS[args.kind]
        argv = [executable, "-B", "-N", "-f", module_pom,
                *(flag.format(output=output) for flag in output_flags), goal]
        try:
            operation = maven_proposal(store, argv, repo,
                                       network_destinations=destinations,
                                       input_paths=pinned,
                                       output_path=output,
                                       allow_overwrite=args.output_overwrite)
        except (MavenExecutionError, operations.OperationsError) as exc:
            raise CliError(f"maven proposal rejected: {_clean(exc)}") from exc
    persisted = operations.propose(store, operation, current["revision"])
    record = next(op for op in persisted["operations"] if op["id"] == operation["id"])
    return _emit({
        "ok": True,
        "command": "propose",
        "run_id": store.run_id,
        "run_dir": store.path,
        "revision": persisted["revision"],
        **({"module": {"id": module_id, "pom": module_pom}}
           if args.kind in _MAVEN_PLUGIN_GOALS else {}),
        "operation": {
            "id": record["id"],
            "run_id": record["run_id"],
            "kind": record["scope"]["kind"],
            "state": record["state"],
            "scope": record["scope"],
            "input_identities": record["input_identities"],
            "tool_identity": record["tool_identity"],
            "disclosure": record["disclosure"],
        },
        "approval": "diagnostic operations run without an approval file under "
                    "the standing policy; supply one only to record a real host "
                    "approval or denial, and "
                    "this CLI never creates an approval file",
    })


def _load_approval(path):
    """Bounded captured strict-JSON approval record; never written by us."""
    try:
        captured = capture_file(path, _APPROVAL_MAX_BYTES)
    except CaptureError as exc:
        raise CliError(f"cannot capture approval file: {_clean(exc)}") from exc
    if not captured.complete:
        raise CliError(f"approval file was not captured completely: "
                       f"{captured.reason or 'incomplete capture'}")
    try:
        approval = json.loads(captured.data.decode("utf-8"),
                              object_pairs_hook=_reject_duplicate_keys,
                              parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CliError(f"approval file is not valid strict JSON: {_clean(exc)}") from exc
    if not isinstance(approval, dict):
        raise CliError("approval file must hold a JSON object")
    return approval


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(token):
    raise ValueError(f"non-finite JSON constant {token!r} is not accepted")


def _output_report_path(argv):
    """The producer output path from this CLI's own -D output flag spellings;
    None when the argv carries none (only the CLI-proposed flags are read)."""
    for item in argv:
        for prefix in _OUTPUT_FLAG_PREFIXES:
            if item.startswith(prefix):
                return item[len(prefix):] or None
    return None




def _auto_import_tree_snapshot(store, current, record, result, document):
    """Record the executed dependency:tree output as a same-run imported-tree
    snapshot, so graph-history builds up without a manual import. The
    provenance is the strongest available: the proposal's own pinned goal, a
    record field the executor re-derived and enforced before consuming
    consent. The snapshot never upgrades the run to verified history: it is
    a machine-produced observation of this one execution. Any failure is
    recorded in the returned envelope and never fails the execution."""
    try:
        if result["outcome"] != "succeeded":
            return {"recorded": False, "reason": "the run outcome was not successful"}
        argv = record["scope"]["argv"]
        goal = argv[-1] if argv else ""
        if not goal.startswith("org.apache.maven.plugins:maven-dependency-plugin:"):
            return {"recorded": False, "reason": "pinned goal is not the dependency tree plugin"}
        parts = goal.split(":")
        if len(parts) != 4 or parts[3] != "tree":
            return {"recorded": False, "reason": "pinned goal is not the dependency tree plugin"}
        output_path = document.get("output_report_path")
        if not output_path:
            return {"recorded": False, "reason": "no output report path was pinned"}
        try:
            captured = capture_file(output_path, _IMPORT_MAX_BYTES)
        except CaptureError as exc:
            return {"recorded": False, "reason": f"cannot capture output report: {_clean(exc)}"}
        if not captured.complete:
            return {"recorded": False,
                    "reason": f"output report was not captured completely: {captured.reason or 'incomplete'}"}
        provenance = dict(_default_provenance("tree-json"))
        provenance.update({"producer": parts[1],
                           "version": parts[2]})
        imported = import_dependency_tree(captured.data, provenance=provenance,
                                          verbose=False)
        latest = result["report"]
        draft = copy.deepcopy(latest)
        evidence_id = _next_evidence_id(draft)
        generation_context = {
            "attestation": "executor-recorded",
            "note": "producer/version/format are attested by this run's own "
                    "audited execution of the pinned goal, verified against the "
                    "recorded argv before consent was consumed; the report bytes "
                    "themselves were produced by the unsandboxed child and were "
                    "not verified against what the pinned plugin would emit; "
                    "not caller-attested and not independently verified",
        }
        record_ev = {
            "id": evidence_id,
            "category": _IMPORT_CATEGORIES["tree-json"],
            "source_locator": captured.path,
            "observed_at": imported["imported_at"],
            "captured_identity": {"path": captured.path,
                                  "sha256": imported["input"]["sha256"]},
            "generation_context": generation_context,
            "producer": parts[1],
            "producer_version": parts[2],
            "payload": {"import": imported,
                        "provenance_attestation": "executor-recorded",
                        "assumed_grammar": None,
                        "auto_snapshot": True},
        }
        _append_evidence(draft, record_ev)
        _append_stage(draft, "import-tree-json", "complete",
                      {"currency": imported["context"]["currency"],
                       "provenance_attestation": "executor-recorded",
                       "assumed_grammar": None,
                       "tree_mode": "nonverbose",
                       "tree_mode_attestation": "executor-recorded",
                       "current_build_attribution": "none: no current context was supplied; "
                       "the evidence is authoritative only for the context its provenance "
                       "records"},
                      [], [evidence_id])
        persisted = store.commit(draft, latest["revision"])
        return {"recorded": True, "evidence_id": evidence_id,
                "revision": persisted["revision"],
                "tree_mode": "nonverbose",
                "provenance_attestation": "executor-recorded"}
    except BaseException as exc:
        return {"recorded": False,
                "reason": f"snapshot recording failed: {type(exc).__name__}: {_clean(exc)}"}




def _split_query(query):
    """groupId:artifactId or bare artifactId -> (group or None, artifact)."""
    if ":" in query:
        group, _, artifact = query.partition(":")
        return (group or None, artifact or None)
    return (None, query or None)


def _cmd_timeline(args):
    """Standalone Git-history timeline for one dependency declaration."""
    from mdi import timeline as timeline_module
    group, artifact = _split_query(args.query)
    if not artifact:
        raise CliError("--query must carry at least an artifactId")
    try:
        result = timeline_module.dependency_timeline(args.repo, group, artifact)
    except timeline_module.TimelineError as exc:
        raise CliError(f"timeline query rejected: {_clean(exc)}") from exc
    return _emit(result)


def _cmd_releases(args):
    """Standalone per-tag declaration coverage for one dependency."""
    from mdi import timeline as timeline_module
    group, artifact = _split_query(args.query)
    if not artifact:
        raise CliError("--query must carry at least an artifactId")
    try:
        result = timeline_module.release_coverage(args.repo, group, artifact)
    except timeline_module.TimelineError as exc:
        raise CliError(f"releases query rejected: {_clean(exc)}") from exc
    return _emit(result)


def _cmd_chain(args):
    """Standalone inclusion chain from a dependency-plugin tree JSON."""
    from mdi import capture as capture_module
    from mdi import timeline as timeline_module
    group, artifact = _split_query(args.query)
    if not artifact:
        raise CliError("--query must carry at least an artifactId")
    try:
        blob = capture_module.capture_file(args.tree, 32 * 1024 * 1024)
    except capture_module.CaptureError as exc:
        raise CliError(f"cannot read tree file: {_clean(exc)}") from exc
    if not blob.complete:
        raise CliError(f"tree file was not read completely: {blob.reason or 'incomplete'}")
    try:
        result = timeline_module.inclusion_chain(blob.data, group, artifact)
    except timeline_module.TimelineError as exc:
        raise CliError(f"chain query rejected: {_clean(exc)}") from exc
    return _emit(result)




def _read_file(path, what, max_bytes=32 * 1024 * 1024):
    from mdi.capture import capture_file, CaptureError
    try:
        captured = capture_file(path, max_bytes)
    except CaptureError as exc:
        raise CliError(f"cannot read {what}: {_clean(exc)}") from exc
    if not captured.complete:
        raise CliError(f"{what} was not read completely: {captured.reason or 'incomplete'}")
    return captured.data


def _split_gav(query):
    """groupId:artifactId[:version] -> (group, artifact, version or None)."""
    parts = query.split(":")
    if len(parts) == 3:
        return (parts[0] or None, parts[1] or None, parts[2] or None)
    if len(parts) == 2:
        return (parts[0] or None, parts[1] or None, None)
    return (None, query or None, None)


def _cmd_security(args):
    from mdi import queries as queries_module
    group, artifact, version = _split_gav(args.query)
    if not artifact or not version:
        raise CliError("--query must carry groupId:artifactId:version for security")
    try:
        result = queries_module.security(group, artifact, version)
    except Exception as exc:
        raise CliError(f"security query failed: {_clean(exc)}") from exc
    return _emit(result)


def _cmd_conflicts(args):
    from mdi import queries as queries_module
    data = _read_file(args.tree, "tree JSON")
    try:
        result = queries_module.conflicts(data)
    except queries_module.TimelineError as exc:
        raise CliError(f"conflicts query rejected: {_clean(exc)}") from exc
    return _emit(result)


def _cmd_versions(args):
    from mdi import queries as queries_module
    group = artifact = None
    if args.query:
        group, artifact, _ = _split_gav(args.query)
    try:
        result = queries_module.versions(args.repo, group_id=group, artifact_id=artifact)
    except queries_module.TimelineError as exc:
        raise CliError(f"versions query rejected: {_clean(exc)}") from exc
    return _emit(result)


def _cmd_diff(args):
    from mdi import queries as queries_module
    data_a = _read_file(args.tree_a, "tree A")
    data_b = _read_file(args.tree_b, "tree B")
    try:
        result = queries_module.diff_trees(data_a, data_b,
                                           label_a=args.label_a, label_b=args.label_b)
    except queries_module.TimelineError as exc:
        raise CliError(f"diff query rejected: {_clean(exc)}") from exc
    return _emit(result)


def _cmd_usage(args):
    from mdi import queries as queries_module
    try:
        result = queries_module.usage(args.jar, args.classes, scope=args.scope)
    except queries_module.TimelineError as exc:
        raise CliError(f"usage query rejected: {_clean(exc)}") from exc
    return _emit(result)


def _cmd_orphans(args):
    from mdi import queries as queries_module
    kwargs = {}
    if args.maven_repo:
        kwargs["maven_repo"] = args.maven_repo
    try:
        result = queries_module.orphans(args.repo, args.classes, **kwargs)
    except queries_module.TimelineError as exc:
        raise CliError(f"orphans query rejected: {_clean(exc)}") from exc
    return _emit(result)

def _cmd_execute(args):
    if (args.capture_operation is None) != (args.capture_approval_file is None):
        raise CliError("--capture-operation and --capture-approval-file must be supplied together")
    store = _open_run(args.run_dir)
    current = _checked_load(store, args.expect_revision)
    record = next((op for op in current["operations"]
                   if op["id"] == args.operation), None)
    if record is None:
        raise CliError(f"unknown operation {args.operation!r} in run {store.run_id}")
    kind = record["scope"].get("kind")
    if kind == "raw-capture":
        raise CliError("raw-capture cannot execute directly; execute its source with "
                       "--capture-operation and --capture-approval-file; the capture "
                       "approval is always a real host approval")
    if args.approval_file is not None:
        approval = _load_approval(args.approval_file)
    elif kind in ("maven", "advisory"):
        # Maintainer standing policy (2026-10-25): diagnostic operations never
        # pause for approval. The synthesized record is labeled as policy, is
        # never presented as a host approval, and the full operation audit
        # still records intent, argv or destination, and outcome.
        approval = {
            "run_id": store.run_id,
            "operation_id": args.operation,
            "scope": copy.deepcopy(record["scope"]),
            "approved": True,
            "approved_by": "standing-diagnostic-policy",
            "reference": "maintainer decision 2026-10-25: diagnostic maven goals "
                         "and advisory lookups are pre-approved",
        }
    else:
        raise CliError(f"operation {args.operation!r} carries unsupported kind "
                       f"{kind!r}; no executor was invoked")
    capture_args = {}
    if args.capture_operation is not None:
        capture_args = {"capture_operation_id": args.capture_operation,
                        "capture_approval": _load_approval(args.capture_approval_file)}
    try:
        if kind == "maven":
            result = execute_maven(store, args.operation, approval,
                                   expected_revision=current["revision"], **capture_args)
        elif kind == "advisory":
            result = execute_advisory(store, args.operation, approval,
                                      expected_revision=current["revision"], **capture_args)
        else:
            raise CliError(f"operation {args.operation!r} carries unsupported kind "
                           f"{kind!r}; no executor was invoked")
    except RunStoreError:
        raise
    except (MavenExecutionError, AdvisoryExecutionError,
            operations.OperationsError) as exc:
        error = CliError(f"execution refused: {_clean(exc)}")
        if hasattr(exc, 'raw_capture'):
            error.raw_capture = exc.raw_capture
        raise error from exc
    document = {
        "ok": True,
        "command": "execute",
        "run_id": store.run_id,
        "run_dir": store.path,
        "revision": result["revision"],
        "operation_id": args.operation,
        "outcome": result["outcome"],
        "evidence_id": result["evidence_id"],
    }
    if "capture" in result:
        document["capture"] = result["capture"]
        finalized_capture = next(op for op in result["report"]["operations"]
                                 if op["id"] == args.capture_operation)
        document["capture_outcome"] = finalized_capture["audit"]["outcome"]["result"]
        document["capture_evidence_id"] = result["capture_evidence_id"]
    if kind == "maven":
        # raw child output never reaches stdout or the report; only size/digest
        document["process"] = result["process"]
        document["output_meta"] = result["output_meta"]
        document["output_report_path"] = _output_report_path(record["scope"]["argv"])
        document["import"] = ("not performed: process success is not a parsed report; "
                              "run 'import' with an explicit --report file and "
                              "--provenance to make the captured output evidence")
        snapshot = _auto_import_tree_snapshot(
            store, current, record, result, document)
        if snapshot is not None:
            document["snapshot"] = snapshot
            if snapshot.get("recorded"):
                document["revision"] = snapshot["revision"]
                document["import"] = ("tree snapshot recorded automatically from the "
                                      "pinned tree goal; an explicit 'import' remains "
                                      "available for reports from other tools")
    else:
        document["advisory_result"] = result["result"]
        document["next_page_token"] = result["next_page_token"]
        document["next_page"] = ("a continuation page requires a new proposal with "
                                 "--page-token; it then runs under the standing policy")
    return _emit(document)


# ---------------------------------------------------------------------------
# entry


def _run(argv):
    args = _build_parser().parse_args(argv)
    if args.command == "discover":
        return _cmd_discover(args)
    if args.command == "select":
        return _cmd_select(args)
    if args.command == "collect":
        return _cmd_collect(args)
    if args.command == "import":
        return _cmd_import(args)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "propose":
        return _cmd_propose(args)
    if args.command == "timeline":
        return _cmd_timeline(args)
    if args.command == "releases":
        return _cmd_releases(args)
    if args.command == "chain":
        return _cmd_chain(args)
    if args.command == "security":
        return _cmd_security(args)
    if args.command == "conflicts":
        return _cmd_conflicts(args)
    if args.command == "versions":
        return _cmd_versions(args)
    if args.command == "diff":
        return _cmd_diff(args)
    if args.command == "usage":
        return _cmd_usage(args)
    if args.command == "orphans":
        return _cmd_orphans(args)
    return _cmd_execute(args)


def main(argv=None):
    try:
        return _run(list(sys.argv[1:] if argv is None else argv))
    except CliError as exc:
        return _emit_error(exc)
    except RunConflict as exc:
        return _emit_error(CliError(_clean(exc), EXIT_CONFLICT), source=exc)
    except CommitUncertain as exc:
        return _emit({
            "ok": False,
            "error": {"exit_code": EXIT_UNEXPECTED,
                      "message": _clean(exc),
                      "kind": "commit-uncertain",
                      "published_revision": exc.published_revision},
            **({'cleanup': exc.collection_cleanup} if hasattr(exc, 'collection_cleanup') else {}),
            **({'capture': exc.raw_capture} if hasattr(exc, 'raw_capture') else {}),
        }, EXIT_UNEXPECTED)
    except RunStoreError as exc:
        return _emit_error(CliError(f"run store rejected the operation: {_clean(exc)}"), source=exc)
    except (MavenExecutionError, AdvisoryExecutionError, operations.OperationsError) as exc:
        return _emit_error(CliError(f"operation refused: {_clean(exc)}"), source=exc)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # fixed message; exception text may hold secrets
        return _emit_error(CliError(
            f"unexpected internal failure: {type(exc).__name__}",
            EXIT_UNEXPECTED), source=exc)
