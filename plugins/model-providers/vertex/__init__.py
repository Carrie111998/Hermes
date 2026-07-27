"""Google Vertex AI provider profile.

vertex: Gemini models via Google Cloud's OpenAI-compatible endpoint.

Auth is OAuth2 — short-lived access tokens minted from a service-account JSON
or Application Default Credentials (ADC), NOT a static API key. Token
resolution and refresh live in ``agent/vertex_adapter.py``; runtime_provider.py
calls it to obtain a fresh ``(token, base_url)`` pair, then hands the token to
the standard OpenAI client as ``api_key``. Because the wire format is the
OpenAI-compatible chat/completions surface, no message translation is needed —
the only Gemini-specific concern is the ``thinking_config`` reasoning hook,
which is emitted here exactly as the ``gemini`` provider does for its
OpenAI-compat subpath (``extra_body.google.thinking_config``).

``auth_type="vertex"`` marks this as an OAuth-token provider (resolved
specially, like bedrock's ``aws_sdk``) so it is never treated as an
api_key provider that would mistake a credentials-file path for a key.
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class VertexProfile(ProviderProfile):
    """Vertex AI — reuse Gemini's thinking_config translation for extra_body."""

    def prepare_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Sanitize messages for Vertex AI.

        Vertex AI's OpenAPI endpoint (specifically for models like
        gemini-3.6-flash and gemini-3.5-flash-lite) rejects requests with
        HTTP 400 INVALID_ARGUMENT if an assistant message in history contains
        both `tool_calls` and a non-empty `content` string.
        Clear `content` to "" on any assistant message that carries `tool_calls`.
        """
        needs_fix = False
        for msg in messages:
            if (
                isinstance(msg, dict)
                and msg.get("role") == "assistant"
                and msg.get("tool_calls")
                and msg.get("content")
            ):
                needs_fix = True
                break

        if not needs_fix:
            return messages

        sanitized = list(messages)
        for idx, msg in enumerate(messages):
            if (
                isinstance(msg, dict)
                and msg.get("role") == "assistant"
                and msg.get("tool_calls")
                and msg.get("content")
            ):
                copied = dict(msg)
                copied["content"] = ""
                sanitized[idx] = copied
        return sanitized

    def build_extra_body(
        self, *, session_id: str | None = None, **context: Any
    ) -> dict[str, Any]:
        """Emit ``extra_body.google.thinking_config`` for the OpenAI-compat
        Vertex surface, mirroring the ``gemini`` provider's behavior.
        """
        from agent.transports.chat_completions import (
            _build_gemini_thinking_config,
            _snake_case_gemini_thinking_config,
        )

        model = context.get("model") or ""
        reasoning_config = context.get("reasoning_config")

        raw_thinking_config = _build_gemini_thinking_config(model, reasoning_config)
        if not raw_thinking_config:
            return {}

        thinking_config = _snake_case_gemini_thinking_config(raw_thinking_config)
        if not thinking_config:
            return {}
        return {"google": {"thinking_config": thinking_config}}

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Vertex's OpenAI-compat endpoint has no ``/models`` listing route;
        model discovery is not available. The setup wizard ships a curated list.
        """
        return None


vertex = VertexProfile(
    name="vertex",
    aliases=("google-vertex", "vertex-ai", "gcp-vertex"),
    api_mode="chat_completions",
    env_vars=(),  # OAuth2 via service account / ADC — not a static key env var
    base_url="https://aiplatform.googleapis.com",  # real base_url computed at runtime
    auth_type="vertex",
    default_aux_model="google/gemini-3-flash-preview",
)

register_provider(vertex)
