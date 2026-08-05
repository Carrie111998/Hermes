"""Regression tests for the s5 god-file extraction (wave 1, mixin lifts).

Covers the PURE helpers that moved out of ``plugins/platforms/telegram/
adapter.py`` into the ``*_mixin`` modules (mentions, group observe, media
cache, inbound handlers, text batching, media batching, dm topics, rich-text
flattening, reactions) during the god-file decomposition campaign.  The
point of these tests is twofold:

1. Behavior parity: the moved helpers still answer the same questions
   (mention extraction, group-chat observation gates, media classification,
   DM-topic caching, rich-reply plaintext flattening, reaction toggles) now
   that they resolve via the mixin MRO instead of living on
   ``TelegramAdapter`` directly.
2. Extraction integrity: the methods are actually provided by the mixins —
   the adapter no longer defines them, so a regression to the extraction
   (e.g. a duplicated definition shadowing the mixin) is caught here, and
   module-level helpers/constants that moved (``_redact_telegram_error_text``
   stays shared; ``_TELEGRAM_IMAGE_*`` moved) resolve to exactly one home.

Pure helpers only: no network, no PTB application, no event loop beyond the
bare ``object.__new__`` adapter seam the rest of the gateway test suite uses.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


def _make_adapter(**extra):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM  # drives the read-only .name property
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="hermes_test_bot")
    return adapter


def _source(chat_id="123", chat_type="private", user_id="42", thread_id=None, user_name="TestUser"):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name=user_name,
        thread_id=thread_id,
    )


def _entity(etype, offset, length):
    return SimpleNamespace(type=etype, offset=offset, length=length)


def _msg(text=None, caption=None, entities=None, caption_entities=None, chat=None, **extra):
    msg = SimpleNamespace(
        message_id=1,
        text=text,
        caption=caption,
        entities=entities or [],
        caption_entities=caption_entities or [],
        message_thread_id=None,
        chat=chat or SimpleNamespace(id=-100, type="group"),
        from_user=SimpleNamespace(id=111, first_name="Alice"),
        reply_to_message=None,
        sticker=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        media_group_id=None,
        location=None,
        venue=None,
    )
    for key, value in extra.items():
        setattr(msg, key, value)
    return msg


# ---------------------------------------------------------------------------
# Extraction integrity (MRO + single-home helpers/constants)
# ---------------------------------------------------------------------------


def test_all_s5_mixins_in_mro_before_base():
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from plugins.platforms.telegram.dm_topics_cache_mixin import DmTopicsCacheMixin
    from plugins.platforms.telegram.group_observe_mixin import GroupObserveMixin
    from plugins.platforms.telegram.inbound_handlers_mixin import InboundHandlersMixin
    from plugins.platforms.telegram.media_batching_mixin import MediaBatchingMixin
    from plugins.platforms.telegram.media_cache_mixin import MediaCacheMixin
    from plugins.platforms.telegram.mentions_mixin import MentionsMixin
    from plugins.platforms.telegram.reactions_mixin import ReactionsMixin
    from plugins.platforms.telegram.rich_text_flatten_mixin import RichTextFlattenMixin
    from plugins.platforms.telegram.text_batching_mixin import TextBatchingMixin

    mro = TelegramAdapter.__mro__
    from gateway.platforms.base import BasePlatformAdapter

    base_idx = mro.index(BasePlatformAdapter)
    for mixin in (
        MentionsMixin,
        GroupObserveMixin,
        MediaCacheMixin,
        InboundHandlersMixin,
        TextBatchingMixin,
        MediaBatchingMixin,
        DmTopicsCacheMixin,
        RichTextFlattenMixin,
        ReactionsMixin,
    ):
        assert mixin in mro, f"{mixin.__name__} missing from TelegramAdapter.__mro__"
        assert mro.index(mixin) < base_idx, (
            f"{mixin.__name__} must precede BasePlatformAdapter in the MRO"
        )


def test_moved_methods_not_defined_on_adapter_class():
    """The lifted defs must live on the mixins, not on TelegramAdapter itself."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    own = set(TelegramAdapter.__dict__)
    for name in (
        "_extract_bot_mention_usernames", "_message_mentions_bot",
        "_schedule_bot_identity_recheck", "_explicit_bot_mentions_exclude_self",
        "_message_matches_mention_patterns", "_is_guest_mention",
        "_clean_bot_trigger_text",
        "_should_observe_unmentioned_group_message",
        "_telegram_group_observe_shared_source",
        "_telegram_group_observe_attributed_text",
        "_telegram_group_observe_channel_prompt",
        "_apply_telegram_group_observe_attribution",
        "_observe_unmentioned_group_message",
        "_media_message_type", "_cache_observed_media", "_cache_replied_media",
        "_observed_media_source", "_append_observed_note",
        "_surface_media_cache_failure",
        "_ensure_forum_commands", "_effective_update_message",
        "_handle_text_message", "_handle_command", "_handle_location_message",
        "_handle_media_message", "_handle_sticker",
        "_text_batch_key", "_enqueue_text_event", "_flush_text_batch",
        "_photo_batch_key", "_flush_photo_batch", "_enqueue_photo_event",
        "_queue_media_group_event", "_flush_media_group_event",
        "_reload_dm_topics_from_config", "_get_dm_topic_info",
        "_cache_dm_topic_from_message",
        "_flatten_rich_inline_text", "_flatten_rich_blocks",
        "_extract_rich_reply_text",
        "_reactions_enabled", "_set_reaction", "_clear_reactions",
        "on_processing_start", "on_processing_complete",
    ):
        assert name not in own, f"{name} still defined directly on TelegramAdapter"
        assert hasattr(TelegramAdapter, name), f"{name} not resolvable via MRO"


def test_shared_helper_single_home():
    """_redact_telegram_error_text stays in adapter; mixins re-import it.

    Compared by code object rather than object identity because
    tests/gateway/test_dm_topics.py force-reimports the adapter module
    mid-session (sys.modules.pop + reimport), which legitimately creates a
    second function object from the same source; the invariant that matters
    is that both bindings come from the adapter module's source.
    """
    from plugins.platforms.telegram import adapter as adapter_mod
    from plugins.platforms.telegram.media_cache_mixin import _redact_telegram_error_text as a
    from plugins.platforms.telegram.reactions_mixin import _redact_telegram_error_text as b

    assert a.__module__ == adapter_mod.__name__
    assert b.__module__ == adapter_mod.__name__
    assert a.__code__ == adapter_mod._redact_telegram_error_text.__code__
    assert b.__code__ == adapter_mod._redact_telegram_error_text.__code__


def test_image_constants_moved_to_inbound_handlers_mixin():
    from plugins.platforms.telegram import adapter as adapter_mod
    from plugins.platforms.telegram.inbound_handlers_mixin import (
        _TELEGRAM_IMAGE_EXTENSIONS,
        _TELEGRAM_IMAGE_EXT_TO_MIME,
        _TELEGRAM_IMAGE_MIME_TO_EXT,
    )

    assert not hasattr(adapter_mod, "_TELEGRAM_IMAGE_EXTENSIONS")
    assert ".jpg" in _TELEGRAM_IMAGE_EXTENSIONS
    assert _TELEGRAM_IMAGE_MIME_TO_EXT["image/png"] == ".png"
    assert _TELEGRAM_IMAGE_EXT_TO_MIME[".png"] == "image/png"


def test_inbound_handlers_cache_helpers_delegate_through_adapter():
    """Existing tests patch adapter.cache_image_from_bytes; the lifted media
    handler must observe that patch through the adapter-module delegation.

    The patch is applied via the mixin's own ``_adapter_mod`` reference so
    the test stays valid even when tests/gateway/test_dm_topics.py has
    force-reimported the adapter module mid-session (the delegation contract
    is: the lifted handler resolves the helper through the adapter module it
    was bound to at mixin import time)."""
    from unittest.mock import patch

    from plugins.platforms.telegram.inbound_handlers_mixin import (
        _adapter_mod,
        cache_audio_from_bytes,
        cache_image_from_bytes,
        cache_video_from_bytes,
    )

    with patch.object(
        _adapter_mod, "cache_image_from_bytes", return_value="/tmp/patched.jpg"
    ) as mocked:
        assert cache_image_from_bytes(b"data", ext=".jpg") == "/tmp/patched.jpg"
        mocked.assert_called_once_with(b"data", ext=".jpg")
    with patch.object(
        _adapter_mod, "cache_audio_from_bytes", return_value="/tmp/patched.ogg"
    ):
        assert cache_audio_from_bytes(b"data", ext=".ogg") == "/tmp/patched.ogg"
    with patch.object(
        _adapter_mod, "cache_video_from_bytes", return_value="/tmp/patched.mp4"
    ):
        assert cache_video_from_bytes(b"data", ext=".mp4") == "/tmp/patched.mp4"


# ---------------------------------------------------------------------------
# Mentions cluster (mentions_mixin)
# ---------------------------------------------------------------------------


def test_extract_bot_mention_usernames_entity_mentions():
    adapter = _make_adapter()
    msg = _msg(
        text="hello @other_bot and @human",
        entities=[
            _entity("mention", 6, 10),   # @other_bot
            _entity("mention", 21, 7),   # @human (not bot-shaped)
        ],
    )
    found = adapter._extract_bot_mention_usernames(msg, "hermes_test_bot")
    assert found == {"other_bot"}


def test_extract_bot_mention_usernames_own_collectible_handle():
    """A collectible own handle (no ...bot suffix) still counts as us."""
    adapter = _make_adapter()
    msg = _msg(
        text="hi @jarvis",
        entities=[_entity("mention", 3, 7)],
    )
    found = adapter._extract_bot_mention_usernames(msg, "jarvis")
    assert found == {"jarvis"}


def test_extract_bot_mention_usernames_bot_command_target():
    adapter = _make_adapter()
    msg = _msg(
        text="/start@other_bot",
        entities=[_entity("bot_command", 0, 16)],
    )
    found = adapter._extract_bot_mention_usernames(msg, "hermes_test_bot")
    assert found == {"other_bot"}


def test_extract_bot_mention_usernames_raw_text_fallback():
    adapter = _make_adapter()
    msg = _msg(text="ping @some_bot now", entities=[])
    found = adapter._extract_bot_mention_usernames(msg, "hermes_test_bot")
    assert found == {"some_bot"}


def test_message_mentions_bot_mention_entity():
    adapter = _make_adapter()
    msg = _msg(
        text="please @hermes_test_bot help",
        entities=[_entity("mention", 7, 16)],
    )
    assert adapter._message_mentions_bot(msg) is True


def test_message_mentions_bot_other_bot_not_us():
    adapter = _make_adapter()
    msg = _msg(
        text="@other_bot help",
        entities=[_entity("mention", 0, 10)],
    )
    assert adapter._message_mentions_bot(msg) is False


def test_message_mentions_bot_command_suffix():
    adapter = _make_adapter()
    msg = _msg(
        text="/help@hermes_test_bot",
        entities=[_entity("bot_command", 0, 21)],
    )
    assert adapter._message_mentions_bot(msg) is True


def test_clean_bot_trigger_text_strips_own_mention():
    adapter = _make_adapter()
    assert adapter._clean_bot_trigger_text("@hermes_test_bot hello") == "hello"
    assert adapter._clean_bot_trigger_text("hello") == "hello"
    assert adapter._clean_bot_trigger_text(None) is None
    assert adapter._clean_bot_trigger_text("@HERMES_TEST_BOT, hi") == "hi"


def test_message_matches_mention_patterns():
    import re
    adapter = _make_adapter()
    adapter._mention_patterns = [re.compile(r"\bwake\b", re.I)]
    assert adapter._message_matches_mention_patterns(_msg(text="wake up")) is True
    assert adapter._message_matches_mention_patterns(_msg(text="sleep")) is False
    adapter._mention_patterns = []
    assert adapter._message_matches_mention_patterns(_msg(text="wake")) is False


def test_explicit_bot_mentions_exclude_self():
    adapter = _make_adapter()
    # "other_bot" and "third_bot" are bot-shaped (end in "bot"); a human
    # @handle never suppresses this bot.
    msg = _msg(
        text="@other_bot hi @third_bot",
        entities=[_entity("mention", 0, 10), _entity("mention", 14, 10)],
    )
    assert adapter._explicit_bot_mentions_exclude_self(msg) is True
    own = _msg(
        text="@hermes_test_bot hi",
        entities=[_entity("mention", 0, 16)],
    )
    assert adapter._explicit_bot_mentions_exclude_self(own) is False


# ---------------------------------------------------------------------------
# Group-observe cluster (group_observe_mixin)
# ---------------------------------------------------------------------------


def test_group_observe_shared_source_anonymises():
    from plugins.platforms.telegram.group_observe_mixin import GroupObserveMixin

    source = _source(user_id="42", user_name="TestUser")
    shared = GroupObserveMixin._telegram_group_observe_shared_source(None, source)
    assert shared.user_id is None
    assert shared.user_name is None
    assert shared.user_id_alt is None
    assert shared.chat_id == "123"


def test_group_observe_attributed_text():
    from plugins.platforms.telegram.group_observe_mixin import GroupObserveMixin

    event = MessageEvent(
        text="hello", message_type=MessageType.TEXT, source=_source()
    )
    out = GroupObserveMixin._telegram_group_observe_attributed_text(None, event)
    assert out.startswith("[TestUser|42]\n")


def test_group_observe_channel_prompt_mentions_identity():
    adapter = _make_adapter()
    prompt = adapter._telegram_group_observe_channel_prompt()
    assert "hermes_test_bot" in prompt
    assert "user_id=999" in prompt


def test_apply_telegram_group_observe_attribution_anonymises_group_turn():
    adapter = _make_adapter(observe_unmentioned_group_messages=True)
    raw = _msg(text="hello", chat=SimpleNamespace(id=-100, type="group"))
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=_source(chat_id="-100", chat_type="group"),
        raw_message=raw,
    )
    adapter._telegram_observe_allowed_chats = lambda: {"-100"}
    out = adapter._apply_telegram_group_observe_attribution(event)
    assert out.source.user_id is None
    assert out.text.startswith("[TestUser|42]")


def test_should_observe_unmentioned_group_message_gate():
    adapter = _make_adapter(
        observe_unmentioned_group_messages=True,
        require_mention=True,
    )
    adapter._mention_patterns = []
    # allowlist empty -> never observed
    msg = _msg(text="hi", chat=SimpleNamespace(id=-100, type="group"))
    assert adapter._should_observe_unmentioned_group_message(msg) is False
    # allowlisted chat + mention gate -> observed
    adapter._telegram_observe_allowed_chats = lambda: {"-100"}
    adapter._telegram_free_response_chats = lambda: set()
    adapter._telegram_is_free_response_topic = lambda m: False
    adapter._telegram_ignored_threads = lambda: set()
    adapter._telegram_allowed_topics = lambda: set()
    assert adapter._should_observe_unmentioned_group_message(msg) is True
    # message that mentions the bot is a request, not observed
    mentioned = _msg(
        text="@hermes_test_bot hi",
        entities=[_entity("mention", 0, 16)],
        chat=SimpleNamespace(id=-100, type="group"),
    )
    assert adapter._should_observe_unmentioned_group_message(mentioned) is False


# ---------------------------------------------------------------------------
# Media-cache cluster (media_cache_mixin)
# ---------------------------------------------------------------------------


def test_media_message_type_classification():
    adapter = _make_adapter()
    assert adapter._media_message_type(_msg(sticker=object())) is MessageType.STICKER
    assert adapter._media_message_type(_msg(photo=[object()])) is MessageType.PHOTO
    assert adapter._media_message_type(_msg(video=object())) is MessageType.VIDEO
    assert adapter._media_message_type(_msg(audio=object())) is MessageType.AUDIO
    assert adapter._media_message_type(_msg(voice=object())) is MessageType.VOICE
    assert adapter._media_message_type(_msg()) is MessageType.DOCUMENT


def test_observed_media_source_variants():
    adapter = _make_adapter()
    photo = SimpleNamespace(photo=[SimpleNamespace(file_size=5)])
    source, filename, mime, kind = adapter._observed_media_source(photo)
    assert kind == "image" and source is not None
    doc = _msg(document=SimpleNamespace(file_name="a.pdf", mime_type="application/pdf"))
    source, filename, mime, kind = adapter._observed_media_source(doc)
    assert filename == "a.pdf" and mime == "application/pdf" and kind is None
    assert adapter._observed_media_source(_msg())[0] is None


def test_append_observed_note():
    adapter = _make_adapter()
    assert adapter._append_observed_note(None, "n") == "n"
    assert adapter._append_observed_note("a", None) == "a"
    assert adapter._append_observed_note("a", "b") == "a\n\nb"


# ---------------------------------------------------------------------------
# Reactions cluster (reactions_mixin)
# ---------------------------------------------------------------------------


def test_reactions_enabled_env_gate(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    assert adapter._reactions_enabled() is True
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")
    assert adapter._reactions_enabled() is False


@pytest.mark.asyncio
async def test_set_and_clear_reaction(monkeypatch):
    from unittest.mock import AsyncMock

    adapter = _make_adapter()
    adapter._bot = AsyncMock()
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    assert await adapter._set_reaction("123", "456", "👍") is True
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=456, reaction="👍"
    )


@pytest.mark.asyncio
async def test_processing_lifecycle_reactions(monkeypatch):
    from unittest.mock import AsyncMock

    adapter = _make_adapter()
    adapter._bot = AsyncMock()
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    event = MessageEvent(
        text="x", message_type=MessageType.TEXT, source=_source(), message_id="456"
    )

    await adapter.on_processing_start(event)
    adapter._bot.set_message_reaction.assert_awaited_with(
        chat_id=123, message_id=456, reaction="\U0001f440"
    )

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    adapter._bot.set_message_reaction.assert_awaited_with(
        chat_id=123, message_id=456, reaction="\U0001f44d"
    )

    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)
    assert adapter._bot.set_message_reaction.await_count == 3
    # CANCELLED clears (reaction=None)
    assert adapter._bot.set_message_reaction.await_args.kwargs["reaction"] is None


# ---------------------------------------------------------------------------
# DM-topic cache cluster (dm_topics_cache_mixin)
# ---------------------------------------------------------------------------


def test_cache_dm_topic_from_message():
    adapter = _make_adapter()
    adapter._dm_topics = {}
    adapter._cache_dm_topic_from_message("123", "42", "work")
    assert adapter._dm_topics == {"123:work": 42}
    # idempotent re-cache
    adapter._cache_dm_topic_from_message("123", "42", "work")
    assert adapter._dm_topics == {"123:work": 42}


def test_get_dm_topic_info_from_cache():
    adapter = _make_adapter()
    adapter._dm_topics = {"123:work": 42}
    adapter._dm_topics_config = [
        {"chat_id": "123", "topics": [{"name": "work", "thread_id": 42, "skill": "coding"}]}
    ]
    adapter._reload_dm_topics_from_config = lambda: None  # keep hermetic
    info = adapter._get_dm_topic_info("123", "42")
    assert info is not None
    assert info["name"] == "work"
    assert info["skill"] == "coding"
    assert adapter._get_dm_topic_info("123", None) is None
    assert adapter._get_dm_topic_info("123", "99") is None


# ---------------------------------------------------------------------------
# Rich-text flatten cluster (rich_text_flatten_mixin)
# ---------------------------------------------------------------------------


def test_flatten_rich_inline_text():
    from plugins.platforms.telegram.rich_text_flatten_mixin import RichTextFlattenMixin as M

    assert M._flatten_rich_inline_text(None) == ""
    assert M._flatten_rich_inline_text("hi") == "hi"
    assert M._flatten_rich_inline_text(["a", {"text": "b"}, {"children": [{"text": "c"}]}]) == "abc"
    assert M._flatten_rich_inline_text({"text": "x"}) == "x"
    assert M._flatten_rich_inline_text(42) == ""


def test_flatten_rich_blocks():
    from plugins.platforms.telegram.rich_text_flatten_mixin import RichTextFlattenMixin as M

    blocks = [
        {"type": "paragraph", "text": [{"text": "first line"}, {"text": "second"}]},
        {"type": "list", "items": [{"label": "L1", "blocks": [{"type": "p", "text": "item text"}]}]},
        {"type": "heading", "text": "  padded  "},
    ]
    out = M._flatten_rich_blocks(blocks)
    assert "first line" in out
    assert "L1 item text" in out
    assert "padded" in out


def test_extract_rich_reply_text():
    from plugins.platforms.telegram.rich_text_flatten_mixin import RichTextFlattenMixin as M

    reply = SimpleNamespace(
        api_kwargs={"rich_message": {"blocks": [{"type": "p", "text": "echo"}]}}
    )
    assert M._extract_rich_reply_text(reply) == "echo"
    assert M._extract_rich_reply_text(SimpleNamespace(api_kwargs={})) is None
    assert M._extract_rich_reply_text(SimpleNamespace()) is None


# ---------------------------------------------------------------------------
# Batching clusters (text_batching_mixin / media_batching_mixin)
# ---------------------------------------------------------------------------


def test_text_batch_key_uses_session_key():
    adapter = _make_adapter(group_sessions_per_user=True)
    event = MessageEvent(
        text="hello", message_type=MessageType.TEXT, source=_source(thread_id="5")
    )
    key = adapter._text_batch_key(event)
    assert isinstance(key, str) and key


def test_photo_batch_key_album_and_burst():
    adapter = _make_adapter()
    event = MessageEvent(
        text="", message_type=MessageType.PHOTO, source=_source(thread_id="5")
    )
    album = _msg(text=None, media_group_id="g1")
    assert adapter._photo_batch_key(event, album).endswith(":album:g1")
    burst = _msg(text=None, media_group_id=None)
    assert adapter._photo_batch_key(event, burst).endswith(":photo-burst")


@pytest.mark.asyncio
async def test_enqueue_text_event_merges_chunks():
    adapter = _make_adapter()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    first = MessageEvent(
        text="part1", message_type=MessageType.TEXT, source=_source()
    )
    second = MessageEvent(
        text="part2", message_type=MessageType.TEXT, source=_source()
    )
    adapter._enqueue_text_event(first)
    adapter._enqueue_text_event(second)
    key = adapter._text_batch_key(first)
    merged = adapter._pending_text_batches[key]
    assert merged.text == "part1\npart2"
    task = adapter._pending_text_batch_tasks[key]
    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task


@pytest.mark.asyncio
async def test_enqueue_photo_event_merges():
    import asyncio

    adapter = _make_adapter()
    adapter._pending_photo_batches = {}
    adapter._pending_photo_batch_tasks = {}
    event = MessageEvent(
        text="", message_type=MessageType.PHOTO, source=_source(),
        media_urls=["/tmp/a.jpg"], media_types=["image/jpeg"],
    )
    adapter._enqueue_photo_event("k", event)
    adapter._enqueue_photo_event("k", event)
    assert len(adapter._pending_photo_batches["k"].media_urls) == 2
    task = adapter._pending_photo_batch_tasks["k"]
    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task
