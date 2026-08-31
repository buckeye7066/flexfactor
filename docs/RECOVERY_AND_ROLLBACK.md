# FlexFactor - Recovery and rollback (0.5.0 working tree)

## 1. Orphan WIP transaction (audit / prodready with `--allow-dirty`)

Implemented in `flexfactor_wip.py`, wired in `audit_one_program`:

1. **capture** - `porcelain_fingerprint` (pre-run), then
   `capture_orphan_wip_snapshot`: `git add -A` -> `write-tree` -> `commit-tree
   <tree> -m <DIRTY_SNAPSHOT_MSG>` (NO parent) -> `update-ref
   refs/flexfactor-wip/<sha12>` -> `scan_tree_for_secrets` -> `reset --hard
   HEAD` -> unlink exactly the captured untracked paths (never `git clean`).
   Ignored files are not captured and stay on disk. Failure before the ref
   exists leaves the tree dirty and refuses the run; failure after leaves the
   ref and still refuses ("could not snapshot the dirty working tree").
2. **run** - every FlexFactor commit is tool-only; `_wip_publish_guard` refuses
   push/merge unless `publish_allowed` proves the snapshot is not an ancestor of
   HEAD and has no secret findings.
3. **restore** (`_restore_wip_if_active`, in the `finally`): refused if the tree
   is still dirty from FlexFactor; else `restore_orphan_wip_snapshot` (re-apply
   WIP deletions via `diff --diff-filter=D HEAD <sha>`, `checkout <sha> -- .`,
   fallback `read-tree -u --reset`, then `reset` to unstage).
4. **fingerprint** - `porcelain_fingerprint` after restore compared to step 1.
5. **drop-or-retain** - match -> `update-ref -d`; mismatch/failure/exception
   -> ref RETAINED and `wip_restore` says so (stderr WARNING + manifest).

Tests: `test_flexfactor_wip.py` 19 OK (fixed 5 bugs: deletions not restored,
`git mv`, unicode paths unscanned, reset failure reported as success,
fingerprint on quoted paths).

## 2. Manual recovery

```
git show-ref | findstr flexfactor-wip        # list retained snapshots
git show --stat refs/flexfactor-wip/<id>     # what it holds
git checkout <sha> -- .                      # re-apply as uncommitted changes
git reset                                    # unstage
git update-ref -d refs/flexfactor-wip/<id>   # drop when satisfied
```
Never `git push --mirror` / `--all` while a ref is retained.

## 3. Crash / verifier outage / cancel paths (as implemented)

| Event | Behaviour |
|---|---|
| Verifier (adversarial reviewer) outage during a fix | fail-closed: candidate rolled back to the pre-change bytes, rejected, never kept as `[unverified]`; if rollback is REFUSED -> `DirtyTreeError` -> cycle aborts without committing (`[dirty-abort]`); CI pins `test_c_transport_failure_rolls_back_fail_closed` and `test_verifier_outage_skips_success_commit` |
| Provider route fault (three zero-completion batches) | `infrastructure_abort`, STOP with the reviewed/candidate ratio; tree reset (`reset --hard` + `clean -fd`) ONLY when not `--allow-dirty`; checkpoint stays resumable |
| Budget cap / manual-review leftovers / caught exception | `checkpoint.finish(status="interrupted")`, resumable; exit code non-zero where applicable |
| Ctrl-C / process death | `finish()` never runs -> checkpoint stays `running` -> resumable once the PID is gone; death instrumentation writes the obituary; WIP `finally` runs on KeyboardInterrupt paths inside `audit_one_program` |
| Final gate red | local commit kept, push/merge refused (`PUSH REFUSED - ...`), the next green cycle pushes the accumulated commits |
| Bootstrap side effects | `_discard_bootstrap_side_effects` before each commit |

## 4. Checkpoint / resume (`flexfactor_runstate.py`, as implemented)

- One file per run: `~/.flexfactor/runs/<run_id>/checkpoint.json`, atomic
  writes, throttled `save()` (force at phase boundaries), per-file
  `record_reviewed(rel, sha, findings)` immediately after every completed
  review. (A fixed file's stale review entry is dropped ON RESUME by
  `verify_reviewed`'s sha re-check - the `record_file_outcome` writer this
  line used to credit was never called from anywhere and was deleted
  2026-08-30.)
- Resume (`_resume_recover`): `latest_resumable` (status in LIVE_STATUSES, PID
  not alive, something recorded) -> `verify_reviewed(data, hasher, POLICY_VERSION)`
  re-hashes EVERY `reviewed` entry with the contained reader; policy mismatch
  drops all; changed/unreadable/missing-hash entries are re-reviewed; clean
  entries join the skip set; findings are replayed without re-paying.
- Scope limits: only `reviewed` entries are hash-verified; `files` outcomes
  and `bootstrap.done` are carried as-is; a checkpoint under another policy is
  never continued (fresh run). `--recheck` ignores checkpoints.
- `prune` keeps `DEFAULT_KEEP_RUNS`.

## 5. Scout apply rollback (separate mechanism)

`apply_integration`: per-file byte backups keyed by repo-relative path, new
files tracked in `created`, restore/unlink on any failure (build, npm, policy);
no sandbox branch; dirty tree -> `skipped-dirty` unless `--allow-dirty`. Not
the orphan-WIP transaction (BLOCKED item).
