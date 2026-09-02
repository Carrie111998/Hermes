"""Tests for inbound Bot API 10.x rich_message recovery.

Premium/formatted Telegram clients deliver long texts as
``message.rich_message`` with NO plain ``text`` field. Before the fix,
``filters.TEXT`` never matched those updates and they were silently
dropped. These tests lock in both halves of the fix:

1. ``_recover_rich_message_inbound_text`` renders the block tree to text.
2. ``_RichMessageInboundFilter`` matches only rich_message-only updates.
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

def _load_real_ptb():
    """Return real PTB classes even when another conftest mocked ``telegram``.

    ``tests/gateway/conftest.py`` installs a MagicMock ``telegram`` package at
    collection time. When both gateway and root files are collected in one
    pytest session, that mock replaces the real library in ``sys.modules``
    before this module imports. Re-import the real distribution directly so
    these tests always exercise actual PTB behaviour.
    """
    import importlib
    import importlib.util

    existing = sys.modules.get("telegram")
    if existing is not None and getattr(existing, "__file__", None):
        return existing

    saved = {k: v for k, v in sys.modules.items() if k == "telegram" or k.startswith("telegram.")}
    for k in saved:
        del sys.modules[k]
    try:
        spec = importlib.util.find_spec("telegram")
        if spec is None or spec.loader is None:
            pytest.skip("python-telegram-bot not installed")
        mod = importlib.import_module("telegram")
        if getattr(mod, "__file__", None) is None:
            pytest.skip("real python-telegram-bot unavailable")
        # Keep the real package alive under a private name and restore whatever
        # the conftest had installed so other suites are unaffected.
        sys.modules["_real_telegram_for_rich_tests"] = mod
        return mod
    finally:
        for k, v in saved.items():
            sys.modules[k] = v


_real_telegram = _load_real_ptb()
Message = _real_telegram.Message  # noqa: E402
Update = _real_telegram.Update  # noqa: E402
Chat = _real_telegram.Chat  # noqa: E402


RICH_BLOCKS = {
    "blocks": [
        {"type": "paragraph", "text": "Here is the full answer you asked for."},
        {"type": "paragraph", "text": ""},
        {"type": "paragraph", "text": "These are the main findings:"},
        {"type": "list", "items": [
            {"label": "1.", "blocks": [
                {"type": "paragraph", "text": "The first finding is documented here"}],
             "type": "1", "value": 1},
            {"label": "2.", "blocks": [
                {"type": "paragraph", "text": "The second finding is measured"}],
             "type": "1", "value": 2},
        ]},
        {"type": "paragraph", "text": "The system includes:"},
        {"type": "list", "items": [
            {"label": "-", "blocks": [
                {"type": "paragraph", "text": "First limitation described here"}]},
            {"label": "-", "blocks": [
                {"type": "paragraph", "text": "Second limitation described here"}]},
        ]},
    ]
}


def _make_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    return object.__new__(TelegramAdapter)


def _rich_message(**kwargs):
    """Build a PTB Message carrying only a rich_message body (api_kwargs)."""
    return Message(
        message_id=kwargs.get("message_id", 1),
        date=0,
        chat={"id": 12345, "type": "private"},
        api_kwargs={"rich_message": RICH_BLOCKS},
    )


def _event_adapter():
    """Build the narrow adapter fixture needed by _build_message_event."""
    adapter = _make_adapter()
    adapter.config = SimpleNamespace(extra={})
    adapter.build_source = lambda **_kwargs: SimpleNamespace()
    adapter._effective_message_thread_id = lambda _message: None
    return adapter


class TestRecoverRichMessageInboundText:
    def test_renders_paragraphs_and_lists(self):
        adapter = _make_adapter()
        out = adapter._recover_rich_message_inbound_text(_rich_message())
        assert out is not None
        lines = out.splitlines()
        assert lines[0] == "Here is the full answer you asked for."
        assert "These are the main findings:" in out
        assert "1. The first finding is documented here" in out
        assert "2. The second finding is measured" in out
        assert "First limitation described here" in out

    def test_empty_blocks_yield_none(self):
        adapter = _make_adapter()
        msg = Message(message_id=1, date=0,
                      chat={"id": 1, "type": "private"},
                      api_kwargs={"rich_message": {"blocks": []}})
        assert adapter._recover_rich_message_inbound_text(msg) is None

    def test_plain_text_message_untouched(self):
        adapter = _make_adapter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        object.__setattr__(msg, "text", "normal message")
        assert adapter._recover_rich_message_inbound_text(msg) is None

    def test_caption_message_untouched(self):
        adapter = _make_adapter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        object.__setattr__(msg, "caption", "photo caption")
        assert adapter._recover_rich_message_inbound_text(msg) is None

    def test_no_api_kwargs_is_none(self):
        adapter = _make_adapter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        assert adapter._recover_rich_message_inbound_text(msg) is None

    def test_garbage_shapes_are_none(self):
        adapter = _make_adapter()
        for kw in ({}, {"rich_message": None}, {"rich_message": "x"},
                   {"rich_message": {}}, {"rich_message": {"blocks": "no"}}):
            msg = Message(message_id=1, date=0,
                          chat={"id": 1, "type": "private"}, api_kwargs=kw)
            assert adapter._recover_rich_message_inbound_text(msg) is None

    def test_list_item_first_line_gets_label_rest_indented(self):
        adapter = _make_adapter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"},
                      api_kwargs={"rich_message": {"blocks": [
                          {"type": "list", "items": [
                              {"label": "-", "blocks": [
                                  {"type": "paragraph", "text": "first line"},
                                  {"type": "paragraph", "text": "second line"}]},
                          ]}]}})
        out = adapter._recover_rich_message_inbound_text(msg)
        assert "- first line" in out
        assert "\n  second line" in out

    def test_list_keeps_own_text_and_sibling_blocks(self):
        adapter = _make_adapter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"},
                      api_kwargs={"rich_message": {"blocks": [
                          {"type": "list", "text": "list title", "blocks": [
                              {"type": "paragraph", "text": "sibling block"}],
                           "items": [{"label": "-", "blocks": [
                               {"type": "paragraph", "text": "list item"}]}]},
                      ]}})
        out = adapter._recover_rich_message_inbound_text(msg)
        assert out == "- list item\nlist title\nsibling block"


class TestRecoveredTextNormalisation:
    def test_build_event_uses_override_and_preserves_raw_message(self):
        from gateway.platforms.base import MessageType

        adapter = _event_adapter()
        msg = Message(
            message_id=1,
            date=0,
            chat=Chat(id=12345, type="private"),
            api_kwargs={"rich_message": RICH_BLOCKS},
        )
        recovered = adapter._recover_rich_message_inbound_text(msg)
        event = adapter._build_message_event(msg, MessageType.TEXT, text_override=recovered)

        assert recovered is not None
        assert msg.text is None
        assert event.raw_message is msg
        assert event.text == recovered

    def test_handler_enqueues_recovered_text_without_mutating_raw_message(self):
        from gateway.platforms.base import MessageType

        adapter = _event_adapter()
        msg = Message(
            message_id=1,
            date=0,
            chat=Chat(id=12345, type="private"),
            api_kwargs={"rich_message": RICH_BLOCKS},
        )
        update = Update(update_id=1, message=msg)
        queued = []
        seen = {}

        adapter._is_user_authorized_from_message = lambda _message: True
        adapter._should_process_message = lambda _message, **kwargs: seen.update(kwargs) or True
        adapter._clean_bot_trigger_text = lambda text: text
        adapter._apply_telegram_group_observe_attribution = lambda event: event
        adapter._enqueue_text_event = queued.append

        async def _noop(*_args, **_kwargs):
            return None

        adapter._ensure_forum_commands = _noop
        adapter._cache_replied_media = _noop

        asyncio.run(adapter._handle_text_message(update, None))

        assert seen["text_override"] == adapter._recover_rich_message_inbound_text(msg)
        assert len(queued) == 1
        assert queued[0].message_type == MessageType.TEXT
        assert queued[0].text == seen["text_override"]
        assert queued[0].raw_message is msg
        assert msg.text is None

    def test_recovered_text_participates_in_bot_mention_detection(self):
        adapter = _make_adapter()
        adapter._bot = SimpleNamespace(id=7)
        adapter._current_bot_username = lambda: "hermesbot"
        msg = _rich_message()

        assert adapter._message_mentions_bot(msg, text_override="@hermesbot hello")


class TestRichMessageInboundFilter:
    def _filter(self):
        # The nested registration filter delegates to this production predicate;
        # testing it directly prevents a copied test-only implementation drifting.
        from plugins.platforms.telegram.adapter import TelegramAdapter

        return TelegramAdapter._is_rich_message_inbound

    def test_matches_rich_only_update(self):
        f = self._filter()
        assert f(_rich_message()) is True

    def test_ignores_plain_text_update(self):
        f = self._filter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        object.__setattr__(msg, "text", "hello")
        assert f(msg) is False

    def test_ignores_caption_update(self):
        f = self._filter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        object.__setattr__(msg, "caption", "cap")
        assert f(msg) is False

    def test_ignores_message_without_anything(self):
        f = self._filter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        assert f(msg) is False
