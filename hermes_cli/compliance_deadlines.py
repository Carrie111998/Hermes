"""Deterministically wake governed work for approaching compliance deadlines."""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from hermes_cli import accounting_db
from hermes_cli import objective_portfolio
from hermes_cli import objectives_db
from hermes_cli import operational_control
from hermes_cli import regulatory_compliance


TERMINAL_OBJECTIVE_STATUSES = (
    "verified",
    "closed",
    "cancelled",
    "expired",
    "abandoned",
    "superseded",
)


def _active_root_objective(
    conn: sqlite3.Connection, organization_id: str
) -> Optional[str]:
    row = conn.execute(
        """
        SELECT o.id
          FROM objectives o
         WHERE o.organization_id=?
           AND o.status NOT IN (?,?,?,?,?,?,?)
           AND NOT EXISTS (
                 SELECT 1
                   FROM objective_relationships r
                  WHERE r.child_objective_id=o.id
                    AND r.relationship='decomposes_to'
               )
         ORDER BY o.created_at,o.id
         LIMIT 1
        """,
        (organization_id, "proposed", *TERMINAL_OBJECTIVE_STATUSES),
    ).fetchone()
    return str(row["id"]) if row is not None else None


def _latest_expiring_rows(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    horizon_at: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT id,due_at,amount_minor,currency,status
          FROM tax_obligations
         WHERE organization_id=?
           AND status NOT IN ('filed','paid','cancelled')
           AND due_at<=?
         ORDER BY due_at,id
        """,
        (organization_id, horizon_at),
    ).fetchall():
        rows.append(
            {
                "kind": "tax_obligation",
                "record_id": str(row["id"]),
                "due_at": int(row["due_at"]),
                "status": str(row["status"]),
                "amount_minor": int(row["amount_minor"]),
                "currency": str(row["currency"]),
            }
        )
    for row in conn.execute(
        """
        SELECT a.id,a.regime_id,a.verdict,a.expires_at
          FROM compliance_applicability a
         WHERE a.organization_id=? AND a.expires_at<=?
           AND a.assessed_at=(
                 SELECT MAX(latest.assessed_at)
                   FROM compliance_applicability latest
                  WHERE latest.organization_id=a.organization_id
                    AND latest.regime_id=a.regime_id
               )
         ORDER BY a.expires_at,a.id
        """,
        (organization_id, horizon_at),
    ).fetchall():
        rows.append(
            {
                "kind": "applicability_assessment",
                "record_id": str(row["id"]),
                "regime_id": str(row["regime_id"]),
                "verdict": str(row["verdict"]),
                "due_at": int(row["expires_at"]),
            }
        )
    for row in conn.execute(
        """
        SELECT e.id,e.control_name,e.verdict,e.expires_at
          FROM compliance_control_evidence e
         WHERE e.organization_id=? AND e.expires_at<=?
           AND e.verified_at=(
                 SELECT MAX(latest.verified_at)
                   FROM compliance_control_evidence latest
                  WHERE latest.organization_id=e.organization_id
                    AND latest.control_name=e.control_name
               )
         ORDER BY e.expires_at,e.id
        """,
        (organization_id, horizon_at),
    ).fetchall():
        rows.append(
            {
                "kind": "control_evidence",
                "record_id": str(row["id"]),
                "control_name": str(row["control_name"]),
                "verdict": str(row["verdict"]),
                "due_at": int(row["expires_at"]),
            }
        )
    for row in conn.execute(
        """
        SELECT id,review_due_at
          FROM compliance_regimes
         WHERE status='active' AND review_due_at<=?
         ORDER BY review_due_at,id
        """,
        (horizon_at,),
    ).fetchall():
        rows.append(
            {
                "kind": "regime_review",
                "record_id": str(row["id"]),
                "regime_id": str(row["id"]),
                "due_at": int(row["review_due_at"]),
            }
        )
    return rows


def dispatch_deadlines(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    horizon_seconds: int,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Route due records to an active root objective or an advisor handoff."""
    if not organization_id:
        raise ValueError("organization_id is required")
    if horizon_seconds < 0:
        raise ValueError("horizon_seconds must be non-negative")
    accounting_db.ensure_schema(conn)
    objective_portfolio.ensure_schema(conn)
    operational_control.ensure_schema(conn)
    regulatory_compliance.ensure_schema(conn)
    current = int(time.time()) if now is None else int(now)
    root_objective_id = _active_root_objective(conn, organization_id)
    records = _latest_expiring_rows(
        conn,
        organization_id=organization_id,
        horizon_at=current + horizon_seconds,
    )
    event_ids: list[str] = []
    intervention_ids: list[str] = []
    for record in records:
        payload = {
            **record,
            "organization_id": organization_id,
            "overdue": int(record["due_at"]) <= current,
        }
        dedupe = (
            f"compliance-deadline:{organization_id}:{record['kind']}:"
            f"{record['record_id']}:{record['due_at']}:"
            f"{'overdue' if payload['overdue'] else 'upcoming'}"
        )
        if root_objective_id is not None:
            before = conn.execute(
                "SELECT id FROM objective_inbox WHERE dedupe_key=?", (dedupe,)
            ).fetchone()
            event_id = objectives_db.enqueue_objective_event(
                conn,
                objective_id=root_objective_id,
                event_type="compliance.deadline.approaching",
                payload=payload,
                dedupe_key=dedupe,
            )
            if before is None:
                event_ids.append(event_id)
            continue
        intervention_dedupe = f"unowned:{dedupe}"
        before = conn.execute(
            """SELECT id FROM intervention_queue
                 WHERE dedupe_key=? AND status='open'""",
            (intervention_dedupe,),
        ).fetchone()
        intervention_id = operational_control.raise_intervention(
            conn,
            organization_id=organization_id,
            category="compliance_deadline_unowned",
            summary=(
                f"{record['kind']} deadline has no active objective owner"
            ),
            context=payload,
            options=[
                {"id": "create_objective", "label": "Create governed objective"},
                {"id": "review", "label": "Review deadline evidence"},
                {"id": "manual", "label": "Handle manually"},
            ],
            dedupe_key=intervention_dedupe,
        )
        if before is None:
            intervention_ids.append(intervention_id)
    return {
        "organization_id": organization_id,
        "root_objective_id": root_objective_id,
        "records_considered": len(records),
        "events_enqueued": len(event_ids),
        "event_ids": event_ids,
        "interventions_raised": len(intervention_ids),
        "intervention_ids": intervention_ids,
    }
