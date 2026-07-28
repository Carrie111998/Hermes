"""Hetzner AI Inference provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


hetzner = ProviderProfile(
    name="hetzner",
    aliases=("hetzner-ai", "hetzner-inference"),
    display_name="Hetzner AI Inference",
    description="Hetzner AI Inference — OpenAI-compatible hosted open models",
    signup_url="https://console.hetzner.com/",
    env_vars=("HETZNER_API_KEY", "HETZNER_BASE_URL"),
    base_url="https://inference.hetzner.com/api/v1",
    auth_type="api_key",
    supports_vision=True,
    default_aux_model="Qwen/Qwen3.6-35B-A3B-FP8",
    fallback_models=("Qwen/Qwen3.6-35B-A3B-FP8",),
)

register_provider(hetzner)
