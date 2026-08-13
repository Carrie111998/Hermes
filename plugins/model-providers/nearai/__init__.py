"""NEAR AI Cloud provider profile.

NEAR AI Cloud is an OpenAI-compatible inference gateway that runs models
inside TEEs for verifiable private inference, fronting both frontier
(Anthropic, OpenAI, Gemini) and open (Qwen, GLM, DeepSeek, Kimi) models.
The live catalog at ``https://cloud-api.near.ai/v1/models`` is public (no
key required) and follows the standard ``{"data": [{"id": ...}]}`` shape,
so the base ``fetch_models`` implementation covers model discovery.
"""

from providers import register_provider
from providers.base import ProviderProfile


nearai = ProviderProfile(
    name="nearai",
    aliases=("near-ai", "near"),
    display_name="NEAR AI",
    description="NEAR AI Cloud — verifiable private inference (TEE), frontier + open models",
    signup_url="https://cloud.near.ai",
    env_vars=("NEAR_AI_API_KEY", "NEAR_AI_BASE_URL"),
    base_url="https://cloud-api.near.ai/v1",
    auth_type="api_key",
    # Catalog spans providers with different output ceilings — let each
    # upstream apply its own cap rather than flattening them here.
    default_max_tokens=None,
    supports_vision=True,
    default_aux_model="openai/gpt-5-mini",
    # Curated tool-calling models, shown only when the live fetch fails.
    fallback_models=(
        "anthropic/claude-sonnet-5",
        "openai/gpt-5.4",
        "google/gemini-3.5-flash",
        "moonshotai/kimi-k2.6",
        "z-ai/glm-5.2",
        "qwen/qwen3.7-max",
        "deepseek-ai/DeepSeek-V4-Flash",
    ),
)

register_provider(nearai)
