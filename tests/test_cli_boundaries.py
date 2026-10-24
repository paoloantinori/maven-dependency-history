"""In-process cli.main boundary tests for the offline CLI (TASK-23).

Closes the two tracked CLI test gaps. First, the deterministic mapping of
an unexpected internal exception: exit 4 with exactly one sanitized JSON
document on stdout, a fixed message with the exception type only, and never a
traceback, a source line or anything printed on stderr. Second, the
explicit refusal of oversized report persistence: no silent truncation, no
false success, the prior revisions and evidence byte-identical afterwards,
and nothing at all created when even revision 0 cannot be stored.

Every case runs ``mdi.cli.main`` in-process against the real RunStore and
the real filesystem inside a private temporary root. Only library
boundaries are patched with ``unittest.mock`` (the report validator and
``runstore.MAX_READ_BYTES``); each patch is restored on exit, so no module
state leaks into other tests. Module isolation follows
tests/test_cli_operations.py: the mdi package is imported once per class
from a private copy of the skill, with any previously imported mdi modules
saved aside first and restored on teardown. No network, no Maven, no Java,
no repository commits, nothing outside the temporary root.
"""

import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SKILL_SRC = REPO / "skills" / "maven-dependency-investigator"

NS = "http://maven.apache.org/POM/4.0.0"


def _pom(deps):
    rows = "".join(
        f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>"
        f"<version>{v}</version></dependency>"
        for g, a, v in deps
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<project xmlns="{NS}">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>org.example</groupId>\n"
        "  <artifactId>app</artifactId>\n"
        "  <version>1.0.0</version>\n"
        f"  <dependencies>\n{rows}\n  </dependencies>\n"
        "</project>\n"
    )


def make_project(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(
        _pom((("org.example", "b-lib", "1.0.0"),)), encoding="utf-8")
    return root


class BoundaryCliTestCase(unittest.TestCase):
    """In-process cli.main harness over a private copy of the skill.

    The mdi package is imported from the copied skill exactly once for the
    whole class; mdi modules already imported by earlier test modules are
    saved aside first and restored on teardown, so this class neither sees
    nor leaves foreign module state.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mdi-cli-bounds-tests-"))
        cls.skill = cls.tmp / "skill"
        shutil.copytree(SKILL_SRC, cls.skill,
                        ignore=shutil.ignore_patterns("__pycache__"))
        cls.scripts = cls.skill / "scripts"
        cls._saved_modules = {name: module for name, module in sys.modules.items()
                              if name == "mdi" or name.startswith("mdi.")}
        for name in cls._saved_modules:
            del sys.modules[name]
        sys.path.insert(0, str(cls.scripts))
        import mdi.cli
        import mdi.runstore
        cls.cli = mdi.cli
        cls.runstore = mdi.runstore

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.scripts))
        for name in [n for n in sys.modules if n == "mdi" or n.startswith("mdi.")]:
            del sys.modules[name]
        sys.modules.update(cls._saved_modules)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.tmp))
        self.state = self.work / "state"
        self.project = make_project(self.work / "proj")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def run_main(self, *args):
        """Run cli.main in-process; enforce the one-JSON-document contract
        and return (exit status, parsed document, raw stdout, stderr)."""
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main([str(a) for a in args])
        raw = out.getvalue()
        self.assertTrue(raw.endswith("\n"), f"stdout lacks its final newline: {raw!r}")
        self.assertEqual(raw.count("\n"), 1, f"stdout is not one line: {raw!r}")
        return code, json.loads(raw), raw, err.getvalue()

    def discover(self):
        code, doc, raw, err = self.run_main(
            "discover", "--repo", self.project,
            "--query", "org.example:b-lib:1.0.0",
            "--state-dir", self.state)
        self.assertEqual(code, 0, f"doc={doc!r} stderr={err!r}")
        self.assertFalse(doc["no_match"], doc)
        self.assertEqual(doc["revision"], 1, doc)
        return doc


class MemoryBackedLocationTests(BoundaryCliTestCase):
    """tmpfs or ramfs state, run and scratch directories are refused before IO.

    Archive spools and scratch written to a memory-backed filesystem consume
    RAM directly, up to the per-input and quota defaults; on a desktop where
    /tmp is tmpfs that competes with the session for memory.
    """

    TMPFS = 0x01021994
    RAMFS = 0x858458F6

    def probe(self, magic):
        import mdi.fsprobe
        return mock.patch.object(mdi.fsprobe, "fs_magic", return_value=magic)

    def test_probe_constants_are_the_documented_memory_filesystems(self):
        import mdi.fsprobe
        self.assertEqual(mdi.fsprobe.MEMORY_FS_MAGICS, {self.TMPFS, self.RAMFS})

    def test_discover_refuses_memory_backed_state_dir(self):
        with self.probe(self.TMPFS):
            code, doc, raw, err = self.run_main(
                "discover", "--repo", self.project,
                "--query", "org.example:b-lib:1.0.0",
                "--state-dir", self.work / "ram-state")
        self.assertEqual(code, 2, f"doc={doc!r} stderr={err!r}")
        self.assertIn("memory-backed", doc["error"]["message"])
        self.assertFalse((self.work / "ram-state").exists(),
                         "a refused state directory must not be created")

    def test_existing_state_dir_on_memory_backed_fs_is_refused(self):
        self.state.mkdir()
        with self.probe(self.TMPFS):
            code, doc, raw, err = self.run_main(
                "discover", "--repo", self.project,
                "--query", "org.example:b-lib:1.0.0",
                "--state-dir", self.state)
        self.assertEqual(code, 2, f"doc={doc!r} stderr={err!r}")
        self.assertIn("memory-backed", doc["error"]["message"])
        self.assertEqual(list(self.state.iterdir()), [],
                         "a refused existing state directory must stay untouched")

    def test_existing_run_on_memory_backed_fs_is_refused_before_open(self):
        run_dir = self.discover()["run_dir"]
        with self.probe(self.RAMFS):
            code, doc, raw, err = self.run_main("report", "--run-dir", run_dir)
        self.assertEqual(code, 2, f"doc={doc!r} stderr={err!r}")
        self.assertIn("memory-backed", doc["error"]["message"])

    def test_archive_scratch_funnel_guards_its_own_base(self):
        """A disk-backed run dir whose parent is memory-backed still refuses
        scratch creation: the collect-flow scratch base is dirname(run_dir)."""
        import mdi.fsprobe
        run_dir = self.discover()["run_dir"]
        base = os.path.dirname(os.path.abspath(run_dir))
        real = mdi.fsprobe.fs_magic

        def parent_is_tmpfs(path):
            return self.TMPFS if os.path.abspath(path) == base else real(path)

        with mock.patch.object(mdi.fsprobe, "fs_magic", side_effect=parent_is_tmpfs):
            with self.assertRaises(self.cli.CliError) as caught:
                with self.cli._archive_scratch(base):
                    self.fail("scratch must not be entered on a memory-backed base")
        self.assertIn("archive scratch base", str(caught.exception))
        self.assertFalse((Path(base) / "archive-scratch").exists())

    def test_real_tmpfs_state_dir_is_refused_when_tmp_is_tmpfs(self):
        import mdi.fsprobe
        if not mdi.fsprobe.is_memory_backed("/tmp"):
            self.skipTest("/tmp is not memory-backed on this machine")
        target = Path("/tmp") / f"mdi-guard-proof-{os.getpid()}"
        code, doc, raw, err = self.run_main(
            "discover", "--repo", self.project,
            "--query", "org.example:b-lib:1.0.0",
            "--state-dir", target)
        self.assertEqual(code, 2, f"doc={doc!r} stderr={err!r}")
        self.assertIn("memory-backed", doc["error"]["message"])
        self.assertFalse(target.exists(), "a refused state directory must not be created")


class UnexpectedFailureTests(BoundaryCliTestCase):
    """A deterministic unexpected exception must surface as exit 4 and one
    sanitized JSON document whose message is fixed, with nothing derived
    from the exception on any stream."""

    _CREDENTIAL = "https://ci:TOPSECRET-9f31c7-token@repo.example.invalid/nexus"

    def test_unexpected_exception_maps_to_exit_4_sanitized_document(self):
        run_dir = self.discover()["run_dir"]
        detonated = []

        def detonate(_report):
            detonated.append(True)
            # two leak vectors: the credential rides in the exception message
            # itself, and the marker below rides in this frame's source line,
            # which any printed traceback would reproduce verbatim
            raise RuntimeError(
                f"connection failed for {self._CREDENTIAL}")  # SECRET-FRAME-4b7d2e-never-emit

        with mock.patch.object(self.cli.report_module, "validate_report", detonate):
            code, body, raw, err = self.run_main("report", "--run-dir", run_dir,
                                                 "--validate")
        self.assertEqual(detonated, [True])
        self.assertEqual(code, 4)
        self.assertEqual(body, {
            "ok": False,
            "error": {"message": "unexpected internal failure: RuntimeError",
                      "exit_code": 4},
        })
        for stream in (raw, err):
            self.assertNotIn("TOPSECRET", stream)
            self.assertNotIn("repo.example.invalid", stream)
            self.assertNotIn("SECRET-FRAME-4b7d2e", stream)
            self.assertNotIn("Traceback", stream)
        self.assertEqual(err, "")

    def test_unexpected_exception_message_is_fixed_regardless_of_content(self):
        run_dir = self.discover()["run_dir"]
        noisy = "line-one\nline-two\t tabbed " + "x" * 5000 + " " + self._CREDENTIAL

        def detonate(_report):
            raise ValueError(noisy)

        with mock.patch.object(self.cli.report_module, "validate_report", detonate):
            code, body, raw, err = self.run_main("report", "--run-dir", run_dir,
                                                 "--validate")
        self.assertEqual(code, 4)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["exit_code"], 4)
        # the document is the same fixed shape for any exception content: no
        # filler, no whitespace noise, no credential, never the raw message
        self.assertEqual(body["error"]["message"],
                         "unexpected internal failure: ValueError")
        self.assertNotIn("xxxx", raw)
        self.assertNotIn("TOPSECRET", raw)
        self.assertEqual(err, "")


class OversizedPersistenceTests(BoundaryCliTestCase):
    """A report revision too large to store must fail explicitly: no silent
    truncation, no false success, and the run's prior state untouched."""

    def test_oversized_successor_revision_is_refused_without_truncation(self):
        doc = self.discover()
        run_dir = Path(doc["run_dir"])
        candidate = doc["candidates"][0]["id"]

        store = self.runstore.RunStore.open(str(run_dir))
        before = store.load()
        self.assertEqual(before["revision"], 1)
        snapshot = copy.deepcopy(before)
        rev1 = run_dir / "report-00000001.json"
        rev1_bytes = rev1.read_bytes()

        # a cap that still admits every revision on disk but sits below the
        # successor 'select' would persist: reads pass, the write cannot
        cap = rev1.stat().st_size + 8
        successor = copy.deepcopy(snapshot)
        successor["selection"] = {"candidate_ids": [candidate], "mode": "single"}
        successor_size = len(json.dumps(successor, sort_keys=True,
                                        allow_nan=False)) + 1
        self.assertGreater(successor_size, cap)

        with mock.patch.object(self.runstore, "MAX_READ_BYTES", cap):
            code, body, raw, err = self.run_main(
                "select", "--run-dir", run_dir,
                "--expect-revision", "1", "--candidate", candidate)
        self.assertEqual(code, 2, f"doc={body!r} stderr={err!r}")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["exit_code"], 2)
        self.assertIn("run store rejected the operation",
                      body["error"]["message"])
        self.assertIn(f"serialized revision 2 exceeds the {cap} byte read limit",
                      body["error"]["message"])
        self.assertEqual(err, "")

        # nothing was written and nothing was truncated: the exact prior
        # members, no revision 2, no leftover temporary, no writer lock
        self.assertEqual(sorted(os.listdir(run_dir)),
                         [".mdi-run.json", "report-00000000.json",
                          "report-00000001.json"])
        self.assertEqual(rev1.read_bytes(), rev1_bytes)

        # the run still opens, and its revision and evidence are unchanged
        after = self.runstore.RunStore.open(str(run_dir)).load()
        self.assertEqual(after, snapshot)
        self.assertEqual(after["revision"], 1)
        self.assertIsNone(after["selection"])

    def test_oversized_revision_zero_creates_nothing(self):
        with mock.patch.object(self.runstore, "MAX_READ_BYTES", 64):
            code, body, raw, err = self.run_main(
                "discover", "--repo", self.project,
                "--query", "org.example:b-lib:1.0.0",
                "--state-dir", self.state)
        self.assertEqual(code, 2, f"doc={body!r} stderr={err!r}")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["exit_code"], 2)
        self.assertIn("run store rejected the operation",
                      body["error"]["message"])
        self.assertIn("serialized revision 0 exceeds the 64 byte read limit",
                      body["error"]["message"])
        self.assertEqual(err, "")
        # revision 0 is size-gated before the run directory is created, so a
        # refusal leaves the state base empty: no partial run to clean up
        self.assertTrue(self.state.is_dir())
        self.assertEqual(os.listdir(self.state), [])


if __name__ == "__main__":
    unittest.main()
