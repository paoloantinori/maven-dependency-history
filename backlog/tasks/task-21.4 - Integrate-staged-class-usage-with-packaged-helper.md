---
id: TASK-21.4
title: Integrate staged class usage with packaged helper
status: Done
assignee: []
created_date: '2026-09-11 20:29'
updated_date: '2026-09-12 07:45'
labels:
  - implementation
  - integration
dependencies: []
parent_task_id: TASK-21
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Connect reviewed safe staging and hash-verified helper package to usage runner using separate orchestration module and end-to-end tests. No network or Maven bootstrap; preserve source/staged evidence mappings and cleanup on errors.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Real staged A/B/C fixture produces upstream B-to-C evidence without application-to-C reference.
- [x] #2 Original source provenance survives temporary cleanup; helper inputs and hashes are verified.
- [x] #3 Unknown/MR/duplicate-owner and failure outcomes remain explicit; no project-code execution or bootstrap.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Worker ab3462b604a0c72ee owns usage_pipeline.py/test_usage_pipeline.py only, integrating reviewed staging/runner/packaging. Real temp fixture build allowed from existing pinned jars, no network/Maven/project commits. History worker owns history.py/test_history.py concurrently; no overlap.

Usage pipeline delivered; parent independently ran16 scoped tests GREEN and recorded hashes. Worker reports invoking skills/reviews despite explicit no-recursive-skills/no-subagents dispatch, so claimed gates require actual reports and are not accepted blindly. Final ten-finding disposition not fully delivered (six fixed plus three skipped leaves one unspecified); request complete compressed list. Known skipped items recorded: shared temporary test dependency path; avoid staging before runner argument rejection; structured orphan-stage details for StageError. Parent slice review still required; no child-task Done.

Usage pipeline compliance reviewer a6fa9dae720178ebb found original-source mapping gap for noncanonical staging-base spelling plus helper issue/skipped input: refs use dual-key mapping but issue by_staged only caller spelling, so MR issue loses input_source and labels deleted stage path original. Fix shared alias lookup for refs/issues/skipped inputs and regression // base +MR. Partial status was truthful; source provenance still must be preserved. No approval yet.

Parent independently ran17 usage pipeline tests GREEN at source5c196c3bc60e31a57ef4f29b26bcade74c9c293563a79aff33e1c559d7d1c42a/test0816f97583dccf7bdf1fda4fe6edd93732fbf6ab3e39e28f6065c84867ea3f6d. Shared dual-key stage map now preserves original issue/skipped provenance for //base; raw runner unchanged. All prior worker10 findings now dispositioned: fixed1/2/3/5/8/9/10, partial4 tracked21.7, skipped6 tracked21.5 and7 tracked21.6. Worker own pre-fix review is not final gate approval; parent compliance recheck requested.

Usage pipeline compliance reviewer a6fa9dae720178ebb APPROVED final5c196c3b/0816f975 after independent17 tests and //base+MR probe. Original source mapping and raw runner immutability verified, no residual. Begin bounded quality review; child still requires integrated DoD gates. History correction worker remains independent.

Usage pipeline quality reviewer aa48ecc233526834a APPROVED exact5c196c3b/0816f975 after independent17 real-JVM tests. Source mappings, duplicate owners, cleanup/body exception handling, partial outcomes and raw-report preservation checked. Both slice reviews closed; integrated DoD still pending. Start portable test dependency configuration as separate TASK21.5, avoiding history files.

Shared TASK21 final gates cover staged usage pipeline with original-source mapping, cleanup metadata and preflight. Final658 tests passed.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
