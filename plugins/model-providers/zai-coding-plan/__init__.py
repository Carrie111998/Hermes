"""Z.AI Coding Plan provider profile.

Separate from the standard ``zai`` profile because coding-plan subscriptions
(a) authenticate on a different endpoint — ``/api/coding/paas/v4`` — the
standard ``/api/paas/v4`` route rejects coding-plan keys with HTTP 429
``1113 Insufficient balance or no resource package``, and (b) z.ai's coding
plan is the subscription tier most agent users actually hold.

Mirrors the ``alibaba-coding-plan`` / ``kimi-coding`` pattern: a dedicated
selectable provider so coding-plan users get a working default without
hand-editing ``GLM_BASE_URL``. Subclasses ``ZaiProfile`` so the GLM
thinking / reasoning_effort wiring is shared with the standard profile.
"""

from plugins.model_providers.zai import ZaiProfile
from providers import register_provider

zai_coding_plan = ZaiProfile(
    name="zai-coding-plan",
    aliases=("zai-coding", "glm-coding", "z-ai-coding"),
    display_name="Z.AI / GLM (Coding Plan)",
    description="Z.AI Coding Plan (GLM subscription tier, api.z.ai/api/coding/paas/v4)",
    signup_url="https://z.ai/subscribe",
    env_vars=(
        "ZAI_CODING_PLAN_API_KEY",
        "GLM_CODING_PLAN_API_KEY",
        "ZAI_API_KEY",
    ),
    base_url="https://api.z.ai/api/coding/paas/v4",
    default_aux_model="glm-4.5-flash",
)

register_provider(zai_coding_plan)
