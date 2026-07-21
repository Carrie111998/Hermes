"""Ant Ling provider profile.

Ant Ling's OpenAI-compatible endpoint (``https://api.ant-ling.com/v1``)
speaks standard ``chat/completions``. The one provider-specific knob is the
``reasoning`` request-body field:

    {"extra_body": {"reasoning": {"effort": "high" | "xhigh"}}}

Ant Ling exposes exactly two enabled effort levels — ``high`` (default,
balanced agent workflows) and ``xhigh`` (math / research / hard logic,
significantly more tokens). Hermes' richer effort scale
(``low``/``medium``/``minimal``/``high``/``xhigh``/``max``/``ultra``) is
collapsed onto those two so the user's effort preference actually reaches
the model instead of being silently dropped:

  - ``xhigh`` / ``max`` / ``ultra``  →  ``"xhigh"``
  - every other enabled effort       →  ``"high"``  (Ant Ling's minimum)

When no reasoning preference is set (``reasoning_config is None``) or
reasoning is explicitly disabled, the field is omitted so the server
default (``high``) applies — matching behavior for users who never touch
the reasoning controls.

Which models may receive the field is controlled by an **allow-list**
(``_REASONING_MODEL_MARKERS``), not a deny-list. Reasoning is opt-in per
model: the field is only sent to a model documented as accepting it, and a
future release that gains reasoning (e.g. ``ling-3.0-flash``) is added to
the allow-list deliberately rather than assumed compatible. This is the
safe default for a private, collaboratively-evolved API — sending an
undocumented field risks a 400 or a silent no-op that wastes the user's
explicit effort choice. Markers are matched against a normalised model
name, so ``Ring-2.6-1T`` / ``ring_2_6_1t`` / ``ant-ling/Ring-2.6-1T`` all
resolve the same way.
"""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


# Model-name markers for Ant Ling models documented as accepting the
# ``reasoning`` request field. Allow-list on purpose: reasoning support is a
# per-model opt-in, so the field is only ever sent to a model listed here. A
# future release that gains reasoning (e.g. ``ling-3.0-flash``) is added here
# deliberately — sending the field to an unlisted model risks a 400 or a silent
# no-op, which is worse than (temporarily) dropping the user's effort choice.
#
# Each marker is pre-normalised the same way ``_normalize_model`` normalises the
# input (lower-case, ``_``/``.`` → ``-``). Substring match (not startswith) so
# vendor-prefixed forms (``ant-ling/Ring-2.6-1T``) and dated variants are
# covered by a single entry.
_REASONING_MODEL_MARKERS = tuple(
    m.lower().replace("_", "-").replace(".", "-")
    for m in ("ring-2.6-1t",)
)

def _normalize_model(model: str | None) -> str:
    """Lower-case and collapse separator variants so name-matching is tolerant.

    ``Ring-2.6-1T`` / ``ring-2.6-1t`` / ``ring_2_6_1t`` and any relay
    vendor-prefix (``ant-ling/Ring-2.6-1T``) all collapse to a single normal
    form, so the allow-list is matched once regardless of spelling.
    """
    return (model or "").strip().lower().replace("_", "-").replace(".", "-")

def _model_supports_reasoning(model: str | None) -> bool:
    """True only for Ant Ling models explicitly allow-listed as reasoning-capable.

    Today only the ``Ring-2.6-1T`` family accepts ``reasoning.effort``. Future
    Ling releases (e.g. ``ling-3.0-flash``) must be added to
    ``_REASONING_MODEL_MARKERS`` once their reasoning support is documented —
    the allow-list is intentionally exclusive so the field is never sent to a
    model that may reject or ignore it.

    ``None``/empty model → False: without a resolved model the relay may route
    to any backend, and the only safe default is to omit the field.
    """
    token = _normalize_model(model)
    if not token:
        return False
    return any(marker in token for marker in _REASONING_MODEL_MARKERS)

def _reasoning_effort(reasoning_config: dict | None) -> str | None:
    """Map Hermes reasoning effort onto Ant Ling's native ``high``/``xhigh``.

    Ant Ling models that accept ``reasoning.effort`` support only two enabled
    levels. ``xhigh``/``max``/``ultra`` request the top tier; every other
    enabled effort clamps to ``high`` (Ant Ling's minimum thinking level).
    When reasoning is explicitly disabled, or no effort preference is
    supplied, the server default is left untouched (return None).
    """
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return None

    effort = (reasoning_config.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None

    if effort in {"xhigh", "max", "ultra"}:
        return "xhigh"
    # low / medium / minimal / high all clamp to Ant Ling's minimum: high.
    return "high"


class AntLingProfile(ProviderProfile):
    """Ant Ling — ``reasoning.effort`` via extra_body for allow-listed models."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Only allow-listed reasoning-capable models receive the field. An
        # unlisted model (and an unresolved model) never does — reasoning is
        # opt-in per model so we never send an undocumented field.
        if not _model_supports_reasoning(model):
            return extra_body, top_level

        effort = _reasoning_effort(reasoning_config)
        if effort is not None:
            extra_body["reasoning"] = {"effort": effort}

        return extra_body, top_level

ant_ling = AntLingProfile(
    name="ant-ling",
    aliases=("antling",),
    env_vars=("ANT_LING_API_KEY",),
    display_name="Ant Ling",
    description="Ant Ling (Ling & Ring models, direct API)",
    signup_url="https://developer.ant-ling.com/",
    fallback_models=(
        "Ling-2.6-flash",
        "Ring-2.6-1T",
        "Ling-2.6-1T",
    ),
    base_url="https://api.ant-ling.com/v1",
    auth_type="api_key",
)

register_provider(ant_ling)
