"""Obsidian-minnesprovider — FTS5-retrieval och säkert write-back.

Config in $HERMES_HOME/config.yaml (profile-scoped):
  plugins:
    obsidian:
      vault_path: /srv/dj/obsidian
      top_k: 5
      exclude_dirs: [".git", ".obsidian", ".trash"]
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from agent.redact import redact_sensitive_text
from agent.memory_provider import MemoryProvider
from plugins.memory.obsidian.chunker import strip_frontmatter
from plugins.memory.obsidian.config import build_obsidian_config
from plugins.memory.obsidian.index import ObsidianIndex
from tools.registry import tool_error, tool_result
from utils import atomic_replace

logger = logging.getLogger(__name__)

_PINNED_NOTE_LIMIT = 4000
_REMEMBER_SCHEMA = {
    "name": "obsidian_remember",
    "description": (
        "Save a durable fact or lesson to the configured Obsidian vault. "
        "Use only for information worth remembering across sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Fact or lesson to save."},
            "title": {"type": "string", "description": "Optional note title."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional Obsidian tags.",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
}


def _safe_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:80] or "memory"


def _inside_vault(vault: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(vault.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_gitignored(vault: Path, relative_path: str) -> bool:
    if (vault / ".git").exists():
        try:
            command = [
                "git", "-C", str(vault), "check-ignore", "--no-index",
                "--quiet", "--", relative_path,
            ]
            checked = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=5,
            )
            if checked.returncode in (0, 1):
                return checked.returncode == 0
            logger.warning("obsidian git check-ignore failed with code %s", checked.returncode)
            return True
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("obsidian git check-ignore failed", exc_info=True)
            return True
    gitignore = vault / ".gitignore"
    if not gitignore.is_file():
        return False
    try:
        from pathspec import PathSpec

        spec = PathSpec.from_lines(
            "gitignore", gitignore.read_text(encoding="utf-8").splitlines()
        )
        return spec.match_file(relative_path) or spec.match_file(
            relative_path.split("/", 1)[0] + "/"
        )
    except (OSError, UnicodeError, ValueError):
        logger.warning("obsidian could not evaluate vault .gitignore", exc_info=True)
        return True


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
        self._sync_stop = threading.Event()
        self._sync_thread: "threading.Thread | None" = None

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
            self._sync_once()
        except OSError as exc:
            logger.warning("obsidian vault sync failed: %s", exc)
            # vault unreadable — provider degrades to empty recall
        if self._cfg.sync_interval_minutes > 0:
            self._sync_stop.clear()
            self._sync_thread = threading.Thread(
                target=self._sync_loop,
                name="obsidian-vault-sync",
                daemon=True,
            )
            self._sync_thread.start()

    def _sync_once(self) -> dict:
        if self._index is None:
            return {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}
        return self._index.sync_vault(
            self._cfg.vault_path, exclude_dirs=self._cfg.exclude_dirs
        )

    def _sync_loop(self) -> None:
        interval_seconds = self._cfg.sync_interval_minutes * 60
        while not self._sync_stop.wait(interval_seconds):
            try:
                summary = self._sync_once()
                if any(summary[key] for key in ("added", "updated", "deleted")):
                    logger.info("obsidian vault re-sync: %s", summary)
            except Exception as exc:
                logger.warning("obsidian vault re-sync failed: %s", exc)

    def system_prompt_block(self) -> str:
        vault = Path(self._cfg.vault_path).resolve()
        blocks: list[str] = []
        for relative in self._cfg.pinned:
            note = vault / relative
            if not _inside_vault(vault, note) or not note.is_file():
                continue
            try:
                content = strip_frontmatter(note.read_text(encoding="utf-8")).strip()
            except (OSError, UnicodeError) as exc:
                logger.warning("obsidian pinned note unreadable (%s): %s", relative, exc)
                continue
            if not content:
                continue
            content = redact_sensitive_text(
                content, force=True, redact_url_credentials=True
            )
            if len(content) > _PINNED_NOTE_LIMIT:
                content = content[:_PINNED_NOTE_LIMIT].rstrip() + "\n[trunkerad]"
            blocks.append(f"### {relative}\n{content}")
        if not blocks:
            return ""
        return "## Pinnat minne från Obsidian\n\n" + "\n\n".join(blocks)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [_REMEMBER_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "obsidian_remember":
            return tool_error(f"Unknown tool: {tool_name}")
        return self._remember(args)

    def _remember(self, args: Dict[str, Any]) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            return tool_error("content is required")
        scrubbed = redact_sensitive_text(
            content, force=True, redact_url_credentials=True
        )
        secret_redacted = scrubbed != content
        raw_title = str(args.get("title") or "").strip()
        title = redact_sensitive_text(raw_title, force=True) if raw_title else ""
        if not title:
            title = scrubbed.splitlines()[0][:80].strip() or "Hermes memory"
        raw_tags = args.get("tags") or []
        if not isinstance(raw_tags, list):
            return tool_error("tags must be an array")
        tags = [
            redact_sensitive_text(str(tag), force=True).strip()
            for tag in raw_tags
            if str(tag).strip()
        ]

        vault = Path(self._cfg.vault_path).resolve()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        relative = Path("hermes") / f"{_safe_slug(title)}-{stamp}.md"
        target = vault / relative
        relative_posix = relative.as_posix()
        if not _inside_vault(vault, target):
            return tool_error("refusing to write outside the Obsidian vault")
        if _is_gitignored(vault, relative_posix):
            return tool_error("refusing to write to a gitignored vault path")

        created = datetime.now(timezone.utc).isoformat()
        tag_lines = "\n".join(f"  - {json.dumps(tag, ensure_ascii=False)}" for tag in tags)
        frontmatter = f"---\ncreated: {created}\nsource: hermes\ntags:"
        frontmatter += f"\n{tag_lines}" if tag_lines else " []"
        note_text = f"{frontmatter}\n---\n\n# {title}\n\n{scrubbed}\n"

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".obsidian-memory-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(note_text)
                fh.flush()
                os.fsync(fh.fileno())
            atomic_replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        if self._index is not None:
            self._index.upsert_note(relative_posix, note_text, target.stat().st_mtime)
        return tool_result(
            path=relative_posix,
            secret_redacted=secret_redacted,
            message=(
                "Saved to Obsidian; one or more secrets were redacted."
                if secret_redacted
                else "Saved to Obsidian."
            ),
        )

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

    def shutdown(self) -> None:
        self._sync_stop.set()
        thread = self._sync_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._sync_thread = None
        if self._index is not None:
            self._index.close()
            self._index = None


def register(ctx) -> None:
    """Registrera obsidian-providern med plugin-systemet."""
    config = _load_plugin_config()
    ctx.register_memory_provider(ObsidianMemoryProvider(config=config))
