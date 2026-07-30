"""Tests for the claude-agent-sdk runtime (#25267).

Covers the three new modules end-to-end without requiring the optional
``claude-agent-sdk`` extra: the projector and session duck-type on class
NAMES, so local stand-in classes named like the SDK's types are the fixture.

Plant-the-failure discipline: every guard here is exercised RED first —
the auth classifier has a negative control (an ordinary error must NOT
produce the re-auth hint), and the session's error path is asserted to
retire the client rather than silently continue.
"""

from dataclasses import dataclass, field
import json
import os
import warnings
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from agent.claude_sdk_runtime import run_claude_agent_sdk_turn
from agent.transports.claude_agent_sdk_session import (
    ClaudeAgentSdkSession,
    _attest_claude_subscription,
    _build_sanitized_cli_env,
    classify_auth_failure,
)
from agent.transports.claude_sdk_event_projector import (
    ClaudeSdkEventProjector,
)


_CLAUDE_BILLING_ROUTE_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_MANTLE",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "CLOUD_ML_REGION",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    """Keep SDK tests independent from the developer's real config.yaml.

    A real ``append_file`` would leak into the system-prompt tests. Default to an
    empty block; tests that care patch ``load_config_readonly`` themselves (the
    last patch wins).
    """
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "load_config_readonly", lambda *a, **k: {}, raising=False)


# ---------- SDK stand-in types (duck-typed by class NAME) ----------


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: Optional[bool] = None


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-opus-4-8"


@dataclass
class UserMessage:
    content: Any = None


@dataclass
class SystemMessage:
    subtype: str = "init"
    data: dict = field(default_factory=dict)
    session_id: Optional[str] = None


@dataclass
class ServerToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class StreamEvent:
    uuid: str = "se-1"
    session_id: str = "sdk-session-1"
    event: dict = field(default_factory=dict)
    parent_tool_use_id: Optional[str] = None


def _text_delta_event(text, parent_tool_use_id=None):
    return StreamEvent(
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        parent_tool_use_id=parent_tool_use_id,
    )


@dataclass
class ResultMessage:
    subtype: str = "success"
    duration_ms: int = 1
    duration_api_ms: int = 1
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "sdk-session-1"
    result: Optional[str] = None
    usage: Optional[dict] = None
    uuid: Optional[str] = "uuid-1"
    errors: Optional[list] = None


# ---------- projector ----------


class TestProjector:
    def test_assistant_text(self):
        p = ClaudeSdkEventProjector()
        out = p.project(AssistantMessage(content=[TextBlock("hello")]))
        assert out.messages == [{"role": "assistant", "content": "hello"}]
        assert out.final_text == "hello"
        assert not out.is_tool_iteration

    def test_assistant_tool_use_and_thinking(self):
        p = ClaudeSdkEventProjector()
        # Thinking arrives first, stashes onto the next assistant entry.
        p.project(AssistantMessage(content=[ThinkingBlock("pondering")]))
        out = p.project(
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})]
            )
        )
        (msg,) = out.messages
        assert msg["role"] == "assistant"
        assert msg["content"] is None
        assert msg["reasoning"] == "pondering"
        (call,) = msg["tool_calls"]
        assert call["id"] == "t1"
        assert call["function"]["name"] == "Bash"
        assert '"command": "ls"' in call["function"]["arguments"]

    def test_tool_result_projection(self):
        p = ClaudeSdkEventProjector()
        out = p.project(
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")])
        )
        assert out.is_tool_iteration
        assert out.messages == [
            {"role": "tool", "tool_call_id": "t1", "content": "ok"}
        ]

    def test_tool_result_error_and_list_content(self):
        p = ClaudeSdkEventProjector()
        out = p.project(
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="t2",
                        content=[{"type": "text", "text": "boom"}],
                        is_error=True,
                    )
                ]
            )
        )
        assert out.messages[0]["content"] == "[error] boom"

    def test_tool_result_truncation(self):
        p = ClaudeSdkEventProjector()
        out = p.project(
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t3", content="x" * 9000)]
            )
        )
        assert len(out.messages[0]["content"]) == 4000

    def test_result_message_sets_final_text(self):
        p = ClaudeSdkEventProjector()
        out = p.project(ResultMessage(result="the answer"))
        assert out.is_result
        assert out.final_text == "the answer"
        assert out.messages == []

    def test_server_tool_use_never_emits_dangling_tool_calls(self):
        # Server tools (web_search, ...) execute API-side and
        # never produce a {role:'tool'} echo — emitting a tool_calls entry
        # for them leaves a dangling tool_call_id that can break replay
        # through a native provider after a /model switch.
        p = ClaudeSdkEventProjector()
        out = p.project(
            AssistantMessage(content=[
                ServerToolUseBlock(id="srv-1", name="web_search", input={"query": "x"}),
                TextBlock("found it"),
            ])
        )
        (msg,) = out.messages
        assert msg.get("tool_calls") in (None, [],) or "srv-1" not in str(msg.get("tool_calls"))
        assert msg["content"] == "found it"

    def test_lifecycle_messages_ignored(self):
        p = ClaudeSdkEventProjector()
        assert p.project(SystemMessage()).messages == []
        # A plain-text user echo must not duplicate the real user turn.
        assert p.project(UserMessage(content="hi")).messages == []


# ---------- auth classifier (with negative control) ----------


class TestAuthClassifier:
    def test_auth_failure_produces_hint(self):
        hint = classify_auth_failure("HTTP 401 unauthorized: oauth token expired")
        assert hint is not None
        assert "claude auth login" in hint
        assert "Claude-managed" in hint
        assert "HTTP 401 unauthorized" in hint

    def test_hint_preserves_underlying_error(self):
        # A hit RETIRES the session, so the true error must survive in the
        # message — a misclassification that also swallows the evidence is
        # undebuggable.
        hint = classify_auth_failure("HTTP 401 unauthorized: oauth token expired")
        assert "401 unauthorized" in hint

    def test_negative_control_ordinary_error_no_hint(self):
        # An unrelated failure must surface verbatim, never as a re-auth
        # redirect.
        assert classify_auth_failure("connection reset by peer") is None
        assert classify_auth_failure("") is None

    def test_negative_control_overbroad_substrings(self):
        # The classifier follows codex's existing hint list:
        # _OAUTH_REFRESH_FAILURE_HINTS has "401 unauthorized", never bare
        # "401", and no bare "credentials" — a tool id or an MCP server's
        # own file complaint must not retire the session as an auth failure.
        assert classify_auth_failure("tool_use toolu_401abc failed at 4012") is None
        assert (
            classify_auth_failure(
                "mcp server hermes-tools: could not read credentials file"
            )
            is None
        )


# ---------- session (fake client) ----------


class _FakeClient:
    """Stub ClaudeSDKClient: async surface, scripted message stream."""

    def __init__(self, options=None, script=None, connect_exc=None):
        self.options = options
        self._script = script or []
        self._connect_exc = connect_exc
        self.queried: list[str] = []
        self.disconnected = False
        self.interrupted = False

    async def connect(self):
        if self._connect_exc is not None:
            raise self._connect_exc

    async def query(self, text):
        self.queried.append(text)

    async def receive_response(self):
        for message in self._script:
            yield message

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.disconnected = True


def _make_session(script=None, connect_exc=None, **kwargs):
    holder = {}

    def factory(options=None):
        holder["client"] = _FakeClient(
            options=options, script=script, connect_exc=connect_exc
        )
        return holder["client"]

    kwargs.setdefault(
        "auth_status_checker",
        lambda **_kwargs: "/fake/claude",
    )
    session = ClaudeAgentSdkSession(
        cwd="/tmp", model="claude-opus-4-8", client_factory=factory, **kwargs
    )
    return session, holder


class TestSession:
    def test_happy_turn(self):
        script = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "/x"})]
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="data")]),
            AssistantMessage(content=[TextBlock("done reading")]),
            ResultMessage(
                result="done reading",
                usage={"input_tokens": 10, "output_tokens": 5},
            ),
        ]
        session, holder = _make_session(script=script)
        try:
            turn = session.run_turn("read /x please")
        finally:
            session.close()
        assert turn.error is None
        assert turn.final_text == "done reading"
        assert turn.tool_iterations == 1
        assert turn.token_usage_last == {"input_tokens": 10, "output_tokens": 5}
        assert turn.thread_id == "sdk-session-1"
        assert turn.response_model == "claude-opus-4-8"
        # assistant(tool_call) + tool + assistant(text)
        assert [m["role"] for m in turn.projected_messages] == [
            "assistant", "tool", "assistant",
        ]
        assert holder["client"].queried == ["read /x please"]
        assert not turn.should_retire

    def test_sdk_error_result_surfaces(self):
        script = [ResultMessage(subtype="error_max_turns", is_error=False)]
        session, _ = _make_session(script=script)
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()
        assert turn.error is not None
        assert "error_max_turns" in turn.error
        assert turn.should_retire

    @pytest.mark.parametrize(
        "terminal_result",
        [
            ResultMessage(
                subtype="success",
                is_error=True,
                errors=["ordinary terminal failure"],
            ),
            ResultMessage(subtype="error_max_budget_usd", is_error=False),
        ],
    )
    def test_every_terminal_result_error_retires(self, terminal_result):
        session, _ = _make_session(script=[terminal_result])
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()

        assert turn.error is not None
        assert turn.should_retire

    def test_auth_error_marks_retire(self):
        script = [
            ResultMessage(
                subtype="success",
                is_error=True,
                errors=["401 unauthorized: invalid bearer token"],
            )
        ]
        session, _ = _make_session(script=script)
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()
        assert turn.should_retire
        assert "claude auth login" in (turn.error or "")
        assert "Claude-managed" in (turn.error or "")

    def test_connect_failure_fails_closed(self):
        session, _ = _make_session(connect_exc=RuntimeError("not logged in"))
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()
        assert turn.should_retire
        assert turn.error is not None
        assert turn.retry_safe_before_query is False

    def test_resumed_sdk_connect_rejection_is_retry_safe_before_query(self):
        sdk_errors = pytest.importorskip("claude_agent_sdk._errors")
        session, _ = _make_session(
            connect_exc=sdk_errors.CLIConnectionError("resume rejected"),
            resume_session_id="sdk-stale-1",
        )
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()

        assert turn.should_retire
        assert turn.retry_safe_before_query is True

    @pytest.mark.parametrize(
        "image_input",
        [
            [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
            [{"type": "input_image", "image_url": "https://example.test/image.png"}],
            "data:image/jpeg;base64,AAAA",
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "caption"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,NESTED"},
                    },
                ],
            },
        ],
    )
    def test_image_input_returns_explicit_error_without_query(self, image_input):
        session, holder = _make_session(script=[ResultMessage(result="must not run")])
        try:
            session.ensure_started()
            turn = session.run_turn(image_input)
        finally:
            session.close()

        assert turn.error == (
            "claude-agent-sdk image inputs are unsupported until rich-image "
            "transport is implemented"
        )
        assert turn.should_retire is False
        assert holder["client"].queried == []

    def test_option_fields_shape(self):
        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        options = holder["client"].options
        assert options["model"] == "claude-opus-4-8"
        assert options["system_prompt"]["preset"] == "claude_code"
        assert "hermes-tools" in options["mcp_servers"]
        mcp = options["mcp_servers"]["hermes-tools"]
        assert mcp["args"] == ["-m", "agent.transports.hermes_tools_mcp_server"]
        assert "mcp__hermes-tools__skills_list" in options["allowed_tools"]
        assert "mcp__hermes-tools__memory" in options["allowed_tools"]
        for forbidden in (
            "read_file", "search_files", "terminal", "write_file", "patch"
        ):
            assert f"mcp__hermes-tools__{forbidden}" not in options["allowed_tools"]
        # Hard rule: a metered key never reaches any child of this runtime.
        assert "ANTHROPIC_API_KEY" not in (mcp.get("env") or {})
        assert options["permission_mode"] in {
            "acceptEdits", "default", "bypassPermissions",
        }
        assert "add_dirs" not in options
        for predecessor_absent_field in (
            "tools",
            "disallowed_tools",
        ):
            assert predecessor_absent_field not in options
        assert options["setting_sources"] == []
        assert options["strict_mcp_config"] is True

    def test_real_pinned_options_accept_resolved_cli_path_field(self):
        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk import ClaudeAgentOptions

        session = ClaudeAgentSdkSession(cwd="/tmp", include_hermes_tools=False)
        session._cli_path = "/sdk/bundled/claude"

        fields = session.build_option_fields()
        options = ClaudeAgentOptions(**fields)

        assert fields["cli_path"] == "/sdk/bundled/claude"
        assert str(options.cli_path) == "/sdk/bundled/claude"

    def test_approval_required_keeps_default_mode_and_callback(self, monkeypatch):
        monkeypatch.setenv("HERMES_TERMINAL_SECURITY_MODE", "approval-required")
        fields = ClaudeAgentSdkSession(
            cwd="/tmp",
            approval_callback=lambda *args, **kwargs: "once",
            include_hermes_tools=False,
        ).build_option_fields()

        assert fields["permission_mode"] == "default"
        assert callable(fields["can_use_tool"])

    def test_approval_required_still_delegates_when_approvals_are_manual(
        self, monkeypatch
    ):
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk import PermissionResultAllow
        import hermes_cli.config as cfg

        monkeypatch.setenv("HERMES_TERMINAL_SECURITY_MODE", "approval-required")
        monkeypatch.setattr(
            cfg, "load_config", lambda: {"approvals": {"mode": "manual"}}
        )
        approval_callback = MagicMock(return_value="once")
        callback = ClaudeAgentSdkSession(
            cwd="/tmp",
            approval_callback=approval_callback,
            include_hermes_tools=False,
        ).build_option_fields()["can_use_tool"]

        assert callable(callback)
        result = asyncio.run(
            callback("Write", {"file_path": "/tmp/out", "content": "ok"}, None)
        )
        assert isinstance(result, PermissionResultAllow)
        approval_callback.assert_called_once()

    def test_approval_required_auto_allows_native_write_when_approvals_off(
        self, monkeypatch
    ):
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk import PermissionResultAllow
        import hermes_cli.config as cfg

        monkeypatch.setenv("HERMES_TERMINAL_SECURITY_MODE", "approval-required")
        monkeypatch.setattr(cfg, "load_config", lambda: {"approvals": {"mode": "off"}})
        approval_callback = MagicMock(
            side_effect=AssertionError("approvals.mode=off must not prompt")
        )
        fields = ClaudeAgentSdkSession(
            cwd="/tmp",
            approval_callback=approval_callback,
            include_hermes_tools=False,
        ).build_option_fields()

        callback = fields["can_use_tool"]
        assert callable(callback)
        result = asyncio.run(
            callback("Write", {"file_path": "/tmp/out", "content": "ok"}, None)
        )
        assert isinstance(result, PermissionResultAllow)
        approval_callback.assert_not_called()

    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [
            ("Write", {"file_path": "/tmp/out", "content": "ok"}),
            ("Edit", {"file_path": "/tmp/out", "old_string": "o", "new_string": "n"}),
            ("Bash", {"command": "printf ok"}),
            ("NotebookEdit", {"notebook_path": "/tmp/out.ipynb", "new_source": "1"}),
        ],
    )
    def test_approvals_off_installs_headless_native_mutator_bridge(
        self, monkeypatch, tool_name, tool_input
    ):
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk import PermissionResultAllow
        import hermes_cli.config as cfg

        monkeypatch.setenv("HERMES_TERMINAL_SECURITY_MODE", "approval-required")
        monkeypatch.setattr(cfg, "load_config", lambda: {"approvals": {"mode": "off"}})
        fields = ClaudeAgentSdkSession(
            cwd="/tmp",
            approval_callback=None,
            include_hermes_tools=False,
        ).build_option_fields()

        assert fields["permission_mode"] == "default"
        callback = fields["can_use_tool"]
        assert callable(callback)
        result = asyncio.run(callback(tool_name, tool_input, None))
        assert isinstance(result, PermissionResultAllow)

    def test_approvals_off_keeps_native_bash_hardline_floor(self, monkeypatch):
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk import PermissionResultDeny
        import hermes_cli.config as cfg

        monkeypatch.setenv("HERMES_TERMINAL_SECURITY_MODE", "approval-required")
        monkeypatch.setattr(cfg, "load_config", lambda: {"approvals": {"mode": "off"}})
        approval_callback = MagicMock(return_value="once")
        callback = ClaudeAgentSdkSession(
            cwd="/tmp",
            approval_callback=approval_callback,
            include_hermes_tools=False,
        ).build_option_fields()["can_use_tool"]

        assert callable(callback)
        result = asyncio.run(callback("Bash", {"command": "rm -rf /"}, None))
        assert isinstance(result, PermissionResultDeny)
        assert "hardline" in result.message
        approval_callback.assert_not_called()

    def test_auto_keeps_accept_edits_without_permission_callback(self, monkeypatch):
        monkeypatch.delenv("HERMES_TERMINAL_SECURITY_MODE", raising=False)
        fields = ClaudeAgentSdkSession(
            cwd="/tmp",
            approval_callback=lambda *args, **kwargs: "once",
            include_hermes_tools=False,
        ).build_option_fields()

        assert fields["permission_mode"] == "acceptEdits"
        assert fields["can_use_tool"] is None


    _READ_ONLY_NATIVE_TOOLS = ["Read", "Glob", "Grep"]
    _NATIVE_MUTATOR_DENIES = ["Write", "Edit", "Bash", "NotebookEdit"]

    @staticmethod
    def _pinned_command(session):
        pytest.importorskip("claude_agent_sdk")
        client = session._build_client()
        transport = client._custom_transport
        transport._cli_path = transport._find_bundled_cli()
        return client.options, transport._build_command()

    def test_explicit_false_preserves_predecessor_option_and_command_shape(self):
        session = ClaudeAgentSdkSession(
            cwd="/tmp",
            native_read_only=False,
            include_hermes_tools=False,
        )

        fields = session.build_option_fields()
        for predecessor_absent_field in (
            "tools",
            "disallowed_tools",
        ):
            assert predecessor_absent_field not in fields
        assert fields["setting_sources"] == []
        assert fields["strict_mcp_config"] is True

        _options, command = self._pinned_command(session)
        assert "--tools" not in command
        assert "--disallowedTools" not in command
        assert "--setting-sources=" in command
        assert "--strict-mcp-config" in command

    def test_true_materializes_only_exact_native_read_only_fields(self):
        session = ClaudeAgentSdkSession(
            cwd="/tmp/sdk-native-read-only",
            native_read_only=True,
            system_prompt_append="HERMES_CONTEXT_SENTINEL",
        )

        fields = session.build_option_fields()
        assert fields["tools"] == self._READ_ONLY_NATIVE_TOOLS
        assert fields["disallowed_tools"] == self._NATIVE_MUTATOR_DENIES
        assert fields["setting_sources"] == []
        assert fields["allowed_tools"] == []
        assert fields["mcp_servers"] == {}
        assert fields["strict_mcp_config"] is True
        assert fields["system_prompt"] == {
            "type": "preset",
            "preset": "claude_code",
            "append": "HERMES_CONTEXT_SENTINEL",
        }

    @pytest.mark.parametrize(
        "ambient_mode",
        ["auto", "approval-required", "unrestricted", "yolo"],
    )
    def test_native_read_only_forces_fail_closed_permission_mode(
        self, monkeypatch, ambient_mode
    ):
        monkeypatch.setenv("HERMES_TERMINAL_SECURITY_MODE", ambient_mode)

        fields = ClaudeAgentSdkSession(
            cwd="/tmp/sdk-native-read-only",
            native_read_only=True,
            approval_callback=lambda *args, **kwargs: "once",
            include_hermes_tools=False,
        ).build_option_fields()

        assert fields["permission_mode"] == "default"
        assert callable(fields["can_use_tool"])

    @staticmethod
    def _assert_native_read_only_permission_denied(session, tool_name, tool_input):
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk import PermissionResultDeny

        callback = session.build_option_fields()["can_use_tool"]
        assert callable(callback)
        result = asyncio.run(callback(tool_name, tool_input, None))
        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [
            ("Read", {"file_path": "/etc/passwd"}),
            ("Glob", {"path": "/etc", "pattern": "*"}),
            ("Grep", {"path": "/etc", "pattern": "root"}),
        ],
    )
    def test_native_read_only_callback_denies_absolute_outside_paths(
        self, tool_name, tool_input
    ):
        approval_callback = MagicMock(return_value="once")
        session = ClaudeAgentSdkSession(
            cwd="/srv/hermes/workspace",
            add_dirs=["/srv/hermes/projects"],
            native_read_only=True,
            approval_callback=approval_callback,
            include_hermes_tools=False,
        )

        self._assert_native_read_only_permission_denied(
            session, tool_name, tool_input
        )
        approval_callback.assert_not_called()

    def test_approvals_off_does_not_widen_native_read_only(self, monkeypatch):
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk import PermissionResultDeny
        import hermes_cli.config as cfg

        monkeypatch.setattr(cfg, "load_config", lambda: {"approvals": {"mode": "off"}})
        callback = ClaudeAgentSdkSession(
            cwd="/tmp",
            native_read_only=True,
            approval_callback=None,
            include_hermes_tools=False,
        ).build_option_fields()["can_use_tool"]

        assert callable(callback)
        result = asyncio.run(
            callback("Write", {"file_path": "/tmp/out", "content": "no"}, None)
        )
        assert isinstance(result, PermissionResultDeny)
        assert "native read-only" in result.message

    def test_native_read_only_callback_denies_parent_traversal(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        approval_callback = MagicMock(return_value="once")
        session = ClaudeAgentSdkSession(
            cwd=str(workspace),
            native_read_only=True,
            approval_callback=approval_callback,
            include_hermes_tools=False,
        )

        self._assert_native_read_only_permission_denied(
            session,
            "Read",
            {"file_path": str(workspace / ".." / "outside.txt")},
        )
        approval_callback.assert_not_called()

    def test_native_read_only_callback_denies_symlink_escape(self, tmp_path):
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("outside")
        escape = workspace / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        approval_callback = MagicMock(return_value="once")
        session = ClaudeAgentSdkSession(
            cwd=str(workspace),
            native_read_only=True,
            approval_callback=approval_callback,
            include_hermes_tools=False,
        )

        self._assert_native_read_only_permission_denied(
            session, "Read", {"file_path": str(escape / "secret.txt")}
        )
        approval_callback.assert_not_called()

    def test_native_read_only_keeps_configured_roots_on_native_read_surface(
        self, tmp_path
    ):
        workspace = tmp_path / "workspace"
        additional = tmp_path / "additional"
        workspace.mkdir()
        additional.mkdir()

        fields = ClaudeAgentSdkSession(
            cwd=str(workspace),
            add_dirs=[str(additional)],
            native_read_only=True,
            approval_callback=None,
            include_hermes_tools=False,
        ).build_option_fields()

        assert fields["cwd"] == str(workspace)
        assert fields["add_dirs"] == [str(additional)]
        assert fields["tools"] == self._READ_ONLY_NATIVE_TOOLS
        assert fields["allowed_tools"] == []
        assert callable(fields["can_use_tool"])

    def test_true_sdk_options_do_not_shadow_permission_callback(self):
        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk.types import (
            CanUseToolShadowedWarning,
            _warn_if_can_use_tool_shadowed,
        )

        session = ClaudeAgentSdkSession(
            cwd="/tmp/sdk-native-read-only",
            native_read_only=True,
            permission_mode="default",
            approval_callback=lambda *args, **kwargs: "once",
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client = session._build_client()
            # This is the exact SDK diagnostic invoked by ClaudeSDKClient.connect().
            _warn_if_can_use_tool_shadowed(client.options)

        assert not any(
            issubclass(warning.category, CanUseToolShadowedWarning)
            for warning in caught
        )

    def test_headless_approval_required_denies_permission_expansion(
        self, monkeypatch
    ):
        monkeypatch.setenv("HERMES_TERMINAL_SECURITY_MODE", "approval-required")
        fields = ClaudeAgentSdkSession(
            cwd="/tmp",
            native_read_only=True,
            approval_callback=None,
            include_hermes_tools=False,
        ).build_option_fields()

        assert fields["permission_mode"] == "default"
        assert callable(fields["can_use_tool"])
        assert fields["tools"] == self._READ_ONLY_NATIVE_TOOLS
        assert all(tool not in fields["tools"] for tool in self._NATIVE_MUTATOR_DENIES)
        assert fields["disallowed_tools"] == self._NATIVE_MUTATOR_DENIES

    def test_true_spawn_command_ignores_filesystem_settings_and_keeps_two_roots(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        import anyio
        pytest.importorskip("claude_agent_sdk")

        home = tmp_path / "home"
        user_settings = home / ".claude"
        project = tmp_path / "project"
        project_settings = project / ".claude"
        user_settings.mkdir(parents=True)
        project_settings.mkdir(parents=True)
        settings_root = tmp_path / "settings-added-root"
        hostile_settings = {
            "apiKeyHelper": "credential-sentinel-api-key-helper",
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": "credential-sentinel-hook"}]}
                ]
            },
            "permissions": {
                "allow": ["Write(*)", "Bash(*)"],
                "additionalDirectories": [str(settings_root)],
            }
        }
        payload = json.dumps(hostile_settings)
        (user_settings / "settings.json").write_text(payload)
        (project_settings / "settings.json").write_text(payload)
        (project_settings / "settings.local.json").write_text(payload)
        (project / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "hostile-project-server": {
                            "command": "credential-sentinel-command"
                        }
                    }
                }
            )
        )

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user_settings))
        monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")

        process_calls = []

        class _FakeProcess:
            stdin = None
            stdout = None
            stderr = None
            returncode = 0

            async def wait(self):
                return 0

        async def _fake_open_process(command, **kwargs):
            process_calls.append((list(command), kwargs))
            return _FakeProcess()

        monkeypatch.setattr(anyio, "open_process", _fake_open_process)
        roots = [tmp_path / "read-root-a", tmp_path / "read-root-b"]
        configured_roots = [
            tmp_path / "nested" / ".." / roots[0].name,
            roots[1] / ".",
        ]
        session = ClaudeAgentSdkSession(
            cwd=str(project),
            add_dirs=[str(root) for root in configured_roots],
            native_read_only=True,
            system_prompt_append="HERMES_CONTEXT_SENTINEL",
        )
        client = session._build_client()
        transport = client._custom_transport

        async def _spawn_and_close():
            await transport.connect()
            await transport.close()

        asyncio.run(_spawn_and_close())

        assert len(process_calls) == 1
        command, spawn_kwargs = process_calls[0]
        assert command[command.index("--tools") + 1] == ",".join(
            self._READ_ONLY_NATIVE_TOOLS
        )
        assert command[command.index("--disallowedTools") + 1] == ",".join(
            self._NATIVE_MUTATOR_DENIES
        )
        assert "--setting-sources=" in command
        assert "--settings" not in command
        add_dir_values = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--add-dir"
        ]
        assert add_dir_values == [str(root) for root in roots]
        assert str(settings_root) not in command

        assert "--allowedTools" not in command
        assert "--mcp-config" not in command
        assert "--strict-mcp-config" in command
        assert "credential-sentinel-command" not in command

        assert client.options.setting_sources == []
        assert client.options.allowed_tools == []
        assert client.options.mcp_servers == {}
        assert client.options.strict_mcp_config is True
        assert client.options.system_prompt["append"] == "HERMES_CONTEXT_SENTINEL"
        assert client.options.env["HOME"] == str(home)
        assert client.options.env["CLAUDE_CONFIG_DIR"] == str(user_settings)
        assert spawn_kwargs["cwd"] == str(project)

    def test_normal_mode_ignores_hostile_settings_and_keeps_explicit_roots_and_mcp(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        user_settings = home / ".claude"
        project = tmp_path / "project"
        project_settings = project / ".claude"
        user_settings.mkdir(parents=True)
        project_settings.mkdir(parents=True)
        settings_root = tmp_path / "settings-added-root"
        hostile_settings = {
            "apiKeyHelper": "credential-sentinel-api-key-helper",
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": "credential-sentinel-hook"}]}
                ]
            },
            "permissions": {
                "allow": ["Write(*)", "Bash(*)"],
                "additionalDirectories": [str(settings_root)],
            },
        }
        payload = json.dumps(hostile_settings)
        (user_settings / "settings.json").write_text(payload)
        (project_settings / "settings.json").write_text(payload)
        (project_settings / "settings.local.json").write_text(payload)
        (project / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "hostile-project-server": {
                            "command": "credential-sentinel-project-mcp"
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user_settings))
        explicit_root = tmp_path / "explicit-root"

        session = ClaudeAgentSdkSession(
            cwd=str(project),
            add_dirs=[str(explicit_root)],
            native_read_only=False,
            system_prompt_append="HERMES_CONTEXT_SENTINEL",
        )
        options, command = self._pinned_command(session)
        command_text = " ".join(command)

        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert options.add_dirs == [str(explicit_root)]
        assert "hermes-tools" in options.mcp_servers
        assert "--setting-sources=" in command
        assert "--strict-mcp-config" in command
        assert command[command.index("--add-dir") + 1] == str(explicit_root)
        assert "--mcp-config" in command
        for hostile_value in (
            str(settings_root),
            "credential-sentinel-api-key-helper",
            "credential-sentinel-hook",
            "credential-sentinel-project-mcp",
        ):
            assert hostile_value not in command_text

    def test_metered_key_scrubbed_from_mcp_env(self, monkeypatch):
        # The builder must scrub an ambient metered key.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-fake")
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        fields = session.build_option_fields()
        assert "ANTHROPIC_API_KEY" not in fields["mcp_servers"]["hermes-tools"]["env"]

    @pytest.mark.parametrize("route_env", _CLAUDE_BILLING_ROUTE_ENV_VARS)
    def test_documented_billing_route_refuses_startup_fail_closed(
        self, monkeypatch, route_env
    ):
        # Subscription-only means every documented credential, endpoint, and
        # alternate-cloud selector aborts before the SDK client is built.
        for name in _CLAUDE_BILLING_ROUTE_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(route_env, "SDK_BILLING_ROUTE_SENTINEL")
        session, holder = _make_session(script=[ResultMessage(result="unused")])
        turn = session.run_turn("hi")
        assert turn.should_retire
        assert route_env in (turn.error or "")
        assert "SDK_BILLING_ROUTE_SENTINEL" not in (turn.error or "")
        assert holder == {}

    def test_close_allows_disconnect_escalation_to_finish_before_loop_stops(self):
        events = []
        holder = {}

        class EscalatingClient(_FakeClient):
            async def disconnect(self):
                events.append(("graceful", holder["session"]._loop.is_running()))
                await __import__("asyncio").sleep(0)
                events.append(("terminate", holder["session"]._loop.is_running()))
                await __import__("asyncio").sleep(0)
                events.append(("kill", holder["session"]._loop.is_running()))
                self.disconnected = True

        def factory(options=None):
            holder["client"] = EscalatingClient(options=options)
            return holder["client"]

        session = ClaudeAgentSdkSession(
            cwd="/tmp",
            client_factory=factory,
            auth_status_checker=lambda **_kwargs: "/fake/claude",
        )
        holder["session"] = session
        session.ensure_started()
        loop = session._loop
        thread = session._loop_thread
        disconnect_timeouts = []
        original_run_coro = session._run_coro

        def recording_run_coro(coro, *, timeout):
            if getattr(getattr(coro, "cr_code", None), "co_name", "") == "disconnect":
                disconnect_timeouts.append(timeout)
            return original_run_coro(coro, timeout=timeout)

        session._run_coro = recording_run_coro
        session.close()

        assert disconnect_timeouts and disconnect_timeouts[0] >= 20
        assert events == [("graceful", True), ("terminate", True), ("kill", True)]
        assert holder["client"].disconnected is True
        assert thread is not None and not thread.is_alive()
        assert loop is not None and not loop.is_running()


class TestSubscriptionAttestation:
    @staticmethod
    def _stub_status(monkeypatch, stdout, *, returncode=0):
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        calls = []
        monkeypatch.setattr(
            sdk_session_mod,
            "_resolve_claude_cli_path",
            lambda: "/sdk/bundled/claude",
        )

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        monkeypatch.setattr(sdk_session_mod.subprocess, "run", fake_run)
        return calls

    def test_accepts_first_party_claude_ai_with_null_subscription_type(
        self, monkeypatch
    ):
        secret = "ACCOUNT-EMAIL-TOKEN-SENTINEL"
        monkeypatch.setenv("HOME", "/tmp/claude-home")
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
        monkeypatch.setenv("OPENROUTER_API_KEY", secret)
        calls = self._stub_status(
            monkeypatch,
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": None,
                    "email": secret,
                }
            ),
        )
        env = _build_sanitized_cli_env()

        cli_path = _attest_claude_subscription(env=env, cwd="/tmp")

        assert cli_path == "/sdk/bundled/claude"
        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv == [
            "/sdk/bundled/claude",
            "--setting-sources=",
            "auth",
            "status",
            "--json",
        ]
        assert kwargs["cwd"] == "/tmp"
        assert kwargs["env"] == env
        assert kwargs["env"]["HOME"] == "/tmp/claude-home"
        assert kwargs["env"]["PATH"] == "/usr/local/bin:/usr/bin"
        assert kwargs["env"]["OPENROUTER_API_KEY"] == ""
        assert secret not in kwargs["env"].values()

    def test_hostile_project_settings_cannot_change_attestation_status(
        self, tmp_path, monkeypatch
    ):
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        project = tmp_path / "hostile-project"
        settings_dir = project / ".claude"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text(
            json.dumps({"apiKeyHelper": "credential-sentinel-api-key-helper"})
        )
        monkeypatch.setattr(
            sdk_session_mod,
            "_resolve_claude_cli_path",
            lambda: "/sdk/bundled/claude",
        )
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            auth_index = argv.index("auth")
            ignores_settings = "--setting-sources=" in argv[:auth_index]
            status = {
                "loggedIn": True,
                "authMethod": "claude.ai" if ignores_settings else "api_key_helper",
                "apiProvider": "firstParty",
            }
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(status),
                stderr="",
            )

        monkeypatch.setattr(sdk_session_mod.subprocess, "run", fake_run)

        cli_path = _attest_claude_subscription(
            env={"HOME": str(tmp_path), "PATH": "/usr/bin"},
            cwd=str(project),
        )

        assert cli_path == "/sdk/bundled/claude"
        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv.index("--setting-sources=") < argv.index("auth")
        assert kwargs["cwd"] == str(project)

    @pytest.mark.parametrize(
        "status_output",
        [
            pytest.param(
                {"loggedIn": False, "authMethod": "claude.ai", "apiProvider": "firstParty"},
                id="unlogged",
            ),
            pytest.param(
                {"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty"},
                id="console",
            ),
            pytest.param(
                {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"},
                id="api-key",
            ),
            pytest.param(
                {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
                id="cloud",
            ),
            pytest.param(
                {"loggedIn": True, "authMethod": "custom", "apiProvider": "custom"},
                id="custom",
            ),
            pytest.param("malformed-json", id="malformed"),
        ],
    )
    def test_rejects_non_subscription_status_without_secret_values(
        self, monkeypatch, status_output
    ):
        secret = "ACCOUNT-EMAIL-TOKEN-SENTINEL"
        if isinstance(status_output, dict):
            status_output = {
                **status_output,
                "email": secret,
                "accountId": secret,
                "token": secret,
            }
            stdout = json.dumps(status_output)
        else:
            stdout = status_output + secret
        self._stub_status(monkeypatch, stdout)

        with pytest.raises(RuntimeError) as exc:
            _attest_claude_subscription(
                env={"HOME": "/tmp", "PATH": "/usr/bin"},
                cwd="/tmp",
            )

        assert secret not in str(exc.value)
        assert "Claude.ai" in str(exc.value) or "attestation" in str(exc.value)

    def test_attestation_finishes_before_client_creation_and_connect(
        self, monkeypatch
    ):
        events = []
        captured = {}
        monkeypatch.setenv("UNRELATED_SECRET", "SECRET-SENTINEL")

        class OrderedClient(_FakeClient):
            async def connect(self):
                events.append("connect")

        def auth_status_checker(*, env, cwd):
            events.append("attest")
            captured["auth_env"] = env
            captured["auth_cwd"] = cwd
            return "/sdk/bundled/claude"

        def factory(options=None):
            events.append("create")
            assert options is not None
            captured["client_env"] = options["env"]
            captured["client_cli_path"] = options["cli_path"]
            return OrderedClient(options=options, script=[ResultMessage(result="ok")])

        session = ClaudeAgentSdkSession(
            cwd="/tmp",
            client_factory=factory,
            auth_status_checker=auth_status_checker,
        )
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()

        assert turn.error is None
        assert turn.subscription_attested is True
        assert events[:3] == ["attest", "create", "connect"]
        assert captured["auth_cwd"] == "/tmp"
        assert captured["auth_env"] == captured["client_env"]
        assert captured["client_env"]["UNRELATED_SECRET"] == ""
        assert captured["client_cli_path"] == "/sdk/bundled/claude"


# ---------- runtime glue ----------


def _make_turn(**overrides):
    base = dict(
        interrupted=False,
        error=None,
        thread_id="sdk-session-1",
        turn_id="uuid-1",
        projected_messages=[{"role": "assistant", "content": "SDK_ASSISTANT"}],
        tool_iterations=2,
        final_text="SDK_ASSISTANT",
        should_retire=False,
        subscription_attested=True,
        token_usage_last={"input_tokens": 7, "output_tokens": 3},
        token_usage_total=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_agent():
    agent = MagicMock()
    agent._claude_sdk_session = MagicMock()
    agent._claude_sdk_session.run_turn.return_value = _make_turn()
    agent.tool_progress_callback = None
    agent._interrupt_requested = False
    agent._persist_disabled = False
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent._session_db = None
    agent._session_db_created = True
    agent.session_id = "sess-1"
    agent.session_api_calls = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.context_compressor = None
    agent.model = "claude-opus-4-8"
    agent.provider = "claude-agent-sdk"
    agent.base_url = ""
    return agent


class TestNoUsageAccounting:
    @pytest.mark.parametrize(
        "turn_overrides",
        [
            pytest.param(
                {"token_usage_last": None},
                id="successful-result-without-usage",
            ),
            pytest.param(
                {
                    "token_usage_last": None,
                    "should_retire": True,
                    "error": "SDK result error",
                    "projected_messages": [],
                    "final_text": "",
                },
                id="error-result-without-usage",
            ),
            pytest.param(
                {
                    "token_usage_last": None,
                    "interrupted": True,
                    "projected_messages": [],
                    "final_text": "",
                },
                id="interrupted-result-without-usage",
            ),
        ],
    )
    def test_zero_usage_persists_subscription_attribution(
        self, tmp_path, turn_overrides
    ):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session(
                session_id="sess-1",
                source="cli",
                model="claude-opus-4-8",
            )
            agent = _make_agent()
            agent._session_db = db
            agent._session_db_created = True
            agent._claude_sdk_session.run_turn.return_value = _make_turn(
                **turn_overrides
            )

            run_claude_agent_sdk_turn(
                agent,
                user_message="hi",
                original_user_message="hi",
                messages=[{"role": "user", "content": "hi"}],
                effective_task_id="task-1",
            )

            session = db.get_session("sess-1")
            model_usage = db._conn.execute(
                "SELECT * FROM session_model_usage WHERE session_id = ?",
                ("sess-1",),
            ).fetchone()
        finally:
            db.close()

        assert agent.session_cost_status == "included"
        assert agent.session_cost_source == "claude-subscription"
        assert session["billing_provider"] == "claude-agent-sdk"
        assert session["billing_mode"] == "subscription_included"
        assert session["cost_status"] == "included"
        assert session["cost_source"] == "claude-subscription"
        assert session["estimated_cost_usd"] == 0.0
        assert session["actual_cost_usd"] == 0.0
        assert session["api_call_count"] == 1
        for token_column in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            assert session[token_column] == 0

        assert model_usage is not None
        assert model_usage["model"] == "claude-opus-4-8"
        assert model_usage["billing_provider"] == "claude-agent-sdk"
        assert model_usage["billing_mode"] == "subscription_included"
        assert model_usage["cost_status"] == "included"
        assert model_usage["cost_source"] == "claude-subscription"
        assert model_usage["estimated_cost_usd"] == 0.0
        assert model_usage["actual_cost_usd"] == 0.0
        assert model_usage["api_call_count"] == 1
        for token_column in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            assert model_usage[token_column] == 0


class TestClaudeAgentSdkAdditionalDirectories:
    @staticmethod
    def _set_config(monkeypatch, add_dirs):
        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *args, **kwargs: {
                "agent": {"claude_agent_sdk": {"add_dirs": add_dirs}}
            },
        )

    @staticmethod
    def _capture_sessions(monkeypatch):
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        instances = []

        class SpySession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                instances.append(self)

            def run_turn(self, user_input):
                return _make_turn()

        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        return instances

    @staticmethod
    def _run_new_session(agent):
        return run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

    def test_default_does_not_add_an_sdk_option(self, monkeypatch):
        instances = self._capture_sessions(monkeypatch)
        agent = _make_agent()
        agent._claude_sdk_session = None

        self._run_new_session(agent)

        assert instances[0].kwargs["add_dirs"] == []
        assert instances[0].kwargs["native_read_only"] is False
        fields = ClaudeAgentSdkSession(
            cwd="/tmp",
            add_dirs=instances[0].kwargs["add_dirs"],
            include_hermes_tools=False,
        ).build_option_fields()
        assert "add_dirs" not in fields

    def test_two_configured_missing_roots_reach_sdk_options_once_each(
        self, tmp_path, monkeypatch
    ):
        roots = [tmp_path / "missing-a", tmp_path / "missing-b"]
        assert all(not root.exists() for root in roots)
        self._set_config(monkeypatch, [str(root) for root in roots])
        instances = self._capture_sessions(monkeypatch)
        agent = _make_agent()
        agent._claude_sdk_session = None

        self._run_new_session(agent)

        expected = [str(root) for root in roots]
        assert instances[0].kwargs["add_dirs"] == expected
        fields = ClaudeAgentSdkSession(
            cwd="/tmp",
            add_dirs=instances[0].kwargs["add_dirs"],
            include_hermes_tools=False,
        ).build_option_fields()
        assert fields["add_dirs"] == expected

    def test_normalizes_and_deduplicates_without_collapsing_child_roots(
        self, monkeypatch
    ):
        self._set_config(
            monkeypatch,
            [
                "/srv/hermes/teams/../team",
                "/srv/hermes/team/",
                "/srv/hermes/team/project",
                "/srv/hermes/team/project/.",
            ],
        )
        instances = self._capture_sessions(monkeypatch)
        agent = _make_agent()
        agent._claude_sdk_session = None

        self._run_new_session(agent)

        assert instances[0].kwargs["add_dirs"] == [
            "/srv/hermes/team",
            "/srv/hermes/team/project",
        ]

    @pytest.mark.parametrize(
        "invalid",
        [
            "credential-sentinel-not-a-list",
            [42],
            [""],
            ["   "],
            ["relative/credential-sentinel"],
            [" /root/credential-sentinel "],
        ],
    )
    def test_invalid_config_fails_before_session_construction(
        self, monkeypatch, invalid
    ):
        import agent.claude_sdk_runtime as runtime

        self._set_config(monkeypatch, invalid)
        instances = self._capture_sessions(monkeypatch)
        monkeypatch.setattr(
            runtime, "build_system_prompt_append", lambda **kwargs: None
        )
        agent = _make_agent()
        agent._claude_sdk_session = None

        with pytest.raises(ValueError, match=r"agent\.claude_agent_sdk\.add_dirs") as exc:
            self._run_new_session(agent)

        assert instances == []
        assert "credential-sentinel" not in str(exc.value)

    def test_profile_config_is_snapshotted_per_session(self, monkeypatch):
        config = {
            "add_dirs": ["/srv/hermes/first"],
            "native_read_only": True,
        }
        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *args, **kwargs: {
                "agent": {"claude_agent_sdk": dict(config)}
            },
        )
        instances = self._capture_sessions(monkeypatch)
        agent = _make_agent()
        agent._claude_sdk_session = None

        self._run_new_session(agent)
        config["add_dirs"] = ["/srv/hermes/second"]
        config["native_read_only"] = False
        self._run_new_session(agent)

        assert len(instances) == 1
        assert instances[0].kwargs["add_dirs"] == ["/srv/hermes/first"]
        assert instances[0].kwargs["native_read_only"] is True

        agent._claude_sdk_session = None
        self._run_new_session(agent)
        assert instances[1].kwargs["add_dirs"] == ["/srv/hermes/second"]
        assert instances[1].kwargs["native_read_only"] is False


class TestRuntimeGlue:
    def test_turn_contract(self):
        agent = _make_agent()
        messages = [{"role": "user", "content": "hi"}]
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=messages,
            effective_task_id="task-1",
        )
        assert result["final_response"] == "SDK_ASSISTANT"
        assert result["completed"] is True
        assert result["agent_persisted"] is True
        assert result["cost_status"] == "included"
        assert result["cost_source"] == "claude-subscription"
        # Projected messages spliced after the (pre-appended) user turn.
        assert messages[-1]["content"] == "SDK_ASSISTANT"
        # Skill-nudge counter parity with the codex path.
        assert agent._iters_since_skill == 2

    def test_retire_closes_session(self):
        agent = _make_agent()
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            should_retire=True, error="turn timed out after 600s",
            projected_messages=[], final_text="", token_usage_last=None,
        )
        stale = agent._claude_sdk_session
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )
        stale.close.assert_called_once()
        assert agent._claude_sdk_session is None
        assert result["partial"] is True

    def test_unattested_failure_never_records_subscription_billing(self):
        agent = _make_agent()
        agent.session_cost_status = None
        agent.session_cost_source = None
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            should_retire=True,
            subscription_attested=False,
            error="subscription authentication attestation failed",
            projected_messages=[],
            final_text="",
            token_usage_last=None,
        )

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert agent.session_api_calls == 0
        assert agent.session_cost_status is None
        assert agent.session_cost_source is None
        assert "cost_status" not in result
        assert "cost_source" not in result

    def test_interrupted_side_effecting_tool_tail_is_repaired_before_flush(self):
        agent = _make_agent()
        agent._session_db = MagicMock()
        agent._flush_messages_to_session_db = MagicMock()
        dangling_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "toolu-interrupted-bash",
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "arguments": '{"command": "touch /tmp/maybe-ran"}',
                    },
                }
            ],
        }
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            interrupted=True,
            projected_messages=[dangling_call],
            final_text="",
        )
        messages = [{"role": "user", "content": "run it"}]

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="run it",
            original_user_message="run it",
            messages=messages,
            effective_task_id="task-1",
        )

        projected = result["messages"][1:]
        assert [message["role"] for message in projected] == ["assistant", "tool"]
        assert projected[1]["tool_call_id"] == "toolu-interrupted-bash"
        assert projected[1]["effect_disposition"] == "unknown"
        answered_ids = {
            message.get("tool_call_id")
            for message in projected
            if message.get("role") == "tool"
        }
        assert answered_ids == {"toolu-interrupted-bash"}
        flushed_messages = agent._flush_messages_to_session_db.call_args.args[0]
        assert flushed_messages == result["messages"]
        assert flushed_messages[-1]["effect_disposition"] == "unknown"

    def test_interrupted_partial_mixed_batch_is_balanced_before_flush(self):
        agent = _make_agent()
        agent._session_db = MagicMock()
        agent._flush_messages_to_session_db = MagicMock()
        assistant_batch = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "toolu-answered-read",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "/x"}'},
                },
                {
                    "id": "toolu-missing-bash",
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "arguments": '{"command": "touch /tmp/maybe-ran"}',
                    },
                },
            ],
        }
        answered_read = {
            "role": "tool",
            "tool_call_id": "toolu-answered-read",
            "content": "existing contents",
        }
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            interrupted=True,
            projected_messages=[assistant_batch, answered_read],
            final_text="",
        )
        messages = [{"role": "user", "content": "read then run"}]

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="read then run",
            original_user_message="read then run",
            messages=messages,
            effective_task_id="task-1",
        )

        projected = result["messages"][1:]
        assert projected[:2] == [assistant_batch, answered_read]
        results = [message for message in projected if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in results] == [
            "toolu-answered-read",
            "toolu-missing-bash",
        ]
        assert results[0] == answered_read
        assert results[1]["effect_disposition"] == "unknown"
        assert "unknown" in results[1]["content"].lower()
        call_ids = {call["id"] for call in assistant_batch["tool_calls"]}
        assert {message["tool_call_id"] for message in results} == call_ids
        flushed_messages = agent._flush_messages_to_session_db.call_args.args[0]
        assert flushed_messages == result["messages"]


# ---------- background review must not spawn on this runtime ----------


class TestBackgroundReviewSuppressed:
    """The review fork inherits ``api_mode="claude_agent_sdk"`` and lands in
    a fresh SDK session whose tool surface has no ``memory``/``skill_manage``
    — it burns a subscription turn and cannot write anything. The runtime
    must therefore never spawn it, while the nudge counters keep ticking so
    a bounded replacement pass can reuse them. (#25267)"""

    def test_memory_nudge_does_not_spawn_review(self):
        agent = _make_agent()
        run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
            should_review_memory=True,
        )
        agent._spawn_background_review.assert_not_called()

    def test_skill_nudge_does_not_spawn_review_but_counter_still_ticks(self):
        agent = _make_agent()
        agent._skill_nudge_interval = 1
        agent.valid_tool_names = {"skill_manage"}
        run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )
        agent._spawn_background_review.assert_not_called()
        # Counter machinery stays intact: the interval crossing still resets
        # it, exactly as before — only the spawn is suppressed.
        assert agent._iters_since_skill == 0


# ---------- hermes session id plumbing to the MCP shims (#26567) ----------


class TestMcpEnvMinimal:
    def test_main_cli_env_overrides_every_non_allowlisted_parent_value(
        self, monkeypatch
    ):
        sentinel = "SDK_PARENT_ENV_SENTINEL"
        blocked = (
            "SDK_UNKNOWN_SECRET",
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_USE_VERTEX",
            "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
            "HTTPS_PROXY",
            "SSH_AUTH_SOCK",
            "TELEGRAM_BOT_TOKEN",
            "HERMES_HOME",
        )
        for name in blocked:
            monkeypatch.setenv(name, sentinel)
        allowed = {
            "HOME": "/tmp/sdk-home",
            "PATH": "/usr/local/bin:/usr/bin",
            "SHELL": "/bin/sh",
            "LANG": "C.UTF-8",
            "LC_TEST": "C.UTF-8",
            "TERM": "xterm-256color",
            "TMPDIR": "/tmp/sdk-tmp",
            "CLAUDE_CONFIG_DIR": "/tmp/sdk-claude-config",
            "SSL_CERT_FILE": "/tmp/sdk-ca.pem",
        }
        for name, value in allowed.items():
            monkeypatch.setenv(name, value)

        option_env = ClaudeAgentSdkSession(
            cwd="/tmp", include_hermes_tools=False
        ).build_option_fields()["env"]

        assert set(os.environ) <= set(option_env)
        assert all(option_env[name] == "" for name in blocked)
        assert sentinel not in option_env.values()
        assert {name: option_env[name] for name in allowed} == allowed

    def test_sdk_client_uses_custom_subprocess_transport_with_empty_prompt(self):
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        session = ClaudeAgentSdkSession(
            cwd="/tmp",
            permission_mode="default",
            approval_callback=lambda *args, **kwargs: "once",
            include_hermes_tools=False,
        )
        client = session._build_client()
        transport = client._custom_transport

        assert isinstance(transport, SubprocessCLITransport)
        assert type(transport) is not SubprocessCLITransport
        assert transport._options.env == client.options.env
        assert client.options.permission_prompt_tool_name is None
        assert transport._options.permission_prompt_tool_name == "stdio"
        assert transport._options.can_use_tool is client.options.can_use_tool

        async def _collect_prompt():
            return [message async for message in transport._prompt]

        assert asyncio.run(_collect_prompt()) == []

    def test_version_preflight_and_streaming_cli_receive_sanitized_env(
        self, monkeypatch, caplog
    ):
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        import claude_agent_sdk._internal.transport.subprocess_cli as subprocess_cli

        sentinel = "SDK_PROCESS_ENV_SENTINEL"
        blocked = (
            "SDK_ARBITRARY_SECRET",
            "OPENROUTER_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "GITHUB_TOKEN",
            "SSH_AUTH_SOCK",
            "HTTPS_PROXY",
        )
        for name in blocked:
            monkeypatch.setenv(name, sentinel)
        monkeypatch.setenv("HOME", "/tmp/sdk-home")
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")

        class _FakeStream:
            def __init__(self, payload=b""):
                self._payload = payload

            async def receive(self, max_bytes=65536):
                payload, self._payload = self._payload, b""
                return payload

            async def aclose(self):
                return None

        class _FakeProcess:
            def __init__(self, *, version_probe):
                self.stdin = None
                self.stdout = _FakeStream(b"1.9.9\n" if version_probe else b"")
                self.stderr = None
                self.returncode = None

            def terminate(self):
                self.returncode = 0

            def kill(self):
                self.returncode = -9

            async def wait(self):
                self.returncode = 0
                return 0

        process_calls = []

        async def _fake_open_process(argv, **kwargs):
            process_calls.append((list(argv), kwargs))
            return _FakeProcess(version_probe=argv[-1] == "-v")

        monkeypatch.setattr(subprocess_cli.anyio, "open_process", _fake_open_process)
        session = ClaudeAgentSdkSession(cwd="/tmp", include_hermes_tools=False)
        client = session._build_client()
        transport = client._custom_transport
        transport._cli_path = "/fake/claude"

        async def _connect_and_close():
            await transport.connect()
            await transport.close()

        asyncio.run(_connect_and_close())

        assert len(process_calls) == 2
        version_call = next(call for call in process_calls if call[0][-1] == "-v")
        streaming_call = next(call for call in process_calls if call[0][-1] != "-v")
        for argv, kwargs in (version_call, streaming_call):
            assert kwargs.get("env") is not None, f"missing explicit env for {argv}"
            child_env = kwargs["env"]
            assert child_env["HOME"] == "/tmp/sdk-home"
            assert child_env["PATH"] == "/usr/local/bin:/usr/bin"
            assert all(child_env[name] == "" for name in blocked)
            assert sentinel not in child_env.values()
        assert "Minimum required version is 2.0.0" in caplog.text

    def test_session_module_import_stays_lazy_without_optional_sdk(self):
        import subprocess
        import sys
        import textwrap
        from pathlib import Path

        script = textwrap.dedent(
            """
            import builtins
            original_import = builtins.__import__

            def blocked_import(name, *args, **kwargs):
                if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
                    raise ImportError("optional SDK blocked by test")
                return original_import(name, *args, **kwargs)

            builtins.__import__ = blocked_import
            import agent.transports.claude_agent_sdk_session
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    def test_mcp_env_carries_no_secrets(self, monkeypatch):
        # The SDK inlines the MCP config, including its env, into the Claude
        # CLI argv where it is visible via ps. The
        # env must be a minimal allowlist, never the credentialed environ.
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake")
        # ANTHROPIC_AUTH_TOKEN is deliberately absent here because the
        # billing-route guard would refuse startup before the MCP config is
        # built. The allowlist excludes it regardless.
        monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-test-home")
        session = ClaudeAgentSdkSession(
            cwd="/tmp", hermes_session_id="sess-9"
        )
        env = session.build_option_fields()["mcp_servers"]["hermes-tools"]["env"]
        for secret in ("CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY",
                       "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            assert secret not in env, f"{secret} leaked into the MCP argv env"
        assert "PYTHONPATH" in env
        assert env["HERMES_SESSION_ID"] == "sess-9"
        assert env["HERMES_HOME"] == "/tmp/hermes-test-home"

    def test_state_db_override_rides_the_mcp_env(self, monkeypatch):
        # The MCP environment allowlist must retain HERMES_MCP_STATE_DB;
        # otherwise the documented state-DB override is lost and the MCP
        # subprocess searched the DEFAULT DB with no error. A path, not a
        # secret, so it belongs on the allowlist.
        monkeypatch.setenv("HERMES_MCP_STATE_DB", "/tmp/custom-state.db")
        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        env = holder["client"].options["mcp_servers"]["hermes-tools"]["env"]
        assert env["HERMES_MCP_STATE_DB"] == "/tmp/custom-state.db"

    def test_anthropic_auth_token_refuses_startup(self, monkeypatch):
        # The CLI also honors ANTHROPIC_AUTH_TOKEN (bearer,
        # typically metered/proxy) — same fail-closed class as the API key.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-bearer")
        session = ClaudeAgentSdkSession(cwd="/tmp")  # no factory → real path
        turn = session.run_turn("hi")
        assert turn.should_retire
        assert "ANTHROPIC_AUTH_TOKEN" in (turn.error or "")

    def test_legacy_allow_metered_key_config_cannot_bypass_guard(self, monkeypatch):
        # Old configs may still contain the removed key. It must be ignored:
        # this runtime has no metered escape hatch.
        import hermes_cli.config as cfg

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-fake")
        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *a, **k: {
                "agent": {"claude_agent_sdk": {"allow_metered_key": True}}
            },
        )
        session, _holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            turn = session.run_turn("ping")
        finally:
            session.close()
        assert turn.should_retire
        assert "ANTHROPIC_API_KEY" in (turn.error or "")

    def test_half_connected_client_is_reaped_on_close(self):
        # On a connect failure, assigning the client only after connect()
        # returned made close() skip disconnect and left the CLI subprocess
        # orphaned.
        session, holder = _make_session(connect_exc=RuntimeError("connect blew up"))
        turn = session.run_turn("hi")
        assert turn.should_retire
        session.close()
        assert holder["client"].disconnected is True

    def test_mid_stream_interrupt_breaks_and_discards_tail(self):
        # Exercise /stop arriving during streaming at the session boundary.
        holder = {}

        class MidStreamClient(_FakeClient):
            async def receive_response(self):
                yield AssistantMessage(content=[TextBlock("first chunk")])
                holder["session"]._interrupt_event.set()
                yield AssistantMessage(content=[TextBlock("tail that must be discarded")])
                yield ResultMessage(result="tail that must be discarded")

        def factory(options=None):
            client = MidStreamClient(options=options)
            holder["client"] = client
            return client

        session = ClaudeAgentSdkSession(cwd="/tmp", client_factory=factory)
        holder["session"] = session
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()
        assert turn.interrupted is True
        assert all("discarded" not in str(m.get("content")) for m in turn.projected_messages)


class TestHermesSessionIdPlumbing:
    def test_session_id_rides_mcp_env(self):
        session, holder = _make_session(
            script=[ResultMessage(result="ok")], hermes_session_id="sess-42"
        )
        try:
            session.run_turn("ping")
        finally:
            session.close()
        env = holder["client"].options["mcp_servers"]["hermes-tools"]["env"]
        assert env["HERMES_SESSION_ID"] == "sess-42"
        # The invented pre-fix name must never come back: the shim consumer
        # reads only the canonical HERMES_SESSION_ID.
        assert "HERMES_MCP_SESSION_ID" not in env

    def test_no_session_id_no_env_var(self):
        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        env = holder["client"].options["mcp_servers"]["hermes-tools"]["env"]
        assert "HERMES_SESSION_ID" not in env
        assert "HERMES_MCP_SESSION_ID" not in env

    def test_runtime_passes_agent_session_id(self, monkeypatch):
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        captured = {}

        class SpySession:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run_turn(self, user_input):
                return _make_turn()

        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        agent = _make_agent()
        agent._claude_sdk_session = None
        run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )
        assert captured.get("hermes_session_id") == "sess-1"

    def test_runtime_passes_context_to_append_builder(self, monkeypatch):
        # The append builder receives the agent's platform/session/model plus
        # the resolved runtime cwd so native project context is loaded from the
        # same workspace the SDK subprocess operates in.
        import agent.claude_sdk_runtime as rt
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        captured = {}

        def fake_append(**kwargs):
            captured.update(kwargs)
            return "APPEND-UNDER-TEST"

        class SpySession:
            def __init__(self, **kwargs):
                pass

            def run_turn(self, user_input):
                return _make_turn()

        monkeypatch.setattr(rt, "build_system_prompt_append", fake_append)
        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        agent = _make_agent()
        agent._claude_sdk_session = None
        agent.platform = "telegram"
        agent.session_cwd = "/tmp/sdk-runtime-workspace"
        run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )
        assert captured == {
            "platform": "telegram",
            "session_id": "sess-1",
            "model": "claude-opus-4-8",
            "cwd": "/tmp/sdk-runtime-workspace",
            "context_length": None,
            "native_read_only": False,
        }


# ---------- interrupt routing to the SDK session ----------


class TestInterruptRoutesToSdkSession:
    """/stop and new-message preemption call AIAgent.interrupt(); the SDK
    session's request_interrupt (event + client.interrupt()) already works —
    this pins the one missing caller."""

    @staticmethod
    def _make_real_agent():
        from run_agent import AIAgent

        return AIAgent(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    def test_interrupt_reaches_live_sdk_session(self):
        agent = self._make_real_agent()
        agent._claude_sdk_session = MagicMock()
        agent.interrupt()
        agent._claude_sdk_session.request_interrupt.assert_called_once()

    def test_interrupt_without_sdk_session_stays_safe(self):
        agent = self._make_real_agent()
        agent._claude_sdk_session = None
        agent.interrupt()  # must not raise

    def test_release_clients_disconnects_sdk_session(self):
        # The gateway's routine evictions (LRU cap, idle-TTL sweep, model
        # switch) release via release_clients(), which previously
        # never touched the SDK session — leaking the loop thread + the
        # Claude CLI subprocess per eviction on a 24/7 gateway.
        agent = self._make_real_agent()
        sdk_session = MagicMock()
        agent._claude_sdk_session = sdk_session
        agent.release_clients()
        sdk_session.close.assert_called_once()
        assert agent._claude_sdk_session is None

    def test_pending_interrupt_flag_short_circuits_cold_turn(self, monkeypatch):
        # An interrupt landing before the SDK session exists sets only
        # agent._interrupt_requested, which the SDK
        # path never read — the turn ran uninterruptible for up to 600s.
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        instances = []

        class SpySession:
            def __init__(self, **kwargs):
                instances.append(self)

            def run_turn(self, user_input):
                return _make_turn()

        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        agent = _make_agent()
        agent._claude_sdk_session = None
        agent._interrupt_requested = True
        result = run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        assert instances == []  # no session created, no subscription burn
        assert result["completed"] is False and result["partial"] is True
        assert agent._interrupt_requested is False  # consumed, next turn runs

    def test_honored_interrupt_consumes_agent_flag(self, monkeypatch):
        # After an interrupt is honored mid-turn, the agent-level flag must not
        # remain set or the cold-flag check will short-circuit
        # the NEXT turn into an empty answer. Honoring must consume it.
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        agent = _make_agent()
        agent._claude_sdk_session = None
        agent._session_db = None

        class SpySession:
            def __init__(self, **kwargs):
                pass

            def run_turn(self, user_input):
                agent._interrupt_requested = True  # user hit /stop mid-turn
                return _make_turn(interrupted=True, final_text="", projected_messages=[])

            def close(self):
                pass

        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        result = run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        assert result["partial"] is True
        assert agent._interrupt_requested is False  # consumed — next turn runs

    def test_thread_id_captured_from_init_message(self):
        # A FIRST-turn interrupt used to lose the resume id (only the final
        # ResultMessage carried it). The SDK announces session_id in its init
        # SystemMessage — capture it from any message.
        session, _ = _make_session(script=[SystemMessage(session_id="sdk-early-7")])
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()
        assert turn.thread_id == "sdk-early-7"

    def test_pre_set_interrupt_event_honored_then_next_turn_runs(self):
        # Clearing the interrupt event after connect erases an interrupt that
        # arrives during the up-to-60-second connect window. It must instead be
        # honored by THIS turn, and must not bleed into the next one.
        session, holder = _make_session(
            script=[ResultMessage(result="ok")]
        )
        try:
            session.ensure_started()
            session.request_interrupt()
            turn1 = session.run_turn("first")
            assert turn1.interrupted is True
            assert holder["client"].queried == []  # never reached the model
            turn2 = session.run_turn("second")
            assert turn2.interrupted is False
            assert holder["client"].queried == ["second"]
        finally:
            session.close()


# ---------- streaming deltas (config-gated, default off) ----------


class TestStreaming:
    def test_env_var_cannot_enable_streaming(self, monkeypatch):
        # AGENTS.md:102-107 keeps behavioural settings out of HERMES_* env
        # vars. The old HERMES_CLAUDE_SDK_STREAMING override is gone, so
        # setting it must have NO effect — config.yaml is the only interface.
        monkeypatch.setenv("HERMES_CLAUDE_SDK_STREAMING", "1")
        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        assert "include_partial_messages" not in holder["client"].options

    def test_option_absent_by_default(self):
        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        assert "include_partial_messages" not in holder["client"].options

    def test_config_yaml_is_the_operator_interface(self, monkeypatch):
        # AGENTS.md: behavioral settings live in config.yaml, not env.
        # agent.claude_agent_sdk.streaming turns the option on without any env.
        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *a, **k: {"agent": {"claude_agent_sdk": {"streaming": True}}},
        )
        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        assert holder["client"].options["include_partial_messages"] is True

    def test_env_var_cannot_disable_config_streaming(self, monkeypatch):
        # The mirror of the test above: an explicit env "0" must NOT be able to
        # veto config.yaml either. Together the pair pins the override as fully
        # inert in both directions, so it cannot creep back in unnoticed.
        import hermes_cli.config as cfg

        monkeypatch.setenv("HERMES_CLAUDE_SDK_STREAMING", "0")
        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *a, **k: {"agent": {"claude_agent_sdk": {"streaming": True}}},
        )
        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        assert holder["client"].options["include_partial_messages"] is True

    def test_deltas_reach_callback_and_never_the_transcript(self):
        got = []
        script = [
            _text_delta_event("Hel"),
            _text_delta_event("lo"),
            AssistantMessage(content=[TextBlock("Hello")]),
            ResultMessage(result="Hello"),
        ]
        session, _ = _make_session(script=script, on_stream_delta=got.append)
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()
        assert got == ["Hel", "lo"]
        # Display-only: deltas never become transcript rows.
        assert [m["role"] for m in turn.projected_messages] == ["assistant"]
        assert turn.final_text == "Hello"

    def test_subagent_deltas_are_not_forwarded(self):
        got = []
        script = [
            _text_delta_event("sub", parent_tool_use_id="tool-1"),
            ResultMessage(result="done"),
        ]
        session, _ = _make_session(script=script, on_stream_delta=got.append)
        try:
            session.run_turn("hi")
        finally:
            session.close()
        assert got == []

    def test_runtime_wires_late_bound_stream_callback(self, monkeypatch):
        # The gateway assigns agent.stream_delta_callback per turn AFTER the
        # session exists — the wiring must read it at call time.
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        captured = {}

        class SpySession:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run_turn(self, user_input):
                return _make_turn()

        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        agent = _make_agent()
        agent._claude_sdk_session = None
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        relay = captured.get("on_stream_delta")
        assert callable(relay)
        seen = []
        agent.stream_delta_callback = seen.append  # assigned AFTER creation
        relay("delta-text")
        assert seen == ["delta-text"]
        agent.stream_delta_callback = None  # cleared between turns → no crash
        relay("dropped")
        assert seen == ["delta-text"]


# ---------- continuity: resume + digest fallback ----------


class TestContinuity:
    """Retire matrix under test:
      /new, expiry      → new Hermes session row → no persisted id → FRESH
      restart/eviction  → same row, id persisted → RESUME
      error retire      → persisted id CLEARED → next turn fresh + digest
      stale resume      → retire → clear → ONE fresh retry with digest
    """

    @staticmethod
    def _db_agent(persisted_sdk_id=None):
        agent = _make_agent()
        agent._claude_sdk_session = None
        db = MagicMock()
        db.get_session.return_value = {"claude_sdk_session_id": persisted_sdk_id}
        agent._session_db = db
        agent._session_db_created = True
        return agent, db

    @staticmethod
    def _spy_sessions(monkeypatch, behaviors):
        """Install a SpySession whose Nth instance behaves per behaviors[N]:
        a TurnResult-like object to return, or an Exception to raise."""
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        instances = []

        class SpySession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.inputs = []
                instances.append(self)

            def run_turn(self, user_input):
                self.inputs.append(user_input)
                behavior = behaviors[len(instances) - 1]
                if isinstance(behavior, Exception):
                    raise behavior
                return behavior

            def close(self):
                pass

        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        return instances

    def test_creation_resumes_from_persisted_id(self, monkeypatch):
        agent, _db = self._db_agent(persisted_sdk_id="sdk-old-1")
        instances = self._spy_sessions(monkeypatch, [_make_turn()])
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        assert instances[0].kwargs.get("resume_session_id") == "sdk-old-1"
        # A resumed session already holds the context — no digest.
        assert instances[0].inputs == ["hi"]

    def test_successful_turn_persists_thread_id(self, monkeypatch):
        agent, db = self._db_agent()
        self._spy_sessions(monkeypatch, [_make_turn(thread_id="sdk-new-9")])
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        db.update_claude_sdk_session_id.assert_called_with("sess-1", "sdk-new-9")

    def test_error_retire_clears_persisted_id(self, monkeypatch):
        agent, db = self._db_agent()
        self._spy_sessions(monkeypatch, [_make_turn(
            should_retire=True, error="turn timed out", projected_messages=[],
            final_text="", token_usage_last=None,
        )])
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        db.update_claude_sdk_session_id.assert_called_with("sess-1", None)

    def test_digest_prepended_on_fresh_session_with_history(self, monkeypatch):
        agent, _db = self._db_agent(persisted_sdk_id=None)
        instances = self._spy_sessions(monkeypatch, [_make_turn()])
        messages = [
            {"role": "user", "content": "the linter flags shadowed imports"},
            {"role": "assistant", "content": "Fixed by renaming the local."},
            {"role": "user", "content": "and the tests?"},
        ]
        run_claude_agent_sdk_turn(
            agent, user_message="and the tests?", original_user_message="and the tests?",
            messages=messages, effective_task_id="t",
        )
        sent = instances[0].inputs[0]
        assert sent.startswith("[Continuity digest")
        assert "shadowed imports" in sent
        assert sent.endswith("and the tests?")

    def test_no_digest_on_brand_new_conversation(self, monkeypatch):
        agent, _db = self._db_agent(persisted_sdk_id=None)
        instances = self._spy_sessions(monkeypatch, [_make_turn()])
        run_claude_agent_sdk_turn(
            agent, user_message="hello", original_user_message="hello",
            messages=[{"role": "user", "content": "hello"}], effective_task_id="t",
        )
        assert instances[0].inputs == ["hello"]

    def test_stale_resume_retires_then_retries_fresh_with_digest(self, monkeypatch):
        # A stale resume id fails the session. The runtime
        # must clear the id and retry ONCE fresh (digest included) — the
        # user gets an answer, not an error.
        agent, db = self._db_agent(persisted_sdk_id="sdk-stale-7")
        instances = self._spy_sessions(monkeypatch, [
            _make_turn(should_retire=True, error="resume failed",
                       projected_messages=[], final_text="", tool_iterations=0,
                       token_usage_last=None, retry_safe_before_query=True),
            _make_turn(final_text="fresh answer",
                       projected_messages=[{"role": "assistant", "content": "fresh answer"}]),
        ])
        messages = [
            {"role": "user", "content": "earlier context line"},
            {"role": "assistant", "content": "earlier reply"},
            {"role": "user", "content": "current question"},
        ]
        result = run_claude_agent_sdk_turn(
            agent, user_message="current question",
            original_user_message="current question",
            messages=messages, effective_task_id="t",
        )
        assert result["final_response"] == "fresh answer"
        assert len(instances) == 2
        assert instances[0].kwargs.get("resume_session_id") == "sdk-stale-7"
        assert instances[1].kwargs.get("resume_session_id") is None
        assert instances[1].inputs[0].startswith("[Continuity digest")
        db.update_claude_sdk_session_id.assert_any_call("sess-1", None)

    def test_resumed_execution_error_without_projection_is_never_replayed(
        self, monkeypatch
    ):
        agent, db = self._db_agent(persisted_sdk_id="sdk-live-7")
        failed_turn = _make_turn(
            should_retire=True,
            error=(
                "SDK result error (subtype=error_during_execution): "
                "error_during_execution"
            ),
            projected_messages=[],
            final_text="",
            tool_iterations=0,
            token_usage_last=None,
        )
        instances = self._spy_sessions(
            monkeypatch,
            [failed_turn, _make_turn(final_text="must not replay")],
        )

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="perform one action",
            original_user_message="perform one action",
            messages=[{"role": "user", "content": "perform one action"}],
            effective_task_id="t",
        )

        assert len(instances) == 1
        assert sum(len(instance.inputs) for instance in instances) == 1
        assert result["error"] == failed_turn.error
        db.update_claude_sdk_session_id.assert_called_with("sess-1", None)

    @pytest.mark.parametrize(
        "failed_behavior",
        [
            pytest.param(RuntimeError("generic adapter failure"), id="generic-exception"),
            pytest.param(
                _make_turn(
                    should_retire=True,
                    error="turn timed out after 600s",
                    projected_messages=[],
                    final_text="",
                    tool_iterations=0,
                    token_usage_last=None,
                ),
                id="timeout-result",
            ),
        ],
    )
    def test_resumed_generic_and_timeout_failures_are_never_replayed(
        self, monkeypatch, failed_behavior
    ):
        agent, db = self._db_agent(persisted_sdk_id="sdk-live-8")
        instances = self._spy_sessions(
            monkeypatch,
            [failed_behavior, _make_turn(final_text="must not replay")],
        )

        run_claude_agent_sdk_turn(
            agent,
            user_message="perform one action",
            original_user_message="perform one action",
            messages=[{"role": "user", "content": "perform one action"}],
            effective_task_id="t",
        )

        assert len(instances) == 1
        assert sum(len(instance.inputs) for instance in instances) == 1
        db.update_claude_sdk_session_id.assert_called_with("sess-1", None)

    def test_fresh_continuity_rejects_image_before_session_creation(
        self, monkeypatch
    ):
        agent, _db = self._db_agent(persisted_sdk_id=None)
        instances = self._spy_sessions(monkeypatch, [])
        rich_input = [
            {"type": "text", "text": "inspect this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,IMAGE-SENTINEL"},
            },
        ]
        messages = [
            {"role": "user", "content": "earlier context"},
            {"role": "assistant", "content": "earlier reply"},
            {"role": "user", "content": rich_input},
        ]

        result = run_claude_agent_sdk_turn(
            agent,
            user_message=rich_input,
            original_user_message=rich_input,
            messages=messages,
            effective_task_id="t",
        )

        assert result["error"] == (
            "claude-agent-sdk image inputs are unsupported until rich-image "
            "transport is implemented"
        )
        assert result["api_calls"] == 0
        assert instances == []
        assert "IMAGE-SENTINEL" not in result["final_response"]

    def test_fresh_continuity_rejects_image_history_before_digest(
        self, monkeypatch
    ):
        agent, _db = self._db_agent(persisted_sdk_id=None)
        instances = self._spy_sessions(monkeypatch, [])
        messages = [
            {
                "role": "user",
                "content": {
                    "envelope": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,HISTORY-SENTINEL"
                            },
                        }
                    ]
                },
            },
            {"role": "assistant", "content": "earlier reply"},
            {"role": "user", "content": "current question"},
        ]

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="current question",
            original_user_message="current question",
            messages=messages,
            effective_task_id="t",
        )

        assert result["error"] == (
            "claude-agent-sdk image inputs are unsupported until rich-image "
            "transport is implemented"
        )
        assert result["api_calls"] == 0
        assert instances == []
        assert "HISTORY-SENTINEL" not in result["final_response"]

    @pytest.mark.parametrize(
        "failed_turn",
        [
            pytest.param(
                _make_turn(
                    should_retire=True,
                    error="resume failed after projection",
                    projected_messages=[
                        {"role": "assistant", "content": "partial response"}
                    ],
                    final_text="partial response",
                    tool_iterations=0,
                    token_usage_last=None,
                ),
                id="projected-message",
            ),
            pytest.param(
                _make_turn(
                    should_retire=True,
                    error="resume failed after tool iteration",
                    projected_messages=[],
                    final_text="",
                    tool_iterations=1,
                    token_usage_last=None,
                ),
                id="tool-iteration",
            ),
        ],
    )
    def test_resumed_mid_turn_failure_is_never_replayed(
        self, monkeypatch, failed_turn
    ):
        agent, db = self._db_agent(persisted_sdk_id="sdk-live-7")
        instances = self._spy_sessions(
            monkeypatch,
            [failed_turn, _make_turn(final_text="must not replay")],
        )

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="perform one action",
            original_user_message="perform one action",
            messages=[{"role": "user", "content": "perform one action"}],
            effective_task_id="t",
        )

        assert len(instances) == 1
        assert result["error"] == failed_turn.error
        db.update_claude_sdk_session_id.assert_called_with("sess-1", None)

    def test_cold_short_circuit_consumes_live_session_event_too(self, monkeypatch):
        # An interrupt racing turn completion sets both the
        # agent flag and the live session's event. The short-circuit consumed
        # only the flag — the NEXT legit message then died on the stale
        # session event with no model call. Honoring must consume both.
        agent, _db = self._db_agent()
        live = MagicMock()
        agent._claude_sdk_session = live
        agent._interrupt_requested = True
        result = run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        assert result["partial"] is True
        live.consume_interrupt.assert_called_once()
        live.run_turn.assert_not_called()

    def test_resume_id_persisted_after_flush_and_gated_on_persist_disabled(self, monkeypatch):
        # Writing the resume id before the flush that (re)creates the session
        # row after a transient turn-start lock can silently discard
        # continuity. Order must be flush-then-store.
        agent, db = self._db_agent()
        order = []
        agent._flush_messages_to_session_db = MagicMock(
            side_effect=lambda *a, **k: order.append("flush"))
        db.update_claude_sdk_session_id.side_effect = (
            lambda *a, **k: order.append("store"))
        self._spy_sessions(monkeypatch, [_make_turn(thread_id="sdk-z-1")])
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        assert "store" in order and "flush" in order
        assert order.index("flush") < order.index("store")
        # And a fork with persistence disabled must never touch the parent row.
        agent2, db2 = self._db_agent(persisted_sdk_id="sdk-parent-1")
        agent2._persist_disabled = True
        self._spy_sessions(monkeypatch, [_make_turn(thread_id="sdk-fork-9")])
        run_claude_agent_sdk_turn(
            agent2, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        db2.update_claude_sdk_session_id.assert_not_called()

    def test_interrupted_turn_retires_client_but_persists_resume_id(self, monkeypatch):
        # Breaking out of receive_response() on interrupt leaves the
        # interrupted turn's ResultMessage queued in the
        # client's stream — a REUSED client would serve it as the NEXT turn's
        # answer. The runtime must retire the client (clean stream) while
        # persisting the SDK id, so the next turn RESUMES the conversation.
        agent, db = self._db_agent()
        self._spy_sessions(monkeypatch, [_make_turn(
            interrupted=True, final_text="partial answer", thread_id="sdk-live-3",
        )])
        result = run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        assert agent._claude_sdk_session is None  # client retired
        db.update_claude_sdk_session_id.assert_called_with("sess-1", "sdk-live-3")
        assert result["partial"] is True

    def test_fresh_retire_does_not_retry(self, monkeypatch):
        # Only a RESUMED session earns the retry — a fresh session that
        # retires is a real error and must surface, never loop.
        agent, _db = self._db_agent(persisted_sdk_id=None)
        instances = self._spy_sessions(monkeypatch, [_make_turn(
            should_retire=True, error="boom", projected_messages=[],
            final_text="", token_usage_last=None,
        )])
        result = run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        assert len(instances) == 1
        assert result["partial"] is True

    def test_effective_prompt_snapshot_replaces_native_one(self, monkeypatch):
        # The prologue persists Hermes' native composed prompt — a prompt
        # this runtime never sends. The runtime overwrites the snapshot with
        # the EFFECTIVE prompt so the audit trail tells the truth.
        agent, db = self._db_agent()
        self._spy_sessions(monkeypatch, [_make_turn()])
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        args = db.update_system_prompt.call_args
        assert args is not None
        assert args.args[0] == "sess-1"
        assert args.args[1].startswith("[claude_code preset]")


class TestSessionResumeField:
    def test_resume_rides_options_when_set(self):
        session, holder = _make_session(
            script=[ResultMessage(result="ok")], resume_session_id="sdk-abc"
        )
        try:
            session.run_turn("ping")
        finally:
            session.close()
        assert holder["client"].options["resume"] == "sdk-abc"

    def test_no_resume_field_when_unset(self):
        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        assert "resume" not in holder["client"].options


# ---------- agent close() releases the SDK session ----------


class TestAgentCloseClosesSdkSession:
    """AIAgent.close() runs on /new, session expiry, and agent-cache
    eviction. Without an explicit disconnect the SDK client (and its CLI
    subprocess) is dropped to GC — a leak. (#25267)"""

    @staticmethod
    def _make_real_agent():
        from run_agent import AIAgent

        return AIAgent(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    def test_close_disconnects_claude_sdk_session(self):
        agent = self._make_real_agent()
        sdk_session = MagicMock()
        agent._claude_sdk_session = sdk_session
        agent.close()
        sdk_session.close.assert_called_once()
        assert agent._claude_sdk_session is None

    def test_close_without_sdk_session_stays_safe(self):
        # Negative control: an agent that never created an SDK session (or
        # already closed it) must close without raising — idempotency.
        agent = self._make_real_agent()
        agent.close()
        agent._claude_sdk_session = None
        agent.close()


# ---------- provider wiring ----------


class TestProviderWiring:
    def test_profile_registered_with_aliases(self):
        from providers import get_provider_profile

        profile = get_provider_profile("claude-agent-sdk")
        assert profile is not None
        assert profile.api_mode == "claude_agent_sdk"
        assert profile.auth_type == "oauth_external"
        assert profile.env_vars == ()
        assert get_provider_profile("claude-sdk") is profile
        # The anthropic profile keeps its own alias namespace untouched.
        anthropic = get_provider_profile("claude")
        assert anthropic is not None and anthropic.name == "anthropic"

    def test_runtime_resolution_short_circuit(self):
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="claude-agent-sdk")
        assert runtime["provider"] == "claude-agent-sdk"
        assert runtime["api_mode"] == "claude_agent_sdk"
        # No credential-pool machinery, no metered key.
        assert runtime["api_key"] == "claude-subscription-oauth"

    def test_api_mode_accepted_by_agent_init(self):
        from hermes_cli.runtime_provider import _parse_api_mode

        assert _parse_api_mode("claude_agent_sdk") == "claude_agent_sdk"


class TestSystemPromptAppend:
    # The append is composed from Hermes' native
    # builders — memory gauge via MemoryStore.format_for_system_prompt,
    # guidance constants from agent.prompt_builder, the skills index via
    # build_skills_system_prompt — never re-implemented formats. Guidance
    # appears only for tools that are actually callable through the MCP
    # shims.

    @staticmethod
    def _home(tmp_path, monkeypatch, *, soul=None, memory=None, user=None):
        hermes_home = tmp_path / "hermes"
        memories = hermes_home / "memories"
        memories.mkdir(parents=True)
        if memory is not None:
            (memories / "MEMORY.md").write_text(memory)
        if user is not None:
            (memories / "USER.md").write_text(user)
        import hermes_cli.config as cfg

        append_file = ""
        if soul is not None:
            soul_file = tmp_path / "SOUL.md"
            soul_file.write_text(soul)
            append_file = str(soul_file)
        # config.yaml is the only interface for the persona file
        # (agent.claude_agent_sdk.append_file); the old env var is gone.
        # Patching unconditionally also isolates the suite from a developer's
        # real config.yaml, which would otherwise leak a live append_file in.
        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *a, **k: {
                "agent": {"claude_agent_sdk": {"append_file": append_file}}
            },
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        return hermes_home

    def test_soul_first_and_user_content_present(self, tmp_path, monkeypatch):
        from agent.claude_sdk_runtime import build_system_prompt_append

        self._home(
            tmp_path, monkeypatch,
            soul="# I am the persona under test",
            user="The user prefers concise results",
        )
        out = build_system_prompt_append()
        assert out is not None
        assert out.startswith("# I am the persona under test")
        assert "The user prefers concise results" in out

    def test_active_hermes_soul_is_default_identity(self, tmp_path, monkeypatch):
        from agent.claude_sdk_runtime import build_system_prompt_append

        home = self._home(tmp_path, monkeypatch)
        (home / "SOUL.md").write_text("# Active Hermes identity")

        out = build_system_prompt_append()

        assert out is not None
        assert out.startswith("# Active Hermes identity")

    def test_cwd_agents_content_appears_after_single_active_soul(
        self, tmp_path, monkeypatch
    ):
        from agent.claude_sdk_runtime import build_system_prompt_append

        home = self._home(tmp_path, monkeypatch)
        (home / "SOUL.md").write_text("# Active Hermes identity")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "AGENTS.md").write_text(
            "# Workspace contract\nUNIQUE_SDK_WORKSPACE_RULE"
        )

        out = build_system_prompt_append(cwd=str(workspace))

        assert out is not None
        assert out.startswith("# Active Hermes identity")
        assert out.count("# Active Hermes identity") == 1
        assert "## AGENTS.md" in out
        assert "UNIQUE_SDK_WORKSPACE_RULE" in out
        assert out.index("# Project Context") > out.index("# Active Hermes identity")

    def test_native_context_builder_owns_priority_and_scanning(
        self, tmp_path, monkeypatch
    ):
        import agent.prompt_builder as prompt_builder
        from agent.claude_sdk_runtime import build_system_prompt_append

        self._home(tmp_path, monkeypatch, soul="# SDK identity")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        captured = {}

        def fake_context_builder(**kwargs):
            captured.update(kwargs)
            return "# Project Context\n\nNATIVE_PRIORITY_AND_SCAN_SENTINEL"

        monkeypatch.setattr(
            prompt_builder, "build_context_files_prompt", fake_context_builder
        )

        out = build_system_prompt_append(
            cwd=str(workspace), context_length=123_456
        )

        assert captured == {
            "cwd": str(workspace),
            "skip_soul": True,
            "context_length": 123_456,
        }
        assert "NATIVE_PRIORITY_AND_SCAN_SENTINEL" in (out or "")

    def test_native_agents_priority_beats_lower_priority_claude_file(
        self, tmp_path, monkeypatch
    ):
        from agent.claude_sdk_runtime import build_system_prompt_append

        self._home(tmp_path, monkeypatch, soul="# SDK identity")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "AGENTS.md").write_text("AGENTS_PRIORITY_SENTINEL")
        (workspace / "CLAUDE.md").write_text("CLAUDE_LOWER_PRIORITY_SENTINEL")

        out = build_system_prompt_append(cwd=str(workspace)) or ""

        assert "AGENTS_PRIORITY_SENTINEL" in out
        assert "CLAUDE_LOWER_PRIORITY_SENTINEL" not in out

    def test_normal_twenty_k_agents_block_fits_with_active_soul(
        self, tmp_path, monkeypatch
    ):
        from agent.claude_sdk_runtime import build_system_prompt_append

        home = self._home(tmp_path, monkeypatch)
        (home / "SOUL.md").write_text("# Active identity\n" + ("s" * 7_500))
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        agents_body = "# Workspace rules\n" + ("a" * 19_000) + "\nAGENTS_TAIL_SENTINEL"
        (workspace / "AGENTS.md").write_text(agents_body)

        out = build_system_prompt_append(cwd=str(workspace)) or ""

        assert out.startswith("# Active identity")
        assert "## AGENTS.md" in out
        assert "AGENTS_TAIL_SENTINEL" in out

    def test_gauge_blocks_are_the_native_render(self, tmp_path, monkeypatch):
        # Byte-pin: the memory/user blocks are EXACTLY what the native
        # composer injects (MemoryStore.format_for_system_prompt output,
        # gauge header included) — never a re-implementation.
        from agent.claude_sdk_runtime import build_system_prompt_append
        from tools.memory_tool import load_on_disk_store

        self._home(
            tmp_path, monkeypatch,
            memory="ci runs on the drone server",
            user="prefers squash merges",
        )
        store = load_on_disk_store()
        expected_memory = store.format_for_system_prompt("memory")
        expected_user = store.format_for_system_prompt("user")
        assert "MEMORY (your personal notes) [" in expected_memory  # sanity
        assert "USER PROFILE (who the user is) [" in expected_user

        out = build_system_prompt_append()
        assert expected_memory in out
        assert expected_user in out

    def test_memory_guidance_present_skill_sentence_stripped(self, tmp_path, monkeypatch):
        # MEMORY_GUIDANCE ships verbatim EXCEPT its one sentence instructing
        # the skill tool because skill_manage is not exposed. Guidance is only
        # included for callable tools. The strip must be a pure
        # deletion of a sentence that actually exists in the native constant
        # — if upstream rewords it, this test goes red and we re-derive.
        from agent.claude_sdk_runtime import (
            _strip_uncallable_tool_guidance,
            build_system_prompt_append,
        )
        from agent.prompt_builder import MEMORY_GUIDANCE

        self._home(tmp_path, monkeypatch, memory="uses trunk-based development")
        stripped = _strip_uncallable_tool_guidance(MEMORY_GUIDANCE)
        assert stripped != MEMORY_GUIDANCE, "skill sentence not found — upstream reworded it"
        assert "save it as a skill with the skill tool" not in stripped

        out = build_system_prompt_append()
        assert "You have persistent memory across sessions" in out
        assert stripped in out
        assert "save it as a skill with the skill tool" not in out
        # The claude_code preset has its own file-based memory convention, so
        # the append must identify the
        # hermes-tools memory tool as the ONLY durable store.
        assert "ONLY durable memory" in out
        assert "hermes-tools MCP server" in out
        # The preset's memory directory persists per cwd, so the addendum must
        # describe it as
        # (unmanaged/disposable), never the false "will not be injected".
        assert "disposable" in out
        assert "will not be injected" not in out

    def test_skills_guidance_never_injected(self, tmp_path, monkeypatch):
        # SKILLS_GUIDANCE instructs skill_manage — unexposed by design.
        from agent.claude_sdk_runtime import build_system_prompt_append

        self._home(tmp_path, monkeypatch, memory="a fact")
        out = build_system_prompt_append()
        assert "skill_manage" not in out

    def test_session_search_guidance_always_present(self, tmp_path, monkeypatch):
        from agent.claude_sdk_runtime import build_system_prompt_append
        from agent.prompt_builder import SESSION_SEARCH_GUIDANCE

        self._home(tmp_path, monkeypatch)  # no memory files at all
        out = build_system_prompt_append()
        assert out is not None
        assert SESSION_SEARCH_GUIDANCE in out
        # Multi-term FTS queries use AND and can miss relevant sessions.
        assert "ALL terms must match" in out

    def test_native_read_only_append_omits_absent_tool_guidance(
        self, tmp_path, monkeypatch
    ):
        import agent.prompt_builder as prompt_builder
        from agent.claude_sdk_runtime import build_system_prompt_append

        self._home(
            tmp_path,
            monkeypatch,
            soul="# Read-only analyst",
            memory="a read-only injected fact",
        )

        def unexpected_skills_index(**kwargs):
            pytest.fail(f"native-read-only built a skills index: {kwargs}")

        monkeypatch.setattr(
            prompt_builder, "build_skills_system_prompt", unexpected_skills_index
        )
        out = build_system_prompt_append(native_read_only=True) or ""

        assert out.startswith("# Read-only analyst")
        assert "a read-only injected fact" in out
        for absent_tool_advertisement in (
            "hermes-tools MCP server",
            "session_search",
            "skill_view",
            "skills_list",
            "browser_navigate",
            "memory tool",
        ):
            assert absent_tool_advertisement not in out

    def test_memory_disabled_removes_blocks_and_guidance(self, tmp_path, monkeypatch):
        from agent.claude_sdk_runtime import build_system_prompt_append
        import hermes_cli.config as cfg

        self._home(tmp_path, monkeypatch, memory="should not appear")
        monkeypatch.setattr(
            cfg, "load_config", lambda *a, **k: {"memory": {"memory_enabled": False}}
        )
        out = build_system_prompt_append()
        assert "should not appear" not in (out or "")
        assert "You have persistent memory" not in (out or "")
        # session_search still works when memory is off — its guidance stays.
        assert "session_search" in (out or "")

    def test_external_memory_provider_removes_tool_guidance(self, tmp_path, monkeypatch):
        # memory.provider: honcho (or ANY external backend) leaves the memory
        # shim UNREGISTERED (hermes_tools_mcp_server._stateless_shim_defs
        # requires enabled AND no external provider), so the append must not
        # instruct or advertise an absent tool. The on-disk store block stays:
        # external providers run alongside the builtin store, and its facts
        # remain readable. This exercises the enabled-only gate.
        import agent.prompt_builder as pb
        import hermes_cli.config as cfg
        from agent.claude_sdk_runtime import build_system_prompt_append

        self._home(tmp_path, monkeypatch, memory="a durable fact")
        monkeypatch.setattr(
            cfg,
            "load_config",
            lambda *a, **k: {
                "memory": {"memory_enabled": True, "provider": "honcho"}
            },
        )
        captured = {}

        def fake_index(**kwargs):
            captured.update(kwargs)
            return ""

        monkeypatch.setattr(pb, "build_skills_system_prompt", fake_index)
        out = build_system_prompt_append() or ""
        assert "You have persistent memory" not in out
        assert "ONLY durable memory" not in out
        # The store block itself survives — facts stay readable.
        assert "a durable fact" in out
        # session_search is unaffected.
        assert "session_search" in out
        # And the skills filter is not told the tool exists.
        tools = captured.get("available_tools") or set()
        assert "memory" not in tools
        assert "session_search" in tools

    def test_session_line_and_platform_hint(self, tmp_path, monkeypatch):
        from agent.claude_sdk_runtime import build_system_prompt_append
        from agent.prompt_builder import PLATFORM_HINTS

        self._home(tmp_path, monkeypatch)
        out = build_system_prompt_append(
            platform="telegram", session_id="sess-77", model="claude-opus-4-8"
        )
        assert "Conversation started:" in out  # date-only, native format
        assert "Session ID: sess-77" in out
        assert "Model: claude-opus-4-8" in out
        assert "Provider: claude-agent-sdk" in out
        assert PLATFORM_HINTS["telegram"].strip() in out

    def test_unknown_platform_no_hint_and_none_safe(self, tmp_path, monkeypatch):
        from agent.claude_sdk_runtime import build_system_prompt_append

        self._home(tmp_path, monkeypatch)
        out = build_system_prompt_append(platform="faxmachine")
        assert out is not None  # None-safe, no crash, no bogus hint

    def test_budget_skips_oversized_block_keeps_later_blocks(self, tmp_path, monkeypatch):
        # Whole-block budget policy: a block that does not fit is SKIPPED
        # entirely (never truncated mid-block) and later, smaller blocks
        # still make it in. An oversized hand-edited MEMORY.md must not evict
        # the guidance. The store renders whole blocks and the budget governs.
        from agent.claude_sdk_runtime import (
            _APPEND_TOTAL_MAX_CHARS,
            build_system_prompt_append,
        )

        self._home(tmp_path, monkeypatch, memory="y" * (_APPEND_TOTAL_MAX_CHARS + 5000))
        out = build_system_prompt_append()
        assert "yyyyyyyyyy" not in out  # oversized memory block skipped whole
        assert "session_search" in out  # later block survived
        assert len(out) <= _APPEND_TOTAL_MAX_CHARS

    def test_skills_index_wiring(self, tmp_path, monkeypatch):
        # The index rides the NATIVE builder; we pin OUR wiring — called
        # with the honest MCP-exposed tool set (shims included).
        import agent.prompt_builder as pb
        from agent.claude_sdk_runtime import build_system_prompt_append
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS

        self._home(tmp_path, monkeypatch)
        captured = {}

        def fake_index(**kwargs):
            captured.update(kwargs)
            # Includes the index's real unconditional boilerplate sentence.
            # The native index instructs skill_manage regardless of
            # available_tools, and the strip must
            # remove it (a tmp home's empty index made the old pin vacuous).
            return (
                "## Skills (mandatory)\n"
                "If a skill has issues, fix it with skill_manage(action='patch').\n"
                "- fixture-skill: proves the wiring"
            )

        monkeypatch.setattr(pb, "build_skills_system_prompt", fake_index)
        out = build_system_prompt_append()
        assert "fixture-skill: proves the wiring" in out
        assert "skill_manage" not in out
        tools = captured.get("available_tools") or set()
        assert "memory" in tools and "session_search" in tools
        assert set(EXPOSED_TOOLS) <= tools

    def test_root_files_are_not_read(self, tmp_path, monkeypatch):
        # Negative control: there is one canonical location. Files left at the
        # HERMES_HOME root must NOT be injected.
        from agent.claude_sdk_runtime import build_system_prompt_append

        hermes_home = tmp_path / "hermes"
        (hermes_home / "memories").mkdir(parents=True)
        (hermes_home / "USER.md").write_text("stale root copy")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        assert "stale root copy" not in (build_system_prompt_append() or "")

    def test_memory_shim_write_is_visible_to_next_append(self, tmp_path, monkeypatch):
        # The loop closes: a fact saved through the stateless MCP shim must
        # appear in the next session's system-prompt append.
        from agent.claude_sdk_runtime import build_system_prompt_append
        from agent.transports.hermes_tools_mcp_server import dispatch_memory

        self._home(tmp_path, monkeypatch)
        dispatch_memory(
            {"action": "add", "target": "memory", "content": "the beta build ships friday"}
        )
        out = build_system_prompt_append()
        assert out is not None
        assert "the beta build ships friday" in out

    def test_empty_home_still_provides_guidance(self, tmp_path, monkeypatch):
        # The append always carries the recall/memory behavior contract, so a
        # brand-new installation still knows its tools.
        from agent.claude_sdk_runtime import build_system_prompt_append

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # empty dir
        out = build_system_prompt_append()
        assert out is not None
        assert "session_search" in out


class TestAuxLaneFailClosed:
    def test_aux_auto_detect_disabled_under_claude_sdk(self, monkeypatch):
        # With the main provider on the subscription
        # lane, aux tasks (title-gen, compression) silently fell through to
        # the metered OpenRouter/Nous auto-detect chain. Auto-detect must
        # fail closed; explicit aux config remains the operator's opt-in.
        from agent.auxiliary_client import _resolve_auto

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-key")
        client, model = _resolve_auto(main_runtime={
            "provider": "claude-agent-sdk",
            "model": "claude-opus-4-8",
            "api_mode": "claude_agent_sdk",
            "base_url": "",
            "api_key": "claude-subscription-oauth",
        })
        assert client is None and model is None


class TestSdkAvailabilityGate:
    def test_check_reports_missing_sdk(self, monkeypatch):
        # With the optional SDK import broken, the gate must
        # fail with the install hint — never silently pass.
        import builtins

        import tools.lazy_deps as lazy_deps

        events = []
        monkeypatch.setattr(
            lazy_deps,
            "ensure",
            lambda feature, **kwargs: events.append(("ensure", feature, kwargs)),
        )
        real_import = builtins.__import__

        def _broken(name, *args, **kwargs):
            if name == "claude_agent_sdk":
                events.append(("import", name))
                raise ImportError("No module named 'claude_agent_sdk'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _broken)
        from agent.transports.claude_agent_sdk_session import (
            check_claude_sdk_available,
        )

        ok, msg = check_claude_sdk_available()
        assert ok is False
        assert "hermes-agent[claude-agent-sdk]" in msg
        assert events[0] == (
            "ensure",
            "provider.claude_agent_sdk",
            {"prompt": False},
        )
        assert events[1] == ("import", "claude_agent_sdk")
