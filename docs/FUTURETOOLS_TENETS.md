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

For machine-readable ranking, the adapter gives Tenets an isolated temporary
`--output` file and reads that bounded JSON file after the CLI exits. This avoids
TTY/console-output differences across Windows and Linux while keeping the real
pinned Tenets CLI in the execution path.

The Tenets process also starts in that temporary directory rather than in the
audited checkout. Its home, configuration, cache, and data directories are
disposable. The child receives a minimal OS-variable allowlist rather than
provider keys, repository credentials, proxies, or unrelated application
secrets. Its inherited Python import overrides are removed, its `PATH` is empty,
Git ranking is disabled, and GitPython is pointed at a private nonexistent
executable. This prevents an untrusted checkout from shadowing the pinned
package, supplying a helper executable, or triggering executable local Git
configuration such as `core.fsmonitor`. The absolute project argument remains
read-only ranking input.

Writing outside the target repository is deliberate: generating context must
not make a clean repository dirty and trip FlexFactor's own dirty-tree gate.
Use `--output` to select another evidence path.

Desktop launches resolve `tenets.exe` beside the selected virtual-environment
Python before consulting the ambient `PATH`. This matches FlexFactor's
PowerShell launcher behavior, which invokes the virtual-environment interpreter
directly without activating it.

## Failure and resource contract

Tenets is an enhancement, not a release authority. If the executable is absent,
its installed-package metadata is unreadable, it times out, exits non-zero,
emits malformed JSON, exceeds either output safety limit, or returns no safe
paths, the adapter records `unavailable` or `degraded` evidence and preserves
FlexFactor's original file order and cap. If
uncapped candidate discovery cannot complete, the adapter records degraded
evidence and reruns the original capped enumerator unchanged.
`flexfactor-context --strict` returns non-zero in those cases for CI or operator
verification.

Stdout and stderr are consumed concurrently with hard limits while the process
is running. FlexFactor terminates the child as soon as either stream exceeds its
limit, preventing a noisy or defective subprocess from exhausting memory. The
ranker starts in an isolated POSIX session. On Windows it starts suspended, is
assigned to a kill-on-close Job Object, and only then resumes, so no helper can
escape in the launch-to-containment interval. FlexFactor closes the retained
process group or Job Object after timeout, overflow, or normal leader exit;
descendants therefore cannot survive by inheriting the output pipes. Timeouts
must be positive finite numbers. The temporary JSON output is also bounded
before parsing and is removed before the adapter returns.

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
bounded stdout and stderr, isolated process state and helper lookup,
Python-path and Git isolation, process-tree termination,
timeout/non-zero/malformed-output degradation, cache
behavior, idempotent runtime installation, the disable switch, parameter and
global cap lifting, cap restoration, and the invariant that prioritization
never enlarges a bounded review. Regression tests also prove that ranked files
beyond 100,000 candidates remain selectable, that failed uncapped discovery
falls back to the original capped order with degraded evidence, and that real
CLI file output is consumed even when console stdout contains status text. The
launcher tests also cover explicit argument forwarding, duplicate project
basenames, and routed multi-program session prompts so one program cannot
inherit another program's ranking objective.

The `tenets-context` GitHub Actions workflow runs those tests on Windows and
Linux. Separate live jobs on both operating systems install the exact pinned
package, deliberately remove the selected Python directory from `PATH`, prove
that the adapter still resolves the sibling Tenets executable, invoke the real
CLI against the checkout, and validate that every ranked path remains contained
by the repository.
