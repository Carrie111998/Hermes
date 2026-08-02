"""Scheduled terminal authority is whole-surface and content-opaque."""

from __future__ import annotations

import pytest

from tools import approval


@pytest.fixture(autouse=True)
def cron_session(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.setattr(
        approval,
        "check_exact_execution_authority",
        lambda *_args, **_kwargs: None,
    )
    tokens = set_session_vars(cron_session=True)
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize(
    "command",
    (
        "printf harmless",
        "opaque words that once matched a denylist",
        "sudo -S opaque",
        "launchctl submit -- opaque",
    ),
)
def test_cron_deny_blocks_whole_terminal_surface(monkeypatch, command):
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")

    result = approval.check_all_command_guards(command, "local")

    assert result["approved"] is False
    assert result["error_code"] == "cron_terminal_execution_not_authorized"
    assert "No command text was inspected" in result["message"]


@pytest.mark.parametrize(
    "command",
    (
        "printf harmless",
        "opaque words that once matched a denylist",
        "sudo -S opaque",
        "launchctl submit -- opaque",
    ),
)
def test_cron_approve_allows_whole_terminal_surface(monkeypatch, command):
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "approve")

    assert approval.check_all_command_guards(command, "local") == {
        "approved": True,
        "message": None,
    }


def test_isolated_backend_remains_structurally_allowed(monkeypatch):
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")

    assert approval.check_all_command_guards("opaque", "docker") == {
        "approved": True,
        "message": None,
    }


def test_docker_host_bind_is_not_treated_as_isolated(monkeypatch):
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")

    result = approval.check_all_command_guards(
        "opaque",
        "docker",
        has_host_access=True,
    )
    assert result["error_code"] == "cron_terminal_execution_not_authorized"
