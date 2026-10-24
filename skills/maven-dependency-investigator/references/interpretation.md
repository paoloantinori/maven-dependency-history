# Interpretation Guide

Companion to [../SKILL.md](../SKILL.md). Rules for reading investigator evidence and turning it into the user-facing answer without upgrading, merging, or inventing claims.

The schema and collector behaviors described here are implemented in `scripts/mdi/`, with all seven CLI verbs including consent-gated Maven and advisory execution. The initial delivery passed its recorded reviews and tests. The CLI now selects file-backed isolated archive inspection; TASK-25 records completed archive-extension gates. The status section of SKILL.md and [archive coverage and limits](cli.md#archive-coverage-and-limits) distinguish current implementation from final acceptance and from legacy direct-library behavior.

## 1. The report and its identifiers

Report shape, validated field by field by `scripts/mdi/report.py`:

- report: `schema_version`, `run_id`, `revision`, `query`, `selection`, `subjects[]`, `evidence[]`, `analyses[]`, `stages[]`, `operations[]`, `qualifications[]`
- subject: `id`, `realm` (`project` | `build-plugin` | `unknown`), `module`, `coordinates?`, `owning_plugin?`, `build_context?`, `packaged_copy?` (`archive_digest`, `entry_path`)
- evidence: `id`, `category`, `subject_id?`, `source_locator`, `observed_at`, `captured_identity?`, `producer?`, `producer_version?`, `generation_context?`, `payload`
- analysis: `id`, `evidence_ids[]`, `statement_kind` (`observation-summary` | `inference`), `limitations[]`
- stage: `name`, `outcome` (`complete` | `partial` | `unavailable` | `denied` | `failed` | `not_applicable`), `coverage`, `reasons[]`, `evidence_ids[]`
- operation: `id`, `run_id`, `scope`, `input_identities`, `tool_identity`, `disclosure`, `state` (`proposed` | `denied` | `consumed` | `finished`), `audit`, `evidence_ids[]`
- qualification: `affected_evidence_ids[]`, `kind`, `reason`, `observed_at`

Identifiers are run-local. Two runs may each contain an evidence ID `E1`; those are different evidence and are never combined. References resolve only inside one report; validation rejects dangling and cross-run references. Revisions are complete immutable snapshots: a later revision adds records, never rewrites an earlier observation, and existing evidence, subjects, and qualifications cannot be removed or modified. Existing analyses may gain required limitations when later qualifications affect their evidence; their statements and evidence references stay unchanged.

## 2. Observation types and proof strength

| Observation | What it is | Proves | Does not prove |
|---|---|---|---|
| POM declaration (`discover`) | Raw text of a dependency, dependencyManagement, or plugin entry in one module POM | The text exists in that POM at the inspected revision | Resolution, profile activation, inheritance, or the final graph |
| Imported report (`maven`) | Producer output the user already generated (pinned: dependency-plugin 3.11.0 tree JSON and classpath, help-plugin 3.5.2 effective POM, requested with verbose origin comments) | What that producer reports for its recorded context: explicitly attested nonverbose tree inclusion, effective-model declarations with origin comments, or opaque classpath entries | Effective-model declarations do not prove resolved inclusion; a tree is diagnostic verbose unless nonverbose inclusion is explicitly attested; absent origin comments stay absent; unknown or mismatched context does not prove current-checkout state |
| Packaged copy (archive reader) | An entry observed inside a JAR/WAR or nested archive, with path, digest, identity candidates and coverage | Completely captured bytes sit at the recorded archive location | Which build produced the enclosing archive, or that a partial digest identifies the full entry |
| Class references (`usage`) | One analyzed binary names a class | A static class reference exists (see section 4) | Execution, reachability, or removability |
| History event (`history`) | A classified declaration change at a commit (see section 5) | The observed POM snapshot changed at that commit | Decision authority or the project's first occurrence |
| Advisory page (`advisories`) | One recorded OSV response page for an exact queried identity | Provider-reported advisories for the queried package at retrieval time | Safety, or coverage of unfetched pages (see section 6) |

Declaration caveats: coordinate strings are preserved exactly as written; unresolved `${...}` placeholders are not known identity and never satisfy a qualified query. A declaration seen only under `dependencyManagement` or a BOM does not establish inclusion. It also does not prove absence from a resolved graph or packaged output; a management entry is not an inclusion edge. A dependency tree imports as diagnostic verbose by default: in that reading node inclusion is unknown and no resolved inclusion is computed, and the authoritative nonverbose reading applies only when nonverbose inclusion is explicitly attested through `--tree-mode nonverbose` or a provenance `format` value of exactly `nonverbose`; a flag contradicting an attested format is refused, never silently resolved. An explicit JSON `null` for producer or version in supplied provenance is treated exactly like an omitted field: the value stays unknown and is never presented as attested.

Candidate matching (`candidates`): bare queries match observed coordinate text and archive entry filenames; qualified `g:a[:v]` queries require known coordinates. `pom.properties` identity is `metadata-candidate`, never verified identity. A qualified query against an archive without usable metadata records at most a basename corroboration, marked `ambiguous-possible`. Filename or coordinate resemblance is candidate association only, never identity. Package copies never merge across paths or nested chains, and conflicting metadata candidates are all preserved.

Archive coverage is mode-specific. The CLI records `isolated-file`, its actual `reader_limits`, a shared archive-stage deadline and per-attempt pipeline observations. Direct `discover_candidates` calls without a scratch base retain `bounded-memory`; their lower limits and ZIP64 refusal are not the CLI defaults. Neither backend's unsupported, timed-out or quota-refused archive proves that a dependency is absent.

Keep capture, inspection and cleanup separate. A complete outer-file digest can coexist with partial nested inspection; it does not authenticate missing entry metadata. An entry exceeding the 1 MiB metadata-retention cap may still have a full content digest, but its truncated metadata is not parsed into coordinates. A changed nested spool invalidates affected digests and candidate claims rather than mixing old bytes with new metadata. Cleanup uncertainty can make the pipeline partial even when the archive was inspected: retain the valid observation with its limitations, and report the retained scratch rather than calling the whole operation complete.

The shared 120-second stage clock enforces hard interruption of child decompression, not hard interruption of parent filesystem IO, process creation or JSON handling. Report deadline and resource limits as coverage constraints, not universal runtime or memory-performance guarantees. XML beyond 64 element levels is rejected before tree construction across discovery, history and effective-POM import; this is an input restriction, not evidence of malformed Maven semantics or missing dependencies.

## 3. Digests, byte identity, and build provenance

Three claims, in increasing strength. Keep them verbally distinct in the answer.

1. **Candidate association.** Coordinates or a filename resemble the subject. Weakest; never word this as identity.
2. **Byte identity.** The full-content SHA256 of a completely captured packaged entry equals the full-content SHA256 of a completely captured resolved artifact. A truncated capture cannot support it. Transformed or shaded content never inherits it from coordinate matching, even on equal digests.
3. **Built from this checkout.** Requires build provenance linking the enclosing archive digest to the inspected revision and context, on top of byte identity and graph attribution. Even matching bytes do not prove the enclosing archive was built from this checkout.

`correlate_artifact` returns a `kind` of `byte_identity`, `graph_attribution`, `built_from_checkout`, or `unresolved`, with `byte_identity`, `graph_attribution`, and `built_from_checkout` flags plus `checks`, `unknowns`, and `conflicts`. Report the flags and the unknowns; unknowns and conflicts are part of the answer, not noise. A dirty checkout against clean provenance is a conflict, not a pass. Same coordinates with different content support candidate association, not byte identity. Identical completely captured bytes inside a stale enclosing archive can still establish byte identity, but not that the enclosing archive was built from the current checkout.

## 4. Class references: what they prove

A class reference proves one analyzed binary names another class. It does not prove the class is executed, reachable, needed, or safely removable.

- Never convert class references into reachability or safe-removal claims. No detected usage never implies safe removal; reflection, service loading, generated code, and missing bytecode are explicit limitations.
- Production and test scopes are separate evidence; never pool them into one claim.
- References originating inside upstream dependencies are the normal way a transitive library is explained when application code never imports it directly.
- Duplicate classes get no arbitrary owner; report the ambiguity.
- Multi-release JAR entries the helper skips are reported as skipped, not hidden.
- Source attachment identity must match before relating source text to a binary. Purpose may be explained by reading surrounding inspected source, but technical purpose inferred from code is a labeled inference; the collector records facts, not business intent.

## 5. History and rationale

Event vocabulary from `history`: `addition`, `removal`, `reintroduction`, `version-change`, `property-change`, `management-change`, `declaration-change`. Events are computed by comparing complete historical POM snapshots (full blobs, never diff fragments) per declaration identity, so a version-only change is still found when the artifactId line never moved.

For transitive targets, `candidate-introduction` events (a direct dependency or build-model change that likely brought the target in) are leads, never resolved or shipped facts. Label them as candidates in the answer.

Bounds on claims:

- Earliest observed addition means earliest in the inspected first-parent history of the supplied POM paths, not the project's first occurrence. A shallow graft boundary keeps parentage unknown and the event unclassified; state the gap rather than aging the oldest event into a "first introduced" claim.
- Author and committer are distinct recorded roles. Neither is approval authority or organizational responsibility; PR approval requires separate evidence. Blame is last-line-change evidence, never original introduction.
- Quoted rationale (commit message, comment, issue text, design doc) is quoted untrusted content, bounded in length, and always distinguished from inferred purpose. No recorded rationale is a valid outcome; say so instead of inventing intent.
- Do not check out old revisions over the working tree or run historical builds.

## 6. Advisories

- Query exactly identified versions as exact. Unknown or conflicting component identity produces an unresolved-security assessment, or clearly separated candidate results; never a clean result. Unresolved version placeholders are never sent as exact queries.
- Applicability is provider-reported. OSV fuzzy-matches the queried version against upstream versions and this collector does not independently evaluate ranges. Preserve every affected interval, branch-specific fix, and alias; never flatten multiple ranges into a single fixed version.
- Results are page-local. A `next_page_token` in a response means more pages exist and were not fetched; that is an explicit unknown in the answer. A follow-up page requires a new proposal and fresh host approval; continuation is never automatic.
- Empty means no known matches from that source at that time. It never means safe. Failed, denied, and unsupported-identity lookups are distinct statuses, never reported as empty or clean.

## 7. Missing states and qualifications

| Stage outcome | Meaning | Answer handling |
|---|---|---|
| `complete` | Finished within declared coverage | Claims allowed within that coverage only |
| `partial` | Some coverage missing (limit reached, unsupported construct, truncated input) | State exactly what was not covered |
| `unavailable` | Prerequisite absent (tool missing, no Java, no history) | Name the missing prerequisite |
| `denied` | Host refused consent | Report as denied, never as clean or complete |
| `failed` | Attempted and errored | Report the failure |
| `not_applicable` | Stage does not apply to this subject | State it briefly |

An outcome describes completion within declared coverage, never global certainty. No-match is no match within inspected coverage, not proof of absence.

Qualifications attach to affected evidence: a changed-input qualification marks evidence whose source changed between reads, and an unknown-provenance qualification marks imported evidence without verified generation context. The CLI import step produces two concrete provenance qualifications: `assumed-producer-grammar` (producer and version were not attested, so the pinned parser grammar was assumed and the real generating producer stays unknown) and `caller-attested-provenance` (supplied provenance was not verified against the generating process). Every analysis and every answer statement citing qualified evidence inherits the qualification and cannot present it as the current file state. Later observations never silently replace earlier evidence. Imported evidence also carries a `currency` status; because the CLI supplies no current build context for comparison, currency is `unknown`, so imported evidence never supports an unqualified current-checkout claim.

## 8. Assembling the answer

After a short lead explanation, provide these sections in order, all of them, even when a section's content is mostly its gaps:

1. **Inclusion paths.** Direct or transitive path(s), realm, module, and scope, with evidence IDs. A management-only presence is stated as management, not inclusion.
2. **Version, scope, and model evidence.** Selected version, parent/BOM/property/profile evidence, and an explicit qualification when no verified effective model exists.
3. **Usage and purpose.** Class-reference evidence with its scope; purpose clearly split into observations versus labeled inference.
4. **History and rationale.** Introduction and upgrade events with commit attribution, author and committer as recorded roles, quoted versus inferred rationale.
5. **Security.** Advisory source, exact queried identity, provider-reported applicability basis, page-local unknowns; or the explicit unresolved, denied, or unavailable state.
6. **Unknowns and gaps.** Every partial, unavailable, denied, and failed stage, and every qualification, in plain terms.

Rules: every factual conclusion cites at least one evidence ID from the current run; a partial report is never presented as complete; evidence from a separate run is presented as a separately identified report, never merged; no remediation is proposed or performed.
