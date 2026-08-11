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
from events import paths as event_paths

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
    db = db_path or event_paths.events_db_path()
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


def _iso_now(now: Optional[str]) -> str:
    if now is not None:
        return now
    return datetime.now(timezone.utc).isoformat()


def _read_allowlist_live_gateway_imports(path: Path, errors: list[str]) -> dict[str, bool]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"allowlist: {type(exc).__name__}: {exc}")
        return {}
    targets = data.get("targets", {}) if isinstance(data, dict) else {}
    if not isinstance(targets, dict):
        return {}
    return {
        str(name): bool(config.get("live_gateway_imports", False))
        for name, config in targets.items()
        if isinstance(config, dict)
    }


def _coerce_config_id_list(raw: Any) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items = raw
    elif isinstance(raw, str):
        items = (item for item in raw.split(",") if item.strip())
    else:
        items = (raw,)
    return frozenset(str(item).strip() for item in items if str(item).strip())


# Keep this local and explicit: importing gateway configuration can initialize
# runtime-only dependencies, while accepting arbitrary YAML mapping names leaks
# unrelated configuration sections into the read-only Canvas projection.
# ``local`` is deliberately absent because it is not an external command transport.
_DDP_AUTH_PLATFORM_NAMES = frozenset({
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal",
    "mattermost", "matrix", "homeassistant", "email", "sms", "dingtalk",
    "api_server", "webhook", "msgraph_webhook", "feishu", "wecom",
    "wecom_callback", "weixin", "bluebubbles", "qqbot", "yuanbao", "relay",
})


def _read_gateway_auth_summary(path: Path, errors: list[str]) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:
        errors.append(f"gateway_config: {type(exc).__name__}: {exc}")
        return {}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        errors.append("gateway_config: YAMLError: could not parse config")
        return {}
    except OSError as exc:
        errors.append(f"gateway_config: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(parsed, dict):
        return {}

    nested = parsed.get("platforms") if isinstance(parsed.get("platforms"), dict) else {}
    names = {
        name for name in set(nested) | set(parsed)
        if isinstance(name, str) and name in _DDP_AUTH_PLATFORM_NAMES
    }
    out: dict[str, dict[str, object]] = {}
    for name in sorted(names):
        nested_block = nested.get(name, {}) if isinstance(nested, dict) else {}
        nested_extra = nested_block.get("extra", {}) if isinstance(nested_block, dict) else {}
        nested_extra = nested_extra if isinstance(nested_extra, dict) else {}
        top_level = parsed.get(name, {})
        top_level = top_level if isinstance(top_level, dict) else {}
        merged = {**nested_extra}
        for key in ("allow_admin_from", "group_allow_admin_from"):
            if key in top_level:
                merged[key] = top_level[key]
        dm_ids = _coerce_config_id_list(merged.get("allow_admin_from"))
        group_ids = _coerce_config_id_list(merged.get("group_allow_admin_from"))
        out[str(name)] = {
            "dm_configured": bool(dm_ids),
            "dm_admin_count": len(dm_ids),
            "group_configured": bool(group_ids),
            "group_admin_count": len(group_ids),
        }
    return out


def _parse_autonomy_gate(ref: object) -> Optional[dict]:
    if not isinstance(ref, str):
        return None
    parts = ref.split(":", 4)
    if len(parts) != 5:
        return None
    mode, action, tier, reason, _digest = parts
    return {"mode": mode, "action": action, "tier": tier, "reason": reason}


_DDP_SIDE_STATES = frozenset({
    "DUPLICATE", "SUPPRESSED", "DECLINED", "FAILED", "CANCELLED",
    "REVERT_REQUESTED", "REVERTED",
})
_DDP_MAX_PAGE_SIZE = 100
_DDP_APPROVAL_QUEUE_DISPLAY_CAP = 200
_DDP_TITLE_DISPLAY_CAP = 160
_DDP_CRITERION_DISPLAY_CAP = 220


def _truncate_display_text(value: object, limit: int) -> str:
    """Bound an untrusted text field for read-only Canvas presentation."""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_request_summary(row: sqlite3.Row, *, lease: Optional[dict], latest_artifact: Optional[dict]) -> dict:
    """Return presentation-safe request fields from a ledger row.

    The API returns data, not HTML; nevertheless it deliberately excludes raw
    envelope/evidence strings so Canvas cannot accidentally turn persisted
    untrusted content into executable markup.
    """
    try:
        envelope = json.loads(row["envelope_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        envelope = {}
    if not isinstance(envelope, dict):
        envelope = {}
    criteria = envelope.get("acceptance_criteria")
    return {
        "request_id": row["request_id"],
        "state": row["state"],
        "source_agent": row["source_agent"],
        "source_kind": row["source_kind"],
        "target_repo": row["target_repo"],
        "target_subsystem": row["target_subsystem"],
        "kind": row["kind"],
        "severity": row["severity"],
        "terminal_reason": row["terminal_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "title": _truncate_display_text(
            envelope.get("title") or "(unreadable envelope)", _DDP_TITLE_DISPLAY_CAP,
        ),
        "acceptance_criteria": [
            _truncate_display_text(item, _DDP_CRITERION_DISPLAY_CAP)
            for item in criteria
        ] if isinstance(criteria, list) else [],
        "lease": lease,
        "latest_artifact": latest_artifact,
    }


def _safe_evidence_summary(raw: object) -> object:
    """Parse structured evidence while retaining only a small safe shape."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return {"status": "unreadable"}
    if not isinstance(parsed, dict):
        return {"status": "unreadable"}
    out: dict[str, object] = {}
    for key in ("reason", "kind", "summary", "source", "ref"):
        if key not in parsed:
            continue
        value = parsed[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out or {"status": "recorded"}


def _parse_page_args(
    *, state: Optional[str], source: Optional[str], cursor: Optional[str], limit: int, errors: list[str],
) -> tuple[Optional[str], Optional[str], Optional[str], int]:
    clean_state = state.strip().upper() if isinstance(state, str) else None
    if clean_state and (not clean_state.replace("_", "").isalpha() or len(clean_state) > 40):
        errors.append("query: invalid state filter")
        clean_state = None
    clean_source = source.strip() if isinstance(source, str) else None
    if clean_source and len(clean_source) > 200:
        errors.append("query: source filter is too long")
        clean_source = None
    clean_cursor = cursor.strip() if isinstance(cursor, str) else None
    if clean_cursor and (len(clean_cursor) > 200 or not clean_cursor.startswith("dwr_")):
        errors.append("query: invalid cursor")
        clean_cursor = None
    try:
        clean_limit = int(limit)
    except (TypeError, ValueError):
        clean_limit = _DDP_MAX_PAGE_SIZE
        errors.append("query: invalid limit")
    if clean_limit < 1:
        clean_limit = 1
        errors.append("query: limit must be positive")
    if clean_limit > _DDP_MAX_PAGE_SIZE:
        clean_limit = _DDP_MAX_PAGE_SIZE
        errors.append(f"query: limit capped at {_DDP_MAX_PAGE_SIZE}")
    return clean_state, clean_source, clean_cursor, clean_limit


def read_devflow(
    *,
    ledger_path: Optional[Path] = None,
    allowlist_path: Optional[Path] = None,
    sentinel_path: Optional[Path] = None,
    gateway_config_path: Optional[Path] = None,
    cron_jobs_path: Optional[Path] = None,
    now: Optional[str] = None,
    state: Optional[str] = None,
    source: Optional[str] = None,
    request_id: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 40,
) -> dict:
    """Return a read-only, schema-tolerant DevFlow ledger summary for Canvas.

    This intentionally opens SQLite in URI read-only mode rather than using
    ``DelegationLedger``: constructing that class can initialize/migrate schema.
    Missing files and Stage-1 tables produce empty data plus diagnostics, never
    writes or exceptions.
    """
    root = _root()
    db = (Path(ledger_path) if ledger_path is not None
          else event_paths.delegation_ledger_path())
    allowlist = (Path(allowlist_path) if allowlist_path is not None
                 else event_paths.devflow_allowlist_path())
    sentinel = (Path(sentinel_path) if sentinel_path is not None
                else event_paths.autonomy_sentinel_path())
    config_path = (Path(gateway_config_path) if gateway_config_path is not None
                   else root / "profiles" / "main" / "config.yaml")
    cron_path = (Path(cron_jobs_path) if cron_jobs_path is not None
                 else root / "profiles" / "main" / "cron" / "jobs.json")
    generated_at = _iso_now(now)
    errors: list[str] = []
    result = {
        "generated_at": generated_at,
        "ledger_available": False,
        "ledger_total": 0,
        "by_state": {},
        "by_source": {},
        "active_leases": [],
        "expired_leases": [],
        "awaiting_approval_count": 0,
        "autonomy_decisions_recent": [],
        "live_gateway_imports": _read_allowlist_live_gateway_imports(allowlist, errors),
        "autonomy_sentinel_note": (
            "enabled" if sentinel.is_file() else
            "invalid (not a file)" if sentinel.exists() else
            "no (shadow-first)"
        ),
        "side_state_counts": {},
        "requests": [],
        "approval_queue": [],
        "approval_queue_page": {
            "limit": _DDP_APPROVAL_QUEUE_DISPLAY_CAP,
            "next_cursor": None,
            "has_more": False,
        },
        "request_page": {"limit": 0, "next_cursor": None, "has_more": False},
        "request_detail": None,
        "ledger_freshness": {
            "latest_request_updated_at": None,
            "latest_transition_at": None,
            "last_successful_read_at": None,
        },
        "read_errors": errors,
    }
    result["ddp_auth_readiness"] = _read_gateway_auth_summary(config_path, errors)
    try:
        result["tick_health"] = [
            {
                "name": job.get("name"),
                "state": job.get("state"),
                "last_run_at": job.get("last_run_at"),
                "last_status": job.get("last_status"),
                "consecutive_errors": job.get("consecutive_errors"),
                "next_run_at": job.get("next_run_at"),
            }
            for job in read_cron(jobs_path=cron_path)
            if isinstance(job.get("name"), str) and job["name"].startswith("devflow-")
        ]
    except Exception as exc:  # noqa: BLE001 - read-only API must fail soft
        errors.append(f"tick_health: {type(exc).__name__}: {exc}")
        result["tick_health"] = []
    clean_state, clean_source, clean_cursor, clean_limit = _parse_page_args(
        state=state, source=source, cursor=cursor, limit=limit, errors=errors,
    )
    result["request_page"]["limit"] = clean_limit
    invalid_request_id = (
        request_id is not None
        and (not isinstance(request_id, str) or not request_id.startswith("dwr_") or len(request_id) > 200)
    )
    if invalid_request_id:
        errors.append("query: invalid request_id")
    if not db.exists():
        result["ledger_available"] = True
        result["ledger_freshness"]["last_successful_read_at"] = generated_at
        return result

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        state_rows = conn.execute(
            "SELECT state, COUNT(*) AS count FROM requests GROUP BY state"
        ).fetchall()
        source_rows = conn.execute(
            "SELECT source_agent, COUNT(*) AS count FROM requests GROUP BY source_agent"
        ).fetchall()
        result["by_state"] = {str(row["state"]): int(row["count"]) for row in state_rows}
        result["by_source"] = {
            str(row["source_agent"]): int(row["count"]) for row in source_rows
        }
        result["ledger_total"] = sum(result["by_state"].values())
        result["awaiting_approval_count"] = result["by_state"].get("TRIAGED", 0)
        result["side_state_counts"] = {
            key: value for key, value in result["by_state"].items() if key in _DDP_SIDE_STATES
        }
        result["ledger_available"] = True
    except sqlite3.Error as exc:
        errors.append(f"requests: {type(exc).__name__}: {exc}")
        if conn is not None:
            conn.close()
        return result

    assert conn is not None
    if invalid_request_id:
        result["ledger_freshness"]["last_successful_read_at"] = generated_at
        conn.close()
        return result

    lease_by_request: dict[str, dict] = {}
    try:
        lease_rows = conn.execute("SELECT * FROM leases ORDER BY expires_at").fetchall()
        for row in lease_rows:
            lease = dict(row)
            lease_by_request[str(lease.get("request_id") or "")] = lease
            key = "expired_leases" if str(lease.get("expires_at") or "") < generated_at else "active_leases"
            result[key].append(lease)
    except sqlite3.Error as exc:
        errors.append(f"leases: {type(exc).__name__}: {exc}")

    latest_artifact_by_request: dict[str, dict] = {}
    try:
        artifact_rows = conn.execute(
            "SELECT request_id, kind, ref, created_at FROM artifacts ORDER BY id DESC"
        ).fetchall()
        for row in artifact_rows:
            artifact = {"kind": row["kind"], "ref": row["ref"], "created_at": row["created_at"]}
            latest_artifact_by_request.setdefault(row["request_id"], artifact)
            if row["kind"] != "autonomy_gate":
                continue
            decision = _parse_autonomy_gate(row["ref"])
            if decision is not None and len(result["autonomy_decisions_recent"]) < 20:
                result["autonomy_decisions_recent"].append({
                    "request_id": row["request_id"], **decision, "created_at": row["created_at"],
                })
    except sqlite3.Error as exc:
        errors.append(f"artifacts: {type(exc).__name__}: {exc}")

    try:
        approval_rows = conn.execute(
            "SELECT request_id, state, source_agent, source_kind, target_repo, target_subsystem, "
            "kind, severity, terminal_reason, created_at, updated_at, envelope_json "
            "FROM requests WHERE state = 'TRIAGED' ORDER BY request_id DESC LIMIT ?",
            (_DDP_APPROVAL_QUEUE_DISPLAY_CAP + 1,),
        ).fetchall()
        has_more = len(approval_rows) > _DDP_APPROVAL_QUEUE_DISPLAY_CAP
        approval_rows = approval_rows[:_DDP_APPROVAL_QUEUE_DISPLAY_CAP]
        result["approval_queue"] = [
            _safe_request_summary(
                row,
                lease=lease_by_request.get(row["request_id"]),
                latest_artifact=latest_artifact_by_request.get(row["request_id"]),
            )
            for row in approval_rows
        ]
        result["approval_queue_page"] = {
            "limit": _DDP_APPROVAL_QUEUE_DISPLAY_CAP,
            "next_cursor": approval_rows[-1]["request_id"] if has_more and approval_rows else None,
            "has_more": has_more,
        }
    except sqlite3.Error as exc:
        errors.append(f"approval_queue: {type(exc).__name__}: {exc}")

    try:
        latest_request = conn.execute("SELECT MAX(updated_at) FROM requests").fetchone()[0]
        result["ledger_freshness"]["latest_request_updated_at"] = latest_request
    except sqlite3.Error as exc:
        errors.append(f"freshness: {type(exc).__name__}: {exc}")

    try:
        transition_latest = conn.execute("SELECT MAX(created_at) FROM transitions").fetchone()[0]
        result["ledger_freshness"]["latest_transition_at"] = transition_latest
    except sqlite3.Error as exc:
        errors.append(f"transitions: {type(exc).__name__}: {exc}")

    try:
        where: list[str] = []
        params: list[object] = []
        if clean_state:
            where.append("state = ?")
            params.append(clean_state)
        if clean_source:
            where.append("source_agent = ?")
            params.append(clean_source)
        if clean_cursor:
            where.append("request_id < ?")
            params.append(clean_cursor)
        if request_id:
            where.append("request_id = ?")
            params.append(request_id)
        predicate = f" WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            "SELECT request_id, state, source_agent, source_kind, target_repo, target_subsystem, kind, "
            "severity, terminal_reason, created_at, updated_at, envelope_json FROM requests"
            + predicate + " ORDER BY request_id DESC LIMIT ?",
            (*params, clean_limit + 1),
        ).fetchall()
        has_more = len(rows) > clean_limit
        rows = rows[:clean_limit]
        result["requests"] = [
            _safe_request_summary(
                row,
                lease=lease_by_request.get(row["request_id"]),
                latest_artifact=latest_artifact_by_request.get(row["request_id"]),
            )
            for row in rows
        ]
        result["request_page"] = {
            "limit": clean_limit,
            "next_cursor": rows[-1]["request_id"] if rows else None,
            "has_more": has_more,
        }
    except sqlite3.Error as exc:
        errors.append(f"request_detail: {type(exc).__name__}: {exc}")

    if request_id and result["requests"]:
        detail = dict(result["requests"][0])
        try:
            transitions = conn.execute(
                "SELECT from_state, to_state, policy_version, evidence_ref, created_at "
                "FROM transitions WHERE request_id=? ORDER BY id", (request_id,),
            ).fetchall()
            detail["transitions"] = [dict(row) for row in transitions]
        except sqlite3.Error as exc:
            errors.append(f"transitions: {type(exc).__name__}: {exc}")
            detail["transitions"] = []
        try:
            evidence = conn.execute(
                "SELECT evidence_json, created_at FROM evidence_log WHERE request_id=? ORDER BY id", (request_id,),
            ).fetchall()
            detail["evidence"] = [
                {"created_at": row["created_at"], "summary": _safe_evidence_summary(row["evidence_json"])}
                for row in evidence
            ]
        except sqlite3.Error as exc:
            errors.append(f"evidence_log: {type(exc).__name__}: {exc}")
            detail["evidence"] = []
        try:
            decisions = conn.execute(
                "SELECT decision, evidence_ref, created_at "
                "FROM human_decisions WHERE request_id=? ORDER BY id",
                (request_id,),
            ).fetchall()
            detail["human_decisions"] = [dict(row) for row in decisions]
        except sqlite3.Error as exc:
            errors.append(f"human_decisions: {type(exc).__name__}: {exc}")
            detail["human_decisions"] = []
        result["request_detail"] = detail

    result["ledger_freshness"]["last_successful_read_at"] = generated_at
    conn.close()
    return result


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


@router.get("/devflow")
async def api_devflow(
    state: str = "",
    source: str = "",
    request_id: str = "",
    cursor: str = "",
    limit: int = 40,
) -> JSONResponse:
    return JSONResponse(read_devflow(
        state=state or None,
        source=source or None,
        request_id=request_id or None,
        cursor=cursor or None,
        limit=limit,
    ))
