"""Ownership-aware updater finalization regressions.

The gateway that launched ``hermes update --gateway`` may also own the
updater process through its systemd cgroup.  Restarting that unit before the
dashboard and result marker are finalized kills the updater mid-flight.
"""

from __future__ import annotations

import json
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli import update_cmd


def _owner() -> tuple[str, list[str], str]:
    return (
        "user",
        ["systemctl", "--user"],
        "hermes-gateway-coding_lead",
    )


def test_dashboard_and_durable_status_precede_terminal_owner_restart(monkeypatch):
    events: list[str] = []

    monkeypatch.setattr(
        update_cmd,
        "_finish_dashboard_update_cleanup",
        lambda _failures: events.append("dashboard") or True,
    )
    monkeypatch.setattr(
        update_cmd,
        "_restart_updater_owning_gateway",
        lambda _owner, *, final_exit_code, persist_result: events.append(
            f"owner:{final_exit_code}:{persist_result}"
        )
        or True,
    )

    ok = update_cmd._finish_update_service_finalization(
        [],
        gateway_mode=True,
        gateway_fleet_restart_incomplete=False,
        updater_owner_discovery_failed=False,
        deferred_owner=_owner(),
    )

    assert ok is True
    assert events == ["dashboard", "owner:0:True"]


def test_partial_failure_is_persisted_before_owner_restart(monkeypatch, capsys):
    events: list[str] = []

    monkeypatch.setattr(
        update_cmd,
        "_finish_dashboard_update_cleanup",
        lambda _failures: events.append("dashboard") or False,
    )
    monkeypatch.setattr(
        update_cmd,
        "_restart_updater_owning_gateway",
        lambda _owner, *, final_exit_code, persist_result: events.append(
            f"owner:{final_exit_code}:{persist_result}"
        )
        or True,
    )

    ok = update_cmd._finish_update_service_finalization(
        [],
        gateway_mode=True,
        gateway_fleet_restart_incomplete=False,
        updater_owner_discovery_failed=False,
        deferred_owner=_owner(),
    )

    assert ok is False
    assert events == ["dashboard", "owner:1:True"]
    assert "Update finalization incomplete" in capsys.readouterr().out


def test_owner_signal_failure_persists_error_result(monkeypatch, capsys):
    events: list[str] = []

    monkeypatch.setattr(
        update_cmd, "_finish_dashboard_update_cleanup", lambda _failures: True
    )
    monkeypatch.setattr(
        update_cmd,
        "_write_gateway_update_exit_code",
        lambda _required, code: events.append(f"status:{code}") or True,
    )
    monkeypatch.setattr(
        update_cmd,
        "_restart_updater_owning_gateway",
        lambda _owner, *, final_exit_code, persist_result: events.append(
            f"owner:{final_exit_code}:{persist_result}"
        )
        or False,
    )

    ok = update_cmd._finish_update_service_finalization(
        [],
        gateway_mode=True,
        gateway_fleet_restart_incomplete=False,
        updater_owner_discovery_failed=False,
        deferred_owner=_owner(),
    )

    assert ok is False
    assert events == ["owner:0:True", "status:1"]
    assert "Could not prepare or signal" in capsys.readouterr().out


def test_no_owner_persists_final_status_after_dashboard(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        update_cmd,
        "_finish_dashboard_update_cleanup",
        lambda _failures: events.append("dashboard") or True,
    )
    monkeypatch.setattr(
        update_cmd,
        "_write_gateway_update_exit_code",
        lambda required, code: events.append(f"status:{required}:{code}") or True,
    )

    assert update_cmd._finish_update_service_finalization(
        [],
        gateway_mode=True,
        gateway_fleet_restart_incomplete=False,
        updater_owner_discovery_failed=False,
        deferred_owner=None,
    ) is True

    assert events == ["dashboard", "status:True:0"]


def test_owner_pending_result_is_durable_before_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path)

    def fake_run(args, *unused_args, **unused_kwargs):
        assert args == [
            "systemctl",
            "--user",
            "show",
            "hermes-gateway-coding_lead",
            "--property=MainPID",
            "--value",
        ]
        return MagicMock(returncode=0, stdout="4321\n", stderr="")

    def fake_kill(pid, sig):
        assert (pid, sig) == (4321, signal.SIGUSR1)
        pending = json.loads(
            (tmp_path / ".update_owner_restart_pending.json").read_text()
        )
        assert pending == {
            "exit_code": 0,
            "owner_pid": 4321,
            "owner_service": "hermes-gateway-coding_lead",
            "version": 1,
        }
        assert not (tmp_path / ".update_exit_code").exists()

    with patch.object(update_cmd.subprocess, "run", side_effect=fake_run), patch.object(
        update_cmd.os, "kill", side_effect=fake_kill
    ):
        assert update_cmd._restart_updater_owning_gateway(
            _owner(), final_exit_code=0, persist_result=True
        ) is True


def test_owner_is_not_signalled_when_pending_result_cannot_be_written(monkeypatch):
    monkeypatch.setattr(
        update_cmd, "_write_gateway_owner_restart_pending", lambda *args, **kwargs: False
    )
    with patch.object(
        update_cmd.subprocess,
        "run",
        return_value=MagicMock(returncode=0, stdout="4321\n", stderr=""),
    ), patch.object(update_cmd.os, "kill") as kill:
        assert update_cmd._restart_updater_owning_gateway(
            _owner(), final_exit_code=0, persist_result=True
        ) is False

    kill.assert_not_called()


def test_owner_detection_uses_pid_cgroup_scope(monkeypatch):
    live = SimpleNamespace(
        _get_pid_cgroup_path=lambda _pid: (
            "/user.slice/user-1000.slice/hermes-gateway-coding_lead.service"
        ),
        _get_systemd_service_for_pid=lambda _pid: (
            "hermes-gateway-coding_lead.service"
        ),
        _extract_scope_from_cgroup=lambda _path: "user",
    )
    monkeypatch.setattr(update_cmd, "_m", lambda: live)

    owner, failed = update_cmd._detect_updater_systemd_gateway_owner()

    assert owner == _owner()
    assert failed is False


def test_nested_gateway_cgroup_resolves_owner_component(monkeypatch):
    live = SimpleNamespace(
        _get_pid_cgroup_path=lambda _pid: (
            "/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "hermes-gateway-coding_lead.service/worker.scope"
        ),
        _get_systemd_service_for_pid=lambda _pid: None,
        _extract_scope_from_cgroup=lambda _path: "user",
    )
    monkeypatch.setattr(update_cmd, "_m", lambda: live)

    assert update_cmd._detect_updater_systemd_gateway_owner() == (_owner(), False)


def test_unknown_non_systemd_ownership_is_not_a_failure(monkeypatch):
    live = SimpleNamespace(
        _get_pid_cgroup_path=lambda _pid: None,
        _get_systemd_service_for_pid=lambda _pid: None,
        _extract_scope_from_cgroup=lambda _path: None,
    )
    monkeypatch.setattr(update_cmd, "_m", lambda: live)

    assert update_cmd._detect_updater_systemd_gateway_owner() == (None, False)


def test_malformed_gateway_cgroup_that_cannot_resolve_owner_is_a_failure(monkeypatch):
    live = SimpleNamespace(
        _get_pid_cgroup_path=lambda _pid: (
            "/user.slice/user-1000.slice/hermes-gateway-coding_lead.service.extra"
        ),
        _get_systemd_service_for_pid=lambda _pid: None,
        _extract_scope_from_cgroup=lambda _path: "user",
    )
    monkeypatch.setattr(update_cmd, "_m", lambda: live)

    assert update_cmd._detect_updater_systemd_gateway_owner() == (None, True)


def test_terminal_owner_restart_signals_only_the_service_main_pid():
    def fake_run(args, *unused_args, **unused_kwargs):
        assert args == [
            "systemctl",
            "--user",
            "show",
            "hermes-gateway-coding_lead",
            "--property=MainPID",
            "--value",
        ]
        return MagicMock(returncode=0, stdout="4321\n", stderr="")

    with patch.object(update_cmd.subprocess, "run", side_effect=fake_run), patch.object(
        update_cmd.os, "kill"
    ) as kill:
        assert update_cmd._restart_updater_owning_gateway(
            _owner(), final_exit_code=0, persist_result=False
        ) is True

    kill.assert_called_once_with(4321, signal.SIGUSR1)
