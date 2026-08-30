"""Persistence-readiness gates — GrantFlow Factory Deck class.

Run:  python flexfactor_prodready_persistence_tests.py

Lives next to flexfactor_tests.py so the prodready suite can stay loadable
while these mutation-style fixtures stay small. Same helpers the rubric
tests already use (_RepoFixture + a git-failing _run so the walk sees
every fixture file).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

import flexfactor_prodready as pr


class _RepoFixture:
    """Build a throwaway repo from a {relpath: contents} map."""

    def __init__(self, files: dict):
        self.files = files
        self._tmp = None

    def __enter__(self) -> str:
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        for rel, body in self.files.items():
            path = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return root

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


def _fake_run(results=None):
    results = results or {}

    def run(cmd, cwd, timeout=None, **kw):
        rc = results.get(cmd[0], 0)
        return subprocess.CompletedProcess(cmd, rc, "", "")
    return run


class PersistenceReadinessGateTests(unittest.TestCase):
    """GrantFlow Factory Deck persistence class (PR #1266 / SHA 3060385).

    A tree shaped like the pre-repair extend — browser-minted invoice
    numbers, createStubEntityClient, root _gh_* leftovers, a collapsed
    server.js beside a huge schema.sql, tables only in numbered
    migrations — must FAIL the high gates. A clean / no-surface tree
    must PASS or NA. Mutation-style: each defect is named in evidence.
    """

    _DIRTY = {
        "package.json": '{"dependencies":{"react":"18.0.0"}}',
        "src/pages/CreateInvoice.jsx": (
            "export function issueInvoice(settings) {\n"
            "  const last = settings.last_invoice_number || 0;\n"
            "  const nextNumber = last + 1;\n"
            "  settings.last_invoice_number = nextNumber;\n"
            "  return nextNumber;\n"
            "}\n"
        ),
        "src/api/client.js": (
            "export const KNOWN_STUB_ENTITIES = "
            "['AiArtifact','PartnerSource','SearchJob','Taxonomy'];\n"
            "export function createStubEntityClient(name) {\n"
            "  const store = new Map();\n"
            "  return { create(row) { store.set(row.id, row); return row; } };\n"
            "}\n"
        ),
        "_gh_foo.sql": "-- leftover factory overlay\n",
        "_restore_server_from_2a77487.js": "export default {};\n",
        "backend/server.js": (
            "import express from 'express';\n"
            "const app = express();\n"
            "app.listen(3000);\n"
        ),
        "backend/db/schema.sql": (
            "CREATE TABLE users (id INTEGER PRIMARY KEY);\n"
            "CREATE TABLE grants (id TEXT);\n" + ("-- catalog pad\n" * 9000)
        ),
        "backend/db/migrations/174_org_scoped_workspace_entities.sql": (
            "CREATE TABLE consultant_invoice_counters (\n"
            "  org_id TEXT PRIMARY KEY,\n"
            "  last_number INTEGER NOT NULL\n"
            ");\n"
        ),
        "backend/db/migrate.js": "export function migrate() {}\n",
    }

    _CLEAN = {
        "package.json": '{"dependencies":{"react":"18.0.0"}}',
        "src/pages/Home.jsx": "export default function Home() { return null; }\n",
        "src/api/client.js": (
            "export async function apiGet(path) {\n"
            "  const res = await fetch(path);\n"
            "  return res.json();\n"
            "}\n" + ("// persist helper so this is not a stub client\n" * 40)
        ),
        "backend/server.js": ("// express host spine\napp.use(router);\n" * 80),
        "backend/db/schema.sql": (
            "CREATE TABLE users (id INTEGER PRIMARY KEY);\n"
            "CREATE TABLE consultant_invoice_counters "
            "(org_id TEXT, last_number INTEGER);\n"
        ),
        "backend/db/migrations/174_counters.sql": (
            "CREATE TABLE consultant_invoice_counters "
            "(org_id TEXT, last_number INTEGER);\n"
        ),
        "backend/db/workspacePersistenceTables.sql": (
            "CREATE TABLE IF NOT EXISTS consultant_invoice_counters "
            "(org_id TEXT, last_number INTEGER);\n"
        ),
        "backend/db/postgres/migrations/0179_counters.sql": (
            "CREATE TABLE consultant_invoice_counters "
            "(org_id TEXT, last_number INTEGER);\n"
        ),
        "backend/db/migrate.js": (
            "import fs from 'fs';\n"
            "export async function migrate(db) {\n"
            "  const sql = fs.readFileSync(new URL('./schema.sql', import.meta.url), 'utf8');\n"
            "  await db.exec(sql);\n"
            "}\n" + ("// apply numbered migrations with ON CONFLICT\n" * 20)
        ),
    }

    _GIDS = (
        "no_client_unique_counters",
        "no_in_memory_entity_stubs",
        "no_factory_overlay",
        "spine_modules_intact",
        "schema_bootstrap_covers_extras",
    )

    def _gates(self, files, **kw):
        with _RepoFixture(files) as root:
            chains = pr.detect_toolchains(root)
            return {g.id: g for g in
                    pr.assess_readiness(root, chains, _fake_run({"git": 1}), **kw)}

    def test_grantflow_shaped_tree_fails_every_persistence_gate(self):
        g = self._gates(self._DIRTY)
        for gid in self._GIDS:
            self.assertEqual(g[gid].status, "fail", gid)
            self.assertEqual(g[gid].severity, "high", gid)
            self.assertTrue(pr.is_blocking(g[gid]), gid)
        self.assertIn("CreateInvoice.jsx", g["no_client_unique_counters"].evidence)
        self.assertIn("createStubEntityClient", g["no_in_memory_entity_stubs"].evidence)
        self.assertIn("_gh_foo.sql", g["no_factory_overlay"].evidence)
        self.assertIn("_restore_server_from_2a77487.js",
                      g["no_factory_overlay"].evidence)
        self.assertIn("consultant_invoice_counters",
                      g["schema_bootstrap_covers_extras"].evidence)
        ready, blockers = pr.readiness_verdict(list(g.values()))
        self.assertFalse(ready)
        self.assertTrue({b.id for b in blockers} >= set(self._GIDS))

    def test_clean_tree_passes_or_covers_the_surface(self):
        g = self._gates(self._CLEAN)
        for gid in self._GIDS:
            self.assertEqual(g[gid].status, "pass", f"{gid}: {g[gid].evidence}")
            self.assertFalse(pr.is_blocking(g[gid]), gid)

    def test_python_only_repo_is_na_where_the_surface_is_absent(self):
        g = self._gates({"app.py": "print(1)\n", "requirements.txt": "x==1\n"})
        self.assertEqual(g["no_client_unique_counters"].status, "pass")
        self.assertEqual(g["no_in_memory_entity_stubs"].status, "na")
        self.assertEqual(g["no_factory_overlay"].status, "pass")
        self.assertEqual(g["spine_modules_intact"].status, "na")
        self.assertEqual(g["schema_bootstrap_covers_extras"].status, "na")
        for gid in self._GIDS:
            self.assertFalse(pr.is_blocking(g[gid]), gid)

    def test_stub_and_counter_in_tests_do_not_fail(self):
        g = self._gates({
            "src/api/client.js": (
                "export async function apiGet(p) { return fetch(p); }\n"
                + ("// persist helper\n" * 40)
            ),
            "src/api/client.test.js": (
                "import { createStubEntityClient } from './client';\n"
                "const KNOWN_STUB_ENTITIES = ['AiArtifact'];\n"
                "const n = settings.last_invoice_number + 1;\n"
            ),
        })
        self.assertEqual(g["no_in_memory_entity_stubs"].status, "pass")
        self.assertEqual(g["no_client_unique_counters"].status, "pass")

    def test_empty_known_stubs_is_not_a_failure(self):
        g = self._gates({
            "src/lib/entities.js": "export const KNOWN_STUB_ENTITIES = [];\n",
        })
        self.assertEqual(g["no_in_memory_entity_stubs"].status, "pass")

    def test_overlay_outside_repo_root_is_ignored(self):
        g = self._gates({"backend/_gh_foo.sql": "-- not a root leftover\n"})
        self.assertEqual(g["no_factory_overlay"].status, "pass")

    def test_nextNumber_pagination_without_invoice_domain_does_not_fail(self):
        g = self._gates({
            "src/pages/Pager.jsx": (
                "export function page(i) { const nextNumber = i + 1; return nextNumber; }\n"
            ),
        })
        self.assertEqual(g["no_client_unique_counters"].status, "pass")

    def test_extras_bootstrap_covers_a_migrated_table(self):
        g = self._gates({
            "backend/db/schema.sql": "CREATE TABLE users (id INTEGER);\n",
            "backend/db/migrations/174_x.sql": (
                "CREATE TABLE consultant_invoice_counters (org_id TEXT);\n"
            ),
            "backend/db/workspacePersistenceTables.sql": (
                "CREATE TABLE IF NOT EXISTS consultant_invoice_counters "
                "(org_id TEXT);\n"
            ),
        })
        self.assertEqual(g["schema_bootstrap_covers_extras"].status, "pass")

    def test_postgres_twin_missing_for_extras_table_fails(self):
        g = self._gates({
            "backend/db/schema.sql": "CREATE TABLE users (id INTEGER);\n",
            "backend/db/migrations/174_x.sql": (
                "CREATE TABLE consultant_invoice_counters (org_id TEXT);\n"
            ),
            "backend/db/workspacePersistenceTables.sql": (
                "CREATE TABLE IF NOT EXISTS consultant_invoice_counters "
                "(org_id TEXT);\n"
            ),
            "backend/db/postgres/migrations/0178_unrelated.sql": (
                "CREATE TABLE other_thing (id INT);\n"
            ),
        })
        self.assertEqual(g["schema_bootstrap_covers_extras"].status, "fail")
        self.assertIn("no postgres twin", g["schema_bootstrap_covers_extras"].evidence)

    def test_no_schema_plus_migrations_layout_is_na(self):
        g = self._gates({"README.md": "hi\n"})
        self.assertEqual(g["schema_bootstrap_covers_extras"].status, "na")


class LicenceDeclarationGateTests(unittest.TestCase):
    """"License declared" asked a question it would not accept the real answer to.

    The gate accepted only a LICENSE/COPYING FILE, so a PRIVATE, proprietary
    package failed it forever: npm's own declaration for that case is
    `"license": "UNLICENSED"` in package.json, there is no file to add, and
    adding an OSS one would be actively wrong.

    Measured on repo-rewards 2026-08-29: `private: true`, licence declared
    UNLICENSED, gate still FAIL - a finding that can never be closed, which is
    how a rubric teaches its reader to ignore it."""

    BASE = {"package.json": '{"name":"x","version":"1.0.0"}'}

    def _gate(self, files):
        with _RepoFixture(files) as root:
            chains = pr.detect_toolchains(root)
            return {g.id: g for g in
                    pr.assess_readiness(root, chains, _fake_run())}["license_present"]

    def test_a_private_package_declaring_UNLICENSED_passes(self):
        g = self._gate({"package.json":
                        '{"name":"x","private":true,"license":"UNLICENSED"}'})
        self.assertEqual(g.status, "pass")
        self.assertIn("package.json", g.evidence)

    def test_an_spdx_licence_in_the_manifest_passes(self):
        self.assertEqual(
            self._gate({"package.json": '{"name":"x","license":"MIT"}'}).status, "pass")

    def test_a_LICENSE_file_still_passes_and_is_still_what_evidence_says(self):
        g = self._gate({**self.BASE, "LICENSE": "MIT License\n"})
        self.assertEqual(g.status, "pass")
        self.assertIn("license file present", g.evidence)

    def test_no_file_and_no_manifest_field_still_FAILS(self):
        # The gate must keep its teeth: this is the case it exists for.
        g = self._gate(self.BASE)
        self.assertEqual(g.status, "fail")
        self.assertIn("no license file", g.evidence)

    def test_an_empty_licence_field_is_not_a_declaration(self):
        self.assertEqual(
            self._gate({"package.json": '{"name":"x","license":""}'}).status, "fail")

    def test_a_malformed_manifest_declares_nothing(self):
        # Must not raise, and must not pass on unparseable input.
        self.assertEqual(
            self._gate({"package.json": '{ broken'}).status, "fail")

    def test_a_cargo_manifest_licence_passes(self):
        g = self._gate({"Cargo.toml": '[package]\nname = "x"\nlicense = "Apache-2.0"\n'})
        self.assertEqual(g.status, "pass")

    def test_a_licence_in_a_NESTED_manifest_does_not_count(self):
        # A dependency's or subpackage's manifest is not this project declaring
        # its own licence.
        g = self._gate({**self.BASE,
                        "packages/inner/package.json": '{"name":"i","license":"MIT"}'})
        self.assertEqual(g.status, "fail")

    def test_composer_multi_licence_LIST_passes(self):
        # Composer's valid multi-licence form. Rejecting the list reproduced the
        # very "no licence field" result this change removes.
        g = self._gate({"composer.json":
                        '{"name":"x/y","license":["MIT","GPL-3.0-or-later"]}'})
        self.assertEqual(g.status, "pass")

    def test_an_empty_licence_list_is_not_a_declaration(self):
        self.assertEqual(
            self._gate({"composer.json": '{"name":"x/y","license":[]}'}).status, "fail")

    def test_a_licence_under_an_UNRELATED_toml_table_does_not_pass(self):
        # `[tool.foo] license = "MIT"` says nothing about the distributable
        # package's licence; an unscoped search passed it and gave the gate a
        # way to be wrong in the permissive direction.
        for manifest, body in (("pyproject.toml", '[tool.foo]\nlicense = "MIT"\n'),
                               ("Cargo.toml", '[package.metadata.foo]\nlicense = "MIT"\n')):
            self.assertEqual(self._gate({manifest: body}).status, "fail", manifest)

    def test_licence_under_a_real_package_table_passes(self):
        for manifest, body in (
                ("Cargo.toml", '[package]\nname = "x"\nlicense = "Apache-2.0"\n'),
                ("pyproject.toml", '[project]\nname = "x"\nlicense = "MIT"\n'),
                ("pyproject.toml", '[tool.poetry]\nname = "x"\nlicense = "MIT"\n')):
            self.assertEqual(self._gate({manifest: body}).status, "pass", body)

    def test_cargo_license_file_pointing_at_a_TRACKED_file_passes(self):
        # `license-file = "EULA.txt"` is a real declaration, and the filename is
        # routinely not licence-shaped, so the basename check cannot see it.
        g = self._gate({"Cargo.toml": '[package]\nname = "x"\nlicense-file = "EULA.txt"\n',
                        "EULA.txt": "All rights reserved.\n"})
        self.assertEqual(g.status, "pass")

    def test_cargo_license_file_pointing_at_NOTHING_still_fails(self):
        # A manifest may name a file that was never committed; that is a broken
        # declaration, not a licence.
        with _RepoFixture({"Cargo.toml":
                           '[package]\nname = "x"\nlicense-file = "EULA.txt"\n'}) as root:
            chains = pr.detect_toolchains(root)
            gates = {g.id: g for g in pr.assess_readiness(root, chains, _fake_run())}
            self.assertEqual(gates["license_present"].status, "fail")

    def test_the_gate_stays_low_severity_and_non_blocking(self):
        # It reports; it must never block a release on its own.
        g = self._gate(self.BASE)
        self.assertEqual(g.severity, "low")
        self.assertFalse(pr.is_blocking(g))


if __name__ == "__main__":
    unittest.main(verbosity=2)
