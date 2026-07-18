"""Authenticated loopback MCP surface for the unified session catalog."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time
from typing import Any, cast

from hermes_constants import get_hermes_home

from .catalog import UnifiedCatalog
from .claude_visibility_codes import (
    CLAUDE_VISIBILITY_FATAL_CODES,
    CLAUDE_VISIBILITY_RETRY_CODES,
)
from .config import BridgeConfig
from .coordinator import ContinueRequest, ContinueResult
from .mirror import MirrorPolicy, enqueue_mirror_job
from .models import (
    BridgeMarkerPayload,
    MirrorJobState,
    Provider,
    SidebarJobState,
    encode_bridge_marker,
)
from .sidebar import (
    build_registration_prompt,
)
from .store import (
    SIDEBAR_FATAL_ERRORS,
    SIDEBAR_RETRYABLE_ERRORS,
    SessionBridgeStore,
    redact_codex_thread_id,
)


EXPECTED_TOOLS = {
    "session_search",
    "session_get",
    "session_continue",
    "session_mirror",
    "session_status",
    "session_claude_visibility_status",
    "session_sidebar_pending",
    "session_sidebar_bind",
    "session_sidebar_commit",
    "session_sidebar_fail",
}
_TOKEN_ENV = "HERMES_SESSION_BRIDGE_TOKEN"
_MIN_TOKEN_BYTES = 32
_MIN_MARKER_KEY_BYTES = 32
_MAX_MARKER_KEY_BYTES = 4096
_WINDOWS_ACL_TIMEOUT_SECONDS = 15
_MAX_CONTEXT_BUDGET = 100_000
_DEFAULT_CONTEXT_BUDGET = 24_000
_FIXED_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


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


def resolve_marker_key(*, marker_key_file: Path | None = None) -> bytes:
    """Load the origin-marker HMAC key from its independent restricted file."""

    path = Path(
        marker_key_file
        if marker_key_file is not None
        else get_hermes_home() / "session-bridge" / "marker-key"
    ).expanduser()
    try:
        key = _read_restricted_marker_key(path)
    except FileNotFoundError:
        raise RuntimeError("session bridge marker key file is missing") from None
    if len(key) < _MIN_MARKER_KEY_BYTES:
        raise ValueError("session bridge marker key must be at least 32 bytes")
    return key


def _read_restricted_marker_key(path: Path) -> bytes:
    descriptor = -1
    try:
        before = os.lstat(path)
        if _secret_metadata_is_redirect(before) or not stat.S_ISREG(before.st_mode):
            raise PermissionError(
                "session bridge marker key file must be a non-redirect regular file"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not _same_secret_file(before, opened):
            raise PermissionError("session bridge marker key file identity changed")

        _require_restricted_token_file(path)
        verified = os.lstat(path)
        if _secret_metadata_is_redirect(verified) or not _same_secret_file(
            opened, verified
        ):
            raise PermissionError("session bridge marker key file identity changed")
        if opened.st_size > _MAX_MARKER_KEY_BYTES:
            raise ValueError("session bridge marker key file is too large")

        chunks: list[bytes] = []
        remaining = _MAX_MARKER_KEY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        key = b"".join(chunks)
        if len(key) > _MAX_MARKER_KEY_BYTES:
            raise ValueError("session bridge marker key file is too large")

        after = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            _secret_metadata_is_redirect(current)
            or not _same_secret_file(opened, after)
            or not _same_secret_file(opened, current)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or len(key) != after.st_size
        ):
            raise PermissionError("session bridge marker key file changed while read")
        return key
    except (FileNotFoundError, PermissionError, ValueError):
        raise
    except OSError as exc:
        raise PermissionError(
            "session bridge marker key file could not be read safely"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def create_app(
    *,
    catalog: UnifiedCatalog,
    coordinator: object,
    store: SessionBridgeStore,
    config: BridgeConfig,
    token: str | bytes | None = None,
    marker_key: bytes | None = None,
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
    if marker_key is not None and (
        not isinstance(marker_key, bytes) or len(marker_key) < _MIN_MARKER_KEY_BYTES
    ):
        raise ValueError("session bridge marker key must be at least 32 bytes")

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
        exact_cwd = result.exact_cwd
        if exact_cwd is not None and (
            type(exact_cwd) is not str
            or not exact_cwd
            or exact_cwd != os.path.abspath(os.path.normpath(exact_cwd))
            or any(character in exact_cwd for character in "\x00\r\n")
        ):
            raise RuntimeError("session continuation returned an invalid exact cwd")
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
            "exact_cwd": exact_cwd,
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
        sidebar_status = await asyncio.to_thread(store.sidebar_delivery_status)
        sidebar_status["last_visible_task_id"] = redact_codex_thread_id(
            sidebar_status.get("last_visible_task_id")
        )
        return _status_payload(health, catalog_status, sidebar_status)

    @mcp.tool()
    async def session_claude_visibility_status() -> dict[str, Any]:
        """Return read-only Claude native-visibility health and cost gates."""

        visibility = config.claude_visibility
        if not visibility.enabled:
            raw: Mapping[str, Any] = {
                "counts": {
                    "claude_pending": 0,
                    "claude_leased": 0,
                    "claude_retry": 0,
                    "claude_visible": 0,
                    "claude_failed": 0,
                },
                "retry_codes": {},
                "failed_codes": {},
                "fatal": [],
                "usage": {
                    "local_day": None,
                    "attempts": 0,
                    "reserved_cost_usd": "0",
                },
            }
        else:
            raw = await asyncio.to_thread(store.claude_visibility_status, time.time())
        return _claude_visibility_status_payload(raw, visibility)

    @mcp.tool()
    async def session_sidebar_pending(limit: Any = 5) -> dict[str, Any]:
        """Lease up to five native sidebar registrations for the Codex broker."""

        if type(limit) is not int:
            raise ValueError("sidebar_pending_invalid_request")
        bounded_limit = max(1, min(limit, 5))
        claim_method = getattr(coordinator, "claim_sidebar_jobs_for_delivery", None)
        if not callable(claim_method):
            raise RuntimeError("sidebar_pending_unavailable")
        claimed_tokens: list[str] = []
        try:
            secret = marker_key
            if secret is None:
                secret = await asyncio.to_thread(resolve_marker_key)
            claims = await claim_method(limit=bounded_limit)
            if not isinstance(claims, tuple) or len(claims) > bounded_limit:
                raise ValueError("malformed sidebar claims")
            malformed_token = False
            for claim in claims:
                try:
                    claimed_tokens.append(
                        _exact_sidebar_text(
                            getattr(claim, "lease_token", None), "lease token"
                        )
                    )
                except ValueError:
                    malformed_token = True
            if malformed_token or len(set(claimed_tokens)) != len(claimed_tokens):
                raise ValueError("malformed sidebar lease batch")
            jobs: list[dict[str, Any]] = []
            for claim, token_text in zip(claims, claimed_tokens, strict=True):
                try:
                    job = await asyncio.to_thread(
                        _build_sidebar_broker_job,
                        store,
                        claim,
                        secret,
                    )
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    await _settle_sidebar_claim(
                        store,
                        token_text,
                        "source_identity_mismatch",
                    )
                    continue
                jobs.append(job)
            return {"jobs": jobs}
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            await _rollback_sidebar_claims(store, claimed_tokens)
            raise
        except Exception:
            await _rollback_sidebar_claims(store, claimed_tokens)
            raise ValueError("sidebar_pending_failed") from None

    @mcp.tool()
    async def session_sidebar_bind(
        lease_token: Any,
        codex_thread_id: Any,
    ) -> dict[str, Any]:
        """Durably bind one native Codex task ID before rename or commit."""

        token_text = _exact_sidebar_text(lease_token, "lease token")
        thread_id = _exact_sidebar_text(codex_thread_id, "Codex thread ID")
        bind_method = getattr(coordinator, "bind_sidebar_thread", None)
        if not callable(bind_method):
            raise RuntimeError("sidebar_bind_unavailable")
        try:
            result = await bind_method(
                lease_token=token_text,
                codex_thread_id=thread_id,
            )
            if (
                not isinstance(result, Mapping)
                or result.get("state") != "sidebar_leased"
                or result.get("codex_thread_id") != thread_id
            ):
                raise ValueError("invalid sidebar bind")
            return {
                "state": "sidebar_leased",
                "codex_thread_id": thread_id,
            }
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ValueError("sidebar_bind_failed") from None

    @mcp.tool()
    async def session_sidebar_commit(
        lease_token: Any,
        codex_thread_id: Any,
    ) -> dict[str, Any]:
        """Verify and commit one native Codex sidebar task."""

        token_text = _exact_sidebar_text(lease_token, "lease token")
        thread_id = _exact_sidebar_text(codex_thread_id, "Codex thread ID")
        commit_method = getattr(coordinator, "commit_sidebar_job", None)
        if not callable(commit_method):
            raise RuntimeError("sidebar_commit_unavailable")
        try:
            result = await commit_method(
                lease_token=token_text,
                codex_thread_id=thread_id,
                ensure_lineage=True,
            )
            if (
                not isinstance(result, Mapping)
                or result.get("state") != "sidebar_visible"
                or result.get("codex_thread_id") != thread_id
            ):
                raise ValueError("invalid sidebar commit")
            return {
                "state": "sidebar_visible",
                "codex_thread_id": thread_id,
            }
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ValueError("sidebar_commit_failed") from None

    @mcp.tool()
    async def session_sidebar_fail(
        lease_token: Any,
        error_code: Any,
    ) -> dict[str, Any]:
        """Release or retry one leased sidebar registration with a fixed code."""

        token_text = _exact_sidebar_text(lease_token, "lease token")
        if (
            type(error_code) is not str
            or error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS
        ):
            raise ValueError("sidebar_fail_invalid_request")
        try:
            result = await asyncio.to_thread(
                store.fail_sidebar_job,
                lease_token=token_text,
                error_code=error_code,
                now=time.time(),
            )
            state = result.get("state") if isinstance(result, Mapping) else None
            if state not in {
                "sidebar_pending",
                "sidebar_retry",
                "sidebar_failed",
            }:
                raise ValueError("invalid sidebar failure result")
            return {"state": state, "error_code": error_code}
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ValueError("sidebar_fail_failed") from None

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


def _claude_visibility_status_payload(
    raw: Mapping[str, Any], config: object
) -> dict[str, Any]:
    """Shape the store's read-only status into a fixed public contract."""

    states = (
        "claude_pending",
        "claude_leased",
        "claude_retry",
        "claude_visible",
        "claude_failed",
    )
    counts_raw = raw.get("counts")
    retry_raw = raw.get("retry_codes")
    failed_raw = raw.get("failed_codes")
    usage_raw = raw.get("usage")
    degraded: set[str] = set()
    if not isinstance(counts_raw, Mapping):
        counts_raw = {}
        degraded.add("invalid_status")
    if not isinstance(retry_raw, Mapping):
        retry_raw = {}
        degraded.add("invalid_status")
    if not isinstance(failed_raw, Mapping):
        failed_raw = {}
        degraded.add("invalid_status")
    if not isinstance(usage_raw, Mapping):
        usage_raw = {}
        degraded.add("invalid_status")

    counts: dict[str, int] = {}
    for state in states:
        try:
            count = int(counts_raw.get(state, 0))
        except (TypeError, ValueError):
            count = 0
            degraded.add("invalid_status")
        if count < 0:
            count = 0
            degraded.add("invalid_status")
        counts[state] = count

    retry_codes = _fixed_count_mapping(retry_raw, degraded)
    failed_codes = _fixed_count_mapping(failed_raw, degraded)
    for code, count in retry_codes.items():
        if count > 0 and code not in CLAUDE_VISIBILITY_RETRY_CODES:
            degraded.add("unknown_retry_code")
    for code, count in failed_codes.items():
        if count <= 0:
            continue
        degraded.add(
            code if code in CLAUDE_VISIBILITY_FATAL_CODES else "unknown_failed_code"
        )
    fatal = raw.get("fatal", [])
    if not isinstance(fatal, list):
        degraded.add("invalid_status")
        fatal = []
    else:
        for item in fatal:
            if not isinstance(item, Mapping) or item.get("code") not in {
                "unknown_job_state",
                "unknown_error_code",
            }:
                degraded.add("invalid_status")
            else:
                degraded.add(str(item["code"]))

    attempts = _nonnegative_int(usage_raw.get("attempts"), degraded)
    reserved_cost = _nonnegative_decimal(
        usage_raw.get("reserved_cost_usd", "0"), degraded
    )
    daily_limit = int(getattr(config, "daily_registration_limit"))
    attempt_cost = Decimal(str(getattr(config, "reserved_cost_per_attempt_usd")))
    emergency_limit = Decimal(str(getattr(config, "emergency_daily_cost_usd")))
    cost_remaining = max(Decimal("0"), emergency_limit - reserved_cost)
    cost_blocked = reserved_cost + attempt_cost > emergency_limit

    def tracked(name: str) -> dict[str, Any]:
        value = raw.get(name, {"tracked": False, "value": None})
        if not isinstance(value, Mapping):
            degraded.add("invalid_status")
            return {"tracked": False, "value": None}
        return dict(value)

    payload = {
        "enabled": bool(getattr(config, "enabled")),
        "continuous": bool(getattr(config, "continuous")),
        "counts": counts,
        "retry_codes": retry_codes,
        "failed_codes": failed_codes,
        "usage": {
            "local_day": usage_raw.get("local_day"),
            "attempts": attempts,
            "reserved_cost_usd": str(reserved_cost),
        },
        "cost_gates": {
            "daily_registration_limit": daily_limit,
            "attempts_remaining": max(0, daily_limit - attempts),
            "reserved_cost_per_attempt_usd": str(attempt_cost),
            "emergency_daily_cost_usd": str(emergency_limit),
            "reserved_cost_remaining_usd": str(cost_remaining),
            "registration_limit_reached": attempts >= daily_limit,
            "emergency_cost_limit_reached": cost_blocked,
        },
        "degraded_reasons": [],
        "last_cycle": tracked("last_cycle"),
        "last_empty_cycle": tracked("last_empty_cycle"),
        "last_registrar_result": tracked("last_registrar_result"),
    }
    payload["degraded_reasons"] = sorted(degraded)
    return payload


def _fixed_count_mapping(
    value: Mapping[Any, Any], degraded: set[str]
) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw_code, raw_count in value.items():
        if type(raw_code) is not str or not _FIXED_CODE.fullmatch(raw_code):
            degraded.add("invalid_status")
            continue
        result[raw_code] = _nonnegative_int(raw_count, degraded)
    return result


def _nonnegative_int(value: Any, degraded: set[str]) -> int:
    try:
        selected = int(value)
    except (TypeError, ValueError):
        degraded.add("invalid_status")
        return 0
    if selected < 0:
        degraded.add("invalid_status")
        return 0
    return selected


def _nonnegative_decimal(value: Any, degraded: set[str]) -> Decimal:
    try:
        selected = Decimal(str(value))
    except (InvalidOperation, ValueError):
        degraded.add("invalid_status")
        return Decimal("0")
    if not selected.is_finite() or selected < 0:
        degraded.add("invalid_status")
        return Decimal("0")
    return selected


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
    info = os.lstat(path)
    if _secret_metadata_is_redirect(info):
        raise PermissionError("session bridge token file must not be a redirect")
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
            timeout=_WINDOWS_ACL_TIMEOUT_SECONDS,
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


def _secret_metadata_is_redirect(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _same_secret_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_ino != 0
        and second.st_ino != 0
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
        and stat.S_ISREG(second.st_mode)
    )


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


def _status_payload(
    raw_health: object,
    raw_catalog: object,
    raw_sidebar: object,
) -> dict[str, Any]:
    return {
        "health": _health_status(raw_health),
        "catalog": _catalog_status(raw_catalog),
        "sidebar": _sidebar_status(raw_sidebar),
    }


def _status_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return {}


def _health_status(value: object) -> dict[str, Any]:
    source = _status_mapping(value)
    result: dict[str, Any] = {}
    if type(source.get("running")) is bool:
        result["running"] = source["running"]
    providers = source.get("providers")
    if isinstance(providers, Mapping):
        provider_map = cast(Mapping[str, Any], providers)
        shaped_providers: dict[str, Any] = {}
        for provider in (Provider.CLAUDE.value, Provider.CODEX.value):
            raw_provider = provider_map.get(provider)
            if not isinstance(raw_provider, Mapping):
                continue
            raw_provider = cast(Mapping[str, Any], raw_provider)
            shaped: dict[str, Any] = {}
            for field in ("last_success", "lag_seconds"):
                number = _finite_status_number(raw_provider.get(field))
                if number is not None or raw_provider.get(field) is None:
                    shaped[field] = number
            reason = raw_provider.get("degraded_reason")
            if reason is None or _fixed_status_code(reason) is not None:
                shaped["degraded_reason"] = reason
            shaped_providers[provider] = shaped
        if shaped_providers:
            result["providers"] = shaped_providers
    watcher_state = source.get("watcher_state")
    if type(watcher_state) is str and watcher_state in {
        "not_started",
        "running",
        "stopped",
        "degraded",
    }:
        result["watcher_state"] = watcher_state
    watcher_error = _fixed_status_code(source.get("watcher_error_code"))
    if watcher_error is not None or source.get("watcher_error_code") is None:
        if "watcher_error_code" in source:
            result["watcher_error_code"] = watcher_error
    queue_counts = source.get("queue_counts")
    if isinstance(queue_counts, Mapping):
        queue_counts = cast(Mapping[str, Any], queue_counts)
        result["queue_counts"] = {
            state.value: _nonnegative_status_int(queue_counts.get(state.value), 0)
            for state in MirrorJobState
        }
    mirror_mode = source.get("mirror_mode")
    if type(mirror_mode) is str and mirror_mode in {"automatic", "manual"}:
        result["mirror_mode"] = mirror_mode
    backfill = source.get("backfill_progress")
    if isinstance(backfill, Mapping):
        backfill = cast(Mapping[str, Any], backfill)
        shaped_backfill: dict[str, Any] = {}
        for provider in (Provider.CLAUDE.value, Provider.CODEX.value):
            progress = backfill.get(provider)
            if not isinstance(progress, Mapping):
                continue
            progress = cast(Mapping[str, Any], progress)
            shaped_progress = {
                field: _nonnegative_status_int(progress.get(field), 0)
                for field in ("version", "indexed_total", "remaining")
            }
            native_id = progress.get("last_committed_native_id")
            if (
                type(native_id) is str
                and 0 < len(native_id) <= 512
                and native_id == native_id.strip()
                and "\n" not in native_id
                and "\r" not in native_id
            ):
                shaped_progress["last_committed_native_id"] = native_id
            shaped_backfill[provider] = shaped_progress
        result["backfill_progress"] = shaped_backfill
    fallback = source.get("registration_turn_fallback")
    if fallback is None or type(fallback) is bool:
        if "registration_turn_fallback" in source:
            result["registration_turn_fallback"] = fallback
    registration = source.get("sidebar_registration_counts")
    if isinstance(registration, Mapping):
        registration = cast(Mapping[str, Any], registration)
        result["sidebar_registration_counts"] = {
            field: _nonnegative_status_int(registration.get(field), 0)
            for field in ("examined", "queued", "claude", "hermes", "failed")
        }
    if "provider_calls_inflight" in source:
        result["provider_calls_inflight"] = _nonnegative_status_int(
            source.get("provider_calls_inflight"), 0
        )
    errors = source.get("recent_error_codes")
    if isinstance(errors, (list, tuple)):
        result["recent_error_codes"] = _fixed_status_codes(errors)
    return result


def _catalog_status(value: object) -> dict[str, Any]:
    source = _status_mapping(value)
    providers = source.get("providers")
    shaped_providers: dict[str, dict[str, int]] = {}
    if isinstance(providers, Mapping):
        providers = cast(Mapping[str, Any], providers)
        for provider in (
            Provider.CLAUDE.value,
            Provider.CODEX.value,
            Provider.HERMES.value,
        ):
            raw_provider = providers.get(provider)
            if not isinstance(raw_provider, Mapping):
                continue
            raw_provider = cast(Mapping[str, Any], raw_provider)
            shaped_providers[provider] = {
                "sessions": _nonnegative_status_int(
                    raw_provider.get("sessions"), 0
                ),
                "degraded": _nonnegative_status_int(
                    raw_provider.get("degraded"), 0
                ),
            }
    return {
        "providers": shaped_providers,
        "total_sessions": _nonnegative_status_int(source.get("total_sessions"), 0),
    }


def _sidebar_status(value: object) -> dict[str, Any]:
    source = _status_mapping(value)
    providers = source.get("eligible_by_provider")
    provider_counts = _status_mapping(providers)
    counts = source.get("counts")
    state_counts = _status_mapping(counts)
    latencies = source.get("delivery_latency_seconds")
    latency_values = _status_mapping(latencies)
    task_id = source.get("last_visible_task_id")
    if type(task_id) is not str or re.fullmatch(
        r"task:[0-9a-f]{16}", task_id
    ) is None:
        task_id = None
    recent = source.get("recent_error_codes")
    return {
        "eligible_by_provider": {
            provider: _nonnegative_status_int(provider_counts.get(provider), 0)
            for provider in (Provider.CLAUDE.value, Provider.HERMES.value)
        },
        "counts": {
            state.value: _nonnegative_status_int(state_counts.get(state.value), 0)
            for state in SidebarJobState
        },
        "oldest_pending_age_seconds": _finite_status_number(
            source.get("oldest_pending_age_seconds")
        ),
        "last_heartbeat_at": _finite_status_number(source.get("last_heartbeat_at")),
        "last_visible_task_id": task_id,
        "recent_error_codes": (
            _fixed_status_codes(recent)
            if isinstance(recent, (list, tuple))
            else []
        ),
        "delivery_latency_seconds": {
            percentile: _finite_status_number(latency_values.get(percentile))
            for percentile in ("p50", "p95", "p99")
        },
    }


def _finite_status_number(value: object) -> float | None:
    if type(value) is int:
        number = float(cast(int, value))
    elif type(value) is float:
        number = cast(float, value)
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _nonnegative_status_int(value: object, default: int) -> int:
    if type(value) is int and value >= 0:
        return value
    return default


def _fixed_status_code(value: object) -> str | None:
    if type(value) is str and _FIXED_CODE.fullmatch(value):
        return value
    return None


def _fixed_status_codes(values: tuple[Any, ...] | list[Any]) -> list[str]:
    result: list[str] = []
    for value in values[:10]:
        code = _fixed_status_code(value)
        if code is not None and code not in result:
            result.append(code)
    return result


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


def _exact_sidebar_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"sidebar {label} is malformed")
    return value


def _build_sidebar_broker_job(
    store: SessionBridgeStore,
    claim: object,
    marker_key: bytes,
) -> dict[str, Any]:
    lease_token = _exact_sidebar_text(
        getattr(claim, "lease_token", None), "lease token"
    )
    source_session_id = _exact_sidebar_text(
        getattr(claim, "source_session_id", None), "source session ID"
    )
    bridge_id = _exact_sidebar_text(
        getattr(claim, "bridge_id", None), "bridge ID"
    )
    reconcile_required = getattr(claim, "reconcile_required", None)
    rename_required = getattr(claim, "rename_required", None)
    if type(reconcile_required) is not bool or type(rename_required) is not bool:
        raise ValueError("sidebar claim flags are malformed")
    candidate = store.get_sidebar_candidate_for_delivery(source_session_id)
    if candidate.bridge_id != bridge_id:
        raise ValueError("sidebar claim bridge identity is malformed")
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        marker_key,
    )
    recovered = getattr(claim, "recovered_thread", None)
    reserved_thread_id = getattr(claim, "reserved_thread_id", None)
    recovered_thread_id = (
        None
        if reserved_thread_id is None
        else _exact_sidebar_text(reserved_thread_id, "reserved thread ID")
    )
    if recovered is not None:
        verified_thread_id = _exact_sidebar_text(
            getattr(recovered, "thread_id", None), "recovered thread ID"
        )
        if (
            getattr(recovered, "source_session_id", None) != source_session_id
            or getattr(recovered, "bridge_id", None) != bridge_id
        ):
            raise ValueError("recovered sidebar identity is malformed")
        if recovered_thread_id is not None and recovered_thread_id != verified_thread_id:
            raise ValueError("recovered sidebar thread identity is malformed")
        recovered_thread_id = verified_thread_id
    return {
        "lease_token": lease_token,
        "registration_prompt": build_registration_prompt(candidate, marker),
        "title": candidate.title,
        "provider": candidate.provider.value,
        "cwd": candidate.cwd,
        "git_root": candidate.git_root,
        "git_branch": candidate.git_branch,
        "git_head": candidate.git_head,
        "worktree_id": candidate.worktree_id,
        "reconcile_required": reconcile_required,
        "rename_required": rename_required,
        "recovered_thread_id": recovered_thread_id,
    }


async def _settle_sidebar_claim(
    store: SessionBridgeStore,
    lease_token: str,
    error_code: str,
) -> None:
    await asyncio.to_thread(
        store.fail_sidebar_job,
        lease_token=lease_token,
        error_code=error_code,
        now=time.time(),
    )


async def _rollback_sidebar_claims(
    store: SessionBridgeStore,
    lease_tokens: list[str],
) -> None:
    for lease_token in dict.fromkeys(lease_tokens):
        try:
            await _settle_sidebar_claim(store, lease_token, "broker_time_budget")
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            continue


__all__ = [
    "EXPECTED_TOOLS",
    "create_app",
    "resolve_bearer_token",
    "resolve_marker_key",
]
