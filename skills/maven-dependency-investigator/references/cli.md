# CLI Reference

Usage of the bundled investigator CLI. The initial delivery passed its recorded reviews and tests. This reference describes the current file-backed archive integration; TASK-25 records completed archive-extension gates. The worked example below records an earlier offline session, not an acceptance run of the new archive pipeline. Entry point, from the repository root:

```text
python3 skills/maven-dependency-investigator/scripts/investigate.py VERB [FLAGS]
```

The CLI writes exactly one JSON document to stdout per invocation (sorted keys, one line) and exits 0, 2, 3, or 4.

| Exit | Meaning |
|---|---|
| 0 | Valid result, including partial evidence and explicit no-match |
| 2 | Invalid invocation, schema violation, or rejected input |
| 3 | Stale expected revision, concurrent writer, or archive scratch busy in another discovery (`RunConflict`) |
| 4 | Unexpected internal failure (sanitized, never a traceback); also the exit when `report --validate` finds a problem, with `ok: false` |

The five offline verbs `discover`, `select`, `collect`, `import`, `report` never invoke Maven, a wrapper, a version probe, project code, or the network. `discover` can run the trusted isolated Python archive reader, and `collect` can run the verified Java analysis helper; neither executes inspected application classes. `propose` appends an inert record and runs nothing. Only `execute` with a real approval file runs an approved Maven child process or performs the approved advisory query.

For explicitly supplied local distribution archives and OCI layouts, see [distribution and OCI collection](distributions.md). These optional `collect` inputs do not download or run images.

## discover

```text
--repo PATH        required; the Maven checkout to investigate
--query QUERY      required, nonempty; bare name or groupId:artifactId[:version]
--module M         optional module filter
--artifact PATH    optional explicit packaged artifact to also inspect
--state-dir PATH   optional; default ${XDG_STATE_HOME:-~/.local/state}/maven-dependency-investigator/runs
```

Validates the state location, creates or opens that state base, and acquires an exclusive nonblocking lock on its held directory descriptor before creating `archive-scratch` or inspecting archives. It retains the lock through capture, worker inspection, quota accounting and scratch cleanup. A competing discovery using the same state base exits 3 before inspection; it is not retried silently. The lock coordinates cooperating local callers, not hostile same-user path replacement or unsupported filesystems.

Inspection does not modify the checkout. Captured archive bytes are written only to private scratch outside it. After discovery, the CLI creates a fresh owner-only (0700) UUID run directory and commits the findings as revision 1. An existing plain state-base directory is accepted; unrelated contents remain untouched. Files and symlinks at the state-base path are refused, and an unrelated directory is never adopted as an existing investigation run. The explicit or default state location must not overlap the checkout, checked on lexical and realpath spellings before creation.

Output includes `run_id`, `run_dir`, `revision`, `candidates` (each with `id`, `kind`, `realm`, `module`, `coordinates`), `no_match`, `selection_required`, `distinct_subjects`, `stage`, and `issues`. A missing module `target/` directory is an explicit `target-missing` issue making the stage partial, still exit 0.

### Archive coverage and limits

The CLI uses `isolated-file` inspection by default: the original descriptor is captured into a private spool, then a trusted `python -B -I -S` worker reads that captured file. This supports ordinary ZIP-based JAR/WAR, nested archives and ZIP64, including supported prepended-data layouts. STORED and DEFLATED entries are supported; multidisk, encrypted, symlink entries and other compression methods remain rejected with explicit issues, not evidence of dependency absence. ZIP central-directory structure and allocation bounds are checked before `ZipFile` constructs its entry inventory. Required runtime capabilities are checked rather than assuming every Python version or platform supports this backend.

| Default bound | Scope |
|---|---|
| 2 GiB outer input | Each outer archive capture |
| 2 GiB expanded bytes | Cumulative within one outer archive's nested traversal |
| 256 MiB entry | Each decompressed entry |
| 20,000 entries | Cumulative within one outer archive's nested traversal |
| Four nesting levels | An outer archive is level zero |
| 4 MiB central-directory metadata | Each ZIP container, including ZIP64 |
| 1 MiB retained metadata | Each metadata entry; larger content does not yield partially parsed coordinates |
| 768 MiB worker address space | Each isolated worker; failure to apply the required cap refuses inspection |
| 24 MiB worker response | Bounded JSON result |
| 32 outer captures | One discovery by default |
| 120 seconds | One shared archive-stage deadline across quota checks, capture and all worker inspections; never reset per archive |
| 8 GiB scratch quota | Existing logical spool sizes plus a conservative reservation for the next outer and nested spools |

Nested container bytes are conservatively charged again before recursion, so the expanded-byte limit is not simply the sum of final extracted payloads. A ZIP64 archive can still exceed the entry or metadata budget. Reaching a limit yields partial, unavailable or failed coverage rather than silently dropping a dependency.

The worker provides a hard interruption boundary for child decompression. Parent-side capture, filesystem and metadata IO, process creation and JSON handling are cooperatively checked, not hard-interruptible. Startup consumes the same stage budget; a bounded two-second worker-reaping grace can extend return latency. Do not describe 120 seconds as a hard bound on every OS operation or on the whole investigation, which also includes non-archive work.

Scratch consists of generated 0600 files inside private 0700 directories under the outside-checkout state base. Quota accounting is bounded and reserves space before capture. Unknown contents, unavailable accounting or quota exhaustion refuse new work; no retained scratch is auto-pruned. Confirmed worker termination and reaping, the trusted-worker-no-descendants contract, closed readers and verified owned file identities are all required before cleanup. Uncertainty retains scratch and reports the remaining paths, subject to metadata bounds. The CLI holds the state-base lock throughout this lifetime. This is not a disk reservation against unrelated processes or an atomic defense against hostile same-user mutation.

**Direct-library compatibility:** `discover_candidates` without `archive_scratch_base` retains the `bounded-memory` backend without implicit scratch writes. Its defaults remain 64 MiB outer input, 128 MiB expanded bytes, 16 MiB per entry, 20,000 entries, four nesting levels, 4 MiB central metadata and a cooperative 30-second per-archive deadline; it rejects ZIP64. Supplying a dedicated private `archive_scratch_base` selects the file-backed backend and `FileArchiveLimits`. The library caller owns outside-checkout placement and exclusive scratch lifetime; it does not receive the CLI's lifetime lock automatically. Coverage records `backend`, `reader_limits`, `deadline_scope` and pipeline observations so consumers can distinguish the modes.

TASK-25 records completed archive-extension gates. The implemented CLI path and measured fixtures do not establish worst-case performance for every archive layout, expanded payload or compression method.

## select

```text
--run-dir PATH          required
--expect-revision N     required; must equal the run's latest revision
--candidate ID | --all  required, mutually exclusive
```

Records the immutable selection. Only candidate IDs from the discovery record are accepted; contextual subjects cannot be selected. A second `select` is refused, and investigating other candidates requires a new run. `--all` selects the frozen set of query-matching candidates recorded at discovery.

## collect

```text
--run-dir PATH             required
--expect-revision N        required
--usage-root PATH          optional; class-usage root input (a built jar or classes dir)
--usage-dependency PATH    optional, repeatable; dependency inputs to analyze against the root
--usage-scope              production (default) or test
--java PATH                optional explicit trusted java executable
--vendor-dir PATH          optional operator override of the vendored helper tree
--staging-base PATH        optional; default is a staging directory next to the run directory;
                           refused (both directions) if it overlaps the checkout
```

Requires a recorded selection. Runs two stages. The history stage walks the selected subjects' POM paths through first-parent history; an archive-only selection with no POM declarations yields `not_applicable` with that stated reason. The usage stage requires both `--usage-root` and at least one `--usage-dependency`, otherwise it records `unavailable` with the reason. Before any JVM start, the vendored helper package is verified: every `vendor/SHA256SUMS` entry is hashed and compared, the classpath closure is cross-checked against the installed `helper/DEPENDENCIES.json` pins and the packaged `vendor/BUILD-INFO.json` digests, and any missing, inconsistent, or malformed piece makes the stage `unavailable`; nothing is ever built or downloaded at investigation time. A failed vendor verification is a usage-stage unavailability only: the history stage's evidence is still collected and committed in the same revision. The staging base, explicit or default, is refused before creation when it overlaps the inspected checkout in either direction, checked on lexical and realpath spellings. The helper analyzes staged copies outside the checkout, never application classes in place.

## import

```text
--run-dir PATH          required
--expect-revision N     required
--report FILE           required; the recorded producer output (bounded capture, 8 MiB)
--kind KIND             required: tree-json | effective-pom | classpath
--provenance FILE       optional JSON object
--tree-mode MODE        optional, tree-json only: nonverbose | verbose
```

Accepted provenance fields, exactly: `producer`, `version`, `generated_at`, `format`, `revision`, `dirty`, `working_tree_digest`, `module`, `profiles`, `jdk`, `maven`, `settings_digest`. Unknown fields are an exit-2 error. Supplied fields are caller-attested only and are qualified as such; producer and version left unattested are assumed from the pinned parser grammar and qualified `assumed-producer-grammar`, so the actual generating producer stays explicitly unknown. An explicit JSON `null` for `producer` or `version` behaves exactly like an omitted field: the pinned grammar still parses, the value stays unknown, and the caller's null remains visible in `supplied_provenance`. Tree mode is conservative: the default reading is the diagnostic `verbose` mode with no inclusion authority, and the authoritative `nonverbose` mode is selected only by an explicit `--tree-mode nonverbose` or a provenance `format` value of exactly `nonverbose`; omitting the flag never silently grants inclusion authority, and a flag contradicting an attested format is an exit-2 refusal, never a silent resolution. The import response and stage carry `tree_mode` plus `tree_mode_attestation` (`default`, `flag`, or `provenance`) for tree-json. The CLI supplies no current build context, so `currency` is always `unknown`: imported evidence is authoritative only for the context its provenance records, never for the current working tree. Each import adds one evidence record plus one qualification.

XML used by discovery, local history and effective-POM import has a fixed 64-element-level ceiling, with the root at level one. The Expat safety pass rejects excess depth before ElementTree construction, alongside the existing DTD/entity rejection and byte limits. Comments and processing instructions do not add element levels; byte-limit overrides cannot relax the ceiling. This is a new accepted-input restriction, not a promise that every valid Maven plugin configuration fits it. Rejected XML remains an explicit coverage or import error.

## report

```text
--run-dir PATH    required
--revision N      optional; default latest
--validate        optional; wrap the stored report with a validation verdict
```

Validation checks schema, references, unique IDs, and the revision transition chain. A valid check is `ok: true` exit 0; a problem yields `ok: false` and exit 4 with the problem string.

## propose

```text
--run-dir PATH             required
--expect-revision N        required
--kind KIND                required: tree | effective-pom | classpath | advisory | raw-capture
--candidate ID             source kinds: required; must be part of the recorded selection
--source-operation ID      raw-capture only: required, still-proposed source in this run
--max-capture-bytes N       raw-capture only: required, 1 through 67108864
--maven PATH               Maven kinds: required, exact absolute executable path
--output PATH              Maven kinds: required, absolute output file path
--input PATH               Maven kinds: required at least one, repeatable, absolute pinned inputs
--destination DEST         Maven kinds: repeatable explicit download destination descriptions
--destinations-unknown     Maven kinds: declare that destinations cannot be enumerated
--output-overwrite         Maven kinds: allow replacing an existing observed output file
--page-token TOKEN         advisory only: continuation page token, nonempty
```

Maven kinds compose argv as `[maven, -B, -N, -f <selected module pom>, <output flags>, <pinned goal>]`, scoping the goal to the selected candidate's module. The module POM is pinned automatically as a required input alongside the explicit `--input` files; a selected module without a `pom.xml` is a clear refusal, never an ignored module. A selected candidate of realm `build-plugin` is refused for these project diagnostics: a project tree, effective POM, or classpath cannot explain plugin-realm inclusion, and no unscoped goal is substituted. The response includes the composed `module` object with its `id` and `pom` path.

| Kind | Pinned goal | Output flags |
|---|---|---|
| tree | org.apache.maven.plugins:maven-dependency-plugin:3.11.0:tree | -DoutputType=json -DoutputFile=OUTPUT |
| effective-pom | org.apache.maven.plugins:maven-help-plugin:3.5.2:effective-pom | -Dverbose=true -Doutput=OUTPUT |
| classpath | org.apache.maven.plugins:maven-dependency-plugin:3.11.0:build-classpath | -Dmdep.outputFile=OUTPUT |

The effective-pom goal requests verbose origin comments (`-Dverbose=true`), which is where effective-model origin evidence comes from; absent comments stay absent.

Download destinations must be declared one way: either `--destinations-unknown` or at least one `--destination`; passing both is an error, and absent flags are an invocation error, never an implicit unknown. The output target's prestate is observed and bound into the proposal: an absent target is disclosed as possibly created; an existing target refuses the proposal unless `--output-overwrite` records that the user explicitly approved replacing that observed file. Advisory proposals accept no Maven flags and require an exactly known `groupId:artifactId:version` observed at discovery; raw `${...}` placeholders are never resolved values. The proposal output ends with the reminder that the CLI never creates approval files. See [permissions.md](permissions.md).

## execute

```text
--run-dir PATH        required
--expect-revision N   required
--operation ID        required
--approval-file FILE  optional; without it, diagnostic operations (maven
                      tree/effective-pom/classpath and advisory lookups) run
                      under the standing policy, audited as such. A real file
                      with approved:false denies. Raw capture approval is
                      always a real file.
--capture-operation ID         optional; requires --capture-approval-file
--capture-approval-file FILE   separate actual approval for the registered capture
```

Raw capture must be registered before source execution. Execute the source with both capture flags, never the capture directly. Both approvals are single-use and consumed atomically before any capture or source execution. See [raw-capture.md](raw-capture.md) for the complete workflow and recovery limits.

Routes by the stored operation kind. A Maven execution reports the process status and output metadata (size, and sha256 when the bounded capture was not capped); raw child output is never printed or embedded in JSON, and is not persisted unless separately approved raw capture is requested, and the response carries the explicit note that importing the produced output is a separate `import` with its own `--report` and `--provenance`. An advisory execution reports the one fetched page; `next_page_token` means a continuation page exists and was not fetched, and fetching it requires a new proposal with `--page-token` and fresh approval.

## Worked example

Recorded offline session from the initial delivery (2026-09-12), before the file-backed archive integration, on a scratch checkout with a root POM declaring `com.example:useful-lib:1.2.3` and one Git commit. Only the five offline verbs ran: no Maven, no wrapper, no network, no approval. It illustrates run and revision continuity, not the new archive backend's acceptance. JSON is trimmed with `...`; the revisions, IDs and keys below are the recorded values, not identifiers to reuse in another run.

The state directory and every scratch location must be disk-backed. The CLI refuses a state or run directory on tmpfs or ramfs: archive spools and scratch written to a memory-backed filesystem consume RAM directly, up to the 2 GiB per-input and 8 GiB quota defaults. On systems where `/tmp` is tmpfs, use the default state location or a path under `/var/tmp`; the example below uses `/var/tmp` for that reason.

```console
$ INV discover --repo /var/tmp/mdi-doc2 --query useful-lib --state-dir /var/tmp/mdi-doc2-state
{"candidates": [{"coordinates": "com.example:useful-lib:1.2.3", "id": "mdi-821866e3a212b06d",
  "kind": "declaration", "module": ".", "realm": "project"}], ...,
 "revision": 1, "run_dir": "/var/tmp/mdi-doc2-state/27c055ca-e914-4d3a-a37f-edd024133504",
 "selection_required": false, "stage": {"name": "discover", "outcome": "partial"},
 "issues": [{"code": "target-missing", ...}]}            # exit 0

$ INV select --run-dir /var/tmp/mdi-doc2-state/27c055ca-e914-4d3a-a37f-edd024133504 \
    --expect-revision 1 --candidate mdi-821866e3a212b06d
{"revision": 2, "selection": {"candidate_ids": ["mdi-821866e3a212b06d"], "mode": "single"}, ...}

$ INV import --run-dir ... --expect-revision 2 --report /var/tmp/mdi-doc2-tree.json \
    --kind tree-json --provenance /var/tmp/mdi-doc2-prov.json --tree-mode nonverbose
{"evidence_id": "E3", "provenance_attestation": "caller-attested", "currency": "unknown",
 "tree_mode": "nonverbose", "tree_mode_attestation": "flag",
 "supplied_provenance": {"format": "nonverbose", "producer": "maven-dependency-plugin",
  "version": "3.11.0", ...}, "revision": 3, ...}        # exit 0

$ INV collect --run-dir ... --expect-revision 3
{"history_outcome": "complete", "revision": 4,
 "stages": [{"name": "history", "outcome": "complete"},
            {"name": "usage", "outcome": "unavailable"}], ...}

$ INV report --run-dir ... --revision 4 --validate
{"ok": true, "validation": {"valid": true, "problem": null, "revision": 4,
 "checks": ["schema", "references", "unique-ids", "transition-chain"], ...}}
```

Continuing with a stale expectation is the documented conflict, not a silent retry:

```console
$ INV select --run-dir ... --expect-revision 1 --candidate mdi-821866e3a212b06d
{"error": {"exit_code": 3, "message": "stale expected revision 1: run 27c055ca-... is at
  revision 4; re-read the run and retry with the current revision"}, "ok": false}
```

The imported tree is an explicitly synthetic sample in the pinned grammar, used only to exercise the mechanics offline; in real use the `--report` file is the output produced by an approved and executed Maven operation, and its provenance should record the actual generation context rather than hand-written values. Every `--expect-revision` above was read from the previous command's output, never guessed.
