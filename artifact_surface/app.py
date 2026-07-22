"""FastAPI app for Hermes Canvas — the artifact viewer + read-only data API.

Routes:
    GET /                  viewer index (artifact cards, client-side filter)
    GET /a/{artifact_id}   wrapper page embedding the artifact in a sandboxed iframe
    GET /raw/{artifact_id} the raw artifact HTML (iframe src), served with a tight CSP
    GET /api/health        JSON liveness probe (laptop-monitor)
    GET /api/{events,cron,jobflow,financier,boot}   read-only data (see data_api)
    /static/*              hermes-live.js + assets

Loopback-only; everything is read-only. Mirrors the control_center pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import store
from .data_api import router as api_router

THIS_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(THIS_DIR / "templates"))

ARTIFACT_CSP = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"

app = FastAPI(title="Hermes Canvas", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(THIS_DIR / "static")), name="static")
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html",
                                      {"artifacts": store.list_artifacts()})


@app.get("/raw/{artifact_id}", response_class=HTMLResponse)
async def raw_artifact(artifact_id: str):
    html = store.get_artifact_html(artifact_id)
    if html is None:
        return HTMLResponse("artifact not found", status_code=404)
    return HTMLResponse(html, headers={"Content-Security-Policy": ARTIFACT_CSP})


@app.get("/a/{artifact_id}", response_class=HTMLResponse)
async def view_artifact(artifact_id: str):
    if store.get_artifact_html(artifact_id) is None:
        return HTMLResponse("artifact not found", status_code=404)
    body = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>{artifact_id}</title>'
        f'<style>html,body{{margin:0;height:100%}}iframe{{border:0;width:100%;height:100vh}}</style>'
        f'</head><body>'
        f'<iframe src="/raw/{artifact_id}" sandbox="allow-scripts allow-same-origin"></iframe>'
        f'</body></html>'
    )
    return HTMLResponse(body)


@app.get("/api/health")
async def api_health():
    return JSONResponse({
        "ok": True,
        "service": "hermes-canvas",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "artifact_count": len(store.list_artifacts()),
    })
