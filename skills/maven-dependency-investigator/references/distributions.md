# Local distributions and OCI layouts

Inspect a distribution or OCI image layout that already exists on disk. These commands never pull images, run containers, execute archive contents, invoke Maven, or build a project. They use trusted isolated readers and produce evidence within explicit coverage limits.

OCI layout support is currently marked `provisional-synthetic-fixtures`: the implementation has generated-fixture coverage, not blanket compatibility established against external real-world layouts. Raw layer inventories and derived visibility are different evidence. Neither is an authenticated build record or a runnable reconstructed filesystem.

## Collect one explicit local input

Use an existing investigation with a recorded candidate selection. Start with `discover` and `select` as described in [cli.md](cli.md), then use the actual run directory and latest revision returned by that investigation.

For a tar distribution:

```sh
python3 /path/to/skill/scripts/investigate.py collect \
  --run-dir /path/to/existing/run \
  --expect-revision REVISION_FROM_LATEST_REPORT \
  --distribution /path/to/distribution.tar \
  --distribution-format tar
```

For gzip-compressed tar, pass `--distribution-format tar.gz`. The format is explicit; it is not inferred from the filename. Passing `--distribution-format` without `--distribution` is rejected.

For an OCI image layout directory:

```sh
python3 /path/to/skill/scripts/investigate.py collect \
  --run-dir /path/to/existing/run \
  --expect-revision REVISION_FROM_LATEST_REPORT \
  --oci-layout /path/to/local-image-layout
```

If the index identifies multiple distinct manifests, inspection records the ambiguity rather than choosing the first. Repeat collection at the new revision with an explicit local manifest digest:

```sh
python3 /path/to/skill/scripts/investigate.py collect \
  --run-dir /path/to/existing/run \
  --expect-revision REVISION_FROM_LATEST_REPORT \
  --oci-layout /path/to/local-image-layout \
  --oci-manifest sha256:FULL_64_CHARACTER_LOWERCASE_HEX_DIGEST
```

Replace all placeholders with values from the current investigation and local inventory. `--oci-manifest` requires `--oci-layout` and a lowercase SHA256 descriptor. `--distribution` and `--oci-layout` are mutually exclusive: one distribution or layout is inspected per collection call. Without either flag, this extra inspection does not run. Normal history and optional usage collection still follow; supplying usage options can run the verified Java analysis helper, but never the inspected application.

The collect response includes `distribution_outcome` and the `distribution` stage in addition to the ordinary collection fields. Read the persisted evidence with `report --validate`. Exit zero means a valid command result, not necessarily complete inspection. Stale revisions are rejected before source inspection, and a competing scratch owner produces an explicit conflict rather than an implicit retry.

## What the evidence means

### Distribution inventory

A `distribution-inspection` evidence record contains the worker exchange, inventory, quota observation, cleanup obligations and candidate associations. For tar inputs, the held source file is hashed in bounded chunks and the worker verifies it again. Member indices distinguish duplicate names. Nested JAR/WAR/EAR/ZIP copies retain their archive identity and entry-chain locators.

Associations compare observed Maven metadata with selected candidate coordinates. They are labeled `metadata-candidate-only`; they do not establish component binary identity, dependency resolution or build provenance. A missing, mismatched or ambiguous association keeps coverage partial. The selected subjects are not rewritten, and component realm remains `unknown`: a match does not identify a library as application-owned or container-provided.

The evidence always receives an `unverified-distribution-attribution` qualification. Even complete tar inspection only reports the inspected distribution contents; it does not prove that the current checkout built that file.

### Raw OCI layer inventory

The layout reader validates local descriptor-referenced blob sizes and SHA256 digests. Nested indexes and explicit manifest selection are supported within the limits below. Missing blobs, unsupported media types, inconsistent descriptors or source changes are recorded as gaps or refusals, never resolved by downloading data. Descriptor URLs are not fetched. Configuration output is limited to platform information, not `Env` or `Cmd` values.

Each selected-manifest layer is inspected in manifest order. Raw associations use `association_scope: recorded-layer-members`: a candidate observed in a lower layer remains part of that historical layer evidence even if an upper layer removes it. The raw OCI result has `merged_filesystem: false` and scope `layer-inventory-only`.

### Derived OCI visibility

The CLI also records separate `oci-visibility` evidence referring to the raw distribution evidence. The supported composition covers regular files and directories:

- A valid `.wh.NAME` marker removes the corresponding lower-layer path and its descendants from the recorded visible view.
- `.wh..wh..opq` hides lower-layer children of its containing directory.
- Whiteouts act on lower layers, not additions made in the same layer. Directory-over-directory preserves children; other supported replacements displace the prior path.
- Valid OCI markers are zero-length regular files. A marker such as `lib/.wh.library.jar` is not inspected as an empty JAR. This special handling is enabled only for OCI layers; the same name in an ordinary tar does not acquire deletion semantics.
- Duplicate paths, reserved whiteout ancestors, invalid markers, missing layers, unsupported members or incomplete nested Java identities make the composed view partial. Apparent removals then remain uncertain, and no definitive visible Java candidates are exported.

Visibility can be `complete-within-recorded-layers` for this supported subset. That is not global absence, a runnable merged filesystem, application/container attribution or authenticated image provenance. The overall OCI collection stage remains partial and `merged_filesystem` remains false. Source qualifications are inherited by the derived evidence, which also carries a `recorded-layer-visibility` qualification.

For example, a synthetic lower layer containing `lib/example.jar` and an upper layer containing the valid empty marker `lib/.wh.example.jar` retain the original layer observation but remove its candidate from the supported visible view. This is an explanation of the rule, not output from a user image or proof that any image was executed.

## Supported formats and explicit gaps

Tar input supports ordinary tar and gzip-compressed tar only. PAX headers, GNU long-name/long-link and sparse extensions are unsupported. Links, devices, FIFOs and other unsupported member types are not followed or executed. Traversal-shaped names are rejected, and nonregular entries with nonzero sizes stop inspection rather than consuming a subsequent header as payload. Truncation and unsupported constructs yield partial coverage or a structured failure; later uninspected members cannot be declared absent.

Nested ZIP-based archives support STORED and DEFLATED entries, including bounded ZIP64 inspection. Multidisk, encrypted and other-compression ZIP content remains unsupported. See [cli.md](cli.md#archive-coverage-and-limits) for the archive reader contract. OCI layers support the recognized uncompressed-tar and gzip-tar media types; other layer formats stay explicit gaps.

## Resource limits and lifecycle

Current CLI defaults are fixed by the readers and workers, not inferred from input names:

| Bound | Default scope and limit |
|---|---|
| Tar source | 2 GiB input |
| Expanded distribution data | 2 GiB cumulatively, including skipped member data, tar framing and nested ZIP expansion |
| Member size | 256 MiB |
| Distribution members | 20,000 |
| Nested Java archives / ZIP entries | 32 archives and 20,000 ZIP entries across the distribution |
| Nested ZIP depth | 4 |
| Tar/ZIP metadata and retained metadata entry | 4 MiB metadata budget; nested ZIP metadata retention is at most 1 MiB per entry |
| OCI blobs | 2 GiB per layer blob and 2 GiB total referenced input, including metadata; repeated references consume budget again |
| OCI index metadata | 4 MiB per JSON input, 100,000 JSON nodes, depth 32, text length 4,096, 256 descriptors, index depth 4 |
| OCI layer inventory | 64 layers; 2 GiB expanded, 20,000 members, 32 Java archives and 20,000 ZIP entries cumulatively across inspected layers |
| Retained inventory / worker response | 24 MiB inventory budget; a separate 24 MiB serialized worker-response cap |
| Worker address space | 768 MiB, enforced before inspection; unavailable required controls cause refusal |
| Distribution stage | One shared absolute 120-second budget, starting before source opening and covering hashing and worker startup/inspection |
| Scratch | 8 GiB logical-size quota with a conservative 4 GiB reservation for new work; bounded accounting of at most 10,000 entries |

Visibility has separate limits: 64 layers, 20,000 members, 4,096-character text, path depth 128, 500,000 JSON nodes, JSON depth 32, 24 MiB input and output, and 1,000,000 comparisons. Exceeding a limit does not create a complete view.

The worker can be terminated during child processing, including decompression. Parent filesystem calls, source hashing, process creation, JSON validation and visibility composition are not hard-interruptible. Deadline overruns are checked when control returns; the workers also have a two-second reap grace, so wall-clock return time is not guaranteed to be exactly 120 seconds. The stage budget does not cover the subsequent separate history/usage collectors as one combined deadline.

Scratch is private and outside the checkout; OCI scratch must also be outside the layout. Input access uses nofollow directory traversal and held file or layout descriptors. Source changes invalidate derived identities, but these checks do not establish an atomic snapshot against same-user mutation.

An exclusive lock on the state-base directory is held across scratch creation, quota accounting, worker execution and cleanup. A second cooperating inspection using that base receives conflict exit 3 before inspection. Unknown scratch contents, unavailable accounting or insufficient quota refuse new work. No old files are automatically pruned to make space.

Only an unchanged, empty directory owned by the current operation is removed after confirmed worker termination. Cleanup safety assumes the trusted worker creates no descendant processes and the parent owns child reaping; unsupported external reaping is not made atomic by a check. Unconfirmed termination, retained nested spools or cleanup errors remain in an independent bounded cleanup ledger, with a recovery-root marker when individual details are incomplete. Cleanup obligations survive discarded identity evidence, interruptions and later report-commit failures. Do not delete reported residues merely because an inspection failed.

See [permissions.md](permissions.md) for the general consent and process-lifecycle boundaries, and [interpretation.md](interpretation.md) for evidence qualifications. No image pull, build, container execution, remediation or publication is authorized by local inspection.
