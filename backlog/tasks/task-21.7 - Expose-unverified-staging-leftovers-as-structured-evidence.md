---
id: TASK-21.7
title: Expose unverified staging leftovers as structured evidence
status: Done
assignee: []
created_date: '2026-09-11 21:33'
updated_date: '2026-09-12 07:45'
labels:
  - integration
  - cleanup
dependencies: []
parent_task_id: TASK-21
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Review-advisory from pipeline: failed staging can deliberately retain an unverified/replaced directory but StageError lacks structured leftover-location metadata for caller reporting. Add bounded explicit leftovers/cleanup status without unsafe deletion or treating error text as identity.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Pipeline reports retained staging location and reason without parsing human error strings.
- [x] #2 Unverified or replaced paths remain untouched; original exceptions and observations preserved.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Parent independently verified structured cleanup evidence change:82 staging+pipeline tests GREEN with explicit pinned jar dir. StageError optional cleanup_status/leftovers and immutable StageLeftover now surface retained stage paths without prose parsing; pipeline refusal cleanup reflects partial/failed removal. Original body exception behavior preserved by tests. Exact hashes recorded; bounded spec review follows, no child-task completion.

Structured leftovers compliance reviewer a7db20b9016002280 APPROVED exact staging5a7df9bc/pipelinefd1eeedc after independent63+19 unittest tests. Cleanup statuses derive from actual removal, unverified replacements survive, observations/body errors preserved, structured records bounded and JSONsafe. No residuals. Begin bounded quality check, integrated task DoD still pending.

Quality review verified82 scoped tests with two minor accepted corrections: remove unused LEFTOVER_CODES test constant; unknown/unrecognized cleanup_status must not map to benign not-reached. Preserve raw structured unknown but outer cleanup failed unless explicit not-created/no leftovers. Reviewer confused SHA256 citations with git commits; parent hashes, not git revision existence, remain identity evidence. No new staging behavior or policy expansion.

Parent independently reran83 staging+pipeline tests GREEN after conservative unknown-cleanup fallback and dead constant removal. Pipelineac1b188fd64716f13ff24959062b5555767f392d767eaa1267c406d84cb72859, staging source unchanged5a7df9bc. Unknown/raw cleanup remains visible, outer failed unless explicit complete or not-created without leftovers. Same quality reviewer targeted closure requested; no full parent-task completion.

Structured leftovers quality reviewer a0af5c457951a78a3 final APPROVED pinned pipelineac1b188f/staging5a7df9bc with independent63+20 tests. Both residuals closed, no scope expansion; slice reviews complete. Integrated parent simplify/code-review gates still pending.

Shared TASK21 final gates cover structured cleanup outcome and conservative unknown fallback. Final658 tests passed.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
