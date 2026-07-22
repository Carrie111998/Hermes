"""Read-only JSON data API for live artifacts.

Five readers over Hermes sources. Each accepts its source path(s) as a parameter
(defaulting to the canonical location) for testability, and fails soft: a missing
or unreadable source yields an empty result, never an exception. A FastAPI
APIRouter at the bottom exposes them under /api/*.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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


_BOOT_DONE_STATES = ("started", "already-up")
_BOOT_FAIL_STATES = ("start-error", "timeout")


def _parse_boot_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _summarize_boot_jsonl(path: Path) -> Optional[dict]:
    """Fold one boot-<id>.jsonl (schema v1 events: boot-start/phase/step/boot-end)
    into a summary dict. Tolerant of BOM, blank and malformed lines."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    boot_id = path.stem[len("boot-"):] if path.stem.startswith("boot-") else path.stem
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    state = "incomplete"
    phase = ""
    phases: list[dict] = []
    steps: dict[str, dict] = {}  # insertion-ordered; one entry per step name
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("ev")
        at = ev.get("at")
        if kind == "boot-start":
            started_at = at
            boot_id = ev.get("bootId") or boot_id
        elif kind == "phase":
            phase = ev.get("phase") or ""
            phases.append({"phase": phase, "at": at})
        elif kind == "boot-end":
            finished_at = at
            state = ev.get("state") or state
        elif kind == "step":
            name = ev.get("name")
            if not isinstance(name, str) or not name:
                continue
            s = steps.setdefault(name, {
                "name": name, "tier": ev.get("tier") or "",
                "category": ev.get("category") or "",
                "state": "running", "startedAt": at, "phase": phase,
                "durationMs": 0, "detail": "", "offsetMs": None,
            })
            if ev.get("state") == "running":
                s["startedAt"] = at
                s["phase"] = phase
            else:
                s["state"] = ev.get("state") or s["state"]
                s["durationMs"] = ev.get("durationMs") or 0
                s["detail"] = ev.get("detail") or ""
    if not steps and started_at is None:
        return None
    t0 = _parse_boot_ts(started_at)
    for s in steps.values():
        t = _parse_boot_ts(s["startedAt"])
        if t0 is not None and t is not None:
            s["offsetMs"] = int((t - t0).total_seconds() * 1000)
    step_list = list(steps.values())
    duration_secs = None
    t1 = _parse_boot_ts(finished_at)
    if t0 is not None and t1 is not None:
        duration_secs = int((t1 - t0).total_seconds())
    counts = {
        "total": len(step_list),
        "done": sum(1 for s in step_list if s["state"] in _BOOT_DONE_STATES),
        "failed": sum(1 for s in step_list if s["state"] in _BOOT_FAIL_STATES),
        "skipped": sum(1 for s in step_list if str(s["state"]).startswith("skipped")),
    }
    return {
        "bootId": boot_id, "state": state,
        "startedAt": started_at, "finishedAt": finished_at,
        "durationSecs": duration_secs, "counts": counts,
        "phases": phases, "steps": step_list,
        "anomalies": [], "sweep": {}, "anomalyCount": 0,
    }


def read_boot(*, boot_dir: Optional[Path] = None,
              progress_path: Optional[Path] = None, limit: int = 20) -> dict:
    """Current boot-progress.json + per-boot history from the JSONL dir.

    Anomalies never reach the JSONL (the sweep merges them into
    boot-progress.json only), so each boot's anomalies come from its
    boot-<id>.final.json snapshot (written by emit-boot-history-artifact.py)
    or, for the boot boot-progress.json still describes, from that live file.
    """
    home = Path.home()
    d = Path(boot_dir) if boot_dir else home / "architecture-map" / "boot"
    pp = Path(progress_path) if progress_path else home / "architecture-map" / "boot-progress.json"
    current: dict = {}
    if pp.exists():
        try:
            loaded = json.loads(pp.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            current = {}
    boots: list[dict] = []
    if d.is_dir():
        # bootId is yyyyMMdd-HHmmss, so reverse name order == newest first
        for f in sorted(d.glob("boot-*.jsonl"), reverse=True)[:limit]:
            summary = _summarize_boot_jsonl(f)
            if summary is None:
                continue
            summary["jsonlPath"] = str(f)
            merged: Optional[dict] = None
            snap = f.with_name(f"boot-{summary['bootId']}.final.json")
            if snap.exists():
                try:
                    loaded = json.loads(snap.read_text(encoding="utf-8-sig"))
                    if isinstance(loaded, dict):
                        merged = loaded
                except (OSError, json.JSONDecodeError):
                    merged = None
            if merged is None and current.get("bootId") == summary["bootId"]:
                merged = current
            if merged is not None:
                summary["anomalies"] = merged.get("anomalies") or []
                summary["sweep"] = merged.get("sweep") or {}
                if merged.get("state"):
                    summary["state"] = merged["state"]
                summary["anomalyCount"] = len(summary["anomalies"])
            boots.append(summary)
    return {"current": current, "boots": boots}


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


@router.get("/boot")
async def api_boot(limit: int = 20) -> JSONResponse:
    return JSONResponse(read_boot(limit=limit))
