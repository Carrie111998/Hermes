"""Unit tests for the Xiaomi MiMo provider profile reasoning clamp."""

from __future__ import annotations

import pytest


@pytest.fixture
def xiaomi_profile():
    import model_tools  # noqa: F401 — registers provider profiles
    import providers

    profile = providers.get_provider_profile("xiaomi")
    assert profile is not None
    return profile


class TestXiaomiReasoningClamp:
    def test_max_clamps_to_high(self, xiaomi_profile):
        _, top_level = xiaomi_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
        )
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("effort", ["xhigh", "minimal", "garbage", "MAX"])
    def test_unsupported_efforts_clamp_to_high(self, xiaomi_profile, effort):
        _, top_level = xiaomi_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
        )
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_supported_efforts_pass_through(self, xiaomi_profile, effort):
        _, top_level = xiaomi_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
        )
        assert top_level == {"reasoning_effort": effort}

    def test_disabled_omits_reasoning(self, xiaomi_profile):
        extra_body, top_level = xiaomi_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "max"},
        )
        assert extra_body == {}
        assert top_level == {}


class TestXiaomiFallbackClampHelper:
    def test_clamp_agent_reasoning_on_fallback(self):
        from plugins.model_providers.xiaomi import clamp_xiaomi_reasoning_config

        class _Agent:
            reasoning_config = {"enabled": True, "effort": "max"}

        agent = _Agent()
        clamp_xiaomi_reasoning_config(agent)
        assert agent.reasoning_config == {"enabled": True, "effort": "high"}
