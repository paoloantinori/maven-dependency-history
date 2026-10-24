"""Pure entry-correspondence tests grounded in actual archive inventories."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
import warnings
from unittest import mock
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.archives import inspect_archive
from mdi.shaded import MatchLimits, ShadedError, match_archives


def inventory(entries, *, path="/fixture.jar", complete=True):
    records = []
    for index, (name, data) in enumerate(entries):
        records.append({"archive_id": "a0", "index": index, "name": name,
                        "kind": "file", "size_declared": len(data), "size_actual": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(), "status": "hashed",
                        "compression": 0, "is_archive": False})
    return {"archives": [{"archive_id": "a0", "path": path, "display_path": path,
                           "entry_chain": [], "size": 100, "sha256": "a" * 64,
                           "inspection_status": "complete" if complete else "partial",
                           "identity": {"status": "unknown", "conflict": False,
                                        "candidates": [], "evidence": []}}],
            "entries": records, "issues": [], "coverage": {"status": "inspected"}}


class CorrespondenceTests(unittest.TestCase):
    def test_oversized_text_rejected_before_json_encoder(self):
        sample = inventory([], path="x" * (8 * 1024 * 1024))
        with mock.patch("mdi.shaded.json.JSONEncoder.iterencode", side_effect=AssertionError("encoder reached")):
            with self.assertRaises(ShadedError):
                match_archives(sample, inventory([]), limits=MatchLimits(max_input_bytes=1024))

    def test_deep_or_oversized_unknown_containers_refused_before_encoding(self):
        for value in ([None] * 2048, {"child": []}):
            sample = inventory([])
            sample["extra"] = value
            if isinstance(value, dict):
                value["child"].append(value)
            with mock.patch("mdi.shaded.json.JSONEncoder.iterencode", side_effect=AssertionError("encoder reached")):
                with self.assertRaises(ShadedError):
                    match_archives(sample, inventory([]), limits=MatchLimits(max_input_bytes=1024))

    def test_oversized_metadata_coordinate_refused(self):
        sample = inventory([])
        sample["archives"][0]["identity"]["candidates"] = [
            {"groupId": "g", "artifactId": "a", "version": "v" * 4097}]
        with self.assertRaises(ShadedError):
            match_archives(sample, inventory([]))

    def test_mutable_digest_and_size_fields_are_rejected(self):
        for field in ("sha256", "size_actual", "size_declared"):
            for invalid in ([], {}, True):
                with self.subTest(field=field, invalid=invalid):
                    sample = inventory([("A", b"a")])
                    sample["entries"][0][field] = invalid
                    with self.assertRaises(ShadedError):
                        match_archives(sample, inventory([("A", b"a")]))

    def test_null_incomplete_fields_are_preserved_without_aliasing(self):
        sample = inventory([("A", b"a")])
        sample["entries"][0].update(sha256=None, size_actual=None, status="too-large")
        result = match_archives(sample, sample)
        self.assertIsNone(result["matches"][0]["reference"]["sha256"])
        self.assertIsNone(result["matches"][0]["reference"]["size_actual"])
        self.assertEqual(result["coverage"]["unresolved"], 1)

    def test_reserved_exact_entry_not_counted_as_changed_path_ambiguity(self):
        result = match_archives(inventory([("X", b"same"), ("Y", b"same")]),
                                inventory([("Y", b"same"), ("OLD", b"same")]))
        self.assertEqual(result["matches"][1]["container"]["entry_name"], "X")
        self.assertFalse(result["matches"][1]["pairing_ambiguous"])

    def test_complete_entries_not_whole_artifact_identity(self):
        ref = inventory([("a/A.class", b"a"), ("resource", b"r")])
        container = inventory([("extra", b"e"), ("resource", b"r"), ("a/A.class", b"a")])
        result = match_archives(container, ref)
        self.assertEqual(result["verdict"], "complete-entry-content-correspondence")
        self.assertFalse(result["artifact_byte_identity"])
        self.assertFalse(result["relocation_proven"])
        self.assertEqual(result["coverage"]["exact_matches"], 2)
        self.assertEqual(len(result["surplus"]), 1)
        self.assertEqual(result["coverage"]["exact_fraction"], 1.0)
        json.dumps(result, allow_nan=False)

    def test_reference_duplicate_multiplicity_is_not_collapsed(self):
        result = match_archives(inventory([("A", b"a")]), inventory([("A", b"a"), ("A", b"a")]))
        self.assertEqual(result["verdict"], "partial-entry-content-correspondence")
        self.assertEqual([r["classification"] for r in result["matches"]], ["exact", "missing"])
        self.assertEqual(result["matches"][1]["reference"]["entry_index"], 1)

    def test_container_duplicate_surplus_is_observation(self):
        result = match_archives(inventory([("A", b"a"), ("A", b"a")]), inventory([("A", b"a")]))
        self.assertEqual(result["verdict"], "complete-entry-content-correspondence")
        self.assertEqual(result["coverage"]["conflicts"], 0)
        self.assertEqual(result["surplus"][0]["entry_index"], 1)
        self.assertTrue(result["matches"][0]["pairing_ambiguous"])

    def test_exact_matches_reserved_before_changed_paths(self):
        result = match_archives(inventory([("B", b"same")]), inventory([("A", b"same"), ("B", b"same")]))
        self.assertEqual([r["classification"] for r in result["matches"]], ["missing", "exact"])

    def test_changed_path_is_only_candidate_basis(self):
        result = match_archives(inventory([("new/A.class", b"class")]), inventory([("old/A.class", b"class")]))
        self.assertEqual(result["matches"][0]["classification"], "byte-equal-different-path")
        self.assertEqual(result["verdict"], "candidate-entry-content-correspondence")
        self.assertFalse(result["relocation_proven"])

    def test_changed_path_multiplicity_does_not_reuse_container_entry(self):
        result = match_archives(inventory([("new", b"same")]),
                                inventory([("old1", b"same"), ("old2", b"same")]))
        self.assertEqual(result["coverage"]["changed_path_matches"], 1)
        self.assertEqual(result["coverage"]["missing"], 1)
        self.assertEqual(result["matches"][0]["container"]["entry_index"], 0)
        self.assertIsNone(result["matches"][1]["container"])

    def test_unknown_explicit_archive_refused(self):
        with self.assertRaises(ShadedError):
            match_archives(inventory([]), inventory([]), container_archive_id="absent")

    def test_empty_changed_path_is_not_correspondence(self):
        result = match_archives(inventory([("new", b"")]), inventory([("old", b"")]))
        self.assertEqual(result["matches"][0]["classification"], "missing")
        self.assertEqual(result["coverage"]["changed_path_matches"], 0)

    def test_stale_same_path_bytes_conflict(self):
        result = match_archives(inventory([("A", b"new")]), inventory([("A", b"old")]))
        self.assertEqual(result["matches"][0]["classification"], "path-equal-bytes-differ")
        self.assertEqual(result["coverage"]["conflicts"], 1)
        self.assertEqual(result["verdict"], "unresolved")

    def test_incomplete_reference_digest_not_used(self):
        ref = inventory([("A", b"a")])
        ref["entries"][0]["status"] = "too-large"
        result = match_archives(inventory([("A", b"a")]), ref)
        self.assertEqual(result["matches"][0]["classification"], "unresolved-reference")
        self.assertEqual(result["coverage"]["exact_matches"], 0)

    def test_partial_container_missing_is_unknown_not_absence(self):
        result = match_archives(inventory([], complete=False), inventory([("A", b"a")]))
        self.assertEqual(result["matches"][0]["classification"], "unresolved-container")

    def test_partial_reference_never_full_correspondence(self):
        result = match_archives(inventory([("A", b"a")]), inventory([("A", b"a")], complete=False))
        self.assertEqual(result["verdict"], "partial-entry-content-correspondence")

    def test_size_mismatch_prevents_digest_use(self):
        ref = inventory([("A", b"a")])
        ref["entries"][0]["size_actual"] = 2
        self.assertEqual(match_archives(inventory([("A", b"a")]), ref)["matches"][0]["classification"], "unresolved-reference")

    def test_empty_reference_is_unresolved(self):
        result = match_archives(inventory([]), inventory([]))
        self.assertEqual(result["verdict"], "unresolved")
        self.assertIsNone(result["coverage"]["exact_fraction"])

    def test_metadata_conflict_preserved_without_exact_identity(self):
        ref = inventory([("A", b"a")])
        ref["archives"][0]["identity"] = {"status": "candidate", "conflict": True,
            "candidates": [{"groupId": "g", "artifactId": "a", "version": v,
                            "entry_index": i, "entry_name": f"meta{i}"} for i, v in enumerate(("1", "2"))], "evidence": []}
        result = match_archives(inventory([("A", b"a")]), ref)
        self.assertTrue(result["reference_identity"]["conflict"])
        self.assertEqual(result["reference_identity"]["basis"], "metadata-candidates-only")
        ref["archives"][0]["identity"]["candidates"][0]["version"] = "mutated"
        self.assertEqual(result["reference_identity"]["candidates"][0]["version"], "1")

    def test_nested_inventory_requires_explicit_container_selection(self):
        container = inventory([])
        nested = copy.deepcopy(inventory([("A", b"a")])["archives"][0])
        nested.update(archive_id="a0[3]", entry_chain=[3], display_path="/fixture.jar!lib.jar")
        container["archives"].append(nested)
        entry = inventory([("A", b"a")])["entries"][0]
        entry["archive_id"] = "a0[3]"
        container["entries"].append(entry)
        ref = inventory([("A", b"a")])
        self.assertEqual(match_archives(container, ref)["coverage"]["exact_matches"], 0)
        result = match_archives(container, ref, container_archive_id="a0[3]")
        self.assertEqual(result["matches"][0]["container"]["entry_chain"], [3])
        self.assertEqual(result["coverage"]["exact_matches"], 1)

    def test_duplicate_entry_identity_rejected(self):
        container = inventory([("A", b"a"), ("B", b"b")])
        container["entries"][1]["index"] = 0
        with self.assertRaises(ShadedError):
            match_archives(container, inventory([]))

    def test_bounded_inputs_and_output(self):
        sample = inventory([("A", b"a"), ("B", b"b")])
        with self.assertRaises(ShadedError):
            match_archives(sample, sample, limits=MatchLimits(max_entries=1))
        with self.assertRaises(ShadedError):
            match_archives(sample, sample, limits=MatchLimits(max_output_bytes=10))
        with self.assertRaises(ShadedError):
            match_archives(sample, sample, limits=MatchLimits(max_input_bytes=10))

    def test_invalid_limits_and_non_json_input(self):
        for value in (True, 0, -1, 1.5):
            with self.assertRaises(ValueError):
                MatchLimits(max_entries=value)
        sample = inventory([])
        sample["other"] = float("nan")
        with self.assertRaises(ShadedError):
            match_archives(sample, inventory([]))

    def test_inputs_unmodified_and_result_independent(self):
        source = inventory([("A", b"a")])
        before = copy.deepcopy(source)
        result = match_archives(source, source)
        self.assertEqual(source, before)
        result["container"]["entry_chain"].append(99)
        self.assertEqual(source, before)


class ActualArchiveTests(unittest.TestCase):
    def test_actual_nested_archive_ids_and_chains(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp, "ref.jar")
            outer = Path(tmp, "app.jar")
            with zipfile.ZipFile(ref, "w") as zf:
                zf.writestr("p/A.class", b"class")
            with zipfile.ZipFile(outer, "w") as zf:
                zf.writestr("BOOT-INF/lib/ref.jar", ref.read_bytes())
            source = inspect_archive(outer)
            nested = next(a for a in source["archives"] if a["entry_chain"])
            result = match_archives(source, inspect_archive(ref), container_archive_id=nested["archive_id"])
            self.assertEqual(result["verdict"], "complete-entry-content-correspondence")
            self.assertEqual(result["matches"][0]["container"]["entry_chain"], [0])
            self.assertEqual(result["container"]["display_path"], str(outer) + "!BOOT-INF/lib/ref.jar")

    def test_real_inventory_metadata_and_duplicate_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp, "reference.jar")
            container = Path(tmp, "shaded.jar")
            entries = [("p/A.class", b"fixture-class-bytes"),
                       ("META-INF/maven/g/a/pom.properties", b"groupId=g\nartifactId=a\nversion=1\n")]
            with zipfile.ZipFile(ref, "w") as zf:
                for name, data in entries:
                    zf.writestr(name, data)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(container, "w", zipfile.ZIP_DEFLATED) as zf:
                    for name, data in entries + [entries[0]]:
                        zf.writestr(name, data)
            inspected_ref = inspect_archive(ref)
            inspected_container = inspect_archive(container)
            result = match_archives(inspected_container, inspected_ref)
            self.assertEqual(result["verdict"], "complete-entry-content-correspondence")
            self.assertNotEqual(inspected_ref["archives"][0]["sha256"], inspected_container["archives"][0]["sha256"])
            self.assertEqual(result["reference_identity"]["candidates"][0]["version"], "1")
            self.assertEqual(result["surplus"][0]["entry_index"], 2)


if __name__ == "__main__":
    unittest.main()
