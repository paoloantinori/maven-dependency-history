# Maven Dependency Investigator

A bundled coding-agent skill for investigating why a Maven dependency appears in a local project or its packaged output.

It collects inspectable evidence for dependency declarations and inclusion paths, packaged copies, static class references, local Git history, and permissioned advisory queries. Missing evidence remains explicit. A declaration is not a resolved dependency, a class reference is not observed execution, and an empty advisory result is not proof of safety.

## Status

The initial collector and CLI passed their recorded reviews and configured integration tests. The current CLI now uses file-backed, isolated archive inspection with ZIP64 support; TASK-25 records its completed archive-extension reviews and verification. Read the [archive limits and compatibility modes](skills/maven-dependency-investigator/references/cli.md#archive-coverage-and-limits) before use. Further evidence extensions remain separately tracked.

## Use the bundled skill

Read [SKILL.md](skills/maven-dependency-investigator/SKILL.md) and the [CLI reference](skills/maven-dependency-investigator/references/cli.md). The skill includes its collector scripts and a packaged Java helper; no separate global CLI installation is required.

Start with local discovery. Choose a state directory outside the inspected checkout:

```sh
python3 /path/to/maven-dependency-history/skills/maven-dependency-investigator/scripts/investigate.py \
  discover --repo /path/to/project --query groupId:artifactId \
  --state-dir /path/outside/project/investigator-state
```

Use the returned run directory, revision and candidate identifiers for subsequent commands. Do not copy identifiers from another investigation. The available commands are `discover`, `select`, `collect`, `import`, `report`, `propose`, and `execute`; use their `--help` output for flags.

Dependency trees import conservatively as diagnostic evidence unless their nonverbose mode is explicitly attested. Historical producer context is distinct from the current checkout. See the [interpretation guide](skills/maven-dependency-investigator/references/interpretation.md).

## Permission boundaries

Local inspection never invokes Maven or a project wrapper. Archive discovery runs a trusted Python worker against private captured files outside the checkout; optional usage analysis runs only the verified bundled Java helper against staged copies, not application classes. Parent-side capture and filesystem IO remain cooperatively bounded; the archive worker's deadline interrupts child decompression, not every operation in the investigation.

Every Maven invocation and advisory request requires a disclosed, single-use approval. The CLI never invents approval records. Maven execution is not sandboxed; project code and configuration may override defaults and cause side effects. Existing output files are refused by default. Read the [permission contract](skills/maven-dependency-investigator/references/permissions.md) before proposing execution.

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
