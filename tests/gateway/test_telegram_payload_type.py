"""TelegramAdapter.send() honors a declared payload_type (TKT-0033).

When the delivery metadata declares ``payload_type == "text/html"``, the
adapter must bypass the MarkdownV2 conversion (``format_message``) entirely
and send the raw content with ``ParseMode.HTML``.  Anything else — missing
metadata, missing key, or an unknown payload_type — takes today's
MarkdownV2 path, byte-identical.

These tests pin the ``_resolve_send_format`` helper contract plus the
``_VALID_PAYLOAD_TYPES`` constant, and exercise the ``send()`` choke point
to prove the resolved parse mode is threaded into ``send_message``.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter() -> TelegramAdapter:
    """Bare adapter via __new__ (tests here avoid __init__), matching the
    codebase idiom; only the attributes _resolve_send_format/send touch are
    stubbed."""
    from gateway.config import Platform

    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.platform = Platform.TELEGRAM
    return adapter


# ---------------------------------------------------------------------------
# _VALID_PAYLOAD_TYPES constant
# ---------------------------------------------------------------------------

def test_valid_payload_types_constant():
    assert TelegramAdapter._VALID_PAYLOAD_TYPES == frozenset(
        {"text/markdown", "text/html"}
    )


# ---------------------------------------------------------------------------
# _resolve_send_format helper contract
# ---------------------------------------------------------------------------

def test_resolve_send_format_html_bypasses_markdownv2():
    """text/html → raw content returned UNCHANGED, parse mode HTML."""
    adapter = _make_adapter()
    # Sentinel: if format_message is called at all for HTML payloads, fail.
    adapter.format_message = MagicMock(
        side_effect=AssertionError("format_message must not run for text/html")
    )
    raw = "<b>bold</b> & <i>italic</i> — chars that MarkdownV2 would escape: ._~"
    text, parse_mode_name = adapter._resolve_send_format(
        raw, {"payload_type": "text/html"}
    )
    assert text == raw  # byte-identical, no escaping
    assert parse_mode_name == "HTML"
    adapter.format_message.assert_not_called()


def test_resolve_send_format_default_markdownv2():
    """metadata=None → MarkdownV2 path (format_message applied)."""
    adapter = _make_adapter()
    adapter.format_message = lambda content: f"FMT::{content}"
    text, parse_mode_name = adapter._resolve_send_format("hello_world", None)
    assert text == "FMT::hello_world"
    assert parse_mode_name == "MARKDOWN_V2"


def test_resolve_send_format_empty_metadata_markdownv2():
    """metadata present but no payload_type key → MarkdownV2 path."""
    adapter = _make_adapter()
    adapter.format_message = lambda content: f"FMT::{content}"
    text, parse_mode_name = adapter._resolve_send_format("hi", {"notify": True})
    assert text == "FMT::hi"
    assert parse_mode_name == "MARKDOWN_V2"


def test_resolve_send_format_unknown_payload_type_falls_back():
    """Unknown payload_type (e.g. application/json) → MarkdownV2 fallback."""
    adapter = _make_adapter()
    adapter.format_message = lambda content: f"FMT::{content}"
    text, parse_mode_name = adapter._resolve_send_format(
        "data", {"payload_type": "application/json"}
    )
    assert text == "FMT::data"
    assert parse_mode_name == "MARKDOWN_V2"


def test_resolve_send_format_explicit_markdown_payload_type():
    """Declared text/markdown → MarkdownV2 path (same as default)."""
    adapter = _make_adapter()
    adapter.format_message = lambda content: f"FMT::{content}"
    text, parse_mode_name = adapter._resolve_send_format(
        "doc", {"payload_type": "text/markdown"}
    )
    assert text == "FMT::doc"
    assert parse_mode_name == "MARKDOWN_V2"


# ---------------------------------------------------------------------------
# send() choke point: resolved parse mode reaches send_message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_html_payload_uses_parse_mode_html_and_raw_text():
    """End-to-end through send(): text/html payload reaches send_message
    with ParseMode.HTML and the raw (unescaped) content."""
    adapter = _make_adapter()
    adapter._send_path_degraded = False
    adapter._should_attempt_rich = lambda content, metadata=None: False
    adapter.format_message = MagicMock(
        side_effect=AssertionError("format_message must not run for text/html")
    )

    sent = []

    async def _send_message(**kwargs):
        sent.append(kwargs)
        return MagicMock(message_id=42)

    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(side_effect=_send_message)
    # Neutralize threading/notification helpers with light defaults.
    adapter._metadata_thread_id = lambda metadata: None
    adapter._message_thread_id_for_send = lambda thread_id: None
    adapter._metadata_reply_to_message_id = lambda metadata: None
    adapter._is_private_dm_topic_send = lambda chat_id, thread_id, metadata: False
    adapter._reply_to_mode = "first"
    adapter._thread_kwargs_for_send = lambda *a, **k: {}
    adapter._link_preview_kwargs = lambda: {}
    adapter._notification_kwargs = lambda metadata: {}
    adapter.send_typing = AsyncMock()

    raw = "<b>Report</b><ul><li>one</li></ul>"
    result = await adapter.send("123", raw, metadata={"payload_type": "text/html"})

    assert result.success is True
    assert len(sent) == 1
    assert sent[0]["text"] == raw  # raw, not MarkdownV2-escaped
    from telegram.constants import ParseMode  # type: ignore

    assert sent[0]["parse_mode"] == ParseMode.HTML
    adapter.format_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_default_payload_still_markdown_v2():
    """Default path (no payload_type) stays byte-identical: format_message
    output + ParseMode.MARKDOWN_V2."""
    adapter = _make_adapter()
    adapter._send_path_degraded = False
    adapter._should_attempt_rich = lambda content, metadata=None: False
    adapter.format_message = lambda content: f"FMT::{content}"

    sent = []

    async def _send_message(**kwargs):
        sent.append(kwargs)
        return MagicMock(message_id=7)

    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(side_effect=_send_message)
    adapter._metadata_thread_id = lambda metadata: None
    adapter._message_thread_id_for_send = lambda thread_id: None
    adapter._metadata_reply_to_message_id = lambda metadata: None
    adapter._is_private_dm_topic_send = lambda chat_id, thread_id, metadata: False
    adapter._reply_to_mode = "first"
    adapter._thread_kwargs_for_send = lambda *a, **k: {}
    adapter._link_preview_kwargs = lambda: {}
    adapter._notification_kwargs = lambda metadata: {}
    adapter.send_typing = AsyncMock()

    result = await adapter.send("123", "plain text", metadata=None)

    assert result.success is True
    assert len(sent) == 1
    assert sent[0]["text"] == "FMT::plain text"
    from telegram.constants import ParseMode  # type: ignore

    assert sent[0]["parse_mode"] == ParseMode.MARKDOWN_V2
