"""Barrier-controlled regression tests for the delivery revocation boundary.

These tests prove that synchronous invalidation prevents new work, while
externally observable async lifecycle boundaries wait for any invocation that
already won to settle before returning. Already-started transport work is not
cancelled because cancellation cannot prove external non-occurrence.
"""

import asyncio

import pytest

from gateway.message_hooks import GatewayDelivery, GatewayDeliveryReceipt
from gateway.platforms.base import NativeDeliveryAck, SendResult


def _runner_for_inflight():
    """Minimal GatewayRunner double with a barrier-controlled adapter send."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner

    entered = asyncio.Event()
    release = asyncio.Event()

    async def send_native(chat_id, content, reply_to=None, metadata=None):
        entered.set()
        await release.wait()
        return SendResult(
            success=True,
            message_id="out-1",
            native_delivery_ack=NativeDeliveryAck(
                platform="discord",
                room_id=str(chat_id),
                self_actor_id="9",
                effect_kind="reply" if reply_to is not None else "send",
                submitted_content=content,
                reply_to_message_id=str(reply_to) if reply_to is not None else None,
                message_id="out-1",
                effect_id="out-1",
            ),
        )

    adapter = SimpleNamespace(
        send=send_native,
        authenticated_actor_id=MagicMock(return_value="9"),
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True)}
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._gateway_deliveries_by_session = {}
    return runner, entered, release


def _retained_delivery(runner, session_key="session-1"):
    from gateway.config import Platform
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.DISCORD,
        profile="work",
        scope_id=None,
        chat_id="room-1",
        chat_name=None,
        chat_type="group",
        thread_id=None,
        user_id="user-1",
        user_name=None,
    )
    adapter = runner.adapters[Platform.DISCORD]
    delivery = GatewayDelivery(
        lambda content: adapter.send("room-1", content),
        platform="discord",
        room_id="room-1",
        profile="work",
        self_actor_id="9",
        source_message_id="source-1",
    )
    runner._register_gateway_delivery(session_key, delivery)
    return delivery, source


def _ack(effect_kind, **overrides):
    fields = {
        "platform": "discord",
        "room_id": "room-1",
        "self_actor_id": "bot-9",
        "effect_kind": effect_kind,
        "submitted_content": None,
        "reply_to_message_id": None,
        "target_message_id": None,
        "reaction": None,
        "reaction_operation": None,
        "message_id": None,
        "effect_id": None,
    }
    fields.update(overrides)
    return NativeDeliveryAck(**fields)


def _make_delivery(send_callback, *, reply_callback=None, react_callback=None):
    return GatewayDelivery(
        send_callback,
        reply_callback=reply_callback,
        react_callback=react_callback,
        platform="discord",
        room_id="room-1",
        profile="work",
        self_actor_id="bot-9",
        source_message_id="source-1",
    )


async def _settle(delivery):
    """Let the delivery worker drain before the loop closes."""
    await asyncio.wait_for(delivery._settle_revocation(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send", "reply", "react"])
async def test_direct_revoke_marks_inflight_native_effect_unknown(kind):
    """Begin-only invalidation does not pretend an in-flight effect was aborted.

    ``_revoke()`` is deliberately synchronous and is not itself a completed
    lifecycle boundary. The adapter operation is allowed to settle and its
    receipt becomes unknown because revocation arrived after invocation.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def native_call(*_args):
        entered.set()
        await release.wait()
        if kind == "react":
            return SendResult(
                success=True,
                message_id="source-1",
                native_delivery_ack=_ack(
                    "react",
                    target_message_id="source-1",
                    reaction="+1",
                    reaction_operation="add",
                    effect_id="discord:reaction:inflight",
                ),
            )
        return SendResult(
            success=True,
            message_id="out-1",
            native_delivery_ack=_ack(
                kind,
                submitted_content="payload",
                reply_to_message_id="source-1" if kind == "reply" else None,
                message_id="out-1",
                effect_id="out-1",
            ),
        )

    delivery = _make_delivery(
        native_call,
        reply_callback=native_call,
        react_callback=native_call,
    )

    if kind == "send":
        submit = asyncio.create_task(delivery.send("payload"))
    elif kind == "reply":
        submit = asyncio.create_task(delivery.reply("payload"))
    else:
        submit = asyncio.create_task(delivery.react("+1", operation="add"))

    await asyncio.wait_for(entered.wait(), timeout=1)
    delivery._revoke()
    release.set()

    receipt = await asyncio.wait_for(submit, timeout=1)
    await _settle(delivery)

    assert receipt.status == "unknown"
    assert receipt.message_id is None


@pytest.mark.asyncio
async def test_revocation_wins_over_not_yet_started_invocation():
    """Revocation before native invocation prevents the adapter call."""
    native_calls = []

    async def native_call(content):
        native_calls.append(content)
        return SendResult(success=False)

    delivery = _make_delivery(native_call)
    submit = asyncio.create_task(delivery.send("payload"))
    # The request may already sit in the queue, but the worker has not
    # entered the native callback yet.
    delivery._revoke()

    receipt = await asyncio.wait_for(submit, timeout=1)
    await _settle(delivery)

    assert receipt.status == "failed"
    assert native_calls == []


@pytest.mark.asyncio
async def test_delivery_settles_exactly_once_without_hang_or_raise():
    """Concurrent revocation and invocation leave one settled outcome."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def native_call(_content):
        entered.set()
        await release.wait()
        return SendResult(
            success=True,
            message_id="out-1",
            native_delivery_ack=_ack(
                "send",
                submitted_content="payload",
                message_id="out-1",
                effect_id="out-1",
            ),
        )

    delivery = _make_delivery(native_call)
    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    delivery._revoke()
    # Repeated revocation is idempotent and cannot unsettle the request.
    delivery._revoke()
    release.set()

    receipt = await asyncio.wait_for(submit, timeout=1)
    await _settle(delivery)

    assert receipt.status in {"failed", "unknown"}
    # The capability remains consumed: no second effect can ever start.
    followup = await delivery.send("second")
    assert followup == GatewayDeliveryReceipt(status="failed")


@pytest.mark.asyncio
async def test_cancellation_of_submit_cannot_block_revocation_or_leak_effect():
    """A cancelled submitter still cannot leave an unbounded in-flight effect."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def native_call(_content):
        entered.set()
        await release.wait()
        return SendResult(
            success=True,
            message_id="out-1",
            native_delivery_ack=_ack(
                "send",
                submitted_content="payload",
                message_id="out-1",
                effect_id="out-1",
            ),
        )

    delivery = _make_delivery(native_call)
    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    delivery._revoke()
    submit.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await submit
    await _settle(delivery)

    # After the boundary, the capability is terminally closed.
    assert (await delivery.send("later")) == GatewayDeliveryReceipt(status="failed")


@pytest.mark.asyncio
async def test_session_cancel_fences_inflight_native_send(monkeypatch):
    """An in-flight send cannot complete after session cancellation returns."""
    runner, entered, release = _runner_for_inflight()
    delivery, source = _retained_delivery(runner)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", None, raising=False)

    async def no_observer(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook_async", no_observer, raising=False
    )

    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    boundary = asyncio.create_task(
        runner._notify_gateway_session_cancel(
            "session-1", source, reason="stop"
        )
    )
    await asyncio.sleep(0.05)
    assert not boundary.done()
    release.set()
    await asyncio.wait_for(boundary, timeout=1)

    receipt = await asyncio.wait_for(submit, timeout=1)
    await _settle(delivery)

    assert receipt.status == "unknown"


@pytest.mark.asyncio
async def test_shutdown_fences_inflight_native_send():
    """An in-flight send settles before shutdown's awaited boundary can return."""
    runner, entered, release = _runner_for_inflight()
    delivery, _source = _retained_delivery(runner)

    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    runner._revoke_all_gateway_deliveries()
    release.set()

    receipt = await asyncio.wait_for(submit, timeout=1)
    await _settle(delivery)

    assert receipt.status == "unknown"


@pytest.mark.asyncio
async def test_shutdown_sync_revoke_retains_inflight_worker_for_later_settlement():
    """The eager shutdown revoke must not discard its settlement handle."""
    runner, entered, release = _runner_for_inflight()
    delivery, _source = _retained_delivery(runner)

    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    runner._revoke_all_gateway_deliveries()
    boundary = asyncio.create_task(
        runner._revoke_all_gateway_deliveries_and_settle()
    )
    await asyncio.sleep(0.05)

    assert not boundary.done()
    release.set()
    await asyncio.wait_for(boundary, timeout=1)
    assert (await asyncio.wait_for(submit, timeout=1)).status == "unknown"
    assert runner._gateway_deliveries_by_session == {}


@pytest.mark.asyncio
async def test_consumed_deliveries_unregister_and_leave_no_worker_registry():
    import gateway.message_hooks as message_hooks

    runner, _entered, _release = _runner_for_inflight()
    created = []

    async def send_native(_chat_id, content):
        return SendResult(
            success=True,
            message_id=f"out-{content}",
            native_delivery_ack=_ack(
                "send",
                self_actor_id="9",
                submitted_content=content,
                message_id=f"out-{content}",
                effect_id=f"out-{content}",
            ),
        )

    runner.adapters[next(iter(runner.adapters))].send = send_native
    for index in range(300):
        delivery, _source = _retained_delivery(
            runner,
            session_key=f"session-{index}",
        )
        created.append(delivery)
        receipt = await delivery.send(str(index))
        assert receipt.status == "sent"

    await asyncio.sleep(0)
    assert runner._gateway_deliveries_by_session == {}
    assert getattr(runner, "_gateway_delivery_settlement_workers", {}) == {}
    assert all(
        delivery not in message_hooks._DELIVERY_CHANNELS
        for delivery in created
    )


@pytest.mark.asyncio
async def test_unused_delivery_has_bounded_nonpolling_lifetime(monkeypatch):
    """An unused one-shot capability expires without a polling task leak."""
    import gateway.message_hooks as message_hooks

    monkeypatch.setattr(
        message_hooks,
        "GATEWAY_DELIVERY_CAPABILITY_TTL_SECONDS",
        0.01,
        raising=False,
    )
    delivery = _make_delivery(
        lambda _content: SendResult(
            success=False,
            native_delivery_non_occurrence_attested=True,
        )
    )

    await asyncio.sleep(0.03)

    assert await delivery.send("too late") == GatewayDeliveryReceipt(status="failed")
    assert delivery not in message_hooks._DELIVERY_CHANNELS

@pytest.mark.asyncio
async def test_timeout_boundary_revokes_session_deliveries():
    """The inactivity-timeout path invalidates retained route capabilities."""
    runner, entered, release = _runner_for_inflight()
    delivery, _source = _retained_delivery(runner)

    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    # The timeout boundary in _handle_message_with_agent revokes before the
    # timed-out turn is considered returned.
    runner._revoke_gateway_deliveries("session-1")
    release.set()

    receipt = await asyncio.wait_for(submit, timeout=1)
    await _settle(delivery)

    assert receipt.status == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send", "reply", "react"])
async def test_boundary_awaits_native_invocation_that_won_before_revocation(kind):
    """A callback already in flight must finish before lifecycle return.

    The settlement wait must stay pending until the native effect actually
    completes; only then may the lifecycle boundary return.
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    effects = []
    boundary_holder = {}

    async def native_call(*_args):
        entered.set()
        await release.wait()
        boundary = boundary_holder.get("task")
        if boundary is not None and boundary.done():
            # The lifecycle boundary returned while this native effect was
            # still live: the revocation boundary is broken. Fail fast
            # instead of hanging against a regression that never awaits.
            pytest.fail(
                f"{kind} lifecycle boundary returned before the "
                "already-started native effect settled"
            )
        effects.append(f"{kind}-effect-completed")
        return SendResult(
            success=True,
            message_id="out-1",
            native_delivery_ack=_ack(
                kind,
                submitted_content="payload",
                reply_to_message_id="source-1" if kind == "reply" else None,
                target_message_id="source-1" if kind == "react" else None,
                reaction="+1" if kind == "react" else None,
                reaction_operation="add" if kind == "react" else None,
                message_id="out-1",
                effect_id="out-1",
            ),
        )

    runner, _entered, _release = _runner_for_inflight()
    delivery, source = _retained_delivery(runner)
    # Replace the runner-built capability with one bound to the suppressing
    # callback, still retained under the same session.
    runner._gateway_deliveries_by_session["session-1"].discard(delivery)
    delivery = _make_delivery(
        native_call,
        reply_callback=native_call,
        react_callback=native_call,
    )
    runner._register_gateway_delivery("session-1", delivery)

    if kind == "send":
        submit = asyncio.create_task(delivery.send("payload"))
    elif kind == "reply":
        submit = asyncio.create_task(delivery.reply("payload"))
    else:
        submit = asyncio.create_task(delivery.react("+1", operation="add"))

    await asyncio.wait_for(entered.wait(), timeout=1)

    boundary = asyncio.create_task(
        runner._revoke_gateway_deliveries_and_settle("session-1")
    )
    boundary_holder["task"] = boundary
    # Revocation is synchronous: it prevents anything new, but an invocation
    # that already won is not cancelled and must settle before return.
    await asyncio.sleep(0.05)
    assert not boundary.done()
    assert effects == []

    release.set()
    await asyncio.wait_for(boundary, timeout=1)
    assert effects == [f"{kind}-effect-completed"]

    receipt = await asyncio.wait_for(submit, timeout=1)
    # Exactly-once settlement: the requester sees the truthful pre-commit
    # state, never a fabricated success and never a second settlement.
    assert receipt.status == "unknown"
    assert receipt.message_id is None


@pytest.mark.asyncio
async def test_boundary_tracks_shielded_native_effect_after_invocation_wins():
    """Revocation must not detach a shielded transport effect from settlement."""
    entered = asyncio.Event()
    release = asyncio.Event()
    effects = []
    effect_tasks = []

    async def external_effect(content):
        entered.set()
        await release.wait()
        effects.append(content)

    async def native_call(content):
        effect = asyncio.create_task(external_effect(content))
        effect_tasks.append(effect)
        await asyncio.shield(effect)
        return SendResult(
            success=True,
            message_id="out-1",
            native_delivery_ack=_ack(
                "send",
                submitted_content=content,
                message_id="out-1",
                effect_id="out-1",
            ),
        )

    delivery = _make_delivery(native_call)
    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    boundary = asyncio.create_task(delivery._settle_revocation())
    await asyncio.sleep(0.05)

    assert not boundary.done()
    assert not submit.done()
    assert len(effect_tasks) == 1
    assert not effect_tasks[0].done()
    assert effects == []

    release.set()
    await asyncio.wait_for(boundary, timeout=1)
    receipt = await asyncio.wait_for(submit, timeout=1)

    assert effects == ["payload"]
    assert receipt.status == "unknown"
    assert receipt.message_id is None


@pytest.mark.asyncio
async def test_settlement_preserves_outer_cancel_when_worker_also_cancels():
    """A settled worker must not swallow a concurrent boundary cancellation."""
    from gateway.message_hooks import _await_delivery_settlement

    worker = asyncio.create_task(asyncio.sleep(3600))
    boundary = asyncio.create_task(_await_delivery_settlement(worker))
    await asyncio.sleep(0)

    worker.cancel()
    boundary.cancel()

    with pytest.raises(asyncio.CancelledError):
        await boundary
    assert worker.done()


@pytest.mark.asyncio
async def test_boundary_settlement_is_shielded_from_outer_cancellation():
    """Cancelling the boundary defers propagation until the effect settles."""
    entered = asyncio.Event()
    release = asyncio.Event()
    effects = []

    async def native_call(_content):
        entered.set()
        await release.wait()
        effects.append("send-effect-completed")
        return SendResult(success=True, message_id="out-1")

    runner, _entered, _release = _runner_for_inflight()
    delivery, _source = _retained_delivery(runner)
    runner._gateway_deliveries_by_session["session-1"].discard(delivery)
    delivery = _make_delivery(native_call)
    runner._register_gateway_delivery("session-1", delivery)

    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    boundary = asyncio.create_task(
        runner._revoke_gateway_deliveries_and_settle("session-1")
    )
    await asyncio.sleep(0.05)
    assert not boundary.done()

    # Cancellation of the lifecycle task must not let the boundary complete
    # while the native effect is still live.
    boundary.cancel()
    await asyncio.sleep(0.05)
    assert not boundary.done()
    assert effects == []
    assert not submit.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(boundary, timeout=1)
    assert effects == ["send-effect-completed"]

    receipt = await asyncio.wait_for(submit, timeout=1)
    assert receipt.status == "unknown"


@pytest.mark.asyncio
async def test_aggregate_settlement_defers_cancel_until_all_workers_finish():
    """Boundary cancellation settles every retained worker before propagating."""
    from gateway.run import GatewayRunner

    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    effects = []

    async def cancellation_suppressing_worker(index):
        entered[index].set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        await release[index].wait()
        effects.append(index)

    workers = [
        asyncio.create_task(cancellation_suppressing_worker(index))
        for index in range(2)
    ]
    await asyncio.gather(*(event.wait() for event in entered))
    for worker in workers:
        worker.cancel()

    runner = object.__new__(GatewayRunner)
    boundary = asyncio.create_task(
        runner._await_gateway_delivery_settlement(workers)
    )
    await asyncio.sleep(0)
    boundary.cancel()
    await asyncio.sleep(0)

    release[0].set()
    await asyncio.sleep(0.05)
    assert effects == [0]
    assert workers[0].done()
    assert not workers[1].done()
    assert not boundary.done()

    release[1].set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(boundary, timeout=1)
    assert effects == [0, 1]
    assert all(worker.done() for worker in workers)


@pytest.mark.asyncio
async def test_message_hook_fallthrough_settles_suppressing_native_effect(
    monkeypatch,
):
    """Hook fallthrough cannot return while its revoked native effect is live."""
    from types import SimpleNamespace

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    entered = asyncio.Event()
    release = asyncio.Event()
    effects = []

    async def send_native(_chat_id, _content, **_kwargs):
        entered.set()
        await release.wait()
        effects.append("send-effect-completed")
        return SendResult(success=False)

    adapter = SimpleNamespace(
        send=send_native,
        authenticated_actor_id=lambda: "9",
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True)}
    )
    setattr(runner, "adapters", {Platform.DISCORD: adapter})
    runner._gateway_deliveries_by_session = {}
    setattr(runner, "_adapter_for_source", lambda source: adapter)
    setattr(
        runner,
        "_thread_metadata_for_source",
        lambda source, reply_to_message_id=None: None,
    )

    source = SessionSource(
        platform=Platform.DISCORD,
        profile="work",
        scope_id=None,
        chat_id="room-1",
        chat_name=None,
        chat_type="group",
        thread_id=None,
        user_id="user-1",
        user_name=None,
    )
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=source,
        message_id="source-1",
    )
    submissions = []

    async def hook(*_args, **kwargs):
        submissions.append(asyncio.create_task(kwargs["delivery"].send("payload")))
        await entered.wait()
        return [{"decision": "pass"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook_async", hook)
    boundary = asyncio.create_task(
        runner._run_gateway_message_hook(event, source, "session-1")
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.sleep(0.05)

    assert not boundary.done()
    assert effects == []

    release.set()
    assert await asyncio.wait_for(boundary, timeout=1) is False
    assert effects == ["send-effect-completed"]
    receipt = await asyncio.wait_for(submissions[0], timeout=1)
    assert receipt.status == "unknown"


@pytest.mark.asyncio
async def test_boundary_await_skips_own_worker_to_avoid_self_deadlock():
    """A lifecycle path unwinding inside the callback frame must not hang."""
    entered = asyncio.Event()
    finished = asyncio.Event()

    runner, _entered, _release = _runner_for_inflight()
    holder = {}

    async def native_call(_content):
        entered.set()
        # Simulate host lifecycle code reached from inside the delivery
        # worker's own callback frame: awaiting its own settlement would be
        # a self-deadlock, so the boundary skips it.
        await runner._revoke_gateway_deliveries_and_settle("session-1")
        finished.set()
        return SendResult(success=True, message_id="out-1")

    delivery = _make_delivery(native_call)
    holder["delivery"] = delivery
    runner._register_gateway_delivery("session-1", delivery)

    submit = asyncio.create_task(delivery.send("payload"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.wait_for(finished.wait(), timeout=1)

    receipt = await asyncio.wait_for(submit, timeout=1)
    await _settle(delivery)
    # The effect completed before the outer revocation could fence it, but
    # revocation won while the invocation was in flight: no success receipt.
    assert receipt.status == "unknown"
