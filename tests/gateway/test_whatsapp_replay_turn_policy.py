from __future__ import annotations

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.whatsapp import WhatsAppAdapter


CHAT = "120363403845802098@g.us"


def _message(message_id: str, ts: int, body: str, chat_id: str = CHAT) -> dict:
    return {
        "messageId": message_id,
        "chatId": chat_id,
        "chatName": "MM2 Maintenance (SK)",
        "senderId": "251547711758376@lid",
        "senderName": "251547711758376",
        "isGroup": True,
        "timestamp": ts,
        "body": body,
        "hasMedia": False,
        "mediaUrls": [],
    }


def _adapter(extra: dict | None = None) -> WhatsAppAdapter:
    return WhatsAppAdapter(PlatformConfig(enabled=True, extra=extra or {}))


async def _capture_replay(adapter: WhatsAppAdapter, messages: list[dict]) -> list:
    captured = []

    async def handle(event):
        captured.append(event)

    adapter.handle_message = handle  # type: ignore[method-assign]
    processed = await adapter.replay_bridge_messages(messages)
    assert processed == len(messages)
    return captured


@pytest.mark.asyncio
async def test_ingest_chat_bypasses_require_mention_for_ops_capture():
    adapter = _adapter({"require_mention": True, "ingest_chats": [CHAT]})

    event = await adapter._build_message_event(_message("m1", 1779679800, "normal worker update"))

    assert event is not None
    assert event.text == "normal worker update"
    assert event.source.chat_id == CHAT


@pytest.mark.asyncio
async def test_replay_uses_timestamp_debounce_without_wall_sleep():
    adapter = _adapter({
        "require_mention": True,
        "ingest_chats": [CHAT],
        "turn_policy": {
            CHAT: {"debounce_seconds": 300, "direct_mention_immediate": False},
        },
    })

    captured = await _capture_replay(adapter, [
        _message("m1", 1000, "before"),
        _message("m2", 1100, "install done"),
        _message("m3", 1501, "new job"),
    ])

    assert len(captured) == 2
    assert captured[0].raw_message["sourceMessageIds"] == ["m1", "m2"]
    assert captured[1].message_id == "m3"


@pytest.mark.asyncio
async def test_direct_trigger_closes_replay_turn_immediately():
    adapter = _adapter({
        "require_mention": True,
        "ingest_chats": [CHAT],
        "turn_policy": {
            CHAT: {"debounce_seconds": 300, "direct_mention_immediate": True},
        },
    })

    captured = await _capture_replay(adapter, [
        _message("m1", 1000, "worker context"),
        _message("m2", 1010, "/status please"),
        _message("m3", 1020, "after direct trigger"),
    ])

    assert len(captured) == 2
    assert captured[0].raw_message["sourceMessageIds"] == ["m1", "m2"]
    assert captured[1].message_id == "m3"


@pytest.mark.asyncio
async def test_replay_bundle_hard_capped_at_ten_messages():
    """A run of messages all inside the debounce window must not accumulate
    into one mega-bundle: the replay cap flushes at 10, spillover starts the
    next turn."""
    adapter = _adapter({
        "require_mention": True,
        "ingest_chats": [CHAT],
        "turn_policy": {
            CHAT: {"debounce_seconds": 300, "direct_mention_immediate": False},
        },
    })

    # 22 messages, each 100s apart (< debounce) — would previously bundle
    # into a single 22-message turn spanning ~35 minutes.
    captured = await _capture_replay(adapter, [
        _message(f"m{i:02d}", 1000 + i * 100, f"update {i}") for i in range(22)
    ])

    assert len(captured) == 3
    sizes = [
        len(e.raw_message["sourceMessageIds"]) if e.raw_message.get("bundle") else 1
        for e in captured
    ]
    assert sizes == [10, 10, 2]
    assert captured[0].raw_message["sourceMessageIds"][0] == "m00"
    assert captured[1].raw_message["sourceMessageIds"][0] == "m10"
    assert captured[2].raw_message["sourceMessageIds"][0] == "m20"


@pytest.mark.asyncio
async def test_replay_bundle_still_splits_on_timestamp_gap():
    """Original-timestamp gap > debounce starts a new turn even under the cap."""
    adapter = _adapter({
        "require_mention": True,
        "ingest_chats": [CHAT],
        "turn_policy": {
            CHAT: {"debounce_seconds": 300, "direct_mention_immediate": False},
        },
    })

    captured = await _capture_replay(adapter, [
        _message("m1", 1000, "first"),
        _message("m2", 1100, "second"),
        _message("m3", 9000, "after a 2h-style gap"),
        _message("m4", 9100, "fourth"),
    ])

    assert len(captured) == 2
    assert captured[0].raw_message["sourceMessageIds"] == ["m1", "m2"]
    assert captured[1].raw_message["sourceMessageIds"] == ["m3", "m4"]
