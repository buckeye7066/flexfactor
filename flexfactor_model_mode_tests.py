#!/usr/bin/env python3
"""Contract tests for FlexFactor's single best-available model ladder."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import flexfactor as ff
import flexfactor_rotation as rot

_ISOLATED = tempfile.mkdtemp(prefix="ffmode-")
ff.BRAIN_PATH = os.path.join(_ISOLATED, "brain.json")
ff.STATUS_PATH = os.path.join(_ISOLATED, "status.json")
ff.RUNS_PATH = os.path.join(_ISOLATED, "runs")


def _route(**kw):
    base = dict(
        id="x/y", backend="openrouter", backend_label="OpenRouter",
        model="m", wire_model="m", api="openai", base_url="",
        pool="p", auth_env="", cost_class=rot.FREE_TIER, tier=rot.STRONG,
    )
    base.update(kw)
    return rot.Route(**base)


class NormalizeTests(unittest.TestCase):
    def test_best_is_the_only_product_mode(self):
        self.assertEqual(tuple(ff.MODEL_MODES), ("best",))
        self.assertEqual(ff.normalize_model_mode("best"), "best")

    def test_every_retired_route_name_converges_on_best(self):
        for legacy in ("paid", "free", "auto", "local", "best-available"):
            self.assertEqual(ff.normalize_model_mode(legacy), "best", legacy)

    def test_unknown_and_empty_values_cannot_create_a_second_path(self):
        for value in (None, "", "cheap", "whatever"):
            self.assertEqual(ff.normalize_model_mode(value), "best")


class OneAdmissionBoundary(unittest.TestCase):
    def test_paid_subscription_metered_and_free_routes_share_one_ladder(self):
        for cost in (rot.SUBSCRIPTION, rot.PAID_METERED,
                     rot.FREE_TIER, rot.LOCAL_UNLIMITED):
            self.assertEqual(
                ff.model_mode_refusal(_route(cost_class=cost), "best"), ""
            )

    def test_builtin_catalog_exists_without_desktop_state(self):
        routes = ff._builtin_route_catalog(rot)
        self.assertTrue(any(route.cost_class == rot.PAID_METERED for route in routes))
        self.assertTrue(any(route.cost_class == rot.SUBSCRIPTION for route in routes))
        self.assertTrue(any(route.cost_class == rot.LOCAL_UNLIMITED for route in routes))

    def test_fresh_runner_builds_rotation_instead_of_fixed_provider(self):
        class Args:
            economy = False
            max_cost = 150.0

        ollama = _route(
            id="builtin/ollama", backend="ollama", api="ollama",
            model="qwen2.5-coder:7b", wire_model="qwen2.5-coder:7b",
            pool="ollama:local", cost_class=rot.LOCAL_UNLIMITED,
        )
        with mock.patch.object(rot, "load_catalog", return_value=None), \
             mock.patch.object(ff, "_builtin_route_catalog", return_value=[ollama]), \
             mock.patch.object(ff, "_ollama_route_health", return_value=(True, "ok")):
            provider = ff._build_rotating_provider(Args(), None, "best", quiet=True)
        self.assertIsInstance(provider, rot.RotatingProvider)
        self.assertTrue(provider._paid_first)

    def test_best_available_banner_does_not_advertise_an_ignored_pin(self):
        class Args:
            economy = False
            max_cost = 150.0

        ollama = _route(
            id="builtin/ollama", backend="ollama", api="ollama",
            model="qwen2.5-coder:7b", wire_model="qwen2.5-coder:7b",
            pool="ollama:local", cost_class=rot.LOCAL_UNLIMITED,
        )
        output = io.StringIO()
        with mock.patch.object(rot, "load_catalog", return_value=None), \
             mock.patch.object(ff, "_builtin_route_catalog", return_value=[ollama]), \
             mock.patch.object(ff, "_ollama_route_health", return_value=(True, "ok")), \
             mock.patch.object(rot.StateStore, "get_pin", return_value="legacy/free"), \
             mock.patch.dict(os.environ, {"AI_ROTATE_PIN": "legacy/free"}), \
             contextlib.redirect_stderr(output):
            provider = ff._build_rotating_provider(Args(), None, "best", quiet=False)
        self.assertIsInstance(provider, rot.RotatingProvider)
        self.assertNotIn("pinned to", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
