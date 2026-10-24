# Arena synthesis: Maven Dependency Investigator

Date: 2026-09-10
Artifact: 2026-09-10-maven-dependency-investigator.md
Status: Synthesized; final plan review pending. No implementation claimed.

## Candidates and selection

All three plan/rationale pairs under /tmp/arena-maven-investigator/candidate-N/ were read end to end by the parent and the cross-judge. No dropouts. Parent dispatch IDs disagreed with runner-reported received assignments; verified frozen artifact paths/hashes were used as labels, with the discrepancy tracked in TASK-12.

Plan SHA256 values: C1 8864ea4a5d4c09ce8982052a2887fdb69eed0ce7744c04a4f02c6f56db6e7ff8; C2 6654cafbac52fd9d815ac35a540c375cd3e0894d76c251c5064064514b89384b; C3 e3494c99a6d2caaf2f19153e4d1fd1d510a6a1fdbb0e98e1e8c10ab339d160ad.

Rubric: spec fidelity, executable sequencing, real-tool grounding, safety/provenance, maintainability/delivery. Each criterion scored 0-4; these are subjective design assessments, not measured performance.

| Candidate | Parent scores in rubric order | Parent total | Judge scores | Judge total |
| --- | --- | --- | --- | --- |
| C1 | 3, 3, 3, 2, 4 | 15 | 4, 4, 4, 4, 4 | 20 |
| C2 | 3, 3, 2, 2, 2 | 12 | 4, 4, 3, 4, 3 | 18 |
| C3 | 2, 3, 2, 1, 3 | 11 | 4, 4, 4, 3, 4 | 19 |

Both recommend C1. Parent does not accept perfect safety/sequencing scores: all XML guards fail UTF-16; C3 invokes Maven version probing without consent; C1 CLI omits the required query; candidate claims about tool APIs require verification. The judge's ranking of C3 over C2 emphasizes small primary footprint; parent ranks C2 above C3 because its concrete evidence/test contracts require fewer safety and state-store repairs. This disagreement does not alter base selection.

Model diversity was reduced: override configuration was absent and the current variant has no Fable tier. All runners and the independent judge used the explicit Opus judgment alias. Independent contexts are not independent model families.

## Base and grafts

C1 supplies the Python/unittest collector, XDG state outside checkout, reusable Apache analyzer helper and focused verbs. C2 contributes detailed producer/subject evidence, source-attachment identity, cross-run duplicate-ID and qualification-propagation fixtures, write-confinement sentinels, and injectable advisory transport. C3 contributes durable consumption before spawn, runtime absence reporting without unsafe version probes, and stricter risky-unknown sequencing.

Consent consumption and started audit are published in the same immutable report revision before execution. The separate journal/state files proposed by candidates are not retained. Git hardening combines useful flags and environment isolation but is command-specifically spike-tested, not declared safe by copying argv.

Boot provided-scope assumptions are removed: actual contents determine presence, and the research's optional-dependency documentation conflict remains qualified. Source metadata, lookup outcomes and unknown provenance remain explicit.

Rejected: custom ASM primary, ASCII/prefix XML guards, unconditional Maven check command, repository-local default state, automatic prune/purge, unrestricted raw response retention, local-path-only exact artifact identity, diff-hunk XML as complete history context, unverified performance estimates, a general provider registry, automatic fallback that silently drops mandatory coverage. Raw capture is tracked in TASK-17, not forgotten. A separate check verb is unnecessary; inert discovery reports missing tools.

## Verification evidence and limits

Parent verified standalone signatures in exact-tag source and the published analyzer JAR using javap: ASMDependencyAnalyzer.analyzeUsages(URL, ClassesPatterns), DefaultClassAnalyzer.analyze(URL, ClassesPatterns). This corrects C1's one-argument claim. Runtime helper closure and actual upstream edges still require the blocking end-to-end spike.

A harmless UTF-16 document containing an internal DTD bypassed the candidate ASCII search and ElementTree expanded the entity. After reading official https://docs.python.org/3/library/pyexpat.html, a local callback rejection probe rejected UTF-8, UTF-16 and UTF-16BE documents with long prologs. This verifies the rejection mechanism only; the full parser resource limits and product tests remain planned.

Every candidate summary and hash was checked; claimed candidate probes were not treated as broad implementation proof. No product tests, live security query or Maven project execution was performed by the parent during this Arena synthesis. Tests in the plan remain expected outcomes, not results.

Cross-judge findings all land here or in TASK-2/TASK-14/TASK-16. Final synthesized-plan review is bounded to one independent reviewer, no recursive fan-out. User approval of commits remains separate. The remaining old simplify result is not silently marked passed.
