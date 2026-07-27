"""Authoritative lifecycle for customer, vendor, contractual, and internal promises."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional


KINDS = frozenset(
    {
        "customer_delivery",
        "service_level",
        "vendor",
        "contractual",
        "renewal",
        "financial",
        "internal",
    }
)
TERMINAL_STATUSES = frozenset({"fulfilled", "cancelled", "superseded"})

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS business_commitments (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, objective_id TEXT NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
    counterparty_type TEXT NOT NULL, counterparty_reference TEXT NOT NULL,
    source_system TEXT NOT NULL, source_reference TEXT NOT NULL,
    due_at INTEGER NOT NULL, grace_seconds INTEGER NOT NULL,
    required_verifier TEXT NOT NULL, financial_exposure_minor INTEGER NOT NULL,
    currency TEXT, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
    fulfilled_at INTEGER, fulfilment_verification_id TEXT,
    cancelled_at INTEGER, cancelled_by TEXT, cancellation_reason TEXT,
    supersedes_id TEXT,
    UNIQUE(organization_id,idempotency_key),
    FOREIGN KEY(objective_id) REFERENCES objectives(id),
    FOREIGN KEY(supersedes_id) REFERENCES business_commitments(id),
    FOREIGN KEY(fulfilment_verification_id) REFERENCES verification_records(id)
);
CREATE TABLE IF NOT EXISTS business_commitment_events (
    id TEXT PRIMARY KEY, commitment_id TEXT NOT NULL, event_type TEXT NOT NULL,
    actor TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at INTEGER NOT NULL,
    FOREIGN KEY(commitment_id) REFERENCES business_commitments(id)
);
CREATE INDEX IF NOT EXISTS idx_business_commitments_due
    ON business_commitments(organization_id,status,due_at,id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_business_commitment_fulfillment_evidence
    ON business_commitments(fulfilment_verification_id)
    WHERE fulfilment_verification_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS business_commitments_contract_immutable
BEFORE UPDATE ON business_commitments
WHEN OLD.organization_id IS NOT NEW.organization_id
  OR OLD.objective_id IS NOT NEW.objective_id
  OR OLD.kind IS NOT NEW.kind
  OR OLD.title IS NOT NEW.title
  OR OLD.description IS NOT NEW.description
  OR OLD.counterparty_type IS NOT NEW.counterparty_type
  OR OLD.counterparty_reference IS NOT NEW.counterparty_reference
  OR OLD.source_system IS NOT NEW.source_system
  OR OLD.source_reference IS NOT NEW.source_reference
  OR OLD.due_at IS NOT NEW.due_at
  OR OLD.grace_seconds IS NOT NEW.grace_seconds
  OR OLD.required_verifier IS NOT NEW.required_verifier
  OR OLD.financial_exposure_minor IS NOT NEW.financial_exposure_minor
  OR OLD.currency IS NOT NEW.currency
  OR OLD.idempotency_key IS NOT NEW.idempotency_key
  OR OLD.contract_sha256 IS NOT NEW.contract_sha256
  OR OLD.created_by IS NOT NEW.created_by
  OR OLD.created_at IS NOT NEW.created_at
  OR OLD.supersedes_id IS NOT NEW.supersedes_id
BEGIN SELECT RAISE(ABORT, 'business commitment contract is immutable'); END;
CREATE TRIGGER IF NOT EXISTS business_commitments_immutable_delete
BEFORE DELETE ON business_commitments
BEGIN SELECT RAISE(ABORT, 'business commitments cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS business_commitments_status_guard
BEFORE UPDATE OF status ON business_commitments
WHEN NOT (
  OLD.status = NEW.status
  OR (OLD.status='active' AND NEW.status IN (
    'fulfilled','cancelled','superseded','breached'
  ))
  OR (OLD.status='breached' AND NEW.status IN (
    'fulfilled','cancelled','superseded'
  ))
)
BEGIN SELECT RAISE(ABORT, 'invalid business commitment transition'); END;
CREATE TRIGGER IF NOT EXISTS business_commitments_fulfillment_guard
BEFORE UPDATE ON business_commitments
WHEN NEW.status='fulfilled' AND (
  NEW.fulfilled_at IS NULL OR NEW.fulfilment_verification_id IS NULL
  OR NOT EXISTS (
    SELECT 1 FROM verification_records v
     WHERE v.id=NEW.fulfilment_verification_id
       AND v.objective_id=NEW.objective_id
       AND v.verdict='pass'
       AND v.method=NEW.required_verifier
       AND v.created_at>=NEW.created_at
  )
)
BEGIN SELECT RAISE(ABORT, 'commitment fulfillment lacks matching evidence'); END;
CREATE TRIGGER IF NOT EXISTS business_commitments_cancellation_guard
BEFORE UPDATE ON business_commitments
WHEN NEW.status='cancelled' AND (
  NEW.cancelled_at IS NULL OR length(trim(COALESCE(NEW.cancelled_by,'')))=0
  OR length(trim(COALESCE(NEW.cancellation_reason,'')))=0
)
BEGIN SELECT RAISE(ABORT, 'commitment cancellation lacks authority evidence'); END;
CREATE TRIGGER IF NOT EXISTS business_commitments_resolution_immutable
BEFORE UPDATE ON business_commitments
WHEN (OLD.fulfilled_at IS NOT NULL AND OLD.fulfilled_at IS NOT NEW.fulfilled_at)
  OR (OLD.fulfilment_verification_id IS NOT NULL
      AND OLD.fulfilment_verification_id IS NOT NEW.fulfilment_verification_id)
  OR (OLD.cancelled_at IS NOT NULL AND OLD.cancelled_at IS NOT NEW.cancelled_at)
  OR (OLD.cancelled_by IS NOT NULL AND OLD.cancelled_by IS NOT NEW.cancelled_by)
  OR (OLD.cancellation_reason IS NOT NULL
      AND OLD.cancellation_reason IS NOT NEW.cancellation_reason)
BEGIN SELECT RAISE(ABORT, 'commitment resolution evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS business_commitment_events_immutable_update
BEFORE UPDATE ON business_commitment_events
BEGIN SELECT RAISE(ABORT, 'business commitment events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS business_commitment_events_immutable_delete
BEFORE DELETE ON business_commitment_events
BEGIN SELECT RAISE(ABORT, 'business commitment events are immutable'); END;
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _event(
    conn: sqlite3.Connection,
    commitment_id: str,
    event_type: str,
    actor: str,
    evidence: Mapping[str, Any],
    *,
    now: Optional[int] = None,
) -> str:
    event_id = f"commitment_event_{uuid.uuid4().hex}"
    conn.execute(
        """INSERT INTO business_commitment_events
           (id,commitment_id,event_type,actor,evidence_json,created_at)
           VALUES (?,?,?,?,?,?)""",
        (
            event_id,
            commitment_id,
            event_type,
            actor,
            _json(dict(evidence)),
            int(time.time()) if now is None else int(now),
        ),
    )
    return event_id


def create_commitment(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    objective_id: str,
    kind: str,
    title: str,
    description: str,
    counterparty_type: str,
    counterparty_reference: str,
    source_system: str,
    source_reference: str,
    due_at: int,
    grace_seconds: int,
    required_verifier: str,
    financial_exposure_minor: int,
    currency: Optional[str],
    idempotency_key: str,
    created_by: str,
    supersedes_id: Optional[str] = None,
    now: Optional[int] = None,
) -> tuple[str, bool]:
    ensure_schema(conn)
    ts = int(time.time()) if now is None else int(now)
    if kind not in KINDS:
        raise ValueError("unsupported business commitment kind")
    required = {
        "title": title,
        "description": description,
        "counterparty_type": counterparty_type,
        "counterparty_reference": counterparty_reference,
        "source_system": source_system,
        "source_reference": source_reference,
        "required_verifier": required_verifier,
        "idempotency_key": idempotency_key,
        "created_by": created_by,
    }
    if any(not str(value).strip() for value in required.values()):
        raise ValueError("commitment contract fields must not be empty")
    if due_at <= ts or grace_seconds < 0 or financial_exposure_minor < 0:
        raise ValueError("commitment timing or financial exposure is invalid")
    if financial_exposure_minor and not currency:
        raise ValueError("financial commitments require a currency")
    objective = conn.execute(
        "SELECT organization_id,status FROM objectives WHERE id=?", (objective_id,)
    ).fetchone()
    if (
        objective is None
        or str(objective["organization_id"]) != organization_id
        or str(objective["status"])
        in {"closed", "cancelled", "expired", "abandoned", "superseded"}
    ):
        raise ValueError("commitment requires an active same-organization objective")
    if supersedes_id is not None:
        prior = conn.execute(
            """SELECT organization_id,status FROM business_commitments WHERE id=?""",
            (supersedes_id,),
        ).fetchone()
        if (
            prior is None
            or str(prior["organization_id"]) != organization_id
            or str(prior["status"]) not in {"active", "breached"}
        ):
            raise ValueError("superseded commitment is not active in this organization")
    contract = {
        "organization_id": organization_id,
        "objective_id": objective_id,
        "kind": kind,
        "title": title.strip(),
        "description": description.strip(),
        "counterparty_type": counterparty_type.strip(),
        "counterparty_reference": counterparty_reference.strip(),
        "source_system": source_system.strip(),
        "source_reference": source_reference.strip(),
        "due_at": int(due_at),
        "grace_seconds": int(grace_seconds),
        "required_verifier": required_verifier.strip(),
        "financial_exposure_minor": int(financial_exposure_minor),
        "currency": currency.upper() if currency else None,
        "idempotency_key": idempotency_key.strip(),
        "created_by": created_by.strip(),
        "supersedes_id": supersedes_id,
    }
    digest = hashlib.sha256(_json(contract).encode("utf-8")).hexdigest()
    existing = conn.execute(
        """SELECT id,contract_sha256 FROM business_commitments
           WHERE organization_id=? AND idempotency_key=?""",
        (organization_id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if str(existing["contract_sha256"]) != digest:
            raise ValueError("commitment idempotency key was reused with different terms")
        return str(existing["id"]), False
    commitment_id = f"commitment_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO business_commitments
               (id,organization_id,objective_id,kind,title,description,
                counterparty_type,counterparty_reference,source_system,
                source_reference,due_at,grace_seconds,required_verifier,
                financial_exposure_minor,currency,status,idempotency_key,
                contract_sha256,created_by,created_at,supersedes_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                commitment_id,
                contract["organization_id"],
                contract["objective_id"],
                contract["kind"],
                contract["title"],
                contract["description"],
                contract["counterparty_type"],
                contract["counterparty_reference"],
                contract["source_system"],
                contract["source_reference"],
                contract["due_at"],
                contract["grace_seconds"],
                contract["required_verifier"],
                contract["financial_exposure_minor"],
                contract["currency"],
                "active",
                contract["idempotency_key"],
                digest,
                contract["created_by"],
                ts,
                supersedes_id,
            ),
        )
        _event(
            conn,
            commitment_id,
            "created",
            created_by,
            {"contract_sha256": digest, "source_reference": source_reference},
            now=ts,
        )
        if supersedes_id is not None:
            conn.execute(
                """UPDATE business_commitments SET status='superseded'
                   WHERE id=? AND status IN ('active','breached')""",
                (supersedes_id,),
            )
            _event(
                conn,
                supersedes_id,
                "superseded",
                created_by,
                {"successor_commitment_id": commitment_id},
                now=ts,
            )
    return commitment_id, True


def cancel_by_creation_key(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    idempotency_key: str,
    actor: str,
    reason: str,
    now: Optional[int] = None,
) -> str:
    ensure_schema(conn)
    if not actor.strip() or not reason.strip():
        raise ValueError("commitment cancellation requires actor and reason")
    row = conn.execute(
        """SELECT id,status FROM business_commitments
           WHERE organization_id=? AND idempotency_key=?""",
        (organization_id, idempotency_key),
    ).fetchone()
    if row is None:
        raise KeyError("commitment not found for creation key")
    if row["status"] == "cancelled":
        return str(row["id"])
    if row["status"] not in {"active", "breached"}:
        raise ValueError("terminal commitment cannot be cancelled")
    ts = int(time.time()) if now is None else int(now)
    with conn:
        conn.execute(
            """UPDATE business_commitments SET status='cancelled',cancelled_at=?,
               cancelled_by=?,cancellation_reason=? WHERE id=?""",
            (ts, actor, reason.strip(), row["id"]),
        )
        _event(
            conn,
            str(row["id"]),
            "cancelled",
            actor,
            {"reason": reason.strip()},
            now=ts,
        )
    return str(row["id"])


def fulfill_commitment(
    conn: sqlite3.Connection,
    *,
    commitment_id: str,
    verification_id: str,
    actor: str,
    now: Optional[int] = None,
) -> str:
    """Fulfil only from a pre-existing passing independent verification record."""
    ensure_schema(conn)
    commitment = conn.execute(
        "SELECT * FROM business_commitments WHERE id=?", (commitment_id,)
    ).fetchone()
    if commitment is None:
        raise KeyError("business commitment not found")
    if commitment["status"] == "fulfilled":
        if commitment["fulfilment_verification_id"] != verification_id:
            raise ValueError("commitment was fulfilled by different evidence")
        return commitment_id
    if commitment["status"] not in {"active", "breached"}:
        raise ValueError("terminal commitment cannot be fulfilled")
    verification = conn.execute(
        """SELECT * FROM verification_records WHERE id=?""", (verification_id,)
    ).fetchone()
    if (
        verification is None
        or str(verification["objective_id"]) != str(commitment["objective_id"])
        or str(verification["verdict"]) != "pass"
        or str(verification["method"]) != str(commitment["required_verifier"])
        or int(verification["created_at"]) < int(commitment["created_at"])
    ):
        raise ValueError("fulfillment requires matching passing independent evidence")
    used = conn.execute(
        """SELECT id FROM business_commitments
           WHERE fulfilment_verification_id=? AND id<>?""",
        (verification_id, commitment_id),
    ).fetchone()
    if used is not None:
        raise ValueError("fulfillment evidence is already bound to another commitment")
    ts = int(time.time()) if now is None else int(now)
    with conn:
        conn.execute(
            """UPDATE business_commitments SET status='fulfilled',fulfilled_at=?,
               fulfilment_verification_id=? WHERE id=?""",
            (ts, verification_id, commitment_id),
        )
        _event(
            conn,
            commitment_id,
            "fulfilled",
            actor,
            {
                "verification_id": verification_id,
                "evidence_sha256": str(verification["evidence_sha256"]),
                "late": ts > int(commitment["due_at"]),
            },
            now=ts,
        )
    return commitment_id


def dispatch_due(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    horizon_seconds: int = 7 * 86400,
    now: Optional[int] = None,
) -> dict[str, int]:
    """Wake owners and deterministically mark breaches without model judgment."""
    ensure_schema(conn)
    from hermes_cli import objectives_db, operational_control

    ts = int(time.time()) if now is None else int(now)
    rows = conn.execute(
        """SELECT c.*,o.status objective_status FROM business_commitments c
           JOIN objectives o ON o.id=c.objective_id
          WHERE c.organization_id=? AND c.status IN ('active','breached')
            AND c.due_at<=?
          ORDER BY c.due_at,c.id""",
        (organization_id, ts + max(0, horizon_seconds)),
    ).fetchall()
    result = {"approaching": 0, "breached": 0, "unowned": 0}
    terminal_objectives = {
        "verified", "closed", "cancelled", "expired", "abandoned", "superseded"
    }
    for row in rows:
        overdue = ts > int(row["due_at"])
        breached = ts > int(row["due_at"]) + int(row["grace_seconds"])
        if breached and row["status"] == "active":
            with conn:
                conn.execute(
                    """UPDATE business_commitments SET status='breached'
                       WHERE id=? AND status='active'""",
                    (row["id"],),
                )
                _event(
                    conn,
                    str(row["id"]),
                    "breached",
                    "control:commitments",
                    {"due_at": int(row["due_at"]), "detected_at": ts},
                    now=ts,
                )
            result["breached"] += 1
        payload = {
            "commitment_id": str(row["id"]),
            "kind": str(row["kind"]),
            "due_at": int(row["due_at"]),
            "overdue": overdue,
            "breached": breached,
            "financial_exposure_minor": int(row["financial_exposure_minor"]),
            "currency": row["currency"],
        }
        if str(row["objective_status"]) in terminal_objectives:
            operational_control.raise_intervention(
                conn,
                organization_id=organization_id,
                category="business_commitment_unowned",
                summary="Active business commitment has no active objective owner",
                context=payload,
                options=[
                    {"id": "assign", "label": "Assign a governed objective"},
                    {"id": "cancel", "label": "Review cancellation authority"},
                    {"id": "manual", "label": "Handle manually"},
                ],
                dedupe_key=f"business-commitment-unowned:{row['id']}",
            )
            result["unowned"] += 1
            continue
        event_type = (
            "commitment.breached"
            if breached
            else "commitment.deadline.approaching"
        )
        phase = "breached" if breached else ("overdue" if overdue else "upcoming")
        objectives_db.enqueue_objective_event(
            conn,
            objective_id=str(row["objective_id"]),
            event_type=event_type,
            payload=payload,
            dedupe_key=f"{event_type}:{row['id']}:{row['due_at']}:{phase}",
        )
        result["approaching"] += 1
    return result


def planning_snapshot(
    conn: sqlite3.Connection, organization_id: str, *, limit: int = 30
) -> dict[str, Any]:
    ensure_schema(conn)
    now = int(time.time())
    rows = conn.execute(
        """SELECT id,objective_id,kind,title,due_at,grace_seconds,
                  required_verifier,financial_exposure_minor,currency,status,
                  created_at
             FROM business_commitments
            WHERE organization_id=? AND status IN ('active','breached')
            ORDER BY CASE status WHEN 'breached' THEN 0 ELSE 1 END,due_at,id
            LIMIT ?""",
        (organization_id, max(1, limit)),
    ).fetchall()
    commitments = []
    for row in rows:
        item = dict(row)
        item["overdue"] = now > int(item["due_at"])
        item["eligible_fulfillment_evidence"] = [
            {
                "verification_id": str(evidence["id"]),
                "method": str(evidence["method"]),
                "action_id": (
                    str(evidence["action_id"]) if evidence["action_id"] else None
                ),
                "evidence_sha256": str(evidence["evidence_sha256"]),
                "created_at": int(evidence["created_at"]),
            }
            for evidence in conn.execute(
                """SELECT v.id,v.method,v.action_id,v.evidence_sha256,v.created_at
                     FROM verification_records v
                    WHERE v.objective_id=? AND v.method=? AND v.verdict='pass'
                      AND v.created_at>=?
                      AND NOT EXISTS (
                        SELECT 1 FROM business_commitments used
                         WHERE used.fulfilment_verification_id=v.id
                      )
                    ORDER BY v.created_at DESC,v.id DESC LIMIT 5""",
                (
                    row["objective_id"],
                    row["required_verifier"],
                    row["created_at"],
                ),
            ).fetchall()
        ]
        commitments.append(item)
    return {
        "open": commitments,
        "open_count": conn.execute(
            """SELECT COUNT(*) FROM business_commitments
               WHERE organization_id=? AND status IN ('active','breached')""",
            (organization_id,),
        ).fetchone()[0],
        "breached_count": conn.execute(
            """SELECT COUNT(*) FROM business_commitments
               WHERE organization_id=? AND status='breached'""",
            (organization_id,),
        ).fetchone()[0],
        "limit": max(1, limit),
        "counterparty_details_included": False,
    }


def verify_contracts(conn: sqlite3.Connection, organization_id: str) -> bool:
    """Recompute immutable terms and resolution evidence for integrity preflight."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM business_commitments WHERE organization_id=?",
        (organization_id,),
    ).fetchall()
    for row in rows:
        contract = {
            "organization_id": str(row["organization_id"]),
            "objective_id": str(row["objective_id"]),
            "kind": str(row["kind"]),
            "title": str(row["title"]),
            "description": str(row["description"]),
            "counterparty_type": str(row["counterparty_type"]),
            "counterparty_reference": str(row["counterparty_reference"]),
            "source_system": str(row["source_system"]),
            "source_reference": str(row["source_reference"]),
            "due_at": int(row["due_at"]),
            "grace_seconds": int(row["grace_seconds"]),
            "required_verifier": str(row["required_verifier"]),
            "financial_exposure_minor": int(row["financial_exposure_minor"]),
            "currency": str(row["currency"]) if row["currency"] else None,
            "idempotency_key": str(row["idempotency_key"]),
            "created_by": str(row["created_by"]),
            "supersedes_id": str(row["supersedes_id"]) if row["supersedes_id"] else None,
        }
        if hashlib.sha256(_json(contract).encode("utf-8")).hexdigest() != str(
            row["contract_sha256"]
        ):
            return False
        if row["status"] == "fulfilled":
            verification = conn.execute(
                """SELECT objective_id,method,verdict FROM verification_records
                   WHERE id=?""",
                (row["fulfilment_verification_id"],),
            ).fetchone()
            if (
                verification is None
                or verification["objective_id"] != row["objective_id"]
                or verification["method"] != row["required_verifier"]
                or verification["verdict"] != "pass"
                or int(verification["created_at"]) < int(row["created_at"])
            ):
                return False
        if row["status"] == "cancelled" and (
            row["cancelled_at"] is None
            or not str(row["cancelled_by"] or "").strip()
            or not str(row["cancellation_reason"] or "").strip()
        ):
            return False
    return True
