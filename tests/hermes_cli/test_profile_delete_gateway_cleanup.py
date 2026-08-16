"""Regression tests for gateway cleanup during profile deletion (#87761)."""

import json
import os
from pathlib import Path

import pytest

from hermes_cli import profiles
from hermes_cli.profiles import create_profile


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Isolate profile paths from the user's real Hermes installation."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


@pytest.mark.windows_only
def test_windows_service_cleanup_uses_profile_scoped_uninstall(
    profile_env, monkeypatch
):
    """Windows removes both task and Startup persistence for the profile."""
    from hermes_cli import gateway_windows

    profile_dir = create_profile("coder", no_alias=True)
    default_home = os.environ["HERMES_HOME"]
    homes_seen = []

    monkeypatch.setattr(
        gateway_windows,
        "uninstall",
        lambda: homes_seen.append(os.environ.get("HERMES_HOME")),
    )

    profiles._cleanup_gateway_service("coder", profile_dir)

    assert homes_seen == [str(profile_dir)]
    assert os.environ["HERMES_HOME"] == default_home


def test_stop_gateway_uses_lock_fallback_when_pid_file_is_missing(
    tmp_path, monkeypatch
):
    """The authoritative resolver can recover a PID from gateway.lock."""
    import gateway.status as gateway_status

    pid_file = tmp_path / "gateway.pid"
    lock_file = tmp_path / "gateway.lock"
    lock_file.write_text(
        json.dumps(
            {
                "pid": 4321,
                "kind": "hermes-gateway",
                "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
                "start_time": 77,
            }
        ),
        encoding="utf-8",
    )
    calls = []
    liveness = iter((True, False))

    monkeypatch.setattr(
        gateway_status, "is_gateway_runtime_lock_active", lambda _path: True
    )
    monkeypatch.setattr(gateway_status, "_pid_exists", lambda _pid: next(liveness))
    monkeypatch.setattr(gateway_status, "_get_process_start_time", lambda _pid: 77)
    monkeypatch.setattr(
        gateway_status,
        "_read_process_cmdline",
        lambda _pid: "python -m hermes_cli.main gateway run",
    )
    monkeypatch.setattr(
        gateway_status,
        "terminate_pid",
        lambda pid, force=False: calls.append(("terminate", pid, force)),
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    profiles._stop_gateway_process(tmp_path)

    assert not pid_file.exists()
    assert calls == [("terminate", 4321, False)]
