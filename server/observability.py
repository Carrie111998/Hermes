"""Logging, request correlation, and readiness.

The product had no log statements at all, which meant every production failure
was invisible: unhandled exceptions became a bare uvicorn traceback on stdout
and integration failures were swallowed. This module is deliberately stdlib-only
— no structlog, no OpenTelemetry — because one formatter and one middleware
covers what an MVP actually needs to debug an incident.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar


request_id: ContextVar[str] = ContextVar("request_id", default="-")

logger = logging.getLogger("interfaze")

# Paths that would otherwise dominate the log with no diagnostic value.
_QUIET_PATHS = frozenset({"/health", "/ready", "/favicon.ico"})


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so any log aggregator can parse it unaided."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id.get(),
        }
        for key, value in getattr(record, "context", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Idempotent root-logger setup. Honors INTERFAZE_LOG_LEVEL and _LOG_FORMAT."""
    level = os.environ.get("INTERFAZE_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("INTERFAZE_LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root = logging.getLogger()
    # Replace our own handler rather than stacking one per create_app() call,
    # which would otherwise duplicate every line once per test client.
    for existing in list(root.handlers):
        if getattr(existing, "_interfaze", False):
            root.removeHandler(existing)
    handler._interfaze = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    # httpx logs every outbound provider call at INFO, which buries our own
    # access log. Their warnings still surface.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(message: str, level: int = logging.INFO, **context) -> None:
    logger.log(level, message, extra={"context": context})


def install(app, database) -> None:
    """Attach request correlation, an access log, and a catch-all handler."""
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def correlate_and_log(request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        # Bound and sanitized: this value is echoed back and written to logs, so
        # an attacker-supplied header must not inject newlines or unbounded text.
        rid = "".join(ch for ch in incoming if ch.isalnum() or ch in "-_")[:64]
        # Deliberately not reset afterwards. Starlette runs each request in its
        # own context, so there is nothing to leak into — and the catch-all
        # exception handler runs OUTSIDE this middleware (ServerErrorMiddleware
        # is outermost), so resetting here would blank the id on exactly the
        # 500 responses that need it most.
        request_id.set(rid or uuid.uuid4().hex[:16])
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Only the access line here; the handler below logs the traceback.
            log("request failed", logging.ERROR, method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 1))
            raise
        response.headers["X-Request-ID"] = request_id.get()
        if request.url.path not in _QUIET_PATHS:
            log("request", logging.INFO, method=request.method, path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1))
        return response

    @app.exception_handler(Exception)
    async def unhandled(request, exc):
        """Never leak an internal error string to the client."""
        logger.exception("unhandled exception", extra={"context": {
            "path": request.url.path, "method": request.method}})
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id.get()},
            headers={"X-Request-ID": request_id.get()},
        )

    @app.get("/ready")
    def ready():
        """Readiness, unlike /health, must actually touch the database.

        /health stays green while Postgres is down, which makes it useless as a
        rollout gate.
        """
        try:
            database.one("SELECT id FROM companies LIMIT 1")
        except Exception as exc:
            log("readiness probe failed", logging.ERROR, error=str(exc))
            return JSONResponse(status_code=503,
                                content={"status": "unavailable", "database": "error"})
        return {"status": "ready", "database": "ok"}
