---
id: TASK-21.1
title: Prepare offline helper packaging and dependency notices
status: Done
assignee: []
created_date: '2026-09-11 16:35'
updated_date: '2026-09-12 07:45'
labels:
  - implementation
  - packaging
dependencies: []
parent_task_id: TASK-21
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Parallel with staging: prepare local-only reproducible Java helper packaging using already downloaded pinned artifacts. No network/Maven, no commits. Own helper/build_local.py, helper/DEPENDENCIES.json, helper/THIRD-PARTY-NOTICES.md and tests/test_helper_packaging.py only; built artifacts outside repo until reviewed.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Verify runtime closure and license notices from actual local artifacts and exact sources.
- [x] #2 Local build recipe uses explicit pinned inputs, deterministic JAR metadata and no implicit download.
- [x] #3 Tests exercise successful packaging and hash mismatch rejection without changing staging/usage files.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Parent independently ran5 packaging tests GREEN; all four artifact hashes match worker. Local deterministic helper build and hash rejection work. Redistribution NOT ready: notices record missing externally sourced ASM/Gson/error-prone license texts; no new network request authorized or executed. AC1 remains open until license provenance handled. Parent read notices, bounded packaging compliance review follows. No gate exemption or child-task Done claim.

Packaging compliance reviewer afb75607ab46abd94 APPROVED bounded build contract after independent5 tests, double build and local jar/POM/hash/license inspection. Distribution blockers remain: ASM full license text/copyright provenance missing; Gson/error-prone canonical Apache text reuse is a possible explicitly documented provenance decision, not automatically approved legal clearance. No blockers silently removed and no network requested. Nonblocking BUILD-INFO omission accepted for correction: record Python and both compile/runtime zlib identities so reproducibility evidence names serializer/compressor runtime. Quality review follows after metadata edit; output artifact claims remain same-toolchain only.

Parent reran5 packaging tests GREEN after Python/zlib BUILD-INFO addition. Hashes match build6d871d48/test19acdbb3; manifest/notices unchanged. Same-toolchain reproducibility scope explicit, all redistribution blockers remain. Begin bounded second-stage quality review; no license clearance or task completion.

Packaging quality reviewer a1209b896b7b46898 APPROVED bounded builder after independent5 tests and artifact probes. Release remains blocked on missing license texts. One nonblocking error-channel issue accepted for correction: dangling-symlink output path bypasses Path.exists precheck and produces traceback/exit1 instead of clean collision exit2. Use lexists-style rejection before any build work and test symlink/target unchanged; no new network or license decision.

Parent independently reran6 packaging tests GREEN after output symlink fix; build0077c56ff1c918a8a9e32d8f171153ea22cdffb025015c117a4ce3b889655e18 test7f4056d030496e0437d243d69c7d6deeb9ce32284961536ad94a7002fb643c4a. Output lexists checked before resolve; dangling link rejected cleanly before javac, target unchanged. Same quality reviewer targeted closure requested; license blockers unchanged.

Packaging quality reviewer a1209b896b7b46898 final APPROVED build0077c56f/test7f4056d0 after independent6 tests and dangling-link probe. No residual within offline builder slice. Redistribution still explicitly blocked by missing license text provenance; child task remains In Progress (AC1 not met), not Done. Parent already verified exact hashes/tests. Remaining parent integrated simplify/code-review evidence still required, no gate exemption.

User explicitly authorized downloading missing public license texts from official sites, recording sources/checksums and including them in helper package. No source or credentials sent; no commit/push authorized. Proceed with official license text retrieval and evidence-backed manifest/build updates.

Authorized official-license retrieval and offline bundle integration assigned to ad2b86fba63a85d3c with exclusive helper packaging/license/test ownership. Existing packaging worker is finished; staging and other modules untouched. Must preserve raw text/copyright, record official source/access/hash, verify vendored text before compilation, keep builder offline and clear only evidenced missing-text blockers.

Authorized missing license texts retrieved and integrated. Parent13 packaging tests GREEN and hashes verified: Apache canonicalcfc7749b..., ASM exact-tag293b6af3..., manifest1f9cb091...; read full ASM copyright/conditions/disclaimer and provenance README. Offline builder copies/checks vendored bytes before compiler and reports no missing-text blockers in tested output. This clears specific technical missing-text prerequisite, not legal opinion or full-product release approval. Final review must inspect manifestv2/path safety/builder changes; gates still open.

Final packaging/license integration reviewer a1209b896b7b46898 APPROVED exact build caa4141882b4622f5df87290fa8a019d3312b0b1dac8091609803d40e0f31bea and manifest1f9cb0912fea4564669c7c201b0378c77940f6c631a3306f3473498f8743bcf0. Parent reran13 packaging tests GREEN and verified hashes. Official-license provenance, vendored path validation, hash-before-compiler and output byte preservation approved; specific missing-text blockers resolved, no general legal clearance claimed. Nonblocking missing current blocker-emission test accepted for integrated task test follow-up using an intentionally textless dependency fixture; no production change requested. Child remains open for inherited simplify/code-review invocation evidence, not silently Done.

Shared TASK21 final artifact-scoped simplify/high-effort code-review approval and658-test final run cover this child. Missing license texts resolved by authorized official-source vendoring; no legal-clearance claim.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
