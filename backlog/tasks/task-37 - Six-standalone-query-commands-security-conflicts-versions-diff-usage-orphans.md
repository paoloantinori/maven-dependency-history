---
id: TASK-37
title: >-
  Six standalone query commands: security, conflicts, versions, diff, usage,
  orphans
status: Done
assignee: []
created_date: '2026-10-25 15:35'
updated_date: '2026-10-25 15:44'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Six discrete standalone commands, each one query = one JSON answer, all MCP-ready. security: coordinate in, OSV vulnerabilities out (direct transport, no run-store). conflicts: tree JSON in, version mediation conflicts out. versions: repo in, per-GA version landscape across modules out. diff: two tree JSONs in, dependency graph changes out. usage: jar + compiled classes in, project-to-library references out. orphans: repo + classes in, declared-but-unreferenced dependencies out.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Each command is standalone (no run-dir required) and returns a self-contained JSON document.
- [x] #2 security consults OSV under the standing policy without a run-store lifecycle.
- [x] #3 conflicts finds all same-GA different-version mediations in a tree.
- [x] #4 versions maps GA to versions across all module POMs.
- [x] #5 diff reports added, removed and version-changed coordinates between two trees.
- [x] #6 usage returns project-class to library-class references from the Java helper.
- [x] #7 orphans reports declared dependencies with zero static references.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
All six commands implemented in mdi/queries.py and wired as CLI verbs. security: direct OSV query via DeadlineTransport, no run-store, standing policy. conflicts: tree node GA grouping for version mediations. versions: POM scan with GA grouping and inconsistency detection. diff: flat node-set comparison of two trees (added/removed/changed). usage: full inspect_usage with vendored helper, commonpath checkout for cross-tree inputs, MR jar support. orphans: POM scan + Maven repo jar location + batch usage analysis with per-jar reference attribution. All tested on real apicurio data where applicable. Contract tests 48 green.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
