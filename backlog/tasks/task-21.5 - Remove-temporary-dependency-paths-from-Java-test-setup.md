---
id: TASK-21.5
title: Remove temporary dependency paths from Java test setup
status: Done
assignee: []
created_date: '2026-09-11 21:33'
updated_date: '2026-09-12 07:45'
labels:
  - test-infrastructure
  - portability
dependencies: []
parent_task_id: TASK-21
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Multiple Java helper/runner/pipeline/packaging tests depend on session-specific /tmp pinned jars. Consolidate explicit offline dependency configuration and reusable fixture setup without automatic network/bootstrap. Required before portable test/release claims; preserve checksum verification.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Fresh checkout tests locate explicitly supplied pinned dependencies or fail with actionable prerequisite, never rely on vanished session paths.
- [x] #2 Shared setup preserves offline behavior and genuine helper integration tests.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Portability worker ae20d17a5cf308a1e owns shared Java test support, its tests, four Java integration test modules and one prerequisites doc only. Explicit MDI_TEST_DEPENDENCY_DIR and manifest pins replace hardcoded session /tmp paths; no import-time failure/no silent skip/no automatic downloads. Production modules frozen during test infrastructure change. History quality review continues independently.

Parent independently ran100 Java support+four integration tests GREEN with explicit MDI_TEST_DEPENDENCY_DIR. Shared manifest-driven pins replace session-specific paths; import-time env/filesystem dependency removed, mandatory Java prerequisites fail clearly not skip. Production files unchanged. Bounded compliance review requested before accepting test portability; inherited completion gates remain open.

Portability compliance review verified22 shared-support and78 integration tests, found2 minor issues accepted now: doc overstates no duplicated pins (independent expected-pin regression fixture deliberately duplicates them); missing manifest leaks raw FileNotFoundError instead of actionable DependencyError. Fix doc scope and normalize manifest read errors with regression; no production change. Reviewer broad grep/no-branches statement not adopted as evidence outside owned scope.

Parent independently verified portability fixes:103 shared-support/four-Java integration tests GREEN with explicit dependency env. Missing/unreadable/nonUTF8 manifest failures now actionable DependencyError with cause; doc accurately scopes runtime pin reuse while retaining independent assertions. Shared supporta7b99699/tests f35fa84a/doc03ec8d2f. Same compliance reviewer targeted final check requested; no task/gate completion yet.

Portability compliance reviewer a313f0449f017b576 final APPROVED exact shared-support/test/doc hashes after independent25 support +78 integration tests. Missing manifest error and documentation scope fixed; no residuals. Both acceptance criteria evidenced; bounded quality review next, explicit integrated simplify/code-review gates remain open.

Shared Java test-support quality reviewer a3acef68408e5c37b APPROVED bounded scope with25 support tests under unset dependency env. Four integration tests not rerun by this reviewer; parent103 and earlier compliance103 runs remain their execution evidence. Both slice reviews closed, no concrete findings. Mtime statements do not prove unchanged files; prior explicit hashes/file ownership remain evidence. Integrated simplify/code-review gates still pending, no child Done claim.

Shared TASK21 final gates cover Java test portability; final suite uses explicit offline dependency directory, no session-path fallback.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
