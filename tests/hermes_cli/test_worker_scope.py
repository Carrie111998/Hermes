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


def test_cgroup_parser_accepts_exact_scope_component_and_descendants():
    from hermes_cli.worker_scope import _cgroup_output_matches_scope

    unit = "hermes-kanban-isolation-probe-123-1"
    assert _cgroup_output_matches_scope(
        f"0::/user.slice/app.slice/{unit}.scope/child\n",
        unit,
    )
    assert _cgroup_output_matches_scope(
        f"7:cpu,cpuacct:/user.slice/{unit}.scope\n"
        f"8:name=systemd:/user.slice/{unit}.scope/child\n",
        unit,
    )


@pytest.mark.parametrize(
    "output_factory",
    [
        lambda unit: f"0::/user.slice/{unit}.scope-shadow\n",
        lambda unit: f"0::/user.slice/shadow-{unit}.scope\n",
        lambda unit: (
            f"0::/user.slice/{unit}.scope\n"
            "1:name=systemd:/user.slice/hermes-gateway-athena.service\n"
        ),
        lambda unit: (
            f"0::/user.slice/{unit}.scope\n"
            "1:name=systemd:/user.slice/hermes-kanban-dispatcher.service\n"
        ),
        lambda unit: (
            f"0::/user.slice/{unit}.scope\n"
            "1:name=systemd:/user.slice/unrelated.scope\n"
        ),
        lambda unit: (
            f"0::/user.slice/{unit}.scope\n"
            f"0::/user.slice/{unit}.scope\n"
        ),
    ],
)
def test_cgroup_parser_rejects_spoofed_duplicate_or_conflicting_records(output_factory):
    from hermes_cli.worker_scope import _cgroup_output_matches_scope

    unit = "hermes-kanban-isolation-probe-123-1"
    assert not _cgroup_output_matches_scope(output_factory(unit), unit)


def test_cgroup_parser_rejects_duplicate_exact_component_in_one_path():
    from hermes_cli.worker_scope import _cgroup_output_matches_scope

    unit = "hermes-kanban-isolation-probe-123-1"
    assert not _cgroup_output_matches_scope(
        f"0::/user.slice/{unit}.scope/child/{unit}.scope\n",
        unit,
    )


def test_scope_health_rejects_duplicate_exact_component_in_one_path(monkeypatch):
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
        unit = next(part.split("=", 1)[1] for part in argv if part.startswith("--unit="))
        return SimpleNamespace(
            returncode=0,
            stdout=f"0::/user.slice/{unit}.scope/child/{unit}.scope\n",
        )

    monkeypatch.setattr(worker_scope.subprocess, "run", fake_run)
    with pytest.raises(WorkerIsolationError):
        build_scoped_worker_command(["hermes"], env={}, require_isolation=True)


@pytest.mark.parametrize(
    "output_factory",
    [
        lambda unit: f"not-a-cgroup-record:/fake/{unit}.scope\n",
        lambda unit: f"0:/fake/{unit}.scope\n",
        lambda unit: f"0::extra:/fake/{unit}.scope\n",
        lambda unit: f"x::/fake/{unit}.scope\n",
        lambda unit: f"0:cpu:/fake/{unit}.scope\n",
        lambda unit: f"1::/fake/{unit}.scope\n",
        lambda unit: f"1:cpu,,memory:/fake/{unit}.scope\n",
        lambda unit: f"1:cpu=bad:/fake/{unit}.scope\n",
        lambda unit: f"1:name=:/fake/{unit}.scope\n",
        lambda unit: f"1:cpu:relative/{unit}.scope\n",
        lambda unit: f"0::/fake//{unit}.scope\n",
        lambda unit: f"0::/fake/../{unit}.scope\n",
        lambda unit: f"0::/fake/{unit}.scope\ninvalid\n",
        lambda unit: f"0::/fake/{unit}.scope\n\n",
    ],
)
def test_cgroup_parser_rejects_malformed_records(output_factory):
    from hermes_cli.worker_scope import _cgroup_output_matches_scope

    unit = "hermes-kanban-isolation-probe-123-1"
    assert not _cgroup_output_matches_scope(output_factory(unit), unit)


@pytest.mark.parametrize(
    "output_factory",
    [
        lambda unit: f"0::/fake/{unit}.scope /child\n",
        lambda unit: f"0::/fake/{unit}.scope\t\n",
        lambda unit: f"0::/fake/{unit}.scope\x00\n",
        lambda unit: f"0::/fake/{unit}.scope\u00a0\n",
        lambda unit: f"0::/fake/{unit}.scope\u2003\n",
        lambda unit: f"0::/fake/{unit}.scope\u2028",
        lambda unit: f"0::/fake/{unit}.scope\r\n",
        lambda unit: f"0::/fake/gateway_alias/{unit}.scope\n",
        lambda unit: f"0::/fake/HERMES.GATEWAY/{unit}.scope\n",
        lambda unit: f"0::/fake/dispatcherd/{unit}.scope\n",
        lambda unit: f"0::/fake/DISPATCHER_ALIAS/{unit}.scope\n",
    ],
)
def test_cgroup_parser_rejects_unicode_control_whitespace_and_aliases(output_factory):
    from hermes_cli.worker_scope import _cgroup_output_matches_scope

    unit = "hermes-kanban-isolation-probe-123-1"
    assert not _cgroup_output_matches_scope(output_factory(unit), unit)


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, "running", True),
        (0, "running\n", True),
        (0, "degraded", True),
        (1, "degraded\n", True),
        (0, " running\n", False),
        (0, "running \n", False),
        (0, "running\n\n", False),
        (0, "running\textra\n", False),
        (0, "running\u00a0\n", False),
        (0, "running\u2028", False),
        (0, "running\x00\n", False),
        (1, "running\n", False),
        (2, "degraded\n", False),
    ],
)
def test_manager_state_parser_requires_exact_unambiguous_output(returncode, stdout, expected):
    from hermes_cli.worker_scope import _manager_state_is_operational

    assert _manager_state_is_operational(stdout, returncode) is expected


def test_required_scope_fails_closed_when_backend_missing(monkeypatch):
    from hermes_cli.worker_scope import WorkerIsolationError, build_scoped_worker_command

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(WorkerIsolationError):
        build_scoped_worker_command(["hermes"], env={"HERMES_KANBAN_TASK": "t_1"}, require_isolation=True)


def test_bounded_fallback_is_explicit_when_isolation_optional(monkeypatch):
    from hermes_cli.worker_scope import build_scoped_worker_command

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert build_scoped_worker_command(["hermes"], env={}, require_isolation=False) == ["hermes"]
