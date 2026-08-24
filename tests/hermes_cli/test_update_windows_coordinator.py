"""Windows external rollout coordinator handoff contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import hermes_cli.update_coordinator as coordinator


def _project_venv(project: Path) -> Path:
    venv = project / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"python-launcher")
    (venv / "Lib" / "site-packages" / "demo").mkdir(parents=True)
    (venv / "Lib" / "site-packages" / "demo" / "__init__.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (venv / "pyvenv.cfg").write_text("home = C:\\Python\n", encoding="utf-8")
    return venv


def test_venv_python_layout_is_pure_platform_policy(tmp_path: Path):
    venv = tmp_path / "venv"

    assert coordinator._venv_python_for_platform(
        venv, windows=True
    ) == venv / "Scripts" / "python.exe"
    assert coordinator._venv_python_for_platform(
        venv, windows=False
    ) == venv / "bin" / "python"


@pytest.mark.parametrize(
    (
        "platform",
        "coordinator_child",
        "explicit_rollback",
        "rollout_enabled",
        "expected",
    ),
    [
        ("win32", False, True, False, True),
        ("win32", False, False, True, True),
        ("win32", False, False, False, False),
        ("win32", True, True, True, False),
        ("linux", False, True, True, False),
    ],
)
def test_external_coordinator_policy_is_host_independent(
    platform: str,
    coordinator_child: bool,
    explicit_rollback: bool,
    rollout_enabled: bool,
    expected: bool,
):
    assert (
        coordinator._coordinator_policy_applies(
            platform=platform,
            coordinator_child=coordinator_child,
            explicit_rollback=explicit_rollback,
            rollout_enabled=rollout_enabled,
        )
        is expected
    )


def test_windows_coordinator_spawn_policy_requires_job_breakaway():
    flags = 0x01000000 | 0x08000000 | 0x00000200

    assert coordinator._coordinator_detach_popen_kwargs(
        "win32", windows_flags=flags
    ) == {"creationflags": flags}
    assert coordinator._coordinator_detach_popen_kwargs(
        "linux", windows_flags=0
    ) == {}
    with pytest.raises(
        coordinator.CoordinatorHandoffError,
        match="cannot prove Windows job breakaway",
    ):
        coordinator._coordinator_detach_popen_kwargs(
            "win32", windows_flags=0x08000000 | 0x00000200
        )


def test_verified_snapshot_hashes_before_copy_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import hermes_cli.update_rollout as rollout

    project = tmp_path / "repo"
    project.mkdir()
    live_venv = _project_venv(project)
    state_paths: list[Path] = []
    real_dependency_state = rollout._dependency_state

    def tracked_dependency_state(path: Path):
        state_paths.append(Path(path).resolve())
        return real_dependency_state(path)

    monkeypatch.setattr(rollout, "_dependency_state", tracked_dependency_state)

    snapshot, copied_venv = coordinator._create_verified_snapshot(project)

    assert state_paths == [
        live_venv.resolve(),
        copied_venv.resolve(),
        live_venv.resolve(),
    ]
    assert copied_venv.parent == snapshot
    assert copied_venv.readlink() if copied_venv.is_symlink() else copied_venv.is_dir()
    assert (copied_venv / "Scripts" / "python.exe").read_bytes() == b"python-launcher"
    assert not coordinator._under(snapshot, live_venv)


def test_unresolved_marker_owner_cannot_authorize_coordinator_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import hermes_cli.update_lock as update_lock

    monkeypatch.setattr(
        update_lock,
        "read_live_update",
        lambda **kwargs: SimpleNamespace(
            pid=None,
            unavailable_reason="marker is malformed",
        ),
    )
    monkeypatch.setattr(update_lock, "_handoff_pid", lambda: None)
    monkeypatch.setattr(
        update_lock,
        "_is_ancestor_pid",
        lambda pid: pytest.fail("an unresolved PID must not reach ancestry probing"),
    )

    with pytest.raises(
        coordinator.CoordinatorHandoffError,
        match="owner is unavailable.*marker is malformed",
    ):
        coordinator._authorized_marker_owner(
            SimpleNamespace(path=tmp_path / "marker", acquired=False)
        )


def test_parent_handoff_preserves_exact_argv_and_detached_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "repo"
    project.mkdir()
    snapshot = tmp_path / ".repo-update-coordinators" / ("a" * 32)
    copied_venv = snapshot / "venv"
    (copied_venv / "Scripts").mkdir(parents=True)
    interpreter = copied_venv / "Scripts" / "python.exe"
    interpreter.write_bytes(b"python")
    marker = tmp_path / "marker"
    lock = SimpleNamespace(path=marker, acquired=True)
    calls: list[object] = []
    launched: dict[str, Any] = {}
    import hermes_constants

    home = tmp_path / "home"
    home.mkdir()
    correlation_id = "12345678-1234-5678-9234-567812345678"
    tauri_ready = home / f".update_coordinator_ready.{correlation_id}"

    class FakeChild:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        launched.update(argv=list(argv), **kwargs)
        return FakeChild()

    monkeypatch.setattr(
        coordinator, "_rollout_needs_external_coordinator", lambda args, root: True
    )
    monkeypatch.setattr(
        coordinator,
        "_authorized_marker_owner",
        lambda update_lock: calls.append("owner") or 314,
    )
    monkeypatch.setattr(
        coordinator,
        "_create_verified_snapshot",
        lambda root: (snapshot, copied_venv),
    )
    monkeypatch.setattr(
        coordinator,
        "_verify_copied_interpreter",
        lambda executable, **kwargs: calls.append(("verify", executable, kwargs)),
    )
    monkeypatch.setattr(coordinator, "_venv_python", lambda venv: interpreter)
    monkeypatch.setattr(
        coordinator,
        "_wait_for_coordinator_ready",
        lambda child, **kwargs: calls.append(("ready", child.pid, kwargs)),
    )
    monkeypatch.setattr(coordinator.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(coordinator.os, "getpid", lambda: 2718)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    monkeypatch.setenv("HERMES_UPDATE_WINDOWS_DETACHED", "stale-request")
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", correlation_id)
    monkeypatch.setenv(coordinator.TAURI_READY_ENV, str(tauri_ready))
    monkeypatch.setenv("PYTHONHOME", "unsafe-home")
    monkeypatch.setenv("PYTHONPATH", "unsafe-path")

    result = coordinator.handoff_windows_rollout_coordinator(
        SimpleNamespace(rollback=None),
        update_lock=lock,
        gateway_mode=True,
        project_root=project,
        argv=["update", "--branch=next", "--gateway"],
    )

    assert result == 75
    assert launched["argv"] == [
        str(interpreter),
        "-I",
        "-m",
        "hermes_cli.main",
        "update",
        "--branch=next",
        "--gateway",
    ]
    assert launched["cwd"] == str(project.resolve())
    assert launched["close_fds"] is True
    durable_stdout = launched["stdout"]
    assert Path(durable_stdout.name) == home.resolve() / "logs" / "update.log"
    assert durable_stdout.closed is True
    assert launched["stderr"] == subprocess.STDOUT
    env = cast(dict[str, str], launched["env"])
    assert env["HERMES_UPDATE_COORDINATOR_TAKEOVER_PID"] == "314"
    assert env["HERMES_UPDATE_HANDOFF_PID"] == "314"
    assert env[coordinator.COORDINATOR_PARENT_PID_ENV] == "2718"
    assert env[coordinator.COORDINATOR_SNAPSHOT_ENV] == str(snapshot)
    assert env["HERMES_UPDATE_WINDOWS_DETACHED"] == correlation_id
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert calls[0] == "owner"
    assert calls[1][0] == "verify"
    assert calls[2] == "owner"
    assert calls[3][0] == "ready"
    assert json.loads(tauri_ready.read_text(encoding="utf-8")) == {
        "correlation_id": correlation_id,
        "pid": 4242,
    }


def test_tauri_coordinator_durable_output_outlives_parent_pipe_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The child writes after the spawning scope has closed its log handle.

    Rust may close the original parent's anonymous pipe readers after its
    20-second drain.  Duration is irrelevant once the coordinator owns an
    independently duplicated file handle, so this uses a short delay while
    exercising the real subprocess/descriptor boundary.
    """

    import hermes_constants

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    monkeypatch.setenv(coordinator.TAURI_READY_ENV, str(home / "ready.json"))

    child = coordinator._spawn_coordinator_process(
        [
            sys.executable,
            "-I",
            "-c",
            "import time; time.sleep(0.2); "
            "print('coordinator-survived-parent-pipe-drain', flush=True)",
        ],
        project=project,
        env=dict(os.environ),
    )
    try:
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    assert (
        "coordinator-survived-parent-pipe-drain"
        in (home / "logs" / "update.log").read_text(encoding="utf-8")
    )


def test_failed_handshake_stops_child_before_snapshot_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "repo"
    project.mkdir()
    snapshot = tmp_path / ".repo-update-coordinators" / ("b" * 32)
    copied_venv = snapshot / "venv"
    (copied_venv / "Scripts").mkdir(parents=True)
    (copied_venv / "Scripts" / "python.exe").write_bytes(b"python")
    events: list[str] = []

    class FakeChild:
        pid = 5252
        return_code = None

        def poll(self):
            return self.return_code

        def terminate(self):
            events.append("terminate")
            self.return_code = 1

        def wait(self, timeout=None):
            events.append("wait")
            return self.return_code

        def kill(self):
            events.append("kill")
            self.return_code = -9

    monkeypatch.setattr(
        coordinator, "_rollout_needs_external_coordinator", lambda args, root: True
    )
    monkeypatch.setattr(coordinator, "_authorized_marker_owner", lambda lock: 123)
    monkeypatch.setattr(
        coordinator,
        "_create_verified_snapshot",
        lambda root: (snapshot, copied_venv),
    )
    monkeypatch.setattr(
        coordinator,
        "_venv_python",
        lambda venv: copied_venv / "Scripts" / "python.exe",
    )
    monkeypatch.setattr(coordinator, "_verify_copied_interpreter", lambda *a, **k: None)
    monkeypatch.setattr(coordinator.subprocess, "Popen", lambda *a, **k: FakeChild())
    monkeypatch.setattr(
        coordinator,
        "_wait_for_coordinator_ready",
        lambda *a, **k: (_ for _ in ()).throw(
            coordinator.CoordinatorHandoffError("no acknowledgement")
        ),
    )
    with pytest.raises(coordinator.CoordinatorHandoffError, match="acknowledgement"):
        coordinator.handoff_windows_rollout_coordinator(
            SimpleNamespace(rollback="checkpoint"),
            update_lock=SimpleNamespace(path=tmp_path / "marker", acquired=True),
            gateway_mode=False,
            project_root=project,
            argv=["update", "--rollback", "checkpoint"],
        )

    assert events == ["terminate", "wait"]
    assert not snapshot.exists()


@pytest.mark.windows_only
def test_child_takes_over_then_waits_before_returning_for_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "repo"
    project.mkdir()
    snapshot = tmp_path / ".repo-update-coordinators" / ("c" * 32)
    copied_venv = snapshot / "venv"
    interpreter = copied_venv / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python")
    token = "d" * 32
    events: list[str] = []

    class FakeLock:
        def take_over_handoff(self):
            events.append("takeover")
            return True

    real_atomic_write = coordinator._atomic_write_json

    def tracked_write(path: Path, payload: dict):
        events.append("ready" if path.name == "ready.json" else "owner")
        real_atomic_write(path, payload)

    monkeypatch.setattr(coordinator.sys, "prefix", str(copied_venv))
    monkeypatch.setattr(coordinator.sys, "executable", str(interpreter))
    monkeypatch.setenv(coordinator.COORDINATOR_SNAPSHOT_ENV, str(snapshot))
    monkeypatch.setenv(coordinator.COORDINATOR_PARENT_PID_ENV, "1234")
    monkeypatch.setenv(coordinator.COORDINATOR_TOKEN_ENV, token)
    monkeypatch.setattr(
        "hermes_cli.update_rollout.validate_rollout_coordinator",
        lambda root: events.append("validate"),
    )
    monkeypatch.setattr(
        coordinator,
        "_open_parent_wait_handle",
        lambda pid: events.append("open") or 88,
    )
    monkeypatch.setattr(
        coordinator,
        "_wait_parent_handle",
        lambda handle: events.append("wait"),
    )
    monkeypatch.setattr(
        coordinator,
        "_close_process_handle",
        lambda handle: events.append("close"),
    )
    monkeypatch.setattr(
        coordinator,
        "_write_owner",
        lambda root, pid: events.append("owner"),
    )
    monkeypatch.setattr(coordinator, "_atomic_write_json", tracked_write)

    assert (
        coordinator.acquire_windows_coordinator_takeover(
            FakeLock(), project_root=project
        )
        is True
    )

    assert events == [
        "validate",
        "open",
        "takeover",
        "owner",
        "ready",
        "wait",
        "close",
    ]
    payload = json.loads((snapshot / "ready.json").read_text(encoding="utf-8"))
    assert payload == {
        "parent_pid": 1234,
        "pid": os.getpid(),
        "token": token,
    }


def test_main_handoff_occurs_after_lock_and_before_update_mutation(
    monkeypatch: pytest.MonkeyPatch,
):
    import hermes_cli.config as cli_config
    import hermes_cli.main as cli_main
    import hermes_cli.update_lock as update_lock

    events: list[str] = []

    class FakeLock:
        holder = None
        path = Path("marker")
        acquired = True

        def acquire(self):
            events.append("acquire")
            return True

        def release(self):
            events.append("release")

    monkeypatch.setattr(cli_config, "is_managed", lambda: False)
    monkeypatch.setattr(cli_config, "detect_install_method", lambda root=None: "git")
    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(cli_main, "_install_hangup_protection", lambda gateway_mode: {})
    monkeypatch.setattr(cli_main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(coordinator, "is_windows_coordinator_child", lambda: False)
    monkeypatch.setattr(
        coordinator,
        "handoff_windows_rollout_coordinator",
        lambda *args, **kwargs: events.append("handoff") or 0,
    )
    monkeypatch.setattr(
        cli_main,
        "_cmd_update_impl",
        lambda *args, **kwargs: events.append("mutate"),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.cmd_update(
            SimpleNamespace(
                rollback="checkpoint",
                plan=False,
                check=False,
                gateway=False,
            )
        )

    assert exc_info.value.code == 0
    assert events == ["acquire", "handoff", "release"]


def test_main_child_wait_completes_before_update_mutation(
    monkeypatch: pytest.MonkeyPatch,
):
    import hermes_cli.config as cli_config
    import hermes_cli.main as cli_main
    import hermes_cli.update_lock as update_lock

    events: list[str] = []

    class FakeLock:
        holder = None

        def release(self):
            events.append("release")

    monkeypatch.setattr(cli_config, "is_managed", lambda: False)
    monkeypatch.setattr(cli_config, "detect_install_method", lambda root=None: "git")
    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(cli_main, "_install_hangup_protection", lambda gateway_mode: {})
    monkeypatch.setattr(cli_main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(coordinator, "is_windows_coordinator_child", lambda: True)
    monkeypatch.setattr(
        coordinator,
        "acquire_windows_coordinator_takeover",
        lambda lock, **kwargs: events.append("takeover-and-parent-wait") or True,
    )
    monkeypatch.setattr(
        coordinator,
        "handoff_windows_rollout_coordinator",
        lambda *args, **kwargs: pytest.fail("child must not recursively hand off"),
    )
    monkeypatch.setattr(
        coordinator,
        "schedule_windows_coordinator_cleanup",
        lambda root: events.append("cleanup") or True,
    )
    monkeypatch.setattr(
        cli_main,
        "_cmd_update_impl",
        lambda *args, **kwargs: events.append("mutate"),
    )

    cli_main.cmd_update(
        SimpleNamespace(
            rollback="checkpoint",
            plan=False,
            check=False,
            gateway=False,
        )
    )

    assert events == [
        "takeover-and-parent-wait",
        "mutate",
        "release",
        "cleanup",
    ]


@pytest.mark.windows_only
def test_cleanup_uses_external_base_python_and_waits_for_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "repo"
    project.mkdir()
    snapshot = tmp_path / ".repo-update-coordinators" / ("e" * 32)
    (snapshot / "venv").mkdir(parents=True)
    base_python = tmp_path / "base-python.exe"
    base_python.write_bytes(b"python")
    launched: dict[str, Any] = {}

    monkeypatch.setattr(coordinator.sys, "_base_executable", str(base_python))
    monkeypatch.setenv(coordinator.COORDINATOR_SNAPSHOT_ENV, str(snapshot))
    monkeypatch.setattr(
        coordinator.subprocess, "CREATE_NEW_PROCESS_GROUP", 1, raising=False
    )
    monkeypatch.setattr(coordinator.subprocess, "DETACHED_PROCESS", 2, raising=False)
    monkeypatch.setattr(coordinator.subprocess, "CREATE_NO_WINDOW", 4, raising=False)
    monkeypatch.setattr(
        coordinator.subprocess,
        "Popen",
        lambda argv, **kwargs: (
            launched.update(argv=list(argv), **kwargs) or SimpleNamespace()
        ),
    )

    assert coordinator.schedule_windows_coordinator_cleanup(project) is True

    assert launched["argv"][:3] == [str(base_python), "-I", "-c"]
    assert launched["argv"][-2:] == [str(snapshot), str(os.getpid())]
    assert launched["creationflags"] == 7
    assert launched["cwd"] == str(project.parent)
    env = cast(dict[str, str], launched["env"])
    assert coordinator.COORDINATOR_SNAPSHOT_ENV not in env


@pytest.mark.windows_only
def test_live_win32_handle_waits_for_exact_parent_process():
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        close_fds=True,
    )
    handle = 0
    try:
        handle = coordinator._open_parent_wait_handle(child.pid)
        child.terminate()
        child.wait(timeout=10)
        coordinator._wait_parent_handle(handle)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if handle:
            coordinator._close_process_handle(handle)


def test_external_child_publishes_correlation_bound_tauri_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "home"
    home.mkdir()
    correlation_id = "12345678-1234-5678-9234-567812345678"
    outcome = home / f".update_exit_code.{correlation_id}"
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setenv(
        coordinator.COORDINATOR_SNAPSHOT_ENV,
        str(tmp_path / "snapshot"),
    )
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", correlation_id)
    monkeypatch.setenv("HERMES_UPDATE_TAURI_OUTCOME_PATH", str(outcome))

    update_cmd._write_gateway_update_status(3)

    assert not (home / ".update_exit_code").exists()
    assert outcome.read_text(encoding="utf-8") == "3"

    update_cmd._write_tauri_coordinator_outcome(3)

    assert outcome.read_text(encoding="utf-8") == "3"

    outside = tmp_path / "outside"
    monkeypatch.setenv("HERMES_UPDATE_TAURI_OUTCOME_PATH", str(outside))
    update_cmd._write_tauri_coordinator_outcome(4)
    assert not outside.exists()


@pytest.mark.linux_only
def test_external_child_accepts_exact_tauri_outcome_through_symlinked_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import hermes_cli.update_cmd as update_cmd

    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    correlation_id = "12345678-1234-5678-9234-567812345678"
    linked_outcome = linked_home / f".update_exit_code.{correlation_id}"
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: linked_home)
    monkeypatch.setenv(
        coordinator.COORDINATOR_SNAPSHOT_ENV,
        str(tmp_path / "snapshot"),
    )
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", correlation_id)
    monkeypatch.setenv("HERMES_UPDATE_TAURI_OUTCOME_PATH", str(linked_outcome))

    update_cmd._write_tauri_coordinator_outcome(0)

    assert (
        real_home / f".update_exit_code.{correlation_id}"
    ).read_text(encoding="utf-8") == "0"
