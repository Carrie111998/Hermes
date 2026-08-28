"""Real gateway/session lifecycle coverage for pending clarifies."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore, build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture(autouse=True)
def _isolated_clarifies():
    from tools import clarify_gateway as cm

    cm.clear_all()
    yield
    cm.clear_all()


def _wait_until_waiter_started(entry, timeout: float = 2.0) -> None:
    """Synchronize with wait_for_response without a timing-only sleep."""
    from tools import clarify_gateway as cm

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with cm._lock:
            if entry.waiter_started:
                return
        time.sleep(0.001)
    raise AssertionError("clarify waiter did not start")


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123456",
        chat_type="dm",
        user_id="u1",
    )


@pytest.mark.asyncio
async def test_busy_stop_invalidates_waiter_before_agent_interrupt():
    """The real busy /stop path wakes clarify before interrupting its agent."""
    from tools import clarify_gateway as cm

    runner, _adapter = make_restart_runner()
    source = _source()
    session_key = build_session_key(source)
    entry = cm.register("stop-pending", session_key, "Pick", ["A"])
    agent = MagicMock()

    def _interrupt(_reason):
        assert not cm.has_pending(session_key)
        assert entry.event.is_set()

    agent.interrupt.side_effect = _interrupt
    runner._running_agents = {session_key: agent}
    event = MessageEvent(text="/stop", source=source, message_id="m-stop")

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiter = pool.submit(cm.wait_for_response, entry.clarify_id, 5.0)
        _wait_until_waiter_started(entry)
        await runner._busy_stop_command(event, session_key, source)
        assert waiter.result(timeout=2.0) == ""

    agent.interrupt.assert_called_once()


@pytest.mark.asyncio
async def test_idle_stop_invalidates_orphan_before_session_lookup():
    """Normal /stop dispatch clears a waiter even when no agent slot exists."""
    from tools import clarify_gateway as cm

    runner, _adapter = make_restart_runner()
    source = _source()
    session_key = build_session_key(source)
    entry = cm.register("idle-stop-pending", session_key, "Pick", ["A"])

    async def _get_after_clear(_source_arg):
        assert not cm.has_pending(session_key)
        assert entry.event.is_set()
        return SimpleNamespace(session_key=session_key)

    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(side_effect=_get_after_clear),
    )
    runner._sibling_thread_run_keys = lambda _source_arg, _key: []
    event = MessageEvent(text="/stop", source=source, message_id="m-stop-idle")

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiter = pool.submit(cm.wait_for_response, entry.clarify_id, 5.0)
        _wait_until_waiter_started(entry)
        result = await runner._handle_stop_command(event)
        assert waiter.result(timeout=2.0) == ""

    assert "no active" in str(result).lower()


def test_turn_completion_release_invalidates_waiter():
    """The gateway's common completion/release funnel owns clarify cleanup."""
    from tools import clarify_gateway as cm

    runner, _adapter = make_restart_runner()
    session_key = build_session_key(_source())
    runner._running_agents = {session_key: MagicMock()}
    entry = cm.register("complete-pending", session_key, "Pick", ["A"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiter = pool.submit(cm.wait_for_response, entry.clarify_id, 5.0)
        _wait_until_waiter_started(entry)
        assert runner._release_running_agent_state(session_key) is True
        assert waiter.result(timeout=2.0) == ""

    assert not cm.has_pending(session_key)


def test_idle_reset_exact_boundary_then_invalidates_before_rotation(tmp_path):
    """Idle policy remains strict-greater-than and clears on the first due tick."""
    from tools import clarify_gateway as cm

    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode="idle", idle_minutes=1)
    )
    store = SessionStore(
        sessions_dir=tmp_path,
        config=config,
        session_boundary_cleanup_fn=cm.clear_session,
    )
    source = _source()
    original = store.get_or_create_session(source, touch_activity=False)
    boundary = datetime(2026, 8, 28, 12, 0, 0)
    original.updated_at = boundary - timedelta(minutes=1)
    entry = cm.register(
        "idle-pending",
        original.session_key,
        "Pick",
        ["A"],
        origin=cm.ClarifyOrigin("u1", "123456"),
        session_id=original.session_id,
        active_session_transaction=lambda action: store.run_if_session_current(
            original.session_key, original.session_id, action
        ),
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiter = pool.submit(cm.wait_for_response, entry.clarify_id, 5.0)
        _wait_until_waiter_started(entry)
        with patch("gateway.session._now", return_value=boundary):
            same = store.get_or_create_session(source, touch_activity=False)
        assert same.session_id == original.session_id
        assert cm.has_pending(original.session_key)

        with patch(
            "gateway.session._now",
            return_value=boundary + timedelta(microseconds=1),
        ):
            rotated = store.get_or_create_session(source, touch_activity=False)
        assert rotated.session_id != original.session_id
        assert waiter.result(timeout=2.0) == ""


def test_callback_consumption_is_atomic_against_concurrent_rotation(tmp_path):
    """A callback holding the route transaction completes before reset rotates."""
    from tools import clarify_gateway as cm

    store = SessionStore(
        sessions_dir=tmp_path,
        config=GatewayConfig(),
        session_boundary_cleanup_fn=cm.clear_session,
    )
    source = _source()
    original = store.get_or_create_session(source)
    callback_holds_route = threading.Event()
    allow_callback = threading.Event()
    rotation_done = threading.Event()

    def _controlled_transaction(action):
        def _inside_route():
            callback_holds_route.set()
            assert allow_callback.wait(2.0)
            return action()

        return store.run_if_session_current(
            original.session_key, original.session_id, _inside_route
        )

    entry = cm.register(
        "callback-wins",
        original.session_key,
        "Pick",
        ["A"],
        origin=cm.ClarifyOrigin("u1", "123456"),
        session_id=original.session_id,
        active_session_transaction=_controlled_transaction,
    )

    def _rotate():
        try:
            return store.reset_session(original.session_key)
        finally:
            rotation_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        callback = pool.submit(
            cm.resolve_bound_choice,
            entry.clarify_id,
            0,
            binding=entry.binding,
            observed_origin=entry.binding.origin,
        )
        assert callback_holds_route.wait(2.0)
        rotation = pool.submit(_rotate)
        assert not rotation_done.wait(0.05), "rotation crossed the callback transaction"
        allow_callback.set()
        assert callback.result(timeout=2.0) is True
        rotated = rotation.result(timeout=2.0)

    assert rotated is not None
    assert rotated.session_id != original.session_id
    # Callback-before-wait is a supported first-writer-wins interleaving.
    assert cm.wait_for_response(entry.clarify_id, timeout=0.1) == "A"


def test_rotation_cleanup_wins_before_late_callback(tmp_path):
    """A rotation already holding SessionStore invalidates before callback entry."""
    from tools import clarify_gateway as cm

    rotation_holds_route = threading.Event()
    allow_rotation = threading.Event()
    callback_attempted = threading.Event()

    def _controlled_cleanup(session_key):
        rotation_holds_route.set()
        assert allow_rotation.wait(2.0)
        return cm.clear_session(session_key)

    store = SessionStore(
        sessions_dir=tmp_path,
        config=GatewayConfig(),
        session_boundary_cleanup_fn=_controlled_cleanup,
    )
    source = _source()
    original = store.get_or_create_session(source)

    def _late_transaction(action):
        callback_attempted.set()
        return store.run_if_session_current(
            original.session_key, original.session_id, action
        )

    entry = cm.register(
        "rotation-wins",
        original.session_key,
        "Pick",
        ["A"],
        origin=cm.ClarifyOrigin("u1", "123456"),
        session_id=original.session_id,
        active_session_transaction=_late_transaction,
    )

    with ThreadPoolExecutor(max_workers=3) as pool:
        waiter = pool.submit(cm.wait_for_response, entry.clarify_id, 5.0)
        _wait_until_waiter_started(entry)
        rotation = pool.submit(store.reset_session, original.session_key)
        assert rotation_holds_route.wait(2.0)
        callback = pool.submit(
            cm.resolve_bound_choice,
            entry.clarify_id,
            0,
            binding=entry.binding,
            observed_origin=entry.binding.origin,
        )
        assert callback_attempted.wait(2.0)
        allow_rotation.set()
        assert rotation.result(timeout=2.0) is not None
        assert callback.result(timeout=2.0) is False
        assert waiter.result(timeout=2.0) == ""
