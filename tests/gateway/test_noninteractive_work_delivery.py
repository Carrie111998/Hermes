"""Focused tests for the producer-neutral non-interactive delivery contract."""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.delivery import (
    NonInteractiveWorkDescriptor,
    NonInteractiveWorkPolicy,
    resolve_noninteractive_work_policy,
)


def test_missing_configuration_preserves_origin_delivery():
    policy = resolve_noninteractive_work_policy({})

    assert policy.enabled is False
    assert policy.route_for("background") == "origin"
    assert policy.route_for("cron") == "origin"


def test_enabled_operations_policy_normalizes_values_without_mutating_input():
    raw = {
        "enabled": True,
        "channel_id": "123456789012345678",
        "chief_user_id": " 987654321098765432 ",
        "mention_on": ["intervention", "failure"],
        "channel_name": "background-sessions",
        "auto_archive_duration": "4320",
        "cleanup": "RETAIN",
        "routing": {"background": "local"},
    }
    original = dict(raw)

    policy = resolve_noninteractive_work_policy(raw)

    assert policy.enabled is True
    assert policy.channel_id == "123456789012345678"
    assert policy.chief_user_id == "987654321098765432"
    assert policy.mention_on == ("failure", "intervention")
    assert policy.channel_name == "background-sessions"
    assert policy.auto_archive_duration == 4320
    assert policy.cleanup == "retain"
    assert policy.route_for("background") == "local"
    assert policy.route_for("cron") == "operations"
    assert raw == original


def test_invalid_values_fall_back_safely():
    policy = resolve_noninteractive_work_policy(
        {
            "enabled": True,
            "channel_id": "background-sessions",
            "auto_archive_duration": 999,
            "cleanup": "purge",
            "routing": {"background": "operations"},
        }
    )

    assert policy.enabled is False
    assert policy.channel_id is None
    assert policy.auto_archive_duration == 1440
    assert policy.cleanup == "archive"
    assert policy.route_for("background") == "origin"
    assert policy.route_for("cron") == "origin"


def test_public_config_entry_point_resolves_nested_raw_config():
    from gateway.config import resolve_noninteractive_work_policy

    policy = resolve_noninteractive_work_policy(
        {
            "platforms": {
                "discord": {
                    "extra": {
                        "noninteractive_work": {
                            "enabled": True,
                            "channel_id": "123456789012345678",
                        }
                    }
                }
            }
        }
    )

    assert policy.enabled is True
    assert policy.channel_id == "123456789012345678"


def test_gateway_config_platform_shape_resolves_discord_extra():
    config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                extra={
                    "noninteractive_work": {
                        "enabled": True,
                        "channel_id": "123456789012345678",
                    }
                }
            )
        }
    )

    policy = resolve_noninteractive_work_policy(config)

    assert policy.enabled is True
    assert policy.channel_id == "123456789012345678"


def test_unknown_boolean_tokens_use_each_field_default():
    policy = resolve_noninteractive_work_policy(
        {
            "enabled": "maybe",
            "channel_id": "123456789012345678",
            "retain_failures": "maybe",
            "fallback_to_origin": "maybe",
            "include_start_message": "maybe",
            "include_cron": "maybe",
            "include_background": "maybe",
            "include_delegated": "maybe",
        }
    )

    assert policy.enabled is False
    assert policy.retain_failures is True
    assert policy.fallback_to_origin is True
    assert policy.include_start_message is True
    assert policy.include_cron is True
    assert policy.include_background is True
    assert policy.include_delegated is True


def test_channel_id_whitespace_is_stripped_before_validation():
    policy = resolve_noninteractive_work_policy(
        {"enabled": True, "channel_id": " 123456789012345678 "}
    )

    assert policy.enabled is True
    assert policy.channel_id == "123456789012345678"


@pytest.mark.parametrize("value", [None, "", "  ", "123", "12345678901234567890a"])
def test_chief_user_id_requires_a_trimmed_17_to_20_digit_string(value):
    policy = resolve_noninteractive_work_policy({"chief_user_id": value})

    assert policy.chief_user_id is None


def test_mention_on_defaults_to_failure_and_intervention():
    policy = resolve_noninteractive_work_policy({})

    assert policy.mention_on == ("failure", "intervention")


@pytest.mark.parametrize(
    "value",
    [
        ["success"],
        ["failure", "start"],
        "failure,intervention",
        [],
    ],
)
def test_malformed_mention_on_uses_safe_default_without_routine_events(value):
    policy = resolve_noninteractive_work_policy({"mention_on": value})

    assert policy.mention_on == ("failure", "intervention")
    assert not set(policy.mention_on) & {"start", "progress", "success"}


def test_frozen_policy_mappings_are_immutable():
    policy = NonInteractiveWorkPolicy(
        routing={"cron": "local"},
    )
    work = NonInteractiveWorkDescriptor(
        producer="cron",
        work_id="run-1",
        title="Job",
        origin={"chat_id": "99"},
        reply_binding={"session_id": "session-1"},
    )

    with pytest.raises(TypeError):
        policy.routing["cron"] = "origin"
    with pytest.raises(TypeError):
        work.origin["chat_id"] = "100"
    with pytest.raises(TypeError):
        work.reply_binding["session_id"] = "session-2"


def test_descriptor_takes_a_deeply_immutable_snapshot_of_nested_values():
    origin = {"metadata": {"labels": ["initial"], "scopes": {"read"}}}
    binding = {"parameters": [{"name": "region"}]}

    work = NonInteractiveWorkDescriptor(
        producer="cron",
        work_id="run-1",
        title="Job",
        origin=origin,
        reply_binding=binding,
    )

    origin["metadata"]["labels"].append("changed")
    binding["parameters"][0]["name"] = "changed"

    assert list(work.origin["metadata"]["labels"]) == ["initial"]
    assert work.origin["metadata"]["scopes"] == frozenset({"read"})
    assert work.reply_binding["parameters"][0]["name"] == "region"
    with pytest.raises(TypeError):
        work.origin["metadata"]["labels"][0] = "blocked"
    with pytest.raises(TypeError):
        work.reply_binding["parameters"][0]["name"] = "blocked"


def test_policy_takes_a_deeply_immutable_snapshot_of_routing_values():
    routing = {"cron": {"destinations": ["operations"]}}

    policy = NonInteractiveWorkPolicy(routing=routing)

    routing["cron"]["destinations"].append("local")

    assert list(policy.routing["cron"]["destinations"]) == ["operations"]
    with pytest.raises(TypeError):
        policy.routing["cron"]["destinations"][0] = "blocked"


@pytest.mark.parametrize("channel_id", [None, "background-sessions"])
def test_direct_enabled_policy_with_invalid_channel_never_routes_to_operations(channel_id):
    policy = NonInteractiveWorkPolicy(enabled=True, channel_id=channel_id)

    assert policy.route_for("background") == "origin"
    assert policy.route_for("cron") == "origin"


def test_direct_enabled_policy_with_valid_channel_routes_to_operations():
    policy = NonInteractiveWorkPolicy(
        enabled=True,
        channel_id="123456789012345678",
    )

    assert policy.route_for("background") == "operations"
    assert policy.route_for("cron") == "operations"


def test_disabled_policy_keeps_safe_local_and_origin_overrides_but_not_operations():
    policy = resolve_noninteractive_work_policy(
        {
            "routing": {"cron": "local", "background": "operations"},
        }
    )

    assert policy.enabled is False
    assert policy.route_for("cron") == "local"
    assert policy.route_for("background") == "origin"
    assert policy.route_for("delegated") == "origin"


@pytest.mark.parametrize(
    ("producer", "field"),
    [
        ("cron", "include_cron"),
        ("background", "include_background"),
        ("delegated", "include_delegated"),
    ],
)
def test_disabled_producer_routes_to_origin_even_with_operations_override(producer, field):
    policy = NonInteractiveWorkPolicy(
        enabled=True,
        channel_id="123456789012345678",
        **{field: False},
        routing={producer: "operations"},
    )

    assert policy.route_for(producer) == "origin"


def test_disabled_producer_preserves_safe_local_override():
    policy = NonInteractiveWorkPolicy(
        enabled=True,
        channel_id="123456789012345678",
        include_background=False,
        routing={"background": "local"},
    )

    assert policy.route_for("background") == "local"


def test_work_descriptor_carries_producer_neutral_lifecycle_and_binding():
    work = NonInteractiveWorkDescriptor(
        producer="delegated",
        work_id="run-42",
        title="Deploy service",
        origin={"platform": "discord", "chat_id": "99"},
        interaction_mode="intervention-capable",
        terminal_status="needs_intervention",
        reply_binding={"session_id": "session-42"},
    )

    assert work.producer == "delegated"
    assert work.work_id == "run-42"
    assert work.origin["chat_id"] == "99"
    assert work.interaction_mode == "intervention-capable"
    assert work.terminal_status == "needs_intervention"
    assert work.reply_binding == {"session_id": "session-42"}
