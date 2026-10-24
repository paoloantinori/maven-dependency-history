"""Tests for mdi.candidates.discover_candidates: TASK20 orchestration slice.

Most cases run against real temp POMs and zipfile-built archives with the git
context mocked, so they exercise only declaration and archive discovery. One
case builds a real synthetic git checkout and lets the real checkout_context
run. No network, no Maven, no writes inside the inspected tree.
"""

import json
import os
import re
import shutil
import sys
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.candidates import discover_candidates  # noqa: E402
from tests import child_env

_FAKE_CONTEXT = {
    "repo": "/fake",
    "status": "unavailable",
    "revision": None,
    "dirty": None,
    "issues": ["mocked: no git context"],
    "tool": {"name": "git", "path": None},
    "coverage": "mocked",
    "observed_at": "2026-01-01T00:00:00+00:00",
}


def _context():
    return json.loads(json.dumps(_FAKE_CONTEXT))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdi-candidates-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mock_ctx = mock.patch("mdi.candidates.checkout_context",
                                   side_effect=lambda repo, **kw: _context())
        self.mock_ctx.start()
        self.addCleanup(self.mock_ctx.stop)

    def repo(self, rel):
        return os.path.join(self.tmp, rel)

    def write(self, rel, text):
        path = self.repo(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def pom(self, rel="pom.xml", body=""):
        return self.write(rel, f"<project><artifactId>root</artifactId>{body}</project>")

    def jar(self, rel, props=None, extra=None):
        path = self.repo(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            if props is not None:
                lines = "".join(f"{k}={v}\n" for k, v in props.items())
                zf.writestr("META-INF/maven/g/a/pom.properties", lines)
            for name, data in (extra or {}).items():
                zf.writestr(name, data)
        return path

    def discover(self, query, **kw):
        return discover_candidates(self.tmp, query, **kw)


class QueryValidation(Base):
    def test_empty_query_rejected(self):
        with self.assertRaises(ValueError):
            self.discover("   ")

    def test_qualified_query_shape(self):
        result = self.discover("g:a:1.0")
        self.assertEqual(result["query"], {"raw": "g:a:1.0", "kind": "qualified",
                                           "group_id": "g", "artifact_id": "a",
                                           "version": "1.0"})

    def test_bare_query_shape(self):
        result = self.discover("lib")
        self.assertEqual(result["query"]["kind"], "bare")
        self.assertIsNone(result["query"]["group_id"])

    def test_too_many_components_rejected(self):
        with self.assertRaises(ValueError):
            self.discover("g:a:1.0:x")


class DeclarationCandidates(Base):
    def test_plain_dependency_matches_bare(self):
        self.pom(body="<dependencies><dependency>"
                       "<groupId>org.io</groupId><artifactId>commons-io</artifactId>"
                       "<version>2.11.0</version></dependency></dependencies>")
        result = self.discover("commons-io")
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertEqual(cand["realm"], "project")
        self.assertEqual(cand["coordinates"]["version"], "2.11.0")
        self.assertTrue(cand["match"]["text_matches"])
        self.assertTrue(cand["inclusion_evidence"])
        self.assertFalse(result["selection_required"])
        self.assertEqual(result["distinct_subjects"], 1)

    def test_duplicate_declarations_merge_but_profiles_stay_distinct(self):
        self.pom(body="<dependencyManagement><dependencies><dependency>"
                       "<groupId>g</groupId><artifactId>lib</artifactId>"
                       "<version>1.0</version></dependency></dependencies></dependencyManagement>"
                       "<dependencies><dependency><groupId>g</groupId>"
                       "<artifactId>lib</artifactId><version>1.0</version></dependency></dependencies>")
        result = self.discover("g:lib")
        self.assertEqual(len(result["candidates"]), 1)
        locators = [d["source"]["xml_path"] for d in result["candidates"][0]["declarations"]]
        self.assertEqual(len(locators), 2)
        self.assertEqual(len(set(locators)), 2)
        self.assertTrue(any(d["management_only"] for d in result["candidates"][0]["declarations"]))
        self.assertFalse(all(d["management_only"] for d in result["candidates"][0]["declarations"]))

    def test_profile_declaration_activation_unknown(self):
        self.pom(body="<profiles><profile><id>extra</id><activation><activeByDefault/>"
                       "</activation><dependencies><dependency><groupId>g</groupId>"
                       "<artifactId>plib</artifactId><version>2.0</version></dependency>"
                       "</dependencies></profile></profiles>")
        result = self.discover("plib")
        cand = result["candidates"][0]
        self.assertEqual(cand["declarations"][0]["activation"], "unknown")
        self.assertEqual(cand["declarations"][0]["profile"]["id"], "extra")

    def test_plugin_realm_separate_from_project(self):
        self.pom(body="<dependencies><dependency><groupId>g</groupId>"
                       "<artifactId>plug</artifactId><version>1</version></dependency></dependencies>"
                       "<build><plugins><plugin><groupId>g</groupId><artifactId>plug</artifactId>"
                       "<version>1</version></plugin></plugins></build>")
        result = self.discover("g:plug:1")
        realms = sorted(c["realm"] for c in result["candidates"])
        self.assertEqual(realms, ["build-plugin", "project"])
        self.assertTrue(result["selection_required"])

    def test_unresolved_property_never_matches_qualified(self):
        self.pom(body="<dependencies><dependency><groupId>g</groupId>"
                       "<artifactId>lib</artifactId><version>${lib.version}</version>"
                       "</dependency></dependencies>")
        result = self.discover("g:lib:1.0")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["distinct_subjects"], 0)

    def test_bom_import_not_inclusion_evidence(self):
        self.pom(body="<dependencyManagement><dependencies><dependency>"
                       "<groupId>g</groupId><artifactId>bomlib</artifactId>"
                       "<version>1</version><scope>import</scope><type>pom</type>"
                       "</dependency></dependencies></dependencyManagement>")
        result = self.discover("g:bomlib")
        cand = result["candidates"][0]
        self.assertEqual(cand["declarations"][0]["kind"], "bom-import")
        self.assertFalse(cand["inclusion_evidence"])


class ArchiveCandidates(Base):
    def test_outer_filename_with_exclamation_matches_literal_name(self):
        self.pom()
        self.jar("target/needle!other.jar")
        result = self.discover("needle")
        self.assertEqual(len(result["candidates"]), 1)
        copy = result["candidates"][0]["copies"][0]
        self.assertEqual(copy["entry_chain"], [])
        self.assertEqual(copy["display_path"], "target/needle!other.jar")

    def test_checkout_exclamation_does_not_fabricate_nested_ancestry(self):
        self.pom("root!part/pom.xml")
        self.jar("root!part/target/needle.jar")
        result = discover_candidates(self.repo("root!part"), "needle")
        self.assertEqual(len(result["candidates"]), 1)
        copy = result["candidates"][0]["copies"][0]
        self.assertEqual(copy["entry_chain"], [])
        self.assertEqual(copy["display_path"], "target/needle.jar")

    def test_explicit_filename_exclamation_keeps_exact_outer_display(self):
        self.pom()
        artifact = self.jar("dist/needle!other.jar")
        result = self.discover("needle", artifact=artifact)
        self.assertEqual(len(result["candidates"]), 1)
        copy = result["candidates"][0]["copies"][0]
        self.assertEqual(copy["path"], artifact)
        self.assertEqual(copy["display_path"], "needle!other.jar")
        self.assertEqual(copy["entry_chain"], [])

    def test_nested_exclamation_names_preserve_duplicate_index_chains(self):
        import io
        import warnings
        self.pom()
        inner_bytes = io.BytesIO()
        with zipfile.ZipFile(inner_bytes, "w"):
            pass
        middle_bytes = io.BytesIO()
        with zipfile.ZipFile(middle_bytes, "w") as middle:
            middle.writestr("lib/needle!inner.jar", inner_bytes.getvalue())
        outer = self.repo("target/outer!part.jar")
        os.makedirs(os.path.dirname(outer), exist_ok=True)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
            with zipfile.ZipFile(outer, "w") as archive:
                archive.writestr("lib/middle!part.jar", middle_bytes.getvalue())
                archive.writestr("lib/middle!part.jar", middle_bytes.getvalue())
        result = self.discover("needle")
        self.assertEqual(len(result["candidates"]), 2)
        copies = [candidate["copies"][0] for candidate in result["candidates"]]
        self.assertEqual(sorted(copy["entry_chain"] for copy in copies), [[0, 0], [1, 0]])
        self.assertEqual({copy["display_path"] for copy in copies},
                         {"target/outer!part.jar!lib/middle!part.jar!lib/needle!inner.jar"})
        self.assertEqual(len({candidate["id"] for candidate in result["candidates"]}), 2)

    def test_archive_metadata_candidate(self):
        self.pom()
        self.jar("target/commons-io-2.11.0.jar",
                 props={"groupId": "commons-io", "artifactId": "commons-io",
                        "version": "2.11.0"})
        result = self.discover("commons-io")
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertEqual(cand["kind"], "packaged-copy")
        self.assertEqual(cand["realm"], "unknown")
        self.assertEqual(cand["coordinate_status"], "metadata-candidate")
        self.assertTrue(cand["match"]["text_matches"])
        self.assertEqual(cand["match"]["basis"], "pom.properties")
        self.assertTrue(cand["copies"][0]["sha256"])

    def test_nested_unknown_jar_matches_bare_by_filename(self):
        self.pom()
        self.jar("target/app.war", extra={"WEB-INF/lib/mylib-1.2.jar": b"nested"})
        result = self.discover("mylib")
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertEqual(cand["coordinate_status"], "unknown")
        self.assertFalse(cand["match"]["text_matches"])
        self.assertEqual(cand["copies"][0]["entry_chain"], [0])

    def test_qualified_query_without_metadata_is_ambiguous_unverified(self):
        self.pom()
        self.jar("target/g-a-1.0.jar")
        result = self.discover("g:a:1.0")
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertEqual(cand["match"]["kind"], "ambiguous-possible")
        self.assertFalse(cand["match"]["text_matches"])

    def test_pom_artifact_does_not_suppress_archive_elsewhere(self):
        self.pom(body="<dependencies><dependency><groupId>g</groupId>"
                       "<artifactId>lib</artifactId><version>1</version></dependency></dependencies>")
        self.jar("target/other.war", extra={"WEB-INF/lib/lib-1.jar": b"x"})
        result = self.discover("lib")
        kinds = sorted(c["kind"] for c in result["candidates"])
        self.assertEqual(kinds, ["declaration", "packaged-copy"])
        realms = sorted(c["realm"] for c in result["candidates"])
        self.assertEqual(realms, ["project", "unknown"])
        self.assertTrue(result["selection_required"])

    def test_duplicate_copies_stay_distinct(self):
        self.pom()
        self.jar("target/a/commons-io-1.0.jar",
                 props={"groupId": "g", "artifactId": "commons-io", "version": "1.0"})
        self.jar("target/b/commons-io-1.0.jar",
                 props={"groupId": "g", "artifactId": "commons-io", "version": "1.0"})
        result = self.discover("g:commons-io")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(len({c["id"] for c in result["candidates"]}), 2)
        paths = {c["copies"][0]["display_path"] for c in result["candidates"]}
        self.assertEqual(len(paths), 2)

    def test_conflicting_pom_properties_kept(self):
        self.pom()
        os.makedirs(self.repo("target"), exist_ok=True)
        path = self.repo("target/conflict.jar")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("META-INF/maven/g/one/pom.properties",
                        "groupId=g\nartifactId=one\nversion=1\n")
            zf.writestr("META-INF/maven/g/two/pom.properties",
                        "groupId=g\nartifactId=two\nversion=2\n")
        result = self.discover("g:one")
        cands = result["candidates"]
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["coordinates"]["artifactId"], "one")
        result2 = self.discover("g:two")
        self.assertEqual(result2["candidates"][0]["coordinates"]["artifactId"], "two")

    def test_malformed_archive_does_not_block_sibling(self):
        self.pom()
        self.write("target/bad.jar", "not a zip at all")
        self.jar("target/good/commons-io-1.0.jar",
                 props={"groupId": "g", "artifactId": "commons-io", "version": "9"})
        result = self.discover("g:commons-io")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(any(i["code"] == "archive-malformed" for i in result["issues"]))

    def test_symlink_entry_in_target_skipped_with_issue(self):
        self.pom()
        real = self.jar("target/real/other-1.0.jar",
                        props={"groupId": "g", "artifactId": "other", "version": "1"})
        os.makedirs(self.repo("target/link"), exist_ok=True)
        os.symlink(real, self.repo("target/link/commons-io-1.0.jar"))
        result = self.discover("g:commons-io")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(any(i["code"] == "symlink-skipped" for i in result["issues"]))

    def test_max_archives_caps_and_discloses(self):
        self.pom()
        for n in range(6):
            self.jar(f"target/d{n}/commons-io-{n}.jar",
                     props={"groupId": "g", "artifactId": "commons-io", "version": str(n)})
        result = self.discover("g:commons-io", max_archives=2)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("archive-budget", result["coverage"]["archives"]["truncated"])
        self.assertEqual(result["coverage"]["archives"]["limits"]["max_archives"], 2)

    def test_explicit_artifact_inspected_even_outside_target(self):
        self.pom()
        self.jar("src/misc/g-a-1.0.jar")
        artifact = self.repo("src/misc/g-a-1.0.jar")
        result = self.discover("g:a:1.0", artifact=artifact)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["copies"][0]["display_path"],
                         os.path.basename(artifact))

    def test_explicit_artifact_outside_root_rejected(self):
        self.pom()
        with self.assertRaises(ValueError):
            self.discover("a", artifact=os.path.join(tempfile.gettempdir(), "elsewhere.jar"))

    def test_symlinked_artifact_rejected_with_issue(self):
        self.pom()
        real = self.jar("target/real.jar")
        link = self.repo("target/link.jar")
        os.symlink(real, link)
        result = self.discover("zzznope", artifact=link)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(any(i["code"] == "artifact-symlink" for i in result["issues"]))


class ModuleFilter(Base):
    def setUp(self):
        super().setUp()
        self.pom(body="<modules><module>app</module></modules>")
        self.pom("app/pom.xml",
                 body="<dependencies><dependency><groupId>g</groupId>"
                      "<artifactId>mlib</artifactId><version>1</version></dependency></dependencies>")
        self.jar("app/target/mlib-1.jar",
                 props={"groupId": "g", "artifactId": "mlib", "version": "1"})

    def test_module_filter_narrows_scope(self):
        result = self.discover("mlib", module="app")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(all(c["module"] == "app" for c in result["candidates"]))
        self.assertEqual(result["coverage"]["modules"]["selected"], ["app"])

    def test_unknown_module_rejected(self):
        with self.assertRaises(ValueError):
            self.discover("mlib", module="nope")

    def test_root_module_not_selected_by_filter(self):
        result = self.discover("root", module="app")
        self.assertEqual(result["candidates"], [])


class IdentityStability(Base):
    def test_ids_stable_across_runs(self):
        self.pom(body="<dependencies><dependency><groupId>g</groupId>"
                       "<artifactId>a</artifactId><version>1</version></dependency></dependencies>")
        self.jar("target/g-a-1.jar", props={"groupId": "g", "artifactId": "a", "version": "1"})
        first = self.discover("a")
        second = self.discover("a")
        self.assertEqual([c["id"] for c in first["candidates"]],
                         [c["id"] for c in second["candidates"]])
        self.assertGreater(len(first["candidates"]), 1)
        self.assertTrue(all(c["id"].startswith("mdi-") for c in first["candidates"]))

    def test_result_is_json_safe(self):
        self.pom()
        self.jar("target/g-a-1.jar", props={"groupId": "g", "artifactId": "a", "version": "1"})
        result = self.discover("a")
        json.dumps(result)


class CoverageDisclosure(Base):
    def test_coverage_names_missing_domains(self):
        result = self.discover("a")
        coverage = result["coverage"]
        for domain in ("resolution", "usage", "security", "history"):
            self.assertEqual(coverage[domain], "not-computed")
        self.assertIn("declarations", coverage)
        self.assertIn("archives", coverage)
        self.assertEqual(coverage["git_context"], "unavailable")

    def test_no_modules_declaration_only(self):
        self.write("notes.txt", "hello")
        result = self.discover("a")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["coverage"]["declarations"]["counts"]["modules_read"], 0)


class RealGitContext(Base):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdi-candidates-git-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
                   HOME=os.environ.get("HOME", "/tmp"))
        def git(*args):
            subprocess.run(["git", *args], cwd=self.tmp, env=env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        with open(os.path.join(self.tmp, "pom.xml"), "w") as fh:
            fh.write("<project><artifactId>root</artifactId></project>")
        git("add", "pom.xml")
        git("commit", "-q", "-m", "init")

    def test_real_context_complete(self):
        result = discover_candidates(self.tmp, "lib")
        self.assertEqual(result["context"]["status"], "complete")
        self.assertRegex(result["context"]["revision"], r"^[0-9a-f]{40}$")
        self.assertFalse(result["context"]["dirty"])
        self.assertEqual(result["coverage"]["git_context"], "complete")


class CoordinatorCorrections(Base):
    """Regressions for the review corrections."""

    def test_plugin_dependency_subject_carries_owning_plugin(self):
        self.pom(body="<build><plugins><plugin><groupId>pg</groupId>"
                      "<artifactId>host</artifactId><version>1</version>"
                      "<dependencies><dependency><groupId>g</groupId>"
                      "<artifactId>dep</artifactId><version>2</version></dependency>"
                      "</dependencies></plugin></plugins></build>")
        result = self.discover("g:dep:2")
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertEqual(cand["kind"], "declaration")
        self.assertEqual(cand["realm"], "build-plugin")
        self.assertEqual(cand["owning_plugin"]["artifactId"], "host")
        self.assertEqual(cand["declarations"][0]["kind"], "plugin-dependency")

    def test_management_and_direct_aggregate_inclusion(self):
        self.pom(body="<dependencyManagement><dependencies><dependency>"
                      "<groupId>g</groupId><artifactId>lib</artifactId>"
                      "<version>1</version></dependency></dependencies></dependencyManagement>"
                      "<dependencies><dependency><groupId>g</groupId>"
                      "<artifactId>lib</artifactId><version>1</version></dependency></dependencies>")
        result = self.discover("g:lib:1")
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertEqual(len(cand["declarations"]), 2)
        flags = sorted(d["management_only"] for d in cand["declarations"])
        self.assertEqual(flags, [False, True])
        self.assertTrue(cand["inclusion_evidence"])

    def test_management_only_subject_keeps_inclusion_false(self):
        self.pom(body="<dependencyManagement><dependencies><dependency>"
                      "<groupId>g</groupId><artifactId>mlib</artifactId>"
                      "<version>1</version></dependency></dependencies></dependencyManagement>")
        result = self.discover("g:mlib")
        self.assertFalse(result["candidates"][0]["inclusion_evidence"])

    def test_explicit_artifact_consumes_global_budget(self):
        self.pom()
        explicit = self.jar("elsewhere/g-a-1.0.jar")
        self.jar("target/g-a-1.0.jar")
        result = self.discover("g:a:1.0", artifact=explicit, max_archives=1)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["copies"][0]["display_path"],
                         "g-a-1.0.jar")
        self.assertIn("archive-budget", result["coverage"]["archives"]["truncated"])

    def test_missing_pom_does_not_suppress_root_target_scan(self):
        self.pom()
        os.unlink(self.repo("pom.xml"))
        self.jar("target/g-a-1.0.jar",
                 props={"groupId": "g", "artifactId": "a", "version": "1.0"})
        result = self.discover("g:a:1.0")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["coverage"]["modules"]["discovered"], ["."])
        decl_issues = [i["code"] for i in result["source_results"]["declarations"]["issues"]]
        self.assertIn("missing-pom", decl_issues)

    def test_symlinked_checkout_root_rejected_not_followed(self):
        real = tempfile.mkdtemp(prefix="mdi-candidates-real-")
        self.addCleanup(shutil.rmtree, real, True)
        os.symlink(real, self.repo("link"))
        result = discover_candidates(self.repo("link"), "a")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(any(i["code"] == "root-rejected" for i in result["issues"]))

    def test_symlinked_target_dir_rejected(self):
        self.pom()
        other = self.jar("other/g-a-1.0.jar")
        os.makedirs(self.repo("real-target"), exist_ok=True)
        os.symlink(self.repo("real-target"), self.repo("target"))
        result = self.discover("g:a:1.0", artifact=other)
        self.assertEqual(result["coverage"]["archives"]["captured"], 1)


    def test_explicit_artifact_in_target_not_double_counted(self):
        self.pom()
        path = self.jar("target/g-a-1.0.jar")
        result = self.discover("g:a:1.0", artifact=path)
        self.assertEqual(result["coverage"]["archives"]["captured"], 1)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(len(result["candidates"][0]["copies"]), 1)

    def test_bad_limit_types_rejected(self):
        with self.assertRaises(TypeError):
            self.discover("a", max_archives=True)
        with self.assertRaises(ValueError):
            self.discover("a", max_archives=0)
        with self.assertRaises(ValueError):
            self.discover("a", max_traversal_depth=-1)


class DescriptorHygiene(Base):
    """File-descriptor ownership regressions for the target walk."""

    def setUp(self):
        super().setUp()
        self.pom()
        for n in range(3):
            self.jar(f"target/d{n}/g-a-1.0.jar",
                     props={"groupId": "g", "artifactId": "a", "version": "1"})

    def fd_count(self):
        return len(os.listdir("/proc/self/fd"))

    def test_repeated_discovery_leaks_no_descriptors(self):
        self.discover("g:a:1.0")
        before = self.fd_count()
        for _ in range(25):
            self.discover("g:a:1.0")
        after = self.fd_count()
        self.assertEqual(after, before)

    def test_scandir_constructor_failure_closes_duplicate(self):
        from mdi import candidates
        root_fd = candidates.open_checkout_fd(self.tmp)
        self.addCleanup(os.close, root_fd)
        dir_fd = candidates.open_module_dir(root_fd, ("target",), "subject")
        self.addCleanup(os.close, dir_fd)
        before = self.fd_count()
        with mock.patch("os.scandir", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                candidates._read_dir_bounded(dir_fd, 10)
        self.assertEqual(self.fd_count(), before)
        # the dup opened for the aborted scan must be closed: opening one more
        # name relative to dir_fd still works and the fd count is unchanged.
        names, _ = candidates._read_dir_bounded(dir_fd, 10)
        self.assertTrue(any(n.directory for n in names))


class NonregularSiblingHygiene(Base):
    """FIFO/non-regular files are isolated issues, never scan aborts."""

    def test_target_fifo_does_not_abort_sibling_scan(self):
        self.pom()
        os.makedirs(self.repo("target"), exist_ok=True)
        os.mkfifo(self.repo("target/evil.jar"))
        self.jar("target/good/g-a-1.0.jar",
                 props={"groupId": "g", "artifactId": "a", "version": "1.0"})
        result = self.discover("g:a:1.0")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["copies"][0]["display_path"],
                         "target/good/g-a-1.0.jar")
        self.assertTrue(any(i["code"] == "archive-nonregular" for i in result["issues"]))

    def test_fifo_explicit_preserves_target_scan(self):
        self.pom()
        os.makedirs(self.repo("misc"), exist_ok=True)
        os.mkfifo(self.repo("misc/pipe.jar"))
        self.jar("target/g-a-1.0.jar",
                 props={"groupId": "g", "artifactId": "a", "version": "1.0"})
        result = self.discover("g:a:1.0", artifact=self.repo("misc/pipe.jar"))
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(any(i["code"] == "artifact-nonregular" for i in result["issues"]))


class _LazyEntry:
    """Fake DirEntry whose stat-backed methods must only run while armed."""

    def __init__(self, name, symlink, directory):
        self.name = name
        self._symlink = symlink
        self._directory = directory
        self.armed = True
        self.calls = 0

    def _check(self):
        if not self.armed:
            raise OSError(9, "Bad file descriptor (lazy stat after fd closed)")
        self.calls += 1

    def is_symlink(self):
        self._check()
        return self._symlink

    def is_dir(self, follow_symlinks=False):
        self._check()
        return self._directory


class _FakeScan:
    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc):
        return False


class EntryMaterializationTest(Base):
    def test_entry_metadata_materialized_while_scan_fd_live(self):
        from mdi import candidates
        os.makedirs(self.repo("target"), exist_ok=True)
        root_fd = candidates.open_checkout_fd(self.tmp)
        self.addCleanup(os.close, root_fd)
        dir_fd = candidates.open_module_dir(root_fd, ("target",), "subject")
        self.addCleanup(os.close, dir_fd)
        fakes = [_LazyEntry("b.jar", False, False), _LazyEntry("sub", False, True),
                 _LazyEntry("link", True, False)]
        with mock.patch("os.scandir", return_value=_FakeScan(fakes)):
            records = candidates._read_dir_bounded(dir_fd, 10)[0]
        for fake in fakes:
            fake.armed = False  # any later lazy stat now fails loudly
        self.assertEqual([r.name for r in records], ["b.jar", "link", "sub"])
        self.assertEqual([(r.symlink, r.directory) for r in records],
                         [(False, False), (True, False), (False, True)])
        for record in records:  # touching records must need no live fd
            _ = (record.name, record.symlink, record.directory)

    def test_explicit_in_target_inspected_once(self):
        self.pom()
        explicit = self.jar("target/g-a-1.0.jar",
                            props={"groupId": "g", "artifactId": "a", "version": "1.0"})
        self.jar("target/other-b-2.0.jar",
                 props={"groupId": "g", "artifactId": "b", "version": "2.0"})
        calls = []
        from mdi import candidates as _candidates
        real = _candidates.inspect_captured_archive
        def spy(captured, *, limits=None):
            if captured.path.endswith("g-a-1.0.jar"):
                calls.append(captured.path)
            return real(captured, limits=limits)
        with mock.patch("mdi.candidates.inspect_captured_archive", side_effect=spy):
            result = self.discover("g:a:1.0", artifact=explicit, max_archives=2)
        self.assertEqual(calls, [explicit])
        self.assertEqual(result["coverage"]["archives"]["captured"], 2)

    def test_explicit_budget_not_eaten_by_duplicate(self):
        self.pom()
        explicit = self.jar("target/g-a-1.0.jar",
                            props={"groupId": "g", "artifactId": "a", "version": "1.0"})
        self.jar("target/g-a-2.0.jar",
                 props={"groupId": "g", "artifactId": "a", "version": "2.0"})
        result = self.discover("g:a", artifact=explicit, max_archives=2)
        self.assertEqual(len(result["candidates"]), 2)
        displays = sorted(c["copies"][0]["display_path"] for c in result["candidates"])
        self.assertEqual(displays, ["g-a-1.0.jar", "target/g-a-2.0.jar"])
        self.assertNotIn("archive-budget", result["coverage"]["archives"]["truncated"])

    def test_failed_explicit_stays_eligible_in_traversal(self):
        self.pom()
        os.makedirs(self.repo("target"), exist_ok=True)
        fifo = self.repo("target/g-a-1.0.jar")
        os.mkfifo(fifo)
        result = self.discover("g:a:1.0", artifact=fifo)
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("artifact-nonregular", codes)
        # the failed explicit left no inspection record, so the traversal
        # must still attempt the same logical path rather than skip it
        self.assertIn("archive-nonregular", codes)
        self.assertEqual(result["coverage"]["archives"]["captured"], 0)


class WalkerFdDiscipline(Base):
    """Child directories open only inside the reserved budget, fds close promptly."""

    def setUp(self):
        super().setUp()
        self.pom()
        for n in range(24):
            self.jar(f"target/d{n}/g-a-1.0.jar",
                     props={"groupId": "g", "artifactId": "a", "version": "1"})

    def test_cap_one_scans_no_child_directory(self):
        # os.open must not be patched here: a Mock replacement drops os.open
        # out of os.supports_dir_fd and mdi.discover degrades to its
        # unsupported-platform branch. scandir is called exactly once per
        # visited directory, so counting scans proves no child was opened.
        scans = []
        real_scandir = os.scandir
        def counting_scandir(path):
            scans.append(path)
            return real_scandir(path)
        with mock.patch("os.scandir", side_effect=counting_scandir):
            result = self.discover("g:a:1", max_traversal_dirs=1)
        self.assertEqual(len(scans), 1)  # only the module target was scanned
        self.assertEqual(result["coverage"]["archives"]["captured"], 0)
        self.assertIn("traversal-dirs", result["coverage"]["archives"]["truncated"])
        self.assertTrue(any(i["code"] == "traversal-limit" for i in result["issues"]))

    def test_low_fd_limit_many_dirs_no_emfile(self):
        script = (
            "import resource, sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'skills' / 'maven-dependency-investigator' / 'scripts')!r})\n"
            "resource.setrlimit(resource.RLIMIT_NOFILE, (24, 24))\n"
            "from mdi.candidates import discover_candidates\n"
            f"r = discover_candidates({self.tmp!r}, 'g:a:1', max_traversal_dirs=9)\n"
            "print(r['coverage']['archives']['captured'])\n"
        )
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=60,
                              env=child_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(int(proc.stdout.strip()), 8)
        self.assertNotIn("EMFILE", proc.stderr)

    def test_fd_balance_with_bounded_queue(self):
        self.discover("g:a:1", max_traversal_dirs=4)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(10):
            self.discover("g:a:1", max_traversal_dirs=4)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


class ModuleContainment(Base):
    """Explicit artifact containment compares exact path components."""

    def setUp(self):
        super().setUp()
        self.pom(body="<modules><module>app</module><module>app2</module></modules>")
        self.pom("app/pom.xml", body="")
        self.pom("app2/pom.xml", body="")

    def test_root_module_explicit_accepted(self):
        artifact = self.jar("misc/g-a-1.0.jar",
                            props={"groupId": "g", "artifactId": "a", "version": "1.0"})
        result = self.discover("g:a:1.0", module=".", artifact=artifact)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["coverage"]["archives"]["captured"], 1)

    def test_sibling_prefix_module_escape_rejected(self):
        artifact = self.jar("app2/g-a-1.0.jar",
                            props={"groupId": "g", "artifactId": "a", "version": "1.0"})
        with self.assertRaises(ValueError):
            self.discover("g:a:1.0", module="app", artifact=artifact)


class QueuedFdDrain(Base):
    """Still-queued child fds are closed on every early exit from the walk."""

    def setUp(self):
        super().setUp()
        self.pom()
        # pop order is LIFO: d2, then d1 (captures the last archive budget),
        # then the walk must exit early with d0 still queued.
        self.jar("target/d0/x.jar", props={"groupId": "g", "artifactId": "x", "version": "1"})
        self.jar("target/d1/y.jar", props={"groupId": "g", "artifactId": "y", "version": "1"})
        os.makedirs(self.repo("target/d2"))

    def fd_count(self):
        return len(os.listdir("/proc/self/fd"))

    def test_archive_budget_exit_drains_queue(self):
        result = self.discover("g:x", max_archives=1)
        self.assertEqual(result["coverage"]["archives"]["captured"], 1)
        self.assertIn("archive-budget", result["coverage"]["archives"]["truncated"])
        before = self.fd_count()
        for _ in range(20):
            self.discover("g:x", max_archives=1)
        self.assertEqual(self.fd_count(), before)

    def test_dir_budget_exit_drains_queue(self):
        self.discover("g:x", max_archives=8, max_traversal_dirs=2)
        before = self.fd_count()
        for _ in range(20):
            self.discover("g:x", max_archives=8, max_traversal_dirs=2)
        self.assertEqual(self.fd_count(), before)

    def test_injected_exception_drains_queue(self):
        import mdi.candidates as candidates
        before = self.fd_count()
        for exc_type in (RuntimeError, KeyboardInterrupt):
            with mock.patch.object(candidates, "_capture_archive", side_effect=exc_type("stop")):
                with self.assertRaises(exc_type):
                    self.discover("g:x", max_archives=8)
            self.assertEqual(self.fd_count(), before,
                             f"fd leak after injected {exc_type.__name__}")


if __name__ == "__main__":
    unittest.main()
