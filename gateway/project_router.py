"""Matrix project routing backed by Hermes state_meta."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# One-time legacy seed. Runtime lookups use the persisted registry exclusively.
_BOOTSTRAP_PROJECTS = {
    "newmoon": "/home/rle/projects/NewMoonNailsAndSpa",
    "fivehours": "/home/rle/projects/savefivehours",
}
_META_PREFIX = "matrix_project_router:"
_REGISTRY_META_KEY = "matrix_project_router:registry"
_REGISTRY_VERSION = 1
_PROJECT_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "tsconfig.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "mix.exs",
    "pubspec.yaml",
    "CMakeLists.txt",
    "Makefile",
    "Dockerfile",
)
_CONTEXT_PATHS = (
    ("AGENTS.md", lambda root: (root / "AGENTS.md").is_file()),
    ("README*", lambda root: any(path.is_file() for path in root.glob("README*"))),
    ("CONTRIBUTING.md", lambda root: (root / "CONTRIBUTING.md").is_file()),
    ("package.json", lambda root: (root / "package.json").is_file()),
    ("pyproject.toml", lambda root: (root / "pyproject.toml").is_file()),
    ("Cargo.toml", lambda root: (root / "Cargo.toml").is_file()),
    ("go.mod", lambda root: (root / "go.mod").is_file()),
    ("docs/", lambda root: (root / "docs").is_dir()),
    ("docs/STATUS.md", lambda root: (root / "docs" / "STATUS.md").is_file()),
    ("docs/decisions/", lambda root: (root / "docs" / "decisions").is_dir()),
)


@dataclass(frozen=True)
class RegisteredProject:
    key: str
    path: Path
    context: tuple[tuple[str, bool], ...]


def normalize_project_key(value: str) -> str:
    """Return a Matrix-friendly key derived from a directory or project name."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _bootstrap_registry_value() -> dict:
    return {
        "version": _REGISTRY_VERSION,
        "projects": {
            key: {"path": path, "metadata": {}}
            for key, path in sorted(_BOOTSTRAP_PROJECTS.items())
        },
    }


def _load_registry(db) -> dict:
    raw = db.get_meta(_REGISTRY_META_KEY)
    if raw is None:
        registry = _bootstrap_registry_value()
        db.set_meta(_REGISTRY_META_KEY, json.dumps(registry, sort_keys=True))
        return registry
    try:
        registry = json.loads(raw)
        projects = registry["projects"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("project registry state is invalid") from exc
    if registry.get("version") != _REGISTRY_VERSION or not isinstance(projects, dict):
        raise ValueError("project registry state is invalid")
    for key, entry in projects.items():
        if not isinstance(key, str) or key != normalize_project_key(key):
            raise ValueError("project registry state is invalid")
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("project registry state is invalid")
    return registry


def _save_registry(db, registry: dict) -> None:
    db.set_meta(_REGISTRY_META_KEY, json.dumps(registry, sort_keys=True))


def bootstrap_registry(db) -> None:
    """Create the legacy registry only when no persisted registry exists."""
    _load_registry(db)


def _registry_projects(db) -> dict:
    return _load_registry(db)["projects"]


def project_keys(db) -> tuple[str, ...]:
    return tuple(sorted(_registry_projects(db)))


def project_path(db, key: str) -> Path | None:
    entry = _registry_projects(db).get(normalize_project_key(key))
    return Path(entry["path"]) if entry else None


def inspect_project_context(path: Path) -> tuple[tuple[str, bool], ...]:
    """Inspect bounded, static repository context without executing project code."""
    return tuple((label, present(path)) for label, present in _CONTEXT_PATHS)


def _appears_to_be_project(path: Path) -> bool:
    return (path / ".git").exists() or any((path / marker).is_file() for marker in _PROJECT_MARKERS)


def register_project(db, raw_path: str, *, key: str | None = None) -> RegisteredProject:
    """Validate and persist a project without modifying its repository files."""
    candidate = Path((raw_path or "").strip())
    if not candidate.is_absolute():
        raise ValueError("project path must be absolute")
    if not candidate.exists():
        raise ValueError(f"project path does not exist: {candidate}")
    if not candidate.is_dir():
        raise ValueError(f"project path is not a directory: {candidate}")
    path = candidate.resolve()
    if not _appears_to_be_project(path):
        raise ValueError(f"project path does not appear to be a project or repository: {path}")

    normalized_key = normalize_project_key(key if key is not None else path.name)
    if not normalized_key:
        raise ValueError("project key must contain at least one ASCII letter or number")

    registry = _load_registry(db)
    projects = registry["projects"]
    existing = projects.get(normalized_key)
    if existing:
        existing_path = Path(existing["path"])
        if existing_path == path:
            raise ValueError(f"project key '{normalized_key}' is already registered for this path")
        raise ValueError(
            f"project key '{normalized_key}' is already registered for: {existing_path}"
        )
    for existing_key, entry in projects.items():
        if Path(entry["path"]) == path:
            raise ValueError(f"project path is already registered as '{existing_key}'")

    context = inspect_project_context(path)
    projects[normalized_key] = {"path": str(path), "metadata": {}}
    _save_registry(db, registry)
    return RegisteredProject(normalized_key, path, context)


def select_project(db, session_key: str, key: str) -> Path:
    normalized_key = normalize_project_key(key)
    path = project_path(db, normalized_key)
    if path is None:
        raise ValueError(
            f"unknown project '{normalized_key}'. Valid projects: {', '.join(project_keys(db))}"
        )
    if not path.is_dir():
        raise ValueError(f"configured project path does not exist: {path}")
    db.set_meta(_META_PREFIX + session_key, normalized_key)
    return path


def clear_project(db, session_key: str) -> None:
    db.delete_meta(_META_PREFIX + session_key)


def active_project(db, session_key: str) -> tuple[str, Path] | None:
    key = normalize_project_key(db.get_meta(_META_PREFIX + session_key) or "")
    if not key:
        return None
    path = project_path(db, key)
    return (key, path) if path and path.is_dir() else None


def active_project_path(db, session_key: str) -> Path | None:
    project = active_project(db, session_key)
    return project[1] if project else None
