"""Per-session admission exclusion shared by turn start and runtime options.

Contract (#92185 / review finding 2 on #92187):

* ``gateway.session_options.session_admission_lock(runner, key)`` is the ONE
  exclusion a session crosses when it flips idle->running. ``_handle_message`` awaits it around
  its claim block; ``apply_session_options`` holds it across check + persist +
  mutate; the boot-resume pre-claim (which cannot await) skips a session whose
  lock is held.
* Taking the uncontended lock never yields, so the gateway's "claim before
  any await" discipline is unchanged on the hot path.

Plus the /model half of review finding 1: every session-runtime mutation goes
through one durable-first primitive, so a failed durable write makes the slash
command fail loudly with live state untouched, and ``/model --once`` stays
live-only while sibling durable writes persist the pre-once model.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session_options import session_admission_lock, session_admission_lock_held
from gateway.session import SessionEntry, SessionSource, SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _event(text: str) -> MessageEvent:
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_make_source())


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """SessionStores over one shared sessions dir, without SQLite."""

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)

    def _make() -> SessionStore:
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
        assert store._db is None
        return store

    return _make


# ---------------------------------------------------------------------------
# The exclusion itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uncontended_admission_lock_never_yields():
    """The claim block in ``_handle_message`` runs under the shared lock. For
    the "claim before any await" discipline to survive, taking the lock when
    nobody holds it must not give the loop a chance to run anything else."""
    runner = object.__new__(GatewayRunner)
    key = "agent:main:telegram:dm:c1"
    sibling_ran = asyncio.Event()

    async def _sibling():
        sibling_ran.set()

    sibling = asyncio.create_task(_sibling())
    async with session_admission_lock(runner, key):
        assert not sibling_ran.is_set(), "uncontended acquire yielded to the loop"
    await sibling
    assert session_admission_lock(runner, key) is session_admission_lock(runner, key)
    assert session_admission_lock(runner, key) is not session_admission_lock(runner, key + "x")


@pytest.mark.asyncio
async def test_lock_held_probe_tracks_in_flight_transaction():
    runner = object.__new__(GatewayRunner)
    key = "agent:main:telegram:dm:c1"
    assert session_admission_lock_held(runner, key) is False  # never created
    async with session_admission_lock(runner, key):
        assert session_admission_lock_held(runner, key) is True
    assert session_admission_lock_held(runner, key) is False


@pytest.mark.asyncio
async def test_boot_resume_pre_claim_skips_session_under_options_transaction():
    """The startup auto-resume pre-claims ``turn.agent`` synchronously (it
    cannot await the lock). While an options transaction owns the session it
    must leave the session resume_pending instead of claiming underneath the
    in-flight commit; once the lock is free the same pass resumes it."""
    runner, adapter = make_restart_runner()
    runner._is_user_authorized = lambda _source: True
    runner._restart_loop_guard_config = lambda: (0, 0, 0)
    runner._run_startup_resume_event = AsyncMock()
    adapter.handle_message = AsyncMock()
    source = make_restart_source(chat_id="held-chat")
    key = "agent:main:telegram:dm:held-chat"
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
        assert runner._is_session_running(key) is False

    assert runner._schedule_resume_pending_sessions() == 1
    assert runner._is_session_running(key) is True


# ---------------------------------------------------------------------------
# /model through the shared durable-first primitive
# ---------------------------------------------------------------------------


def _model_runner(store: SessionStore, tmp_path, monkeypatch) -> GatewayRunner:
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
            new_model="gpt-5.5",
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

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = GatewayConfig()
    runner._voice_mode = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._service_tier = None
    runner._show_reasoning = False
    runner.session_store = store
    runner._session_options_locks = {}
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "old-model",
        {"provider": "openrouter", "base_url": "", "api_key": ""},
    )
    return runner


def _live_model(runner: GatewayRunner, key: str):
    override = runner._session_state(key).conversation.model_override
    return override.get("model") if override else None


def _durable_model(store: SessionStore, key: str):
    opts = store.get_runtime_options(key) or {}
    override = opts.get("model_override")
    return override.get("model") if override else None


@pytest.mark.asyncio
async def test_model_slash_fails_loudly_and_leaves_state_untouched_when_save_fails(
    store_factory, tmp_path, monkeypatch
):
    store = store_factory()
    store.get_or_create_session(_make_source())
    runner = _model_runner(store, tmp_path, monkeypatch)
    key = runner._session_key_for_source(_make_source())
    notes_before = dict(getattr(runner, "_pending_model_notes", {}) or {})

    def _fail(_data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_save_sessions_json", _fail)

    with pytest.raises(OSError):
        await runner._handle_model_command(_event("/model gpt-5.5"))

    assert _live_model(runner, key) is None
    assert _durable_model(store, key) is None
    assert dict(getattr(runner, "_pending_model_notes", {}) or {}) == notes_before
    fresh = _model_runner(store_factory(), tmp_path, monkeypatch)
    fresh._rehydrate_session_runtime_options(key)
    assert _live_model(fresh, key) is None


@pytest.mark.asyncio
async def test_model_slash_is_durable_on_healthy_store(store_factory, tmp_path, monkeypatch):
    store = store_factory()
    store.get_or_create_session(_make_source())
    runner = _model_runner(store, tmp_path, monkeypatch)
    key = runner._session_key_for_source(_make_source())

    reply = await runner._handle_model_command(_event("/model gpt-5.5"))

    assert reply and "gpt-5.5" in reply
    assert _live_model(runner, key) == "gpt-5.5"
    assert _durable_model(store, key) == "gpt-5.5"
    fresh = _model_runner(store_factory(), tmp_path, monkeypatch)
    fresh._rehydrate_session_runtime_options(key)
    assert _live_model(fresh, key) == "gpt-5.5"


@pytest.mark.asyncio
async def test_sibling_durable_write_during_model_once_persists_pre_once_model(
    store_factory, tmp_path, monkeypatch
):
    """``/model --once`` is live-only (#29923). A ``/reasoning`` issued while
    the one-turn override is live must persist the PRE-once model, and the
    post-turn restore must leave live == durable."""
    store = store_factory()
    store.get_or_create_session(_make_source())
    runner = _model_runner(store, tmp_path, monkeypatch)
    key = runner._session_key_for_source(_make_source())

    await runner._handle_model_command(_event("/model gpt-5.5 --once"))
    assert _live_model(runner, key) == "gpt-5.5"
    assert _durable_model(store, key) is None

    await runner._handle_reasoning_command(_event("/reasoning high"))
    assert _live_model(runner, key) == "gpt-5.5"  # one-turn override still live
    assert _durable_model(store, key) is None  # but never persisted
    assert store.get_runtime_options(key)["reasoning_override"] == {
        "enabled": True,
        "effort": "high",
    }

    runner._restore_pending_one_turn_model_override(key)
    assert _live_model(runner, key) is None
    fresh = _model_runner(store_factory(), tmp_path, monkeypatch)
    fresh._rehydrate_session_runtime_options(key)
    assert _live_model(fresh, key) is None
    assert fresh._session_state(key).conversation.reasoning_override == {
        "enabled": True,
        "effort": "high",
    }
