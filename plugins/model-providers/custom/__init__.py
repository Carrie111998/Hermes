"""Custom / Ollama (local) provider profile.

Covers any endpoint registered as provider="custom", including local
Ollama instances and OpenAI-compatible reasoning endpoints (GLM-5.2 on
Volcengine ARK, vLLM, llama.cpp). Key quirks:
  - ollama_num_ctx → extra_body.options.num_ctx (local context window)
  - local Ollama without the ``thinking`` capability → omit reasoning_effort
    entirely (sending it 400s: '"model" does not support thinking')
  - reasoning_config disabled → top-level reasoning_effort="none"
    (Ollama /v1/chat/completions ignores think=False — ollama#14820)
    + extra_body.think = False for /api/chat and proxies
  - reasoning_config enabled + effort → top-level reasoning_effort
    (the native OpenAI-compatible format GLM/ARK expect; unset omits it
    so the endpoint's server default applies)
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


def _endpoint_is_ollama(
    *,
    ollama_num_ctx: int | None = None,
    base_url: str | None = None,
) -> bool:
    """True when this custom endpoint is an Ollama server, not vLLM/ARK/etc.

    Local Ollama rejects ``reasoning_effort`` on instruct models with HTTP 400
    ('does not support thinking'). GLM-5.2 on ARK still needs that field, so
    we only gate it when we already know the backend is Ollama.
    """
    if ollama_num_ctx:
        return True
    url = (base_url or "").strip().lower()
    if ":11434" in url:
        return True
    try:
        from utils import base_url_host_matches

        if base_url_host_matches(url, "ollama.com"):
            return True
    except Exception:
        pass
    return False


class CustomProfile(ProviderProfile):
    """Custom/Ollama local provider — think=false and num_ctx support."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        ollama_num_ctx: int | None = None,
        supports_reasoning: bool = False,
        base_url: str | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Ollama context window
        if ollama_num_ctx:
            options = extra_body.get("options", {})
            options["num_ctx"] = ollama_num_ctx
            extra_body["options"] = options

        # Local Ollama instruct models (qwen3-coder, gemma, …) 400 if we send
        # reasoning_effort at all — including "none". Omit the field unless
        # /api/show advertised the thinking capability. Non-Ollama custom
        # endpoints (GLM-5.2/ARK, vLLM) keep the un-gated path below.
        if _endpoint_is_ollama(
            ollama_num_ctx=ollama_num_ctx, base_url=base_url
        ) and not supports_reasoning:
            return extra_body, top_level

        # Reasoning / thinking control for custom OpenAI-compatible endpoints
        # (GLM-5.2 on Volcengine ARK, vLLM, Ollama, llama.cpp, …).
        #
        #   - disabled  → extra_body.think = False (Ollama's thinking-off flag)
        #   - enabled + effort set → TOP-LEVEL reasoning_effort string, the
        #     format GLM-5.2/ARK and other OpenAI-compatible reasoning APIs
        #     expect (GLM documents "high" and "max"; "max" is its default).
        #   - enabled + no effort  → omit both, so the endpoint applies its own
        #     server-side default (do NOT force a level the user didn't pick).
        #
        # We deliberately do NOT emit ``think=True`` on enable: it is an
        # Ollama-only flag and thinking is already server-default-on for these
        # backends, so forcing it risks a 400 on GLM/vLLM endpoints that don't
        # recognize it. Mirrors the DeepSeek/Zai profile precedent.
        if reasoning_config and isinstance(reasoning_config, dict):
            _effort = (reasoning_config.get("effort") or "").strip().lower()
            _enabled = reasoning_config.get("enabled", True)
            if _effort == "none" or _enabled is False:
                # Ollama's /v1/chat/completions silently ignores
                # extra_body.think (only /api/chat honours it — ollama#14820)
                # but respects the top-level reasoning_effort field, so both
                # are needed to actually stop a thinking-capable model from
                # reasoning (#25758). Endpoints that recognize neither simply
                # ignore them.
                top_level["reasoning_effort"] = "none"
                extra_body["think"] = False
            elif _effort:
                # Clamp the internal ladder onto the widest OpenAI-compatible
                # wire vocabulary (shared policy in agent.reasoning_effort) —
                # GLM/ARK, vLLM and SGLang all top out at "max"; forwarding
                # "ultra" verbatim is a guaranteed 400 (#89503).
                from agent.reasoning_effort import (
                    OPENAI_COMPAT_WIRE_EFFORTS,
                    clamp_effort,
                )

                top_level["reasoning_effort"] = clamp_effort(
                    _effort, OPENAI_COMPAT_WIRE_EFFORTS
                )

        return extra_body, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Custom/Ollama: base_url is user-configured; fetch if set."""
        if not (base_url or self.base_url):
            return None
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)


custom = CustomProfile(
    name="custom",
    aliases=(
        "ollama",
        "local",
        "vllm",
        "llamacpp",
        "llama.cpp",
        "llama-cpp",
    ),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    # Without this, no max_tokens is sent and Ollama falls back to its internal
    # num_predict=128, truncating responses after a few tokens (#39281). This is
    # only a floor used when the user hasn't set model.max_tokens — they can
    # override per-model — so we set it generously rather than lowballing it.
    default_max_tokens=65536,
)

register_provider(custom)
