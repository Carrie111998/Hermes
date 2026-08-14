"""Profile-scoped fail-closed gateway approval behavior."""

import pytest

import tools.approval as approval


@pytest.fixture(autouse=True)
def unattended_gateway(monkeypatch):
    monkeypatch.setattr(
        approval,
        "_get_approval_config",
        lambda: {"mode": "smart", "gateway_mode": "deny"},
    )
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False, raising=False)
    monkeypatch.setattr(
        approval,
        "get_current_session_key",
        lambda default="default": "unattended-gateway-test",
    )
    monkeypatch.setattr(approval, "is_approved", lambda _session, _key: False)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )


def test_gateway_deny_blocks_dangerous_terminal_without_smart_or_notify(monkeypatch):
    monkeypatch.setattr(
        approval,
        "_smart_approve",
        lambda *_args, **_kwargs: pytest.fail("gateway deny must run before smart approval"),
    )
    monkeypatch.setattr(
        approval,
        "_await_gateway_decision",
        lambda *_args, **_kwargs: pytest.fail("gateway deny must not notify the user"),
    )

    result = approval.check_all_command_guards("rm -rf /tmp/review-artifacts", "local")

    assert result["approved"] is False
    assert result["outcome"] == "gateway_denied"
    assert "unattended" in result["message"].lower()
    assert "do not ask the user" in result["message"].lower()


@pytest.mark.parametrize(
    "configured,expected",
    [
        (None, "prompt"),
        ("prompt", "prompt"),
        ("deny", "deny"),
        (" DENY ", "deny"),
        ("block", "deny"),
        ("approve", "prompt"),
        ("unknown", "prompt"),
    ],
)
def test_gateway_mode_parser_is_backward_compatible_and_fail_closed_on_request(
    monkeypatch, configured, expected
):
    config = {"mode": "smart"}
    if configured is not None:
        config["gateway_mode"] = configured
    monkeypatch.setattr(approval, "_get_approval_config", lambda: config)
    assert approval._get_gateway_approval_mode() == expected


def test_gateway_deny_blocks_execute_code_before_smart_approval(monkeypatch):
    monkeypatch.setattr(
        approval,
        "_smart_approve",
        lambda *_args, **_kwargs: pytest.fail(
            "gateway deny must run before execute_code smart approval"
        ),
    )

    result = approval.check_execute_code_guard("print('safe-looking')", "local")

    assert result["approved"] is False
    assert result["outcome"] == "gateway_denied"
    assert result["pattern_key"] == "execute_code"


def test_gateway_deny_blocks_execute_code_without_notifying(monkeypatch):
    monkeypatch.setattr(
        approval,
        "_get_approval_config",
        lambda: {"mode": "manual", "gateway_mode": "deny"},
    )
    monkeypatch.setattr(
        approval,
        "_await_gateway_decision",
        lambda *_args, **_kwargs: pytest.fail(
            "gateway deny must not notify for execute_code"
        ),
    )

    result = approval.check_execute_code_guard("print('x')", "local")

    assert result["approved"] is False
    assert result["outcome"] == "gateway_denied"
    assert result["pattern_key"] == "execute_code"


def test_gateway_deny_declines_mcp_elicitation_without_notifying(monkeypatch):
    monkeypatch.setitem(
        approval._gateway_notify_cbs,
        "unattended-gateway-test",
        lambda _data: None,
    )
    monkeypatch.setattr(
        approval,
        "_await_gateway_decision",
        lambda *_args, **_kwargs: pytest.fail(
            "gateway deny must not notify for MCP elicitation"
        ),
    )

    result = approval.request_elicitation_consent(
        "authorize write-capable MCP call",
        "MCP server requested consent",
    )

    assert result == "decline"


def test_mode_off_bypasses_gateway_deny_for_plugin_approval(monkeypatch):
    monkeypatch.setattr(
        approval,
        "_get_approval_config",
        lambda: {"mode": "off", "gateway_mode": "deny"},
    )
    monkeypatch.setattr(
        approval,
        "_await_gateway_decision",
        lambda *_args, **_kwargs: pytest.fail(
            "mode off must bypass plugin approval without notifying"
        ),
    )

    result = approval.request_tool_approval(
        "terminal",
        "plugin requested approval",
        rule_key="mode-off-ordering",
    )

    assert result == {"approved": True, "message": None}


def test_gateway_deny_blocks_plugin_approval_without_notify(monkeypatch):
    monkeypatch.setattr(
        approval,
        "_await_gateway_decision",
        lambda *_args, **_kwargs: pytest.fail("gateway deny must not notify the user"),
    )

    result = approval.request_tool_approval(
        "terminal",
        "plugin requested approval",
        rule_key="unattended-test",
    )

    assert result["approved"] is False
    assert result["outcome"] == "gateway_denied"
    assert "unattended" in result["message"].lower()
    assert "do not ask the user" in result["message"].lower()
