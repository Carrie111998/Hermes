"""Contract tests for the bundled Omnious model-provider plugin."""

from __future__ import annotations

from providers import get_provider_profile


def test_omnious_profile_is_discoverable():
    profile = get_provider_profile("omnious")

    assert profile is not None
    assert profile.display_name == "Omnious"
    assert profile.base_url == "https://api.omnious.xyz/v1"
    assert profile.env_vars == ("OMNIOUS_CREDIT_KEY",)
    assert profile.default_aux_model == "auto"
    assert profile.fallback_models == (
        "auto",
        "glm-5.2",
        "kimi-k2.7-code",
        "kimi-k3",
        "minimax-m3",
    )


def test_omnious_market_alias_resolves_to_native_profile():
    profile = get_provider_profile("omnious-market")

    assert profile is not None
    assert profile.name == "omnious"
