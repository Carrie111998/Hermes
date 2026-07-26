"""Public contract tests for the generic gateway user-message hook."""

import asyncio
import dataclasses
import threading
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
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
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
        metadata={
            "secret": "must-not-leak",
            "mentioned_user_ids": ["user-2", "user-3"],
            "mentions_room": True,
        },
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
    assert normalized_event.mentioned_user_ids == ("user-2", "user-3")
    assert normalized_event.mentions_room is True
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


def test_message_snapshot_marks_unattested_mentions_unknown():
    event = _event()
    event.metadata["mentioned_user_ids"] = None
    event.metadata["mentions_room"] = "unknown"

    normalized_event = GatewayMessageEvent.from_event(event)

    assert normalized_event.mentioned_user_ids is None
    assert normalized_event.mentions_room is None


def test_message_hook_snapshot_bounds_oversized_fields_and_declares_gaps():
    event = _event()
    event.text = "x" * 2_000_000
    event.reply_to_text = "r" * 100_000
    event.message_id = "m" * 5_000
    event.media_urls = [f"https://example.invalid/{index}/" + "u" * 5_000 for index in range(50)]
    event.media_types = ["t" * 500 for _ in range(50)]
    event.metadata["mentioned_user_ids"] = [f"user-{index}" for index in range(500)]

    snapshot = GatewayMessageEvent.from_event(event)

    assert len(snapshot.text.encode("utf-8")) <= 65_536
    assert len((snapshot.reply_to_text or "").encode("utf-8")) <= 16_384
    assert len((snapshot.message_id or "").encode("utf-8")) <= 512
    assert len(snapshot.media_urls) <= 16
    assert all(len(item.encode("utf-8")) <= 4_096 for item in snapshot.media_urls)
    assert len(snapshot.media_types) <= 16
    assert len(snapshot.mentioned_user_ids or ()) <= 128
    assert {
        "text-overflow",
        "reply-text-overflow",
        "message-id-overflow",
        "media-count-overflow",
        "media-url-overflow",
        "media-type-overflow",
        "mention-count-overflow",
    }.issubset(set(snapshot.coverage_gaps))


def test_route_snapshot_bounds_oversized_fields_and_declares_gaps():
    source = _source()
    source.chat_id = "c" * 5_000
    source.user_name = "u" * 10_000

    route = GatewayMessageRoute.from_source(source, session_key="s" * 10_000)

    assert len(route.session_key.encode("utf-8")) <= 2_048
    assert len(route.chat_id.encode("utf-8")) <= 512
    assert len((route.user_name or "").encode("utf-8")) <= 4_096
    assert {
        "session-key-overflow",
        "chat-id-overflow",
        "user-name-overflow",
    }.issubset(set(route.coverage_gaps))


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
async def test_route_delivery_reports_raised_send_as_unknown_without_native_details():
    async def send_native(_content: str):
        raise RuntimeError("transport down")

    receipt = await GatewayDelivery(send_native).send("hello")

    assert receipt.status == "unknown"
    assert receipt.message_id is None
    assert not hasattr(receipt, "error")


@pytest.mark.asyncio
async def test_route_delivery_does_not_expose_native_send_callback():
    import gateway.message_hooks as message_hooks

    async def send_native(_content: str):
        return SendResult(success=True, message_id="out-1")

    delivery = GatewayDelivery(send_native)

    assert not hasattr(delivery, "_send_callback")
    assert not hasattr(delivery, "__dict__")
    assert not hasattr(message_hooks, "_DELIVERY_SEND_CALLBACKS")
    assert send_native not in vars(message_hooks).values()
    channels = list(message_hooks._DELIVERY_CHANNELS.values())
    assert channels
    await asyncio.sleep(0)
    assert all(not channel.queue._getters for channel in channels)
    assert all(
        not any(callable(value) for value in (channel.queue, channel.revoked, channel.consumed))
        for channel in channels
    )
    assert delivery.send.__func__.__closure__ is None
    delivery._revoke()


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
async def test_route_multiplexed_hook_runs_inside_resolved_profile_scope(
    monkeypatch, tmp_path
):
    from hermes_cli.config import get_hermes_home

    runner, _adapter = _runner_for_dispatch()
    runner.config.multiplex_profiles = True
    default_home = tmp_path / "default"
    work_home = tmp_path / "profiles" / "work"
    default_home.mkdir(parents=True)
    work_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setattr(
        runner,
        "_resolve_profile_home_for_source",
        lambda _source: work_home,
    )
    observed = {}

    async def hook(_name, **kwargs):
        observed["home"] = get_hermes_home()
        observed["profile"] = kwargs["route"].profile
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    await runner._handle_message(_event())

    assert observed == {"home": work_home, "profile": "work"}


@pytest.mark.asyncio
async def test_route_multiplexed_cancel_hook_runs_inside_resolved_profile_scope(
    monkeypatch,
    tmp_path,
):
    from hermes_cli.config import get_hermes_home

    runner, _adapter = _runner_for_dispatch()
    runner.config.multiplex_profiles = True
    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(
        runner,
        "_resolve_profile_home_for_source",
        lambda _source: profile_home,
    )
    observed_homes = []

    async def hook(name, **_kwargs):
        if name == "gateway_session_cancel":
            observed_homes.append(get_hermes_home())
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)
    source = _source()
    source.profile = "work"

    await runner._notify_gateway_session_cancel(
        build_session_key(source, profile="work"),
        source,
        reason="stop",
    )

    assert observed_homes == [profile_home.resolve()]


@pytest.mark.asyncio
async def test_route_delivery_reply_is_bound_to_ingress_message(monkeypatch):
    runner, adapter = _runner_for_dispatch()
    receipts = []

    async def hook(name, **kwargs):
        if name == "gateway_message":
            receipts.append(await kwargs["delivery"].reply("threaded"))
            return [{"decision": "handled"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)
    event = _event()
    event.message_id = "in-1"

    await runner._handle_message(event)

    assert receipts == [GatewayDeliveryReceipt(status="sent", message_id="out-1")]
    adapter.send.assert_awaited_once_with(
        "channel-1",
        "threaded",
        reply_to="in-1",
        metadata={"thread_id": "thread-1"},
    )


@pytest.mark.asyncio
async def test_route_delivery_reaction_is_bound_to_ingress_message(monkeypatch):
    runner, adapter = _runner_for_dispatch()
    adapter.react = AsyncMock(
        return_value=SendResult(success=True, message_id="in-1")
    )
    receipts = []

    async def hook(name, **kwargs):
        if name == "gateway_message":
            receipts.append(
                await kwargs["delivery"].react("👍", operation="add")
            )
            return [{"decision": "handled"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)
    event = _event()
    event.message_id = "in-1"

    await runner._handle_message(event)

    assert receipts == [GatewayDeliveryReceipt(status="sent", message_id="in-1")]
    adapter.react.assert_awaited_once_with(
        "channel-1",
        "in-1",
        "👍",
        operation="add",
    )
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_cancel_revokes_retained_delivery_before_observer(monkeypatch):
    runner, adapter = _runner_for_dispatch()
    retained = []
    cancel_observed_revoked = []

    async def hook(name, **kwargs):
        if name == "gateway_message":
            retained.append(kwargs["delivery"])
            return [{"decision": "handled"}]
        if name == "gateway_session_cancel":
            cancel_observed_revoked.append(
                (await retained[0].send("too late")).status
            )
            return []
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    event = _event()
    await runner._handle_message(event)
    session_key = build_session_key(event.source)
    await runner._notify_gateway_session_cancel(
        session_key,
        event.source,
        reason="stop",
    )

    assert cancel_observed_revoked == ["failed"]
    assert (await retained[0].send("still too late")).status == "failed"
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_revokes_retained_delivery_without_active_agent(monkeypatch):
    runner, adapter = _runner_for_dispatch()
    retained = []

    async def hook(name, **kwargs):
        if name == "gateway_message":
            retained.append(kwargs["delivery"])
            return [{"decision": "handled"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)
    await runner._handle_message(_event())

    runner._revoke_all_gateway_deliveries()

    assert (await retained[0].send("after drain")).status == "failed"
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_ingress_revokes_prior_retained_delivery(monkeypatch):
    runner, adapter = _runner_for_dispatch()
    retained = []

    async def hook(name, **kwargs):
        if name == "gateway_message":
            retained.append(kwargs["delivery"])
            return [{"decision": "handled"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    first = _event()
    first.message_id = "m1"
    second = _event()
    second.message_id = "m2"
    await runner._handle_message(first)
    await runner._handle_message(second)

    assert len(retained) == 2
    assert (await retained[0].send("superseded")).status == "failed"
    assert (await retained[1].send("current")).status == "sent"
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_text_batch_invokes_hook_once_per_native_event(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    first = _event()
    first.message_id = "m1"
    first.text = "ordinary"
    first.metadata["mentioned_user_ids"] = ()
    second = _event()
    second.message_id = "m2"
    second.text = "@bot"
    second.source.user_id = "8"
    second.source.user_name = "second-user"
    second.metadata["mentioned_user_ids"] = ("9",)
    merged = _event()
    merged.message_id = "m1"
    merged.text = "ordinary\n@bot"
    merged.metadata.update(
        {
            "mentioned_user_ids": ("9",),
            "_gateway_native_events": (first, second),
        }
    )
    observed = []

    async def hook(_name, **kwargs):
        observed.append(
            (
                kwargs["event"].message_id,
                kwargs["event"].text,
                kwargs["event"].mentioned_user_ids,
                kwargs["route"].user_id,
            )
        )
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    await runner._handle_message(merged)

    assert observed == [
        ("m1", "ordinary", (), "user-1"),
        ("m2", "@bot", ("9",), "8"),
    ]
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_pre_dispatch_skip_runs_before_gateway_message(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    event = _event()
    session_key = build_session_key(event.source)
    participant_hook = AsyncMock()

    def pre_hook(name, **_kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "skip", "reason": "fixture"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", pre_hook)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", participant_hook)

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    participant_hook.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker",
    ["_hermes_startup_restore_replay", "_hermes_historical_replay"],
)
async def test_replayed_ingress_never_reaches_gateway_message(monkeypatch, marker):
    runner, _adapter = _runner_for_dispatch()
    event = _event()
    setattr(event, marker, True)
    participant_hook = AsyncMock()
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", participant_hook)

    suppressed = await runner._run_gateway_message_hook(
        event,
        event.source,
        build_session_key(event.source),
    )

    assert suppressed is False
    participant_hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_base_adapter_busy_ingress_reaches_gateway_message_hook_once(monkeypatch):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, _chat_id, _content, **_kwargs):
        return SendResult(success=True, message_id="busy-out")

    Adapter = type(
        "BusyHookAdapter",
        (BasePlatformAdapter,),
        {"connect": connect, "disconnect": disconnect, "send": send},
    )
    Adapter.__abstractmethods__ = frozenset()

    runner, _old_adapter = _runner_for_dispatch()
    adapter = Adapter(PlatformConfig(enabled=True), Platform.DISCORD)
    adapter._message_handler = AsyncMock()
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {"work": {Platform.DISCORD: adapter}}
    runner._busy_ack_ts = {}

    event = MessageEvent(
        text="busy follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="busy-in",
    )
    session_key = build_session_key(event.source)
    running_agent = MagicMock()
    running_agent.get_activity_summary.return_value = {}
    runner._running_agents[session_key] = running_agent
    adapter._active_sessions[session_key] = asyncio.Event()
    calls = []

    async def hook(name, **kwargs):
        calls.append((name, kwargs["route"].session_key))
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook)

    await adapter.handle_message(event)

    assert calls == [("gateway_message", session_key)]
    running_agent.interrupt.assert_not_called()
    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_busy_pass_event_crosses_gateway_message_hook_only_once_after_replay(monkeypatch):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, _chat_id, _content, **_kwargs):
        return SendResult(success=True, message_id="busy-pass-out")

    Adapter = type(
        "BusyPassAdapter",
        (BasePlatformAdapter,),
        {"connect": connect, "disconnect": disconnect, "send": send},
    )
    Adapter.__abstractmethods__ = frozenset()

    runner, _old_adapter = _runner_for_dispatch()
    runner._busy_text_mode = "queue"
    adapter = Adapter(PlatformConfig(enabled=True), Platform.DISCORD)
    adapter._message_handler = runner._handle_message
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {"work": {Platform.DISCORD: adapter}}
    runner._busy_ack_ts = {}

    event = MessageEvent(
        text="busy passing follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="busy-pass-in",
    )
    session_key = build_session_key(event.source)
    running_agent = MagicMock()
    running_agent.get_activity_summary.return_value = {}
    runner._running_agents[session_key] = running_agent
    adapter._active_sessions[session_key] = asyncio.Event()
    calls = []

    async def hook(name, **kwargs):
        calls.append((name, kwargs["event"].message_id))
        return [{"decision": "pass"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])

    await adapter.handle_message(event)
    assert calls == [("gateway_message", "busy-pass-in")]
    queued = adapter._pending_messages.pop(session_key)

    adapter._active_sessions.pop(session_key, None)
    runner._running_agents.pop(session_key, None)
    await adapter.handle_message(queued)
    await adapter._session_tasks[session_key]

    assert calls == [("gateway_message", "busy-pass-in")]


@pytest.mark.asyncio
async def test_busy_external_drain_rejects_before_gateway_message_hook(monkeypatch):
    runner, adapter = _runner_for_dispatch()
    runner._external_drain_active = True
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="drain-refusal")
    )
    event = _event()
    event.message_id = "busy-drain-in"
    session_key = build_session_key(event.source)
    calls = []

    async def hook(name, **_kwargs):
        calls.append(name)
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook)

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert calls == []
    adapter._send_with_retry.assert_awaited_once()
    assert "draining for a maintenance action" in adapter._send_with_retry.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_busy_multiplexed_route_uses_profile_scoped_session_key(monkeypatch):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, _chat_id, _content, **_kwargs):
        return SendResult(success=True, message_id="profile-out")

    Adapter = type(
        "BusyProfileAdapter",
        (BasePlatformAdapter,),
        {"connect": connect, "disconnect": disconnect, "send": send},
    )
    Adapter.__abstractmethods__ = frozenset()

    runner, _old_adapter = _runner_for_dispatch()
    runner.config.multiplex_profiles = True
    adapter = Adapter(PlatformConfig(enabled=True), Platform.DISCORD)
    adapter.gateway_runner = runner
    adapter._message_handler = runner._handle_message
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {"work": {Platform.DISCORD: adapter}}

    event = _event()
    event.source.profile = "work"
    event.message_id = "busy-profile-in"
    profile_key = runner._session_key_for_source(event.source)
    legacy_key = build_session_key(event.source)
    assert profile_key != legacy_key
    adapter._active_sessions[profile_key] = asyncio.Event()
    adapter._active_sessions[legacy_key] = asyncio.Event()
    runner._running_agents[profile_key] = MagicMock()
    runner._running_agents[legacy_key] = MagicMock()
    seen = []

    async def hook(_name, **kwargs):
        seen.append(kwargs["route"].session_key)
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook)

    await adapter.handle_message(event)

    assert seen == [profile_key]


@pytest.mark.asyncio
async def test_busy_slash_command_queues_without_gateway_message_hook(monkeypatch):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, _chat_id, _content, **_kwargs):
        return SendResult(success=True, message_id="command-out")

    Adapter = type(
        "BusyCommandAdapter",
        (BasePlatformAdapter,),
        {"connect": connect, "disconnect": disconnect, "send": send},
    )
    Adapter.__abstractmethods__ = frozenset()

    runner, _old_adapter = _runner_for_dispatch()
    adapter = Adapter(PlatformConfig(enabled=True), Platform.DISCORD)
    adapter._message_handler = AsyncMock(return_value="unknown command")
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {"work": {Platform.DISCORD: adapter}}
    event = _event()
    event.text = "/plugin-command"
    event.message_id = "busy-command-in"
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    seen = []

    async def hook(name, **_kwargs):
        seen.append(name)
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook)

    await adapter.handle_message(event)

    assert seen == []
    adapter._message_handler.assert_not_awaited()
    assert adapter._pending_messages[session_key] is event


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
        metadata={
            "thread_id": "thread-1",
            "scope_id": "guild-1",
            "user_id": "user-1",
        },
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
async def test_active_session_drain_gate_prevents_hook_side_effects(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    session_key = build_session_key(_source())
    runner._running_agents[session_key] = MagicMock()
    runner._draining = True
    hook = AsyncMock(return_value=[])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook, raising=False)

    result = await runner._handle_message(_event())

    hook.assert_not_awaited()
    assert "not accepting" in str(result).lower()


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
async def test_active_session_new_notifies_cancel_hook_before_interrupt(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    source = _source()
    session_key = build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    order = []

    async def notify(*_args, **_kwargs):
        order.append("notify")

    async def interrupt(*_args, **_kwargs):
        order.append("interrupt")

    async def reset(*_args, **kwargs):
        order.append(("reset", kwargs))
        return "reset"

    runner._notify_gateway_session_cancel = notify
    runner._interrupt_and_clear_session = interrupt
    runner._handle_reset_command = reset
    event = _event()
    event.text = "/new"
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])

    assert await runner._handle_message(event) == "reset"

    assert order == ["notify", "interrupt", ("reset", {"cancel_notified": True})]


@pytest.mark.asyncio
async def test_cold_hook_sees_recovered_telegram_route(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        profile="default",
        scope_id="scope-1",
        chat_id="chat-1",
        chat_name="Room",
        chat_type="dm",
        thread_id="stale-thread",
        user_id="user-1",
        user_name="User",
    )
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="native-1")),
        _pending_messages={},
        _active_sessions={},
        _session_locks={},
        _stop_typing=AsyncMock(),
        _pending_lock=threading.Lock(),
        cancel_pending_drain=lambda _key: None,
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True, token="***")
    runner._recover_telegram_topic_thread_id = lambda _source: "canonical-thread"
    runner._is_telegram_topic_root_lobby = lambda _source: False
    captured = {}

    async def hook(_name, **kwargs):
        captured.update(kwargs)
        return [{"decision": "handled"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook)
    event = MessageEvent(text="hello", source=source, message_id="in-1")

    await runner._handle_message(event)

    assert captured["route"].thread_id == "canonical-thread"
    assert captured["route"].session_key == build_session_key(
        dataclasses.replace(source, thread_id="canonical-thread")
    )
    runner._handle_message_with_agent.assert_not_awaited()


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
    assert set(captured["kwargs"]) == {"route", "reason", "offload_callbacks"}
    assert captured["kwargs"]["offload_callbacks"] is True
    assert captured["kwargs"]["reason"] == "stop"
    assert captured["kwargs"]["route"] == GatewayMessageRoute.from_source(
        source, session_key=session_key
    )


@pytest.mark.asyncio
async def test_gateway_session_cancel_hook_timeout_does_not_block_stop(monkeypatch):
    runner, _adapter = _runner_for_dispatch()
    entered = asyncio.Event()

    async def stuck_hook(_name, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", stuck_hook)
    monkeypatch.setattr("gateway.run.GATEWAY_SESSION_CANCEL_TIMEOUT_SECONDS", 0.01)
    source = _source()

    await runner._notify_gateway_session_cancel(
        build_session_key(source),
        source,
        reason="stop",
    )

    assert entered.is_set()


@pytest.mark.asyncio
async def test_gateway_session_cancel_timeout_survives_cancel_suppressing_async_observer(
    monkeypatch,
):
    from gateway import run as run_module
    from hermes_cli.plugins import get_plugin_manager

    runner, _adapter = _runner_for_dispatch()
    manager = get_plugin_manager()
    original = list(manager._hooks.get("gateway_session_cancel", []))
    entered = threading.Event()
    release = threading.Event()

    async def cancellation_suppressing_observer(**_kwargs):
        entered.set()
        try:
            while not release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            while not release.is_set():
                await asyncio.sleep(0.01)

    manager._hooks["gateway_session_cancel"] = [cancellation_suppressing_observer]
    monkeypatch.setattr(run_module, "GATEWAY_SESSION_CANCEL_TIMEOUT_SECONDS", 0.01)
    source = _source()
    started = asyncio.get_running_loop().time()
    try:
        await asyncio.wait_for(
            runner._notify_gateway_session_cancel(
                build_session_key(source),
                source,
                reason="stop",
            ),
            timeout=0.1,
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert entered.is_set()
        assert elapsed < 0.1
    finally:
        release.set()
        manager._hooks["gateway_session_cancel"] = original


@pytest.mark.asyncio
async def test_gateway_session_cancel_timeout_bounds_blocking_sync_observer(monkeypatch):
    from gateway import run as run_module
    from hermes_cli.plugins import get_plugin_manager

    runner, _adapter = _runner_for_dispatch()
    manager = get_plugin_manager()
    original = list(manager._hooks.get("gateway_session_cancel", []))
    entered = threading.Event()
    release = threading.Event()

    def blocking_observer(**_kwargs):
        entered.set()
        release.wait(1)

    manager._hooks["gateway_session_cancel"] = [blocking_observer]
    monkeypatch.setattr(run_module, "GATEWAY_SESSION_CANCEL_TIMEOUT_SECONDS", 0.01)
    source = _source()
    session_key = build_session_key(source)
    started = asyncio.get_running_loop().time()
    try:
        await runner._notify_gateway_session_cancel(session_key, source, reason="stop")
        elapsed = asyncio.get_running_loop().time() - started
        assert entered.is_set()
        assert elapsed < 0.1
    finally:
        release.set()
        manager._hooks["gateway_session_cancel"] = original
