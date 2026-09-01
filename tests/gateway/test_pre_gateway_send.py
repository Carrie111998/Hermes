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


@pytest.mark.asyncio
async def test_redirect_fail_closed_on_missing_adapter(monkeypatch):
    """When redirect target platform has no adapter, message is dropped (fail-closed)."""
    gateway = SimpleNamespace(
        config=SimpleNamespace(gate=None),
        adapters={},  # no adapters at all
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

    # Original adapter should NOT be called (fail-closed)
    adapter.send.assert_not_awaited()
    assert result == "reply_1"


@pytest.mark.asyncio
async def test_redirect_fail_closed_on_malformed_target(monkeypatch):
    """When redirect target is malformed (no colon), message is dropped (fail-closed)."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "redirect", "target": "no-colon-here"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_not_awaited()
    assert result == "reply_1"


@pytest.mark.asyncio
async def test_redirect_normalizes_platform_case(monkeypatch):
    """Redirect with lowercase platform string ('telegram') still works."""
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

    adapter.send.assert_not_awaited()
    redirect_adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_hook_context_missing_gateway(monkeypatch):
    """hook_context with gateway=None skips the hook entirely."""
    called = {"count": 0}

    def _fake_hook(name, **kwargs):
        called["count"] += 1
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = {"gateway": None, "source": None, "chat_type": "group", "session_store": None}
    consumer, adapter = _make_consumer(hook_context=ctx)

    await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    assert called["count"] == 0
    adapter.send.assert_awaited_once()


# --- builtin_hooks reference gate tests ---


class TestBuiltinOutboundGate:
    """Tests for the reference pre_gateway_send gate in builtin_hooks."""

    def test_dm_always_passes(self):
        from gateway.builtin_hooks import outbound_gate

        assert outbound_gate("admin panel access", chat_type="dm") is None

    def test_group_without_config_passes(self):
        from gateway.builtin_hooks import outbound_gate

        gateway = SimpleNamespace(config=SimpleNamespace(gate=None))
        assert outbound_gate("admin panel access", chat_type="group", gateway=gateway) is None

    def test_group_with_gate_disabled_passes(self):
        from gateway.builtin_hooks import outbound_gate

        gate_cfg = SimpleNamespace(enabled=False)
        gateway = SimpleNamespace(config=SimpleNamespace(gate=gate_cfg))
        assert outbound_gate("admin panel access", chat_type="group", gateway=gateway) is None

    def test_group_with_gate_enabled_blocks_admin_content(self):
        from gateway.builtin_hooks import outbound_gate

        gate_cfg = SimpleNamespace(enabled=True, group_chat_types=["group", "supergroup"], redirect_target="")
        gateway = SimpleNamespace(config=SimpleNamespace(gate=gate_cfg))
        result = outbound_gate("admin panel access", chat_type="group", gateway=gateway)
        assert result == {"action": "block", "reason": "admin-content-gate"}

    def test_group_with_gate_enabled_passes_normal_content(self):
        from gateway.builtin_hooks import outbound_gate

        gate_cfg = SimpleNamespace(enabled=True, group_chat_types=["group", "supergroup"], redirect_target="")
        gateway = SimpleNamespace(config=SimpleNamespace(gate=gate_cfg))
        assert outbound_gate("Hello, how are you?", chat_type="group", gateway=gateway) is None

    def test_group_with_redirect_target(self):
        from gateway.builtin_hooks import outbound_gate

        gate_cfg = SimpleNamespace(enabled=True, group_chat_types=["group", "supergroup"], redirect_target="telegram:ADMIN_CHAT")
        gateway = SimpleNamespace(config=SimpleNamespace(gate=gate_cfg))
        result = outbound_gate("admin panel access", chat_type="group", gateway=gateway)
        assert result == {"action": "redirect", "target": "telegram:ADMIN_CHAT"}

    def test_channel_type_passes(self):
        from gateway.builtin_hooks import outbound_gate

        gate_cfg = SimpleNamespace(enabled=True, group_chat_types=["group", "supergroup"], redirect_target="")
        gateway = SimpleNamespace(config=SimpleNamespace(gate=gate_cfg))
        assert outbound_gate("admin panel access", chat_type="channel", gateway=gateway) is None

    def test_admin_patterns_match(self):
        from gateway.builtin_hooks import _is_admin_content

        assert _is_admin_content("admin panel access") is True
        assert _is_admin_content("internal debug message") is True
        assert _is_admin_content("recovery job started") is True
        assert _is_admin_content("cold-start cursor restore") is True
        assert _is_admin_content("system notification alert") is True
        assert _is_admin_content("Hello, how are you?") is False
        assert _is_admin_content("The weather is nice today") is False


# --- Composition semantics tests ---


@pytest.mark.asyncio
async def test_composition_allow_then_block_blocks(monkeypatch):
    """[allow, block] → block. Safety plugin registered after permissive one still wins."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "allow"}, {"action": "block", "reason": "safety"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_not_awaited()
    assert result == "reply_1"


@pytest.mark.asyncio
async def test_composition_none_then_redirect_redirects(monkeypatch):
    """[None, redirect] → redirect. First actionable result wins."""
    redirect_adapter = AsyncMock()
    redirect_adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="redir_msg"))

    gateway = SimpleNamespace(
        config=SimpleNamespace(gate=None),
        adapters={Platform.TELEGRAM: redirect_adapter},
        session_store=MagicMock(),
    )

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [None, {"action": "redirect", "target": "telegram:99999"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context(gateway=gateway)
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_not_awaited()
    redirect_adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_composition_multiple_allows_delivers(monkeypatch):
    """[allow, allow, allow] → allow (no actionable result, default allow)."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "allow"}, {"action": "allow"}, {"action": "allow"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_awaited_once()
    assert result == "msg_1"


@pytest.mark.asyncio
async def test_composition_block_then_allow_blocks(monkeypatch):
    """[block, allow] → block. First actionable result wins."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "block", "reason": "first"}, {"action": "allow"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_not_awaited()
    assert result == "reply_1"


@pytest.mark.asyncio
async def test_composition_conflicting_non_allow_first_wins(monkeypatch):
    """[block, redirect] → block. First actionable result wins."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [
                {"action": "block", "reason": "first"},
                {"action": "redirect", "target": "telegram:99999"},
            ]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_new_chunk("Hello world", "reply_1", final=False)

    adapter.send.assert_not_awaited()
    assert result == "reply_1"


# --- Real dispatch path tests ---


@pytest.mark.asyncio
async def test_gate_fires_on_send_or_edit_path(monkeypatch):
    """The gate fires through _send_or_edit (the main streaming delivery path)."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "block", "reason": "gate-on-edit-path"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._send_or_edit("Hello world", finalize=False)

    # _send_or_edit returns False when blocked
    assert result is False
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_fires_on_try_fresh_final_path(monkeypatch):
    """The gate fires through _try_fresh_final."""
    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_send":
            return [{"action": "block", "reason": "gate-on-fresh-final"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    ctx = _make_hook_context()
    consumer, adapter = _make_consumer(hook_context=ctx)

    result = await consumer._try_fresh_final("Hello world")

    # _try_fresh_final returns False when blocked
    assert result is False
    adapter.send.assert_not_awaited()
