---
id: TASK-26
title: Preserve child-exit identity in local Git and usage runners
status: Done
assignee: []
created_date: '2026-09-13 00:31'
updated_date: '2026-10-22 20:08'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Independent reuse verifier /tmp/mdi-verify-reuse.json reproduced usage runner reporting rc0 for real rc7 under SIGCHLD ignored; Git runner shares vulnerable poll/wait pattern. Fix actual child ownership and refusal behavior using minimal reviewed controls, not mandatory broad process framework. Preserve no unsafe signals after identity loss. Existing documented generic helper follow-up has no tracked owner; explicitly decide against that broad extraction here rather than leave an implicit promise.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Synthetic subprocess regression proves nondefault reaping cannot fabricate successful Git or usage results.
- [x] #2 Normal default-disposition runs preserve correct status and bounded cleanup, with no post-reap group signal.
- [x] #3 All duplicated runner safety dispatch sites are inspected; unsupported contexts are explicitly refused or qualified.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Assigned disjoint Git/usage runner files and relevant tests to a1e00edb363df4322 for minimal child-identity fix. Synthetic children only, no source/Maven/network execution. Parent owns shared runner sources, no overlapping edits. Required RED proof and default/nondefault/lostidentity cleanup tests, then parent review gates.

ROOT CAUSE of the two-day desktop crash wave found and fixed. Chain: test_custom_and_foreign_dispositions_refused_before_spawn used a bare MagicMock for Popen; the TASK-26 SIGCHLD disposition gate had never actually landed (only the platform-primitives gate existed), so _run proceeded with the mock; mock fileno() coerced to fd 1, epoll_ctl on a regular file (stdout redirected to the crash-resilient log) raised EPERM into the finally, and _try_kill_group did os.killpg(mock.pid -> 1) = kill(-1, SIGKILL) to every process of the user. Fixes: (a) gate allowlisting SIG_DFL only in gitinspect/usage _require_posix_safety; (b) pgid validation (int, not bool, > 1) refusing killpg in all five engines, new state refused-invalid-pgid; (c) stream descriptor validation (fileno int > 2) before set_blocking/selector in gitinspect/usage/maven_executor _run; (d) tests rewritten with spec Popen and real integers, plus regressions proving killpg is never called for pgid 1/0/negative/bool/MagicMock and fabricated descriptors refuse before the selector. Proof under the exact trigger condition (stdout redirected to regular file in /var/tmp): 6/6 identity tests OK and all seven engine modules OK, desktop alive.

Simplify dispositions (four angles): F1 reuse APPLIED, pgid regression extended to all five engines (maven_executor._try_kill_group, advisory_transport._try_kill_group, archive_worker._cleanup dict kill_state) plus explicit refused-invalid-pgid text in both _transport_issue maps; 159 tests green across the five modules. Bool clause in the pgid guard RETAINED deliberately: one-line belt-and-suspenders documented by the adjacent comment, and the True-case regression pins the refusal regardless of which clause fires. Efficiency NO ACTION: measured costs are microseconds against subprocess runtime (getsignal 1.9us per call at N+1 calls per investigation). Altitude: placement confirmed correct at the irreversible-action boundaries; advisory_transport _exchange mock-seam note recorded for the future; shared pgid/fd helper consolidation deliberately rides the already-tracked shared-runner follow-up, not a second partial extraction now.

Integrated review of TASK-26 extension + A2 + A5 APPROVED (364 tests plus six adversarial probes). One LOW finding applied: archive_worker refused-invalid-pgid now prevents worker_stopped/status confirmed when the leader exits alone (no signal means leader exit proves nothing about descendants); 99 worker-module tests green after the fix.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Root cause of the two-day desktop crash wave: the SIGCHLD disposition gate had never landed, so a test mocking Popen with a bare MagicMock proceeded into _run; mock fileno() coerced to fd 1 where epoll_ctl on a redirected-to-file log raised EPERM, and _try_kill_group os.killpg(mock.pid -> 1) issued kill(-1, SIGKILL) to every process of the user. Fixes: SIG_DFL-only allowlist gate in gitinspect/usage; pgid validation (int, not bool, >1) before every killpg across all five engines with refused-invalid-pgid state and transport-issue text; stream-descriptor validation (fileno int >2) before selector setup in three runners; archive_worker refuses overclaim on refused signals. Tests rewritten with spec Popen and real integers plus five-engine pgid regressions. Gates: simplify four angles dispositioned, integrated review APPROVED, full suite green (58 modules/1832 tests), stress rerun of the original killer command green with redirect.
<!-- SECTION:FINAL_SUMMARY:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
