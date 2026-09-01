"""apply_session_options() must rendezvous with turn admission (#92185).

Reviewer finding 2 on #92187: ``apply_gateway_session_options`` reads
``_is_session_running`` once, *before* it yields to the event loop, and never
re-checks. A turn admitted during any of its awaits is therefore invisible to
it and the call returns ``accepted`` while the session is busy.

These tests are the deterministic barrier the review asked for: pause the
options coroutine after its idle observation (at a real await seam), admit a
turn the same way ``_handle_message`` does, resume, and require a
``session_busy`` rejection with no durable write and no live mutation.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import _AGENT_PENDING_SENTINEL, GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.session_state import SERVICE_TIER_UNSET


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


@pytest.fixture
def store(tmp_path, monkeypatch) -> SessionStore:
    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)
    built = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    assert built._db is None
    return built


def _make_runner(store: SessionStore) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = store
    runner._session_options_locks = {}
    runner._session_db = None
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "old-model",
        {"provider": "openrouter", "base_url": "", "api_key": ""},
    )
    runner.evictions = []
    runner._evict_cached_agent = lambda session_key: runner.evictions.append(session_key)
    return runner


def _admit_turn(runner: GatewayRunner, session_key: str) -> int:
    """Mirror the synchronous claim block in ``_handle_message``.

    gateway/run.py:18176-18182 — claim the slot (sentinel + started_ts) and
    bump the run generation before any await. ``_claim_active_session_slot``
    returns ``(None, None)`` when ``max_concurrent_sessions`` is unset, so the
    lease assignment is a no-op in the default configuration.
    """
    state = runner._session_state(session_key)
    state.turn.agent = _AGENT_PENDING_SENTINEL
    state.turn.started_ts = 0.0
    return runner._begin_session_run_generation(session_key)


class _PausingStore(AsyncSessionStore):
    """AsyncSessionStore whose first get_or_create_session parks on a gate."""

    def __init__(self, inner: SessionStore, reached: asyncio.Event, gate: asyncio.Event):
        super().__init__(inner)
        self._reached = reached
        self._gate = gate

    async def get_or_create_session(self, *args, **kwargs):
        self._reached.set()
        await self._gate.wait()
        return await asyncio.to_thread(self._store.get_or_create_session, *args, **kwargs)


def _assert_untouched(runner: GatewayRunner, store: SessionStore, session_key: str) -> None:
    durable = store.get_runtime_options(session_key)
    assert durable in (
        None,
        {"model_override": None, "reasoning_override": None, "service_tier_override": None},
    ), durable
    state = runner._session_state(session_key)
    assert state.conversation.model_override is None
    assert state.conversation.reasoning_override is None
    assert state.conversation.service_tier_override is SERVICE_TIER_UNSET
    assert runner.evictions == []
    assert not getattr(runner, "_pending_model_notes", {})


@pytest.mark.asyncio
async def test_turn_admitted_during_persist_await_is_rejected(store):
    """Reasoning-only patch: the only awaits after the idle check are the two
    store calls (session_options.py:254-255). Pause inside the first one."""
    runner = _make_runner(store)
    source = _make_source()
    session_key = runner._session_key_for_source(
        runner._normalize_source_for_session_key(source)
    )
    reached, gate = asyncio.Event(), asyncio.Event()
    runner._async_session_store = _PausingStore(store, reached, gate)

    options_task = asyncio.create_task(
        runner.apply_session_options(source, {"reasoning_effort": "high"})
    )
    await asyncio.wait_for(reached.wait(), timeout=2.0)
    assert runner._is_session_running(session_key) is False  # idle was observed

    _admit_turn(runner, session_key)
    assert runner._is_session_running(session_key) is True

    gate.set()
    result = await asyncio.wait_for(options_task, timeout=2.0)

    assert result["status"] == "rejected", result
    assert result["code"] == "session_busy", result
    _assert_untouched(runner, store, session_key)


@pytest.mark.asyncio
async def test_turn_admitted_during_model_resolution_is_rejected(store):
    """Model patch: ``switch_model`` runs on a worker thread
    (session_options.py:149). Park the worker, admit a turn on the loop."""
    runner = _make_runner(store)
    runner._async_session_store = AsyncSessionStore(store)
    source = _make_source()
    session_key = runner._session_key_for_source(
        runner._normalize_source_for_session_key(source)
    )
    loop = asyncio.get_running_loop()
    reached = asyncio.Event()
    release = threading.Event()

    def _blocking_switch_model(**_kwargs):
        loop.call_soon_threadsafe(reached.set)
        assert release.wait(timeout=5.0), "test did not release switch_model"
        return SimpleNamespace(
            success=True,
            new_model="gpt-5",
            target_provider="openai",
            api_key="sk-live-only",
            base_url="https://api.openai.com/v1",
            api_mode="responses",
            model_info=None,
            warning_message="",
        )

    with (
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("hermes_cli.model_switch.switch_model", _blocking_switch_model),
        patch(
            "hermes_cli.model_selection_guards.combined_selection_warning",
            return_value=None,
        ),
    ):
        options_task = asyncio.create_task(
            runner.apply_session_options(
                source,
                {"model": "gpt-5", "provider": "openai", "confirm_model_selection": True},
            )
        )
        await asyncio.wait_for(reached.wait(), timeout=2.0)
        assert runner._is_session_running(session_key) is False

        _admit_turn(runner, session_key)

        release.set()
        result = await asyncio.wait_for(options_task, timeout=5.0)

    assert result["status"] == "rejected", result
    assert result["code"] == "session_busy", result
    _assert_untouched(runner, store, session_key)


@pytest.mark.asyncio
async def test_turn_arriving_mid_transaction_waits_for_commit(store):
    """Shared-exclusion direction: a turn whose claim block runs while an
    options transaction is in flight must wait for the commit, then admit
    and see the committed values (it must not be rejected or race)."""
    runner = _make_runner(store)
    source = _make_source()
    session_key = runner._session_key_for_source(
        runner._normalize_source_for_session_key(source)
    )
    reached, gate = asyncio.Event(), asyncio.Event()
    runner._async_session_store = _PausingStore(store, reached, gate)

    options_task = asyncio.create_task(
        runner.apply_session_options(source, {"reasoning_effort": "high"})
    )
    await asyncio.wait_for(reached.wait(), timeout=2.0)

    async def _admission():
        # Same shape as the claim block in _handle_message: take the shared
        # per-session lock, then claim synchronously.
        from gateway.session_options import session_admission_lock

        async with session_admission_lock(runner, session_key):
            return _admit_turn(runner, session_key)

    admit_task = asyncio.create_task(_admission())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not admit_task.done()  # parked behind the in-flight transaction
    assert runner._is_session_running(session_key) is False

    gate.set()
    result = await asyncio.wait_for(options_task, timeout=2.0)
    await asyncio.wait_for(admit_task, timeout=2.0)

    assert result["status"] == "accepted", result
    assert runner._is_session_running(session_key) is True
    assert store.get_runtime_options(session_key)["reasoning_override"] == {
        "enabled": True,
        "effort": "high",
    }
    assert runner._session_state(session_key).conversation.reasoning_override == {
        "enabled": True,
        "effort": "high",
    }
