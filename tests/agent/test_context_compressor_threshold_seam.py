"""Seam identity for context_compressor_threshold mixin extract (LB7).

Part of #78645 + #78647.
"""

from agent import context_compressor as cc
from agent.context_compressor_threshold import ContextCompressorThresholdMixin as M


def test_mixin_in_mro():
    assert M in cc.ContextCompressor.__mro__
    # mixin resolves before object
    assert cc.ContextCompressor.__mro__.index(M) > 0


def test_all_members_resolve_is_identical_through_class():
    members = [
        "_MIN_CTX_TRIGGER_RATIO",
        "_ANTI_THRASH_RECOVERY_SECONDS",
        "_coerce_max_tokens",
        "_coerce_threshold_tokens_cap",
        "_apply_threshold_tokens_cap",
        "_effective_threshold_percent",
        "_compute_threshold_tokens",
    ]
    for m in members:
        assert getattr(cc.ContextCompressor, m) is getattr(M, m), f"{m} not is-identical"


def test_no_duplicate_defs_in_godfile():
    from pathlib import Path

    src = Path(cc.__file__).read_text(encoding="utf-8")
    for name in [
        "_coerce_max_tokens",
        "_coerce_threshold_tokens_cap",
        "_apply_threshold_tokens_cap",
        "_effective_threshold_percent",
        "_compute_threshold_tokens",
    ]:
        assert src.count(f"def {name}") == 0, f"duplicate def {name} left in godfile"
    assert src.count("_MIN_CTX_TRIGGER_RATIO = ") == 0
    assert "ContextCompressorThresholdMixin" in src


def test_behavior_smoke():
    C = cc.ContextCompressor
    # class attrs preserved
    assert C._MIN_CTX_TRIGGER_RATIO == 0.85
    assert C._ANTI_THRASH_RECOVERY_SECONDS == 300.0
    # static coercions
    assert C._coerce_max_tokens(None) is None
    assert C._coerce_max_tokens(0) is None
    assert C._coerce_max_tokens(100) == 100
    assert C._coerce_threshold_tokens_cap(None) is None
    # threshold percent + compute
    pct = C._effective_threshold_percent(1000, 0.5)
    assert 0 < pct <= 1
    tokens = C._compute_threshold_tokens(10000, 0.8, 4000)
    assert 0 < tokens <= 10000


def test_import_orders_no_cycle():
    import importlib

    import agent.context_compressor_threshold as a
    import agent.context_compressor as b

    importlib.reload(a)
    importlib.reload(b)
    assert b.ContextCompressor._coerce_max_tokens is a.ContextCompressorThresholdMixin._coerce_max_tokens
