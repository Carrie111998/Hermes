"""SambaNova provider profile.

SambaNova provides an OpenAI-compatible API endpoint for their AI models.
This profile enables users to use SambaNova as a model provider in Hermes.
"""

from __future__ import annotations

from providers import register_provider
from providers.base import ProviderProfile

# SambaNova API endpoints
SAMBA_NOVA_BASE_URL = "https://api.sambanova.ai/v1"
SAMBA_NOVA_MODELS_URL = "https://api.sambanova.ai/v1/models"


sambanova = ProviderProfile(
    name="sambanova",
    env_vars=("SAMBANOVA_API_KEY",),
    display_name="SambaNova",
    description="SambaNova — AI acceleration platform with OpenAI-compatible API",
    signup_url="https://cloud.sambanova.ai/",
    base_url=SAMBA_NOVA_BASE_URL,
    models_url=SAMBA_NOVA_MODELS_URL,
    fallback_models=(
        "Meta-Llama-3.3-70B-Instruct",
        "Qwen2.5-72B-Instruct",
    ),
    supports_vision=False,
)

register_provider(sambanova)