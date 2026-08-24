"""Real-systemd witness for the Phase 4 canary stop/restart route.

The ordinary Linux suite proves the kernel/manual-process leg without mocks.
This file is opt-in because it writes one uniquely named transient unit under
``/etc/systemd/system``.  CI runs it as root on an Ubuntu host whose PID 1 is
systemd; local and container suites skip it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import psutil
import pytest

import hermes_cli.gateway as gateway_cli
import hermes_constants
from hermes_cli.update_inventory import RuntimeRecord
from hermes_cli.update_rollout import (
    RolloutConfig,
    quiesce_profile_gateway,
    restart_profile_gateway,
)


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )


def _wait_for_running_status(path: Path, *, previous_pid: int | None = None) -> int:
    deadline = time.monotonic() + 45
    last = "missing"
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0) or 0)
            last = json.dumps(payload, sort_keys=True)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
            last = str(exc)
            time.sleep(0.1)
            continue
        if (
            payload.get("gateway_state") == "running"
            and pid > 0
            and pid != previous_pid
            and psutil.pid_exists(pid)
        ):
            return pid
        time.sleep(0.1)
    raise AssertionError(f"gateway did not publish a running replacement: {last}")


def _service_main_pid(service: str) -> int:
    """Return systemd's authoritative MainPID for the disposable unit."""

    result = _systemctl("show", service, "--property=MainPID", "--value")
    return int(result.stdout.strip() or "0")


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_canary_quiesce_and_restart_use_real_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("HERMES_RUN_SYSTEMD_ROLLOUT_E2E") != "1":
        pytest.skip("dedicated real-systemd CI witness")
    assert os.geteuid() == 0, "real-systemd witness must run as root"
    assert shutil.which("systemctl"), "systemctl is required"
    assert Path("/run/systemd/system").is_dir(), "PID 1 is not systemd"

    service_base = f"hermes-phase4-e2e-{os.getpid()}"
    hermes_home = tmp_path / "home" / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None)
    monkeypatch.setattr(gateway_cli, "_SERVICE_BASE", service_base)
    # Keep every runtime file in tmp_path while still exercising the real
    # system-scope unit generator and systemctl calls. The service itself runs
    # as root only inside this disposable CI VM.
    monkeypatch.setattr(
        gateway_cli,
        "_system_service_identity",
        lambda _run_as_user=None: ("root", "root", str(tmp_path / "home")),
    )

    unit_path = gateway_cli.get_systemd_unit_path(system=True)
    status_path = hermes_home / "gateway_state.json"
    assert not unit_path.exists(), f"refusing to replace existing unit: {unit_path}"

    try:
        unit_path.write_text(
            gateway_cli.generate_systemd_unit(system=True, run_as_user="root"),
            encoding="utf-8",
        )
        _systemctl("daemon-reload")
        _systemctl("start", service_base)
        first_pid = _wait_for_running_status(status_path)
        assert first_pid == _service_main_pid(service_base)

        runtime = RuntimeRecord(
            kind="gateway",
            profile="default",
            pid=first_pid,
            supervisor="systemd",
            restart_via="systemd",
            detail={"start_time": psutil.Process(first_pid).create_time()},
        )
        config = RolloutConfig(
            enabled=True,
            canary_profile="default",
            restart_timeout_seconds=15,
        )

        quiesced = quiesce_profile_gateway("default", runtime, config=config)
        assert quiesced["quiesced"] is True
        assert (
            _systemctl("is-active", service_base, check=False).stdout.strip()
            == "inactive"
        )
        assert _service_main_pid(service_base) == 0
        assert not psutil.pid_exists(first_pid)

        restarted = restart_profile_gateway("default", runtime, config=config)
        second_pid = _wait_for_running_status(status_path, previous_pid=first_pid)

        assert restarted["restarted_services"] == ["hermes-gateway.service"]
        assert second_pid != first_pid
        assert second_pid == _service_main_pid(service_base)
        assert _systemctl("is-active", service_base).stdout.strip() == "active"
    finally:
        _systemctl("stop", service_base, check=False)
        _systemctl("disable", service_base, check=False)
        unit_path.unlink(missing_ok=True)
        _systemctl("daemon-reload", check=False)
