"""Hetzner AI Inference provider profile."""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class HetznerProfile(ProviderProfile):
    """Hetzner Qwen thinking control through Hermes reasoning settings."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(reasoning_config, dict):
            return {}, {}

        return {
            "chat_template_kwargs": {
                "enable_thinking": reasoning_config.get("enabled") is not False,
            }
        }, {}


hetzner = HetznerProfile(
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
