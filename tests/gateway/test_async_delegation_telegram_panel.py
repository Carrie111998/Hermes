"""Telegram-only async-delegation status-panel behavior."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections import deque
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner(adapter):
    if not hasattr(adapter, "platform"):
        adapter.platform = Platform.TELEGRAM
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._async_delegation_panels = {}
    runner._async_delegation_panel_chats = {}
    runner._canonical_async_delegation_panel_source = lambda session_key: SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100123",
        chat_type="group",
        thread_id="42",
        profile="default",
    )
    runner._adapter_for_source = lambda source: adapter
    runner._thread_metadata_for_source = lambda source: {"thread_id": source.thread_id}
    return runner


def test_running_panel_uses_canonical_telegram_source_and_allowlisted_plain_text(monkeypatch):
    adapter = SimpleNamespace(send_or_update_status=AsyncMock(
        return_value=SendResult(success=True, message_id="7"),
    ))
    runner = _runner(adapter)
    records = [{
        "delegation_id": "deleg_abcdef123456",
        "session_key": "agent:main:telegram:group:-100123:42",
        "status": "RUNNING",
        "goal": "Investigate\ncredentials=do-not-show " + "x" * 110,
        "role": "leaf " + "y" * 50,
        "dispatched_at": 10.0,
        "seconds_since_progress": 4.0,
        "in_tool": True,
        "children_activity": [{
            "api_calls": 3,
            "current_tool": "terminal --token private " + "z" * 60,
            "seconds_since_activity": 2.0,
        }],
        "context": "must never be displayed",
        "model": "must never be displayed",
        "result": "must never be displayed",
        "error": "must never be displayed",
    }]
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: records)
    runner._async_delegation_panel_clock = lambda: 20.0

    asyncio.run(runner._update_async_delegation_telegram_panels())

    adapter.send_or_update_status.assert_awaited_once()
    args, kwargs = adapter.send_or_update_status.await_args
    assert args[:2] == ("-100123", "delegation:deleg_abcdef123456")
    content = args[2]
    assert kwargs["metadata"] == {"thread_id": "42"}
    assert kwargs["best_effort"] is True
    assert content.splitlines()[0] == "Delegation deleg_abcdef123456"
    assert "running" in content
    assert "in tool" in content
    assert "credentials" in content
    assert "do-not-show" not in content
    assert "credentials=***" in content
    assert "must never" not in content
    assert "private" not in content


def test_panel_rejects_non_telegram_delivery_adapters(monkeypatch):
    relay_adapter = SimpleNamespace(
        platform=Platform.RELAY,
        send_or_update_status=AsyncMock(
            return_value=SendResult(success=True, message_id="7"),
        ),
    )
    runner = _runner(relay_adapter)
    monkeypatch.setattr(
        "tools.async_delegation.list_async_delegations",
        lambda: [{
            "delegation_id": "deleg_abcdef123456",
            "session_key": "agent:main:telegram:group:-100123:42",
            "status": "running",
            "goal": "safe goal",
            "role": "leaf",
        }],
    )

    asyncio.run(runner._update_async_delegation_telegram_panels())

    relay_adapter.send_or_update_status.assert_not_awaited()


def test_multiplex_panel_requires_registered_transport_even_for_profile_stamped_source(monkeypatch):
    adapter = SimpleNamespace(send_or_update_status=AsyncMock(
        return_value=SendResult(success=True, message_id="7"),
    ))
    runner = _runner(adapter)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="99",
        chat_type="dm",
        profile="secondary",
    )
    runner._canonical_async_delegation_panel_source = lambda _key: source
    transport_known = False
    runner._registered_transport_adapter = lambda _source: adapter if transport_known else None
    records = [{
        "delegation_id": "deleg_abcdef123456",
        "session_key": "derived-only-key",
        "status": "running",
        "goal": "safe goal",
        "role": "leaf",
        "dispatched_at": time.time(),
    }]
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: records)

    asyncio.run(runner._update_async_delegation_telegram_panels())
    adapter.send_or_update_status.assert_not_awaited()

    transport_known = True
    asyncio.run(runner._update_async_delegation_telegram_panels())
    adapter.send_or_update_status.assert_awaited_once()


def test_multiplex_live_source_provenance_survives_cache_and_beats_persisted_origin(monkeypatch):
    class LiveAdapter:
        platform = Platform.TELEGRAM

        def __init__(self):
            self.send_or_update_status = AsyncMock(
                return_value=SendResult(success=True, message_id="7"),
            )
            self.gateway_runner = SimpleNamespace(
                _profile_name_for_source=lambda _source: "secondary",
            )

    adapter = LiveAdapter()
    source = BasePlatformAdapter.build_source(
        adapter,
        chat_id="99",
        chat_type="dm",
    )
    session_key = "agent:main:telegram:dm:99"
    persisted_source = SessionSource.from_dict(source.to_dict())

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._async_delegation_panels = {}
    runner._async_delegation_panel_chats = {}
    runner._thread_metadata_for_source = lambda _source: {}
    runner.session_store = SimpleNamespace(
        _ensure_loaded=lambda: None,
        _entries={session_key: SimpleNamespace(origin=persisted_source)},
    )
    runner._cache_session_source(session_key, source)

    cached_source = runner._get_cached_session_source(session_key)
    assert runner._registered_transport_adapter(cached_source) is adapter
    assert runner._registered_transport_adapter(persisted_source) is None

    monkeypatch.setattr(
        "tools.async_delegation.list_async_delegations",
        lambda: [{
            "delegation_id": "deleg_abcdef123456",
            "session_key": session_key,
            "status": "running",
            "goal": "safe goal",
            "role": "leaf",
        }],
    )

    asyncio.run(runner._update_async_delegation_telegram_panels())

    adapter.send_or_update_status.assert_awaited_once()


def test_terminal_first_observation_stays_a_complete_silent_tombstone(monkeypatch):
    adapter = SimpleNamespace(send_or_update_status=AsyncMock())
    runner = _runner(adapter)
    record = {
        "delegation_id": "deleg_abcdef123456",
        "session_key": "agent:main:telegram:group:-100123:42",
        "status": "completed",
        "goal": "already done",
        "role": "leaf",
    }
    monkeypatch.setattr(
        "tools.async_delegation.list_async_delegations",
        lambda: [record],
    )

    asyncio.run(runner._update_async_delegation_telegram_panels())
    asyncio.run(runner._update_async_delegation_telegram_panels())

    adapter.send_or_update_status.assert_not_awaited()
    assert runner._async_delegation_panels[record["delegation_id"]] == {
        "adapter": adapter,
        "chat_id": "-100123",
        "status_key": "delegation:deleg_abcdef123456",
        "signature": None,
        "sent": False,
        "terminal": True,
    }


def test_panel_normalizes_lifecycle_statuses_through_an_explicit_allowlist():
    expected = {
        "queued": "pending", "ACTIVE": "running", "completing": "finalizing",
        "success": "completed", "failed": "error", "cancelled": "interrupted",
        "timed_out": "timed out", "arbitrary internal detail": "unknown",
    }
    for raw, normalized in expected.items():
        content, _signature = GatewayRunner._async_delegation_panel_text(
            {"delegation_id": "deleg_abcdef123456", "status": raw, "goal": "g", "role": "leaf"},
            now=0.0,
        )
        assert f"status: {normalized}" in content


@pytest.mark.parametrize("delegation_id", ["deleg_abcdef123456", "abc"])
def test_panel_displays_the_full_actionable_delegation_id(delegation_id):
    content, signature = GatewayRunner._async_delegation_panel_text(
        {"delegation_id": delegation_id, "status": "running", "goal": "g", "role": "leaf"},
        now=0.0,
    )

    assert content.splitlines()[0] == f"Delegation {delegation_id}"
    assert signature[0] == delegation_id


def test_panel_renders_every_visible_child_and_temporal_ages_do_not_change_signature():
    record = {
        "delegation_id": "deleg_abcdef123456",
        "status": "running",
        "goal": "fan out safely",
        "role": "orchestrator",
        "dispatched_at": 10.0,
        "seconds_since_progress": 4.0,
        "children_activity": [
            {"api_calls": 3, "current_tool": "terminal", "seconds_since_activity": 2.0},
            {"api_calls": 7, "current_tool": "web_search", "seconds_since_activity": 1.0},
            {"api_calls": 1, "current_tool": "read_file", "seconds_since_activity": 0.5},
        ],
    }

    first, first_signature = GatewayRunner._async_delegation_panel_text(record, now=20.0)
    record["seconds_since_progress"] = 40.0
    for child in record["children_activity"]:
        child["seconds_since_activity"] += 30.0
    second, second_signature = GatewayRunner._async_delegation_panel_text(record, now=80.0)

    assert "child 1: 3 calls · terminal" in first
    assert "child 2: 7 calls · web_search" in first
    assert "child 3: 1 calls · read_file" in first
    assert len(first) <= 500
    assert first != second
    assert first_signature == second_signature


def test_panel_bounds_pathological_child_api_call_counts():
    content, _signature = GatewayRunner._async_delegation_panel_text(
        {
            "delegation_id": "deleg_abcdef123456",
            "status": "running",
            "goal": "bounded",
            "role": "leaf",
            "children_activity": [{
                "api_calls": 10 ** 10_000,
                "current_tool": "terminal",
                "seconds_since_activity": 10 ** 10_000,
            }],
            "seconds_since_progress": 10 ** 10_000,
            "dispatched_at": 10 ** 10_000,
        },
        now=0.0,
    )

    assert "child 1: 999999999+ calls · terminal" in content
    assert len(content) <= 500


@pytest.mark.asyncio
async def test_snapshot_acquisition_is_inside_the_cycle_budget(monkeypatch):
    adapter = SimpleNamespace(send_or_update_status=AsyncMock())
    runner = _runner(adapter)
    release = threading.Event()

    def slow_snapshot():
        release.wait(timeout=1.0)
        return []

    monkeypatch.setattr("tools.async_delegation.list_async_delegations", slow_snapshot)
    started = time.perf_counter()
    await runner._update_async_delegation_telegram_panels()

    assert time.perf_counter() - started < 0.35
    adapter.send_or_update_status.assert_not_awaited()
    release.set()
    snapshot_task = runner._async_delegation_panel_snapshot_task
    assert snapshot_task is not None
    await snapshot_task


def test_rate_rejection_preserves_signature_eligibility_and_terminal_without_a_sent_panel_is_silent(monkeypatch):
    adapter = SimpleNamespace(send_or_update_status=AsyncMock(
        return_value=SendResult(success=True, message_id="7"),
    ))
    runner = _runner(adapter)
    record = {
        "delegation_id": "deleg_abcdef123456",
        "session_key": "agent:main:telegram:group:-100123:42",
        "status": "running",
        "goal": "safe goal",
        "role": "leaf",
        "dispatched_at": 10.0,
    }
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: [record])
    now = 20.0
    runner._async_delegation_panel_clock = lambda: now
    chat_key = (id(adapter), "-100123")
    runner._async_delegation_panel_chats[chat_key] = {
        "last_send": 19.5,
        "last_edit": 0.0,
        "blocked_until": 0.0,
        "writes": deque(),
    }

    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 0
    assert runner._async_delegation_panels[record["delegation_id"]]["signature"] is None

    now = 21.0
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 1

    # If the admitted send had failed to create a message, terminal state must
    # tombstone instead of creating a new cosmetic message after completion.
    panel = runner._async_delegation_panels[record["delegation_id"]]
    panel["sent"] = False
    record["status"] = "completed"
    now = 30.0
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 1
    assert panel["terminal"] is True


def test_retry_after_timedelta_opens_adapter_chat_circuit_and_rolling_cap_is_finite(monkeypatch):
    adapter = SimpleNamespace(send_or_update_status=AsyncMock(
        return_value=SendResult(
            success=False,
            error="flood controlled",
            retry_after=cast(Any, timedelta(seconds=30)),
        ),
    ))
    runner = _runner(adapter)
    record = {
        "delegation_id": "deleg_abcdef123456",
        "session_key": "agent:main:telegram:group:-100123:42",
        "status": "running",
        "goal": "first state",
        "role": "leaf",
        "dispatched_at": 10.0,
    }
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: [record])
    now = 20.0
    runner._async_delegation_panel_clock = lambda: now

    asyncio.run(runner._update_async_delegation_telegram_panels())
    chat = runner._async_delegation_panel_chats[(id(adapter), "-100123")]
    assert chat["blocked_until"] == pytest.approx(50.0)
    assert adapter.send_or_update_status.await_count == 1

    record["goal"] = "changed while blocked"
    now = 25.0
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 1

    # Once the circuit expires, six prior admitted writes in the rolling
    # window still suppress the update; expiring those writes admits it.
    now = 51.0
    chat["writes"] = deque([45.0, 46.0, 47.0, 48.0, 49.0, 50.0])
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 1

    now = 111.0
    adapter.send_or_update_status.return_value = SendResult(success=True, message_id="7")
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 2


def test_first_send_is_immediately_eligible_and_chat_send_spacing_is_1_1_seconds(monkeypatch):
    adapter = SimpleNamespace(send_or_update_status=AsyncMock(
        return_value=SendResult(success=True, message_id="7"),
    ))
    runner = _runner(adapter)
    records = [
        {
            "delegation_id": "deleg_abcdef111111",
            "session_key": "agent:main:telegram:group:-100123:42",
            "status": "running",
            "goal": "first",
            "role": "leaf",
            "dispatched_at": 0.0,
        },
        {
            "delegation_id": "deleg_abcdef222222",
            "session_key": "agent:main:telegram:group:-100123:42",
            "status": "running",
            "goal": "second",
            "role": "leaf",
            "dispatched_at": 0.0,
        },
    ]
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: records)
    now = 0.1
    runner._async_delegation_panel_clock = lambda: now

    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 1

    now = 1.19
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 1

    now = 1.21
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 2


def test_consecutive_edits_share_a_five_second_chat_spacing(monkeypatch):
    adapter = SimpleNamespace(send_or_update_status=AsyncMock(
        return_value=SendResult(success=True, message_id="7"),
    ))
    runner = _runner(adapter)
    record = {
        "delegation_id": "deleg_abcdef123456",
        "session_key": "agent:main:telegram:group:-100123:42",
        "status": "running",
        "goal": "initial",
        "role": "leaf",
        "dispatched_at": 0.0,
    }
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: [record])
    now = 10.0
    runner._async_delegation_panel_clock = lambda: now

    asyncio.run(runner._update_async_delegation_telegram_panels())
    record["goal"] = "first edit"
    now = 11.0
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 2

    record["goal"] = "second edit"
    now = 15.9
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 2

    now = 16.0
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 3


def test_terminal_edit_is_attempted_once_then_absent_state_prunes_panel_and_adapter_cache(monkeypatch):
    adapter = SimpleNamespace(
        platform=Platform.TELEGRAM,
        _status_message_ids={},
        send_or_update_status=AsyncMock(side_effect=[
            SendResult(success=True, message_id="7"),
            SendResult(success=False, error="cosmetic edit failed"),
        ]),
    )
    runner = _runner(adapter)
    record = {
        "delegation_id": "deleg_abcdef123456",
        "session_key": "agent:main:telegram:group:-100123:42",
        "status": "running",
        "goal": "initial",
        "role": "leaf",
        "dispatched_at": 0.0,
    }
    records = [record]
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: records)
    now = 10.0
    runner._async_delegation_panel_clock = lambda: now

    asyncio.run(runner._update_async_delegation_telegram_panels())
    cache_key = ("-100123", "delegation:deleg_abcdef123456")
    adapter._status_message_ids[cache_key] = "7"

    record["status"] = "completed"
    now = 20.0
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 2
    assert runner._async_delegation_panels[record["delegation_id"]]["terminal"] is True

    record["goal"] = "must not trigger another terminal edit"
    now = 30.0
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert adapter.send_or_update_status.await_count == 2

    records.clear()
    asyncio.run(runner._update_async_delegation_telegram_panels())
    assert record["delegation_id"] not in runner._async_delegation_panels
    assert cache_key not in adapter._status_message_ids


@pytest.mark.asyncio
async def test_slow_panel_request_is_cancelled_inside_the_cycle_budget(monkeypatch):
    never = asyncio.Event()

    async def slow_status(*_args, **_kwargs):
        await never.wait()

    adapter = SimpleNamespace(send_or_update_status=AsyncMock(side_effect=slow_status))
    runner = _runner(adapter)
    records = [{
        "delegation_id": "deleg_abcdef123456",
        "session_key": "agent:main:telegram:group:-100123:42",
        "status": "running",
        "goal": "bounded request",
        "role": "leaf",
        "dispatched_at": time.time(),
    }]
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: records)

    started = time.perf_counter()
    await runner._update_async_delegation_telegram_panels()

    assert time.perf_counter() - started < 0.35
    assert adapter.send_or_update_status.await_count == 1


def test_completion_delivery_precedes_nonfatal_panel_failure(monkeypatch):
    from tools import process_registry as process_registry_module

    events = []
    completion_queue = queue.Queue()
    completion_queue.put({
        "type": "async_delegation",
        "delegation_id": "deleg_abcdef123456",
        "status": "completed",
    })
    monkeypatch.setattr(
        process_registry_module,
        "process_registry",
        SimpleNamespace(completion_queue=completion_queue),
    )
    monkeypatch.setattr("gateway.run._format_gateway_process_notification", lambda _evt: "done")

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._enrich_async_delegation_routing = lambda _evt: None

    async def deliver(_text, _evt):
        events.append("completion")
        return True

    async def panel():
        events.append("panel")
        raise RuntimeError("cosmetic failure")

    runner._deliver_completion_notification = deliver
    runner._update_async_delegation_telegram_panels = panel
    sleep_calls = 0

    async def stop_after_one_cycle(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            runner._running = False

    monkeypatch.setattr("gateway.run.asyncio.sleep", stop_after_one_cycle)
    asyncio.run(runner._async_delegation_watcher(interval=0))

    assert events == ["completion", "panel"]
