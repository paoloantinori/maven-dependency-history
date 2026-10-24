---
id: TASK-2
title: Plan implementation after design approval
status: Done
assignee: []
created_date: '2026-09-10 14:29'
updated_date: '2026-09-12 15:47'
labels: []
dependencies:
  - TASK-1
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
After the user approves the reviewed specification, produce the implementation plan and create implementation tasks in Backlog.md. Do not implement before approval.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 User has approved the written design.
- [x] #2 Implementation plan and scoped Backlog.md tasks are recorded.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Design constraint from simplification review: detected cross-read changes mark the affected collection stage partial. This intentionally over-approximates invalidation; do not add a file-to-evidence-to-correlation dependency-map subsystem solely for finer precision.

Efficiency guidance from simplify review aaccadf6ec1d0b6ad (estimates unverified; no performance numbers accepted): reuse bounded captured bytes/extractions within a run when tool interfaces permit, without imposing a universal single-read rule or weakening digests/limits. Apply user-provided module/artifact filters early and use cheap metadata before expensive analysis, but do not skip packaged-copy discovery merely because a POM match exists; preserve ambiguity and explicit scan coverage. Scope history searches to relevant paths and classify events from shared results, widening with explicit coverage limits because current introducing paths may differ historically. Evaluate reuse candidates sequentially and stop once required fixtures pass; do not build unused adapter infrastructure or an unnecessary all-pairs reference graph. Compose fixture repositories where practical while preserving every scenario-specific assertion and adversarial safety test. These are implementation-plan considerations, not permission to start TASK-2 before user approval.

Reuse review guidance (a6d28dfb308da4e4b): (R1) prefer existing analyzers; if none meets mandatory initial class-reference/upstream fixtures, record a blocker and obtain a scope decision rather than silently deferring to TASK-3 or writing a new analyzer. (R2) evaluate project-to-library and upstream-library-to-library evidence separately; Apache analyzer API, jdeps and DepClean are candidates, not guaranteed substitutes. Test actual upstream evidence and output granularity. (R3) pin tested tool/plugin versions and prefer machine-readable output for fresh Maven evidence; any imported legacy text report needs explicit format/provenance coverage, not an ad hoc authoritative parser. (R4) evaluate standard/runtime or maintained archive libraries before custom ZIP-format parsing; preserve all archive safety limits. (R5) begin provider selection with the existing research: OSV/deps.dev APIs, OSV-Scanner and Dependency-Check as alternatives; GitHub dependency review is an optional change-evidence source, not proof of human intent. Maven-based tools can also make network requests, so execution consent does not replace destination/outgoing-data consent. Re-verify chosen interfaces only when needed; do not repeat the broad survey. No implementation authorized yet.

User arena go-ahead authorized continuation; synthesized plan independently approved and exact hash verified. Scoped implementation tasks TASK-18 through TASK-23 created with dependency order and all final-review advisories. DoD evidence must remain truthful: prior spec simplify/code-review is not automatically evidence for new plan/code. Implementation starts with bounded probe task; Maven/network operations require separate approval.

Final doc simplify duplicate-guidance advisory accepted: retain existing historical notes; stop propagating copies. Canonical operative implementation guidance is now the approved plan plus chunk task acceptance criteria.

Historical planning closure: original three documentation simplify angles reconciled in TASK1; missing fourth replaced explicitly by a2579bc75052d6cd2, NO ADDITIONAL SIMPLIFICATION NEEDED (not recovered old output). Shared /simplify invocation and high-effort /code-review invocation recorded in transcript; fresh independent final document-consistency review APPROVED same three frozen artifacts. Parent reverified SHA256 spec907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848/plan6063beb3b3f45e4f0773cd2d81e62f825b081ded9fe6c1e6f7970aa68bd8b822/Arena4cfe9850ac3c06980941f89ad5178a0ad7c66fd36e3cf8fe406d9afcec4abaef, candidate/handoff references. Application tests not applicable to documentation-only historical design work; implementation tests belong TASK18 onward. No frozen document rewritten, no commit/push. Reflection: design/planning scope fulfilled, not product completion.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
