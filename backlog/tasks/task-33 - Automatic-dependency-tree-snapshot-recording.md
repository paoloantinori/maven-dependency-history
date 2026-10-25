---
id: TASK-33
title: Automatic dependency-tree snapshot recording
status: Done
assignee: []
created_date: '2026-10-25 12:37'
updated_date: '2026-10-25 14:12'
labels: []
dependencies: []
priority: medium
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
With diagnostics auto-approved, each tree run can persist its own resolved snapshot as run evidence so future graph-history comparisons build up automatically run after run. Record the imported snapshot with producer context at collection time; graph-history then finds prior snapshots in the same run lineage or state base.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A diagnostic tree execution leaves a snapshot record usable by later graph-history without manual import.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation: _cmd_execute for a successful pinned dependency:tree goal automatically records the produced output as a same-run imported-tree snapshot (category imported-tree-json, provenance executor-recorded from the pinned argv the executor itself enforced, tree_mode nonverbose with executor-recorded attestation, stage import-tree-json complete). The snapshot commit builds on the post-finalize report; the execute document exposes a snapshot envelope (recorded/evidence_id/revision or a sanitized reason) and its revision follows the snapshot commit; failures never fail the execution. Manual import remains available. Tests: new regression test_tree_execution_records_snapshot_automatically; happy-path, replay and no-output tests adapted to the new revision flow; 159 tests green across 7 modules.

Review APPROVED with two findings fixed in-review by the reviewer (F1: import message rewrite now only when snapshot recorded, regression test_classpath_execution_reports_no_snapshot_message added; F2: generation_context note extended to state the report bytes are unsandboxed-child-produced and unverified against what the pinned plugin would have emitted). Out-of-scope residual fixed by parent: advisory next_page message aligned to the standing policy. Verification: 41 cli_operations tests green in review, 63 green after the residual fix. Gates complete: implementation, review APPROVED, full suite 58/58 (1837 tests).
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
