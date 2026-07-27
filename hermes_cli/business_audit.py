"""Append-only hash-chained lineage for governed business decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS business_audit_events (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
    sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
    objective_id TEXT, plan_id TEXT, action_id TEXT, permit_id TEXT,
    execution_result_id TEXT, verification_id TEXT,
    payload_json TEXT NOT NULL, previous_hash TEXT,
    event_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(organization_id, sequence)
);
CREATE TRIGGER IF NOT EXISTS business_audit_events_immutable_update
BEFORE UPDATE ON business_audit_events
BEGIN SELECT RAISE(ABORT, 'business audit events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS business_audit_events_immutable_delete
BEFORE DELETE ON business_audit_events
BEGIN SELECT RAISE(ABORT, 'business audit events are immutable'); END;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def append(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    event_type: str,
    payload: Any,
    objective_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    action_id: Optional[str] = None,
    permit_id: Optional[str] = None,
    execution_result_id: Optional[str] = None,
    verification_id: Optional[str] = None,
) -> str:
    ensure_schema(conn)
    previous = conn.execute(
        """SELECT sequence, event_hash FROM business_audit_events
           WHERE organization_id=? ORDER BY sequence DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    sequence = (int(previous["sequence"]) + 1) if previous else 1
    previous_hash = previous["event_hash"] if previous else None
    created_at = int(time.time())
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    material = json.dumps(
        {
            "action_id": action_id,
            "created_at": created_at,
            "event_type": event_type,
            "execution_result_id": execution_result_id,
            "objective_id": objective_id,
            "organization_id": organization_id,
            "payload": json.loads(payload_json),
            "permit_id": permit_id,
            "plan_id": plan_id,
            "previous_hash": previous_hash,
            "sequence": sequence,
            "verification_id": verification_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    event_hash = hashlib.sha256(material.encode()).hexdigest()
    event_id = f"audit_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO business_audit_events
               (id, organization_id, sequence, event_type, objective_id, plan_id,
                action_id, permit_id, execution_result_id, verification_id,
                payload_json, previous_hash, event_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, organization_id, sequence, event_type, objective_id,
                plan_id, action_id, permit_id, execution_result_id,
                verification_id, payload_json, previous_hash, event_hash, created_at,
            ),
        )
    return event_id


def verify_chain(conn: sqlite3.Connection, organization_id: str) -> bool:
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT * FROM business_audit_events WHERE organization_id=?
           ORDER BY sequence""",
        (organization_id,),
    ).fetchall()
    previous_hash = None
    for expected_sequence, row in enumerate(rows, 1):
        material = json.dumps(
            {
                "action_id": row["action_id"],
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "execution_result_id": row["execution_result_id"],
                "objective_id": row["objective_id"],
                "organization_id": row["organization_id"],
                "payload": json.loads(row["payload_json"]),
                "permit_id": row["permit_id"],
                "plan_id": row["plan_id"],
                "previous_hash": previous_hash,
                "sequence": expected_sequence,
                "verification_id": row["verification_id"],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        expected_hash = hashlib.sha256(material.encode()).hexdigest()
        if (
            row["sequence"] != expected_sequence
            or row["previous_hash"] != previous_hash
            or row["event_hash"] != expected_hash
        ):
            return False
        previous_hash = expected_hash
    return True


def export_audit_package(
    conn: sqlite3.Connection,
    organization_id: str,
) -> dict[str, Any]:
    """Produce a deterministic accountant/regulator package with lineage."""
    from hermes_cli import (
        accounting_db,
        approval_artifacts,
        authority_integrity,
        authority_recovery,
        business_commitments,
        outcome_attribution,
        objective_worker,
        operational_control,
        objective_triggers,
        finance_db,
        resource_budget,
        workforce_delegation,
        organization_db,
        planner_inferences,
    )

    organization_db.ensure_schema(conn)
    accounting_db.ensure_schema(conn)
    approval_artifacts.ensure_schema(conn)
    planner_inferences.ensure_schema(conn)
    authority_integrity.ensure_schema(conn)
    authority_recovery.ensure_schema(conn)
    business_commitments.ensure_schema(conn)
    outcome_attribution.ensure_schema(conn)
    objective_worker.ensure_schema(conn)
    operational_control.ensure_schema(conn)
    objective_triggers.ensure_schema(conn)
    finance_db.ensure_schema(conn)
    resource_budget.ensure_schema(conn)
    workforce_delegation.ensure_schema(conn)
    if not verify_chain(conn, organization_id):
        raise ValueError("business audit chain verification failed")

    def rows(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(query, params).fetchall()]

    organization = conn.execute(
        "SELECT * FROM organizations WHERE id=?", (organization_id,)
    ).fetchone()
    if organization is None:
        raise KeyError("organization not found")
    package: dict[str, Any] = {
        "format": "charterforge-business-audit-v1",
        # Existing consumers may key on the inherited export identifier.
        "compatibility_format": "hermes-business-audit-v1",
        "organization": dict(organization),
        "audit_events": rows(
            """SELECT * FROM business_audit_events WHERE organization_id=?
               ORDER BY sequence""",
            (organization_id,),
        ),
        "objectives": rows(
            "SELECT * FROM objectives WHERE organization_id=? ORDER BY created_at,id",
            (organization_id,),
        ),
        "interventions": rows(
            """SELECT * FROM intervention_queue WHERE organization_id=?
               ORDER BY created_at,id""",
            (organization_id,),
        ),
        "external_event_receipts": rows(
            """SELECT * FROM external_event_receipts
                WHERE organization_id=? ORDER BY received_at,id""",
            (organization_id,),
        ),
        "objective_event_subscriptions": rows(
            """SELECT * FROM objective_event_subscriptions
                WHERE organization_id=? ORDER BY created_at,id""",
            (organization_id,),
        ),
        "objective_schedules": rows(
            """SELECT * FROM objective_schedules
                WHERE organization_id=? ORDER BY created_at,id""",
            (organization_id,),
        ),
        "planner_inferences": rows(
            """SELECT * FROM planner_inferences WHERE organization_id=?
               ORDER BY started_at,id""",
            (organization_id,),
        ),
        "objective_resource_usage": rows(
            """SELECT usage.* FROM objective_resource_usage usage
                 JOIN objectives objective ON objective.id=usage.objective_id
                WHERE objective.organization_id=?
                ORDER BY usage.updated_at,usage.objective_id""",
            (organization_id,),
        ),
        "planner_compute_reservations": rows(
            """SELECT * FROM planner_compute_reservations
                WHERE organization_id=?
                ORDER BY created_at,id""",
            (organization_id,),
        ),
        "planner_compute_reconciliations": rows(
            """SELECT reconciliation.*
                 FROM planner_compute_reconciliations reconciliation
                 JOIN planner_compute_reservations reservation
                   ON reservation.id=reconciliation.reservation_id
                WHERE reservation.organization_id=?
                ORDER BY reconciliation.reconciled_at,reconciliation.id""",
            (organization_id,),
        ),
        "budget_reservations": rows(
            """SELECT reservation.* FROM budget_reservations reservation
                 JOIN treasury_accounts account
                   ON account.id=reservation.account_id
                WHERE account.organization_id=?
                ORDER BY reservation.created_at,reservation.id""",
            (organization_id,),
        ),
        "treasury_entries": rows(
            """SELECT entry.* FROM treasury_entries entry
                 JOIN treasury_accounts account ON account.id=entry.account_id
                WHERE account.organization_id=?
                ORDER BY entry.created_at,entry.id""",
            (organization_id,),
        ),
        "authority_policy_baselines": rows(
            """SELECT * FROM authority_policy_baselines WHERE organization_id=?
               ORDER BY version,id""",
            (organization_id,),
        ),
        "approval_artifacts": rows(
            """SELECT * FROM approval_artifacts WHERE organization_id=?
               ORDER BY approved_at,id""",
            (organization_id,),
        ),
        "authority_integrity_runs": rows(
            """SELECT * FROM authority_integrity_runs WHERE organization_id=?
               ORDER BY checked_at,id""",
            (organization_id,),
        ),
        "authority_recovery_events": rows(
            """SELECT * FROM authority_recovery_events WHERE organization_id=?
               ORDER BY created_at,id""",
            (organization_id,),
        ),
        "business_commitments": rows(
            """SELECT * FROM business_commitments WHERE organization_id=?
               ORDER BY created_at,id""",
            (organization_id,),
        ),
        "business_commitment_events": rows(
            """SELECT e.* FROM business_commitment_events e
               JOIN business_commitments c ON c.id=e.commitment_id
              WHERE c.organization_id=? ORDER BY e.created_at,e.id""",
            (organization_id,),
        ),
        "outcome_attributions": rows(
            """SELECT * FROM outcome_attributions WHERE organization_id=?
               ORDER BY recorded_at,id""",
            (organization_id,),
        ),
        "outcome_attribution_contradictions": rows(
            """SELECT contradiction.*
                 FROM outcome_attribution_contradictions contradiction
                 JOIN outcome_attributions attribution
                   ON attribution.id=contradiction.attribution_id
                WHERE attribution.organization_id=?
                ORDER BY contradiction.recorded_at,contradiction.id""",
            (organization_id,),
        ),
        "objective_workers": rows(
            """SELECT * FROM objective_workers
                WHERE organization_id=?
               ORDER BY started_at,id""",
            (organization_id,),
        ),
        "employee_task_grants": rows(
            """SELECT * FROM employee_task_grants WHERE organization_id=?
               ORDER BY created_at,id""",
            (organization_id,),
        ),
        "employee_task_bindings": rows(
            """SELECT binding.* FROM employee_task_bindings binding
                 JOIN employee_task_grants grant ON grant.id=binding.grant_id
                WHERE grant.organization_id=?
                ORDER BY binding.bound_at,binding.id""",
            (organization_id,),
        ),
        "employee_task_grant_revocations": rows(
            """SELECT revocation.*
                 FROM employee_task_grant_revocations revocation
                 JOIN employee_task_grants grant
                   ON grant.id=revocation.grant_id
                WHERE grant.organization_id=?
                ORDER BY revocation.revoked_at,revocation.id""",
            (organization_id,),
        ),
        "plans": rows(
            """SELECT p.* FROM plans p JOIN objectives o ON o.id=p.objective_id
                WHERE o.organization_id=? ORDER BY p.created_at,p.id""",
            (organization_id,),
        ),
        "actions": rows(
            """SELECT a.* FROM candidate_actions a
               JOIN objectives o ON o.id=a.objective_id
              WHERE o.organization_id=? ORDER BY a.created_at,a.id""",
            (organization_id,),
        ),
        "permits": rows(
            """SELECT p.* FROM permits p
               JOIN candidate_actions a ON a.id=p.action_id
               JOIN objectives o ON o.id=a.objective_id
              WHERE o.organization_id=? ORDER BY p.issued_at,p.id""",
            (organization_id,),
        ),
        "execution_results": rows(
            """SELECT r.* FROM execution_results r
               JOIN candidate_actions a ON a.id=r.action_id
               JOIN objectives o ON o.id=a.objective_id
              WHERE o.organization_id=? ORDER BY r.started_at,r.id""",
            (organization_id,),
        ),
        "verifications": rows(
            """SELECT v.* FROM verification_records v
               JOIN objectives o ON o.id=v.objective_id
              WHERE o.organization_id=? ORDER BY v.created_at,v.id""",
            (organization_id,),
        ),
        "ledger_entries": rows(
            """SELECT * FROM journal_entries WHERE organization_id=?
               ORDER BY occurred_at,id""",
            (organization_id,),
        ),
        "ledger_lines": rows(
            """SELECT l.* FROM journal_lines l JOIN journal_entries e
                 ON e.id=l.journal_entry_id WHERE e.organization_id=?
               ORDER BY e.occurred_at,e.id,l.id""",
            (organization_id,),
        ),
        "fiscal_periods": rows(
            """SELECT * FROM fiscal_periods WHERE organization_id=?
               ORDER BY starts_at,id""",
            (organization_id,),
        ),
        "fiscal_period_events": rows(
            """SELECT event.* FROM fiscal_period_events event
                 JOIN fiscal_periods period ON period.id=event.period_id
                WHERE period.organization_id=?
                ORDER BY event.occurred_at,event.id""",
            (organization_id,),
        ),
        "tax_registrations": rows(
            """SELECT * FROM tax_registrations WHERE organization_id=?
               ORDER BY effective_from,id""",
            (organization_id,),
        ),
        "tax_rates": rows(
            """SELECT rate.* FROM tax_rates rate
                 JOIN tax_registrations registration
                   ON registration.id=rate.registration_id
                WHERE registration.organization_id=?
                ORDER BY rate.effective_from,rate.id""",
            (organization_id,),
        ),
        "tax_obligations": rows(
            """SELECT * FROM tax_obligations WHERE organization_id=?
               ORDER BY due_at,id""",
            (organization_id,),
        ),
        "tax_obligation_events": rows(
            """SELECT event.* FROM tax_obligation_events event
                 JOIN tax_obligations obligation
                   ON obligation.id=event.obligation_id
                WHERE obligation.organization_id=?
                ORDER BY event.occurred_at,event.id""",
            (organization_id,),
        ),
    }
    canonical = json.dumps(package, separators=(",", ":"), sort_keys=True)
    package["package_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return package


def verify_audit_package(package: Mapping[str, Any]) -> bool:
    supplied = package.get("package_sha256")
    unsigned = dict(package)
    unsigned.pop("package_sha256", None)
    canonical = json.dumps(unsigned, separators=(",", ":"), sort_keys=True)
    return supplied == hashlib.sha256(canonical.encode()).hexdigest()
