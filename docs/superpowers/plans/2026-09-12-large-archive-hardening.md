# Large-archive and decompression hardening: bounded design proposal (TASK-25)

Status: DESIGN AND SPIKE ONLY, 2026-09-12, revision 5 (revision 1 received
by the coordinator as hash 6ef10552; revisions 2 through 4 applied
coordinator corrections; this revision applies one mandatory factual
correction: the Expat-ceiling rationale "every current caller already
rejects deeper" was false and is replaced by the accurate new-input-
restriction statement with a compatibility fixture requirement; the
recommended ceiling of 64 is unchanged). The parent has verified the plan
hash d0a6aa4a (revision 4), the E7 embedded-signature
counterexample and ZIP64 structural-walk results. TASK-25 remains open; no
production code, test, tracker item, or configuration was changed by this
document, and nothing in it is complete
work. Every numeric observation below was measured locally by the working
agent on this machine (commands, logs, and dates cited per row) and is
PENDING INDEPENDENT RE-RUN by the parent before being accepted as evidence.
Every choice that touches the approved capacity numbers (plan
`docs/superpowers/plans/2026-09-10-maven-dependency-investigator.md`,
section "Limits and offline posture": archive input 2 GiB, expanded 2 GiB
across traversal, entry 256 MiB, 20,000 entries, depth 4, archive stage 120
seconds) is a recommendation behind a user gate: implementing it requires
the same review that approved those numbers. Nothing here replaces or
silently reinterprets the approved plan; the current staged defaults in
`mdi/archives.py` (64 MiB input / 128 MiB expanded / 16 MiB entry / 30 s)
remain in force until that gate.

## 1. Problem

`mdi/archives.py` inspects one packaged ZIP copy by holding its full input
bytes in memory (`mdi.capture.capture_file`), retaining each nested archive
as an in-memory `bytearray` (`_read_stream` with `want_archive`), and
enforcing the deadline cooperatively between 64 KiB chunk reads. That design
cannot reach the approved capacity: 2 GiB input bytes plus up to 2 GiB of
retained nested containers in the parent process, with a deadline that
cannot interrupt a native decompressor call at all. TASK-25 also carries two
review findings: the EOCD entry-count lie does not bound `ZipFile`'s
pre-check allocation, and a raised-depth XML configuration reaches a
recursive `itertext` that raises an uncaught `RecursionError` under
pure-Python ElementTree.

## 2. Evidence

### 2.1 Stdlib source facts (local primary source)

Interpreter: Python 3.14.7,
`/home/linuxbrew/.linuxbrew/Cellar/python@3.14/3.14.7/lib/python3.14/zipfile/__init__.py`
(2,454 lines; read 2026-09-12). Line numbers below refer to that file.

| Fact | Source | Consequence for the design |
|---|---|---|
| `_RealGetContents` reads the whole declared central directory in one call (`data = fp.read(size_cd)`, line ~1563) and loops `while total < size_cd` (line ~1566), consuming at least 46 bytes per header (`structCentralDir`, line 104) | verified by reading and by E3 | `ZipInfo` allocation is bounded by the declared `size_cd`, never by the EOCD count fields; `max_metadata_bytes` is the allocation bound |
| EOCD entry counts are not used to bound allocation | verified by E2 (count lies change nothing) | the existing post-construction `len(zf.infolist())` check is the correct enforcement point and must stay |
| `_EndRecData64` (lines 285-344) rewrites `_ECD_ENTRIES_TOTAL`, `_ECD_SIZE`, `_ECD_DISK_NUMBER`, `_ECD_DISK_START`, `_ECD_LOCATION` and `_ECD_SIGNATURE` from the ZIP64 record, and itself enforces single-disk plus `diroffset + dirsize == reloff` | verified by reading and by E1 | ZIP64 support is "read the numbers the stdlib already rewrote"; the existing size_cd cap and disk checks then apply to 64-bit values unchanged |
| `_handle_prepended_data` (lines 272-283): `start_dir = offset_cd + concat`, `concat = ECD_LOCATION - size_cd - offset_cd`, so the parse effectively starts at `ECD_LOCATION - size_cd` | verified by reading; E2 sizesmall/sizebig behavior matches | the parse span is `size_cd` bytes; a lying-small `size_cd` shrinks the span (safe), a lying-large one is capped by `max_metadata_bytes` |
| `ZipExtFile._read1` (lines ~1161-1199): DEFLATE uses `self._decompressor.decompress(data, n)` (bounded output) while the BZIP2/LZMA branch uses `self._decompressor.decompress(data)` with no `max_length` | verified by reading and by E4b | non-DEFLATE methods break per-call memory bounds; see decision D2 |
| `ZipFile` over a passed file object never closes it (`_fpclose`, line ~2103) | verified by E6 | the descriptor spool seam is clean: `os.fdopen(os.dup(fd))` + `zf.close()` leaves our descriptor ours |
| `_EndRecData` fast path requires a zero EOCD comment size, else scans at most 64 KiB + 22 bytes from the end (lines 345-403) | verified by reading | end-record preflight cost on a 2 GiB spool is bounded and trivial |
| `MAX_EXTRACT_VERSION = 63` (line 75); unknown methods raise `NotImplementedError` in `_get_decompressor` (line ~872) | verified by reading | existing `unsupported-zip-version` / `unsupported-compression` statuses map onto real stdlib failure modes |

ElementTree facts (local 3.14.7):
`xml/etree/ElementTree.py` line 403: pure-Python `itertext` is recursive
(`yield from e.itertext()`), so depth beyond the interpreter recursion limit
(default 1000) raises `RecursionError`. The C accelerator
(`lib-dynload/_elementtree.cpython-314-x86_64-linux-gnu.so`) survived depth
3000 in E5 but its teardown crashed at depth 24000 (E5c).

### 2.2 Spike results (all run 2026-09-12, scripts and logs under /tmp/spike25/)

Fixtures generated by `/tmp/spike25/gen_fixtures.py` (streamed writes,
bounded generator memory, largest fixture 5.7 MB). Every experiment ran in
its own subprocess with `resource.setrlimit(RLIMIT_AS, ...)` caps as noted.
Logs: `e123_1515.log` (first E1-E3 run), `e23_corrected.log` (E2/E3 after
fixing my EOCD field-index labels; the first run's declared-field labels
were wrong, construction numbers were unaffected), `e4.log`, `e4b.log`,
`e5.log`, `e5c.log`, `e6.log`.

| ID | Question | Command | Result |
|---|---|---|---|
| E1 | What does `_EndRecData` return for a real >65535-entry ZIP64 archive, and does `ZipFile` open it? | `python3 /tmp/spike25/run_e1_e3.py E1` (AS cap 512 MiB) | signature `PK\x06\x06` (= `stringEndArchive64`), `entries_total=65537`, `size_cd=3396814`, disk fields 0/0; `ZipFile` opens, `infolist=65537`, 3.8 s, tracemalloc peak 34.1 MB, RSS 89.6 MB |
| E2 | Do EOCD count/size lies change zipfile allocation? | `run_e1_e3.py E2count0 / E2countbig / E2sizesmall / E2sizebig` | count0 and countbig (60000) both open with `infolist=1000` (counts never bound allocation); sizesmall (46) and sizebig (+4096 with appended zeros) fail `BadZipFile("Bad magic number for central directory")` |
| E3 | Worst-case `ZipInfo` allocation under the 4 MiB metadata cap | `run_e1_e3.py E3a/E3b/E3c` | cd_4mib: 91,180 headers parsed, 2.542 s, tracemalloc 34.8 MB (400.2 B per ZipInfo), RSS 79.2 MB. cd_20k: 20,000 parsed, 0.517 s, 7.7 MB. cd_100k (4.6 MB span): 100,000 parsed, 2.776 s, 38.1 MB. Allocation grows linearly with `size_cd` and is NOT bounded by `max_entries` today. (This run also trialed a raw `bytes.count(PK central signature)` pre-count; E7 below shows that idea unsound and it is superseded by the structural walk) |
| E4 | Cooperative deadline granularity on a 256 MiB DEFLATE entry (261 KB compressed, ratio 1029x) | `run_e4.py deflate` | 4,097 reads of 64 KiB, total 0.169 s, max gap between deadline checks 0.37 ms, median 0.039 ms, RSS 14.4 MB. Pathological-stream CPU per chunk NOT measured (honest zeros only); this number is a benign-data baseline, not a bound |
| E4b | What does ONE bounded `read(65536)` allocate for LZMA/BZIP2? | `run_e4b.py lzma / bzip2` | lzma (9,551 B compressed, 7,026x): first read returns 65,536 B after 305.1 ms but materializes the full entry (`_readbuffer` length 67,108,864), RSS 13.9 to 86.3 MB. bzip2 (79 B compressed, 849,478x): 448.8 ms, same 64 MiB materialization, RSS 81.6 MB. Per-call memory for non-DEFLATE methods is unbounded in this zipfile |
| E5 | Reviewer residual: raised-depth XML under pure-Python ET | `run_e5.py c|pure <depth>` (AS cap 256 MiB) | depth 1000/2000/3000 pure: `parse_xml` ok, structural preflight (limit 4000) ok, then `itertext` raises `RecursionError`; depth 980 pure ok (threshold matches the default recursion limit 1000). Same depths on C ET: all ok. Default config (preflight limit 64) rejects the fixture at depth 65 on both, so the default is protected by preflight, not luck |
| E5c | Does a deep C-ET tree crash even when preflight rejects it? | `run_e5c.py 4000/24000/60000` | depth 4000: full lifecycle incl. teardown clean. depth 24000: parse ok, preflight rejects, `gc.collect()` ok, then interpreter teardown SEGFAULT (exit 139). depth 60000: `MemoryError` inside expat under the 256 MiB cap (fails safely). So a parsed-but-rejected deep C tree can kill the process at teardown on this build |
| E6 | Descriptor capture to owner-only spool, streaming digest, `ZipFile` over `fdopen` | `run_e6.py` | 5,745,134 B copied in 0.024 s; streaming sha256 equals re-read disk digest (`56c2f47d855c7e5d...`); mid-read stat unchanged; `ZipFile` over `os.fdopen(os.dup(fd),"rb")` lists 65,537 entries; `zf.close()` leaves our descriptor readable. The spool seam works end to end |
| E7 | Is a raw signature-byte count a sound structural pre-count? Counterexample plus a bounded structural header walk | `run_e7.py counterexample`; `run_e7.py walk base1000/cd_4mib/cd_100k/zip64/embedded/sizesmall/sizebig` | COUNTEREXAMPLE: an honest 1000-entry archive whose filenames each embed one `PK\x01\x02` (stdlib-written, zipfile opens it, names round-trip as `n0PK\x01\x02x`) yields byte-count 2000 while the true record count is 1000; under a 1200-entry budget the byte count falsely REJECTS an honest archive. Walk: a cursor walk over 46-byte headers validating signature, the three length fields, span containment, exact span termination, and the budget, matches `zipfile` exactly on every fixture: base1000 1000, cd_4mib 91,180, cd_100k 100,000, embedded 1000, zip64 65,537 (only when the span is derived from the endrec AFTER `_EndRecData` applied the ZIP64 record; deriving it from the raw 32-bit EOCD field gives a wrong span and a spurious bad-signature). The E2 patched variants fail the walk with bad-signature exactly where `zipfile` raised BadZipFile |
| Baseline | Do the existing suites pass on this machine? | `python3 -m unittest tests.test_advisory_transport` / `tests.test_archives` from the repo root, 2026-09-12 | 48 tests OK in 3.852 s; 66 tests OK in 0.049 s. The killable-worker pattern (`mdi/advisory_transport.py`: `Popen(..., start_new_session=True)`, `killpg` SIGKILL, bounded stdout pump, 10 s bounded reap, allowlist env, `python -I` worker) is proven locally and reusable |

Not found / not measured (labeled, not guessed): per-chunk CPU for
adversarial DEFLATE streams; worker interpreter startup cost on this
machine (needed to know how many one-shot workers fit inside one shared
120 s stage deadline); behavior of other Python builds for the E5c
teardown crash (single-build evidence only).

E5c evidence freeze (coordinator instruction; no further crash probes will
be run until the parent has inspected these): both segfaults are preserved
in systemd-coredump on this host, `coredumpctl list` rows of 2026-09-12:
PID 2896599 15:19:34 CEST SIGSEGV python3.14 (the depth-24000 run of
`run_e5.py c 24000`, log `/tmp/spike25/e5.log`) and PID 2897870 15:19:53
CEST SIGSEGV python3.14 (the depth-24000 run of `run_e5c.py 24000`, log
`/tmp/spike25/e5c.log`), both dumps "present" (2.4M/2.3M), retrievable with
`coredumpctl info <pid>`. Shell context: `ulimit -c` unlimited,
`core_pattern=|/usr/lib/systemd/systemd-coredump ...`. In-process context:
`resource.setrlimit(RLIMIT_AS, 256 MiB)` applied at interpreter start of the
script; scripts and logs are byte-preserved under /tmp/spike25/ as listed in
2.2. The C-stack cause is NOT diagnosed here; that is parent inspection
work, and no hypothesis below depends on a specific cause.

## 3. Recommended design (Phase A: reach approved capacity with hard bounds)

One sentence: keep the streaming budgets, statuses, and result semantics as
they are, refactor the traversal's bytes-to-source seam to bounded-lifetime
descriptors, move each outer-archive inspection into a one-shot killable
worker process modeled on `mdi/advisory_transport.py`, capture the outer
file to a parent-owned owner-only spool instead of parent RAM, spool nested
containers inside the worker, and gate `ZipFile` construction behind a
bounded structural header walk over the ZIP64-aware central-directory span;
leave compression support at STORED/DEFLATED with an explicit deferral (D2).

### 3.1 Components

1. `mdi/capture.py`: add `capture_to_spool(path_or_dirfd_leaf, max_bytes,
   scratch_dir) -> CapturedSpool`. Same discipline as `capture_file`
   (O_NOFOLLOW|O_NONBLOCK final component, regular files only, fstat before
   and after on the same descriptor, max_bytes+1 sentinel read), but the
   bytes stream into a 0o600 generated-name file inside a 0o700
   parent-created scratch directory, with the sha256 computed while
   streaming. `CapturedSpool` carries path, size, digest, complete/reason,
   stat identities; it never carries the bytes. The existing in-memory
   `capture_file` stays for its current callers.
2. `mdi/archives.py` preflight: replace the raw signature-byte count idea
   from revision 1 (unsound, E7 counterexample: embedded signatures inside
   filename/extra/comment fields overcount and would falsely reject honest
   archives) with a bounded structural header walk over the declared
   central-directory span, BEFORE `ZipFile` construction. The walk advances
   a cursor by each 46-byte header plus its filename/extra/comment length
   fields (read with the stdlib's own `structCentralDir` layout and
   `_CD_*` offsets, already version-guarded), and rejects, in order:
   truncated header at the cursor, missing central-directory signature,
   lengths running past the span, cursor not terminating exactly at span
   end, and record count over the remaining shared entry budget. The span
   is derived from the endrec AFTER `_EndRecData` applied any ZIP64 record
   (E7: deriving it from the raw 32-bit EOCD field yields a wrong span);
   ZIP64 end records are accepted rather than declined, keeping multi-disk
   rejection (E1). E7 measured the walk matching `zipfile.infolist()`
   exactly on every fixture (1000, 91,180, 100,000, embedded-signature
   1,000, ZIP64 65,537) and failing safely on both E2 size lies. Contract
   tension, addressed head on: the module today promises "stdlib zipfile
   parses; no ZIP parser of its own". The walk validates structure only
   (signature, three length fields, span termination, count); it builds no
   `ZipInfo`, decodes no names, interprets no record semantics, and uses
   the stdlib's own layout constants, so parsing of record semantics stays
   solely with zipfile. The module docstring must be revised to say exactly
   that; if the review judges even this walk a second parser, the fallback
   is the measured transient bound alone (D3) and that decision is D10.
3. `mdi/archives.py` traversal seam: this is a bounded descriptor-lifetime
   refactor, NOT "the existing traversal unchanged" (revision 1 wording was
   inaccurate). Today `_inspect_container` receives `bytes`, constructs
   `ZipFile(io.BytesIO(data))`, records `size = len(data)`, and passes
   retained nested bytes into recursion. Phase A threads a container source
   object through `_State` and the container records that is either `bytes`
   (nested entries below an in-memory threshold, preserving today's path
   for small archives) or a read-only descriptor over a spool file
   (E6-proven seam: `os.fdopen(os.dup(fd), "rb")`; `ZipFile` will not close
   it). Descriptor lifetime rule: opened at container entry, closed in the
   same scope's `finally`, never closed twice, and the count of live spool
   descriptors bounded by `max_depth` (4), so nesting bounds live file
   handles; `len(data)` becomes the source size (from `fstat` for spools)
   in the preflight range checks. Budgets, statuses, result shape, and
   pooling semantics are preserved; the bytes-to-source seam is the
   refactor. `_read_stream` gains a spool-writing variant for `want_archive`
   entries: hash while writing, cap at `allowed+1` as today, delete the
   spool on any non-hash outcome. `want_metadata` entries stay in-memory
   `bytearray` retention (transient, bounded by `max_entry_bytes`; see D4).
4. New `mdi/archive_isolation.py` (parent + worker in one file, the
   `advisory_transport` layout): `inspect_archive_isolated(spool, limits)`.
   Parent half: spawn `python -I <this file> --mdi-archive-worker` with
   `start_new_session=True`, argv list only, fresh allowlist env, cwd the
   trusted parent dir; one JSON request line on stdin (spool path, limits,
   precomputed digest); pump stdout on a selector against a monotonic
   deadline; SIGKILL the group and reap with a bounded wait on deadline,
   stdout overflow, malformed protocol, or any BaseException. Worker half:
   validate the request, self-apply `RLIMIT_AS` (D6), open the spool by
   descriptor, run the refactored traversal (component 3) with nested spools
   inside the same scratch directory, emit exactly one JSON line (the
   unchanged `inspect_captured_archive` result shape). The worker is
   trusted mdi code and by contract never spawns processes of its own; that
   contract, not the group signals, is what makes parent-side scratch
   removal after confirmed reap sound (see 3.2).
5. `mdi/candidates.py`: `_capture_archive` and `_capture_explicit` swap
   `capture_at` for `capture_to_spool` and `inspect_captured_archive` for
   `inspect_archive_isolated`, one spool+scratch per archive. The descriptor
   walk itself (`open_checkout_fd`, `open_module_dir`, relative capture) is
   untouched: the capture discipline stays in the parent; only the bytes
   destination changes.
6. `mdi/archives.py` defaults: after the review gate, the dataclass defaults
   become the approved numbers (input 2 GiB becomes a spool cap rather than
   a RAM cap, expanded 2 GiB, entry 256 MiB, entries 20,000, depth 4,
   120 s). Until the gate, defaults stay as-is and the new path must be
   exercised by injected tiny limits in tests, per the plan's testing rule.

### 3.2 Lifecycle and protocol (explicit)

- Parent: scratch dir (mkdtemp 0o700) -> capture to spool (streaming digest,
  stat race check) -> spawn worker (deadline anchored at Popen return, the
  advisory_transport convention) -> pump bounded stdout -> validate one JSON
  line strictly (schema, no duplicate keys, no non-finite constants) ->
  reap. Cleanup safety rests on an explicit assumption stated here, not on
  the process signals alone: the archive worker is TRUSTED mdi code and by
  contract never spawns children, so the only processes that can touch the
  scratch are the leader and its in-group lifetime. Under that assumption,
  a confirmed leader reap plus the preceding group SIGKILL is sufficient
  basis to remove the scratch. The signals themselves prove less: killpg
  acceptance and leader reaping prove nothing about a process that
  detached into another session, so if the no-descendants assumption were
  ever violated, cleanup would NOT be proven safe and this design does not
  promise general cleanup-safety from group signals alone. Scratch removal
  therefore happens after confirmed reap, with the assumption recorded in
  the result (a `cleanup-assumptions` field naming
  `trusted-worker-no-descendants`).
  Abnormal paths (deadline, overflow, protocol error, KeyboardInterrupt):
  killpg while the leader is unreaped, then bounded reap. If the bounded
  reap wait expires, termination is UNCONFIRMED: the scratch directory is
  RETAINED (not removed), and the parent records a qualification entry
  (stable code `unconfirmed-termination`, retained scratch path, spool
  digest and size, monotonic timestamps) plus an issue, and the run's
  result for that archive is a rejected/partial outcome, never a silent
  success. Rationale: an unconfirmed worker may still hold or reopen paths
  inside the scratch; removing the directory under it races worker writes
  and can strand recreated files after removal.
  PID lifetime guard: group signals go only to an UNREAPED leader
  (`poll() is None`), the `advisory_transport._terminate` rule; once the
  leader is reaped its PID may have been reused for an unrelated process
  group, so no signal is sent after reaping and no post-reap signal result
  is ever interpreted as evidence about the original worker.
  Retained scratch (unconfirmed termination) is NEVER deleted
  automatically, matching the plan's "no auto-pruning" state principle
  (plan line 99: "No registry, shared cross-run cache, auto-pruning or
  cross-run merge"): an age or quota sweeper could delete a directory
  under a worker that is still live. Instead, a scratch quota being
  exhausted REFUSES NEW WORK (new archive inspections do not start; the
  refusal and the explicit list of retained scratch paths go into issues
  and coverage), and retained scratch is cleaned only by (a) verified-safe
  owned current-run cleanup after confirmed reap under the trust
  assumption, or (b) separately authorized recovery performed outside this
  tool. Nothing is claimed about descendants that escaped the session.
- Worker: single read of one bounded request line; re-validate everything
  (spool size against `max_input_bytes`, digest re-verification is a
  decision, D7); self-rlimit; run the refactored traversal; write one JSON
  line; exit. The worker never emits exception text: failures collapse to
  stable codes, the `advisory_transport` redaction rule.
- Deadline semantics (corrected against the approved wording): the plan
  says "archive stage 120 seconds", a bound on the archive STAGE, not on
  each archive. Default: one shared overall stage deadline of 120 s for the
  whole archive stage of one discovery run, capture included; each archive
  inspection is handed the REMAINING stage time on the same monotonic
  clock, `max_archives` still bounds the count, and an exhausted stage
  deadline yields honest partial coverage plus an issue rather than
  silently skipping archives. Per-archive 120 s budgets (the revision-1
  reading, which would allow 32 x 120 s = 64 min under `max_archives=32`)
  contradict the approved stage figure and are available only as an
  explicit user decision (D1). Cooperative in-worker checks stay as a
  fast partial-result mechanism; the killpg is the hard deadline that also
  covers native decompressor CPU, which cooperative checks cannot interrupt
  (E4 granularity is sub-millisecond on benign data, but that is not a
  bound; see Not-measured list).
- Result bound: parent caps worker stdout at a computed bound, e.g.
  `max_entries * 768 + 2 * max_metadata_bytes + 64 KiB` slack for entry
  records plus names (names total at most the central-directory size) plus
  issues/evidence. To keep that bound tight the worker also enforces a
  cumulative raw-evidence preview cap (D5) instead of allowing
  `max_entries * 4 KiB` of previews.

### 3.3 Byte identity versus inspection coverage (unchanged semantics)

Identity is a digest over the exact captured bytes: outer archive digest
from the spool-streaming hash; nested archive digest from the entry-spool
hash; entry digests from the existing streaming hash. Identity completeness
and inspection coverage remain separate fields exactly as today
(`inspection_status` per archive, `coverage` block per call): a deadline
kill or budget stop yields partial coverage and preserved identities, never
a candidate downgrade. Truncated captures still yield
`input-truncated`/`input-changed` with no digest. Paths remain provenance
only; no path is reopened after capture.

### 3.4 Invariants: what enforces them, attempted breaks, residual risk

1. Same-bytes identity (digest over captured bytes, paths never reopened).
   Break attempts: E6 (stream vs disk digest equal), existing
   `input-digest-mismatch` test path. Residual: the worker trusts the
   parent-owned 0o700 scratch; assumption is local-host ownership, same as
   the current in-memory bytes.
2. Central-directory/metadata budget bounds allocation BEFORE `ZipInfo`
   allocation. Break attempts: E2 (count lies: no allocation effect, so the
   post-construction length check stays as defense), E2 (size lies:
   BadZipFile; the parse span is the declared `size_cd`), E3
   (crafted-header blowup: linear in size_cd, 4 MiB cap means 91,180
   ZipInfos, 34.8 MB tracemalloc, 2.5 s, all measured), E7 (signature-byte
   counting is NOT structural record counting: an honest 1000-entry archive
   with embedded signatures counts 2000; the revision-1 pre-count idea is
   withdrawn). Enforcement after Phase A: size_cd cap PLUS the structural
   header walk (signature, length fields, span containment, exact span
   termination, budget) over the ZIP64-applied span before `ZipFile`
   construction, so allocation is bounded by the remaining entry budget.
   Walk-versus-zipfile agreement measured exact on five fixtures including
   the embedded-signature and ZIP64 cases (E7). Residual: none known; the
   walk rejects strictly more than zipfile accepts, and rejects BEFORE
   allocating. Parent validation pending.
3. Hard deadline including a blocked or CPU-spinning decompressor.
   Enforcement: killpg on the one-shot worker (pattern green locally, 48
   tests), under a shared archive-STAGE deadline (3.2). Break attempts:
   none possible in-process today (that is the gap); the worker makes the
   kill total including native calls. Residual: an un-reaped-after-10 s
   leader is reported unconfirmed, never assumed dead; group signals are
   sent only while the leader is unreaped, and no post-reap signal claim
   is made because the PID may have been reused; session-escaping
   descendants excluded, documented.
4. No-follow and cleanup ownership. Capture: unchanged O_NOFOLLOW final
   component discipline. Scratch: parent creates with generated names;
   removal after confirmed reap ONLY under the stated trust assumption
   (worker is trusted mdi code that never spawns children); confirmed
   leader reap proves leader termination, not the absence of processes in
   other sessions, and group signals alone are NOT claimed to prove
   cleanup-safety in general. On unconfirmed termination the scratch is
   retained with a qualification record, never raced against a
   possibly-live worker, and never auto-deleted: no age/quota sweep exists
   (conflicts with the plan's no-auto-pruning principle, and prior
   unconfirmed workers may still be live); quota exhaustion refuses new
   work and reports the retained paths (3.2, D11). Break attempts
   REQUIRED at implementation time
   and NOT attempted in this spike: kill-mid-spool with confirmed reap
   (scratch removed, no leaked spools) and the unconfirmed-reap path
   (scratch retained, qualification recorded, no second removal attempt).
5. Total nested budgets (expanded/entries/depth) shared across the whole
   traversal. Budget pooling and charging rules are unchanged by the seam
   refactor; nested container bytes are charged when spooled exactly as
   they are charged today when retained; double-charge on recursion
   preserved.
6. File-backed protocol cannot blow up parent memory. One JSON line, strict
   validation, computed stdout bound, worker-side evidence caps. Break
   attempt REQUIRED at implementation time (flooding/oversized-worker
   fixtures, the `test_advisory_transport` style); NOT attempted here.
7. Archive-only candidate continuity (AC#3: unknown/unsupported reported,
   never read as absence). `discover_candidates` aggregation is untouched;
   unsupported ZIP64-turned-supported, multi-disk, encrypted,
   unsupported-compression, and malformed outcomes keep their statuses and
   issues. Tests must add: a ZIP64 archive now inspected (not silently
   dropped), a multi-disk fixture still declined with an issue, unsupported
   formats still produce zero candidates plus an issue entry.

### 3.5 Rejected alternatives (why this is the smallest sufficient change)

- Raise in-memory limits only: parent RSS up to input+retained+nested
  tables (approved caps imply multi-GiB); violates the plan's spool language
  and bounded-memory posture. Rejected.
- Worker without spool (capture full bytes inside the child): smallest diff
  and isolates the deadline, but the child then needs ~2 to 4 GiB RSS at
  approved caps (input plus retained nested containers), risking OOM kills
  and breaking the plan's "nested archives spool captured bytes to
  owner-only scratch" requirement. Kept as the documented fallback if the
  full Phase A proves too heavy, not recommended.
- Threads or signal-based interruption: cannot interrupt a native zlib call
  (the original finding); signals only run between bytecodes. Rejected.
- Write our own streaming ZIP parser to avoid spooling central-directory
  reads: violates the module's "stdlib only parses" contract. Rejected.

### 3.6 Minimal containment option (separate, for review)

If Phase A is deferred, the smallest deadline-only step is the
"worker without spool" alternative from 3.5 (spawn the killable worker,
keep full-bytes capture inside it, staged limits in place): it delivers the
hard deadline and parent isolation at the cost of child RSS equal to
today's byte holdings. It does NOT reach approved capacity and must be
labeled as such wherever its limits are reported.

## 4. XML raised-depth residual (TASK-25 note), findings and proposal

Independently reproduced (E5, 2026-09-12): under pure-Python ET a
depth-1000 foreign subtree under `groupId` passes `xmlsafe.parse_xml` and
the structural preflight (limit 4000) and then raises uncaught
`RecursionError` in `discover._text`'s `itertext`; threshold sits at the
default recursion limit (980 ok, 1000 fails). The C accelerator handles
depth 3000 cleanly, BUT a parsed depth-24000 C tree segfaults this
interpreter at teardown even after preflight rejection (E5c, exit 139);
depth 4000 is clean end to end; depth 60000 fails as MemoryError under the
256 MiB cap (safe direction).

Proposal (smallest first, all gated):
1. NEW PRIMARY CONTROL, pre-construction: `xmlsafe.parse_xml` gains a
   CONFIG-INDEPENDENT element-depth ceiling enforced inside the Expat
   safety pass, BEFORE any ElementTree construction. The safety pass
   already installs handlers; it additionally tracks element nesting depth
   per start/end event and raises `UnsafeXML` the moment the ceiling is
   exceeded, so a deep document is never built into a C or pure-Python
   tree at all. This closes both hazard classes at the source: the
   pure-Python `itertext` RecursionError and the deep-C-tree teardown
   crash, because neither can occur for a tree that was never
   constructed. Recommended ceiling: 64. Corrected grounding (the
   revision-4 rationale "every current caller already rejects deeper" was
   FALSE and is withdrawn): `xmlsafe.parse_xml` enforces NO element-depth
   bound today (`mdi/xmlsafe.py` contains no depth logic);
   `discover_declarations`'s `max_depth=16` bounds MODULE-directory
   traversal, not element depth; and a 64-element default exists only in
   the effectivePOM importer's post-parse structural preflight, on that
   one path, AFTER the tree is built. `parse_xml` has three call sites
   (discover.py:336 module POMs, history.py:425 git blob POMs,
   maven.py:582 importer input; line numbers as of 2026-09-12), and today
   all three accept arbitrarily deep element trees. The Expat-pass ceiling
   of 64 is therefore a NEW accepted-input restriction applied to every
   parse path, aligned with the effectivePOM importer default, NOT a
   codification of existing behavior. Why still 64, and conservative: the
   nearest measured hazards on this build are the pure-Python `itertext`
   failure at the default recursion limit (980 ok, 1000 fails, E5) and
   the C teardown crash at 24000 (E5c, unverified by parent), and NO
   cross-build safe depth above 64 has been measured. Compatibility
   requirement before freeze: a fixture corpus spanning ordinary
   POM/profile/plugin-configuration shapes (the documents
   discover/history/maven actually parse) must be verified to stay within
   64 element levels, so the new restriction rejects no legitimate
   document; any corpus document deeper than 64 is a compatibility finding
   requiring either an evidence-based, user-gated ceiling adjustment or a
   fixture correction before the ceiling ships. As a new input
   restriction, enabling this control goes through the user gate. Caller
   `max_depth` can tighten below the ceiling but
   never relax past it; raising the ceiling itself is a code change that
   additionally requires cross-build measurement evidence (D9).
2. Default `max_depth=64` stays the supported configuration (E5: both
   parsers safe; the default is enforced by preflight before any itertext).
3. Make `discover._text` (and the two other `itertext` join sites in
   `discover.py`) iterative with an explicit stack. Stated precisely: this
   removes the pure-Python RecursionError class for any tree that is
   somehow already built; it does NOT prevent teardown of a huge C tree
   (the E5c crash happens to a rejected tree that was already constructed;
   reported crash still unverified by parent, no further crash probes).
   With control 1 in place this is defense-in-depth, not the primary
   protection.
4. Minimal containment at the importer boundary: catch `RecursionError`
   alongside parse failures and convert to the existing stable
   `UnsafeXML`/`MavenImportError` vocabulary so no configuration crashes the
   caller.
5. Raised-depth configurations remain unsupported until a ceiling is
   measured across the supported Python builds; keep default 64.
   No further crash probes will be run before the parent has inspected the
   preserved dumps listed in the E5c evidence freeze (2.2).

## 5. Final recommended defaults (each still requires the review gate; none
enacted here; per-decision alternatives exist only where the user must
choose, not as open engineering questions)

- D1 Deadline scope: DEFAULT = one shared 120 s archive-STAGE deadline per
  discovery run, covering capture and all worker inspections on one
  monotonic clock, each inspection receiving the remaining stage time,
  honest partial coverage on exhaustion. The per-archive alternative
  (120 s each, up to 64 min at `max_archives=32`) conflicts with the
  approved "archive stage 120 seconds" and is available ONLY as an explicit
  user decision recorded with the run.
- D2 Compression beyond STORED/DEFLATED: E4b shows one bounded read call on
  a 64 MiB LZMA/BZIP2 entry materializes the entire entry and costs
  305 to 449 ms; per-call memory is unbounded by the stdlib API shape.
  Options: explicit scoped deferral (AC#2 allows it) versus a
  bounded-feed decompressor wrapper (feed compressed bytes in small slices,
  cap decompressed output per call) plus worker isolation. Recommendation:
  deferral now, wrapper as its own reviewed slice if real JARs need it.
- D3 `max_metadata_bytes`: keep 4 MiB. Accurate transient accounting,
  measured without the walk (E3): a full 4 MiB crafted span allocates
  91,180 ZipInfo objects worth 34.8 MB of Python allocations (tracemalloc
  peak; 400 B per ZipInfo) while process RSS peaks at 79.2 MB, and spends
  2.5 s constructing; WITH the structural walk the pre-construction
  rejection caps allocation at the entry budget instead (E3b: 20,000
  entries = 7.7 MB tracemalloc, RSS 28.0 MB, 0.5 s), so the metadata cap is
  a second, coarser bound and 4 MiB stands.
- D4 Metadata (pom.properties) in-memory retention at a 256 MiB entry cap:
  transient per entry, one at a time. Accept the transient, or introduce a
  `max_metadata_entry_bytes` (e.g., 1 MiB) above which parsing is reported
  `unsupported-oversized` while hashing still proves identity.
- D5 Worker stdout bound and cumulative evidence preview cap: proposed
  formula in 3.2; needs exact numbers at review.
- D6 Worker `RLIMIT_AS` value: must exceed peak legit usage (256 MiB entry
  transient + ~40 MB tables + interpreter); proposed 768 MiB default,
  configurable, plus a MemoryError to stable-code mapping.
- D7 Digest re-verification in the worker: re-hash the spool (cost: one
  extra pass over up to 2 GiB) versus trusting the parent-owned 0o600
  spool. Recommendation: trust; the digest already covers the bytes the
  parent streamed.
- D8 Scratch location: `TemporaryDirectory` default TMPDIR versus the
  existing state directory (space and cleanup semantics differ; 2 GiB
  inputs need a scratch host with headroom).
- D9 XML depth: DEFAULT = config-independent Expat-pass ceiling of 64
  before any tree construction (section 4.1; a NEW accepted-input
  restriction on all three `parse_xml` call sites, subject to the
  POM/profile/plugin-config compatibility fixture corpus and the user
  gate), iterative text extraction as
  defense-in-depth (4.3), RecursionError boundary catch (4.4), raised-depth
  unsupported until measured across builds. Raising the ceiling requires
  cross-build evidence and the user gate.
- D10 Preflight walk contract: the structural header walk (3.1 component 2)
  validates structure only and uses the stdlib's own layout constants, but
  it is more than the current docstring promises ("stdlib zipfile parses;
  no ZIP parser of its own"). Accept the walk with a revised docstring, or
  reject it and fall back to the measured-transient bound alone (D3),
  keeping the post-construction count check. Recommendation: accept the
  walk; the counterexample (E7) shows the cheap alternative is unsound, and
  E3 shows no walk means multi-second, tens-of-MB transients on
  hostile metadata.
- D11 Retained scratch after unconfirmed termination: NO automatic
  deletion, ever. The revision-2 idea of an age/quota sweep is REJECTED:
  it conflicts with the plan's no-auto-pruning state principle (plan line
  99), and a prior unconfirmed worker may still be live when a sweeper
  fires. DEFAULT = quota exhaustion refuses new archive work and reports
  the explicit list of retained scratch paths in issues and coverage;
  cleanup happens only via verified-safe owned current-run removal
  (confirmed reap under the trust assumption) or separately authorized
  recovery outside this tool. The remaining open sub-question is only the
  quota number and the qualification-record fields.

## 6. Implementation-phase file list (not started here)

Source: `mdi/capture.py` (spool capture), `mdi/archives.py` (structural
walk preflight with ZIP64-aware span, descriptor-lifetime container seam,
nested spools), new `mdi/archive_isolation.py` (parent/worker),
`mdi/candidates.py` (seam swap), `mdi/xmlsafe.py` (config-independent
Expat-pass depth ceiling, section 4.1), `mdi/discover.py` (iterative
itertext), `mdi/maven.py` (RecursionError boundary). Tests:
`tests/test_archives.py`
(ZIP64 accept; walk/zipfile agreement on honest, embedded-signature,
crafted-header, size-lie, and ZIP64 fixtures; walk rejection over budget
BEFORE construction; dishonest counts still caught post-construction;
descriptor spool entry reads; descriptor lifetime under nesting; tiny
injected limits), new `tests/test_archive_isolation.py` (protocol,
shared-stage deadline including remaining-time handoff, deadline kill,
flooding, kill-mid-spool cleanup with confirmed reap,
scratch RETENTION plus qualification record on the unconfirmed-reap path,
quota refusal of new work with retained paths reported, no signal after
reap, killpg liveness checks in the `test_advisory_transport` style),
`tests/test_candidates.py` (seam swap, archive-only candidate continuity,
unsupported-format reporting), `tests/test_xmlsafe.py` (Expat-pass depth
ceiling: a depth-65 document is rejected as UnsafeXML BEFORE any tree
construction, verified by asserting the TreeBuilder is never fed, on both
C and pure-Python ET; plus the compatibility fixture corpus of ordinary
POM/profile/plugin-configuration shapes asserting acceptance within the 64
ceiling across all three `parse_xml` call sites), `tests/test_discover.py` (iterative text at depth
under pure-Python ET via blocked `_elementtree`),
`tests/test_maven.py` (RecursionError to stable code). No fixture in the
suite may allocate hundreds of MiB and no test may crash the interpreter:
the spike recipes (crafted central-directory-only archives, patched EOCD
lies, embedded-signature names, tiny LZMA/BZIP2 entries) reproduce every
hazard at kilobyte-to-megabyte scale and belong in
`tests/history_fixtures.py`-style helpers; the E5c teardown crash stays OUT
of the suite (single-build evidence, preserved dumps pending parent
inspection).

## 7. Coverage statement (AC#2/AC#3)

After Phase A: ZIP64 EOCD records accepted and inspected under the same
budgets; multi-disk declined; encrypted entries recorded not read;
STORED/DEFLATED read; LZMA/BZIP2/other methods explicitly reported
`unsupported-compression` (deferral D2) and never treated as dependency
absence; every unsupported or partial outcome lands in `issues` and
`coverage`, and candidates are produced only from inspected evidence. Any
change to that list, or to the approved numbers, goes back through the
review gate.
