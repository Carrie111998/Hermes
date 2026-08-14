"""Regression coverage for Telegram inbound update concurrency.

Telegram forum topics map to distinct Hermes sessions.  The PTB application must
therefore dispatch a bounded number of inbound updates concurrently so a
long-running command in one topic cannot stop another topic from reaching its
own session guard.
"""

import asyncio
from unittest.mock import MagicMock

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter


class _StopAfterBuild(Exception):
    """Abort connect after the application builder has been fully configured."""


def _build_adapter(*, extra=None) -> TelegramAdapter:
    return TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    )


def _record_builder_concurrency(monkeypatch, *, extra=None) -> MagicMock:
    """Run connect up to build(), recording the PTB builder configuration."""
    builder = MagicMock()
    for method in (
        "token",
        "concurrent_updates",
        "base_url",
        "base_file_url",
        "local_mode",
        "request",
        "get_updates_request",
    ):
        getattr(builder, method).return_value = builder
    builder.build.side_effect = _StopAfterBuild

    application = MagicMock()
    application.builder.return_value = builder
    monkeypatch.setattr(tg_adapter, "Application", application)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", MagicMock())

    async def _no_fallback_ips():
        return []

    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", _no_fallback_ips)
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *_args, **_kwargs: None)

    adapter = _build_adapter(extra=extra)
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *_args: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])

    # connect() turns a builder error into a failed connection.  That is fine:
    # this test only exercises the real adapter's configuration path before
    # any network or PTB startup work begins.
    assert asyncio.run(adapter.connect()) is False
    return builder


def test_telegram_connect_uses_four_concurrent_updates_by_default(monkeypatch):
    """Separate topic updates have a safe, useful default concurrency."""
    builder = _record_builder_concurrency(monkeypatch)

    builder.concurrent_updates.assert_called_once_with(4)


def test_telegram_connect_honors_bounded_concurrent_updates_setting(monkeypatch):
    """Operators can narrow or widen Telegram dispatch without source patches."""
    builder = _record_builder_concurrency(monkeypatch, extra={"concurrent_updates": 7})

    builder.concurrent_updates.assert_called_once_with(7)


def test_telegram_connect_clamps_invalid_concurrent_updates_setting(monkeypatch):
    """Malformed or excessive settings cannot create an unbounded update fan-out."""
    for configured, expected in ((0, 1), (99, 16), ("fast", 4)):
        builder = _record_builder_concurrency(
            monkeypatch, extra={"concurrent_updates": configured}
        )

        builder.concurrent_updates.assert_called_once_with(expected)
