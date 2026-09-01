"""``/moa`` must never let a runtime-options commit persist its virtual provider.

``/moa <prompt>`` installs a live-only ``model_override`` whose provider is
the virtual ``moa`` provider. Two windows exist in which a durable commit
(host ``apply_session_options()``, or a ``/reasoning``/``/fast`` slash) can
observe the session as idle while that override is live:

* pre-claim: between the ``/moa`` slash branch and the idle->running claim in
  ``_handle_message`` there is a real ``asyncio.to_thread`` yield (the Telegram
  lobby probe). The install therefore runs only after the claim
  (``_install_moa_one_shot``), so nothing is live in this window.
* post-turn: ``_run_agent_inner``'s ``finally`` releases the running slot,
  then ``_handle_message_with_agent`` keeps awaiting before
  ``_handle_message``'s ``finally`` restores. The install parks the prior
  override in ``conversation.one_turn_restore`` (the ``/model --once`` slot),
  so the durable-first primitive persists that snapshot, never ``moa``.

These tests drive the real ``_handle_message`` and park it at each seam while
a commit lands, then require that nothing durable ever says ``moa``, that the
turn itself still ran through MoA, and that live and durable state agree once
the turn has restored the prior override.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.fixture
def store(tmp_path, monkeypatch) -> SessionStore:
    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)
    built = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    assert built._db is None
    return built


def _wire_runner(store, monkeypatch, chat_id):
    """Real ``_handle_message`` over a real store; the agent turn is stubbed."""
    runner, adapter = make_restart_runner()
    runner.session_store = store
    runner._session_db = None
    runner._session_options_locks = {}
    source = make_restart_source(chat_id=chat_id)
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
    runner.evictions = []
    runner._evict_cached_agent = lambda key: runner.evictions.append(key)
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "old-model",
        {"provider": "openrouter", "base_url": "", "api_key": ""},
    )
    runner._is_telegram_topic_root_lobby = lambda _source: False
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})

    adapter.set_message_handler(runner._handle_message)
    adapter.send = AsyncMock()
    adapter._keep_typing = AsyncMock()
    adapter._stop_typing_refresh = AsyncMock()
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True, message_id="1"))
    adapter._run_processing_hook = AsyncMock()
    store.get_or_create_session(source)  # routing entry exists, as for a live chat
    return runner, source, session_key


def _moa_event(source):
    return MessageEvent(text="/moa summarise this", message_type=MessageType.TEXT, source=source)


@pytest.mark.asyncio
async def test_host_commit_in_moa_pre_claim_window_never_persists_moa(store, monkeypatch):
    runner, source, session_key = _wire_runner(store, monkeypatch, "moa-chat")

    # Park the turn at the one real yield between the /moa branch and the
    # idle->running claim: the lobby probe runs via asyncio.to_thread.
    reached_seam = asyncio.Event()
    release_seam = threading.Event()
    loop = asyncio.get_running_loop()

    def _lobby_probe(_source):
        loop.call_soon_threadsafe(reached_seam.set)
        release_seam.wait(timeout=5)
        return False

    runner._is_telegram_topic_root_lobby = _lobby_probe

    seen_models: list = []

    async def _fake_run(event, source, _quick_key, run_generation):
        # What the agent turn would resolve: the live override at run time.
        seen_models.append(runner._session_state(_quick_key).conversation.model_override)
        return "OK"

    runner._handle_message_with_agent = _fake_run

    turn = asyncio.create_task(runner._handle_message(_moa_event(source)))
    await asyncio.wait_for(reached_seam.wait(), timeout=5)
    assert runner._is_session_running(session_key) is False  # pre-claim window
    assert runner._session_state(session_key).conversation.model_override is None

    # A host commit lands in the window. It must not see the MoA override.
    result = await runner.apply_session_options(source, {"reasoning_effort": "high"})
    assert result["status"] == "accepted", result
    durable = store.get_runtime_options(session_key)
    assert durable["model_override"] is None, durable
    assert durable["reasoning_override"] == {"enabled": True, "effort": "high"}

    release_seam.set()
    assert await asyncio.wait_for(turn, timeout=5) == "OK"

    # The turn itself still ran through MoA, and restored the prior override.
    assert seen_models and seen_models[0]["provider"] == "moa"
    conv = runner._session_state(session_key).conversation
    assert conv.model_override is None
    assert conv.one_turn_restore is None
    assert conv.reasoning_override == {"enabled": True, "effort": "high"}
    assert store.get_runtime_options(session_key)["model_override"] is None
    assert runner._is_session_running(session_key) is False
    # Every /moa install is paired with its restore eviction.
    assert runner.evictions.count(session_key) >= 2


@pytest.mark.asyncio
async def test_host_commit_after_claim_is_rejected_busy_while_moa_turn_runs(store, monkeypatch):
    """Once the turn is claimed and MoA installed, a host commit is busy-rejected
    rather than persisting the live MoA override."""
    runner, source, session_key = _wire_runner(store, monkeypatch, "moa-chat-2")

    in_turn = asyncio.Event()
    finish_turn = asyncio.Event()

    async def _fake_run(event, source, _quick_key, run_generation):
        assert runner._session_state(_quick_key).conversation.model_override["provider"] == "moa"
        in_turn.set()
        await finish_turn.wait()
        return "OK"

    runner._handle_message_with_agent = _fake_run

    turn = asyncio.create_task(runner._handle_message(_moa_event(source)))
    await asyncio.wait_for(in_turn.wait(), timeout=5)

    result = await runner.apply_session_options(source, {"reasoning_effort": "high"})
    assert result["status"] == "rejected" and result["code"] == "session_busy", result
    durable = store.get_runtime_options(session_key)
    assert (durable or {}).get("model_override") is None, durable

    finish_turn.set()
    assert await asyncio.wait_for(turn, timeout=5) == "OK"
    assert runner._session_state(session_key).conversation.model_override is None


async def _park_in_post_turn_window(runner, source):
    """Start a /moa turn and park it after the running slot is released but
    before ``_handle_message``'s ``finally`` restores the override."""
    agent_done = asyncio.Event()
    release_post = asyncio.Event()
    seen_models: list = []

    async def _fake_run(event, source, _quick_key, run_generation):
        seen_models.append(runner._session_state(_quick_key).conversation.model_override)
        # Mimic _run_agent_inner's finally: the running slot is released here,
        # then _handle_message_with_agent keeps awaiting (transcript appends,
        # update_session, sends) before returning to _handle_message's finally.
        runner._release_running_agent_state(_quick_key, run_generation=run_generation)
        agent_done.set()
        await release_post.wait()
        return "OK"

    runner._handle_message_with_agent = _fake_run
    turn = asyncio.create_task(runner._handle_message(_moa_event(source)))
    await asyncio.wait_for(agent_done.wait(), timeout=5)
    assert seen_models and seen_models[0]["provider"] == "moa"
    return turn, release_post


@pytest.mark.asyncio
async def test_host_commit_in_moa_post_turn_window_persists_prior_model(store, monkeypatch):
    """Slot released, override still live: a commit that does not name a model
    persists the parked pre-MoA snapshot, and the restore leaves live == disk."""
    runner, source, session_key = _wire_runner(store, monkeypatch, "moa-chat-3")
    turn, release_post = await _park_in_post_turn_window(runner, source)
    assert runner._is_session_running(session_key) is False  # post-turn, pre-restore
    assert runner._session_state(session_key).conversation.model_override["provider"] == "moa"

    result = await runner.apply_session_options(source, {"reasoning_effort": "high"})
    assert result["status"] == "accepted", result
    durable = store.get_runtime_options(session_key)
    assert durable["model_override"] is None, durable
    assert durable["reasoning_override"] == {"enabled": True, "effort": "high"}

    release_post.set()
    assert await asyncio.wait_for(turn, timeout=5) == "OK"
    conv = runner._session_state(session_key).conversation
    assert conv.model_override is None
    assert conv.one_turn_restore is None
    assert store.get_runtime_options(session_key)["model_override"] is None


@pytest.mark.asyncio
async def test_explicit_model_commit_in_moa_post_turn_window_is_not_reverted(store, monkeypatch):
    """A commit that names a model in the post-turn window wins: it clears the
    parked snapshot, so the turn's restore does not revert it and live == disk."""
    runner, source, session_key = _wire_runner(store, monkeypatch, "moa-chat-4")
    turn, release_post = await _park_in_post_turn_window(runner, source)

    from gateway.session_options import commit_session_runtime_options

    new_override = {"provider": "openrouter", "model": "gpt-4", "base_url": "", "api_key": ""}
    assert await commit_session_runtime_options(
        runner, session_key, model_override=new_override, require_routing_entry=True
    )
    assert store.get_runtime_options(session_key)["model_override"] == {
        "provider": "openrouter", "model": "gpt-4",
    }

    release_post.set()
    assert await asyncio.wait_for(turn, timeout=5) == "OK"
    conv = runner._session_state(session_key).conversation
    assert conv.model_override == new_override
    assert conv.one_turn_restore is None
    assert store.get_runtime_options(session_key)["model_override"] == {
        "provider": "openrouter", "model": "gpt-4",
    }


@pytest.mark.asyncio
async def test_moa_refused_by_drain_gate_leaves_no_live_override(store, monkeypatch):
    """``/moa`` that is turned away between dispatch and the claim (here: the
    gateway is draining) must not leave the MoA override live: the install
    only happens once the turn is actually admitted."""
    runner, adapter = make_restart_runner()
    runner.session_store = store
    runner._session_db = None
    runner._session_options_locks = {}
    source = make_restart_source(chat_id="moa-chat-5")
    session_key = runner._session_key_for_source(source)

    runner._handle_message = GatewayRunner._handle_message.__get__(runner, GatewayRunner)
    runner._check_slash_access = lambda *a, **k: None
    runner._is_user_authorized = lambda _source: True
    runner.evictions = []
    runner._evict_cached_agent = lambda key: runner.evictions.append(key)
    runner._handle_message_with_agent = AsyncMock(return_value="OK")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    adapter.set_message_handler(runner._handle_message)
    adapter._run_processing_hook = AsyncMock()
    runner._draining = True

    reply = await runner._handle_message(_moa_event(source))
    assert reply and "not accepting new work" in reply
    runner._handle_message_with_agent.assert_not_awaited()
    assert runner._session_state(session_key).conversation.model_override is None
    assert runner._session_state(session_key).conversation.one_turn_restore is None
