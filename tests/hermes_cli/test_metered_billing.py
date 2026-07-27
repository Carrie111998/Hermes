import sqlite3

import pytest

from hermes_cli import metered_billing


def test_meter_schema_read_preserves_active_transaction(tmp_path):
    conn = sqlite3.connect(tmp_path / "authority.db")
    conn.row_factory = sqlite3.Row
    metered_billing.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    metered_billing.ensure_schema(conn)
    assert conn.in_transaction is True
    conn.rollback()


def test_subcent_usage_accumulates_without_rounding_loss():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    meter = metered_billing.create_meter(
        conn,
        organization_id="org_1",
        name="inference-token",
        currency="USD",
        unit_price_microminor=250_000,
        unit_name="token",
    )
    for index in range(3):
        metered_billing.record_usage(
            conn,
            meter_id=meter,
            customer_id="customer_1",
            quantity=1,
            idempotency_key=f"request-{index}",
            evidence={"request_id": f"request-{index}"},
            occurred_at=10,
        )
    first = metered_billing.close_usage_window(
        conn, meter_id=meter, customer_id="customer_1", through_at=10
    )
    assert first["amount_minor"] == 0
    assert first["carry_microminor"] == 750_000
    metered_billing.record_usage(
        conn,
        meter_id=meter,
        customer_id="customer_1",
        quantity=1,
        idempotency_key="request-3",
        evidence={"request_id": "request-3"},
        occurred_at=11,
    )
    second = metered_billing.close_usage_window(
        conn, meter_id=meter, customer_id="customer_1", through_at=11
    )
    assert second["amount_minor"] == 1
    assert second["carry_microminor"] == 0


def test_usage_retry_rejects_event_parameter_drift():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    meter = metered_billing.create_meter(
        conn,
        organization_id="org_1",
        name="inference-token",
        currency="USD",
        unit_price_microminor=250_000,
        unit_name="token",
    )
    metered_billing.record_usage(
        conn,
        meter_id=meter,
        customer_id="customer_1",
        quantity=2,
        idempotency_key="usage-replay-drift-0001",
        evidence={"request_id": "request-1"},
        occurred_at=10,
    )
    with pytest.raises(ValueError, match="different event parameters"):
        metered_billing.record_usage(
            conn,
            meter_id=meter,
            customer_id="customer_1",
            quantity=3,
            idempotency_key="usage-replay-drift-0001",
            evidence={"request_id": "request-1"},
            occurred_at=10,
        )
    with pytest.raises(ValueError, match="different event parameters"):
        metered_billing.record_usage(
            conn,
            meter_id=meter,
            customer_id="customer_1",
            quantity=2,
            idempotency_key="usage-replay-drift-0001",
            evidence={"request_id": "request-2"},
            occurred_at=10,
        )
