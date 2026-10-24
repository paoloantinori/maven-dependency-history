"""Versioned, JSON-safe evidence reports for Maven dependency investigation.

A report is a plain dict that must survive a JSON round trip unchanged: every
value is None, bool, int, finite float, string, list, or an object whose keys
are strings; nothing is ever coerced, so non-string keys, non-finite numbers
and non-JSON types are rejected outright. The schema is versioned and
validated field by field, and unknown fields are rejected everywhere.

Documented choices for fields the contracts leave open: identifiers, names,
coordinates, categories, locators, timestamps, producer identities, plugin
references, disclosure and free text are nonempty strings; contextual bundles
(build_context, packaged_copy, captured_identity, generation_context, payload,
coverage, scope, tool_identity, audit) are JSON objects whose contents are
unconstrained beyond JSON safety, because category-specific payload semantics
belong to future collectors. Operation input identities are lists of objects
shaped exactly {path, sha256}, with a nonempty path and the full 64-character
lowercase hex sha256 (matching hashlib hexdigest output, never normalized) of
one captured local input; the list is empty only when the operation consulted
no local inputs, such as a pure network lookup. `query` is None, a nonempty
string, or a small JSON object with nonempty string keys and JSON-safe values;
both spellings are preserved exactly as given. Selection candidates are
nonempty and unique: 'all' means the frozen explicit set of query-matching
candidates the caller enumerated before recording the choice, not every
subject in the report, which may also hold contextual upstream subjects.
All references are run-local: they resolve against the collections of the
same report and never across reports.

Transition rules keep history honest: existing subjects, evidence, analyses
and qualifications cannot be removed, reordered or modified; analyses may only
gain the limitation strings that qualifications targeting their evidence
require; stages are updatable in place by name because the prior revision
snapshot still holds the historical view; operations move through explicit
single-use states, where newly appended operations must begin proposed, while
their scope, inputs, tool identity and disclosure stay immutable. Revisions
advance by exactly one within one run; the selection is frozen exactly as
recorded, never auto-grown, and investigating anything else requires a new
investigation rather than a silent re-selection.
"""

import copy
import math
import re


class ReportError(ValueError):
    """Raised when a report, a qualification, or a transition violates its contract."""


SCHEMA_VERSION = 1

_SUBJECT_REALMS = ("project", "build-plugin", "unknown")
_STATEMENT_KINDS = ("observation-summary", "inference")
_STAGE_OUTCOMES = ("complete", "partial", "unavailable", "denied", "failed", "not_applicable")
_OPERATION_STATES = ("proposed", "denied", "consumed", "finished")
_SELECTION_MODES = ("single", "all")
_SELECTION_FIELDS = frozenset({"candidate_ids", "mode"})

_TOP_FIELDS = frozenset(
    {
        "schema_version", "run_id", "revision", "query", "selection",
        "subjects", "evidence", "analyses", "stages", "operations", "qualifications",
    }
)
_TOP_LISTS = ("subjects", "evidence", "analyses", "stages", "operations", "qualifications")

_SPECS = {
    "subject": (
        ("id", "realm"),
        ("module", "coordinates", "owning_plugin", "build_context", "packaged_copy"),
    ),
    "evidence": (
        ("id", "category", "source_locator", "observed_at", "payload"),
        ("subject_id", "captured_identity", "producer", "producer_version", "generation_context"),
    ),
    "analysis": (("id", "evidence_ids", "statement_kind", "limitations"), ("statement",)),
    "stage": (("name", "outcome", "coverage", "reasons", "evidence_ids"), ()),
    "operation": (
        (
            "id", "run_id", "state", "scope", "input_identities",
            "tool_identity", "disclosure", "audit", "evidence_ids",
        ),
        (),
    ),
    "qualification": (("id", "affected_evidence_ids", "kind", "reason", "observed_at"), ()),
}

_ALLOWED_STATE_MOVES = {
    ("proposed", "proposed"), ("proposed", "denied"), ("proposed", "consumed"),
    ("denied", "denied"), ("consumed", "consumed"), ("consumed", "finished"),
    ("finished", "finished"),
}
_IMMUTABLE_OPERATION_FIELDS = ("scope", "input_identities", "tool_identity", "disclosure")
_IDENTITY_FIELDS = frozenset({"path", "sha256"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _fail(path, message):
    raise ReportError(f"{path}: {message}")


def _json_equal(left, right):
    """Deep equality for validated JSON that keeps type fidelity: True is not
    1, 1 is not 1.0, and 0.0 is not -0.0. Object key order is insignificant.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _json_equal(item, right[key]) for key, item in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(item, other) for item, other in zip(left, right)
        )
    if isinstance(left, float):
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    return left == right


def _check_json(value, path):
    """Reject anything that would not survive a JSON round trip unchanged."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(path, "non-finite numbers are not JSON-safe")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, f"object keys must be strings, got {type(key).__name__}")
            _check_json(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json(item, f"{path}[{index}]")
        return
    _fail(path, f"{type(value).__name__} is not JSON-safe")


def _nonempty_str(value, path):
    if not isinstance(value, str) or not value:
        _fail(path, "must be a nonempty string")


def _obj(value, path):
    if not isinstance(value, dict):
        _fail(path, "must be a JSON object")


def _enum(value, allowed, path):
    if not isinstance(value, str) or value not in allowed:
        _fail(path, f"must be one of {', '.join(allowed)}")


def _str_list(value, path):
    if not isinstance(value, list):
        _fail(path, "must be a list")
    for index, item in enumerate(value):
        _nonempty_str(item, f"{path}[{index}]")


def _check_refs(refs, known, path, target="evidence"):
    if not isinstance(refs, list):
        _fail(path, "must be a list")
    for index, ref in enumerate(refs):
        _nonempty_str(ref, f"{path}[{index}]")
        if ref not in known:
            _fail(f"{path}[{index}]", f"unknown {target} reference {ref!r}")


def _check_query(query, path="query"):
    if query is None:
        return
    if isinstance(query, str):
        _nonempty_str(query, path)
        return
    if isinstance(query, dict):
        _check_json(query, path)
        for key in query:
            _nonempty_str(key, f"{path} key {key!r}")
        return
    _fail(path, "must be None, a nonempty string, or a JSON object")


def _check_record_shape(record, kind, path):
    if not isinstance(record, dict):
        _fail(path, f"{kind} must be a JSON object")
    required, optional = _SPECS[kind]
    unknown = sorted(set(record) - set(required) - set(optional))
    if unknown:
        _fail(path, f"unknown {kind} fields: {unknown}")
    missing = [name for name in required if name not in record]
    if missing:
        _fail(path, f"missing {kind} fields: {missing}")


def _check_subject(record, path):
    _nonempty_str(record["id"], f"{path}.id")
    _enum(record["realm"], _SUBJECT_REALMS, f"{path}.realm")
    for field in ("module", "coordinates", "owning_plugin"):
        if field in record:
            _nonempty_str(record[field], f"{path}.{field}")
    for field in ("build_context", "packaged_copy"):
        if field in record:
            _obj(record[field], f"{path}.{field}")


def _check_evidence(record, path, subject_ids):
    for field in ("id", "category", "source_locator", "observed_at"):
        _nonempty_str(record[field], f"{path}.{field}")
    _obj(record["payload"], f"{path}.payload")
    for field in ("producer", "producer_version"):
        if field in record:
            _nonempty_str(record[field], f"{path}.{field}")
    for field in ("captured_identity", "generation_context"):
        if field in record:
            _obj(record[field], f"{path}.{field}")
    if "subject_id" in record:
        _nonempty_str(record["subject_id"], f"{path}.subject_id")
        if record["subject_id"] not in subject_ids:
            _fail(f"{path}.subject_id", f"unknown subject reference {record['subject_id']!r}")


def _check_input_identity(value, path):
    if not isinstance(value, dict):
        _fail(path, "must be a JSON object with path and sha256")
    unknown = sorted(set(value) - _IDENTITY_FIELDS)
    if unknown:
        _fail(path, f"unknown input identity fields: {unknown}")
    for name in ("path", "sha256"):
        if name not in value:
            _fail(path, f"missing input identity field {name!r}")
    _nonempty_str(value["path"], f"{path}.path")
    digest = value["sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        _fail(f"{path}.sha256", "must be a 64-character lowercase hex sha256")


def _check_analysis(record, path, evidence_ids):
    _nonempty_str(record["id"], f"{path}.id")
    _enum(record["statement_kind"], _STATEMENT_KINDS, f"{path}.statement_kind")
    _str_list(record["limitations"], f"{path}.limitations")
    if record["statement_kind"] == "inference" and not record["limitations"]:
        _fail(f"{path}.limitations", "inference requires at least one limitation")
    if not isinstance(record["evidence_ids"], list) or not record["evidence_ids"]:
        _fail(f"{path}.evidence_ids", "must be a nonempty list of evidence references")
    _check_refs(record["evidence_ids"], evidence_ids, f"{path}.evidence_ids")
    if "statement" in record:
        _nonempty_str(record["statement"], f"{path}.statement")


def _check_stage(record, path, evidence_ids):
    _nonempty_str(record["name"], f"{path}.name")
    _enum(record["outcome"], _STAGE_OUTCOMES, f"{path}.outcome")
    _obj(record["coverage"], f"{path}.coverage")
    _str_list(record["reasons"], f"{path}.reasons")
    if record["outcome"] not in ("complete", "not_applicable") and not record["reasons"]:
        _fail(f"{path}.reasons", f"outcome {record['outcome']!r} requires at least one reason")
    _check_refs(record["evidence_ids"], evidence_ids, f"{path}.evidence_ids")


def _check_operation(record, path, report, evidence_ids):
    _nonempty_str(record["id"], f"{path}.id")
    _nonempty_str(record["run_id"], f"{path}.run_id")
    if record["run_id"] != report["run_id"]:
        _fail(f"{path}.run_id", f"must match report run_id {report['run_id']!r}")
    _enum(record["state"], _OPERATION_STATES, f"{path}.state")
    _obj(record["scope"], f"{path}.scope")
    if not isinstance(record["input_identities"], list):
        _fail(f"{path}.input_identities", "must be a list")
    for index, identity in enumerate(record["input_identities"]):
        _check_input_identity(identity, f"{path}.input_identities[{index}]")
    _obj(record["tool_identity"], f"{path}.tool_identity")
    _nonempty_str(record["disclosure"], f"{path}.disclosure")
    _obj(record["audit"], f"{path}.audit")
    _check_refs(record["evidence_ids"], evidence_ids, f"{path}.evidence_ids")


def _check_qualification(record, path, evidence_ids):
    _nonempty_str(record["id"], f"{path}.id")
    affected = record["affected_evidence_ids"]
    if not isinstance(affected, list) or not affected:
        _fail(f"{path}.affected_evidence_ids", "must be a nonempty list")
    _check_refs(affected, evidence_ids, f"{path}.affected_evidence_ids")
    for field in ("kind", "reason", "observed_at"):
        _nonempty_str(record[field], f"{path}.{field}")


def _check_collection(records, kind, field, checker):
    seen = set()
    identity_field = "name" if kind == "stage" else "id"
    for index, record in enumerate(records):
        path = f"{field}[{index}]"
        _check_record_shape(record, kind, path)
        checker(record, path)
        identity = record[identity_field]
        if identity in seen:
            _fail(path, f"duplicate {kind} {identity!r}")
        seen.add(identity)
    return seen


def _check_selection(selection, subject_ids):
    if selection is None:
        return
    if not isinstance(selection, dict):
        _fail("selection", "must be None or a JSON object")
    unknown = sorted(set(selection) - _SELECTION_FIELDS)
    if unknown:
        _fail("selection", f"unknown selection fields: {unknown}")
    for name in ("candidate_ids", "mode"):
        if name not in selection:
            _fail("selection", f"missing selection field {name!r}")
    _enum(selection["mode"], _SELECTION_MODES, "selection.mode")
    candidates = selection["candidate_ids"]
    if not isinstance(candidates, list) or not candidates:
        _fail("selection.candidate_ids", "must be a nonempty list of candidates")
    _check_refs(candidates, subject_ids, "selection.candidate_ids", target="subject")
    if len(set(candidates)) != len(candidates):
        _fail("selection.candidate_ids", "candidates must be unique")
    if selection["mode"] == "single" and len(candidates) != 1:
        _fail("selection.candidate_ids", "single mode requires exactly one candidate")


def _qualification_text(qualification):
    """The stable limitation string a qualification requires of analyses."""
    return f"{qualification['kind']}: {qualification['reason']}"


def _required_limitations(report):
    """Map analysis id to the limitation strings its evidence qualifications require."""
    required = {}
    for qualification in report["qualifications"]:
        text = _qualification_text(qualification)
        affected = set(qualification["affected_evidence_ids"])
        for analysis in report["analyses"]:
            if not affected.isdisjoint(analysis["evidence_ids"]):
                required.setdefault(analysis["id"], set()).add(text)
    return required


def new_report(run_id, query=None):
    """Create an empty revision-0 report for `run_id`, preserving `query` as given."""
    if not isinstance(run_id, str) or not run_id:
        raise ReportError(f"run_id must be a nonempty string, got {run_id!r}")
    _check_query(query)
    if isinstance(query, dict):
        query = copy.deepcopy(query)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "revision": 0,
        "query": query,
        "selection": None,
        "subjects": [],
        "evidence": [],
        "analyses": [],
        "stages": [],
        "operations": [],
        "qualifications": [],
    }


def validate_report(report):
    """Raise ReportError unless `report` satisfies the full schema contract."""
    if not isinstance(report, dict):
        raise ReportError(f"report must be a JSON object, got {type(report).__name__}")
    unknown = sorted(set(report) - _TOP_FIELDS, key=repr)
    if unknown:
        raise ReportError(f"unknown top-level fields: {unknown}")
    missing = sorted(_TOP_FIELDS - set(report))
    if missing:
        raise ReportError(f"missing top-level fields: {missing}")
    _check_json(report, "report")
    for field in _TOP_LISTS:
        if not isinstance(report[field], list):
            _fail(field, "must be a list")
    for field in ("schema_version", "revision"):
        if isinstance(report[field], bool) or not isinstance(report[field], int):
            _fail(field, "must be an int, not bool")
    if report["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version", f"unsupported version {report['schema_version']!r}")
    if report["revision"] < 0:
        _fail("revision", "must not be negative")
    _nonempty_str(report["run_id"], "run_id")
    _check_query(report["query"])
    subject_ids = _check_collection(report["subjects"], "subject", "subjects", _check_subject)
    evidence_ids = _check_collection(
        report["evidence"], "evidence", "evidence",
        lambda record, path: _check_evidence(record, path, subject_ids),
    )
    _check_collection(
        report["analyses"], "analysis", "analyses",
        lambda record, path: _check_analysis(record, path, evidence_ids),
    )
    _check_collection(
        report["stages"], "stage", "stages",
        lambda record, path: _check_stage(record, path, evidence_ids),
    )
    _check_collection(
        report["operations"], "operation", "operations",
        lambda record, path: _check_operation(record, path, report, evidence_ids),
    )
    _check_collection(
        report["qualifications"], "qualification", "qualifications",
        lambda record, path: _check_qualification(record, path, evidence_ids),
    )
    _check_selection(report["selection"], subject_ids)
    required = _required_limitations(report)
    for analysis in report["analyses"]:
        for text in sorted(required.get(analysis["id"], ())):
            if text not in analysis["limitations"]:
                _fail(
                    f"analyses[{analysis['id']}].limitations",
                    f"missing qualification limitation {text!r}",
                )


def add_qualification(report, qualification):
    """Return a deep-copied valid report with `qualification` appended and the
    limitation strings it requires derived into every affected analysis. The
    result stays at the input revision: it is a draft for the caller's
    transaction, and the storage layer advances the revision exactly once when
    that transaction commits, so several qualifications may be added before a
    single transition. The input report and the qualification record are left
    untouched.
    """
    validate_report(report)
    updated = copy.deepcopy(report)
    _check_record_shape(qualification, "qualification", "qualification")
    known = {record["id"] for record in updated["evidence"]}
    _check_qualification(qualification, "qualification", known)
    if qualification["id"] in {record["id"] for record in updated["qualifications"]}:
        _fail("qualification", f"duplicate qualification {qualification['id']!r}")
    updated["qualifications"].append(copy.deepcopy(qualification))
    text = _qualification_text(qualification)
    affected = set(qualification["affected_evidence_ids"])
    for analysis in updated["analyses"]:
        if not affected.isdisjoint(analysis["evidence_ids"]) and text not in analysis["limitations"]:
            analysis["limitations"].append(text)
    validate_report(updated)
    return updated


def _preserved_prefix(previous, current, field):
    if len(current) < len(previous):
        _fail(field, "records were removed across transition")
    for index, record in enumerate(previous):
        if not _json_equal(record, current[index]):
            _fail(f"{field}[{index}]", "was removed, reordered, or modified across transition")


def _preserved_analyses(previous, current, required):
    if len(current) < len(previous):
        _fail("analyses", "records were removed across transition")
    for index, before in enumerate(previous):
        after = current[index]
        if _json_equal(before, after):
            continue
        reshaped = set(before) != set(after) or any(
            name != "limitations" and not _json_equal(before[name], after[name])
            for name in before
        )
        if reshaped:
            _fail(
                f"analyses[{index}]",
                "was modified across transition; only limitations may gain required strings",
            )
        old, new = before["limitations"], after["limitations"]
        if new[: len(old)] != old:
            _fail(f"analyses[{index}].limitations", "lost or reordered entries across transition")
        for text in new[len(old):]:
            if text not in required.get(before["id"], set()):
                _fail(f"analyses[{index}].limitations", f"gained non-required string {text!r}")


def _preserved_stages(previous, current):
    current_names = {stage["name"] for stage in current}
    for before in previous:
        if before["name"] not in current_names:
            _fail("stages", f"stage {before['name']!r} was removed across transition")


def _preserved_operations(previous, current):
    current_by_id = {operation["id"]: operation for operation in current}
    previous_ids = {operation["id"] for operation in previous}
    for before in previous:
        after = current_by_id.get(before["id"])
        if after is None:
            _fail("operations", f"operation {before['id']!r} was removed across transition")
        pair = (before["state"], after["state"])
        if pair not in _ALLOWED_STATE_MOVES:
            _fail(
                f"operations[{before['id']}]",
                f"cannot move from {pair[0]!r} to {pair[1]!r}",
            )
        for field in _IMMUTABLE_OPERATION_FIELDS:
            if not _json_equal(before[field], after[field]):
                _fail(f"operations[{before['id']}].{field}", "changed across transition")
        appended = after["evidence_ids"]
        if appended[: len(before["evidence_ids"])] != before["evidence_ids"]:
            _fail(f"operations[{before['id']}].evidence_ids", "not append-only across transition")
        for key, value in before["audit"].items():
            if key not in after["audit"] or not _json_equal(after["audit"][key], value):
                _fail(f"operations[{before['id']}].audit[{key!r}]", "changed across transition")
    for operation in current:
        if operation["id"] not in previous_ids and operation["state"] != "proposed":
            _fail(
                f"operations[{operation['id']}]",
                f"new operations must begin proposed, got {operation['state']!r}",
            )


def _preserved_selection(previous, current):
    if previous is None:
        return
    if not _json_equal(current, previous):
        _fail("selection", "changed across transition; a new investigation is required")


def validate_transition(previous, current):
    """Raise ReportError unless `current` is the legal revision-1 successor of
    `previous`: same run and schema, exactly one revision forward, immutable
    query and selection choice, preserved historical records, and operations
    moving only through their explicit single-use states.
    """
    validate_report(previous)
    validate_report(current)
    if current["schema_version"] != previous["schema_version"]:
        _fail("schema_version", "changed across transition")
    if current["run_id"] != previous["run_id"]:
        _fail("run_id", f"changed across transition from {previous['run_id']!r}")
    expected = previous["revision"] + 1
    if current["revision"] != expected:
        _fail("revision", f"must advance to {expected}, got {current['revision']}")
    if not _json_equal(current["query"], previous["query"]):
        _fail("query", "changed across transition")
    _preserved_prefix(previous["subjects"], current["subjects"], "subjects")
    _preserved_prefix(previous["evidence"], current["evidence"], "evidence")
    _preserved_analyses(previous["analyses"], current["analyses"], _required_limitations(current))
    _preserved_prefix(previous["qualifications"], current["qualifications"], "qualifications")
    _preserved_stages(previous["stages"], current["stages"])
    _preserved_operations(previous["operations"], current["operations"])
    _preserved_selection(previous["selection"], current["selection"])
