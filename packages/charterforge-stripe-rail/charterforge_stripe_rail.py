"""Optional Stripe rail; credentials never enter Charterforge state or prompts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Mapping

import httpx
# The provider contract remains in the core compatibility package so this
# optional package does not add a model-tool or runtime dependency surface.
from hermes_cli.payments import PaymentRail, ProviderPayment


class StripeWebhookError(ValueError):
    """Raised when a Stripe webhook cannot be authenticated or routed."""


def _verify_signature(raw_body: bytes, signature_header: str, secret: str,
                      *, now: int | None = None,
                      tolerance_seconds: int = 300) -> dict[str, Any]:
    values: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, _, value = item.partition("=")
        if key and value:
            values.setdefault(key, []).append(value)
    timestamp = values.get("t", [""])[0]
    try:
        signed_at = int(timestamp)
    except ValueError as exc:
        raise StripeWebhookError("Stripe webhook timestamp is invalid") from exc
    current = int(time.time()) if now is None else int(now)
    if abs(current - signed_at) > tolerance_seconds:
        raise StripeWebhookError("Stripe webhook timestamp is outside the replay window")
    expected = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in values.get("v1", [])):
        raise StripeWebhookError("Stripe webhook signature is invalid")
    return {
        "scheme": "stripe_signature_v1",
        "signed_timestamp": timestamp,
        "signature_validated": True,
    }


def route_webhook_event(conn, *, organization_id: str, raw_body: bytes,
                        signature_header: str, signing_secret: str,
                        now: int | None = None) -> list[str]:
    """Authenticate and durably route a Stripe payment event into objectives."""
    if not signing_secret:
        raise StripeWebhookError("Stripe webhook signing secret is required")
    evidence = _verify_signature(raw_body, signature_header, signing_secret, now=now)
    try:
        event = json.loads(raw_body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StripeWebhookError("Stripe webhook body is not valid JSON") from exc
    if not isinstance(event, Mapping):
        raise StripeWebhookError("Stripe webhook body must be an object")
    event_type = str(event.get("type") or "")
    allowed = {
        "checkout.session.completed",
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
    }
    if event_type not in allowed:
        raise StripeWebhookError(f"unsupported Stripe payment event: {event_type}")
    event_id = str(event.get("id") or "")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, Mapping) else None
    if not event_id or not isinstance(obj, Mapping) or not obj.get("id"):
        raise StripeWebhookError("Stripe webhook omitted event or object identity")
    amount = obj.get("amount_total", obj.get("amount"))
    currency = str(obj.get("currency") or "").strip().upper()
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise StripeWebhookError(
            "Stripe webhook omitted a positive integer payment amount"
        )
    if len(currency) != 3 or not currency.isalpha():
        raise StripeWebhookError(
            "Stripe webhook omitted a valid three-letter payment currency"
        )
    payload = {
        "provider_event_id": event_id,
        "provider_object_id": str(obj["id"]),
        "status": str(obj.get("payment_status", obj.get("status", "unknown"))),
        "amount_minor": amount,
        "currency": currency,
        "livemode": bool(obj.get("livemode", False)),
    }
    from hermes_cli import objective_triggers

    try:
        return objective_triggers.route_external_event(
            conn,
            organization_id=organization_id,
            source_type="stripe",
            event_type=event_type,
            source_reference=event_id,
            payload=payload,
            authentication_evidence={**evidence, "provider_event_id": event_id},
        )
    except objective_triggers.TriggerError as exc:
        raise StripeWebhookError(str(exc)) from exc


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
                 idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        response = self._client.request(method, path, data=data, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Stripe returned a non-object response")
        return payload

    @staticmethod
    def _safe_object(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Keep provider evidence bounded to fields needed for reconciliation."""
        allowed = {
            "id", "object", "status", "payment_status", "amount", "amount_total",
            "currency", "livemode", "created", "transfer_group",
        }
        return {key: payload[key] for key in allowed if key in payload}

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
            evidence={"provider": "stripe", "object": StripeRail._safe_object(payload)},
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
        endpoint = "payment_intents" if reference.startswith("pi_") else "checkout/sessions"
        payload = self._request("GET", f"/v1/{endpoint}/{reference}")
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
            "transfer_data[destination]": str(connected),
        }
        payload = self._request(
            "POST", "/v1/payment_intents", data=data,
            idempotency_key=idempotency_key,
        )
        status = str(payload.get("status", "unknown"))
        return ProviderPayment(
            reference=str(payload["id"]), status="succeeded" if status == "succeeded" else status,
            amount_minor=amount_minor, currency=currency.upper(),
            evidence={"provider": "stripe", "connected_account_id": str(connected),
                      "object": self._safe_object(payload)},
        )
