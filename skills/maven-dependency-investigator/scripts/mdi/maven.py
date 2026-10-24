"""Offline import of recorded Maven producer reports into JSON-safe evidence.

import_dependency_tree, import_effective_pom and import_classpath consume
already captured producer output as bytes and return plain JSON-safe dicts.
Nothing here runs Maven, opens files, or touches the network: source
identifiers and locators are caller supplied, the observation timestamp is
the real import time in UTC, and the recorded digest hashes the exact input
bytes. Grammar is pinned, never presumed: only the producer/version pairs in
SUPPORTED_PRODUCERS parse; anything else is rejected with an explicit code
instead of guessed at.

For the pinned dependency-plugin 3.11.0 JSON tree, node fields are observed
strings (optional is a string there, though a JSON boolean or 'true'/'false'
is accepted); raw and parsed values are both preserved, unknown extra node
fields are preserved verbatim and never interpreted, and node ids are tree
paths so one artifact under two paths stays two nodes. A verbose tree is
diagnostic only: every node's inclusion is unknown, resolved inclusion is
never computed, and duplicate coordinates are reported as observed facts
without omission or conflict semantics. A non-verbose tree is authoritative
for the context its provenance records and never for the current working
tree when that context is unknown, partial or mismatched. Provenance must
carry every key in PROVENANCE_FIELDS (values may be None) and the caller may
supply a current_context bundle compared field by field into an explicit
currency status. The subject realm is caller declared (project,
build-plugin) or unknown; it is never inferred. Effective-POM origins are
enumerated across the whole project subtree (dependencies,
dependencyManagement/BOM, build and plugin dependencies, profile-gated
fields) from immediate trailing sibling comments matching the observed
'coordinates, line N' grammar; locators follow the discover extractor
conventions, foreign-namespace subtrees are not descended, adjacency is
broken by any intervening node (processing instructions are preserved for
this check by an importer-only reparse of the same validated bytes, because
the shared xmlsafe tree drops them), absent comments stay absent, unknown
grammar is preserved raw, an iterative structural preflight bounds the
document first (every XML node counts against max_xml_nodes and max_depth:
the root, foreign-namespace interiors, comments and processing
instructions, all before any recursive extraction runs), a parsed nested
aggregator document is rejected rather than first-selected, and concatenated
multi-document input fails parsing as malformed XML instead of being
heuristically classified. Classpath tokens are opaque strings
split on a caller-declared separator: no existence check, no local-repository
layout mapping, no coordinate inference. Declaration extraction reuses the
private discover._extract_project/_root_problem/_split_tag/_tag_positions as
a documented temporary bridge (a public byte-level extractor does not exist
yet); those functions perform no I/O.
"""

import copy
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Comment

from mdi.discover import _extract_project, _kids, _root_problem, _split_tag, _tag_positions, _text
from mdi.limits import Limits
from mdi.xmlsafe import UnsafeXML, XMLDepthExceeded, parse_xml

SUPPORTED_PRODUCERS = {
    "dependency-tree": ("maven-dependency-plugin", "3.11.0"),
    "build-classpath": ("maven-dependency-plugin", "3.11.0"),
    "effective-pom": ("maven-help-plugin", "3.5.2"),
}
PROVENANCE_FIELDS = (
    "producer", "version", "format", "generated_at", "revision", "module",
    "profiles", "jdk", "maven", "settings_digest", "dirty", "working_tree_digest",
)
_PROVENANCE_STRINGS = frozenset(
    {"producer", "version", "format", "generated_at", "revision", "module",
     "jdk", "maven", "settings_digest", "working_tree_digest"}
)
CURRENCY_FIELDS = (
    "revision", "module", "profiles", "jdk", "maven",
    "settings_digest", "dirty", "working_tree_digest",
)
REALMS = ("project", "build-plugin", "unknown")
_NODE_STRING_FIELDS = ("groupId", "artifactId", "version", "type", "scope", "classifier")
_KNOWN_NODE_KEYS = frozenset(_NODE_STRING_FIELDS) | {"optional", "children"}
_ORIGIN_RE = re.compile(r"([^,]+?),\s*line\s+(\d+)")


class MavenImportError(ValueError):
    """Raised when recorded producer output or caller metadata is unacceptable.

    `code` is a stable machine-readable reason (e.g. "duplicate-json-key")
    so callers never match on message text.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _nonnegative_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return value


def _bytes_input(data) -> bytes:
    if not isinstance(data, bytes):
        raise TypeError(f"data must be bytes, got {type(data).__name__}")
    return data


def _check_json_safe(value, path: str, code: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MavenImportError(f"{path}: non-finite numbers are not JSON-safe", code)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MavenImportError(f"{path}: object keys must be strings", code)
            _check_json_safe(item, f"{path}.{key}", code)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_safe(item, f"{path}[{index}]", code)
        return
    raise MavenImportError(f"{path}: {type(value).__name__} is not JSON-safe", code)


def _validate_provenance(provenance) -> dict:
    if not isinstance(provenance, dict):
        raise TypeError(f"provenance must be dict, got {type(provenance).__name__}")
    unknown = sorted(set(provenance) - set(PROVENANCE_FIELDS))
    if unknown:
        raise MavenImportError(f"unknown provenance fields: {unknown}", "provenance-unknown-keys")
    missing = [name for name in PROVENANCE_FIELDS if name not in provenance]
    if missing:
        raise MavenImportError(f"missing provenance fields: {missing}", "provenance-missing-keys")
    for name in _PROVENANCE_STRINGS:
        value = provenance[name]
        if value is not None and (not isinstance(value, str) or not value):
            raise MavenImportError(f"provenance {name} must be a nonempty string or None",
                                   "provenance-shape")
    profiles = provenance["profiles"]
    if profiles is not None:
        if not isinstance(profiles, list):
            raise MavenImportError("provenance profiles must be a list of strings or None",
                                   "provenance-shape")
        for index, item in enumerate(profiles):
            if not isinstance(item, str) or not item:
                raise MavenImportError(f"provenance profiles[{index}] must be a nonempty string",
                                       "provenance-shape")
    if provenance["dirty"] is not None and not isinstance(provenance["dirty"], bool):
        raise MavenImportError("provenance dirty must be a bool or None", "provenance-shape")
    _check_json_safe(provenance, "provenance", "provenance-shape")
    return copy.deepcopy(provenance)


def _validate_current_context(current_context) -> None:
    if not isinstance(current_context, dict):
        raise TypeError("current_context must be dict or None")
    _check_json_safe(current_context, "current_context", "current-context-shape")


def _validated_realm(realm) -> str:
    if realm is None:
        return "unknown"
    if realm not in REALMS:
        raise MavenImportError(f"realm must be one of {', '.join(REALMS)} or None", "bad-realm")
    return realm


def _pin_producer(kind: str, provenance: dict) -> None:
    producer, version = SUPPORTED_PRODUCERS[kind]
    if provenance["producer"] != producer:
        raise MavenImportError(
            f"unsupported producer {provenance['producer']!r} for {kind}; "
            f"only {producer!r} output is pinned", "unsupported-producer")
    if provenance["version"] != version:
        raise MavenImportError(
            f"unsupported producer version {provenance['version']!r} for {kind}; "
            f"only {version!r} is pinned", "unsupported-producer-version")


def _context_report(provenance: dict, current_context) -> dict:
    if current_context is None:
        return {
            "current_context": None,
            "currency": "unknown",
            "fields": {},
            "notes": ["no current context was supplied; the report is authoritative only for "
                      "the context its provenance records"],
        }
    # snapshot the caller's bundle: the result records what was supplied at
    # import time and later caller mutations must not rewrite evidence
    current_context = copy.deepcopy(current_context)
    fields = {}
    for name in CURRENCY_FIELDS:
        if name not in current_context:
            continue
        recorded, current = provenance[name], current_context[name]
        if recorded is None and current is None:
            status = "match"
        elif recorded is None:
            status = "provenance-unknown"
        elif current is None:
            status = "current-unknown"
        elif recorded == current:
            status = "match"
        else:
            status = "mismatch"
        fields[name] = {"recorded": recorded, "current": current, "status": status}
    statuses = {field["status"] for field in fields.values()}
    if "mismatch" in statuses:
        currency = "mismatch"
    elif statuses & {"provenance-unknown", "current-unknown"}:
        currency = "partial"
    elif statuses:
        currency = "matching"
    else:
        currency = "unknown"
    notes = {
        "matching": ["all compared fields match the supplied current context"],
        "mismatch": ["the recorded provenance differs from the supplied current context; "
                     "the parsed evidence describes the recorded context only"],
        "partial": ["some compared fields are unknown on one side; currency is partial, "
                    "not established"],
        "unknown": ["no comparable fields were supplied; the report is authoritative only "
                    "for the context its provenance records"],
    }[currency]
    return {"current_context": current_context, "currency": currency,
            "fields": fields, "notes": notes}


def _base_result(kind: str, data: bytes, provenance: dict, current_context, realm: str) -> dict:
    return {
        "kind": kind,
        "imported_at": _now(),
        "input": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        "subject_realm": realm,
        "provenance": provenance,
        "context": _context_report(provenance, current_context),
    }


def _reject_constant(name: str):
    raise MavenImportError(f"{name} literal is not accepted", "non-finite-number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise MavenImportError(f"non-finite number {text!r}", "non-finite-number")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MavenImportError(f"duplicate JSON object key {key!r}", "duplicate-json-key")
        result[key] = value
    return result


def _strict_json(data: bytes):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MavenImportError(f"invalid UTF-8: {exc}", "invalid-utf8") from exc
    try:
        return json.loads(text, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except MavenImportError:
        raise
    except RecursionError as exc:
        raise MavenImportError("JSON nesting exceeds the parser depth", "json-depth-exceeded") from exc
    except ValueError as exc:
        raise MavenImportError(f"malformed JSON: {exc}", "malformed-json") from exc


def _parse_optional(raw):
    if raw is True or raw is False:
        return raw
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


def _checked_node(node) -> None:
    if not isinstance(node, dict):
        raise MavenImportError("tree node must be a JSON object", "malformed-tree-node")
    for name in _NODE_STRING_FIELDS:
        if name not in node or not isinstance(node[name], str):
            raise MavenImportError(f"tree node field {name!r} must be a string", "malformed-tree-node")
    if "optional" not in node or not isinstance(node["optional"], (str, bool)):
        raise MavenImportError("tree node field 'optional' must be a string or bool",
                               "malformed-tree-node")
    if "children" in node and not isinstance(node["children"], list):
        raise MavenImportError("tree node field 'children' must be a list", "malformed-tree-node")


def _coordinates(node: dict) -> str:
    return ":".join(node[name] for name in ("groupId", "artifactId", "version"))


def _walk_tree(root_value, max_nodes: int, max_depth: int, mode: str) -> list:
    _checked_node(root_value)
    inclusion = (
        {"status": "unknown",
         "reason": "verbose report is diagnostic; the producer emits no omission metadata"}
        if mode == "verbose" else
        {"status": "included-per-report",
         "qualifier": "authoritative for the recorded provenance context only"}
    )
    nodes, stack, count = [], [(root_value, None, [], 0, None)], 0
    while stack:
        raw, parent_id, parent_path, depth, index = stack.pop()
        count += 1
        if count > max_nodes:
            raise MavenImportError(f"tree exceeds max_nodes={max_nodes}", "tree-node-limit-exceeded")
        if depth > max_depth:
            raise MavenImportError(f"tree node at depth {depth} exceeds max_depth={max_depth}",
                                   "tree-depth-exceeded")
        _checked_node(raw)
        coords = _coordinates(raw)
        label = coords if index is None else f"{index}:{coords}"
        node_id = coords if parent_id is None else f"{parent_id}/{label}"
        path = list(parent_path) + [label]
        child_ids = []
        for child_index, child in enumerate(raw.get("children", [])):
            _checked_node(child)
            child_ids.append(f"{node_id}/{child_index}:{_coordinates(child)}")
        nodes.append({
            "id": node_id,
            "parent": parent_id,
            "depth": depth,
            "path": path,
            "coordinates": {name: raw[name] for name in ("groupId", "artifactId", "version")},
            "type": raw["type"],
            "scope": raw["scope"],
            "classifier": raw["classifier"],
            "optional": {"raw": raw["optional"], "parsed": _parse_optional(raw["optional"])},
            "children": child_ids,
            "inclusion": inclusion,
            "foreign_fields": {key: value for key, value in raw.items()
                               if key not in _KNOWN_NODE_KEYS},
        })
        for child_index, child in reversed(list(enumerate(raw.get("children", [])))):
            stack.append((child, node_id, path, depth + 1, child_index))
    return nodes


def _prepare(kind: str, data: bytes, provenance, current_context, realm, max_input_bytes: int):
    """Shared import prefix: bytes, size cap, provenance, current context,
    realm and producer pin, all checked before any parsing starts."""
    _bytes_input(data)
    _positive_int("max_input_bytes", max_input_bytes)
    recorded = _validate_provenance(provenance)
    if current_context is not None:
        _validate_current_context(current_context)
    realm = _validated_realm(realm)
    _pin_producer(kind, recorded)
    if len(data) > max_input_bytes:
        raise MavenImportError(
            f"input of {len(data)} bytes exceeds max_input_bytes={max_input_bytes}",
            "input-bytes-exceeded")
    return recorded, realm


def import_dependency_tree(data: bytes, *, provenance: dict, current_context: dict | None = None,
                           verbose: bool = False, realm: str | None = None,
                           max_input_bytes: int = 8 * 1024 * 1024,
                           max_nodes: int = 10000, max_depth: int = 64) -> dict:
    """Import a recorded dependency-plugin JSON tree as path-keyed graph evidence."""
    if not isinstance(verbose, bool):
        raise TypeError(f"verbose must be an explicit bool, got {type(verbose).__name__}")
    _positive_int("max_nodes", max_nodes)
    _nonnegative_int("max_depth", max_depth)
    recorded, realm = _prepare("dependency-tree", data, provenance, current_context,
                               realm, max_input_bytes)
    mode = "verbose" if verbose else "nonverbose"
    nodes = _walk_tree(_strict_json(data), max_nodes, max_depth, mode)
    by_artifact = {}
    for record in nodes:
        key = f"{record['coordinates']['groupId']}:{record['coordinates']['artifactId']}"
        entry = by_artifact.setdefault(key, {"coordinates": key, "versions": set(), "node_ids": []})
        entry["versions"].add(record["coordinates"]["version"])
        entry["node_ids"].append(record["id"])
    duplicates = [
        {"coordinates": entry["coordinates"], "versions": sorted(entry["versions"]),
         "node_ids": entry["node_ids"]}
        for entry in by_artifact.values() if len(entry["node_ids"]) > 1
    ]
    limitations = [
        ("verbose report is diagnostic: the producer emits no omission or conflict metadata, "
         "so resolved inclusion stays unknown for every node") if verbose else
        ("non-verbose report nodes are included per the report for the context its provenance "
         "records; this is never an authority claim about the current working tree"),
        "duplicate coordinates are observed facts only; no omission, conflict or mediation "
        "semantics are inferred",
        "optional is emitted as a string by the pinned producer; the raw value and the parsed "
        "boolean are both preserved",
    ]
    if any(record["foreign_fields"] for record in nodes):
        limitations.append("extra node fields beyond the pinned grammar are preserved verbatim "
                           "and left uninterpreted")
    result = _base_result("dependency-tree", data, recorded, current_context, realm)
    result["mode"] = mode
    result["inclusion_authority"] = "diagnostic-only" if verbose else "recorded-context"
    result["evidence"] = {"root": nodes[0]["id"], "nodes": nodes}
    result["coverage"] = {
        "resolved_inclusion": "not-computed",
        "conflict_mediation": "not-recorded-by-producer",
        "duplicate_coordinates": duplicates,
        "counts": {"nodes": len(nodes), "max_nodes": max_nodes, "max_depth": max_depth},
        "limitations": limitations,
    }
    return result


def _parse_with_pis(data: bytes):
    """Importer-only second ElementTree pass over the same bytes xmlsafe just
    validated (size cap, DTD rejection, full structure): the shared xmlsafe
    tree drops processing instructions, so origin adjacency could never see
    them. Preserving them here adds no new input surface and no guards."""
    builder = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    try:
        builder.feed(data)
        return builder.close()
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise MavenImportError(f"malformed XML: {exc}", "malformed-xml") from exc


def _origin_record(locator: str, text):
    """One origin record: a comment present is always preserved raw, and the
    structured origin parses only the known 'coordinates, line N' grammar."""
    record = {"xml_path": locator, "trailing_comment": None, "origin": None, "grammar": None}
    if text is not None:
        record["trailing_comment"] = {"raw": text, "stripped": text.strip()}
        match = _ORIGIN_RE.fullmatch(text.strip())
        if match:
            record["origin"] = {"coordinates": match.group(1).strip(), "line": int(match.group(2))}
            record["grammar"] = "known"
        else:
            record["grammar"] = "unknown"
    return record


def _structural_preflight(root, max_nodes: int, max_depth: int) -> dict:
    """Iterative preflight over every XML node of the parsed document: the
    root, every element including foreign-namespace interiors, every comment
    and processing instruction. All structural bounds are enforced here,
    before any itertext or recursive extraction can traverse the tree (those
    helpers descend into places the origin walk deliberately does not).
    Returns the structural counts; the root itself is part of the budget."""
    counts = {"xml_nodes": 1, "xml_elements": 1, "xml_comments": 0, "xml_pis": 0}
    stack = [(root, 0)]
    while stack:
        elem, depth = stack.pop()
        for child in elem:
            counts["xml_nodes"] += 1
            if counts["xml_nodes"] > max_nodes:
                raise MavenImportError(
                    f"document exceeds max_xml_nodes={max_nodes} "
                    "(every XML node counts, root included)", "xml-node-limit-exceeded")
            node_depth = depth + 1
            if child.tag is Comment:
                counts["xml_comments"] += 1
            elif isinstance(child.tag, str):
                counts["xml_elements"] += 1
                stack.append((child, node_depth))
            else:
                counts["xml_pis"] += 1
            if node_depth > max_depth:
                raise MavenImportError(f"XML node at depth {node_depth} exceeds "
                                       f"max_depth={max_depth}", "xml-depth-exceeded")
    return counts


def _origin_records(root, doc_ns: str):
    """Namespace-aware origin enumeration over the whole project subtree
    (dependencies, dependencyManagement/BOM, build/plugin dependencies,
    profile-gated fields): one record per document-namespace element, its
    locator following the discover extractor conventions (first same-tag
    sibling unindexed, repeats [n], unique-id top-level profiles as
    profile[@id='...']). A comment attaches only as the immediately
    following sibling node within the same parent; any intervening element
    (including foreign-namespace ones, whose subtrees are not descended) or
    processing instruction breaks that adjacency. The walk is iterative and
    enforces no bounds itself: _structural_preflight has already bounded
    every node it can touch, counting each exactly once. Returns
    (origins, unattached)."""
    origins, unattached = [], []
    positions = {}

    def locate(parent_path, parent, child):
        table = positions.get(id(parent))
        if table is None:
            table = _tag_positions(parent)
            positions[id(parent)] = table
        ordinal = table[id(child)]
        suffix = f"[{ordinal + 1}]" if ordinal else ""
        return f"{parent_path}/{_split_tag(child.tag)[1]}{suffix}"

    stack = [(root, _split_tag(root.tag)[1] or "project", 0, False)]
    while stack:
        parent, parent_path, depth, parent_is_root_child = stack.pop()
        profile_ids = None
        if parent_is_root_child and _split_tag(parent.tag)[1] == "profiles":
            identified = [(elem, _text(elem, "id", doc_ns) or None)
                          for elem in _kids(parent, "profile", doc_ns)]
            id_counts = {}
            for _elem, pid in identified:
                if pid is not None:
                    id_counts[pid] = id_counts.get(pid, 0) + 1
            profile_ids = {id(elem): (pid, pid is not None and "'" not in pid
                                      and id_counts[pid] == 1) for elem, pid in identified}
        pending, pushes = None, []
        for child in parent:
            if child.tag is Comment:
                if pending is not None:
                    origins.append(_origin_record(pending, child.text or ""))
                    pending = None
                else:
                    unattached.append(child.text or "")
                continue
            if not isinstance(child.tag, str):  # processing instruction: breaks adjacency
                if pending is not None:
                    origins.append(_origin_record(pending, None))
                pending = None
                continue
            if pending is not None:
                origins.append(_origin_record(pending, None))
            if _split_tag(child.tag)[0] != doc_ns:  # foreign subtree: no descent
                pending = None
                continue
            if profile_ids is not None and id(child) in profile_ids:
                pid, unique = profile_ids[id(child)]
                locator = (f"{parent_path}/profile[@id='{pid}']" if unique
                           else locate(parent_path, parent, child))
            else:
                locator = locate(parent_path, parent, child)
            pending = locator
            pushes.append((child, locator, depth + 1, parent is root))
        if pending is not None:
            origins.append(_origin_record(pending, None))
        stack.extend(reversed(pushes))
    return origins, unattached


def import_effective_pom(data: bytes, *, provenance: dict, current_context: dict | None = None,
                         realm: str | None = None, pom_locator: str | None = None,
                         xml_bytes: int = 8 * 1024 * 1024,
                         max_xml_nodes: int = 50000, max_depth: int = 64) -> dict:
    """Import a recorded help-plugin effective POM as declarations plus
    field-origin evidence across the project subtree; pom_locator is a caller
    label, never opened."""
    if pom_locator is not None and (not isinstance(pom_locator, str) or not pom_locator):
        raise ValueError("pom_locator must be a nonempty string or None")
    _positive_int("max_xml_nodes", max_xml_nodes)
    _positive_int("xml_bytes", xml_bytes)
    _nonnegative_int("max_depth", max_depth)
    recorded, realm = _prepare("effective-pom", data, provenance, current_context,
                               realm, xml_bytes)
    try:
        root = parse_xml(data, limits=Limits(xml_bytes=xml_bytes))
    except XMLDepthExceeded as exc:
        raise MavenImportError(str(exc), "xml-depth-exceeded") from exc
    except UnsafeXML as exc:
        # no byte heuristic classifies trailing junk: concatenated
        # multi-document output is reported as what it is, malformed XML,
        # and nothing is ever first-selected.
        raise MavenImportError(f"malformed XML: {exc}", "malformed-xml") from exc
    problem = _root_problem(root)
    if problem is not None:
        raise MavenImportError(problem, "not-a-project-root")
    doc_ns = _split_tag(root.tag)[0]
    direct = [child for child in root if isinstance(child.tag, str)]
    if any(_split_tag(child.tag) == (doc_ns, "project") for child in direct):
        raise MavenImportError("nested project documents are unsupported aggregator output; "
                               "not selecting the first", "unsupported-aggregator-multi-project")
    doc = _parse_with_pis(data)
    # the iterative structural preflight bounds every node (root, foreign
    # interiors, comments, PIs) before the origin walk, the itertext-based
    # profile detection and the recursive discover extractor run
    counts = _structural_preflight(doc, max_xml_nodes, max_depth)
    origins, unattached = _origin_records(doc, doc_ns)
    declarations, _module_refs, meta = _extract_project(doc, recorded["module"], pom_locator)
    result = _base_result("effective-pom", data, recorded, current_context, realm)
    result["evidence"] = {
        "project": meta,
        "declarations": declarations,
        "origins": origins,
        "unattached_comment_texts": unattached,
    }
    result["coverage"] = {
        "inclusion": "not-claimed",
        "counts": {"declarations": len(declarations), "origins": len(origins), **counts},
        "limitations": [
            "effective-model declarations are not resolved inclusion evidence; "
            "dependencyManagement and BOM-import entries stay management-only",
            "origin comments attach to document-namespace elements of the project subtree "
            "only as immediate trailing siblings within the same parent; intervening "
            "elements (including foreign-namespace ones) and processing instructions break "
            "adjacency, and absent comments are never invented",
            "origin locators follow the discover declaration xml_path conventions, including "
            "profile[@id='...'] for unique-id top-level profiles; foreign-namespace subtrees "
            "are not descended",
            "profile activation remains unknown; parent and BOM mediation are not evaluated",
            "a structural preflight counts every XML node, root and foreign-namespace "
            "interiors included, against max_xml_nodes and max_depth before any extraction",
            "nested aggregator project documents are rejected explicitly rather than "
            "first-selected; concatenated multi-document input fails parsing as malformed "
            "XML, never first-selected",
        ],
    }
    return result


def import_classpath(data: bytes, *, provenance: dict, current_context: dict | None = None,
                     realm: str | None = None, separator: str = os.pathsep,
                     max_input_bytes: int = 8 * 1024 * 1024,
                     max_paths: int = 4096) -> dict:
    """Import a recorded build-classpath as verbatim separator-split tokens."""
    if not isinstance(separator, str):
        raise TypeError(f"separator must be str, got {type(separator).__name__}")
    if len(separator) != 1 or separator == "\x00":
        raise MavenImportError("separator must be exactly one non-NUL character", "bad-separator")
    _positive_int("max_paths", max_paths)
    recorded, realm = _prepare("build-classpath", data, provenance, current_context,
                               realm, max_input_bytes)
    if b"\x00" in data:
        raise MavenImportError("classpath data contains a NUL byte", "classpath-nul-byte")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MavenImportError(f"invalid UTF-8: {exc}", "invalid-utf8") from exc
    paths = text.split(separator) if text else []
    if len(paths) > max_paths:
        raise MavenImportError(f"classpath of {len(paths)} paths exceeds max_paths={max_paths}",
                               "classpath-path-limit-exceeded")
    result = _base_result("build-classpath", data, recorded, current_context, realm)
    result["evidence"] = {
        "paths": paths,
        "separator": separator,
        "platform_pathsep": os.pathsep,
        "path_count": len(paths),
    }
    result["coverage"] = {
        "existence_checked": False,
        "limitations": [
            "paths are recorded verbatim; no filesystem existence check, local-repository "
            "layout mapping or coordinate inference is performed",
            "the separator is caller-declared; tokens are opaque strings, not validated paths",
        ],
    }
    return result
