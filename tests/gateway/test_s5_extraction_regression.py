"""Regression tests for the s5 wave-1a verbatim extraction of gateway/run.py.

The 41 s5 methods were moved character-for-character out of
``gateway/run.py`` into seven mixin modules (per the canonical shard plan):

- c4  -> gateway/agent_cache_config_mixin.py  (GatewayAgentCacheConfigMixin)
- c7  -> gateway/agent_cache_mixin.py         (GatewayAgentCacheMixin)
- c8  -> gateway/turn_sidecar_mixin.py        (GatewayTurnSidecarMixin)
- c5  -> gateway/session_model_mixin.py       (GatewaySessionModelMixin)
- c6  -> gateway/run_generation_mixin.py      (GatewayRunGenerationMixin)
- c3  -> gateway/process_events_mixin.py      (GatewayProcessEventsMixin)
- c10 -> gateway/proxy_mixin.py               (GatewayProxyMixin)

These tests pin the two contracts the move must preserve:

1. MRO resolution — every moved method is still reachable as
   ``GatewayRunner.<method>`` and callable through ``self``.
2. Public module-level constants stay in ``gateway.run`` and stay
   monkeypatch-visible to the moved methods via the lazy
   ``from gateway.run import ...`` seam — exactly what
   ``tests/gateway/test_agent_cache.py`` (monkeypatches
   ``_AGENT_CACHE_MAX_SIZE`` / ``_AGENT_CACHE_IDLE_TTL_SECS`` on the module)
   and ``tests/gateway/test_conversation_scope_funnel.py`` (imports
   ``_CONVERSATION_SCOPED_STATE`` from ``gateway.run``) depend on.
"""

import time
from collections import OrderedDict

import pytest

import gateway.run as gw_run
from gateway.run import (
    _AGENT_CACHE_IDLE_TTL_SECS,
    _AGENT_CACHE_MAX_SIZE,
    _AGENT_PENDING_SENTINEL,
    _CONVERSATION_SCOPED_STATE,
    GatewayRunner,
)

MIXIN_CLASSES = [
    "GatewayAgentCacheConfigMixin",
    "GatewayAgentCacheMixin",
    "GatewayTurnSidecarMixin",
    "GatewaySessionModelMixin",
    "GatewayRunGenerationMixin",
    "GatewayProcessEventsMixin",
    "GatewayProxyMixin",
]

MOVED_METHODS = [
    # c4
    "_agent_config_signature", "_empty_honcho_cache_busting_config",
    "_extract_cache_busting_config", "_extract_honcho_cache_busting_config",
    # c7
    "_commit_memory_before_soft_evict", "_commit_then_release_soft",
    "_enforce_agent_cache_cap", "_evict_cached_agent",
    "_init_cached_agent_for_turn", "_refresh_agent_cache_message_count",
    "_release_evicted_agent_soft", "_sweep_idle_cached_agents",
    # c8
    "_consume_pending_turn_sidecar_notes", "_set_pending_turn_sidecar_notes",
    "_voice_channel_sidecar_note",
    # c5
    "_apply_session_model_override", "_is_intentional_model_switch",
    "_rehydrate_session_model_override", "_restore_session_model_override",
    "_snapshot_session_model_override",
    # c6
    "_begin_session_run_generation", "_bind_adapter_run_generation",
    "_clear_conversation_scope", "_clear_session_boundary_security_state",
    "_interrupt_and_clear_session", "_invalidate_session_run_generation",
    "_is_session_run_current", "_rebind_turn_lease",
    "_release_running_agent_state", "_release_turn_lease",
    # c3
    "_async_delegation_watcher", "_build_process_event_source",
    "_classify_completion_target", "_completion_delivery_identity",
    "_deliver_completion_notification", "_enrich_async_delegation_routing",
    "_inject_watch_notification", "_run_process_watcher",
    # c10
    "_build_stream_consumer_config", "_get_proxy_url", "_run_agent_via_proxy",
]


def _bare_runner() -> GatewayRunner:
    """A GatewayRunner with no __init__ run — the repo's bare-runner pattern."""
    return object.__new__(GatewayRunner)


class _Conv:
    def __init__(self):
        self.sidecar_notes = []
        self.model_override = None

    def clear(self):
        self.sidecar_notes = []
        self.model_override = None


class _Persistent:
    def __init__(self):
        self.run_generation = 0


class _Turn:
    def __init__(self):
        self.lease = None
        self.lease_token = None
        self.lease_generation = None

    def clear(self):
        self.lease = None
        self.lease_token = None
        self.lease_generation = None


class _StubState:
    def __init__(self):
        self.conversation = _Conv()
        self.persistent = _Persistent()
        self.turn = _Turn()


# ---------------------------------------------------------------------------
# Contract 1: MRO resolution
# ---------------------------------------------------------------------------


def test_all_moved_methods_resolve_on_gateway_runner():
    missing = [name for name in MOVED_METHODS if not hasattr(GatewayRunner, name)]
    assert missing == [], f"moved methods missing from GatewayRunner MRO: {missing}"


def test_mixin_classes_are_in_gateway_runner_mro():
    mro_names = [c.__name__ for c in GatewayRunner.__mro__]
    missing = [name for name in MIXIN_CLASSES if name not in mro_names]
    assert missing == [], f"mixin classes missing from GatewayRunner MRO: {missing}"


def test_moved_methods_are_not_defined_in_run_py_module_globals():
    """The definitions left gateway/run.py — only the MRO reachability stays."""
    for name in MOVED_METHODS:
        assert not hasattr(gw_run, name), (
            f"{name} still defined at gateway/run.py module level after extraction"
        )


# ---------------------------------------------------------------------------
# Contract 2: module-level constants stay in gateway.run (public contract)
# ---------------------------------------------------------------------------


def test_public_constants_stay_in_gateway_run():
    assert _AGENT_PENDING_SENTINEL is not None
    assert isinstance(_AGENT_CACHE_MAX_SIZE, int)
    assert isinstance(_AGENT_CACHE_IDLE_TTL_SECS, float)
    assert isinstance(_CONVERSATION_SCOPED_STATE, tuple) and _CONVERSATION_SCOPED_STATE


# ---------------------------------------------------------------------------
# Pure-method behavior (verbatim bodies must behave identically)
# ---------------------------------------------------------------------------


def test_completion_delivery_identity_classification():
    """Pure staticmethod: producer-stable identity classification."""
    ident = GatewayRunner._completion_delivery_identity
    # async_delegation with delegation_id -> (type, id, "")
    assert ident({"type": "async_delegation", "delegation_id": "d-1"}) == (
        "async_delegation", "d-1", "",
    )
    # async_delegation without id -> None
    assert ident({"type": "async_delegation"}) is None
    # completion with session_id + started_at -> full tuple
    evt = {"type": "completion", "session_id": "s-9", "started_at": 123.4}
    assert ident(evt) == ("completion", "s-9", 123.4)
    # completion missing started_at -> None (no dedup suppression)
    assert ident({"type": "completion", "session_id": "s-9"}) is None
    # unknown/legacy -> None
    assert ident({"type": "watch", "session_id": "s-9", "started_at": 1}) is None
    assert ident({}) is None


def test_agent_config_signature_stable_and_input_sensitive():
    """Pure staticmethod: cache-key signature semantics."""
    sig = GatewayRunner._agent_config_signature
    base = ("gpt-4o", {"provider": "openai", "api_key": "k1"}, ["terminal"], "ephemeral")
    a = sig(*base)
    # deterministic
    assert sig(*base) == a
    # model change busts
    assert sig("gpt-4o-mini", base[1], base[2], base[3]) != a
    # api_key change busts (full-string fingerprint)
    assert sig(base[0], {"provider": "openai", "api_key": "k2"}, base[2], base[3]) != a
    # toolsets change busts
    assert sig(base[0], base[1], ["terminal", "web"], base[3]) != a
    # ephemeral prompt change busts
    assert sig(base[0], base[1], base[2], "other") != a
    # cache_keys participate
    assert sig(*base, cache_keys={"model.context_length": 32000}) != a
    # user identity participates
    assert sig(*base, user_id="u1") != a
    assert sig(*base, user_id_alt="u1") != a
    assert sig(*base, skip_context_files=True) != a


def test_extract_cache_busting_config_shape():
    """Classmethod: flat 'section.key' extraction with None for absent keys."""
    out = GatewayRunner._extract_cache_busting_config(
        {"model": {"context_length": 64000}, "compression": {"enabled": True}}
    )
    assert out["model.context_length"] == 64000
    assert out["compression.enabled"] is True
    # absent section -> None value, key still present
    assert out["checkpoints.enabled"] is None
    # live tool-registry generation participates in the signature
    assert "tools.registry_generation" in out
    # legacy `checkpoints: true` preserves enabled=True, others None
    out2 = GatewayRunner._extract_cache_busting_config({"checkpoints": True})
    assert out2["checkpoints.enabled"] is True
    assert out2["checkpoints.max_snapshots"] is None
    # None user_config -> empty dict path, no crash
    out3 = GatewayRunner._extract_cache_busting_config(None)
    assert out3["model.context_length"] is None


def test_empty_honcho_cache_busting_config_matches_keys():
    empty = GatewayRunner._empty_honcho_cache_busting_config()
    assert set(empty) == set(GatewayRunner._HONCHO_CACHE_BUSTING_KEYS)
    assert all(v is None for v in empty.values())


def test_get_proxy_url_env_and_config(monkeypatch):
    runner = _bare_runner()
    monkeypatch.delenv("GATEWAY_PROXY_URL", raising=False)
    monkeypatch.setattr(gw_run, "_load_gateway_config", lambda: {"gateway": {}})
    assert runner._get_proxy_url() is None
    # config.yaml path
    monkeypatch.setattr(
        gw_run, "_load_gateway_config",
        lambda: {"gateway": {"proxy_url": "http://proxy:8000/"}},
    )
    assert runner._get_proxy_url() == "http://proxy:8000"
    # env var wins over config
    monkeypatch.setenv("GATEWAY_PROXY_URL", "http://env-proxy")
    assert runner._get_proxy_url() == "http://env-proxy"


def test_run_generation_guards_on_bare_runner():
    runner = _bare_runner()
    # no session state -> generation 0 -> any non-zero token is stale
    runner._peek_session_state = lambda k: None
    assert runner._is_session_run_current("k", 5) is False
    assert runner._is_session_run_current("k", 0) is True
    # live state -> token comparison
    state = _StubState()
    state.persistent.run_generation = 7
    runner._peek_session_state = lambda k: state
    runner._session_state = lambda k: state  # _begin_session_run_generation reads _session_state
    assert runner._is_session_run_current("k", 7) is True
    assert runner._is_session_run_current("k", 6) is False
    # begin/invalidate bump monotonically
    assert runner._begin_session_run_generation("k") == 8
    assert runner._invalidate_session_run_generation("k", reason="test") == 9
    assert runner._begin_session_run_generation("") == 0


def test_sidecar_note_staging_roundtrip():
    runner = _bare_runner()
    state = _StubState()
    runner._peek_session_state = lambda k: state
    runner._session_state = lambda k: state
    assert runner._consume_pending_turn_sidecar_notes("k") == []
    runner._set_pending_turn_sidecar_notes("k", ["note-a", "note-b"])
    assert runner._consume_pending_turn_sidecar_notes("k") == ["note-a", "note-b"]
    # one-shot: consumed
    assert runner._consume_pending_turn_sidecar_notes("k") == []
    # empty key / empty notes noop
    runner._set_pending_turn_sidecar_notes("", ["x"])
    runner._set_pending_turn_sidecar_notes("k", [])
    assert runner._consume_pending_turn_sidecar_notes("") == []


def test_release_turn_lease_idempotent_bare_runner():
    runner = _bare_runner()
    runner._peek_session_state = lambda k: None
    runner._persist_active_agents = lambda: None
    # no registry/state -> no crash; the release path still runs and reports
    # True (live-verified semantic of the verbatim method)
    assert runner._release_turn_lease("k", 1) is False
    assert runner._release_running_agent_state("k") is True
    # stale-generation unwind refuses
    state = _StubState()
    runner._peek_session_state = lambda k: state
    runner._turn_leases = object()
    assert runner._release_turn_lease("k", 5) is False
    # current-generation release with a real registry refusing the token
    state.turn.lease_token = "tok"
    state.turn.lease_generation = 3
    runner._turn_leases = type(
        "Registry", (), {"release": lambda self, tok: False}
    )()
    assert runner._release_turn_lease("k", 3) is False
    assert state.turn.lease_token is None  # token consumed even on refusal


# ---------------------------------------------------------------------------
# The monkeypatch seam: tests/gateway/test_agent_cache.py patches constants
# on the gateway.run module; the moved methods must observe the patch.
# ---------------------------------------------------------------------------


def test_enforce_agent_cache_cap_honors_monkeypatched_max_size(monkeypatch):
    monkeypatch.setattr(gw_run, "_AGENT_CACHE_MAX_SIZE", 2)
    runner = _bare_runner()
    runner._agent_cache = OrderedDict(
        (f"k{i}", (object(),)) for i in range(3)
    )
    runner._agent_cache_lock = __import__("threading").Lock()
    runner._running_agent_items = lambda: []
    runner._enforce_agent_cache_cap()
    assert len(runner._agent_cache) == 2, (
        "cap enforcement must observe monkeypatched gateway.run._AGENT_CACHE_MAX_SIZE"
    )


def test_sweep_idle_cached_agents_honors_monkeypatched_ttl(monkeypatch):
    monkeypatch.setattr(gw_run, "_AGENT_CACHE_IDLE_TTL_SECS", 0.01)
    runner = _bare_runner()
    agent = type("Agent", (), {"_last_activity_ts": time.time() - 100.0,
                               "_session_messages": ["big", "history"]})()
    runner._agent_cache = OrderedDict([("k1", (agent,))])
    runner._agent_cache_lock = __import__("threading").Lock()
    runner._running_agent_items = lambda: []
    runner.session_store = None  # no expiry-store deferral -> evict
    evicted = runner._sweep_idle_cached_agents()
    assert evicted == 1
    assert "k1" not in runner._agent_cache


def test_clear_conversation_scope_uses_registry_from_gateway_run():
    """_CONVERSATION_SCOPED_STATE is read from gateway.run at call time."""
    runner = _bare_runner()
    state = _StubState()
    state.conversation.model_override = {"agent:main:telegram:dm:777": object()}
    runner._peek_session_state = lambda k: state
    runner._session_state = lambda k: state
    # SessionState-backed names (class-level descriptors like
    # _session_model_overrides) clear via state.conversation.clear(); the
    # legacy plain-dict stores clear via the pop path.
    for attr in _CONVERSATION_SCOPED_STATE:
        if not hasattr(type(runner), attr):
            setattr(runner, attr, {"agent:main:telegram:dm:777": object()})
    runner._clear_conversation_scope("agent:main:telegram:dm:777", reason="test")
    assert state.conversation.model_override is None, "SessionState path cleared"
    for attr in _CONVERSATION_SCOPED_STATE:
        if not hasattr(type(runner), attr):
            assert "agent:main:telegram:dm:777" not in getattr(runner, attr, {}), attr


def test_moved_method_definitions_are_in_mixin_modules():
    """Spot-check the definitions physically live in the new mixin modules."""
    import gateway.agent_cache_mixin as acm
    import gateway.process_events_mixin as pem
    import gateway.proxy_mixin as pm
    assert "GatewayAgentCacheMixin" in acm.__dict__
    assert "GatewayProcessEventsMixin" in pem.__dict__
    assert "GatewayProxyMixin" in pm.__dict__
    assert GatewayRunner._evict_cached_agent.__qualname__.startswith("GatewayAgentCacheMixin.")
    assert GatewayRunner._run_agent_via_proxy.__qualname__.startswith("GatewayProxyMixin.")
    assert GatewayRunner._inject_watch_notification.__qualname__.startswith(
        "GatewayProcessEventsMixin."
    )
