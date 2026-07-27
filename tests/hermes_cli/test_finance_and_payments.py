from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import (
    accounting_db,
    compliance_db,
    finance_db,
    payment_controls,
    usage_billing,
)
from hermes_cli import objectives_db
from hermes_cli.payments import (
    PaymentRail,
    PaymentService,
    ProviderPayment,
    UncertainProviderAction,
)


def test_finance_schema_read_preserves_active_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    finance_db.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO treasury_accounts "
        "(id, organization_id, name, currency, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("acct_txn", "org_txn", "operating", "USD", 1),
    )
    assert finance_db.ensure_schema(conn) is None
    assert conn.in_transaction is True
    conn.rollback()


def test_payment_control_schema_read_preserves_active_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    payment_controls.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    payment_controls.ensure_schema(conn)
    assert conn.in_transaction is True
    conn.rollback()


def test_payment_and_metering_schema_reads_preserve_active_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    PaymentService(conn, {})
    usage_billing.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    PaymentService(conn, {})
    usage_billing.ensure_schema(conn)
    assert conn.in_transaction is True
    conn.rollback()


@pytest.mark.parametrize(
    "payment",
    [
        ProviderPayment("", "succeeded", 100, "USD"),
        ProviderPayment("provider-1", "", 100, "USD"),
        ProviderPayment("provider-1", "succeeded", True, "USD"),
        ProviderPayment("provider-1", "succeeded", 100, "US"),
    ],
)
def test_payment_provider_readback_rejects_malformed_identity_and_money(payment):
    with pytest.raises(ValueError):
        PaymentService._validate_remote(payment, 100, "USD")


class FakeRail(PaymentRail):
    name = "fake"

    def __init__(self):
        self.payments = {}

    def create_receivable(self, **kwargs):
        payment = ProviderPayment(
            "incoming-1",
            "pending",
            kwargs["amount_minor"],
            kwargs["currency"],
            "https://pay.example/incoming-1",
        )
        self.payments[payment.reference] = payment
        return payment

    def send_payment(self, **kwargs):
        payment = ProviderPayment(
            "outgoing-1",
            "succeeded",
            kwargs["amount_minor"],
            kwargs["currency"],
            evidence={"provider_read_back": True},
        )
        self.payments[payment.reference] = payment
        return payment

    def get_payment(self, reference):
        return self.payments[reference]


class InterruptedRail(FakeRail):
    def __init__(self):
        super().__init__()
        self.send_attempts = 0

    def send_payment(self, **kwargs):
        self.send_attempts += 1
        payment = super().send_payment(**kwargs)
        raise UncertainProviderAction(
            "provider response lost after acceptance",
            provider_reference=payment.reference,
            evidence={"phase": "after_provider_acceptance"},
        )


@pytest.fixture
def treasury(tmp_path):
    conn = objectives_db.connect(tmp_path / "business.db")
    account = finance_db.create_treasury_account(
        conn, organization_id="org_1", currency="USD"
    )
    compliance_db.configure_profile(
        conn,
        organization_id="org_1",
        legal_entity_type="corporation",
        home_jurisdiction="CA-ON",
    )
    for direction in ("inbound", "outbound"):
        compliance_db.verify_payment_provider(
            conn,
            organization_id="org_1",
            provider="fake",
            direction=direction,
            jurisdiction="GLOBAL",
            registry_authority="test-registry",
            registry_reference=f"fake-{direction}",
            aml_screening_delegated=True,
            sanctions_screening_delegated=True,
            verified_at=int(time.time()) - 1,
            expires_at=int(time.time()) + 3600,
            evidence={"test": True},
        )
    return conn, account


def test_initial_capital_has_ten_dollar_floor_and_no_upper_cap(treasury):
    conn, account = treasury
    with pytest.raises(finance_db.BudgetError, match=r"at least \$10"):
        finance_db.seed_initial_capital(
            conn, account_id=account, amount_minor=999, currency="USD", actor="human"
        )
    finance_db.seed_initial_capital(
        conn,
        account_id=account,
        amount_minor=10**15,
        currency="USD",
        actor="human",
    )
    assert finance_db.account_balance(conn, account) == 10**15


def test_provider_assessment_rejects_future_dated_evidence(tmp_path):
    conn = objectives_db.connect(tmp_path / "business.db")
    with pytest.raises(
        compliance_db.ComplianceError, match="future-dated"
    ):
        compliance_db.verify_payment_provider(
            conn,
            organization_id="org_1",
            provider="fake",
            direction="inbound",
            jurisdiction="GLOBAL",
            registry_authority="test-registry",
            registry_reference="future-assessment",
            aml_screening_delegated=True,
            sanctions_screening_delegated=True,
            verified_at=int(time.time()) + 60,
            expires_at=int(time.time()) + 3_600,
            evidence={"test": True},
        )


def test_provider_assessment_rejects_expired_evidence_at_admission(tmp_path):
    conn = objectives_db.connect(tmp_path / "business.db")
    now = int(time.time())
    with pytest.raises(
        compliance_db.ComplianceError, match="already expired"
    ):
        compliance_db.verify_payment_provider(
            conn,
            organization_id="org_1",
            provider="fake",
            direction="outbound",
            jurisdiction="GLOBAL",
            registry_authority="test-registry",
            registry_reference="expired-assessment",
            aml_screening_delegated=True,
            sanctions_screening_delegated=True,
            verified_at=now - 60,
            expires_at=now - 1,
            evidence={"test": True},
        )


def test_provider_assessment_requires_auditable_registry_reference(tmp_path):
    conn = objectives_db.connect(tmp_path / "business.db")
    with pytest.raises(
        compliance_db.ComplianceError, match="registry_reference"
    ):
        compliance_db.verify_payment_provider(
            conn,
            organization_id="org_1",
            provider="fake",
            direction="inbound",
            jurisdiction="GLOBAL",
            registry_authority="test-registry",
            registry_reference="",
            aml_screening_delegated=True,
            sanctions_screening_delegated=True,
            verified_at=int(time.time()) - 1,
            expires_at=int(time.time()) + 3_600,
            evidence={"test": True},
        )


def test_reservations_prevent_oversubscription_and_exact_settlement(treasury):
    conn, account = treasury
    finance_db.seed_initial_capital(
        conn, account_id=account, amount_minor=10_000, currency="USD", actor="human"
    )
    finance_db.reserve_budget(
        conn,
        account_id=account,
        objective_id="obj_1",
        action_id="act_1",
        amount_minor=8_000,
        currency="USD",
        expires_at=9999999999,
    )
    with pytest.raises(finance_db.BudgetError, match="different budget reservation"):
        finance_db.reserve_budget(
            conn,
            account_id=account,
            objective_id="obj_changed",
            action_id="act_1",
            amount_minor=8_000,
            currency="USD",
            expires_at=9999999999,
        )
    with pytest.raises(finance_db.BudgetError, match="insufficient"):
        finance_db.reserve_budget(
            conn,
            account_id=account,
            objective_id="obj_2",
            action_id="act_2",
            amount_minor=3_000,
            currency="USD",
            expires_at=9999999999,
        )
    with pytest.raises(finance_db.BudgetError, match="exceeds"):
        finance_db.settle_reservation(
            conn,
            action_id="act_1",
            actual_amount_minor=8_001,
            external_reference="provider-1",
            evidence={},
        )
    finance_db.settle_reservation(
        conn,
        action_id="act_1",
        actual_amount_minor=7_500,
        external_reference="provider-1",
        evidence={"read_back": True},
    )
    assert finance_db.account_balance(conn, account) == 2_500


def test_objective_cumulative_budget_is_atomic(treasury):
    conn, account = treasury
    finance_db.seed_initial_capital(
        conn, account_id=account, amount_minor=2_000, currency="USD", actor="human"
    )
    finance_db.reserve_budget(
        conn, account_id=account, objective_id="obj_cap", action_id="act_a",
        amount_minor=600, currency="USD", expires_at=9_999_999_999,
        objective_budget_minor=1_000,
    )
    with pytest.raises(finance_db.BudgetError, match="cumulative budget"):
        finance_db.reserve_budget(
            conn, account_id=account, objective_id="obj_cap", action_id="act_b",
            amount_minor=401, currency="USD", expires_at=9_999_999_999,
            objective_budget_minor=1_000,
        )
    # A released reservation no longer consumes the objective ceiling.
    finance_db.release_reservation(conn, "act_a", reason="test")
    finance_db.reserve_budget(
        conn, account_id=account, objective_id="obj_cap", action_id="act_c",
        amount_minor=1_000, currency="USD", expires_at=9_999_999_999,
        objective_budget_minor=1_000,
    )


def test_concurrent_objective_budget_reservations_are_serialized(tmp_path):
    path = tmp_path / "objective-budget.db"
    conn = objectives_db.connect(path)
    account = finance_db.create_treasury_account(
        conn, organization_id="org_1", currency="USD"
    )
    finance_db.seed_initial_capital(
        conn, account_id=account, amount_minor=2_000, currency="USD", actor="human"
    )
    conn.close()

    def reserve(action_id):
        worker = objectives_db.connect(path)
        try:
            finance_db.reserve_budget(
                worker, account_id=account, objective_id="obj_cap",
                action_id=action_id, amount_minor=600, currency="USD",
                expires_at=9_999_999_999, objective_budget_minor=1_000,
            )
            return "reserved"
        except finance_db.BudgetError:
            return "rejected"
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ("act_1", "act_2")))
    assert sorted(outcomes) == ["rejected", "reserved"]


def test_concurrent_payment_reservations_cannot_double_allocate_cash(tmp_path):
    path = tmp_path / "business.db"
    conn = objectives_db.connect(path)
    account = finance_db.create_treasury_account(
        conn, organization_id="org_1", currency="USD"
    )
    finance_db.seed_initial_capital(
        conn,
        account_id=account,
        amount_minor=1_000,
        currency="USD",
        actor="human",
    )
    conn.close()

    def reserve(action_id):
        worker = objectives_db.connect(path)
        try:
            finance_db.reserve_budget(
                worker,
                account_id=account,
                objective_id="obj_1",
                action_id=action_id,
                amount_minor=600,
                currency="USD",
                expires_at=9_999_999_999,
            )
            return "reserved"
        except finance_db.BudgetError:
            return "rejected"
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ("action-1", "action-2")))
    assert sorted(outcomes) == ["rejected", "reserved"]
    restarted = objectives_db.connect(path)
    assert finance_db.reserved_balance(restarted, account) == 600
    assert finance_db.available_balance(restarted, account) == 400


def test_business_accepts_payment_only_after_provider_readback(treasury):
    conn, account = treasury
    rail = FakeRail()
    service = PaymentService(conn, {"fake": rail})
    intent = service.create_receivable(
        organization_id="org_1",
        account_id=account,
        provider="fake",
        amount_minor=2_500,
        currency="USD",
        customer={"email": "buyer@example.com"},
        customer_jurisdiction="CA-ON",
        purpose="Consulting",
        idempotency_key="invoice-1",
    )
    assert service.create_receivable(
        organization_id="org_1",
        account_id=account,
        provider="fake",
        amount_minor=2_500,
        currency="USD",
        customer={"email": "buyer@example.com"},
        customer_jurisdiction="CA-ON",
        purpose="Consulting",
        idempotency_key="invoice-1",
    )["id"] == intent["id"]
    with pytest.raises(ValueError, match="different payment parameters"):
        service.create_receivable(
            organization_id="org_1",
            account_id=account,
            provider="fake",
            amount_minor=2_501,
            currency="USD",
            customer={"email": "buyer@example.com"},
            customer_jurisdiction="CA-ON",
            purpose="Consulting",
            idempotency_key="invoice-1",
        )
    assert intent["payment_url"]
    assert finance_db.account_balance(conn, account) == 0

    rail.payments["incoming-1"] = ProviderPayment(
        "incoming-1",
        "succeeded",
        2_500,
        "USD",
        evidence={"event_id": "evt_paid"},
    )
    service.reconcile(intent["id"])
    service.reconcile(intent["id"])
    assert finance_db.account_balance(conn, account) == 2_500
    readbacks = conn.execute(
        """SELECT * FROM payment_provider_readbacks
           WHERE payment_intent_id=? ORDER BY observed_at,id""",
        (intent["id"],),
    ).fetchall()
    assert len(readbacks) == 1
    assert '"event_id": "evt_paid"' in readbacks[0]["evidence_json"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE payment_provider_readbacks SET status='failed' WHERE id=?",
            (readbacks[0]["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "DELETE FROM payment_provider_readbacks WHERE id=?",
            (readbacks[0]["id"],),
        )


def test_treasury_idempotency_rejects_entry_parameter_drift(treasury):
    conn, account = treasury
    first = finance_db.record_entry(
        conn,
        account_id=account,
        kind="customer_payment",
        amount_minor=250,
        currency="USD",
        idempotency_key="ledger-retry-1",
        objective_id="obj-1",
        action_id="act-1",
        external_reference="provider-1",
        evidence={"source": "test"},
    )
    assert finance_db.record_entry(
        conn,
        account_id=account,
        kind="customer_payment",
        amount_minor=250,
        currency="USD",
        idempotency_key="ledger-retry-1",
        objective_id="obj-1",
        action_id="act-1",
        external_reference="provider-1",
        evidence={"source": "replay"},
    ) == first
    with pytest.raises(finance_db.BudgetError, match="different entry parameters"):
        finance_db.record_entry(
            conn,
            account_id=account,
            kind="customer_payment",
            amount_minor=251,
            currency="USD",
            idempotency_key="ledger-retry-1",
            objective_id="obj-1",
            action_id="act-1",
            external_reference="provider-1",
            evidence={"source": "drift"},
        )


def test_tax_bearing_invoice_accrues_tax_and_revenue_separately(treasury):
    conn, account = treasury
    service = PaymentService(conn, {"fake": FakeRail()})
    registration = accounting_db.configure_tax_registration(
        conn,
        organization_id="org_1",
        jurisdiction="US-PA",
        tax_type="sales",
        filing_frequency="quarterly",
        effective_from=int(time.time()) - 1,
        evidence={"registration": "verified"},
    )
    tax_rule = accounting_db.configure_tax_rate(
        conn,
        registration_id=registration,
        tax_code="standard",
        rate_basis_points=600,
        effective_from=int(time.time()) - 1,
        authority_source="https://authority.example",
        verified_at=int(time.time()),
    )
    calculated, effective_rule = accounting_db.calculate_tax_for_rule(
        conn,
        organization_id="org_1",
        tax_rule_id=tax_rule,
        jurisdiction="US-PA",
        taxable_minor=2_500,
        occurred_at=int(time.time()),
    )
    assert (calculated, effective_rule) == (150, tax_rule)
    with pytest.raises(accounting_db.AccountingError, match="active"):
        accounting_db.calculate_tax_for_rule(
            conn,
            organization_id="org_1",
            tax_rule_id=tax_rule,
            jurisdiction="CA-ON",
            taxable_minor=2_500,
            occurred_at=int(time.time()),
        )
    intent = service.create_receivable(
        organization_id="org_1",
        account_id=account,
        provider="fake",
        amount_minor=2_650,
        currency="USD",
        customer={"email": "buyer@example.com"},
        customer_jurisdiction="CA-ON",
        purpose="Taxable service",
        idempotency_key="invoice-tax-1",
        tax_minor=150,
        tax_rule_id=tax_rule,
    )
    assert intent["tax_minor"] == 150
    statements = accounting_db.financial_statements(conn, "org_1")
    assert statements["profit_and_loss"]["revenue_minor"] == 2_500
    assert statements["tax_liability_minor"] == 150
    with pytest.raises(accounting_db.AccountingError, match="verified tax rule"):
        service.create_receivable(
            organization_id="org_1",
            account_id=account,
            provider="fake",
            amount_minor=1_060,
            currency="USD",
            customer={},
            customer_jurisdiction="CA-ON",
            purpose="Invalid tax",
            idempotency_key="invoice-tax-invalid",
            tax_minor=60,
        )


def test_business_can_pay_only_against_exact_reservation(treasury):
    conn, account = treasury
    finance_db.seed_initial_capital(
        conn, account_id=account, amount_minor=5_000, currency="USD", actor="human"
    )
    service = PaymentService(conn, {"fake": FakeRail()})
    instrument = payment_controls.register_tokenized_instrument(
        conn,
        organization_id="org_1",
        provider="fake",
        provider_instrument_id="provider-card-token-1",
        rail_type="virtual_card",
        currency="USD",
        label="CEO procurement card",
        last4="4242",
    )
    payment_controls.set_spend_controls(
        conn,
        instrument_id=instrument,
        max_transaction_minor=2_000,
        max_daily_minor=3_000,
        allowed_merchant_categories=["software"],
        allowed_payees=["vendor-1"],
        policy_version="finance-v1",
    )
    with pytest.raises(finance_db.BudgetError, match="exact open"):
        service.send_payable(
            organization_id="org_1",
            account_id=account,
            objective_id="obj_1",
            action_id="act_1",
            provider="fake",
            amount_minor=1_000,
            currency="USD",
            payee={"account": "vendor"},
            payee_jurisdiction="US",
            instrument_id=instrument,
            merchant_category="software",
            payee_id="vendor-1",
            purpose="Contract work",
            idempotency_key="pay-1",
        )
    finance_db.reserve_budget(
        conn,
        account_id=account,
        objective_id="obj_1",
        action_id="act_1",
        amount_minor=1_000,
        currency="USD",
        expires_at=9999999999,
    )
    result = service.send_payable(
        organization_id="org_1",
        account_id=account,
        objective_id="obj_1",
        action_id="act_1",
        provider="fake",
        amount_minor=1_000,
        currency="USD",
        payee={"account": "vendor"},
        payee_jurisdiction="US",
        instrument_id=instrument,
        merchant_category="software",
        payee_id="vendor-1",
        purpose="Contract work",
        idempotency_key="pay-1",
    )
    assert result["status"] == "succeeded"
    assert service.send_payable(
        organization_id="org_1", account_id=account, objective_id="obj_1",
        action_id="act_1", provider="fake", amount_minor=1_000,
        currency="USD", payee={"account": "vendor"}, payee_jurisdiction="US",
        instrument_id=instrument, merchant_category="software",
        payee_id="vendor-1", purpose="Contract work", idempotency_key="pay-1",
    )["id"] == result["id"]
    with pytest.raises(ValueError, match="different payment parameters"):
        service.send_payable(
            organization_id="org_1", account_id=account, objective_id="obj_1",
            action_id="act_1", provider="fake", amount_minor=999,
            currency="USD", payee={"account": "vendor"}, payee_jurisdiction="US",
            instrument_id=instrument, merchant_category="software",
            payee_id="vendor-1", purpose="Contract work", idempotency_key="pay-1",
        )
    assert conn.execute(
        "SELECT status FROM payment_spend_holds WHERE action_id=?", ("act_1",)
    ).fetchone()["status"] == "settled"
    assert finance_db.account_balance(conn, account) == 4_000
    registration = accounting_db.configure_tax_registration(
        conn,
        organization_id="org_1",
        jurisdiction="CA-ON",
        tax_type="income",
        filing_frequency="annual",
        effective_from=100,
        evidence={"authority": "CRA"},
    )
    obligation = accounting_db.record_tax_obligation(
        conn,
        organization_id="org_1",
        registration_id=registration,
        period_start=100,
        period_end=199,
        due_at=250,
        amount_minor=1_000,
        currency="USD",
        evidence={"workpaper": "income-tax"},
    )
    payment_event = accounting_db.record_tax_payment(
        conn,
        obligation,
        organization_id="org_1",
        paid_at=220,
        payment_intent_id=result["id"],
        evidence={"provider_readback_id": result["provider_readback_id"]},
    )
    assert payment_event.startswith("taxevent_")
    assert conn.execute(
        "SELECT status FROM tax_obligations WHERE id=?", (obligation,)
    ).fetchone()["status"] == "paid"


def test_concurrent_spend_controls_reserve_daily_velocity_atomically(tmp_path):
    path = tmp_path / "spend-velocity.db"
    conn = objectives_db.connect(path)
    instrument = payment_controls.register_tokenized_instrument(
        conn,
        organization_id="org_1",
        provider="fake",
        provider_instrument_id="provider-card-token-velocity",
        rail_type="virtual_card",
        currency="USD",
        label="Velocity test card",
    )
    payment_controls.set_spend_controls(
        conn,
        instrument_id=instrument,
        max_transaction_minor=1_000,
        max_daily_minor=1_000,
        allowed_merchant_categories=[],
        allowed_payees=[],
        policy_version="finance-v1",
    )
    conn.close()

    def authorize(action_id):
        worker = objectives_db.connect(path)
        try:
            payment_controls.authorize_spend(
                worker,
                instrument_id=instrument,
                provider="fake",
                amount_minor=600,
                currency="USD",
                merchant_category="software",
                payee_id="vendor-velocity",
                action_id=action_id,
            )
            return "reserved"
        except payment_controls.SpendControlError:
            return "rejected"
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(authorize, ("velocity-a", "velocity-b")))
    assert sorted(outcomes) == ["rejected", "reserved"]
    conn = objectives_db.connect(path)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM payment_spend_holds WHERE status='reserved'"
    ).fetchone()["n"] == 1


def test_raw_financial_credentials_are_rejected(treasury):
    conn, _ = treasury
    with pytest.raises(payment_controls.SpendControlError, match="raw financial"):
        payment_controls.register_tokenized_instrument(
            conn,
            organization_id="org_1",
            provider="fake",
            provider_instrument_id="tok_1",
            rail_type="virtual_card",
            currency="USD",
            label="unsafe",
            metadata={"card_number": "4242424242424242"},
        )


def test_interrupted_outbound_payment_converges_from_readback_without_replay(
    treasury,
):
    conn, account = treasury
    finance_db.seed_initial_capital(
        conn, account_id=account, amount_minor=5_000, currency="USD", actor="human"
    )
    rail = InterruptedRail()
    service = PaymentService(conn, {"fake": rail})
    instrument = payment_controls.register_tokenized_instrument(
        conn,
        organization_id="org_1",
        provider="fake",
        provider_instrument_id="provider-card-token-uncertain",
        rail_type="virtual_card",
        currency="USD",
        label="Interrupted provider card",
    )
    payment_controls.set_spend_controls(
        conn,
        instrument_id=instrument,
        max_transaction_minor=2_000,
        max_daily_minor=3_000,
        allowed_merchant_categories=["software"],
        allowed_payees=["vendor-uncertain"],
        policy_version="finance-v1",
    )
    finance_db.reserve_budget(
        conn,
        account_id=account,
        objective_id="obj-uncertain",
        action_id="act-uncertain",
        amount_minor=1_000,
        currency="USD",
        expires_at=9_999_999_999,
    )

    uncertain = service.send_payable(
        organization_id="org_1",
        account_id=account,
        objective_id="obj-uncertain",
        action_id="act-uncertain",
        provider="fake",
        amount_minor=1_000,
        currency="USD",
        payee={"account": "vendor-uncertain"},
        payee_jurisdiction="US",
        instrument_id=instrument,
        merchant_category="software",
        payee_id="vendor-uncertain",
        purpose="Interrupted provider action",
        idempotency_key="pay-uncertain-0001",
    )
    assert uncertain["status"] == "uncertain"
    assert uncertain["provider_reference"] == "outgoing-1"
    assert rail.send_attempts == 1
    assert finance_db.account_balance(conn, account) == 5_000

    # Restart the local service and reconcile only through provider read-back.
    database_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    restarted = objectives_db.connect(database_path)
    try:
        resumed = PaymentService(restarted, {"fake": rail})
        settled = resumed.reconcile(uncertain["id"])
        assert settled["status"] == "succeeded"
        assert finance_db.account_balance(restarted, account) == 4_000
        assert restarted.execute(
            "SELECT status FROM budget_reservations WHERE action_id=?",
            ("act-uncertain",),
        ).fetchone()["status"] == "settled"
        assert restarted.execute(
            "SELECT status FROM payment_spend_holds WHERE action_id=?",
            ("act-uncertain",),
        ).fetchone()["status"] == "settled"

        # A retry with the same idempotency key returns the durable intent and
        # never calls the provider a second time.
        retry = resumed.send_payable(
            organization_id="org_1",
            account_id=account,
            objective_id="obj-uncertain",
            action_id="act-uncertain",
            provider="fake",
            amount_minor=1_000,
            currency="USD",
            payee={"account": "vendor-uncertain"},
            payee_jurisdiction="US",
            instrument_id=instrument,
            merchant_category="software",
            payee_id="vendor-uncertain",
            purpose="Interrupted provider action",
            idempotency_key="pay-uncertain-0001",
        )
        assert retry["id"] == uncertain["id"]
        assert retry["status"] == "succeeded"
        assert rail.send_attempts == 1
        assert finance_db.account_balance(restarted, account) == 4_000
        assert restarted.execute(
            "SELECT COUNT(*) AS n FROM payment_provider_readbacks WHERE payment_intent_id=?",
            (uncertain["id"],),
        ).fetchone()["n"] == 1
    finally:
        restarted.close()
