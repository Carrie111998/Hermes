"""Impossibl AI API provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


impossibl = ProviderProfile(
    name="impossibl",
    aliases=("imp",),
    display_name="Impossibl AI API",
    description="Impossibl AI API — one API for models across providers",
    signup_url="https://impossibl.com/",
    env_vars=("IMPOSSIBL_API_KEY",),
    base_url="https://api.impossibl.com/v1",
    models_url="https://api.impossibl.com/v1/models",
    auth_type="api_key",
    # Cheap, long-context model for compression and other side tasks.
    default_aux_model="moonshotai/kimi-k3",
    # Small/current agentic models verified against the public live catalog.
    fallback_models=(
        "moonshotai/kimi-k3",
        "deepseek/deepseek-v4-flash",
        "xiaomi/mimo-v2.5",
        "google/gemini-3.5-flash-lite",
        "openai/gpt-5.4-mini",
    ),
)

register_provider(impossibl)
