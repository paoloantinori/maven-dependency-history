# Maven Dependency Investigator Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved bundled skill with reproducible local evidence for direct and transitive Maven dependencies, including packaged copies, upstream class usage, history, and permissioned security enrichment.

**Architecture:** Python orchestration and a small Java helper wrapping Apache's existing class-reference analyzer. Immutable per-run report revisions carry evidence, selection, operation state, and qualifications; the coding agent interprets these records without inventing consent or factual conclusions.

**Tech Stack:** Python 3.11+ standard library and unittest; Git; optional Java runtime with a bundled analyzer helper; Maven Dependency Plugin 3.11.0; Maven Help Plugin 3.5.2; one OSV query adapter. Tool versions and helper closure are pinned and tested in the blocking spike before release.

## Authority and execution gates

Specification: `docs/superpowers/specs/2026-09-10-maven-dependency-investigator-design.md`, SHA256 `907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848`. Research: `claudedocs/research_maven-dependency-explanation_2026-09-10.md`. Tracking: TASK-2 and Arena TASK-11 through TASK-16. Candidate selection and verification are recorded in the companion synthesis document.

The user authorized moving forward, not automatic commits or publication. Run no Maven/wrapper, download, HTTP lookup, or bootstrap merely because this plan contains its command. Obtain operation-specific approval including destination/outgoing data and anticipated writes first. Local compilation of our own fixture/helper code is distinct from running an inspected project's code. All commands below are planned commands unless the synthesis document explicitly records a completed probe.

Do not silently weaken initial requirements when a spike fails. Record a blocker; stop dependent work. Offline inspection remains useful, but release verification must include real upstream-library reference evidence and a functioning advisory adapter. Lack of permission to test those means verification is blocked, not passed.

## Arena decisions

Use candidate-1 as the base: reusable Apache helper, unittest, state outside the checkout, small command surface. Graft candidate-2's detailed evidence/fixture coverage and injectable transport; graft candidate-3's durable consent consumption before process creation. Do not implement candidate-2's custom ASM reference traversal, candidate-3's automatic Maven version probing, raw-output capture, pruning commands, or a redundant report-plus-journal state store.

All three candidates' ASCII byte-level DOCTYPE guards are rejected. A harmless UTF-16 probe bypassed them. Use encoding-aware parser callbacks instead. The correct release API is `DefaultClassAnalyzer.analyze(URL, ClassesPatterns)`, not the single-argument call in candidate-1.

## File map

Create these files; no existing application files exist:

```text
skills/maven-dependency-investigator/
  SKILL.md
  references/cli.md
  references/evidence.md
  references/permissions.md
  scripts/investigate.py
  scripts/mdi/__init__.py
  scripts/mdi/cli.py          # arguments, JSON envelope and dispatch
  scripts/mdi/report.py       # evidence schema, joins, qualification validation
  scripts/mdi/runstore.py     # immutable revisions, generation and writer lock
  scripts/mdi/capture.py      # bounded bytes, digests, changing-input evidence
  scripts/mdi/limits.py       # shared limits and overrides
  scripts/mdi/xmlsafe.py      # encoding-aware DTD/entity rejection
  scripts/mdi/discover.py     # POMs, candidate selection and realms
  scripts/mdi/archives.py     # bounded ZIP reader and nested content
  scripts/mdi/correlate.py    # byte identity versus build provenance
  scripts/mdi/gitinspect.py  # hardened Git reads and history evidence
  scripts/mdi/usage.py        # shipped helper invocation and class/artifact joins
  scripts/mdi/operations.py   # proposals, single-use approval and audit
  scripts/mdi/maven.py        # pinned goals and report import
  scripts/mdi/advisories.py   # OSV transport and applicability evidence
  helper/pom.xml
  helper/src/main/java/mdi/UsageHelper.java
  helper/BUILD-INFO.md
  vendor/usage-helper.jar
  vendor/SHA256SUMS
  vendor/THIRD-PARTY-NOTICES
 tests/__init__.py
 tests/support.py
 tests/test_spike.py
 tests/test_report.py
 tests/test_runstore.py
 tests/test_capture.py
 tests/test_xmlsafe.py
 tests/test_discover.py
 tests/test_archives.py
 tests/test_correlate.py
 tests/test_gitinspect.py
 tests/test_usage.py
 tests/test_operations.py
 tests/test_maven.py
 tests/test_advisories.py
 tests/test_cli.py
 tests/test_e2e.py
 tests/fixtures/recorded/     # small reviewed Maven/advisory output with provenance
 tools/build_test_fixtures.py
 docs/verification/maven-investigator-spike.md
```

Fixture archives and Git repositories are generated in temporary directories, not placed in the user's Maven cache. Small reviewed recorded responses may live under fixtures/recorded. License and reproducible-helper build metadata are mandatory before bundling binaries. Adding files is not authorization to commit them.

## CLI and evidence contract

Entry point: `python3 skills/maven-dependency-investigator/scripts/investigate.py` (abbreviated `INV` below; expand it in task commands).

```text
INV discover --repo PATH --query QUERY [--module M] [--artifact PATH] [--state-dir PATH]
INV select --run-dir PATH --expect-revision N (--candidate ID | --all)
INV collect --run-dir PATH --expect-revision N
INV import --run-dir PATH --expect-revision N --report FILE --kind tree-json|effective-pom|classpath [--provenance FILE]
INV propose --run-dir PATH --expect-revision N --kind tree|effective-pom|classpath|advisory --candidate ID
INV execute --run-dir PATH --expect-revision N --operation ID --approval-file FILE
INV report --run-dir PATH [--revision N] [--validate]
```

Bare queries match artifact names/observed entries; qualified queries use `groupId:artifactId[:version]`. Type/classifier and packaged-copy ambiguity remain explicit candidate fields rather than inventing another ambiguous coordinate syntax. Selection records the original query, filters, candidates and chosen IDs or all-candidates choice. Multiple dependency paths alone do not create ambiguous subjects.

Every command emits one JSON document. Exit 0 means a valid result, including partial evidence; exit 2 invalid invocation/schema; exit 3 stale/concurrent run conflict; exit 4 unexpected internal failure. Stage outcome, not process success, determines evidence completeness. No-match and missing tools remain explicit result data.

Default run location: `${XDG_STATE_HOME:-~/.local/state}/maven-dependency-investigator/runs/<uuid>/`, owner-only. `--state-dir` must not select an existing unrelated directory for a new run. No registry, shared cross-run cache, auto-pruning or cross-run merge. Each revision is a complete immutable JSON snapshot; one revision contains operation consumption and its started audit entry atomically before execution. Write temporary file, flush/fsync and atomically publish under a single-writer lock. On partial storage failure, refuse execution. Never use two independently updated files as authorization truth.

```text
report: schema_version, run_id, revision, query, selection, subjects[], evidence[],
        analyses[], stages[], operations[], qualifications[]
subject: id, realm(project|build-plugin|unknown), module, coordinates?,
         owning_plugin?, build_context?, packaged_copy?(archive_digest, entry_path)
evidence: id, category, subject_id?, source_locator, observed_at, captured_identity?,
          producer?, producer_version?, generation_context?, payload
analysis: id, evidence_ids[], statement_kind(observation-summary|inference), limitations[]
stage: name, outcome(complete|partial|unavailable|denied|failed|not_applicable),
       coverage, reasons[], evidence_ids[]
operation: id, run_id, scope, input_identities, tool_identity, disclosure,
           state(proposed|denied|consumed|finished), audit, evidence_ids[]
qualification: affected_evidence_ids[], kind, reason, observed_at
```

Validate every reference, unique ID, subject realm, operation/run binding, and append-only identity across revisions. Qualification updates add records rather than rewrite old observations. Derived conclusions inherit changed-input/unknown-provenance limitations. Two independent reports can each have E1; they cannot be merged as one report.

Approval files are a transport for actual host/user approval, not proof generated by an untrusted repository or by the collector. The skill asks the user and records the host conversation/approval reference. Never auto-create an affirmative approval file. Validate operation/run/input bindings, consume once before spawn, and require renewed approval for retry or changed inputs. This does not sandbox the agent itself; host tool permissions remain the authority.

## Limits and offline posture

Initial defaults: XML 8 MiB; archive input 2 GiB; expanded bytes across the entire nested traversal 2 GiB; individual entry 256 MiB; total entries 20,000 across traversal; nesting depth 4; archive stage 120 seconds; Git response 8 MiB/30 seconds; helper response 16 MiB/120 seconds; Maven 900 seconds; HTTP response 8 MiB/20 seconds. Validate positive overrides. Tests use tiny injected limits rather than huge test files. A timeout/output limit applies to the process and its children; do not leave subprocesses running after reporting failure.

No claim of bounded memory solely from checking ZIP central-directory metadata. Validate archive input/metadata budgets before allocating unbounded entry tables; spike the chosen runtime library's behavior. Nested archives spool captured bytes to owner-only scratch using generated names when needed. Full digests require full captured content; truncated content cannot prove byte identity.

No external execution for presence detection: locate tools and hash them without running `mvn --version`, wrappers or project code. Java analysis executes the shipped trusted helper only, never loads application classes. Use a sanitized JVM environment to prevent inherited agent/options injection. Missing Java yields unavailable usage; the release fixture still requires Java. No auto-bootstrap during an investigation.

## Chunk 0: prove the uncertain tool boundary before product scaffolding

**Files:** `docs/verification/maven-investigator-spike.md`, `tests/test_spike.py`, `tools/build_test_fixtures.py`, helper source/pom only after permissioned dependency acquisition.

- [ ] Write a failing real-use probe: application A references library B; B references C; A never imports C. Compile our fixture sources with explicit local `javac` and create ordinary JARs plus a Boot-style container. Keep all fixtures in a temporary root. Run `python3 -m unittest tests.test_spike -v`; expected RED because the helper is absent, not a skipped test.
- [ ] Inspect exact-tag analyzer source and published JAR signatures. Existing verified entry points are `new ASMDependencyAnalyzer().analyzeUsages(url, new ClassesPatterns())` and `new DefaultClassAnalyzer().analyze(url, new ClassesPatterns())`. Pin the published analyzer 1.17.2 dependency closure from its POM; do not assume the candidate's ASM pin or guessed transitive dependencies.
- [ ] With explicit Maven/network approval, build a tiny helper wrapping those APIs, not a new bytecode parser. Emit JSON containing input identity, contained classes and referenced class pairs. Use a maintained JSON serializer pinned through the helper build rather than unsafe string concatenation. Run the probe to GREEN and retain actual B-to-C evidence. Verify duplicate classes, missing targets and multi-release JAR handling are identified or qualified. If helper fails mandatory reference fixtures, evaluate jdeps once as the fallback; if neither succeeds, record a blocker and stop.
- [ ] Search/read official Python pyexpat documentation, then test encoding-aware DTD rejection on UTF-8, UTF-16LE/BE, long prologs, external/local entities and expansion attempts. Use callbacks rejecting DOCTYPE/entities and parameter-entity parsing disabled. Build ElementTree only from the same bounded validated bytes with comments retained. Never use ASCII byte scans or prefix windows as the security boundary.
- [ ] Verify Git command-specific hardening on a generated hostile repository. Check log/show/blame/status separately with textconv, external-diff, fsmonitor and config-injected helpers; assert no marker command executes. Disable optional locks, external helpers and network-promisor fetching; if the installed Git cannot safely inspect missing objects, return unavailable rather than auto-fetch. Record exact accepted argv and environment, not memorized flags applied indiscriminately.
- [ ] Search/read OSV `/v1/query` docs and record Maven identity conventions, applicability semantics, response pagination and range shapes. With explicit approval, make a bounded known-package lookup; record a redacted/public fixture and source/date. Default tests use an injected in-memory transport, not a network server. An untested production provider keeps the release gate open.
- [ ] With explicit approval, run fully qualified dependency-plugin 3.11.0 tree/classpath and help-plugin 3.5.2 effective-pom goals on the fixture. Confirm per-module JSON destinations, conflict/omitted-node information actually present, and effective-model origin comments. Record supported fields and omissions; JSON mode is not presumed to expose every conflict explanation. No quiet downgrade of origin evidence to a fabricated model.
- [ ] Record every command, output, versions, artifact checksums, licenses and decision in the spike document. Update TASK-2 blockers. Product implementation starts only after mandatory safety/usage probes pass; denied external probes remain explicit blockers for their dependent integration/release tasks.

## Chunk 1: report, capture, safe XML and run state

**Files:** report.py, runstore.py, capture.py, limits.py, xmlsafe.py and corresponding tests.

- [ ] Write `tests/test_report.py`: accept a minimal report; reject dangling IDs, duplicate subjects/evidence, realm mixing, unknown outcome and cross-run references. Run `python3 -m unittest tests.test_report -v` to RED, implement builders/validator, rerun to GREEN.
- [ ] Write `tests/test_capture.py` and limits tests: digest equals captured bytes; truncation has no full-content identity; controlled between-read changes qualify old observations and their analyses. Run to RED, implement bounded capture and within-run reuse, rerun to GREEN.
- [ ] Write `tests/test_xmlsafe.py` with the encoding matrix from Chunk 0. Include XInclude and schema references that remain inert; reject unsafe constructs without local/remote reads. Run to RED; implement callback gate plus comment-retaining parse; rerun to GREEN. Parser exceptions yield partial evidence rather than unsafe fallback.
- [ ] Write `tests/test_runstore.py`: immutable revisions, atomic failure, expected-generation mismatch, simultaneous writers, crash after consumed authorization, safe refusal of stale lock, and resume preserving selection/IDs. Run to RED; implement lock plus one snapshot source of truth; rerun to GREEN. No automatic replay of consumed operations or cross-run merges.

Critical XML mechanism (verify complete resource bounds in tests):

```python
from xml.parsers import expat

def reject(*args):
    raise UnsafeXML("DTD or entity declarations are not supported")

parser = expat.ParserCreate()
parser.StartDoctypeDeclHandler = reject
parser.EntityDeclHandler = reject
parser.ExternalEntityRefHandler = reject
parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
parser.Parse(captured_bytes, True)
# Parse these same bytes with ElementTree; never run XInclude expansion.
```

## Chunk 2: discovery, archives and identity

**Files:** discover.py, archives.py, correlate.py, tests/test_discover.py, tests/test_archives.py, tests/test_correlate.py.

- [ ] Test POM module/declaration extraction, management-only entries, profiles, properties, parent links and plugin/project realms. RED, implement declaration observations without Maven resolution, GREEN. Bound module traversal; cycles and paths outside the selected checkout require explicit coverage handling, not unrestricted reads.
- [ ] Test candidates from declarations, imported reports and packaged content, including an archive-only dependency despite a different POM match. RED, implement query/filter-driven discovery without hiding packaged copies, GREEN. Selection records ambiguity and explicit all-candidates choice.
- [ ] Test ordinary JAR/WAR/Boot nested layouts, unknown identity, duplicate entries/copies, traversal, symlinks, malformed archives and cumulative budgets. RED, implement zipfile-based bounded inspection and safe generated scratch names, GREEN. Do not extract application content into source paths or execute it.
- [ ] Test correlations: coordinates give candidate links; full digest match gives byte identity; verified graph context gives graph attribution; enclosing archive build provenance is a separate requirement. Same-coordinate/different-content and same-bytes/stale-enclosing-archive fixtures must remain distinguished. RED, implement typed correlation evidence, GREEN.
- [ ] Preserve observed metadata disagreements. Filename-derived GAVs and local-repository layout alone are candidate identity, not verified component identity. Unresolved versions cannot be sent as exact security queries.

## Chunk 3: usage and local history

**Files:** usage.py, gitinspect.py, tests/test_usage.py, tests/test_gitinspect.py.

- [ ] Write real-helper tests for project-to-library and upstream-library-to-library references; production/test roots; duplicate-class owners; missing referenced classes; multi-release output. RED, invoke the shipped helper on bounded captured inputs, GREEN. Record helper/runtime identity and qualify stale inputs. Never choose an arbitrary owner for duplicate classes.
- [ ] Test service descriptors and source-reference evidence separately from bytecode. Include mismatched source attachment identity. RED, implement supporting observations, GREEN. No method-reachability or safe-removal inference from absent class references.
- [ ] Write generated Git fixtures for explicit addition, removal/readdition, direct upgrade introducing the target, property/BOM changes and shallow history. RED, implement path-scoped candidate searches and fetch complete bounded historical POM blobs for classification, GREEN. Do not interpret XML diff fragments as reliable dependency context.
- [ ] Do not rely solely on artifactId pickaxe: a version-only change leaves its count unchanged. Search relevant file changes and compare parsed historical declaration states; order/widen searches with recorded coverage. Use current inclusion paths as leads, not proof of historical paths. Tests must place irrelevant later commits around the true introduction.
- [ ] Quote inspected rationale only as untrusted source content, distinguish inferred purpose and author/committer roles, preserve unavailable history. Test sensitive strings in source/commit/metadata payloads do not silently become command arguments or automatic external requests. Bound excerpts and disclose their sensitivity; do not promise arbitrary free-text secret detection.

## Chunk 4: permissioned operations, imports and advisory lookup

**Files:** operations.py, maven.py, advisories.py and corresponding tests.

- [ ] Test proposal creation without executing any probed Maven binary; required disclosure of command/directory, possible writes, destinations/outgoing data, captured project/wrapper/settings identities and unknowns. RED, implement inert proposals, GREEN. Do not persist settings contents or credentials.
- [ ] Test exact operation/run binding, actual host approval reference, single use, changed inputs, denial, sibling rejection, failed spawn and crash. RED, implement atomic consumed+started revision before spawn, GREEN. Missing host approval cannot be synthesized from a repository file. Renew consent for retries.
- [ ] Test subprocess timeout/output limits, sanitized runtime variables, child-process cleanup, non-secret audit fields and unsandboxed-Maven disclosure. RED, implement executor, GREEN. Maven can contact registries through project/settings configuration; disclose configured destinations or request explicit broader download scope when unknowable. Do not claim a network sandbox this implementation does not provide.
- [ ] Test imported tree JSON/effective POM/classpath observations, producer/version/context unknowns, conflicting profiles and incomplete formats. RED, implement format-specific parsers using recorded fixtures, GREEN. Missing producer context is partial for current-checkout claims. Distinguish graph roots/realm from classpath location; preserve all available paths, never invent omitted ones.
- [ ] Test exact OSV request, provider-derived applicability, all affected intervals/branch fixes, aliases and pagination where documented, empty/error/denied/unknown-identity results. RED, implement allowlisted structured response mapping with in-memory transport injection, GREEN. Preserve range structures, not unrestricted raw response fields. Never implement LLM version comparisons.
- [ ] Test redirects and destination changes against approval scope: disable automatic HTTP redirects and require a new disclosed request if needed. A caller-supplied test transport is not a production arbitrary-URL option. No network occurs during collect/import/discover.

## Chunk 5: skill integration, delivery and acceptance

**Files:** cli.py, investigate.py, SKILL.md, references/*, tests/test_cli.py, tests/test_e2e.py, vendor/*.

- [ ] Wire verbs and JSON-only stdout. Test every exit class, offline missing-tool behavior, persisted candidate selection and pause/resume. RED, implement minimal dispatch, GREEN. A valid partial report is never represented as complete success by the explanatory skill.
- [ ] Write skill instructions for query clarification, host approval, evidence-cited answer sections, transitive archive-only targets, inference labels and explicit gaps. Test the required permission/Maven-side-effect wording against documented examples, without overfitting all prose.
- [ ] Add final adversarial matrix: encoding-aware XML; cumulative ZIP limits; hostile Git helpers; process/JVM option injection; unknown producer/realm; stale copy identity; changed-evidence propagation; single-use consent/crash; HTTP redirect; secret-bearing fields; prompt-injection strings; duplicate IDs across runs. Each named case gets an independent assertion even if fixture repositories are shared.
- [ ] Run the mandatory end-to-end fixture from a fresh copied skill directory: C absent from application POMs, B introduces C transitively, packaged Boot JAR contains C, B bytecode references C, Git upgrade is correctly labeled candidate introduction. Verify report evidence, identity checks and unknowns. No Maven/network is needed for this offline recorded-evidence run.
- [ ] Build/rebuild the helper through the approved maintainer procedure; compare reproducibility claims and document any nondeterminism honestly. Ship pinned source, exact dependency closure, notices, SHA256 and BUILD-INFO; users must not compile/download at investigation time. Verify every prerequisite and missing-runtime degradation in a clean environment.
- [ ] Run `python3 -m unittest discover -s tests -t . -v` and read complete output. Separately run the approved real-Maven/advisory integration probes; record any withheld permission as incomplete verification. Do not mark release ready when mandatory tests are skipped.
- [ ] Invoke `/simplify` and `/code-review` with high effort on the final implementation. Resolve findings or track explicit accepted deferrals, rerun changed tests, and obtain final-state review approval. Update Backlog tasks with evidence, then offer the verified result. No commit/push without authorization.

## Dependency order and backlog handoff

Chunk 0 is the critical-path spike. Chunk 1 provides safety/report/storage foundations. Chunk 2 enables real identity and discovery. Chunk 3 depends on captured inputs and completed helper proof. Chunk 4 depends on run storage and context; its advisory tests can be developed independently using injected responses. Chunk 5 integrates only completed foundations. Do not expose an execution command before single-use permission tests pass.

Create one implementation task per chunk, with fixture subsets in its acceptance criteria and inherited project DoD. Keep TASK-3 through TASK-9 intact for deeper capabilities. Raw diagnostic capture remains deferred and must have its own task before claiming the full optional diagnostic feature is delivered. This plan is an implementation artifact, not a claim that its proposed tests or application already work.
