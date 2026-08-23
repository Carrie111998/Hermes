import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from hermes_cli.heartbeat import HeartbeatState


def _entry(session_id: str, chat_id: str, thread_id: str) -> SessionEntry:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="group",
        thread_id=thread_id,
        user_id="owner",
    )
    return SessionEntry(
        session_key=f"agent:main:telegram:group:{chat_id}:thread:{thread_id}",
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="group",
    )


def _runner(*entries: SessionEntry) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: object()}
    runner._heartbeat_watch = {}
    runner._heartbeat_poll_task = None
    runner._background_tasks = set()
    runner._start_heartbeat_poller = MagicMock()
    runner._warm_goals_session_db = AsyncMock()
    runner._is_user_authorized = MagicMock(return_value=True)
    runner.session_store = SimpleNamespace(
        _lock=threading.RLock(),
        _entries={entry.session_key: entry for entry in entries},
        _ensure_loaded_locked=lambda: None,
    )
    return runner


@pytest.mark.asyncio
async def test_restore_heartbeat_watches_preserves_multiple_topic_routes(monkeypatch):
    first = _entry("sid-1", "chat", "topic-1")
    second = _entry("sid-2", "chat", "topic-2")
    states = {
        "sid-1": HeartbeatState("one", 300, status="active"),
        "sid-2": HeartbeatState("two", 300, status="active"),
    }

    monkeypatch.setattr("hermes_cli.heartbeat.list_heartbeats", lambda: states)
    runner = _runner(first, second)

    restored = await runner._restore_heartbeat_watches()

    assert restored == 2
    assert runner._heartbeat_watch == {
        first.session_key: (first.origin, "sid-1"),
        second.session_key: (second.origin, "sid-2"),
    }
    runner._start_heartbeat_poller.assert_called_once()


@pytest.mark.asyncio
async def test_restore_heartbeat_watches_skips_paused_and_unroutable(monkeypatch, caplog):
    active = _entry("sid-active", "chat", "topic-active")
    paused = _entry("sid-paused", "chat", "topic-paused")
    unroutable = _entry("sid-unroutable", "chat", "topic-unroutable")
    unroutable.origin.platform = Platform.DISCORD
    states = {
        "sid-active": HeartbeatState("active", 300, status="active"),
        "sid-paused": HeartbeatState("paused", 300, status="paused"),
        "sid-unroutable": HeartbeatState("missing adapter", 300, status="active"),
        "sid-orphan": HeartbeatState("missing route", 300, status="active"),
    }

    monkeypatch.setattr("hermes_cli.heartbeat.list_heartbeats", lambda: states)
    runner = _runner(active, paused, unroutable)

    restored = await runner._restore_heartbeat_watches()

    assert restored == 1
    assert set(runner._heartbeat_watch) == {active.session_key}
    assert "heartbeat sid-unroutable is active but unroutable" in caplog.text
    assert "heartbeat sid-orphan is active but unroutable" in caplog.text
    assert "no durable gateway route exists" in caplog.text


@pytest.mark.asyncio
async def test_restore_heartbeat_watches_is_idempotent(monkeypatch):
    entry = _entry("sid", "chat", "topic")

    monkeypatch.setattr(
        "hermes_cli.heartbeat.list_heartbeats",
        lambda: {"sid": HeartbeatState("active", 300, status="active")},
    )
    runner = _runner(entry)

    assert await runner._restore_heartbeat_watches() == 1
    assert await runner._restore_heartbeat_watches() == 0
    assert list(runner._heartbeat_watch) == [entry.session_key]


@pytest.mark.asyncio
async def test_heartbeat_status_reports_active_but_unregistered_runtime():
    runner = GatewayRunner.__new__(GatewayRunner)
    source = _entry("sid", "chat", "topic").origin
    manager = MagicMock(session_id="sid")
    manager.is_active.return_value = True
    manager.status_line.return_value = "configured active"
    runner._get_heartbeat_manager_for_event = AsyncMock(
        return_value=(manager, SimpleNamespace(session_id="sid"))
    )
    runner._session_key_for_source = MagicMock(return_value="quick-key")
    runner._heartbeat_watch = {}
    event = SimpleNamespace(
        source=source,
        get_command_args=lambda: "status",
    )

    result = await runner._handle_heartbeat_command(event)

    assert result == (
        "configured active\n"
        "⚠ Runtime: active but not registered; routing is unavailable."
    )


@pytest.mark.asyncio
async def test_pause_unregisters_runtime_watch():
    runner = GatewayRunner.__new__(GatewayRunner)
    source = _entry("sid", "chat", "topic").origin
    state = HeartbeatState("pause me", 300, status="paused")
    manager = MagicMock(session_id="sid")
    manager.pause.return_value = state
    runner._get_heartbeat_manager_for_event = AsyncMock(
        return_value=(manager, SimpleNamespace(session_id="sid"))
    )
    runner._session_key_for_source = MagicMock(return_value="quick-key")
    runner._heartbeat_watch = {"quick-key": (source, "sid")}
    event = SimpleNamespace(
        source=source,
        get_command_args=lambda: "pause",
    )

    result = await runner._handle_heartbeat_command(event)

    assert result == "⏸ Heartbeat paused: pause me"
    assert runner._heartbeat_watch == {}
