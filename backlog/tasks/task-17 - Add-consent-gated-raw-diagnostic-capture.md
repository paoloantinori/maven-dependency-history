---
id: TASK-17
title: Add consent-gated raw diagnostic capture
status: Done
assignee: []
created_date: '2026-09-10 17:04'
updated_date: '2026-10-22 20:08'
labels:
  - follow-up
  - diagnostics
dependencies: []
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Implement optional bounded owner-only raw diagnostic capture only with separate explicit user consent. Initial implementation retains structured evidence only. Never describe local raw output as sanitized or upload it automatically.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Separate raw-capture consent and sensitivity disclosure are required.
- [x] #2 Tests cover byte limits, permissions, retention, and no default capture/upload.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Local design synthesis includes separate bounded raw-capture operation consent and owner-only retention. No raw output capture enabled by default and no approval generated. Implementation follows reviewed consent design, not generic Maven approval.

Invariant review required B: post-hoc raw capture of already-finished operation contradicts no default raw retention. Recommend pre-registered separate approval consumed before any tee at source execution start, both approvals exact-bound/single-use; no retention while awaiting consent. This is design correction requiring user plan approval, not authorization to capture now.

Revised extension design a31c70e6211ba9402cdda22a76f77fe12cb8fad2936f870e1af670c4f9e44514 now specifies pre-registered capture consent, atomic source+capture consume before tee, no raw retention pending/denied/absent, posthoc refusal. Parent verified artifact hash; invariant reviewer targeted check pending, user design gate still required. No production implementation.

Independent invariant reviewer a89d04b48fba46f59 verified exact extension-plana31c70e6211ba9402cdda22a76f77fe12cb8fad2936f870e1af670c4f9e44514 and all original amendments. APPROVED_FOR_USER_REVIEW only: preregistered exact-bound dual consent atomic before tee, no pending retention/posthoc capture; asymmetric shaded multiplicities/nonempty correspondence remain candidate, missing revisions unordered with issues. No new findings. User design approval remains ungranted; do not implement new operation kind from reviewer approval.

Next-phase consent design approved via ExitPlanMode: preregistered separate raw-capture approval consumed atomically with source approval before tee; no posthoc or pending retention. This authorizes implementing the gate, not enabling any real raw capture without per-operation user consent.

Started approved preregistered dual-consent pure lifecycle slice raw_capture_consent.py/tests, NO raw capture/file writes/execution/network. Closed operations schema shared modifications require explicit owner coordination, no spoofed kind bypass. Two host approvals atomically consumed before future tee; existing no-capture paths unchanged. Parent reviews prior to executor/CLI integration.

Confirmed shared operations seam rejects raw-capture; parent authorized minimal explicit kind/closed-scope dispatch extension within previously user-approved design, expanding owner to operations.py/test_operations. Canonical operations.propose retained, no direct RunStore validator bypass or fakekind. Implement atomic pairconsume with synthetic actual-shaped approvals and all-or-none revision; real executors/CLI/raw writes remain absent. Check allkind dispatchsites for failclosed behavior.

Parent inspected raw_capture_consent actual artifact ee314fa0/tests b4d99547;63 validators+operations tests pass but operations unchanged9542fecb and module explicitly no registration/atomic consume implemented. Shared integration authorization had been sent; do NOT treat green pure tests as atomic lifecycle delivery. Owner asked precise current status/ownership release, no rawfiles/executors enabled.

Fallback confirms operations still2kinds and no atomicconsume, prior owner not delivering shared extension. Reassigned bounded completion to a6f5382a7aa3a0912, previous owner told stopwrites. Must implement actual registeredkind+pairconsume and block single-consume bypass, not more validator-only tests. No raw bytes/realexec authorized.

Actual shared registration+consume_pair now on disk; parent203 rawconsent/operations/Maven/advisory tests -Werror pass4.577s. rawconsentd1e757abc6845d1cd85526990962174e2e3cf53856a7d3e4b650197e7b21966b/ops997d7f76ba65aa6204345ec018e456312a8ababcbaf4cc007645a2c844e4b365/testraw18da9d59. Canonical rawkind dispatch/registration and singleconsume blocking present. Needs independent transaction/approval-binding review, no rawbyte sinks/paired executors yet. No realcapture authorization inferred.

Recovered final atomic consent review a637517 APPROVED rawd1e757ab/ops997d7f76 after203tests and independent twoapproval/scope/tool/stale/atomicreopen/replay probes. No rawIO yet; source pairedexecutor/storage/CLI remains. Actual canonical registration delivered, no bypass. All scoped recommendations resolved.

Raw sink worker verified shared RunStore seam: approved raw-UUID.bin after atomicconsume currently makes store.load unexpected-member. No files changed or bypass. Required canonical raw-file admission validated against latest full report chain, unique capture registration and actual same-revision source/capture consume, private regular nofollow capped file; missing file allowed crashbeforecreation. Historical revision reads must use latest-chain authorization for existing raw members without merging old evidence. Parent authorizes narrow shared validator extension within approved rawcapture design before implementing sink.

Recovered raw sink+RunStore shared seam; parent247 rawstore/runstore/rawconsent/ops/report tests -Werror pass2.123s. RunStore canonicalrawnames latestchain actualsameconsumerevision/startedattransition/modecap/no hardlinks validated w/o rawreads; historical semantics preserve no-raw oldloads. RawCaptureSink holds writerlock whilewriting, must close before finalization; exacttoolhash/exclusivefile/boundedprefixdigest no fullstreamclaim. Independent authorization/lifecycle review mandatory before executor integration.

Recovered actual raw sink/latestchain review ab31a416 APPROVEDrawstoreaed8ba84/runstore9f491fb5 after247tests and independent lock/constructor/partialwrite/historical/splitconsumption/primaryexception probes. No residual/simplify recommendations. Rawfile sink and canonical dualconsent ready pairedexecutor integration, no realcapture/userdata enabled. WholeTASK17 still open.

Started two disjoint pairedcapture executor integrations: Maven streamingtee and advisory boundedreceivedbody, each explicit optionalcaptureID+approval and atomic dualconsume; default nofiles unchanged. Raw sink writerlock must close before evidencefinalization, both states consumed on failure/no retry. Syntheticchildren/inmemory transports only, no realMaven/HTTP/userdata capture. Shared lowerconsent/store approved untouched.

Parent167 advisoryraw/executor/transport/rawstore/consent tests -Werror pass4.164s executor0d9736ad34e513333d9cb809fc5bac535b0f61a4ae14f8f71cb96ea1dc5e471f/testraw14485a68c73e731169140dee92a29de9b35498e1a53b7d207bafb3a4e759f1ae. Dualconsume before sink/request, close before publication/finalize, no defaultfile/rawJSONleak, returned boundedbody only (no inaccessible partial transport reads), separate finalize crashwindows explicit. Independent pairedexecutor review next, no realHTTP/actualusercapture.

Parent192 Mavenraw/executor/rawstore/consent/operations tests -Werror pass4.674s Mavenexecutor ddeed2e7c371e8b52d7b7d94da37ae219b5555abaf4b2d3bcb14ce3fdd056ebd/testc4d9795dcac0b8bdab31a22a3790d06a49f9882a796055dc8f5f551cfaf49065. Real syntheticchild teesonly after dualconsume and sinkopen; independentrawcap/no default files/no retry, sink closes before publication, failure/interruption metadata explicit. ParentIO synchronous not hardFS cancellation, combined readorder not originalinterleaving, streamshaNone. Independent pairedexecutor review next; no realMaven or user rawcapture.

Paired advisory reviewer ac1d7a9 final APPROVED0d9736ad/14485a68 after167tests and independent dualconsume/lock/exactcap/truncation/malformedbody/primaryinterrupt probes. No findings/simplify notes. Captures returnedbody only, separate finalization crashwindows explicit. Mavenreview and CLIpairedintegration still pending, no wholeTASK17 closure.

Maven pairedcapture reviewer aaa19580 NOTAPPROVED P2: sink.open KeyboardInterrupt after exclusivefile creation (directoryfsync) carries raw_capture actualpath/retainedTrue, _run_captured reads only capture then overwrites raw_capture pathNone/retainedFalse. Actualfile remains with bothconsents spent, recovery lost. Preserve both supported sinkmetadata attributes before finalattach; actualfsyncinterrupt regression must assert exactpath/retained/spentpair/lockrelease.192tests passed missingcase, no approval.

Parent194 Mavenraw/executor/store/consent tests -Werror pass4.667s P2fixae940b30e46dea51a55990789814992723457c4a82868bfde2d71c5467abb35c/test11a73ffb8040ab47703c18ffb97e96fa40e2741696fc7d925d913da8b85ee74c. Actual fsyncinterrupt retainedpath and primary preserved, recoverymetadata bounded/exactapprovedpath only; unknown defaults conservative. Same reviewer original repro recheck pending, no taskcompletion.

Mavenraw same-reviewer aaa19580 final targeted APPROVEDae940b30/11a73ffb; original realdirfsync interrupt retains actualzero-bytefile/exactpath/True, sameKI, bothconsumed/no spawn/lockreleased.9 metadata normalization probes correct bounded approvedpath/types/fallback/noextras;2new regressions pass. No residual/optionalrecommendations. PairedMaven/advisory lowerexecutors now approved; CLIintegration/finaldocs/sharedgates remain.

Parent148 rawcaptureCLI/ops/boundaries/consent/store/Mavenraw/advisoryraw tests pass with ResourceWarning-errors56.615s CLIcc1d027c726c51922cadf0926c43675d974a4cddd4839dfb102c47f76f7a99c0/test9c11437b0bd80caf0de857e805209c4170ac9627ddc0b28cb132e1dd76f82914. Explicit capture proposal/sourceID/cap and dual approvalfile execution, noautoassociation/retrospective/directcapture, metadataonly response/recovery. Independent final CLI review/docs/sharedgates pending; no realMaven/HTTP/usercapture.

Final shared /simplify dispatched four read-only angles and /code-review high invoked for integrated raw capture, offline forge importer and no-selection CI/MCP evaluation. Raw capture reference linked from SKILL/CLI/permissions; stale blanket no-persistence wording corrected to no default retention plus separately approved private bounded capture. Full configured suite bybam3xr3 running with unraisable hook and GC. No real source operation or user data captured.

Full configured unittest run bybam3xr3 passed 1807 tests in 399.302s, exit0, explicit unraisable hook plus GC count0. Only expected duplicate-ZIP fixture UserWarning. Separately added raw-capture documentation contract test: all6 skill-contract tests pass. Actual propose/execute help confirms documented capture flags; raw reference SHA256 7dc2f02d74fd02f98f4c1ffe050380f4726d3dd39e77a33680e7797e46f4738b. Final review/simplify results pending; no completion claim.

Recovered missing raw CLI reviewer final report: REQUEST CHANGES P2 advisory_executor finalization conflicts after capture evidence commit omit raw_capture immediate recovery metadata. Actual copied CLI/mock transport reproduces exit3, existing34-byte file and revision6 evidence, but no metadata in error JSON. Preserve bounded capture envelope through BOTH finalization calls and regress their conflicts/CommitUncertain. Also simplify efficiency recommends reusing initial precheck report for source+capture instead of redundant load; preserve consume_pair fresh authority and locked sink checks. Reuse/altitude angles no recommendations.

All four simplify angles dispositioned: reuse/altitude none; efficiency fixed redundant advisory precheck load while preserving consume_pair fresh authority and locked sink authorization. CLI review P2 reproduced by new copiedCLI regression: both finalization rev6/rev7 failed pre-fix with KeyError capture (initial wrong test-class command was a separate invocation error). Existing metadata exception guard now surrounds evidence commit and both finalizations. Scoped179 tests pass97.175s in bhuh66gg3, ResourceWarning errors. Same reviewer targeted check and final high review pending; final fullsuite bpokpcu57 running.

Final full configured suite bpokpcu57 passed1809 tests404.900s exit0 with explicit unraisable hook+GC count0; only expected duplicateZIP UserWarning. Original rawCLI reviewer targeted APPROVED advisorya7d4fb49/CLIcc1d027c/test29773520 after both rev6/rev7 recovery regressions passed3.987s. All simplify recommendations addressed. Final integrated high review still collecting its finder reports; no task closure yet.

Integrated altitude finder candidates received, not yet verified: (1) crash during long sink-held writer lock leaves stale lock, recovery undocumented; (2) Maven post-run publication/finalization errors replace actual sink metadata with conservative fallback; (4) CLI has no denial route after pending capture registration (documented fresh-proposal workaround); (5) sink recovery whitelist duplicated, hypothetical drift; (6) unauthorized raw-named file fails all reads, tamper recovery undocumented. All retained for integrated verifier disposition; do not weaken exact consent, locks or fail-closed membership based on candidates alone.

Integrated cross-file finder candidates received for verifier disposition: C1 Maven proposal custom input/output prestate caps mismatch execution hardcoded8MiB re-observation, unchanged16MiB target cannot execute; C2 advisory sink write/close errors after successfulPOST mislabeled transport-failure and discard received advisory evidence; C3 historical read two directory enumerations race new authorized rawfile and spuriously reject; C4 expected revision mismatch surfaces RunConflict instead of consent-specific refusal (failclosed, no authority hole). First two finder-probed, latter code-traced; parent has not independently verified. Finder reported concurrent-edit tool-digest flake during prior source changes, not final stable suite failure. All candidates tracked, await integrated disposition.

Integrated reviewer reports eight-angle collection complete and independent verification underway. Additional candidate: authorized Maven output path inside run directory can introduce a disallowed member and make run unreadable. Recorded pending reproduction/disposition alongside historical raw-creation race, Maven recovery metadata loss and advisory sinkfailure classification. No fixes or completion claims based on unverified candidates.

Behavior finder candidates queued for independent integrated verification: B1 sink-open writer-lock conflict after paired consumption wrapped as source/capture failure and CLI exit0, despite concurrent-writer exit3 contract; finder probed both executors with one-shot lock acquire failure. B2 suspicion unconfirmed-to-partial status on unopened sink may mislead status-only consumers; B3 suspicion differing raw-diagnostic-capture versus raw-capture category names hinder common filtering. No parent confirmation yet. Finder443 pytest tests with plugin autoload disabled are supplementary only; configured unittest1809 remains parent proof. Clean reported invariants preserved; no source changes.

Read independent verifier artifacts mdi-verify-coordinate.json and mdi-verify-altitude.json. Confirmed A1 invalid known advisory coordinate whitespace/oversize maps to unexpected exit4 rather than input exit2, precommit no state effect. Confirmed altitude1 stale sink-held writer lock after hard crash blocks documented same-run retry; fix recovery documentation or explicitly approved repair, never auto-clear. Refuted altitude4 no-denialCLI (documented new-proposal workflow), altitude5 whitelist drift (deliberate bounded security boundary), altitude6 strayraw tolerance (intentional antitamper). Retain altitude6 one-line warning recommendation for documentation. Integrated verification of remaining candidates blocked by classifier outage; no final approval.

Confirmed A1 fixed minimally: CLI catches AdvisoryError only at advisory_proposal boundary and preserves sanitized rejection reason with exit2. New copiedCLI regression whitespace/4104char versions failed both pre-fix (exit4), then scoped39 tests passed13.184s bqzv9uqyp. Stale-lock recovery documentation added: same-run writes blocked after hardcrash, never autoclear; require exclusive-owner verification beyond PID, private backup/inspection and explicit authorization for removing only stale lock, reopen validation/no consent replay. Added warning against arbitrary raw-named files. No locking/consent behavior change; integrated reviewer targeted recheck requested. Earlier1809 fullsuite predates this final CLI edit.

Read newly available independent verifier artifacts: outputcap A2 confirmed library-only caller-raised capture cap not threaded into executor re-observation; transport C2 confirmed returned advisory body followed by local sink failure is falsely labeled no-response transport-error. C4 verifier reports documentation/error-taxonomy mismatch but also proves RunConflict enforces exact same-snapshot publication; retain conflict behavior and clarify doc only, no authority change. Simplification verifier reports6 real duplications, no current bugs. Dispositions: disclosure tuple duplication accept bounded local cleanup; execute error wrapper retain documented/pinned execution-refused context; raw registration uniqueness and proposal checks retain current separately reviewed boundaries for this fix set (unification changes error precedence, no defect); ID allocators outside rawcapture scope retain collision-checked append-only behavior; per-module timestamp/numeric helpers retain established self-contained convention, no broad limits framework. No deferred implementation promised for declined cleanups.

C2 fixed: ordinary advisory sink write/close failures preserve mapping of already received bounded provider body, source and capture outcomes independent; interrupts still propagate, no retry. Two existing tests strengthened and provedRED pre-fix; added actual advisory+continuation preservation case. Combined135 executor/transport/rawCLI tests passed41.693s b3yustlf6. C4 doc clarified consume_pair fresh snapshot plus RunConflict stale refusal; no consent error-class change. Simplify disclosure constants now spliced from existing tuple, byte-order preserved. Integrated targeted review requested; full suite must rerun after all confirmed fixes.

Independent recovery verifier A3 CONFIRMED: Maven loses precise closed-sink metadata on each evidence/source/capture publication failure and substitutes unknown despite verified retained file. Preserve bounded sink result across all3 commits, retain conservative fallback for earlier unknown-state failures. Existing test pins imprecise status and must be strengthened, not removed. Parent now fixing this confirmed issue.

Independent race verifier A5 CONFIRMED deterministic historical-load race: raw-presence decision and later raw enumeration use different directory snapshots; new legitimately authorized capture can be falsely judged against old history. Immediate retry succeeds, no failopen. Also latest discovery can race authorizing commit. Fix must use one coherent directory membership snapshot or bounded reread without weakening latest-chain authorization; parent implementation pending.

Reuse verifier dispositions: actual ECHILD false-success in usage/Git runners gets separate high-priority task immediately (not lost as generic extraction). Started-audit schema and raw-name grammar coupling are current-equivalent future-drift observations; retain independently rederived storage checks, no abstraction change. Capture orchestration differences assessed through concrete A3/C2 fixes and pending category/status verifiers, no shared orchestration framework. JSON decoder differences fail downstream exact schemas, milliondigit claim refuted; retain deliberate separate trust-boundary parsing. No deferred cleanup promises.

A5 fixed: runstore._read_chain now derives the revision chain AND raw membership from ONE directory snapshot (_scan_names over _list_names); a raw file created mid-read is invisible to that read and authorized by the next one, instead of being judged against a stale revision view and falsely rejected. _check_raw_members takes the snapshot raw names instead of re-listing; _discover_latest is a thin wrapper. Fail-closed preserved: an unauthorized canonical raw name present in the snapshot is still rejected. Regression tests in tests/test_runstore.RawSnapshotRaceTest (mid-read creation via _read_report seam, stranger-name rejection, authorized historical read); full module green. Verified CONFIRMED race from /tmp/mdi-verify-race.json mechanism now closed; remaining TASK-17 confirmed item is A2 outputcap.

A2 fixed: maven_proposal pins both caps into the record (tool_identity.input_max_bytes always; output prestate carries max_bytes when a target is pinned). execute_maven honors the pinned input cap over its parameter for pre-consume re-observation and downstream rechecks; _observed_tool_binding re-observes the output under the pinned cap and re-attaches it so deep-equal consent comparison stays exact; post-run observation uses the pinned cap. Old records without pinned caps fall back to defaults (backward compatible). Regressions PinnedCapTests: 10MiB present target with 16MiB proposal cap executes (was permanently unexecutable pre-fix), 10MiB input with raised input cap executes, post-run 10MiB output observed present at exact size instead of unknown. Consumers green: 188 tests across maven_executor/raw_capture/consent/cli_operations/raw_capture_cli/local_runner_identity. TASK-17 now has both confirmed defects (A2, A5) fixed; remaining: integrated final review of the two fixes plus full-suite gate.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Consent-gated raw diagnostic capture complete. Preregistered capture proposal bound to a still-proposed source; two real single-use approvals consumed atomically in one revision before any byte is written; sink holds the writer lock across capture and closes before evidence publication; bounded 0600 run-local files with stored-prefix digest and honest recovery metadata on every failure path (advisory finalization preserves mapping of already-received bodies; Maven publication failures preserve precise sink metadata). All confirmed integrated-review defects fixed and verified: advisory coordinate rejection exit code, stale-lock recovery documentation, C2 sink failure classification, A3 Maven recovery metadata, A5 runstore single-snapshot race fix (one directory snapshot decides revisions and raw membership; mid-read capture creation invisible, fail-closed preserved), A2 proposal-pinned caps honored at every re-observation. Gates: simplify dispositioned, integrated review of the final fix set APPROVED (364 tests plus adversarial probes; LOW finding applied), full suite green (58 modules/1832 tests).
<!-- SECTION:FINAL_SUMMARY:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
