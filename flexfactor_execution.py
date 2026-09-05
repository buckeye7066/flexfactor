"""Shared execution contract for every FlexFactor mode.

The owner-facing shape is deliberately small and fixed:

* one queue containing at most thirty targets;
* exactly one target may execute at a time;
* a repository receives no more than six semantic passes;
* audit/prodready pass one covers every reviewable repository file;
* refactor pass one covers its explicitly selected file and Scout pass one
  records repository understanding (neither may claim a whole-repo audit);
* later passes cover only files changed by the immediately preceding pass; and
* the three best corroborated competitors are considered between passes one
  and two.

This module is dependency-free so the CLI, workflows, and contract tests can
all import the same policy without constructing a provider or touching a repo.
"""

from __future__ import annotations

from collections.abc import Iterable
import json
import os
import tempfile
import threading
import time
import uuid
from typing import Callable


MAX_TARGETS = 30
MAX_PASSES = 6

# RAISED 3 -> 25 on 2026-09-04 by owner order: "program scout's code currently
# limits how much of the 'copycat' code he can produce on the new branch in his
# repo which sets him up for failure. He needs to be able to fully reproduce the
# code."
#
# This one constant was BOTH the research target (how many competitors get
# studied) and the fix-stream cap (how many of their ideas may become code), so
# Scout could reproduce at most THREE things per run no matter how much the
# research found. flexfactor_competitors.competitor_findings deliberately
# documented that as "a bounded number of changes and never a rewrite spree" —
# a design opinion the owner has now overridden for this tool.
#
# Tunable so a run can still be narrowed without editing source. 0 remains an
# explicit OFF for the fix stream (see competitor_findings); it is not a
# silent no-op.
TOP_COMPETITORS = max(0, int(os.environ.get("FLEXFACTOR_TOP_COMPETITORS", "25") or 25))
MODEL_POLICY = "best-available"

FIRST_PASS_SCOPE = {
    "audit": "whole-repository",
    "prodready": "whole-repository",
    "refactor": "selected-file",
    "scout": "repository-understanding",
}


class ExecutionContractError(ValueError):
    """The requested queue or pass count violates the product contract."""


class OrchestrationOrderError(RuntimeError):
    """A worker tried to advance outside the orchestrator-owned order."""


def target_queue(values: Iterable[object] | None) -> list[str]:
    """Return a validated, ordered target queue.

    Empty entries are rejected rather than silently discarded: a queue whose
    displayed length differs from the work actually attempted is misleading.
    Duplicate targets remain distinct because the caller may intentionally
    apply different mode-specific options to the same path in a future typed
    request surface.
    """

    targets = [str(value).strip() for value in (values or [])]
    if not targets:
        raise ExecutionContractError("choose at least one target")
    if any(not value for value in targets):
        raise ExecutionContractError("queue targets cannot be blank")
    if len(targets) > MAX_TARGETS:
        raise ExecutionContractError(
            f"choose no more than {MAX_TARGETS} targets (received {len(targets)})"
        )
    return targets


def pass_count(value: object) -> int:
    """Validate the hard semantic-pass ceiling without silently clamping."""

    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError("pass count must be an integer from 1 through 6") from exc
    if count < 1 or count > MAX_PASSES:
        raise ExecutionContractError(
            f"pass count must be from 1 through {MAX_PASSES}"
        )
    return count


def changed_file_scope(changed_files: Iterable[object] | None) -> list[str]:
    """Build a stable follow-up scope from verified edits only.

    Paths are normalized to repository separators and deduplicated in first-
    seen order. Unreviewed or merely attempted files are intentionally absent:
    an incomplete pass blocks completion but does not widen the next pass.
    """

    out: list[str] = []
    seen: set[str] = set()
    for raw in changed_files or []:
        path = str(raw).strip().replace("\\", "/")
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _default_state_path(queue_id: str) -> str:
    root = os.environ.get("FLEXFACTOR_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".flexfactor"
    )
    return os.path.join(root, "queues", f"{queue_id}.json")


class SequentialOrchestrator:
    """Durable authority for one mode's target queue and pass transitions.

    Workers do not choose their successor. They ask this object to start and
    finish a target/pass; an overlapping target, skipped pass, widened delta,
    or missing competitor gate raises before work continues. Every transition
    is atomically persisted as an owner-readable execution receipt.
    """

    # Schema 2 makes exact final-tree reconciliation part of the durable
    # contract.  Schema-1 success receipts cannot be resumed as though they had
    # satisfied an invariant that did not exist when they were written.
    SCHEMA = 2

    def __init__(self, mode: str, targets: Iterable[object], *,
                 state_path: str | None = None, queue_id: str | None = None):
        clean_mode = str(mode or "").strip().lower()
        if not clean_mode:
            raise ExecutionContractError("queue mode is required")
        clean_targets = target_queue(targets)
        explicit_id = queue_id or os.environ.get("FLEXFACTOR_QUEUE_ID")
        requested_id = str(explicit_id or uuid.uuid4())
        if not all(ch.isalnum() or ch in "-_" for ch in requested_id):
            raise ExecutionContractError("queue identifier contains unsafe characters")
        self.mode = clean_mode
        self.targets = clean_targets
        self.queue_id = requested_id
        self.state_path = state_path or _default_state_path(requested_id)
        self._lock = threading.RLock()
        if os.path.isfile(self.state_path):
            self._state = self._load_existing(explicit_id)
            self.queue_id = str(self._state["queue_id"])
            self._recover_interrupted_target()
            self._save()
            return
        self._state = {
            "schema": self.SCHEMA,
            "queue_id": self.queue_id,
            "mode": self.mode,
            "policy": MODEL_POLICY,
            "target_limit": MAX_TARGETS,
            "pass_limit": MAX_PASSES,
            "competitor_target": TOP_COMPETITORS,
            "status": "queued",
            "next_index": 0,
            "active_index": None,
            "created_at": time.time(),
            "updated_at": time.time(),
            "items": [
                {"index": index, "target": target, "status": "queued", "passes": []}
                for index, target in enumerate(self.targets)
            ],
        }
        self._save()

    def _load_existing(self, explicit_id: object | None) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as stream:
                state = json.load(stream)
        except (OSError, ValueError, TypeError) as exc:
            raise ExecutionContractError(
                f"saved queue receipt is unreadable: {self.state_path}"
            ) from exc
        if not isinstance(state, dict) or state.get("schema") != self.SCHEMA:
            raise ExecutionContractError("saved queue receipt has an unsupported schema")
        saved_targets = [str(row.get("target", ""))
                         for row in (state.get("items") or [])
                         if isinstance(row, dict)]
        checks = {
            "mode": state.get("mode") == self.mode,
            "targets": saved_targets == self.targets,
            "policy": state.get("policy") == MODEL_POLICY,
            "target limit": state.get("target_limit") == MAX_TARGETS,
            "pass limit": state.get("pass_limit") == MAX_PASSES,
            "competitor target": state.get("competitor_target") == TOP_COMPETITORS,
            "queue identifier": (explicit_id is None
                                 or str(state.get("queue_id")) == str(explicit_id)),
        }
        failed = [name for name, okay in checks.items() if not okay]
        if failed:
            raise ExecutionContractError(
                "saved queue receipt does not match this request: " + ", ".join(failed)
            )
        next_index = state.get("next_index")
        active_index = state.get("active_index")
        if (not isinstance(next_index, int) or not 0 <= next_index <= len(self.targets)
                or (active_index is not None
                    and (not isinstance(active_index, int)
                         or active_index != next_index
                         or not 0 <= active_index < len(self.targets)))):
            raise ExecutionContractError("saved queue receipt has invalid queue pointers")
        return state

    def _recover_interrupted_target(self) -> None:
        """Make a crash-resumable target queued while preserving its attempt."""
        index = self._state.get("active_index")
        if index is None:
            return
        item = self._state["items"][index]
        prior = {
            "status": "interrupted",
            "started_at": item.get("started_at"),
            "recovered_at": time.time(),
            "passes": item.get("passes") or [],
        }
        if item.get("competitor_gate") is not None:
            prior["competitor_gate"] = item.get("competitor_gate")
        if item.get("finalization") is not None:
            prior["finalization"] = item.get("finalization")
        item.setdefault("attempts", []).append(prior)
        for field in ("started_at", "finished_at", "exit_code", "note",
                      "competitor_gate", "finalization"):
            item.pop(field, None)
        item["passes"] = []
        item["status"] = "queued"
        self._state["active_index"] = None
        self._state["status"] = "queued"
        self._state["resumed_at"] = time.time()

    def _save(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(parent, exist_ok=True)
        self._state["updated_at"] = time.time()
        handle, temp_path = tempfile.mkstemp(prefix="queue-", suffix=".json", dir=parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._state, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.state_path)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    @property
    def next_index(self) -> int:
        with self._lock:
            return int(self._state["next_index"])

    def start_target(self, index: int) -> None:
        with self._lock:
            if self._state["active_index"] is not None:
                raise OrchestrationOrderError("another target is already active")
            if index != self._state["next_index"] or not 0 <= index < len(self.targets):
                raise OrchestrationOrderError(
                    f"target {index + 1} cannot start; target "
                    f"{self._state['next_index'] + 1} is next"
                )
            item = self._state["items"][index]
            item["status"] = "running"
            item["started_at"] = time.time()
            self._state["active_index"] = index
            self._state["status"] = "running"
            self._save()

    def note_active_target(self, note: object) -> None:
        """Attach a bounded worker outcome to the durable target receipt."""
        with self._lock:
            index = self._state["active_index"]
            if index is None:
                raise OrchestrationOrderError("no target is active")
            self._state["items"][index]["worker_note"] = str(note or "")[:1000]
            self._save()

    def begin_pass(self, number: int, scope: Iterable[object], *,
                   whole_repository: bool = False,
                   scope_kind: str | None = None,
                   exhaustive: bool | None = None) -> list[str]:
        with self._lock:
            index = self._state["active_index"]
            if index is None:
                raise OrchestrationOrderError("no target is active")
            count = pass_count(number)
            item = self._state["items"][index]
            expected = len(item["passes"]) + 1
            if count != expected:
                raise OrchestrationOrderError(
                    f"pass {count} cannot start; pass {expected} is next"
                )
            expected_first_scope = FIRST_PASS_SCOPE.get(self.mode, "whole-repository")
            if count == 1 and expected_first_scope == "whole-repository" \
                    and not whole_repository:
                raise OrchestrationOrderError(
                    "audit/prodready pass 1 must cover the whole repository")
            if count > 1 and whole_repository:
                raise OrchestrationOrderError("follow-up passes cannot widen to the whole repository")
            if count == 2 and item.get("competitor_gate", {}).get("attempted") is not True:
                raise OrchestrationOrderError(
                    "pass 2 cannot start before the top-three competitor gate"
                )
            paths = changed_file_scope(scope)
            if count > 1:
                expected_paths = list(item["passes"][-1].get("changed_files") or [])
                if count == 2:
                    expected_paths = changed_file_scope(
                        expected_paths
                        + list(item.get("competitor_gate", {}).get(
                            "implemented_files") or [])
                    )
                if paths != expected_paths:
                    raise OrchestrationOrderError(
                        f"pass {count} scope must exactly equal the previous "
                        f"verified edit delta (expected {expected_paths!r}, got {paths!r})"
                    )
            declared_scope = (str(scope_kind or "").strip()
                              or ("whole-repository" if whole_repository
                                  else "previous-pass-edits"))
            if count == 1 and declared_scope != expected_first_scope:
                raise OrchestrationOrderError(
                    f"{self.mode} pass 1 scope must be {expected_first_scope!r}, "
                    f"got {declared_scope!r}"
                )
            is_exhaustive = (bool(exhaustive) if exhaustive is not None else
                             bool(count == 1 and whole_repository
                                  and self.mode in {"audit", "prodready"}))
            record = {
                "number": count,
                "scope": declared_scope,
                "exhaustive": is_exhaustive,
                "files": paths,
                "status": "running",
                "started_at": time.time(),
            }
            item["passes"].append(record)
            self._save()
            return paths

    def finish_pass(self, number: int, changed_files: Iterable[object], *,
                    reviewed_files: Iterable[object] | None = None,
                    incomplete_files: Iterable[object] | None = None,
                    repair_candidate_files: Iterable[object] | None = None,
                    repair_attempted_files: Iterable[object] | None = None) -> list[str]:
        with self._lock:
            index = self._state["active_index"]
            if index is None:
                raise OrchestrationOrderError("no target is active")
            item = self._state["items"][index]
            if not item["passes"] or item["passes"][-1]["number"] != pass_count(number):
                raise OrchestrationOrderError("the requested pass is not active")
            record = item["passes"][-1]
            if record["status"] != "running":
                raise OrchestrationOrderError("the requested pass already finished")
            reviewed = changed_file_scope(reviewed_files)
            incomplete = changed_file_scope(incomplete_files)
            candidates = changed_file_scope(repair_candidate_files)
            attempted = changed_file_scope(repair_attempted_files)
            if record.get("exhaustive"):
                required = list(record.get("files") or [])
                missing = [path for path in required if path not in set(reviewed)]
                unattempted = [path for path in candidates
                               if path not in set(attempted)]
                failures = []
                if missing:
                    failures.append(
                        f"{len(missing)} scoped file(s) lack a completed review")
                if incomplete:
                    failures.append(
                        f"{len(incomplete)} scoped file(s) are incomplete")
                if unattempted:
                    failures.append(
                        f"{len(unattempted)} repair candidate(s) were not attempted")
                if failures:
                    record["reconciliation"] = {
                        "reviewed_files": reviewed,
                        "incomplete_files": incomplete,
                        "repair_candidate_files": candidates,
                        "repair_attempted_files": attempted,
                        "missing_review_files": missing,
                        "unattempted_repair_files": unattempted,
                    }
                    self._save()
                    raise OrchestrationOrderError(
                        "exhaustive pass cannot complete: " + "; ".join(failures))
            changed = changed_file_scope(changed_files)
            record["reconciliation"] = {
                "reviewed_files": reviewed,
                "incomplete_files": incomplete,
                "repair_candidate_files": candidates,
                "repair_attempted_files": attempted,
                "missing_review_files": [],
                "unattempted_repair_files": [],
            }
            record["changed_files"] = changed
            record["status"] = "completed"
            record["finished_at"] = time.time()
            self._save()
            return changed

    def reconcile_first_pass_scope(self, scope: Iterable[object]) -> list[str]:
        """Replace pass one's manifest after governed pre-sweep mutations.

        Audit and production-readiness may repair a red baseline or bridge an
        authored purpose gap before the generic semantic sweep.  Those writes
        still belong to pass one.  The repository manifest can therefore grow
        between ``begin_pass`` and the sweep, but only while the exhaustive
        first pass is active.  Recording the replacement here keeps the durable
        receipt authoritative instead of letting pre-sweep work happen outside
        it.
        """
        with self._lock:
            index = self._state["active_index"]
            if index is None:
                raise OrchestrationOrderError("no target is active")
            item = self._state["items"][index]
            if len(item["passes"]) != 1:
                raise OrchestrationOrderError(
                    "only the active first pass may reconcile its scope"
                )
            record = item["passes"][0]
            if (record.get("number") != 1 or record.get("status") != "running"
                    or record.get("exhaustive") is not True
                    or record.get("scope") != "whole-repository"):
                raise OrchestrationOrderError(
                    "only a running exhaustive whole-repository pass may "
                    "reconcile its scope"
                )
            previous = list(record.get("files") or [])
            paths = changed_file_scope(scope)
            record["files"] = paths
            record["scope_reconciled_at"] = time.time()
            previous_set = set(previous)
            paths_set = set(paths)
            record.setdefault("scope_revisions", []).append({
                "previous_files": previous,
                "files": paths,
                "added_files": [path for path in paths if path not in previous_set],
                "removed_files": [path for path in previous if path not in paths_set],
                "recorded_at": time.time(),
            })
            self._save()
            return paths

    def record_finalization(self, *, changed_files: Iterable[object],
                            final_commit: object = None,
                            quality_gates_passed: bool,
                            publication_required: bool,
                            publication_complete: bool,
                            note: str = "") -> None:
        """Reconcile the pass ledger with the exact final repository state.

        Some deterministic completion work (currently readiness remediation)
        can make a verified edit after the semantic pass loop.  It may not be
        invisible to the durable receipt.  This record separates those files
        from pass-owned edits and binds them to the final evidence and
        publication gates that reviewed the resulting tree.
        """
        with self._lock:
            index = self._state["active_index"]
            if index is None:
                raise OrchestrationOrderError("no target is active")
            item = self._state["items"][index]
            passes = list(item.get("passes") or [])
            if not passes:
                raise OrchestrationOrderError(
                    "finalization requires a recorded repository pass"
                )
            if "finalization" in item:
                raise OrchestrationOrderError("target finalization already recorded")
            final_files = changed_file_scope(changed_files)
            pass_files = changed_file_scope(
                path
                for row in passes
                for path in (row.get("changed_files") or [])
            )
            gate_files = changed_file_scope(
                (item.get("competitor_gate") or {}).get("implemented_files") or []
            )
            governed = set(pass_files + gate_files)
            post_pass = [path for path in final_files if path not in governed]
            gates_ok = bool(quality_gates_passed)
            publication_ok = bool(publication_complete)
            passes_complete = all(
                row.get("status") == "completed" for row in passes)
            item["finalization"] = {
                "changed_files": final_files,
                "pass_changed_files": pass_files,
                "competitor_changed_files": gate_files,
                "post_pass_changed_files": post_pass,
                "final_commit": str(final_commit or ""),
                "quality_gates_passed": gates_ok,
                "passes_complete": passes_complete,
                "publication_required": bool(publication_required),
                "publication_complete": publication_ok,
                "status": ("completed" if passes_complete and gates_ok
                           and publication_ok else "failed"),
                "note": str(note or "")[:1000],
                "finished_at": time.time(),
            }
            self._save()

    def record_competitor_gate(self, *, attempted: bool, implemented_files: Iterable[object],
                               verified: int = 0, note: str = "",
                               not_applicable: bool = False) -> None:
        with self._lock:
            index = self._state["active_index"]
            if index is None:
                raise OrchestrationOrderError("no target is active")
            item = self._state["items"][index]
            passes = item["passes"]
            if len(passes) != 1 or passes[0]["status"] != "completed":
                raise OrchestrationOrderError(
                    "the competitor gate belongs after pass 1 and before pass 2"
                )
            if "competitor_gate" in item:
                raise OrchestrationOrderError("the competitor gate already ran")
            implemented = changed_file_scope(implemented_files)
            verified_count = max(0, int(verified))
            no_delta = not changed_file_scope(
                passes[0].get("changed_files") or []
            )
            if not_applicable and (
                    attempted or implemented or verified_count or not no_delta):
                raise OrchestrationOrderError(
                    "the competitor gate is not applicable only when pass 1 "
                    "retained no verified edit delta"
                )
            item["competitor_gate"] = {
                "attempted": bool(attempted),
                "not_applicable": bool(not_applicable),
                "target": TOP_COMPETITORS,
                "verified": verified_count,
                "implemented_files": implemented,
                "note": str(note or "")[:1000],
                "finished_at": time.time(),
            }
            self._save()

    def finish_target(self, index: int, exit_code: int, *, note: str = "") -> int:
        with self._lock:
            if self._state["active_index"] != index:
                raise OrchestrationOrderError("only the active target can finish")
            item = self._state["items"][index]
            note = str(note or item.pop("worker_note", ""))
            code = int(exit_code)
            if item["passes"] and item["passes"][-1]["status"] == "running":
                item["passes"][-1]["status"] = "interrupted"
                item["passes"][-1]["finished_at"] = time.time()
                if code == 0:
                    code = 1
                    note = (str(note or "") + "; " if note else "") + (
                        "orchestrator refused success while a pass was still active"
                    )
            if code == 0:
                failures: list[str] = []
                passes = list(item.get("passes") or [])
                if not passes:
                    failures.append("no repository pass was recorded")
                else:
                    expected_scope = FIRST_PASS_SCOPE.get(
                        self.mode, "whole-repository")
                    if (passes[0].get("number") != 1
                            or passes[0].get("scope") != expected_scope):
                        failures.append(
                            f"pass 1 did not use required {expected_scope!r} scope")
                    if (self.mode in {"audit", "prodready"}
                            and passes[0].get("exhaustive") is not True):
                        failures.append(
                            "whole-repository pass 1 was not marked exhaustive")
                if any(row.get("status") != "completed" for row in passes):
                    failures.append("not every recorded pass completed")
                gate = item.get("competitor_gate")
                gate_valid = isinstance(gate, dict) and gate.get("attempted") is True
                if isinstance(gate, dict) and gate.get("not_applicable") is True:
                    pass_one_delta = changed_file_scope(
                        passes[0].get("changed_files") or []
                    ) if passes else []
                    gate_valid = bool(
                        not pass_one_delta
                        and not changed_file_scope(
                            gate.get("implemented_files") or []
                        )
                        and int(gate.get("verified") or 0) == 0
                    )
                if not gate_valid:
                    failures.append(
                        "the top-three competitor gate was neither attempted "
                        "nor truthfully marked not applicable"
                    )
                if passes and len(passes) < MAX_PASSES:
                    pending = changed_file_scope(passes[-1].get("changed_files") or [])
                    if len(passes) == 1 and isinstance(gate, dict):
                        pending = changed_file_scope(
                            pending + list(gate.get("implemented_files") or [])
                        )
                    if pending:
                        failures.append(
                            "verified edits were not followed by the required exact-delta pass"
                        )
                if self.mode in {"audit", "prodready"}:
                    finalization = item.get("finalization")
                    if not isinstance(finalization, dict):
                        failures.append("exact final-tree reconciliation was not recorded")
                    elif finalization.get("status") != "completed":
                        failures.append(
                            "final quality or publication reconciliation did not complete"
                        )
                if failures:
                    code = 1
                    refusal = "orchestrator refused success: " + "; ".join(failures)
                    note = (str(note or "") + "; " if note else "") + refusal
            item["exit_code"] = code
            item["status"] = "completed" if code == 0 else "failed"
            item["note"] = str(note or "")[:1000]
            item["finished_at"] = time.time()
            self._state["active_index"] = None
            self._state["next_index"] = index + 1
            if self._state["next_index"] >= len(self.targets):
                failed = any(row["status"] != "completed" for row in self._state["items"])
                self._state["status"] = "failed" if failed else "completed"
            else:
                self._state["status"] = "queued"
            self._save()
            return code


def run_sequential_queue(mode: str, targets: Iterable[object],
                         runner: Callable[[str, int, int, SequentialOrchestrator], int],
                         *, state_path: str | None = None,
                         queue_id: str | None = None,
                         orchestrator: SequentialOrchestrator | None = None
                         ) -> tuple[int, SequentialOrchestrator]:
    """Run every target in order, continuing after a target-level failure."""

    requested_targets = target_queue(targets)
    if orchestrator is None:
        orchestrator = SequentialOrchestrator(
            mode, requested_targets, state_path=state_path, queue_id=queue_id
        )
    elif orchestrator.mode != str(mode or "").strip().lower() \
            or orchestrator.targets != requested_targets:
        raise ExecutionContractError(
            "provided orchestrator does not match the requested queue")
    snapshot = orchestrator.snapshot()
    results: list[int] = [int(row.get("exit_code") or 0)
                          for row in snapshot["items"][:orchestrator.next_index]]
    total = len(orchestrator.targets)
    for index in range(orchestrator.next_index, total):
        target = orchestrator.targets[index]
        orchestrator.start_target(index)
        try:
            code = int(runner(target, index + 1, total, orchestrator))
        except (KeyboardInterrupt, SystemExit):
            orchestrator.finish_target(index, 130, note="operator interruption")
            raise
        except Exception as exc:
            code = orchestrator.finish_target(
                index, 1, note=f"{type(exc).__name__}: {exc}"
            )
            results.append(code)
            continue
        code = orchestrator.finish_target(index, code)
        results.append(code)
    return (next((code for code in results if code != 0), 0), orchestrator)


__all__ = [
    "ExecutionContractError",
    "MAX_PASSES",
    "MAX_TARGETS",
    "MODEL_POLICY",
    "OrchestrationOrderError",
    "SequentialOrchestrator",
    "TOP_COMPETITORS",
    "changed_file_scope",
    "pass_count",
    "run_sequential_queue",
    "target_queue",
]
