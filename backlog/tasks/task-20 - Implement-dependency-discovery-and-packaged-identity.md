---
id: TASK-20
title: Implement dependency discovery and packaged identity
status: Done
assignee: []
created_date: '2026-09-10 17:15'
updated_date: '2026-09-11 12:59'
labels:
  - implementation
dependencies:
  - TASK-19
references:
  - docs/superpowers/plans/2026-09-10-maven-dependency-investigator.md
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Implement Chunk 2: bounded declarations, archive discovery, candidate selection and typed artifact correlation.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Discovery records checkout revision and dirty state explicitly, or identifies unavailable Git context.
- [x] #2 Discover archive-only transitive candidates even when another POM candidate matches.
- [x] #3 Distinguish plugin/project realms, byte identity and current-build provenance; enforce cumulative archive limits.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Feasibility review confirms helper spike covered ordinary JARs/exploded roots but no Boot nested-container fixture. Do not assume packaged Boot discovery was proved by upstream reference tests; explicitly exercise nested identity/inspection before its acceptance criterion is complete.

First declarations-only discovery slice implemented in discover.py/test_discover.py. Parent full unittest run 214 passed; read actual508-line module. Descriptor-relative bounded POM capture preserves nofollow ancestor contract without editing prior foundations. Potential shared capture_at extraction is deferred until integrated simplify, not adopted blindly. Bounded compliance reviewer a6a1109274d88d405 checking semantic namespace/root/profile cases and safety. Archives, candidate integration and Git context still unimplemented; no TASK-20 completion claim.

Discovery compliance review returned four actionable items despite summary saying three: namespace/root validation, id-less profile provenance, quadratic logical locator computation, and omitted profile properties. Accept fixes with targeted regressions; do not rely on unverified Maven-legality claim for id-less profiles: preserve profile context even for incomplete/invalid input. Reviewer timing values are probe-specific, not performance guarantees. Grandchild conditionality noted: preserve parent linkage and avoid inference of unconditional activation; no need resolve profiles. Existing safety/raw-coordinate cases and full214 tests passed in review; slice remains unapproved.

Parent verified four declaration fixes: full220 tests passed; no remaining profile_id consumers under skills/tests. Profile object now explicitly retains optional id and source locator, scoped properties; namespace/root guards added; quadratic locator rescans replaced with cached per-parent positions. discover hash739c0b983dc89ed017e57138a2af0308a1af31b016ddd08ac9b0b128fcc485bc. Same compliance reviewer requested targeted closure; quality and remaining archive/context work still pending.

Declaration compliance reviewer a6a1109274d88d405 APPROVED fixed source739c0b983dc89ed017e57138a2af0308a1af31b016ddd08ac9b0b128fcc485bc with independent220-test run and original-case reprobes. All four findings closed. Reviewer timing results are local observations, not claimed product benchmarks. Nonblocking duplicate-profile-id locator collision noted for targeted quality review; exact namespace matching is intentional, not residual. Proceed bounded code quality, archives/Git context still pending.

Declaration quality reviewer aad574e93626a923d APPROVED with one concrete nonblocking locator issue: duplicate sibling profile IDs produce identical source locators. Accept small fix now: count IDs per profiles container and use positional locators for all duplicated IDs, preserving unique-ID locator compatibility. Add regression before final reviewer closure; descriptor/namespace/module-limit probes passed.

Parent verified duplicate-profile provenance fix: full221 tests passed. All repeated IDs within profiles container now use positional locators (including first); unique unquoted IDs unchanged. Regression checks declarations and profile-module provenance separately. Same quality reviewer final targeted recheck requested; remaining TASK-20 archives/context work not yet implemented.

Declaration discovery final quality recheck aad574e93626a923d APPROVED exact discover c62f8c9d492a3923f049f6b2d129824f656ef107a4d86eb810298712374e1b96; independent36 tests and nonadjacent/per-container duplicate probes passed. Declaration slice closed. Begin bounded packaged archive inspection; no whole-task completion.

Archive slice implementer a7f33b67d04b41d64 started, owns archives.py/test_archives.py only. Initial bounded in-memory single-artifact reader with nested JAR/WAR/Boot, duplicate-copy identity and candidate metadata. Must preflight ZIP metadata before entry-list allocation and preserve shared expanded/entry/depth budgets. Conservative initial memory limits (64MiB input vs plan final2GiB) are staged implementation limits, NOT silent replacement of final supported-output goals; assess streaming/process boundary before task acceptance and track any deferred large-artifact support explicitly. No unapproved tool/network execution.

Archive slice implemented; parent full273 tests passed (expected zipfile duplicate-name fixture warning). archives SHA256 f32f8feaef9411dffbc861cbc34d2f76832dcde6be13de78f759ed71e3785cc1. Bounded compliance review a18b4c33f8e5a64db running. Initial limits are not final plan defaults; large archive/ZIP64/compression/hard decompressor deadline gaps explicitly tracked TASK-25. These remain product requirements/decisions, not silently complete. Private stdlib source behavior and declared/actual mismatch need independent checking before acceptance.

Archive compliance reviewer a18b4c33f8e5a64db APPROVED bounded slice after independent52 archive tests and probes. Preflight/actual size mismatch/nested budgets/provenance covered. Nonblocking lying-low directory count still allocates entries up to metadata cap before recheck; record with TASK-25 memory-hardening follow-up, not an unbounded-safety claim. Doc runtime version typo requires correction before quality review.

Corrected archives.py doc-only CPython version typo from 3.14.14 to verified3.14.7. No behavior changes. Begin second-stage archive quality review with known bounded metadata allocation qualification and unsupported formats tracked.

Archive quality review aa746554e1010c1f7 found no blockers and one actionable cross-module coupling: truncation classification parses CapturedFile.reason prose. Accept bounded regression at archive/capture boundary now, preserving shared capture API; document diagnostic wording as an explicit compatibility dependency until structured capture status is introduced. Implementer to test real oversized capture -> input_truncated/issue mapping, not mock a prewritten reason. No other quality findings.

Parent full-suite rerun after archive/capture boundary tests: 275 passed; expected duplicate-name warning from fixture. Added real oversized capture -> input-truncated and fstat-drift -> input-changed assertions plus local compatibility note. Archive implementation behavior unchanged. archives hash bd119b9b45d5d93ceaad524bf2cb1ac95ceb6f5d2ec130977a68c7b5a18e31e2; tests17807ce0426d2f72506274b8b1deb1d0fcfe6f0340e7ce28640ff10e8dc1b8a3. Implementer runtime variation attribution to contention is unverified and not accepted as a causal finding. Request quality reviewer targeted closure; no whole-task completion.

Archive quality reviewer aa746554e1010c1f7 final APPROVED after independent275-test run and mutation probe proving truncation-wording regression detected. Changed-input wording remains intentionally unconstrained; classifier behavior only is tested. Archive slice reviews closed within documented bounds; TASK25 retains resource/format gaps. Start checkout context slice; no whole-task completion.

Checkout-context slice implemented; parent full295 tests passed, gitinspect SHA2566f421a19be36413dbd58743a87c62fcbac3ea4129cb2062aad6d90716aec9559. Bounded compliance review a54702d6c5771bce8 probing leader-exited descendant cleanup, structured root parsing vs diagnostic truncation, relative PATH executable identity and double-slash root handling. No confirmed findings yet. Checkout context acceptance waits review; no history collection implied.

Checkout-context compliance reviewer confirmed four blockers against gitinspect6f421a19: exited leader prevents group kill on timeout/cap leaving pipe-holding descendant; diagnostic240-character/line-splitting parser corrupts long/newline checkout root identity; relative shutil.which path checked in parent cwd but executed in repo cwd can invoke a different wrapper; double-slash root loops in path hygiene before timeout. Return targeted regression fixes to implementer. Qualify check-then-use path/tool races; no claim of adversarial filesystem isolation. Slice not approved; existing295 GREEN does not cover these failures.

Parent full302-test run after four checkout-context fixes returned OK and hashes matched source74a2c66a248c7d5ef2c12dc781246f76906fcd619c242b63ff3d60a0d65089e9/tests94493bce5e3e4b8388bfaa6337a617192ab14abb01ae3e34ce19edcd25faa1a8. Unexpected ResourceWarning: subprocess551821 still running. Sent warning to implementer for isolation, not ignored due to green suite. Original compliance reviewer waiting for stable final state. Kill delivery wording now precise; no approved cleanup claim until warning explained/fixed.

Stable checkout fixes parent-verified: full302 tests passed with ResourceWarning explicitly enabled and none emitted (only known duplicate-ZIP-name UserWarning). Source unchanged74a2c66a248c7d5ef2c12dc781246f76906fcd619c242b63ff3d60a0d65089e9; test23095a5dc24afb4c4f19a11205333769e1d87868a7b24b3b239c8d01512657c0. Implementer attributed old warning to intentionally refused kill mock retaining live child; revised mock actually terminates then raises, tests unconfirmed-report semantics without fixture leak. Do not treat this as proof of cleanup under real persistent kill denial. Same compliance reviewer final recheck resumed.

Checkout-context compliance reviewer a54702d6c5771bce8 APPROVED source74a2c66a/tests23095a5d after independent302 warning-enabled tests and original-four reprobes. All blockers closed. Nonblocking absent-process-group message branch lacks dedicated test: retain as minor test-coverage consideration for integrated task tests, no extra review cycle now. Begin bounded code-quality review; candidate/correlation integration still pending.

Checkout-context quality reviewer ab09c457acf1aa106 APPROVED after two27-test runs incl dev/ResourceWarning-as-error. All slice reviews closed; existing absent-group optional branch note already tracked. Start pure artifact correlation before final candidate orchestration; no task completion.

Correlation slice received; parent330 tests passed and source inspected. Before formal review, returned concrete contract gaps: graph attribution ignores dirty-working-tree identity, known coordinate conflicts do not block strong claims, packaged realm/module absent from comparison, missing generation time not gated, partial digest contradicts schema prose, normalized blank profiles accepted. Require targeted RED/GREEN tests preserving byte-identity vs provenance distinctions. Implementer reported unexplained mid-run test edit; parent made no such edit and does not infer cause/ownership. No correlation acceptance yet.

Parent reran revised correlation: full341 tests passed; source0f74d098a1d665da5db730167b399db6567bd92c8adfffc781cfb9206c2aebbf tests015fbda64fcf200c472996bc409b3a01b15dc9c7762da362eaca020c8b2593d2. Stronger claims now gated by dirty/context/identity/generation evidence; incomplete digest and normalized blank profile rejection added. Bounded compliance review a7733fc22a9f3343d started. Implementer ownership note internally inconsistent (denies any test edits then reports own later edits); parent has not modified test file, judge actual current artifact/tests rather than inferring edit provenance. No correlation approval yet.

Correlation compliance found sentinel handling gap: report realm literal unknown can satisfy known-equality gates; whitespace-only provenance strings accepted. Fix realm validation to project/build-plugin/unknown-or-null with unknown treated as missing, and reject blank strings across string fields. Do not reserve legitimate module name unknown globally: non-realm missing values must be represented as null by adapters. Regression tests require unknown realm preserves byte identity but blocks graph/build attribution. Reviewer pytest setup failure is environment-only; supported command remains unittest.

Correlation reviewer accepted narrowed sentinel contract and withdrew module-name portion of its first finding: only realm reserves unknown; other missing dimensions use null, real module named unknown remains valid. Realm-sentinel and blank-string fixes remain pending with implementer; no re-review or approval yet.

Parent verified realm/blank-string fixes: full347 tests passed. Realm restricted project/build-plugin/unknown/null; unknown normalized on copies, missing blocks relevant provenance flags; real module unknown remains valid, whitespace-only provenance rejected without stripping identities. Same compliance reviewer final recheck requested before quality. Candidate orchestration still pending.

Correlation compliance reviewer a7733fc22a9f3343d APPROVED final source791261d7440e05618c95085ad53492a5f8326bb2d9b4db21532a15ee585f1b58; independent45 tests plus sentinel/blank/identity probes passed. All original gaps closed. Begin bounded quality review; candidate orchestration next.

Correlation quality reviewer ab207c0b2b54d20fa APPROVED with no correctness findings. Nonblocking nits dispositioned: duplicate realm in internal required-key tuple is diagnostic-only and reserved for integrated simplify; unreachable missing-digest branch retained as defensive code, no separate refactor. Correlation slice reviews closed. Start candidate orchestration with existing declarations/archive/context outputs; no need reopen completed slices.

Candidate orchestration implemented; parent377 tests GREEN and source read. Parent found contract gaps requiring fixes before review: new packaged-copy realm conflicts existing closed realm vocabulary; package kind must be separate from dependency realm. Path preflight/result dropping cannot prevent outside-root reads and scoped prompt required safe integration or an explicit API-extension request. sorted(scandir) allocates all entries before cap; traversal/depth/error counters not consistently global/honest. inspect_archive failures can abort siblings; max archive budget explicit path duplication may overrun. Candidate aggregation can conflate management and direct inclusion or different owning plugins; metadata verified flag must not mean verified component identity. Return targeted integration corrections, not accept declared qualifications as substitutes for safety.

Approved narrow internal API extension for candidate safe integration: archives.inspect_captured_archive(CapturedFile) reuses existing inspection without reopening paths; discover exposes descriptor-root/module/capture helpers with explicit ownership and path-component validation. Implementer scope expanded to archives/discover and corresponding tests, no concurrent owners. Candidates must scandir held descriptors and capture relative single leaf, not reopen entry.path; no private-import shortcut. Existing APIs/tests retained. Proceed other assigned realm/budget/failure/no-POM fixes in same slice. No external operation authorization implied.

Safe candidate API extension/full corrections received; parent401 tests passed. Found actual fd-ownership defect by local probe: os.scandir(fd) does not close caller fd; candidate _bounded_entries leaked its explicit os.dup per directory despite false ownership doc. Requested close in finally + repeated fd-count/constructor-error regressions. Also requested machine field text_matches/verified_component distinction instead of ambiguous verified=True for metadata text. Candidate review waits corrected final state; no completion.

Candidate fd-leak and match-field fixes parent-verified: full403 tests passed; explicit scandir duplicate fd closed in finally with repeated-run/constructor-error tests. Match flag now text_matches, no verified component-identity implication. Bounded compliance reviewer afe2e7cd66db3f388 reviewing orchestrator and narrow archives/discover public API changes, exact cap/traversal determinism/global budget semantics. No TASK20 completion yet.

Candidate compliance review confirmed one blocker: CaptureError (nonregular FIFO leaf) escapes archive/explicit capture handlers that catch only OSError, aborting siblings. Fix both with isolated issues and real FIFO regressions. Also track nonblocking DT_UNKNOWN DirEntry lazy stat after closed scan descriptor: use held directory fd for explicit entry stat or materialize metadata while iterator descriptor remains open; do not accept avoidable filesystem-dependent discovery loss. Remove dead finally:pass with scoped cleanup. Unknown plugin-coordinate merging remains candidate-only with raw occurrences, not resolved identity; no stronger claim accepted. Same reviewer recheck after changes.

Parent verified candidate isolation fixes: full406 tests passed; actual FIFO target/explicit leaves now isolated issues preserving good siblings. DirEntry metadata frozen while scanfd live; dead finally removed. candidates SHA256 f1a7d6a1ddbf90398895c0be6e5c6cd9973959a8577e48eef34a098f61dbf25a. Same compliance reviewer requested targeted closure. No candidate approval or TASK20 completion yet.

Candidate orchestration compliance reviewer afe2e7cd66db3f388 APPROVED source f1a7d6a1/tests7f7fac31 after independent406 tests, exact-cap and25-run FIFO/fd probes. Narrow archive/discover API hashes unchanged. One nonblocking over-indented else body noted for integrated simplify, no functional gap. All TASK20 implementation slices now exist; second-stage candidate quality will be combined with integrated high-effort code-review. Final task scope retains explicit TASK25 large/archive CPU limitations, not full-product completion.

Integrated completion gates launched on TASK20 Python only: direct /code-review high (no fan-out, includes candidate second-stage quality) and /simplify four bounded cleanup angles. No design/research re-review. Current full406 tests pass. Final acceptance still limited to initial archive envelope; TASK25 remains explicit large-format/resource follow-up, not silently delivered.

Integrated simplify simplicity findings applied: dedented candidate orchestration try body, replaced duplicate/dead derived resolved-key tuple with one explicit unique field tuple. No accepted key-set/API/behavior change intended; parent89 candidate+correlation tests passed. Final reviewer will receive current hashes; remaining simplify angles pending.

Integrated efficiency review returned two accepted small cleanups: deduplicate successfully inspected explicit artifact before target traversal so it does not consume budget/work twice; use early-exit first-child lookup for scalar POM fields instead of building full child lists. Add work-count and budget regression for explicit artifact, preserve retry of failed explicit capture. No performance measurements claimed; protected metadata/digest/path guards unchanged.

Integrated high-effort review found five verified defects: Git status can execute clean filter on mtime-only changes; nested archive loop exceeds shared entry budget after recursion; target traversal opens/retains child descriptors before directory-budget enforcement; pom.properties parses only4096-byte preview allowing hidden conflict/truncated identity; module=. explicit artifact containment comparison rejects valid root paths. All fixes assigned with reproductions; no gate approval. Existing efficiency edits must integrate with these fixes without overlapping owners.

Discover early-exit scalar lookup cleanup delivered; parent45 discover tests passed. Cross-worker full-suite failures observed during candidate edits are not accepted as settled regressions; await stable owner result. New potential NUL-validation literal bug in public relative-part helper reported for verification/fix in discover owner scope; no unsafe OS access succeeded, but declared input-validation contract needs checking. Track all suggestions same turn.

Parent independently verified NUL guard fix:47 discover tests pass. Literal backslash-x00 guard replaced actual NUL character test; regression proves rejection before os.open, legitimate POSIX backslash filename still supported. Scalar first-child cleanup remains applied. Other worker tests observed mid-edit are not final regression evidence; no integrated pass claimed until all owners stable.

Archive integrated-review fixes delivered; parent independently ran66 archive tests GREEN. Shared entry count now checked before every resumed parent entry; metadata parser uses full bounded content while raw preview remains4096 display-only. Captured archive API retained. Worker full-suite run had2 Git failures during active Git edits, so no integrated GREEN claimed; Git owner still must deliver stable state. No proof inferred from worker mutation attribution; API refactor was previously authorized and retained.

Candidate integrated fixes stable; parent165 candidates/archive/discover tests GREEN. Explicit successful artifact skipped before work/budget, directory slots reserved before opening and owned fds promptly closed, normalized root-module containment corrected. candidates033a606d495ca03fec1f1577db7787d86910be0afee3abb30dc9e13824ab7f35. Worker reported intermittent Git RevisionShape unavailable-vs-partial failure on first full run then pass; do not dismiss rerun pass, forwarded to Git owner for investigation. Final full review waits stable Git.

All integrated-fix owners now stable. Parent warning-enabled full425 tests GREEN, no ResourceWarning (known duplicate-ZIP fixture warning only). Git sourcef09c17e735a74bd3b8e5a986ffd7550dace8713fdc513919b801fa6d0381a1b3 tests9059c413f344b5bc8379c4a835b0a84eaeaaa48b47bf153856063561edcefa62. Dirty-state status now refuses configured filter.* drivers, retaining revision with dirty unknown and explicit issue, rather than executing clean/process commands. Worker reproduced old-shim/new-module mismatch signature and reports repeated settled passes; parent confirms current stability but does not independently establish past timing. Timeout starts after Popen (child execution budget, launch latency not bounded). Existing final reviewer resumed once for original five+minor fixes; no fanout.

Final reuse simplify result received: accepted single archive-suffix constant shared by nested and target scanning. Accepted directory-open safety reuse via existing public open_module_dir rather than exporting raw flags; remove candidates _DIR_OPEN. Narrow cleanup assigned candidates owner for archives/candidates only, preserve directory budgets and fd ownership, tests required. No broader shared framework. Final reviewer notified of pending small delta.

Final reviewer referenced undelivered queued-descriptor finding; requested exact repro. Parent source inspection independently identified missing outer cleanup: _walk_target_tree budget return/interrupt can abandon still-queued owned child fds. Assigned outer finally draining queued owned fds plus archive-budget/interrupt regressions to existing candidates owner alongside reuse delta. Do not close review based on missing report; no gate approval.

Queued-fd cleanup delivered; parent warning-enabled full428 tests passed, no ResourceWarning. Outer walker finally drains queued owned descriptors while inner finally closes popped owned descriptors, caller descriptor untouched; archive-budget and exception/interrupt regressions added. Reuse suffix/shared safe-open delta retained. All implementers now stable; send exact hashes for final bounded review. No task completion until final approval and remaining simplify disposition.

Final altitude simplify report received and applied: shared _capture_or_issue for identical fd-relative leaf capture/error mapping; explicit parent-open/close and inspecting wrapper responsibilities preserved. Parent55 candidate tests passed. All four simplify angles now addressed; latest candidate hash sent for final same-reviewer delta verification. No new architecture or unresolved cleanup recommendation.

Final integrated reviewer APPROVED all10 scoped files; parent computed sorted path/SHA256 manifest4906c3efab5324cb72febc3d75ff1758665844238639c4376337cbb7a6fa0f95 matching reviewer exactly. Parent warning-enabled full428 tests passed, no ResourceWarning, expected duplicate-ZIP fixture warning only. Original five integrated issues plus queuedfd/reuse deltas verified, no residual. /simplify all findings addressed; /code-review high approved; reflect confirms bounded TASK20 scope fulfilled, NOT full large-archive/resource support (TASK25) or complete product. TASK20 Done. No commit/push.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
