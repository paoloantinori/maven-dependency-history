# Maven Dependency Investigator: Project Context

A bundled coding-agent skill that answers "why is this Maven dependency in the output?" from a local checkout with cited evidence instead of guesses. Every claim cites a run-local evidence ID; missing evidence stays explicit.

## Commands

```sh
# Full suite (58 modules, 1837 tests; requires a JDK for the Java tests)
MDI_TEST_DEPENDENCY_DIR="$PWD/skills/maven-dependency-investigator/vendor/lib" \
  PYTHONWARNINGS=always::ResourceWarning \
  python3 -m unittest discover -s tests -t . -q

# Quick query (offline, no run-dir needed)
python3 skills/maven-dependency-investigator/scripts/investigate.py timeline \
  --repo /path/to/project --query com.fasterxml.jackson.core:jackson-databind
```

The CLI entry point is `skills/maven-dependency-investigator/scripts/investigate.py`. Sixteen verbs; nine are standalone queries that need no run-dir: `timeline`, `releases`, `chain`, `security`, `conflicts`, `versions`, `diff`, `usage`, `orphans`. The other seven (`discover`, `select`, `collect`, `import`, `report`, `propose`, `execute`) form the investigation workflow using a run-dir and `--expect-revision` for continuity.

## Architecture

```
skills/maven-dependency-investigator/
+-- SKILL.md                    # agent-facing skill definition
+-- scripts/investigate.py      # CLI entry point (thin launcher)
+-- scripts/mdi/                # ~40 Python modules (the collector)
|   +-- cli.py                  # all 16 CLI verbs
|   +-- discover.py             # POM scanning (safe XML, namespace-aware)
|   +-- runstore.py             # append-only evidence chain (revisioned snapshots)
|   +-- gitinspect.py           # bounded git runner (SIGCHLD gate, pgid guard)
|   +-- usage.py                # bounded Java helper runner (same guards)
|   +-- maven_executor.py       # consent-gated Maven execution
|   +-- advisory_executor.py    # consent-gated OSV lookup
|   +-- fsprobe.py              # statfs probe (tmpfs/ramfs refusal)
|   +-- timeline.py             # Git history + release coverage queries
|   +-- queries.py              # standalone query implementations
+-- helper/                     # Java source for the usage analyzer
|   +-- src/main/java/mdi/UsageHelper.java
+-- vendor/                     # pre-built helper.jar + pinned deps + SHA256SUMS
+-- references/                 # agent-facing reference docs
tests/                          # 58 unittest modules, ~1837 tests
docs/verification/              # verification evidence and dispositions
```

## Safety constraints (do not break these)

- **/tmp on this host is tmpfs (RAM)**: test scratch and Maven control homes must stay on disk. The test suite auto-redirects to `/var/tmp`; the CLI refuses tmpfs state dirs; `TMPDIR` must point to disk for Maven execution.
- **Every `os.killpg` validates its pgid**: a mock or fabricated pid coerces to 1, and `killpg(1)` is `kill(-1)`, which is SIGKILL to every process of the user. The pgid guard (int, not bool, >1) exists in five engines and must never be removed.
- **Runners refuse non-default SIGCHLD dispositions**: SIG_IGN makes the kernel auto-reap, fabricating rc 0 for real failures.
- **POM parsing is namespace-aware**: never use bare tag names without checking the namespace.
- **The helper.jar in `vendor/` is hash-pinned**: rebuild only via `helper/build_local.py --dependency-dir vendor/lib --output-dir <dir>`; never build at investigation time.

## Gotchas

- Python was upgraded from 3.14 to 3.15 mid-project: `resource.RLIM_INFINITY` changed from -1 to 2^64-1, and `subprocess.Popen` now uses pidfds (closes an extra fd through `os.close`). Tests are adapted; don't re-add the old patterns.
- The dependency:tree JSON output is nonverbose. `import_dependency_tree(verbose=True)` expects verbose origin comments that JSON output doesn't have.
- The CLI `execute` verb uses a standing policy for diagnostic operations: no approval file is needed. A supplied approval file with `approved: false` still denies. Raw capture always requires a real approval file.
- `os.killpg(pid, sig)` with pid=1 issues `kill(-1, SIGKILL)` to every process of the user. This killed the desktop twice. The guards exist for a reason.

## Backlog

Tracked in `backlog/`. All 27 original tasks are Done. Three Draft/To Do tasks track deferred capabilities (rewritten bytecode recognition, reachability provider evaluation, live forge retrieval). Code completion requires `/simplify`, high-effort code review, and applicable tests with recorded evidence. Nothing is committed or pushed automatically.
