"""Provider configuration and digest contract for Hermes session affinity."""

import re

from hermes_cli.config import (
    _normalize_custom_provider_entry,
    build_session_affinity_key,
    custom_provider_session_affinity_enabled,
    normalize_session_affinity_base_url,
    providers_dict_to_custom_providers,
)


def test_named_provider_normalizes_explicit_boolean_opt_in():
    enabled = providers_dict_to_custom_providers({
        "trusted-local": {
            "api": "https://llm.example/v1",
            "session_affinity": True,
        }
    })
    disabled = providers_dict_to_custom_providers({
        "ordinary": {"api": "https://cloud.example/v1"}
    })

    assert enabled[0]["session_affinity"] is True
    assert "session_affinity" not in disabled[0]


def test_legacy_provider_normalizes_false_and_invalid_values_to_disabled():
    explicit_false = _normalize_custom_provider_entry({
        "name": "disabled",
        "base_url": "https://disabled.example/v1",
        "session_affinity": False,
    })
    quoted_true = _normalize_custom_provider_entry({
        "name": "not-an-explicit-bool",
        "base_url": "https://invalid.example/v1",
        "session_affinity": "true",
    })

    assert explicit_false is not None
    assert explicit_false["session_affinity"] is False
    assert quoted_true is not None
    assert quoted_true["session_affinity"] is False


def test_affinity_opt_in_lookup_is_exact_and_route_scoped():
    providers = [
        {
            "name": "trusted-local",
            "provider_key": "trusted-local",
            "base_url": "https://llm.example/v1/",
            "session_affinity": True,
        },
        {
            "name": "cloud",
            "provider_key": "cloud",
            "base_url": "https://cloud.example/v1",
            "session_affinity": False,
        },
    ]

    assert custom_provider_session_affinity_enabled(
        "https://llm.example/v1", providers, provider_name="custom:trusted-local"
    )
    assert not custom_provider_session_affinity_enabled(
        "https://cloud.example/v1", providers, provider_name="custom:cloud"
    )
    assert not custom_provider_session_affinity_enabled(
        "https://unknown.example/v1", providers, provider_name="custom"
    )


def test_named_provider_opt_in_survives_model_switch_on_the_same_route():
    providers = [
        {
            "name": "trusted-local",
            "provider_key": "trusted-local",
            "base_url": "https://llm.example/v1",
            "model": "initial-model",
            "session_affinity": True,
        }
    ]

    assert custom_provider_session_affinity_enabled(
        "https://llm.example/v1",
        providers,
        provider_name="custom:trusted-local",
        model="switched-model",
    )


def test_exact_versioned_digest_contract_and_normalized_route_reuse():
    expected = "v1.50d7bc4b8de379a2d029943e7060e8e3ad3e31d981d84bdf1796a5f17a0c60cc"

    assert normalize_session_affinity_base_url(" https://llm.gucci.dev/v1/ ") == (
        "https://llm.gucci.dev/v1"
    )
    assert (
        build_session_affinity_key("https://llm.gucci.dev/v1", "parent-session-123")
        == expected
    )
    assert (
        build_session_affinity_key("https://llm.gucci.dev/v1/", "parent-session-123")
        == expected
    )
    assert re.fullmatch(r"v1\.[0-9a-f]{64}", expected)
    assert "parent-session-123" not in expected


def test_route_normalization_does_not_strip_query_value_slashes():
    route = "https://llm.example/v1?upstream=https://replica.example/"

    assert normalize_session_affinity_base_url(route) == route
    assert build_session_affinity_key(route, "session") != build_session_affinity_key(
        route.rstrip("/"), "session"
    )


def test_different_sessions_and_provider_routes_have_different_keys():
    parent = build_session_affinity_key(
        "https://llm.gucci.dev/v1", "parent-session-123"
    )
    child = build_session_affinity_key("https://llm.gucci.dev/v1", "child-session-456")
    other_route = build_session_affinity_key(
        "https://other.example/v1", "parent-session-123"
    )

    assert parent != child
    assert parent != other_route
    assert child != other_route
