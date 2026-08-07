"""Tests for opt-in staging of static Telegram location pins."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource


def _event(
    text: str,
    *,
    chat_id: str = "1001",
    user_id: str = "2002",
    chat_type: str = "dm",
    thread_id: str | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            thread_id=thread_id,
        ),
    )


def _adapter(*, mode: str = "stage_next", ttl: float = 300.0):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={
            "location_pin_mode": mode,
            "location_pin_ttl_seconds": ttl,
        },
    )
    adapter._pending_location_context = {}
    adapter._pending_location_expiry_handles = {}
    adapter._pending_location_ack_intents = {}
    adapter.set_authorization_check(lambda user_id, chat_type, chat_id: True)
    adapter.handle_message = AsyncMock()
    adapter._text_batch_key = lambda event: "\x1f".join((
        str(event.source.chat_id or ""),
        str(event.source.thread_id or ""),
    ))
    adapter._is_user_authorized_from_message = lambda msg: True
    adapter._should_drop_delayed_delivery = lambda: False
    adapter._should_process_message = lambda msg, **kwargs: True
    adapter._effective_update_message = lambda update: update.effective_message
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    adapter._build_message_event = lambda msg, message_type, update_id=None: (
        MessageEvent(
            text="",
            message_type=message_type,
            source=SessionSource(
                platform=Platform.TELEGRAM,
                chat_id=str(msg.chat.id),
                chat_type=getattr(msg.chat, "type", "dm"),
                user_id=(str(msg.from_user.id) if msg.from_user is not None else None),
                thread_id=(
                    str(msg.message_thread_id)
                    if getattr(msg, "message_thread_id", None) is not None
                    else None
                ),
            ),
            raw_message=msg,
        )
    )
    return adapter


def _message(
    *,
    live_period=None,
    chat_id=1001,
    user_id=2002,
    chat_type="dm",
    thread_id=None,
    latitude=12.3456,
    longitude=65.4321,
    text="message",
):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id),
        message_thread_id=thread_id,
        venue=None,
        location=SimpleNamespace(
            latitude=latitude,
            longitude=longitude,
            live_period=live_period,
        ),
        text=text,
        reply_text=AsyncMock(),
    )


def _update(msg, *, edited=False):
    return SimpleNamespace(
        effective_message=msg,
        message=None if edited else msg,
        edited_message=msg if edited else None,
        edited_channel_post=None,
        update_id=77,
    )


@pytest.mark.asyncio
async def test_static_pin_is_staged_and_acknowledged_without_agent_call(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic", lambda: 100.0
    )
    msg = _message()

    await adapter._handle_location_message(_update(msg), None)

    adapter.handle_message.assert_not_called()
    msg.reply_text.assert_awaited_once_with(
        "Location will be attached to your next message."
    )
    assert len(adapter._pending_location_context) == 1


@pytest.mark.asyncio
async def test_next_text_consumes_staged_pin(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic", lambda: 100.0
    )
    await adapter._handle_location_message(_update(_message()), None)

    request = _event("Find a cafe nearby")
    adapter._attach_staged_location(request)

    assert "Find a cafe nearby" in request.text
    assert "latitude: 12.3456" in request.text
    assert "longitude: 65.4321" in request.text
    assert adapter._pending_location_context == {}


@pytest.mark.asyncio
async def test_staged_pin_expires(monkeypatch):
    adapter = _adapter(ttl=60.0)
    clock = {"now": 100.0}
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic",
        lambda: clock["now"],
    )
    await adapter._handle_location_message(_update(_message()), None)

    clock["now"] = 161.0
    request = _event("What is nearby?")
    adapter._attach_staged_location(request)

    assert request.text == "What is nearby?"
    assert adapter._pending_location_context == {}


@pytest.mark.asyncio
async def test_expiry_callback_removes_pin_without_another_message(monkeypatch):
    adapter = _adapter(ttl=60.0)
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic", lambda: 100.0
    )
    await adapter._handle_location_message(_update(_message()), None)
    key = next(iter(adapter._pending_location_context))
    token = adapter._pending_location_context[key][0]

    adapter._expire_staged_location(key, token)

    assert adapter._pending_location_context == {}
    assert adapter._pending_location_expiry_handles == {}


def test_stale_expiry_callback_does_not_remove_newer_pin():
    adapter = _adapter()
    key = "scope"
    stale_token = object()
    newer_token = object()
    newer_handle = SimpleNamespace(cancel=lambda: None)
    adapter._pending_location_context[key] = (newer_token, 200.0, "new location")
    adapter._pending_location_expiry_handles[key] = newer_handle

    adapter._expire_staged_location(key, stale_token)

    assert adapter._pending_location_context[key] == (
        newer_token,
        200.0,
        "new location",
    )
    assert adapter._pending_location_expiry_handles[key] is newer_handle


@pytest.mark.asyncio
async def test_staged_pin_is_scoped_by_sender_chat_and_topic(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic", lambda: 100.0
    )
    await adapter._handle_location_message(
        _update(_message(chat_id=-1001, user_id=2002, chat_type="group", thread_id=7)),
        None,
    )

    for request in (
        _event(
            "Nearby?", chat_id="-1001", user_id="9999", chat_type="group", thread_id="7"
        ),
        _event(
            "Nearby?", chat_id="-2002", user_id="2002", chat_type="group", thread_id="7"
        ),
        _event(
            "Nearby?", chat_id="-1001", user_id="2002", chat_type="group", thread_id="8"
        ),
    ):
        adapter._attach_staged_location(request)
        assert request.text == "Nearby?"

    matching = _event(
        "Nearby?",
        chat_id="-1001",
        user_id="2002",
        chat_type="group",
        thread_id="7",
    )
    adapter._attach_staged_location(matching)
    assert "latitude: 12.3456" in matching.text
    assert adapter._pending_location_context == {}


@pytest.mark.asyncio
async def test_stage_mode_accepts_authorized_group_pin_without_mention(monkeypatch):
    adapter = _adapter()
    adapter._should_process_message = lambda msg, **kwargs: bool(
        kwargs.get("ignore_trigger")
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic", lambda: 100.0
    )
    msg = _message(chat_id=-1001, chat_type="group")

    await adapter._handle_location_message(_update(msg), None)

    adapter.handle_message.assert_not_called()
    msg.reply_text.assert_awaited_once()
    assert len(adapter._pending_location_context) == 1


@pytest.mark.asyncio
async def test_default_mode_preserves_conversational_pin_behavior():
    adapter = _adapter(mode="conversational")
    msg = _message()

    await adapter._handle_location_message(_update(msg), None)

    msg.reply_text.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert "latitude: 12.3456" in event.text
    assert "Ask what they'd like to find nearby" in event.text
    assert adapter._pending_location_context == {}


@pytest.mark.asyncio
async def test_unpaired_sender_is_not_staged_or_acknowledged():
    adapter = _adapter()
    adapter.set_authorization_check(lambda user_id, chat_type, chat_id: False)
    msg = _message()

    await adapter._handle_location_message(_update(msg), None)

    msg.reply_text.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    assert adapter._pending_location_context == {}


@pytest.mark.asyncio
async def test_identityless_group_sender_is_not_staged_or_acknowledged():
    adapter = _adapter()
    msg = _message(chat_id=-1001, chat_type="group")
    msg.from_user = None
    msg.sender_chat = SimpleNamespace(id=-2002)

    await adapter._handle_location_message(_update(msg), None)

    msg.reply_text.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    assert adapter._pending_location_context == {}


def test_multiplex_profile_authorization_callback_is_supported():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: source.profile == "secondary"
    callback = runner._make_adapter_auth_check(
        Platform.TELEGRAM,
        profile_name="secondary",
    )
    adapter = _adapter()
    adapter.set_authorization_check(callback)

    assert adapter._is_authorized_for_location_staging(_event("")) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True])
async def test_live_location_updates_remain_conversational_in_stage_mode(edited):
    adapter = _adapter()
    msg = _message(live_period=900)

    await adapter._handle_location_message(_update(msg, edited=edited), None)

    msg.reply_text.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    assert adapter._pending_location_context == {}


@pytest.mark.asyncio
async def test_acknowledgement_failure_rolls_back_staged_pin(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic", lambda: 100.0
    )
    msg = _message()
    msg.reply_text.side_effect = RuntimeError("transport unavailable")

    await adapter._handle_location_message(_update(msg), None)

    assert adapter._pending_location_context == {}
    assert adapter._pending_location_expiry_handles == {}
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_pin_is_not_staged_until_acknowledgement_succeeds():
    adapter = _adapter()
    ack_started = asyncio.Event()
    release_ack = asyncio.Event()
    msg = _message()

    async def delayed_ack(*args, **kwargs):
        ack_started.set()
        await release_ack.wait()

    msg.reply_text.side_effect = delayed_ack
    task = asyncio.create_task(adapter._handle_location_message(_update(msg), None))
    await ack_started.wait()
    assert adapter._pending_location_context == {}

    release_ack.set()
    await task
    assert len(adapter._pending_location_context) == 1


@pytest.mark.asyncio
async def test_failed_delayed_ack_does_not_remove_newer_pin():
    adapter = _adapter()
    ack_started = asyncio.Event()
    release_ack = asyncio.Event()
    older = _message()
    newer = _message()

    async def delayed_failure(*args, **kwargs):
        ack_started.set()
        await release_ack.wait()
        raise RuntimeError("transport unavailable")

    older.reply_text.side_effect = delayed_failure
    older_task = asyncio.create_task(
        adapter._handle_location_message(_update(older), None)
    )
    await ack_started.wait()
    await adapter._handle_location_message(_update(newer), None)
    newer_pending = dict(adapter._pending_location_context)

    release_ack.set()
    await older_task
    assert adapter._pending_location_context == newer_pending


@pytest.mark.asyncio
async def test_delayed_older_ack_cannot_replace_newer_pin():
    adapter = _adapter()
    ack_started = asyncio.Event()
    release_ack = asyncio.Event()
    older = _message(latitude=11.1111, longitude=22.2222)
    newer = _message(latitude=88.8888, longitude=99.9999)

    async def delayed_success(*args, **kwargs):
        ack_started.set()
        await release_ack.wait()

    older.reply_text.side_effect = delayed_success
    older_task = asyncio.create_task(
        adapter._handle_location_message(_update(older), None)
    )
    await ack_started.wait()
    await adapter._handle_location_message(_update(newer), None)

    release_ack.set()
    await older_task
    pending_text = next(iter(adapter._pending_location_context.values()))[2]
    assert "latitude: 88.8888" in pending_text
    assert "latitude: 11.1111" not in pending_text


@pytest.mark.asyncio
async def test_location_pin_is_dropped_after_disconnect_fence():
    adapter = _adapter()
    adapter._should_drop_delayed_delivery = lambda: True
    msg = _message()

    await adapter._handle_location_message(_update(msg), None)

    msg.reply_text.assert_not_awaited()
    assert adapter._pending_location_context == {}
    assert adapter._pending_location_ack_intents == {}
    assert adapter._pending_location_expiry_handles == {}


@pytest.mark.asyncio
async def test_disconnect_during_ack_does_not_stage_location():
    adapter = _adapter()
    disconnecting = False
    adapter._should_drop_delayed_delivery = lambda: disconnecting
    ack_started = asyncio.Event()
    release_ack = asyncio.Event()
    msg = _message()

    async def delayed_ack(*args, **kwargs):
        ack_started.set()
        await release_ack.wait()

    msg.reply_text.side_effect = delayed_ack
    task = asyncio.create_task(adapter._handle_location_message(_update(msg), None))
    await ack_started.wait()
    disconnecting = True
    release_ack.set()
    await task

    assert adapter._pending_location_context == {}
    assert adapter._pending_location_ack_intents == {}
    assert adapter._pending_location_expiry_handles == {}


def test_stage_mode_text_batches_are_always_sender_scoped():
    adapter = _adapter()
    adapter._text_batch_key = adapter.__class__._text_batch_key.__get__(adapter)
    adapter._apply_topic_recovery = lambda event: None
    adapter.config.extra.update(
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )

    alice = _event(
        "first",
        chat_id="-1001",
        user_id="2002",
        chat_type="forum",
        thread_id="7",
    )
    bob = _event(
        "second",
        chat_id="-1001",
        user_id="3003",
        chat_type="forum",
        thread_id="7",
    )

    assert adapter._text_batch_key(alice) != adapter._text_batch_key(bob)


@pytest.mark.asyncio
async def test_stage_mode_observed_group_text_batches_preserve_sender_scope(monkeypatch):
    adapter = _adapter()
    adapter._text_batch_key = adapter.__class__._text_batch_key.__get__(adapter)
    adapter._apply_telegram_group_observe_attribution = (
        adapter.__class__._apply_telegram_group_observe_attribution.__get__(adapter)
    )
    adapter._apply_topic_recovery = lambda event: None
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 60.0
    adapter._text_batch_split_delay_seconds = 60.0
    adapter._clean_bot_trigger_text = lambda text: text
    adapter._cache_replied_media = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter._bot = SimpleNamespace(username="hermes_bot", id=999)
    adapter.config.extra.update(
        observe_unmentioned_group_messages=True,
        group_allowed_chats=["-1001"],
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic", lambda: 100.0
    )

    staged_for_alice = _event(
        "",
        chat_id="-1001",
        user_id="2002",
        chat_type="group",
    )
    stage_key = adapter._location_stage_key(staged_for_alice)
    adapter._pending_location_context[stage_key] = (
        object(),
        100.0,
        "[Alice's staged location]",
    )

    alice = _message(
        chat_id=-1001,
        user_id=2002,
        chat_type="group",
        text="Find a cafe nearby",
    )
    bob = _message(
        chat_id=-1001,
        user_id=3003,
        chat_type="group",
        text="What is the weather?",
    )

    await adapter._handle_text_message(_update(alice), None)
    await adapter._handle_text_message(_update(bob), None)

    assert len(adapter._pending_text_batches) == 2
    pending = list(adapter._pending_text_batches.values())
    assert all(event.source.user_id is None for event in pending)
    assert sum("Alice's staged location" in (event.text or "") for event in pending) == 1
    assert not any(
        "Find a cafe nearby" in (event.text or "")
        and "What is the weather?" in (event.text or "")
        for event in pending
    )

    tasks = list(adapter._pending_text_batch_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
