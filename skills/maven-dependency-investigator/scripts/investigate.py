#!/usr/bin/env python3
"""Entry point for the offline Maven dependency investigator CLI.

Thin launcher only: it puts this script's directory on sys.path so the
bundled ``mdi`` package imports from the skill itself, then delegates to
``mdi.cli.main``. All behavior, exit codes and the one-JSON-document
contract are documented in ``mdi/cli.py``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from mdi.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
