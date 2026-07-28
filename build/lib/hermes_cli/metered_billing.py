"""Usage metering with sub-minor-unit accumulation and invoice reconciliation."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any


MICROMINOR_PER_MINOR = 1_000_000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS billing_meters (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL,
    currency TEXT NOT NULL, unit_price_microminor INTEGER NOT NULL,
    unit_name TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(organization_id, name)
);
CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY, meter_id TEXT NOT NULL, customer_id TEXT NOT NULL,
    quantity INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
    evidence_json TEXT NOT NULL, occurred_at INTEGER NOT NULL,
    billing_run_id TEXT, created_at INTEGER NOT NULL,
    FOREIGN KEY(meter_id) REFERENCES billing_meters(id)
);
CREATE TABLE IF NOT EXISTS billing_state (
    meter_id TEXT NOT NULL, customer_id TEXT NOT NULL,
    carry_microminor INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(meter_id, customer_id)
);
CREATE TABLE IF NOT EXISTS billing_runs (
    id TEXT PRIMARY KEY, meter_id TEXT NOT NULL, customer_id TEXT NOT NULL,
    through_at INTEGER NOT NULL, amount_minor INTEGER NOT NULL,
    carry_microminor INTEGER NOT NULL, usage_event_count INTEGER NOT NULL,
    currency TEXT NOT NULL, created_at INTEGER NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='billing_meters'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)


def create_meter(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    name: str,
    currency: str,
    unit_price_microminor: int,
    unit_name: str,
) -> str:
    if unit_price_microminor <= 0:
        raise ValueError("meter price must be positive")
    ensure_schema(conn)
    meter_id = f"meter_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO billing_meters
               (id, organization_id, name, currency, unit_price_microminor,
                unit_name, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
            (
                meter_id, organization_id, name, currency.upper(),
                unit_price_microminor, unit_name, int(time.time()),
            ),
        )
    return meter_id


def record_usage(
    conn: sqlite3.Connection,
    *,
    meter_id: str,
    customer_id: str,
    quantity: int,
    idempotency_key: str,
    evidence: Any,
    occurred_at: int | None = None,
) -> str:
    if quantity <= 0 or not evidence:
        raise ValueError("usage requires positive quantity and source evidence")
    ensure_schema(conn)
    existing = conn.execute(
        "SELECT * FROM usage_events WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if existing:
        expected_evidence = json.dumps(evidence, sort_keys=True)
        if (
            str(existing["meter_id"]) != meter_id
            or str(existing["customer_id"]) != customer_id
            or int(existing["quantity"]) != quantity
            or str(existing["evidence_json"]) != expected_evidence
            or (
                occurred_at is not None
                and int(existing["occurred_at"]) != int(occurred_at)
            )
        ):
            raise ValueError(
                "usage idempotency key was reused with different event parameters"
            )
        return str(existing["id"])
    meter = conn.execute(
        "SELECT status FROM billing_meters WHERE id = ?", (meter_id,)
    ).fetchone()
    if meter is None or meter["status"] != "active":
        raise ValueError("billing meter is missing or inactive")
    event_id = f"usage_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO usage_events
               (id, meter_id, customer_id, quantity, idempotency_key,
                evidence_json, occurred_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, meter_id, customer_id, quantity, idempotency_key,
                json.dumps(evidence, sort_keys=True), occurred_at or int(time.time()),
                int(time.time()),
            ),
        )
    return event_id


def close_usage_window(
    conn: sqlite3.Connection,
    *,
    meter_id: str,
    customer_id: str,
    through_at: int,
) -> dict[str, Any]:
    """Convert evidenced usage to minor units while preserving sub-cent carry."""
    ensure_schema(conn)
    meter = conn.execute(
        "SELECT * FROM billing_meters WHERE id = ?", (meter_id,)
    ).fetchone()
    if meter is None:
        raise KeyError(f"billing meter not found: {meter_id}")
    events = conn.execute(
        """SELECT id, quantity FROM usage_events
           WHERE meter_id = ? AND customer_id = ? AND billing_run_id IS NULL
             AND occurred_at <= ? ORDER BY occurred_at, id""",
        (meter_id, customer_id, through_at),
    ).fetchall()
    state = conn.execute(
        "SELECT carry_microminor FROM billing_state WHERE meter_id = ? AND customer_id = ?",
        (meter_id, customer_id),
    ).fetchone()
    carry = int(state["carry_microminor"]) if state else 0
    total = carry + sum(
        int(row["quantity"]) * int(meter["unit_price_microminor"]) for row in events
    )
    amount_minor, remainder = divmod(total, MICROMINOR_PER_MINOR)
    run_id = f"billrun_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO billing_runs
               (id, meter_id, customer_id, through_at, amount_minor,
                carry_microminor, usage_event_count, currency, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, meter_id, customer_id, through_at, amount_minor,
                remainder, len(events), meter["currency"], int(time.time()),
            ),
        )
        conn.execute(
            """INSERT INTO billing_state (meter_id, customer_id, carry_microminor)
               VALUES (?, ?, ?) ON CONFLICT(meter_id, customer_id)
               DO UPDATE SET carry_microminor = excluded.carry_microminor""",
            (meter_id, customer_id, remainder),
        )
        if events:
            conn.executemany(
                "UPDATE usage_events SET billing_run_id = ? WHERE id = ?",
                ((run_id, row["id"]) for row in events),
            )
    return {
        "billing_run_id": run_id,
        "amount_minor": amount_minor,
        "carry_microminor": remainder,
        "currency": meter["currency"],
        "usage_event_count": len(events),
    }
