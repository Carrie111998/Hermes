import httpx
import pytest
import hashlib
import hmac
import json
import time

from charterforge_stripe_rail import StripeRail, StripeWebhookError, route_webhook_event
from hermes_cli import objectives_db, organization_db


def test_inbound_checkout_is_idempotent_and_readback_maps_paid():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={
                "id": "cs_test_1", "status": "complete", "payment_status": "unpaid",
                "amount_total": 1000, "currency": "usd", "url": "https://checkout.test/1",
                "customer_details": {"email": "sensitive@example.com"},
            })
        return httpx.Response(200, json={
            "id": "cs_test_1", "status": "complete", "payment_status": "paid",
            "amount_total": 1000, "currency": "usd",
        })

    rail = StripeRail(api_key="sk_test", transport=httpx.MockTransport(handler))
    created = rail.create_receivable(
        amount_minor=1000, currency="USD", customer={"email": "buyer@example.com"},
        purpose="Consulting", idempotency_key="intent-1",
    )
    readback = rail.get_payment(created.reference)
    assert created.reference == "cs_test_1"
    assert created.status == "unpaid"
    assert "customer_details" not in created.evidence["object"]
    assert readback.status == "succeeded"
    assert requests[0].headers["Idempotency-Key"] == "intent-1"
    assert requests[0].headers["Authorization"] == "Bearer sk_test"


def test_outbound_requires_connected_account_and_readback_uses_payment_intent():
    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/v1/payment_intents/pi_1"
            return httpx.Response(200, json={
                "id": "pi_1", "status": "succeeded", "amount": 100,
                "currency": "usd", "payment_method": "pm_sensitive",
            })
        return httpx.Response(200, json={"id": "pi_1", "status": "succeeded"})

    rail = StripeRail(api_key="sk_test", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="connected_account_id"):
        rail.send_payment(
            amount_minor=100, currency="USD", payee={}, instrument_reference="pm_1",
            purpose="Vendor", idempotency_key="pay-1",
        )
    payment = rail.send_payment(
        amount_minor=100, currency="USD",
        payee={"connected_account_id": "acct_1", "payment_method_id": "pm_1"},
        instrument_reference="opaque", purpose="Vendor", idempotency_key="pay-1",
    )
    assert payment.reference == "pi_1"
    assert payment.status == "succeeded"
    assert rail.get_payment(payment.reference).amount_minor == 100
    assert "payment_method" not in payment.evidence["object"]


def test_webhook_authenticates_and_routes_idempotently(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn, organization_name="Stripe Company", purpose="Receive payments",
        profile_name="default", charter={},
    )
    body = json.dumps({
        "id": "evt_1", "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_1", "status": "succeeded",
                              "amount": 1000, "currency": "usd"}},
    }, separators=(",", ":")).encode()
    secret = "whsec_test"
    timestamp = int(time.time())
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body,
                      hashlib.sha256).hexdigest()
    header = f"t={timestamp},v1={digest}"
    first = route_webhook_event(
        conn, organization_id=organization_id, raw_body=body,
        signature_header=header, signing_secret=secret,
    )
    second = route_webhook_event(
        conn, organization_id=organization_id, raw_body=body,
        signature_header=header, signing_secret=secret,
    )
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM external_event_receipts").fetchone()[0] == 1
    with pytest.raises(StripeWebhookError, match="invalid"):
        route_webhook_event(
            conn, organization_id=organization_id, raw_body=body,
            signature_header=header.replace(digest, "0" * 64), signing_secret=secret,
        )
