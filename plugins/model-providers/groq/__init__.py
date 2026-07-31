"""Groq provider profile.

Groq's OpenAI-compatible endpoint"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class GroqProfile(ProviderProfile):
    """Groq profile: clamp reasoning to top-level "none"/"default" and
    avoid emitting any Ollama or nested-reasoning fields.
    """

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (extra_body_additions, top_level_kwargs).

        - If `reasoning_config` is absent: omit both fields (let Groq use its default).
        - If `enabled` is False or effort == "none": set reasoning_effort = "none".
        - Otherwise: set reasoning_effort = "default" (Groq only accepts "default"/"none").
        - Never emit `extra_body.think` or a nested `reasoning` object.
        """
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if reasoning_config and isinstance(reasoning_config, dict):
            _enabled = reasoning_config.get("enabled", True)
            _raw_effort = (reasoning_config.get("effort") or "").strip().lower()

            if _enabled is False or _raw_effort == "none":
                top_level["reasoning_effort"] = "none"
            else:

                top_level["reasoning_effort"] = "default"

        return extra_body, top_level


groq = GroqProfile(
    name="groq",
    aliases=("api.groq.com",),
    display_name="Groq",
    description="Groq — OpenAI-compatible endpoint",
    signup_url="https://www.groq.ai/",
    env_vars=("GROQ_API_KEY", "GROQ_BASE_URL"),
    base_url="https://api.groq.com/openai/v1",
    auth_type="api_key",
)

register_provider(groq)