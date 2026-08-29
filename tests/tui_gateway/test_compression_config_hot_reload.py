"""Desktop/TUI sessions must adopt live compression config on the next turn.

Regression for #95151: ``_sync_agent_model_with_config`` only compared the
model/provider. After ``hermes config set compression.threshold_tokens 100000``
the already-open session kept the computed threshold from agent creation.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.context_compressor import ContextCompressor
from tui_gateway import server


def _session_with_compressor(**compression_ctor):
    compressor = ContextCompressor(
        model="gpt-5.6-sol",
        threshold_percent=0.85,
        config_context_length=272_000,
        quiet_mode=True,
        **compression_ctor,
    )
    agent = SimpleNamespace(
        model="gpt-5.6-sol",
        provider="openai-codex",
        context_compressor=compressor,
        compression_enabled=True,
        compression_idle_compact_after_seconds=0,
    )
    return {
        "agent": agent,
        "session_key": "session-95151",
    }, compressor


def test_live_threshold_tokens_applies_on_next_turn_without_rebuild(monkeypatch):
    session, compressor = _session_with_compressor()
    stale = compressor.threshold_tokens
    assert stale > 100_000

    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
                "context_length": 272_000,
            },
            "compression": {
                "threshold_tokens": 100_000,
                "proactive_prune_tokens": 48_000,
                "idle_compact_after_seconds": 1800,
                "tail_mode": "lean",
            },
        },
    )

    live_agent = session["agent"]
    server._sync_agent_compression_with_config("sid-95151", session)

    assert session["agent"] is live_agent
    assert compressor.threshold_tokens == 100_000
    assert compressor.proactive_prune_tokens == 48_000
    assert compressor.tail_mode == "lean"
    assert live_agent.compression_idle_compact_after_seconds == 1800


def test_unchanged_compression_config_is_noop(monkeypatch):
    session, compressor = _session_with_compressor(threshold_tokens_cap=100_000)
    cfg = {
        "model": {"context_length": 272_000},
        "compression": {"threshold_tokens": 100_000},
    }
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    session["config_compression_seen"] = server._tui_compression_config_signature(cfg)

    compressor.threshold_tokens = 99_999
    server._sync_agent_compression_with_config("sid-95151", session)

    assert compressor.threshold_tokens == 99_999


def test_provider_model_context_change_invalidates_live_compression_signature():
    def _cfg(context_length):
        return {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "relay",
                "base_url": "https://relay.example.test/v1",
            },
            "providers": {
                "relay": {
                    "api": "https://relay.example.test/v1",
                    "models": {
                        "gpt-5.6-sol": {"context_length": context_length},
                    },
                },
            },
            "compression": {},
        }

    before = server._tui_compression_config_signature(_cfg(256_000))
    after = server._tui_compression_config_signature(_cfg(192_000))

    assert before != after


def test_clearing_threshold_tokens_restores_ratio_trigger(monkeypatch):
    session, compressor = _session_with_compressor(threshold_tokens_cap=100_000)
    assert compressor.threshold_tokens == 100_000

    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "model": {"context_length": 272_000},
            "compression": {},
        },
    )
    server._sync_agent_compression_with_config("sid-95151", session)

    assert compressor.threshold_tokens > 100_000
    assert compressor.threshold_tokens_cap is None


def test_prompt_submit_calls_compression_sync_after_model_sync():
    source = open(server.__file__, encoding="utf-8").read()
    model_idx = source.find("_sync_agent_model_with_config(sid, session)")
    compression_idx = source.find("_sync_agent_compression_with_config(sid, session)")
    assert model_idx != -1
    assert compression_idx != -1
    assert model_idx < compression_idx


# ── Unset semantics (#94724 review finding on #95980) ────────────────────
# ``_apply_live_compression_config`` used to act only on PRESENT keys, so
# removing tail_mode / context_length / target_ratio / model_thresholds /
# proactive_prune_* / protect_last_n / min_tail_user_messages / threshold /
# idle_compact_after_seconds from config.yaml left stale values active in
# live sessions forever. Absence must restore the normalized default (or the
# model-derived value) through the same derivation the agent-construction
# path uses.


def _neutral_session(**compression_ctor):
    """Session on a model with no per-model threshold override in play."""
    compressor = ContextCompressor(
        model="unset-test-model",
        config_context_length=600_000,  # >=512K: no small-context floor
        quiet_mode=True,
        **compression_ctor,
    )
    agent = SimpleNamespace(
        model="unset-test-model",
        provider="",
        context_compressor=compressor,
        compression_enabled=True,
        compression_idle_compact_after_seconds=0,
    )
    return {"agent": agent, "session_key": "session-unset"}, compressor


def _sync_with_cfg(monkeypatch, session, cfg):
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    server._sync_agent_compression_with_config("sid-unset", session)


def test_removing_tail_mode_restores_lean_default(monkeypatch):
    session, compressor = _neutral_session(tail_mode="legacy")
    assert compressor.tail_mode == "legacy"
    _sync_with_cfg(monkeypatch, session, {"compression": {}})
    assert compressor.tail_mode == "lean"


def test_removing_target_ratio_restores_default(monkeypatch):
    session, compressor = _neutral_session(summary_target_ratio=0.60)
    assert compressor.summary_target_ratio == 0.60
    _sync_with_cfg(monkeypatch, session, {"compression": {}})
    assert compressor.summary_target_ratio == 0.20


def test_removing_protect_last_n_restores_default(monkeypatch):
    session, compressor = _neutral_session(protect_last_n=5)
    _sync_with_cfg(monkeypatch, session, {"compression": {}})
    assert compressor.protect_last_n == 20


def test_removing_proactive_prune_keys_restores_defaults(monkeypatch):
    session, compressor = _neutral_session(
        proactive_prune_tokens=48_000,
        proactive_prune_min_result_chars=30_000,
        proactive_prune_min_reclaim_tokens=1,
    )
    _sync_with_cfg(monkeypatch, session, {"compression": {}})
    assert compressor.proactive_prune_tokens == 0
    assert compressor.proactive_prune_min_result_chars == 8000
    assert compressor.proactive_prune_min_reclaim_tokens == 4096


def test_removing_min_tail_user_messages_restores_default(monkeypatch):
    session, compressor = _neutral_session(min_tail_user_messages=4)
    _sync_with_cfg(monkeypatch, session, {"compression": {}})
    assert compressor.min_tail_user_messages == 1


def test_removing_model_thresholds_restores_empty_map(monkeypatch):
    session, compressor = _neutral_session(
        model_thresholds={"unset-test-model": 0.95}
    )
    assert compressor.threshold_percent == 0.95
    _sync_with_cfg(monkeypatch, session, {"compression": {}})
    assert compressor.model_thresholds == {}
    # The stale per-model override must stop steering the live threshold too.
    assert compressor.threshold_percent == 0.50


def test_removing_threshold_restores_derived_default(monkeypatch):
    session, compressor = _neutral_session(threshold_percent=0.85)
    assert compressor.threshold_percent == 0.85
    _sync_with_cfg(
        monkeypatch,
        session,
        {"model": {"context_length": 600_000}, "compression": {}},
    )
    assert compressor._config_threshold_percent == 0.50
    assert compressor.threshold_percent == 0.50
    assert compressor.threshold_tokens == int(600_000 * 0.50)


def test_removing_context_length_reinfers_from_model_metadata(monkeypatch):
    import agent.context_compressor as cc_mod

    session, compressor = _neutral_session()
    assert compressor.context_length == 600_000

    monkeypatch.setattr(
        cc_mod,
        "get_model_context_length",
        lambda *a, **k: 1_000_000,
    )
    _sync_with_cfg(monkeypatch, session, {"model": {}, "compression": {}})
    assert compressor._config_context_length is None
    assert compressor.context_length == 1_000_000
    assert compressor.threshold_tokens == int(1_000_000 * 0.50)


def test_removing_context_length_invalidates_summary_budget(monkeypatch):
    import agent.context_compressor as cc_mod

    session, compressor = _neutral_session()
    assert compressor.max_summary_tokens == 10_000

    monkeypatch.setattr(
        cc_mod,
        "get_model_context_length",
        lambda *a, **k: 100_000,
    )
    _sync_with_cfg(monkeypatch, session, {"model": {}, "compression": {}})

    assert compressor.context_length == 100_000
    assert compressor.max_summary_tokens == 5_000


def test_provider_model_context_length_survives_live_sync_without_global_pin(
    monkeypatch,
):
    import agent.context_compressor as cc_mod

    base_url = "https://relay.example.test/v1"
    compressor = ContextCompressor(
        model="gpt-5.6-sol",
        config_context_length=256_000,
        quiet_mode=True,
    )
    agent = SimpleNamespace(
        model="gpt-5.6-sol",
        provider="custom",
        base_url=base_url,
        context_compressor=compressor,
        compression_enabled=True,
        compression_idle_compact_after_seconds=0,
        _config_context_length=256_000,
    )
    session = {"agent": agent, "session_key": "session-provider-context"}
    cfg = {
        "model": {
            "default": "gpt-5.6-sol",
            "provider": "relay",
            "base_url": base_url,
        },
        "providers": {
            "relay": {
                "api": base_url,
                "models": {
                    "gpt-5.6-sol": {"context_length": 256_000},
                },
            },
        },
        "compression": {},
    }
    monkeypatch.setattr(
        cc_mod,
        "get_model_context_length",
        lambda *a, **k: 1_050_000,
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)

    server._sync_agent_compression_with_config("sid-provider-context", session)

    assert compressor._config_context_length == 256_000
    assert compressor.context_length == 256_000
    assert agent._config_context_length == 256_000


def test_global_context_pin_does_not_override_different_active_custom_route(
    monkeypatch,
):
    default_url = "https://default.example.test/v1"
    active_url = "https://active.example.test/v1"
    compressor = ContextCompressor(
        model="active-model",
        config_context_length=100_000,
        quiet_mode=True,
    )
    agent = SimpleNamespace(
        model="active-model",
        provider="custom",
        base_url=active_url,
        context_compressor=compressor,
        compression_enabled=True,
        compression_idle_compact_after_seconds=0,
        _config_context_length=100_000,
    )
    session = {"agent": agent, "session_key": "session-active-route"}
    cfg = {
        "model": {
            "default": "default-model",
            "provider": "default-relay",
            "base_url": default_url,
            "context_length": 500_000,
        },
        "providers": {
            "active-relay": {
                "api": active_url,
                "models": {
                    "active-model": {"context_length": 100_000},
                },
            },
            "default-relay": {
                "api": default_url,
                "models": {
                    "default-model": {"context_length": 500_000},
                },
            },
        },
        "compression": {},
    }
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)

    server._sync_agent_compression_with_config("sid-active-route", session)

    assert compressor._config_context_length == 100_000
    assert compressor.context_length == 100_000
    assert agent._config_context_length == 100_000


def test_global_context_pin_wins_on_same_named_custom_route_without_model_url(
    monkeypatch,
):
    base_url = "https://relay.example.test/v1"
    compressor = ContextCompressor(
        model="gpt-5.6-sol",
        config_context_length=100_000,
        quiet_mode=True,
    )
    agent = SimpleNamespace(
        model="gpt-5.6-sol",
        provider="custom",
        base_url=base_url,
        context_compressor=compressor,
        compression_enabled=True,
        compression_idle_compact_after_seconds=0,
        _config_context_length=100_000,
    )
    session = {"agent": agent, "session_key": "session-same-named-route"}
    cfg = {
        "model": {
            "default": "gpt-5.6-sol",
            "provider": "relay",
            "context_length": 500_000,
        },
        "providers": {
            "relay": {
                "api": base_url,
                "models": {
                    "gpt-5.6-sol": {"context_length": 100_000},
                },
            },
        },
        "compression": {},
    }
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)

    server._sync_agent_compression_with_config("sid-same-named-route", session)

    assert compressor._config_context_length == 500_000
    assert compressor.context_length == 500_000
    assert agent._config_context_length == 500_000


def test_removing_idle_compact_after_seconds_restores_zero(monkeypatch):
    session, _ = _neutral_session()
    session["agent"].compression_idle_compact_after_seconds = 1800
    _sync_with_cfg(monkeypatch, session, {"compression": {}})
    assert session["agent"].compression_idle_compact_after_seconds == 0


def test_removing_enabled_restores_true(monkeypatch):
    session, _ = _neutral_session()
    session["agent"].compression_enabled = False
    _sync_with_cfg(monkeypatch, session, {"compression": {}})
    assert session["agent"].compression_enabled is True
