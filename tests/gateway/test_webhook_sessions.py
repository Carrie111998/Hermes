"""Task 12 session/profile and unattended interaction policy regressions."""

import pytest

from gateway.platforms.webhook_policy import (
    WebhookPolicyError,
    interaction_context,
    resolve_webhook_session_key,
    session_is_one_shot,
    validate_webhook_route_policy,
)


def test_event_sessions_are_unique_and_profile_scoped():
    route = {"session_mode": "event"}
    keys = {
        resolve_webhook_session_key(
            profile=profile,
            route_name="route",
            route=route,
            payload={},
            delivery_id=delivery,
        )
        for profile, delivery in (("alpha", "one"), ("alpha", "two"), ("beta", "one"))
    }
    assert len(keys) == 3
    assert session_is_one_shot(route)


def test_keyed_session_is_stable_across_deliveries_but_not_profiles():
    route = {"session_mode": "keyed", "session_key_template": "{repo.id}:{issue.number}"}
    payload = {"repo": {"id": 7}, "issue": {"number": 42}}
    alpha_one = resolve_webhook_session_key(
        profile="alpha", route_name="issues", route=route, payload=payload, delivery_id="one"
    )
    alpha_two = resolve_webhook_session_key(
        profile="alpha", route_name="issues", route=route, payload=payload, delivery_id="two"
    )
    beta = resolve_webhook_session_key(
        profile="beta", route_name="issues", route=route, payload=payload, delivery_id="one"
    )
    assert alpha_one == alpha_two
    assert alpha_one != beta
    assert not session_is_one_shot(route)


def test_missing_session_template_token_fails_closed():
    route = {"session_mode": "keyed", "session_key_template": "{missing.id}"}
    with pytest.raises(WebhookPolicyError, match="missing"):
        resolve_webhook_session_key(
            profile="default", route_name="route", route=route, payload={}, delivery_id="one"
        )


def test_unattended_interactions_default_to_deny_and_fail():
    validate_webhook_route_policy("route", {})
    context = interaction_context(
        profile="default", route_name="route", session_key="key", route={}
    )
    assert context.approval_mode == "deny"
    assert context.clarification_mode == "fail"


def test_delivery_target_interactions_require_real_bidirectional_targets():
    with pytest.raises(WebhookPolicyError, match="bidirectional"):
        validate_webhook_route_policy(
            "route",
            {"approval_mode": "delivery_target", "deliveries": [{"target": "log"}]},
        )
    validate_webhook_route_policy(
        "route",
        {
            "approval_mode": "delivery_target",
            "clarification_mode": "delivery_target",
            "deliveries": [{"target": "discord", "chat_id": "123"}],
        },
    )


def test_callback_is_not_misrepresented_as_interactive_clarification():
    with pytest.raises(WebhookPolicyError, match="unsupported clarification_mode"):
        validate_webhook_route_policy(
            "route", {"clarification_mode": "callback", "callback": {"url": "https://example.test"}}
        )
