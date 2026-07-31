"""Groq provider profile.

Groq's OpenAI-compatible API (api.groq.com) rejects fields that the generic
``custom`` profile emits:

  - ``extra_body.think``  → 400 "property 'think' is unsupported"
  - ``extra_body.reasoning``  → 400 "property 'reasoning' is unsupported"

Groq accepts only a top-level ``reasoning_effort`` of ``"none"`` or
``"default"``.  This profile overrides :meth:`build_api_kwargs_extras` so
that reasoning control uses the correct wire shape and the generic
``extra_body.reasoning`` fallback in ``_build_call_kwargs`` is suppressed.

Ref: https://github.com/NousResearch/hermes-agent/issues/75089
"""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class GroqProfile(ProviderProfile):
    """Groq — top-level reasoning_effort only, no think/reasoning body."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not isinstance(reasoning_config, dict):
            return extra_body, top_level

        enabled = reasoning_config.get("enabled", True)
        effort = (reasoning_config.get("effort") or "").strip().lower()

        if enabled is False or effort == "none":
            # Groq only accepts "none" or "default" for reasoning_effort.
            top_level["reasoning_effort"] = "none"
        elif effort:
            # Map non-standard effort levels to Groq's "default".
            # Groq does not support low/medium/high/max — only "default".
            top_level["reasoning_effort"] = "default"
        # If enabled but no effort set, omit so the server default applies.

        return extra_body, top_level


groq = GroqProfile(
    name="groq",
    env_vars=("GROQ_API_KEY",),
    display_name="Groq",
    description="Groq — fast inference on LPU hardware",
    signup_url="https://console.groq.com/",
    fallback_models=(
        "llama-3.3-70b-versatile",
        "mixtral-8x7b-32768",
    ),
    base_url="https://api.groq.com/openai/v1",
)

register_provider(groq)
