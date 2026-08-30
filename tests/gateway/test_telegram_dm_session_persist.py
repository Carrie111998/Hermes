"""Telegram DM/command persistence into state.db via SessionStore.

Regression coverage for the fix that calls ``get_or_create_session`` +
``append_to_transcript`` from ``_handle_text_message`` / ``_handle_command``
so private-chat traffic appears in ``hermes sessions list``, ``/resume``,
and the WebUI (group observe already persisted; DMs previously only wrote
``.jsonl``).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig


class _FakeSessionEntry:
    session_id = "telegram-dm-session"


class _FakeSessionStore:
    def __init__(self):
        self.sources = []
        self.messages = []

    def get_or_create_session(self, source):
        self.sources.append(source)
        return _FakeSessionEntry()

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.messages.append((session_id, message, skip_db))


class _RaisingOnCreateStore(_FakeSessionStore):
    def get_or_create_session(self, source):
        raise RuntimeError("store create failed")


class _RaisingOnAppendStore(_FakeSessionStore):
    def append_to_transcript(self, session_id, message, skip_db=False):
        raise RuntimeError("store append failed")


def _make_adapter(*, allow_from=None, **extra_overrides):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    extra = {}
    if allow_from is not None:
        extra["allow_from"] = allow_from
    extra.update(extra_overrides)

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="test_bot")
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.05
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter.handle_message = AsyncMock()
    return adapter


def _dm_message(text="hello", *, from_user_id=111, message_id=43):
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(
            id=from_user_id,
            type="private",
            full_name="Alice Example",
            title=None,
            is_forum=False,
        ),
        from_user=SimpleNamespace(
            id=from_user_id,
            full_name="Alice Example",
            first_name="Alice",
        ),
        reply_to_message=None,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
    )


def _group_message(text="side chatter", *, chat_id=-100, from_user_id=111):
    return SimpleNamespace(
        message_id=42,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(
            id=chat_id, type="group", title="Test Group", is_forum=False
        ),
        from_user=SimpleNamespace(
            id=from_user_id,
            full_name="Alice Example",
            first_name="Alice",
        ),
        reply_to_message=None,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
    )


def _make_update(message):
    return SimpleNamespace(update_id=1, message=message, effective_message=None)


def _assert_user_persist(store, *, content, message_id, chat_type="dm"):
    assert len(store.sources) == 1
    assert store.sources[0].chat_type == chat_type
    assert len(store.messages) == 1
    session_id, message, skip_db = store.messages[0]
    assert session_id == "telegram-dm-session"
    assert skip_db is False
    assert message["role"] == "user"
    assert message["content"] == content
    assert message["message_id"] == str(message_id)
    assert "observed" not in message
    assert "timestamp" in message


@pytest.mark.asyncio
async def test_dm_text_persists_to_session_store():
    adapter = _make_adapter()
    store = _FakeSessionStore()
    adapter._session_store = store

    await adapter._handle_text_message(
        _make_update(_dm_message("hello from dm", message_id=43)),
        SimpleNamespace(),
    )

    _assert_user_persist(store, content="hello from dm", message_id=43)
    assert store.sources[0].chat_id == "111"
    assert store.sources[0].user_id == "111"
    # Enqueued for batching — not dispatched synchronously.
    assert len(adapter._pending_text_batches) == 1
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_short_command_persists_and_dispatches():
    adapter = _make_adapter()
    store = _FakeSessionStore()
    adapter._session_store = store

    await adapter._handle_command(
        _make_update(_dm_message("/status", message_id=44)),
        SimpleNamespace(),
    )

    _assert_user_persist(store, content="/status", message_id=44)
    adapter.handle_message.assert_awaited_once()
    assert not adapter._pending_text_batches


@pytest.mark.asyncio
async def test_dm_near_limit_command_persists_without_immediate_dispatch():
    adapter = _make_adapter()
    store = _FakeSessionStore()
    adapter._session_store = store
    long_cmd = "/queue " + "x" * 4090

    await adapter._handle_command(
        _make_update(_dm_message(long_cmd, message_id=45)),
        SimpleNamespace(),
    )

    _assert_user_persist(store, content=long_cmd, message_id=45)
    adapter.handle_message.assert_not_awaited()
    assert len(adapter._pending_text_batches) == 1


@pytest.mark.asyncio
async def test_dm_text_without_session_store_still_enqueues():
    adapter = _make_adapter()
    # No _session_store attribute at all.
    assert not hasattr(adapter, "_session_store")

    await adapter._handle_text_message(
        _make_update(_dm_message("still works")),
        SimpleNamespace(),
    )

    assert len(adapter._pending_text_batches) == 1


@pytest.mark.asyncio
async def test_dm_command_with_none_session_store_still_dispatches():
    adapter = _make_adapter()
    adapter._session_store = None

    await adapter._handle_command(
        _make_update(_dm_message("/help")),
        SimpleNamespace(),
    )

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_dm_text_store_create_failure_is_non_fatal():
    adapter = _make_adapter()
    adapter._session_store = _RaisingOnCreateStore()

    await adapter._handle_text_message(
        _make_update(_dm_message("enqueue anyway")),
        SimpleNamespace(),
    )

    assert len(adapter._pending_text_batches) == 1


@pytest.mark.asyncio
async def test_dm_command_store_append_failure_is_non_fatal():
    adapter = _make_adapter()
    adapter._session_store = _RaisingOnAppendStore()

    await adapter._handle_command(
        _make_update(_dm_message("/stop")),
        SimpleNamespace(),
    )

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unauthorized_dm_text_does_not_persist():
    adapter = _make_adapter(allow_from=["222"])
    store = _FakeSessionStore()
    adapter._session_store = store

    await adapter._handle_text_message(
        _make_update(_dm_message("blocked", from_user_id=111)),
        SimpleNamespace(),
    )

    assert store.sources == []
    assert store.messages == []
    assert not adapter._pending_text_batches
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_dm_command_does_not_persist():
    adapter = _make_adapter(allow_from=["222"])
    store = _FakeSessionStore()
    adapter._session_store = store

    await adapter._handle_command(
        _make_update(_dm_message("/status", from_user_id=111)),
        SimpleNamespace(),
    )

    assert store.sources == []
    assert store.messages == []
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_text_early_return_skips_store():
    adapter = _make_adapter()
    store = _FakeSessionStore()
    adapter._session_store = store
    msg = _dm_message("")
    msg.text = ""

    await adapter._handle_text_message(_make_update(msg), SimpleNamespace())

    assert store.sources == []
    assert store.messages == []
    assert not adapter._pending_text_batches


@pytest.mark.asyncio
async def test_missing_message_early_return_skips_store():
    adapter = _make_adapter()
    store = _FakeSessionStore()
    adapter._session_store = store
    update = SimpleNamespace(update_id=1, message=None, effective_message=None)

    await adapter._handle_command(update, SimpleNamespace())

    assert store.sources == []
    assert store.messages == []
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_observe_still_persists_with_observed_flag():
    """Focused regression: unmentioned group observe path remains intact."""
    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        observe_unmentioned_group_messages=True,
    )
    # Force-authorize so observe gating (not allowlist) is under test.
    adapter._is_callback_user_authorized = lambda user_id, **_kw: True
    store = _FakeSessionStore()
    adapter._session_store = store

    await adapter._handle_text_message(
        _make_update(_group_message("side chatter")),
        SimpleNamespace(),
    )

    adapter.handle_message.assert_not_awaited()
    assert len(store.messages) == 1
    session_id, message, skip_db = store.messages[0]
    assert session_id == "telegram-dm-session"
    assert skip_db is False
    assert message["role"] == "user"
    assert message["observed"] is True
    assert message["message_id"] == "42"
    assert "side chatter" in message["content"]
    assert store.sources[0].chat_type == "group"


@pytest.mark.asyncio
async def test_dm_persist_does_not_set_observed():
    """DM path must not mark entries as observed (group-observe only)."""
    adapter = _make_adapter()
    store = _FakeSessionStore()
    adapter._session_store = store

    await adapter._handle_text_message(
        _make_update(_dm_message("plain dm")),
        SimpleNamespace(),
    )

    _, message, _ = store.messages[0]
    assert "observed" not in message
