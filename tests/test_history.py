"""Tests for mdi.history bounded local dependency history analysis (TASK-21.3).

Every assertion runs against the deterministic temporary Git fixture from
tests.history_fixtures (plus synthetic commits layered on top of it inside the
temp fixture, never in any project repository). Real git reads only: the
module under test observes blobs through git itself; these tests cross-check
its claims against fixture.commit_map and fixture git output.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import history
from mdi.gitinspect import _RunOutcome
from mdi.history import inspect_history
from tests.history_fixtures import (
    APP_CODE_RELPATH,
    AUTHOR_EMAIL,
    AUTHOR_NAME,
    COMMENT,
    COMMITTER_EMAIL,
    COMMITTER_NAME,
    DATES,
    LIB_B_PROP,
    PROPS,
    POM_RELPATH,
    SUBJECTS,
    _app_pom,
    _dep,
    _identity_env,
    dependency_history_fixture,
    missing_history_fixture,
)

APP_CODE_V3 = 'package com.example;\n\npublic class App {\n    public String greet() {\n        return "hello, merge";\n    }\n}\n'
LIB_B_EVENTS = [
    ("add_lib_b", "addition", None, "1.0.0"),
    ("remove_lib_b", "removal", "1.0.0", None),
    ("reintroduce_lib_b", "reintroduction", None, "1.0.0"),
    ("upgrade_lib_b_2", "version-change", "1.0.0", "2.0.0"),
    ("property_version_bump", "property-change", "2.0.0", "${lib-b.version}"),
]


def lib_b_dep(version="${lib-b.version}", **extra):
    """lib-b declaration block carrying optional raw field tags verbatim."""
    lines = ["    <dependency>",
             "      <groupId>com.example</groupId>",
             "      <artifactId>lib-b</artifactId>",
             f"      <version>{version}</version>"]
    for tag in ("type", "classifier", "scope", "optional"):
        if extra.get(tag) is not None:
            lines.append(f"      <{tag}>{extra[tag]}</{tag}>")
    lines.append("    </dependency>")
    return "\n".join(lines) + "\n"


def standalone_pom(dep, artifact_id="bracket-app"):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>{artifact_id}</artifactId>
  <version>1.0.0</version>
  <dependencies>
{dep}  </dependencies>
</project>
"""


def commit(fx, subject, date, updates):
    """Layer one synthetic commit onto the temp fixture; returns its id."""
    for relpath, content in updates.items():
        target = fx.repo_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    fx.run("add", "-A")
    fx.run("commit", "--no-verify", "--quiet", "-m", subject, env_extra=_identity_env(date))
    return fx.run("rev-parse", "HEAD").stdout.strip()


def event_key(event):
    return event["commit"]["id"]


def commit_of(fx, result, key):
    matched = [event for event in result["events"] if event["commit"]["id"] == fx.commits[key]]
    return matched[0] if matched else None


class JsonSafeMixin:
    def assert_json_safe(self, result):
        encoded = json.dumps(result, allow_nan=False)
        self.assertEqual(result, json.loads(encoded))


class DirectHistoryTest(unittest.TestCase, JsonSafeMixin):
    def test_lib_b_full_history_events_and_commit_metadata(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            self.assertEqual(fx.commits["bom_upgrade"], result["revision"])
            self.assertEqual(["app/pom.xml"], result["coverage"]["paths"])
            observed = [
                (key, event["kind"], event["old_version"], event["new_version"])
                for key, event in ((k, commit_of(fx, result, k)) for k in fx.commit_order)
                if event is not None
            ]
            self.assertEqual(LIB_B_EVENTS, observed)
            self.assertEqual([e["kind"] for e in result["events"]],
                             [row[1] for row in LIB_B_EVENTS])
            self.assertEqual("app/pom.xml", result["events"][0]["pom"])
            self.assertEqual([], result["issues"])
            self.assertEqual("complete", result["coverage"]["status"])
            self.assertFalse(result["coverage"]["truncated"])
            self.assertFalse(result["coverage"]["shallow"])
            for key, _, _, _ in LIB_B_EVENTS:
                event = commit_of(fx, result, key)
                head = event["commit"]
                self.assertEqual(SUBJECTS[key], head["subject"])
                self.assertEqual(AUTHOR_NAME, head["author"]["name"])
                self.assertEqual(AUTHOR_EMAIL, head["author"]["email"])
                self.assertEqual(COMMITTER_NAME, head["committer"]["name"])
                self.assertEqual(COMMITTER_EMAIL, head["committer"]["email"])
                self.assertEqual(datetime.fromisoformat(DATES[key]),
                                 datetime.fromisoformat(head["author_time"]))
                self.assertEqual(datetime.fromisoformat(DATES[key]),
                                 datetime.fromisoformat(head["commit_time"]))
            property_event = commit_of(fx, result, "property_version_bump")
            self.assertEqual({"lib-b.version": "3.0.0"}, property_event["property_values"])
            self.assertIsNone(commit_of(fx, result, "comment_and_code_only"))
            self.assertIsNone(commit_of(fx, result, "bom_upgrade"))
            last = result["last_modification"]
            self.assertEqual(fx.commits["property_version_bump"], last["declaration"]["commit"]["id"])
            self.assertEqual("property-change", last["declaration"]["kind"])
            self.assertEqual(fx.commits["property_version_bump"], last["file"]["commit"]["id"])
            self.assert_json_safe(result)

    def test_version_qualified_query_matches_only_known_exact_versions(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-b:2.0.0", pom_paths=["app/pom.xml"])
            self.assertEqual(
                [(event_key(e), e["kind"], e["old_version"], e["new_version"]) for e in result["events"]],
                [(fx.commits["upgrade_lib_b_2"], "addition", None, "2.0.0"),
                 (fx.commits["property_version_bump"], "removal", "2.0.0", None)],
            )
            self.assertEqual({"raw": "com.example:lib-b:2.0.0", "kind": "qualified",
                              "group_id": "com.example", "artifact_id": "lib-b", "version": "2.0.0"},
                             result["query"])

    def test_bare_query_covers_same_commits_as_qualified(self):
        with dependency_history_fixture() as fx:
            qualified = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            bare = inspect_history(fx.repo_dir, "lib-b", pom_paths=["app/pom.xml"])
            self.assertEqual([event_key(e) for e in qualified["events"]],
                             [event_key(e) for e in bare["events"]])
            self.assertEqual("bare", bare["query"]["kind"])

    def test_input_sequences_are_not_mutated(self):
        with dependency_history_fixture() as fx:
            paths = ["app/pom.xml"]
            introducing = ("com.example:lib-b",)
            inspect_history(fx.repo_dir, "com.example:lib-c", pom_paths=paths,
                            introducing_dependencies=introducing)
            self.assertEqual(["app/pom.xml"], paths)
            self.assertEqual(("com.example:lib-b",), introducing)


class CandidateIntroductionTest(unittest.TestCase, JsonSafeMixin):
    def test_lib_c_has_no_direct_events_and_b_upgrade_is_candidate_lead(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-c", pom_paths=["app/pom.xml"],
                                     introducing_dependencies=("com.example:lib-b",))
            self.assertTrue(result["events"])
            self.assertTrue(all(e["kind"] == "candidate-introduction" for e in result["events"]))
            self.assertEqual(["addition", "reintroduction", "version-change", "property-change"],
                             [e["introducing"]["change_kind"] for e in result["events"]])
            expected = ["add_lib_b", "reintroduce_lib_b", "upgrade_lib_b_2", "property_version_bump"]
            self.assertEqual([fx.commits[k] for k in expected], [event_key(e) for e in result["events"]])
            for event in result["events"]:
                self.assertEqual("com.example:lib-b", event["introducing"]["query"])
                self.assertIn("candidate", event["evidence"])
                self.assertIn("not a resolved or shipped fact", event["evidence"])
            upgrade = result["events"][2]
            self.assertEqual("1.0.0", upgrade["introducing"]["old_version"])
            self.assertEqual("2.0.0", upgrade["introducing"]["new_version"])
            self.assertIsNone(result["last_modification"]["declaration"])
            self.assertIn("candidate", " ".join(result["coverage"]["limitations"]))
            self.assert_json_safe(result)

    def test_lib_c_without_introducing_dependencies_reports_nothing(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-c", pom_paths=["app/pom.xml"])
            self.assertEqual([], result["events"])
            self.assertIsNone(result["last_modification"]["declaration"])
            self.assertEqual([], result["issues"])


class BomsAndScopesTest(unittest.TestCase, JsonSafeMixin):
    def test_root_bom_change_is_separate_from_app_module_history(self):
        with dependency_history_fixture() as fx:
            bom = inspect_history(fx.repo_dir, "com.example:corp-bom", pom_paths=["pom.xml"])
            self.assertEqual(
                [(event_key(e), e["kind"], e["old_version"], e["new_version"]) for e in bom["events"]],
                [(fx.commits["initial_poms"], "addition", None, "1.0.0"),
                 (fx.commits["bom_upgrade"], "management-change", "1.0.0", "2.0.0")],
            )
            self.assertTrue(bom["events"][0]["new_management_only"])
            self.assertIsNone(bom["events"][0]["new_profile"])
            app_query = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["pom.xml"])
            self.assertEqual([], app_query["events"])
            app_bom = inspect_history(fx.repo_dir, "com.example:corp-bom", pom_paths=["app/pom.xml"])
            self.assertEqual([], app_bom["events"])
            self.assert_json_safe(bom)


class LastModificationTest(unittest.TestCase, JsonSafeMixin):
    def test_file_only_touch_differs_from_declaration_change(self):
        with dependency_history_fixture() as fx:
            sha = commit(fx, "Comment-only pom touch", "2024-06-01T10:00:00+00:00",
                         {POM_RELPATH: _app_pom(dep=LIB_B_PROP, comment=COMMENT + "  <!-- two -->\n",
                                                props=PROPS)})
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            last = result["last_modification"]
            self.assertEqual(sha, last["file"]["commit"]["id"])
            self.assertEqual("Comment-only pom touch", last["file"]["commit"]["subject"])
            self.assertEqual(fx.commits["property_version_bump"], last["declaration"]["commit"]["id"])
            self.assertNotEqual(last["file"]["commit"]["id"], last["declaration"]["commit"]["id"])
            self.assertNotIn(sha, [event_key(e) for e in result["events"]])
            self.assertIn("not a git blame", last["note"])


class TruncationTest(unittest.TestCase, JsonSafeMixin):
    def test_max_commits_window_bounds_analysis_without_first_ever_claims(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"],
                                     max_commits=2)
            self.assertTrue(result["coverage"]["truncated"])
            self.assertEqual(2, result["coverage"]["limits"]["max_commits"])
            self.assertEqual(
                [(event_key(e), e["kind"], e["old_version"], e["new_version"]) for e in result["events"]],
                [(fx.commits["upgrade_lib_b_2"], "version-change", "1.0.0", "2.0.0"),
                 (fx.commits["property_version_bump"], "property-change", "2.0.0", "${lib-b.version}")],
            )
            joined = " ".join(result["coverage"]["limitations"])
            self.assertIn("truncated", joined)
            self.assertIn("not necessarily the first ever", joined)
            self.assert_json_safe(result)


class FirstParentMergeTest(unittest.TestCase, JsonSafeMixin):
    def test_merge_commit_attributed_and_second_parent_commit_not_listed(self):
        with dependency_history_fixture() as fx:
            fx.run("checkout", "--quiet", "-b", "side")
            commit(fx, "Side literal lib-b version", "2024-06-02T10:00:00+00:00",
                   {POM_RELPATH: _app_pom(dep="    <dependency>\n      <groupId>com.example</groupId>\n"
                                               "      <artifactId>lib-b</artifactId>\n"
                                               "      <version>2.0.0</version>\n    </dependency>\n",
                                           comment=COMMENT)})
            side_sha = fx.run("rev-parse", "HEAD").stdout.strip()
            fx.run("checkout", "--quiet", "main")
            commit(fx, "Main code-only move", "2024-06-03T10:00:00+00:00",
                   {APP_CODE_RELPATH: APP_CODE_V3})
            fx.run("merge", "--no-ff", "side", "-m", "Merge side lib-b literal",
                   env_extra=_identity_env("2024-06-04T10:00:00+00:00"))
            merge_sha = fx.run("rev-parse", "HEAD").stdout.strip()
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            merges = [e for e in result["events"] if event_key(e) == merge_sha]
            self.assertEqual(1, len(merges))
            self.assertEqual("property-change", merges[0]["kind"])
            self.assertEqual("${lib-b.version}", merges[0]["old_version"])
            self.assertEqual("2.0.0", merges[0]["new_version"])
            self.assertNotIn(side_sha, [event_key(e) for e in result["events"]])
            joined = " ".join(result["coverage"]["limitations"])
            self.assertIn("first-parent", joined)
            self.assert_json_safe(result)


class MissingHistoryTest(unittest.TestCase, JsonSafeMixin):
    def test_repo_without_git_history_is_reported_not_guessed(self):
        with missing_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            self.assertIsNone(result["revision"])
            self.assertEqual([], result["events"])
            self.assertIsNone(result["last_modification"]["declaration"])
            self.assertIsNone(result["last_modification"]["file"])
            self.assertTrue(result["issues"])
            self.assertNotEqual("complete", result["coverage"]["status"])
            self.assert_json_safe(result)


class ShallowHistoryTest(unittest.TestCase, JsonSafeMixin):
    def test_shallow_clone_is_flagged_and_boundary_is_not_first_ever(self):
        with dependency_history_fixture() as fx:
            with tempfile.TemporaryDirectory(prefix="mdi-history-shallow-") as tmp:
                shallow = Path(tmp) / "shallow"
                fx.run("clone", "--quiet", "--depth", "2", "--no-local",
                       f"file://{fx.repo_dir}", str(shallow))
                result = inspect_history(shallow, "com.example:lib-b", pom_paths=["app/pom.xml"])
                self.assertTrue(result["coverage"]["shallow"])
                joined = " ".join(result["coverage"]["limitations"])
                self.assertIn("shallow", joined)
                self.assertIn("not necessarily the first ever", joined)
                for event in result["events"]:
                    self.assertNotEqual("addition", event["kind"])
                self.assert_json_safe(result)


class WorkingTreeSafetyTest(unittest.TestCase, JsonSafeMixin):
    def test_analysis_never_touches_working_tree_or_index(self):
        with dependency_history_fixture() as fx:
            dirty = (fx.repo_dir / POM_RELPATH).read_text() + "<!-- uncommitted -->\n"
            (fx.repo_dir / POM_RELPATH).write_text(dirty)
            before_status = fx.text("status", "--porcelain")
            before_head = fx.text("rev-parse", "HEAD").strip()
            before_git = sorted(p.name for p in (fx.repo_dir / ".git").iterdir())
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            self.assertEqual(before_status, fx.text("status", "--porcelain"))
            self.assertEqual(before_head, fx.text("rev-parse", "HEAD").strip())
            self.assertEqual(before_git, sorted(p.name for p in (fx.repo_dir / ".git").iterdir()))
            self.assertFalse((fx.repo_dir / ".git" / "index.lock").exists())
            self.assertEqual(dirty, (fx.repo_dir / POM_RELPATH).read_text())
            self.assertTrue(result["events"])
            self.assert_json_safe(result)


class MalformedPomTest(unittest.TestCase, JsonSafeMixin):
    def test_malformed_historical_pom_is_an_issue_without_guessed_events(self):
        with dependency_history_fixture() as fx:
            good = _app_pom(dep="    <dependency>\n      <groupId>com.example</groupId>\n"
                                 "      <artifactId>lib-b</artifactId>\n"
                                 "      <version>${lib-b.version}</version>\n    </dependency>\n",
                            comment=COMMENT, props=PROPS)
            bad = good.replace("</project>", "")
            bad_sha = commit(fx, "Break the app pom", "2024-06-05T10:00:00+00:00", {POM_RELPATH: bad})
            fix_sha = commit(fx, "Restore the app pom", "2024-06-06T10:00:00+00:00", {POM_RELPATH: good})
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            shas = [event_key(e) for e in result["events"]]
            self.assertEqual([fx.commits[k] for k, _, _, _ in LIB_B_EVENTS], shas)
            self.assertNotIn(bad_sha, shas)
            self.assertNotIn(fix_sha, shas)
            malformed = [i for i in result["issues"] if i["code"] == "malformed-pom"]
            self.assertTrue(malformed)
            self.assertTrue(all(i["commit"] == bad_sha for i in malformed))
            self.assertNotEqual("complete", result["coverage"]["status"])
            self.assert_json_safe(result)


class UnobservedPathTest(unittest.TestCase, JsonSafeMixin):
    def test_path_never_seen_in_any_analyzed_snapshot_is_flagged(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["ghost/pom.xml"])
            self.assertEqual([], result["events"])
            codes = [i["code"] for i in result["issues"]]
            self.assertIn("pom-path-unobserved", codes)


class BoundsTest(unittest.TestCase, JsonSafeMixin):
    def test_tiny_output_cap_aborts_listing_with_issue(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"],
                                     max_output_bytes=256)
            self.assertEqual([], result["events"])
            self.assertTrue(any("max_output_bytes" in i["detail"] for i in result["issues"]))
            self.assertNotEqual("complete", result["coverage"]["status"])


class ArgumentValidationTest(unittest.TestCase):
    def test_bad_pom_paths_are_rejected_before_any_git_run(self):
        bad_paths = ["../evil.xml", "/abs/pom.xml", "a/../../b.xml", "", "a//b.xml",
                     "app/./pom.xml", ":magic/pom.xml", "a\x00b"]
        with mock.patch.object(history, "checkout_context", side_effect=AssertionError("must not run")):
            for paths in bad_paths:
                with self.assertRaises(ValueError, msg=repr(paths)):
                    inspect_history("/tmp", "com.example:lib-b", pom_paths=[paths])
            with self.assertRaises(ValueError):
                inspect_history("/tmp", "com.example:lib-b", pom_paths=[])
            with self.assertRaises(TypeError):
                inspect_history("/tmp", "com.example:lib-b", pom_paths="app/pom.xml")

    def test_bad_numeric_and_query_arguments_are_rejected(self):
        with dependency_history_fixture() as fx:
            for kwargs in ({"max_commits": 0}, {"max_commits": -1}, {"max_commits": True},
                           {"timeout_seconds": 0}, {"timeout_seconds": 1.5},
                           {"max_output_bytes": -1}, {"max_output_bytes": True}):
                with self.assertRaises((TypeError, ValueError), msg=repr(kwargs)):
                    inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"], **kwargs)
            for query in ("", "   ", "a:b:c:d", "com.example:", ":lib-b", "com.example::1.0"):
                with self.assertRaises(ValueError, msg=repr(query)):
                    inspect_history(fx.repo_dir, query, pom_paths=["app/pom.xml"])
            with self.assertRaises(TypeError):
                inspect_history(fx.repo_dir, "com.example:lib-c", pom_paths=["app/pom.xml"],
                                introducing_dependencies=(42,))
            with self.assertRaises(ValueError):
                inspect_history(fx.repo_dir, "com.example:lib-c", pom_paths=["app/pom.xml"],
                                introducing_dependencies=("a:b:c:d",))

    def test_symlinked_checkout_is_rejected(self):
        with dependency_history_fixture() as fx:
            with tempfile.TemporaryDirectory(prefix="mdi-history-link-") as tmp:
                link = Path(tmp) / "link"
                link.symlink_to(fx.repo_dir)
                result = inspect_history(link, "com.example:lib-b", pom_paths=["app/pom.xml"])
                self.assertEqual([], result["events"])
                self.assertIsNone(result["revision"])
                self.assertTrue(result["issues"])
                self.assertNotEqual("complete", result["coverage"]["status"])


class LiteralPathspecTest(unittest.TestCase, JsonSafeMixin):
    def test_bracket_directory_path_is_matched_literally(self):
        with dependency_history_fixture() as fx:
            bracket = "app/[x]/pom.xml"
            sha = commit(fx, "Add literal bracket module pom", "2024-06-07T10:00:00+00:00",
                         {bracket: standalone_pom(lib_b_dep(version="1.0.0"))})
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=[bracket])
            self.assertEqual(1, result["coverage"]["commits_touching"])
            self.assertEqual(
                [(event_key(e), e["kind"], e["old_version"], e["new_version"]) for e in result["events"]],
                [(sha, "addition", None, "1.0.0")],
            )
            self.assertEqual([], result["issues"])
            self.assertEqual(sha, result["last_modification"]["file"]["commit"]["id"])
            self.assert_json_safe(result)

    def test_star_path_never_broadens_into_whole_repository_glob(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["*"])
            self.assertEqual([], result["events"])
            self.assertEqual(0, result["coverage"]["commits_touching"])
            self.assertIsNone(result["last_modification"]["file"])
            self.assertIsNone(result["last_modification"]["declaration"])
            self.assertIn("pom-path-unobserved", [i["code"] for i in result["issues"]])
            self.assert_json_safe(result)


class DeclarationFieldChangeTest(unittest.TestCase, JsonSafeMixin):
    def _inspect(self, fx):
        return inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])

    def _commit(self, fx, subject, dep, date):
        return commit(fx, subject, date,
                      {POM_RELPATH: _app_pom(dep=dep, comment=COMMENT, props=PROPS)})

    def test_scope_only_change_is_a_declaration_change_event(self):
        with dependency_history_fixture() as fx:
            first = self._commit(fx, "Scope lib-b to test", lib_b_dep(scope="test"),
                                 "2024-06-08T10:00:00+00:00")
            second = self._commit(fx, "Rescope lib-b to provided", lib_b_dep(scope="provided"),
                                  "2024-06-09T10:00:00+00:00")
            result = self._inspect(fx)
            self.assertEqual([row[1] for row in LIB_B_EVENTS] + ["declaration-change", "declaration-change"],
                             [e["kind"] for e in result["events"]])
            scoped, rescoped = result["events"][-2:]
            self.assertEqual((None, "test"), (scoped["old_scope"], scoped["new_scope"]))
            self.assertEqual(("test", "provided"), (rescoped["old_scope"], rescoped["new_scope"]))
            for event in (scoped, rescoped):
                self.assertEqual("${lib-b.version}", event["old_version"])
                self.assertEqual("${lib-b.version}", event["new_version"])
                self.assertIsNone(event["old_type"])
                self.assertIsNone(event["new_type"])
            self.assertEqual([first, second], [event_key(e) for e in result["events"][-2:]])
            self.assertEqual([], result["issues"])
            self.assertEqual("complete", result["coverage"]["status"])
            self.assert_json_safe(result)

    def test_optional_only_change_is_declaration_change_and_classifier_swaps_identity(self):
        with dependency_history_fixture() as fx:
            first = self._commit(fx, "Mark lib-b optional", lib_b_dep(optional="true"),
                                 "2024-06-10T10:00:00+00:00")
            second = self._commit(fx, "Swap optional for tests classifier",
                                  lib_b_dep(classifier="tests"), "2024-06-11T10:00:00+00:00")
            result = self._inspect(fx)
            self.assertEqual([row[1] for row in LIB_B_EVENTS]
                             + ["declaration-change", "removal", "addition"],
                             [e["kind"] for e in result["events"]])
            optional, removal, addition = result["events"][-3:]
            self.assertEqual((None, "true"), (optional["old_optional"], optional["new_optional"]))
            self.assertIsNone(optional["old_classifier"])
            self.assertEqual("lib-b", removal["matched_coordinates"]["artifact_id"])
            self.assertEqual("true", removal["old_optional"])
            self.assertIsNone(removal["new_optional"])
            self.assertEqual("lib-b", addition["matched_coordinates"]["artifact_id"])
            self.assertIsNone(addition["old_classifier"])
            self.assertEqual("tests", addition["new_classifier"])
            self.assertEqual([first, second, second], [event_key(e) for e in result["events"][-3:]])
            self.assertEqual([], result["issues"])
            self.assert_json_safe(result)


LIB_B_CORE_1 = _dep("com.example", "lib-b-core", "1.0.0")
LIB_B_API_15 = _dep("com.example", "lib-b-api", "1.5.0")
LIB_B_API_20 = _dep("com.example", "lib-b-api", "2.0.0")
LIB_B_1_LITERAL = _dep("com.example", "lib-b", "1.0.0")
LIB_B_2_LITERAL = _dep("com.example", "lib-b", "2.0.0")
LIB_B_3_LITERAL = _dep("com.example", "lib-b", "3.0.0")


class BareQueryIdentityTest(unittest.TestCase, JsonSafeMixin):
    def _commit_pom(self, fx, subject, date, dep):
        return commit(fx, subject, date, {POM_RELPATH: _app_pom(dep=dep, comment=COMMENT, props=PROPS)})

    def test_bare_query_multi_artifact_events_are_scoped_per_declaration(self):
        with dependency_history_fixture() as fx:
            core_sha = self._commit_pom(fx, "Add lib-b-core alongside lib-b",
                                        "2024-06-12T10:00:00+00:00", LIB_B_PROP + LIB_B_CORE_1)
            api_sha = self._commit_pom(fx, "Add lib-b-api consumer",
                                       "2024-06-13T10:00:00+00:00",
                                       LIB_B_PROP + LIB_B_CORE_1 + LIB_B_API_15)
            drop_sha = self._commit_pom(fx, "Drop lib-b-core",
                                        "2024-06-14T10:00:00+00:00", LIB_B_PROP + LIB_B_API_15)
            bump_sha = self._commit_pom(fx, "Upgrade lib-b-api to 2.0.0",
                                        "2024-06-15T10:00:00+00:00", LIB_B_PROP + LIB_B_API_20)
            result = inspect_history(fx.repo_dir, "lib-b", pom_paths=["app/pom.xml"])
            tail = result["events"][-4:]
            self.assertEqual(
                [(core_sha, "addition", None, "1.0.0", "lib-b-core"),
                 (api_sha, "addition", None, "1.5.0", "lib-b-api"),
                 (drop_sha, "removal", "1.0.0", None, "lib-b-core"),
                 (bump_sha, "version-change", "1.5.0", "2.0.0", "lib-b-api")],
                [(event_key(e), e["kind"], e["old_version"], e["new_version"],
                  e["matched_coordinates"]["artifact_id"]) for e in tail],
            )
            self.assertEqual(len(result["events"]), len(LIB_B_EVENTS) + 4)
            for event in result["events"]:
                coordinates = event["matched_coordinates"]
                self.assertIn(coordinates["artifact_id"], {"lib-b", "lib-b-core", "lib-b-api"})
                self.assertEqual("com.example", coordinates["group_id"])
            for key, _, _, _ in LIB_B_EVENTS:
                self.assertEqual("lib-b", commit_of(fx, result, key)["matched_coordinates"]["artifact_id"])
            self.assertEqual([], result["issues"])
            self.assertEqual("complete", result["coverage"]["status"])
            self.assert_json_safe(result)

    def test_duplicate_declarations_are_flagged_ambiguous_and_never_dropped(self):
        with dependency_history_fixture() as fx:
            dup_sha = self._commit_pom(fx, "Declare lib-b twice",
                                       "2024-06-16T10:00:00+00:00", LIB_B_PROP + LIB_B_PROP)
            split_sha = self._commit_pom(fx, "Split duplicate lib-b versions",
                                         "2024-06-17T10:00:00+00:00",
                                         LIB_B_1_LITERAL + LIB_B_2_LITERAL)
            swap_sha = self._commit_pom(fx, "Swap one duplicate lib-b to 3.0.0",
                                        "2024-06-18T10:00:00+00:00",
                                        LIB_B_1_LITERAL + LIB_B_3_LITERAL)
            result = inspect_history(fx.repo_dir, "lib-b", pom_paths=["app/pom.xml"])
            tail = result["events"][-3:]
            self.assertEqual([dup_sha, split_sha, swap_sha], [event_key(e) for e in tail])
            self.assertEqual(["addition", "declaration-change", "declaration-change"],
                             [e["kind"] for e in tail])
            self.assertTrue(all(e["ambiguous"] for e in tail))
            self.assertTrue(all(e["matched_coordinates"]["artifact_id"] == "lib-b" for e in tail))
            self.assertEqual((None, "${lib-b.version}"),
                             (tail[0]["old_version"], tail[0]["new_version"]))
            self.assertEqual("${lib-b.version}", tail[1]["old_version"])
            self.assertIsNone(tail[1]["new_version"])
            self.assertEqual(("2.0.0", "3.0.0"),
                             (tail[2]["old_version"], tail[2]["new_version"]))
            self.assertTrue(all(e.get("observed_values") for e in tail))
            self.assertEqual([], result["issues"])
            self.assert_json_safe(result)


class TimeoutPhaseTest(unittest.TestCase):
    def test_coverage_documents_separate_context_and_analysis_timeout_phases(self):
        with dependency_history_fixture() as fx:
            result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            note = result["coverage"]["limits"]["timeout_note"]
            self.assertIn("context phase", note)
            self.assertIn("analysis phase", note)
            self.assertIn("aggregate", note)
            self.assertIn("not bounded by a single", note)


class SeedFailureTest(unittest.TestCase, JsonSafeMixin):
    def test_head_seed_failure_is_explicit_issue_not_silent_suppression(self):
        with dependency_history_fixture() as fx:
            head = fx.commits["bom_upgrade"]
            real_run = history._Runner.run

            def failing_seed(self, command):
                if command[0] == "ls-tree" and head in command:
                    return _RunOutcome("error", 1, b"", b"", "simulated seed failure")
                return real_run(self, command)

            with mock.patch.object(history._Runner, "run", failing_seed):
                result = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=["app/pom.xml"])
            codes = [i["code"] for i in result["issues"]]
            self.assertIn("head-tree-read", codes)
            self.assertNotIn("pom-path-unobserved", codes)
            self.assertNotEqual("complete", result["coverage"]["status"])
            self.assertTrue(any(i["commit"] == head and i["pom"] == "app/pom.xml"
                                for i in result["issues"] if i["code"] == "head-tree-read"))
            self.assertEqual([row[1] for row in LIB_B_EVENTS], [e["kind"] for e in result["events"]])
            self.assert_json_safe(result)


class DuplicateMixedValueTest(unittest.TestCase, JsonSafeMixin):
    def _commit_pom(self, fx, subject, date, dep):
        return commit(fx, subject, date, {POM_RELPATH: _app_pom(dep=dep, comment=COMMENT, props=PROPS)})

    def test_duplicate_mixed_scope_change_and_partial_removal_no_exception(self):
        with dependency_history_fixture() as fx:
            mixed = self._commit_pom(fx, "Two lib-b one scoped",
                                     "2024-06-19T10:00:00+00:00",
                                     lib_b_dep(version="1.0.0", scope="test") + lib_b_dep(version="1.0.0"))
            rescoped = self._commit_pom(fx, "Rescope one duplicate lib-b",
                                        "2024-06-20T10:00:00+00:00",
                                        lib_b_dep(version="1.0.0", scope="provided") + lib_b_dep(version="1.0.0"))
            dropped = self._commit_pom(fx, "Drop the scoped duplicate",
                                       "2024-06-21T10:00:00+00:00", lib_b_dep(version="1.0.0"))
            result = inspect_history(fx.repo_dir, "lib-b", pom_paths=["app/pom.xml"])
            tail = result["events"][-3:]
            self.assertEqual([mixed, rescoped, dropped], [event_key(e) for e in tail])
            self.assertEqual(["declaration-change", "declaration-change", "removal"],
                             [e["kind"] for e in tail])
            self.assertTrue(all(e["ambiguous"] for e in tail))
            self.assertTrue(all(e["matched_coordinates"]["artifact_id"] == "lib-b" for e in tail))
            self.assertEqual(("${lib-b.version}", "1.0.0"),
                             (tail[0]["old_version"], tail[0]["new_version"]))
            self.assertEqual(("test", "provided"),
                             (tail[1]["old_scope"], tail[1]["new_scope"]))
            self.assertEqual(("1.0.0", "1.0.0"),
                             (tail[1]["old_version"], tail[1]["new_version"]))
            self.assertEqual(("provided", None),
                             (tail[2]["old_scope"], tail[2]["new_scope"]))
            self.assertEqual({"version": "1.0.0", "scope": None, "optional": None},
                             tail[0]["observed_values"]["added"][0])
            self.assertEqual({"version": "1.0.0", "scope": "test", "optional": None},
                             tail[0]["observed_values"]["added"][1])
            self.assertEqual([], result["issues"])
            self.assert_json_safe(result)

    def test_duplicate_mixed_optional_full_removal_no_exception(self):
        with dependency_history_fixture() as fx:
            mixed = self._commit_pom(fx, "Two lib-b one optional",
                                     "2024-06-22T10:00:00+00:00",
                                     lib_b_dep(version="1.0.0", optional="true") + lib_b_dep(version="1.0.0"))
            gone = self._commit_pom(fx, "Drop every lib-b declaration",
                                    "2024-06-23T10:00:00+00:00", "")
            result = inspect_history(fx.repo_dir, "lib-b", pom_paths=["app/pom.xml"])
            tail = result["events"][-2:]
            self.assertEqual([mixed, gone], [event_key(e) for e in tail])
            self.assertEqual(["declaration-change", "removal"], [e["kind"] for e in tail])
            self.assertTrue(all(e["ambiguous"] for e in tail))
            self.assertEqual(("${lib-b.version}", "1.0.0"),
                             (tail[0]["old_version"], tail[0]["new_version"]))
            self.assertEqual(("1.0.0", None),
                             (tail[1]["old_version"], tail[1]["new_version"]))
            self.assertEqual({"version": "1.0.0", "scope": None, "optional": None},
                             tail[1]["observed_values"]["removed"][0])
            self.assertEqual({"version": "1.0.0", "scope": None, "optional": "true"},
                             tail[1]["observed_values"]["removed"][1])
            self.assertEqual([], result["issues"])
            self.assert_json_safe(result)


class SeenBeforePerPathTest(unittest.TestCase, JsonSafeMixin):
    OTHER_POM = "other/pom.xml"

    def _other(self, dep):
        return standalone_pom(dep, artifact_id="other-app")

    def _commit_other(self, fx, subject, date, dep):
        return commit(fx, subject, date, {self.OTHER_POM: self._other(dep)})

    def test_first_addition_in_other_pom_stays_addition_when_queried_with_app(self):
        with dependency_history_fixture() as fx:
            added = self._commit_other(fx, "First lib-b in other module",
                                       "2024-06-24T10:00:00+00:00", lib_b_dep(version="1.0.0"))
            dropped = self._commit_other(fx, "Drop lib-b from other module",
                                         "2024-06-25T10:00:00+00:00", "")
            readded = self._commit_other(fx, "Bring lib-b back to other module",
                                         "2024-06-26T10:00:00+00:00", lib_b_dep(version="1.0.0"))
            together = inspect_history(fx.repo_dir, "com.example:lib-b",
                                       pom_paths=["app/pom.xml", self.OTHER_POM])
            tail = together["events"][-3:]
            self.assertEqual([added, dropped, readded], [event_key(e) for e in tail])
            self.assertEqual(["addition", "removal", "reintroduction"], [e["kind"] for e in tail])
            self.assertEqual([self.OTHER_POM] * 3, [e["pom"] for e in tail])
            self.assertTrue(all(e["matched_coordinates"]["artifact_id"] == "lib-b" for e in tail))
            self.assertEqual(len(together["events"]), len(LIB_B_EVENTS) + 3)
            self.assertEqual([], together["issues"])
            self.assertEqual("complete", together["coverage"]["status"])
            alone = inspect_history(fx.repo_dir, "com.example:lib-b", pom_paths=[self.OTHER_POM])
            self.assertEqual(["addition", "removal", "reintroduction"], [e["kind"] for e in alone["events"]])
            self.assertEqual([added, dropped, readded], [event_key(e) for e in alone["events"]])
            for field in ("kind", "pom", "old_version", "new_version", "matched_coordinates"):
                self.assertEqual(tail[0][field], alone["events"][0][field], field)
            self.assertEqual([self.OTHER_POM] * 3, [e["pom"] for e in alone["events"]])
            self.assert_json_safe(together)
            self.assert_json_safe(alone)


if __name__ == "__main__":
    unittest.main()
