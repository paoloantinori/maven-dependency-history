---
id: TASK-1
title: Design local-checkout Maven dependency investigation
status: Done
assignee: []
created_date: '2026-09-10 14:27'
updated_date: '2026-09-12 15:47'
labels: []
dependencies: []
references:
  - claudedocs/research_maven-dependency-explanation_2026-09-10.md
documentation:
  - docs/superpowers/specs/2026-09-10-maven-dependency-investigator-design.md
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Brainstorm a developer-facing dependency investigator for a local Maven checkout. The user selected the local-checkout experience, not the skill-only implementation approach. Use the completed research to compare approaches and obtain design approval before implementation. Track all project work in Backlog.md.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Agree on first-version questions, scope, constraints, and success criteria.
- [x] #2 Compare approaches and obtain user approval for the design.
- [x] #3 Save and review the specification before requesting approval for implementation planning.
- [x] #4 Explain dependencies absent from project POM declarations but introduced transitively. When an existing packaged artifact is available, support identifying the dependency there and tracing its inclusion back to the relevant dependency chain; report unresolved attribution explicitly.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Settled: initial experience is investigating an unfamiliar dependency in a local checkout. Not settled: implementation interface and first-version scope. Required project DoD: invoke /simplify and /code-review at high effort; retain evidence. No implementation has started.

User approved first-version breadth: cover dependency paths, code usage and purpose, history and attribution, recorded intent, and security. Return explicit gaps where evidence is unavailable instead of limiting the first version to graph and usage alone. Initial experience remains a local checkout; implementation approach is not yet selected.

User approved execution policy: inspect existing files and build outputs first; request explicit permission before executing Maven builds. Investigations must remain useful when a project cannot build, returning available declaration/history evidence and explicit gaps. Do not treat permission to inspect a checkout as permission to execute project code or download dependencies.

User approved first-version implementation direction: a local CLI evidence collector plus a thin agent skill. The existing coding agent interprets evidence and writes cited explanations; MCP is deferred. User asks whether the skill can bundle scripts, which may simplify distribution without changing the collector/interpretation boundary.

User approved distribution: ship one skill directory containing SKILL.md, bundled collector scripts, and reference documents. Collector remains independently runnable and testable; no separate global CLI installation or separate LLM integration is required initially. Bundling does not bypass permission gates for Maven execution or dependency downloads.

User clarified that first-version investigation must cover dependencies discovered in packaged output even when they are absent from explicit project declarations. Local checkout remains the entry point, not a POM-only evidence boundary. Candidate discovery must consider available resolved graphs and existing packaged artifacts, not just project POMs. Do not infer absence from a missing declaration or equate resolved presence with packaged presence. Creating fresh artifacts still requires build permission.

User approved initial artifact coverage: ordinary JAR/WAR files and Spring Boot nested JARs; shaded or relocated copies get best-effort identification with explicit uncertainty. This is staged delivery, not removal of the full functionality. Follow-up artifact capabilities are tracked separately in Backlog.md. Design section 1 is accepted with the clarified transitive packaged-dependency coverage.

User approved design section 2: separate discovery, inclusion/usage, history, security enrichment, and structured evidence output. Search POMs, available resolved graphs, and existing packaged artifacts. Inspect locally first; require permission for Maven execution; disclose data leaving the machine before external lookups. No default source-code or credential uploads. Collectors fail independently and distinguish unavailable analysis from negative findings.

User approved design section 3: fixture-based tests for direct/transitive paths, Maven model context, source/upstream usage, packaging, stale artifacts, history, failures, and denied permissions. Assert evidence and attribution rather than exact agent wording. Preserve uncertainty; bound archive inspection; never execute archive contents. Completion requires /simplify, /code-review high effort, and recorded verification; deferred behavior must have linked backlog tasks.

Written design draft saved. /simplify invoked with four documentation-focused reviewers; /code-review invoked at high effort on the spec; independent persistent invariant review launched. All reviews remain pending. Follow-ups TASK-3 through TASK-9 preserve deferred capabilities. No implementation or commit performed; written-spec user approval remains required.

Simplify review a17f1a970b826cc82 received. Applied advisory-provider simplification: one lookup function using the evidence format, no registry/SPI. Folded relevant context into the observation payload. Other findings are under explicit review, not dropped: freshness proposal may weaken consistency evidence; reintroduction can be tested rather than deferred; raw scratch output may still expose credentials. Requested reviewer reconsideration before changes to these guarantees.

Applied remaining review dispositions in the draft: digest derives from captured observation bytes; detected cross-read changes mark the affected stage partial without claiming an atomic checkout snapshot. Added deterministic mutation and add/remove/re-add history fixture requirements rather than deferring tested reintroduction. Default raw-output retention removed; optional sensitive diagnostic capture requires separate consent and owner-only local storage. Rejected the claim that scratch placement alone ensures secret safety. Reviewer re-approval requested; gates remain unchecked.

Reviewer a17f1a970b826cc82 accepted all four main simplification dispositions after rereading the updated spec. Both residual wording fixes applied: acceptance fixtures now explicitly include removal/reintroduction; security results use the same evidence-record/versioned-report terminology. Final-state recheck requested. Conservative stage-level freshness marking is intentional; do not introduce a per-correlation dependency map for extra precision without a separately justified design change.

Independent invariant reviewer session 2e2d12dc-e254-443c-8e82-962024926ae8 returned APPROVED on its inspected draft with two advisories. Applied both: explicit permission-scope-match fixture and hostile Git textconv/external-diff fixtures. Its report quotes older freshness/raw-output wording, so approval is not treated as covering the current revision. Resume the same reviewer for final-state verification of all intervening edits. Review result: /tmp/maven-investigator-spec-review-result.json.

Independent invariant review round 2: APPROVED for exact spec SHA256 23d68a74ab68572b6e498b007b0bb4f64b93601221fc4e2b0e0db4e5acc278dd. Parent mechanically verified current file matches the approved hash. No blocking issues or new advisories. Persistent reviewer session 2e2d12dc-e254-443c-8e82-962024926ae8; result /tmp/maven-investigator-spec-review-round2.json. This satisfies the independent specification-review step only; remaining simplify/code-review results and explicit user review of written spec are still pending. TASK-1 remains In Progress; TASK-2 remains blocked.

Code-review finder a1ddad6c470f8fc67 returned six UNVERIFIED candidates, routed to the code-review lead for adversarial verification against current approved hash: (1) disambiguation criterion/tests; (2) alleged Git read side effects requiring primary verification; (3) analyzer execution mode versus explicitly permitted partial coverage; (4) packaged-copy/current-graph correlation evidence contract; (5) explicit archive-metadata/source-text untrusted-input coverage; (6) obsolete where-feasible freshness text. Candidate 6 references text already removed. Candidate 4 incorrectly says stale/mismatch fixtures are absent; any residual contract gap still needs verification. No candidate is accepted as a defect yet, no functionality deferred, and no edits made from this finder report.

Finder a13cc2aed8270b664 delivered five additional UNVERIFIED candidates: global-install wording consistency; repeated consent text; archive/per-collector limit authority; partial versus incomplete outcome terminology; disambiguation owner/selection recording. Sent to code-review lead for verification against current spec. Disambiguation overlaps earlier finder candidate and must be deduplicated. Repetition, complementary limits, and prose synonyms alone are not established defects; require a concrete current contradiction. All candidates tracked; no spec edits or accepted findings from this report.

Code-review lead reported provisional verification areas while XML verification remains pending: graph/package correlation proof; existing-report producer provenance; plugin-realm separation; disambiguation/resume state; advisory-range fidelity; changed-observation qualification. These remain UNVERIFIED pending consolidated synthesis, not accepted defects or authorized scope changes. Wording-only PLAUSIBLE recommendations must remain distinguished from correctness findings. Specification remains frozen at SHA256 23d68a74ab68572b6e498b007b0bb4f64b93601221fc4e2b0e0db4e5acc278dd.

Open review candidate: explicit safe XML parsing for POMs, including external entities and local-file disclosure. Code-review reports its XML verifier has not returned a result. Status is UNVERIFIED/BLOCKED, not refuted or accepted. Existing no-network wording does not by itself settle the local-file question. Preserve this candidate in the consolidated review with the missing verification stated; do not claim the overall code-review gate passed while unresolved.

Code-review reports all candidate verifications returned; consolidated final findings have not yet been received. Below-cap consent-restatement recommendation filed as TASK-10: PLAUSIBLE maintenance concern, explicitly no current bypass and not a planning blocker. XML verdict is still unknown to this session until final report arrives; do not infer its outcome or mark the review gate complete.

Consolidated high-effort code-review received with ten reported gaps; three scenarios explicitly labeled plausible (report continuity, approval lifetime, no-overwrite/Maven wording). Addressed all as bounded contract clarifications, not proven implementation bugs: reject XML DTD/entities/XInclude and implicit parser I/O; define byte-identity versus current-build correlation; require imported report producer/context provenance; distinguish dependency realms; retain provider advisory applicability/ranges; propagate changed-input qualification; single-run report continuity with no cross-run merge; explicit disambiguation/selection; single-use scoped consent plus non-secret operation audit; scope no-overwrite to collector-owned writes and disclose unsandboxed Maven side effects. Added corresponding fixtures. Changes invalidate prior exact-hash approval; targeted re-review of final state is required. TASK-10 remains the separately tracked nonblocking permission-wording advisory.

High-effort code-review final targeted recheck: APPROVED for SHA256 907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848. Parent verified current bytes match. Reviewer personally traced all ten fixes and corresponding fixtures; no residual issues within the bounded review. This is design review only, not implementation test evidence. Independent invariant round 3 and remaining simplify reconciliation still pending; explicit user approval of written spec remains required.

Independent invariant review round 3 APPROVED exact SHA256 907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848; parent mechanically verified current bytes match. No residual blocking issues or new advisories. Result /tmp/maven-investigator-spec-review-round3.json; persistent session 2e2d12dc-e254-443c-8e82-962024926ae8. Both code-review and invariant review now approve this state. Simplify completion still needs remaining reviewer results reconciled; written-spec user approval still outstanding.

Simplify reviewer aaccadf6ec1d0b6ad delivered six efficiency recommendations. Parent checked actual spec at approved SHA256 907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848. Dispositions: (1) rejected stale premise: line 108 already derives digest from captured observation bytes, not pre/post hash passes; claimed read-volume savings are unmeasured and not accepted. (2) within-run reuse is useful plan guidance, but absolute one-read-per-archive mandate rejected because adapters may require different access patterns. (3) query-driven discovery is useful plan guidance; fallback only when POM matching fails is rejected because an existing POM match must not hide another packaged copy or ambiguity. (4) path-scoped history is useful plan guidance; current-path-only historical inference and claimed guaranteed time savings are rejected. (5) spec does not mandate parallel dual-adapter evaluation or a closure-wide reference matrix; choose the first fixture-satisfying candidate and focus on query-relevant evidence in planning. (6) composable fixtures are useful planning guidance, not a missing correctness requirement; preserve independent assertions for every required scenario. All actionable guidance recorded on TASK-2. No spec changes necessary from this report. Reviewer-proposed gate exemption is not adopted; project DoD remains applicable. Awaiting reviewer reconciliation and other simplify results.

Simplify reviewer a6d28dfb308da4e4b delivered five reuse recommendations for current spec. Dispositions: R1 not adopted as a spec change; evaluated-tool failure must not silently defer the required initial class-reference/upstream fixture. TASK-3 concerns shaded identification, not generic analyzer fallback. Record any genuine capability blocker and seek a scope decision before replacing required evidence with a gap. R2 accepted as adapter capability-matrix guidance in TASK-2, without assuming any candidate alone satisfies upstream references. R3 accepted as planning guidance to pin tested producers and prefer structured output for fresh reports. R4 accepted as standard-library/maintained-library evaluation guidance, not a researched library choice. R5 accepted as research-shortlist guidance; Maven execution and network permission requirements are cumulative, not alternatives. Reviewer cost estimates are unverified and not used. All actionable guidance recorded in TASK-2. Reviewer-local empty diff and claimed gate exemption do not close the parent project DoD. No spec changes made.

Simplify reviewer a6d28dfb308da4e4b completed reconciliation and confirmed every recorded disposition faithfully represents its recommendations. It explicitly accepted rejecting automatic analyzer-failure deferral and correcting cumulative execution/network consent. Its bounded review is closed; no further findings or spec edits. This closes that reviewer only, not outstanding simplify results or user approval.

Simplify reviewer aaccadf6ec1d0b6ad completed reconciliation against current SHA256 907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848. Withdrew stale digest/four-read finding and all numerical savings estimates; accepted recorded dispositions for F2-F6 as TASK-2 guidance without spec edits. Its assigned review is closed. Its later reviewer-local empty-diff statement does not define parent scope: the explicitly targeted untracked specification was reviewed, and no parent gate exemption is inferred. Remaining original simplify reviewer results still need reconciliation before marking DoD item 1 complete.

User invoked pstack:arena to pick up and go ahead with the project. This authorizes proceeding from the reviewed written specification into planning. Previous code-review and invariant approvals remain valid for unchanged spec hash. Outstanding original simplify thread closure remains an explicitly open process item, not an undisclosed passed gate.

Original simplify reviewer a17f1a970b826cc82 explicitly closed its assignment: all original recommendations and residual fixes resolved, none open. It mechanically checked the current spec hash and survival of both residual fixes; it distinguishes its original approval from later changes covered by code-review/invariant approvals. One remaining original simplify reviewer result (aba402635d32da7d1) is outstanding. Arena planning candidates continue independently; no additional review launched.

Historical planning closure: original three documentation simplify angles reconciled in TASK1; missing fourth replaced explicitly by a2579bc75052d6cd2, NO ADDITIONAL SIMPLIFICATION NEEDED (not recovered old output). Shared /simplify invocation and high-effort /code-review invocation recorded in transcript; fresh independent final document-consistency review APPROVED same three frozen artifacts. Parent reverified SHA256 spec907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848/plan6063beb3b3f45e4f0773cd2d81e62f825b081ded9fe6c1e6f7970aa68bd8b822/Arena4cfe9850ac3c06980941f89ad5178a0ad7c66fd36e3cf8fe406d9afcec4abaef, candidate/handoff references. Application tests not applicable to documentation-only historical design work; implementation tests belong TASK18 onward. No frozen document rewritten, no commit/push. Reflection: design/planning scope fulfilled, not product completion.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
