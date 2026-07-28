"""Lifecycle-scoped gateway delivery regressions for terminal completions.

The gateway contract here is deliberately narrower than exactly-once: one live
GatewayRunner suppresses concurrent/replayed copies after successful adapter
injection, failed injection remains retryable, and durable async-delegation
state (when available) is acknowledged through its authoritative SQLite API.
"""

import asyncio
import json
import queue
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run_module
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource, build_session_key
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Any current/future durable compatibility path must stay in tmp state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import async_delegation

    async_delegation._reset_for_tests()
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    try:
        yield registry
    finally:
        async_delegation._reset_for_tests()


def _runner(adapter, *, origins=None):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner.session_store = SimpleNamespace(
        _ensure_loaded=lambda: None,
        _entries=origins or {},
    )
    runner._session_source_cache = {}
    runner._completion_delivery_lock = __import__("threading").Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    runner._completion_retry_lock = threading.Lock()
    runner._completion_retry_timers = {}
    return runner


class _DeliveryLifecycleAdapter(BasePlatformAdapter):
    """Minimal adapter for asserting the real background-handler boundary."""

    def __init__(self):
        super().__init__(
            PlatformConfig(
                enabled=True,
                token="test",
                typing_indicator=False,
            ),
            Platform.TELEGRAM,
        )

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="delivery-lifecycle")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _async_event(delegation_id="deleg_duplicate"):
    return {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "status": "completed",
        "summary": "Found it",
        "api_calls": 1,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
        # PR #62479 stamps these on gateway-owned events. They must not
        # change the producer identity used for queue replay.
        "origin_profile": "default",
        "origin_hermes_home": "/tmp/hermes-default",
    }


def _runtime_effect():
    return {
        "schema": "hermes.runtime-effect.v1",
        "kind": "isolated_workspace_may_have_changed.v1",
        "workspace_lease_authority": "conversation-root-gateway-test",
        "baseline_edit_generation": 23,
    }


def _completion_event(*, started_at, session_id="proc_reused"):
    return {
        "type": "completion",
        "session_id": session_id,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "started_at": started_at,
        "command": "echo done",
        "exit_code": 0,
        "completion_reason": "exited",
        "output": "done\n",
    }


def _stop_after_sleeps(monkeypatch, runner, count):
    sleep_calls = 0

    async def _bounded_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= count:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _bounded_sleep)


class _FakeTimer:
    instances = []
    fail_start = False

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.started = False
        self.daemon = False
        self.__class__.instances.append(self)

    def start(self):
        if self.__class__.fail_start:
            raise RuntimeError("timer start failed")
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


def test_authored_gateway_event_cannot_forge_runtime_effect():
    """Readable text/metadata from a real user never crosses the host seam."""
    effect = _runtime_effect()
    authored = SimpleNamespace(
        internal=False,
        text=json.dumps({"runtime_effect": effect}),
        metadata={"runtime_effect": effect},
    )

    assert GatewayRunner._runtime_effect_for_internal_event(authored) is None

    internal = SimpleNamespace(
        internal=True,
        text="host completion",
        metadata={"runtime_effect": effect},
    )
    normalized = GatewayRunner._runtime_effect_for_internal_event(internal)
    assert normalized == effect
    assert normalized is not effect


def test_malformed_internal_gateway_runtime_effect_fails_closed():
    effect = _runtime_effect()
    effect["forged_extra_field"] = True
    internal = SimpleNamespace(
        internal=True,
        metadata={"runtime_effect": effect},
    )

    with pytest.raises(ValueError, match="runtime_effect_fields_invalid"):
        GatewayRunner._runtime_effect_for_internal_event(internal)


@pytest.mark.asyncio
async def test_in_band_internal_delivery_acks_after_outer_handler_returns():
    """A queued wake cannot ACK while its enclosing handler is still live."""
    adapter = _DeliveryLifecycleAdapter()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="delivery-lifecycle-chat",
        chat_type="dm",
        user_id="delivery-lifecycle-user",
    )
    lifecycle_event = MessageEvent(
        text="outer turn",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"_hermes_internal_turn_persisted": True},
    )
    delivery_future = asyncio.get_running_loop().create_future()
    queued_event = MessageEvent(
        text="internal completion",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        metadata={
            "_hermes_internal_delivery_future": delivery_future,
        },
    )
    deferred = asyncio.Event()
    release_handler = asyncio.Event()

    async def _handler(event):
        GatewayRunner._defer_internal_delivery_success(
            queued_event,
            event,
        )
        deferred.set()
        await release_handler.wait()
        GatewayRunner._mark_internal_turn_persisted(event)
        return None

    adapter.set_message_handler(_handler)
    task = asyncio.create_task(
        adapter._process_message_background(
            lifecycle_event,
            build_session_key(source),
        )
    )

    await deferred.wait()
    assert not delivery_future.done()

    release_handler.set()
    await task

    assert delivery_future.done()
    assert delivery_future.result() is True


@pytest.mark.asyncio
async def test_in_band_internal_delivery_rejects_outer_persistence_failure():
    """A failed outer handler cannot turn FIFO adapter acceptance into an ACK."""

    adapter = _DeliveryLifecycleAdapter()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="delivery-failure-chat",
        chat_type="dm",
        user_id="delivery-failure-user",
    )
    lifecycle_event = MessageEvent(
        text="outer turn",
        message_type=MessageType.TEXT,
        source=source,
        # Simulate stale/authored metadata: BasePlatform must clear this before
        # the handler and accept only the post-persistence host marker.
        metadata={"_hermes_internal_turn_persisted": True},
    )
    delivery_future = asyncio.get_running_loop().create_future()
    queued_event = MessageEvent(
        text="internal completion",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        metadata={
            "_hermes_internal_delivery_future": delivery_future,
        },
    )

    async def _handler(event):
        GatewayRunner._defer_internal_delivery_success(
            queued_event,
            event,
        )
        raise OSError("simulated transcript persistence failure")

    adapter.set_message_handler(_handler)
    await adapter._process_message_background(
        lifecycle_event,
        build_session_key(source),
    )

    assert delivery_future.done()
    assert delivery_future.result() is False


def test_duplicate_async_queue_replay_injects_once(monkeypatch, isolated_registry):
    """Byte-identical queue replays produce one turn in one gateway lifecycle."""
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(dict(_async_event()))
    isolated.put(dict(_async_event()))

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()


def test_unroutable_async_event_is_not_requeued_forever(
    monkeypatch, isolated_registry,
):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    event = _async_event("deleg_desktop_or_cli")
    event["session_key"] = "20260711_unparseable_ui_session"
    isolated.put(event)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_not_awaited()
    assert isolated.empty()


def test_concurrent_claims_share_the_same_narrow_delivery_seam():
    """Concurrent consumers in one runner cannot both enter the adapter."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_injection(_event):
        entered.set()
        await release.wait()

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_blocked_injection))
    runner = _runner(adapter)
    event = _async_event()
    text = "completion"

    async def _exercise():
        first = asyncio.create_task(runner._deliver_completion_notification(text, dict(event)))
        await entered.wait()
        second = asyncio.create_task(runner._deliver_completion_notification(text, dict(event)))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    assert sorted(asyncio.run(_exercise()), key=str) == [None, True]
    adapter.handle_message.assert_awaited_once()


def test_failed_async_injection_is_retried_and_only_success_is_acked(
    monkeypatch, isolated_registry,
):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(_async_event())

    adapter = SimpleNamespace(
        handle_message=AsyncMock(side_effect=[RuntimeError("temporary"), None])
    )
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=3)

    from tools import async_delegation

    acknowledgements = []
    monkeypatch.setattr(
        async_delegation,
        "complete_event_delivery",
        lambda event, _claim_id, **_kwargs: acknowledgements.append(
            event["delegation_id"]
        )
        or True,
        raising=False,
    )

    asyncio.run(runner._async_delegation_watcher(interval=0))

    assert adapter.handle_message.await_count == 2
    assert acknowledgements == ["deleg_duplicate"]


def _persist_pending_completion(event):
    from tools import async_delegation

    async_delegation._persist_dispatch({
        "delegation_id": event["delegation_id"],
        "session_key": event["session_key"],
        "origin_ui_session_id": "",
        "parent_session_id": event.get("parent_session_id"),
        "dispatched_at": event["dispatched_at"],
    })
    async_delegation._persist_completion(event, {
        "status": "completed",
        "summary": event["summary"],
    })


def _persist_pending_completion_for_profile(
    event,
    *,
    profile_home,
    profile,
):
    from tools import async_delegation

    with _profile_runtime_scope(profile_home):
        _persist_pending_completion(event)
    restored = queue.Queue()
    assert (
        async_delegation.restore_undelivered_completions(
            restored,
            hermes_home=profile_home,
            profile=profile,
        )
        == 1
    )
    return restored.get_nowait()


def _restore_pending_completion_for_runner(
    event,
    runner,
    *,
    profile_home,
):
    """Return a store-stamped event and authorize that exact store on runner."""

    from tools import async_delegation

    _persist_pending_completion(event)
    restored = queue.Queue()
    assert (
        async_delegation.restore_undelivered_completions(
            restored,
            hermes_home=profile_home,
        )
        == 1
    )
    stamped = restored.get_nowait()
    store = async_delegation.get_event_delivery_store(stamped)
    assert store is not None
    runner._async_delivery_profile_homes = {
        store.hermes_home: ("default", Path(store.source_home)),
    }
    runner._async_delivery_profile_stores = {
        store.hermes_home: store,
    }
    return stamped, store


def _push_wake_fingerprint(runner, event, store, text):
    from tools.async_delegation import durable_wake_execution_fingerprint

    source = runner._build_process_event_source(event)
    assert source is not None
    platform = (
        source.platform.value
        if hasattr(source.platform, "value")
        else str(source.platform)
    )
    return durable_wake_execution_fingerprint(
        delegation_id=event["delegation_id"],
        destination={
            "platform": platform,
            "chat_id": str(source.chat_id or ""),
            "chat_type": str(source.chat_type or ""),
            "thread_id": str(source.thread_id or ""),
            "profile": str(source.profile or "default"),
            "session_key": str(event.get("session_key") or ""),
            "parent_session_id": str(
                event.get("parent_session_id") or ""
            ),
        },
        text=text,
        runtime_effect=event.get("runtime_effect"),
        execution_context=event.get("api_execution_context"),
        store=store,
    )


def test_push_wake_receipt_closes_crash_gap_before_outer_ack(
    tmp_path,
    monkeypatch,
):
    """A persisted inner turn is never executed again after outer ACK loss."""

    from tools import async_delegation

    event = _async_event("deleg_push_receipt_ack_gap")
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    event, _store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    inject = AsyncMock(return_value=True)
    runner._inject_watch_notification = inject
    real_complete = async_delegation.complete_event_delivery
    ack_calls = 0

    def _crash_gap_ack(evt, claim_id):
        nonlocal ack_calls
        ack_calls += 1
        if ack_calls == 1:
            raise OSError("simulated crash after wake receipt")
        return real_complete(evt, claim_id)

    monkeypatch.setattr(
        async_delegation,
        "complete_event_delivery",
        _crash_gap_ack,
    )

    async def _exercise():
        first = await runner._deliver_completion_notification(
            "completion",
            event,
        )
        after_first = async_delegation.get_durable_delegation(
            event["delegation_id"],
        )
        second = await runner._deliver_completion_notification(
            "completion",
            event,
        )
        return first, after_first, second

    first, after_first, second = asyncio.run(_exercise())

    assert first is False
    assert after_first is not None
    assert after_first["wake_state"] == "completed"
    assert after_first["delivery_state"] == "pending"
    assert after_first["delivery_attempts"] == 0
    assert second is True
    inject.assert_awaited_once()
    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["delivery_state"] == "delivered"


def test_push_live_wake_owner_preserves_outer_carrier_without_injection(
    tmp_path,
):
    """A live semantic owner defers the carrier without burning its budget."""

    from tools import async_delegation

    event = _async_event("deleg_push_live_owner")
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    event, store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    inject = AsyncMock(return_value=True)
    runner._inject_watch_notification = inject
    fingerprint = _push_wake_fingerprint(
        runner,
        event,
        store,
        "completion",
    )
    owner = async_delegation.claim_durable_wake_execution(
        delegation_id=event["delegation_id"],
        idempotency_key=fingerprint,
        store=store,
    )
    assert owner.state == "claimed"

    assert (
        asyncio.run(
            runner._deliver_completion_notification(
                "completion",
                event,
            )
        )
        is False
    )

    inject.assert_not_awaited()
    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["wake_state"] == "running"
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0
    assert async_delegation.release_durable_wake_execution(
        delegation_id=event["delegation_id"],
        idempotency_key=fingerprint,
        claim_id=owner.claim_id,
        store=store,
    )


def test_push_stale_wake_owner_is_quarantined_without_injection(
    tmp_path,
):
    """Owner loss becomes explicit uncertainty, never a second agent turn."""

    from tools import async_delegation

    event = _async_event("deleg_push_stale_owner")
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    event, store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    inject = AsyncMock(return_value=True)
    runner._inject_watch_notification = inject
    fingerprint = _push_wake_fingerprint(
        runner,
        event,
        store,
        "completion",
    )
    owner = async_delegation.claim_durable_wake_execution(
        delegation_id=event["delegation_id"],
        idempotency_key=fingerprint,
        store=store,
    )
    assert owner.state == "claimed"
    with async_delegation._ACTIVE_WAKE_CLAIMS_LOCK:
        async_delegation._ACTIVE_WAKE_CLAIMS.discard(owner.claim_id)

    assert (
        asyncio.run(
            runner._deliver_completion_notification(
                "completion",
                event,
            )
        )
        is None
    )

    inject.assert_not_awaited()
    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["wake_state"] == "uncertain"
    assert durable["wake_disposition_reason"]
    assert durable["delivery_state"] == "dropped"


def test_push_completed_wake_requires_exact_persistence_receipt(
    tmp_path,
):
    """A malformed completed response cannot masquerade as persisted proof."""

    from tools import async_delegation

    event = _async_event("deleg_push_malformed_receipt")
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    event, store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    inject = AsyncMock(return_value=True)
    runner._inject_watch_notification = inject
    fingerprint = _push_wake_fingerprint(
        runner,
        event,
        store,
        "completion",
    )
    owner = async_delegation.claim_durable_wake_execution(
        delegation_id=event["delegation_id"],
        idempotency_key=fingerprint,
        store=store,
    )
    assert owner.state == "claimed"
    assert async_delegation.complete_durable_wake_execution(
        delegation_id=event["delegation_id"],
        idempotency_key=fingerprint,
        claim_id=owner.claim_id,
        response={
            "schema": "hermes.push-wake-persistence-receipt.v1",
            "delegation_id": event["delegation_id"],
            "status": "persisted",
            "unexpected": True,
        },
        store=store,
    )

    assert (
        asyncio.run(
            runner._deliver_completion_notification(
                "completion",
                event,
            )
        )
        is None
    )
    inject.assert_not_awaited()
    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["wake_state"] == "completed"
    assert durable["delivery_state"] == "dropped"


def test_nonpush_deferred_wakes_do_not_exhaust_outer_delivery_budget(
    tmp_path,
):
    """Every typed API deferral releases the carrier without attempt burn."""

    from gateway.wake import DurableWakeDeferredError
    from tools import async_delegation

    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        supports_async_delivery=False,
    )
    event = _async_event("deleg_nonpush_deferred_budget")
    runner = _runner(adapter)
    event, _store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    inject = AsyncMock(
        side_effect=DurableWakeDeferredError("claim_unavailable")
    )
    runner._inject_watch_notification = inject

    async def _exercise():
        for _ in range(async_delegation._MAX_DELIVERY_ATTEMPTS + 3):
            assert (
                await runner._deliver_completion_notification(
                    "completion",
                    event,
                )
                is False
            )

    asyncio.run(_exercise())

    assert inject.await_count == async_delegation._MAX_DELIVERY_ATTEMPTS + 3
    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0


def test_nonpush_transport_outage_does_not_exhaust_outer_delivery_budget(
    tmp_path,
):
    """The inner HTTP CAS makes transient transport replay budget-neutral."""

    from tools import async_delegation

    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        supports_async_delivery=False,
    )
    event = _async_event("deleg_nonpush_transport_budget")
    runner = _runner(adapter)
    event, _store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    inject = AsyncMock(return_value=False)
    runner._inject_watch_notification = inject

    async def _exercise():
        for _ in range(async_delegation._MAX_DELIVERY_ATTEMPTS + 3):
            assert (
                await runner._deliver_completion_notification(
                    "completion",
                    event,
                )
                is False
            )

    asyncio.run(_exercise())

    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0


def test_push_authority_outage_before_execution_preserves_budget(
    tmp_path,
    monkeypatch,
):
    """Fingerprint/store/CAS failures before effects never consume attempts."""

    from tools import async_delegation

    event = _async_event("deleg_push_authority_outage")
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    event, _store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    inject = AsyncMock(return_value=True)
    runner._inject_watch_notification = inject

    def _claim_outage(**_kwargs):
        raise sqlite3.OperationalError("temporary state.db outage")

    monkeypatch.setattr(
        async_delegation,
        "claim_durable_wake_execution",
        _claim_outage,
    )

    async def _exercise():
        for _ in range(async_delegation._MAX_DELIVERY_ATTEMPTS + 3):
            assert (
                await runner._deliver_completion_notification(
                    "completion",
                    event,
                )
                is False
            )

    asyncio.run(_exercise())

    inject.assert_not_awaited()
    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["wake_state"] == "not_started"
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0


@pytest.mark.parametrize("mode", ["false", "exception"])
def test_push_abandon_failure_preserves_outer_carrier(
    tmp_path,
    monkeypatch,
    mode,
):
    """No terminal outer drop is allowed without a committed uncertainty CAS."""

    from tools import async_delegation

    event = _async_event(f"deleg_push_abandon_{mode}")
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    event, _store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    runner._inject_watch_notification = AsyncMock(return_value=False)

    def _abandon(**_kwargs):
        if mode == "exception":
            raise sqlite3.OperationalError("abandon persistence unavailable")
        return False

    monkeypatch.setattr(
        async_delegation,
        "abandon_durable_wake_execution",
        _abandon,
    )

    assert (
        asyncio.run(
            runner._deliver_completion_notification(
                "completion",
                event,
            )
        )
        is None
    )

    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["wake_state"] == "running"
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0


def test_duplicate_inflight_carriers_do_not_exhaust_delivery_budget(
    tmp_path,
):
    """A local duplicate is coordination, not a failed delivery attempt."""

    from tools import async_delegation

    event = _async_event("deleg_duplicate_inflight_budget")
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    event, _store = _restore_pending_completion_for_runner(
        event,
        runner,
        profile_home=tmp_path,
    )
    identity = runner._completion_delivery_identity(event)
    assert identity is not None
    runner._completion_deliveries_inflight.add(identity)

    async def _exercise():
        for _ in range(async_delegation._MAX_DELIVERY_ATTEMPTS + 3):
            assert (
                await runner._deliver_completion_notification(
                    "completion",
                    event,
                )
                is None
            )

    asyncio.run(_exercise())

    durable = async_delegation.get_durable_delegation(
        event["delegation_id"],
    )
    assert durable is not None
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0


def _create_session(profile_home, session_id, *, end_reason=None):
    from hermes_state import SessionDB

    db = SessionDB(profile_home / "state.db")
    db.create_session(session_id, "gateway")
    if end_reason:
        db.end_session(session_id, end_reason)
    db.close()


def test_gateway_watcher_retains_carrier_for_fresh_foreign_claim(
    monkeypatch,
    isolated_registry,
):
    """A rolling gateway handoff cannot consume the only pending queue item."""

    from tools import async_delegation

    event = _async_event("deleg_gateway_foreign_claim")
    _persist_pending_completion(event)
    claim = async_delegation.claim_event_delivery(event, "old-gateway")
    assert claim
    assert (
        async_delegation.restore_undelivered_completions(
            isolated_registry.completion_queue,
        )
        == 1
    )
    event = isolated_registry.completion_queue.queue[0]
    _FakeTimer.instances = []
    _FakeTimer.fail_start = False
    monkeypatch.setattr(gateway_run_module.threading, "Timer", _FakeTimer)
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    assert isolated_registry.completion_queue.empty()
    assert len(_FakeTimer.instances) == 1
    timer = _FakeTimer.instances[0]
    assert timer.started
    assert timer.delay > 299.0
    assert (
        async_delegation.get_durable_delegation(event["delegation_id"])[
            "delivery_state"
        ]
        == "pending"
    )

    # Simulate the old process disappearing before ACK. Once its lease is
    # stale, the delayed callback becomes the one replacement queue carrier.
    with (
        async_delegation._DB_LOCK,
        async_delegation._transaction() as conn,
    ):
        conn.execute(
            """UPDATE async_delegations SET delivery_claimed_at=0
               WHERE delegation_id=?""",
            (event["delegation_id"],),
        )
    runner._running = True
    timer.fire()

    assert isolated_registry.completion_queue.get_nowait() is event
    assert runner._completion_retry_timers == {}


def test_gateway_retry_carrier_terminal_recheck_is_noop(
    monkeypatch,
    isolated_registry,
):
    from tools import async_delegation

    event = _async_event("deleg_gateway_terminal_retry")
    _persist_pending_completion(event)
    _FakeTimer.instances = []
    _FakeTimer.fail_start = False
    monkeypatch.setattr(gateway_run_module.threading, "Timer", _FakeTimer)
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))

    assert runner._schedule_completion_delivery_retry(event) is True
    timer = _FakeTimer.instances[0]
    claim = async_delegation.claim_event_delivery(event, "terminal-owner")
    assert claim
    assert async_delegation.complete_event_delivery(event, claim)
    timer.fire()

    assert isolated_registry.completion_queue.empty()
    assert runner._completion_retry_timers == {}


def test_gateway_retry_identity_separates_same_id_across_profile_stores(
    tmp_path,
    monkeypatch,
    isolated_registry,
):
    from tools import async_delegation

    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    delegation_id = "deleg_gateway_same_profile_id"

    default_event = _async_event(delegation_id)
    _persist_pending_completion(default_event)
    default_restored = queue.Queue()
    assert (
        async_delegation.restore_undelivered_completions(
            default_restored,
            hermes_home=tmp_path,
        )
        == 1
    )
    default_event = default_restored.get_nowait()

    alpha_event = _async_event(delegation_id)
    alpha_event["session_key"] = "agent:alpha:telegram:dm:alpha-chat"
    alpha_event = _persist_pending_completion_for_profile(
        alpha_event,
        profile_home=alpha_home,
        profile="alpha",
    )
    _FakeTimer.instances = []
    _FakeTimer.fail_start = False
    monkeypatch.setattr(gateway_run_module.threading, "Timer", _FakeTimer)
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))

    assert runner._schedule_completion_delivery_retry(default_event) is True
    assert runner._schedule_completion_delivery_retry(alpha_event) is True

    assert len(runner._completion_retry_timers) == 2
    assert len(_FakeTimer.instances) == 2
    assert {
        identity[0] for identity in runner._completion_retry_timers
    } == {
        str(tmp_path.resolve()),
        str(alpha_home.resolve()),
    }


def test_gateway_lifecycle_delivers_same_id_from_two_profile_stores(
    tmp_path,
):
    """A multiplex runner's delivered cache is scoped by durable store."""

    from tools import async_delegation

    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    delegation_id = "deleg_gateway_same_id_sequential"

    default_event = _async_event(delegation_id)
    _persist_pending_completion(default_event)
    default_restored = queue.Queue()
    assert (
        async_delegation.restore_undelivered_completions(
            default_restored,
            hermes_home=tmp_path,
        )
        == 1
    )
    default_event = default_restored.get_nowait()

    alpha_event = _async_event(delegation_id)
    alpha_event["session_key"] = "agent:alpha:telegram:dm:alpha-chat"
    alpha_event = _persist_pending_completion_for_profile(
        alpha_event,
        profile_home=alpha_home,
        profile="alpha",
    )

    default_adapter = SimpleNamespace(handle_message=AsyncMock())
    alpha_adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(default_adapter)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._profile_adapters = {
        "alpha": {Platform.TELEGRAM: alpha_adapter},
    }

    async def _exercise():
        first = await runner._deliver_completion_notification(
            "default completion",
            default_event,
        )
        second = await runner._deliver_completion_notification(
            "alpha completion",
            alpha_event,
        )
        return first, second

    assert asyncio.run(_exercise()) == (True, True)
    default_adapter.handle_message.assert_awaited_once()
    alpha_adapter.handle_message.assert_awaited_once()
    assert (
        async_delegation.get_durable_delegation(delegation_id)[
            "delivery_state"
        ]
        == "delivered"
    )
    with _profile_runtime_scope(alpha_home):
        assert (
            async_delegation.get_durable_delegation(delegation_id)[
                "delivery_state"
            ]
            == "delivered"
        )


def test_gateway_lifecycle_concurrently_delivers_same_id_from_two_profiles(
    tmp_path,
):
    """One profile's in-flight identity cannot suppress another profile."""

    from tools import async_delegation

    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    delegation_id = "deleg_gateway_same_id_concurrent"

    default_event = _async_event(delegation_id)
    _persist_pending_completion(default_event)
    default_restored = queue.Queue()
    assert (
        async_delegation.restore_undelivered_completions(
            default_restored,
            hermes_home=tmp_path,
        )
        == 1
    )
    default_event = default_restored.get_nowait()

    alpha_event = _async_event(delegation_id)
    alpha_event["session_key"] = "agent:alpha:telegram:dm:alpha-chat"
    alpha_event = _persist_pending_completion_for_profile(
        alpha_event,
        profile_home=alpha_home,
        profile="alpha",
    )

    default_entered = asyncio.Event()
    release_default = asyncio.Event()

    async def _blocked_default(_event):
        default_entered.set()
        await release_default.wait()

    default_adapter = SimpleNamespace(
        handle_message=AsyncMock(side_effect=_blocked_default)
    )
    alpha_adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(default_adapter)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._profile_adapters = {
        "alpha": {Platform.TELEGRAM: alpha_adapter},
    }

    async def _exercise():
        default_task = asyncio.create_task(
            runner._deliver_completion_notification(
                "default completion",
                default_event,
            )
        )
        await default_entered.wait()
        alpha_result = await runner._deliver_completion_notification(
            "alpha completion",
            alpha_event,
        )
        release_default.set()
        return await default_task, alpha_result

    assert asyncio.run(_exercise()) == (True, True)
    default_adapter.handle_message.assert_awaited_once()
    alpha_adapter.handle_message.assert_awaited_once()
    assert (
        async_delegation.get_durable_delegation(delegation_id)[
            "delivery_state"
        ]
        == "delivered"
    )
    with _profile_runtime_scope(alpha_home):
        assert (
            async_delegation.get_durable_delegation(delegation_id)[
                "delivery_state"
            ]
            == "delivered"
        )


def test_gateway_retry_timer_replacement_failure_preserves_old_carrier(
    monkeypatch,
    isolated_registry,
):
    from tools import async_delegation

    event = _async_event("deleg_gateway_timer_replacement")
    _persist_pending_completion(event)
    claim = async_delegation.claim_event_delivery(event, "old-gateway")
    assert claim
    _FakeTimer.instances = []
    _FakeTimer.fail_start = False
    monkeypatch.setattr(gateway_run_module.threading, "Timer", _FakeTimer)
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))

    assert runner._schedule_completion_delivery_retry(event) is True
    original = _FakeTimer.instances[0]
    assert async_delegation.release_event_delivery(event, claim)
    _FakeTimer.fail_start = True

    assert runner._schedule_completion_delivery_retry(event) is False
    assert original.cancelled is False
    assert (
        runner._completion_retry_timers[
            runner._completion_retry_identity(event)
        ]
        is original
    )

    _FakeTimer.fail_start = False
    original.fire()
    assert isolated_registry.completion_queue.get_nowait() is event
    assert runner._completion_retry_timers == {}


def test_renewal_loss_prevents_stale_gateway_owner_ack_and_allows_takeover(
    monkeypatch, isolated_registry,
):
    """A consumer that loses its lease after persistence cannot settle the row."""

    from tools import async_delegation

    event = _async_event("deleg_renewal_takeover")
    _persist_pending_completion(event)
    runner = _runner(SimpleNamespace())
    captured = {}
    original_begin = async_delegation.begin_event_delivery_renewal

    def _begin_fast(evt, claim_id):
        handle = original_begin(
            evt,
            claim_id,
            interval_seconds=0.01,
        )
        captured["handle"] = handle
        return handle

    monkeypatch.setattr(
        async_delegation,
        "begin_event_delivery_renewal",
        _begin_fast,
    )

    async def _persist_then_lose_claim(_text, _event):
        # Make gateway-A's claim stale, then let gateway-B take it before A
        # crosses its final durable ACK CAS.
        with async_delegation._DB_LOCK, async_delegation._transaction() as conn:
            conn.execute(
                """UPDATE async_delegations SET delivery_claimed_at=0
                   WHERE delegation_id=?""",
                (event["delegation_id"],),
            )
        assert async_delegation.claim_completion_delivery(
            event["delegation_id"],
            "gateway-b",
        )
        deadline = asyncio.get_running_loop().time() + 2.0
        while not captured["handle"].ownership_lost:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        return True

    runner._inject_watch_notification = _persist_then_lose_claim

    assert asyncio.run(
        runner._deliver_completion_notification("completion", event)
    ) is False
    durable = async_delegation.get_durable_delegation(event["delegation_id"])
    assert durable is not None
    assert durable["delivery_state"] == "pending"

    # The takeover owner remains authoritative and can settle after its own
    # successful injection; stale gateway-A neither ACKed nor released B.
    assert async_delegation.complete_completion_delivery(
        event["delegation_id"],
        "gateway-b",
    )
    assert (
        async_delegation.get_durable_delegation(event["delegation_id"])[
            "delivery_state"
        ]
        == "delivered"
    )


def test_compression_parent_delivery_targets_tip_and_is_acked(
    monkeypatch, isolated_registry,
):
    """A compression-rotated parent with a live tip is deliverable + acked."""
    from tools import async_delegation

    event = _async_event("deleg_compression")
    event["parent_session_id"] = "sess_parent"
    _persist_pending_completion(event)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(side_effect=lambda session_id: {
            "sess_parent": {
                "id": "sess_parent",
                "ended_at": "2026-07-16T12:00:00",
                "end_reason": "compression",
            },
            "sess_tip": {"id": "sess_tip", "ended_at": None, "end_reason": None},
        }.get(session_id)),
        get_compression_tip=AsyncMock(return_value="sess_tip"),
    )

    assert asyncio.run(
        runner._deliver_completion_notification("completion", event)
    ) is True

    adapter.handle_message.assert_awaited_once()
    durable = async_delegation.get_durable_delegation(event["delegation_id"])
    assert durable is not None
    assert durable["delivery_state"] == "delivered"


def test_named_profile_delivery_uses_its_db_adapter_and_ack_store(
    tmp_path,
    monkeypatch,
):
    """Alpha push delivery separates shared session DB from result storage."""
    from hermes_state import AsyncSessionDB, SessionDB
    from tools import async_delegation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    parent_id = "shared-parent"
    delegation_id = "shared-delegation-id"

    # Multiplexed push sessions live in the runner's shared gateway DB even
    # when their async result rows live in a named profile's state.db. This is
    # the real GatewayRunner wiring: source.profile namespaces the routing key,
    # while the one runner-owned SessionDB persists every push transcript.
    _create_session(tmp_path, parent_id)
    default_event = _async_event(delegation_id)
    default_event["parent_session_id"] = parent_id
    _persist_pending_completion(default_event)

    alpha_event = _async_event(delegation_id)
    alpha_event["session_key"] = "agent:alpha:telegram:dm:12345:678"
    alpha_event["parent_session_id"] = parent_id
    alpha_event = _persist_pending_completion_for_profile(
        alpha_event,
        profile_home=alpha_home,
        profile="alpha",
    )

    default_adapter = SimpleNamespace(handle_message=AsyncMock())
    alpha_adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(default_adapter)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._profile_adapters = {
        "alpha": {Platform.TELEGRAM: alpha_adapter},
    }
    default_db = SessionDB(tmp_path / "state.db", read_only=True)
    runner._session_db = AsyncSessionDB(default_db)

    assert asyncio.run(
        runner._deliver_completion_notification("completion", alpha_event)
    ) is True
    default_db.close()

    default_adapter.handle_message.assert_not_awaited()
    alpha_adapter.handle_message.assert_awaited_once()
    delivered_event = alpha_adapter.handle_message.await_args.args[0]
    assert delivered_event.source.profile == "alpha"

    with _profile_runtime_scope(alpha_home):
        # The alpha DB owns only the durable result in this flow, not the
        # gateway transcript/session row used by preflight and inner pinning.
        with sqlite3.connect(alpha_home / "state.db") as alpha_db:
            assert (
                alpha_db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'sessions'"
                ).fetchone()
                is None
            )
        assert (
            async_delegation.get_durable_delegation(delegation_id)[
                "delivery_state"
            ]
            == "delivered"
        )
    assert (
        async_delegation.get_durable_delegation(delegation_id)[
            "delivery_state"
        ]
        == "pending"
    )


def test_stamped_profile_session_key_mismatch_is_quarantined_not_injected(
    tmp_path,
    monkeypatch,
):
    from tools import async_delegation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    event = _async_event("deleg-alpha-wrong-route")
    # In multiplex mode agent:main means default, but the authoritative row
    # lives in alpha. Injecting through either adapter would cross a tenant
    # boundary; the alpha row must be terminally quarantined.
    event["session_key"] = "agent:main:telegram:dm:12345"
    event = _persist_pending_completion_for_profile(
        event,
        profile_home=alpha_home,
        profile="alpha",
    )

    default_adapter = SimpleNamespace(handle_message=AsyncMock())
    alpha_adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(default_adapter)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._profile_adapters = {
        "alpha": {Platform.TELEGRAM: alpha_adapter},
    }

    assert asyncio.run(
        runner._deliver_completion_notification("completion", event)
    ) is None
    default_adapter.handle_message.assert_not_awaited()
    alpha_adapter.handle_message.assert_not_awaited()
    with _profile_runtime_scope(alpha_home):
        assert (
            async_delegation.get_durable_delegation(event["delegation_id"])[
                "delivery_state"
            ]
            == "dropped"
        )


def test_stamped_profile_persisted_source_mismatch_is_quarantined(
    tmp_path,
    monkeypatch,
):
    from tools import async_delegation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    event = _async_event("deleg-alpha-wrong-source")
    event["session_key"] = "agent:alpha:telegram:dm:12345"
    event = _persist_pending_completion_for_profile(
        event,
        profile_home=alpha_home,
        profile="alpha",
    )

    default_adapter = SimpleNamespace(handle_message=AsyncMock())
    alpha_adapter = SimpleNamespace(handle_message=AsyncMock())
    wrong_origin = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        profile="default",
    )
    runner = _runner(
        default_adapter,
        origins={
            event["session_key"]: SimpleNamespace(origin=wrong_origin),
        },
    )
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._profile_adapters = {
        "alpha": {Platform.TELEGRAM: alpha_adapter},
    }

    assert asyncio.run(
        runner._deliver_completion_notification("completion", event)
    ) is None
    default_adapter.handle_message.assert_not_awaited()
    alpha_adapter.handle_message.assert_not_awaited()
    with _profile_runtime_scope(alpha_home):
        assert (
            async_delegation.get_durable_delegation(event["delegation_id"])[
                "delivery_state"
            ]
            == "dropped"
        )


def test_unserved_store_is_never_settled_even_with_malformed_effect(
    tmp_path,
    monkeypatch,
):
    """A default-only gateway cannot mutate an alpha store for any reason."""
    from tools import async_delegation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    event = _async_event("deleg-alpha-unserved")
    event["session_key"] = "agent:alpha:telegram:dm:12345"
    event = _persist_pending_completion_for_profile(
        event,
        profile_home=alpha_home,
        profile="alpha",
    )
    # Corrupt the in-memory payload only after restore has attached the trusted
    # store capability; the foreign durable row itself remains valid/pending.
    event["runtime_effect"] = {"untrusted": "malformed"}

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    runner.config = SimpleNamespace(multiplex_profiles=False)

    assert asyncio.run(
        runner._deliver_completion_notification("completion", event)
    ) is False
    adapter.handle_message.assert_not_awaited()
    with _profile_runtime_scope(alpha_home):
        durable = async_delegation.get_durable_delegation(
            event["delegation_id"]
        )
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0


def test_missing_secondary_adapter_requeues_without_burning_claim(
    tmp_path,
    monkeypatch,
):
    from tools import async_delegation
    from tools.process_registry import process_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    event = _async_event("deleg-alpha-adapter-reconnect")
    event["session_key"] = "agent:alpha:telegram:dm:12345:678"
    event = _persist_pending_completion_for_profile(
        event,
        profile_home=alpha_home,
        profile="alpha",
    )

    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    process_registry.completion_queue.put(event)
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._profile_adapters = {"alpha": {}}
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    assert process_registry.completion_queue.qsize() == 1
    with _profile_runtime_scope(alpha_home):
        durable = async_delegation.get_durable_delegation(
            event["delegation_id"]
        )
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0


def test_multiplex_restart_restores_default_and_named_profile_stores(
    tmp_path,
    monkeypatch,
):
    from tools import async_delegation
    from tools.process_registry import ProcessRegistry
    import tools.process_registry as process_registry_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)

    default_event = _async_event("deleg-default-restart")
    _persist_pending_completion(default_event)
    alpha_event = _async_event("deleg-alpha-restart")
    alpha_event["session_key"] = "agent:alpha:telegram:dm:alpha-chat"
    with _profile_runtime_scope(alpha_home):
        _persist_pending_completion(alpha_event)

    # Constructing the registry simulates process start and restores the launch
    # home. Gateway startup must then add every other validated served home
    # before its completion watcher starts.
    restarted_registry = ProcessRegistry()
    monkeypatch.setattr(
        process_registry_module,
        "process_registry",
        restarted_registry,
    )
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    runner.config = SimpleNamespace(multiplex_profiles=True)

    assert runner._restore_async_delegations_for_served_profiles() == 1
    restored = []
    while not restarted_registry.completion_queue.empty():
        restored.append(restarted_registry.completion_queue.get_nowait())
    assert {
        event["delegation_id"] for event in restored
    } == {"deleg-default-restart", "deleg-alpha-restart"}
    stores = {
        event["delegation_id"]: async_delegation.get_event_delivery_store(event)
        for event in restored
    }
    # The default store is canonically represented by a null profile plus its
    # exact HERMES_HOME; named profiles retain their explicit profile id.
    assert stores["deleg-default-restart"].profile is None
    assert stores["deleg-default-restart"].hermes_home == str(tmp_path.resolve())
    assert stores["deleg-alpha-restart"].profile == "alpha"
    assert stores["deleg-alpha-restart"].hermes_home == str(alpha_home.resolve())


def test_named_profile_api_completion_ignores_raw_id_cache_collision(
    tmp_path,
    monkeypatch,
):
    from hermes_constants import get_hermes_home
    from hermes_state import AsyncSessionDB, SessionDB
    from tools import async_delegation
    import gateway.wake as wake_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    parent_id = "alpha-api-parent"
    # A colliding terminal row in the push runner's shared DB must not decide
    # the alpha API conversation's fate.
    _create_session(tmp_path, parent_id, end_reason="session_reset")
    _create_session(alpha_home, parent_id)
    event = _async_event("deleg-alpha-api")
    event["session_key"] = "alpha-raw-session"
    event["origin_session_id"] = "alpha-raw-session"
    event["parent_session_id"] = parent_id
    event = _persist_pending_completion_for_profile(
        event,
        profile_home=alpha_home,
        profile="alpha",
    )

    api_adapter = SimpleNamespace(supports_async_delivery=False)
    # Raw API ids have no profile namespace. A colliding default-profile
    # SessionStore entry must not override the trusted alpha delivery-store
    # stamp or quarantine the event.
    colliding_default_origin = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="unrelated-default-chat",
        chat_type="dm",
        profile="default",
    )
    runner = _runner(
        SimpleNamespace(handle_message=AsyncMock()),
        origins={
            "alpha-raw-session": SimpleNamespace(
                origin=colliding_default_origin,
            ),
        },
    )
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner.adapters[Platform.API_SERVER] = api_adapter
    runner._profile_adapters = {"alpha": {}}
    default_db = SessionDB(tmp_path / "state.db", read_only=True)
    runner._session_db = AsyncSessionDB(default_db)
    captured = {}

    async def _capture_wake(adapter, **kwargs):
        captured["adapter"] = adapter
        captured["profile"] = kwargs.get("profile")
        captured["home"] = get_hermes_home()
        captured["session_id"] = kwargs.get("session_id")

    monkeypatch.setattr(wake_module, "deliver_wake", _capture_wake)

    try:
        assert asyncio.run(
            runner._deliver_completion_notification("completion", event)
        ) is True
    finally:
        default_db.close()
    assert captured == {
        "adapter": api_adapter,
        "profile": "alpha",
        "home": alpha_home,
        "session_id": "alpha-raw-session",
    }
    with _profile_runtime_scope(alpha_home):
        assert (
            async_delegation.get_durable_delegation(event["delegation_id"])[
                "delivery_state"
            ]
            == "delivered"
        )


def test_explicit_reset_drop_is_terminal_not_falsely_delivered(
    monkeypatch, isolated_registry,
):
    """An explicit /new boundary drop gets a terminal 'dropped' disposition.

    Not 'delivered' (the ack must stay honest — nothing was injected) and not
    'pending' (restart recovery would replay a completion that is fail-closed
    dropped again on every boot).
    """
    from tools import async_delegation

    event = _async_event("deleg_explicit_new")
    event["parent_session_id"] = "sess_reset"
    _persist_pending_completion(event)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(return_value={
            "id": "sess_reset",
            "ended_at": "2026-07-16T12:00:00",
            "end_reason": "session_reset",
        }),
        get_compression_tip=AsyncMock(),
    )

    assert asyncio.run(
        runner._deliver_completion_notification("completion", event)
    ) is None

    adapter.handle_message.assert_not_awaited()
    durable = async_delegation.get_durable_delegation(event["delegation_id"])
    assert durable is not None
    assert durable["delivery_state"] == "dropped"
    restored = queue.Queue()
    assert async_delegation.restore_undelivered_completions(restored) == 0


def test_midflight_compression_rotation_stays_pending_for_retry(
    monkeypatch, isolated_registry,
):
    """A rotation without a visible continuation yet is retryable, not dropped."""
    from tools import async_delegation

    event = _async_event("deleg_midflight")
    event["parent_session_id"] = "sess_rotating"
    _persist_pending_completion(event)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(return_value={
            "id": "sess_rotating",
            "ended_at": "2026-07-16T12:00:00",
            "end_reason": "compression",
        }),
        get_compression_tip=AsyncMock(return_value=None),
    )

    assert asyncio.run(
        runner._deliver_completion_notification("completion", event)
    ) is False

    adapter.handle_message.assert_not_awaited()
    durable = async_delegation.get_durable_delegation(event["delegation_id"])
    assert durable is not None
    assert durable["delivery_state"] == "pending"
    restored = queue.Queue()
    assert async_delegation.restore_undelivered_completions(restored) == 1
    assert restored.get_nowait()["delegation_id"] == event["delegation_id"]


def test_transient_parent_resolution_retries_do_not_consume_attempts(
    monkeypatch, isolated_registry,
):
    """A compression race/DB outage cannot age a valid result into a drop."""
    from tools import async_delegation

    event = _async_event("deleg_attempt_cap")
    event["parent_session_id"] = "sess_rotating"
    _persist_pending_completion(event)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(return_value={
            "id": "sess_rotating",
            "ended_at": "2026-07-16T12:00:00",
            "end_reason": "compression",
        }),
        get_compression_tip=AsyncMock(return_value=None),
    )

    async def _churn():
        for _ in range(async_delegation._MAX_DELIVERY_ATTEMPTS + 2):
            await runner._deliver_completion_notification("completion", event)

    asyncio.run(_churn())

    adapter.handle_message.assert_not_awaited()
    durable = async_delegation.get_durable_delegation(event["delegation_id"])
    assert durable is not None
    assert durable["delivery_state"] == "pending"
    assert durable["delivery_attempts"] == 0
    restored = queue.Queue()
    assert async_delegation.restore_undelivered_completions(restored) == 1
    assert restored.get_nowait()["delegation_id"] == event["delegation_id"]


def test_distinct_process_incarnations_are_not_deduplicated():
    """Producer spawn time distinguishes a reused process session ID."""
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _exercise():
        first = await runner._deliver_completion_notification(
            "first", _completion_event(started_at=10.0)
        )
        second = await runner._deliver_completion_notification(
            "second", _completion_event(started_at=20.0)
        )
        return first, second

    assert asyncio.run(_exercise()) == (True, True)

    assert adapter.handle_message.await_count == 2


def test_delivered_identity_retention_is_bounded():
    """Lifecycle dedupe cannot grow without bound in a long-running gateway."""
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    runner._completion_delivery_retention = 2
    runner._completion_deliveries_delivered = OrderedDict()

    async def _exercise():
        for index in range(3):
            await runner._deliver_completion_notification(
                f"completion {index}",
                _async_event(f"deleg_retention_{index}"),
            )

    asyncio.run(_exercise())

    assert len(runner._completion_deliveries_delivered) == 2
    assert ("async_delegation", "deleg_retention_0", "") not in (
        runner._completion_deliveries_delivered
    )
    assert ("async_delegation", "deleg_retention_2", "") in (
        runner._completion_deliveries_delivered
    )


def test_delivery_state_is_isolated_per_gateway_profile_lifecycle():
    """A process-local claim in one profile never suppresses another runner."""
    default_adapter = SimpleNamespace(handle_message=AsyncMock())
    profile_adapter = SimpleNamespace(handle_message=AsyncMock())
    default_runner = _runner(default_adapter)
    profile_runner = _runner(profile_adapter)
    event = _async_event("deleg_same_producer_id")

    async def _exercise():
        first = await default_runner._deliver_completion_notification(
            "default", dict(event),
        )
        second = await profile_runner._deliver_completion_notification(
            "profile", dict(event),
        )
        return first, second

    assert asyncio.run(_exercise()) == (True, True)
    default_adapter.handle_message.assert_awaited_once()
    profile_adapter.handle_message.assert_awaited_once()


def test_async_completion_uses_canonical_origin_routing(monkeypatch, isolated_registry):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    event = _async_event("deleg_routing")
    isolated.put(event)

    canonical = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="canonical-chat",
        chat_type="group",
        thread_id="canonical-topic",
    )
    entry = SimpleNamespace(origin=canonical)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter, origins={event["session_key"]: entry})
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    delivered = adapter.handle_message.await_args.args[0]
    assert delivered.source == canonical


def test_explicit_kill_returns_output_before_consuming_notification(monkeypatch):
    import tools.process_registry as pr_module

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_kill_consumed",
        command="sleep 999",
        task_id="task",
        started_at=1.0,
        output_buffer="important terminal output\n",
        notify_on_complete=True,
    )
    session.process = MagicMock()
    session.process.pid = 4242
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_terminate_host_pid", lambda *_a, **_kw: None)
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    monkeypatch.setattr(pr_module, "process_registry", registry)

    result = registry.kill_process(session.id)
    assert result["status"] == "killed"
    assert result["output"] == "important terminal output\n"
    assert registry.is_completion_consumed(session.id)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _instant_sleep(*_a, **_kw):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    asyncio.run(runner._run_process_watcher({
        "session_id": session.id,
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "notify_on_complete": True,
    }))

    adapter.handle_message.assert_not_awaited()


def test_process_tool_redacts_explicit_kill_output(monkeypatch):
    from tools import process_registry as pr_module

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_kill_redacted",
        command="printenv",
        task_id="task",
        owner_task_id="redaction-test-owner",
        started_at=1.0,
        output_buffer="PRIVATE_TOKEN=opaque-value\n",
        exited=True,
        exit_code=0,
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(pr_module, "process_registry", registry)

    def _redact(result):
        assert result["output"] == "PRIVATE_TOKEN=opaque-value\n"
        result["output"] = "PRIVATE_TOKEN=<redacted>\n"
        return result

    monkeypatch.setattr(pr_module, "_redact_process_result", _redact)

    result = json.loads(pr_module._handle_process({
        "action": "kill",
        "session_id": session.id,
    }, task_id="redaction-test-owner"))
    assert result["output"] == "PRIVATE_TOKEN=<redacted>\n"


def test_kill_of_already_exited_process_returns_output_before_consuming():
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_already_exited",
        command="echo complete",
        task_id="task",
        started_at=1.0,
        output_buffer="complete\n",
        exited=True,
        exit_code=0,
    )
    registry._finished[session.id] = session

    result = registry.kill_process(session.id)

    assert result["status"] == "already_exited"
    assert result["output"] == "complete\n"
    assert registry.is_completion_consumed(session.id)


def test_read_log_only_consumes_when_terminal_output_page_is_observed():
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_paged_log",
        command="printf lines",
        task_id="task",
        started_at=1.0,
        output_buffer="first\nsecond\nfinal\n",
        exited=True,
        exit_code=0,
    )
    registry._finished[session.id] = session

    middle_page = registry.read_log(session.id, offset=1, limit=1)
    assert middle_page["output"] == "second"
    assert not registry.is_completion_consumed(session.id)

    final_page = registry.read_log(session.id, offset=2, limit=1)
    assert final_page["output"] == "final"
    assert registry.is_completion_consumed(session.id)


def test_bulk_kill_does_not_consume_discarded_completion_output(monkeypatch):
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_bulk_kill",
        command="sleep 999",
        task_id="task",
        started_at=1.0,
        output_buffer="output bulk cleanup does not return\n",
        notify_on_complete=True,
    )
    session.process = MagicMock()
    session.process.pid = 4243
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_terminate_host_pid", lambda *_a, **_kw: None)
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)

    assert registry.kill_all() == 1
    assert not registry.is_completion_consumed(session.id)
    queued = registry.completion_queue.get_nowait()
    assert queued["session_id"] == session.id
    assert queued["started_at"] == session.started_at
    assert queued["output"] == "output bulk cleanup does not return\n"


def test_unobserved_normal_completion_still_notifies(monkeypatch):
    import tools.process_registry as pr_module

    class _Registry:
        def get(self, _session_id):
            return SimpleNamespace(
                output_buffer="done\n",
                exited=True,
                exit_code=0,
                command="echo done",
                started_at=1234.5,
            )

        def is_completion_consumed(self, _session_id):
            return False

    monkeypatch.setattr(pr_module, "process_registry", _Registry())
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _instant_sleep(*_a, **_kw):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    asyncio.run(runner._run_process_watcher({
        "session_id": "proc_unobserved",
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "notify_on_complete": True,
    }))

    adapter.handle_message.assert_awaited_once()


def test_autonomous_completion_redacts_real_command_and_output_secrets(monkeypatch):
    import agent.redact as redact_module
    import tools.process_registry as pr_module

    secret = "abc123randomopaquetokenvalue999"
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_autonomous_redaction",
        command=f"printenv MY_SERVICE_TOKEN={secret}",
        task_id="task",
        started_at=1234.5,
        output_buffer=f"MY_SERVICE_TOKEN={secret}\nHOME=/home/user\n",
        exited=True,
        exit_code=0,
        notify_on_complete=True,
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(pr_module, "process_registry", registry)
    monkeypatch.setattr(redact_module, "_REDACT_ENABLED", True)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _instant_sleep(*_a, **_kw):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    asyncio.run(runner._run_process_watcher({
        "session_id": session.id,
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "notify_on_complete": True,
    }))

    delivered = adapter.handle_message.await_args.args[0]
    assert secret not in delivered.text
    assert "HOME=/home/user" in delivered.text
