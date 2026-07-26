"""ai& (aiand) provider profile.

ai& serves a curated catalog of open frontier models (DeepSeek, Kimi, GLM,
Qwen, Gemma, gpt-oss) through an OpenAI-compatible chat-completions endpoint
at ``https://api.aiand.com/v1``. Model IDs are vendor-prefixed slugs matching
the upstream org names, e.g. ``deepseek-ai/deepseek-v4-flash`` or
``moonshotai/kimi-k2.7-code``.

The provider is listed on models.dev under the ``aiand`` slug, so context
windows, pricing, and capability metadata resolve through the standard
models.dev path (see ``agent/models_dev.py::PROVIDER_TO_MODELS_DEV``).
"""

from providers import register_provider
from providers.base import ProviderProfile

aiand = ProviderProfile(
    name="aiand",
    aliases=("ai&", "ai-and"),
    display_name="ai&",
    description="ai& — open frontier models, OpenAI-compatible direct API",
    signup_url="https://docs.aiand.com/",
    env_vars=("AIAND_API_KEY",),
    base_url="https://api.aiand.com/v1",
    auth_type="api_key",
    # Auxiliary model for cheap side tasks (compaction, title generation,
    # session search): DeepSeek V4 Flash — $0.15/$0.25 per Mtok, 1M context.
    default_aux_model="deepseek-ai/deepseek-v4-flash",
    # Curated agentic (tool-calling) models, shown when the live /models
    # fetch fails. Mirrors the models.dev catalog for the aiand provider.
    fallback_models=(
        "moonshotai/kimi-k2.7-code",
        "moonshotai/kimi-k2.6",
        "zai-org/glm-5.2",
        "zai-org/glm-5.1",
        "deepseek-ai/deepseek-v4-pro",
        "deepseek-ai/deepseek-v4-flash",
        "qwen/qwen3.6-27b",
        "openai/gpt-oss-120b",
    ),
)

register_provider(aiand)
