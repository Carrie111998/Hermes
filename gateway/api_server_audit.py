"""Structured audit logging for API-server requests and decisions."""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Mapping

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - aiohttp is present in gateway tests
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.session_acl import scope_fingerprint


REQUEST_ID_HEADER = "X-Hermes-Request-Id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
logger = logging.getLogger("gateway.api_server.audit")


def _clean(value: Any, *, max_len: int = 240) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len]


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name, "")
    return _clean(value, max_len=256)


def ensure_request_id(request: "web.Request") -> str:
    existing = _header(request.headers, REQUEST_ID_HEADER) or _header(request.headers, "X-Request-Id")
    request_id = existing if _REQUEST_ID_RE.match(existing) else f"req_{uuid.uuid4().hex}"
    try:
        request["hermes_request_id"] = request_id
    except Exception:
        _ = request_id
    return request_id


def request_id_for(request: "web.Request") -> str:
    try:
        request_id = request.get("hermes_request_id")
    except Exception:
        request_id = None
    return _clean(request_id, max_len=96) or ensure_request_id(request)


def request_id_headers(request: "web.Request") -> dict[str, str]:
    return {REQUEST_ID_HEADER: request_id_for(request)}


def _request_fields(request: "web.Request") -> dict[str, str]:
    peer_ip = ""
    try:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if isinstance(peer, (tuple, list)) and peer:
            peer_ip = str(peer[0])
    except Exception:
        peer_ip = ""
    fields = {
        "request_id": request_id_for(request),
        "method": _clean(getattr(request, "method", ""), max_len=16),
        "path": _clean(getattr(request, "path_qs", ""), max_len=500),
        "remote": _clean(getattr(request, "remote", "") or peer_ip),
        "peer_ip": _clean(peer_ip),
        "user_agent": _clean(request.headers.get("User-Agent", ""), max_len=300),
    }
    try:
        run_id = request.get("hermes_run_id")
    except Exception:
        run_id = None
    run_id = _clean(run_id, max_len=128)
    if run_id:
        fields["run_id"] = run_id
    return fields


def _scope_fields(scope: Mapping[str, Any] | None) -> dict[str, str]:
    if not scope:
        return {"scope": "legacy"}
    fields = {
        "scope": scope_fingerprint(scope),
        "tenant_id": _clean(scope.get("tenant_id")),
        "workspace_id": _clean(scope.get("workspace_id")),
        "project_id": _clean(scope.get("project_id")),
        "user_id": _clean(scope.get("user_id")),
    }
    roles = scope.get("roles")
    if roles:
        fields["roles"] = ",".join(_clean(role, max_len=64) for role in roles)
    sandbox_id = scope.get("sandbox_id")
    if sandbox_id:
        fields["sandbox_id"] = _clean(sandbox_id)
    return fields


def log_api_decision(
    request: "web.Request",
    *,
    action: str,
    result: str,
    status: int | None = None,
    principal_scope: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    reason: str | None = None,
    started_at: float | None = None,
    level: int | None = None,
) -> None:
    fields: dict[str, Any] = {
        **_request_fields(request),
        **_scope_fields(principal_scope),
        "action": _clean(action, max_len=80),
        "result": _clean(result, max_len=40),
    }
    if status is not None:
        fields["status"] = int(status)
    if session_id:
        fields["session_id"] = _clean(session_id)
    if reason:
        fields["reason"] = _clean(reason, max_len=300)
    if started_at is not None:
        fields["duration_ms"] = round((time.monotonic() - started_at) * 1000, 2)

    if level is None:
        level = logging.WARNING if result in {"denied", "failed", "error"} else logging.INFO
    logger.log(level, "api_server_decision %s", fields)


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def request_audit_middleware(request, handler):
        started_at = time.monotonic()
        ensure_request_id(request)
        log_api_decision(
            request,
            action="api.request",
            result="started",
            started_at=started_at,
            level=logging.INFO,
        )
        try:
            response = await handler(request)
        except Exception:
            log_api_decision(
                request,
                action="api.request",
                result="failed",
                status=500,
                reason="unhandled_exception",
                started_at=started_at,
                level=logging.ERROR,
            )
            raise

        try:
            if not response.prepared:
                response.headers[REQUEST_ID_HEADER] = request_id_for(request)
        except Exception:
            _ = response
        log_api_decision(
            request,
            action="api.request",
            result="completed" if getattr(response, "status", 500) < 400 else "denied",
            status=getattr(response, "status", None),
            started_at=started_at,
            level=logging.INFO if getattr(response, "status", 500) < 400 else logging.WARNING,
        )
        return response
else:
    request_audit_middleware = None  # type: ignore[assignment]
