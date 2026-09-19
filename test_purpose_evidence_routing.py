"""Regression for mobile run 35324314025: citations must fail inside routing."""
from __future__ import annotations

import copy
import io
import json
import os
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

import flexfactor as ff
import flexfactor_purpose as fp


REFS = ["README.md:3", "tests/test_stats_utils.py:11", "git:log -50"]
CATALOGUE = "EXACT CITATION IDENTIFIERS (JSON strings; copy verbatim):\n"


def evidence(refs=REFS):
    return {"sources": [
        {"kind": "readme", "confidence": "high", "path_or_ref": ref,
         "excerpt": "tinystats computes arithmetic means", "why": "source"}
        for ref in refs
    ]}


def answer(refs=REFS):
    return {"purpose": "Compute arithmetic means from command-line numbers.",
            "primary_users": ["Command-line users"],
            "core_journeys": ["Supply numbers and read their arithmetic mean."],
            "acceptance_criteria": ["The mean of 2 and 4 is 3."],
            "evidence_refs": list(refs)}


class Routes:
    """Model-output seam: call the real engine validator on each route."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.rejections = []
        self.validators = []

    def structured_validated(self, system, prompt, schema, *, validator, **kwargs):
        self.calls.append((system, prompt, schema, kwargs))
        self.validators.append(validator)
        for response in self.responses:
            try:
                return validator(copy.deepcopy(response))
            except ff.StructuredOutputShapeError as exc:
                self.rejections.append(str(exc))
        raise ff.StructuredOutputShapeError("Every model returned invalid evidence")


class Fixed:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def structured(self, *args, **kwargs):
        self.calls += 1
        return copy.deepcopy(next(self.responses))


class PurposeEvidenceRoutingTests(unittest.TestCase):
    def infer(self, provider, *, refs=REFS, name="tinystats", contract=None):
        root = os.path.abspath(os.path.join("purpose-evidence-fixture", name))
        with patch.dict(ff._PURPOSE_EVIDENCE_CACHE,
                        {os.path.normcase(root): evidence(refs)}, clear=True):
            with redirect_stdout(io.StringIO()):
                return ff._infer_purpose_contract(
                    provider, name, root, authoritative_contract=contract)

    def test_decorated_production_reference_falls_through_inside_one_call(self):
        bad = answer(["[readme|high] README.md:3: headings: tinystats"])
        provider = Routes([bad, answer()])
        contract, error = self.infer(provider)
        self.assertIsNotNone(contract, error)
        self.assertEqual(contract.evidence_refs, REFS)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(provider.rejections), 1)
        self.assertIn("invented evidence", provider.rejections[0])
        self.assertFalse(contract.authored)

    def test_excerpt_suffix_is_not_silently_converted_into_evidence(self):
        provider = Routes([answer(["git:log -50: Merge pull request #1"])])
        contract, error = self.infer(provider)
        self.assertIsNone(contract)
        self.assertTrue(error)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(provider.rejections), 2)

    def test_exact_reference_catalogue_is_unambiguous(self):
        provider = Routes([answer()])
        contract, error = self.infer(provider)
        self.assertIsNotNone(contract, error)
        prompt = provider.calls[0][1]
        self.assertIn(CATALOGUE, prompt)
        listed = json.loads(prompt.split(CATALOGUE, 1)[1].split("\n\n", 1)[0])
        self.assertEqual(listed, REFS)
        self.assertIs(provider.calls[0][2], ff.PROGRAM_UNDERSTANDING_SCHEMA)

    def test_reference_catalogue_is_bounded_without_truncating_a_json_string(self):
        refs = [f"dir/{'long-name-' * 25}/{number}.py:1" for number in range(400)]
        provider = Routes([answer([refs[0]])])
        contract, error = self.infer(provider, refs=refs)
        self.assertIsNotNone(contract, error)
        prompt = provider.calls[0][1]
        self.assertIn(CATALOGUE, prompt)
        catalogue = prompt.split(CATALOGUE, 1)[1].split("\n\n", 1)[0]
        self.assertLessEqual(len(catalogue), 12000)
        listed = json.loads(catalogue)
        self.assertTrue(listed)
        self.assertLess(len(listed), len(refs))
        self.assertTrue(set(listed).issubset(refs))

    def test_scope_is_frozen_per_repository_not_mutated_globally(self):
        first = Routes([answer(["one.py:1"])])
        second = Routes([answer(["two.py:2"])])
        self.assertIsNotNone(self.infer(first, refs=["one.py:1"], name="one")[0])
        self.assertIsNotNone(self.infer(second, refs=["two.py:2"], name="two")[0])
        with self.assertRaises(ff.StructuredOutputShapeError):
            first.validators[0](answer(["two.py:2"]))
        with self.assertRaises(ff.StructuredOutputShapeError):
            second.validators[0](answer(["one.py:1"]))
        self.assertEqual(first.validators[0](answer(["one.py:1"]))["evidence_refs"],
                         ["one.py:1"])

    def test_fixed_provider_still_gets_only_bounded_corrective_retry(self):
        provider = Fixed([answer(["README.md:9999"]), answer()])
        contract, error = self.infer(provider)
        self.assertIsNotNone(contract, error)
        self.assertEqual(provider.calls, 2)

    def test_owner_contract_reference_and_authority_are_preserved(self):
        owner = fp.PurposeContract(
            name="tinystats", purpose="OWNER OUTCOME", authored=True,
            acceptance_criteria=["OWNER CRITERION"],
            source={"doc": "PURPOSE.md"})
        provider = Routes([answer(["owner-contract:PURPOSE.md"])])
        contract, error = self.infer(provider, contract=owner)
        self.assertIsNotNone(contract, error)
        self.assertEqual(contract.purpose, "OWNER OUTCOME")
        self.assertEqual(contract.acceptance_criteria, ["OWNER CRITERION"])
        self.assertTrue(contract.authored)

    def test_lossless_array_shape_recovery_still_works(self):
        response = answer()
        response["evidence_refs"] = REFS[0]
        provider = Routes([response])
        contract, error = self.infer(provider)
        self.assertIsNotNone(contract, error)
        self.assertEqual(contract.evidence_refs, [REFS[0]])

    def test_invalid_reference_after_first_hundred_is_not_ignored(self):
        refs = [f"source-{number}.py:1" for number in range(100)]
        provider = Routes([answer(refs + ["unread-secret.py:1"]), answer(refs)])
        contract, error = self.infer(provider, refs=refs)
        self.assertIsNotNone(contract, error)
        self.assertEqual(len(provider.rejections), 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn("unread-secret.py:1", contract.evidence_refs)

    def test_no_evidence_never_calls_a_provider(self):
        provider = Routes([answer()])
        contract, error = self.infer(provider, refs=[])
        self.assertIsNone(contract)
        self.assertIn("no citable", error)
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
