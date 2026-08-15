"""Baseline-relative filesystem, Git, and attachment evidence."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from .canonical import canonical_json_bytes, sha256_hex
from .types import ContractError, RunFence


@dataclass(frozen=True, slots=True)
class Baseline:
    baseline_id: str
    filesystem_sha256: str
    vcs_sha256: str
    attachment_sha256: str
    exclusions_sha256: str
    detail: Mapping[str, object]


def _normalize_exclusions(root: Path, exclusions: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in exclusions:
        rel = PurePosixPath(str(raw).replace("\\", "/"))
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise ContractError("baseline exclusion must be a normalized relative path")
        normalized.append(rel.as_posix().rstrip("/"))
    return tuple(sorted(set(normalized)))


def _excluded(rel: str, exclusions: tuple[str, ...]) -> bool:
    return any(rel == prefix or rel.startswith(prefix + "/") for prefix in exclusions)


def filesystem_manifest(root: str | Path, exclusions: Iterable[str]) -> tuple[str, dict[str, object]]:
    workspace = Path(root).resolve(strict=True)
    frozen_exclusions = _normalize_exclusions(workspace, exclusions)
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        base = Path(dirpath)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            rel = (base / dirname).relative_to(workspace).as_posix()
            if not _excluded(rel, frozen_exclusions):
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            path = base / filename
            rel = path.relative_to(workspace).as_posix()
            if _excluded(rel, frozen_exclusions):
                continue
            try:
                st = path.lstat()
                if path.is_symlink():
                    rows.append({"path": rel, "kind": "symlink", "target": os.readlink(path)})
                elif path.is_file():
                    rows.append(
                        {
                            "path": rel,
                            "kind": "file",
                            "size": int(st.st_size),
                            "mtime_ns": int(st.st_mtime_ns),
                            "mode": int(st.st_mode & 0o7777),
                        }
                    )
                else:
                    rows.append({"path": rel, "kind": "other", "mode": int(st.st_mode)})
            except OSError as exc:
                errors.append(f"{rel}:{type(exc).__name__}")
    detail = {"entries": rows, "errors": errors, "exclusions": frozen_exclusions}
    return sha256_hex(canonical_json_bytes(detail)), detail


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if completed.returncode:
        raise ContractError(f"git probe failed: {' '.join(args)}")
    return completed.stdout


def vcs_manifest(root: str | Path) -> tuple[str, dict[str, object]]:
    repo = Path(root).resolve(strict=True)
    try:
        head = _git(repo, "rev-parse", "HEAD").strip()
        refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
        status = _git(repo, "status", "--porcelain=v2", "--untracked-files=all").splitlines()
        index_tree = _git(repo, "write-tree").strip()
        local = _git(repo, "log", "--format=%H", "--branches", "--not", "--remotes").splitlines()
        detail: dict[str, object] = {
            "available": True,
            "head": head,
            "refs": sorted(refs),
            "status": status,
            "index_tree": index_tree,
            "local_commits": sorted(local),
        }
    except (ContractError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        detail = {"available": False, "error": type(exc).__name__}
    return sha256_hex(canonical_json_bytes(detail)), detail


def attachment_manifest(conn, task_id: str) -> tuple[str, list[dict[str, object]]]:
    rows = conn.execute(
        "SELECT id, filename, stored_path, content_type, size, created_at "
        "FROM task_attachments WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall() if _has_table(conn, "task_attachments") else []
    values = [
        {
            "id": int(row[0]),
            "filename": str(row[1]),
            "stored_path_digest": hashlib.sha256(str(row[2]).encode()).hexdigest(),
            "content_type": row[3],
            "size": int(row[4]),
            "created_at": int(row[5]),
        }
        for row in rows
    ]
    return sha256_hex(canonical_json_bytes(values)), values


def _has_table(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def create_baseline(conn, *, fence: RunFence, workspace: str | Path, exclusions: Iterable[str]) -> Baseline:
    fs_sha, fs_detail = filesystem_manifest(workspace, exclusions)
    vcs_sha, vcs_detail = vcs_manifest(workspace)
    attachment_sha, attachment_detail = attachment_manifest(conn, fence.task_id)
    normalized = _normalize_exclusions(Path(workspace).resolve(), exclusions)
    exclusions_sha = sha256_hex(canonical_json_bytes(normalized))
    detail = {
        "filesystem": fs_detail,
        "vcs": vcs_detail,
        "attachments": attachment_detail,
        "exclusions": normalized,
    }
    baseline = Baseline(
        baseline_id=str(uuid.uuid4()),
        filesystem_sha256=fs_sha,
        vcs_sha256=vcs_sha,
        attachment_sha256=attachment_sha,
        exclusions_sha256=exclusions_sha,
        detail=detail,
    )
    conn.execute(
        """
        INSERT INTO run_baselines(
            baseline_id, task_id, run_id, claim_generation, filesystem_sha256,
            vcs_sha256, attachment_sha256, exclusions_sha256, detail_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            baseline.baseline_id,
            fence.task_id,
            fence.run_id,
            fence.claim_generation,
            fs_sha,
            vcs_sha,
            attachment_sha,
            exclusions_sha,
            canonical_json_bytes(detail).decode("utf-8"),
            int(time.time()),
        ),
    )
    return baseline
