"""DeepSeek provider profile.

DeepSeek's V4 family defaults to thinking-mode ON when ``extra_body.thinking``
is unset.  The API then returns ``reasoning_content`` and starts enforcing
the contract that subsequent turns echo it back; combined with how Hermes
replays history this lands on the notorious HTTP 400
``reasoning_content must be passed back`` error after the first tool call
(#15700, #17212, #17825).

This profile overrides :meth:`build_api_kwargs_extras` to produce the exact
wire shape DeepSeek's OpenAI-compat endpoint expects:

    {"reasoning_effort": "<high|max>",
     "extra_body": {"thinking": {"type": "enabled" | "disabled"}}}

Effort vocabulary is the official DeepSeek one — ``off|high|max`` on the
wire.  ``off`` (and the user-facing alias ``none``) disable thinking mode
entirely, so no ``reasoning_effort`` is sent.  ``low`` / ``medium`` /
``minimal`` normalize to ``high`` (DeepSeek maps them server-side to high
anyway) and ``xhigh`` / ``ultra`` normalize to ``max``.  Any other value
raises ``ValueError`` *before* any I/O so a bad config fails loudly instead
of silently degrading to the server default.

Non-thinking models (``deepseek-v3-*`` variants) are left as no-ops so we
don't perturb the V3 wire format.

The legacy aliases ``deepseek-chat`` / ``deepseek-reasoner`` were retired on
2026-07-24.  Use ``deepseek-v4-flash`` or ``deepseek-v4-pro``; Hermes remaps
the retired IDs in ``hermes_cli.model_normalize``.
"""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


def _model_supports_thinking(model: str | None) -> bool:
    """DeepSeek thinking-capable model families.

    Currently covers the V4 family (``deepseek-v4-pro``, ``deepseek-v4-flash``,
    and any future ``deepseek-v4-*`` variants).  Retired aliases are remapped
    before requests leave Hermes, so they are not listed here.
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if m.startswith("deepseek-v") and not m.startswith("deepseek-v3"):
        # deepseek-v4-*, deepseek-v5-*, etc. — every V4+ generation has
        # thinking. v3 explicitly excluded.
        return True
    return False


# Effort values that disable thinking mode entirely (official "off" plus the
# user-facing "none" alias).  No reasoning_effort goes on the wire.
_DEEPSEEK_DISABLE_EFFORTS = frozenset({"none", "off"})

# User-facing effort → official DeepSeek wire vocabulary (off|high|max).
# DeepSeek's server maps medium/xhigh to high (api-docs.deepseek.com, thinking
# mode guide); low is normalized to high here too so the wire only ever
# carries the official off|high|max set.  xhigh/ultra have historically
# resolved to max in Hermes and keep doing so.
_DEEPSEEK_EFFORT_ALIASES = {
    "minimal": "high",
    "low": "high",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
    "ultra": "max",
}


class DeepSeekProfile(ProviderProfile):
    """DeepSeek — extra_body.thinking + top-level reasoning_effort."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not _model_supports_thinking(model):
            # V3 / unknown — leave wire format untouched, current behavior.
            return extra_body, top_level

        # Determine enabled/disabled.  Default is enabled to match DeepSeek's
        # API default; the API requires this to be set explicitly to avoid the
        # reasoning_content echo trap on subsequent turns.
        enabled = True
        if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
            enabled = False

        if not enabled:
            # Thinking off → explicit disabled marker (DeepSeek defaults to ON
            # when the field is absent) and NO reasoning_effort on the wire —
            # DeepSeek rejects effort alongside disabled thinking.
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, top_level

        effort = reasoning_config.get("effort") if isinstance(reasoning_config, dict) else None
        if effort is None:
            effort = ""
        if not isinstance(effort, str):
            effort = str(effort)
        effort = effort.strip().lower()

        if not effort:
            # Unset → omit reasoning_effort so DeepSeek applies its server
            # default (currently high).
            extra_body["thinking"] = {"type": "enabled"}
            return extra_body, top_level

        if effort in _DEEPSEEK_DISABLE_EFFORTS:
            # "off" / "none" → thinking disabled, no effort on the wire.
            # Matches the official vocabulary: off is not a wire effort, it
            # is the disabled switch.
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, top_level

        mapped = _DEEPSEEK_EFFORT_ALIASES.get(effort)
        if mapped is None:
            # Invalid effort must fail BEFORE any I/O with a clear error —
            # never silently degrade to the server default.
            raise ValueError(
                f"DeepSeek does not support reasoning effort {effort!r}; expected one of "
                "off, none, minimal, low, medium, high, xhigh, max, ultra "
                "(wire vocabulary: off|high|max)"
            )

        extra_body["thinking"] = {"type": "enabled"}
        top_level["reasoning_effort"] = mapped
        return extra_body, top_level


deepseek = DeepSeekProfile(
    name="deepseek",
    aliases=("deepseek-chat",),
    env_vars=("DEEPSEEK_API_KEY",),
    display_name="DeepSeek",
    description="DeepSeek — native DeepSeek API",
    signup_url="https://platform.deepseek.com/",
    fallback_models=(
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ),
    base_url="https://api.deepseek.com/v1",
    default_aux_model="deepseek-v4-flash",
)

register_provider(deepseek)
