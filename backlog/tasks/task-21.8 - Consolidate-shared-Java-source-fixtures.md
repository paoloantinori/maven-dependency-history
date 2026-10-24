---
id: TASK-21.8
title: Consolidate shared Java source fixtures
status: Done
assignee: []
created_date: '2026-09-12 07:29'
updated_date: '2026-09-12 16:56'
labels:
  - test-infrastructure
  - review-advisory
dependencies: []
parent_task_id: TASK-21
priority: low
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Reuse review identified repeated A/B/C/Dup Java source fixtures and associated test helpers across usage helper, runner and pipeline tests. Consolidate into a test-only fixture module after TASK21 integration, keeping java_support focused on prerequisite resolution. Preserve independent behavioral assertions and fixture lifetimes; no runtime dependency or auto-download.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Verify shared snippets actually identical before consolidation; preserve deliberately distinct fixtures.
- [x] #2 All three real-JVM suites remain meaningful and pass with explicit pinned dependencies.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Started isolated test-fixture consolidation after TASK21 integration under explicit continue-all-backlog instruction. Require mechanical identical-source comparison, preserve distinct fixtures/assertions and explicit bundled Java dependencies. Parent final gates remain required.

Parent independently verified delivered shared fixture artifact:80 actual-JVM helper/runner/pipeline tests pass -Werror with explicit vendor/lib. Shared java_fixtures34aefa55c1d8099a77b832df77b8a2facc4be3fa9599c06686ea89b4d41ac212; suitesd9852210/80462fdb/02967e50. Source/constants+sha256 helper consolidated; import has no filesystem/process behavior. Worker exact pre-consolidation identity comparison report still pending recovery, final shared review required before Done.

Applicable real-JVM suites verified80 tests with explicit packaged dependency closure. Identical-source provenance and final code-review still gate task closure; no default skips.

Completion reviewer confirms current fixture artifacts clean, pure import AST, sentinel/MR assertions preserved,80 test cases. Cannot independently recover preconsolidation byte equality without old sources; do not check AC1 from worker assertion alone. Amend fixture docstring to current shared-source/behavior claim, retain exact-comparison gate unresolved unless worker evidence recovered. Optional duplicated sha256 helper in java_support vs java_fixtures explicitly declined for this task: prerequisite helper separation intentional, two tiny standalone loops not justification for new cross-helper dependency.

Parent recovered original PRE-EDIT Read tool-result source blocks from worker output with bounded JSON extraction (not worker summary). Independently AST-parsed all3 original suites and current java_fixtures; every moved constant matches exact original values, MR only helper/pipeline, all3 comparison assertions pass. This resolves AC1 provenance gap without relying on overwritten pyc or fabricated reconstruction. Existing80 real-JVM tests and current high-effort fixture review clean; docstring remains modest shared-source wording.

Recovered worker residual unused import re in test_usage_pipeline.py explicitly declined: pre-existing and unrelated to identical-source extraction, no runtime defect. Optional sha256-helper dedup likewise declined to keep prerequisite module independent. All known review recommendations dispositioned, no untracked follow-up.

Fixture consolidation complete: parent independently recovered pre-edit Read tool sources and AST-compared all moved constants across3 suites, current shared imports verified; final high-effort fixture reviewer clean, optional helper dedup/pre-existing import cleanup explicitly declined. Shared simplify review and final integrated high code-review cover test delta; parent fresh80 real-JVM tests pass with explicit vendor/lib. No production changes/runtime downloads. Reflection and verification-before-completion invoked; original snapshots not inferred from overwritten pyc.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
