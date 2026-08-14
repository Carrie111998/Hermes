"""Tests for MoA aggregator-slot exposure used by session cost accounting.

Regression guard for the ~50% MoA cost undercount: on the MoA path the
agent's model/provider are the virtual preset name (e.g. "closed") and "moa",
which have no pricing entry. Session cost accounting must price the
aggregator's acting turn at its REAL model/provider, read from the MoA
client's ``last_aggregator_slot``. Before the fix that slot did not exist and
the aggregator's spend (often >50% of the turn) was silently dropped, leaving
the session cost as advisor-fan-out only.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _response(content="ok"):
    message = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None, model="fake")


@pytest.fixture
def moa_config(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  default_preset: closed
  presets:
    closed:
      enabled: true
      reference_models:
        - provider: openrouter
          model: anthropic/claude-opus-4.8
        - provider: openrouter
          model: openai/gpt-5.5
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_create_populates_last_aggregator_slot(moa_config, monkeypatch):
    """After a create() turn, last_aggregator_slot carries the REAL aggregator
    model/provider — not the virtual preset name."""
    from agent.moa_loop import MoAChatCompletions

    def fake_call_llm(**kwargs):
        return _response("acted" if kwargs.get("task") != "moa_reference" else "advice")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    facade = MoAChatCompletions("closed")
    # Slot is unset before any turn runs.
    assert facade.last_aggregator_slot is None

    facade.create(
        model="closed",
        messages=[{"role": "user", "content": "clean the db"}],
    )

    slot = facade.last_aggregator_slot
    assert slot is not None
    # The virtual preset name / "moa" must NOT leak into the priced slot.
    assert slot["model"] == "anthropic/claude-opus-4.8"
    assert slot["provider"] == "openrouter"
    assert slot["model"] != "closed"


def test_client_exposes_last_aggregator_slot(moa_config, monkeypatch):
    """MoAClient delegates last_aggregator_slot to its completions facade so
    session accounting can read it without touching internals."""
    from agent.moa_loop import MoAClient

    def fake_call_llm(**kwargs):
        return _response("acted" if kwargs.get("task") != "moa_reference" else "advice")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    client = MoAClient("closed")
    assert client.last_aggregator_slot is None

    client.chat.completions.create(
        model="closed",
        messages=[{"role": "user", "content": "clean the db"}],
    )

    slot = client.last_aggregator_slot
    assert slot is not None
    assert slot["model"] == "anthropic/claude-opus-4.8"
    assert slot["provider"] == "openrouter"


def test_create_captures_successful_aggregator_fallback_route(moa_config, monkeypatch):
    from agent.moa_loop import MoAClient

    def fake_call_llm(**kwargs):
        if kwargs.get("task") == "moa_aggregator":
            kwargs["route_info"].update(
                provider="openai",
                model="fallback-model",
                base_url="https://fallback.test/v1",
            )
            return _response("acted")
        return _response("advice")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    client = MoAClient("closed")
    client.chat.completions.create(
        model="closed",
        messages=[{"role": "user", "content": "clean the db"}],
    )

    assert client.last_aggregator_runtime == {
        "provider": "openai",
        "model": "fallback-model",
        "base_url": "https://fallback.test/v1",
    }


def test_prepare_preserves_previous_successful_aggregator_route(moa_config, monkeypatch):
    """Advisor preparation must not erase the route that produced history."""
    from agent.moa_loop import MoAClient

    def fake_call_llm(**kwargs):
        assert kwargs.get("task") == "moa_reference"
        return _response("advice")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)
    client = MoAClient("closed")
    previous_route = {
        "provider": "openrouter",
        "model": "google/gemma-4-31b-it",
        "base_url": "https://fallback.test/v1",
    }
    client.chat.completions.last_aggregator_runtime = dict(previous_route)

    prepared = client.chat.completions.prepare(
        [{"role": "user", "content": "continue the tool turn"}]
    )

    assert prepared["aggregator"]
    assert client.last_aggregator_runtime == previous_route


def test_failed_aggregator_call_preserves_previous_successful_route(
    moa_config, monkeypatch
):
    """A failed replacement attempt cannot invalidate history provenance."""
    from agent.moa_loop import MoAClient

    def fake_call_llm(**kwargs):
        if kwargs.get("task") == "moa_reference":
            return _response("advice")
        raise RuntimeError("all aggregator routes failed")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)
    client = MoAClient("closed")
    previous_route = {
        "provider": "openrouter",
        "model": "google/gemma-4-31b-it",
        "base_url": "https://fallback.test/v1",
    }
    client.chat.completions.last_aggregator_runtime = dict(previous_route)

    with pytest.raises(RuntimeError, match="all aggregator routes failed"):
        client.chat.completions.create(
            model="closed",
            messages=[{"role": "user", "content": "continue"}],
        )

    assert client.last_aggregator_runtime == previous_route
