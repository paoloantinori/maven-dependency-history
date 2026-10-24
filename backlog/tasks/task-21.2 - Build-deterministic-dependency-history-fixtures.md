---
id: TASK-21.2
title: Build deterministic dependency history fixtures
status: Done
assignee: []
created_date: '2026-09-11 16:35'
updated_date: '2026-09-12 07:45'
labels:
  - implementation
  - history
dependencies: []
parent_task_id: TASK-21
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Parallel with staging: implement reusable temporary Git fixture builder and its tests for direct addition, removal/reintroduction, version-only upgrade introducing transitive library, parent/BOM/property change, and missing history. Own tests/history_fixtures.py and tests/test_history_fixtures.py only. No edits to production Git inspection or staging.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Fixtures encode known commits and file states mechanically verified with bounded safe Git reads.
- [x] #2 Expected answers distinguish last modification, explicit addition and candidate transitive introduction.
- [x] #3 No application build, network access or project repository commit; synthetic commits confined to temporary fixtures.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Parent independently ran14 fixture tests GREEN and hashes recorded. Deterministic temporary Git histories implemented with direct/reintroduced/version-only/property/BOM changes and missing-history fixture; actual commits are synthetic temp-only. Child task NOT complete pending parent reviews/DoD; implementer-suggested gate exemption rejected because user requires explicit simplify/code-review. No production history analyzer yet.

History fixture compliance reviewer a8bf45b2b92f76512 APPROVED exact d6cdad26/e8650983 artifacts after independent14 tests and real-Git checks. Scenario/author/committer/determinism/isolation verified; absent .git is explicitly missing history, not shallow. Frozen container has mutable maps; no deep-immutability claim adopted. Proceed bounded fixture quality review while other parallel work continues; no child-task completion yet.

Fixture quality review found vacuous hook isolation assertion: test adds hostile hook but commit --no-verify bypasses it independently of hooksPath. Accept remove --no-verify from that synthetic commit, add/verify mutation control so redirect is load-bearing; no change to project commit policy. Reviewer14 tests passed but test must actually exercise claimed behavior before approval.

Parent reran14 history-fixture tests after hook-isolation correction: GREEN. Removed --no-verify from positive synthetic commit; mutation half redirects to hostile hook and asserts blocked commit plus marker, so test now exercises isolation behavior. Fixture builder unchanged; no project commits. Same quality reviewer targeted closure requested; worker generic code-review relay is not accepted as correct reviewer routing.

After classifier recovery parent reran14 history fixture tests GREEN; hashes d6cdad26e68267e2a31a438b559cb4d26fa9530a4de34b1183796e99554fc51d/fdda866947b24e9a445f6c41dd42fb1462e2c5a27af62763f7d2262c7a511439 match correct reviewer approval. Both fixture spec and quality reviews complete. Child task stays open for inherited integrated simplify/code-review gate evidence; no exemption inferred.

Shared TASK21 final simplify/high-effort review and658-test run cover this fixture child; targeted reviews and meaningful hook mutation test also recorded.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
