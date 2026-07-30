"""Skills materialization from Mongo GridFS into a local cache directory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def mongo_skills_cache_dir() -> Path:
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sync_skills_from_mongo() -> Optional[Path]:
    """Materialize all remote skills into the local cache. Returns cache root.

    In Mongo mode failures raise — never silently return the classic skills dir.
    """
    from hermes_storage import is_mongo_mode, require_storage

    if not is_mongo_mode():
        return None
    storage = require_storage()

    dest = mongo_skills_cache_dir()
    try:
        for skill in storage.skills.list_skills():
            name = skill.get("name")
            if not name:
                continue
            try:
                storage.skills.materialize(name, dest)
            except Exception as exc:
                logger.warning("Failed to materialize skill %s: %s", name, exc)
    except Exception as exc:
        from hermes_storage.errors import raise_mongo_unavailable
        raise_mongo_unavailable(f"skills list failed: {exc}", cause=exc)
    return dest


def upload_local_skill_tree(skill_dir: Path, *, name: Optional[str] = None) -> None:
    """Push a local skill directory into Mongo (shared skills DB)."""
    from hermes_storage import require_storage

    storage = require_storage()

    skill_dir = Path(skill_dir)
    skill_name = name or skill_dir.name
    skill_md = skill_dir / "SKILL.md"
    body = skill_md.read_text(encoding="utf-8") if skill_md.is_file() else ""
    files: dict[str, bytes] = {}
    for f in skill_dir.rglob("*"):
        if f.is_file():
            rel = str(f.relative_to(skill_dir)).replace("\\", "/")
            files[rel] = f.read_bytes()
    storage.skills.put_skill(
        {"name": skill_name, "skill_md": body, "path": skill_name},
        files=files,
    )
