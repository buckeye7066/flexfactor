#!/usr/bin/env python3
"""flexfactor_run.py — CLI entry that installs directed orchestration, then runs FlexFactor.

GitHub Contents API cannot safely rewrite the ~800KB flexfactor.py monolith in
one shot. This launcher loads flexfactor as a module, calls
flexfactor_directed.install(globals), then invokes main() so unfit-route
filtering, skip-dir failure paths, and _directed_work_theme_block are live
even when flexfactor.py itself has no install one-liner.

Usage (same argv as flexfactor.py):
  python flexfactor_run.py audit --program GrantFlow ...
  python flexfactor_run.py 3   # interactive menu path still works via main()
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_PATH = HERE / "flexfactor.py"
_SPEC = importlib.util.spec_from_file_location("flexfactor", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise SystemExit(f"cannot load {_PATH}")
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["flexfactor"] = _MOD
_SPEC.loader.exec_module(_MOD)

try:
    import flexfactor_directed as _ff_directed

    _ff_directed.install(_MOD.__dict__)
except Exception as exc:  # noqa: BLE001 — never block the CLI on helper install
    print(f"[flexfactor_run] directed install skipped: {exc}", file=sys.stderr)

if hasattr(_MOD, "_arm_death_instrumentation"):
    _MOD._arm_death_instrumentation()
try:
    rc = _MOD.main()
    if hasattr(_MOD, "_mark_run_finished"):
        _MOD._mark_run_finished()
    raise SystemExit(rc)
except SystemExit:
    if hasattr(_MOD, "_mark_run_finished"):
        _MOD._mark_run_finished()
    raise
