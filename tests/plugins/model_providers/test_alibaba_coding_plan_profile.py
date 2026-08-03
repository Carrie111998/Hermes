"""Unit tests for the Alibaba Cloud Coding Plan provider profile's
reasoning_effort wiring (issue #77818).

Before this fix, alibaba-coding-plan was a thin-shell ProviderProfile
instance with no build_api_kwargs_extras() override, so it inherited the
base class's no-op default (({}, {})) -- a configured reasoning_effort was
silently discarded and never reached the outgoing API request, with no
error or warning anywhere in the pipeline.

The dashscope Qwen3 thinking models this endpoint serves always have
thinking ON (cannot be disabled) and accept reasoning_effort:
xhigh/medium/low (server default: xhigh).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def alibaba_coding_plan_profile():
    """Resolve the registered profile via the provider registry.

    Going through get_provider_profile() (not importing the module's
    instance directly) keeps the test honest: if the registered class is
    ever swapped back for a plain ProviderProfile, the assertions below
    collapse rather than silently testing a stale reference.
    """
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("alibaba-coding-plan")
    assert profile is not None, "alibaba-coding-plan provider profile must be registered"
    return profile


class TestAlibabaCodingPlanReasoningWiring:
    def test_no_reasoning_config_emits_nothing(self, alibaba_coding_plan_profile):
        """No configured reasoning_config: omit the field entirely so the
        endpoint's own server-side default (xhigh) applies -- don't force
        a level the user didn't pick."""
        extra_body, top_level = alibaba_coding_plan_profile.build_api_kwargs_extras(
            reasoning_config=None,
        )
        assert "reasoning_effort" not in top_level
        assert "reasoning_effort" not in extra_body

    def test_enabled_with_effort_forwards_top_level_reasoning_effort(
        self, alibaba_coding_plan_profile
    ):
        """The exact regression from #77818: a configured effort level
        must reach the wire as a top-level reasoning_effort field."""
        extra_body, top_level = alibaba_coding_plan_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "low"},
        )
        assert top_level.get("reasoning_effort") == "low"

    def test_enabled_without_effort_omits_field(self, alibaba_coding_plan_profile):
        """Enabled but no specific effort chosen: omit the field, letting
        the server's own default (xhigh) apply."""
        extra_body, top_level = alibaba_coding_plan_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": ""},
        )
        assert "reasoning_effort" not in top_level

    def test_disabled_remaps_to_lowest_level_not_forwarded_as_none(
        self, alibaba_coding_plan_profile
    ):
        """These Qwen3 thinking models cannot disable thinking outright --
        the server always has it on. A disabled reasoning_config must be
        remapped to the lowest available level ("low"), not forwarded
        verbatim as reasoning_effort="none" (which the endpoint would
        reject) and not silently omitted either (issue #65233 established
        this same remap pattern for CustomProfile's own none-handling)."""
        extra_body, top_level = alibaba_coding_plan_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": ""},
        )
        assert top_level.get("reasoning_effort") == "low"

    def test_effort_none_string_remaps_to_lowest_level(self, alibaba_coding_plan_profile):
        """Same remap must apply when effort is explicitly the string
        "none" (however enabled is set), not just when enabled=False."""
        extra_body, top_level = alibaba_coding_plan_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
        )
        assert top_level.get("reasoning_effort") == "low"

    def test_medium_and_xhigh_pass_through_unchanged(self, alibaba_coding_plan_profile):
        for level in ("medium", "xhigh"):
            _, top_level = alibaba_coding_plan_profile.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": level},
            )
            assert top_level.get("reasoning_effort") == level

    def test_no_thinking_related_extra_body_emitted(self, alibaba_coding_plan_profile):
        """This profile forwards reasoning purely as a top-level field --
        it must not also emit an extra_body.think/thinking entry (that's
        the Ollama-style flag other profiles use, not applicable here)."""
        extra_body, _ = alibaba_coding_plan_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "low"},
        )
        assert "think" not in extra_body
        assert "thinking" not in extra_body
