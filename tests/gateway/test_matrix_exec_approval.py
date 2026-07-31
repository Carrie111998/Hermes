import types
import time

import pytest
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig


class TestMatrixExecApprovalReactions:
    @pytest.mark.asyncio
    async def test_reaction_resolves_pending_approval(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@liizfq:liizfq.top")
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.org"}))
        # Resolve user_id so _is_self_sender doesn't defensively drop all traffic (#15763).
        adapter._user_id = "@bot:example.org"
        adapter._approval_prompts_by_event["$target"] = _MatrixApprovalPrompt(
            session_key="sess-1",
            approval_id="approval-1",
            chat_id="!room:example.org",
            message_id="$target",
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

        mock_resolve.assert_called_once_with(
            "sess-1", "once", approval_id="approval-1"
        )
        assert "$target" not in adapter._approval_prompts_by_event
        assert "sess-1" not in adapter._approval_prompt_by_session

    @pytest.mark.asyncio
    async def test_old_reaction_resolves_its_exact_approval(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@liizfq:liizfq.top")
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.org"}))
        adapter._user_id = "@bot:example.org"
        adapter._approval_prompts_by_event["$first"] = _MatrixApprovalPrompt(
            session_key="sess-1",
            approval_id="approval-first",
            chat_id="!room:example.org",
            message_id="$first",
        )
        adapter._approval_prompts_by_event["$second"] = _MatrixApprovalPrompt(
            session_key="sess-1",
            approval_id="approval-second",
            chat_id="!room:example.org",
            message_id="$second",
        )
        adapter._approval_prompt_by_session["sess-1"] = "$second"

        event = types.SimpleNamespace(
            sender="@liizfq:liizfq.top",
            event_id="$react-first",
            room_id="!room:example.org",
            content={"m.relates_to": {"event_id": "$first", "key": "✅"}},
        )

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
            await adapter._on_reaction(event)

        resolve.assert_called_once_with(
            "sess-1", "once", approval_id="approval-first"
        )
        assert adapter._approval_prompt_by_session["sess-1"] == "$second"
        assert "$second" in adapter._approval_prompts_by_event

    def test_prompt_registry_expires_and_bounds_exact_prompts(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@liizfq:liizfq.top")
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(
            PlatformConfig(
                enabled=True,
                token="tok",
                extra={"homeserver": "https://matrix.example.org"},
            )
        )
        adapter._APPROVAL_PROMPT_CACHE_SIZE = 2

        def prompt(event_id, session_key, *, expires_at=None):
            return _MatrixApprovalPrompt(
                session_key=session_key,
                approval_id=f"approval-{event_id}",
                chat_id="!room:example.org",
                message_id=event_id,
                expires_at=expires_at,
            )

        expired = prompt("$expired", "expired", expires_at=time.monotonic() - 1)
        adapter._approval_prompts_by_event["$expired"] = expired
        adapter._approval_prompt_by_session["expired"] = "$expired"

        first = prompt("$first", "sess-1")
        second = prompt("$second", "sess-1")
        third = prompt("$third", "sess-2")
        adapter._remember_matrix_approval_prompt("$first", first)
        adapter._remember_matrix_approval_prompt("$second", second)
        adapter._remember_matrix_approval_prompt("$third", third)

        assert set(adapter._approval_prompts_by_event) == {"$second", "$third"}
        assert adapter._approval_prompt_by_session == {
            "sess-1": "$second",
            "sess-2": "$third",
        }
