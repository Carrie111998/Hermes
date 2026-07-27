"""Deterministic staffing policy for a CEO-first autonomous business.

Charterforge starts as a solo founder.  Extra employee agents are organizational
cost and coordination surface, so a planner may propose a hire but cannot
justify one with prose alone.  This module requires measurable evidence of a
blocking capability gap, sustained capacity pressure, or separation-of-duty
need before a hire is warranted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hiring_decisions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    proposed_case_json TEXT NOT NULL,
    derived_evidence_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    employment_class TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    evaluated_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hiring_decisions_org
    ON hiring_decisions(organization_id, created_at);
CREATE TABLE IF NOT EXISTS hiring_engagements (
    decision_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL,
    employment_class TEXT NOT NULL,
    materialized_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS hiring_decisions_immutable_update
BEFORE UPDATE ON hiring_decisions
BEGIN SELECT RAISE(ABORT, 'hiring decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS hiring_decisions_immutable_delete
BEFORE DELETE ON hiring_decisions
BEGIN SELECT RAISE(ABORT, 'hiring decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS hiring_engagements_immutable_update
BEFORE UPDATE ON hiring_engagements
BEGIN SELECT RAISE(ABORT, 'hiring engagements are immutable'); END;
CREATE TRIGGER IF NOT EXISTS hiring_engagements_immutable_delete
BEFORE DELETE ON hiring_engagements
BEGIN SELECT RAISE(ABORT, 'hiring engagements are immutable'); END;
"""


@dataclass(frozen=True)
class HiringDecision:
    verdict: str  # hire | defer | deny
    reason: str
    employment_class: str = ""  # contractor | fte when verdict=hire
    evidence: dict[str, Any] = field(default_factory=dict)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def evaluate_hiring_case_from_state(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    case: Mapping[str, Any],
    policy: Mapping[str, Any],
    idempotency_key: str,
    evaluated_by: str,
) -> tuple[str, HiringDecision]:
    """Evaluate and immutably record a staffing case from authoritative state."""
    from hermes_cli import objectives_db, organization_db

    ensure_schema(conn)
    organization_db.ensure_schema(conn)
    if len(idempotency_key.strip()) < 16:
        raise ValueError("hiring decision requires a high-entropy idempotency key")
    existing = conn.execute(
        "SELECT * FROM hiring_decisions WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        return str(existing["id"]), HiringDecision(
            str(existing["verdict"]),
            str(existing["reason"]),
            str(existing["employment_class"]),
            json.loads(existing["derived_evidence_json"]),
        )

    objective_id = str(case.get("objective_id") or "")
    objective = conn.execute(
        "SELECT * FROM objectives WHERE id=? AND organization_id=?",
        (objective_id, organization_id),
    ).fetchone()
    if objective is None:
        raise ValueError("hiring case objective does not belong to the organization")
    if objective["status"] in {
        "proposed",
        "closed",
        "cancelled",
        "expired",
        "abandoned",
        "superseded",
    }:
        raise ValueError(
            f"objective status {objective['status']} cannot sponsor a hire"
        )
    organization = conn.execute(
        "SELECT * FROM organizations WHERE id=?", (organization_id,)
    ).fetchone()
    if organization is None:
        raise ValueError("organization not found")
    workforce = conn.execute(
        """SELECT COUNT(*) AS headcount,
                  COALESCE(SUM(annual_cost_minor),0) AS payroll
           FROM employees WHERE organization_id=?
             AND status IN ('approved','provisioning','active','suspended')""",
        (organization_id,),
    ).fetchone()

    missing_capability = str(case.get("missing_capability") or "").strip()
    derived = dict(case)
    if missing_capability:
        gap = conn.execute(
            """SELECT
                 COUNT(DISTINCT CASE WHEN EXISTS (
                   SELECT 1 FROM objective_events blocked_event
                    WHERE blocked_event.objective_id=o.id
                      AND blocked_event.next_status='blocked'
                 ) THEN o.id END)
                   AS blocked_objectives,
                 COUNT(DISTINCT a.plan_id) AS capability_gap_cycles
               FROM objectives o
               JOIN candidate_actions a ON a.objective_id=o.id
              WHERE o.organization_id=? AND a.required_capability=?
                AND a.status IN ('proposed','denied','failed','expired')""",
            (organization_id, missing_capability),
        ).fetchone()
        derived["blocked_objectives"] = int(gap["blocked_objectives"])
        derived["capability_gap_cycles"] = int(gap["capability_gap_cycles"])
    else:
        derived["blocked_objectives"] = 0
        derived["capability_gap_cycles"] = 0

    decision = evaluate_hiring_case(
        case=derived,
        organization=dict(organization),
        current_headcount=int(workforce["headcount"]),
        current_payroll_minor=int(workforce["payroll"]),
        policy=policy,
    )
    evidence = {
        **decision.evidence,
        "organization_id": organization_id,
        "objective_id": objective_id,
        "current_headcount": int(workforce["headcount"]),
        "current_payroll_minor": int(workforce["payroll"]),
        "authoritative_blocked_objectives": int(derived["blocked_objectives"]),
        "authoritative_capability_gap_cycles": int(
            derived["capability_gap_cycles"]
        ),
    }
    recorded = HiringDecision(
        decision.verdict,
        decision.reason,
        decision.employment_class,
        evidence,
    )
    decision_id = f"hiring_decision_{uuid.uuid4().hex}"
    evidence_json = _json(evidence)
    with conn:
        conn.execute(
            """INSERT INTO hiring_decisions (
                 id,organization_id,objective_id,idempotency_key,
                 proposed_case_json,derived_evidence_json,policy_json,
                 verdict,reason,employment_class,evidence_sha256,
                 evaluated_by,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id,
                organization_id,
                objective_id,
                idempotency_key,
                _json(dict(case)),
                evidence_json,
                _json(dict(policy)),
                recorded.verdict,
                recorded.reason,
                recorded.employment_class,
                hashlib.sha256(evidence_json.encode()).hexdigest(),
                evaluated_by,
                int(time.time()),
            ),
        )
    return decision_id, recorded


def materialize_hiring_decision(
    conn: sqlite3.Connection,
    decision_id: str,
    *,
    display_name: str,
    title: str,
    level: str,
    manager_id: str,
    mandate: Mapping[str, Any],
    actor: str,
) -> str:
    """Create one approved employee from an immutable positive decision."""
    from hermes_cli import organization_db

    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM hiring_decisions WHERE id=?", (decision_id,)
    ).fetchone()
    if row is None:
        raise ValueError("hiring decision not found")
    if row["verdict"] != "hire":
        raise PermissionError("only a positive hiring decision may add headcount")
    existing_engagement = conn.execute(
        "SELECT employee_id FROM hiring_engagements WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    if existing_engagement is not None:
        return str(existing_engagement["employee_id"])

    proposed_case = json.loads(row["proposed_case_json"])
    organization_id = str(row["organization_id"])
    employment_class = str(row["employment_class"])
    employment_type = "contractor" if employment_class == "contractor" else "agent"
    mandate_expires_at = mandate.get("expires_at")
    if employment_class == "contractor" and (
        not isinstance(mandate_expires_at, int)
        or mandate_expires_at <= int(time.time())
    ):
        raise ValueError("contractor hiring decision requires a future mandate expiry")
    annual_cost_minor = int(proposed_case.get("annual_cost_minor") or 0)
    organization = conn.execute(
        "SELECT base_currency, headcount_limit, payroll_budget_minor FROM organizations WHERE id=?",
        (organization_id,),
    ).fetchone()
    if organization is None:
        raise ValueError("hiring organization not found")
    headcount, payroll = organization_db._active_headcount_and_payroll(
        conn, organization_id
    )
    if (
        organization["headcount_limit"] is not None
        and headcount + 1 > int(organization["headcount_limit"])
    ):
        raise PermissionError(
            "hiring decision is stale: headcount limit is no longer available"
        )
    if (
        organization["payroll_budget_minor"] is not None
        and payroll + annual_cost_minor > int(organization["payroll_budget_minor"])
    ):
        raise PermissionError(
            "hiring decision is stale: payroll budget is no longer available"
        )
    manager = organization_db.get_employee_record(conn, manager_id)
    if manager["organization_id"] != organization_id:
        raise PermissionError("hire manager belongs to another organization")
    proposed_by = f"hiring-decision:{decision_id}"
    existing_employee = conn.execute(
        """SELECT id,status FROM employees
           WHERE organization_id=? AND proposed_by=?
           ORDER BY created_at,id LIMIT 1""",
        (organization_id, proposed_by),
    ).fetchone()
    if existing_employee is None:
        employee_id = organization_db.propose_employee(
            conn,
            organization_id=organization_id,
            display_name=display_name,
            title=title,
            level=level,
            manager_id=manager_id,
            proposed_by=proposed_by,
            employment_type=employment_type,
            annual_cost_minor=annual_cost_minor,
            currency=str(organization["base_currency"]),
            hired_for_objective_id=str(row["objective_id"]),
        )
    else:
        employee_id = str(existing_employee["id"])

    current_mandate = organization_db.get_current_mandate(conn, employee_id)
    if current_mandate is None:
        organization_db.create_mandate(
            conn,
            employee_id,
            purpose=str(mandate.get("purpose") or title),
            responsibilities=list(mandate.get("responsibilities") or []),
            decision_rights=list(mandate.get("decision_rights") or []),
            prohibited_actions=list(mandate.get("prohibited_actions") or []),
            capabilities=list(mandate.get("capabilities") or []),
            systems=list(mandate.get("systems") or []),
            kpis=list(mandate.get("kpis") or []),
            escalation=dict(mandate.get("escalation") or {}),
            toolsets=list(mandate.get("toolsets") or []),
            skills=list(mandate.get("skills") or []),
            created_by=actor,
            budget_minor=int(mandate.get("budget_minor") or 0),
            expires_at=(
                mandate_expires_at
                if isinstance(mandate_expires_at, int)
                else None
            ),
        )
    employee = organization_db.get_employee_record(conn, employee_id)
    if employee["status"] == "proposed":
        organization_db.transition_employee(
            conn, employee_id, "approved", actor=actor
        )
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO hiring_engagements (
                 decision_id,employee_id,organization_id,employment_class,
                 materialized_by,created_at
               ) VALUES (?,?,?,?,?,?)""",
            (
                decision_id,
                employee_id,
                organization_id,
                employment_class,
                actor,
                int(time.time()),
            ),
        )
    return employee_id


def default_hiring_policy() -> dict[str, Any]:
    return {
        "solo_founder": True,
        "minimum_backlog": 5,
        "minimum_sustained_cycles": 3,
        "minimum_blocked_objectives": 1,
        "allow_separation_of_duty_hires": True,
        "require_linked_objective": True,
        "max_direct_reports_per_manager": 7,
        "allow_contractors": True,
        "fte_duration_threshold_cycles": 12,
        "max_contract_duration_cycles": 12,
    }


def evaluate_hiring_case(
    *,
    case: Mapping[str, Any],
    organization: Mapping[str, Any],
    current_headcount: int,
    current_payroll_minor: int,
    policy: Mapping[str, Any],
) -> HiringDecision:
    """Return whether evidence warrants expanding beyond the current staff."""
    linked_objective_id = str(case.get("objective_id", "")).strip()
    if policy.get("require_linked_objective", True) and not linked_objective_id:
        return HiringDecision("deny", "hire is not linked to an accepted objective")

    annual_cost = case.get("annual_cost_minor", 0)
    if not isinstance(annual_cost, int) or annual_cost < 0:
        return HiringDecision("deny", "annual employee cost must be non-negative")
    headcount_limit = organization.get("headcount_limit")
    if headcount_limit is not None and current_headcount + 1 > int(headcount_limit):
        return HiringDecision("deny", "hire would exceed the headcount charter")
    payroll_limit = organization.get("payroll_budget_minor")
    if (
        payroll_limit is not None
        and current_payroll_minor + annual_cost > int(payroll_limit)
    ):
        return HiringDecision("deny", "hire would exceed the payroll charter")

    separation_required = bool(case.get("separation_of_duty_required", False))
    if separation_required:
        if not policy.get("allow_separation_of_duty_hires", True):
            return HiringDecision(
                "defer", "separation-of-duty hiring is not authorized by policy"
            )
        duty = str(case.get("separation_duty", "")).strip()
        if not duty:
            return HiringDecision(
                "deny", "separation-of-duty case must identify the independent duty"
            )
        employment_class = _classify_employment(case=case, policy=policy)
        if employment_class.verdict != "hire":
            return employment_class
        return HiringDecision(
            "hire",
            "independent authority is required for separation of duties",
            employment_class.employment_class,
            {
                "separation_duty": duty,
                "objective_id": linked_objective_id,
                **employment_class.evidence,
            },
        )

    missing_capability = str(case.get("missing_capability", "")).strip()
    blocked_objectives = int(case.get("blocked_objectives", 0) or 0)
    capability_cycles = int(case.get("capability_gap_cycles", 0) or 0)
    if (
        missing_capability
        and blocked_objectives >= int(policy.get("minimum_blocked_objectives", 1))
        and capability_cycles >= int(policy.get("minimum_sustained_cycles", 3))
    ):
        employment_class = _classify_employment(case=case, policy=policy)
        if employment_class.verdict != "hire":
            return employment_class
        return HiringDecision(
            "hire",
            "a sustained capability gap is blocking accepted objectives",
            employment_class.employment_class,
            {
                "missing_capability": missing_capability,
                "blocked_objectives": blocked_objectives,
                "sustained_cycles": capability_cycles,
                **employment_class.evidence,
            },
        )

    backlog = int(case.get("qualified_backlog", 0) or 0)
    capacity_cycles = int(case.get("capacity_pressure_cycles", 0) or 0)
    ceo_utilization = float(case.get("ceo_utilization", 0.0) or 0.0)
    if (
        backlog >= int(policy.get("minimum_backlog", 5))
        and capacity_cycles >= int(policy.get("minimum_sustained_cycles", 3))
        and ceo_utilization >= 1.0
    ):
        employment_class = _classify_employment(case=case, policy=policy)
        if employment_class.verdict != "hire":
            return employment_class
        return HiringDecision(
            "hire",
            "sustained qualified workload exceeds solo-founder capacity",
            employment_class.employment_class,
            {
                "qualified_backlog": backlog,
                "sustained_cycles": capacity_cycles,
                "ceo_utilization": ceo_utilization,
                **employment_class.evidence,
            },
        )

    return HiringDecision(
        "defer",
        "no sustained staffing requirement is proven; CEO remains solo founder",
        evidence={
            "qualified_backlog": backlog,
            "capacity_pressure_cycles": capacity_cycles,
            "blocked_objectives": blocked_objectives,
            "capability_gap_cycles": capability_cycles,
        },
    )


def _classify_employment(
    *,
    case: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> HiringDecision:
    """Choose contractor vs FTE from the shape and duration of the need."""
    duration = int(case.get("expected_duration_cycles", 0) or 0)
    recurring = bool(case.get("recurring_need", False))
    continuous_owner = bool(case.get("continuous_ownership", False))
    strategic_core = bool(case.get("strategic_core", False))
    standing_privilege = bool(case.get("standing_privileged_access", False))
    scoped_deliverable = str(case.get("scoped_deliverable", "")).strip()

    fte_threshold = int(policy.get("fte_duration_threshold_cycles", 12))
    if (
        recurring
        or continuous_owner
        or strategic_core
        or standing_privilege
        or duration >= fte_threshold
    ):
        return HiringDecision(
            "hire",
            "the role requires durable organizational ownership",
            "fte",
            {
                "employment_basis": "durable",
                "expected_duration_cycles": duration,
                "recurring_need": recurring,
                "continuous_ownership": continuous_owner,
                "strategic_core": strategic_core,
                "standing_privileged_access": standing_privilege,
            },
        )

    if not policy.get("allow_contractors", True):
        return HiringDecision(
            "defer",
            "the need is temporary but the charter does not authorize contractors",
        )
    max_contract = int(policy.get("max_contract_duration_cycles", 12))
    if scoped_deliverable and 0 < duration <= max_contract:
        return HiringDecision(
            "hire",
            "a scoped, time-bounded deliverable warrants a contractor",
            "contractor",
            {
                "employment_basis": "time_bounded",
                "expected_duration_cycles": duration,
                "scoped_deliverable": scoped_deliverable,
            },
        )
    return HiringDecision(
        "defer",
        "staffing need is plausible but contractor versus FTE is not evidenced",
    )
