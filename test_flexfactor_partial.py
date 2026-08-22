"""Adversarial tests for flexfactor_partial (partial structured-output salvage).

Run:  python test_flexfactor_partial.py
"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import flexfactor_partial as fp  # noqa: E402

KEY = fp.PARTIAL_META_KEY


def _verdict_after_guard(data) -> str:
    """The shape the runtime will use: a review with no findings is CLEAN only
    if the guard says the data may authorize it."""
    if fp.may_authorize_clean(data) and not (data or {}).get("findings"):
        return "clean"
    return "needs_work"


class SalvageTests(unittest.TestCase):
    def test_complete_json_is_not_partial(self):
        data, ev = fp.salvage_truncated_json_ex('{"findings": [{"line": 1}], "summary": "ok"}')
        self.assertIsNone(ev)
        self.assertEqual(data["summary"], "ok")
        self.assertFalse(fp.is_partial_structured(data))

    def test_truncated_mid_array_keeps_complete_leading_elements(self):
        text = ('{"findings": [{"line": 1, "title": "a"}, {"line": 2, "title": "b"}, '
                '{"line": 3, "tit')
        data, ev = fp.salvage_truncated_json_ex(text, provider="p1")
        self.assertIsNotNone(ev)
        self.assertTrue(ev.partial)
        self.assertTrue(fp.is_partial_structured(data))
        self.assertEqual([f["line"] for f in data["findings"]], [1, 2])
        self.assertEqual(ev.closers_appended, "]}")
        self.assertEqual(ev.cut_point, text.index('{"line": 3') - 2)  # after 2nd elem
        self.assertEqual(ev.provider, "p1")
        self.assertEqual(ev.raw_len, len(text))
        self.assertEqual(ev.reason, "appended_closers")
        self.assertTrue(ev.correlation_id)
        meta = fp.partial_evidence(data)
        self.assertEqual(meta["cut_point"], ev.cut_point)
        self.assertEqual(meta["closers_appended"], "]}")
        self.assertIn("must not authorize", meta["missing_scope_warning"])

    def test_truncated_mid_string_after_a_complete_element(self):
        text = '{"findings": [{"line": 1, "title": "ok"}, {"line": 2, "title": "cut he'
        data, ev = fp.salvage_truncated_json_ex(text)
        self.assertIsNotNone(ev)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["line"], 1)
        self.assertEqual(ev.closers_appended, "]}")

    def test_truncated_mid_string_with_nothing_complete_is_unsalvageable(self):
        self.assertEqual(fp.salvage_truncated_json_ex('{"findings": [{"title": "abc'),
                         (None, None))

    def test_fenced_complete_json_is_NOT_partial(self):
        # Regression: the trailing ``` used to force a false partial.
        text = '```json\n{"findings": [], "summary": "clean"}\n```'
        data, ev = fp.salvage_truncated_json_ex(text)
        self.assertIsNone(ev, "complete fenced payload must not be stamped partial")
        self.assertEqual(data, {"findings": [], "summary": "clean"})
        self.assertTrue(fp.may_authorize_clean(data))

    def test_fenced_truncated_json_is_partial(self):
        text = '```json\n{"findings": [{"line": 1}, {"line": 2}, {"li'
        data, ev = fp.salvage_truncated_json_ex(text)
        self.assertIsNotNone(ev)
        self.assertEqual([f["line"] for f in data["findings"]], [1, 2])

    def test_malformed_tail_after_complete_value_is_partial(self):
        text = '{"findings": [{"line": 1}]} and then the model kept talking'
        data, ev = fp.salvage_truncated_json_ex(text)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.reason, "dropped_incomplete_tail")
        self.assertEqual(ev.closers_appended, "")
        self.assertEqual(ev.cut_point, len('{"findings": [{"line": 1}]}'))
        self.assertEqual(data["findings"], [{"line": 1}])
        self.assertFalse(fp.may_authorize_clean(data))

    def test_bare_list_salvage_is_wrapped(self):
        data, ev = fp.salvage_truncated_json_ex('[{"a": 1}, {"b": 2}, {"c"')
        self.assertIsNotNone(ev)
        self.assertTrue(fp.is_partial_structured(data))
        self.assertEqual(fp.strip_partial_meta(data), [{"a": 1}, {"b": 2}])

    def test_no_json_at_all(self):
        self.assertEqual(fp.salvage_truncated_json_ex("nothing here"), (None, None))
        self.assertEqual(fp.salvage_truncated_json_ex(""), (None, None))
        self.assertEqual(fp.salvage_truncated_json_ex(None), (None, None))

    def test_correlation_id_and_history_are_carried(self):
        hist = [{"correlation_id": "abc", "merged_keys": ["findings"]}]
        data, ev = fp.salvage_truncated_json_ex(
            '{"findings": [{"line": 1}, {"li', correlation_id="abc",
            continuation_history=hist)
        self.assertEqual(ev.correlation_id, "abc")
        self.assertEqual(ev.continuation_history, hist)
        self.assertEqual(fp.partial_evidence(data)["correlation_id"], "abc")

    def test_backward_compatible_wrapper(self):
        self.assertTrue(fp.is_partial_structured(
            fp.salvage_truncated_json('{"findings": [{"line": 1}, {"li')))
        self.assertIsNone(fp.salvage_truncated_json("junk"))


class MetaHelpersTests(unittest.TestCase):
    def setUp(self):
        self.ev = fp.PartialSalvageEvidence(cut_point=5, provider="x", correlation_id="cid")

    def test_attach_is_partial_evidence_strip_on_dict(self):
        original = {"findings": [1]}
        data = fp.attach_partial_meta(original, self.ev)
        self.assertNotIn(KEY, original, "attach must not mutate its input")
        self.assertTrue(fp.is_partial_structured(data))
        self.assertEqual(fp.partial_evidence(data)["correlation_id"], "cid")
        self.assertTrue(fp.partial_evidence(data)["partial"])
        self.assertEqual(fp.strip_partial_meta(data), original)
        self.assertNotIn(KEY, fp.strip_partial_meta(data))

    def test_attach_strip_on_non_dict(self):
        data = fp.attach_partial_meta([1, 2], self.ev)
        self.assertTrue(fp.is_partial_structured(data))
        self.assertEqual(fp.strip_partial_meta(data), [1, 2])

    def test_evidence_partial_flag_is_forced_true(self):
        ev = fp.PartialSalvageEvidence(partial=False)
        data = fp.attach_partial_meta({}, ev)
        self.assertTrue(fp.is_partial_structured(data))

    def test_non_partial_inputs(self):
        for v in (None, "str", 3, [], {}, {"findings": []},
                  {KEY: "not a dict"}, {KEY: {"partial": False}}):
            self.assertFalse(fp.is_partial_structured(v), v)
            self.assertIsNone(fp.partial_evidence(v), v)
        for v in (None, "str", 3, [], {}, {"findings": []}):
            self.assertEqual(fp.strip_partial_meta(v), v)
        # The meta key is stripped whatever its value holds.
        self.assertEqual(fp.strip_partial_meta({KEY: "not a dict"}), {})
        self.assertEqual(fp.strip_partial_meta({KEY: {"partial": False}}), {})


class AuthorizationGuardTests(unittest.TestCase):
    def test_may_authorize_clean_false_for_partial_true_for_complete(self):
        ev = fp.PartialSalvageEvidence()
        self.assertFalse(fp.may_authorize_clean(fp.attach_partial_meta({"findings": []}, ev)))
        self.assertFalse(fp.may_authorize_clean(None))
        self.assertTrue(fp.may_authorize_clean({"findings": []}))
        self.assertTrue(fp.may_authorize_clean({"findings": [{"line": 1}]}))

    def test_MUTATION_partial_review_with_empty_findings_is_never_clean(self):
        # THE guard the runtime calls. A truncated review that salvaged to
        # findings: [] looks exactly like a clean review - and a silent
        # false-clean is the worst outcome this tool has.
        partial = fp.attach_partial_meta({"findings": []}, fp.PartialSalvageEvidence())
        self.assertEqual(_verdict_after_guard(partial), "needs_work")
        self.assertEqual(_verdict_after_guard({"findings": []}), "clean")
        self.assertEqual(_verdict_after_guard({"findings": [{"line": 1}]}), "needs_work")

    def test_MUTATION_removing_the_guard_is_detectable(self):
        # Prove the test above can fail: replace the guard with the "mutant"
        # (always True) and the same partial payload flips to clean.
        partial = fp.attach_partial_meta({"findings": []}, fp.PartialSalvageEvidence())
        real = fp.may_authorize_clean
        try:
            fp.may_authorize_clean = lambda _d: True
            self.assertEqual(_verdict_after_guard(partial), "clean",
                             "mutant guard must be observable, or the test is toothless")
        finally:
            fp.may_authorize_clean = real
        self.assertEqual(_verdict_after_guard(partial), "needs_work")

    def test_MUTATION_salvage_must_stamp_partial_or_guard_is_blind(self):
        # If salvage ever returned an un-stamped dict for a truncated payload,
        # the guard would pass it. Pin the coupling.
        data, ev = fp.salvage_truncated_json_ex('{"findings": [], "summary": "ok", "x')
        self.assertIsNotNone(ev)
        self.assertEqual(data["findings"], [])
        self.assertEqual(_verdict_after_guard(data), "needs_work")


class RefuseCleanTests(unittest.TestCase):
    def test_each_clean_verdict_flips_to_needs_work_with_residual(self):
        for v in ("clean", "CLEAN", "keep", "approve", "approved", "ready", "pass", "Ready"):
            data = fp.attach_partial_meta({"verdict": v, "residual": [{"title": "old"}]},
                                          fp.PartialSalvageEvidence())
            out = fp.refuse_clean_if_partial(data)
            self.assertEqual(out["verdict"], "needs_work", v)
            self.assertEqual(len(out["residual"]), 2, v)
            added = out["residual"][-1]
            self.assertEqual(added["title"], "partial verifier output")
            self.assertEqual(added["severity"], "high")
            self.assertTrue(added["realistic_input"] and added["affects_core"])
            self.assertEqual(added["problem"], fp.MISSING_SCOPE_WARNING)
            self.assertEqual(data["verdict"], v, "input must not be mutated")

    def test_needs_work_and_non_partial_untouched(self):
        data = fp.attach_partial_meta({"verdict": "needs_work"}, fp.PartialSalvageEvidence())
        self.assertEqual(fp.refuse_clean_if_partial(data)["verdict"], "needs_work")
        self.assertNotIn("residual", fp.refuse_clean_if_partial(data))
        complete = {"verdict": "clean"}
        self.assertIs(fp.refuse_clean_if_partial(complete), complete)
        self.assertEqual(fp.refuse_clean_if_partial(None), None)

    def test_custom_verdict_key(self):
        data = fp.attach_partial_meta({"status": "approve"}, fp.PartialSalvageEvidence())
        self.assertEqual(fp.refuse_clean_if_partial(data, "status")["status"], "needs_work")


class MergeContinuationTests(unittest.TestCase):
    def _prefix(self):
        data, ev = fp.salvage_truncated_json_ex(
            '{"summary": "s", "findings": [{"file": "a.py", "line": 1, "title": "Dup", '
            '"severity": "high"}, {"file": "a.py", "line": 2, "title": "Two", "severity": "low"}, '
            '{"file": "b.py", "li', correlation_id="cid-1")
        self.assertIsNotNone(ev)
        return data

    def test_dedups_by_element_key_and_stays_partial(self):
        prefix = self._prefix()
        cont = {"findings": [
            {"file": "a.py", "line": 1, "title": "dup", "severity": "HIGH"},  # same key
            {"file": "b.py", "line": 3, "title": "Three", "severity": "low"},
            "not a dict",
        ]}
        out = fp.merge_continuation_fragments(prefix, cont)
        self.assertEqual([f["title"] for f in out["findings"]], ["Dup", "Two", "Three"])
        self.assertEqual(out["summary"], "s")
        self.assertTrue(fp.is_partial_structured(out), "no mark_complete -> still partial")
        self.assertFalse(fp.may_authorize_clean(out))
        meta = fp.partial_evidence(out)
        self.assertEqual(meta["correlation_id"], "cid-1")
        self.assertEqual(meta["reason"], "continuation_merge_still_partial")
        self.assertEqual(meta["continuation_history"][-1]["merged_keys"], ["findings"])
        self.assertEqual(meta["continuation_history"][-1]["correlation_id"], "cid-1")
        self.assertEqual(len(meta["continuation_history"]), 1)
        # Second merge appends to history and still dedups.
        out2 = fp.merge_continuation_fragments(out, cont, correlation_id="cid-2")
        self.assertEqual(len(out2["findings"]), 3)
        self.assertEqual(len(fp.partial_evidence(out2)["continuation_history"]), 2)
        self.assertEqual(fp.partial_evidence(out2)["correlation_id"], "cid-2")

    def test_mark_complete_with_clean_fragment_clears_partial(self):
        prefix = self._prefix()
        cont = {"findings": [{"file": "b.py", "line": 3, "title": "Three", "severity": "low"}]}
        out = fp.merge_continuation_fragments(prefix, cont, mark_complete=True)
        self.assertFalse(fp.is_partial_structured(out))
        self.assertNotIn(KEY, out)
        self.assertEqual(len(out["findings"]), 3)
        self.assertTrue(fp.may_authorize_clean(out))

    def test_mark_complete_with_a_PARTIAL_fragment_stays_partial(self):
        prefix = self._prefix()
        cont, ev = fp.salvage_truncated_json_ex(
            '{"findings": [{"file": "b.py", "line": 3, "title": "Three"}, {"fi')
        self.assertIsNotNone(ev)
        out = fp.merge_continuation_fragments(prefix, cont, mark_complete=True)
        self.assertTrue(fp.is_partial_structured(out),
                        "a fragment that was itself truncated cannot complete anything")
        self.assertEqual(len(out["findings"]), 3)
        self.assertFalse(fp.may_authorize_clean(out))

    def test_non_dict_or_missing_sides(self):
        prefix = self._prefix()
        self.assertIs(fp.merge_continuation_fragments(None, {"a": 1})["a"], 1)
        self.assertIs(fp.merge_continuation_fragments(prefix, None), prefix)
        self.assertIs(fp.merge_continuation_fragments(prefix, [1, 2]), prefix)
        self.assertIs(fp.merge_continuation_fragments(prefix, "x"), prefix)

    def test_continuation_prompt_carries_correlation_id(self):
        p = fp.continuation_prompt("corr-xyz", "summary " * 1000)
        self.assertIn("correlation_id=corr-xyz", p)
        self.assertIn("Do not repeat", p)
        self.assertLess(len(p), 2600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
