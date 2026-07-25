import json

import pytest

from agent.error_classifier import FailoverReason


def test_subscription_policy_filters_by_provenance_and_capability():
    from agent.provider_route_policy import (
        Capability,
        RouteRole,
        SubscriptionRoutePolicy,
    )

    policy = SubscriptionRoutePolicy({"orchestrator": {"enabled": True, "billing_policy": "subscription_only"}})

    builder = {"provider": "openai-codex", "model": "gpt-5-codex", "auth_type": "oauth", "source": "manual:device_code"}
    assert policy.evaluate(builder, role=RouteRole.BUILDER, capability=Capability.WRITE).allowed is True

    forbidden_openai = {"provider": "openai", "model": "gpt-5", "auth_type": "api_key", "source": "env"}
    assert policy.evaluate(forbidden_openai, role=RouteRole.BUILDER, capability=Capability.WRITE).allowed is False

    attacker = {"provider": "anthropic", "model": "claude-sonnet-4-5", "auth_type": "oauth", "source": "claude_code"}
    assert policy.evaluate(attacker, role=RouteRole.ATTACKER, capability=Capability.READ).allowed is True
    blocked_write = policy.evaluate(attacker, role=RouteRole.ATTACKER, capability=Capability.WRITE)
    assert blocked_write.allowed is False
    assert "read_only" in blocked_write.reason

    anthropic_api_key = {"provider": "anthropic", "model": "claude-sonnet-4-5", "auth_type": "api_key", "source": "env"}
    assert policy.evaluate(anthropic_api_key, role=RouteRole.ATTACKER, capability=Capability.READ).allowed is False

    claude_fallback_builder = {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "auth_type": "oauth",
        "source": "env:ANTHROPIC_TOKEN",
    }
    assert policy.evaluate(
        claude_fallback_builder,
        role=RouteRole.BUILDER,
        capability=Capability.WRITE,
    ).allowed is True
    self_attack = policy.evaluate(
        attacker,
        role=RouteRole.ATTACKER,
        capability=Capability.READ,
        builder_provider="anthropic",
    )
    assert self_attack.allowed is False
    assert self_attack.reason == "self_attack_forbidden"

    diagnostician = {"provider": "gemini-code-assist", "model": "gemini", "auth_type": "oauth", "source": "google_code_assist"}
    assert policy.evaluate(diagnostician, role=RouteRole.DIAGNOSTICIAN, capability=Capability.READ).allowed is True
    vertex = {"provider": "vertex", "model": "gemini", "auth_type": "oauth", "source": "gcloud"}
    assert policy.evaluate(vertex, role=RouteRole.DIAGNOSTICIAN, capability=Capability.READ).allowed is False


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"status": "ok", "expires_at": "2999-01-01T00:00:00Z", "refresh_token": "sentinel-refresh"}, "healthy_reusable"),
        ({"status": "exhausted", "last_error_code": 429, "last_error_reset_at": 1999999999}, "temporary_rate_limit"),
        ({"status": "ok", "expires_at": "2000-01-01T00:00:00Z", "refresh_token": "sentinel-refresh"}, "expired_access_refresh_available"),
        ({"status": "ok", "expires_at": "2000-01-01T00:00:00Z"}, "expired_access_missing_refresh"),
        ({"status": "dead", "last_error_reason": "token_revoked"}, "revoked_dead"),
        ({"status": "browser_timeout"}, "browser_oauth_timeout"),
        ({"status": "unavailable_cli"}, "unavailable_cli_model"),
        ({"status": "unknown_transport"}, "provider_outage_unknown_transport"),
    ],
)
def test_no_spend_health_inventory_classifies_local_metadata(entry, expected):
    from agent.provider_route_policy import classify_route_health

    health = classify_route_health(entry, now=1700000000.0)
    assert health.availability.value == expected
    assert "sentinel-refresh" not in repr(health)
    assert "sentinel-refresh" not in json.dumps(health.to_summary(), sort_keys=True)


def test_recovery_decision_table_is_bounded_and_deterministic():
    from agent.orchestrator_recovery import FailureClass, RecoveryContext, decide_recovery
    from agent.provider_route_policy import Capability, RouteRole

    alternatives = [
        {"provider": "openai", "model": "gpt-5", "auth_type": "api_key", "source": "env"},
        {"provider": "openai-codex", "model": "gpt-5-codex-high", "auth_type": "oauth", "source": "manual:device_code"},
    ]
    cfg = {"orchestrator": {"enabled": True, "billing_policy": "subscription_only", "max_total_attempts": 3, "max_route_attempts": 1}}

    decision = decide_recovery(
        RecoveryContext(
            failure=FailureClass.TEMPORARY_RATE_LIMIT,
            current_route={"provider": "openai-codex", "model": "gpt-5-codex", "auth_type": "oauth", "source": "manual:device_code"},
            alternatives=alternatives,
            role=RouteRole.BUILDER,
            capability=Capability.WRITE,
            total_attempts=1,
            route_attempts={"openai-codex:gpt-5-codex": 1},
            config=cfg,
        )
    )
    assert decision.action == "switch_route"
    assert decision.next_route["provider"] == "openai-codex"
    assert decision.next_route["model"] == "gpt-5-codex-high"
    assert decision.escalation is None

    exhausted = decide_recovery(
        RecoveryContext(
            failure=FailureClass.FAILED_VERIFICATION,
            current_route={"provider": "openai-codex", "model": "gpt-5-codex", "auth_type": "oauth", "source": "manual:device_code"},
            alternatives=[],
            role=RouteRole.BUILDER,
            capability=Capability.WRITE,
            total_attempts=3,
            route_attempts={},
            config=cfg,
        )
    )
    assert exhausted.action == "escalate"
    assert exhausted.escalation == "no_approved_route"


def test_all_required_failure_classes_have_deterministic_actions():
    from agent.orchestrator_recovery import FailureClass, RecoveryContext, decide_recovery

    cfg = {"orchestrator": {"enabled": True, "billing_policy": "subscription_only"}}
    for failure in FailureClass:
        decision = decide_recovery(RecoveryContext(failure=failure, config=cfg))
        assert decision.action
        assert decision.failure == failure.value
        assert "prompt" not in decision.to_dict()


def test_fallback_chain_filters_paid_routes_only_when_orchestrator_enabled():
    from hermes_cli.fallback_config import get_fallback_chain

    config = {
        "fallback_providers": [
            {"provider": "openai", "model": "gpt-5", "auth_type": "api_key", "source": "env"},
            {"provider": "openai-codex", "model": "gpt-5-codex", "auth_type": "oauth", "source": "manual:device_code"},
            {"provider": "openrouter", "model": "anthropic/claude", "auth_type": "api_key", "source": "env"},
        ]
    }
    assert [e["provider"] for e in get_fallback_chain(config)] == ["openai", "openai-codex", "openrouter"]

    config["orchestrator"] = {"enabled": True, "billing_policy": "subscription_only"}
    assert [e["provider"] for e in get_fallback_chain(config)] == ["openai-codex"]


def test_subscription_policy_keeps_unresolved_claude_fallback_for_runtime_auth_check():
    from hermes_cli.fallback_config import get_fallback_chain

    config = {
        "orchestrator": {"enabled": True, "billing_policy": "subscription_only"},
        "fallback_providers": [
            {"provider": "anthropic", "model": "claude-opus-4-8"},
        ],
    }
    assert get_fallback_chain(config) == [
        {"provider": "anthropic", "model": "claude-opus-4-8"},
    ]


def test_pooled_credential_repr_never_serializes_token_values():
    from agent.credential_pool import PooledCredential

    cred = PooledCredential(
        provider="openai-codex",
        id="codex-1",
        label="Codex",
        auth_type="oauth",
        priority=0,
        source="manual:device_code",
        access_token="sentinel-access-token",
        refresh_token="sentinel-refresh-token",
    )
    text = repr(cred)
    assert "sentinel-access-token" not in text
    assert "sentinel-refresh-token" not in text
    assert "openai-codex" in text
