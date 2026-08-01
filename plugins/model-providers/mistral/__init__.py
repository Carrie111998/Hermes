"""Mistral AI provider profile — production-grade.

Wire-format quirks of https://api.mistral.ai/v1 that the generic ``custom``
profile (and the earlier v1 of this plugin) get wrong:

1. ``reasoning_effort`` accepts ONLY ``none`` or ``high``. A global Hermes
   ``medium`` is rejected with HTTP 400 / code 3051.
2. ``reasoning_effort`` is rejected ENTIRELY on models without the reasoning
   capability — even ``none`` (HTTP 400 / code 3051, "reasoning_effort is not
   enabled for this model"). Verified on codestral-latest, 2026-08. So the
   field must only be emitted for known reasoning-capable families; for every
   other model we stay silent and let the API apply its default.
3. The ``extra_body.think`` flag (Ollama quirk emitted by the generic custom
   profile) is rejected with HTTP 422 ``extra_forbidden`` — never emitted.
4. With ``reasoning_effort=high`` the API returns ``message.content`` as a
   *list* of parts (``{"type": "thinking", ...}`` then ``{"type": "text"}``);
   with ``none`` (or the field omitted) it is a plain string. Hermes' stream
   transport (``chat_completion_helpers``, v0.19.0) appends ``delta.content``
   to a list and ``"".join()``s it — a list content chunk raises
   ``TypeError: sequence item 0: expected str instance, list found``, which
   surfaces as "Stream interrupted" / "Response remained truncated". Until
   Hermes learns to flatten content-part arrays in streams, the profile
   therefore NEVER emits ``reasoning_effort=high``: positive efforts are
   mapped to *omit* (Mistral's server default — verified to return plain
   string content), and only explicit disable maps to ``none``. Flip
   ``_SEND_HIGH_REASONING`` to True after a Hermes upgrade that handles
   list-content streams.
5. Image input is accepted only by vision-capable models (HTTP 400 "Image
   input is not enabled for this model" otherwise). ``supports_vision`` is
   therefore True provider-wide; non-vision models fall back to the
   auxiliary vision model via Hermes' image routing.
"""

from __future__ import annotations

import logging
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile, _profile_user_agent

logger = logging.getLogger(__name__)

# Set to True once Hermes' chat-completions stream parser can flatten
# Mistral's list-content deltas (see docstring point 4). While False,
# positive reasoning efforts are omitted from the request instead of being
# sent as "high" — which would crash the stream with a TypeError.
_SEND_HIGH_REASONING: bool = False

# Model families that expose the reasoning capability (verified against the
# 2026-08 live catalog: capabilities.reasoning == true). For every other
# model, sending reasoning_effort at all — even "none" — returns HTTP 400.
# Prefix match so dated revisions (mistral-medium-2604, ...) and -latest
# aliases are covered.
_REASONING_PREFIXES: tuple[str, ...] = (
    "mistral-small",
    "mistral-medium",
    "magistral",
    "labs-leanstral",
    "mistral-vibe-cli",
)

# Dated revisions of reasoning families that do NOT expose the reasoning
# capability (verified against the 2026-08 catalog: no capabilities.reasoning).
# The API rejects reasoning_effort on them with HTTP 400 / code 3051.
_NON_REASONING_EXACT: frozenset[str] = frozenset(
    {"mistral-medium-2505", "mistral-medium-2508"}
)

# Curated chat models for the /model picker when the live catalog fetch
# fails. All support tool calling. Ordered by preference.
FALLBACK_MODELS: tuple[str, ...] = (
    "mistral-large-latest",
    "mistral-small-latest",
    "mistral-medium-latest",
    "codestral-latest",
    "devstral-latest",
    "ministral-8b-latest",
    "ministral-3b-latest",
)


def _model_supports_reasoning(model: str | None) -> bool:
    """True when *model* is in a reasoning-capable Mistral family.

    Unknown/out-of-catalog models return False on purpose: the safe default
    is to never send ``reasoning_effort`` (it 400s on non-reasoning models),
    so reasoning simply stays on the server default for those.
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if m in _NON_REASONING_EXACT:
        return False
    return any(m.startswith(prefix) for prefix in _REASONING_PREFIXES)


class MistralProfile(ProviderProfile):
    """Mistral AI — reasoning_effort in {none, high}, only on reasoning models."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not _model_supports_reasoning(model):
            # Non-reasoning family (codestral, devstral, mistral-code-*,
            # ministral, mistral-large, voxtral-small, ...): sending
            # reasoning_effort — even "none" — is HTTP 400. Leave the wire
            # format untouched and let the API use its default.
            return extra_body, top_level

        if not reasoning_config or not isinstance(reasoning_config, dict):
            # No reasoning config at all — don't force anything.
            return extra_body, top_level

        enabled = reasoning_config.get("enabled", True)
        if enabled is False:
            top_level["reasoning_effort"] = "none"
            return extra_body, top_level

        effort = (reasoning_config.get("effort") or "").strip().lower()
        if effort in {"none", "false", "disabled"}:
            top_level["reasoning_effort"] = "none"
        elif effort and _SEND_HIGH_REASONING:
            # Mistral supports only "high" as a positive level — map
            # low/medium/high/xhigh all onto it. Disabled by default:
            # Hermes' stream parser crashes on Mistral's list-content
            # reasoning stream (see module docstring point 4).
            top_level["reasoning_effort"] = "high"
        # else: positive effort while _SEND_HIGH_REASONING is False → omit
        # the field entirely; Mistral's server default returns plain
        # string content, so the stream stays crash-free.

        return extra_body, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Live catalog filtered to chat-capable models.

        Mistral's /models endpoint also lists embeddings, OCR, moderation,
        TTS and audio-transcription models. Only ``capabilities.completion_chat``
        entries are usable through chat completions, so everything else is
        filtered out of the picker.
        """
        effective_base = base_url or self.base_url
        if not effective_base:
            return None
        url = effective_base.rstrip("/") + "/models"

        import json
        import urllib.request

        from hermes_cli.urllib_security import open_credentialed_url

        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _profile_user_agent())
        for k, v in self.default_headers.items():
            req.add_header(k, v)

        try:
            with open_credentialed_url(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            chat_models: list[str] = []
            for m in items:
                if not isinstance(m, dict) or "id" not in m:
                    continue
                caps = m.get("capabilities") or {}
                if caps.get("completion_chat"):
                    chat_models.append(m["id"])
            return chat_models or None
        except Exception as exc:
            logger.debug("fetch_models(mistral): %s", exc)
            return None

    def default_vision_model(self) -> str | None:
        """Best vision-capable chat model for auxiliary vision tasks."""
        return "mistral-large-latest"


mistral = MistralProfile(
    name="mistral",
    aliases=(),
    env_vars=("MISTRAL_API_KEY",),
    display_name="Mistral AI",
    description="Mistral AI — native Mistral API",
    signup_url="https://console.mistral.ai/",
    fallback_models=FALLBACK_MODELS,
    base_url="https://api.mistral.ai/v1",
    default_aux_model="mistral-small-latest",
    supports_vision=True,
)

register_provider(mistral)
