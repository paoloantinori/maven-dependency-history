"""Standalone query commands: one query in, one JSON answer out.

Every function here is a pure, self-contained answer designed to be
exposable as an individual MCP tool. None requires a run-store, a
proposal, or an approval; the diagnostic standing policy covers the
operations that touch Maven or the network. Each return value carries
its own schema label and honest scope statement.
"""

import hashlib
import json
import os
from pathlib import Path

from mdi import gitinspect
from mdi.timeline import TimelineError, _git, _declaration_entries
from mdi.xmlsafe import UnsafeXML, parse_xml
from mdi.limits import Limits

PRODUCER = "mdi.queries/1"

_MAX_XML = 8 * 1024 * 1024


# -- security ---------------------------------------------------------------

def security(group_id, artifact_id, version, *, timeout_seconds=30):
    """Known OSV vulnerabilities for one exact coordinate.

    Makes one HTTPS request to api.osv.dev with the coordinate query only;
    no settings, credentials, source code or environment data leaves the
    machine. Under the maintainer's standing policy this runs without a
    pause. An empty vulns list means no known matches from OSV at this
    time, never that the coordinate is safe.
    """
    from mdi.advisories import build_query, map_response, AdvisoryLimits
    from mdi.advisory_transport import DeadlineTransport

    limits = AdvisoryLimits()
    query = build_query(group_id, artifact_id, version, limits=limits)
    body = json.dumps(query, sort_keys=True, separators=(",", ":")).encode("utf-8")
    transport = DeadlineTransport()
    raw = transport.post("https://api.osv.dev/v1/query", body,
                         timeout_seconds, limits.response_bytes)
    result = map_response(raw, group_id, artifact_id, version,
                          retrieved_at=None, limits=limits)
    return {
        "schema": PRODUCER,
        "query": {"groupId": group_id, "artifactId": artifact_id, "version": version},
        "status": result["status"],
        "vulnerabilities": result.get("vulnerabilities", []),
        "more_pages": result.get("more_pages", False),
        "scope": "one OSV query at one point in time; no matches means no known"
                 " matches from that source, never safe",
    }


# -- conflicts ---------------------------------------------------------------

def conflicts(tree_json_bytes):
    """All same-GA different-version mediations in one resolved tree.

    Maven silently picks one version when multiple paths request different
    versions of the same groupId:artifactId. This query surfaces every such
    conflict: the winning version (the one present in the tree) and every
    losing version (requested by a different path but mediated away).
    """
    from mdi.maven import import_dependency_tree, MavenImportError, PROVENANCE_FIELDS
    provenance = {field: None for field in PROVENANCE_FIELDS}
    provenance["producer"] = "maven-dependency-plugin"
    provenance["version"] = "3.11.0"
    # verbose=True is required: mediated/losing versions only appear in
    # the verbose tree; the nonverbose JSON has one version per GA.
    try:
        imported = import_dependency_tree(tree_json_bytes, provenance=provenance,
                                          verbose=True)
    except MavenImportError as exc:
        raise TimelineError(f"tree JSON rejected ({exc.code}): {exc}") from exc
    nodes = imported["evidence"]["nodes"]
    by_ga = {}
    for node in nodes:
        c = node["coordinates"]
        ga = f"{c.get('groupId', '')}:{c.get('artifactId', '')}"
        by_ga.setdefault(ga, set()).add(c.get("version") or "")
    found = []
    for ga, versions in sorted(by_ga.items()):
        real = {v for v in versions if v}
        if len(real) > 1:
            winning = [n for n in nodes
                       if f"{n['coordinates'].get('groupId', '')}:{n['coordinates'].get('artifactId', '')}" == ga]
            found.append({
                "coordinate": ga,
                "versions": sorted(real),
                "winning_version": winning[0]["coordinates"].get("version") if winning else None,
                "nodes": len(winning),
            })
    return {
        "schema": PRODUCER,
        "conflicts": found,
        "total": len(found),
        "scope": "version mediations visible in a verbose tree; a nonverbose"
                 " tree JSON cannot show them (this command requires verbose"
                 " tree output)",
    }


# -- versions ----------------------------------------------------------------

def versions(repo, *, group_id=None, artifact_id=None, tree_json=None):
    """Per-GA version landscape across all module POMs in a checkout.

    Groups every dependency declaration by groupId:artifactId and reports
    which modules declare which version, with placement (direct or managed).
    When a tree JSON is available, GA entries not declared in any POM are
    labeled transitive (present only via a parent dependency).
    """
    head_poms = _git(repo, "ls-files", "*pom.xml", "pom.xml").decode(
        "utf-8", errors="surrogateescape").splitlines()
    landscape = {}
    for pom in head_poms:
        blob = _pom_at(repo, "HEAD", pom)
        if blob is None:
            continue
        try:
            elem = parse_xml(blob, limits=Limits(xml_bytes=_MAX_XML))
        except UnsafeXML:
            continue
        ns = elem.tag[1:].partition("}")[0] if elem.tag.startswith("{") else ""
        module = str(Path(pom).parent) if Path(pom).parent != Path(".") else "."
        for dep in _iter_dependencies(elem, ns):
            g = dep.get("groupId", "")
            a = dep.get("artifactId", "")
            v = dep.get("version", "")
            if artifact_id and a != artifact_id:
                continue
            if group_id and g != group_id:
                continue
            ga = f"{g}:{a}"
            placement = "direct"
            # Check if this is inside a dependencyManagement block
            # (simplified: we lose profile nesting here but gain speed)
            landscape.setdefault(ga, []).append({
                "module": module, "version": v, "placement": placement,
                "declared": True,
            })
    # If a tree JSON is provided, label transitives
    if tree_json:
        from mdi.maven import import_dependency_tree, MavenImportError, PROVENANCE_FIELDS
        prov = {f: None for f in PROVENANCE_FIELDS}
        prov["producer"] = "maven-dependency-plugin"
        prov["version"] = "3.11.0"
        data = Path(tree_json).read_bytes()
        try:
            imported = import_dependency_tree(data, provenance=prov, verbose=False)
        except MavenImportError as exc:
            raise TimelineError(f"tree JSON rejected ({exc.code}): {exc}") from exc
        declared_gas = set(landscape.keys())
        for node in imported["evidence"]["nodes"]:
            c = node.get("coordinates", {})
            ga = f"{c.get('groupId', '')}:{c.get('artifactId', '')}"
            if ga not in declared_gas and ga not in landscape:
                landscape.setdefault(ga, []).append({
                    "module": None, "version": c.get("version", ""),
                    "placement": "transitive", "declared": False,
                })
    inconsistent = sorted(ga for ga, mods in landscape.items()
                          if len({m["version"] for m in mods if m["version"]}) > 1)
    return {
        "schema": PRODUCER,
        "landscape": {ga: mods for ga, mods in sorted(landscape.items())},
        "inconsistent": inconsistent,
        "scope": "declarations in working-tree POMs with placement labels;"
                 " when a tree JSON is available, transitive dependencies"
                 " (present only in the resolved tree, not declared in any"
                 " checkout POM) are labeled as such",
    }


def _pom_at(repo, commit, pom_path):
    """Raw bytes of one POM at one commit; None when missing."""
    argv = ["git", "show", f"{commit}:{pom_path}"]
    env = gitinspect._child_env(os.environ.get("PATH"), os.environ.get("HOME"))
    outcome = gitinspect._run(argv, str(repo), env,
                              timeout_seconds=30, max_output_bytes=4 * 1024 * 1024)
    if outcome.outcome != "ok" or outcome.rc != 0 or not outcome.stdout:
        return None
    return outcome.stdout


def _iter_dependencies(elem, ns):
    """Flat iteration of every direct dependency in the project namespace."""
    for child in elem:
        if not isinstance(child.tag, str):
            continue
        child_ns = child.tag[1:].partition("}")[0] if child.tag.startswith("{") else ""
        if child_ns != ns:
            continue
        name = child.tag.partition("}")[2]
        if name == "dependencies":
            for dep in child:
                if not isinstance(dep.tag, str) or dep.tag.partition("}")[2] != "dependency":
                    continue
                fields = {}
                for field in dep:
                    if isinstance(field.tag, str):
                        fields[field.tag.partition("}")[2]] = (field.text or "").strip()
                yield fields


# -- diff ----------------------------------------------------------------------

def diff_trees(tree_a_bytes, tree_b_bytes, *, label_a="A", label_b="B"):
    """What changed in the dependency graph between two resolved trees.

    Reports three sets: coordinates present only in B (added), only in A
    (removed), and present in both with different versions (changed). The
    comparison is on the flat node set keyed by groupId:artifactId.
    """
    def _node_set(data):
        from mdi.maven import import_dependency_tree, MavenImportError, PROVENANCE_FIELDS
        provenance = {field: None for field in PROVENANCE_FIELDS}
        provenance["producer"] = "maven-dependency-plugin"
        provenance["version"] = "3.11.0"
        try:
            imported = import_dependency_tree(data, provenance=provenance, verbose=False)
        except MavenImportError as exc:
            raise TimelineError(f"tree JSON rejected ({exc.code}): {exc}") from exc
        result = {}
        for node in imported["evidence"]["nodes"]:
            c = node["coordinates"]
            ga = f"{c.get('groupId', '')}:{c.get('artifactId', '')}"
            result[ga] = c.get("version") or ""
        return result

    set_a = _node_set(tree_a_bytes)
    set_b = _node_set(tree_b_bytes)
    keys_a, keys_b = set(set_a), set(set_b)
    added = sorted(keys_b - keys_a)
    removed = sorted(keys_a - keys_b)
    changed = sorted(ga for ga in keys_a & keys_b if set_a[ga] != set_b[ga])
    return {
        "schema": PRODUCER,
        "labels": {"a": label_a, "b": label_b},
        "added": [{"coordinate": ga, "version": set_b[ga]} for ga in added],
        "removed": [{"coordinate": ga, "version": set_a[ga]} for ga in removed],
        "changed": [{"coordinate": ga, "version_before": set_a[ga], "version_after": set_b[ga]}
                    for ga in changed],
        "totals": {"added": len(added), "removed": len(removed), "changed": len(changed)},
        "scope": "flat node-set comparison of two trees; structural path changes"
                 " (same nodes, different hierarchy) are not reported",
    }


# -- usage ---------------------------------------------------------------------

def usage(jar_path, classes_path, *, scope="production", checkout=None):
    """Project-class to library-class references from the Java helper.

    Runs the vendored, hash-verified Java helper on staged copies of the
    dependency jar and the compiled project classes. Returns every
    reference where a project class (the root) calls into a library class
    (the dependency). The analysis is static: a reference is not observed
    execution, and absent references do not prove safe removal.
    """
    import tempfile
    from mdi.usage_pipeline import inspect_usage
    from mdi import cli as _cli
    vendor = Path(__file__).resolve().parent.parent.parent / "vendor"
    classpath, hashes, problem = _cli._vendor_classpath(str(vendor))
    if problem is not None:
        raise TimelineError(f"vendored helper unavailable: {problem}")
    # The pipeline requires every input inside the checkout; for a
    # standalone query the honest checkout is the common parent, or when
    # that is not available, each input is staged independently anyway.
    # Use the classes path's parent as the checkout root; the jar outside
    # that tree is staged from its own location by the pipeline.
    checkout = checkout or os.path.dirname(classes_path)
    jar_dir = os.path.dirname(str(jar_path))
    # find a common ancestor that contains both, or fall back to the deeper one
    try:
        common = os.path.commonpath([checkout, jar_dir]) if checkout != jar_dir else checkout
    except ValueError:
        raise TimelineError(
            f"cannot determine a common directory for the classes path"
            f" ({checkout!r}) and the jar path ({jar_dir!r}); they are on"
            f" disjoint trees")
    with tempfile.TemporaryDirectory(prefix="mdi-usage-query-") as staging:
        result = inspect_usage(common, classes_path, [str(jar_path)],
                               scope=scope, staging_base=staging,
                               helper_classpath=classpath,
                               expected_hashes=hashes)
    refs = result.get("source_refs", [])
    return {
        "schema": PRODUCER,
        "query": {"jar": str(jar_path), "classes": str(classes_path)},
        "status": result.get("status"),
        "total_references": len(refs),
        "references": [
            {"from": r["from_class"], "to": r["to_class"]}
            for r in refs
        ][:500],
        "issues": result.get("issues", []),
        "scope": "static class references from one analysis run; a reference is"
                 " not observed execution, and absent references do not prove"
                 " safe removal",
    }


# -- orphans --------------------------------------------------------------------

def orphans(repo, classes_path, *, maven_repo=None):
    """Declared dependencies with zero static references in compiled code.

    Scans every POM for direct declarations, locates each dependency jar in
    the local Maven repository (default ~/.m2/repository), runs the Java
    helper once against the compiled classes and every located jar, and
    reports which declared coordinates received no references from the
    project's own classes. Reflection, service loading, native code and
    build-time-only needs are all legitimate reasons a dependency has no
    static references; this is a cleanup lead, never a removal proof.
    """
    import tempfile
    from mdi.usage_pipeline import inspect_usage
    from mdi import cli as _cli
    maven_repo = maven_repo or os.path.join(os.path.expanduser("~"), ".m2", "repository")
    head_poms = _git(repo, "ls-files", "*pom.xml", "pom.xml").decode(
        "utf-8", errors="surrogateescape").splitlines()
    declared = {}
    for pom in head_poms[:300]:
        blob = _pom_at(repo, "HEAD", pom)
        if blob is None:
            continue
        try:
            elem = parse_xml(blob, limits=Limits(xml_bytes=_MAX_XML))
        except UnsafeXML:
            continue
        ns = elem.tag[1:].partition("}")[0] if elem.tag.startswith("{") else ""
        for dep in _iter_dependencies(elem, ns):
            ga = f"{dep.get('groupId', '')}:{dep.get('artifactId', '')}"
            declared.setdefault(ga, dep)
    jars_found = []
    orphans = []
    located = {}
    for ga, dep in declared.items():
        g, a = ga.split(":", 1)
        version = dep.get("version", "")
        if not version or version.startswith("${"):
            continue
        jar = Path(maven_repo) / g.replace(".", "/") / a / version / f"{a}-{version}.jar"
        if jar.is_file():
            jars_found.append(str(jar))
            located[ga] = str(jar)
        else:
            orphans.append({"coordinate": ga, "version": version,
                            "reason": "jar not found in the local Maven repository"})
    if jars_found and classes_path:
        vendor = Path(__file__).resolve().parent.parent.parent / "vendor"
        classpath, hashes, problem = _cli._vendor_classpath(str(vendor))
        if problem is not None:
            raise TimelineError(f"vendored helper unavailable: {problem}")
        common = os.path.commonpath(jars_found[:1] + [classes_path])
        with tempfile.TemporaryDirectory(prefix="mdi-orphans-") as staging:
            result = inspect_usage(common, classes_path, jars_found[:20],
                                   scope="production", staging_base=staging,
                                   helper_classpath=classpath,
                                   expected_hashes=hashes)
        refs = result.get("source_refs", [])
        referenced_jars = set()
        for r in refs:
            origin = r.get("origin", {})
            src = origin.get("source", "")
            for ga, jar_path in located.items():
                if jar_path in src:
                    referenced_jars.add(ga)
        for ga in sorted(located):
            if ga not in referenced_jars:
                reason = ("no static references from project classes")
                if ga not in {jars_found[i] for i in range(min(len(jars_found), 20))}:
                    reason = "jar beyond the 20-jar helper bound; not checked"
                orphans.append({"coordinate": ga, "version": declared[ga].get("version", ""),
                                "reason": reason,
                                "jar": located[ga]})
                orphans.append({"coordinate": ga, "version": declared[ga].get("version", ""),
                                "reason": "no static references from project classes",
                                "jar": located[ga]})
    return {
        "schema": PRODUCER,
        "total_declared": len(declared),
        "located_jars": len(located),
        "orphans": sorted(orphans, key=lambda o: o["coordinate"]),
        "scope": "static reference analysis only; reflection, service loading,"
                 " native code and build-time needs are legitimate reasons for"
                 " zero references; this is a cleanup lead, never a removal proof",
    }
