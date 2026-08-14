"""Minimal Matrix project routing backed by Hermes state_meta."""
from __future__ import annotations

from pathlib import Path

PROJECTS = {
    "newmoon": "/home/rle/projects/NewMoonNailsAndSpa",
}
_META_PREFIX = "matrix_project_router:"


def project_path(key: str) -> Path | None:
    raw = PROJECTS.get((key or "").strip().lower())
    return Path(raw) if raw else None


def select_project(db, session_key: str, key: str) -> Path:
    path = project_path(key)
    if path is None:
        raise ValueError("unknown project")
    if not path.is_dir():
        raise ValueError(f"configured project path does not exist: {path}")
    db.set_meta(_META_PREFIX + session_key, key)
    return path


def active_project_path(db, session_key: str) -> Path | None:
    key = db.get_meta(_META_PREFIX + session_key)
    if not key:
        return None
    path = project_path(key)
    return path if path and path.is_dir() else None
