---
id: TASK-9
title: Evaluate continuous history and MCP integration
status: Done
assignee: []
created_date: '2026-09-10 14:49'
updated_date: '2026-10-22 20:08'
labels:
  - follow-up
  - integration
dependencies: []
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Evaluate optional CI evidence capture and MCP access once the bundled-skill investigation is validated. These are deferred options, not requirements for the first release; split into separate designs before implementation.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Demonstrate user value beyond the bundled CLI/skill before selecting either extension.
- [x] #2 Keep existing collector evidence and permission contracts compatible.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Local evaluation design synthesis underway. CI/MCP remain optional and require demonstrated value beyond verified CLI before selection; not new implicit implementation scope.

Started bounded CI/MCP value evaluation against actual current CLI/report/history capabilities. No new server/config/CI publishing/providerSDK assumptions; recommend implement only demonstrated value, explicit retain/defer valid. Parent review/gates before closing assessment, no external actions.

Evaluation aeaca776 recommends select neither CI nor MCP now: potential CI value preserving build-time evidence is distinct but no named CI workflow/capture gap/retention consumer approved; MCP adds transport not evidence and no client lacking CLI access identified. Record demand NOT_DEMONSTRATED, not impossiblevalue. Future CI immutable perbuild evidence+scoped publication, no endless/sharedRunStore; any MCP thin adapter same collector/consent/state contracts no authority bypass. No implementation task/scaffolding added; parent assessment/gates pending.

Parent adopts no-selection evaluation: AC1 conditional gate satisfied by selecting neither extension without demonstrated named need, not by claiming value was demonstrated. AC2 preserved because no implementation/config changed; future thin adapters/perbuild immutable captures must retain consent/evidence contracts. Final duplicate notification adds no new findings. Formal shared review/verification checklist remains open, no scaffolding created.

Shared /simplify and /code-review high invoked on no-selection evaluation alongside TASK17/TASK8. Full configured suite bybam3xr3 passed1807 tests with unraisable count0; no CI/MCP source/config created. Review results pending, evaluation not yet closed.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
CI/MCP evaluation closed as select-neither. No named user need beyond the bundled CLI/skill was demonstrated: CI build-time capture has conditional value only with a named workflow and retention consumer; MCP would duplicate transport without a client lacking CLI access. No scaffolding, server or publication created; existing evidence and consent contracts unchanged. Future guidance recorded: CI captures must be immutable per-build bundles (no shared endless RunStore), any MCP adapter must be thin over the same IDs/selection/single-use consent. Gates: shared simplify disposition (none selected, no code), integrated review coverage of the no-selection rationale, full suite green.
<!-- SECTION:FINAL_SUMMARY:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
