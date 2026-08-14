"""Unit tests for the opt-in Claude Remote Control recovery boundary."""

from __future__ import annotations

import subprocess
import pytest

from hermes_cli import claude_recovery as recovery


def _cfg(**overrides):
    cfg = {
        "external_cli": {
            "remote_control_recovery": {
                "enabled": True,
                "task_name": "Hermes-Claude-RemoteControl",
                "remote_name": "cwliao-hermes",
                "wsl_distro": "Ubuntu",
                "claude_bin": "claude",
                "probe_timeout_seconds": 3,
                "repair_wait_seconds": 0,
            }
        }
    }
    cfg["external_cli"]["remote_control_recovery"].update(overrides)
    return cfg


def test_disabled_by_default_is_fail_closed(monkeypatch):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    monkeypatch.setattr(recovery, "_powershell", lambda *_: pytest.fail("no probe"))

    result = recovery.inspect({})

    assert result.status == "DISABLED"


def test_known_klib_task_cannot_be_selected_by_empty_defaults(monkeypatch):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    result = recovery.inspect(
        {"external_cli": {"remote_control_recovery": {"enabled": True}}}
    )

    assert result.status == "NOT_CONFIGURED"
    assert "task_name" in result.detail


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"enabled": "false"}, "enabled must be a boolean"),
        ({"probe_timeout_seconds": float("nan")}, "probe_timeout_seconds must be a non-negative number"),
        ({"repair_wait_seconds": float("inf")}, "repair_wait_seconds must be a non-negative number"),
    ],
)
def test_invalid_control_config_fails_closed(monkeypatch, override, expected):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")

    result = recovery.inspect(_cfg(**override))

    assert result.status == "NOT_CONFIGURED"
    assert result.detail == expected


def test_auth_parser_does_not_misclassify_not_logged_in():
    result = subprocess.CompletedProcess(
        ["claude"], 1, stdout="loggedIn: false\n", stderr=""
    )

    # The helper must classify false before the broad "logged in" fallback.
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(recovery, "_wsl", lambda *_: result)
        assert recovery._auth_state(recovery._config(_cfg()))[0] is False
    finally:
        monkeypatch.undo()


def test_inspect_ready_is_redacted(monkeypatch):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    monkeypatch.setattr(recovery, "_task_state", lambda cfg: ("Ready", None))
    monkeypatch.setattr(recovery, "_auth_state", lambda cfg: (True, None))
    monkeypatch.setattr(recovery, "_remote_count", lambda cfg: (1, None))

    result = recovery.inspect(_cfg())

    assert result.status == "READY"
    assert "session-url" not in result.detail
    assert "token" not in result.detail.lower()
    assert result.as_dict() == {
        "status": "READY",
        "detail": "one configured Remote Control session is present",
        "task_state": "Ready",
        "remote_count": 1,
        "auth_logged_in": True,
    }


@pytest.mark.parametrize("count,status", [(2, "AMBIGUOUS_MULTIPLE_SESSIONS"), (0, "REMOTE_CONTROL_MISSING")])
def test_inspect_classifies_session_count(monkeypatch, count, status):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    monkeypatch.setattr(recovery, "_task_state", lambda cfg: ("Ready", None))
    monkeypatch.setattr(recovery, "_auth_state", lambda cfg: (True, None))
    monkeypatch.setattr(recovery, "_remote_count", lambda cfg: (count, None))

    assert recovery.inspect(_cfg()).status == status


def test_repair_refuses_running_task_without_duplicate_start(monkeypatch):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        recovery,
        "inspect",
        lambda cfg: recovery.RecoveryResult(
            "TASK_RUNNING_REMOTE_CONTROL_MISSING",
            "task is running but the configured session is not present",
            task_state="Running",
            remote_count=0,
            auth_logged_in=True,
        ),
    )
    monkeypatch.setattr(recovery, "_powershell", lambda *_: pytest.fail("duplicate start"))

    assert recovery.repair(_cfg()).status == "TASK_RUNNING_REMOTE_CONTROL_MISSING"


@pytest.mark.parametrize("state", ["Queued", "Disabled"])
def test_repair_refuses_non_ready_task(monkeypatch, state):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    monkeypatch.setattr(recovery, "_task_state", lambda cfg: (state, None))
    monkeypatch.setattr(recovery, "_auth_state", lambda cfg: (True, None))
    monkeypatch.setattr(recovery, "_remote_count", lambda cfg: (0, None))
    monkeypatch.setattr(recovery, "_powershell", lambda *_: pytest.fail("must not start"))

    result = recovery.repair(_cfg())

    assert result.status == f"TASK_{state.upper()}_REMOTE_CONTROL_MISSING"


def test_repair_starts_only_existing_task_after_missing_preflight(monkeypatch):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        recovery,
        "inspect",
        lambda cfg: recovery.RecoveryResult(
            "REMOTE_CONTROL_MISSING",
            "configured Remote Control session is not present",
            task_state="Ready",
            remote_count=0,
            auth_logged_in=True,
        ),
    )
    calls = []

    def fake_powershell(cfg, operation):
        calls.append((cfg["task_name"], operation))
        return subprocess.CompletedProcess(["powershell.exe"], 0, stdout="", stderr="")

    monkeypatch.setattr(recovery, "_powershell", fake_powershell)

    result = recovery.repair(_cfg())

    assert result.status == "REPAIR_TRIGGERED"
    assert calls == [("Hermes-Claude-RemoteControl", "start")]


def test_repair_returns_terminal_poll_failure(monkeypatch):
    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    results = iter(
        [
            recovery.RecoveryResult(
                "REMOTE_CONTROL_MISSING",
                "configured Remote Control session is not present",
                task_state="Ready",
                remote_count=0,
                auth_logged_in=True,
            ),
            recovery.RecoveryResult(
                "REAUTH_REQUIRED",
                "Claude CLI is not authenticated",
                task_state="Ready",
                remote_count=0,
                auth_logged_in=False,
            ),
        ]
    )
    monkeypatch.setattr(recovery, "inspect", lambda cfg: next(results))
    monkeypatch.setattr(
        recovery,
        "_powershell",
        lambda *_: subprocess.CompletedProcess(
            ["powershell.exe"], 0, stdout="", stderr=""
        ),
    )

    result = recovery.repair(_cfg(repair_wait_seconds=1))

    assert result.status == "REAUTH_REQUIRED"
    assert result.auth_logged_in is False


def test_powershell_command_has_no_shell_and_uses_quoted_task(monkeypatch):
    calls = []
    monkeypatch.setattr(recovery.shutil, "which", lambda name: "powershell.exe")

    def fake_runner(argv, timeout):
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, stdout="Ready\n", stderr="")

    monkeypatch.setattr(recovery, "_runner", fake_runner)
    config = recovery._config(_cfg())
    config["task_name"] = "Hermes Task"
    recovery._powershell(config, "state")

    argv = calls[0][0]
    assert argv[0] == "powershell.exe"
    assert "-NoProfile" in argv
    assert "-NonInteractive" in argv
    assert "Start-ScheduledTask" not in argv[-1]
    assert "'Hermes Task'" in argv[-1]
