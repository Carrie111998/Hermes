"""Regression tests for #95382: fresh-final must record WHAT it delivered.

`_try_fresh_final` set `_final_response_sent = True` without recording
`_delivered_final_text`. `delivered_final_matches` then returned None
("nothing to compare"), so the gateway's `_stream_confirmed_final_delivery`
took its legacy trust branch and accepted a PARTIAL fresh-final as complete
delivery — the user kept a truncated message and no recovery path (re-send /
continuation prompt) ever fired.

Recording the payload makes the reconciliation real: a genuine mismatch now
returns False, the gateway stops suppressing, and the existing queued
re-send path recovers the full answer.
"""

import asyncio

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


PARTIAL = "The photo shows a dog on a beach"
TAIL = " with a red frisbee in its mouth, mid-leap over the surf."
FULL = PARTIAL + TAIL


class _FreshFinalAdapter(BasePlatformAdapter):
    """Records sends; can fail them, and optionally offers delete_message."""

    def __init__(self, *, fail_send: bool = False, with_delete: bool = False):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.deleted = []
        self._fail_send = fail_send
        self._next_id = 0
        if not with_delete:
            self.delete_message = None  # type: ignore[assignment]

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"content": content, "metadata": metadata})
        if self._fail_send:
            return SendResult(success=False, error="send refused")
        self._next_id += 1
        return SendResult(success=True, message_id=f"m-{self._next_id}")

    async def edit_message(
        self, chat_id, message_id, content, *, finalize: bool = False, metadata=None
    ) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _consumer(adapter=None):
    adapter = adapter or _FreshFinalAdapter()
    return GatewayStreamConsumer(
        adapter, "chat-1", StreamConsumerConfig(cursor=" ▉")
    )


class TestFreshFinalRecordsPayload:
    def test_turn_final_records_delivered_text(self):
        """The #95382 fix: a turn-final fresh send records its payload."""
        c = _consumer()
        ok = asyncio.run(c._try_fresh_final(FULL, is_turn_final=True))

        assert ok is True
        assert c._final_response_sent is True
        assert c._delivered_final_text == FULL, (
            "fresh-final must record WHAT it delivered — without this the "
            "gateway cannot distinguish partial from complete (#95382)"
        )

    def test_partial_fresh_final_is_detected_as_mismatch(self):
        """The incident shape: partial delivered, complete response differs."""
        c = _consumer()
        asyncio.run(c._try_fresh_final(PARTIAL, is_turn_final=True))

        assert c.delivered_final_matches(FULL) is False, (
            "a partial fresh-final must reconcile as a MISMATCH so the gateway "
            "stops suppressing and recovery fires (#95382)"
        )

    def test_complete_fresh_final_still_suppresses(self):
        """A complete fresh-final must still suppress the duplicate send."""
        c = _consumer()
        asyncio.run(c._try_fresh_final(FULL, is_turn_final=True))

        assert c.delivered_final_matches(FULL) is True, (
            "no duplicate re-send when the fresh-final carried the whole answer"
        )

    def test_non_turn_final_does_not_record(self):
        """A non-turn-final fresh send must not claim turn-final delivery."""
        c = _consumer()
        asyncio.run(c._try_fresh_final("interim preamble", is_turn_final=False))

        assert c._final_response_sent is False
        assert c._delivered_final_text is None

    def test_failed_send_records_nothing(self):
        """A failed fresh send must not set flags or record a payload."""
        c = _consumer(_FreshFinalAdapter(fail_send=True))
        ok = asyncio.run(c._try_fresh_final(FULL, is_turn_final=True))

        assert ok is False
        assert c._final_response_sent is False
        assert c._delivered_final_text is None

    def test_split_turn_refuses_fresh_final(self):
        """Split turns take the edit path — the #78541 guard is preserved."""
        c = _consumer()
        c._turn_split_delivery = True
        c._stream_ledger = FULL

        ok = asyncio.run(c._try_fresh_final(TAIL, is_turn_final=True))

        assert ok is False, (
            "_try_fresh_final must refuse split turns so sealed head messages "
            "are never deleted (#78541)"
        )
        assert c._delivered_final_text is None
