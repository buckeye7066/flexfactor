"""Tests for holding the local Muse Glimmer route out of rotation.

The failure this guards against is specific and was measured, not imagined: on
this machine Ollama has no usable GPU, so `ollama/muse-glimmer:30b` generates at
about 1.6 tokens/second. Rotation is CHEAPEST-FIRST and a local route is cost
class 0, so an un-excluded Glimmer would be selected FIRST on every sweep and
then blow the 600s ollama HTTP timeout on any real generation -- appearing as a
provider outage rather than as "that model is slow here".

The second failure, equally important, is OVER-blocking: the catalog also
carries the same model served from NVIDIA NIM (free-tier) and OpenRouter (paid).
Those are cloud routes at cloud speed and must keep rotating; excluding them
would quietly cost the owner a good free route.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest

import tempfile

sys.argv = sys.argv[:1]          # flexfactor parses argv at import time
# Isolate the measured-speed gate: without this the tests read the REAL
# %LOCALAPPDATA%\AITime\local-bench.json and every verdict below depends
# on whatever was benched on this machine last night.
_ISOLATED_STATE_DIR = tempfile.mkdtemp(prefix="ff-glimmer-tests-")
os.environ["AITIME_STATE_DIR"] = _ISOLATED_STATE_DIR
import flexfactor as F           # noqa: E402
F._LOCAL_BENCH_CACHE = None


def route(rid: str, model: str, api: str = "ollama",
          base: str = "http://127.0.0.1:11434",
          backend: str | None = None, cost_class: str | None = None):
    """Minimal stand-in for an aitime.catalog Route row.

    `backend` and `cost_class` are NOT optional decoration: the mode filter
    reads them, and a stub that omits them reports cost_class 'unset', which
    the two-mode boundary treats as PAID (unknown cost must never be assumed
    free - that assumption is the one that spends money). They default off the
    rid the way discovery fills them, so a stub route is self-consistent.
    """
    r = types.SimpleNamespace()
    r.id, r.model, r.api, r.base_url = rid, model, api, base
    r.wire_model, r.auth_env, r.pool = model, None, "local:ollama"
    r.backend = backend or rid.split("/", 1)[0]
    r.cost_class = cost_class or ("local-unlimited" if r.backend == "ollama"
                                  else "free-tier")
    return r


class ExclusionMatching(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXCLUDE", None)
        # Re-isolate every test: a sibling test module pops AITIME_STATE_DIR
        # in its tearDown, and under pytest ordering these tests would then
        # read the real local-bench.json.
        os.environ["AITIME_STATE_DIR"] = _ISOLATED_STATE_DIR
        F._LOCAL_BENCH_CACHE = None

    tearDown = setUp

    def test_local_glimmer_is_excluded(self):
        self.assertTrue(F._rotation_excluded_reason("ollama/muse-glimmer:30b"))

    def test_local_glimmer_custom_tag_is_excluded(self):
        self.assertTrue(F._rotation_excluded_reason("ollama/muse-glimmer:30b-q4kxl"))

    def test_reason_names_the_standalone_escape_hatch(self):
        why = F._rotation_excluded_reason("ollama/muse-glimmer:30b")
        # A dead-end reason string is how an operator ends up thinking the model
        # is broken. It must say what to do instead.
        self.assertIn("standalone", why)

    def test_nvidia_nim_glimmer_still_rotates(self):
        # Free-tier cloud route: fast, and excluding it would cost real capacity.
        self.assertEqual(
            F._rotation_excluded_reason("nvidia_nim/meta/muse-glimmer-30b"), "")

    def test_openrouter_glimmer_still_rotates(self):
        self.assertEqual(
            F._rotation_excluded_reason("openrouter/meta/muse-glimmer-30b"), "")

    def test_other_local_models_untouched(self):
        for rid in ("ollama/qwen3-coder:30b", "ollama/llama3.2:latest",
                    "ollama/deepseek-coder:33b"):
            self.assertEqual(F._rotation_excluded_reason(rid), "", rid)

    def test_paid_cloud_untouched(self):
        self.assertEqual(F._rotation_excluded_reason("openai/gpt-4o"), "")


class ExclusionIsConfigurable(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXCLUDE", None)
        # Re-isolate every test: a sibling test module pops AITIME_STATE_DIR
        # in its tearDown, and under pytest ordering these tests would then
        # read the real local-bench.json.
        os.environ["AITIME_STATE_DIR"] = _ISOLATED_STATE_DIR
        F._LOCAL_BENCH_CACHE = None

    tearDown = setUp

    def test_empty_env_lets_glimmer_rotate(self):
        os.environ["FLEXFACTOR_ROTATION_EXCLUDE"] = ""
        self.assertEqual(
            F._rotation_excluded_reason("ollama/muse-glimmer:30b"), "")

    def test_env_can_target_something_else(self):
        os.environ["FLEXFACTOR_ROTATION_EXCLUDE"] = "qwen3-coder"
        self.assertTrue(F._rotation_excluded_reason("ollama/qwen3-coder:30b"))
        self.assertEqual(
            F._rotation_excluded_reason("ollama/muse-glimmer:30b"), "")


class WiredIntoTheRealFilter(unittest.TestCase):
    """The helper being correct proves nothing if nothing calls it."""

    def setUp(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXCLUDE", None)
        # Re-isolate every test: a sibling test module pops AITIME_STATE_DIR
        # in its tearDown, and under pytest ordering these tests would then
        # read the real local-bench.json.
        os.environ["AITIME_STATE_DIR"] = _ISOLATED_STATE_DIR
        F._LOCAL_BENCH_CACHE = None

    tearDown = setUp

    def test_filter_rejects_local_glimmer(self):
        why = F._route_unusable_reason(
            route("ollama/muse-glimmer:30b", "muse-glimmer:30b"), "free")
        self.assertIn("excluded from rotation", why)

    def test_filter_admits_a_normal_local_route(self):
        self.assertEqual(
            F._route_unusable_reason(
                route("ollama/qwen3-coder:30b", "qwen3-coder:30b"), "free"),
            "")

    def test_filter_admits_the_free_cloud_glimmer(self):
        r = route("nvidia_nim/meta/muse-glimmer-30b", "meta/muse-glimmer-30b",
                  api="openai", base="https://integrate.api.nvidia.com/v1")
        self.assertEqual(F._route_unusable_reason(r, "free"), "")



class ReasoningBudgetIsNotAnEmptyReply(unittest.TestCase):
    """Measured against meta/muse-glimmer-30b on NVIDIA NIM 2026-08-22:
    `content: null`, `reasoning_content` populated, finish_reason "length".
    The old `(content or "")` scored that as an empty answer, and the grade
    path fed "{}" to _parse_grade -- a default grade from a call that never
    produced one."""

    @staticmethod
    def _resp(content, reasoning=None, finish="stop"):
        m = types.SimpleNamespace(content=content, reasoning_content=reasoning)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=m, finish_reason=finish)])

    def test_normal_answer_passes_through(self):
        self.assertEqual(F._openai_message_text(self._resp("hello"), "x"), "hello")

    def test_answer_wins_when_both_present(self):
        self.assertEqual(
            F._openai_message_text(self._resp("51", "thinking"), "x"), "51")

    def test_null_content_with_reasoning_raises(self):
        with self.assertRaises(F.ReasoningBudgetExhausted):
            F._openai_message_text(self._resp(None, "thinking", "length"), "x")

    def test_genuinely_empty_is_unchanged(self):
        self.assertEqual(F._openai_message_text(self._resp(None), "x"), "")

    def test_no_choices_is_unchanged(self):
        self.assertEqual(
            F._openai_message_text(types.SimpleNamespace(choices=[]), "x"), "")


class OllamaProviderThinkingChannel(unittest.TestCase):
    """Local calls run with think=false, and a reasoning-only reply raises
    instead of collapsing to ''. Measured 2026-08-23: gemma4:26b could not fix
    a planted off-by-one in 551 s of reasoning and fixed it in 7 s without."""

    class _Resp:
        def __init__(self, payload): self._b = json.dumps(payload).encode()
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _provider(self, reply):
        captured = {}
        class Opener:
            def open(self_inner, req, timeout=None):
                captured["payload"] = json.loads(req.data.decode())
                return OllamaProviderThinkingChannel._Resp(reply)
        p = object.__new__(F.OllamaProvider)
        p.base_url = "http://127.0.0.1:11434"; p._opener = Opener(); p.meter = None
        return p, captured

    def setUp(self): os.environ.pop("FLEXFACTOR_OLLAMA_THINK", None)
    tearDown = setUp

    def test_think_is_off_by_default(self):
        p, cap = self._provider({"message": {"content": "ok"}})
        self.assertEqual(p._chat("m", "s", "u", 32), "ok")
        self.assertIs(cap["payload"]["think"], False)

    def test_owner_can_turn_thinking_back_on(self):
        os.environ["FLEXFACTOR_OLLAMA_THINK"] = "1"
        p, cap = self._provider({"message": {"content": "ok"}})
        p._chat("m", "s", "u", 32)
        self.assertIs(cap["payload"]["think"], True)

    def test_reasoning_only_reply_raises_not_empty(self):
        p, _ = self._provider({"message": {"content": "", "thinking": "let me see..."},
                               "done_reason": "length"})
        with self.assertRaises(F.ReasoningBudgetExhausted):
            p._chat("m", "s", "u", 32)

    def test_truly_empty_reply_is_still_empty(self):
        p, _ = self._provider({"message": {"content": ""}, "done_reason": "stop"})
        self.assertEqual(p._chat("m", "s", "u", 32), "")


class CloudReasoningKnob(unittest.TestCase):
    """Live IPlay audit 2026-08-23: 8 of 20 failed calls were OpenRouter
    thinking models exhausting the judge budget on reasoning. Routes on
    backends with a documented knob get reasoning turned down; others are
    left untouched (an unknown body field can be a fatal 400)."""

    def _route(self, base): 
        import types; return types.SimpleNamespace(base_url=base)

    def test_openrouter_and_nim_get_a_knob_others_do_not(self):
        self.assertEqual(F._reasoning_extra_body(self._route("https://openrouter.ai/api/v1")),
                         {"reasoning": {"effort": "low"}})
        self.assertEqual(F._reasoning_extra_body(self._route("https://integrate.api.nvidia.com/v1")),
                         {"chat_template_kwargs": {"thinking": False}})
        self.assertIsNone(F._reasoning_extra_body(self._route("https://api.groq.com/openai/v1")))

    def test_owner_can_restore_full_reasoning(self):
        os.environ["FLEXFACTOR_CLOUD_REASONING"] = "full"
        try:
            self.assertIsNone(F._reasoning_extra_body(self._route("https://openrouter.ai/api/v1")))
        finally:
            os.environ.pop("FLEXFACTOR_CLOUD_REASONING", None)

    def test_structured_call_carries_extra_body(self):
        captured = {}
        class _Completions:
            def create(self_inner, **kw):
                captured.update(kw)
                import types
                msg = types.SimpleNamespace(content='{"ok": 1}', reasoning_content=None)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                                             usage=None)
        p = object.__new__(F.OpenAIProvider)
        p.model = "m"; p.judge_model = "m"; p.meter = None
        p.client = type("C", (), {"chat": type("Ch", (), {"completions": _Completions()})()})()
        p._extra_body = {"reasoning": {"effort": "low"}}
        p.structured("sys", "user", {"type": "object", "properties": {"ok": {"type": "integer"}}}, max_tokens=64)
        self.assertEqual(captured.get("extra_body"), {"reasoning": {"effort": "low"}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
