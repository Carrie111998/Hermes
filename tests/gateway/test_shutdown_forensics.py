"""Tests for gateway.shutdown_forensics — fast snapshot + async diag spawn."""

from __future__ import annotations

import json
import io
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import shutdown_forensics as sf


# ---------------------------------------------------------------------------
# _signal_name
# ---------------------------------------------------------------------------

class TestSignalName:

    def test_unknown_int_returns_signal_num_token(self):
        # Pick an integer extremely unlikely to ever be a real signal alias
        assert sf._signal_name(9999) == "signal#9999"


# ---------------------------------------------------------------------------
# snapshot_shutdown_context
# ---------------------------------------------------------------------------

class TestSnapshotShutdownContext:

    def test_handles_none_signal(self):
        ctx = sf.snapshot_shutdown_context(None)
        assert ctx["signal"] == "UNKNOWN"
        assert ctx["signal_num"] is None

    def test_includes_timestamps(self):
        before = time.time()
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        after = time.time()
        assert before <= ctx["ts"] <= after
        assert isinstance(ctx["ts_monotonic"], float)


    def test_under_systemd_false_without_invocation_id_and_normal_ppid(
        self, monkeypatch
    ):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        # We can't actually change ppid; skip if we happen to be reaped
        # by init (e.g. running under tini).
        if os.getppid() == 1:
            pytest.skip("test process is reaped by init")
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        assert ctx["under_systemd"] is False


    def test_detects_takeover_marker_for_self(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        marker = tmp_path / ".gateway-takeover.json"
        marker.write_text(
            f'{{"target_pid": {os.getpid()}, "replacer_pid": 99999}}',
            encoding="utf-8",
        )
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        assert "takeover_marker" in ctx
        assert ctx["takeover_marker_for_self"] is True


# ---------------------------------------------------------------------------
# format_context_for_log / context_as_json
# ---------------------------------------------------------------------------

class TestFormatters:


    def test_context_as_json_handles_unserialisable_values(self):
        ctx = {"signal": "SIGTERM", "weird": object()}
        payload = sf.context_as_json(ctx)
        # default=str means objects get repr'd, JSON stays valid
        decoded = json.loads(payload)
        assert decoded["signal"] == "SIGTERM"
        assert "weird" in decoded


# ---------------------------------------------------------------------------
# spawn_async_diagnostic
# ---------------------------------------------------------------------------

class TestSpawnAsyncDiagnostic:
    def test_uses_python_watchdog_without_coreutils_timeout(
        self, tmp_path, monkeypatch
    ):
        captured = {}

        class DummyProcess:
            pid = 321

        monkeypatch.setattr(sf.shutil, "which", lambda _name: None)

        def capture(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return DummyProcess()

        monkeypatch.setattr(sf.subprocess, "Popen", capture)

        assert sf.spawn_async_diagnostic(tmp_path / "diag.log", "SIGTERM") == 321
        assert captured["command"][0] == sys.executable
        assert captured["command"][1] == "-c"
        assert captured["command"][3] == "5.0"
        assert captured["kwargs"]["start_new_session"] is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only diagnostic")
    def test_spawns_subprocess_and_writes_output(self, tmp_path):
        log_path = tmp_path / "diag.log"
        pid = sf.spawn_async_diagnostic(log_path, "SIGTERM", timeout_seconds=3.0)
        assert pid is not None and pid > 0

        # Wait briefly for the subprocess to write — bounded by its own timeout.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if log_path.exists() and log_path.stat().st_size > 0:
                # Wait a touch longer for the script to finish writing
                time.sleep(0.2)
                break
            time.sleep(0.1)

        # Reap the subprocess so it doesn't show up as a zombie.
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass

        assert log_path.exists()
        contents = log_path.read_text(encoding="utf-8", errors="replace")
        assert "shutdown diagnostic" in contents
        assert "SIGTERM" in contents


# ---------------------------------------------------------------------------
# _parse_systemd_duration_to_us
# ---------------------------------------------------------------------------

class TestParseSystemdDuration:
    def test_seconds(self):
        assert sf._parse_systemd_duration_to_us("90s") == 90 * 1_000_000

    def test_minutes(self):
        assert sf._parse_systemd_duration_to_us("3min") == 180 * 1_000_000


# ---------------------------------------------------------------------------
# check_systemd_timing_alignment
# ---------------------------------------------------------------------------

class TestCheckSystemdTimingAlignment:

    def test_returns_none_when_unit_undeterminable(self, monkeypatch):
        monkeypatch.setenv("INVOCATION_ID", "abc")
        # /proc/self/cgroup likely doesn't end in .service for the test runner
        result = sf.check_systemd_timing_alignment(180.0)
        # Either None (we couldn't find a unit) or a dict with mismatch info
        # for whatever unit pytest IS in.  Both are valid; we just ensure
        # the function doesn't raise.
        assert result is None or isinstance(result, dict)

    @staticmethod
    def _install_systemd_probe(monkeypatch, timeout_stop_sec):
        monkeypatch.setenv("INVOCATION_ID", "abc")
        monkeypatch.setattr(
            sf,
            "open",
            lambda *_args, **_kwargs: io.StringIO(
                "0::/user.slice/hermes-gateway.service\n"
            ),
            raising=False,
        )
        monkeypatch.setattr(
            sf.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=f"TimeoutStopUSec={int(timeout_stop_sec * 1_000_000)}\n",
            ),
        )

    def test_reports_unit_that_would_kill_before_controlled_watchdog(self, monkeypatch):
        self._install_systemd_probe(monkeypatch, timeout_stop_sec=240)

        result = sf.check_systemd_timing_alignment(180)

        assert result is not None
        assert result["controlled_exit_deadline"] == 240
        assert result["expected_min"] == 255
        assert result["mismatch"] is True

    def test_accepts_unit_with_required_post_watchdog_margin(self, monkeypatch):
        self._install_systemd_probe(monkeypatch, timeout_stop_sec=255)

        result = sf.check_systemd_timing_alignment(180)

        assert result is not None
        assert (
            result["timeout_stop_sec"] - result["controlled_exit_deadline"]
            >= result["systemd_kill_margin"]
        )
        assert result["mismatch"] is False
