"""CLI-backed provider adapter for flexfactor_rotation.

Implements the same surface as AnthropicProvider / OpenAIProvider / the Cursor
adapter so RotatingProvider can call it transparently. Claude and Copilot use
bounded local CLI subprocesses. Codex prefers the owner's exportable ChatGPT
OAuth subscription over HTTPS and retains the official CLI as a fallback:

    api="claude-code"  -> the `claude` CLI   (flat-rate subscription)
    api="codex-cli"    -> ChatGPT OAuth, then `codex` CLI (flat-rate subscription)
    api="copilot-cli"  -> the `copilot` CLI  (GitHub Copilot entitlement)

WHY THIS EXISTS
---------------
Both CLIs are already installed and flat-rate on this machine, so every call
routed through them is capacity the catalog otherwise cannot see. Adding them
as rotation POOLS spreads work off the metered/quota'd HTTP routes.

WHY IT IS WRITTEN THIS WAY
--------------------------
1. PROMPTS GO OVER STDIN, NEVER argv. This machine's launchers run under
   Windows PowerShell 5.1, which mangles embedded quotes in native arguments -
   a review prompt full of source code and JSON braces on a command line is a
   guaranteed corruption. stdin has no such problem and no length limit.

2. EVERY CALL IS BOUNDED. An unbounded subprocess wait is the exact shape that
   froze a live run for 25+ minutes with a static cost meter. `timeout` is
   always passed, expiry kills the child, and the error is raised as
   `CliUnavailable` so the rotator rolls over to the next pool instead of
   hanging the sweep.

3. IT FAILS CLOSED, LOUDLY. A missing binary, a non-zero exit, an empty answer
   or unparseable JSON all raise `CliUnavailable` (a RuntimeError subclass).
   Nothing here can return a plausible-looking empty result that a caller would
   record as a completed review.

4. IT REFUSES TO RECURSE. `claude` invoked from inside a Claude Code session
   would spawn a nested agent, and a rotation sweep could fan out into dozens.
   `_recursion_guard_env` stamps a marker into the child's environment and the
   provider refuses when it sees its own marker already set.

SECRETS
-------
No API keys are read, stored, or logged - both CLIs carry their own
authentication. The prompt is passed to the child process only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from typing import Any, Dict, Optional


class CliUnavailable(RuntimeError):
    """Raised when the CLI cannot serve a call; the rotator handles this."""


#: Marker used to detect (and refuse) a nested invocation.
_RECURSION_MARKER = "FLEXFACTOR_CLI_PROVIDER_ACTIVE"

#: Per-call ceiling. Generous because these are flat-rate and a real code
#: review legitimately takes minutes - but never unbounded.
DEFAULT_TIMEOUT_S = float(os.environ.get("FLEXFACTOR_CLI_TIMEOUT", "600") or 600)

#: Which executable serves which catalog api id.
CLI_BINARIES = {
    "claude-code": "claude",
    "codex-cli": "codex",
    "copilot-cli": "copilot",
}

_GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "grade": {"type": "integer"},
        "meets_goal": {"type": "boolean"},
        "rationale": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["grade", "meets_goal", "rationale", "issues"],
    "additionalProperties": False,
}

_GRADE_SYSTEM = (
    "You are a strict independent code reviewer. Grade the candidate against "
    "the stated goal. Reserve 90 or above for a complete result without "
    "correctness or completeness defects. Return only the requested JSON."
)


def _extensions_enabled() -> bool:
    """Same switch the Cursor adapter honours, so one flag governs both.

    It did NOT govern both: this accepted any non-empty value outside a small
    off-list while the other three call sites demanded the exact string "1".
    See flexfactor_flags for the drift that caused and the shared resolver.
    """
    try:
        from flexfactor_flags import rotation_extensions_enabled
        return rotation_extensions_enabled()
    except ImportError:
        return os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS", "").strip().lower() \
            not in ("0", "false", "no", "off")


def cli_binary_for(api: str) -> Optional[str]:
    """Resolved path of the CLI serving `api`, or None when unavailable."""
    name = CLI_BINARIES.get(str(api or "").strip().lower())
    return shutil.which(name) if name else None


def _recursion_guard_env() -> Dict[str, str]:
    env = dict(os.environ)
    env[_RECURSION_MARKER] = "1"
    # Keep the child non-interactive no matter how it is configured.
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    return env


def _argv_for(api: str, binary: str, system: Optional[str],
              model: Optional[str] = None) -> list:
    """Non-interactive argv for one CLI. The PROMPT is never included here."""
    api = str(api or "").lower()
    if api == "claude-code":
        # `-p` is print/non-interactive mode; it reads the prompt from stdin
        # when one is piped in.
        argv = [binary, "-p", "--output-format", "text"]
        if system:
            # Carries the run's DIRECTED WORK THEME through to the CLI, so a
            # rotated call stays on the same task as every other provider.
            argv += ["--append-system-prompt", system]
        return argv
    if api == "codex-cli":
        # `exec` is codex's non-interactive one-shot mode. Keep provider calls
        # ephemeral and read-only: FlexFactor owns every filesystem mutation;
        # this nested process supplies inference only.
        return [binary, "exec", "--ephemeral", "--ignore-user-config",
                "--sandbox", "read-only", "--color", "never",
                "--skip-git-repo-check", "-"]
    if api == "copilot-cli":
        # Silent programmatic mode reads the prompt from stdin. No tools are
        # allowlisted: FlexFactor needs model inference here, not a second agent
        # with shell or filesystem authority.  Pin the catalog's concrete model
        # so the rotation ledger records the family that actually served the
        # call; `auto` remains accepted for ordinary calls but is explicitly
        # ineligible for family-independent authorization upstream.
        argv = [binary, "-s", "--no-ask-user", "--no-auto-update", "--no-color",
                "--no-custom-instructions"]
        if model:
            argv += ["--model", str(model)]
        return argv
    raise CliUnavailable(f"no CLI argv defined for api '{api}'")


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Kill the CLI *and* every descendant that inherited its captured pipes.

    ``Popen.kill()`` reaches only the direct child.  Agent CLIs routinely spawn
    a worker that keeps the inherited stdout/stderr write handles open, and a
    surviving holder is precisely what makes the advertised timeout escapable
    (see ``_run_process_tree``).  Each CLI is therefore started in its own
    process group so the whole group can be terminated in one call.
    """
    if os.name == "nt":
        # ``taskkill /T`` walks the parent-PID tree, which is what actually
        # reaches a worker the CLI spawned. Bounded, and failure is not fatal:
        # the direct-child kill below is the fallback.
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10, check=False, shell=False,
                )
            except Exception:                                 # pragma: no cover
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
            pass


def _run_process_tree(argv: list, **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` with a timeout that is actually enforced.

    WHY THIS EXISTS - measured 2026-09-05 on Windows 11 / CPython 3.14.6.
    ``subprocess.run(..., timeout=N)`` does not bound anything when the child
    leaves a descendant holding the captured pipes.  On expiry ``run`` kills
    only the direct child and then calls ``process.communicate()`` a second
    time *with no timeout*; on Windows that joins the daemon reader threads,
    which block until EOF, which never arrives while a grandchild holds the
    write handle.  Confirmed directly: with no timeout at all, ``run`` returns
    only after the GRANDCHILD exits and captures what the grandchild wrote,
    long after the child itself is gone.

    THE OVERRUN IS INTERMITTENT, and that is the important part.  Whether the
    worker wins the race to inherit the handle before the child exits is not
    deterministic.  Measured over a 12-run sweep at ``timeout=3`` against a
    worker living 20s: 2 runs raised after 24.0s and 25.7s - an 8x overrun -
    and 10 returned in ~3.3s.  So the failure is real, rare per call, and
    certain to recur across the thousands of calls an audit makes.  This is
    the shape behind ``cli/claude-code: exceeded 600s and was killed`` taking
    far longer than 600s to say so, and it is also why the regression tests
    for this function assert on cleanup BEHAVIOUR rather than on elapsed time
    (a wall-clock assertion would pass against the unpatched code ~80% of the
    time - a tripwire that mostly does not trip is worse than none).

    On expiry: terminate the entire process group, reap the direct child, and
    re-raise.  Deliberately NOT done afterwards - both of these reintroduce the
    unbounded wait this function exists to remove:

      * a second ``communicate()``, even with a timeout, re-joins the same
        blocked reader threads;
      * ``stream.close()`` from this thread blocks on the ``BufferedReader``
        lock that the still-reading daemon thread holds.

    The cost is a deliberately leaked daemon reader thread per timed-out call,
    holding one pipe handle and whatever the descendant writes.  It is bounded
    and self-clearing: CPython's ``_readerthread`` closes the handle itself and
    exits as soon as the descendant does, and it owns the only strong reference
    to that handle in the meantime, so nothing else can close it early.  At a
    600s per-call ceiling these are rare, and one parked thread is strictly
    cheaper than an unbounded hang in the rotation ladder.

    The return value and every raised exception keep ``subprocess.run``'s
    shapes so callers need no change: ``CompletedProcess(args, returncode,
    stdout, stderr)``, ``FileNotFoundError`` from spawn, ``TimeoutExpired`` on
    expiry, and a killed tree on ``KeyboardInterrupt``.
    """
    input_data = kwargs.pop("input", None)
    timeout = kwargs.pop("timeout", None)
    capture_output = bool(kwargs.pop("capture_output", False))
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used "
                             "with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input_data is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **kwargs)
    try:
        stdout, stderr = proc.communicate(input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):           # pragma: no cover
            pass
        raise
    except BaseException:
        # Same contract as subprocess.run, KeyboardInterrupt included: never
        # leave the tree running behind a propagating exception.
        _terminate_process_tree(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _run_cli(api: str, binary: str, prompt: str, *, system: Optional[str],
             timeout: float, model: Optional[str] = None) -> str:
    if os.environ.get(_RECURSION_MARKER):
        raise CliUnavailable(
            f"refusing to invoke {binary}: already running inside a "
            "CLI-provider call (nested agents would fan out per rotation step)")
    argv = _argv_for(api, binary, system, model)
    # `codex exec` takes no --append-system-prompt, so the theme is prepended
    # to the prompt instead. Losing it would let a rotated call wander off the
    # run's task, which is the whole reason the theme block exists.
    body = prompt if (api not in ("codex-cli", "copilot-cli") or not system) \
        else f"{system}\n\n{prompt}"
    try:
        proc = _run_process_tree(
            argv,
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_recursion_guard_env(),
            shell=False,
        )
    except FileNotFoundError:
        raise CliUnavailable(f"{binary}: not found on PATH")
    except subprocess.TimeoutExpired:
        raise CliUnavailable(f"{binary}: exceeded {timeout:.0f}s and was killed")
    except Exception as exc:                                  # pragma: no cover
        raise CliUnavailable(f"{binary}: {exc}")

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        tail = ((proc.stderr or "") or out)[-400:]
        raise CliUnavailable(f"{binary}: exited {proc.returncode}: {tail}")
    if not out:
        # An empty answer must never read as a successful empty review.
        raise CliUnavailable(f"{binary}: returned no output")
    return out


def _inside_managed_codex_session() -> bool:
    """Whether credentials are brokered to this process instead of exported.

    Work Mode deliberately places short sentinels in ``auth.json`` and keeps
    the real credential in its parent service. Starting another Codex process
    there stalls at thread creation and can disconnect the workspace executor.
    Refuse that known-dead recursion immediately; a normal local Codex session
    with a real OAuth file takes the direct subscription path before this guard.
    """
    return any(os.environ.get(name) for name in (
        "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_ENVIRONMENT_ID",
    ))


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Parse JSON out of CLI prose. Raises CliUnavailable when there is none.

    A CLI answers in prose by default, so the object is usually fenced or
    embedded. Never returns a partial object: the caller's schema check is
    downstream, and handing it half a payload turns a transport problem into a
    phantom review defect.
    """
    candidates = [text]
    m = _FENCE.search(text)
    if m:
        candidates.insert(0, m.group(1))
    # Widest balanced span, both object and array shapes.
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if 0 <= i < j:
            candidates.append(text[i:j + 1])
    for c in candidates:
        c = (c or "").strip()
        if not c:
            continue
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise CliUnavailable("CLI output contained no parseable JSON")


#: Sentinel distinguishing "build the default transport" from an injected
#: ``subscription=None`` that deliberately means "no subscription route".
_DEFAULT_SUBSCRIPTION = object()


def build_subscription_client(api: str, model: str, binary: str,
                              timeout: float) -> Optional[Any]:
    """The ONE place a network-capable ChatGPT client is constructed.

    WHY THIS IS A NAMED, SEPARATE FUNCTION
    --------------------------------------
    It used to be four inline statements in ``CliProvider.__init__``, and that
    is precisely how ``flexfactor_cli_provider_tests.py`` came to make real
    authenticated calls to chatgpt.com on any host with an exportable OAuth
    file: constructing the provider AT ALL loaded the owner's live credential,
    ``_complete`` then took the subscription branch, and the suite's
    ``_run_process_tree`` doubles - patched at ``subprocess`` - guarded nothing
    because that path was never reached (issue #161).

    A seam a test can intercept has to be reachable by name and injectable by
    argument, the same discipline ``gather_purpose_evidence``'s required
    ``git_runner``/``gh_runner`` and the injected ``_run`` in
    ``flexfactor_prodready`` already use. Returns ``None`` when this api has no
    subscription transport or the host has no exportable credential.
    """
    if str(api or "").lower() != "codex-cli":
        return None
    # Lazy import keeps the other CLI adapters dependency-free and preserves
    # their original startup behavior.
    from providers.chatgpt_subscription import (
        ChatGPTSubscriptionClient, load_exportable_oauth,
    )
    oauth = load_exportable_oauth()
    if oauth is None:
        return None
    return ChatGPTSubscriptionClient(
        oauth, model=model, binary=binary, timeout=float(timeout))


class CliProvider:
    """Provider adapter backed by a local, flat-rate CLI."""

    def __init__(self, api: str, model: str, binary: str,
                 judge_model: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT_S,
                 subscription: Any = _DEFAULT_SUBSCRIPTION) -> None:
        self.api = api
        self.model = model
        self.judge_model = judge_model or model
        self._binary = binary
        self._timeout = float(timeout or DEFAULT_TIMEOUT_S)
        # `subscription` is the injection seam. Passing it explicitly - most
        # importantly `subscription=None` - is how a caller (a test above all)
        # states that this provider must NOT hold a live paid transport,
        # instead of that being decided by whether the machine happens to be
        # signed in to ChatGPT. See build_subscription_client and issue #161.
        self._subscription = (
            build_subscription_client(api, model, binary, self._timeout)
            if subscription is _DEFAULT_SUBSCRIPTION else subscription)

    def _complete(self, prompt: str, *, system: Optional[str],
                  max_tokens: int, timeout: Optional[float] = None) -> str:
        if self._subscription is not None:
            from providers.chatgpt_subscription import (
                SubscriptionAuthenticationError, SubscriptionUnavailable,
            )
            try:
                answer = self._subscription.complete(
                    prompt, system=system, max_tokens=max_tokens,
                    timeout=timeout,
                )
            except SubscriptionAuthenticationError as exc:
                # An access token can expire while the official Codex CLI still
                # has a refresh token and knows how to renew it. Hand this exact
                # call to that supported client instead of treating one stale
                # bearer as proof the paid subscription route is exhausted.
                if _inside_managed_codex_session():
                    raise CliUnavailable(
                        f"{exc}; official Codex CLI refresh is unavailable inside "
                        "this managed Codex session") from None
                self._subscription = None
                return _run_cli(
                    self.api, self._binary, prompt, system=system,
                    timeout=float(timeout or self._timeout),
                    model=self.model,
                )
            except SubscriptionUnavailable as exc:
                # RotatingProvider recognizes CliUnavailable as a fault of this
                # route and continues down the AI Time ladder.
                raise CliUnavailable(str(exc)) from None
            self.model = self._subscription.model or self.model
            return answer
        if (self.api == "codex-cli" and os.path.isabs(self._binary)
                and _inside_managed_codex_session()):
            raise CliUnavailable(
                "codex: this managed Codex session brokers its ChatGPT "
                "credential to the parent service; nested inference is not "
                "available. Run FlexFactor outside the active Work Mode "
                "session or provide another AI Time route")
        return _run_cli(
            self.api, self._binary, prompt, system=system,
            timeout=float(timeout or self._timeout),
            model=self.model,
        )

    #: Cost label. Both CLIs are flat-rate, so rotated calls bill $0.
    #:
    #: THIS WAS NAMED `meter` AND IT KNOCKED THE STRONGEST POOLS OUT OF ROTATION.
    #: Everywhere else in FlexFactor `provider.meter` is the shared CostMeter
    #: OBJECT, and `RotatingProvider._provider_for` assigns it on first use:
    #:     if hasattr(provider, "meter"): provider.meter = self.meter
    #: Against a read-only PROPERTY that raises
    #:     AttributeError: property 'meter' of 'CliProvider' object has no setter
    #: so every claude-code / codex-cli / cursor route - the FRONTIER subscription
    #: pools, the ones that would have done the work - died on selection. Measured
    #: in the owner's 2026-08-20/21 overnight manifests: that exact message is the
    #: `not judged:` reason on Iplay and PromoPilot, and the run fell through to
    #: `google/recurrentgemma-2b` and `groq/compound`, which then 400'd on
    #: max_tokens. A cost LABEL and a cost METER are different things and must
    #: never share a name.
    @property
    def cost_label(self) -> str:
        """Flat-rate billing label for reporting (NOT a CostMeter)."""
        return f"{self.api}:subscription"

    #: Assignable, because RotatingProvider shares one CostMeter across routes.
    meter: Any = None

    def ping(self, **_: Any) -> bool:
        """Prove authenticated inference, not merely executable presence."""
        answer = self._complete(
            # Reasoning tokens count against the Responses output ceiling. A
            # 16-token ceiling can consume itself before emitting the two
            # visible characters and falsely label a healthy route dead.
            "Reply with OK only.", system=None, max_tokens=256,
            timeout=min(self._timeout, 60),
        )
        return bool(answer.strip())

    def complete(self, prompt: str, *, system: Optional[str] = None,
                 max_tokens: int = 4096, **_: Any) -> str:
        return self._complete(prompt, system=system, max_tokens=max_tokens)

    def grade(self, prompt: str, *, system: Optional[str] = None,
              max_tokens: int = 4096, **_: Any) -> Dict[str, Any]:
        """Request the same typed grade contract as the HTTP adapters.

        A plain ``complete`` call leaves the CLI free to answer in prose. The
        rotating provider preserves that raw string, so downstream attribute
        access used to fail despite a healthy reviewer. Reuse the JSON-schema
        transport here; the core still independently normalizes and validates
        the result before any grade can authorize code.
        """
        return self.structured(
            system or _GRADE_SYSTEM,
            prompt,
            _GRADE_SCHEMA,
            max_tokens=max_tokens,
        )

    def structured(self, system: str, prompt: Optional[str] = None,
                   schema: Optional[Dict[str, Any]] = None,
                   max_tokens: int = 4096, model: Optional[str] = None,
                   salvage_truncated: bool = False, **_: Any) -> Any:
        """Match FlexFactor's provider contract while accepting legacy forms."""
        if isinstance(prompt, dict) and schema is None:
            schema = prompt
            prompt = system
            system = ""
        elif prompt is None:
            prompt = system
            system = ""
        schema = schema or {}
        instruction = (
            "Reply with a single JSON value that satisfies this schema. "
            "Output JSON only - no prose, no code fence, no commentary.\n"
            f"SCHEMA: {json.dumps(schema)}\n\n{prompt}"
        )
        return _extract_json(
            self._complete(instruction, system=system or None,
                           max_tokens=max_tokens))


def make_cli_provider(route: Any) -> CliProvider:
    """Build a provider for one catalog route, or raise CliUnavailable."""
    if not _extensions_enabled():
        raise CliUnavailable(
            "CLI providers are off (set FLEXFACTOR_ROTATION_EXTENSIONS=1)")
    api = str(getattr(route, "api", "") or "").strip().lower()
    binary = cli_binary_for(api)
    if not binary:
        want = CLI_BINARIES.get(api, api)
        raise CliUnavailable(f"{want}: not installed or not on PATH")
    wire = getattr(route, "wire_model", None) or getattr(route, "model", "") or api
    return CliProvider(api=api, model=wire, binary=binary,
                       judge_model=wire)
