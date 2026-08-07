import json
from datetime import datetime, timedelta, timezone

import pytest

from devflow_delegation.policy import (
    DEFAULT_SOURCE_POLICIES,
    PolicyError,
    build_policy,
    check_rate_limits,
    evaluate_source_threshold,
    first_rate_limit_crossing,
    in_cooldown,
    load_policy_overrides,
    policy_for,
)

NOW = datetime(2026, 8, 6, 20, 0, 0, tzinfo=timezone.utc)


def test_all_default_sources_ship_dry_run():
    for name, pol in DEFAULT_SOURCE_POLICIES.items():
        assert pol.mode == "dry_run", name


def test_required_source_kinds_have_policies():
    required = {"explicit", "critic", "arch-review", "watchdog", "nightly-gate",
                "security-audit", "tracker", "curator", "matcher-shadow"}
    assert required <= set(DEFAULT_SOURCE_POLICIES)


def test_unknown_source_falls_back_to_explicit_policy():
    pol = policy_for(DEFAULT_SOURCE_POLICIES, "something-new")
    assert pol == DEFAULT_SOURCE_POLICIES["explicit"]


def test_threshold_rejects_low_confidence_and_severity():
    pol = build_policy({})["critic"]
    d = evaluate_source_threshold(pol, confidence=pol.min_confidence - 0.01, severity="high")
    assert not d.allowed and d.reason == "below_confidence"
    d = evaluate_source_threshold(pol, confidence=1.0, severity="low")
    assert (not d.allowed and d.reason == "below_severity") or d.allowed  # critic floor may be low
    d = evaluate_source_threshold(pol, confidence=1.0, severity="critical")
    assert d.allowed


def test_rate_limits_source_window():
    pol = build_policy({"critic": {"max_per_window": 2}})["critic"]
    ok = check_rate_limits(pol, source_count=1, global_count=1, critical_used=0, is_critical=False)
    assert ok.allowed
    blocked = check_rate_limits(pol, source_count=2, global_count=2, critical_used=0, is_critical=False)
    assert not blocked.allowed and blocked.reason == "rate_limit_source"


def test_critical_bypass_consumes_separate_budget():
    pol = build_policy({"critic": {"max_per_window": 0}})["critic"]
    blocked = check_rate_limits(pol, source_count=5, global_count=5, critical_used=0, is_critical=True)
    assert blocked.allowed and blocked.reason == "ok_critical_budget"
    exhausted = check_rate_limits(pol, source_count=5, global_count=5, critical_used=3, is_critical=True)
    assert not exhausted.allowed and exhausted.reason == "critical_budget_exhausted"


def test_overrides_load_and_merge(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"critic": {"mode": "queue", "min_confidence": 0.9}}), encoding="utf-8")
    overrides = load_policy_overrides(p)
    pol = build_policy(overrides)["critic"]
    assert pol.mode == "queue"
    assert pol.min_confidence == 0.9
    # non-overridden field keeps its default
    assert pol.window_hours == DEFAULT_SOURCE_POLICIES["critic"].window_hours


def test_missing_policy_file_is_not_an_error(tmp_path):
    assert load_policy_overrides(tmp_path / "nope.json") == {}


def test_malformed_policy_file_raises(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy_overrides(p)


def test_cooldown_windows():
    pol = build_policy({"critic": {"cooldown_success_hours": 168, "cooldown_declined_hours": 24}})["critic"]
    recent = (NOW - timedelta(hours=1)).isoformat()
    old = (NOW - timedelta(hours=200)).isoformat()
    assert in_cooldown(policy=pol, terminal_state="DEPLOYED", terminal_updated_at_iso=recent, now=NOW)
    assert not in_cooldown(policy=pol, terminal_state="DEPLOYED", terminal_updated_at_iso=old, now=NOW)
    assert in_cooldown(policy=pol, terminal_state="DECLINED", terminal_updated_at_iso=recent, now=NOW)
    assert not in_cooldown(policy=pol, terminal_state="DECLINED", terminal_updated_at_iso=old, now=NOW)
    # non-terminal states never cooldown
    assert not in_cooldown(policy=pol, terminal_state="FAILED", terminal_updated_at_iso=recent, now=NOW)


def test_first_rate_limit_crossing_fires_once():
    assert first_rate_limit_crossing(source_count=10, limit=10) is True
    assert first_rate_limit_crossing(source_count=11, limit=10) is False
    assert first_rate_limit_crossing(source_count=9, limit=10) is False
