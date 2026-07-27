"""Budget-aware build, FOSS, or buy decision policy."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS procurement_decisions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    case_json TEXT NOT NULL,
    source_evidence_json TEXT NOT NULL,
    available_budget_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    choice TEXT NOT NULL,
    reason TEXT NOT NULL,
    committed_cost_minor INTEGER NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    evaluated_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS procurement_commitments (
    decision_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    committed_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS procurement_decisions_immutable_update
BEFORE UPDATE ON procurement_decisions
BEGIN SELECT RAISE(ABORT, 'procurement decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS procurement_decisions_immutable_delete
BEFORE DELETE ON procurement_decisions
BEGIN SELECT RAISE(ABORT, 'procurement decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS procurement_commitments_immutable_update
BEFORE UPDATE ON procurement_commitments
BEGIN SELECT RAISE(ABORT, 'procurement commitments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS procurement_commitments_immutable_delete
BEFORE DELETE ON procurement_commitments
BEGIN SELECT RAISE(ABORT, 'procurement commitments are immutable'); END;
"""


@dataclass(frozen=True)
class ProcurementDecision:
    choice: str  # existing | foss | build | buy | defer
    reason: str
    committed_cost_minor: int = 0


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='procurement_decisions'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)


def evaluate_procurement_from_state(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    objective_id: str,
    case: Mapping[str, Any],
    source_evidence: Mapping[str, Any],
    idempotency_key: str,
    evaluated_by: str,
) -> tuple[str, ProcurementDecision]:
    """Evaluate sourcing against unreserved base-currency treasury."""
    from hermes_cli import finance_db

    ensure_schema(conn)
    finance_db.ensure_schema(conn)
    if len(idempotency_key.strip()) < 16:
        raise ValueError("procurement decision requires a high-entropy idempotency key")
    if not source_evidence or not str(source_evidence.get("reference") or ""):
        raise ValueError("procurement decision requires source evidence reference")
    existing = conn.execute(
        "SELECT * FROM procurement_decisions WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["organization_id"]) != organization_id
            or str(existing["objective_id"]) != objective_id
            or str(existing["case_json"]) != _json(dict(case))
            or str(existing["source_evidence_json"]) != _json(dict(source_evidence))
        ):
            raise ValueError(
                "procurement idempotency key was reused with different parameters"
            )
        return str(existing["id"]), ProcurementDecision(
            str(existing["choice"]),
            str(existing["reason"]),
            int(existing["committed_cost_minor"]),
        )
    organization = conn.execute(
        "SELECT base_currency FROM organizations WHERE id=?",
        (organization_id,),
    ).fetchone()
    objective = conn.execute(
        "SELECT status FROM objectives WHERE id=? AND organization_id=?",
        (objective_id, organization_id),
    ).fetchone()
    if organization is None or objective is None:
        raise ValueError("procurement objective is outside the organization")
    if objective["status"] in {
        "proposed", "closed", "cancelled", "expired", "abandoned", "superseded"
    }:
        raise ValueError("procurement objective is not active")
    currency = str(organization["base_currency"])
    account_id = finance_db.operating_account_for_organization(
        conn, organization_id, currency
    )
    if account_id is None:
        raise ValueError("organization has no operating treasury account")
    available = finance_db.available_balance(conn, account_id)
    decision = evaluate_procurement(
        case=case, available_budget_minor=available
    )
    facts = {
        "organization_id": organization_id,
        "objective_id": objective_id,
        "available_budget_minor": available,
        "currency": currency,
        "choice": decision.choice,
        "committed_cost_minor": decision.committed_cost_minor,
        "source_reference": str(source_evidence["reference"]),
    }
    facts_json = _json(facts)
    decision_id = f"procurement_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO procurement_decisions (
                 id,organization_id,objective_id,idempotency_key,case_json,
                 source_evidence_json,available_budget_minor,currency,choice,
                 reason,committed_cost_minor,evidence_sha256,evaluated_by,
                 created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id,
                organization_id,
                objective_id,
                idempotency_key,
                _json(dict(case)),
                _json(dict(source_evidence)),
                available,
                currency,
                decision.choice,
                decision.reason,
                decision.committed_cost_minor,
                hashlib.sha256(facts_json.encode()).hexdigest(),
                evaluated_by,
                int(time.time()),
            ),
        )
    return decision_id, decision


def commit_software_purchase(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    organization_id: str,
    objective_id: str,
    action_id: str,
    amount_minor: int,
    currency: str,
) -> None:
    """Bind one positive buy decision to one exact governed payment action."""
    ensure_schema(conn)
    existing = conn.execute(
        "SELECT * FROM procurement_commitments WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    if existing is not None:
        if (
            existing["action_id"] == action_id
            and int(existing["amount_minor"]) == amount_minor
            and existing["currency"] == currency.upper()
        ):
            return
        raise PermissionError("procurement decision is already committed")
    row = conn.execute(
        """SELECT * FROM procurement_decisions
           WHERE id=? AND organization_id=? AND objective_id=?""",
        (decision_id, organization_id, objective_id),
    ).fetchone()
    if row is None:
        raise PermissionError("procurement decision does not match this action")
    if row["choice"] != "buy":
        raise PermissionError(
            f"procurement decision chose {row['choice']}, not a paid purchase"
        )
    if (
        int(row["committed_cost_minor"]) != amount_minor
        or row["currency"] != currency.upper()
    ):
        raise PermissionError("payment does not match the exact procurement decision")
    with conn:
        conn.execute(
            """INSERT INTO procurement_commitments (
                 decision_id,action_id,organization_id,objective_id,
                 amount_minor,currency,committed_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                decision_id,
                action_id,
                organization_id,
                objective_id,
                amount_minor,
                currency.upper(),
                int(time.time()),
            ),
        )


def evaluate_procurement(
    *,
    case: Mapping[str, Any],
    available_budget_minor: int,
) -> ProcurementDecision:
    if available_budget_minor < 0:
        raise ValueError("available budget cannot be negative")
    if bool(case.get("existing_capability_sufficient", False)):
        return ProcurementDecision("existing", "existing capability fills the gap")

    foss_fit = float(case.get("foss_fit", 0.0) or 0.0)
    foss_cost = int(case.get("foss_integration_cost_minor", 0) or 0)
    foss_risk = str(case.get("foss_risk", "low"))
    if (
        foss_fit >= 0.75
        and foss_cost <= available_budget_minor
        and foss_risk in {"low", "medium"}
    ):
        return ProcurementDecision(
            "foss",
            "a suitable FOSS solution fills the gap within budget",
            foss_cost,
        )

    build_cost = int(case.get("build_cost_minor", 0) or 0)
    build_feasible = bool(case.get("build_feasible", True))
    if build_feasible and build_cost <= available_budget_minor:
        return ProcurementDecision(
            "build",
            "internal build is feasible and preferred at the current capital stage",
            build_cost,
        )

    paid_cost = int(case.get("paid_cost_minor", 0) or 0)
    paid_required = bool(case.get("paid_required", False))
    roi = float(case.get("paid_expected_roi", 0.0) or 0.0)
    persistent_need = bool(case.get("persistent_need", False))
    if (
        paid_required
        and persistent_need
        and roi >= 1.0
        and paid_cost <= available_budget_minor
    ):
        return ProcurementDecision(
            "buy",
            "paid product is warranted after existing, FOSS, and build paths failed",
            paid_cost,
        )
    return ProcurementDecision(
        "defer",
        "no admissible option fits the available budget and procurement policy",
    )
