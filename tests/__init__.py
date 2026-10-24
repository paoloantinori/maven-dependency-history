"""Test suite for the maven-dependency-investigator skill."""

import os
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from mdi import fsprobe  # noqa: E402  (leaf module, no other mdi imports)


def child_env(extra=None):
    """Environment for every child process spawned by the tests.

    The graphical session variables are stripped so JVM tooling can never
    touch the live desktop (headless mode is automatic without DISPLAY) and
    no test child inherits the session by accident. Explicit per-site
    additions merge on top via ``extra``.
    """
    env = {name: value for name, value in os.environ.items()
           if name not in ("DISPLAY", "WAYLAND_DISPLAY")}
    if extra:
        env.update(extra)
    return env


def _prefer_disk_backed_tmp():
    """Keep spool and scratch fixtures off a RAM-backed temporary directory.

    Archive, staging and run-store tests materialize their private trees
    through the temporary directory. When it is tmpfs those bytes are RAM
    and a suite run competes with the desktop for memory. Prefer /var/tmp
    when it is disk-backed, or MDI_TEST_TMP_BASE when set.
    """
    base = os.environ.get("MDI_TEST_TMP_BASE")
    if (not base and fsprobe.is_memory_backed(tempfile.gettempdir())
            and not fsprobe.is_memory_backed("/var/tmp")):
        base = "/var/tmp"
    if base:
        os.makedirs(base, exist_ok=True)
        tempfile.tempdir = base
        os.environ["TMPDIR"] = base


_prefer_disk_backed_tmp()
