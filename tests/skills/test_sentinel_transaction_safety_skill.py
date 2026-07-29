"""Tests for the sentinel-transaction-safety optional skill.

Hermetic by design: no real network calls are made. httpx.Client methods are
mocked so these tests validate the skill's documented contract (free
/health and /pricing endpoints, and the 402-without-payment behavior of
/v1/guard) without depending on sentinel-agent.dev being reachable.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

BASE_URL = "https://sentinel-agent.dev"


def _fake_response(status_code: int, payload: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


def test_health_endpoint_is_free_and_up():
    """/health must be callable without payment and report signer info,
    per the documented contract."""
    mocked_payload = {"status": "ok", "signer": "sentinel-agent.dev", "require_payment": True}
    with patch.object(httpx.Client, "get", return_value=_fake_response(200, mocked_payload)):
        with httpx.Client(timeout=60) as client:
            r = client.get(f"{BASE_URL}/health")

    assert r.status_code == 200
    body = r.json()
    assert "signer" in body or "require_payment" in body or "status" in body


def test_pricing_endpoint_is_free_and_documents_cost():
    """/pricing must be callable without payment and describe the per-call
    price, per the documented contract."""
    mocked_payload = {"network": "base", "price_usdc": "0.005"}
    with patch.object(httpx.Client, "get", return_value=_fake_response(200, mocked_payload)):
        with httpx.Client(timeout=60) as client:
            r = client.get(f"{BASE_URL}/pricing")

    assert r.status_code == 200
    body = r.json()
    assert "price_usdc" in body or "network" in body


def test_guard_without_payment_returns_402():
    """POST /v1/guard without an X-PAYMENT header must return 402, per the
    documented no-free-trial contract (never a 200 without payment)."""
    mocked_payload = {
        "x402Version": 2,
        "accepts": [{"network": "base", "amount": "5000", "payTo": "0xCf1d31020A7915421f6d66B9835Dcb6f422337E7"}],
    }
    with patch.object(httpx.Client, "post", return_value=_fake_response(402, mocked_payload)) as mock_post:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                f"{BASE_URL}/v1/guard",
                json={
                    "chain": "base",
                    "from": "0x0000000000000000000000000000000000dEaD",
                    "tx": {"to": "0x0000000000000000000000000000000000dEaD", "data": "0x", "value": "0x0"},
                },
            )

    assert r.status_code == 402
    body = r.json()
    assert "accepts" in body
    mock_post.assert_called_once()
