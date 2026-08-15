"""Content-addressed artifact declaration and freeze support."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .canonical import canonical_json_bytes, sha256_hex
from .types import ArtifactDeclaration, ContractError, RunFence

_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_SET_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FrozenArtifact:
    artifact_id: str
    relative_path: str
    display_name: str
    media_type: str
    byte_length: int
    sha256: str
    blob_path: str


def _safe_relative(path: str) -> PurePosixPath:
    if "\\" in path:
        path = path.replace("\\", "/")
    rel = PurePosixPath(path)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise ContractError("artifact path must be a normalized relative path")
    return rel


def declare_artifact(conn, fence: RunFence, declaration: ArtifactDeclaration) -> str:
    rel = _safe_relative(declaration.relative_path).as_posix()
    declaration_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO run_artifact_declarations(
            declaration_id, task_id, run_id, claim_generation, relative_path,
            display_name, media_type, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            declaration_id,
            fence.task_id,
            fence.run_id,
            fence.claim_generation,
            rel,
            declaration.display_name,
            declaration.media_type,
            int(time.time()),
        ),
    )
    return declaration_id


def _copy_regular_file(source: Path, destination: Path) -> tuple[int, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(source, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ContractError("artifact must be a regular file")
        if before.st_size > _MAX_ARTIFACT_BYTES:
            raise ContractError("artifact exceeds per-file size limit")
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(os.dup(fd), "rb", closefd=True) as src, destination.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_ARTIFACT_BYTES:
                    raise ContractError("artifact grew beyond size limit while freezing")
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise ContractError("artifact changed while it was being frozen")
        return total, digest.hexdigest()
    finally:
        os.close(fd)


def freeze_artifacts(
    *,
    workspace: str | Path,
    blob_root: str | Path,
    declarations: Iterable[ArtifactDeclaration],
) -> tuple[list[FrozenArtifact], str]:
    root = Path(workspace).resolve()
    blobs = Path(blob_root).resolve()
    blobs.mkdir(parents=True, exist_ok=True)
    frozen: list[FrozenArtifact] = []
    total_set = 0

    with tempfile.TemporaryDirectory(prefix="kanban-artifacts-", dir=str(blobs)) as stage_dir:
        stage = Path(stage_dir)
        for declaration in declarations:
            rel = _safe_relative(declaration.relative_path)
            source = root.joinpath(*rel.parts)
            # Resolve parent only; the leaf is opened with O_NOFOLLOW where
            # supported.  This rejects parent traversal and symlink escapes.
            parent = source.parent.resolve(strict=True)
            if os.path.commonpath((str(root), str(parent))) != str(root):
                raise ContractError("artifact parent escapes the workspace")
            temp_target = stage / f"{len(frozen):04d}.blob"
            size, digest = _copy_regular_file(source, temp_target)
            total_set += size
            if total_set > _MAX_SET_BYTES:
                raise ContractError("artifact set exceeds total size limit")
            final_dir = blobs / digest[:2]
            final_dir.mkdir(parents=True, exist_ok=True)
            final_path = final_dir / digest
            if final_path.exists():
                if final_path.stat().st_size != size:
                    raise ContractError("content-addressed blob collision")
                temp_target.unlink()
            else:
                os.replace(temp_target, final_path)
                try:
                    os.chmod(final_path, 0o400)
                except OSError:
                    pass
            frozen.append(
                FrozenArtifact(
                    artifact_id=str(uuid.uuid4()),
                    relative_path=rel.as_posix(),
                    display_name=declaration.display_name,
                    media_type=declaration.media_type,
                    byte_length=size,
                    sha256=digest,
                    blob_path=str(final_path),
                )
            )

    manifest = [
        {
            "relative_path": item.relative_path,
            "display_name": item.display_name,
            "media_type": item.media_type,
            "byte_length": item.byte_length,
            "sha256": item.sha256,
        }
        for item in sorted(frozen, key=lambda value: value.relative_path)
    ]
    return frozen, sha256_hex(canonical_json_bytes(manifest))


def persist_frozen_artifacts(conn, fence: RunFence, artifacts: Iterable[FrozenArtifact]) -> None:
    now = int(time.time())
    for item in artifacts:
        conn.execute(
            """
            INSERT INTO run_artifacts(
                artifact_id, task_id, run_id, claim_generation, relative_path,
                display_name, media_type, byte_length, sha256, blob_path, frozen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.artifact_id,
                fence.task_id,
                fence.run_id,
                fence.claim_generation,
                item.relative_path,
                item.display_name,
                item.media_type,
                item.byte_length,
                item.sha256,
                item.blob_path,
                now,
            ),
        )
