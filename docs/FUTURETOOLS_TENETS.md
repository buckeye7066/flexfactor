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
Tenets may only move its ranked paths earlier in that set. It cannot mark a file
clean, modify code, approve a change, or weaken any build, test, review,
evidence, or release gate.

For bounded audits, Tenets now ranks before the review limit is applied. The
adapter temporarily lifts a detected `max_files` parameter or canonical
`MAX_FILES_PER_RUN` global for one enumeration, reorders only the canonical
candidates returned by FlexFactor, restores the original limit, and returns
exactly that many files. This permits a relevant file beyond the old
alphabetical or walk-order cutoff to enter the budget without increasing the
budget or bypassing source-enumerator rules.

## Install

The regular full installation includes the pinned Tenets release:

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

Desktop launches resolve `tenets.exe` beside the selected virtual-environment
Python before consulting the ambient `PATH`. This matches FlexFactor's
PowerShell launcher behavior, which invokes the virtual-environment interpreter
directly without activating it.

## Failure and resource contract

Tenets is an enhancement, not a release authority. If the executable is absent,
times out, exits non-zero, emits malformed JSON, exceeds either output safety
limit, or returns no safe paths, the adapter records `unavailable` or `degraded`
evidence and preserves FlexFactor's original file order and cap. If
uncapped candidate discovery cannot complete, the adapter records degraded
evidence and reruns the original capped enumerator unchanged.
`flexfactor-context --strict` returns non-zero in those cases for CI or operator
verification.

Stdout and stderr are consumed concurrently with hard limits while the process
is running. FlexFactor terminates the child as soon as either stream exceeds its
limit, preventing a noisy or defective subprocess from exhausting memory.
Timeouts must be positive finite numbers.

Automatic launcher integration can be disabled without uninstalling the tool:

```text
FLEXFACTOR_TENETS=0
```

A custom objective can be supplied for launcher runs with
`FLEXFACTOR_TENETS_TASK`. Per-process results are cached by project and task so
repeated review passes do not rerun the ranker.

## Verification

`flexfactor_tenets_tests.py` is hermetic and covers path containment, duplicate
removal, virtual-environment executable discovery, finite timeout validation,
bounded stdout and stderr, timeout/non-zero/malformed-output degradation, cache
behavior, idempotent runtime installation, the disable switch, parameter and
global cap lifting, cap restoration, and the invariant that prioritization
never enlarges a bounded review. Regression tests also prove that ranked files
beyond 100,000 candidates remain selectable and that failed uncapped discovery
falls back to the original capped order with degraded evidence.

The `tenets-context` GitHub Actions workflow runs those tests on Windows and
Linux. Separate live jobs on both operating systems install the exact pinned
package, deliberately remove the selected Python directory from `PATH`, prove
that the adapter still resolves the sibling Tenets executable, invoke the real
CLI against the checkout, and validate that every ranked path remains contained
by the repository.
