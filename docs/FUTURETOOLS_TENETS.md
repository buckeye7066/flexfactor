# FutureTools integration: Tenets

FlexFactor integrates [Tenets](https://github.com/jddunn/tenets) as a local,
optional code-context ranker. Tenets was selected from the FutureTools catalog
because it can identify the files most relevant to a concrete repair objective
without sending repository content to a hosted service or requiring another API
account.

## What the integration changes

Launcher-driven audit and production-readiness runs install an idempotent hook
around FlexFactor's existing source enumerator. The original enumerator still
owns containment, skip rules, clean-file memory, and the complete set of files.
Tenets may only move its ranked paths earlier in that set. It cannot omit a file,
mark a file clean, modify code, approve a change, or weaken any build, test,
review, evidence, or release gate.

This matters when an audit has a bounded review budget: authentication, launch,
persistence, security, user-journey, and test files relevant to the current task
are examined earlier while the full source sweep remains intact.

## Install

The regular full installation now includes the pinned Tenets release:

```text
py -3.12 -m pip install -e ".[all]"
```

For only the context feature:

```text
py -3.12 -m pip install -e ".[context]"
```

The integration is pinned to `tenets==0.13.3`. It uses the CLI-only core; no
model download, Tenets cloud account, or API key is required.

## Direct use

```text
flexfactor-context C:\path\to\project "repair login, persistence, and release blockers" --strict
```

The command runs `tenets rank ... --format json`, validates every returned path
against the real project root, removes duplicates and out-of-repository paths,
and writes a deterministic JSON manifest under:

```text
~/.flexfactor/context/<project>-<path-hash>/tenets-context.json
```

Writing outside the target repository is deliberate: generating context must
not make a clean repository dirty and trip FlexFactor's own dirty-tree gate.
Use `--output` to select another evidence path.

## Failure contract

Tenets is an enhancement, not a release authority. If the executable is absent,
times out, exits non-zero, emits malformed JSON, or returns no safe paths, the
adapter records `unavailable` or `degraded` evidence and preserves FlexFactor's
original file order. `flexfactor-context --strict` returns non-zero in those
cases for CI or operator verification.

Automatic launcher integration can be disabled without uninstalling the tool:

```text
FLEXFACTOR_TENETS=0
```

A custom objective can be supplied for launcher runs with
`FLEXFACTOR_TENETS_TASK`. Per-process results are cached by project and task so
repeated review passes do not rerun the ranker.

## Verification

`flexfactor_tenets_tests.py` is hermetic and covers path containment, duplicate
removal, timeout/non-zero/malformed-output degradation, cache behavior,
idempotent runtime installation, the disable switch, and the invariant that
prioritization never drops or duplicates source files.

The `tenets-context` GitHub Actions workflow runs those tests on Windows and
Linux, installs the exact pinned package on Linux, invokes the real Tenets CLI
against the checkout, and validates that the resulting manifest contains only
paths contained by the repository.
