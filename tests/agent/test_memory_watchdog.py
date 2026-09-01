"""Tests for agent/memory_watchdog.py — the agent-process memory watchdog.

Covers:
* resolve_settings: defaults (silent no-op), malformed sections, threshold
  and interval normalization (junk → safe defaults, never raises)
* start_memory_watchdog: idempotence per process, no thread when disabled,
  no thread when both thresholds are 0, first-caller cadence wins
* _watchdog loop: warn fires once per growth episode and re-arms via the
  10% hysteresis band; hard threshold exits (tested via _fire_exit
  monkeypatch — an os._exit in-process would kill the test runner)
* _sample_rss_kib: /proc path on Linux, psutil fallback, 0 on failure
* _fire_warn: writes the dump file with the header + thread stacks
* resolve_settings tolerance: never raises on junk input
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

import agent.memory_watchdog as mw


@pytest.fixture(autouse=True)
def _reset_process_singletons():
    """Isolate the process-level singleton state between tests."""
    mw.stop_memory_watchdog()
    mw._settings = {}
    yield
    mw.stop_memory_watchdog()
    mw._settings = {}


# ---------------------------------------------------------------------------
# resolve_settings
# ---------------------------------------------------------------------------


class TestResolveSettings:
    def test_default_section_is_silent_noop(self):
        # enabled defaults true but both thresholds 0 → nothing to watch
        s = mw.resolve_settings(None)
        assert s == {
            "enabled": True,
            "interval_s": 30.0,
            "warn_threshold_kib": 0,
            "hard_threshold_kib": 0,
        }

    def test_explicit_thresholds_roundtrip(self):
        s = mw.resolve_settings(
            {"enabled": True, "interval_s": 10, "warn_threshold_kib": 2048, "hard_threshold_kib": 4096}
        )
        assert s["warn_threshold_kib"] == 2048
        assert s["hard_threshold_kib"] == 4096
        assert s["interval_s"] == 10.0

    def test_malformed_section_falls_back_to_defaults(self):
        for junk in ("nope", 123, [1, 2], {"enabled": "banana", "warn_threshold_kib": "x"}):
            s = mw.resolve_settings(junk)
            assert s["enabled"] is True
            assert s["warn_threshold_kib"] == 0
            assert s["hard_threshold_kib"] == 0
            assert s["interval_s"] == 30.0

    def test_enabled_false_disables(self):
        s = mw.resolve_settings({"enabled": False, "warn_threshold_kib": 1024})
        assert s["enabled"] is False

    def test_negative_threshold_means_off(self):
        s = mw.resolve_settings({"warn_threshold_kib": -5, "hard_threshold_kib": -1})
        assert s["warn_threshold_kib"] == 0
        assert s["hard_threshold_kib"] == 0

    def test_interval_floor(self):
        assert mw.resolve_settings({"interval_s": 0.1, "warn_threshold_kib": 1})["interval_s"] == 5.0
        assert mw.resolve_settings({"interval_s": -3})["interval_s"] == 30.0
        assert mw.resolve_settings({"interval_s": "junk"})["interval_s"] == 30.0


# ---------------------------------------------------------------------------
# start_memory_watchdog — process-level singleton behavior
# ---------------------------------------------------------------------------


class TestStartWatchdog:
    def test_no_thread_when_disabled(self):
        assert mw.start_memory_watchdog({"enabled": False, "warn_threshold_kib": 1024}) is None

    def test_no_thread_when_both_thresholds_off(self):
        # Schema default state: enabled=true, thresholds 0 → silent no-op.
        assert mw.start_memory_watchdog(mw.resolve_settings(None)) is None

    def test_start_returns_thread(self):
        t = mw.start_memory_watchdog({"warn_threshold_kib": 10**9})
        assert t is not None and t.is_alive()
        assert t.daemon is True
        assert t.name == "agent-memory-watchdog"

    def test_idempotent_per_process(self):
        t1 = mw.start_memory_watchdog({"warn_threshold_kib": 10**9, "interval_s": 60})
        t2 = mw.start_memory_watchdog({"warn_threshold_kib": 10**9, "interval_s": 5})
        assert t1 is t2  # same thread object — no per-request leak
        assert t2 is not None and t2.is_alive()

    def test_stop_then_restart_starts_new_thread(self):
        t1 = mw.start_memory_watchdog({"warn_threshold_kib": 10**9})
        mw.stop_memory_watchdog()
        t2 = mw.start_memory_watchdog({"warn_threshold_kib": 10**9})
        assert t1 is not t2
        assert t2 is not None and t2.is_alive()


# ---------------------------------------------------------------------------
# _watchdog loop — warn episode + hysteresis re-arm (no real exit)
# ---------------------------------------------------------------------------


class TestWatchdogLoop:
    def test_warn_fires_once_per_episode_then_rearms(self, tmp_path, monkeypatch):
        fired = []

        def fake_fire_warn(rss, path):
            fired.append(rss)

        monkeypatch.setattr(mw, "_fire_warn", fake_fire_warn)
        monkeypatch.setattr(mw, "_fire_exit", lambda rss, path: None)
        stop = threading.Event()
        monkeypatch.setattr(mw, "_stop_event", stop)
        # Sample sequence: cross (fire), stay above (no re-fire), drop into
        # the re-arm band (< 90%), stay under, cross again (fire), stay.
        samples = iter([1200, 1300, 850, 960, 1100, 1100])
        monkeypatch.setattr(mw, "_sample_rss_kib", lambda: next(samples, 850))

        t = threading.Thread(
            target=mw._watchdog,
            kwargs={
                "interval_s": 0.02,
                "warn_threshold_kib": 1000,
                "hard_threshold_kib": 0,
                "dump_path": tmp_path / "dump.log",
            },
            daemon=True,
        )
        t.start()

        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()
        # Warn fired exactly at the two crossings, not on every sample
        # above the line, and re-armed after the band dip.
        assert len(fired) == 2

    def test_hard_threshold_calls_exit(self, tmp_path, monkeypatch):
        exits = []
        monkeypatch.setattr(mw, "_fire_exit", lambda rss, path: exits.append(rss))
        stop = threading.Event()
        monkeypatch.setattr(mw, "_stop_event", stop)
        monkeypatch.setattr(mw, "_sample_rss_kib", lambda: 5_000_000)

        t = threading.Thread(
            target=mw._watchdog,
            kwargs={
                "interval_s": 0.02,
                "warn_threshold_kib": 0,
                "hard_threshold_kib": 1_000_000,
                "dump_path": tmp_path / "dump.log",
            },
            daemon=True,
        )
        t.start()
        t.join(timeout=2)
        assert not t.is_alive()
        assert exits == [5_000_000]


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class TestSampling:
    def test_proc_path_returns_positive_int_on_linux(self):
        rss = mw._sample_rss_kib()
        assert isinstance(rss, int)
        assert rss > 0  # the test process itself always has RSS on Linux

    def test_all_paths_failing_returns_zero(self, monkeypatch):
        # /proc unavailable AND psutil unimportable → 0, never raises.
        import sys

        def broken_open(*args, **kwargs):
            raise OSError("no proc")

        monkeypatch.setitem(sys.modules, "psutil", None)
        monkeypatch.setattr("builtins.open", broken_open)
        assert mw._sample_rss_kib() == 0


# ---------------------------------------------------------------------------
# _fire_warn dump file
# ---------------------------------------------------------------------------


class TestFireWarn:
    def test_dump_written_with_header_and_stacks(self, tmp_path, caplog):
        dump = tmp_path / "watchdog" / "dump.log"
        with caplog.at_level(logging.WARNING, logger="agent.memory_watchdog"):
            mw._fire_warn(123_456, dump)
        assert dump.exists()
        content = dump.read_text(encoding="utf-8")
        assert "memory-watchdog warn" in content
        assert "rss_kib=123456" in content
        assert "Current thread" in content  # faulthandler stacks present
        assert any("memory-watchdog" in r.message for r in caplog.records)

    def test_dump_failure_never_raises(self, tmp_path, monkeypatch):
        # Unwritable dump location: _fire_warn must swallow and continue.
        dump = tmp_path / "nope" / "dump.log"
        monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        mw._fire_warn(1, dump)  # must not raise


# ---------------------------------------------------------------------------
# Exit code
# ---------------------------------------------------------------------------


def test_exit_code_distinctive():
    assert mw.MEMORY_WATCHDOG_EXIT_CODE == 42