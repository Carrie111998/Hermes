"""Fail-closed configuration loading for the Codex bridge."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_constants import get_hermes_home


logger = logging.getLogger("gateway.codex_bridge")

_DEFAULT_COMMAND_PREFIX = "/codex"


def _coerce_string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


@dataclass(frozen=True)
class CodexBridgeSettings:
    """Non-secret bridge settings loaded from ``config.yaml``."""

    enabled: bool = False
    allowed_origins: tuple[str, ...] = ("local",)
    workspace_allowlist: tuple[str, ...] = ()
    default_workspace: str | None = None
    command_prefix: str = _DEFAULT_COMMAND_PREFIX
    model: str | None = None
    sandbox: str = "workspace-write"
    collaboration_mode: str = "default"
    stale_recovery_seconds: int = 60

    @classmethod
    def from_mapping(cls, value: Any) -> "CodexBridgeSettings":
        data = value if isinstance(value, Mapping) else {}
        raw_prefix = data.get("command_prefix", _DEFAULT_COMMAND_PREFIX)
        prefix = str(raw_prefix).strip() or _DEFAULT_COMMAND_PREFIX
        if not prefix.startswith("/"):
            prefix = f"/{prefix}"
        sandbox = str(data.get("sandbox", "workspace-write")).strip().lower()
        if sandbox not in {"read-only", "workspace-write"}:
            sandbox = "workspace-write"
        collaboration_mode = str(
            data.get("collaboration_mode", "default")
        ).strip().lower()
        if collaboration_mode not in {"default", "plan"}:
            collaboration_mode = "default"
        try:
            stale_seconds = max(1, int(data.get("stale_recovery_seconds", 60)))
        except (TypeError, ValueError):
            stale_seconds = 60
        model = data.get("model")
        default_workspace = data.get("default_workspace")
        return cls(
            enabled=data.get("enabled") is True,
            allowed_origins=tuple(
                item.lower() for item in _coerce_string_list(data.get("allowed_origins"))
            )
            or ("local",),
            workspace_allowlist=_coerce_string_list(data.get("workspace_allowlist")),
            default_workspace=(
                str(default_workspace).strip() if default_workspace else None
            ),
            command_prefix=prefix,
            model=str(model).strip() if model else None,
            sandbox=sandbox,
            collaboration_mode=collaboration_mode,
            stale_recovery_seconds=stale_seconds,
        )


def load_codex_bridge_settings(config_path: Path | None = None) -> CodexBridgeSettings:
    """Load the feature flag without importing or starting the Codex SDK."""

    path = config_path or (get_hermes_home() / "config.yaml")
    if not path.exists():
        return CodexBridgeSettings()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not load Codex bridge config from %s: %s", path, exc)
        return CodexBridgeSettings()
    return CodexBridgeSettings.from_mapping(data.get("codex_bridge"))


def legacy_workers_auto_dispatch_enabled(config_path: Path | None = None) -> bool:
    """Return the explicit legacy-worker gate; absence always means off."""

    path = config_path or (get_hermes_home() / "config.yaml")
    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    section = data.get("legacy_hermes_workers")
    return isinstance(section, Mapping) and section.get("auto_dispatch_enabled") is True
