---
id: TASK-7
title: Assess vulnerable-function reachability with explicit coverage limits
status: Done
assignee: []
created_date: '2026-09-10 14:49'
updated_date: '2026-10-23 06:27'
labels:
  - follow-up
  - security
dependencies: []
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Extend package advisory lookup and class references to vulnerable-function evidence through evaluated existing tools. Do not conflate a package match or class usage with exploitability. Provider choice and deployment require later design approval.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Link function-level claims to identifiable analysis evidence and vulnerability mappings.
- [x] #2 Surface unsupported reflection, incomplete call graphs and missing mappings.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Local next-phase design synthesis underway from existing research; no provider selected/deployed or vulnerability reachability claim made. Existing requirement for later design approval retained.

Started bounded reachability evidence contract evaluation from approved design/current research. No live provider selected/executed, no classreference->function exploitability inference. Need identifiable function/mapping/tool/graph coverage and explicit unsupported cases; import-only contract recommendation before source implementation. Existing provider-selection design gate retained.

Evaluation a6f949 recommends vendor-neutral recorded function evidence but explicitly NOT TASK7 completion/provider selection. Proposed closed artifact/function JVMdescriptor/mapping/advisoryrefs/path/workload/coverage contract, provider claims qualified, classrefs never become methodpaths, no-path not safe/unreachable. Current native provider export/API NOT VERIFIED; retained2026-09-10 research not fresh proof. Task description+approved Stage7 still require actual existing-tool evaluation and concrete provider/internalscope design approval; synthetic neutral parser alone insufficient. No source implementation authorized from evaluation, no externaldata/tool execution. Continue other actionable backlog while this provider gate remains.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Maintainer decision 2026-10-23: deferred to a future cycle. The vendor-neutral function-evidence contract stays recorded in the task notes and research; no provider or native-export format was evaluated, no scanner was run, no code was written. Reopen when a concrete provider is worth evaluating.
<!-- SECTION:FINAL_SUMMARY:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
