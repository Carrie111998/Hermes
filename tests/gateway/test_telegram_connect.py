"""Tests for Telegram connect() non-retryable fatal error on missing credentials.

When Telegram has no bot token or no python-telegram-bot installed, connect()
must set a non-retryable fatal error so the gateway does not queue it for
background reconnection (#31049).
"""

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"

    telegram_mod.error.NetworkError = type("NetworkError", (OSError,), {})
    telegram_mod.error.TimedOut = type("TimedOut", (OSError,), {})
    telegram_mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)
    sys.modules.setdefault("telegram.error", telegram_mod.error)


_ensure_telegram_mock()

import plugins.platforms.telegram.adapter as telegram_mod  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


class TestTelegramUnconfiguredNonRetryable:
    """Verify that missing dependency/token sets a non-retryable fatal error."""

    @pytest.mark.asyncio
    async def test_no_telegram_lib_sets_non_retryable_fatal(self, monkeypatch):
        """connect() with python-telegram-bot unavailable → non-retryable fatal error."""
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
        monkeypatch.setattr(telegram_mod, "TELEGRAM_AVAILABLE", False)
        result = await adapter.connect()
        assert result is False
        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_retryable is False
        assert adapter.fatal_error_code == "missing_dependency"


def test_lazy_telegram_dependency_rebinds_type_handler(monkeypatch):
    """Lazy PTB install must rebind every symbol used by handler registration."""

    class FakeUpdate:
        ALL_TYPES = object()

    class FakeBot:
        pass

    class FakeMessage:
        pass

    class FakeInlineKeyboardButton:
        pass

    class FakeInlineKeyboardMarkup:
        pass

    class FakeLinkPreviewOptions:
        pass

    class FakeApplication:
        pass

    class FakeCommandHandler:
        pass

    class FakeCallbackQueryHandler:
        pass

    class FakeMessageHandler:
        pass

    class FakeTypeHandler:
        pass

    class FakeHTTPXRequest:
        pass

    telegram_pkg = types.ModuleType("telegram")
    telegram_pkg.Update = FakeUpdate
    telegram_pkg.Bot = FakeBot
    telegram_pkg.Message = FakeMessage
    telegram_pkg.InlineKeyboardButton = FakeInlineKeyboardButton
    telegram_pkg.InlineKeyboardMarkup = FakeInlineKeyboardMarkup
    telegram_pkg.LinkPreviewOptions = FakeLinkPreviewOptions

    ext_mod = types.ModuleType("telegram.ext")
    ext_mod.Application = FakeApplication
    ext_mod.CommandHandler = FakeCommandHandler
    ext_mod.CallbackQueryHandler = FakeCallbackQueryHandler
    ext_mod.MessageHandler = FakeMessageHandler
    ext_mod.TypeHandler = FakeTypeHandler
    ext_mod.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    ext_mod.filters = types.SimpleNamespace()

    constants_mod = types.ModuleType("telegram.constants")
    constants_mod.ParseMode = types.SimpleNamespace(MARKDOWN_V2="MarkdownV2")
    constants_mod.ChatType = types.SimpleNamespace(
        GROUP="group",
        SUPERGROUP="supergroup",
        CHANNEL="channel",
        PRIVATE="private",
    )

    request_mod = types.ModuleType("telegram.request")
    request_mod.HTTPXRequest = FakeHTTPXRequest

    for name, module in {
        "telegram": telegram_pkg,
        "telegram.ext": ext_mod,
        "telegram.constants": constants_mod,
        "telegram.request": request_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    from tools import lazy_deps

    monkeypatch.setattr(lazy_deps, "ensure", lambda key, prompt=False: None)

    monkeypatch.setattr(telegram_mod, "TELEGRAM_AVAILABLE", False)
    monkeypatch.setattr(telegram_mod, "Update", Any)
    monkeypatch.setattr(telegram_mod, "Bot", Any)
    monkeypatch.setattr(telegram_mod, "Message", Any)
    monkeypatch.setattr(telegram_mod, "InlineKeyboardButton", Any)
    monkeypatch.setattr(telegram_mod, "InlineKeyboardMarkup", Any)
    monkeypatch.setattr(telegram_mod, "LinkPreviewOptions", None)
    monkeypatch.setattr(telegram_mod, "Application", Any)
    monkeypatch.setattr(telegram_mod, "CommandHandler", Any)
    monkeypatch.setattr(telegram_mod, "CallbackQueryHandler", Any)
    monkeypatch.setattr(telegram_mod, "TypeHandler", Any)
    monkeypatch.setattr(telegram_mod, "TelegramMessageHandler", Any)
    monkeypatch.setattr(telegram_mod, "ContextTypes", Any)
    monkeypatch.setattr(telegram_mod, "filters", None)
    monkeypatch.setattr(telegram_mod, "ParseMode", None)
    monkeypatch.setattr(telegram_mod, "ChatType", None)
    monkeypatch.setattr(telegram_mod, "HTTPXRequest", Any)

    assert telegram_mod.check_telegram_requirements() is True
    assert telegram_mod.TELEGRAM_AVAILABLE is True
    assert telegram_mod.TypeHandler is FakeTypeHandler
    assert telegram_mod.Update is FakeUpdate
    assert telegram_mod.TelegramMessageHandler is FakeMessageHandler
