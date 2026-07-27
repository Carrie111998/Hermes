"""Deterministic time-based lifecycle maintenance independent of model events."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Optional

from hermes_cli import (
    approval_artifacts,
    finance_db,
    objectives_db,
    organization_db,
    resource_budget,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS objective_maintenance_runs (
    id TEXT PRIMARY KEY, started_at INTEGER NOT NULL, finished_at INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objective_maintenance_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    last_checked_at INTEGER NOT NULL, last_change_run_id TEXT,
    last_summary_json TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='objective_maintenance_runs'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)


def run_housekeeping(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    compute_reconciliation_grace_seconds: int = 86_400,
) -> dict[str, Any]:
    """Expire authority and release capital without waiting for a wake event."""
    ensure_schema(conn)
    finance_db.ensure_schema(conn)
    organization_db.ensure_schema(conn)
    ts = int(time.time()) if now is None else int(now)
    started = int(time.time())
    summary: dict[str, Any] = {
        "expired_objectives": [],
        "requeued_objectives": [],
        "revoked_permits": [],
        "released_reservations": [],
        "suspended_employees": [],
        "expired_approvals": [],
        "disabled_triggers": 0,
        "repaired_compute_reconciliations": [],
        "compute_reconciliation_interventions": [],
    }
    summary["expired_approvals"] = approval_artifacts.expire_due(
        conn, now=ts
    )
    summary["repaired_compute_reconciliations"] = (
        resource_budget.repair_ledger_backed_reconciliations(conn)
    )
    from hermes_cli import operational_control

    operational_control.ensure_schema(conn)

    # Lifecycle state and its wake event can be committed by separate control
    # paths (for example an operator accepting an objective outside the worker
    # process). Reconcile that boundary before schedule dispatch so an active
    # objective cannot become permanently idle after a lost wake or restart.
    # The objective version makes the repair idempotent across workers.
    active_objectives = conn.execute(
        """SELECT o.id, o.version, o.status
             FROM objectives o
            WHERE o.status IN ('accepted', 'planned')
              AND NOT EXISTS (
                    SELECT 1 FROM objective_inbox i
                     WHERE i.objective_id=o.id
                       AND i.status IN ('pending', 'processing')
              )
            ORDER BY o.updated_at, o.id"""
    ).fetchall()
    for row in active_objectives:
        objective_id = str(row["id"])
        objectives_db.enqueue_objective_event(
            conn,
            objective_id=objective_id,
            event_type=f"objective.{row['status']}.reconcile",
            payload={
                "source": "objective_maintenance",
                "status": str(row["status"]),
                "objective_version": int(row["version"]),
                "reason": "active objective had no pending or processing wake",
            },
            dedupe_key=(
                f"objective-maintenance-wakeup:{objective_id}:"
                f"{int(row['version'])}:{row['status']}"
            ),
        )
        summary["requeued_objectives"].append(objective_id)
    for gap in resource_budget.stale_compute_reservations(
        conn,
        now=ts,
        grace_seconds=compute_reconciliation_grace_seconds,
    ):
        dedupe_key = (
            "compute-cost-unreconciled:"
            + str(gap["reservation_id"])
        )
        existing = conn.execute(
            """SELECT id FROM intervention_queue
                WHERE dedupe_key=? AND status='open'""",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            continue
        intervention_id = operational_control.raise_intervention(
            conn,
            organization_id=gap["organization_id"],
            objective_id=gap["objective_id"],
            category="compute_cost_unreconciled",
            summary=(
                "Planner compute cost lacks authoritative billing evidence"
            ),
            context={
                **gap,
                "grace_seconds": compute_reconciliation_grace_seconds,
                "sensitive_data_included": False,
            },
            options=[
                {
                    "id": "reconcile",
                    "label": "Attach provider billing evidence",
                },
                {
                    "id": "confirm_included",
                    "label": "Prove usage was subscription-included",
                },
            ],
            dedupe_key=(
                dedupe_key
            ),
        )
        summary["compute_reconciliation_interventions"].append(
            intervention_id
        )

    objectives = conn.execute(
        """SELECT id FROM objectives WHERE expires_at IS NOT NULL AND expires_at<=?
           AND status IN (
             'proposed','accepted','planned','authorized','executing','blocked'
           ) ORDER BY expires_at,id""",
        (ts,),
    ).fetchall()
    for row in objectives:
        objectives_db.transition_objective(
            conn, row["id"], "expired", actor="control:lifecycle",
            reason="objective duration elapsed",
        )
        summary["expired_objectives"].append(row["id"])
        with conn:
            for table in ("objective_event_subscriptions", "objective_schedules"):
                try:
                    updated = conn.execute(
                        f"UPDATE {table} SET status='disabled' "
                        "WHERE objective_id=? AND status='active'",
                        (row["id"],),
                    )
                    summary["disabled_triggers"] += updated.rowcount
                except sqlite3.OperationalError:
                    pass

    permits = conn.execute(
        """SELECT p.id permit_id,p.action_id,a.objective_id
           FROM permits p JOIN candidate_actions a ON a.id=p.action_id
          WHERE p.consumed_at IS NULL AND p.revoked_at IS NULL
            AND (
              p.expires_at<=? OR a.objective_id IN (
                SELECT id FROM objectives WHERE status IN (
                  'closed','cancelled','expired','abandoned','superseded'
                )
              )
            )
          ORDER BY p.expires_at,p.id""",
        (ts,),
    ).fetchall()
    for row in permits:
        with conn:
            conn.execute(
                "UPDATE permits SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (ts, row["permit_id"]),
            )
            conn.execute(
                """UPDATE candidate_actions SET status='expired',updated_at=?
                   WHERE id=? AND status='permitted'""",
                (ts, row["action_id"]),
            )
            objectives_db._append_event(
                conn,
                row["objective_id"],
                "permit_revoked",
                "control:lifecycle",
                {
                    "permit_id": row["permit_id"],
                    "action_id": row["action_id"],
                    "reason": "authority expired",
                },
            )
        summary["revoked_permits"].append(row["permit_id"])

    reservations = conn.execute(
        """SELECT action_id FROM budget_reservations
           WHERE status='reserved' AND (
             expires_at<=? OR action_id IN (
               SELECT id FROM candidate_actions WHERE status IN (
                 'denied','failed','expired'
               )
             )
           ) ORDER BY expires_at,action_id""",
        (ts,),
    ).fetchall()
    for row in reservations:
        finance_db.release_reservation(
            conn, row["action_id"], reason="authority_or_reservation_expired"
        )
        summary["released_reservations"].append(row["action_id"])

    employees = conn.execute(
        """SELECT e.id FROM employees e
           JOIN employee_mandates m ON m.employee_id=e.id
          WHERE e.status='active' AND m.expires_at IS NOT NULL AND m.expires_at<=?
            AND m.version=(
              SELECT MAX(m2.version) FROM employee_mandates m2
               WHERE m2.employee_id=e.id
            )
          ORDER BY m.expires_at,e.id""",
        (ts,),
    ).fetchall()
    for row in employees:
        organization_db.transition_employee(
            conn, row["id"], "suspended", actor="control:lifecycle"
        )
        summary["suspended_employees"].append(row["id"])

    finished = int(time.time())
    changed = any(
        bool(value)
        for key, value in summary.items()
        if key != "disabled_triggers"
    ) or int(summary["disabled_triggers"]) > 0
    run_id = f"maintenance_{uuid.uuid4().hex}" if changed else None
    with conn:
        if run_id is not None:
            conn.execute(
                """INSERT INTO objective_maintenance_runs
                   (id,started_at,finished_at,summary_json) VALUES (?,?,?,?)""",
                (run_id, started, finished, json.dumps(summary, sort_keys=True)),
            )
        conn.execute(
            """INSERT INTO objective_maintenance_state
               (singleton,last_checked_at,last_change_run_id,last_summary_json)
               VALUES (1,?,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 last_checked_at=excluded.last_checked_at,
                 last_change_run_id=COALESCE(
                   excluded.last_change_run_id,
                   objective_maintenance_state.last_change_run_id
                 ),
                 last_summary_json=CASE
                   WHEN excluded.last_change_run_id IS NULL
                   THEN objective_maintenance_state.last_summary_json
                   ELSE excluded.last_summary_json END""",
            (finished, run_id, json.dumps(summary, sort_keys=True)),
        )
    summary["run_id"] = run_id
    return summary


def latest_run(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM objective_maintenance_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["last_summary"] = json.loads(result.pop("last_summary_json"))
    return result
