"""Public contract tests for the generic gateway user-message hook."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.message_hooks import (
    GatewayDelivery,
    GatewayDeliveryReceipt,
    GatewayMessageEvent,
    GatewayMessageRoute,
)
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import SessionSource, build_session_key
from gateway.config import GatewayConfig, Platform, PlatformConfig


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        profile="work",
        scope_id="guild-1",
        chat_id="channel-1",
        chat_name="general",
        chat_type="thread",
        thread_id="thread-1",
        user_id="user-1",
        user_name="Tester",
    )


def _event() -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.PHOTO,
        source=_source(),
        message_id="incoming-1",
        media_urls=["/tmp/a.png"],
        media_types=["image/png"],
        reply_to_message_id="prior-1",
        reply_to_text="prior text",
        metadata={"secret": "must-not-leak"},
        raw_message={"token": "must-not-leak"},
    )


def test_message_and_route_contexts_are_immutable_normalized_snapshots():
    event = _event()

    normalized_event = GatewayMessageEvent.from_event(event)
    route = GatewayMessageRoute.from_source(event.source, session_key="route-key")

    assert normalized_event.text == "hello"
    assert normalized_event.message_type == "photo"
    assert normalized_event.media_urls == ("/tmp/a.png",)
    assert normalized_event.media_types == ("image/png",)
    assert normalized_event.reply_to_message_id == "prior-1"
    assert route.platform == "discord"
    assert route.profile == "work"
    assert route.scope_id == "guild-1"
    assert route.session_key == "route-key"

    with pytest.raises(FrozenInstanceError):
        normalized_event.text = "changed"
    with pytest.raises(FrozenInstanceError):
        route.chat_id = "elsewhere"

    assert not hasattr(normalized_event, "raw_message")
    assert not hasattr(normalized_event, "metadata")
    assert not hasattr(normalized_event, "source")
    assert not hasattr(route, "role_authorized")
    assert not hasattr(route, "delivered_via_upstream_relay")


def test_route_snapshot_fills_resolved_active_profile_when_source_is_unset(monkeypatch):
    source = _source()
    source.profile = None
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )

    route = GatewayMessageRoute.from_source(source, session_key="route-key")

    assert route.profile == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native_result", "expected"),
    [
        (
            SendResult(success=True, message_id="out-1"),
            GatewayDeliveryReceipt(status="sent", message_id="out-1"),
        ),
        (
            SendResult(success=False, error="rejected with credential-shaped detail"),
            GatewayDeliveryReceipt(status="failed"),
        ),
        (
            SendResult(success=True, message_id=None),
            GatewayDeliveryReceipt(status="unknown"),
        ),
        (None, GatewayDeliveryReceipt(status="unknown")),
        (True, GatewayDeliveryReceipt(status="unknown")),
    ],
)
async def test_route_delivery_returns_truthful_normalized_receipts(native_result, expected):
    sent_content = []

    async def send_native(content: str):
        sent_content.append(content)
        return native_result

    delivery = GatewayDelivery(send_native)

    assert await delivery.send("host delivery") == expected
    assert sent_content == ["host delivery"]
    assert not hasattr(delivery, "adapter")
    assert not hasattr(delivery, "_adapter")
    assert not hasattr(delivery, "runner")
    assert not hasattr(delivery, "gateway")
    assert not hasattr(delivery, "credentials")
    assert not hasattr(delivery, "event")
    assert not hasattr(delivery, "_send_callback")


@pytest.mark.asyncio
async def test_route_delivery_reports_raised_send_as_failed_without_native_details():
    async def send_native(_content: str):
        raise RuntimeError("transport down")

    receipt = await GatewayDelivery(send_native).send("hello")

    assert receipt.status == "failed"
    assert receipt.message_id is None
    assert not hasattr(receipt, "error")


def test_route_delivery_does_not_expose_native_send_callback():
    async def send_native(_content: str):
        return SendResult(success=True, message_id="out-1")

    delivery = GatewayDelivery(send_native)

    assert not hasattr(delivery, "_send_callback")
    assert not hasattr(delivery, "__dict__")


def _runner_for_dispatch():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True)}
    )
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="out-1")),
        _pending_messages={},
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {"work": {Platform.DISCORD: adapter}}
    runner.pairing_store = MagicMock()
    runner.pairing_store._is_rate_limited.return_value = False
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompt_pending = {}
    runner._startup_restore_in_progress = False
    runner._external_drain_active = False
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda _source: True
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._claim_active_session_slot = lambda *_args, **_kwargs: (None, None)
    runner._persist_active_agents = lambda: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = MagicMock()
    runner._release_turn_lease = MagicMock()
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="")
    runner._queue_or_replace_pending_event = MagicMock()
    runner.session_store = MagicMock()
    runner.hooks = SimpleNamespace(emit_collect=AsyncMock(return_value=[]))
    return runner, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["handled", "suppress"])
async def test_terminal_hook_handling_suppresses_cold_agent_dispatch(
    monkeypatch, decision
):
    runner, adapter = _runner_for_dispatch()
    captured = {}

    async def hook(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs
        receipt = await kwargs["delivery"].send("plugin reply")
        assert receipt == GatewayDeliveryReceipt(status="sent", message_id="out-1")
        return [{"decision": decision}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    raw_event = _event()
    result = await runner._handle_message(raw_event)

    assert result is None
    runner._handle_message_with_agent.assert_not_awaited()
    assert captured["name"] == "gateway_message"
    assert set(captured["kwargs"]) == {
        "event",
        "route",
        "delivery",
        "raise_exceptions",
        "stop_when",
    }
    assert captured["kwargs"]["raise_exceptions"] is True
    assert captured["kwargs"]["stop_when"]({"decision": "handled"}) is True
    assert captured["kwargs"]["stop_when"]({"decision": "pass"}) is False
    assert not isinstance(captured["kwargs"]["event"], MessageEvent)
    assert captured["kwargs"]["route"].session_key == build_session_key(_source())
    adapter.send.assert_awaited_once_with(
        "channel-1", "plugin reply", metadata={"thread_id": "thread-1"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["continue", "pass"])
async def test_continue_hook_decisions_reach_cold_agent_dispatch(monkeypatch, decision):
    runner, _adapter = _runner_for_dispatch()

    async def hook(_name, **_kwargs):
        return [{"decision": decision}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    await runner._handle_message(_event())

    runner._handle_message_with_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_awaited_cold_hook_cannot_start_duplicate_agents(monkeypatch):
    runner, adapter = _runner_for_dispatch()
    first_hook_entered = asyncio.Event()
    release_first_hook = asyncio.Event()
    hook_calls = 0
    active_agent_calls = 0
    max_concurrent_agent_calls = 0

    async def hook(_name, **_kwargs):
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            first_hook_entered.set()
            await release_first_hook.wait()
        return [{"decision": "pass"}]

    async def run_agent(*_args, **_kwargs):
        nonlocal active_agent_calls, max_concurrent_agent_calls
        active_agent_calls += 1
        max_concurrent_agent_calls = max(
            max_concurrent_agent_calls,
            active_agent_calls,
        )
        await asyncio.sleep(0)
        active_agent_calls -= 1
        return ""

    runner._handle_message_with_agent = run_agent
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    first = asyncio.create_task(runner._handle_message(_event()))
    await first_hook_entered.wait()
    second = asyncio.create_task(runner._handle_message(_event()))
    for _ in range(100):
        if hook_calls == 2:
            break
        await asyncio.sleep(0)
    release_first_hook.set()
    await asyncio.gather(first, second)

    assert hook_calls == 2
    assert max_concurrent_agent_calls == 1
    assert build_session_key(_source()) in adapter._pending_messages


@pytest.mark.asyncio
async def test_route_delivery_binds_relay_to_ingress_logical_platform(monkeypatch):
    runner, _native_adapter = _runner_for_dispatch()
    relay = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="wrong-route")),
        send_for_platform=AsyncMock(
            return_value=SendResult(success=True, message_id="relay-out")
        ),
        _pending_messages={},
    )
    runner.adapters = {Platform.RELAY: relay}
    event = _event()
    event.source.delivered_via_upstream_relay = True

    async def hook(_name, **kwargs):
        receipt = await kwargs["delivery"].send("relay reply")
        assert receipt == GatewayDeliveryReceipt(status="sent", message_id="relay-out")
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    await runner._handle_message(event)

    relay.send_for_platform.assert_awaited_once_with(
        Platform.DISCORD,
        "channel-1",
        "relay reply",
        metadata={"thread_id": "thread-1"},
    )
    relay.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmatched_none_hook_reaches_cold_agent_dispatch(monkeypatch):
    runner, _adapter = _runner_for_dispatch()

    async def hook(_name, **_kwargs):
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    await runner._handle_message(_event())

    runner._handle_message_with_agent.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_result", ["handled", {}, {"decision": "other"}])
async def test_invalid_terminal_hook_results_fail_closed(monkeypatch, invalid_result):
    runner, _adapter = _runner_for_dispatch()

    async def hook(_name, **_kwargs):
        return [invalid_result]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    assert await runner._handle_message(_event()) is None
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_hook_exception_fails_closed(monkeypatch):
    runner, _adapter = _runner_for_dispatch()

    async def hook(_name, **_kwargs):
        raise RuntimeError("hook failed")

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    assert await runner._handle_message(_event()) is None
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_hook_runs_on_active_session_before_busy_queue(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    session_key = build_session_key(_source())
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent

    async def hook(_name, **_kwargs):
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    assert await runner._handle_message(_event()) is None
    runner._queue_or_replace_pending_event.assert_not_called()
    running_agent.interrupt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_mutation", "authorized"),
    [
        (lambda event: setattr(event, "internal", True), True),
        (lambda _event: None, False),
        (lambda event: setattr(event, "text", "/help"), True),
    ],
)
async def test_hook_never_runs_for_internal_unauthorized_or_slash_events(
    monkeypatch, event_mutation, authorized
):
    runner, _adapter = _runner_for_dispatch()
    runner._is_user_authorized = lambda _source: authorized
    runner._handle_help_command = AsyncMock(return_value="help")
    hook = AsyncMock(return_value=[{"decision": "handled"}])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)
    event = _event()
    if not authorized:
        event.source.chat_type = "group"
    event_mutation(event)

    await runner._handle_message(event)

    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_runs_after_pending_update_response_intercept(monkeypatch, tmp_path):
    runner, _adapter = _runner_for_dispatch()
    event = _event()
    event.text = "yes"
    session_key = build_session_key(event.source)
    runner._update_prompt_pending[session_key] = True
    hook = AsyncMock(return_value=[{"decision": "handled"}])
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    result = await runner._handle_message(event)

    assert "Sent" in result
    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_session_stop_notifies_cancel_hook_before_interrupt(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    source = _source()
    session_key = build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    runner._notify_gateway_session_cancel = AsyncMock()
    runner._interrupt_and_clear_session = AsyncMock()
    event = _event()
    event.text = "/stop"
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])

    await runner._handle_message(event)

    runner._notify_gateway_session_cancel.assert_awaited_once_with(
        session_key,
        source,
        reason="stop",
    )
    runner._interrupt_and_clear_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_session_cancel_hook_receives_only_route_and_reason(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    captured = {}

    async def hook(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)
    source = _source()
    session_key = build_session_key(source)

    await runner._notify_gateway_session_cancel(session_key, source, reason="stop")

    assert captured["name"] == "gateway_session_cancel"
    assert set(captured["kwargs"]) == {"route", "reason"}
    assert captured["kwargs"]["reason"] == "stop"
    assert captured["kwargs"]["route"] == GatewayMessageRoute.from_source(
        source, session_key=session_key
    )
