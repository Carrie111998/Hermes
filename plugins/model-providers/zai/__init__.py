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

GLM-5.2 and GLM-5.3 (same base model; 5.3 is post-training gains only)
expose a native ``reasoning_effort`` knob on the OpenAI-compatible endpoint.
GLM-5.2 supports two enabled levels — ``high`` and ``max`` (per Z.AI /
BigModel docs). GLM-5.3 accepts the full graded ladder ``low``..``max``,
live-verified 2026-08-21/22 on the coding-plan endpoint (issue #91789):
every level accepted, no HTTP 400, monotonic reasoning-token scaling
(low 4 / medium 11 / high 98 / max 125 vs. 69 unset). Hermes' richer effort
scale is clamped onto each family's supported levels so the user's effort
preference actually reaches the model instead of being silently dropped —
or escalated (a ``low`` ask on 5.3 is served ``low``, not clamped up to
``high``).
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


def _glm_native_effort_knob(
    model: str | None,
) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """GLM models with a native ``reasoning_effort`` knob, and their levels.

    Returns ``(supported_levels, overrides)`` for the model's family, or
    ``None`` when the model has no native knob.

    - GLM-5.2: exactly two enabled levels — ``high`` (its minimum thinking
      level) and ``max`` (per Z.AI / BigModel docs).
    - GLM-5.3: same base model, post-training gains only, but the graded
      ladder it accepts is wider: ``low``..``max``, live-verified
      2026-08-21/22 on the coding-plan endpoint (issue #91789) — every level
      accepted, no HTTP 400, monotonic reasoning-token scaling.

    Covers the canonical ``glm-5.2`` / ``glm-5.3`` plus the ``glm-5-2`` /
    ``glm-5p2`` / ``glm-5-3`` / ``glm-5p3`` variants seen on relays
    (Fireworks ``glm-5p2``, etc.) and any vendor-prefixed form
    (``z-ai/glm-5.2``, ``zai-org-glm-5-2``).
    """
    from agent.reasoning_effort import (
        GLM52_EFFORTS,
        GLM52_OVERRIDES,
        GLM53_EFFORTS,
        GLM53_OVERRIDES,
    )

    m = (model or "").strip().lower()
    if not m:
        return None
    if any(token in m for token in ("glm-5.3", "glm-5-3", "glm-5p3")):
        return GLM53_EFFORTS, GLM53_OVERRIDES
    if any(token in m for token in ("glm-5.2", "glm-5-2", "glm-5p2")):
        return GLM52_EFFORTS, GLM52_OVERRIDES
    return None


def _glm_native_reasoning_effort(
    reasoning_config: dict | None,
    supported: tuple[str, ...],
    overrides: dict[str, str],
) -> str | None:
    """Map Hermes reasoning effort onto a GLM family's native levels.

    The requested effort is clamped onto the family's supported levels via
    the shared :func:`agent.reasoning_effort.clamp_effort` policy: keep a
    supported level verbatim, otherwise take the nearest weaker level, and
    only when nothing weaker exists take the family's weakest level (GLM-5.2's
    floor is ``high``; GLM-5.3's is ``low``). When reasoning is explicitly
    disabled, or no effort preference is supplied, the server default is left
    untouched.
    """
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return None

    effort = (reasoning_config.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None

    from agent.reasoning_effort import clamp_effort

    clamped = clamp_effort(effort, supported, overrides)
    return clamped if clamped in supported else supported[0]


class ZaiProfile(ProviderProfile):
    """Z.AI / GLM — extra_body.thinking on/off + GLM-5.2/5.3 reasoning_effort."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        knob = _glm_native_effort_knob(model)
        if not _model_supports_thinking(model) and knob is None:
            return extra_body, top_level

        # Only emit when the user expressed a preference; omitting the field
        # keeps the server default (enabled) exactly as before.
        if isinstance(reasoning_config, dict):
            enabled = reasoning_config.get("enabled") is not False
            extra_body["thinking"] = {"type": "enabled" if enabled else "disabled"}

        if knob is not None:
            supported, overrides = knob
            effort = _glm_native_reasoning_effort(reasoning_config, supported, overrides)
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
