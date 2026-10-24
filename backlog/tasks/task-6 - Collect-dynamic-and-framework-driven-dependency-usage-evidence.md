---
id: TASK-6
title: Collect dynamic and framework-driven dependency usage evidence
status: Done
assignee: []
created_date: '2026-09-10 14:49'
updated_date: '2026-09-12 20:37'
labels:
  - follow-up
  - usage
dependencies: []
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Extend static class-reference evidence with framework-specific and runtime observations for reflection, service loading, generated code and native integration. Execution must be explicitly authorized; observed workloads do not establish global non-use.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Preserve workload and environment provenance for dynamic observations.
- [x] #2 Distinguish unobserved usage from proven absence and never claim safe removal solely from missing observations.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Local next-phase design synthesis underway for imported runtime/framework observations with workload provenance. No application/workload execution authorized or performed; static references remain distinct.

Started approved pure runtime_evidence.py/test_runtime_evidence import slice, no actual workload execution/instrumentation. Workload/environment/observer provenance and empty-observation limitations explicit; CLI integration later. No network/Maven or inferred nonuse.

Parent25 runtime importer tests -Werror pass sourcec305258536a2ddc5b3863b26c879084cd7b65352d46b432685e45c72fa4884ef/tests42c8e38af5f6e65e3baa95747ffed5ad195c1a9cdadd56e7578b97bdae45752e. Pure caller-attested workload/window/scope observations and explicit not-observed-in-workload only, empty not safe/nonuse. Lexical preparse limits/strict schema review needed before integration; no real instrumentation/runtime execution/CLI/provider format support yet.

Recovered runtime review aad15005 NOT APPROVED3 tiny repros: arbitrary fractional precision truncated6digits permits event outside submicro window/reversed bounds; string preguard6bytes perchar rejects valid escaped supplementary Unicode needing12bytes; offset minute60/99 normalized by datetime accepts invalid timestamps. Restrict/compare exact precision, correct escaped surrogate bound with decoded validation, explicit offset component ranges. All assigned owner,25tests not sufficient.

Parent29 runtime tests -Werror pass0.030s after precision/offset/Unicode fixes source65900def55ba73e87bf68439e7c78fb33a665f3de9ff8885fe420b9e6153f622/tests6a3b877ffb69cd385055dcf3b62bfc32a3ff682f1facd4055fe52a2d09d34a12. Explicit unsupported >6 fractional digits (no silent truncation), offset range validation, correct escaped supplementary character budget while decoded cap preserved. Same reviewer targeted original repro recheck pending; no CLI/runtime instrumentation claim.

Runtime importer same-reviewer aad15005 final targeted APPROVED65900def/6a3b877f after29 tests and original submicro/window/offset/escapedUnicode independent probes. No residual or simplify recommendations. Pure importer ready CLI integration; actual trace generation/provider integrations not delivered or inferred.

Parent119 runtimeCLI/importer/legacyCLI tests -Werror pass61.259s CLI d311b063c850340f45cb7ca23e3bbfa84abffc0ce19528ec08e706d43f06f03b/test a14aa4cf57674bfb9b5b0d9e5a5be39904a072d6a852cb881b5ac5c0ef2c6bc5. Runtime import kind persisted caller-attested evidence/qualification, bounded selection matching candidate-only/no subject binding on ambiguity; no actual workload execution. Independent CLI provenance/selection review and docs/shared gates remain.

Runtime CLI reviewer a35c325b4d6a07b76 APPROVED runtime-only wiring, no findings/simplify suggestions.119tests and independent scope/digest/qualification/ambiguousduplicate/comparisonbudget probes pass. CLI changed concurrently by history owner fromd311b063 to02091699; reviewer reread runtime functions unchanged, approval bound to AST hashes _runtime_associations ed1c3c460b7715e72d4fba5f5e292b6c7ffd056720912cc62cff5dfc1ec5d254/_import_runtime09eba017fe0d0103c138bf05ef0c4204b7d81475b4e20df94fad177595f61b64, not blanket history approval. Runtime docs/shared gates remain before TASK6 closure.

Runtime reference example independently parsed by parent:1 observation, partial solely unknown environment.fingerprint, expected caller/workload/nonuse limits retained. Original doc hashad3666e1 verified; added --realm to forbidden runtime overrides to match pending/current explicit Maven realm CLI contract, embedded observation realms remain authority. No workload/CLI network execution. Final docs/shared gates remain.

Shares TASK5/6 final four-angle simplify gate; runtime user reference sample independently verified earlier, SKILL now links actual importworkflow.27 linked workflow tests pass; final gate evidence shared not duplicated.

TASK5/6 shared altitude cleanup review complete no recommendations; linked evidence/CLI authority boundaries retained. Remaining shared angles/gates open.

Shared efficiency runtime candidate: precompute selectedsubject coordinates/module/realm once rather than perobservation; preserve100k pairing cap and unknown/ambiguity logic. Tracked with history efficiency items, no edit yet pending all angles.

Shares completed TASK5/6 simplify four-angle gate; no runtime code edits necessary, optional precomputed tuple change explicitly declined to preserve unknown association semantics without measured bottleneck.100 mixed regression tests pass after history-only simplifications. Final code-review/test gates pending.

Shares full configured1582tests OK and integrated TASK5/6 final review with no runtime findings; runtime precision/offset/Unicode earlier findings fixed+approved, CLI provenance/association approved. Documentation example verified, no instrumentation/provider-native parser shipped. Completion only imported-recorded-observation scope per approved design.

TASK6 completed within approved import-only runtime observations design: workload/environment/observer provenance retained and caller-attested, missing/empty observations never absence/removal proof, scope/bounds/precision safe, CLI import and user reference verified. No live instrumentation/native profiler formats or application execution. Shared simplify/highreview all findings resolved, full1582 and fresh scoped tests pass; reflect/verification gates invoked. Later execution-provider work requires separate authorization/design.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
