"""Liveness probes for a dockerized gateway: health, auth, models, basic chat.

Parametrized over the provider matrix via the ``gateway`` fixture — see
``conftest.py``. Opt-in: nothing runs unless ``HERMES_E2E=1``.
"""

from __future__ import annotations

import pytest

from .constants import MODEL, STEER
from .http_client import chat_content

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(0)]


def test_health(gateway):
    resp = gateway.get("/health", auth=False)
    assert resp.status == 200, resp.text


def test_auth_required(gateway):
    """`/v1/*` must reject a request with no bearer token."""
    resp = gateway.get("/v1/models", auth=False)
    assert resp.status in (401, 403), f"expected auth challenge, got {resp.status}: {resp.text[:200]}"


def test_models_advertises_name(gateway):
    resp = gateway.get("/v1/models")
    assert resp.status == 200, resp.text
    ids = [m.get("id") for m in resp.json().get("data", [])]
    assert ids, f"/v1/models returned no models: {resp.text[:200]}"
    assert MODEL in ids, f"{MODEL!r} not advertised; got {ids}"


def test_chat_basic(gateway):
    resp = gateway.post(
        "/v1/chat/completions",
        {
            "model": MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": STEER},
                {"role": "user", "content": "Reply with exactly the word PONG."},
            ],
        },
    )
    assert resp.status == 200, f"HTTP {resp.status}: {resp.text[:300]}"
    content = chat_content(resp.json())
    assert content, f"empty assistant content: {resp.text[:300]}"
    assert "pong" in content.lower(), f"unexpected reply: {content!r}"
