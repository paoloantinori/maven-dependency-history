---
id: TASK-5
title: Reconstruct historical resolved and shipped dependency graphs
status: Done
assignee: []
created_date: '2026-09-10 14:49'
updated_date: '2026-09-12 20:37'
labels:
  - follow-up
  - history
dependencies: []
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Extend candidate introduction analysis to historical resolved/build evidence. Support additions, upgrades, removals and reintroductions while preserving historical toolchain, repository and profile context. Prefer captured evidence; any historical build requires explicit permission and isolation from the working checkout.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Differentiate exact historical observations from reconstruction and unresolved candidates.
- [x] #2 Explain transitive introduction without requiring the target name in project POMs.
- [x] #3 Handle unavailable historical artifacts and changing repository data explicitly.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Local next-phase design synthesis underway for historical captured graph evidence, preserving no cross-run identifier merge and explicit reconstruction vs observation. No historical builds executed; scoped approval remains required.

Parent requested next-phase design correction: commit hashes do not establish ordering; compare via verified Git ancestry or explicitly timestamp-only sequence with limitations. Local import permission does not waive design approval. Baseline final-review fixes remain prerequisite.

Extension reviewer clarification: revision absent from local history must emit explicit issue and ancestry pair remains unordered; no hash or import-order chronology inference.

Started approved pure graph_history.py/test_graph_history comparator slice, independent local recorded snapshots only. Explicit ancestry proof edges or qualified temporal ordering, no hash/import-order chronology, no historical builds/network. Attested nonverbose graph observations and run-local evidence refs required; CLI/actual ancestry collection integration later, no shipped-history claim.

Parent69 graph comparator+importer tests -Werror pass0.066s; source768cd8921b33e23a161cf862345d193dab0180252332012b533f7f08fd993b53/tests3c5d965f60927ef24947ab40da085c3670365757dbd39fe320c2d2c21409d795. Pure comparator delivered, caller-provided ancestry proof IDs/one-run assumption not authenticity verification. Actual local Git ancestry collection/declaration history linking and CLI integration remain missing. Independent topology/multiset/context/order review required, no historical build or exact intro/shipped claim.

Graph-history reviewer a67a52306656462cd NOT APPROVED3 reproduced findings despite69 tests: semantic optionalfalse vs rawstringfalse manufactures add/remove because full optional object in identity; extra coordinate None/bool causes raw TypeError and dict extra accepted; empty snapshot list claims complete unlike single snapshot partial. Fix semantic validated optional identity with raw provenance retained/unknown qualified, exact coordinate schema, explicit insufficient evidence for0 snapshots. All assigned owner, no extra simplify notes or integration approval.

Parent75 comparator/importer tests -Werror pass after3 fixes sourceaeaa4c126ce902d05e4763f6952e8d66799e2f79734a4b5f584886eed0191669/testb7a4c09b0144cdfaa46c04f27a83e53385aeba21e6e3ca58167eb10aaac9057f. Semantic optional matching/raw provenance, exact coordinates and empty insufficient snapshot status delivered. Same reviewer targeted original repros requested; actual Git ancestry and CLI integration remain open.

Graph comparator same-reviewer a67a523 final targeted APPROVEDaeaa4c12/b7a4c09b after75 tests and all original independent repros; unknown-optional interval correctly leaves gap and prevents false reintroduction across it. No residual/simplify notes. Pure slice approved only, actual ancestry evidence and CLI/declaration history integration still required.

Parent56 ancestry+graph tests -Werror pass0.640s sourcee7366d76029a94cc7791cc1767b1c6ee5ce05eacf23d94d2a361fc7145e1e7ac/tests0197a068301e53ed8f76398a41b81eef04172ffe10355969b9f7fab292e8331b. Local exact full commit objects/ancestry DAG proofs, shallow/graft unknown, replacements disabled, aggregate budgets delivered. Local object existence is not necessarily ref-reachable membership; qualification intentional. No fetch/Maven/build, CLI integration/review still pending.

Ancestry reviewer a0f86c7d621be697c NOT APPROVED P1: SIGCHLD ignored -> Popen lost exit status substitutes0, unrelated sibling commits become falsely verified ancestry edge and comparator emits change. Reject unsupported child-reaping policy before any Git command; lost/unknown exit status must never certify rc0; custom/ignored regression required, external concurrent reaping unsupported explicit. Parent56 baseline green did not cover this. Same reviewer independently isolated repro with real DAG and code source, no fetch/network.

Parent60 ancestry+graph tests -Werror pass0.972s after reaping P1 fix sourcede2f93f52428e1bca215b051bbf5dde52fc532b98f7f2a4ef63c754cc168cb20/testsed30f14480f9a8d0e22f1b82b04db9e4acdeab353d34e18a4ff0bc12ca38b63e. Nondefault reaping refused before Git and rechecked around calls, exact int exitstatus required, abort clears proofs. Unsupported concurrent external reaping explicit, no shared runner change. Same reviewer original repro check pending.

Ancestry same-reviewer a0f86c7 final targeted APPROVEDde2f93f5/ed30f144 within documented sole-reaping contract.60tests and independent default siblings/nondefault policy zero-Git/midcommand invalidation probes pass; no residual or simplification notes. Actual ancestry+pure graph modules ready for CLI integration; no historical build/shipped-proof claim.

Parent207 history_pipeline/graph/ancestry/history/report tests -Werror pass4.724s. Pipeline9ae7d9843b693e1a6466bea7172e17b8e3bac990de129f6530c0bfad57bc8dd0/tests2d92baa7d1a777502767586832b3e2494e97efaaf5a4d0208c1da7f4df51bcad delivers same-run draft evidence/ancestry/comparison/optional declaration leads, no persistence. Independent selection/qualification/reference review started; CLI wiring and final gates remain.

History pipeline reviewer aca1685167472558c NOT APPROVED: source changed-input qualifier copied only into payload, not new graph/declaration evidence qualification records. validate_report enforces limitation on original citation but accepts derived-only citation without source warning; stagepartial insufficient. Append fresh run-local qualifiers for derived dependent evidence, propagate concrete reasons to events/leads, preserve old qualifiers/IDs and recollection.207 tests pass but new realGit+validator probe reproduces. Assigned owner; no extra simplify notes.

Parent210 history pipeline/graph/ancestry/history/report tests -Werror pass5.716s sourcee823383888eb5a1a3f29b862428d8257e4858bb47b4fff59334d04e7bbe4f368/tests76ea152221167ad453cb5a02dd23f7c70483e2c3792c244fc82943ac558fd065. Derived graph/declaration qualifiers now explicit new records concrete reasons, event/lead limitations propagated, recollect prefixes/IDs preserved. Same reviewer targeted original enforcement probe pending. Worker initial empty-limitations RED was invalid unrelated schema rejection, corrected genericlimit probe genuinely reproduced before fix; no mistaken proof accepted.

Same history-pipeline reviewer aca1685 final targeted APPROVEDe8233838/76ea1522 after210tests and independent derived-only qualifier omission rejection/exact-text acceptance, event/lead inheritance and recollect ID/prefix validation. No residual/simplify notes. All history lower components ready CLI integration, task not complete yet.

History CLI integration found required provenance seam: existing tree imports default realmunknown so comparator correctly refuses graph comparison. Add explicit import --realm project|build-plugin|unknown attestation defaultunknown, record choice, no selection/POM inference; runtime import rejects override (embedded realm authority). Keep unknown=>partial and realm separation tests, do not weaken comparator. Scoped CLI prerequisite approved within recorded graph-history design.

Parent65 historyCLI/legacyCLI/runtimeCLI tests -Werror pass56.452s currentCLI a9b2eeee0557bbf6a5a9c49196d118f6a7fd5a638162ce52eaa17745aa63cc04/testhistory5e4d8a40a4d8f31789b8d64cf063b9062d0917c7e98701c6c948725098758ce1. Explicit --graph-history optin and --realm import attestation added, defaultunknown no false comparison, runtime overrides refused. Independent final CLI review required.

Historical CLI reviewer a7130d49689f807c6 final APPROVEDa9b2eeee/5e4d8a40 no findings/simplify recommendations.65 tests and independent IDcollision/foreignmodule/sourcequalifier/callerimmutability/diagnosticgap probes verified. Explicit graphhistory/stale preflight/selectedPOM/onecommit and caller realm semantics intact. Docs and required shared completion gates still pending; full1419 suite parent green.

Graph-history reference delivered eba958759f7fe3c5e49aaa07dfd844c5ced8db131eae1694506adc2b616765d7 with synthetic importer-validated example, no historical build. Parent final docs sample/links/gates pending.

Shared TASK5/6 /simplify final four-angle gate started on history/ancestry/runtime modules and CLI slices, read-only no broad refactors. Graph/runtime references linked from SKILL and27 skill/historyCLI/runtimeCLI tests -Werror pass. Final highreview/fullsuite after any cleanup; no task closure yet.

Shared TASK5/6 simplify altitude ade4163d complete: no additional refactor; pure comparator, local ancestry, orchestration/qualifiers, runtime parser and CLI publication responsibilities appropriately separated. Existing private bridges intentional, no broad framework. Other3 angles pending, not correctness/task completion.

Shared simplify efficiency acf383c89449d2fa8 returned3 candidates: history_pipeline extra whole-report deepcopy only for successor revision validation can shallow-copy (validator readonly); hoist event evidenceIDset outside qualifier comprehension; runtimeCLI precompute selected identity tuples/observed tuple preserving unknown logic and100k cap. No measured speedup claim. Retain caller-detaching copies/reaping/provenance checks. Await other2angles then dedup/apply bounded changes.

Shared simplify simplification aa4763e complete3 suggestions: graph_history internal snapshot imported field unread (ASTzero reads) remove; ancestry two ASTidentical proof invalidation blocks local helper preserving exception-specific reasons; history_pipeline readonly validation successor shallow-copy duplicates efficiency recommendation. All tracked for final dedup, other defensive copies/qualifiers/gap/budget distinctions retained. Reuse angle pending before changes.

Fourth TASK5/6 simplify reuse angle a53ab568 complete no additional reuse changes. Retain shape-specific coordinate matching, collector-vsCLI normalization errors, runtime lexical/time parser, small context-specific IDscans and distinct Git fixtures; all explicit dispositions. All4angles now received; apply bounded unusedstate/shallowcopy/sethoist/proofreset cleanup, no framework changes.

All4 TASK5/6 simplify angles complete. Parent applied unused imported snapshot key removal, local shared proof invalidation helper, readonly validation shallow successor and per-event IDset hoist.100 history/comparator/ancestry/CLI/runtimeCLI tests -Werror pass31.351s. Declined additional tuple-cache/runtime matching refactor and optional qualification cache: bounded100k comparisons, extra normalized state risks unknown-value semantics for unmeasured gain; preserve independent error contracts and private helper layers per reuse/altitude dispositions. All recommendations landed, final highreview/fullsuite next.

Final integrated TASK5/6 reviewer reports3 reproduced candidates pending consolidated verdict: bareartifact query retained after exact selection allows othergroup sameartifact events; unknownmodule/realm snapshots dropped before ordering permits comparisons across missingcontext middle gap; timestamp ordering accepts invalid+00:60 offset normalized. All retained for final verification/disposition, no task closure. No runtime finding reported yet, not inferred clean approval.

Final integrated /code-review high ade08bc5 NOTAPPROVED3 confirmed findings after129tests. F1 selectedexample:c but bare queryc emits othergroup:c graph additions; constrain to selectedGA/context while versionless historical comparison retained, optional declarations same. F2 unknown module/realm snapshots dropped before grouping permit nonadjacent comparisons/reintroductions over gaps; retain compatible unknown barriers or unavailable group order, clear seen acrossgap. F3 timestamp API accepts offsetminute60 normalized and overprecision; strict component/precision validation needed, no fabricated chronology. Exact reviewed source graph7b54f4d4/ancestrya043deb6/pipeline2a153484/runtime65900def/CLI15018509; manifestcbbdb06e. All assigned disjoint pipeline/comparator owners, no cut findings.

Parent223 history pipeline+lower/report tests -Werror pass4.852s F1selectedscope fix pipelineabe65366aca9b99883f9ad778773bc2b18696f2a20993982ca403287e1b2cf95/tests99818118446883ac58a66140989b61a7de45dcdf5ee6a88e3c64c85a05b0dfe6. Versionless selectedGA/module/realm scopes prevent unselectedgroup events and retain upgrade history; aggregate caps/sourcequalifiers preserved, unknown snapshots forwarded barriers. Same final reviewer F1repro requested; comparatorF2/F3 pending.

Final reviewer ade08bc5 F1 selectedscope targeted APPROVEDabe65366/99818118 after25 pipeline tests and original realGit barequeryothergroup repro events[]/selectedscopeexample:c/project/. . No added findings. Version/multiGA/module/declarationpaths/unknownselection/aggregatecap/unknownbarrier-forwarding regressions pass. ComparatorF2/F3 explicitly outside approval remain pending; task not complete.

Parent current graph/pipeline/historyCLI76-test run FAILED2failures1error: archive-only transitive graph no event; branch/unattested tests now selected-subject-scope-unknown instead expected ambiguous/unknown context. Full trace read. Current comparator62efe682/teste56fdbfe, selected-scope pipeline fix now refuses archive-only unknown realm, so must reconcile user-visible workflow without inventing project realm or weakening selection. Integration owner to inspect actual evidence and define explicit same-run attested scope linking, not just delete assertions. No completion/green claim.

Recovered final comparatorF2/F3 same-reviewer ade08bc5 APPROVED62efe682/e56fdbfe after6 targeted tests and original gap/+00:60/.1234567Z probes. Unknowncontexts barriers/reset memory, knownforeign excluded, invalid chronology no events. No new findings; archive-only selectedunknownrealm CLI integration regression remains separately open pipelineowner. All original3 findings scopedfixed, not whole integration approval.

Parent210 pipeline/comparator/historyCLI/ancestry/report tests -Werror pass24.214s integration remedypipeline89a222d882ef49cf1cba7346f1f3c98f56f2b322a497f1f35f5b752280579a39/testa8c89e5fced665f7fd63d7082fe395ed805b9571d1a25b0ed179e68eac1d1bfe. Archive-only selectedknownGA/module unknownrealm now candidate scope only explicit imported realm+module attestation and actual matchednonrootnode; subject unchanged, derived candidate-history-scope qualifiers enforce no packagedrealm/build identity upgrade, stagepartial. Noattestation no events, unknownbarriers maintained; original3CLI regression tests now pass. Final same reviewer integrated recheck pending.

Final integrated reviewer ade08bc5 FULL APPROVED originalF1/F2/F3 + archive-only scope remedy, no surviving findings.82 focused tests + independent realGit subjectunchanged/partial/derivedqualifier/unselectedGA probes verified pipeline89a222d8/testa8c89e5f/graph62efe682/teste56fdbfe/historyCLI5e4d8a40. Current CLI0c4533b9 history behavior tested; unrelated shade CLI edits approved separatelya5039. Ancestry/runtime unchanged from integrated review. Shared simplify completed, final docs sync/fullsuite needed before TASK5/6 closure.

Final graphhistory reference2f779259934d32f4eaf75927c0dbda98d335bf1c7d80d8625cfa180b8d7b257d hash parentverified;5 skill-contract tests pass. Documents versionless selectedGA/context, unknownbarriers, candidate-only attested archive scope/inheritedqualifier, ancestryCLI vs stricttimestamp comparator. Sample worker reverified importer, no actual historical build. Final configured suite pending; no additional code changes.

Parent read full configured bpykb4kpe:1582tests in209.255s OK exit0, only expected duplicateZIPwarning noResourceWarning. All final history3findings+archiveunknownscope remedy approved, shared simplify all4dispositioned, final docs aligned. Prepare scoped completion proof, no runtime/build execution implied.

TASK5 completed as approved recorded-snapshot reconstruction: local actual ancestry+qualified graph diffs and candidate declaration leads, exact vs unknown/temporal scope distinguished, archive-only target via explicit attested candidate scope not packagedrealm inference, missing historical input explicit, no historical builds. Shared simplify fourangles dispositioned and code-review high finalAPPROVED all3findings+integrationregression; full1582 and fresh scoped tests pass. Reflect/verification invoked, docs aligned, original qualifications immutable and derived required. No new network/Maven/commits.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
