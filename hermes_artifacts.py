"""Helper any Hermes agent/cron imports to publish a self-contained HTML artifact
to the Hermes Canvas (~/.hermes/artifacts/). Writes <id>.html + <id>.json
atomically (temp file -> os.replace) so the viewer never reads a half-written file.

    import hermes_artifacts
    hermes_artifacts.emit(html, title="Ops Overview", source="scout",
                          tags=["ops"], refresh_secs=15,
                          data_endpoints=["/api/cron", "/api/events"])
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from hermes_constants import get_default_hermes_root

_SLUG = re.compile(r"[^a-z0-9]+")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def artifacts_dir() -> Path:
    return get_default_hermes_root() / "artifacts"


def _slugify(text: str) -> str:
    s = _SLUG.sub("-", text.strip().lower()).strip("-")
    return s or "artifact"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def emit(html: str, *, title: str, source: str, id: Optional[str] = None,
         tags: Iterable[str] = (), summary: str = "",
         refresh_secs: Optional[int] = None,
         data_endpoints: Iterable[str] = (), pinned: bool = False) -> str:
    """Write an artifact + manifest; return the artifact id (slug).

    Set ``pinned=True`` to protect the artifact from the retention cron
    (``artifact_surface.retention``), e.g. a long-lived reference page.
    """
    slug = id or _slugify(title)
    if not _SAFE_ID.match(slug) or slug in {".", ".."}:
        raise ValueError(
            f"unsafe artifact id {slug!r}: must match [A-Za-z0-9._-]+ "
            "(the viewer store will not serve other ids)"
        )
    d = artifacts_dir()
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / f"{slug}.html", html)
    manifest = {
        "id": slug, "title": title, "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tags": list(tags), "summary": summary,
        "refresh_secs": refresh_secs, "data_endpoints": list(data_endpoints),
        "pinned": pinned,
    }
    _atomic_write(d / f"{slug}.json", json.dumps(manifest, indent=2))
    return slug
