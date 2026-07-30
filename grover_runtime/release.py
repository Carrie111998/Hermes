"""Deterministic, credential-free Grover release artifact construction."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_RELEASE_FILE_BYTES = 64 * 1024 * 1024
_MAX_RELEASE_BYTES = 256 * 1024 * 1024

_MUTABLE_DIRECTORY_NAMES = frozenset({
    ".git",
    ".pytest_cache",
    "__pycache__",
    "browser_cache",
    "cache",
    "logs",
    "sessions",
})
_MUTABLE_FILE_NAMES = frozenset({
    ".env",
    "auth.json",
    "credentials.json",
    "state.db",
    "state.db-shm",
    "state.db-wal",
})


@dataclass(frozen=True)
class ReleaseSpec:
    release_id: str
    source_tree: Path
    patch_file: Path
    output_dir: Path
    upstream_commit: str
    rollback_target: str
    required_paths: tuple[str, ...]
    dependency_lock: str = "uv.lock"


@dataclass(frozen=True)
class ReleaseResult:
    artifact: Path
    manifest: Path
    evidence: Path
    patch: Path


def create_release(spec: ReleaseSpec) -> ReleaseResult:
    """Create a deterministic allowlisted tar plus manifest and evidence.

    Inputs are fully validated and snapshotted before the exclusive output
    directory is created.  No source path outside ``required_paths`` and the
    dependency lock can enter the tar.
    """

    if not isinstance(spec, ReleaseSpec):
        raise TypeError("release spec has the wrong type")
    if not isinstance(spec.release_id, str) or not _RELEASE_ID_RE.fullmatch(
        spec.release_id
    ):
        raise ValueError("release id is not a safe artifact name")
    _validate_commit(spec.upstream_commit, "upstream commit")
    _validate_commit(spec.rollback_target, "rollback target")

    source_tree = Path(spec.source_tree)
    patch_file = Path(spec.patch_file)
    output_dir = Path(spec.output_dir)
    if not source_tree.exists() or not source_tree.is_dir():
        raise ValueError("release source tree is missing")
    if patch_file.is_symlink() or not patch_file.is_file():
        raise ValueError("release patch file is missing or not a regular file")
    if output_dir.exists():
        raise FileExistsError(str(output_dir))
    patch_name = _normalize_relative_path(patch_file.name, "release patch name")
    if patch_name in {"artifact.tar", "manifest.json", "evidence.json"}:
        raise ValueError("release patch name collides with a reserved artifact")

    required_paths = _normalize_required_paths(spec.required_paths)
    lock_path = _normalize_relative_path(spec.dependency_lock, "dependency lock")
    if _is_mutable_path(lock_path):
        raise ValueError("dependency lock path is not release-safe")

    source_root = source_tree.resolve(strict=True)
    included_names = tuple(sorted(set(required_paths) | {lock_path}))
    included_bytes: dict[str, bytes] = {}
    total_bytes = 0
    for relative_name in included_names:
        purpose = (
            "dependency lock" if relative_name == lock_path else "required release path"
        )
        source_file = _resolve_regular_source_file(
            source_root,
            relative_name,
            purpose=purpose,
        )
        data = _bounded_read(source_file, purpose)
        total_bytes += len(data)
        if total_bytes > _MAX_RELEASE_BYTES:
            raise ValueError("release allowlist exceeds the total size limit")
        included_bytes[relative_name] = data

    patch_bytes = _bounded_read(patch_file, "release patch")
    patch_sha256 = _sha256_bytes(patch_bytes)
    artifact_bytes = _build_deterministic_tar(included_bytes)
    artifact_sha256 = _sha256_bytes(artifact_bytes)

    files_manifest = {
        name: {
            "sha256": _sha256_bytes(data),
            "size_bytes": len(data),
        }
        for name, data in sorted(included_bytes.items())
    }
    manifest_data: dict[str, Any] = {
        "artifact": {
            "format": "tar",
            "path": "artifact.tar",
            "sha256": artifact_sha256,
        },
        "artifact_sha256": artifact_sha256,
        "dependency_lock": {
            "path": lock_path,
            "sha256": _sha256_bytes(included_bytes[lock_path]),
            "size_bytes": len(included_bytes[lock_path]),
        },
        "files": files_manifest,
        "patch": {
            "path": patch_name,
            "sha256": patch_sha256,
            "size_bytes": len(patch_bytes),
        },
        "patch_sha256": patch_sha256,
        "release_id": spec.release_id,
        "required_paths": list(required_paths),
        "rollback_target": spec.rollback_target.lower(),
        "schema_version": "grover.release.v1",
        "upstream_commit": spec.upstream_commit.lower(),
    }
    manifest_bytes = _canonical_json_bytes(manifest_data)
    manifest_sha256 = _sha256_bytes(manifest_bytes)

    all_source_files = _inventory_source_paths(source_root)
    included_set = set(included_names)
    evidence_data: dict[str, Any] = {
        "artifact_sha256": artifact_sha256,
        "excluded_paths": sorted(all_source_files - included_set),
        "included_paths": list(included_names),
        "manifest_sha256": manifest_sha256,
        "patch_sha256": patch_sha256,
        "release_id": spec.release_id,
        "schema_version": "grover.release-evidence.v1",
    }
    evidence_bytes = _canonical_json_bytes(evidence_data)

    artifact_path = output_dir / "artifact.tar"
    manifest_path = output_dir / "manifest.json"
    evidence_path = output_dir / "evidence.json"
    patch_path = output_dir / patch_name
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        _exclusive_write(artifact_path, artifact_bytes)
        _exclusive_write(patch_path, patch_bytes)
        _exclusive_write(manifest_path, manifest_bytes)
        _exclusive_write(evidence_path, evidence_bytes)
        for path in (artifact_path, manifest_path, evidence_path, patch_path):
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except BaseException:
        if output_dir.exists():
            for path in (artifact_path, manifest_path, evidence_path, patch_path):
                try:
                    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
            shutil.rmtree(output_dir, ignore_errors=True)
        raise

    return ReleaseResult(
        artifact=artifact_path,
        manifest=manifest_path,
        evidence=evidence_path,
        patch=patch_path,
    )


def verify_wheel_modules(
    wheel: Path,
    *,
    required_modules: tuple[str, ...],
) -> None:
    """Verify exact, safe module members in a wheel without extracting it."""

    wheel_path = Path(wheel)
    if wheel_path.is_symlink() or not wheel_path.is_file():
        raise ValueError("wheel is missing or not a regular file")
    required = _normalize_required_paths(required_modules, reject_mutable=False)

    try:
        with zipfile.ZipFile(wheel_path, "r") as archive:
            infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("wheel is not a valid zip archive") from exc

    names: set[str] = set()
    for info in infos:
        raw_name = info.filename
        candidate = (
            raw_name[:-1] if info.is_dir() and raw_name.endswith("/") else raw_name
        )
        normalized = _normalize_relative_path(candidate, "wheel member")
        if normalized in names:
            raise ValueError("wheel contains duplicate members")
        names.add(normalized)

    missing = sorted(module for module in required if module not in names)
    if missing:
        raise ValueError("wheel is missing required modules: " + ", ".join(missing))


def _validate_commit(value: str, label: str) -> None:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise ValueError(f"{label} must be a 40-character commit hash")


def _normalize_required_paths(
    paths: tuple[str, ...],
    *,
    reject_mutable: bool = True,
) -> tuple[str, ...]:
    if not isinstance(paths, tuple) or not paths:
        raise ValueError("required release paths must be a non-empty tuple")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        relative_name = _normalize_relative_path(raw_path, "required release path")
        if relative_name in seen:
            raise ValueError(f"duplicate required release path: {relative_name}")
        if reject_mutable and _is_mutable_path(relative_name):
            raise ValueError(
                f"required release path is mutable or credential-bearing: {relative_name}"
            )
        seen.add(relative_name)
        normalized.append(relative_name)
    return tuple(sorted(normalized))


def _normalize_relative_path(raw_path: str, label: str) -> str:
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or raw_path != raw_path.strip()
        or "\x00" in raw_path
        or "\\" in raw_path
        or len(raw_path.encode("utf-8")) > 4096
    ):
        raise ValueError(f"{label} is not a safe relative path")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", raw_path):
        raise ValueError(f"{label} is not a safe relative path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} is not a safe relative path")
    normalized = path.as_posix()
    if normalized != raw_path:
        raise ValueError(f"{label} is not a canonical relative path")
    return normalized


def _is_mutable_path(relative_name: str) -> bool:
    parts = PurePosixPath(relative_name).parts
    lowered_parts = tuple(part.lower() for part in parts)
    name = lowered_parts[-1]
    if any(part in _MUTABLE_DIRECTORY_NAMES for part in lowered_parts[:-1]):
        return True
    if name in _MUTABLE_FILE_NAMES or name.startswith(".env."):
        return True
    return (
        name.endswith(".db")
        or name.endswith(".db-shm")
        or name.endswith(".db-wal")
        or name.endswith(".log")
        or name.endswith(".pyc")
        or name.endswith(".sqlite")
        or name.endswith(".sqlite3")
    )


def _resolve_regular_source_file(
    source_root: Path,
    relative_name: str,
    *,
    purpose: str,
) -> Path:
    candidate = source_root.joinpath(*PurePosixPath(relative_name).parts)
    cursor = source_root
    for part in PurePosixPath(relative_name).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            if purpose == "required release path":
                raise ValueError(
                    f"required release path is not a regular file: {relative_name}"
                )
            raise ValueError(f"{purpose} is not a regular file: {relative_name}")
    if not candidate.exists():
        if purpose == "required release path":
            raise ValueError(f"required release path is missing: {relative_name}")
        raise ValueError(f"{purpose} is missing: {relative_name}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(source_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{purpose} escapes the source tree: {relative_name}") from exc
    if not resolved.is_file():
        raise ValueError(f"{purpose} is not a regular file: {relative_name}")
    return resolved


def _bounded_read(path: Path, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}") from exc
    if size > _MAX_RELEASE_FILE_BYTES:
        raise ValueError(f"{label} exceeds the file size limit")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {label}") from exc
    if len(data) != size or len(data) > _MAX_RELEASE_FILE_BYTES:
        raise ValueError(f"{label} changed while being read")
    return data


def _build_deterministic_tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _inventory_source_paths(source_root: Path) -> set[str]:
    paths: set[str] = set()
    for path in source_root.rglob("*"):
        if path.is_file() or path.is_symlink():
            paths.add(path.relative_to(source_root).as_posix())
    return paths


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exclusive_write(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
