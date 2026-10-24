---
id: TASK-21.6
title: Validate runner inputs before expensive staging
status: Done
assignee: []
created_date: '2026-09-11 21:33'
updated_date: '2026-09-12 07:45'
labels:
  - integration
  - efficiency
dependencies: []
parent_task_id: TASK-21
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Review-advisory from usage pipeline: invalid runner arguments can be rejected only after safe staging performs avoidable work. Evaluate a small side-effect-free runner preflight API shared with executor; do not duplicate validation or weaken hash/permission checks.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Invalid scope/classpath/hash configuration rejected before staging where safely testable.
- [x] #2 Execution rechecks mutable evidence; preflight is not authorization or immutable-state proof.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Parent independently verified option preflight slice:60 runner+pipeline tests GREEN with explicit pinned dependencies. Shared pure shape checks run before staging, runner still rechecks files/hashes, one-shot classpath materialized once. No completion gate claimed; bounded compliance review follows. Production/refusal precedence changes must be tested as documented, no synthetic executed-run evidence.

Option preflight compliance reviewer a8c2a38398eee2f54 APPROVED after independent36 runner+24 pipeline tests and pure-call bombs/single-consumption probes. Runtime rechecks preserved, no residual. Cited values are file SHA256s not Git commits; parent previously verified exact hashes. Second-stage quality will be covered by integrated TASK21 high-effort code-review, not another standalone round.

Shared TASK21 final gates cover pure preflight and execution rechecks incl classpath separator/wildcard rejection. Final658 tests passed.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
