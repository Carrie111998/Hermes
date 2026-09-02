"""Child-side liveness for SSH-isolated ``hermes serve`` (#101626).

The remote backend is ``setsid``/``nohup`` detached on purpose (#91668), so
``PPID=1`` is not an orphan signal. These tests pin the actual contract:
idle grace after a missing client, exclusive per-home writer lock, and
leaving plain loopback Desktop pings disabled.
"""

from __future__ import annotations

import os

import pytest

from hermes_cli.ssh_isolated_liveness import (
    SshIsolatedIdleTracker,
    acquire_ssh_isolated_home_lock,
    ssh_isolated_idle_step,
    ssh_isolated_should_exit,
    ssh_isolated_ws_ping_window,
    track_ssh_isolated_ws,
)


def test_plain_loopback_keeps_protocol_ping_disabled():
    assert ssh_isolated_ws_ping_window(
        is_loopback=True,
        ssh_session_token="",
        default_interval=20.0,
        default_timeout=20.0,
    ) == (None, None)


def test_ssh_isolated_loopback_enables_half_open_ping():
    interval, timeout = ssh_isolated_ws_ping_window(
        is_loopback=True,
        ssh_session_token="a" * 64,
        default_interval=20.0,
        default_timeout=20.0,
    )
    assert interval and interval >= 60.0
    assert timeout and timeout >= 300.0
    assert timeout >= interval


def test_non_loopback_ping_unchanged_without_ssh_token():
    interval, timeout = ssh_isolated_ws_ping_window(
        is_loopback=False,
        ssh_session_token="",
        default_interval=20.0,
        default_timeout=25.0,
    )
    assert interval == 20.0
    assert timeout == 25.0


def test_idle_grace_does_not_exit_without_ssh_token():
    assert (
        ssh_isolated_should_exit(
            has_ssh_token=False,
            now=1_000.0,
            last_client_at=0.0,
            grace_s=10.0,
            ppid=1,
        )
        is False
    )


def test_ppid_one_is_not_an_exit_signal_while_client_is_recent():
    assert (
        ssh_isolated_should_exit(
            has_ssh_token=True,
            now=1_000.0,
            last_client_at=999.0,
            grace_s=30.0,
            ppid=1,
        )
        is False
    )


def test_ssh_isolated_exits_after_idle_grace():
    assert (
        ssh_isolated_should_exit(
            has_ssh_token=True,
            now=1_000.0,
            last_client_at=900.0,
            grace_s=30.0,
            ppid=1,
        )
        is True
    )


def test_ssh_isolated_stays_up_inside_grace_window():
    assert (
        ssh_isolated_should_exit(
            has_ssh_token=True,
            now=1_000.0,
            last_client_at=980.0,
            grace_s=30.0,
            ppid=42,
        )
        is False
    )


def test_idle_grace_does_not_exit_while_agent_turn_is_in_flight():
    assert (
        ssh_isolated_should_exit(
            has_ssh_token=True,
            now=1_000.0,
            last_client_at=0.0,
            grace_s=10.0,
            turn_in_flight=True,
        )
        is False
    )


def test_idle_step_refreshes_grace_while_turn_runs_then_exits_after():
    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    tracker = SshIsolatedIdleTracker(clock=clock)
    clock.now = 50.0
    assert (
        ssh_isolated_idle_step(
            has_ssh_token=True,
            tracker=tracker,
            grace_s=10.0,
            turn_in_flight=True,
        )
        is False
    )
    clock.now = 55.0
    assert (
        ssh_isolated_idle_step(
            has_ssh_token=True,
            tracker=tracker,
            grace_s=10.0,
            turn_in_flight=False,
        )
        is False
    )
    clock.now = 70.0
    assert (
        ssh_isolated_idle_step(
            has_ssh_token=True,
            tracker=tracker,
            grace_s=10.0,
            turn_in_flight=False,
        )
        is True
    )


def test_track_ws_context_does_not_leak_on_error():
    tracker = SshIsolatedIdleTracker()
    import hermes_cli.ssh_isolated_liveness as mod

    previous = mod._idle_tracker
    mod._idle_tracker = tracker
    try:
        assert tracker.live_count() == 0
        with track_ssh_isolated_ws():
            assert tracker.live_count() == 1
            raise RuntimeError("disconnect")
    except RuntimeError:
        pass
    finally:
        mod._idle_tracker = previous
    assert tracker.live_count() == 0


@pytest.mark.linux_only
def test_second_ssh_isolated_serve_cannot_take_the_home_lock(tmp_path):
    first = acquire_ssh_isolated_home_lock(tmp_path)
    assert first is not None
    try:
        second = acquire_ssh_isolated_home_lock(tmp_path)
        assert second is None
    finally:
        os.close(first)
