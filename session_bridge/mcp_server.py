"""Authenticated loopback MCP surface for the unified session catalog."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
from typing import Any, cast

from hermes_constants import get_hermes_home

from .catalog import UnifiedCatalog
from .config import BridgeConfig
from .coordinator import ContinueRequest, ContinueResult
from .mirror import MirrorPolicy, enqueue_mirror_job
from .models import Provider
from .store import SessionBridgeStore


EXPECTED_TOOLS = {
    "session_search",
    "session_get",
    "session_continue",
    "session_mirror",
    "session_status",
}
_TOKEN_ENV = "HERMES_SESSION_BRIDGE_TOKEN"
_MIN_TOKEN_BYTES = 32
_MAX_CONTEXT_BUDGET = 100_000
_DEFAULT_CONTEXT_BUDGET = 24_000
_SENSITIVE_STATUS_KEYS = frozenset(
    {
        "authorization",
        "credential",
        "error_detail",
        "native_path",
        "password",
        "payload",
        "secret",
        "source_cursor",
        "source_hash",
        "token",
    }
)


def resolve_bearer_token(
    *,
    environ: Mapping[str, str] | None = None,
    token_file: Path | None = None,
) -> bytes:
    """Resolve the bearer secret from environment or a restricted file."""

    environment = os.environ if environ is None else environ
    raw = environment.get(_TOKEN_ENV)
    if raw is None:
        path = (
            token_file
            if token_file is not None
            else get_hermes_home() / "session-bridge" / "token"
        )
        if not path.exists():
            raise RuntimeError("session bridge bearer token is missing")
        _require_restricted_token_file(path)
        raw = path.read_text(encoding="utf-8")
    return _validated_token(raw)


def create_app(
    *,
    catalog: UnifiedCatalog,
    coordinator: object,
    store: SessionBridgeStore,
    config: BridgeConfig,
    token: str | bytes | None = None,
):
    """Create the parent Starlette app with an exact ``/mcp`` endpoint."""

    if not isinstance(catalog, UnifiedCatalog):
        raise TypeError("catalog must be a UnifiedCatalog")
    if not isinstance(store, SessionBridgeStore):
        raise TypeError("store must be a SessionBridgeStore")
    if not isinstance(config, BridgeConfig):
        raise TypeError("config must be a BridgeConfig")
    if catalog.store is not store:
        raise ValueError("catalog and MCP service must share one bridge store")
    if not _is_loopback_host(config.service.host):
        raise ValueError("session bridge MCP must bind to a loopback host")
    bearer_token = (
        resolve_bearer_token()
        if token is None
        else _validated_token(token)
    )

    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route
    except ImportError as exc:  # pragma: no cover - guarded by the mcp extra
        raise RuntimeError("session bridge MCP dependencies are not installed") from exc

    authority = _host_authority(config.service.host, config.service.port)
    mcp = FastMCP(
        "hermes-session-bridge",
        instructions=(
            "Search, inspect, mirror, and continue Claude Code and Codex sessions "
            "through the authoritative Hermes catalog."
        ),
        host=config.service.host,
        port=config.service.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=False,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[authority],
            allowed_origins=[f"http://{authority}", f"https://{authority}"],
        ),
    )

    @mcp.tool()
    async def session_search(
        query: str = "",
        session_id: str | None = None,
        around_message_id: int | None = None,
        window: int = 5,
        limit: int = 10,
        provider: str | None = None,
        mirror_state: str | None = None,
        relation: str | None = None,
        cwd: str | None = None,
        repo: str | None = None,
        before: float | None = None,
        after: float | None = None,
    ) -> dict[str, Any]:
        """Search or browse sessions, read one, or scroll around a message."""

        return await asyncio.to_thread(
            catalog.search,
            query=query,
            session_id=session_id,
            around_message_id=around_message_id,
            window=window,
            limit=limit,
            provider=provider,
            mirror_state=mirror_state,
            relation=relation,
            cwd=cwd,
            repo=repo,
            before=before,
            after=after,
        )

    @mcp.tool()
    async def session_get(session_id: str, window: int = 50) -> dict[str, Any]:
        """Read a bounded transcript and its unified bridge metadata."""

        return await asyncio.to_thread(catalog.get, session_id, window=window)

    @mcp.tool()
    async def session_continue(
        session_id: str | None = None,
        bridge_id: str | None = None,
        target_provider: str | None = None,
        context_budget_chars: int = _DEFAULT_CONTEXT_BUDGET,
    ) -> dict[str, Any]:
        """Freeze or reuse one continuation pack and hydrate the native mirror."""

        budget = _clamp_int(
            context_budget_chars,
            default=_DEFAULT_CONTEXT_BUDGET,
            minimum=1,
            maximum=_MAX_CONTEXT_BUDGET,
        )
        resolved = await asyncio.to_thread(
            catalog.resolve_continuation,
            session_id=session_id,
            bridge_id=bridge_id,
            target_provider=target_provider,
        )
        continue_method = getattr(coordinator, "continue_session", None)
        if not callable(continue_method):
            raise RuntimeError("session bridge coordinator cannot continue sessions")
        result = await continue_method(
            ContinueRequest(
                session_id=resolved["source_session_id"],
                bridge_id=resolved["bridge_id"],
                target_provider=Provider(resolved["target_provider"]),
                context_budget_chars=budget,
            )
        )
        if not isinstance(result, ContinueResult):
            raise RuntimeError("session continuation returned an invalid result")
        return {
            "session_id": result.pack.source_session_id,
            "target_session_id": result.pack.target_session_id,
            "target_provider": resolved["target_provider"],
            "bridge_id": result.pack.bridge_id,
            "pack_id": result.pack.id,
            "payload": result.pack.payload,
            "budget_chars": result.pack.budget_chars,
            "immutable_at": result.pack.immutable_at,
            "relation": result.link.relation.value,
            "warnings": list(result.warnings),
        }

    @mcp.tool()
    async def session_mirror(
        session_id: str,
        target_provider: str,
        dry_run: bool = True,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        """Preview or enqueue a manually authorized native mirror creation."""

        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        if type(retry_failed) is not bool:
            raise ValueError("retry_failed must be a boolean")
        preview = await asyncio.to_thread(
            catalog.mirror_preview,
            session_id,
            target_provider,
        )
        response = {**preview, "dry_run": dry_run}
        if dry_run:
            return response
        if not preview["would_enqueue"] and not (
            retry_failed and preview["reason"] == "failed"
        ):
            return response
        job = await asyncio.to_thread(
            enqueue_mirror_job,
            store,
            preview["session_id"],
            Provider(preview["target_provider"]),
            policy=_mirror_policy(config),
            manual_authorized=True,
            retry_failed=retry_failed,
            require_unmapped=True,
        )
        return {
            "session_id": job["source_session_id"],
            "target_provider": job["target_provider"],
            "job_id": job["id"],
            "state": job["state"],
            "attempts": int(job["attempts"]),
            "authority": "manual",
            "dry_run": False,
        }

    @mcp.tool()
    async def session_status() -> dict[str, Any]:
        """Return sanitized indexing, queue, and catalog health."""

        health_method = getattr(coordinator, "health", None)
        health = health_method() if callable(health_method) else {"running": False}
        catalog_status = await asyncio.to_thread(catalog.status)
        return _sanitize_status(
            {
                "health": health,
                "catalog": catalog_status,
            }
        )

    actual_tools = set(mcp._tool_manager._tools)
    if actual_tools != EXPECTED_TOOLS:
        raise RuntimeError("session bridge MCP tool registration is incomplete")

    mcp_app = mcp.streamable_http_app()

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        start = getattr(coordinator, "start", None)
        stop = getattr(coordinator, "stop", None)
        if not callable(start) or not callable(stop):
            raise RuntimeError("session bridge coordinator has no lifecycle")
        await start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await stop()

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(cast(Any, _BearerMcpAuth), token=bearer_token)],
        lifespan=lifespan,
    )
    app.state.mcp = mcp
    return app


class _BearerMcpAuth:
    """Pure ASGI bearer guard that preserves streaming cancellation behavior."""

    def __init__(self, app: Any, *, token: bytes) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not _is_mcp_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        raw_headers = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"authorization"
        ]
        authorized = False
        if len(raw_headers) == 1:
            try:
                header = raw_headers[0].decode("ascii")
            except UnicodeDecodeError:
                header = ""
            scheme, separator, credential = header.partition(" ")
            if separator and scheme.lower() == "bearer" and credential:
                try:
                    candidate = credential.encode("utf-8")
                except UnicodeEncodeError:
                    candidate = b""
                authorized = secrets.compare_digest(candidate, self.token)
        if authorized:
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def _validated_token(value: str | bytes) -> bytes:
    if isinstance(value, str):
        normalized = value.strip()
        if any(character.isspace() for character in normalized):
            raise ValueError("session bridge bearer token must not contain whitespace")
        encoded = normalized.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = value.strip()
        if any(byte in b" \t\r\n\v\f" for byte in encoded):
            raise ValueError("session bridge bearer token must not contain whitespace")
    else:
        raise TypeError("session bridge bearer token must be text or bytes")
    if len(encoded) < _MIN_TOKEN_BYTES:
        raise ValueError("session bridge bearer token must be at least 32 bytes")
    return encoded


def _require_restricted_token_file(path: Path) -> None:
    if path.is_symlink():
        raise PermissionError("session bridge token file must not be a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise PermissionError("session bridge token file must be a regular file")
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("session bridge token file permissions are too broad")
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and info.st_uid != getuid():
            raise PermissionError("session bridge token file has the wrong owner")
        return
    script = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:HERMES_SESSION_BRIDGE_ACL_PATH
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
$rules = @($acl.GetAccessRules(
    $true,
    $true,
    [System.Security.Principal.SecurityIdentifier]
) | ForEach-Object {
    [pscustomobject]@{
        identity = $_.IdentityReference.Value
        type = $_.AccessControlType.ToString()
    }
})
[pscustomobject]@{
    current_sid = $current
    owner_sid = $owner
    rules = $rules
} | ConvertTo-Json -Compress -Depth 5
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=True,
            capture_output=True,
            env={**os.environ, "HERMES_SESSION_BRIDGE_ACL_PATH": str(path)},
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermissionError("session bridge token file ACL could not be verified") from exc
    try:
        snapshot = json.loads(result.stdout)
        _validate_windows_token_acl(
            current_sid=snapshot["current_sid"],
            owner_sid=snapshot["owner_sid"],
            rules=snapshot["rules"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PermissionError("session bridge token file ACL could not be verified") from exc


def _validate_windows_token_acl(
    *,
    current_sid: str,
    owner_sid: str,
    rules: object,
) -> None:
    if not isinstance(current_sid, str) or not current_sid.strip():
        raise PermissionError("session bridge token file ACL has no current user")
    normalized_current = current_sid.strip().casefold()
    if not isinstance(owner_sid, str) or owner_sid.strip().casefold() != normalized_current:
        raise PermissionError("session bridge token file has the wrong owner")
    if not isinstance(rules, list):
        raise PermissionError("session bridge token file ACL is invalid")
    system_sid = "s-1-5-18"
    allowed = {normalized_current, system_sid}
    seen_allow: set[str] = set()
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise PermissionError("session bridge token file ACL is invalid")
        normalized_rule = cast(Mapping[str, Any], rule)
        identity = normalized_rule.get("identity")
        access_type = normalized_rule.get("type")
        if not isinstance(identity, str) or not isinstance(access_type, str):
            raise PermissionError("session bridge token file ACL is invalid")
        if access_type.casefold() != "allow":
            continue
        normalized_identity = identity.strip().casefold()
        if normalized_identity not in allowed:
            raise PermissionError(
                "session bridge token file ACL grants an unauthorized principal"
            )
        seen_allow.add(normalized_identity)
    if seen_allow != allowed:
        raise PermissionError(
            "session bridge token file ACL must allow only the current user and SYSTEM"
        )


def _mirror_policy(config: BridgeConfig) -> MirrorPolicy:
    mirrors = config.mirrors
    return MirrorPolicy(
        automatic_creation=mirrors.automatic_creation,
        backfill_days=mirrors.backfill_days,
        creates_per_minute=mirrors.creates_per_minute,
        max_attempts=mirrors.max_attempts,
        stop_after_attempts=mirrors.stop_after_attempts,
        stop_error_rate=mirrors.stop_error_rate,
    )


def _sanitize_status(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_status(item)
            for key, item in value.items()
            if str(key).casefold() not in _SENSITIVE_STATUS_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_status(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _sanitize_status(asdict(value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_authority(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        authority_host = host
    else:
        authority_host = f"[{host}]" if address.version == 6 else host
    return f"{authority_host}:{port}"


def _clamp_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        value = default
    return max(minimum, min(int(value), maximum))


__all__ = ["EXPECTED_TOOLS", "create_app", "resolve_bearer_token"]
