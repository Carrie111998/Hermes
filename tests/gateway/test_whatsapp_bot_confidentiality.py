"""Confidentiality boundaries for customer-facing WhatsApp bot turns."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import (
    GatewayRunner,
    TurnRunner,
    _customer_facing_whatsapp_bot_turn,
    _requires_explicit_internal_display_opt_in,
)


def _event(*, platform=Platform.WHATSAPP, from_owner=False):
    return SimpleNamespace(
        source=SimpleNamespace(platform=platform),
        metadata={"whatsapp_from_owner": True} if from_owner else {},
    )


def test_customer_facing_bot_turn_excludes_owner_and_self_chat(monkeypatch):
    monkeypatch.setenv("WHATSAPP_MODE", "bot")

    assert _customer_facing_whatsapp_bot_turn(_event()) is True
    assert _customer_facing_whatsapp_bot_turn(_event(from_owner=True)) is False
    assert _customer_facing_whatsapp_bot_turn(_event(platform=Platform.TELEGRAM)) is False

    monkeypatch.setenv("WHATSAPP_MODE", "self-chat")
    assert _customer_facing_whatsapp_bot_turn(_event()) is False


def test_customer_facing_turn_requires_per_platform_opt_in_for_internal_display():
    source = SimpleNamespace(_customer_facing_bot_turn=True)

    assert (
        _requires_explicit_internal_display_opt_in(
            {}, "whatsapp", "tool_progress", source
        )
        is True
    )
    assert (
        _requires_explicit_internal_display_opt_in(
            {
                "display": {
                    "tool_progress": "all",
                    "platforms": {"whatsapp": {"tool_progress": "new"}},
                }
            },
            "whatsapp",
            "tool_progress",
            source,
        )
        is False
    )


def test_regular_turn_does_not_require_extra_internal_display_opt_in():
    source = SimpleNamespace(_customer_facing_bot_turn=False)

    assert (
        _requires_explicit_internal_display_opt_in(
            {}, "whatsapp", "tool_progress", source
        )
        is False
    )


@pytest.mark.asyncio
async def test_customer_facing_bot_turn_drops_operator_notices():
    runner = object.__new__(GatewayRunner)
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.WHATSAPP: adapter}
    runner.config = None
    source = SimpleNamespace(
        platform=Platform.WHATSAPP,
        chat_id="customer@s.whatsapp.net",
        _customer_facing_bot_turn=True,
    )

    await runner._deliver_platform_notice(source, "Type /sethome")

    adapter.send.assert_not_awaited()


def test_customer_facing_bot_turn_drops_status_callbacks():
    runner = object.__new__(TurnRunner)
    status_adapter = MagicMock()
    runner._ctx = SimpleNamespace(
        source=SimpleNamespace(_customer_facing_bot_turn=True),
        _status_adapter=status_adapter,
    )

    runner._status_callback_sync("lifecycle", "internal retry details")

    assert status_adapter.mock_calls == []