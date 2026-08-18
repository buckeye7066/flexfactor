"""Production-readiness engine for FlexFactor: detect, bootstrap, assess.

The rubric body lives in flexfactor_prodready_engine.py (verbatim main).
This module is the public import path and attaches the GrantFlow Factory
Deck persistence gates (PR #1266 / SHA 3060385) onto assess_readiness.

Stdlib only. Never imports flexfactor. Never launches a subprocess.
"""
from __future__ import annotations

from flexfactor_prodready_engine import *  # noqa: F403
from flexfactor_prodready_engine import (
    Gate,
    _tracked_files,
    assess_readiness as _assess_readiness_core,
)


def assess_readiness(project_dir, toolchains, run,
                     build_ok=None, tests_ok=None):
    """Deterministic rubric, plus the five high persistence gates."""
    gates = _assess_readiness_core(
        project_dir, toolchains, run,
        build_ok=build_ok, tests_ok=tests_ok)
    from flexfactor_prodready_persist import apply_persistence_gates
    extras = []

    def add(**kw):
        extras.append(Gate(**kw))

    apply_persistence_gates(add, project_dir, _tracked_files(project_dir, run))
    return list(gates) + extras
