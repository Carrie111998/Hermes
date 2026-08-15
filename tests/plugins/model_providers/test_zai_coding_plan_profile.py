"""zai-coding-plan profile: endpoint separation from the standard zai provider.

Coding-plan subscriptions authenticate on /api/coding/paas/v4; the standard
/api/paas/v4 route rejects coding-plan keys (HTTP 429, code 1113). The
dedicated profile mirrors alibaba-coding-plan / kimi-coding so coding-plan
users get a working default without hand-editing GLM_BASE_URL.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def coding_profile():
    import model_tools  # noqa: F401  — triggers plugin discovery
    import providers

    p = providers.get_provider_profile("zai-coding-plan")
    assert p is not None, "zai-coding-plan must be registered"
    return p


class TestZaiCodingPlanProfile:
    def test_coding_endpoint_is_default(self, coding_profile):
        import providers

        assert coding_profile.base_url == "https://api.z.ai/api/coding/paas/v4"
        std = providers.get_provider_profile("zai")
        assert std.base_url != coding_profile.base_url, (
            "coding-plan profile must not share the standard endpoint"
        )

    def test_distinct_from_standard_zai(self, coding_profile):
        import providers

        assert coding_profile.name == "zai-coding-plan"
        assert coding_profile.name != "zai"

    def test_env_var_chain_includes_fallback(self, coding_profile):
        """Dedicated vars first, ZAI_API_KEY as fallback so users with one
        key don't need to duplicate it."""
        assert coding_profile.env_vars[0] == "ZAI_CODING_PLAN_API_KEY"
        assert "ZAI_API_KEY" in coding_profile.env_vars

    def test_shares_glm_reasoning_wiring(self, coding_profile):
        """Subclassing ZaiProfile keeps the GLM thinking / reasoning_effort
        wiring — the coding endpoint speaks the same wire shape."""
        extra_body, top_level = coding_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="glm-5.3",
        )
        assert top_level == {"reasoning_effort": "high"}
        assert extra_body.get("thinking") == {"type": "enabled"}

    def test_model_list_registered(self):
        from hermes_cli.models import _PROVIDER_MODELS

        assert "zai-coding-plan" in _PROVIDER_MODELS
        assert "glm-5.3" in _PROVIDER_MODELS["zai-coding-plan"]
