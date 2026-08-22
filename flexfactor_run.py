#!/usr/bin/env python3
"""flexfactor_run.py - COMPATIBILITY SHIM. The canonical entry is flexfactor.run_cli.

Directed orchestration used to be monkey-patched in here because the monolith
could not be rewritten through the GitHub Contents API. It is now part of the
runtime itself (flexfactor.py hard-imports flexfactor_directed), so this file
only forwards to the same entry point the installed `flexfactor` command and
`python -m flexfactor` use. It MUST NOT alter any runtime guarantee.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flexfactor import run_cli  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_cli())
