---
id: TASK-18
title: Prove analyzer and safety boundaries before collector implementation
status: Done
assignee: []
created_date: '2026-09-10 17:15'
updated_date: '2026-09-10 20:33'
labels:
  - implementation
  - spike
dependencies: []
references:
  - docs/superpowers/plans/2026-09-10-maven-dependency-investigator.md
documentation:
  - docs/verification/maven-investigator-spike.md
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Implement Chunk 0 blocking probes. Prove real app-to-library and upstream-library-to-library references using Apache analyzer helper; verify XML, Git, archive and permissioned Maven/OSV boundaries. No application scaffolding before mandatory local safety/usage probes pass. Obtain explicit permission for Maven, downloads and external requests; record blockers rather than claiming skipped probes pass.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Real helper emits fixture B-to-C class-reference evidence with no direct application import of C.
- [x] #2 Encoding-aware XML and command-specific hostile Git probes pass with recorded output.
- [x] #3 Every external/Maven probe has explicit consent and records actual evidence or an open verification blocker.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Next consequential probe needs user consent: build our analyzer helper and synthetic Maven fixture using temporary build/local-repository paths, download pinned public dependencies/plugins from Maven Central, and query OSV with a public known-package coordinate/version only. No user project source or credentials uploaded, no commit/push. Waiting for explicit operation scope approval before those operations.

User explicitly approved the proposed initial Maven/security probe scope: build our helper and synthetic projects in temporary directories; download pinned public dependencies/plugins from Maven Central; query OSV with public package coordinate/version. No source/credential upload, commit or push authorized. Approval applies to this initial probe batch, not future retries after failures or changed scope.

Authorized OSV probe succeeded: POST /v1/query with public Maven org.apache.logging.log4j:log4j-core version 2.14.1, no redirects, 30s timeout/8MiB cap. Response contains known CVE-2021-44228 alias and multiple affected objects with distinct branch ranges. Response saved /tmp/maven-investigator-probe.WKTQpn/osv-log4j-response.json, SHA256 955f2ff3ac97c8ba2c9fdbd6fad5cb38c3366b0842b8c81a559647576e19836c. Official docs confirm pagination via next_page_token and page_token; response had no continuation. Actual field is modified, not candidate-1 lastModified. Applicability is provider-reported, not independent proof; preserve exact query and full relevant affected structures. Analyzer spike delegated to adf2cef3e7494806f in isolated helper-probe directory with separate Maven local repo and no personal settings.

Local safety mechanism probes run and read: /tmp/maven-investigator-probe.WKTQpn/test_xml_probe.py passed 4 unittest methods covering UTF-8/UTF-16/UTF-16BE DTD rejection, long prolog, entity declarations, comments/unicode, inert XInclude and bounded/malformed input. test_git_probe.py passed on installed Git 2.55.0: log/show/blame/status under sanitized environment plus command-specific flags did not run configured textconv/external-diff/fsmonitor/pager markers and left synthetic index bytes unchanged. Synthetic fixture commit was only in temporary test repository; project repository remains uncommitted. These are bounded mechanism probes, not full product safety proof. OSV docs explicitly describe fuzzy upstream-version matching; report applicability as provider-reported with exact query retained, not independent exact-version equivalence.

Parent reran helper contract tests: both exploded-dir and JAR roots pass real A->B and B->C references with no direct A->C. Inspected source confirms existing Apache APIs, not own parser. Verified tree JSON counterexample: verbose output includes Gson 2.10.1 plus selected 2.14.0 without omitted-state fields. Reject subagent suggestion to infer mediation from duplicates; keep diagnostic verbose tree separate and corroborate resolved presence from non-verbose tree/classpath. Effective POM probe lacks -Dverbose and parent found no origin-location comments; origin-comment extraction remains UNVERIFIED. Probe first-wins duplicate class owners and untested multi-release handling cannot become production policy. No extra Maven retry run.

Probe author accepted all parent qualifications and inlined them in temporary report.md: no duplicate-based mediation inference; verbose tree diagnostic only; first-wins duplicate owners spike-only; effective-POM origin comments NOT VERIFIED because -Dverbose was absent. Parent inspected updated statements. No additional Maven operation executed; feasibility review remains pending.

Independent spec-compliance reviewer a8c59ecb9b3ab99e6 APPROVED bounded upstream feasibility proof after live helper run, exact API/source/hash checks and artifact inspection. AC1 verified; TASK-18 not wholly complete. Nonblocking nits: helper-run-dir.txt/helper-run-jar.txt are empty stdout logs because reports went to out/*.json; cite JSON/live rerun as substantive evidence. DONE header applies only to bounded helper spike, not entire task. Remaining Chunk 0 gaps: explicit verbose effective-POM origin probe and Boot-style nested-container fixture. Duplicate-owner ambiguity and multirelease handling remain release requirements in TASK-21; mediation source restriction recorded in TASK-22. Request targeted quality review next; no broad fan-out.

Parent local Boot-style layout probe now passed: synthetic outer JAR contains BOOT-INF/lib/libB.jar and libC.jar; extracted nested bytes match original complete SHA256 digests and nested class listings contain BService and CUtil. No Spring Boot launcher executed; this is layout/byte-identity feasibility, not a full bounded archive reader test. Only remaining external evidence probe identified here is effective-POM origin comments with explicit -Dverbose; ask renewed operation-specific consent before running.

User explicitly approved one additional isolated Maven invocation: help-plugin 3.5.2 effective-pom with -Dverbose, new output/log paths, existing isolated cache and Central-only settings. Quality reviewer adac9428d041145f9 APPROVED bounded helper proof; report wording advisory pending correction: edges exclude all intra-entry references, not only same-class self-references. Cosmetic missing-path error ordering accepted as nonblocking spike-only and no code change planned.

Renewed approved verbose effective-POM probe PASSED: help-plugin 3.5.2 effective-pom -Dverbose=true wrote out/effective-pom-verbose.xml with fixture source comments (Gson fields lines 18/19/20, converter-gson fields 23/24/25). Parent read BUILD SUCCESS log and parsed the actual XML with comments retained; field locations match inspected synthetic POM. XML SHA256 f08ef4e7fea52547be46ac35c0f06edd4900ee5debe056f46aca870806c0e4b8. Empty user/global Central-only settings and isolated local repository used; Maven/JVM option environment overrides cleared. This verifies direct fixture origin metadata, not full BOM/profile/multi-module semantics. TASK-18 gates/remainder remain open; no blanket completion claim.

Report clarification verified: artifact edges exclude all intra-entry references while usages retain them. Parent independently parsed verbose XML and rejected subagent claim of 107 origin-location comments: parsed element-tree comments total 95, line-location comments 40, fixture-specific origin comments 11. Nonessential erroneous count removed from temporary report; verified dependency field locations remain valid. No additional Maven execution. Report correction does not alter the successful origin probe or helper proof.

Continue requested: persisted concise evidence handoff docs/verification/maven-investigator-spike.md. Fresh parent rerun XML/Git/helper tests all passed; recorded verbose XML/advisory checked without new Maven/network. AC2/AC3 and verification DoD checked with evidence. Explicit /simplify invoked for handoff (four bounded one-file readers) and /code-review high invoked with direct one-file review/no fan-out. Await those results before TASK-18 Done and TASK-19 implementation. Temporary probe code is not promoted to production.

Simplicity review received: removed self-referential earlier-comment-count sentence from evidence handoff; corrected historical count remains in task audit only. Optional repeated-path shortening skipped: full command paths preserve directly runnable evidence and are intentionally retained. Other distinct scope disclaimers retained. No new runtime/probe changes.

High-effort code-review a7ee4ad4793ab0858 APPROVED evidence handoff at SHA256 2cd322ca2d8cea8036d3e7314e4e60bb457acb147ac1ff02457f13ad62d128fd; parent verified current bytes match. Reviewer checked cited artifact claims and ran all seven local tests; no issues. Documentation handoff only, no production-readiness claim. Remaining simplify angles still pending; task stays In Progress.

User explicitly reinvoked /simplify without a path. Inspected actual scope: unborn branch, no HEAD/tracked code; untracked docs/backlog are real review scope, not an empty diff. Launched exactly four bounded review angles over current docs/backlog including evidence handoff. No web/Maven/recursive fan-out permitted. This pass supersedes missing earlier handoff responses for gate collection; late prior findings still get dispositioned, not ignored. No changes or gate completion claimed until results arrive.

Current simplify reviewer a1ea7e3d8df42cdcc identified superseded instruction in TASK-22 notes. Applied: removed obsolete claim that the direct-origin verbose probe remains pending; retained warning that ordinary nonverbose effective POM is not origin proof and retained remaining parent/BOM/profile/module fixture obligations. Approved spec/plan and evidence handoff unchanged.

Current simplify results received: efficiency reviewer a0f4c75ca82a3ea4a recommends shared artifact-scoped gate evidence for Arena phase tasks; disposition and review-scope correction recorded in TASK-16. Altitude reviewer a5637da54ef7443e4 reports no findings. Prior current-pass finding in TASK-22 already fixed. One current-pass reviewer response remains outstanding; DoD simplify not yet checked.

Final current-pass simplify response ad5050b982caea96f received. F1 overlaps the already-applied TASK-22 correction; operative constraints plus canonical spike pointer retained. F2 accepted: retain historical review guidance but stop propagating further copies. All four current-pass simplify results addressed; no pending finding. Fresh local XML/Git/helper rerun passed; handoff hash still matches code-review approval. TASK-18 bounded feasibility scope complete, not product readiness.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
