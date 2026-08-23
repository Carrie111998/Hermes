"""Contract of the shared runtime-options commit (#92185, review on #92187).

Every per-session runtime mutation — the structured host API and the
``/model``, ``/reasoning``, ``/fast`` slash commands — goes through one
durable-first primitive under one per-session admission lock. These tests pin
the behaviours that fall out of that single seam:

* a user slash command that lands while the host API is still validating
  stays authoritative (the API is refused with ``conflict``);
* an inbound turn whose claim runs during an in-flight API commit parks
  behind it and then runs exactly once (real ``_handle_message`` path);
* first-touch rehydration after a restart never clobbers a live one-shot
  override, and the one-shot restore puts the persisted override back;
* an API write on an idle-expired session consumes the auto-reset boundary
  and never resurrects the previous conversation's overrides;
* re-asserting the current options is a no-op (no durable write, no agent
  eviction, no "switched from X to X" note);
* a durable model commit supersedes a pending ``/model --once`` restore.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import _AGENT_PENDING_SENTINEL, GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore
from gateway.session_options import session_admission_lock
from gateway.session_state import SERVICE_TIER_UNSET
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _src() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _event(text: str) -> MessageEvent:
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_src())


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """SessionStores over one shared sessions dir, without SQLite."""

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)

    def _make(cfg: GatewayConfig | None = None) -> SessionStore:
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=cfg or GatewayConfig())
        assert store._db is None
        return store

    return _make


def _runner(store: SessionStore, cfg: GatewayConfig | None = None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = cfg or GatewayConfig()
    runner.session_store = store
    runner._session_db = None
    runner._session_options_locks = {}
    runner.evictions = []
    runner._evict_cached_agent = lambda key: runner.evictions.append(key)
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "old-model",
        {"provider": "openrouter", "base_url": "", "api_key": ""},
    )
    return runner


def _switch_result(model: str = "gpt-5") -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        new_model=model,
        target_provider="openai",
        api_key="sk-live-only",
        base_url="https://api.openai.com/v1",
        api_mode="responses",
        model_info=None,
        warning_message="",
        error_message="",
    )


def _durable(store: SessionStore, key: str) -> dict:
    return store.get_runtime_options(key) or {}


# ---------------------------------------------------------------------------
# Slash vs API: later user-issued slash commands stay authoritative
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_slash_during_api_validation_stays_authoritative(store_factory):
    """Slash dispatch runs before the turn claim, so a ``/reasoning`` on an
    idle session never flips the busy gate. The API validated against a
    snapshot that the user has since moved: it must refuse, not overwrite."""
    store = store_factory()
    key = store.get_or_create_session(_src()).session_key
    runner = _runner(store)
    loop = asyncio.get_running_loop()
    reached, release = asyncio.Event(), threading.Event()

    def _slow_switch(**_kwargs):
        loop.call_soon_threadsafe(reached.set)
        assert release.wait(timeout=5.0), "test did not release switch_model"
        return _switch_result()

    with (
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("hermes_cli.model_switch.switch_model", _slow_switch),
        patch(
            "hermes_cli.model_selection_guards.combined_selection_warning",
            return_value=None,
        ),
    ):
        api_task = asyncio.create_task(
            runner.apply_session_options(_src(), {"model": "gpt-5"})
        )
        await asyncio.wait_for(reached.wait(), timeout=2.0)
        # The user's slash write lands while the API is in its worker hop.
        await runner._set_session_reasoning_override(
            key, {"enabled": True, "effort": "low"}
        )
        release.set()
        result = await asyncio.wait_for(api_task, timeout=5.0)

    assert (result["status"], result["code"]) == ("rejected", "conflict"), result
    low = {"enabled": True, "effort": "low"}
    assert runner._session_state(key).conversation.reasoning_override == low
    assert _durable(store, key)["reasoning_override"] == low
    assert runner._session_state(key).conversation.model_override is None
    assert _durable(store, key)["model_override"] is None
    assert runner.evictions == []


@pytest.mark.asyncio
async def test_api_commit_and_slash_commit_serialise_on_the_same_lock(store_factory):
    """Both writers hold the admission lock across persist + live mutation, so
    their steps cannot interleave: the later writer sees the earlier commit."""
    store = store_factory()
    key = store.get_or_create_session(_src()).session_key
    runner = _runner(store)
    entered = asyncio.Event()
    gate = asyncio.Event()
    real_store_set = store.set_runtime_options
    calls: list[str] = []

    def _slow_set(*args, **kwargs):
        calls.append("set")
        return real_store_set(*args, **kwargs)

    class _GatedStore:
        """Async facade that parks the API's get_or_create inside the lock."""

        def __init__(self, inner):
            self._store = inner

        async def get_or_create_session(self, *a, **k):
            entered.set()
            await gate.wait()
            return await asyncio.to_thread(self._store.get_or_create_session, *a, **k)

        def __getattr__(self, name):
            attr = getattr(self._store, name)

            async def _offloaded(*a, **k):
                return await asyncio.to_thread(attr, *a, **k)

            return _offloaded

    runner._async_session_store = _GatedStore(store)
    store.set_runtime_options = _slow_set  # type: ignore[method-assign]

    api_task = asyncio.create_task(
        runner.apply_session_options(_src(), {"reasoning_effort": "high"})
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    slash_task = asyncio.create_task(
        runner._set_session_service_tier_override(key, "priority")
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not slash_task.done()  # parked behind the API's lock
    assert calls == []

    gate.set()
    result = await asyncio.wait_for(api_task, timeout=2.0)
    await asyncio.wait_for(slash_task, timeout=2.0)

    assert result["status"] == "accepted", result
    assert calls == ["set", "set"]
    conv = runner._session_state(key).conversation
    assert conv.reasoning_override == {"enabled": True, "effort": "high"}
    assert conv.service_tier_override == "priority"
    assert _durable(store, key) == {
        "model_override": None,
        "reasoning_override": {"enabled": True, "effort": "high"},
        "service_tier_override": "priority",
    }


# ---------------------------------------------------------------------------
# Real _handle_message claim parks behind an in-flight commit, runs once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_turn_parks_behind_in_flight_commit_then_runs_once():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="park-chat")
    session_key = runner._session_key_for_source(source)

    runner._handle_message = GatewayRunner._handle_message.__get__(runner, GatewayRunner)
    runner._release_running_agent_state = (
        GatewayRunner._release_running_agent_state.__get__(runner, GatewayRunner)
    )
    runner._check_slash_access = lambda *a, **k: None
    runner._begin_session_run_generation = lambda session_key: 1
    runner._is_session_run_current = lambda session_key, generation: True
    runner._invalidate_session_run_generation = lambda *a, **k: 0
    runner._claim_active_session_slot = lambda session_key, source: (object(), None)
    runner._active_session_leases = {}
    runner._busy_ack_ts = {}
    runner._post_turn_goal_continuation = AsyncMock()
    runner._is_user_authorized = lambda _source: True
    runner.session_store.get_or_create_session.return_value = None
    agent_runs: list[str] = []

    async def _fake_run(event, source, _quick_key, run_generation):
        agent_runs.append(_quick_key)
        return "OK"

    runner._handle_message_with_agent = _fake_run
    adapter.set_message_handler(runner._handle_message)
    adapter.send = AsyncMock()
    adapter._keep_typing = AsyncMock()
    adapter._stop_typing_refresh = AsyncMock()
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True, message_id="1"))
    adapter._run_processing_hook = AsyncMock()

    inbound = MessageEvent(text="hello", message_type=MessageType.TEXT, source=source)

    # Hold the session's admission lock as an in-flight options commit would.
    lock = session_admission_lock(runner, session_key)
    await lock.acquire()
    try:
        turn = asyncio.create_task(runner._handle_message(inbound))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not turn.done()
        assert agent_runs == []
        assert runner._is_session_running(session_key) is False
    finally:
        lock.release()

    assert await asyncio.wait_for(turn, timeout=2.0) == "OK"
    assert agent_runs == [session_key]
    assert runner._is_session_running(session_key) is False


# ---------------------------------------------------------------------------
# Boot-resume pre-claim skip: the skipped session is resumed by the next pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_resume_skipped_session_resumes_once_lock_is_free():
    runner, adapter = make_restart_runner()
    runner._is_user_authorized = lambda _source: True
    runner._restart_loop_guard_config = lambda: (0, 0, 0)
    runner._run_startup_resume_event = AsyncMock()
    adapter.handle_message = AsyncMock()
    source = make_restart_source(chat_id="held-chat")
    key = runner._session_key_for_source(source)
    entry = SessionEntry(
        session_key=key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {key: entry}

    async with session_admission_lock(runner, key):
        assert runner._schedule_resume_pending_sessions() == 0
        assert entry.resume_pending is True  # left for the next pass
        assert runner._is_session_running(key) is False

    assert runner._schedule_resume_pending_sessions() == 1
    assert runner._is_session_running(key) is True


# ---------------------------------------------------------------------------
# Rehydrate never clobbers a live one-shot override
# ---------------------------------------------------------------------------


def _model_runner(store: SessionStore, tmp_path, monkeypatch, switched_to: str) -> GatewayRunner:
    import yaml as _yaml

    from hermes_cli.model_switch import ModelSwitchResult

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        _yaml.safe_dump({"model": {"default": "old-model", "provider": "openrouter"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kw: ModelSwitchResult(
            success=True,
            new_model=switched_to,
            target_provider="openrouter",
            provider_changed=False,
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            provider_label="OpenRouter",
        ),
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hermes_home)

    runner = _runner(store)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._reasoning_config = None
    runner._service_tier = None
    runner._show_reasoning = False
    return runner


def _live_model(runner: GatewayRunner, key: str):
    override = runner._session_state(key).conversation.model_override
    return override.get("model") if override else None


@pytest.mark.asyncio
async def test_rehydrate_never_clobbers_a_live_one_shot_override(
    store_factory, tmp_path, monkeypatch
):
    """Restart with a persisted /model Y; the first action is ``/model X --once``.
    The turn must run X (not be swapped back to Y by the first-touch
    rehydrate) and the post-turn restore must put Y back, live AND durable."""
    store = store_factory()
    key = store.get_or_create_session(_src()).session_key
    store.set_runtime_options(
        key,
        model_override={"model": "persisted-y", "provider": "openrouter"},
        reasoning_override=None,
        service_tier_override=None,
    )
    runner = _model_runner(store, tmp_path, monkeypatch, switched_to="once-x")
    assert runner._session_state(key).persistent.runtime_options_rehydrated is False

    await runner._handle_model_command(_event("/model once-x --once"))
    assert _live_model(runner, key) == "once-x"

    # Every first-touch reader of the runtime rehydrates; it must not clobber.
    runner._rehydrate_session_runtime_options(key)
    assert _live_model(runner, key) == "once-x"
    assert _durable(store, key)["model_override"]["model"] == "persisted-y"

    runner._restore_pending_one_turn_model_override(key)
    assert _live_model(runner, key) == "persisted-y"
    assert _durable(store, key)["model_override"]["model"] == "persisted-y"


def test_rehydrate_fills_only_fields_still_at_default(store_factory):
    store = store_factory()
    key = store.get_or_create_session(_src()).session_key
    store.set_runtime_options(
        key,
        model_override={"model": "persisted-y", "provider": "openrouter"},
        reasoning_override={"enabled": True, "effort": "high"},
        service_tier_override="priority",
    )
    runner = _runner(store)
    conv = runner._session_state(key).conversation
    # A /moa-style live override installed before the first rehydrate touch.
    conv.model_override = {"provider": "moa", "model": "preset-a", "base_url": "moa://local"}

    runner._rehydrate_session_runtime_options(key)

    assert conv.model_override["provider"] == "moa"  # live wins
    assert conv.reasoning_override == {"enabled": True, "effort": "high"}  # filled
    assert conv.service_tier_override == "priority"  # filled


# ---------------------------------------------------------------------------
# Auto-reset boundary consumed by the API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_on_idle_expired_session_consumes_boundary_and_drops_old_scope(
    store_factory,
):
    cfg = GatewayConfig()
    cfg.default_reset_policy = SessionResetPolicy(mode="idle", idle_minutes=1)
    store = store_factory(cfg)
    entry = store.get_or_create_session(_src())
    key = entry.session_key
    runner = _runner(store, cfg)

    # Previous conversation: model + tier overrides, live and durable.
    await runner._commit_session_runtime_options(
        key,
        model_override={"model": "pre-reset-model", "provider": "openrouter"},
        service_tier_override="priority",
    )
    runner._pending_model_notes = {key: "[Note: stale note from the old conversation]"}
    store.lookup_by_session_key(key).updated_at -= timedelta(minutes=5)

    result = await runner.apply_session_options(_src(), {"reasoning_effort": "low"})

    assert result["status"] == "accepted", result
    assert result["applied"] == ["reasoning_effort"]
    fresh = store.lookup_by_session_key(key)
    assert fresh.session_id != entry.session_id
    assert fresh.was_auto_reset is False  # consumed: the next turn won't wipe us
    low = {"enabled": True, "effort": "low"}
    conv = runner._session_state(key).conversation
    assert conv.reasoning_override == low
    assert conv.model_override is None  # the old conversation's model is gone
    assert conv.service_tier_override is SERVICE_TIER_UNSET
    assert _durable(store, key) == {
        "model_override": None,
        "reasoning_override": low,
        "service_tier_override": None,
    }
    assert key not in runner._pending_model_notes


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_patch_is_a_noop(store_factory, monkeypatch):
    store = store_factory()
    runner = _runner(store)
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "gpt-5",  # a model with a fast tier
        {"provider": "openai", "base_url": "", "api_key": ""},
    )
    first = await runner.apply_session_options(_src(), {"reasoning_effort": "high", "fast": True})
    assert first["status"] == "accepted" and first["applied"] == ["reasoning_effort", "fast"]
    assert runner.evictions == [first["session_key"]]

    saves: list[int] = []
    real_save = store._save

    def _counting_save():
        saves.append(1)
        return real_save()

    monkeypatch.setattr(store, "_save", _counting_save)

    again = await runner.apply_session_options(_src(), {"reasoning_effort": "high", "fast": True})

    assert again["status"] == "accepted", again
    assert again["applied"] == []
    assert again["effective"]["reasoning_effort"] == "high"
    assert again["effective"]["fast"] is True
    assert saves == []
    assert runner.evictions == [first["session_key"]]  # no second eviction


@pytest.mark.asyncio
async def test_same_model_reassert_does_not_stage_a_switch_note(store_factory):
    store = store_factory()
    runner = _runner(store)
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "gpt-5",
        {"provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": "k"},
    )
    with (
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("hermes_cli.model_switch.switch_model", lambda **_k: _switch_result("gpt-5")),
        patch(
            "hermes_cli.model_selection_guards.combined_selection_warning",
            return_value=None,
        ),
    ):
        result = await runner.apply_session_options(_src(), {"model": "gpt-5"})

    assert result["status"] == "accepted", result
    assert not getattr(runner, "_pending_model_notes", {})


# ---------------------------------------------------------------------------
# Durable model commit supersedes a pending /model --once restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_model_commit_clears_pending_once_restore(
    store_factory, tmp_path, monkeypatch
):
    store = store_factory()
    key = store.get_or_create_session(_src()).session_key
    runner = _model_runner(store, tmp_path, monkeypatch, switched_to="once-x")

    await runner._handle_model_command(_event("/model once-x --once"))
    assert runner._session_state(key).conversation.one_turn_restore is not None

    with (
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("hermes_cli.model_switch.switch_model", lambda **_k: _switch_result("gpt-5")),
        patch(
            "hermes_cli.model_selection_guards.combined_selection_warning",
            return_value=None,
        ),
    ):
        result = await runner.apply_session_options(_src(), {"model": "gpt-5"})

    assert result["status"] == "accepted", result
    assert runner._session_state(key).conversation.one_turn_restore is None
    runner._restore_pending_one_turn_model_override(key)  # must be a no-op now
    assert _live_model(runner, key) == "gpt-5"
    assert _durable(store, key)["model_override"]["model"] == "gpt-5"


@pytest.mark.asyncio
async def test_busy_rejection_is_authoritative_under_the_lock(store_factory):
    """A turn that claims the slot while the API is validating (outside the
    lock) is seen by the locked busy check: nothing is written."""
    store = store_factory()
    runner = _runner(store)
    key = runner._session_key_for_source(runner._normalize_source_for_session_key(_src()))
    loop = asyncio.get_running_loop()
    reached, release = asyncio.Event(), threading.Event()

    def _slow_switch(**_kwargs):
        loop.call_soon_threadsafe(reached.set)
        assert release.wait(timeout=5.0)
        return _switch_result()

    with (
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("hermes_cli.model_switch.switch_model", _slow_switch),
        patch(
            "hermes_cli.model_selection_guards.combined_selection_warning",
            return_value=None,
        ),
    ):
        api_task = asyncio.create_task(runner.apply_session_options(_src(), {"model": "gpt-5"}))
        await asyncio.wait_for(reached.wait(), timeout=2.0)
        runner._session_state(key).turn.agent = _AGENT_PENDING_SENTINEL
        release.set()
        result = await asyncio.wait_for(api_task, timeout=5.0)

    assert (result["status"], result["code"]) == ("rejected", "session_busy"), result
    assert store.lookup_by_session_key(key) is None
    assert runner._session_state(key).conversation.model_override is None
