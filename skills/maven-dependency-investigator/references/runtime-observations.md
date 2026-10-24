# Recorded runtime observations

Import observations that a caller has already recorded for a specific workload. This feature reads only the internal `mdi.runtime-observations/1` JSON schema. It does not attach to a JVM, run a workload, install instrumentation, read a profiler's native format, or query a provider. Convert external trace formats separately and preserve the original observer and source locators in the document.

## Import into an investigation

Use the existing run directory and its latest revision. Discover and select candidates first when you want the imported observations compared with those subjects.

```sh
python3 /path/to/skill/scripts/investigate.py import \
  --run-dir /path/to/existing/run \
  --expect-revision REVISION_FROM_LATEST_REPORT \
  --kind runtime-observations \
  --report /path/to/recorded-observations.json
```

Replace the paths and revision placeholder with actual values from this investigation. The command captures the file with an 8 MiB limit and records its exact-byte SHA256. It never fetches a `source_locator` or executes text from the document. Runtime provenance is embedded and caller-attested: **do not pass `--provenance`, `--tree-mode` or `--realm`**, which are rejected for this import kind; each observation carries its own realm.

The result names the new `evidence_id`, revision, stage outcome and issues. The stored evidence category is `imported-runtime-observations`; its payload contains the imported observations and per-observation associations with the recorded selection. A `caller-attested-runtime` qualification always accompanies the evidence. The capture digest identifies the input bytes, not their authenticity or the identity of a running binary.

## Minimal valid example

This is a synthetic example of the internal format, not a trace obtained from an actual workload. It deliberately leaves the environment fingerprint unknown, so its import coverage is partial.

```json
{
  "schema": "mdi.runtime-observations/1",
  "observer": {
    "tool": "example-observer",
    "version": "1",
    "format": "example-recorded-trace",
    "capture_method": "caller-converted class-load observation"
  },
  "workload": {
    "id": "request-1",
    "description": "One recorded test request"
  },
  "environment": {
    "summary": "Test JVM",
    "fingerprint": null,
    "unknowns": []
  },
  "window": {
    "start": "2026-09-12T10:00:00Z",
    "end": "2026-09-12T10:01:00Z"
  },
  "scope": "test",
  "observations": [
    {
      "id": "event-1",
      "kind": "loaded",
      "mechanism": "class-loading",
      "observed_at": "2026-09-12T10:00:10Z",
      "source_locator": "recorded-trace.json#event-1",
      "subject": {
        "module": ".",
        "realm": "project",
        "coordinates": {
          "groupId": "example",
          "artifactId": "library",
          "version": "1.0"
        },
        "class_name": "example.Library",
        "method_name": null
      }
    }
  ]
}
```

## Schema rules

Every key shown in the example is required, even when its value is unknown. Unknown object keys are rejected. Unknown information is represented by JSON `null` in the nullable fields, not by omitting the key.

- `schema` must be exactly `mdi.runtime-observations/1`.
- `observer` requires `tool`, `version`, `format` and `capture_method`. Each is a nonempty string or `null`.
- `workload` requires `id` and `description`, each a nonempty string or `null`.
- `environment` requires `summary` and `fingerprint`, each a nonempty string or `null`, and `unknowns`, a list of nonempty strings. The fingerprint is opaque caller-supplied text; it is not checked against a machine or runtime. A nonempty `unknowns` list keeps coverage partial.
- `window` requires `start` and `end`, each a supported timestamp or `null`. When both are known, the end must not precede the start.
- `scope` is exactly `production` or `test`. It describes the recorded workload and is preserved, not inferred from a directory name.
- `observations` is a list. Each observation has a unique, nonempty string `id`, a nonempty string `source_locator`, a `subject`, and the following fields:
  - `kind`: `used`, `loaded`, `invoked`, or `not-observed-in-workload`.
  - `mechanism`: `class-loading`, `service-loading`, `reflection`, `native`, or `other`.
  - `observed_at`: a supported timestamp or `null`. A known timestamp must fall within each known endpoint of the window, inclusively.
- `subject` requires `module`, `realm`, `coordinates`, `class_name` and `method_name`. `module`, `class_name` and `method_name` are nonempty strings or `null`. `realm` is `project`, `build-plugin` or `unknown`. `coordinates` is either `null` or an object with exactly `groupId`, `artifactId` and `version`, each a nonempty string or `null`.

Missing provenance, unknown realm or coordinates, missing class identity, and coordinate placeholders or whitespace keep coverage partial. A null method name is permitted without a missing-method qualification for class-level claims, but an `invoked` claim without a method name is partial. An empty observation list is valid input with an explicit `no-observations` issue, not evidence of non-use.

## Timestamp and size limits

Timestamps use ASCII digits in `YYYY-MM-DDTHH:MM:SS[.fraction]Z` or `YYYY-MM-DDTHH:MM:SS[.fraction]+HH:MM` form; negative offsets are also accepted. Dates and times must be valid. Fractional seconds contain one through six digits. Greater precision is rejected with `unsupported-timestamp-precision`, never silently truncated. Offset hours must be `00..23`, and offset minutes `00..59`; invalid components are rejected rather than normalized. Known timestamps are compared as instants, accounting for their offsets. Original spellings remain in the recorded document.

Default limits used by the CLI are:

| Bound | Limit |
|---|---:|
| Captured input | 8 MiB |
| Observations | 10,000 |
| Decoded string length | 4,096 Unicode characters |
| JSON nesting | 16 levels |
| JSON validation nodes | 500,000 |
| Selected subjects multiplied by observations | 100,000 pairs |

Object keys also count toward the string and validation-node bounds. The parser applies byte and lexical bounds before decoding, then validates the decoded structure before producing copies. Escaped surrogate pairs and raw UTF-8 receive the same decoded character limit. JSON integers additionally have a 4,096-digit cap and may encounter a tighter interpreter integer limit. These are internal-schema bounds, not claims about any external trace format. The CLI offers no runtime-import limit override.

Duplicate JSON keys, nonfinite numbers, invalid UTF-8, unpaired Unicode surrogates, duplicate observation IDs, invalid timestamps, schema violations and exceeded limits are rejected. The CLI reports invalid imports as exit 2. A valid import with partial coverage returns exit 0 and names the gaps; that exit code does not mean complete evidence.

## Associations and interpretation

The CLI compares each observation's caller-attested coordinates, module and realm with the selected discovery subjects. Association status is `candidate-match`, `ambiguous`, `unknown`, `mismatch` or `unselected`. Anything other than a unique `candidate-match` keeps the import stage partial. Even a match is a metadata candidate association, not verified binary or build identity. Production and test scope remain workload provenance; neither scope upgrades a candidate association.

A complete stage means only complete coverage within the recorded workload and supplied metadata. It does not authenticate the observer or establish that the same binary was packaged or executed by the current checkout.

`not-observed-in-workload` carries an explicit limitation: not observed in this workload does not prove absence or safe removal. The same caution applies to an empty trace. `used`, `loaded` and `invoked` remain the observer's workload-local claims, not independent proof of execution, universal reachability or dependency necessity. Static service descriptors and configuration cannot be promoted to runtime observations merely by selecting a mechanism label.

Do not derive global non-use, safe removal, vulnerable-function reachability or exploitability from these imports. The library's summary API accepts only `observation-summary` and refuses absence, removal and exploitability modes. When answering, cite the current run's imported evidence and its qualifications; observation IDs are local to that imported document and are not report evidence IDs. See the [interpretation guide](interpretation.md) for the broader evidence rules.
