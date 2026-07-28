"""Stripe integration for the billing engine.

Handles:
- Customer creation and subscription management
- Usage-based billing (metered subscriptions)
- Webhook event processing (invoice.paid, subscription updates)

Configuration:
  STRIPE_API_KEY: Secret key for Stripe API calls
  STRIPE_WEBHOOK_SECRET: Webhook endpoint signing secret
"""

from __future__ import annotations

import os
from typing import Any, Optional

try:
    import stripe
except ImportError:
    stripe = None  # type: ignore


def _get_stripe():
    """Get configured Stripe module, or None if not available."""
    if stripe is None:
        return None
    api_key = os.environ.get("STRIPE_API_KEY", "")
    if not api_key:
        return None
    stripe.api_key = api_key
    return stripe


def create_customer(
    *,
    tenant_id: str,
    email: str,
    name: str = "",
    metadata: dict[str, str] | None = None,
) -> Optional[str]:
    """Create a Stripe customer for a tenant. Returns customer ID or None."""
    s = _get_stripe()
    if not s:
        return None

    params: dict[str, Any] = {
        "email": email,
        "metadata": {"tenant_id": tenant_id, **(metadata or {})},
    }
    if name:
        params["name"] = name

    customer = s.Customer.create(**params)
    return customer.id


def create_subscription(
    *,
    customer_id: str,
    price_id: str,
    trial_days: int = 0,
) -> Optional[dict]:
    """Create a Stripe subscription. Returns subscription info or None."""
    s = _get_stripe()
    if not s:
        return None

    params: dict[str, Any] = {
        "customer": customer_id,
        "items": [{"price": price_id}],
    }
    if trial_days > 0:
        params["trial_period_days"] = trial_days

    sub = s.Subscription.create(**params)
    return {
        "id": sub.id,
        "status": sub.status,
        "current_period_start": sub.current_period_start,
        "current_period_end": sub.current_period_end,
    }


def cancel_subscription(*, subscription_id: str) -> bool:
    """Cancel a Stripe subscription at period end."""
    s = _get_stripe()
    if not s:
        return False

    s.Subscription.modify(subscription_id, cancel_at_period_end=True)
    return True


def report_usage(
    *,
    subscription_item_id: str,
    quantity: int,
    timestamp: int | None = None,
) -> bool:
    """Report usage to a metered subscription item."""
    s = _get_stripe()
    if not s:
        return False

    params: dict[str, Any] = {
        "quantity": quantity,
        "action": "set",
    }
    if timestamp:
        params["timestamp"] = timestamp

    s.SubscriptionItem.create_usage_record(subscription_item_id, **params)
    return True


def verify_webhook_signature(
    payload: bytes,
    sig_header: str,
    webhook_secret: str = "",
) -> Optional[dict]:
    """Verify and parse a Stripe webhook event.

    Returns the event dict if valid, None if verification fails.
    """
    s = _get_stripe()
    if not s:
        return None

    secret = webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        return None

    try:
        event = s.Webhook.construct_event(payload, sig_header, secret)
        return dict(event)
    except (ValueError, s.error.SignatureVerificationError):
        return None


def handle_webhook_event(event: dict) -> dict[str, Any]:
    """Process a verified Stripe webhook event.

    Returns an action dict describing what authority-store updates are needed.
    The caller (API endpoint) is responsible for applying these to the database.
    """
    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    if event_type == "invoice.paid":
        return {
            "action": "invoice_paid",
            "stripe_invoice_id": data.get("id", ""),
            "customer_id": data.get("customer", ""),
            "amount_paid": data.get("amount_paid", 0),
            "subscription_id": data.get("subscription", ""),
        }

    elif event_type == "invoice.payment_failed":
        return {
            "action": "payment_failed",
            "stripe_invoice_id": data.get("id", ""),
            "customer_id": data.get("customer", ""),
            "subscription_id": data.get("subscription", ""),
        }

    elif event_type == "customer.subscription.updated":
        return {
            "action": "subscription_updated",
            "subscription_id": data.get("id", ""),
            "customer_id": data.get("customer", ""),
            "status": data.get("status", ""),
            "cancel_at_period_end": data.get("cancel_at_period_end", False),
        }

    elif event_type == "customer.subscription.deleted":
        return {
            "action": "subscription_canceled",
            "subscription_id": data.get("id", ""),
            "customer_id": data.get("customer", ""),
        }

    return {"action": "unhandled", "event_type": event_type}
