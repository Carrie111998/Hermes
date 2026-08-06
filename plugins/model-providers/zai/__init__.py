"""ZAI / GLM provider profile.

Z.AI's GLM-4.5-and-later chat models default to thinking-mode ON when the
request omits ``thinking``.  Hermes' ``reasoning_config = {"enabled": False}``
was previously a silent no-op on this route — the base profile emits nothing,
so users who turned thinking off (desktop toggle, ``/reasoning none``,
``reasoning_effort: none``/``false`` in config.yaml) kept burning thinking
tokens on every turn.

:meth:`ZaiProfile.build_api_kwargs_extras` translates the Hermes reasoning
config into the wire shape Z.AI's OpenAI-compat endpoint expects:

    {"extra_body": {"thinking": {"type": "enabled" | "disabled"}}}

When no reasoning preference is set (``reasoning_config is None``) the field
is omitted so the server default applies, matching prior behavior.  GLM
models before 4.5 (e.g. ``glm-4-9b``) don't accept ``thinking`` and are left
untouched.

GLM-5.2 additionally exposes a native ``reasoning_effort`` knob with exactly
two enabled levels — ``high`` and ``max`` — on the OpenAI-compatible endpoint
(per Z.AI / BigModel docs).  Hermes' richer effort scale is collapsed onto
those two so the user's effort preference actually reaches the model instead
of being silently dropped.

──────────────────────────────────────────────────────────────────────────
Known operational failure modes (see plugins/platforms/discord/ISSUES.md):
──────────────────────────────────────────────────────────────────────────

D-002 — Z.AI "Insufficient balance" billing 429 (code 1113)
    Z.AI mislabels account-balance exhaustion as HTTP 429 (standards-
    correct would be 402). Body shape:
        {"error": {"code": "1113",
                   "message": "Insufficient balance or no resource package.
                                Please recharge."}}
    The 429 branch of ``agent/error_classifier.py::_classify_by_status``
    now checks ``_BILLING_PATTERNS`` and code ``1113`` is in
    ``_BILLING_ERROR_CODES``, so this classifies as ``billing`` (non-
    retryable, rotate+fallback) instead of ``rate_limit`` (retryable,
    burns 3 retries).

    Reproduction:
        1. Set GLM_API_KEY to a key with zero account balance
        2. Run: hermes chat --provider zai --model glm-5
        3. Send any message — observe 0 wasted retries (was 3 before fix)
    Cross-ref: CRITICAL-ISSUES.md #16, RCA-B, D-002.

D-004 — GLM-5/5.2 "hangs without doing anything" mid-think
    GLM-5.2 ships with thinking ON by default (see ``build_api_kwargs_extras``
    below — emits ``extra_body.thinking.type=enabled`` when no preference
    is set). During extended thinking, GLM emits no content tokens for
    several minutes before producing its first output. The default
    HERMES_STREAM_STALE_TIMEOUT of 180s and the httpx socket read timeout
    of 120s both fire BEFORE GLM-5.2 finishes thinking, tearing down a
    healthy reasoning stream mid-think.

    Fix: ``agent/reasoning_timeouts.py::_REASONING_STALE_TIMEOUT_FLOORS``
    now has ``("glm-5.2", 300)``, ``("glm-5", 240)``, ``("glm-4.6", 180)``,
    ``("glm-4.5", 180)`` — see that table for the floor rationale.

    Reproduction of fix:
        >>> from agent.reasoning_timeouts import get_reasoning_stale_timeout_floor
        >>> get_reasoning_stale_timeout_floor("glm-5.2")
        300.0
        >>> get_reasoning_stale_timeout_floor("glm-4-9b") is None  # non-thinking
        True
    Cross-ref: CRITICAL-ISSUES.md #18, RCA-C, D-004.

Standing rule for future maintainers:
    When a new GLM variant is added here, also add it to
    ``_REASONING_STALE_TIMEOUT_FLOORS`` in ``agent/reasoning_timeouts.py``
    if it ships with thinking ON by default. The provider profile and the
    timeout table are maintained separately — there is no automated check.
"""

from __future__ import annotations

import re
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

_GLM_VERSION_RE = re.compile(r"^glm-(\d+)(?:\.(\d+))?")


def _model_supports_thinking(model: str | None) -> bool:
    """GLM thinking-capable model families: glm-4.5 and later (4.5, 4.6, 5…)."""
    m = (model or "").strip().lower()
    match = _GLM_VERSION_RE.match(m)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return (major, minor) >= (4, 5)


def _is_glm_5_2(model: str | None) -> bool:
    """Detect GLM-5.2 across the alias spellings providers use.

    Covers the canonical ``glm-5.2`` plus the ``glm-5-2`` / ``glm-5p2``
    variants seen on relays (Fireworks ``glm-5p2``, etc.) and any
    vendor-prefixed form (``z-ai/glm-5.2``, ``zai-org-glm-5-2``).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return any(token in m for token in ("glm-5.2", "glm-5-2", "glm-5p2"))


def _glm_5_2_reasoning_effort(reasoning_config: dict | None) -> str | None:
    """Map Hermes reasoning effort onto GLM-5.2's native ``high``/``max``.

    GLM-5.2 only supports two enabled effort levels. ``xhigh``/``max``/``ultra``
    request the top tier; everything else that is enabled requests ``high``
    (its minimum thinking level). When reasoning is explicitly disabled, or
    no effort preference is supplied, the server default is left untouched.
    """
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return None

    effort = (reasoning_config.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None

    if effort in {"xhigh", "max", "ultra"}:
        return "max"
    # low / medium / minimal / high all clamp to GLM-5.2's minimum: high.
    return "high"


class ZaiProfile(ProviderProfile):
    """Z.AI / GLM — extra_body.thinking on/off + GLM-5.2 reasoning_effort."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not _model_supports_thinking(model) and not _is_glm_5_2(model):
            return extra_body, top_level

        # Only emit when the user expressed a preference; omitting the field
        # keeps the server default (enabled) exactly as before.
        if isinstance(reasoning_config, dict):
            enabled = reasoning_config.get("enabled") is not False
            extra_body["thinking"] = {"type": "enabled" if enabled else "disabled"}

        if _is_glm_5_2(model):
            effort = _glm_5_2_reasoning_effort(reasoning_config)
            if effort is not None:
                top_level["reasoning_effort"] = effort

        return extra_body, top_level


zai = ZaiProfile(
    name="zai",
    aliases=("glm", "z-ai", "z.ai", "zhipu"),
    env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
    display_name="Z.AI (GLM)",
    description="Z.AI / GLM — Zhipu AI models",
    signup_url="https://z.ai/",
    fallback_models=(
        "glm-5.2",
        "glm-5",
        "glm-4-9b",
    ),
    base_url="https://api.z.ai/api/paas/v4",
    default_aux_model="glm-4.5-flash",
)

register_provider(zai)
