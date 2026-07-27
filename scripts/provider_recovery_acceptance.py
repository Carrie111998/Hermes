#!/usr/bin/env python3
"""Provider interruption/recovery acceptance contract.

The fake rail durably records the provider effect and then raises an
uncertain-response exception. The recovery phase runs in a new process and
must settle only from provider read-back.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from hermes_cli import compliance_db, finance_db, objectives_db, payment_controls
from hermes_cli.payments import (
    InboundPaymentRail,
    OutboundPaymentRail,
    PaymentService,
    ProviderPayment,
    UncertainProviderAction,
)
from hermes_constants import get_hermes_home


ORG = "org_provider_recovery"
PROVIDER = "acceptance-provider"
OBJECTIVE = "obj_provider_recovery"
ACTION = "act_provider_recovery"
INSTRUMENT = "instrument_provider_recovery"
INTENT_MARKER = "provider-recovery-intent.json"
PROVIDER_STATE = "provider-recovery-provider.json"


def _home() -> Path:
    path = get_hermes_home().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path() -> Path:
    return _home() / PROVIDER_STATE


def _load_provider() -> dict:
    path = _state_path()
    if not path.exists():
        return {"send_attempts": 0, "payments": {}, "receivables": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_provider(value: dict) -> None:
    _state_path().write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class InterruptedRail(OutboundPaymentRail, InboundPaymentRail):
    name = PROVIDER

    def create_receivable(self, **kwargs) -> ProviderPayment:
        state = _load_provider()
        reference = "provider-receivable-0001"
        state.setdefault("receivables", {})[reference] = {
            "status": "succeeded",
            "amount_minor": int(kwargs["amount_minor"]),
            "currency": str(kwargs["currency"]).upper(),
            "evidence": {"provider_event": "receivable-created"},
        }
        _save_provider(state)
        return ProviderPayment(
            reference,
            "succeeded",
            int(kwargs["amount_minor"]),
            str(kwargs["currency"]).upper(),
            payment_url="https://provider.invalid/receivable/0001",
            evidence={"provider_event": "receivable-created"},
        )

    def send_payment(self, **kwargs) -> ProviderPayment:
        state = _load_provider()
        state["send_attempts"] = int(state.get("send_attempts", 0)) + 1
        state.setdefault("payments", {})["provider-payment-0001"] = {
            "status": "succeeded",
            "amount_minor": int(kwargs["amount_minor"]),
            "currency": str(kwargs["currency"]).upper(),
            "evidence": {"provider_event": "accepted-before-response-loss"},
        }
        _save_provider(state)
        raise UncertainProviderAction(
            "provider response lost after provider accepted the action",
            provider_reference="provider-payment-0001",
            evidence={"phase": "after_provider_acceptance"},
        )

    def get_payment(self, reference: str) -> ProviderPayment:
        provider = _load_provider()
        payment = (
            provider.get("payments", {}).get(reference)
            or provider.get("receivables", {}).get(reference)
        )
        if payment is None:
            raise KeyError(reference)
        return ProviderPayment(
            reference,
            str(payment["status"]),
            int(payment["amount_minor"]),
            str(payment["currency"]),
            evidence=payment.get("evidence"),
        )


def _service(conn: object) -> PaymentService:
    return PaymentService(conn, {PROVIDER: InterruptedRail()})


def _payment_args(account_id: str, instrument_id: str) -> dict:
    return {
        "organization_id": ORG,
        "account_id": account_id,
        "objective_id": OBJECTIVE,
        "action_id": ACTION,
        "provider": PROVIDER,
        "amount_minor": 1_000,
        "currency": "USD",
        "payee": {"account": "vendor-provider-recovery"},
        "payee_jurisdiction": "US",
        "instrument_id": instrument_id,
        "merchant_category": "software",
        "payee_id": "vendor-provider-recovery",
        "purpose": "Interrupted provider action acceptance",
        "idempotency_key": "provider-recovery-payment-0001",
    }


def interrupt() -> None:
    db_path = objectives_db.objectives_db_path()
    conn = objectives_db.connect(db_path)
    try:
        account_id = finance_db.create_treasury_account(
            conn, organization_id=ORG, currency="USD"
        )
        finance_db.seed_initial_capital(
            conn, account_id=account_id, amount_minor=5_000, currency="USD", actor="seed"
        )
        compliance_db.configure_profile(
            conn,
            organization_id=ORG,
            legal_entity_type="corporation",
            home_jurisdiction="CA-ON",
        )
        compliance_db.verify_payment_provider(
            conn,
            organization_id=ORG,
            provider=PROVIDER,
            direction="outbound",
            jurisdiction="GLOBAL",
            registry_authority="acceptance-registry",
            registry_reference="provider-recovery-outbound",
            aml_screening_delegated=True,
            sanctions_screening_delegated=True,
            verified_at=int(time.time()) - 1,
            expires_at=int(time.time()) + 3600,
            evidence={"test_provider": True},
        )
        compliance_db.verify_payment_provider(
            conn,
            organization_id=ORG,
            provider=PROVIDER,
            direction="inbound",
            jurisdiction="GLOBAL",
            registry_authority="acceptance-registry",
            registry_reference="provider-recovery-inbound",
            aml_screening_delegated=True,
            sanctions_screening_delegated=True,
            verified_at=int(time.time()) - 1,
            expires_at=int(time.time()) + 3600,
            evidence={"test_provider": True},
        )
        instrument_id = payment_controls.register_tokenized_instrument(
            conn,
            organization_id=ORG,
            provider=PROVIDER,
            provider_instrument_id="provider-token-recovery",
            rail_type="virtual_card",
            currency="USD",
            label="Provider recovery acceptance card",
        )
        payment_controls.set_spend_controls(
            conn,
            instrument_id=instrument_id,
            max_transaction_minor=2_000,
            max_daily_minor=3_000,
            allowed_merchant_categories=["software"],
            allowed_payees=["vendor-provider-recovery"],
            policy_version="provider-recovery-v1",
        )
        finance_db.reserve_budget(
            conn,
            account_id=account_id,
            objective_id=OBJECTIVE,
            action_id=ACTION,
            amount_minor=1_000,
            currency="USD",
            expires_at=9_999_999_999,
        )
        uncertain = _service(conn).send_payable(
            **_payment_args(account_id, instrument_id)
        )
        assert uncertain["status"] == "uncertain", uncertain
        assert uncertain["provider_reference"] == "provider-payment-0001", uncertain
        assert finance_db.account_balance(conn, account_id) == 5_000
        (_home() / INTENT_MARKER).write_text(
            json.dumps(
                {
                    "intent_id": uncertain["id"],
                    "account_id": account_id,
                    "instrument_id": instrument_id,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        provider = _load_provider()
        assert provider["send_attempts"] == 1, provider
        assert len(provider["payments"]) == 1, provider
        print(json.dumps({"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}))
    finally:
        conn.close()


def recover() -> None:
    marker = json.loads((_home() / INTENT_MARKER).read_text(encoding="utf-8"))
    conn = objectives_db.connect(objectives_db.objectives_db_path())
    try:
        intent_id = str(marker["intent_id"])
        before = _load_provider()
        service = _service(conn)
        settled = service.reconcile(intent_id)
        assert settled["status"] == "succeeded", settled
        assert finance_db.account_balance(conn, str(marker["account_id"])) == 4_000
        assert conn.execute(
            "SELECT status FROM budget_reservations WHERE action_id=?", (ACTION,)
        ).fetchone()["status"] == "settled"
        assert conn.execute(
            "SELECT status FROM payment_spend_holds WHERE action_id=?", (ACTION,)
        ).fetchone()["status"] == "settled"
        retry = service.send_payable(
            **_payment_args(str(marker["account_id"]), str(marker["instrument_id"]))
        )
        assert retry["id"] == intent_id and retry["status"] == "succeeded", retry
        after = _load_provider()
        assert after == before, {"before": before, "after": after}
        assert after["send_attempts"] == 1, after
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM payment_provider_readbacks WHERE payment_intent_id=?",
            (intent_id,),
        ).fetchone()["n"] == 1
        assert finance_db.account_balance(conn, str(marker["account_id"])) == 4_000
        receivable = service.create_receivable(
            organization_id=ORG,
            account_id=str(marker["account_id"]),
            provider=PROVIDER,
            amount_minor=500,
            currency="USD",
            customer={"account": "customer-provider-recovery"},
            customer_jurisdiction="US",
            purpose="Inbound provider recovery acceptance",
            idempotency_key="provider-recovery-receivable-0001",
        )
        assert receivable["direction"] == "incoming", receivable
        settled_receivable = service.reconcile(receivable["id"])
        assert settled_receivable["status"] == "succeeded", settled_receivable
        retried_receivable = service.create_receivable(
            organization_id=ORG,
            account_id=str(marker["account_id"]),
            provider=PROVIDER,
            amount_minor=500,
            currency="USD",
            customer={"account": "customer-provider-recovery"},
            customer_jurisdiction="US",
            purpose="Inbound provider recovery acceptance",
            idempotency_key="provider-recovery-receivable-0001",
        )
        assert retried_receivable["id"] == receivable["id"]
        assert finance_db.account_balance(conn, str(marker["account_id"])) == 4_500
        print(json.dumps({"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1, "inbound_received_minor": 500}))
    finally:
        conn.close()


if __name__ == "__main__":
    phase = sys.argv[1]
    if phase == "interrupt":
        interrupt()
    elif phase == "recover":
        recover()
    else:
        raise SystemExit(f"unknown phase: {phase}")
