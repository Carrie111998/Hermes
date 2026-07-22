"""Obsidian-minnesprovider — FTS5-retrieval över valvet (Fas A).

Config in $HERMES_HOME/config.yaml (profile-scoped):
  plugins:
    obsidian:
      vault_path: /srv/dj/obsidian
      top_k: 5
      exclude_dirs: [".git", ".obsidian", ".trash"]
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from plugins.memory.obsidian.config import build_obsidian_config
from plugins.memory.obsidian.index import ObsidianIndex

logger = logging.getLogger(__name__)


def _load_plugin_config() -> dict:
    from hermes_cli.config import cfg_get
    from hermes_constants import get_hermes_home

    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml

        with open(config_path, encoding="utf-8-sig") as f:
            all_config = yaml.safe_load(f) or {}
        return cfg_get(all_config, "plugins", "obsidian", default={}) or {}
    except Exception as exc:
        logger.debug("obsidian plugin config load failed: %s", exc)
        return {}


class ObsidianMemoryProvider(MemoryProvider):
    def __init__(self, config: "Dict[str, Any] | None" = None) -> None:
        self._cfg = build_obsidian_config(config)
        self._index: "ObsidianIndex | None" = None
        self._db_path = ""

    @property
    def name(self) -> str:
        return "obsidian"

    def is_available(self) -> bool:
        # Local, stdlib-only. Available iff the vault dir exists (no network).
        return os.path.isdir(self._cfg.vault_path)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = kwargs.get("hermes_home") or os.path.expanduser("~/.hermes")
        self._db_path = os.path.join(hermes_home, "obsidian_index.db")
        self._index = ObsidianIndex(self._db_path)
        try:
            self._index.sync_vault(
                self._cfg.vault_path, exclude_dirs=self._cfg.exclude_dirs
            )
        except OSError as exc:
            logger.warning("obsidian vault sync failed: %s", exc)
            # vault unreadable — provider degrades to empty recall

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []  # Phase A: context-only, no tools

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._index is None or not query:
            return ""
        try:
            hits = self._index.search(query, top_k=self._cfg.top_k)
        except Exception as exc:
            logger.warning("obsidian prefetch failed: %s", exc)
            return ""
        if not hits:
            return ""
        blocks = []
        for h in hits:
            anchor = f"{h.path}#{h.heading}" if h.heading else h.path
            blocks.append(f"[[{anchor}]]\n{h.content}")
        return "## Från Obsidian-valvet\n\n" + "\n\n".join(blocks)

    def backup_paths(self) -> List[str]:
        return [self._db_path] if self._db_path else []


def register(ctx) -> None:
    """Registrera obsidian-providern med plugin-systemet."""
    config = _load_plugin_config()
    ctx.register_memory_provider(ObsidianMemoryProvider(config=config))
