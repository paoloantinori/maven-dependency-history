---
id: TASK-22.1
title: Enforce a total advisory request deadline
status: Done
assignee: []
created_date: '2026-09-12 09:27'
updated_date: '2026-09-12 16:21'
labels:
  - hardening
  - network
dependencies: []
parent_task_id: TASK-22
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Current urllib transport timeout applies per blocking socket operation, not whole request. Add an enforceable total request deadline before final executor completion; preserve durable single-use consent, response-byte cap and cleanup. Do not claim that a cooperative clock check alone interrupts blocked reads. Use offline slow/trickling transport fixtures; no live requests without approval.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A response making incremental progress cannot extend execution beyond the declared total deadline indefinitely.
- [x] #2 Timeout closes transport resources and records failure with consent already consumed; no implicit retry.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Started isolated killable advisory transport implementation in new advisory_transport.py/test_advisory_transport.py. Existing per-socket timeout is not accepted as total deadline. Worker tests local synthetic children only, no external HTTP. Parent handles executor integration after original privacy review closes.

Parent independently48 advisory_transport tests pass -Werror, sourcea9a588bd41e49a74f6509c3ac19100a0648c006b8a992c616b4ff0a9cd08d001/tests1fef49202b934f138a3ead1492e60fe2afcf1736cbec53da22abccc4cad2131a. Started bounded independent transport review and disjoint executor integration: default DeadlineTransport, bind transport identity, synthetic total-timeout consent test, safe failure serialization. No live HTTP. Worker completion message retrieval pending but artifact tests verified.

Transport reviewer a5b2a747b1a1b28bd APPROVED a9a588bd/1fef4920 after48 tests and independent drip/deadline/interrupt/fd probes, with four actionable notes retained for correction: F1 docstring wrongly promises every error AdvisoryError (HTTPError/URLError differ); F2 unconditional cleanup/reaping claim too strong because bounded wait may expire; F3 assert leader PID absent as well as process group absent to detect zombies; F4 cap individual os.read at64KiB and handle spurious writable BlockingIOError. Assign targeted owner corrections then same reviewer recheck. Do not adopt reviewer suggested wording that timed-out SIGKILL guarantees never running; report termination/reaping unconfirmed when wait expires. Reviewer observed5 executor redaction failures while separate executor owner is writing RED tests, not accepted as stable regression in transport.

Parent independently verified48 -Werror tests after transport F1-F4 delta. Source3ae6ebb22a628bb9753dee82aa380b8d0d3e01a58359e0ef02c64e46596ecd6a/tests4e777b95ab95dca3d617b7e7da8dcc4117876db85465cf4fba4926c7eaf37d91. Corrected exception-surface and bounded-cleanup claims, leader-reaping regression assertion,64KiB reads and transient write handling. Same reviewer final targeted recheck requested. No executor integration or TASK22.1 completion claim yet.

Same transport reviewer final APPROVED F1-F4 on3ae6ebb2/4e777b95 after48 tests and independent deadline/reap/fd probes. Parent then corrected only opening docstring from executor default UrllibTransport to worker inner socket transport to match completed integration. Current source1f9af47bd002a14bd5dc2b6b9fd02183d5ed4780f33d452c59eb30e118f9f6c6; tests unchanged. Parent97 combined transport/executor tests -Werror pass. Documentation-only delta covered by upcoming final integrated gates; no additional behavior change or separate review cycle. TASK22.1 awaits shared gates withTASK22.

Transport reviewer a5b2a747b1a1b28bd confirmed final exact source1f9af47bd002a14bd5dc2b6b9fd02183d5ed4780f33d452c59eb30e118f9f6c6/tests4e777b95ab95dca3d617b7e7da8dcc4117876db85465cf4fba4926c7eaf37d91 and accepted single-sentence documentation delta. Final APPROVED, review closed with no residual; shared TASK22 completion gates still pending.

Shares completed TASK22 six-module simplify gate, all review recommendations dispositioned; does not close parent correctness review.

Completion verified: shared simplify four angles resolved; final transport review APPROVED1f9af47b plus integrated executor reviews and final1095 configured tests. Parent fresh346 scoped tests pass. Deadline worker terminates/reaps within documented limits; consumed-before-spawn/failure/no-retry fixtures verified. No live HTTP in production tests. Reflect: approved bounded request-deadline requirement fulfilled, no total-system or detached-process sandbox claim.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
