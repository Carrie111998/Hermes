"""Unit tests for the per-provider wallet cap."""

from gateway.fleet_safety.wallet_cap import (
    RoutingRequest,
    WalletAction,
    WalletCap,
    WalletCapConfig,
)


def _cap(**kw):
    return WalletCap(WalletCapConfig(**kw))


def test_light_request_never_capped_even_at_100pct():
    cap = _cap()
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=False), 100.0)
    assert d.action is WalletAction.ALLOW
    assert d.effort == "max"


def test_heavy_below_downgrade_allowed():
    cap = _cap(cap_percent=90, downgrade_percent=80)
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=True), 50.0)
    assert d.action is WalletAction.ALLOW
    assert d.effort == "max"


def test_heavy_at_downgrade_band_downgrades_effort():
    cap = _cap(cap_percent=90, downgrade_percent=80, downgrade_floor="medium")
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=True), 85.0)
    assert d.action is WalletAction.DOWNGRADE_EFFORT
    assert d.effort == "high"      # one rung down from max
    assert d.allowed


def test_heavy_at_cap_falls_to_next_provider():
    cap = _cap(cap_percent=90, downgrade_percent=80)
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=True), 95.0)
    assert d.action is WalletAction.FALLBACK_PROVIDER
    assert not d.allowed
    assert d.used_percent == 95.0


def test_incident_100pct_provider_is_blocked_for_heavy():
    # The 2026-07-25 scenario: provider actually at 100% → no new heavy work.
    cap = _cap()
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=True), 100.0)
    assert d.action is WalletAction.FALLBACK_PROVIDER


def test_downgrade_respects_floor():
    cap = _cap(downgrade_percent=80, downgrade_floor="high")
    # effort already at floor → allow, not an infinite downgrade
    d = cap.decide(RoutingRequest("grok", effort="high", is_heavy=True), 85.0)
    assert d.action is WalletAction.ALLOW


def test_unknown_usage_downgrades_by_default():
    cap = _cap(on_unknown="downgrade")
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=True), None)
    assert d.action is WalletAction.DOWNGRADE_EFFORT
    assert d.effort == "high"


def test_unknown_usage_fallback_policy():
    cap = _cap(on_unknown="fallback")
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=True), None)
    assert d.action is WalletAction.FALLBACK_PROVIDER


def test_unknown_usage_allow_policy():
    cap = _cap(on_unknown="allow")
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=True), None)
    assert d.action is WalletAction.ALLOW


def test_disabled_cap_allows_everything():
    cap = _cap(enabled=False)
    d = cap.decide(RoutingRequest("grok", effort="max", is_heavy=True), 100.0)
    assert d.action is WalletAction.ALLOW


def test_config_from_dict_validates_enums():
    c = WalletCapConfig.from_config({
        "cap_percent": "90",
        "downgrade_percent": 75,
        "downgrade_floor": "bogus",      # invalid → default
        "on_unknown": "nonsense",        # invalid → default
    })
    assert c.cap_percent == 90.0
    assert c.downgrade_percent == 75.0
    assert c.downgrade_floor == WalletCapConfig().downgrade_floor
    assert c.on_unknown == WalletCapConfig().on_unknown
