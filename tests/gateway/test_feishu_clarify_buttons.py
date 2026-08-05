"""Tests for Feishu interactive card clarify buttons + plain-text fallback.

Covers:
- send_clarify sends an interactive card with one button per choice + Other
- send_clarify stores clarify state for callback routing
- send_clarify falls back to plain text when the card send fails (B plan:
  the md renderer swallows ordered-list syntax, so the fallback sends a
  ``text`` message instead of ``post``/md)
- _handle_clarify_card_action resolves the pending clarify via
  tools.clarify_gateway
- picking "Other" flips the entry into text-capture mode
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Feishu mock so FeishuAdapter can be imported without lark-oapi
# ---------------------------------------------------------------------------
def _ensure_feishu_mocks():
    """Provide stubs for lark-oapi / aiohttp.web so the import succeeds."""
    if importlib.util.find_spec("lark_oapi") is None and "lark_oapi" not in sys.modules:
        mod = MagicMock()
        for name in (
            "lark_oapi", "lark_oapi.api.im.v1",
            "lark_oapi.event", "lark_oapi.event.callback_type",
        ):
            sys.modules.setdefault(name, mod)
    if importlib.util.find_spec("aiohttp") is None and "aiohttp" not in sys.modules:
        aio = MagicMock()
        sys.modules.setdefault("aiohttp", aio)
        sys.modules.setdefault("aiohttp.web", aio.web)


_ensure_feishu_mocks()

from gateway.config import PlatformConfig  # noqa: E402
import plugins.platforms.feishu.adapter as feishu_module  # noqa: E402
from plugins.platforms.feishu.adapter import FeishuAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter() -> FeishuAdapter:
    """Create a FeishuAdapter with mocked internals."""
    config = PlatformConfig(enabled=True)
    adapter = FeishuAdapter(config)
    adapter._client = MagicMock()
    return adapter


def _make_card_action_data(
    action_value: dict,
    chat_id: str = "oc_12345",
    open_id: str = "ou_user1",
    token: str = "tok_clarify_1",
) -> SimpleNamespace:
    """Create a mock Feishu card action callback data object."""
    return SimpleNamespace(
        event=SimpleNamespace(
            token=token,
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(
                tag="button",
                value=action_value,
            ),
        ),
    )


def _close_submitted_coro(coro, _loop):
    """Close scheduled coroutines in sync-handler tests to avoid unawaited warnings."""
    coro.close()
    return SimpleNamespace(add_done_callback=lambda *_args, **_kwargs: None)


# ===========================================================================
# send_clarify — interactive card with buttons (A plan)
# ===========================================================================

class TestFeishuClarifyCard:
    """Test send_clarify sends an interactive card with choice buttons."""

    @pytest.mark.asyncio
    async def test_sends_interactive_card(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_cl_001"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_clarify(
                chat_id="oc_12345",
                question="你想选哪个方案？",
                choices=["方案A", "方案B", "方案C"],
                clarify_id="cl_abc123",
                session_key="agent:main:feishu:dm:oc_12345",
            )

        assert result.success is True
        assert result.message_id == "msg_cl_001"

        kwargs = mock_send.call_args[1]
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"

        card = json.loads(kwargs["payload"])
        # Question in the markdown body
        body = card["elements"][0]["content"]
        assert "你想选哪个方案？" in body
        # Option text is rendered as plain lines (no md ordered-list syntax)
        assert "1. 方案A" in body
        assert "2. 方案B" in body
        assert "3. 方案C" in body

        # Buttons: one per choice + Other
        actions = card["elements"][1]["actions"]
        assert len(actions) == 4
        values = [a["value"] for a in actions]
        assert values[0] == {"hermes_clarify_action": "cl_abc123", "choice_index": 0}
        assert values[1] == {"hermes_clarify_action": "cl_abc123", "choice_index": 1}
        assert values[2] == {"hermes_clarify_action": "cl_abc123", "choice_index": 2}
        assert values[3] == {"hermes_clarify_action": "cl_abc123", "choice_index": -1}

    @pytest.mark.asyncio
    async def test_stores_clarify_state(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_cl_002"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_clarify(
                chat_id="oc_12345",
                question="Q?",
                choices=["X", "Y"],
                clarify_id="cl_store1",
                session_key="my-session-key",
            )

        state = adapter._clarify_state.get("cl_store1")
        assert state is not None
        assert state["session_key"] == "my-session-key"
        assert state["message_id"] == "msg_cl_002"
        assert state["chat_id"] == "oc_12345"
        assert state["choices"] == ["X", "Y"]


# ===========================================================================
# send_clarify — plain-text fallback (B plan: md swallows lists)
# ===========================================================================

class TestFeishuClarifyTextFallback:
    """Test send_clarify falls back to a text message when the card fails."""

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_text_on_card_failure(self):
        adapter = _make_adapter()

        fail_response = SimpleNamespace(
            success=lambda: False,
            data=SimpleNamespace(message_id=""),
        )
        # First call (interactive card) fails → second call (text) succeeds
        mock_send = AsyncMock(side_effect=[fail_response, fail_response])

        with patch.object(adapter, "_feishu_send_with_retry", mock_send):
            with patch("tools.clarify_gateway.mark_awaiting_text") as mock_mark:
                result = await adapter.send_clarify(
                    chat_id="oc_12345",
                    question="选择困难？",
                    choices=["A", "B"],
                    clarify_id="cl_fb1",
                    session_key="sess",
                )

        assert result.success is False  # text send also "failed" in this mock
        # Second send must be msg_type="text" (not post/md) so the option
        # lines render verbatim instead of being swallowed by the md renderer.
        text_kwargs = mock_send.call_args_list[1][1]
        assert text_kwargs["msg_type"] == "text"
        text_payload = json.loads(text_kwargs["payload"])
        assert "❓ 选择困难？" in text_payload["text"]
        assert "1. A" in text_payload["text"]
        assert "2. B" in text_payload["text"]
        mock_mark.assert_called_once_with("cl_fb1")

    @pytest.mark.asyncio
    async def test_open_ended_question_uses_text_directly(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_cl_open"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            with patch("tools.clarify_gateway.mark_awaiting_text"):
                result = await adapter.send_clarify(
                    chat_id="oc_12345",
                    question="随便说点啥",
                    choices=None,
                    clarify_id="cl_open1",
                    session_key="sess",
                )

        assert result.success is True
        kwargs = mock_send.call_args[1]
        assert kwargs["msg_type"] == "text"
        payload = json.loads(kwargs["payload"])
        assert payload["text"] == "❓ 随便说点啥"


# ===========================================================================
# _handle_clarify_card_action — callback routing
# ===========================================================================

class TestFeishuClarifyCardAction:
    """Test card button clicks resolve the pending clarify."""

    def test_resolves_choice(self):
        adapter = _make_adapter()
        adapter._clarify_state["cl_abc"] = {
            "session_key": "sess-abc",
            "message_id": "msg_abc",
            "chat_id": "oc_12345",
            "choices": ["方案A", "方案B", "方案C"],
        }

        data = _make_card_action_data(
            {"hermes_clarify_action": "cl_abc", "choice_index": 1},
        )

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_submitted_coro) as mock_submit:
            with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as mock_resolve:
                with patch.object(adapter, "_allow_group_message", return_value=True):
                    adapter._handle_clarify_card_action(
                        event=data.event, action_value=data.event.action.value, loop=MagicMock(),
                    )

        mock_submit.assert_called_once()
        # The scheduled coroutine resolves with the choice text at index 1
        scheduled = mock_submit.call_args[0][1]
        assert scheduled is not None

        # Verify _resolve_clarify pops state and calls resolve_gateway_clarify
        with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as mock_resolve2:
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                adapter._resolve_clarify(
                    clarify_id="cl_abc",
                    choice_text="方案B",
                    user_name="Norbert",
                )
            )
        mock_resolve2.assert_called_once_with("cl_abc", "方案B")
        assert "cl_abc" not in adapter._clarify_state

    def test_other_button_marks_awaiting_text(self):
        adapter = _make_adapter()
        adapter._clarify_state["cl_other"] = {
            "session_key": "sess-other",
            "message_id": "msg_other",
            "chat_id": "oc_12345",
            "choices": ["A", "B"],
        }

        data = _make_card_action_data(
            {"hermes_clarify_action": "cl_other", "choice_index": -1},
        )

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_submitted_coro):
            with patch.object(adapter, "_allow_group_message", return_value=True):
                adapter._handle_clarify_card_action(
                    event=data.event, action_value=data.event.action.value, loop=MagicMock(),
                )

        with patch("tools.clarify_gateway.mark_awaiting_text") as mock_mark:
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                adapter._resolve_clarify(
                    clarify_id="cl_other",
                    choice_text=None,  # Other → text capture
                    user_name="Norbert",
                )
            )
        mock_mark.assert_called_once_with("cl_other")
        assert "cl_other" not in adapter._clarify_state

    def test_unknown_clarify_is_ignored(self):
        adapter = _make_adapter()
        data = _make_card_action_data(
            {"hermes_clarify_action": "cl_ghost", "choice_index": 0},
        )
        with patch.object(adapter, "_submit_on_loop") as mock_submit:
            adapter._handle_clarify_card_action(
                event=data.event, action_value=data.event.action.value, loop=MagicMock(),
            )
        mock_submit.assert_not_called()

    def test_unauthorized_click_is_rejected(self):
        adapter = _make_adapter()
        adapter._clarify_state["cl_auth"] = {
            "session_key": "sess-auth",
            "message_id": "msg_auth",
            "chat_id": "oc_12345",
            "choices": ["A"],
        }
        data = _make_card_action_data(
            {"hermes_clarify_action": "cl_auth", "choice_index": 0},
            open_id="ou_intruder",
        )
        with patch.object(adapter, "_submit_on_loop") as mock_submit:
            with patch.object(adapter, "_allow_group_message", return_value=False):
                adapter._handle_clarify_card_action(
                    event=data.event, action_value=data.event.action.value, loop=MagicMock(),
                )
        mock_submit.assert_not_called()
        assert "cl_auth" in adapter._clarify_state  # state preserved
