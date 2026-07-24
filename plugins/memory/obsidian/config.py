"""Parsning av obsidian-providerns config (ren)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_VAULT = "/srv/dj/obsidian"
DEFAULT_TOP_K = 5
DEFAULT_EXCLUDES = (".git", ".obsidian", ".trash")
DEFAULT_SYNC_INTERVAL_MINUTES = 5


def _string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return (value,)
        value = decoded if isinstance(decoded, list) else value
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class ObsidianConfig:
    vault_path: str = DEFAULT_VAULT
    top_k: int = DEFAULT_TOP_K
    exclude_dirs: tuple[str, ...] = DEFAULT_EXCLUDES
    pinned: tuple[str, ...] = ()
    sync_interval_minutes: float = DEFAULT_SYNC_INTERVAL_MINUTES


def build_obsidian_config(cfg: "Mapping[str, Any] | None") -> ObsidianConfig:
    cfg = cfg or {}
    excludes = cfg.get("exclude_dirs")
    pinned = cfg.get("pinned")
    try:
        sync_interval = max(0.0, float(cfg.get(
            "sync_interval_minutes", DEFAULT_SYNC_INTERVAL_MINUTES
        )))
    except (TypeError, ValueError):
        sync_interval = float(DEFAULT_SYNC_INTERVAL_MINUTES)
    return ObsidianConfig(
        vault_path=str(cfg.get("vault_path", DEFAULT_VAULT)),
        top_k=int(cfg.get("top_k", DEFAULT_TOP_K)),
        exclude_dirs=_string_tuple(excludes, DEFAULT_EXCLUDES),
        pinned=_string_tuple(pinned, ()),
        sync_interval_minutes=sync_interval,
    )
