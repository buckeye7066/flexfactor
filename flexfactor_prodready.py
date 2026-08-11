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
    raw = _read_text(os.path.join(project_dir, rel_path), limit=8 * 1024 * 1024)
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
        install=[["dotnet", "restore"]],
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
    if missing:
        names = ", ".join(f"{t.ecosystem}:{t.root}" for t in missing)
        return False, f"dependencies not installed for {names} - build gate would false-fail"
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
}


def _current_lockfile(project_dir: str, tc: Toolchain) -> str | None:
    """Look for the lockfile ON DISK right now, rather than trusting what
    detection saw. Bootstrap runs between the two and routinely CREATES it
    (`npm install` writes package-lock.json), so the detect-time value reports a
    project as unpinned moments after the tool pinned it."""
    if tc.lockfile and _exists(project_dir, tc.root, tc.lockfile):
        return tc.lockfile
    root = os.path.join(project_dir, tc.root)
    for name in _LOCKFILES.get(tc.manager, ()):
        if _exists(root, name):
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
    return None


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
        remediation="Commit the lockfile so builds are reproducible.",
        auto_fixable=True)

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

    has_license = any(f.lower().split("/")[-1].startswith(("license", "licence",
                                                           "copying"))
                      for f in files)
    add(id="license_present", title="License declared", status="pass" if has_license
        else "fail", severity="low",
        evidence="license file present" if has_license else "no license file",
        remediation="Add a LICENSE file.", auto_fixable=True)

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
