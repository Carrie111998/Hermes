"""Profile-local project context bindings with no filesystem paths."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from utils import atomic_json_write

_SLACK_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]+$")
_NOTION_PAGE_RE = re.compile(r"^[A-Za-z0-9-]{16,200}$")


class WorkspaceContextStore:
    def __init__(self, profile_home: Path) -> None:
        self.profile_home = Path(profile_home).expanduser().resolve()
        self.path = self.profile_home / "workspace-context.json"
        self._lock = threading.RLock()

    @staticmethod
    def _project_id(value: str) -> str:
        project_id = str(value or "").strip()
        if not project_id or len(project_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", project_id):
            raise ValueError("workspace project ID is invalid")
        return project_id

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"projects": {}, "version": 1}
        except (OSError, ValueError) as exc:
            raise ValueError("workspace context store is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("projects"), dict):
            raise ValueError("workspace context store is invalid")
        return payload

    def get(self, project_id: str) -> dict[str, list[str]]:
        key = self._project_id(project_id)
        with self._lock:
            raw = self._read()["projects"].get(key) or {}
        return {
            "notion_page_ids": [str(value) for value in raw.get("notion_page_ids") or []],
            "slack_channel_ids": [str(value) for value in raw.get("slack_channel_ids") or []],
        }

    def set(
        self,
        project_id: str,
        *,
        notion_page_ids: list[str],
        slack_channel_ids: list[str],
    ) -> dict[str, list[str]]:
        key = self._project_id(project_id)
        notion = sorted({str(value).strip() for value in notion_page_ids if str(value).strip()})
        slack = sorted({str(value).strip().upper() for value in slack_channel_ids if str(value).strip()})
        if len(notion) > 100 or len(slack) > 40:
            raise ValueError("workspace context binding limit exceeded")
        if any(not _NOTION_PAGE_RE.fullmatch(value) for value in notion):
            raise ValueError("Notion context bindings must be page IDs")
        if any(not _SLACK_CHANNEL_RE.fullmatch(value) for value in slack):
            raise ValueError("Slack context bindings must be channel IDs")
        value = {"notion_page_ids": notion, "slack_channel_ids": slack}
        with self._lock:
            payload = self._read()
            payload["projects"][key] = value
            self.profile_home.mkdir(parents=True, exist_ok=True)
            atomic_json_write(self.path, payload, indent=2)
            if os.name != "nt":
                self.path.chmod(0o600)
        return value
