"""Tests for the pure TASK20 correlation adapter mdi.correlate."""

import copy
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.correlate import CorrelationError, correlate_artifact

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def packaged(**over):
    base = {
        "evidence_id": "pkg-1",
        "coordinates": "com.example:app:1.0",
        "realm": "project",
        "module": "app",
        "sha256": SHA_A,
        "digest_complete": True,
        "transformed": False,
        "archive_sha256": SHA_B,
    }
    base.update(over)
    return base


def resolved(**over):
    base = {
        "evidence_id": "res-1",
        "coordinates": "com.example:app:1.0",
        "realm": "project",
        "sha256": SHA_A,
        "digest_complete": True,
        "dirty": False,
        "working_tree_digest": None,
        "revision": "abc123",
        "module": "app",
        "profiles": ["p1"],
        "jdk": "17",
        "maven": "3.9.6",
        "settings_digest": SHA_C,
        "producer": "maven",
        "producer_version": "3.9.6",
        "generation_time": "2026-09-10T00:00:00Z",
    }
    base.update(over)
    return base


def context(**over):
    base = {
        "realm": "project",
        "revision": "abc123",
        "dirty": False,
        "working_tree_digest": None,
        "module": "app",
        "profiles": ["p1"],
        "jdk": "17",
        "maven": "3.9.6",
        "settings_digest": SHA_C,
    }
    base.update(over)
    return base


def build_provenance(**over):
    base = {
        "evidence_id": "bp-1",
        "archive_sha256": SHA_B,
        "realm": "project",
        "revision": "abc123",
        "dirty": False,
        "working_tree_digest": None,
        "module": "app",
        "profiles": ["p1"],
        "jdk": "17",
        "maven": "3.9.6",
        "settings_digest": SHA_C,
        "generation_time": "2026-09-10T00:00:00Z",
    }
    base.update(over)
    return base


class CorrelateTest(unittest.TestCase):
    def call(self, p=None, r=None, c=None, bp="absent"):
        args = [p or packaged(), r or resolved(), c or context()]
        if bp == "absent":
            return correlate_artifact(*args)
        return correlate_artifact(*args, build_provenance=bp)

    def test_resolved_dirty_flag_conflict_blocks_graph(self):
        out = self.call(r=resolved(dirty=True, working_tree_digest=SHA_B))
        self.assertTrue(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("dirty" in s for s in out["conflicts"]))

    def test_resolved_dirty_matching_digest_graph_and_build(self):
        c = context(dirty=True, working_tree_digest=SHA_C)
        out = self.call(
            r=resolved(dirty=True, working_tree_digest=SHA_C),
            bp=build_provenance(dirty=True, working_tree_digest=SHA_C), c=c)
        self.assertEqual(out["kind"], "built_from_checkout")

    def test_resolved_dirty_mismatched_digest_blocks_graph(self):
        c = context(dirty=True, working_tree_digest=SHA_B)
        out = self.call(
            r=resolved(dirty=True, working_tree_digest=SHA_C),
            bp=build_provenance(dirty=True, working_tree_digest=SHA_C), c=c)
        self.assertTrue(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("working_tree_digest" in s for s in out["conflicts"]))

    def test_null_coordinates_blocks_graph_unknown_not_pass(self):
        out = self.call(p=packaged(coordinates=None))
        self.assertTrue(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertEqual(out["kind"], "byte_identity")
        self.assertTrue(any("coordinates" in u for u in out["unknowns"]))
        self.assertFalse(any(
            k["name"] == "coordinates:identity" and k["status"] == "pass"
            for k in out["checks"]))

    def test_coordinate_conflict_blocks_graph_and_built(self):
        out = self.call(r=resolved(coordinates="other:other:1.0"))
        self.assertTrue(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("coordinates" in s for s in out["conflicts"]))

    def test_packaged_realm_mismatch_conflict_blocks_graph(self):
        out = self.call(p=packaged(realm="build-plugin"))
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("realm" in s for s in out["conflicts"]))

    def test_packaged_module_unknown_blocks_graph(self):
        out = self.call(p=packaged(module=None))
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("module" in u for u in out["unknowns"]))

    def test_unknown_generation_time_blocks_graph(self):
        out = self.call(r=resolved(generation_time=None))
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("generation_time" in u for u in out["unknowns"]))

    def test_unknown_bp_generation_time_blocks_built(self):
        out = self.call(bp=build_provenance(generation_time=None))
        self.assertTrue(out["graph_attribution"])
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("generation_time" in u for u in out["unknowns"]))

    def test_rejects_digest_with_incomplete_flag(self):
        with self.assertRaises(CorrelationError):
            self.call(p=packaged(sha256=SHA_A, digest_complete=False))
        with self.assertRaises(CorrelationError):
            self.call(r=resolved(sha256=SHA_A, digest_complete=False))

    def test_rejects_whitespace_only_profiles(self):
        with self.assertRaises(CorrelationError):
            self.call(c=context(profiles=["  "]))

    def test_valid_full_proof(self):
        out = self.call(bp=build_provenance())
        self.assertEqual(out["kind"], "built_from_checkout")
        self.assertTrue(out["byte_identity"])
        self.assertTrue(out["graph_attribution"])
        self.assertTrue(out["built_from_checkout"])
        self.assertEqual(out["unknowns"], [])
        self.assertEqual(out["conflicts"], [])

    def test_same_coordinates_different_content_conflict(self):
        out = self.call(r=resolved(sha256=SHA_B, digest_complete=True))
        self.assertFalse(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("sha256" in c for c in out["conflicts"]))
        self.assertTrue(any(k["status"] == "conflict" for k in out["checks"]))

    def test_same_bytes_unknown_generation_keeps_byte_identity(self):
        r = resolved(
            revision=None, module=None, profiles=None, jdk=None, maven=None,
            settings_digest=None, producer=None, producer_version=None,
            realm=None, coordinates=None,
        )
        out = self.call(p=packaged(coordinates=None), r=r)
        self.assertTrue(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertEqual(out["kind"], "byte_identity")
        self.assertTrue(out["unknowns"])

    def test_dirty_head_only_blocked(self):
        c = context(dirty=True, working_tree_digest=None)
        out = self.call(r=resolved(dirty=True, working_tree_digest=None),
                        bp=build_provenance(), c=c)
        self.assertFalse(out["built_from_checkout"])
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("working_tree_digest" in u for u in out["unknowns"]))

    def test_dirty_matching_digest_proves_build(self):
        c = context(dirty=True, working_tree_digest=SHA_B)
        out = self.call(
            r=resolved(dirty=True, working_tree_digest=SHA_B),
            bp=build_provenance(dirty=True, working_tree_digest=SHA_B), c=c)
        self.assertEqual(out["kind"], "built_from_checkout")

    def test_dirty_mismatched_digest_conflict(self):
        c = context(dirty=True, working_tree_digest=SHA_B)
        out = self.call(
            r=resolved(dirty=True, working_tree_digest=SHA_B),
            bp=build_provenance(dirty=True, working_tree_digest=SHA_C), c=c)
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("working_tree_digest" in s for s in out["conflicts"]))

    def test_dirty_provenance_against_clean_context_conflict(self):
        out = self.call(bp=build_provenance(dirty=True, working_tree_digest=SHA_B))
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(out["conflicts"])

    def test_realm_conflict_blocks_graph_merge(self):
        out = self.call(r=resolved(realm="build-plugin"))
        self.assertTrue(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("realm" in s for s in out["conflicts"]))

    def test_unknown_realm_sentinel_blocks_graph_not_bytes(self):
        out = self.call(r=resolved(realm="unknown"))
        self.assertTrue(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("realm" in u for u in out["unknowns"]))
        self.assertEqual(out["conflicts"], [])

    def test_unknown_provenance_realm_blocks_built_only(self):
        out = self.call(bp=build_provenance(realm="unknown"))
        self.assertTrue(out["graph_attribution"])
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("build_provenance.realm" in u for u in out["unknowns"]))

    def test_unknown_packaged_realm_blocks_graph(self):
        out = self.call(p=packaged(realm="unknown"))
        self.assertTrue(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("packaged.realm" in u for u in out["unknowns"]))

    def test_invalid_realm_rejected_everywhere(self):
        with self.assertRaises(CorrelationError):
            self.call(p=packaged(realm="central"))
        with self.assertRaises(CorrelationError):
            self.call(r=resolved(realm="central"))
        with self.assertRaises(CorrelationError):
            self.call(c=context(realm="central"))
        with self.assertRaises(CorrelationError):
            self.call(bp=build_provenance(realm="central"))

    def test_blank_strings_rejected(self):
        blanks = {
            "r": [resolved(evidence_id="  "), resolved(producer=" "),
                  resolved(producer_version="\t"), resolved(generation_time=" "),
                  resolved(revision="  "), resolved(module=" ")],
            "p": [packaged(evidence_id=" "), packaged(module=" ")],
            "c": [context(revision=" "), context(module=" ")],
            "bp": [build_provenance(evidence_id=" "),
                   build_provenance(generation_time=" ")],
        }
        for kwarg, cases in blanks.items():
            for bad in cases:
                with self.assertRaises(CorrelationError):
                    self.call(**{kwarg: bad})

    def test_module_unknown_is_legitimate_value(self):
        out = self.call(
            p=packaged(module="unknown"),
            r=resolved(module="unknown"),
            c=context(module="unknown"),
            bp=build_provenance(module="unknown"))
        self.assertEqual(out["kind"], "built_from_checkout")
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(out["unknowns"], [])

    def test_different_module_blocks_graph_merge(self):
        out = self.call(r=resolved(module="other"))
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("module" in s for s in out["conflicts"]))

    def test_missing_context_dimension_is_unknown_not_wildcard(self):
        out = self.call(c=context(jdk=None))
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("jdk" in u for u in out["unknowns"]))
        self.assertFalse(out["conflicts"])

    def test_unknown_producer_blocks_graph(self):
        out = self.call(r=resolved(producer=None))
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("producer" in u for u in out["unknowns"]))

    def test_unknown_settings_blocks_graph(self):
        out = self.call(c=context(settings_digest=None))
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("settings_digest" in u for u in out["unknowns"]))

    def test_profiles_mismatch_conflict(self):
        out = self.call(c=context(profiles=["p2"]))
        self.assertFalse(out["graph_attribution"])
        self.assertTrue(any("profiles" in s for s in out["conflicts"]))

    def test_profiles_order_insensitive(self):
        out = self.call(
            r=resolved(profiles=["p2", "p1"]),
            c=context(profiles=["p1", "p2"]), bp=build_provenance(profiles=["p1", "p2"]))
        self.assertEqual(out["kind"], "built_from_checkout")

    def test_same_bytes_no_enclosing_provenance_not_built(self):
        out = self.call()
        self.assertEqual(out["kind"], "graph_attribution")
        self.assertFalse(out["built_from_checkout"])

    def test_enclosing_digest_mismatch_conflict(self):
        bp = build_provenance(archive_sha256=SHA_C)
        out = self.call(bp=bp)
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("archive_sha256" in s for s in out["conflicts"]))

    def test_enclosing_revision_mismatch_conflict(self):
        bp = build_provenance(revision="def456")
        out = self.call(bp=bp)
        self.assertFalse(out["built_from_checkout"])
        self.assertTrue(any("revision" in s for s in out["conflicts"]))

    def test_transformed_no_byte_claim_even_equal_digest(self):
        out = self.call(p=packaged(transformed=True))
        self.assertFalse(out["byte_identity"])
        self.assertFalse(out["graph_attribution"])
        self.assertFalse(out["conflicts"])
        self.assertTrue(any("transformed" in u for u in out["unknowns"]))

    def test_evidence_ids_listed(self):
        out = self.call(bp=build_provenance())
        self.assertEqual(out["evidence_ids"], ["pkg-1", "res-1", "bp-1"])

    # --- validation -------------------------------------------------------

    def test_rejects_partial_digest(self):
        with self.assertRaises(CorrelationError):
            self.call(p=packaged(sha256="a" * 63))

    def test_rejects_uppercase_digest(self):
        with self.assertRaises(CorrelationError):
            self.call(p=packaged(sha256="A" * 64))

    def test_rejects_complete_digest_flag_without_digest(self):
        with self.assertRaises(CorrelationError):
            self.call(p=packaged(sha256=None, digest_complete=True))

    def test_rejects_wrong_types(self):
        for over in (
            {"digest_complete": "yes"},
            {"transformed": 1},
            {"evidence_id": ""},
            {"coordinates": 5},
            {"archive_sha256": 7},
        ):
            with self.assertRaises(CorrelationError):
                self.call(p=packaged(**over))

    def test_rejects_missing_and_extra_keys(self):
        r = resolved()
        del r["producer"]
        with self.assertRaises(CorrelationError):
            self.call(r=r)
        with self.assertRaises(CorrelationError):
            self.call(r={**resolved(), "surprise": 1})

    def test_rejects_bad_profiles(self):
        with self.assertRaises(CorrelationError):
            self.call(c=context(profiles=["p1", "p1"]))
        with self.assertRaises(CorrelationError):
            self.call(c=context(profiles="p1"))

    def test_rejects_bad_context(self):
        with self.assertRaises(CorrelationError):
            self.call(c=context(dirty=None))
        with self.assertRaises(CorrelationError):
            self.call(c=context(working_tree_digest="short"))

    def test_rejects_bad_build_provenance(self):
        with self.assertRaises(CorrelationError):
            self.call(bp=build_provenance(evidence_id=""))
        with self.assertRaises(CorrelationError):
            self.call(bp=build_provenance(generation_time=""))
        with self.assertRaises(CorrelationError):
            self.call(bp={"evidence_id": "x"})

    # --- purity and json --------------------------------------------------

    def test_no_input_mutation_and_json_roundtrip(self):
        p, r, c, bp = packaged(), resolved(), context(), build_provenance()
        frozen = copy.deepcopy((p, r, c, bp))
        out = correlate_artifact(p, r, c, build_provenance=bp)
        self.assertEqual((p, r, c, bp), frozen)
        json.dumps(out)
        self.assertEqual(json.loads(json.dumps(out)), out)


if __name__ == "__main__":
    unittest.main()
