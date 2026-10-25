"""MCP stdio server exposing the nine standalone query commands as native tools.

Each function maps to one MCP tool with typed parameters. The underlying
implementations in timeline.py and queries.py are pure query-in/JSON-out;
this module is a thin wrapper, not a second implementation.

Launch: ``uv run --with mcp python mdi/mcp_server.py``
or equivalently from an agent config: ``uvx --from mcp python mcp_server.py``
"""

import sys
from pathlib import Path

# Make the sibling modules importable when launched from any cwd.
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# The mcp package is a runtime dependency for launching this server;
# declare it so uv/uvx resolves it. Do not install into the system Python.

from mcp.server.mcpserver import MCPServer

from mdi import queries as queries_module
from mdi import timeline as timeline_module
from mdi.timeline import TimelineError

mcp = MCPServer("maven-dependency-investigator")


def _split_query(query):
    """groupId:artifactId -> (group or None, artifact or None)."""
    if ":" in query:
        group, _, artifact = query.partition(":")
        return (group or None, artifact or None)
    return (None, query or None)


def _read_tree(path):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read tree file {path!r}: {exc}") from exc


def _wrap_timeline(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TimelineError as exc:
        raise ValueError(str(exc)) from exc


def _wrap_queries(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TimelineError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def timeline(repo: str, query: str) -> dict:
    """When was this dependency added to the project? Who added it? What versions over time?

    Walks the local Git history of POM files for every declaration event:
    introduced, version-changed, removed, re-added. Each event cites the
    commit hash, date, author, committer, message and POM path. Offline,
    no Maven, no network.
    """
    group, artifact = _split_query(query)
    return _wrap_timeline(timeline_module.dependency_timeline, repo,
                          group_id=group, artifact_id=artifact)


@mcp.tool()
def releases(repo: str, query: str) -> dict:
    """Which tagged releases of this project carry this dependency declaration?

    Enumerates local Git tags, reads each POM that ever declared the
    coordinate at each tag, and reports per tag: version declared or absent.
    Declaration coverage only, never a resolved dependency tree of a release.
    """
    group, artifact = _split_query(query)
    return _wrap_timeline(timeline_module.release_coverage, repo,
                          group_id=group, artifact_id=artifact)


@mcp.tool()
def chain(tree: str, query: str) -> dict:
    """Every inclusion path from the tree root to the target dependency.

    Reads a tree JSON file produced by dependency:tree and traces every path
    from the root to the requested coordinate. Multiple paths through
    different parents are all reported.
    """
    group, artifact = _split_query(query)
    data = _read_tree(tree)
    return _wrap_timeline(timeline_module.inclusion_chain, data,
                          group_id=group, artifact_id=artifact)


@mcp.tool()
def security(query: str) -> dict:
    """Known OSV vulnerabilities for one exact coordinate.

    Makes one HTTPS request to api.osv.dev with the coordinate query only.
    Under the standing policy this runs without a pause. An empty
    vulnerabilities list means no known matches from OSV at this time,
    never that the coordinate is safe.
    """
    group, artifact, version = _parse_gav(query)
    return _wrap_queries(queries_module.security, group, artifact, version)


@mcp.tool()
def conflicts(tree: str) -> dict:
    """Version mediations in a resolved dependency tree.

    Finds every groupId:artifactId that appears with multiple different
    versions in the tree, meaning Maven silently mediated away a losing
    version. Requires verbose tree output (the pinned nonverbose JSON
    format cannot show mediated versions).
    """
    data = _read_tree(tree)
    return _wrap_queries(queries_module.conflicts, data)


@mcp.tool()
def versions(repo: str, query: str = "") -> dict:
    """Per-GA version landscape across all module POMs in a checkout.

    Groups every direct dependency declaration by groupId:artifactId and
    reports which modules declare which version. Inconsistent versions
    across modules are the governance signal. Optional query filters to a
    single coordinate.
    """
    group, artifact = _split_query(query) if query else (None, None)
    return _wrap_timeline(queries_module.versions, repo,
                          group_id=group, artifact_id=artifact)


@mcp.tool()
def diff(tree_a: str, tree_b: str, label_a: str = "A", label_b: str = "B") -> dict:
    """What changed in the dependency graph between two resolved trees.

    Reports three sets: coordinates present only in B (added), only in A
    (removed), and present in both with different versions (changed).
    """
    data_a = _read_tree(tree_a)
    data_b = _read_tree(tree_b)
    return _wrap_queries(queries_module.diff_trees, data_a, data_b,
                         label_a=label_a, label_b=label_b)


@mcp.tool()
def usage(jar: str, classes: str, scope: str = "production") -> dict:
    """Which project classes reference the classes of this library jar?

    Runs the vendored Java helper on staged copies of the dependency jar
    and the compiled project classes. Returns project-to-library class
    references. Static analysis: a reference is not observed execution,
    and absent references do not prove safe removal.
    """
    return _wrap_queries(queries_module.usage, jar, classes, scope=scope)


@mcp.tool()
def orphans(repo: str, classes: str, maven_repo: str = "") -> dict:
    """Declared dependencies with zero static references in compiled code.

    Scans every POM for direct declarations, locates each dependency jar in
    the local Maven repository, runs the Java helper once against the
    compiled classes and all located jars, and reports which coordinates
    received no references. Reflection, service loading and build-time-only
    needs are legitimate reasons for zero references; this is a cleanup
    lead, never a removal proof.
    """
    kwargs = {"maven_repo": maven_repo} if maven_repo else {}
    return _wrap_queries(queries_module.orphans, repo, classes, **kwargs)


def _parse_gav(query):
    parts = query.split(":")
    if len(parts) == 3:
        return (parts[0] or None, parts[1] or None, parts[2] or None)
    raise ValueError(
        f"security requires groupId:artifactId:version, got {query!r}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
