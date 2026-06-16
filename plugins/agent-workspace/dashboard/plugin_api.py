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
kernel's /mcp directly.

Kernel client: this plugin carries its own stdlib-only MCP Streamable HTTP
client (call_kernel.py, sibling file) — the same self-contained pattern every
Hermes plugin uses. We deliberately do NOT import it from the fridai-memory
plugin: that copy diverges per profile (some deployments still ship the stale
pre-#278 /call client, which 404s against the current kernel), so depending on
it is fragile. Self-containing the client guarantees the tab works regardless of
which fridai-memory version a given profile happens to have.
TODO(promote): if a third consumer appears, lift call_kernel.py into one shared
Hermes FRIDAI module and have all plugins import it.
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
    """Load this plugin's own MCP kernel client (sibling call_kernel.py).
    Cached after first load. Loaded by path because the dashboard imports
    plugin_api.py via spec_from_file_location, so the plugin dir isn't on
    sys.path for a plain `import call_kernel`."""
    global _call_kernel
    if _call_kernel is not None:
        return _call_kernel
    path = Path(__file__).resolve().parent / "call_kernel.py"
    if not path.is_file():
        raise RuntimeError(f"agent-workspace: kernel client missing at {path}")
    spec = importlib.util.spec_from_file_location("agent_workspace_call_kernel", path)
    if not (spec and spec.loader):
        raise RuntimeError("agent-workspace: cannot load call_kernel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _call_kernel = mod.call_kernel
    return _call_kernel


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
