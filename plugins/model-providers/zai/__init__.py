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

Vision routing is endpoint-aware: the Coding Plan endpoint serves
``glm-4.5v`` but not the general API's ``glm-5v-turbo`` (429 code 1311 —
not in subscription).  :meth:`ZaiProfile.default_vision_model` picks the
vision default for the billing pool the request actually lands on (issue
#92817).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from providers import register_provider
from providers.base import ProviderProfile

_GLM_VERSION_RE = re.compile(r"^glm-(\d+)(?:\.(\d+))?")

#: Z.AI / BigModel hosts — the single source of truth for Z.AI host facts.
#: The shared aux client does NOT keep its own copy: ``agent.auxiliary_client.
#: _is_zai_host_url`` delegates to :meth:`ZaiProfile.is_zai_host_url` (its
#: local host pair is only a fallback for contexts where this plugin is not
#: loaded).  Add any new Z.AI/BigModel endpoint here so every consumer picks
#: it up in one edit (#92817).  Host-anchored matching so a lookalike host
#: (``api.z.ai.evil.example.com``) or a path marker on a gateway URL never
#: triggers endpoint-specific routing.
_ZAI_HOSTS = ("api.z.ai", "open.bigmodel.cn")


def _is_zai_host_url(base_url: str | None) -> bool:
    """True when *base_url* points at a Z.AI / BigModel host.

    Host-anchored (never path-anchored) and subdomain-tolerant — mirrors
    ``utils.base_url_host_matches`` semantics — so a lookalike host
    (``api.z.ai.evil.example.com``) or a path marker on a gateway URL never
    matches, while a genuine ``*.api.z.ai`` endpoint still does.  The aux
    client reaches this through :meth:`ZaiProfile.is_zai_host_url`.
    """
    url = str(base_url or "").strip()
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _ZAI_HOSTS)


def _is_zai_coding_endpoint(base_url: str | None) -> bool:
    """True when *base_url* is a Z.AI Coding Plan endpoint.

    Covers the OpenAI-wire form ``/api/coding/paas/v4`` and the
    Anthropic-wire form ``/api/anthropic`` (whose OpenAI sibling is the
    coding endpoint — see ``_to_openai_base_url``).  The general API
    (``/api/paas/v4``) is billed independently and is NOT a coding endpoint.
    """
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host not in _ZAI_HOSTS:
        return False
    return (
        "/coding/" in url
        or url.endswith("/coding")
        or url.endswith("/api/anthropic")
    )


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
    """Detect GLM-5.2/5.3 (reasoning_effort-capable) across alias spellings.

    Covers the canonical ``glm-5.2``/``glm-5.3`` plus the ``glm-5-2`` /
    ``glm-5p2`` variants seen on relays (Fireworks ``glm-5p2``, etc.) and any
    vendor-prefixed form (``z-ai/glm-5.2``, ``zai-org-glm-5-2``).  GLM-5.3
    uses the same base model as 5.2 (post-training gains only) and exposes
    the same ``reasoning_effort`` knob (verified live 2026-08-14: the
    coding-plan endpoint accepts ``reasoning_effort: high`` for glm-5.3).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return any(
        token in m
        for token in ("glm-5.2", "glm-5-2", "glm-5p2", "glm-5.3", "glm-5-3", "glm-5p3")
    )


def _is_glm_5_3(model: str | None) -> bool:
    """Detect GLM-5.3 specifically — it has a wider effort vocabulary.

    5.2 accepts only ``high``/``max``; 5.3 accepts a graded
    ``low``/``medium``/``high``/``max`` scale (verified live, issue #91789),
    so effort mapping must pick the vocabulary per model.
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return any(token in m for token in ("glm-5.3", "glm-5-3", "glm-5p3"))


def _glm_5_2_reasoning_effort(
    reasoning_config: dict | None, *, model: str | None = None
) -> str | None:
    """Map Hermes reasoning effort onto GLM's native vocabulary.

    GLM-5.2 supports two enabled effort levels (``high``/``max``);
    GLM-5.3 supports the graded ``low``/``medium``/``high``/``max`` scale.
    ``xhigh``/``max``/``ultra`` request the top tier; anything below the
    model's floor clamps to that floor. When reasoning is explicitly
    disabled, or no effort preference is supplied, the server default is
    left untouched.
    """
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return None

    effort = (reasoning_config.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None

    # Per-model vocabulary declared in agent.reasoning_effort; xhigh rounds
    # up to max on both. 5.2 cannot think less than high; 5.3 accepts a
    # graded scale down to low (issue #91789).
    from agent.reasoning_effort import (
        GLM52_EFFORTS,
        GLM52_OVERRIDES,
        GLM53_EFFORTS,
        GLM53_OVERRIDES,
        clamp_effort,
    )

    if _is_glm_5_3(model):
        efforts, overrides, floor = GLM53_EFFORTS, GLM53_OVERRIDES, "low"
    else:
        efforts, overrides, floor = GLM52_EFFORTS, GLM52_OVERRIDES, "high"

    clamped = clamp_effort(effort, efforts, overrides)
    return clamped if clamped in efforts else floor


class ZaiProfile(ProviderProfile):
    """Z.AI / GLM — extra_body.thinking on/off + GLM-5.2 reasoning_effort."""

    def is_zai_host_url(self, base_url: str | None = None) -> bool:
        """Host check delegated from ``agent.auxiliary_client._is_zai_host_url``.

        Public so the shared aux client asks the profile instead of
        re-deriving Z.AI host facts — when a new Z.AI/BigModel endpoint is
        added to :data:`_ZAI_HOSTS`, aux picks it up with no change there.
        """
        return _is_zai_host_url(base_url)

    def default_vision_model(self, base_url: str | None = None) -> str | None:
        """Vision default for the billing pool *base_url* lands on.

        The Coding Plan endpoint (``/api/coding/paas/v4``, or its
        Anthropic-wire form ``/api/anthropic``) does not serve
        ``glm-5v-turbo`` — it answers 429 code 1311 ("not in subscription").
        It serves ``glm-4.5v``, which is included in the plan (measured live,
        issue #92817).  The general API keeps ``glm-5v-turbo``.
        """
        if _is_zai_coding_endpoint(base_url):
            return "glm-4.5v"
        return "glm-5v-turbo"

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
            effort = _glm_5_2_reasoning_effort(reasoning_config, model=model)
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
