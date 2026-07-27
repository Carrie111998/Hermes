"""Append-only treasury, budgets, and reservations for the Business OS."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Optional

from hermes_cli import accounting_db

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS treasury_accounts (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    name            TEXT NOT NULL,
    currency        TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    UNIQUE(organization_id, name, currency)
);

CREATE TABLE IF NOT EXISTS treasury_entries (
    id                 TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL,
    kind               TEXT NOT NULL,
    amount_minor       INTEGER NOT NULL,
    currency           TEXT NOT NULL,
    objective_id       TEXT,
    action_id          TEXT,
    external_reference TEXT,
    idempotency_key    TEXT UNIQUE,
    evidence_json      TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    FOREIGN KEY(account_id) REFERENCES treasury_accounts(id)
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    id              TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    objective_id    TEXT NOT NULL,
    action_id       TEXT NOT NULL UNIQUE,
    amount_minor    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    status          TEXT NOT NULL,
    expires_at      INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    settled_at      INTEGER,
    FOREIGN KEY(account_id) REFERENCES treasury_accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_treasury_entries_account
    ON treasury_entries(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_open
    ON budget_reservations(account_id, status, expires_at);
"""

MINIMUM_INITIAL_CAPITAL_MINOR = 1000  # $10.00


class BudgetError(ValueError):
    """Raised when a financial operation violates available authority."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> int:
    return int(time.time())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='treasury_accounts'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)


def create_treasury_account(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    currency: str = "USD",
    name: str = "operating",
) -> str:
    ensure_schema(conn)
    existing = conn.execute(
        """
        SELECT id FROM treasury_accounts
         WHERE organization_id = ? AND name = ? AND currency = ?
        """,
        (organization_id, name, currency.upper()),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    account_id = _id("acct")
    with conn:
        conn.execute(
            """
            INSERT INTO treasury_accounts (
                id, organization_id, name, currency, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, organization_id, name, currency.upper(), _now()),
        )
    accounting_db.ensure_standard_chart(conn, organization_id)
    return account_id


def operating_account_for_organization(
    conn: sqlite3.Connection, organization_id: str, currency: str
) -> Optional[str]:
    ensure_schema(conn)
    row = conn.execute(
        """SELECT id FROM treasury_accounts
           WHERE organization_id = ? AND name = 'operating' AND currency = ?""",
        (organization_id, currency.upper()),
    ).fetchone()
    return str(row["id"]) if row is not None else None


def account_balance(conn: sqlite3.Connection, account_id: str) -> int:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_minor), 0) AS balance "
        "FROM treasury_entries WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return int(row["balance"])


def reserved_balance(conn: sqlite3.Connection, account_id: str) -> int:
    ensure_schema(conn)
    now = _now()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_minor), 0) AS reserved
          FROM budget_reservations
         WHERE account_id = ? AND status = 'reserved' AND expires_at > ?
        """,
        (account_id, now),
    ).fetchone()
    return int(row["reserved"])


def committed_compute_balance(
    conn: sqlite3.Connection, account_id: str
) -> int:
    """Return conservative planner cost already committed by the organization."""
    ensure_schema(conn)
    return _committed_compute_balance_raw(conn, account_id)


def _committed_compute_balance_raw(
    conn: sqlite3.Connection, account_id: str
) -> int:
    """Compute commitment without schema work that could end an open transaction."""
    account = conn.execute(
        "SELECT organization_id FROM treasury_accounts WHERE id=?",
        (account_id,),
    ).fetchone()
    if account is None:
        raise KeyError(f"treasury account not found: {account_id}")
    tables = {
        str(row["name"])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                WHERE type='table'
                  AND name IN ('objective_resource_usage','objectives')"""
        ).fetchall()
    }
    if tables != {"objective_resource_usage", "objectives"}:
        return 0
    reconciliation_tables = {
        str(row["name"])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                WHERE type='table'
                  AND name IN (
                    'planner_compute_reconciliations',
                    'planner_compute_reservations'
                  )"""
        ).fetchall()
    }
    if reconciliation_tables == {
        "planner_compute_reconciliations",
        "planner_compute_reservations",
    }:
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(usage.estimated_compute_cost_minor),0)
                 - COALESCE((
                       SELECT SUM(reservation.reserved_minor)
                         FROM planner_compute_reconciliations reconciliation
                         JOIN planner_compute_reservations reservation
                           ON reservation.id=reconciliation.reservation_id
                        WHERE reservation.organization_id=?
                   ),0) AS n
                 FROM objective_resource_usage usage
                 JOIN objectives objective ON objective.id=usage.objective_id
                WHERE objective.organization_id=?""",
            (account["organization_id"], account["organization_id"]),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COALESCE(
                         SUM(usage.estimated_compute_cost_minor),0
                       ) AS n
                 FROM objective_resource_usage usage
                 JOIN objectives objective ON objective.id=usage.objective_id
                WHERE objective.organization_id=?""",
            (account["organization_id"],),
        ).fetchone()
    return int(row["n"])


def available_balance(conn: sqlite3.Connection, account_id: str) -> int:
    return (
        account_balance(conn, account_id)
        - reserved_balance(conn, account_id)
        - committed_compute_balance(conn, account_id)
    )


def record_entry(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    kind: str,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
    evidence: Any,
    objective_id: Optional[str] = None,
    action_id: Optional[str] = None,
    external_reference: Optional[str] = None,
    reserved_action_id: Optional[str] = None,
) -> str:
    if not isinstance(amount_minor, int) or amount_minor == 0:
        raise BudgetError("ledger amount must be a non-zero integer in minor units")
    account = conn.execute(
        "SELECT currency FROM treasury_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None:
        raise KeyError(f"treasury account not found: {account_id}")
    if account["currency"] != currency.upper():
        raise BudgetError("ledger currency does not match treasury account")
    existing = conn.execute(
        "SELECT * FROM treasury_entries WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if any(
            actual != expected
            for actual, expected in (
                (str(existing["account_id"]), str(account_id)),
                (str(existing["kind"]), str(kind)),
                (int(existing["amount_minor"]), int(amount_minor)),
                (str(existing["currency"]), str(currency).upper()),
                (existing["objective_id"], objective_id),
                (existing["action_id"], action_id),
                (existing["external_reference"], external_reference),
            )
        ):
            raise BudgetError(
                "ledger idempotency key was reused with different entry parameters"
            )
        return str(existing["id"])
    if amount_minor < 0:
        spendable = available_balance(conn, account_id)
        if reserved_action_id is not None:
            reservation = conn.execute(
                """
                SELECT amount_minor FROM budget_reservations
                 WHERE account_id = ? AND action_id = ? AND status = 'reserved'
                """,
                (account_id, reserved_action_id),
            ).fetchone()
            if reservation is not None:
                spendable += int(reservation["amount_minor"])
        if spendable < -amount_minor:
            raise BudgetError("insufficient treasury balance")
    entry_id = _id("entry")
    with conn:
        conn.execute(
            """
            INSERT INTO treasury_entries (
                id, account_id, kind, amount_minor, currency, objective_id,
                action_id, external_reference, idempotency_key, evidence_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                account_id,
                kind,
                amount_minor,
                currency.upper(),
                objective_id,
                action_id,
                external_reference,
                idempotency_key,
                _json(evidence),
                _now(),
            ),
        )
    evidence_map = evidence if isinstance(evidence, dict) else {}
    counter_code = str(
        evidence_map.get("accounting", {}).get(
            "counter_account_code",
            {
                "capital_contribution": "3000",
                "customer_payment": "4000",
                "payment": "6000",
            }.get(kind, "6000" if amount_minor < 0 else "4000"),
        )
    )
    if amount_minor > 0:
        lines = (
            {"account_code": "1000", "debit_minor": amount_minor},
            {"account_code": counter_code, "credit_minor": amount_minor},
        )
    else:
        lines = (
            {"account_code": counter_code, "debit_minor": -amount_minor},
            {"account_code": "1000", "credit_minor": -amount_minor},
        )
    accounting_db.post_journal(
        conn,
        organization_id=conn.execute(
            "SELECT organization_id FROM treasury_accounts WHERE id = ?", (account_id,)
        ).fetchone()["organization_id"],
        description=kind.replace("_", " "),
        source_type="treasury_entry",
        source_id=entry_id,
        currency=currency,
        lines=lines,
        evidence=evidence,
    )
    return entry_id


def seed_initial_capital(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    amount_minor: int,
    currency: str,
    actor: str,
) -> str:
    if amount_minor < MINIMUM_INITIAL_CAPITAL_MINOR:
        raise BudgetError("initial capital must be at least $10.00")
    return record_entry(
        conn,
        account_id=account_id,
        kind="capital_contribution",
        amount_minor=amount_minor,
        currency=currency,
        idempotency_key=f"initial-capital:{account_id}",
        evidence={"actor": actor, "kind": "initial_capital"},
    )


def reserve_budget(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    objective_id: str,
    action_id: str,
    amount_minor: int,
    currency: str,
    expires_at: int,
    objective_budget_minor: Optional[int] = None,
) -> str:
    ensure_schema(conn)
    if amount_minor < 0:
        raise BudgetError("reservation amount must be non-negative")
    if expires_at <= _now():
        raise BudgetError("reservation expiry must be in the future")
    if objective_budget_minor is not None and objective_budget_minor < 0:
        raise BudgetError("objective budget cannot be negative")
    if conn.in_transaction:
        raise BudgetError(
            "budget reservation requires an independent atomic transaction"
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, amount_minor FROM budget_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if existing is not None:
            if int(existing["amount_minor"]) != amount_minor:
                raise BudgetError(
                    "action already has a different budget reservation"
                )
            conn.commit()
            return str(existing["id"])
        if objective_budget_minor is not None:
            objective_spend = int(
                conn.execute(
                    """SELECT COALESCE(SUM(amount_minor),0) AS n
                         FROM budget_reservations
                        WHERE objective_id=? AND status IN ('reserved','settled')
                          AND currency=?""",
                    (objective_id, currency.upper()),
                ).fetchone()["n"]
            )
            if objective_spend + amount_minor > objective_budget_minor:
                raise BudgetError(
                    "objective cumulative budget would be exceeded"
                )
        cash = int(
            conn.execute(
                """SELECT COALESCE(SUM(amount_minor),0) AS n
                     FROM treasury_entries WHERE account_id=?""",
                (account_id,),
            ).fetchone()["n"]
        )
        externally_reserved = int(
            conn.execute(
                """SELECT COALESCE(SUM(amount_minor),0) AS n
                     FROM budget_reservations
                    WHERE account_id=? AND status='reserved'
                      AND expires_at>?""",
                (account_id, _now()),
            ).fetchone()["n"]
        )
        compute_committed = _committed_compute_balance_raw(
            conn, account_id
        )
        if (
            cash - externally_reserved - compute_committed
            < amount_minor
        ):
            raise BudgetError("insufficient available capital for reservation")
        reservation_id = _id("reserve")
        conn.execute(
            """
            INSERT INTO budget_reservations (
                id, account_id, objective_id, action_id, amount_minor,
                currency, status, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
            """,
            (
                reservation_id,
                account_id,
                objective_id,
                action_id,
                amount_minor,
                currency.upper(),
                expires_at,
                _now(),
            ),
        )
        conn.commit()
        return reservation_id
    except Exception:
        conn.rollback()
        raise


def settle_reservation(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    actual_amount_minor: int,
    external_reference: str,
    evidence: Any,
) -> str:
    if actual_amount_minor < 0:
        raise BudgetError("actual spend must be non-negative")
    reservation = conn.execute(
        "SELECT * FROM budget_reservations WHERE action_id = ?", (action_id,)
    ).fetchone()
    if reservation is None or reservation["status"] != "reserved":
        raise BudgetError("action has no open budget reservation")
    if actual_amount_minor > int(reservation["amount_minor"]):
        raise BudgetError("actual spend exceeds the exact reserved amount")
    entry_id = record_entry(
        conn,
        account_id=reservation["account_id"],
        kind="payment",
        amount_minor=-actual_amount_minor,
        currency=reservation["currency"],
        objective_id=reservation["objective_id"],
        action_id=action_id,
        external_reference=external_reference,
        idempotency_key=f"settlement:{action_id}",
        evidence=evidence,
        reserved_action_id=action_id,
    )
    with conn:
        conn.execute(
            """
            UPDATE budget_reservations
               SET status = 'settled', settled_at = ?
             WHERE action_id = ? AND status = 'reserved'
            """,
            (_now(), action_id),
        )
    return entry_id


def release_reservation(conn: sqlite3.Connection, action_id: str, *, reason: str) -> None:
    with conn:
        updated = conn.execute(
            """
            UPDATE budget_reservations
               SET status = 'released', settled_at = ?
             WHERE action_id = ? AND status = 'reserved'
            """,
            (_now(), action_id),
        )
        if updated.rowcount != 1:
            raise BudgetError("action has no open budget reservation")
