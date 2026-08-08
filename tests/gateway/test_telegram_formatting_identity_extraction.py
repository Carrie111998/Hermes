"""Regression tests for the formatting, bot-identity and typing clusters
extracted to ``FormattingMixin`` / ``BotIdentityMixin`` / ``TypingMixin``
(shard s4 of the adapter god-file decomposition).

Covers the PURE moved helpers: ``format_message`` and ``_escape_mdv2``,
``_extract_bot_mention_usernames`` (entity + fallback parsing), bot-identity
observation, and ``_is_transient_typing_error`` classification.
"""

from types import SimpleNamespace

from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import (
    TelegramAdapter,
    _escape_mdv2 as adapter_escape_mdv2,
    _wrap_markdown_tables as adapter_wrap_markdown_tables,
)
from plugins.platforms.telegram.bot_identity_mixin import BotIdentityMixin
from plugins.platforms.telegram.formatting_mixin import (
    FormattingMixin,
    _escape_mdv2,
    _wrap_markdown_tables,
)
from plugins.platforms.telegram.typing_mixin import TypingMixin


def _make_adapter(bot_username="hermes_bot"):
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={})
    adapter._bot = SimpleNamespace(id=999, username=bot_username)
    return adapter


def _mention_entity(text, mention):
    offset = text.index(mention)
    return SimpleNamespace(type="mention", offset=offset, length=len(mention))


def _bot_command_entity(text, command):
    offset = text.index(command)
    return SimpleNamespace(type="bot_command", offset=offset, length=len(command))


def _message(text, entities=None):
    return SimpleNamespace(
        text=text,
        caption=None,
        entities=entities or [],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=-100, type="supergroup", is_forum=True),
        from_user=SimpleNamespace(id=111),
        reply_to_message=None,
    )


# ---------------------------------------------------------------------------
# MRO wiring + helper re-exports
# ---------------------------------------------------------------------------

def test_mixins_wired_into_adapter_mro():
    adapter = _make_adapter()
    assert isinstance(adapter, FormattingMixin)
    assert isinstance(adapter, BotIdentityMixin)
    assert isinstance(adapter, TypingMixin)


def test_formatting_helpers_reexported_by_adapter():
    assert _escape_mdv2 is adapter_escape_mdv2
    assert _wrap_markdown_tables is adapter_wrap_markdown_tables


# ---------------------------------------------------------------------------
# format_message (moved with FormattingMixin)
# ---------------------------------------------------------------------------

def test_format_message_basic_escapes():
    adapter = _make_adapter()
    assert adapter.format_message("hello world") == "hello world"
    assert adapter.format_message("") == ""


def test_format_message_bold_italic_and_inline_code():
    adapter = _make_adapter()
    out = adapter.format_message("**bold** and *italic*")
    assert "*bold*" in out and "_italic_" in out


def test_format_message_protects_inline_code():
    adapter = _make_adapter()
    out = adapter.format_message("keep `a*b` as code")
    assert "`a*b`" in out


def test_format_message_pipe_table_becomes_bullet_rows():
    # GFM pipe tables are rewritten to Telegram-friendly bullet row groups
    # before MarkdownV2 conversion (convert_table_to_bullets), not fenced.
    adapter = _make_adapter()
    out = adapter.format_message("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "• b: 2" in out and "*1*" in out


def test_format_message_headers_and_links():
    adapter = _make_adapter()
    out = adapter.format_message("# Title")
    assert "*Title*" in out
    out2 = adapter.format_message("[click](https://example.com)")
    assert "[click](https://example.com)" in out2


def test_escape_mdv2_pure():
    assert _escape_mdv2("a*b") == "a\\*b"
    assert _escape_mdv2("v2.0") == "v2\\.0"
    assert _escape_mdv2("wow!") == "wow\\!"


# ---------------------------------------------------------------------------
# _extract_bot_mention_usernames (classmethod, moved with BotIdentityMixin)
# ---------------------------------------------------------------------------

def test_extract_mention_entity():
    text = "hi @hermes_bot"
    msg = _message(text, [_mention_entity(text, "@hermes_bot")])
    assert TelegramAdapter._extract_bot_mention_usernames(msg) == {"hermes_bot"}


def test_extract_bot_command_suffix():
    text = "/start@other_bot"
    msg = _message(text, [_bot_command_entity(text, "/start@other_bot")])
    assert TelegramAdapter._extract_bot_mention_usernames(msg) == {"other_bot"}


def test_extract_foreign_human_handle_excluded():
    text = "hi @alice"
    msg = _message(text, [_mention_entity(text, "@alice")])
    assert TelegramAdapter._extract_bot_mention_usernames(msg) == set()


def test_extract_own_handle_regardless_of_shape():
    # collectible (Fragment) bot handle that does not end in "bot"
    text = "ping @jarvis"
    msg = _message(text, [_mention_entity(text, "@jarvis")])
    assert TelegramAdapter._extract_bot_mention_usernames(
        msg, self_username="jarvis") == {"jarvis"}


def test_extract_entity_less_fallback():
    text = "ping @hermes_bot"
    msg = _message(text)  # no entities -> regex fallback
    assert TelegramAdapter._extract_bot_mention_usernames(msg) == {"hermes_bot"}


# ---------------------------------------------------------------------------
# Bot identity observation
# ---------------------------------------------------------------------------

def test_current_bot_username_prefers_observed():
    adapter = _make_adapter()
    assert adapter._current_bot_username() == "hermes_bot"
    # the observed handle wins verbatim (recording lowercases via
    # _note_bot_username; reading does not re-normalize)
    adapter._bot_username_observed = "MyBot"
    assert adapter._current_bot_username() == "MyBot"


def test_note_bot_username_records_and_renames():
    adapter = _make_adapter()
    adapter._note_bot_username("@first_bot")
    assert adapter._bot_username_observed == "first_bot"
    adapter._note_bot_username("first_bot")  # no-op on same handle
    assert adapter._bot_username_observed == "first_bot"
    adapter._note_bot_username("SecondBot")
    assert adapter._bot_username_observed == "secondbot"


def test_bot_identity_is_fresh_false_when_never_checked():
    adapter = _make_adapter()
    assert adapter._bot_identity_is_fresh() is False


def test_observe_bot_identity_from_message_only_own_user():
    adapter = _make_adapter()
    own = SimpleNamespace(
        from_user=SimpleNamespace(id=999, username="NewBot"),
        reply_to_message=None,
    )
    adapter._observe_bot_identity_from_message(own)
    assert adapter._bot_username_observed == "newbot"
    # another user's message must not be adopted
    adapter._bot_username_observed = None
    other = SimpleNamespace(
        from_user=SimpleNamespace(id=111, username="evil_bot"),
        reply_to_message=None,
    )
    adapter._observe_bot_identity_from_message(other)
    assert adapter._bot_username_observed is None


def test_is_reply_to_bot():
    adapter = _make_adapter()
    reply = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        message_id=10, text="ok",
    )
    msg = SimpleNamespace(reply_to_message=reply, from_user=SimpleNamespace(id=111))
    assert adapter._is_reply_to_bot(msg) is True
    human_reply = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        message_id=11, text="ok",
    )
    assert adapter._is_reply_to_bot(
        SimpleNamespace(reply_to_message=human_reply)) is False
    assert adapter._is_reply_to_bot(SimpleNamespace(reply_to_message=None)) is False


# ---------------------------------------------------------------------------
# _is_transient_typing_error (staticmethod, moved with TypingMixin)
# ---------------------------------------------------------------------------

def test_is_transient_typing_error():
    assert TelegramAdapter._is_transient_typing_error(
        SimpleNamespace(retry_after=5)) is True
    assert TelegramAdapter._is_transient_typing_error(
        SimpleNamespace(status_code=429)) is True
    assert TelegramAdapter._is_transient_typing_error(
        SimpleNamespace(code=503)) is True
    assert TelegramAdapter._is_transient_typing_error(
        Exception("too many requests")) is True
    assert TelegramAdapter._is_transient_typing_error(TimeoutError()) is True
    assert TelegramAdapter._is_transient_typing_error(OSError("conn reset")) is True
    assert TelegramAdapter._is_transient_typing_error(
        ValueError("bad request")) is False
    assert TelegramAdapter._is_transient_typing_error(
        SimpleNamespace(status_code=400)) is False
