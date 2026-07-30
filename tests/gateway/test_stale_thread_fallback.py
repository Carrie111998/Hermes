"""Stale-topic fallback — a dead thread binding must not be replayed.

Production 2026-07-30: three Telegram responses were lost outright.  The send
was rejected with "Message thread not found", and the plain-text fallback
re-sent with the *same* metadata, so it was rejected identically and the user
got nothing.  A fallback that reuses the routing that just failed cannot help.
"""

import asyncio

from gateway.platforms.base import (
    BasePlatformAdapter,
    Platform,
    PlatformConfig,
    SendResult,
)


class _StaleTopicAdapter(BasePlatformAdapter):
    """Rejects any send that still carries a thread binding."""

    def __init__(self, error="Message thread not found"):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.calls = []
        self._error = error

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append({"reply_to": reply_to, "metadata": metadata})
        if (metadata or {}).get("thread_id"):
            return SendResult(success=False, error=self._error)
        return SendResult(success=True, message_id="m1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _FormattingFailureAdapter(_StaleTopicAdapter):
    """Fails the first send for a reason unrelated to routing."""

    def __init__(self):
        super().__init__(error="Bad Request: can't parse entities")
        self._sends = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append({"reply_to": reply_to, "metadata": metadata})
        self._sends += 1
        if self._sends == 1:
            return SendResult(success=False, error=self._error)
        return SendResult(success=True, message_id="m1")


def test_fallback_drops_dead_thread_binding():
    adapter = _StaleTopicAdapter()
    result = asyncio.run(
        adapter._send_with_retry(
            chat_id="123",
            content="hello",
            reply_to="456",
            metadata={"thread_id": "999", "keep": "me"},
            max_retries=0,
        )
    )

    assert result.success is True, "fallback must land once the dead topic is dropped"
    assert len(adapter.calls) == 2, f"expected original + fallback, got {adapter.calls}"

    fallback = adapter.calls[1]
    assert not (fallback["metadata"] or {}).get("thread_id"), "dead thread_id was replayed"
    assert fallback["reply_to"] is None, "reply_to anchors inside the dead topic"
    assert (fallback["metadata"] or {}).get("keep") == "me", "unrelated metadata must survive"


def test_fallback_keeps_routing_for_unrelated_errors():
    """Regression guard: only the stale-topic class loses its routing."""
    adapter = _FormattingFailureAdapter()
    result = asyncio.run(
        adapter._send_with_retry(
            chat_id="123",
            content="hello",
            reply_to="456",
            metadata={"thread_id": "999"},
            max_retries=0,
        )
    )

    assert result.success is True
    assert len(adapter.calls) == 2
    fallback = adapter.calls[1]
    assert (fallback["metadata"] or {}).get("thread_id") == "999"
    assert fallback["reply_to"] == "456"
