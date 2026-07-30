"""Tests for the watch-pattern idle watcher (issue #75065).

The watch-pattern watcher drains ``watch_match`` / ``watch_disabled`` events
from the completion queue when no agent turn is running, ensuring that
background-process watch-pattern notifications are delivered promptly even
on idle sessions — rather than sitting in the queue until the next user
message triggers a post-turn drain.
"""

import asyncio
import queue
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner, _drain_gateway_watch_events, _format_gateway_process_notification
from tools.process_registry import ProcessRegistry


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Any current/future durable compatibility path must stay in tmp state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    return registry


def _runner(adapter):
    """Build a minimal GatewayRunner-like object for testing idle watchers."""
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner.session_store = SimpleNamespace(
        _ensure_loaded=lambda: None,
        _entries={},
    )
    runner._session_source_cache = {}
    runner._completion_delivery_lock = __import__("threading").Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    return runner


def _stop_after_sleeps(monkeypatch, runner, count):
    sleep_calls = 0

    async def _bounded_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= count:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _bounded_sleep)


def _watch_event(session_id="proc_test_watch"):
    return {
        "type": "watch_match",
        "session_id": session_id,
        "session_key": "agent:main:discord:dm:999:888",
        "platform": "discord",
        "chat_id": "999",
        "thread_id": "",
        "user_id": "user_123",
        "user_name": "alice",
        "message_id": "",
        "pattern": "RENDER_DONE",
        "output": "RENDER_DONE",
        "suppressed": 0,
    }


class TestWatchPatternIdleWatcher:
    def test_idle_watcher_injects_watch_match(self, monkeypatch, isolated_registry):
        """A watch_match event queued while idle gets injected by the watcher."""
        isolated = queue.Queue()
        monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
        isolated.put(_watch_event())

        adapter = SimpleNamespace(handle_message=AsyncMock())
        runner = _runner(adapter)
        _stop_after_sleeps(monkeypatch, runner, count=2)

        asyncio.run(runner._watch_pattern_watcher(interval=0))

        adapter.handle_message.assert_awaited_once()
        msg = adapter.handle_message.await_args.args[0]
        assert "RENDER_DONE" in msg.text
        assert "proc_test_watch" in msg.text

    def test_idle_watcher_injects_watch_disabled(self, monkeypatch, isolated_registry):
        """A watch_disabled summary event is also injected by the watcher."""
        isolated = queue.Queue()
        monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
        isolated.put({
            "type": "watch_disabled",
            "session_id": "proc_abc",
            "session_key": "agent:main:discord:dm:999:888",
            "platform": "discord",
            "chat_id": "999",
            "thread_id": "",
            "user_id": "u1",
            "user_name": "bob",
            "message_id": "",
            "message": "Watch patterns disabled for process proc_abc",
        })

        adapter = SimpleNamespace(handle_message=AsyncMock())
        runner = _runner(adapter)
        _stop_after_sleeps(monkeypatch, runner, count=2)

        asyncio.run(runner._watch_pattern_watcher(interval=0))

        adapter.handle_message.assert_awaited_once()
        msg = adapter.handle_message.await_args.args[0]
        assert "Watch patterns disabled" in msg.text

    def test_idle_watcher_skips_completion_and_async_events(
        self, monkeypatch, isolated_registry
    ):
        """The watcher only drains watch_match/watch_disabled; other event types
        are left on the queue for their respective consumers."""
        isolated = queue.Queue()
        monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
        isolated.put(_watch_event())
        isolated.put({
            "type": "async_delegation",
            "delegation_id": "deleg_1",
            "session_key": "agent:main:discord:dm:999:888",
            "goal": "test",
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 5.0,
            "dispatched_at": 1.0,
            "completed_at": 6.0,
            "origin_profile": "default",
            "origin_hermes_home": "/tmp/test",
        })
        isolated.put({
            "type": "completion",
            "session_id": "proc_comp",
            "session_key": "agent:main:discord:dm:999:888",
            "platform": "discord",
            "chat_id": "999",
            "started_at": 1.0,
            "command": "echo hi",
            "exit_code": 0,
            "completion_reason": "exited",
            "output": "hi\n",
        })

        adapter = SimpleNamespace(handle_message=AsyncMock())
        runner = _runner(adapter)
        _stop_after_sleeps(monkeypatch, runner, count=2)

        asyncio.run(runner._watch_pattern_watcher(interval=0))

        # Only the watch_match event was handled (once).
        adapter.handle_message.assert_awaited_once()

        # _drain_gateway_watch_events: requeues async_delegation, drops
        # completion (owned by _run_process_watcher). So only async_delegation
        # survives on the queue.
        assert isolated.qsize() == 1
        remaining = isolated.get_nowait()
        assert remaining["type"] == "async_delegation"

    def test_idle_watcher_drains_multiple_events(self, monkeypatch, isolated_registry):
        """Multiple watch events queued while idle are all injected."""
        isolated = queue.Queue()
        monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
        for i in range(3):
            evt = _watch_event(session_id=f"proc_{i}")
            isolated.put(evt)

        adapter = SimpleNamespace(handle_message=AsyncMock())
        runner = _runner(adapter)
        _stop_after_sleeps(monkeypatch, runner, count=2)

        asyncio.run(runner._watch_pattern_watcher(interval=0))

        assert adapter.handle_message.await_count == 3

    def test_idle_watcher_quiet_when_queue_empty(
        self, monkeypatch, isolated_registry
    ):
        """No events on the queue → no handle_message call."""
        isolated = queue.Queue()
        monkeypatch.setattr(isolated_registry, "completion_queue", isolated)

        adapter = SimpleNamespace(handle_message=AsyncMock())
        runner = _runner(adapter)
        _stop_after_sleeps(monkeypatch, runner, count=2)

        asyncio.run(runner._watch_pattern_watcher(interval=0))

        adapter.handle_message.assert_not_awaited()
