"""Rich-only Telegram messages (Bot API 10.1 rich_message blocks) must not be
silently dropped — they are flattened to plaintext and dispatched as TEXT."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig


def _make_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="test-token")
    adapter._running = True
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._pending_photo_batches = {}
    adapter._pending_photo_batch_tasks = {}
    adapter._media_group_events = {}
    adapter._media_group_tasks = {}
    adapter._text_batch_delay_seconds = 0.1
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._held_inbound_events = []
    adapter.HELD_INBOUND_MAX = 64
    adapter.handle_message = AsyncMock()
    return adapter


def _rich_update(text: str = "富文本内容", *, with_text: bool = False):
    blocks = [{"type": "paragraph", "text": [{"type": "plain", "text": text}]}]
    api_kwargs = {"rich_message": {"blocks": blocks}}
    msg = SimpleNamespace(
        text=text if with_text else None,
        caption=None,
        api_kwargs=api_kwargs,
        chat=SimpleNamespace(id=1, type="private", title=None),
        from_user=SimpleNamespace(id=1, full_name="T", is_bot=False),
        message_id=10,
        date=None,
    )
    return SimpleNamespace(update_id=1, message=msg)


@pytest.mark.asyncio
async def test_rich_only_message_recovered_as_text(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setattr(adapter, "_is_user_authorized_from_message", lambda m: True)
    monkeypatch.setattr(adapter, "_should_process_message", lambda m, **k: True)
    monkeypatch.setattr(adapter, "_ensure_forum_commands", AsyncMock())
    monkeypatch.setattr(adapter, "_cache_replied_media", AsyncMock())
    monkeypatch.setattr(adapter, "_apply_telegram_group_observe_attribution", lambda e: e)
    monkeypatch.setattr(adapter, "_clean_bot_trigger_text", lambda t: t)
    monkeypatch.setattr(adapter, "_build_message_event", lambda m, t, update_id=None: SimpleNamespace(text=""))
    enqueued = []
    monkeypatch.setattr(adapter, "_enqueue_text_event", enqueued.append)

    await adapter._handle_rich_only_message(_rich_update(), context=None)

    assert len(enqueued) == 1
    assert "富文本内容" in enqueued[0].text


@pytest.mark.asyncio
async def test_plain_message_not_touched(monkeypatch):
    """A message with text is owned by the group-0 TEXT handler — never enqueue here."""
    adapter = _make_adapter()
    enqueued = []
    monkeypatch.setattr(adapter, "_enqueue_text_event", enqueued.append)

    await adapter._handle_rich_only_message(_rich_update(with_text=True), context=None)

    assert enqueued == []
