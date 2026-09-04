#!/usr/bin/env python3
"""flexfactor_run.py - launcher/compatibility shim for the canonical runtime.

The canonical implementation remains ``flexfactor.run_cli``. This shim arms the
idempotent directed-runtime hooks and optional local Tenets prioritizer before
forwarding, so desktop/PowerShell launches get the same provider-capacity
admission, truthful partial-run status, and task-relevant file ordering without
duplicating audit logic here.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flexfactor as _flexfactor  # noqa: E402
from obsidian_memory import recall as _memory_recall, remember as _memory_remember  # noqa: E402
import flexfactor_directed as _directed  # noqa: E402
import flexfactor_tenets as _tenets  # noqa: E402

_directed.install(vars(_flexfactor))
_tenets.install(vars(_flexfactor), argv=sys.argv[1:])
run_cli = _flexfactor.run_cli

if __name__ == "__main__":
    # Executable recall prevents a fresh audit from re-discovering known defects.
    _memory_recall("audit purpose defects decisions blockers", limit=8)
    _exit_code = run_cli()
    if _exit_code:
        _memory_remember(
            "Run blocker",
            f"FlexFactor exited with status {_exit_code}. Inspect the run ledger and error evidence before retrying.",
            tag="blocker",
        )
    raise SystemExit(_exit_code)
