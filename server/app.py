"""FastAPI application for the interfaze-agent Sales Agent MVP."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import AuthService
from .config import Settings
from .crypto import CredentialCipher
from .db import Database
from .outreach_service import OutreachService
from .postgres import create_database
from .storage import create_storage
from .agent_service import AgentRunService, StubRunExecutor
from .routes import admin, agent_runs, auth, company, integrations, knowledge, onboarding, operations, outreach, sales_intelligence


def create_app(settings: Settings | None = None, db: Database | None = None,
               run_executor=None) -> FastAPI:
    settings = settings or Settings.load()
    database = db or create_database(settings)
    service = AuthService(database, settings)
    service.bootstrap_admin()
    run_service = AgentRunService(database, run_executor)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
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

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "interfaze-agent", "api_version": "v1"}

    return app


def app_factory() -> FastAPI:
    """Uvicorn factory entry point without import-time filesystem writes."""
    return create_app()
