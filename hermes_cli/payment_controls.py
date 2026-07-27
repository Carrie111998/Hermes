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
        raise SpendControlError("raw financial credentials cannot enter Hermes state")
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
    now = int(time.time())
    instrument = conn.execute(
        """SELECT * FROM payment_instruments
           WHERE id = ? AND status = 'active'
             AND (expires_at IS NULL OR expires_at > ?)""",
        (instrument_id, now),
    ).fetchone()
    if instrument is None:
        raise SpendControlError("payment instrument is missing, inactive, or expired")
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
    if amount_minor > int(control["max_transaction_minor"]):
        raise SpendControlError("payment exceeds per-transaction limit")
    categories = set(json.loads(control["allowed_merchant_categories_json"]))
    payees = set(json.loads(control["allowed_payees_json"]))
    if categories and merchant_category not in categories:
        raise SpendControlError("merchant category is not allowed")
    if payees and payee_id not in payees:
        raise SpendControlError("payee is not allowed")
    since = now - 86400
    daily = conn.execute(
        """SELECT COALESCE(SUM(amount_minor), 0) AS total FROM payment_intents
           WHERE direction = 'outgoing' AND status = 'succeeded'
             AND json_extract(metadata_json, '$.instrument_id') = ?
             AND updated_at >= ?""",
        (instrument_id, since),
    ).fetchone()
    if int(daily["total"]) + amount_minor > int(control["max_daily_minor"]):
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
            raise SpendControlError("payment exceeds the human authorization threshold")
    return {
        "provider_instrument_id": instrument["provider_instrument_id"],
        "policy_version": control["policy_version"],
        "control_id": control["id"],
    }
