"""Defer-to-gateway guard on the desktop cron ticker (2026-07-16).

The desktop-spawned `hermes serve` backend (HERMES_DESKTOP=1) runs its own
cron ticker, designed for installs with no gateway. On a machine where a real
`hermes gateway run` is alive, both processes compete for the per-tick file
lock and the serve process can phase-lock into winning every tick for hours —
running LLM cron jobs inside the process that serves the TUI (GIL stalls),
without the gateway's live delivery adapters, and dying when the app closes.

The guard: each loop iteration stats the machine gateway's liveness file
(events.paths.gateway_heartbeat_path(), written every 60s by the gateway) and
skips the tick while it is fresh. Stale or missing heartbeat = tick as before,
so desktop-only installs and gateway-downtime fallback are unchanged.

HERMES_DESKTOP_CRON overrides: "0" = never tick, "1" = legacy always-tick,
unset/other = the auto heartbeat guard.
"""
import logging
import os
import threading
import time
from unittest.mock import patch

import pytest


def _wait_until(predicate, timeout=10.0, interval=0.005):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _write_gateway_heartbeat(age_seconds=0):
    """Create the machine gateway heartbeat file with the given mtime age."""
    from events.paths import gateway_heartbeat_path

    path = gateway_heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


def _ticker_heartbeat_file():
    from cron.jobs import _get_ticker_heartbeat_file

    return _get_ticker_heartbeat_file()


def _run_ticker(stop, **kwargs):
    from hermes_cli.web_server import _start_desktop_cron_ticker

    kwargs.setdefault("interval", 0.01)
    t = threading.Thread(
        target=_start_desktop_cron_ticker, args=(stop,), kwargs=kwargs, daemon=True
    )
    t.start()
    return t


class TestMachineGatewayAlive:
    def test_fresh_heartbeat_is_alive(self):
        from hermes_cli.web_server import _machine_gateway_alive

        _write_gateway_heartbeat(age_seconds=0)
        assert _machine_gateway_alive() is True

    def test_stale_heartbeat_is_not_alive(self):
        from hermes_cli.web_server import _machine_gateway_alive

        _write_gateway_heartbeat(age_seconds=600)
        assert _machine_gateway_alive() is False

    def test_missing_heartbeat_is_not_alive(self):
        from hermes_cli.web_server import _machine_gateway_alive

        from events.paths import gateway_heartbeat_path

        assert not gateway_heartbeat_path().exists()
        assert _machine_gateway_alive() is False


class TestDeferToGatewayGuard:
    def test_defers_while_gateway_heartbeat_fresh(self):
        """Fresh gateway heartbeat: no tick fires and the cron ticker
        heartbeat is NOT written (the gateway owns both)."""
        _write_gateway_heartbeat(age_seconds=0)
        calls = []
        stop = threading.Event()

        with patch("cron.scheduler.tick", side_effect=lambda *a, **k: calls.append(k) or 0):
            t = _run_ticker(stop)
            time.sleep(0.3)
            stop.set()
            t.join(timeout=5)

        assert not t.is_alive()
        assert calls == [], "ticker fired a tick despite a fresh gateway heartbeat"
        assert not _ticker_heartbeat_file().exists(), (
            "deferring ticker must not write the cron ticker heartbeat"
        )

    def test_ticks_when_gateway_heartbeat_stale(self):
        _write_gateway_heartbeat(age_seconds=600)
        calls = []
        stop = threading.Event()

        with patch("cron.scheduler.tick", side_effect=lambda *a, **k: calls.append(k) or 0):
            t = _run_ticker(stop)
            assert _wait_until(lambda: len(calls) >= 1), "stale heartbeat should tick"
            stop.set()
            t.join(timeout=5)

        assert not t.is_alive()
        assert calls[0].get("sync") is False
        assert _ticker_heartbeat_file().exists()

    def test_ticks_when_gateway_heartbeat_missing(self):
        calls = []
        stop = threading.Event()

        with patch("cron.scheduler.tick", side_effect=lambda *a, **k: calls.append(k) or 0):
            t = _run_ticker(stop)
            assert _wait_until(lambda: len(calls) >= 1), "missing heartbeat should tick"
            stop.set()
            t.join(timeout=5)

        assert not t.is_alive()

    def test_resumes_ticking_when_heartbeat_goes_stale_mid_run(self):
        hb = _write_gateway_heartbeat(age_seconds=0)
        calls = []
        stop = threading.Event()

        with patch("cron.scheduler.tick", side_effect=lambda *a, **k: calls.append(k) or 0):
            t = _run_ticker(stop)
            time.sleep(0.2)
            assert calls == []
            stamp = time.time() - 600
            os.utime(hb, (stamp, stamp))
            assert _wait_until(lambda: len(calls) >= 1), (
                "ticker did not take over after the gateway heartbeat went stale"
            )
            stop.set()
            t.join(timeout=5)

        assert not t.is_alive()

    def test_logs_state_transitions_once(self, caplog):
        hb = _write_gateway_heartbeat(age_seconds=0)
        calls = []
        stop = threading.Event()

        with caplog.at_level(logging.INFO, logger="hermes_cli.web_server"):
            with patch("cron.scheduler.tick", side_effect=lambda *a, **k: calls.append(k) or 0):
                t = _run_ticker(stop)
                time.sleep(0.25)
                stamp = time.time() - 600
                os.utime(hb, (stamp, stamp))
                assert _wait_until(lambda: len(calls) >= 3)
                stop.set()
                t.join(timeout=5)

        defer_lines = [r for r in caplog.records if "deferring" in r.getMessage()]
        active_lines = [r for r in caplog.records if "ticker active" in r.getMessage()]
        assert len(defer_lines) == 1, "deferring must be logged exactly once per transition"
        assert len(active_lines) == 1, "activation must be logged exactly once per transition"


class TestEnvOverrides:
    def test_hermes_desktop_cron_0_never_ticks(self, monkeypatch):
        monkeypatch.setenv("HERMES_DESKTOP_CRON", "0")
        calls = []
        stop = threading.Event()

        with patch("cron.scheduler.tick", side_effect=lambda *a, **k: calls.append(k) or 0):
            t = _run_ticker(stop)
            t.join(timeout=5)  # disabled mode returns without waiting on stop

        assert not t.is_alive(), "HERMES_DESKTOP_CRON=0 should return immediately"
        assert calls == []

    def test_hermes_desktop_cron_1_always_ticks(self, monkeypatch):
        monkeypatch.setenv("HERMES_DESKTOP_CRON", "1")
        _write_gateway_heartbeat(age_seconds=0)  # fresh — would defer in auto mode
        calls = []
        stop = threading.Event()

        with patch("cron.scheduler.tick", side_effect=lambda *a, **k: calls.append(k) or 0):
            t = _run_ticker(stop, interval=0)
            assert _wait_until(lambda: len(calls) >= 1), (
                "HERMES_DESKTOP_CRON=1 must tick despite a fresh gateway heartbeat"
            )
            stop.set()
            t.join(timeout=5)

        assert not t.is_alive()


class TestNonBuiltinProvider:
    def test_external_provider_bypasses_guard(self):
        """A configured external provider keeps the legacy blocking start();
        the guard is builtin-only (external providers arm schedules, they
        don't run a local tick loop worth deferring)."""

        class FakeProvider:
            name = "chronos"

            def __init__(self):
                self.started = []

            def start(self, stop_event, **kwargs):
                self.started.append(kwargs)

        fake = FakeProvider()
        _write_gateway_heartbeat(age_seconds=0)
        stop = threading.Event()

        with patch("cron.scheduler_provider.resolve_cron_scheduler", return_value=fake):
            t = _run_ticker(stop, interval=7)
            t.join(timeout=5)

        assert not t.is_alive()
        assert fake.started == [{"interval": 7}]
