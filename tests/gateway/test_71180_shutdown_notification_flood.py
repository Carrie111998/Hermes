"""Regression test for #71180: gateway re-broadcasts the shutdown
notification to the same destination on every process start/stop cycle.

``_notify_active_sessions_of_shutdown`` dedupes sends within a *single*
shutdown via a local in-memory set, but that set is reset on every fresh
gateway process, so rapid host-driven restart cycling (WSL suspend/resume
tearing the distro down and back up, a flapping supervisor, ...) causes each
new process to re-announce a "first" shutdown seconds after the previous
process already announced one. ``restart_loop_guard.should_suppress_shutdown_notification``
persists the last-notified timestamp per destination across process
restarts so rapid re-cycling is suppressed while a genuine isolated
shutdown still notifies normally.
"""

import gateway.restart_loop_guard as restart_loop_guard
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

import pytest


@pytest.fixture(autouse=True)
def _isolated_notification_state(tmp_path, monkeypatch):
    monkeypatch.setattr(restart_loop_guard, "get_hermes_home", lambda: tmp_path)


async def _fresh_process_notifies(now: float, monkeypatch):
    """Simulate a brand-new gateway process (fresh in-memory dedup set)
    experiencing its own first shutdown and calling the shared
    notification path."""
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="active-42")
    session_key = build_session_key(source)
    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = type("E", (), {"origin": source})()

    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult

    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m"))

    import gateway.run as gateway_run  # noqa: F401
    monkeypatch.setattr(restart_loop_guard.time, "time", lambda: now)
    await runner._notify_active_sessions_of_shutdown()
    return adapter


@pytest.mark.asyncio
async def test_rapid_restart_cycling_suppresses_repeat_notifications(monkeypatch):
    """Simulates ~10 process cycles inside a 60s cooldown window (mirroring
    the WSL suspend/resume flap from the issue) — only the FIRST process's
    notification should actually send.
    """
    base = 1_000_000.0
    sends = []
    for i in range(10):
        adapter = await _fresh_process_notifies(base + i * 3, monkeypatch)  # every 3s, like a fast flap
        sends.append(adapter.send.await_count)

    assert sends[0] == 1, "first (genuine) shutdown must notify"
    assert sum(sends[1:]) == 0, (
        f"repeat process cycles inside the cooldown window must be suppressed, got per-cycle sends={sends}"
    )


@pytest.mark.asyncio
async def test_isolated_shutdown_after_cooldown_still_notifies(monkeypatch):
    """A genuine, isolated shutdown that happens well outside the cooldown
    window (not part of a rapid restart loop) must still notify normally.
    """
    base = 2_000_000.0
    first = await _fresh_process_notifies(base, monkeypatch)
    assert first.send.await_count == 1

    later = await _fresh_process_notifies(base + restart_loop_guard.DEFAULT_NOTIFICATION_COOLDOWN_SECONDS + 5, monkeypatch)
    assert later.send.await_count == 1, "shutdown outside the cooldown window must notify again"


def test_should_suppress_shutdown_notification_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(restart_loop_guard, "get_hermes_home", lambda: tmp_path)

    assert restart_loop_guard.should_suppress_shutdown_notification(
        "telegram:42:None", cooldown_seconds=60, now=1000.0
    ) is False
    # Immediately repeating within the cooldown is suppressed.
    assert restart_loop_guard.should_suppress_shutdown_notification(
        "telegram:42:None", cooldown_seconds=60, now=1005.0
    ) is True
    # Past the cooldown, it notifies again.
    assert restart_loop_guard.should_suppress_shutdown_notification(
        "telegram:42:None", cooldown_seconds=60, now=1065.0
    ) is False
    # A cooldown of 0 disables suppression entirely (legacy behavior).
    assert restart_loop_guard.should_suppress_shutdown_notification(
        "telegram:42:None", cooldown_seconds=0, now=1065.1
    ) is False
