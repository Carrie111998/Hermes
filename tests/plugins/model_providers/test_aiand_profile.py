"""Unit tests for the ai& (aiand) provider profile.

Pins the profile's contract without going live: identity, alias
registration, and the curated model defaults (vendor-prefixed catalog IDs
matching the models.dev ``aiand`` provider).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def aiand_profile():
    """Resolve the registered ai& profile through the real discovery path."""
    # Importing model_tools triggers plugin discovery, registering the profile.
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("aiand")
    assert profile is not None, "aiand provider profile must be registered"
    return profile


class TestAiandIdentity:
    def test_core_fields(self, aiand_profile):
        p = aiand_profile
        assert p.name == "aiand"
        assert p.auth_type == "api_key"
        assert p.base_url == "https://api.aiand.com/v1"
        assert "AIAND_API_KEY" in p.env_vars
        assert "AIAND_BASE_URL" not in p.env_vars

    def test_display_metadata_present(self, aiand_profile):
        assert aiand_profile.display_name == "ai&"
        assert aiand_profile.description
        assert aiand_profile.signup_url.startswith("https://")


class TestAiandHeaders:
    def test_no_partner_attribution_headers(self, aiand_profile):
        assert "HTTP-Referer" not in aiand_profile.default_headers
        assert "X-Title" not in aiand_profile.default_headers


class TestAiandAliases:
    @pytest.mark.parametrize("alias", ["ai&", "ai-and"])
    def test_alias_resolves_via_registry(self, aiand_profile, alias):
        import providers

        resolved = providers.get_provider_profile(alias)
        assert resolved is not None
        assert resolved.name == "aiand"

    def test_aliases_declared_on_profile(self, aiand_profile):
        assert "ai&" in aiand_profile.aliases
        assert "ai-and" in aiand_profile.aliases


class TestAiandModelDefaults:
    """Defaults must be usable with a standard ai& API key.

    All catalog IDs are vendor-prefixed slugs matching the upstream org
    names (deepseek-ai/…, moonshotai/…, zai-org/…), as published on
    models.dev under the ``aiand`` provider.
    """

    def test_aux_model_is_cheap_catalog_model(self, aiand_profile):
        aux = aiand_profile.default_aux_model
        assert aux == "deepseek-ai/deepseek-v4-flash"

    def test_fallback_models_are_vendor_prefixed(self, aiand_profile):
        assert aiand_profile.fallback_models, "expected curated fallbacks"
        for model in aiand_profile.fallback_models:
            assert "/" in model, f"aiand model {model!r} missing vendor prefix"

    def test_fallback_models_track_models_dev_catalog(self, aiand_profile):
        # Spot-check the agentic leads of the models.dev aiand catalog.
        assert "moonshotai/kimi-k2.7-code" in aiand_profile.fallback_models
        assert "zai-org/glm-5.2" in aiand_profile.fallback_models
        assert "deepseek-ai/deepseek-v4-flash" in aiand_profile.fallback_models


class TestAiandModelsDevMapping:
    def test_models_dev_provider_id(self):
        from agent.models_dev import PROVIDER_TO_MODELS_DEV

        assert PROVIDER_TO_MODELS_DEV.get("aiand") == "aiand"
