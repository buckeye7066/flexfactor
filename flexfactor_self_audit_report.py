#!/usr/bin/env python3
"""
FlexFactor self-audit, report-only (owner order 2026-08-12): the ONE named
exception to "every run is real" apply-and-merge. When FlexFactor audits
ITSELF (not a program entered into it), findings go to the owner by email
instead of being auto-fixed/committed. Every other program stays on the
normal flexfactor.py CLI path unchanged -- this script never edits that
file; it drives the real audit engine (real review, real cost, real resume
checkpointing) via flexfactor.py's own Python API and substitutes only the
fix/commit steps with a capturing no-op, entirely inside this process.

Also exercises the resume mechanism at real scale: run with a low
--max-cost first so it interrupts mid-sweep, then rerun to prove it picks
up the checkpoint instead of re-reviewing from scratch.

Usage:
  python flexfactor_self_audit_report.py [--max-cost N]
"""
import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FF_PATH = os.path.join(HERE, "flexfactor.py")

# Hermetic load pattern from flexfactor_tests.py: register in sys.modules
# BEFORE exec_module, or dataclasses with future annotations die at import.
_spec = importlib.util.spec_from_file_location("flexfactor", FF_PATH)
ff = importlib.util.module_from_spec(_spec)
sys.modules["flexfactor"] = ff
_spec.loader.exec_module(ff)

captured_findings: dict[str, list[dict]] = {}
captured_stack: dict = {}


def _capturing_fix_files(author, cross, project_dir, file_findings, stack,
                          baseline_ok, args, meter=None, oversized=None,
                          report=None, err_base=0, done_set=None,
                          total_overall=0, commit_cb=None, commit_every=12,
                          adversarial=True, adversarial_rounds=2,
                          materiality="material"):
    """Stand-in for flexfactor._fix_files, scoped to THIS process only --
    the committed flexfactor.py is untouched. Captures findings, writes and
    fixes nothing, so self-audit produces a report but never edits itself."""
    for rel, findings in file_findings.items():
        captured_findings.setdefault(rel, [])
        for f in findings:
            if f not in captured_findings[rel]:
                captured_findings[rel].append(f)
    captured_stack.update(stack or {})
    return ([], [], ["self-audit: fix phase replaced by owner directive -- report-only via email"])


def _noop_commit_and_sync(project_dir, branch, prev_branch, args, label, stack):
    """Stand-in for flexfactor._commit_and_sync. Nothing was written by the
    fix stub above, so there is nothing to commit; this is a defense-in-depth
    no-op so a genuinely real invocation of the real function is never
    reachable during self-audit no matter what future code paths call it."""
    return "skipped-self-audit-report-only"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-cost", type=float, default=15.0)
    ns = parser.parse_args()

    ff._fix_files = _capturing_fix_files
    ff._commit_and_sync = _noop_commit_and_sync

    argv = [
        "audit", "--program", HERE,
        "--no-push", "--no-merge", "--yes", "--no-dashboard",
        "--max-cost", str(ns.max_cost),
    ]
    # FlexFactor's FREE-FIRST policy (owner order 2026-08-11) prefers local
    # ollama over any cloud provider whenever --provider isn't explicitly
    # named -- correct default for real programs, but on this CPU-only
    # machine local inference makes a genuine per-file review take 15-20+
    # minutes, impractical for a report a human is waiting on. Naming
    # --provider explicitly routes through ANTHROPIC_BASE_URL (the free FCC
    # proxy, set by the caller) instead, without touching that default policy
    # for any other program audited through the normal CLI.
    if os.environ.get("FF_SELF_AUDIT_PROVIDER"):
        argv += ["--provider", os.environ["FF_SELF_AUDIT_PROVIDER"]]
    print(f"Running real flexfactor audit engine (report-only self-audit): {argv}")
    rc = ff.main(argv)
    print(f"\nflexfactor.main() returned {rc}")
    print(f"Captured findings across {len(captured_findings)} file(s), "
          f"{sum(len(v) for v in captured_findings.values())} total.")

    import json
    # Written OUTSIDE the repo deliberately: audit_one_program refuses to run
    # against a dirty working tree (a real safety guard -- "Commit or stash
    # first"), so writing evidence inside the repo would make every SUBSEQUENT
    # self-audit run refuse to start because of this script's own prior output.
    out_dir = os.environ.get("FF_SELF_AUDIT_OUT_DIR") or os.path.join(
        os.path.expanduser("~"), ".flexfactor", "self-audit-reports"
    )
    out_path = os.path.join(out_dir, "self-audit-findings.json")
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "generated_by": "flexfactor_self_audit_report.py",
                "mode": "report-only (owner exception 2026-08-12; no fixes applied to flexfactor itself)",
                "flexfactor_main_rc": rc,
                "files_with_findings": len(captured_findings),
                "total_findings": sum(len(v) for v in captured_findings.values()),
                "findings_by_file": captured_findings,
            },
            fh,
            indent=2,
        )
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
