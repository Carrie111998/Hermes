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


def test_stale_turn_release_clears_only_its_owned_clarify():
    """An old unwind cannot cancel a replacement turn's prompt."""
    from tools import clarify_gateway as cm

    runner, _adapter = make_restart_runner()
    session_key = build_session_key(_source())
    old_generation = runner._begin_session_run_generation(session_key)
    old_entry = cm.register(
        "old-generation-pending",
        session_key,
        "Old prompt",
        ["old"],
        run_generation=old_generation,
    )

    runner._invalidate_session_run_generation(session_key, reason="test_stop")
    new_generation = runner._begin_session_run_generation(session_key)
    fresh_agent = MagicMock()
    runner._running_agents = {session_key: fresh_agent}
    new_entry = cm.register(
        "new-generation-pending",
        session_key,
        "New prompt",
        ["new"],
        run_generation=new_generation,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_waiter = pool.submit(cm.wait_for_response, old_entry.clarify_id, 5.0)
        new_waiter = pool.submit(cm.wait_for_response, new_entry.clarify_id, 5.0)
        _wait_until_waiter_started(old_entry)
        _wait_until_waiter_started(new_entry)

        released = runner._release_running_agent_state(
            session_key,
            run_generation=old_generation,
        )

        assert released is False
        assert old_waiter.result(timeout=2.0) == ""
        assert runner._running_agents[session_key] is fresh_agent
        assert cm.get_entry(new_entry.clarify_id) is new_entry
        assert not new_entry.event.is_set()

        assert cm.resolve_gateway_clarify(new_entry.clarify_id, "new") is True
        assert new_waiter.result(timeout=2.0) == "new"


def test_turn_runner_old_unwind_preserves_new_generation_clarify():
    """TurnRunner's real finally clears only the turn that is unwinding."""
    from gateway.run import TurnRunner
    from gateway.turn_context import TurnContext
    from tools import clarify_gateway as cm

    old_run_started = threading.Event()
    allow_old_unwind = threading.Event()

    class _OldTurnAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.session_id = kwargs["session_id"]
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=200_000,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0

        def run_conversation(self, _message, **_kwargs):
            old_run_started.set()
            assert allow_old_unwind.wait(2.0)
            return {
                "final_response": "old turn finished",
                "failed": False,
                "messages": [],
            }

    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._running = True
    runner._draining = False
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {
        "model": "test-model",
        "runtime": {},
    }
    runner._agent_config_signature.return_value = ("test-signature",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    runner._is_telegram_topic_lane.return_value = False
    runner._is_discord_auto_thread_lane.return_value = False
    runner._is_relay_discord_channel_lane.return_value = False

    session_key = build_session_key(_source())
    ctx = TurnContext(
        source=_source(),
        message="old turn",
        history=[],
        session_id="old-session",
        session_key=session_key,
        run_generation=1,
        user_config={},
        AIAgent=_OldTurnAgent,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: False,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    old_entry = cm.register(
        "turn-runner-old-pending",
        session_key,
        "Old prompt",
        ["old"],
        run_generation=1,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_run = pool.submit(TurnRunner(runner, ctx).run_sync)
        try:
            assert old_run_started.wait(2.0)
            new_entry = cm.register(
                "turn-runner-new-pending",
                session_key,
                "New prompt",
                ["new"],
                run_generation=2,
            )
            new_waiter = pool.submit(
                cm.wait_for_response,
                new_entry.clarify_id,
                5.0,
            )
            _wait_until_waiter_started(new_entry)
        finally:
            allow_old_unwind.set()

        assert old_run.result(timeout=2.0)["final_response"] == "old turn finished"
        assert old_entry.event.is_set()
        assert cm.get_entry(old_entry.clarify_id) is None
        assert cm.get_entry(new_entry.clarify_id) is new_entry
        assert not new_entry.event.is_set()
        assert cm.resolve_gateway_clarify(new_entry.clarify_id, "new") is True
        assert new_waiter.result(timeout=2.0) == "new"


def test_turn_runner_teardown_before_clarify_registration_fences_stale_prompt():
    """A stopped turn cannot register after its generation teardown finished."""
    from gateway.run import TurnRunner
    from gateway.turn_context import TurnContext
    from tools import clarify_gateway as cm

    register_entered = threading.Event()
    allow_register = threading.Event()
    turn_alive = threading.Event()
    turn_alive.set()
    original_register = cm.register

    def _register_after_teardown(*args, **kwargs):
        register_entered.set()
        assert allow_register.wait(2.0)
        return original_register(*args, **kwargs)

    class _ClarifyingAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.session_id = kwargs["session_id"]
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=200_000,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0

        def run_conversation(self, _message, **_kwargs):
            response = self.clarify_callback("Pick", ["A"])
            return {
                "final_response": response,
                "failed": False,
                "messages": [],
            }

    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._running = True
    runner._draining = False
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {
        "model": "test-model",
        "runtime": {},
    }
    runner._agent_config_signature.return_value = ("test-signature",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    runner._is_telegram_topic_lane.return_value = False
    runner._is_discord_auto_thread_lane.return_value = False
    runner._is_relay_discord_channel_lane.return_value = False

    session_key = build_session_key(_source())
    ctx = TurnContext(
        source=_source(),
        message="old turn",
        history=[],
        session_id="old-session",
        session_key=session_key,
        run_generation=1,
        user_config={},
        AIAgent=_ClarifyingAgent,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=turn_alive.is_set,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
        _status_adapter=MagicMock(),
        _status_chat_id="123456",
    )

    with (
        patch.object(cm, "register", side_effect=_register_after_teardown),
        patch(
            "gateway.run.safe_schedule_threadsafe",
            side_effect=AssertionError("stale clarify reached delivery"),
        ),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        old_run = pool.submit(TurnRunner(runner, ctx).run_sync)
        assert register_entered.wait(2.0)
        turn_alive.clear()
        assert cm.clear_session(session_key, run_generation=1) == 0
        allow_register.set()
        result = old_run.result(timeout=2.0)

    assert "no longer active" in result["final_response"]
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
