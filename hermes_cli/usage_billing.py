"""Immutable usage metering for governed, machine-readable invoicing."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Iterable, Mapping


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agentic_usage_events (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    customer_ref TEXT NOT NULL,
    metric TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price_minor INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_agentic_usage_events_customer
    ON agentic_usage_events(organization_id, customer_ref, currency, occurred_at, id);
CREATE TABLE IF NOT EXISTS agentic_usage_invoice_allocations (
    id TEXT PRIMARY KEY,
    usage_event_id TEXT NOT NULL UNIQUE,
    payment_intent_id TEXT NOT NULL,
    allocated_at INTEGER NOT NULL,
    FOREIGN KEY(usage_event_id) REFERENCES agentic_usage_events(id),
    FOREIGN KEY(payment_intent_id) REFERENCES payment_intents(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_usage_allocations_intent
    ON agentic_usage_invoice_allocations(payment_intent_id);
CREATE TRIGGER IF NOT EXISTS agentic_usage_events_immutable_update
BEFORE UPDATE ON agentic_usage_events
BEGIN SELECT RAISE(ABORT, 'usage events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agentic_usage_events_immutable_delete
BEFORE DELETE ON agentic_usage_events
BEGIN SELECT RAISE(ABORT, 'usage events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agentic_usage_invoice_allocations_immutable_update
BEFORE UPDATE ON agentic_usage_invoice_allocations
BEGIN SELECT RAISE(ABORT, 'usage invoice allocations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agentic_usage_invoice_allocations_immutable_delete
BEFORE DELETE ON agentic_usage_invoice_allocations
BEGIN SELECT RAISE(ABORT, 'usage invoice allocations are immutable'); END;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Allocations bind to payment intents; initialize that parent table when
    # metering is used independently of PaymentService (for example in setup
    # and reconciliation tooling).
    from hermes_cli import payments

    payments.ensure_schema(conn)
    if conn.in_transaction:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if {
            "agentic_usage_events",
            "agentic_usage_invoice_allocations",
        }.issubset(tables):
            return
    conn.executescript(SCHEMA_SQL)


def record_usage(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    customer_ref: str,
    metric: str,
    quantity: int,
    unit_price_minor: int,
    currency: str,
    idempotency_key: str,
    occurred_at: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one billable event, safely idempotent by organization and key."""
    ensure_schema(conn)
    if not all(str(value).strip() for value in (organization_id, customer_ref, metric, currency, idempotency_key)):
        raise ValueError("usage event identity fields are required")
    if quantity <= 0 or unit_price_minor < 0:
        raise ValueError("usage quantity must be positive and unit price non-negative")
    normalized_currency = currency.upper()
    amount_minor = quantity * unit_price_minor
    metadata_json = json.dumps(dict(metadata or {}), separators=(",", ":"), sort_keys=True)
    existing = conn.execute(
        "SELECT * FROM agentic_usage_events WHERE organization_id=? AND idempotency_key=?",
        (organization_id, idempotency_key),
    ).fetchone()
    if existing:
        if any(existing[key] != value for key, value in {
            "customer_ref": customer_ref,
            "metric": metric,
            "quantity": quantity,
            "unit_price_minor": unit_price_minor,
            "currency": normalized_currency,
            "amount_minor": amount_minor,
        }.items()):
            raise ValueError("usage idempotency key was reused with different event data")
        return dict(existing)
    now = int(time.time())
    event = {
        "id": uuid.uuid4().hex,
        "organization_id": organization_id,
        "customer_ref": customer_ref,
        "metric": metric,
        "quantity": quantity,
        "unit_price_minor": unit_price_minor,
        "amount_minor": amount_minor,
        "currency": normalized_currency,
        "occurred_at": int(occurred_at if occurred_at is not None else now),
        "idempotency_key": idempotency_key,
        "metadata_json": metadata_json,
        "created_at": now,
    }
    conn.execute(
        """INSERT INTO agentic_usage_events
           (id,organization_id,customer_ref,metric,quantity,unit_price_minor,
            amount_minor,currency,occurred_at,idempotency_key,metadata_json,created_at)
           VALUES (:id,:organization_id,:customer_ref,:metric,:quantity,:unit_price_minor,
                   :amount_minor,:currency,:occurred_at,:idempotency_key,:metadata_json,:created_at)""",
        event,
    )
    conn.commit()
    return event


def invoice_context(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    customer_ref: str,
    currency: str,
    event_ids: Iterable[str],
    existing_payment_intent_id: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    ids = list(dict.fromkeys(str(value) for value in event_ids))
    if not ids:
        raise ValueError("metered invoice requires at least one usage event")
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT u.*, a.payment_intent_id AS allocated_payment_intent_id FROM agentic_usage_events u "
        f"LEFT JOIN agentic_usage_invoice_allocations a ON a.usage_event_id=u.id "
        f"WHERE u.id IN ({placeholders}) ORDER BY u.occurred_at,u.id",
        ids,
    ).fetchall()
    if len(rows) != len(ids):
        raise ValueError("metered invoice references an unknown usage event")
    normalized_currency = currency.upper()
    for row in rows:
        if row["organization_id"] != organization_id or row["customer_ref"] != customer_ref:
            raise ValueError("usage events must belong to the invoice organization and customer")
        if row["currency"] != normalized_currency:
            raise ValueError("metered invoice cannot mix currencies")
        if row["allocated_payment_intent_id"] and row["allocated_payment_intent_id"] != existing_payment_intent_id:
            raise ValueError("usage event is already allocated to an invoice")
    return {
        "customer_ref": customer_ref,
        "currency": normalized_currency,
        "amount_minor": sum(int(row["amount_minor"]) for row in rows),
        "event_ids": ids,
        "metrics": {metric: sum(int(row["quantity"]) for row in rows if row["metric"] == metric)
                     for metric in sorted({row["metric"] for row in rows})},
    }


def allocate_events(conn: sqlite3.Connection, *, event_ids: Iterable[str], payment_intent_id: str) -> None:
    ensure_schema(conn)
    now = int(time.time())
    for event_id in dict.fromkeys(str(value) for value in event_ids):
        try:
            conn.execute(
                "INSERT INTO agentic_usage_invoice_allocations (id,usage_event_id,payment_intent_id,allocated_at) VALUES (?,?,?,?)",
                (uuid.uuid4().hex, event_id, payment_intent_id, now),
            )
        except sqlite3.IntegrityError as exc:
            existing = conn.execute(
                "SELECT payment_intent_id FROM agentic_usage_invoice_allocations WHERE usage_event_id=?",
                (event_id,),
            ).fetchone()
            if existing and existing["payment_intent_id"] == payment_intent_id:
                continue
            raise ValueError("usage event is already allocated to another invoice") from exc
    conn.commit()


def allocation_readback(
    conn: sqlite3.Connection,
    *,
    payment_intent_id: str,
    event_ids: Iterable[str],
    expected_amount_minor: int,
) -> dict[str, Any]:
    """Independently read back the durable event-to-invoice ledger binding."""
    ensure_schema(conn)
    expected = set(str(value) for value in event_ids)
    rows = conn.execute(
        """SELECT u.id, u.amount_minor
             FROM agentic_usage_invoice_allocations a
             JOIN agentic_usage_events u ON u.id=a.usage_event_id
            WHERE a.payment_intent_id=?
            ORDER BY u.id""",
        (payment_intent_id,),
    ).fetchall()
    actual = {str(row["id"]) for row in rows}
    amount = sum(int(row["amount_minor"]) for row in rows)
    return {
        "passed": bool(expected) and actual == expected and amount == int(expected_amount_minor),
        "expected_event_count": len(expected),
        "actual_event_count": len(actual),
        "expected_event_ids": sorted(expected),
        "actual_event_ids": sorted(actual),
        "allocated_amount_minor": amount,
        "expected_amount_minor": int(expected_amount_minor),
    }
