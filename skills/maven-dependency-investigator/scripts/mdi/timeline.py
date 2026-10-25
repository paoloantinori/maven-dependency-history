"""Offline Git-history queries over dependency declarations in POM files.

Two standalone answers, each a self-contained JSON document designed to be
exposable as an individual tool (MCP or otherwise):

- ``dependency_timeline``: every declaration event for one coordinate across
  the local Git history of a checkout (introduced, version-changed, removed,
  re-added), each citing commit hash, date, author, committer, message and
  POM path.
- ``release_coverage``: which tagged releases carry the declaration, at what
  version, per tag.

Both use the bounded gitinspect runner (SIGCHLD allowlist, pgid guard,
group kill) and the safe XML parser; no Maven is run, nothing is built,
nothing reaches the network. POM parsing is namespace-aware and profile
placement is preserved. Events describe the raw declaration text at a
commit, never a resolved dependency graph of that commit; a declaration
that entered a profile or dependencyManagement stays labeled with that
placement, and version placeholders are reported verbatim, never resolved.
"""

import os
import os
from dataclasses import dataclass
from mdi import gitinspect
from mdi.xmlsafe import UnsafeXML, parse_xml
from mdi.limits import Limits

PRODUCER = "mdi.timeline/1"

_GIT_TIMEOUT_SECONDS = 30
_GIT_MAX_OUTPUT = 4 * 1024 * 1024


class TimelineError(ValueError):
    """Invalid invocation or over-budget history; never a silent empty answer."""


@dataclass(frozen=True)
class TimelineLimits:
    max_events: int = 500
    max_commits: int = 2000
    max_tags: int = 200
    max_poms: int = 300
    max_xml_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        for field in ("max_events", "max_commits", "max_tags", "max_poms", "max_xml_bytes"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")


def _git(repo, *args):
    """One bounded git invocation in the checkout; output bytes on stdout."""
    argv = ["git", *args]
    env = gitinspect._child_env(os.environ.get("PATH"), os.environ.get("HOME"))
    outcome = gitinspect._run(argv, str(repo), env,
                              timeout_seconds=_GIT_TIMEOUT_SECONDS,
                              max_output_bytes=_GIT_MAX_OUTPUT)
    if outcome.outcome != "ok":
        raise TimelineError(
            f"git {' '.join(args[:2])} failed: {outcome.outcome}"
            + (f" ({outcome.issue})" if outcome.issue else ""))
    return outcome.stdout


def _parse_commit_line(line):
    """Parse the NUL-free record format '%H%x1f%aI%x1f%an%x1f%cI%x1f%cn%x1f%s'."""
    parts = line.split("\x1f")
    if len(parts) != 6:
        return None
    return {
        "commit": parts[0], "authored_at": parts[1], "author": parts[2],
        "committed_at": parts[3], "committer": parts[4], "subject": parts[5],
    }


def _pickaxe_commits(repo, artifact_id):
    """Commits that touched a line containing the artifactId declaration,
    newest first, bounded."""
    needle = f"<artifactId>{artifact_id}</artifactId>"
    raw = _git(repo, "log", "--all", "--first-parent", "--format=%H%x1f%aI%x1f%an%x1f%cI%x1f%cn%x1f%s",
               "-S", needle, "--", "*pom.xml", "pom.xml")
    commits = []
    for line in raw.decode("utf-8", errors="surrogateescape").splitlines():
        record = _parse_commit_line(line)
        if record is not None:
            commits.append(record)
    return commits


def _pom_at(repo, commit, pom_path):
    """Raw bytes of one POM at one commit; None when the path did not exist
    or git failed (the runner reports outcome ok with empty stdout on a
    nonzero exit for git show, so the exit code is checked too)."""
    argv = ["git", "show", f"{commit}:{pom_path}"]
    env = gitinspect._child_env(os.environ.get("PATH"), os.environ.get("HOME"))
    outcome = gitinspect._run(argv, str(repo), env,
                              timeout_seconds=_GIT_TIMEOUT_SECONDS,
                              max_output_bytes=_GIT_MAX_OUTPUT)
    if outcome.outcome != "ok" or outcome.rc != 0 or not outcome.stdout:
        return None
    return outcome.stdout


def _declaration_entries(pom_bytes, group_id, artifact_id, *, limits):
    """Namespace-aware declarations of one coordinate in one POM's bytes.

    Returns a list of placement records: management, build-plugin or project
    placement, profile id when inside a profile, and the version text
    verbatim (None when the version element is absent).
    """
    try:
        elem = parse_xml(pom_bytes, limits=Limits(xml_bytes=limits))
    except UnsafeXML:
        return None  # unparseable at this commit: the event stays unknown
    ns = elem.tag[1:].partition("}")[0] if elem.tag.startswith("{") else ""
    entries = []

    def _local(tag):
        return tag.partition("}")[2] if isinstance(tag, str) else ""

    def _ns(tag):
        return tag[1:].partition("}")[0] if isinstance(tag, str) and tag.startswith("{") else ""

    def walk(node, path, *, profile=None, management=False, plugin=None):
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if _ns(child.tag) != ns:
                continue
            name = _local(child.tag)
            child_path = f"{path}/{name}"
            if name == "dependencies":
                for dep in child:
                    if _ns(dep.tag) != ns or _local(dep.tag) != "dependency":
                        continue
                    g = a = v = None
                    for field in dep:
                        if not isinstance(field.tag, str):
                            continue
                        local = _local(field.tag)
                        text = (field.text or "").strip()
                        if local == "groupId":
                            g = text
                        elif local == "artifactId":
                            a = text
                        elif local == "version":
                            v = text
                    if a == artifact_id and (group_id is None or g == group_id):
                        entries.append({
                            "management": management, "profile": profile,
                            "plugin": plugin, "version": v,
                            "locator": child_path,
                        })
            elif name == "dependencyManagement":
                for deps in child:
                    if isinstance(deps.tag, str) and _local(deps.tag) == "dependencies":
                        for dep in deps:
                            if not isinstance(dep.tag, str) or _local(dep.tag) != "dependency":
                                continue
                            g = a = v = None
                            for field in dep:
                                if not isinstance(field.tag, str):
                                    continue
                                local = _local(field.tag)
                                text = (field.text or "").strip()
                                if local == "groupId":
                                    g = text
                                elif local == "artifactId":
                                    a = text
                                elif local == "version":
                                    v = text
                            if a == artifact_id and (group_id is None or g == group_id):
                                entries.append({
                                    "management": True, "profile": profile,
                                    "plugin": None, "version": v,
                                    "locator": f"{child_path}/dependencies/dependency",
                                })
            elif name == "profiles":
                for prof in child:
                    if not isinstance(prof.tag, str) or _local(prof.tag) != "profile":
                        continue
                    prof_id = None
                    for field in prof:
                        if isinstance(field.tag, str) and _local(field.tag) == "id":
                            prof_id = (field.text or "").strip()
                    walk(prof, child_path, profile=prof_id, management=management, plugin=plugin)
            elif name == "build":
                walk(child, child_path, profile=profile, management=management, plugin=plugin)

    walk(elem, "")
    return entries


def _changed_poms(repo, commit):
    """POM paths touched by one commit; only files named pom.xml count."""
    raw = _git(repo, "show", "--name-only", "--format=", commit)
    return [p for p in raw.decode("utf-8", errors="surrogateescape").splitlines()
            if p.strip() and p.strip().endswith("/pom.xml") or p.strip() == "pom.xml"]


def _classify(before, after):
    """Classify the transition of one placement record between two snapshots."""
    if before is None and after is not None:
        return "introduced"
    if before is not None and after is None:
        return "removed"
    if before is not None and after is not None and before != after:
        return "version-changed"
    return None


def dependency_timeline(repo, group_id, artifact_id, *, limits=None):
    """Every declaration event for one coordinate across Git history.

    Events are derived from the pickaxe set (commits whose diff touched a
    line containing the artifactId tag) and classified by comparing the
    parsed POM at the commit and at its parent. Each event carries the
    commit metadata, the POM path, the placement and both version texts.
    A commit that only moved lines (same state before and after) produces
    no event; this is diff-based history, never a claim about resolved
    graphs. The result is ordered oldest-first by commit date.
    """
    limits = limits or TimelineLimits()
    commits = _pickaxe_commits(repo, artifact_id)
    if len(commits) > limits.max_commits:
        raise TimelineError(
            f"pickaxe found {len(commits)} commits, over the bound of {limits.max_commits};"
            " narrow the history (shallow clone bounds or a smaller checkout)")
    events = []
    truncated = False
    for record in reversed(commits):  # oldest first
        poms = _changed_poms(repo, record["commit"])
        if len(poms) > limits.max_poms:
            poms = poms[:limits.max_poms]
            truncated = True
        for pom_path in poms:
            current = _pom_at(repo, record["commit"], pom_path)
            parent = _pom_at(repo, record["commit"] + "~1", pom_path)
            if current is None and parent is None:
                continue
            now_entries = _declaration_entries(current, group_id, artifact_id,
                                               limits=limits.max_xml_bytes) if current else []
            then_entries = _declaration_entries(parent, group_id, artifact_id,
                                                limits=limits.max_xml_bytes) if parent else []
            if now_entries is None or then_entries is None:
                events.append({**record, "pom": pom_path, "kind": "unparseable-pom",
                               "detail": "the POM bytes at this commit could not be parsed safely"})
                continue
            now_set = {(e["management"], e["profile"], e["plugin"]): e["version"] for e in now_entries}
            then_set = {(e["management"], e["profile"], e["plugin"]): e["version"] for e in then_entries}
            for key in sorted(set(now_set) | set(then_set), key=str):
                kind = _classify(then_set.get(key), now_set.get(key))
                if kind is None:
                    continue
                events.append({**record, "pom": pom_path, "kind": kind,
                               "placement": {"management": key[0], "profile": key[1], "plugin": key[2]},
                               "version_before": then_set.get(key),
                               "version_after": now_set.get(key)})
                if len(events) >= limits.max_events:
                    return {"schema": PRODUCER, "query": {"groupId": group_id, "artifactId": artifact_id},
                            "events": events, "truncated": True,
                            "limit": "max_events"}
    return {"schema": PRODUCER, "query": {"groupId": group_id, "artifactId": artifact_id},
            "events": events, "truncated": truncated, "limit": None}


def _clean_coordinate(segment):
    """Strip the leading child-index prefix from a tree path segment."""
    if isinstance(segment, str) and ":" in segment:
        parts = segment.split(":", 1)
        if parts[0].isdigit():
            return parts[1]
    return segment


def inclusion_chain(tree_json_bytes, group_id, artifact_id):
    """Every inclusion path from the tree root to the target coordinate.

    Reads the raw dependency-plugin JSON tree (the same format the tree
    diagnostic produces), walks it to the target nodes, and returns each
    path as an ordered list of clean coordinates. Multiple paths through
    different parents are all reported, never collapsed. The tree is an
    observation of one resolution at one point in time; it is never a
    claim about other profiles, other environments or other revisions.
    """
    from mdi.maven import import_dependency_tree, MavenImportError, PROVENANCE_FIELDS
    provenance = {field: None for field in PROVENANCE_FIELDS}
    provenance["producer"] = "maven-dependency-plugin"
    provenance["version"] = "3.11.0"
    try:
        imported = import_dependency_tree(tree_json_bytes, provenance=provenance,
                                          verbose=False)
    except MavenImportError as exc:
        raise TimelineError(f"tree JSON rejected ({exc.code}): {exc}") from exc
    nodes = imported["evidence"]["nodes"]
    root = next((n for n in nodes if n.get("depth") == 0), None)
    if root is None:
        raise TimelineError("the tree has no root node")
    by_id = {n["id"]: n for n in nodes}
    paths = []
    for node in nodes:
        coords = node.get("coordinates", {})
        if coords.get("artifactId") != artifact_id:
            continue
        if group_id is not None and coords.get("groupId") != group_id:
            continue
        chain = []
        for segment in node.get("path", []):
            chain.append(_clean_coordinate(segment))
        if not chain:
            chain = [f"{coords.get('groupId', '')}:{coords.get('artifactId', '')}:{coords.get('version', '')}"]
        paths.append({
            "path": chain,
            "version": coords.get("version"),
            "scope": node.get("scope"),
            "optional": node.get("optional"),
            "inclusion": node.get("inclusion"),
            "depth": node.get("depth"),
        })
    return {
        "schema": PRODUCER,
        "query": {"groupId": group_id, "artifactId": artifact_id},
        "root": f"{root['coordinates'].get('groupId', '')}:"
                f"{root['coordinates'].get('artifactId', '')}:"
                f"{root['coordinates'].get('version', '')}",
        "paths": paths,
        "scope": "one resolved tree at one point in time; other profiles,"
                 " environments and revisions may resolve differently",
    }


def release_coverage(repo, group_id, artifact_id, *, limits=None):
    """Per-tag declaration status for one coordinate.

    For every local tag, every POM that currently declares the coordinate is
    read at that tag; the answer reports the declared version (verbatim, never
    resolved) or absence. A tag whose tree lacks a POM reports the POM as
    absent at that tag. This is declaration coverage only: a release that
    declares a dependency is reported as declaring it, never as resolving or
    shipping it; the resolved tree of a historical release is unknown here.
    """
    limits = limits or TimelineLimits()
    raw = _git(repo, "tag", "--list", "--sort=version:refname")
    tags = [t for t in raw.decode("utf-8", errors="surrogateescape").splitlines() if t.strip()]
    if len(tags) > limits.max_tags:
        raise TimelineError(
            f"{len(tags)} tags found, over the bound of {limits.max_tags}")
    # POMs that declare the coordinate at HEAD, plus any POM path that
    # ever appeared in the pickaxe set (historical declarations).
    pickaxe = _pickaxe_commits(repo, artifact_id)
    historical_poms = set()
    for record in pickaxe:
        historical_poms.update(_changed_poms(repo, record["commit"]))
    head_poms = _git(repo, "ls-files", "*pom.xml", "pom.xml").decode(
        "utf-8", errors="surrogateescape").splitlines()
    all_poms = sorted(set(head_poms) | historical_poms)
    if len(all_poms) > limits.max_poms:
        raise TimelineError(
            f"{len(all_poms)} POM files to check, over the bound of {limits.max_poms}")
    declaring = []
    for pom in all_poms:
        current = _pom_at(repo, "HEAD", pom)
        if current is None:
            continue
        entries = _declaration_entries(current, group_id, artifact_id,
                                       limits=limits.max_xml_bytes)
        if entries:
            declaring.append(pom)
    releases = []
    for tag in tags:
        entry = {"tag": tag, "declarations": [], "status": "absent"}
        for pom in declaring:
            blob = _pom_at(repo, tag, pom)
            if blob is None:
                continue
            entries = _declaration_entries(blob, group_id, artifact_id,
                                           limits=limits.max_xml_bytes)
            if entries is None:
                entry["declarations"].append({"pom": pom, "status": "unparseable"})
                continue
            for e in entries:
                entry["declarations"].append({
                    "pom": pom, "version": e["version"],
                    "management": e["management"], "profile": e["profile"],
                    "plugin": e["plugin"]})
        if entry["declarations"]:
            entry["status"] = "declared"
        releases.append(entry)
    return {"schema": PRODUCER, "query": {"groupId": group_id, "artifactId": artifact_id},
            "releases": releases,
            "scope": "declaration coverage per tag; not a resolved dependency tree of any release"}
