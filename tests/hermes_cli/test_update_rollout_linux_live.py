"""Native Linux witnesses for Phase 4 gateway rollout topology."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_cli.gateway as gateway_cli
import gateway.status as gateway_status
from hermes_cli.update_inventory import RuntimeRecord
from hermes_cli.update_rollout import (
    RolloutConfig,
    quiesce_profile_gateway,
    restart_profile_gateway,
)


def _config() -> RolloutConfig:
    return RolloutConfig(
        enabled=True,
        canary_profile="canary",
        restart_timeout_seconds=5,
    )


def _wait_for_pid(path: Path, *, different_from: int | None = None) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            pid = int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            time.sleep(0.01)
            continue
        if pid > 0 and pid != different_from:
            return pid
        time.sleep(0.01)
    raise AssertionError(f"process did not publish a new PID to {path}")


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_manual_gateway_uses_real_sigterm_and_relaunches_in_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise kernel SIGTERM/absence/setsid with an isolated child.

    The canonical test container mounts the outer host's ``/proc`` while
    Python reports namespace-local PIDs.  Inject only that visibility seam;
    signal delivery, process death, the one-second absence proof, Popen, and
    session creation remain real Linux operations.
    """

    ready_path = tmp_path / "gateway.pid"
    probe = tmp_path / "gateway_probe.py"
    probe.write_text(
        """\
import os
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    argv = [
        sys.executable,
        str(probe),
        str(ready_path),
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
    ]
    original = subprocess.Popen(argv)
    relaunched_pid: int | None = None
    try:
        assert _wait_for_pid(ready_path) == original.pid

        def pid_alive(pid: int) -> bool:
            if pid == original.pid and original.poll() is not None:
                return False
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        class NamespaceProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def create_time(self) -> float:
                return 1.0

        monkeypatch.setattr(gateway_status, "_pid_exists", pid_alive)
        monkeypatch.setattr(
            gateway_status,
            "_looks_like_gateway_process",
            lambda pid: pid == original.pid,
        )
        monkeypatch.setattr("psutil.Process", NamespaceProcess)
        runtime = RuntimeRecord(
            kind="gateway",
            profile="default",
            pid=original.pid,
            supervisor="manual",
            restart_via="manual",
            detail={"argv": argv, "start_time": 1.0},
        )

        quiesced = quiesce_profile_gateway("default", runtime, config=_config())
        assert quiesced["quiesced"] is True
        assert quiesced["stopped_pids"] == [original.pid]
        assert original.wait(timeout=10) == 0

        restart_runtime = RuntimeRecord(
            kind="gateway",
            profile="default",
            pid=None,
            supervisor="manual",
            restart_via="manual",
            detail={"argv": argv},
        )
        restarted = restart_profile_gateway(
            "default", restart_runtime, config=_config()
        )
        assert restarted["relaunched_profiles"] == ["default"]
        assert restarted["old_pid"] is None
        relaunched_pid = _wait_for_pid(ready_path, different_from=original.pid)
        assert os.getsid(relaunched_pid) == relaunched_pid
    finally:
        if original.poll() is None:
            original.terminate()
            original.wait(timeout=10)
        if relaunched_pid is not None:
            try:
                os.kill(relaunched_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                os.waitpid(relaunched_pid, 0)


@pytest.mark.linux_only
def test_systemd_canary_routes_stop_and_restart_on_native_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove Linux selects systemd; service-manager calls stay hermetic."""

    actions: list[tuple[str, bool]] = []
    monkeypatch.setattr(gateway_cli, "get_installed_systemd_scopes", lambda: ["user"])
    monkeypatch.setattr(
        gateway_cli,
        "systemd_stop",
        lambda *, system: actions.append(("stop", system)),
    )
    monkeypatch.setattr(
        gateway_cli,
        "systemd_restart",
        lambda *, system: actions.append(("restart", system)),
    )
    runtime = RuntimeRecord(
        kind="gateway",
        profile="canary",
        pid=None,
        supervisor="systemd",
        restart_via="systemd",
    )

    quiesced = quiesce_profile_gateway("canary", runtime, config=_config())
    restarted = restart_profile_gateway("canary", runtime, config=_config())

    assert quiesced["quiesced"] is True
    assert restarted["restarted_services"] == ["hermes-gateway-canary.service"]
    assert actions == [("stop", False), ("restart", False)]
