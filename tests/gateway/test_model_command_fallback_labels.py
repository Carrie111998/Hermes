"""Regression tests for #83910: gateway /status and /model must distinguish the
configured primary from the active runtime route when provider fallback is live.

Previously /model labeled the configured primary as "Current:" even while the
running agent answered from a fallback provider, making a successfully
activated fallback look like inconsistent state. /status reported the live
route without explaining the fallback either.

Fix contract:

* /model (no args, text list) — when fallback is active it shows distinct
  labels: "Configured primary", "Session override" (if any), and
  "Active route (fallback)". Without fallback the output is unchanged.
* /status — when fallback is active the model line says "(fallback active)"
  and the configured primary is shown on its own line. Without fallback the
  output is unchanged. The fallback flag is read from the live/cached agent
  and, when idle, from the persisted ``gateway_runtime`` session metadata.
"""

import json
import threading
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.slash_commands as slash_commands
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_state import AsyncSessionDB

SK = "agent:main:telegram:private:12345"


def _make_agent(model="deepseek-v4-pro", provider="deepseek", fallback=False):
    """A bare agent carrying just the route fields the handlers read."""
    agent = MagicMock()
    agent.model = model
    agent.provider = provider
    agent.base_url = None
    agent._fallback_activated = fallback
    return agent


def _make_runner(agent=None, cached_agent=None, overrides=None):
    """Bare GatewayRunner with the fields _handle_model_command needs."""
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = dict(overrides or {})
    runner._session_key_for_source = MagicMock(return_value=SK)
    runner._normalize_source_for_session_key = MagicMock(side_effect=lambda s: s)
    if agent is not None:
        runner._running_agents[SK] = agent
    if cached_agent is not None:
        runner._agent_cache[SK] = (cached_agent, "sig")
    return runner


class _FakeSessionEntry:
    session_key = SK
    session_id = "sess-83910"
    created_at = datetime(2026, 8, 11, 12, 0, 0)
    updated_at = datetime(2026, 8, 11, 12, 30, 0)
    last_prompt_tokens = 0
    total_tokens = 0


def _make_status_runner(agent=None, session_row=None):
    """Bare GatewayRunner with the fields _handle_status_command needs."""
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    store = AsyncMock()
    store.get_or_create_session.return_value = _FakeSessionEntry()
    # async_session_store is a read-only property backed by session_store;
    # plant the mock so the property returns it unchanged.
    runner.session_store = MagicMock()
    store._store = runner.session_store
    runner._async_session_store = store
    if session_row is not None:
        db = AsyncSessionDB(MagicMock())
        db._db.get_session_title.return_value = None
        db._db.get_session.return_value = session_row
        runner._session_db = db
    else:
        runner._session_db = None
    runner._queue_depth = lambda *a, **k: 0
    if agent is not None:
        runner._running_agents[SK] = agent
    return runner


def _make_event(text="/model"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )


@pytest.fixture
def _isolated_config(tmp_path, monkeypatch):
    """Point the handler at an isolated home with a known configured primary."""
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: gpt-5.6-luna\n  provider: openai-codex\nproviders: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    return hermes_home


def _patch_provider_listing(monkeypatch):
    """No-op the authenticated-provider listing so tests never touch the network."""
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **kwargs: [],
    )


# --------------------------------------------------------------------------- #
# /model
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_model_no_args_fallback_active_shows_distinct_labels(
    _isolated_config, monkeypatch
):
    """Fallback live => /model labels configured primary and fallback route separately."""
    _patch_provider_listing(monkeypatch)
    runner = _make_runner(agent=_make_agent(fallback=True))

    result = await runner._handle_model_command(_make_event("/model"))

    assert "Configured primary: `gpt-5.6-luna`" in result
    assert "Active route (fallback): `deepseek-v4-pro`" in result
    assert "Current:" not in result, (
        "the configured primary must not be labeled 'Current' while fallback is live"
    )


@pytest.mark.asyncio
async def test_model_no_args_fallback_active_with_session_override(
    _isolated_config, monkeypatch
):
    """Fallback live + session /model override => all three layers are shown."""
    _patch_provider_listing(monkeypatch)
    overrides = {SK: {"model": "claude-sonnet-4.6", "provider": "anthropic"}}
    runner = _make_runner(agent=_make_agent(fallback=True), overrides=overrides)

    result = await runner._handle_model_command(_make_event("/model"))

    assert "Configured primary: `gpt-5.6-luna`" in result
    assert "Session override: `claude-sonnet-4.6`" in result
    assert "Active route (fallback): `deepseek-v4-pro`" in result
    assert "Current:" not in result


@pytest.mark.asyncio
async def test_model_no_args_cached_agent_fallback_active(_isolated_config, monkeypatch):
    """Between turns the route comes from the agent cache, not just _running_agents."""
    _patch_provider_listing(monkeypatch)
    runner = _make_runner(cached_agent=_make_agent(fallback=True))

    result = await runner._handle_model_command(_make_event("/model"))

    assert "Configured primary: `gpt-5.6-luna`" in result
    assert "Active route (fallback): `deepseek-v4-pro`" in result


@pytest.mark.asyncio
async def test_model_no_args_without_fallback_is_unchanged(_isolated_config, monkeypatch):
    """No fallback => /model keeps the existing 'Current:' label."""
    _patch_provider_listing(monkeypatch)
    runner = _make_runner(
        agent=_make_agent(model="gpt-5.6-luna", provider="openai-codex", fallback=False)
    )

    result = await runner._handle_model_command(_make_event("/model"))

    assert "Current: `gpt-5.6-luna`" in result
    assert "Active route" not in result
    assert "Configured primary" not in result


# --------------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_status_fallback_active_labels_route_and_primary(_isolated_config):
    """Fallback live => /status marks the route and shows the configured primary."""
    runner = _make_status_runner(agent=_make_agent(fallback=True))

    result = await runner._handle_status_command(_make_event("/status"))

    assert "**Model (fallback active):** `deepseek-v4-pro` (deepseek)" in result
    assert "**Configured primary:** `gpt-5.6-luna` (openai-codex)" in result


@pytest.mark.asyncio
async def test_status_idle_uses_persisted_fallback_flag(_isolated_config):
    """Idle session => /status trusts persisted gateway_runtime fallback flag."""
    row = {
        "model": "deepseek-v4-pro",
        "billing_provider": "deepseek",
        "billing_base_url": None,
        "model_config": json.dumps(
            {"gateway_runtime": {"provider": "deepseek", "fallback_active": True}}
        ),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }
    runner = _make_status_runner(session_row=row)

    result = await runner._handle_status_command(_make_event("/status"))

    assert "**Model (fallback active):** `deepseek-v4-pro` (deepseek)" in result
    assert "**Configured primary:** `gpt-5.6-luna` (openai-codex)" in result


@pytest.mark.asyncio
async def test_status_without_fallback_is_unchanged(_isolated_config):
    """No fallback => /status keeps the existing model line and no fallback text."""
    runner = _make_status_runner(
        agent=_make_agent(model="gpt-5.6-luna", provider="openai-codex", fallback=False)
    )

    result = await runner._handle_status_command(_make_event("/status"))

    assert "**Model:** `gpt-5.6-luna` (openai-codex)" in result
    assert "fallback" not in result.lower()
    assert "Configured primary" not in result
