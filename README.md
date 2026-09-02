# FlexFactor

FlexFactor is a managed, purpose-driven code improvement system with four
interfaces: Refactor, Scout, Audit, and Production Ready. Version 0.6.1 uses one
quality-first model ladder, one durable orchestrator, and one fail-closed
publication contract on desktop and in the signed Android app.

## The product contract

- A queue contains from 1 through 30 files or repositories.
- The orchestrator starts exactly one queued target at a time. A crash resumes
  the durable queue; it never admits the next target while one is active.
- Audit and Production Ready perform at most six semantic passes. Pass 1 covers
  the complete repository. Every later pass covers exactly the files whose
  verified bytes changed in the preceding pass.
- Between passes 1 and 2, FlexFactor researches the top three corroborated
  competitors and attempts their strongest purpose-compatible, licence-safe
  capabilities. Those edits pass the same build and independent-review gates as
  every other edit.
- There are no paid and free routes. Every call starts at the strongest
  available paid or subscription model, descends through lower paid capacity
  only when credit/quota is unavailable, and reaches free/local capacity last.
- A writing mode refuses to begin model work without Git, an `origin`, a named
  branch, and mandatory push-and-merge publication.
- Generated code is never trusted because it was generated. It must pass the
  target's real build and strongest project suite, complete changed-file
  rescanning and evidence gates, and an exact-commit review by a model family
  that authored none of the candidate.
- Exit 0 requires proof that the exact reviewed commit is reachable from the
  authoritative remote default branch. A local commit, an open PR, a red or
  missing test gate, reviewer loss, partial model output, or exhausted budget is
  incomplete—not success.

## Modes

| Mode | Selection | Behavior |
|---|---|---|
| Refactor | Up to 30 repository-relative files | Rewrite and grade each file toward its stated goal, then verify, independently review, and land the exact commit. |
| Scout | Up to 30 repositories | Profile the product and research improvements. Mutation requires explicit Scout apply authorization and then uses the same verification/publication contract. |
| Audit | Up to 30 repositories | Whole-repository purpose, defect, test, journey, evidence, competitor, repair, and publication pipeline. |
| Production Ready | Up to 30 repositories | Audit with the full readiness rubric, medium-severity repair, unattended defaults, and no relaxed completion claim. |

All selected targets run sequentially. Legacy `--parallel`, provider,
`--economy`, and `--model-mode free|paid` arguments may parse for saved-command
compatibility, but they cannot create another execution policy.

## Install and verify

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[all]"
.venv/bin/python flexfactor_tests.py
```

On Windows, use `.venv\Scripts\python.exe`. To install desktop shortcuts:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_desktop_shortcuts.ps1
```

Examples:

```bash
python flexfactor.py refactor --file src/app.py --goal "Make failures explicit"
python flexfactor.py scout --program /path/to/repo
python flexfactor.py audit --program /repo/one --program /repo/two --yes
python flexfactor.py prodready --program /path/to/repo --yes
python flexfactor.py --runtime-manifest
```

The cost cap bounds paid use; it does not select a lower-quality path. If a
stronger account has no remaining capacity, the same call continues down the
single ladder. The managed runner includes independent Qwen and DeepSeek local
code-model families so the free fallback can still separate author and reviewer.

## Managed Android product

Android 3.5.1 is a native phone interface, not a Termux or desktop remote-control
screen. The user taps the icon, signs in with GitHub device authorization, picks
one of the four modes, and queues up to 30 targets. The queue is committed
synchronously to private app storage and dispatch uses a persistent UUID, so
a phone crash after GitHub accepted a request recovers the existing run rather
than dispatching a duplicate.

The managed control plane:

1. validates the complete request and OAuth scope;
2. resolves the exact target ref before any workflow or secret mutation;
3. installs a caller pinned to the matching FlexFactor release;
4. seals optional provider credentials to the selected repository;
5. atomically claims the request UUID before dispatching ephemeral compute;
6. correlates and returns the authoritative run and bounded evidence artifact;
7. removes phone-supplied repository secrets and the claim at terminal status.

An existing owner-managed provider secret is never replaced by a phone-supplied
credential. A terminal cleanup failure blocks queue advancement and retries;
it is not reported as a completed target.

GitHub Actions is the compute substrate, not the user-facing product. The cloud
service never stores GitHub sessions or plaintext provider keys. The legacy
Termux launch endpoints are retired; the web dashboard is viewer/steering only.
See [android/README.md](android/README.md) and
[cloud/THREAT_MODEL.md](cloud/THREAT_MODEL.md).

Version 3.5.1 normalizes whole-file Markdown responses at their matching outer
closing fence, preserves nested examples, rejects unclosed wrappers as partial,
and discards later provider prose. It treats an independently verified
unchanged refactor as a real no-op only after proving its baseline is already
on the authoritative remote default branch.

## Evidence and recovery

Durable state lives outside target repositories under `~/.flexfactor/`:

- `queues/`: ordered queue receipts and pass transitions;
- `runs/`: resumable per-repository checkpoints;
- `evidence/`: indexes, changed-file rescans, coverage, quality gates, SARIF,
  independent-review ledger, and publication proof;
- `events/`: redacted event streams;
- `status.json`: dashboard state.

Recovery reuses a review only when its file SHA and policy match. Interrupted
work is never converted to clean. Owner work under `--allow-dirty` is captured
on an orphan ref, excluded from FlexFactor commits, and restored only after a
fingerprint check.

## Security boundaries

- Repository text and model output are untrusted data. Prompt fences, contained
  no-follow reads/writes, exact edit anchors, rollback, and executable gates
  protect the target.
- Cloud egress is scanned for high-confidence secrets and PII. The safe default
  is refusal; `--redact` masks matched spans.
- Target install/build/test commands cross the execution broker. On a host
  without enforceable OS containment, an untrusted repository is refused unless
  the owner explicitly trusts it.
- Windows network isolation remains best-effort proxy poisoning; the runtime
  reports that limitation instead of claiming full containment.
- FlexFactor never force-pushes. Protected default branches use a normal PR and
  remain incomplete until GitHub reports it merged and the reviewed SHA is
  proven on the default branch.

## Release verification

`.github/workflows/production-readiness.yml` is the binding desktop/cloud
matrix gate on Ubuntu and Windows. `.github/workflows/android-client.yml`
builds, tests, lints, signs, and publishes an APK only from the exact merged
`main` commit and verifies that the release tag resolves to that same SHA.

For failure diagnosis, see [docs/troubleshooting.md](docs/troubleshooting.md).
