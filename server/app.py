"""FastAPI application for the interfaze-agent Sales Agent MVP."""
from __future__ import annotations

import json
import logging
import mimetypes
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
from .document_artifacts import DocumentArtifactRepository
from .document_processing_service import DocumentProcessingService
from .observability import configure_logging, install as install_observability, log
from .outreach_service import OutreachService
from .postgres import create_database
from .storage import create_storage
from .agent_service import AgentRunService, StubRunExecutor
from .chat_bridge import ChatBridge
from .scheduler import DailyDigestScheduler
from .lead_research import LeadResearchService
from .routes import admin, admin_documents, agent_runs, auth, chat, company, integrations, knowledge, onboarding, operations, outreach, oauth, research_campaigns, sales_intelligence, unsubscribe


def create_app(settings: Settings | None = None, db: Database | None = None,
               run_executor=None, chat_agent_factory=None) -> FastAPI:
    settings = settings or Settings.load()
    configure_logging()
    database = db or create_database(settings)
    _warn_on_incomplete_config(settings)
    service = AuthService(database, settings)
    service.bootstrap_admin()
    run_service = AgentRunService(database, run_executor)
    document_artifacts = DocumentArtifactRepository(database, settings.upload_dir)
    document_processing = DocumentProcessingService(
        document_artifacts,
        workers=settings.document_workers,
        timeout_seconds=settings.document_processing_timeout_seconds,
        max_output_bytes=settings.document_output_max_bytes,
    )
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
        _resume_document_processing(application)
        if settings.scheduler_enabled:
            digest_scheduler.start()
        yield
        digest_scheduler.shutdown()
        if chat_service:
            chat_service.shutdown()
        run_service.pool.shutdown(wait=False, cancel_futures=True)
        # Before the database closes: an in-flight attempt still writes rows.
        document_processing.shutdown()
        application.state.lead_research.shutdown()
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
    # No live route reads this any more — uploads go straight into
    # document_artifacts. It survives solely as the resolver the one-time
    # legacy backfill uses to pull pre-artifact documents out of Supabase
    # Storage, after which nothing depends on a signed URL again.
    app.state.storage = create_storage(settings)
    app.state.document_artifacts = document_artifacts
    app.state.document_processing = document_processing
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
    app.include_router(admin_documents.router, prefix=api_prefix)
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


def _resume_document_processing(app: FastAPI) -> None:
    """Give legacy and interrupted documents their processed form.

    Runs on every boot and is cheap when there is nothing to do: both queries
    are `NOT EXISTS` / `IS NULL` filters that match only unfinished rows, so a
    fully-migrated database does no work and needs no separate marker. Failures
    are logged, never fatal — the API must still come up.
    """
    try:
        artifacts = app.state.document_artifacts
        summary = artifacts.backfill_existing_documents(
            resolver=getattr(app.state.storage, "resolve", None)
        )
        if summary["backfilled"] or summary["missing"]:
            log(f"document backfill: {summary}")

        pending = artifacts.documents_awaiting_processing()
        for company_id, document_id in pending:
            app.state.document_processing.submit(company_id, document_id)
        if pending:
            log(f"queued {len(pending)} document(s) for processing at startup")
    except Exception as exc:  # noqa: BLE001
        log(f"document backfill skipped: {type(exc).__name__}: {exc}", logging.ERROR)


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

    # StaticFiles resolves content types through the stdlib mimetypes registry,
    # which has no entry for woff2 on Windows — fonts then go out as
    # text/plain and the browser drops the <link rel="preload"> on type
    # mismatch. Register explicitly so behaviour matches across platforms.
    mimetypes.add_type("font/woff2", ".woff2")
    mimetypes.add_type("font/woff", ".woff")

    class RevalidatingStatic(StaticFiles):
        """Serve the SPA with must-revalidate on code, long cache on fonts.

        The dashboard is unbundled ES modules with stable filenames, so there
        is no content hash to bust a cache with. Browsers cache modules
        aggressively and heuristically, which means an edited .js can keep
        serving from disk cache through several reloads: the page renders a
        mix of old and new code and the change looks like it silently failed.

        `no-cache` does not mean "do not cache" — it means "revalidate before
        reuse". StaticFiles already emits ETag and Last-Modified, so this
        costs a 304 per file and guarantees freshness. Fonts are exempt: their
        names never change meaning and they are the largest payload here.
        """

        def file_response(self, *args, **kwargs):  # type: ignore[override]
            response = super().file_response(*args, **kwargs)
            path = str(getattr(response, "path", ""))
            if path.endswith((".woff2", ".woff")):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/", RevalidatingStatic(directory=str(webui_dir)), name="webui")


def app_factory() -> FastAPI:
    """Uvicorn factory entry point without import-time filesystem writes."""
    return create_app()
