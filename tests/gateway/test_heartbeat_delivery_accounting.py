"""Gateway heartbeat poll: fire accounting must reflect real delivery.

A heartbeat tick is only "fired" once its staged prompt actually becomes
a turn. Claims that vanish without any turn must be counted as missed
(with a warning), and a session without a live adapter must never be
claimed in the first place. Regression coverage for #92837.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli.heartbeat import HeartbeatManager


class _HeartbeatAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:  # pragma: no cover
        return True

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:  # pragma: no cover
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):  # pragma: no cover
        return {}


def _due_heartbeat(session_id: str, seconds_ago: float) -> HeartbeatManager:
    mgr = HeartbeatManager(session_id=session_id)
    mgr.set("tick", 600)
    mgr.state.created_at = time.time() - seconds_ago
    from hermes_cli.heartbeat import save_heartbeat

    save_heartbeat(session_id, mgr.state)
    return mgr


def _make_runner(monkeypatch, session_id: str = "session-1"):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="42",
        chat_type="dm",
        user_id="42",
    )
    key = build_session_key(source)
    adapter = _HeartbeatAdapter(
        PlatformConfig(enabled=True, token="test", typing_indicator=False),
        Platform.TELEGRAM,
    )
    runner = object.__new__(GatewayRunner)
    runner._heartbeat_watch = {key: (source, session_id)}
    runner._heartbeat_inflight = {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._adapter_for_source = lambda _source: adapter
    runner._peek_session_state = lambda _key: None
    return runner, adapter, key, source


@pytest.mark.asyncio
async def test_due_tick_stages_without_counting_fired(monkeypatch):
    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)

    await runner._poll_heartbeat_watches_once()

    staged = adapter._pending_messages.get(key)
    assert staged is not None and staged._hermes_heartbeat_tick is True
    mgr = HeartbeatManager(session_id="session-1")
    # Staged ≠ delivered: nothing may be counted as fired yet.
    assert mgr.state.fire_count == 0
    assert mgr.state.claimed_at is not None
    assert key in runner._heartbeat_inflight


@pytest.mark.asyncio
async def test_turn_consuming_staged_tick_confirms_the_fire(monkeypatch):
    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    await runner._poll_heartbeat_watches_once()

    # Simulate a real turn: the adapter drained the pending slot and the
    # session's agent recorded activity after the tick was staged.
    adapter._pending_messages.pop(key, None)
    agent = type("Agent", (), {"_last_activity_ts": time.time()})()
    runner._agent_cache[key] = (agent,)
    await runner._poll_heartbeat_watches_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 1
    assert mgr.state.last_delivered_at > 0
    assert mgr.state.missed_count == 0
    assert mgr.state.claimed_at is None
    assert runner._heartbeat_inflight == {}


@pytest.mark.asyncio
async def test_staged_tick_vanishing_without_turn_counts_missed(monkeypatch, caplog):
    import logging

    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    await runner._poll_heartbeat_watches_once()

    # Session reset / stale-lock heal / eviction discards the staged event
    # without any turn ever running.
    adapter._pending_messages.pop(key, None)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        await runner._poll_heartbeat_watches_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 0
    assert mgr.state.missed_count == 1
    assert mgr.state.claimed_at is None
    assert "never became a turn" in caplog.text or "no turn" in caplog.text

    # The tick was never delivered: it stays due and is re-claimed on the
    # following poll instead of being silently dropped.
    await runner._poll_heartbeat_watches_once()
    assert adapter._pending_messages.get(key) is not None


@pytest.mark.asyncio
async def test_missing_adapter_never_claims_the_tick(monkeypatch):
    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    runner._adapter_for_source = lambda _source: None

    await runner._poll_heartbeat_watches_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 0
    assert mgr.state.claimed_at is None
    assert runner._heartbeat_inflight == {}


@pytest.mark.asyncio
async def test_busy_session_leaves_staged_tick_in_flight(monkeypatch):
    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    await runner._poll_heartbeat_watches_once()

    # A turn starts (session busy) but has not drained the slot yet.
    runner._running_agents[key] = object()
    await runner._poll_heartbeat_watches_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 0
    assert mgr.state.claimed_at is not None
    assert key in runner._heartbeat_inflight
