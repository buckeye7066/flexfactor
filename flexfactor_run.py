#!/usr/bin/env python3
"""flexfactor_run.py - launcher/compatibility shim for the canonical runtime.

The canonical implementation remains ``flexfactor.run_cli``. This shim arms the
idempotent directed-runtime hooks before forwarding, so desktop/PowerShell
launches get the same provider-capacity admission and truthful partial-run status
semantics without duplicating audit logic here.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flexfactor as _flexfactor  # noqa: E402
import flexfactor_directed as _directed  # noqa: E402

_directed.install(vars(_flexfactor))
run_cli = _flexfactor.run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli())
