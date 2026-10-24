# Maven Dependency Investigator

Date: 2026-09-10
Status: Draft for independent review and user approval
Tracking: TASK-1; implementation planning is TASK-2
Research: claudedocs/research_maven-dependency-explanation_2026-09-10.md

## Goal

A developer asks their existing coding agent why a Maven dependency is present in a local project. The answer connects inclusion paths, version selection, usage and purpose, history and attribution, recorded rationale, and security evidence. It remains useful when the project cannot build and reports missing evidence rather than inventing conclusions.

The local checkout is the investigation entry point, not a POM-only boundary. A dependency absent from every project POM but present transitively in packaged output is a first-class investigation target.

## Approved approach

Ship a skill directory containing SKILL.md, collector scripts, and reference documents. The collector has a command-line interface and is independently runnable and testable. The existing coding agent interprets the evidence and writes the explanation. There is no separate LLM integration, hosted service, or required global CLI installation.

Alternatives considered were an instruction-only skill orchestrating existing commands and a continuously maintained CI dependency-history service. The bundled collector provides a testable evidence boundary without the operational cost of a service. MCP and continuous collection are not prerequisites for the initial experience.

Implementation language, exact CLI syntax, dependency versions, and packaging mechanics are implementation-plan decisions. The implementation plan must confirm a reproducible way to deliver any required Java helper alongside the skill; it must not silently introduce a global installation requirement.

## Initial scope

Cover all requested question categories, with explicit coverage limits:

1. Identify direct and transitive inclusion paths and their module context.
2. Explain available version, scope, parent, BOM, property, and profile evidence.
3. Identify production, test, and upstream-library usage where evidence is available; infer purpose only with cited surrounding code.
4. Locate candidate introduction and subsequent upgrade changes, with commit attribution and recorded rationale.
5. Inspect existing ordinary JAR/WAR archives and Spring Boot nested JARs.
6. Enrich identified versions with known vulnerability information when an authorized source is available.
7. Produce explicit ambiguity, incomplete-analysis, and unavailable-evidence results.

Shaded, relocated, or metadata-stripped copies receive best-effort identification initially. They must not disappear from the explanation solely because coordinates cannot be established. More complete attribution is tracked in TASK-3. Distribution archives and container output are tracked in TASK-4.

The investigator does not remove dependencies, edit POMs, apply upgrades, or create commits. Permissioned Maven execution can create normal build outputs and cache entries; the read-only promise concerns remediation, not an assertion that Maven execution has no side effects.

## User interaction

Input is a repository location and a dependency query. The query can name coordinates, an artifact name, or an observed archive entry. The user may provide a module or existing artifact path. These are filters, not requirements to find an explicit declaration.

The skill first discovers available local evidence without invoking project code. The collector emits candidate subjects; the skill owns clarification. Different groups, versions, dependency realms, module/build contexts, or distinct packaged copies are ambiguity cases unless the supplied filters already select one subject. The skill presents these candidates and asks the user to select a subject or explicitly investigate all candidates. The report records the original query, candidate identifiers, and selection; subsequent collection stays within that selection. Multiple paths to the same subject do not alone require clarification. No match is reported as no match within the inspected coverage, not proof that the dependency is absent everywhere.

The response leads with a short explanation and then covers inclusion, usage/purpose, history/rationale, security, and unknowns. Each substantive factual conclusion references an evidence item. Each inference is labeled and points to its supporting observations.

When additional evidence requires Maven execution or external access, the skill presents the proposed operation and permission request. Denial leaves a partial report with the missing coverage recorded. No mutation or external request is performed while waiting for permission.

## Components and boundaries

### Discovery and context

Enumerate project modules, POM declarations, available dependency reports, compiled outputs, Git availability, and candidate packaged artifacts. Preserve module-specific identities rather than flattening the reactor into one authoritative graph.

Record checkout revision when available, dirty state, inspected file identities, supplied build context, and artifact digests. Detect missing context. Existing build outputs do not become current-checkout evidence merely because they are under target/.

Do not independently reimplement Maven resolution from XML. Before resolution evidence exists, report declarations and candidate relationships as such. Parent/BOM/profile interpretation without a verified effective model remains qualified.

### Inclusion and packaging

Consume available resolved graphs and effective-model origin data. Record each imported report's digest, producer and version, format, generation command/time, and available revision, module, profiles, JDK/Maven, and relevant settings context. Missing provenance fields are explicitly unknown. A parsable report with unknown or mismatched generation context is evidence about that report, not authoritative resolution for the current checkout; the stage remains partial for current-context claims. With permission, obtain fresh evidence using existing Maven tooling. A declaration, resolved graph node, and packaged component are different observation types.

Retain multiple introducing paths when available, selected versions, omitted/conflicting nodes where the source exposes them, scope, classifier, and module. Do not equate a BOM version-management entry with an inclusion edge. Every dependency subject carries a realm: project, build-plugin, or unknown. Plugin subjects additionally identify the owning plugin and execution context when available. Never merge realms or infer application inclusion from plugin-only presence. Initial project analysis may report plugin-path analysis as unavailable, but must preserve known plugin-only observations and that limitation for matching queries.

Archive inspection locates nested dependency JARs and available identifying metadata. Record archive path, nested entry path, digest, observed identity, and confidence. Coordinates and filenames establish candidate matches only. For an unmodified nested dependency JAR, a verified byte-identity link requires its complete content digest to match the resolved artifact's complete digest, with both observations cited. Attribution to the current graph also requires graph provenance matching the inspected build context. Record the correlation kind, linked evidence identifiers, checks performed, and remaining unknowns. Even matching bytes do not prove that the enclosing archive was built from this checkout; that claim additionally requires build provenance linking the enclosing archive digest to the checkout/context. When proof is missing, report observations separately with a candidate association. Transformed or shaded content never inherits an exact-byte claim from coordinate matching. Duplicate versions remain distinct copies.

Inspect archive contents without executing them. Stream inspection where practical; enforce limits on input size, expanded bytes, entry count, nesting depth, and processing time. Limits are explicit documented parameters with tested defaults chosen in the implementation plan. If extraction is necessary, use a private scratch directory and reject traversal, escaping links, and unsafe entry paths. Reaching a limit yields incomplete coverage, not absence.

### Usage evidence

Use existing source references and compiled-code analysis as separate evidence classes. Apache's analyzer and DepClean are evaluated reuse candidates, not assumed interchangeable or accurate without fixtures. jdeps is a complementary class-reference source. The exact adapter choice must be established by the implementation plan and verified against real artifacts.

The initial deliverable must include actual class-reference evidence for supported compiled fixtures, not just a placeholder field. Include references originating in dependencies where available so a transitive library can be explained even when application code never imports it directly.

Separate production and test evidence. Do not turn class references into method-level reachability claims. Reflection, service loading, generated code, and missing bytecode produce explicit limitations. Inspect relevant configuration/resources as supporting evidence; no detected static usage never implies safe removal.

The agent may read surrounding source to explain purpose, but the collector records facts rather than inventing business intent. Source locations must refer to inspected files; source attachment identity must be checked before relating it to a binary.

### History and rationale

Inspect local Git changes relevant to declarations, parent/BOM references, direct dependency upgrades, and relevant usage sites. Keep original addition, reintroduction, current version introduction, and last modification distinct.

Blame is last-line-change evidence, not proof of original introduction or decision authority. Report author and committer as their recorded roles; PR approval and organizational responsibility require separate evidence.

A transitive dependency can be introduced without its name appearing in the application's POM. Investigate the introducing direct-dependency or build-model changes rather than only searching for the target name. Without historical resolved graphs, report candidate introducing changes instead of claiming an exact first resolved or shipped occurrence.

Initial local-history support must be exercised on controlled fixtures containing an explicit addition, removal followed by reintroduction, and a transitive introduction through an upgrade. Earliest observed addition means earliest in the available inspected history, not necessarily the project's original addition. Missing/shallow history and unavailable old artifacts remain gaps. Do not check out old revisions over the user's working tree or execute historical builds automatically.

Recorded rationale comes from inspected commit messages, comments, issues, PR discussions, or design documents. Distinguish quoted rationale from inferred technical purpose. Local history is the baseline; forge enrichment is optional and permissioned. No recorded rationale is a valid outcome.

### Security enrichment

The initial implementation includes one functioning known-vulnerability lookup source whose results are evidence records in the versioned structured report, with deterministic recorded-response tests. Provider selection belongs in the implementation plan. No provider registry or plugin framework is required. Reuse public advisory infrastructure rather than creating a vulnerability database.

Only query an exact identified version as exact. Unknown or conflicting component identity produces an unresolved-security assessment or clearly separated candidate results. Record advisory source, identifiers, affected/fixed version information when available, retrieval time, and coverage/error status. Preserve the provider's applicability result, exact queried identity, and relevant affected-range structures, including disjoint intervals and branch-specific fixes. Identify whether applicability was determined by the provider or a documented ecosystem-aware evaluator. Do not flatten multiple ranges into a single fixed version or let the language model decide Maven version ordering; unsupported applicability semantics produce an unresolved assessment.

No findings means no known matches from the queried source at that time. It does not mean safe. Lookup failure, denied access, and unsupported identity are not empty successful scans. No function-level exploitability guarantee is made from package matching or class references.

## Evidence contract and data flow

The collector returns a versioned structured report. Each evidence record has a stable report-local identifier, category, subject, source locator, observation time, and an observation payload containing the relevant context. Subjects distinguish dependency realm, module, and artifact coordinates, including version/type/classifier when known; packaged copies additionally carry archive identity and nested location.

Analysis results reference existing evidence identifiers. Report validation rejects dangling references. Inferences are represented separately from observations, with supporting evidence and a limitation statement.

Each collection stage has an outcome such as complete, partial, unavailable, denied, failed, or not applicable. Outcome describes completion within declared coverage, never global certainty. Reasons and coverage limits accompany non-complete outcomes. Default reports retain only allowlisted structured fields; raw tool output is not persisted or included in the agent context by default. Optional raw diagnostic capture requires separate explicit consent identifying the potential sensitive content, uses bounded owner-only local files, and never uploads automatically. Pattern-based scrubbing is not a guarantee that raw output is secret-free.

The flow is local discovery, candidate disambiguation, local evidence collection, permission proposals, optional enrichment, report validation, and agent explanation. No enrichment starts merely because a previous different operation was approved. Each investigation has a unique run identifier and one logically append-only report, stored as atomic revisions. Clarification and permission pauses resume that run and preserve candidate selection, evidence identifiers, and prior observations. Allow only one writer per run; a conflicting or stale resume fails without merging reports. Cross-run merging is unsupported initially; any separately returned report must be identified and explained separately. A resume rechecks relevant input identities and preserves changed-input qualifications rather than silently refreshing old evidence.

A run stores only its own report and scratch files in a dedicated local location outside source files by default. No automatic global cache or shared database is required. Derive each content digest from the same bounded captured bytes used for its observation, not from a separate unrelated read. When a stage needs multiple reads of a file, compare their identities; a detected change marks that stage partial and prevents an unqualified cross-read correlation. Affected observations retain their captured digest/time and acquire an explicit changed-input qualification. Conclusions citing them inherit that qualification and cannot present them as the current file state; later observations do not silently replace earlier evidence. This is not a guarantee of an atomic snapshot of the entire checkout. Test changes deterministically between controlled reads rather than relying on timing races. Collector-owned writes must never overwrite unrelated user files or broadly delete target/ or caches. This guarantee does not sandbox approved Maven/project-controlled execution, which can perform broader writes than ordinary build outputs. Disclose that distinction before approval; never describe approval as protection against arbitrary project-code side effects. Output paths and retention are documented by the implementation plan.

## Permissions and failure handling

Local inspection must not invoke Maven, a project wrapper, project scripts, Git hooks, archive contents, or implicit remote fetches. Any Git inspection command must disable external diff/text-conversion helpers and optional write behavior. Treat all inspected content, including POMs, source text, archive metadata, repository documents, commit messages, and downloaded metadata, as untrusted data, not instructions. XML inspection rejects DOCTYPE/DTD declarations and disables external entities, external schema retrieval, and XInclude processing. Parsers must not initiate local-file or network reads from XML content and must bound input size and expansion work. Unsupported or rejected XML yields a partial result with no unsafe-parser fallback.

Even Maven diagnostic goals can load project extensions or download plugins. Therefore all Maven/wrapper invocation requires explicit execution approval, not only compile/package goals. The request names the command, directory, anticipated writes/downloads, and relevant tool provenance. An approval parameter must reflect actual user consent; the agent may not manufacture it. Consent is single-use for one named operation in one run; it is consumed when execution starts, including failed or interrupted attempts. Retrying, restarting after a crash, or observing changed relevant project/wrapper inputs requires renewed approval. Before execution, compare the proposed operation and relevant captured input identities with those approved. Record a bounded non-secret operation identifier, run identifier, consent scope, actual command/directory or destination/payload field names, tool identity, start/end status, and resulting evidence identifiers. Credential values and raw request bodies do not belong in that audit record. These lifetime and audit rules also govern external lookups and optional raw capture.

External lookups require disclosure and approval of destination and outgoing data. Coordinate queries and forge identifiers can be sensitive. No source code, credentials, complete settings files, or environment dumps are uploaded by default. Exclude credential-bearing fields and sensitive settings/environment dumps from structured reports. Apply the explicit-consent diagnostic-capture policy to raw output; do not describe it as sanitized merely because it is stored locally. A local MCP process or script is not presumed offline.

Each collector has timeout and output bounds. Failures are isolated when other evidence remains usable. Invocation-level errors such as invalid input differ from successful partial reports; the CLI contract must make that distinction machine-readable. Never silently fall back from a failed build to an unqualified POM-based resolved graph.

## Verification and acceptance

Tests assert structured evidence and attribution, not exact LLM prose. Fixtures cover direct/transitive paths, multiple paths, management versus inclusion, profiles, multi-module contexts, production/test/upstream usage, supported archives, stale artifacts, explicit addition, removal followed by reintroduction, upgrade-based introduction, missing history, unavailable tools/services, and denied permissions.

Adversarial fixtures cover duplicate versions, unknown coordinates, reflection/service loading, malformed or oversized archives, traversal entries, nested limits, changing files during inspection, secrets in output, instructions embedded in repository data, and hostile Git configuration including .gitattributes-driven textconv and external diff drivers.

Permission tests assert no Maven subprocess or network request during inspection and no action after denial. An approval-scope fixture verifies that only the named operation runs with its approved command, directory, destination, and outgoing data; an unapproved sibling operation never runs under that consent. Security fixtures distinguish empty matches from failed/denied lookups. Report schema tests verify referential integrity and preserve uncertainty. At least one end-to-end fixture demonstrates a dependency absent from application declarations but present in packaged output, introduced transitively and used by an upstream dependency.

Additional contract fixtures cover same-coordinate/different-content archives, unknown or mismatched report producer/build context, identical coordinates in plugin and project realms, multi-match selection and explicit all-candidates selection, report continuity across pauses, duplicate IDs across separate runs, concurrent/stale resume rejection, disjoint advisory intervals and branch-specific fixes, changed-observation qualification in conclusions, consumed approval after failure/crash, changed wrapper inputs before execution, and bounded operation audit records. XML fixtures include local-file entities, remote entities, internal entity-expansion attacks, and XInclude; assert no disclosure or implicit I/O. Prompt-injection fixtures include source comments and archive metadata. Collector-write tests distinguish owned report revisions from unrelated user files; execution-disclosure tests verify that Maven is not described as sandboxed.

Every work item is tracked in Backlog.md. The project Definition of Done includes invoking /simplify, invoking /code-review with high effort, addressing or explicitly tracking findings, and recording applicable verification commands/results. No unperformed gate is checked off. Non-applicable checks require explicit treatment rather than a silent exemption.

## Deferred capability tracking

TASK-3 covers deeper shaded/relocated/metadata-stripped component identification and attribution. TASK-4 covers distribution and container output. TASK-5 covers historical resolved/shipped graph reconstruction. TASK-6 covers dynamic and framework-driven usage evidence. TASK-7 covers vulnerable-function reachability. TASK-8 covers richer forge rationale collection. TASK-9 evaluates continuous history and MCP integration. Further deferred capabilities must receive explicit Backlog.md tasks before implementation planning is approved.

MCP and continuously maintained history remain optional future interfaces. They do not replace the approved bundled-skill delivery. Broad initial question coverage does not imply complete analysis of every framework, artifact format, or historical environment.

## Approval gates

The user approved the experience, bundled-collector approach, permission model, first artifact formats, evidence separation, and testing principles during brainstorming. This written specification still requires independent review and explicit user review. TASK-2 cannot start until the user approves the reviewed written specification. No application implementation has begun. Committing or publishing requires separate user authorization.
