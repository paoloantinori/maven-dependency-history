"""Tests for mdi.runstore: create/open/load and the commit/locking slice."""

import copy
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import runstore
from tests import child_env

QUERY = "org.apache.commons:commons-lang3:3.12.0"


def _evidence_record(record_id="E1"):
    return {
        "id": record_id,
        "category": "tree-json",
        "source_locator": "file:///fixtures/tree.json",
        "observed_at": "2026-09-11T00:00:00Z",
        "payload": {"root": "org.apache.commons"},
    }


def _analysis_record(evidence_id="E1"):
    return {
        "id": "A1",
        "evidence_ids": [evidence_id],
        "statement_kind": "observation-summary",
        "limitations": [],
        "statement": "commons-lang3 appears in the resolved tree",
    }


def _evidence_draft(store):
    draft = copy.deepcopy(store.load())
    draft["evidence"].append(_evidence_record())
    draft["analyses"].append(_analysis_record())
    return draft


def _proposal_draft(store, operation_id="OP1"):
    draft = copy.deepcopy(store.load())
    draft["operations"].append(
        {
            "id": operation_id,
            "run_id": store.run_id,
            "state": "proposed",
            "scope": {"kind": "dependency-tree"},
            "input_identities": [],
            "tool_identity": {"tool": "mdi-probe"},
            "disclosure": "reads local build metadata only",
            "audit": {},
            "evidence_ids": [],
        }
    )
    return draft


def _consumed_draft(report, operation_id="OP1"):
    draft = copy.deepcopy(report)
    for operation in draft["operations"]:
        if operation["id"] == operation_id:
            operation["state"] = "consumed"
            operation["audit"]["started"] = "2026-09-11T00:00:01Z"
    return draft


def _members(store):
    return sorted(os.listdir(store.path))


def _temp_leftovers(store):
    return [name for name in os.listdir(store.path) if name.startswith(".report-")]


def _write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _rewrite_json(path, payload):
    _write(path, json.dumps(payload))


def _snapshot(path):
    info = os.stat(path)
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_fd_count():
    return len(os.listdir("/proc/self/fd"))


class RunStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_create_and_load_roundtrip(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        reopened = runstore.RunStore.open(store.path)
        self.assertEqual(reopened.path, store.path)
        self.assertEqual(reopened.run_id, store.run_id)
        first = reopened.load()
        self.assertEqual(first["query"], QUERY)
        self.assertEqual(first["revision"], 0)
        self.assertEqual(first["run_id"], store.run_id)
        first["query"] = "mutated"
        again = reopened.load()
        self.assertEqual(again["query"], QUERY)
        self.assertIsNot(first, again)

    def test_create_layout_uuid_and_modes(self):
        store = runstore.RunStore.create(str(self.base))
        run_dir = Path(store.path)
        self.assertEqual(run_dir.name, store.run_id)
        self.assertEqual(str(uuid.UUID(store.run_id)), store.run_id)
        self.assertEqual(stat.S_IMODE(os.stat(run_dir).st_mode), 0o700)
        marker_path = run_dir / runstore.MARKER_NAME
        report_path = run_dir / runstore.REPORT_NAME
        self.assertEqual(stat.S_IMODE(os.stat(marker_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(report_path).st_mode), 0o600)
        self.assertEqual(
            json.loads(marker_path.read_text(encoding="utf-8")),
            {"format": 1, "run_id": store.run_id},
        )
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["run_id"], store.run_id)

    def test_create_normalizes_lexical_alias_path(self):
        spelled = os.path.join(str(self.base), ".", "sub", "..") + os.sep
        store = runstore.RunStore.create(spelled)
        self.assertEqual(store.path, os.path.join(str(self.base), store.run_id))
        previous = os.getcwd()
        os.chdir(self.base)
        try:
            relative = runstore.RunStore.create(".")
        finally:
            os.chdir(previous)
        self.assertEqual(relative.path, os.path.join(str(self.base), relative.run_id))

    def test_create_rejects_symlinked_base(self):
        real = self.base / "real"
        real.mkdir()
        alias = self.base / "alias"
        os.symlink(real, alias)
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.create(str(alias))
        self.assertEqual(os.listdir(real), [])

    def test_open_rejects_unrelated_directory(self):
        plain = self.base / "not-a-run"
        plain.mkdir()
        (plain / "sentinel").write_text("keep me", encoding="utf-8")
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.open(str(plain))
        uuid_leaf = self.base / str(uuid.uuid4())
        uuid_leaf.mkdir()
        (uuid_leaf / "sentinel").write_text("keep me", encoding="utf-8")
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.open(str(uuid_leaf))
        self.assertTrue((uuid_leaf / "sentinel").exists())

    def test_rejects_symlinked_paths_and_members(self):
        store = runstore.RunStore.create(str(self.base))
        run_dir = Path(store.path)
        leaf_alias = self.base / "leaf-alias"
        os.symlink(run_dir, leaf_alias)
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.open(str(leaf_alias))
        base_alias = self.base / "base-alias"
        os.symlink(self.base, base_alias)
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.open(str(base_alias / store.run_id))
        for name in (runstore.MARKER_NAME, runstore.REPORT_NAME):
            member = run_dir / name
            kept = run_dir / (name + ".kept")
            os.rename(member, kept)
            os.symlink(kept.name, member)
            with self.assertRaises(runstore.RunStoreError):
                runstore.RunStore.open(store.path).load()
            os.unlink(member)
            os.rename(kept, member)

    def test_open_rejects_malformed_marker(self):
        store = runstore.RunStore.create(str(self.base))
        marker = Path(store.path) / runstore.MARKER_NAME
        other = str(uuid.uuid4())
        cases = [
            "not json {",
            '{"format": 1, "format": 2, "run_id": "%s"}' % store.run_id,
            '{"format": 1, "run_id": NaN}',
            '{"format": 2, "run_id": "%s"}' % store.run_id,
            '{"format": true, "run_id": "%s"}' % store.run_id,
            '{"format": 1}',
            '{"format": 1, "run_id": "%s", "extra": 0}' % store.run_id,
            json.dumps({"format": 1, "run_id": other}),
        ]
        for text in cases:
            with self.subTest(marker=text):
                _write(marker, text)
                with self.assertRaises(runstore.RunStoreError):
                    runstore.RunStore.open(store.path)

    def test_load_rejects_invalid_report(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        report_path = Path(store.path) / runstore.REPORT_NAME
        other = str(uuid.uuid4())
        cases = []
        dropped = store.load()
        del dropped["stages"]
        cases.append(dropped)
        bumped = store.load()
        bumped["revision"] = 1
        cases.append(bumped)
        mismatched = store.load()
        mismatched["run_id"] = other
        cases.append(mismatched)
        for payload in cases:
            with self.subTest(report=payload):
                _rewrite_json(report_path, payload)
                with self.assertRaises(runstore.RunStoreError):
                    store.load()

    def test_open_rejects_missing_report(self):
        store = runstore.RunStore.create(str(self.base))
        report = Path(store.path) / runstore.REPORT_NAME
        kept = Path(store.path) / "report.kept"
        os.rename(report, kept)
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.open(store.path)
        os.rename(kept, report)
        self.assertEqual(runstore.RunStore.open(store.path).run_id, store.run_id)

    def test_load_rejects_oversized_report(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        limit = os.path.getsize(os.path.join(store.path, runstore.MARKER_NAME)) + 1
        with mock.patch.object(runstore, "MAX_READ_BYTES", limit):
            with self.assertRaises(runstore.RunStoreError):
                store.load()

    def test_read_rejects_fifo_without_blocking(self):
        store = runstore.RunStore.create(str(self.base))
        for name in (runstore.MARKER_NAME, runstore.REPORT_NAME):
            with self.subTest(member=name):
                member = Path(store.path) / name
                kept = Path(store.path) / (name + ".kept")
                os.rename(member, kept)
                os.mkfifo(member)
                code = (
                    "import sys\n"
                    f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
                    "from mdi import runstore\n"
                    f"runstore.RunStore.open({store.path!r}).load()\n"
                )
                try:
                    result = subprocess.run(
                        [sys.executable, "-c", code],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        env=child_env(),
                    )
                except subprocess.TimeoutExpired:
                    self.fail(f"open/load blocked on FIFO member {name}")
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("RunStoreError", result.stderr)
                os.unlink(member)
                os.rename(kept, member)

    def test_create_collision_does_not_overwrite(self):
        first = runstore.RunStore.create(str(self.base))
        marker = Path(first.path) / runstore.MARKER_NAME
        report = Path(first.path) / runstore.REPORT_NAME
        before = (marker.read_bytes(), report.read_bytes())
        with mock.patch.object(runstore.uuid, "uuid4", return_value=uuid.UUID(first.run_id)):
            with self.assertRaises(runstore.RunStoreError):
                runstore.RunStore.create(str(self.base))
        self.assertEqual((marker.read_bytes(), report.read_bytes()), before)
        self.assertEqual(first.load()["run_id"], first.run_id)

    def test_create_rejects_oversized_revision_zero_without_creating_a_run(self):
        query = "g:a:" + "a" * 600
        with mock.patch.object(runstore, "MAX_READ_BYTES", 512):
            with self.assertRaises(runstore.RunStoreError) as caught:
                runstore.RunStore.create(str(self.base), query=query)
        self.assertIn("byte read limit", str(caught.exception))
        self.assertEqual(os.listdir(self.base), [])

    def test_create_failure_leaves_identifiable_partial_run(self):
        real_write = runstore._write_new_file
        calls = []

        def fail_on_report(dir_fd, name, data):
            calls.append(name)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real_write(dir_fd, name, data)

        with mock.patch.object(runstore, "_write_new_file", side_effect=fail_on_report):
            with self.assertRaises(runstore.RunStoreError):
                runstore.RunStore.create(str(self.base))
        children = list(self.base.iterdir())
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(str(uuid.UUID(child.name)), child.name)
        self.assertEqual(
            (child / runstore.MARKER_NAME).read_text(encoding="utf-8"),
            json.dumps({"format": 1, "run_id": child.name}, sort_keys=True) + "\n",
        )
        self.assertFalse((child / runstore.REPORT_NAME).exists())

    def test_create_control_flow_exception_propagates_and_partial_stays(self):
        before = _open_fd_count()
        real_write = runstore._write_new_file
        calls = []

        def interrupt_on_report(dir_fd, name, data):
            calls.append(name)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_write(dir_fd, name, data)

        with mock.patch.object(runstore, "_write_new_file", side_effect=interrupt_on_report):
            with self.assertRaises(KeyboardInterrupt):
                runstore.RunStore.create(str(self.base))
        children = list(self.base.iterdir())
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(str(uuid.UUID(child.name)), child.name)
        self.assertTrue((child / runstore.MARKER_NAME).exists())
        self.assertFalse((child / runstore.REPORT_NAME).exists())
        self.assertEqual(_open_fd_count(), before)

    def test_read_errors_normalize_to_runstore_error_with_fd_closed(self):
        store = runstore.RunStore.create(str(self.base))
        for call in ("fstat", "read"):
            with self.subTest(call=call):
                before = _open_fd_count()
                with mock.patch.object(runstore.os, call, side_effect=OSError(errno.EIO, "I/O error")):
                    with self.assertRaises(runstore.RunStoreError) as caught:
                        store.load()
                self.assertIsInstance(caught.exception.__cause__, OSError)
                self.assertEqual(caught.exception.__cause__.errno, errno.EIO)
                self.assertEqual(_open_fd_count(), before)

    def test_create_requires_existing_base_directory(self):
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.create(str(self.base / "missing"))
        plain_file = self.base / "file"
        plain_file.write_text("data", encoding="utf-8")
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.create(str(plain_file))
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.create(None)

    def test_open_and_load_write_nothing(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        run_dir = Path(store.path)
        members = sorted(os.listdir(run_dir))
        before = {name: _snapshot(run_dir / name) for name in members}
        dir_before = _snapshot(run_dir)
        runstore.RunStore.open(store.path).load()
        store.load()
        self.assertEqual(sorted(os.listdir(run_dir)), members)
        self.assertEqual({name: _snapshot(run_dir / name) for name in members}, before)
        self.assertEqual(_snapshot(run_dir), dir_before)

    def test_missing_primitives_fail_closed(self):
        store = runstore.RunStore.create(str(self.base))
        for missing in (None, 0):
            for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_EXCL"):
                with self.subTest(flag=flag, missing=missing):
                    with mock.patch.object(os, flag, missing):
                        self.assertRaises(runstore.RunStoreError, runstore.RunStore.open, store.path)
                        self.assertRaises(runstore.RunStoreError, runstore.RunStore.create, str(self.base))
        with mock.patch.object(os, "supports_dir_fd", frozenset()):
            self.assertRaises(runstore.RunStoreError, runstore.RunStore.open, store.path)
            self.assertRaises(runstore.RunStoreError, runstore.RunStore.create, str(self.base))
        with mock.patch.object(os, "supports_dir_fd", {os.open}):
            self.assertRaises(runstore.RunStoreError, runstore.RunStore.open, store.path)
            self.assertRaises(runstore.RunStoreError, runstore.RunStore.create, str(self.base))


class RunStoreCommitTest(unittest.TestCase):
    """Commit, writer-lock, chain-discovery, and failure-policy behaviour."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_commit_appends_revision_and_loads_history(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        revision_zero = Path(store.path) / runstore.REPORT_NAME
        original_bytes = revision_zero.read_bytes()
        persisted = store.commit(_evidence_draft(store), 0)
        self.assertEqual(persisted["revision"], 1)
        self.assertEqual(persisted["run_id"], store.run_id)
        self.assertEqual(revision_zero.read_bytes(), original_bytes)
        revision_one = Path(store.path) / "report-00000001.json"
        self.assertEqual(stat.S_IMODE(os.stat(revision_one).st_mode), 0o600)
        self.assertEqual(json.loads(revision_one.read_text(encoding="utf-8"))["revision"], 1)
        latest = store.load()
        self.assertEqual(latest["revision"], 1)
        self.assertEqual(latest["evidence"][0]["id"], "E1")
        historical = store.load(0)
        self.assertEqual(historical["revision"], 0)
        self.assertEqual(historical["evidence"], [])
        reopened = runstore.RunStore.open(store.path)
        self.assertEqual(reopened.load()["revision"], 1)
        self.assertEqual(reopened.load(0)["revision"], 0)

    def test_commit_returns_deepcopy_and_leaves_caller_draft_fresh(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        draft = _evidence_draft(store)
        persisted = store.commit(draft, 0)
        self.assertIsNot(persisted, draft)
        persisted["evidence"][0]["category"] = "mutated"
        persisted["analyses"].clear()
        self.assertEqual(store.load()["evidence"][0]["category"], "tree-json")
        self.assertEqual(len(store.load()["analyses"]), 1)
        self.assertEqual(draft["revision"], 0)
        draft["query"] = "mutated"
        self.assertEqual(store.load()["query"], QUERY)

    def test_commit_invalid_expected_revision_and_draft_arguments(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        draft = _evidence_draft(store)
        for bad in (True, False, -1, "0", 1.0, None):
            with self.subTest(expected_revision=bad):
                with self.assertRaises(runstore.RunStoreError):
                    store.commit(draft, bad)
        for bad_draft in (None, "draft", ["list"]):
            with self.subTest(draft=bad_draft):
                with self.assertRaises(runstore.RunStoreError):
                    store.commit(bad_draft, 0)
        self.assertEqual(_members(store), sorted([runstore.MARKER_NAME, runstore.REPORT_NAME]))

    def test_commit_stale_expected_and_draft_revision_mismatch(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        store.commit(_evidence_draft(store), 0)
        with self.assertRaises(runstore.RunConflict):
            store.commit(_evidence_draft(store), 0)
        fresh = runstore.RunStore.create(str(self.base), query=QUERY)
        with self.assertRaises(runstore.RunConflict):
            fresh.commit(_evidence_draft(fresh), 1)
        mismatch = _evidence_draft(fresh)
        mismatch["revision"] = 7
        with self.assertRaises(runstore.RunStoreError) as caught:
            fresh.commit(mismatch, 0)
        self.assertIn("revision", str(caught.exception))
        self.assertEqual(fresh.load()["revision"], 0)

    def test_commit_rejects_transition_mutations_without_publication(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        store.commit(_evidence_draft(store), 0)
        latest = store.load()
        mutated_query = copy.deepcopy(latest)
        mutated_query["query"] = "org.other:artifact:1.0"
        dropped = copy.deepcopy(latest)
        dropped["evidence"] = []
        non_finite = copy.deepcopy(latest)
        non_finite["evidence"][0]["payload"] = {"ratio": float("nan")}
        illegal_state = copy.deepcopy(latest)
        illegal_state["operations"].append(
            {
                "id": "OP9",
                "run_id": store.run_id,
                "state": "consumed",
                "scope": {},
                "input_identities": [],
                "tool_identity": {},
                "disclosure": "unused",
                "audit": {},
                "evidence_ids": [],
            }
        )
        cases = {
            "mutated query": mutated_query,
            "dropped evidence": dropped,
            "non-finite payload": non_finite,
            "new operation not proposed": illegal_state,
        }
        for name, draft in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(runstore.RunStoreError):
                    store.commit(draft, 1)
        self.assertEqual(store.load()["revision"], 1)
        self.assertEqual(
            _members(store),
            sorted([runstore.MARKER_NAME, runstore.REPORT_NAME, "report-00000001.json"]),
        )
        self.assertEqual(_temp_leftovers(store), [])

    def test_commit_blocked_by_existing_stale_or_symlinked_lock(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        draft = _evidence_draft(store)
        lock_path = os.path.join(store.path, runstore.LOCK_NAME)
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"nonce": str(uuid.uuid4()), "pid": 4194304}))
        with self.assertRaises(runstore.RunConflict):
            store.commit(draft, 0)
        with open(lock_path, encoding="utf-8") as handle:
            self.assertIn("4194304", handle.read())
        self.assertEqual(store.load()["revision"], 0)
        os.unlink(lock_path)
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"nonce": "stale", "pid": 1}))
        with self.assertRaises(runstore.RunConflict):
            store.commit(draft, 0)
        self.assertTrue(os.path.exists(lock_path))
        os.unlink(lock_path)
        target = os.path.join(store.path, "lock-target")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("target")
        os.symlink("lock-target", lock_path)
        with self.assertRaises(runstore.RunConflict):
            store.commit(draft, 0)
        self.assertTrue(os.path.islink(lock_path))

    def test_commit_closes_descriptors_and_releases_lock(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        before = _open_fd_count()
        store.commit(_evidence_draft(store), 0)
        self.assertEqual(_open_fd_count(), before)
        self.assertFalse(os.path.exists(os.path.join(store.path, runstore.LOCK_NAME)))
        with self.assertRaises(runstore.RunConflict):
            store.commit(_evidence_draft(store), 0)
        self.assertEqual(_open_fd_count(), before)
        self.assertFalse(os.path.exists(os.path.join(store.path, runstore.LOCK_NAME)))
        store.load()
        self.assertEqual(_open_fd_count(), before)

    def test_commit_interrupt_releases_lock_without_publication(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        before = _open_fd_count()
        with mock.patch.object(runstore, "validate_transition", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                store.commit(_evidence_draft(store), 0)
        self.assertEqual(_open_fd_count(), before)
        self.assertFalse(os.path.exists(os.path.join(store.path, runstore.LOCK_NAME)))
        self.assertEqual(store.load()["revision"], 0)

    def test_commit_interrupt_during_temp_write_leaves_tolerated_orphan(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        draft = _evidence_draft(store)
        before = _open_fd_count()
        real_write = os.write

        def write(fd, data):
            if len(data) > 200:
                raise KeyboardInterrupt
            return real_write(fd, data)

        with mock.patch.object(runstore.os, "write", side_effect=write):
            with self.assertRaises(KeyboardInterrupt):
                store.commit(draft, 0)
        self.assertEqual(_open_fd_count(), before)
        self.assertFalse(os.path.exists(os.path.join(store.path, runstore.LOCK_NAME)))
        self.assertEqual(store.load()["revision"], 0)
        orphans = _temp_leftovers(store)
        self.assertEqual(len(orphans), 1)
        store.commit(_evidence_draft(store), 0)
        self.assertEqual(store.load()["revision"], 1)
        self.assertTrue(os.path.exists(os.path.join(store.path, orphans[0])))

    def test_commit_destination_collision_sentinel_unchanged(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        draft = _evidence_draft(store)
        destination = os.path.join(store.path, "report-00000001.json")
        real_dumps = runstore._dumps

        def inject_destination(payload):
            with open(destination, "w", encoding="utf-8") as handle:
                handle.write("sentinel")
            return real_dumps(payload)

        with mock.patch.object(runstore, "_dumps", side_effect=inject_destination):
            with self.assertRaises(runstore.RunConflict):
                store.commit(draft, 0)
        with open(destination, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "sentinel")
        self.assertEqual(_temp_leftovers(store), [])
        self.assertFalse(os.path.exists(os.path.join(store.path, runstore.LOCK_NAME)))
        with self.assertRaises(runstore.RunStoreError):
            store.load()

    def test_commit_write_fault_cleans_own_temp(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        real_write = os.write

        def write(fd, data):
            if len(data) > 200:
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_write(fd, data)

        with mock.patch.object(runstore.os, "write", side_effect=write):
            with self.assertRaises(runstore.RunStoreError) as caught:
                store.commit(_evidence_draft(store), 0)
        self.assertNotIsInstance(caught.exception, runstore.CommitUncertain)
        self.assertEqual(store.load()["revision"], 0)
        self.assertEqual(_members(store), sorted([runstore.MARKER_NAME, runstore.REPORT_NAME]))

    def test_commit_temp_fsunc_fault_cleans_own_temp(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        real_fsync = os.fsync
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError(errno.EIO, "input/output error")
            return real_fsync(fd)

        with mock.patch.object(runstore.os, "fsync", side_effect=fsync):
            with self.assertRaises(runstore.RunStoreError) as caught:
                store.commit(_evidence_draft(store), 0)
        self.assertNotIsInstance(caught.exception, runstore.CommitUncertain)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(store.load()["revision"], 0)
        self.assertEqual(_members(store), sorted([runstore.MARKER_NAME, runstore.REPORT_NAME]))

    def test_commit_dir_fsunc_fault_is_commit_uncertain(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        store.commit(_proposal_draft(store), 0)
        consumed = _consumed_draft(store.load())
        real_fsync = os.fsync
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 3:
                raise OSError(errno.EIO, "input/output error")
            return real_fsync(fd)

        with mock.patch.object(runstore.os, "fsync", side_effect=fsync):
            with self.assertRaises(runstore.CommitUncertain) as caught:
                store.commit(consumed, 1)
        self.assertEqual(caught.exception.published_revision, 2)
        self.assertEqual(_temp_leftovers(store), [])
        latest = store.load()
        self.assertEqual(latest["revision"], 2)
        operation = latest["operations"][0]
        self.assertEqual(operation["state"], "consumed")
        self.assertEqual(operation["audit"]["started"], "2026-09-11T00:00:01Z")
        self.assertTrue(
            os.path.exists(os.path.join(store.path, "report-00000002.json"))
        )
        with self.assertRaises(runstore.RunConflict):
            store.commit(_consumed_draft(store.load()), 1)

    def test_commit_replaced_lock_left_intact_fails_conflict(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        lock_path = os.path.join(store.path, runstore.LOCK_NAME)
        real_validate = runstore.validate_transition

        def swap_lock(previous, current):
            os.unlink(lock_path)
            with open(lock_path, "w", encoding="utf-8") as handle:
                handle.write("sentinel lock")
            return real_validate(previous, current)

        with mock.patch.object(runstore, "validate_transition", side_effect=swap_lock):
            with self.assertRaises(runstore.RunConflict) as caught:
                store.commit(_evidence_draft(store), 0)
        self.assertIn("replaced", str(caught.exception))
        with open(lock_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "sentinel lock")
        self.assertEqual(store.load()["revision"], 1)

    def test_discovery_rejects_gaps_and_malformed_names(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        store.commit(_evidence_draft(store), 0)
        revision_one = os.path.join(store.path, "report-00000001.json")
        kept = os.path.join(store.path, "kept.json")
        os.rename(revision_one, kept)
        with self.assertRaises(runstore.RunStoreError):
            store.load()
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.open(store.path)
        with self.assertRaises(runstore.RunStoreError):
            store.load(0)
        os.rename(kept, revision_one)
        for name in (
            "report-1.json",
            "report-0000000x.json",
            "report-000000001.json",
            "report-00000001.json.bak",
            "report-00000002.json.tmp",
            ".report-not-a-uuid.tmp",
            "notes.txt",
            "raw-12345678-1234-1234-1234-123456789abc.bin",
            "raw-not-a-uuid.bin",
        ):
            with self.subTest(member=name):
                path = os.path.join(store.path, name)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("x")
                with self.assertRaises(runstore.RunStoreError):
                    store.load()
                os.unlink(path)
        self.assertEqual(store.load()["revision"], 1)

    def test_load_rejects_corrupt_foreign_and_reordered_revisions(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        store.commit(_evidence_draft(store), 0)
        store.commit(_proposal_draft(store), 1)
        revision_one = os.path.join(store.path, "report-00000001.json")
        revision_two = os.path.join(store.path, "report-00000002.json")
        with open(revision_one, encoding="utf-8") as handle:
            first_text = handle.read()
        with open(revision_two, encoding="utf-8") as handle:
            second_text = handle.read()
        _write(revision_one, "not json {")
        with self.assertRaises(runstore.RunStoreError):
            store.load()
        with self.assertRaises(runstore.RunStoreError):
            store.load(1)
        self.assertEqual(store.load(0)["revision"], 0)
        _write(revision_one, first_text)
        foreign = json.loads(first_text)
        foreign["run_id"] = str(uuid.uuid4())
        _rewrite_json(revision_one, foreign)
        with self.assertRaises(runstore.RunStoreError):
            store.load()
        _write(revision_one, first_text)
        _write(revision_one, second_text)
        _write(revision_two, first_text)
        with self.assertRaises(runstore.RunStoreError):
            store.load()
        _write(revision_one, first_text)
        _write(revision_two, second_text)
        self.assertEqual(store.load()["revision"], 2)

    def test_load_revision_argument_validation(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        store.commit(_evidence_draft(store), 0)
        for bad in (True, False, -1, 5, "0", 1.5):
            with self.subTest(revision=bad):
                with self.assertRaises(runstore.RunStoreError):
                    store.load(bad)
        self.assertEqual(store.load(0)["revision"], 0)
        self.assertEqual(store.load(1)["revision"], 1)
        self.assertEqual(store.load()["revision"], 1)
        self.assertIsNot(store.load(1), store.load(1))

    def test_revision_bound_enforced(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        with mock.patch.object(runstore, "MAX_REVISIONS", 1):
            self.assertEqual(store.load()["revision"], 0)
            with self.assertRaises(runstore.RunStoreError):
                store.commit(_evidence_draft(store), 0)
        store.commit(_evidence_draft(store), 0)
        with mock.patch.object(runstore, "MAX_REVISIONS", 2):
            self.assertEqual(store.load()["revision"], 1)
            with self.assertRaises(runstore.RunStoreError):
                store.commit(_proposal_draft(store), 1)
        with mock.patch.object(runstore, "MAX_REVISIONS", 1):
            with self.assertRaises(runstore.RunStoreError):
                runstore.RunStore.open(store.path)

    def test_symlinked_revision_member_rejected(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        store.commit(_evidence_draft(store), 0)
        revision_one = os.path.join(store.path, "report-00000001.json")
        kept = revision_one + ".kept"
        os.rename(revision_one, kept)
        os.symlink(os.path.basename(kept), revision_one)
        with self.assertRaises(runstore.RunStoreError):
            store.load()
        with self.assertRaises(runstore.RunStoreError):
            runstore.RunStore.open(store.path)
        os.unlink(revision_one)
        os.rename(kept, revision_one)
        self.assertEqual(store.load()["revision"], 1)

    def test_two_independent_runs_share_evidence_ids(self):
        first = runstore.RunStore.create(str(self.base), query=QUERY)
        second = runstore.RunStore.create(str(self.base), query=QUERY)
        first.commit(_evidence_draft(first), 0)
        second.commit(_evidence_draft(second), 0)
        self.assertEqual([record["id"] for record in first.load()["evidence"]], ["E1"])
        self.assertEqual([record["id"] for record in second.load()["evidence"]], ["E1"])
        self.assertNotEqual(first.run_id, second.run_id)
        first.commit(_proposal_draft(first), 1)
        self.assertEqual(len(second.load()["operations"]), 0)
        self.assertEqual(second.load()["revision"], 1)

    def test_commit_serialization_bound_rejects_oversized_draft(self):
        store = runstore.RunStore.create(str(self.base), query=QUERY)
        draft = _evidence_draft(store)
        revision_zero = os.path.getsize(os.path.join(store.path, runstore.REPORT_NAME))
        serialized = len((json.dumps(dict(draft, revision=1), sort_keys=True) + "\n").encode("utf-8"))
        self.assertGreater(serialized, revision_zero + 2)
        limit = revision_zero + (serialized - revision_zero) // 2
        with mock.patch.object(runstore, "MAX_READ_BYTES", limit):
            with self.assertRaises(runstore.RunStoreError):
                store.commit(draft, 0)
        self.assertEqual(_members(store), sorted([runstore.MARKER_NAME, runstore.REPORT_NAME]))



class RawSnapshotRaceTest(unittest.TestCase):
    """One directory snapshot must decide revisions AND raw membership.

    A capture file legitimately created between two separate listings used
    to be judged against the stale snapshot and falsely rejected; the fix
    derives both views from a single listing, so a mid-read creation is
    simply invisible to that read and authorized by the next one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = runstore.RunStore.create(self.tmp.name, query='race:lib:1')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                                / 'skills/maven-dependency-investigator/scripts'))
        from mdi import operations, raw_capture_consent, raw_capture_store
        self.operations = operations
        self.consent = raw_capture_consent
        self.capture_store = raw_capture_store

    def build_consumed_pair(self):
        source = self.operations.build_proposal(
            'maven', {'kind': 'maven', 'argv': ['/synthetic/mvn'], 'cwd': '/synthetic'},
            [], {'name': 'synthetic'}, 'No execution in this test.', self.store.run_id)
        self.operations.propose(self.store, source, 0)
        capture = self.consent.build_capture_proposal(
            self.store.load(), source['id'], max_bytes=4096,
            tool_identity=self.capture_store.TOOL_IDENTITY)
        self.operations.propose(self.store, capture, self.store.load()['revision'])
        approvals = {
            record['id']: dict(run_id=self.store.run_id, operation_id=record['id'],
                               scope=copy.deepcopy(record['scope']), approved=True,
                               approved_by='fixture', reference='race regression')
            for record in (source, capture)}
        self.consent.consume_pair(
            self.store, capture['id'], approvals[source['id']], approvals[capture['id']],
            observed_source_inputs=[], observed_source_tool={'name': 'synthetic'},
            observed_capture_inputs=[],
            observed_capture_tool=self.capture_store.TOOL_IDENTITY,
            expected_revision=self.store.load()['revision'])
        self.name = capture['scope']['output_name']
        self.path = Path(self.store.path) / self.name

    def test_raw_created_mid_read_is_invisible_not_rejected(self):
        self.build_consumed_pair()
        real_read = runstore._read_report

        def create_after_first_read(dir_fd, index, run_id):
            if index == 0 and not self.path.exists():
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, b'authorized bytes')
                os.close(fd)
            return real_read(dir_fd, index, run_id)

        with mock.patch.object(runstore, '_read_report', side_effect=create_after_first_read):
            report = self.store.load(1)
        self.assertEqual(report['revision'], 1)

    def test_unauthorized_raw_in_snapshot_still_rejected(self):
        self.build_consumed_pair()
        stranger = Path(self.store.path) / f"raw-{uuid.uuid4()}.bin"
        fd = os.open(stranger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b'unauthorized bytes')
        os.close(fd)
        with self.assertRaises(runstore.RunStoreError):
            self.store.load(1)

    def test_authorized_raw_in_snapshot_authorizes_historical_read(self):
        self.build_consumed_pair()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b'authorized bytes')
        os.close(fd)
        report = self.store.load(1)
        self.assertEqual(report['revision'], 1)


if __name__ == "__main__":
    unittest.main()
