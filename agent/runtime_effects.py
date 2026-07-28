"""Closed host-only contracts for effects that cross agent-turn boundaries.

Runtime effects are trusted transport metadata.  They are never model/tool
text, never plugin context, and never persisted on a transcript message.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


RUNTIME_EFFECT_SCHEMA = "hermes.runtime-effect.v1"
ISOLATED_WORKSPACE_MAY_HAVE_CHANGED = (
    "isolated_workspace_may_have_changed.v1"
)
_RUNTIME_EFFECT_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "workspace_lease_authority",
        "baseline_edit_generation",
    }
)
_MAX_AUTHORITY_LENGTH = 4096


class RuntimeEffectError(ValueError):
    """Raised when host runtime-effect metadata is malformed."""


def normalize_runtime_effect(value: Any) -> dict[str, Any]:
    """Validate and clone one exact runtime-effect record.

    ``baseline_edit_generation`` is nullable only because a failed
    dispatch-time status read must remain visible and fail closed later.
    """

    if not isinstance(value, Mapping) or set(value) != _RUNTIME_EFFECT_FIELDS:
        raise RuntimeEffectError("runtime_effect_fields_invalid")
    if value.get("schema") != RUNTIME_EFFECT_SCHEMA:
        raise RuntimeEffectError("runtime_effect_schema_invalid")
    if value.get("kind") != ISOLATED_WORKSPACE_MAY_HAVE_CHANGED:
        raise RuntimeEffectError("runtime_effect_kind_invalid")

    authority = value.get("workspace_lease_authority")
    if (
        not isinstance(authority, str)
        or not authority
        or authority != authority.strip()
        or authority == "default"
        or len(authority) > _MAX_AUTHORITY_LENGTH
    ):
        raise RuntimeEffectError("runtime_effect_authority_invalid")

    baseline = value.get("baseline_edit_generation")
    if baseline is not None and (
        type(baseline) is not int or baseline < 0
    ):
        raise RuntimeEffectError("runtime_effect_baseline_invalid")

    return {
        "schema": RUNTIME_EFFECT_SCHEMA,
        "kind": ISOLATED_WORKSPACE_MAY_HAVE_CHANGED,
        "workspace_lease_authority": authority,
        "baseline_edit_generation": baseline,
    }


def normalize_optional_runtime_effect(
    value: Any,
) -> Optional[dict[str, Any]]:
    """Validate an optional host envelope without treating absence as data."""

    if value is None:
        return None
    return normalize_runtime_effect(value)


def build_isolated_workspace_runtime_effect(
    workspace_lease_authority: str,
    baseline_edit_generation: int | None,
) -> dict[str, Any]:
    """Build one validated isolated-workspace effect."""

    return normalize_runtime_effect(
        {
            "schema": RUNTIME_EFFECT_SCHEMA,
            "kind": ISOLATED_WORKSPACE_MAY_HAVE_CHANGED,
            "workspace_lease_authority": workspace_lease_authority,
            "baseline_edit_generation": baseline_edit_generation,
        }
    )


__all__ = [
    "ISOLATED_WORKSPACE_MAY_HAVE_CHANGED",
    "RUNTIME_EFFECT_SCHEMA",
    "RuntimeEffectError",
    "build_isolated_workspace_runtime_effect",
    "normalize_optional_runtime_effect",
    "normalize_runtime_effect",
]
