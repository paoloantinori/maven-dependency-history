"""Bounded, reproducible allocation/time measurement of mdi.report validation.

TASK-24 evidence collector. It builds seeded synthetic valid reports in three
sizes, then measures the real production functions with time.perf_counter
(timing runs) and tracemalloc (allocation runs, kept separate so tracemalloc
overhead never contaminates timings). The cost is decomposed by also measuring
the module-internal helpers the two review suggestions target:
_required_limitations (the qualification mapping that validate_transition
computes twice: once inside validate_report(current) and once explicitly), and
_check_json (the recursive JSON-safety walk that renders an eager f-string
error path for every node). Two probe replicas of that walk, one building the
same path strings and one building none, isolate the path-rendering share.
Probes are measurement code in this file only; production modules are imported
read-only and never modified.

validate_transition deep-compares record subtrees with _json_equal, never the
whole report (whose revision always differs), so the deep-equality proxy here
compares a same-revision twin of the report: a full walk of identical
structures, which upper-bounds the summed per-record comparisons.

Run from the repository root:

    python3 docs/verification/measure_report_validation.py

Bounds enforced here: every generated report and transition pair is asserted
valid; canonical serialized size is capped at 16 MiB, under the run store's
32 MiB MAX_READ_BYTES; 1 warmup plus at most 3 measured repetitions per target
with targets interleaved per repetition; bundle counts and build retries are
capped. No network, no filesystem writes, no Maven.
"""

import copy
import gc
import hashlib
import json
import math
import platform
import random
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import report as report_module  # noqa: E402
from mdi import runstore as runstore_module  # noqa: E402
from mdi.report import validate_report, validate_transition  # noqa: E402

SEED = 20260912
RUN_ID = "measure-task24-run"
QUERY = "which declared dependencies does the application actually use?"
WARMUP = 1
REPEATS = 3  # measurement cap fixed by the task bounds; do not raise casually
GENERATION_CAP_BYTES = 16 * 1024 * 1024  # self-imposed, below MAX_READ_BYTES
MAX_BUNDLES = 20000
BUILD_ATTEMPTS = 8
# (name, target serialized bytes, evidence per qualification)
SIZES = (
    ("small", 128 * 1024, 8),
    ("medium", 2 * 1024 * 1024, 8),
    ("large", 12 * 1024 * 1024, 8),
    ("large-qlight", 12 * 1024 * 1024, 100),
)
_STAGE_SPECS = (
    ("capture", "complete"),
    ("xml-parse", "complete"),
    ("discover", "complete"),
    ("history", "complete"),
    ("packaged-copy", "partial"),
    ("correlate", "complete"),
    ("usage", "complete"),
    ("advise", "complete"),
)


def _timestamp(index):
    return f"2026-09-10T{(index // 60) % 24:02d}:{index % 60:02d}:00+00:00"


def _payload(rng, index, slot, eid):
    """A nested, JSON-safe capture payload; depth 3, deterministic."""
    return {
        "sha256": hashlib.sha256(f"payload:{eid}".encode()).hexdigest(),
        "bytes": 900 + (index * 7 + slot * 13) % 2048,
        "entries": [
            {
                "group": group,
                "artifact": f"org.example:{group}-{index % 5}",
                "scope": rng.choice(("compile", "runtime", "provided")),
                "lines": [1 + slot, 2 + slot, 3 + slot],
            }
            for group in ("direct", "transitive", "management")
        ],
        "note": f"captured from module {index} slot {slot} during measurement generation",
    }


def _assemble(rng_seed, bundle_count, qual_cadence):
    """Build one full valid report; bundle i's content depends only on the
    seed and i, so retries at a different count extend, never rewrite."""
    rng = random.Random(rng_seed)
    report = report_module.new_report(RUN_ID, query=QUERY)
    evidence_ids = []
    for index in range(bundle_count):
        sid = f"s{index}"
        report["subjects"].append(
            {
                "id": sid,
                "realm": rng.choice(("project", "build-plugin", "unknown")),
                "module": f"org.example:lib{index}",
                "coordinates": f"org.example:lib{index}:jar:{1 + index % 9}.{index % 7}.0",
                "build_context": {
                    "profile": rng.choice(("release", "debug", "default")),
                    "depth": 1 + index % 4,
                },
            }
        )
        for slot in range(2):
            eid = f"e{index * 2 + slot}"
            report["evidence"].append(
                {
                    "id": eid,
                    "category": rng.choice(("pom-bytes", "tree-text", "jar-index", "effective-pom")),
                    "source_locator": f"{sid}/pom.xml#L{1 + slot}-L{40 + slot}",
                    "observed_at": _timestamp(index * 2 + slot),
                    "payload": _payload(rng, index, slot, eid),
                    "subject_id": sid,
                    "producer": "mdi.capture",
                    "producer_version": "1.0.0",
                    "generation_context": {
                        "tool": rng.choice(("maven", "direct", "jar-scan")),
                        "depth": 2 + slot,
                    },
                }
            )
            evidence_ids.append(eid)
        if index % 2 == 0:
            report["operations"].append(
                {
                    "id": f"op{index}",
                    "run_id": RUN_ID,
                    "state": rng.choice(("proposed", "consumed", "finished")),
                    "scope": {"paths": [f"{sid}/pom.xml"], "modules": 1},
                    "input_identities": [
                        {
                            "path": f"/checkout/lib{index}/pom.xml",
                            "sha256": hashlib.sha256(f"pom:{index}".encode()).hexdigest(),
                        }
                    ],
                    "tool_identity": {"name": "mdi-capture", "version": "1.0.0"},
                    "disclosure": "local read only, nothing leaves the checkout",
                    "audit": {"started": _timestamp(index), "pid": 4000 + index % 100},
                    "evidence_ids": [f"e{index * 2}", f"e{index * 2 + 1}"],
                }
            )
    evidence_head = evidence_ids[:3]
    for name, outcome in _STAGE_SPECS:
        report["stages"].append(
            {
                "name": name,
                "outcome": outcome,
                "coverage": {
                    "attempted": bundle_count,
                    "covered": bundle_count if outcome == "complete" else bundle_count // 2,
                },
                "reasons": [] if outcome in ("complete", "not_applicable") else ["one module skipped the stage"],
                "evidence_ids": list(evidence_head),
            }
        )
    analysis_index = 0
    for start in range(0, len(evidence_ids), 2):
        cited = evidence_ids[start : start + 1 + start % 3]
        kind = "inference" if analysis_index % 4 == 3 else "observation-summary"
        report["analyses"].append(
            {
                "id": f"a{analysis_index}",
                "evidence_ids": cited,
                "statement_kind": kind,
                "limitations": ["only the inspected modules were considered"] if kind == "inference" else [],
                "statement": f"module {start // 2} is declared and resolved",
            }
        )
        analysis_index += 1
    for start in range(0, len(evidence_ids), qual_cadence):
        report["qualifications"].append(
            {
                "id": f"q{start // qual_cadence}",
                "affected_evidence_ids": evidence_ids[start : start + 3],
                "kind": "truncated-capture",
                "reason": f"capture of module {start // 2} truncated at the byte limit",
                "observed_at": _timestamp(start),
            }
        )
    # Same derivation rule as mdi.report._qualification_text: every analysis
    # citing affected evidence must carry the qualification's limitation text.
    for qualification in report["qualifications"]:
        text = f"{qualification['kind']}: {qualification['reason']}"
        affected = set(qualification["affected_evidence_ids"])
        for analysis in report["analyses"]:
            if not affected.isdisjoint(analysis["evidence_ids"]) and text not in analysis["limitations"]:
                analysis["limitations"].append(text)
    return report


def build_size(name, target_bytes, qual_cadence):
    """Assemble a report whose canonical serialization reaches target_bytes."""
    rng_seed = f"{SEED}:{name}"
    probe = _assemble(rng_seed, 8, qual_cadence)
    per_bundle = len(runstore_module._dumps(probe)) / 8
    count = max(1, round(target_bytes / per_bundle))
    build_seconds = None
    report = None
    size = 0
    for _ in range(BUILD_ATTEMPTS):
        count = min(count, MAX_BUNDLES)
        started = time.perf_counter()
        report = _assemble(rng_seed, count, qual_cadence)
        build_seconds = time.perf_counter() - started
        size = len(runstore_module._dumps(report))
        if size >= target_bytes or count == MAX_BUNDLES:
            break
        count += max(1, math.ceil((target_bytes - size) / per_bundle))
    validate_report(report)  # ground truth: the synthetic report is valid
    if size > GENERATION_CAP_BYTES or size > runstore_module.MAX_READ_BYTES:
        raise SystemExit(f"{name}: serialized {size} bytes exceeds the measurement cap")
    return report, size, build_seconds, count


def _count_nodes(value):
    """Untimed inventory of the report the JSON-safety walk will traverse."""
    counts = {"scalars": 0, "dicts": 0, "lists": 0, "path_strings": 0}

    def walk(node):
        if node is None or isinstance(node, (bool, int, str, float)):
            counts["scalars"] += 1
            return
        if isinstance(node, dict):
            counts["dicts"] += 1
            for item in node.values():
                counts["path_strings"] += 1
                walk(item)
            return
        counts["lists"] += 1
        for item in node:
            counts["path_strings"] += 1
            walk(item)

    walk(value)
    return counts


def _probe_paths_built(value, path):
    """Measurement probe, not production code: mirrors mdi.report._check_json's
    traversal and renders the same eager per-child error-path f-strings."""
    if value is None or isinstance(value, (bool, int, str, float)):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _probe_paths_built(item, f"{path}.{key}")
        return
    for index, item in enumerate(value):
        _probe_paths_built(item, f"{path}[{index}]")


def _probe_paths_none(value, path):
    """Same traversal and dispatch as _probe_paths_built, but no path string
    is ever constructed, so the difference between the two probes isolates
    path-string rendering cost."""
    if value is None or isinstance(value, (bool, int, str, float)):
        return
    if isinstance(value, dict):
        for item in value.values():
            _probe_paths_none(item, path)
        return
    for item in value:
        _probe_paths_none(item, path)


def measure_interleaved(targets):
    """Run every target once per repetition, in the same order each round, so
    machine drift spreads evenly across targets instead of confounding one.
    Timing rounds run without tracemalloc; allocation rounds run with it and
    report peak and delta per call. Returns per-target raw samples."""
    for _, fn in targets:
        for _ in range(WARMUP):
            fn()
    times = {label: [] for label, _ in targets}
    for _ in range(REPEATS):
        for label, fn in targets:
            started = time.perf_counter()
            fn()
            times[label].append(time.perf_counter() - started)
    peaks = {label: [] for label, _ in targets}
    deltas = {label: [] for label, _ in targets}
    tracemalloc.start()
    try:
        for _ in range(REPEATS):
            for label, fn in targets:
                gc.collect()
                tracemalloc.reset_peak()
                before = tracemalloc.get_traced_memory()[0]
                fn()
                current, peak = tracemalloc.get_traced_memory()
                deltas[label].append(current - before)
                peaks[label].append(peak - before)
    finally:
        tracemalloc.stop()
    return times, peaks, deltas


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cpu_model():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def main():
    meta = {
        "utc_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "cpu": _cpu_model(),
        "seed": SEED,
        "warmup": WARMUP,
        "repeats": REPEATS,
        "generation_cap_bytes": GENERATION_CAP_BYTES,
        "max_read_bytes": runstore_module.MAX_READ_BYTES,
        "report_py_sha256": _sha256(SCRIPTS / "mdi" / "report.py"),
        "runstore_py_sha256": _sha256(SCRIPTS / "mdi" / "runstore.py"),
    }
    results = {"meta": meta, "sizes": {}}
    print(json.dumps(meta, indent=2))
    for name, target, qual_cadence in SIZES:
        report, size, build_seconds, bundles = build_size(name, target, qual_cadence)
        started = time.perf_counter()
        successor = copy.deepcopy(report)
        successor["revision"] += 1
        twin = copy.deepcopy(report)
        deepcopy_seconds = time.perf_counter() - started
        validate_transition(report, successor)  # ground truth: the pair transitions
        nodes = _count_nodes(report)
        counts = {
            "subjects": len(report["subjects"]),
            "evidence": len(report["evidence"]),
            "analyses": len(report["analyses"]),
            "stages": len(report["stages"]),
            "operations": len(report["operations"]),
            "qualifications": len(report["qualifications"]),
        }
        print(f"\n== {name} ==")
        print(
            f"target {target} B | serialized {size} B | bundles {bundles} "
            f"| build {build_seconds:.3f}s | deepcopy {deepcopy_seconds:.3f}s"
        )
        print("records " + json.dumps(counts))
        print("nodes " + json.dumps(nodes))
        print(f"{'measurement':<26}{'time s (reps)':<34}{'alloc peak KiB (reps)':<30}{'alloc delta KiB (reps)'}")
        targets = (
            ("validate_report", lambda: validate_report(report)),
            ("validate_transition", lambda: validate_transition(report, successor)),
            ("_check_json", lambda: report_module._check_json(report, "report")),
            ("_required_limitations", lambda: report_module._required_limitations(report)),
            ("_json_equal_full_twin", lambda: report_module._json_equal(report, twin)),
            ("probe_paths_built", lambda: _probe_paths_built(report, "report")),
            ("probe_paths_none", lambda: _probe_paths_none(report, "report")),
        )
        times, peaks, deltas = measure_interleaved(targets)
        measurements = {}
        for label, _ in targets:
            measurements[label] = {
                "seconds": [round(t, 6) for t in times[label]],
                "alloc_peak_bytes": peaks[label],
                "alloc_delta_bytes": deltas[label],
            }
            print(
                f"{label:<26}"
                f"{' '.join(f'{t:.6f}' for t in times[label]):<34}"
                f"{' '.join(f'{p / 1024:.1f}' for p in peaks[label]):<30}"
                f"{' '.join(f'{d / 1024:.1f}' for d in deltas[label])}"
            )
        results["sizes"][name] = {
            "shape": {
                "target_bytes": target,
                "serialized_bytes": size,
                "bundles": bundles,
                "qual_cadence": qual_cadence,
                "build_seconds": round(build_seconds, 6),
                "deepcopy_seconds": round(deepcopy_seconds, 6),
                "records": counts,
                "json_nodes": nodes,
            },
            "measurements": measurements,
        }
    print("\n== raw json ==")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
