import sqlite3
import time

import pytest

from hermes_cli import accounting_db


@pytest.fixture
def conn():
    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    accounting_db.ensure_schema(value)
    yield value
    value.close()


def test_accounting_schema_read_preserves_active_transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO fiscal_periods "
        "(id, organization_id, name, starts_at, ends_at, evidence_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("period_txn", "org_txn", "FY", 1, 2, '{"source":"test"}'),
    )
    accounting_db.ensure_schema(conn)
    assert conn.in_transaction is True
    assert conn.execute(
        "SELECT evidence_json FROM fiscal_periods WHERE id=?", ("period_txn",)
    ).fetchone()[0] == '{"source":"test"}'
    conn.rollback()


def test_journal_is_balanced_idempotent_and_immutable(conn):
    entry = accounting_db.post_journal(
        conn,
        organization_id="org_1",
        description="Founder capital",
        source_type="test",
        source_id="capital-1",
        currency="USD",
        lines=(
            {"account_code": "1000", "debit_minor": 1000},
            {"account_code": "3000", "credit_minor": 1000},
        ),
        evidence={"receipt": "bank-readback"},
    )
    assert accounting_db.post_journal(
        conn,
        organization_id="org_1",
        description="Founder capital",
        source_type="test",
        source_id="capital-1",
        currency="USD",
        lines=(
            {"account_code": "1000", "debit_minor": 1000},
            {"account_code": "3000", "credit_minor": 1000},
        ),
        evidence={},
    ) == entry
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE journal_entries SET description = 'changed' WHERE id = ?", (entry,))
    with pytest.raises(accounting_db.AccountingError, match="balanced"):
        accounting_db.post_journal(
            conn,
            organization_id="org_1",
            description="bad",
            source_type="test",
            source_id="bad",
            currency="USD",
            lines=({"account_code": "1000", "debit_minor": 100},),
            evidence={},
        )
    statements = accounting_db.financial_statements(conn, "org_1")
    assert statements["balance_sheet"]["balanced"] is True
    assert statements["balance_sheet"]["assets_minor"] == 1000


def test_journal_retry_rejects_source_parameter_drift(conn):
    accounting_db.post_journal(
        conn,
        organization_id="org_1",
        description="Founder capital",
        source_type="payment",
        source_id="payment-1",
        currency="USD",
        lines=(
            {"account_code": "1000", "debit_minor": 1000},
            {"account_code": "3000", "credit_minor": 1000},
        ),
        evidence={"provider": "test"},
    )
    with pytest.raises(accounting_db.AccountingError, match="different parameters"):
        accounting_db.post_journal(
            conn,
            organization_id="org_1",
            description="Founder capital",
            source_type="payment",
            source_id="payment-1",
            currency="USD",
            lines=(
                {"account_code": "1000", "debit_minor": 1100},
                {"account_code": "3000", "credit_minor": 1100},
            ),
            evidence={"provider": "test"},
        )
    with pytest.raises(accounting_db.AccountingError, match="different parameters"):
        accounting_db.post_journal(
            conn,
            organization_id="org_1",
            description="Changed description",
            source_type="payment",
            source_id="payment-1",
            currency="USD",
            lines=(
                {"account_code": "1000", "debit_minor": 1000},
                {"account_code": "3000", "credit_minor": 1000},
            ),
            evidence={"provider": "test"},
        )


def test_tax_calculation_requires_verified_effective_rule(conn):
    now = int(time.time())
    with pytest.raises(accounting_db.AccountingError, match="no verified tax rule"):
        accounting_db.calculate_tax(
            conn, organization_id="org_1", jurisdiction="US-PA",
            tax_type="sales", tax_code="standard", taxable_minor=1000, occurred_at=now,
        )
    registration = accounting_db.configure_tax_registration(
        conn, organization_id="org_1", jurisdiction="US-PA", tax_type="sales",
        filing_frequency="quarterly", effective_from=now - 1,
        evidence={"authority": "state registration"},
    )
    rule = accounting_db.configure_tax_rate(
        conn, registration_id=registration, tax_code="standard",
        rate_basis_points=600, effective_from=now - 1,
        authority_source="https://authority.example/tax", verified_at=now,
    )
    assert accounting_db.calculate_tax(
        conn, organization_id="org_1", jurisdiction="US-PA",
        tax_type="sales", tax_code="standard", taxable_minor=1000, occurred_at=now,
    ) == (60, rule)


def test_tax_rate_supersession_is_immutable_and_current_only(conn):
    now = int(time.time())
    registration = accounting_db.configure_tax_registration(
        conn, organization_id="org_1", jurisdiction="US-PA", tax_type="sales",
        filing_frequency="quarterly", effective_from=now - 10,
        evidence={"authority": "state registration"},
    )
    prior = accounting_db.configure_tax_rate(
        conn, registration_id=registration, tax_code="standard",
        rate_basis_points=600, effective_from=now - 10,
        authority_source="https://authority.example/tax", verified_at=now - 10,
    )
    replacement = accounting_db.configure_tax_rate(
        conn, registration_id=registration, tax_code="standard",
        rate_basis_points=700, effective_from=now - 5,
        authority_source="https://authority.example/tax-amended", verified_at=now - 5,
        supersedes_id=prior, supersession_reason="State rate amended",
    )
    assert accounting_db.calculate_tax(
        conn, organization_id="org_1", jurisdiction="US-PA",
        tax_type="sales", tax_code="standard", taxable_minor=1000,
        occurred_at=now,
    ) == (70, replacement)
    with pytest.raises(accounting_db.AccountingError, match="current record"):
        accounting_db.configure_tax_rate(
            conn, registration_id=registration, tax_code="standard",
            rate_basis_points=800, effective_from=now,
            authority_source="https://authority.example/tax-branch", verified_at=now,
            supersedes_id=prior, supersession_reason="Conflicting branch",
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE tax_rates SET rate_basis_points=1 WHERE id=?", (replacement,))


def test_fiscal_period_lifecycle_is_evidenced_idempotent_and_non_overlapping(conn):
    period = accounting_db.open_fiscal_period(
        conn,
        organization_id="org_1",
        name="2026-Q1",
        starts_at=1_767_225_600,
        ends_at=1_774_915_199,
        evidence={"calendar": "board-approved-2026"},
    )
    assert accounting_db.open_fiscal_period(
        conn,
        organization_id="org_1",
        name="2026-Q1",
        starts_at=1_767_225_600,
        ends_at=1_774_915_199,
        evidence={"retry": True},
    ) == period
    with pytest.raises(accounting_db.AccountingError, match="overlap"):
        accounting_db.open_fiscal_period(
            conn,
            organization_id="org_1",
            name="overlap",
            starts_at=1_770_000_000,
            ends_at=1_780_000_000,
            evidence={"calendar": "invalid"},
        )

    with pytest.raises(KeyError, match="fiscal period not found"):
        accounting_db.close_fiscal_period(
            conn,
            period,
            organization_id="org_2",
            evidence={"wrong_tenant": True},
        )
    accounting_db.close_fiscal_period(
        conn,
        period,
        organization_id="org_1",
        evidence={"trial_balance": "verified:2026-Q1"},
    )
    accounting_db.close_fiscal_period(
        conn,
        period,
        organization_id="org_1",
        evidence={"retry": True},
    )
    row = conn.execute(
        "SELECT status,closed_at FROM fiscal_periods WHERE id=?", (period,)
    ).fetchone()
    assert row["status"] == "closed"
    assert row["closed_at"] is not None
    assert {
        item["event_type"]
        for item in conn.execute(
            "SELECT event_type FROM fiscal_period_events WHERE period_id=?",
            (period,),
        )
    } == {"opened", "closed"}
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE fiscal_period_events SET event_type='changed' WHERE period_id=?",
            (period,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="contract is immutable"):
        conn.execute(
            "UPDATE fiscal_periods SET starts_at=starts_at+1 WHERE id=?",
            (period,),
        )


def test_tax_obligation_requires_covered_registration_and_retries_exactly(conn):
    registration = accounting_db.configure_tax_registration(
        conn,
        organization_id="org_1",
        jurisdiction="CA-ON",
        tax_type="sales",
        filing_frequency="quarterly",
        effective_from=100,
        evidence={"authority": "CRA registration"},
    )
    obligation = accounting_db.record_tax_obligation(
        conn,
        organization_id="org_1",
        registration_id=registration,
        period_start=100,
        period_end=199,
        due_at=250,
        amount_minor=1300,
        currency="CAD",
        evidence={"return_workpaper": "sha256:abc"},
    )
    assert accounting_db.record_tax_obligation(
        conn,
        organization_id="org_1",
        registration_id=registration,
        period_start=100,
        period_end=199,
        due_at=250,
        amount_minor=1300,
        currency="cad",
        evidence={"retry": True},
    ) == obligation
    with pytest.raises(accounting_db.AccountingError, match="different assessed"):
        accounting_db.record_tax_obligation(
            conn,
            organization_id="org_1",
            registration_id=registration,
            period_start=100,
            period_end=199,
            due_at=250,
            amount_minor=1400,
            currency="CAD",
            evidence={"changed": True},
        )
    with pytest.raises(accounting_db.AccountingError, match="active registration"):
        accounting_db.record_tax_obligation(
            conn,
            organization_id="org_2",
            registration_id=registration,
            period_start=100,
            period_end=199,
            due_at=250,
            amount_minor=1300,
            currency="CAD",
            evidence={"cross_tenant": True},
        )
    event = conn.execute(
        "SELECT * FROM tax_obligation_events WHERE obligation_id=?",
        (obligation,),
    ).fetchone()
    assert event["event_type"] == "assessed"
    with pytest.raises(sqlite3.IntegrityError, match="contract is immutable"):
        conn.execute(
            "UPDATE tax_obligations SET amount_minor=1 WHERE id=?",
            (obligation,),
        )
    filing_event = accounting_db.record_tax_filing(
        conn,
        obligation,
        organization_id="org_1",
        filed_at=300,
        evidence={"authority_receipt": "CRA:filed"},
    )
    assert accounting_db.record_tax_filing(
        conn,
        obligation,
        organization_id="org_1",
        filed_at=300,
        evidence={"retry": True},
    ) == filing_event
    with pytest.raises(sqlite3.IntegrityError, match="contract is immutable"):
        conn.execute(
            "UPDATE tax_obligations SET status='accrued' WHERE id=?",
            (obligation,),
        )
    zero_obligation = accounting_db.record_tax_obligation(
        conn,
        organization_id="org_1",
        registration_id=registration,
        period_start=200,
        period_end=299,
        due_at=350,
        amount_minor=0,
        currency="CAD",
        evidence={"return_workpaper": "zero"},
    )
    paid_event = accounting_db.record_tax_payment(
        conn,
        zero_obligation,
        organization_id="org_1",
        paid_at=320,
        payment_intent_id="not_required:zero_balance",
        evidence={"authority_balance": "zero"},
    )
    assert accounting_db.record_tax_payment(
        conn,
        zero_obligation,
        organization_id="org_1",
        paid_at=320,
        payment_intent_id="not_required:zero_balance",
        evidence={"retry": True},
    ) == paid_event
    assert conn.execute(
        "SELECT status FROM tax_obligations WHERE id=?", (zero_obligation,)
    ).fetchone()["status"] == "paid"


def test_tax_mutations_are_bound_to_organization(conn):
    registration = accounting_db.configure_tax_registration(
        conn,
        organization_id="org_1",
        jurisdiction="CA-ON",
        tax_type="sales",
        filing_frequency="quarterly",
        effective_from=1,
        evidence={"source": "test"},
    )
    obligation = accounting_db.record_tax_obligation(
        conn,
        organization_id="org_1",
        registration_id=registration,
        period_start=400,
        period_end=499,
        due_at=550,
        amount_minor=0,
        currency="CAD",
        evidence={"workpaper": "cross-tenant"},
    )
    with pytest.raises(KeyError, match="tax obligation not found"):
        accounting_db.record_tax_filing(
            conn,
            obligation,
            organization_id="org_2",
            filed_at=600,
            evidence={"authority_receipt": "wrong-tenant"},
        )
    assert conn.execute(
        "SELECT status FROM tax_obligations WHERE id=?", (obligation,)
    ).fetchone()["status"] == "accrued"


def test_existing_fiscal_period_schema_migrates_before_contract_trigger():
    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    legacy.execute(
        """CREATE TABLE fiscal_periods (
           id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
           name TEXT NOT NULL, starts_at INTEGER NOT NULL,
           ends_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',
           closed_at INTEGER, UNIQUE(organization_id,name))"""
    )
    accounting_db.ensure_schema(legacy)
    columns = {
        row["name"] for row in legacy.execute("PRAGMA table_info(fiscal_periods)")
    }
    assert "evidence_json" in columns
