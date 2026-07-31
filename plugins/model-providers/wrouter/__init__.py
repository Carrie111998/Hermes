"""WRouter provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

# WRouter (https://wrouter.ai) is an OpenAI-compatible AI gateway that
# aggregates Anthropic, OpenAI, Google and other upstreams behind a single
# /v1 endpoint. Standard api_key auth, so hermes_cli/auth.py, models.py and
# doctor.py pick it up automatically from the registry.
wrouter = ProviderProfile(
    name="wrouter",
    aliases=("wr",),
    env_vars=("WROUTER_API_KEY",),
    display_name="WRouter",
    description="WRouter — OpenAI-compatible AI gateway",
    signup_url="https://wrouter.ai",
    base_url="https://wrouter.ai/v1",
    hostname="wrouter.ai",
    supports_vision=True,
    fallback_models=(
        "claude-sonnet-5",
        "claude-opus-4-8",
        "gpt-5",
        "gemini-2.5-pro",
        "deepseek-v3",
    ),
)

register_provider(wrouter)
