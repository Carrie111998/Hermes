"""Optional Stripe rail; credentials never enter Charterforge state or prompts."""

from __future__ import annotations

import os
from typing import Any, Mapping

import httpx
# The provider contract remains in the core compatibility package so this
# optional package does not add a model-tool or runtime dependency surface.
from hermes_cli.payments import PaymentRail, ProviderPayment


class StripeRail(PaymentRail):
    name = "stripe"

    def __init__(self, *, api_key: str | None = None, base_url: str = "https://api.stripe.com",
                 transport: httpx.BaseTransport | None = None):
        self._api_key = api_key or os.environ.get("STRIPE_SECRET_KEY")
        if not self._api_key:
            raise ValueError("STRIPE_SECRET_KEY is required for the Stripe rail")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=30.0,
            transport=transport,
        )

    def _request(self, method: str, path: str, *, data: Mapping[str, Any] | None = None,
                 idempotency_key: str | None = None,
                 connected_account_id: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        if connected_account_id:
            headers["Stripe-Account"] = connected_account_id
        response = self._client.request(method, path, data=data, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Stripe returned a non-object response")
        return payload

    @staticmethod
    def _payment(payload: Mapping[str, Any], *, amount_minor: int | None = None,
                 currency: str | None = None) -> ProviderPayment:
        reference = str(payload.get("id") or "")
        if not reference:
            raise ValueError("Stripe response omitted an object id")
        amount = payload.get("amount_total", payload.get("amount"))
        resolved_currency = payload.get("currency")
        if amount is None or not resolved_currency:
            raise ValueError("Stripe response omitted amount or currency")
        status = str(payload.get("payment_status", payload.get("status", "unknown")))
        if status == "paid":
            status = "succeeded"
        return ProviderPayment(
            reference=reference,
            status=status,
            amount_minor=int(amount if amount_minor is None else amount_minor),
            currency=str(resolved_currency if currency is None else currency).upper(),
            payment_url=payload.get("url"),
            evidence={"provider": "stripe", "object": dict(payload)},
        )

    def create_receivable(self, *, amount_minor: int, currency: str,
                          customer: Mapping[str, Any], purpose: str,
                          idempotency_key: str) -> ProviderPayment:
        data: dict[str, Any] = {
            "mode": "payment",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_minor),
            "line_items[0][price_data][product_data][name]": purpose,
            "line_items[0][quantity]": "1",
            "success_url": str(customer.get("success_url") or "https://checkout.stripe.com/success"),
            "cancel_url": str(customer.get("cancel_url") or "https://checkout.stripe.com/cancel"),
            "client_reference_id": idempotency_key,
        }
        if customer.get("email"):
            data["customer_email"] = str(customer["email"])
        payload = self._request("POST", "/v1/checkout/sessions", data=data,
                                idempotency_key=idempotency_key)
        return self._payment(payload, amount_minor=amount_minor, currency=currency)

    def get_payment(self, reference: str) -> ProviderPayment:
        payload = self._request("GET", f"/v1/checkout/sessions/{reference}")
        return self._payment(payload)

    def send_payment(self, *, amount_minor: int, currency: str,
                     payee: Mapping[str, Any], instrument_reference: str,
                     purpose: str, idempotency_key: str) -> ProviderPayment:
        connected = payee.get("connected_account_id")
        payment_method = payee.get("payment_method_id") or instrument_reference
        if not connected or not payment_method:
            raise RuntimeError(
                "Stripe outbound rail requires an explicit connected_account_id "
                "and provider payment method; arbitrary vendor payouts are blocked"
            )
        data = {
            "amount": str(amount_minor),
            "currency": currency.lower(),
            "payment_method": str(payment_method),
            "confirm": "true",
            "off_session": "true",
            "description": purpose,
        }
        payload = self._request(
            "POST", "/v1/payment_intents", data=data,
            idempotency_key=idempotency_key,
            connected_account_id=str(connected),
        )
        status = str(payload.get("status", "unknown"))
        return ProviderPayment(
            reference=str(payload["id"]), status="succeeded" if status == "succeeded" else status,
            amount_minor=amount_minor, currency=currency.upper(),
            evidence={"provider": "stripe", "connected_account_id": str(connected),
                      "object": dict(payload)},
        )
