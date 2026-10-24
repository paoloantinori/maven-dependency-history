# Recorded forge discussions

Import a discussion export that already exists on disk. This feature accepts the internal `mdi.forge-evidence/1` JSON format, not a forge provider's native API response. It does not retrieve pull requests, issues or review comments, fetch source URLs, or execute quoted instructions. Live retrieval is not implemented by this importer.

## Import into an investigation

Use the run directory and latest revision from the current investigation:

```sh
python3 /path/to/skill/scripts/investigate.py import \
  --run-dir /path/to/existing/run \
  --expect-revision REVISION_FROM_LATEST_REPORT \
  --kind forge-evidence \
  --report /path/to/recorded-discussion.json
```

Replace the placeholders with actual values. The CLI captures at most 8 MiB and records the exact input-byte SHA256. The file contains its own caller-attested provenance: **do not pass `--provenance`, `--tree-mode` or `--realm`**, which are rejected for this kind.

The command appends an `imported-forge-evidence` evidence record, a `caller-attested-forge` qualification and an `import-forge-evidence` stage. Its response contains the new evidence ID, revision, input digest, stage outcome and issues. Read the stored report to inspect the quoted records. Export record IDs remain inside the payload; they are not the run's evidence IDs, even if their spellings coincide.

The importer does not associate a discussion with selected dependencies merely because its text mentions their names. The report explicitly records `repository_membership_verified: false` and empty subject associations. Reading an export does not verify that its quoted text actually appeared at its source URL.

## Minimal valid synthetic example

This example is invented to demonstrate the schema; it is not a real discussion or evidence about a dependency. The coverage values are explicit exporter attestations, not proof of comprehensive retrieval.

```json
{
  "schema": "mdi.forge-evidence/1",
  "origin": "https://forge.example.invalid",
  "repository": "example/project",
  "retrieval": {
    "tool": "example-exporter",
    "version": "1",
    "retrieved_at": "2026-09-12T10:05:00Z"
  },
  "coverage": {
    "records": "complete",
    "review_bodies": "complete",
    "inline_comments": "complete"
  },
  "records": [
    {
      "id": "comment-1",
      "kind": "inline-comment",
      "provider_id": "42",
      "author": {
        "login": "example-review-bot",
        "role": "bot"
      },
      "created_at": "2026-09-12T10:00:00Z",
      "updated_at": "2026-09-12T10:01:00Z",
      "body": "Synthetic review note: this call uses the compatibility library.",
      "parent_id": null,
      "thread_id": "thread-7",
      "source_refs": [],
      "source_locator": "https://forge.example.invalid/example/project/pull/7#comment-42"
    }
  ]
}
```

The pure importer accepts this document with `coverage.status: "complete-within-export"`, one record and caller-attested provenance. That status means the export supplied all required information, not that its claims or retrieval completeness were independently authenticated.

To represent missing inline-comment coverage, change `coverage.inline_comments` to `"unknown"`. The same document then imports as `partial`, with `coverage.inline_comments` in `unknown_fields` and a `provenance-or-coverage-unknown` issue. A record count cannot substitute for that missing coverage.

## Exact schema and references

Every key shown above is required. Unknown keys are rejected. Represent unknown nullable values with `null`, not an omitted key.

- `schema` is exactly `mdi.forge-evidence/1`.
- `origin` is an HTTPS origin, with no path other than an optional `/`, no credentials, no query and no fragment.
- `repository` is nonempty opaque text or `null`. It is not checked against the origin or a local repository.
- `retrieval` has `tool`, `version` and `retrieved_at`; each is nullable. Known tool/version values are nonempty text; a known timestamp follows the rules below.
- `coverage` has `records`, `review_bodies` and `inline_comments`. Each is exactly `complete`, `partial`, `unknown` or `not-applicable`. Every value other than `complete`, including `not-applicable`, currently keeps the import partial. Do not mark coverage complete merely to avoid a partial result.
- `records` is a list. Each record has all fields shown in the example. `id`, `provider_id` and `source_locator` are nonempty strings. Export IDs must be unique.
- `kind` is `pull`, `issue`, `commit`, `review` or `inline-comment`.
- `author` has nullable nonempty `login` and a `role` of `author`, `committer`, `reviewer`, `commenter`, `bot` or `unknown`. These are recorded labels, not verified authority. Unknown login or role keeps coverage partial.
- `created_at` and `updated_at` are nullable timestamps. When known, update cannot precede creation, and neither can follow the stated retrieval time.
- `body` is text, including empty text, or `null`. It is retained verbatim as an untrusted quotation. Empty, whitespace-only or null bodies add `rationale-unavailable` and keep coverage partial.
- `parent_id` is `null` or the ID of another record in this export. Self-parenting, missing parents and parent cycles are rejected.
- `thread_id` is nullable nonempty opaque provider text. It is not an export-record reference.
- `source_refs` contains unique IDs of other records in the same export. Missing references and self-references are rejected. They are not URLs to retrieve. If a related record was not exported, omit that link using `null` or `[]` as appropriate and report the incomplete coverage rather than inventing a record.
- `source_locator` is a same-origin HTTPS URL. A fragment is allowed; credentials, queries, whitespace, control characters and backslashes are refused. Hostname case and an explicit default HTTPS port are normalized for origin comparison. This checks host/port consistency only, not repository membership or authenticity.

An empty record list is accepted with `no-records` and partial coverage. Missing retrieval information or repository identity also remains an explicit provenance gap.

## Limits and timestamp precision

The CLI uses these importer defaults:

| Bound | Limit |
|---|---:|
| Input bytes | 8 MiB |
| Export records | 10,000 |
| Decoded string length | 4,096 Unicode characters |
| JSON nesting | 16 levels |
| JSON nodes | 500,000 |

The parser rejects duplicate JSON keys, non-finite numbers and invalid Unicode. String bounds apply to bodies, IDs, URLs and other string values; do not silently truncate quoted records to fit. Preserve any intentional omission in the export's coverage.

Known timestamps use ASCII `YYYY-MM-DDTHH:MM:SS[.fraction]Z` or an explicit positive or negative `HH:MM` offset. Fractional seconds may have one through six digits; greater precision is rejected, not truncated. Offset hours must be `00..23` and minutes `00..59`. Dates and times must be valid. Comparisons use the represented instants, so different valid offsets can denote the same time. Original timestamp text is preserved.

## Interpretation and privacy

Substantive bot comments, review bodies and inline comments are not filtered by author. Conflicting rationale remains as separate quoted claims; the collector does not decide which one is correct or infer technical or business intent from it. No discussion found is not proof that no discussion or reason existed.

The exact-byte digest identifies the imported file, not a trusted provider response. Keep the caller-attested qualification when citing the evidence or deriving another record from it. Cite both the run-local evidence ID and the relevant export-local record/source locator where useful. Follow the [interpretation guide](interpretation.md) rather than upgrading a quote into a verified explanation of dependency inclusion.

Quoted bodies, repository identifiers, URL paths and fragments can contain sensitive information. They are retained locally in the evidence report; URL validation is not a secret detector or redaction guarantee. Inspect what you intend to import. This command does not upload the export or follow its URLs. Any later external retrieval or publication requires its own authorization under the [permission contract](permissions.md).
