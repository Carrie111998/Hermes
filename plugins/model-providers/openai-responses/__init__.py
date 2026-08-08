"""OpenAI Responses API provider profile.

Uses ``api_mode="codex_responses"`` against ``api.openai.com`` and enables
Responses API features that keep reasoning traces across tool calls and
context boundaries (``retained_reasoning``, ``compaction``).
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class OpenAIResponsesProfile(ProviderProfile):
    """OpenAI provider using the Responses API with retained reasoning."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # These fields are not typed on every openai SDK build; send them via
        # extra_body so ResponsesApiTransport / preflight forward them into the
        # request JSON body unchanged.
        del reasoning_config, supports_reasoning, context  # unused; transport owns effort
        extra_body: dict[str, Any] = {
            "retained_reasoning": True,
            "compaction": True,
        }
        return extra_body, {}


openai_responses = OpenAIResponsesProfile(
    name="openai-responses",
    aliases=("openai_responses",),
    display_name="OpenAI (Responses API)",
    description=(
        "OpenAI provider using the Responses API with retained reasoning "
        "and compaction."
    ),
    api_mode="codex_responses",
    env_vars=("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    base_url="https://api.openai.com/v1",
)

register_provider(openai_responses)
