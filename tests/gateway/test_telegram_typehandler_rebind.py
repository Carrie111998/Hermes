"""Regression test: check_telegram_requirements() must rebind TypeHandler.

When the adapter module loads while python-telegram-bot is missing, every PTB
name falls back to typing.Any (mock mode). The registry then lazy-installs the
SDK and calls check_telegram_requirements(), which re-imports the real classes
and rebinds the module globals. It used to miss TypeHandler, so connect()
failed with "Any cannot be instantiated" at `TypeHandler(Update, ...)` even
after the dependency was restored. See adapter.py:4193.
"""

import typing
from pathlib import Path

import pytest

pytest.importorskip("telegram")

from gateway.config import PlatformConfig  # noqa: E402

import plugins.platforms.telegram.adapter as telegram_mod  # noqa: E402


class TestTypeHandlerRebind:
    def test_mock_boot_recovery_rebinds_typehandler(self, monkeypatch):
        """After mock-mode boot, the lazy-install recovery must restore
        TypeHandler to the real class, not leave it as typing.Any."""
        # Simulate the module having loaded while python-telegram-bot was
        # missing: every PTB name is typing.Any (the except-ImportError branch).
        monkeypatch.setattr(telegram_mod, "TELEGRAM_AVAILABLE", False)
        for name in (
            "Update", "Bot", "Message", "InlineKeyboardButton",
            "InlineKeyboardMarkup", "Application", "CommandHandler",
            "CallbackQueryHandler", "TypeHandler", "TelegramMessageHandler",
            "HTTPXRequest",
        ):
            monkeypatch.setattr(telegram_mod, name, typing.Any)

        assert telegram_mod.TypeHandler is typing.Any

        # The registry's ensure_deps_fn runs the recovery path. With the real
        # library importable, it re-imports and rebinds every mock name.
        assert telegram_mod.check_telegram_requirements() is True

        assert telegram_mod.TELEGRAM_AVAILABLE is True
        assert telegram_mod.TypeHandler is not typing.Any

        # The exact call connect() makes at adapter.py:4193 must not raise
        # "Any cannot be instantiated" — the pre-fix failure. (Under the
        # conftest telegram mock this returns a MagicMock; with the real
        # library it returns a TypeHandler. Both are fine — the regression
        # is that it no longer hits typing.Any's __init__.)
        handler = telegram_mod.TypeHandler(telegram_mod.Update, lambda x: None)
        assert handler is not None

    def test_recovery_is_idempotent_when_lib_present(self, monkeypatch):
        """A healthy module returns True without touching anything."""
        monkeypatch.setattr(telegram_mod, "TELEGRAM_AVAILABLE", True)
        assert telegram_mod.check_telegram_requirements() is True
