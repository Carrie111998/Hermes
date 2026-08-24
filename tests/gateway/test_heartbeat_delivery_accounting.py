"""Gateway heartbeat poll: fire accounting must reflect real delivery.

A heartbeat tick is only "fired" once its staged prompt actually becomes
a turn. Claims that vanish without any turn must be counted as missed
(with a warning), a session without a live adapter must never be claimed
in the first place, and a claim that hangs forever (staged event stuck
in the pending slot, no turn starts) must be abandoned loudly after the
claim timeout and re-claimed. Regression coverage for #92837.
"""

from __future__ import annotations

import asyncio
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
    runner._session_key_for_source = lambda _source: key
    # Deterministic claim timeout (real config is not consulted in tests);
    # individual tests re-patch to a tiny value for timeout scenarios.
    from hermes_cli import heartbeat as hb_module

    monkeypatch.setattr(hb_module, "_default_claim_timeout_seconds", lambda: 300.0)
    return runner, adapter, key, source


@pytest.mark.asyncio
async def test_due_tick_stages_without_counting_fired(monkeypatch):
    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)

    await runner._poll_heartbeat_delivery_accounting_once()

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
    await runner._poll_heartbeat_delivery_accounting_once()

    # Simulate the drain consuming the staged tick: the pending slot is
    # emptied and the tagged event enters the live message pipeline —
    # the ONLY evidence that confirms a delivery.
    staged = adapter._pending_messages.pop(key, None)
    assert staged is not None and staged._hermes_heartbeat_tick is True
    runner._confirm_heartbeat_delivery_for_event(staged)
    await runner._poll_heartbeat_delivery_accounting_once()

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
    await runner._poll_heartbeat_delivery_accounting_once()

    # Session reset / stale-lock heal / eviction discards the staged event
    # without any turn ever running.
    adapter._pending_messages.pop(key, None)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        await runner._poll_heartbeat_delivery_accounting_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 0
    assert mgr.state.missed_count == 1
    assert mgr.state.claimed_at is None
    assert "never became a turn" in caplog.text or "no turn" in caplog.text

    # The tick was never delivered: it stays due and is re-claimed on the
    # following poll instead of being silently dropped.
    await runner._poll_heartbeat_delivery_accounting_once()
    assert adapter._pending_messages.get(key) is not None


@pytest.mark.asyncio
async def test_vanished_tick_with_unrelated_activity_counts_missed(monkeypatch, caplog):
    """Unrelated session activity is NOT delivery evidence (#92837).

    A turn that ran without consuming the tagged tick (or any other
    activity-timestamp refresh) must never confirm the delivery. Only the
    tagged event entering the live message pipeline confirms; a vanished
    staged event without that evidence counts as missed.
    """
    import logging

    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    await runner._poll_heartbeat_delivery_accounting_once()

    # The staged event disappears without ever entering the message
    # pipeline, but the session shows unrelated activity afterwards.
    adapter._pending_messages.pop(key, None)
    agent = type("Agent", (), {"_last_activity_ts": time.time()})()
    runner._agent_cache[key] = (agent,)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        await runner._poll_heartbeat_delivery_accounting_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 0
    assert mgr.state.missed_count == 1
    assert mgr.state.claimed_at is None


@pytest.mark.asyncio
async def test_claim_timeout_abandons_stuck_staged_tick_and_reclaims(monkeypatch, caplog):
    """The main #92837 failure mode: the staged tick sits in the pending
    slot forever and no turn ever starts. After the claim timeout the
    claim must be abandoned loudly, counted missed, and the still-due
    tick re-claimed and re-staged instead of hanging silently.
    """
    import logging

    from hermes_cli import heartbeat as hb_module

    runner, adapter, key, _source = _make_runner(monkeypatch)
    monkeypatch.setattr(hb_module, "_default_claim_timeout_seconds", lambda: 0.05)
    _due_heartbeat("session-1", 700)
    await runner._poll_heartbeat_delivery_accounting_once()
    assert key in runner._heartbeat_inflight
    staged_old = adapter._pending_messages.get(key)

    # The staged event never leaves the pending slot and no turn starts.
    await asyncio.sleep(0.2)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        await runner._poll_heartbeat_delivery_accounting_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 0
    assert mgr.state.missed_count == 1
    assert "no turn" in caplog.text
    # The claim is resolved and the stale staged tick discarded — the
    # tick stays due instead of hanging silently.
    assert mgr.state.claimed_at is None
    assert adapter._pending_messages.get(key) is None
    assert key not in runner._heartbeat_inflight

    # The following poll re-claims the still-due tick and re-stages a
    # fresh delivery attempt.
    await runner._poll_heartbeat_delivery_accounting_once()
    mgr2 = HeartbeatManager(session_id="session-1")
    assert mgr2.state.claimed_at is not None
    staged_new = adapter._pending_messages.get(key)
    assert staged_new is not None and staged_new is not staged_old
    assert key in runner._heartbeat_inflight


@pytest.mark.asyncio
async def test_watch_reregistration_clears_orphan_claim(monkeypatch, caplog):
    """Re-registering a watch clears a persisted claim whose in-process
    staging context was lost — otherwise the heartbeat stalls forever.
    """
    import logging

    runner, adapter, key, source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    await runner._poll_heartbeat_delivery_accounting_once()
    mgr0 = HeartbeatManager(session_id="session-1")
    assert mgr0.state.claimed_at is not None  # claimed by THIS process

    # The in-memory staging context is lost while the persisted claim
    # survives (watch + inflight rebuilt under the live process).
    runner._heartbeat_watch.pop(key, None)
    runner._heartbeat_inflight.pop(key, None)

    runner._start_heartbeat_poller = lambda: None  # avoid a stray task
    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        runner._register_heartbeat_watch(key, source, "session-1")

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.claimed_at is None
    assert mgr.state.missed_count == 1
    assert key in runner._heartbeat_watch


@pytest.mark.asyncio
async def test_unregister_watch_abandons_dangling_claim(monkeypatch, caplog):
    """Unregistering a watch (e.g. /heartbeat clear) resolves any
    in-flight claim so the persisted state keeps no dangling claim.
    """
    import logging

    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    await runner._poll_heartbeat_delivery_accounting_once()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.heartbeat"):
        runner._unregister_heartbeat_watch(key)

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.claimed_at is None
    assert mgr.state.missed_count == 1
    assert runner._heartbeat_watch == {}
    assert runner._heartbeat_inflight == {}


@pytest.mark.asyncio
async def test_missing_adapter_never_claims_the_tick(monkeypatch):
    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    runner._adapter_for_source = lambda _source: None

    await runner._poll_heartbeat_delivery_accounting_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 0
    assert mgr.state.claimed_at is None
    assert runner._heartbeat_inflight == {}


@pytest.mark.asyncio
async def test_busy_session_leaves_staged_tick_in_flight(monkeypatch):
    runner, adapter, key, _source = _make_runner(monkeypatch)
    _due_heartbeat("session-1", 700)
    await runner._poll_heartbeat_delivery_accounting_once()

    # A turn starts (session busy) but has not drained the slot yet.
    runner._running_agents[key] = object()
    await runner._poll_heartbeat_delivery_accounting_once()

    mgr = HeartbeatManager(session_id="session-1")
    assert mgr.state.fire_count == 0
    assert mgr.state.claimed_at is not None
    assert key in runner._heartbeat_inflight


@pytest.mark.asyncio
async def test_poll_reuses_cached_state_instead_of_rereading_disk(monkeypatch):
    """The 5s poll must not re-read heartbeat state from disk per watch.

    Without the mtime-checked cache, each watched session is reloaded from
    SessionDB on every poll inside the event loop (N reads per poll, every
    poll). With the cache: the cold poll loads each watch once, a poll that
    follows a real DB change re-reads each watch exactly once (the state
    genuinely changed), and a poll where nothing changed does ZERO disk
    reads regardless of watch count — the O(1) steady state.
    """
    import hermes_cli.heartbeat as hb

    runner, adapter, key, _source = _make_runner(monkeypatch)
    # Three watches sharing the same adapter.
    runner._heartbeat_watch = {}
    for chat, sid in (("42", "session-1"), ("43", "session-2"), ("44", "session-3")):
        src = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat,
            chat_type="dm",
            user_id=chat,
        )
        runner._heartbeat_watch[build_session_key(src)] = (src, sid)
        _due_heartbeat(sid, 700)

    real_load = hb.load_heartbeat
    reads = {"count": 0}

    def counting_load(session_id: str):
        reads["count"] += 1
        return real_load(session_id)

    monkeypatch.setattr(hb, "load_heartbeat", counting_load)

    await runner._poll_heartbeat_delivery_accounting_once()
    # Cold cache: each watched session loaded exactly once.
    assert reads["count"] == 3

    await runner._poll_heartbeat_delivery_accounting_once()
    # The previous poll's claims really changed the DB, so each watch is
    # re-read exactly once — never once per watch per poll.
    assert reads["count"] == 6

    await runner._poll_heartbeat_delivery_accounting_once()
    # Stable poll: nothing changed on disk -> ZERO reads for 3 watches
    # (uncached this would be 3 more reads, every 5s, forever).
    assert reads["count"] == 6

    # A real state change written through the same SessionDB (this fresh
    # manager's own load counts as one read).
    HeartbeatManager(session_id="session-1").pause()
    assert reads["count"] == 7

    await runner._poll_heartbeat_delivery_accounting_once()
    # The changed DB fingerprint is detected: exactly one re-read per
    # watch, then the cache is warm again.
    assert reads["count"] == 10
