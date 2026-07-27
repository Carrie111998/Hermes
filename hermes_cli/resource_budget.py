"""Durable compute ceilings for long-running objectives."""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any, Mapping


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS objective_resource_usage (
    objective_id TEXT PRIMARY KEY, cycles INTEGER NOT NULL DEFAULT 0,
    actions INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_compute_cost_minor INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS planner_compute_reservations (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    inbox_event_id TEXT,
    projected_input_tokens INTEGER NOT NULL,
    projected_output_tokens INTEGER NOT NULL,
    reserved_minor INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unreconciled',
    created_at INTEGER NOT NULL,
    FOREIGN KEY(objective_id) REFERENCES objectives(id)
);
CREATE INDEX IF NOT EXISTS idx_planner_compute_reservations_org
    ON planner_compute_reservations(organization_id,status,created_at,id);
CREATE TRIGGER IF NOT EXISTS planner_compute_reservations_immutable_update
BEFORE UPDATE ON planner_compute_reservations
BEGIN SELECT RAISE(ABORT, 'planner compute reservations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS planner_compute_reservations_immutable_delete
BEFORE DELETE ON planner_compute_reservations
BEGIN SELECT RAISE(ABORT, 'planner compute reservations are immutable'); END;
CREATE TABLE IF NOT EXISTS planner_compute_reconciliations (
    id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    actual_minor INTEGER NOT NULL,
    model TEXT,
    billing_provider TEXT,
    provider_reference TEXT,
    evidence_json TEXT NOT NULL,
    reconciled_at INTEGER NOT NULL,
    FOREIGN KEY(reservation_id) REFERENCES planner_compute_reservations(id)
);
CREATE TRIGGER IF NOT EXISTS planner_compute_reconciliations_immutable_update
BEFORE UPDATE ON planner_compute_reconciliations
BEGIN SELECT RAISE(ABORT, 'planner compute reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS planner_compute_reconciliations_immutable_delete
BEFORE DELETE ON planner_compute_reconciliations
BEGIN SELECT RAISE(ABORT, 'planner compute reconciliations are immutable'); END;
"""


class ResourceBudgetError(PermissionError):
    pass


DEFAULT_LIMITS = {
    "max_cycles_per_objective": 100,
    "max_actions_per_cycle": 1,
    "max_actions_per_objective": 500,
    "max_input_tokens_per_objective": 2_000_000,
    "max_output_tokens_per_objective": 500_000,
    "max_compute_cost_minor_per_objective": 10_000,
    "max_compute_cost_minor_per_organization": 10_000,
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='objective_resource_usage'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)


def usage(conn: sqlite3.Connection, objective_id: str) -> dict[str, int]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM objective_resource_usage WHERE objective_id=?",
        (objective_id,),
    ).fetchone()
    return dict(row) if row else {
        "cycles": 0,
        "actions": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_compute_cost_minor": 0,
    }


def assert_admissible(
    conn: sqlite3.Connection,
    objective_id: str,
    limits: Mapping[str, Any],
) -> None:
    current = usage(conn, objective_id)
    for field, key in _CEILINGS:
        limit = int(limits.get(key, 0))
        if limit <= 0:
            raise ResourceBudgetError(f"positive resource ceiling required: {key}")
        if int(current[field]) >= limit:
            raise ResourceBudgetError(f"objective exhausted {key}")


_CEILINGS = (
    ("cycles", "max_cycles_per_objective"),
    ("actions", "max_actions_per_objective"),
    ("input_tokens", "max_input_tokens_per_objective"),
    ("output_tokens", "max_output_tokens_per_objective"),
    ("estimated_compute_cost_minor", "max_compute_cost_minor_per_objective"),
)


def assert_projected_admissible(
    conn: sqlite3.Connection,
    objective_id: str,
    limits: Mapping[str, Any],
    *,
    cycles: int = 0,
    actions: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_compute_cost_minor: int = 0,
) -> None:
    """Reject work whose conservative reservation would cross a hard ceiling."""
    projected = {
        "cycles": cycles,
        "actions": actions,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_compute_cost_minor": estimated_compute_cost_minor,
    }
    if min(projected.values()) < 0:
        raise ValueError("projected resource usage cannot be negative")
    current = usage(conn, objective_id)
    for field, key in _CEILINGS:
        limit = int(limits.get(key, 0))
        if limit <= 0:
            raise ResourceBudgetError(f"positive resource ceiling required: {key}")
        if int(current[field]) + int(projected[field]) > limit:
            raise ResourceBudgetError(f"objective exhausted {key}")


def record_cycle(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    actions: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_compute_cost_minor: int = 0,
) -> None:
    if min(actions, input_tokens, output_tokens, estimated_compute_cost_minor) < 0:
        raise ValueError("resource usage cannot be negative")
    ensure_schema(conn)
    with conn:
        conn.execute(
            """INSERT INTO objective_resource_usage
               (objective_id, cycles, actions, input_tokens, output_tokens,
                estimated_compute_cost_minor, updated_at)
               VALUES (?, 1, ?, ?, ?, ?, ?)
               ON CONFLICT(objective_id) DO UPDATE SET
                 cycles=cycles+1, actions=actions+excluded.actions,
                 input_tokens=input_tokens+excluded.input_tokens,
                 output_tokens=output_tokens+excluded.output_tokens,
                 estimated_compute_cost_minor=estimated_compute_cost_minor+
                   excluded.estimated_compute_cost_minor,
                 updated_at=excluded.updated_at""",
            (
                objective_id, actions, input_tokens, output_tokens,
                estimated_compute_cost_minor, int(time.time()),
            ),
        )


def organization_compute_usage(
    conn: sqlite3.Connection, organization_id: str
) -> int:
    """Return conservative compute commitments across the company portfolio."""
    ensure_schema(conn)
    row = conn.execute(
        """SELECT
             COALESCE(SUM(usage.estimated_compute_cost_minor),0)
             - COALESCE((
                 SELECT SUM(reservation.reserved_minor-reconciliation.actual_minor)
                   FROM planner_compute_reconciliations reconciliation
                   JOIN planner_compute_reservations reservation
                     ON reservation.id=reconciliation.reservation_id
                  WHERE reservation.organization_id=?
               ),0) AS n
             FROM objective_resource_usage usage
             JOIN objectives objective ON objective.id=usage.objective_id
            WHERE objective.organization_id=?""",
        (organization_id, organization_id),
    ).fetchone()
    return int(row["n"])


def reserve_planner_call(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    limits: Mapping[str, Any],
    input_tokens: int,
    output_tokens: int,
    estimated_compute_cost_minor: int,
    enforce_treasury: bool,
    inbox_event_id: str | None = None,
) -> str:
    """Atomically reserve one planner call across objective and company cash."""
    if min(input_tokens, output_tokens, estimated_compute_cost_minor) < 0:
        raise ValueError("planner reservation cannot be negative")
    ensure_schema(conn)
    if enforce_treasury:
        from hermes_cli import finance_db

        finance_db.ensure_schema(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """SELECT cycles,actions,input_tokens,output_tokens,
                      estimated_compute_cost_minor
                 FROM objective_resource_usage WHERE objective_id=?""",
            (objective_id,),
        ).fetchone()
        current_usage = (
            dict(current)
            if current is not None
            else {
                "cycles": 0,
                "actions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_compute_cost_minor": 0,
            }
        )
        if objective_id:
            reconciled = int(
                conn.execute(
                    """SELECT COALESCE(
                               SUM(reservation.reserved_minor-
                                   reconciliation.actual_minor),0
                             ) AS n
                         FROM planner_compute_reconciliations reconciliation
                         JOIN planner_compute_reservations reservation
                           ON reservation.id=reconciliation.reservation_id
                        WHERE reservation.objective_id=?""",
                    (objective_id,),
                ).fetchone()["n"]
            )
            current_usage["estimated_compute_cost_minor"] = max(
                0,
                int(current_usage["estimated_compute_cost_minor"])
                - reconciled,
            )
        projected = {
            "cycles": 1,
            "actions": 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_compute_cost_minor": estimated_compute_cost_minor,
        }
        for field, key in _CEILINGS:
            limit = int(limits.get(key, 0))
            if limit <= 0:
                raise ResourceBudgetError(
                    f"positive resource ceiling required: {key}"
                )
            if int(current_usage[field]) + projected[field] > limit:
                raise ResourceBudgetError(f"objective exhausted {key}")

        objective = conn.execute(
            "SELECT organization_id,currency FROM objectives WHERE id=?",
            (objective_id,),
        ).fetchone()
        if objective is None:
            if enforce_treasury:
                raise ResourceBudgetError(
                    "planner reservation has no organization-bound objective"
                )
        else:
            organization_id = str(objective["organization_id"])
            organization_used = int(
                conn.execute(
                    """SELECT
                         COALESCE(SUM(usage.estimated_compute_cost_minor),0)
                         - COALESCE((
                             SELECT SUM(
                                      reservation.reserved_minor-
                                      reconciliation.actual_minor
                                    )
                               FROM planner_compute_reconciliations reconciliation
                               JOIN planner_compute_reservations reservation
                                 ON reservation.id=
                                    reconciliation.reservation_id
                              WHERE reservation.organization_id=?
                           ),0) AS n
                         FROM objective_resource_usage usage
                         JOIN objectives objective
                           ON objective.id=usage.objective_id
                        WHERE objective.organization_id=?""",
                    (organization_id, organization_id),
                ).fetchone()["n"]
            )
            organization_limit = int(
                limits.get(
                    "max_compute_cost_minor_per_organization", 0
                )
            )
            if organization_limit <= 0:
                raise ResourceBudgetError(
                    "positive resource ceiling required: "
                    "max_compute_cost_minor_per_organization"
                )
            if (
                organization_used + estimated_compute_cost_minor
                > organization_limit
            ):
                raise ResourceBudgetError(
                    "organization exhausted "
                    "max_compute_cost_minor_per_organization"
                )
            if enforce_treasury:
                currency = objective["currency"]
                if not currency:
                    organization = conn.execute(
                        """SELECT base_currency FROM organizations
                            WHERE id=?""",
                        (organization_id,),
                    ).fetchone()
                    currency = (
                        str(organization["base_currency"])
                        if organization is not None
                        else ""
                    )
                account = conn.execute(
                    """SELECT id FROM treasury_accounts
                        WHERE organization_id=? AND name='operating'
                          AND currency=?""",
                        (
                            organization_id,
                            str(currency).upper(),
                        ),
                ).fetchone()
                if account is None:
                    raise ResourceBudgetError(
                        "organization has no operating treasury account"
                    )
                now = int(time.time())
                cash = int(
                    conn.execute(
                        """SELECT COALESCE(SUM(amount_minor),0) AS n
                             FROM treasury_entries WHERE account_id=?""",
                        (account["id"],),
                    ).fetchone()["n"]
                )
                external_reserved = int(
                    conn.execute(
                        """SELECT COALESCE(SUM(amount_minor),0) AS n
                             FROM budget_reservations
                            WHERE account_id=? AND status='reserved'
                              AND expires_at>?""",
                        (account["id"], now),
                    ).fetchone()["n"]
                )
                if (
                    cash - external_reserved - organization_used
                    < estimated_compute_cost_minor
                ):
                    raise ResourceBudgetError(
                        "insufficient uncommitted treasury for planner call"
                    )
        reservation_id = f"compute_{uuid.uuid4().hex}"
        organization_id = (
            str(objective["organization_id"])
            if objective is not None
            else "__unscoped__"
        )
        conn.execute(
            """INSERT INTO objective_resource_usage
               (objective_id,cycles,actions,input_tokens,output_tokens,
                estimated_compute_cost_minor,updated_at)
               VALUES (?,1,0,?,?,?,?)
               ON CONFLICT(objective_id) DO UPDATE SET
                 cycles=cycles+1,
                 input_tokens=input_tokens+excluded.input_tokens,
                 output_tokens=output_tokens+excluded.output_tokens,
                 estimated_compute_cost_minor=estimated_compute_cost_minor+
                   excluded.estimated_compute_cost_minor,
                 updated_at=excluded.updated_at""",
            (
                objective_id,
                input_tokens,
                output_tokens,
                estimated_compute_cost_minor,
                int(time.time()),
            ),
        )
        conn.execute(
            """INSERT INTO planner_compute_reservations
               (id,organization_id,objective_id,inbox_event_id,
                projected_input_tokens,projected_output_tokens,reserved_minor,
                status,created_at)
               VALUES (?,?,?,?,?,?,?,'unreconciled',?)""",
            (
                reservation_id,
                organization_id,
                objective_id,
                inbox_event_id,
                input_tokens,
                output_tokens,
                estimated_compute_cost_minor,
                int(time.time()),
            ),
        )
        conn.commit()
        return reservation_id
    except Exception:
        conn.rollback()
        raise


def compute_reservation_posture(
    conn: sqlite3.Connection, organization_id: str
) -> dict[str, Any]:
    """Summarize individually traceable compute exposure without prompt data."""
    ensure_schema(conn)
    row = conn.execute(
        """SELECT COUNT(*) AS n,COALESCE(SUM(reserved_minor),0) AS amount,
                  MIN(created_at) AS oldest
             FROM planner_compute_reservations reservation
            WHERE organization_id=?
              AND NOT EXISTS (
                  SELECT 1 FROM planner_compute_reconciliations reconciliation
                   WHERE reconciliation.reservation_id=reservation.id
              )""",
        (organization_id,),
    ).fetchone()
    now = int(time.time())
    return {
        "unreconciled_count": int(row["n"]),
        "unreconciled_minor": int(row["amount"]),
        "oldest_age_seconds": (
            max(0, now - int(row["oldest"]))
            if row["oldest"] is not None
            else None
        ),
        "sensitive_data_included": False,
    }


def stale_compute_reservations(
    conn: sqlite3.Connection,
    *,
    now: int,
    grace_seconds: int,
) -> list[dict[str, Any]]:
    """Group unreconciled compute evidence gaps that require advisor help."""
    if grace_seconds <= 0:
        raise ValueError("compute reconciliation grace must be positive")
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT reservation.*
             FROM planner_compute_reservations reservation
            WHERE reservation.created_at<=?
              AND NOT EXISTS (
                  SELECT 1 FROM planner_compute_reconciliations reconciliation
                   WHERE reconciliation.reservation_id=reservation.id
              )
            ORDER BY reservation.organization_id,reservation.created_at,
                     reservation.id""",
        (int(now) - grace_seconds,),
    ).fetchall()
    return [
        {
            "organization_id": str(row["organization_id"]),
            "objective_id": str(row["objective_id"]),
            "reservation_id": str(row["id"]),
            "reserved_minor": int(row["reserved_minor"]),
            "created_at": int(row["created_at"]),
        }
        for row in rows
    ]


def reconcile_compute_reservation(
    conn: sqlite3.Connection,
    *,
    reservation_id: str,
    status: str,
    actual_minor: int,
    model: str | None,
    billing_provider: str | None,
    provider_reference: str | None,
    evidence: Mapping[str, Any],
) -> str:
    """Append independently evidenced settlement for one compute reservation."""
    if status not in {"included", "provider_confirmed", "released"}:
        raise ValueError("compute reconciliation status is invalid")
    if actual_minor < 0:
        raise ValueError("actual compute cost cannot be negative")
    ensure_schema(conn)
    reservation = conn.execute(
        "SELECT * FROM planner_compute_reservations WHERE id=?",
        (reservation_id,),
    ).fetchone()
    if reservation is None:
        raise KeyError("compute reservation not found")
    if actual_minor > int(reservation["reserved_minor"]):
        raise ResourceBudgetError(
            "actual compute cost exceeds conservative reservation"
        )
    if status in {"included", "released"} and actual_minor != 0:
        raise ValueError("included compute must reconcile at zero cost")
    if not billing_provider:
        raise ValueError(
            "compute reconciliation requires the billable provider identity"
        )
    if status == "provider_confirmed" and (
        not provider_reference or not evidence
    ):
        raise ValueError(
            "paid compute reconciliation requires provider evidence"
        )
    import json

    evidence_json = json.dumps(
        dict(evidence), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    existing = conn.execute(
        """SELECT * FROM planner_compute_reconciliations
            WHERE reservation_id=?""",
        (reservation_id,),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["status"]) != status
            or int(existing["actual_minor"]) != actual_minor
            or (existing["model"] or None) != (model or None)
            or (existing["billing_provider"] or None) != (billing_provider or None)
            or (existing["provider_reference"] or None)
            != (provider_reference or None)
            or str(existing["evidence_json"]) != evidence_json
        ):
            raise ResourceBudgetError(
                "compute reservation already has different reconciliation parameters"
            )
        return str(existing["id"])
    if actual_minor:
        from hermes_cli import finance_db

        objective = conn.execute(
            "SELECT organization_id,currency FROM objectives WHERE id=?",
            (reservation["objective_id"],),
        ).fetchone()
        currency = objective["currency"] if objective is not None else None
        if not currency and objective is not None:
            organization = conn.execute(
                "SELECT base_currency FROM organizations WHERE id=?",
                (objective["organization_id"],),
            ).fetchone()
            currency = organization["base_currency"] if organization else None
        account_id = (
            finance_db.operating_account_for_organization(
                conn,
                str(reservation["organization_id"]),
                str(currency or ""),
            )
            if currency
            else None
        )
        if account_id is None:
            raise ResourceBudgetError(
                "compute reconciliation has no operating treasury account"
            )
        finance_db.record_entry(
            conn,
            account_id=account_id,
            kind="ai_compute",
            amount_minor=-actual_minor,
            currency=str(currency),
            objective_id=str(reservation["objective_id"]),
            external_reference=str(provider_reference),
            idempotency_key=f"compute-settlement:{reservation_id}",
            evidence={
                **dict(evidence),
                "billing_provider": billing_provider,
                "model": model,
            },
        )
    reconciliation_id = f"compute_recon_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO planner_compute_reconciliations
               (id,reservation_id,status,actual_minor,model,billing_provider,
                provider_reference,evidence_json,reconciled_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                reconciliation_id,
                reservation_id,
                status,
                actual_minor,
                model,
                billing_provider,
                provider_reference,
                evidence_json,
                int(time.time()),
            ),
        )
    return reconciliation_id


def repair_ledger_backed_reconciliations(
    conn: sqlite3.Connection,
) -> list[str]:
    """Finish settlements whose idempotent expense committed before lineage."""
    ensure_schema(conn)
    from hermes_cli import finance_db

    finance_db.ensure_schema(conn)
    rows = conn.execute(
        """SELECT reservation.id AS reservation_id,
                  entry.amount_minor,entry.external_reference,
                  entry.evidence_json
             FROM planner_compute_reservations reservation
             JOIN treasury_entries entry
               ON entry.idempotency_key=
                  'compute-settlement:' || reservation.id
            WHERE entry.kind='ai_compute'
              AND entry.amount_minor<0
              AND NOT EXISTS (
                  SELECT 1 FROM planner_compute_reconciliations reconciliation
                   WHERE reconciliation.reservation_id=reservation.id
              )
            ORDER BY entry.created_at,entry.id""",
    ).fetchall()
    repaired = []
    import json

    for row in rows:
        evidence = json.loads(row["evidence_json"])
        reconcile_compute_reservation(
            conn,
            reservation_id=str(row["reservation_id"]),
            status="provider_confirmed",
            actual_minor=-int(row["amount_minor"]),
            model=(
                str(evidence.get("model"))
                if evidence.get("model")
                else None
            ),
            billing_provider=(
                str(evidence.get("billing_provider"))
                if evidence.get("billing_provider")
                else None
            ),
            provider_reference=str(row["external_reference"]),
            evidence={
                **evidence,
                "recovered_from_idempotent_treasury_entry": True,
            },
        )
        repaired.append(str(row["reservation_id"]))
    return repaired


def record_actions(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    actions: int,
) -> None:
    """Add post-plan action usage when the planner reserved its call up front."""
    if actions < 0:
        raise ValueError("resource usage cannot be negative")
    ensure_schema(conn)
    with conn:
        updated = conn.execute(
            """UPDATE objective_resource_usage
                  SET actions=actions+?,updated_at=?
                WHERE objective_id=?""",
            (actions, int(time.time()), objective_id),
        )
        if updated.rowcount != 1:
            raise ResourceBudgetError(
                "planner action accounting has no pre-call reservation"
            )
