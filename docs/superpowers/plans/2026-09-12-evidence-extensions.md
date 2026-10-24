# Evidence Extensions Design: TASK-3/4/5/6/7/8/9/17

Date: 2026-09-12
Status: Design recommendations awaiting parent review. Writing this document implements nothing and claims no capability.
Tracking: TASK-3, TASK-4, TASK-5, TASK-6, TASK-7, TASK-8, TASK-9, TASK-17 in `backlog/tasks/`
Owned artifact: this file only. No other document, task file, module, or reference was modified by this design pass.

## 0. Scope and honesty statement

This document prepares local design recommendations for the tracked follow-up tasks so the parent can review one coherent staged approach and invoke the required plan approval gates. It is a design, not an implementation: no module listed here exists yet, no test passes because of this document, and no new capability is claimed. TASK-4 and TASK-7 explicitly require design approval before implementation per their task text (updated 2026-09-12 13:07 and 2026-09-10 14:49 respectively), and TASK-17's note (updated 2026-09-12 13:07) states implementation follows a reviewed consent design. Those approvals are inputs this document requests; it does not grant them.

No external research, network access, or Maven execution occurred for this document. Every factual claim about local code, contracts, or fixtures below cites a local file read on 2026-09-12. Claims that come from memory about third-party behavior are explicitly labeled "remembered, not researched" and must be verified against primary sources before any implementation depends on them.

## 1. Grounding: the implemented baseline these designs extend

All citations verified 2026-09-12:

1. Approved specification: `docs/superpowers/specs/2026-09-10-maven-dependency-investigator-design.md`, SHA256 `907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848` (recomputed 2026-09-12; identical to the hash recorded in the implementation plan's authority section, so the spec is unchanged). Its sections "Components and boundaries", "Evidence contract and data flow", "Permissions and failure handling", and "Deferred capability tracking" are the governing contracts for everything below.
2. Implementation plan: `docs/superpowers/plans/2026-09-10-maven-dependency-investigator.md` (2026-09-10), especially "CLI and evidence contract", "Limits and offline posture", and "Dependency order and backlog handoff", which requires each deferred capability to keep its Backlog task before implementation planning.
3. Research: `claudedocs/research_maven-dependency-explanation_2026-09-10.md`, sections 4 (shaded/relocated content and delivered artifact), 5 (limits of usage evidence), 6 (history and reconstruction), 7 (security position and reachability caveats), sources accessed 2026-09-10.
4. Spike evidence: `docs/verification/maven-investigator-spike.md` (2026-09-10): pinned producer behaviors (dependency-plugin 3.11.0, help-plugin 3.5.2, verbose-tree diagnostics, OSV `/v1/query` semantics).
5. User-facing contracts: `skills/maven-dependency-investigator/SKILL.md`, `references/permissions.md`, `references/interpretation.md`, `references/cli.md`, all marked verified 2026-09-12.
6. Modules actually present under `skills/maven-dependency-investigator/scripts/mdi/`: `report.py`, `runstore.py`, `capture.py`, `limits.py`, `xmlsafe.py`, `discover.py`, `candidates.py`, `archives.py`, `correlate.py`, `gitinspect.py`, `history.py`, `staging.py`, `usage.py`, `usage_pipeline.py`, `operations.py`, `maven.py`, `maven_executor.py`, `advisories.py`, `advisory_executor.py`, `advisory_transport.py`, `cli.py`.

Baseline facts each stage builds on:

- Evidence machinery. Reports are immutable per-run revisions with run-local IDs; cross-run merges are rejected and two runs may each contain an `E1` that is never the same evidence (`references/interpretation.md` section 1; plan section "CLI and evidence contract"). The evidence `category` field is unconstrained beyond JSON safety (`mdi/report.py` module docstring, line 14, and required-field list at line 72), so new evidence categories need no schema change; stage names and outcomes ride the existing stage record shape.
- Archive inspection. `mdi/archives.py` already produces, for every entry of every nested ZIP container, a record with `name`, `index`, `size_declared`, `size_actual`, `sha256`, `status`, and compression (`_record_entry`, lines 433-481), retains `META-INF/maven/**/pom.properties` bytes as metadata evidence (regex at line 58), retains nested archive containers for recursion, preserves duplicate entries as distinct records, and refuses ZIP64, multi-disk, encrypted, and non-STORED/DEFLATED entries with explicit statuses. Bounds are cumulative (`ArchiveLimits`, lines 83-117). This per-entry digest inventory is the substrate Stage 1 needs; no new reader is required for it.
- Correlation ladder. `mdi/correlate.py` classifies candidate association, byte identity, graph attribution, and built-from-checkout, with a `transformed` flag that already forbids byte-identity claims on rewritten content (input schema lines 13-21; byte-identity gate lines 270-290). `references/interpretation.md` section 3 fixes the wording rules.
- Imports. `mdi/maven.py` imports exactly three pinned producer formats (`tree-json`, `effective-pom`, `classpath`); provenance is caller-attested, unattested producer identity is qualified `assumed-producer-grammar`, currency is `unknown` (`references/cli.md` import section; `references/interpretation.md` section 7). Provenance already carries `revision`, `dirty`, `working_tree_digest`, `profiles`, `jdk`, `maven`, and `settings_digest`, which is exactly the shape historical snapshots need.
- Consent. `mdi/operations.py` supports exactly two kinds (`maven`, `advisory`) with strict closed scope schemas, single-use durable consumption, and refusal of unknown keys (module docstring and `KINDS` at line 50). `references/permissions.md` fixes disclosure contents, the approval-file rules, and the rule that raw child output is never persisted (only size and digest).
- History. `mdi/history.py` walks first-parent history over caller-supplied POM paths using complete historical blob snapshots (never diff fragments), classifies declaration events (`addition`, `removal`, `reintroduction`, `version-change`, `property-change`, `management-change`, `declaration-change`), and emits `candidate-introduction` leads for transitive targets. `mdi/gitinspect.py` supplies the hardened bounded Git runner.
- Usage. `mdi/usage.py` runs the vendored Java helper over staged copies and yields class-reference evidence with production/test scope only; it contains no call graph and no reachability computation.
- CLI. Seven verbs (`discover`, `select`, `collect`, `import`, `report`, `propose`, `execute`) with four exit classes (`references/cli.md`).

Two NOT_FOUND facts: the plan's file map named `references/evidence.md`, which was never created; that content lives in `references/interpretation.md` sections 1-3. And no OCI image-layout fixture or tar distribution fixture exists anywhere in this repository today, which matters for Stage 4.

Local tarfile verification recorded 2026-09-12, exact and narrow: the installed CPython 3.14.7 `tarfile` source at `/home/linuxbrew/.linuxbrew/Cellar/python@3.14/3.14.7/lib/python3.14/tarfile.py` documents stream modes `r|*`, `r|`, `r|gz`, `r|bz2`, `r|xz`, and `r|zst` (source lines 1930-1935), and a local probe confirmed a `w|zst` stream opens and `compression.zstd` imports on this machine. An earlier draft of this document claimed zstd was absent; that claim came from a faulty API-presence probe and is retracted. Stage 4 still selects plain and gzip streaming only, as a deliberate scope choice rather than a stdlib limitation.

Work still in flight that gates sequencing: TASK-22 and TASK-23 are both In Progress with shared simplify/code-review/full-suite gates pending (task notes updated 2026-09-12 13:05 and 12:47); the parent additionally reports the CLI final review is currently NOT APPROVED with eight bugs in repair (parent message, 2026-09-12). The stages below therefore extend contract shapes (schema fields, outcome vocabulary, scope schemas, exit classes), not provisional correctness claims about the current CLI and executors, and no stage starts before TASK-22/23 closure includes final review approval. TASK-25 large-archive hardening design is In Progress (updated 2026-09-12 13:02); TASK-24 is measurement-only.

## 2. Cross-cutting design rules

1. Every new fact enters through the existing report machinery: one run, one append-only report, run-local evidence IDs, qualifications attached to affected evidence. No second store, no cross-run ID merge, no shared cache. A separate investigation's report may be quoted as a separately identified report, never merged (spec "Evidence contract and data flow"; interpretation.md section 1).
2. Offline default. Stages 1-4 add zero network and zero execution surface. Only Stages 5-7 introduce new operation kinds, each with its own single-use consent, disclosure, and audit under the existing `mdi/operations.py` lifecycle. "No new consent surface" below always means exactly that: no external execution, download, or lookup permission is required for the stage's local reads. It never waives design approval; every stage's new architecture still passes the parent's plan approval gate and the standard Definition of Done gates before implementation.
3. Unsupported is a result, never an absence. Any unsupported format, limit, or missing input yields a partial or unavailable stage with a stated reason (spec: "Reaching a limit yields incomplete coverage, not absence").
4. One recommendation per task is given here. Alternatives were considered internally and rejected; this document does not present option menus.
5. Module budget guard: at most one new `mdi/` module per stage, plus CLI wiring and tests. No framework, no provider registry, no plugin system (spec "Security enrichment"; plan "Arena decisions" rejected a general provider registry).

## 3. Stage 1, TASK-3: offline shaded and relocated candidate matching

Recommendation: a pure matcher over archive entry inventories, with exact per-entry byte evidence kept strictly separate from relocation evidence, and identity never inferred from package names.

Supported initial inputs:

- Shaded side: any ZIP container `mdi/archives.py` already supports (`.jar`, `.war`, `.ear`, `.zip`; STORED/DEFLATED; non-ZIP64), within existing `ArchiveLimits` budgets.
- Reference side: explicit caller-supplied local `.jar` files passed to `collect` (new repeatable `--reference-jar PATH`). No automatic discovery of reference JARs from `~/.m2` or any repository layout: filenames and repository layout are candidate identity at best (spec "Inclusion and packaging"), and reading the user's repository cache silently would be a new implicit-read surface. The user or the run's own packaged-copy evidence supplies references.

Mechanism (new `mdi/shaded.py`, pure, no filesystem access):

1. Take two inspect results (shaded container and reference JAR) and build entry inventories as ordered lists of `(index, path, sha256, status)` records. Inventories are lists, never path-to-digest maps: a map would silently overwrite duplicate entry names, which ZIP permits and `mdi/archives.py` already preserves by keying reads on the ZipInfo index rather than the name. Matching over duplicates is multiset matching on `(path, digest)` pairs, asymmetrically: a reference entry whose multiplicity is not fully matched (two reference entries at one path, only one correspondence found) is a recorded conflict or partial coverage, never silently dropped; a container surplus at a matched path (the container holds more entries at that path than the reference) is a recorded surplus observation, not an automatic conflict, because a shaded container legitimately contains entries beyond the reference.
2. Relocation evidence from byte equality excludes trivial content: an empty or zero-byte entry byte-equals at any path by construction and proves nothing, so basis (b) below requires nonempty byte-equal content under a changed path prefix.
3. Classify each reference entry against the shaded inventory: exact match (path and complete digest equal), byte-equal-under-different-path (complete digests equal, path prefix differs, nonempty content), path-equal-bytes-differ (explicit conflict), absent.
4. Emit per-reference-artifact results: coverage fraction over reference entries, per-classification counts, and an explicit verdict whose strongest form is complete entry-content correspondence: every entry of the reference artifact appears byte-identical inside the container. That verdict is deliberately not artifact byte identity. Whole-artifact byte identity would require the complete container digest to equal the complete reference digest, which a merged, reordered, recompressed shaded container can never satisfy by construction: entry ordering, per-entry compression, container metadata, and additional entries all differ. The verdict therefore states entry-content correspondence with the container-level differences listed as limitations, and the `transformed` semantics of `mdi/correlate.py` (rewritten content never inherits a byte claim) continue to govern any downstream correlation.
5. Relocation evidence comes only from observations, in this precedence: (a) retained `META-INF/maven/<g>/<a>/pom.{properties,xml}` inside the shaded container, which `archives.py` already collects as metadata candidates; (b) nonempty byte-equal entries observed under changed path prefixes; (c) shade relocation configuration observed as raw text in checkout POMs, where the declared original pattern matches reference package paths and the declared relocated pattern matches observed entry prefixes. Absent all three, identity is unresolved. Package-name or path resemblance alone produces no identity claim (task AC 2).

One mechanism note, labeled: shade relocation rewrites bytecode, so genuinely relocated classes are expected to have digests that differ from the originals, and byte-equal-under-different-path is expected mainly for unmodified retained resources and classes outside relocation patterns. That expectation is remembered from the Maven Shade documentation (research source list entry 22 names the page but did not verify rewriting details) and is not load-bearing: classification is driven by observed digests and paths, and fixtures prove the mechanics either way.

Attribution (task AC 3): link the matched copy to an introducing dependency only through imported resolved-graph evidence plus retained metadata coordinates, as candidate association; link it to packaging configuration only through observed plugin-configuration text in inspected POMs, qualified as a configuration observation. Without build provenance linking the enclosing archive to the checkout, neither link is stronger than candidate (correlate.py ladder; spec: "Even matching bytes do not prove that the enclosing archive was built from this checkout").

Explicitly unsupported initially: bytecode similarity or constant-pool heuristics for rewritten classes; automatic reference acquisition; ZIP64/encrypted/other-compression containers (unchanged from `archives.py`); multi-release versioned-entry handling beyond what the helper already reports as skipped.

Fixtures and tests (task AC 4): extend the existing deterministic fixture builders (`tests/java_fixtures.py`, `tests/history_fixtures.py`) with a synthetic shaded container assembled from a built fixture library JAR: full retention, partial retention, retained `pom.properties` with relocated package prefixes, stale reference (same coordinates, different version bytes), duplicate shaded copies of one library, conflicting metadata, duplicate entry paths inside one container, container surplus at a matched path, reference multiplicity not fully matched, and zero-byte entries at changed paths. Assertions: the strongest emitted verdict is entry-content correspondence with coverage and container-level limitations, never artifact byte identity; duplicate paths match as multisets; reference unmatched multiplicity is a conflict or partial coverage; container surplus at a matched path is an observation, not a conflict; empty or zero-byte entries never contribute relocation evidence; relocation reported with its evidence basis; no identity from package names alone; duplicate copies stay distinct copies; coverage fraction and partial status recorded; stale reference produces conflicts, not silence.

Approval boundary: no external execution or lookup permission is required; all inputs are local files the caller names, and no new operation kind is introduced. Design approval for the new architecture and the standard DoD gates still apply.

Files: `mdi/shaded.py`, `tests/test_shaded.py`, fixture additions, `cli.py` collect flag plus a `shaded` stage for packaged-copy selections, and a matching interpretation-reference section when the implementation task lands.

## 4. Stage 2, TASK-5: historical recorded graph snapshots

Recommendation: reconstruct history from imported recorded snapshots inside the single investigation run; never merge IDs across runs; never execute historical builds.

Supported initial input: the already-pinned dependency-plugin 3.11.0 tree JSON (existing `import --kind tree-json`), one import per historical snapshot, each carrying provenance whose `revision`, `dirty`, `working_tree_digest`, `profiles`, `jdk`, `maven`, and `settings_digest` record the historical generation context. Provenance remains caller-attested and qualified exactly as today (`references/cli.md` import section). Where the user already possesses recorded trees (CI archives, committed reports), those are the preferred captured evidence; the task text's "prefer captured evidence" is satisfied by imports, not by rebuilding.

Mechanism (extend `mdi/history.py` with a pure `compare_graph_snapshots`): snapshot ordering is established only in one of two labeled ways. First, verified Git ancestry: when two snapshots record revisions that both exist in the local history, their ancestor order is established through the existing bounded Git machinery, and the emitted order carries that verification basis. A snapshot whose recorded revision is missing from the local history (absent, beyond a shallow boundary, or not a commit of this repository) produces an explicit issue on that snapshot and leaves it unordered with respect to ancestry: no ancestor order is ever inferred for it. Second, recorded generation timestamps: these order snapshots temporally only, and every event derived across a timestamp ordering is labeled temporal, not causal; timestamps can tie, be wrong, or be absent, and a tied or missing timestamp leaves the pair unordered. Commit hashes themselves are never treated as sortable chronology, and unordered pairs are reported as explicit ambiguity, never resolved by filename or import order. Only between verified-adjacent snapshots are graph events emitted for the selected subject: graph-addition, graph-removal, graph-reintroduction, graph-version-change, each citing the two snapshot evidence IDs. For transitive subjects (task AC 2), correlate the snapshot revision with the existing Git declaration history: the introducing change is the POM change at or before that revision that added or upgraded an introducing direct dependency, reported as `candidate-introduction`, exactly the existing lead semantics; the target's name never needs to appear in a project POM.

Exact versus reconstruction (task AC 1): snapshot contents are exact historical observations of what that producer reported for its recorded context; any statement bridging two snapshots (intervals, trends) or bridging a snapshot to Git history is labeled reconstruction with its supporting evidence IDs; gaps between snapshots are gaps. Non-verbose trees are authoritative only for their recorded context and verbose trees stay diagnostic-only, per the spike findings (2026-09-10: verbose JSON contains omitted versions with no omission-state attribute).

Unavailable artifacts and repository drift (task AC 3): missing snapshots, unknown provenance fields, and the fact that today's remote repository data may differ from historical are recorded as explicit statements and qualifications; no assumption is made that a today-resolved version matches a historical one (research section 6: today's rebuild is today's resolution).

Historical builds: out of scope for this stage. Any future capability to execute a historical build requires its own explicit permission and isolation from the working checkout (task text), which would be a new operation kind and a separate design; this stage deliberately does not design it.

No cross-run merge: all snapshots are imported into the one run; a separately run investigation stays a separately identified report (interpretation.md section 1; schema rejects cross-run references per plan).

Fixtures and tests: generate two or three synthetic snapshot fixtures at different synthetic revisions with the existing fixture builders, including a target that appears transitively when a direct dependency is upgraded; a pair ordered by verified ancestry (real commits in a generated fixture repository); a pair ordered only by timestamps, asserted to carry the temporal-not-causal label; a snapshot whose recorded revision is absent from the local repository, asserted to produce an explicit issue and to stay unordered by ancestry; an unordered pair asserted to stay ambiguous; rejected unknown producer; verbose-tree diagnostic handling; missing-snapshot gap labeling.

Approval boundary: no external execution or lookup permission is required for import and comparison; design approval and standard DoD gates still apply.

Files: `mdi/history.py` extension, `cli.py` collect stage `graph-history`, `tests/test_history.py` additions.

## 5. Stage 3, TASK-6: imported runtime workload observations

Recommendation: record and interpret imported runtime observations; collect nothing at runtime initially, and keep observed versus unobserved structurally distinct.

Supported initial input: MDI runtime-observation JSON documents supplied through `import --kind runtime-observation`. MDI defines one strict schema and parses no third-party tool formats initially; the caller converts from whatever produced the observation, and the original tool and format are recorded as caller-attested provenance. Schema fields (all validated, bounded): workload identity and free-text description; environment summary; observation window (timestamp or start/end); declared capture method; scope (production or test); subject references (coordinates and observed class or method names); observation kind, exactly one of `used`, `loaded`, `invoked`, `not-observed-in-workload`; and a source locator for each claim.

Semantics: `used`, `loaded`, and `invoked` are workload-local facts with full workload provenance (task AC 1) and are never generalized beyond the observed workload. `not-observed-in-workload` is recorded as exactly that; observed workloads do not establish global non-use (task text). The separation is enforced structurally, not rhetorically: not-observed records carry a mandatory limitation, and the analysis builder API refuses to emit any safe-removal or absence statement citing them; tests assert the refusal (task AC 2). This mirrors the existing rule that no detected static usage never implies safe removal (spec "Usage evidence").

Framework-driven static evidence stays where it is: service descriptors and configuration resources remain static supporting observations; a runtime observation never upgrades a static limitation into a proof.

Explicitly unsupported initially: attaching to running JVMs, agents, any specific profiler or flight-recorder format, and every form of execution. Execution would be a new operation kind requiring its own consent design; none is proposed here, deliberately, until a concrete user demand exists (the same logic TASK-9 applies to CI/MCP).

Fixtures and tests: schema acceptance and rejection matrix; mandatory-provenance enforcement; not-observed refusal at the analysis API; bounded excerpt handling; untrusted-content treatment (strings in payloads never become arguments or requests, matching the existing hostile-content fixture family).

Approval boundary: no external execution or lookup permission is required; offline import only, no new operation kind. Design approval and standard DoD gates still apply.

Files: `mdi/runtime.py`, `cli.py` import kind, `tests/test_runtime.py`.

## 6. Stage 4, TASK-4: distribution tar and OCI layout reads, no pulls

Recommendation (design approval required per task text): read two distribution shapes locally and nothing else; no registry pull, no image build, no export command, ever, inside this collector.

Supported initial formats:

1. Uncompressed tar (`.tar`) and gzip tar (`.tar.gz`, `.tgz`), read in streaming mode with per-member and cumulative budgets; nested ZIP containers located inside are captured bounded and fed to the existing archive inspector unchanged. Grounded in the 2026-09-12 verification against the installed tarfile source (stream modes `r|` and `r|gz` documented at local source lines 1930-1935).
2. An OCI image layout directory the user explicitly names (`--image-layout DIR`): read the layout's index and manifest JSON documents bounded and JSON-safely to enumerate layer descriptors, then stream the referenced layer blobs from the layout's content store as tars per format 1. Label, stated plainly: the OCI layout file structure (index document, blob store layout, manifest media types) is remembered from the OCI image-layout specification and was not verified in this design; external research was out of scope. NOT_FOUND: no real OCI layout fixture exists in this repository today. Before any implementation claim, the structure must be verified against a real locally-present layout, and until then any fixture built for tests is explicitly synthetic and the format support is explicitly provisional in its stage coverage output.

Explicitly unsupported initially (task AC 1): any pull, build, or export operation (task AC 4 is satisfied by absence: no such code path exists, and any future one is a new operation kind requiring approval); docker-save export directories; compression beyond gzip (the local tarfile documents and this machine can open `r|bz2`, `r|xz`, and `r|zst` streams, so bz2, xz, and zstd are unsupported by deliberate initial scope choice, not by stdlib absence); encrypted or signed layer handling; squashed-layer diffing; attribution of container-provided libraries versus application dependencies. That last exclusion is deliberate: distinguishing them requires comparing against a base image that is not available offline, so every found copy is a container-packaged copy with realm `unknown` and the discrepancy versus checkout resolution is reported without invented attribution (task AC 3).

Correlation with build evidence (task AC 2): each nested JAR found becomes a packaged-copy subject through the existing candidate machinery (`mdi/candidates.py` packaged-copy kind), and correlation to resolved evidence reuses `mdi/correlate.py` unchanged: byte identity only on complete digests, checkout-versus-packaged mismatches reported as conflicts.

Budgets: tar traversal reuses `ArchiveLimits`-scale budgets charged across the whole stream; hitting a bound is partial coverage. Large-layer scalability must ride the TASK-25 file-backed, process-isolated substrate decision that is currently in design (task notes 2026-09-12); this stage is sequenced after that decision precisely so the project does not grow a second reader substrate.

Fixtures and tests: a tiny synthetic tar and a tiny synthetic layout fixture built locally; unsupported-compression and truncated-blob cases produce explicit partial results, never absence; traversal-style member names inside tars are recorded but never extracted; no network code exists in the module (asserted by test inventory).

Approval boundary: no external execution or lookup permission is required; offline reads of user-named paths only, no new operation kind. Separately, the task's own design approval gate applies per its text before implementation starts.

Files: `mdi/distarch.py`, fixture builders, `discover`/`collect` input extensions, `tests/test_distarch.py`.

## 7. Stage 5, TASK-17: consent-gated raw bounded capture

Recommendation (reviewed consent design required per task note; the user design approval gate has NOT been granted as of 2026-09-12 and this section is a proposal awaiting it): raw capture is a separate, pre-registered capture approval that must be consumed at source execution start, before any raw byte is retained. Posthoc capture of an already-finished operation is not supported and cannot be: by default no raw bytes survive an execution (size and digest only), so there is nothing to capture from later, and adding default retention to enable later capture is exactly what this design forbids.

Design: a `raw-capture` proposal is created before the source operation executes and binds the run, the source operation ID, the exact target stream (exactly one of Maven child output or advisory response body), and an exact byte bound. Executing a source operation that carries a pending raw-capture proposal therefore requires two approvals consumed at that same execution start: the source operation's own approval, and the capture approval. The capture consent is consumed atomically with the source operation's consumed transition, and the tee into the capture file begins only after that commit; while capture consent is pending, denied, or absent, zero raw bytes are retained and the executor's default output handling (bounded read, size and digest only) is exactly today's behavior. A capture proposal whose source operation already ran, was denied, or was consumed without the capture consent is refused as unusable; renewal means a new source operation with a fresh pair of approvals. The disclosure states, verbatim in the proposal: that raw output is not sanitized and routinely carries secrets in Maven diagnostics (matching the wording basis of `references/permissions.md` "Output handling after a Maven run"); the exact byte bound; the owner-only retention location inside the run directory; and that nothing is ever uploaded. Its scope schema follows the existing closed-field discipline of `mdi/operations.py`: unknown keys refused, no free-form payload fields.

Storage and retention (task AC 1 and 2): one owner-only (0600) file per captured stream inside the run directory, written only after the capture consent is consumed; the report gains only a record with path, size, digest, and cap status; a capped capture is labeled truncated, never complete, consistent with the digest-completeness rules in `mdi/correlate.py` and `mdi/capture.py`. The file lives exactly as long as the run directory; there is no global location; the bytes are never embedded in report JSON and never printed.

Tests (task AC 2): byte-limit boundaries (exactly at bound, bound plus one); file mode and ownership; retention tied to the run directory; no default capture and no upload path (module contains no network code); the ordering invariant itself (no capture file exists, not even partial, while consent is pending, and none is created for a source run without a consumed capture approval); posthoc refusal (a capture proposal whose source already finished is refused); full consent lifecycle (wrong binding, replay, sibling refusal) through the real RunStore.

Approval boundary: this is the first new consent kind on top of the frozen TASK-22 machinery; the parent must review this section and the user must explicitly approve the consent design before implementation. Continuous-backlog instruction is not consent for raw capture, matching the task note.

Files: `mdi/operations.py` kind and scope addition, new `mdi/rawcapture.py`, the executor tee gated by the consumed capture approval, `tests/test_rawcapture.py` plus `tests/test_operations.py` additions.

## 8. Stage 6, TASK-8: authorized forge rationale enrichment

Recommendation: design the operation kind and evidence contract now; implement after Stage 5, network-gated.

Operation kind `forge`: scope is an HTTPS destination with the same structural discipline as the advisory scope (no userinfo, no query, no fragment) plus structured outgoing identifiers: host, repository identifier, and pull/issue/commit identifiers. Private repository identifiers never leave the machine without permission explicitly naming them (task text). Every retrieval is its own single-use approval.

Evidence: `forge-content` records carry response digest, retrieval time, endpoint, and a role-preserving payload: author, committer, and reviewer are recorded as their distinct roles; quoted rationale is bounded untrusted text always separated from inferred purpose, per the spec's history and rationale rules. Missing and conflicting sources stay explicit (task AC 2).

Substantive review content is mandatory, not optional: when a pull request is retrieved, its inline review comments are fetched as first-class content on every retrieval, never reconstructed from counts or summary bodies, and every technically substantive comment is preserved regardless of author or bot status. This is an operator standing rule recorded here so a future implementation cannot quietly ship a counts-only view (task AC 2, "Preserve substantive review content"). Label: the concrete endpoint shapes for review comments are remembered from the GitHub REST API and were not researched in this design; the implementation task must verify current API documentation at execution time, since external research was out of scope here.

Fixtures and tests (later): injected-transport tests mirroring the advisory executor pattern; role preservation; inline-comment completeness; denied and unavailable outcomes distinct from empty; outgoing-data disclosure exactness.

Approval boundary: new network operation kind; design approval before implementation, and every retrieval consented individually.

Files (later): `mdi/operations.py` kind, `mdi/forge.py`, tests with injected transport.

## 9. Stage 7, TASK-7: provider-evidenced function reachability

Recommendation (design approval required per task text): fix the evidence contract now; select no provider and implement nothing until a provider evaluation spike (external research plus permissioned probe) and explicit design approval exist.

Hard rule, restated as design: the collector never computes reachability. Class references from `mdi/usage.py` stay class references; no call graph is ever constructed from them; a package match or a class reference is never exploitability (task text; spec "No function-level exploitability guarantee is made from package matching or class references"). This is also what the research found in every inspected provider: unsupported reflection, no-path-found not establishing impossibility, and untested JAR-reachability contracts (research section 7, accessed 2026-09-10).

Evidence contract when implemented: `reachability` records carry provider identity, provider-reported function or method identifiers, the analyzed scope the provider itself states, links to the existing advisory evidence IDs, the provider's stated coverage limits reproduced verbatim, and retrieval time. Applicability stays provider-reported, exactly as `mdi/advisories.py` keeps version-range applicability provider-reported and never evaluates ranges locally (module docstring). Anything the provider does not state is unknown, and unknowns are part of the record (task AC 1 and 2).

Operation kind: a reachability lookup (destination plus exact coordinate plus optional function identifiers), one response page per approval, with the same redirect and destination discipline as advisory lookups.

Tests (later): recorded-response mapping; unknown, failed, and denied distinct from empty; an explicit assertion that usage evidence never feeds reachability records (no own-graph guessing).

Approval boundary: design approval gate now; implementation additionally blocked on provider evaluation. No provider is named here because none could be evaluated offline, and naming one from memory would violate the project's evidence rules.

## 10. Stage 8, TASK-9: CI and MCP evaluation stays deferred

Recommendation: no implementation, no scaffolding, no evaluation task creation now. Record the decision criteria only: CI evidence capture or MCP access starts only when a named, demonstrated user workflow needs something the bundled CLI and skill cannot do (task AC 1), and any future variant must keep the collector evidence and permission contracts compatible (task AC 2), which this document treats as a standing constraint on Stages 1 through 7 rather than a future retrofit.

## 11. Sequencing and module budget

Order after TASK-22 and TASK-23 close (their shared simplify, high-effort code-review, and full-suite gates, plus review approval):

1. Stage 1, TASK-3. Pure offline adapter on a frozen contract; no dependencies beyond the existing archive inventory.
2. Stage 2, TASK-5. Import plus pure comparison; no new readers.
3. Stage 3, TASK-6. Import kind plus schema; no new readers.
4. Stage 4, TASK-4. Sequenced after the TASK-25 substrate decision so the tar and layer readers build on the chosen streaming or spooling approach instead of growing a parallel one. TASK-24 is measurement-only and blocks nothing here.
5. Stage 5, TASK-17. First new consent kind; lands only after the TASK-22 consent machinery has fully closed and been reviewed.
6. Stage 6, TASK-8. Network operation kind, after Stage 5.
7. Stage 7, TASK-7. Gated on provider evaluation plus design approval.
8. Stage 8, TASK-9. A standing decision, not work.

Rationale: pure offline adapters first because they carry zero new consent surface and are testable immediately against the frozen report and CLI contracts; the reader extension waits on the hardening decision; consent-surface extensions follow consent-machinery closure in increasing order of network exposure; provider-gated work last.

Each stage still requires the parent to update the corresponding Backlog task (this document does not edit Backlog), passes the standard Definition of Done gates, and adds at most one new `mdi/` module with its tests. Stages 1 through 3 are intentionally small; none of them introduces a framework, registry, or second state store.

## 12. Approval boundaries summary

| Stage | Task | New consent surface (external execution/lookup) | Design approval required before implementation |
|---|---|---|---|
| 1 | TASK-3 | none; local reads only | parent plan-approval gate plus standard DoD gates |
| 2 | TASK-5 | none; local reads only | parent plan-approval gate plus standard DoD gates |
| 3 | TASK-6 | none; local reads only | parent plan-approval gate plus standard DoD gates |
| 4 | TASK-4 | none; local reads only | yes, per task text, plus parent plan-approval gate |
| 5 | TASK-17 | new operation kind (pre-registered capture approval) | yes, reviewed consent design; not yet granted |
| 6 | TASK-8 | new network operation kind | yes |
| 7 | TASK-7 | new provider operation kind | yes, plus provider evaluation |
| 8 | TASK-9 | none | decision recorded; nothing to implement |

"No new consent surface" in this table means no external execution, download, or lookup permission; it is never a bypass of design approval, which every stage requires through the parent's plan approval gate.

## 13. Sources

- `backlog/tasks/task-3` through `task-9` and `task-17` (exact texts read 2026-09-12; task-3, task-4, task-17 updated 2026-09-12 13:07).
- `backlog/tasks/task-22` (updated 2026-09-12 13:05), `task-23` (2026-09-12 12:47), `task-24` (2026-09-12 12:58), `task-25` (2026-09-12 13:02), `task-10` (2026-09-12 12:59) for in-flight state.
- `docs/superpowers/specs/2026-09-10-maven-dependency-investigator-design.md`, SHA256 `907304354d889ee88c2af4f9ca622e238344e1dfa7e01b92a4e5bf76cbd51848` (recomputed 2026-09-12).
- `docs/superpowers/plans/2026-09-10-maven-dependency-investigator.md` and `2026-09-10-maven-dependency-investigator-arena.md` (2026-09-10).
- `claudedocs/research_maven-dependency-explanation_2026-09-10.md` (sources accessed 2026-09-10; no re-fetch performed).
- `docs/verification/maven-investigator-spike.md` (2026-09-10).
- `skills/maven-dependency-investigator/SKILL.md`, `references/permissions.md`, `references/interpretation.md`, `references/cli.md` (verified 2026-09-12).
- `skills/maven-dependency-investigator/scripts/mdi/`: `archives.py`, `correlate.py`, `candidates.py`, `maven.py`, `operations.py`, `history.py`, `gitinspect.py`, `usage.py`, `report.py`, `capture.py`, `cli.py` (read 2026-09-12).
- Local tarfile verification: source lines 1930-1935 of `/home/linuxbrew/.linuxbrew/Cellar/python@3.14/3.14.7/lib/python3.14/tarfile.py` plus a local stream-open and `compression.zstd` import probe, run 2026-09-12 on this machine.

Remembered and not researched (must be verified before implementation relies on them): Maven Shade relocation bytecode rewriting; OCI image-layout structure details; GitHub review-comment endpoint shapes; behavior of any reachability provider beyond what the 2026-09-10 research recorded.
