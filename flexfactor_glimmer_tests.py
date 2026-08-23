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

import os
import sys
import types
import unittest

sys.argv = sys.argv[:1]          # flexfactor parses argv at import time
import flexfactor as F           # noqa: E402


def route(rid: str, model: str, api: str = "ollama",
          base: str = "http://127.0.0.1:11434"):
    """Minimal stand-in for an aitime.catalog Route row."""
    r = types.SimpleNamespace()
    r.id, r.model, r.api, r.base_url = rid, model, api, base
    r.wire_model, r.auth_env, r.pool = model, None, "local:ollama"
    return r


class ExclusionMatching(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXCLUDE", None)

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

    tearDown = setUp

    def test_filter_rejects_local_glimmer(self):
        why = F._route_unusable_reason(
            route("ollama/muse-glimmer:30b", "muse-glimmer:30b"), "auto")
        self.assertIn("excluded from rotation", why)

    def test_filter_admits_a_normal_local_route(self):
        self.assertEqual(
            F._route_unusable_reason(
                route("ollama/qwen3-coder:30b", "qwen3-coder:30b"), "auto"),
            "")

    def test_filter_admits_the_free_cloud_glimmer(self):
        r = route("nvidia_nim/meta/muse-glimmer-30b", "meta/muse-glimmer-30b",
                  api="openai", base="https://integrate.api.nvidia.com/v1")
        self.assertEqual(F._route_unusable_reason(r, "auto"), "")



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


if __name__ == "__main__":
    unittest.main(verbosity=2)
