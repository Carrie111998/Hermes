"""Tests for the optional systemd event-loop watchdog protocol."""

from __future__ import annotations

import asyncio
import os
import socket

import pytest


def _install_valid_env(monkeypatch, *, usec: str = "20000000") -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setenv("WATCHDOG_USEC", usec)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="Unix datagram sockets are unavailable"
)
def test_notify_supports_systemd_abstract_socket(monkeypatch):
    name = "\0hermes-test-notify"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(name)
    receiver.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", "@hermes-test-notify")

    try:
        from gateway.systemd_notify import notify

        assert notify("WATCHDOG=1") is True
        assert receiver.recv(4096) == b"WATCHDOG=1"
    finally:
        receiver.close()


def test_notify_uses_nonblocking_datagram_send(monkeypatch):
    calls: list[object] = []

    class _Sender:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def setblocking(self, value):
            calls.append(("setblocking", value))

        def connect(self, address):
            calls.append(("connect", address))

        def send(self, payload):
            calls.append(("send", payload))

    import gateway.systemd_notify as notify_mod

    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setattr(notify_mod.socket, "socket", lambda *_args: _Sender())

    assert notify_mod.notify("READY=1") is True
    assert calls[0] == ("setblocking", False)


@pytest.mark.asyncio
async def test_watchdog_sends_ready_heartbeat_and_stopping(monkeypatch):
    calls: list[str] = []
    tick_seen = asyncio.Event()
    _install_valid_env(monkeypatch, usec="20000")

    import gateway.systemd_notify as notify_mod

    def capture_notify(message: str) -> bool:
        calls.append(message)
        if message == "WATCHDOG=1":
            tick_seen.set()
        return True

    monkeypatch.setattr(notify_mod, "notify", capture_notify)
    watchdog = notify_mod.SystemdWatchdog(
        ping_interval_seconds=0.001,
        lag_tolerance_seconds=0.001,
    )

    assert watchdog.start() is True
    assert watchdog.ready("Gateway running") is True
    await asyncio.wait_for(tick_seen.wait(), timeout=2.0)
    await watchdog.stop()

    assert any(message.startswith("READY=1") for message in calls)
    assert "WATCHDOG=1" in calls
    assert calls[-1] == "STOPPING=1"
    assert watchdog.unhealthy is False


# ── Finding 1: STATUS newline injection ─────────────────────────────────────


def test_sanitize_status_strips_injected_newlines(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    captured: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: captured.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog()

    injected = 'ok\nWATCHDOG=triggered'
    assert watchdog.ready(injected) is True
    payload = captured[-1]
    lines = payload.split("\n")
    assert len(lines) == 2
    assert lines[0] == "READY=1"
    assert lines[1] == "STATUS=ok WATCHDOG=triggered"


def test_sanitize_status_replaces_cr_lf_and_nul(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    captured: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: captured.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog()

    injected = "ok\r\nWATCHDOG=triggered\0extra"
    assert watchdog.ready(injected) is True
    payload = captured[-1]
    assert payload.count("\n") == 1
    assert "\r" not in payload
    assert "\0" not in payload
    lines = payload.split("\n")
    assert len(lines) == 2
    assert lines[0] == "READY=1"
    assert lines[1] == "STATUS=ok  WATCHDOG=triggered extra"


# ── Finding 2: real enablement gating ─────────────────────────────────────────


def test_config_disabled_is_noop_even_with_valid_env(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog(config_enabled=False)

    assert watchdog.enabled is False
    assert watchdog.start() is False
    assert watchdog.ready("ignored") is False
    assert watchdog.record_tick(deadline=0.0, now=0.0) is False
    assert calls == []


@pytest.mark.parametrize(
    "env_patch",
    [
        {"NOTIFY_SOCKET": ""},
        {"WATCHDOG_USEC": ""},
        {"WATCHDOG_USEC": "0"},
        {"WATCHDOG_USEC": "-1000"},
        {"WATCHDOG_USEC": "not-a-number"},
        {"WATCHDOG_PID": "999999999"},
    ],
)
def test_start_returns_false_without_full_runtime_env(monkeypatch, env_patch):
    _install_valid_env(monkeypatch)
    for key, value in env_patch.items():
        monkeypatch.setenv(key, value)

    import gateway.systemd_notify as notify_mod

    watchdog = notify_mod.SystemdWatchdog()
    assert watchdog.enabled is False
    assert watchdog.start() is False
    assert watchdog._task is None


def test_override_does_not_bypass_missing_watchdog_usec(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))

    import gateway.systemd_notify as notify_mod

    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=1.0)
    assert watchdog.enabled is False
    assert watchdog.start() is False


def test_missing_af_unix_disables_watchdog(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    class _NoAfUnixModule:
        SOCK_DGRAM = socket.SOCK_DGRAM

        def socket(self, *args, **kwargs):
            return socket.socket(*args, **kwargs)

    monkeypatch.setattr(notify_mod, "socket", _NoAfUnixModule())

    watchdog = notify_mod.SystemdWatchdog()
    assert watchdog.enabled is False
    assert watchdog.start() is False


def test_watchdog_pid_match_enables_mismatch_disables(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    assert notify_mod.SystemdWatchdog().enabled is True

    monkeypatch.setenv("WATCHDOG_PID", "999999999")
    assert notify_mod.SystemdWatchdog().enabled is False


# ── Finding 3: true lateness measurement ────────────────────────────────────


def test_on_time_tick_has_near_zero_lag(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog()
    watchdog.start()

    deadline = 100.0
    now = 100.0001
    assert watchdog.record_tick(deadline=deadline, now=now) is True
    assert "WATCHDOG=1" in calls
    assert watchdog.unhealthy is False


def test_lag_within_tolerance_pings(monkeypatch):
    _install_valid_env(monkeypatch, usec="10000000")
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog()
    watchdog.start()

    tolerance = watchdog._resolve_lag_tolerance()
    half = tolerance / 2.0
    assert watchdog.record_tick(deadline=0.0, now=half) is True
    assert "WATCHDOG=1" in calls


def test_lag_at_or_over_tolerance_latches_unhealthy(monkeypatch):
    _install_valid_env(monkeypatch, usec="10000000")
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog()
    watchdog.start()

    tolerance = watchdog._resolve_lag_tolerance()
    assert watchdog.record_tick(deadline=0.0, now=tolerance + 0.001) is False
    assert watchdog.unhealthy is True
    assert "WATCHDOG=1" not in [c for c in calls if c == "WATCHDOG=1" or c.startswith("STATUS=")]
    status_msgs = [c for c in calls if c.startswith("STATUS=")]
    assert status_msgs


def test_interval_plus_tolerance_stays_inside_watchdog_budget(monkeypatch):
    _install_valid_env(monkeypatch, usec="20000000")
    import gateway.systemd_notify as notify_mod

    watchdog = notify_mod.SystemdWatchdog()
    watchdog.start()
    wd = notify_mod.watchdog_interval_seconds()
    interval = watchdog._resolved_ping_interval
    tolerance = watchdog._resolve_lag_tolerance()
    assert interval is not None
    assert wd is not None
    assert interval + tolerance < wd


# ── Finding 4: restart resets unhealthy latch ─────────────────────────────────


@pytest.mark.asyncio
async def test_restart_after_unhealthy_resets_latch(monkeypatch):
    _install_valid_env(monkeypatch, usec="10000000")
    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda _message: True)
    watchdog = notify_mod.SystemdWatchdog()
    watchdog.start()

    tolerance = watchdog._resolve_lag_tolerance()
    watchdog.record_tick(deadline=0.0, now=tolerance + 1.0)
    assert watchdog.unhealthy is True

    await watchdog.stop()
    assert watchdog.start() is True
    assert watchdog.unhealthy is False
    assert watchdog.record_tick(deadline=0.0, now=0.0) is True


# ── Finding 5: lifecycle race hardening ───────────────────────────────────────


@pytest.mark.asyncio
async def test_repeated_start_yields_one_task(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda _message: True)
    watchdog = notify_mod.SystemdWatchdog()

    assert watchdog.start() is True
    first_task = watchdog._task
    assert watchdog.start() is True
    assert watchdog._task is first_task

    await watchdog.stop()


@pytest.mark.asyncio
async def test_repeated_stop_sends_stopping_once(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=3600.0)
    watchdog.start()

    await watchdog.stop()
    await watchdog.stop()
    await watchdog.stop()

    stopping = [m for m in calls if m == "STOPPING=1"]
    assert len(stopping) == 1


@pytest.mark.asyncio
async def test_start_during_stop_is_refused(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda _message: True)

    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()

    async def slow_heartbeat(self) -> None:
        try:
            while not self._stopped:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_started.set()
            await release_cancel.wait()
            raise

    monkeypatch.setattr(notify_mod.SystemdWatchdog, "_heartbeat", slow_heartbeat)
    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=3600.0)
    watchdog.start()

    stop_task = asyncio.create_task(watchdog.stop())
    await cancel_started.wait()
    assert watchdog._state == notify_mod._STATE_STOPPING
    assert watchdog.start() is False
    release_cancel.set()
    await stop_task


# ── Finding 6: cancellation semantics ───────────────────────────────────────


@pytest.mark.asyncio
async def test_external_cancellation_of_stop_propagates(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog()

    heartbeat_started = asyncio.Event()
    heartbeat_exit = asyncio.Event()

    async def slow_cancel_heartbeat() -> None:
        heartbeat_started.set()
        try:
            await heartbeat_exit.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise

    watchdog._resolved_ping_interval = 3600.0
    watchdog._state = notify_mod._STATE_RUNNING
    watchdog._lock = asyncio.Lock()
    watchdog._task = asyncio.create_task(slow_cancel_heartbeat())
    await heartbeat_started.wait()

    stop_task = asyncio.create_task(watchdog.stop())
    await asyncio.sleep(0.01)
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    heartbeat_exit.set()


def test_start_without_event_loop_returns_false(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    def _no_running_loop() -> None:
        raise RuntimeError("no running event loop")

    def _no_event_loop() -> None:
        raise RuntimeError("no current event loop")

    monkeypatch.setattr(asyncio, "get_running_loop", _no_running_loop)
    monkeypatch.setattr(asyncio, "get_event_loop", _no_event_loop)

    watchdog = notify_mod.SystemdWatchdog()
    assert watchdog.start() is False
    assert watchdog._state == notify_mod._STATE_IDLE
    assert watchdog._task is None


@pytest.mark.asyncio
async def test_record_tick_noops_after_stop(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=3600.0)
    watchdog.start()
    await watchdog.stop()

    calls.clear()
    assert watchdog.record_tick(deadline=0.0, now=0.0) is False
    assert "WATCHDOG=1" not in calls
    assert not any(m.startswith("STATUS=") for m in calls)


@pytest.mark.asyncio
async def test_heartbeat_unexpected_error_latches_unhealthy(monkeypatch, caplog):
    _install_valid_env(monkeypatch, usec="20000")
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    tick_count = {"n": 0}

    def boom_record_tick(self, *, deadline, now):
        tick_count["n"] += 1
        if tick_count["n"] == 1:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(notify_mod.SystemdWatchdog, "record_tick", boom_record_tick)

    async def instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=0.001)
    assert watchdog.start() is True
    task = watchdog._task
    assert task is not None

    await asyncio.wait_for(task, timeout=2.0)

    assert watchdog.unhealthy is True
    assert watchdog._last_error == "boom"
    assert watchdog._state == notify_mod._STATE_RUNNING
    assert watchdog._task is not None
    assert watchdog._task.done()
    status_msgs = [m for m in calls if m.startswith("STATUS=")]
    assert status_msgs
    assert "heartbeat error" in status_msgs[-1]


# ── Round-2 finding A: self-exit must not skip STOPPING on stop() ───────────


@pytest.mark.asyncio
async def test_unhealthy_self_exit_stop_still_sends_stopping_once(monkeypatch):
    _install_valid_env(monkeypatch, usec="20000")
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )

    async def instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    real_record_tick = notify_mod.SystemdWatchdog.record_tick
    tick_count = {"n": 0}

    def latch_on_first_tick(self, *, deadline, now):
        tick_count["n"] += 1
        if tick_count["n"] == 1:
            tolerance = self._resolve_lag_tolerance()
            return real_record_tick(self, deadline=0.0, now=tolerance + 1.0)
        return real_record_tick(self, deadline=deadline, now=now)

    monkeypatch.setattr(
        notify_mod.SystemdWatchdog, "record_tick", latch_on_first_tick
    )

    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=0.001)
    assert watchdog.start() is True
    task = watchdog._task
    assert task is not None

    await asyncio.wait_for(task, timeout=2.0)

    assert watchdog.unhealthy is True
    assert watchdog._state == notify_mod._STATE_RUNNING
    assert task.done()

    await watchdog.stop()

    stopping = [m for m in calls if m == "STOPPING=1"]
    assert len(stopping) == 1
    assert watchdog._state == notify_mod._STATE_IDLE
    assert watchdog._task is None


@pytest.mark.asyncio
async def test_heartbeat_error_stop_still_sends_stopping_once(monkeypatch):
    _install_valid_env(monkeypatch, usec="20000")
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    tick_count = {"n": 0}

    def boom_record_tick(self, *, deadline, now):
        tick_count["n"] += 1
        if tick_count["n"] == 1:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(notify_mod.SystemdWatchdog, "record_tick", boom_record_tick)

    async def instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )

    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=0.001)
    assert watchdog.start() is True
    task = watchdog._task
    assert task is not None

    await asyncio.wait_for(task, timeout=2.0)

    assert watchdog.unhealthy is True
    assert task.done()

    await watchdog.stop()

    stopping = [m for m in calls if m == "STOPPING=1"]
    assert len(stopping) == 1
    assert watchdog._state == notify_mod._STATE_IDLE


@pytest.mark.asyncio
async def test_start_after_self_exit_then_stop_sends_stopping_once(monkeypatch):
    _install_valid_env(monkeypatch, usec="20000")
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )

    async def instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    real_record_tick = notify_mod.SystemdWatchdog.record_tick
    tick_count = {"n": 0}

    def latch_on_first_tick(self, *, deadline, now):
        tick_count["n"] += 1
        if tick_count["n"] == 1:
            tolerance = self._resolve_lag_tolerance()
            return real_record_tick(self, deadline=0.0, now=tolerance + 1.0)
        return real_record_tick(self, deadline=deadline, now=now)

    monkeypatch.setattr(
        notify_mod.SystemdWatchdog, "record_tick", latch_on_first_tick
    )

    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=0.001)
    assert watchdog.start() is True
    task = watchdog._task
    assert task is not None

    await asyncio.wait_for(task, timeout=2.0)
    assert watchdog.unhealthy is True
    assert task.done()

    assert watchdog.start() is True
    assert watchdog.unhealthy is False
    assert watchdog._task is not None
    assert not watchdog._task.done()

    await watchdog.stop()

    stopping = [m for m in calls if m == "STOPPING=1"]
    assert len(stopping) == 1


# ── Round-2 finding B: cancelled stop must not orphan overlapping heartbeats ──


@pytest.mark.asyncio
async def test_cancelled_stop_cleans_up_and_allows_restart(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )

    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()
    heartbeat_exited = asyncio.Event()

    async def slow_heartbeat(self) -> None:
        try:
            while not self._stopped:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_started.set()
            await release_cancel.wait()
            raise
        finally:
            heartbeat_exited.set()

    monkeypatch.setattr(notify_mod.SystemdWatchdog, "_heartbeat", slow_heartbeat)
    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=3600.0)
    old_task = None
    assert watchdog.start() is True
    old_task = watchdog._task

    stop_task = asyncio.create_task(watchdog.stop())
    await cancel_started.wait()
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert watchdog._state == notify_mod._STATE_IDLE
    stopping = [m for m in calls if m == "STOPPING=1"]
    assert len(stopping) == 1
    assert old_task is not None

    if not old_task.done():
        assert watchdog._task is old_task
        assert watchdog.start() is False
        release_cancel.set()
        await asyncio.wait_for(heartbeat_exited.wait(), timeout=2.0)
    else:
        # External cancellation can finish the heartbeat before we inspect;
        # finally still must have cleared a completed handle.
        assert watchdog._task is None
        release_cancel.set()

    assert old_task.done()
    assert watchdog.start() is True
    assert watchdog._task is not None
    assert watchdog._task is not old_task
    await watchdog.stop()
    stopping_after_restart = [m for m in calls if m == "STOPPING=1"]
    assert len(stopping_after_restart) == len(stopping) + 1
    await watchdog.stop()
    assert len([m for m in calls if m == "STOPPING=1"]) == len(stopping_after_restart)


@pytest.mark.asyncio
async def test_start_refuses_orphaned_live_heartbeat(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda _message: True)
    watchdog = notify_mod.SystemdWatchdog()
    heartbeat_running = asyncio.Event()
    release = asyncio.Event()

    async def orphan_heartbeat() -> None:
        heartbeat_running.set()
        await release.wait()

    watchdog._state = notify_mod._STATE_IDLE
    watchdog._task = asyncio.create_task(orphan_heartbeat())
    await heartbeat_running.wait()

    assert watchdog.start() is False
    assert not watchdog._task.done()

    release.set()
    await watchdog._task
    watchdog._task = None
    assert watchdog.start() is True
    await watchdog.stop()


# ── Finding 7: override validation ──────────────────────────────────────────


@pytest.mark.parametrize(
    "override",
    [0, -1, float("nan"), float("inf"), float("-inf"), "bad"],
)
def test_invalid_ping_interval_override_falls_back(monkeypatch, override):
    _install_valid_env(monkeypatch, usec="20000000")
    import gateway.systemd_notify as notify_mod

    wd = notify_mod.watchdog_interval_seconds()
    assert wd is not None
    expected = max(wd / 2.0, 0.001)

    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=override)
    resolved = watchdog._resolve_ping_interval()
    assert resolved == expected


@pytest.mark.parametrize(
    "override",
    [0, -1, float("nan"), float("inf"), float("-inf"), "bad"],
)
def test_invalid_lag_tolerance_override_falls_back(monkeypatch, override):
    _install_valid_env(monkeypatch, usec="20000000")
    import gateway.systemd_notify as notify_mod

    watchdog = notify_mod.SystemdWatchdog(lag_tolerance_seconds=override)
    watchdog.start()
    wd = notify_mod.watchdog_interval_seconds()
    interval = watchdog._resolved_ping_interval
    assert wd is not None and interval is not None
    safe = watchdog._safe_default_tolerance(wd, interval)
    assert watchdog._resolve_lag_tolerance() == safe


def test_budget_exceeding_overrides_rejected(monkeypatch):
    _install_valid_env(monkeypatch, usec="1000000")
    import gateway.systemd_notify as notify_mod

    wd = notify_mod.watchdog_interval_seconds()
    assert wd is not None
    default_interval = max(wd / 2.0, 0.001)

    too_fast = notify_mod.SystemdWatchdog(ping_interval_seconds=wd - 0.0005)
    assert too_fast._resolve_ping_interval() == default_interval

    too_lax = notify_mod.SystemdWatchdog(lag_tolerance_seconds=wd * 0.9)
    too_lax.start()
    safe = too_lax._safe_default_tolerance(wd, too_lax._resolved_ping_interval)
    assert too_lax._resolve_lag_tolerance() == safe


# ── Finding 8: best-effort robustness ─────────────────────────────────────────


@pytest.mark.parametrize(
    "deadline,now",
    [
        (float("nan"), 1.0),
        (1.0, float("inf")),
        (None, 1.0),
        ("bad", 1.0),
    ],
)
def test_record_tick_invalid_inputs_return_false_without_latching(
    monkeypatch, deadline, now
):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda _message: True)
    watchdog = notify_mod.SystemdWatchdog()
    watchdog.start()

    assert watchdog.record_tick(deadline=deadline, now=now) is False
    assert watchdog.unhealthy is False


def test_notify_failure_returns_false_without_raising(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda _message: False)
    watchdog = notify_mod.SystemdWatchdog()
    watchdog.start()

    assert watchdog.record_tick(deadline=0.0, now=0.0) is False
    assert watchdog.unhealthy is False


@pytest.mark.asyncio
async def test_heartbeat_survives_record_tick_notify_failure(monkeypatch):
    _install_valid_env(monkeypatch, usec="20000")
    import gateway.systemd_notify as notify_mod

    notify_calls = {"count": 0}
    tick_seen = asyncio.Event()

    def flaky_notify(message: str) -> bool:
        notify_calls["count"] += 1
        if message == "WATCHDOG=1":
            tick_seen.set()
        return message != "WATCHDOG=1"

    monkeypatch.setattr(notify_mod, "notify", flaky_notify)
    watchdog = notify_mod.SystemdWatchdog(
        ping_interval_seconds=0.001,
        lag_tolerance_seconds=0.001,
    )
    watchdog.start()
    await asyncio.wait_for(tick_seen.wait(), timeout=2.0)
    await watchdog.stop()
    assert notify_calls["count"] >= 1
    assert watchdog.unhealthy is False


# ── Round-3: huge-int / overflow robustness ───────────────────────────────────


_HUGE_INT = 10**10000


def test_record_tick_huge_int_returns_false_without_latching(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda _message: True)
    watchdog = notify_mod.SystemdWatchdog()
    watchdog.start()

    assert watchdog.record_tick(deadline=_HUGE_INT, now=1.0) is False
    assert watchdog.unhealthy is False
    assert watchdog.record_tick(deadline=0.0, now=_HUGE_INT) is False
    assert watchdog.unhealthy is False


def test_huge_int_overrides_fall_back_to_safe_defaults(monkeypatch):
    _install_valid_env(monkeypatch, usec="20000000")
    import gateway.systemd_notify as notify_mod

    wd = notify_mod.watchdog_interval_seconds()
    assert wd is not None
    expected_interval = max(wd / 2.0, 0.001)

    ping_watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=_HUGE_INT)
    assert ping_watchdog._resolve_ping_interval() == expected_interval
    assert ping_watchdog.start() is True

    lag_watchdog = notify_mod.SystemdWatchdog(lag_tolerance_seconds=_HUGE_INT)
    lag_watchdog.start()
    interval = lag_watchdog._resolved_ping_interval
    assert interval is not None
    safe_tolerance = lag_watchdog._safe_default_tolerance(wd, interval)
    assert lag_watchdog._resolve_lag_tolerance() == safe_tolerance


def test_huge_watchdog_usec_disables_cleanly(monkeypatch):
    huge_usec = "1" + "0" * 399
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setenv("WATCHDOG_USEC", huge_usec)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))

    import gateway.systemd_notify as notify_mod

    assert notify_mod.watchdog_interval_seconds() is None

    watchdog = notify_mod.SystemdWatchdog()
    assert watchdog.enabled is False
    assert watchdog.start() is False
    assert watchdog.ready("ignored") is False
    assert watchdog.record_tick(deadline=0.0, now=0.0) is False


# ── Round-3: orphan heartbeat done-callback reconciliation ────────────────────


@pytest.mark.asyncio
async def test_orphan_heartbeat_auto_clears_after_cancelled_stop(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )

    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()
    heartbeat_exited = asyncio.Event()

    async def slow_heartbeat(self) -> None:
        try:
            while not self._stopped:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_started.set()
            await release_cancel.wait()
            raise
        finally:
            heartbeat_exited.set()

    monkeypatch.setattr(notify_mod.SystemdWatchdog, "_heartbeat", slow_heartbeat)
    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=3600.0)
    assert watchdog.start() is True
    orphan_task = watchdog._task
    assert orphan_task is not None

    stop_task = asyncio.create_task(watchdog.stop())
    await cancel_started.wait()
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert watchdog._state == notify_mod._STATE_IDLE
    stopping = [m for m in calls if m == "STOPPING=1"]
    assert len(stopping) == 1

    if not orphan_task.done():
        assert watchdog._task is orphan_task
        release_cancel.set()
        await asyncio.wait_for(heartbeat_exited.wait(), timeout=2.0)
    else:
        release_cancel.set()

    try:
        await orphan_task
    except asyncio.CancelledError:
        pass

    assert orphan_task.done()
    assert watchdog._task is None

    assert watchdog.start() is True
    assert watchdog._task is not None
    assert watchdog._task is not orphan_task

    await watchdog.stop()
    stopping_after_restart = [m for m in calls if m == "STOPPING=1"]
    assert len(stopping_after_restart) == 2


@pytest.mark.asyncio
async def test_orphan_done_callback_identity_check_preserves_new_task(monkeypatch):
    _install_valid_env(monkeypatch)
    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda _message: True)

    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()

    async def slow_heartbeat(self) -> None:
        try:
            while not self._stopped:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_started.set()
            await release_cancel.wait()
            raise

    monkeypatch.setattr(notify_mod.SystemdWatchdog, "_heartbeat", slow_heartbeat)
    watchdog = notify_mod.SystemdWatchdog(ping_interval_seconds=3600.0)
    assert watchdog.start() is True
    orphan_task = watchdog._task
    assert orphan_task is not None

    stop_task = asyncio.create_task(watchdog.stop())
    await cancel_started.wait()
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    release_cancel.set()
    try:
        await orphan_task
    except asyncio.CancelledError:
        pass
    assert orphan_task.done()
    assert watchdog._task is None

    assert watchdog.start() is True
    new_task = watchdog._task
    assert new_task is not None
    assert new_task is not orphan_task

    watchdog._on_heartbeat_done(orphan_task)

    assert watchdog._task is new_task
    assert watchdog._state == notify_mod._STATE_RUNNING

    await watchdog.stop()
