"""Durable supervision contract: process-exit, exact-head, descendant reconcile.

This module does not loosen ``tools/kanban_tools._enforce_worker_task_ownership``.
Cross-task close is only possible through an explicitly issued, one-shot
descendant grant that is consumed exactly once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ALLOWED_RECONCILE_TRANSITIONS = frozenset({"complete", "fail"})
PROCESS_EXIT_STATUSES = frozenset({
    "exited", "stopped", "cli_return", "timeout", "timed_out", "killed", "",
})
SUCCESS_MARKERS = frozenset({"pass", "success", "ok", "done", "completed"})
FAILURE_MARKERS = frozenset({"fail", "failed", "error", "failure", "timeout", "timed_out"})
SUCCESS_RUN_STATUSES = frozenset({"done", "completed"})
FAILURE_RUN_STATUSES = frozenset({"failed", "error", "crashed", "killed"})
BLOCKING_VERDICTS = frozenset({"fail", "insufficient_evidence"})
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REVIEW_CAP = 5
REVIEW_CAP_CHOICES = ("Add 5 reviews", "Stop here", "Uncap until pass")
CLASS_SUCCESS = "success"
CLASS_FAILURE = "failure"
CLASS_MALFORMED = "malformed"
CLASS_NO_OUTPUT = "no_output"
NONTERMINAL_EXIT_CLASSES = frozenset({CLASS_FAILURE, CLASS_MALFORMED, CLASS_NO_OUTPUT})


def _now() -> int:
    return int(time.time())


def _full_git_sha(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip().lower()
    if _FULL_GIT_SHA_RE.fullmatch(text):
        return text
    return None


def _latest_ended_run(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, outcome, summary, status FROM task_runs WHERE task_id = ? "
        "AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _run_is_successful_proven(run: Optional[sqlite3.Row]) -> bool:
    if run is None:
        return False
    outcome = str(run["outcome"] or "").strip().lower()
    status = str(run["status"] or "").strip().lower()
    if outcome in FAILURE_MARKERS or status in FAILURE_RUN_STATUSES:
        return False
    return outcome in SUCCESS_MARKERS or status in SUCCESS_RUN_STATUSES


def _canonical_proof_is_current(proof: dict[str, Any], run: Optional[sqlite3.Row]) -> bool:
    """True when unit proof is still bound to the latest successful/proven run."""
    if not proof:
        return False
    if run is None:
        return True
    if not _run_is_successful_proven(run):
        return False
    proof_run = proof.get("run_id")
    if proof_run is None:
        return True
    try:
        return int(proof_run) == int(run["id"])
    except (TypeError, ValueError):
        return False


def ensure_contract_tables(conn: sqlite3.Connection) -> None:
    from hermes_cli.kanban_supervisor import ensure_supervisor_tables

    ensure_supervisor_tables(conn)


def scoped_caller_task_id() -> Optional[str]:
    return os.environ.get("HERMES_KANBAN_TASK") or None


def canonical_evidence_hash(packet: Any) -> str:
    payload = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_terminal_result(result: Any) -> str:
    """Classify a structured subprocess result. Exit alone is never success."""
    if result is None or result == "" or result == {} or result == []:
        return CLASS_NO_OUTPUT
    data = result
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return CLASS_NO_OUTPUT
        try:
            data = json.loads(text)
        except Exception:
            return CLASS_MALFORMED
    if not isinstance(data, dict):
        return CLASS_MALFORMED
    status = str(
        data.get("status") or data.get("verdict") or data.get("jude-verdict") or ""
    ).strip().lower()
    ok = data.get("ok")
    if ok is False or status in FAILURE_MARKERS:
        return CLASS_FAILURE
    if ok is True or status in SUCCESS_MARKERS:
        if data.get("blockers"):
            return CLASS_FAILURE
        return CLASS_SUCCESS
    if data.get("error"):
        return CLASS_FAILURE
    if not data:
        return CLASS_NO_OUTPUT
    return CLASS_MALFORMED


def process_exit_status(raw_status: Optional[str]) -> bool:
    return str(raw_status or "").strip().lower() in PROCESS_EXIT_STATUSES


def _parents_of(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id ASC",
        (task_id,),
    ).fetchall()
    return [r["parent_id"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]


def is_graph_descendant(conn: sqlite3.Connection, ancestor_id: str, descendant_id: str) -> bool:
    """True when ``ancestor_id`` is reachable via any parent walk (DAG-safe)."""
    if not ancestor_id or not descendant_id or ancestor_id == descendant_id:
        return False
    seen: set[str] = set()
    stack = [descendant_id]
    while stack:
        current = stack.pop()
        if not current or current in seen:
            continue
        seen.add(current)
        parents = _parents_of(conn, current)
        if ancestor_id in parents:
            return True
        stack.extend(parents)
    return False


def task_in_objective(conn: sqlite3.Connection, objective_id: str, task_id: str) -> bool:
    from hermes_cli.kanban_supervisor import get_objective

    obj = get_objective(conn, objective_id)
    if obj is None or not task_id:
        return False
    root = str(obj.get("root_task_id") or "")
    if task_id == root:
        return True
    unit = conn.execute(
        "SELECT 1 FROM kanban_objective_units WHERE objective_id = ? AND ref = ? LIMIT 1",
        (objective_id, task_id),
    ).fetchone()
    if unit is not None:
        return True
    return bool(root) and is_graph_descendant(conn, root, task_id)


def supervisor_owns_objective(
    conn: sqlite3.Connection,
    supervisor_task_id: str,
    objective_id: str,
) -> bool:
    """True when ``objective_id`` is rooted at the supervisor or an ancestor.

    Graph membership is the ownership relation. A sibling root that shares a
    descendant does not own that sibling's objective, even when the shared
    child carries proof for the other objective.
    """
    from hermes_cli.kanban_supervisor import get_objective

    oid = str(objective_id or "").strip()
    supervisor = str(supervisor_task_id or "").strip()
    if not oid or not supervisor:
        return False
    obj = get_objective(conn, oid)
    if obj is None:
        return False
    root = str(obj.get("root_task_id") or "").strip()
    if not root:
        return False
    if supervisor == root:
        return True
    return is_graph_descendant(conn, root, supervisor)


def _foreign_supervisor_objective_error(
    supervisor_task_id: str, objective_id: str
) -> str:
    return (
        f"objective {objective_id} is not the owning objective of "
        f"supervisor {supervisor_task_id}"
    )


def task_has_live_claim(conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None) -> bool:
    now = int(now if now is not None else _now())
    row = conn.execute(
        "SELECT status, claim_lock, claim_expires FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    lock = row["claim_lock"]
    expires = row["claim_expires"]
    if lock and (expires is None or int(expires) > now):
        return True
    run = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND status = 'running' "
        "AND ended_at IS NULL AND (claim_expires IS NULL OR claim_expires > ?) LIMIT 1",
        (task_id, now),
    ).fetchone()
    return run is not None


def build_canonical_evidence(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    objective_id: str,
    supervisor_task_id: Optional[str] = None,
) -> dict[str, Any]:
    proof: dict[str, Any] = {}
    predicate = None
    oid = str(objective_id or "").strip()
    unit = None
    owned = True
    if supervisor_task_id is not None:
        owned = supervisor_owns_objective(conn, supervisor_task_id, oid)
    if oid and owned:
        unit = conn.execute(
            "SELECT proof, terminal_predicate FROM kanban_objective_units "
            "WHERE objective_id = ? AND kind = 'kanban' AND ref = ? "
            "ORDER BY last_progress_at DESC LIMIT 1",
            (oid, task_id),
        ).fetchone()
    if unit is not None:
        predicate = unit["terminal_predicate"]
        if unit["proof"]:
            try:
                loaded = json.loads(unit["proof"])
                if isinstance(loaded, dict):
                    proof = loaded
            except Exception:
                proof = {}
    run = _latest_ended_run(conn, task_id)
    if not _canonical_proof_is_current(proof, run):
        proof = {}
    return {
        "task_id": task_id,
        "objective_id": oid,
        "run_id": int(run["id"]) if run else None,
        "run_outcome": (run["outcome"] if run else None),
        "head": proof.get("head"),
        "verdict": proof.get("verdict"),
        "predicate": predicate,
        "proof": proof,
    }


def persisted_proof_present(packet: dict[str, Any]) -> bool:
    proof = packet.get("proof") or {}
    if not (proof.get("verdict") or proof.get("head") or proof.get("type")):
        return False
    run_id = packet.get("run_id")
    if run_id is None:
        return True
    outcome = str(packet.get("run_outcome") or "").strip().lower()
    if outcome in FAILURE_MARKERS or (outcome and outcome not in SUCCESS_MARKERS):
        return False
    proof_run = proof.get("run_id")
    if proof_run is None:
        return True
    try:
        return int(proof_run) == int(run_id)
    except (TypeError, ValueError):
        return False


def _grant_head_error(
    conn: sqlite3.Connection, task_id: str, packet: dict[str, Any]
) -> Optional[str]:
    proof = packet.get("proof") or {}
    submitted = packet.get("head") or proof.get("head")
    submitted_sha = _full_git_sha(submitted)
    if not submitted_sha:
        return f"{task_id} proof HEAD is not a full 40-character SHA"
    from hermes_cli.kanban_supervisor import _task_git_head

    live_sha = _full_git_sha(_task_git_head(conn, task_id))
    if not live_sha:
        return f"{task_id} live Git HEAD could not be established"
    if live_sha != submitted_sha:
        return f"{task_id} proof HEAD does not match live Git HEAD"
    return None


def process_exit_evidence(proof: dict[str, Any]) -> dict[str, Any]:
    return {
        "terminal": proof.get("terminal"),
        "classification": proof.get("classification"),
        "result": proof.get("result"),
        "child_status": proof.get("child_status"),
        "session_id": proof.get("session_id"),
        "ref": proof.get("ref"),
    }


def _caller_is_supervisor(supervisor_task_id: str, caller_task_id: Optional[str]) -> Optional[str]:
    env_tid = scoped_caller_task_id()
    if env_tid and env_tid != supervisor_task_id:
        return (
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"foreign supervisor {supervisor_task_id}"
        )
    if not caller_task_id or caller_task_id != supervisor_task_id:
        return (
            f"caller {caller_task_id or '-'} is not the bound supervisor "
            f"{supervisor_task_id}"
        )
    return None


def _parse_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def issue_descendant_grant(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    supervisor_task_id: str,
    descendant_task_id: str,
    transition: str,
    evidence_hash: str,
    caller_task_id: Optional[str],
) -> dict[str, Any]:
    """Audited issuance. Does not mutate the descendant. Consumed later once."""
    from hermes_cli.kanban_supervisor import _new_id, _record_supervisor_event, get_objective

    ensure_contract_tables(conn)
    transition = str(transition or "").strip().lower()
    if transition not in ALLOWED_RECONCILE_TRANSITIONS:
        return {"ok": False, "error": f"transition {transition!r} is not allowlisted"}
    denied = _caller_is_supervisor(supervisor_task_id, caller_task_id)
    if denied:
        return {"ok": False, "error": denied}
    if get_objective(conn, objective_id) is None:
        return {"ok": False, "error": f"objective {objective_id} not found"}
    if not supervisor_owns_objective(conn, supervisor_task_id, objective_id):
        return {
            "ok": False,
            "error": _foreign_supervisor_objective_error(
                supervisor_task_id, objective_id
            ),
        }
    if not is_graph_descendant(conn, supervisor_task_id, descendant_task_id):
        return {
            "ok": False,
            "error": (
                f"{descendant_task_id} is not a graph descendant of "
                f"{supervisor_task_id}"
            ),
        }
    if not task_in_objective(conn, objective_id, descendant_task_id):
        return {"ok": False, "error": f"{descendant_task_id} is not in objective {objective_id}"}
    if task_has_live_claim(conn, descendant_task_id):
        return {"ok": False, "error": f"{descendant_task_id} still has a live claim"}
    packet = build_canonical_evidence(
        conn,
        descendant_task_id,
        objective_id=objective_id,
        supervisor_task_id=supervisor_task_id,
    )
    if packet.get("objective_id") != objective_id or not persisted_proof_present(packet):
        return {
            "ok": False,
            "error": f"{descendant_task_id} has no persisted exact-run/revision/review proof",
        }
    denied_head = _grant_head_error(conn, descendant_task_id, packet)
    if denied_head:
        return {"ok": False, "error": denied_head}
    expected = canonical_evidence_hash(packet)
    if evidence_hash != expected:
        return {
            "ok": False,
            "error": "evidence hash does not match persisted proof",
            "expected_hash": expected,
        }

    existing = conn.execute(
        "SELECT id, consumed_at FROM kanban_reconcile_grants "
        "WHERE objective_id = ? AND supervisor_task_id = ? "
        "AND descendant_task_id = ? AND transition = ? AND evidence_hash = ?",
        (objective_id, supervisor_task_id, descendant_task_id, transition, evidence_hash),
    ).fetchone()
    if existing and existing["consumed_at"]:
        return {
            "ok": False,
            "error": "descendant reconcile grant already consumed",
            "grant_id": existing["id"],
        }
    if existing:
        return {
            "ok": True,
            "grant_id": existing["id"],
            "issued": False,
            "consumed": False,
        }

    from hermes_cli.kanban_db import write_txn

    grant_id = _new_id("rg_")
    now = _now()
    with write_txn(conn, allow_nested=True):
        try:
            conn.execute(
                """
                INSERT INTO kanban_reconcile_grants (
                    id, objective_id, supervisor_task_id, descendant_task_id,
                    transition, evidence_hash, consumed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    grant_id, objective_id, supervisor_task_id, descendant_task_id,
                    transition, evidence_hash, now,
                ),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id, consumed_at FROM kanban_reconcile_grants "
                "WHERE objective_id = ? AND supervisor_task_id = ? "
                "AND descendant_task_id = ? AND transition = ? AND evidence_hash = ?",
                (objective_id, supervisor_task_id, descendant_task_id, transition, evidence_hash),
            ).fetchone()
            if row and not row["consumed_at"]:
                return {"ok": True, "grant_id": row["id"], "issued": False, "consumed": False}
            return {"ok": False, "error": "descendant reconcile grant already consumed"}
        _record_supervisor_event(
            conn,
            event_key=f"reconcile_grant_issued:{grant_id}",
            kind="descendant_grant_issued",
            task_id=supervisor_task_id,
            objective_id=objective_id,
            payload={
                "grant_id": grant_id,
                "descendant_task_id": descendant_task_id,
                "transition": transition,
                "evidence_hash": evidence_hash,
            },
        )
    return {
        "ok": True,
        "grant_id": grant_id,
        "issued": True,
        "consumed": False,
    }


def _close_descendant_task(
    conn: sqlite3.Connection,
    *,
    descendant_task_id: str,
    supervisor_task_id: str,
    grant_id: str,
    evidence_hash: str,
) -> None:
    """Close a descendant without the parent-done gate on ``complete_task``."""
    from hermes_cli.kanban_db import _append_event

    now = _now()
    summary = "supervisor reconciled descendant"
    conn.execute(
        """
        UPDATE tasks
           SET status = 'done',
               result = ?,
               completed_at = COALESCE(completed_at, ?),
               claim_lock = NULL,
               claim_expires = NULL,
               worker_pid = NULL,
               block_kind = NULL,
               block_recurrences = 0
         WHERE id = ? AND status NOT IN ('done', 'archived')
        """,
        (summary, now, descendant_task_id),
    )
    conn.execute(
        """
        UPDATE task_runs
           SET status = 'done', outcome = 'completed', ended_at = ?,
               summary = COALESCE(summary, ?),
               claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
         WHERE task_id = ? AND ended_at IS NULL
        """,
        (now, summary, descendant_task_id),
    )
    _append_event(
        conn,
        descendant_task_id,
        "completed",
        {
            "summary": summary,
            "reconciled_by": supervisor_task_id,
            "grant_id": grant_id,
            "evidence_hash": evidence_hash,
        },
    )


def reconcile_descendant(
    conn: sqlite3.Connection,
    *,
    supervisor_task_id: str,
    descendant_task_id: str,
    transition: str,
    evidence_hash: str,
    caller_task_id: Optional[str],
    objective_id: str,
) -> dict[str, Any]:
    """Consume an already-issued grant exactly once and apply the transition."""
    from hermes_cli.kanban_db import write_txn
    from hermes_cli.kanban_supervisor import (
        _record_supervisor_event,
        note_kanban_terminal,
        upsert_unit,
    )

    ensure_contract_tables(conn)
    transition = str(transition or "").strip().lower()
    if transition not in ALLOWED_RECONCILE_TRANSITIONS:
        return {"ok": False, "error": f"transition {transition!r} is not allowlisted"}
    denied = _caller_is_supervisor(supervisor_task_id, caller_task_id)
    if denied:
        return {"ok": False, "error": denied}
    if not supervisor_owns_objective(conn, supervisor_task_id, objective_id):
        return {
            "ok": False,
            "error": _foreign_supervisor_objective_error(
                supervisor_task_id, objective_id
            ),
        }
    if not is_graph_descendant(conn, supervisor_task_id, descendant_task_id):
        return {
            "ok": False,
            "error": (
                f"{descendant_task_id} is not a graph descendant of "
                f"{supervisor_task_id}"
            ),
        }
    if not task_in_objective(conn, objective_id, descendant_task_id):
        return {"ok": False, "error": f"{descendant_task_id} is not in objective {objective_id}"}
    if task_has_live_claim(conn, descendant_task_id):
        return {"ok": False, "error": f"{descendant_task_id} still has a live claim"}

    now = _now()
    with write_txn(conn, allow_nested=True):
        grant = conn.execute(
            "SELECT id, consumed_at FROM kanban_reconcile_grants "
            "WHERE objective_id = ? AND supervisor_task_id = ? "
            "AND descendant_task_id = ? AND transition = ? AND evidence_hash = ?",
            (objective_id, supervisor_task_id, descendant_task_id, transition, evidence_hash),
        ).fetchone()
        if grant is None:
            return {"ok": False, "error": "no issued descendant reconcile grant"}
        if grant["consumed_at"]:
            return {
                "ok": False,
                "error": "descendant reconcile grant already consumed",
                "grant_id": grant["id"],
            }
        packet = build_canonical_evidence(
            conn,
            descendant_task_id,
            objective_id=objective_id,
            supervisor_task_id=supervisor_task_id,
        )
        if packet.get("objective_id") != objective_id or not persisted_proof_present(packet):
            return {
                "ok": False,
                "error": f"{descendant_task_id} has no persisted exact-run/revision/review proof",
            }
        denied_head = _grant_head_error(conn, descendant_task_id, packet)
        if denied_head:
            return {"ok": False, "error": denied_head}
        expected = canonical_evidence_hash(packet)
        if evidence_hash != expected:
            return {
                "ok": False,
                "error": "evidence hash does not match persisted proof",
                "expected_hash": expected,
            }
        grant_id = grant["id"]
        consumed = conn.execute(
            "UPDATE kanban_reconcile_grants SET consumed_at = ? "
            "WHERE id = ? AND consumed_at IS NULL",
            (now, grant_id),
        )
        if consumed.rowcount != 1:
            return {
                "ok": False,
                "error": "descendant reconcile grant already consumed",
                "grant_id": grant_id,
            }
        if transition == "complete":
            _close_descendant_task(
                conn,
                descendant_task_id=descendant_task_id,
                supervisor_task_id=supervisor_task_id,
                grant_id=grant_id,
                evidence_hash=evidence_hash,
            )
            note_kanban_terminal(
                conn,
                descendant_task_id,
                status="done",
                proof={
                    "type": "descendant_reconcile",
                    "grant_id": grant_id,
                    "evidence_hash": evidence_hash,
                    "verified": True,
                    "classification": CLASS_SUCCESS,
                },
            )
        else:
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'blocked',
                       block_kind = 'capability',
                       claim_lock = NULL,
                       claim_expires = NULL,
                       worker_pid = NULL
                 WHERE id = ? AND status NOT IN ('done', 'archived')
                """,
                (descendant_task_id,),
            )
            upsert_unit(
                conn,
                objective_id=objective_id,
                kind="kanban",
                ref=descendant_task_id,
                status="failed",
                proof={
                    "type": "descendant_reconcile",
                    "transition": "fail",
                    "grant_id": grant_id,
                    "verified": True,
                    "classification": CLASS_FAILURE,
                },
            )
        _record_supervisor_event(
            conn,
            event_key=f"reconcile:{grant_id}",
            kind="descendant_reconcile",
            task_id=supervisor_task_id,
            objective_id=objective_id,
            payload={
                "grant_id": grant_id,
                "descendant_task_id": descendant_task_id,
                "transition": transition,
                "evidence_hash": evidence_hash,
            },
        )
    return {
        "ok": True,
        "grant_id": grant_id,
        "transition": transition,
        "descendant_task_id": descendant_task_id,
        "consumed": True,
    }


def review_policy_state(conn: sqlite3.Connection, task_id: str) -> tuple[str, int]:
    rows = conn.execute(
        "SELECT payload FROM kanban_supervisor_events "
        "WHERE kind = 'review_cap_policy' AND task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    policy = "capped"
    extra = 0
    for row in rows:
        payload = _parse_json(row["payload"])
        next_policy = str(payload.get("policy") or "")
        if next_policy == "uncapped":
            policy = "uncapped"
        elif next_policy == "parked":
            policy = "parked"
        elif next_policy == "add5":
            policy = "capped"
            extra += int(payload.get("extra") or 5)
    return policy, REVIEW_CAP + extra


def blocking_review_count_since_pass(conn: sqlite3.Connection, task_id: str) -> int:
    rows = conn.execute(
        "SELECT payload FROM kanban_supervisor_events "
        "WHERE kind = 'review_verdict' AND task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    count = 0
    for row in rows:
        payload = _parse_json(row["payload"])
        verdict = str(payload.get("verdict") or "").lower()
        if verdict == "pass" and not payload.get("blockers") and not payload.get("stale"):
            count = 0
            continue
        if verdict in BLOCKING_VERDICTS:
            count += 1
    return count


def record_review_verdict(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    verdict: str,
    head: Optional[str],
    blockers: Optional[list[str]] = None,
    remoko: Any = None,
    current_head: Optional[str] = None,
    git_head_fn: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> dict[str, Any]:
    from hermes_cli.kanban_supervisor import (
        _record_supervisor_event,
        _root_task_id,
        ensure_objective,
        invalidate_stale_reviews,
        request_owner_blocker,
        resolve_notify_origin,
        upsert_unit,
    )

    ensure_contract_tables(conn)
    verdict_n = str(verdict or "").strip().lower()
    blockers = list(blockers or [])
    live_head = current_head
    workspace = None
    row = conn.execute("SELECT workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is not None:
        workspace = row["workspace_path"]
    workspace_live = None
    if git_head_fn is not None:
        workspace_live = git_head_fn(workspace)
    elif workspace:
        from hermes_cli.kanban_supervisor import git_head as _git_head

        workspace_live = _git_head(workspace)
    if live_head is None:
        live_head = workspace_live
    stale = False
    if verdict_n == "pass" and not blockers:
        submitted_sha = _full_git_sha(head)
        current_sha = _full_git_sha(live_head)
        live_sha = _full_git_sha(workspace_live)
        if not live_sha:
            return {
                "ok": False,
                "error": "exact-head review requires an independently established live Git HEAD",
                "verdict": "insufficient_evidence",
                "head": head,
                "current_head": live_head,
            }
        if not submitted_sha or not current_sha:
            return {
                "ok": False,
                "error": "exact-head review requires full 40-character submitted and current HEAD",
                "verdict": "insufficient_evidence",
                "head": head,
                "current_head": live_head,
            }
        stale = bool(submitted_sha != current_sha or live_sha != submitted_sha)
    verified_pass = bool(
        verdict_n == "pass" and not blockers and not stale
    )
    payload = {
        "verdict": verdict_n,
        "head": head,
        "current_head": live_head,
        "blockers": blockers,
        "stale": stale,
        "verified": verified_pass,
    }
    count_before = blocking_review_count_since_pass(conn, task_id)
    seq = count_before + (1 if verdict_n in BLOCKING_VERDICTS else 0)
    _record_supervisor_event(
        conn,
        event_key=f"review_verdict:{task_id}:{head or 'none'}:{seq}:{verdict_n}:{int(stale)}",
        kind="review_verdict",
        task_id=task_id,
        payload=payload,
    )
    # Review-cap must not persist a live worker WebUI session as the
    # objective origin before the missing-origin check below.
    oid = ensure_objective(conn, _root_task_id(conn, task_id), allow_live=False)
    if stale:
        upsert_unit(
            conn,
            objective_id=oid,
            kind="kanban",
            ref=task_id,
            status="pending",
            next_gate="re-review",
            terminal_predicate="jude_verdict_pass",
            proof={
                "type": "jude_verdict",
                "verdict": "pass",
                "head": head,
                "current_head": live_head,
                "blockers": [],
                "stale": True,
            },
        )
        invalidate_stale_reviews(conn, git_head_fn=git_head_fn or (lambda _p: live_head))
        return {"ok": True, "verdict": "stale_pass", "review_cap": False, "invalidated": True}

    if verdict_n == "pass" and not blockers:
        bound_run = _latest_ended_run(conn, task_id)
        bound_run_id = (
            int(bound_run["id"]) if bound_run is not None and _run_is_successful_proven(bound_run) else None
        )
        upsert_unit(
            conn,
            objective_id=oid,
            kind="kanban",
            ref=task_id,
            status="done",
            terminal_predicate="jude_verdict_pass",
            proof={
                "type": "jude_verdict",
                "verdict": "pass",
                "head": _full_git_sha(head),
                "blockers": [],
                "verified": True,
                **({"run_id": bound_run_id} if bound_run_id is not None else {}),
            },
        )
        if git_head_fn is not None:
            invalidate_stale_reviews(conn, git_head_fn=git_head_fn)
        return {"ok": True, "verdict": "pass", "review_cap": False}

    count = blocking_review_count_since_pass(conn, task_id)
    policy, allowance = review_policy_state(conn, task_id)
    if policy == "uncapped" or policy == "parked" or count < allowance:
        return {
            "ok": True,
            "verdict": verdict_n,
            "blocking_count": count,
            "review_cap": policy == "parked" and count >= REVIEW_CAP,
            "allowance": allowance,
        }

    origin = resolve_notify_origin(conn, task_id, allow_live=False)
    if origin is None or not origin.usable:
        _record_supervisor_event(
            conn,
            event_key=f"lifecycle_fault:{task_id}:review_cap_missing_origin",
            kind="lifecycle_fault",
            task_id=task_id,
            objective_id=oid,
            payload={
                "reason": "review_cap_missing_origin",
                "internal": True,
                "decision_key": "review_cap",
            },
        )
        return {
            "ok": False,
            "error": "review-cap notification requires durable objective or supervisor origin",
            "verdict": verdict_n,
            "blocking_count": count,
            "review_cap": True,
            "request_id": None,
            "lifecycle_fault": "review_cap_missing_origin",
        }
    request_id = request_owner_blocker(
        conn,
        objective_id=oid,
        task_id=task_id,
        decision_key="review_cap",
        purpose=(
            "Review has hit the five-fail cap. Choose whether to keep reviewing "
            "this exact change, stop here, or lift the cap until it is clean."
        ),
        choices=list(REVIEW_CAP_CHOICES),
        recommendation="Add 5 reviews — keep going on this exact change.",
        consequence="The worker will send more reviews on the current head.",
        prohibitions="Do not merge, deploy, or restart anything.",
        risk="high",
        remoko=remoko,
    )
    return {
        "ok": True,
        "verdict": verdict_n,
        "blocking_count": count,
        "review_cap": True,
        "request_id": request_id,
        "allowance": allowance,
    }


def apply_review_cap_answer(conn: sqlite3.Connection, task_id: str, answer: str) -> str:
    from hermes_cli.kanban_supervisor import _record_supervisor_event

    text = str(answer or "").strip()
    if text == "Uncap until pass":
        policy = "uncapped"
        extra = 0
    elif text == "Stop here":
        policy = "parked"
        extra = 0
    else:
        policy = "add5"
        extra = 5
    _record_supervisor_event(
        conn,
        event_key=f"review_cap_policy:{task_id}:{policy}:{_now()}",
        kind="review_cap_policy",
        task_id=task_id,
        payload={"policy": policy, "answer": text, "extra": extra},
    )
    if policy != "parked":
        wake_durable_owner(conn, task_id, reason=f"review_cap:{policy}")
    return policy


def owner_blocker_is_pending(conn: sqlite3.Connection, objective_id: str) -> bool:
    row = conn.execute(
        "SELECT remoko_request_id, remoko_external_id, status FROM kanban_objectives WHERE id = ?",
        (objective_id,),
    ).fetchone()
    if row is None or not row["remoko_request_id"]:
        return False
    processed = conn.execute(
        "SELECT 1 FROM kanban_supervisor_events WHERE kind = 'owner_blocker_processed' "
        "AND objective_id = ? LIMIT 1",
        (objective_id,),
    ).fetchone()
    if processed:
        return False
    return str(row["status"] or "") == "blocked_owner"


def mark_owner_blocker_processed(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    request_id: Optional[str],
    mark_processed: Any = None,
) -> None:
    from hermes_cli.kanban_supervisor import _record_supervisor_event

    _record_supervisor_event(
        conn,
        event_key=f"owner_blocker_processed:{objective_id}:{request_id or 'none'}",
        kind="owner_blocker_processed",
        objective_id=objective_id,
        payload={"request_id": request_id},
    )
    if mark_processed is not None and request_id:
        try:
            mark_processed(request_id=request_id)
        except Exception:
            logger.debug("mark_processed failed", exc_info=True)


def missing_origin_after_remoko(conn: sqlite3.Connection, objective_id: str) -> bool:
    from hermes_cli.kanban_supervisor import origin_from_row

    row = conn.execute(
        "SELECT * FROM kanban_objectives WHERE id = ?",
        (objective_id,),
    ).fetchone()
    if row is None or not row["remoko_request_id"]:
        return False
    origin = origin_from_row(row)
    if origin.usable:
        return False
    subs = conn.execute(
        "SELECT 1 FROM kanban_notify_subs WHERE task_id = ? LIMIT 1",
        (row["root_task_id"],),
    ).fetchone()
    return subs is None


def wake_durable_owner(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Transition/wake a task through the real scheduler primitives."""
    from hermes_cli.kanban_db import (
        _append_event,
        promote_task,
        recompute_ready,
        release_stale_claims,
        unblock_task,
        write_txn,
    )
    from hermes_cli.kanban_supervisor import _record_supervisor_event

    ensure_contract_tables(conn)
    row = conn.execute(
        "SELECT id, status, claim_lock, claim_expires, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"task {task_id} not found"}
    before = str(row["status"] or "")
    action = "none"
    if before == "blocked":
        if unblock_task(conn, task_id):
            action = "unblocked"
    elif before == "todo":
        ok, _err = promote_task(
            conn, task_id, actor="supervisor", reason=reason, force=True,
        )
        if ok:
            action = "promoted"
    elif before == "running":
        now = _now()
        expires = row["claim_expires"]
        if expires is not None and int(expires) <= now:
            release_stale_claims(conn)
            action = "stale_claim_released"
        else:
            action = "already_running"
    elif before == "ready":
        action = "already_ready"
    elif before == "review":
        action = "already_review"
    after_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    after = str(after_row["status"] if after_row else before)
    with write_txn(conn, allow_nested=True):
        _append_event(
            conn,
            task_id,
            "status",
            {"status": after, "reason": reason, "wake_action": action, "prior_status": before},
        )
        _record_supervisor_event(
            conn,
            event_key=f"wake:{task_id}:{reason}:{before}:{after}:{_now()}",
            kind="supervisor_wake",
            task_id=task_id,
            payload={"reason": reason, "action": action, "from": before, "to": after},
        )
    try:
        recompute_ready(conn)
    except Exception:
        logger.debug("recompute_ready after wake failed", exc_info=True)
    return {"ok": True, "action": action, "from": before, "to": after}


def ingest_direct_fallback_answer(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    task_id: str,
    answer: str,
) -> dict[str, Any]:
    """A non-Remoko owner answer still wakes the durable subscribed owner."""
    from hermes_cli.kanban_supervisor import _record_supervisor_event

    ensure_contract_tables(conn)
    if not answer:
        return {"ok": False, "error": "empty answer"}
    conn.execute(
        "UPDATE kanban_objectives SET status = 'open', updated_at = ? WHERE id = ?",
        (_now(), objective_id),
    )
    _record_supervisor_event(
        conn,
        event_key=f"direct_fallback:{objective_id}:{_now()}",
        kind="direct_fallback_answer",
        task_id=task_id,
        objective_id=objective_id,
        payload={"answer": str(answer)[:200]},
    )
    wake = wake_durable_owner(conn, task_id, reason="direct_fallback_answer")
    return {"ok": True, "wake": wake}


def requeue_after_timeout(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_supervisor import (
        _root_task_id,
        ensure_objective,
        upsert_unit,
    )

    ensure_contract_tables(conn)
    now = _now()
    conn.execute(
        """
        UPDATE tasks
           SET claim_expires = ?,
               worker_pid = CASE WHEN worker_pid IS NULL THEN worker_pid ELSE 999999999 END,
               last_heartbeat_at = ?
         WHERE id = ? AND status = 'running'
        """,
        (now - 1, now - 3600, task_id),
    )
    kb.release_stale_claims(conn)
    oid = ensure_objective(conn, _root_task_id(conn, task_id))
    upsert_unit(
        conn,
        objective_id=oid,
        kind="kanban",
        ref=task_id,
        status="pending",
        next_gate="timeout_requeue",
    )
    wake = wake_durable_owner(conn, _root_task_id(conn, task_id), reason="timeout_requeue")
    child = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return {
        "ok": True,
        "child_status": child["status"] if child else None,
        "wake": wake,
    }


def verify_process_exit(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref: str,
    evidence_hash: str,
) -> dict[str, Any]:
    """Evidence-bound transition out of ``awaiting_verification``."""
    from hermes_cli.kanban_supervisor import get_objective, upsert_unit

    ensure_contract_tables(conn)
    row = conn.execute(
        "SELECT * FROM kanban_objective_units WHERE kind = ? AND "
        "(ref = ? OR ref LIKE ?) ORDER BY last_progress_at DESC LIMIT 1",
        (kind, ref, f"%:{ref}"),
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"no {kind} unit for {ref}"}
    proof = _parse_json(row["proof"])
    expected = canonical_evidence_hash(process_exit_evidence(proof))
    if evidence_hash != expected:
        return {
            "ok": False,
            "error": "evidence hash does not match persisted process-exit proof",
            "expected_hash": expected,
        }
    classification = str(proof.get("classification") or CLASS_MALFORMED)
    obj = get_objective(conn, row["objective_id"])
    supervisor_id = str((obj or {}).get("root_task_id") or "")
    if classification == CLASS_SUCCESS:
        proof = {**proof, "verified": True}
        upsert_unit(
            conn,
            objective_id=row["objective_id"],
            kind=kind,
            ref=row["ref"],
            status="done",
            proof=proof,
            next_gate="verified",
        )
        return {
            "ok": True,
            "status": "done",
            "classification": classification,
            "verified": True,
        }
    next_status = "failed" if classification == CLASS_FAILURE else "pending"
    proof = {**proof, "verified": False, "verification": classification}
    upsert_unit(
        conn,
        objective_id=row["objective_id"],
        kind=kind,
        ref=row["ref"],
        status=next_status,
        next_gate=f"process_exit:{classification}",
        proof=proof,
    )
    wake = None
    if supervisor_id:
        wake = wake_durable_owner(
            conn, supervisor_id, reason=f"process_exit:{classification}",
        )
    return {
        "ok": True,
        "status": next_status,
        "classification": classification,
        "verified": False,
        "wake": wake,
    }


def wake_after_process_exit(conn: sqlite3.Connection, *, kind: str, ref: str) -> list[dict[str, Any]]:
    """Wake the objective supervisor after a process exit (any classification)."""
    from hermes_cli.kanban_supervisor import get_objective

    ensure_contract_tables(conn)
    rows = conn.execute(
        "SELECT objective_id, proof, ref FROM kanban_objective_units WHERE kind = ?",
        (kind,),
    ).fetchall()
    wakes: list[dict[str, Any]] = []
    for row in rows:
        if row["ref"] != ref and not str(row["ref"]).endswith(f":{ref}"):
            continue
        obj = get_objective(conn, row["objective_id"])
        if not obj:
            continue
        proof = _parse_json(row["proof"])
        classification = str(proof.get("classification") or CLASS_NO_OUTPUT)
        wakes.append(
            wake_durable_owner(
                conn,
                str(obj["root_task_id"]),
                reason=f"process_exit:{classification}",
            )
        )
    return wakes


def reconcile_process_exits(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Supervisor tick: wake owners for unverified non-success process exits."""
    from hermes_cli.kanban_supervisor import _record_supervisor_event, get_objective

    ensure_contract_tables(conn)
    rows = conn.execute(
        "SELECT * FROM kanban_objective_units WHERE status = 'awaiting_verification'"
    ).fetchall()
    actions: list[dict[str, Any]] = []
    for row in rows:
        proof = _parse_json(row["proof"])
        classification = str(proof.get("classification") or CLASS_NO_OUTPUT)
        if classification == CLASS_SUCCESS:
            continue
        event_key = f"exit_wake:{row['id']}:{row['last_progress_at']}"
        inserted = _record_supervisor_event(
            conn,
            event_key=event_key,
            kind="process_exit_wake",
            task_id=None,
            objective_id=row["objective_id"],
            payload={"unit_id": row["id"], "classification": classification, "ref": row["ref"]},
        )
        if not inserted:
            continue
        obj = get_objective(conn, row["objective_id"])
        if not obj:
            continue
        wake = wake_durable_owner(
            conn,
            str(obj["root_task_id"]),
            reason=f"process_exit:{classification}",
        )
        actions.append({"unit_id": row["id"], "wake": wake, "classification": classification})
    return actions


def unit_is_verified_success(unit: dict[str, Any]) -> bool:
    if unit.get("status") != "done":
        return False
    proof = unit.get("proof")
    if isinstance(proof, str):
        proof = _parse_json(proof)
    if not isinstance(proof, dict) or not proof:
        return False
    return proof.get("verified") is True and (
        proof.get("classification") == CLASS_SUCCESS
        or proof.get("verdict") == "pass"
        or proof.get("type") in {"descendant_reconcile", "jude_verdict"}
    )
