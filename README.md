# Maven Dependency Investigator

A bundled coding-agent skill for investigating why a Maven dependency appears in a local project or its packaged output.

It collects inspectable evidence for dependency declarations and inclusion paths, packaged copies, static class references, local Git history, and permissioned advisory queries. Missing evidence remains explicit. A declaration is not a resolved dependency, a class reference is not observed execution, and an empty advisory result is not proof of safety.

## Status

The collector, CLI and MCP server passed their recorded reviews and integration tests. The CLI uses file-backed archive inspection with ZIP64 and multi-release jar support. Diagnostic Maven goals run under a standing policy without per-operation approval. All extension tasks are complete.

## Install

**Prerequisites**: Python 3.10+ and git. Optional: [uv](https://docs.astral.sh/uv/) (for the MCP server), a JDK (for `usage` and `orphans`), Maven (for the full investigation workflow).

```sh
git clone https://github.com/paoloantinori/maven-dependency-history.git
```

That is all the installation requires: the tool is self-contained. The Python scripts, the Java helper and the pinned dependencies live inside the cloned repo.

Verify it works:

```sh
python3 maven-dependency-investigator/scripts/investigate.py
```

You should see a JSON welcome document listing every available query.

If you want to use it as an MCP server from Claude Code or another agent, install [uv](https://docs.astral.sh/uv/) and see the MCP section below.

## Three ways to use it

### 1. As an MCP server (preferred for agents)

Add to a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "maven-dependency-investigator": {
      "command": "uv",
      "args": ["run", "--with", "mcp", "python",
               "/path/to/maven-dependency-history/skills/maven-dependency-investigator/scripts/mdi/mcp_server.py"]
    }
  }
}
```

The agent discovers nine tools (timeline, releases, chain, security, conflicts, versions, diff, usage, orphans) plus two resources: `mdi://guide` (complete how-to) and `mdi://tools/{name}` (per-tool docs).

### 2. As a CLI with standalone queries

Run the entry point with no arguments for a welcome overview:

```sh
python3 skills/maven-dependency-investigator/scripts/investigate.py
```

Nine commands answer discrete questions without a run-dir:

```sh
python3 $INV timeline --repo /path/to/project --query groupId:artifactId
# When was it added? Who added it? What versions over time?

python3 $INV releases --repo /path/to/project --query groupId:artifactId
# Which tagged releases carry it?

python3 $INV chain --tree /path/to/tree.json --query groupId:artifactId
# What is the inclusion path from root to this dependency?

python3 $INV security --query groupId:artifactId:version
# Known OSV vulnerabilities for this exact coordinate?

python3 $INV conflicts --tree /path/to/tree.json
# Version mediations in the resolved tree?

python3 $INV versions --repo /path/to/project [--tree tree.json]
# Per-GA version landscape; with --tree, transitives are labeled too

python3 $INV diff --tree-a a.json --tree-b b.json
# What changed in the dependency graph between two trees?

python3 $INV usage --jar /path/to/lib.jar --classes /path/to/target/classes
# Which project classes reference this library?

python3 $INV orphans --repo /path/to/project --classes /path/to/target/classes
# Declared dependencies with zero static references?
```

Queries accept partial names: `--query kiota` finds all kiota-related artifacts. Version placeholders like `${kiota.libs.version}` are resolved to real version numbers through POM properties.

### 3. As a full investigation workflow

For the complete picture with evidence IDs and a cited report:

```sh
python3 $INV discover --repo /path/to/project --query groupId:artifactId \
  --state-dir /path/outside/project/investigator-state
python3 $INV select --run-dir <run> --expect-revision 1 --candidate <id>
python3 $INV collect --run-dir <run> --expect-revision 2
python3 $INV report --run-dir <run>
```

Use the returned run directory, revision and candidate identifiers for subsequent commands. Do not copy identifiers from another investigation.

## Permission boundaries

Local inspection never invokes Maven or a project wrapper. Archive discovery runs a trusted Python worker against private captured files outside the checkout; optional usage analysis runs only the verified bundled Java helper against staged copies, not application classes.

Diagnostic Maven goals and advisory lookups run automatically under the maintainer's standing policy, with every run audited (exact command or destination, outcome); a supplied denial file still refuses. Explicit host approval is required only for raw diagnostic retention and for replacing an existing output file. The CLI never invents a host approval. Maven execution is not sandboxed; project code and configuration may override defaults and cause side effects. Read the [permission contract](skills/maven-dependency-investigator/references/permissions.md) before proposing execution.

The collector does not remove dependencies, edit project POMs, apply upgrades, commit changes, or publish results.

## Development verification

Run the unittest suite with an explicit pinned dependency directory:

```sh
MDI_TEST_DEPENDENCY_DIR="$PWD/skills/maven-dependency-investigator/vendor/lib" \
  PYTHONWARNINGS=always::ResourceWarning \
  python3 -m unittest discover -s tests -t . -q
```

The real Java tests require a JDK and fail clearly when prerequisites are missing; they do not download dependencies. When `/tmp` is tmpfs the suite automatically places its temporary trees under `/var/tmp`. See [Java prerequisites](docs/verification/java-test-prerequisites.md).

Work is tracked under [backlog/](backlog/). Code completion requires `/simplify`, high-effort code review, and applicable tests with recorded evidence.
