"""Tests for Stripe billing integration.

Tests the webhook event handling logic without requiring a live Stripe connection.
The billing_stripe module is designed to work with Stripe's API, but the
handle_webhook_event function is pure logic that can be tested in isolation.
"""

import pytest

from hermes_cli.billing_stripe import handle_webhook_event


class TestWebhookEventHandling:
    def test_invoice_paid(self):
        event = {
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_test_123",
                    "customer": "cus_test_456",
                    "amount_paid": 2900,
                    "subscription": "sub_test_789",
                }
            },
        }
        result = handle_webhook_event(event)
        assert result["action"] == "invoice_paid"
        assert result["stripe_invoice_id"] == "in_test_123"
        assert result["customer_id"] == "cus_test_456"
        assert result["amount_paid"] == 2900

    def test_payment_failed(self):
        event = {
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_test_fail",
                    "customer": "cus_test_456",
                    "subscription": "sub_test_789",
                }
            },
        }
        result = handle_webhook_event(event)
        assert result["action"] == "payment_failed"
        assert result["customer_id"] == "cus_test_456"

    def test_subscription_updated(self):
        event = {
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_test_789",
                    "customer": "cus_test_456",
                    "status": "past_due",
                    "cancel_at_period_end": False,
                }
            },
        }
        result = handle_webhook_event(event)
        assert result["action"] == "subscription_updated"
        assert result["status"] == "past_due"
        assert result["cancel_at_period_end"] is False

    def test_subscription_deleted(self):
        event = {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_test_789",
                    "customer": "cus_test_456",
                }
            },
        }
        result = handle_webhook_event(event)
        assert result["action"] == "subscription_canceled"

    def test_unhandled_event(self):
        event = {
            "type": "charge.refunded",
            "data": {"object": {"id": "ch_test"}},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "unhandled"
        assert result["event_type"] == "charge.refunded"
