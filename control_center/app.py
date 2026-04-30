"""FastAPI app for the Hermes Control Center.

Routes:
    GET  /                         main HTML page (shell + 4 panels load via HTMX)
    GET  /partials/health          HTMX partial: health summary
    GET  /partials/approvals       HTMX partial: pending approval queue
    GET  /partials/proposals       HTMX partial: pending Critic proposals
    GET  /partials/activity        HTMX partial: recent events
    POST /api/approve              { thread_id, reason } -> resume graph approved
    POST /api/reject               { thread_id, reason } -> resume graph rejected
    POST /api/proposal/{pid}/applied
    POST /api/proposal/{pid}/skipped
    POST /api/proposal/{pid}/snoozed
    GET  /api/health               JSON for laptop-monitor probe
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import apply as apply_mod
from . import storage
from .ws import broadcaster, ws_endpoint

logger = logging.getLogger(__name__)

THIS_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(THIS_DIR / "templates"))


# ---------------------------------------------------------------------------
# Opt-in auth (Phase D iter2). When HERMES_CC_TOKEN is set in the environment,
# every request that isn't /api/health (the laptop-monitor probe) requires
# either:
#   * Header   X-Hermes-Token: <token>
#   * Cookie   hermes_cc_token=<token>      (set by /login?token=... once)
#   * Query    ?token=<token>               (one-shot fallback for curl)
# Loopback-only listening still applies; this is for LAN exposure if you ever
# bind to 0.0.0.0 in the future.
# ---------------------------------------------------------------------------


class TokenAuthMiddleware(BaseHTTPMiddleware):
    EXEMPT_PATHS = {"/api/health"}

    async def dispatch(self, request: Request, call_next):
        token = os.environ.get("HERMES_CC_TOKEN", "").strip()
        if not token:
            return await call_next(request)  # auth disabled

        path = request.url.path
        if path in self.EXEMPT_PATHS or path.startswith("/static"):
            return await call_next(request)

        # Skip auth on WebSocket here — Starlette processes the WS upgrade
        # before middleware can decide; the WS endpoint can re-check itself.
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # Try the three sources
        provided = (
            request.headers.get("X-Hermes-Token", "")
            or request.cookies.get("hermes_cc_token", "")
            or request.query_params.get("token", "")
        )
        if provided != token:
            return JSONResponse(
                {"ok": False, "error": "unauthorized"}, status_code=401
            )

        # If the token came in via ?token=, set it as a cookie so subsequent
        # navigations work without the query string.
        response = await call_next(request)
        if request.query_params.get("token") == token:
            response.set_cookie(
                "hermes_cc_token",
                token,
                httponly=True,
                samesite="strict",
                max_age=86400,
            )
        return response


app = FastAPI(title="Hermes Control Center", version="1.1.0")
app.add_middleware(TokenAuthMiddleware)
app.mount("/static", StaticFiles(directory=str(THIS_DIR / "static")), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _humanize_age(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        # Tolerate trailing Z
        s = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
        if "+" not in s and "-" not in s[-6:]:
            s += "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs/60)}m ago"
        if secs < 86400:
            return f"{int(secs/3600)}h ago"
        return f"{int(secs/86400)}d ago"
    except Exception:
        return iso[:16] if isinstance(iso, str) else "?"


# ---------------------------------------------------------------------------
# Pages + partials
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Starlette 1.0 signature: TemplateResponse(request, name, context)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
    )


@app.get("/partials/health", response_class=HTMLResponse)
async def partial_health(request: Request):
    h = storage.read_health()
    summary = h.get("summary") or {}
    components = h.get("components") or []
    by_state: dict[str, list] = {"healthy": [], "down": [], "error": [], "other": []}
    for c in components:
        st = c.get("state", "unknown")
        (by_state[st] if st in by_state else by_state["other"]).append(c)
    return templates.TemplateResponse(
        request,
        "_health_panel.html",
        {
            "summary": summary,
            "by_state": by_state,
            "ts": h.get("timestamp"),
            "ts_age": _humanize_age(h.get("timestamp")),
            "error": h.get("_error"),
        },
    )


@app.get("/partials/approvals", response_class=HTMLResponse)
async def partial_approvals(request: Request, since_h: int = 24):
    effective = since_h if since_h and since_h > 0 else None
    pending = storage.list_pending_approvals(window_h=effective)
    for r in pending:
        r["age"] = _humanize_age(r.get("requested_at"))
    return templates.TemplateResponse(
        request, "_approvals_panel.html", {"approvals": pending}
    )


@app.get("/api/v1/pending-approvals")
async def api_v1_pending_approvals(window_h: int = 24):
    """JSON endpoint for cross-process consumers (JobFlow Dashboard,
    laptop-monitor, future CLI dashboards).

    Query params:
        window_h: max age in hours of approval requests to include (default 24).
                  Set to 0 / negative to disable the window filter (returns all
                  unresolved entries).

    Response: {"pending": [...], "count": N, "window_h": <echo>}
    """
    effective = window_h if window_h and window_h > 0 else None
    pending = storage.list_pending_approvals(window_h=effective)
    return JSONResponse(
        {
            "pending": pending,
            "count": len(pending),
            "window_h": effective,
        }
    )


@app.post("/api/apply_packet/next", response_class=HTMLResponse)
async def api_apply_packet_next():
    """iter3: trigger bin/apply_packet_operator.py to open the next pending packet.

    Returns an inline HTMX result message. Long-running (browser-harness can
    take 25-30s); the UI shows a spinner via hx-indicator.
    """
    import asyncio

    operator = Path.home() / ".hermes" / "bin" / "apply_packet_operator.py"
    proc = await asyncio.create_subprocess_exec(
        "python", str(operator),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        return HTMLResponse(
            "<div class='text-rose-600 text-xs'>Operator timed out (90s). "
            "Check ~/.hermes/control-center.log + run from CLI for details.</div>"
        )

    out = (stdout or b"").decode("utf-8", errors="replace").strip()
    err = (stderr or b"").decode("utf-8", errors="replace").strip()
    # Operator's stdout is JSON for processed packets, plain text for empty queue
    if "No pending APPLY_PACKETs" in out or "No pending APPLY_PACKETs" in err:
        return HTMLResponse("<div class='text-zinc-500 text-xs'>No pending APPLY_PACKETs.</div>")

    try:
        import json as _json
        result = _json.loads(out)
        if result.get("ok"):
            return HTMLResponse(
                f"<div class='text-emerald-700 text-xs'>"
                f"✓ Opened {result.get('event_id','?')[:8]} → "
                f"{result.get('title_loaded','?')[:60]}<br>"
                f"<span class='text-zinc-500'>Telegram sent: {result.get('telegram_sent', False)}; "
                f"check JobFlow Decisions topic for screenshot.</span>"
                f"</div>"
            )
        return HTMLResponse(
            f"<div class='text-rose-600 text-xs'>"
            f"⚠ Operator returned not-ok: "
            f"{result.get('harness_error') or result.get('reason') or result.get('error') or '?'}"
            f"</div>"
        )
    except Exception:
        return HTMLResponse(
            f"<div class='text-amber-700 text-xs'>Operator output unparseable; "
            f"stderr: {err[:200]}</div>"
        )


@app.get("/partials/proposals", response_class=HTMLResponse)
async def partial_proposals(request: Request):
    pending = storage.list_pending_proposals()
    for p in pending:
        p["age"] = _humanize_age(p.get("queued_at"))
        full = storage.get_proposal_full(p["proposal_id"])
        if full and isinstance(full, dict):
            payload = full.get("payload") or full
            p["rationale"] = payload.get("rationale", p.get("rationale", ""))
            p["specific_change"] = payload.get("specific_change", p.get("specific_change", ""))
            p["replay"] = payload.get("replay") or {}
    return templates.TemplateResponse(
        request, "_proposals_panel.html", {"proposals": pending}
    )


@app.get("/partials/activity", response_class=HTMLResponse)
async def partial_activity(
    request: Request,
    event_type: str = "",
    source: str = "",
    since: str = "",  # "5m" / "30m" / "1h" / "6h" / "24h" / "" (no limit)
):
    # Parse "since" shorthand
    since_minutes = None
    if since:
        try:
            unit = since[-1].lower()
            n = int(since[:-1])
            since_minutes = n * (1 if unit == "m" else 60 if unit == "h" else 1440 if unit == "d" else 1)
        except Exception:
            since_minutes = None
    events = storage.list_recent_events(
        limit=80,
        event_type=event_type or None,
        source_substring=source or None,
        since_minutes=since_minutes,
    )
    for e in events:
        e["age"] = _humanize_age(e.get("created_at"))
    return templates.TemplateResponse(
        request,
        "_activity_panel.html",
        {
            "events": events,
            "filter_type": event_type,
            "filter_source": source,
            "filter_since": since,
        },
    )


# ---------------------------------------------------------------------------
# API endpoints (HTMX hx-post targets)
# ---------------------------------------------------------------------------


@app.post("/api/approve", response_class=HTMLResponse)
async def api_approve(request: Request, thread_id: str = Form(...), reason: str = Form("")):
    try:
        result = storage.resume_graph(thread_id, "approved", reason)
    except Exception as e:
        return HTMLResponse(
            f"<div class='text-red-600 text-xs'>Resume failed for {thread_id}: {e}</div>",
            status_code=500,
        )
    storage.record_resolution("approval", thread_id, "approved", reason)
    return HTMLResponse(
        f"<div class='text-green-700 text-xs'>✓ Approved {thread_id} → "
        f"stage={result.get('tracker_stage','?')} applied={result.get('applied')}</div>"
    )


@app.post("/api/reject", response_class=HTMLResponse)
async def api_reject(request: Request, thread_id: str = Form(...), reason: str = Form("")):
    try:
        result = storage.resume_graph(thread_id, "rejected", reason)
    except Exception as e:
        return HTMLResponse(
            f"<div class='text-red-600 text-xs'>Resume failed for {thread_id}: {e}</div>",
            status_code=500,
        )
    storage.record_resolution("approval", thread_id, "rejected", reason)
    return HTMLResponse(
        f"<div class='text-amber-700 text-xs'>✗ Rejected {thread_id} → "
        f"stage={result.get('tracker_stage','?')}</div>"
    )


@app.post("/api/proposal/{pid}/applied", response_class=HTMLResponse)
async def api_proposal_applied(pid: str, reason: str = Form("")):
    """iter2: actually run the apply executor for safe kinds; record intent
    only for non-safe kinds. Either way, mark resolved so it disappears from
    the queue.
    """
    proposal = storage.get_proposal_full(pid)
    if not proposal:
        storage.record_resolution("proposal", pid, "applied", reason or "(proposal not found)")
        return HTMLResponse(
            f"<div class='text-amber-700 text-xs'>⚠ Marked {pid} applied (proposal payload not found; intent recorded only)</div>"
        )

    payload = proposal.get("payload") or proposal
    executed, note, rev_path = apply_mod.execute_apply(payload)
    storage.record_resolution(
        "proposal",
        pid,
        "applied",
        f"executed={executed} | {note}" + (f" | reason={reason}" if reason else ""),
    )

    if executed:
        return HTMLResponse(
            f"<div class='text-emerald-700 text-xs'>✓ Applied {pid} — {note} "
            f"(reversal: {rev_path.name if rev_path else 'n/a'})</div>"
        )
    return HTMLResponse(
        f"<div class='text-amber-700 text-xs'>⚠ Intent recorded for {pid} — {note}"
        + (f" (reversal stub: {rev_path.name})" if rev_path else "")
        + "</div>"
    )


@app.post("/api/proposal/{pid}/skipped", response_class=HTMLResponse)
async def api_proposal_skipped(pid: str, reason: str = Form("")):
    storage.record_resolution("proposal", pid, "skipped", reason)
    return HTMLResponse(
        f"<div class='text-zinc-600 text-xs'>⊘ Skipped {pid}</div>"
    )


@app.post("/api/proposal/{pid}/snoozed", response_class=HTMLResponse)
async def api_proposal_snoozed(pid: str, reason: str = Form("")):
    storage.record_resolution("proposal", pid, "snoozed", reason)
    return HTMLResponse(
        f"<div class='text-blue-700 text-xs'>⏱ Snoozed {pid}</div>"
    )


# iter4: bulk actions on the proposals queue
@app.post("/api/proposals/bulk-skip-low-risk", response_class=HTMLResponse)
async def api_proposals_bulk_skip_low_risk():
    """Mark every pending low-risk proposal as 'skipped'. Quick triage."""
    pending = storage.list_pending_proposals()
    skipped: list[str] = []
    for p in pending:
        if (p.get("risk") or "").lower() == "low":
            storage.record_resolution(
                "proposal",
                p["proposal_id"],
                "skipped",
                "bulk-skip-low-risk via Control Center",
            )
            skipped.append(p["proposal_id"])
    return HTMLResponse(
        f"<div class='text-zinc-700 text-xs'>"
        f"⊘ Bulk-skipped {len(skipped)} low-risk proposal(s)"
        + (f": {', '.join(skipped[:3])}{'…' if len(skipped) > 3 else ''}" if skipped else "")
        + "</div>"
    )


@app.post("/api/proposals/bulk-snooze-all", response_class=HTMLResponse)
async def api_proposals_bulk_snooze_all():
    """Snooze every pending proposal. Useful when Diego is busy and wants to
    clear the queue without losing the items.
    """
    pending = storage.list_pending_proposals()
    snoozed: list[str] = []
    for p in pending:
        storage.record_resolution(
            "proposal",
            p["proposal_id"],
            "snoozed",
            "bulk-snooze-all via Control Center",
        )
        snoozed.append(p["proposal_id"])
    return HTMLResponse(
        f"<div class='text-blue-700 text-xs'>"
        f"⏱ Bulk-snoozed {len(snoozed)} proposal(s)"
        + "</div>"
    )


# ---------------------------------------------------------------------------
# Pipeline state JSON API (v1) — for the legacy :3002 dashboard server.js
# and any other process-local client that needs a write-through into
# pipeline.json. Replaces the pattern of foreign processes doing
# read-modify-writeFile on pipeline.json directly (which races with
# PipelineManager and produces history entries without source attribution).
# ---------------------------------------------------------------------------


@app.post("/api/v1/pipeline/jobs/{job_id}/stage")
async def api_v1_pipeline_jobs_stage(job_id: str, request: Request) -> JSONResponse:
    """Update a job's stage via PipelineManager.

    Request body (JSON):
        {
            "stage": "approved",                 # required, in VALID_STAGES
            "actor": "diego",                     # optional, default "diego"
            "source": "legacy_dashboard",         # optional, default "legacy_dashboard"
            "notes": "...",                       # optional
            "metadata": {...}                     # optional, merged non-destructively
        }

    Returns: {"ok": True, "job_id": "...", "stage": "..."} on success.

    Errors:
        400 if stage missing
        404 if job_id not in pipeline.json (no upsert from this endpoint —
            unknown jobs are surfaced as errors so callers don't accidentally
            create stray records via typos)
        500 on PipelineManager exception
    """
    from pipeline_state import PipelineManager

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)

    stage = (payload.get("stage") or "").strip()
    if not stage:
        return JSONResponse({"ok": False, "error": "stage is required"}, status_code=400)

    actor = (payload.get("actor") or "diego").strip()
    source = (payload.get("source") or "legacy_dashboard").strip()
    notes = payload.get("notes") or ""
    metadata = payload.get("metadata") or None

    mgr = PipelineManager()
    if mgr.get_job(job_id) is None:
        return JSONResponse(
            {"ok": False, "error": f"job {job_id} not found in pipeline"},
            status_code=404,
        )

    try:
        mgr.update_stage(
            job_id=job_id,
            new_stage=stage,
            actor=actor,
            source=source,
            notes=notes,
            metadata=metadata,
        )
    except Exception as exc:
        logger.exception("api_v1_pipeline_jobs_stage failed for %s", job_id)
        return JSONResponse(
            {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"},
            status_code=500,
        )

    return JSONResponse({"ok": True, "job_id": job_id, "stage": stage})


# ---------------------------------------------------------------------------
# Health (for laptop-monitor probe)
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def api_health():
    return JSONResponse(
        {
            "ok": True,
            "service": "hermes-control-center",
            "version": "1.1.0",  # iter2: WebSocket + filters + critic-apply + auth
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ws_subscribers": len(broadcaster._subs),
        }
    )


# WebSocket endpoint for live event stream (Phase D iter2).
@app.websocket("/ws")
async def websocket_route(websocket: WebSocket):
    await ws_endpoint(websocket)


@app.on_event("startup")
async def _startup():
    await broadcaster.start()


@app.on_event("shutdown")
async def _shutdown():
    await broadcaster.stop()
