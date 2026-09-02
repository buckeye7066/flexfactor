# FlexFactor governing purpose and completion contract

FlexFactor exists to improve an owner's software without turning generated code,
partial execution, or a stranded branch into a false success.

It determines why a target exists, accounts for the complete repository,
reviews and attempts to break relevant behavior, applies only corrections that
survive executable verification and independent review, preserves owner work,
protects sensitive source, and produces reproducible evidence tied to one exact
revision.

## Non-negotiable product invariants

1. One request contains at most 30 targets and one orchestrator runs them in
   order, never concurrently.
2. Repository repair has at most six semantic passes: the whole repository
   first, then only the immediately preceding verified edit delta.
3. The top three corroborated competitors are considered after pass 1 and
   before pass 2. Only purpose-compatible and licence-safe capabilities may be
   implemented, through normal verification.
4. Model selection is one strongest-to-weakest ladder: paid/subscription
   capacity first while available, then lower paid tiers, then free/local.
   Workers cannot select paid/free/provider side paths.
5. No production mutation starts without Git, `origin`, a named branch, a
   resolvable authoritative default branch, and mandatory push/merge.
6. Model output is untrusted. A candidate must pass the target's real build and
   strongest suite; an absent gate is not a pass.
7. The complete candidate patch is reviewed in content-addressed chunks against
   its exact SHA by a model family that authored none of it.
8. Intermediate commits remain local. Exit 0 after a change requires a fresh
   fetch proving the reviewed SHA is contained in the remote default branch.
9. Partial output, reviewer loss, quota exhaustion, red tests, missing tools,
   incomplete coverage, an open PR, or a local-only commit yields an incomplete
   or blocked result.
10. FlexFactor never force-pushes and never silently includes owner WIP.

## Modes

| Mode | Purpose |
|---|---|
| Refactor | Improve selected source files toward explicit goals, then verify, independently review, and publish. |
| Scout | Find useful competitive/open-source capabilities; mutation requires explicit Scout authorization and all normal gates. |
| Audit | Whole-repository purpose, defect, repair, test, journey, evidence, and publication pipeline. |
| Production Ready | Audit plus the complete readiness rubric and unattended production defaults. |

The target-queue contract applies to all four modes. The six-pass
whole-repository/delta contract applies to the repository repair loop in Audit
and Production Ready; Refactor's bounded reps stay scoped to its selected file,
and Scout stays a discovery/proposal flow until apply is authorized.

## Completion evidence

A complete changed run must provide:

- baseline and final commit SHAs;
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
- proof that the reviewed SHA is reachable from that remote branch.

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
- Install/build/test execution crosses the command and containment broker.
- Cloud payloads cross the secret/PII egress scanner.
- Dirty owner work is captured under an orphan ref and restored by fingerprint.
- Sealed mobile provider credentials are decrypted only by GitHub for the
  selected repository; FlexFactor Cloud stores no bearer or provider secrets.
- When the host cannot enforce a property, the evidence names the limitation
  instead of claiming containment.

## Release meaning

For FlexFactor itself, “production ready” additionally requires:

1. the exact reviewed change merged to GitHub `main`;
2. required Ubuntu and Windows readiness jobs green on that SHA;
3. FlexFactor Cloud deployed from that SHA and its OAuth health proven;
4. the signed Android release tag resolving to that SHA;
5. the production APK and update manifest published;
6. a live authenticated four-mode workflow proven end to end.

Anything less is reported as BLOCKED or INCOMPLETE.
