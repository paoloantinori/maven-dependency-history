"""Candidate discovery across declarations and existing packaged copies.

discover_candidates(repo, query) orchestrates three reviewed, read-only
probes and aggregates their outputs into JSON-safe candidate subjects:

- mdi.gitinspect.checkout_context for the revision/dirty snapshot. Git being
  missing or the tree not being a repository is partial context, never an
  abort: declaration and archive discovery still run.
- mdi.discover.discover_declarations for raw POM declarations, unchanged.
- With archive_scratch_base, the isolated file-backed pipeline uses one
  shared 120-second archive-stage deadline and large FileArchiveLimits. The
  caller supplies a dedicated private base outside the checkout. Parent IO
  remains cooperatively bounded; child decompression runs in a killable worker.
  Without that explicit storage location, the library retains its original
  bounded-memory ArchiveLimits contract. The CLI always supplies the location.
- Both backends read archives through the
  descriptor path: the checkout root is opened by
  mdi.discover.open_checkout_fd (lexical walk, O_DIRECTORY|O_NOFOLLOW, no
  symlinked component followed), each module's ``target`` directory is
  opened relative to that descriptor with mdi.discover.open_module_dir,
  directories are enumerated with os.scandir over a duplicate of the held
  descriptor, files are captured with mdi.discover.capture_at relative to
  the held directory descriptor (final component O_NOFOLLOW|O_NONBLOCK,
  fstat identity before and after on the same descriptor), and the logical
  path recorded in each capture is provenance only, never reopened. A
  swapped ancestor directory therefore cannot redirect what is read; what
  the race still permits is reading the old inode of a replaced directory,
  which the recorded path cannot prove away.

Scope of the archive scan: the selected modules' ``target`` subtrees plus at
most one explicit ``artifact`` captured the same descriptor way. Bounds are
global across the whole call: max_archives captured archives (the explicit
artifact included), max_traversal_dirs directories, max_traversal_depth
levels, max_dir_entries entries read per directory before sorting. Hitting a
bound yields truncated coverage plus an issue, not a silent empty result;
one unreadable child never ends a directory walk; symlink entries are
skipped with an issue, never followed. An unreadable or missing POM never
suppresses a module's target scan.

What this module deliberately does NOT do: no Maven resolution, no
downloads, no interpolation, no activation, no correlation with resolved
graphs (mdi.correlate is never invoked), no auto-selection of a candidate
and no authorization persistence. Aggregation is a pure candidate relation:
a declaration and an archive copy describing the same coordinates remain
two distinct subjects (kind differs), because no evidence here proves they
are the same artifact.

Subject identity. A candidate id is sha256 over a canonical JSON payload of
(kind, realm, module, known coordinates, owning plugin, package copy).
Realm stays inside the report enum (project, build-plugin, unknown); a
packaged copy is realm 'unknown' with kind 'packaged-copy'. Declarations
aggregate only into the same subject when kind, realm, module, owning
plugin and every known coordinate component match exactly; unresolved
${...} coordinate values never constitute known identity, so they never
satisfy a qualified query. Inclusion evidence is aggregated across all
occurrences of one subject: any plain dependency/plugin occurrence makes it
True, a subject seen only under management/BOM keeps False, and
per-occurrence flags stay in the declaration list. Package copies never
merge across paths or nested chains, and conflicting pom.properties
candidates are all preserved. Unqualified (bare) queries also match
unknown-coordinate archives by filename substring; a qualified query
against an archive without usable metadata cannot claim a match, and only a
basename corroboration is recorded as an ``ambiguous-possible`` match. No
match field claims component identity nowhere. ``text_matches`` is True
only when the query text matched observed coordinate text (the declaration
text, or pom.properties whose own coordinates stay 'metadata-candidate');
filename-based matches carry text_matches False because a filename is not
a coordinate observation.
"""

import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from errno import ELOOP

from mdi.archives import ARCHIVE_SUFFIXES, ArchiveLimits, inspect_captured_archive
from mdi.capture import CaptureError
from mdi.archive_files import FileArchiveLimits
from mdi.archive_pipeline import inspect_archive as inspect_archive_pipeline
from mdi.discover import (capture_at, discover_declarations, open_checkout_fd,
                          open_module_dir)
from mdi.gitinspect import checkout_context

_COORD_KEYS = ("groupId", "artifactId", "version", "type", "classifier")
_DEFAULT_XML_BYTES = 8 * 1024 * 1024
_READ_SAFETY = ("descriptor walk from the checkout root, O_DIRECTORY|O_NOFOLLOW on every "
                "component, scandir over a duplicate of the held fd, capture_at relative "
                "to the held fd with same-descriptor fstat race check; logical paths are "
                "provenance only")
_NON_INCLUSION_KINDS = frozenset({"managed-dependency", "bom-import"})


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _parse_query(query):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty string")
    raw = query.strip()
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) > 3 or any(not p.strip() for p in parts):
            raise ValueError(f"qualified query must be g:a[:v] with nonempty parts: {raw!r}")
        return {"raw": raw, "kind": "qualified", "group_id": parts[0].strip(),
                "artifact_id": parts[1].strip(),
                "version": parts[2].strip() if len(parts) == 3 else None}
    return {"raw": raw, "kind": "bare", "group_id": None,
            "artifact_id": raw, "version": None}


def _known(coords):
    """Known coordinate components only; unresolved placeholders are unknown."""
    known = {}
    for key in _COORD_KEYS:
        value = (coords.get(key) or "").strip()
        if value and "${" not in value:
            known[key] = value
    return known


def _coords_match(query, coords):
    artifact = (coords.get("artifactId") or "").strip()
    if not artifact or "${" in artifact:
        return False
    if query["kind"] == "bare":
        return query["artifact_id"] in artifact
    group = (coords.get("groupId") or "").strip()
    if group != query["group_id"] or "${" in group:
        return False
    if artifact != query["artifact_id"]:
        return False
    version = (coords.get("version") or "").strip()
    return query["version"] is None or (version == query["version"] and "${" not in version)


def _subject_id(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "mdi-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class _Candidates:
    def __init__(self):
        self.by_id = {}

    def add_declaration(self, realm, module, coords, owning_plugin, match, declaration):
        payload = {"kind": "declaration", "realm": realm, "module": module,
                   "coordinates": coords, "owning_plugin": owning_plugin}
        cid = _subject_id(payload)
        cand = self.by_id.setdefault(cid, {
            "id": cid, "kind": "declaration", "realm": realm, "module": module,
            "coordinates": coords or None, "owning_plugin": owning_plugin,
            "coordinate_status": "declared", "match": match, "inclusion_evidence": None,
            "declarations": [], "metadata": [], "copies": [], "notes": []})
        cand["declarations"].append(declaration)
        return cand

    def add_copy(self, module, coords, match, metadata, copy):
        payload = {"kind": "packaged-copy", "realm": "unknown", "module": module,
                   "coordinates": coords, "owning_plugin": None,
                   "copy": {"display_path": copy["display_path"],
                            "sha256": copy["sha256"],
                            "entry_chain": copy["entry_chain"]}}
        cid = _subject_id(payload)
        cand = self.by_id.setdefault(cid, {
            "id": cid, "kind": "packaged-copy", "realm": "unknown", "module": module,
            "coordinates": coords or None, "owning_plugin": None,
            "coordinate_status": "metadata-candidate" if coords else "unknown",
            "match": match, "inclusion_evidence": "unknown",
            "declarations": [], "metadata": [], "copies": [], "notes": []})
        if metadata is not None:
            cand["metadata"].append(metadata)
        cand["copies"].append(copy)
        return cand

    def ordered(self):
        ordered = [self.by_id[key] for key in sorted(self.by_id)]
        for cand in ordered:
            if cand["kind"] != "packaged-copy":
                plain = [d for d in cand["declarations"]
                         if not d["management_only"] and d["kind"] not in _NON_INCLUSION_KINDS]
                cand["inclusion_evidence"] = bool(plain)
        return ordered


class _EntryInfo:
    """Entry name and type materialized while the scan descriptor is live.

    DirEntry stat-backed answers can lazily hit the scandir descriptor
    (DT_UNKNOWN filesystems), so both are resolved inside the scan and the
    walk afterwards touches only these frozen values, never DirEntry.
    """

    __slots__ = ("name", "symlink", "directory")

    def __init__(self, name, symlink, directory):
        self.name = name
        self.symlink = symlink
        self.directory = directory


def _read_dir_bounded(dir_fd, max_dir_entries):
    """Bounded, deterministic entry read; dir_fd stays open for relative opens.

    os.scandir does not take ownership of a supplied descriptor (verified
    against the running interpreter: the fd survives the closed iterator),
    so the duplicate is closed here on every path, including a scandir
    construction failure. Entry name/type metadata is materialized inside
    that live-fd window and returned frozen.
    """
    records = []
    capped = False
    scan_fd = os.dup(dir_fd)
    try:
        with os.scandir(scan_fd) as iterator:
            for entry in iterator:
                if len(records) >= max_dir_entries:
                    capped = True
                    break
                records.append(_EntryInfo(entry.name, entry.is_symlink(),
                                          entry.is_dir(follow_symlinks=False)))
    finally:
        os.close(scan_fd)
    records.sort(key=lambda r: r.name)
    return records, capped


def _walk_target_tree(dir_fd, subject, display, module_id, limits, issues,
                      truncated, budget, inspections, skip_subjects, archive_stage=None):
    """Depth-first bounded walk of one already-open target directory.

    `dir_fd` stays valid for relative opens for the whole walk (scandir runs
    on duplicates). Each archive file captured becomes one inspection entry
    keyed by its logical subject path; failures are isolated per child.

    Descriptor discipline: a child directory is opened only after its slot
    is reserved against the global directory budget (dirs already visited
    plus dirs still queued), and every child descriptor is closed as soon as
    that directory's turn ends, so the number of live descriptors is bounded
    by the queue, which the budget itself bounds. The caller's own dir_fd is
    never closed here.
    """
    stack = [(dir_fd, subject, display, 0, False)]
    max_dirs = budget["max_dirs"]
    try:
        while stack:
            if archive_stage is not None and archive_stage.expired():
                truncated.append("archive-stage-deadline")
                return
            if budget["archives"] <= 0:
                truncated.append("archive-budget")
                return
            if budget["dirs"] <= 0:
                truncated.append("traversal-dirs")
                issues.append({"code": "traversal-limit",
                               "detail": f"{subject}: directory budget exhausted"})
                return
            current_fd, cur_subject, cur_display, depth, owned = stack.pop()
            budget["dirs"] -= 1
            try:
                try:
                    entries, capped = _read_dir_bounded(current_fd, budget["max_dir_entries"])
                except OSError as exc:
                    issues.append({"code": "target-scan-failed", "detail": f"{cur_subject}: {exc}"})
                    continue
                if capped:
                    issues.append({"code": "dir-entries-limit",
                                   "detail": f"{cur_subject}: entry read capped at "
                                             f"{budget['max_dir_entries']}"})
                    truncated.append("dir-entries")
                for entry in entries:
                    name = entry.name
                    child_subject = os.path.join(cur_subject, name)
                    child_display = os.path.join(cur_display, name)
                    try:
                        if entry.symlink:
                            issues.append({"code": "symlink-skipped",
                                           "detail": f"{child_subject}: symlink entries are never followed"})
                            continue
                        if entry.directory:
                            if depth + 1 > budget["max_depth"]:
                                truncated.append("traversal-depth")
                                issues.append({"code": "traversal-depth-limit",
                                               "detail": f"{child_subject}: beyond max_traversal_depth"})
                                continue
                            committed = max_dirs - budget["dirs"] + len(stack)
                            if committed + 1 > max_dirs:
                                truncated.append("traversal-dirs")
                                issues.append({"code": "traversal-limit",
                                               "detail": f"{child_subject}: directory budget "
                                                         f"reserved for already-queued directories"})
                                continue
                            child_fd = open_module_dir(current_fd, (name,), child_subject)
                            stack.append((child_fd, child_subject, child_display,
                                          depth + 1, True))
                        elif name.lower().endswith(ARCHIVE_SUFFIXES):
                            if child_subject in skip_subjects:
                                # already successfully inspected (e.g. the explicit
                                # artifact); skipping precedes any budget or capture
                                continue
                            if budget["archives"] <= 0:
                                truncated.append("archive-budget")
                                continue
                            if archive_stage is not None and archive_stage.expired():
                                truncated.append("archive-stage-deadline")
                                return
                            budget["archives"] -= 1
                            _capture_archive(current_fd, name, child_subject, child_display,
                                             module_id, "archive", limits, issues, inspections,
                                             archive_stage=archive_stage)
                    except (OSError, ValueError) as exc:
                        issues.append({"code": "target-entry-failed",
                                       "detail": f"{child_subject}: {exc}"})
            finally:
                # owned child descriptors close when their turn ends, never twice;
                # the caller's dir_fd (owned=False) stays open.
                if owned:
                    os.close(current_fd)
    finally:
        # every early exit (budget returns) and every injected interrupt must
        # drain the still-queued owned descriptors; the caller's dir_fd
        # (owned=False) is never closed here, and a popped fd was already
        # closed by its own turn's finally above, so no double close.
        for queued_fd, _subject, _display, _depth, queued_owned in stack:
            if queued_owned:
                os.close(queued_fd)
        stack.clear()


def _capture_or_issue(dir_fd, name, subject, code_prefix, limits, issues):
    """Capture a descriptor-relative leaf or record its isolated failure."""
    try:
        return capture_at(dir_fd, name, subject, limits.max_input_bytes)
    except CaptureError as exc:
        issues.append({"code": f"{code_prefix}-nonregular",
                       "detail": f"{subject}: rejected, {exc}"})
        return
    except OSError as exc:
        code = f"{code_prefix}-symlink" if exc.errno == ELOOP else f"{code_prefix}-unreadable"
        issues.append({"code": code, "detail": f"{subject}: {exc.strerror or exc}"})
        return


class _ArchiveStage:
    """One shared stage clock and caller-provided dedicated private spool base."""

    def __init__(self, checkout, scratch_base, limits, seconds):
        self.checkout = checkout
        self.scratch_base = os.path.abspath(os.fspath(scratch_base))
        self.limits = limits
        self.deadline = time.monotonic() + seconds
        self.attempts = []
        for root, scratch in ((checkout, self.scratch_base),
                              (os.path.realpath(checkout), os.path.realpath(self.scratch_base))):
            root, scratch = os.path.normpath(root), os.path.normpath(scratch)
            if os.path.commonpath((root, scratch)) in (root, scratch):
                raise ValueError("archive scratch must be outside the checkout")
        fd = open_checkout_fd(self.scratch_base)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("archive scratch must be a private caller-owned directory")
        finally:
            os.close(fd)

    def expired(self):
        return time.monotonic() >= self.deadline

    def inspect(self, dir_fd, name, subject, issues):
        if self.expired():
            issues.append({"code": "archive-stage-deadline", "detail": subject})
            return None
        if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_NONBLOCK")):
            raise ValueError("safe descriptor archive opening is unavailable")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("archive source must be a regular file")
            response = inspect_archive_pipeline(
                fd, source_path=subject, checkout=self.checkout,
                scratch_base=self.scratch_base, deadline=self.deadline, limits=self.limits)
        finally:
            primary = sys.exception()
            try:
                os.close(fd)
            except BaseException:
                if primary is None:
                    raise
                primary.add_note("archive source descriptor close unconfirmed")
        self.attempts.append({"source_path": subject,
                              **{key: value for key, value in response.items() if key != "result"}})
        for issue in response["issues"]:
            issues.append({"code": issue["code"], "detail": subject})
        result = response["result"]
        if result is not None:
            for issue in result.get("issues", []):
                issues.append({"code": issue["code"], "detail": subject})
        if response["outcome"] != "complete" and not response["issues"]:
            issues.append({"code": "archive-partial", "detail": subject})
        return result


def _capture_archive(dir_fd, name, subject, display, module_id, code_prefix,
                     limits, issues, inspections, archive_stage=None):
    """Read the held descriptor through the selected bounded backend."""
    if archive_stage is not None:
        result = archive_stage.inspect(dir_fd, name, subject, issues)
        if result is None:
            return
    else:
        captured = _capture_or_issue(dir_fd, name, subject, code_prefix, limits, issues)
        if captured is None:
            return
        result = inspect_captured_archive(captured, limits=limits)
    inspections.append({"subject": subject, "display": display,
                        "module": module_id, "result": result})


def _capture_explicit(root_fd, root_abs, artifact_abs, limits, issues, archive_stage=None):
    """Capture the explicit artifact through the descriptor path, not a path open."""
    try:
        rel = os.path.relpath(artifact_abs, root_abs)
    except ValueError as exc:
        raise ValueError(f"explicit artifact must be inside the checkout: {artifact_abs!r}") from exc
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise ValueError(f"explicit artifact must be inside the checkout: {artifact_abs!r}")
    parts = rel.split(os.sep)
    leaf, parents = parts[-1], parts[:-1]
    subject = artifact_abs
    try:
        dir_fd = open_module_dir(root_fd, parents, subject)
    except OSError as exc:
        issues.append({"code": "artifact-open-failed",
                       "detail": f"{os.path.dirname(subject)}: {exc.strerror or exc}"})
        return None
    try:
        if archive_stage is not None:
            try:
                return archive_stage.inspect(dir_fd, leaf, subject, issues)
            except (OSError, ValueError) as exc:
                issues.append({"code": "artifact-unreadable", "detail": f"{subject}: {exc}"})
                return None
        return _capture_or_issue(dir_fd, leaf, subject, "artifact", limits, issues)
    finally:
        os.close(dir_fd)


def _module_root(root_abs, module_id):
    if module_id in (None, "."):
        return os.path.normpath(root_abs)
    return os.path.normpath(os.path.join(root_abs, *module_id.split("/")))


def _within_containment(path, directory):
    """Exact component containment: True only when path sits inside directory.

    os.path.commonpath compares component-wise on normalized spellings, so
    the root module ('.') compares against root_abs itself and a sibling
    whose name merely shares a prefix (moduleX vs module) is rejected.
    """
    try:
        return os.path.commonpath([os.path.normpath(path), directory]) == directory
    except ValueError:
        return False


def _owning_module(path, root_abs, module_ids):
    """Deepest discovered module whose directory contains path."""
    best, best_len = ".", -1
    for module_id in module_ids:
        base = root_abs if module_id == "." else os.path.join(root_abs, *module_id.split("/"))
        if path.startswith(base + os.sep) and len(base) > best_len:
            best, best_len = module_id, len(base)
    return best


def _archive_candidates(candidates, inspections, root_abs, module_ids, query,
                        explicit, issues):
    for item in inspections:
        result = item["result"]
        subject, display = item["subject"], item["display"]
        module = item["module"] or _owning_module(subject, root_abs, module_ids)
        if explicit is not None and subject == explicit:
            display = os.path.basename(subject)
        archives_by_chain = {tuple(arch["entry_chain"]): arch for arch in result["archives"]}
        entries_by_index = {(entry["archive_id"], entry["index"]): entry
                            for entry in result["entries"]}
        outer = archives_by_chain.get(())
        outer_status = outer["inspection_status"] if outer else "rejected"
        for arch in result["archives"]:
            # Display delimiters are cosmetic: real filenames may contain '!'.
            # Resolve ancestry using the reader's indexed archive-entry chain.
            suffix = []
            chain = tuple(arch["entry_chain"])
            for depth, index in enumerate(chain):
                parent = archives_by_chain.get(chain[:depth])
                entry = (entries_by_index.get((parent["archive_id"], index))
                         if parent is not None else None)
                if entry is None:
                    issues.append({"code": "archive-ancestry-unresolved", "detail": subject})
                    break
                suffix.append(entry["name"])
            if len(suffix) != len(chain):
                continue
            basename = suffix[-1] if suffix else os.path.basename(subject)
            copy = {"path": subject,
                    "display_path": display + ("!" + "!".join(suffix) if suffix else ""),
                    "sha256": arch["sha256"], "entry_chain": arch["entry_chain"],
                    "inspection_status": arch["inspection_status"]}
            metas = arch["identity"]["candidates"]
            if metas:
                for meta in metas:
                    if not _coords_match(query, meta):
                        continue
                    candidates.add_copy(module, _known(meta),
                                        {"kind": query["kind"], "basis": "pom.properties",
                                         "text_matches": True, "query": query["raw"]},
                                        metadata=dict(meta), copy=copy)
            elif query["kind"] == "bare" and query["artifact_id"] in basename:
                candidates.add_copy(module, {},
                                    {"kind": "bare", "basis": "filename-substring",
                                     "text_matches": False, "query": query["raw"]},
                                    metadata=None, copy=copy)
            elif query["kind"] == "qualified" and query["artifact_id"] in basename:
                candidates.add_copy(module, {},
                                    {"kind": "ambiguous-possible",
                                     "basis": "filename-corroboration",
                                     "text_matches": False, "query": query["raw"]},
                                    metadata=None, copy=copy)
        if outer_status in ("malformed", "unsupported", "rejected"):
            issues.append({"code": f"archive-{outer_status}",
                           "detail": f"{subject}: outer archive not inspected ({outer_status})"})


def discover_candidates(repo, query, *, module=None, artifact=None,
                        archive_limits=None, max_modules=128, max_depth=16,
                        xml_bytes=_DEFAULT_XML_BYTES, max_archives=32,
                        max_traversal_dirs=256, max_traversal_depth=8,
                        max_dir_entries=2000, archive_scratch_base=None,
                        archive_seconds=120):
    """Aggregate declaration and packaged-copy candidates for `query`.

    Returns {'query', 'context', 'candidates', 'distinct_subjects',
    'selection_required', 'source_results', 'coverage', 'issues'}; JSON-safe
    and read-only with respect to the checkout. archive_scratch_base selects
    file-backed isolated inspection and must already exist, be dedicated and
    private, and lie outside the checkout. Otherwise the legacy in-memory
    reader is retained without implicit storage writes. Raises ValueError for
    an invalid query, unknown module, or artifact outside the selected scope.
    """
    parsed = _parse_query(query)
    for name, value, minimum in (("max_archives", max_archives, 1),
                                 ("max_modules", max_modules, 1),
                                 ("max_depth", max_depth, 0),
                                 ("max_traversal_dirs", max_traversal_dirs, 1),
                                 ("max_traversal_depth", max_traversal_depth, 0),
                                 ("max_dir_entries", max_dir_entries, 1)):
        _positive_int(value, name, minimum)
    file_backed = archive_scratch_base is not None
    limits = archive_limits if archive_limits is not None else (
        FileArchiveLimits() if file_backed else ArchiveLimits())
    expected_limits = FileArchiveLimits if file_backed else ArchiveLimits
    if not isinstance(limits, expected_limits):
        raise TypeError(f"archive_limits must be {expected_limits.__name__}")
    if type(archive_seconds) not in (int, float) or not 0 < archive_seconds <= 120:
        raise ValueError("archive_seconds must be positive and at most 120")
    root_abs = os.path.abspath(os.fspath(repo))
    issues = []
    truncated = []
    context = checkout_context(root_abs)
    declarations = discover_declarations(root_abs, max_modules=max_modules,
                                         max_depth=max_depth, xml_bytes=xml_bytes)

    # Every discovered module participates in scope, whatever its POM status:
    # an unreadable or missing POM never suppresses its target scan.
    module_ids = [record["id"] for record in declarations["modules"]]
    norm = None
    if module is not None:
        norm = os.path.normpath(module)
        if norm.startswith("..") or os.path.isabs(norm):
            raise ValueError(f"module filter must be repo-relative: {module!r}")
        if norm not in module_ids:
            raise ValueError(f"module filter {module!r} is not a discovered module")
    selected_ids = [norm] if norm is not None else module_ids

    candidates = _Candidates()
    for decl in declarations["declarations"]:
        if decl["module"] not in selected_ids:
            continue
        coords = decl["coordinates"]
        if not _coords_match(parsed, coords):
            continue
        plugin = decl["owning_plugin"]
        owning = _known(plugin) if plugin else None
        candidates.add_declaration(
            decl["realm"], decl["module"], _known(coords), owning,
            {"kind": parsed["kind"], "basis": "declaration", "text_matches": True,
             "query": parsed["raw"]},
            {"kind": decl["kind"], "management_only": decl["management_only"],
             "profile": decl["profile"], "activation": decl["activation"],
             "owning_plugin": plugin, "source": dict(decl["source"])})

    inspections = []
    archive_stage = (_ArchiveStage(root_abs, archive_scratch_base, limits, archive_seconds)
                     if file_backed else None)
    budget = {"archives": max_archives - (1 if artifact is not None else 0),
              "dirs": max_traversal_dirs, "max_dirs": max_traversal_dirs,
              "max_depth": max_traversal_depth, "max_dir_entries": max_dir_entries}
    explicit_subject = None
    try:
        root_fd = open_checkout_fd(root_abs)
    except OSError as exc:
        issues.append({"code": "root-rejected",
                       "detail": f"{root_abs}: {exc.strerror or exc}"})
    else:
        try:
            if artifact is not None:
                explicit_subject = os.path.abspath(os.fspath(artifact))
                if module is not None and not _within_containment(explicit_subject, _module_root(root_abs, norm)):
                    raise ValueError(
                        f"explicit artifact outside module {module!r}: {artifact!r}")
                captured = _capture_explicit(root_fd, root_abs, explicit_subject, limits, issues,
                                             archive_stage=archive_stage)
                if captured is not None:
                    result = captured if file_backed else inspect_captured_archive(captured, limits=limits)
                    inspections.append({"subject": explicit_subject,
                                        "display": os.path.basename(explicit_subject),
                                        "module": None, "result": result})
            already_inspected = {item["subject"] for item in inspections}
            if archive_stage is not None:
                already_inspected.update(item["source_path"] for item in archive_stage.attempts)
            for module_id in selected_ids:
                parts = () if module_id == "." else tuple(module_id.split("/"))
                subject = os.path.join(root_abs, *parts, "target")
                try:
                    dir_fd = open_module_dir(root_fd, parts + ("target",), subject)
                except OSError as exc:
                    code = ("target-missing" if exc.errno == 2 else "target-rejected")
                    issues.append({"code": code,
                                   "detail": f"{subject}: {exc.strerror or exc}"})
                    continue
                try:
                    display = os.path.join(*(("target",) if not parts else parts + ("target",)))
                    _walk_target_tree(dir_fd, subject, display, module_id, limits,
                                      issues, truncated, budget, inspections,
                                      already_inspected, archive_stage=archive_stage)
                finally:
                    os.close(dir_fd)
        finally:
            os.close(root_fd)
    # Dedup by logical subject, keeping first occurrence: the explicit
    # artifact may also sit in a scanned target subtree, and one physical
    # copy must stay one inspection.
    seen_subjects = set()
    deduped = []
    for item in inspections:
        if item["subject"] not in seen_subjects:
            seen_subjects.add(item["subject"])
            deduped.append(item)
    inspections = deduped
    _archive_candidates(candidates, inspections, root_abs, module_ids, parsed,
                        explicit_subject, issues)
    if archive_stage is not None and archive_stage.expired():
        truncated.append("archive-stage-deadline")
        if not any(issue["code"] == "archive-stage-deadline" for issue in issues):
            issues.append({"code": "archive-stage-deadline", "detail": root_abs})

    ordered = candidates.ordered()
    distinct = len(ordered)
    coverage = {
        "declarations": declarations["coverage"],
        "modules": {"selected": selected_ids, "discovered": module_ids,
                    "profile_gated": [record["id"] for record in declarations["modules"]
                                      if record.get("declared_in_profile")]},
        "archives": {"captured": len(inspections),
                     "backend": "isolated-file" if file_backed else "bounded-memory",
                     "stage_seconds": archive_seconds if file_backed else None,
                     "reader_limits": asdict(limits),
                     "pipeline_observations": archive_stage.attempts if file_backed else [],
                     "deadline_scope": ("shared archive stage, parent IO cooperative; child decompression isolated"
                                        if file_backed else "cooperative per archive"),
                     "truncated": sorted(set(truncated)),
                     "read_safety": (_READ_SAFETY if not file_backed else
                         "descriptor walk with nofollow ancestors and readonly nofollow/nonblocking leaf; "
                         "bounded pread spool capture and isolated reader; logical source path is provenance only"),
                     "limits": {"max_archives": max_archives,
                                "max_traversal_dirs": max_traversal_dirs,
                                "max_traversal_depth": max_traversal_depth,
                                "max_dir_entries": max_dir_entries}},
        "git_context": context["status"],
        "resolution": "not-computed",
        "usage": "not-computed",
        "security": "not-computed",
        "history": "not-computed",
        "limits": {"max_modules": max_modules, "max_depth": max_depth,
                   "xml_bytes": xml_bytes},
        "observed_at": _now_iso(),
    }
    return {
        "query": parsed,
        "context": context,
        "candidates": ordered,
        "distinct_subjects": distinct,
        "selection_required": distinct > 1,
        "source_results": {
            "declarations": declarations,
            "archives": [item["result"] for item in inspections],
        },
        "coverage": coverage,
        "issues": issues,
    }
