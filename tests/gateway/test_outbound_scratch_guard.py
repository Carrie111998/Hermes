import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


class DummyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SIGNAL)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self):
        return None

    async def start(self):
        return None

    async def stop(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="m1")

    async def get_updates(self, timeout=30):
        return []

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.mark.asyncio
async def test_send_with_retry_blocks_scratch_need_notes_before_delivery():
    adapter = DummyAdapter()

    result = await adapter._send_with_retry(
        "chat1",
        "Need install copy.\nNeed commit push.\nNeed verify status and maybe update todo/objective.",
    )

    assert result.success is False
    assert result.error_kind == "bad_format"
    assert "internal scratch" in (result.error or "")
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_send_with_retry_allows_user_visible_need_sentences():
    adapter = DummyAdapter()

    result = await adapter._send_with_retry(
        "chat1",
        "You need to restart the bridge before the new code is live.",
    )

    assert result.success is True
    assert adapter.sent == ["You need to restart the bridge before the new code is live."]


@pytest.mark.asyncio
async def test_send_with_retry_allows_quoted_scratch_diagnosis():
    adapter = DummyAdapter()

    result = await adapter._send_with_retry(
        "chat1",
        "The screenshot showed quoted scratch text: `Need commit push.`",
    )

    assert result.success is True
    assert adapter.sent == ["The screenshot showed quoted scratch text: `Need commit push.`"]
