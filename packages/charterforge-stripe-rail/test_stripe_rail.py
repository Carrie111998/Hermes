import httpx
import pytest

from charterforge_stripe_rail import StripeRail


def test_inbound_checkout_is_idempotent_and_readback_maps_paid():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={
                "id": "cs_test_1", "status": "complete", "payment_status": "unpaid",
                "amount_total": 1000, "currency": "usd", "url": "https://checkout.test/1",
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
    assert readback.status == "succeeded"
    assert requests[0].headers["Idempotency-Key"] == "intent-1"
    assert requests[0].headers["Authorization"] == "Bearer sk_test"


def test_outbound_requires_connected_account_and_uses_scoped_header():
    def handler(request):
        assert request.headers["Stripe-Account"] == "acct_1"
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
