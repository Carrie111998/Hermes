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
    CLAUDE_VISIBILITY_STATUS_FATAL_CODES,
)
from .codex_adapter import SidebarVerificationError
from .config import BridgeConfig, is_canonical_sidebar_string
from .coordinator import ContinueRequest, ContinueResult
from .health import build_session_health_evidence
from .mirror import MirrorPolicy, enqueue_mirror_job
from .models import (
    BridgeMarkerPayload,
    HydrationMarkerPayload,
    MirrorJobState,
    Provider,
    SidebarHydrationState,
    SidebarJobState,
    encode_bridge_marker,
)
from .preview import build_session_preview
from .sidebar import (
    build_registration_prompt,
    decode_hydration_marker,
)
from .sidebar_reconciliation import SidebarReconciliationState
from .store import (
    HYDRATION_FATAL_ERRORS,
    HYDRATION_RETRYABLE_ERRORS,
    SIDEBAR_FATAL_ERRORS,
    SIDEBAR_PRECREATE_RESOLUTION_CODE,
    SIDEBAR_RETRYABLE_ERRORS,
    SIDEBAR_TERMINAL_RESOLUTION_CODE,
    SIDEBAR_UNBOUND_RESOLUTION_CODE,
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
    "session_sidebar_reserve",
    "session_sidebar_bind",
    "session_sidebar_commit",
    "session_sidebar_fail",
    "session_sidebar_hydration_pending",
    "session_sidebar_hydration_reserve",
    "session_sidebar_hydration_commit",
    "session_sidebar_hydration_fail",
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
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
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
    bearer_token = resolve_bearer_token() if token is None else _validated_token(token)
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

        observation_started_at = time.time()
        health_method = getattr(coordinator, "health", None)
        health = health_method() if callable(health_method) else {"running": False}
        health_observed_at = time.time()
        catalog_status = await asyncio.to_thread(catalog.status)
        catalog_observed_at = time.time()
        sidebar_status = await asyncio.to_thread(
            store.sidebar_delivery_status,
            inbox_cwd=config.sidebar.inbox_cwd,
            placement_generation=config.sidebar.placement_generation,
        )
        status_time = time.time()
        sidebar_observed_at = status_time
        heartbeat_at = _finite_status_number(sidebar_status.get("last_heartbeat_at"))
        heartbeat_age = (
            max(0.0, status_time - heartbeat_at) if heartbeat_at is not None else None
        )
        counts = _status_mapping(sidebar_status.get("counts"))
        pending = sum(
            _nonnegative_status_int(counts.get(state.value), 0)
            for state in (
                SidebarJobState.PENDING,
                SidebarJobState.LEASED,
                SidebarJobState.RETRY,
            )
        )
        oldest_eligible_age = _finite_status_number(
            sidebar_status.get("oldest_eligible_age_seconds")
        )
        sidebar_status.update({
            "heartbeat_stale": (
                heartbeat_age is not None
                and heartbeat_age > config.sidebar.heartbeat_stale_seconds
            ),
            "oldest_job_overdue": (
                pending > 0
                and oldest_eligible_age is not None
                and oldest_eligible_age > config.sidebar.oldest_job_alert_seconds
            ),
            "broker": {
                "thread_id": config.sidebar.broker_thread_id,
                "project_id": config.sidebar.broker_project_id,
                "cwd": config.sidebar.broker_cwd,
            },
        })
        sidebar_status["degraded_reasons"] = [
            code
            for code, active in (
                ("broker_heartbeat_stale", sidebar_status["heartbeat_stale"]),
                ("oldest_pending_stale", sidebar_status["oldest_job_overdue"]),
            )
            if active
        ]
        hydration_status = await asyncio.to_thread(
            store.sidebar_hydration_status,
            time.time(),
        )
        hydration_observed_at = time.time()
        visibility = config.claude_visibility
        if visibility.enabled:
            raw_visibility_status = await asyncio.to_thread(
                store.claude_visibility_status,
                time.time(),
            )
        else:
            raw_visibility_status = _disabled_claude_visibility_status()
        visibility_status = _claude_visibility_status_payload(
            raw_visibility_status,
            visibility,
        )
        claude_visibility_observed_at = time.time()
        sidebar_status["last_visible_task_id"] = redact_codex_thread_id(
            sidebar_status.get("last_visible_task_id")
        )
        payload = _status_payload(
            health,
            catalog_status,
            sidebar_status,
            hydration_status,
            hydration_enabled=config.sidebar.legacy_hydration_enabled,
        )
        observation_completed_at = time.time()
        payload["evidence_v1"] = build_session_health_evidence(
            observation_started_at=observation_started_at,
            observation_completed_at=observation_completed_at,
            health_observed_at=health_observed_at,
            catalog_observed_at=catalog_observed_at,
            sidebar_observed_at=sidebar_observed_at,
            hydration_observed_at=hydration_observed_at,
            claude_visibility_observed_at=claude_visibility_observed_at,
            coordinator_health=health,
            catalog_status=catalog_status,
            sidebar_status=sidebar_status,
            hydration_status=hydration_status,
            claude_visibility_status=visibility_status,
            catalog_scan_seconds=config.service.catalog_scan_seconds,
            hydration_enabled=config.sidebar.legacy_hydration_enabled,
            claude_visibility_enabled=visibility.enabled,
        )
        return payload

    @mcp.tool()
    async def session_claude_visibility_status() -> dict[str, Any]:
        """Return read-only Claude native-visibility health and cost gates."""

        visibility = config.claude_visibility
        raw = (
            _disabled_claude_visibility_status()
            if not visibility.enabled
            else await asyncio.to_thread(store.claude_visibility_status, time.time())
        )
        return _claude_visibility_status_payload(raw, visibility)

    @mcp.tool()
    async def session_sidebar_pending(limit: Any = 1) -> dict[str, Any]:
        """Lease exactly one native sidebar registration for the Codex broker."""

        if type(limit) is not int or limit != 1:
            raise ValueError("sidebar_pending_invalid_request")
        bounded_limit = 1
        claim_method = getattr(coordinator, "claim_sidebar_jobs_for_delivery", None)
        if not callable(claim_method):
            raise RuntimeError("sidebar_pending_unavailable")
        claimed_tokens: list[str] = []
        try:
            secret = marker_key
            if secret is None:
                secret = await asyncio.to_thread(resolve_marker_key)
            claims = await claim_method(limit=bounded_limit)
            claimed_tokens, malformed_claims = _sidebar_delivery_claim_tokens(
                claims,
                limit=bounded_limit,
            )
            if malformed_claims:
                raise ValueError("malformed sidebar lease batch")
            assert isinstance(claims, tuple)
            jobs: list[dict[str, Any]] = []
            for claim, token_text in zip(claims, claimed_tokens, strict=True):
                try:
                    job = await asyncio.to_thread(
                        _build_sidebar_broker_job,
                        store,
                        claim,
                        secret,
                        config.sidebar.preview_budget_chars,
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
    async def session_sidebar_hydration_pending(limit: Any = 1) -> dict[str, Any]:
        """Lease exactly one in-place hydration job for the Codex broker."""

        if type(limit) is not int or limit != 1:
            raise ValueError("sidebar_hydration_pending_invalid_request")
        claim_method = getattr(
            coordinator,
            "claim_sidebar_hydration_for_delivery",
            None,
        )
        if not callable(claim_method):
            raise RuntimeError("sidebar_hydration_pending_unavailable")
        claimed_tokens: list[tuple[str, str]] = []
        try:
            secret = marker_key
            if secret is None:
                secret = await asyncio.to_thread(resolve_marker_key)
            claims = await claim_method(limit=1)
            if not isinstance(claims, tuple) or len(claims) > 1:
                raise ValueError("malformed sidebar hydration lease batch")
            jobs: list[dict[str, Any]] = []
            for claim in claims:
                lease_token = _exact_sidebar_text(
                    getattr(claim, "lease_token", None),
                    "hydration lease token",
                )
                thread_id = _exact_sidebar_text(
                    getattr(claim, "codex_thread_id", None),
                    "hydration Codex thread ID",
                )
                claimed_tokens.append((lease_token, thread_id))
                jobs.append(_build_sidebar_hydration_broker_job(claim, secret))
            return {"jobs": jobs}
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            for lease_token, thread_id in claimed_tokens:
                try:
                    await asyncio.to_thread(
                        store.fail_sidebar_hydration_job,
                        lease_token=lease_token,
                        error_code="source_identity_mismatch",
                        codex_thread_id=thread_id,
                        now=time.time(),
                    )
                except Exception:
                    pass
            raise ValueError("sidebar_hydration_pending_failed") from None

    @mcp.tool()
    async def session_sidebar_hydration_reserve(
        lease_token: Any,
    ) -> dict[str, Any]:
        """Freeze one hydration send before dispatch to the exact native task."""

        token_text = _exact_sidebar_text(lease_token, "hydration lease token")
        try:
            result = await asyncio.to_thread(
                store.reserve_sidebar_hydration_send,
                lease_token=token_text,
                now=time.time(),
            )
            if (
                not isinstance(result, Mapping)
                or result.get("state") != "hydration_leased"
                or result.get("send_reserved_at") is None
            ):
                raise ValueError("malformed hydration reservation")
            return {"state": "hydration_leased", "send_reserved": True}
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ValueError("sidebar_hydration_reserve_failed") from None

    @mcp.tool()
    async def session_sidebar_hydration_commit(
        lease_token: Any,
        codex_thread_id: Any,
        hydration_marker: Any,
    ) -> dict[str, Any]:
        """Commit a verified hydration marker on one exact Codex task."""

        token_text = _exact_sidebar_text(lease_token, "hydration lease token")
        thread_id = _exact_sidebar_text(
            codex_thread_id,
            "hydration Codex thread ID",
        )
        marker = _exact_sidebar_text(hydration_marker, "hydration marker")
        try:
            result = await asyncio.to_thread(
                store.commit_sidebar_hydration_job,
                lease_token=token_text,
                codex_thread_id=thread_id,
                hydration_marker=marker,
                now=time.time(),
            )
            if (
                not isinstance(result, Mapping)
                or result.get("state") != "hydration_visible"
                or result.get("codex_thread_id") != thread_id
            ):
                raise ValueError("malformed hydration completion")
            return {
                "state": "hydration_visible",
                "codex_thread_id": thread_id,
            }
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ValueError("sidebar_hydration_commit_failed") from None

    @mcp.tool()
    async def session_sidebar_hydration_fail(
        lease_token: Any,
        error_code: Any,
        codex_thread_id: Any,
    ) -> dict[str, Any]:
        """Fail or retry one exact hydration lease using a fixed public code."""

        token_text = _exact_sidebar_text(lease_token, "hydration lease token")
        thread_id = _exact_sidebar_text(
            codex_thread_id,
            "hydration Codex thread ID",
        )
        if (
            type(error_code) is not str
            or error_code
            not in HYDRATION_RETRYABLE_ERRORS | HYDRATION_FATAL_ERRORS
        ):
            raise ValueError("sidebar_hydration_fail_invalid_request")
        try:
            result = await asyncio.to_thread(
                store.fail_sidebar_hydration_job,
                lease_token=token_text,
                error_code=error_code,
                codex_thread_id=thread_id,
                now=time.time(),
            )
            if not isinstance(result, Mapping):
                raise ValueError("malformed hydration failure")
            return {
                "state": result.get("state"),
                "error_code": result.get("error_code"),
                "send_reserved": result.get("send_reserved_at") is not None,
            }
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ValueError("sidebar_hydration_fail_failed") from None

    @mcp.tool()
    async def session_sidebar_reserve(
        lease_token: Any,
        reconciliation_proof_digest: Any,
        reconciliation_generation: Any,
    ) -> dict[str, Any]:
        """Freshly reconcile and reserve create only from authoritative absence."""

        try:
            token_text = _exact_sidebar_text(lease_token, "lease token")
            proof_digest = _exact_sidebar_sha256(
                reconciliation_proof_digest,
                "reconciliation proof digest",
            )
            proof_generation = _exact_sidebar_text(
                reconciliation_generation,
                "reconciliation generation",
            )
            reserve_method = getattr(
                coordinator,
                "reserve_sidebar_create_authoritatively",
                None,
            )
            if not callable(reserve_method):
                raise TypeError("authoritative sidebar reserve is unavailable")
            result = await reserve_method(
                lease_token=token_text,
                reconciliation_proof_digest=proof_digest,
                reconciliation_generation=proof_generation,
            )
            if not isinstance(result, Mapping):
                raise ValueError("authoritative sidebar reserve is malformed")
            if set(result) == {"state", "create_reserved"} and result == {
                "state": "sidebar_leased",
                "create_reserved": True,
            }:
                return dict(result)
            if (
                set(result) == {"state", "codex_thread_id"}
                and result.get("state") == "recovered"
            ):
                thread_id = _exact_sidebar_text(
                    result.get("codex_thread_id"),
                    "recovered Codex thread ID",
                )
                return {
                    "state": "recovered",
                    "codex_thread_id": thread_id,
                    "create_reserved": False,
                }
            raise ValueError("authoritative sidebar reserve is malformed")
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ValueError("sidebar_reserve_failed") from None

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
        except SidebarVerificationError as exc:
            raise ValueError(exc.code) from None
        except Exception:
            raise ValueError("sidebar_commit_failed") from None

    @mcp.tool()
    async def session_sidebar_fail(
        lease_token: Any,
        error_code: Any,
        codex_thread_id: Any = None,
    ) -> dict[str, Any]:
        """Release or retry one leased sidebar registration with a fixed code."""

        token_text = _exact_sidebar_text(lease_token, "lease token")
        if (
            type(error_code) is not str
            or error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS
        ):
            raise ValueError("sidebar_fail_invalid_request")
        thread_id = (
            None
            if codex_thread_id is None
            else _exact_sidebar_text(codex_thread_id, "Codex thread ID")
        )
        try:
            result = await asyncio.to_thread(
                store.fail_sidebar_job,
                lease_token=token_text,
                error_code=error_code,
                now=time.time(),
                codex_thread_id=thread_id,
            )
            state = result.get("state") if isinstance(result, Mapping) else None
            if state not in {
                "sidebar_pending",
                "sidebar_retry",
                "sidebar_failed",
            }:
                raise ValueError("invalid sidebar failure result")
            result_thread_id = (
                result.get("codex_thread_id") if isinstance(result, Mapping) else None
            )
            if thread_id is not None and result_thread_id != thread_id:
                raise ValueError("invalid sidebar failure thread identity")
            response = {"state": state, "error_code": error_code}
            if thread_id is not None:
                response["codex_thread_id"] = thread_id
            return response
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


def _disabled_claude_visibility_status() -> Mapping[str, Any]:
    return {
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
    lineage_raw = raw.get(
        "lineage",
        {
            "unlinked_visible": 0,
            "repairable": 0,
            "blocked": 0,
            "blocker_codes": {},
        },
    )
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
    if not isinstance(lineage_raw, Mapping) or set(lineage_raw) != {
        "unlinked_visible",
        "repairable",
        "blocked",
        "blocker_codes",
    }:
        lineage_raw = {
            "unlinked_visible": 0,
            "repairable": 0,
            "blocked": 0,
            "blocker_codes": {},
        }
        degraded.add("invalid_status")

    counts: dict[str, int] = {}
    if set(counts_raw) != set(states):
        degraded.add("invalid_status")
    for state in states:
        counts[state] = _nonnegative_int(counts_raw.get(state, 0), degraded)

    retry_codes = _fixed_count_mapping(retry_raw, degraded)
    failed_codes = _fixed_count_mapping(failed_raw, degraded)
    lineage_blockers_raw = lineage_raw.get("blocker_codes")
    if not isinstance(lineage_blockers_raw, Mapping):
        lineage_blockers_raw = {}
        degraded.add("invalid_status")
    lineage = {
        "unlinked_visible": _nonnegative_int(
            lineage_raw.get("unlinked_visible"), degraded
        ),
        "repairable": _nonnegative_int(lineage_raw.get("repairable"), degraded),
        "blocked": _nonnegative_int(lineage_raw.get("blocked"), degraded),
        "blocker_codes": _fixed_count_mapping(lineage_blockers_raw, degraded),
    }
    if (
        lineage["repairable"] + lineage["blocked"] != lineage["unlinked_visible"]
        or sum(lineage["blocker_codes"].values()) != lineage["blocked"]
    ):
        degraded.add("invalid_status")
    if lineage["unlinked_visible"] > 0:
        degraded.add("unlinked_visible_lineage")
    for code, count in lineage["blocker_codes"].items():
        if count > 0 and code != "claude_lineage_target_missing":
            degraded.add(code)
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
            if (
                not isinstance(item, Mapping)
                or item.get("code") not in CLAUDE_VISIBILITY_STATUS_FATAL_CODES
            ):
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
        if not isinstance(value, Mapping) or set(value) != {"tracked", "value"}:
            degraded.add("invalid_status")
            return {"tracked": False, "value": None}
        selected = value.get("tracked")
        nested = value.get("value")
        if type(selected) is not bool or (not selected and nested is not None):
            degraded.add("invalid_status")
            return {"tracked": False, "value": None}
        if not selected:
            return {"tracked": False, "value": None}
        if name == "last_empty_cycle":
            timestamp = _finite_number(nested)
            if timestamp is None:
                degraded.add("invalid_status")
                return {"tracked": False, "value": None}
            return {"tracked": True, "value": timestamp}
        include_empty = name == "last_cycle"
        shaped = _tracked_result_value(nested, include_empty=include_empty)
        if shaped is None:
            degraded.add("invalid_status")
            return {"tracked": False, "value": None}
        return {"tracked": True, "value": shaped}

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
        "lineage": lineage,
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
    if type(value) is not int:
        degraded.add("invalid_status")
        return 0
    selected = value
    if selected < 0:
        degraded.add("invalid_status")
        return 0
    return selected


def _tracked_result_value(value: Any, *, include_empty: bool) -> dict[str, Any] | None:
    expected = {"at", "sequence", "status", "error_code"}
    if include_empty:
        expected.add("empty_verified")
    if not isinstance(value, Mapping) or set(value) != expected:
        return None
    at = _finite_number(value.get("at"))
    sequence = value.get("sequence")
    status = value.get("status")
    error_code = value.get("error_code")
    if (
        at is None
        or type(sequence) is not int
        or sequence < 1
        or type(status) is not str
        or not _FIXED_CODE.fullmatch(status)
        or (
            error_code is not None
            and (type(error_code) is not str or not _FIXED_CODE.fullmatch(error_code))
        )
    ):
        return None
    result: dict[str, Any] = {
        "at": at,
        "sequence": sequence,
        "status": status,
        "error_code": error_code,
    }
    if include_empty:
        empty_verified = value.get("empty_verified")
        if type(empty_verified) is not bool:
            return None
        result["empty_verified"] = empty_verified
    return result


def _finite_number(value: Any) -> float | None:
    if type(value) not in {int, float}:
        return None
    try:
        selected = float(value)
    except OverflowError:
        return None
    if not math.isfinite(selected):
        return None
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
        raise PermissionError(
            "session bridge token file ACL could not be verified"
        ) from exc
    try:
        snapshot = json.loads(result.stdout)
        _validate_windows_token_acl(
            current_sid=snapshot["current_sid"],
            owner_sid=snapshot["owner_sid"],
            rules=snapshot["rules"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PermissionError(
            "session bridge token file ACL could not be verified"
        ) from exc


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
    if (
        not isinstance(owner_sid, str)
        or owner_sid.strip().casefold() != normalized_current
    ):
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
    raw_hydration: object,
    *,
    hydration_enabled: bool,
) -> dict[str, Any]:
    sidebar = _sidebar_status(raw_sidebar)
    sidebar["hydration"] = _hydration_status(
        raw_hydration,
        enabled=hydration_enabled,
    )
    return {
        "health": _health_status(raw_health),
        "catalog": _catalog_status(raw_catalog),
        "sidebar": sidebar,
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
                "sessions": _nonnegative_status_int(raw_provider.get("sessions"), 0),
                "degraded": _nonnegative_status_int(raw_provider.get("degraded"), 0),
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
    stage_latency_values = _status_mapping(source.get("stage_latency_seconds"))
    task_id = source.get("last_visible_task_id")
    if type(task_id) is not str or re.fullmatch(r"task:[0-9a-f]{16}", task_id) is None:
        task_id = None
    recent = source.get("recent_error_codes")
    failed_count = _nonnegative_status_int(
        state_counts.get(SidebarJobState.FAILED.value), 0
    )
    blocking_failed_count = _nonnegative_status_int(
        source.get("blocking_failed_count"), failed_count
    )
    terminally_resolved_failed_count = _nonnegative_status_int(
        source.get("terminally_resolved_failed_count"), 0
    )
    ineffective_terminal_resolution_count = _nonnegative_status_int(
        source.get("ineffective_terminal_resolution_count"), 0
    )
    terminal_source = _status_mapping(source.get("terminal_resolutions"))
    raw_resolution_codes = terminal_source.get("by_resolution_code")
    resolution_codes = _status_mapping(raw_resolution_codes)
    terminal_effective = _nonnegative_status_int(
        terminal_source.get("effective"), terminally_resolved_failed_count
    )
    fixed_resolution_codes = (
        SIDEBAR_TERMINAL_RESOLUTION_CODE,
        SIDEBAR_PRECREATE_RESOLUTION_CODE,
        SIDEBAR_UNBOUND_RESOLUTION_CODE,
    )
    shaped_resolution_codes = {
        code: _nonnegative_status_int(
            resolution_codes.get(code),
            terminally_resolved_failed_count
            if code == SIDEBAR_TERMINAL_RESOLUTION_CODE
            else 0,
        )
        for code in fixed_resolution_codes
    }
    resolution_codes_valid = (
        isinstance(raw_resolution_codes, Mapping)
        and all(
            type(code) is str and code in fixed_resolution_codes
            for code in resolution_codes
        )
        and all(
            type(count) is int and count >= 0
            for code, count in resolution_codes.items()
            if code in fixed_resolution_codes
        )
        and sum(shaped_resolution_codes.values()) == terminal_effective
    )
    terminal_resolution_ledger_valid = (
        source.get("terminal_resolution_ledger_valid") is True
        and resolution_codes_valid
    )
    raw_blockers = source.get("execution_blockers")
    execution_blockers = (
        [
            code
            for code in _fixed_status_codes(list(raw_blockers))
            if code
            in {
                "sidebar_failed",
                "sidebar_terminal_resolution_mismatch",
                "sidebar_terminal_resolution_ledger_invalid",
                "unknown_retry_code",
            }
        ]
        if isinstance(raw_blockers, (list, tuple))
        else []
    )
    if (
        not resolution_codes_valid
        and "sidebar_terminal_resolution_ledger_invalid" not in execution_blockers
    ):
        execution_blockers.append("sidebar_terminal_resolution_ledger_invalid")
    scheduler_source = _status_mapping(source.get("scheduler"))
    fresh_claims = _nonnegative_status_int(
        scheduler_source.get("fresh_claims_since_oldest"),
        0,
    )
    if fresh_claims > 3:
        fresh_claims = 0
    expected_lane = "oldest" if fresh_claims == 3 else "fresh"
    next_lane = scheduler_source.get("next_lane")
    if next_lane != expected_lane:
        next_lane = expected_lane
    recovery_source = _status_mapping(source.get("recovery"))
    recovery_lane = recovery_source.get("lane")
    recovery_status = recovery_source.get("status")
    recovery_at = _finite_status_number(recovery_source.get("last_cycle_at"))
    if (
        recovery_lane not in {"hydration", "registration"}
        or recovery_status not in {"idle", "visible", "retry", "failed", "unsettled"}
        or recovery_at is None
    ):
        recovery_lane = None
        recovery_status = None
        recovery_at = None
    reconciliation_source = _status_mapping(source.get("reconciliation_counts"))
    reconciliation_blocked_source = _status_mapping(
        source.get("reconciliation_blocked_codes")
    )
    placement = _sidebar_placement_status(source.get("placement"))
    broker_source = _status_mapping(source.get("broker"))
    broker = {
        key: value
        for key, value in (
            ("thread_id", broker_source.get("thread_id")),
            ("project_id", broker_source.get("project_id")),
            ("cwd", broker_source.get("cwd")),
        )
        if is_canonical_sidebar_string(value)
    }
    raw_degraded_reasons = source.get("degraded_reasons")
    degraded_reasons = (
        [
            code
            for code in raw_degraded_reasons
            if code in {"broker_heartbeat_stale", "oldest_pending_stale"}
        ]
        if isinstance(raw_degraded_reasons, (list, tuple))
        else []
    )
    public_counts = {
        **{
            state.value: _nonnegative_status_int(state_counts.get(state.value), 0)
            for state in SidebarJobState
        },
        **{
            field: _nonnegative_status_int(state_counts.get(field), 0)
            for field in (
                "ambiguous",
                "needs_attention",
                "projectless_legacy_count",
            )
        },
    }
    public_counts["needs_attention"] = blocking_failed_count
    result = {
        "eligible_by_provider": {
            provider: _nonnegative_status_int(provider_counts.get(provider), 0)
            for provider in (Provider.CLAUDE.value, Provider.HERMES.value)
        },
        "counts": public_counts,
        "blocking_failed_count": blocking_failed_count,
        "terminally_resolved_failed_count": terminally_resolved_failed_count,
        "ineffective_terminal_resolution_count": (
            ineffective_terminal_resolution_count
        ),
        "terminal_resolution_ledger_valid": terminal_resolution_ledger_valid,
        "terminal_resolutions": {
            "total": _nonnegative_status_int(
                terminal_source.get("total"),
                terminally_resolved_failed_count
                + ineffective_terminal_resolution_count,
            ),
            "effective": terminal_effective,
            "ineffective": _nonnegative_status_int(
                terminal_source.get("ineffective"),
                ineffective_terminal_resolution_count,
            ),
            "by_resolution_code": shaped_resolution_codes,
        },
        "execution_blockers": execution_blockers,
        "oldest_eligible_age_seconds": _finite_status_number(
            source.get("oldest_eligible_age_seconds")
        ),
        "oldest_pending_age_seconds": _finite_status_number(
            source.get("oldest_pending_age_seconds")
        ),
        "last_heartbeat_at": _finite_status_number(source.get("last_heartbeat_at")),
        "heartbeat_stale": source.get("heartbeat_stale") is True,
        "oldest_job_overdue": source.get("oldest_job_overdue") is True,
        "degraded_reasons": degraded_reasons,
        "broker": broker,
        "last_visible_task_id": task_id,
        "recent_error_codes": (
            _fixed_status_codes(recent) if isinstance(recent, (list, tuple)) else []
        ),
        "reconciliation_counts": {
            state: _nonnegative_status_int(reconciliation_source.get(state), 0)
            for state in ("recovered", "absence_proven", "blocked")
        },
        "reconciliation_blocked_codes": {
            code: _nonnegative_status_int(
                reconciliation_blocked_source.get(code), 0
            )
            for code in (
                "marker_conflict",
                "native_create_ambiguous",
                "bridge_temporarily_unavailable",
            )
        },
        "oldest_reconciliation_wait_age_seconds": _finite_status_number(
            source.get("oldest_reconciliation_wait_age_seconds")
        ),
        "reconciliation_scan_age_seconds": _finite_status_number(
            source.get("reconciliation_scan_age_seconds")
        ),
        "recovered_existing_total": _nonnegative_status_int(
            source.get("recovered_existing_total"), 0
        ),
        "created_new_total": _nonnegative_status_int(
            source.get("created_new_total"), 0
        ),
        "delivery_latency_seconds": {
            percentile: _finite_status_number(latency_values.get(percentile))
            for percentile in ("p50", "p95", "p99")
        },
        "stage_latency_seconds": {
            stage: {
                percentile: _finite_status_number(
                    _status_mapping(stage_latency_values.get(stage)).get(percentile)
                )
                for percentile in ("p50", "p95")
            }
            for stage in (
                "source_to_index",
                "index_to_queue",
                "queue_to_visible",
                "source_to_visible",
            )
        },
        "scheduler": {
            "fresh_claims_since_oldest": fresh_claims,
            "next_lane": next_lane,
        },
        "recovery": {
            "lane": recovery_lane,
            "status": recovery_status,
            "last_cycle_at": recovery_at,
        },
    }
    if placement is not None:
        result["placement"] = placement
    return result


def _sidebar_placement_status(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    inbox_cwd = value.get("inbox_cwd")
    if not is_canonical_sidebar_string(inbox_cwd):
        return None
    generation = _nonnegative_status_int(value.get("generation"), 0)
    if generation < 1:
        return None
    canary_source = _status_mapping(value.get("canary"))
    canary_status = canary_source.get("status")
    verified_at = _finite_status_number(canary_source.get("verified_at"))
    if canary_status == "not_run" and canary_source.get("verified_at") is None:
        verified_at = None
    elif canary_status not in {"passed", "failed"} or verified_at is None:
        canary_status = "not_run"
        verified_at = None
    return {
        "inbox_cwd": inbox_cwd,
        "generation": generation,
        "verified_visible": _nonnegative_status_int(
            value.get("verified_visible"),
            0,
        ),
        "mismatch_count": _nonnegative_status_int(
            value.get("mismatch_count"),
            0,
        ),
        "canary": {
            "status": canary_status,
            "verified_at": verified_at,
        },
    }


def _hydration_status(value: object, *, enabled: bool) -> dict[str, Any]:
    source = _status_mapping(value)
    raw_counts = _status_mapping(source.get("counts"))
    health_counts = _status_mapping(source.get("health_counts"))
    recent = source.get("recent_error_codes")
    return {
        "enabled": enabled is True,
        "counts": {
            "pending": _nonnegative_status_int(
                health_counts.get(
                    "pending", raw_counts.get(SidebarHydrationState.PENDING.value)
                ),
                0,
            ),
            "leased": _nonnegative_status_int(
                health_counts.get(
                    "leased", raw_counts.get(SidebarHydrationState.LEASED.value)
                ),
                0,
            ),
            "retry": _nonnegative_status_int(
                health_counts.get(
                    "retry", raw_counts.get(SidebarHydrationState.RETRY.value)
                ),
                0,
            ),
            "committed": _nonnegative_status_int(
                health_counts.get(
                    "committed", raw_counts.get(SidebarHydrationState.VISIBLE.value)
                ),
                0,
            ),
            "ambiguous": _nonnegative_status_int(health_counts.get("ambiguous"), 0),
            "failed": _nonnegative_status_int(
                health_counts.get(
                    "failed", raw_counts.get(SidebarHydrationState.FAILED.value)
                ),
                0,
            ),
        },
        "oldest_pending_age_seconds": _finite_status_number(
            source.get("oldest_pending_age_seconds")
        ),
        "active_lease": source.get("active_lease") is True,
        "reserved_reconciliation": _nonnegative_status_int(
            source.get("reserved_reconciliation"),
            0,
        ),
        "recent_error_codes": (
            [
                code
                for code in _fixed_status_codes(list(recent))
                if code in HYDRATION_RETRYABLE_ERRORS | HYDRATION_FATAL_ERRORS
            ]
            if isinstance(recent, (list, tuple))
            else []
        ),
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


def _exact_sidebar_sha256(value: object, label: str) -> str:
    digest = _exact_sidebar_text(value, label)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"sidebar {label} is malformed")
    return digest


def _sidebar_delivery_claim_tokens(
    claims: object,
    *,
    limit: int,
) -> tuple[list[str], bool]:
    """Extract recoverable tokens without consuming arbitrary iterables."""

    if not isinstance(claims, (list, tuple)):
        return [], True
    malformed = not isinstance(claims, tuple) or len(claims) > limit
    tokens: list[str] = []
    for claim in claims:
        try:
            tokens.append(
                _exact_sidebar_text(
                    getattr(claim, "lease_token", None),
                    "lease token",
                )
            )
        except ValueError:
            malformed = True
    if len(set(tokens)) != len(tokens):
        malformed = True
    return tokens, malformed


def _build_sidebar_broker_job(
    store: SessionBridgeStore,
    claim: object,
    marker_key: bytes,
    preview_budget_chars: int = 24_000,
) -> dict[str, Any]:
    lease_token = _exact_sidebar_text(
        getattr(claim, "lease_token", None), "lease token"
    )
    source_session_id = _exact_sidebar_text(
        getattr(claim, "source_session_id", None), "source session ID"
    )
    bridge_id = _exact_sidebar_text(getattr(claim, "bridge_id", None), "bridge ID")
    reconciliation_state = getattr(claim, "reconciliation_state", None)
    reconciliation_generation = _exact_sidebar_text(
        getattr(claim, "reconciliation_generation", None),
        "reconciliation generation",
    )
    reconciliation_proof_digest = _exact_sidebar_text(
        getattr(claim, "reconciliation_proof_digest", None),
        "reconciliation proof digest",
    )
    if re.fullmatch(r"[0-9a-f]{64}", reconciliation_proof_digest) is None:
        raise ValueError("sidebar reconciliation proof digest is malformed")
    recovered_thread_id_value = getattr(claim, "recovered_thread_id", None)
    recovered_thread_id = (
        None
        if recovered_thread_id_value is None
        else _exact_sidebar_text(
            recovered_thread_id_value,
            "recovered thread ID",
        )
    )
    create_eligible = getattr(claim, "create_eligible", None)
    rename_required = getattr(claim, "rename_required", None)
    create_reserved = getattr(claim, "create_reserved", None)
    if (
        type(create_eligible) is not bool
        or type(rename_required) is not bool
        or type(create_reserved) is not bool
    ):
        raise ValueError("sidebar claim flags are malformed")
    if reconciliation_state is SidebarReconciliationState.RECOVERED:
        if recovered_thread_id is None or create_eligible:
            raise ValueError("recovered sidebar reconciliation shape is malformed")
    elif reconciliation_state is SidebarReconciliationState.ABSENCE_PROVEN:
        if recovered_thread_id is not None:
            raise ValueError("absent sidebar reconciliation shape is malformed")
    else:
        raise ValueError("sidebar reconciliation state is not broker deliverable")
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
    snapshot = store.get_sidebar_preview_source(source_session_id)
    if (
        snapshot.get("source_session_id") != source_session_id
        or snapshot.get("provider") != candidate.provider.value
    ):
        raise ValueError("sidebar source preview identity is malformed")
    preview = build_session_preview(
        source_session_id=source_session_id,
        source_cursor=_exact_sidebar_text(
            snapshot.get("source_cursor"),
            "preview source cursor",
        ),
        source_hash=_exact_sidebar_text(
            snapshot.get("source_hash"),
            "preview source hash",
        ),
        title=cast(str | None, snapshot["title"]),
        provider=candidate.provider.value,
        cwd=candidate.cwd,
        captured_at=snapshot.get("captured_at"),
        messages=cast(list[Mapping[str, Any]], snapshot.get("messages")),
        git_root=candidate.git_root,
        git_branch=candidate.git_branch,
        git_head=candidate.git_head,
        worktree_id=candidate.worktree_id,
        budget_chars=preview_budget_chars,
    )
    job = {
        "lease_token": lease_token,
        "registration_prompt": build_registration_prompt(
            candidate,
            marker,
            preview=preview,
        ),
        "title": candidate.title,
        "provider": candidate.provider.value,
        "cwd": candidate.cwd,
        "git_root": candidate.git_root,
        "git_branch": candidate.git_branch,
        "git_head": candidate.git_head,
        "worktree_id": candidate.worktree_id,
        "rename_required": rename_required,
    }
    job.update({
        "reconciliation_state": reconciliation_state.value,
        "reconciliation_generation": reconciliation_generation,
        "reconciliation_proof_digest": reconciliation_proof_digest,
        "recovered_thread_id": recovered_thread_id,
        "create_eligible": create_eligible,
        "create_reserved": create_reserved,
    })
    return job


def _build_sidebar_hydration_broker_job(
    claim: object,
    marker_key: bytes,
) -> dict[str, Any]:
    lease_token = _exact_sidebar_text(
        getattr(claim, "lease_token", None),
        "hydration lease token",
    )
    source_session_id = _exact_sidebar_text(
        getattr(claim, "source_session_id", None),
        "hydration source session ID",
    )
    bridge_id = _exact_sidebar_text(
        getattr(claim, "bridge_id", None),
        "hydration bridge ID",
    )
    thread_id = _exact_sidebar_text(
        getattr(claim, "codex_thread_id", None),
        "hydration Codex thread ID",
    )
    source_cursor = _exact_sidebar_text(
        getattr(claim, "source_cursor", None),
        "hydration source cursor",
    )
    source_hash = _exact_sidebar_text(
        getattr(claim, "source_hash", None),
        "hydration source hash",
    )
    preview_digest = _exact_sidebar_text(
        getattr(claim, "preview_digest", None),
        "hydration preview digest",
    )
    preview_version = getattr(claim, "preview_version", None)
    if type(preview_version) is not int or preview_version != 1:
        raise ValueError("hydration preview version is malformed")
    marker = _exact_sidebar_text(
        getattr(claim, "hydration_marker", None),
        "hydration marker",
    )
    decoded = decode_hydration_marker(marker, marker_key)
    if decoded != HydrationMarkerPayload(
        bridge_id=bridge_id,
        codex_thread_id=thread_id,
        preview_digest=preview_digest,
        preview_version=preview_version,
        source_cursor=source_cursor,
        source_hash=source_hash,
        source_session_id=source_session_id,
    ):
        raise ValueError("hydration marker identity mismatch")
    message = getattr(claim, "hydration_message", None)
    if (
        not isinstance(message, str)
        or not message.startswith("# ")
        or message.count(marker) != 1
    ):
        raise ValueError("hydration message is malformed")
    cwd = _exact_sidebar_text(getattr(claim, "cwd", None), "hydration cwd")
    raw_git_root = getattr(claim, "git_root", None)
    git_root = (
        None
        if raw_git_root is None
        else _exact_sidebar_text(raw_git_root, "hydration git root")
    )
    send_reserved = getattr(claim, "send_reserved", None)
    if type(send_reserved) is not bool:
        raise ValueError("hydration send reservation flag is malformed")
    return {
        "lease_token": lease_token,
        "codex_thread_id": thread_id,
        "hydration_message": message,
        "hydration_marker": marker,
        "cwd": cwd,
        "git_root": git_root,
        "send_reserved": send_reserved,
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
