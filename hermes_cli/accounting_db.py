"""Immutable double-entry accounting and evidence-bound tax accruals."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fiscal_periods (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL,
    starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', closed_at INTEGER,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(organization_id, name)
);
CREATE TABLE IF NOT EXISTS fiscal_period_events (
    id TEXT PRIMARY KEY, period_id TEXT NOT NULL, event_type TEXT NOT NULL,
    evidence_json TEXT NOT NULL, occurred_at INTEGER NOT NULL,
    FOREIGN KEY(period_id) REFERENCES fiscal_periods(id),
    UNIQUE(period_id, event_type)
);
CREATE TABLE IF NOT EXISTS ledger_accounts (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, code TEXT NOT NULL,
    name TEXT NOT NULL, account_type TEXT NOT NULL, normal_side TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
    UNIQUE(organization_id, code)
);
CREATE TABLE IF NOT EXISTS journal_entries (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, period_id TEXT,
    occurred_at INTEGER NOT NULL, description TEXT NOT NULL,
    source_type TEXT NOT NULL, source_id TEXT NOT NULL,
    currency TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(organization_id, source_type, source_id)
);
CREATE TABLE IF NOT EXISTS journal_lines (
    id TEXT PRIMARY KEY, journal_entry_id TEXT NOT NULL, account_id TEXT NOT NULL,
    debit_minor INTEGER NOT NULL DEFAULT 0, credit_minor INTEGER NOT NULL DEFAULT 0,
    tax_code TEXT, party_json TEXT NOT NULL DEFAULT '{}', memo TEXT,
    FOREIGN KEY(journal_entry_id) REFERENCES journal_entries(id),
    FOREIGN KEY(account_id) REFERENCES ledger_accounts(id),
    CHECK(debit_minor >= 0 AND credit_minor >= 0),
    CHECK((debit_minor = 0) != (credit_minor = 0))
);
CREATE TABLE IF NOT EXISTS tax_registrations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, jurisdiction TEXT NOT NULL,
    tax_type TEXT NOT NULL, registration_number TEXT, filing_frequency TEXT NOT NULL,
    effective_from INTEGER NOT NULL, effective_to INTEGER,
    status TEXT NOT NULL, evidence_json TEXT NOT NULL,
    UNIQUE(organization_id, jurisdiction, tax_type, effective_from)
);
CREATE TABLE IF NOT EXISTS tax_rates (
    id TEXT PRIMARY KEY, registration_id TEXT NOT NULL, tax_code TEXT NOT NULL,
    rate_basis_points INTEGER NOT NULL, effective_from INTEGER NOT NULL,
    effective_to INTEGER, authority_source TEXT NOT NULL, verified_at INTEGER NOT NULL,
    FOREIGN KEY(registration_id) REFERENCES tax_registrations(id),
    CHECK(rate_basis_points >= 0)
);
CREATE TABLE IF NOT EXISTS tax_obligations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, registration_id TEXT NOT NULL,
    period_start INTEGER NOT NULL, period_end INTEGER NOT NULL, due_at INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
    evidence_json TEXT NOT NULL, filed_at INTEGER, paid_at INTEGER,
    UNIQUE(registration_id, period_start, period_end)
);
CREATE TABLE IF NOT EXISTS tax_obligation_events (
    id TEXT PRIMARY KEY, obligation_id TEXT NOT NULL, event_type TEXT NOT NULL,
    evidence_json TEXT NOT NULL, occurred_at INTEGER NOT NULL,
    FOREIGN KEY(obligation_id) REFERENCES tax_obligations(id),
    UNIQUE(obligation_id, event_type)
);
CREATE TRIGGER IF NOT EXISTS journal_entries_immutable_update
BEFORE UPDATE ON journal_entries BEGIN SELECT RAISE(ABORT, 'journal entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS journal_entries_immutable_delete
BEFORE DELETE ON journal_entries BEGIN SELECT RAISE(ABORT, 'journal entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS journal_lines_immutable_update
BEFORE UPDATE ON journal_lines BEGIN SELECT RAISE(ABORT, 'journal lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS journal_lines_immutable_delete
BEFORE DELETE ON journal_lines BEGIN SELECT RAISE(ABORT, 'journal lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fiscal_period_events_immutable_update
BEFORE UPDATE ON fiscal_period_events BEGIN SELECT RAISE(ABORT, 'fiscal period events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fiscal_period_events_immutable_delete
BEFORE DELETE ON fiscal_period_events BEGIN SELECT RAISE(ABORT, 'fiscal period events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tax_obligation_events_immutable_update
BEFORE UPDATE ON tax_obligation_events BEGIN SELECT RAISE(ABORT, 'tax obligation events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tax_obligation_events_immutable_delete
BEFORE DELETE ON tax_obligation_events BEGIN SELECT RAISE(ABORT, 'tax obligation events are immutable'); END;
"""

CONTRACT_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS fiscal_periods_contract_immutable_update
BEFORE UPDATE ON fiscal_periods
WHEN NEW.id != OLD.id
  OR NEW.organization_id != OLD.organization_id
  OR NEW.name != OLD.name
  OR NEW.starts_at != OLD.starts_at
  OR NEW.ends_at != OLD.ends_at
  OR NEW.evidence_json != OLD.evidence_json
  OR (OLD.status = 'closed' AND NEW.status != 'closed')
  OR NEW.status NOT IN ('open','closed')
BEGIN SELECT RAISE(ABORT, 'fiscal period contract is immutable'); END;
CREATE TRIGGER IF NOT EXISTS fiscal_periods_immutable_delete
BEFORE DELETE ON fiscal_periods
BEGIN SELECT RAISE(ABORT, 'fiscal periods are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tax_obligations_contract_immutable_update
BEFORE UPDATE ON tax_obligations
WHEN NEW.id != OLD.id
  OR NEW.organization_id != OLD.organization_id
  OR NEW.registration_id != OLD.registration_id
  OR NEW.period_start != OLD.period_start
  OR NEW.period_end != OLD.period_end
  OR NEW.due_at != OLD.due_at
  OR NEW.amount_minor != OLD.amount_minor
  OR NEW.currency != OLD.currency
  OR NEW.evidence_json != OLD.evidence_json
  OR NEW.status NOT IN ('accrued','filed','paid')
  OR (OLD.status = 'filed' AND NEW.status = 'accrued')
  OR (OLD.status = 'paid' AND NEW.status != 'paid')
BEGIN SELECT RAISE(ABORT, 'tax obligation contract is immutable'); END;
CREATE TRIGGER IF NOT EXISTS tax_obligations_immutable_delete
BEFORE DELETE ON tax_obligations
BEGIN SELECT RAISE(ABORT, 'tax obligations are immutable'); END;
"""

STANDARD_CHART = (
    ("1000", "Operating cash", "asset", "debit"),
    ("1100", "Accounts receivable", "asset", "debit"),
    ("2000", "Accounts payable", "liability", "credit"),
    ("2100", "Sales tax payable", "liability", "credit"),
    ("2200", "Payroll tax payable", "liability", "credit"),
    ("2300", "Income tax payable", "liability", "credit"),
    ("3000", "Owner capital", "equity", "credit"),
    ("3100", "Retained earnings", "equity", "credit"),
    ("4000", "Sales revenue", "revenue", "credit"),
    ("5000", "Cost of goods sold", "expense", "debit"),
    ("6000", "Operating expense", "expense", "debit"),
    ("6100", "Contractor expense", "expense", "debit"),
    ("6200", "Payroll expense", "expense", "debit"),
    ("6300", "Software and services", "expense", "debit"),
    ("6400", "Tax expense", "expense", "debit"),
)


class AccountingError(ValueError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    period_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(fiscal_periods)")
    }
    if "evidence_json" not in period_columns:
        conn.execute(
            "ALTER TABLE fiscal_periods ADD COLUMN "
            "evidence_json TEXT NOT NULL DEFAULT '{}'"
        )
    conn.executescript(CONTRACT_TRIGGER_SQL)


def open_fiscal_period(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    name: str,
    starts_at: int,
    ends_at: int,
    evidence: Any,
) -> str:
    """Open one non-overlapping period with retry-safe exact semantics."""
    ensure_schema(conn)
    if not organization_id or not name.strip() or starts_at <= 0 or ends_at < starts_at:
        raise AccountingError("fiscal period requires valid organization, name, and dates")
    if not evidence:
        raise AccountingError("fiscal period requires supporting evidence")
    existing = conn.execute(
        "SELECT * FROM fiscal_periods WHERE organization_id=? AND name=?",
        (organization_id, name.strip()),
    ).fetchone()
    if existing is not None:
        if (
            int(existing["starts_at"]) == starts_at
            and int(existing["ends_at"]) == ends_at
        ):
            return str(existing["id"])
        raise AccountingError("fiscal period name already exists with different dates")
    overlap = conn.execute(
        """SELECT id FROM fiscal_periods
           WHERE organization_id=? AND starts_at<=? AND ends_at>=?
           LIMIT 1""",
        (organization_id, ends_at, starts_at),
    ).fetchone()
    if overlap is not None:
        raise AccountingError("fiscal periods must not overlap")
    period_id = _id("period")
    now = int(time.time())
    with conn:
        conn.execute(
            """INSERT INTO fiscal_periods
               (id,organization_id,name,starts_at,ends_at,status,evidence_json)
               VALUES (?,?,?,?,?,'open',?)""",
            (
                period_id,
                organization_id,
                name.strip(),
                starts_at,
                ends_at,
                _json(evidence),
            ),
        )
        conn.execute(
            """INSERT INTO fiscal_period_events
               (id,period_id,event_type,evidence_json,occurred_at)
               VALUES (?,?,'opened',?,?)""",
            (_id("periodevent"), period_id, _json(evidence), now),
        )
    return period_id


def ensure_standard_chart(conn: sqlite3.Connection, organization_id: str) -> None:
    ensure_schema(conn)
    now = int(time.time())
    with conn:
        for code, name, account_type, normal_side in STANDARD_CHART:
            conn.execute(
                """INSERT OR IGNORE INTO ledger_accounts
                   (id, organization_id, code, name, account_type, normal_side, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (_id("gl"), organization_id, code, name, account_type, normal_side, now),
            )


def _account_id(conn: sqlite3.Connection, organization_id: str, code: str) -> str:
    row = conn.execute(
        "SELECT id FROM ledger_accounts WHERE organization_id = ? AND code = ? AND active = 1",
        (organization_id, code),
    ).fetchone()
    if row is None:
        raise AccountingError(f"active ledger account not found: {code}")
    return str(row["id"])


def post_journal(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    description: str,
    source_type: str,
    source_id: str,
    currency: str,
    lines: Iterable[Mapping[str, Any]],
    evidence: Any,
    occurred_at: Optional[int] = None,
) -> str:
    ensure_standard_chart(conn, organization_id)
    materialized = [dict(line) for line in lines]
    debit = sum(int(line.get("debit_minor", 0)) for line in materialized)
    credit = sum(int(line.get("credit_minor", 0)) for line in materialized)
    if not materialized or debit <= 0 or debit != credit:
        raise AccountingError("journal entry must contain balanced, non-zero debits and credits")
    existing = conn.execute(
        """SELECT id FROM journal_entries
           WHERE organization_id = ? AND source_type = ? AND source_id = ?""",
        (organization_id, source_type, source_id),
    ).fetchone()
    if existing:
        return str(existing["id"])
    occurred = occurred_at or int(time.time())
    period = conn.execute(
        """SELECT id, status FROM fiscal_periods
           WHERE organization_id = ? AND starts_at <= ? AND ends_at >= ?
           ORDER BY starts_at DESC LIMIT 1""",
        (organization_id, occurred, occurred),
    ).fetchone()
    if period is not None and period["status"] != "open":
        raise AccountingError("cannot post into a closed fiscal period")
    entry_id = _id("journal")
    with conn:
        conn.execute(
            """INSERT INTO journal_entries
               (id, organization_id, period_id, occurred_at, description, source_type,
                source_id, currency, evidence_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id, organization_id, period["id"] if period else None, occurred,
                description, source_type, source_id, currency.upper(), _json(evidence),
                int(time.time()),
            ),
        )
        for line in materialized:
            conn.execute(
                """INSERT INTO journal_lines
                   (id, journal_entry_id, account_id, debit_minor, credit_minor,
                    tax_code, party_json, memo) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _id("line"), entry_id,
                    _account_id(conn, organization_id, str(line["account_code"])),
                    int(line.get("debit_minor", 0)), int(line.get("credit_minor", 0)),
                    line.get("tax_code"), _json(line.get("party", {})), line.get("memo"),
                ),
            )
    return entry_id


def configure_tax_registration(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    jurisdiction: str,
    tax_type: str,
    filing_frequency: str,
    effective_from: int,
    evidence: Any,
    registration_number: Optional[str] = None,
) -> str:
    if not jurisdiction.strip() or not tax_type.strip() or not evidence:
        raise AccountingError("tax registration requires jurisdiction, type, and evidence")
    ensure_schema(conn)
    registration_id = _id("taxreg")
    with conn:
        conn.execute(
            """INSERT INTO tax_registrations
               (id, organization_id, jurisdiction, tax_type, registration_number,
                filing_frequency, effective_from, status, evidence_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (
                registration_id, organization_id, jurisdiction, tax_type,
                registration_number, filing_frequency, effective_from, _json(evidence),
            ),
        )
    return registration_id


def configure_tax_rate(
    conn: sqlite3.Connection,
    *,
    registration_id: str,
    tax_code: str,
    rate_basis_points: int,
    effective_from: int,
    authority_source: str,
    verified_at: int,
) -> str:
    if not authority_source or verified_at <= 0:
        raise AccountingError("tax rate requires a verified authority source")
    rate_id = _id("taxrate")
    with conn:
        conn.execute(
            """INSERT INTO tax_rates
               (id, registration_id, tax_code, rate_basis_points, effective_from,
                authority_source, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                rate_id, registration_id, tax_code, rate_basis_points, effective_from,
                authority_source, verified_at,
            ),
        )
    return rate_id


def calculate_tax(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    jurisdiction: str,
    tax_type: str,
    tax_code: str,
    taxable_minor: int,
    occurred_at: int,
) -> tuple[int, str]:
    row = conn.execute(
        """SELECT tr.rate_basis_points, tr.id
           FROM tax_rates tr JOIN tax_registrations rg ON rg.id = tr.registration_id
           WHERE rg.organization_id = ? AND rg.jurisdiction = ? AND rg.tax_type = ?
             AND rg.status = 'active' AND tr.tax_code = ?
             AND rg.effective_from <= ? AND tr.effective_from <= ?
             AND (rg.effective_to IS NULL OR rg.effective_to >= ?)
             AND (tr.effective_to IS NULL OR tr.effective_to >= ?)
           ORDER BY tr.effective_from DESC LIMIT 1""",
        (
            organization_id, jurisdiction, tax_type, tax_code, occurred_at,
            occurred_at, occurred_at, occurred_at,
        ),
    ).fetchone()
    if row is None:
        raise AccountingError("no verified tax rule covers this transaction")
    amount = (Decimal(taxable_minor) * Decimal(int(row["rate_basis_points"])) / Decimal(10000))
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), str(row["id"])


def trial_balance(conn: sqlite3.Connection, organization_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT a.code, a.name, a.account_type,
                  COALESCE(SUM(l.debit_minor), 0) AS debits,
                  COALESCE(SUM(l.credit_minor), 0) AS credits
           FROM ledger_accounts a
           LEFT JOIN journal_lines l ON l.account_id = a.id
           WHERE a.organization_id = ?
           GROUP BY a.id ORDER BY a.code""",
        (organization_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def tax_liability(conn: sqlite3.Connection, organization_id: str) -> int:
    row = conn.execute(
        """SELECT COALESCE(SUM(l.credit_minor - l.debit_minor), 0) AS liability
           FROM journal_lines l JOIN ledger_accounts a ON a.id = l.account_id
           WHERE a.organization_id = ? AND a.code IN ('2100', '2200', '2300')""",
        (organization_id,),
    ).fetchone()
    return int(row["liability"])


def financial_statements(
    conn: sqlite3.Connection, organization_id: str
) -> dict[str, Any]:
    """Return balance sheet and profit/loss from the immutable journal."""
    trial = trial_balance(conn, organization_id)
    balances = {
        row["code"]: (
            int(row["debits"]) - int(row["credits"])
            if row["account_type"] in {"asset", "expense"}
            else int(row["credits"]) - int(row["debits"])
        )
        for row in trial
    }
    assets = sum(
        balances[row["code"]] for row in trial if row["account_type"] == "asset"
    )
    liabilities = sum(
        balances[row["code"]] for row in trial if row["account_type"] == "liability"
    )
    equity = sum(
        balances[row["code"]] for row in trial if row["account_type"] == "equity"
    )
    revenue = sum(
        balances[row["code"]] for row in trial if row["account_type"] == "revenue"
    )
    expenses = sum(
        balances[row["code"]] for row in trial if row["account_type"] == "expense"
    )
    net_income = revenue - expenses
    return {
        "balance_sheet": {
            "assets_minor": assets,
            "liabilities_minor": liabilities,
            "equity_before_current_earnings_minor": equity,
            "current_earnings_minor": net_income,
            "balanced": assets == liabilities + equity + net_income,
        },
        "profit_and_loss": {
            "revenue_minor": revenue,
            "expenses_minor": expenses,
            "net_income_minor": net_income,
        },
        "tax_liability_minor": tax_liability(conn, organization_id),
        "trial_balance": trial,
    }


def record_tax_obligation(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    registration_id: str,
    period_start: int,
    period_end: int,
    due_at: int,
    amount_minor: int,
    currency: str,
    evidence: Any,
) -> str:
    ensure_schema(conn)
    if period_end < period_start or due_at <= period_end or amount_minor < 0:
        raise AccountingError("invalid tax obligation period, due date, or amount")
    if not evidence:
        raise AccountingError("tax obligation requires supporting evidence")
    registration = conn.execute(
        """SELECT * FROM tax_registrations
           WHERE id=? AND organization_id=? AND status='active'
             AND effective_from<=?
             AND (effective_to IS NULL OR effective_to>=?)""",
        (registration_id, organization_id, period_start, period_end),
    ).fetchone()
    if registration is None:
        raise AccountingError(
            "tax obligation requires an active registration covering the period"
        )
    existing = conn.execute(
        """SELECT * FROM tax_obligations
           WHERE registration_id=? AND period_start=? AND period_end=?""",
        (registration_id, period_start, period_end),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["organization_id"]) == organization_id
            and int(existing["due_at"]) == due_at
            and int(existing["amount_minor"]) == amount_minor
            and str(existing["currency"]).upper() == currency.upper()
        ):
            return str(existing["id"])
        raise AccountingError(
            "tax obligation period already exists with different assessed facts"
        )
    obligation_id = _id("taxdue")
    now = int(time.time())
    with conn:
        conn.execute(
            """INSERT INTO tax_obligations
               (id, organization_id, registration_id, period_start, period_end,
                due_at, amount_minor, currency, status, evidence_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accrued', ?)""",
            (
                obligation_id, organization_id, registration_id, period_start,
                period_end, due_at, amount_minor, currency.upper(), _json(evidence),
            ),
        )
        conn.execute(
            """INSERT INTO tax_obligation_events
               (id,obligation_id,event_type,evidence_json,occurred_at)
               VALUES (?,?,'assessed',?,?)""",
            (_id("taxevent"), obligation_id, _json(evidence), now),
        )
    return obligation_id


def record_tax_filing(
    conn: sqlite3.Connection,
    obligation_id: str,
    *,
    filed_at: int,
    evidence: Any,
) -> str:
    """Record an externally evidenced filing without rewriting its assessment."""
    ensure_schema(conn)
    if filed_at <= 0 or not evidence:
        raise AccountingError("tax filing requires time and authority evidence")
    obligation = conn.execute(
        "SELECT * FROM tax_obligations WHERE id=?", (obligation_id,)
    ).fetchone()
    if obligation is None:
        raise KeyError(f"tax obligation not found: {obligation_id}")
    existing = conn.execute(
        """SELECT id FROM tax_obligation_events
           WHERE obligation_id=? AND event_type='filed'""",
        (obligation_id,),
    ).fetchone()
    if existing is not None:
        if int(obligation["filed_at"] or 0) == filed_at:
            return str(existing["id"])
        raise AccountingError("tax filing already exists with a different time")
    event_id = _id("taxevent")
    next_status = "paid" if obligation["status"] == "paid" else "filed"
    with conn:
        conn.execute(
            """UPDATE tax_obligations SET status=?,filed_at=?
               WHERE id=?""",
            (next_status, filed_at, obligation_id),
        )
        conn.execute(
            """INSERT INTO tax_obligation_events
               (id,obligation_id,event_type,evidence_json,occurred_at)
               VALUES (?,?,'filed',?,?)""",
            (event_id, obligation_id, _json(evidence), filed_at),
        )
    return event_id


def record_tax_payment(
    conn: sqlite3.Connection,
    obligation_id: str,
    *,
    paid_at: int,
    payment_intent_id: str,
    evidence: Any,
) -> str:
    """Bind a tax obligation to an exact successful provider payment."""
    ensure_schema(conn)
    if paid_at <= 0 or not payment_intent_id or not evidence:
        raise AccountingError(
            "tax payment requires time, payment reference, and provider evidence"
        )
    obligation = conn.execute(
        "SELECT * FROM tax_obligations WHERE id=?", (obligation_id,)
    ).fetchone()
    if obligation is None:
        raise KeyError(f"tax obligation not found: {obligation_id}")
    existing = conn.execute(
        """SELECT id,evidence_json FROM tax_obligation_events
           WHERE obligation_id=? AND event_type='paid'""",
        (obligation_id,),
    ).fetchone()
    if existing is not None:
        stored = json.loads(existing["evidence_json"])
        if (
            int(obligation["paid_at"] or 0) == paid_at
            and stored.get("payment_intent_id") == payment_intent_id
        ):
            return str(existing["id"])
        raise AccountingError("tax payment already exists with different facts")
    amount_minor = int(obligation["amount_minor"])
    if amount_minor == 0:
        if payment_intent_id != "not_required:zero_balance":
            raise AccountingError(
                "zero tax obligation must use not_required:zero_balance"
            )
    else:
        payment_table = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='payment_intents'"""
        ).fetchone()
        payment = (
            conn.execute(
                """SELECT * FROM payment_intents
                   WHERE id=? AND organization_id=? AND direction='outgoing'
                     AND status='succeeded'""",
                (
                    payment_intent_id,
                    obligation["organization_id"],
                ),
            ).fetchone()
            if payment_table is not None
            else None
        )
        if (
            payment is None
            or int(payment["amount_minor"]) != amount_minor
            or str(payment["currency"]).upper()
            != str(obligation["currency"]).upper()
        ):
            raise AccountingError(
                "tax payment requires an exact successful provider payment"
            )
    event_id = _id("taxevent")
    event_evidence = {
        **(
            dict(evidence)
            if isinstance(evidence, Mapping)
            else {"evidence": evidence}
        ),
        "payment_intent_id": payment_intent_id,
    }
    with conn:
        conn.execute(
            """UPDATE tax_obligations SET status='paid',paid_at=?
               WHERE id=?""",
            (paid_at, obligation_id),
        )
        conn.execute(
            """INSERT INTO tax_obligation_events
               (id,obligation_id,event_type,evidence_json,occurred_at)
               VALUES (?,?,'paid',?,?)""",
            (event_id, obligation_id, _json(event_evidence), paid_at),
        )
    return event_id


def close_fiscal_period(
    conn: sqlite3.Connection,
    period_id: str,
    *,
    evidence: Any,
    closed_at: Optional[int] = None,
) -> None:
    ensure_schema(conn)
    if not evidence:
        raise AccountingError("closing a fiscal period requires supporting evidence")
    period = conn.execute(
        "SELECT * FROM fiscal_periods WHERE id = ?", (period_id,)
    ).fetchone()
    if period is None:
        raise KeyError(f"fiscal period not found: {period_id}")
    if period["status"] == "closed":
        return
    if period["status"] != "open":
        raise AccountingError("only an open fiscal period can be closed")
    statements = financial_statements(conn, period["organization_id"])
    if not statements["balance_sheet"]["balanced"]:
        raise AccountingError("cannot close an unbalanced fiscal period")
    missing_tax = conn.execute(
        """SELECT COUNT(*) AS n FROM tax_registrations r
           WHERE r.organization_id = ? AND r.status = 'active'
             AND NOT EXISTS (
               SELECT 1 FROM tax_obligations o
               WHERE o.registration_id = r.id AND o.period_start <= ?
                 AND o.period_end >= ?
             )""",
        (period["organization_id"], period["starts_at"], period["ends_at"]),
    ).fetchone()["n"]
    if missing_tax:
        raise AccountingError(
            "cannot close period before every active tax registration is assessed"
        )
    with conn:
        conn.execute(
            "UPDATE fiscal_periods SET status = 'closed', closed_at = ? WHERE id = ?",
            (closed_at or int(time.time()), period_id),
        )
        conn.execute(
            """INSERT INTO fiscal_period_events
               (id,period_id,event_type,evidence_json,occurred_at)
               VALUES (?,?,'closed',?,?)""",
            (
                _id("periodevent"),
                period_id,
                _json(evidence),
                closed_at or int(time.time()),
            ),
        )
