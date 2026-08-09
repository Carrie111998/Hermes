"""Safe-default configuration for the Phase 1 observe-only daemon."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from plugins.agentops.control.models import AuthorityMode


CONFIG_SCHEMA_VERSION = 1
DEFAULT_SPOOL_MAX_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class AgentOpsConfig:
    config_path: Path
    state_dir: Path
    sqlite_path: Path
    spool_dir: Path
    socket_path: Path
    event_spool_max_bytes: int
    default_authority: AuthorityMode
    global_write_enabled: bool
    safe_start_reasons: tuple[str, ...]


def default_config_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "agentops" / "agentops.yaml"


def _default_config(path: Path, reasons: tuple[str, ...]) -> AgentOpsConfig:
    state_dir = path.parent / "agentops-state" if path.name != "agentops.yaml" else path.parent
    return AgentOpsConfig(
        config_path=path,
        state_dir=state_dir,
        sqlite_path=state_dir / "state.db",
        spool_dir=state_dir / "event-spool",
        socket_path=state_dir / "agentops.sock",
        event_spool_max_bytes=DEFAULT_SPOOL_MAX_BYTES,
        default_authority=AuthorityMode.OBSERVE_ONLY,
        global_write_enabled=False,
        safe_start_reasons=reasons,
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _resolve_path(value: Any, fallback: Path, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        return fallback
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate)


def load_agentops_config(path: Path) -> AgentOpsConfig:
    """Load configuration without creating files and retain safe defaults on error."""
    path = Path(path).expanduser()
    if not path.exists():
        return _default_config(path, ("config_missing",))
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return _default_config(path, ("config_invalid",))
    if not isinstance(parsed, Mapping):
        return _default_config(path, ("config_invalid",))

    reasons: list[str] = []
    if parsed.get("schema_version", CONFIG_SCHEMA_VERSION) != CONFIG_SCHEMA_VERSION:
        reasons.append("unsupported_config_schema")

    storage = _as_mapping(parsed.get("storage"))
    control_plane = _as_mapping(parsed.get("control_plane"))
    safety = _as_mapping(parsed.get("safety"))
    default_state_dir = path.parent / "agentops-state"
    sqlite_path = _resolve_path(storage.get("sqlite_path"), default_state_dir / "state.db", path.parent)
    state_dir = sqlite_path.parent
    socket_path = _resolve_path(control_plane.get("socket_path"), state_dir / "agentops.sock", path.parent)
    if socket_path.parent != state_dir:
        reasons.append("unsafe_socket_path")
        socket_path = state_dir / "agentops.sock"
    spool_dir = state_dir / "event-spool"

    raw_spool_mb = control_plane.get("event_spool_max_mb", 256)
    if not isinstance(raw_spool_mb, int) or isinstance(raw_spool_mb, bool) or raw_spool_mb <= 0:
        reasons.append("invalid_spool_budget")
        spool_bytes = DEFAULT_SPOOL_MAX_BYTES
    else:
        spool_bytes = raw_spool_mb * 1024 * 1024

    requested_authority = safety.get("default_authority", AuthorityMode.OBSERVE_ONLY.value)
    if requested_authority != AuthorityMode.OBSERVE_ONLY.value:
        reasons.append("unsupported_authority_requested")
    if safety.get("global_write_enabled") is True:
        reasons.append("write_requested_but_disabled")

    return AgentOpsConfig(
        config_path=path,
        state_dir=state_dir,
        sqlite_path=sqlite_path,
        spool_dir=spool_dir,
        socket_path=socket_path,
        event_spool_max_bytes=spool_bytes,
        default_authority=AuthorityMode.OBSERVE_ONLY,
        global_write_enabled=False,
        safe_start_reasons=tuple(reasons),
    )
