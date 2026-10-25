---
id: TASK-40
title: Distinguish direct vs transitive dependencies in the versions command
status: Done
assignee: []
created_date: '2026-10-25 16:39'
updated_date: '2026-10-25 16:47'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The versions command currently flattens every dependency declaration from every POM without distinguishing direct declarations from managed/transitive ones. The resolved tree already knows the inclusion path; the POM analysis should label each declaration as direct (in the modules own dependencies block), managed (dependencyManagement), or inherited (from a parent POM). Additionally, when a resolved tree is available, the command should distinguish which GA entries are only present transitively (not declared anywhere in the checkout).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 The versions command output distinguishes direct, managed, and parent-inherited declarations per module.
- [x] #2 When a tree JSON is available, GA entries not declared in any checkout POM are labeled transitive.
- [x] #3 Existing tests remain green.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation: _declaration_entries now emits placement (direct or managed) per entry. versions() accepts an optional tree_json parameter: GA entries found in the resolved tree but not declared in any checkout POM are labeled transitive. The pickaxe needle was changed from the XML-tagged form (<artifactId>X</artifactId>) to the bare artifact string, so version-change commits (which only touch the version line) are now caught. Contract tests 48 green, usage-related modules 160 green.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
