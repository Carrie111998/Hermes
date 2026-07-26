"""FastAPI application for the interfaze-agent Sales Agent MVP."""
from __future__ import annotations

import json
import logging
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
from .observability import configure_logging, install as install_observability, log
from .outreach_service import OutreachService
from .postgres import create_database
from .storage import create_storage
from .agent_service import AgentRunService, StubRunExecutor
from .chat_bridge import ChatBridge
from .scheduler import DailyDigestScheduler
from .lead_research import LeadResearchService
from .routes import admin, agent_runs, auth, chat, company, integrations, knowledge, onboarding, operations, outreach, oauth, research_campaigns, sales_intelligence, unsubscribe


def create_app(settings: Settings | None = None, db: Database | None = None,
               run_executor=None, chat_agent_factory=None) -> FastAPI:
    settings = settings or Settings.load()
    configure_logging()
    database = db or create_database(settings)
    _warn_on_incomplete_config(settings)
    service = AuthService(database, settings)
    service.bootstrap_admin()
    run_service = AgentRunService(database, run_executor)
    chat_service = (ChatBridge(database, settings, run_service, agent_factory=chat_agent_factory)
                    if settings.chat_enabled else None)
    # Always constructed so tests and the CLI can drive tick() directly; only
    # the background thread is gated on the setting.
    digest_scheduler = DailyDigestScheduler(
        database,
        plan_hour=settings.digest_plan_hour,
        report_hour=settings.digest_report_hour,
        interval_seconds=settings.scheduler_interval_seconds,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if settings.scheduler_enabled:
            digest_scheduler.start()
        yield
        digest_scheduler.shutdown()
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
    app.state.scheduler = digest_scheduler
    app.state.cipher = CredentialCipher(settings.credential_key)
    app.state.outreach = OutreachService(
        database, app.state.cipher,
        public_base_url=settings.public_base_url,
        credential_key=settings.credential_key,
    )
    app.state.storage = create_storage(settings)
    app.state.lead_research = LeadResearchService(database)
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

    install_observability(app, database)

    api_prefix = "/api/v1"
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(admin.router, prefix=api_prefix)
    app.include_router(company.router, prefix=api_prefix)
    app.include_router(onboarding.router, prefix=api_prefix)
    app.include_router(agent_runs.router, prefix=api_prefix)
    app.include_router(knowledge.router, prefix=api_prefix)
    # research_campaigns must precede sales_intelligence: its static /research/*
    # collection routes (configuration, sectors, model-profiles, ...) would
    # otherwise be shadowed by sales_intelligence's catch-all /research/{id}.
    app.include_router(research_campaigns.router, prefix=api_prefix)
    app.include_router(sales_intelligence.router, prefix=api_prefix)
    app.include_router(integrations.router, prefix=api_prefix)
    app.include_router(outreach.router, prefix=api_prefix)
    app.include_router(operations.router, prefix=api_prefix)
    app.include_router(oauth.router, prefix=api_prefix)
    app.include_router(unsubscribe.router, prefix=api_prefix)
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


def _warn_on_incomplete_config(settings: Settings) -> None:
    """Surface deployment gaps at boot instead of at first customer action.

    These are warnings, not hard failures: a developer running the API locally
    to work on the dashboard should not be blocked by production-only config.
    The individual code paths still fail closed when the value is actually
    needed (crypto.CredentialCipher, compliance.sign_token).
    """
    if not settings.credential_key:
        log("INTERFAZE_CREDENTIAL_KEY is unset: integrations cannot be connected "
            "and outbound email cannot be sent (opt-out links require it)",
            logging.WARNING)
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        log("INTERFAZE_BOOTSTRAP_ADMIN_EMAIL/_PASSWORD unset: no admin account "
            "will be created, so nobody can sign in", logging.WARNING)
    if settings.public_base_url.startswith("http://localhost"):
        log("INTERFAZE_PUBLIC_BASE_URL is still localhost: unsubscribe links in "
            "outbound email will not resolve for recipients", logging.WARNING)
    if settings.auth_mode == "supabase" and not (settings.supabase_url and settings.supabase_anon_key):
        log("auth_mode is supabase but SUPABASE_URL/SUPABASE_ANON_KEY are unset: "
            "all authentication will return 503", logging.ERROR)
    if shutil.which("hermes") is None:
        log("hermes CLI is not on PATH: every agent run (lead discovery, "
            "research, outreach generation) will fail", logging.ERROR)


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
