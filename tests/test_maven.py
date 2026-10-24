"""Tests for mdi.maven offline imports of recorded Maven producer reports.

Fixtures are faithful extracts of the authorized 2026-09-10 probe outputs
pinned in docs/verification/maven-investigator-spike.md: dependency-plugin
3.11.0 JSON trees (string-valued optional field, gson 2.14.0 winning over a
transitive 2.10.1 that only verbose output shows), help-plugin 3.5.2 verbose
effective-POM origin comments of the form 'g:a:v, line N', and a
':'-separated build-classpath with no trailing newline. No test reads /tmp
or any session path; everything is inline.
"""

import hashlib
import json
import socket
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.maven import (
    MavenImportError,
    import_classpath,
    import_dependency_tree,
    import_effective_pom,
)


def node(group_id, artifact_id, version, scope="compile", optional="false", children=None, **extra):
    record = {
        "groupId": group_id,
        "artifactId": artifact_id,
        "version": version,
        "type": "jar",
        "scope": scope,
        "classifier": "",
        "optional": optional,
    }
    if children is not None:
        record["children"] = children
    record.update(extra)
    return record


GSON_WINNER = node(
    "com.google.code.gson", "gson", "2.14.0",
    children=[node("com.google.errorprone", "error_prone_annotations", "2.48.0")],
)
GSON_LOSER = node("com.google.code.gson", "gson", "2.10.1")
CONVERTER_CHILDREN = [
    node("com.squareup.retrofit2", "retrofit", "2.11.0",
         children=[node("com.squareup.okhttp3", "okhttp", "3.14.9",
                        children=[node("com.squareup.okio", "okio", "1.17.2")])]),
]

TREE_NONVERBOSE = node("com.example.probe", "conflict-fixture", "1.0.0", scope="",
                       children=[GSON_WINNER,
                                 node("com.squareup.retrofit2", "converter-gson", "2.11.0",
                                      children=list(CONVERTER_CHILDREN))])
TREE_VERBOSE = node("com.example.probe", "conflict-fixture", "1.0.0", scope="",
                    children=[GSON_WINNER,
                              node("com.squareup.retrofit2", "converter-gson", "2.11.0",
                                   children=CONVERTER_CHILDREN + [GSON_LOSER])])


def tree_provenance(**overrides):
    base = {
        "producer": "maven-dependency-plugin",
        "version": "3.11.0",
        "format": "dependency-tree-json",
        "generated_at": "2026-09-10T14:03:11+00:00",
        "revision": "9f2c1ab",
        "module": "conflict-fixture",
        "profiles": [],
        "jdk": "17.0.12",
        "maven": "Apache Maven 3.9.12",
        "settings_digest": "a" * 64,
        "dirty": False,
        "working_tree_digest": "b" * 64,
    }
    base.update(overrides)
    return base


def current_context(**overrides):
    base = {
        "revision": "9f2c1ab",
        "dirty": False,
        "working_tree_digest": "b" * 64,
        "module": "conflict-fixture",
        "profiles": [],
        "jdk": "17.0.12",
        "maven": "Apache Maven 3.9.12",
        "settings_digest": "a" * 64,
    }
    base.update(overrides)
    return base


def pom_provenance(**overrides):
    base = tree_provenance(
        producer="maven-help-plugin", version="3.5.2", format="effective-pom-xml",
        module="conflict-fixture",
    )
    base.update(overrides)
    return base


# Faithful shape of the observed verbose effective POM: origin comments name
# the source coordinates and line, on fields at every depth (dependency
# fields, managed/BOM fields, plugin dependency fields, profile fields);
# scope fields and container elements carry none in the observed output.
EFFECTIVE_POM = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
    '  <modelVersion>4.0.0</modelVersion>  <!-- com.example.probe:conflict-fixture:1.0.0, line 5 -->\n'
    '  <groupId>com.example.probe</groupId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 7 -->\n'
    '  <artifactId>conflict-fixture</artifactId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 8 -->\n'
    '  <version>1.0.0</version>  <!-- com.example.probe:conflict-fixture:1.0.0, line 9 -->\n'
    '  <dependencies>\n'
    '    <dependency>\n'
    '      <groupId>com.google.code.gson</groupId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 18 -->\n'
    '      <artifactId>gson</artifactId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 19 -->\n'
    '      <version>2.14.0</version>  <!-- com.example.probe:conflict-fixture:1.0.0, line 20 -->\n'
    '      <scope>compile</scope>\n'
    '    </dependency>\n'
    '  </dependencies>\n'
    '  <dependencyManagement>\n'
    '    <dependencies>\n'
    '      <dependency>\n'
    '        <groupId>junit</groupId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 31 -->\n'
    '        <artifactId>junit</artifactId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 32 -->\n'
    '        <version>4.13.2</version>  <!-- com.example.probe:conflict-fixture:1.0.0, line 33 -->\n'
    '      </dependency>\n'
    '      <dependency>\n'
    '        <groupId>org.junit</groupId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 35 -->\n'
    '        <artifactId>junit-bom</artifactId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 36 -->\n'
    '        <version>5.10.0</version>  <!-- com.example.probe:conflict-fixture:1.0.0, line 37 -->\n'
    '        <scope>import</scope>  <!-- com.example.probe:conflict-fixture:1.0.0, line 38 -->\n'
    '        <type>pom</type>\n'
    '      </dependency>\n'
    '    </dependencies>\n'
    '  </dependencyManagement>\n'
    '  <build>\n'
    '    <plugins>\n'
    '      <plugin>\n'
    '        <groupId>org.apache.maven.plugins</groupId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 45 -->\n'
    '        <artifactId>maven-dependency-plugin</artifactId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 46 -->\n'
    '        <version>3.11.0</version>  <!-- com.example.probe:conflict-fixture:1.0.0, line 47 -->\n'
    '        <dependencies>\n'
    '          <dependency>\n'
    '            <groupId>org.apache.maven.doxia</groupId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 50 -->\n'
    '            <artifactId>doxia-core</artifactId>\n'
    '            <version>1.12.0</version>\n'
    '          </dependency>\n'
    '        </dependencies>\n'
    '      </plugin>\n'
    '    </plugins>\n'
    '  </build>\n'
    '  <profiles>\n'
    '    <profile>\n'
    '      <id>it</id>\n'
    '      <dependencies>\n'
    '        <dependency>\n'
    '          <groupId>org.hamcrest</groupId>  <!-- com.example.probe:conflict-fixture:1.0.0, line 60 -->\n'
    '          <artifactId>hamcrest</artifactId>\n'
    '          <version>2.2</version>\n'
    '          <flag xmlns="urn:other">x</flag>\n'
    '          <!-- org.hamcrest:hamcrest:2.2, line 62 -->\n'
    '        </dependency>\n'
    '      </dependencies>\n'
    '    </profile>\n'
    '  </profiles>\n'
    '</project>\n'
).encode("utf-8")


def import_error_code(callable_, *args, **kwargs):
    try:
        callable_(*args, **kwargs)
    except MavenImportError as exc:
        return exc.code
    raise AssertionError("MavenImportError was not raised")


class TreeImportTest(unittest.TestCase):
    def test_nonverbose_graph_paths_and_raw_optional(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        result = import_dependency_tree(data, provenance=tree_provenance())
        self.assertEqual(result["kind"], "dependency-tree")
        self.assertEqual(result["input"]["bytes"], len(data))
        self.assertEqual(result["input"]["sha256"], hashlib.sha256(data).hexdigest())
        datetime.fromisoformat(result["imported_at"])  # real observation timestamp
        nodes = {record["id"]: record for record in result["evidence"]["nodes"]}
        self.assertEqual(len(nodes), 7)
        root = nodes["com.example.probe:conflict-fixture:1.0.0"]
        self.assertEqual(root["scope"], "")  # observed empty root scope preserved
        self.assertEqual(root["optional"], {"raw": "false", "parsed": False})
        okio = nodes["com.example.probe:conflict-fixture:1.0.0"
                     "/1:com.squareup.retrofit2:converter-gson:2.11.0"
                     "/0:com.squareup.retrofit2:retrofit:2.11.0"
                     "/0:com.squareup.okhttp3:okhttp:3.14.9"
                     "/0:com.squareup.okio:okio:1.17.2"]
        self.assertEqual(okio["depth"], 4)
        self.assertEqual(okio["path"][-1], "0:com.squareup.okio:okio:1.17.2")
        self.assertEqual(result["evidence"]["root"], "com.example.probe:conflict-fixture:1.0.0")
        self.assertEqual(result["mode"], "nonverbose")
        self.assertEqual(result["inclusion_authority"], "recorded-context")

    def test_nonverbose_inclusion_qualifier_and_never_resolved(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        result = import_dependency_tree(data, provenance=tree_provenance())
        for record in result["evidence"]["nodes"]:
            self.assertEqual(record["inclusion"]["status"], "included-per-report")
            self.assertIn("recorded", record["inclusion"]["qualifier"])
        self.assertEqual(result["coverage"]["resolved_inclusion"], "not-computed")
        self.assertEqual(result["coverage"]["conflict_mediation"], "not-recorded-by-producer")

    def test_verbose_duplicate_versions_kept_as_separate_paths_all_unknown(self):
        data = json.dumps(TREE_VERBOSE).encode("utf-8")
        result = import_dependency_tree(data, provenance=tree_provenance(), verbose=True)
        self.assertEqual(result["mode"], "verbose")
        self.assertEqual(result["inclusion_authority"], "diagnostic-only")
        nodes = {record["id"]: record for record in result["evidence"]["nodes"]}
        self.assertEqual(len(nodes), 8)
        loser_id = ("com.example.probe:conflict-fixture:1.0.0"
                    "/1:com.squareup.retrofit2:converter-gson:2.11.0"
                    "/1:com.google.code.gson:gson:2.10.1")
        self.assertIn(loser_id, nodes)  # second path to gson retained, not merged
        self.assertEqual(nodes[loser_id]["coordinates"]["version"], "2.10.1")
        for record in result["evidence"]["nodes"]:
            self.assertEqual(record["inclusion"]["status"], "unknown")
        duplicates = result["coverage"]["duplicate_coordinates"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["coordinates"], "com.google.code.gson:gson")
        self.assertEqual(sorted(duplicates[0]["versions"]), ["2.10.1", "2.14.0"])
        self.assertEqual(result["coverage"]["resolved_inclusion"], "not-computed")

    def test_optional_accepts_bool_and_true_string(self):
        base = dict(TREE_NONVERBOSE)
        base["optional"] = True
        result = import_dependency_tree(json.dumps(base).encode("utf-8"),
                                        provenance=tree_provenance())
        root = result["evidence"]["nodes"][0]
        self.assertEqual(root["optional"], {"raw": True, "parsed": True})
        base["optional"] = "true"
        result = import_dependency_tree(json.dumps(base).encode("utf-8"),
                                        provenance=tree_provenance())
        self.assertEqual(result["evidence"]["nodes"][0]["optional"],
                         {"raw": "true", "parsed": True})

    def test_context_matching_mismatch_and_unknown(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        result = import_dependency_tree(data, provenance=tree_provenance(),
                                        current_context=current_context())
        self.assertEqual(result["context"]["currency"], "matching")
        self.assertEqual(result["context"]["fields"]["revision"]["status"], "match")
        result = import_dependency_tree(data, provenance=tree_provenance(),
                                        current_context=current_context(revision="0000000"))
        self.assertEqual(result["context"]["currency"], "mismatch")
        self.assertEqual(result["context"]["fields"]["revision"]["status"], "mismatch")
        self.assertTrue(any("recorded context" in note for note in result["context"]["notes"]))
        result = import_dependency_tree(data, provenance=tree_provenance())
        self.assertEqual(result["context"]["currency"], "unknown")
        self.assertTrue(any("authoritative only" in note for note in result["context"]["notes"]))

    def test_partial_when_provenance_field_unknown_but_current_known(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        result = import_dependency_tree(
            data, provenance=tree_provenance(dirty=None, revision=None),
            current_context=current_context())
        self.assertEqual(result["context"]["currency"], "partial")
        self.assertEqual(result["context"]["fields"]["dirty"]["status"], "provenance-unknown")

    def test_provenance_contract(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        bad = tree_provenance()
        del bad["dirty"]
        self.assertEqual(import_error_code(import_dependency_tree, data, provenance=bad),
                         "provenance-missing-keys")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(dirty="nope")),
                         "provenance-shape")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(profiles="it")),
                         "provenance-shape")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(extra="x")),
                         "provenance-unknown-keys")

    def test_producer_pinned(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(producer="other-plugin")),
                         "unsupported-producer")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(producer=None)),
                         "unsupported-producer")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(version="3.7.0")),
                         "unsupported-producer-version")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(version=None)),
                         "unsupported-producer-version")

    def test_verbose_flag_must_be_explicit_bool(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        for bad in (1, "yes", None):
            with self.assertRaises(TypeError):
                import_dependency_tree(data, provenance=tree_provenance(), verbose=bad)

    def test_hostile_json(self):
        good = tree_provenance()
        dup = b'{"groupId":"a","groupId":"b","artifactId":"x","version":"1",\
"type":"jar","scope":"","classifier":"","optional":"false"}'
        self.assertEqual(import_error_code(import_dependency_tree, dup, provenance=good),
                         "duplicate-json-key")
        nan = b'{"groupId":"a","artifactId":"x","version":"1","type":"jar","scope":"",\
"classifier":"","optional":NaN}'
        self.assertEqual(import_error_code(import_dependency_tree, nan, provenance=good),
                         "non-finite-number")
        huge = b'{"groupId":"a","artifactId":"x","version":"1","type":"jar","scope":"",\
"classifier":"","optional":"false","score":1e999}'
        self.assertEqual(import_error_code(import_dependency_tree, huge, provenance=good),
                         "non-finite-number")
        self.assertEqual(import_error_code(import_dependency_tree, b"\xff\xfe{}",
                                           provenance=good), "invalid-utf8")
        deep = b"[" * 100000 + b"]" * 100000
        self.assertEqual(import_error_code(import_dependency_tree, deep, provenance=good),
                         "json-depth-exceeded")
        self.assertEqual(import_error_code(import_dependency_tree, b"not json",
                                           provenance=good), "malformed-json")

    def test_malformed_node_grammar(self):
        good = tree_provenance()
        missing = {k: v for k, v in TREE_NONVERBOSE.items() if k != "artifactId"}
        self.assertEqual(import_error_code(import_dependency_tree,
                                           json.dumps(missing).encode(), provenance=good),
                         "malformed-tree-node")
        wrong_type = dict(TREE_NONVERBOSE, optional=5)
        self.assertEqual(import_error_code(import_dependency_tree,
                                           json.dumps(wrong_type).encode(), provenance=good),
                         "malformed-tree-node")
        wrong_children = dict(TREE_NONVERBOSE, children="many")
        self.assertEqual(import_error_code(import_dependency_tree,
                                           json.dumps(wrong_children).encode(), provenance=good),
                         "malformed-tree-node")
        self.assertEqual(import_error_code(import_dependency_tree, b"[1,2]",
                                           provenance=good), "malformed-tree-node")

    def test_node_depth_and_input_caps(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(), max_nodes=3),
                         "tree-node-limit-exceeded")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(), max_depth=2),
                         "tree-depth-exceeded")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(), max_input_bytes=10),
                         "input-bytes-exceeded")
        for label, kwargs in (("max_nodes", {"max_nodes": 0}), ("max_input_bytes", {"max_input_bytes": 0})):
            with self.assertRaises(ValueError):
                import_dependency_tree(data, provenance=tree_provenance(), **kwargs)
        with self.assertRaises(TypeError):
            import_dependency_tree(data, provenance=tree_provenance(), max_depth="2")
        with self.assertRaises(ValueError):
            import_dependency_tree(data, provenance=tree_provenance(), max_depth=-1)

    def test_foreign_node_fields_preserved_never_interpreted(self):
        tree = json.loads(json.dumps(TREE_VERBOSE))
        tree["children"][1]["children"][1]["omitted"] = "duplicated"
        result = import_dependency_tree(json.dumps(tree).encode("utf-8"),
                                        provenance=tree_provenance(), verbose=True)
        loser = [record for record in result["evidence"]["nodes"]
                 if record["coordinates"]["version"] == "2.10.1"][0]
        self.assertEqual(loser["foreign_fields"], {"omitted": "duplicated"})
        self.assertEqual(loser["inclusion"]["status"], "unknown")  # still unknown, not 'omitted'
        self.assertTrue(any("extra" in note for note in result["coverage"]["limitations"]))

    def test_realm_explicit_only(self):
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        result = import_dependency_tree(data, provenance=tree_provenance())
        self.assertEqual(result["subject_realm"], "unknown")  # never inferred
        result = import_dependency_tree(data, provenance=tree_provenance(), realm="build-plugin")
        self.assertEqual(result["subject_realm"], "build-plugin")
        self.assertEqual(import_error_code(import_dependency_tree, data,
                                           provenance=tree_provenance(), realm="plugin"),
                         "bad-realm")

    def test_result_is_json_safe_and_touches_no_io(self):
        data = json.dumps(TREE_VERBOSE).encode("utf-8")
        with mock.patch("builtins.open") as fake_open, mock.patch("socket.socket") as fake_socket:
            result = import_dependency_tree(data, provenance=tree_provenance(), verbose=True,
                                            realm="project")
        fake_open.assert_not_called()
        fake_socket.assert_not_called()
        self.assertEqual(json.loads(json.dumps(result)), result)


class EffectivePomImportTest(unittest.TestCase):
    def test_direct_field_origins_known_and_absent(self):
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance(),
                                      pom_locator="/checkouts/probe/pom.xml")
        self.assertEqual(result["kind"], "effective-pom")
        origins = {record["xml_path"]: record for record in result["evidence"]["origins"]}
        self.assertEqual(origins["project/modelVersion"]["origin"],
                         {"coordinates": "com.example.probe:conflict-fixture:1.0.0", "line": 5})
        self.assertEqual(origins["project/groupId"]["origin"]["line"], 7)
        self.assertEqual(origins["project/artifactId"]["origin"]["line"], 8)
        self.assertEqual(origins["project/version"]["origin"]["line"], 9)
        self.assertEqual(origins["project/modelVersion"]["grammar"], "known")
        # the dependencies container carries no trailing comment in the
        # observed output: absent, never invented
        self.assertIsNone(origins["project/dependencies"]["origin"])
        self.assertIsNone(origins["project/dependencies"]["trailing_comment"])

    def test_deep_field_origins_exact_locators(self):
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance())
        origins = {record["xml_path"]: record for record in result["evidence"]["origins"]}
        coords = "com.example.probe:conflict-fixture:1.0.0"
        self.assertEqual(origins["project/dependencies/dependency/groupId"]["origin"],
                         {"coordinates": coords, "line": 18})
        self.assertEqual(origins["project/dependencies/dependency/artifactId"]["origin"],
                         {"coordinates": coords, "line": 19})
        self.assertEqual(origins["project/dependencies/dependency/version"]["origin"],
                         {"coordinates": coords, "line": 20})
        self.assertIsNone(origins["project/dependencies/dependency/scope"]["origin"])
        # dependencyManagement fields, including the second (BOM) dependency
        # under its positional locator
        self.assertEqual(origins["project/dependencyManagement/dependencies/dependency/groupId"]
                         ["origin"]["line"], 31)
        self.assertEqual(origins["project/dependencyManagement/dependencies/dependency[2]/artifactId"]
                         ["origin"]["line"], 36)
        self.assertEqual(origins["project/dependencyManagement/dependencies/dependency[2]/scope"]
                         ["origin"]["line"], 38)
        # build plugin fields and plugin dependency fields
        self.assertEqual(origins["project/build/plugins/plugin/artifactId"]["origin"]["line"], 46)
        self.assertEqual(origins["project/build/plugins/plugin/dependencies/dependency/groupId"]
                         ["origin"]["line"], 50)
        # profile-gated dependency fields keep the profile locator convention
        profile_field = "project/profiles/profile[@id='it']/dependencies/dependency/groupId"
        self.assertEqual(origins[profile_field]["origin"]["line"], 60)

    def test_bom_import_is_management_not_inclusion(self):
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance())
        boms = [d for d in result["evidence"]["declarations"] if d["kind"] == "bom-import"]
        self.assertEqual(len(boms), 1)
        self.assertEqual(boms[0]["coordinates"]["artifactId"], "junit-bom")
        self.assertTrue(boms[0]["management_only"])

    def test_origin_locators_share_declaration_conventions(self):
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance())
        declaration_paths = {d["source"]["xml_path"] for d in result["evidence"]["declarations"]}
        origin_paths = {record["xml_path"] for record in result["evidence"]["origins"]}
        self.assertTrue(declaration_paths <= origin_paths,
                        f"declarations without origin records: {sorted(declaration_paths - origin_paths)}")

    def test_nested_foreign_element_breaks_comment_adjacency(self):
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance())
        origins = {record["xml_path"]: record for record in result["evidence"]["origins"]}
        version = origins["project/profiles/profile[@id='it']/dependencies/dependency/version"]
        self.assertIsNone(version["trailing_comment"])  # comment follows the foreign element, not the field
        self.assertIn(" org.hamcrest:hamcrest:2.2, line 62 ",
                      result["evidence"]["unattached_comment_texts"])

    def test_origin_walk_depth_and_node_budget(self):
        self.assertEqual(import_error_code(import_effective_pom, EFFECTIVE_POM,
                                           provenance=pom_provenance(), max_depth=2),
                         "xml-depth-exceeded")
        self.assertEqual(import_error_code(import_effective_pom, EFFECTIVE_POM,
                                           provenance=pom_provenance(), max_xml_nodes=10),
                         "xml-node-limit-exceeded")
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance())
        counts = result["coverage"]["counts"]
        self.assertGreater(counts["xml_elements"], 30)
        self.assertGreater(counts["xml_comments"], 15)  # comments share the node budget

    def test_unknown_grammar_comment_preserved_raw(self):
        doc = EFFECTIVE_POM.replace(
            b"  <dependencies>\n",
            b"  <packaging>jar</packaging>  <!-- injected by build magic -->\n  <dependencies>\n",
        )
        result = import_effective_pom(doc, provenance=pom_provenance())
        origins = {record["xml_path"]: record for record in result["evidence"]["origins"]}
        packaging = origins["project/packaging"]
        self.assertEqual(packaging["grammar"], "unknown")
        self.assertIsNone(packaging["origin"])
        self.assertEqual(packaging["trailing_comment"]["raw"], " injected by build magic ")

    def test_comment_never_attached_across_foreign_element(self):
        doc = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "<groupId>g</groupId>"
            '<foreign xmlns="urn:other">x</foreign>'
            "<!-- g:a:1, line 7 -->"
            "</project>"
        ).encode("utf-8")
        result = import_effective_pom(doc, provenance=pom_provenance())
        origins = {record["xml_path"]: record for record in result["evidence"]["origins"]}
        self.assertIsNone(origins["project/groupId"]["trailing_comment"])
        self.assertIn(" g:a:1, line 7 ", result["evidence"]["unattached_comment_texts"])

    def test_declarations_preserve_contexts_and_management_is_not_inclusion(self):
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance(),
                                      pom_locator="/checkouts/probe/pom.xml")
        declarations = result["evidence"]["declarations"]
        gson = [d for d in declarations if d["coordinates"]["artifactId"] == "gson"][0]
        self.assertEqual(gson["kind"], "dependency")
        self.assertFalse(gson["management_only"])
        self.assertEqual(gson["source"]["pom"], "/checkouts/probe/pom.xml")
        junit = [d for d in declarations if d["coordinates"]["artifactId"] == "junit"][0]
        self.assertEqual(junit["kind"], "managed-dependency")
        self.assertTrue(junit["management_only"])
        hamcrest = [d for d in declarations if d["coordinates"]["artifactId"] == "hamcrest"][0]
        self.assertEqual(hamcrest["profile"], {"id": "it", "source": "project/profiles/profile[@id='it']"})
        self.assertEqual(hamcrest["activation"], "unknown")
        self.assertEqual(result["coverage"]["inclusion"], "not-claimed")
        self.assertTrue(any("management" in note for note in result["coverage"]["limitations"]))

    def test_parent_metadata_preserved(self):
        doc = EFFECTIVE_POM.replace(
            b"  <modelVersion>",
            b"  <parent><groupId>org.example</groupId><artifactId>parent</artifactId>"
            b"<version>3.1</version></parent>\n  <modelVersion>",
        )
        result = import_effective_pom(doc, provenance=pom_provenance())
        parent = result["evidence"]["project"]["parent"]
        self.assertEqual(parent["groupId"], "org.example")
        self.assertEqual(parent["artifactId"], "parent")
        self.assertEqual(parent["version"], "3.1")

    def test_nested_aggregator_rejected_concatenated_is_malformed(self):
        single = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>m</artifactId></project>'
        ).encode("utf-8")
        # Deliberate expected-code change (compliance): concatenated
        # multi-document output is indistinguishable from generic trailing
        # junk by any byte heuristic, so it reports malformed-xml; only a
        # document that actually parses to nested projects is classified
        # unsupported. Nothing is ever first-selected.
        concatenated = single + b"\n" + single
        self.assertEqual(import_error_code(import_effective_pom, concatenated,
                                           provenance=pom_provenance()),
                         "malformed-xml")
        nested = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            '<project xmlns="http://maven.apache.org/POM/4.0.0"/></project>'
        ).encode("utf-8")
        self.assertEqual(import_error_code(import_effective_pom, nested,
                                           provenance=pom_provenance()),
                         "unsupported-aggregator-multi-project")

    def test_deep_foreign_subtree_rejected_before_extraction(self):
        # F1: the origin walk skips foreign-namespace subtrees, but
        # itertext/_extract_project still traverse them; only a structural
        # preflight over every node can enforce the depth bound.
        foreign_deep = '<x xmlns="urn:x">' + "<f>" * 200 + "</f>" * 200 + "</x>"
        under_group = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "<groupId>g</groupId>" + foreign_deep + "</project>"
        ).encode("utf-8")
        self.assertEqual(import_error_code(import_effective_pom, under_group,
                                           provenance=pom_provenance()),
                         "xml-depth-exceeded")
        in_profile_id = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><profiles><profile>'
            "<id>it" + foreign_deep + "</id>"
            "</profile></profiles></project>"
        ).encode("utf-8")
        self.assertEqual(import_error_code(import_effective_pom, in_profile_id,
                                           provenance=pom_provenance()),
                         "xml-depth-exceeded")

    def test_broad_foreign_subtree_hits_node_cap(self):
        foreign_wide = '<x xmlns="urn:x">' + "<f/>" * 20 + "</x>"
        doc = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "<groupId>g</groupId>" + foreign_wide + "</project>"
        ).encode("utf-8")
        self.assertEqual(import_error_code(import_effective_pom, doc,
                                           provenance=pom_provenance(), max_xml_nodes=10),
                         "xml-node-limit-exceeded")

    def test_root_counted_in_structural_budget_exact_boundary(self):
        # F3: the root itself is part of the node budget: project + a = 2.
        tiny = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><a>1</a></project>'
        ).encode("utf-8")
        result = import_effective_pom(tiny, provenance=pom_provenance(), max_xml_nodes=2)
        self.assertEqual(result["coverage"]["counts"]["xml_elements"], 2)
        self.assertEqual(result["coverage"]["counts"]["xml_nodes"], 2)
        self.assertEqual(import_error_code(import_effective_pom, tiny,
                                           provenance=pom_provenance(), max_xml_nodes=1),
                         "xml-node-limit-exceeded")

    def test_context_snapshot_not_shared_with_caller(self):
        # F2: the result must embed a deep copy of current_context, never a
        # reference the caller can mutate after the fact.
        data = json.dumps(TREE_NONVERBOSE).encode("utf-8")
        ctx = current_context()
        result = import_dependency_tree(data, provenance=tree_provenance(),
                                        current_context=ctx)
        ctx["revision"] = "mutated"
        ctx["profiles"].append("evil")
        self.assertEqual(result["context"]["current_context"]["revision"], "9f2c1ab")
        self.assertEqual(result["context"]["current_context"]["profiles"], [])
        self.assertEqual(result["context"]["fields"]["revision"]["current"], "9f2c1ab")
        self.assertEqual(result["context"]["currency"], "matching")

    def test_pi_breaks_comment_adjacency_nested_and_recovers(self):
        doc = (
            '<?xml version="1.0"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            '  <groupId>g</groupId>\n'
            '  <?audit flag?>\n'
            '  <!-- g:a:1, line 7 -->\n'
            '  <artifactId>a</artifactId>  <!-- g:a:1, line 8 -->\n'
            '  <dependencies>\n'
            '    <dependency>\n'
            '      <groupId>dep</groupId>\n'
            '      <?pi inner?>\n'
            '      <!-- dep:a:2, line 12 -->\n'
            '      <artifactId>d</artifactId>  <!-- dep:a:2, line 13 -->\n'
            '    </dependency>\n'
            '  </dependencies>\n'
            '</project>\n'
        ).encode("utf-8")
        result = import_effective_pom(doc, provenance=pom_provenance())
        origins = {record["xml_path"]: record for record in result["evidence"]["origins"]}
        # the comment after the PI must not attach to the preceding field
        self.assertIsNone(origins["project/groupId"]["trailing_comment"])
        self.assertIn(" g:a:1, line 7 ", result["evidence"]["unattached_comment_texts"])
        # a properly adjacent comment still attaches
        self.assertEqual(origins["project/artifactId"]["origin"]["line"], 8)
        dep = "project/dependencies/dependency/"
        self.assertIsNone(origins[dep + "groupId"]["trailing_comment"])
        self.assertIn(" dep:a:2, line 12 ", result["evidence"]["unattached_comment_texts"])
        self.assertEqual(origins[dep + "artifactId"]["origin"]["line"], 13)
        # declaration locators stay on the discover convention despite PIs
        self.assertEqual([d["source"]["xml_path"] for d in result["evidence"]["declarations"]],
                         ["project/dependencies/dependency"])

    def test_hostile_and_nonproject_xml(self):
        doctype = b'<!DOCTYPE project [<!ENTITY x "x">]><project/>'
        self.assertEqual(import_error_code(import_effective_pom, doctype,
                                           provenance=pom_provenance()), "malformed-xml")
        self.assertEqual(import_error_code(import_effective_pom, b"<notpom/>",
                                           provenance=pom_provenance()), "not-a-project-root")
        broken = b"<project><dependencies></project>"
        self.assertEqual(import_error_code(import_effective_pom, broken,
                                           provenance=pom_provenance()), "malformed-xml")

    def test_effective_pom_producer_pinned_and_locator_is_label_only(self):
        self.assertEqual(import_error_code(import_effective_pom, EFFECTIVE_POM,
                                           provenance=tree_provenance()),
                         "unsupported-producer")
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance())
        self.assertIsNone(result["evidence"]["declarations"][0]["source"]["pom"])
        with self.assertRaises(ValueError):
            import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance(), pom_locator="")

    def test_effective_pom_node_cap_and_no_io(self):
        with mock.patch("builtins.open") as fake_open, mock.patch("socket.socket") as fake_socket:
            result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance(),
                                          realm="project")
        fake_open.assert_not_called()
        fake_socket.assert_not_called()
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(import_error_code(import_effective_pom, EFFECTIVE_POM,
                                           provenance=pom_provenance(), max_xml_nodes=5),
                         "xml-node-limit-exceeded")

    def test_context_shared_contract(self):
        result = import_effective_pom(EFFECTIVE_POM, provenance=pom_provenance(
            revision=None, dirty=None), current_context=current_context())
        self.assertEqual(result["context"]["currency"], "partial")


class ClasspathImportTest(unittest.TestCase):
    def test_posix_paths_verbatim_no_existence(self):
        data = (b"/repo/com/google/code/gson/gson/2.14.0/gson-2.14.0.jar"
                b":/repo/com/squareup/okio/okio/1.17.2/okio-1.17.2.jar")
        result = import_classpath(data, provenance=tree_provenance(format="classpath-mvn"))
        self.assertEqual(result["kind"], "build-classpath")
        self.assertEqual(result["evidence"]["paths"], [
            "/repo/com/google/code/gson/gson/2.14.0/gson-2.14.0.jar",
            "/repo/com/squareup/okio/okio/1.17.2/okio-1.17.2.jar"])
        self.assertEqual(result["evidence"]["separator"], ":")
        self.assertEqual(result["evidence"]["path_count"], 2)
        self.assertFalse(result["coverage"]["existence_checked"])
        self.assertNotIn("coordinates", json.dumps(result["evidence"]))

    def test_windows_separator_explicit(self):
        data = b"C:\\libs\\a.jar;C:\\libs\\b.jar"
        result = import_classpath(data, provenance=tree_provenance(), separator=";")
        self.assertEqual(result["evidence"]["paths"], ["C:\\libs\\a.jar", "C:\\libs\\b.jar"])
        self.assertEqual(result["evidence"]["separator"], ";")
        self.assertEqual(result["evidence"]["platform_pathsep"], ":")  # POSIX host, declared sep recorded

    def test_empty_is_valid_and_trailing_separator_honest(self):
        result = import_classpath(b"", provenance=tree_provenance())
        self.assertEqual(result["evidence"]["paths"], [])
        result = import_classpath(b"a.jar:", provenance=tree_provenance())
        self.assertEqual(result["evidence"]["paths"], ["a.jar", ""])

    def test_nul_and_utf8_and_limits(self):
        self.assertEqual(import_error_code(import_classpath, b"a\x00b",
                                           provenance=tree_provenance()),
                         "classpath-nul-byte")
        self.assertEqual(import_error_code(import_classpath, b"\xff\xfe.jar",
                                           provenance=tree_provenance()), "invalid-utf8")
        self.assertEqual(import_error_code(import_classpath, b"a:b:c",
                                           provenance=tree_provenance(), max_paths=2),
                         "classpath-path-limit-exceeded")
        self.assertEqual(import_error_code(import_classpath, b"a:b:c",
                                           provenance=tree_provenance(), max_input_bytes=2),
                         "input-bytes-exceeded")

    def test_separator_validation(self):
        for bad in ("", "::", "\x00"):
            self.assertEqual(import_error_code(import_classpath, b"a",
                                               provenance=tree_provenance(), separator=bad),
                             "bad-separator")
        with self.assertRaises(TypeError):
            import_classpath(b"a", provenance=tree_provenance(), separator=5)

    def test_classpath_producer_pinned_context_and_no_io(self):
        self.assertEqual(import_error_code(import_classpath, b"a",
                                           provenance=pom_provenance()),
                         "unsupported-producer")
        with mock.patch("builtins.open") as fake_open, mock.patch("socket.socket") as fake_socket:
            result = import_classpath(b"/x/a.jar", provenance=tree_provenance(),
                                      current_context=current_context(revision="9f2c1ab"))
        fake_open.assert_not_called()
        fake_socket.assert_not_called()
        self.assertEqual(result["context"]["currency"], "matching")
        self.assertEqual(json.loads(json.dumps(result)), result)


if __name__ == "__main__":
    unittest.main()
