"""Verbatim production-readiness engine body (main blob ed3b443).

Split into two part files only so GitHub MCP can upload the original
module without truncating it. exec() keeps one namespace — this is not
a second rubric.
"""
from __future__ import annotations

from pathlib import Path

_d = Path(__file__).resolve().parent
_src = (_d / "flexfactor_prodready_engine.part1").read_text(encoding="utf-8")
_src += (_d / "flexfactor_prodready_engine.part2").read_text(encoding="utf-8")
exec(_src, globals())
