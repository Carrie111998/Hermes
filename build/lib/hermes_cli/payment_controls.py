"""Non-custodial instruments and deterministic outbound spend controls."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS payment_instruments (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, provider TEXT NOT NULL,
    provider_instrument_id TEXT NOT NULL, rail_type TEXT NOT NULL,
    currency TEXT NOT NULL, label TEXT NOT NULL, last4 TEXT,
    status TEXT NOT NULL, expires_at INTEGER, created_at INTEGER NOT NULL,
    UNIQUE(provider, provider_instrument_id)
);
CREATE TABLE IF NOT EXISTS spend_controls (
    id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL,
    max_transaction_minor INTEGER NOT NULL, max_daily_minor INTEGER NOT NULL,
    human_threshold_minor INTEGER,
    allowed_merchant_categories_json TEXT NOT NULL,
    allowed_payees_json TEXT NOT NULL,
    effective_from INTEGER NOT NULL, expires_at INTEGER,
    policy_version TEXT NOT NULL, created_at INTEGER NOT NULL,
    FOREIGN KEY(instrument_id) REFERENCES payment_instruments(id)
);
CREATE TABLE IF NOT EXISTS payment_spend_holds (
    id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, action_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
    created_at INTEGER NOT NULL, released_at INTEGER, release_reason TEXT,
    UNIQUE(action_id),
    FOREIGN KEY(instrument_id) REFERENCES payment_instruments(id)
);
CREATE INDEX IF NOT EXISTS idx_payment_spend_holds_velocity
    ON payment_spend_holds(instrument_id,status,created_at);
"""

FORBIDDEN_CREDENTIAL_FIELDS = frozenset(
    {
        "pan", "card_number", "cvv", "cvc", "account_number",
        "routing_number", "private_key", "seed_phrase", "entity_secret",
    }
)


class SpendControlError(PermissionError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if {"payment_instruments", "spend_controls"}.issubset(tables):
            conn.execute(
                """CREATE TABLE IF NOT EXISTS payment_spend_holds (
                    id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL,
                    action_id TEXT NOT NULL, amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL, status TEXT NOT NULL,
                    created_at INTEGER NOT NULL, released_at INTEGER,
                    release_reason TEXT, UNIQUE(action_id),
                    FOREIGN KEY(instrument_id) REFERENCES payment_instruments(id)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_payment_spend_holds_velocity
                   ON payment_spend_holds(instrument_id,status,created_at)"""
            )
            return
    conn.executescript(SCHEMA_SQL)


def register_tokenized_instrument(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    provider: str,
    provider_instrument_id: str,
    rail_type: str,
    currency: str,
    label: str,
    metadata: Optional[Mapping[str, Any]] = None,
    last4: Optional[str] = None,
    expires_at: Optional[int] = None,
) -> str:
    """Store only a provider-side opaque identifier, never raw credentials."""
    lower_fields = {str(key).lower() for key in (metadata or {})}
    if lower_fields & FORBIDDEN_CREDENTIAL_FIELDS:
        raise SpendControlError("raw financial credentials cannot enter Charterforge state")
    if not provider_instrument_id.strip():
        raise ValueError("provider instrument id is required")
    ensure_schema(conn)
    instrument_id = f"instrument_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO payment_instruments
               (id, organization_id, provider, provider_instrument_id, rail_type,
                currency, label, last4, status, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                instrument_id, organization_id, provider, provider_instrument_id,
                rail_type, currency.upper(), label, last4, expires_at, int(time.time()),
            ),
        )
    return instrument_id


def set_spend_controls(
    conn: sqlite3.Connection,
    *,
    instrument_id: str,
    max_transaction_minor: int,
    max_daily_minor: int,
    allowed_merchant_categories: list[str],
    allowed_payees: list[str],
    policy_version: str,
    human_threshold_minor: Optional[int] = None,
    expires_at: Optional[int] = None,
) -> str:
    if min(max_transaction_minor, max_daily_minor) < 0:
        raise ValueError("spend limits cannot be negative")
    ensure_schema(conn)
    control_id = f"spend_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO spend_controls
               (id, instrument_id, max_transaction_minor, max_daily_minor,
                human_threshold_minor, allowed_merchant_categories_json,
                allowed_payees_json, effective_from, expires_at, policy_version,
                created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                control_id, instrument_id, max_transaction_minor, max_daily_minor,
                human_threshold_minor,
                json.dumps(sorted(set(allowed_merchant_categories))),
                json.dumps(sorted(set(allowed_payees))),
                int(time.time()), expires_at, policy_version, int(time.time()),
            ),
        )
    return control_id


def authorize_spend(
    conn: sqlite3.Connection,
    *,
    instrument_id: str,
    provider: str,
    amount_minor: int,
    currency: str,
    merchant_category: str,
    payee_id: str,
    action_id: str,
) -> dict[str, Any]:
    if amount_minor <= 0:
        raise ValueError("payment amount must be positive")
    now = int(time.time())
    ensure_schema(conn)
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        instrument = conn.execute(
            """SELECT * FROM payment_instruments
               WHERE id = ? AND status = 'active'
                 AND (expires_at IS NULL OR expires_at > ?)""",
            (instrument_id, now),
        ).fetchone()
        if instrument is None:
            raise SpendControlError(
                "payment instrument is missing, inactive, or expired"
            )
        if instrument["provider"] != provider or instrument["currency"] != currency.upper():
            raise SpendControlError("instrument does not match provider or currency")
        control = conn.execute(
            """SELECT * FROM spend_controls WHERE instrument_id = ?
                 AND effective_from <= ? AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY effective_from DESC, created_at DESC LIMIT 1""",
            (instrument_id, now, now),
        ).fetchone()
        if control is None:
            raise SpendControlError("instrument has no active spend controls")
        existing_hold = conn.execute(
            """SELECT amount_minor,currency,status FROM payment_spend_holds
               WHERE action_id=?""",
            (action_id,),
        ).fetchone()
        if existing_hold is not None:
            if (
                int(existing_hold["amount_minor"]) != amount_minor
                or str(existing_hold["currency"]) != currency.upper()
            ):
                raise SpendControlError("action already has a different spend hold")
            if str(existing_hold["status"]) == "released":
                raise SpendControlError("action's spend hold was already released")
        else:
            if amount_minor > int(control["max_transaction_minor"]):
                raise SpendControlError("payment exceeds per-transaction limit")
            categories = set(json.loads(control["allowed_merchant_categories_json"]))
            payees = set(json.loads(control["allowed_payees_json"]))
            if categories and merchant_category not in categories:
                raise SpendControlError("merchant category is not allowed")
            if payees and payee_id not in payees:
                raise SpendControlError("payee is not allowed")
            since = now - 86400
            payment_intents_table = conn.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='payment_intents'"""
            ).fetchone()
            daily_total = 0
            if payment_intents_table is not None:
                daily_total = int(
                    conn.execute(
                        """SELECT COALESCE(SUM(amount_minor), 0) AS total
                             FROM payment_intents
                            WHERE direction = 'outgoing' AND status = 'succeeded'
                              AND json_extract(metadata_json, '$.instrument_id') = ?
                              AND updated_at >= ?""",
                        (instrument_id, since),
                    ).fetchone()["total"]
                )
            held = conn.execute(
                """SELECT COALESCE(SUM(amount_minor), 0) AS total
                     FROM payment_spend_holds
                    WHERE instrument_id=? AND status='reserved'""",
                (instrument_id,),
            ).fetchone()
            if daily_total + int(held["total"]) + amount_minor > int(
                control["max_daily_minor"]
            ):
                raise SpendControlError("payment exceeds 24-hour velocity limit")
            threshold = control["human_threshold_minor"]
            if threshold is not None and amount_minor > int(threshold):
                permit = conn.execute(
                    """SELECT p.approval_artifact_id FROM permits p
                       WHERE p.action_id = ? AND p.consumed_at IS NOT NULL
                       ORDER BY p.issued_at DESC LIMIT 1""",
                    (action_id,),
                ).fetchone()
                if permit is None or not permit["approval_artifact_id"]:
                    raise SpendControlError(
                        "payment exceeds the human authorization threshold"
                    )
            conn.execute(
                """INSERT INTO payment_spend_holds
                   (id,instrument_id,action_id,amount_minor,currency,status,created_at)
                   VALUES (?,?,?,?,?,'reserved',?)""",
                (
                    f"spendhold_{uuid.uuid4().hex}", instrument_id, action_id,
                    amount_minor, currency.upper(), now,
                ),
            )
        if started_transaction:
            conn.commit()
    except Exception:
        if started_transaction:
            conn.rollback()
        raise
    return {
        "provider_instrument_id": instrument["provider_instrument_id"],
        "policy_version": control["policy_version"],
        "control_id": control["id"],
    }


def release_spend_hold(
    conn: sqlite3.Connection, action_id: str, *, reason: str
) -> bool:
    """Release a pending outbound hold after a terminal provider result."""
    ensure_schema(conn)
    with conn:
        updated = conn.execute(
            """UPDATE payment_spend_holds
                  SET status='released', released_at=?, release_reason=?
                WHERE action_id=? AND status='reserved'""",
            (int(time.time()), reason, action_id),
        )
    return updated.rowcount == 1


def settle_spend_hold(conn: sqlite3.Connection, action_id: str) -> bool:
    """Mark a hold settled after a provider result is durably recorded."""
    ensure_schema(conn)
    with conn:
        updated = conn.execute(
            """UPDATE payment_spend_holds
                  SET status='settled', released_at=?
                WHERE action_id=? AND status='reserved'""",
            (int(time.time()), action_id),
        )
    return updated.rowcount == 1


def stale_spend_holds(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    grace_seconds: int = 3600,
) -> list[dict[str, Any]]:
    """Return reserved holds whose provider outcome needs advisor reconciliation."""
    if grace_seconds <= 0:
        raise ValueError("spend hold grace period must be positive")
    ensure_schema(conn)
    cutoff = (int(time.time()) if now is None else int(now)) - grace_seconds
    payment_table = conn.execute(
        """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='payment_intents'"""
    ).fetchone()
    query = (
        """SELECT h.*, i.organization_id,
                  p.objective_id AS payment_objective_id
             FROM payment_spend_holds h
             JOIN payment_instruments i ON i.id=h.instrument_id
             LEFT JOIN payment_intents p ON p.action_id=h.action_id
            WHERE h.status='reserved' AND h.created_at<=?
            ORDER BY h.created_at,h.id"""
        if payment_table is not None
        else
        """SELECT h.*, i.organization_id,
                  NULL AS payment_objective_id
             FROM payment_spend_holds h
             JOIN payment_instruments i ON i.id=h.instrument_id
            WHERE h.status='reserved' AND h.created_at<=?
            ORDER BY h.created_at,h.id"""
    )
    rows = conn.execute(query, (cutoff,)).fetchall()
    return [dict(row) for row in rows]
