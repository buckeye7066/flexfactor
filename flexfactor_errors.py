"""Per-run ERROR LEDGER: what went wrong, which code is responsible, how to fix it.

Owner, 2026-08-23: "let there be a place in the run that shows me what errors
occurred, what code was responsible for the error, and a suggestion on how to
fix it" -- instead of a person watching logs.

Every entry carries:
  when / phase      -- where in the run it happened
  error             -- the exception (type + message) or the failure text
  responsible       -- FlexFactor file:line:function + that source line when
                       the stack passes through FlexFactor's own code; the
                       program's file when the failure points at the repo
                       (a failing test, a red build); the route id when a
                       provider failed
  kind              -- flexfactor-defect | program-defect | environment |
                       provider | budget | unknown
  suggestion        -- from a deterministic SIGNATURE table first (shapes this
                       toolchain has actually hit), else a model's suggestion
                       labelled 'model suggestion, unverified', else the
                       honest 'no known fix; see responsible code'

Written after EVERY record (atomic replace) to <run dir>/errors.json and
errors.md, and rendered into the audit report, so a crashed run still leaves
its ledger behind. Stdlib only; never imports flexfactor.
"""

from __future__ import annotations

import datetime as _dt
import json
import linecache
import os
import re
import sys
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

KIND_TOOL = "flexfactor-defect"
KIND_PROGRAM = "program-defect"
KIND_ENV = "environment"
KIND_PROVIDER = "provider"
KIND_BUDGET = "budget"
KIND_UNKNOWN = "unknown"

# (regex over "ExcType: message", kind, suggestion). First match wins. Keep
# each entry tied to a failure this toolchain has actually produced; a guess
# dressed as a signature is worse than the honest fallback below.
SIGNATURES: List[Tuple[str, str, str]] = [
    (r"unknown model architecture", KIND_ENV,
     "The installed Ollama predates this model. Upgrade Ollama (winget upgrade Ollama.Ollama) "
     "and make sure the G:\\Programs\\AppData\\Ollama build is the one on PATH and serving :11434."),
    (r"model .{0,60}not found|404.*pull", KIND_ENV,
     "The route names a model Ollama does not have. `ollama pull <tag>`, then refresh the catalog "
     "with `python -m aitime.catalog`."),
    (r"ReasoningBudgetExhausted|spent its entire token budget reasoning|reasoning-only", KIND_PROVIDER,
     "The model thought until its budget ran out and never answered. Raise max_tokens for that "
     "call or shorten the prompt; for local Ollama routes keep FLEXFACTOR_OLLAMA_THINK unset so the "
     "reasoning channel stays off."),
    (r"max_tokens.{0,40}less than or equal to|maximum context length|context_length_exceeded", KIND_PROVIDER,
     "The route's output/context ceiling is below what was requested. FlexFactor learns the ceiling "
     "from this 400 and retries once; if it recurs, the prompt unit must shrink (fewer findings per "
     "call) or the route should be excluded for large files."),
    (r"Interactions API|only supports Interactions", KIND_PROVIDER,
     "This is a 'deep research' product, not a chat model; it can never serve code work through this "
     "transport. It is on the unfit list (deep-research); refresh the catalog if it reappears."),
    (r"\b402\b|requires more credits|can only afford", KIND_BUDGET,
     "The provider's allowance for this key is spent (OpenRouter's free tier is balance-bound). "
     "The rotator cools that pool and moves on; it recovers when the allowance resets. Do not "
     "add paid credit to compensate."),
    (r"\b403\b|PermissionDenied|not permitted|gated", KIND_PROVIDER,
     "This route is gated or not permitted for the key in use. Rotation skips it after strikes; "
     "to stop retrying it, exclude it (FLEXFACTOR_ROTATION_EXCLUDE=<fragment>) or have AI Time's "
     "catalog mark it disabled."),
    (r"not a chat model|does not support chat|unsupported_endpoint", KIND_PROVIDER,
     "The catalog lists a model that cannot serve chat completions (realtime/audio/embedding "
     "products). Add its family to the unfit list (flexfactor_directed._UNFIT_CODE_PATTERNS and "
     "the Factory Deck twin) so rotation never selects it."),
    (r"\b429\b|rate.?limit|Too Many Requests", KIND_PROVIDER,
     "Rate-limited. The rotator cools the pool down and moves on; nothing to fix unless it recurs "
     "on every pool, which means the free tiers are exhausted for now."),
    (r"\b529\b|\b503\b|overloaded|capacity", KIND_PROVIDER,
     "Provider overloaded. Rotation already moves to the next pool; no change needed."),
    (r"quota|insufficient_quota|credit|billing", KIND_BUDGET,
     "The pool's allowance is spent. The rotator benches it until reset; check AI Time for the "
     "reset time. Do not add paid keys to compensate."),
    (r"TLS handshake timeout|ETIMEDOUT|ECONNRESET|Connection reset|Remote end closed|RemoteDisconnected",
     KIND_ENV, "Transient network failure. Retry; if it repeats for one host only, that host is down."),
    (r"WinError 10061|Connection refused|actively refused", KIND_ENV,
     "Nothing is listening at that address. Start the service (Ollama: `ollama serve`; FCC proxy: "
     "fcc-toggle.ps1) and re-run."),
    (r"PinUnavailable|pinned target .{0,60}(matches no route|cannot serve)", KIND_ENV,
     "A pin names a route that cannot serve. Clear it (`pnpm rotation:pin --clear --app global` or "
     "unset AI_ROTATE_PIN) or wait for the pinned pool's reset."),
    (r"RotationError|every .{0,20} pool failed|no usable route", KIND_PROVIDER,
     "Every candidate pool was unavailable for this call's needs. Read the per-pool reasons in the "
     "message: cooling pools recover on their own; 'lacks <capability>' means the catalog has no "
     "fit route for this role -- refresh the catalog or relax the role's needs."),
    (r"EgressBlockedError|flexfactor_egress_blocked", KIND_PROGRAM,
     "The egress gate found a secret/PII pattern in repo-derived text and refused to send it to a "
     "cloud model. Remove the secret from the repo (or use --redact / FLEXFACTOR_ALLOW_EGRESS for a "
     "known-safe fixture)."),
    (r"flexfactor_policy_blocked|rc 126|containment_blocked", KIND_ENV,
     "The command policy or containment gate refused the command. Trust the repo (--trust-repo / "
     "FLEXFACTOR_TRUSTED_REPOS) or allow the command class in ~/.flexfactor/policy.json."),
    (r"UnicodeDecodeError.*charmap|'charmap' codec", KIND_TOOL,
     "A subprocess was read with the Windows locale codec. Every capture site must pass "
     "encoding='utf-8', errors='replace' (see _run)."),
    (r"ModuleNotFoundError: No module named '([^']+)'", KIND_ENV,
     "A Python dependency is missing in the interpreter that ran. Install it in that interpreter "
     "(`python -m pip install <module>`), or point the run at the project's venv."),
    (r"pytest|FAILED |AssertionError|Error: .* test", KIND_PROGRAM,
     "The program's own test suite is red. Open the named test, read its assertion, and fix the "
     "implementation it exercises (not the test) -- the build gate will not publish until it passes."),
    (r"npm ERR|ERR_PNPM|tsc .*error TS|error TS\d+", KIND_PROGRAM,
     "The program's build/typecheck failed. Fix the named file:line; a red build gate blocks publish."),
    (r"OutputBudgetError|hit the .{0,20}token budget", KIND_TOOL,
     "The model's output was cut off by the budget. FlexFactor shrinks the unit of work and retries; "
     "if the file still cannot be regenerated, it is recorded as oversized for that model."),
    (r"PartialOutputError|salvaged", KIND_PROVIDER,
     "The model returned truncated JSON. Treated as failure evidence, never as a clean verdict; "
     "the file stays INCOMPLETE and is retried on another route."),
    (r"PermissionError.*13|os\.replace|being used by another process", KIND_ENV,
     "A file was locked by another process (often AV scanning or an editor). Close it and retry."),
    (r"TypeError: .*got an unexpected keyword argument", KIND_TOOL,
     "A call passes a kwarg the callee does not accept -- usually a signature changed on one side "
     "of a test double or a provider class. Align the signature; see the responsible line."),
]

_SUGGEST_FALLBACK = "no known fix; start from the responsible code above"


def classify(text: str) -> Tuple[str, str]:
    """(kind, suggestion) from the signature table, or (unknown, fallback)."""
    for pattern, kind, suggestion in SIGNATURES:
        if re.search(pattern, text, re.I | re.S):
            return kind, suggestion
    return KIND_UNKNOWN, _SUGGEST_FALLBACK


def responsible_frame(exc: Optional[BaseException], tool_root: str) -> Optional[Dict[str, Any]]:
    """The innermost stack frame inside FlexFactor's own code, with its source line.

    Innermost because the deepest FlexFactor frame is the one that made the
    call that failed; outer frames are the phases that happened to be running.
    None when the stack never passes through tool_root (pure program/provider
    failure) or when there is no exception.
    """
    if exc is None or exc.__traceback__ is None:
        return None
    root = os.path.normcase(os.path.abspath(tool_root))
    chosen = None
    for frame in traceback.extract_tb(exc.__traceback__):
        fn = os.path.normcase(os.path.abspath(frame.filename))
        if fn.startswith(root) and os.path.basename(fn).startswith("flexfactor"):
            chosen = frame
    if chosen is None:
        return None
    src = (chosen.line or linecache.getline(chosen.filename, chosen.lineno) or "").strip()
    return {"file": os.path.basename(chosen.filename), "line": chosen.lineno,
            "function": chosen.name, "source": src[:200]}


class ErrorLedger:
    """Append-only, written to disk after every record."""

    def __init__(self, run_dir: str, program: str, tool_root: str,
                 suggester: Optional[Callable[[str, str], str]] = None):
        self.run_dir = run_dir
        self.program = program
        self.tool_root = tool_root
        self._suggester = suggester        # optional model fallback, label-aware
        self.entries: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    # -- paths --------------------------------------------------------------
    @property
    def json_path(self) -> str:
        return os.path.join(self.run_dir, "errors.json")

    @property
    def md_path(self) -> str:
        return os.path.join(self.run_dir, "errors.md")

    # -- recording ----------------------------------------------------------
    def record(self, phase: str, error: Any, *, program_file: str = "",
               route: str = "", kind: str = "", suggestion: str = "",
               detail: str = "") -> Dict[str, Any]:
        """Record one error. `error` may be an exception or a string."""
        exc = error if isinstance(error, BaseException) else None
        text = (f"{type(exc).__name__}: {exc}" if exc is not None else str(error or "")).strip()
        where = responsible_frame(exc, self.tool_root)
        auto_kind, auto_sugg = classify(text + "\n" + (detail or ""))
        kind = kind or auto_kind
        if not kind or kind == KIND_UNKNOWN:
            # A provider call always has our HTTP client on the stack; that
            # frame is where the error SURFACED, not what caused it. Live
            # 2026-08-23: a gated 403 and a 'not a chat model' 404 were
            # filed as flexfactor-defect for exactly that reason. When the
            # failure belongs to a route, it is the provider's unless a
            # signature says otherwise; a program file means the program's;
            # only then does a FlexFactor frame mean it is ours.
            kind = (KIND_PROVIDER if route else
                    KIND_PROGRAM if program_file else
                    KIND_TOOL if where else KIND_UNKNOWN)
        sugg = suggestion or auto_sugg
        sugg_source = "signature" if (suggestion or auto_sugg != _SUGGEST_FALLBACK) else "none"
        # The model fallback is for errors in OUR phases. A route failure is
        # the provider's business and the signature table covers it; asking a
        # model about it would itself be a rotated call, whose own failure
        # would land back here -- the loop this guard exists to prevent.
        if sugg_source == "none" and self._suggester is not None and not route \
                and not getattr(self, "_in_suggester", False):
            self._in_suggester = True
            try:
                model_sugg = self._suggester(text, json.dumps(where or {}))
                if model_sugg and model_sugg.strip():
                    sugg = "model suggestion, unverified: " + model_sugg.strip()[:600]
                    sugg_source = "model"
            except Exception as sx:  # noqa: BLE001 - the ledger must never throw
                sugg = f"{_SUGGEST_FALLBACK} (model suggester failed: {sx})"
            finally:
                self._in_suggester = False
        entry = {
            "n": len(self.entries) + 1,
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "phase": str(phase or "")[:80],
            "error": text[:2000],
            "detail": str(detail or "")[:2000],
            "responsible": where,                 # FlexFactor frame, or None
            "program_file": str(program_file or ""),
            "route": str(route or ""),
            "kind": kind,
            "suggestion": sugg,
            "suggestion_source": sugg_source,
        }
        if exc is not None:
            entry["traceback"] = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))[-4000:]
        with self._lock:
            self.entries.append(entry)
            self._write_locked()
        return entry

    # -- rendering ----------------------------------------------------------
    def _write_locked(self) -> None:
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            tmp = self.json_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"program": self.program, "entries": self.entries}, fh, indent=2)
            os.replace(tmp, self.json_path)
            tmp = self.md_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(self.render_markdown())
            os.replace(tmp, self.md_path)
        except OSError as exc:
            print(f"  [error-ledger] could not write {self.run_dir}: {exc}", file=sys.stderr)

    def render_markdown(self, heading_level: int = 1) -> str:
        h = "#" * heading_level
        if not self.entries:
            return f"{h} Errors\n\nNone recorded.\n"
        by_kind: Dict[str, int] = {}
        for e in self.entries:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        out = [f"{h} Errors ({len(self.entries)})", "",
               "| # | phase | kind | error | responsible |", "|---|---|---|---|---|"]
        for e in self.entries:
            resp = (f"{e['responsible']['file']}:{e['responsible']['line']}" if e.get("responsible")
                    else e.get("program_file") or e.get("route") or "-")
            err = e["error"].splitlines()[0][:90].replace("|", "\\|")
            out.append(f"| {e['n']} | {e['phase']} | {e['kind']} | {err} | {resp} |")
        out.append("")
        out.append("Counts by kind: " + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
        for e in self.entries:
            out += ["", f"{h}# {e['n']}. {e['phase']} — {e['kind']}", "",
                    "**Error**", "", "```", e["error"], "```"]
            if e.get("detail"):
                out += ["", "**Detail**", "", "```", e["detail"][:1500], "```"]
            out += ["", "**Responsible code**", ""]
            if e.get("responsible"):
                r = e["responsible"]
                out += [f"- FlexFactor `{r['file']}:{r['line']}` in `{r['function']}()`",
                        "", "```python", r.get("source", ""), "```"]
            if e.get("program_file"):
                out.append(f"- Program file: `{e['program_file']}`")
            if e.get("route"):
                out.append(f"- Route: `{e['route']}`")
            if not (e.get("responsible") or e.get("program_file") or e.get("route")):
                out.append("- Not attributable to a specific line from the evidence recorded.")
            out += ["", f"**Suggested fix** ({e['suggestion_source']})", "", e["suggestion"]]
        out.append("")
        return "\n".join(out)

    def summary_line(self) -> str:
        if not self.entries:
            return "[errors] none recorded"
        return f"[errors] {len(self.entries)} recorded -> {self.md_path}"
