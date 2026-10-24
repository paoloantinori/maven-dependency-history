"""Tests for mdi.report: versioned JSON-safe evidence report contracts."""

import copy
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.report import (
    SCHEMA_VERSION,
    ReportError,
    add_qualification,
    new_report,
    validate_report,
    validate_transition,
)

RUN = "run-2026-09-10"
QUALIFICATION_TEXT = "truncated-capture: capture truncated at limit"
INPUT_SHA256 = "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b"


def input_identity(path="pom.xml"):
    return {"path": path, "sha256": INPUT_SHA256}

TOP_FIELDS = {
    "schema_version",
    "run_id",
    "revision",
    "query",
    "selection",
    "subjects",
    "evidence",
    "analyses",
    "stages",
    "operations",
    "qualifications",
}


def subject(sid="s1", **extra):
    record = {"id": sid, "realm": "project"}
    record.update(extra)
    return record


def evidence(eid="e1", **extra):
    record = {
        "id": eid,
        "category": "pom-bytes",
        "source_locator": "pom.xml#1-10",
        "observed_at": "2026-09-10T00:00:00+00:00",
        "payload": {"sha256": "abc"},
    }
    record.update(extra)
    return record


def analysis(aid="a1", eids=("e1",), **extra):
    record = {
        "id": aid,
        "evidence_ids": list(eids),
        "statement_kind": "observation-summary",
        "limitations": [],
    }
    record.update(extra)
    return record


def stage(name="capture", **extra):
    record = {
        "name": name,
        "outcome": "complete",
        "coverage": {"attempted": 1, "covered": 1},
        "reasons": [],
        "evidence_ids": ["e1"],
    }
    record.update(extra)
    return record


def operation(oid="op1", **extra):
    record = {
        "id": oid,
        "run_id": RUN,
        "state": "proposed",
        "scope": {"paths": ["pom.xml"]},
        "input_identities": [input_identity()],
        "tool_identity": {"name": "mdi-capture", "version": "1.0.0"},
        "disclosure": "local read only, nothing leaves the checkout",
        "audit": {},
        "evidence_ids": ["e1"],
    }
    record.update(extra)
    return record


def qualification(qid="q1", eids=("e1",), **extra):
    record = {
        "id": qid,
        "affected_evidence_ids": list(eids),
        "kind": "truncated-capture",
        "reason": "capture truncated at limit",
        "observed_at": "2026-09-10T00:00:00+00:00",
    }
    record.update(extra)
    return record


def full_report():
    report = new_report(RUN, query="which declared dependencies are unused?")
    report["subjects"].append(subject())
    report["evidence"].append(evidence())
    report["analyses"].append(analysis())
    report["stages"].append(stage())
    report["operations"].append(operation())
    return report


def bump(report):
    """Deep copy one revision forward with no content change."""
    updated = copy.deepcopy(report)
    updated["revision"] += 1
    return updated


def rejects(testcase, mutate, *args):
    testcase.assertRaises(ReportError, mutate, *args)


class NewReportTests(unittest.TestCase):
    def test_defaults_are_complete_and_valid(self):
        report = new_report(RUN)
        self.assertEqual(set(report), TOP_FIELDS)
        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["revision"], 0)
        self.assertEqual(report["run_id"], RUN)
        self.assertIsNone(report["query"])
        self.assertIsNone(report["selection"])
        for field in ("subjects", "evidence", "analyses", "stages", "operations", "qualifications"):
            self.assertEqual(report[field], [])
        validate_report(report)

    def test_string_query_is_preserved(self):
        report = new_report(RUN, query="unused dependencies in app?")
        self.assertEqual(report["query"], "unused dependencies in app?")
        validate_report(report)

    def test_dict_query_is_preserved(self):
        query = {"question": "unused deps", "scope": ["app"], "depth": 2, "safe": True}
        report = new_report(RUN, query=query)
        self.assertEqual(report["query"], query)
        validate_report(report)

    def test_dict_query_is_deep_copied(self):
        query = {"scope": ["app"]}
        report = new_report(RUN, query=query)
        query["scope"].append("lib")
        self.assertEqual(report["query"], {"scope": ["app"]})
        validate_report(report)

    def test_run_id_must_be_nonempty_string(self):
        for bad in ("", None, 5, True, ["run"]):
            with self.subTest(run_id=bad):
                rejects(self, new_report, bad)

    def test_invalid_query_is_rejected(self):
        for bad in (
            5,
            ["scope"],
            "",
            {"scope": {"a"}},
            {"depth": float("nan")},
            {1: "coerced"},
            {"": "empty key"},
        ):
            with self.subTest(query=bad):
                rejects(self, new_report, RUN, bad)


class ValidationShapeTests(unittest.TestCase):
    def test_report_must_be_object(self):
        for bad in (None, [], "report", 5):
            rejects(self, validate_report, bad)

    def test_unknown_top_level_field_rejected(self):
        report = full_report()
        report["executor"] = "run me"
        rejects(self, validate_report, report)

    def test_missing_top_level_field_rejected(self):
        report = full_report()
        del report["stages"]
        rejects(self, validate_report, report)

    def test_bool_version_and_revision_rejected(self):
        report = full_report()
        report["schema_version"] = True
        rejects(self, validate_report, report)
        report = full_report()
        report["revision"] = False
        rejects(self, validate_report, report)

    def test_nonint_and_negative_revision_rejected(self):
        for bad in ("0", 1.0, -1):
            with self.subTest(revision=bad):
                report = full_report()
                report["revision"] = bad
                rejects(self, validate_report, report)

    def test_unsupported_schema_version_rejected(self):
        report = full_report()
        report["schema_version"] = 2
        rejects(self, validate_report, report)

    def test_nonfinite_numbers_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=bad):
                report = full_report()
                report["evidence"][0]["payload"]["ratio"] = bad
                rejects(self, validate_report, report)

    def test_non_json_values_rejected_without_coercion(self):
        for bad in ({1, 2}, (1, 2), datetime(2026, 9, 10), b"bytes"):
            with self.subTest(value=bad):
                report = full_report()
                report["evidence"][0]["payload"]["blob"] = bad
                rejects(self, validate_report, report)

    def test_non_string_object_keys_rejected(self):
        report = full_report()
        report["evidence"][0]["payload"] = {1: "silently coerced by json"}
        rejects(self, validate_report, report)

    def test_collections_must_be_lists(self):
        for field in ("subjects", "evidence", "analyses", "stages", "operations", "qualifications"):
            with self.subTest(field=field):
                report = full_report()
                report[field] = {}
                rejects(self, validate_report, report)


class SubjectTests(unittest.TestCase):
    def test_all_realms_accepted(self):
        for realm in ("project", "build-plugin", "unknown"):
            with self.subTest(realm=realm):
                report = full_report()
                report["subjects"][0]["realm"] = realm
                validate_report(report)

    def test_unknown_realm_rejected(self):
        report = full_report()
        report["subjects"][0]["realm"] = "module"
        rejects(self, validate_report, report)

    def test_duplicate_subject_id_rejected(self):
        report = full_report()
        report["subjects"].append(subject())
        rejects(self, validate_report, report)

    def test_empty_subject_id_rejected(self):
        report = full_report()
        report["subjects"][0]["id"] = ""
        rejects(self, validate_report, report)

    def test_unknown_subject_field_rejected(self):
        report = full_report()
        report["subjects"][0]["version"] = "1.0"
        rejects(self, validate_report, report)

    def test_optional_fields_accepted(self):
        report = full_report()
        report["subjects"][0].update(
            {
                "module": "org.example:app",
                "coordinates": "org.example:lib:jar:1.2.3",
                "owning_plugin": "org.apache.maven.plugins:maven-shade-plugin",
                "build_context": {"profile": "release"},
                "packaged_copy": {"path": "BOOT-INF/lib/lib-1.2.3.jar"},
            }
        )
        validate_report(report)

    def test_string_fields_must_be_strings_and_bundles_objects(self):
        report = full_report()
        report["subjects"][0]["module"] = 5
        rejects(self, validate_report, report)
        report = full_report()
        report["subjects"][0]["build_context"] = "release"
        rejects(self, validate_report, report)


class EvidenceTests(unittest.TestCase):
    def test_duplicate_evidence_id_rejected(self):
        report = full_report()
        report["evidence"].append(evidence())
        rejects(self, validate_report, report)

    def test_dangling_subject_reference_rejected(self):
        report = full_report()
        report["evidence"][0]["subject_id"] = "ghost"
        rejects(self, validate_report, report)

    def test_subject_reference_resolves_when_present(self):
        report = full_report()
        report["evidence"][0]["subject_id"] = "s1"
        validate_report(report)

    def test_payload_must_be_object(self):
        for bad in (["not", "object"], "payload", None, 5):
            with self.subTest(payload=bad):
                report = full_report()
                report["evidence"][0]["payload"] = bad
                rejects(self, validate_report, report)

    def test_empty_required_strings_rejected(self):
        for field in ("id", "category", "source_locator", "observed_at"):
            with self.subTest(field=field):
                report = full_report()
                report["evidence"][0][field] = ""
                rejects(self, validate_report, report)

    def test_unknown_evidence_field_rejected(self):
        report = full_report()
        report["evidence"][0]["digest"] = "abc"
        rejects(self, validate_report, report)

    def test_optional_fields_accepted(self):
        report = full_report()
        report["evidence"][0].update(
            {
                "subject_id": "s1",
                "captured_identity": {"sha256": "abc", "size": 10},
                "producer": "mdi.capture",
                "producer_version": "1.0.0",
                "generation_context": {"tool": "maven"},
            }
        )
        validate_report(report)


class AnalysisTests(unittest.TestCase):
    def test_duplicate_analysis_id_rejected(self):
        report = full_report()
        report["analyses"].append(analysis())
        rejects(self, validate_report, report)

    def test_dangling_evidence_reference_rejected(self):
        report = full_report()
        report["analyses"][0]["evidence_ids"] = ["missing"]
        rejects(self, validate_report, report)

    def test_analyses_must_cite_evidence(self):
        report = full_report()
        report["analyses"][0]["evidence_ids"] = []
        rejects(self, validate_report, report)

    def test_unknown_statement_kind_rejected(self):
        report = full_report()
        report["analyses"][0]["statement_kind"] = "guess"
        rejects(self, validate_report, report)

    def test_inference_requires_limitations(self):
        report = full_report()
        report["analyses"][0]["statement_kind"] = "inference"
        rejects(self, validate_report, report)
        report["analyses"][0]["limitations"] = ["only the root pom was inspected"]
        validate_report(report)

    def test_summary_allows_zero_limitations(self):
        report = full_report()
        report["analyses"][0]["limitations"] = []
        validate_report(report)

    def test_limitations_must_be_nonempty_strings(self):
        for bad in ([""], [5], "text", [{"why": "x"}]):
            with self.subTest(limitations=bad):
                report = full_report()
                report["analyses"][0]["limitations"] = bad
                rejects(self, validate_report, report)

    def test_statement_must_be_string(self):
        report = full_report()
        report["analyses"][0]["statement"] = 5
        rejects(self, validate_report, report)
        report = full_report()
        report["analyses"][0]["statement"] = "lib is declared but unused"
        validate_report(report)

    def test_unknown_analysis_field_rejected(self):
        report = full_report()
        report["analyses"][0]["confidence"] = 0.9
        rejects(self, validate_report, report)

    def test_evidence_references_never_cross_reports(self):
        other = new_report("run-other")
        other["evidence"].append(evidence("shared-e1"))
        validate_report(other)
        mine = full_report()
        mine["evidence"][0]["id"] = "e2"
        mine["analyses"][0]["evidence_ids"] = ["shared-e1"]
        rejects(self, validate_report, mine)

class StageTests(unittest.TestCase):
    def test_duplicate_stage_name_rejected(self):
        report = full_report()
        report["stages"].append(stage())
        rejects(self, validate_report, report)

    def test_valid_outcomes_accepted(self):
        for outcome, reasons in (
            ("complete", []),
            ("not_applicable", []),
            ("partial", ["one pom skipped"]),
            ("unavailable", ["checkout missing"]),
            ("denied", ["consent refused"]),
            ("failed", ["parser died"]),
        ):
            with self.subTest(outcome=outcome):
                report = full_report()
                report["stages"][0]["outcome"] = outcome
                report["stages"][0]["reasons"] = reasons
                validate_report(report)

    def test_unknown_outcome_rejected(self):
        report = full_report()
        report["stages"][0]["outcome"] = "skipped"
        rejects(self, validate_report, report)

    def test_noncomplete_reasonable_outcomes_require_reasons(self):
        for outcome in ("partial", "unavailable", "denied", "failed"):
            with self.subTest(outcome=outcome):
                report = full_report()
                report["stages"][0]["outcome"] = outcome
                report["stages"][0]["reasons"] = []
                rejects(self, validate_report, report)

    def test_coverage_must_be_object(self):
        report = full_report()
        report["stages"][0]["coverage"] = [1, 1]
        rejects(self, validate_report, report)

    def test_dangling_evidence_reference_rejected(self):
        report = full_report()
        report["stages"][0]["evidence_ids"] = ["missing"]
        rejects(self, validate_report, report)


class OperationTests(unittest.TestCase):
    def test_duplicate_operation_id_rejected(self):
        report = full_report()
        report["operations"].append(operation())
        rejects(self, validate_report, report)

    def test_run_id_must_match_report(self):
        report = full_report()
        report["operations"][0]["run_id"] = "run-other"
        rejects(self, validate_report, report)

    def test_unknown_state_rejected(self):
        report = full_report()
        report["operations"][0]["state"] = "archived"
        rejects(self, validate_report, report)

    def test_object_fields_must_be_objects_and_lists_lists(self):
        for field, bad in (
            ("scope", ["pom.xml"]),
            ("tool_identity", "mdi"),
            ("audit", []),
            ("input_identities", {"sha256:abc"}),
        ):
            with self.subTest(field=field):
                report = full_report()
                report["operations"][0][field] = bad
                rejects(self, validate_report, report)

    def test_disclosure_must_be_nonempty_string(self):
        report = full_report()
        report["operations"][0]["disclosure"] = ""
        rejects(self, validate_report, report)

    def test_dangling_evidence_reference_rejected(self):
        report = full_report()
        report["operations"][0]["evidence_ids"] = ["missing"]
        rejects(self, validate_report, report)

    def test_input_identities_are_captured_file_identities(self):
        for bad in (
            ["sha256:abc"],
            [{}],
            [{"path": "pom.xml"}],
            [{"sha256": INPUT_SHA256}],
            [{"path": "", "sha256": INPUT_SHA256}],
            [{"path": "pom.xml", "sha256": "abc"}],
            [{"path": "pom.xml", "sha256": INPUT_SHA256.upper()}],
            [{"path": "pom.xml", "sha256": INPUT_SHA256, "size": 10}],
            [input_identity(), "scalar"],
            [{"path": "pom.xml", "sha256": None}],
        ):
            with self.subTest(identity=bad):
                report = full_report()
                report["operations"][0]["input_identities"] = bad
                rejects(self, validate_report, report)

    def test_empty_input_identities_allowed_for_non_local_operations(self):
        report = full_report()
        report["operations"][0]["input_identities"] = []
        validate_report(report)


class QualificationTests(unittest.TestCase):
    def test_valid_qualification_with_inherited_limitation(self):
        report = full_report()
        report["qualifications"].append(qualification())
        report["analyses"][0]["limitations"] = [QUALIFICATION_TEXT]
        validate_report(report)

    def test_missing_inherited_limitation_rejected(self):
        report = full_report()
        report["qualifications"].append(qualification())
        rejects(self, validate_report, report)

    def test_affected_evidence_ids_must_be_nonempty_and_resolve(self):
        report = full_report()
        bad = qualification(eids=[])
        bad["affected_evidence_ids"] = []
        report["qualifications"].append(bad)
        rejects(self, validate_report, report)
        report = full_report()
        report["qualifications"].append(qualification(eids=("missing",)))
        rejects(self, validate_report, report)

    def test_duplicate_qualification_id_rejected(self):
        report = add_qualification(full_report(), qualification())
        report["qualifications"].append(qualification(qid="q1"))
        rejects(self, validate_report, report)

    def test_unknown_qualification_field_rejected(self):
        report = full_report()
        record = qualification(severity="high")
        report["qualifications"].append(record)
        report["analyses"][0]["limitations"] = [QUALIFICATION_TEXT]
        rejects(self, validate_report, report)

    def test_qualification_without_analyses_needs_no_limitations(self):
        report = full_report()
        report["evidence"].append(evidence("e2", payload={"sha256": "def"}))
        report["qualifications"].append(qualification(eids=("e2",)))
        validate_report(report)
        self.assertEqual(report["analyses"][0]["limitations"], [])


class SelectionTests(unittest.TestCase):
    def test_pending_selection_allowed(self):
        report = full_report()
        validate_report(report)

    def test_single_selection_rules(self):
        report = full_report()
        report["selection"] = {"mode": "single", "candidate_ids": ["s1"]}
        validate_report(report)
        report["subjects"].append(subject("s2", realm="unknown"))
        report["selection"]["candidate_ids"] = ["s1", "s2"]
        rejects(self, validate_report, report)
        report["selection"]["candidate_ids"] = []
        rejects(self, validate_report, report)

    def test_all_selection_accepted(self):
        report = full_report()
        report["subjects"].append(subject("s2", realm="unknown"))
        report["selection"] = {"mode": "all", "candidate_ids": ["s1", "s2"]}
        validate_report(report)

    def test_unknown_candidate_rejected(self):
        report = full_report()
        report["selection"] = {"mode": "all", "candidate_ids": ["ghost"]}
        rejects(self, validate_report, report)

    def test_invalid_mode_and_shape_rejected(self):
        report = full_report()
        report["selection"] = {"mode": "some", "candidate_ids": ["s1"]}
        rejects(self, validate_report, report)
        report = full_report()
        report["selection"] = {"mode": "single", "candidate_ids": ["s1"], "reason": "why not"}
        rejects(self, validate_report, report)
        report = full_report()
        report["selection"] = ["s1"]
        rejects(self, validate_report, report)
        report = full_report()
        report["selection"] = {"mode": "single"}
        rejects(self, validate_report, report)

    def test_candidates_must_be_nonempty_and_unique(self):
        report = full_report()
        report["subjects"].append(subject("s2", realm="unknown"))
        for bad in (
            {"mode": "all", "candidate_ids": []},
            {"mode": "all", "candidate_ids": ["s1", "s1"]},
            {"mode": "single", "candidate_ids": ["s1", "s1"]},
        ):
            with self.subTest(selection=bad):
                report["selection"] = bad
                rejects(self, validate_report, report)

    def test_all_mode_is_explicit_candidates_not_every_subject(self):
        report = full_report()
        report["subjects"].append(subject("s2", realm="unknown"))
        report["selection"] = {"mode": "all", "candidate_ids": ["s1"]}
        validate_report(report)


class AddQualificationTests(unittest.TestCase):
    def test_appends_qualification_and_derives_limitation(self):
        original = full_report()
        snapshot = copy.deepcopy(original)
        updated = add_qualification(original, qualification())
        self.assertEqual(original, snapshot)
        self.assertEqual(updated["revision"], original["revision"])
        self.assertEqual(len(original["qualifications"]), 0)
        self.assertEqual(original["analyses"][0]["limitations"], [])
        self.assertEqual(updated["qualifications"], [qualification()])
        self.assertEqual(updated["analyses"][0]["limitations"], [QUALIFICATION_TEXT])
        validate_report(updated)

    def test_returns_independent_copy(self):
        original = full_report()
        record = qualification()
        updated = add_qualification(original, record)
        record["kind"] = "mutated-after-call"
        updated["analyses"][0]["limitations"].append("late addition")
        self.assertEqual(updated["qualifications"][0]["kind"], "truncated-capture")
        self.assertEqual(original["analyses"][0]["limitations"], [])
        self.assertEqual(original["qualifications"], [])

    def test_derived_string_is_not_duplicated(self):
        original = full_report()
        original["analyses"][0]["limitations"] = [QUALIFICATION_TEXT]
        updated = add_qualification(original, qualification())
        self.assertEqual(updated["analyses"][0]["limitations"], [QUALIFICATION_TEXT])

    def test_unaffected_analyses_untouched(self):
        original = full_report()
        original["evidence"].append(evidence("e2", payload={"sha256": "def"}))
        original["analyses"].append(analysis("a2", ("e2",)))
        updated = add_qualification(original, qualification(eids=("e2",)))
        self.assertEqual(updated["analyses"][0]["limitations"], [])
        self.assertEqual(updated["analyses"][1]["limitations"], [QUALIFICATION_TEXT])

    def test_dangling_references_rejected(self):
        original = full_report()
        rejects(self, add_qualification, original, qualification(eids=("missing",)))

    def test_invalid_input_report_rejected(self):
        original = full_report()
        original["evidence"][0]["payload"] = "not an object"
        rejects(self, add_qualification, original, qualification())

    def test_invalid_qualification_record_rejected(self):
        original = full_report()
        rejects(self, add_qualification, original, {"id": "q1"})
        rejects(self, add_qualification, original, "q1")


class TransitionTests(unittest.TestCase):
    def test_identical_successor_is_valid(self):
        previous = full_report()
        validate_transition(previous, bump(previous))

    def test_both_inputs_must_be_valid_reports(self):
        previous = full_report()
        previous["revision"] = True
        rejects(self, validate_transition, previous, bump(full_report()))
        rejects(self, validate_transition, full_report(), {})

    def test_run_and_schema_are_immutable(self):
        previous = full_report()
        current = bump(previous)
        current["run_id"] = "run-other"
        rejects(self, validate_transition, previous, current)
        current = bump(previous)
        current["schema_version"] = 2
        rejects(self, validate_transition, previous, current)

    def test_revision_must_advance_by_exactly_one(self):
        previous = full_report()
        current = bump(previous)
        current["revision"] = previous["revision"] + 2
        rejects(self, validate_transition, previous, current)
        current = bump(previous)
        current["revision"] = previous["revision"]
        rejects(self, validate_transition, previous, current)

    def test_query_is_immutable(self):
        previous = full_report()
        current = bump(previous)
        current["query"] = "a different question"
        rejects(self, validate_transition, previous, current)

    def test_subjects_are_append_only(self):
        previous = full_report()
        current = bump(previous)
        current["subjects"].append(subject("s2", realm="unknown"))
        validate_transition(previous, current)
        modified = bump(previous)
        modified["subjects"][0]["realm"] = "unknown"
        rejects(self, validate_transition, previous, modified)
        removed = bump(previous)
        removed["subjects"] = []
        rejects(self, validate_transition, previous, removed)
        reordered = bump(previous)
        reordered["subjects"].append(subject("s2", realm="unknown"))
        reordered["subjects"].reverse()
        rejects(self, validate_transition, previous, reordered)

    def test_evidence_is_append_only(self):
        previous = full_report()
        current = bump(previous)
        current["evidence"].append(evidence("e2", payload={"sha256": "def"}))
        current["analyses"].append(analysis("a2", ("e2",)))
        validate_transition(previous, current)
        rewritten = bump(previous)
        rewritten["evidence"][0]["payload"] = {"sha256": "changed"}
        rejects(self, validate_transition, previous, rewritten)
        dropped = bump(previous)
        dropped["evidence"] = []
        rejects(self, validate_transition, previous, dropped)

    def test_analyses_only_gain_required_limitations(self):
        previous = full_report()
        qualified = bump(previous)
        qualified["qualifications"].append(qualification())
        qualified["analyses"][0]["limitations"] = [QUALIFICATION_TEXT]
        validate_transition(previous, qualified)
        unsupported = bump(previous)
        unsupported["analyses"][0]["limitations"] = ["unsupported: free text"]
        rejects(self, validate_transition, previous, unsupported)
        changed = bump(previous)
        changed["analyses"][0]["statement"] = "a new claim"
        rejects(self, validate_transition, previous, changed)
        removed = bump(previous)
        removed["analyses"] = []
        rejects(self, validate_transition, previous, removed)

    def test_qualifications_are_append_only(self):
        previous = add_qualification(full_report(), qualification())
        appended = bump(previous)
        appended["evidence"].append(evidence("e2", payload={"sha256": "def"}))
        appended["qualifications"].append(qualification(qid="q2", eids=("e2",)))
        validate_transition(previous, appended)
        rewritten = bump(previous)
        rewritten["qualifications"][0]["reason"] = "changed"
        rejects(self, validate_transition, previous, rewritten)
        dropped = bump(previous)
        dropped["qualifications"] = []
        rejects(self, validate_transition, previous, dropped)

    def test_stages_update_by_name(self):
        previous = full_report()
        current = bump(previous)
        current["stages"][0]["outcome"] = "partial"
        current["stages"][0]["reasons"] = ["one pom skipped"]
        validate_transition(previous, current)
        current["stages"].append(stage("collect", outcome="not_applicable"))
        validate_transition(previous, current)
        dropped = bump(previous)
        dropped["stages"] = []
        rejects(self, validate_transition, previous, dropped)

    def test_operation_progresses_proposed_consumed_finished(self):
        previous = full_report()
        consumed = bump(previous)
        consumed["operations"][0]["state"] = "consumed"
        consumed["operations"][0]["audit"]["at"] = "2026-09-10T01:00:00+00:00"
        validate_transition(previous, consumed)
        finished = bump(consumed)
        finished["operations"][0]["state"] = "finished"
        finished["operations"][0]["audit"]["closed"] = "2026-09-10T02:00:00+00:00"
        validate_transition(consumed, finished)

    def test_operation_state_moves_are_single_use(self):
        moves = [
            ("proposed", "denied", True),
            ("proposed", "consumed", True),
            ("proposed", "finished", False),
            ("denied", "consumed", False),
            ("denied", "proposed", False),
            ("consumed", "proposed", False),
            ("consumed", "denied", False),
            ("consumed", "finished", True),
            ("finished", "consumed", False),
        ]
        for before_state, after_state, allowed in moves:
            with self.subTest(move=f"{before_state}->{after_state}"):
                previous = full_report()
                previous["operations"][0]["state"] = before_state
                current = bump(previous)
                current["operations"][0]["state"] = after_state
                if allowed:
                    validate_transition(previous, current)
                else:
                    rejects(self, validate_transition, previous, current)

    def test_operation_identity_fields_are_immutable(self):
        for field, value in (
            ("scope", {"paths": ["elsewhere"]}),
            ("input_identities", ["sha256:other"]),
            ("tool_identity", {"name": "other-tool"}),
            ("disclosure", "now it leaks"),
        ):
            with self.subTest(field=field):
                previous = full_report()
                current = bump(previous)
                current["operations"][0][field] = value
                rejects(self, validate_transition, previous, current)

    def test_operation_references_and_audit_are_append_only(self):
        previous = full_report()
        previous["evidence"].append(evidence("e2", payload={"sha256": "def"}))
        previous["operations"][0]["evidence_ids"] = ["e1", "e2"]
        previous["operations"][0]["audit"]["at"] = "2026-09-10T00:00:00+00:00"
        grown = bump(previous)
        grown["operations"][0]["audit"]["closed"] = "2026-09-10T01:00:00+00:00"
        validate_transition(previous, grown)
        shrunk = bump(previous)
        shrunk["operations"][0]["evidence_ids"] = ["e1"]
        rejects(self, validate_transition, previous, shrunk)
        altered = bump(previous)
        altered["operations"][0]["audit"]["at"] = "2026-09-10T09:00:00+00:00"
        rejects(self, validate_transition, previous, altered)
        removed_operation = bump(previous)
        removed_operation["operations"] = []
        rejects(self, validate_transition, previous, removed_operation)

    def test_new_operations_must_begin_proposed(self):
        previous = full_report()
        current = bump(previous)
        current["operations"].append(operation("op2"))
        validate_transition(previous, current)
        for state in ("denied", "consumed", "finished"):
            with self.subTest(state=state):
                current = bump(previous)
                current["operations"].append(operation("op2", state=state))
                rejects(self, validate_transition, previous, current)

    def test_new_analyses_must_cite_evidence(self):
        previous = full_report()
        current = bump(previous)
        current["analyses"].append(analysis("a2", eids=[]))
        rejects(self, validate_transition, previous, current)
        grounded = bump(previous)
        grounded["evidence"].append(evidence("e2", payload={"sha256": "def"}))
        grounded["analyses"].append(analysis("a2", ("e2",)))
        validate_transition(previous, grounded)


class JsonTypeFidelityTests(unittest.TestCase):
    """Immutable comparisons must distinguish JSON types: True != 1, 1 != 1.0,
    0.0 != -0.0, while object key order stays insignificant. Input identities,
    disclosure, analysis fields and qualification fields hold only validated
    strings, so no type-confused rewrite of them survives validation."""

    def test_query_type_confusion_rejected(self):
        previous = full_report()
        previous["query"] = {"depth": 1}
        for changed in (True, 1.0):
            with self.subTest(value=changed):
                current = bump(previous)
                current["query"] = {"depth": changed}
                rejects(self, validate_transition, previous, current)

    def test_query_signed_zero_change_rejected(self):
        previous = full_report()
        previous["query"] = {"ratio": 0.0}
        current = bump(previous)
        current["query"] = {"ratio": -0.0}
        rejects(self, validate_transition, previous, current)

    def test_subject_context_type_confusion_rejected(self):
        previous = full_report()
        previous["subjects"][0]["build_context"] = {"depth": 1}
        current = bump(previous)
        current["subjects"][0]["build_context"] = {"depth": 1.0}
        rejects(self, validate_transition, previous, current)

    def test_evidence_payload_type_confusion_rejected(self):
        previous = full_report()
        previous["evidence"][0]["payload"] = {"size": 1}
        current = bump(previous)
        current["evidence"][0]["payload"] = {"size": True}
        rejects(self, validate_transition, previous, current)

    def test_operation_scope_and_tool_type_confusion_rejected(self):
        previous = full_report()
        previous["operations"][0]["scope"] = {"depth": 1}
        current = bump(previous)
        current["operations"][0]["scope"] = {"depth": True}
        rejects(self, validate_transition, previous, current)
        previous = full_report()
        previous["operations"][0]["tool_identity"] = {"version": 1}
        current = bump(previous)
        current["operations"][0]["tool_identity"] = {"version": 1.0}
        rejects(self, validate_transition, previous, current)

    def test_audit_value_type_confusion_rejected(self):
        previous = full_report()
        previous["operations"][0]["audit"] = {"count": 1, "ratio": 0.0}
        for audit in (
            {"count": True, "ratio": 0.0},
            {"count": 1.0, "ratio": 0.0},
            {"count": 1, "ratio": -0.0},
        ):
            with self.subTest(audit=audit):
                current = bump(previous)
                current["operations"][0]["audit"] = audit
                rejects(self, validate_transition, previous, current)

    def test_object_key_order_is_insignificant(self):
        previous = full_report()
        previous["query"] = {"a": 1, "b": 2}
        current = bump(previous)
        current["query"] = {"b": 2, "a": 1}
        validate_transition(previous, current)

    def test_unchanged_data_remains_allowed(self):
        previous = full_report()
        previous["query"] = {"depth": 1, "ratio": 0.0}
        previous["evidence"][0]["payload"] = {"size": 1}
        previous["operations"][0]["audit"] = {"count": 1}
        validate_transition(previous, bump(previous))


class SelectionTransitionTests(unittest.TestCase):
    def test_pending_may_become_choice(self):
        previous = full_report()
        current = bump(previous)
        current["selection"] = {"mode": "single", "candidate_ids": ["s1"]}
        validate_transition(previous, current)

    def test_chosen_subject_cannot_change(self):
        previous = full_report()
        previous["subjects"].append(subject("s2", realm="unknown"))
        previous["selection"] = {"mode": "single", "candidate_ids": ["s1"]}
        changed = bump(previous)
        changed["selection"] = {"mode": "single", "candidate_ids": ["s2"]}
        rejects(self, validate_transition, previous, changed)

    def test_selection_is_frozen_once_set(self):
        base = full_report()
        base["subjects"].append(subject("s2", realm="unknown"))
        base["selection"] = {"mode": "all", "candidate_ids": ["s1", "s2"]}
        validate_transition(base, bump(base))
        for changed in (
            {"mode": "single", "candidate_ids": ["s1"]},
            {"mode": "all", "candidate_ids": ["s1"]},
        ):
            with self.subTest(selection=changed):
                current = bump(base)
                current["selection"] = changed
                rejects(self, validate_transition, base, current)
        single = full_report()
        single["subjects"].append(subject("s2", realm="unknown"))
        single["selection"] = {"mode": "single", "candidate_ids": ["s1"]}
        widened = bump(single)
        widened["selection"] = {"mode": "all", "candidate_ids": ["s1", "s2"]}
        rejects(self, validate_transition, single, widened)

    def test_choice_cannot_be_cleared(self):
        previous = full_report()
        previous["selection"] = {"mode": "single", "candidate_ids": ["s1"]}
        cleared = bump(previous)
        cleared["selection"] = None
        rejects(self, validate_transition, previous, cleared)


if __name__ == "__main__":
    unittest.main()
