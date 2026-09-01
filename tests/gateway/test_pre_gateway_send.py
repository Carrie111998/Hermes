"""Tests for the pre_gateway_send plugin hook.

The hook allows plugins to intercept outbound messages before they reach
the platform adapter. It runs in GatewayStreamConsumer._send_new_chunk()
and acts on returned action dicts:
  {"action": "allow"} / None  -> normal delivery
  {"action": "block", "reason": "..."}  -> silently drop
  {"action": "redirect", "target": "platform:chat_id"}  -> send elsewhere
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _make_consumer(hook_context=None, platform=Platform.WHATSAPP):
    """Build a GatewayStreamConsumer with a mock adapter."""
    adapter = AsyncMock()
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="msg_1"))
    adapter._platform = platform
    adapter.truncate_message = None
    adapter.max_message_length = 4096
    adapter.formatting_mode = "plain"

    consumer = GatewayStreamConsumer(
        adapter=adapter,
        chat_id="chat_123",
        config=StreamConsumerConfig(),
        hook_context=hook_context,
    )
    return consumer, adapter


def _make_hook_context(gateway=None, source=None, chat_type="group"):
    """Build a hook_context dict."""
    if gateway is None:
        gateway = SimpleNamespace(
            config=SimpleNamespace(gate=None),
            adapters={},
            session_store=MagicMock(),
        )
    if source is None:
        source = SimpleNamespace(
            platform=Platform.WHATSAPP,
            user_id="user_1",
            chat_id="chat_123",
            user_name="tester",
            chat_type=chat_type,
        )
    return {
        "gateway": gateway,
        "source": source,
        "chat_type": chat_type,
        "session_store": getattr(gateway, "session_store", None),
    }


@pytest.mark.asyncio
async def test_block_action_drops_message(monkeypatch):
    """Hook returning block prevents adapter.send from being called."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "block", "reason": "test-block"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_not_awaited()
    assert result == "reply_1"  # returns reply_to_id (drop)


@pytest.mark.asyncio
async def test_allow_action_delivers_normally(monkeypatch):
    """Hook returning allow lets the message through."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "allow"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_awaited_once()
    assert result == "msg_1"


@pytest.mark.asyncio
async def test_none_result_delivers_normally(monkeypatch):
    """Hook returning None lets the message through."""
    def _fake_hook(name, **kwargs):
        return [None]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_redirect_action_sends_to_different_target(monkeypatch):
    """Hook returning redirect sends to the redirect target instead."""
    redirect_adapter = AsyncMock()
    redirect_adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="redir_msg"))

    gateway = SimpleNamespace(
        config=SimpleNamespace(gate=None),
        adapters={Platform.TELEGRAM: redirect_adapter},
        session_store=MagicMock(),
    )

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "redirect", "target": "telegram:99999"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context(gateway=gateway)
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    # Original adapter should NOT be called
    adapter.send.assert_not_awaited()
    # Redirect adapter should be called
    redirect_adapter.send.assert_awaited_once()
    call_kwargs = redirect_adapter.send.call_args
    assert call_kwargs.kwargs["chat_id"] == "99999" or call_kwargs[1].get("chat_id") == "99999"


@pytest.mark.asyncio
async def test_hook_exception_falls_through(monkeypatch):
    """Hook exception is caught and message delivers normally."""
    def _failing_hook(name, **kwargs):
        raise RuntimeError("plugin crash")

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _failing_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    # Should still deliver
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_hook_context_skips_hook(monkeypatch):
    """Consumer without hook_context never invokes the hook."""
    called = {"count": 0}
    def _fake_hook(name, **kwargs):
        called["count"] += 1
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    consumer, adapter = _make_consumer(hook_context=None)

    await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    assert called["count"] == 0
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_block_returns_reply_to_id(monkeypatch):
    """When blocked, returns the original reply_to_id (not None)."""
    def _fake_hook(name, **kwargs):
        return [{"action": "block", "reason": "test"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("text", "original_reply", final=True)
    assert result == "original_reply"
