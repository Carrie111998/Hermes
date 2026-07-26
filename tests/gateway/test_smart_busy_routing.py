import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    Platform,
    SessionSource,
    build_session_key,
)
from hermes_cli.smart_orchestrator import (
    ROUTE_AMBIGUOUS,
    ROUTE_CONTROL,
    ROUTE_DEPENDENT,
    ROUTE_INDEPENDENT,
    ROUTE_RELATED,
    SmartRouteDecision,
)


def _event(text="follow-up", message_type=MessageType.TEXT):
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=SessionSource(
            platform=Platform.WHATSAPP,
            chat_id="chat-1",
            chat_type="dm",
            user_id="user-1",
        ),
        message_id=f"msg-{text[:8]}",
    )


def _runner_and_agent():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._busy_ack_ts = {}
    runner._smart_route_locks = {}
    runner._smart_active_missions = {}
    runner._busy_input_mode = "smart"
    runner._busy_text_mode = "interrupt"
    runner._draining = False
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.group_sessions_per_user = True
    runner.config.thread_sessions_per_user = False

    agent = MagicMock()
    agent.steer.return_value = True
    agent.get_activity_summary.return_value = {
        "api_call_count": 5,
        "max_iterations": 500,
        "current_tool": "terminal",
    }
    return runner, agent


async def _decision(route, payload="payload", confidence=0.95):
    return (
        SmartRouteDecision(
            route=route,
            confidence=confidence,
            reason=f"reason-{route}",
            source="classifier",
        ),
        payload,
    )


@pytest.mark.asyncio
async def test_smart_related_steers_and_never_interrupts():
    runner, agent = _runner_and_agent()
    event = _event("consider this correction")
    sk = build_session_key(event.source)
    runner._running_agents[sk] = agent
    runner._classify_smart_busy_message = AsyncMock(
        return_value=await _decision(ROUTE_RELATED, event.text)
    )
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    handled = await runner._handle_smart_busy_message(event, sk, agent, MagicMock())

    assert handled is True
    agent.steer.assert_called_once_with(event.text)
    agent.interrupt.assert_not_called()
    runner._queue_or_replace_pending_event.assert_not_called()
    runner._send_smart_busy_ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_smart_independent_steers_parallel_directive_without_false_parallel_ack():
    runner, agent = _runner_and_agent()
    event = _event("research another market")
    sk = build_session_key(event.source)
    runner._running_agents[sk] = agent
    runner._classify_smart_busy_message = AsyncMock(
        return_value=await _decision(ROUTE_INDEPENDENT, event.text)
    )
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    await runner._handle_smart_busy_message(event, sk, agent, MagicMock())

    injected = agent.steer.call_args.args[0]
    assert "SMART ORCHESTRATOR" in injected
    assert "research another market" in injected
    assert "delegate_task" in injected
    agent.interrupt.assert_not_called()
    runner._queue_or_replace_pending_event.assert_not_called()
    # A steering instruction is not a parallel-worker receipt. Until a real
    # worker accepts ownership, acknowledge only active-run delivery.
    assert runner._send_smart_busy_ack.await_args.args[3].route == ROUTE_RELATED


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [ROUTE_DEPENDENT, ROUTE_AMBIGUOUS, ROUTE_CONTROL])
async def test_smart_unsafe_or_non_parallel_routes_queue_losslessly(route):
    runner, agent = _runner_and_agent()
    event = _event(f"message-{route}")
    sk = build_session_key(event.source)
    runner._running_agents[sk] = agent
    runner._classify_smart_busy_message = AsyncMock(
        return_value=await _decision(route, event.text)
    )
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    await runner._handle_smart_busy_message(event, sk, agent, MagicMock())

    runner._queue_or_replace_pending_event.assert_called_once_with(sk, event)
    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_smart_steer_rejection_falls_back_to_queue():
    runner, agent = _runner_and_agent()
    agent.steer.return_value = False
    event = _event("related but no tool checkpoint")
    sk = build_session_key(event.source)
    runner._running_agents[sk] = agent
    runner._classify_smart_busy_message = AsyncMock(
        return_value=await _decision(ROUTE_RELATED, event.text)
    )
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    await runner._handle_smart_busy_message(event, sk, agent, MagicMock())

    runner._queue_or_replace_pending_event.assert_called_once_with(sk, event)
    agent.interrupt.assert_not_called()
    ack_decision = runner._send_smart_busy_ack.await_args.args[-1]
    assert ack_decision.route == ROUTE_AMBIGUOUS


@pytest.mark.asyncio
async def test_smart_media_never_goes_through_text_classifier_or_steer():
    runner, agent = _runner_and_agent()
    event = _event("photo caption", message_type=MessageType.PHOTO)
    sk = build_session_key(event.source)
    runner._running_agents[sk] = agent
    runner._classify_smart_busy_message = AsyncMock(
        side_effect=AssertionError("must not classify media")
    )
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    await runner._handle_smart_busy_message(event, sk, agent, MagicMock())

    runner._classify_smart_busy_message.assert_not_awaited()
    runner._queue_or_replace_pending_event.assert_called_once_with(sk, event)
    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_smart_pending_agent_sentinel_queues_without_calling_classifier():
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, _agent = _runner_and_agent()
    event = _event("same mission")
    sk = build_session_key(event.source)
    runner._running_agents[sk] = _AGENT_PENDING_SENTINEL
    runner._classify_smart_busy_message = AsyncMock(
        side_effect=AssertionError("must not classify before agent is ready")
    )
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    await runner._handle_smart_busy_message(
        event, sk, _AGENT_PENDING_SENTINEL, MagicMock()
    )

    runner._classify_smart_busy_message.assert_not_awaited()
    runner._queue_or_replace_pending_event.assert_called_once_with(sk, event)


@pytest.mark.asyncio
async def test_smart_rechecks_active_agent_after_classification_race():
    runner, agent = _runner_and_agent()
    event = _event("same mission")
    sk = build_session_key(event.source)
    runner._running_agents[sk] = agent

    async def classify(*_args, **_kwargs):
        runner._running_agents.pop(sk, None)
        return await _decision(ROUTE_RELATED, event.text)

    runner._classify_smart_busy_message = classify
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    await runner._handle_smart_busy_message(event, sk, agent, MagicMock())

    runner._queue_or_replace_pending_event.assert_called_once_with(sk, event)
    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_smart_rejects_classifier_result_from_reused_agent_next_generation():
    """Object identity is insufficient when the cached agent runs turn N+1."""
    runner, agent = _runner_and_agent()
    event = _event("update for turn N")
    sk = build_session_key(event.source)
    runner._running_agents[sk] = agent
    runner._session_run_generation = {sk: 10}
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    async def classify(*_args, **_kwargs):
        # Same cached AIAgent object, but a new top-level turn owns it now.
        runner._session_run_generation[sk] = 11
        return await _decision(ROUTE_RELATED, event.text)

    runner._classify_smart_busy_message = AsyncMock(side_effect=classify)

    await runner._handle_smart_busy_message(event, sk, agent, MagicMock())

    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()
    runner._queue_or_replace_pending_event.assert_called_once_with(sk, event)
    assert runner._send_smart_busy_ack.await_args.args[3].route == ROUTE_AMBIGUOUS


@pytest.mark.asyncio
async def test_smart_per_session_lock_serializes_classification_order():
    runner, agent = _runner_and_agent()
    first = _event("first")
    second = _event("second")
    sk = build_session_key(first.source)
    runner._running_agents[sk] = agent
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    entered = []
    release_first = asyncio.Event()

    async def classify(event, *_args, **_kwargs):
        entered.append(event.text)
        if event.text == "first":
            await release_first.wait()
        return await _decision(ROUTE_RELATED, event.text)

    runner._classify_smart_busy_message = classify

    t1 = asyncio.create_task(
        runner._handle_smart_busy_message(first, sk, agent, MagicMock())
    )
    await asyncio.sleep(0)
    t2 = asyncio.create_task(
        runner._handle_smart_busy_message(second, sk, agent, MagicMock())
    )
    await asyncio.sleep(0.02)
    assert entered == ["first"]

    release_first.set()
    await asyncio.gather(t1, t2)
    assert entered == ["first", "second"]
    assert [call.args[0] for call in agent.steer.call_args_list] == ["first", "second"]


@pytest.mark.asyncio
async def test_smart_route_lock_remains_stable_with_waiter_and_third_arrival():
    """A released handler cannot orphan a waiter behind a replacement lock."""
    runner, agent = _runner_and_agent()
    first = _event("first")
    second = _event("second")
    third = _event("third")
    sk = build_session_key(first.source)
    runner._running_agents[sk] = agent
    runner._send_smart_busy_ack = AsyncMock()
    runner._queue_or_replace_pending_event = MagicMock()

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    third_entered = asyncio.Event()
    release_waiters = asyncio.Event()

    async def classify(event, *_args, **_kwargs):
        if event.text == "first":
            # Force the current cleanup branch to consider the session idle.
            # The second call is already waiting on this call's lock.
            runner._running_agents.pop(sk, None)
            first_entered.set()
            await release_first.wait()
        elif event.text == "second":
            second_entered.set()
            await release_waiters.wait()
        else:
            third_entered.set()
            await release_waiters.wait()
        return await _decision(ROUTE_DEPENDENT, event.text)

    runner._classify_smart_busy_message = AsyncMock(side_effect=classify)
    adapter = MagicMock()

    first_task = asyncio.create_task(
        runner._handle_smart_busy_message(first, sk, agent, adapter)
    )
    await first_entered.wait()
    second_task = asyncio.create_task(
        runner._handle_smart_busy_message(second, sk, agent, adapter)
    )
    # Let the second task register as a waiter before releasing the holder.
    await asyncio.sleep(0)
    release_first.set()
    await first_task
    await second_entered.wait()

    # A new run may install the same cached object while the old waiter owns
    # the routing lock. The third arrival must queue behind that waiter, not
    # create a second lock and classify concurrently.
    runner._running_agents[sk] = agent
    third_task = asyncio.create_task(
        runner._handle_smart_busy_message(third, sk, agent, adapter)
    )
    scheduler_probe = asyncio.Event()
    asyncio.get_running_loop().call_soon(scheduler_probe.set)
    await scheduler_probe.wait()
    serialized = not third_entered.is_set()

    release_waiters.set()
    await asyncio.gather(second_task, third_task)

    assert serialized, "third arrival bypassed a waiter via a replacement lock"


@pytest.mark.asyncio
async def test_active_busy_handler_dispatches_smart_before_legacy_busy_text_guard():
    runner, agent = _runner_and_agent()
    runner._busy_text_mode = "queue"
    event = _event("route me")
    sk = build_session_key(event.source)
    adapter = MagicMock()
    runner.adapters[event.source.platform] = adapter
    runner._running_agents[sk] = agent
    runner._handle_smart_busy_message = AsyncMock(return_value=True)

    handled = await runner._handle_active_session_busy_message(event, sk)

    assert handled is True
    runner._handle_smart_busy_message.assert_awaited_once_with(
        event, sk, agent, adapter
    )
    agent.interrupt.assert_not_called()


def test_smart_mode_preserves_inbound_messages_during_graceful_restart():
    runner, _agent = _runner_and_agent()
    runner._restart_requested = True
    runner._busy_input_mode = "smart"

    assert runner._queue_during_drain_enabled() is True


@pytest.mark.asyncio
async def test_smart_ack_accepts_truthy_env_and_uses_managed_prefix(monkeypatch):
    from gateway import run as gateway_run

    runner, _agent = _runner_and_agent()
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    adapter = MagicMock()
    adapter._send_with_retry = AsyncMock()
    event = _event("parallel work")

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "1")
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {
            "display": {"busy_ack_enabled": False},
            "orchestration": {"smart": {"ack_prefix": "HEADER"}},
        },
    )

    await runner._send_smart_busy_ack(
        event,
        build_session_key(event.source),
        adapter,
        (await _decision(ROUTE_INDEPENDENT))[0],
    )

    adapter._send_with_retry.assert_awaited_once()
    sent_text = adapter._send_with_retry.await_args.kwargs["content"]
    assert sent_text.startswith("HEADER\n[M-")
    assert "paralelo" in sent_text.lower()


@pytest.mark.asyncio
async def test_smart_ack_can_be_disabled_by_managed_config(monkeypatch):
    from gateway import run as gateway_run

    runner, _agent = _runner_and_agent()
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    adapter = MagicMock()
    adapter._send_with_retry = AsyncMock()
    event = _event("queued work")

    monkeypatch.delenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", raising=False)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"display": {"busy_ack_enabled": False}},
    )

    await runner._send_smart_busy_ack(
        event,
        build_session_key(event.source),
        adapter,
        (await _decision(ROUTE_DEPENDENT))[0],
    )

    adapter._send_with_retry.assert_not_awaited()


def test_leftover_steer_is_prioritized_without_losing_existing_fifo_events():
    runner, _agent = _runner_and_agent()
    sk = "whatsapp:chat-1"
    selected = _event("selected queued event")
    staged = _event("already staged next")
    overflow = _event("overflow after staged")
    adapter = MagicMock()
    adapter._pending_messages = {sk: staged}
    runner._queued_events = {sk: [overflow]}

    event_after, text_after = runner._prioritize_leftover_steer(
        session_key=sk,
        adapter=adapter,
        pending_event=selected,
        pending_text=selected.text,
        result={"pending_steer": "late related update"},
    )

    assert event_after is None
    assert text_after == "late related update"
    assert adapter._pending_messages[sk] is selected
    assert runner._queued_events[sk] == [staged, overflow]
