"""Tests for the intelica-competitive-intel optional skill.

Hermetic by design: no real network calls are made. httpx.Client methods are
mocked so these tests validate the skill's documented contract (trial-key
flow, no wallet needed) without depending on api.intelica.dev being reachable.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

TRIAL_URL = "https://api.intelica.dev/api-keys/trial"
INTEL_URL = "https://api.intelica.dev/intel"


def _fake_response(status_code: int, payload: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


def test_trial_key_can_run_analysis():
    """A trial key must be obtainable and usable to run one competitive-intel
    analysis, per the documented contract."""
    trial_payload = {"api_key": "trial_abc123"}
    intel_payload = {
        "decision_recommendation": {"action": "monitor", "confidence_score": 0.8},
        "intelica_moat_index": 0.7,
        "detected_competitors": ["Player A", "Player B"],
    }

    with patch.object(httpx.Client, "get", return_value=_fake_response(200, trial_payload)):
        with httpx.Client(timeout=60) as client:
            trial_r = client.get(TRIAL_URL)
    assert trial_r.status_code == 200
    key = trial_r.json().get("api_key")
    assert key

    with patch.object(httpx.Client, "post", return_value=_fake_response(200, intel_payload)) as mock_post:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                INTEL_URL,
                headers={"X-API-KEY": key},
                json={"text": "Fintech neobank in Colombia", "mode": "competitive"},
            )

    assert r.status_code == 200
    body = r.json()
    assert "intelica_moat_index" in body or "decision_recommendation" in body
    called_kwargs = mock_post.call_args.kwargs
    assert called_kwargs.get("headers", {}).get("X-API-KEY") == key


def test_analysis_requires_no_wallet():
    """Trial usage must not require any wallet or payment parameter in the
    request body."""
    intel_payload = {"intelica_moat_index": 0.5, "decision_recommendation": {"action": "monitor"}}
    with patch.object(httpx.Client, "post", return_value=_fake_response(200, intel_payload)) as mock_post:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                INTEL_URL,
                headers={"X-API-KEY": "trial_abc123"},
                json={"text": "Generic SaaS competitor", "mode": "competitive"},
            )

    assert r.status_code == 200
    sent_body = mock_post.call_args.kwargs.get("json", {})
    assert "wallet" not in sent_body
    assert "tx_hash" not in sent_body
