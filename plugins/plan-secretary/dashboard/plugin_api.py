"""Plan Secretary dashboard backend — FastAPI router mounted by the web server
under ``/api/plugins/plan-secretary/`` (discovered via dashboard/manifest.json).

Serves REAL pending-capture data from the hook plugin's state file, so the
desktop pane shows the same truth as the underlying secretary.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from hermes_constants import get_hermes_home

router = APIRouter()


def _load_pending() -> dict:
    p = Path(get_hermes_home()) / "state" / "plan_secretary" / "pending_captures.json"
    if not p.exists():
        return {"count": 0, "recent": [], "status": "empty"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"count": 0, "recent": [], "status": "error"}
    caps = data.get("captures", []) if isinstance(data, dict) else []
    pending = [c for c in caps if c.get("status") == "pending"]
    recent = [
        {
            "id": str(c.get("id", "")),
            "text": str(c.get("text", ""))[:160],
            "session": str(c.get("source_session_id", "")),
            "ts": str(c.get("created_at", "")),
        }
        for c in pending[-5:][::-1]
    ]
    return {"count": len(pending), "recent": recent, "status": "ok"}


@router.get("/pending")
def pending() -> dict:
    """GET /pending — real pending count + recent items."""
    return _load_pending()
