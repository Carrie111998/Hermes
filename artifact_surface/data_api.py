"""Read-only JSON data API for live artifacts.

Four readers over Hermes sources. Each accepts its source path(s) as a parameter
(defaulting to the canonical location) for testability, and fails soft: a missing
or unreadable source yields an empty result, never an exception. A FastAPI
APIRouter at the bottom exposes them under /api/*.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from hermes_constants import get_default_hermes_root
from events.paths import events_db_path

ACTIVITY_EVENT_TYPES = (
    "job_discovered", "job_vip_discovered", "job_scored", "job_high_score",
    "tailor_completed", "application_ready", "application_submitted",
    "approval_request", "apply_packet", "stage_transition",
    "critic_proposal", "interview_signal", "offer_signal",
    "agent_error", "cron_failed", "cron_failed_consecutive",
    "gateway_health", "secret_detected",
)

_CRON_FIELDS = (
    "id", "name", "schedule_display", "enabled", "state", "paused_at",
    "next_run_at", "last_run_at", "last_status", "last_error",
    "consecutive_errors", "deliver",
)


def _root() -> Path:
    return get_default_hermes_root()


def read_events(*, db_path: Optional[Path] = None, limit: int = 80,
                event_type: Optional[str] = None) -> list[dict]:
    db = db_path or events_db_path()
    if not Path(db).exists():
        return []
    try:
        conn = sqlite3.connect(str(db))
    except sqlite3.Error:
        return []
    try:
        if event_type:
            where, params = "event_type = ?", [event_type]
        else:
            csv = ",".join(f"'{t}'" for t in ACTIVITY_EVENT_TYPES)
            where, params = f"event_type IN ({csv})", []
        rows = conn.execute(
            f"SELECT event_id, event_type, source, priority, created_at, payload "
            f"FROM events WHERE {where} ORDER BY rowid DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[5]) if r[5] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        out.append({"event_id": r[0], "event_type": r[1], "source": r[2],
                    "priority": r[3], "created_at": r[4], "payload": payload})
    return out


def read_cron(*, jobs_path: Optional[Path] = None) -> list[dict]:
    p = jobs_path or (_root() / "profiles" / "main" / "cron" / "jobs.json")
    if not Path(p).exists():
        return []
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        out.append({k: j.get(k) for k in _CRON_FIELDS})
    return out


def _read_pipeline_summary() -> dict:
    """Pipeline stage/state counts from the jobflow control plane (:4100).

    Uses the API's fast SQL summary instead of parsing the ~38MB canonical
    pipeline.json per poll (live artifacts hit this endpoint every ~15s).
    Must dial 127.0.0.1 — Windows resolves localhost to ::1 first, where
    nothing answers for :4100. Soft-fails to a note so the submissions view
    still renders when the control plane is down.
    """
    import urllib.request

    url = "http://127.0.0.1:4100/api/v1/jobs/summary"
    try:
        with urllib.request.urlopen(url, timeout=2.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any failure means "plane down"
        return {"unavailable": f"{exc.__class__.__name__}: {exc}"}


def read_jobflow(*, submissions_dir: Optional[Path] = None) -> dict:
    d = submissions_dir or (_root() / "profiles" / "applier" / "workspace" / "submissions")
    subs: list[dict] = []
    counts: dict[str, int] = {}
    if Path(d).exists():
        for status_file in Path(d).glob("*/status.json"):
            try:
                s = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            entry = {
                "job_id": s.get("job_id"), "platform": s.get("platform"),
                "status": s.get("status"), "submitted": s.get("submitted"),
                "success": s.get("success"), "requiresHuman": s.get("requiresHuman"),
            }
            subs.append(entry)
            st = str(s.get("status") or "unknown")
            counts[st] = counts.get(st, 0) + 1
    return {
        "pipeline": _read_pipeline_summary(),
        "submissions": subs,
        "counts_by_status": counts,
    }


def read_financier(*, workspace_dir: Optional[Path] = None) -> dict:
    ws = workspace_dir or (_root() / "profiles" / "financier" / "workspace")
    snapshot: dict = {}
    latest = Path(ws) / "snapshots" / "latest.json"
    if latest.exists():
        try:
            snapshot = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            snapshot = {}
    digest = ""
    runs = Path(ws) / "runs"
    if runs.exists():
        txts = sorted(runs.glob("*.txt"), key=lambda p: p.name, reverse=True)
        if txts:
            try:
                digest = txts[0].read_text(encoding="utf-8")
            except OSError:
                digest = ""
    return {"snapshot": snapshot, "latest_digest": digest}


router = APIRouter(prefix="/api")


@router.get("/events")
async def api_events(limit: int = 80, event_type: str = "") -> JSONResponse:
    return JSONResponse(read_events(limit=limit, event_type=event_type or None))


@router.get("/cron")
async def api_cron() -> JSONResponse:
    return JSONResponse(read_cron())


@router.get("/jobflow")
async def api_jobflow() -> JSONResponse:
    return JSONResponse(read_jobflow())


@router.get("/financier")
async def api_financier() -> JSONResponse:
    return JSONResponse(read_financier())
