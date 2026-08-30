from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import MessageEvent, Platform, SessionSource
from gateway.run import GatewayRunner
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class _Adapter:
    platform = Platform.WHATSAPP

    def __init__(self, claim):
        self.config = SimpleNamespace(extra={"exclusive_inbound": claim})
        self.exclusive_handler = None
        self.send = AsyncMock(
            return_value=SimpleNamespace(success=True, error=None)
        )

    def set_exclusive_inbound_handler(self, handler):
        self.exclusive_handler = handler


def _event(chat_id="codex@g.us", user_id="15551234567", message_id="m1"):
    return MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.WHATSAPP,
            chat_id=chat_id,
            chat_type="group",
            user_id=user_id,
        ),
        message_id=message_id,
    )


def _runner(authorized=True):
    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized_for_source = MagicMock(return_value=authorized)
    return runner


def _claim():
    return {"chat_id": "codex@g.us", "handler": "codex_bridge"}


def _sender_claim():
    return {
        "chat_id": "codex@g.us",
        "handler": "codex_bridge",
        "allowed_senders": ["15551234567"],
    }


@pytest.mark.asyncio
async def test_unrelated_chat_falls_through_without_auth_or_plugin(monkeypatch):
    runner = _runner()
    adapter = _Adapter(_claim())
    get_manager = MagicMock()
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", get_manager)

    runner._configure_exclusive_inbound(adapter)

    assert await adapter.exclusive_handler(_event(chat_id="other@g.us")) is False
    runner._is_user_authorized_for_source.assert_not_called()
    get_manager.assert_not_called()


@pytest.mark.asyncio
async def test_exact_chat_awaits_one_registered_durable_admission(monkeypatch):
    runner = _runner()
    adapter = _Adapter(_claim())
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="bridge"), manager)
    admitted = []

    async def accept(event):
        admitted.append(event.message_id)
        return True

    context.register_exclusive_inbound_handler("codex_bridge", accept)
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    runner._configure_exclusive_inbound(adapter)

    event = _event()
    assert await adapter.exclusive_handler(event) is True
    assert admitted == ["m1"]
    assert event.source._authorization_profile_home is not None
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_sender_allowlist_and_gateway_authorization_are_both_required(monkeypatch):
    runner = _runner(authorized=True)
    adapter = _Adapter(_sender_claim())
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="bridge"), manager)
    admitted = []

    async def accept(event):
        admitted.append(event.message_id)
        return True

    context.register_exclusive_inbound_handler("codex_bridge", accept)
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    runner._configure_exclusive_inbound(adapter)

    assert await adapter.exclusive_handler(_event()) is True
    assert admitted == ["m1"]


@pytest.mark.asyncio
async def test_claim_sender_allowlist_cannot_bypass_revoked_gateway_authorization(monkeypatch):
    runner = _runner(authorized=False)
    adapter = _Adapter(_sender_claim())
    get_manager = MagicMock()
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", get_manager)
    runner._configure_exclusive_inbound(adapter)

    assert await adapter.exclusive_handler(_event()) is True
    get_manager.assert_not_called()


@pytest.mark.asyncio
async def test_claim_sender_allowlist_still_drops_other_sender(monkeypatch):
    runner = _runner(authorized=True)
    adapter = _Adapter(_sender_claim())
    get_manager = MagicMock()
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", get_manager)
    runner._configure_exclusive_inbound(adapter)

    assert await adapter.exclusive_handler(_event(user_id="19990000000")) is True
    get_manager.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "duplicate", "raises", "rejects", "sync"])
async def test_claimed_chat_fails_closed_when_handler_is_unavailable(
    monkeypatch, mode
):
    runner = _runner()
    adapter = _Adapter(_claim())
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="bridge"), manager)

    async def accept(_event):
        return True

    async def raises(_event):
        raise RuntimeError("boom")

    async def rejects(_event):
        return False

    if mode == "duplicate":
        context.register_exclusive_inbound_handler("codex_bridge", accept)
        context.register_exclusive_inbound_handler("codex_bridge", accept)
    elif mode == "raises":
        context.register_exclusive_inbound_handler("codex_bridge", raises)
    elif mode == "rejects":
        context.register_exclusive_inbound_handler("codex_bridge", rejects)
    elif mode == "sync":
        context.register_exclusive_inbound_handler(
            "codex_bridge", lambda _event: True
        )

    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    runner._configure_exclusive_inbound(adapter)

    assert await adapter.exclusive_handler(_event()) is True
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_can_return_bounded_visible_rejection(monkeypatch):
    runner = _runner()
    adapter = _Adapter(_claim())
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="bridge"), manager)

    async def reject(_event):
        return "That quoted Codex message is unknown or expired."

    context.register_exclusive_inbound_handler("codex_bridge", reject)
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    runner._configure_exclusive_inbound(adapter)

    assert await adapter.exclusive_handler(_event()) is True
    assert adapter.send.await_args.args[1] == (
        "That quoted Codex message is unknown or expired."
    )


@pytest.mark.asyncio
async def test_unauthorized_claim_is_consumed_without_invoking_plugin(monkeypatch):
    runner = _runner(authorized=False)
    adapter = _Adapter(_claim())
    get_manager = MagicMock()
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", get_manager)
    runner._configure_exclusive_inbound(adapter)

    assert await adapter.exclusive_handler(_event()) is True
    get_manager.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.parametrize(
    "claim",
    [
        None,
        [],
        {},
        {"chat_id": "codex@g.us"},
        {"handler": "bridge"},
        {"chat_id": "codex@g.us", "handler": "bridge", "allowed_senders": []},
    ],
)
def test_malformed_claim_is_rejected_at_adapter_configuration(claim):
    runner = _runner()
    adapter = _Adapter(claim)
    if claim is None:
        adapter.config.extra["exclusive_inbound"] = None
        runner._configure_exclusive_inbound(adapter)
        assert adapter.exclusive_handler is None
    else:
        with pytest.raises(ValueError):
            runner._configure_exclusive_inbound(adapter)


def test_registration_cleanup_removes_handler():
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="bridge"), manager)

    async def accept(_event):
        return True

    registration = context.register_exclusive_inbound_handler(
        "codex_bridge", accept
    )
    assert manager.iter_exclusive_inbound_handlers("codex_bridge") == (accept,)

    registration.dispose()

    assert manager.iter_exclusive_inbound_handlers("codex_bridge") == ()
