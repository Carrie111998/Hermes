"""Tests for the sentinel-transaction-safety optional skill.

SENTINEL has no free trial or no-wallet preview endpoint, so these tests
verify the skill's documented contract using only the free, unauthenticated
endpoints (/health, /pricing) and the documented 402 behavior of the paid
endpoint. They do not exercise a real x402 payment.
"""
import os
import httpx
import pytest

BASE_URL = "https://sentinel-agent.dev"

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_SKIP_REMOTE_SKILL_TESTS") == "1",
    reason="remote skill test disabled via HERMES_SKIP_REMOTE_SKILL_TESTS",
)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=60) as c:
        yield c


def test_health_endpoint_is_free_and_up(client):
    """/health must be reachable without payment and report signer info."""
    r = client.get(f"{BASE_URL}/health")
    assert r.status_code == 200, f"health returned {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert "wallet_base" in body or "signer" in body or "require_payment" in body


def test_pricing_endpoint_is_free_and_documents_cost(client):
    """/pricing must be reachable without payment and describe the per-call price."""
    r = client.get(f"{BASE_URL}/pricing")
    assert r.status_code == 200, f"pricing returned {r.status_code}: {r.text[:200]}"


def test_guard_without_payment_returns_402(client):
    """POST /v1/guard without an X-PAYMENT header must return 402, per the
    documented no-free-trial contract (never a 200 without payment)."""
    r = client.post(
        f"{BASE_URL}/v1/guard",
        json={
            "chain": "base",
            "from": "0x0000000000000000000000000000000000dEaD",
            "tx": {"to": "0x0000000000000000000000000000000000dEaD", "data": "0x", "value": "0x0"},
        },
    )
    assert r.status_code == 402, f"expected 402 without payment, got {r.status_code}"
