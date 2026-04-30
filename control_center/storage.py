"""Read-only data access + a tiny resolution-state SQLite for the Control Center.

We don't reimplement the canonical state — every panel reads its source-of-truth
file/DB directly. We only persist a small "resolution" log so resolved items
disappear from the queue across restarts.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

HERMES = Path.home() / ".hermes"

STATUS_JSON = Path("C:/Users/diego/architecture-map/status.json")
EVENT_BUS = HERMES / "events" / "event_bus.db"
APPROVAL_LOG = HERMES / "graphs" / "approval-log.jsonl"
APPLY_LOG = HERMES / "graphs" / "apply-log.jsonl"
TRACKER_LOG = HERMES / "graphs" / "tracker-log.jsonl"
CHECKPOINTS_DB = HERMES / "graphs" / "checkpoints.db"
CRITIC_QUEUE = HERMES / "profiles" / "critic" / "workspace" / "whatsapp_queue.jsonl"
CRITIC_CHANGELOG = HERMES / "profiles" / "critic" / "workspace" / "changelog.jsonl"

# Control Center's own state DB (resolution log)
STATE_DB = HERMES / "control_center" / "state.db"


# ---------------------------------------------------------------------------
# Resolution state DB
# ---------------------------------------------------------------------------


def _ensure_state_db() -> None:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STATE_DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS resolutions (
            kind TEXT NOT NULL,           -- 'approval' | 'proposal'
            item_id TEXT NOT NULL,        -- thread_id for approval, proposal_id for proposal
            decision TEXT NOT NULL,       -- 'approved'|'rejected'|'applied'|'skipped'|'snoozed'
            reason TEXT,
            decided_at TEXT NOT NULL,
            PRIMARY KEY (kind, item_id, decided_at)
        )
        """
    )
    conn.commit()
    conn.close()


def record_resolution(kind: str, item_id: str, decision: str, reason: str = "") -> None:
    _ensure_state_db()
    conn = sqlite3.connect(str(STATE_DB))
    conn.execute(
        "INSERT OR REPLACE INTO resolutions (kind, item_id, decision, reason, decided_at) VALUES (?, ?, ?, ?, ?)",
        (kind, item_id, decision, reason, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
    )
    conn.commit()
    conn.close()


def list_resolutions(kind: str) -> dict[str, dict]:
    """Return {item_id: {decision, reason, decided_at}} for that kind."""
    _ensure_state_db()
    out: dict[str, dict] = {}
    if not STATE_DB.exists():
        return out
    conn = sqlite3.connect(str(STATE_DB))
    for row in conn.execute(
        "SELECT item_id, decision, reason, decided_at FROM resolutions WHERE kind=?",
        (kind,),
    ):
        out[row[0]] = {"decision": row[1], "reason": row[2], "decided_at": row[3]}
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Health (laptop-monitor status.json)
# ---------------------------------------------------------------------------


def read_health() -> dict[str, Any]:
    if not STATUS_JSON.exists():
        return {"summary": {}, "components": [], "timestamp": None, "_error": f"{STATUS_JSON} missing"}
    try:
        # status.json is written by laptop-monitor.ps1 which uses PowerShell's
        # default UTF-8-with-BOM encoding. Python json.load chokes on the BOM
        # unless we read with utf-8-sig.
        return json.loads(STATUS_JSON.read_text(encoding="utf-8-sig"))
    except Exception as e:
        return {"summary": {}, "components": [], "timestamp": None, "_error": str(e)}


# ---------------------------------------------------------------------------
# Approvals (graphs HITL queue)
# ---------------------------------------------------------------------------


def list_pending_approvals(window_h: int | None = None) -> list[dict]:
    """Return APPROVAL_REQUEST entries in approval-log.jsonl that don't have a
    matching resolution recorded in our state DB.

    Each entry: {thread_id, job_id, job_title, job_company, score, primary_angle,
                 cover_preview, requested_at}.
    thread_id reconstructed from job_id ('job-<id>') matching the LangGraph runner's default.

    If ``window_h`` is set, entries older than now - window_h hours are excluded.
    The badge condition (Gap C3) uses window_h=24 so stale pending entries
    don't trigger the visual flag indefinitely.
    """
    if not APPROVAL_LOG.exists():
        return []
    seen_resolutions = list_resolutions("approval")

    cutoff = None
    if window_h is not None and window_h > 0:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)

    out: list[dict] = []
    for line in APPROVAL_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("auto_resolved"):
            continue
        jid = e.get("job_id") or "unknown"
        thread_id = f"job-{jid}"
        if thread_id in seen_resolutions:
            continue

        requested_at = e.get("at") or ""
        if cutoff is not None:
            from datetime import datetime, timezone
            try:
                s = requested_at.replace("Z", "+00:00") if requested_at.endswith("Z") else requested_at
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except Exception:
                # Unparseable timestamp -> treat as in-window (fail-open)
                pass

        out.append(
            {
                "thread_id": thread_id,
                "job_id": jid,
                "job_title": e.get("job_title", ""),
                "job_company": e.get("job_company", ""),
                "score": e.get("score"),
                "primary_angle": e.get("primary_angle", ""),
                "cover_preview": e.get("cover_preview", ""),
                "requested_at": requested_at,
            }
        )
    # Newest first
    out.sort(key=lambda r: r.get("requested_at") or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# Critic proposals queue
# ---------------------------------------------------------------------------


def list_pending_proposals() -> list[dict]:
    """Read whatsapp_queue.jsonl, filter out resolved ones."""
    if not CRITIC_QUEUE.exists():
        return []
    seen = list_resolutions("proposal")
    proposals: list[dict] = []
    for line in CRITIC_QUEUE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        pid = e.get("proposal_id")
        if not pid:
            continue
        if pid in seen:
            continue
        proposals.append(e)
    # Dedup: a proposal_id may appear across multiple Critic runs; keep newest queued_at
    by_id: dict[str, dict] = {}
    for p in proposals:
        existing = by_id.get(p["proposal_id"])
        if not existing or (p.get("queued_at") or "") > (existing.get("queued_at") or ""):
            by_id[p["proposal_id"]] = p
    out = list(by_id.values())
    out.sort(key=lambda r: r.get("queued_at") or "", reverse=True)
    return out


def get_proposal_full(proposal_id: str) -> Optional[dict]:
    """Read the full proposal JSON from mailbox if available, else fallback to queue stub."""
    inbox = HERMES / "mailbox" / "main" / "inbox"
    if inbox.exists():
        # Files are named like {ts}_{pid}_CRITIC_PROPOSAL_critic.json — newest wins
        candidates = sorted(
            inbox.glob(f"*_{proposal_id}_CRITIC_PROPOSAL_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            try:
                return json.loads(candidates[0].read_text(encoding="utf-8"))
            except Exception:
                pass
    # Fallback: queue stub (no rationale/etc.)
    for q in list_pending_proposals():
        if q["proposal_id"] == proposal_id:
            return q
    return None


# ---------------------------------------------------------------------------
# Activity feed (recent events)
# ---------------------------------------------------------------------------

ACTIVITY_EVENT_TYPES = (
    "job_discovered", "job_vip_discovered", "job_scored", "job_high_score",
    "tailor_completed", "application_ready", "application_submitted",
    "approval_request", "apply_packet", "stage_transition",
    "critic_proposal", "interview_signal", "offer_signal",
    "agent_error", "cron_failed", "cron_failed_consecutive",
    "gateway_health", "secret_detected",
)


def list_recent_events(
    limit: int = 50,
    *,
    event_type: Optional[str] = None,
    source_substring: Optional[str] = None,
    since_minutes: Optional[int] = None,
) -> list[dict]:
    """Query the bus with optional filters (Phase D iter2).

    Filters:
      event_type: exact match. If empty/None, includes all ACTIVITY_EVENT_TYPES.
      source_substring: substring match on source column (case-insensitive).
      since_minutes: only events whose created_at is newer than now-N minutes.
    """
    if not EVENT_BUS.exists():
        return []
    conn = sqlite3.connect(str(EVENT_BUS))

    where_clauses: list[str] = []
    params: list[Any] = []

    if event_type:
        where_clauses.append("event_type = ?")
        params.append(event_type)
    else:
        types_csv = ",".join(f"'{t}'" for t in ACTIVITY_EVENT_TYPES)
        where_clauses.append(f"event_type IN ({types_csv})")

    if source_substring:
        where_clauses.append("LOWER(source) LIKE ?")
        params.append(f"%{source_substring.lower()}%")

    if since_minutes is not None and since_minutes > 0:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()
        where_clauses.append("created_at >= ?")
        params.append(cutoff)

    where_sql = " AND ".join(where_clauses)
    rows = conn.execute(
        f"""
        SELECT event_id, event_type, source, priority, created_at, payload
        FROM events
        WHERE {where_sql}
        ORDER BY rowid DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[5]) if r[5] else {}
        except Exception:
            payload = {}
        out.append(
            {
                "event_id": r[0],
                "event_type": r[1],
                "source": r[2],
                "priority": r[3],
                "created_at": r[4],
                "payload": payload,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Resume helpers (call into graphs.resume_full)
# ---------------------------------------------------------------------------


def resume_graph(thread_id: str, decision: str, reason: str = "") -> dict:
    """Resume a paused LangGraph run with the supplied decision.

    The graph's tracker_update_node now writes through PipelineManager
    automatically (iter cross-surface unification) — this function just calls
    resume_full and reports the result. We ALSO emit an approval-event audit
    pass through pipeline.json directly (source=control_center) so the
    history entry records that Diego clicked a UI button, not that the
    graph-internal approval_hitl returned.
    """
    import sys

    sys.path.insert(0, str(HERMES / "agent-src"))
    from graphs import resume_full  # type: ignore

    payload = {
        "approval": "approved" if decision == "approved" else "rejected",
        "approval_reason": reason or f"resumed via Control Center: {decision}",
    }
    result = resume_full(thread_id, payload)

    # Write an explicit control_center history entry to pipeline.json so the
    # audit trail shows WHERE the approval came from. The graph will write a
    # separate langgraph-sourced entry in tracker_update_node.
    #
    # IMPORTANT: the real job_id lives in result['job_id'] (LangGraph state),
    # NOT in thread_id. thread_id is often "job-<id>" but may be a custom tag
    # like "real-ft-vp-ai". If we use thread_id we create a stray pipeline
    # record; always prefer state.job_id.
    #
    # Stage policy: MIRROR what the graph actually produced in tracker_stage
    # (not a forced "ready_to_submit"). This way:
    #   - If the graph paused at HITL and Diego approved, tracker_update_node
    #     writes stage=ready_to_submit (or submitted if apply_node ran), and
    #     our control_center entry stays at that same stage — just audits WHO
    #     clicked the button.
    #   - If the graph already completed (e.g. decision=review, no HITL), the
    #     button click doesn't forcibly override the stage; it just records
    #     that Diego acknowledged the run.
    try:
        from pipeline_state import PipelineManager

        mgr = PipelineManager()
        job = result.get("job") or {}
        job_id = result.get("job_id") or job.get("id")
        if not job_id:
            job_id = thread_id[4:] if thread_id.startswith("job-") else thread_id
        graph_stage = result.get("tracker_stage")
        # Fall back to decision-implied stage only if tracker didn't run at all.
        if not graph_stage:
            graph_stage = (
                "ready_to_submit" if decision == "approved"
                else "rejected_by_user"
            )
        audit_note = (
            f"Control Center: diego clicked {decision!r}"
            + (f" — {reason}" if reason else "")
        )
        mgr.update_stage(
            job_id=job_id,
            new_stage=graph_stage,
            actor="diego",
            source="control_center",
            notes=audit_note,
            metadata={
                "title": job.get("title"),
                "company": job.get("company"),
                "score": result.get("score"),
                "url": job.get("url"),
                "apply_url": job.get("apply_url"),
            },
        )
    except Exception as exc:
        import logging
        logging.warning("resume_graph: pipeline.json audit write failed: %s", exc)

    return {
        "thread_id": thread_id,
        "approval": result.get("approval"),
        "applied": bool(result.get("applied")),
        "tracker_stage": result.get("tracker_stage"),
        "final_score": result.get("score"),
    }
