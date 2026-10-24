---
id: TASK-21
title: Implement actual usage evidence and local history
status: Done
assignee: []
created_date: '2026-09-10 17:15'
updated_date: '2026-09-12 07:45'
labels:
  - implementation
dependencies:
  - TASK-20
references:
  - docs/superpowers/plans/2026-09-10-maven-dependency-investigator.md
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Implement Chunk 3: real helper references and bounded local Git event/rationale collection.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Test production, test and upstream usage, including explicit reflection and service-loading limitations.
- [x] #2 Keep last modification distinct from addition, removal/reintroduction and current-version introduction; fixture tests assert each role.
- [x] #3 Qualify historical candidates, source/binary mismatches and duplicate class owners without guessing.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Real standalone Apache analyzer probe passed upstream B-to-C reference extraction for JAR/exploded roots. Temporary probe first-wins duplicate ownership is NOT acceptable production behavior; preserve multiple candidate owners and ambiguity. Multi-release handling requires fixture qualification before claims; no production code copied yet.

Independent feasibility reviewer confirmed no non-empty duplicateOwners fixture and no multi-release fixture exist in the temporary helper probe. Add these before production usage claims; do not promote first-wins spike logic. Empty helper stdout logs are not usage evidence; saved JSON and independently repeated live runs are.

Production usage-helper first slice written; parent supported unittest command full440 tests passed, Java helper3aa73c004a75f48f2fe9454e1f22e8a71c91261123520144db01ab94da93e5d9 tests88e527c160cfe2baf7ea64772d7a8ffffb026a90d1a9ffb4c874d997fa0c80c0. Temporary pinned-jar path remains development bridge, not distributable packaging. Reviewer a837acd287bd23844 checking bounded helper contracts/API/safety; Python staging/adapter/history still missing. Worker pytest detour unnecessary, unittest passed without environment workaround. No-commit state does not prove files unmodified; parent uses actual files/hashes/tests.

Usage-helper compliance review found two gaps: exploded-directory META-INF/versions entries bypass jar-only multirelease guard and emit misleading prefixed class names; budget Javadoc overclaims pre-analyzer enforcement for total classes and materialized reference counts. Fix directory guard and pre-sum preflight class counts before analyze; label reference cap as output cap with analyzer allocation bounded by staged input + runtime process memory limits (not preallocation proof). Python adapter must provide hard JVM heap/output/time bounds; no hidden guarantee. Existing12 helper tests/API reuse/all-owner/sentinel checked by reviewer.

Parent plain unittest full442 tests passed after MR exploded-directory guard and total-class preflight fixes; helper1fb779c532e499940fb64f59bee16595cac2c5dee3ee7087da235a11d0be4e54. Reference cap documented output-only, JVM allocation isolation remains Python adapter responsibility. Same compliance reviewer checking fixes and narrow-only test limit property. No packaged helper/adapter/history completion yet.

Helper compliance recheck confirmed original MRdirectory and pre-analysis budget fixes, but test-only mdi.maxTotalClasses property can raise fixed65536 limit. Accept narrow-only validation fix: permitted integer range1..MAX_TOTAL_CLASSES, reject zero/negative/above-max/noninteger with clean validation error. Add no-analyzer invalid-class regression; no scope expansion. Helper still not approved.

Parent full443 unittest tests passed after narrow-only helper override fix. Valid range1..65536 enforced before analyzer, invalid/overflow clean exit2, allowed max and positive narrowing retained. Same reviewer requested final residual closure; helper hashb9b79a44b95db670ae052c79f16f2e30481722f02f5a062bed862d5b39fc5a89. No TASK21 completion.

Usage-helper compliance reviewer a837acd287bd23844 APPROVED final helperb9b79a44/testae4d3cca after independent443 tests and original override/MR/preanalysis probes. No residual slice findings. Begin bounded second-stage helper quality review; JVM bounds/staging/portable dependencies/history still pending.

Helper quality reviewer ab9a291a934b4fdb9 found two small proven issues: budget tests share fake-classes dir causing reverse-order FileExistsError; manifest-only MRjar reports fictional versioned-entry count1. Accept fixes now: isolate per-test fixture dirs (not just masking with exist_ok if file counts differ), preserve conservative MR rejection but distinguish manifest flag from observed versioned entries. Add reversed-order and flag-only diagnostic regressions; no scope change.

Parent full444 unittest tests passed after helper quality fixes; helper373b75a5f2728ecc4d56b98fe30e1b9c424f546b4c27598b35bc2d79f9ce520e. Fake-class fixtures isolated per test; manifest-only MR diagnostics retain actual0 count and separate flag. Fixture jar creation now uses explicit --manifest so manifest-only condition is genuinely exercised. Same quality reviewer final targeted recheck requested. No full TASK21 completion.

Usage-helper quality reviewer ab9a291a934b4fdb9 final APPROVED helper373b75a5/tests a7f9a24a after independent reversed-order tests and manifest-only probe. No residuals; helper slice reviews closed. Start Python execution adapter with explicit supplied helper classpath, sanitized JVM environment, heap/time/output limits and strict result validation. Portable distribution and safe input staging remain separate steps; no full-task completion.

Python usage runner slice implemented; parent supported full467 unittest run passed and hashes verified usagea576c317fd92f1759ff73336252d3c523aeaa5b7ab32e9399ef31e5bbc3d9761/test543f06b05e955c4eb8d9fc2996e72ae66a6d0e85aef1dcd63e5bc55eddbcc29b. Explicit hash-verified classpath, staged-input caller boundary, sanitized JVM heap/time/output and schema validation now exist. Bounded compliance reviewer ac6183bb56730ca33 dispatched. Local subprocess mirror recommendation tracked for integrated simplify; no automatic extraction into already-reviewed Git module without evidence. Staging/portable dependency delivery/history still incomplete.

Usage adapter compliance review found source-backed issues: list-based class membership repeated per owner/ref yields avoidable quadratic validation; origin from_class not required to exist in origin class index. Reviewer execution was blocked by classifier outage, so numerical timing/comparison estimates are not measurements and parent467 GREEN remains last suite evidence. Accept set indexing, declared helper count caps, origin-membership regression and robust invalid-shape rejection; no validator benchmark claims. Review remains unapproved.

Parent verified usage-validator fixes: full471 tests passed at usage5e5a225146e27d51c397fa4236a469d98d3f292b73407bec0803a9a42e6167e0. Added per-input sets, count limits, origin class ownership, robust malformed types, duplicate-target rejection and separate Python validation deadline/diagnostic bounds. Same compliance reviewer targeted recheck requested; no measured performance claim or completion yet. Runner remains1028-line slice, integrated simplify must assess targeted reuse only after correctness.

Usage adapter compliance reviewer ac6183bb56730ca33 APPROVED exact usage5e5a2251/tests869055a5 after independent27 tests and explicit diagnostic-cap/deadline probes. Nonblocking two missing regression tests accepted for immediate addition before quality review: cap enforced during accumulation and deadline-expiry yielding failed/no report. Linear count/issue loops bounded by output byte cap noted, not claimed hard global Python runtime isolation. No new feature scope.

Parent full473 tests passed after adding diagnostic-cap and mid-validation deadline regressions. Usage source remains compliance-approved5e5a225146e27d51c397fa4236a469d98d3f292b73407bec0803a9a42e6167e0; testf5403ccc99a5d4c3d7a5fbae975eb428e588d01db3c29006557bc9c9def4f520. No production changes; begin bounded adapter code-quality review. Staging/packaging/history still open.

Usage quality reviewer a5a56bf391f01bdcf found two concrete residuals: deeply nested helper JSON raises uncaught RecursionError at decoding; helper issues array lacks count/deadline enforcement and can yield complete after validation budget overrun. Assign bounded decode/validation error normalization and issue count/deadline regressions. Reviewer29 tests GREEN does not cover failures. Timing values remain reviewer-local observations, not product benchmark. Slice not approved.

Parent final usage-fix verification: full476 tests passed; usagee5216afe13a913c4810c3d9875da7fdfde45dc26e0733e5dc2cd7f594b9545be tests92fd175864aa11ebb71f10893453c9a7485004b064face51813a17580ce6f434. JSON recursion narrowly normalized; helper issue count64 prechecked and loop deadline/aggregation bound added. Same quality reviewer final two-gap check resumed. Deep recursion test threshold is interpreter-specific, not advertised universal count. Staging/packaging/history still pending.

Usage adapter quality reviewer a5a56bf391f01bdcf final APPROVED e5216afe/92fd1758 after independent32 tests and both original probes. No residual findings; helper and adapter slice reviews closed. Begin safe immutable input staging next; portable packaging and local history remain. No full TASK21 completion.

User explicitly authorized parallelizing independent work. Staging owner adf079878897676b6 keeps staging.py/test_staging.py. New independent packaging task26 owns helper build/manifest/notices + its test; history fixture task27 owns tests/history_fixtures.py/test_history_fixtures.py. No overlapping writes, no Maven/network/commit authorization added. Parent will integrate/review stable outputs and avoid full-suite claims during concurrent edits.

Tracker assigned child IDs TASK-21.1 (packaging) and TASK-21.2 (history fixtures), not numeric26/27 guessed in prior note. These actual IDs are authoritative.

Staging slice delivered and parent independently ran115 staging/capture/discover tests GREEN, hashes match staging288573d5/testcd42fbfd. Implementer explicitly reports code-first development, NOT requested TDD; preserve that process deviation honestly, no retrospective RED claim. Tests did find/fix tree-digest and jar-time-budget defects. Bound compliance review will inspect staging path/digest/cleanup contracts; adapter integration not yet wired. Packaging/history fixture workers remain independent and in progress; no unstable whole-suite run.

Staging compliance review NOT APPROVED: nonUTF8 member path strict encoding can escape after partial writes and leak stage/base fds. Also accept immediate fixes for post-mkdir stat outside cleanup guard and OSError failure dropping collected observations. Fix name validation before member writes and comprehensive resource cleanup/error normalization, preserve unrelated-file ownership guards. Reviewer pytest detour not needed; supported plain unittest remains command. Parallel packaging/history work unaffected.

Classifier recovered enough for SendMessage and this tracker call. Delivered previously blocked staging correction to original owner: no deletion by stage name before inode identity is established; retain partial path and close descriptors, with replacement-race regression. Last reported staging51 tests not independently rerun; wait stable corrected state. Other parallel task statuses will be recovered without restarting work.

Recovered final staging fix output; parent independently ran122 staging/capture/discover tests GREEN and verified staging09bbb70edc5a9377d149d26f56721f537bbac658392261c46938f689ac5d1d0f/test9aabdb606d59316da2c67ea49121621b59e71b2aa8164943c6ec2f863f055192. Unverified-name rmdir removed, identity-stat failure leaves named partial with fd closure; replacement-race regression added. Original nonUTF8/general cleanup/observation fixes included. Same compliance reviewer final targeted recheck requested; no staging approval yet.

Staging compliance reviewer addcee90385f340bb APPROVED final09bbb70e/9aabdb60 after independent52 tests and original nonUTF8 repro. All cleanup/observation/fd gaps closed. Bare stage_inputs owns resources until cleanup by contract; preferred contextmanager closes on exit. Unverified inode leaves named partial deliberately. Begin bounded staging quality review; other parallel packaging/history fixtures reviewed, parent integration still pending.

Staging quality reviewer adf935965ea3286c4 found two verified issues despite52 GREEN: vanished ledger member/stage leaks raw OSError from cleanup and can mask context body exception; source directory fds leak when _mkdir_staged fails before finally starts. Accept targeted cleanup-error normalization and guard placement fixes with deletion/body-error and both mkdir-failure fd regressions. History implementation continues independently; staging not approved.

Parent independently verified staging quality fixes:126 scoped tests passed, stagingf72e33c59ba5957f2bd079e9cc2e19fe77e7884cc9c2babb914cea251966f076 tests67228d6cca423d358af20bcc5ecbb1f7279f991033d10e9abb0cd8e2b8e86aea. Cleanup errors normalized, context body exception preserved with note, source fds guarded across mkdir failures. Same quality reviewer targeted final recheck; no staging approval yet. History worker continues separately.

Staging quality reviewer adf935965ea3286c4 final APPROVED f72e33c5/67228d6c after independent56 tests and both original exception/fd reprobes. Both staging slice reviews closed; retained ledger/interference boundaries verified. Next integrate staging with reviewed helper runner and offline package, independently of ongoing history.py owner.

All TASK21 slices implemented; parent integrated warning-enabled unittest full648 tests GREEN using explicit MDI_TEST_DEPENDENCY_DIR, no ResourceWarning (expected duplicateZIP warning only). Final shared gates launched once across TASK21+child final artifacts: /code-review high DIRECT no fanout (also preflight second-stage quality), /simplify four bounded angles. Do not rerun separate gates per child on identical artifact set; record actual final coverage/results. No complete product/CLI claim.

Integrated simplify altitude findings applied: removed unused seen_before parameter from _classify_pair; centralized refusal-record shape in private pipeline helper. Parent53 history+pipeline tests GREEN; no public API/semantics change intended. Final integrated reviewer must receive current hashes; remaining simplify angles pending.

Integrated high-effort review returned5 verified defects (no approval): P1 classpath component containing platform separator can make JVM load an unlisted unhashed jar; P1 Linux // spelling bypasses staging-base outside-checkout overlap check; P2 helper input kind list/dict leaks TypeError; P2 top-level max_dirs refusal leaks already-open source fd; P2 shared seen_before reintroduction state conflates distinct POM modules. All assigned to original owners with disjoint files and focused real regressions. Do not weaken safe-path/pinned-classpath provenance or classify partial green tests as complete.

Parent independently verified two integrated runner fixes:64 runner+pipeline tests GREEN. Raw and canonical classpath components reject platform separators and wildcard expansion before execution/hash-boundary confusion; real-JVM exploit regression included. Malformed kind types fail structured, not TypeError. usage793ee225b1fd3865a0a1aac9524cf12d69612a29188e8b509ca4f79b4c443c16/tests4d8276ab7145056aec0289cd4d99b5713788f1202851aa6fc85e43062b1dda1e. Staging final stable result still awaited; no full combined approval yet.

User requested continuation. Parent current staging file differs from earlier65? actual SHA25665dedf60d0cd73b36bcf5d4688b6858ae08cc59c11b5dc1db78ece0016b75c81; scoped68 staging tests GREEN. Await implementer stable ownership/result confirmation before final review; no assumed completion. Requested missing existing simplify reuse/simplicity/efficiency outputs directly, no new review fanout.

User asked proceed. Parent full warning-enabled unittest run exceeded120s command limit and exited143 before summary; NOT a passing run. Started one verbose bounded600s background run bdmgy16jy to locate slow case and obtain complete evidence; no network/Maven, no code changes or repeated blind retries. Current scoped staging68 test result remains valid only for that slice.

Verbose integrated rerun bdmgy16jy completed exit0:658 tests in78.769s, OK; parent read output summary and verified0 ResourceWarning occurrences. Earlier120s timeout remains unexplained (no claim root cause fixed or isolated); no failing test in completed rerun. All current fixes ready for exact-state final review once stable manifests confirmed.

Final high-effort integrated reviewer APPROVED original5 fixes with independent230 scoped tests and original regressions. Parent mechanically reproduced full-scope manifest SHA256 d4e72178106f69b88a5d19e7810a96060e7711332f7044e514911f5ef0d70698 exactly, binding actual current files. Parent full658 GREEN already recorded. No residual original/below-cap findings. Integrated simplify missing reuse/simplicity/efficiency outputs still must be dispositioned; no TASK21 Done yet.

Final simplify reuse/simplicity results received. Shared Java fixture consolidation tracked as child task (assigned ID authoritative); not essential to runtime correctness. Private import coupling from usage to six Git internals SKIPPED: identical small helpers alone does not justify introducing a Git dependency into standalone usage; current distinct execution semantics intentionally kept. No general runner extraction. Dead history parameter/refusal record already fixed; one redundant packaging sort accepted for immediate removal. No recommendation lost.

Removed only redundant intermediate sorting in write_helper_jar; final canonical entry sort retained. Parent13 packaging tests GREEN incl deterministic builds. Shared Java fixture recommendation tracked TASK-21.8; Git-private-helper import skipped with boundary rationale. Final reviewer notified of one-line packaging delta; final efficiency response still pending.

Final builder-only delta reviewer APPROVED; parent independently reproduced updated full-scope manifest b6667adb6c390cc65ecbf302b823f158b44073d540a85d96a4718ad69a1c8ba2 matching exact approved files. Reviewer13 packaging tests GREEN, all other files proven unchanged. Review gate remains satisfied; final simplify efficiency result outstanding only.

Final simplify efficiency report received/applied: helper maxTotalClasses parsed once before analyze loop; license extraction lazily opens at most one ZipFile per dependency, closes via ExitStack, preserves vendored-only no-open behavior and checksum checks. Parent29 helper/packaging tests GREEN. All four simplify angles now dispositioned (fixture consolidation TASK21.8, private Git coupling skipped). Current tiny helper/build delta needs final existing-reviewer recheck; review gate reopened pending exact-state approval. No product completion yet.

Final efficiency deltas independently APPROVED by high-effort reviewer; parent reconstructed full-scope manifestd59c0bc1495d6463c4be765f7e3ba6574146c5d2ebfa3815aad693f9d60f7eb3 exactly. All simplify recommendations resolved/tracked, no residual code-review findings. Final full warning-enabled suite bszugo11z running with600s cap; wait actual output before marking parent/children complete. Reflect: TASK21 usage/history/packaging bounded implementation, not full CLI/task22/23 or large archiveTASK25.

Final full-suite bszugo11z output read:658 tests in71.639s OK exit0, ResourceWarning enabled and none emitted; expected duplicate-ZIP fixture warning only. Exact approved manifestd59c0bc1495d6463c4be765f7e3ba6574146c5d2ebfa3815aad693f9d60f7eb3 matched. Shared /simplify and /code-review high gates satisfied across parent and child implementation artifacts; technical usage/history/staging/license packaging scope complete. TASK21.8 optional fixture consolidation remains tracked nonblocking, no broader product/CLI or TASK25 claim. No commits/push.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
