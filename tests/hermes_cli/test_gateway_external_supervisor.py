"""Tests for explicit ownership by a wrapped external gateway supervisor."""

from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway


def _clear_native_supervisor_markers(monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")


def test_external_marker_identifies_supervisor_process(monkeypatch):
    _clear_native_supervisor_markers(monkeypatch)
    monkeypatch.setenv(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")

    assert gateway._running_under_gateway_supervisor() is True


def test_shell_without_marker_still_refuses_running_supervised_gateway(
    monkeypatch, capsys
):
    _clear_native_supervisor_markers(monkeypatch)
    monkeypatch.delenv(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    monkeypatch.setattr(
        gateway,
        "get_gateway_runtime_snapshot",
        lambda: gateway.GatewayRuntimeSnapshot(
            manager="launchd", service_installed=True, service_running=True
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        gateway._guard_supervised_gateway_conflict()

    assert exc_info.value.code == 1
    assert "already running under launchd" in capsys.readouterr().out


def test_gateway_run_external_supervisor_flag_marks_process(monkeypatch):
    monkeypatch.delenv(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    monkeypatch.setattr(
        gateway, "_maybe_redirect_run_to_s6_supervision", lambda _args: False
    )
    observed = []
    monkeypatch.setattr(
        gateway,
        "run_gateway",
        lambda *_args, **_kwargs: observed.append(
            gateway.os.environ.get(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV)
        ),
    )

    gateway._gateway_command_inner(
        SimpleNamespace(gateway_command="run", external_supervisor=True)
    )

    assert observed == ["1"]


def test_update_hands_external_supervisor_gateway_back_without_watcher(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_argv",
        lambda _pid: [
            "python",
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
            "--external-supervisor",
        ],
    )
    monkeypatch.setattr(
        gateway,
        "launch_detached_profile_gateway_restart",
        lambda *_args: pytest.fail("detached watcher must not be launched"),
    )

    assert gateway._prepare_profile_gateway_update_restart("work", 1234) == (
        "external-supervisor"
    )


