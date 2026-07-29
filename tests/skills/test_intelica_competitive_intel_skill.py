"""Tests for the intelica-competitive-intel optional skill.

Uses the free trial-key flow (no wallet, no payment). Verifies the skill's
documented contract: a trial key can be obtained and used to run one
competitive-intelligence analysis.
"""
import os
import httpx
import pytest

TRIAL_URL = "https://api.intelica.dev/api-keys/trial"
INTEL_URL = "https://api.intelica.dev/intel"

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_SKIP_REMOTE_SKILL_TESTS") == "1",
    reason="remote skill test disabled via HERMES_SKIP_REMOTE_SKILL_TESTS",
)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=60) as c:
        yield c


@pytest.fixture(scope="module")
def trial_key(client):
    r = client.get(TRIAL_URL)
    assert r.status_code == 200, f"trial key endpoint returned {r.status_code}: {r.text[:200]}"
    body = r.json()
    key = body.get("api_key") or body.get("key") or body.get("trial_key")
    assert key, f"no trial key found in response: {body}"
    return key


def test_trial_key_can_run_analysis(client, trial_key):
    """A trial key must be usable to run one competitive-intel analysis."""
    r = client.post(
        INTEL_URL,
        headers={"X-API-KEY": trial_key},
        json={"text": "Fintech neobank in Colombia", "mode": "competitive"},
    )
    assert r.status_code == 200, f"analysis returned {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert "intelica_moat_index" in body or "decision_recommendation" in body


def test_analysis_requires_no_wallet(client, trial_key):
    """Trial usage must not require any wallet or payment parameter."""
    r = client.post(
        INTEL_URL,
        headers={"X-API-KEY": trial_key},
        json={"text": "Generic SaaS competitor", "mode": "competitive"},
    )
    assert r.status_code == 200
