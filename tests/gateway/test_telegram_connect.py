"""Tests for Telegram connect() non-retryable fatal error on missing credentials.

When Telegram has no bot token or no python-telegram-bot installed, connect()
must set a non-retryable fatal error so the gateway does not queue it for
background reconnection (#31049).
"""


import pytest

from gateway.config import PlatformConfig
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


def test_active_telegram_check_revalidates_exact_lazy_contract(monkeypatch):
    """Importability must not bypass the exact active Telegram contract."""
    ensure_calls = []
    stale_alias = object()
    monkeypatch.setattr(telegram_mod, "TELEGRAM_AVAILABLE", True)
    monkeypatch.setattr(telegram_mod, "Update", stale_alias)
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda feature, **kwargs: ensure_calls.append((feature, kwargs)),
    )

    assert telegram_mod.check_telegram_requirements() is True
    assert ensure_calls == [("platform.telegram", {"prompt": False})]
    assert telegram_mod.Update is not stale_alias
