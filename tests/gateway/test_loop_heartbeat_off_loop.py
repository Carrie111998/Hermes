"""Off-loop heartbeat write + two-witness confirmation before exit 75.

The on-loop asyncio heartbeat can stall the loop it is supposed to monitor
(atomic JSON write / fsync). Production must write the file from a thread
gated by a loop tick, and must not hard-exit until a second witness agrees
the loop is dead.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.gateway as gateway_cli
from gateway.shutdown_watchdog import (
    get_loop_heartbeat_path,
    start_loop_heartbeat,
    start_loop_liveness_watchdog,
    write_loop_heartbeat,
)


def _write_heartbeat(home, pid, age_s=0.0):
    path = get_loop_heartbeat_path(home)
    write_loop_heartbeat(pid=pid, home=home)
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


def test_off_loop_heartbeat_refreshes_while_loop_runs(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    handle = None
    try:
        handle = start_loop_heartbeat(
            loop,
            interval_s=0.05,
            tick_timeout_s=0.5,
            home=tmp_path,
            start_time=time.time(),
        )
        path = get_loop_heartbeat_path(tmp_path)
        deadline = time.time() + 2.0
        while time.time() < deadline and not path.is_file():
            time.sleep(0.02)
        assert path.is_file(), "heartbeat was never written"
        first = path.stat().st_mtime
        deadline = time.time() + 2.0
        while time.time() < deadline and path.stat().st_mtime <= first:
            time.sleep(0.02)
        assert path.stat().st_mtime > first
        writer_names = {t.name for t in threading.enumerate()}
        assert "hermes-loop-heartbeat" in writer_names
    finally:
        if handle is not None:
            handle.stop()
            handle.join(timeout=1.0)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1.0)
        loop.close()


def test_frozen_loop_does_not_refresh_heartbeat(tmp_path):
    loop = MagicMock(spec=asyncio.AbstractEventLoop)

    def never_run(callback):
        return None

    loop.call_soon_threadsafe.side_effect = never_run
    handle = start_loop_heartbeat(
        loop,
        interval_s=0.05,
        tick_timeout_s=0.05,
        home=tmp_path,
        start_time=time.time(),
    )
    try:
        time.sleep(0.2)
        assert not get_loop_heartbeat_path(tmp_path).exists()
    finally:
        handle.stop()
        handle.join(timeout=1.0)


def test_slow_heartbeat_write_does_not_block_loop(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    progressed = threading.Event()
    handle = None

    def slow_write(**kwargs):
        time.sleep(0.3)
        return write_loop_heartbeat(**kwargs)

    try:
        with patch("gateway.shutdown_watchdog.write_loop_heartbeat", side_effect=slow_write):
            handle = start_loop_heartbeat(
                loop,
                interval_s=0.05,
                tick_timeout_s=0.5,
                home=tmp_path,
                start_time=time.time(),
            )
            loop.call_soon_threadsafe(progressed.set)
            assert progressed.wait(timeout=0.2), "loop blocked by heartbeat write"
    finally:
        if handle is not None:
            handle.stop()
            handle.join(timeout=1.0)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1.0)
        loop.close()


def test_probe_requires_two_stale_samples_before_wedged(tmp_path, monkeypatch):
    _write_heartbeat(tmp_path, pid=4242, age_s=600.0)

    def refresh(_seconds):
        _write_heartbeat(tmp_path, pid=4242, age_s=0.0)

    monkeypatch.setattr(gateway_cli.time, "sleep", refresh)
    assert (
        gateway_cli.probe_gateway_loop_liveness(4242, home=tmp_path, confirm_s=1.0)
        == gateway_cli.GATEWAY_LOOP_ALIVE
    )


def test_probe_two_stale_samples_are_wedged(tmp_path, monkeypatch):
    _write_heartbeat(tmp_path, pid=4242, age_s=600.0)
    monkeypatch.setattr(gateway_cli.time, "sleep", lambda _seconds: None)
    assert (
        gateway_cli.probe_gateway_loop_liveness(4242, home=tmp_path, confirm_s=1.0)
        == gateway_cli.GATEWAY_LOOP_WEDGED
    )


def test_watchdog_holds_exit_75_when_heartbeat_is_fresh(tmp_path):
    write_loop_heartbeat(pid=os.getpid(), home=tmp_path)
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.call_soon_threadsafe.side_effect = lambda _cb: None
    with (
        patch("gateway.shutdown_watchdog.get_loop_heartbeat_path", return_value=get_loop_heartbeat_path(tmp_path)),
        patch("gateway.shutdown_watchdog.os._exit") as hard_exit,
        patch("gateway.shutdown_watchdog.logger.critical"),
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback"),
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        time.sleep(0.15)
        handle.stop()
        handle.join(timeout=1.0)
    hard_exit.assert_not_called()


def test_watchdog_exits_75_when_probe_and_heartbeat_agree_loop_is_dead(tmp_path):
    path = _write_heartbeat(tmp_path, pid=os.getpid(), age_s=600.0)
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.call_soon_threadsafe.side_effect = lambda _cb: None
    exit_codes = []
    fired = threading.Event()

    def fake_exit(code):
        if not fired.is_set():
            exit_codes.append(code)
            fired.set()

    with (
        patch("gateway.shutdown_watchdog.get_loop_heartbeat_path", return_value=path),
        patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit),
        patch("gateway.shutdown_watchdog.logger.critical"),
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback"),
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        assert fired.wait(timeout=2.0), "watchdog did not hard-exit"
        handle.stop()
        handle.join(timeout=1.0)
    assert exit_codes == [75]
