"""Result contract for the base multi-image delivery fallback."""

import asyncio

from gateway.platforms.base import BasePlatformAdapter, SendResult


class _BatchResultAdapter(BasePlatformAdapter):
    name = "batch-result-test"

    def __init__(self, *, fail_url: str = ""):
        self.fail_url = fail_url
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, **kwargs):
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {}

    async def send_image(self, chat_id, image_url, caption=None, **kwargs):
        self.sent.append(image_url)
        if image_url == self.fail_url:
            return SendResult(success=False, error="upload failed")
        return SendResult(success=True, message_id=image_url)


def test_send_multiple_images_returns_success_after_all_items_deliver():
    adapter = _BatchResultAdapter()

    result = asyncio.run(
        adapter.send_multiple_images(
            "chat-1",
            [("https://example.test/a.png", ""), ("https://example.test/b.png", "")],
        )
    )

    assert result.success is True
    assert adapter.sent == [
        "https://example.test/a.png",
        "https://example.test/b.png",
    ]


def test_send_multiple_images_reports_any_failed_item():
    failed_url = "https://example.test/b.png"
    adapter = _BatchResultAdapter(fail_url=failed_url)

    result = asyncio.run(
        adapter.send_multiple_images(
            "chat-1",
            [("https://example.test/a.png", ""), (failed_url, "")],
        )
    )

    assert result.success is False
    assert "upload failed" in (result.error or "")
    assert adapter.sent == [
        "https://example.test/a.png",
        failed_url,
    ]
