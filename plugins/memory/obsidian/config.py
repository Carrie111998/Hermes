"""Parsning av obsidian-providerns config (ren)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_VAULT = "/srv/dj/obsidian"
DEFAULT_TOP_K = 5
DEFAULT_EXCLUDES = (".git", ".obsidian", ".trash")


@dataclass(frozen=True)
class ObsidianConfig:
    vault_path: str = DEFAULT_VAULT
    top_k: int = DEFAULT_TOP_K
    exclude_dirs: tuple[str, ...] = DEFAULT_EXCLUDES
    pinned: tuple[str, ...] = ()


def build_obsidian_config(cfg: "Mapping[str, Any] | None") -> ObsidianConfig:
    cfg = cfg or {}
    excludes = cfg.get("exclude_dirs")
    pinned = cfg.get("pinned")
    return ObsidianConfig(
        vault_path=str(cfg.get("vault_path", DEFAULT_VAULT)),
        top_k=int(cfg.get("top_k", DEFAULT_TOP_K)),
        exclude_dirs=tuple(excludes) if excludes is not None else DEFAULT_EXCLUDES,
        pinned=tuple(str(p) for p in pinned) if pinned is not None else (),
    )
