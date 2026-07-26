"""Live ai& smoke test — exercises the Hermes runtime, not a raw SDK client.

Opt-in only:
    HERMES_LIVE_TESTS=1 AIAND_API_KEY=... \\
        pytest tests/run_agent/test_aiand_live.py -q

Unlike a bare OpenAI() client pointed at the endpoint, this drives Hermes'
own provider resolution — ``resolve_provider_client('aiand')`` — so it
verifies the auth/config/base-URL/aux-model wiring that the bundled
provider actually ships, then makes a real call through that client.
"""

from __future__ import annotations

import os

import pytest

LIVE = os.environ.get("HERMES_LIVE_TESTS") == "1"
AIAND_KEY = os.environ.get("AIAND_API_KEY", "")

pytestmark = [
    pytest.mark.skipif(not LIVE, reason="live-only: set HERMES_LIVE_TESTS=1"),
    pytest.mark.skipif(not AIAND_KEY, reason="AIAND_API_KEY not configured"),
    pytest.mark.integration,
]


def _resolve_runtime_client(provider="aiand"):
    """Build the ai& client the way the Hermes runtime does."""
    from agent.auxiliary_client import resolve_provider_client

    client, model = resolve_provider_client(provider)
    assert client is not None, "Hermes failed to build an ai& client"
    return client, model


def test_hermes_wires_aiand_client():
    """The runtime resolves an ai& client pointed at the right endpoint —
    no network required."""
    client, model = _resolve_runtime_client()
    assert "api.aiand.com" in str(client.base_url)
    assert model == "deepseek-ai/deepseek-v4-flash"


def test_aiand_basic_chat_through_runtime():
    """A single-turn completion via the Hermes-resolved client returns text."""
    client, model = _resolve_runtime_client()

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Say exactly the word 'pong' and nothing else."}],
        timeout=60,
    )

    content = response.choices[0].message.content
    assert content and "pong" in content.lower()


def test_aiand_alias_resolves_through_runtime():
    """The 'ai-and' alias resolves to the same ai& client via the runtime."""
    client, _ = _resolve_runtime_client("ai-and")
    assert "api.aiand.com" in str(client.base_url)
