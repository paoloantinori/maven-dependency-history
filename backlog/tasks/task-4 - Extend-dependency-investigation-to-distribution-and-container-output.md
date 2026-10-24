---
id: TASK-4
title: Extend dependency investigation to distribution and container output
status: Done
assignee: []
created_date: '2026-09-10 14:40'
updated_date: '2026-09-12 22:48'
labels:
  - follow-up
  - artifact-analysis
dependencies: []
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Track the wider packaged-output goal deferred from the initial local-checkout JAR/WAR experience. Cover distribution archives and container images, distinguishing application dependencies from container-provided libraries and other software. Preserve evidence provenance and permission boundaries. Scope and implementation require a later design decision.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Define supported distribution and container formats and explicit unsupported cases.
- [x] #2 Locate Java dependency copies in supported outputs and correlate them with application build evidence where available.
- [x] #3 Distinguish current checkout resolution from actual packaged contents and identify discrepancies without inventing attribution.
- [x] #4 Require appropriate permission before image pulls, builds, or other external operations.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Local design synthesis now covers candidate initial distribution/OCI-layout support with no image pulls/builds. Existing task requirement for later scope/design approval remains; continuous-work instruction is not external-operation consent.

Started approved first local distribution tar/tar.gz inventory slice distributions.py/tests only. Bounded readonly streaming/no path extraction, Java nested artifact capture with explicit provenance, no OCI layer composition yet. No image pull/build/network/Maven. Reuse reviewed archive pipeline where possible; parent review/integration/gates later.

Parent46 distribution+archive reader tests -Werror pass0.032s new distributionsa59f6e04a6124b59ea3f21c528aa0ccdc5429fc2b4c43080efd9769ffa0b8960/tests30336e0b5d82972e15f9ce60881ab555df7ece55411b65e58ee2723e1f3cc872. Local tar/targz descriptor streaming/private spool nested JAR observations delivered, links/devices/traversal/PAX/GNUlong/sparse unsupported explicit. Worker/deadline/response cap/quota/OCI layer+CLI integration missing, no wholeTASK4 completion. Independent header/gzip cumulative budget/identity/lifecycle review next.

Distribution reviewer a4cbc26 NOTAPPROVED2 independent tiny repros despite46tests: size from firstfstat and baselineidentity fromsecond allows appendbetween => complete digest originalbytes not actualfull source; use one observation or compare before capture. Nonzero directory size drained unlike tar type semantics can consume next JAR header and falsely complete inventory; reject invalid nonregular size or implement verified type-specific semantics. All assigned owner, no extra simplify recommendation. Worker/CLI/OCI integration still pending.

Parent49 distribution+archive tests -Werror pass afterF1/F2 source471fdf9555ab51a7db693f87cd6e16c04423534ab7618489779a791cedbeb2ab/tests56ef0260ed5b1f8e0f0419e5d34c791aa4e7de0bd7ce0c80e47929eeb6293e9a. Single stat identity+size, postgrowth invalidation, nonregular size explicitstop before payloaddrain. Same reviewer original tiny repro check pending; no taskcompletion.

Same distribution reviewer a4cbc26 final targeted APPROVED471fdf95/56ef0260 after49tests and original appendedsource/directoryhiddenheader probes. Sourcechanges partial noidentity, nonregularsize stops predrain; no residual/simplify notes. Local reader approved only; hard worker/OCI layer/CLI integration still open.

Parent73 OCI/distribution/reader tests -Werror pass0.057s OCIc53b9de435e633123e0d4e4e7a58ef48c8ce606162ecd612fcc6982a35b85ffe/test7dd3d6a8da283312d916a581e955f213bb6ec2b6add56ddce65824856c299908. Local selected manifest/layer inventories only, synthetic fixtures, whiteouts observed NOT merged finalfilesystem; no application/container realm inference. Strictblobdigest/size checks reported, supervising worker/memory/responsecaps and CLI still missing. Worker official OCI1.1.1 docs researched claim awaiting source citations if loadbearing, no real image acceptance.

OCI reviewer a3a7801 NOTAPPROVED cleanup ledger defect: inventoryresponse cap before appending layer or source-mutation invalidation clears layer result containing retained-spool paths, leaving undisclosed files. Two actual tiny layer/delete-refusal probes reproduce; maintain separate bounded top-level cleanup obligations populated immediately before evidence validation/retention, preserved on invalidation/interrupt.73tests passed but missing cases. Owner assigned, no other simplify findings.

Parent77 OCI/distribution/reader tests -Werror pass0.065s cleanup fix OCI3d636d7b6f371b1821da60591a8954a620bb4a3ad13c808f0b7c435fd3fcb720/tests3d691051538d1d3cfbd8cfb6c2c67749aed4c9becb47ae60990b91fbc3bced56. Separate bounded cleanup ledger preserves retainedpaths across inventorylimit/sourceinvalid/interrupt; saturation explicit stops furtherlayers with recoveryroot marker. Same reviewer original repros pending; no worker/CLI/finalfilesystem claim.

OCI cleanup same-reviewer a3a7801 final targeted APPROVED3d636d7b/3d691051 after77tests and original real retainedspool lowcap/sourcechange probes, plus saturation and exact interrupt preservation. No residual/simplify recommendations. Per-layer inventory slice approved only; worker supervision/CLI integration and final merged-filesystem qualification remain task work.

Parent79 distributionworker/distribution/archiveworker tests -Werror pass7.604s source8b58b368800b1b0d275ce30296c3af6957211dca9f643b7cf18058a5ad526b23/tests51805f3e88b5ed0f68991f6b460a6f8ef573d828b596e96a5f9244f3df8423bc. Tar/targz isolated deadline/resource/protocol+independent cleanup ledger delivered, OCI supervision explicitly absent due path-only lowerAPI. Independent review needed, no CLI/currentimage mergedfilesystem claim.

Distribution worker same reviewer aad642a final APPROVED8b58b368/51805f3e after79tests and independent limits/partial/empty/unsupported members/dripdeadline/reap/fd probes. No residual/simplify suggestions. ParentIO/Popen/JSON cooperative and2s grace/no-descendants limits explicit; approval tar/targz worker only, OCI supervisor+CLI integration not covered.

Parent86 OCI/distribution/reader tests -Werror pass0.139s descriptor API source184009edfdade667214362ef9de93c91f090c9ad00c489333df92b0d151966bb/tests3fb91d1ffa5b57e0c742edde6d111d703b51fc850489063c652ecddfc1566a0f. New caller-ownedreadonly directoryfd anchoredblob reads logicalpath not reopened, original path API behavior retained, rootrename ctime handling qualified. Scratch/checkout remain pathcontracts, supervisor/mergedFS/CLI not delivered. Independent targeted fd-lifecycle/rootmutation review required.

OCI descriptor API same-reviewer a3a7801 final APPROVED184009ed/3fb91d1f after86tests and independent rename+replacement/originallayer mutation/caller offset+fd inventory probes. No residual/simplify recommendations. Descriptor seam ready trusted worker integration; no CLI/finalmergedFS proof delivered.

Parent82 OCIworker/layout/distributionworker tests -Werror pass5.162s new OCIworker0ffedd33a4f04129c9e51ad7702959ecb8935d2e1e43a4311a455946466e04c8/testb5787368f441e82172f83b45e169d195c5d2756c9630b436ecf32be8818a46ed. Inheriteddescriptor isolated inventory/strict response/cleanup ledger delivered, no finalmergedFS or CLI support. Independent boundary review next, parentIO co-op2s grace/no-descendants assumptions explicit.

Started pure OCI visible-member composition slice oci_visibility.py/tests, no extraction/workload/CLI edits. Must verify whiteout/opaque/replacement semantics against source, preserve uncertain/unsupported layer coverage, never infer application/base ownership. Supervised OCI inventory already independent review pending; composition not active until reviewed.

Parent57 OCIvisibility/layout tests -Werror pass0.053s visibility78f274eb1a8fa4bd394ed9f81fd78f252841d83cd1b72ddd3e6f24230c914f9d/testc38b8a7d8447e0797ab86559634dab1cf57ab4014cdd49c9f54d6e46538bfd17. Pure regularfile/directory visibility computation lower-only whiteouts/opaque/replacement, uncertainunsupportedlayer=>globalpartial nofinalJava claims, not runnable mergedfilesystem. Independent spec/ordering/bounds/candidateassociation review next; caller inventory authenticity not assumed verified by pure mapper.

OCIvisibility reviewer adcaf80 NOTAPPROVED2: reserved.wh. checked basename only, implicitancestor.wh.hidden/a.jar exportedcompleteJava though OCI forbids reserved directory names; all path components require safe classification, partial/nocandidates. NestedJava candidate validation ignores rootdigest/memberbinding and invalid archiveid/chain/status, forgedrejected/mismatcheddigest stillcompletecandidate. Validate nested source/root linkage and chain/status before export, invalidatedpartial noclaim.57tests pass but2actualtinyprobes reproduce; assigned owner no extra simplify notes.

Distribution CLI owner reports implementation complete in ownedCLI/newtests but combined verification still running;18focused tests reported not parentverified yet. Explicit tarformat/OCI manifest inputs, isolated workers/scratch lock+quota, candidate-only layer evidence and cleanup obligations. Respect CLI ownership until stable handoff; no premature acceptance or overlapping edits.

Parent18 distributionCLI+boundary tests -Werror pass13.172s stableCLI61e8ecd78748f96a5c052ea849e8b3b1935b7a4ffaa39e605732546a7d4f7039/testsbcf13b9ea085eb4797299732bf3dc271a7e2f4c387c50a224da5821eee06f0d8. Explicit oneinput tarformat/OCImanifest isolated inspections, candidate-only/no subjectmutation, separate cleanup ledger on commiterror/interrupt. OCI alwayspartial no mergedFS. Independent scope/quota/source lifecycle/privacy review needed; final additive recoveryroot tested18 not entire104 baseline, fullsuite later.

Parent61 visibility/layout tests -Werror pass0.074s correctedvisibilityecf77df657a7ad0c4f94712bbef5f9a45b70a9481e1b91391c2f6b8ec81cdb70/test784f4896f831ff33b8a8b7f070da7cd0d6a747049034d3ce00440dc96a6fe58c. Reserved ancestors partial, nestedJava root/member/hierarchy digest/status bound before exporting, invalid =>globaluncertain/nocandidates. Same reviewer original2probe closure pending. Still separate pure composition no CLImergedFS claim.

DistributionCLI reviewer a18718f NOTAPPROVED F1 cleanup rmdir catches onlyOSError, secondary BaseException masks original workerKeyboardInterrupt. SafeactualCLI probe remover succeeds thenraises differentKI -> outwardsecondary, original lost. Preserve activeprimary across all scratch cleanup, conservative metadata, finish ownedfd closes, no repeated removedpath action.18tests pass but missing identity regression. Owner assigned, no other findings/simplify notes.

OCIvisibility same-reviewer adcaf80 final targeted APPROVEDecf77df6/784f4896 after61tests and independent actualOCIreservedancestor/normalJAR/emptyJAR/forgednestedroot probes. No residual/simplify suggestions. Visibility mapper ready for supervised/CLI use, no standalone finalfilesystem extraction claim. DistributionCLI interruption fix and OCIworker review still pending.

Parent20 distributionCLI/boundary tests -Werror pass10.280s CLI11e1fd17ad1f90cc858c880ce649d12f3962d335595698b620fe3b2f11498ec1/tests68ed3e783dc297c0ce7ca7aec51a48ba18f40249b923c72a45ba4c4ac3f7e507. Activeprimary retained across cleanupBaseException, standaloneinterrupt metadata preserved, actualrmdir once/fd inventory regressions. Same reviewer F1targeted recheck pending.

Started OCI visible-member derived evidence CLI linkage using approved pure mapper, ownera9d5cc14 onlyCLI/distributiontests; preserve independent rawlayers/cleanup and sourcequalification, whiteouted candidates not finalvisible, unsupportedglobaluncertainty. No runnable filesystem or application/base realm inference.

OCI visibility CLI owner found valid whiteout lib/.wh.b.jar misclassified by suffix as empty JAR, causing layer-incomplete and conservative no visible candidates. Do not strip warnings or weaken visibility. Required lower-reader context-sensitive whiteout handling: OCI whiteout marker must not be parsed as archive; ordinary tar filenames must not gain OCI deletion semantics. Directory whiteout/opaque/replacement fixtures already exercise actual composition. Assign precise regression/fix after source inspection; task remains open.

Whiteout fix ownership confirmed no competing lower-reader writer. Explicit oci_whiteouts=False compatibility option, OCI opts in; safe zero-byte regular validmarker before JARsuffix parse, malformed partial. Worker validator kind dispatch references must be checked before integration, not assume new memberkind accepted. Lowerowner fourfiles only, CLI separate.

Parent attempted visibilityCLI verification50tests FAILED7fail13errors during lowerwhiteout edits. Targeted fulltrace confirmed distributions._inventory requiresoci_whiteouts but inspect_distribution_fd430 call missingargument; not merely guessed race. Lowerowner notified finish allcalls/propagation before stable handoff; CLIowner told no compensation/assertion weakening. Current whole suite notgreen, visibility integration not accepted yet.

Parent stablewhiteout lower91tests pass exact distributionsb4af6fb4/OCIa3f6895e/tests9135a342/9b87fc57. Cross-consumer68test run FAIL4+1: OCIworker whiteout response invalid-response and CLI selectedmanifest/resultNone/visibility lost. Confirms previously warned newkind protocolconsumer mismatch, not accepted stableintegration. Worker validator update needed contextualwhiteout validation (zero regularmarker), CLIjar-whiteout expectation must then become precise supported removal not oldbug partial. Full traces read, no silent test weakening.

Empty worker completion notice hid unfinished validator changes: parent49worker tests initially12fail. Parent took over two validator files, stopped ownerwrites; added explicit OCIcontext-only whiteout field/status validation, target/type/zero-size/digest checks, coverage mode binding and projection, excluded VALID markers from expected archive suffix set. Intermediate11fail traced via actualfd lowerresult to inconsistent-complete expectedarchives (not guessed). Final49 OCI/distribution worker tests -Werror PASS5.907s. Need same worker reviewer targeted newprotocol review; CLI old whiteoutpartial expectation update next, no taskclosure.

Parent50 distributionCLI/visibility/boundary tests -Werror pass17.011s after whiteouttest precise removal. Workers distributionf52c44c048be805621cbeda05df7d1169d5f58dbf6bbcdd19ca13f003ca42063/OCIf29bf3982d5721771bdc9784ac85caca7efa7cffa836947b37c6cbbd9f6982dd/testCLI4acfb5c3d83d0488fe075215f01e17448c636c901654d8fccb0b3047f12e0d34. Original layercandidate remains, visiblecandidate removed via exactmarker, completewithinrecordedlayers but overallOCIpartial/nonrunnable qualification. Need independent final whiteoutprotocol/visibilityCLI review before taskgates.

Integrated whiteout+visibilityCLI reviewer a11be73 final APPROVED exactdistributionsb4af6fb4/OCIa3f6895e/distworkerf52c44c0/OCIworkerf29bf398/CLI61a605a9/test4acfb5c3 after99tests and independent actualOCI+ordinarytar+jointforgery probes. No findings/simplify notes. Valid markers skipped asJAR onlyOCI, rawlayer evidence retained/visible removed, falsekindtarget rejected, sourcequalifications+cleanup preserved. WholeTASK4 final docs/shared simplify/highreview/fullsuite still required.

Recovered OCIworker review accd411 NOTAPPROVED selection defect: implicit request with two known manifests accepts forged valid explicit-choice response chosenfirst. Require implicit chosen only when exactlyone knownmanifest; multiple staysambiguity unless explicit request. Add regression, no crypto authenticity overclaim. Separately recovered distributionCLI primaryinterrupt cleanup reviewer a18718f APPROVED11e1fd17/68ed3e78 exact originalremoval/secondaryKI probe; no residual in cleanupdelta.

Parent reproduced implicitmultiplemanifest selection bug with validexplicitresponse replay:RED errorNone. Added guard implicit chosen only exactlyoneknownmanifest, preserves explicit selection.49 OCIworker+distributionCLI tests -Werror pass17.540s source8bf03e40f586a7a180b938a9c57e0f8f3fb6cb1a0262ad3a945217869ba91454/testd6bf5d561e279842eff7c4faecc35ee370df9a321933d01f69cbc20573d2ca3a. Same original reviewer targeted recheck requested; whiteout delta reviewed separately.

OCIworker original-selection reviewer accd411 final targeted APPROVED8bf03e40/d6bf5d56 after4controls and independent explicitresponse replay underimplicit request nowinvalid; realimplicitmultiple remainsambiguity, explicitchosen succeeds. No residual; whiteoutprotocol changes approved separatelya11be. All currently reported distribution/OCI/visibility/CLI findings closed, taskfinal docs/sharedcleanup/highreview/fullsuite remain.

TASK4 shared simplify reuse af8f5736 complete no meaningful changes recommended. Existing process/JSON/resource/traversal/nestedvalidator reuse adequate; whiteout reader-vsprotocol and completevisibility-vsworkerpartial checks different contracts intentionally retained. Tinyclosehelpers/identity variants/testtar shapes not consolidated, explicit disposition no futurepromise. Other3angles pending; no correctness/taskapproval from cleanup.

TASK4 simplify efficiency a65e14cf complete2 recommendations: split OCI visibility path once instead of perancestor; precompute selected parsedcoordinates and observedGAV/known once in rawdistribution and visibility candidate association loops, retain order/comparison100k cap. Codegrounded allocation reduction no measuredtiming. Retain digest/stat/protocol/layerbudget checks and budgetedremove scans; trivial repeatedhexdigest explicitly not recommended. Await remaining2angles before bounded cleanup.

TASK4 simplify altitude aadb3e complete no architectural refactor; workers/protocol/cleanup ledgers appropriately separate. One docs-only recommendation: distribution_worker opening rationale says OCI lacksdescriptorAPI nowstale, replace OCI uses separateoci_worker boundary. Accepted for final cleanup with otherangles. No runtime defect claimed.

TASK4 fourth simplify simplification ad42055f complete no meaningful additional change. Preserve exceptionownership/cleanup-vs-evidence maps and twophasewhiteouts; no unreadstate found. Tiny overwrittenprimary init not recommended; pathsplit suggestion already efficiencycandidate. All4angles now received, apply bounded pathsplit/association-hoist/docfix then verify, no genericframework.

Shared simplify4angles complete, all recommendations dispositioned. Parent applied precomputed pathsplit in visibility, selectedcoordinate parsing/observedGAV outside association innerloops with unchanged counters/order/caps, stale tarworker doc corrected OCIseparateboundary.96 distributionCLI/visibility/workers -Werror tests pass28.736s. No broad refactors, deliberate lifecycle/protocol checks retained. Final highreview/fullsuite/docs still pending.

Final integrated review reports2 reproduced defects pending final consolidation: OCIvisibility rejects legitimate ZIP directory records statusrecorded/shanull, adds META-INF/ to otherwise validJAR =>globalpartial/zerocandidates; nested distribution cleanup copied only after readerfdclose, close-afterrelease OSError loses retained mdi-nested ledger.160scopedtests pass not coverageproof. Both retained for targeted correction, no taskclosure.

Final reviewer additional documentation recommendation: distributions.md unlinked from SKILL/CLI navigation. Accepted linkaddition alongside correctness fixes, not runtime defect. Nestedcleanup loss now independently reproduced through actual worker completeexchange/no cleanupmetadata with retainedfile, reinforces mandatory lowerledger-beforeclose fix; no reviewapproval yet.

Parent149distribution/reader/OCI tests -Werror pass3.878s nestedcleanup fix6b025e1afa1308bee7e4873522792b471b741bbdd6501fb2954a2e135af58e63/tests2bdc572aa00557e29eb46dee4a2e54fad06bdc02077e9f270be7fb0b74a451ea. Immediate lowerledger merge onreturn/exception beforeclose/budget, boundedfallback recoveryroot preserves obligations. Same finalreviewer aaf8e743 requested originalactualworker repro; doclinks fixed. OCI validdirectory-record issue still pending owner.

Final reviewer targeted original nested exactpath loss FIXED and doclinks verified, but fix introduced incomplete-ledger flag gap: rootonly nested-cleanup-ledger-unconfirmed lacks individual_paths_completeFalse, distribution_worker defaults missingTrue; actualworker lowerOSError/nometadata =>root recovery plus incorrectlycomplete details despite undisclosedfile. Propagate incomplete status return/exception/OCI layercollectors or explicit normalization; root recovery not complete enumeration.52tests pass but fullapproval withheld. Assigned owner, no candidate scope broadening.

Recovered OCI directory fix3bda6123/de3b19f7 parent27 visibilitytests pass. Combined68 with activeOCIcleanup tests failed2 expected unfinished individual_paths_complete propagation (True vsFalse); full traces read, no allgreen claim. Final same-reviewer directory original repro can check independently, cleanupowner still fixing. No candidate validation weakening.

Parent145 related reader/worker/CLI tests returnedOK108.612s and hashes match new cleanupflag fix distdfadb43f/OCI13fc394a/testsaaa82cf8/a7bd4b35. BUT output contains unraisable ResourceWarning subprocess1318682 stillrunning from Popen.__del__; do NOT call warning-clean or final passing gate. Need identify test/process lifecycle with full verbose log and unraisablehook discriminator, inspect current PID before any action, no blind kill/PIDreuse. Explicit cleanupFalse propagation awaits same final reviewer independent repro.

Final reviewer rootfallback completeness targeted APPROVEDdfadb43f/13fc394a after actual distribution+OCI worker unknownnestedfile/noledger probes, recoveryroot and individualpathsFalse survive all layers,8focused warningclean tests. Reviewer states all original findings individually closed (directoryentry3bda6123, exactledger/docs links); no new findings. Parent lifecyclewarning discriminator bxp1wpctf pending before finalclean gates, no attribution of priorwarning to fix.

Warning discriminator bxp1wpctf FAILEDexit1 despite145 test assertionsOK: unraisablehook pinpointed tests.test_oci_worker.test_signals_precede_reap_and_lost_identity_never_signals. Source trace: test mocks _probe=>lost while real spawned passchild may stillalive; cleanup correctly suppresses signals, wait(timeout0) may timeout, fixture abandons actual Popen. Fix test to simulate true external reap (wait actual child before returning lost) rather than contradictory alive/lost mock. No production broadcleanup weakening; assert real object reaped/no unraisable, rerun discriminator.

Parent test-only correction simulates actually reaped child before _probe lost, retains no-signal/unconfirmed assertions and adds actual assert_reaped. Focused test with sys.unraisablehook+gc FAILED on any warning nowPASS;27 fullOCIworker tests -Werror PASS no warning. Final reviewer targeted fixture delta requested, full clean suite next. Production cleanup unchanged.

Final integrated code-review aaf8e743 verdict APPROVED all findings+followups closed, no cut/deferred recommendations. Test-only lostidentity fixture37240ed1195a2fce6ef0e8bc7b7dd123558dc660e50551360b32edf0aadd49ea independently3cases -Werror/unraisablehook/gc clean. No production behavior weakening. Fullsuite/finalgates parent still pending.

Parent read full configured bhio2xgls output:1752tests in495.773s OK exit0, explicit sys.unraisablehook count0 aftergc. Only expected duplicateZIPwarning, prior Popenunraisable fixture issue resolved with actualreap test. Final integrated highreview APPROVED allfindings/followups, simplify4angles complete. Prepare TASK4 closure with supportedlocaltar/OCIinventory+qualifiedvisibility limits, not unrestricted realimagecompatibility.

TASK4 complete within user-approved local evidence scope: tar/targz and provisional OCIlayout inventories, verifiedblob/member identities, supervised readers+bounded response/cleanup, supported regular/dir layervisibility incl whiteout/opaque qualified (not runnable filesystem), candidate application association realmunknown absentprovenance, same-run sourcequalifiers, explicitunsupportedformats/no implicit pulls/build/network. Four-angle simplify dispositions applied, final highreview APPROVED all issues+testwarningfix, full1752tests/unraisable0 and fresh scopedtests pass after reflect+verification invocation. SyntheticOCI support explicitly labeled, no claim full externalimagecompatibility or executing containers. Further live acquisition/provider breadth remains separate.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
