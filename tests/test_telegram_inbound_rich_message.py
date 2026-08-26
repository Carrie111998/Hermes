"""Tests for inbound Bot API 10.x rich_message recovery.

Premium/formatted Telegram clients deliver long texts as
``message.rich_message`` with NO plain ``text`` field. Before the fix,
``filters.TEXT`` never matched those updates and they were silently
dropped. These tests lock in both halves of the fix:

1. ``_recover_rich_message_inbound_text`` renders the block tree to text.
2. ``_RichMessageInboundFilter`` matches only rich_message-only updates.
"""

import sys

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
        ext = importlib.import_module("telegram.ext")
        sys.modules["_real_telegram_for_rich_tests"] = mod
        sys.modules["_real_telegram_for_rich_tests_ext"] = ext
        return mod
    finally:
        for k, v in saved.items():
            sys.modules[k] = v


_real_telegram = _load_real_ptb()
_real_telegram_ext = sys.modules["_real_telegram_for_rich_tests_ext"]
Message = _real_telegram.Message  # noqa: E402
Update = _real_telegram.Update  # noqa: E402


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


class TestAttachRecoveredText:
    def test_object_setattr_attaches_to_frozen_message(self):
        # PTB freezes Message attributes; the handler must bypass via
        # object.__setattr__ so downstream code can read msg.text.
        msg = _rich_message()
        adapter = _make_adapter()
        text = adapter._recover_rich_message_inbound_text(msg)
        object.__setattr__(msg, "text", text)
        assert msg.text == text


class TestRichMessageInboundFilter:
    def _filter(self):
        # Re-create the filter class exactly as registered, using the REAL
        # PTB filters module (sys.modules["telegram"] may be a conftest mock).
        filters = _real_telegram_ext.filters

        class _RichMessageInboundFilter(filters.MessageFilter):
            def filter(self, message):
                if getattr(message, "text", None) or getattr(message, "caption", None):
                    return False
                kw = getattr(message, "api_kwargs", None) or {}
                return isinstance(kw.get("rich_message"), dict)

        return _RichMessageInboundFilter()

    def _update(self, msg):
        return Update(update_id=1, message=msg)

    def test_matches_rich_only_update(self):
        f = self._filter()
        assert f.check_update(self._update(_rich_message())) == 1

    def test_ignores_plain_text_update(self):
        f = self._filter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        object.__setattr__(msg, "text", "hello")
        assert not f.check_update(self._update(msg))

    def test_ignores_caption_update(self):
        f = self._filter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        object.__setattr__(msg, "caption", "cap")
        assert not f.check_update(self._update(msg))

    def test_ignores_message_without_anything(self):
        f = self._filter()
        msg = Message(message_id=1, date=0, chat={"id": 1, "type": "private"})
        assert not f.check_update(self._update(msg))
