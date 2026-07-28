"""Omnious provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


omnious = ProviderProfile(
    name="omnious",
    aliases=("omnious-market",),
    display_name="Omnious",
    description="Omnious — live-market routing for OpenAI-compatible inference",
    signup_url="https://www.omnious.xyz/integrations?side=customer",
    env_vars=("OMNIOUS_CREDIT_KEY",),
    base_url="https://api.omnious.xyz/v1",
    auth_type="api_key",
    default_aux_model="auto",
    fallback_models=(
        "auto",
        "glm-5.2",
        "kimi-k2.7-code",
        "kimi-k3",
        "minimax-m3",
    ),
)

register_provider(omnious)
