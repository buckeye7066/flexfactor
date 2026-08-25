#!/usr/bin/env python3
"""Executable pins for FlexFactor's six purpose-contract invariants.

The invariants in `.flexfactor-purpose.json` were held by discipline and prose:
`_full_gate`'s docstring TELLS future callers to write `is True` and never
truthiness. That rule is correct, currently obeyed, and exactly the kind of
thing that erodes the first time somebody adds a call site in a hurry - which
is why it is now a build failure instead of a comment.

Every check here is MECHANICAL (it reads the tree, not a list of blessed
strings) and every allowlist entry carries a WRITTEN REASON. A sweep that
cries wolf gets muted, which is worse than no sweep, so each rule also has a
canary test that feeds the analyser a synthetic violation and proves the
analyser catches it. A check that cannot fail proves nothing.

  i-1  no review-only / dry-run mode in audit or prodready
  i-2  a verifier outage leaves the target unchanged, no success commit
  i-3  owner WIP never becomes an ancestor of a pushed branch
  i-4  partial output never authorizes CLEAN / READY / approval / merge / push
  i-5  a capability the host cannot enforce is named best-effort
  i-6  every claimed gate must have run; None is not a pass
"""
from __future__ import annotations

import ast
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))

# Directories that are not the running product. Each exclusion is a blind spot,
# so each one has to earn its place:
#   .git / build / dist / __pycache__ / .pytest_cache / flexfactor.egg-info /
#   node_modules  - build outputs and caches; `build/` in particular holds a
#                   STALE COPY of flexfactor.py, and sweeping it would report
#                   violations that no longer exist in the source.
#   .remember     - agent scratch state, not code.
#   eval_fixtures - corpora that exist precisely to contain bad inputs.
#   docs          - recorded evidence artifacts (docs/evidence/*.py are frozen
#                   samples of OTHER programs, not this one's code).
# Nothing else is excluded: `competitors/` and `flexfactor_assets/` contain no
# Python at all, and `providers/` is product code and IS swept.
_SKIP_DIRS = {
    ".git", "build", "dist", "__pycache__", ".pytest_cache", ".remember",
    "flexfactor.egg-info", "node_modules", "eval_fixtures", "docs",
}

# Test modules are excluded from the truthiness sweep on purpose, with reason:
# they DRIVE gates with each of True/False/None deliberately, and unittest's
# assertions (assertIs/assertIsNone/assertTrue) already state the intended
# semantics at the call site. Including them would flag the very tests that
# prove the tri-state works.
def _is_test_module(name: str) -> bool:
    return name.endswith("_tests.py") or name.startswith("test_")


def repo_python_files(include_tests: bool = False) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(_HERE):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if not include_tests and _is_test_module(fn):
                continue
            # `.bak-*` snapshots are frozen history, not the running product.
            if ".bak" in fn:
                continue
            out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# i-6: a tri-state gate must never be read with truthiness
# --------------------------------------------------------------------------- #
def _annotation_is_tristate(text: str) -> bool:
    flat = text.replace(" ", "")
    return "bool|None" in flat or "None|bool" in flat or "Optional[bool]" in flat


def _split_top_level(inner: str) -> list[str]:
    depth, cur, parts = 0, "", []
    for ch in inner:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def discover_tristate_producers(sources: dict[str, str]) -> dict[str, tuple[str, int | None]]:
    """Map function name -> ("scalar", None) | ("tuple", index-of-the-tri-state).

    Discovered from RETURN ANNOTATIONS, so a new `bool | None` function is
    covered the moment it is written - nobody has to remember to register it.
    """
    producers: dict[str, tuple[str, int | None]] = {}
    for path, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.returns is None:
                continue
            text = ast.unparse(node.returns)
            if not _annotation_is_tristate(text):
                continue
            flat = text.replace(" ", "")
            if flat.startswith("tuple[") or flat.startswith("Tuple["):
                parts = _split_top_level(flat[flat.index("[") + 1:-1])
                for i, part in enumerate(parts):
                    if _annotation_is_tristate(part):
                        producers[node.name] = ("tuple", i)
                        break
            else:
                producers[node.name] = ("scalar", None)
    return producers


# Names that carry a gate verdict by convention even where the producer is not
# annotated (a dict lookup, a parameter, a value threaded through a call).
_SEED_GATE_NAMES = frozenset({
    "final_ok", "build_ok", "final_build", "gate_ok", "publication_ok",
    "suite_ok", "verify_ok",
})


class _TriStateScanner(ast.NodeVisitor):
    """Flag tri-state gate values read in a boolean context.

    NARROWING is what keeps this precise. A name stops being tri-state when the
    code has already collapsed it to a real bool - `x = y is True`, `x = bool(y)`
    - or when the read happens inside a branch that excluded None
    (`if x is None: ... else: <x is a bool here>`). Without that, the sweep
    would flag four call sites that are already correct, and a sweep that cries
    wolf gets muted.
    """

    def __init__(self, producers):
        self.producers = producers
        self.violations: list[tuple[int, str, str]] = []
        self._tri: set[str] = set(_SEED_GATE_NAMES)
        self._narrowed: set[str] = set()

    # -- scoping -----------------------------------------------------------
    def visit_FunctionDef(self, node) -> None:
        """Names are function-scoped, or a narrowing in one function would
        silently excuse a truthiness read in the next one (a false NEGATIVE,
        which is the direction that actually costs something here)."""
        saved_tri, saved_narrow = set(self._tri), set(self._narrowed)
        self._tri = set(_SEED_GATE_NAMES)
        self._narrowed = set()
        self.generic_visit(node)
        self._tri, self._narrowed = saved_tri, saved_narrow

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- provenance --------------------------------------------------------
    @staticmethod
    def _callee(call: ast.Call) -> str | None:
        if isinstance(call.func, ast.Name):
            return call.func.id
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        value = node.value
        targets = [t for t in node.targets]
        # (a) assignment FROM a tri-state producer marks the target tri-state
        if isinstance(value, ast.Call):
            kind = self.producers.get(self._callee(value) or "")
            if kind:
                for tgt in targets:
                    if kind[0] == "scalar" and isinstance(tgt, ast.Name):
                        self._tri.add(tgt.id)
                        self._narrowed.discard(tgt.id)
                    elif kind[0] == "tuple" and isinstance(tgt, (ast.Tuple, ast.List)):
                        idx = kind[1] or 0
                        if idx < len(tgt.elts) and isinstance(tgt.elts[idx], ast.Name):
                            self._tri.add(tgt.elts[idx].id)
                            self._narrowed.discard(tgt.elts[idx].id)
        # (b) assignment that COLLAPSES to a real bool narrows the name
        narrows = (
            isinstance(value, ast.Compare)
            and any(isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) for op in value.ops)
        ) or (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "bool"
        ) or (
            isinstance(value, ast.Constant) and isinstance(value.value, bool)
        )
        if narrows:
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    self._narrowed.add(tgt.id)
        self.generic_visit(node)

    # -- boolean contexts --------------------------------------------------
    def _flag(self, expr: ast.AST, why: str) -> None:
        if isinstance(expr, ast.Name):
            if expr.id in self._tri and expr.id not in self._narrowed:
                self.violations.append((expr.lineno, expr.id, why))
        elif isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
            self._flag(expr.operand, why + "/not")
        elif isinstance(expr, ast.BoolOp):
            for value in expr.values:
                self._flag(value, why + "/boolop")

    @staticmethod
    def _none_guard(test: ast.AST) -> tuple[str, bool] | None:
        """`x is None` -> (x, narrowed_in_else); `x is not None` -> (x, False)."""
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return None
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if not (isinstance(right, ast.Constant) and right.value is None):
            return None
        if not isinstance(left, ast.Name):
            return None
        if isinstance(op, ast.Is):
            return left.id, True     # narrowed in the ELSE branch
        if isinstance(op, ast.IsNot):
            return left.id, False    # narrowed in the BODY
        return None

    def visit_If(self, node: ast.If) -> None:
        guard = self._none_guard(node.test)
        if guard is None:
            self._flag(node.test, "if")
        name, in_else = (guard or (None, False))
        added = name is not None and name not in self._narrowed
        for branch, narrow in ((node.body, not in_else), (node.orelse, in_else)):
            if name is not None and narrow and added:
                self._narrowed.add(name)
            for stmt in branch:
                self.visit(stmt)
            if name is not None and narrow and added:
                self._narrowed.discard(name)

    def visit_While(self, node: ast.While) -> None:
        self._flag(node.test, "while")
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._flag(node.test, "ternary")
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._flag(node.test, "assert")
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        for value in node.values:
            self._flag(value, "boolop")
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if isinstance(node.op, ast.Not):
            self._flag(node.operand, "not")
        self.generic_visit(node)


def scan_tristate_truthiness(source: str, producers) -> list[tuple[int, str, str]]:
    scanner = _TriStateScanner(producers)
    scanner.visit(ast.parse(source))
    return scanner.violations


# --------------------------------------------------------------------------- #
# i-2 / i-4: nothing commits, merges or pushes without a gate
# --------------------------------------------------------------------------- #
_GIT_MUTATIONS = frozenset({"commit", "push", "merge"})

# The ONLY functions permitted to run `git commit|push|merge`, each with the
# gate that stands between the mutation and the owner's repository. A new
# mutation site anywhere else fails this test, which forces whoever adds one to
# declare its gate instead of inheriting trust by proximity.
_MUTATION_SITES = {
    "_commit_and_sync": (
        "audit/prodready publication. Guarded by `if final_ok is not True` - "
        "catches None (nothing ran) AND False (ran and failed) - which hard-"
        "resets, cleans newly added paths and returns REJECTED with no local "
        "commit and no push. Behaviourally pinned by "
        "flexfactor_tests.VacuousGateTests."
    ),
    "_apply_integration_impl": (
        "scout apply. Gate is exception-based, not tri-state: a failing verify "
        "command raises ApplyError, which rolls every touched file back before "
        "any commit. The no-command case cannot raise, so it is disclosed "
        "instead - _verify_disclosure on the approval card BEFORE the owner "
        "approves, and `verify_note` in the ApplyResult AFTER, so an "
        "integration nothing executed can never read as a verified one."
    ),
}


def scan_git_mutation_sites(source: str) -> dict[str, list[int]]:
    """Return {enclosing function name: [line, ...]} for git commit/push/merge."""
    found: dict[str, list[int]] = {}
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            for arg in node.args:
                if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
                    first = arg.elts[0]
                    if (isinstance(first, ast.Constant)
                            and first.value in _GIT_MUTATIONS):
                        found.setdefault(stack[-1] if stack else "<module>",
                                         []).append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return found


# --------------------------------------------------------------------------- #
# i-6 / i-4: verification_is_real must be read fail-closed
# --------------------------------------------------------------------------- #
_VERIFICATION_KEY = "verification_is_real"


def scan_verification_reads(source: str) -> list[tuple[int, str]]:
    """Flag reads of the verification_is_real flag that fail OPEN.

    Two shapes lie in the same direction - a MISSING key reading as "verified":
      * `.get("verification_is_real", True)` - absent evidence becomes a pass.
      * `... is False` - a None (key never written) skips the disclosure that
        tells the reader no build ran.
    """
    bad: list[tuple[int, str]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == _VERIFICATION_KEY):
            default = node.args[1]
            if isinstance(default, ast.Constant) and default.value is True:
                bad.append((node.lineno,
                            f'.get("{_VERIFICATION_KEY}", True) fails OPEN: an '
                            "absent probe reads as verified"))
        if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                and isinstance(node.ops[0], ast.Is) \
                and isinstance(node.comparators[0], ast.Constant) \
                and node.comparators[0].value is False:
            left = node.left
            if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                    and left.func.attr == "get" and left.args
                    and isinstance(left.args[0], ast.Constant)
                    and left.args[0].value == _VERIFICATION_KEY):
                bad.append((node.lineno,
                            f'`{_VERIFICATION_KEY} is False` fails OPEN on a '
                            "missing key (None); use `is not True`"))
    return bad


# =========================================================================== #
#                                   TESTS
# =========================================================================== #
class TriStateTruthinessSweepTests(unittest.TestCase):
    """i-6. `_full_gate`'s docstring says callers must write `is True`, never
    truthiness, because `None` means NOTHING WAS VERIFIED. This turns that
    instruction into a build failure."""

    @classmethod
    def setUpClass(cls):
        cls.sources = {p: _read(p) for p in repo_python_files()}
        cls.producers = discover_tristate_producers(cls.sources)

    def test_the_sweep_actually_found_the_tristate_gates(self):
        # A sweep that discovered nothing would pass forever while proving
        # nothing. _full_gate is the canonical one; if it is missing, the
        # discovery half is broken and every green result below is vacuous.
        self.assertIn("_full_gate", self.producers)
        self.assertIn("_publication_gate", self.producers)
        self.assertGreaterEqual(len(self.producers), 8,
                                f"only {len(self.producers)} tri-state producers "
                                "discovered - annotation discovery looks broken")

    def test_no_tristate_gate_is_read_with_truthiness(self):
        offences = []
        for path, src in self.sources.items():
            for lineno, name, why in scan_tristate_truthiness(src, self.producers):
                line = src.splitlines()[lineno - 1].strip()
                offences.append(f"{os.path.relpath(path, _HERE)}:{lineno} "
                                f"[{why}] {name} -> {line[:110]}")
        self.assertEqual(offences, [], "\n".join(
            ["a tri-state gate value was read with truthiness. `None` means "
             "NOTHING WAS VERIFIED and must never take the True branch - use "
             "`is True` / `is False` / `is None`:"] + offences))

    def test_canary_a_truthiness_read_IS_caught(self):
        # Verify the verification: feed the analyser a synthetic violation and
        # prove it fails. Without this, a broken analyser reports "clean".
        canary = (
            "def _full_gate(p, s) -> tuple[bool | None, str]:\n"
            "    return None, ''\n"
            "def publish(p, s):\n"
            "    final_ok, log = _full_gate(p, s)\n"
            "    if final_ok:\n"            # <- the violation
            "        push()\n"
        )
        producers = discover_tristate_producers({"<canary>": canary})
        hits = scan_tristate_truthiness(canary, producers)
        self.assertTrue(any(name == "final_ok" and why == "if" for _, name, why in hits),
                        f"the sweep did not catch a plain truthiness read: {hits}")

    def test_canary_a_negated_truthiness_read_IS_caught(self):
        canary = (
            "def _gate(p) -> bool | None:\n"
            "    return None\n"
            "def publish(p):\n"
            "    ok = _gate(p)\n"
            "    if not ok:\n"
            "        return\n"
            "    push()\n"
        )
        producers = discover_tristate_producers({"<canary>": canary})
        hits = scan_tristate_truthiness(canary, producers)
        self.assertTrue(hits, "`if not ok:` on a tri-state was not caught")

    def test_canary_an_already_narrowed_read_is_NOT_flagged(self):
        # The other half of "does not cry wolf": code that has already
        # collapsed the tri-state to a bool must stay green, or the rule gets
        # muted and stops protecting anything.
        clean = (
            "def _gate(p) -> bool | None:\n"
            "    return None\n"
            "def publish(p):\n"
            "    build_ok = _gate(p)\n"
            "    if build_ok is None:\n"
            "        report('not run')\n"
            "    else:\n"
            "        report('pass' if build_ok else 'fail')\n"
            "    gate_ok = build_ok is True\n"
            "    if gate_ok:\n"
            "        push()\n"
        )
        producers = discover_tristate_producers({"<canary>": clean})
        self.assertEqual(scan_tristate_truthiness(clean, producers), [],
                         "narrowed reads must NOT be reported")


class GitMutationGateSweepTests(unittest.TestCase):
    """i-2 / i-4. Nothing may reach `git commit|push|merge` except through a
    declared gate. The registry is the point: a NEW mutation site anywhere in
    the tree fails, so the person adding it has to say what stops it."""

    @classmethod
    def setUpClass(cls):
        cls.sources = {p: _read(p) for p in repo_python_files()}

    def test_every_git_mutation_site_is_a_declared_gated_site(self):
        found: dict[str, list[str]] = {}
        for path, src in self.sources.items():
            for fn, lines in scan_git_mutation_sites(src).items():
                rel = os.path.relpath(path, _HERE)
                found.setdefault(fn, []).extend(f"{rel}:{ln}" for ln in lines)
        undeclared = {fn: where for fn, where in found.items()
                      if fn not in _MUTATION_SITES}
        self.assertEqual(undeclared, {}, (
            "a new git commit/push/merge site appeared outside the declared "
            "gated functions. Add it to _MUTATION_SITES with the gate that "
            "stands between it and the owner's repository - or route it "
            f"through an existing one: {undeclared}"))
        # And the registry must not rot: a declared site that no longer exists
        # is a stale reason nobody will notice is stale.
        self.assertEqual(sorted(found), sorted(_MUTATION_SITES),
                         "the declared mutation-site registry no longer matches "
                         "the tree")

    def test_the_audit_publication_gate_still_requires_is_True(self):
        import inspect
        import flexfactor as ff
        src = inspect.getsource(ff._commit_and_sync)
        self.assertIn("final_ok is not True", src,
                      "the publication gate stopped distinguishing None "
                      "(nothing ran) from True (verified)")

    def test_canary_a_new_mutation_site_IS_caught(self):
        canary = (
            "def ship(p):\n"
            "    _git(['push', '-u', 'origin', 'main'], p)\n"
        )
        sites = scan_git_mutation_sites(canary)
        self.assertIn("ship", sites)
        self.assertNotIn("ship", _MUTATION_SITES,
                         "canary function must not be a declared site")


class VerificationRealitySweepTests(unittest.TestCase):
    """i-4 / i-6. A report may not read as verified when verification was never
    available. Both failure shapes point the same way: a MISSING probe result
    silently becoming a pass."""

    @classmethod
    def setUpClass(cls):
        cls.sources = {p: _read(p) for p in repo_python_files()}

    def test_no_read_of_verification_is_real_fails_open(self):
        offences = []
        for path, src in self.sources.items():
            for lineno, why in scan_verification_reads(src):
                offences.append(f"{os.path.relpath(path, _HERE)}:{lineno} {why}")
        self.assertEqual(offences, [], "\n".join(
            ["verification availability was read fail-OPEN:"] + offences))

    def test_verification_is_real_is_false_when_nothing_can_build(self):
        # Behavioural, not a grep: the function itself must refuse.
        import flexfactor_prodready_engine as pr
        ok, why = pr.verification_is_real([])
        self.assertFalse(ok)
        self.assertTrue(why, "a refusal must carry a reason")

    def test_canary_a_fail_open_default_IS_caught(self):
        canary = 'x = stack.get("verification_is_real", True)\n'
        self.assertTrue(scan_verification_reads(canary),
                        "a fail-open default was not caught")

    def test_canary_an_is_False_comparison_IS_caught(self):
        canary = 'if a.get("verification_is_real") is False:\n    warn()\n'
        self.assertTrue(scan_verification_reads(canary),
                        "`is False` on a possibly-missing key was not caught")


class FalseSubstituteSweepTests(unittest.TestCase):
    """The eight things the contract says are NOT evidence of success.

    Each check is named after the substitute it forbids and asserts the
    specific mechanism that keeps it out, so a regression names itself.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_HERE, ".flexfactor-purpose.json"),
                  encoding="utf-8") as fh:
            cls.contract = json.load(fh)

    def test_the_contract_still_lists_all_eight(self):
        subs = self.contract.get("false_substitutes") or []
        self.assertEqual(len(subs), 8,
                         f"false_substitutes changed shape: {subs}")

    def test_1_the_build_passing_is_not_publication_evidence(self):
        # A green bundle is not a green repository. _publication_gate must run
        # the project's OWN suite after the build, and a build that is not
        # True must short-circuit rather than fall through to a pass.
        import inspect
        import flexfactor as ff
        src = inspect.getsource(ff._publication_gate_after_build)
        self.assertIn("if build_ok is not True", src)
        self.assertIn("full_suite_cmd", src)

    def test_2_an_http_200_never_sets_a_completeness_flag(self):
        # Nothing may assign run-completeness / verification from a bare
        # status-code comparison.
        offences = []
        for path in repo_python_files():
            tree = ast.parse(_read(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Compare):
                    continue
                cmp_src = ast.unparse(node.value)
                if "status_code" not in cmp_src and "status ==" not in cmp_src:
                    continue
                for tgt in node.targets:
                    name = getattr(tgt, "id", "")
                    if name in {"complete", "run_complete", "verified", "passed",
                                "final_ok", "build_ok"}:
                        offences.append(
                            f"{os.path.relpath(path, _HERE)}:{node.lineno} "
                            f"{name} = {cmp_src[:80]}")
        self.assertEqual(offences, [], "\n".join(
            ["an HTTP status was used as proof of success:"] + offences))

    def test_3_tests_existing_is_never_a_publication_gate(self):
        # `_has_tests` may report a readiness gate; it may not gate a commit.
        import inspect
        import flexfactor as ff
        for fn in (ff._commit_and_sync, ff._publication_gate,
                   ff._publication_gate_after_build):
            self.assertNotIn("_has_tests", inspect.getsource(fn),
                             f"{fn.__name__} treats the EXISTENCE of tests as "
                             "verification")

    def test_4_a_merge_is_not_evidence_the_work_is_verified(self):
        # The merge happens BECAUSE the gate was True, never the other way
        # round: the merge arm must sit behind the `is True` test.
        import inspect
        import flexfactor as ff
        src = inspect.getsource(ff._commit_and_sync)
        self.assertIn("args.merge and final_ok is True", src,
                      "the merge arm no longer requires a verified gate")

    def test_5_module_import_is_not_direct_function_coverage(self):
        import flexfactor_coverage as cov
        src = _read(cov.__file__)
        self.assertIn("direct", src)
        # The audit's dashboard record must report DIRECT invocation as the
        # executed count, never module execution.
        ff_src = _read(os.path.join(_HERE, "flexfactor.py"))
        self.assertIn('"functions_executed": _cov.get("function_direct_coverage_total", 0)',
                      ff_src,
                      "functions_executed stopped reading the DIRECT total - "
                      "module execution is context, not evidence")

    def test_6_a_partial_or_salvaged_model_answer_is_a_provider_failure(self):
        # Behavioural, not a grep: a salvaged/truncated structured answer must
        # not be able to authorize CLEAN, and the verdict must be forced off.
        import flexfactor_partial as partial
        salvaged = partial.attach_partial_meta(
            {"verdict": "CLEAN"},
            partial.PartialSalvageEvidence(reason="truncated by max_tokens"))
        self.assertFalse(partial.may_authorize_clean(salvaged),
                         "a salvaged/partial model answer authorized CLEAN")
        forced = partial.refuse_clean_if_partial(dict(salvaged))
        self.assertNotEqual(str(forced.get("verdict", "")).upper(), "CLEAN",
                            "a partial answer kept its CLEAN verdict")
        # ... and a COMPLETE answer must still be able to say CLEAN, or the
        # rule is a blanket block that proves nothing.
        self.assertTrue(partial.may_authorize_clean({"verdict": "CLEAN"}))

    def test_7_a_poisoned_environment_is_never_claimed_as_containment(self):
        import flexfactor_sandbox as sb
        rep = sb.capability_report(refresh=True)
        claim, headline = rep["claim"], rep["claim_headline"]
        if rep["network_isolation"] != "os-enforced":
            self.assertIn("NOT", claim,
                          "best-effort env isolation was claimed as containment")
            self.assertIn("NOT", headline,
                          "the SHORT containment line hides that the network is "
                          "not contained")
            self.assertIn("network", headline)
        # And the honest half must survive every surface: the headline exists
        # precisely so no caller has to slice `claim` to make it fit.
        self.assertLessEqual(len(headline), 110,
                             "claim_headline must fit a UI row without slicing")

    def test_8_a_checkpoint_existing_is_not_a_resume_authorization(self):
        # A resume must hash-verify against the same purpose contract; the mere
        # presence of a checkpoint file proves nothing about the tree.
        ff_src = _read(os.path.join(_HERE, "flexfactor.py"))
        self.assertIn("sha256", ff_src)
        self.assertTrue("checkpoint" in ff_src.lower() or "resume" in ff_src.lower())


class ContainmentClaimSingleSourceTests(unittest.TestCase):
    """i-5. There is ONE containment claim and every report reads it."""

    def test_trust_delegates_to_the_sandbox_probe(self):
        import flexfactor_sandbox as sb
        import flexfactor_trust as trust
        self.assertEqual(trust.containment_claim(),
                         sb.capability_report()["claim"],
                         "flexfactor_trust grew a second, independent answer to "
                         "'is this contained?'")

    def test_no_surface_slices_the_containment_claim(self):
        # Truncating the claim is how the honest half disappeared: it names the
        # OS-enforced mechanisms first and what is NOT contained last.
        offences = []
        for path in repo_python_files():
            src = _read(path)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Subscript):
                    continue
                inner = ast.unparse(node.value)
                if "claim" in inner and "claim_headline" not in inner \
                        and isinstance(node.slice, ast.Slice):
                    offences.append(f"{os.path.relpath(path, _HERE)}:{node.lineno} "
                                    f"{ast.unparse(node)[:100]}")
        self.assertEqual(offences, [], "\n".join(
            ["the containment claim was sliced - render `claim_headline` "
             "instead, which is built negative-first:"] + offences))


class SweepIsWiredIntoCITests(unittest.TestCase):
    """A sweep nobody runs is a comment with extra steps.

    This repo's own recorded lesson is to test the INJECTION, not just the
    module: four separate features here were written, merged and never wired.
    """

    _WORKFLOW = os.path.join(_HERE, ".github", "workflows",
                             "production-readiness.yml")

    # Test modules deliberately NOT in the CI list, each with its reason.
    # Empty today; an entry here is a decision, not an oversight.
    _NOT_IN_CI: dict[str, str] = {}

    def test_this_module_is_in_the_workflow_test_list(self):
        self.assertTrue(os.path.isfile(self._WORKFLOW), self._WORKFLOW)
        self.assertIn(os.path.basename(__file__), _read(self._WORKFLOW),
                      "the invariant sweep is not in the list of modules CI "
                      "runs, so it can never fail a build")

    def test_EVERY_test_module_is_in_the_workflow_test_list(self):
        """The generalization, because this repo keeps hitting it.

        Five features here were written, tested and never wired
        (flexfactor_runstate, the set_phase/record_cycle group, _UI_EXPLORER_JS,
        the purpose-evidence gather) - and on 2026-08-25 the same trap turned up
        one level higher: FIVE whole test modules existed and passed locally
        while CI never ran any of them, so an expired time-bomb fixture in
        flexfactor_route_fault_tests sat red and invisible. A suite CI does not
        run is a suite that does not exist.
        """
        wf = _read(self._WORKFLOW)
        modules = sorted(
            f for f in os.listdir(_HERE)
            if f.endswith(".py") and _is_test_module(f))
        missing = [m for m in modules
                   if m not in wf and m not in self._NOT_IN_CI]
        self.assertEqual(missing, [], (
            "these test modules exist but CI never runs them, so they can "
            "never fail a build. Add them to the workflow's module list, or "
            "record them in _NOT_IN_CI with the reason they are excluded: "
            f"{missing}"))
        # Reasons must not outlive their module.
        stale = [m for m in self._NOT_IN_CI if m not in modules]
        self.assertEqual(stale, [], f"_NOT_IN_CI names modules that are gone: {stale}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
