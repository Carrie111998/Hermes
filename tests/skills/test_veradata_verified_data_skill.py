"""Tests for the veradata-verified-data optional skill.

Hermetic by design: no real network calls are made. httpx.Client methods are
mocked so these tests validate the skill's documented contract (X-TRIAL
header flow, no wallet needed) without depending on api.veradata.dev being
reachable.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

RATES_URL = "https://api.veradata.dev/rates"


def _fake_response(status_code: int, payload: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


def test_trial_rates_request_succeeds():
    """Trial header must return live-shaped CO rate data without payment,
    per the documented contract."""
    mocked_payload = {
        "country": "CO",
        "timestamp": "2026-07-12T20:20:05Z",
        "usd_cop": 3248.87,
        "trm_official": 3248.87,
        "source": "Banco de la República de Colombia",
    }
    with patch.object(httpx.Client, "post", return_value=_fake_response(200, mocked_payload)) as mock_post:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                RATES_URL,
                headers={"X-TRIAL": "true"},
                json={"country": "CO", "signals": ["usd_cop"]},
            )

    assert r.status_code == 200
    body = r.json()
    assert body.get("country") == "CO"
    assert "usd_cop" in body
    called_kwargs = mock_post.call_args.kwargs
    assert called_kwargs.get("headers", {}).get("X-TRIAL") == "true"


def test_trial_requires_no_wallet():
    """Trial usage must not require any wallet or tx_hash parameter in the
    request body."""
    mocked_payload = {"country": "CO", "dtf": 9.75}
    with patch.object(httpx.Client, "post", return_value=_fake_response(200, mocked_payload)) as mock_post:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                RATES_URL,
                headers={"X-TRIAL": "true"},
                json={"country": "CO", "signals": ["dtf"]},
            )

    assert r.status_code == 200
    sent_body = mock_post.call_args.kwargs.get("json", {})
    assert "wallet" not in sent_body
    assert "tx_hash" not in sent_body
