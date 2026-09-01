"""Production-readiness engine for FlexFactor: detect, bootstrap, assess.

FlexFactor's audit could already hunt and fix defects, but it could only BUILD
two ecosystems (Node + Python) and it never installed a project's dependencies.
Both gaps are silent-failure shaped:

  * No install  -> `npm run build` fails on a fresh checkout, the per-cycle build
    gate goes red, and every otherwise-good fix is vetoed and rolled back. The
    run "completes" having changed nothing.
  * Unknown ecosystem -> `_full_gate` has no commands, returns
    True/"(no build/verify command available)", and fixes to Go/Rust/Java/.NET/
    Ruby/PHP/Elixir code ship with NO verification at all while the report reads
    green. A vacuous gate is worse than an absent one because it looks passed.

This module closes both, plus adds the deterministic rubric that makes
"production ready" a checkable claim instead of a vibe.

DESIGN CONSTRAINTS (mirroring flexfactor_cmdpolicy.py / flexfactor_egress.py):

  * Stdlib only, and it never imports flexfactor -> unit-testable in isolation.
  * It NEVER launches a subprocess itself. Detection is pure filesystem reads;
    execution happens through a `run` callable the caller injects, which in
    practice is flexfactor._run. That keeps every command flowing through the
    existing cmdpolicy gate and _winify - this module cannot become a second,
    ungated egress path for command execution.
  * Fail closed and fail HONEST: a toolchain we cannot verify is reported as
    unverifiable rather than silently passed. `verification_is_real()` exists
    specifically so callers can refuse to claim a green build they never ran.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, asdict

# Bound every config read: a hostile or corrupt repo must not be able to make
# detection allocate unbounded memory before the audit has even started.
MAX_CONFIG_BYTES = 512 * 1024
# Directories that never contain the project's own source or manifests. Walking
# into them produces phantom toolchains (e.g. a vendored package.json inside
# node_modules would register as a second Node project).
SKIP_DIRS = frozenset({
    "node_modules", ".git", ".hg", ".svn", "venv", ".venv", "env", ".env.d",
    "vendor", "target", "dist", "build", "out", ".next", ".nuxt", ".svelte-kit",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "bin", "obj", "coverage", ".gradle", ".idea", ".vscode", "Pods",
    "bower_components", ".terraform", ".serverless", "site-packages",
    ".cargo", ".stack-work", "_build", "deps", "elm-stuff", ".dart_tool",
})
# How deep to hunt for nested manifests (monorepo packages live at depth 1-2:
# `packages/api/package.json`, `services/web/go.mod`). Depth 3 covers the
# realistic layouts without turning detection into a full-tree crawl.
MAX_SCAN_DEPTH = 3


# MANAGERS WHOSE PINNING LIVES IN THE MANIFEST, so EDITING that file is the
# remedy and a fix loop can perform it.
#
# Deliberately excludes every lockfile-based manager (npm/pnpm/yarn/bun, cargo,
# composer, bundler, go, deno, dart/flutter, pipenv, poetry, uv, pdm). For those
# the remedy is GENERATING the lockfile - `npm install` writes
# package-lock.json - which the bootstrap phase already does and which no edit
# to package.json can accomplish. Handing that blocker to the fix loop pointed
# at package.json would ask a model to repair a file that is not broken, and
# invite a bogus edit to a manifest that was already correct (caught in review).
_PINNED_IN_MANIFEST = {
    "pip": "requirements.txt",     # pinning IS `==` in the file
    "gradle": "build.gradle",      # exact declared versions
    "maven": "pom.xml",            # exact declared versions
}


# A hash a resolver never produced. Measured from the lockfile the author model
# invented for IPlay 2026-08-30: `"sha256": "generated-lockfile-sha"`.
# Matched against a hash FIELD'S VALUE in full, never against raw file text -
# scanning the text would accuse a lock containing a package named
# "placeholder" or a resolved URL with "todo" in the path.
_PLACEHOLDER_HASH_RE = re.compile(
    r"""(?:sha\d*[:=-]?\s*)?(?:generated[- ]?lockfile[- ]?sha|placeholder|todo|"""
    r"""changeme|fake[- ]?hash|sha256-placeholder|x{3,})""", re.I)


def _lockfile_is_fabricated(path: str) -> bool:
    """True when a lockfile was hand-written rather than produced by a resolver.

    A FABRICATED LOCKFILE IS WORSE THAN A MISSING ONE - it claims a
    reproducibility it cannot deliver, to everyone who later trusts it. Measured
    live on IPlay 2026-08-30: told to "commit the lockfile", the author model
    wrote a Pipfile.lock containing `"sha256": "generated-lockfile-sha"`, no
    per-package hashes at all, and every version guessed as the lower bound of
    its declared range. Nothing rejected it, so a run could have "closed" the
    pinning gate with a file that pins nothing.

    Deliberately CONSERVATIVE, because a false positive here re-opens a gate the
    owner has genuinely satisfied: only a literal placeholder hash, or a JSON
    lock whose packages carry no hash field at all, counts. A lock this cannot
    read is treated as REAL (the gate's other checks still apply) rather than
    accused.
    """
    text = _read_text(path)
    if not text.strip():
        # UNREADABLE IS NOT EMPTY (caught in review). `_read_text` returns ""
        # for a file over MAX_CONFIG_BYTES, a symlink, or any read error - and a
        # genuine package-lock.json ROUTINELY exceeds 512 KiB. Calling those
        # fabricated would re-open a gate the owner has actually satisfied,
        # which is the dangerous direction for this check. Only a file that is
        # really zero bytes locks nothing.
        try:
            return os.path.getsize(path) == 0
        except OSError:
            return False                  # cannot even stat it -> not accused
    if not path.lower().endswith((".lock", ".json")):
        return False                      # not a shape we can judge
    try:
        data = json.loads(text)
    except Exception:
        return False                      # unreadable -> not accused
    if not isinstance(data, dict):
        return False
    # PLACEHOLDER ONLY IN A HASH VALUE (caught in review). Scanning the whole
    # text would accuse a lock containing a package literally named
    # "placeholder", or a resolved URL with "todo" in the path.
    if _has_placeholder_hash(data):
        return True
    # COMPOSER'S GROUPS ARE ARRAYS, not maps (caught in review): composer.lock
    # stores `"packages": [ {...}, ... ]`, so an isinstance(v, dict) filter
    # discarded every entry and the "nothing to judge" branch then ACCEPTED a
    # hand-written Composer lock without inspecting it at all.
    entries: list[tuple[str, dict]] = []
    for key, group in data.items():
        if key not in ("default", "develop", "packages", "packages-dev",
                       "dependencies"):
            continue
        if isinstance(group, dict):
            entries += [(name, e) for name, e in group.items()
                        if isinstance(e, dict)]
        elif isinstance(group, list):
            entries += [(str(e.get("name") or ""), e) for e in group
                        if isinstance(e, dict)]
    # A VALID LOCK CAN LEGITIMATELY HAVE NO HASHED DEPENDENCIES (caught in
    # review): `npm install --package-lock-only` on a dependency-free project
    # writes a v3 lock whose only entry is the root package `""`, which carries
    # no integrity because there is nothing to fetch. That is a real lock.
    real = [(n, e) for n, e in entries if n not in ("", ".")]
    if not real:
        return False                      # nothing to judge, or root-only
    # A real pipenv/npm lock records an integrity hash per package. None at all,
    # across every dependency entry, means nothing was ever resolved.
    # Composer records integrity under dist/source rather than a flat key.
    return not any(
        any(k in e for k in ("hashes", "hash", "integrity", "resolved",
                             "checksum", "dist", "source"))
        for _n, e in real)


def _has_placeholder_hash(data, depth: int = 0) -> bool:
    """A hash-named field whose VALUE is a literal placeholder."""
    if depth > 6 or not isinstance(data, (dict, list)):
        return False
    if isinstance(data, list):
        return any(_has_placeholder_hash(v, depth + 1) for v in data)
    for key, value in data.items():
        k = str(key).lower()
        if k in ("hash", "hashes", "integrity", "checksum", "sha256", "sha512"):
            flat = [value] if isinstance(value, str) else (
                list(value.values()) if isinstance(value, dict) else
                list(value) if isinstance(value, list) else [])
            if any(isinstance(v, str) and _PLACEHOLDER_HASH_RE.fullmatch(v.strip())
                   for v in flat):
                return True
        if _has_placeholder_hash(value, depth + 1):
            return True
    return False


def _pinning_remediation(unpinned) -> str:
    """The remediation text for the dependency-pinning gate.

    "Commit the lockfile so builds are reproducible" is not an instruction a
    text-editing fix loop can carry out, and pointing it at requirements.txt
    made that worse: told to "commit the lockfile", the author model INVENTED a
    Pipfile and a Pipfile.lock for a package manager IPlay does not use, copied
    the same unpinned ranges into it, and wrote

        "hash": {"sha256": "generated-lockfile-sha"}

    with no per-package hashes at all and every version guessed as the lower
    bound of its range. It passed the verification gate because two unused files
    break nothing, and the gate still failed afterwards because nothing was
    actually pinned. A FABRICATED LOCKFILE IS WORSE THAN A MISSING ONE: it
    claims a reproducibility it cannot deliver, to anyone who later trusts it.

    So the text now says exactly what to do for the ecosystem in front of it,
    and says plainly what NOT to do. Live IPlay 2026-08-30 is the measurement.
    """
    managers = {t.manager for t in (unpinned or [])}
    lines = []
    if "pip" in managers:
        # NAME THE FILE THAT ACTUALLY EXISTS (caught in review): pip is also
        # detected from pyproject.toml / setup.py / setup.cfg, and telling the
        # model to edit a requirements.txt that is not there is an instruction
        # it cannot carry out - which is exactly how the Pipfile fabrication
        # happened. Note honestly when pinning that manifest will not by itself
        # close the gate, instead of promising a closure that cannot occur.
        pip_markers = sorted({
            os.path.basename(t.marker or "") or "requirements.txt"
            for t in unpinned if t.manager == "pip"})
        target = ", ".join(pip_markers) or "requirements.txt"
        lines.append(
            f"For pip, pin EVERY requirement to an exact version with `==` in "
            f"{target} (e.g. `numpy==1.26.4`), choosing versions that satisfy "
            "the ranges already there.")
        if not any(m.lower() == "requirements.txt" for m in pip_markers):
            lines.append(
                "NOTE: this gate recognises pip pinning only in a "
                "requirements.txt, so pinning "
                f"{target} alone will not clear it - export a pinned "
                "requirements.txt (e.g. `pip freeze > requirements.txt`) as "
                "well.")
    if managers & {"gradle", "maven"}:
        lines.append(
            "For Gradle/Maven, replace every dynamic version ('1.+', '+', "
            "'latest.release', a Maven range or LATEST/RELEASE) with an exact "
            "version in the manifest.")
    if "dotnet" in managers:
        # NAME THE REAL ACTION: plain `dotnet restore` does not write
        # packages.lock.json - the generic "run the installer" clause below
        # promised a closure the installer could not produce.
        lines.append(
            "For dotnet, run `dotnet restore --use-lock-file` (or set "
            "<RestorePackagesWithLockFile>true</RestorePackagesWithLockFile> "
            "in the project file) and commit the generated packages.lock.json "
            "- plain `dotnet restore` does not write it.")
    generated = sorted(managers - {"pip", "gradle", "maven", "dotnet"})
    if generated:
        lines.append(
            "For " + ", ".join(generated) + " the lockfile is GENERATED by the "
            "package manager (e.g. `npm install` writes package-lock.json) - run "
            "the installer and commit its output.")
    lines.append(
        "NEVER hand-write a lockfile, and never introduce a different package "
        "manager to satisfy this: a lockfile with placeholder or absent hashes "
        "is a false claim of reproducibility, and is worse than having none.")
    return " ".join(lines)


def _pinning_edit_paths(project_dir: str, unpinned) -> list[str]:
    """Repo-relative manifests a remediation could EDIT to pin dependencies.

    Never a guess: a path is returned only when the file exists on disk, because
    a caller must be able to open what it is handed. A component whose pinning
    is a generated lockfile contributes nothing here - there is no edit that
    fixes it.
    """
    out: list[str] = []
    for tc in unpinned:
        if tc.manager not in _PINNED_IN_MANIFEST:
            continue
        # PREFER THE MARKER THE DETECTOR ACTUALLY FOUND. The table's name is a
        # fallback: a Gradle component's real manifest may be build.gradle.kts,
        # and hard-coding build.gradle made its blocker unfixable even though
        # the detector had already recorded the true file (caught in review).
        for name in (os.path.basename(tc.marker or ""),
                     _PINNED_IN_MANIFEST[tc.manager]):
            if not name:
                continue
            root = (tc.root or ".").strip("./")
            rel = f"{root}/{name}" if root and root != "." else name
            if os.path.isfile(os.path.join(project_dir, rel)):
                if rel not in out:
                    out.append(rel)
                break
    return out


def _license_declared_in_manifest(project_dir: str, files) -> str | None:
    """The manifest path declaring a licence, or None.

    Deliberately narrow: only the ecosystem's OWN package manifest counts, only
    a non-empty scalar value, and the file must be one this project actually
    tracks. A licence mentioned in a README or a dependency's manifest is not
    this project declaring its own licence.
    """
    wanted = ("package.json", "pyproject.toml", "cargo.toml", "composer.json")
    tracked = {f.lower().lstrip("./") for f in files}
    for rel in files:
        base = rel.lower().split("/")[-1]
        if base not in wanted and not base.endswith(".gemspec"):
            continue
        # Only the project's own manifest, never one nested in a subpackage we
        # happen to have walked.
        if rel.strip("./").count("/") > 0:
            continue
        text = _read_text(os.path.join(project_dir, rel))
        if not text:
            continue
        if base.endswith(".json"):
            try:
                value = (json.loads(text) or {}).get("license")
            except Exception:            # a malformed manifest declares nothing
                continue
            # npm also allows the deprecated {"type": ..., "url": ...} form,
            # and Composer's multi-licence form is a LIST of SPDX ids
            # ("license": ["MIT", "GPL-3.0-or-later"]) - rejecting the list
            # reproduced the exact "no licence field" this function removes.
            if isinstance(value, dict):
                value = value.get("type")
            if isinstance(value, list):
                value = next((v for v in value
                              if isinstance(v, str) and v.strip()), None)
            if isinstance(value, str) and value.strip():
                return rel
        elif base.endswith(".gemspec"):
            if re.search(r"""(?m)^\s*\w+\.license\s*=\s*['"][^'"]+['"]""", text):
                return rel
        else:
            # TOML: the key MUST belong to the table that describes the
            # distributable package. An unscoped search matched
            # `[tool.foo] license = "MIT"` or `[package.metadata.x]` and passed
            # a project whose own package declares nothing.
            if _toml_package_license(text):
                return rel
            # Cargo's `license-file` names a file by PATH, and that filename is
            # routinely not license-shaped (EULA.txt), so the basename check in
            # the gate cannot see it. Honour it only when the named file is
            # actually tracked.
            path = _toml_package_license_file(text)
            if path and path.lower().lstrip("./") in tracked:
                return rel
    return None


_TOML_PACKAGE_TABLES = ("package", "project", "tool.poetry")


def _toml_scalar_in_package_table(text: str, key: str) -> str | None:
    """Value of `key` declared directly under a package-describing TOML table.

    A hand-rolled section scan rather than a TOML parse: this module is
    stdlib-only and must not fail closed on a manifest that a strict parser
    would reject for reasons unrelated to the licence.
    """
    section = ""
    pattern = re.compile(r"""^\s*%s\s*=\s*['"]([^'"]+)['"]""" % re.escape(key))
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").strip().strip('"').lower()
            continue
        if section not in _TOML_PACKAGE_TABLES:
            continue
        m = pattern.match(line)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def _toml_package_license(text: str) -> str | None:
    return _toml_scalar_in_package_table(text, "license")


def _toml_package_license_file(text: str) -> str | None:
    return _toml_scalar_in_package_table(text, "license-file")


def _read_text(path: str, limit: int = MAX_CONFIG_BYTES) -> str:
    """Read a config file defensively. Returns "" for anything unreadable.

    Detection must never raise: a project with a broken symlink, a permission
    hole, or a binary file where a manifest is expected still has to produce a
    usable profile rather than crashing the audit at startup.
    """
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return ""
        if os.path.getsize(path) > limit:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except (OSError, ValueError):
        return ""


def _read_json(path: str) -> dict:
    raw = _read_text(path)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _exists(root: str, *rel: str) -> bool:
    return os.path.exists(os.path.join(root, *rel))


def _glob_one(root: str, suffix: str) -> str | None:
    """First direct child of `root` ending in `suffix` (e.g. a .csproj)."""
    try:
        for name in sorted(os.listdir(root)):
            if name.lower().endswith(suffix.lower()):
                return name
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- #
# Toolchain model
# --------------------------------------------------------------------------- #
@dataclass
class Toolchain:
    """One buildable component: how to install, build, test and check it.

    `root` is RELATIVE to the audited project so the record stays portable in
    reports and brain.json. Command lists are argv lists (never shell strings)
    because they are handed to flexfactor._run, which gates and _winify's them.
    """
    ecosystem: str                       # "node" | "python" | "go" | ...
    root: str                            # relative dir, "." for project root
    manager: str                         # "npm" | "poetry" | "cargo" | ...
    marker: str                          # the file that proved it
    install: list[list[str]] = field(default_factory=list)
    build: list[list[str]] = field(default_factory=list)
    test: list[list[str]] = field(default_factory=list)
    lint: list[list[str]] = field(default_factory=list)
    typecheck: list[list[str]] = field(default_factory=list)
    run: list[str] | None = None
    lockfile: str | None = None
    deps_installed: bool = False
    # Whether the BUILD command needs the dependency tree present to be
    # meaningful. `python -m compileall` and `gofmt` parse without resolving a
    # single import, so those stay verifiable on a bare checkout; `tsc`, `cargo
    # check` and `dotnet build` do not. Conflating the two made a working Python
    # project report as unverifiable purely for lacking a .venv.
    build_needs_deps: bool = True
    is_web: bool = False
    framework: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# Per-file syntax gates. The audit writes a fix then wants a sub-second answer
# to "did I just break the parse?" without paying for a whole-project build.
# {ext: (argv_template, uses_relative_path)} - "{file}" is substituted.
# Only checks that are genuinely FAST and genuinely PARSE-ONLY belong here; a
# check that needs a resolved classpath or crate graph is not a per-file gate.
SYNTAX_GATES: dict[str, list[str]] = {
    ".py": ["python", "-m", "py_compile", "{file}"],
    ".rb": ["ruby", "-c", "{file}"],
    ".php": ["php", "-l", "{file}"],
    ".go": ["gofmt", "-e", "-l", "{file}"],
    ".sh": ["bash", "-n", "{file}"],
    ".bash": ["bash", "-n", "{file}"],
    ".lua": ["luac", "-p", "{file}"],
    ".pl": ["perl", "-c", "{file}"],
}
# Parsed in-process (no subprocess, no external tool needed at all).
INPROC_SYNTAX_EXTS = frozenset({".json", ".toml"})


def syntax_gate_cmd(rel_path: str) -> list[str] | None:
    """argv for a fast parse-only check of one file, or None if we have none."""
    ext = os.path.splitext(rel_path)[1].lower()
    tmpl = SYNTAX_GATES.get(ext)
    if not tmpl:
        return None
    return [rel_path if part == "{file}" else part for part in tmpl]


def inproc_syntax_ok(project_dir: str, rel_path: str) -> tuple[bool | None, str]:
    """Parse JSON/TOML in-process. (None, reason) when the type isn't handled.

    Config files are a real fix target (an audit routinely rewrites a tsconfig
    or a pyproject) and a corrupted one breaks the build in a way that is
    tedious to trace back, so gating them is worth the few lines.
    """
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in INPROC_SYNTAX_EXTS:
        return None, ""
    path = os.path.join(project_dir, rel_path)
    # EMPTY IS A DEFINITE FAILURE, NOT "NOTHING WAS VERIFIED". `_read_text`
    # collapses empty / over-limit / unreadable into "", and the caller's gate
    # KEEPS a file whose check returns None (only False rolls it back) - so a
    # model fix that TRUNCATED a tsconfig.json to zero bytes used to survive
    # the gate. Stat first: 0 bytes is provably not valid JSON/TOML (False);
    # over-limit and unreadable genuinely verify nothing (None). Tri-state
    # contract, permissive direction.
    limit = 8 * 1024 * 1024
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, "unreadable or empty"
    if size == 0:
        name = ext.lstrip(".")
        return False, f"{name} file is empty (not valid {name})"
    if size > limit:
        return None, "unreadable or empty"
    raw = _read_text(path, limit=limit)
    if not raw:
        return None, "unreadable or empty"
    try:
        if ext == ".json":
            json.loads(raw)
        else:
            import tomllib
            tomllib.loads(raw)
        return True, f"{ext.lstrip('.')} parse"
    except ImportError:                      # tomllib is 3.11+; degrade honestly
        return None, "no toml parser available"
    except (ValueError, TypeError) as exc:
        return False, f"{ext.lstrip('.')} parse error: {exc}"


# --------------------------------------------------------------------------- #
# Per-ecosystem detectors
# --------------------------------------------------------------------------- #
def _detect_node(root: str, rel: str) -> Toolchain | None:
    pkg_path = os.path.join(root, "package.json")
    if not os.path.isfile(pkg_path):
        return None
    data = _read_json(pkg_path)
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    deps = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)

    # Manager is decided by the LOCKFILE, not by preference: running `npm ci` in
    # a pnpm workspace produces a broken tree, and the resulting build failure
    # would be blamed on the audit's fixes rather than on the wrong installer.
    manager, lockfile, install = "npm", None, ["npm", "install"]
    if _exists(root, "pnpm-lock.yaml"):
        manager, lockfile, install = "pnpm", "pnpm-lock.yaml", ["pnpm", "install"]
    elif _exists(root, "yarn.lock"):
        manager, lockfile, install = "yarn", "yarn.lock", ["yarn", "install"]
    elif _exists(root, "bun.lockb") or _exists(root, "bun.lock"):
        manager, lockfile, install = "bun", "bun.lockb", ["bun", "install"]
    elif _exists(root, "package-lock.json"):
        # `npm ci` is the reproducible path, but it hard-fails when the lockfile
        # has drifted from package.json - common in a repo that needs auditing.
        # `npm install` reconciles instead, which is what we actually want here.
        manager, lockfile = "npm", "package-lock.json"

    tc = Toolchain(ecosystem="node", root=rel, manager=manager,
                   marker="package.json", install=[install], lockfile=lockfile,
                   deps_installed=os.path.isdir(os.path.join(root, "node_modules")))

    runner = [manager, "run"] if manager != "npm" else ["npm", "run"]
    for name in ("build", "compile"):
        if name in scripts:
            tc.build.append(runner + [name])
            break
    for name in ("typecheck", "type-check", "tsc"):
        if name in scripts:
            tc.typecheck.append(runner + [name])
            break
    if not tc.typecheck and _exists(root, "tsconfig.json"):
        tc.typecheck.append(["npx", "tsc", "--noEmit"])
    if "lint" in scripts:
        tc.lint.append(runner + ["lint"])
    for name in ("test:unit", "unit", "test"):
        if name in scripts:
            tc.test.append(runner + [name])
            break
    for name in ("dev", "start", "serve"):
        if name in scripts:
            tc.run = runner + [name]
            break

    for fw in ("next", "nuxt", "vite", "react-scripts", "@angular/core",
               "svelte", "vue", "astro", "remix", "express", "fastify",
               "@nestjs/core", "koa", "hapi"):
        if fw in deps:
            tc.framework = fw
            break
    tc.is_web = any(k in deps for k in
                    ("react", "next", "vite", "vue", "svelte", "@angular/core",
                     "astro", "remix", "nuxt"))
    return tc


def _detect_python(root: str, rel: str) -> Toolchain | None:
    markers = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
               "Pipfile")
    marker = next((m for m in markers if _exists(root, m)), None)
    if not marker:
        return None

    pyproject = _read_text(os.path.join(root, "pyproject.toml"))
    manager, install, lockfile = "pip", [], None
    if _exists(root, "uv.lock") or "[tool.uv]" in pyproject:
        manager, install, lockfile = "uv", [["uv", "sync"]], "uv.lock"
    elif _exists(root, "poetry.lock") or "[tool.poetry]" in pyproject:
        manager, install, lockfile = "poetry", [["poetry", "install"]], "poetry.lock"
    elif _exists(root, "pdm.lock"):
        manager, install, lockfile = "pdm", [["pdm", "install"]], "pdm.lock"
    elif _exists(root, "Pipfile"):
        manager, install, lockfile = "pipenv", [["pipenv", "install", "--dev"]], "Pipfile.lock"
    elif _exists(root, "requirements.txt"):
        install = [["python", "-m", "pip", "install", "-r", "requirements.txt"]]
        # A requirements.txt with `==` pins IS this ecosystem's reproducibility
        # mechanism - there is no separate lockfile to demand. Treating its
        # absence as "unpinned" would fail a correctly-pinned project.
        if "==" in _read_text(os.path.join(root, "requirements.txt")):
            lockfile = "requirements.txt"
    elif _exists(root, "pyproject.toml") or _exists(root, "setup.py"):
        # Editable install of the project itself pulls its declared deps.
        install = [["python", "-m", "pip", "install", "-e", "."]]
    if _exists(root, "requirements-dev.txt"):
        install.append(["python", "-m", "pip", "install", "-r", "requirements-dev.txt"])

    tc = Toolchain(ecosystem="python", root=rel, manager=manager, marker=marker,
                   install=install, lockfile=lockfile,
                   # compileall parses without importing, so the build gate works
                   # on a bare checkout even though the TEST command still needs
                   # the dependencies that bootstrap installs.
                   build_needs_deps=False,
                   deps_installed=any(os.path.isdir(os.path.join(root, d))
                                      for d in (".venv", "venv", ".tox")))
    prefix = {"poetry": ["poetry", "run"], "uv": ["uv", "run"],
              "pdm": ["pdm", "run"], "pipenv": ["pipenv", "run"]}.get(manager, [])
    tc.test.append(prefix + ["python", "-m", "pytest", "-q"])
    # Compile-all is the closest thing Python has to a build gate: it catches
    # every syntax error in the tree without importing (and thus without running)
    # a single module. Importing would execute audited code - not acceptable.
    tc.build.append(prefix + ["python", "-m", "compileall", "-q", "."])
    if "[tool.mypy]" in pyproject or _exists(root, "mypy.ini"):
        tc.typecheck.append(prefix + ["python", "-m", "mypy", "."])
    if "[tool.ruff]" in pyproject or _exists(root, "ruff.toml"):
        tc.lint.append(prefix + ["python", "-m", "ruff", "check", "."])
    return tc


def _detect_go(root: str, rel: str) -> Toolchain | None:
    if not _exists(root, "go.mod"):
        return None
    return Toolchain(
        ecosystem="go", root=rel, manager="go", marker="go.mod",
        install=[["go", "mod", "download"]],
        build=[["go", "build", "./..."]],
        test=[["go", "test", "./..."]],
        lint=[["go", "vet", "./..."]],
        lockfile="go.sum" if _exists(root, "go.sum") else None,
        deps_installed=True)  # module cache is global; no per-repo dir to check


def _detect_rust(root: str, rel: str) -> Toolchain | None:
    if not _exists(root, "Cargo.toml"):
        return None
    return Toolchain(
        ecosystem="rust", root=rel, manager="cargo", marker="Cargo.toml",
        install=[["cargo", "fetch"]],
        # `cargo check` type-checks the whole crate graph without codegen: the
        # full correctness signal at a fraction of a release build's time.
        build=[["cargo", "check", "--all-targets"]],
        test=[["cargo", "test"]],
        lint=[["cargo", "clippy", "--all-targets"]],
        lockfile="Cargo.lock" if _exists(root, "Cargo.lock") else None,
        deps_installed=os.path.isdir(os.path.join(root, "target")))


def _detect_java(root: str, rel: str) -> Toolchain | None:
    if _exists(root, "pom.xml"):
        return Toolchain(
            ecosystem="java", root=rel, manager="maven", marker="pom.xml",
            install=[["mvn", "-B", "-q", "dependency:go-offline"]],
            build=[["mvn", "-B", "-q", "compile"]],
            test=[["mvn", "-B", "test"]],
            deps_installed=os.path.isdir(os.path.join(root, "target")))
    gradle = next((m for m in ("build.gradle", "build.gradle.kts")
                   if _exists(root, m)), None)
    if gradle:
        # Prefer the wrapper: it pins the Gradle version the project was built
        # with, so we never silently build under a different toolchain.
        exe = "./gradlew" if _exists(root, "gradlew") else "gradle"
        if os.name == "nt" and _exists(root, "gradlew.bat"):
            exe = "gradlew.bat"
        return Toolchain(
            ecosystem="java", root=rel, manager="gradle", marker=gradle,
            install=[[exe, "--quiet", "dependencies"]],
            build=[[exe, "--quiet", "compileJava"]],
            test=[[exe, "test"]],
            deps_installed=os.path.isdir(os.path.join(root, "build")))
    return None


def _detect_dotnet(root: str, rel: str) -> Toolchain | None:
    marker = (_glob_one(root, ".sln") or _glob_one(root, ".csproj")
              or _glob_one(root, ".fsproj") or _glob_one(root, ".vbproj"))
    if not marker:
        return None
    return Toolchain(
        ecosystem="dotnet", root=rel, manager="dotnet", marker=marker,
        # --use-lock-file is what makes the deps_pinned gate closable at all:
        # bare `dotnet restore` never writes packages.lock.json (the one file
        # _LOCKFILES recognises for dotnet), so the gate's "run the installer"
        # remediation was an instruction whose execution could not satisfy it
        # and every .NET repo stayed NOT PRODUCTION READY forever.
        install=[["dotnet", "restore", "--use-lock-file"]],
        build=[["dotnet", "build", "--nologo", "--no-restore"]],
        test=[["dotnet", "test", "--nologo", "--no-build"]],
        deps_installed=os.path.isdir(os.path.join(root, "obj")))


def _detect_ruby(root: str, rel: str) -> Toolchain | None:
    if not _exists(root, "Gemfile"):
        return None
    tc = Toolchain(
        ecosystem="ruby", root=rel, manager="bundler", marker="Gemfile",
        install=[["bundle", "install"]],
        test=[["bundle", "exec", "rspec"]] if _exists(root, "spec")
        else [["bundle", "exec", "rake", "test"]],
        lockfile="Gemfile.lock" if _exists(root, "Gemfile.lock") else None,
        deps_installed=os.path.isdir(os.path.join(root, "vendor", "bundle")))
    if _exists(root, ".rubocop.yml"):
        tc.lint.append(["bundle", "exec", "rubocop"])
    return tc


def _detect_php(root: str, rel: str) -> Toolchain | None:
    if not _exists(root, "composer.json"):
        return None
    return Toolchain(
        ecosystem="php", root=rel, manager="composer", marker="composer.json",
        install=[["composer", "install", "--no-interaction"]],
        test=[["composer", "exec", "phpunit"]] if _exists(root, "phpunit.xml")
        else [],
        lockfile="composer.lock" if _exists(root, "composer.lock") else None,
        deps_installed=os.path.isdir(os.path.join(root, "vendor")))


def _detect_elixir(root: str, rel: str) -> Toolchain | None:
    if not _exists(root, "mix.exs"):
        return None
    return Toolchain(
        ecosystem="elixir", root=rel, manager="mix", marker="mix.exs",
        install=[["mix", "deps.get"]],
        build=[["mix", "compile", "--warnings-as-errors"]],
        test=[["mix", "test"]],
        lockfile="mix.lock" if _exists(root, "mix.lock") else None,
        deps_installed=os.path.isdir(os.path.join(root, "deps")))


def _detect_native(root: str, rel: str) -> Toolchain | None:
    if _exists(root, "CMakeLists.txt"):
        return Toolchain(
            ecosystem="cpp", root=rel, manager="cmake", marker="CMakeLists.txt",
            install=[["cmake", "-S", ".", "-B", "build"]],
            build=[["cmake", "--build", "build"]],
            test=[["ctest", "--test-dir", "build"]],
            deps_installed=os.path.isdir(os.path.join(root, "build")))
    if _exists(root, "meson.build"):
        return Toolchain(
            ecosystem="cpp", root=rel, manager="meson", marker="meson.build",
            install=[["meson", "setup", "build"]],
            build=[["meson", "compile", "-C", "build"]],
            test=[["meson", "test", "-C", "build"]])
    if _exists(root, "Makefile") or _exists(root, "makefile"):
        # A bare Makefile gives us a build but no reliable test target name, and
        # guessing one wrong would report a phantom failure. Build only.
        return Toolchain(ecosystem="make", root=rel, manager="make",
                         marker="Makefile", build=[["make"]])
    return None


def _detect_deno(root: str, rel: str) -> Toolchain | None:
    marker = next((m for m in ("deno.json", "deno.jsonc") if _exists(root, m)), None)
    if not marker:
        return None
    return Toolchain(
        ecosystem="deno", root=rel, manager="deno", marker=marker,
        install=[["deno", "cache", "--reload", marker]],
        build=[["deno", "check", "."]],
        test=[["deno", "test", "-A"]],
        lint=[["deno", "lint"]],
        lockfile="deno.lock" if _exists(root, "deno.lock") else None,
        deps_installed=True)


def _detect_dart(root: str, rel: str) -> Toolchain | None:
    if not _exists(root, "pubspec.yaml"):
        return None
    flutter = "flutter:" in _read_text(os.path.join(root, "pubspec.yaml"))
    exe = "flutter" if flutter else "dart"
    return Toolchain(
        ecosystem="dart", root=rel, manager=exe, marker="pubspec.yaml",
        install=[[exe, "pub", "get"]],
        build=[[exe, "analyze"]],
        test=[[exe, "test"]],
        lockfile="pubspec.lock" if _exists(root, "pubspec.lock") else None,
        deps_installed=os.path.isdir(os.path.join(root, ".dart_tool")))


def _detect_swift(root: str, rel: str) -> Toolchain | None:
    if not _exists(root, "Package.swift"):
        return None
    return Toolchain(
        ecosystem="swift", root=rel, manager="swiftpm", marker="Package.swift",
        install=[["swift", "package", "resolve"]],
        build=[["swift", "build"]],
        test=[["swift", "test"]],
        lockfile="Package.resolved" if _exists(root, "Package.resolved") else None)


# Order matters only for report readability; every detector that matches runs.
_DETECTORS = (_detect_node, _detect_deno, _detect_python, _detect_go,
              _detect_rust, _detect_java, _detect_dotnet, _detect_ruby,
              _detect_php, _detect_elixir, _detect_dart, _detect_swift,
              _detect_native)


def detect_toolchains(project_dir: str,
                      max_depth: int = MAX_SCAN_DEPTH) -> list[Toolchain]:
    """Every buildable component in the tree, root first.

    Monorepos are the common case in this portfolio (npm workspaces, pnpm,
    services/*), so this walks to `max_depth` rather than only inspecting the
    root. A directory can yield several toolchains: a Node service with a Python
    worker beside it is one project with two build systems, and verifying only
    one of them is how an unverified fix reaches a report as "gated".
    """
    if not os.path.isdir(project_dir):
        return []
    found: list[Toolchain] = []
    seen: set[tuple[str, str]] = set()

    for current, dirnames, _files in os.walk(project_dir):
        rel_dir = os.path.relpath(current, project_dir).replace("\\", "/")
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
        for detector in _DETECTORS:
            try:
                tc = detector(current, rel_dir)
            except Exception:
                # A single malformed manifest must not abort detection of the
                # rest of the tree; the component simply goes undetected and
                # the readiness rubric reports reduced coverage.
                tc = None
            if tc and (tc.ecosystem, tc.root) not in seen:
                seen.add((tc.ecosystem, tc.root))
                found.append(tc)
    # Root-level toolchains first, then shallowest, then alphabetical - so the
    # primary component of a monorepo leads the report.
    found.sort(key=lambda t: (t.root != ".", t.root.count("/"), t.root,
                              t.ecosystem))
    return found

# --------------------------------------------------------------------------- #
# Bootstrap: make the build gate MEAN something
# --------------------------------------------------------------------------- #
@dataclass
class StepResult:
    label: str
    cmd: list[str]
    cwd: str
    ok: bool
    detail: str = ""
    skipped: bool = False


def bootstrap_plan(toolchains: list[Toolchain], allow_scripts: bool = False,
                   force: bool = False) -> list[tuple[Toolchain, list[str]]]:
    """(toolchain, argv) install steps to run, in order.

    `allow_scripts=False` appends --ignore-scripts to npm/pnpm/yarn installs.
    This mirrors the policy scout already enforces: installing a dependency tree
    runs that tree's postinstall hooks, which is arbitrary third-party code
    execution on the owner's machine triggered by a repo they merely pointed the
    tool at. Some native packages genuinely need their scripts to build, so the
    escape hatch exists - it is just not the default.
    """
    plan: list[tuple[Toolchain, list[str]]] = []
    for tc in toolchains:
        if tc.deps_installed and not force:
            continue
        for cmd in tc.install:
            argv = list(cmd)
            if not allow_scripts and argv and argv[0] in ("npm", "pnpm", "yarn", "bun"):
                argv.append("--ignore-scripts")
            plan.append((tc, argv))
    return plan


def run_bootstrap(project_dir: str, toolchains: list[Toolchain], run,
                  allow_scripts: bool = False, force: bool = False,
                  timeout: int = 1800, log=None) -> list[StepResult]:
    """Execute the install plan through the caller's gated `run`.

    `run(cmd, cwd, timeout=...)` must return an object with `.returncode`,
    `.stdout`, `.stderr` - i.e. flexfactor._run, which never raises and which
    routes through cmdpolicy + _winify. Failures are RECORDED, not raised: a
    project whose install fails is still worth auditing (the review phase needs
    no toolchain at all), it just cannot claim a verified build - and
    `verification_is_real` is what enforces that distinction downstream.
    """
    results: list[StepResult] = []
    for tc, cmd in bootstrap_plan(toolchains, allow_scripts, force):
        cwd = os.path.normpath(os.path.join(project_dir, tc.root))
        if log:
            log(f"    bootstrap [{tc.ecosystem}:{tc.root}]: {' '.join(cmd)}")
        r = run(cmd, cwd, timeout=timeout)
        ok = getattr(r, "returncode", 1) == 0
        detail = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "")[-2000:]
        results.append(StepResult(label=f"install:{tc.ecosystem}:{tc.root}",
                                  cmd=cmd, cwd=tc.root, ok=ok, detail=detail))
        if ok:
            tc.deps_installed = True
    return results


def _host_can_build(tc: "Toolchain") -> bool:
    """False when this machine could not build the component whatever the owner
    does. Narrow on purpose, so a genuine "dependencies not installed" is still
    reported for everything else.

    SWIFT IS NOT APPLE-ONLY (caught in review): Swift and SwiftPM support Linux
    and Windows, so classifying every `swift` component as unbuildable off macOS
    would SUPPRESS real missing-dependency failures for server-side and
    cross-platform Swift packages - the permissive direction, and the opposite
    of this gate's job. Swift therefore counts as foreign only when this host
    has no Swift toolchain at all, which is measured rather than assumed.

    There used to be an `_APPLE_ONLY_ECOSYSTEMS = {"xcode", "cocoapods", "ios"}`
    branch here - deleted 2026-08-30 because NO detector in `_DETECTORS` ever
    emits those ecosystem strings (the emitted set is node/deno/python/go/rust/
    java/dotnet/ruby/php/elixir/dart/swift/cpp/make), so the branch was inert
    while reading as live Apple-toolchain coverage. The real Capacitor-ios case
    it cited is served by the measured `swift` test below. If an Xcode/CocoaPods
    detector is ever ADDED, re-key this on the string that detector actually
    emits - a check nothing can reach is worse than no check.
    """
    if sys.platform == "darwin":
        return True
    if tc.ecosystem == "swift":
        return shutil.which("swift") is not None
    return True


def verification_is_real(toolchains: list[Toolchain]) -> tuple[bool, str]:
    """Can we actually prove a change didn't break this project?

    The honesty guard for the whole mode. `_full_gate` returning True because it
    had no commands to run is indistinguishable, at the call site, from a build
    that genuinely passed. Callers use this to label the difference so a report
    never claims verification that never happened.
    """
    if not toolchains:
        return False, "no build system detected - changes cannot be build-verified"
    buildable = [t for t in toolchains if t.build]
    if not buildable:
        eco = ", ".join(sorted({t.ecosystem for t in toolchains}))
        return False, f"detected {eco} but no usable build command - changes are UNVERIFIED"
    # `t.install` guards the no-installer case: a bare Makefile or meson project
    # has no dependency step at all, so "deps not installed" is not a coherent
    # complaint about it - there is nothing that could have installed them.
    missing = [t for t in buildable
               if t.build_needs_deps and t.install and not t.deps_installed]
    # A COMPONENT THIS HOST CANNOT BUILD AT ALL IS NOT AN INSTALL FAILURE.
    #
    # Measured on GrantFlow 2026-08-30 (win32): node + three gradle components
    # all had deps_installed=True, and the ENTIRE program was reported
    # "Changes can be build-verified: FAIL [critical] - dependencies not
    # installed for swift:ios/App/CapApp-SPM". That component is a
    # Capacitor-generated iOS Swift package; Apple toolchains require macOS and
    # neither swift nor xcodebuild exists on this machine, so its dependencies
    # can NEVER be installed here. The finding was unclosable by any action the
    # owner could take, on a CRITICAL gate, while four of five components were
    # fully verifiable.
    #
    # The honesty guard is preserved, not weakened: the unbuildable components
    # are NAMED in the message so the verification claim stays scoped to what
    # was actually provable, and if NOTHING is buildable on this host the gate
    # still fails. What changes is that a platform the owner is not on can no
    # longer veto verification of the parts that do build here.
    foreign = [t for t in missing if not _host_can_build(t)]
    missing = [t for t in missing if _host_can_build(t)]
    # THE VERDICT STAYS CONSERVATIVE; ONLY THE SENTENCE GETS HONEST.
    #
    # An unbootstrapped component means FlexFactor's fixes TO THAT COMPONENT are
    # unverified, and `False` here is what makes the readiness scorecard record
    # `final_build = None`. Returning `True` on the strength of a sibling
    # component's green suite would let a fix land in the unverified one - the
    # exact overclaim this function exists to prevent - so the boolean is
    # deliberately unchanged, and `test_swift_with_a_REAL_toolchain_is_not_foreign`
    # pins it.
    #
    # What WAS wrong is the sentence. Measured 2026-09-01 on two repos: GrantFlow
    # (java, node, swift) and Ellie (java, node, python) both reported only
    # "dependencies not installed for <one component> - build gate would
    # false-fail", which the report renders as "Build verification: NOT AVAILABLE
    # ... Fixes in this run were NOT build-verified". In both, node - holding
    # those projects' 8242- and 1034-test suites - was fully bootstrapped, and
    # `_full_gate` ran its commands. So the line said nothing was verified while
    # something was, named no action beyond a component the owner may not be able
    # to bootstrap at all, and appeared on every single run. A critical-severity
    # line that is unactionable and always present is one an operator learns to
    # scroll past, which costs the gate its teeth on the day it is real.
    #
    # Naming BOTH halves keeps the refusal and makes it useful.
    verifiable = [t for t in buildable if _host_can_build(t) and t not in missing]
    if missing:
        bad = ", ".join(f"{t.ecosystem}:{t.root}" for t in missing)
        note = f"dependencies not installed for {bad} - build gate would false-fail"
        if verifiable:
            ok = ", ".join(f"{t.ecosystem}:{t.root}" for t in verifiable)
            note += f"; {ok} IS bootstrapped and was verified"
        if foreign:
            note += ("; not verifiable on this host "
                     f"({sys.platform}): "
                     + ", ".join(f"{t.ecosystem}:{t.root}" for t in foreign))
        return False, note
    local = [t for t in buildable if _host_can_build(t)]
    if not local:
        eco = ", ".join(sorted({t.ecosystem for t in buildable}))
        return False, (f"the only build system(s) detected ({eco}) cannot run on "
                       f"this host ({sys.platform}) - changes are UNVERIFIED here")
    if foreign:
        names = ", ".join(f"{t.ecosystem}:{t.root}" for t in foreign)
        return True, ("build verification available; NOT verifiable on this host "
                      f"({sys.platform}): {names}")
    return True, "build verification available"


# --------------------------------------------------------------------------- #
# The production-readiness rubric
# --------------------------------------------------------------------------- #
@dataclass
class Gate:
    """One checkable production-readiness property.

    `status` is deliberately four-valued. "unknown" is not a synonym for "fail":
    conflating them either cries wolf (blocking a release over an undetectable
    property) or hides a real hole. Kept distinct, the scorecard can say
    truthfully which gates were actually evaluated.
    """
    id: str
    title: str
    status: str          # "pass" | "fail" | "na" | "unknown"
    severity: str        # "critical" | "high" | "medium" | "low"
    evidence: str = ""
    remediation: str = ""
    auto_fixable: bool = False
    # THE FILE A REMEDIATION WOULD EDIT, repo-relative, when one is knowable.
    #
    # Without it a readiness blocker is unfixable BY CONSTRUCTION: the audit
    # turns each one into a finding filed against the placeholder "(readiness)",
    # and `_fix_files` only ever edits real paths - so no run, and no number of
    # runs, could close it. Measured: repo-rewards carried "License declared:
    # FAIL" across four runs, IPlay "no lockfile: python:." across twelve, and
    # GrantFlow's persistence findings across sixteen. Every one was reported
    # every time and fixed never.
    #
    # `auto_fixable` claimed these were fixable while nothing could act on them.
    # This field is what makes that claim true.
    #
    # A LIST, because a monorepo can have several unpinned components and a
    # single path would send the fix loop at one of them while the gate stayed
    # red for the rest (caught in review).
    paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# Gates at or above this severity must PASS before anything may be called
# production ready. Everything below is reported but does not block.
#
# ===> TUNABLE <=== This line encodes the definition of "production ready".
# Raising it to "critical" ships faster and accepts more risk; lowering it to
# "medium" blocks on documentation and CI gaps too. "high" is the defensible
# middle: it blocks on anything that can break or expose the running system
# (secrets, broken build, failing tests, unpinned deps) while letting
# docs/CI/licence gaps be reported rather than veto a release.
BLOCKING_SEVERITY = "high"
_SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}

_SECRET_FILE_PAT = re.compile(
    r"(?:^|/)(?:\.env(?:\.[\w-]+)?|.*\.pem|.*\.p12|.*\.pfx|id_rsa|id_ed25519|"
    r".*\.keystore|credentials\.json|serviceaccount\.json)$", re.I)
# .env.example / .env.sample / .env.template are the DOCUMENTED-config pattern,
# the opposite of a leaked secret - they must never trip the secret gate.
_SECRET_FILE_SAFE = re.compile(r"\.(?:example|sample|template|dist)$", re.I)

_CI_PATHS = (".github/workflows", ".gitlab-ci.yml", ".circleci/config.yml",
             "azure-pipelines.yml", "Jenkinsfile", ".travis.yml",
             "bitbucket-pipelines.yml", ".drone.yml")
_TEST_DIR_NAMES = frozenset({"test", "tests", "spec", "specs", "__tests__",
                             "e2e", "it", "testing"})
# Matches test_x.py, x_test.go, x_tests.py, x.test.ts, x.spec.js - the plural and
# dotted forms included, because requiring the singular missed real suites
# (flexfactor's own `flexfactor_tests.py` among them). The separator before
# "test" is required so `latest.py` and `contest.js` don't register as suites.
_TEST_FILE_PAT = re.compile(
    r"(?:^|[/\\])(?:tests?[_.-][^/\\]+|[^/\\]+[_.-]tests?|[^/\\]+\.(?:test|spec))"
    r"\.[\w]+$", re.I)


def _tracked_files(project_dir: str, run) -> list[str]:
    """git-tracked paths, or a bounded filesystem walk when git is unavailable.

    Tracked-vs-present is the distinction that matters for the secret gate: a
    local .env is correct practice, a COMMITTED .env is the incident.
    """
    try:
        r = run(["git", "ls-files"], project_dir, timeout=120)
        if getattr(r, "returncode", 1) == 0 and getattr(r, "stdout", "").strip():
            return [ln.strip().replace("\\", "/")
                    for ln in r.stdout.splitlines() if ln.strip()]
    except Exception:
        pass
    out: list[str] = []
    for current, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            rel = os.path.relpath(os.path.join(current, name), project_dir)
            out.append(rel.replace("\\", "/"))
            if len(out) > 20000:
                return out
    return out


# Lockfile names by package manager, for the re-check below.
_LOCKFILES = {
    "npm": ("package-lock.json", "npm-shrinkwrap.json"),
    "pnpm": ("pnpm-lock.yaml",), "yarn": ("yarn.lock",),
    "bun": ("bun.lockb", "bun.lock"), "poetry": ("poetry.lock",),
    "uv": ("uv.lock",), "pdm": ("pdm.lock",), "pipenv": ("Pipfile.lock",),
    "go": ("go.sum",), "cargo": ("Cargo.lock",), "bundler": ("Gemfile.lock",),
    "composer": ("composer.lock",), "mix": ("mix.lock",), "deno": ("deno.lock",),
    "dart": ("pubspec.lock",), "flutter": ("pubspec.lock",),
    "swiftpm": ("Package.resolved",),
    # `dotnet restore --use-lock-file` writes this. Without the entry the
    # remediation promised that running the installer would close the gate
    # while nothing could ever recognise its output (caught in review).
    "dotnet": ("packages.lock.json",),
}


def _current_lockfile(project_dir: str, tc: Toolchain) -> str | None:
    """Look for the lockfile ON DISK right now, rather than trusting what
    detection saw. Bootstrap runs between the two and routinely CREATES it
    (`npm install` writes package-lock.json), so the detect-time value reports a
    project as unpinned moments after the tool pinned it."""
    # BOTH branches must reject a fabrication, not just the _LOCKFILES loop
    # below: the detector's own recorded lockfile is checked FIRST, so a fake
    # one found at detection time would otherwise sail straight past. (My first
    # version guarded only the second branch; the end-to-end test caught it.)
    if tc.lockfile and _exists(project_dir, tc.root, tc.lockfile):
        if not _lockfile_is_fabricated(
                os.path.join(project_dir, tc.root, tc.lockfile)):
            return tc.lockfile
    root = os.path.join(project_dir, tc.root)
    for name in _LOCKFILES.get(tc.manager, ()):
        if _exists(root, name) and not _lockfile_is_fabricated(
                os.path.join(root, name)):
            return name
    # pip's pinning mechanism is a pinned requirements.txt, not a lockfile.
    if tc.manager == "pip" and "==" in _read_text(
            os.path.join(root, "requirements.txt")):
        return "requirements.txt"
    # A component that declares NO dependencies has nothing to pin, and no
    # lockfile is generated for it. Reporting that as a pinning failure is a
    # false blocker - go.sum is simply absent for a dependency-free module.
    if tc.manager == "go" and "require" not in _read_text(
            os.path.join(root, "go.mod")):
        return "go.mod (no dependencies)"
    # GRADLE AND MAVEN PIN BY DECLARATION, and _LOCKFILES had no entry for
    # either - so `.get(manager, ())` was always empty and EVERY Java component
    # failed this high-severity gate permanently, with no action that could ever
    # close it. That is the same unclosable-finding shape as the licence gate,
    # and it is worse here because this one BLOCKS.
    #
    # Measured on sermonsmith 2026-08-30: apps/mobile/android is a Capacitor
    # shell whose every version is an exact pin in variables.gradle
    # (androidxCoreVersion = '1.17.0', ...) with ZERO dynamic versions anywhere
    # in its .gradle files - and it was reported "no lockfile: java:apps/mobile/
    # android" as one of three blockers keeping the program NOT PRODUCTION
    # READY.
    #
    # This mirrors the pip precedent directly above: a pinned requirements.txt
    # IS pip's pinning mechanism, and exact declared versions are Gradle's and
    # Maven's. The teeth stay in: a DYNAMIC version anywhere ("1.+", "+",
    # "latest.release", or a Maven range/LATEST/RELEASE) means the build is not
    # reproducible and still fails.
    if tc.manager in ("gradle", "maven"):
        declared = _declared_jvm_versions(root, tc.manager)
        if declared == "dynamic":
            return None
        if declared == "pinned":
            return ("exact declared versions (gradle)" if tc.manager == "gradle"
                    else "exact declared versions (pom.xml)")
    return None


# Gradle dependency locking, when a project opts into it, writes one of these.
_GRADLE_LOCK_DIR = os.path.join("gradle", "dependency-locks")
_GRADLE_LOCK_FILES = ("gradle.lockfile", "buildscript-gradle.lockfile")

# A version that is resolved at build time rather than written down. Gradle:
# "1.+", "+", "latest.release". Maven: "LATEST", "RELEASE", and range syntax
# "[1.0,2.0)". Any of these means two builds can differ.
_DYNAMIC_GRADLE_RE = re.compile(
    r"""["']([A-Za-z0-9._-]+):([A-Za-z0-9._-]+):([^"']*(?:\+|latest\.\w+))["']""",
    re.I)
_DYNAMIC_MAVEN_RE = re.compile(
    r"<version>\s*(?:LATEST|RELEASE|[\[\(][^<]*)\s*</version>", re.I)
# `libVersion = '1.+'` / `libVersion = "latest.release"` - a dynamic version
# held in a variable and referenced as "$libVersion" in the coordinate.
# NOT line-anchored: the idiomatic form is `ext { libVersion = '1.+' }`, all on
# one line after an opening brace, so anchoring to ^\s* missed exactly the case
# this exists to catch.
_DYNAMIC_GRADLE_VAR_RE = re.compile(
    r"""(?i)\b\w*version\w*\s*=\s*['"][^'"]*(?:\+|latest\.\w+)['"]""")
# <version>${lib.version}</version> - the range lives in the property, not in
# the <version> tag, so the literal scan above cannot see it.
_MAVEN_PROPERTY_REF_RE = re.compile(r"<version>\s*\$\{([^}]+)\}\s*</version>")


def _maven_property(text: str, name: str) -> str:
    m = re.search(r"<%s>\s*([^<]*)\s*</%s>" % (re.escape(name), re.escape(name)),
                  text, re.I)
    return (m.group(1) or "").strip() if m else ""


def _declared_jvm_versions(root: str, manager: str) -> str:
    """"pinned" | "dynamic" | "none" for a Gradle/Maven component.

    "none" means no dependency declarations were found at all, which is NOT the
    same as pinned - the caller must not treat an unreadable or empty component
    as satisfied.
    """
    if manager == "gradle":
        if any(_exists(root, n) for n in _GRADLE_LOCK_FILES):
            return "pinned"
        # An EMPTY gradle/dependency-locks directory is an abandoned or failed
        # locking setup, not a lock (caught in review). Returning "pinned" for
        # the bare directory let a project with `a:b:1.+` pass without any
        # declaration ever being read.
        lock_dir = os.path.join(root, _GRADLE_LOCK_DIR)
        if os.path.isdir(lock_dir):
            try:
                # An actual *.lockfile, not merely "some file" - a `.keep` or a
                # stray README is not a lock, and accepting one would pass a
                # project whose declarations were never read.
                if any(n.lower().endswith(".lockfile")
                       and os.path.isfile(os.path.join(lock_dir, n))
                       for n in os.listdir(lock_dir)):
                    return "pinned"
            except OSError:
                pass
        texts, saw_dep = [], False
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name.endswith((".gradle", ".gradle.kts", ".properties")):
                    texts.append(_read_text(os.path.join(current, name)))
        for text in texts:
            if not text:
                continue
            if _DYNAMIC_GRADLE_RE.search(text):
                return "dynamic"
            # A DYNAMIC VERSION HELD IN A VARIABLE (caught in review). The
            # common Gradle form is `libVersion = '1.+'` followed by
            # `implementation "a:b:$libVersion"` - the coordinate regex above
            # sees no `+` because the version is not in the string, and the
            # generic coordinate check below would then bank it as "pinned".
            # This is the same defect the whole gate exists to prevent, hiding
            # one indirection away.
            if _DYNAMIC_GRADLE_VAR_RE.search(text):
                return "dynamic"
            if re.search(r"""["'][A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[^"']+["']""", text):
                saw_dep = True
            # Capacitor/Android convention: versions live in ext {} as literals
            # and are referenced by name, so the coordinate string alone does
            # not carry them. A quoted x.y(.z) literal is the declaration.
            if re.search(r"""^\s*\w*[Vv]ersion\s*=\s*['"][0-9]+(?:\.[0-9]+)+""",
                         text, re.M):
                saw_dep = True
        return "pinned" if saw_dep else "none"
    text = _read_text(os.path.join(root, "pom.xml"))
    if not text:
        return "none"
    if _DYNAMIC_MAVEN_RE.search(text):
        return "dynamic"
    # A RANGE HELD IN A PROPERTY (caught in review): with
    # <version>${lib.version}</version> and <lib.version>[1.0,2.0)</lib.version>
    # the literal scan above sees only the reference, and the mere presence of
    # a <version> tag then banked it as "pinned". Resolve one level - which is
    # the level Maven itself uses for this idiom - and re-test the value.
    for name in _MAVEN_PROPERTY_REF_RE.findall(text):
        value = _maven_property(text, name)
        if not value:
            continue
        if value.upper() in ("LATEST", "RELEASE") or value[:1] in "[(":
            return "dynamic"
    return "pinned" if "<version>" in text else "none"


# --------------------------------------------------------------------------- #
# JSON-LD structured-data validation (the local, offline equivalent of the
# machine-checkable part of Google's Rich Results Test - which has no free API).
# Google silently IGNORES an invalid application/ld+json block, so broken
# structured data is a silent-failure class: the page ships, the rich result
# never appears, and nothing ever errors.
# --------------------------------------------------------------------------- #
_JSONLD_PAT = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.I | re.S)


def _validate_jsonld(project_dir: str, files: list[str]) -> "tuple[int, list[str]]":
    """Parse every application/ld+json block in tracked .html/.htm files.

    Returns (total_blocks, problems). A block is valid when it parses as JSON
    and every node object carries @context (top level) and @type (per node,
    with @graph items checked individually; a bare @id reference node is
    legal without @type). Bounded: first 200 HTML files, MAX_CONFIG_BYTES per
    read via _read_text - a hostile repo cannot balloon the walk."""
    problems: list[str] = []
    total = 0
    html_files = [f for f in files if f.lower().endswith((".html", ".htm"))]
    for rel in html_files[:200]:
        text = _read_text(os.path.join(project_dir, rel))
        if not text or "ld+json" not in text.lower():
            continue
        for i, m in enumerate(_JSONLD_PAT.finditer(text), 1):
            total += 1
            where = f"{rel}#block{i}"
            raw = m.group(1).strip()
            if not raw:
                problems.append(f"{where}: empty block")
                continue
            try:
                data = json.loads(raw)
            except Exception as exc:
                problems.append(f"{where}: invalid JSON ({exc})")
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    problems.append(
                        f"{where}: top-level {type(node).__name__}, expected object")
                    continue
                if "@context" not in node:
                    problems.append(f"{where}: missing @context")
                graph = node.get("@graph")
                items = graph if isinstance(graph, list) else [node]
                for item in items:
                    if isinstance(item, dict) and "@type" not in item \
                            and "@id" not in item:
                        problems.append(f"{where}: node missing @type")
    return total, problems


def _has_tests(files: list[str]) -> bool:
    for f in files:
        parts = f.lower().split("/")
        if any(p in _TEST_DIR_NAMES for p in parts[:-1]):
            return True
        if _TEST_FILE_PAT.search(f):
            return True
    return False


def assess_readiness(project_dir: str, toolchains: list[Toolchain], run,
                     build_ok: bool | None = None,
                     tests_ok: bool | None = None) -> list[Gate]:
    """Deterministic production-readiness rubric. No model calls, no network.

    Every gate answers a question an operator would ask before putting the thing
    in front of real users. `build_ok`/`tests_ok` are threaded in from the
    caller's real gate runs rather than re-run here, so the scorecard reports
    the same evidence the audit acted on instead of a second opinion.
    """
    files = _tracked_files(project_dir, run)
    lower = [f.lower() for f in files]
    gates: list[Gate] = []

    def add(**kw):
        gates.append(Gate(**kw))

    # --- Buildability ------------------------------------------------------ #
    real, why = verification_is_real(toolchains)
    add(id="build_verifiable", title="Changes can be build-verified",
        status="pass" if real else "fail", severity="critical", evidence=why,
        remediation="Install dependencies and ensure a build/check command exists; "
                    "without one no fix in this repo can be proven safe.")
    if build_ok is None:
        add(id="build_passes", title="Project builds", status="unknown",
            severity="critical", evidence="build was not run",
            remediation="Run the detected build command and fix the errors.")
    else:
        add(id="build_passes", title="Project builds",
            status="pass" if build_ok else "fail", severity="critical",
            evidence="build command exited 0" if build_ok else "build command failed",
            remediation="Fix the compile/build errors.", auto_fixable=True)

    # --- Tests ------------------------------------------------------------- #
    have_tests = _has_tests(files)
    add(id="tests_present", title="Automated tests exist",
        status="pass" if have_tests else "fail", severity="high",
        evidence="test files found" if have_tests else "no test files found",
        remediation="Add a unit test suite covering the primary entry points.",
        auto_fixable=True)
    if tests_ok is None:
        add(id="tests_pass", title="Test suite passes", status="unknown",
            severity="high",
            evidence="tests were not run" if have_tests else "no suite to run",
            remediation="Run the suite and fix failures.")
    else:
        add(id="tests_pass", title="Test suite passes",
            status="pass" if tests_ok else "fail", severity="high",
            evidence="suite exited 0" if tests_ok else "suite failed",
            remediation="Fix the failing tests.", auto_fixable=True)

    # --- Secrets ----------------------------------------------------------- #
    leaked = [f for f in files
              if _SECRET_FILE_PAT.search(f) and not _SECRET_FILE_SAFE.search(f)]
    add(id="no_committed_secrets", title="No secret material committed",
        status="fail" if leaked else "pass", severity="critical",
        evidence=("committed: " + ", ".join(leaked[:5])) if leaked
        else "no secret-shaped files tracked",
        remediation="Remove from the index, rotate the credentials, add to "
                    ".gitignore. Deleting the file alone does NOT purge history.")

    gitignore = _read_text(os.path.join(project_dir, ".gitignore"))
    ignores_env = ".env" in gitignore
    add(id="gitignore_protects", title=".gitignore covers secrets and artifacts",
        status="pass" if ignores_env else "fail", severity="high",
        evidence=".env ignored" if ignores_env else ".gitignore missing or does not ignore .env",
        remediation="Ignore .env, build output, and dependency directories.",
        auto_fixable=True)

    # --- Config externalisation -------------------------------------------- #
    has_env_example = any(re.search(r"(?:^|/)\.env\.(?:example|sample|template)$", f, re.I)
                          for f in files)
    uses_env = any(f.endswith((".js", ".ts", ".jsx", ".tsx", ".py", ".go",
                               ".rb", ".php", ".rs", ".java", ".cs"))
                   for f in files)
    add(id="config_documented", title="Required configuration is documented",
        status="pass" if has_env_example else ("fail" if uses_env else "na"),
        severity="medium",
        evidence=".env.example present" if has_env_example else "no .env.example",
        remediation="Commit a .env.example listing every required variable "
                    "with placeholder values.", auto_fixable=True)

    # --- Dependency pinning ------------------------------------------------ #
    unpinned = [t for t in toolchains
                if t.install and t.ecosystem not in ("make", "cpp", "swift")
                and _current_lockfile(project_dir, t) is None]
    add(id="deps_pinned", title="Dependencies are lock-pinned",
        status="fail" if unpinned else ("pass" if toolchains else "na"),
        severity="high",
        evidence=("no lockfile: " + ", ".join(f"{t.ecosystem}:{t.root}"
                                              for t in unpinned[:5]))
        if unpinned else "lockfiles present for all components",
        remediation=_pinning_remediation(unpinned),
        auto_fixable=True,
        paths=_pinning_edit_paths(project_dir, unpinned))

    # --- Operational readiness --------------------------------------------- #
    has_ci = any(any(p.lower() in f for p in _CI_PATHS) for f in lower)
    add(id="ci_configured", title="Continuous integration is configured",
        status="pass" if has_ci else "fail", severity="medium",
        evidence="CI config found" if has_ci else "no CI config",
        remediation="Add a pipeline that installs, builds, and tests on push.",
        auto_fixable=True)

    readme = next((f for f in files if f.lower().split("/")[-1].startswith("readme")), None)
    readme_text = _read_text(os.path.join(project_dir, readme)) if readme else ""
    runnable = bool(re.search(r"(npm|pnpm|yarn|pip|poetry|uv|go|cargo|docker|make|"
                              r"dotnet|bundle|composer|mix)\s+\w", readme_text, re.I))
    add(id="readme_runnable", title="README explains how to install and run",
        status="pass" if runnable else "fail", severity="medium",
        evidence="README contains setup commands" if runnable
        else ("README has no run instructions" if readme else "no README"),
        remediation="Document prerequisites, install, configure, run, and test.",
        auto_fixable=True)

    # A LICENCE FILE IS ONE OF TWO STANDARD DECLARATIONS, NOT THE ONLY ONE.
    #
    # This gate asks "is the licence declared?" and used to accept only a
    # LICENSE/COPYING file, so it reported "no license file" for every package
    # that declares its licence in the ecosystem's OWN manifest - which for a
    # PRIVATE, proprietary package is the only correct answer available. npm's
    # own convention for that case is `"license": "UNLICENSED"` in package.json;
    # there is no file to add, and adding an OSS one would be actively wrong
    # (measured on repo-rewards 2026-08-29: `private: true`, licence declared
    # UNLICENSED, gate still FAIL -> a defect that can never be closed, which is
    # how a rubric trains its reader to ignore it).
    #
    # A manifest declaration therefore counts. `license-file` (Cargo) still
    # points at a file and is covered by the file check above.
    has_license_file = any(f.lower().split("/")[-1].startswith(("license", "licence",
                                                                "copying"))
                           for f in files)
    declared_in = None
    if not has_license_file:
        declared_in = _license_declared_in_manifest(project_dir, files)
    has_license = has_license_file or bool(declared_in)
    add(id="license_present", title="License declared", status="pass" if has_license
        else "fail", severity="low",
        evidence=("license file present" if has_license_file else
                  f"licence declared in {declared_in}" if declared_in else
                  "no license file and no licence field in the package manifest"),
        remediation="Add a LICENSE file, or declare the licence in the package "
                    "manifest (npm's `\"license\": \"UNLICENSED\"` is the correct "
                    "declaration for a private, proprietary package).",
        auto_fixable=True,
        paths=([m] if not has_license and
               (m := next((f for f in files
                           if f.lower() in ("package.json", "pyproject.toml",
                                            "cargo.toml", "composer.json")),
                          None)) else []))

    # A service is something that serves traffic; a library legitimately has no
    # container or health endpoint, so this is "na" rather than a failure.
    is_service = any(t.is_web or t.framework in
                     ("express", "fastify", "@nestjs/core", "koa", "hapi",
                      "next", "nuxt", "remix")
                     for t in toolchains)
    has_container = any(f.lower().split("/")[-1] in
                        ("dockerfile", "containerfile", "procfile") or
                        f.lower().endswith("dockerfile") for f in files)
    add(id="deployable_artifact", title="Deployable artifact defined",
        status=("pass" if has_container else "fail") if is_service else "na",
        severity="medium",
        evidence="Dockerfile/Procfile present" if has_container
        else ("service with no container/Procfile" if is_service else "not a service"),
        remediation="Add a Dockerfile or Procfile that starts the service.",
        auto_fixable=True)

    # --- Structured data (SEO markup) --------------------------------------- #
    # "na" when the project ships no JSON-LD at all: most apps legitimately
    # don't, and absence of SEO markup is not a readiness defect. Severity low:
    # reported, never blocks a release - but Google silently ignores an invalid
    # block, so when JSON-LD IS present it must at least be machine-valid.
    jsonld_total, jsonld_problems = _validate_jsonld(project_dir, files)
    add(id="structured_data_valid", title="JSON-LD structured data is valid",
        status="na" if jsonld_total == 0
        else ("pass" if not jsonld_problems else "fail"),
        severity="low",
        evidence=(f"{jsonld_total} JSON-LD block(s), all parse with @context/@type"
                  if jsonld_total and not jsonld_problems
                  else ("; ".join(jsonld_problems[:5])
                        if jsonld_problems else "no JSON-LD blocks found")),
        remediation="Fix the application/ld+json blocks: must parse as JSON and "
                    "carry @context plus @type per node - Google silently "
                    "ignores invalid blocks, so they fail without any error.",
        auto_fixable=True)

    return gates


def is_blocking(gate: Gate, floor: str = BLOCKING_SEVERITY) -> bool:
    """A gate whose failure must prevent a production-ready claim.

    "unknown" counts as blocking at or above the floor: an unevaluated critical
    property is not evidence of safety, and treating it as a pass is precisely
    the overclaim this module exists to prevent.
    """
    if gate.status in ("pass", "na"):
        return False
    return _SEV_RANK.get(gate.severity, 0) >= _SEV_RANK.get(floor, 3)


def readiness_verdict(gates: list[Gate],
                      floor: str = BLOCKING_SEVERITY) -> tuple[bool, list[Gate]]:
    """(production_ready, blockers). Ready only when NO blocking gate is open."""
    blockers = [g for g in gates if is_blocking(g, floor)]
    return (not blockers), blockers


def readiness_score(gates: list[Gate]) -> tuple[int, int, int]:
    """(passed, evaluated, total). Evaluated excludes 'na' and 'unknown' so the
    ratio never flatters the project by counting checks that never ran."""
    total = len(gates)
    evaluated = [g for g in gates if g.status in ("pass", "fail")]
    passed = [g for g in evaluated if g.status == "pass"]
    return len(passed), len(evaluated), total


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
_STATUS_MARK = {"pass": "PASS", "fail": "FAIL", "na": "n/a", "unknown": "????"}


def render_scorecard(project_name: str, toolchains: list[Toolchain],
                     gates: list[Gate], bootstrap: list[StepResult] | None = None,
                     floor: str = BLOCKING_SEVERITY) -> str:
    """Markdown scorecard. Leads with the verdict and the blockers, because a
    reader who stops after the first section must not come away with a rosier
    picture than the evidence supports."""
    ready, blockers = readiness_verdict(gates, floor)
    passed, evaluated, total = readiness_score(gates)
    out: list[str] = [f"# Production readiness — {project_name}", ""]
    out.append(f"**Verdict: {'PRODUCTION READY' if ready else 'NOT PRODUCTION READY'}**")
    out.append("")
    out.append(f"- Gates passed: {passed}/{evaluated} evaluated ({total} total)")
    out.append(f"- Blocking failures: {len(blockers)} (severity >= {floor})")
    out.append("")

    if blockers:
        out.append("## Blockers")
        out.append("")
        for g in blockers:
            out.append(f"- **{g.title}** [{g.severity}] — {g.evidence or g.status}")
            if g.remediation:
                out.append(f"  - Fix: {g.remediation}")
        out.append("")

    out.append("## Detected toolchains")
    out.append("")
    if toolchains:
        out.append("| Component | Ecosystem | Manager | Build | Test | Deps |")
        out.append("|---|---|---|---|---|---|")
        for t in toolchains:
            out.append(
                f"| `{t.root}` | {t.ecosystem} | {t.manager} | "
                f"{'yes' if t.build else 'NONE'} | {'yes' if t.test else 'none'} | "
                f"{'installed' if t.deps_installed else 'MISSING'} |")
    else:
        out.append("_No build system detected._")
    out.append("")

    if bootstrap:
        out.append("## Bootstrap")
        out.append("")
        for s in bootstrap:
            out.append(f"- [{'ok' if s.ok else 'FAILED'}] `{' '.join(s.cmd)}` "
                       f"in `{s.cwd}`")
            if not s.ok and s.detail:
                out.append(f"  - {s.detail.strip().splitlines()[-1][:200]}"
                           if s.detail.strip() else "")
        out.append("")

    out.append("## All gates")
    out.append("")
    out.append("| Gate | Status | Severity | Evidence |")
    out.append("|---|---|---|---|")
    for g in sorted(gates, key=lambda x: (-_SEV_RANK.get(x.severity, 0), x.id)):
        out.append(f"| {g.title} | {_STATUS_MARK.get(g.status, g.status)} | "
                   f"{g.severity} | {g.evidence} |")
    out.append("")
    return "\n".join(out)
