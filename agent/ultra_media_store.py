"""Local Ultra Studio media job and asset store.

This is the P0 persistence layer for provider-neutral media job records.
It intentionally stays local and small: Hermes can record job state and
output assets today without pretending that the full Ultra Studio Asset
Service or TokenRouter control plane already exists.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hermes_constants import get_hermes_home


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def default_db_path() -> Path:
    return get_hermes_home() / "ultra-studio" / "media_jobs.sqlite"


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media_jobs (
            job_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            session_id TEXT,
            run_id TEXT,
            tool_call_id TEXT,
            media_type TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            provider_task_id TEXT,
            prompt TEXT NOT NULL,
            negative_prompt TEXT,
            input_assets_json TEXT NOT NULL DEFAULT '[]',
            request_json TEXT NOT NULL DEFAULT '{}',
            provider_result_json TEXT NOT NULL DEFAULT '{}',
            output_ref TEXT,
            output_assets_json TEXT NOT NULL DEFAULT '[]',
            error_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            session_id TEXT,
            job_id TEXT,
            media_type TEXT NOT NULL,
            status TEXT NOT NULL,
            uri TEXT NOT NULL,
            url TEXT,
            path TEXT,
            mime_type TEXT,
            name TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            lineage_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(job_id, uri)
        );

        CREATE TABLE IF NOT EXISTS media_events (
            event_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            job_id TEXT,
            asset_id TEXT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_media_jobs_session
            ON media_jobs(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_media_jobs_status
            ON media_jobs(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_assets_session
            ON assets(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_assets_job
            ON assets(job_id);
        CREATE INDEX IF NOT EXISTS idx_media_events_job
            ON media_events(job_id, created_at);
        """
    )
    conn.commit()


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "tool_call_id": row["tool_call_id"],
        "media_type": row["media_type"],
        "mode": row["mode"],
        "status": row["status"],
        "provider": row["provider"],
        "model": row["model"],
        "provider_task_id": row["provider_task_id"],
        "prompt": row["prompt"],
        "negative_prompt": row["negative_prompt"],
        "input_assets": _json_loads(row["input_assets_json"], []),
        "request": _json_loads(row["request_json"], {}),
        "provider_result": _json_loads(row["provider_result_json"], {}),
        "output_ref": row["output_ref"],
        "output_assets": _json_loads(row["output_assets_json"], []),
        "error": _json_loads(row["error_json"], {}),
    }


def _row_to_asset(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "asset_id": row["asset_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "session_id": row["session_id"],
        "job_id": row["job_id"],
        "media_type": row["media_type"],
        "status": row["status"],
        "uri": row["uri"],
        "url": row["url"],
        "path": row["path"],
        "mime_type": row["mime_type"],
        "name": row["name"],
        "metadata": _json_loads(row["metadata_json"], {}),
        "lineage": _json_loads(row["lineage_json"], {}),
    }


def _emit_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    job_id: str | None = None,
    asset_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO media_events (
            event_id, created_at, job_id, asset_id, event_type, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _new_id("evt"),
            _now(),
            job_id,
            asset_id,
            event_type,
            _json_dumps(payload or {}),
        ),
    )


def create_job(
    *,
    media_type: str,
    prompt: str,
    mode: str = "generate",
    session_id: str | None = None,
    run_id: str | None = None,
    tool_call_id: str | None = None,
    negative_prompt: str | None = None,
    input_assets: list[Any] | None = None,
    request: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    media_type = media_type.strip().lower()
    mode = mode.strip().lower() or "generate"
    if media_type not in {"image", "video"}:
        raise ValueError("media_type must be 'image' or 'video'")
    if mode not in {"generate", "edit", "extend"}:
        raise ValueError("mode must be one of: generate, edit, extend")
    if not prompt.strip():
        raise ValueError("prompt is required")

    now = _now()
    job_id = _new_id("job")
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO media_jobs (
                job_id, created_at, updated_at, session_id, run_id, tool_call_id,
                media_type, mode, status, prompt, negative_prompt,
                input_assets_json, request_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                now,
                now,
                session_id,
                run_id,
                tool_call_id,
                media_type,
                mode,
                "created",
                prompt,
                negative_prompt,
                _json_dumps(input_assets or []),
                _json_dumps(request or {}),
            ),
        )
        _emit_event(conn, "media_job.created", job_id=job_id, payload={"status": "created"})
        conn.commit()
    job = get_job(job_id, db_path=db_path)
    if job is None:
        raise RuntimeError(f"created job {job_id} could not be loaded")
    return job


def update_job_running(job_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
    with _connect(db_path) as conn:
        now = _now()
        updated = conn.execute(
            "UPDATE media_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
            ("running", now, job_id),
        ).rowcount
        if not updated:
            raise KeyError(job_id)
        _emit_event(conn, "media_job.updated", job_id=job_id, payload={"status": "running"})
        conn.commit()
    job = get_job(job_id, db_path=db_path)
    if job is None:
        raise KeyError(job_id)
    return job


def complete_job(
    job_id: str,
    *,
    provider_result: dict[str, Any],
    output_ref: str,
    provider: str | None = None,
    model: str | None = None,
    provider_task_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if not output_ref:
        raise ValueError("output_ref is required")
    with _connect(db_path) as conn:
        now = _now()
        updated = conn.execute(
            """
            UPDATE media_jobs
            SET status = ?, updated_at = ?, provider = ?, model = ?,
                provider_task_id = ?, provider_result_json = ?, output_ref = ?,
                error_json = '{}'
            WHERE job_id = ?
            """,
            (
                "succeeded",
                now,
                provider,
                model,
                provider_task_id,
                _json_dumps(provider_result),
                output_ref,
                job_id,
            ),
        ).rowcount
        if not updated:
            raise KeyError(job_id)
        _emit_event(conn, "media_job.updated", job_id=job_id, payload={"status": "succeeded"})
        conn.commit()
    job = get_job(job_id, db_path=db_path)
    if job is None:
        raise KeyError(job_id)
    return job


def fail_job(
    job_id: str,
    *,
    error: dict[str, Any],
    provider_result: dict[str, Any] | None = None,
    provider: str | None = None,
    model: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    with _connect(db_path) as conn:
        now = _now()
        updated = conn.execute(
            """
            UPDATE media_jobs
            SET status = ?, updated_at = ?, provider = ?, model = ?,
                provider_result_json = ?, error_json = ?
            WHERE job_id = ?
            """,
            (
                "failed",
                now,
                provider,
                model,
                _json_dumps(provider_result or {}),
                _json_dumps(error),
                job_id,
            ),
        ).rowcount
        if not updated:
            raise KeyError(job_id)
        _emit_event(
            conn,
            "media_job.failed",
            job_id=job_id,
            payload={"status": "failed", "error": error},
        )
        conn.commit()
    job = get_job(job_id, db_path=db_path)
    if job is None:
        raise KeyError(job_id)
    return job


def get_job(job_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM media_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def _split_uri(uri: str) -> tuple[str | None, str | None]:
    parsed = urlparse(uri)
    if parsed.scheme in {"http", "https", "data"}:
        return uri, None
    return None, uri


def _asset_name(uri: str, media_type: str) -> str:
    parsed = urlparse(uri)
    candidate = Path(parsed.path or uri).name
    if candidate:
        return candidate[:180]
    return f"{media_type}-asset"


def finalize_job(job_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM media_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        job = _row_to_job(row)
        if job["status"] != "succeeded":
            raise ValueError(f"job {job_id} is not succeeded; current status is {job['status']}")
        output_ref = job.get("output_ref")
        if not isinstance(output_ref, str) or not output_ref:
            raise ValueError(f"job {job_id} has no output_ref")

        existing = conn.execute(
            "SELECT * FROM assets WHERE job_id = ? AND uri = ?",
            (job_id, output_ref),
        ).fetchone()
        if existing is not None:
            return _row_to_asset(existing)

        now = _now()
        asset_id = _new_id("asset")
        url, path = _split_uri(output_ref)
        provider_result = job.get("provider_result") or {}
        metadata = {
            "provider": job.get("provider"),
            "model": job.get("model"),
            "provider_task_id": job.get("provider_task_id"),
            "modality": provider_result.get("modality"),
            "aspect_ratio": provider_result.get("aspect_ratio"),
            "duration": provider_result.get("duration"),
        }
        lineage = {
            "source_job_id": job_id,
            "session_id": job.get("session_id"),
            "run_id": job.get("run_id"),
            "tool_call_id": job.get("tool_call_id"),
            "input_assets": job.get("input_assets") or [],
            "prompt": job.get("prompt"),
            "negative_prompt": job.get("negative_prompt"),
        }
        conn.execute(
            """
            INSERT INTO assets (
                asset_id, created_at, updated_at, session_id, job_id, media_type,
                status, uri, url, path, mime_type, name, metadata_json, lineage_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                now,
                now,
                job.get("session_id"),
                job_id,
                job["media_type"],
                "ready",
                output_ref,
                url,
                path,
                None,
                _asset_name(output_ref, job["media_type"]),
                _json_dumps(metadata),
                _json_dumps(lineage),
            ),
        )
        asset_row = conn.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        asset = _row_to_asset(asset_row)
        output_assets = list(job.get("output_assets") or [])
        output_assets.append(asset)
        conn.execute(
            "UPDATE media_jobs SET updated_at = ?, output_assets_json = ? WHERE job_id = ?",
            (now, _json_dumps(output_assets), job_id),
        )
        _emit_event(conn, "asset.ready", job_id=job_id, asset_id=asset_id, payload=asset)
        conn.commit()
    return asset


def job_events(job_id: str, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM media_events WHERE job_id = ? ORDER BY created_at, event_id",
            (job_id,),
        ).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "created_at": row["created_at"],
            "job_id": row["job_id"],
            "asset_id": row["asset_id"],
            "event_type": row["event_type"],
            "payload": _json_loads(row["payload_json"], {}),
        }
        for row in rows
    ]
