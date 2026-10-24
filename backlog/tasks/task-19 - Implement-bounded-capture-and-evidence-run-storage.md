---
id: TASK-19
title: Implement bounded capture and evidence run storage
status: Done
assignee: []
created_date: '2026-09-10 17:15'
updated_date: '2026-09-11 05:38'
labels:
  - implementation
dependencies:
  - TASK-18
references:
  - docs/superpowers/plans/2026-09-10-maven-dependency-investigator.md
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Implement Chunk 1: report schema, bounded captures, safe XML and atomic immutable per-run revisions using TDD.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Enforce reference integrity, qualification propagation and single-writer generation checks.
- [x] #2 Test collector-owned report writes against unrelated-file sentinels; unrelated files cannot be overwritten, including unsafe run/output paths.
- [x] #3 Test crash-safe consent state and XML safety across supported encodings.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation started after TASK-18 gates closed. Worker a26e66d82bf92aff9 owns only Python foundation modules and unit tests; no Maven/network/commits or shared tracker/docs writes. TDD required for report validation, captured-byte identities, encoding-aware XML and immutable single-writer run revisions. Parent will rerun tests, inspect implementation, then perform spec and quality reviews before task completion.

Implementer a26e66d82bf92aff9 failed with harness watchdog: no progress for 600 seconds. No completion/test evidence received. Inspect filesystem before replacement; do not count any claimed work as complete. Recover with a smaller focused implementation slice and preserve any partial files.

First production slice now exists: mdi limits/xmlsafe modules and tests. Parent ran python3 -m unittest tests.test_xmlsafe -v: 17 tests passed. Parent inspection found requested adversarial encoding coverage incomplete: hostile DTD tests only UTF8, benign UTF16 tests not equivalent; long prolog case below 4KiB. Returned to implementer for encoded hostile matrix, long prolog, local entity/no-I/O guard and precise frozen-dataclass assertion before spec review. No acceptance or DoD completion claimed; report/capture/runstore not implemented yet.

XML slice revision verified by parent: python3 -m unittest tests.test_xmlsafe -v passes 18 tests. Read actual revised tests: hostile DTD matrix UTF8/UTF8-BOM/UTF16LE-BOM/UTF16BE-BOM, explicit DOCTYPE messages, temp file entity and open/socket spies, precise FrozenInstanceError. Long-prolog test is benign and separate; bounded spec reviewer acb4dbf08c0119143 dispatched for this slice only before quality review. No report/capture/runstore implementation yet; task remains In Progress.

XML slice spec compliance reviewer acb4dbf08c0119143 APPROVED after fresh 18-test run and actual code inspection. It additionally probed long-prolog hostile DTDs and reported rejection; no bug found. Optional parse=xml/xpointer XInclude test not required for approval; defer extra variant to subsequent safety-test expansion within this task, preserving inert-XInclude requirement. Bounded code-quality review follows; report/capture/runstore remain unreviewed/unimplemented.

XML slice code-quality reviewer a574034e905f71919 APPROVED; fresh 18 tests passed. Both slice reviews closed. Advisory dispositions: retain second-pass ParseError handling as defense-in-depth; relative-import preference not applied because supported package name is mdi and tests intentionally load its scripts path, with no requirement for arbitrary parent-package embedding. No untracked promise to change imports. Next bounded slice is capture with tests; whole TASK-19 still open.

Bounded capture slice implemented in capture.py/test_capture.py. Parent full unittest run passes 36 tests (18 capture plus 18 XML). Read implementation: immutable capture data, max+1 sentinel, full digest only complete reads, fstat change qualification. Bounded spec reviewer a24a3c76154d25d04 checking exact symlink guarantee including ancestor components, relative path identity, and fail-closed unsupported flags before quality review. No confirmed new defect until verification; no whole-task completion.

Capture compliance reviewer verified two path-layer gaps: raw relative subject spelling aliases different files after cwd changes and rejects relative/absolute aliases of same path; no-symlink docstring overclaims ancestor protection when O_NOFOLLOW covers final component only. Unsupported O_NOFOLLOW currently degrades to racy lstat. Return to implementer: store lexical absolute capture-time path, scope documented guarantee to final component with explicit caller ancestor/root confinement, fail closed if required safe-open flags unavailable, add regression tests; no realpath or silent capability downgrade. Reviewer reconfirmed bounded-read/digest contract and 36 tests.

Capture fix review confirmed code changes; last doc-only overclaim narrowed to lexical relative/absolute normalization, explicitly excluding different symlinked-ancestor spellings. Parent independently reran entire suite after temporary Bash classifier recovered: 41 tests passed. No bypass used. Send final wording to same compliance reviewer for closure before quality review. Whole task still missing report/runstore foundations.

Capture slice compliance reviewer a24a3c76154d25d04 APPROVED final source/test state. All original path identity, final-component no-follow, fail-closed flags and wording gaps closed. Parent 41-test GREEN is the post-fix execution evidence; reviewer explicitly attributes it correctly. Begin bounded second-stage capture quality review; no whole-task completion.

Capture slice quality reviewer ad451d4a2827e9769 APPROVED after local 23-test capture run. Verified descriptor cleanup and agreed bounded-read/identity semantics. No actionable findings. Both capture slice reviews closed; start report schema implementation next. Whole TASK-19 DoD still requires integrated simplify/code-review/testing after remaining report/runstore work.

Report slice implemented; parent full suite 128 tests passed and read report.py. Parent minimal repro confirms selection-preservation defect: single s1 can silently widen to all s1/s2 because transition checks subset only, contrary to immutable selected scope. Return for exact selection equality after initial choice; other review pending. No report slice approval or task completion yet.

Additional bounded report fixes requested from inspection: newly appended operations must enter proposed state under local transition; analyses require nonempty evidence references; deepcopy constructor query to prevent external alias mutation. Standalone persisted consumed-state validation remains allowed for reload. Implementer will add RED/GREEN regressions and remove tests endorsing selection growth. No broader schema rework requested.

Parent reran report fixes: full suite 130 tests passed. Implementer corrected immutable post-selection scope, proposed-only new operation state, new-analysis evidence requirement and deepcopy query. Bounded report compliance reviewer af132cdd1ae2e420e now checking actual schema/transition contracts, including distinction between standalone snapshot validity and transition-only rules. No report approval yet; runstore still pending.

Report compliance review returned three gaps. Accept canonical nonempty analysis evidence at create/load, not transition-only. Accept input_identities item validation, but identities are structured capture metadata objects rather than generic strings; define minimal path/digest contract. All-mode semantics need clarification: freeze the explicitly selected query-matching candidate set, not every contextual/upstream subject in the evidence report. No auto-expansion after selection. Add nonempty/unique selected IDs and document collector responsibility for enumerating matching candidates; ask reviewer to assess this precise boundary. Unittest remains the test command; reviewer pytest environment issue is not a project failure.

Parent independently reran full suite after three report compliance fixes: 135 tests passed. Canonical analysis evidence requirement, strict path/lowercase SHA256 identity objects and nonempty unique frozen candidate selection implemented. Await same compliance reviewer final-state check; no schema approval or whole-task completion yet.

Report slice compliance reviewer af132cdd1ae2e420e APPROVED exact report.py dea5b08de16539335421be534e3a2ad9e2e0528bfc6cf141a09d9f9ec6e6e151 / tests 7fdb676fa8e914fa7b85fa69a3363b671a72ecadbcde8b7963b2c7a13aec6593 after independent 135-test run and targeted probes. All three gaps resolved; no residual findings. Proceed to bounded report quality review, then runstore implementation. No full TASK-19 completion.

Report quality reviewer ae6bdf9f8d897d1db found Python equality permits JSON type rewrites (1/True/1.0) in immutable fields. Parent reproduced query depth 1->True accepted by validate_transition. Fix required across append-only/immutable comparisons with JSON-type-sensitive equality. Minor add_qualification revision behavior: document as in-revision draft helper; runstore owns revision bump, do not auto-increment per qualification. Both tracked and returned to implementer; report slice not approved.

Parent full suite rerun after JSON type-fidelity fix: 143 tests passed. Implementer replaced immutable comparisons with recursive JSON-type-sensitive equality, preserving object-order insignificance and signed-zero/type fidelity; documented add_qualification draft revision semantics. Same quality reviewer requested to verify final fixes; no new review fan-out. Runstore waits on closure.

Report quality reviewer ae6bdf9f8d897d1db APPROVED bounded fix recheck on report.py 61e9395d8de4de3d05ace4776a62177250105609073670dc8430f58eef315d5e / tests c0f3db824f73a3e758953219d3ecc1ee668678b49cc6992b1b2a022d1d7e9fbd. Independent full 143 tests GREEN and adversarial type-rewrite probes rejected; object reordering accepted. Draft revision documentation accepted. Both report slice reviews closed. Begin final foundation slice: runstore; integrated task gates still pending.

Runstore implementer ab4e36fe8d3ef02d4 terminated by harness no-progress watchdog after 600 seconds. No completed implementation/test result received. Inspect partial files before recovery; preserve existing reviewed foundations. Recover with first smaller explicit-path storage slice, then add transactions; no failed work marked complete.

Runstore create/open/load slice implemented; parent suite 155 GREEN but actual source/probes reveal violations: create follows symlink base and returns unloadable store; open validates marker only and accepts missing initial report. Source inspection also finds blocking FIFO open before regular-file check, zero safe flags accepted, path-based chmod and unsafe cleanup-by-reopened-name. Returned for bounded TDD fixes: descriptor-safe create/no-follow paths, validate report on open, O_NONBLOCK and complete fail-closed capability checks, fchmod child fd/base durability, leave identifiable partial run rather than risk deleting replaced names. No runstore acceptance yet; commit/locking still pending.

Parent verified corrected initial runstore slice: full unittest 159 tests passed; runstore SHA256 16fa473fcffed9ce71566e771343d5126f1df7d46dce7cc540e1e606257022c0. Create/open path semantics unified, revision0 required on open, FIFO reads nonblocking, zero/missing capability flags fail closed, descriptor chmod, base fsync, no deletion rollback. Bounded spec reviewer a662787466db1d472 checking initial storage slice. Commit/locking remains next slice; no task completion claimed.

Initial runstore create/open/load spec reviewer a662787466db1d472 APPROVED after 16 storage tests and independent path/mode/JSON/bound/fd probes. All six parent gaps closed. Nonblocking restrictive-umask behavior accepted: removing owner bits may leave an inaccessible partial run and fail closed; no permission escalation or automatic recovery promised. Proceed to bounded quality review; commit/locks intentionally not yet implemented.

Initial runstore quality reviewer a3ce1f22864c240c7 APPROVED bounded slice with two small advisories accepted for immediate correction: preserve KeyboardInterrupt/SystemExit rather than wrap BaseException, and normalize fstat/read OSError as RunStoreError while keeping fd cleanup. Add deterministic error-injection tests before final quality closure; no scope change or task completion.

Parent reran full suite after runstore error-handling fixes: 161 tests passed. create now preserves control-flow exceptions; member fstat/read EIO normalized with cause and fd cleanup. Same quality reviewer requested final targeted closure; initial storage slice still lacks commits/locks by design.

Initial runstore quality recheck a3ce1f22864c240c7 final APPROVED at runstore a1af4e11c9d7023bec7d7ed091adf8374d818e1aa8c22a2586f5440f53143924; reviewer reran 18 storage tests and verified both error fixes. No residuals. Start commit/locking slice; current parent full suite 161 tests. Whole task gates remain pending.

Atomic commit/locking implementation received; parent full suite 181 tests passed, runstore SHA256 9d317164e6667f0b76c754b8ce2c8869c926e1978b2884f920530a9101aff641. Parent read implementation; bounded compliance review ab9d9400715e9bcab checks current guarantees including publication vs durability, interruption cleanup and lock/temp handling. Unknown-member strict policy retained for now; future scratch artifacts must integrate explicitly rather than silently weakening validation. No whole-task completion until reviews and integrated gates.

Atomic storage compliance review found two contract corrections: CommitUncertain currently overclaims durability after failed directory fsync; pre-publication cleanup wording overclaims interrupt/fstat-failure cleanup. Accept fixes: distinguish linked/contents-fsynced from unknown power-loss dirent survival; document best-effort cleanup with tolerated orphan temps and add inside-temp interrupt regression. Clarify writer lock coordinates cooperating writers, not same-user adversarial modification of owner-only directory; no claim that marker/nonce authenticates evidence. Main chain/generation/no-overwrite tests independently passed 38 storage tests. Not approved until corrected wording/test rechecked.

Parent reran final crash-contract fixes: 182 tests passed. Documentation now distinguishes visible linked report from unknown power-loss survival after failed directory fsync, states best-effort orphan cleanup, and bounds lock guarantees to cooperating writers. New inside-temp KeyboardInterrupt test checks old revision, released lock/fds, tolerated orphan and later successful commit. Same compliance reviewer requested closure before quality review.

Atomic commit/locking compliance reviewer ab9d9400715e9bcab APPROVED exact runstore 9acfe6a60e8b8d59a53206cf4cc71c5040298a3f2359d6ef894f950439c03fd9 / tests 936c746a9df8587de289c29ae27aca696a57deefad0c70f0af6ac1e87e96f550, independent 39 storage tests passed. Both crash-contract findings closed, no residuals. All TASK-19 implementation slices now exist; final storage quality and integrated simplify/code-review/test gates remain.

All foundation implementation exists. Integrated final gates now running on Python modules/tests only: /code-review high invoked as direct bounded review (also second-stage atomic-storage quality), /simplify invoked with four bounded cleanup angles. No recursive review fan-out or design/document re-review. Completion waits for findings disposition and final suite rerun.

Integrated simplify reuse review returned two accepted cleanups: single-source runstore fchmod/write/fsync sequence while retaining caller-specific exception/ownership handling; single-source qualification limitation text shared by builder and validator. Apply small internal helpers, preserve public APIs and syscall ordering, rerun full tests and notify final reviewer of changed hashes. No completion gate marked yet.

Integrated code-review returned two confirmed defects. Parent reproduced with small injected limit: create publishes revision0 exceeding MAX_READ_BYTES then cannot load. Also unsupported XML encoding leaks LookupError/ValueError instead of UnsafeXML. Fix initial serialization-size gate and normalize encoding errors with no parser fallback; add regressions and rerun. Approval withheld until final reviewed state.

Parent reran storage tests after initial-size fix and _sync_write cleanup: 40 storage tests passed; hashes match implementer runstore3497353e6d3e2bec931172f82bef62b91d4428868211fbe7f876d0d92ac95d74/tests edc9c39bb06c95d161e83453302be40fba693829c7353b861def0e5e130e2b68. Initial report serialized/size checked before child creation; fault-injection partial-run tests moved to second member write preserving assertions. Final integrated reviewer notified; XML encoding fix and other simplify responses pending.

Parent full suite after XML encoding boundary fix: 185 tests passed. Unknown/unsupported declarations normalize LookupError/ValueError to UnsafeXML, preserving handler messages and control-flow exceptions; regression no-I/O checks pass. Integrated code-review findings both fixed and ready for final recheck; still collecting simplify completion evidence.

Integrated simplify simplicity result received. Duplicate write-sequence finding already resolved by _sync_write. Verified _MISSING had only its definition via rg and removed unused sentinel; 102 report tests passed after removal. Preserve existing cleanup behavior; do not move unlink into finally because interrupt-orphan behavior is deliberate. Final reviewer notified of one-line change; no API/behavior change.

Integrated code-review a0b807764f159a92c returned APPROVED for foundation readiness, updated report hash 60481f7395badc251d3ca7db5694ca72e2bb887b3a68e7062787fcb612be6321 after unused-sentinel removal; other reviewed hashes unchanged. Parent full185 unittest rerun passed and current hashes verified. Request explicit final confirmation of both original integrated fixes because earlier approval message was not delivered to parent. Simplify efficiency/altitude results still pending; no TASK-19 Done yet.

Integrated high-effort review final evidence received explicitly: both defects fixed (size gate before mkdir including exact limit; unknown/UTF7/UTF32 and second-pass encoding failures -> UnsafeXML); _sync_write checked with short writes; qualification formatting checked through commit/reload/history. APPROVED current report60481f7395badc251d3ca7db5694ca72e2bb887b3a68e7062787fcb612be6321/runstore3497353e6d3e2bec931172f82bef62b91d4428868211fbe7f876d0d92ac95d74/xmlsafe7b794e43825079b0817aaa641bae20c5d65798efc04ef8b04e79a89a5a76babd; reviewer full185 GREEN, parent verified same state. DoD review and tests checked; simplify waits only remaining efficiency/altitude outputs.

User requested continuation. Parent executed fresh cross-module smoke: bounded capture -> safe XML parse -> evidence report -> immutable commit -> qualification -> second commit -> reopen/load; original revision limitations unchanged, current qualifications inherited, stale expected revision rejected. All foundation acceptance criteria now evidenced. Existing review/test gates passed; final efficiency/altitude responses requested directly without expanded review. TASK-20 remains blocked until remaining simplify findings are dispositioned.

Final altitude simplify output received. Qualification-text/write helpers already applied. Proposed moving all test sys.path setup to tests/__init__.py is SKIPPED: it changes supported invocation behavior (direct files and discovery with tests as top-level start directory need not import tests package initializer). Existing module-local bootstrap is small and intentionally preserves those modes; no runtime code duplication or new test-framework setup introduced. Not a correctness blocker and no deferred task promised. All altitude recommendations now dispositioned; efficiency output remains outstanding.

Final efficiency result received. Applied two allocation-only intersection truth checks as not isdisjoint; full185 tests pass. Deferred mapping reuse/lazy error-path formatting to concrete TASK-24 pending measurement: changing validator return API or adding lazy path state is not justified by unmeasured estimates. All four simplify angles now dispositioned; DoD simplify checked. One-line semantic-preserving report change requires final existing reviewer recheck (report SHA256 e0138382c19ad28184dac095f6d9fa52ef17f4bc5c807d6397d5b7710b1447c2); review checkbox temporarily reopened until exact-state confirmation. No further review expansion.

Final-state high-effort reviewer APPROVED report e0138382c19ad28184dac095f6d9fa52ef17f4bc5c807d6397d5b7710b1447c2 after verifying only isdisjoint changes; remaining approved hashes unchanged. Parent final full185 unittest run passed. /pa:reflect completion: TASK-19 foundations meet bounded scope, not complete product. /simplify all recommendations addressed/tracked, code-review high approved, test evidence recorded. TASK-19 Done; proceed TASK-20. No commits/push.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
