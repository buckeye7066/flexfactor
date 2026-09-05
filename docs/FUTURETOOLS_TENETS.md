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

For machine-readable ranking, the adapter drains Tenets' stdout concurrently,
enforces an 8 MiB ceiling, and parses that bounded JSON only after the CLI
exits. It never gives the optional process a separately writable result file.
The temporary directory is solely its isolated working directory and home.

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
`FLEXFACTOR_STATE_DIR` and an explicit `--output` are rejected when their
resolved evidence path is inside the audited repository, another selected
repository, or any Git worktree. This includes the default
`~/.flexfactor/context` location when the user's home directory is itself a
Git worktree: set `FLEXFACTOR_STATE_DIR` to a directory outside every worktree
in that environment. The integrated ranker otherwise records the refusal and
continues with FlexFactor's canonical non-Tenets ordering; the standalone
`flexfactor-context` command returns its validation error without writing a
manifest.

Linux additionally requires `FLEXFACTOR_TENETS_CGROUP_ROOT` to name a real,
owner-delegated cgroup-v2 directory with `memory`, `pids`, and `cgroup.kill`
controls. Each ranking gets a fresh child cgroup capped at 1 GiB and 64 tasks.
If that aggregate job boundary is unavailable, ranking degrades without
launching Tenets and the canonical FlexFactor sweep continues unchanged.
Windows uses the equivalent aggregate memory/task limits on its Job Object.

This is a resource and process-lifecycle boundary for the exact pinned,
installation-owned Tenets package; Linux cgroup delegation is not a privilege
sandbox against malicious code already running as the same operating-system
user. FlexFactor never executes code from the audited repository, strips
ambient executable/import lookup, and refuses an unowned or wrong-version
ranker. Environments that treat the ranker itself as hostile must additionally
run FlexFactor behind a dedicated OS identity or privileged sandbox broker.

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
limit, preventing a noisy or defective subprocess from exhausting memory. On
Linux, a dedicated process starts as the isolated session leader and enables
the kernel child-subreaper contract before it launches Tenets. It retains that
identity after the direct ranker exits, adopts orphan helpers even when they
called `setsid()`, and terminates every adoptee before closing its output pipes.
On Windows, the ranker starts suspended, is assigned to a kill-on-close Job
Object, and only then resumes, so no helper can escape in the
launch-to-containment interval. The Linux supervisor enters a fresh delegated
cgroup-v2 boundary before it launches Tenets; that boundary applies aggregate
memory and task limits and supplies atomic whole-tree kill. A parent-death
signal plus an independent supervisor deadline cover abrupt FlexFactor exit.
The Windows Job Object applies equivalent aggregate memory and active-process
limits. Other POSIX systems do not provide either
boundary through Python's supported process API; FlexFactor therefore records
Tenets as degraded and does not launch it there instead of claiming an
escapable process group as containment. Timeouts must be positive finite
numbers. JSON is read only from the concurrently drained, size-bounded stdout
pipe; no separately writable result file can fill temporary storage while the
ranker runs. Temporary isolation cleanup or evidence persistence failures mark
the result degraded.

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
falls back to the original capped order with degraded evidence, and that CLI
JSON is consumed through bounded stdout without creating an unbounded result
file. Rankings are cached only for an unchanged reviewable repository
fingerprint, so a mutation cannot reuse stale priorities. The launcher tests
also cover explicit argument and environment forwarding, duplicate project
basenames, and routed multi-program session prompts so one program cannot
inherit another program's ranking objective.

The `tenets-context` GitHub Actions workflow runs those tests on Windows and
Linux. Separate live jobs on both operating systems install the exact pinned
package, deliberately remove the selected Python directory from `PATH`, prove
that the adapter still resolves the sibling Tenets executable, invoke the real
CLI against the checkout, and validate that every ranked path remains contained
by the repository.
