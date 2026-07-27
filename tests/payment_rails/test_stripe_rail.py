"""Integration tests for charterforge-stripe-rail.

Tests webhook handling and payment validation for Stripe rail.
"""

import json
import hmac
import hashlib
import time
from pathlib import Path
from typing import Any, Dict

import pytest


def stripe_sig_header(payload: bytes, secret: str, timestamp: int) -> str:
    """Generate Stripe webhook signature header.
    
    Args:
        payload: Raw request body bytes
        secret: Webhook signing secret
        timestamp: Unix timestamp
        
    Returns:
        Stripe-Signature header value
    """
    signed_payload = f"{timestamp}.{payload.decode()}"
    signature = hmac.new(
        secret.encode(),
        signed_payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={signature}"


class TestStripeWebhookValidation:
    """Test Stripe webhook signature validation via _verify_signature."""
    
    @pytest.fixture
    def webhook_secret(self) -> str:
        """Test webhook secret."""
        return "whsec_test_secret_12345"
    
    @pytest.fixture
    def valid_webhook_payload(self) -> Dict[str, Any]:
        """Valid Stripe checkout.session.completed webhook."""
        return {
            "id": "evt_test_123",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "object": "checkout.session",
                    "payment_status": "paid",
                    "amount_total": 1000,
                    "currency": "usd",
                    "customer": "cus_test_123",
                    "metadata": {
                        "charter_id": "charter-001",
                        "objective_id": "obj-001"
                    }
                }
            }
        }
    
    def test_valid_webhook_signature_passes(self, webhook_secret: str, valid_webhook_payload: Dict[str, Any]):
        """Valid webhook signature should pass validation."""
        from charterforge_stripe_rail import _verify_signature
        
        payload = json.dumps(valid_webhook_payload).encode()
        timestamp = int(time.time())
        sig_header = stripe_sig_header(payload, webhook_secret, timestamp)
        
        result = _verify_signature(payload, sig_header, webhook_secret)
        
        assert result["signature_validated"] is True
        assert result["scheme"] == "stripe_signature_v1"
    
    def test_invalid_webhook_signature_fails(self, webhook_secret: str, valid_webhook_payload: Dict[str, Any]):
        """Invalid webhook signature should fail validation."""
        from charterforge_stripe_rail import _verify_signature, StripeWebhookError
        
        payload = json.dumps(valid_webhook_payload).encode()
        timestamp = int(time.time())
        # Use wrong secret
        sig_header = stripe_sig_header(payload, "wrong_secret", timestamp)
        
        with pytest.raises(StripeWebhookError, match="signature"):
            _verify_signature(payload, sig_header, webhook_secret)
    
    def test_expired_webhook_fails(self, webhook_secret: str, valid_webhook_payload: Dict[str, Any]):
        """Expired webhook timestamp should fail validation."""
        from charterforge_stripe_rail import _verify_signature, StripeWebhookError
        
        payload = json.dumps(valid_webhook_payload).encode()
        # Timestamp 10 minutes ago
        timestamp = int(time.time()) - 600
        sig_header = stripe_sig_header(payload, webhook_secret, timestamp)
        
        with pytest.raises(StripeWebhookError, match="time"):
            _verify_signature(payload, sig_header, webhook_secret, tolerance_seconds=300)
    
    def test_payment_intent_succeeded_webhook(self, webhook_secret: str):
        """Payment intent succeeded webhook should parse correctly."""
        from charterforge_stripe_rail import _verify_signature
        
        payload_data = {
            "id": "evt_pi_test",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_test_123",
                    "object": "payment_intent",
                    "amount": 2000,
                    "currency": "usd",
                    "status": "succeeded",
                    "metadata": {
                        "charter_id": "charter-002"
                    }
                }
            }
        }
        
        payload = json.dumps(payload_data).encode()
        timestamp = int(time.time())
        sig_header = stripe_sig_header(payload, webhook_secret, timestamp)
        
        result = _verify_signature(payload, sig_header, webhook_secret)
        
        assert result["signature_validated"] is True


class TestStripeRailEntryPoints:
    """Test entry point discovery."""
    
    def test_inbound_entry_point_importable(self):
        """StripeRail should be importable via inbound entry point."""
        from importlib.metadata import entry_points
        
        eps = entry_points(group="charterforge.inbound_payment_rails")
        stripe_eps = [ep for ep in eps if "stripe" in ep.name]
        
        assert len(stripe_eps) >= 1, "Stripe inbound rail entry point not found"
        
        # Load the entry point
        ep = stripe_eps[0]
        rail_class = ep.load()
        assert rail_class.__name__ == "StripeRail"
    
    def test_outbound_entry_point_importable(self):
        """StripeRail should be importable via outbound entry point."""
        from importlib.metadata import entry_points
        
        eps = entry_points(group="charterforge.outbound_payment_rails")
        stripe_eps = [ep for ep in eps if "stripe" in ep.name]
        
        assert len(stripe_eps) >= 1, "Stripe outbound rail entry point not found"
        
        ep = stripe_eps[0]
        rail_class = ep.load()
        assert rail_class.__name__ == "StripeRail"


class TestStripeRailEnvironmentConfig:
    """Test environment-based configuration."""
    
    def test_api_key_from_env(self, monkeypatch):
        """API key should load from STRIPE_SECRET_KEY environment."""
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_from_env")
        
        from charterforge_stripe_rail import StripeRail
        
        rail = StripeRail()
        assert rail._api_key == "sk_test_from_env"

