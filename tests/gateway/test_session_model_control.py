"""Live gateway model mutation through the internal control socket."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.run import (
    GatewayRunner,
    _AGENT_PENDING_SENTINEL,
    _build_set_session_model_handler,
)
from gateway.session_state import SessionState


class _LoopThread:
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *_exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


def _runner(*, db_error: Exception | None = None):
    state = SessionState()
    store = MagicMock()
    store.has_session.return_value = True
    store.get_session_id.return_value = "transcript-42"
    store.set_model_override.return_value = True
    update = AsyncMock()
    if db_error is not None:
        update.side_effect = db_error
    runner = SimpleNamespace(
        session_store=store,
        _session_db=SimpleNamespace(update_session_model=update),
        _session_state=lambda _key: state,
        _defer_or_evict_session_model_cache=MagicMock(return_value=True),
    )
    return runner, state, store, update


def test_handler_updates_store_state_db_and_defers_running_eviction(monkeypatch):
    runner, state, store, update = _runner()
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda _provider: {
            "provider": "openai-codex",
            "api_key": "resolved-secret",
            "api_mode": "responses",
        },
    )

    with _LoopThread() as loop:
        result = _build_set_session_model_handler(runner, loop)({
            "session_key": "agent:main:telegram:dm:42",
            "model": "gpt-5.6-sol",
            "provider": "openai-codex",
        })

    override = store.set_model_override.call_args.args[1]
    assert override["model"] == "gpt-5.6-sol"
    assert override["provider"] == "openai-codex"
    assert override["api_key"] == "resolved-secret"
    assert state.conversation.model_override == override
    update.assert_awaited_once_with(
        "transcript-42", "gpt-5.6-sol", provider="openai-codex"
    )
    runner._defer_or_evict_session_model_cache.assert_called_once_with(
        "agent:main:telegram:dm:42"
    )
    assert result["applied"] is True
    assert result["eviction_deferred"] is True
    assert result["durability_warning"] == ""


def test_handler_surfaces_durable_write_failure_without_reverting_live_state(
    monkeypatch,
):
    runner, state, store, _update = _runner(db_error=OSError("database busy"))
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda _provider: {"provider": "openai-codex"},
    )

    with _LoopThread() as loop:
        result = _build_set_session_model_handler(runner, loop)({
            "session_key": "agent:main:telegram:dm:42",
            "model": "gpt-5.6-sol",
            "provider": "openai-codex",
        })

    assert store.set_model_override.called
    assert state.conversation.model_override["model"] == "gpt-5.6-sol"
    assert result["applied"] is True
    assert "saved transcript record could not be confirmed" in result[
        "durability_warning"
    ]


def test_handler_deadline_expires_before_any_mutation(monkeypatch):
    runner, _state, store, update = _runner()
    monkeypatch.setattr("gateway.run._SESSION_MODEL_APPLY_TIMEOUT", 0.0)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda _provider: {"provider": "openai-codex"},
    )

    with _LoopThread() as loop:
        handler = _build_set_session_model_handler(runner, loop)
        with pytest.raises(TimeoutError, match="did not begin"):
            handler({
                "session_key": "agent:main:telegram:dm:42",
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
            })

    store.set_model_override.assert_not_called()
    update.assert_not_awaited()


def test_running_session_cache_eviction_waits_for_turn_release():
    runner = object.__new__(GatewayRunner)
    runner.__dict__["_sessions"] = {}
    runner._agent_cache = {
        "agent:main:telegram:dm:42": (_AGENT_PENDING_SENTINEL, 0.0)
    }
    runner._agent_cache_lock = threading.RLock()
    runner._persist_active_agents = MagicMock()
    state = runner._session_state("agent:main:telegram:dm:42")
    state.turn.agent = _AGENT_PENDING_SENTINEL

    assert runner._defer_or_evict_session_model_cache(
        "agent:main:telegram:dm:42"
    ) is True
    assert "agent:main:telegram:dm:42" in runner._agent_cache
    assert state.conversation.model_cache_evict_pending is True

    assert runner._release_running_agent_state(
        "agent:main:telegram:dm:42"
    ) is True
    assert "agent:main:telegram:dm:42" not in runner._agent_cache
    assert state.conversation.model_cache_evict_pending is False
