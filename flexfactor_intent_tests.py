"""Purpose sight: the rotator fits the route to what the call is FOR.

Pool-first rotation decides WHICH LEDGER goes next. These tests pin the layer
in front of it: a route whose measured capabilities cannot do this call's job
is never a candidate, a reviewer is never the author's own family when any
alternative exists, unknown capability data is never treated as a failure,
and every selection records the role and the program purpose it served.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import flexfactor_rotation as R


def route(rid, pool, model=None, caps=(), source="measured", tier=R.STRONG,
          cost=R.FREE_TIER):
    model = model or rid.split("/", 1)[1]
    return R.Route(id=rid, backend=rid.split("/")[0], backend_label="", model=model,
                   wire_model=model, api="openai", base_url="http://x", pool=pool,
                   cost_class=cost, tier=tier, capabilities=tuple(caps),
                   capabilities_source=source if caps else "")


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ff-intent-")
        self.state = os.path.join(self.dir, "state.json")

    def rotator(self, routes):
        return R.Rotator(R.Catalog(routes), R.StateStore(self.state), app="test")


class FitBeforePools(_Base):
    def test_route_lacking_a_hard_need_is_not_a_candidate(self):
        rot = self.rotator([
            route("a/vision-only", "pool-a", caps=(R.CAP_VISION,)),
            route("b/coder", "pool-b", caps=(R.CAP_CODE_AUTHOR, R.CAP_STRUCTURED_JSON)),
        ])
        intent = R.CallIntent(R.ROLE_AUTHOR, (R.CAP_CODE_AUTHOR,))
        for _ in range(4):   # pool-a would be LRU-first; it must never win
            self.assertEqual(rot.next_route(tier=R.STRONG, intent=intent).route.id, "b/coder")

    def test_unknown_capabilities_are_not_a_failure(self):
        rot = self.rotator([route("a/mystery", "pool-a")])
        sel = rot.next_route(tier=R.STRONG, intent=R.CallIntent(R.ROLE_AUTHOR, (R.CAP_CODE_AUTHOR,)))
        self.assertEqual(sel.route.id, "a/mystery")
        self.assertEqual(sel.fit, "unknown")

    def test_known_fit_ranks_ahead_of_unknown_inside_a_pool(self):
        rot = self.rotator([
            route("a/mystery", "pool-a"),
            route("a/measured", "pool-a", caps=(R.CAP_CODE_AUTHOR,)),
        ])
        sel = rot.next_route(tier=R.STRONG, intent=R.CallIntent(R.ROLE_AUTHOR, (R.CAP_CODE_AUTHOR,)))
        self.assertEqual(sel.route.id, "a/measured")
        self.assertEqual(sel.fit, "measured")

    def test_no_intent_changes_nothing(self):
        rot = self.rotator([route("a/x", "pool-a", caps=(R.CAP_VISION,)), route("b/y", "pool-b")])
        sel = rot.next_route(tier=R.STRONG)
        self.assertEqual(sel.intent_role, "")
        self.assertEqual(sel.fit, "")

    def test_when_nothing_fits_the_reason_names_the_need(self):
        rot = self.rotator([route("a/vision-only", "pool-a", caps=(R.CAP_VISION,))])
        with self.assertRaises(R.RotationError) as cm:
            rot.next_route(tier=R.STRONG, intent=R.CallIntent(R.ROLE_AUTHOR, (R.CAP_CODE_AUTHOR,)))
        self.assertIn("lacks code_author for role author", str(cm.exception.reasons))


class FamilyIndependence(_Base):
    def test_reviewer_avoids_the_authors_family_when_it_can(self):
        rot = self.rotator([
            route("a/qwen3-coder:30b", "pool-a"),
            route("b/gemma4:e4b", "pool-b"),
        ])
        intent = R.CallIntent(R.ROLE_REVIEWER, avoid_family="qwen")
        for _ in range(4):
            sel = rot.next_route(tier=R.STRONG, intent=intent)
            self.assertEqual(R.model_family(sel.route.model), "gemma")
            self.assertEqual(sel.family_note, "")

    def test_when_only_the_authors_family_exists_it_still_runs_and_says_so(self):
        rot = self.rotator([route("a/qwen3-coder:30b", "pool-a"),
                            route("b/qwen2.5-coder:7b", "pool-b")])
        sel = rot.next_route(tier=R.STRONG, intent=R.CallIntent(R.ROLE_REVIEWER, avoid_family="qwen"))
        self.assertIn("independence NOT achieved", sel.family_note)

    def test_strict_independence_wins_over_a_conflicting_soft_preference(self):
        rot = self.rotator([
            route("a/gpt-5", "pool-a", model="gpt-5"),
            route("b/qwen3-coder", "pool-b", model="qwen3-coder"),
        ])
        intent = R.CallIntent(
            R.ROLE_REVIEWER, avoid_family="qwen", avoid_families=("openai",)
        )
        sel = rot.next_route(tier=R.STRONG, intent=intent)
        self.assertEqual(R.model_family(sel.route.model), "qwen")
        self.assertIn("independence NOT achieved", sel.family_note)

    def test_model_family_sees_through_route_prefixes(self):
        self.assertEqual(R.model_family("openrouter/qwen/qwen3.6-27b"), "qwen")
        self.assertEqual(R.model_family("ollama/gemma4:26b"), "gemma")
        self.assertEqual(R.model_family("nvidia_nim/openai/gpt-oss-20b"), "gpt-oss")
        self.assertEqual(R.model_family("anthropic/claude-sonnet-5"), "anthropic")
        self.assertEqual(R.model_family("ollama/muse-glimmer:30b"), "muse")


class _FakeProvider:
    def __init__(self, route): self.route = route; self.calls = []
    def structured(self, *a, **k): self.calls.append(("structured", k)); return {"ok": 1}
    def complete(self, *a, **k): return "done"


class ProviderWrapperCarriesPurpose(_Base):
    def _prov(self, routes):
        rot = self.rotator(routes)
        seen = []
        p = R.RotatingProvider(rot, _FakeProvider, tier=R.STRONG, judge_tier=R.STRONG,
                               on_route=seen.append)
        return p, seen

    def test_purpose_rides_on_every_selection(self):
        p, seen = self._prov([route("a/coder", "pool-a", caps=(R.CAP_CODE_AUTHOR,))])
        p.set_purpose("sermonsmith: exact scripture text", needs=())
        p.structured("s", "u", {}, intent=R.CallIntent(R.ROLE_AUTHOR, (R.CAP_CODE_AUTHOR,)))
        self.assertEqual(seen[-1].purpose, "sermonsmith: exact scripture text")
        self.assertEqual(seen[-1].intent_role, R.ROLE_AUTHOR)

    def test_purpose_needs_attach_to_the_vision_role_only(self):
        # Live IPlay run 2026-08-23: a program that PRODUCES video said
        # "needs vision" and every code author call was narrowed to
        # image-capable models. Purpose needs now bind only to ROLE_VISION.
        p, seen = self._prov([
            route("a/text-coder", "pool-a", caps=(R.CAP_CODE_AUTHOR,)),
            route("b/vision-coder", "pool-b", caps=(R.CAP_CODE_AUTHOR, R.CAP_VISION)),
        ])
        p.set_purpose("ui-app", needs=(R.CAP_VISION,))
        authors = set()
        for _ in range(4):
            p.structured("s", "u", {}, intent=R.CallIntent(R.ROLE_AUTHOR, (R.CAP_CODE_AUTHOR,)))
            authors.add(seen[-1].route.id)
        self.assertEqual(authors, {"a/text-coder", "b/vision-coder"})   # both rotate
        for _ in range(3):
            p.structured("s", "u", {}, intent=R.CallIntent(R.ROLE_VISION, ()))
            self.assertEqual(seen[-1].route.id, "b/vision-coder")        # only the seeing one

    def test_intent_kwarg_never_reaches_the_wire_provider(self):
        p, _ = self._prov([route("a/coder", "pool-a")])
        p.structured("s", "u", {}, intent=R.CallIntent(R.ROLE_AUTHOR))
        prov = p._cache["a/coder"]
        self.assertNotIn("intent", prov.calls[0][1])

    def test_reviewer_automatically_avoids_the_last_authors_family(self):
        p, seen = self._prov([route("a/qwen3-coder:30b", "pool-a"),
                              route("b/gemma4:e4b", "pool-b")])
        # Pin the author to qwen by making it the LRU winner first.
        p.structured("s", "u", {}, intent=R.CallIntent(R.ROLE_AUTHOR))
        author_fam = R.model_family(seen[-1].route.model)
        for _ in range(3):
            p.structured("s", "u", {}, intent=R.CallIntent(R.ROLE_REVIEWER))
            self.assertNotEqual(R.model_family(seen[-1].route.model), author_fam)


if __name__ == "__main__":
    unittest.main(verbosity=2)
