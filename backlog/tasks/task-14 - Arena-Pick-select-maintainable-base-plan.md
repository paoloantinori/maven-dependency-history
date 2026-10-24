---
id: TASK-14
title: 'Arena Pick: select maintainable base plan'
status: Done
assignee: []
created_date: '2026-09-10 16:23'
updated_date: '2026-09-12 15:47'
labels:
  - arena
dependencies: []
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Parent reads every candidate in full and scores each criterion; reconcile with cross-judge and select base.
<!-- SECTION:DESCRIPTION:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Parent rubric assessment saved at /tmp/arena-maven-investigator/parent-assessment.md; candidate-1 provisional preferred base, pending independent cross-judge. Shared XML byte-guard flaw reproduced with harmless UTF-16 internal entity; parser callback alternative tested after reading official docs. Candidate-1 DefaultClassAnalyzer signature corrected by exact-tag source and javap: analyze(URL, ClassesPatterns). All scores provisional; no synthesized plan or implementation yet.

Cross-judge independently recommends candidate-1, agreeing with parent base choice. Judge scores C1=20 C2=18 C3=19 versus parent C1=15 C2=12 C3=11. Parent rejects perfect safety/sequencing scores: reproduced shared XML bypass, C3 unapproved Maven version probe, missing query in C1 CLI, and unresolved tool assumptions require repairs. Ranking agreement is not verification. Select C1 maintainable reusable-analyzer/unittest/XDG-state shape; graft C2 fixture/schema detail and C3 consume-before-spawn ordering without duplicative journal. Same-model-family diversity limitation retained.

Historical planning closure: original three documentation simplify angles reconciled in TASK1; missing fourth replaced explicitly by a2579bc75052d6cd2, NO ADDITIONAL SIMPLIFICATION NEEDED (not recovered old output). Shared /simplify invocation and high-effort /code-review invocation recorded in transcript; fresh independent final document-consistency review APPROVED same three frozen artifacts. Parent reverified SHA256 spec907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848/plan6063beb3b3f45e4f0773cd2d81e62f825b081ded9fe6c1e6f7970aa68bd8b822/Arena4cfe9850ac3c06980941f89ad5178a0ad7c66fd36e3cf8fe406d9afcec4abaef, candidate/handoff references. Application tests not applicable to documentation-only historical design work; implementation tests belong TASK18 onward. No frozen document rewritten, no commit/push. Reflection: design/planning scope fulfilled, not product completion.
<!-- SECTION:NOTES:END -->
