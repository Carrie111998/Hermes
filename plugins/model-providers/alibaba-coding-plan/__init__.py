"""Alibaba Cloud Coding Plan provider profile.

Separate from the standard `alibaba` profile because it hits a different
endpoint (coding-intl.dashscope.aliyuncs.com) with a dedicated API key tier.
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class AlibabaCodingPlanProfile(ProviderProfile):
    """Alibaba Cloud Coding Plan — forwards reasoning_effort to the wire.

    The dashscope Qwen3 thinking models this endpoint serves (e.g.
    qwen3.8-max-preview) always have thinking ON and accept
    reasoning_effort: xhigh / medium / low (server default: xhigh) --
    they cannot disable thinking outright. Without this override, the
    base ProviderProfile.build_api_kwargs_extras() default (({}, {}))
    silently discarded the user's configured reasoning_effort with no
    error or warning (issue #77818): the setting parsed correctly and
    /reasoning echoed the change, but the value never reached the API
    request.
    """

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        top_level: dict[str, Any] = {}
        if reasoning_config and isinstance(reasoning_config, dict):
            effort = (reasoning_config.get("effort") or "").strip().lower()
            enabled = reasoning_config.get("enabled", True)
            if not enabled or effort == "none":
                # These models cannot disable thinking (always on) -- map
                # a disabled/none request to the lowest available level
                # instead of forwarding "none" or omitting the field,
                # which the endpoint would reject (same failure mode as
                # #65233 for CustomProfile's own none-handling).
                top_level["reasoning_effort"] = "low"
            elif effort:
                top_level["reasoning_effort"] = effort
            # effort unset (and enabled) -> omit the field; the server's
            # own default (xhigh) applies, matching the "don't force a
            # level the user didn't pick" precedent from CustomProfile.
        return {}, top_level


alibaba_coding_plan = AlibabaCodingPlanProfile(
    name="alibaba-coding-plan",
    aliases=("alibaba_coding", "alibaba-coding", "dashscope-coding"),
    display_name="Alibaba Cloud (Coding Plan)",
    description="Alibaba Cloud Coding Plan (Dedicated coding tier)",
    signup_url="https://help.aliyun.com/zh/model-studio/",
    env_vars=("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_CODING_PLAN_BASE_URL"),
    base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
    auth_type="api_key",
)

register_provider(alibaba_coding_plan)
