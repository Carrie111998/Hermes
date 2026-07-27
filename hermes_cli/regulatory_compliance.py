"""Evidence-based regulatory applicability and action-time compliance gates."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Mapping


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS compliance_regimes (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, authority TEXT NOT NULL,
    source_url TEXT NOT NULL, kind TEXT NOT NULL, triggers_json TEXT NOT NULL,
    status TEXT NOT NULL, reviewed_at INTEGER NOT NULL, review_due_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS compliance_applicability (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, regime_id TEXT NOT NULL,
    verdict TEXT NOT NULL, rationale TEXT NOT NULL, evidence_json TEXT NOT NULL,
    assessed_by TEXT NOT NULL, assessed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    UNIQUE(organization_id, regime_id, assessed_at),
    FOREIGN KEY(regime_id) REFERENCES compliance_regimes(id)
);
CREATE TABLE IF NOT EXISTS compliance_obligations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, regime_id TEXT NOT NULL,
    name TEXT NOT NULL, action_tags_json TEXT NOT NULL, required_control TEXT NOT NULL,
    effective_from INTEGER NOT NULL, effective_to INTEGER,
    evidence_json TEXT NOT NULL, status TEXT NOT NULL,
    FOREIGN KEY(regime_id) REFERENCES compliance_regimes(id)
);
CREATE TABLE IF NOT EXISTS compliance_control_evidence (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, control_name TEXT NOT NULL,
    verifier TEXT NOT NULL, verdict TEXT NOT NULL, evidence_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL, verified_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS compliance_applicability_immutable_update
BEFORE UPDATE ON compliance_applicability
BEGIN SELECT RAISE(ABORT, 'compliance applicability is immutable'); END;
CREATE TRIGGER IF NOT EXISTS compliance_applicability_immutable_delete
BEFORE DELETE ON compliance_applicability
BEGIN SELECT RAISE(ABORT, 'compliance applicability is immutable'); END;
CREATE TRIGGER IF NOT EXISTS compliance_obligations_immutable_update
BEFORE UPDATE ON compliance_obligations
BEGIN SELECT RAISE(ABORT, 'compliance obligations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS compliance_obligations_immutable_delete
BEFORE DELETE ON compliance_obligations
BEGIN SELECT RAISE(ABORT, 'compliance obligations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS compliance_control_evidence_immutable_update
BEFORE UPDATE ON compliance_control_evidence
BEGIN SELECT RAISE(ABORT, 'compliance control evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS compliance_control_evidence_immutable_delete
BEFORE DELETE ON compliance_control_evidence
BEGIN SELECT RAISE(ABORT, 'compliance control evidence is immutable'); END;
"""

STARTER_REGIMES = (
    {
        "id": "can_spam",
        "name": "CAN-SPAM Act",
        "authority": "US Federal Trade Commission",
        "source_url": "https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business",
        "kind": "law",
        "triggers": {"jurisdictions": ["US"], "activities": ["commercial_email"]},
    },
    {
        "id": "casl",
        "name": "Canada Anti-Spam Legislation",
        "authority": "CRTC",
        "source_url": "https://crtc.gc.ca/eng/internet/anti/reg.htm",
        "kind": "law",
        "triggers": {
            "jurisdictions": ["CA"],
            "activities": ["commercial_email", "commercial_electronic_message"],
        },
    },
    {
        "id": "gdpr",
        "name": "General Data Protection Regulation",
        "authority": "European Union",
        "source_url": "https://eur-lex.europa.eu/eli/reg/2016/679/oj",
        "kind": "law",
        "triggers": {
            "jurisdictions": ["EU", "EEA"],
            "data_classes": ["personal_data", "special_category_data"],
        },
    },
    {
        "id": "eu_ai_act",
        "name": "EU Artificial Intelligence Act",
        "authority": "European Commission",
        "source_url": "https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai",
        "kind": "law",
        "triggers": {
            "jurisdictions": ["EU", "EEA"],
            "activities": ["provide_ai_system", "deploy_ai_system"],
        },
    },
    {
        "id": "sox",
        "name": "Sarbanes-Oxley Act",
        "authority": "US Securities and Exchange Commission",
        "source_url": "https://www.sec.gov/files/rules/final/33-8238.htm",
        "kind": "law",
        "triggers": {
            "jurisdictions": ["US"],
            "entity_attributes": ["exchange_act_issuer"],
            "activities": ["financial_reporting"],
        },
    },
    {
        "id": "pci_dss",
        "name": "PCI DSS",
        "authority": "PCI Security Standards Council",
        "source_url": "https://www.pcisecuritystandards.org/",
        "kind": "industry_standard",
        "triggers": {"data_classes": ["cardholder_data", "authentication_data"]},
    },
    {
        "id": "pipeda",
        "name": "PIPEDA",
        "authority": "Office of the Privacy Commissioner of Canada",
        "source_url": "https://www.priv.gc.ca/en/privacy-topics/privacy-laws-in-canada/the-personal-information-protection-and-electronic-documents-act-pipeda/",
        "kind": "law",
        "triggers": {
            "jurisdictions": ["CA"],
            "data_classes": ["personal_data"],
            "activities": ["commercial_activity"],
        },
    },
    {
        "id": "rpaa",
        "name": "Retail Payment Activities Act",
        "authority": "Bank of Canada",
        "source_url": "https://www.bankofcanada.ca/regulatory-oversight/retail-payments/",
        "kind": "law",
        "triggers": {"jurisdictions": ["CA"], "activities": ["payment_service"]},
    },
    {
        "id": "fintrac_msb",
        "name": "FINTRAC MSB obligations",
        "authority": "FINTRAC",
        "source_url": "https://fintrac-canafe.canada.ca/msb-esm/msb-eng",
        "kind": "law",
        "triggers": {"jurisdictions": ["CA"], "activities": ["money_service"]},
    },
)


class ComplianceGateError(PermissionError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def seed_starter_regimes(conn: sqlite3.Connection, *, review_interval_days: int = 30) -> None:
    ensure_schema(conn)
    now = int(time.time())
    with conn:
        for regime in STARTER_REGIMES:
            conn.execute(
                """INSERT INTO compliance_regimes
                   (id, name, authority, source_url, kind, triggers_json,
                    status, reviewed_at, review_due_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (
                    regime["id"], regime["name"], regime["authority"],
                    regime["source_url"], regime["kind"], _json(regime["triggers"]),
                    now, now + review_interval_days * 86400,
                ),
            )


def _intersects(left: Any, right: Any) -> bool:
    return bool(set(left or []) & set(right or []))


def relevant_regimes(
    conn: sqlite3.Connection, context: Mapping[str, Any]
) -> list[dict[str, Any]]:
    seed_starter_regimes(conn)
    relevant = []
    for row in conn.execute(
        "SELECT * FROM compliance_regimes WHERE status = 'active'"
    ).fetchall():
        triggers = json.loads(row["triggers_json"])
        dimensions = (
            "jurisdictions", "activities", "data_classes", "entity_attributes"
        )
        matched = all(
            _intersects(triggers[key], context.get(key))
            for key in dimensions
            if triggers.get(key)
        )
        if matched:
            value = dict(row)
            value["triggers"] = triggers
            relevant.append(value)
    return relevant


def assess_applicability(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    regime_id: str,
    verdict: str,
    rationale: str,
    evidence: Any,
    assessed_by: str,
    expires_at: int,
) -> str:
    if verdict not in {"applicable", "not_applicable"}:
        raise ValueError("applicability verdict must be applicable or not_applicable")
    if not rationale or not evidence or expires_at <= int(time.time()):
        raise ComplianceGateError("applicability requires rationale, evidence, and expiry")
    regime = conn.execute(
        "SELECT status FROM compliance_regimes WHERE id=?", (regime_id,)
    ).fetchone()
    if regime is None or str(regime["status"]) != "active":
        raise ComplianceGateError("applicability requires an active known regime")
    assessment_id = f"applicability_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO compliance_applicability
               (id, organization_id, regime_id, verdict, rationale, evidence_json,
                assessed_by, assessed_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                assessment_id, organization_id, regime_id, verdict, rationale,
                _json(evidence), assessed_by, int(time.time()), expires_at,
            ),
        )
    return assessment_id


def record_control_evidence(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    control_name: str,
    verifier: str,
    verdict: str,
    evidence: Any,
    expires_at: int,
) -> str:
    import hashlib

    if verdict not in {"pass", "fail"} or not evidence:
        raise ValueError("control evidence requires pass/fail and evidence")
    if expires_at <= int(time.time()):
        raise ComplianceGateError("control evidence expiry must be in the future")
    evidence_json = _json(evidence)
    evidence_id = f"controlevidence_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO compliance_control_evidence
               (id, organization_id, control_name, verifier, verdict,
                evidence_json, evidence_sha256, verified_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence_id, organization_id, control_name, verifier, verdict,
                evidence_json, hashlib.sha256(evidence_json.encode()).hexdigest(),
                int(time.time()), expires_at,
            ),
        )
    return evidence_id


def register_obligation(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    regime_id: str,
    name: str,
    action_tags: list[str],
    required_control: str,
    effective_from: int,
    evidence: Any,
    effective_to: int | None = None,
) -> str:
    if not name or not required_control or not evidence:
        raise ValueError("obligation requires name, control, and source evidence")
    regime = conn.execute(
        "SELECT status FROM compliance_regimes WHERE id=?", (regime_id,)
    ).fetchone()
    if regime is None or str(regime["status"]) != "active":
        raise ComplianceGateError("obligation requires an active known regime")
    obligation_id = f"obligation_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO compliance_obligations
               (id, organization_id, regime_id, name, action_tags_json,
                required_control, effective_from, effective_to, evidence_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
            (
                obligation_id, organization_id, regime_id, name, _json(action_tags),
                required_control, effective_from, effective_to, _json(evidence),
            ),
        )
    return obligation_id


def authorize_action(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed on unknown applicability, stale sources, or missing controls."""
    now = int(time.time())
    regimes = relevant_regimes(conn, context)
    decisions = []
    for regime in regimes:
        if int(regime["review_due_at"]) <= now:
            raise ComplianceGateError(
                f"{regime['name']} source review is overdue"
            )
        assessment = conn.execute(
            """SELECT * FROM compliance_applicability
               WHERE organization_id = ? AND regime_id = ? AND expires_at > ?
               ORDER BY assessed_at DESC LIMIT 1""",
            (organization_id, regime["id"], now),
        ).fetchone()
        if assessment is None:
            raise ComplianceGateError(
                f"{regime['name']} applicability has not been assessed"
            )
        if assessment["verdict"] == "not_applicable":
            decisions.append({"regime": regime["id"], "verdict": "not_applicable"})
            continue
        obligations = conn.execute(
            """SELECT * FROM compliance_obligations
               WHERE organization_id = ? AND regime_id = ? AND status = 'active'
                 AND effective_from <= ?
                 AND (effective_to IS NULL OR effective_to >= ?)""",
            (organization_id, regime["id"], now, now),
        ).fetchall()
        if not obligations:
            raise ComplianceGateError(
                f"{regime['name']} is applicable but has no mapped obligations"
            )
        for obligation in obligations:
            tags = json.loads(obligation["action_tags_json"])
            if tags and not _intersects(tags, context.get("activities")):
                continue
            evidence = conn.execute(
                """SELECT id FROM compliance_control_evidence
                   WHERE organization_id = ? AND control_name = ?
                     AND verdict = 'pass' AND expires_at > ?
                   ORDER BY verified_at DESC LIMIT 1""",
                (organization_id, obligation["required_control"], now),
            ).fetchone()
            if evidence is None:
                raise ComplianceGateError(
                    f"required control has no current passing evidence: "
                    f"{obligation['required_control']}"
                )
        decisions.append({"regime": regime["id"], "verdict": "applicable"})
    return {"regimes": decisions, "evaluated_at": now}
