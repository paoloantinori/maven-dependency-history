"""Pure, bounded correspondence between two inspected ZIP entry inventories.

The inputs are results from archives.inspect_archive, not archive bytes or
paths to reopen. Exactly one container from each inventory is compared;
by default it is the outer container (empty entry_chain). Nested containers
require explicit archive IDs, so classes from unrelated nested jars never
silently pool. Matching is multiset-based and reserves exact path/digest
matches before considering nonempty equal bytes under different paths.

A complete result describes entry-content correspondence only. It does not
identify a whole artifact, establish a version, prove relocation, resolve a
dependency graph, or establish which checkout built an enclosing archive.
Digests and inspection status are supplied observations, not independently
recaptured by this pure matcher. Caller-supplied metadata remains candidate
identity even when every observed entry matches.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, fields
import copy
import json
import math
import re


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LIMITATIONS = (
    "entry-content correspondence is not whole-artifact byte identity; ZIP layout, "
    "compression, metadata and surplus entries may differ",
    "nonempty equal bytes under a different path are candidate correspondence only, "
    "not proof of relocation or component identity; common content can be shared by unrelated libraries",
    "metadata coordinates are unverified candidates; no exact component version is inferred from names",
    "no resolved inclusion, historical introduction or checkout build provenance is established",
    "only the explicitly selected archive inventories are compared; missing observed entries "
    "do not prove global dependency absence",
    "supplied digests and completeness flags are trusted observations, not recomputed from archive bytes",
)


class ShadedError(ValueError):
    """Malformed or over-budget input; never an empty successful match."""


@dataclass(frozen=True)
class MatchLimits:
    max_entries: int = 20000
    max_archives: int = 4096
    max_input_bytes: int = 16 * 1024 * 1024
    max_output_bytes: int = 24 * 1024 * 1024
    max_text: int = 4096

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")


def _prevalidate_json(value, maximum, max_text, label):
    """Bound objects before encoding can allocate escaped strings or integers.

    The running charge is a lower bound on serialized size; the encoder
    subsequently enforces the exact byte limit. Depth and collection sizes
    are checked before descent, without copying or materializing children.
    """
    charged = 0

    def visit(node, depth):
        nonlocal charged
        if depth > 64:
            raise ShadedError(f"{label} exceeds JSON nesting bound")
        charged += 1
        if type(node) is str:
            if len(node) > min(max_text, maximum):
                raise ShadedError(f"{label} contains over-bound text")
            charged += len(node)
        elif type(node) in (list, dict):
            if len(node) > maximum - charged:
                raise ShadedError(f"{label} exceeds container size bound")
            if type(node) is dict:
                for key, child in node.items():
                    if type(key) is not str:
                        raise ShadedError(f"{label} requires string object keys")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in node:
                    visit(child, depth + 1)
        elif type(node) is int:
            if node.bit_length() > min(4096, maximum):
                raise ShadedError(f"{label} integer exceeds encoding bound")
        elif type(node) is float:
            if not math.isfinite(node):
                raise ShadedError(f"{label} contains a nonfinite number")
        elif node is not None and type(node) is not bool:
            raise ShadedError(f"{label} must contain JSON values only")
        if charged > maximum:
            raise ShadedError(f"{label} exceeds serialized size bound")

    visit(value, 0)


def _json_bound(value, maximum, label, max_text):
    _prevalidate_json(value, maximum, max_text, label)
    total = 0
    try:
        for chunk in json.JSONEncoder(allow_nan=False, ensure_ascii=True).iterencode(value):
            total += len(chunk)
            if total > maximum:
                raise ShadedError(f"{label} exceeds its serialized byte bound")
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ShadedError):
            raise
        raise ShadedError(f"{label} must be bounded JSON-safe data") from exc


def _text(value, limits, label):
    if not isinstance(value, str) or not value or len(value) > limits.max_text or "\x00" in value:
        raise ShadedError(f"{label} must be nonempty bounded text without NUL")
    return value


def _digest(value):
    if value is not None and (type(value) is not str or _DIGEST.fullmatch(value) is None):
        raise ShadedError("sha256 must be null or 64 lowercase hex characters")


def _size(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ShadedError("entry size must be null or a nonnegative integer")


def _known(entry):
    digest = entry.get("sha256")
    size = entry.get("size_actual")
    return (entry["status"] == "hashed" and isinstance(digest, str)
            and _DIGEST.fullmatch(digest) is not None
            and type(size) is int and size >= 0
            and type(entry.get("size_declared")) is int and size == entry["size_declared"])


def _inventory(value, requested, limits, label):
    if not isinstance(value, dict):
        raise ShadedError(f"{label} must be an inspection object")
    _json_bound(value, limits.max_input_bytes, label, limits.max_text)
    archives, entries = value.get("archives"), value.get("entries")
    if not isinstance(archives, list) or not isinstance(entries, list):
        raise ShadedError(f"{label} requires archives and entries lists")
    if len(archives) > limits.max_archives or len(entries) > limits.max_entries:
        raise ShadedError(f"{label} exceeds inventory count bounds")
    by_id = {}
    roots = []
    for archive in archives:
        if not isinstance(archive, dict):
            raise ShadedError(f"{label} archive must be an object")
        aid = _text(archive.get("archive_id"), limits, "archive_id")
        if aid in by_id:
            raise ShadedError("duplicate archive_id")
        chain = archive.get("entry_chain")
        if not isinstance(chain, list) or any(type(i) is not int or i < 0 for i in chain):
            raise ShadedError("entry_chain must contain nonnegative integers")
        _text(archive.get("path"), limits, "archive path")
        _text(archive.get("display_path"), limits, "archive display_path")
        _text(archive.get("inspection_status"), limits, "inspection_status")
        _digest(archive.get("sha256"))
        _size(archive.get("size"))
        by_id[aid] = archive
        if not chain:
            roots.append(aid)
    if requested is None:
        if len(roots) != 1:
            raise ShadedError(f"{label} requires exactly one outer archive or an explicit archive_id")
        requested = roots[0]
    _text(requested, limits, "requested archive_id")
    if requested not in by_id:
        raise ShadedError(f"{label} requested archive_id not found")
    selected = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ShadedError("entry must be an object")
        aid, index = entry.get("archive_id"), entry.get("index")
        if not isinstance(aid, str) or aid not in by_id or type(index) is not int or index < 0:
            raise ShadedError("entry must name an existing archive and nonnegative index")
        identity = (aid, index)
        if identity in seen:
            raise ShadedError("duplicate archive entry identity")
        seen.add(identity)
        _text(entry.get("name"), limits, "entry name")
        _text(entry.get("status"), limits, "entry status")
        _digest(entry.get("sha256"))
        _size(entry.get("size_actual"))
        _size(entry.get("size_declared"))
        if entry.get("kind") not in ("file", "directory", "symlink"):
            raise ShadedError("unsupported entry kind")
        if aid == requested and entry["kind"] != "directory":
            selected.append(entry)
    archive = by_id[requested]
    complete = archive["inspection_status"] == "complete" and all(_known(e) for e in selected)
    return archive, selected, complete


def _archive_locator(archive):
    return {key: copy.deepcopy(archive.get(key)) for key in
            ("archive_id", "path", "display_path", "entry_chain", "sha256", "inspection_status")}


def _entry_locator(archive, entry):
    return {"archive_id": archive["archive_id"], "path": archive["path"],
            "entry_chain": list(archive["entry_chain"]), "entry_index": entry["index"],
            "entry_name": entry["name"], "sha256": entry.get("sha256"),
            "size_actual": entry.get("size_actual"), "status": entry["status"]}


def _identity(archive):
    identity = archive.get("identity")
    if not isinstance(identity, dict) or not isinstance(identity.get("candidates"), list):
        return {"basis": "metadata-candidates-only", "status": "unknown", "conflict": False,
                "candidates": []}
    candidates = copy.deepcopy(identity["candidates"])
    coordinates = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ShadedError("metadata candidate must be an object")
        values = tuple(candidate.get(key) for key in ("groupId", "artifactId", "version"))
        if any(not isinstance(v, str) or not v for v in values):
            raise ShadedError("metadata candidate coordinates must be nonempty strings")
        coordinates.add(values)
    return {"basis": "metadata-candidates-only", "status": "candidate" if candidates else "unknown",
            "conflict": identity.get("conflict") is True or len(coordinates) > 1,
            "candidates": candidates}


def match_archives(container, reference, *, container_archive_id=None,
                   reference_archive_id=None, limits=MatchLimits()):
    """Compare selected archive records without I/O or mutation of caller data.

    Limits apply to each complete supplied inventory (not just selected
    entries), then to the serialized result. Text fields, JSON nesting
    (64 levels) and scalar/container shapes are checked before encoding;
    max_text also bounds metadata and unknown-field strings. Over-budget results raise
    ShadedError rather than silently dropping evidence. Equal duplicate
    entries are paired in inventory order for accounting, with ambiguity
    explicit; that pairing never attributes semantic ownership.
    """
    if not isinstance(limits, MatchLimits):
        raise ShadedError("limits must be MatchLimits")
    ca, ce, container_complete = _inventory(container, container_archive_id, limits, "container")
    ra, re, reference_complete = _inventory(reference, reference_archive_id, limits, "reference")
    exact = defaultdict(deque)
    digest_entries = defaultdict(deque)
    path_contents = defaultdict(set)
    for pos, entry in enumerate(ce):
        if _known(entry):
            path_contents[entry["name"]].add((entry["sha256"], entry["size_actual"]))
            key = (entry["name"], entry["sha256"], entry["size_actual"])
            exact[key].append(pos)
            if entry["size_actual"] > 0:
                digest_entries[(entry["sha256"], entry["size_actual"])].append(pos)
    used = set()
    matches = []
    for entry in re:
        row = {"reference": _entry_locator(ra, entry), "container": None,
               "classification": "unresolved-reference", "pairing_ambiguous": False}
        if _known(entry):
            row["classification"] = "pending"
            pool = exact[(entry["name"], entry["sha256"], entry["size_actual"])]
            if pool:
                row["pairing_ambiguous"] = len(pool) > 1
                pos = pool.popleft()
                used.add(pos)
                row.update(container=_entry_locator(ca, ce[pos]), classification="exact")
        matches.append(row)
    # Remove all exact reservations once, not merely used entries at the
    # front. Ambiguity counts must include only available correspondences.
    for key, pool in digest_entries.items():
        digest_entries[key] = deque(pos for pos in pool if pos not in used)
    for entry, row in zip(re, matches):
        if row["classification"] != "pending":
            continue
        pool = digest_entries[(entry["sha256"], entry["size_actual"])] if entry["size_actual"] > 0 else deque()
        if pool:
            # All exact correspondences were reserved first; a remaining
            # equal digest cannot share this reference entry's exact path.
            row["pairing_ambiguous"] = len(pool) > 1
            pos = pool.popleft()
            used.add(pos)
            row.update(container=_entry_locator(ca, ce[pos]), classification="byte-equal-different-path")
        elif path_contents[entry["name"]] and (
            len(path_contents[entry["name"]]) > 1
            or (entry["sha256"], entry["size_actual"]) not in path_contents[entry["name"]]
        ):
            row["classification"] = "path-equal-bytes-differ"
        else:
            row["classification"] = "missing" if container_complete else "unresolved-container"
    counts = {name: sum(row["classification"] == name for row in matches) for name in
              ("exact", "byte-equal-different-path", "path-equal-bytes-differ", "missing",
               "unresolved-reference", "unresolved-container")}
    exact_count, changed_count = counts["exact"], counts["byte-equal-different-path"]
    if re and exact_count == len(re) and reference_complete and container_complete:
        verdict = "complete-entry-content-correspondence"
    elif exact_count:
        verdict = "partial-entry-content-correspondence"
    elif changed_count:
        verdict = "candidate-entry-content-correspondence"
    else:
        verdict = "unresolved"
    result = {
        "kind": "shaded-entry-correspondence", "verdict": verdict,
        "artifact_byte_identity": False, "relocation_proven": False,
        "container": _archive_locator(ca), "reference": _archive_locator(ra),
        "container_identity": _identity(ca), "reference_identity": _identity(ra),
        "matches": matches,
        "surplus": [_entry_locator(ca, entry) for pos, entry in enumerate(ce) if pos not in used],
        "coverage": {"reference_entries": len(re), "container_entries": len(ce),
                     "reference_complete": reference_complete, "container_complete": container_complete,
                     "exact_matches": exact_count, "changed_path_matches": changed_count,
                     "conflicts": counts["path-equal-bytes-differ"], "missing": counts["missing"],
                     "unresolved": counts["unresolved-reference"] + counts["unresolved-container"],
                     "exact_fraction": exact_count / len(re) if re else None},
        "limitations": list(_LIMITATIONS),
    }
    if not reference_complete or not container_complete:
        result["limitations"].append("one or both selected inventories have incomplete entry inspection")
    if result["container_identity"]["conflict"] or result["reference_identity"]["conflict"]:
        result["limitations"].append("conflicting coordinate metadata remains unresolved despite entry matches")
    _json_bound(result, limits.max_output_bytes, "result", max(limits.max_text, 4096))
    return result
