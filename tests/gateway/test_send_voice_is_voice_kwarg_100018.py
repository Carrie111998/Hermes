"""Regression tests for #100018.

The gateway media dispatch loop (``gateway/platforms/base.py``) passes
``is_voice=...`` on every ``send_voice`` call for audio MEDIA: attachments.
``MattermostAdapter.send_voice`` and ``LineAdapter.send_voice`` were the only
adapters without a ``**kwargs`` catch-all, so the call raised ``TypeError``
at argument-binding time — before any upload was attempted — and the
attachment silently failed with no user-visible error.

These tests pin both levels: the signature must bind the router's exact
kwarg set, and a live call must reach the adapter's own guard/upload logic
instead of dying at binding time.
"""

from __future__ import annotations

import asyncio
import inspect

from gateway.platforms.base import SendResult


def _bind_router_kwargs(method) -> None:
    """Bind the exact kwarg set used by the gateway media dispatch loop."""
    inspect.signature(method).bind(
        None, chat_id="C1", audio_path="/tmp/x.ogg", metadata=None, is_voice=True
    )


class TestMattermostSendVoiceKwarg:
    def test_signature_binds_router_kwargs(self):
        from plugins.platforms.mattermost.adapter import MattermostAdapter

        _bind_router_kwargs(MattermostAdapter.send_voice)

    def test_call_with_is_voice_reaches_local_file_upload(self):
        from plugins.platforms.mattermost.adapter import MattermostAdapter

        adapter = MattermostAdapter.__new__(MattermostAdapter)
        seen: dict = {}

        async def fake_send_local_file(chat_id, audio_path, caption, reply_to, metadata=None):
            seen["args"] = (chat_id, audio_path, caption, reply_to, metadata)
            return SendResult(success=True)

        adapter._send_local_file = fake_send_local_file

        result = asyncio.run(
            adapter.send_voice(
                chat_id="C1",
                audio_path="/tmp/x.ogg",
                metadata={"thread_id": "t1"},
                is_voice=True,
            )
        )

        assert result.success
        assert seen["args"] == ("C1", "/tmp/x.ogg", None, None, {"thread_id": "t1"})


class TestLineSendVoiceKwarg:
    def test_signature_binds_router_kwargs(self):
        from plugins.platforms.line.adapter import LineAdapter

        _bind_router_kwargs(LineAdapter.send_voice)

    def test_call_with_is_voice_returns_controlled_failure_for_missing_file(self):
        from plugins.platforms.line.adapter import LineAdapter

        adapter = LineAdapter.__new__(LineAdapter)

        result = asyncio.run(
            adapter.send_voice(
                chat_id="C1",
                audio_path="/nonexistent/x.ogg",
                metadata=None,
                is_voice=True,
            )
        )

        assert not result.success
        assert "not found" in (result.error or "")
