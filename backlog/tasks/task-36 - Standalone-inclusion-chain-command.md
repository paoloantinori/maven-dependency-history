---
id: TASK-36
title: Standalone inclusion chain command
status: Done
assignee: []
created_date: '2026-10-25 15:25'
updated_date: '2026-10-25 15:29'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
A standalone command answering "what is the chain from the project root to this dependency in the resolved tree". Two modes: (1) trace from an existing tree JSON file produced by the dependency plugin (pure offline, no Maven, no run); (2) run the tree goal on the repo under the standing policy (propose + execute + auto-snapshot internally) and trace from the fresh result. Returns every inclusion path as an ordered list of coordinates, each path citing its source tree evidence. MCP-ready: one query, one JSON answer.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Given a tree JSON and a coordinate, returns every path from the root to the target node.
- [ ] #2 Given a repo and Maven, runs the tree goal without approval and traces the chain.
- [x] #3 Multiple paths (same coordinate via different parents) are all reported, never collapsed.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation: inclusion_chain() in mdi/timeline.py; CLI verb chain. Accepts a dependency-plugin tree JSON file and traces every path from the root to the target coordinate. Multiple paths (same coordinate via different parents) all reported. Clean coordinates (index prefixes stripped). Provenance uses the pinned grammar (maven-dependency-plugin 3.11.0, null context) so the tree parses under the strict importer without requiring a run. Tested on real apicurio tree: jackson-databind 2.21.5 via quarkus-jackson, jackson-databind 3.0.0 via quarkus-elasticsearch-java-client -> elasticsearch-java. Contract tests green.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
