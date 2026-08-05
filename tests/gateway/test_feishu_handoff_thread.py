"""Tests for Feishu adapter create_handoff_thread + thread routing.

Covers:
- create_handoff_thread: seed-anchor pattern (Feishu surfaces any reply
  chain as a "topic" in DMs and groups, so the anchor message_id is the
  thread handle — mirrors Slack).
- _send_raw_message anchorless thread routing: metadata["thread_id"] must
  go through the reply API with reply_in_thread=true, NOT message.create
  with receive_id_type="thread_id" (rejected by the Feishu API with
  99992402 — #78975).
- Degradation contract: every failure path returns None so callers
  (scheduler ``_open_continuable_cron_thread``, gateway ``_process_handoff``)
  fall back to the origin-DM mirror without failing the delivery.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult


def _make_adapter():
    from plugins.platforms.feishu.adapter import FeishuAdapter

    return FeishuAdapter(PlatformConfig())


class TestCreateHandoffThread(unittest.TestCase):
    def test_anchor_message_and_returns_message_id(self):
        adapter = _make_adapter()
        adapter._client = Mock()

        async def _fake_send(chat_id, content):
            self.assertIn("Hermes —", content)
            return SendResult(success=True, message_id="om_anchor123")

        with patch.object(adapter, "send", side_effect=_fake_send):
            result = asyncio.run(adapter.create_handoff_thread("oc_dm", "daily-review"))

        self.assertEqual(result, "om_anchor123")

    def test_anchor_send_failure_returns_none(self):
        adapter = _make_adapter()
        adapter._client = Mock()

        async def _fake_send(chat_id, content):
            return SendResult(success=False, error="[230002] cannot send")

        with patch.object(adapter, "send", side_effect=_fake_send):
            result = asyncio.run(adapter.create_handoff_thread("oc_dm", "x"))

        self.assertIsNone(result)

    def test_no_client_returns_none(self):
        adapter = _make_adapter()
        adapter._client = None
        result = asyncio.run(adapter.create_handoff_thread("oc_x", "x"))
        self.assertIsNone(result)

    def test_exception_inside_is_swallowed_to_none(self):
        adapter = _make_adapter()
        adapter._client = Mock()

        async def _boom(chat_id, content):
            raise RuntimeError("network down")

        with patch.object(adapter, "send", side_effect=_boom):
            result = asyncio.run(adapter.create_handoff_thread("oc_x", "x"))

        self.assertIsNone(result)


class TestAnchorlessThreadRouting(unittest.TestCase):
    """#78975 regression: metadata['thread_id'] must route via the reply
    API (reply_in_thread=true), never message.create with the invalid
    receive_id_type='thread_id'."""

    def _run_send_raw(self, metadata):
        adapter = _make_adapter()
        captured = {}

        class _MessageAPI:
            def reply(self, request):
                captured["api"] = "reply"
                captured["request"] = request
                return SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(message_id="om_routed"),
                )

            def create(self, request):
                captured["api"] = "create"
                captured["request"] = request
                return SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(message_id="om_created"),
                )

        adapter._client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=_MessageAPI()))
        )

        async def _direct(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch("plugins.platforms.feishu.adapter.asyncio.to_thread", side_effect=_direct):
            result = asyncio.run(
                adapter._send_raw_message(
                    chat_id="oc_chat",
                    msg_type="text",
                    payload='{"text":"hi"}',
                    reply_to=None,
                    metadata=metadata,
                )
            )

        return adapter, captured, result

    def test_thread_id_routes_via_reply_api_with_reply_in_thread(self):
        _, captured, result = self._run_send_raw({"thread_id": "om_anchor1"})

        self.assertTrue(result.success)
        self.assertEqual(captured["api"], "reply")
        self.assertEqual(captured["request"].message_id, "om_anchor1")
        self.assertTrue(captured["request"].request_body.reply_in_thread)

    def test_no_thread_metadata_uses_create_with_chat_id(self):
        _, captured, result = self._run_send_raw(None)

        self.assertTrue(result.success)
        self.assertEqual(captured["api"], "create")
        self.assertEqual(captured["request"].receive_id_type, "chat_id")
        self.assertEqual(captured["request"].request_body.receive_id, "oc_chat")


if __name__ == "__main__":
    unittest.main()
