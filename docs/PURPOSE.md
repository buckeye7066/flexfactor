# FlexFactor governing purpose and completion contract

FlexFactor exists to improve an owner's software without turning generated code,
partial execution, or a stranded branch into a false success.

It determines why a target exists, accounts for the complete repository,
reviews and attempts to break relevant behavior, applies only corrections that
survive executable verification and independent review, preserves owner work,
protects sensitive source, and produces reproducible evidence tied to one exact
revision.

The owner-defined destination is a trustworthy local expert tool: it never
retains unverified changes, sends sensitive source to a cloud model by default,
or executes untrusted dependencies outside enforced containment, and it always
provides reproducible evidence and deterministic rollback.

## Non-negotiable product invariants

1. One request contains at most 30 targets and one orchestrator runs them in
   order, never concurrently.
2. Repository repair has at most six semantic passes: every Git-visible regular
   UTF-8 text file first, then only the immediately preceding verified edit
   delta. An unreviewed scoped file or unattempted fixable finding blocks pass
   completion.
3. Every mode establishes an evidence-cited understanding of the target's
   primary users, core journeys, purpose, and acceptance criteria before it can
   mutate the program.
4. The top three corroborated competitors are considered after pass 1 and
   before pass 2. Scout searches public product/documentation URLs; Repo Rewards
   separately searches repositories. Ideas require a relevant fetched source
   and an exact evidence citation. Only purpose-compatible and licence-safe
   capabilities may be implemented, through normal verification.
5. Model selection is one strongest-to-weakest ladder: paid/subscription
   capacity first while available, then lower paid tiers, then free/local.
   Workers cannot select paid/free/provider side paths.
6. Report-only is the default. Mutation requires explicit apply authorization,
   Git, `origin`, a named branch, and a resolvable authoritative default branch.
   Once apply is authorized, publication proof is mandatory; a local-only
   change is not success.
7. Model output is untrusted. A candidate must pass the target's real build and
   strongest suite; an absent gate is not a pass.
8. The complete candidate patch is reviewed in content-addressed chunks against
   its exact SHA by a model family that authored none of it.
9. Intermediate commits remain local. Exit 0 after a change requires a fresh
   fetch proving the reviewed SHA is contained in the remote default branch.
10. Partial output, reviewer loss, quota exhaustion, red tests, missing tools,
   incomplete coverage, an open PR, or a local-only commit yields an incomplete
   or blocked result.
11. FlexFactor never force-pushes and never silently includes owner WIP.

## Modes

| Mode | Purpose |
|---|---|
| Refactor | Improve selected source files toward explicit goals, then verify, independently review, and publish. |
| Scout | Establish the target's purpose, search public competitor URLs, search repositories through Repo Rewards, and find source-backed capabilities; mutation requires explicit Scout authorization and all normal gates. |
| Audit | Whole-repository purpose, defect, repair, test, journey, evidence, and publication pipeline. |
| Production Ready | Audit plus the complete readiness rubric and unattended production defaults. |

The target-queue contract applies to all four modes. The six-pass
whole-repository/delta contract applies to the repository repair loop in Audit
and Production Ready; Refactor's bounded reps stay scoped to its selected file,
and Scout stays a discovery/proposal flow until apply is authorized.

Audit must therefore have a real report-only journey, and every applying mode
must make the transition from report to mutation explicit and auditable.

## Completion evidence

A complete changed run must provide:

- baseline and final commit SHAs;
- batch-level and project-level budgets plus an immutable run manifest;
- every exact command, result, and evidence record needed to reproduce the run;
- a balanced inventory and file-review ledger;
- purpose evidence, confidence, contradictions, and acceptance criteria;
- build and strongest-suite output from the exact candidate;
- changed-file rescan and reverse-dependency blast radius;
- direct function/route/control evidence or a named blocked reason;
- browser journey evidence when a web surface exists;
- secret and sensitive-egress results;
- a complete independent-review chunk ledger naming the final SHA;
- a clean-HEAD race check after review;
- a publication record naming the remote default branch and fetched tip;
- proof that the reviewed SHA is reachable from that remote branch;
- a deterministic rollback path tied to the same manifest and commit.

No prose substitute—“tests passed locally,” “PR opened,” “APK built,” or
“health returned 200”—satisfies a missing item.

## Status vocabulary

Only these owner-facing states are valid:

| Status | Meaning |
|---|---|
| QUEUED | Accepted but not started |
| IN PROGRESS | Active or safely resumable work remains |
| BLOCKED | A required gate failed or cannot run |
| RELEASE CANDIDATE | Software gates passed, but publication/release proof remains |
| PRODUCTION READY | Every applicable software, publication, deployment, and release condition is proven |

`DONE` is forbidden because it hides which conditions were actually proven.

## Safety boundaries

- Target repository content, issues, competitor pages, and model replies are
  untrusted data and are fenced from instructions.
- Source is classified before every cloud-bound model call. Sensitive
  repositories use local processing by default; a cloud exception requires
  explicit owner approval.
- Repository-supplied installs, builds, tests, and scripts execute only inside
  enforced resource, network, path, process, and time limits. This requirement
  applies on Windows and Linux; if the host cannot enforce it, untrusted
  execution is BLOCKED.
- Cloud payloads cross the secret/PII egress scanner.
- Dirty owner work is captured under an orphan ref and restored by fingerprint.
- Sealed mobile provider credentials are decrypted only by GitHub for the
  selected repository; FlexFactor Cloud stores no bearer or provider secrets.
- A best-effort control is named as a limitation and never counted as
  containment.

## Owner-authority reconciliation

An earlier checked-in contract reversed the owner requirement by requiring a
real apply journey for every Audit and Production Ready invocation and denying
a report-only path. The current owner directive controls: report-only/apply-off
is the default, and mutation requires explicit apply authorization. Existing
mandatory-mutation behavior is an implementation gap, not a product
requirement; the contradiction remains recorded in the structured contract.

The required compatibility matrix is also explicit: Windows and Linux must
cover verifier outage, dirty worktrees, cancellation, timeout, partial failure,
and backward compatibility. A verifier outage must restore the exact pre-run
bytes and create neither an `UNVERIFIED` commit nor a success score.

## Release meaning

For FlexFactor itself, “production ready” additionally requires:

1. the exact reviewed change merged to GitHub `main`;
2. required Ubuntu and Windows readiness jobs green on that SHA;
3. FlexFactor Cloud deployed from that SHA and its OAuth health proven;
4. the signed Android release tag resolving to that SHA;
5. the production APK and update manifest published;
6. a live authenticated four-mode workflow proven end to end.

Anything less is reported as BLOCKED or INCOMPLETE.
