"""Jurisdictional payment controls and tamper-evident Know-Your-Agent logs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS compliance_profiles (
    organization_id TEXT PRIMARY KEY, custody_model TEXT NOT NULL,
    legal_entity_type TEXT NOT NULL, home_jurisdiction TEXT NOT NULL,
    terms_version TEXT, configured_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_provider_assessments (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, provider TEXT NOT NULL,
    direction TEXT NOT NULL, jurisdiction TEXT NOT NULL,
    registry_authority TEXT NOT NULL, registry_reference TEXT NOT NULL,
    aml_screening_delegated INTEGER NOT NULL,
    sanctions_screening_delegated INTEGER NOT NULL,
    status TEXT NOT NULL, verified_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(organization_id, provider, direction, jurisdiction, verified_at)
);
CREATE TABLE IF NOT EXISTS kya_events (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    agent_version TEXT NOT NULL, event_type TEXT NOT NULL, action_id TEXT,
    permit_id TEXT, payload_sha256 TEXT NOT NULL, previous_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE, evidence_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class ComplianceError(PermissionError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(kya_events)")
        }
        index = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_kya_sequence'"
        ).fetchone()
        if "sequence" in columns and index is not None:
            return
    conn.executescript(SCHEMA_SQL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(kya_events)")}
    if "sequence" not in columns:
        conn.execute("ALTER TABLE kya_events ADD COLUMN sequence INTEGER")
        conn.execute(
            """UPDATE kya_events SET sequence = (
               SELECT COUNT(*) FROM kya_events prior
               WHERE prior.organization_id = kya_events.organization_id
                 AND (prior.created_at < kya_events.created_at OR
                      (prior.created_at = kya_events.created_at AND prior.id <= kya_events.id))
            )"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_kya_sequence "
            "ON kya_events(organization_id, sequence)"
        )
    else:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_kya_sequence "
            "ON kya_events(organization_id, sequence)"
        )


def configure_profile(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    legal_entity_type: str,
    home_jurisdiction: str,
    custody_model: str = "non_custodial",
    terms_version: Optional[str] = None,
) -> None:
    if custody_model != "non_custodial":
        raise ComplianceError("Charterforge Business OS must remain non-custodial")
    if not legal_entity_type or legal_entity_type == "unconfigured":
        raise ComplianceError("legal entity type must be configured")
    if not home_jurisdiction:
        raise ComplianceError("home jurisdiction must be configured")
    ensure_schema(conn)
    with conn:
        conn.execute(
            """INSERT INTO compliance_profiles
               (organization_id, custody_model, legal_entity_type,
                home_jurisdiction, terms_version, configured_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(organization_id) DO UPDATE SET
                 custody_model=excluded.custody_model,
                 legal_entity_type=excluded.legal_entity_type,
                 home_jurisdiction=excluded.home_jurisdiction,
                 terms_version=excluded.terms_version,
                 configured_at=excluded.configured_at""",
            (
                organization_id, custody_model, legal_entity_type,
                home_jurisdiction, terms_version, int(time.time()),
            ),
        )


def verify_payment_provider(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    provider: str,
    direction: str,
    jurisdiction: str,
    registry_authority: str,
    registry_reference: str,
    aml_screening_delegated: bool,
    sanctions_screening_delegated: bool,
    verified_at: int,
    expires_at: int,
    evidence: Any,
) -> str:
    if direction not in {"inbound", "outbound"}:
        raise ValueError("payment direction must be inbound or outbound")
    now = int(time.time())
    if verified_at > now:
        raise ComplianceError("provider assessment cannot be future-dated")
    if expires_at <= now:
        raise ComplianceError("provider assessment is already expired")
    if expires_at <= verified_at or not evidence:
        raise ComplianceError("provider assessment requires current external evidence")
    assessment_id = f"assessment_{uuid.uuid4().hex}"
    ensure_schema(conn)
    with conn:
        conn.execute(
            """INSERT INTO payment_provider_assessments
               (id, organization_id, provider, direction, jurisdiction,
                registry_authority, registry_reference, aml_screening_delegated,
                sanctions_screening_delegated, status, verified_at, expires_at,
                evidence_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?)""",
            (
                assessment_id, organization_id, provider, direction, jurisdiction,
                registry_authority, registry_reference,
                int(aml_screening_delegated), int(sanctions_screening_delegated),
                verified_at, expires_at, json.dumps(evidence, sort_keys=True),
            ),
        )
    return assessment_id


def authorize_payment_provider(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    provider: str,
    direction: str,
    jurisdiction: str,
) -> str:
    ensure_schema(conn)
    profile = conn.execute(
        "SELECT custody_model FROM compliance_profiles WHERE organization_id = ?",
        (organization_id,),
    ).fetchone()
    if profile is None or profile["custody_model"] != "non_custodial":
        raise ComplianceError("non-custodial compliance profile is not configured")
    row = conn.execute(
        """SELECT * FROM payment_provider_assessments
           WHERE organization_id = ? AND provider = ? AND direction = ?
             AND jurisdiction IN (?, 'GLOBAL') AND status = 'verified'
             AND expires_at > ?
           ORDER BY CASE WHEN jurisdiction = ? THEN 0 ELSE 1 END, verified_at DESC
           LIMIT 1""",
        (
            organization_id, provider, direction, jurisdiction,
            int(time.time()), jurisdiction,
        ),
    ).fetchone()
    if row is None:
        raise ComplianceError(
            "payment provider lacks a current jurisdictional assessment"
        )
    if not row["aml_screening_delegated"] or not row["sanctions_screening_delegated"]:
        raise ComplianceError("provider screening responsibilities are unresolved")
    return str(row["id"])


def append_kya_event(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    agent_id: str,
    agent_version: str,
    event_type: str,
    payload: Any,
    evidence: Any,
    action_id: Optional[str] = None,
    permit_id: Optional[str] = None,
) -> str:
    ensure_schema(conn)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
    previous = conn.execute(
        """SELECT event_hash, sequence FROM kya_events WHERE organization_id = ?
           ORDER BY sequence DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    previous_hash = str(previous["event_hash"]) if previous else None
    sequence = int(previous["sequence"]) + 1 if previous else 1
    event_id = f"kya_{uuid.uuid4().hex}"
    created_at = int(time.time())
    event_hash = hashlib.sha256(
        "|".join(
            [
                previous_hash or "", event_id, organization_id, agent_id,
                agent_version, event_type, action_id or "", permit_id or "",
                payload_hash, str(created_at),
            ]
        ).encode()
    ).hexdigest()
    with conn:
        conn.execute(
            """INSERT INTO kya_events
               (id, organization_id, sequence, agent_id, agent_version, event_type,
                action_id, permit_id, payload_sha256, previous_hash, event_hash,
                evidence_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, organization_id, sequence, agent_id, agent_version, event_type,
                action_id, permit_id, payload_hash, previous_hash, event_hash,
                json.dumps(evidence, sort_keys=True), created_at,
            ),
        )
    return event_id
