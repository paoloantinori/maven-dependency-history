"""Tests for mdi.discover bounded declaration discovery over a checkout."""

import contextlib
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.discover import capture_at, discover_declarations, open_checkout_fd, open_module_dir

NS = "http://maven.apache.org/POM/4.0.0"


def pom(body, ns=True):
    opener = f'<project xmlns="{NS}">' if ns else "<project>"
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{opener}\n{body}\n</project>\n'


def coords(group_id, artifact_id, version, **extra):
    base = {"groupId": group_id, "artifactId": artifact_id, "version": version,
            "scope": None, "type": None, "classifier": None, "optional": None}
    base.update(extra)
    return base


class DiscoverTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "checkout"
        self.repo.mkdir()
        self.outside = Path(tmp.name) / "outside"
        self.outside.mkdir()

    def write_pom(self, relpath, body, ns=True):
        path = self.repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pom(body, ns), encoding="utf-8")
        return path

    def discover(self, **kwargs):
        return discover_declarations(self.repo, **kwargs)

    def codes(self, result):
        return [issue["code"] for issue in result["issues"]]

    def by_module(self, result, module_id):
        return [d for d in result["declarations"] if d["module"] == module_id]

    def kinds(self, decls):
        return [d["kind"] for d in decls]


class ResultShapeTest(DiscoverTestCase):
    def test_shape_and_json_round_trip(self):
        self.write_pom("pom.xml", """
  <groupId>com.example</groupId><artifactId>root</artifactId><version>1</version>
""")
        result = self.discover()
        self.assertEqual(sorted(result), ["coverage", "declarations", "issues", "modules"])
        round_trip = json.loads(json.dumps(result))
        self.assertEqual(round_trip, result)

    def test_coverage_is_declarations_only(self):
        self.write_pom("pom.xml", """
  <groupId>com.example</groupId><artifactId>root</artifactId><version>1</version>
""")
        coverage = self.discover()["coverage"]
        self.assertEqual(coverage["scope"], "declarations-only")
        self.assertEqual(coverage["resolved_graph"], "not-computed")
        self.assertEqual(coverage["packaged_content"], "not-inspected")
        self.assertEqual(coverage["limits"], {"max_modules": 128, "max_depth": 16,
                                              "xml_bytes": 8 * 1024 * 1024})
        self.assertTrue(coverage["limitations"])
        for stamp in ("started_at", "finished_at"):
            when = datetime.fromisoformat(coverage[stamp])
            self.assertIsNotNone(when.tzinfo)

    def test_parameter_validation(self):
        for bad in (0, -1, True, "8"):
            with self.assertRaises((TypeError, ValueError), msg=repr(bad)):
                self.discover(max_modules=bad)
            with self.assertRaises((TypeError, ValueError), msg=repr(bad)):
                self.discover(xml_bytes=bad)
        for bad in (-1, True, "8"):
            with self.assertRaises((TypeError, ValueError), msg=repr(bad)):
                self.discover(max_depth=bad)


class TraversalTest(DiscoverTestCase):
    def test_root_and_child_modules(self):
        self.write_pom("pom.xml", """
  <groupId>com.example</groupId><artifactId>root</artifactId><version>1</version>
  <modules><module>app</module><module>lib</module></modules>
""")
        self.write_pom("app/pom.xml", """
  <groupId>com.example</groupId><artifactId>app</artifactId><version>1</version>
  <dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId>
    <version>4.13.2</version><scope>test</scope></dependency></dependencies>
""")
        self.write_pom("lib/pom.xml", """
  <groupId>com.example</groupId><artifactId>lib</artifactId><version>1</version>
""")
        result = self.discover()
        self.assertEqual([m["id"] for m in result["modules"]], [".", "app", "lib"])
        self.assertEqual([m["depth"] for m in result["modules"]], [0, 1, 1])
        self.assertEqual([m["declared_by"] for m in result["modules"]], [None, ".", "."])
        self.assertEqual(result["issues"], [])
        app_decls = self.by_module(result, "app")
        self.assertEqual(app_decls[0]["coordinates"],
                         coords("junit", "junit", "4.13.2", scope="test"))
        self.assertEqual(result["coverage"]["counts"],
                         {"modules": 3, "modules_read": 3, "declarations": 1, "issues": 0})

    def test_nested_module_directories(self):
        self.write_pom("pom.xml", "<modules><module>deep/nested</module></modules>")
        self.write_pom("deep/nested/pom.xml", "<artifactId>n</artifactId>")
        result = self.discover()
        self.assertEqual([m["id"] for m in result["modules"]], [".", "deep/nested"])
        self.assertEqual(result["modules"][1]["pom"]["path"],
                         str(self.repo / "deep" / "nested" / "pom.xml"))

    def test_transitive_dependency_not_resolved(self):
        self.write_pom("pom.xml", """
  <groupId>com.example</groupId><artifactId>root</artifactId><version>1</version>
  <dependencies><dependency><groupId>log4j</groupId><artifactId>log4j</artifactId>
    <version>1.2.17</version></dependency></dependencies>
""")
        result = self.discover()
        self.assertEqual(len(result["modules"]), 1)
        self.assertEqual(self.kinds(result["declarations"]), ["dependency"])
        self.assertEqual(result["declarations"][0]["coordinates"]["artifactId"], "log4j")

    def test_namespace_and_no_namespace_agree(self):
        body = """
  <groupId>g</groupId><artifactId>a</artifactId><version>1</version>
  <modules><module>sub</module></modules>
  <dependencies><dependency><groupId>d</groupId><artifactId>d</artifactId>
    <version>2</version></dependency></dependencies>
"""
        self.write_pom("pom.xml", body, ns=True)
        self.write_pom("sub/pom.xml", "<artifactId>s</artifactId>", ns=True)
        namespaced = self.discover()
        self.repo.joinpath("pom.xml").unlink()
        self.write_pom("pom.xml", body, ns=False)
        self.write_pom("sub/pom.xml", "<artifactId>s</artifactId>", ns=False)
        plain = self.discover()
        self.assertEqual([d["coordinates"] for d in namespaced["declarations"]],
                         [d["coordinates"] for d in plain["declarations"]])
        self.assertEqual([d["source"]["xml_path"] for d in namespaced["declarations"]],
                         [d["source"]["xml_path"] for d in plain["declarations"]])
        self.assertEqual([m["id"] for m in namespaced["modules"]],
                         [m["id"] for m in plain["modules"]])

    def test_duplicate_and_cyclic_module_refs_bounded(self):
        self.write_pom("pom.xml", "<modules><module>a</module><module>a</module></modules>")
        self.write_pom("a/pom.xml", "<modules><module>..</module></modules>")
        result = self.discover()
        self.assertEqual([m["id"] for m in result["modules"]], [".", "a"])
        self.assertEqual(self.codes(result), ["duplicate-module", "module-cycle"])

    def test_module_and_depth_limits(self):
        self.write_pom("pom.xml", "<modules><module>a</module><module>b</module></modules>")
        self.write_pom("a/pom.xml", "<modules><module>c</module></modules>")
        self.write_pom("b/pom.xml", "")
        self.write_pom("a/c/pom.xml", "")
        capped = self.discover(max_modules=2)
        self.assertEqual([m["id"] for m in capped["modules"]], [".", "a"])
        self.assertIn("module-limit", self.codes(capped))
        shallow = self.discover(max_depth=1)
        self.assertEqual([m["id"] for m in shallow["modules"]], [".", "a", "b"])
        self.assertEqual(self.codes(shallow), ["depth-limit"])
        root_only = self.discover(max_depth=0)
        self.assertEqual([m["id"] for m in root_only["modules"]], ["."])


class DeclarationSemanticsTest(DiscoverTestCase):
    def test_management_and_bom_import_are_not_inclusion(self):
        self.write_pom("pom.xml", """
  <groupId>g</groupId><artifactId>a</artifactId><version>1</version>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-bom</artifactId>
      <version>5.3.1</version><type>pom</type><scope>import</scope></dependency>
    <dependency><groupId>commons</groupId><artifactId>commons-lang3</artifactId>
      <version>3.12</version></dependency>
  </dependencies></dependencyManagement>
""")
        decls = self.discover()["declarations"]
        self.assertEqual(self.kinds(decls), ["bom-import", "managed-dependency"])
        for decl in decls:
            self.assertEqual(decl["realm"], "project")
            self.assertTrue(decl["management_only"])
        self.assertNotIn("dependency", self.kinds(decls))
        self.assertEqual(decls[0]["coordinates"]["type"], "pom")

    def test_plugin_and_project_realms_are_distinct(self):
        self.write_pom("pom.xml", """
  <groupId>g</groupId><artifactId>a</artifactId><version>1</version>
  <dependencies><dependency><groupId>org.apache.maven.plugins</groupId>
    <artifactId>maven-compiler-plugin</artifactId><version>3.8.1</version></dependency></dependencies>
  <build>
    <pluginManagement><plugins>
      <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId>
        <version>2.22.2</version>
        <dependencies><dependency><groupId>tools</groupId><artifactId>tool-dep</artifactId>
          <version>9</version></dependency></dependencies></plugin>
    </plugins></pluginManagement>
    <plugins>
      <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId>
        <version>3.8.1</version>
        <dependencies><dependency><groupId>ann</groupId><artifactId>annotation-dep</artifactId>
          <version>2</version></dependency></dependencies></plugin>
    </plugins>
  </build>
""")
        decls = self.discover()["declarations"]
        by_kind = {}
        for decl in decls:
            by_kind.setdefault(decl["kind"], []).append(decl)
        self.assertEqual(set(by_kind), {"dependency", "plugin", "plugin-dependency"})
        project_dep, = by_kind["dependency"]
        self.assertEqual(project_dep["realm"], "project")
        self.assertFalse(project_dep["management_only"])
        self.assertIsNone(project_dep["owning_plugin"])
        surefire_plugin, compiler_plugin = by_kind["plugin"]
        self.assertEqual(compiler_plugin["coordinates"]["artifactId"], "maven-compiler-plugin")
        self.assertEqual(surefire_plugin["coordinates"]["artifactId"], "maven-surefire-plugin")
        self.assertTrue(surefire_plugin["management_only"])
        self.assertFalse(compiler_plugin["management_only"])
        for plugin_decl in by_kind["plugin"]:
            self.assertEqual(plugin_decl["realm"], "build-plugin")
        compiler_dep, surefire_dep = sorted(by_kind["plugin-dependency"],
                                            key=lambda d: d["coordinates"]["artifactId"])
        self.assertEqual(compiler_dep["owning_plugin"],
                         {"groupId": "org.apache.maven.plugins",
                          "artifactId": "maven-compiler-plugin", "version": "3.8.1"})
        self.assertEqual(compiler_dep["coordinates"]["artifactId"], "annotation-dep")
        self.assertFalse(compiler_dep["management_only"])
        self.assertEqual(surefire_dep["owning_plugin"]["artifactId"], "maven-surefire-plugin")
        self.assertTrue(surefire_dep["management_only"])
        for decl in by_kind["plugin-dependency"]:
            self.assertEqual(decl["realm"], "build-plugin")
        same_coords = [d for d in decls
                       if d["coordinates"]["artifactId"] == "maven-compiler-plugin"]
        self.assertEqual({d["kind"] for d in same_coords}, {"dependency", "plugin"})

    def test_profile_dependencies_and_modules_active_unknown(self):
        self.write_pom("pom.xml", """
  <groupId>g</groupId><artifactId>a</artifactId><version>1</version>
  <profiles><profile><id>it</id>
    <dependencies><dependency><groupId>pg</groupId><artifactId>pdep</artifactId>
      <version>3</version></dependency></dependencies>
    <modules><module>extra</module></modules>
  </profile></profiles>
""")
        self.write_pom("extra/pom.xml", "<artifactId>extra</artifactId>")
        result = self.discover()
        source = "project/profiles/profile[@id='it']"
        dep, = result["declarations"]
        self.assertEqual(dep["kind"], "dependency")
        self.assertEqual(dep["profile"], {"id": "it", "source": source})
        self.assertEqual(dep["activation"], "unknown")
        extra, = [m for m in result["modules"] if m["id"] == "extra"]
        self.assertEqual(extra["activation"], "unknown")
        self.assertEqual(extra["declared_in_profile"], {"id": "it", "source": source})
        root, = [m for m in result["modules"] if m["id"] == "."]
        self.assertEqual(root["profiles"],
                         [{"id": "it", "activation": "unknown", "source": source,
                           "properties": {}}])
        self.assertEqual(root["profile_modules"],
                         [{"profile": {"id": "it", "source": source}, "path": "extra"}])

    def test_raw_property_strings_preserved(self):
        self.write_pom("pom.xml", """
  <groupId>com.example</groupId><artifactId>root</artifactId><version>${revision}</version>
  <properties><revision>main</revision><empty></empty></properties>
  <dependencies><dependency><groupId>g</groupId><artifactId>d</artifactId>
    <version>${d.version}</version><optional>${maybe}</optional></dependency></dependencies>
""")
        result = self.discover()
        root, = result["modules"]
        self.assertEqual(root["properties"], {"revision": "main", "empty": ""})
        self.assertEqual(root["coordinates"]["version"], "${revision}")
        dep, = result["declarations"]
        self.assertEqual(dep["coordinates"]["version"], "${d.version}")
        self.assertEqual(dep["coordinates"]["optional"], "${maybe}")
        self.assertIsNone(dep["coordinates"]["scope"])

    def test_parent_reference_is_metadata_only(self):
        self.write_pom("pom.xml", "<modules><module>child</module></modules>")
        self.write_pom("child/pom.xml", """
  <parent><groupId>com.example</groupId><artifactId>root</artifactId>
    <version>1</version><relativePath>../pom.xml</relativePath></parent>
  <artifactId>child</artifactId>
""")
        result = self.discover()
        child, = [m for m in result["modules"] if m["id"] == "child"]
        self.assertEqual(child["parent"], {"groupId": "com.example", "artifactId": "root",
                                           "version": "1", "relativePath": "../pom.xml"})
        self.assertEqual([m["id"] for m in result["modules"]], [".", "child"])

    def test_capture_metadata_attached_to_modules(self):
        path = self.write_pom("pom.xml", "<artifactId>r</artifactId>")
        raw = path.read_bytes()
        root, = self.discover()["modules"]
        self.assertEqual(root["pom"]["path"], str(path))
        self.assertEqual(root["pom"]["bytes"], len(raw))
        self.assertEqual(root["pom"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(root["pom"]["complete"])
        when = datetime.fromisoformat(root["pom"]["observed_at"])
        self.assertIsNotNone(when.tzinfo)

    def test_xml_logical_paths(self):
        self.write_pom("pom.xml", """
  <artifactId>r</artifactId>
  <dependencies>
    <dependency><groupId>g</groupId><artifactId>one</artifactId><version>1</version></dependency>
    <dependency><groupId>g</groupId><artifactId>two</artifactId><version>2</version></dependency>
  </dependencies>
  <profiles><profile><id>dev</id>
    <dependencies><dependency><groupId>g</groupId><artifactId>three</artifactId>
      <version>3</version></dependency></dependencies>
  </profile></profiles>
  <build><plugins><plugin><groupId>pg</groupId><artifactId>p</artifactId><version>1</version>
    <dependencies><dependency><groupId>g</groupId><artifactId>four</artifactId>
      <version>4</version></dependency></dependencies></plugin></plugins></build>
""")
        paths = [d["source"]["xml_path"] for d in self.discover()["declarations"]]
        self.assertEqual(paths, [
            "project/dependencies/dependency",
            "project/dependencies/dependency[2]",
            "project/profiles/profile[@id='dev']/dependencies/dependency",
            "project/build/plugins/plugin",
            "project/build/plugins/plugin/dependencies/dependency",
        ])


class RootValidationTest(DiscoverTestCase):
    def write_raw(self, text):
        (self.repo / "pom.xml").write_text(text, encoding="utf-8")

    def test_foreign_namespace_root_rejected_without_traversal(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "pom.xml").write_text("<artifactId>sub</artifactId>", encoding="utf-8")
        self.write_raw('<f:project xmlns:f="urn:foreign">'
                       "<modules><module>sub</module></modules>"
                       "<dependencies><dependency><groupId>g</groupId><artifactId>d</artifactId>"
                       "<version>1</version></dependency></dependencies></f:project>")
        result = self.discover()
        self.assertEqual(self.codes(result), ["nonproject-root"])
        root, = result["modules"]
        self.assertEqual(root["status"], "rejected")
        self.assertIsNotNone(root["pom"])
        self.assertEqual(result["declarations"], [])
        self.assertEqual([m["id"] for m in result["modules"]], ["."])

    def test_wrong_root_local_name_rejected(self):
        self.write_raw("<projectx><artifactId>r</artifactId></projectx>")
        result = self.discover()
        self.assertEqual(self.codes(result), ["nonproject-root"])
        self.assertEqual(result["declarations"], [])

    def test_nested_foreign_namespace_lookalikes_ignored(self):
        self.write_pom("pom.xml", """
  <artifactId>r</artifactId>
  <dependencies><dependency><groupId>g</groupId><artifactId>real</artifactId>
    <version>1</version></dependency></dependencies>
  <x:dependencies xmlns:x="urn:foreign">
    <x:dependency><groupId>g</groupId><artifactId>fake</artifactId><version>2</version></x:dependency>
    <dependency><groupId>g</groupId><artifactId>fake2</artifactId><version>3</version></dependency>
  </x:dependencies>
  <x:modules xmlns:x="urn:foreign"><x:module>ghost</x:module></x:modules>
  <x:project xmlns:x="urn:foreign">
    <modules><module>ghost2</module></modules>
  </x:project>
""")
        for ghost in ("ghost", "ghost2"):
            (self.repo / ghost).mkdir()
            (self.repo / ghost / "pom.xml").write_text(f"<artifactId>{ghost}</artifactId>", encoding="utf-8")
        result = self.discover()
        self.assertEqual(result["issues"], [])
        real, = result["declarations"]
        self.assertEqual(real["coordinates"]["artifactId"], "real")
        self.assertEqual([m["id"] for m in result["modules"]], ["."])


class IdlessProfileTest(DiscoverTestCase):
    def test_idless_profile_keeps_unknown_activation_and_stable_locator(self):
        self.write_pom("pom.xml", """
  <artifactId>r</artifactId>
  <profiles>
    <profile>
      <dependencies><dependency><groupId>g</groupId><artifactId>anon</artifactId>
        <version>1</version></dependency></dependencies>
      <modules><module>extra</module></modules>
    </profile>
    <profile>
      <modules><module>extra2</module></modules>
    </profile>
  </profiles>
""")
        self.write_pom("extra/pom.xml", "<artifactId>extra</artifactId>")
        self.write_pom("extra2/pom.xml", "<artifactId>extra2</artifactId>")
        result = self.discover()
        dep, = result["declarations"]
        self.assertEqual(dep["profile"], {"id": None, "source": "project/profiles/profile"})
        self.assertEqual(dep["activation"], "unknown")
        root, = [m for m in result["modules"] if m["id"] == "."]
        self.assertEqual([p["id"] for p in root["profiles"]], [None, None])
        self.assertEqual([p["source"] for p in root["profiles"]],
                         ["project/profiles/profile", "project/profiles/profile[2]"])
        self.assertEqual([p["activation"] for p in root["profiles"]], ["unknown", "unknown"])
        extra, = [m for m in result["modules"] if m["id"] == "extra"]
        self.assertEqual(extra["activation"], "unknown")
        self.assertEqual(extra["declared_in_profile"],
                         {"id": None, "source": "project/profiles/profile"})
        extra2, = [m for m in result["modules"] if m["id"] == "extra2"]
        self.assertEqual(extra2["activation"], "unknown")
        self.assertEqual(extra2["declared_in_profile"],
                         {"id": None, "source": "project/profiles/profile[2]"})
        self.assertEqual(root["profile_modules"],
                         [{"profile": {"id": None, "source": "project/profiles/profile"},
                           "path": "extra"},
                          {"profile": {"id": None, "source": "project/profiles/profile[2]"},
                           "path": "extra2"}])

    def test_profile_properties_scoped_not_flattened_over_root(self):
        self.write_pom("pom.xml", """
  <artifactId>r</artifactId>
  <properties><feature>root-value</feature></properties>
  <profiles><profile><id>p</id>
    <properties><feature>profile-value</feature><only-profile>yes</only-profile></properties>
  </profile></profiles>
""")
        root, = self.discover()["modules"]
        self.assertEqual(root["properties"], {"feature": "root-value"})
        profile, = root["profiles"]
        self.assertEqual(profile["properties"],
                         {"feature": "profile-value", "only-profile": "yes"})


class DuplicateProfileIdTest(DiscoverTestCase):
    def test_duplicate_profile_ids_get_distinct_positional_locators(self):
        self.write_pom("pom.xml", """
  <artifactId>r</artifactId>
  <profiles>
    <profile><id>dev</id>
      <dependencies><dependency><groupId>g</groupId><artifactId>dep-a</artifactId>
        <version>1</version></dependency></dependencies>
      <modules><module>mod-a</module></modules>
    </profile>
    <profile><id>dev</id>
      <dependencies><dependency><groupId>g</groupId><artifactId>dep-b</artifactId>
        <version>2</version></dependency></dependencies>
      <modules><module>mod-b</module></modules>
    </profile>
    <profile><id>it</id>
      <dependencies><dependency><groupId>g</groupId><artifactId>dep-c</artifactId>
        <version>3</version></dependency></dependencies>
    </profile>
  </profiles>
""")
        self.write_pom("mod-a/pom.xml", "<artifactId>ma</artifactId>")
        self.write_pom("mod-b/pom.xml", "<artifactId>mb</artifactId>")
        result = self.discover()
        first, second, unique = result["modules"][0]["profiles"]
        self.assertEqual([p["id"] for p in (first, second, unique)], ["dev", "dev", "it"])
        self.assertEqual(first["source"], "project/profiles/profile")
        self.assertEqual(second["source"], "project/profiles/profile[2]")
        self.assertEqual(unique["source"], "project/profiles/profile[@id='it']")
        self.assertNotEqual(first["source"], second["source"])
        dep_a, dep_b, dep_c = result["declarations"]
        self.assertEqual(dep_a["profile"], {"id": "dev", "source": "project/profiles/profile"})
        self.assertEqual(dep_b["profile"], {"id": "dev", "source": "project/profiles/profile[2]"})
        self.assertEqual(dep_c["profile"], {"id": "it", "source": "project/profiles/profile[@id='it']"})
        mod_a, = [m for m in result["modules"] if m["id"] == "mod-a"]
        mod_b, = [m for m in result["modules"] if m["id"] == "mod-b"]
        self.assertEqual(mod_a["declared_in_profile"],
                         {"id": "dev", "source": "project/profiles/profile"})
        self.assertEqual(mod_b["declared_in_profile"],
                         {"id": "dev", "source": "project/profiles/profile[2]"})
        self.assertEqual([mod_a["activation"], mod_b["activation"]], ["unknown", "unknown"])


class LinearLocatorTest(DiscoverTestCase):
    def test_large_sibling_batch_scans_each_parent_once(self):
        import mdi.discover as discover_module

        def build(count):
            deps = "".join(
                f"<dependency><groupId>g</groupId><artifactId>a{i}</artifactId>"
                f"<version>1</version></dependency>" for i in range(count))
            self.write_pom("pom.xml",
                           f"<artifactId>r</artifactId><dependencies>{deps}</dependencies>")

        build(25)
        with mock.patch.object(discover_module, "_tag_positions",
                               wraps=discover_module._tag_positions) as small_spy:
            small = self.discover()
        build(500)
        with mock.patch.object(discover_module, "_tag_positions",
                               wraps=discover_module._tag_positions) as large_spy:
            large = self.discover()
        self.assertEqual(len(small["declarations"]), 25)
        self.assertEqual(len(large["declarations"]), 500)
        self.assertEqual(small["declarations"][0]["source"]["xml_path"],
                         "project/dependencies/dependency")
        self.assertEqual(large["declarations"][-1]["source"]["xml_path"],
                         "project/dependencies/dependency[500]")
        self.assertEqual(large_spy.call_count, small_spy.call_count)


class FailureHandlingTest(DiscoverTestCase):
    def test_malformed_child_does_not_block_others(self):
        self.write_pom("pom.xml", "<modules><module>good</module><module>bad</module></modules>")
        self.write_pom("good/pom.xml", """
  <artifactId>good</artifactId>
  <dependencies><dependency><groupId>g</groupId><artifactId>d</artifactId>
    <version>1</version></dependency></dependencies>
""")
        (self.repo / "bad").mkdir()
        (self.repo / "bad" / "pom.xml").write_text("<project><dependencies>", encoding="utf-8")
        result = self.discover()
        self.assertEqual(self.codes(result), ["malformed-pom"])
        bad, = [m for m in result["modules"] if m["id"] == "bad"]
        self.assertEqual(bad["status"], "malformed")
        self.assertEqual(self.kinds(self.by_module(result, "good")), ["dependency"])

    def test_missing_child_pom(self):
        self.write_pom("pom.xml", "<modules><module>ghost</module></modules>")
        (self.repo / "ghost").mkdir()
        result = self.discover()
        self.assertEqual(self.codes(result), ["missing-pom"])
        ghost, = [m for m in result["modules"] if m["id"] == "ghost"]
        self.assertEqual(ghost["status"], "missing")
        self.assertIsNone(ghost["pom"])

    def test_missing_module_directory(self):
        self.write_pom("pom.xml", "<modules><module>void</module></modules>")
        result = self.discover()
        self.assertEqual(self.codes(result), ["module-dir-missing"])
        self.assertEqual([m["status"] for m in result["modules"]], ["read", "missing"])

    def test_missing_root_pom(self):
        result = self.discover()
        self.assertEqual(self.codes(result), ["missing-pom"])
        root, = result["modules"]
        self.assertEqual(root["id"], ".")
        self.assertEqual(root["status"], "missing")
        self.assertEqual(result["declarations"], [])

    def test_oversized_pom_is_incomplete_without_digest(self):
        self.write_pom("pom.xml", "<artifactId>r</artifactId>\n" + "<!-- padding -->\n" * 20)
        result = self.discover(xml_bytes=64)
        self.assertEqual(self.codes(result), ["incomplete-pom"])
        root, = result["modules"]
        self.assertEqual(root["status"], "incomplete")
        self.assertIsNone(root["pom"]["sha256"])
        self.assertFalse(root["pom"]["complete"])
        self.assertEqual(result["declarations"], [])

    def test_unsupported_platform_reports_issue(self):
        self.write_pom("pom.xml", "<artifactId>r</artifactId>")
        with mock.patch("os.O_NOFOLLOW", 0):
            result = self.discover()
        self.assertEqual(self.codes(result), ["unsupported-platform"])
        self.assertEqual(result["modules"], [])


class ContainmentTest(DiscoverTestCase):
    def secret(self):
        path = self.outside / "secret.txt"
        path.write_bytes(b"top secret payload")
        return path

    def assert_secret_absent(self, result, secret_path):
        digest = hashlib.sha256(secret_path.read_bytes()).hexdigest()
        rendered = json.dumps(result)
        self.assertNotIn(digest, rendered)
        self.assertNotIn("top secret payload", rendered)

    def test_escaping_module_path_rejected(self):
        secret = self.secret()
        self.write_pom("pom.xml", "<modules><module>../outside</module></modules>")
        result = self.discover()
        self.assertEqual(self.codes(result), ["outside-root-module-path"])
        self.assertEqual([m["id"] for m in result["modules"]], ["."])
        self.assert_secret_absent(result, secret)

    def test_absolute_module_path_rejected(self):
        self.write_pom("pom.xml", f"<modules><module>{self.outside}</module></modules>")
        result = self.discover()
        self.assertEqual(self.codes(result), ["absolute-module-path"])
        self.assertEqual([m["id"] for m in result["modules"]], ["."])

    def test_empty_module_path_rejected(self):
        self.write_pom("pom.xml", "<modules><module>   </module></modules>")
        self.assertEqual(self.codes(self.discover()), ["invalid-module-path"])

    def test_symlinked_root_rejected(self):
        secret = self.secret()
        link = self.repo.parent / "linked-checkout"
        link.symlink_to(self.repo)
        self.addCleanup(link.unlink)
        result = discover_declarations(link)
        self.assertEqual(self.codes(result), ["root-rejected"])
        self.assertIn("symlink", result["issues"][0]["detail"])
        root, = result["modules"]
        self.assertEqual(root["status"], "rejected")
        self.assert_secret_absent(result, secret)

    def test_symlinked_module_directory_rejected(self):
        secret = self.secret()
        (self.outside / "evil").mkdir()
        (self.outside / "evil" / "pom.xml").write_text(pom("<artifactId>evil</artifactId>"), encoding="utf-8")
        self.write_pom("pom.xml", "<modules><module>trap</module></modules>")
        (self.repo / "trap").symlink_to(self.outside / "evil")
        result = self.discover()
        self.assertEqual(self.codes(result), ["module-dir-rejected"])
        trap, = [m for m in result["modules"] if m["id"] == "trap"]
        self.assertEqual(trap["status"], "rejected")
        self.assert_secret_absent(result, secret)

    def test_symlinked_pom_rejected(self):
        secret = self.secret()
        self.write_pom("pom.xml", "<modules><module>app</module></modules>")
        (self.repo / "app").mkdir()
        (self.repo / "app" / "pom.xml").symlink_to(secret)
        result = self.discover()
        self.assertEqual(self.codes(result), ["pom-symlink-rejected"])
        app, = [m for m in result["modules"] if m["id"] == "app"]
        self.assertEqual(app["status"], "rejected")
        self.assertIsNone(app["pom"])
        self.assert_secret_absent(result, secret)


class PurityTest(DiscoverTestCase):
    def test_no_network_or_process_calls_and_no_writes(self):
        self.write_pom("pom.xml", """
  <groupId>g</groupId><artifactId>a</artifactId><version>1</version>
  <modules><module>app</module></modules>
""")
        self.write_pom("app/pom.xml", """
  <dependencies><dependency><groupId>d</groupId><artifactId>d</artifactId>
    <version>1</version></dependency></dependencies>
""")

        def snapshot():
            state = {}
            for path in sorted(self.repo.rglob("*")):
                state[str(path)] = (path.is_file(), path.read_bytes() if path.is_file() else None)
            return state

        before = snapshot()
        guards = [mock.patch.object(socket, "socket", side_effect=AssertionError("network")),
                  mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")),
                  mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")),
                  mock.patch.object(subprocess, "run", side_effect=AssertionError("process")),
                  mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
                  mock.patch.object(subprocess, "check_output", side_effect=AssertionError("process"))]
        with contextlib.ExitStack() as stack:
            for guard in guards:
                stack.enter_context(guard)
            result = self.discover()
        self.assertEqual(result["coverage"]["counts"]["modules_read"], 2)
        self.assertEqual(snapshot(), before)


class PublicDescriptorHelperTest(DiscoverTestCase):
    """open_checkout_fd / open_module_dir / capture_at public surface."""

    def setUp(self):
        super().setUp()
        (self.repo / "a" / "b").mkdir(parents=True)
        (self.repo / "a" / "b" / "file.txt").write_bytes(b"payload")

    def test_open_checkout_fd_returns_open_directory(self):
        fd = open_checkout_fd(self.repo)
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            os.close(fd)

    def test_open_module_dir_opens_relative_to_root(self):
        root = open_checkout_fd(self.repo)
        try:
            fd = open_module_dir(root, ("a", "b"), str(self.repo / "a" / "b"))
            try:
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
            finally:
                os.close(fd)
        finally:
            os.close(root)

    def test_open_module_dir_empty_parts_dups_root(self):
        root = open_checkout_fd(self.repo)
        try:
            fd = open_module_dir(root, (), str(self.repo))
            try:
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
                self.assertNotEqual(fd, root)
            finally:
                os.close(fd)
        finally:
            os.close(root)

    def test_open_module_dir_rejects_escaping_parts(self):
        root = open_checkout_fd(self.repo)
        self.addCleanup(os.close, root)
        for parts in (("..",), ("a", "..", ".."), (".",), ("",), ("/etc",),
                      ("a/b",), ("a\x00b",)):
            with self.assertRaises(ValueError, msg=repr(parts)):
                open_module_dir(root, parts, str(self.repo))

    def test_open_module_dir_rejects_nul_before_open(self):
        root = open_checkout_fd(self.repo)
        self.addCleanup(os.close, root)
        with mock.patch("os.open", side_effect=AssertionError("validation must precede os.open")):
            with self.assertRaises(ValueError) as ctx:
                open_module_dir(root, ("a\x00b",), str(self.repo))
        self.assertIn("plain directory name", str(ctx.exception))

    def test_open_module_dir_accepts_literal_backslash_name(self):
        weird = self.repo / "a\\x00b"
        weird.mkdir()
        root = open_checkout_fd(self.repo)
        self.addCleanup(os.close, root)
        fd = open_module_dir(root, ("a\\x00b",), str(weird))
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            os.close(fd)

    def test_capture_at_reads_leaf_relative_to_dirfd(self):
        root = open_checkout_fd(self.repo)
        self.addCleanup(os.close, root)
        fd = open_module_dir(root, ("a", "b"), str(self.repo / "a" / "b"))
        self.addCleanup(os.close, fd)
        captured = capture_at(fd, "file.txt", str(self.repo / "a" / "b" / "file.txt"), 4096)
        self.assertEqual(captured.data, b"payload")
        self.assertTrue(captured.complete)
        self.assertEqual(captured.sha256, hashlib.sha256(b"payload").hexdigest())

    def test_capture_at_rejects_non_leaf_names(self):
        root = open_checkout_fd(self.repo)
        self.addCleanup(os.close, root)
        fd = open_module_dir(root, ("a", "b"), str(self.repo / "a" / "b"))
        self.addCleanup(os.close, fd)
        for name in ("", ".", "..", "a/b", "/etc", "file\x00.txt"):
            with self.assertRaises(ValueError, msg=repr(name)):
                capture_at(fd, name, "subject", 4096)

    def test_capture_at_rejects_bad_limits(self):
        root = open_checkout_fd(self.repo)
        self.addCleanup(os.close, root)
        fd = open_module_dir(root, ("a", "b"), str(self.repo / "a" / "b"))
        self.addCleanup(os.close, fd)
        with self.assertRaises(TypeError):
            capture_at(fd, "file.txt", "subject", True)
        with self.assertRaises(ValueError):
            capture_at(fd, "file.txt", "subject", 0)

    def test_capture_at_symlink_leaf_refused(self):
        root = open_checkout_fd(self.repo)
        self.addCleanup(os.close, root)
        fd = open_module_dir(root, ("a", "b"), str(self.repo / "a" / "b"))
        self.addCleanup(os.close, fd)
        os.symlink(self.repo / "a" / "b" / "file.txt", self.repo / "a" / "b" / "link.txt")
        with self.assertRaises(OSError):
            capture_at(fd, "link.txt", "subject", 4096)

    def test_capture_survives_ancestor_replacement_while_fd_held(self):
        root = open_checkout_fd(self.repo)
        self.addCleanup(os.close, root)
        fd = open_module_dir(root, ("a", "b"), str(self.repo / "a" / "b"))
        self.addCleanup(os.close, fd)
        shutil.move(str(self.repo / "a"), str(self.repo / "a2"))
        (self.repo / "a" / "b").mkdir(parents=True)
        (self.repo / "a" / "b" / "file.txt").write_bytes(b"replacement")
        captured = capture_at(fd, "file.txt", "provenance-only", 4096)
        self.assertEqual(captured.data, b"payload")
        self.assertEqual(captured.path, "provenance-only")


if __name__ == "__main__":
    unittest.main()
