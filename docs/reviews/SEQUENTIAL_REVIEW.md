# FlexFactor sequential review (Cursor executor)

**Date:** 2026-08-09  
**Reviewed SHA (merge):** `bd00de667e608e625e6c59be709e63078cf624ff`  
**PR:** https://github.com/buckeye7066/flexfactor/pull/10  

## Scope

Containment capability detection (Linux CI), CI workflow, purpose contract, Windows/Linux suite green on exact PR head, live report-only + apply journeys against disposable fixtures.

## Product

- Purpose matches Appendix A / `docs/purpose-contract.md`: fail-closed auditor/refactorer with containment and rollback.
- Report-only default preserved; mutation requires explicit apply/refactor path.
- No false Production Ready claim for Scout (separate queue item).

## Architecture / implementation

- `_HAS_DIR_FD` correctly keys off `os.stat` (not `os.lstat`) — CPython &lt;3.13 omits lstat from `supports_dir_fd` (gh-134993).
- When `os.replace` lacks dir_fd, rename via `/proc/self/fd/{parent}` keeps O_NOFOLLOW parent handle.
- Verifier-outage path restores tree and skips success commit (unit + CI).

## Security / privacy

- Path/symlink/junction containment remain chokepoints for read/write/unlink.
- Egress gate + cmdpolicy + `--ignore-scripts` unchanged.
- Residual: OS-level network/job-object sandbox for installs/builds is **not** implemented; documented as known limitation (path/cmd containment is).

## QA

| Gate | Result |
|------|--------|
| Local `python flexfactor_tests.py` | 363 OK, 7 skipped |
| CI windows-tests @ PR head | success |
| CI linux-containment @ PR head | success (`POSIX_NOFOLLOW True`) |
| Post-merge CI on `main` | success (run 31309692247) |
| Verifier-outage unit tests | pass |
| Launcher parse | OK |
| Dashboard `--selftest` | OK |

## Accessibility / UX

CLI help lists modes clearly; report-only outcome labeled; apply journey prints backup path and insertion prompt.

## Release

- PR #10 merged to `main` @ `bd00de667e608e625e6c59be709e63078cf624ff`
- No open production-required PRs for FlexFactor core after evidence packet merge

## Findings

| Sev | Finding | Disposition |
|-----|---------|-------------|
| P2 | OS network/job-object sandbox deferred | Accepted residual; does not waive path containment or fail-closed verifier requirements |
| P3 | Windows `_same_id` empty-dir fallback can conflate siblings | Documented residual; parent-swap fail-closed still enforced when identities differ |

**P0/P1 unresolved:** none  

**Review decision:** APPROVE merge identity for production use as FlexFactor core (Scout remains a separate program).
