"""OpenAI Responses API provider profile."""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

class OpenAIResponsesProfile(ProviderProfile):
    """OpenAI provider using the new Responses API."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}
        
        # Enable Responses API settings that maximize benchmark and production
        # capabilities (ARC-AGI-3 scores tripled with these on GPT-5.6 Sol)
        extra_body["retained_reasoning"] = True
        extra_body["compaction"] = True

        if supports_reasoning and reasoning_config is not None:
            extra_body["reasoning"] = dict(reasoning_config)

        return extra_body, top_level


openai_responses = OpenAIResponsesProfile(
    name="openai-responses",
    aliases=("openai_responses",),
    display_name="OpenAI (Responses API)",
    description="OpenAI provider using the Responses API with retained reasoning and compaction.",
    api_mode="codex_responses",
    env_vars=("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    base_url="https://api.openai.com/v1",
)

register_provider(openai_responses)
