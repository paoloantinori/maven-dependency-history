---
id: TASK-21.3
title: Implement bounded local dependency history analysis
status: Done
assignee: []
created_date: '2026-09-11 20:13'
updated_date: '2026-09-12 07:45'
labels:
  - implementation
  - history
dependencies: []
parent_task_id: TASK-21
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Use reviewed history fixtures to implement local declaration-change analysis independently of staging/packaging. Own history.py and test_history.py. Reuse bounded Git execution; no network, historical builds or project commits.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Report addition, removal, reintroduction, version/property changes and last modification from actual POM snapshots.
- [x] #2 Keep transitive introduction a candidate unless historical resolved evidence exists; preserve incomplete/shallow-history limitations.
- [x] #3 Bound Git output/history scans and refuse unsafe implicit fetch/helper execution.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Independent history implementation assigned ab43ad9059f981304, owns history.py/test_history.py only; staging reviewed separately, helper packaging stable. User standing parallel-work authorization applies; no network/Maven/project commits. Reuse actual reviewed fixture history with bounded Git/XML evidence, not fabricated introduction claims.

Production history slice delivered; parent independently ran33 history+fixture tests GREEN, hashes match history27ce45ee4c14a6f93fb435507e3b386039bf09651f969f5532346cc964f813d2/test2d14f18f9886769ff278f7251168bd6fadb96ca923771744f3ff8ad1acac532a. Worker self-review/manual passes do not satisfy explicit /simplify invocation gate; no exemption adopted. Bounded compliance review follows, private helper bridges need cross-module contract verification. Other usage pipeline worker active, no stable whole-suite claim from mixed edits.

History compliance review found two verified silent-conflation gaps: caller POM path glob characters interpreted differently by log vs ls-tree, yielding overbroad commit/file-modification claims; scope/optional/classifier/type-only changes omitted while coverage complete. Fix literal path semantics consistently (prefer Git --literal-pathspecs, support actual unusual filenames) and preserve declaration fields with explicit declaration-change events. No resolver/history replay expansion. Reviewer pytest detour irrelevant; supported unittest19 GREEN did not cover gaps.

Parent independently verified history fixes:37 history+fixture tests GREEN; history535216ce856d804a55ed85246ac4cea9ffb6d2df651f2e3857a2c3acc50ff657/testa02214aa4e9b59c2899870ff74b8fb771be8b671ce9912cf4715504ae35960c3. Literal Git pathspecs prevent wildcard expansion; raw scope/type/classifier/optional preserved and declaration-change emitted. Same compliance reviewer targeted recheck requested; no task completion.

History compliance reviewer a101adbdb30e2d9c7 APPROVED final535216ce/a02214aa after independent23 tests and original wildcard/scope reprobes. Literal paths and raw field events verified; management-only field changes remain management-change with full old/new values intentionally, not hidden. Begin bounded quality review; no whole-task completion.

History quality review found3 issues despite37 scoped GREEN: multi-match bare query rows lose g:a identity and pair unrelated declarations, fabricating version change; aggregate-timeout wording excludes preceding checkout_context commands; failed initial HEAD ls-tree probe swallowed. Fix per-declaration identity/multiset event attribution, explicit context-vs-analysis budget semantics (or true shared deadline), and issue on seed failure. Major multi-match correctness blocker open; no quality approval.

Parent verified history quality fixes:41 history+fixture tests GREEN; history92b7b46262d227ba39d209bf32cfa89d9a456deae83b2c163508847ef7a16877/test4694a88177f038d7b9ed14017dc9aff85a9055615c28573cf2e06a0a43fa0289. Declaration identity/multiset diff prevents unrelated bare-query pairings, duplicates explicitly ambiguous; type/classifier identity shifts removal+addition, scope/optional values declaration-change. Timeout phases honestly separate; failed HEAD tree seed yields explicit partial issue. Same quality reviewer bounded recheck requested.

History final quality recheck found one residual in multiset fix: sorting raw (version,scope,optional) tuples mixes None and str for duplicate declarations and raises TypeError. Original three findings otherwise verified fixed. Assign explicit type-safe deterministic sort key preserving actual None values in observed evidence; add mixed-scope/optional duplicate removal regression. No approval until targeted fix verified.

Parent independently reran43 history+fixture tests GREEN after None-safe multiset sort fix. history43b079c6edcffad94952d1704d0552faf9008d1d2456255fc221c2ca9a9dec92/testca08fc13d852a0566fd03acbec0480f3f14b03c735b8f75309036b6366872782. Both removed/added tuple sorts deterministic with raw None preserved; mixed scope/optional duplicate regressions added. Same quality reviewer final residual closure requested.

History quality reviewer a1446305fed79ac27 final APPROVED exact history43b079c6edcffad94952d1704d0552faf9008d1d2456255fc221c2ca9a9dec92/testca08fc13d852a0566fd03acbec0480f3f14b03c735b8f75309036b6366872782 with independent43 history/fixture tests. All four findings closed; both slice reviews complete. Integrated parent DoD gates still pending, no hidden completion.

Integrated per-POM reintroduction fix parent-verified:44 history+fixture tests GREEN. Seen identity history now scoped by literal POM path; first other-module addition unaffected by prior app-module declarations, same-POM reintroduction retained. historyeb5a2895ddf87ce58ae7cf175eeb992435fab86f58d3697fbbd9fb89609b276f/test97b8c6981b30b6756bd9ab7619ab5c29eb1b123e371f0e9b12c0a71cd751ea58. Other integrated fixes still active; no combined final approval yet.

History original and integrated per-POM findings fixed and final shared TASK21 review approved; parent658 tests GREEN. Child bounded local history scope complete.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
