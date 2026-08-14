"""Live durable-truth recovery in the gateway async-delegation watcher.

Reproduces the 2026-08-13 production incident: durable async completion rows
persisted with delivery_state='pending' whose in-memory queue wake was lost
sat undelivered for ~21h until a gateway restart, and the restart then
replayed pre-terminal events AFTER the stream's terminal event had already
been seen. The watcher now sweeps the durable store every tick, so the store
is authoritative during live operation — a lost wake is rediscovered and
delivered within single-digit seconds, in occurrence order, without a
restart, and without duplicate user turns when the immediate wake and the
sweep race on the same ID.
"""

import asyncio
import queue
import time
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    # Tests exercise the recovery machinery, not the production grace bounds.
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    monkeypatch.setattr(ad, "_SWEEP_REWAKE_INTERVAL_S", 0.0)
    yield registry
    ad._reset_for_tests()


def _runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
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


def _persist_completion_without_wake(delegation_id, *, session_key="agent:main:telegram:dm:12345:678"):
    """The production lost-wake shape: durable truth exists, no queue event."""
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": session_key,
        "goal": "Investigate flaky test",
        "status": "completed",
        "summary": f"result of {delegation_id}",
        "api_calls": 1,
        "duration_seconds": 12.0,
        "dispatched_at": time.time() - 30,
        "completed_at": time.time() - 18,
    }
    ad._persist_dispatch({
        "delegation_id": delegation_id,
        "session_key": session_key,
        "origin_ui_session_id": "",
        "parent_session_id": None,
        "dispatched_at": evt["dispatched_at"],
    })
    ad._persist_completion(evt, {"status": "completed", "summary": evt["summary"]})
    return evt


def test_lost_wake_is_delivered_live_without_restart(monkeypatch, isolated_state):
    """Requirement 1: durable-pending with no queue wake, gateway stays alive."""
    _persist_completion_without_wake("deleg_lost_wake")
    assert isolated_state.completion_queue.empty()

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=4)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()
    assert "deleg_lost_wake" in adapter.handle_message.await_args.args[0].text
    row = ad.get_durable_delegation("deleg_lost_wake")
    assert row["delivery_state"] == "delivered"


def test_lost_wake_recovery_is_prompt_with_real_ticks(monkeypatch, isolated_state):
    """Timing-bounded: recovery lands well inside the single-digit-second
    contract even on a real (compressed) tick cadence, without a restart."""
    _persist_completion_without_wake("deleg_prompt")

    delivered = asyncio.Event()

    async def _accept(_event):
        delivered.set()

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_accept))
    runner = _runner(adapter)

    async def _exercise():
        start = time.monotonic()
        task = asyncio.create_task(runner._async_delegation_watcher(interval=0.02))
        try:
            await asyncio.wait_for(delivered.wait(), timeout=8.0)
        finally:
            runner._running = False
            task.cancel()
        return time.monotonic() - start

    # The watcher's 3s connect settle plus a few ticks — loose upper bound.
    assert asyncio.run(_exercise()) < 6.0
    adapter.handle_message.assert_awaited_once()


def test_immediate_wake_and_sweep_race_produce_one_turn(monkeypatch, isolated_state):
    evt = _persist_completion_without_wake("deleg_race")
    # The immediate wake DID arrive...
    isolated_state.completion_queue.put(dict(evt))
    # ...and the sweep independently re-wakes the same ID (tracker cleared to
    # force the duplicate copy the race would produce).
    ad._clear_wake_activity("deleg_race")
    assert ad.sweep_undelivered_completions(isolated_state.completion_queue) == 1
    assert isolated_state.completion_queue.qsize() == 2

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=4)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()
    row = ad.get_durable_delegation("deleg_race")
    assert row["delivery_state"] == "delivered"
    # The winning copy claimed once; the loser's refused claim burned nothing.
    assert row["delivery_attempts"] == 1


def test_reversed_wakes_deliver_ordered_stream_low_to_high(
    monkeypatch, isolated_state,
):
    """Immediate wakes intentionally reversed: strict order is enforced at the
    claim chokepoint and the sweep re-wakes the deferred higher sequence."""
    for seq in (1, 2):
        ad.publish_background_notification(
            summary=f"stream event {seq}",
            session_key="agent:main:telegram:dm:12345:678",
            notification_id=f"ride/{seq}",
            stream_id="ride",
            sequence=seq,
        )
    # Drop the producer wakes and enqueue them REVERSED (high first).
    wakes = {}
    while not isolated_state.completion_queue.empty():
        e = isolated_state.completion_queue.get_nowait()
        wakes[e["delegation_id"]] = e
    isolated_state.completion_queue.put(wakes["ride/2"])
    isolated_state.completion_queue.put(wakes["ride/1"])

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=6)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    texts = [c.args[0].text for c in adapter.handle_message.await_args_list]
    assert len(texts) == 2
    assert "stream event 1" in texts[0]
    assert "stream event 2" in texts[1]
    assert ad.get_durable_delegation("ride/1")["delivery_state"] == "delivered"
    assert ad.get_durable_delegation("ride/2")["delivery_state"] == "delivered"


def test_delivered_terminal_suppresses_stale_recovered_events(
    monkeypatch, isolated_state,
):
    """The incident shape: pre-terminal events with lost wakes must never
    surface after the stream's superseding terminal event was delivered."""
    for seq, status in ((13, "driver_assigned"), (22, "eta_checkpoint")):
        ad.publish_background_notification(
            summary=f"stale event {seq}",
            session_key="agent:main:telegram:dm:12345:678",
            notification_id=f"chauffeur/r1/{seq}",
            stream_id="chauffeur/r1",
            sequence=seq,
            status=status,
        )
    while not isolated_state.completion_queue.empty():
        isolated_state.completion_queue.get_nowait()  # both wakes lost

    ad.publish_background_notification(
        summary="ride ended",
        session_key="agent:main:telegram:dm:12345:678",
        notification_id="chauffeur/r1/69",
        stream_id="chauffeur/r1",
        sequence=69,
        status="ended_unverified",
        supersedes_before_sequence=69,
    )
    for nid in ("chauffeur/r1/13", "chauffeur/r1/22"):
        ad._clear_wake_activity(nid)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=6)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    texts = [c.args[0].text for c in adapter.handle_message.await_args_list]
    assert len(texts) == 1
    assert "ride ended" in texts[0]
    assert ad.get_durable_delegation("chauffeur/r1/13")["delivery_state"] == "superseded"
    assert ad.get_durable_delegation("chauffeur/r1/22")["delivery_state"] == "superseded"
