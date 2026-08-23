"""Rejection paths of apply_session_options (AI review point 1 on #92187).

Each test asserts only ``status`` + ``code`` (the API contract), never the
human-readable ``error`` text, so none of these are change-detector tests.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)
    s = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    assert s._db is None
    return s


def _make_runner(store, model="anthropic/claude-sonnet-4"):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = store
    runner._session_options_locks = {}
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        model,
        {"provider": "openrouter", "base_url": "", "api_key": ""},
    )
    runner._evict_cached_agent = lambda _session_key: None
    runner._session_db = None
    return runner


# --- cheap: no store, no runner scaffolding beyond object.__new__ -----------


@pytest.mark.asyncio
async def test_unknown_option_key_is_rejected_before_any_state_is_touched(store):
    runner = _make_runner(store)
    result = await runner.apply_session_options(_make_source(), {"temperature": 1})
    assert (result["status"], result["code"]) == ("rejected", "invalid_options")
    # Nothing created: no routing entry, no SessionState, no lock.
    assert store.lookup_by_session_key(
        runner._session_key_for_source(_make_source())
    ) is None
    assert runner._session_options_locks == {}


# --- cheap: same scaffold as the existing happy-path tests ------------------


@pytest.mark.asyncio
async def test_provider_without_model_is_rejected(store):
    runner = _make_runner(store)
    result = await runner.apply_session_options(_make_source(), {"provider": "openai"})
    assert (result["status"], result["code"]) == ("rejected", "invalid_options")
    assert store.lookup_by_session_key(result.get("session_key", "")) is None


@pytest.mark.asyncio
async def test_reasoning_effort_unknown_value_is_rejected(store):
    runner = _make_runner(store)
    result = await runner.apply_session_options(
        _make_source(), {"reasoning_effort": "galactic"}
    )
    assert (result["status"], result["code"]) == ("rejected", "reasoning_rejected")


@pytest.mark.asyncio
async def test_fast_non_bool_is_rejected(store):
    runner = _make_runner(store)
    result = await runner.apply_session_options(_make_source(), {"fast": "yes"})
    assert (result["status"], result["code"]) == ("rejected", "fast_rejected")


@pytest.mark.asyncio
async def test_fast_unsupported_for_model_is_rejected(store):
    # A model that resolve_fast_mode_overrides does not know.
    runner = _make_runner(store, model="someorg/no-fast-tier-model")
    result = await runner.apply_session_options(_make_source(), {"fast": True})
    assert (result["status"], result["code"]) == ("rejected", "fast_unsupported")


@pytest.mark.asyncio
async def test_rejected_patch_persists_nothing_even_when_one_field_was_valid(store):
    """Atomicity: a valid reasoning value + invalid fast value writes nothing."""
    runner = _make_runner(store)
    source = _make_source()
    result = await runner.apply_session_options(
        source, {"reasoning_effort": "high", "fast": "yes"}
    )
    assert result["status"] == "rejected"
    key = runner._session_key_for_source(source)
    assert store.lookup_by_session_key(key) is None
    assert runner._peek_session_state(key) is None or (
        runner._peek_session_state(key).conversation.reasoning_override is None
    )


# --- cheap-ish: needs the admission sentinel -------------------------------


@pytest.mark.asyncio
async def test_session_busy_is_rejected_and_persists_nothing(store):
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner = _make_runner(store)
    source = _make_source()
    key = runner._session_key_for_source(runner._normalize_source_for_session_key(source))
    runner._session_state(key).turn.agent = _AGENT_PENDING_SENTINEL

    result = await runner.apply_session_options(source, {"reasoning_effort": "high"})
    assert (result["status"], result["code"]) == ("rejected", "session_busy")
    assert store.lookup_by_session_key(key) is None
    assert runner._session_state(key).conversation.reasoning_override is None


# --- awkward: session_missing requires faking the store's False return ------


@pytest.mark.asyncio
async def test_session_missing_when_entry_vanishes_between_create_and_persist(
    store, monkeypatch
):
    runner = _make_runner(store)
    source = _make_source()
    key = runner._session_key_for_source(runner._normalize_source_for_session_key(source))

    real_create = store.get_or_create_session

    def _create_then_vanish(src):
        entry = real_create(src)
        # Simulate /new or expiry removing the routing entry mid-flight.
        with store._lock:
            store._entries.pop(key, None)
        return entry

    monkeypatch.setattr(store, "get_or_create_session", _create_then_vanish)
    result = await runner.apply_session_options(source, {"reasoning_effort": "high"})
    assert (result["status"], result["code"]) == ("rejected", "session_missing")
    assert runner._session_state(key).conversation.reasoning_override is None


# --- multiplex: the API runs the same fail-closed profile-routing gate -------
# as the inbound message path (#92185 "normal routing path").


@pytest.mark.asyncio
async def test_explicitly_rejected_profile_route_is_refused(store):
    runner = _make_runner(store)
    source = _make_source()
    source.profile_route_rejected = True
    result = await runner.apply_session_options(source, {"reasoning_effort": "high"})
    assert (result["status"], result["code"]) == ("rejected", "invalid_session")
    assert store.list_sessions() == []


@pytest.mark.asyncio
async def test_unstamped_source_is_routed_and_refused_when_route_targets_unserved_profile(store):
    from gateway.profile_routing import ProfileRouteRejected

    runner = _make_runner(store)
    runner.config = SimpleNamespace(multiplex_profiles=True)

    def _route(_source):
        raise ProfileRouteRejected("unserved")

    runner._profile_name_for_source = _route
    source = _make_source()
    result = await runner.apply_session_options(source, {"reasoning_effort": "high"})
    assert (result["status"], result["code"]) == ("rejected", "invalid_session")
    assert source.profile_route_rejected is True
    assert store.list_sessions() == []


# --- durable write failure: structured rejection, live state untouched ------


@pytest.mark.asyncio
async def test_durable_write_failure_is_a_rejection_not_an_exception(
    store, monkeypatch
):
    runner = _make_runner(store)
    source = _make_source()
    key = runner._session_key_for_source(runner._normalize_source_for_session_key(source))
    # Routing entry exists and is healthy; only the later options write fails.
    store.get_or_create_session(source)

    def _fail(_data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_save_sessions_json", _fail)

    result = await runner.apply_session_options(source, {"reasoning_effort": "high"})

    assert (result["status"], result["code"]) == ("rejected", "durable_write_failed")
    assert runner._session_state(key).conversation.reasoning_override is None
    assert (store.get_runtime_options(key) or {}).get("reasoning_override") is None
    # Nothing is stuck: once the disk is healthy again the same patch lands.
    monkeypatch.undo()
    retry = await runner.apply_session_options(source, {"reasoning_effort": "high"})
    assert retry["status"] == "accepted"
    assert retry["applied"] == ["reasoning_effort"]
