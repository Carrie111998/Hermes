"""End-to-end executor coverage for delegated approval scope preflight."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.delegation_context import DelegatedApprovalScope
from hermes_cli.middleware import RequestMiddlewareResult
from run_agent import AIAgent


def _tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


@pytest.fixture()
def scoped_agent(tmp_path: Path) -> AIAgent:
    names = (
        "terminal",
        "read_file",
        "write_file",
        "web_search",
        "mcp__google_workspace__manage_email",
    )
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs(*names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._delegated_approval_scope = DelegatedApprovalScope(
        enabled=True,
        approved_mission_summary="Run repository checks",
        allowed_workspace_path=str(tmp_path.resolve()),
    )
    agent._approval_scope_escalations = []
    return agent


def _call(name: str, arguments: dict, call_id: str = "call-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _message(*calls: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content="", tool_calls=list(calls))


def _post_hook_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook_name, **kwargs: calls.append(kwargs) if hook_name == "post_tool_call" else [],
    )
    return calls


@pytest.mark.parametrize("tool_name", ["read_file", "write_file"])
def test_sequential_file_escape_is_blocked_before_dispatch(
    scoped_agent: AIAgent, tmp_path: Path, tool_name: str
) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    args = {"path": str(outside)}
    if tool_name == "write_file":
        args["content"] = "must-not-be-written"
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call(tool_name, args)), messages, "task-1"
        )

    dispatch.assert_not_called()
    assert json.loads(messages[0]["content"]) == {
        "error": "Delegated tool call blocked",
        "reason_code": "workspace_escape",
        "status": "blocked",
    }
    assert scoped_agent._approval_scope_escalations == ["workspace_escape"]


def test_concurrent_preflight_blocks_each_call_once_without_dispatch_or_duplicate_post(
    scoped_agent: AIAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    post_hooks = _post_hook_capture(monkeypatch)
    secret_a = str(tmp_path.parent / "raw-secret-a.txt")
    secret_b = str(tmp_path.parent / "raw-secret-b.txt")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_concurrent(
            _message(
                _call("read_file", {"path": secret_a}, "call-a"),
                _call("write_file", {"path": secret_b, "content": "raw-body"}, "call-b"),
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert len(messages) == 2
    assert len(post_hooks) == 2
    assert all(hook["status"] == "blocked" for hook in post_hooks)
    assert all(hook["error_type"] == "delegated_scope_block" for hook in post_hooks)
    assert all(hook["args"] == {} for hook in post_hooks)
    assert secret_a not in repr(messages + post_hooks)
    assert secret_b not in repr(messages + post_hooks)


def test_request_middleware_rewrite_is_checked_by_central_preflight(
    scoped_agent: AIAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = str(tmp_path.parent / "middleware-escape.txt")
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_tool_request_middleware",
        lambda _name, args, **_kwargs: RequestMiddlewareResult(
            payload={"path": outside, "content": "secret"},
            original_payload=args,
            changed=True,
            trace=[{"source": "test"}],
        ),
    )
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("write_file", {"path": "inside.txt", "content": "ok"})),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert json.loads(messages[0]["content"])["reason_code"] == "workspace_escape"


def test_inline_interpreter_escape_is_blocked_before_terminal_dispatch(
    scoped_agent: AIAgent, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "outside" / "pwned"
    command = f'python -c "open(\"{outside}\",\"w\").write(\"x\")"'
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("terminal", {"command": command})), messages, "task-1"
        )

    dispatch.assert_not_called()
    assert json.loads(messages[0]["content"])["reason_code"] == "unbounded_shell_execution"


def test_local_pytest_requires_parent_execution(scoped_agent: AIAgent) -> None:
    messages: list[dict] = []
    with patch("run_agent.handle_function_call", return_value="tests passed") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "terminal",
                    {"command": "pytest tests/agent/test_example.py", "workdir": scoped_agent._delegated_approval_scope.allowed_workspace_path},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert json.loads(messages[0]["content"])["reason_code"] == "terminal_requires_parent_execution"


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("pytest $(printf tests)", "unbounded_shell_execution"),
        ("node -e 'require(\"fs\").writeFileSync(\"/tmp/pwned\", \"x\")'", "unbounded_shell_execution"),
        ("git reset --hard HEAD~1", "git_state_change"),
        ("pip install requests", "global_package_install"),
        ("pip install --user requests", "global_package_install"),
        ("pipx install ruff", "global_package_install"),
    ],
)
def test_terminal_hard_exceptions_are_blocked_at_executor(
    scoped_agent: AIAgent, command: str, reason: str
) -> None:
    messages: list[dict] = []
    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "terminal",
                    {
                        "command": command,
                        "workdir": scoped_agent._delegated_approval_scope.allowed_workspace_path,
                    },
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert f'"reason_code": "{reason}"' in messages[0]["content"]


def test_workspace_virtualenv_pip_requires_parent_execution(scoped_agent: AIAgent) -> None:
    workspace = Path(scoped_agent._delegated_approval_scope.allowed_workspace_path)
    venv_pip = workspace / ".venv" / "bin" / "pip"
    messages: list[dict] = []

    with patch("run_agent.handle_function_call", return_value="installed") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "terminal",
                    {"command": f"{venv_pip} install requests", "workdir": str(workspace)},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert json.loads(messages[0]["content"])["reason_code"] == "terminal_requires_parent_execution"


def test_workspace_targeted_pip_requires_parent_execution(scoped_agent: AIAgent) -> None:
    workspace = Path(scoped_agent._delegated_approval_scope.allowed_workspace_path)
    messages: list[dict] = []

    with patch("run_agent.handle_function_call", return_value="installed locally") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "terminal",
                    {"command": "pip install --target vendor requests", "workdir": str(workspace)},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert json.loads(messages[0]["content"])["reason_code"] == "terminal_requires_parent_execution"


def test_terminal_workdir_outside_scope_is_blocked(scoped_agent: AIAgent, tmp_path: Path) -> None:
    messages: list[dict] = []
    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "terminal",
                    {"command": "pytest tests", "workdir": str(tmp_path.parent)},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "workspace_escape"' in messages[0]["content"]


@pytest.mark.parametrize(
    ("args", "blocked"),
    [
        ({"action": "send", "to": "customer@example.com", "body": "raw-message"}, True),
        ({"action": "send", "draft": True, "to": "customer@example.com", "body": "raw-message"}, False),
        ({"action": "search", "query": "status"}, False),
    ],
)
def test_google_email_send_is_blocked_but_draft_and_read_are_allowed(
    scoped_agent: AIAgent, args: dict, blocked: bool
) -> None:
    messages: list[dict] = []
    with patch("run_agent.handle_function_call", return_value="ok") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("mcp__google_workspace__manage_email", args)),
            messages,
            "task-1",
        )

    if blocked:
        dispatch.assert_not_called()
        assert '"reason_code": "external_transmission"' in messages[0]["content"]
    else:
        dispatch.assert_called_once()
        assert messages[0]["content"] == "ok"


def test_tool_search_unwrap_cannot_bypass_external_send_guard(
    scoped_agent: AIAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agent.tool_executor._tool_search_scoped_names",
        lambda agent: frozenset({"mcp__google_workspace__manage_email"}),
    )
    monkeypatch.setattr("tools.tool_search.is_deferrable_tool_name", lambda name: True)
    messages: list[dict] = []
    bridged = {
        "name": "mcp__google_workspace__manage_email",
        "arguments": {"action": "send", "to": "customer@example.com"},
    }

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("tool_call", bridged)), messages, "task-1"
        )

    dispatch.assert_not_called()
    assert '"reason_code": "external_transmission"' in messages[0]["content"]


@pytest.mark.parametrize(
    ("tool_name", "args", "reason"),
    [
        ("mcp__google_workspace__manage_calendar", {"operation": "events.quickAdd"}, "external_calendar_mutation"),
        ("mcp__google_workspace__manage_drive", {"operation": "files.copy"}, "external_drive_mutation"),
        ("mcp__google_workspace__manage_docs", {"operation": "documents.batchUpdate"}, "external_docs_mutation"),
        ("mcp__google_workspace__manage_sheets", {"operation": "spreadsheets.values.append"}, "external_sheets_mutation"),
        ("mcp__aside__browser", {"code": "await page.click('button')"}, "external_browser_mutation"),
        ("mcp__pencil__batch_design", {"operations": []}, "external_document_mutation"),
        ("mcp__kordoc__fill_template", {"path": "inside.docx"}, "external_document_mutation"),
        ("skill_manage", {"action": "patch", "name": "unsafe"}, "skill_mutation"),
    ],
)
def test_mutating_external_operations_are_blocked_at_executor(
    scoped_agent: AIAgent, tool_name: str, args: dict, reason: str
) -> None:
    messages: list[dict] = []
    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call(tool_name, args)), messages, "task-1"
        )

    dispatch.assert_not_called()
    assert f'"reason_code": "{reason}"' in messages[0]["content"]


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("mcp__google_workspace__manage_calendar", {"operation": "events.list"}),
        ("mcp__google_workspace__manage_drive", {"operation": "files.get"}),
        ("mcp__google_workspace__manage_docs", {"operation": "documents.get"}),
        ("mcp__google_workspace__manage_sheets", {"operation": "spreadsheets.values.get"}),
        ("mcp__aside__browser", {"action": "snapshot"}),
        ("mcp__pencil__get_editor_state", {}),
    ],
)
def test_read_only_external_operations_remain_available(
    scoped_agent: AIAgent, tool_name: str, args: dict
) -> None:
    messages: list[dict] = []
    with patch("run_agent.handle_function_call", return_value="read ok") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call(tool_name, args)), messages, "task-1"
        )

    dispatch.assert_called_once()
    assert messages[0]["content"] == "read ok"


def test_mcp_document_path_outside_workspace_is_blocked(
    scoped_agent: AIAgent, tmp_path: Path
) -> None:
    outside = str(tmp_path.parent / "customer-document.hwpx")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call("mcp__kordoc__parse_document", {"file_path": outside})
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "workspace_escape"' in messages[0]["content"]


def test_narrowed_deny_all_scope_blocks_otherwise_read_only_tool(
    scoped_agent: AIAgent,
) -> None:
    scope = getattr(scoped_agent, "_delegated_approval_scope")
    setattr(
        scoped_agent,
        "_delegated_approval_scope",
        DelegatedApprovalScope(
            enabled=True,
            approved_mission_summary=scope.approved_mission_summary,
            allowed_workspace_path=scope.allowed_workspace_path,
            allow_local_non_destructive=False,
        ),
    )
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("web_search", {"query": "public facts"})),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "local_non_destructive_disabled"' in messages[0]["content"]


def test_scope_block_never_exposes_raw_args_in_error_hook_or_logs(
    scoped_agent: AIAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_marker = "UNIQUE-RAW-DELEGATED-ARG"
    outside = str(tmp_path.parent / raw_marker)
    post_hooks = _post_hook_capture(monkeypatch)
    messages: list[dict] = []
    caplog.set_level("DEBUG")

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("write_file", {"path": outside, "content": raw_marker})),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert len(post_hooks) == 1
    assert post_hooks[0]["args"] == {}
    assert raw_marker not in repr(messages + post_hooks)
    assert raw_marker not in caplog.text


def test_disabled_inheritance_preserves_legacy_executor_behavior(
    scoped_agent: AIAgent, tmp_path: Path
) -> None:
    scoped_agent._delegated_approval_scope = DelegatedApprovalScope(
        enabled=False,
        approved_mission_summary="",
        allowed_workspace_path="",
    )
    outside = str(tmp_path.parent / "legacy-outside.txt")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call", return_value="legacy allowed") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("write_file", {"path": outside, "content": "legacy"})),
            messages,
            "task-1",
        )

    dispatch.assert_called_once()
    assert messages[0]["content"] == "legacy allowed"


def _enable_root_send_guard(agent: AIAgent, latest_user_text: str) -> None:
    agent._delegated_approval_scope = DelegatedApprovalScope(
        enabled=False,
        approved_mission_summary="",
        allowed_workspace_path="",
    )
    agent._customer_send_guard = {
        "enabled": True,
        "approval_prefix": "발송 승인:",
    }
    agent._current_fallback_context_messages = [
        {"role": "user", "content": latest_user_text}
    ]
    agent._customer_send_approval_consumed_turn = None
    agent._current_turn_id = "turn-1"


def test_root_email_send_requires_explicit_recipient_approval(
    scoped_agent: AIAgent,
) -> None:
    _enable_root_send_guard(scoped_agent, "고객에게 이메일 보내줘")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__google_workspace__manage_email",
                    {"operation": "send", "to": "customer@example.com"},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "customer_send_approval_required"' in messages[0]["content"]


def test_root_email_draft_remains_allowed(scoped_agent: AIAgent) -> None:
    _enable_root_send_guard(scoped_agent, "초안 저장해줘")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call", return_value="draft saved") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__google_workspace__manage_email",
                    {
                        "operation": "send",
                        "draft": True,
                        "to": "customer@example.com",
                    },
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_called_once()
    assert messages[0]["content"] == "draft saved"


def test_root_send_exact_recipient_approval_is_single_use(scoped_agent: AIAgent) -> None:
    _enable_root_send_guard(scoped_agent, "발송 승인: customer@example.com")
    first_messages: list[dict] = []
    second_messages: list[dict] = []
    call = _call(
        "mcp__google_workspace__manage_email",
        {"operation": "send", "to": "customer@example.com"},
    )

    with patch("run_agent.handle_function_call", return_value="sent") as dispatch:
        scoped_agent._execute_tool_calls_sequential(_message(call), first_messages, "task-1")
        scoped_agent._execute_tool_calls_sequential(_message(call), second_messages, "task-1")

    dispatch.assert_called_once()
    assert first_messages[0]["content"] == "sent"
    assert '"reason_code": "customer_send_approval_required"' in second_messages[0]["content"]


def test_root_send_approval_is_atomic_for_concurrent_calls(
    scoped_agent: AIAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from agent.tool_executor import _consume_root_send_approval

    class _DelayedConsumedTurn:
        value = None

        def __get__(self, instance, owner=None):
            value = self.value
            time.sleep(0.02)
            return value

        def __set__(self, instance, value):
            self.value = value

    _enable_root_send_guard(scoped_agent, "발송 승인: customer@example.com")
    monkeypatch.setattr(
        type(scoped_agent),
        "_customer_send_approval_consumed_turn",
        _DelayedConsumedTurn(),
        raising=False,
    )
    barrier = threading.Barrier(2)

    def _attempt() -> bool:
        barrier.wait()
        return _consume_root_send_approval(
            scoped_agent,
            {"operation": "send", "to": "customer@example.com"},
            consume=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _attempt(), range(2)))

    assert sorted(results) == [False, True]


def test_child_cannot_consume_root_send_approval(scoped_agent: AIAgent) -> None:
    scoped_agent._customer_send_guard = {
        "enabled": True,
        "approval_prefix": "발송 승인:",
    }
    scoped_agent._current_fallback_context_messages = [
        {"role": "user", "content": "발송 승인: customer@example.com"}
    ]
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__google_workspace__manage_email",
                    {"operation": "send", "to": "customer@example.com"},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "external_transmission"' in messages[0]["content"]


def test_root_send_approval_does_not_cover_a_different_recipient(
    scoped_agent: AIAgent,
) -> None:
    _enable_root_send_guard(scoped_agent, "발송 승인: approved@example.com")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__google_workspace__manage_email",
                    {"operation": "send", "to": "other@example.com"},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "customer_send_approval_required"' in messages[0]["content"]


def test_root_send_approval_target_matching_is_not_substring_based(
    scoped_agent: AIAgent,
) -> None:
    _enable_root_send_guard(scoped_agent, "발송 승인: customer@example.com.evil")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__google_workspace__manage_email",
                    {"operation": "send", "to": "customer@example.com"},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "customer_send_approval_required"' in messages[0]["content"]


def test_root_send_requires_exact_approved_recipient_set(
    scoped_agent: AIAgent,
) -> None:
    _enable_root_send_guard(
        scoped_agent,
        "발송 승인: customer@example.com, other@example.com",
    )
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__google_workspace__manage_email",
                    {"operation": "send", "to": "customer@example.com"},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "customer_send_approval_required"' in messages[0]["content"]


def test_root_generic_message_send_requires_explicit_target_approval(
    scoped_agent: AIAgent,
) -> None:
    _enable_root_send_guard(scoped_agent, "메시지를 보내줘")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "send_message",
                    {"operation": "send", "channel_id": "C_CUSTOMER"},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "customer_send_approval_required"' in messages[0]["content"]


@pytest.mark.parametrize("tool_name", ["yb_send_dm", "yb_send_sticker", "feishu_drive_reply_comment"])
def test_root_named_send_tools_require_explicit_approval(
    scoped_agent: AIAgent, tool_name: str
) -> None:
    _enable_root_send_guard(scoped_agent, "고객에게 보내줘")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call(tool_name, {"userId": "customer-1", "content": "hello"})),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "customer_send_approval_required"' in messages[0]["content"]


def test_root_send_approval_accepts_camel_case_target_key(scoped_agent: AIAgent) -> None:
    _enable_root_send_guard(scoped_agent, "발송 승인: customer-1")
    messages: list[dict] = []

    with patch("run_agent.handle_function_call", return_value="sent") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("yb_send_dm", {"userId": "customer-1", "content": "hello"})),
            messages,
            "task-1",
        )

    dispatch.assert_called_once()
    assert messages[0]["content"] == "sent"


def test_child_fetch_url_is_denied_by_default(scoped_agent: AIAgent) -> None:
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__fetch__fetch",
                    {"url": "https://example.com/?customer=private"},
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "unapproved_tool"' in messages[0]["content"]


def test_mcp_camel_case_output_path_cannot_escape_workspace(
    scoped_agent: AIAgent, tmp_path: Path
) -> None:
    messages: list[dict] = []
    outside = str(tmp_path.parent / "outside-download.bin")

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__google_workspace__manage_drive",
                    {
                        "operation": "download",
                        "fileId": "safe-id",
                        "outputPath": outside,
                    },
                )
            ),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "workspace_escape"' in messages[0]["content"]


def test_child_execute_code_is_denied_before_rpc(scoped_agent: AIAgent) -> None:
    messages: list[dict] = []

    with patch("run_agent.handle_function_call") as dispatch:
        scoped_agent._execute_tool_calls_sequential(
            _message(_call("execute_code", {"code": "print('safe')"})),
            messages,
            "task-1",
        )

    dispatch.assert_not_called()
    assert '"reason_code": "nested_tool_execution"' in messages[0]["content"]


def test_blocked_raw_args_do_not_reach_request_middleware(
    scoped_agent: AIAgent,
) -> None:
    messages: list[dict] = []

    with (
        patch(
            "hermes_cli.middleware.apply_tool_request_middleware"
        ) as middleware,
        patch("run_agent.handle_function_call") as dispatch,
    ):
        scoped_agent._execute_tool_calls_sequential(
            _message(
                _call(
                    "mcp__google_workspace__manage_email",
                    {
                        "operation": "send",
                        "to": "customer@example.com",
                        "body": "private",
                    },
                )
            ),
            messages,
            "task-1",
        )

    middleware.assert_not_called()
    dispatch.assert_not_called()
    assert '"reason_code": "external_transmission"' in messages[0]["content"]
