"""Artifact manifest model + tolerant parsing.

A manifest is an optional `<id>.json` sidecar next to an artifact's `<id>.html`.
Only `id` and `title` are meaningful-required; everything else has a default so a
bare HTML file with no sidecar still produces a valid Manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class Manifest:
    id: str
    title: str
    source: str = "unknown"
    created_at: str = ""
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    refresh_secs: Optional[int] = None
    data_endpoints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "source": self.source,
            "created_at": self.created_at, "tags": self.tags,
            "summary": self.summary, "refresh_secs": self.refresh_secs,
            "data_endpoints": self.data_endpoints,
        }


def _mtime_iso(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return datetime.now(timezone.utc).isoformat()


def parse_manifest(sidecar_path: Path, html_path: Optional[Path] = None) -> Manifest:
    """Parse a sidecar JSON manifest. Tolerant: missing/malformed sidecar derives
    id/title/created_at from the html file (or the sidecar stem)."""
    stem = (html_path.stem if html_path else sidecar_path.stem)
    derived_created = _mtime_iso(html_path) if html_path else datetime.now(timezone.utc).isoformat()

    data: dict = {}
    if sidecar_path.exists():
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}

    return Manifest(
        id=str(data.get("id") or stem),
        title=str(data.get("title") or stem),
        source=str(data.get("source") or "unknown"),
        created_at=str(data.get("created_at") or derived_created),
        tags=list(data.get("tags") or []),
        summary=str(data.get("summary") or ""),
        refresh_secs=data.get("refresh_secs"),
        data_endpoints=list(data.get("data_endpoints") or []),
    )
