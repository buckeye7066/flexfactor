# FlexFactor engine threat model

## Assets

- owner source, history, uncommitted work, credentials, and provider budget;
- integrity of the target's authoritative default branch;
- exact-commit review, test, evidence, and publication receipts;
- host safety while target-controlled install/build/test commands execute.

## Trust boundaries and controls

### Generated or repository-controlled text

Source, documentation, issues, competitor pages, patches, and model output are
untrusted data. Prompt blocks are fenced, filesystem access uses contained
no-follow helpers, edit anchors must match exactly, and generated changes are
rolled back unless executable gates pass. A complete but wrong model answer
remains possible; adversarial and independent review plus real tests are the
defense.

### Partial or deceptive model output

Structured salvage is stamped partial. Partial findings may preserve evidence
but cannot authorize CLEAN, KEEP, APPROVE, READY, commit publication, or merge.
The exact-final review uses a content-addressed chunk ledger; any missing,
blocked, partial, commit-mismatched, or rejected chunk blocks approval.

### Same-author self-review

Every ladder instance in one run shares a `RoleCoordinator`. It records all
families that successfully authored candidate content. Reviewer selection
strictly excludes the complete set and fails if no independent family remains.

### Malicious target execution

All target-controlled install/build/test/server commands cross the command
policy and containment broker. Credentials are scrubbed. Lifecycle scripts are
off unless explicitly allowed. Linux uses the strongest available
namespace/`bwrap`/rlimit mechanism. Windows enforces process-tree/resource
limits but only best-effort network proxy poisoning; the runtime names that
residual risk. An untrusted repository on an unenforceable host is refused
unless the owner explicitly trusts it.

### Owner WIP

Allowed dirty work is captured in an orphan commit under
`refs/flexfactor-wip/*`, scanned for secrets, excluded from candidate ancestry,
and restored only after a porcelain fingerprint match. Unknown separation or a
secret finding refuses publication. Ignored files remain in place and are not
captured.

### Secret egress

Provider payloads cross a high-confidence secret/PII scanner. Default action is
refusal; redaction is explicit. Target processes receive a scrubbed
environment. Ollama URLs are loopback-only. The managed cloud never receives
plaintext provider keys.

### Wrong-commit or stranded publication

Writing modes preflight Git, `origin`, branch identity, remote-default
resolution, and mandatory publication before model construction. Intermediate
commits remain local. After executable and independent review gates, HEAD is
checked again, the publication suite reruns, and the commit is pushed directly
or through a normal PR. FlexFactor never force-pushes. Completion requires a
fresh fetch and ancestry proof for the reviewed SHA on the authoritative remote
default branch.

### Queue replay and duplicate mobile dispatch

Desktop and Android queues admit one target at a time and persist transitions.
The Android request UUID is an idempotency key. FlexFactor Cloud scans GitHub's
paginated workflow history before mutation; if it cannot prove absence inside
its abuse bound, it refuses rather than risk a duplicate dispatch.

## Residual risks

- No model review can prove semantic correctness without adequate executable
  project tests and behavior evidence; a missing gate blocks completion.
- Windows does not provide OS-enforced network isolation in this release.
- Explicitly trusted repositories can execute their own declared build/test
  commands with the authority granted by the host.
- GitHub branch rules or required human approval can keep a PR open; that state
  remains incomplete and is never reported as published.
