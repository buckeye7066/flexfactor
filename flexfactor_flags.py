"""One resolver for cross-module rotation flags.

WHY THIS EXISTS
---------------
`FLEXFACTOR_ROTATION_EXTENSIONS` was read in FOUR places with THREE different
meanings: `flexfactor_rotation`, `flexfactor_discovery` and
`providers/cursor_provider` each required the exact string "1", while
`providers/cli_provider` accepted anything outside ("", "0", "false", "no").
So `FLEXFACTOR_ROTATION_EXTENSIONS=true` enabled the CLI adapter and left the
discovery lane that emits its routes switched OFF — a half-on state in which
the feature looks enabled and produces nothing.

That is the same registry-drift class this repo already documents (a value held
as a literal in a producer and a consumer, silently disagreeing while both
"work"). The fix is one function, imported by all four.

DEFAULT IS ON (owner order 2026-08-21: "make the CLI and Cursor pools visable
and usable"). Opting out is explicit. Deliberately NOT a CLI flag: both .ps1
launchers would need the same change in the same commit, and a stale flag is
argparse exit 2 — the launcher-drift trap that killed a five-program run.
"""
import os

_OFF = ("0", "false", "no", "off")


def rotation_extensions_enabled() -> bool:
    """True unless the owner explicitly turned extensions off.

    An UNSET variable means ON. Note this is the opposite of the usual
    `_env_falsy` convention elsewhere in the codebase, and that is intentional
    here: the extension routes are local, flat-rate transports the owner has
    asked to be in rotation by default.
    """
    return os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS", "").strip().lower() not in _OFF


def extensions_flag_source() -> str:
    """Human-readable provenance, so a run can say WHY extensions are on/off."""
    raw = os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS")
    if raw is None or not raw.strip():
        return "on (default; set FLEXFACTOR_ROTATION_EXTENSIONS=0 to disable)"
    state = "off" if raw.strip().lower() in _OFF else "on"
    return f"{state} (FLEXFACTOR_ROTATION_EXTENSIONS={raw.strip()!r})"
