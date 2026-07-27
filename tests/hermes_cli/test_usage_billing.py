from __future__ import annotations

import pytest

from hermes_cli import metered_billing, objectives_db, usage_billing


def test_usage_is_idempotent_and_prices_are_recorded(tmp_path):
    conn = objectives_db.connect(tmp_path / "usage.db")
    first = usage_billing.record_usage(
        conn, organization_id="org", customer_ref="customer-1", metric="api_calls",
        quantity=3, unit_price_minor=25, currency="usd", idempotency_key="evt-1",
    )
    again = usage_billing.record_usage(
        conn, organization_id="org", customer_ref="customer-1", metric="api_calls",
        quantity=3, unit_price_minor=25, currency="USD", idempotency_key="evt-1",
    )
    assert again["id"] == first["id"]
    assert first["amount_minor"] == 75
    with pytest.raises(ValueError, match="different event data"):
        usage_billing.record_usage(
            conn, organization_id="org", customer_ref="customer-1", metric="api_calls",
            quantity=4, unit_price_minor=25, currency="USD", idempotency_key="evt-1",
        )


def test_invoice_context_and_allocation_prevent_double_billing(tmp_path):
    conn = objectives_db.connect(tmp_path / "usage.db")
    first = usage_billing.record_usage(
        conn, organization_id="org", customer_ref="customer-1", metric="tokens",
        quantity=2, unit_price_minor=10, currency="USD", idempotency_key="evt-1",
    )
    second = usage_billing.record_usage(
        conn, organization_id="org", customer_ref="customer-1", metric="tokens",
        quantity=5, unit_price_minor=10, currency="USD", idempotency_key="evt-2",
    )
    context = usage_billing.invoice_context(
        conn, organization_id="org", customer_ref="customer-1", currency="USD",
        event_ids=[first["id"], second["id"]],
    )
    assert context["amount_minor"] == 70
    assert context["metrics"] == {"tokens": 7}
    conn.execute(
        """INSERT INTO payment_intents
           (id,organization_id,account_id,direction,provider,party_json,amount_minor,
            currency,purpose,status,idempotency_key,metadata_json,created_at,updated_at)
           VALUES ('pi-1','org','acct','inbound','fake','{}',70,'USD','usage','pending','pi-key','{}',1,1)"""
    )
    conn.execute(
        """INSERT INTO payment_intents
           (id,organization_id,account_id,direction,provider,party_json,amount_minor,
            currency,purpose,status,idempotency_key,metadata_json,created_at,updated_at)
           VALUES ('pi-2','org','acct','inbound','fake','{}',70,'USD','usage','pending','pi-key-2','{}',1,1)"""
    )
    conn.commit()
    usage_billing.allocate_events(conn, event_ids=context["event_ids"], payment_intent_id="pi-1")
    with pytest.raises(ValueError, match="already allocated"):
        usage_billing.invoice_context(
            conn, organization_id="org", customer_ref="customer-1", currency="USD",
            event_ids=[first["id"]],
        )
    allowed = usage_billing.invoice_context(
        conn, organization_id="org", customer_ref="customer-1", currency="USD",
        event_ids=[first["id"]], existing_payment_intent_id="pi-1",
    )
    assert allowed["amount_minor"] == 20
    usage_billing.allocate_events(conn, event_ids=context["event_ids"], payment_intent_id="pi-1")
    with pytest.raises(ValueError, match="another invoice"):
        usage_billing.allocate_events(conn, event_ids=[first["id"]], payment_intent_id="pi-2")


def test_invoice_context_rejects_cross_customer_and_currency(tmp_path):
    conn = objectives_db.connect(tmp_path / "usage.db")
    event = usage_billing.record_usage(
        conn, organization_id="org", customer_ref="customer-1", metric="jobs",
        quantity=1, unit_price_minor=100, currency="USD", idempotency_key="evt-1",
    )
    with pytest.raises(ValueError, match="organization and customer"):
        usage_billing.invoice_context(
            conn, organization_id="org", customer_ref="customer-2", currency="USD",
            event_ids=[event["id"]],
        )
    with pytest.raises(ValueError, match="currencies"):
        usage_billing.invoice_context(
            conn, organization_id="org", customer_ref="customer-1", currency="CAD",
            event_ids=[event["id"]],
        )


def test_business_meter_isolated_from_inherited_model_usage_meter(tmp_path):
    conn = objectives_db.connect(tmp_path / "usage.db")
    model_meter = metered_billing.create_meter(
        conn, organization_id="org", name="model-token", currency="USD",
        unit_price_microminor=1, unit_name="token",
    )
    metered_billing.record_usage(
        conn, meter_id=model_meter, customer_id="customer-1", quantity=1,
        idempotency_key="model-event", evidence={"request_id": "r1"},
    )
    business_event = usage_billing.record_usage(
        conn, organization_id="org", customer_ref="customer-1", metric="service_call",
        quantity=1, unit_price_minor=100, currency="USD", idempotency_key="business-event",
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM agentic_usage_events").fetchone()["n"] == 1
    assert business_event["amount_minor"] == 100
