"""Tests for the veradata-verified-data optional skill.

Uses the free X-TRIAL header flow (no wallet, no payment). Verifies the
skill's documented contract: a trial request returns live LATAM rate data.
"""
import os
import httpx
import pytest

RATES_URL = "https://api.veradata.dev/rates"

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_SKIP_REMOTE_SKILL_TESTS") == "1",
    reason="remote skill test disabled via HERMES_SKIP_REMOTE_SKILL_TESTS",
)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=60) as c:
        yield c


def test_trial_rates_request_succeeds(client):
    """Trial header must return live CO rate data without payment."""
    r = client.post(
        RATES_URL,
        headers={"X-TRIAL": "true"},
        json={"country": "CO", "signals": ["usd_cop"]},
    )
    assert r.status_code == 200, f"rates returned {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body.get("country") == "CO"
    assert "usd_cop" in body


def test_trial_requires_no_wallet(client):
    """Trial usage must not require any wallet or tx_hash parameter."""
    r = client.post(
        RATES_URL,
        headers={"X-TRIAL": "true"},
        json={"country": "CO", "signals": ["dtf"]},
    )
    assert r.status_code == 200
