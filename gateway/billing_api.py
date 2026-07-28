"""Billing API endpoints for the gateway.

Provides REST endpoints for billing management:
  GET  /api/billing/plans         — list available plans
  GET  /api/billing/subscription  — get current tenant subscription
  POST /api/billing/subscribe     — subscribe to a plan
  POST /api/billing/cancel        — cancel subscription
  GET  /api/billing/usage         — get current period usage summary
  GET  /api/billing/invoices      — list invoices
  POST /api/billing/webhooks/stripe — Stripe webhook handler

These endpoints require authentication via API_SERVER_KEY and expect
the tenant context to be set (via HERMES_TENANT_ID or session context).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore

logger = logging.getLogger(__name__)


def _get_tenant_id() -> str:
    """Resolve tenant_id from environment."""
    return os.environ.get("HERMES_TENANT_ID", "00000000-0000-0000-0000-000000000000")


def _get_conn():
    """Get Postgres authority connection."""
    from hermes_cli.postgres_authority import get_authority_connection
    return get_authority_connection()


def create_billing_routes() -> list:
    """Create billing route definitions for the API server."""
    if web is None:
        return []

    routes = web.RouteTableDef()

    @routes.get("/api/billing/plans")
    async def list_plans(request):
        from hermes_cli.postgres_authority import list_plans
        conn = _get_conn()
        try:
            plans = list_plans(conn)
            return web.json_response({"plans": plans}, default=str)
        finally:
            conn.close()

    @routes.get("/api/billing/subscription")
    async def get_subscription(request):
        from hermes_cli.postgres_authority import get_subscription
        conn = _get_conn()
        try:
            sub = get_subscription(conn, tenant_id=_get_tenant_id())
            if not sub:
                return web.json_response({"error": "no subscription"}, status=404)
            return web.json_response({"subscription": sub}, default=str)
        finally:
            conn.close()

    @routes.post("/api/billing/subscribe")
    async def subscribe(request):
        from hermes_cli.postgres_authority import subscribe_tenant
        body = await request.json()
        plan_id = body.get("plan_id")
        if not plan_id:
            return web.json_response({"error": "plan_id required"}, status=400)

        conn = _get_conn()
        try:
            sub = subscribe_tenant(
                conn, tenant_id=_get_tenant_id(), plan_id=plan_id,
                stripe_customer_id=body.get("stripe_customer_id", ""),
            )
            return web.json_response({"subscription": sub}, default=str)
        finally:
            conn.close()

    @routes.post("/api/billing/cancel")
    async def cancel(request):
        from hermes_cli.postgres_authority import cancel_subscription
        conn = _get_conn()
        try:
            result = cancel_subscription(conn, tenant_id=_get_tenant_id())
            return web.json_response({"canceled": result})
        finally:
            conn.close()

    @routes.get("/api/billing/usage")
    async def get_usage(request):
        from hermes_cli.postgres_authority import get_usage_summary, check_quota
        period = request.query.get("period", "")
        tenant_id = _get_tenant_id()
        conn = _get_conn()
        try:
            summary = get_usage_summary(conn, tenant_id=tenant_id, billing_period=period)
            quotas = {}
            for meter_type in ("task_claim", "permit_consume", "effect_record"):
                allowed, used, limit = check_quota(conn, tenant_id=tenant_id, meter_type=meter_type)
                quotas[meter_type] = {"allowed": allowed, "used": used, "limit": limit}
            return web.json_response({"usage": summary, "quotas": quotas})
        finally:
            conn.close()

    @routes.get("/api/billing/invoices")
    async def get_invoices(request):
        from hermes_cli.postgres_authority import list_invoices
        conn = _get_conn()
        try:
            invoices = list_invoices(conn, tenant_id=_get_tenant_id())
            return web.json_response({"invoices": invoices}, default=str)
        finally:
            conn.close()

    @routes.post("/api/billing/webhooks/stripe")
    async def stripe_webhook(request):
        from hermes_cli.billing_stripe import verify_webhook_signature, handle_webhook_event
        from hermes_cli.postgres_authority import (
            mark_invoice_paid, update_subscription_status,
        )

        payload = await request.read()
        sig_header = request.headers.get("Stripe-Signature", "")

        event = verify_webhook_signature(payload, sig_header)
        if not event:
            return web.json_response({"error": "invalid signature"}, status=400)

        action = handle_webhook_event(event)
        conn = _get_conn()
        try:
            if action["action"] == "invoice_paid":
                mark_invoice_paid(
                    conn,
                    invoice_id=action["stripe_invoice_id"],
                    stripe_invoice_id=action["stripe_invoice_id"],
                )
            elif action["action"] == "payment_failed":
                if action.get("customer_id"):
                    update_subscription_status(
                        conn, tenant_id=_get_tenant_id(), status="past_due"
                    )
            elif action["action"] == "subscription_canceled":
                update_subscription_status(
                    conn, tenant_id=_get_tenant_id(), status="canceled"
                )

            return web.json_response({"received": True})
        finally:
            conn.close()

    return list(routes)
