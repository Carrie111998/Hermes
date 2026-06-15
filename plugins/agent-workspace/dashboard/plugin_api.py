"""Agent Workspace dashboard plugin — backend API routes.

Mounted at /api/plugins/agent-workspace/ by the dashboard plugin system, BEHIND
the dashboard's session-token auth middleware (same gate as every other
/api/plugins/... route). This is the "real" data path that replaces the test
dashboard's dev-only vite proxy + browser-held Bearer key:

    browser (dashboard session token / OAuth)
      -> GET /api/plugins/agent-workspace/learnings   (same-origin, authed)
        -> call_kernel(...) over MCP Streamable HTTP   (Bearer from container env)
          -> FRIDAI kernel learnings_stats / recall_learnings
            -> real grounding-store data

Connection doctrine (FRIDAY-OS, 2026-06-12): internal services reach the kernel
over MCP, not the external gateway GraphQL. Hermes is internal, so we call the
kernel's /mcp directly — same path the fridai-memory provider uses.

Reuse, don't duplicate: the kernel MCP client (call_kernel) already ships with
the deployed fridai-memory plugin. We import that one implementation rather than
vendoring a second copy. TODO(promote): once the frontend lands and there are
two committed consumers, lift call_kernel.py into a shared Hermes FRIDAI module
both plugins import, and drop this cross-plugin importlib shim.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException

log = logging.getLogger(__name__)

router = APIRouter()

_call_kernel: Optional[Callable[..., Any]] = None


def _load_call_kernel() -> Callable[..., Any]:
    """Import the deployed fridai-memory kernel client (stdlib-only MCP
    Streamable HTTP). Cached after first load. Raises RuntimeError if the
    fridai-memory plugin is not present (it carries the only implementation)."""
    global _call_kernel
    if _call_kernel is not None:
        return _call_kernel
    home = os.getenv("HERMES_HOME", "/opt/data")
    candidates = [
        Path(home) / "plugins" / "fridai-memory" / "call_kernel.py",
        # co-deployed alongside this plugin (bundled set / repo layout)
        Path(__file__).resolve().parents[2] / "fridai-memory" / "call_kernel.py",
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("fridai_call_kernel", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _call_kernel = mod.call_kernel
                log.info("agent-workspace: loaded call_kernel from %s", path)
                return _call_kernel
    raise RuntimeError(
        "agent-workspace: fridai-memory/call_kernel.py not found "
        "(the kernel MCP client lives with the fridai-memory plugin)"
    )


@router.get("/health")
async def health():
    """Liveness + kernel reachability for this plugin's backend.

    Confirms the plugin mounted AND that the MCP path to the kernel is wired,
    without leaking the Bearer key. `kernel` is 'reachable' only if a real
    learnings_stats call round-trips.
    """
    info = {
        "ok": True,
        "plugin": "agent-workspace",
        "kernel_mcp_url": os.getenv("FRIDAY_MCP_URL", "http://mcp:8643/mcp"),
        "api_key_present": bool(os.getenv("FRIDAY_API_KEY")),
    }
    try:
        call_kernel = _load_call_kernel()
        stats = call_kernel("learnings_stats", timeout=15.0)
        info["kernel"] = "reachable"
        info["learnings_total"] = stats.get("total") if isinstance(stats, dict) else None
    except Exception as exc:  # noqa: BLE001 — surface the blocker, don't crash the dashboard
        info["kernel"] = "unreachable"
        info["error"] = str(exc)
    return info


@router.get("/learnings")
async def learnings(query: str = "recent decisions", limit: int = 8):
    """The aux-model feed: grounding-store census + a semantic recall.

    Calls the kernel tools learnings_stats and recall_learnings over MCP and
    returns their parsed results. Same shape the test dashboard consumed
    (stats: {total,by_kind,by_status,edges}; feed.results: [...]), now sourced
    through the dashboard's own auth instead of a browser-held key.
    """
    try:
        call_kernel = _load_call_kernel()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    out: dict[str, Any] = {"query": query, "limit": limit}
    try:
        out["stats"] = call_kernel("learnings_stats", timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"learnings_stats failed: {exc}") from exc
    try:
        recall = call_kernel("recall_learnings", timeout=25.0, query=query, limit=limit)
        out["feed"] = recall.get("results", []) if isinstance(recall, dict) else []
        out["count"] = recall.get("count") if isinstance(recall, dict) else None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"recall_learnings failed: {exc}") from exc
    return out
