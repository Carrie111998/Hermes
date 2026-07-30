"""Read-only inventory of a dirty checkout and exported migration patches."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_PATCH_BYTES = 64 * 1024 * 1024
_MAX_INVENTORY_PATHS = 100_000
_GIT_STATUS_ARGV = (
    "git",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "status",
    "--porcelain=v1",
    "-z",
    "--untracked-files=all",
)


def build_migration_inventory(
    repository: Path,
    *,
    patch_files: Sequence[Path] = (),
) -> dict[str, Any]:
    """Return path/hash metadata without modifying or exposing file content."""

    repo = Path(repository)
    if not repo.exists() or not repo.is_dir():
        raise ValueError("migration repository must be an existing directory")

    status_rows = _read_git_status(repo)
    checkout_paths = sorted({row["path"] for row in status_rows})
    checkout_categories = _group_categories(checkout_paths)

    patch_rows = [_inventory_patch(Path(path)) for path in patch_files]
    return {
        "checkout": {
            "categories": checkout_categories,
            "dirty": bool(status_rows),
            "paths": status_rows,
        },
        "exported_patches": patch_rows,
        "schema_version": "grover.migration-inventory.v1",
    }


def _read_git_status(repository: Path) -> list[dict[str, str]]:
    environment = _git_environment()
    try:
        completed = subprocess.run(
            _GIT_STATUS_ARGV,
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("read-only git status command failed") from exc

    if (
        len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES
        or len(completed.stderr) > _MAX_GIT_OUTPUT_BYTES
    ):
        raise RuntimeError("git status output exceeded the inventory limit")
    if completed.returncode != 0:
        raise ValueError("migration repository is not a readable Git checkout")
    return _parse_porcelain_status(completed.stdout)


def _git_environment() -> dict[str, str]:
    environment: dict[str, str] = {
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for name in ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _parse_porcelain_status(payload: bytes) -> list[dict[str, str]]:
    tokens = payload.split(b"\x00")
    rows: list[dict[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2:3] != b" ":
            raise RuntimeError("git status returned malformed porcelain output")
        try:
            status = token[:2].decode("ascii")
            path = token[3:].decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise RuntimeError("git status returned a non-UTF-8 path") from exc
        path = _normalize_inventory_path(path)
        row = {
            "category": _category_for_path(path),
            "path": path,
            "status": status,
        }

        if "R" in status or "C" in status:
            if index >= len(tokens) or not tokens[index]:
                raise RuntimeError("git status returned an incomplete rename record")
            try:
                previous = tokens[index].decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise RuntimeError("git status returned a non-UTF-8 path") from exc
            index += 1
            row["previous_path"] = _normalize_inventory_path(previous)

        rows.append(row)
        if len(rows) > _MAX_INVENTORY_PATHS:
            raise RuntimeError("checkout inventory contains too many paths")

    rows.sort(key=lambda row: (row["path"], row["status"]))
    return rows


def _inventory_patch(patch_file: Path) -> dict[str, Any]:
    if patch_file.is_symlink() or not patch_file.is_file():
        raise ValueError("exported patch must be a regular file")
    try:
        size = patch_file.stat().st_size
    except OSError as exc:
        raise ValueError("cannot inspect exported patch") from exc
    if size > _MAX_PATCH_BYTES:
        raise ValueError("exported patch exceeds the inventory size limit")
    try:
        payload = patch_file.read_bytes()
    except OSError as exc:
        raise ValueError("cannot read exported patch") from exc
    if len(payload) != size or len(payload) > _MAX_PATCH_BYTES:
        raise ValueError("exported patch changed while being read")

    paths = _patch_paths(payload)
    return {
        "categories": _group_categories(paths),
        "path": patch_file.name,
        "paths": paths,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _patch_paths(payload: bytes) -> list[str]:
    paths: set[str] = set()
    for raw_line in payload.splitlines():
        if not raw_line.startswith(b"diff --git "):
            continue
        try:
            line = raw_line.decode("utf-8", errors="strict")
            fields = shlex.split(line, posix=True)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("exported patch has an invalid diff header") from exc
        if len(fields) != 4 or fields[:2] != ["diff", "--git"]:
            raise ValueError("exported patch has an invalid diff header")
        target = fields[3]
        if target.startswith("b/"):
            target = target[2:]
        paths.add(_normalize_inventory_path(target))
        if len(paths) > _MAX_INVENTORY_PATHS:
            raise ValueError("exported patch contains too many paths")
    return sorted(paths)


def _normalize_inventory_path(raw_path: str) -> str:
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or "\x00" in raw_path
        or "\\" in raw_path
    ):
        raise ValueError("inventory path is invalid")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("inventory path is not repository-relative")
    normalized = path.as_posix()
    if normalized != raw_path:
        raise ValueError("inventory path is not canonical")
    return normalized


def _group_categories(paths: Sequence[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for path in sorted(set(paths)):
        grouped.setdefault(_category_for_path(path), []).append(path)
    return {name: grouped[name] for name in sorted(grouped)}


def _category_for_path(path: str) -> str:
    if path == "plugins/platforms/telegram/adapter.py":
        return "upstreamable_adapter"
    if path.startswith("grover_runtime/") or path.startswith(
        "plugins/grover_shadow_guard/"
    ):
        return "grizzly_specific"
    return "unclassified"
