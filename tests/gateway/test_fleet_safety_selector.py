"""Unit tests for the usage-headroom fallback selector and effort mapping."""

import pytest
from gateway.fleet_safety import selector
from gateway.fleet_safety.selector import (
    select_best_lane,
    rank_fallback_chain,
    resolve_effort_from_map,
    get_lane_name,
    SelectedLane,
)
from gateway.fleet_safety.usage_verify import VerifiedUsage


def _mock_usage(used_percent=10.0, stale=False, suspect=False, reasons=None):
    return VerifiedUsage(
        provider="test",
        used_percent=used_percent,
        source="authoritative",
        stale=stale,
        suspect=suspect,
        reasons=reasons or [],
    )


def test_select_best_lane_ranking_by_headroom(monkeypatch):
    def fake_verify(provider, **kwargs):
        if "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=30.0)  # 70% headroom
        elif "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=15.0)  # 85% headroom
        elif "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=60.0)  # 40% headroom
        elif "antigravity" in provider or "gemini" in provider:
            return _mock_usage(used_percent=50.0)  # 50% headroom
        return _mock_usage(used_percent=50.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected.lane == "claude_code"
    assert selected.remaining_headroom == pytest.approx(85.0)
    assert not selected.is_fallback


def test_select_best_lane_all_lanes_below_floor_edge_case(monkeypatch):
    def fake_verify(provider, **kwargs):
        # All below their respective floors (floors: codex 8%, claude 2%, grok 5%, antigravity 5%)
        if "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=95.0)  # 5% headroom (< 8%)
        elif "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=99.0)  # 1% headroom (< 2%)
        elif "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=97.0)  # 3% headroom (< 5%)
        elif "antigravity" in provider or "gemini" in provider:
            return _mock_usage(used_percent=96.0)  # 4% headroom (< 5%)
        return _mock_usage(used_percent=98.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    # Should select fallback with highest remaining headroom (codex at 5%)
    assert selected.lane == "chatgpt_codex"
    assert selected.is_fallback
    assert "all lanes below floor or unverified" in selected.reason


def test_unverified_or_stale_attestation_treated_as_unknown(monkeypatch):
    def fake_verify(provider, **kwargs):
        if "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=80.0)  # 20% headroom (verified)
        elif "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=10.0, stale=True)  # stale attestation -> unknown
        elif "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=None)  # unverified -> unknown
        return _mock_usage(used_percent=None)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}}, is_heavy=True)
    assert selected.lane == "chatgpt_codex"
    assert selected.remaining_headroom == pytest.approx(20.0)
    assert not selected.is_fallback


def test_resolve_effort_from_map():
    effort_map = {
        "gpt-5.6-sol": "max",
        "chatgpt_codex": "xhigh",
        "claude_code": "high",
        "grok": "medium",
        "antigravity": "high",
    }

    # 1. Exact model match
    assert resolve_effort_from_map(effort_map, model="gpt-5.6-sol") == "max"
    # 2. Canonical lane match
    assert resolve_effort_from_map(effort_map, model="claude-sonnet-4-6") == "high"
    # 3. Provider match
    assert resolve_effort_from_map(effort_map, provider="xai-oauth") == "medium"
    # 4. Fallback when map is a string
    assert resolve_effort_from_map("xhigh", model="anything") == "xhigh"
    # 5. Default lane fallback when map is empty/invalid
    assert resolve_effort_from_map(None, provider="openai-codex") == "xhigh"
    assert resolve_effort_from_map(None, provider="xai-oauth") == "high"


def test_claude_fable_opus_threshold(monkeypatch):
    # When Claude weekly < 50%, switch to Fable. When >= 50%, switch to Opus.
    def fake_verify_fable(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=40.0)  # < 50%
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify_fable)
    selected_fable = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected_fable.lane == "claude_code"
    assert selected_fable.model == "claude-fable-5"

    def fake_verify_opus(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=70.0)  # >= 50%
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify_opus)
    selected_opus = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected_opus.lane == "claude_code"
    assert selected_opus.model == "claude-opus-5"


def test_graceful_degrade_provider_down(monkeypatch):
    # When primary provider is walled (100% used) or down, gracefully cascade to next
    def fake_verify(provider, **kwargs):
        if "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=100.0)  # walled / exhausted
        elif "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=60.0)  # available (40% hr)
        elif "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=50.0)  # available (50% hr)
        return _mock_usage(used_percent=50.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    chain = [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        {"provider": "xai-oauth", "model": "grok-4.5"},
    ]
    ranked = rank_fallback_chain(chain, config={"fleet": {"switch_delta": 0.0}})

    assert len(ranked) == 3
    # Grok has 50% hr, Claude has 40% hr, Codex has 0% hr (below floor / walled)
    assert ranked[0]["provider"] == "xai-oauth"
    assert ranked[1]["provider"] == "anthropic"
    assert ranked[2]["provider"] == "openai-codex"
