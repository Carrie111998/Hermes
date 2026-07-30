"""Mechanical no-effect guarantees for the credential-free shadow runtime."""

from __future__ import annotations

import importlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

os.environ.setdefault("LOCALAPPDATA", os.getcwd())

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult


@pytest.fixture
def telegram_module():
    return importlib.import_module("plugins.platforms.telegram.adapter")


def _config(*, external_effects=...):
    extra = {}
    if external_effects is not ...:
        extra["external_effects"] = external_effects
    return PlatformConfig(enabled=True, token="x", extra=extra)


def test_shadow_policy_is_explicit_fail_closed_and_prod_default_is_unchanged(
    telegram_module,
):
    adapter_type = telegram_module.TelegramAdapter

    assert adapter_type(_config()).external_effects_allowed is True
    assert (
        adapter_type(_config(external_effects=False)).external_effects_allowed is False
    )
    assert (
        adapter_type(_config(external_effects="not-a-boolean")).external_effects_allowed
        is False
    )


@pytest.mark.asyncio
async def test_shadow_connect_never_builds_application_or_starts_polling(
    telegram_module, monkeypatch
):
    adapter = telegram_module.TelegramAdapter(_config(external_effects=False))
    builder = Mock(
        side_effect=AssertionError(
            "Telegram application construction is an external path"
        )
    )
    monkeypatch.setattr(
        telegram_module, "Application", SimpleNamespace(builder=builder)
    )

    connected = await adapter.connect()

    assert connected is False
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_direct_send_is_denied_before_any_bot_call(
    telegram_module, monkeypatch
):
    adapter = telegram_module.TelegramAdapter(_config(external_effects=False))
    adapter._bot = object()
    low_level_send = AsyncMock(
        return_value=(SimpleNamespace(message_id=777), None, None)
    )
    monkeypatch.setattr(adapter, "_send_message_with_thread_fallback", low_level_send)

    result = await adapter.send("123", "must not leave shadow")

    assert isinstance(result, SendResult)
    assert result.success is False
    assert result.error == "external_effects_disabled"
    low_level_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_callback_neither_resolves_answers_nor_edits(
    telegram_module, monkeypatch
):
    adapter = telegram_module.TelegramAdapter(_config(external_effects=False))
    query = SimpleNamespace(
        data="od:" + "A" * 20 + ":approve",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=123, message_id=456),
        from_user=SimpleNamespace(id=789),
    )
    bridge = AsyncMock(
        side_effect=AssertionError("shadow callback reached Control Plane")
    )
    monkeypatch.setattr(telegram_module, "handle_typed_telegram_callback", bridge)

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

    bridge.assert_not_awaited()
    query.answer.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_standalone_send_is_denied_before_provider_call(
    telegram_module, monkeypatch
):
    provider_send = AsyncMock(
        side_effect=AssertionError("shadow reached Telegram provider")
    )
    monkeypatch.setattr("tools.send_message_tool._send_telegram", provider_send)

    result = await telegram_module._standalone_send(
        _config(external_effects=False),
        "123",
        "must not leave shadow",
    )

    assert result == {"error": "external_effects_disabled"}
    provider_send.assert_not_awaited()


def test_cutover_profile_can_preserve_updates_queued_during_single_consumer_handoff(
    telegram_module,
):
    adapter = telegram_module.TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="token",
            extra={"preserve_pending_updates_on_start": True},
        )
    )

    assert adapter._drop_pending_updates(is_reconnect=False) is False
    assert adapter._drop_pending_updates(is_reconnect=True) is False


def test_normal_prod_start_keeps_upstream_drop_pending_default(telegram_module):
    adapter = telegram_module.TelegramAdapter(_config())

    assert adapter._drop_pending_updates(is_reconnect=False) is True
    assert adapter._drop_pending_updates(is_reconnect=True) is False
