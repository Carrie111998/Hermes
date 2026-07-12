"""FastAPI application for the interfaze-agent Sales Agent MVP."""
from __future__ import annotations

import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .auth import AuthService
from .config import Settings
from .crypto import CredentialCipher
from .db import Database
from .outreach_service import OutreachService
from .postgres import create_database
from .storage import create_storage
from .agent_service import AgentRunService, StubRunExecutor
from .chat_bridge import ChatBridge
from .routes import admin, agent_runs, auth, chat, company, integrations, knowledge, onboarding, operations, outreach, sales_intelligence


def create_app(settings: Settings | None = None, db: Database | None = None,
               run_executor=None, chat_agent_factory=None) -> FastAPI:
    settings = settings or Settings.load()
    database = db or create_database(settings)
    service = AuthService(database, settings)
    service.bootstrap_admin()
    run_service = AgentRunService(database, run_executor)
    chat_service = (ChatBridge(database, settings, run_service, agent_factory=chat_agent_factory)
                    if settings.chat_enabled else None)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        if chat_service:
            chat_service.shutdown()
        run_service.pool.shutdown(wait=False, cancel_futures=True)
        close = getattr(database, "close", None)
        if close:
            close()

    app = FastAPI(
        title="interfaze-agent API",
        version="1.0.0",
        description="Tenant-safe Sales Agent backend consumed by the separate dashboard.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.auth = service
    app.state.runs = run_service
    app.state.chat = chat_service
    app.state.cipher = CredentialCipher(settings.credential_key)
    app.state.outreach = OutreachService(database, app.state.cipher)
    app.state.storage = create_storage(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'",
        )
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    api_prefix = "/api/v1"
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(admin.router, prefix=api_prefix)
    app.include_router(company.router, prefix=api_prefix)
    app.include_router(onboarding.router, prefix=api_prefix)
    app.include_router(agent_runs.router, prefix=api_prefix)
    app.include_router(knowledge.router, prefix=api_prefix)
    app.include_router(sales_intelligence.router, prefix=api_prefix)
    app.include_router(integrations.router, prefix=api_prefix)
    app.include_router(outreach.router, prefix=api_prefix)
    app.include_router(operations.router, prefix=api_prefix)
    if chat_service:
        app.include_router(chat.router)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "interfaze-agent", "api_version": "v1",
                "chat_enabled": bool(chat_service),
                "agent_runs_enabled": shutil.which("hermes") is not None}

    webui_dir = Path(__file__).resolve().parent / "webui"
    if settings.webui_enabled and webui_dir.is_dir():
        _mount_webui(app, webui_dir, settings)

    return app


def _mount_webui(app: FastAPI, webui_dir: Path, settings: Settings) -> None:
    """Serve the dashboard SPA (server/webui/) from the API process.

    Registered after every API route so /api/v1/*, /health, and /docs win;
    the StaticFiles catch-all only sees paths nothing else claimed. The SPA
    uses a hash router, so the single "/" HTML route is the only entry point.
    """
    index_path = webui_dir / "index.html"

    @app.get("/", include_in_schema=False)
    def webui_index() -> HTMLResponse:
        html = index_path.read_text(encoding="utf-8")
        html = html.replace("__MAX_UPLOAD_BYTES__", str(max(0, settings.max_upload_bytes)))
        # Auth is Bearer-only on this backend; an empty CSRF token disables
        # the page's inline fetch patch without editing the copied bundle.
        html = html.replace("__CSRF_TOKEN_JSON__", json.dumps(""))
        html = html.replace("__CHAT_ENABLED__", json.dumps(settings.chat_enabled))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount("/", StaticFiles(directory=str(webui_dir)), name="webui")


def app_factory() -> FastAPI:
    """Uvicorn factory entry point without import-time filesystem writes."""
    return create_app()
