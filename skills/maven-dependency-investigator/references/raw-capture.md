# Optional raw diagnostic capture

Raw capture retains a bounded, unsanitized diagnostic file only when the user separately approves both its source operation and the capture itself. It is off by default. Approval to run Maven or query an advisory provider does not authorize retaining raw diagnostics, and capture approval does not authorize the source operation.

Raw bytes can contain credentials, signed URLs, internal paths, or other sensitive information. The collector does not sanitize, print, embed in structured reports, or upload this raw file. The source operation retains its own disclosed effects: Maven is unsandboxed, and an advisory query still contacts its approved destination. Local capture is not permission to send its contents elsewhere.

## Register before execution

First create the Maven or advisory source proposal using the [CLI reference](cli.md#propose). Do not execute it yet. Read its operation ID and the current revision from the actual command output. Capture registration requires that the source still be `proposed` in the same run.

Then propose capture with an explicit positive byte cap. The accepted range is 1 through 67108864 bytes (64 MiB). The source determines the stream: `maven-output` or `advisory-response`. The collector generates the destination basename; the caller cannot choose an arbitrary capture path through this CLI.

The following commands are templates, not authorization to execute. Replace angle-bracket placeholders with values from the current run. Invoke `scripts/investigate.py` from the skill directory, or use its absolute path.

```text
python3 scripts/investigate.py propose \
  --run-dir <RUN_DIRECTORY> \
  --expect-revision <CURRENT_REVISION> \
  --kind raw-capture \
  --source-operation <SOURCE_OPERATION_ID> \
  --max-capture-bytes <APPROVED_CAPTURE_BYTE_CAP>
```

The proposal is inert: it creates neither a raw file nor a process or request. Its disclosure identifies the source, stream, cap, and generated `raw-<UUID>.bin` destination inside the run directory. Source parameters such as `--candidate`, `--maven`, `--output`, and `--page-token` do not belong on a raw-capture proposal.

## Obtain two actual approvals

Present both disclosures to the user and obtain explicit approval for each exact operation. Only after that approval may the agent create each operation's approval record according to the [approval-file contract](permissions.md#the-approval-file). The CLI never generates approval files. A repository JSON file, a previous operation's approval, or silence is not consent. Do not manufacture affirmative records from the command templates on this page.

Execute the source operation, not the capture operation. Supply the source approval plus both capture flags:

```text
python3 scripts/investigate.py execute \
  --run-dir <RUN_DIRECTORY> \
  --expect-revision <CURRENT_REVISION_AFTER_CAPTURE_PROPOSAL> \
  --operation <SOURCE_OPERATION_ID> \
  --approval-file <ACTUAL_SOURCE_APPROVAL_FILE> \
  --capture-operation <CAPTURE_OPERATION_ID> \
  --capture-approval-file <ACTUAL_CAPTURE_APPROVAL_FILE>
```

Use the latest observed revision, not an assumed increment. Both capture flags must be supplied together. Direct execution of a raw-capture operation is refused. The executor checks both exact approval bindings, the source's pinned inputs and tool identity, and the capture tool identity. Both consumption transitions and their started audits must be published in one revision before the sink opens or the source runs.

A pending capture registration blocks ordinary source-only execution; omitting its flags does not silently discard the registration. An explicitly recorded capture denial permits the still-proposed source to execute without capture using its own valid approval. The CLI does not automatically deny a capture and has no denial verb. Never edit run snapshots to simulate a denial. If a fresh workflow is needed, create a new source proposal with fresh approval instead. Only one capture registration is allowed per source, even if that capture was denied.

## What the file and metadata mean

The sink creates one exclusive, owner-only `0600` file inside the private run directory. It never appends to, resumes, or reopens that capture with old consent. Retained files are not automatically deleted or uploaded. Filesystem permissions and writer coordination are not a sandbox against other processes running as the same user.

Successful publication adds capture metadata and a capture evidence ID to the execution result. `capture_outcome` is separate from the source `outcome`; neither should be substituted for the other. Metadata may include:

- `path`, `bytes`, and `retained` describe the recorded local file and retained byte count. Recovery metadata can be incomplete or unconfirmed.
- `truncated` records whether bytes supplied to the sink exceeded the approved cap. A cap-limited file contains only a prefix and has partial status.
- `stored_sha256` identifies the stored bytes when file verification succeeded. It can be null when verification did not succeed.
- `stream_sha256` is always null. The stored-byte digest does not prove complete source-stream coverage.
- `status` describes capture completeness, while `durability` reports the sink's file-flush result or an unconfirmed outcome. Neither proves source execution succeeded or the evidence/finalization revisions were all published.

Maven capture receives chunks in the parent's pipe-read order. It does not preserve original stdout/stderr write boundaries or reconstruct their original interleaving. Its stored digest must not be confused with the separate process-output digest, which uses stdout bytes followed by stderr bytes. The capture cap is independent of the process output cap: reaching the capture cap alone does not stop Maven or expand its existing limits. A capture write failure aborts the run without retry. Capture writes are synchronous parent filesystem IO, not hard-cancelled filesystem operations.

Advisory capture stores only the bounded response body returned by the transport. It is not a wire trace and does not include request or response headers. Bytes read internally before a transport failure are unavailable to capture; an opened sink can therefore remain an empty partial file. A complete returned body may be captured even when malformed JSON makes advisory parsing fail. Conversely, a local capture write or close failure does not turn an already received response into a network failure: the collector still maps that bounded body and records the capture failure separately. The source outcome can succeed while `capture_outcome` fails; no request is retried. Oversized, interrupted, or cap-limited capture is not complete. A continuation page needs a new source proposal and approval, and a new capture proposal and approval if its raw body is also wanted.

## Failure and recovery

There is no retrospective capture of an already consumed or finished source operation. Both approvals are single-use. Once their consumption revision is published, a later failure or interruption can leave them consumed even if no source process or request began and no file was created.

The sink closes before evidence publication. Evidence publication, source finalization, and capture finalization are separate commits. A crash can leave retained bytes without a completed evidence record, or a finished source with a still-consumed capture. An uncertain commit is not permission to replay. Inspect the current run and any bounded `raw_capture` recovery metadata before describing what remains; a missing final record does not prove that no file exists. An opening or closing failure can leave an empty or partial private file with unconfirmed durability.

A hard crash, forced process termination or power loss while the sink is open can leave `.writer.lock` in the run directory. The sink holds this lock throughout source execution. Reads do not acquire it, but subsequent writes refuse with exit 3 whether the owner is alive or the lock is stale. Fresh proposals in that run are therefore blocked until owner-directed recovery; a retry alone cannot repair this state. The collector never automatically clears locks and has no repair verb.

Before manual recovery, stop all writers for this run and establish that the interrupted executor and its source processes are no longer active. The PID recorded in the lock is bookkeeping, not proof of ownership or liveness; PID reuse and surviving descendants prevent treating a missing PID alone as sufficient evidence. If exclusivity cannot be established, leave the lock intact. Preserve a private backup of the complete run directory, inspect the lock's contents, regular-file identity and ownership, and obtain explicit owner authorization before removing only that inspected stale lock. Do not remove report revisions or raw files, manufacture finalization records, or reuse consumed approvals. Reopen and validate the run after recovery; an unreadable or inconsistent run requires investigation, not further deletion. Lock removal restores the possibility of writes, not proof that the interrupted source finished or had no side effects.

Do not create, rename or copy files into the run directory. In particular, an extra `raw-<UUID>.bin` without its recorded paired consumption makes validation fail, including reads. Keep any exported copy outside the run; do not alter stored evidence to make validation pass.

Do not reuse either approval or append to a retained file. Once the run is writable and validated, a new attempt requires new source and capture proposals and fresh approvals. Capture failure, a timeout, or a retained prefix is not evidence of complete diagnostics, successful dependency resolution, runtime usage, or safety. Maven's produced dependency report still requires its own explicit `import`; the raw diagnostic file does not replace that step.
