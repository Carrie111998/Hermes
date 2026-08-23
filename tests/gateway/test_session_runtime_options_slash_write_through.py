"""Later user-issued slash commands must stay durable/authoritative (#92185).

Regression for review finding #1 on PR #92187. On 1ddc941c
``apply_session_options()`` persisted *before* mutating live state, but the
sibling ``/reasoning`` and ``/fast`` seams mutated live state first and then
called a ``_persist_session_runtime_options()`` helper that swallowed every
store failure. Both seams now go through ``commit_session_runtime_options``.

Contract under test (both halves):
  * after a slash change whose durable write fails, live state and durable
    state agree (either the command did not advance live state, or it failed
    explicitly) -- never "live low / durable high"; and a success reply is
    only acceptable if the value actually advanced (no "replied OK, changed
    nothing");
  * a simulated restart rehydrates the same value the live gateway was using.

The save failure is injected at ``SessionStore._save_sessions_json`` so the
exception travels through the real ``_persist_routing_data`` raise path
(no state.db in this fixture => the sessions.json failure is fatal).
"""
from __future__ import annotations

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source())


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """SessionStores over one shared sessions dir, without SQLite."""

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)

    def _make() -> SessionStore:
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        assert store._db is None
        return store

    return _make


def _make_runner(store: SessionStore) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = GatewayConfig()
    runner.session_store = store
    runner._session_options_locks = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._service_tier = None
    runner._show_reasoning = False
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "gpt-5.4",
        {"provider": "openai", "base_url": "", "api_key": ""},
    )
    runner._evict_cached_agent = lambda _session_key: None
    return runner


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path / "home")
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4"
    )


def _live(runner: GatewayRunner, session_key: str) -> dict:
    state = runner._session_state(session_key)
    tier = state.conversation.service_tier_override
    return {
        "reasoning_override": state.conversation.reasoning_override,
        "service_tier_override": (
            None
            if tier is gateway_run._SERVICE_TIER_UNSET
            else ("priority" if tier == "priority" else "normal")
        ),
    }


def _durable(store: SessionStore, session_key: str) -> dict:
    opts = store.get_runtime_options(session_key) or {}
    return {
        "reasoning_override": opts.get("reasoning_override"),
        "service_tier_override": opts.get("service_tier_override"),
    }


async def _seed_host_defaults(runner: GatewayRunner, store: SessionStore) -> str:
    result = await runner.apply_session_options(
        _make_source(),
        {"reasoning_effort": "high", "fast": True, "initial": True},
    )
    assert result["status"] == "accepted", result
    session_key = result["session_key"]
    assert _durable(store, session_key) == {
        "reasoning_override": {"enabled": True, "effort": "high"},
        "service_tier_override": "priority",
    }
    assert _live(runner, session_key) == _durable(store, session_key)
    return session_key


def _arm_save_failure(monkeypatch, store: SessionStore) -> list:
    attempts: list = []

    def _fail(_data):
        attempts.append(1)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_save_sessions_json", _fail)
    return attempts


def _restart(store_factory, session_key: str) -> dict:
    """Fresh store + fresh runner over the same sessions dir."""
    runner = _make_runner(store_factory())
    runner._rehydrate_session_runtime_options(session_key)
    return _live(runner, session_key)


@pytest.mark.asyncio
async def test_reasoning_slash_stays_consistent_when_durable_write_fails(
    store_factory, monkeypatch, env
):
    store = store_factory()
    runner = _make_runner(store)
    session_key = await _seed_host_defaults(runner, store)

    attempts = _arm_save_failure(monkeypatch, store)

    seeded = _live(runner, session_key)
    try:
        reply = await runner._handle_reasoning_command(_make_event("/reasoning low"))
    except OSError:
        reply = None  # explicit failure is an acceptable outcome

    assert attempts, "slash change never attempted a durable write"
    live = _live(runner, session_key)
    durable = _durable(store, session_key)
    assert live == durable, (
        f"live state diverged from durable state after a failed save: "
        f"live={live!r} durable={durable!r} reply={reply!r}"
    )
    if reply is not None:
        assert live != seeded, f"replied {reply!r} but changed nothing"

    # Restart must resurrect exactly what the live gateway was using.
    assert _restart(store_factory, session_key) == live


@pytest.mark.asyncio
async def test_fast_slash_stays_consistent_when_durable_write_fails(
    store_factory, monkeypatch, env
):
    store = store_factory()
    runner = _make_runner(store)
    session_key = await _seed_host_defaults(runner, store)

    attempts = _arm_save_failure(monkeypatch, store)

    seeded = _live(runner, session_key)
    try:
        reply = await runner._handle_fast_command(_make_event("/fast normal"))
    except OSError:
        reply = None

    assert attempts, "slash change never attempted a durable write"
    live = _live(runner, session_key)
    durable = _durable(store, session_key)
    assert live == durable, (
        f"live state diverged from durable state after a failed save: "
        f"live={live!r} durable={durable!r} reply={reply!r}"
    )
    if reply is not None:
        assert live != seeded, f"replied {reply!r} but changed nothing"
    assert _restart(store_factory, session_key) == live


@pytest.mark.asyncio
async def test_slash_write_through_succeeds_on_healthy_store(
    store_factory, monkeypatch, env
):
    """Control: with a healthy store the slash edit is durable and authoritative."""
    store = store_factory()
    runner = _make_runner(store)
    session_key = await _seed_host_defaults(runner, store)

    await runner._handle_reasoning_command(_make_event("/reasoning low"))
    await runner._handle_fast_command(_make_event("/fast normal"))

    expected = {
        "reasoning_override": {"enabled": True, "effort": "low"},
        "service_tier_override": "normal",
    }
    assert _live(runner, session_key) == expected
    assert _durable(store, session_key) == expected
    assert _restart(store_factory, session_key) == expected
