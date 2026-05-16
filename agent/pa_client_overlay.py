"""Client-facing identity overlays for PA constitutions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml


IDENTITY_OVERLAY_FIELDS = frozenset(
    {
        "display_name",
        "voice_register",
        "greeting_style",
        "signature",
        "refer_to_self",
    }
)


def load_client_overlay(path: str | Path) -> Mapping[str, Any]:
    """Load a client overlay YAML mapping."""
    overlay_path = Path(path)
    try:
        loaded = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact YAML errors are library-owned
        raise ValueError(f"Failed to load PA client overlay {overlay_path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise ValueError(f"{overlay_path}: overlay must be a mapping")
    return loaded


def merge_client_overlay(
    *,
    agent_name: str,
    identity: Mapping[str, Any],
    client: Mapping[str, Any],
    overlay: Mapping[str, Any] | None,
    source: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Merge a client overlay into identity-visible constitution fields."""
    if overlay is None:
        return agent_name, dict(identity), dict(client)
    if not isinstance(overlay, Mapping):
        raise ValueError(f"{source}: overlay must be a mapping")

    merged_identity = dict(identity)
    merged_client = dict(client)
    next_agent_name = agent_name

    raw_identity = overlay.get("identity", {})
    if raw_identity is not None:
        if not isinstance(raw_identity, Mapping):
            raise ValueError(f"{source}.identity: must be a mapping")
        merged_identity.update(dict(raw_identity))

    raw_client = overlay.get("client", {})
    if raw_client is not None:
        if not isinstance(raw_client, Mapping):
            raise ValueError(f"{source}.client: must be a mapping")
        merged_client.update(dict(raw_client))

    for field_name in IDENTITY_OVERLAY_FIELDS:
        if field_name in overlay:
            merged_identity[field_name] = overlay[field_name]

    raw_agent_name = overlay.get("agent_name")
    if raw_agent_name is not None:
        if not isinstance(raw_agent_name, str) or not raw_agent_name.strip():
            raise ValueError(f"{source}.agent_name: must be a non-empty string")
        next_agent_name = raw_agent_name

    return next_agent_name, merged_identity, merged_client
