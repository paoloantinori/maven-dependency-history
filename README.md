# Maven Dependency Investigator

A bundled coding-agent skill for investigating why a Maven dependency appears in a local project or its packaged output.

It collects inspectable evidence for dependency declarations and inclusion paths, packaged copies, static class references, local Git history, and permissioned advisory queries. Missing evidence remains explicit. A declaration is not a resolved dependency, a class reference is not observed execution, and an empty advisory result is not proof of safety.

## Status

The initial collector and CLI passed their recorded reviews and configured integration tests. The current CLI now uses file-backed, isolated archive inspection with ZIP64 support. Read the [archive limits and compatibility modes](skills/maven-dependency-investigator/references/cli.md#archive-coverage-and-limits) before use. All extension tasks (archive hardening, multi-release jar support, standing-policy diagnostics, timeline and release queries) are complete.

## Use the bundled skill

Read [SKILL.md](skills/maven-dependency-investigator/SKILL.md) and the [CLI reference](skills/maven-dependency-investigator/references/cli.md). The skill includes its collector scripts and a packaged Java helper; no separate global CLI installation is required.

Start with local discovery. Choose a state directory outside the inspected checkout:

```sh
python3 /path/to/maven-dependency-history/skills/maven-dependency-investigator/scripts/investigate.py \
  discover --repo /path/to/project --query groupId:artifactId \
  --state-dir /path/outside/project/investigator-state
```

Use the returned run directory, revision and candidate identifiers for subsequent commands. Do not copy identifiers from another investigation.

### Standalone query commands

Nine commands answer discrete questions without a run-dir, no Maven, no network:

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

python3 $INV versions --repo /path/to/project
# Per-GA version landscape across all module POMs?

python3 $INV diff --tree-a a.json --tree-b b.json
# What changed in the dependency graph between two trees?

python3 $INV usage --jar /path/to/lib.jar --classes /path/to/target/classes
# Which project classes reference this library?

python3 $INV orphans --repo /path/to/project --classes /path/to/target/classes
# Declared dependencies with zero static references?
```

The other commands (`discover`, `select`, `collect`, `import`, `report`, `propose`, `execute`) form the investigation workflow using a run-dir and `--expect-revision` for continuity. Dependency trees import conservatively as diagnostic evidence unless their nonverbose mode is explicitly attested. Historical producer context is distinct from the current checkout. See the [interpretation guide](skills/maven-dependency-investigator/references/interpretation.md).

## Permission boundaries

Local inspection never invokes Maven or a project wrapper. Archive discovery runs a trusted Python worker against private captured files outside the checkout; optional usage analysis runs only the verified bundled Java helper against staged copies, not application classes. Parent-side capture and filesystem IO remain cooperatively bounded; the archive worker's deadline interrupts child decompression, not every operation in the investigation.

Diagnostic Maven goals and advisory lookups run automatically under the maintainer's standing policy, with every run audited (exact command or destination, outcome); a supplied denial file still refuses. Explicit host approval is required only for raw diagnostic retention and for replacing an existing output file. The CLI never invents a host approval. Maven execution is not sandboxed; project code and configuration may override defaults and cause side effects. Read the [permission contract](skills/maven-dependency-investigator/references/permissions.md) before proposing execution.

The collector does not remove dependencies, edit project POMs, apply upgrades, commit changes, or publish results.

## Development verification

Run the unittest suite with an explicit pinned dependency directory:

```sh
MDI_TEST_DEPENDENCY_DIR="$PWD/skills/maven-dependency-investigator/vendor/lib" \
  PYTHONWARNINGS=always::ResourceWarning \
  python3 -m unittest discover -s tests -t . -q
```

The real Java tests require a JDK and fail clearly when prerequisites are missing; they do not download dependencies. The archive fixture intentionally emits a duplicate-ZIP-name warning. When `/tmp` is tmpfs the suite automatically places its temporary trees under `/var/tmp` so spool and scratch fixtures stay on disk; set `MDI_TEST_TMP_BASE` to pin a directory explicitly. See [Java prerequisites](docs/verification/java-test-prerequisites.md) and [helper build instructions](skills/maven-dependency-investigator/helper/build_local.py).

Work is tracked under [backlog/](backlog/). Code completion requires `/simplify`, high-effort code review, and applicable tests with recorded evidence. Nothing is committed or pushed automatically.
