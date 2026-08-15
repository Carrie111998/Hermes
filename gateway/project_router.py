"""Minimal Matrix project routing backed by Hermes state_meta."""
from __future__ import annotations

from pathlib import Path

PROJECTS = {
    "newmoon": "/home/rle/projects/NewMoonNailsAndSpa",
    "fivehours": "/home/rle/projects/savefivehours",
}
_META_PREFIX = "matrix_project_router:"


def project_keys() -> tuple[str, ...]:
    return tuple(sorted(PROJECTS))


def project_path(key: str) -> Path | None:
    raw = PROJECTS.get((key or "").strip().lower())
    return Path(raw) if raw else None


def select_project(db, session_key: str, key: str) -> Path:
    normalized_key = (key or "").strip().lower()
    path = project_path(normalized_key)
    if path is None:
        raise ValueError(
            f"unknown project '{normalized_key}'. Valid projects: {', '.join(project_keys())}"
        )
    if not path.is_dir():
        raise ValueError(f"configured project path does not exist: {path}")
    db.set_meta(_META_PREFIX + session_key, normalized_key)
    return path


def clear_project(db, session_key: str) -> None:
    db.delete_meta(_META_PREFIX + session_key)


def active_project(db, session_key: str) -> tuple[str, Path] | None:
    key = (db.get_meta(_META_PREFIX + session_key) or "").strip().lower()
    if not key:
        return None
    path = project_path(key)
    return (key, path) if path and path.is_dir() else None


def active_project_path(db, session_key: str) -> Path | None:
    project = active_project(db, session_key)
    return project[1] if project else None
