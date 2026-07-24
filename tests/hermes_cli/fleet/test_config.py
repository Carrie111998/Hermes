from __future__ import annotations

from decimal import Decimal

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.fleet.config import FleetConfigError, parse_fleet_config
from hermes_cli.fleet.profiles import ordered_profiles
from hermes_cli.fleet.types import AdapterKind, Confidence


def test_defaults_are_disabled_conservative_and_documented_in_main_config():
    config = parse_fleet_config({})

    assert config.enabled is False
    assert config.bridge_usage_file.as_posix() == "C:/HermesBridge/usage-weekly.json"
    assert config.switch_delta_pct == Decimal("20.000")
    assert config.minimum_confidence is Confidence.HIGH
    assert config.lease_ttl_seconds == 1800
    assert config.default_reservation_pct == Decimal("5.000")
    assert config.lanes["chatgpt_codex"].enabled is True
    assert config.lanes["claude_code"].enabled is False
    assert DEFAULT_CONFIG["fleet"]["enabled"] is False


def test_profiles_are_fixed_order_truthful_and_defer_unproven_lanes():
    profiles = ordered_profiles()

    assert [profile.lane_id for profile in profiles] == [
        "chatgpt_codex",
        "claude_code",
        "grok",
        "antigravity",
        "kimi",
    ]
    assert profiles[0].adapter_kind is AdapterKind.NATIVE_PROVIDER
    assert profiles[0].provider_id == "openai-codex"
    assert profiles[1].adapter_kind is AdapterKind.EXTERNAL_CLI
    assert profiles[1].executable == "claude"
    assert profiles[2].provider_id == "xai-oauth"
    assert not profiles[3].implemented
    assert not profiles[4].implemented


@pytest.mark.parametrize(
    ("fleet", "message"),
    [
        ({"switch_delta_pct": 19.999}, "switch_delta_pct"),
        ({"switch_delta_pct": 20.001}, "switch_delta_pct"),
        ({"minimum_confidence": "low"}, "minimum_confidence"),
        ({"lease_ttl_seconds": 0}, "lease_ttl_seconds"),
        ({"lease_ttl_seconds": True}, "lease_ttl_seconds"),
        ({"default_reservation_pct": -1}, "default_reservation_pct"),
        (
            {"lanes": {"chatgpt_codex": {"max_concurrency": 0}}},
            "max_concurrency",
        ),
        (
            {"lanes": {"chatgpt_codex": {"reserve_floor_pct": 101}}},
            "reserve_floor_pct",
        ),
        ({"lanes": {"unknown": {"enabled": True}}}, "unknown lane"),
        (
            {"lanes": {"chatgpt_codex": {"unexpected": True}}},
            "unknown lane option",
        ),
        (
            {"lanes": {"antigravity": {"enabled": True}}},
            "deferred",
        ),
        ({"api_key": "secret"}, "credential"),
        (
            {"lanes": {"claude_code": {"auth_token": "secret"}}},
            "credential",
        ),
    ],
)
def test_invalid_or_billing_sensitive_config_fails_closed(fleet, message):
    with pytest.raises(FleetConfigError, match=message):
        parse_fleet_config({"fleet": fleet})
