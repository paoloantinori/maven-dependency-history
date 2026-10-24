---
id: TASK-3
title: Trace shaded and relocated dependencies in packaged output
status: Done
assignee: []
created_date: '2026-09-10 14:40'
updated_date: '2026-10-23 06:27'
labels:
  - follow-up
  - artifact-analysis
dependencies: []
references:
  - claudedocs/research_maven-dependency-explanation_2026-09-10.md
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Preserve the full dependency-explanation goal beyond the first-version best-effort support. Investigate libraries whose classes are merged, relocated, or stripped of identifying metadata in packaged output. Distinguish observed archive contents from inferred identity and trace inclusion to build configuration and dependency history where evidence permits. No implementation approach is selected yet.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Detect and explain shaded or relocated library copies in representative fixtures, including missing or conflicting metadata.
- [x] #2 Report evidence for component identity and version; do not infer exact identity from package names alone.
- [x] #3 Connect identified copies to introducing dependencies and packaging configuration where supported; explicitly report unresolved attribution.
- [x] #4 Verify behavior on stale artifacts, duplicate copies, and partially retained libraries.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Under user continuous-backlog instruction, local design synthesis started jointly with related evidence extensions. No approach selected or production capability claimed; preserve exact bytes vs relocated inference and stale/partial attribution requirements. Design approval required before architecture implementation.

Parent reviewed next-phase design and requested correction before approval: matching all reference entry contents inside larger shaded ZIP is not artifact byte identity; retain entry-content correspondence distinction and duplicate-path multisets. No new shaded capability implemented.

Extension reviewer minor notes to incorporate: reference-side unmatched duplicate multiplicity differs from container-side surplus (extra shaded copies are observations, not automatically conflicts); empty/trivial equal bytes at different paths do not establish relocation identity.

Started approved pure shaded-entry matcher slice shaded.py/test_shaded.py, independent of archive reader integration. Multiset exact complete-entry correspondence/candidate relocation evidence only; no full-JAR identity or inferred version from path. No network/CLI mutation, parent integration/review later.

Parent verified pure matcher22 -Werror tests pass, source86d3fd532836d4ddb1f7e93a9bb8672de5b98174d00c0de4f4717eaf1b4c568c/testf0f40b5b0a5a5c055688fde9704b559704717012f0274417c992ffdf7d600d03. Ordered nested inventories/multiset correspondence, no artifact byte identity or relocation proof claimed. CLI/packaging configuration/graph attribution not implemented, so TASK3 remains open. Independent review bounds/duplicate pairing/metadata ambiguity required.

Recovered actual shaded review a7677da9 NOT APPROVED despite22 tests. F1 JSON encoding before field/count bounds allocates huge string despite tiny max_input_bytes; metadata version4097 also bypasses max_text. Add nonallocating prevalidation. F2 malformed mutable sha/size fields retained by reference can mutate caller through output; validate shapes and preserve valid None, no aliases. F3 ambiguity uses stale digest queue including already reserved exact entries, false ambiguity for containerX/Ysame refsY/OLD; count actual unused candidates. All3 assigned owner for deterministic regressions; no integration approval.

Parent verified corrected on-disk shaded artifact419ec37666cacbb320f8c078e3dac6469f164d1379a50516c2a0842df5f7d443/tests3c9a6a3e6936e65ba665afcc3b1885bff8d6c8986a7791e7c6f7e6ea7e4c610e with28 -Werror tests. Worker completion report missing requested; same reviewer directly checking original3 regressions against actual corrected files, no assumed fix semantics.

Same shaded reviewer a7677da9 final targeted APPROVED419ec376/3c9a6a3e after28tests and original allocation/metadata/alias/ambiguity probes. Preencoding rejection confirmed, malformed mutable scalar rejected with caller unchanged, used-exact candidate excluded from ambiguity. No residual/simplify suggestions. Pure matcher ready integration; packaging config/graph attribution and CLI still open.

Parent final configured suite bfr7lpdvg FAILED:1442 tests,1 error in new test_shaded_cli.test_duplicate_entry_names_accounted_by_index, KeyError index (traceback line rendering shows entry_index, possible concurrent edit; cause not assumed). Full output read, artifact path retained. Shaded CLI owner must freeze current files, inspect actual matcher locator schema and assertion, reproduce scoped test with full trace before fixing; no blind rerun/fixture weakening. TASK25 closure waits reliable stable full verification, prior archive review approval unchanged.

Parent40 shadedCLI+matcher -Werror tests pass20.686s on stableCLI15018509ef26389ffa5d0a5c1414bff04cad08f152d61786e6f4eeb6f2c79308/testsfe21dc7e120387f3e8bf330d60c8803c4e18facb6d92623da3b7bd3b3c0980ca. Owner confirms test locator correction to entry_index (actual schema) and imported nodes payload.evidence.nodes, preserved duplicate semantics; prior fullsuite KeyError remains recorded failed before stable final handoff. New referencejar collection bounded and candidate-only; shade configuration/proven relocation unresolved, not wholeTASK3 completion. Independent CLI integration review and fresh stable fullsuite next.

Recovered shaded CLI final reviewer aa9b806c APPROVED15018509/fe21dc7e after40tests and independent literal! nested chains, foreignmodule exclusion and derived qualifier enforcement probes. No findings/simplify recommendations. All scoped matching/CLI pieces approved, but taskAC packaging configuration/relocated evidence still incomplete and explicitly unresolved; implement bounded observed config linkage next, not false closure.

Recovered rawshade config slice and parent87 shadeconfig/discover/xml tests -Werror pass:sourceee80a2463a8b2de49e41fbe17946a801e2dc08490057fb1493ab159f5481ec49/tests6fd70e84fbc9d004baf18e95449f4143141bebceb1121a535cce9e5f32159a70. Raw namespace/profile/execution/management observations only, no plugin-version glob/interpolation semantics inferred. Explicit pathpair prefix candidates not binary relocation proof; independent review before CLI linkage.

Shade-config reviewer ac5768bec NOTAPPROVED2 despite87tests: node/locator maps+stack allocate allchildren before maxnodes gate (100k/max1 ~28MB), scalar joins fragments before maxtext(~4MB forlimit1024); enforce incremental admission/count-beforejoin. Duplicate/nonscalar explicit plugin group returnsNone like absence then incorrectly defaultgroup candidate; distinguish missing from invalid, invalid explicit group unresolved/excluded. Independent tiny/preset tree probes, all findings assigned owner; no further simplify notes.

Parent90 shadeconfig/discover/xml tests -Werror pass0.062s fixes source7a24e7a24f1e230140f37919c2bba50fbd35a004b4b2480337158e17e6022a35/tests3603ea587e7211e2ca590320b56968085ececa854e2712f935cf3d5bd5033dab. Auxiliary node allocation gated preadmission and scalar join prebounded; invalid explicit plugin groups excluded, absence only defaultcandidate. Same reviewer original boundedallocation/identity probe recheck requested.

Shade-config same-reviewer ac5768 final targeted APPROVED7a24e7a2/3603ea58 after90tests and independent original bounded-allocation/invalid-group probes. No residual/simplify findings. Raw config parser ready candidate-attribution integration, no evaluated Maven plugin semantics/activation or binary relocation proof claimed.

Shade-config CLI owner reports focused70 tests pass, wider135 run hits known3 historyCLI unknown-selectedscope failures trackedTASK5; report preliminary, parent has not verified final hashes yet. No whole-suite green or claim shade caused history issue. Await stable final artifact then review.

Parent126 shadedCLI/config/matcher/baseCLI/runtimeCLI tests -Werror pass85.735s stableCLI0c4533b9ce7385d311524a2e1924fedb3d400668994561afb7f8961c88e3fb3b/tests b514433e10b73ec3bcded7aeb6f5cf4ad5655f05a25afb49eb2ab41bec4ab328. Raw selectedmodulePOM capture/config linkage/derived qualifiers delivered, missingbadPOM partial preserves match. Indexed paths cap256 no display split, differing class bytes link candidate-only, no activation/build/actual relocation proof. Independent integration review next; metadata-free rewritten bytecode identification still unsupported, AC not silently complete.

Shade-config CLI reviewer a5039a9155edb3b8f final APPROVED0c4533b9/b514433e after126tests and independent root-replacement heldfd, traversal refusal, duplicated!path candidate indices, derived qualifier enforcement and capture/parser digest probes. No findings/simplify notes. Approval observed configuration candidate-link scope only; metadata-free rewritten-bytecode identification still unsupported, do not mark fullTASK3 AC complete.

Documented existing local collect --reference-jar workflow in references/shaded-correspondence.md, linked from SKILL. Actual CLI/matcher source checked for selected packaged-copy requirement, eight-reference/copy caps, indexed-chain/digest stale refusal, candidate-only configuration/graph links and missing metadata-free rewrittenbytecode identification. Scope remains partial; no full TASK3 closure. Documentation plus matcher/config/CLI tests78 passed65.625s with ResourceWarning errors (bjg8aw68i). No Maven/network or production code changes.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Maintainer decision 2026-10-23: option (b) declared as the boundary for now, option (a) as a future capability. Shaded dependency tracing ships as: entry-content correspondence between a selected packaged copy and explicit local reference jars, candidate-only packaging configuration and inclusion links, and explicit refusal to identify rewritten bytecode without metadata. That limit is documented as a declared boundary in references/shaded-correspondence.md, not as a missing feature of a pending task. The future capability (metadata-free rewritten-bytecode recognition) is tracked in the new deferred task.
<!-- SECTION:FINAL_SUMMARY:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
