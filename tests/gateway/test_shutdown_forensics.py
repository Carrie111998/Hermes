"""Tests for gateway.shutdown_forensics — fast snapshot + async diag spawn."""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import psutil

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
            '{"target_pid": 1234, "secret": "opaque-marker-secret"}',
            encoding="utf-8",
        )
        ctx = sf.snapshot_shutdown_context(
            signal.SIGTERM,
            planned_takeover=True,
            planned_stop=False,
        )
        serialized = sf.context_as_json(ctx)
        formatted = sf.format_context_for_log(ctx)
        assert ctx["takeover_marker_present"] is True
        assert ctx["planned_takeover"] is True
        assert "takeover_marker" not in ctx
        assert "opaque-marker-secret" not in serialized
        assert "opaque-marker-secret" not in formatted


class TestCommandRedaction:
    def test_proc_cmdline_never_returns_argument_contents(self):
        raw = b"/usr/bin/python3\x00--api-key\x00super-secret-value\x00--safe\x00ok\x00"

        summary = sf._summarize_cmdline_bytes(raw)

        assert summary == "python3 [4 arguments redacted]"
        assert "super-secret-value" not in summary
        assert "api-key" not in summary


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
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only diagnostic")
    def test_timeout_wrapper_force_redacts_captured_output(self):
        secret = "opaque-shutdown-secret-value"
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                sf._diagnostic_timeout_wrapper_source(),
                f"printf 'API_TOKEN={secret}\\n'",
                "1.0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TZ": "UTC",
            },
            check=False,
            timeout=10,
        )
        output = proc.stdout.decode("utf-8", errors="replace")
        assert proc.returncode == 0, output
        assert secret not in output
        assert "***" in output or "redacted" in output.lower()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only diagnostic")
    def test_timeout_wrapper_kills_stubborn_member_after_shell_leader_exits(
        self, tmp_path
    ):
        handoff = tmp_path / "stubborn.pid"
        child_code = (
            "import os,signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(handoff)!r}).write_text(str(os.getpid())); "
            "time.sleep(60)"
        )
        script = (
            f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)} & exit 0"
        )

        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                sf._diagnostic_timeout_wrapper_source(),
                script,
                "0.2",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TZ": "UTC",
            },
        )
        child_pid = None
        try:
            output, _ = proc.communicate(timeout=10)
            assert proc.returncode == 0, output.decode("utf-8", errors="replace")
            assert handoff.exists()
            child_pid = int(handoff.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    child = psutil.Process(child_pid)
                    if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                        break
                except psutil.NoSuchProcess:
                    break
                time.sleep(0.05)
            else:
                pytest.fail(f"TERM-ignoring diagnostic descendant survived: {child_pid}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if child_pid is not None:
                try:
                    child = psutil.Process(child_pid)
                    if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                        child.kill()
                except psutil.NoSuchProcess:
                    pass

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only diagnostic")
    def test_uses_minimal_environment_and_never_collects_full_argv(
        self, tmp_path, monkeypatch
    ):
        captured = {}

        class FakeProcess:
            pid = 43210

        def fake_popen(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProcess()

        monkeypatch.setenv("UNRELATED_AMBIENT_SENTINEL", "must-not-copy")
        monkeypatch.setattr(sf.subprocess, "Popen", fake_popen)

        assert sf.spawn_async_diagnostic(tmp_path / "diag.log", "SIGTERM") == 43210
        child_env = captured["kwargs"]["env"]
        assert "UNRELATED_AMBIENT_SENTINEL" not in child_env
        assert "HOME" not in child_env
        assert set(child_env) == {
            "PATH",
            "LANG",
            "LC_ALL",
            "TZ",
            "HERMES_DIAGNOSTIC_SIGNAL",
        }
        argv = captured["args"][0]
        diagnostic_script = argv[3]
        assert "command=" not in diagnostic_script
        assert "ps aux" not in diagnostic_script
        assert "pstree -plau" not in diagnostic_script

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only diagnostic")
    def test_existing_log_is_forced_private(self, tmp_path):
        log_path = tmp_path / "diag.log"
        log_path.write_text("existing\n", encoding="utf-8")
        log_path.chmod(0o644)

        pid = sf.spawn_async_diagnostic(log_path, "SIGTERM", timeout_seconds=3.0)
        assert pid is not None
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass

        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only diagnostic")
    def test_new_log_is_created_private(self, tmp_path):
        log_path = tmp_path / "diag.log"

        pid = sf.spawn_async_diagnostic(log_path, "SIGTERM", timeout_seconds=3.0)
        assert pid is not None
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass

        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

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
