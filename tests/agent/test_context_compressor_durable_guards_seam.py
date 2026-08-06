"""Seam identity for context_compressor_durable_guards mixin extract (LB6).

Part of #78645 + #78647.
"""

from agent import context_compressor as cc
from agent.context_compressor_durable_guards import ContextCompressorDurableGuardsMixin as M


def test_mixin_in_mro_before_engine():
    # Mixins must precede ContextEngine in bases so their definitions shadow
    # ContextEngine.on_session_end / on_session_start (which the godfile's
    # own defs previously shadowed by being directly on ContextCompressor).
    mro = cc.ContextCompressor.__mro__
    assert M in mro
    assert mro.index(M) < mro.index(cc.ContextEngine)


def test_all_members_resolve_is_identical_through_class():
    members = [
        "on_session_end",
        "on_session_start",
        "bind_session_state",
        "_load_fallback_compression_streak",
        "_load_proactive_prune_rearm_tokens",
        "_clear_durable_proactive_prune_rearm",
        "_persist_fallback_compression_streak",
        "_load_ineffective_compression_count",
        "_persist_ineffective_compression_count",
        "_record_ineffective_compression_verdict",
        "record_completed_compaction",
        "get_active_compression_failure_cooldown",
        "_record_compression_failure_cooldown",
        "record_timeout_failure",
        "_clear_compression_failure_cooldown",
    ]
    for m in members:
        assert getattr(cc.ContextCompressor, m) is getattr(M, m), f"{m} not is-identical"


def test_no_duplicate_defs_in_godfile():
    from pathlib import Path

    src = Path(cc.__file__).read_text(encoding="utf-8")
    for name in [
        "on_session_end",
        "on_session_start",
        "bind_session_state",
        "record_completed_compaction",
        "record_timeout_failure",
    ]:
        assert src.count(f"def {name}") == 0, f"duplicate def {name} left in godfile"
    assert "ContextCompressorDurableGuardsMixin" in src


def test_behavior_smoke():
    C = cc.ContextCompressor
    # session lifecycle method resolves to the mixin implementation
    assert C.on_session_end.__module__ == "agent.context_compressor_durable_guards"
    assert C.on_session_start.__module__ == "agent.context_compressor_durable_guards"
    # instantiate and exercise lifecycle (no DB, should not raise)
    import unittest.mock as um
    with um.patch("agent.context_compressor.get_model_context_length", return_value=100000):
        c = C(model="test/model", quiet_mode=True)
        c.on_session_end("sid", [])
        c.on_session_start("sid")
        c.record_timeout_failure("boom")


def test_import_orders_no_cycle():
    import importlib

    import agent.context_compressor_durable_guards as a
    import agent.context_compressor as b

    importlib.reload(a)
    importlib.reload(b)
    assert b.ContextCompressor.on_session_end is a.ContextCompressorDurableGuardsMixin.on_session_end
