# Scout a Program — Production Readiness (CORRECTED)

**Status:** BLOCKED BY VERIFIED TECHNICAL FAILURE → fixing  
**Updated:** 2026-08-09  
**Main at reopen:** `bcddf2efa0ba4b32a1c4262d92da8e2809d4697c`  
**Branch:** `cursor/production-ready/flexfactor-scout-fix`

## Why reopened

Prior PRODUCTION READY was invalid. Unresolved PR #12 review threads (all still present on main):

1. Silent local→remote Repo Rewards fallback (trust boundary / privacy) — **MEDIUM security**
2. HTTPS reachability probed port 80 instead of 443
3. Auto-start rewrote explicit `--repo-rewards-url` to env DEFAULT
4. Launcher ignored `FLEXFACTOR_REPO_REWARDS_PRODUCTION_URL`
5. Launcher probed `/api/health` (not RR contract) then silently chose remote
6. Ollama default had no daemon/model prerequisite check

## Fixes in this wave (implementing)

- Remote RR requires `--allow-remote-repo-rewards` or `FLEXFACTOR_ALLOW_REMOTE_REPO_REWARDS=1`
- `_host_port` scheme defaults (https→443); `_server_is_up` prefers `/api/version`
- Auto-start keeps requested local URL
- Launcher: `/api/version` probe, production env override, Ollama preflight, no silent remote

## Still required before any Production Ready claim

- Fresh **independent** functional + security + release reviews (not Cursor implementer sequential docs)
- CI green on exact final main SHA
- Purpose journey re-proven with local-first default (no silent remote)
