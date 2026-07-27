"""Immutable, evidence-bound links between governed decisions and outcomes.

This module deliberately records attribution, not inferred causality.  A link is
created only from an authoritative relationship already present in the business
state (an action verification, provider payment read-back, or controlled
experiment evaluation).  Temporal proximity and planner assertions are never
accepted as evidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outcome_attributions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    outcome_kind TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    attribution_method TEXT NOT NULL,
    evidence_strength TEXT NOT NULL,
    verification_id TEXT,
    value_minor INTEGER,
    currency TEXT,
    verdict TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    UNIQUE(organization_id,outcome_kind,outcome_id),
    FOREIGN KEY(objective_id) REFERENCES objectives(id),
    FOREIGN KEY(verification_id) REFERENCES verification_records(id)
);
CREATE INDEX IF NOT EXISTS idx_outcome_attribution_subject
    ON outcome_attributions(organization_id,subject_kind,subject_id,recorded_at);
CREATE TABLE IF NOT EXISTS outcome_attribution_contradictions (
    id TEXT PRIMARY KEY,
    attribution_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    UNIQUE(attribution_id,source_kind,source_id),
    FOREIGN KEY(attribution_id) REFERENCES outcome_attributions(id)
);
CREATE TRIGGER IF NOT EXISTS outcome_attributions_immutable_update
BEFORE UPDATE ON outcome_attributions
BEGIN SELECT RAISE(ABORT, 'outcome attributions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS outcome_attributions_immutable_delete
BEFORE DELETE ON outcome_attributions
BEGIN SELECT RAISE(ABORT, 'outcome attributions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS outcome_attribution_contradictions_immutable_update
BEFORE UPDATE ON outcome_attribution_contradictions
BEGIN SELECT RAISE(ABORT, 'outcome contradictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS outcome_attribution_contradictions_immutable_delete
BEFORE DELETE ON outcome_attribution_contradictions
BEGIN SELECT RAISE(ABORT, 'outcome contradictions are immutable'); END;
"""

METHOD_STRENGTH = {
    "action_verification": "independently_verified",
    "provider_payment_readback": "direct_provider_link",
    "controlled_experiment": "experimental_evidence",
}


class AttributionError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='outcome_attributions'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)


def _contract(
    *,
    organization_id: str,
    objective_id: str,
    subject_kind: str,
    subject_id: str,
    outcome_kind: str,
    outcome_id: str,
    attribution_method: str,
    evidence_strength: str,
    verification_id: Optional[str],
    value_minor: Optional[int],
    currency: Optional[str],
    verdict: str,
    evidence_sha256: str,
) -> dict[str, Any]:
    return {
        "organization_id": organization_id,
        "objective_id": objective_id,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "outcome_kind": outcome_kind,
        "outcome_id": outcome_id,
        "attribution_method": attribution_method,
        "evidence_strength": evidence_strength,
        "verification_id": verification_id,
        "value_minor": value_minor,
        "currency": currency,
        "verdict": verdict,
        "evidence_sha256": evidence_sha256,
    }


def _record(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    objective_id: str,
    subject_kind: str,
    subject_id: str,
    outcome_kind: str,
    outcome_id: str,
    attribution_method: str,
    verification_id: Optional[str],
    value_minor: Optional[int],
    currency: Optional[str],
    verdict: str,
    evidence: dict[str, Any],
) -> tuple[str, bool]:
    ensure_schema(conn)
    strength = METHOD_STRENGTH.get(attribution_method)
    if strength is None:
        raise AttributionError("unsupported attribution method")
    evidence_json = _json(evidence)
    evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
    contract = _contract(
        organization_id=organization_id,
        objective_id=objective_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        outcome_kind=outcome_kind,
        outcome_id=outcome_id,
        attribution_method=attribution_method,
        evidence_strength=strength,
        verification_id=verification_id,
        value_minor=value_minor,
        currency=currency,
        verdict=verdict,
        evidence_sha256=evidence_hash,
    )
    contract_hash = hashlib.sha256(_json(contract).encode()).hexdigest()
    existing = conn.execute(
        """SELECT id,contract_sha256 FROM outcome_attributions
            WHERE organization_id=? AND outcome_kind=? AND outcome_id=?""",
        (organization_id, outcome_kind, outcome_id),
    ).fetchone()
    if existing is not None:
        if str(existing["contract_sha256"]) != contract_hash:
            raise AttributionError(
                "outcome already has a different definitive attribution"
            )
        return str(existing["id"]), False
    attribution_id = f"attribution_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO outcome_attributions (
                 id,organization_id,objective_id,subject_kind,subject_id,
                 outcome_kind,outcome_id,attribution_method,evidence_strength,
                 verification_id,value_minor,currency,verdict,evidence_json,
                 evidence_sha256,contract_sha256,recorded_by,recorded_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                attribution_id,
                organization_id,
                objective_id,
                subject_kind,
                subject_id,
                outcome_kind,
                outcome_id,
                attribution_method,
                strength,
                verification_id,
                value_minor,
                currency,
                verdict,
                evidence_json,
                evidence_hash,
                contract_hash,
                "control:deterministic-attribution",
                int(time.time()),
            ),
        )
    return attribution_id, True


def link_action_verification(
    conn: sqlite3.Connection, verification_id: str
) -> tuple[str, bool]:
    """Link an action to its independent pass verification."""
    row = conn.execute(
        """SELECT v.*,a.objective_id AS action_objective_id,
                  o.organization_id
             FROM verification_records v
             JOIN candidate_actions a ON a.id=v.action_id
             JOIN objectives o ON o.id=a.objective_id
            WHERE v.id=?""",
        (verification_id,),
    ).fetchone()
    if row is None:
        raise AttributionError("action verification not found")
    if (
        str(row["verdict"]) != "pass"
        or not row["execution_result_id"]
        or str(row["objective_id"]) != str(row["action_objective_id"])
    ):
        raise AttributionError(
            "action attribution requires a passing execution verification"
        )
    return _record(
        conn,
        organization_id=str(row["organization_id"]),
        objective_id=str(row["objective_id"]),
        subject_kind="action",
        subject_id=str(row["action_id"]),
        outcome_kind="verification",
        outcome_id=verification_id,
        attribution_method="action_verification",
        verification_id=verification_id,
        value_minor=None,
        currency=None,
        verdict="observed",
        evidence={
            "verification_evidence_sha256": str(row["evidence_sha256"]),
            "execution_result_id": str(row["execution_result_id"]),
            "method": str(row["method"]),
        },
    )


def link_payment_readback(
    conn: sqlite3.Connection, readback_id: str
) -> tuple[str, bool]:
    """Attribute a settled provider payment to its persisted action/objective."""
    row = conn.execute(
        """SELECT r.*,p.organization_id,p.objective_id,p.action_id,p.direction,
                  p.tax_minor
             FROM payment_provider_readbacks r
             JOIN payment_intents p ON p.id=r.payment_intent_id
            WHERE r.id=?""",
        (readback_id,),
    ).fetchone()
    if row is None:
        raise AttributionError("payment read-back not found")
    if str(row["status"]) != "succeeded" or not row["objective_id"]:
        raise AttributionError(
            "payment attribution requires a succeeded objective-bound read-back"
        )
    subject_kind = "action" if row["action_id"] else "objective"
    subject_id = (
        str(row["action_id"]) if row["action_id"] else str(row["objective_id"])
    )
    value = int(row["amount_minor"])
    if str(row["direction"]) == "incoming":
        value -= int(row["tax_minor"] or 0)
    else:
        value = -value
    evidence_json = str(row["evidence_json"])
    objective = conn.execute(
        "SELECT organization_id FROM objectives WHERE id=?",
        (row["objective_id"],),
    ).fetchone()
    if (
        objective is None
        or str(objective["organization_id"]) != str(row["organization_id"])
    ):
        raise AttributionError("payment objective crosses organization boundary")
    if row["action_id"]:
        action = conn.execute(
            "SELECT objective_id FROM candidate_actions WHERE id=?",
            (row["action_id"],),
        ).fetchone()
        if action is None or str(action["objective_id"]) != str(row["objective_id"]):
            raise AttributionError("payment action does not belong to its objective")
    return _record(
        conn,
        organization_id=str(row["organization_id"]),
        objective_id=str(row["objective_id"]),
        subject_kind=subject_kind,
        subject_id=subject_id,
        outcome_kind="payment_intent",
        outcome_id=str(row["payment_intent_id"]),
        attribution_method="provider_payment_readback",
        verification_id=None,
        value_minor=value,
        currency=str(row["currency"]),
        verdict="settled",
        evidence={
            "provider": str(row["provider"]),
            "provider_reference": str(row["provider_reference"]),
            "provider_evidence_sha256": hashlib.sha256(
                evidence_json.encode()
            ).hexdigest(),
            "payment_intent_id": str(row["payment_intent_id"]),
            "readback_id": readback_id,
            "direction": str(row["direction"]),
        },
    )


def record_contradiction(
    conn: sqlite3.Connection,
    *,
    attribution_id: str,
    source_kind: str,
    source_id: str,
    reason: str,
    evidence: dict[str, Any],
) -> tuple[str, bool]:
    """Append evidence that a previously valid attribution no longer holds."""
    ensure_schema(conn)
    if not reason.strip() or not evidence:
        raise AttributionError("contradiction requires reason and evidence")
    if conn.execute(
        "SELECT 1 FROM outcome_attributions WHERE id=?", (attribution_id,)
    ).fetchone() is None:
        raise AttributionError("attribution not found")
    material = _json(evidence)
    digest = hashlib.sha256(material.encode()).hexdigest()
    existing = conn.execute(
        """SELECT id,evidence_sha256 FROM outcome_attribution_contradictions
            WHERE attribution_id=? AND source_kind=? AND source_id=?""",
        (attribution_id, source_kind, source_id),
    ).fetchone()
    if existing is not None:
        if str(existing["evidence_sha256"]) != digest:
            raise AttributionError(
                "contradiction source was reused with different evidence"
            )
        return str(existing["id"]), False
    contradiction_id = f"contradiction_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO outcome_attribution_contradictions (
                 id,attribution_id,source_kind,source_id,reason,evidence_json,
                 evidence_sha256,recorded_at
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                contradiction_id,
                attribution_id,
                source_kind,
                source_id,
                reason,
                material,
                digest,
                int(time.time()),
            ),
        )
    return contradiction_id, True


def link_experiment_evaluation(
    conn: sqlite3.Connection, evaluation_id: str
) -> tuple[str, bool]:
    """Link a controlled experiment to the observation used for its verdict."""
    row = conn.execute(
        """SELECT ev.*,e.objective_id,o.organization_id,
                  observation.evidence_sha256 AS observation_evidence_sha256
             FROM strategy_experiment_evaluations ev
             JOIN strategy_experiments e ON e.id=ev.experiment_id
             JOIN objectives o ON o.id=e.objective_id
             LEFT JOIN metric_observations observation
               ON observation.id=ev.observation_id
            WHERE ev.id=?""",
        (evaluation_id,),
    ).fetchone()
    if row is None:
        raise AttributionError("experiment evaluation not found")
    if not row["observation_id"] or str(row["verdict"]) == "no_evidence":
        raise AttributionError(
            "experiment attribution requires an evidence-backed evaluation"
        )
    return _record(
        conn,
        organization_id=str(row["organization_id"]),
        objective_id=str(row["objective_id"]),
        subject_kind="experiment",
        subject_id=str(row["experiment_id"]),
        outcome_kind="experiment_evaluation",
        outcome_id=evaluation_id,
        attribution_method="controlled_experiment",
        verification_id=None,
        value_minor=None,
        currency=None,
        verdict=str(row["verdict"]),
        evidence={
            "observation_id": str(row["observation_id"]),
            "observation_evidence_sha256": str(
                row["observation_evidence_sha256"]
            ),
            "evaluation_evidence_sha256": hashlib.sha256(
                str(row["evidence_json"]).encode()
            ).hexdigest(),
        },
    )


def sync_authoritative_links(
    conn: sqlite3.Connection, organization_id: str
) -> dict[str, int]:
    """Materialize every currently provable link, idempotently."""
    from hermes_cli import business_metrics, payments

    business_metrics.ensure_schema(conn)
    payments.ensure_schema(conn)
    ensure_schema(conn)
    counts = {
        "action_verifications": 0,
        "payments": 0,
        "experiments": 0,
        "contradictions": 0,
    }
    verification_ids = [
        str(row["id"])
        for row in conn.execute(
            """SELECT v.id FROM verification_records v
                 JOIN objectives o ON o.id=v.objective_id
                WHERE o.organization_id=? AND v.action_id IS NOT NULL
                  AND v.execution_result_id IS NOT NULL AND v.verdict='pass'
                ORDER BY v.created_at,v.id""",
            (organization_id,),
        ).fetchall()
    ]
    for verification_id in verification_ids:
        _, created = link_action_verification(conn, verification_id)
        counts["action_verifications"] += int(created)
    readback_ids = [
        str(row["id"])
        for row in conn.execute(
            """SELECT r.id FROM payment_provider_readbacks r
                 JOIN payment_intents p ON p.id=r.payment_intent_id
                WHERE p.organization_id=? AND p.objective_id IS NOT NULL
                  AND r.status='succeeded'
                  AND NOT EXISTS (
                    SELECT 1 FROM payment_provider_readbacks earlier
                     WHERE earlier.payment_intent_id=r.payment_intent_id
                       AND earlier.status='succeeded'
                       AND (earlier.observed_at<r.observed_at OR (
                         earlier.observed_at=r.observed_at AND earlier.id<r.id
                       ))
                  )
                ORDER BY r.observed_at,r.id""",
            (organization_id,),
        ).fetchall()
    ]
    for readback_id in readback_ids:
        _, created = link_payment_readback(conn, readback_id)
        counts["payments"] += int(created)
    reversed_rows = conn.execute(
        """SELECT a.id AS attribution_id,r.id AS readback_id,r.status,
                  r.evidence_json,r.observed_at
             FROM outcome_attributions a
             JOIN payment_provider_readbacks r
               ON r.payment_intent_id=a.outcome_id
            WHERE a.organization_id=? AND a.outcome_kind='payment_intent'
              AND r.status<>'succeeded'
              AND r.observed_at>(
                SELECT MIN(success.observed_at)
                  FROM payment_provider_readbacks success
                 WHERE success.payment_intent_id=r.payment_intent_id
                   AND success.status='succeeded'
              )
            ORDER BY r.observed_at,r.id""",
        (organization_id,),
    ).fetchall()
    for row in reversed_rows:
        _, created = record_contradiction(
            conn,
            attribution_id=str(row["attribution_id"]),
            source_kind="payment_provider_readback",
            source_id=str(row["readback_id"]),
            reason=f"provider status changed to {row['status']}",
            evidence={
                "status": str(row["status"]),
                "observed_at": int(row["observed_at"]),
                "provider_evidence_sha256": hashlib.sha256(
                    str(row["evidence_json"]).encode()
                ).hexdigest(),
            },
        )
        counts["contradictions"] += int(created)
    evaluation_ids = [
        str(row["id"])
        for row in conn.execute(
            """SELECT ev.id FROM strategy_experiment_evaluations ev
                 JOIN strategy_experiments e ON e.id=ev.experiment_id
                WHERE e.organization_id=? AND ev.observation_id IS NOT NULL
                  AND ev.verdict<>'no_evidence'
                ORDER BY ev.evaluated_at,ev.id""",
            (organization_id,),
        ).fetchall()
    ]
    for evaluation_id in evaluation_ids:
        _, created = link_experiment_evaluation(conn, evaluation_id)
        counts["experiments"] += int(created)
    return counts


def planning_snapshot(
    conn: sqlite3.Connection, organization_id: str
) -> dict[str, Any]:
    """Return bounded aggregates without party, provider, or raw evidence data."""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT subject_kind,subject_id,attribution_method,evidence_strength,
                  verdict,currency,
                  COUNT(*) AS outcome_count,
                  SUM(CASE WHEN value_minor IS NULL THEN 0 ELSE value_minor END)
                    AS net_value_minor,
                  MAX(recorded_at) AS latest_at
             FROM outcome_attributions attribution
            WHERE organization_id=?
              AND NOT EXISTS (
                SELECT 1 FROM outcome_attribution_contradictions contradiction
                 WHERE contradiction.attribution_id=attribution.id
              )
            GROUP BY subject_kind,subject_id,attribution_method,
                     evidence_strength,verdict,currency
            ORDER BY latest_at DESC,subject_kind,subject_id LIMIT 100""",
        (organization_id,),
    ).fetchall()
    return {
        "basis": "authoritative_links_not_inferred_causality",
        "outcomes": [dict(row) for row in rows],
        "limit": 100,
        "sensitive_data_included": False,
    }


def verify_attributions(conn: sqlite3.Connection, organization_id: str) -> bool:
    """Recompute contracts and validate every referenced source."""
    ensure_schema(conn)
    for row in conn.execute(
        """SELECT * FROM outcome_attributions WHERE organization_id=?
            ORDER BY recorded_at,id""",
        (organization_id,),
    ).fetchall():
        evidence_hash = hashlib.sha256(
            str(row["evidence_json"]).encode()
        ).hexdigest()
        contract = _contract(
            organization_id=str(row["organization_id"]),
            objective_id=str(row["objective_id"]),
            subject_kind=str(row["subject_kind"]),
            subject_id=str(row["subject_id"]),
            outcome_kind=str(row["outcome_kind"]),
            outcome_id=str(row["outcome_id"]),
            attribution_method=str(row["attribution_method"]),
            evidence_strength=str(row["evidence_strength"]),
            verification_id=(
                str(row["verification_id"]) if row["verification_id"] else None
            ),
            value_minor=(
                int(row["value_minor"]) if row["value_minor"] is not None else None
            ),
            currency=str(row["currency"]) if row["currency"] else None,
            verdict=str(row["verdict"]),
            evidence_sha256=evidence_hash,
        )
        if (
            evidence_hash != str(row["evidence_sha256"])
            or hashlib.sha256(_json(contract).encode()).hexdigest()
            != str(row["contract_sha256"])
        ):
            return False
        source_table = {
            "verification": "verification_records",
            "payment_intent": "payment_intents",
            "experiment_evaluation": "strategy_experiment_evaluations",
        }.get(str(row["outcome_kind"]))
        if source_table is None:
            return False
        if conn.execute(
            f"SELECT 1 FROM {source_table} WHERE id=?", (row["outcome_id"],)
        ).fetchone() is None:
            return False
    for row in conn.execute(
        """SELECT contradiction.* FROM outcome_attribution_contradictions contradiction
             JOIN outcome_attributions attribution
               ON attribution.id=contradiction.attribution_id
            WHERE attribution.organization_id=?""",
        (organization_id,),
    ).fetchall():
        if hashlib.sha256(str(row["evidence_json"]).encode()).hexdigest() != str(
            row["evidence_sha256"]
        ):
            return False
        if conn.execute(
            "SELECT 1 FROM payment_provider_readbacks WHERE id=?",
            (row["source_id"],),
        ).fetchone() is None:
            return False
    return True
