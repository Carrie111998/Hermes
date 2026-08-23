"""Regression tests for the Telegram adapter's empty-response sentinel guard (#92924).

The agent's ``(empty)`` terminal sentinel means the model produced no visible
content after the retry/fallback chain was exhausted. The gateway converts the
exact sentinel to a friendly message on the normal delivery path, but any path
that hands the raw sentinel (or a whitespace-padded variant of it) straight to
``TelegramAdapter.send()`` — status bubbles, direct pushes, queued delivery —
would render the literal ``(empty)`` text to the user. The adapter must never
send it, mirroring the existing whitespace-only skip.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    cfg = PlatformConfig(enabled=True, token="fake-token", extra={})
    adapter = TelegramAdapter(cfg)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=456))
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    # Force the legacy (non-rich) send path.
    adapter._rich_messages_enabled = False
    return adapter, bot


@pytest.mark.parametrize(
    "content",
    [
        "(empty)",
        "(empty)\n",
        " (empty) ",
        "(empty)\n\n",
        "   ",
        "\n",
    ],
)
def test_send_skips_empty_sentinel_without_network_call(content):
    """The bare sentinel and its whitespace variants must never be sent."""
    adapter, bot = _make_adapter()
    result = asyncio.run(adapter.send(chat_id="123", content=content))
    assert result.success is True
    assert result.message_id is None
    assert bot.send_message.await_count == 0


def test_send_still_delivers_real_content():
    """The guard must not eat genuine replies."""
    adapter, bot = _make_adapter()
    result = asyncio.run(adapter.send(chat_id="123", content="real answer"))
    assert result.success is True
    assert bot.send_message.await_count >= 1


def test_sentinel_guard_follows_canonical_constant(monkeypatch):
    """The adapter must not hardcode the sentinel literal.

    The guard reads ``agent.anthropic_adapter._EMPTY_TEXT_PLACEHOLDER`` at
    call time — the same single source the gateway's classifier imports. If
    someone re-hardcodes ``"(empty)"`` in the adapter, this test fails
    because the re-hardcoded guard would send ``(nil)`` to the Bot API
    instead of skipping it (#92924 review: single-source the sentinel).
    """
    import agent.anthropic_adapter as anthropic_adapter

    monkeypatch.setattr(anthropic_adapter, "_EMPTY_TEXT_PLACEHOLDER", "(nil)")
    adapter, bot = _make_adapter()
    result = asyncio.run(adapter.send(chat_id="123", content="(nil)"))
    assert result.success is True
    assert result.message_id is None
    assert bot.send_message.await_count == 0


def test_sentinel_guard_matches_gateway_classifier():
    """Adapter guard and gateway classifier must agree on the sentinel value."""
    from gateway.run import _is_empty_agent_sentinel
    from plugins.platforms.telegram.adapter import _empty_agent_sentinel_text

    sentinel = _empty_agent_sentinel_text()
    assert sentinel
    assert _is_empty_agent_sentinel(sentinel)
