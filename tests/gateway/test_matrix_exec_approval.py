import types

import pytest
from unittest.mock import AsyncMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult


class TestMatrixExecApprovalReactions:

    def test_text_controls_yaml_seeds_platform_extra(self):
        from plugins.platforms.matrix.adapter import _apply_yaml_config

        assert _apply_yaml_config({}, {"approval_controls": "text"}) == {
            "approval_controls": "text"
        }

    def test_text_controls_load_through_real_gateway_config(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "matrix:\n  approval_controls: text\n",
            encoding="utf-8",
        )

        with patch("gateway.config.get_hermes_home", return_value=tmp_path):
            from gateway.config import load_gateway_config

            config = load_gateway_config()

        matrix_config = config.platforms[Platform.MATRIX]
        assert matrix_config.extra["approval_controls"] == "text"

        from plugins.platforms.matrix.adapter import MatrixAdapter

        adapter = MatrixAdapter(matrix_config)
        assert adapter._approval_controls == "text"


    @pytest.mark.asyncio
    async def test_reaction_resolves_pending_approval(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@liizfq:liizfq.top")
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.org"}))
        # Resolve user_id so _is_self_sender doesn't defensively drop all traffic (#15763).
        adapter._user_id = "@bot:example.org"
        adapter._approval_prompts_by_event["$target"] = _MatrixApprovalPrompt(
            session_key="sess-1", chat_id="!room:example.org", message_id="$target"
        )
        adapter._approval_prompt_by_session["sess-1"] = "$target"

        content = {"m.relates_to": {"event_id": "$target", "key": "✅"}}
        event = types.SimpleNamespace(
            sender="@liizfq:liizfq.top",
            event_id="$react1",
            room_id="!room:example.org",
            content=content,
        )

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._on_reaction(event)

        mock_resolve.assert_called_once_with("sess-1", "once")
        assert "$target" not in adapter._approval_prompts_by_event
        assert "sess-1" not in adapter._approval_prompt_by_session

    @pytest.mark.asyncio
    async def test_text_controls_send_plain_prompt_without_reactions(self):
        from plugins.platforms.matrix.adapter import MatrixAdapter

        adapter = MatrixAdapter(
            PlatformConfig(
                enabled=True,
                token="tok",
                extra={
                    "homeserver": "https://matrix.example.org",
                    "approval_controls": "text",
                },
            )
        )
        adapter._client = object()
        adapter.send = AsyncMock(
            return_value=SendResult(success=True, message_id="$prompt")
        )
        adapter._send_reaction = AsyncMock(return_value="$reaction")

        result = await adapter.send_exec_approval(
            chat_id="!room:example.org",
            command="rm -rf /tmp/old-project",
            session_key="sess-text",
            description="recursive delete",
        )

        assert result.success is True
        send_call = adapter.send.await_args
        assert send_call is not None
        prompt_text = send_call.args[1]
        assert prompt_text.startswith("I need your approval before running this command")
        assert "Reply `!approve` to run it once" in prompt_text
        assert "`!approve session`" in prompt_text
        assert "`!approve always`" in prompt_text
        assert "`!deny`" in prompt_text
        assert not any(symbol in prompt_text for symbol in ("⚠", "✅", "🌀", "♾", "❌", "❎"))
        adapter._send_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "present", "absent"),
        [
            (
                {"smart_denied": True},
                ("!approve`", "!deny`"),
                ("!approve session", "!approve always"),
            ),
            (
                {"allow_session": False, "allow_permanent": False},
                ("!approve`", "!deny`"),
                ("!approve session", "!approve always"),
            ),
            (
                {"allow_session": False, "allow_permanent": True},
                ("!approve`", "!deny`"),
                ("!approve session", "!approve always"),
            ),
        ],
    )
    async def test_text_controls_only_advertise_available_scopes(
        self, kwargs, present, absent
    ):
        from plugins.platforms.matrix.adapter import MatrixAdapter

        adapter = MatrixAdapter(
            PlatformConfig(
                enabled=True,
                token="tok",
                extra={"approval_controls": "text"},
            )
        )
        adapter._client = object()
        adapter.send = AsyncMock(
            return_value=SendResult(success=True, message_id="$prompt")
        )
        adapter._send_reaction = AsyncMock()

        await adapter.send_exec_approval(
            "!room:example.org",
            "rm -rf /tmp/old-project",
            "sess-scopes",
            **kwargs,
        )

        send_call = adapter.send.await_args
        assert send_call is not None
        prompt_text = send_call.args[1]
        assert all(choice in prompt_text for choice in present)
        assert all(choice not in prompt_text for choice in absent)
        adapter._send_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_text_controls_ignore_approval_reactions(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@liizfq:liizfq.top")
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(
            PlatformConfig(
                enabled=True,
                token="tok",
                extra={
                    "homeserver": "https://matrix.example.org",
                    "approval_controls": "text",
                },
            )
        )
        adapter._user_id = "@bot:example.org"
        adapter._approval_prompts_by_event["$target"] = _MatrixApprovalPrompt(
            session_key="sess-text", chat_id="!room:example.org", message_id="$target"
        )

        event = types.SimpleNamespace(
            sender="@liizfq:liizfq.top",
            event_id="$react-text",
            room_id="!room:example.org",
            content={"m.relates_to": {"event_id": "$target", "key": "✅"}},
        )

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._on_reaction(event)

        mock_resolve.assert_not_called()
        assert "$target" in adapter._approval_prompts_by_event
