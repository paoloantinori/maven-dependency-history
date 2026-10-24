"""Tests for mdi.gitinspect checkout-context capture (TASK-20 slice)."""

import contextlib
import gc
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import gitinspect
from tests import child_env


class GitInspectTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mdi-gitinspect-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def git(self, repo, *args):
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.tmp),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        prefix = [
            shutil.which("git"),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
        ]
        return subprocess.run(
            prefix + list(args),
            cwd=str(repo),
            env=env,
            check=True,
            capture_output=True,
            timeout=60,
        )

    def make_repo_at(self, directory):
        directory.mkdir(parents=True)
        self.git(directory, "init", "-q")
        (directory / "pom.xml").write_text("<project><name>fixture</name></project>\n")
        self.git(directory, "add", "pom.xml")
        self.git(directory, "commit", "-qm", "synthetic fixture only")
        return directory

    def make_repo(self, name="repo"):
        return self.make_repo_at(self.tmp / name)

    def arm_pid_cleanup(self, pidfile):
        """Best-effort direct kill of a pid recorded by a shim, so spawned
        descendants are cleaned up even when the module under test fails."""

        def cleanup():
            try:
                pid = int(pidfile.read_text().split()[0])
            except (OSError, ValueError, IndexError):
                return
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        self.addCleanup(cleanup)

    def head_revision(self, repo):
        return self.git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    def patch_git(self, value):
        patcher = mock.patch.object(gitinspect, "_resolve_git", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_helper(self, repo, marker, name="hostile.sh"):
        helper = repo / name
        helper.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\nexit 1\n')
        helper.chmod(stat.S_IRWXU)
        return helper

    def configure_hostile(self, repo, marker):
        helper = self.write_helper(repo, marker)
        for key, value in (
            ("diff.external", str(helper)),
            ("diff.hostile.textconv", str(helper)),
            ("core.fsmonitor", str(helper)),
            ("core.pager", str(helper)),
            ("filter.hostile.clean", str(helper)),
            ("filter.hostile.smudge", str(helper)),
            ("filter.hostile.process", str(helper)),
        ):
            self.git(repo, "config", key, value)
        (repo / ".gitattributes").write_text("*.xml diff=hostile filter=hostile\n")
        return helper

    def setenv_temp(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value

        def restore():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

        self.addCleanup(restore)

    def assert_dead(self, pid, budget=5.0):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"process {pid} still alive after kill budget")

    @contextlib.contextmanager
    def cwd_at(self, path):
        previous = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)


class ArgumentValidationTest(GitInspectTestCase):
    def test_repo_type_rejected(self):
        for bad in (123, None, True, 3.5, b"/tmp"):
            with self.assertRaises(TypeError, msg=repr(bad)):
                gitinspect.checkout_context(bad)

    def test_timeout_validation(self):
        repo = self.make_repo()
        for bad in (True, 1.5, "5", None):
            with self.assertRaises(TypeError, msg=repr(bad)):
                gitinspect.checkout_context(repo, timeout_seconds=bad)
        for bad in (0, -1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                gitinspect.checkout_context(repo, timeout_seconds=bad)

    def test_max_output_bytes_validation(self):
        repo = self.make_repo()
        for bad in (True, 1.5, "5", None):
            with self.assertRaises(TypeError, msg=repr(bad)):
                gitinspect.checkout_context(repo, max_output_bytes=bad)
        for bad in (0, -1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                gitinspect.checkout_context(repo, max_output_bytes=bad)

    def test_missing_path_reports_unavailable(self):
        result = gitinspect.checkout_context(self.tmp / "nope")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(result["issues"])


class CleanRepoTest(GitInspectTestCase):
    def test_complete_clean_checkout(self):
        repo = self.make_repo()
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["repo"], str(repo))
        self.assertEqual(result["revision"], self.head_revision(repo))
        self.assertFalse(result["dirty"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["tool"]["name"], "git")
        self.assertTrue(Path(result["tool"]["path"]).is_absolute())
        self.assertTrue(Path(result["tool"]["path"]).is_file())
        json.loads(json.dumps(result))
        observed = datetime.fromisoformat(result["observed_at"])
        self.assertIsNotNone(observed.tzinfo)

    def test_relative_input_normalized_to_absolute(self):
        repo = self.make_repo()
        with self.cwd_at(self.tmp):
            result = gitinspect.checkout_context(repo.name)
        self.assertEqual(result["repo"], str(repo))
        self.assertEqual(result["status"], "complete")


class DirtyRepoTest(GitInspectTestCase):
    def test_tracked_modification_and_untracked_file(self):
        repo = self.make_repo()
        (repo / "pom.xml").write_text("<project><name>changed</name></project>\n")
        (repo / "extra.txt").write_text("untracked\n")
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["dirty"])
        self.assertEqual(result["revision"], self.head_revision(repo))

    def test_untracked_file_alone_counts_as_dirty(self):
        repo = self.make_repo()
        (repo / "untracked.bin").write_bytes(b"\x00")
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["dirty"])


class UnbornRepoTest(GitInspectTestCase):
    def test_unborn_reports_partial_with_explicit_issue(self):
        repo = self.tmp / "unborn"
        repo.mkdir()
        self.git(repo, "init", "-q")
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["revision"])
        self.assertFalse(result["dirty"])
        self.assertTrue(any("HEAD" in issue for issue in result["issues"]))


class NotARepoTest(GitInspectTestCase):
    def test_plain_directory_unavailable(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        result = gitinspect.checkout_context(plain)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("not a git repository" in issue for issue in result["issues"]))


class BareRepoTest(GitInspectTestCase):
    def test_bare_unavailable(self):
        bare = self.tmp / "bare.git"
        self.git(self.tmp, "init", "-q", "--bare", "bare.git")
        result = gitinspect.checkout_context(bare)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("work tree" in issue for issue in result["issues"]))


class MissingToolTest(GitInspectTestCase):
    def test_missing_git_reports_unavailable(self):
        repo = self.make_repo()
        self.patch_git(None)
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["tool"]["path"])
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("git" in issue for issue in result["issues"]))


class UnsupportedFlagTest(GitInspectTestCase):
    def test_unsupported_flag_fails_once_without_retry(self):
        counter = self.tmp / "calls"
        shim = self.tmp / "oldgit"
        shim.write_text(
            "#!/bin/sh\n"
            f'printf x >> "{counter}"\n'
            "echo \"error: unknown option \\`no-lazy-fetch'\" >&2\n"
            "exit 129\n"
        )
        shim.chmod(stat.S_IRWXU)
        self.patch_git(str(shim))
        result = gitinspect.checkout_context(self.make_repo())
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("lazy-fetch" in issue for issue in result["issues"]))
        self.assertEqual(counter.read_text(), "x")


class NonRootRejectedTest(GitInspectTestCase):
    def test_subdirectory_rejected_without_ancestor_inspection(self):
        repo = self.make_repo()
        nested = repo / "nested"
        nested.mkdir()
        result = gitinspect.checkout_context(nested)
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("checkout root" in issue for issue in result["issues"]))


class SymlinkRejectionTest(GitInspectTestCase):
    def test_symlinked_repo_root_rejected(self):
        repo = self.make_repo()
        link = self.tmp / "link"
        link.symlink_to(repo)
        result = gitinspect.checkout_context(link)
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("symlink" in issue for issue in result["issues"]))

    def test_symlinked_ancestor_rejected(self):
        repo = self.make_repo()
        holder = self.tmp / "holder"
        holder.mkdir()
        exposed = holder / "exposed"
        exposed.symlink_to(self.tmp, target_is_directory=True)
        result = gitinspect.checkout_context(exposed / repo.name)
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("symlink" in issue for issue in result["issues"]))


class RepoWrapperRefusalTest(GitInspectTestCase):
    def test_git_inside_inspected_repo_refused(self):
        repo = self.make_repo()
        wrapper = repo / "git"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        wrapper.chmod(stat.S_IRWXU)
        self.patch_git(str(wrapper))
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("inside" in issue for issue in result["issues"]))


class HostileEnvironmentTest(GitInspectTestCase):
    def test_hostile_config_and_injected_env_run_no_helpers(self):
        repo = self.make_repo()
        marker = self.tmp / "unexpected-helper"
        self.configure_hostile(repo, marker)
        (repo / "pom.xml").write_text("<project><name>after</name></project>\n")
        expected = self.head_revision(repo)
        index = repo / ".git" / "index"
        index_before = hashlib.sha256(index.read_bytes()).hexdigest()
        hostile_global = self.tmp / "hostile.gitconfig"
        helper = repo / "hostile.sh"
        hostile_global.write_text(f"[core]\n\tpager = {helper}\n[diff]\n\texternal = {helper}\n")
        for key, value in (
            ("GIT_DIR", str(self.tmp / "decoy.git")),
            ("GIT_WORK_TREE", str(self.tmp / "decoy-worktree")),
            ("GIT_INDEX_FILE", str(self.tmp / "decoy-index")),
            ("GIT_CONFIG_GLOBAL", str(hostile_global)),
            ("GIT_CONFIG_COUNT", "2"),
            ("GIT_CONFIG_KEY_0", "core.fsmonitor"),
            ("GIT_CONFIG_VALUE_0", str(helper)),
            ("GIT_CONFIG_KEY_1", "core.pager"),
            ("GIT_CONFIG_VALUE_1", str(helper)),
            ("GIT_PAGER", str(helper)),
            ("PAGER", str(helper)),
        ):
            self.setenv_temp(key, value)
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["revision"], expected)
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("filter" in issue for issue in result["issues"]))
        self.assertFalse(marker.exists())
        self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), index_before)


class FakeChildBoundTest(GitInspectTestCase):
    def make_shim(self, body):
        shim = self.tmp / "gitshim"
        shim.write_text(f"#!/bin/sh\n{body}\n")
        shim.chmod(stat.S_IRWXU)
        return shim

    def read_pid(self, pidfile, budget=5.0):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if pidfile.exists():
                return int(pidfile.read_text().strip())
            time.sleep(0.05)
        self.fail(f"pid file {pidfile} never appeared")

    def test_output_capped_and_process_group_killed(self):
        pidfile = self.tmp / "pid"
        shim = self.make_shim(
            f'printf \'%s\\n\' $$ > "{pidfile}"\n'
            "while :; do printf '%1024s' x; printf '%1024s' y >&2; done"
        )
        self.arm_pid_cleanup(pidfile)
        self.patch_git(str(shim))
        repo = self.make_repo()
        started = time.monotonic()
        result = gitinspect.checkout_context(repo, timeout_seconds=20, max_output_bytes=4096)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("output" in issue for issue in result["issues"]))
        self.assertTrue(any("SIGKILL sent to process group" in issue for issue in result["issues"]))
        self.assert_dead(self.read_pid(pidfile))

    def test_timeout_kills_and_reaps_process_group(self):
        pidfile = self.tmp / "pid"
        shim = self.make_shim(f'printf \'%s\\n\' $$ > "{pidfile}"\nsleep 60')
        self.arm_pid_cleanup(pidfile)
        self.patch_git(str(shim))
        repo = self.make_repo()
        started = time.monotonic()
        result = gitinspect.checkout_context(repo, timeout_seconds=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 30)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("timed out" in issue for issue in result["issues"]))
        self.assert_dead(self.read_pid(pidfile))

    def test_descendant_killed_when_leader_exits_holding_pipes(self):
        pidfile = self.tmp / "sleeper-pid"
        shim = self.make_shim(
            f'sleep 300 &\nprintf \'%s\\n\' "$!" > "{pidfile}"\nexit 0'
        )
        self.arm_pid_cleanup(pidfile)
        self.patch_git(str(shim))
        repo = self.make_repo()
        started = time.monotonic()
        result = gitinspect.checkout_context(repo, timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(any("timed out" in issue for issue in result["issues"]))
        self.assertTrue(any("SIGKILL sent to process group" in issue for issue in result["issues"]))
        self.assert_dead(self.read_pid(pidfile))

    def test_kill_failure_not_claimed_as_killed(self):
        pidfile = self.tmp / "pid"
        shim = self.make_shim(f'printf \'%s\\n\' $$ > "{pidfile}"\nexec sleep 60')
        self.arm_pid_cleanup(pidfile)
        self.patch_git(str(shim))
        repo = self.make_repo()
        real_killpg = os.killpg

        def kill_then_refuse(*args):
            real_killpg(*args)
            raise PermissionError("denied")

        with mock.patch.object(gitinspect.os, "killpg", side_effect=kill_then_refuse):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = gitinspect.checkout_context(repo, timeout_seconds=1)
            gc.collect()
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(any("timed out" in issue for issue in result["issues"]))
        self.assertTrue(any("process group termination not confirmed" in issue for issue in result["issues"]))
        self.assertFalse(any("SIGKILL sent to process group" in issue for issue in result["issues"]))
        child_pid = str(self.read_pid(pidfile))
        leaked = [
            warning
            for warning in caught
            if issubclass(warning.category, ResourceWarning)
            and "still running" in str(warning.message)
            and child_pid in str(warning.message)
        ]
        self.assertEqual(leaked, [], "module left its own child unreaped")
        self.assert_dead(self.read_pid(pidfile))


class PathExactnessTest(GitInspectTestCase):
    def test_long_total_path_handled_exactly(self):
        repo = self.make_repo_at(self.tmp / ("d" * 200) / ("e" * 150))
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["repo"], str(repo))
        self.assertEqual(result["revision"], self.head_revision(repo))
        self.assertFalse(result["dirty"])

    def test_embedded_newline_path_handled_exactly(self):
        repo = self.make_repo_at(self.tmp / "we\nird")
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["repo"], str(repo))
        self.assertEqual(result["revision"], self.head_revision(repo))
        self.assertFalse(result["dirty"])


class RevisionShapeTest(GitInspectTestCase):
    def test_non_hex_revision_rejected_with_issue(self):
        repo = self.make_repo()
        shim = self.tmp / "badsha"
        shim.write_text(
            "#!/bin/sh\n"
            f'case "$*" in\n'
            f'  *--show-toplevel*) printf \'%s\\n\' "{repo}"; exit 0;;\n'
            "  *--verify*) printf 'not-a-sha\\n'; exit 0;;\n"
            "  *config*) exit 1;;\n"
            "  *status*) exit 0;;\n"
            "esac\n"
            "exit 0\n"
        )
        shim.chmod(stat.S_IRWXU)
        self.patch_git(str(shim))
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "partial", msg=str(result["issues"]))
        self.assertIsNone(result["revision"])
        self.assertFalse(result["dirty"])
        self.assertTrue(any("unexpected shape" in issue for issue in result["issues"]))


class RelativePathToolTest(GitInspectTestCase):
    def test_relative_path_component_resolved_in_parent_cwd(self):
        repo = self.make_repo()
        hostile_dir = repo / "tools"
        hostile_dir.mkdir()
        hostile = hostile_dir / "git"
        hostile.write_text(f'#!/bin/sh\nprintf hostile > "{self.tmp}/hostile-marker"\nexit 1\n')
        hostile.chmod(stat.S_IRWXU)
        benign_dir = self.tmp / "tools"
        benign_dir.mkdir()
        benign = benign_dir / "git"
        benign.write_text(
            "#!/bin/sh\n"
            f'printf benign >> "{self.tmp}/benign-marker"\n'
            f'printf \'%s\\n\' "$PATH" > "{self.tmp}/path-dump"\n'
            "exit 1\n"
        )
        benign.chmod(stat.S_IRWXU)
        self.setenv_temp("PATH", "tools")
        with self.cwd_at(self.tmp):
            result = gitinspect.checkout_context(repo)
        self.assertEqual(result["tool"]["path"], str(benign))
        self.assertTrue(Path(result["tool"]["path"]).is_absolute())
        self.assertTrue((self.tmp / "benign-marker").exists())
        self.assertFalse((self.tmp / "hostile-marker").exists())
        child_path = (self.tmp / "path-dump").read_text().strip()
        for component in child_path.split(os.pathsep):
            self.assertTrue(os.path.isabs(component), repr(component))


class DoubleSlashRootTest(GitInspectTestCase):
    def test_double_slash_root_no_hang_identity_preserved(self):
        repo = self.make_repo()
        doubled = "/" + str(repo)
        probe = (
            "import resource, sys\n"
            "resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "from mdi.gitinspect import checkout_context\n"
            "import json\n"
            "print(json.dumps(checkout_context(sys.argv[1])))\n"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", probe, doubled],
                capture_output=True,
                text=True,
                timeout=15,
                start_new_session=True,
                env=child_env(),
            )
        except subprocess.TimeoutExpired:
            self.fail("checkout_context hung on a //-prefixed path")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["repo"], doubled)
        self.assertIn(result["status"], ("rejected", "complete"))
        if result["status"] == "rejected":
            self.assertTrue(any("checkout root" in issue for issue in result["issues"]))


class FilterExecutionRegressionTest(GitInspectTestCase):
    """Regressions for in-repo clean/process filter drivers: git status hashes
    stat-dirty tracked files through them, executing repo-configured commands.
    A checkout with any filter.* driver configured must fail closed on dirty
    rather than execute, while filter-free checkouts keep full status."""

    def make_filtered_repo(self, config_key, helper):
        repo = self.make_repo_at(self.tmp / "repo")
        self.git(repo, "config", config_key, str(helper))
        (repo / ".gitattributes").write_text("*.xml filter=hostile\n")
        (repo / "pom.xml").write_text("<project><name>unchanged</name></project>\n")
        self.git(repo, "add", "pom.xml", ".gitattributes")
        self.git(repo, "commit", "-qm", "filtered fixture")
        return repo

    def mtime_touch(self, repo):
        moment = time.time() + 2
        os.utime(repo / "pom.xml", (moment, moment))

    def test_mtime_only_touch_with_clean_filter_fails_closed(self):
        marker = self.tmp / "filter-marker"
        helper = self.tmp / "clean-filter.sh"
        helper.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\ncat\n')
        helper.chmod(stat.S_IRWXU)
        repo = self.make_filtered_repo("filter.hostile.clean", helper)
        marker.unlink()
        expected = self.head_revision(repo)
        self.mtime_touch(repo)
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["revision"], expected)
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("filter" in issue for issue in result["issues"]))
        self.assertFalse(marker.exists())

    def test_mtime_only_touch_with_process_filter_fails_closed(self):
        marker = self.tmp / "process-marker"
        helper = self.tmp / "process-filter.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            f'open("{marker}", "w").write("invoked")\n'
        )
        helper.chmod(stat.S_IRWXU)
        repo = self.make_filtered_repo("filter.hostile.process", helper)
        if marker.exists():
            marker.unlink()
        expected = self.head_revision(repo)
        self.mtime_touch(repo)
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["revision"], expected)
        self.assertIsNone(result["dirty"])
        self.assertTrue(any("filter" in issue for issue in result["issues"]))
        self.assertFalse(marker.exists())

    def test_mtime_only_touch_without_filters_reports_clean(self):
        repo = self.make_repo_at(self.tmp / "repo")
        expected = self.head_revision(repo)
        self.mtime_touch(repo)
        result = gitinspect.checkout_context(repo)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["revision"], expected)
        self.assertFalse(result["dirty"])
        self.assertEqual(result["issues"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
