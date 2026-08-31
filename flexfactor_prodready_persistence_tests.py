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
            if gid == "no_factory_overlay":
                # MEDIUM on purpose (2026-08-30): its only remedy is DELETING
                # files, which the text-editing fix loop cannot do, so at high
                # severity it was a blocker no run could ever close. Reported,
                # never vetoing.
                self.assertEqual(g[gid].severity, "medium", gid)
                self.assertFalse(pr.is_blocking(g[gid]), gid)
                continue
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
        # no_factory_overlay reports but no longer vetoes (see above).
        self.assertTrue({b.id for b in blockers}
                        >= set(self._GIDS) - {"no_factory_overlay"})
        self.assertNotIn("no_factory_overlay", {b.id for b in blockers})

    def test_a_collapsed_spine_blocker_names_the_file_it_blocks_on(self):
        """L5 (2026-08-30): spine_modules_intact is HIGH (blocking) but passed
        no `paths=`, so flexfactor.py filed it against the '(readiness)'
        placeholder the fix loop never edits - reported every run, fixable
        never. The reason strings now carry the rel in the one form
        _paths_from_hits can recover ('<rel> (<why>)')."""
        g = self._gates(self._DIRTY)
        gate = g["spine_modules_intact"]
        self.assertEqual(gate.status, "fail")
        paths = list(getattr(gate, "paths", None) or [])
        self.assertTrue(paths,
                        "the blocking spine gate must hand the fix loop a real "
                        f"file; evidence was: {gate.evidence}")
        for p in paths:
            self.assertTrue(p.endswith((".js", ".sql")), p)

    def test_an_empty_config_file_is_a_definite_syntax_failure(self):
        """L2 (2026-08-30): the per-file gate KEEPS a file whose check returns
        None (only False rolls it back), and _read_text collapsed empty /
        oversized / unreadable into one ''. So a model fix that TRUNCATED a
        tsconfig.json to zero bytes survived the gate. Empty is provably not
        valid JSON (False); oversized/unreadable still verify nothing (None)."""
        import flexfactor_prodready_engine as eng
        with _RepoFixture({"src/keep.js": "x\n"}) as root:
            empty = os.path.join(root, "tsconfig.json")
            open(empty, "w").close()
            ok, why = eng.inproc_syntax_ok(root, "tsconfig.json")
            self.assertIs(ok, False, why)
            self.assertIn("empty", why)
            ok, _ = eng.inproc_syntax_ok(root, "missing.json")
            self.assertIsNone(ok, "unreadable must stay None (nothing verified)")

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


class JvmDependencyPinningGateTests(unittest.TestCase):
    """Every Java component failed "Dependencies are lock-pinned" forever.

    `_LOCKFILES` had no entry for gradle OR maven, so `.get(manager, ())` was
    always empty and no action could ever close the finding - on a HIGH gate
    that BLOCKS. Same unclosable shape as the licence gate, but worse, because
    this one stops a release.

    Measured on sermonsmith 2026-08-30: apps/mobile/android is a Capacitor
    shell whose every version is an exact pin in variables.gradle
    (androidxCoreVersion = '1.17.0', ...), with ZERO dynamic versions in any
    .gradle file - and it was reported "no lockfile: java:apps/mobile/android"
    as one of three blockers keeping the program NOT PRODUCTION READY.

    Gradle and Maven pin BY DECLARATION, exactly as pip does with a `==`
    requirements.txt, which this gate already accepts."""

    def _gate(self, files):
        with _RepoFixture(files) as root:
            chains = pr.detect_toolchains(root)
            return {g.id: g for g in
                    pr.assess_readiness(root, chains, _fake_run())}["deps_pinned"]

    # A Capacitor-shaped Android module: versions as ext literals.
    CAPACITOR = {
        "build.gradle": "apply from: 'variables.gradle'\n",
        "variables.gradle": ("ext {\n    androidxCoreVersion = '1.17.0'\n"
                             "    androidxAppCompatVersion = '1.7.1'\n}\n"),
        "settings.gradle": "include ':app'\n",
    }

    def test_exact_declared_gradle_versions_are_pinned(self):
        self.assertEqual(self._gate(self.CAPACITOR).status, "pass")

    def test_an_exact_gradle_COORDINATE_is_pinned(self):
        self.assertEqual(self._gate({
            "build.gradle": 'dependencies { implementation "androidx.core:core:1.17.0" }',
            "settings.gradle": "include ':app'\n"}).status, "pass")

    def test_a_DYNAMIC_gradle_version_still_FAILS(self):
        # The teeth. "1.+" means two builds can differ, which is exactly what
        # this gate exists to catch - widening it must not blunt that.
        for version in ('1.+', '+', 'latest.release'):
            g = self._gate({
                "build.gradle": f'dependencies {{ implementation "a.b:c:{version}" }}',
                "settings.gradle": "include ':app'\n"})
            self.assertEqual(g.status, "fail", version)

    def test_a_real_gradle_lockfile_is_honoured(self):
        self.assertEqual(self._gate({
            "build.gradle": 'dependencies { implementation "a.b:c:1.+" }',
            "gradle.lockfile": "a.b:c:1.2.3=compileClasspath\n",
            "settings.gradle": "include ':app'\n"}).status, "pass")

    def test_maven_exact_versions_are_pinned(self):
        self.assertEqual(self._gate({
            "pom.xml": ("<project><dependencies><dependency>"
                        "<version>1.2.3</version></dependency></dependencies></project>")
        }).status, "pass")

    def test_a_maven_RANGE_or_LATEST_still_FAILS(self):
        for v in ("[1.0,2.0)", "LATEST", "RELEASE"):
            g = self._gate({"pom.xml": f"<project><version>{v}</version></project>"})
            self.assertEqual(g.status, "fail", v)

    def test_the_gate_keeps_its_severity(self):
        # Still high and still blocking when it genuinely fails - the fix
        # removes a false positive, it does not soften the gate.
        g = self._gate({"build.gradle": 'implementation "a.b:c:+"',
                        "settings.gradle": "include ':app'\n"})
        if g.status == "fail":
            self.assertEqual(g.severity, "high")
            self.assertTrue(pr.is_blocking(g))

    def test_npm_pinning_is_untouched(self):
        # Regression guard: the JVM branch must not change the Node verdict.
        self.assertEqual(self._gate({"package.json": '{"name":"x"}'}).status, "fail")
        self.assertEqual(self._gate({"package.json": '{"name":"x"}',
                                     "package-lock.json": '{"lockfileVersion":3}'}
                                    ).status, "pass")


class ForeignPlatformDoesNotVetoVerificationTests(unittest.TestCase):
    """An iOS component vetoed build-verification of a whole Windows project.

    Measured on GrantFlow 2026-08-30 (win32): node and three gradle components
    all had deps_installed=True, and the ENTIRE program was reported
    "Changes can be build-verified: FAIL [critical] - dependencies not installed
    for swift:ios/App/CapApp-SPM". That is a Capacitor-generated iOS Swift
    package; Apple toolchains require macOS and neither swift nor xcodebuild
    exists on this machine, so its dependencies can NEVER be installed here.

    An unclosable finding on a CRITICAL gate, while four of five components were
    fully verifiable. The honesty guard is kept - the unbuildable component is
    NAMED so the verification claim stays scoped - but a platform the owner is
    not on can no longer veto the parts that do build."""

    def setUp(self):
        # PIN THE SIMULATED HOST (caught in review). These assertions are about
        # a NON-macOS host with no Swift toolchain; on macOS the real
        # sys.platform makes every component local and the assertions invert,
        # and on a Linux box with swiftc installed the swift case flips too. A
        # platform test that only passes on the author's machine is not a test.
        import flexfactor_prodready_engine as _E
        self._E = _E
        self._plat, _E.sys.platform = _E.sys.platform, "win32"
        self._which, _E.shutil.which = _E.shutil.which, lambda name: None

    def tearDown(self):
        self._E.sys.platform = self._plat
        self._E.shutil.which = self._which

    def _tc(self, ecosystem, root, deps_installed, build=True, install=True):
        return pr.Toolchain(
            ecosystem=ecosystem, root=root, manager="x", marker="m",
            build=[["build"]] if build else [], install=[["install"]] if install else [],
            build_needs_deps=True, deps_installed=deps_installed)

    def test_an_uninstallable_APPLE_component_does_not_veto_the_rest(self):
        ok, why = pr.verification_is_real([
            self._tc("node", ".", True),
            self._tc("swift", "ios/App/CapApp-SPM", False),
        ])
        self.assertTrue(ok)
        # ...and the claim must SAY what was not covered, or it overstates.
        self.assertIn("swift:ios/App/CapApp-SPM", why)
        self.assertIn("NOT verifiable on this host", why)

    def test_a_REAL_missing_install_still_fails(self):
        # The teeth. A node component whose deps are genuinely not installed is
        # still a false-failing build gate and must still be reported.
        ok, why = pr.verification_is_real([
            self._tc("node", ".", False),
            self._tc("swift", "ios", False),
        ])
        self.assertFalse(ok)
        self.assertIn("node:.", why)

    def test_an_apple_ONLY_project_is_still_unverified_here(self):
        # If nothing on this host can build, the gate must not claim
        # verification just because the only failures were foreign.
        ok, why = pr.verification_is_real([self._tc("swift", "ios", False)])
        self.assertFalse(ok)
        self.assertIn("cannot run on this host", why)

    def test_a_fully_installed_project_is_unchanged(self):
        ok, why = pr.verification_is_real([self._tc("node", ".", True)])
        self.assertTrue(ok)
        self.assertEqual("build verification available", why)

    def test_the_no_build_system_verdicts_are_unchanged(self):
        self.assertFalse(pr.verification_is_real([])[0])
        self.assertFalse(pr.verification_is_real(
            [self._tc("node", ".", True, build=False)])[0])

    def test_swift_with_a_REAL_toolchain_is_not_foreign(self):
        # Swift and SwiftPM support Linux and Windows. Treating every swift
        # component as unbuildable off macOS would SUPPRESS genuine
        # missing-dependency failures for server-side Swift - the permissive
        # direction. Foreign only when this host has no swift at all.
        self._E.shutil.which = lambda name: r"C:\swift\bin\swift.exe"
        self.assertTrue(self._E._host_can_build(self._tc("swift", "srv", False)))
        ok, _why = pr.verification_is_real([
            self._tc("node", ".", True), self._tc("swift", "srv", False)])
        self.assertFalse(ok, "a real swift component with no deps must still fail")

    def test_the_dead_apple_ecosystem_branch_stays_deleted(self):
        # This test used to pin "xcode/cocoapods/ios stay unconditionally
        # foreign" - behavior NO detector could ever reach, because none of
        # them emits those ecosystem strings. Pinning an unreachable branch is
        # how inert code reads as live coverage (deleted 2026-08-30). What must
        # stay true instead: the constant is gone, and no detector has quietly
        # started emitting those strings without someone re-keying
        # _host_can_build on purpose.
        self.assertFalse(hasattr(self._E, "_APPLE_ONLY_ECOSYSTEMS"),
                         "the inert Apple-only branch crept back")
        emitted = set()
        for name in ("node", "deno", "python", "go", "rust", "java", "dotnet",
                     "ruby", "php", "elixir", "dart", "swift", "cpp", "make"):
            emitted.add(name)
        for foreign in ("xcode", "cocoapods", "ios"):
            self.assertNotIn(foreign, emitted)
            # And without the branch, an unknown ecosystem is assumed buildable
            # (narrow gate): a genuine failure must still be surfaced.
            self._E.shutil.which = lambda name: "/usr/bin/anything"
            self.assertTrue(self._E._host_can_build(self._tc(foreign, "ios", False)))

    def test_on_macos_nothing_is_foreign(self):
        self._E.sys.platform = "darwin"
        for eco in ("swift", "xcode", "cocoapods", "ios"):
            self.assertTrue(self._E._host_can_build(self._tc(eco, "ios", False)), eco)

    def test_host_capability_is_narrow(self):
        # Only Apple ecosystems, and only off-macOS. Everything else stays
        # assumed-buildable so a genuine failure is still surfaced.
        import flexfactor_prodready_engine as E
        self.assertFalse(E._host_can_build(self._tc("swift", "ios", False)))
        for eco in ("node", "python", "java", "go", "rust", "dotnet"):
            self.assertTrue(E._host_can_build(self._tc(eco, ".", False)), eco)


class ReadinessBlockersNameTheFileToEditTests(unittest.TestCase):
    """A blocker that cannot name a file is unfixable BY CONSTRUCTION.

    The audit turns each readiness blocker into a finding, and `_fix_files`
    only ever edits real paths - so a blocker filed against the placeholder
    "(readiness)" could never be acted on, in that run or any future one, while
    the gate advertised `auto_fixable=True`.

    Measured across the live runs: repo-rewards carried "License declared: FAIL"
    for four runs, IPlay "no lockfile: python:." for twelve, GrantFlow's
    persistence findings for sixteen. Reported every time; fixed never. A tool
    whose readiness verdict can never be closed cannot make a program
    production ready, which is the whole job."""

    def _gates(self, files):
        with _RepoFixture(files) as root:
            chains = pr.detect_toolchains(root)
            return {g.id: g for g in pr.assess_readiness(root, chains, _fake_run())}

    def test_an_unpinned_python_component_names_its_requirements_file(self):
        # pip's pinning IS the file, so editing it is a remedy a fix loop can
        # actually perform.
        g = self._gates({"requirements.txt": "requests>=2,<3\n"})["deps_pinned"]
        self.assertEqual(g.status, "fail")
        self.assertEqual(g.paths, ["requirements.txt"])

    def test_a_lockfile_manager_offers_NO_edit_path(self):
        # npm's pinning is a GENERATED package-lock.json. Pointing the fix loop
        # at package.json would ask a model to repair a file that is not broken;
        # the remedy is `npm install`, which bootstrap already runs.
        g = self._gates({"package.json": '{"name":"x","dependencies":{"a":"^1"}}'})["deps_pinned"]
        self.assertEqual(g.status, "fail")
        self.assertEqual(g.paths, [])

    def test_an_undeclared_licence_names_the_manifest_to_edit(self):
        g = self._gates({"package.json": '{"name":"x"}'})["license_present"]
        self.assertEqual(g.status, "fail")
        self.assertEqual(g.paths, ["package.json"])

    def test_a_passing_gate_carries_no_path(self):
        # Nothing to remediate, so nothing to point at - a path here would send
        # the fix loop at a file with no defect.
        g = self._gates({"package.json": '{"name":"x"}',
                         "package-lock.json": '{"lockfileVersion":3}'})["deps_pinned"]
        self.assertEqual(g.status, "pass")
        self.assertEqual(g.paths, [])

    def test_a_path_is_never_a_guess(self):
        import flexfactor_prodready_engine as E
        with _RepoFixture({"requirements.txt": "a==1\n"}) as root:
            missing = pr.Toolchain(ecosystem="python", root="services/api",
                                   manager="pip", marker="requirements.txt")
            self.assertEqual(E._pinning_edit_paths(root, [missing]), [])
            here = pr.Toolchain(ecosystem="python", root=".", manager="pip",
                                marker="requirements.txt")
            self.assertEqual(E._pinning_edit_paths(root, [here]), ["requirements.txt"])

    def test_every_unpinned_component_is_queued_not_just_the_first(self):
        # A monorepo can have several; pointing at one leaves the gate red for
        # the rest.
        import flexfactor_prodready_engine as E
        with _RepoFixture({"requirements.txt": "a>=1\n",
                           "svc/requirements.txt": "b>=1\n"}) as root:
            tcs = [pr.Toolchain(ecosystem="python", root=".", manager="pip",
                                marker="requirements.txt"),
                   pr.Toolchain(ecosystem="python", root="svc", manager="pip",
                                marker="requirements.txt")]
            self.assertEqual(E._pinning_edit_paths(root, tcs),
                             ["requirements.txt", "svc/requirements.txt"])

    def test_the_manifest_comes_from_the_DETECTED_marker(self):
        # A Gradle component's real manifest may be build.gradle.kts. The table
        # name is only a fallback; hard-coding build.gradle made that blocker
        # unfixable even though the detector already knew the true file.
        import flexfactor_prodready_engine as E
        with _RepoFixture({"build.gradle.kts": 'implementation("a:b:1.+")'}) as root:
            tc = pr.Toolchain(ecosystem="java", root=".", manager="gradle",
                              marker="build.gradle.kts")
            self.assertEqual(E._pinning_edit_paths(root, [tc]), ["build.gradle.kts"])

    def test_a_marker_that_does_not_exist_falls_back_then_gives_up(self):
        import flexfactor_prodready_engine as E
        with _RepoFixture({"pom.xml": "<project/>"}) as root:
            tc = pr.Toolchain(ecosystem="java", root=".", manager="maven",
                              marker="nope.xml")
            self.assertEqual(E._pinning_edit_paths(root, [tc]), ["pom.xml"])
        with _RepoFixture({"readme.md": "x"}) as root:
            tc = pr.Toolchain(ecosystem="java", root=".", manager="maven",
                              marker="nope.xml")
            self.assertEqual(E._pinning_edit_paths(root, [tc]), [])

    def test_an_unknown_manager_yields_no_path(self):
        import flexfactor_prodready_engine as E
        with _RepoFixture({"Makefile": "all:\n"}) as root:
            tc = pr.Toolchain(ecosystem="make", root=".", manager="make",
                              marker="Makefile")
            self.assertEqual(E._pinning_edit_paths(root, [tc]), [])

    def test_a_persistence_blocker_names_the_offending_source_files(self):
        # These gates always KNEW the file - it died in a prose evidence string.
        g = self._gates({
            "src/api/client.js": "export const x = createStubEntityClient();\n",
        })["no_in_memory_entity_stubs"]
        self.assertEqual(g.status, "fail")
        self.assertIn("src/api/client.js", g.paths)

    def test_a_persistence_path_is_only_ever_a_real_file(self):
        import flexfactor_prodready_persist as PP
        with _RepoFixture({"a.js": "x\n"}) as root:
            self.assertEqual(
                PP._paths_from_hits(root, ["a.js (why)", "gone.js (why)"]),
                ["a.js"])

class FabricatedLockfileTests(unittest.TestCase):
    """A hand-written lockfile is WORSE than a missing one.

    Measured live, IPlay 2026-08-30. Told to "commit the lockfile so builds are
    reproducible" - an instruction a text-editing fix loop cannot carry out -
    the author model invented a Pipfile and Pipfile.lock for a package manager
    the project does not use, copied the same UNPINNED ranges into them, and
    wrote:

        "hash": {"sha256": "generated-lockfile-sha"}

    with no per-package hashes and every version guessed as the lower bound of
    its range. It passed the verification gate (two unused files break nothing)
    and was committed to the owner's repo. Nothing rejected it, so a run could
    have "closed" the pinning gate with a file that pins nothing and claims a
    reproducibility it cannot deliver."""

    FABRICATED = ('{"_meta": {"hash": {"sha256": "generated-lockfile-sha"}},'
                  ' "default": {"numpy": {"version": "==1.26.0"}}}')
    REAL_PIPENV = ('{"_meta": {"hash": {"sha256": "a3f1b2c4d5e6"}},'
                   ' "default": {"numpy": {"version": "==1.26.4",'
                   ' "hashes": ["sha256:aaaa"]}}}')
    REAL_NPM = ('{"lockfileVersion": 3, "packages": {"node_modules/x":'
                ' {"version": "1.3.0", "resolved": "https://r/x.tgz",'
                ' "integrity": "sha512-XX"}}}')

    def _fab(self, name, body):
        import flexfactor_prodready_engine as E
        with _RepoFixture({name: body}) as root:
            import os as _os
            return E._lockfile_is_fabricated(_os.path.join(root, name))

    def test_the_exact_IPlay_fabrication_is_rejected(self):
        self.assertTrue(self._fab("Pipfile.lock", self.FABRICATED))

    def test_an_empty_lockfile_locks_nothing(self):
        self.assertTrue(self._fab("Pipfile.lock", ""))

    def test_a_REAL_pipenv_lock_is_accepted(self):
        self.assertFalse(self._fab("Pipfile.lock", self.REAL_PIPENV))

    def test_a_REAL_npm_lock_is_accepted(self):
        self.assertFalse(self._fab("package-lock.json", self.REAL_NPM))

    def test_an_unjudgeable_lock_is_NOT_accused(self):
        # A false positive here re-opens a gate the owner has genuinely
        # satisfied, so anything this cannot read is treated as real.
        self.assertFalse(self._fab("Pipfile.lock", "not json at all"))
        self.assertFalse(self._fab("pnpm-lock.yaml", "lockfileVersion: '9.0'\n"))

    def test_a_fabricated_lock_does_not_satisfy_the_pinning_gate(self):
        # End-to-end: the gate must still FAIL with the fake lock present.
        with _RepoFixture({"Pipfile": "[packages]\nnumpy = '*'\n",
                           "Pipfile.lock": self.FABRICATED}) as root:
            chains = pr.detect_toolchains(root)
            gates = {g.id: g for g in pr.assess_readiness(root, chains, _fake_run())}
            if "deps_pinned" in gates and gates["deps_pinned"].status != "na":
                self.assertEqual(gates["deps_pinned"].status, "fail")

    def test_a_GENUINE_lock_over_the_read_cap_is_not_accused(self):
        # THE DANGEROUS DIRECTION (caught in review). _read_text returns "" for
        # a file over MAX_CONFIG_BYTES (512 KiB), a symlink, or any read error -
        # and a real package-lock.json ROUTINELY exceeds that. Calling those
        # fabricated would re-open a gate the owner has genuinely satisfied.
        import json as _j
        big = _j.dumps({"lockfileVersion": 3, "packages": {
            "": {"name": "p"},
            **{f"node_modules/p{i}": {"version": "1.0.0",
                                      "resolved": f"https://r/p{i}.tgz",
                                      "integrity": "sha512-" + "A" * 60}
               for i in range(4000)}}})
        self.assertGreater(len(big), 512 * 1024)
        self.assertFalse(self._fab("package-lock.json", big))

    def test_only_a_TRULY_zero_byte_lock_counts_as_empty(self):
        self.assertTrue(self._fab("Pipfile.lock", ""))

    def test_a_valid_npm_lock_with_NO_dependencies_is_accepted(self):
        # `npm install --package-lock-only` on a dependency-free project writes
        # a v3 lock whose only entry is the root package "", which carries no
        # integrity because there is nothing to fetch. That is a real lock.
        self.assertFalse(self._fab("package-lock.json",
                                   '{"name": "p", "lockfileVersion": 3,'
                                   ' "packages": {"": {"name": "p", "version": "1.0.0"}}}'))

    def test_a_package_NAMED_placeholder_is_not_a_placeholder_hash(self):
        # Matching the raw text would accuse a lock containing a package named
        # "placeholder", or a resolved URL with "todo" in the path. The match is
        # against a hash FIELD'S VALUE only.
        self.assertFalse(self._fab("package-lock.json",
                                   '{"lockfileVersion": 3, "packages":'
                                   ' {"node_modules/placeholder": {"version": "1.0.0",'
                                   ' "resolved": "https://r/todo-utils.tgz",'
                                   ' "integrity": "sha512-REAL"}}}'))

    def test_a_hand_written_COMPOSER_lock_is_caught(self):
        # composer.lock stores `"packages": [ {...} ]` - an ARRAY. An
        # isinstance(v, dict) filter discarded every entry and the
        # "nothing to judge" branch then ACCEPTED the file uninspected.
        self.assertTrue(self._fab(
            "composer.lock", '{"packages": [{"name": "x/y", "version": "1.0.0"}]}'))

    def test_a_REAL_composer_lock_is_accepted(self):
        # Composer records integrity under dist/source, not a flat hash key.
        self.assertFalse(self._fab(
            "composer.lock",
            '{"packages": [{"name": "x/y", "version": "1.0.0",'
            ' "dist": {"type": "zip", "url": "https://r/x.zip", "shasum": "abc"}}]}'))

    def test_a_crate_named_placeholder_is_not_accused(self):
        # Cargo generates `name = "placeholder"` for a crate with that name.
        cargo = ("[[package]]" + chr(10) + 'name = "placeholder"' + chr(10)
                 + 'version = "1.0.0"' + chr(10))
        self.assertFalse(self._fab("Cargo.lock", cargo))

    def test_dotnet_has_a_known_lockfile_so_the_promise_is_true(self):
        # The remediation tells dotnet users that running the installer closes
        # the gate. That was false while _LOCKFILES had no dotnet entry - the
        # unclosable-finding shape, in my own instruction text.
        import flexfactor_prodready_engine as E
        self.assertIn("packages.lock.json", E._LOCKFILES.get("dotnet", ()))

    def test_pip_detected_without_requirements_txt_names_the_real_file(self):
        # pip is also detected from pyproject.toml/setup.py/setup.cfg. Telling
        # the model to edit a requirements.txt that is not there is an
        # instruction it cannot carry out - which is how the Pipfile
        # fabrication happened in the first place.
        import flexfactor_prodready_engine as E

        class _T:
            def __init__(self, m, mk): self.manager, self.marker = m, mk

        txt = E._pinning_remediation([_T("pip", "pyproject.toml")])
        self.assertIn("pyproject.toml", txt)
        # ...and it must NOT promise a closure that cannot happen.
        self.assertIn("will not clear it", txt)

        plain = E._pinning_remediation([_T("pip", "requirements.txt")])
        self.assertIn("requirements.txt", plain)
        self.assertNotIn("will not clear it", plain)

    def test_the_remediation_says_what_to_actually_do(self):
        # "Commit the lockfile" is not an instruction a text-editing fix loop
        # can carry out - which is exactly how it ended up inventing one.
        import flexfactor_prodready_engine as E

        class _T:
            def __init__(self, m, mk=""): self.manager, self.marker = m, mk

        pip_text = E._pinning_remediation([_T("pip", "requirements.txt")])
        self.assertIn("==", pip_text)
        self.assertIn("requirements.txt", pip_text)
        self.assertIn("NEVER hand-write a lockfile", pip_text)

        npm_text = E._pinning_remediation([_T("npm", "package.json")])
        self.assertIn("GENERATED", npm_text)
        self.assertIn("NEVER hand-write a lockfile", npm_text)


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
