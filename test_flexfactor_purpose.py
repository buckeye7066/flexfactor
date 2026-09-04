"""Tests for purpose-discovery evidence in flexfactor_purpose.py.

Stdlib unittest; builds a real temp repo (git init + commits + tag) and drives
`gather_purpose_evidence` / `purpose_confidence` / `mutation_authorized_by_purpose`
/ `render_purpose_evidence_block` / `inferred_contract(evidence=...)`.
The `gh` CLI is never invoked: every test injects a fake `gh_runner`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import flexfactor_purpose as fp


def _w(root: str, rel: str, body: str) -> None:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(textwrap.dedent(body).lstrip("\n"))


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=60)


GIT_AVAILABLE = shutil.which("git") is not None


def _real_git(args, cwd):
    """The test scaffold's own git runner.

    `gather_purpose_evidence` no longer owns a default runner (that default was
    a raw `subprocess.run` outside FlexFactor's command chokepoint - the g-5
    containment hole). Production injects a runner backed by `flexfactor._git`;
    these unit tests, which are not the product, inject this one.
    """
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                        encoding="utf-8", errors="replace", timeout=60)
    return cp.stdout or "" if cp.returncode == 0 else None


def _fake_gh(args, cwd):
    if args[0] == "pr":
        return json.dumps([{"number": 7, "title": "Add checkout flow", "state": "MERGED"},
                           {"number": 9, "title": "Stripe webhook retries", "state": "OPEN"}])
    if args[0] == "issue":
        return json.dumps([{"number": 3, "title": "Invoices not emailed", "state": "OPEN"}])
    return None


def _absent_gh(args, cwd):
    return None


def build_full_fixture(root: str, *, readme_kind: str = "web app") -> None:
    """A small but complete Express+Prisma+Stripe web app repo."""
    _w(root, "package.json", """
    {
      "name": "invoicer",
      "description": "A web app that lets small businesses send invoices and collect card payments",
      "scripts": {"build": "tsc", "test": "vitest run", "start": "node dist/server.js"},
      "dependencies": {"express": "^4.19.0", "@prisma/client": "^5.0.0", "stripe": "^14.0.0"},
      "devDependencies": {"vitest": "^1.0.0", "prisma": "^5.0.0"}
    }
    """)
    _w(root, "README.md", f"""
    # Invoicer

    Invoicer is a {readme_kind} that lets small businesses send invoices and collect
    card payments through Stripe. It automatically emails reminders for overdue invoices.

    ## Features

    - Generates PDF invoices
    - Tracks payment status

    ```bash
    npm start
    ```
    """)
    _w(root, "docs/architecture.md", """
    # Architecture

    The server provides a REST API consumed by the dashboard.
    """)
    _w(root, "tests/invoices.test.ts", """
    import { describe, it, expect } from "vitest";
    describe("invoice lifecycle", () => {
      it("creates an invoice and marks it paid after a Stripe webhook", () => {
        expect(1).toBe(1);
      });
    });
    """)
    _w(root, "prisma/schema.prisma", """
    datasource db { provider = "postgresql" url = env("DATABASE_URL") }
    model Invoice {
      id     Int    @id @default(autoincrement())
      amount Int
    }
    model Customer {
      id   Int    @id
      name String
    }
    """)
    _w(root, "src/routes.ts", """
    import express from "express";
    const router = express.Router();
    router.get("/invoices", list);
    router.post("/invoices", create);
    router.post("/webhooks/stripe", webhook);
    export default router;
    """)
    _w(root, ".env.example", """
    DATABASE_URL=postgres://localhost/invoicer
    STRIPE_SECRET_KEY=sk_test_placeholder
    SENDGRID_API_KEY=
    """)
    _w(root, "Dockerfile", """
    FROM node:20
    CMD ["node", "dist/server.js"]
    """)
    _w(root, ".github/workflows/ci.yml", """
    name: CI
    on: [push]
    jobs: {}
    """)
    # node_modules must be skipped entirely.
    _w(root, "node_modules/leftpad/package.json",
       '{"name": "leftpad", "description": "a library for padding"}')
    _w(root, "node_modules/leftpad/README.md", "# leftpad\n\nA library that pads.\n")


def git_init_with_history(root: str) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    for i, msg in enumerate(("initial invoicer scaffold", "add stripe webhook route",
                             "email overdue reminders")):
        _w(root, f"notes{i}.txt", f"{i}\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", msg)
    _git(root, "tag", "v0.1.0")


class _TempRepo(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ffpurpose-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class PurposeContractV2Tests(_TempRepo):
    @staticmethod
    def _claim(claim_id: str, text: str, confidence: str = "verified",
               refs: list[int] | None = None) -> dict:
        return {
            "id": claim_id,
            "text": text,
            "confidence": confidence,
            "evidence_refs": [0] if refs is None else refs,
        }

    @staticmethod
    def _evidence(**overrides) -> dict:
        record = {
            "kind": "source",
            "locator": "src/receipt.py",
            "content_hash": "a" * 64,
            "observed_at": "2026-09-04T02:03:04Z",
        }
        record.update(overrides)
        return record

    def _contract(self, **overrides) -> dict:
        contract = {
            "schema": "flexfactor.purpose_contract.v2",
            "name": "Receipt Maker",
            "purpose": "Turn invoices into receipts.",
            "users": [self._claim("u-1", "Bookkeepers")],
            "outcomes": [self._claim("o-1", "A valid receipt exists.")],
            "workflows": [
                self._claim("w-1", "Upload an invoice and export its receipt."),
            ],
            "invariants": [
                self._claim("i-1", "Invoice totals remain unchanged."),
            ],
            "acceptance_criteria": ["A receipt can be exported."],
            "evidence": [self._evidence()],
        }
        contract.update(overrides)
        return contract

    def test_structured_users_and_workflows_populate_runtime_contract(self):
        contract_doc = self._contract(
            users=[
                self._claim("u-1", "Bookkeepers"),
                self._claim("u-2", "Business owners", "supported"),
            ],
            workflows=[
                self._claim("w-1", "Upload an invoice and export its receipt."),
            ],
        )
        _w(self.root, ".flexfactor-purpose.json", json.dumps(contract_doc))

        contract = fp.find_contract("Receipt Maker", self.root, registry={})

        self.assertIsNotNone(contract)
        self.assertTrue(contract.authored)
        self.assertEqual(contract.primary_users, ["Bookkeepers", "Business owners"])
        self.assertEqual(
            contract.core_journeys,
            ["Upload an invoice and export its receipt."],
        )

    def test_required_v2_claims_and_evidence_reach_mutation_prompt(self):
        contract_doc = self._contract(
            outcomes=[self._claim(
                "o-safety", "Only an owner-approved receipt is published.",
                "supported",
            )],
            invariants=[self._claim(
                "i-safety", "Never change an invoice total.",
            )],
            evidence=[self._evidence(
                locator="policy/receipt-safety.md",
                content_hash="b" * 64,
                observed_at="2026-09-04T05:06:07+00:00",
                excerpt="Owner approval and immutable totals are required.",
            )],
        )

        contract = fp._contract_from_registry(contract_doc)

        self.assertIsNotNone(contract)
        self.assertEqual(contract.structured_claims["outcomes"],
                         contract_doc["outcomes"])
        self.assertEqual(contract.structured_claims["invariants"],
                         contract_doc["invariants"])
        self.assertEqual(contract.contract_evidence, contract_doc["evidence"])
        prompt = contract.prompt_block(max_chars=10000)
        self.assertIn("REQUIRED OUTCOMES", prompt)
        self.assertIn("Only an owner-approved receipt is published.", prompt)
        self.assertIn("confidence=supported", prompt)
        self.assertIn("MUTATION INVARIANTS", prompt)
        self.assertIn("Never change an invoice total.", prompt)
        self.assertIn("evidence_refs=0", prompt)
        self.assertIn("locator=policy/receipt-safety.md", prompt)
        self.assertIn("content_hash=" + "b" * 64, prompt)
        self.assertIn("observed_at=2026-09-04T05:06:07+00:00", prompt)
        self.assertIn("excerpt=Owner approval", prompt)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            contract.prompt_block(max_chars=200)
        serialized = contract.to_dict()
        self.assertEqual(serialized["structured_claims"]["outcomes"],
                         contract_doc["outcomes"])
        self.assertEqual(serialized["contract_evidence"],
                         contract_doc["evidence"])

    def test_oversized_required_context_never_gains_mutation_authority(self):
        oversized_cases = (
            {"purpose": "x" * 12000},
            {"users": [self._claim("u-huge", "x" * 12000)]},
            {"acceptance_criteria": ["x" * 12000]},
        )
        for oversized in oversized_cases:
            with self.subTest(field=next(iter(oversized))):
                contract = fp.find_contract("Receipt Maker", registry={
                    "receipt-maker": self._contract(**oversized),
                })

                self.assertIsNone(contract)
                confidence = fp.purpose_confidence(contract, {})
                self.assertEqual(confidence, "unresolved")
                self.assertFalse(fp.mutation_authorized_by_purpose(confidence)[0])

    def test_unresolved_contradiction_blocks_owner_authority(self):
        contradiction = self._claim(
            "x-delete", "Owner policy forbids deleting receipt records.",
        )
        contract = fp.find_contract("Receipt Maker", registry={
            "receipt-maker": self._contract(
                outcomes=[self._claim(
                    "o-delete", "Old receipt records are deleted.",
                )],
                contradictions=[contradiction],
            ),
        })

        self.assertIsNone(contract)
        confidence = fp.purpose_confidence(contract, {})
        self.assertEqual(confidence, "unresolved")
        self.assertFalse(fp.mutation_authorized_by_purpose(confidence)[0])

    def test_malformed_v2_claim_rejects_the_entire_contract(self):
        for unsafe in (
            7,
            {"id": "u-2", "text": "   ", "confidence": "verified",
             "evidence_refs": [0]},
            {"id": "u-2", "text": "Operator", "confidence": "verified"},
            {"id": "", "text": "Operator", "confidence": "verified",
             "evidence_refs": [0]},
            {**self._claim("u-2", "Operator"), "unexpected": True},
        ):
            with self.subTest(unsafe=unsafe):
                contract = fp._contract_from_registry(self._contract(
                    users=[self._claim("u-1", "Bookkeeper"), unsafe],
                ))
                self.assertIsNone(contract)

    def test_claim_references_must_be_nonempty_and_resolve_to_evidence(self):
        for unsafe_refs in ([], [-1], [1], [True], [0, 9]):
            with self.subTest(evidence_refs=unsafe_refs):
                contract = fp._contract_from_registry(self._contract(
                    workflows=[self._claim(
                        "w-1", "Run the complete flow", refs=unsafe_refs)],
                ))
                self.assertIsNone(contract)

    def test_every_claim_section_is_reference_checked(self):
        for section in (
                "current_behavior", "aspirations", "users", "outcomes",
                "workflows", "invariants", "contradictions", "gaps"):
            with self.subTest(section=section):
                contract = fp._contract_from_registry(self._contract(**{
                    section: [self._claim("unsafe", "Unsupported", refs=[7])],
                }))
                self.assertIsNone(contract)
        self.assertIsNone(fp._contract_from_registry(self._contract(
            resolved_contradictions=[{
                **self._claim("x-1", "Conflict", refs=[]),
                "resolution": "Implementation is authoritative.",
            }],
        )))

    def test_complete_v2_shape_is_required_before_it_is_authored(self):
        for field in (
                "schema", "name", "purpose", "users", "outcomes", "workflows",
                "invariants", "acceptance_criteria", "evidence"):
            with self.subTest(missing=field):
                candidate = self._contract()
                candidate.pop(field)
                self.assertIsNone(fp._contract_from_registry(candidate))
        for field in ("users", "outcomes", "workflows", "invariants",
                      "acceptance_criteria", "evidence"):
            with self.subTest(empty=field):
                self.assertIsNone(fp._contract_from_registry(
                    self._contract(**{field: []})))

    def test_evidence_records_are_validated_before_claims_can_use_them(self):
        unsafe_records = (
            self._evidence(kind="opinion"),
            self._evidence(locator=" "),
            self._evidence(content_hash="ABC"),
            self._evidence(observed_at="2026-09-04"),
            self._evidence(observed_at="2026-02-30T02:03:04Z"),
            {**self._evidence(), "unexpected": True},
        )
        for unsafe in unsafe_records:
            with self.subTest(evidence=unsafe):
                self.assertIsNone(fp._contract_from_registry(
                    self._contract(evidence=[unsafe])))

    def test_unhashable_enum_values_reject_instead_of_raising(self):
        for confidence in ([], {}, ["verified"]):
            with self.subTest(confidence=confidence):
                unsafe_claim = self._claim("u-1", "Bookkeeper")
                unsafe_claim["confidence"] = confidence
                self.assertIsNone(fp._contract_from_registry(
                    self._contract(users=[unsafe_claim])))
        for kind in ([], {}, ["source"]):
            with self.subTest(kind=kind):
                self.assertIsNone(fp._contract_from_registry(
                    self._contract(evidence=[self._evidence(kind=kind)])))

    def test_optional_sections_and_additional_properties_are_fail_closed(self):
        self.assertIsNone(fp._contract_from_registry(self._contract(
            aspirations=[{"id": "asp-1"}],
        )))
        self.assertIsNone(fp._contract_from_registry(self._contract(
            invented_authority=True,
        )))
        self.assertIsNone(fp._contract_from_registry(self._contract(
            acceptance_criteria=["   "],
        )))

    def test_non_authoritative_required_claim_never_unlocks_contract(self):
        for confidence in ("inferred", "contradicted", "unknown"):
            with self.subTest(confidence=confidence):
                contract = fp._contract_from_registry(self._contract(
                    outcomes=[self._claim(
                        "o-1", "An uncertain outcome", confidence)],
                ))
                self.assertIsNone(contract)

    def test_invalid_in_repo_v2_falls_through_to_valid_legacy_registry(self):
        invalid = self._contract()
        invalid.pop("invariants")
        _w(self.root, ".flexfactor-purpose.json", json.dumps(invalid))
        registry = {
            "receipt-maker": {
                "name": "Receipt Maker",
                "purpose": "Use the owner registry purpose.",
                "primary_users": ["Operators"],
                "core_journeys": ["Complete the trusted flow"],
                "acceptance_criteria": ["The trusted flow completes."],
            },
        }

        contract = fp.find_contract("Receipt Maker", self.root, registry=registry)

        self.assertIsNotNone(contract)
        self.assertEqual(contract.purpose, "Use the owner registry purpose.")
        self.assertEqual(contract.primary_users, ["Operators"])

    def test_invalid_registry_v2_cannot_gain_authored_mutation_authority(self):
        invalid = self._contract()
        invalid["users"][0]["evidence_refs"] = []

        contract = fp.find_contract(
            "Receipt Maker", registry={"receipt-maker": invalid})

        self.assertIsNone(contract)
        confidence = fp.purpose_confidence(contract, {})
        self.assertEqual(confidence, "unresolved")
        self.assertFalse(fp.mutation_authorized_by_purpose(confidence)[0])

    def test_invalid_exact_registry_record_blocks_unrelated_alias_fallback(self):
        invalid_exact = self._contract(name="Receipt Maker")
        invalid_exact["evidence"] = []
        unrelated_alias = {
            "name": "Another Product",
            "purpose": "Perform unrelated owner work.",
            "primary_users": ["Other users"],
            "core_journeys": ["Run another workflow"],
            "aliases": ["receipt-maker"],
        }

        contract = fp.find_contract("Receipt Maker", registry={
            "receipt-maker": invalid_exact,
            "another-product": unrelated_alias,
        })

        self.assertIsNone(contract)
        confidence = fp.purpose_confidence(contract, {})
        self.assertEqual(confidence, "unresolved")
        self.assertFalse(fp.mutation_authorized_by_purpose(confidence)[0])

    def test_invalid_alias_match_blocks_later_valid_alias(self):
        invalid_alias = self._contract(
            name="Damaged Receipt Contract",
            aliases=["receipt bus"],
            evidence=[],
        )
        unrelated_valid_alias = {
            "name": "Another Product",
            "purpose": "Perform unrelated owner work.",
            "primary_users": ["Other users"],
            "core_journeys": ["Run another workflow"],
            "aliases": ["receipt-bus"],
        }

        contract = fp.find_contract("Receipt Bus", registry={
            "damaged-contract": invalid_alias,
            "another-product": unrelated_valid_alias,
        })

        self.assertIsNone(contract)
        confidence = fp.purpose_confidence(contract, {})
        self.assertEqual(confidence, "unresolved")
        self.assertFalse(fp.mutation_authorized_by_purpose(confidence)[0])

    def test_duplicate_valid_alias_is_ambiguous_and_unresolved(self):
        contract = fp.find_contract("Receipt Bus", registry={
            "first-product": {
                "name": "First Product",
                "purpose": "First purpose.",
                "aliases": ["receipt-bus"],
            },
            "second-product": {
                "name": "Second Product",
                "purpose": "Second purpose.",
                "aliases": ["receipt bus"],
            },
        })

        self.assertIsNone(contract)

    def test_invalid_path_match_blocks_later_valid_path(self):
        invalid_path = self._contract(
            name="Damaged Path Contract",
            evidence=[],
            local_path=self.root,
        )
        unrelated_valid_path = {
            "name": "Another Product",
            "purpose": "Perform unrelated owner work.",
            "primary_users": ["Other users"],
            "core_journeys": ["Run another workflow"],
            "local_path": self.root,
        }

        contract = fp.find_contract("Unmatched Name", self.root, registry={
            "damaged-contract": invalid_path,
            "another-product": unrelated_valid_path,
        })

        self.assertIsNone(contract)
        confidence = fp.purpose_confidence(contract, {})
        self.assertEqual(confidence, "unresolved")
        self.assertFalse(fp.mutation_authorized_by_purpose(confidence)[0])

    def test_one_valid_alias_or_path_match_still_resolves(self):
        valid = {
            "name": "Receipt Maker",
            "purpose": "Create the requested receipt.",
            "primary_users": ["Bookkeepers"],
            "core_journeys": ["Export one receipt"],
            "aliases": ["receipt-bus"],
            "local_path": self.root,
        }

        alias_contract = fp.find_contract(
            "Receipt Bus", registry={"receipt-maker": valid})
        path_contract = fp.find_contract(
            "Unmatched Name", self.root, registry={"receipt-maker": valid})

        self.assertEqual(alias_contract.purpose, "Create the requested receipt.")
        self.assertEqual(path_contract.purpose, "Create the requested receipt.")

    def test_checked_in_contract_passes_runtime_validator(self):
        with open(os.path.join(os.path.dirname(fp.__file__),
                               ".flexfactor-purpose.json"), encoding="utf-8") as fh:
            checked_in = json.load(fh)
        self.assertTrue(fp._v2_contract_is_authoritative(checked_in))

    def test_schema_requires_nonempty_evidence_references(self):
        with open(os.path.join(os.path.dirname(fp.__file__), "docs",
                               "purpose-contract.schema.json"),
                  encoding="utf-8") as fh:
            schema = json.load(fh)
        self.assertEqual(schema["$defs"]["claim"]["properties"]
                         ["evidence_refs"]["minItems"], 1)
        self.assertEqual(schema["$defs"]["resolved_claim"]["properties"]
                         ["evidence_refs"]["minItems"], 1)

    def test_legacy_lists_remain_supported_but_are_also_atomic(self):
        contract = fp._contract_from_registry({
            "name": "Legacy",
            "purpose": "Keep compatibility.",
            "primary_users": [" Operators ", "Owners"],
            "core_journeys": ["Run the complete flow"],
        })
        self.assertIsNotNone(contract)
        self.assertEqual(contract.primary_users, ["Operators", "Owners"])
        self.assertEqual(contract.core_journeys, ["Run the complete flow"])

        malformed = fp._contract_from_registry({
            "name": "Legacy",
            "purpose": "Keep compatibility.",
            "primary_users": ["Operator", 7],
        })
        self.assertIsNotNone(malformed)
        self.assertEqual(malformed.primary_users, [])


class GatherEvidenceFullFixtureTests(_TempRepo):
    def setUp(self):
        super().setUp()
        build_full_fixture(self.root)
        if GIT_AVAILABLE:
            git_init_with_history(self.root)
        self.ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_fake_gh)

    def _kinds(self):
        return {s["kind"] for s in self.ev["sources"]}

    def test_every_section_is_populated_with_citations(self):
        ev = self.ev
        for key in ("sources", "contradictions", "unknowns", "integrations", "schemas",
                    "routes", "history", "deploy", "product_claims"):
            self.assertIn(key, ev)
        self.assertTrue(ev["sources"])
        for s in ev["sources"]:
            self.assertTrue(s["path_or_ref"], s)
            self.assertIn(s["confidence"], ("high", "medium", "low"), s)
            self.assertTrue(s["why"], s)
            self.assertLessEqual(len(s["excerpt"]), 600)
            # file-backed citations carry a line; git/gh refs carry their ref
            self.assertTrue(":" in s["path_or_ref"], s["path_or_ref"])
        kinds = self._kinds()
        for want in ("manifest", "readme", "doc", "test", "schema", "route", "env",
                     "deploy", "ci", "pr", "issue"):
            self.assertIn(want, kinds)
        self.assertTrue(ev["integrations"])
        self.assertTrue(ev["schemas"])
        self.assertTrue(ev["routes"])
        self.assertTrue(ev["product_claims"])
        self.assertTrue(ev["deploy"]["targets"])
        self.assertTrue(ev["deploy"]["ci"])
        for item in ev["integrations"] + ev["schemas"] + ev["routes"] + ev["product_claims"] \
                + ev["deploy"]["targets"] + ev["deploy"]["ci"]:
            self.assertIn("path_or_ref", item)
            self.assertTrue(item["path_or_ref"])

    def test_manifest_description_is_cited_with_its_line(self):
        m = [s for s in self.ev["sources"] if s["kind"] == "manifest"
             and "send invoices" in s["excerpt"]]
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["confidence"], "high")
        path, line = m[0]["path_or_ref"].rsplit(":", 1)
        self.assertEqual(path, "package.json")
        self.assertEqual(int(line), 3)

    def test_readme_paragraph_skips_code_and_is_high(self):
        r = [s for s in self.ev["sources"] if s["kind"] == "readme"]
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["confidence"], "high")
        self.assertIn("Invoicer is a web app", r[0]["excerpt"])
        self.assertNotIn("npm start", r[0]["excerpt"])
        self.assertEqual(r[0]["path_or_ref"], "README.md:3")

    def test_tests_cite_describe_titles(self):
        t = [s for s in self.ev["sources"] if s["kind"] == "test"]
        self.assertEqual(len(t), 1)
        self.assertIn("invoice lifecycle", t[0]["excerpt"])
        self.assertIn("marks it paid after a Stripe webhook", t[0]["excerpt"])
        self.assertEqual(t[0]["confidence"], "high")
        self.assertTrue(t[0]["path_or_ref"].startswith("tests/invoices.test.ts:"))

    def test_prisma_models_and_express_routes(self):
        names = {s["name"] for s in self.ev["schemas"]}
        self.assertEqual(names, {"Invoice", "Customer"})
        self.assertTrue(all(s["path_or_ref"].startswith("prisma/schema.prisma:")
                            for s in self.ev["schemas"]))
        routes = {(r["method"], r["path"]) for r in self.ev["routes"]}
        self.assertEqual(routes, {("GET", "/invoices"), ("POST", "/invoices"),
                                  ("POST", "/webhooks/stripe")})
        self.assertTrue(all(r["path_or_ref"].startswith("src/routes.ts:")
                            for r in self.ev["routes"]))

    def test_env_keys_and_deps_become_integrations(self):
        names = {i["name"] for i in self.ev["integrations"]}
        self.assertIn("Stripe", names)
        self.assertIn("SendGrid", names)
        self.assertIn("Express", names)
        self.assertIn("Prisma ORM", names)
        via_env = [i for i in self.ev["integrations"] if i["via"] == "env key STRIPE_SECRET_KEY"]
        self.assertEqual(len(via_env), 1)
        self.assertEqual(via_env[0]["path_or_ref"], ".env.example:2")

    def test_deploy_and_ci_detected(self):
        targets = {t["target"] for t in self.ev["deploy"]["targets"]}
        self.assertIn("Docker", targets)
        self.assertEqual(self.ev["deploy"]["ci"][0]["workflow"], "CI")

    def test_node_modules_is_skipped(self):
        for s in self.ev["sources"]:
            self.assertNotIn("node_modules", s["path_or_ref"])
        self.assertFalse(any("leftpad" in s["excerpt"] for s in self.ev["sources"]))

    @unittest.skipUnless(GIT_AVAILABLE, "git not installed")
    def test_git_history_commits_tags_branches(self):
        h = self.ev["history"]
        self.assertEqual(h["commits"][0], "email overdue reminders")
        self.assertEqual(len(h["commits"]), 3)
        self.assertEqual(h["tags"], ["v0.1.0"])
        self.assertTrue(h["branches"])
        kinds = self._kinds()
        self.assertIn("git-commit", kinds)
        self.assertIn("git-tag", kinds)
        refs = {s["path_or_ref"] for s in self.ev["sources"] if s["kind"].startswith("git-")}
        self.assertIn("git:log -50", refs)
        self.assertIn("git:tag", refs)

    def test_fake_gh_prs_and_issues_are_cited(self):
        h = self.ev["history"]
        self.assertEqual([p["number"] for p in h["prs"]], [7, 9])
        self.assertEqual([i["number"] for i in h["issues"]], [3])
        refs = {s["path_or_ref"] for s in self.ev["sources"] if s["kind"] in ("pr", "issue")}
        self.assertEqual(refs, {"gh:pr #7", "gh:pr #9", "gh:issue #3"})
        self.assertFalse(any("GitHub pull requests unavailable" in u for u in self.ev["unknowns"]))

    def test_no_contradiction_when_manifest_and_readme_agree(self):
        self.assertEqual(self.ev["contradictions"], [])

    def test_confidence_is_strongly_inferred_and_authorizes_mutation(self):
        conf = fp.purpose_confidence(None, self.ev)
        self.assertEqual(conf, "strongly-inferred")
        ok, why = fp.mutation_authorized_by_purpose(conf)
        self.assertTrue(ok, why)

    def test_unknowns_always_name_the_unobservable(self):
        self.assertTrue(any("not observable offline" in u for u in self.ev["unknowns"]))


class GhAbsentTests(_TempRepo):
    def test_gh_absent_records_unknowns_and_never_crashes(self):
        build_full_fixture(self.root)
        ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_absent_gh)
        self.assertEqual(ev["history"]["prs"], [])
        self.assertEqual(ev["history"]["issues"], [])
        self.assertTrue(any("GitHub pull requests unavailable" in u for u in ev["unknowns"]))
        self.assertTrue(any("GitHub issues unavailable" in u for u in ev["unknowns"]))
        # Not a git repo at all -> history is an unknown, not a crash.
        self.assertTrue(any("not a git repository" in u for u in ev["unknowns"]))

    def test_the_module_owns_no_process_launcher_at_all(self):
        """g-5. The default runners were raw `subprocess.run` calls, outside
        FlexFactor's command chokepoint - so a purpose gather could start a
        process that no policy classified and no containment claim covered."""
        self.assertFalse(hasattr(fp, "_default_gh_runner"))
        self.assertFalse(hasattr(fp, "_default_git_runner"))
        with open(fp.__file__, encoding="utf-8") as fh:
            source = fh.read()
        for banned in ("subprocess.run(", "subprocess.Popen(", "os.system(",
                       "os.popen("):
            self.assertNotIn(banned, source,
                             f"{banned} reappeared in flexfactor_purpose.py")

    def test_gather_refuses_to_run_without_injected_runners(self):
        """An unbrokered gather must be impossible to express, not merely
        discouraged: omitting a runner is a TypeError, never a raw process."""
        with self.assertRaises(TypeError):
            fp.gather_purpose_evidence(self.root)
        with self.assertRaises(TypeError):
            fp.gather_purpose_evidence(self.root, gh_runner=_absent_gh)
        with self.assertRaises(TypeError):
            fp.gather_purpose_evidence(self.root, git_runner=_real_git)

    def test_an_absent_runner_yields_unknowns_never_a_subprocess(self):
        ev = fp.gather_purpose_evidence(self.root, git_runner=None, gh_runner=None)
        self.assertTrue(any("no brokered git runner" in u for u in ev["unknowns"]),
                        ev["unknowns"])
        self.assertTrue(any("no brokered gh runner" in u for u in ev["unknowns"]),
                        ev["unknowns"])


class ContradictionTests(_TempRepo):
    def test_cli_manifest_vs_web_app_readme_is_a_contradiction(self):
        build_full_fixture(self.root)
        _w(self.root, "package.json", """
        {"name": "invoicer",
         "description": "A command-line tool that prints invoices",
         "dependencies": {"express": "^4.19.0"}}
        """)
        ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_absent_gh)
        kinds = [c for c in ev["contradictions"] if c["kind"] == "program-kind"]
        self.assertEqual(len(kinds), 1)
        c = kinds[0]
        self.assertEqual(c["a"]["path_or_ref"], "package.json:description")
        self.assertEqual(c["a"]["says"], ["cli"])
        self.assertEqual(c["b"]["path_or_ref"], "README.md:1")
        self.assertEqual(c["b"]["says"], ["web app"])
        # A contradiction caps confidence below strongly-inferred.
        self.assertEqual(fp.purpose_confidence(None, ev), "weakly-inferred")
        self.assertFalse(fp.mutation_authorized_by_purpose(fp.purpose_confidence(None, ev))[0])

    def test_claimed_integration_not_wired(self):
        build_full_fixture(self.root)
        _w(self.root, "README.md", """
        # Invoicer

        Invoicer is a web app that lets businesses send invoices and texts reminders via Twilio.
        """)
        ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_absent_gh)
        hits = [c for c in ev["contradictions"] if c["kind"] == "claimed-integration-not-wired"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["a"]["says"], ["Twilio"])


class ConfidenceLadderTests(_TempRepo):
    def test_readme_only_is_weakly_inferred(self):
        _w(self.root, "README.md", "# Thing\n\nThing is a web app that does a thing.\n")
        ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_absent_gh)
        self.assertEqual(fp.purpose_confidence(None, ev), "weakly-inferred")
        ok, why = fp.mutation_authorized_by_purpose("weakly-inferred")
        self.assertFalse(ok)
        self.assertIn("weakly", why)

    def test_empty_dir_is_unresolved(self):
        ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_absent_gh)
        self.assertEqual(ev["sources"], [])
        self.assertEqual(fp.purpose_confidence(None, ev), "unresolved")
        self.assertTrue(any("no README prose" in u for u in ev["unknowns"]))
        self.assertTrue(any("no package manifest" in u for u in ev["unknowns"]))
        ok, why = fp.mutation_authorized_by_purpose("unresolved")
        self.assertFalse(ok)
        self.assertIn("unresolved", why)

    def test_missing_dir_is_unresolved_not_a_crash(self):
        ev = fp.gather_purpose_evidence(os.path.join(self.root, "nope"), git_runner=_real_git, gh_runner=_absent_gh)
        self.assertEqual(fp.purpose_confidence(None, ev), "unresolved")
        self.assertTrue(any("does not exist" in u for u in ev["unknowns"]))

    def test_authored_contract_wins_regardless_of_evidence(self):
        c = fp.PurposeContract(name="X", purpose="p", authored=True)
        self.assertEqual(fp.purpose_confidence(c, {}), "owner-authored")
        self.assertTrue(fp.mutation_authorized_by_purpose("owner-authored")[0])

    def test_mutation_mapping_is_exhaustive(self):
        self.assertEqual(
            {lvl: fp.mutation_authorized_by_purpose(lvl)[0] for lvl in fp.PURPOSE_CONFIDENCE_LEVELS},
            {"owner-authored": True, "strongly-inferred": True,
             "weakly-inferred": False, "unresolved": False})
        self.assertFalse(fp.mutation_authorized_by_purpose("DONE")[0])
        self.assertFalse(fp.mutation_authorized_by_purpose("")[0])


class RenderBlockTests(_TempRepo):
    def test_render_cites_paths_and_respects_limit(self):
        build_full_fixture(self.root)
        ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_fake_gh)
        block = fp.render_purpose_evidence_block(ev)
        self.assertTrue(block.startswith("```purpose-evidence"))
        self.assertTrue(block.endswith("\n```"))
        self.assertLessEqual(len(block), 12000)
        for cite in ("package.json:3", "README.md:3", "prisma/schema.prisma:", "src/routes.ts:",
                     ".env.example:2", "gh:pr #7", "gh:issue #3", "Dockerfile:1"):
            self.assertIn(cite, block, cite)
        self.assertIn("UNKNOWNS", block)
        self.assertIn("UNTRUSTED", block)

    def test_render_truncates_but_keeps_fence(self):
        build_full_fixture(self.root)
        ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_fake_gh)
        block = fp.render_purpose_evidence_block(ev, limit_chars=400)
        self.assertLessEqual(len(block), 400)
        self.assertTrue(block.endswith("\n```"))
        self.assertIn("[...truncated]", block)

    def test_render_empty_evidence(self):
        block = fp.render_purpose_evidence_block({})
        self.assertTrue(block.startswith("```purpose-evidence"))
        self.assertTrue(block.endswith("```"))


class InferredRecordTests(_TempRepo):
    def test_inferred_contract_without_evidence_is_unchanged(self):
        c = fp.inferred_contract("X", "does a thing", ["c1"])
        self.assertFalse(c.authored)
        self.assertEqual(c.evidence_ledger, [])
        self.assertEqual(c.confidence, "")
        self.assertEqual(c.source["authored_by"], "flexfactor")
        d = c.to_dict()
        for k in ("name", "slug", "purpose", "acceptance_criteria", "authored", "source"):
            self.assertIn(k, d)

    def test_inferred_contract_with_evidence_carries_ledger_and_confidence(self):
        build_full_fixture(self.root)
        ev = fp.gather_purpose_evidence(self.root, git_runner=_real_git, gh_runner=_fake_gh)
        c = fp.inferred_contract("invoicer", "send invoices", ["c1"], evidence=ev)
        self.assertFalse(c.authored)
        self.assertEqual(c.confidence, "strongly-inferred")
        self.assertEqual(len(c.evidence_ledger), len(ev["sources"]))
        self.assertEqual(c.contradictions, [])
        self.assertTrue(c.unknowns)
        self.assertEqual(c.false_substitutes, fp.false_substitutes_default())
        d = c.to_dict()
        for k in ("evidence_ledger", "contradictions", "unknowns", "confidence"):
            self.assertIn(k, d)
        self.assertIn("INFERRED", c.prompt_block())
        rec = fp.infer_purpose_record("invoicer", "send invoices", ["c1"], evidence=ev)
        self.assertTrue(rec["mutation_authorized"])
        self.assertEqual(rec["confidence"], "strongly-inferred")

    def test_false_substitutes_default_names_the_usual_suspects(self):
        subs = " ".join(fp.false_substitutes_default()).lower()
        for phrase in ("build passes", "page loads", "merged", "200", "tests exist"):
            self.assertIn(phrase, subs)


if __name__ == "__main__":
    unittest.main()
