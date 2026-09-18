"""Model/queue regressions retained from the stale-runtime incident.

Source update safety is covered with executable Git fixtures in
``test_source_app_update.py``. The current policy preserves local work and
requires an explicit Update choice; it never automatically stashes or resets.
"""
from __future__ import annotations

import copy
import inspect
import os
import tempfile
import unittest
from unittest import mock

import flexfactor as ff
import flexfactor_rotation as rotation


class StaleRuntimeRecurrenceTests(unittest.TestCase):
    def test_program_understanding_shape_failure_still_rotates_inside_same_call(self):
        valid = {
            "purpose": "Compute arithmetic means from supplied numbers.",
            "primary_users": ["Command-line users"],
            "core_journeys": ["Supply numbers and read the mean."],
            "acceptance_criteria": ["The mean of 2 and 4 is 3."],
            "evidence_refs": ["README.md:3"],
        }
        for invalid_refs in (123, ["[readme|high] README.md:3: headings: tinystats"]):
            with self.subTest(evidence_refs=invalid_refs), tempfile.TemporaryDirectory() as root:
                invalid = dict(valid, evidence_refs=invalid_refs)
                routes = [rotation.Route.from_json({
                    "id": name, "backend": "fixture", "model": name,
                    "pool": name, "api": "ollama", "tier": rotation.LIGHT,
                    "cost_class": rotation.LOCAL_UNLIMITED,
                }) for name in ("bad", "good")]
                store = rotation.StateStore(os.path.join(root, "routing.json"))
                # Force the malformed backend first without replacing the
                # real selector, error classifier, cooldowns, or retry loop.
                store.update(lambda state: state["pools"].update({
                    "good": {"calls": 1, "last_used_at": 1.0},
                }))
                visited = []
                failures = []

                def factory(route):
                    backend = mock.Mock()

                    def structured(*args, **kwargs):
                        visited.append(route.id)
                        return copy.deepcopy(invalid if route.id == "bad" else valid)

                    backend.structured.side_effect = structured
                    return backend

                provider = rotation.RotatingProvider(
                    rotation.Rotator(rotation.Catalog(routes), store=store),
                    factory, tier=rotation.LIGHT,
                    on_error=lambda route, error: failures.append(error),
                )
                key = os.path.normcase(os.path.abspath(root))
                evidence = {"sources": [{
                    "kind": "readme", "confidence": "high",
                    "path_or_ref": "README.md:3", "excerpt": "Compute arithmetic means.",
                }]}
                with mock.patch.dict(ff._PURPOSE_EVIDENCE_CACHE, {key: evidence}, clear=True):
                    with mock.patch.object(
                            provider, "structured_validated",
                            wraps=provider.structured_validated) as call:
                        contract, error = ff._infer_purpose_contract(provider, "tinystats", root)
                self.assertIsNotNone(contract, error)
                self.assertEqual(contract.evidence_refs, ["README.md:3"])
                self.assertEqual(call.call_count, 1)
                self.assertEqual(visited, ["bad", "good"])
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], ff.StructuredOutputShapeError)

    def test_unavailable_copilot_model_keeps_other_subscription_models_eligible(self):
        from providers.cli_provider import CliUnavailable
        with tempfile.TemporaryDirectory() as root:
            catalog = ff._builtin_route_catalog(rotation)
            routes = [r for r in catalog if r.api == "copilot-cli"]
            self.assertGreaterEqual(len(routes), 2,
                                    "one unavailable model must not eliminate Copilot")
            self.assertTrue(all(r.model != "auto" and r.model == r.wire_model for r in routes))
            self.assertGreaterEqual(len({rotation.model_family(r.model) for r in routes}), 2)
            visited = []
            def factory(route):
                backend = mock.Mock()
                def complete(*args, **kwargs):
                    visited.append(route.id)
                    if route.id == routes[0].id:
                        raise CliUnavailable('Error: Model "claude-sonnet-4.6" from --model flag is not available.')
                    return "verified answer"
                backend.complete.side_effect = complete
                return backend
            provider = rotation.RotatingProvider(rotation.Rotator(
                rotation.Catalog(routes), store=rotation.StateStore(os.path.join(root, "state.json"))),
                factory, tier=rotation.STRONG)
            self.assertEqual(provider.complete("Review the provided source"), "verified answer")
            self.assertEqual(visited[:2], [routes[0].id, routes[1].id])

    def test_fresh_runner_uses_current_concrete_copilot_models(self):
        routes = [r for r in ff._builtin_route_catalog(rotation) if r.api == "copilot-cli"]
        models = {r.model for r in routes}
        self.assertTrue({"claude-sonnet-5", "gpt-5.6-terra", "gpt-5.6-luna"}.issubset(models))
        self.assertNotIn("auto", models)
        self.assertTrue(all(r.model == r.wire_model for r in routes))

    def test_current_queue_preflight_resolves_targets_before_work_when_dashboard_active(self):
        source = inspect.getsource(ff.run_audit)
        self.assertIn("resolved_targets", source)
        self.assertIn("if prompts or session_prompt or getattr(args, \"dashboard\", True)", source)
        self.assertLess(source.index("resolved_targets"), source.index("audit_one_program"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

