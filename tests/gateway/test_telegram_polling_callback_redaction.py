"""Callback-level regression tests for Telegram polling-error redaction (#72668).

``_polling_error_callback``'s network-error and fallback branches embedded
``_redact_telegram_error_text(error)`` in the *format string* — the helper's
name appeared as literal message text and the raw exception was passed as the
``%s`` argument, so the redaction pass never ran on those two paths. These
tests drive the real ``connect()``, capture the ``error_callback`` handed to
``_start_polling_resilient``, and invoke it with an error that embeds the
bot-token URL, asserting the emitted log is both well-formed and redacted.
"""
import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)
    sys.modules.setdefault("telegram.error", telegram_mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

_FAKE_TOKEN_PARTS = ("123456789", "AAFakeSecretTelegramBotTokenXYZ")


def _fake_token() -> str:
    return ":".join(_FAKE_TOKEN_PARTS)


@pytest.fixture(autouse=True)
def _no_auto_discovery(monkeypatch):
    async def _noop():
        return []

    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.discover_fallback_ips", _noop
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.HTTPXRequest", lambda **kwargs: MagicMock()
    )


async def _cancel_heartbeat(adapter):
    task = getattr(adapter, "_polling_heartbeat_task", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    adapter._polling_heartbeat_task = None


async def _connected_adapter_with_callback(monkeypatch, adapter):
    """Run the real connect() with a mocked PTB; return the captured
    ``error_callback`` handed to ``_start_polling_resilient``."""

    async def fake_start_polling(**kwargs):
        adapter._record_polling_progress(adapter._polling_generation)
        return True

    updater = SimpleNamespace(
        start_polling=AsyncMock(side_effect=fake_start_polling),
        stop=AsyncMock(),
        running=True,
    )
    bot = SimpleNamespace(set_my_commands=AsyncMock(), delete_webhook=AsyncMock())
    app = SimpleNamespace(
        bot=bot,
        updater=updater,
        add_handler=MagicMock(),
        initialize=AsyncMock(),
        start=AsyncMock(),
    )
    builder = MagicMock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.get_updates_request.return_value = builder
    builder.build.return_value = app
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.Application",
        SimpleNamespace(builder=MagicMock(return_value=builder)),
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    ok = await adapter.connect()
    assert ok is True
    # connect() never awaits the callback-captured kwargs, so pull the
    # registered reference the adapter stores for retry use.
    callback = adapter._polling_error_callback_ref
    assert callable(callback)
    return callback


class TestPollingCallbackRedaction:
    @pytest.mark.asyncio
    async def test_network_branch_redacts_and_is_not_garbled(
        self, monkeypatch, caplog
    ):
        """The network-error branch must run the redactor over the exception
        and emit a well-formed message (#72668): the helper name never
        appears as literal text, and the raw token never appears at all."""
        token = _fake_token()
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token=token))
        monkeypatch.setattr(
            "gateway.status.acquire_scoped_lock",
            lambda scope, identity, metadata=None: (True, None),
        )
        monkeypatch.setattr(
            "gateway.status.release_scoped_lock", lambda scope, identity: None
        )
        callback = await _connected_adapter_with_callback(monkeypatch, adapter)
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            with caplog.at_level(logging.WARNING, logger="plugins.platforms.telegram.adapter"):
                callback(ConnectionError(f"Network error during getUpdates: {url}"))
            rendered = " | ".join(rec.getMessage() for rec in caplog.records)
            assert "_redact_telegram_error_text" not in rendered, (
                "helper name pasted into the format string (the #72668 garble)"
            )
            assert token not in rendered, "raw bot token leaked into the log"
            assert "network" in rendered.lower()
            assert "scheduling reconnect" in rendered
        finally:
            await _cancel_heartbeat(adapter)

    @pytest.mark.asyncio
    async def test_fallback_branch_redacts_and_is_not_garbled(
        self, monkeypatch, caplog
    ):
        """The generic fallback (logger.error) branch has the same fix."""
        token = _fake_token()
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token=token))
        monkeypatch.setattr(
            "gateway.status.acquire_scoped_lock",
            lambda scope, identity, metadata=None: (True, None),
        )
        monkeypatch.setattr(
            "gateway.status.release_scoped_lock", lambda scope, identity: None
        )
        callback = await _connected_adapter_with_callback(monkeypatch, adapter)
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            with caplog.at_level(logging.ERROR, logger="plugins.platforms.telegram.adapter"):
                callback(ValueError(f"Unexpected polling failure via {url}"))
            rendered = " | ".join(rec.getMessage() for rec in caplog.records)
            assert "_redact_telegram_error_text" not in rendered
            assert token not in rendered
            assert "polling" in rendered.lower()
        finally:
            await _cancel_heartbeat(adapter)

    @pytest.mark.asyncio
    async def test_callback_no_longer_formats_raw_error_argument(
        self, monkeypatch, caplog
    ):
        """On main (pre-fix) the raw exception is the %s argument of a format
        string that also garbles the helper name; assert the emitted record
        carries the *redacted* text, not the raw error string."""
        token = _fake_token()
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token=token))
        monkeypatch.setattr(
            "gateway.status.acquire_scoped_lock",
            lambda scope, identity, metadata=None: (True, None),
        )
        monkeypatch.setattr(
            "gateway.status.release_scoped_lock", lambda scope, identity: None
        )
        callback = await _connected_adapter_with_callback(monkeypatch, adapter)
        try:
            raw_error = f"boom at https://api.telegram.org/bot{token}/getMe"
            with caplog.at_level(logging.WARNING, logger="plugins.platforms.telegram.adapter"):
                callback(ConnectionError(raw_error))
            rendered = " | ".join(rec.getMessage() for rec in caplog.records)
            assert rendered, "no log record emitted by the callback"
            assert raw_error not in rendered, "unredacted raw error text emitted"
        finally:
            await _cancel_heartbeat(adapter)
