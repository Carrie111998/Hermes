"""MindsHub provider profile.

MindsHub (https://mindshub.ai) is a fully OpenAI/Anthropic-compatible LLM
inference gateway: one API key and one bill for Claude, GPT, Gemini, Kimi,
DeepSeek, Qwen, GLM, Grok, and more, addressed through short catalog
aliases (``sonnet``, ``opus``, ``kimi``, ``deepseek``, ``gpt``, ...) that
stay stable across upstream model upgrades. See
https://docs.mindshub.ai/inference/.

This profile only relies on the Chat Completions endpoint
(``https://api.mindshub.ai/v1/chat/completions``) — standard OpenAI wire
format, so no new adapter or ``api_mode`` is needed; streaming, tool /
function calling, and image inputs (``image_url`` parts, accepted on every
catalog chat model) all flow through Hermes' existing chat_completions
transport unmodified.

MindsHub forwards ``reasoning_effort`` as a plain top-level Chat
Completions field, so this profile passes the caller's effort straight
through with no per-model gating; unsupported levels are MindsHub's own
concern to handle, not something this integration needs to track.
"""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class MindsHubProfile(ProviderProfile):
    """MindsHub gateway — top-level ``reasoning_effort`` passthrough."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        top_level: dict[str, Any] = {}
        if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is not False:
            effort = str(reasoning_config.get("effort") or "").strip().lower()
            if effort and effort != "none":
                top_level["reasoning_effort"] = effort
        return {}, top_level


mindshub = MindsHubProfile(
    name="mindshub",
    aliases=("mindshub-ai",),
    display_name="MindsHub",
    description="MindsHub — one API key for Claude, GPT, Gemini, Kimi, DeepSeek, and more",
    signup_url="https://console.mindshub.ai",
    env_vars=("MINDSHUB_API_KEY", "MINDSHUB_BASE_URL"),
    base_url="https://api.mindshub.ai/v1",
    auth_type="api_key",
    # gpt-mini over haiku: aux calls (naming, routing, summarization) are
    # high-frequency and latency-insensitive, so price wins over ceiling
    # quality. gpt-mini is ~25-33% cheaper than haiku per MTok on MindsHub's
    # current price list and is a well-exercised general-purpose pick in
    # MindsHub's own docs; the even-cheaper gpt-nano/mindshub_air aliases
    # are undocumented outside the price list (mindshub_air is also a
    # shifting billing-bucket alias, not a fixed model), so they're too
    # unproven to default to.
    default_aux_model="gpt-mini",
    # Chat-capable catalog aliases only (embed-small is an embeddings-only
    # model and is intentionally excluded — see providers/base.py).
    fallback_models=(
        "sonnet",
        "opus",
        "fable",
        "haiku",
        "gpt",
        "gpt-codex",
        "gpt-mini",
        "gemini",
        "gemini-flash",
        "kimi",
        "deepseek",
        "qwen",
        "glm",
        "grok",
    ),
)

register_provider(mindshub)
