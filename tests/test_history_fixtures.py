"""Tests for tests.history_fixtures (TASK-21.2): deterministic Git history fixtures.

Every assertion reads the fixture repository through real git commands
(show, log, rev-list, config); the self-reported commits map is only ever
cross-checked against that output, never trusted on its own.
"""

import json
import os
import subprocess
import unittest
from datetime import datetime

from tests.history_fixtures import (
    APP_CODE_RELPATH,
    AUTHOR_EMAIL,
    AUTHOR_NAME,
    COMMITTER_EMAIL,
    COMMITTER_NAME,
    DATES,
    SUBJECTS,
    UPSTREAM_FILES,
    dependency_history_fixture,
    missing_history_fixture,
)


def added(delta):
    return {line[1:].strip() for line in delta.splitlines() if line.startswith("+") and not line.startswith("+++")}


def removed(delta):
    return {line[1:].strip() for line in delta.splitlines() if line.startswith("-") and not line.startswith("---")}


class HistoryFixtureTest(unittest.TestCase):
    def delta(self, fx, key, path):
        return fx.text("show", "--format=", "--unified=0", fx.commits[key], "--", path)

    def test_layout_paths_and_explicit_main_branch(self):
        with dependency_history_fixture() as fx:
            self.assertTrue(fx.data_root.is_dir())
            self.assertTrue((fx.repo_dir / ".git").is_dir())
            self.assertTrue((fx.repo_dir / "pom.xml").is_file())
            self.assertTrue((fx.repo_dir / fx.pom_relpath).is_file())
            self.assertEqual("app/pom.xml", fx.pom_relpath)
            self.assertEqual("pom.xml", fx.parent_pom_relpath)
            self.assertEqual("refs/heads/main", fx.text("symbolic-ref", "HEAD").strip())
            self.assertTrue(os.path.isabs(fx.git_binary))
            self.assertNotIn(str(fx.data_root), fx.git_binary)

    def test_commit_map_matches_real_git_history(self):
        with dependency_history_fixture() as fx:
            logged = fx.text("log", "--format=%H", "--reverse", "main").split()
            self.assertEqual([fx.commits[key] for key in fx.commit_order], logged)
            for key, sha in fx.commits.items():
                self.assertEqual(SUBJECTS[key], fx.text("log", "--format=%s", "-1", sha).strip())
                self.assertEqual(
                    datetime.fromisoformat(DATES[key]),
                    datetime.fromisoformat(fx.text("log", "--format=%aI", "-1", sha).strip()),
                )

    def test_initial_commit_has_parent_and_module_poms(self):
        with dependency_history_fixture() as fx:
            sha = fx.commits["initial_poms"]
            self.assertEqual(
                {"pom.xml", "app/pom.xml", APP_CODE_RELPATH},
                set(fx.text("show", "--format=", "--name-only", sha).split()),
            )
            self.assertIn("<module>app</module>", fx.show(sha, fx.parent_pom_relpath))
            self.assertNotIn("lib-b", fx.show(sha, fx.pom_relpath))

    def test_addition_commit_introduces_library_b(self):
        with dependency_history_fixture() as fx:
            plus = " ".join(added(self.delta(fx, "add_lib_b", fx.pom_relpath)))
            self.assertIn("<artifactId>lib-b</artifactId>", plus)
            self.assertIn("<version>1.0.0</version>", plus)
            self.assertNotIn("lib-b", fx.show(fx.commits["initial_poms"], fx.pom_relpath))

    def test_comment_and_code_only_delta_differs_from_introduction(self):
        with dependency_history_fixture() as fx:
            key = "comment_and_code_only"
            pom_delta = self.delta(fx, key, fx.pom_relpath)
            self.assertTrue(added(pom_delta))
            self.assertFalse(removed(pom_delta))
            self.assertNotIn("artifactId", pom_delta)
            self.assertNotIn("<version>", pom_delta)
            self.assertIn("hello, world", self.delta(fx, key, APP_CODE_RELPATH))
            code_history = fx.text("log", "--format=%H", "--", APP_CODE_RELPATH).split()
            self.assertEqual(fx.commits[key], code_history[0])
            self.assertNotIn(fx.commits["add_lib_b"], code_history)
            self.assertNotEqual(fx.commits[key], fx.commits["add_lib_b"])

    def test_removal_and_reintroduction_are_distinct_commits(self):
        with dependency_history_fixture() as fx:
            gone = removed(self.delta(fx, "remove_lib_b", fx.pom_relpath))
            back = added(self.delta(fx, "reintroduce_lib_b", fx.pom_relpath))
            original = added(self.delta(fx, "add_lib_b", fx.pom_relpath))
            self.assertIn("<artifactId>lib-b</artifactId>", " ".join(gone))
            self.assertIn("<artifactId>lib-b</artifactId>", " ".join(back))
            self.assertEqual(original, back)
            self.assertEqual(3, len({fx.commits["add_lib_b"], fx.commits["remove_lib_b"], fx.commits["reintroduce_lib_b"]}))

    def test_version_only_upgrade_keeps_artifact_and_hides_c(self):
        with dependency_history_fixture() as fx:
            delta = self.delta(fx, "upgrade_lib_b_2", fx.pom_relpath)
            changed = added(delta) | removed(delta)
            self.assertIn("<version>1.0.0</version>", " ".join(removed(delta)))
            self.assertIn("<version>2.0.0</version>", " ".join(added(delta)))
            for line in changed:
                self.assertNotIn("<artifactId>", line)
            for sha in fx.commits.values():
                self.assertNotIn("lib-c", fx.show(sha, fx.pom_relpath))

    def test_property_and_bom_upgrade_leads(self):
        with dependency_history_fixture() as fx:
            app = fx.show(fx.commits["property_version_bump"], fx.pom_relpath)
            self.assertIn("${lib-b.version}", app)
            self.assertIn("<lib-b.version>3.0.0</lib-b.version>", app)
            self.assertIn(
                "<version>2.0.0</version>",
                " ".join(removed(self.delta(fx, "property_version_bump", fx.pom_relpath))),
            )
            self.assertEqual(
                ["pom.xml"],
                fx.text("show", "--format=", "--name-only", fx.commits["bom_upgrade"]).split(),
            )
            bom_delta = self.delta(fx, "bom_upgrade", fx.parent_pom_relpath)
            self.assertEqual({"<version>1.0.0</version>"}, removed(bom_delta))
            self.assertEqual({"<version>2.0.0</version>"}, added(bom_delta))
            bom_file = fx.show(fx.commits["bom_upgrade"], fx.parent_pom_relpath)
            self.assertIn("<artifactId>corp-bom</artifactId>", bom_file)
            self.assertIn("<scope>import</scope>", bom_file)

    def test_upstream_snapshots_are_candidate_metadata_only(self):
        with dependency_history_fixture() as fx:
            self.assertTrue(fx.upstream_dir.is_dir())
            self.assertFalse(fx.upstream_dir.is_relative_to(fx.repo_dir))
            self.assertLessEqual(UPSTREAM_FILES, {path.name for path in fx.upstream_dir.iterdir()})
            old = (fx.upstream_dir / "lib-b-1.0.0.pom").read_text()
            new = (fx.upstream_dir / "lib-b-2.0.0.pom").read_text()
            self.assertNotIn("lib-c", old)
            self.assertIn("<artifactId>lib-c</artifactId>", new)
            blob = json.dumps(json.loads((fx.upstream_dir / "snapshots.json").read_text())).lower()
            self.assertIn("candidate", blob)
            self.assertNotIn('"resolved": true', blob)
            self.assertNotIn('"shipped": true', blob)
            self.assertNotIn("upstream", fx.text("ls-files"))

    def test_expected_event_descriptions_cover_history(self):
        with dependency_history_fixture() as fx:
            self.assertEqual(set(fx.commit_order), set(fx.events))
            self.assertEqual(list(SUBJECTS), list(fx.commit_order))
            for key, description in fx.events.items():
                self.assertTrue(description.strip(), key)
            upgrade = fx.events["upgrade_lib_b_2"].lower()
            self.assertIn("candidate", upgrade)
            self.assertIn("never named", upgrade)
            self.assertIn("not a resolved", upgrade)
            self.assertIn("candidate lead", fx.events["property_version_bump"].lower())
            self.assertIn("candidate lead", fx.events["bom_upgrade"].lower())

    def test_author_committer_identities_and_fixed_dates(self):
        with dependency_history_fixture() as fx:
            rows = fx.text("log", "--format=%an|%ae|%cn|%ce", "main").splitlines()
            expected = f"{AUTHOR_NAME}|{AUTHOR_EMAIL}|{COMMITTER_NAME}|{COMMITTER_EMAIL}"
            self.assertEqual({expected}, set(rows))
            self.assertNotEqual(AUTHOR_NAME, COMMITTER_NAME)
            self.assertEqual(8, len(rows))

    def test_deterministic_ids_across_two_runs(self):
        with dependency_history_fixture() as first, dependency_history_fixture() as second:
            self.assertEqual(first.commit_order, second.commit_order)
            self.assertEqual(first.commits, second.commits)
            for key, sha in first.commits.items():
                self.assertEqual(
                    first.text("rev-parse", f"{sha}:{first.pom_relpath}"),
                    second.text("rev-parse", f"{second.commits[key]}:{second.pom_relpath}"),
                )

    def test_isolation_from_hostile_environment(self):
        poison = {
            "GIT_AUTHOR_NAME": "Leaky Author",
            "GIT_COMMITTER_NAME": "Leaky Committer",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "leaky config",
            "GIT_CONFIG_GLOBAL": "/nonexistent/leak-config",
            "GIT_TEMPLATE_DIR": "/nonexistent/leak-templates",
        }
        saved = {key: os.environ.get(key) for key in poison}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ.update(poison)
        with dependency_history_fixture() as fx:
            self.assertEqual("Alice Author", fx.text("log", "--format=%an", "-1").strip())
            self.assertNotIn("Leaky", repr(fx.env))
            self.assertNotEqual(poison["GIT_CONFIG_GLOBAL"], fx.env["GIT_CONFIG_GLOBAL"])
            self.assertNotEqual(os.path.expanduser("~"), fx.env["HOME"])
            self.assertTrue(fx.env["HOME"].startswith(str(fx.data_root)))
            self.assertTrue(fx.text("config", "core.hooksPath").startswith(str(fx.data_root)))
            self.assertEqual("false", fx.text("config", "commit.gpgsign").strip())
            self.assertEqual("0", fx.text("config", "gc.auto").strip())
            hostile = fx.repo_dir / ".git" / "hooks"
            hostile.mkdir(parents=True, exist_ok=True)
            hook = hostile / "pre-commit"
            hook.write_text("#!/bin/sh\ntouch HOOK_RAN\nexit 1\n")
            hook.chmod(0o755)
            (fx.repo_dir / "EXTRA.txt").write_text("extra\n")
            fx.run("add", "-A")
            fx.run("commit", "--quiet", "-m", "extra commit outside the map")
            self.assertFalse((fx.repo_dir / "HOOK_RAN").exists())
            self.assertEqual("9", fx.text("rev-list", "--count", "main").strip())
            fx.run("config", "core.hooksPath", str(hostile))
            (fx.repo_dir / "EXTRA2.txt").write_text("blocked\n")
            fx.run("add", "-A")
            blocked = fx.run("commit", "--quiet", "-m", "must not land", check=False)
            self.assertNotEqual(0, blocked.returncode)
            self.assertTrue((fx.repo_dir / "HOOK_RAN").exists())
            self.assertEqual("9", fx.text("rev-list", "--count", "main").strip())

    def test_missing_history_helper_and_cleanup(self):
        with missing_history_fixture() as fx:
            self.assertFalse((fx.repo_dir / ".git").exists())
            self.assertEqual({}, fx.commits)
            self.assertEqual((), fx.commit_order)
            self.assertTrue((fx.repo_dir / fx.pom_relpath).is_file())
            with self.assertRaises(subprocess.CalledProcessError):
                fx.run("rev-parse", "--git-dir")
            missing_root = fx.data_root
        self.assertFalse(missing_root.exists())
        with dependency_history_fixture() as fx:
            live_root = fx.data_root
        self.assertFalse(live_root.exists())


if __name__ == "__main__":
    unittest.main()
