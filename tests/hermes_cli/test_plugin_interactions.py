"""Tests for generic plugin interaction replies and Telegram callbacks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli.plugin_interactions import (
    PluginCallbackResult,
    PluginInlineButton,
    PluginInteractionReply,
    coerce_plugin_command_text,
    plugin_interaction_send_metadata,
    validate_callback_data,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class TestPluginInteractionReply:
    def test_coerce_text_from_structured_reply(self):
        reply = PluginInteractionReply(
            text="Title\nSummary",
            buttons=(
                (PluginInlineButton("✓ Read", "rd:abc:r"),),
                tuple(PluginInlineButton(f"★ {n}", f"rd:abc:{n}") for n in range(1, 6)),
            ),
            parse_mode="html",
        )

        assert coerce_plugin_command_text(reply) == "Title\nSummary"
        assert plugin_interaction_send_metadata(reply) == {
            "plugin_parse_mode": "html",
            "plugin_inline_keyboard": [
                [{"text": "✓ Read", "callback_data": "rd:abc:r"}],
                [{"text": f"★ {n}", "callback_data": f"rd:abc:{n}"} for n in range(1, 6)],
            ],
        }

    def test_plain_string_stays_compatible(self):
        assert coerce_plugin_command_text("hello") == "hello"
        assert plugin_interaction_send_metadata("hello") == {}

    def test_callback_data_must_fit_telegram_limit(self):
        validate_callback_data("rd:0123456789:r")
        with pytest.raises(ValueError, match="exceeds"):
            validate_callback_data("x" * 65)


class TestRegisterTelegramCallbackHandler:
    def test_prefix_handler_is_queued_and_dispatched(self):
        mgr = PluginManager()
        manifest = PluginManifest(name="demo", version="0.1.0", description="test")
        ctx = PluginContext(manifest=manifest, manager=mgr)

        async def on_read_later(data: str, query):
            return PluginCallbackResult(answer_text="done", delete_message=True)

        ctx.register_telegram_callback_handler("rd:", on_read_later)
        handlers = mgr.get_telegram_callback_handlers()
        assert len(handlers) == 1
        assert handlers[0][0] == "rd:"

        query = SimpleNamespace(answer=AsyncMock())
        result = asyncio.run(mgr.dispatch_telegram_callback("rd:token:r", query=query))
        assert isinstance(result, PluginCallbackResult)
        assert result.delete_message is True

    def test_unknown_prefix_returns_none(self):
        mgr = PluginManager()
        assert asyncio.run(mgr.dispatch_telegram_callback("zz:1:r", query=SimpleNamespace())) is None
