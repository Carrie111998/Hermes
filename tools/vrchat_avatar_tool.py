"""Hermes agent tools: VRChat avatar OSC parameter catalog (OpenClaw hypura port)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.openclaw.paths import default_openclaw_config_path
from tools.openclaw.vrchat_avatar_registry import VrchatAvatarRegistry, catalog_to_dict
from tools.registry import registry


def _load_vrchat_control_config(config_path: str = "") -> tuple[dict[str, Any], Path]:
    """Merge optional openclaw.json vrchat.avatarControl with Hermes harness config."""
    merged: dict[str, Any] = {"vrchat": {"avatarControl": {}}}
    repo_root = Path.home()

    openclaw_cfg_path = Path(config_path).expanduser() if config_path else default_openclaw_config_path()
    if openclaw_cfg_path.is_file():
        try:
            raw = json.loads(openclaw_cfg_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict) and isinstance(raw.get("vrchat"), dict):
                merged["vrchat"] = raw["vrchat"]
            repo_root = openclaw_cfg_path.parent
        except (OSError, json.JSONDecodeError):
            pass

    try:
        # Canonical loader: managed-scope overlay + ${VAR} expansion.
        from hermes_cli.config import load_config_readonly

        hermes = load_config_readonly()
        harness_block = hermes.get("harness", {}) if isinstance(hermes, dict) else {}
        if isinstance(harness_block.get("vrchat"), dict):
            merged.setdefault("vrchat", {})
            merged["vrchat"].setdefault("avatarControl", {})
            merged["vrchat"]["avatarControl"].update(
                harness_block["vrchat"].get("avatarControl", {})
            )
    except Exception:
        pass

    return merged, repo_root


def vrchat_avatar_catalog(avatar_id: str, config_path: str = "") -> str:
    config, repo_root = _load_vrchat_control_config(config_path)
    registry_impl = VrchatAvatarRegistry(repo_root, config)
    catalog = registry_impl.load_catalog(avatar_id)
    payload: dict[str, Any] = {
        "success": catalog is not None,
        "avatarId": avatar_id,
        "catalog": catalog_to_dict(catalog),
        "error": registry_impl.last_error,
    }
    return json.dumps(payload, ensure_ascii=False)


def vrchat_avatar_safe_parameters(avatar_id: str, config_path: str = "") -> str:
    config, repo_root = _load_vrchat_control_config(config_path)
    registry_impl = VrchatAvatarRegistry(repo_root, config)
    catalog = registry_impl.load_catalog(avatar_id)
    if catalog is None:
        return json.dumps(
            {"success": False, "avatarId": avatar_id, "error": registry_impl.last_error},
            ensure_ascii=False,
        )
    safe = [
        {
            "name": p.name,
            "role": p.inferredRole,
            "writable": p.writable,
            "input": p.input.address if p.input else None,
        }
        for p in catalog.parameters
        if p.safety == "safe" and p.writable
    ]
    return json.dumps(
        {"success": True, "avatarId": avatar_id, "safeParameters": safe, "count": len(safe)},
        ensure_ascii=False,
    )


registry.register(
    name="vrchat_avatar_catalog",
    toolset="vrchat",
    schema={
        "name": "vrchat_avatar_catalog",
        "description": (
            "Load the VRChat OSC parameter catalog for an avatar id from local OSC config "
            "(VRChat/LocalLow/OSC or configured oscConfigRoots). Classifies parameters as "
            "safe, blocked, or needs_review for agent-driven expression control."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "avatar_id": {
                    "type": "string",
                    "description": "VRChat avatar id (avtr_...).",
                },
                "config_path": {
                    "type": "string",
                    "description": "Optional openclaw.json path for oscConfigRoots overrides.",
                },
            },
            "required": ["avatar_id"],
        },
    },
    handler=lambda args, **kw: vrchat_avatar_catalog(
        args["avatar_id"],
        args.get("config_path", ""),
    ),
    emoji="🎭",
)

registry.register(
    name="vrchat_avatar_safe_parameters",
    toolset="vrchat",
    schema={
        "name": "vrchat_avatar_safe_parameters",
        "description": (
            "List writable OSC parameters classified as safe for the given avatar. "
            "Use before vrchat_avatar_param to avoid blocked system parameters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "avatar_id": {"type": "string", "description": "VRChat avatar id (avtr_...)."},
                "config_path": {"type": "string", "description": "Optional openclaw.json path."},
            },
            "required": ["avatar_id"],
        },
    },
    handler=lambda args, **kw: vrchat_avatar_safe_parameters(
        args["avatar_id"],
        args.get("config_path", ""),
    ),
    emoji="✅",
)
