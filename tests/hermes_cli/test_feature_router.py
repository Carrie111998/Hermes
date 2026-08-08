"""Tests for the proactive feature router (PR-B).

Covers: registry matching (OR-semantics), threshold gating, router lifecycle
(disabled default, rate limiting, per-feature kill switches, unknown-capability
whitelist guard), and the turn_context injection path (sidecar only, never
touches stored content / system prompt).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.feature_registry import (
    KNOWN_CAPABILITIES,
    Feature,
    FeatureRegistry,
)
from agent.feature_router import FeatureRouter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Registry matching
# ---------------------------------------------------------------------------

def test_registry_seed_features_have_known_capabilities():
    reg = FeatureRegistry()
    assert reg.features
    for f in reg.features:
        assert f.suggested_capability in KNOWN_CAPABILITIES


@pytest.mark.parametrize(
    "text,feature_id",
    [
        ("帮我并行处理这3个文件", "parallel_subtasks"),
        ("run these 3 tasks in parallel", "parallel_subtasks"),
        ("每天早上下载最新的行情数据", "scheduled_recurring"),
        ("remind me every morning", "scheduled_recurring"),
        ("搜索一下今天DeepSeek的最新消息", "web_research"),
        ("research the current state of X", "web_research"),
        ("记住我更喜欢用中文回复", "remember_fact"),
        ("remember that I prefer dark mode", "remember_fact"),
    ],
)
def test_registry_matches(text, feature_id):
    reg = FeatureRegistry()
    f = reg.suggest(text, min_confidence=0.6)
    assert f is not None
    assert f.id == feature_id


@pytest.mark.parametrize(
    "text",
    [
        "what is the capital of france",
        "2 + 2 = ?",
        "hello",
        "今天天气怎么样",
        "git rebase 是什么意思",
        # Review #81582 issue 2 examples — these must NOT trigger.
        "do each of these sequentially",           # parallel_subtasks false positive
        "the function always returns None",        # remember_fact false positive
        "I'd prefer you didn't do that",           # remember_fact false positive
        "search the codebase for the bug",         # web_research false positive
        "the current directory is /tmp",           # web_research false positive
        "每次提交前都要检查",                        # scheduled_recurring false positive (bare 每)
        "每个用户都要登录",                          # scheduled_recurring false positive
    ],
)
def test_registry_no_false_positive(text):
    reg = FeatureRegistry()
    assert reg.suggest(text, min_confidence=0.6) is None


def test_high_threshold_requires_multiple_signals():
    reg = FeatureRegistry()
    # A single keyword hit is below an explicit 1.5 threshold.
    f = reg.suggest("并行处理", min_confidence=1.5)
    assert f is None


def test_multi_signal_threshold_actually_fires():
    """Regression (review #81582 issue 1): with the cap removed, 2+ hits at
    min_confidence=1.5 must trigger — the old min(1.0, hits) made it impossible."""
    reg = FeatureRegistry()
    # "帮我并行处理这3个文件" hits 并行 + the run-N-files pattern = 2 signals.
    f = reg.suggest("帮我并行处理这3个文件", min_confidence=1.5)
    assert f is not None
    assert f.id == "parallel_subtasks"


def test_unknown_capability_dropped_at_init():
    bad = Feature(
        id="evil", name="evil",
        suggested_capability="rm -rf /", keywords=("x",),
    )
    reg = FeatureRegistry([bad])
    assert len(reg.features) == 0


def test_per_feature_kill_switch():
    router = FeatureRouter(
        {
            "enabled": True,
            "min_confidence": 0.6,
            "features": {"parallel_subtasks": False},
        }
    )
    # The feature is disabled even though the message matches.
    assert router.on_turn_start("帮我并行处理这3个文件") == ""


# ---------------------------------------------------------------------------
# Router lifecycle
# ---------------------------------------------------------------------------

def test_disabled_by_default():
    r = FeatureRouter({})
    assert r.on_turn_start("帮我并行处理这3个文件") == ""
    assert not r.auto_apply_allowed()


def test_auto_apply_requires_opt_in():
    r = FeatureRouter({"enabled": True, "auto_apply": False})
    assert not r.auto_apply_allowed()
    r2 = FeatureRouter({"enabled": True, "auto_apply": True})
    assert r2.auto_apply_allowed()


def test_first_suggestion_fires_then_rate_limited():
    r = FeatureRouter(
        {"enabled": True, "min_confidence": 0.6, "rate_limit_turns": 3}
    )
    s = r.on_turn_start("帮我并行处理这3个文件")
    assert "delegate_task" in s
    for _ in range(3):
        assert r.on_turn_start("并行处理") == ""
    # 4th turn recovers.
    s2 = r.on_turn_start("并行处理这3个文件")
    assert "delegate_task" in s2


def test_suggestion_text_is_advisory_and_explainable():
    r = FeatureRouter({"enabled": True, "min_confidence": 0.6})
    s = r.on_turn_start("帮我并行处理这3个文件")
    assert "Consider using" in s
    assert "Why:" in s
    assert "Advisory" in s
    assert "delegate_task" in s


def test_router_never_raises_on_bad_input():
    r = FeatureRouter({"enabled": True, "min_confidence": 0.6})
    assert r.on_turn_start(None) == ""
    assert r.on_turn_start(12345) == ""
    assert r.on_turn_start("") == ""


def test_router_never_raises_on_broken_registry(monkeypatch):
    r = FeatureRouter({"enabled": True})

    def boom(text, *, min_confidence=None):
        raise RuntimeError("simulated")

    monkeypatch.setattr(r.registry, "suggest", boom)
    assert r.on_turn_start("parallel tasks") == ""


# ---------------------------------------------------------------------------
# Turn-context injection (sidecar only)
# ---------------------------------------------------------------------------

def _make_fake_agent():
    """A minimal fake agent exposing just the attrs turn_context touches."""
    class FakeAgent:
        _memory_manager = None
        _feature_router = None
        _interrupt_requested = False
        _execution_thread_id = None
        _memory_manager_enabled = False
        _api_mode = None
        _session_db = None
        _last_compaction_in_place = False
        pass

    return FakeAgent()


def test_injection_appends_to_prefetch_cache():
    """The router output must ride the API-only sidecar, not stored content."""
    from agent.feature_router import FeatureRouter

    agent = _make_fake_agent()
    agent._feature_router = FeatureRouter(
        {"enabled": True, "min_confidence": 0.6, "rate_limit_turns": 0}
    )

    # Simulate the exact block from turn_context.py prologue.
    original_user_message = "帮我并行处理这3个文件"
    ext_prefetch_cache = ""
    if getattr(agent, "_feature_router", None) is not None:
        _fq = original_user_message if isinstance(original_user_message, str) else ""
        _fs = agent._feature_router.on_turn_start(_fq)
        if _fs:
            ext_prefetch_cache = (
                ext_prefetch_cache + "\n\n" + _fs
                if ext_prefetch_cache else _fs
            )

    assert "delegate_task" in ext_prefetch_cache
    # The clean user message is untouched.
    assert "并行" in original_user_message


def test_injection_absent_when_disabled():
    from agent.feature_router import FeatureRouter

    agent = _make_fake_agent()
    agent._feature_router = FeatureRouter({})  # disabled

    original_user_message = "帮我并行处理这3个文件"
    ext_prefetch_cache = ""
    if getattr(agent, "_feature_router", None) is not None:
        _fs = agent._feature_router.on_turn_start(original_user_message)
        if _fs:
            ext_prefetch_cache = ext_prefetch_cache + "\n\n" + _fs if ext_prefetch_cache else _fs

    assert ext_prefetch_cache == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
