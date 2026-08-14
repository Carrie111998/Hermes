from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest


def test_scope_command_pins_identity_and_uses_systemd_scope(monkeypatch):
    from hermes_cli import worker_scope
    from hermes_cli.worker_scope import build_scoped_worker_command

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name in {"systemd-run", "systemctl"} else None)
    def fake_run(argv, **_kwargs):
        if argv[0].endswith("systemctl"):
            return SimpleNamespace(returncode=0, stdout="running\n")
        unit = next(part.split("=", 1)[1] for part in argv if part.startswith("--unit="))
        return SimpleNamespace(
            returncode=0,
            stdout=f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}.scope\n",
        )

    monkeypatch.setattr(worker_scope.subprocess, "run", fake_run)
    env = {
        "HERMES_PROFILE": "researcher",
        "HERMES_KANBAN_TASK": "t_12345678",
        "HERMES_KANBAN_RUN_ID": "91",
        "HERMES_KANBAN_CLAIM_LOCK": "host:pid",
        "HERMES_KANBAN_BOARD": "default",
        "HERMES_HOME": "/profiles/researcher",
        "TERMINAL_CWD": "/workspace",
        "HERMES_KANBAN_MODEL": "model-x",
        "HERMES_KANBAN_PROVIDER": "provider-y",
        "HERMES_KANBAN_TOOLSETS": "terminal,file",
    }
    cmd = build_scoped_worker_command(["hermes", "chat"], env=env, require_isolation=True)
    assert cmd[:3] == ["/usr/bin/systemd-run", "--user", "--scope"]
    assert any(part == "--unit=hermes-kanban-t_12345678-r91" for part in cmd)
    for key in env:
        assert any(part == f"--setenv={key}={env[key]}" for part in cmd)


def test_degraded_manager_with_working_transient_scope_is_accepted(monkeypatch):
    from hermes_cli import worker_scope
    from hermes_cli.worker_scope import build_scoped_worker_command

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"systemd-run", "systemctl"} else None,
    )

    def fake_run(argv, **_kwargs):
        if argv[0].endswith("systemctl"):
            return SimpleNamespace(returncode=1, stdout="degraded\n")
        assert "--wait" not in argv
        assert "--pipe" not in argv
        unit = next(part.split("=", 1)[1] for part in argv if part.startswith("--unit="))
        return SimpleNamespace(
            returncode=0,
            stdout=f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}.scope\n",
        )

    monkeypatch.setattr(worker_scope.subprocess, "run", fake_run)
    command = build_scoped_worker_command(
        ["hermes", "chat"],
        env={"HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_RUN_ID": "7"},
        require_isolation=True,
    )
    assert command[:3] == ["/usr/bin/systemd-run", "--user", "--scope"]


def test_running_manager_requires_working_transient_scope(monkeypatch):
    from hermes_cli import worker_scope
    from hermes_cli.worker_scope import WorkerIsolationError, build_scoped_worker_command

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"systemd-run", "systemctl"} else None,
    )

    def fake_run(argv, **_kwargs):
        if argv[0].endswith("systemctl"):
            return SimpleNamespace(returncode=0, stdout="running\n")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(worker_scope.subprocess, "run", fake_run)
    with pytest.raises(WorkerIsolationError):
        build_scoped_worker_command(["hermes"], env={}, require_isolation=True)


def test_inaccessible_user_bus_fails_closed(monkeypatch):
    from hermes_cli import worker_scope
    from hermes_cli.worker_scope import WorkerIsolationError, build_scoped_worker_command

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"systemd-run", "systemctl"} else None,
    )

    def inaccessible(*_args, **_kwargs):
        raise worker_scope.subprocess.TimeoutExpired("systemctl", 5)

    monkeypatch.setattr(worker_scope.subprocess, "run", inaccessible)
    with pytest.raises(WorkerIsolationError):
        build_scoped_worker_command(["hermes"], env={}, require_isolation=True)


@pytest.mark.parametrize("state", ["offline", "stopping", "maintenance", "initializing", "starting", "unknown", ""])
def test_non_operational_manager_states_fail_closed(monkeypatch, state):
    from hermes_cli import worker_scope
    from hermes_cli.worker_scope import WorkerIsolationError, build_scoped_worker_command

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"systemd-run", "systemctl"} else None,
    )
    monkeypatch.setattr(
        worker_scope.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=f"{state}\n"),
    )
    with pytest.raises(WorkerIsolationError):
        build_scoped_worker_command(["hermes"], env={}, require_isolation=True)


@pytest.mark.parametrize(
    ("probe_returncode", "probe_cgroup"),
    [
        (1, ""),
        (0, "0::/user.slice/user@1000.service/app.slice/hermes-gateway-athena.service\n"),
        (0, "0::/user.slice/user@1000.service/app.slice/hermes-kanban-dispatcher.service\n"),
        (0, "0::/user.slice/user@1000.service/app.slice/unrelated.scope\n"),
    ],
)
def test_degraded_manager_rejects_failed_or_wrong_transient_scope(
    monkeypatch, probe_returncode, probe_cgroup
):
    from hermes_cli import worker_scope
    from hermes_cli.worker_scope import WorkerIsolationError, build_scoped_worker_command

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"systemd-run", "systemctl"} else None,
    )

    def fake_run(argv, **_kwargs):
        if argv[0].endswith("systemctl"):
            return SimpleNamespace(returncode=1, stdout="degraded\n")
        return SimpleNamespace(returncode=probe_returncode, stdout=probe_cgroup)

    monkeypatch.setattr(worker_scope.subprocess, "run", fake_run)
    with pytest.raises(WorkerIsolationError):
        build_scoped_worker_command(["hermes"], env={}, require_isolation=True)


def test_required_scope_fails_closed_when_backend_missing(monkeypatch):
    from hermes_cli.worker_scope import WorkerIsolationError, build_scoped_worker_command

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(WorkerIsolationError):
        build_scoped_worker_command(["hermes"], env={"HERMES_KANBAN_TASK": "t_1"}, require_isolation=True)


def test_bounded_fallback_is_explicit_when_isolation_optional(monkeypatch):
    from hermes_cli.worker_scope import build_scoped_worker_command

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert build_scoped_worker_command(["hermes"], env={}, require_isolation=False) == ["hermes"]
