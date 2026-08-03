"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless; opt-in session continuity via X-Hermes-Session-Id header; opt-in long-term memory scoping via X-Hermes-Session-Key header)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id; X-Hermes-Session-Key supported)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists hermes-agent and any configured model_routes aliases
- GET  /v1/capabilities            — machine-readable API capabilities for external UIs
- GET  /api/sessions               — list client-visible Hermes sessions
- POST /api/sessions               — create an empty Hermes session
- GET/PATCH/DELETE /api/sessions/{session_id} — read/update/delete a session
- GET  /api/sessions/{session_id}/messages — read session message history
- POST /api/sessions/{session_id}/fork — branch a session using SessionDB lineage
- POST /api/sessions/{session_id}/chat[/stream] — chat with a persisted session
- POST /v1/runs                    — start a run, returns run_id immediately (202)
- GET  /v1/runs/{run_id}           — retrieve current run status
- GET  /v1/runs/{run_id}/events    — SSE stream of structured lifecycle events
- POST /v1/runs/{run_id}/approval — resolve a pending run approval
- POST /v1/runs/{run_id}/stop       — interrupt a running agent
- GET  /health                     — health check
- GET  /health/detailed            — rich status for cross-container dashboard probing

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to hermes-agent
through this adapter by pointing at http://localhost:8642/v1 and
authenticating with API_SERVER_KEY.

One API-server listener serves exactly one process-frozen profile. Legacy
same-process multiplex configuration fails closed before the listener starts;
profile URL prefixes are not registered.

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import contextvars
import errno
import hashlib
import hmac
import inspect
import ipaddress
import itertools
import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import logging
import os
import re
import secrets
import socket as _socket
import sqlite3
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Sentinel returned by _resolve_request_profile when a forbidden profile
# prefix is supplied. Distinct from None (native single-profile route).
_PROFILE_REJECTED = object()

# Legacy request-profile context retained for handler compatibility. The
# single-profile middleware always binds None.
_api_request_profile: ContextVar[Optional[str]] = ContextVar(
    "api_server_request_profile", default=None
)
# Immutable host-owned tenant authority captured by the profile middleware.
# Background owners copy/pass this value explicitly; raw public IDs never
# become process-local cache/control keys on the listener.
_api_request_authority: ContextVar[Optional["APIRequestScope"]] = ContextVar(
    "api_server_request_authority",
    default=None,
)

async def _await_if_needed(value: Any) -> Any:
    """Await production coroutines while preserving synchronous test seams."""
    if inspect.isawaitable(value):
        return await value
    return value


try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from agent.terminal_outcome import (
    TerminalOutcomeKind,
    normalize_terminal_outcome,
)
from gateway.api_request_scope import (
    APIProfileGenerationError,
    APIProfileIdentity,
    APIRequestScope,
    APIRequestScopeError,
    capture_api_profile_identity,
    freeze_api_profile_inventory,
    resolve_api_request_scope,
    validate_api_profile_inventory,
    verify_api_profile_identity,
    verify_api_request_scope,
)
from gateway.platforms.base import (
    MEDIA_TAG_CLEANUP_RE,
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
    validate_media_delivery_path,
)
from agent.redact import redact_sensitive_text
from agent.interrupt_compat import request_hard_interrupt
from gateway.readiness import collect_runtime_readiness
from gateway import systemd_credentials as systemd_credentials_module
from gateway.systemd_credentials import (
    GATEWAY_API_APPROVAL_CREDENTIAL,
    GATEWAY_API_APPROVAL_VERIFIER_CREDENTIAL,
    GATEWAY_API_BEARER_CREDENTIAL,
    GATEWAY_API_BEARER_VERIFIER_CREDENTIAL,
    GATEWAY_API_UNIT,
    SystemdCredentialError,
    read_systemd_credential,
)
from gateway.api_verifier_credentials import (
    APIApprovalScryptVerifier,
    APIBearerVerifier,
    APIVerifierCredentialError,
    api_approval_passkey_matches,
    api_bearer_matches,
    parse_api_approval_scrypt_verifier,
    parse_api_bearer_verifier,
)

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Secondary profiles construct their adapters under a profile secret
    scope -- the scope is authoritative and a scoped miss returns ``default``
    (no cross-profile borrow from ``os.environ``, which may hold another
    profile's value). The DEFAULT profile's adapter constructs and sends
    *unscoped* under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash this path; there ``os.environ`` is that
    profile's own value, so fall back to it. Same pattern as the Slack
    ``SLACK_APP_TOKEN`` read (#59739) and
    ``gateway/platforms/whatsapp_common.py::_get_wsecret``.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


logger = logging.getLogger(__name__)


def _hermes_version() -> str:
    """Return the canonical Hermes Agent version string.

    ``hermes_cli.__version__`` is the runtime source of truth used by the CLI,
    dashboard, portal tags, and release script. Prefer it over installed
    distribution metadata because editable/source checkouts can retain stale
    ``hermes_agent-*.dist-info`` after a source update until the environment is
    reinstalled. Never raises — a version probe must not be able to break the
    health endpoint.
    """
    try:
        from hermes_cli import __version__

        return __version__
    except Exception:
        pass
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        return "dev"


# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 10_000_000  # 10 MB — accommodates long agent conversations with tool calls
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
API_CLEANUP_SHIELD_TIMEOUT_SECONDS = 5.0
API_CLEANUP_RETRY_BASE_SECONDS = 0.25
API_CLEANUP_RETRY_MAX_SECONDS = 30.0
API_AGENT_CACHE_MAX_SIZE = 128
API_AGENT_CACHE_IDLE_TTL_SECONDS = 3600.0
API_AGENT_SESSION_LOCK_STRIPES = 64
API_CLARIFY_RESPONSE_MAX_LENGTH = 65_536
API_APPROVAL_CHOICES = ("once", "session", "always", "deny")
API_APPROVAL_AUTHORITY_SCHEMA = "hermes.api.approval-owner-authority.v1"
API_RUN_ADMISSION_SCHEMA = "hermes.api.run-admission.v1"
API_MODEL_RELEASE_SCHEMA = "hermes.api.model-release.v1"
RunAdmissionCallback = Callable[[str, str], Mapping[str, Any]]
API_APPROVAL_PASSKEY_AUTHORITY_SCHEMA = (
    "hermes.api.approval-owner-passkey.v1"
)
API_APPROVAL_AUTHORITY_MAX_TTL_SECONDS = 300
API_APPROVAL_AUTHORITY_CLOCK_SKEW_SECONDS = 30
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts
MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array
RESPONSES_AUTO_TRUNCATION_HISTORY_LIMIT = 100
_COMPRESSED_SUMMARY_METADATA_KEY = "_compressed_summary"
MAX_SYSTEMD_CREDENTIAL_BYTES = 8_192
MAX_API_SESSION_ID_LENGTH = 256


class _InvalidInternalAPISessionID(ValueError):
    """An agent/store supplied a session ID outside the public ID contract."""


def _canonical_api_session_id(value: object, *, required: bool) -> str:
    """Return one path/header-safe API session ID or raise ``ValueError``.

    Public request IDs and trusted-looking internal IDs deliberately share
    this exact syntax boundary.  Internal provenance is not a reason to let a
    malformed value reach a header, persistence key, cache namespace, or
    response-chain lookup.
    """

    if value is None or value == "":
        if not required:
            return ""
        raise ValueError("API session ID is required")
    if not isinstance(value, str):
        raise ValueError("API session ID must be a string")
    session_id = value.strip()
    from gateway.session import _is_path_unsafe

    if (
        session_id != value
        or not session_id
        or len(session_id) > MAX_API_SESSION_ID_LENGTH
        or re.search(r"[\r\n\x00]", session_id)
        or _is_path_unsafe(session_id)
    ):
        raise ValueError("Invalid API session ID")
    return session_id


def _validate_internal_api_session_id(
    value: object,
    *,
    source: str,
) -> str:
    """Fail closed on malformed agent/store continuity state.

    The exception contains no attacker-controlled value, so callers may safely
    translate it to a controlled 500/SSE failure without leaking the rejected
    identifier.
    """

    try:
        return _canonical_api_session_id(value, required=True)
    except ValueError as exc:
        logger.error("Rejected malformed internal API session ID from %s", source)
        raise _InvalidInternalAPISessionID(
            "Internal API session continuity state is invalid."
        ) from exc


def _effective_internal_api_session_id(
    result: object,
    *,
    fallback: str,
    source: str,
) -> str:
    """Validate a result session ID, or the already-authoritative fallback."""

    if isinstance(result, Mapping) and "session_id" in result:
        value = result["session_id"]
        value_source = f"{source}.session_id"
    else:
        value = fallback
        value_source = f"{source}.fallback"
    return _validate_internal_api_session_id(value, source=value_source)

def _effective_uid_for_systemd_credential() -> int:
    """Return the POSIX effective UID or reject this Linux-only boundary."""

    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise ValueError("api_server systemd credentials require POSIX UID support")
    return int(getter())

class _APIServerSessionBinding(list):
    """Context reset tokens plus the exact gateway-generated run epoch."""

    __slots__ = ("capability_epoch_sha256",)

    def __init__(self, tokens: list, capability_epoch_sha256: str) -> None:
        super().__init__(tokens)
        self.capability_epoch_sha256 = capability_epoch_sha256


class _APIClarifyAuthority:
    """Exact, turn-owned authority for API clarification callbacks.

    A callback closure keeps this object, not a reusable per-session counter.
    Cleanup first fences ``accepting`` while the worker can still run, then
    retires its exact core generation only at the worker lifecycle boundary.
    """

    __slots__ = (
        "accepting",
        "active",
        "generation",
        "retired",
        "scope",
    )

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.generation: Optional[int] = None
        self.accepting = True
        self.active = True
        self.retired = False

class _APIServerCleanupHandle:
    """Private exact-binding handle for fail-closed cleanup retries.

    The raw session key and copied trusted Context never cross an API response,
    log record, status payload, or callback.  They exist only so a bounded
    background retry can re-run the idempotent Canonical tombstone against the
    *same* server-authored epoch after the request executor has unwound.
    """

    __slots__ = (
        "_session_key",
        "_trusted_context",
        "attempts",
        "capability_epoch_sha256",
        "cleanup_id",
        "durable_revoke_succeeded",
        "last_error",
        "local_clear_succeeded",
        "model_release_receipt",
        "receipt",
        "session_key_sha256",
        "status",
    )

    def __init__(
        self,
        session_key: str,
        capability_epoch_sha256: str,
        trusted_context: contextvars.Context,
    ) -> None:
        self._session_key = session_key
        self._trusted_context: Optional[contextvars.Context] = trusted_context
        self.session_key_sha256 = hashlib.sha256(session_key.encode()).hexdigest()
        self.capability_epoch_sha256 = capability_epoch_sha256
        self.cleanup_id = hashlib.sha256(
            (
                "api-server-cleanup:"
                + self.session_key_sha256
                + ":"
                + capability_epoch_sha256
            ).encode()
        ).hexdigest()
        self.attempts = 0
        self.durable_revoke_succeeded = False
        self.local_clear_succeeded = False
        self.last_error = ""
        self.model_release_receipt: Dict[str, Any] = {}
        self.receipt: Dict[str, Any] = {}
        self.status = "pending"

    def safe_state(self) -> Dict[str, Any]:
        """Return the complete public state without retry authority."""
        receipt = self.receipt
        state: Dict[str, Any] = {
            "cleanup_id": self.cleanup_id,
            "status": self.status,
            "authority_created": True,
            "session_key_sha256": self.session_key_sha256,
            "capability_epoch_sha256": self.capability_epoch_sha256,
            "attempts": self.attempts,
            "durable_revoke_succeeded": self.durable_revoke_succeeded,
            "local_clear_succeeded": self.local_clear_succeeded,
            "authority_active": (
                receipt.get("authority_active")
                if self.durable_revoke_succeeded
                else None
            ),
            "revocation_event_id": receipt.get("revocation_event_id"),
            "inserted": receipt.get("inserted"),
            "deduped": receipt.get("deduped"),
            "writer_required": receipt.get("writer_required"),
        }
        if self.model_release_receipt:
            state["model_release_receipt"] = dict(self.model_release_receipt)
        if self.last_error:
            state["error"] = self.last_error
        return state

    def zeroize_retry_authority(self) -> None:
        """Drop the only raw values capable of retrying the old binding."""
        self._session_key = ""
        self._trusted_context = None

    def __repr__(self) -> str:
        return (
            "<_APIServerCleanupHandle "
            f"cleanup_id={self.cleanup_id} status={self.status}>"
        )

class _APIServerRunReservation:
    """Event-loop-owned, one-shot admission reservation."""

    __slots__ = ("_adapter", "_counted", "_released")

    def __init__(self, adapter: "APIServerAdapter", *, counted: bool) -> None:
        self._adapter = adapter
        self._counted = counted
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._counted:
            self._adapter._agent_run_reservations = max(
                0,
                self._adapter._agent_run_reservations - 1,
            )

def _load_systemd_api_credential(name: Any, *, reviewed_name: str) -> str:
    """Read one purpose-bound gateway credential through the shared boundary."""

    if name != reviewed_name:
        raise ValueError(
            "api_server credential name does not match its reviewed purpose"
        )
    directory = (
        Path(systemd_credentials_module.SYSTEMD_CREDENTIAL_ROOT)
        / GATEWAY_API_UNIT
    )
    raw_directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if raw_directory != str(directory):
        raise ValueError(
            "api_server credential requires the exact gateway "
            "CREDENTIALS_DIRECTORY binding"
        )
    credential_path = directory / reviewed_name
    effective_uid = _effective_uid_for_systemd_credential()
    try:
        raw = read_systemd_credential(
            credential_path,
            unit=GATEWAY_API_UNIT,
            name=reviewed_name,
            service_uid=effective_uid,
            maximum=MAX_SYSTEMD_CREDENTIAL_BYTES,
            credentials_directory=directory,
        )
    except SystemdCredentialError as exc:
        raise ValueError(
            f"api_server systemd credential rejected: {exc.code}"
        ) from exc

    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("api_server systemd credential is not UTF-8") from exc
    if (
        not value
        or value != value.strip()
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ValueError("api_server systemd credential value is malformed")
    return value

def _load_systemd_api_key_credential(name: Any) -> str:
    """Load only the reviewed API control bearer credential."""

    return _load_systemd_api_credential(
        name,
        reviewed_name=GATEWAY_API_BEARER_CREDENTIAL,
    )

def _load_systemd_api_approval_credential(name: Any) -> str:
    """Load only the reviewed owner-approval passkey credential."""

    return _load_systemd_api_credential(
        name,
        reviewed_name=GATEWAY_API_APPROVAL_CREDENTIAL,
    )

def _load_systemd_api_bearer_verifier_credential(name: Any) -> str:
    """Load only the reviewed non-secret API bearer verifier."""

    return _load_systemd_api_credential(
        name,
        reviewed_name=GATEWAY_API_BEARER_VERIFIER_CREDENTIAL,
    )

def _load_systemd_api_approval_verifier_credential(name: Any) -> str:
    """Load only the reviewed non-secret owner-passkey verifier."""

    return _load_systemd_api_credential(
        name,
        reviewed_name=GATEWAY_API_APPROVAL_VERIFIER_CREDENTIAL,
    )

def _resolve_api_server_key(extra: Dict[str, Any]) -> str:
    """Resolve legacy inline/env auth or the mutually-exclusive credential seam."""
    credential_name = extra.get("key_credential")
    env_key = _get_scoped_secret("API_SERVER_KEY", "")
    if extra.get("key_verifier_credential") is not None:
        if credential_name is not None or "key" in extra or env_key:
            raise ValueError(
                "api_server key verifier cannot be combined with secret-bearing auth"
            )
        return ""
    if credential_name is None:
        return extra.get("key", env_key)
    if "key" in extra or env_key:
        raise ValueError(
            "api_server key_credential cannot be combined with inline or env key"
        )
    return _load_systemd_api_key_credential(credential_name)

def _resolve_api_bearer_verifier(extra: Dict[str, Any]) -> APIBearerVerifier | None:
    credential_name = extra.get("key_verifier_credential")
    if credential_name is None:
        return None
    if credential_name != GATEWAY_API_BEARER_VERIFIER_CREDENTIAL:
        raise ValueError("api_server bearer verifier credential is not reviewed")
    try:
        return parse_api_bearer_verifier(
            _load_systemd_api_bearer_verifier_credential(credential_name)
        )
    except APIVerifierCredentialError as exc:
        raise ValueError("api_server bearer verifier is malformed") from exc

def _resolve_api_approval_passkey(extra: Dict[str, Any]) -> str:
    """Resolve the distinct secret used to sign positive approval authority.

    The normal API bearer authenticates a control-plane client but is not
    owner consent.  A separate passkey signs a short-lived, exact approval
    nonce.  As with the API bearer, production may load the secret from a
    systemd credential without placing it in config or process arguments.
    """

    credential_name = extra.get("approval_passkey_credential")
    if extra.get("approval_verifier_credential") is not None:
        if (
            credential_name is not None
            or "approval_passkey" in extra
            or os.getenv("API_SERVER_APPROVAL_PASSKEY")
        ):
            raise ValueError(
                "api_server approval verifier cannot be combined with a passkey secret"
            )
        return ""
    env_value = os.getenv("API_SERVER_APPROVAL_PASSKEY", "")
    inline_present = "approval_passkey" in extra
    if credential_name is not None:
        if inline_present or env_value:
            raise ValueError(
                "api_server approval_passkey_credential cannot be combined "
                "with inline or env approval passkey"
            )
        value = _load_systemd_api_approval_credential(credential_name)
    else:
        value = extra.get("approval_passkey", env_value)

    if value is None or value == "":
        return ""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value.encode("utf-8")) < 32
        or len(value.encode("utf-8")) > MAX_SYSTEMD_CREDENTIAL_BYTES
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ValueError(
            "api_server approval passkey must be a bounded 32+ byte secret"
        )
    return value

def _resolve_api_approval_verifier(
    extra: Dict[str, Any],
) -> APIApprovalScryptVerifier | None:
    credential_name = extra.get("approval_verifier_credential")
    if credential_name is None:
        return None
    if credential_name != GATEWAY_API_APPROVAL_VERIFIER_CREDENTIAL:
        raise ValueError("api_server approval verifier credential is not reviewed")
    try:
        return parse_api_approval_scrypt_verifier(
            _load_systemd_api_approval_verifier_credential(credential_name)
        )
    except APIVerifierCredentialError as exc:
        raise ValueError("api_server approval verifier is malformed") from exc

def _session_stream_outcome(result: Any) -> Dict[str, Any]:
    """Mechanically normalize an agent result for every API response surface.

    A legacy mapping that omits ``completed`` remains a success when it carries
    no contrary flags.  Every other non-complete outcome is explicit and can
    never acquire a successful terminal event or ``finish_reason=stop``.
    """
    outcome = normalize_terminal_outcome(result)
    result_mapping = result if isinstance(result, Mapping) else {}
    exit_reason = result_mapping.get("turn_exit_reason")
    if not isinstance(exit_reason, str) or not exit_reason:
        exit_reason = (
            "invalid_agent_result"
            if not outcome.valid
            else (outcome.reason or outcome.kind.value)
        )

    if outcome.kind is TerminalOutcomeKind.FAILED:
        status = "failed"
        assistant_event = "assistant.failed"
        run_event = "run.failed"
    elif outcome.kind is TerminalOutcomeKind.INTERRUPTED:
        status = "interrupted"
        assistant_event = "assistant.partial"
        run_event = "run.partial"
    elif outcome.kind is TerminalOutcomeKind.PARTIAL:
        raw_status = str(result_mapping.get("status") or "").strip().lower()
        status = (
            "partial"
            if result_mapping.get("partial") is True
            or raw_status == "partial"
            else "incomplete"
        )
        assistant_event = "assistant.partial"
        run_event = "run.partial"
    else:
        status = "completed"
        assistant_event = "assistant.completed"
        run_event = "run.completed"

    if outcome.completed:
        finish_reason = "stop"
    elif (
        outcome.partial
        and (
            result_mapping.get("outcome_code") == "output_truncated"
            or exit_reason == "text_response(finish_reason=length)"
        )
    ):
        # Only exact structured codes may map to OpenAI's truncation finish.
        # Free-form response/error text is never interpreted at this boundary.
        finish_reason = "length"
    else:
        finish_reason = "error"

    return {
        "status": status,
        "assistant_event": assistant_event,
        "run_event": run_event,
        "completed": outcome.completed,
        "partial": outcome.partial,
        "interrupted": outcome.interrupted,
        "failed": outcome.failed,
        "incomplete": outcome.incomplete,
        "finish_reason": finish_reason,
        "turn_exit_reason": exit_reason,
        "terminal_outcome_contradictory": outcome.contradictory,
    }


def _terminal_failure_outcome_fields(
    turn_exit_reason: str,
) -> Dict[str, Any]:
    """Return the shared, exclusive terminal flags for one failed turn."""

    return {
        "completed": False,
        "partial": False,
        "interrupted": False,
        "failed": True,
        "incomplete": True,
        "turn_exit_reason": turn_exit_reason,
    }


_DURABLE_WAKE_RESPONSE_SCHEMA = "hermes.api-durable-wake-response.v1"
_DURABLE_WAKE_RESPONSE_HEADERS = frozenset(
    {
        "X-Hermes-Session-Id",
        "X-Hermes-Session-Key",
        "X-Hermes-Completed",
        "X-Hermes-Partial",
        "X-Hermes-Error",
    }
)


def _canonical_json_http_response(
    body: Dict[str, Any],
    *,
    status: int,
    headers: Optional[Dict[str, str]] = None,
) -> "web.Response":
    """Serialize strict JSON before a durable owner commits it."""

    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return web.Response(
        body=encoded,
        status=status,
        headers=dict(headers or {}),
        content_type="application/json",
    )


def _durable_wake_response_record(
    body: Dict[str, Any],
    *,
    status: int,
    terminal_status: Optional[int] = None,
    headers: Dict[str, str],
) -> Dict[str, Any]:
    """Build the bounded canonical value committed by the durable CAS."""

    if status < 200 or status >= 300:
        raise ValueError("only successful durable wake responses may commit")
    safe_headers: Dict[str, str] = {}
    for key, value in headers.items():
        if key not in _DURABLE_WAKE_RESPONSE_HEADERS:
            raise ValueError(f"unsupported durable wake response header: {key}")
        if not isinstance(value, str):
            raise ValueError("durable wake response headers must be strings")
        safe_headers[key] = value
    record = {
        "schema": _DURABLE_WAKE_RESPONSE_SCHEMA,
        "status": status,
        "terminal_status": (
            status if terminal_status is None else terminal_status
        ),
        "headers": safe_headers,
        "body": body,
    }
    # Prove serializability before the owner CAS.  The persistence helper
    # repeats this check at the storage boundary.
    json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return record


def _durable_wake_replay_response(record: object) -> "web.Response":
    """Reconstruct the exact canonical HTTP result of a completed wake."""

    if not isinstance(record, dict) or set(record) != {
        "schema",
        "status",
        "terminal_status",
        "headers",
        "body",
    }:
        raise ValueError("durable wake response record is malformed")
    if record.get("schema") != _DURABLE_WAKE_RESPONSE_SCHEMA:
        raise ValueError("durable wake response schema is unsupported")
    status = record.get("status")
    terminal_status = record.get("terminal_status")
    headers = record.get("headers")
    body = record.get("body")
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or status < 200
        or status >= 300
        or not isinstance(terminal_status, int)
        or isinstance(terminal_status, bool)
        or terminal_status < 100
        or terminal_status > 599
        or not isinstance(headers, dict)
        or not isinstance(body, dict)
    ):
        raise ValueError("durable wake response record is malformed")
    if any(
        key not in _DURABLE_WAKE_RESPONSE_HEADERS
        or not isinstance(value, str)
        for key, value in headers.items()
    ):
        raise ValueError("durable wake response headers are malformed")
    return _canonical_json_http_response(
        body,
        status=status,
        headers=headers,
    )


def _durable_wake_uncertain_response(
    *,
    session_id: str,
    reason: str = "",
) -> "web.Response":
    """Return a terminal 2xx settlement without invoking a model or tools."""

    logger.error(
        "Durable API wake settled as uncertain for session %s: %s",
        session_id,
        _redact_api_error_text(
            reason or "outcome may include effects",
            limit=300,
        ),
    )
    body = {
        "object": "hermes.durable_wake",
        "status": "partial",
        "completed": False,
        "partial": True,
        "interrupted": False,
        "failed": False,
        "incomplete": True,
        "turn_exit_reason": "durable_wake_uncertain",
        "error_code": "durable_wake_outcome_uncertain",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    return _canonical_json_http_response(
        body,
        status=200,
        headers={
            "X-Hermes-Session-Id": session_id,
            "X-Hermes-Completed": "false",
            "X-Hermes-Partial": "true",
        },
    )


def _durable_wake_in_progress_response(reason: str = "") -> "web.Response":
    """Ask the internal self-poster to retry while another owner is live."""

    body = _openai_error(
        "Durable wake execution is already in progress.",
        err_type="server_error",
        code="durable_wake_in_progress",
    )
    if reason:
        body["error"]["hermes"] = {"status": "in_progress"}
    return _canonical_json_http_response(
        body,
        status=429,
        headers={"Retry-After": "1"},
    )


def _durable_wake_deferred_response(
    *,
    message: str,
    code: str,
) -> "web.Response":
    """Return a non-ACK response for a safely retryable durable carrier."""

    return _canonical_json_http_response(
        _openai_error(
            message,
            err_type="server_error",
            code=code,
        ),
        status=429,
        headers={"Retry-After": "1"},
    )


def _abandon_durable_wake_execution(
    execution: Dict[str, Any],
    *,
    reason: str,
) -> bool:
    """Best-effort owner-CAS to uncertainty without masking the cause."""

    from tools.async_delegation import abandon_durable_wake_execution

    try:
        return abandon_durable_wake_execution(
            delegation_id=execution["delegation_id"],
            idempotency_key=execution["idempotency_key"],
            claim_id=execution["claim_id"],
            reason=reason,
            store=execution["store"],
        )
    except BaseException:
        logger.exception(
            "Failed to abandon durable API wake claim %s",
            execution.get("claim_id", ""),
        )
        return False


def _settle_durable_wake_uncertainty(
    execution: Dict[str, Any],
    *,
    session_id: str,
    disposition_reason: str,
    response_reason: str,
) -> "web.Response":
    """ACK uncertainty only after its owner-CAS is durably observable."""

    if _abandon_durable_wake_execution(
        execution,
        reason=disposition_reason,
    ):
        return _durable_wake_uncertain_response(
            session_id=session_id,
            reason=response_reason,
        )
    return _durable_wake_deferred_response(
        message=(
            "Durable wake terminal settlement is temporarily unavailable."
        ),
        code="durable_wake_settlement_unavailable",
    )


def _chat_completion_http_parts(
    *,
    result: Any,
    usage: Mapping[str, Any],
    completion_id: str,
    model_name: str,
    created: int,
    session_id: str,
    gateway_session_key: str,
) -> tuple[Dict[str, Any], int, Dict[str, str]]:
    """Build one non-streaming chat-completion response without I/O."""

    outcome = _session_stream_outcome(result)
    result_mapping = result if isinstance(result, Mapping) else {}
    final_response = _resolve_media_to_data_urls(
        result_mapping.get("final_response") or ""
    )
    raw_err_msg = result_mapping.get("error")
    err_msg = (
        _redact_api_error_text(raw_err_msg)
        if raw_err_msg
        else raw_err_msg
    )
    finish_reason = outcome["finish_reason"]

    try:
        effective_session_id = _effective_internal_api_session_id(
            result_mapping,
            fallback=session_id,
            source="chat_completion_result",
        )
    except _InvalidInternalAPISessionID:
        return _invalid_internal_session_id_error(), 500, {}
    response_headers = {
        "X-Hermes-Session-Id": effective_session_id,
    }
    if gateway_session_key:
        response_headers["X-Hermes-Session-Key"] = gateway_session_key

    if not final_response and outcome["incomplete"]:
        err_body = _openai_error(
            err_msg or "Agent run did not produce a response.",
            err_type="server_error",
            code="agent_incomplete",
        )
        err_body["error"]["hermes"] = {
            "status": outcome["status"],
            "completed": outcome["completed"],
            "partial": outcome["partial"],
            "interrupted": outcome["interrupted"],
            "failed": outcome["failed"],
            "incomplete": outcome["incomplete"],
            "turn_exit_reason": outcome["turn_exit_reason"],
            "terminal_outcome_contradictory": outcome[
                "terminal_outcome_contradictory"
            ],
        }
        err_body["usage"] = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        response_headers["X-Hermes-Completed"] = "false"
        response_headers["X-Hermes-Partial"] = (
            "true" if outcome["partial"] else "false"
        )
        return err_body, 502, response_headers

    response_data = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": final_response,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }
    if outcome["incomplete"]:
        response_data["hermes"] = {
            "status": outcome["status"],
            "completed": outcome["completed"],
            "partial": outcome["partial"],
            "interrupted": outcome["interrupted"],
            "failed": outcome["failed"],
            "incomplete": outcome["incomplete"],
            "turn_exit_reason": outcome["turn_exit_reason"],
            "terminal_outcome_contradictory": outcome[
                "terminal_outcome_contradictory"
            ],
            "error": err_msg,
            "error_code": (
                "output_truncated"
                if finish_reason == "length"
                else "agent_error"
            ),
        }
        response_headers["X-Hermes-Completed"] = "false"
        response_headers["X-Hermes-Partial"] = (
            "true" if outcome["partial"] else "false"
        )
        if err_msg:
            response_headers["X-Hermes-Error"] = _redact_api_error_text(
                err_msg,
                limit=200,
            )
    return response_data, 200, response_headers


def _coerce_port(value: Any, default: int = DEFAULT_PORT) -> int:
    """Parse a listen port without letting malformed env/config values crash startup."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_TRUE_REQUEST_BOOL_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_REQUEST_BOOL_STRINGS = frozenset({"0", "false", "no", "off"})


def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    """Normalize boolean-like API payload values.

    External clients should send real JSON booleans, but some OpenAI-compatible
    frontends and middleware serialize flags like ``stream`` as strings.  Using
    Python truthiness on those values misroutes requests because ``"false"`` is
    still truthy.  Treat only explicit bool-ish scalars as booleans; everything
    else falls back to the caller's default.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        if normalized in _FALSE_REQUEST_BOOL_STRINGS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


_REQUEST_OPTION_MISSING = object()
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
_RUNTIME_AGENT_OVERRIDE_KEYS = (
    "api_key",
    "base_url",
    "provider",
    "api_mode",
    "command",
    "args",
    "credential_pool",
    "max_tokens",
)


def _clean_request_string(value: Any) -> Optional[str]:
    """Return a stripped request string, or None for absent/non-string values."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _request_reasoning_config(model_options: Any) -> Optional[Dict[str, Any]]:
    """Translate browser/API model_options into AIAgent reasoning_config.

    The browser extension sends both a structured ``reasoning`` object and a
    compatibility ``reasoning_effort`` scalar.  Keep this parser permissive so
    older clients can send either shape, but ignore unknown effort values rather
    than raising on a chat request.
    """
    if not isinstance(model_options, dict):
        return None

    reasoning = model_options.get("reasoning")
    enabled: Any = None
    effort: Any = model_options.get("reasoning_effort")
    if isinstance(reasoning, dict):
        enabled = reasoning.get("enabled")
        effort = reasoning.get("effort", effort)

    effort_norm = str(effort).strip().lower() if effort is not None else ""
    if enabled is False or effort_norm == "none":
        return {"enabled": False}
    if effort_norm in _REASONING_EFFORTS and effort_norm != "none":
        return {"enabled": True, "effort": effort_norm}
    if enabled is True:
        return {"enabled": True}
    return None


def _request_service_tier(model_options: Any) -> Any:
    """Return a per-request service_tier override or _REQUEST_OPTION_MISSING."""
    if not isinstance(model_options, dict):
        return _REQUEST_OPTION_MISSING
    if "service_tier" in model_options:
        from gateway.api_execution_context import normalize_service_tier

        return normalize_service_tier(
            model_options.get("service_tier"),
            field="model_options.service_tier",
        )
    if "fast" in model_options:
        fast = model_options.get("fast")
        if not isinstance(fast, bool):
            from gateway.api_execution_context import ApiExecutionContextError

            raise ApiExecutionContextError(
                "model_options.fast must be a boolean"
            )
        return "priority" if fast else None
    return _REQUEST_OPTION_MISSING


def _normalize_persisted_api_model_options(value: Any) -> Dict[str, Any]:
    """Return only the non-secret model options safe for durable storage."""

    from gateway.api_execution_context import normalize_model_options

    return normalize_model_options(value)


def _invalid_runtime_request_response(exc: Exception) -> "web.Response":
    """Return a controlled 400 for unsafe model-routing metadata."""

    return web.json_response(
        _openai_error(
            str(exc),
            code="invalid_runtime_request",
        ),
        status=400,
    )


def _apply_runtime_agent_overrides(
    runtime_kwargs: Dict[str, Any], overrides: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Merge resolved provider/runtime fields into ``runtime_kwargs`` in place."""
    if not isinstance(overrides, dict):
        return runtime_kwargs
    for key in _RUNTIME_AGENT_OVERRIDE_KEYS:
        if key not in overrides:
            continue
        value = overrides.get(key)
        if value is None:
            continue
        runtime_kwargs[key] = list(value) if key == "args" and isinstance(value, (list, tuple)) else value
    return runtime_kwargs


def _resolve_request_runtime_agent_kwargs(provider: str, target_model: Optional[str] = None) -> Dict[str, Any]:
    """Resolve runtime kwargs for a one-request provider override.

    This mirrors gateway.run._resolve_runtime_agent_kwargs(), but accepts an
    explicit provider/model so an API caller can use the same authenticated
    provider catalog as the TUI without mutating config.yaml.
    """
    from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error, _get_model_config

    try:
        runtime = resolve_runtime_provider(requested=provider, target_model=target_model)
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    model_cfg = _get_model_config()
    max_tokens = None
    env_max_tokens = os.environ.get("HERMES_MAX_TOKENS")
    if env_max_tokens:
        try:
            max_tokens = int(env_max_tokens)
        except (ValueError, TypeError):
            max_tokens = None
    elif isinstance(model_cfg, dict):
        cfg_max_tokens = model_cfg.get("max_tokens")
        if isinstance(cfg_max_tokens, int):
            max_tokens = cfg_max_tokens
    if max_tokens is None:
        runtime_max_tokens = runtime.get("max_output_tokens")
        if isinstance(runtime_max_tokens, int) and runtime_max_tokens > 0:
            max_tokens = runtime_max_tokens

    return {
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
        "credential_pool": runtime.get("credential_pool"),
        "max_tokens": max_tokens,
    }


def _request_agent_overrides(
    body: Any,
    *,
    virtual_model: Optional[str] = None,
    allow_bare_model: bool = True,
) -> Dict[str, Any]:
    """Extract per-request model/provider/options for _run_agent.

    ``/v1/models`` advertises a stable virtual model (usually ``hermes-agent``)
    for OpenAI-compatible clients.  Treat that alias as "use the gateway
    default"; real model picker selections from the browser extension send the
    raw provider model id plus a provider slug and should override this turn.

    ``allow_bare_model`` controls whether a ``model`` value WITHOUT an
    accompanying ``provider`` is honored.  Generic OpenAI clients routinely
    hardcode model names ("gpt-4o", ...), and existing deployments rely on
    those falling back to the gateway default on the OpenAI-compatible
    surfaces — so those handlers pass the opt-in
    ``direct_model_requests`` config value here, while Hermes-native
    endpoints (session chat, /v1/runs) always allow it.  A request that
    sends an explicit ``provider`` is unambiguously Hermes-aware and is
    always honored.
    """
    if not isinstance(body, dict):
        return {}

    from gateway.api_execution_context import (
        normalize_model_identifier,
        normalize_model_options,
        normalize_provider_slug,
    )

    overrides: Dict[str, Any] = {}
    provider = normalize_provider_slug(
        body.get("provider"),
        field="provider",
    )
    if provider:
        overrides["requested_provider"] = provider

    model = normalize_model_identifier(
        body.get("model"),
        field="model",
    )
    if model and model != virtual_model and (provider or allow_bare_model):
        overrides["requested_model"] = model

    model_options = body.get("model_options")
    if model_options is not None:
        normalize_model_options(model_options)
    if isinstance(model_options, dict):
        overrides["model_options"] = dict(model_options)
    return overrides


def _message_text_prefix(content: Any) -> str:
    if isinstance(content, str):
        return content[:128]
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content[:4]:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        if sum(len(part) for part in parts) >= 128:
            break
    return "\n".join(parts)[:128]


def _is_compressed_summary_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get(_COMPRESSED_SUMMARY_METADATA_KEY):
        return True
    prefix = _message_text_prefix(message.get("content"))
    return prefix.startswith("[CONTEXT COMPACTION") or prefix.startswith("[CONTEXT SUMMARY]:")


def _auto_truncate_response_history(
    conversation_history: List[Dict[str, Any]],
    *,
    limit: int = RESPONSES_AUTO_TRUNCATION_HISTORY_LIMIT,
) -> List[Dict[str, Any]]:
    """Keep recent Responses history without dropping the compaction handoff.

    Compaction summaries are preserved wherever they sit in the history —
    the gateway /compress path can leave them after a retained system head
    (see ``context_compressor`` force-user-leading handling), so a
    leading-block-only scan would silently drop them.
    """
    if limit <= 0 or len(conversation_history) <= limit:
        return conversation_history

    summary_indices = [
        index
        for index, message in enumerate(conversation_history)
        if _is_compressed_summary_message(message)
    ]
    if not summary_indices:
        return conversation_history[-limit:]

    kept_indices = set(summary_indices[:limit])
    remaining = limit - len(kept_indices)
    if remaining > 0:
        summary_index_set = set(summary_indices)
        for index in range(len(conversation_history) - 1, -1, -1):
            if index in summary_index_set:
                continue
            kept_indices.add(index)
            remaining -= 1
            if remaining <= 0:
                break

    return [conversation_history[index] for index in sorted(kept_indices)]


def _normalize_chat_content(
    content: Any, *, _max_depth: int = 10, _depth: int = 0,
) -> str:
    """Normalize OpenAI chat message content into a plain text string.

    Some clients (Open WebUI, LobeChat, etc.) send content as an array of
    typed parts instead of a plain string::

        [{"type": "text", "text": "hello"}, {"type": "input_text", "text": "..."}]

    This function flattens those into a single string so the agent pipeline
    (which expects strings) doesn't choke.

    Defensive limits prevent abuse: recursion depth, list size, and output
    length are all bounded.
    """
    if _depth > _max_depth:
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content

    if isinstance(content, list):
        parts: List[str] = []
        total_len = 0
        items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
        for item in items:
            if isinstance(item, str):
                if item:
                    part = item[:MAX_NORMALIZED_TEXT_LENGTH]
                    parts.append(part)
                    total_len += len(part)
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"text", "input_text", "output_text"}:
                    text = item.get("text", "")
                    if text:
                        try:
                            part = str(text)[:MAX_NORMALIZED_TEXT_LENGTH]
                            parts.append(part)
                            total_len += len(part)
                        except Exception:
                            pass
                # Silently skip image_url / other non-text parts
            elif isinstance(item, list):
                nested = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
                if nested:
                    parts.append(nested)
                    total_len += len(nested)
            # Check accumulated size
            if total_len >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        result = "\n".join(parts)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result

    # Fallback for unexpected types (int, float, bool, etc.)
    try:
        result = str(content)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result
    except Exception:
        return ""


# Content part type aliases used by the OpenAI Chat Completions and Responses
# APIs.  We accept both spellings on input and emit a single canonical internal
# shape (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``) that the
# rest of the agent pipeline already understands.
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})
_FILE_PART_TYPES = frozenset({"file", "input_file"})


def _normalize_multimodal_content(content: Any) -> Any:
    """Validate and normalize multimodal content for the API server.

    Returns a plain string when the content is text-only, or a list of
    ``{"type": "text"|"image_url", ...}`` parts when images are present.
    The output shape is the native OpenAI Chat Completions vision format,
    which the agent pipeline accepts verbatim (OpenAI-wire providers) or
    converts (``_preprocess_anthropic_content`` for Anthropic).

    Raises ``ValueError`` with an OpenAI-style code on invalid input:
      * ``unsupported_content_type`` — file/input_file/file_id parts, or
        non-image ``data:`` URLs.
      * ``invalid_image_url`` — missing URL or unsupported scheme.
      * ``invalid_content_part`` — malformed text/image objects.

    Callers translate the ValueError into a 400 response.
    """
    # Scalar passthrough mirrors ``_normalize_chat_content``.
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content
    if not isinstance(content, list):
        # Mirror the legacy text-normalizer's fallback so callers that
        # pre-existed image support still get a string back.
        return _normalize_chat_content(content)

    items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
    normalized_parts: List[Dict[str, Any]] = []
    text_accum_len = 0

    for part in items:
        if isinstance(part, str):
            if part:
                trimmed = part[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if not isinstance(part, dict):
            # Ignore unknown scalars for forward compatibility with future
            # Responses API additions (e.g. ``refusal``).  The same policy
            # the text normalizer applies.
            continue

        raw_type = part.get("type")
        part_type = str(raw_type or "").strip().lower()

        if part_type in _TEXT_PART_TYPES:
            text = part.get("text")
            if text is None:
                continue
            if not isinstance(text, str):
                text = str(text)
            if text:
                trimmed = text[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if part_type in _IMAGE_PART_TYPES:
            detail = part.get("detail")
            image_ref = part.get("image_url")
            # OpenAI Responses sends ``input_image`` with a top-level
            # ``image_url`` string; Chat Completions sends ``image_url`` as
            # ``{"url": "...", "detail": "..."}``.  Support both.
            if isinstance(image_ref, dict):
                url_value = image_ref.get("url")
                detail = image_ref.get("detail", detail)
            else:
                url_value = image_ref
            if not isinstance(url_value, str) or not url_value.strip():
                raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
            url_value = url_value.strip()
            lowered = url_value.lower()
            if lowered.startswith("data:"):
                if not lowered.startswith("data:image/") or "," not in url_value:
                    raise ValueError(
                        "unsupported_content_type:Only image data URLs are supported. "
                        "Non-image data payloads are not supported."
                    )
            elif not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError(
                    "invalid_image_url:Image inputs must use http(s) URLs or data:image/... URLs."
                )
            image_part: Dict[str, Any] = {"type": "image_url", "image_url": {"url": url_value}}
            if detail is not None:
                if not isinstance(detail, str) or not detail.strip():
                    raise ValueError("invalid_content_part:Image detail must be a non-empty string when provided.")
                image_part["image_url"]["detail"] = detail.strip()
            normalized_parts.append(image_part)
            continue

        if part_type in _FILE_PART_TYPES:
            raise ValueError(
                "unsupported_content_type:Inline image inputs are supported, "
                "but uploaded files and document inputs are not supported on this endpoint."
            )

        # Unknown part type — reject explicitly so clients get a clear error
        # instead of a silently dropped turn.
        raise ValueError(
            f"unsupported_content_type:Unsupported content part type {raw_type!r}. "
            "Only text and image_url/input_image parts are supported."
        )

    if not normalized_parts:
        return ""

    # Text-only: collapse to a plain string so downstream logging/trajectory
    # code sees the native shape and prompt caching on text-only turns is
    # unaffected.
    if all(p.get("type") == "text" for p in normalized_parts):
        return "\n".join(p["text"] for p in normalized_parts if p.get("text"))

    return normalized_parts


def _content_has_visible_payload(content: Any) -> bool:
    """True when content has any text or image attachment.  Used to reject empty turns."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = str(part.get("type") or "").strip().lower()
                if ptype in _TEXT_PART_TYPES and str(part.get("text") or "").strip():
                    return True
                if ptype in _IMAGE_PART_TYPES:
                    return True
    return False


def _multimodal_validation_error(exc: ValueError, *, param: str) -> "web.Response":
    """Translate a ``_normalize_multimodal_content`` ValueError into a 400 response."""
    raw = str(exc)
    code, _, message = raw.partition(":")
    if not message:
        code, message = "invalid_content_part", raw
    return web.json_response(
        _openai_error(message, code=code, param=param),
        status=400,
    )


def _reap_disconnected_agent_processes(
    agent: Any, *, source: str = "api_server_sse_disconnect"
) -> None:
    """Reap background processes an abandoned API-server turn created.

    Mirrors the gateway-turn cleanup in ``gateway/run.py`` (#76115) for this
    API-server surface, which runs its own agent lifecycle via ``_run_agent``
    and never passes through ``TurnRunner`` — so it needs its own trigger for
    the same baseline-diff reap. Fire-and-forget on a daemon thread so the
    SSE handler's own cleanup isn't blocked on process-tree teardown.

    Reaping is epoch-gated: client-provided session IDs are conversation
    scopes, and multiple concurrent runs can intentionally share one (see
    ``_handle_runs``). Without the gate, run A disconnecting could kill a
    process a still-live run B (same task_id) spawned after A's baseline
    snapshot — the same stale-reaper bug class the gateway path gates via
    ``run_generation``. The epoch closure skips the reap when a newer run
    has since claimed the task_id; that newer run's own baseline covers its
    eventual cleanup.
    """
    process_task_id = getattr(agent, "_gateway_turn_process_task_id", "")
    process_baseline = getattr(agent, "_gateway_turn_process_baseline", None)
    if not process_task_id or process_baseline is None:
        return
    epoch = getattr(agent, "_gateway_turn_process_epoch", None)
    is_still_current: Optional[Any] = None
    if epoch is not None:
        def _epoch_still_current(_task_id=process_task_id, _epoch=epoch):
            # Skip only when a NEWER run has claimed this task_id. A missing
            # entry means the abandoned run's own clear pruned it (worker
            # returned after the interrupt) — no newer claimant exists, so
            # the reap must still proceed or the leak survives. This matches
            # the gateway gate's semantics: worker completion does not bump
            # run_generation either.
            with _TURN_PROCESS_EPOCH_LOCK:
                current = _TURN_PROCESS_EPOCHS.get(_task_id)
            return current is None or current == _epoch

        is_still_current = _epoch_still_current

    from gateway.run import _reap_gateway_turn_processes

    threading.Thread(
        target=_reap_gateway_turn_processes,
        args=(process_task_id, process_baseline),
        kwargs={"source": source, "is_still_current": is_still_current},
        name=f"api-turn-reaper-{process_task_id[:12]}",
        daemon=True,
    ).start()


# Per-task-id run epochs for the reap gate above. task_id is a conversation
# scope shared by concurrent API runs, so each run that claims it bumps the
# epoch; a reaper holding a stale epoch declines to kill. Epochs come from a
# single monotonic counter (never reused), so pruning an entry and later
# re-claiming the task_id can never resurrect a stale reaper's claim.
# Entries are pruned on clear when still current, bounding the dict to
# in-flight runs.
_TURN_PROCESS_EPOCHS: Dict[str, int] = {}
_TURN_PROCESS_EPOCH_LOCK = threading.Lock()
_TURN_PROCESS_EPOCH_COUNTER = itertools.count(1)


def _publish_turn_process_ownership(agent: Any, task_id: str) -> None:
    """Snapshot the process baseline and claim the task_id's current epoch.

    Single place all API-server agent lifecycles (chat/responses ``_run_agent``
    and ``/v1/runs``) record turn ownership, so the marker attribute names and
    epoch bookkeeping cannot drift between surfaces.
    """
    from tools.process_registry import process_registry

    with _TURN_PROCESS_EPOCH_LOCK:
        epoch = next(_TURN_PROCESS_EPOCH_COUNTER)
        _TURN_PROCESS_EPOCHS[task_id] = epoch
    agent._gateway_turn_process_task_id = task_id
    agent._gateway_turn_process_baseline = process_registry.snapshot_running_ids(
        task_id
    )
    agent._gateway_turn_process_epoch = epoch


def _clear_turn_process_ownership(agent: Any) -> None:
    """Clear turn ownership the moment the turn finishes (success or crash).

    A disconnect/cancel landing after this point must not reap background
    work the turn deliberately left running — mirrors the same race-window
    guard in ``gateway/run.py``'s ``_run_sync_with_timeout_lifecycle``.
    """
    task_id = getattr(agent, "_gateway_turn_process_task_id", "")
    epoch = getattr(agent, "_gateway_turn_process_epoch", None)
    if task_id and epoch is not None:
        with _TURN_PROCESS_EPOCH_LOCK:
            # Prune only when this run is still the current claimant; a
            # newer concurrent run owns the entry otherwise.
            if _TURN_PROCESS_EPOCHS.get(task_id) == epoch:
                del _TURN_PROCESS_EPOCHS[task_id]
    agent._gateway_turn_process_task_id = ""
    agent._gateway_turn_process_baseline = frozenset()
    agent._gateway_turn_process_epoch = None


def _session_chat_user_message(body: Dict[str, Any], *, param: str = "message") -> tuple[Any, Optional["web.Response"]]:
    """Parse and normalize session chat ``message`` / ``input`` like chat completions."""
    user_message = body.get("message") or body.get("input")
    if not _content_has_visible_payload(user_message):
        return None, web.json_response(
            _openai_error("Missing 'message' field", code="missing_message"),
            status=400,
        )
    try:
        return _normalize_multimodal_content(user_message), None
    except ValueError as exc:
        return None, _multimodal_validation_error(exc, param=param)


def check_api_server_requirements() -> bool:
    """Check if API server dependencies are available."""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    SQLite-backed LRU store for Responses API state.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.

    Persists across gateway restarts.  Falls back to in-memory SQLite
    if the on-disk path is unavailable.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        self._max_size = max_size
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        self._db_path: Optional[str] = db_path if db_path != ":memory:" else None
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._db_path = None
        # Use shared WAL-fallback helper so response_store.db degrades
        # gracefully on NFS/SMB/FUSE-mounted HERMES_HOME (same filesystem
        # issue addressed for state.db/kanban.db — see
        # hermes_state._WAL_INCOMPAT_MARKERS).
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="response_store.db")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                name TEXT PRIMARY KEY,
                response_id TEXT NOT NULL
            )"""
        )
        self._conn.commit()
        # response_store.db contains conversation history (tool payloads,
        # prompts, results). Tighten to owner-only after creation so other
        # local users on a shared box can't read it. Run once at __init__
        # rather than after every commit — chmod-on-every-write is wasted
        # syscalls on a hot path.
        self._tighten_file_permissions()

    def _tighten_file_permissions(self) -> None:
        """Force owner-only permissions on the DB and SQLite sidecars."""
        if not self._db_path:
            return
        for candidate in (
            Path(self._db_path),
            Path(f"{self._db_path}-wal"),
            Path(f"{self._db_path}-shm"),
        ):
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                logger.debug(
                    "Failed to restrict response store permissions for %s",
                    candidate,
                    exc_info=True,
                )

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (updates access time for LRU)."""
        row = self._conn.execute(
            "SELECT data FROM responses WHERE response_id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
            (time.time(), response_id),
        )
        self._conn.commit()
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Corrupted JSON in response store for id=%s, evicting entry",
                response_id,
            )
            self._conn.execute(
                "DELETE FROM responses WHERE response_id = ?",
                (response_id,),
            )
            self._conn.commit()
            return None

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """Store a response, evicting the oldest if at capacity."""
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (response_id, data, accessed_at) VALUES (?, ?, ?)",
            (response_id, json.dumps(data, default=str), time.time()),
        )
        # Evict oldest entries beyond max_size
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count > self._max_size:
            # Collect IDs that will be evicted
            evict_ids = [
                row[0]
                for row in self._conn.execute(
                    "SELECT response_id FROM responses ORDER BY accessed_at ASC LIMIT ?",
                    (count - self._max_size,),
                ).fetchall()
            ]
            if evict_ids:
                placeholders = ",".join("?" for _ in evict_ids)
                # Clear conversation mappings pointing to evicted responses
                self._conn.execute(
                    f"DELETE FROM conversations WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
                # Delete evicted responses
                self._conn.execute(
                    f"DELETE FROM responses WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
        self._conn.commit()

    def delete(self, response_id: str) -> bool:
        """Remove a response from the store. Returns True if found and deleted."""
        # Clear conversation mappings pointing to this response
        self._conn.execute(
            "DELETE FROM conversations WHERE response_id = ?", (response_id,)
        )
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE response_id = ?", (response_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str) -> Optional[str]:
        """Get the latest response_id for a conversation name."""
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None

    def set_conversation(self, name: str, response_id: str) -> None:
        """Map a conversation name to its latest response_id."""
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (name, response_id) VALUES (?, ?)",
            (name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers for explicitly allowed origins; handle OPTIONS preflight."""
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


_MEDIA_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_MEDIA_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_MEDIA_DATA_URL_MAX_BYTES = 5 * 1024 * 1024  # skip images larger than 5MB


def _resolve_media_to_data_urls(text: str) -> str:
    """Replace ``MEDIA:<path>`` image tags with inline base64 data URLs.

    Remote OpenAI-compatible frontends can't read local file paths, so
    ``MEDIA:`` tags referencing images on the server are useless to them.
    Inline small local images as markdown data URLs; non-image or unreadable
    paths are left untouched.

    Uses the same anchored ``MEDIA_TAG_CLEANUP_RE`` matcher and
    ``validate_media_delivery_path`` safety check every other platform
    adapter's media delivery already goes through (gateway/platforms/base.py)
    — an absolute-path anchor plus a known-extension requirement, and a
    resolved-path check against the credential/system-path denylist. The
    prior pattern here matched any bare token after ``MEDIA:`` (including a
    relative/traversal path like ``../../etc/passwd.png``) and read the file
    directly with no denylist, so any image-suffixed, readable file the
    process could see was base64-exfiltrated to the API caller if its path
    merely appeared in the model's own final reply text.
    """
    if not text or "MEDIA:" not in text:
        return text
    import base64

    def _to_data_url(path_str: str) -> Optional[str]:
        # validate_media_delivery_path() strips wrapping quotes/backticks
        # and trailing punctuation internally, same as MEDIA_TAG_CLEANUP_RE's
        # other callers (extract_media / _strip_media_tag_directives) rely on.
        safe_path = validate_media_delivery_path(path_str)
        if not safe_path:
            return None
        p = Path(safe_path)
        suffix = p.suffix.lower()
        if suffix not in _MEDIA_IMG_EXT:
            return None
        try:
            if p.stat().st_size > _MEDIA_DATA_URL_MAX_BYTES:
                return None
            b64 = base64.b64encode(p.read_bytes()).decode()
        except OSError:
            return None
        return f"![image](data:{_MEDIA_MIME[suffix]};base64,{b64})"

    def _repl(m: "re.Match[str]") -> str:
        return _to_data_url(m.group("path")) or m.group(0)

    try:
        return MEDIA_TAG_CLEANUP_RE.sub(_repl, text)
    except Exception:
        return text


def _redact_api_error_text(value: Any, *, limit: int | None = None) -> str:
    """Redact API-bound error text before it crosses the HTTP boundary."""
    redacted = redact_sensitive_text(str(value), force=True)
    if limit is not None:
        return redacted[:limit]
    return redacted


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """OpenAI-style error envelope."""
    return {
        "error": {
            "message": _redact_api_error_text(message),
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


def _invalid_internal_session_id_error() -> Dict[str, Any]:
    """Return the fixed, non-reflective error for corrupt internal IDs."""

    return _openai_error(
        "Internal session continuity state is invalid.",
        err_type="server_error",
        code="invalid_internal_session_id",
    )


def _invalid_internal_session_id_response() -> "web.Response":
    """Translate corrupt internal continuity state to a controlled HTTP 500."""

    return web.json_response(
        _invalid_internal_session_id_error(),
        status=500,
    )


_api_agent_request_reservation: ContextVar[Optional[dict[str, bool]]] = ContextVar(
    "api_agent_request_reservation", default=None
)


def _admit_api_agent_request(handler):
    """Reserve an authenticated API turn before its handler first awaits.

    Gateway shutdown and aiohttp requests share an event loop. Keeping the
    drain check and reservation in one non-awaiting block prevents a request
    admitted immediately before shutdown from becoming invisible while it is
    still parsing its body or resolving session state. The mutable reservation
    is intentionally shared with child tasks so agent/task bookkeeping releases
    this one slot exactly once.
    """
    @wraps(handler)
    async def _wrapped(self, request, *args, **kwargs):
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        draining = self._draining_response()
        if draining is not None:
            return draining
        reservation = {"active": True}
        token = _api_agent_request_reservation.set(reservation)
        self._pending_agent_requests += 1
        try:
            return await handler(self, request, *args, **kwargs)
        finally:
            if reservation["active"]:
                reservation["active"] = False
                self._pending_agent_requests = max(0, self._pending_agent_requests - 1)
            _api_agent_request_reservation.reset(token)

    return _wrapped


def _release_pending_api_work(adapter, reservation: dict[str, bool]) -> None:
    """Release a pending-work reservation exactly once."""
    if reservation["active"]:
        reservation["active"] = False
        adapter._pending_agent_requests = max(0, adapter._pending_agent_requests - 1)


@contextmanager
def _reserve_pending_api_work(adapter):
    """Keep externally-triggered background work visible across awaits.

    A handler can detach the reservation to an asyncio task; its done callback
    then owns release so shutdown cannot miss the handoff to background work.
    """
    reservation = {"active": True, "detached": False}
    adapter._pending_agent_requests += 1
    try:
        yield reservation
    finally:
        if not reservation["detached"]:
            _release_pending_api_work(adapter, reservation)


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """Reject overly large request bodies early based on Content-Length."""
        if request.method in {"POST", "PUT", "PATCH"}:
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_REQUEST_BYTES:
                        return web.json_response(_openai_error("Request body too large.", code="body_too_large"), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            # aiohttp's client_max_size tripped mid-read (chunked bodies carry
            # no Content-Length) — return a proper 413 instead of letting the
            # handler's broad JSON except turn it into 400 "Invalid JSON".
            return web.json_response(
                _openai_error("Request body too large.", code="body_too_large"),
                status=413,
            )
else:
    body_limit_middleware = None  # type: ignore[assignment]

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "0",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """Add security headers to all responses (including errors)."""
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """In-memory idempotency cache with TTL and basic LRU semantics.

    ``key`` is deliberately opaque/hashable rather than necessarily a string.
    HTTP callers namespace a client-supplied key by adapter, selected profile,
    conversation session, and long-term-memory scope before it reaches this
    cache.  A module-global bare key would otherwise let one profile/session
    reuse another tenant's completed response.
    """
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        from collections import OrderedDict
        self._store = OrderedDict()
        self._inflight: Dict[tuple[object, str], "asyncio.Task[Any]"] = {}
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: object, fingerprint: str, compute_coro):
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]

        inflight_key = (key, fingerprint)
        task = self._inflight.get(inflight_key)
        if task is None:
            async def _compute_and_store():
                resp = await compute_coro()
                import time as _t
                self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
                self._purge()
                return resp

            task = asyncio.create_task(_compute_and_store())
            self._inflight[inflight_key] = task

            def _clear_inflight(done_task: "asyncio.Task[Any]") -> None:
                if self._inflight.get(inflight_key) is done_task:
                    self._inflight.pop(inflight_key, None)

            task.add_done_callback(_clear_inflight)

        return await asyncio.shield(task)


_idem_cache = _IdempotencyCache()


def _scoped_idempotency_cache_key(
    key: str,
    *,
    adapter_scope: str,
    request_scope: APIRequestScope,
    session_id: str,
    gateway_session_key: str,
) -> tuple[str, str, str, str, str]:
    """Close one idempotency key inside its execution/tenant boundaries."""

    return (
        str(adapter_scope or ""),
        request_scope.bind("idempotency-tenant", "").internal_key,
        str(session_id or ""),
        str(gateway_session_key or ""),
        str(key or ""),
    )


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


def _derive_chat_session_id(
    system_prompt: Optional[str],
    first_user_message: str,
) -> str:
    """Derive a stable session ID from the conversation's first user message.

    OpenAI-compatible frontends (Open WebUI, LibreChat, etc.) send the full
    conversation history with every request.  The system prompt and first user
    message are constant across all turns of the same conversation, so hashing
    them produces a deterministic session ID that lets the API server reuse
    the same Hermes session (and therefore the same Docker container sandbox
    directory) across turns.
    """
    seed = f"{system_prompt or ''}\n{first_user_message}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


_CRON_AVAILABLE = False
try:
    from cron.jobs import (
        list_jobs as _cron_list,
        get_job as _cron_get,
        create_job as _cron_create,
        update_job as _cron_update,
        remove_job as _cron_remove,
        pause_job as _cron_pause,
        resume_job as _cron_resume,
        trigger_job as _cron_trigger,
    )
    _CRON_AVAILABLE = True
except ImportError:
    _cron_list = None
    _cron_get = None
    _cron_create = None
    _cron_update = None
    _cron_remove = None
    _cron_pause = None
    _cron_resume = None
    _cron_trigger = None


def _notify_cron_provider_jobs_changed() -> None:
    """Tell the active cron scheduler provider the job set changed after a REST
    mutation (no-op for the built-in). Best-effort — never breaks the handler."""
    try:
        from cron.scheduler import _notify_provider_jobs_changed
        _notify_provider_jobs_changed()
    except Exception:
        pass

class _ProviderAuthResolutionError(RuntimeError):
    """Raised only when gateway.run._resolve_runtime_agent_kwargs() fails
    to resolve provider credentials.

    That function is the sole raiser of RuntimeError(format_runtime_
    provider_error(...)) anywhere in _create_agent()'s call graph.
    Re-raising it as this dedicated subclass -- instead of catching bare
    RuntimeError around the much wider _create_agent()+run_conversation()
    span -- lets callers distinguish "provider auth/credential failure"
    from any other RuntimeError a provider adapter or run_conversation()
    might legitimately raise (e.g. run_agent.py's "Failed to recreate
    closed OpenAI client"), which a bare `except RuntimeError` there would
    otherwise mislabel as an auth failure.
    """


class _DetachedApiContinuityError(RuntimeError):
    """A trusted detached wake no longer matches its originating API route."""


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through hermes-agent's AIAgent.
    """

    # Stateless request/response: every route (the OpenAI-spec
    # /v1/chat/completions and /v1/responses, and the proprietary /v1/runs SSE
    # stream) tears down its channel when the turn ends. There is no persistent
    # outbound channel to push a background completion to a client that already
    # received its response, and ``send()`` is a no-op stub. So async-delivery
    # tools (terminal notify_on_complete / watch_patterns, delegate_task
    # background=True) must NOT promise delivery on this path — see
    # ``async_delivery_supported()``.
    supports_async_delivery: bool = False

    # Same statelessness applies to the startup auto-resume prompt: no client
    # is waiting to answer "session restored — what next?", so a resumed turn
    # should complete the interrupted work rather than acknowledge (#57056).
    interactive_resume: bool = False

    def __init__(
        self,
        config: PlatformConfig,
        *,
        run_admission_callback: RunAdmissionCallback | None = None,
        require_capability_canary: bool = False,
    ):
        if type(require_capability_canary) is not bool:
            raise TypeError("require_capability_canary must be boolean")
        super().__init__(config, Platform.API_SERVER)
        self._require_capability_canary = require_capability_canary
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        raw_port = extra.get("port")
        if raw_port is None:
            raw_port = os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))
        self._port: int = _coerce_port(raw_port, DEFAULT_PORT)
        self._api_key: str = _resolve_api_server_key(extra)
        self._api_bearer_verifier: APIBearerVerifier | None = (
            _resolve_api_bearer_verifier(extra)
        )
        self._approval_passkey: str = _resolve_api_approval_passkey(extra)
        self._approval_passkey_verifier: APIApprovalScryptVerifier | None = (
            _resolve_api_approval_verifier(extra)
        )
        if (
            self._api_key
            and self._approval_passkey
            and hmac.compare_digest(self._api_key, self._approval_passkey)
        ):
            raise ValueError(
                "api_server control bearer and approval passkey must be distinct"
            )
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._model_name: str = self._resolve_model_name(
            extra.get("model_name", os.getenv("API_SERVER_MODEL_NAME", "")),
        )
        # model_routes: maps incoming ``model`` field values to specific
        # provider/model configs so one API server instance can serve
        # multiple clients on different backends.
        #
        # Config format (platforms.api_server.extra in the gateway config):
        #   model_routes:
        #     minimax-m2:          # alias the client sends as the "model" field
        #       model: "minimax/minimax-m1"
        #       provider: "openrouter"   # optional — resolved via the provider
        #                                # credential chain when set
        #       api_key: "sk-…"          # optional — per-route UPSTREAM provider
        #                                # key override (NOT caller auth; never logged)
        #       base_url: "https://…"    # optional — per-route base URL override
        self._model_routes: Dict[str, Dict[str, Any]] = self._parse_model_routes(
            extra.get("model_routes"),
        )
        # The production capability canary installs one mechanical barrier
        # after its gateway-owned epoch is bound and before the first model
        # call. Public HTTP input can neither supply nor alter that epoch.
        if run_admission_callback is not None:
            if not callable(run_admission_callback):
                raise TypeError("run admission callback must be callable")
            self._run_admission_callback = run_admission_callback
        elif self._require_capability_canary:
            from gateway.canonical_capability_canary_producers import (
                api_server_capability_admission,
            )

            self._run_admission_callback = api_server_capability_admission
        else:
            self._run_admission_callback = None
        # direct_model_requests: opt-in passthrough for a bare ``model`` value
        # (no ``provider``) on the OpenAI-compatible surfaces
        # (/v1/chat/completions, /v1/responses).  Off by default: generic
        # OpenAI clients routinely hardcode model names ("gpt-4o", ...), and
        # existing deployments rely on those falling back to the gateway
        # default rather than switching the executing model.  Requests that
        # send an explicit ``provider`` — and the Hermes-native session-chat
        # and /v1/runs endpoints — are always honored regardless of this flag.
        # (Idea credit: PR #22825 by @mssteuer.)
        self._direct_model_requests: bool = _coerce_request_bool(
            extra.get("direct_model_requests"), default=False
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        # Responses API state belongs to the process-frozen profile. Keep the
        # historical attribute for compatibility; any cached store is keyed by
        # a canonical, host-validated profile home.
        from hermes_constants import get_hermes_home
        from hermes_cli.profiles import get_active_profile_name

        default_response_identity = capture_api_profile_identity(
            get_active_profile_name() or "default",
            Path(get_hermes_home()),
        )
        default_response_home = default_response_identity.canonical_home
        # Marker bootstrap is deliberately complete before SQLite opens. This
        # prevents an early resource from silently binding a replacement home
        # during the constructor-to-listener startup window.
        self._response_store = ResponseStore(
            db_path=str(
                Path(default_response_home) / "response_store.db"
            )
        )
        self._response_store_default_home = default_response_home
        self._response_store_default_identity = default_response_identity
        self._response_stores_by_home: Dict[str, ResponseStore] = {}
        self._response_stores_lock = threading.RLock()
        # The cache is adapter-local, then each request key is additionally
        # scoped by profile/session.  Keep the historical module cache only as
        # a fallback for lightweight ``__new__`` test doubles.
        self._idempotency_cache = _IdempotencyCache()
        self._idempotency_adapter_scope = uuid.uuid4().hex
        # Active run streams: run_id -> asyncio.Queue of SSE event dicts
        self._run_streams: Dict[
            APIRequestScope,
            "asyncio.Queue[Optional[Dict]]",
        ] = {}
        # Creation timestamps for orphaned-run TTL sweep
        self._run_streams_created: Dict[APIRequestScope, float] = {}
        # Runs with a connected SSE consumer; their queue is actively draining.
        self._run_stream_subscribers: set[APIRequestScope] = set()
        # Active run agent/task references for stop support
        self._active_run_agents: Dict[APIRequestScope, Any] = {}
        self._active_run_tasks: Dict[
            APIRequestScope,
            "asyncio.Task",
        ] = {}
        # Stop is cooperative: the executor thread may outlive the HTTP request.
        self._stopping_run_ids: set[APIRequestScope] = set()
        # Pollable run status for dashboards and external control-plane UIs.
        self._run_statuses: Dict[APIRequestScope, Dict[str, Any]] = {}
        # Active approval session key for each run_id.  The approval core
        # resolves requests by session key, while API clients address the
        # in-flight run by run_id.
        self._run_approval_sessions: Dict[APIRequestScope, str] = {}
        self._session_db: Optional[Any] = None  # Lazy-init SessionDB for session continuity
        self._session_dbs: Dict[str, Any] = {}
        self._owned_session_db_ids: set[int] = set()
        self._session_dbs_lock = threading.RLock()
        self._session_db_offload_lock = threading.RLock()
        self._session_db_offloads_closing = False
        self._session_db_offload_futures: set[asyncio.Future] = set()
        # Last-known-good resolved model per session (keyed by gateway_session_key
        # ONLY — never session_id, which rotates/is ephemeral for one-off API
        # server requests; "*" is the process-wide fallback), mirroring
        # GatewayRunner._last_resolved_model in run.py — recovers from a
        # transient empty model resolution (#35314) instead of building an
        # agent with model="" that 400s every call until manual retry.
        self._last_resolved_model: Dict[APIRequestScope, str] = {}
        # Concurrency cap shared across all agent-serving endpoints
        # (/v1/chat/completions, /v1/responses, /v1/runs). Read from
        # config.yaml gateway.api_server.max_concurrent_runs; 0 disables
        # the cap. Bounds CPU / memory / upstream-LLM-quota exhaustion
        # from a request flood (#7483).
        self._max_concurrent_runs: int = self._resolve_max_concurrent_runs()
        # Number of in-flight runs on the non-streaming chat/responses paths
        # (the /v1/runs path tracks its own in-flight set via
        # _active_run_tasks).
        self._inflight_agent_runs: int = 0
        self._agent_run_reservations: int = 0
        # Exact old-epoch cleanup handles. Entries remain present while the
        # Canonical tombstone is unconfirmed; only a verified receipt removes
        # and zeroizes them. Values are private and never serialized.
        self._api_cleanup_handles: Dict[str, _APIServerCleanupHandle] = {}
        self._api_cleanup_tasks: set["asyncio.Task"] = set()
        self._api_cleanup_retry_tasks: Dict[str, "asyncio.Task"] = {}
        self._api_active_agents: Dict[int, Any] = {}
        # Keep one agent per exact API conversation so consecutive turns retain
        # the byte-stable cached prompt prefix.
        self._api_agent_cache: "OrderedDict[APIRequestScope, Dict[str, Any]]" = (
            OrderedDict()
        )
        self._api_agent_cache_lock = threading.RLock()
        self._api_deferred_agent_releases: set[int] = set()
        self._api_agent_run_locks = tuple(
            threading.RLock() for _ in range(API_AGENT_SESSION_LOCK_STRIPES)
        )
        # Frozen when the listener first starts.  A profile directory's
        # canonical path + filesystem generation is part of every internal
        # request key.  New/recreated profiles require a gateway restart;
        # they are never adopted into a live listener.
        self._api_profile_inventory: Optional[
            tuple[APIProfileIdentity, ...]
        ] = None
        self._api_profile_inventory_lock = threading.RLock()
        self._api_pending_clarifications: Dict[
            APIRequestScope,
            Dict[str, Any],
        ] = {}
        self._api_clarifications_lock = threading.RLock()
        self._api_pending_approvals: Dict[
            APIRequestScope,
            Dict[str, Any],
        ] = {}
        self._api_consumed_approval_nonces: Dict[str, float] = {}
        self._api_approvals_lock = threading.RLock()
        # Back-reference to the owning GatewayRunner (set by gateway/run.py)
        # so /api/platforms/{platform}/events can resolve sibling adapters.
        # BasePlatformAdapter declares the class-level default of None.
        self.gateway_runner: Optional[Any] = None
        # Requests admitted before their handler reaches agent bookkeeping.
        # Shutdown counts this reservation so the request cannot slip through
        # the drain between its first await and _run_agent()/task registration.
        self._pending_agent_requests: int = 0

    def _api_agent_run_lock_for(
        self,
        session_id: Optional[str],
        *,
        request_authority: Optional[APIRequestScope] = None,
    ) -> threading.RLock:
        """Return a bounded lock that serializes one conversation's turns.

        A cached AIAgent has mutable per-turn callbacks and transcript state, so
        two requests must never drive the same instance concurrently.  Striped
        locks keep the lock set bounded; an unrelated hash collision merely
        serializes two requests and cannot merge their cache entries.
        """

        cache_key = self._api_request_scope(
            "agent-session",
            session_id,
            authority=request_authority,
        )
        digest = hashlib.sha256(cache_key.internal_key.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % len(self._api_agent_run_locks)
        return self._api_agent_run_locks[index]

    def _api_session_message_count(self, session_id: Optional[str]) -> Optional[int]:
        """Read the persisted live message count used for cache coherence."""

        if not session_id:
            return None
        try:
            db = self._ensure_session_db()
            row = db.get_session(session_id) if db is not None else None
            if row is None:
                return None
            return int(row.get("message_count", 0) or 0)
        except Exception:
            # A failed coherence probe must never create a false mismatch.  The
            # agent still receives the caller/DB history and the next successful
            # probe can re-establish a baseline.
            return None

    def _attest_capability_agent_policy(self, agent: Any) -> None:
        """Fail at the final API boundary if sealed model policy drifted."""

        if not self._require_capability_canary:
            return
        from gateway.canonical_capability_canary_runtime import (
            validate_capability_agent_policy,
        )

        validate_capability_agent_policy(agent)

    @staticmethod
    def _api_credential_pool_identity(pool: Any) -> str:
        """Return a secret-safe fingerprint of the resolved auth route."""

        if pool is None:
            return ""
        try:
            entries = pool.entries() if callable(getattr(pool, "entries", None)) else []
            normalized = []
            for entry in entries:
                access_token = str(getattr(entry, "access_token", "") or "")
                refresh_token = str(getattr(entry, "refresh_token", "") or "")
                agent_key = str(getattr(entry, "agent_key", "") or "")
                normalized.append({
                    "id": str(getattr(entry, "id", "") or ""),
                    "provider": str(getattr(entry, "provider", "") or ""),
                    "auth_type": str(getattr(entry, "auth_type", "") or ""),
                    "priority": getattr(entry, "priority", None),
                    "source": str(getattr(entry, "source", "") or ""),
                    "access_sha256": (
                        hashlib.sha256(access_token.encode()).hexdigest()
                        if access_token
                        else ""
                    ),
                    "refresh_sha256": (
                        hashlib.sha256(refresh_token.encode()).hexdigest()
                        if refresh_token
                        else ""
                    ),
                    "agent_key_sha256": (
                        hashlib.sha256(agent_key.encode()).hexdigest()
                        if agent_key
                        else ""
                    ),
                    "base_url": str(getattr(entry, "base_url", "") or ""),
                    "inference_base_url": str(
                        getattr(entry, "inference_base_url", "") or ""
                    ),
                    "last_status": str(getattr(entry, "last_status", "") or ""),
                })
            payload = {
                "provider": str(getattr(pool, "provider", "") or ""),
                "current_id": str(getattr(pool, "_current_id", "") or ""),
                "entries": normalized,
            }
            return hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode()
            ).hexdigest()
        except Exception:
            # Unknown third-party pool implementations still receive a stable
            # class/provider identity.  Their actual selected API key/base URL
            # remains covered by the primary runtime signature.
            fallback = (
                type(pool).__module__,
                type(pool).__qualname__,
                str(getattr(pool, "provider", "") or ""),
            )
            return hashlib.sha256(repr(fallback).encode()).hexdigest()

    def _parse_api_control_session_id(
        self,
        request: "web.Request",
        *,
        required: bool,
    ) -> tuple[str, Optional["web.Response"]]:
        """Resolve one exact API conversation ID from header/query input."""

        header_value = str(
            request.headers.get("X-Hermes-Session-Id", "") or ""
        )
        query_value = str(request.query.get("session_id", "") or "")
        if header_value and query_value and header_value != query_value:
            return "", web.json_response(
                _openai_error(
                    "Session ID header and query parameter do not match",
                    code="session_id_mismatch",
                ),
                status=400,
            )
        session_id = header_value or query_value
        validated, error = self._validate_api_session_id_value(
            session_id,
            required=required,
        )
        if error is not None and not session_id and required:
            return "", web.json_response(
                _openai_error(
                    "X-Hermes-Session-Id or session_id is required",
                    code="session_id_required",
                ),
                status=400,
            )
        return validated, error

    def _validate_api_session_id_value(
        self,
        value: object,
        *,
        required: bool,
    ) -> tuple[str, Optional["web.Response"]]:
        """Validate every public session ID before it reaches path/state code."""

        try:
            return _canonical_api_session_id(
                value,
                required=required,
            ), None
        except ValueError:
            code = (
                "session_id_required"
                if required and (value is None or value == "")
                else "invalid_session_id"
            )
            message = (
                "API session ID is required"
                if code == "session_id_required"
                else "Invalid API session ID"
            )
            return "", web.json_response(
                _openai_error(
                    message,
                    code=code,
                ),
                status=400,
            )

    @staticmethod
    def _public_api_approval(state: Mapping[str, Any]) -> Dict[str, Any]:
        """Return the client-safe projection of an API approval request."""

        return {
            key: value
            for key, value in state.items()
            if not str(key).startswith("_")
        }

    @staticmethod
    def _api_approval_authority_payload(
        *,
        session_id: str,
        approval_id: str,
        choice: str,
        nonce: str,
        issued_at_unix: int,
        expires_at_unix: int,
        capability_epoch_sha256: str,
        run_id: str = "",
        schema: str = API_APPROVAL_AUTHORITY_SCHEMA,
    ) -> Dict[str, Any]:
        """Return the canonical fields signed by the distinct owner passkey."""

        return {
            "schema": schema,
            "session_id": session_id,
            "run_id": run_id,
            "approval_id": approval_id,
            "choice": choice,
            "nonce": nonce,
            "issued_at_unix": issued_at_unix,
            "expires_at_unix": expires_at_unix,
            "capability_epoch_sha256": capability_epoch_sha256,
        }

    def _verify_and_consume_api_approval_authority(
        self,
        authority: Any,
        *,
        session_id: str,
        approval_id: str,
        choice: str,
        capability_epoch_sha256: str,
        run_id: str = "",
        request: Any = None,
    ) -> Optional["web.Response"]:
        """Validate and atomically consume one exact positive-approval proof."""

        if not self._approval_authority_configured():
            return web.json_response(
                _openai_error(
                    "Positive approval requires configured owner authority",
                    code="approval_owner_authority_unavailable",
                ),
                status=403,
            )
        verifier_mode = self._approval_passkey_verifier is not None
        expected_fields = {
            "schema",
            "nonce",
            "issued_at_unix",
            "expires_at_unix",
            "capability_epoch_sha256",
            "passkey" if verifier_mode else "signature",
        }
        if not isinstance(authority, Mapping) or set(authority) != expected_fields:
            return web.json_response(
                _openai_error(
                    "Owner approval authority is malformed",
                    code="approval_owner_authority_invalid",
                ),
                status=403,
            )

        nonce = authority.get("nonce")
        issued_at = authority.get("issued_at_unix")
        expires_at = authority.get("expires_at_unix")
        authority_epoch = authority.get("capability_epoch_sha256")
        proof = authority.get("passkey" if verifier_mode else "signature")
        expected_schema = (
            API_APPROVAL_PASSKEY_AUTHORITY_SCHEMA
            if verifier_mode
            else API_APPROVAL_AUTHORITY_SCHEMA
        )
        if (
            authority.get("schema") != expected_schema
            or not isinstance(nonce, str)
            or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
            or type(issued_at) is not int
            or type(expires_at) is not int
            or not isinstance(authority_epoch, str)
            or re.fullmatch(r"[0-9a-f]{64}", authority_epoch) is None
            or authority_epoch != capability_epoch_sha256
            or not isinstance(proof, str)
            or (
                not verifier_mode
                and re.fullmatch(r"[0-9a-f]{64}", proof) is None
            )
        ):
            return web.json_response(
                _openai_error(
                    "Owner approval authority is not bound to this request",
                    code="approval_owner_authority_invalid",
                ),
                status=403,
            )

        now = int(time.time())
        if (
            expires_at <= issued_at
            or expires_at - issued_at > API_APPROVAL_AUTHORITY_MAX_TTL_SECONDS
            or issued_at > now + API_APPROVAL_AUTHORITY_CLOCK_SKEW_SECONDS
            or expires_at < now
        ):
            return web.json_response(
                _openai_error(
                    "Owner approval authority expired or has an invalid TTL",
                    code="approval_owner_authority_expired",
                ),
                status=409,
            )

        payload = self._api_approval_authority_payload(
            session_id=session_id,
            approval_id=approval_id,
            choice=choice,
            nonce=nonce,
            issued_at_unix=issued_at,
            expires_at_unix=expires_at,
            capability_epoch_sha256=authority_epoch,
            run_id=run_id,
            schema=expected_schema,
        )
        if verifier_mode:
            if not self._approval_verifier_transport_allowed(request):
                return web.json_response(
                    _openai_error(
                        "Owner passkey requires an authenticated loopback or TLS request",
                        code="approval_owner_transport_invalid",
                    ),
                    status=403,
                )
            verifier = self._approval_passkey_verifier
            assert verifier is not None
            proof_valid = api_approval_passkey_matches(verifier, proof)
        else:
            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            expected_signature = hmac.new(
                self._approval_passkey.encode("utf-8"),
                canonical,
                hashlib.sha256,
            ).hexdigest()
            proof_valid = hmac.compare_digest(proof, expected_signature)
        if not proof_valid:
            return web.json_response(
                _openai_error(
                    "Owner approval authority signature is invalid",
                    code="approval_owner_authority_invalid",
                ),
                status=403,
            )

        replay_key = hashlib.sha256(
            f"{expected_schema}:{nonce}".encode("utf-8")
        ).hexdigest()
        with self._api_approvals_lock:
            self._api_consumed_approval_nonces = {
                key: expiry
                for key, expiry in self._api_consumed_approval_nonces.items()
                if expiry >= now
            }
            if replay_key in self._api_consumed_approval_nonces:
                return web.json_response(
                    _openai_error(
                        "Owner approval authority nonce was already consumed",
                        code="approval_owner_authority_replayed",
                    ),
                    status=409,
                )
            self._api_consumed_approval_nonces[replay_key] = float(expires_at)
        return None

    def _approval_verifier_transport_allowed(self, request: Any) -> bool:
        """Require TLS or a real loopback peer for plaintext passkey proof."""

        if request is None:
            return False
        try:
            if bool(request.secure):
                return True
        except Exception:
            pass
        if self._host not in {"127.0.0.1", "::1", "localhost"}:
            return False
        peer = ""
        try:
            transport = request.transport
            raw_peer = (
                transport.get_extra_info("peername")
                if transport is not None
                else None
            )
            if isinstance(raw_peer, (tuple, list)) and raw_peer:
                peer = str(raw_peer[0])
        except Exception:
            peer = ""
        if not peer:
            try:
                peer = str(request.remote or "")
            except Exception:
                peer = ""
        try:
            return ipaddress.ip_address(peer).is_loopback
        except ValueError:
            return False

    def _clear_api_approval_scope(
        self,
        session_id: Optional[str],
        *,
        approval_session_key: str = "",
        cancel_core: bool = False,
        request_authority: Optional[APIRequestScope] = None,
    ) -> None:
        """Drop one conversation's public approvals and optionally deny waits."""

        normalized_session = str(session_id or "")
        normalized_key = str(approval_session_key or "")
        session_scope = (
            self._api_request_scope(
                "approval-session",
                normalized_session,
                authority=request_authority,
            )
            if normalized_session
            else None
        )
        approvals_lock = getattr(self, "_api_approvals_lock", None)
        pending = getattr(self, "_api_pending_approvals", None)
        if approvals_lock is not None and pending is not None:
            with approvals_lock:
                stale_ids = [
                    approval_id
                    for approval_id, state in pending.items()
                    if (
                        (
                            session_scope is not None
                            and state.get("_request_scope") == session_scope
                        )
                        or (
                            normalized_key
                            and state.get("_approval_session_key")
                            == normalized_key
                        )
                    )
                ]
                for approval_id in stale_ids:
                    pending.pop(approval_id, None)
        if cancel_core and normalized_key:
            try:
                from tools.approval import unregister_gateway_notify

                unregister_gateway_notify(normalized_key)
            except Exception:
                logger.debug("Failed to cancel API approvals", exc_info=True)

    def _make_api_approval_notify(
        self,
        *,
        session_id: str,
        approval_session_key: str,
        event_callback=None,
        request_authority: Optional[APIRequestScope] = None,
    ):
        """Build the exact-ID approval bridge used by ordinary API turns."""

        session_scope = self._api_request_scope(
            "approval-session",
            session_id,
            authority=request_authority,
        )

        def _notify(approval_data: Dict[str, Any]) -> None:
            approval_id = str(approval_data.get("approval_id", "") or "")
            if re.fullmatch(r"[0-9a-f]{32}", approval_id) is None:
                raise RuntimeError("approval core returned an invalid request ID")
            from tools.approval import get_pending_gateway_approvals

            core_state = next(
                (
                    item
                    for item in get_pending_gateway_approvals(
                        approval_session_key,
                        include_authority_binding=True,
                    )
                    if item.get("approval_id") == approval_id
                ),
                {},
            )
            capability_epoch_sha256 = str(
                core_state.get("_capability_epoch_sha256", "") or ""
            )
            if re.fullmatch(r"[0-9a-f]{64}", capability_epoch_sha256) is None:
                raise RuntimeError("approval core returned an invalid authority epoch")

            allow_permanent = bool(approval_data.get("allow_permanent", False))
            allow_session = bool(approval_data.get("allow_session", True))
            choices = (
                ["once", "deny"]
                if not allow_session
                else list(API_APPROVAL_CHOICES)
            )
            if allow_session and not allow_permanent:
                choices.remove("always")
            pattern_keys = [
                str(value) for value in (approval_data.get("pattern_keys") or [])
            ]
            state = {
                "id": approval_id,
                "object": "hermes.approval",
                "status": "pending",
                "session_id": session_id,
                "command": str(approval_data.get("command", "") or ""),
                "description": str(approval_data.get("description", "") or ""),
                "pattern_keys": pattern_keys,
                "allow_permanent": allow_permanent,
                "allow_session": allow_session,
                "choices": choices,
                "owner_authority_required_for": [
                    value for value in choices if value != "deny"
                ],
                "owner_authority_schema": self._approval_authority_schema(),
                "capability_epoch_sha256": capability_epoch_sha256,
                "created_at": time.time(),
                "response_endpoint": f"/v1/approvals/{approval_id}/response",
                "_approval_session_key": approval_session_key,
                "_request_scope": session_scope,
                "_authority_generation": core_state.get(
                    "_authority_generation"
                ),
                "_event_callback": event_callback,
            }
            approval_scope = session_scope.bind(
                "approval-id",
                approval_id,
            )
            with self._api_approvals_lock:
                self._api_pending_approvals[approval_scope] = state
            if event_callback is not None:
                try:
                    event_callback(
                        "approval.request",
                        self._public_api_approval(state),
                    )
                except Exception:
                    # Polling remains authoritative when an SSE subscriber has
                    # gone away.  The pending exact core entry is still live.
                    logger.debug(
                        "API approval event delivery failed", exc_info=True
                    )

        return _notify

    def _api_clarify_scope(
        self,
        session_id: Optional[str],
        *,
        request_authority: Optional[APIRequestScope] = None,
    ) -> str:
        """Namespace API clarify entries away from native gateway sessions."""

        return self._api_request_scope(
            "clarify-session",
            session_id,
            authority=request_authority,
        ).internal_key

    def _clear_api_clarify_scope(
        self,
        scope: str,
        *,
        generation: Optional[int] = None,
    ) -> bool:
        """Cancel one API conversation's pending clarify requests.

        When ``generation`` is supplied, cleanup is exact and cannot consume a
        newer turn's public or core prompt for the same conversation scope.
        """

        if not scope:
            return True
        clarifications_lock = getattr(self, "_api_clarifications_lock", None)
        pending = getattr(self, "_api_pending_clarifications", None)
        if clarifications_lock is not None and pending is not None:
            with clarifications_lock:
                stale_ids = [
                    clarify_id
                    for clarify_id, state in pending.items()
                    if state.get("_scope") == scope
                    and (
                        generation is None
                        or state.get("_core_generation") == generation
                    )
                ]
                for clarify_id in stale_ids:
                    pending.pop(clarify_id, None)
        try:
            from tools.clarify_gateway import clear_session

            clear_session(scope, generation=generation)
            return True
        except Exception:
            logger.debug("Failed to clear API clarify scope", exc_info=True)
            return False

    def _cancel_api_clarify_authority(
        self,
        authority: Optional[_APIClarifyAuthority],
    ) -> None:
        """Fence a live callback and wake only its exact pending prompt."""

        if authority is None:
            return
        with self._api_clarifications_lock:
            authority.accepting = False
            generation = authority.generation
            if generation is not None:
                self._clear_api_clarify_scope(
                    authority.scope,
                    generation=generation,
                )

    def _retire_api_clarify_authority(
        self,
        authority: Optional[_APIClarifyAuthority],
    ) -> bool:
        """Retire exact clarify authority after its worker can no longer run."""

        if authority is None:
            return False
        from tools import clarify_gateway

        with self._api_clarifications_lock:
            if authority.retired:
                return True
            authority.accepting = False
            authority.active = False
            generation = authority.generation
            if generation is None:
                authority.retired = True
                return True
            cleared = self._clear_api_clarify_scope(
                authority.scope,
                generation=generation,
            )
            if not cleared:
                return False
            retired = clarify_gateway.retire_session_generation(
                authority.scope,
                generation,
            )
            if not retired and clarify_gateway.session_generation_retained(
                authority.scope,
                generation,
            ):
                return False
            # ``retire_session_generation`` also returns false when a newer
            # generation already superseded this one.  Exact absence is an
            # equally valid terminal retirement result and must not disturb
            # that newer authority.
            authority.retired = True
            return True

    def _cancel_api_agent_clarifications(self, agent: Any) -> None:
        """Fence an agent's exact turn callback without retiring it early."""

        authority = getattr(agent, "_api_clarify_authority", None)
        if authority is not None:
            self._cancel_api_clarify_authority(authority)
            return
        self._clear_api_clarify_scope(
            str(getattr(agent, "_api_clarify_scope", "") or "")
        )

    def _retire_api_agent_clarifications(self, agent: Any) -> bool:
        """Retire an agent's exact turn callback at worker completion."""

        return self._retire_api_clarify_authority(
            getattr(agent, "_api_clarify_authority", None)
        )

    def _release_api_cached_agent(self, agent: Any) -> None:
        """Soft-release a cache-evicted agent without tearing down task state."""

        if agent is None:
            return
        authority = getattr(agent, "_api_clarify_authority", None)
        if authority is not None and authority.active:
            self._cancel_api_clarify_authority(authority)
        else:
            self._retire_api_clarify_authority(authority)
            if authority is None:
                self._clear_api_clarify_scope(
                    str(getattr(agent, "_api_clarify_scope", "") or "")
                )
        cache_lock = getattr(self, "_api_agent_cache_lock", None)
        active_agents = getattr(self, "_api_active_agents", {})
        deferred_releases = getattr(self, "_api_deferred_agent_releases", None)
        if cache_lock is not None and deferred_releases is not None:
            with cache_lock:
                is_active = bool(
                    (authority is not None and authority.active)
                    or id(agent) in active_agents
                )
                if is_active:
                    deferred_releases.add(id(agent))
        else:
            is_active = False
        if is_active:
            # Session deletion/config invalidation may race a live turn.
            # Fence it from future lookup now, but let the exact execution
            # owner release provider clients after the worker exits.
            return
        if cache_lock is not None and deferred_releases is not None:
            with cache_lock:
                deferred_releases.discard(id(agent))
        try:
            release = getattr(agent, "release_clients", None)
            if callable(release):
                release()
        except Exception:
            logger.debug("Failed to release evicted API agent", exc_info=True)
        finally:
            # A resumed agent reconstructs its transcript from SessionDB.
            # Drop potentially large tool outputs after soft eviction while
            # preserving terminal/browser task state, matching native gateway
            # cache eviction semantics.
            if hasattr(agent, "_session_messages"):
                agent._session_messages = []

    def _pop_cached_api_agent(
        self,
        session_id: Optional[str],
        *,
        expected_agent: Any = None,
        request_authority: Optional[APIRequestScope] = None,
    ) -> Any:
        """Atomically remove and return one exact cached agent."""

        if not session_id:
            return None
        cache_key = self._api_request_scope(
            "agent-session",
            session_id,
            authority=request_authority,
        )
        with self._api_agent_cache_lock:
            entry = self._api_agent_cache.get(cache_key)
            if entry is None:
                return None
            agent = entry.get("agent")
            if expected_agent is not None and agent is not expected_agent:
                return None
            self._api_agent_cache.pop(cache_key, None)
            return agent

    def _retire_api_session_agents(
        self,
        session_id: Optional[str],
        *,
        reason: str,
        request_authority: Optional[APIRequestScope] = None,
    ) -> None:
        """Interrupt and evict every process-local owner of one API session."""

        normalized_session = str(session_id or "")
        if not normalized_session:
            return

        session_scope = self._api_request_scope(
            "agent-session",
            normalized_session,
            authority=request_authority,
        )
        cached_agent = self._pop_cached_api_agent(
            normalized_session,
            request_authority=request_authority,
        )
        with self._api_agent_cache_lock:
            active_agents = [
                agent
                for agent in self._api_active_agents.values()
                if getattr(agent, "_api_agent_session_scope", None)
                == session_scope
            ]

        owned_agents = {
            id(agent): agent
            for agent in [cached_agent, *active_agents]
            if agent is not None
        }
        if not owned_agents:
            self._clear_api_approval_scope(
                normalized_session,
                request_authority=request_authority,
            )
            self._clear_api_clarify_scope(
                self._api_clarify_scope(
                    normalized_session,
                    request_authority=request_authority,
                )
            )
            return

        for agent in owned_agents.values():
            try:
                agent.interrupt(reason)
            except Exception:
                logger.debug(
                    "Failed to interrupt retired API session agent",
                    exc_info=True,
                )
            self._cancel_api_agent_clarifications(agent)
            self._clear_api_approval_scope(
                normalized_session,
                approval_session_key=str(
                    getattr(agent, "_api_approval_session_key", "") or ""
                ),
                cancel_core=True,
                request_authority=(
                    getattr(agent, "_api_request_authority", None)
                    or request_authority
                ),
            )
            self._release_api_cached_agent(agent)

    def _prune_api_agent_cache_locked(self, now: float) -> List[Any]:
        """Prune idle/LRU agents while holding ``_api_agent_cache_lock``."""

        active_ids = set(self._api_active_agents)
        evicted: List[Any] = []
        for cache_key, entry in list(self._api_agent_cache.items()):
            agent = entry.get("agent")
            if id(agent) in active_ids:
                continue
            last_used = float(entry.get("last_used", 0.0) or 0.0)
            if now - last_used > API_AGENT_CACHE_IDLE_TTL_SECONDS:
                self._api_agent_cache.pop(cache_key, None)
                evicted.append(agent)

        while len(self._api_agent_cache) > API_AGENT_CACHE_MAX_SIZE:
            removable_key = None
            for cache_key, entry in self._api_agent_cache.items():
                if id(entry.get("agent")) not in active_ids:
                    removable_key = cache_key
                    break
            if removable_key is None:
                break
            entry = self._api_agent_cache.pop(removable_key)
            evicted.append(entry.get("agent"))
        return evicted

    def _make_api_clarify_callback(
        self,
        session_id: Optional[str],
        notify_callback=None,
        *,
        request_authority: Optional[APIRequestScope] = None,
    ):
        """Bridge the synchronous clarify tool to authenticated API control.

        The existing ``tools.clarify_gateway`` event primitive remains the
        single resolution mechanism.  API clients discover the public pending
        record through ``GET /v1/clarifications`` (and streaming clients also
        receive a structured event), then resolve it through the response
        endpoint.  No prompt text is classified or routed here.
        """

        session_scope = self._api_request_scope(
            "clarify-session",
            session_id,
            authority=request_authority,
        )
        scope = session_scope.internal_key
        authority = _APIClarifyAuthority(scope)

        def _clarify(question: str, choices) -> str:
            from tools import clarify_gateway

            clarify_id = uuid.uuid4().hex
            core_clarify_id = session_scope.bind(
                "clarification-core",
                clarify_id,
            ).internal_key
            normalized_choices = list(choices) if choices else None
            clarification_scope = session_scope.bind(
                "clarification-id",
                clarify_id,
            )
            with self._api_clarifications_lock:
                if (
                    authority.retired
                    or not authority.active
                    or not authority.accepting
                ):
                    return "[clarification unavailable: session ended]"
                if authority.generation is None:
                    authority.generation = (
                        clarify_gateway.claim_session_generation(scope)
                    )
                clarify_generation = authority.generation
                clarify_gateway.register(
                    clarify_id=core_clarify_id,
                    session_key=scope,
                    question=question,
                    choices=normalized_choices,
                    generation=clarify_generation,
                    identity_v1=True,
                )
                public_state = {
                    "id": clarify_id,
                    "object": "hermes.clarification",
                    "status": "pending",
                    "session_id": str(session_id or ""),
                    "question": question,
                    "choices": normalized_choices,
                    "created_at": time.time(),
                    "response_endpoint": (
                        f"/v1/clarifications/{clarify_id}/response"
                    ),
                    "_scope": scope,
                    "_request_scope": session_scope,
                    "_core_clarify_id": core_clarify_id,
                    "_core_generation": clarify_generation,
                }
                self._api_pending_clarifications[
                    clarification_scope
                ] = public_state

            if notify_callback is not None:
                try:
                    notify_callback(self._public_api_clarification(public_state))
                except Exception:
                    # Polling remains a complete delivery path even if an SSE
                    # subscriber disappeared between registration and notify.
                    logger.debug("API clarify event delivery failed", exc_info=True)

            timeout = clarify_gateway.get_clarify_timeout()
            try:
                response = clarify_gateway.wait_for_response(
                    core_clarify_id,
                    timeout=float(timeout),
                    session_key=scope,
                    generation=clarify_generation,
                )
                if response is None or response == "":
                    return f"[user did not respond within {int(timeout / 60)}m]"
                return response
            finally:
                with self._api_clarifications_lock:
                    self._api_pending_clarifications.pop(
                        clarification_scope,
                        None,
                    )

        _clarify._api_clarify_authority = authority
        return _clarify

    @staticmethod
    def _public_api_clarification(state: Mapping[str, Any]) -> Dict[str, Any]:
        """Return the client-safe projection of an internal pending entry."""

        return {
            key: value
            for key, value in state.items()
            if not str(key).startswith("_")
        }

    def _finalize_api_agent_cache_after_turn(
        self,
        *,
        requested_session_id: Optional[str],
        agent: Any,
        result: Any,
        execution_error: Optional[Exception],
        request_authority: Optional[APIRequestScope] = None,
    ) -> Any:
        """Refresh a successful cache entry or fence a terminally failed one.

        Returns an evicted agent for release after it is removed from the
        active-agent registry.  The cache pop happens while the per-session run
        lock is still held, so a queued next turn can never acquire the failed
        instance in the gap.
        """

        if agent is None or not requested_session_id:
            return None
        effective_session_id = getattr(agent, "session_id", requested_session_id)
        outcome = _session_stream_outcome(result)
        reusable = bool(
            execution_error is None
            and outcome.get("completed") is True
            and isinstance(effective_session_id, str)
            and effective_session_id == requested_session_id
            and not getattr(agent, "_interrupt_requested", False)
        )
        if not reusable:
            return self._pop_cached_api_agent(
                requested_session_id,
                expected_agent=agent,
                request_authority=(
                    getattr(agent, "_api_request_authority", None)
                    or request_authority
                ),
            )

        cache_key = self._api_request_scope(
            "agent-session",
            requested_session_id,
            authority=(
                getattr(agent, "_api_request_authority", None)
                or request_authority
            ),
        )
        message_count = self._api_session_message_count(requested_session_id)
        with self._api_agent_cache_lock:
            entry = self._api_agent_cache.get(cache_key)
            if entry is not None and entry.get("agent") is agent:
                entry["message_count"] = message_count
                entry["last_used"] = time.monotonic()
                self._api_agent_cache.move_to_end(cache_key)
        return None

    def active_agent_work_count(self) -> int:
        """Return all live agent work owned by this API adapter.

        ``/v1/runs`` registers an asyncio task before it constructs and stores
        its agent, so ``_active_run_agents`` has a real queued-before-agent gap.
        Reuse the task-based accounting used by the concurrent-run limit: it
        covers that gap and excludes completed tasks retained until cleanup.
        """
        try:
            return int(getattr(self, "_pending_agent_requests", 0)) + int(
                self._active_agent_run_count()
            )
        except Exception:
            return 0

    @staticmethod
    def _gateway_is_draining() -> bool:
        """Whether the owning gateway currently refuses new agent turns."""
        try:
            from gateway.run import _gateway_runner_ref

            runner = _gateway_runner_ref()
            return bool(
                runner
                and (
                    getattr(runner, "_draining", False)
                    or getattr(runner, "_external_drain_active", False)
                )
            )
        except Exception:
            return False

    def _draining_response(self) -> Optional["web.Response"]:
        """Return a retryable response while the gateway drains existing work."""
        if not self._gateway_is_draining():
            return None
        return web.json_response(
            _openai_error(
                "Gateway is draining existing work; retry shortly.",
                code="gateway_draining",
            ),
            status=503,
            headers={"Retry-After": "1"},
        )

    def _activate_admitted_request(self) -> None:
        """Transfer this request's drain reservation to agent bookkeeping."""
        reservation = _api_agent_request_reservation.get()
        if reservation and reservation["active"]:
            reservation["active"] = False
            self._pending_agent_requests = max(0, self._pending_agent_requests - 1)

    def _readiness_work_counts(self) -> tuple[int, int, int]:
        """Return bounded work counts from each subsystem's public state."""
        active_api_runs = sum(
            1
            for status in self._run_statuses.values()
            # "stopping" (set by _handle_stop_run) is not terminal: the run
            # stays in this state, doing real executor-thread work, until the
            # agent actually notices the interrupt and the task settles to
            # "cancelled" — an unbounded window, not the old ~5s hard-timeout
            # wait. Excluding it here undercounts active_api_runs for the
            # whole duration of a cooperative stop.
            if status.get("status") in {"queued", "running", "waiting_for_approval", "stopping"}
        )
        active_api_runs += len(self._api_cleanup_handles)
        process_depth = 0
        active_delegations = 0
        try:
            from tools.process_registry import process_registry

            process_depth = process_registry.completion_queue.qsize()
        except Exception:
            pass
        try:
            from tools.async_delegation import active_count

            active_delegations = active_count()
        except Exception:
            pass
        return active_api_runs, process_depth, active_delegations

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """Normalize configured CORS origins into a stable tuple."""
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    @staticmethod
    def _resolve_max_concurrent_runs() -> int:
        """Read the concurrent-run cap from config.yaml (0 disables).

        gateway.api_server.max_concurrent_runs. Falls back to the historical
        default of 10 when unset or malformed. Negative values are clamped
        to 0 (disabled).
        """
        default = 10
        try:
            from hermes_cli.config import (
                PinnedEffectiveConfigError,
                cfg_get,
                load_config,
            )

            raw = cfg_get(
                load_config(),
                "gateway",
                "api_server",
                "max_concurrent_runs",
                default=default,
            )
            value = int(raw)
        except PinnedEffectiveConfigError:
            # The isolated process pin is an authority boundary. Falling back
            # to 10 here could widen a sealed max_concurrent_runs: 1 policy.
            raise
        except Exception:
            return default
        return max(0, value)

    @staticmethod
    def _resolve_model_name(explicit: str) -> str:
        """Derive the advertised model name for /v1/models.

        Priority:
        1. Explicit override (config extra or API_SERVER_MODEL_NAME env var)
        2. Active profile name (so each profile advertises a distinct model)
        3. Fallback: "hermes-agent"

        Delegates the tiered fallthrough to
        :func:`hermes_cli.model_switch.resolve_effective_model` (the shared
        override > mid-tier > default precedence owner).
        """
        from hermes_cli.model_switch import resolve_effective_model

        profile_name = ""
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
            if profile and profile not in {"default", "custom"}:
                profile_name = profile
        except Exception:
            pass
        return resolve_effective_model(explicit, profile_name, "hermes-agent")

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """Return CORS headers for an allowed browser origin."""
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """Allow non-browser clients and explicitly configured browser origins."""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    @staticmethod
    def _clean_log_value(value: Any, *, max_len: int = 200) -> str:
        """Sanitize request metadata before it reaches security logs."""
        if value is None:
            return ""
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        return text[:max_len]

    def _request_audit_context(self, request: "web.Request") -> Dict[str, str]:
        """Return non-secret source metadata for security/audit warnings."""
        peer_ip = ""
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if isinstance(peer, (tuple, list)) and peer:
                peer_ip = str(peer[0])
        except Exception:
            peer_ip = ""

        return {
            "remote": self._clean_log_value(getattr(request, "remote", "") or peer_ip),
            "peer_ip": self._clean_log_value(peer_ip),
            "forwarded_for": self._clean_log_value(request.headers.get("X-Forwarded-For", "")),
            "real_ip": self._clean_log_value(request.headers.get("X-Real-IP", "")),
            "method": self._clean_log_value(request.method, max_len=16),
            "path": self._clean_log_value(request.path_qs, max_len=500),
            "user_agent": self._clean_log_value(request.headers.get("User-Agent", ""), max_len=300),
        }

    def _request_audit_log_suffix(self, request: "web.Request") -> str:
        ctx = self._request_audit_context(request)
        fields = [f"{key}={value!r}" for key, value in ctx.items() if value]
        return " ".join(fields) if fields else "source='unknown'"

    def _cron_origin_from_request(self, request: "web.Request") -> Dict[str, str]:
        """Persist safe API source metadata on cron jobs created over HTTP."""
        ctx = self._request_audit_context(request)
        origin = {
            "platform": "api_server",
            "chat_id": "api",
        }
        if ctx.get("remote"):
            origin["source_ip"] = ctx["remote"]
        if ctx.get("peer_ip"):
            origin["peer_ip"] = ctx["peer_ip"]
        if ctx.get("forwarded_for"):
            origin["forwarded_for"] = ctx["forwarded_for"]
        if ctx.get("real_ip"):
            origin["real_ip"] = ctx["real_ip"]
        if ctx.get("user_agent"):
            origin["user_agent"] = ctx["user_agent"]
        return origin

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _api_auth_configured(self) -> bool:
        return bool(self._api_key) or self._api_bearer_verifier is not None

    def _approval_authority_configured(self) -> bool:
        return (
            bool(self._approval_passkey)
            or self._approval_passkey_verifier is not None
        )

    def _approval_authority_schema(self) -> str:
        if self._approval_passkey_verifier is not None:
            return API_APPROVAL_PASSKEY_AUTHORITY_SCHEMA
        return API_APPROVAL_AUTHORITY_SCHEMA

    def _expected_api_key(self) -> str:
        """Return the API key authorized for the URL-selected profile."""
        profile = _api_request_profile.get()
        if not profile or profile == "default":
            return self._api_key

        try:
            from agent.secret_scope import get_secret
            from hermes_cli.auth import has_usable_secret

            key = get_secret("API_SERVER_KEY", "") or ""
            if not has_usable_secret(key, min_length=16):
                return ""
            return key
        except Exception as exc:
            # Fail closed if the profile scope or strength guard cannot resolve
            # the credential. Do not log the key or exception text.
            logger.warning(
                "Failed to resolve a usable profile-scoped API_SERVER_KEY for %r: %s",
                profile,
                type(exc).__name__,
            )
            return ""

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        connect() refuses to start the API server without API_SERVER_KEY, so
        the no-key branch only exists for tests or unsupported manual wiring.
        """
        profile = _api_request_profile.get()
        is_named_profile = bool(profile and profile != "default")
        expected_key = self._expected_api_key()
        if is_named_profile and not expected_key:
            # Preserve the historical no-key test/manual-wiring behavior only
            # for the default listener. Named profiles must fail closed rather
            # than inherit the listener owner's key.
            if not is_named_profile:
                return None
            logger.warning(
                "API server rejected request for profile %r: no profile-scoped "
                "API_SERVER_KEY is configured; %s",
                profile,
                self._request_audit_log_suffix(request),
            )
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid gateway API key (API_SERVER_KEY)",
                        "type": "gateway_auth_error",
                        "code": "gateway_auth_failed",
                    }
                },
                status=401,
            )
        if not is_named_profile and not self._api_auth_configured():
            return None

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            # Compare as bytes: ``hmac.compare_digest`` raises TypeError on a
            # str containing non-ASCII characters, and ``token`` is the raw
            # client-supplied header. A stray non-ASCII byte in the key would
            # otherwise crash this handler (500) instead of returning a clean
            # 401. Encoding both sides keeps the timing-safe comparison and
            # matches web_server.py's dashboard-token check.
            legacy_valid = bool(expected_key) and hmac.compare_digest(
                token.encode(),
                expected_key.encode(),
            )
            verifier_valid = (
                not is_named_profile
                and
                self._api_bearer_verifier is not None
                and api_bearer_matches(self._api_bearer_verifier, token)
            )
            if legacy_valid or verifier_valid:
                return None  # Auth OK

        logger.warning(
            "API server rejected invalid API key: %s",
            self._request_audit_log_suffix(request),
        )
        return web.json_response(
            {"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error", "code": "gateway_auth_failed"}},
            status=401,
        )

    @staticmethod
    def _normalize_callback_platform(value: str) -> str:
        normalized = (value or "").strip().lower().replace("-", "_")
        if not re.fullmatch(r"[a-z0-9_]+", normalized):
            return ""
        return normalized

    def _get_platform_callback_adapter(
        self,
        request: "web.Request",
        platform_name: str,
    ) -> Optional[Any]:
        authority = _api_request_authority.get()
        profile = (
            authority.profile
            if authority is not None
            else (_api_request_profile.get() or "default")
        )
        named_multiplex_profile = bool(
            self._api_multiplex_enabled() and profile != "default"
        )

        injected = request.app.get("platform_event_adapters")
        if isinstance(injected, dict):
            scoped_injected = injected.get(profile)
            if isinstance(scoped_injected, dict):
                adapter = scoped_injected.get(platform_name)
            elif named_multiplex_profile:
                adapter = None
            else:
                adapter = injected.get(platform_name)
            if adapter is not None:
                return adapter

        if not named_multiplex_profile:
            adapter = request.app.get(f"{platform_name}_adapter")
            if adapter is not None:
                return adapter

        runner = self.gateway_runner or request.app.get("gateway_runner")
        if named_multiplex_profile:
            profile_adapters = getattr(runner, "_profile_adapters", {})
            adapters = (
                profile_adapters.get(profile)
                if isinstance(profile_adapters, dict)
                else None
            )
        else:
            adapters = getattr(runner, "adapters", None)
        if not adapters:
            return None

        try:
            from gateway.config import Platform as _Platform
            return adapters.get(_Platform(platform_name))
        except Exception:
            for platform, candidate in adapters.items():
                if getattr(platform, "value", platform) == platform_name:
                    return candidate
        return None

    async def _handle_platform_event_callback(self, request: "web.Request") -> "web.Response":
        platform_name = self._normalize_callback_platform(
            request.match_info.get("platform", "")
        )
        if not platform_name:
            return web.json_response(
                _openai_error(
                    "Invalid platform name",
                    code="invalid_platform",
                ),
                status=400,
            )

        adapter = self._get_platform_callback_adapter(request, platform_name)
        if adapter is None:
            return web.json_response(
                _openai_error(
                    "Platform adapter is not connected",
                    code="platform_unavailable",
                ),
                status=503,
            )

        verifier = getattr(adapter, "verify_http_event_request", None)
        dispatcher = getattr(adapter, "dispatch_http_event", None)
        if verifier is None or dispatcher is None:
            return web.json_response(
                _openai_error(
                    "Platform adapter does not support HTTP events",
                    code="platform_http_events_unsupported",
                ),
                status=503,
            )

        auth_header = request.headers.get("Authorization", "")
        try:
            if asyncio.iscoroutinefunction(verifier):
                ok, code = await verifier(auth_header)
            else:
                # Platform verifiers may do blocking network I/O (e.g. Google
                # signing-cert fetches) — keep that off the event loop.
                ok, code = await asyncio.to_thread(verifier, auth_header)
        except Exception:
            # Fail closed: a crashing verifier must never admit the event.
            logger.exception(
                "Platform HTTP event verifier failed for %s", platform_name
            )
            ok, code = False, "platform_event_verifier_error"
        if not ok:
            return web.json_response(
                _openai_error(
                    "Invalid platform event authorization",
                    code=code or "invalid_platform_event_authorization",
                ),
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                _openai_error("Invalid JSON in platform event", code="invalid_json"),
                status=400,
            )

        if not isinstance(payload, dict):
            return web.json_response(
                _openai_error(
                    "Platform event must be a JSON object",
                    code="invalid_request",
                ),
                status=400,
            )

        try:
            result = await dispatcher(payload)
        except Exception:
            logger.exception("Platform HTTP event dispatch failed for %s", platform_name)
            return web.json_response(
                _openai_error(
                    "Platform event dispatch failed",
                    err_type="server_error",
                    code="platform_event_dispatch_failed",
                ),
                status=500,
            )

        return web.json_response(result if isinstance(result, dict) else {})

    # ------------------------------------------------------------------
    # Frozen single-profile listener authority
    # ------------------------------------------------------------------

    def _api_multiplex_enabled(self) -> bool:
        runner = getattr(self, "gateway_runner", None)
        cfg = getattr(runner, "config", None)
        if bool(getattr(cfg, "multiplex_profiles", False)):
            # GatewayRunner rejects this at construction. Keep the adapter
            # boundary equally strict for direct/embedded construction where
            # a caller attaches a runner-like object only after __init__.
            from gateway.run import _require_single_profile_gateway_process

            _require_single_profile_gateway_process(cfg)
        return False

    def _freeze_api_profile_inventory(
        self,
    ) -> tuple[APIProfileIdentity, ...]:
        """Return the one listener-lifetime profile inventory snapshot."""

        lock = getattr(self, "_api_profile_inventory_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._api_profile_inventory_lock = lock
        with lock:
            frozen = getattr(self, "_api_profile_inventory", None)
            if frozen is not None:
                # The tuple was validated when GatewayRunner injected it (or
                # when this standalone adapter captured it).  Per-request
                # scope resolution reverifies the selected identity, so
                # reopening every served profile marker here would add
                # all-profile I/O to every request.
                return frozen

            runner = getattr(self, "gateway_runner", None)
            runner_freeze = getattr(
                runner,
                "_freeze_served_profile_inventory",
                None,
            )
            if callable(runner_freeze):
                # GatewayRunner is the sole owner of the listener snapshot.
                # Keep the exact tuple (and exact identity objects) it hands
                # us; validation below verifies it without creating a second
                # authority snapshot.
                frozen = runner_freeze()
                frozen = validate_api_profile_inventory(frozen)
            else:
                # A directly constructed adapter (unit tests and supported
                # standalone embedding) has no GatewayRunner to own this
                # lifecycle, so it must bootstrap its own one-time snapshot.
                from hermes_cli.profiles import profiles_to_serve

                frozen = freeze_api_profile_inventory(
                    profiles_to_serve(
                        multiplex=False,
                    )
                )
                frozen = validate_api_profile_inventory(frozen)
            self._rebind_startup_response_store(frozen)
            self._api_profile_inventory = frozen
            return frozen

    def _rebind_startup_response_store(
        self,
        inventory: tuple[APIProfileIdentity, ...],
    ) -> None:
        """Reopen a pre-listener ResponseStore after profile replacement."""

        original_identity = getattr(
            self,
            "_response_store_default_identity",
            None,
        )
        original_store = getattr(self, "_response_store", None)
        if original_identity is None or original_store is None:
            return
        current_identity = next(
            (
                identity
                for identity in inventory
                if identity.profile == original_identity.profile
            ),
            None,
        )
        if current_identity is None:
            current_identity = next(
                (
                    identity
                    for identity in inventory
                    if identity.canonical_home
                    == original_identity.canonical_home
                ),
                None,
            )
        if current_identity is None:
            raise APIRequestScopeError(
                "API ResponseStore owner is not in the listener inventory"
            )
        if current_identity == original_identity:
            return

        replacement = ResponseStore(
            max_size=getattr(
                original_store,
                "_max_size",
                MAX_STORED_RESPONSES,
            ),
            db_path=str(
                Path(current_identity.canonical_home) / "response_store.db"
            ),
        )
        try:
            original_store.close()
        except Exception:
            replacement.close()
            raise
        # Publish the replacement only after both its open and the old
        # handle's close succeeded. A failed close must never leave adapter
        # attributes pointing at the replacement we just closed.
        self._response_store = replacement
        self._response_store_default_home = current_identity.canonical_home
        self._response_store_default_identity = current_identity

    def _api_request_scope(
        self,
        kind: str,
        raw_id: object = "",
        *,
        authority: Optional[APIRequestScope] = None,
    ) -> APIRequestScope:
        """Bind an internal identity to the current host-owned request scope.

        Middleware supplies the immutable authority on real HTTP paths.
        Direct/internal callers fall back to the same served-profile
        validation so tests and non-HTTP entry points cannot mint a profile
        from an arbitrary path.
        """

        base = authority or _api_request_authority.get()
        if base is not None:
            return base.bind(kind, raw_id)

        inventory = self._freeze_api_profile_inventory()
        if len(inventory) != 1:
            raise APIRequestScopeError(
                "single-profile API listener requires exactly one frozen "
                "profile identity"
            )
        identity = inventory[0]
        verify_api_profile_identity(identity)
        return APIRequestScope(
            canonical_home=identity.canonical_home,
            source_home=identity.source_home,
            profile=identity.profile,
            profile_generation=identity.profile_generation,
            kind=kind,
            raw_id=str(raw_id or ""),
        )

    def _api_internal_session_key(
        self,
        raw_key: object,
        *,
        kind: str,
        authority: Optional[APIRequestScope] = None,
    ) -> str:
        return self._api_request_scope(
            kind,
            raw_key,
            authority=authority,
        ).internal_session_key(raw_key, kind=kind)

    def _resolve_request_profile(self, request: "web.Request"):
        """Reject profile-prefixed requests; one listener owns one profile."""

        self._api_multiplex_enabled()
        profile = (request.match_info.get("profile") or "").strip()
        return _PROFILE_REJECTED if profile else None

    def _profile_scope(self, profile: Optional[str]):
        """Enter one exact listener-frozen profile runtime scope."""

        inventory = self._freeze_api_profile_inventory()
        if len(inventory) != 1:
            raise APIRequestScopeError(
                "single-profile API listener requires exactly one frozen "
                "profile identity"
            )
        identity = inventory[0]
        requested = str(profile or "").strip()
        if requested and requested != identity.profile:
            raise APIRequestScopeError(
                f"API profile {requested!r} is not served by this listener"
            )
        verify_api_profile_identity(identity)
        from gateway.run import _profile_runtime_scope

        return _profile_runtime_scope(Path(identity.canonical_home))

    def _make_profile_prefix_middleware(self):
        """Bind the listener's one frozen profile for the whole request."""

        @web.middleware
        async def profile_prefix_middleware(request: "web.Request", handler):
            try:
                self._api_multiplex_enabled()
                inventory = self._freeze_api_profile_inventory()
                if len(inventory) != 1:
                    raise APIRequestScopeError(
                        "single-profile API listener requires exactly one "
                        "frozen profile identity"
                    )
                identity = inventory[0]
                verify_api_profile_identity(identity)
                authority = APIRequestScope(
                    canonical_home=identity.canonical_home,
                    source_home=identity.source_home,
                    profile=identity.profile,
                    profile_generation=identity.profile_generation,
                    kind="request",
                    raw_id="",
                )
            except APIProfileGenerationError:
                return web.json_response(
                    _openai_error(
                        "API profile changed after listener startup; restart "
                        "the gateway before serving more requests.",
                        err_type="server_error",
                        code="profile_restart_required",
                    ),
                    status=503,
                )
            except APIRequestScopeError:
                return web.json_response(
                    {"error": "Unknown or unconfigured profile"},
                    status=404,
                )

            profile_token = _api_request_profile.set(None)
            authority_token = _api_request_authority.set(authority)
            try:
                # Both multiplex and single-profile listeners execute under
                # the exact frozen home.  Leaving the single-profile branch
                # ambient would let a retargeted HERMES_HOME/config alias read
                # tenant B while its database authority remained tenant A.
                from gateway.run import _profile_runtime_scope

                runtime_scope = _profile_runtime_scope(
                    Path(authority.canonical_home)
                )
                with runtime_scope:
                    return await handler(request)
            finally:
                _api_request_authority.reset(authority_token)
                _api_request_profile.reset(profile_token)

        return profile_prefix_middleware

    def _http_route_table(self) -> List[tuple]:
        """Return native (method, path, handler) rows registered by ``connect()``."""
        routes: List[tuple] = [
            ("GET", "/health", self._handle_health),
            ("GET", "/health/detailed", self._handle_health_detailed),
            ("GET", "/v1/health", self._handle_health),
            ("GET", "/v1/models", self._handle_models),
            ("GET", "/api/model/options", self._handle_model_options),
            ("GET", "/v1/capabilities", self._handle_capabilities),
            ("GET", "/v1/skills", self._handle_skills),
            ("GET", "/v1/toolsets", self._handle_toolsets),
            ("GET", "/api/sessions", self._handle_list_sessions),
            ("POST", "/api/sessions", self._handle_create_session),
            ("GET", "/api/sessions/{session_id}", self._handle_get_session),
            ("PATCH", "/api/sessions/{session_id}", self._handle_patch_session),
            ("DELETE", "/api/sessions/{session_id}", self._handle_delete_session),
            ("GET", "/api/sessions/{session_id}/messages", self._handle_session_messages),
            ("POST", "/api/sessions/{session_id}/fork", self._handle_fork_session),
            ("POST", "/api/sessions/{session_id}/chat", self._handle_session_chat),
            ("POST", "/api/sessions/{session_id}/chat/stream", self._handle_session_chat_stream),
            ("POST", "/api/sessions/{session_id}/model", self._handle_session_model_lock),
            ("GET", "/api/delegations/{delegation_id}", self._handle_get_delegation),
            ("POST", "/v1/chat/completions", self._handle_chat_completions),
            ("POST", "/v1/responses", self._handle_responses),
            ("GET", "/v1/responses/{response_id}", self._handle_get_response),
            ("DELETE", "/v1/responses/{response_id}", self._handle_delete_response),
            # Generic platform HTTP event callback ingress. Authenticated by
            # the target adapter's own verifier (platform-signed bearer), NOT
            # API_SERVER_KEY — external platforms hold no API server key.
            ("POST", "/api/platforms/{platform}/events", self._handle_platform_event_callback),
            ("GET", "/api/jobs", self._handle_list_jobs),
            ("POST", "/api/jobs", self._handle_create_job),
            ("GET", "/api/jobs/{job_id}", self._handle_get_job),
            ("PATCH", "/api/jobs/{job_id}", self._handle_update_job),
            ("DELETE", "/api/jobs/{job_id}", self._handle_delete_job),
            ("POST", "/api/jobs/{job_id}/pause", self._handle_pause_job),
            ("POST", "/api/jobs/{job_id}/resume", self._handle_resume_job),
            ("POST", "/api/jobs/{job_id}/run", self._handle_run_job),
            ("POST", "/v1/runs", self._handle_runs),
            ("GET", "/v1/runs/{run_id}", self._handle_get_run),
            ("GET", "/v1/runs/{run_id}/events", self._handle_run_events),
            ("POST", "/v1/runs/{run_id}/approval", self._handle_run_approval),
            ("POST", "/v1/runs/{run_id}/stop", self._handle_stop_run),
        ]
        if _CRON_AVAILABLE:
            # Chronos managed-cron fire webhook (NAS → agent). Authenticated
            # by a NAS-minted JWT (NOT API_SERVER_KEY).
            routes.append(("POST", "/api/cron/fire", self._handle_cron_fire))
        return routes

    # ------------------------------------------------------------------
    # Session header helpers
    # ------------------------------------------------------------------

    # Soft length cap for session identifiers.  Headers are bounded in
    # aggregate by aiohttp (``client_max_size`` / default 8 KiB per
    # header), but we impose a tighter limit on the session headers so a
    # caller can't burn memory by passing a multi-kilobyte "session key".
    # 256 chars is well above any realistic stable channel identifier
    # (e.g. ``agent:main:webui:dm:user-42``) while staying small enough
    # that the sanitized form is safe to pass into Honcho / state.db.
    _MAX_SESSION_HEADER_LEN = MAX_API_SESSION_ID_LENGTH

    def _parse_session_key_header(
        self, request: "web.Request"
    ) -> tuple[Optional[str], Optional["web.Response"]]:
        """Extract and validate the ``X-Hermes-Session-Key`` header.

        The session key is a stable per-channel identifier that scopes
        long-term memory (e.g. Honcho sessions) across transcripts.  It
        is independent of ``X-Hermes-Session-Id``: callers may send
        either, both, or neither.

        Returns ``(session_key, None)`` on success (with an empty/absent
        header yielding ``None`` for the key), or ``(None, error_response)``
        on validation failure.

        Security: like session continuation, accepting a caller-supplied
        memory scope requires API-key authentication so that an
        unauthenticated client on a local-only server can't inject itself
        into another user's long-term memory scope by guessing a key.
        """
        raw = request.headers.get("X-Hermes-Session-Key", "").strip()
        if not raw:
            return None, None

        if not self._api_auth_configured():
            logger.warning(
                "X-Hermes-Session-Key rejected: no API key configured. "
                "Set API_SERVER_KEY to enable long-term memory scoping."
            )
            return None, web.json_response(
                _openai_error(
                    "X-Hermes-Session-Key requires API key authentication. "
                    "Configure API_SERVER_KEY to enable this feature."
                ),
                status=403,
            )

        # Reject control characters that could enable header injection on
        # the echo path.
        if re.search(r'[\r\n\x00]', raw):
            return None, web.json_response(
                {"error": {"message": "Invalid session key", "type": "invalid_request_error"}},
                status=400,
            )

        if len(raw) > self._MAX_SESSION_HEADER_LEN:
            return None, web.json_response(
                {"error": {"message": "Session key too long", "type": "invalid_request_error"}},
                status=400,
            )

        return raw, None

    # ------------------------------------------------------------------
    # Session DB helper
    # ------------------------------------------------------------------

    def _session_db_pool_key(
        self,
        home: Path,
        *,
        request_authority: Optional[APIRequestScope] = None,
    ) -> tuple[str, APIRequestScope]:
        authority = (
            request_authority
            or _api_request_authority.get()
            or self._api_request_scope("request")
        )
        canonical_home = str(Path(home).expanduser().resolve())
        if canonical_home != authority.canonical_home:
            raise APIRequestScopeError(
                "SessionDB home does not match immutable API request scope"
            )
        verify_api_request_scope(authority)
        return (
            authority.bind("session-db", "").internal_key,
            authority,
        )

    def _open_and_cache_session_db(
        self,
        home,
        *,
        request_authority: Optional[APIRequestScope] = None,
    ) -> Optional[Any]:
        """Sync core: return the cached SessionDB for ``home``, opening it once.

        Shared by the sync (``_ensure_session_db``) and async
        (``_ensure_session_db_async``) entry points so both honor the same
        per-profile cache. Deliberately does NOT write into ``self._session_db``
        — that stays reserved for an explicit test/manual override, so the first
        profile served can't pin every later request to its DB.
        """
        from hermes_state import SessionDB

        canonical_home = Path(home).expanduser().resolve()
        key, _authority = self._session_db_pool_key(
            canonical_home,
            request_authority=request_authority,
        )
        lock = getattr(self, "_session_dbs_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._session_dbs_lock = lock
        with lock:
            cache = getattr(self, "_session_dbs", None)
            if cache is None:
                cache = {}
                self._session_dbs = cache
            db = cache.get(key)
            if db is None:
                db = SessionDB(db_path=canonical_home / "state.db")
                cache[key] = db
                owned = getattr(self, "_owned_session_db_ids", None)
                if owned is None:
                    owned = set()
                    self._owned_session_db_ids = owned
                owned.add(id(db))
            return db

    def _ensure_session_db_for_authority(
        self,
        authority: APIRequestScope,
    ):
        """Return/open one DB after its caller has acquired admission."""
        if self._session_db is not None and not self._api_multiplex_enabled():
            return self._session_db
        return self._open_and_cache_session_db(
            Path(authority.canonical_home),
            request_authority=authority,
        )

    def _ensure_session_db(self):
        """Lazily initialise and return the SessionDB for the active profile home.

        Sessions are persisted to ``state.db`` so that ``hermes sessions list``
        shows API-server conversations alongside CLI and gateway ones.

        The listener middleware binds the process-frozen profile runtime scope,
        so the DB cannot drift with ambient configuration. Synchronous: used by
        ``_create_agent`` (itself sync, and run in both loop and worker
        contexts). Request handlers use ``_ensure_session_db_async`` to keep
        the SQLite open off the event loop.
        """
        try:
            authority = (
                _api_request_authority.get()
                or self._api_request_scope("request")
            )
            admission_lock = getattr(
                self,
                "_session_db_offload_lock",
                None,
            )
            if admission_lock is None:
                admission_lock = threading.RLock()
                self._session_db_offload_lock = admission_lock
            # Sync callers (notably worker-side agent creation/coherence) must
            # not obtain even a cached handle after disconnect sealed the
            # adapter pool. The check and handle acquisition are one atomic
            # region with the seal.
            with admission_lock:
                if getattr(self, "_session_db_offloads_closing", False):
                    return None
                return self._ensure_session_db_for_authority(authority)
        except Exception as e:
            logger.debug("SessionDB unavailable for API server: %s", e)
            return None

    async def _ensure_session_db_async(self):
        """Async variant for request handlers: offload the SQLite open/schema
        init off the single aiohttp event-loop thread.

        The active profile home is captured on the loop thread (its runtime
        scope is not visible inside ``asyncio.to_thread``); only the blocking
        construction runs in the worker. A single-flight lock prevents duplicate
        concurrent construction for the same home.
        """
        try:
            authority = (
                _api_request_authority.get()
                or self._api_request_scope("request")
            )
            # Cached handles follow the same admitted offload path as first
            # open. Returning one directly would bypass the disconnect seal
            # and let a cancellation-resistant request resume against a pool
            # already selected for close.
            return await self._offload_session_db(
                self._ensure_session_db_for_authority,
                authority,
            )
        except Exception as e:
            logger.debug("SessionDB unavailable for API server: %s", e)
            return None

    async def _offload_session_db(self, func, *args, **kwargs):
        """Submit one adapter-pool DB operation under an atomic close seal."""
        lock = getattr(self, "_session_db_offload_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._session_db_offload_lock = lock
        with lock:
            if getattr(self, "_session_db_offloads_closing", False):
                raise RuntimeError("API SessionDB pool is shutting down")
            runner = getattr(self, "gateway_runner", None)
            submit = getattr(runner, "_submit_in_executor_with_context", None)
            if callable(submit):
                future, _worker_future = submit(func, *args, **kwargs)
            else:
                future = asyncio.create_task(
                    asyncio.to_thread(func, *args, **kwargs)
                )
            pending = getattr(self, "_session_db_offload_futures", None)
            if pending is None:
                pending = set()
                self._session_db_offload_futures = pending
            pending.add(future)

        def _discard(_future) -> None:
            with lock:
                current = getattr(self, "_session_db_offload_futures", None)
                if current is not None:
                    current.discard(future)

        future.add_done_callback(_discard)
        # Cancellation belongs to the request wrapper, not the real DB worker.
        # Keep the inner future alive and visible to disconnect's close barrier.
        return await asyncio.shield(future)

    async def _seal_and_wait_session_db_offloads(
        self,
        *,
        timeout: float = 5.0,
    ) -> bool:
        """Reject new pool operations, then await every admitted real worker."""
        self._seal_session_db_offload_admission()
        lock = getattr(self, "_session_db_offload_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._session_db_offload_lock = lock

        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            with lock:
                pending = [
                    future
                    for future in (
                        getattr(self, "_session_db_offload_futures", None) or set()
                    )
                    if not future.done()
                ]
            if not pending:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)

    def _seal_session_db_offload_admission(self) -> None:
        """Atomically forbid new operations against this adapter's DB pool."""
        lock = getattr(self, "_session_db_offload_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._session_db_offload_lock = lock
        with lock:
            self._session_db_offloads_closing = True

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_model_routes(raw: Any) -> Dict[str, Dict[str, Any]]:
        """Validate and normalize the ``model_routes`` config block.

        Accepts a mapping of ``alias -> {model, provider?, api_key?, base_url?}``.
        Invalid shapes are dropped (never raised) so a config typo can't take
        the whole API server down.  Route values are coerced to strings.

        Security: per-route ``api_key`` values are UPSTREAM provider
        credentials (used to call the routed model's backend), not caller
        authentication — callers still authenticate with the global
        API_SERVER_KEY bearer token via ``_check_auth``.  Route api_keys must
        never be logged; only alias names and non-secret fields may appear in
        logs.
        """
        if not isinstance(raw, dict):
            if raw:
                logger.warning(
                    "api_server model_routes ignored: expected a mapping, got %s",
                    type(raw).__name__,
                )
            return {}

        allowed_keys = ("model", "provider", "api_key", "base_url")
        routes: Dict[str, Dict[str, Any]] = {}
        for alias, cfg in raw.items():
            alias_str = str(alias).strip()
            if not alias_str or not isinstance(cfg, dict):
                logger.warning(
                    "api_server model_routes: dropping invalid route entry %r", alias_str or alias
                )
                continue
            route = {
                key: str(cfg[key]).strip()
                for key in allowed_keys
                if cfg.get(key) is not None and str(cfg[key]).strip()
            }
            if not route.get("model"):
                logger.warning(
                    "api_server model_routes: route %r has no 'model'; dropping", alias_str
                )
                continue
            routes[alias_str] = route
        return routes

    def _resolve_route(self, model_alias: Any) -> Optional[Dict[str, Any]]:
        """Return the model_routes entry for *model_alias*, or None."""
        routes = self._active_model_routes()
        if not routes or not isinstance(model_alias, str):
            return None
        route = routes.get(model_alias)
        return dict(route) if isinstance(route, dict) else None

    def _active_api_server_extra(self) -> Dict[str, Any]:
        """Return API settings for the process-frozen request profile."""

        if not _api_request_profile.get():
            return {
                "model_routes": self._model_routes,
                "direct_model_requests": self._direct_model_requests,
            }
        try:
            from gateway.run import _load_gateway_config

            cfg = _load_gateway_config()
        except Exception:
            logger.warning(
                "Could not load profile-scoped API model settings",
                exc_info=True,
            )
            return {}
        if not isinstance(cfg, dict):
            return {}
        gateway_cfg = cfg.get("gateway")
        gateway_cfg = gateway_cfg if isinstance(gateway_cfg, dict) else {}
        gateway_platforms = gateway_cfg.get("platforms")
        gateway_platforms = (
            gateway_platforms
            if isinstance(gateway_platforms, dict)
            else {}
        )
        top_platforms = cfg.get("platforms")
        top_platforms = (
            top_platforms if isinstance(top_platforms, dict) else {}
        )

        # Mirror GatewayConfig's accepted YAML shapes.  Merge the ordinary
        # nested form first, then the top-level compatibility form, then the
        # direct gateway.api_server form.  A named profile must never inherit
        # the listener adapter's default-profile routes when its own config is
        # absent.
        out: Dict[str, Any] = {}
        for node in (
            gateway_platforms.get("api_server"),
            top_platforms.get("api_server"),
            gateway_cfg.get("api_server"),
        ):
            if not isinstance(node, dict):
                continue
            extra = node.get("extra")
            if isinstance(extra, dict):
                out.update(extra)
            for key in (
                "model_routes",
                "direct_model_requests",
                "model_name",
            ):
                if key in node:
                    out[key] = node.get(key)
        return out

    def _active_model_routes(self) -> Dict[str, Dict[str, Any]]:
        extra = self._active_api_server_extra()
        raw = extra.get("model_routes")
        if raw is self._model_routes:
            return self._model_routes
        return self._parse_model_routes(raw)

    def _direct_model_requests_enabled(self) -> bool:
        extra = self._active_api_server_extra()
        return _coerce_request_bool(
            extra.get("direct_model_requests"),
            default=False,
        )

    def _active_model_name(self) -> str:
        """Return the model advertised by the immutable request profile."""

        if not self._api_multiplex_enabled():
            return self._model_name
        authority = _api_request_authority.get()
        profile = (
            authority.profile
            if authority is not None
            else (_api_request_profile.get() or "default")
        )
        extra = self._active_api_server_extra()
        explicit = extra.get("model_name")
        if isinstance(explicit, str) and explicit.strip():
            return self._resolve_model_name(explicit)
        if profile == "default":
            # Preserve the listener owner's explicit environment/config
            # resolution when the default profile has no per-profile override.
            return self._model_name
        return self._resolve_model_name("")

    def _route_alias_for_execution_context(
        self,
        route: Optional[Dict[str, Any]],
        *candidates: Any,
    ) -> str:
        """Find the non-secret config reference for a resolved route."""

        if not isinstance(route, dict):
            return ""
        routes = self._active_model_routes()
        for candidate in candidates:
            if isinstance(candidate, str) and routes.get(candidate) == route:
                return candidate
        for alias, configured in routes.items():
            if configured == route:
                return alias
        return ""

    def _build_api_detached_execution_context(
        self,
        *,
        agent: Any,
        gateway_session_key: Optional[str],
        ephemeral_system_prompt: Optional[str],
        requested_model: Optional[str],
        requested_provider: Optional[str],
        model_options: Optional[Dict[str, Any]],
        route: Optional[Dict[str, Any]],
        session_model: Optional[str],
        requested_runtime: Optional[Dict[str, Any]],
        route_source: str,
        confirmed_runtime_lock: bool,
    ) -> tuple[Optional[Dict[str, Any]], str]:
        """Capture the safe replayable portion of one API execution contract."""

        if isinstance(ephemeral_system_prompt, str) and ephemeral_system_prompt:
            return (
                None,
                "the originating API turn has an ephemeral system prompt "
                "that must never be persisted",
            )
        route_alias = self._route_alias_for_execution_context(
            route,
            requested_model,
            (requested_runtime or {}).get("raw_model")
            if isinstance(requested_runtime, dict)
            else None,
            (requested_runtime or {}).get("model")
            if isinstance(requested_runtime, dict)
            else None,
        )
        if (
            isinstance(route, dict)
            and not route_alias
            and (route.get("api_key") or route.get("base_url"))
        ):
            return (
                None,
                "the originating API route contains request-local transport "
                "state without a safe model-route alias",
            )

        from gateway.api_execution_context import (
            ApiExecutionContextError,
            SCHEMA,
            normalize_api_execution_context,
            transport_semantic_digest,
        )

        try:
            route_semantic_sha256 = (
                transport_semantic_digest(
                    model=route.get("model"),
                    provider=route.get("provider"),
                    base_url=route.get("base_url"),
                    api_mode="",
                )
                if route_alias and isinstance(route, dict)
                else ""
            )
            effective_transport_sha256 = transport_semantic_digest(
                model=getattr(agent, "model", ""),
                provider=getattr(agent, "provider", ""),
                base_url=getattr(agent, "base_url", ""),
                api_mode=getattr(agent, "api_mode", ""),
            )
            context = normalize_api_execution_context(
                {
                    "schema": SCHEMA,
                    "gateway_session_key": str(gateway_session_key or ""),
                    "request_model": str(requested_model or ""),
                    "request_provider": str(requested_provider or ""),
                    "model_options": model_options or {},
                    "route_alias": route_alias,
                    "route_model": (
                        str(route.get("model") or "")
                        if route_alias and isinstance(route, dict)
                        else ""
                    ),
                    "route_provider": (
                        str(route.get("provider") or "")
                        if route_alias and isinstance(route, dict)
                        else ""
                    ),
                    "route_semantic_sha256": route_semantic_sha256,
                    "session_model": str(session_model or ""),
                    "confirmed_runtime_lock": bool(confirmed_runtime_lock),
                    "requested_runtime": requested_runtime or {},
                    "route_source": str(route_source or "global"),
                    "effective_model": str(
                        getattr(agent, "model", "") or ""
                    ),
                    "effective_provider": str(
                        getattr(agent, "provider", "") or ""
                    ),
                    "effective_transport_sha256": (
                        effective_transport_sha256
                    ),
                },
                allow_none=False,
            )
        except ApiExecutionContextError as exc:
            return None, f"the API execution context is not safely replayable: {exc}"
        return context, ""

    def _route_from_api_execution_context(
        self,
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        alias = str(context.get("route_alias") or "")
        if not alias:
            return None
        route = self._resolve_route(alias)
        if not isinstance(route, dict):
            raise _DetachedApiContinuityError(
                f"detached API model route no longer exists: {alias}"
            )
        current_model = self._clean_runtime_id(route.get("model"))
        current_provider = self._clean_runtime_id(
            route.get("provider"),
            max_len=80,
        )
        try:
            from gateway.api_execution_context import (
                transport_semantic_digest,
            )

            current_semantic_sha256 = transport_semantic_digest(
                model=route.get("model"),
                provider=route.get("provider"),
                base_url=route.get("base_url"),
                api_mode="",
            )
        except Exception as exc:
            raise _DetachedApiContinuityError(
                f"detached API model route became unsafe: {alias}"
            ) from exc
        if (
            current_model != str(context.get("route_model") or "")
            or current_provider != str(context.get("route_provider") or "")
            or current_semantic_sha256
            != str(context.get("route_semantic_sha256") or "")
        ):
            raise _DetachedApiContinuityError(
                f"detached API model route changed before completion: {alias}"
            )
        return route

    def _assert_api_execution_context_matches_agent(
        self,
        context: Optional[Dict[str, Any]],
        agent: Any,
    ) -> None:
        if context is None:
            return
        expected_model = str(context.get("effective_model") or "")
        expected_provider = str(context.get("effective_provider") or "")
        actual_model = self._clean_runtime_id(getattr(agent, "model", ""))
        actual_provider = self._clean_runtime_id(
            getattr(agent, "provider", ""),
            max_len=80,
        )
        try:
            from gateway.api_execution_context import (
                transport_semantic_digest,
            )

            actual_transport_sha256 = transport_semantic_digest(
                model=getattr(agent, "model", ""),
                provider=getattr(agent, "provider", ""),
                base_url=getattr(agent, "base_url", ""),
                api_mode=getattr(agent, "api_mode", ""),
            )
        except Exception as exc:
            raise _DetachedApiContinuityError(
                "detached API completion resolved an unsafe transport"
            ) from exc
        if (
            (expected_model and actual_model != expected_model)
            or (expected_provider and actual_provider != expected_provider)
            or actual_transport_sha256
            != str(context.get("effective_transport_sha256") or "")
        ):
            raise _DetachedApiContinuityError(
                "detached API completion resolved a different model route "
                f"(expected {expected_provider or '<default>'}/"
                f"{expected_model or '<default>'}, got "
                f"{actual_provider or '<default>'}/"
                f"{actual_model or '<default>'})"
            )

    @staticmethod
    def _clean_runtime_id(value: Any, *, max_len: int = 200) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text or len(text) > max_len:
            return ""
        if re.search(r"[\r\n\x00]", text):
            return ""
        return text

    @classmethod
    def _split_provider_prefixed_model(cls, model: str) -> tuple[str, str]:
        text = cls._clean_runtime_id(model)
        if "::" in text:
            provider, raw = text.split("::", 1)
            if re.match(r"^[a-zA-Z0-9_.-]{2,64}$", provider) and raw.strip():
                return provider, raw.strip()
        return "", text

    @classmethod
    def _runtime_options_from_model_options(cls, model_options: Any) -> Dict[str, Any]:
        from gateway.api_execution_context import normalize_model_options

        canonical_options = normalize_model_options(model_options)
        runtime_options: Dict[str, Any] = {}
        reasoning = canonical_options.get("reasoning")
        if isinstance(reasoning, dict):
            enabled = reasoning.get("enabled")
            effort = cls._clean_runtime_id(reasoning.get("effort"), max_len=32)
            if enabled is False:
                runtime_options["reasoning_config"] = {"enabled": False}
            elif effort:
                runtime_options["reasoning_config"] = {"enabled": True, "effort": effort}
            elif enabled is True:
                runtime_options["reasoning_config"] = {"enabled": True}
        service_tier = canonical_options.get("service_tier")
        if service_tier:
            runtime_options["service_tier"] = service_tier
        return runtime_options

    def _session_runtime_request_from_body(self, body: Dict[str, Any]) -> Dict[str, Any]:
        from gateway.api_execution_context import (
            normalize_model_identifier,
            normalize_model_options,
            normalize_provider_slug,
        )

        raw_model = normalize_model_identifier(
            body.get("model") or body.get("model_id"),
            field="model",
        )
        raw_provider = normalize_provider_slug(
            body.get("provider") or body.get("provider_id"),
            field="provider",
        )
        raw_model_options = body.get("model_options")
        normalize_model_options(raw_model_options)
        prefixed_provider, split_model = self._split_provider_prefixed_model(raw_model)
        provider = raw_provider or normalize_provider_slug(
            prefixed_provider,
            field="model provider prefix",
        )
        model = split_model or raw_model
        alias_route = self._resolve_route(raw_model) or self._resolve_route(model)
        route = dict(alias_route) if isinstance(alias_route, dict) else None
        route_source = "model_routes" if route else "global"
        if not route and model and model != self._active_model_name():
            route = {"model": model}
            if provider:
                route["provider"] = provider
            route_source = "raw_request"
        elif not route and provider and model:
            route = {"model": model, "provider": provider}
            route_source = "raw_request"
        runtime_options = self._runtime_options_from_model_options(
            raw_model_options
        )
        requested = {"provider": provider, "model": model, "raw_model": raw_model}
        return {
            "requested": requested,
            "route": route,
            "route_source": route_source,
            "runtime_options": runtime_options,
            "require_model_lock": _coerce_request_bool(body.get("require_model_lock"), default=False),
            "model_options": (
                dict(raw_model_options)
                if isinstance(raw_model_options, dict)
                else {}
            ),
        }

    def _runtime_lock_error(self, runtime_request: Dict[str, Any]) -> Optional["web.Response"]:
        if not runtime_request.get("require_model_lock"):
            return None
        requested = runtime_request.get("requested") or {}
        model = self._clean_runtime_id(requested.get("model"))
        provider = self._clean_runtime_id(requested.get("provider"), max_len=80)
        route = runtime_request.get("route")
        if not model and not provider:
            return web.json_response(
                _openai_error("require_model_lock was set but no model/provider was provided", code="missing_model"),
                status=400,
            )
        if not route or runtime_request.get("route_source") == "global":
            return web.json_response(
                _openai_error("Requested Browser model lock cannot be routed; refusing silent global fallback", code="model_lock_unavailable"),
                status=409,
            )
        return None

    async def _persist_session_runtime_lock(
        self,
        session_id: str,
        runtime_request: Dict[str, Any],
    ) -> bool:
        # Persist only a newly confirmed lock. Reusing a stored lock should not
        # rewrite its timestamp/prompt state on every turn, and an ordinary
        # one-off request override must not erase a previously confirmed lock.
        if runtime_request.get("persisted_lock") or not runtime_request.get("require_model_lock"):
            return True
        requested = runtime_request.get("requested") or {}
        model = self._clean_runtime_id(requested.get("model"))
        provider = self._clean_runtime_id(requested.get("provider"), max_len=80)
        if not model and not provider:
            return False
        db = await self._ensure_session_db_async()
        if db is None:
            return False
        try:
            safe_model_options = _normalize_persisted_api_model_options(
                runtime_request.get("model_options")
            )
            await self._offload_session_db(
                db.update_session_runtime_lock,
                session_id,
                model=model or None,
                provider=provider or None,
                model_options=safe_model_options,
                route_source=runtime_request.get("route_source") or "",
                confirmed=bool(runtime_request.get("require_model_lock")),
            )
            return True
        except Exception:
            logger.warning("[%s] failed to persist session runtime lock for %s", self.name, session_id, exc_info=True)
            return False

    @staticmethod
    def _parse_session_model_config(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except Exception:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _runtime_request_from_persisted_session_lock(
        self,
        session: Optional[Dict[str, Any]],
        body: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(session, dict):
            return None
        from gateway.api_execution_context import (
            normalize_model_identifier,
            normalize_model_options,
            normalize_provider_slug,
            normalize_route_source,
        )
        from hermes_state import normalize_session_model_config

        model_config = normalize_session_model_config(
            session.get("model_config"),
            field="persisted API session.model_config",
        ) or {}
        lock = model_config.get("browser_model_lock")
        if not isinstance(lock, dict) or lock.get("confirmed") is not True:
            return None
        model = normalize_model_identifier(
            lock.get("model"),
            field="persisted model lock model",
        )
        provider = normalize_provider_slug(
            lock.get("provider"),
            field="persisted model lock provider",
        )
        if not model and not provider:
            return None
        persisted_route_source = normalize_route_source(
            lock.get("route_source"),
            field="persisted model lock route_source",
        )
        route: Optional[Dict[str, Any]] = None
        if persisted_route_source == "model_routes":
            route = self._resolve_route(model) if model else None
        else:
            route = {"model": model} if model else {}
            if provider:
                route["provider"] = provider
        model_options = (
            body.get("model_options")
            if isinstance(body.get("model_options"), dict)
            else lock.get("model_options")
        )
        safe_model_options = normalize_model_options(model_options)
        return {
            "requested": {
                "provider": provider,
                "model": model,
                "raw_model": model,
            },
            "route": route or None,
            "route_source": "session_model_lock",
            "runtime_options": self._runtime_options_from_model_options(
                safe_model_options
            ),
            "require_model_lock": True,
            "model_options": safe_model_options,
            "persisted_lock": True,
        }

    def _effective_session_runtime_request(
        self,
        *,
        session: Optional[Dict[str, Any]],
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        runtime_request = self._session_runtime_request_from_body(body)
        requested = runtime_request.get("requested") or {}
        if requested.get("model") or requested.get("provider"):
            return runtime_request
        persisted = self._runtime_request_from_persisted_session_lock(session, body)
        return persisted or runtime_request

    @classmethod
    def _sanitize_runtime_metadata(
        cls,
        *,
        runtime: Optional[Dict[str, Any]] = None,
        requested_runtime: Optional[Dict[str, Any]] = None,
        route_source: str = "global",
        model_lock: str = "",
    ) -> Dict[str, Any]:
        payload = dict(runtime or {})
        provider = cls._clean_runtime_id(
            payload.get("provider") or payload.get("provider_id") or payload.get("effective_provider"),
            max_len=80,
        )
        model = cls._clean_runtime_id(payload.get("model") or payload.get("model_id") or payload.get("effective_model"))
        result: Dict[str, Any] = {
            "provider": provider,
            "model": model,
            "route_source": cls._clean_runtime_id(payload.get("route_source") or route_source, max_len=64) or "global",
        }
        if requested_runtime or payload.get("requested"):
            req = requested_runtime or payload.get("requested") or {}
            result["requested"] = {
                "provider": cls._clean_runtime_id(req.get("provider"), max_len=80),
                "model": cls._clean_runtime_id(req.get("model")),
            }
        if model_lock or payload.get("model_lock"):
            result["model_lock"] = cls._clean_runtime_id(model_lock or payload.get("model_lock"), max_len=32)
        return result

    @staticmethod
    def _normalize_session_source(value: Any) -> str:
        text = str(value or "").strip().lower()
        allowed = {"api_server", "hermes_browser", "browser", "cli", "telegram", "discord", "slack", "desktop", "dashboard"}
        if text in allowed:
            return "hermes_browser" if text == "browser" else text
        return "api_server"

    def _session_model_override_for(
        self,
        session_key: Optional[str],
        *,
        request_authority: Optional[APIRequestScope] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the gateway's session ``/model`` override for *session_key*, if any.

        The gateway tracks per-session ``/model`` switches in
        ``GatewayRunner._session_model_overrides``.  API-server requests that
        share such a session key must keep honouring the explicit session
        override even when the request's ``model`` field matches a configured
        route — a user-issued ``/model`` always wins over static config.
        """
        if not session_key:
            return None
        try:
            from gateway.run import _gateway_runner_ref

            runner = self.gateway_runner or _gateway_runner_ref()
            if runner is None:
                return None
            internal_key = self._api_internal_session_key(
                session_key,
                kind="runner-model",
                authority=request_authority,
            )
            try:
                rehydrate = getattr(runner, "_rehydrate_session_model_override", None)
                if callable(rehydrate):
                    rehydrate(internal_key)
            except Exception:
                logger.debug(
                    "api_server failed to rehydrate session /model override for %s",
                    session_key,
                    exc_info=True,
                )
            override = runner._session_model_overrides.get(internal_key)
            return dict(override) if isinstance(override, dict) else None
        except Exception:
            return None

    def _request_route_conflict_error(
        self,
        *,
        session_id: Optional[str],
        gateway_session_key: Optional[str],
        requested_model: Optional[str],
        requested_provider: Optional[str],
        route: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Return a 400-worthy conflict string for ambiguous route/provider mixes."""
        request_provider = _clean_request_string(requested_provider)
        if not request_provider or not isinstance(route, dict):
            return None
        if self._session_model_override_for(gateway_session_key or session_id):
            # Session /model wins over both the route and the request override, so
            # there is no ambiguity to reject on this request path.
            return None

        route_provider = _clean_request_string(route.get("provider"))
        route_api_key = _clean_request_string(route.get("api_key"))
        route_base_url = _clean_request_string(route.get("base_url"))
        route_alias = _clean_request_string(requested_model) or "requested model"

        if route_provider and request_provider != route_provider:
            return (
                f"Model route '{route_alias}' is pinned to provider '{route_provider}'. "
                f"Remove 'provider' or use '{route_provider}'."
            )
        if not route_provider and (route_api_key or route_base_url):
            return (
                f"Model route '{route_alias}' pins route credentials/base_url. "
                "Do not combine it with an explicit 'provider'."
            )
        return None

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        gateway_session_key: Optional[str] = None,
        requested_model: Optional[str] = None,
        requested_provider: Optional[str] = None,
        model_options: Optional[Dict[str, Any]] = None,
        route: Optional[Dict[str, Any]] = None,
        session_model: Optional[str] = None,
        confirmed_runtime_lock: bool = False,
        clarify_notify_callback=None,
        reuse_cached_agent: bool = True,
        request_authority: Optional[APIRequestScope] = None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the hermes-api-server default.

        ``gateway_session_key`` is a stable per-channel identifier supplied
        by the client (via ``X-Hermes-Session-Key``).  Unlike ``session_id``
        which scopes the short-term transcript and rotates on /new, this
        key is meant to persist across transcripts so long-term memory
        providers (e.g. Honcho) can scope their per-chat state correctly
        — matching the semantics of the native gateway's ``session_key``.

        ``route`` is an optional ``model_routes`` entry (per-client model
        routing).  When set — and no session ``/model`` override exists for
        this session — its model/provider/api_key/base_url override the
        global defaults for this agent instance only.

        ``session_model`` is the raw model persisted on a native API session
        row at creation time (``POST /api/sessions {"model": ...}``) when
        that value does not resolve to a ``model_routes`` alias.  Session-chat
        handlers pass either ``route`` (alias hit) or ``session_model`` (raw
        model), never both.  Precedence: session ``/model`` override →
        ``session_model`` → route alias / per-request selection → global.

        ``confirmed_runtime_lock`` marks a backend-acknowledged Browser model
        lock (POST /api/sessions/{id}/model).  A confirmed lock beats the
        session ``/model`` override, disables the global fallback model
        chain, and fails closed if the locked provider's credentials cannot
        be resolved.
        """
        from run_agent import AIAgent
        from gateway.run import (
            _checkpoint_agent_kwargs,
            _current_max_iterations,
            _resolve_runtime_agent_kwargs,
            _resolve_gateway_model,
            _load_gateway_config,
            _isolated_gateway_runtime_active,
            GatewayRunner,
        )
        from hermes_cli.tools_config import _get_platform_tools

        request_authority = (
            request_authority
            or _api_request_authority.get()
            or self._api_request_scope("request")
        )
        agent_session_scope = request_authority.bind(
            "agent-session",
            session_id,
        )
        internal_memory_key = request_authority.internal_session_key(
            gateway_session_key,
            kind="memory",
        )

        # Catch RuntimeError ONLY around this call, not the wider
        # _create_agent()+run_conversation() span --
        # _resolve_runtime_agent_kwargs() is the sole raiser of
        # RuntimeError(format_runtime_provider_error(...)) for provider
        # auth/credential failure.  Re-raising as
        # _ProviderAuthResolutionError lets _run_agent() (and
        # _handle_runs()) distinguish this from an unrelated RuntimeError
        # elsewhere in the call graph.
        try:
            runtime_kwargs = _resolve_runtime_agent_kwargs()
        except RuntimeError as exc:
            raise _ProviderAuthResolutionError(str(exc)) from exc
        reasoning_config = GatewayRunner._load_reasoning_config()
        model = _resolve_gateway_model()

        # When the primary provider's auth fails (expired token / 429 quota
        # cap), _resolve_runtime_agent_kwargs() falls through to the fallback
        # provider chain, whose runtime dict carries its own ``model`` key.
        # Pop it and let it override the config model, mirroring the native
        # gateway path (_resolve_session_agent_runtime in run.py). Otherwise
        # the explicit ``model=model`` below collides with the ``**runtime_kwargs``
        # spread → "got multiple values for keyword argument 'model'", 500ing
        # every /v1/chat/completions request while a fallback is active.
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            model = runtime_model

        request_reasoning_config = _request_reasoning_config(model_options)
        if request_reasoning_config is not None:
            reasoning_config = request_reasoning_config
        request_service_tier = _request_service_tier(model_options)

        request_model = _clean_request_string(requested_model)
        request_provider = _clean_request_string(requested_provider)
        route_model = _clean_request_string(route.get("model")) if isinstance(route, dict) else None
        route_provider = _clean_request_string(route.get("provider")) if isinstance(route, dict) else None
        route_api_key = _clean_request_string(route.get("api_key")) if isinstance(route, dict) else None
        route_base_url = _clean_request_string(route.get("base_url")) if isinstance(route, dict) else None
        if self._require_capability_canary and route is not None:
            raise RuntimeError(
                "capability canary request model routes are forbidden"
            )

        def _resolve_provider_runtime(
            provider: Optional[str],
            *,
            target_model: Optional[str],
            required: bool,
        ) -> Optional[Dict[str, Any]]:
            provider_name = _clean_request_string(provider)
            if not provider_name:
                return None
            try:
                return _resolve_request_runtime_agent_kwargs(
                    provider_name,
                    target_model=target_model or None,
                )
            except Exception as exc:
                try:
                    from gateway.run import _resolve_runtime_agent_kwargs_for_provider

                    return _resolve_runtime_agent_kwargs_for_provider(provider_name)
                except Exception:
                    pass
                if required:
                    # Surface as the typed provider-auth failure so
                    # _run_agent()/_handle_runs() return the controlled
                    # response shape instead of a raw 500.
                    raise _ProviderAuthResolutionError(str(exc)) from exc
                logger.debug(
                    "api_server provider-runtime refresh failed for provider=%s model=%s",
                    provider_name,
                    target_model or "",
                    exc_info=True,
                )
                return None

        # Final precedence mirrors the gateway contract:
        # confirmed Browser model lock → session /model override →
        # session-persisted model (POST /api/sessions {"model": ...}) →
        # model_routes mapping selected by the request model alias → direct
        # per-request provider/model → global defaults.  model_options stay
        # request-scoped regardless of which selection wins.  A confirmed
        # lock is an execution contract: it bypasses the session /model
        # override and fails closed (never reuses global credentials) if
        # its provider cannot be resolved.
        session_key = gateway_session_key or session_id
        session_row_model = _clean_request_string(session_model)
        session_override = None
        if not confirmed_runtime_lock and not self._require_capability_canary:
            # The request authority is already installed in the copied
            # request/executor context.  Keep this call positional so existing
            # platform/test extension seams that replace this hook with a
            # one-argument callable remain compatible.
            session_override = self._session_model_override_for(session_key)
        # Model-string precedence delegates to the shared owner
        # hermes_cli.model_switch.resolve_effective_model (session /model
        # override > session-persisted model > global) — the rule 7dd00bb47d
        # had to re-fix here after it diverged from gateway/run.py.
        from hermes_cli.model_switch import resolve_effective_model
        if session_override:
            override_model = resolve_effective_model(session_override, None, model)
            session_provider = _clean_request_string(session_override.get("provider"))
            current_provider = _clean_request_string(runtime_kwargs.get("provider"))
            provider_runtime = _resolve_provider_runtime(
                session_provider or current_provider,
                target_model=override_model,
                required=False,
            )
            if provider_runtime:
                _apply_runtime_agent_overrides(runtime_kwargs, provider_runtime)
            _apply_runtime_agent_overrides(runtime_kwargs, session_override)
            model = override_model
            if route or request_model or request_provider:
                logger.debug(
                    "api_server request selection skipped: session /model override wins for %s",
                    session_key or "",
                )
        elif session_row_model and not confirmed_runtime_lock:
            # Session-persisted model (raw string that resolved to no route
            # alias).  Pins this session's turns ahead of per-request body
            # values — a session's chosen model is a standing selection,
            # matching the native gateway's session-model semantics.
            current_provider = _clean_request_string(runtime_kwargs.get("provider"))
            provider_runtime = _resolve_provider_runtime(
                current_provider,
                target_model=session_row_model,
                required=False,
            )
            if provider_runtime:
                _apply_runtime_agent_overrides(runtime_kwargs, provider_runtime)
            model = resolve_effective_model(None, session_row_model, model)
            if request_model or request_provider:
                logger.debug(
                    "api_server request selection skipped: session-persisted model wins for %s",
                    session_key or "",
                )
        else:
            if route is not None:
                # The request's ``model`` field selected this route, so its
                # value is the route ALIAS — never usable as a model name.
                # A route with no ``model`` key keeps the global default
                # (pre-existing model_routes behavior).
                effective_model = route_model or model
            else:
                effective_model = request_model or model
            current_provider = _clean_request_string(runtime_kwargs.get("provider"))
            effective_provider = request_provider or route_provider or current_provider
            provider_runtime = None
            if effective_provider and (
                bool(request_provider or route_provider) or effective_model != model
            ):
                provider_runtime = _resolve_provider_runtime(
                    effective_provider,
                    target_model=effective_model,
                    # A confirmed Browser lock fails closed: if the locked
                    # provider cannot be resolved, never fall through to
                    # the previous global provider's credentials.
                    required=bool(request_provider) or confirmed_runtime_lock,
                )
            if provider_runtime:
                _apply_runtime_agent_overrides(runtime_kwargs, provider_runtime)
            elif effective_provider and effective_provider != current_provider:
                runtime_kwargs["provider"] = effective_provider
            model = effective_model
            # Per-route explicit transport secrets/base URLs win within the
            # route contract after provider resolution.
            if route_api_key:
                runtime_kwargs["api_key"] = route_api_key
            if route_base_url:
                runtime_kwargs["base_url"] = route_base_url
            if route:
                logger.debug(
                    "api_server request selection applied: model=%s provider=%s route_provider=%s request_provider=%s",
                    model,
                    runtime_kwargs.get("provider"),
                    route_provider or "",
                    request_provider or "",
                )

        # When the config has no model.default but a provider was resolved
        # (e.g. user ran `hermes auth add openai-codex` without `hermes model`),
        # fall back to the provider's first catalog model so the API call
        # doesn't fail with "model must be a non-empty string". Mirrors
        # run.py::_resolve_session_agent_runtime. Runs after the selection
        # block above so a route/session/request override that already
        # resolved a model is never treated as "empty" here.
        if not model and runtime_kwargs.get("provider"):
            try:
                from hermes_cli.models import get_default_model_for_provider
                model = get_default_model_for_provider(runtime_kwargs["provider"])
                if model:
                    logger.info(
                        "No model configured — defaulting to %s for provider %s",
                        model, runtime_kwargs["provider"],
                    )
            except Exception:
                pass

        # Final safety net (#35314): if resolution still produced an empty
        # model — e.g. a transient config-cache miss — reuse the last model
        # successfully resolved for this session (or, failing that, the most
        # recent one resolved process-wide). Building an agent with model=""
        # makes every API call fail HTTP 400 until a manual retry. Mirrors
        # run.py::_resolve_session_agent_runtime.
        #
        # Cache key is gateway_session_key ONLY, never session_id — unlike
        # run.py's native gateway (stable, long-lived chat scopes), the API
        # server hands out a fresh UUID session_id per one-off request
        # (/v1/responses, /v1/runs when no explicit session is supplied).
        # Keying on session_id would leave one permanent dict entry per
        # stateless request, growing unbounded for the life of the process.
        _resolved_key = (
            request_authority.bind(
                "last-model",
                gateway_session_key,
            )
            if gateway_session_key
            else None
        )
        _global_resolved_key = request_authority.bind(
            "last-model-global",
            "*",
        )
        if not model:
            _recovered = (
                self._last_resolved_model.get(_resolved_key)
                if _resolved_key is not None
                else None
            ) or self._last_resolved_model.get(_global_resolved_key)
            if _recovered:
                logger.warning(
                    "Empty model resolved for session=%s — recovering "
                    "last-known-good model %s (config read likely returned "
                    "empty; see #35314)",
                    _resolved_key, _recovered,
                )
                model = _recovered
        elif model:
            if _resolved_key is not None:
                self._last_resolved_model[_resolved_key] = model
            self._last_resolved_model[_global_resolved_key] = model

        user_config = _load_gateway_config()
        if self._require_capability_canary:
            from gateway.canonical_capability_canary_runtime import (
                validate_capability_gateway_config,
            )

            validate_capability_gateway_config(user_config)
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))
        if self._require_capability_canary:
            from gateway.production_capability_prerequisites import (
                FIRST_WAVE_TOOLSETS,
            )

            if enabled_toolsets != sorted(FIRST_WAVE_TOOLSETS):
                raise RuntimeError(
                    "capability canary API toolset projection is not exact"
                )

        max_iterations = _current_max_iterations()

        # Load fallback provider chain so the API server platform has the
        # same fallback behaviour as Telegram/Discord/Slack (fixes #4954).
        fallback_model = (
            None
            if confirmed_runtime_lock or self._require_capability_canary
            else GatewayRunner._load_fallback_model()
        )
        isolated_runtime = _isolated_gateway_runtime_active()
        if self._require_capability_canary:
            from gateway.canonical_capability_canary_runtime import (
                validate_capability_model_runtime_route,
            )

            validate_capability_model_runtime_route(model, runtime_kwargs)
            if reasoning_config != {"enabled": True, "effort": "high"}:
                raise RuntimeError(
                    "capability canary reasoning baseline is not exact"
                )
            if isolated_runtime is not False:
                raise RuntimeError(
                    "capability canary API loop isolation mode is not exact"
                )

        cache_keys = GatewayRunner._extract_cache_busting_config(user_config)
        cache_keys["api.isolated_runtime"] = isolated_runtime
        hermes_home = request_authority.canonical_home
        cache_keys["api.profile_home_sha256"] = hashlib.sha256(
            hermes_home.encode()
        ).hexdigest()
        cache_keys["api.session_id_sha256"] = (
            hashlib.sha256(str(session_id).encode()).hexdigest()
            if session_id
            else ""
        )
        cache_keys["api.gateway_session_key_sha256"] = (
            hashlib.sha256(gateway_session_key.encode()).hexdigest()
            if gateway_session_key
            else ""
        )
        cache_keys["api.runtime_command"] = str(
            runtime_kwargs.get("command", "") or ""
        )
        cache_keys["api.runtime_args"] = list(runtime_kwargs.get("args") or [])
        cache_keys["api.credential_pool_sha256"] = (
            self._api_credential_pool_identity(
                runtime_kwargs.get("credential_pool")
            )
        )
        cache_signature = GatewayRunner._agent_config_signature(
            model,
            runtime_kwargs,
            enabled_toolsets,
            ephemeral_system_prompt or "",
            cache_keys=cache_keys,
        )
        clarify_callback = self._make_api_clarify_callback(
            session_id,
            notify_callback=clarify_notify_callback,
            request_authority=request_authority,
        )
        clarify_authority = getattr(
            clarify_callback,
            "_api_clarify_authority",
            None,
        )

        cache_key = agent_session_scope if session_id else None
        current_message_count = self._api_session_message_count(session_id)
        stale_agent = None
        cached_agent = None
        if reuse_cached_agent and cache_key is not None:
            now = time.monotonic()
            with self._api_agent_cache_lock:
                entry = self._api_agent_cache.get(cache_key)
                if entry is not None:
                    cached_count = entry.get("message_count")
                    coherent = bool(
                        entry.get("signature") == cache_signature
                        and entry.get("session_id") == session_id
                        and not (
                            cached_count is not None
                            and current_message_count is not None
                            and cached_count != current_message_count
                        )
                        and now - float(entry.get("last_used", 0.0) or 0.0)
                        <= API_AGENT_CACHE_IDLE_TTL_SECONDS
                    )
                    if coherent:
                        cached_agent = entry.get("agent")
                        entry["last_used"] = now
                        self._api_agent_cache.move_to_end(cache_key)
                    else:
                        stale_agent = entry.get("agent")
                        self._api_agent_cache.pop(cache_key, None)

        if stale_agent is not None:
            self._release_api_cached_agent(stale_agent)

        if cached_agent is not None:
            # The per-session run lock proves the previous turn is quiescent
            # before this mutable cached agent is rebound to a fresh callback.
            self._retire_api_agent_clarifications(cached_agent)
            GatewayRunner._init_cached_agent_for_turn(cached_agent, 0)
            cached_agent.stream_delta_callback = stream_delta_callback
            cached_agent.tool_progress_callback = tool_progress_callback
            cached_agent.tool_start_callback = tool_start_callback
            cached_agent.tool_complete_callback = tool_complete_callback
            cached_agent.clarify_callback = clarify_callback
            cached_agent.reasoning_config = reasoning_config
            cached_agent.max_iterations = max_iterations
            if request_service_tier is not _REQUEST_OPTION_MISSING:
                cached_agent.service_tier = request_service_tier
            GatewayRunner._apply_fallback_chain_to_agent(
                cached_agent,
                fallback_model,
            )
            self._attest_capability_agent_policy(cached_agent)
            cached_agent._api_clarify_scope = self._api_clarify_scope(
                session_id,
                request_authority=request_authority,
            )
            cached_agent._api_clarify_authority = clarify_authority
            cached_agent._api_request_authority = request_authority
            cached_agent._api_agent_session_scope = agent_session_scope
            logger.debug(
                "Reusing API agent for session %s (sig=%s)",
                cache_key,
                cache_signature,
            )
            return cached_agent

        agent_kwargs = {
            "model": model,
            **runtime_kwargs,
            **_checkpoint_agent_kwargs(user_config),
            "max_iterations": max_iterations,
            "quiet_mode": True,
            "verbose_logging": False,
            "ephemeral_system_prompt": ephemeral_system_prompt or None,
            "enabled_toolsets": enabled_toolsets,
            "session_id": session_id,
            "platform": "api_server",
            "stream_delta_callback": stream_delta_callback,
            "tool_progress_callback": tool_progress_callback,
            "tool_start_callback": tool_start_callback,
            "tool_complete_callback": tool_complete_callback,
            "clarify_callback": clarify_callback,
            "session_db": self._ensure_session_db(),
            "fallback_model": fallback_model,
            "reasoning_config": reasoning_config,
            "gateway_session_key": internal_memory_key or None,
            "skip_memory": isolated_runtime,
            "skip_context_files": isolated_runtime,
        }
        if request_service_tier is not _REQUEST_OPTION_MISSING:
            agent_kwargs["service_tier"] = request_service_tier

        try:
            agent = AIAgent(**agent_kwargs)
        except Exception:
            # Constructor failure is a lifecycle boundary too.  This is
            # normally an unclaimed lazy authority, but exact retirement also
            # covers constructors that invoked the callback before failing.
            self._retire_api_clarify_authority(clarify_authority)
            raise
        agent._api_clarify_scope = self._api_clarify_scope(
            session_id,
            request_authority=request_authority,
        )
        agent._api_clarify_authority = clarify_authority
        agent._api_request_authority = request_authority
        agent._api_agent_session_scope = agent_session_scope
        agent._api_raw_gateway_session_key = gateway_session_key
        agent._api_internal_memory_key = internal_memory_key
        agent._api_cache_signature = cache_signature
        agent._api_cache_session_id = session_id
        self._attest_capability_agent_policy(agent)
        agent._hermes_api_runtime = {
            "provider": runtime_kwargs.get("provider") or getattr(agent, "provider", "") or "",
            "model": getattr(agent, "model", None) or model,
            "route_source": (
                "session_model_lock"
                if confirmed_runtime_lock
                else "session_model_override"
                if session_override
                else "raw_request"
                if route or request_model or request_provider
                else "global"
            ),
        }
        evicted_agents: List[Any] = []
        if reuse_cached_agent and cache_key is not None:
            now = time.monotonic()
            with self._api_agent_cache_lock:
                replaced = self._api_agent_cache.pop(cache_key, None)
                if replaced is not None and replaced.get("agent") is not agent:
                    evicted_agents.append(replaced.get("agent"))
                self._api_agent_cache[cache_key] = {
                    "agent": agent,
                    "signature": cache_signature,
                    "session_id": session_id,
                    "request_scope": agent_session_scope,
                    "message_count": current_message_count,
                    "last_used": now,
                }
                evicted_agents.extend(self._prune_api_agent_cache_locked(now))
        for evicted_agent in evicted_agents:
            if evicted_agent is not agent:
                self._release_api_cached_agent(evicted_agent)
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response(
            {"status": "ok", "platform": "hermes-agent", "version": _hermes_version()}
        )

    async def _handle_health_detailed(self, request: "web.Request") -> "web.Response":
        """GET /health/detailed — rich status for cross-container dashboard probing.

        Returns gateway state, connected platforms, PID, and uptime so the
        dashboard can display full status without needing a shared PID file or
        /proc access.  Requires the same Bearer auth as other API routes.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        from gateway.status import (
            derive_gateway_busy,
            derive_gateway_drainable,
            normalize_updated_at,
            parse_active_agents,
            read_runtime_status,
        )

        runtime = read_runtime_status() or {}
        gw_state = runtime.get("gateway_state")
        gw_active = parse_active_agents(runtime.get("active_agents", 0))
        # This endpoint is served BY the gateway process, so it is by definition
        # alive — gateway_running is True. Derive busy/drainable from the same
        # shared contract /api/status uses so the two surfaces never disagree.
        active_api_runs, process_depth, active_delegations = self._readiness_work_counts()
        from gateway.run import _resolve_gateway_model

        readiness = collect_runtime_readiness(
            configured_model=_resolve_gateway_model(),
            runtime_status=runtime,
            active_api_runs=active_api_runs,
            process_completion_queue_depth=process_depth,
            active_delegations=active_delegations,
        )
        return web.json_response({
            "status": readiness["status"],
            "readiness": readiness,
            "platform": "hermes-agent",
            "version": _hermes_version(),
            "gateway_state": gw_state,
            "platforms": runtime.get("platforms", {}),
            "active_agents": gw_active,
            "gateway_busy": derive_gateway_busy(
                gateway_running=True,
                gateway_state=gw_state,
                active_agents=gw_active,
            ),
            "gateway_drainable": derive_gateway_drainable(
                gateway_running=True,
                gateway_state=gw_state,
            ),
            "exit_reason": runtime.get("exit_reason"),
            # Contract: updated_at is RFC3339 string | null, never a number —
            # the state file may carry legacy epoch floats or hand-edited junk.
            "updated_at": normalize_updated_at(runtime.get("updated_at")),
            "pid": os.getpid(),
        })

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models — list hermes-agent and configured route aliases."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        now = int(time.time())
        # Middleware already entered the frozen profile runtime scope.
        model_name = self._active_model_name()
        models = [
            {
                "id": model_name,
                "object": "model",
                "created": now,
                "owned_by": "hermes",
                "permission": [],
                "root": model_name,
                "parent": None,
            }
        ]
        # Expose configured model route aliases so clients can discover them.
        # Only the alias and resolved model name are exposed — never provider
        # credentials.
        for alias, route_cfg in self._active_model_routes().items():
            if alias == model_name:
                continue  # already listed above
            models.append({
                "id": alias,
                "object": "model",
                "created": now,
                "owned_by": "hermes",
                "permission": [],
                "root": route_cfg.get("model", alias),
                "parent": model_name,
            })

        return web.json_response({"object": "list", "data": models})

    async def _handle_model_options(self, request: "web.Request") -> "web.Response":
        """GET /api/model/options — return Hermes provider/model inventory.

        This mirrors the dashboard/TUI model picker inventory endpoint so
        external clients using the API server can sync to the user's configured
        Hermes provider catalog instead of scraping the single OpenAI-compatible
        `/v1/models` alias.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        refresh = _coerce_request_bool(request.query.get("refresh"), default=False)
        try:
            from hermes_cli.inventory import build_model_options_payload, load_picker_context

            def _build_payload() -> Dict[str, Any]:
                return build_model_options_payload(
                    load_picker_context(),
                    include_unconfigured=True,
                    refresh=refresh,
                )

            # Inventory enrichment can fetch pricing and provider catalogs.
            # Keep all synchronous picker work off aiohttp's event loop.
            payload = await asyncio.to_thread(_build_payload)
            return web.json_response(payload)
        except Exception:
            logger.exception("[%s] GET /api/model/options failed", self.name)
            return web.json_response(
                _openai_error(
                    "Failed to list model options.",
                    code="model_options_failed",
                ),
                status=500,
            )

    async def _handle_capabilities(self, request: "web.Request") -> "web.Response":
        """GET /v1/capabilities — advertise the stable API surface.

        External UIs and orchestrators use this endpoint to discover the API
        server's plugin-safe contract without scraping docs or assuming that
        every Hermes version exposes the same endpoints.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "hermes.api_server.capabilities",
            "platform": "hermes-agent",
            "model": self._active_model_name(),
            "auth": {
                "type": "bearer",
                "required": self._api_auth_configured(),
                "approval_owner_authority": {
                    "schema": self._approval_authority_schema(),
                    "configured": self._approval_authority_configured(),
                    "positive_choices": ["once", "session", "always"],
                    "generic_bearer_choices": ["deny"],
                    "max_ttl_seconds": (
                        API_APPROVAL_AUTHORITY_MAX_TTL_SECONDS
                    ),
                    "nonce_replay_protected": True,
                },
            },
            "runtime": {
                "mode": "server_agent",
                "tool_execution": "server",
                "split_runtime": False,
                "description": (
                    "The API server creates a server-side Hermes AIAgent; "
                    "tools execute on the API-server host unless a future "
                    "explicit split-runtime mode is enabled."
                ),
            },
            "features": {
                "chat_completions": True,
                "chat_completions_streaming": True,
                "responses_api": True,
                "responses_streaming": True,
                "run_submission": True,
                "run_status": True,
                "run_events_sse": True,
                "run_stop": True,
                "run_approval_response": True,
                "approval_response": True,
                "clarification_response": True,
                "tool_progress_events": True,
                "approval_events": True,
                "clarification_events": True,
                "session_resources": True,
                "model_options": True,
                "session_chat": True,
                "session_chat_streaming": True,
                "session_fork": True,
                "session_model_lock": True,
                "delegation_status": True,
                "admin_config_rw": False,
                "jobs_admin": False,
                "memory_write_api": False,
                "skills_api": True,
                "audio_api": False,
                "realtime_voice": False,
                "session_continuity_header": "X-Hermes-Session-Id",
                "session_key_header": "X-Hermes-Session-Key",
                "cors": bool(self._cors_origins),
            },
            "endpoints": {
                "health": {"method": "GET", "path": "/health"},
                "health_detailed": {"method": "GET", "path": "/health/detailed"},
                "models": {"method": "GET", "path": "/v1/models"},
                "model_options": {"method": "GET", "path": "/api/model/options"},
                "chat_completions": {"method": "POST", "path": "/v1/chat/completions"},
                "responses": {"method": "POST", "path": "/v1/responses"},
                "runs": {"method": "POST", "path": "/v1/runs"},
                "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
                "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
                "run_approval": {"method": "POST", "path": "/v1/runs/{run_id}/approval"},
                "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
                "approvals": {"method": "GET", "path": "/v1/approvals"},
                "approval_response": {
                    "method": "POST",
                    "path": "/v1/approvals/{approval_id}/response",
                },
                "clarifications": {"method": "GET", "path": "/v1/clarifications"},
                "clarification_response": {
                    "method": "POST",
                    "path": "/v1/clarifications/{clarify_id}/response",
                },
                "skills": {"method": "GET", "path": "/v1/skills"},
                "toolsets": {"method": "GET", "path": "/v1/toolsets"},
                "sessions": {"method": "GET", "path": "/api/sessions"},
                "session_create": {"method": "POST", "path": "/api/sessions"},
                "session": {"method": "GET", "path": "/api/sessions/{session_id}"},
                "session_update": {"method": "PATCH", "path": "/api/sessions/{session_id}"},
                "session_delete": {"method": "DELETE", "path": "/api/sessions/{session_id}"},
                "session_messages": {"method": "GET", "path": "/api/sessions/{session_id}/messages"},
                "session_fork": {"method": "POST", "path": "/api/sessions/{session_id}/fork"},
                "session_chat": {"method": "POST", "path": "/api/sessions/{session_id}/chat"},
                "session_chat_stream": {"method": "POST", "path": "/api/sessions/{session_id}/chat/stream"},
                "session_model_lock": {"method": "POST", "path": "/api/sessions/{session_id}/model"},
                "delegation_status": {
                    "method": "GET",
                    "path": "/api/delegations/{delegation_id}",
                },
            },
        })

    async def _handle_list_approvals(
        self,
        request: "web.Request",
    ) -> "web.Response":
        """GET /v1/approvals — poll one exact conversation's live approvals."""

        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if not self._api_auth_configured():
            return web.json_response(
                _openai_error(
                    "Approval control requires API key authentication",
                    code="approval_auth_required",
                ),
                status=403,
            )
        session_id, session_err = self._parse_api_control_session_id(
            request,
            required=True,
        )
        if session_err is not None:
            return session_err
        approval_scope = self._api_request_scope(
            "approval-session",
            session_id,
        )

        with self._api_approvals_lock:
            candidates = [
                dict(state)
                for state in self._api_pending_approvals.values()
                if state.get("_request_scope") == approval_scope
                and state.get("status") == "pending"
            ]

        # Cross-check the adapter projection against the approval core.  A
        # timeout/boundary may detach the core entry immediately before this
        # poll; stale projection state must never be advertised as actionable.
        from tools.approval import get_pending_gateway_approvals

        live_ids_by_key: Dict[str, set[str]] = {}
        for state in candidates:
            approval_key = str(state.get("_approval_session_key", "") or "")
            if approval_key not in live_ids_by_key:
                live_ids_by_key[approval_key] = {
                    str(item.get("approval_id", "") or "")
                    for item in get_pending_gateway_approvals(approval_key)
                }

        pending: List[Dict[str, Any]] = []
        with self._api_approvals_lock:
            for state in candidates:
                approval_id = str(state.get("id", "") or "")
                approval_entry_scope = approval_scope.bind(
                    "approval-id",
                    approval_id,
                )
                approval_key = str(
                    state.get("_approval_session_key", "") or ""
                )
                current = self._api_pending_approvals.get(
                    approval_entry_scope
                )
                if (
                    current is None
                    or current.get("_request_scope") != approval_scope
                    or approval_id not in live_ids_by_key.get(approval_key, set())
                ):
                    self._api_pending_approvals.pop(
                        approval_entry_scope,
                        None,
                    )
                    continue
                pending.append(self._public_api_approval(current))

        pending.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0))
        return web.json_response({"object": "list", "data": pending})

    async def _handle_resolve_approval(
        self,
        request: "web.Request",
    ) -> "web.Response":
        """POST one exact enum decision for one exact pending approval ID."""

        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if not self._api_auth_configured():
            return web.json_response(
                _openai_error(
                    "Approval control requires API key authentication",
                    code="approval_auth_required",
                ),
                status=403,
            )
        session_id, session_err = self._parse_api_control_session_id(
            request,
            required=True,
        )
        if session_err is not None:
            return session_err
        approval_scope = self._api_request_scope(
            "approval-session",
            session_id,
        )

        approval_id = str(request.match_info.get("approval_id", "") or "")
        if re.fullmatch(r"[0-9a-f]{32}", approval_id) is None:
            return web.json_response(
                _openai_error("Invalid approval ID", code="invalid_approval_id"),
                status=400,
            )
        approval_entry_scope = approval_scope.bind(
            "approval-id",
            approval_id,
        )
        body, body_err = await self._read_json_body(request)
        if body_err is not None:
            return body_err
        choice = body.get("choice")
        if not isinstance(choice, str) or choice not in API_APPROVAL_CHOICES:
            return web.json_response(
                _openai_error(
                    "Invalid approval choice; expected once, session, always, or deny",
                    code="invalid_approval_choice",
                ),
                status=400,
            )
        with self._api_approvals_lock:
            state = self._api_pending_approvals.get(approval_entry_scope)
            if (
                state is None
                or state.get("_request_scope") != approval_scope
            ):
                # Do not reveal whether the opaque ID exists in another session.
                return web.json_response(
                    _openai_error(
                        f"Approval not found: {approval_id}",
                        code="approval_not_found",
                    ),
                    status=404,
                )
            if state.get("status") != "pending":
                return web.json_response(
                    _openai_error(
                        "Approval is no longer pending",
                        code="approval_not_pending",
                    ),
                    status=409,
                )
            if choice == "always" and not state.get("allow_permanent"):
                return web.json_response(
                    _openai_error(
                        "Permanent approval is not offered for this action",
                        code="permanent_approval_not_allowed",
                    ),
                    status=400,
                )
            if choice not in state.get("choices", ()):
                return web.json_response(
                    _openai_error(
                        "Approval choice is not offered for this exact action",
                        code="approval_choice_not_allowed",
                    ),
                    status=400,
                )
            approval_session_key = str(
                state.get("_approval_session_key", "") or ""
            )
            event_callback = state.get("_event_callback")
            capability_epoch_sha256 = str(
                state.get("capability_epoch_sha256", "") or ""
            )
            authority_generation = state.get("_authority_generation")

        from tools.approval import (
            get_pending_gateway_approvals,
            session_authority_fence_is_current,
        )

        core_state = next(
            (
                item
                for item in get_pending_gateway_approvals(
                    approval_session_key,
                    include_authority_binding=True,
                )
                if item.get("approval_id") == approval_id
            ),
            None,
        )
        if (
            core_state is None
            or type(authority_generation) is not int
            or core_state.get("_authority_generation") != authority_generation
            or core_state.get("_capability_epoch_sha256")
            != capability_epoch_sha256
            or not session_authority_fence_is_current(
                approval_session_key,
                authority_generation,
                capability_epoch_sha256,
            )
        ):
            with self._api_approvals_lock:
                self._api_pending_approvals.pop(
                    approval_entry_scope,
                    None,
                )
            return web.json_response(
                _openai_error(
                    "Approval authority epoch is stale",
                    code="approval_authority_stale",
                ),
                status=409,
            )

        if (
            choice == "deny"
            and set(body) != {"choice"}
        ) or (
            choice != "deny"
            and bool(set(body) - {"choice", "owner_authority"})
        ):
            return web.json_response(
                _openai_error(
                    "Positive approval accepts only owner_authority; deny "
                    "accepts no authority fields",
                    code="invalid_approval_response",
                ),
                status=400,
            )

        if choice != "deny":
            authority_err = self._verify_and_consume_api_approval_authority(
                body.get("owner_authority"),
                session_id=session_id,
                approval_id=approval_id,
                choice=choice,
                capability_epoch_sha256=capability_epoch_sha256,
                request=request,
            )
            if authority_err is not None:
                return authority_err

        with self._api_approvals_lock:
            current = self._api_pending_approvals.get(
                approval_entry_scope
            )
            if (
                current is not state
                or current.get("_request_scope") != approval_scope
                or current.get("status") != "pending"
            ):
                return web.json_response(
                    _openai_error(
                        "Approval is no longer pending",
                        code="approval_not_pending",
                    ),
                    status=409,
                )
            current["status"] = "resolving"

        from tools.approval import resolve_gateway_approval_by_id

        resolved = resolve_gateway_approval_by_id(
            approval_session_key,
            approval_id,
            choice,
        )
        with self._api_approvals_lock:
            self._api_pending_approvals.pop(
                approval_entry_scope,
                None,
            )
        if resolved != 1:
            return web.json_response(
                _openai_error(
                    "Approval expired before the response was accepted",
                    code="approval_expired",
                ),
                status=409,
            )

        responded = {
            "id": approval_id,
            "object": "hermes.approval.response",
            "status": "resolved",
            "session_id": session_id,
            "choice": choice,
        }
        if event_callback is not None:
            try:
                event_callback("approval.responded", dict(responded))
            except Exception:
                logger.debug(
                    "API approval response event delivery failed", exc_info=True
                )
        return web.json_response(responded)

    async def _handle_list_clarifications(
        self,
        request: "web.Request",
    ) -> "web.Response":
        """GET /v1/clarifications — poll pending structured questions."""

        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if not self._api_auth_configured():
            return web.json_response(
                _openai_error(
                    "Clarification control requires API key authentication",
                    code="clarification_auth_required",
                ),
                status=403,
            )
        session_id, session_err = self._parse_api_control_session_id(
            request,
            required=True,
        )
        if session_err is not None:
            return session_err
        clarify_scope = self._api_request_scope(
            "clarify-session",
            session_id,
        )

        with self._api_clarifications_lock:
            pending = [
                self._public_api_clarification(state)
                for state in self._api_pending_clarifications.values()
                if state.get("status") == "pending"
                and state.get("_request_scope") == clarify_scope
            ]
        pending.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0))
        return web.json_response({"object": "list", "data": pending})

    async def _handle_resolve_clarification(
        self,
        request: "web.Request",
    ) -> "web.Response":
        """POST a free-text or indexed answer to one pending clarification."""

        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if not self._api_auth_configured():
            return web.json_response(
                _openai_error(
                    "Clarification control requires API key authentication",
                    code="clarification_auth_required",
                ),
                status=403,
            )
        session_id, session_err = self._parse_api_control_session_id(
            request,
            required=True,
        )
        if session_err is not None:
            return session_err
        clarify_scope = self._api_request_scope(
            "clarify-session",
            session_id,
        )

        clarify_id = str(request.match_info.get("clarify_id", "") or "")
        if re.fullmatch(r"[0-9a-f]{32}", clarify_id) is None:
            return web.json_response(
                _openai_error("Invalid clarification ID"),
                status=400,
            )
        clarification_entry_scope = clarify_scope.bind(
            "clarification-id",
            clarify_id,
        )
        body, body_err = await self._read_json_body(request)
        if body_err is not None:
            return body_err

        with self._api_clarifications_lock:
            state = self._api_pending_clarifications.get(
                clarification_entry_scope
            )
            if (
                state is None
                or state.get("_request_scope") != clarify_scope
            ):
                return web.json_response(
                    _openai_error(
                        f"Clarification not found: {clarify_id}",
                        code="clarification_not_found",
                    ),
                    status=404,
                )
            if state.get("status") != "pending":
                return web.json_response(
                    _openai_error(
                        "Clarification has already been resolved",
                        code="clarification_already_resolved",
                    ),
                    status=409,
                )

            has_response = "response" in body
            has_choice_index = "choice_index" in body
            if has_response == has_choice_index:
                return web.json_response(
                    _openai_error(
                        "Provide exactly one of 'response' or 'choice_index'",
                        code="invalid_clarification_response",
                    ),
                    status=400,
                )

            if has_choice_index:
                choice_index = body.get("choice_index")
                choices = state.get("choices")
                if (
                    type(choice_index) is not int
                    or not isinstance(choices, list)
                    or choice_index < 0
                    or choice_index >= len(choices)
                ):
                    return web.json_response(
                        _openai_error(
                            "choice_index is outside the offered choices",
                            code="invalid_clarification_choice",
                        ),
                        status=400,
                    )
                response_text = str(choices[choice_index])
            else:
                raw_response = body.get("response")
                if not isinstance(raw_response, str):
                    return web.json_response(
                        _openai_error(
                            "response must be a string",
                            code="invalid_clarification_response",
                        ),
                        status=400,
                    )
                response_text = raw_response.strip()
                if not response_text or len(response_text) > API_CLARIFY_RESPONSE_MAX_LENGTH:
                    return web.json_response(
                        _openai_error(
                            "response must be non-empty and at most 65536 characters",
                            code="invalid_clarification_response",
                        ),
                        status=400,
                    )
            state["status"] = "resolving"
            core_clarify_id = str(
                state.get("_core_clarify_id") or ""
            )
            core_generation = state.get("_core_generation")

        try:
            from tools.clarify_gateway import resolve_gateway_clarify

            resolved = resolve_gateway_clarify(
                core_clarify_id,
                response_text,
                session_key=clarify_scope.internal_key,
                generation=(
                    int(core_generation)
                    if core_generation is not None
                    else None
                ),
            )
        except Exception:
            resolved = False
            logger.exception("Failed to resolve API clarification")

        if not resolved:
            with self._api_clarifications_lock:
                current = self._api_pending_clarifications.get(
                    clarification_entry_scope
                )
                if current is state:
                    self._api_pending_clarifications.pop(
                        clarification_entry_scope,
                        None,
                    )
            return web.json_response(
                _openai_error(
                    "Clarification expired before the response was accepted",
                    code="clarification_expired",
                ),
                status=409,
            )

        run_scope = state.get("_run_scope")
        if isinstance(run_scope, APIRequestScope):
            run_id = run_scope.public_id
            self._set_run_status(
                run_id,
                "running",
                run_scope=run_scope,
                last_event="clarify.responded",
            )
            run_queue = self._run_streams.get(run_scope)
            if run_queue is not None:
                try:
                    run_queue.put_nowait({
                        "event": "clarify.responded",
                        "run_id": run_id,
                        "clarify_id": clarify_id,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass

        return web.json_response({
            "object": "hermes.clarification.response",
            "clarify_id": clarify_id,
            "status": "resolved",
        })

    async def _handle_skills(self, request: "web.Request") -> "web.Response":
        """GET /v1/skills — list installed skills visible to the API-server agent.

        Read-only listing intended for external clients that need to know
        which skills are available without sending a chat message and asking
        the model. Mirrors what the gateway/CLI surfaces through
        ``/skills list``, but as a deterministic JSON payload.

        Returns the same skill metadata (name, description, category) the
        skills hub uses internally. Disabled skills are excluded so the
        listing matches what the agent actually loads.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            from tools.skills_tool import _find_all_skills, _sort_skills
            skills = _sort_skills(_find_all_skills(skip_disabled=False))
        except Exception:
            logger.exception("GET /v1/skills failed")
            return web.json_response(
                _openai_error("Failed to enumerate skills", err_type="server_error"),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "data": skills,
        })

    async def _handle_toolsets(self, request: "web.Request") -> "web.Response":
        """GET /v1/toolsets — project the model's currently exposable schemas.

        The projection is computed through the same registry/check_fn/dynamic
        schema path as AIAgent construction.  Configured candidates that fail
        a service gate never appear in ``tools`` or ``tool_schemas``.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            from hermes_cli.config import load_config
            from hermes_cli.tools_config import (
                _get_effective_configurable_toolsets,
                _get_platform_tools,
                _toolset_has_keys,
                get_nous_subscription_features,
            )
            from model_tools import _compute_tool_definitions
            from tools.registry import registry

            config = load_config()
            enabled_toolsets = sorted(_get_platform_tools(
                config, "api_server", include_default_mcp_servers=False
            ))
            definitions = _compute_tool_definitions(
                enabled_toolsets=enabled_toolsets,
                disabled_toolsets=None,
                quiet_mode=True,
                # The catalog endpoint projects the concrete schemas that are
                # currently callable.  Tool-search assembly intentionally
                # replaces that catalog with a smaller model-facing surface,
                # which would hide individually service-gated tools here.
                skip_tool_search_assembly=True,
            )
            metadata = {
                name: {"label": label, "description": desc}
                for name, label, desc in _get_effective_configurable_toolsets()
            }
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for definition in definitions:
                function = definition.get("function")
                if not isinstance(function, Mapping):
                    continue
                tool_name = str(function.get("name", "") or "")
                if not tool_name:
                    continue
                toolset_name = registry.get_toolset_for_tool(tool_name)
                if not toolset_name:
                    # Dynamic schemas without a registered owner cannot be
                    # projected as a fabricated toolset.
                    continue
                grouped.setdefault(toolset_name, []).append(dict(definition))

            group_names = set(metadata) | set(enabled_toolsets) | set(grouped)
            features = get_nous_subscription_features(config)
            data: List[Dict[str, Any]] = []
            for name in sorted(group_names):
                schemas = sorted(
                    grouped.get(name, []),
                    key=lambda item: str(
                        item.get("function", {}).get("name", "")
                    ),
                )
                try:
                    configured = _toolset_has_keys(
                        name,
                        config,
                        features=features,
                    )
                except Exception:
                    configured = False
                meta = metadata.get(name, {})
                data.append({
                    "name": name,
                    "label": meta.get("label", name),
                    "description": meta.get("description", ""),
                    "enabled": name in enabled_toolsets,
                    "configured": configured,
                    "available": bool(schemas),
                    "tools": [
                        str(item["function"]["name"]) for item in schemas
                    ],
                    "tool_schemas": schemas,
                })
        except Exception:
            logger.exception("GET /v1/toolsets failed")
            return web.json_response(
                _openai_error("Failed to enumerate toolsets", err_type="server_error"),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "platform": "api_server",
            "data": data,
        })

    # ------------------------------------------------------------------
    # /api/sessions — thin client/session resource API
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_nonnegative_int(value: Any, default: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed < 0:
            return default
        return min(parsed, maximum)

    @staticmethod
    def _session_response(session: Dict[str, Any]) -> Dict[str, Any]:
        """Return a stable, client-safe session representation."""
        safe_keys = (
            "id", "source", "user_id", "model", "title", "started_at", "ended_at",
            "end_reason", "message_count", "tool_call_count", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
            "api_call_count", "parent_session_id", "last_active", "preview",
            "_lineage_root_id",
        )
        payload = {key: session.get(key) for key in safe_keys if key in session}
        # Avoid exposing full system prompts/model_config through the client API;
        # callers only need to know whether those snapshots exist.
        payload["has_system_prompt"] = bool(session.get("system_prompt"))
        payload["has_model_config"] = bool(session.get("model_config"))
        return payload

    @staticmethod
    def _message_response(message: Dict[str, Any]) -> Dict[str, Any]:
        safe_keys = (
            "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
            "tool_name", "timestamp", "token_count", "finish_reason", "reasoning",
            "reasoning_content",
        )
        return {key: message.get(key) for key in safe_keys if key in message}

    async def _read_json_body(self, request: "web.Request") -> tuple[Dict[str, Any], Optional["web.Response"]]:
        try:
            body = await request.json()
        except Exception:
            return {}, web.json_response(_openai_error("Invalid JSON in request body"), status=400)
        if not isinstance(body, dict):
            return {}, web.json_response(_openai_error("Request body must be a JSON object"), status=400)
        return body, None

    async def _get_existing_session_or_404(self, session_id: str) -> tuple[Optional[Dict[str, Any]], Optional["web.Response"]]:
        session_id, validation_error = self._validate_api_session_id_value(
            session_id,
            required=True,
        )
        if validation_error is not None:
            return None, validation_error
        db = await self._ensure_session_db_async()
        if db is None:
            return None, web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)
        # Offload the blocking SQLite read off the event loop (CWE/perf: the
        # API server is single-threaded aiohttp; a sync SessionDB call here
        # freezes every in-flight request, see PR discussion on event-loop
        # blocking SQLite in the gateway surface).
        session = await self._offload_session_db(db.get_session, session_id)
        if not session:
            return None, web.json_response(_openai_error(f"Session not found: {session_id}", code="session_not_found"), status=404)
        return session, None

    async def _conversation_history_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        db = await self._ensure_session_db_async()
        if db is None:
            return []
        try:
            return await self._offload_session_db(
                db.get_messages_as_conversation,
                session_id,
            )
        except Exception as exc:
            logger.warning("Failed to load session history for %s: %s", session_id, exc)
            return []

    async def _handle_get_delegation(
        self,
        request: "web.Request",
    ) -> "web.Response":
        """Expose durable delivery/wake state without mutating chat history."""

        if not self._api_auth_configured():
            return web.json_response(
                _openai_error(
                    "Delegation status requires API key authentication",
                    code="delegation_auth_required",
                ),
                status=403,
            )
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        delegation_id = str(
            request.match_info.get("delegation_id") or ""
        )
        if not re.fullmatch(r"deleg_[0-9a-f]{32}", delegation_id):
            return web.json_response(
                _openai_error(
                    "Invalid delegation ID",
                    code="invalid_delegation_id",
                ),
                status=400,
            )

        authority = (
            _api_request_authority.get()
            or self._api_request_scope("request")
        )
        verify_api_request_scope(authority)
        from tools.async_delegation import (
            EventDeliveryStore,
            get_durable_delegation,
        )

        store = EventDeliveryStore(
            hermes_home=authority.canonical_home,
            source_home=authority.source_home,
            profile=authority.profile,
            profile_generation=authority.profile_generation,
        )
        try:
            record = await asyncio.to_thread(
                get_durable_delegation,
                delegation_id,
                store=store,
            )
        except Exception:
            logger.exception(
                "Failed to read durable delegation status for %s",
                delegation_id,
            )
            return web.json_response(
                _openai_error(
                    "Delegation status is unavailable because its durable "
                    "record is malformed",
                    err_type="server_error",
                    code="delegation_status_corrupt",
                ),
                status=500,
            )
        if record is None:
            return web.json_response(
                _openai_error(
                    "Delegation not found",
                    code="delegation_not_found",
                ),
                status=404,
            )

        allowed_states = {
            "running",
            "stalling",
            "finalizing",
            "completed",
            "failed",
            "interrupted",
            "partial",
            "stalled",
            "unknown",
            "error",
        }
        state = record.get("state") if isinstance(record, Mapping) else None
        delivery_state = (
            record.get("delivery_state")
            if isinstance(record, Mapping)
            else None
        )
        wake_state = (
            record.get("wake_state")
            if isinstance(record, Mapping)
            else None
        )
        delivery_attempts = (
            record.get("delivery_attempts")
            if isinstance(record, Mapping)
            else None
        )
        timestamps = (
            record.get("dispatched_at"),
            record.get("completed_at"),
        ) if isinstance(record, Mapping) else (None, None)
        valid_timestamp = lambda value: (
            value is None
            or (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        )
        if (
            not isinstance(record, Mapping)
            or record.get("delegation_id") != delegation_id
            or state not in allowed_states
            or delivery_state not in {"pending", "delivered", "dropped"}
            or wake_state not in {
                "not_started",
                "running",
                "completed",
                "uncertain",
            }
            or not isinstance(delivery_attempts, int)
            or isinstance(delivery_attempts, bool)
            or delivery_attempts < 0
            or not all(valid_timestamp(value) for value in timestamps)
            or not isinstance(
                record.get("delivery_disposition_reason", ""),
                str,
            )
            or not isinstance(
                record.get("wake_disposition_reason", ""),
                str,
            )
        ):
            logger.error(
                "Durable delegation status row failed validation for %s",
                delegation_id,
            )
            return web.json_response(
                _openai_error(
                    "Delegation status is unavailable because its durable "
                    "record is malformed",
                    err_type="server_error",
                    code="delegation_status_corrupt",
                ),
                status=500,
            )

        return web.json_response(
            {
                "object": "hermes.async_delegation.status",
                "delegation_id": delegation_id,
                "state": state,
                "dispatched_at": record.get("dispatched_at"),
                "completed_at": record.get("completed_at"),
                "delivery_state": delivery_state,
                "delivery_attempts": delivery_attempts,
                "delivery_disposition_reason": _redact_api_error_text(
                    record.get("delivery_disposition_reason") or "",
                    limit=500,
                ),
                "wake_state": wake_state,
                "wake_disposition_reason": _redact_api_error_text(
                    record.get("wake_disposition_reason") or "",
                    limit=500,
                ),
            }
        )

    async def _handle_list_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions — list persisted Hermes sessions."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        limit = self._parse_nonnegative_int(request.query.get("limit"), default=50, maximum=200)
        offset = self._parse_nonnegative_int(request.query.get("offset"), default=0, maximum=1_000_000)
        source = request.query.get("source") or None
        include_children = _coerce_request_bool(request.query.get("include_children"), default=False)
        sessions = await self._offload_session_db(
            db.list_sessions_rich,
            source=source,
            limit=limit,
            offset=offset,
            include_children=include_children,
            order_by_last_active=True,
        )
        return web.json_response({
            "object": "list",
            "data": [self._session_response(s) for s in sessions],
            "limit": limit,
            "offset": offset,
            "has_more": len(sessions) == limit,
        })

    async def _handle_create_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions -- create an empty Hermes session row.

        The existence check, insert, title handling, and invalid-title
        rollback run as a single off-loop operation to avoid a TOCTOU
        window between the duplicate check and the insert (concurrent
        same-ID creates could otherwise both pass the check and both
        return 201 via the ON CONFLICT enrichment upsert).
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        body, err = await self._read_json_body(request)
        if err:
            return err

        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        if "id" in body:
            raw_id = body["id"]
        else:
            raw_id = body.get("session_id")
        if raw_id is None and "id" not in body and "session_id" not in body:
            session_id = f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        else:
            session_id, id_err = self._validate_api_session_id_value(
                raw_id,
                required=True,
            )
            if id_err is not None:
                return id_err

        system_prompt = body.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_prompt must be a string", code="invalid_system_prompt"), status=400)
        source = self._normalize_session_source(body.get("source") or "api_server")
        try:
            runtime_request = self._session_runtime_request_from_body(body)
            from gateway.api_execution_context import (
                normalize_model_identifier,
            )

            default_model_name = normalize_model_identifier(
                self._active_model_name(),
                field="default session model",
            )
        except ValueError as exc:
            return _invalid_runtime_request_response(exc)
        lock_error = self._runtime_lock_error(runtime_request)
        if lock_error is not None:
            return lock_error
        requested = runtime_request.get("requested") or {}
        model_name = requested.get("model") or default_model_name or None
        model_config = None
        if requested.get("model") or requested.get("provider"):
            try:
                safe_model_options = _normalize_persisted_api_model_options(
                    runtime_request.get("model_options")
                )
            except ValueError as exc:
                return web.json_response(
                    _openai_error(
                        str(exc),
                        code="invalid_model_options",
                    ),
                    status=400,
                )
            model_config = {
                "browser_model_lock": {
                    "provider": requested.get("provider") or "",
                    "model": requested.get("model") or "",
                    "model_options": safe_model_options,
                    "route_source": runtime_request.get("route_source") or "",
                    "confirmed": bool(runtime_request.get("require_model_lock")),
                    "updated_at": time.time(),
                }
            }
        title = body.get("title")

        # Run the entire check-insert-title sequence inside a single
        # _execute_write call (BEGIN IMMEDIATE + commit) so the existence
        # check and the insert are atomic at the SQLite level.  Two
        # concurrent requests for the same ID serialize here: the second
        # one blocks on the write lock and sees the row the first inserted.
        def _do_create():
            def _atomic(conn):
                row = conn.execute(
                    "SELECT id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row:
                    return None, "exists"
                import time as _time
                conn.execute(
                    """INSERT INTO sessions (
                       id, source, model, model_config, system_prompt, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        source,
                        model_name,
                        json.dumps(model_config) if model_config else None,
                        system_prompt,
                        _time.time(),
                    ),
                )
                if title is not None:
                    clean_title = db.sanitize_title(str(title))
                    if clean_title:
                        conflict = conn.execute(
                            "SELECT id FROM sessions WHERE title = ? AND id != ?",
                            (clean_title, session_id),
                        ).fetchone()
                        if conflict:
                            conn.execute(
                                "DELETE FROM sessions WHERE id = ?", (session_id,)
                            )
                            return None, f"title:Title already in use by session {conflict['id']}"
                    conn.execute(
                        "UPDATE sessions SET title = ? WHERE id = ?",
                        (clean_title, session_id),
                    )
                session_row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                return (dict(session_row) if session_row else {
                    "id": session_id, "source": source,
                    "model": model_name, "title": title,
                }), None
            return db._execute_write(_atomic)

        session, err = await self._offload_session_db(_do_create)
        if err == "exists":
            return web.json_response(_openai_error(f"Session already exists: {session_id}", code="session_exists"), status=409)
        if err and err.startswith("title:"):
            return web.json_response(_openai_error(err[len("title:"):], code="invalid_title"), status=400)
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)}, status=201)

    async def _handle_get_session(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session, err = await _await_if_needed(
            self._get_existing_session_or_404(request.match_info["session_id"])
        )
        if err:
            return err
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_patch_session(self, request: "web.Request") -> "web.Response":
        """PATCH /api/sessions/{session_id} — update client-safe session metadata."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = await _await_if_needed(
            self._get_existing_session_or_404(session_id)
        )
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        allowed = {"title", "end_reason"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            return web.json_response(_openai_error(f"Unsupported session fields: {', '.join(unknown)}", code="unsupported_session_field"), status=400)

        db = await self._ensure_session_db_async()
        if "title" in body:
            try:
                await self._offload_session_db(
                    db.set_session_title,
                    session_id,
                    "" if body["title"] is None else str(body["title"]),
                )
            except ValueError as exc:
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        if body.get("end_reason"):
            await self._offload_session_db(
                db.end_session,
                session_id,
                str(body["end_reason"]),
            )
            self._retire_api_session_agents(
                session_id,
                reason="API session ended",
            )
        session = await self._offload_session_db(db.get_session, session_id) or session
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_delete_session(self, request: "web.Request") -> "web.Response":
        """DELETE /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = await _await_if_needed(
            self._get_existing_session_or_404(session_id)
        )
        if err:
            return err
        db = await self._ensure_session_db_async()
        deleted = await self._offload_session_db(db.delete_session, session_id)
        if deleted:
            self._retire_api_session_agents(
                session_id,
                reason="API session deleted",
            )
        return web.json_response({"object": "hermes.session.deleted", "id": session_id, "deleted": bool(deleted)})

    async def _handle_session_messages(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}/messages."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        _, err = await _await_if_needed(
            self._get_existing_session_or_404(session_id)
        )
        if err:
            return err
        db = await self._ensure_session_db_async()
        resolved_id = await self._offload_session_db(
            db.resolve_resume_session_id,
            session_id,
        )
        messages = await self._offload_session_db(db.get_messages, resolved_id)
        return web.json_response({
            "object": "list",
            "session_id": resolved_id,
            "data": [self._message_response(m) for m in messages],
        })

    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/fork — branch via current SessionDB primitives."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        source_id = request.match_info["session_id"]
        source, err = await _await_if_needed(
            self._get_existing_session_or_404(source_id)
        )
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        db = await self._ensure_session_db_async()
        if "id" in body:
            raw_fork_id = body["id"]
        else:
            raw_fork_id = body.get("session_id")
        if (
            raw_fork_id is None
            and "id" not in body
            and "session_id" not in body
        ):
            fork_id = f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        else:
            fork_id, id_err = self._validate_api_session_id_value(
                raw_fork_id,
                required=True,
            )
            if id_err is not None:
                return id_err
        if await self._offload_session_db(db.get_session, fork_id):
            return web.json_response(_openai_error(f"Session already exists: {fork_id}", code="session_exists"), status=409)

        # Match the CLI /branch semantics: mark the original as branched, then
        # create a child session that carries the transcript forward. This uses
        # SessionDB's native parent_session_id/end_reason visibility model rather
        # than inventing a parallel fork store.
        await self._offload_session_db(db.end_session, source_id, "branched")
        await self._offload_session_db(
            db.create_session,
            fork_id,
            "api_server",
            model=source.get("model"),
            system_prompt=source.get("system_prompt"),
            parent_session_id=source_id,
        )
        messages = await self._offload_session_db(db.get_messages, source_id)
        await self._offload_session_db(db.replace_messages, fork_id, messages)
        title = body.get("title")
        if title is None:
            base = source.get("title") or "fork"
            try:
                title = await self._offload_session_db(
                    db.get_next_title_in_lineage,
                    base,
                )
            except Exception:
                title = f"{base} fork"
        try:
            await self._offload_session_db(
                db.set_session_title,
                fork_id,
                str(title),
            )
        except ValueError as exc:
            return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        fork = await self._offload_session_db(db.get_session, fork_id) or {
            "id": fork_id,
            "parent_session_id": source_id,
        }
        return web.json_response({"object": "hermes.session", "session": self._session_response(fork)}, status=201)

    @_admit_api_agent_request
    async def _handle_session_chat(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat — one synchronous agent turn."""
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        session, err = await _await_if_needed(
            self._get_existing_session_or_404(session_id)
        )
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        # Runtime selection. A backend-acknowledged Browser model lock
        # (require_model_lock in the body, or a previously confirmed lock
        # persisted on the session row) is an execution contract and wins.
        # Otherwise: session-persisted model (POST /api/sessions
        # {"model": ...}) — previously fetched and discarded here — routes
        # through model_routes when it is an alias (route
        # provider/credentials come along) or threads through as
        # session_model when it is a raw string; per-request body values
        # come after that.
        try:
            runtime_request = self._effective_session_runtime_request(
                session=session,
                body=body,
            )
        except ValueError as exc:
            return _invalid_runtime_request_response(exc)
        lock_error = self._runtime_lock_error(runtime_request)
        if lock_error is not None:
            return lock_error
        if not await self._persist_session_runtime_lock(
            session_id,
            runtime_request,
        ):
            return web.json_response(
                _openai_error(
                    "Could not persist the requested session model lock",
                    code="model_lock_persistence_failed",
                ),
                status=500,
            )
        lock_active = bool(runtime_request.get("require_model_lock"))
        if lock_active:
            route = runtime_request.get("route")
            session_model = None
            requested = runtime_request.get("requested") or {}
            agent_overrides: Dict[str, Any] = {}
            if requested.get("model"):
                agent_overrides["requested_model"] = requested["model"]
            if requested.get("provider"):
                agent_overrides["requested_provider"] = requested["provider"]
            if runtime_request.get("model_options"):
                agent_overrides["model_options"] = runtime_request["model_options"]
        else:
            try:
                from gateway.api_execution_context import (
                    normalize_model_identifier,
                )

                stored_model = normalize_model_identifier(
                    session.get("model")
                    if isinstance(session, dict)
                    else None,
                    field="stored API session model",
                ) or None
            except ValueError as exc:
                return _invalid_runtime_request_response(exc)
            stored_route = self._resolve_route(stored_model)
            route = stored_route or self._resolve_route(body.get("model"))
            session_model = stored_model if (stored_model and stored_route is None) else None
            agent_overrides = _request_agent_overrides(
                body,
                virtual_model=self._active_model_name(),
            )
            selection_error = self._request_route_conflict_error(
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                requested_model=agent_overrides.get("requested_model"),
                requested_provider=agent_overrides.get("requested_provider"),
                route=route,
            )
            if selection_error:
                return web.json_response(_openai_error(selection_error), status=400)
        history = await _await_if_needed(
            self._conversation_history_for_session(session_id)
        )
        result, usage = await self._run_agent(
            user_message=user_message,
            conversation_history=history,
            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            route=route,
            session_model=session_model,
            requested_runtime=runtime_request.get("requested") or {},
            route_source=runtime_request.get("route_source") or "global",
            confirmed_runtime_lock=lock_active,
            **agent_overrides,
        )
        outcome = _session_stream_outcome(result)
        result_mapping = result if isinstance(result, Mapping) else {}
        try:
            effective_session_id = _effective_internal_api_session_id(
                result_mapping,
                fallback=session_id,
                source="session_chat_result",
            )
        except _InvalidInternalAPISessionID:
            return _invalid_internal_session_id_response()
        final_response = _resolve_media_to_data_urls(
            result_mapping.get("final_response", "") or ""
        )
        headers = {"X-Hermes-Session-Id": effective_session_id}
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        if outcome["incomplete"]:
            headers["X-Hermes-Completed"] = "false"
            headers["X-Hermes-Partial"] = (
                "true" if outcome["partial"] else "false"
            )
        runtime = {}
        if isinstance(result, dict):
            runtime = result.get("runtime") or {}
        if not runtime and isinstance(usage, dict):
            runtime = usage.get("runtime") or {}
        runtime = self._sanitize_runtime_metadata(
            runtime=runtime,
            requested_runtime=runtime_request.get("requested"),
            route_source=runtime_request.get("route_source") or "global",
            model_lock=(
                "confirmed"
                if runtime and runtime_request.get("require_model_lock")
                else "accepted"
                if runtime_request.get("require_model_lock")
                else ""
            ),
        )
        shared_outcome = {
            "status": outcome["status"],
            "completed": outcome["completed"],
            "partial": outcome["partial"],
            "interrupted": outcome["interrupted"],
            "failed": outcome["failed"],
            "incomplete": outcome["incomplete"],
            "turn_exit_reason": outcome["turn_exit_reason"],
            "terminal_outcome_contradictory": outcome[
                "terminal_outcome_contradictory"
            ],
        }
        if not final_response and outcome["incomplete"]:
            error_payload = _openai_error(
                "Agent run did not produce a response.",
                err_type="server_error",
                code="agent_incomplete",
            )
            error_payload["error"]["hermes"] = shared_outcome
            return web.json_response(error_payload, status=502, headers=headers)
        return web.json_response(
            {
                "object": "hermes.session.chat.completion",
                "session_id": effective_session_id,
                "message": {"role": "assistant", "content": final_response},
                "usage": usage,
                "runtime": runtime,
                "outcome": shared_outcome,
            },
            headers=headers,
        )

    @_admit_api_agent_request
    async def _handle_session_chat_stream(self, request: "web.Request") -> "web.StreamResponse":
        """POST /api/sessions/{session_id}/chat/stream — SSE wrapper over _run_agent."""
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        session, err = await _await_if_needed(
            self._get_existing_session_or_404(session_id)
        )
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        # Runtime selection — mirrors _handle_session_chat (lock wins,
        # otherwise session-persisted model then per-request values).
        try:
            runtime_request = self._effective_session_runtime_request(
                session=session,
                body=body,
            )
        except ValueError as exc:
            return _invalid_runtime_request_response(exc)
        lock_error = self._runtime_lock_error(runtime_request)
        if lock_error is not None:
            return lock_error
        if not await self._persist_session_runtime_lock(
            session_id,
            runtime_request,
        ):
            return web.json_response(
                _openai_error(
                    "Could not persist the requested session model lock",
                    code="model_lock_persistence_failed",
                ),
                status=500,
            )
        lock_active = bool(runtime_request.get("require_model_lock"))
        if lock_active:
            route = runtime_request.get("route")
            session_model = None
            requested = runtime_request.get("requested") or {}
            agent_overrides: Dict[str, Any] = {}
            if requested.get("model"):
                agent_overrides["requested_model"] = requested["model"]
            if requested.get("provider"):
                agent_overrides["requested_provider"] = requested["provider"]
            if runtime_request.get("model_options"):
                agent_overrides["model_options"] = runtime_request["model_options"]
        else:
            try:
                from gateway.api_execution_context import (
                    normalize_model_identifier,
                )

                stored_model = normalize_model_identifier(
                    session.get("model")
                    if isinstance(session, dict)
                    else None,
                    field="stored API session model",
                ) or None
            except ValueError as exc:
                return _invalid_runtime_request_response(exc)
            stored_route = self._resolve_route(stored_model)
            route = stored_route or self._resolve_route(body.get("model"))
            session_model = stored_model if (stored_model and stored_route is None) else None
            agent_overrides = _request_agent_overrides(
                body,
                virtual_model=self._active_model_name(),
            )
            selection_error = self._request_route_conflict_error(
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                requested_model=agent_overrides.get("requested_model"),
                requested_provider=agent_overrides.get("requested_provider"),
                route=route,
            )
            if selection_error:
                return web.json_response(_openai_error(selection_error), status=400)
        runtime_meta = self._sanitize_runtime_metadata(
            requested_runtime=runtime_request.get("requested"),
            route_source=runtime_request.get("route_source") or "global",
            model_lock=("accepted" if lock_active else ""),
        )

        reservation = self._reserve_agent_run()
        if reservation is None:
            return self._concurrency_limit_response()

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Optional[tuple[str, Dict[str, Any]]]]" = asyncio.Queue()
        message_id = f"msg_{uuid.uuid4().hex}"
        run_id = f"run_{uuid.uuid4().hex}"
        agent_ref: list[Any] = [None]
        cleanup_ref: list[Any] = [None]
        self._publish_api_authority_not_created(cleanup_ref, None)
        seq = 0

        def _event_payload(name: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
            nonlocal seq
            seq += 1
            payload.setdefault("session_id", session_id)
            payload.setdefault("run_id", run_id)
            payload.setdefault("seq", seq)
            payload.setdefault("ts", time.time())
            return name, payload

        def _enqueue(name: str, payload: Dict[str, Any]) -> None:
            event = _event_payload(name, payload)
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            try:
                if running_loop is loop:
                    queue.put_nowait(event)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

        def _delta(delta: str) -> None:
            if delta:
                _enqueue("assistant.delta", {"message_id": message_id, "delta": delta})

        def _tool_progress(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs) -> None:
            if event_type == "reasoning.available":
                _enqueue("tool.progress", {"message_id": message_id, "tool_name": tool_name or "_thinking", "delta": preview or ""})
            elif event_type in {"tool.started", "tool.completed", "tool.failed"}:
                event_name = event_type.replace("tool.", "tool.")
                _enqueue(event_name, {"message_id": message_id, "tool_name": tool_name, "preview": preview, "args": args})

        def _clarify_notify(payload: Dict[str, Any]) -> None:
            _enqueue("clarify.request", {"message_id": message_id, **payload})

        def _approval_event(name: str, payload: Dict[str, Any]) -> None:
            _enqueue(name, {"message_id": message_id, **payload})

        def _cleanup_state(state: Dict[str, Any]) -> None:
            if state.get("status") in {"cleanup_blocked", "cleanup_degraded"}:
                _enqueue(
                    "run.cleanup_blocked",
                    {
                        "message_id": message_id,
                        "status": "cleanup_blocked",
                        "completed": False,
                        "incomplete": True,
                        "cleanup": state,
                    },
                )

        async def _run_and_signal() -> None:
            terminal_emitted = False
            try:
                await queue.put(_event_payload("run.started", {
                    "user_message": {"role": "user", "content": user_message},
                    "runtime": runtime_meta,
                }))
                await queue.put(_event_payload("message.started", {"message": {"id": message_id, "role": "assistant"}}))
                history = await _await_if_needed(
                    self._conversation_history_for_session(session_id)
                )
                result, usage = await self._run_agent(
                    user_message=user_message,
                    conversation_history=history,
                    ephemeral_system_prompt=system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_delta,
                    tool_progress_callback=_tool_progress,
                    agent_ref=agent_ref,
                    cleanup_ref=cleanup_ref,
                    cleanup_state_callback=_cleanup_state,
                    gateway_session_key=gateway_session_key,
                    route=route,
                    session_model=session_model,
                    requested_runtime=runtime_request.get("requested") or {},
                    route_source=runtime_request.get("route_source") or "global",
                    confirmed_runtime_lock=lock_active,
                    **agent_overrides,
                )
                outcome = _session_stream_outcome(result)
                result_mapping = dict(result) if isinstance(result, Mapping) else {}
                final_response = _resolve_media_to_data_urls(
                    result_mapping.get("final_response", "") or ""
                )
                effective_session_id = _effective_internal_api_session_id(
                    result_mapping,
                    fallback=session_id,
                    source="session_chat_stream_result",
                )
                turn_messages = (
                    self._turn_transcript_messages(
                        history, user_message, result_mapping
                    )
                    if result_mapping
                    else []
                )
                effective_runtime = {}
                if result_mapping:
                    effective_runtime = result_mapping.get("runtime") or {}
                if not effective_runtime and isinstance(usage, dict):
                    effective_runtime = usage.get("runtime") or {}
                effective_runtime = self._sanitize_runtime_metadata(
                    runtime=effective_runtime,
                    requested_runtime=runtime_request.get("requested"),
                    route_source=runtime_request.get("route_source") or "global",
                    model_lock=(
                        "confirmed"
                        if effective_runtime and runtime_request.get("require_model_lock")
                        else "accepted"
                        if runtime_request.get("require_model_lock")
                        else ""
                    ),
                )
                shared_outcome = {
                    "status": outcome["status"],
                    "completed": outcome["completed"],
                    "partial": outcome["partial"],
                    "interrupted": outcome["interrupted"],
                    "failed": outcome["failed"],
                    "incomplete": outcome["incomplete"],
                    "turn_exit_reason": outcome["turn_exit_reason"],
                    "terminal_outcome_contradictory": outcome[
                        "terminal_outcome_contradictory"
                    ],
                }
                await queue.put(_event_payload(outcome["assistant_event"], {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "content": final_response,
                    "runtime": effective_runtime,
                    **shared_outcome,
                }))
                run_payload = {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "messages": turn_messages,
                    "usage": usage,
                    "runtime": effective_runtime,
                    **shared_outcome,
                }
                if result_mapping.get("error"):
                    run_payload["error"] = _redact_api_error_text(
                        result_mapping["error"]
                    )
                await queue.put(
                    _event_payload(outcome["run_event"], run_payload)
                )
                terminal_emitted = True
            except Exception as exc:
                logger.exception("[api_server] session chat stream failed")
                if not self._api_cleanup_allows_terminal(cleanup_ref):
                    state = cleanup_ref[0] if cleanup_ref else None
                    await queue.put(_event_payload("run.cleanup_blocked", {
                        "message_id": message_id,
                        "status": "cleanup_blocked",
                        "completed": False,
                        "incomplete": True,
                        "terminal": False,
                        "error": _redact_api_error_text(exc),
                        "cleanup": dict(state)
                        if isinstance(state, Mapping)
                        else None,
                    }))
                    return
                invalid_internal_session_id = isinstance(
                    exc,
                    _InvalidInternalAPISessionID,
                )
                failure = _session_stream_outcome(None)
                shared_failure = {
                    "status": failure["status"],
                    "completed": failure["completed"],
                    "partial": failure["partial"],
                    "interrupted": failure["interrupted"],
                    "failed": failure["failed"],
                    "incomplete": failure["incomplete"],
                    "turn_exit_reason": (
                        "invalid_internal_session_id"
                        if invalid_internal_session_id
                        else "api_run_exception"
                    ),
                    "terminal_outcome_contradictory": failure[
                        "terminal_outcome_contradictory"
                    ],
                }
                error_text = (
                    "Internal session continuity state is invalid."
                    if invalid_internal_session_id
                    else _redact_api_error_text(exc)
                )
                await queue.put(_event_payload(failure["assistant_event"], {
                    "message_id": message_id,
                    "content": "",
                    "error_code": (
                        "invalid_internal_session_id"
                        if invalid_internal_session_id
                        else "agent_error"
                    ),
                    **shared_failure,
                }))
                await queue.put(_event_payload(failure["run_event"], {
                    "message_id": message_id,
                    "messages": [],
                    "error": error_text,
                    "error_code": (
                        "invalid_internal_session_id"
                        if invalid_internal_session_id
                        else "agent_error"
                    ),
                    **shared_failure,
                }))
                terminal_emitted = True
            finally:
                if terminal_emitted:
                    await queue.put(_event_payload("done", {}))
                    await queue.put(None)

        try:
            task = asyncio.create_task(_run_and_signal())
        except Exception:
            reservation.release()
            raise
        task.add_done_callback(lambda _task: reservation.release())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Hermes-Session-Id": session_id,
        }
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if item is None:
                    break
                name, payload = item
                data = json.dumps(payload, ensure_ascii=False)
                await response.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
        except (asyncio.CancelledError, ConnectionResetError):
            agent = agent_ref[0]
            if agent is not None:
                try:
                    agent.interrupt("Session SSE client disconnected")
                except Exception:
                    pass
            # ``task`` is already strongly tracked in ``_background_tasks``.
            # Do not cancel it: it owns the executor and exact cleanup receipt,
            # and will emit no terminal queue event before that receipt exists.
            raise
        except Exception as exc:
            logger.debug("[api_server] session SSE stream error: %s", exc)
            agent = agent_ref[0]
            if agent is not None:
                try:
                    agent.interrupt("Session SSE writer failed")
                except Exception:
                    pass
        return response

    async def _handle_session_model_lock(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/model — backend-ack a Browser model lock."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        _, err = await _await_if_needed(
            self._get_existing_session_or_404(session_id)
        )
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        try:
            runtime_request = self._session_runtime_request_from_body(body)
        except ValueError as exc:
            return _invalid_runtime_request_response(exc)
        runtime_request["require_model_lock"] = True
        lock_error = self._runtime_lock_error(runtime_request)
        if lock_error is not None:
            return lock_error
        if not await self._persist_session_runtime_lock(
            session_id,
            runtime_request,
        ):
            return web.json_response(
                _openai_error(
                    "Could not persist the requested session model lock",
                    code="model_lock_persistence_failed",
                ),
                status=500,
            )
        requested = runtime_request.get("requested") or {}
        route = runtime_request.get("route") or {}
        runtime = self._sanitize_runtime_metadata(
            runtime={
                "provider": route.get("provider") or requested.get("provider") or "",
                "model": route.get("model") or requested.get("model") or "",
                "route_source": runtime_request.get("route_source") or "raw_request",
            },
            requested_runtime=requested,
            route_source=runtime_request.get("route_source") or "raw_request",
            model_lock="accepted",
        )
        return web.json_response({
            "object": "hermes.session.model_lock",
            "session_id": session_id,
            "runtime": runtime,
        })
    @_admit_api_agent_request
    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions — OpenAI Chat Completions format."""
        # Bound total in-flight agent runs (configurable; #7483).
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(_openai_error("Invalid JSON in request body"), status=400)

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        stream = _coerce_request_bool(body.get("stream"), default=False)

        # Extract system message (becomes ephemeral system prompt layered ON TOP of core)
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            raw_content = msg.get("content", "")
            if role == "system":
                # System messages don't support images (Anthropic rejects, OpenAI
                # text-model systems don't render them).  Flatten to text.
                content = _normalize_chat_content(raw_content)
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in {"user", "assistant"}:
                try:
                    content = _normalize_multimodal_content(raw_content)
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"messages[{idx}].content")
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message: Any = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not _content_has_visible_payload(user_message):
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        # Allow caller to scope long-term memory (e.g. Honcho) with a
        # stable per-channel identifier via X-Hermes-Session-Key.  This
        # is independent of X-Hermes-Session-Id: the key persists across
        # transcripts while the id rotates when the caller starts a new
        # transcript (i.e. /new semantics).  See _parse_session_key_header.
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # Allow caller to continue an existing session by passing X-Hermes-Session-Id.
        # When provided, history is loaded from state.db instead of from the request body.
        #
        # Security: session continuation exposes conversation history, so it is
        # only allowed when the API key is configured and the request is
        # authenticated.  Without this gate, any unauthenticated client could
        # read arbitrary session history by guessing/enumerating session IDs.
        raw_provided_session_id = request.headers.get(
            "X-Hermes-Session-Id",
            "",
        )
        provided_session_id = raw_provided_session_id.strip()
        if raw_provided_session_id:
            if not self._api_auth_configured():
                logger.warning(
                    "Session continuation via X-Hermes-Session-Id rejected: "
                    "no API key configured.  Set API_SERVER_KEY to enable "
                    "session continuity."
                )
                return web.json_response(
                    _openai_error(
                        "Session continuation requires API key authentication. "
                        "Configure API_SERVER_KEY to enable this feature."
                    ),
                    status=403,
                )
            session_id, session_id_error = self._validate_api_session_id_value(
                raw_provided_session_id,
                required=True,
            )
            if session_id_error is not None:
                return session_id_error
            try:
                db = await self._ensure_session_db_async()
                if db is not None:
                    history = await self._offload_session_db(
                        db.get_messages_as_conversation,
                        session_id,
                    )
            except Exception as e:
                logger.warning("Failed to load session history for %s: %s", session_id, e)
                history = []
        else:
            # Derive a stable session ID from the conversation fingerprint so
            # that consecutive messages from the same Open WebUI (or similar)
            # conversation map to the same Hermes session.  The first user
            # message + system prompt are constant across all turns.
            first_user = ""
            for cm in conversation_messages:
                if cm.get("role") == "user":
                    first_user = cm.get("content", "")
                    break
            session_id = _derive_chat_session_id(system_prompt, first_user)
            # history already set from request body above

        # Host runtime effects never come from the OpenAI-compatible JSON
        # body or a readable effect header.  The sole ingress is a one-use,
        # process-local capability minted by gateway.wake after it has
        # resolved the live compression tip and conversation-root authority.
        runtime_effect = None
        internal_wake_envelope: Optional[Dict[str, Any]] = None
        internal_idempotency_key = ""
        wake_authority: Optional[APIRequestScope] = None
        from gateway.wake import (
            INTERNAL_WAKE_TOKEN_HEADER,
            InternalWakeTokenError,
            consume_internal_wake_token,
        )

        internal_wake_token = request.headers.get(
            INTERNAL_WAKE_TOKEN_HEADER,
            "",
        ).strip()
        if internal_wake_token:
            internal_idempotency_key = request.headers.get(
                "Idempotency-Key",
                "",
            ).strip()
            if (
                not provided_session_id
                or not isinstance(user_message, str)
                or stream
                or not internal_idempotency_key
                or system_prompt is not None
                or len(conversation_messages) != 1
            ):
                return web.json_response(
                    _openai_error(
                        "Invalid internal wake capability",
                        code="invalid_internal_wake",
                    ),
                    status=403,
                )
            try:
                wake_authority = (
                    _api_request_authority.get()
                    or self._api_request_scope(
                        "internal-wake",
                        session_id,
                    )
                )
                internal_wake_envelope = consume_internal_wake_token(
                    internal_wake_token,
                    session_id=session_id,
                    text=user_message,
                    idempotency_key=internal_idempotency_key,
                    gateway_session_key=gateway_session_key or "",
                    profile=wake_authority.profile,
                    source_home=wake_authority.source_home,
                    canonical_home=wake_authority.canonical_home,
                    profile_generation=(
                        wake_authority.profile_generation
                    ),
                    return_envelope=True,
                )
                runtime_effect = (
                    internal_wake_envelope or {}
                ).get("runtime_effect")
            except InternalWakeTokenError:
                logger.warning(
                    "Rejected invalid/replayed internal wake capability for "
                    "session %s",
                    session_id,
                )
                return web.json_response(
                    _openai_error(
                        "Invalid or expired internal wake capability",
                        code="invalid_internal_wake",
                    ),
                    status=403,
                )

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", self._active_model_name())
        created = int(time.time())

        # Per-client model routing: if the requested model matches a
        # configured model_routes alias, this request's agent is created
        # with that route's model/provider instead of the global default.
        route = self._resolve_route(model_name)
        try:
            agent_overrides = _request_agent_overrides(
                body,
                virtual_model=self._active_model_name(),
                allow_bare_model=self._direct_model_requests_enabled(),
            )
        except ValueError as exc:
            return _invalid_runtime_request_response(exc)
        api_execution_context = (
            (internal_wake_envelope or {}).get("execution_context")
        )
        wake_session_model = None
        wake_requested_runtime = None
        wake_route_source = "global"
        wake_confirmed_runtime_lock = False
        if api_execution_context is not None:
            route = self._route_from_api_execution_context(
                api_execution_context
            )
            agent_overrides = {
                "requested_model": (
                    api_execution_context.get("request_model") or None
                ),
                "requested_provider": (
                    api_execution_context.get("request_provider") or None
                ),
                "model_options": dict(
                    api_execution_context.get("model_options") or {}
                ),
            }
            wake_session_model = (
                api_execution_context.get("session_model") or None
            )
            wake_requested_runtime = dict(
                api_execution_context.get("requested_runtime") or {}
            )
            wake_route_source = str(
                api_execution_context.get("route_source") or "global"
            )
            wake_confirmed_runtime_lock = bool(
                api_execution_context.get("confirmed_runtime_lock")
            )
        selection_error = self._request_route_conflict_error(
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            requested_model=agent_overrides.get("requested_model"),
            requested_provider=agent_overrides.get("requested_provider"),
            route=route,
        )

        durable_wake_execution: Optional[Dict[str, Any]] = None
        if (internal_wake_envelope or {}).get("durable_wake_required"):
            envelope = internal_wake_envelope or {}
            delegation_id = str(
                envelope.get("durable_delegation_id") or ""
            ).strip()
            execution_owner = str(
                envelope.get("durable_execution_owner") or ""
            ).strip()
            profile_identity = envelope.get("profile_identity")
            expected_profile_identity = (
                {
                    "profile": wake_authority.profile,
                    "source_home": wake_authority.source_home,
                    "canonical_home": wake_authority.canonical_home,
                    "profile_generation": wake_authority.profile_generation,
                }
                if wake_authority is not None
                else None
            )
            if (
                execution_owner != "api"
                or not delegation_id
                or not internal_idempotency_key
                or profile_identity != expected_profile_identity
            ):
                return web.json_response(
                    _openai_error(
                        "Invalid durable wake capability",
                        code="invalid_internal_wake",
                    ),
                    status=403,
                )
            from tools.async_delegation import (
                EventDeliveryStore,
                claim_durable_wake_execution,
            )

            store = EventDeliveryStore(
                hermes_home=wake_authority.canonical_home,
                source_home=wake_authority.source_home,
                profile=wake_authority.profile,
                profile_generation=wake_authority.profile_generation,
            )
            try:
                durable_claim = claim_durable_wake_execution(
                    delegation_id=delegation_id,
                    idempotency_key=internal_idempotency_key,
                    store=store,
                )
            except BaseException as exc:
                if isinstance(
                    exc,
                    (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                ):
                    raise
                logger.exception(
                    "Durable API wake claim failed for %s",
                    delegation_id,
                )
                return _durable_wake_deferred_response(
                    message=(
                        "Durable wake claim storage is temporarily "
                        "unavailable."
                    ),
                    code="durable_wake_claim_unavailable",
                )
            if durable_claim.state == "completed":
                try:
                    return _durable_wake_replay_response(
                        durable_claim.response
                    )
                except (TypeError, ValueError):
                    logger.exception(
                        "Durable API wake replay is malformed for %s",
                        delegation_id,
                    )
                    return _durable_wake_deferred_response(
                        message=(
                            "Durable wake terminal settlement is "
                            "temporarily unavailable."
                        ),
                        code="durable_wake_settlement_unavailable",
                    )
            if durable_claim.state == "in_progress":
                return _durable_wake_in_progress_response(
                    durable_claim.reason
                )
            if durable_claim.state == "uncertain":
                return _durable_wake_uncertain_response(
                    session_id=session_id,
                    reason=durable_claim.reason,
                )
            durable_wake_execution = {
                "delegation_id": delegation_id,
                "idempotency_key": internal_idempotency_key,
                "claim_id": durable_claim.claim_id,
                "store": store,
            }

        if selection_error:
            if durable_wake_execution is not None:
                return _settle_durable_wake_uncertainty(
                    durable_wake_execution,
                    session_id=session_id,
                    disposition_reason=(
                        "durable wake execution context conflicts with the "
                        "target session route"
                    ),
                    response_reason=(
                        "durable wake route selection was rejected"
                    ),
                )
            return web.json_response(
                _openai_error(selection_error),
                status=400,
            )

        if stream:
            reservation = self._reserve_agent_run()
            if reservation is None:
                return self._concurrency_limit_response()
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # Filter out None — the agent fires stream_delta_callback(None)
                # to signal the CLI display to close its response box before
                # tool execution, but the SSE writer uses None as end-of-stream
                # sentinel.  Forwarding it would prematurely close the HTTP
                # response, causing Open WebUI (and similar frontends) to miss
                # the final answer after tool calls.  The SSE loop detects
                # completion via agent_task.done() instead.
                if delta is not None:
                    _stream_q.put(delta)

            # Track which tool_call_ids we've emitted a "running" lifecycle
            # event for, so a "completed" event without a matching "running"
            # (e.g. internal/filtered tools) is silently dropped instead of
            # producing an orphaned event clients can't correlate.
            _started_tool_call_ids: set[str] = set()

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Emit ``hermes.tool.progress`` with ``status: running``.

                Replaces the old ``tool_progress_callback("tool.started",
                ...)`` emit so SSE consumers receive a single event per
                tool start, carrying both the legacy ``tool``/``emoji``/
                ``label`` payload (for #6972 frontends) and the new
                ``toolCallId``/``status`` correlation fields (#16588).

                Skips tools whose names start with ``_`` so internal
                events (``_thinking``, …) stay off the wire — matching
                the prior ``_on_tool_progress`` filter exactly.
                """
                if not tool_call_id or function_name.startswith("_"):
                    return
                _started_tool_call_ids.add(tool_call_id)
                from agent.display import build_tool_preview, get_tool_emoji
                label = build_tool_preview(function_name, function_args) or function_name
                _stream_q.put(("__tool_progress__", {
                    "tool": function_name,
                    "emoji": get_tool_emoji(function_name),
                    "label": label,
                    "toolCallId": tool_call_id,
                    "status": "running",
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Emit the matching ``status: completed`` event.

                Dropped if the start was filtered (internal tool, missing
                id, or never seen) so clients never get an orphaned
                ``completed`` they can't correlate to a prior ``running``.
                """
                if not tool_call_id or tool_call_id not in _started_tool_call_ids:
                    return
                _started_tool_call_ids.discard(tool_call_id)
                _stream_q.put(("__tool_progress__", {
                    "tool": function_name,
                    "toolCallId": tool_call_id,
                    "status": "completed",
                }))

            def _on_clarify(payload: Dict[str, Any]) -> None:
                _stream_q.put(("__clarify_request__", payload))

            def _on_approval(name: str, payload: Dict[str, Any]) -> None:
                tag = (
                    "__approval_request__"
                    if name == "approval.request"
                    else "__approval_responded__"
                )
                _stream_q.put((tag, payload))

            # Start agent in background.  agent_ref is a mutable container
            # so the SSE writer can interrupt the agent on client disconnect.
            #
            # ``tool_progress_callback`` is intentionally not wired here:
            # it would duplicate every emit because ``run_agent`` fires it
            # side-by-side with ``tool_start_callback``/``tool_complete_callback``.
            # The structured callbacks are strictly richer (they carry
            # the tool_call id), so they own the chat-completions SSE channel.
            agent_ref = [None]
            cleanup_ref = [None]
            self._publish_api_authority_not_created(cleanup_ref, None)
            try:
                agent_task = asyncio.ensure_future(
                    self._run_agent_from_reservation(
                        reservation,
                        user_message=user_message,
                        conversation_history=history,
                        ephemeral_system_prompt=system_prompt,
                        session_id=session_id,
                        stream_delta_callback=_on_delta,
                        tool_start_callback=_on_tool_start,
                        tool_complete_callback=_on_tool_complete,
                        agent_ref=agent_ref,
                        cleanup_ref=cleanup_ref,
                        gateway_session_key=gateway_session_key,
                        **agent_overrides,
                        route=route,
                        clarify_notify_callback=_on_clarify,
                        approval_event_callback=_on_approval,
                        runtime_effect=runtime_effect,
                        session_model=wake_session_model,
                        requested_runtime=wake_requested_runtime,
                        route_source=wake_route_source,
                        confirmed_runtime_lock=wake_confirmed_runtime_lock,
                        api_execution_context=api_execution_context,
                    )
                )
            except Exception:
                reservation.release()
                raise
            agent_task.add_done_callback(lambda _task: reservation.release())
            # Ensure SSE drain loops can terminate without relying on polling
            # agent_task.done(), which can race with queue timeout checks.
            agent_task.add_done_callback(lambda _fut: _stream_q.put(None))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
                gateway_session_key=gateway_session_key,
                cleanup_ref=cleanup_ref,
            )

        reservation = self._reserve_agent_run()
        if reservation is None:
            if durable_wake_execution is not None:
                from tools.async_delegation import (
                    release_durable_wake_execution,
                )
                try:
                    released = release_durable_wake_execution(
                        delegation_id=durable_wake_execution["delegation_id"],
                        idempotency_key=durable_wake_execution[
                            "idempotency_key"
                        ],
                        claim_id=durable_wake_execution["claim_id"],
                        store=durable_wake_execution["store"],
                    )
                except BaseException:
                    logger.exception(
                        "Durable wake capacity claim release failed"
                    )
                    released = False
                if released:
                    return self._concurrency_limit_response()
                return _settle_durable_wake_uncertainty(
                    durable_wake_execution,
                    session_id=session_id,
                    disposition_reason=(
                        "durable wake capacity release owner-CAS failed "
                        "before the agent started"
                    ),
                    response_reason=(
                        "durable wake capacity claim could not be released"
                    ),
                )
            return self._concurrency_limit_response()

        # Non-streaming: run the agent (with optional Idempotency-Key)
        async def _compute_completion():
            computed_result, computed_usage = await self._run_agent_from_reservation(
                reservation,
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                **agent_overrides,
                route=route,
                runtime_effect=runtime_effect,
                session_model=wake_session_model,
                requested_runtime=wake_requested_runtime,
                route_source=wake_route_source,
                confirmed_runtime_lock=wake_confirmed_runtime_lock,
                api_execution_context=api_execution_context,
            )
            # Validate before ``IdempotencyCache.get_or_set`` can retain the
            # result.  The response builder repeats the check when replaying a
            # previously cached value.
            if durable_wake_execution is None:
                _effective_internal_api_session_id(
                    computed_result,
                    fallback=session_id,
                    source="chat_completion_result_before_cache",
                )
            return computed_result, computed_usage

        idempotency_key = request.headers.get("Idempotency-Key")
        try:
            if durable_wake_execution is not None:
                # The durable owner CAS is the idempotency authority.  Never
                # hide the live execution behind the process-local cache:
                # cancellation and completion must settle this exact claim.
                result, usage = await _compute_completion()
            elif idempotency_key:
                cache_key = _scoped_idempotency_cache_key(
                    idempotency_key,
                    adapter_scope=getattr(
                        self,
                        "_idempotency_adapter_scope",
                        f"legacy-adapter:{id(self)}",
                    ),
                    request_scope=(
                        _api_request_authority.get()
                        or self._api_request_scope("request")
                    ),
                    session_id=(
                        str(
                            (internal_wake_envelope or {}).get(
                                "origin_session_id"
                            )
                            or ""
                        )
                        or session_id
                    ),
                    gateway_session_key=gateway_session_key,
                )
                fp = _make_request_fingerprint(
                    body,
                    keys=[
                        "model",
                        "provider",
                        "model_options",
                        "messages",
                        "tools",
                        "tool_choice",
                        "stream",
                    ],
                )
                cache = getattr(self, "_idempotency_cache", _idem_cache)
                result, usage = await cache.get_or_set(
                    cache_key,
                    fp,
                    _compute_completion,
                )
            else:
                result, usage = await _compute_completion()
        except _InvalidInternalAPISessionID:
            return _invalid_internal_session_id_response()
        except BaseException as e:
            if durable_wake_execution is not None:
                settled_response = _settle_durable_wake_uncertainty(
                    durable_wake_execution,
                    session_id=session_id,
                    disposition_reason=(
                        "durable wake agent execution ended before its "
                        "canonical response was committed"
                    ),
                    response_reason="durable wake agent execution failed",
                )
                if isinstance(
                    e,
                    (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                ):
                    raise
                return settled_response
            if isinstance(
                e,
                (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
            ):
                raise
            logger.error("Error running agent for chat completions: %s", e, exc_info=True)
            return web.json_response(
                _openai_error(f"Internal server error: {e}", err_type="server_error"),
                status=500,
            )
        finally:
            reservation.release()

        try:
            response_data, response_status, response_headers = (
                _chat_completion_http_parts(
                    result=result,
                    usage=usage,
                    completion_id=completion_id,
                    model_name=model_name,
                    created=created,
                    session_id=session_id,
                    gateway_session_key=gateway_session_key or "",
                )
            )
            if durable_wake_execution is None:
                return web.json_response(
                    response_data,
                    status=response_status,
                    headers=response_headers,
                )

            response_record = _durable_wake_response_record(
                response_data,
                # The internal self-poster treats a 2xx as terminal ACK.  The
                # original normalized HTTP status is retained verbatim in the
                # durable record while known failed/interrupted/partial
                # outcomes remain explicit in the response body and headers.
                status=200,
                terminal_status=response_status,
                headers=response_headers,
            )
            # Construct the live response before committing, so a
            # serialization failure cannot strand a completed durable row
            # whose first owner never had a response to send.
            live_response = _durable_wake_replay_response(response_record)
            from tools.async_delegation import (
                complete_durable_wake_execution,
            )

            completed = complete_durable_wake_execution(
                delegation_id=durable_wake_execution["delegation_id"],
                idempotency_key=durable_wake_execution["idempotency_key"],
                claim_id=durable_wake_execution["claim_id"],
                response=response_record,
                store=durable_wake_execution["store"],
            )
            if not completed:
                return _settle_durable_wake_uncertainty(
                    durable_wake_execution,
                    session_id=session_id,
                    disposition_reason=(
                        "durable wake completion owner-CAS failed; outcome "
                        "may include effects"
                    ),
                    response_reason=(
                        "durable wake completion could not be committed"
                    ),
                )
            return live_response
        except BaseException as exc:
            if durable_wake_execution is not None:
                settled_response = _settle_durable_wake_uncertainty(
                    durable_wake_execution,
                    session_id=session_id,
                    disposition_reason=(
                        "durable wake response serialization or completion "
                        "failed; outcome may include effects"
                    ),
                    response_reason=(
                        "durable wake response could not be committed"
                    ),
                )
                if isinstance(
                    exc,
                    (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                ):
                    raise
                return settled_response
            raise

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
        gateway_session_key: str = None, cleanup_ref: Optional[list] = None,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue.

        If the client disconnects mid-stream (network drop, browser tab close),
        the agent is interrupted and its task remains strongly tracked until
        exact cleanup confirms.  The wrapper is never used as a cancellation
        shortcut because executor cancellation cannot stop ``run_conversation``.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        # CORS middleware can't inject headers into StreamResponse after
        # prepare() flushes them, so resolve CORS headers up front.
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            last_activity = time.monotonic()

            # Role chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
            last_activity = time.monotonic()

            # Helper — route a queue item to the correct SSE event.
            async def _emit(item):
                """Write a single queue item to the SSE stream.

                Plain strings are sent as normal ``delta.content`` chunks.
                Tagged tuples ``("__tool_progress__", payload)`` are sent
                as a custom ``event: hermes.tool.progress`` SSE event so
                frontends can display them without storing the markers in
                conversation history.  See #6972 for the original event,
                #16588 for the ``toolCallId``/``status`` lifecycle fields.
                """
                if isinstance(item, tuple) and len(item) == 2:
                    if item[0] == "__tool_progress__":
                        event_data = json.dumps(item[1])
                        await response.write(
                            f"event: hermes.tool.progress\ndata: {event_data}\n\n".encode()
                        )
                    elif item[0] == "__clarify_request__":
                        event_data = json.dumps(item[1])
                        await response.write(
                            f"event: hermes.clarify.request\ndata: {event_data}\n\n".encode()
                        )
                    elif item[0] == "__approval_request__":
                        event_data = json.dumps(item[1])
                        await response.write(
                            f"event: hermes.approval.request\ndata: {event_data}\n\n".encode()
                        )
                    elif item[0] == "__approval_responded__":
                        event_data = json.dumps(item[1])
                        await response.write(
                            f"event: hermes.approval.responded\ndata: {event_data}\n\n".encode()
                        )
                else:
                    content_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"content": item}, "finish_reason": None}],
                    }
                    await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                return time.monotonic()

            # Stream content chunks as they arrive from the agent
            loop = asyncio.get_running_loop()
            while True:
                try:
                    delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain any remaining items
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                last_activity = await _emit(delta)
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if delta is None:  # End of stream sentinel
                    break

                last_activity = await _emit(delta)

            # Get usage from completed agent. The agent can fail two ways
            # after the content queue terminates cleanly: (1) ``agent_task``
            # raises, or (2) it returns a ``result`` dict flagged
            # failed/partial/incomplete. Both previously fell through to a
            # ``finish_reason: "stop"`` chunk, so OpenAI-compatible clients
            # saw a fake success. Surface either as a non-"stop" finish so
            # the failure is detectable — mirroring the non-streaming path's
            # decision logic (see the finish_reason block above).
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            result = None
            agent_error = None
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
                _effective_internal_api_session_id(
                    result,
                    fallback=session_id or "",
                    source="chat_completion_stream_result",
                )
            except _InvalidInternalAPISessionID as exc:
                agent_error = exc
                result = None
                logger.error(
                    "Agent task %s returned an invalid internal session ID",
                    completion_id,
                )
            except Exception as exc:
                agent_error = exc
                logger.error(
                    "Agent task %s failed during SSE streaming: %s", completion_id, exc
                )

            if not self._api_cleanup_allows_terminal(cleanup_ref):
                logger.error(
                    "SSE task %s finished without an exact cleanup receipt; "
                    "terminal chunk withheld",
                    completion_id,
                )
                return response

            result_mapping = result if isinstance(result, Mapping) else {}
            outcome_input = result
            if agent_error is not None:
                outcome_input = {
                    "completed": False,
                    "failed": True,
                    "turn_exit_reason": "api_run_exception",
                    "error": str(agent_error),
                }
            outcome = _session_stream_outcome(outcome_input)
            invalid_internal_session_id = isinstance(
                agent_error,
                _InvalidInternalAPISessionID,
            )
            raw_err_msg = (
                "Internal session continuity state is invalid."
                if invalid_internal_session_id
                else str(agent_error)
                if agent_error is not None
                else result_mapping.get("error")
            )
            err_msg = (
                _redact_api_error_text(raw_err_msg) if raw_err_msg else raw_err_msg
            )
            finish_reason = outcome["finish_reason"]

            # Finish chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            if outcome["incomplete"]:
                finish_chunk["choices"][0]["delta"] = {}
                if err_msg:
                    finish_chunk["error"] = {
                        "message": err_msg,
                        "type": type(agent_error).__name__ if agent_error else "agent_error",
                    }
                finish_chunk["hermes"] = {
                    "status": outcome["status"],
                    "completed": outcome["completed"],
                    "partial": outcome["partial"],
                    "interrupted": outcome["interrupted"],
                    "failed": outcome["failed"],
                    "incomplete": outcome["incomplete"],
                    "turn_exit_reason": (
                        "invalid_internal_session_id"
                        if invalid_internal_session_id
                        else outcome["turn_exit_reason"]
                    ),
                    "terminal_outcome_contradictory": outcome[
                        "terminal_outcome_contradictory"
                    ],
                    "error": err_msg,
                    "error_code": (
                        "invalid_internal_session_id"
                        if invalid_internal_session_id
                        else "output_truncated"
                        if finish_reason == "length"
                        else "agent_error"
                    ),
                }
            await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Capture and reap only the background processes owned by this
            # abandoned turn before the worker clears its ownership markers.
            # The exact Canonical cleanup below remains the sole owner of
            # capability revocation and is deliberately not cancelled.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                _reap_disconnected_agent_processes(agent)
            await self._interrupt_and_await_api_task(
                agent_task,
                agent_ref,
                cleanup_ref,
                reason="SSE client disconnected",
            )
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)
        except asyncio.CancelledError:
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                _reap_disconnected_agent_processes(
                    agent, source="api_server_sse_cancelled"
                )
            await self._interrupt_and_await_api_task(
                agent_task,
                agent_ref,
                cleanup_ref,
                reason="SSE task cancelled",
            )
            raise
        except Exception as _exc:
            # A writer failure is not terminal while executor authority is
            # uncertain.  Interrupt, then emit the error terminator only after
            # the exact cleanup receipt confirms within the bounded wait.
            import traceback as _tb
            logger.error("Agent crashed mid-stream for %s: %s", completion_id, _tb.format_exc()[:300])
            cleanup_confirmed = await self._interrupt_and_await_api_task(
                agent_task,
                agent_ref,
                cleanup_ref,
                reason="SSE writer failed",
            )
            if cleanup_confirmed:
                try:
                    error_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                        "hermes": {
                            "status": "failed",
                            "completed": False,
                            "partial": False,
                            "interrupted": False,
                            "failed": True,
                            "incomplete": True,
                            "turn_exit_reason": "sse_writer_exception",
                        },
                    }
                    await response.write(f"data: {json.dumps(error_chunk)}\n\n".encode())
                    await response.write(b"data: [DONE]\n\n")
                except Exception:
                    pass

        return response

    def _response_store_home_for_request(self) -> Path:
        """Return the host-authoritative home for the current API request.

        Response persistence consumes the same immutable authority as every
        other API cache/control domain; it never re-resolves a caller path.
        """
        scope = self._api_request_scope("response-store")
        verify_api_request_scope(scope)
        return Path(scope.canonical_home)

    def _response_store_for_request(self) -> ResponseStore:
        """Resolve and lazily open the current profile's Responses database."""
        response_scope = self._api_request_scope("response-store")
        verify_api_request_scope(response_scope)
        home = Path(response_scope.canonical_home)
        # The immutable listener inventory selects the only permissible
        # profile.  The active per-task runtime scope must independently agree
        # before opening state, so a stale ContextVar cannot redirect a
        # default-route operation into another tenant (or vice versa).
        from hermes_constants import get_hermes_home

        active_runtime_home = Path(get_hermes_home()).expanduser().resolve()
        if active_runtime_home != home:
            raise RuntimeError(
                f"API profile {response_scope.profile!r} does not match the "
                f"active runtime home {active_runtime_home}"
            )
        home_key = str(home)
        pool_key = response_scope.bind(
            "response-store",
            "",
        ).internal_key
        default_store = getattr(self, "_response_store", None)
        default_home = str(
            getattr(self, "_response_store_default_home", "") or ""
        )

        # Preserve the historical attribute as the default store.  A number
        # of integrations and lightweight test doubles replace it directly,
        # so it remains authoritative for the home it was created for.
        if default_store is not None and (
            home_key == default_home
            or (not default_home and not _api_request_profile.get())
        ):
            cache = getattr(self, "_response_stores_by_home", None)
            lock = getattr(self, "_response_stores_lock", None)
            if cache is not None:
                if lock is None:
                    cache[pool_key] = default_store
                else:
                    with lock:
                        cache[pool_key] = default_store
            return default_store

        lock = getattr(self, "_response_stores_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._response_stores_lock = lock
        with lock:
            cache = getattr(self, "_response_stores_by_home", None)
            if cache is None:
                cache = {}
                self._response_stores_by_home = cache
            response_store = cache.get(pool_key)
            if response_store is None:
                response_store = ResponseStore(
                    max_size=getattr(
                        default_store,
                        "_max_size",
                        MAX_STORED_RESPONSES,
                    ),
                    db_path=str(home / "response_store.db"),
                )
                cache[pool_key] = response_store
            return response_store

    async def _write_sse_responses(
        self,
        request: "web.Request",
        response_id: str,
        model: str,
        created_at: int,
        stream_q,
        agent_task,
        agent_ref,
        conversation_history: List[Dict[str, str]],
        user_message: str,
        instructions: Optional[str],
        conversation: Optional[str],
        store: bool,
        session_id: str,
        gateway_session_key: Optional[str] = None,
        cleanup_ref: Optional[list] = None,
        response_store: Optional[ResponseStore] = None,
    ) -> "web.StreamResponse":
        """Write an SSE stream for POST /v1/responses (OpenAI Responses API).

        Emits spec-compliant event types as the agent runs:

        - ``response.created`` — initial envelope (status=in_progress)
        - ``response.output_text.delta`` / ``response.output_text.done`` —
          streamed assistant text
        - ``response.output_item.added`` / ``response.output_item.done``
          with ``item.type == "function_call"`` — when the agent invokes a
          tool (both events fire; the ``done`` event carries the finalized
          ``arguments`` string)
        - ``response.output_item.added`` with
          ``item.type == "function_call_output"`` — tool result with
          ``{call_id, output, status}``
        - ``response.completed`` — terminal event carrying the full
          response object with all output items + usage (same payload
          shape as the non-streaming path for parity)
        - ``response.incomplete`` — terminal event for a non-failed run that
          did not complete
        - ``response.failed`` — terminal event on agent error

        If the client disconnects mid-stream, ``agent.interrupt()`` is called
        and the task remains strongly tracked.  A stored response stays
        ``in_progress`` with an exact cleanup detail until the Canonical
        tombstone confirms; only then may it become terminal.
        """
        import queue as _q

        if response_store is None:
            response_store = self._response_store_for_request()

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        # State accumulated during the stream
        final_text_parts: List[str] = []
        # Track open function_call items by name so we can emit a matching
        # ``done`` event when the tool completes.  Order preserved.
        pending_tool_calls: List[Dict[str, Any]] = []
        # Output items we've emitted so far (used to build the terminal
        # response.completed payload).  Kept in the order they appeared.
        emitted_items: List[Dict[str, Any]] = []
        # Monotonic counter for output_index (spec requires it).
        output_index = 0
        # Monotonic counter for call_id generation if the agent doesn't
        # provide one (it doesn't, from tool_progress_callback).
        call_counter = 0
        # Canonical Responses SSE events include a monotonically increasing
        # sequence_number. Add it server-side for every emitted event so
        # clients that validate the OpenAI event schema can parse our stream.
        sequence_number = 0
        # Track the assistant message item id + content index for text
        # delta events — the spec ties deltas to a specific item.
        message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        message_output_index: Optional[int] = None
        message_opened = False

        async def _write_event(event_type: str, data: Dict[str, Any]) -> None:
            nonlocal sequence_number
            if "sequence_number" not in data:
                data["sequence_number"] = sequence_number
            sequence_number += 1
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            await response.write(payload.encode())

        def _envelope(status: str) -> Dict[str, Any]:
            env: Dict[str, Any] = {
                "id": response_id,
                "object": "response",
                "status": status,
                "created_at": created_at,
                "model": model,
            }
            return env

        final_response_text = ""
        agent_error: Optional[str] = None
        usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        terminal_snapshot_persisted = False

        def _persist_response_snapshot(
            response_env: Dict[str, Any],
            *,
            conversation_history_snapshot: Optional[List[Dict[str, Any]]] = None,
            session_id_snapshot: Optional[str] = None,
        ) -> None:
            if not store:
                return
            if conversation_history_snapshot is None:
                conversation_history_snapshot = list(conversation_history)
                conversation_history_snapshot.append({"role": "user", "content": user_message})
            persisted_session_id = _validate_internal_api_session_id(
                (
                    session_id
                    if session_id_snapshot is None
                    else session_id_snapshot
                ),
                source="responses_stream_snapshot",
            )
            response_store.put(response_id, {
                "response": response_env,
                "conversation_history": conversation_history_snapshot,
                "instructions": instructions,
                "session_id": persisted_session_id,
            })
            if conversation and response_env.get("status") == "completed":
                response_store.set_conversation(conversation, response_id)

        def _persist_incomplete_if_needed() -> None:
            """Persist an ``incomplete`` snapshot if no terminal one was written.

            Called from both the client-disconnect (``ConnectionResetError``)
            and server-cancellation (``asyncio.CancelledError``) paths so
            GET /v1/responses/{id} and ``previous_response_id`` chaining keep
            working after abrupt stream termination.
            """
            nonlocal terminal_snapshot_persisted
            if not store or terminal_snapshot_persisted:
                return
            incomplete_text = "".join(final_text_parts) or final_response_text
            incomplete_items: List[Dict[str, Any]] = list(emitted_items)
            if incomplete_text:
                incomplete_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": incomplete_text}],
                })
            incomplete_env = _envelope("incomplete")
            incomplete_env["output"] = incomplete_items
            incomplete_env["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            incomplete_history = list(conversation_history)
            incomplete_history.append({"role": "user", "content": user_message})
            if incomplete_text:
                incomplete_history.append({"role": "assistant", "content": incomplete_text})
            _persist_response_snapshot(
                incomplete_env,
                conversation_history_snapshot=incomplete_history,
            )
            terminal_snapshot_persisted = True

        def _cleanup_state() -> Optional[Mapping[str, Any]]:
            state = cleanup_ref[0] if cleanup_ref else None
            return state if isinstance(state, Mapping) else None

        def _persist_cleanup_pending(reason: str) -> None:
            """Persist nonterminal truth while exact revoke is outstanding."""
            if not store or terminal_snapshot_persisted:
                return
            state = _cleanup_state()
            cleanup_status = (
                "cleanup_blocked"
                if state
                and state.get("status")
                in {"cleanup_blocked", "cleanup_degraded"}
                else "stopping"
            )
            partial_text = "".join(final_text_parts) or final_response_text
            pending_items: List[Dict[str, Any]] = list(emitted_items)
            if partial_text:
                pending_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": partial_text}],
                })
            pending_env = _envelope("in_progress")
            pending_env["output"] = pending_items
            pending_env["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            pending_env["hermes"] = {
                "status": cleanup_status,
                "terminal": False,
                "completed": False,
                "reason": reason,
                "cleanup": dict(state) if state else None,
            }
            pending_history = list(conversation_history)
            pending_history.append({"role": "user", "content": user_message})
            if partial_text:
                pending_history.append(
                    {"role": "assistant", "content": partial_text}
                )
            _persist_response_snapshot(
                pending_env,
                conversation_history_snapshot=pending_history,
            )

        def _persist_writer_failed_if_needed(error_text: str) -> Dict[str, Any]:
            nonlocal terminal_snapshot_persisted
            failed_env = _envelope("failed")
            failed_env["output"] = list(emitted_items)
            failed_env["error"] = {
                "message": _redact_api_error_text(error_text, limit=500),
                "type": "server_error",
            }
            failed_env["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            failed_env["hermes"] = {
                "status": "failed",
                "terminal": True,
                "completed": False,
                "failed": True,
                "incomplete": True,
                "turn_exit_reason": "sse_writer_exception",
                "cleanup": dict(_cleanup_state() or {}),
            }
            if not terminal_snapshot_persisted:
                _persist_response_snapshot(failed_env)
                terminal_snapshot_persisted = True
            return failed_env

        async def _finalize_aborted_stream_after_cleanup(
            reason: str,
            failure_error: Optional[str] = None,
        ) -> None:
            """Strong background owner for post-disconnect terminalization."""
            try:
                await asyncio.shield(agent_task)
            except asyncio.CancelledError:
                _persist_cleanup_pending(reason)
                return
            except Exception:
                # Execution errors are released only after cleanup confirms.
                pass
            if self._api_cleanup_allows_terminal(cleanup_ref):
                if failure_error:
                    _persist_writer_failed_if_needed(failure_error)
                else:
                    _persist_incomplete_if_needed()
            else:
                _persist_cleanup_pending(reason)

        def _track_aborted_stream_finalizer(
            reason: str,
            failure_error: Optional[str] = None,
        ) -> None:
            finalizer = asyncio.create_task(
                _finalize_aborted_stream_after_cleanup(reason, failure_error)
            )
            self._track_api_background_task(finalizer)

        try:
            # response.created — initial envelope, status=in_progress
            created_env = _envelope("in_progress")
            created_env["output"] = []
            await _write_event("response.created", {
                "type": "response.created",
                "response": created_env,
            })
            _persist_response_snapshot(created_env)
            last_activity = time.monotonic()

            async def _open_message_item() -> None:
                """Emit response.output_item.added for the assistant message
                the first time any text delta arrives."""
                nonlocal message_opened, message_output_index, output_index
                if message_opened:
                    return
                message_opened = True
                message_output_index = output_index
                output_index += 1
                item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": message_output_index,
                    "item": item,
                })

            async def _emit_text_delta(delta_text: str) -> None:
                await _open_message_item()
                final_text_parts.append(delta_text)
                await _write_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "delta": delta_text,
                    "logprobs": [],
                })

            async def _emit_tool_started(payload: Dict[str, Any]) -> str:
                """Emit response.output_item.added for a function_call.

                Returns the call_id so the matching completion event can
                reference it.  Prefer the real ``tool_call_id`` from the
                agent when available; fall back to a generated call id for
                safety in tests or older code paths.
                """
                nonlocal output_index, call_counter
                call_counter += 1
                call_id = payload.get("tool_call_id") or f"call_{response_id[5:]}_{call_counter}"
                args = payload.get("arguments", {})
                if isinstance(args, dict):
                    arguments_str = json.dumps(args)
                else:
                    arguments_str = str(args)
                item = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "name": payload.get("name", ""),
                    "call_id": call_id,
                    "arguments": arguments_str,
                }
                idx = output_index
                output_index += 1
                pending_tool_calls.append({
                    "call_id": call_id,
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "item_id": item["id"],
                    "output_index": idx,
                })
                emitted_items.append({
                    "type": "function_call",
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "call_id": call_id,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": item,
                })
                return call_id

            async def _emit_tool_completed(payload: Dict[str, Any]) -> None:
                """Emit response.output_item.done (function_call) followed
                by response.output_item.added (function_call_output)."""
                nonlocal output_index
                call_id = payload.get("tool_call_id")
                result = payload.get("result", "")
                pending = None
                if call_id:
                    for i, p in enumerate(pending_tool_calls):
                        if p["call_id"] == call_id:
                            pending = pending_tool_calls.pop(i)
                            break
                if pending is None:
                    # Completion without a matching start — skip to avoid
                    # emitting orphaned done events.
                    return

                # function_call done
                done_item = {
                    "id": pending["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "name": pending["name"],
                    "call_id": pending["call_id"],
                    "arguments": pending["arguments"],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": pending["output_index"],
                    "item": done_item,
                })

                # function_call_output added (result)
                result_str = result if isinstance(result, str) else json.dumps(result)
                output_parts = [{"type": "input_text", "text": result_str}]
                output_item = {
                    "id": f"fco_{uuid.uuid4().hex[:24]}",
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                    "status": "completed",
                }
                idx = output_index
                output_index += 1
                emitted_items.append({
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": output_item,
                })
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": idx,
                    "item": output_item,
                })

            # Main drain loop — thread-safe queue fed by agent callbacks.
            async def _dispatch(it) -> None:
                """Route a queue item to the correct SSE emitter.

                Plain strings are text deltas — they are batched (50ms)
                to reduce Open WebUI re-render storms.  Tagged tuples
                with ``__tool_started__`` / ``__tool_completed__``
                prefixes are tool lifecycle events and flush the buffer
                before emitting.
                """
                nonlocal _batch_timer
                if isinstance(it, tuple) and len(it) == 2 and isinstance(it[0], str):
                    tag, payload = it
                    # Flush batched text before tool events
                    if _batch_buf:
                        await _flush_batch()
                    if tag == "__tool_started__":
                        await _emit_tool_started(payload)
                    elif tag == "__tool_completed__":
                        await _emit_tool_completed(payload)
                    elif tag == "__clarify_request__":
                        await _write_event(
                            "response.hermes.clarify.request",
                            {
                                "type": "response.hermes.clarify.request",
                                **payload,
                            },
                        )
                    elif tag == "__approval_request__":
                        await _write_event(
                            "response.hermes.approval.request",
                            {
                                "type": "response.hermes.approval.request",
                                **payload,
                            },
                        )
                    elif tag == "__approval_responded__":
                        await _write_event(
                            "response.hermes.approval.responded",
                            {
                                "type": "response.hermes.approval.responded",
                                **payload,
                            },
                        )
                elif isinstance(it, str):
                    # Batch text deltas — append to buffer, flush on timer
                    _batch_buf.append(it)
                    if _batch_timer is None:
                        _batch_timer = asyncio.create_task(_batch_flush_after(0.05))
                # Other types are silently dropped.

            # ── Batching state ──
            _batch_buf: List[str] = []
            _batch_timer: Optional[asyncio.Task] = None
            _batch_lock = asyncio.Lock()

            async def _batch_flush_after(delay: float) -> None:
                """Wait delay seconds, then flush accumulated text deltas."""
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                # Clear timer reference BEFORE flush so new deltas
                # can start a fresh timer while we emit
                nonlocal _batch_buf, _batch_timer
                _batch_timer = None
                await _flush_batch()

            async def _flush_batch() -> None:
                """Emit a single SSE delta for all accumulated text."""
                nonlocal _batch_buf
                async with _batch_lock:
                    if _batch_buf:
                        combined = "".join(_batch_buf)
                        _batch_buf = []
                        await _emit_text_delta(combined)

            loop = asyncio.get_running_loop()
            while True:
                try:
                    item = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain remaining
                        while True:
                            try:
                                item = stream_q.get_nowait()
                                if item is None:
                                    break
                                await _dispatch(item)
                                last_activity = time.monotonic()
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if item is None:  # EOS sentinel
                    # Cancel pending timer and flush remaining batched text
                    if _batch_timer and not _batch_timer.done():
                        _batch_timer.cancel()
                        _batch_timer = None
                    if _batch_buf:
                        await _flush_batch()
                    break

                await _dispatch(item)
                last_activity = time.monotonic()

            # Flush any final batched text before processing result
            if _batch_buf:
                await _flush_batch()

            # Pick up agent result + usage from the completed task.
            result: Any = None
            effective_session_id = session_id
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
                outcome_input = result
                result_mapping = result if isinstance(result, Mapping) else {}
                effective_session_id = _effective_internal_api_session_id(
                    result_mapping,
                    fallback=session_id,
                    source="responses_stream_result",
                )
                # If the agent produced a final_response but no text
                # deltas were streamed (e.g. some providers only emit
                # the full response at the end), emit a single fallback
                # delta so Responses clients still receive a live text part.
                agent_final = result_mapping.get("final_response", "") or ""
                if agent_final and not final_text_parts:
                    await _emit_text_delta(agent_final)
                if agent_final and not final_response_text:
                    final_response_text = agent_final
                if result_mapping.get("error"):
                    agent_error = _redact_api_error_text(result_mapping["error"])
            except _InvalidInternalAPISessionID:
                logger.error(
                    "Agent returned an invalid internal session ID for "
                    "Responses stream %s",
                    response_id,
                )
                agent_error = "Internal session continuity state is invalid."
                outcome_input = {
                    "completed": False,
                    "failed": True,
                    "turn_exit_reason": "invalid_internal_session_id",
                    "error": agent_error,
                }
                result_mapping = {}
            except Exception as e:  # noqa: BLE001
                logger.error("Error running agent for streaming responses: %s", e, exc_info=True)
                agent_error = _redact_api_error_text(e)
                outcome_input = {
                    "completed": False,
                    "failed": True,
                    "turn_exit_reason": "api_run_exception",
                    "error": agent_error,
                }
                result_mapping = {}
            if not self._api_cleanup_allows_terminal(cleanup_ref):
                _persist_cleanup_pending("agent_task_finished_without_cleanup_receipt")
                logger.error(
                    "Responses task %s finished without exact cleanup; "
                    "terminal event withheld",
                    response_id,
                )
                return response
            outcome = _session_stream_outcome(outcome_input)

            # Close the message item if it was opened
            final_response_text = "".join(final_text_parts) or final_response_text
            if message_opened:
                await _write_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "text": final_response_text,
                    "logprobs": [],
                })
                msg_done_item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": (
                        "completed" if outcome["completed"] else "incomplete"
                    ),
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": final_response_text}
                    ],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": message_output_index,
                    "item": msg_done_item,
                })

            # Always append a final message item in the completed
            # response envelope so clients that only parse the terminal
            # payload still see the assistant text.  This mirrors the
            # shape produced by _extract_output_items in the batch path.
            final_items: List[Dict[str, Any]] = list(emitted_items)

            # Trim large content from tool call arguments to keep the
            # response.completed event under ~100KB.  Clients already
            # received full details via incremental events.
            for _item in final_items:
                if _item.get("type") == "function_call":
                    try:
                        _args = json.loads(_item.get("arguments", "{}")) if isinstance(_item.get("arguments"), str) else _item.get("arguments", {})
                        if isinstance(_args, dict):
                            for _k in ("content", "query", "pattern", "old_string", "new_string"):
                                if isinstance(_args.get(_k), str) and len(_args[_k]) > 500:
                                    _args[_k] = "[" + str(len(_args[_k])) + " chars — truncated for response.completed]"
                            _item["arguments"] = json.dumps(_args)
                    except Exception:
                        pass
                elif _item.get("type") == "function_call_output":
                    _output = _item.get("output", [])
                    if isinstance(_output, list) and _output:
                        _first = _output[0]
                        if isinstance(_first, dict) and _first.get("type") == "input_text":
                            _text = _first.get("text", "")
                            if len(_text) > 1000:
                                _first["text"] = _text[:500] + "...[" + str(len(_text) - 500) + " more chars]"
                                _item["output"] = [_first]

            final_items.append({
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": final_response_text or (_redact_api_error_text(agent_error) if agent_error else "")}
                ],
            })

            outcome_fields = {
                "status": outcome["status"],
                "completed": outcome["completed"],
                "partial": outcome["partial"],
                "interrupted": outcome["interrupted"],
                "failed": outcome["failed"],
                "incomplete": outcome["incomplete"],
                "turn_exit_reason": outcome["turn_exit_reason"],
            }
            if outcome["failed"]:
                failed_env = _envelope("failed")
                failed_env["output"] = final_items
                failed_env["error"] = {
                    "message": _redact_api_error_text(
                        agent_error or "Agent run failed."
                    ),
                    "type": "server_error",
                }
                failed_env["hermes"] = outcome_fields
                if (
                    outcome["turn_exit_reason"]
                    == "invalid_internal_session_id"
                ):
                    failed_env["error"][
                        "code"
                    ] = "invalid_internal_session_id"
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                _failed_history = list(conversation_history)
                _failed_history.append({"role": "user", "content": user_message})
                if final_response_text or agent_error:
                    _failed_history.append({
                        "role": "assistant",
                        "content": final_response_text or _redact_api_error_text(agent_error),
                    })
                _persist_response_snapshot(
                    failed_env,
                    conversation_history_snapshot=_failed_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            elif outcome["incomplete"]:
                incomplete_env = _envelope("incomplete")
                incomplete_env["output"] = final_items
                incomplete_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                incomplete_env["hermes"] = outcome_fields
                if agent_error:
                    incomplete_env["error"] = {
                        "message": _redact_api_error_text(agent_error),
                        "type": "agent_incomplete",
                    }
                incomplete_history = self._build_response_conversation_history(
                    conversation_history,
                    user_message,
                    dict(result_mapping),
                    final_response_text,
                )
                _persist_response_snapshot(
                    incomplete_env,
                    conversation_history_snapshot=incomplete_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.incomplete", {
                    "type": "response.incomplete",
                    "response": incomplete_env,
                })
            else:
                completed_env = _envelope("completed")
                completed_env["output"] = final_items
                completed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                full_history = self._build_response_conversation_history(
                    conversation_history,
                    user_message,
                    dict(result_mapping),
                    final_response_text,
                )
                # Compression-aware transcript substitution happens inside
                # _build_response_conversation_history (result["_compressed"]);
                # here we only propagate a compression-rotated session_id so
                # previous_response_id chaining resumes the child session.
                _persist_response_snapshot(
                    completed_env,
                    conversation_history_snapshot=full_history,
                    session_id_snapshot=effective_session_id,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.completed", {
                    "type": "response.completed",
                    "response": completed_env,
                })

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Reap the abandoned turn's process-baseline diff while its
            # ownership markers are still available.  Do not cancel the
            # executor wrapper: exact capability cleanup remains shielded.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                _reap_disconnected_agent_processes(agent)
            cleanup_confirmed = await self._interrupt_and_await_api_task(
                agent_task,
                agent_ref,
                cleanup_ref,
                reason="SSE client disconnected",
            )
            if cleanup_confirmed:
                _persist_incomplete_if_needed()
            else:
                _persist_cleanup_pending("sse_client_disconnected")
                _track_aborted_stream_finalizer("sse_client_disconnected")
            logger.info("SSE client disconnected; interrupted agent task %s", response_id)
        except asyncio.CancelledError:
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                # Same abandonment as a client disconnect: the run will never
                # be resumed, so reap the background processes it created
                # (#76115). Epoch-gated; no-op when the turn already
                # finished and cleared its markers.
                _reap_disconnected_agent_processes(
                    agent, source="api_server_sse_cancelled"
                )
            cleanup_confirmed = await self._interrupt_and_await_api_task(
                agent_task,
                agent_ref,
                cleanup_ref,
                reason="SSE task cancelled",
            )
            if cleanup_confirmed:
                _persist_incomplete_if_needed()
            else:
                _persist_cleanup_pending("sse_task_cancelled")
                _track_aborted_stream_finalizer("sse_task_cancelled")
            logger.info("SSE task cancelled; cleanup tracked for %s", response_id)
            raise
        except Exception as _exc:
            # Agent crashed with an unhandled error (e.g. model API error like
            # BadRequestError, AuthenticationError).  Emit a response.failed
            # event and properly terminate the SSE stream so the client doesn't
            # get a TransferEncodingError from incomplete chunked encoding.
            import traceback as _tb
            agent_error = _redact_api_error_text(_tb.format_exc())
            cleanup_confirmed = await self._interrupt_and_await_api_task(
                agent_task,
                agent_ref,
                cleanup_ref,
                reason="Responses SSE writer failed",
            )
            if cleanup_confirmed:
                failed_env = _persist_writer_failed_if_needed(str(_exc))
                try:
                    await _write_event("response.failed", {
                        "type": "response.failed",
                        "response": failed_env,
                    })
                except Exception:
                    pass
            else:
                _persist_cleanup_pending("sse_writer_exception")
                _track_aborted_stream_finalizer(
                    "sse_writer_exception",
                    _redact_api_error_text(_exc, limit=500),
                )
            logger.error("Agent crashed mid-stream for %s: %s", response_id, str(agent_error)[:300])

        return response

    @_admit_api_agent_request
    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        # Bound total in-flight agent runs (configurable; #7483).
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        provided_session_id, provided_session_err = (
            self._parse_api_control_session_id(request, required=False)
        )
        if provided_session_err is not None:
            return provided_session_err
        if provided_session_id and not self._api_auth_configured():
            return web.json_response(
                _openai_error(
                    "Responses session continuity requires API key authentication",
                    code="session_auth_required",
                ),
                status=403,
            )

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = _coerce_request_bool(body.get("store"), default=True)

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        response_store = self._response_store_for_request()

        # Resolve conversation name to latest response_id
        if conversation:
            previous_response_id = response_store.get_conversation(conversation)
            # No error if conversation doesn't exist yet — it's a new conversation

        # Normalize input to message list
        input_messages: List[Dict[str, Any]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for idx, item in enumerate(raw_input):
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    try:
                        content = _normalize_multimodal_content(item.get("content", ""))
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"input[{idx}].content")
                    input_messages.append({"role": role, "content": content})
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        # Accept explicit conversation_history from the request body.
        # This lets stateless clients supply their own history instead of
        # relying on server-side response chaining via previous_response_id.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, Any]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                try:
                    entry_content = _normalize_multimodal_content(entry["content"])
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                conversation_history.append({"role": str(entry["role"]), "content": entry_content})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = response_store.get(previous_response_id)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            conversation_history = list(stored.get("conversation_history", []))
            if "session_id" in stored:
                try:
                    stored_session_id = _validate_internal_api_session_id(
                        stored["session_id"],
                        source="responses_previous_response",
                    )
                except _InvalidInternalAPISessionID:
                    return _invalid_internal_session_id_response()
            # If no instructions provided, carry forward from previous
            if instructions is None:
                instructions = stored.get("instructions")

        if (
            provided_session_id
            and stored_session_id
            and provided_session_id != stored_session_id
        ):
            return web.json_response(
                _openai_error(
                    "X-Hermes-Session-Id does not match previous_response_id",
                    code="response_session_mismatch",
                ),
                status=409,
            )
        if (
            provided_session_id
            and not conversation_history
            and not previous_response_id
        ):
            try:
                db = await self._ensure_session_db_async()
                if db is not None:
                    conversation_history = await self._offload_session_db(
                        db.get_messages_as_conversation,
                        provided_session_id,
                    )
            except Exception:
                logger.debug(
                    "Failed to load Responses API session history",
                    exc_info=True,
                )

        # Append new input messages to history (all but the last become history)
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        # Last input message is the user_message
        user_message: Any = input_messages[-1].get("content", "") if input_messages else ""
        if not _content_has_visible_payload(user_message):
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # Truncation support
        if body.get("truncation") == "auto":
            conversation_history = _auto_truncate_response_history(conversation_history)

        # Reuse session from previous_response_id chain so the dashboard
        # groups the entire conversation under one session entry.
        # Preserve creation-request idempotency when no session existed before
        # this call: the execution session itself is newly generated on every
        # HTTP retry, so it cannot be the cache namespace in that case.
        idempotency_session_scope = (
            provided_session_id
            or stored_session_id
            or (f"conversation:{conversation}" if conversation else "new-response")
        )
        session_id = provided_session_id or stored_session_id or str(uuid.uuid4())

        stream = _coerce_request_bool(body.get("stream"), default=False)
        route = self._resolve_route(body.get("model"))
        try:
            agent_overrides = _request_agent_overrides(
                body,
                virtual_model=self._active_model_name(),
                allow_bare_model=self._direct_model_requests_enabled(),
            )
        except ValueError as exc:
            return _invalid_runtime_request_response(exc)
        selection_error = self._request_route_conflict_error(
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            requested_model=agent_overrides.get("requested_model"),
            requested_provider=agent_overrides.get("requested_provider"),
            route=route,
        )
        if selection_error:
            return web.json_response(_openai_error(selection_error), status=400)
        if stream:
            reservation = self._reserve_agent_run()
            if reservation is None:
                return self._concurrency_limit_response()
            # Streaming branch — emit OpenAI Responses SSE events as the
            # agent runs so frontends can render text deltas and tool
            # calls in real time.  See _write_sse_responses for details.
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # None from the agent is a CLI box-close signal, not EOS.
                # Forwarding would kill the SSE stream prematurely; the
                # SSE writer detects completion via agent_task.done().
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """Queue non-start tool progress events if needed in future.

                The structured Responses stream uses ``tool_start_callback``
                and ``tool_complete_callback`` for exact call-id correlation,
                so progress events are currently ignored here.
                """
                return

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Queue a started tool for live function_call streaming."""
                _stream_q.put(("__tool_started__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Queue a completed tool result for live function_call_output streaming."""
                _stream_q.put(("__tool_completed__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                    "result": function_result,
                }))

            def _on_clarify(payload: Dict[str, Any]) -> None:
                _stream_q.put(("__clarify_request__", payload))

            def _on_approval(name: str, payload: Dict[str, Any]) -> None:
                tag = (
                    "__approval_request__"
                    if name == "approval.request"
                    else "__approval_responded__"
                )
                _stream_q.put((tag, payload))

            agent_ref = [None]
            cleanup_ref = [None]
            self._publish_api_authority_not_created(cleanup_ref, None)
            try:
                agent_task = asyncio.ensure_future(
                    self._run_agent_from_reservation(
                        reservation,
                        user_message=user_message,
                        conversation_history=conversation_history,
                        ephemeral_system_prompt=instructions,
                        session_id=session_id,
                        stream_delta_callback=_on_delta,
                        tool_progress_callback=_on_tool_progress,
                        tool_start_callback=_on_tool_start,
                        tool_complete_callback=_on_tool_complete,
                        agent_ref=agent_ref,
                        cleanup_ref=cleanup_ref,
                        gateway_session_key=gateway_session_key,
                        **agent_overrides,
                        route=route,
                        clarify_notify_callback=_on_clarify,
                        approval_event_callback=_on_approval,
                    )
                )
            except Exception:
                reservation.release()
                raise
            agent_task.add_done_callback(lambda _task: reservation.release())
            # Ensure SSE drain loops can terminate without relying on polling
            # agent_task.done(), which can race with queue timeout checks.
            agent_task.add_done_callback(lambda _fut: _stream_q.put(None))

            response_id = f"resp_{uuid.uuid4().hex[:28]}"
            model_name = body.get("model", self._active_model_name())
            created_at = int(time.time())

            return await self._write_sse_responses(
                request=request,
                response_id=response_id,
                model=model_name,
                created_at=created_at,
                stream_q=_stream_q,
                agent_task=agent_task,
                agent_ref=agent_ref,
                conversation_history=conversation_history,
                user_message=user_message,
                instructions=instructions,
                conversation=conversation,
                store=store,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                cleanup_ref=cleanup_ref,
                response_store=response_store,
            )

        reservation = self._reserve_agent_run()
        if reservation is None:
            return self._concurrency_limit_response()

        async def _compute_response():
            computed_result, computed_usage = await self._run_agent_from_reservation(
                reservation,
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                **agent_overrides,
                route=route,
            )
            # Keep corrupt result continuity state out of the idempotency cache.
            _effective_internal_api_session_id(
                computed_result,
                fallback=session_id,
                source="responses_result_before_cache",
            )
            return computed_result, computed_usage

        idempotency_key = request.headers.get("Idempotency-Key")
        try:
            if idempotency_key:
                cache_key = _scoped_idempotency_cache_key(
                    idempotency_key,
                    adapter_scope=getattr(
                        self,
                        "_idempotency_adapter_scope",
                        f"legacy-adapter:{id(self)}",
                    ),
                    request_scope=(
                        _api_request_authority.get()
                        or self._api_request_scope("request")
                    ),
                    session_id=idempotency_session_scope,
                    gateway_session_key=gateway_session_key,
                )
                fp = _make_request_fingerprint(
                    body,
                    keys=[
                        "input",
                        "instructions",
                        "previous_response_id",
                        "conversation",
                        "model",
                        "provider",
                        "model_options",
                        "tools",
                        "conversation_history",
                        "truncation",
                        "store",
                    ],
                )
                cache = getattr(self, "_idempotency_cache", _idem_cache)
                result, usage = await cache.get_or_set(
                    cache_key,
                    fp,
                    _compute_response,
                )
            else:
                result, usage = await _compute_response()
        except _InvalidInternalAPISessionID:
            return _invalid_internal_session_id_response()
        except Exception as e:
            logger.error("Error running agent for responses: %s", e, exc_info=True)
            return web.json_response(
                _openai_error(f"Internal server error: {e}", err_type="server_error"),
                status=500,
            )
        finally:
            reservation.release()

        outcome = _session_stream_outcome(result)
        result_mapping = dict(result) if isinstance(result, Mapping) else {}
        final_response = _resolve_media_to_data_urls(
            result_mapping.get("final_response", "") or ""
        )
        if not final_response and outcome["completed"]:
            final_response = _redact_api_error_text(
                result_mapping.get("error", "(No response generated)")
            )

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # Build the full conversation history for storage
        # (includes tool calls from the agent run)
        full_history = self._build_response_conversation_history(
            conversation_history,
            user_message,
            result_mapping,
            final_response,
        )

        # Persist the effective session ID surfaced by _run_agent so that
        # compression-triggered session rotations propagate to the stored
        # response and the X-Hermes-Session-Id header.  Without this,
        # previous_response_id chaining keeps resuming the pre-rotation
        # session and re-triggers compression on every subsequent request.
        try:
            _effective_session_id = _effective_internal_api_session_id(
                result_mapping,
                fallback=session_id,
                source="responses_result",
            )
        except _InvalidInternalAPISessionID:
            return _invalid_internal_session_id_response()

        # Build output items from the current turn only.  AIAgent returns a
        # full transcript in result["messages"], while older/mocked paths may
        # return only the current turn suffix.
        output_start_index = self._response_messages_turn_start_index(
            conversation_history,
            user_message,
            result_mapping,
        )
        output_items = self._extract_output_items(
            result_mapping,
            start_index=output_start_index,
        )

        response_status = (
            "completed"
            if outcome["completed"]
            else "failed"
            if outcome["failed"]
            else "incomplete"
        )

        response_data = {
            "id": response_id,
            "object": "response",
            "status": response_status,
            "created_at": created_at,
            "model": body.get("model", self._active_model_name()),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        if outcome["incomplete"]:
            response_data["hermes"] = {
                "status": outcome["status"],
                "completed": outcome["completed"],
                "partial": outcome["partial"],
                "interrupted": outcome["interrupted"],
                "failed": outcome["failed"],
                "incomplete": outcome["incomplete"],
                "turn_exit_reason": outcome["turn_exit_reason"],
            }
            raw_error = result_mapping.get("error")
            if raw_error:
                response_data["error"] = {
                    "message": _redact_api_error_text(raw_error),
                    "type": (
                        "server_error" if outcome["failed"] else "agent_incomplete"
                    ),
                }

        # Store the complete response object for future chaining / GET retrieval
        if store:
            response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
                "session_id": _effective_session_id,
            })
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response
            if conversation and outcome["completed"]:
                response_store.set_conversation(conversation, response_id)

        response_headers = {"X-Hermes-Session-Id": _effective_session_id}
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key
        if outcome["incomplete"]:
            response_headers["X-Hermes-Completed"] = "false"
            response_headers["X-Hermes-Partial"] = (
                "true" if outcome["partial"] else "false"
            )
        return web.json_response(response_data, headers=response_headers)

    # ------------------------------------------------------------------
    # GET / DELETE response endpoints
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        stored = self._response_store_for_request().get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        deleted = self._response_store_for_request().delete(response_id)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    # ------------------------------------------------------------------
    # Cron jobs API
    # ------------------------------------------------------------------

    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # Allowed fields for update — prevents clients injecting arbitrary keys
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    @staticmethod
    def _check_jobs_available() -> Optional["web.Response"]:
        """Return error response if cron module isn't available."""
        if not _CRON_AVAILABLE:
            return web.json_response(
                {"error": "Cron module not available"}, status=501,
            )
        return None

    def _check_job_id(self, request: "web.Request") -> tuple:
        """Validate and extract job_id. Returns (job_id, error_response)."""
        job_id = request.match_info["job_id"]
        if not self._JOB_ID_RE.fullmatch(job_id):
            logger.warning(
                "Cron jobs API rejected invalid job_id %r: %s",
                job_id,
                self._request_audit_log_suffix(request),
            )
            return job_id, web.json_response(
                {"error": "Invalid job ID format"}, status=400,
            )
        return job_id, None

    async def _handle_list_jobs(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs — list all cron jobs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            include_disabled = request.query.get("include_disabled", "").lower() in {"true", "1"}
            jobs = _cron_list(include_disabled=include_disabled)
            return web.json_response({"jobs": jobs})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_create_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs — create a new cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            prompt = body.get("prompt", "")
            deliver = body.get("deliver", "local")
            skills = body.get("skills")
            repeat = body.get("repeat")

            if not name:
                return web.json_response({"error": "Name is required"}, status=400)
            if len(name) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if not schedule:
                return web.json_response({"error": "Schedule is required"}, status=400)
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
                return web.json_response({"error": "Repeat must be a positive integer"}, status=400)

            kwargs = {
                "prompt": prompt,
                "schedule": schedule,
                "name": name,
                "deliver": deliver,
                "origin": self._cron_origin_from_request(request),
            }
            # These are ordinary create-time routing axes supported by the
            # cron store. In sealed production, create_job mechanically fills
            # omitted values with the attested route and rejects explicit
            # alternatives; forwarding supplied values prevents a caller's
            # alternate route from being silently ignored.
            if "provider" in body:
                kwargs["provider"] = body["provider"]
            if "model" in body:
                kwargs["model"] = body["model"]
            if skills:
                kwargs["skills"] = skills
            if repeat is not None:
                kwargs["repeat"] = repeat

            job = _cron_create(**kwargs)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_get_job(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs/{job_id} — get a single cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_update_job(self, request: "web.Request") -> "web.Response":
        """PATCH /api/jobs/{job_id} — update a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            body = await request.json()
            # Whitelist allowed fields to prevent arbitrary key injection
            sanitized = {k: v for k, v in body.items() if k in self._UPDATE_ALLOWED_FIELDS}
            if not sanitized:
                return web.json_response({"error": "No valid fields to update"}, status=400)
            # Validate lengths if present
            if "name" in sanitized and len(sanitized["name"]) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if "prompt" in sanitized and len(sanitized["prompt"]) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            job = _cron_update(job_id, sanitized)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_delete_job(self, request: "web.Request") -> "web.Response":
        """DELETE /api/jobs/{job_id} — delete a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            success = _cron_remove(job_id)
            if not success:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_pause_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/pause — pause a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_pause(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_resume_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/resume — resume a paused cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_resume(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_run_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/run — trigger immediate execution."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        draining = self._draining_response()
        if draining is not None:
            return draining
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_trigger(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_cron_fire(self, request: "web.Request") -> "web.Response":
        """POST /api/cron/fire — Chronos managed-cron fire webhook (NAS → agent).

        Authenticated by a NAS-minted JWT (verified via the pluggable
        fire-verifier), NOT API_SERVER_KEY — NAS holds no API server key, and
        this is the only inbound that can trigger remote job execution, so it
        gets its own purpose-scoped token check.

        Returns 202 + runs the job in the background so a long agent turn never
        trips NAS's HTTP timeout. The store CAS claim inside fire_due guards
        against double-fire on a NAS/scheduler retry.
        """
        from hermes_cli.config import cfg_get, load_config
        from plugins.cron_providers.chronos.verify import get_fire_verifier

        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""

        cfg = load_config()
        verifier = get_fire_verifier()
        verify_kwargs = dict(
            token=token,
            expected_audience=cfg_get(cfg, "cron", "chronos", "expected_audience", default=""),
            jwks_or_key=cfg_get(cfg, "cron", "chronos", "nas_jwks_url", default="") or None,
            issuer=cfg_get(cfg, "cron", "chronos", "portal_url", default="") or None,
        )
        try:
            if asyncio.iscoroutinefunction(verifier):
                claims = await verifier(**verify_kwargs)
            else:
                # The verifier resolves the NAS signing key from a JWKS URL,
                # which is a synchronous HTTP GET on a cache miss (cold client
                # or a rotated kid) — keep that blocking I/O off the event loop
                # so a slow or rate-limited portal can't stall every other
                # adapter sharing this loop. Same hardening the platform HTTP
                # event verifier already got.
                claims = await asyncio.to_thread(verifier, **verify_kwargs)
            # ``asyncio.iscoroutinefunction`` does not recognize callable
            # objects whose ``__call__`` is async.  The worker-thread branch
            # therefore returns their coroutine object; await any awaitable
            # result before deciding whether authentication succeeded.
            if inspect.isawaitable(claims):
                claims = await claims
        except Exception:
            # Fail closed: a crashing verifier must never admit a fire — this
            # is the only inbound that can trigger remote job execution.
            logger.exception("cron fire: verifier crashed; rejecting token")
            claims = None
        if not isinstance(claims, Mapping):
            logger.warning(
                "cron fire: rejected invalid token: %s",
                self._request_audit_log_suffix(request),
            )
            return web.json_response({"error": "invalid fire token"}, status=401)
        draining = self._draining_response()
        if draining is not None:
            return draining

        with _reserve_pending_api_work(self) as reservation:
            try:
                body = await request.json()
            except Exception:
                body = {}
            job_id = (body or {}).get("job_id")
            if not job_id:
                return web.json_response({"error": "missing job_id"}, status=400)

            from cron.scheduler_provider import resolve_cron_scheduler
            provider = resolve_cron_scheduler()

            loop = asyncio.get_running_loop()
            # Fire in the background (202 immediately). fire_due claims via the
            # store CAS, so a retry while this is in flight is de-duped.
            task = asyncio.create_task(
                asyncio.to_thread(provider.fire_due, job_id, adapters=None, loop=loop)
            )
            reservation["detached"] = True
            task.add_done_callback(
                lambda _task: _release_pending_api_work(self, reservation)
            )
            try:
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except (TypeError, AttributeError):
                pass

            return web.json_response({"status": "accepted", "job_id": job_id}, status=202)


    # ------------------------------------------------------------------
    # Output extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_response_conversation_history(
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
        final_response: Any,
    ) -> List[Dict[str, Any]]:
        """Build the stored Responses transcript without duplicating history.

        When context compression occurs during a turn the agent returns a
        compressed full transcript in ``result["messages"]`` (starting with a
        summary) and sets ``result["_compressed"] = True``.  Because the
        compressed transcript does not share the input ``conversation_history``
        prefix, the normal turn-start detection fails and old code would
        concatenate the uncompressed history on front, bloating the stored
        context and re-triggering compression on every subsequent request.
        """
        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        agent_messages = result.get("messages") if isinstance(result, dict) else None

        if isinstance(agent_messages, list) and agent_messages:
            turn_start = APIServerAdapter._response_messages_turn_start_index(
                conversation_history,
                user_message,
                result,
            )
            if turn_start:
                return list(agent_messages)

            # turn_start == 0: agent_messages does not start with prior.
            # This can happen because compression rewrote the transcript
            # (summary prefix replaces original history), OR because
            # agent_messages only carries the current turn without prior.
            # The ``_compressed`` flag (set by _run_agent after compaction)
            # distinguishes — skip the concatenation and use the compressed
            # transcript directly.
            if result.get("_compressed"):
                return list(agent_messages)

            full_history = prior
            full_history.append(current_user)
            full_history.extend(agent_messages)
            return full_history

        full_history = prior
        full_history.append(current_user)
        full_history.append({"role": "assistant", "content": final_response})
        return full_history

    @staticmethod
    def _response_messages_turn_start_index(
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> int:
        """Detect transcript-shaped result["messages"] and return turn start."""
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return 0

        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        expected_prefix = prior + [current_user]
        if agent_messages[:len(expected_prefix)] == expected_prefix:
            return len(expected_prefix)
        if prior and agent_messages[:len(prior)] == prior:
            return len(prior)
        return 0

    @classmethod
    def _turn_transcript_messages(
        cls,
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Return this turn's assistant/tool messages in client-safe shape.

        The streaming SSE contract delivers all assistant text as
        ``assistant.delta`` events under one ``message_id`` interleaved with
        ``tool.*`` events, and a single ``assistant.completed`` carrying only
        the final reply.  A client that accumulates deltas into one buffer
        cannot reconstruct *intermediate* assistant text segments that preceded
        tool calls — so when the page is re-opened mid/post-stream those
        segments appear lost, even though state.db persisted them correctly.

        Emitting the authoritative per-turn transcript on ``run.completed`` lets
        any SSE consumer reconcile its live view against ground truth without a
        separate ``GET /messages`` round-trip.  Purely additive: clients that
        ignore the field are unaffected.  Refs #34703.
        """
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return []
        start = cls._response_messages_turn_start_index(
            conversation_history, user_message, result
        )
        turn = agent_messages[start:]
        out: List[Dict[str, Any]] = []
        for msg in turn:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in {"assistant", "tool"}:
                continue
            out.append(cls._message_response(msg))
        return out

    @staticmethod
    def _extract_output_items(result: Dict[str, Any], start_index: int = 0) -> List[Dict[str, Any]]:
        """
        Build the output item array from the agent's messages.

        Walks *result["messages"]* starting at *start_index* and emits:
        - ``function_call`` items for each tool_call on assistant messages
        - ``function_call_output`` items for each tool-role message
        - a final ``message`` item with the assistant's text reply
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])
        if start_index > 0:
            messages = messages[start_index:]

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        # Final assistant message
        final = result.get("final_response", "")
        if not final:
            final = _redact_api_error_text(result.get("error", "(No response generated)"))

        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final,
                }
            ],
        })
        return items

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    def _active_agent_run_count(self) -> int:
        """Return authority/task ownership count, independent of subscribers."""
        active_structured_runs = sum(
            1 for task in self._active_run_tasks.values() if not task.done()
        )
        return (
            self._inflight_agent_runs
            + active_structured_runs
            + len(self._api_cleanup_handles)
            + self._agent_run_reservations
        )

    def _concurrency_limit_response(self) -> "web.Response":
        limit = self._max_concurrent_runs
        return web.json_response(
            _openai_error(
                f"Too many concurrent runs (max {limit})",
                err_type="rate_limit_error",
                code="rate_limit_exceeded",
            ),
            status=429,
            headers={"Retry-After": "1"},
        )

    def _concurrency_limited_response(self) -> Optional["web.Response"]:
        """Return a 429 response if the concurrent-run cap is reached, else None.

        The cap bounds total in-flight agent activity across every
        agent-serving endpoint. It includes requests admitted before handler
        bookkeeping, non-streaming turns, structured-run task ownership,
        pending authority cleanup, and exact reservation handoffs. Optional
        SSE subscriber queues are transport state and never define liveness.
        A configured value of 0 disables the cap.
        """
        limit = self._max_concurrent_runs
        if limit <= 0:
            return None
        inflight = self.active_agent_work_count()
        # The current request owns one reservation until it hands off to
        # _run_agent() or /v1/runs task registration. It must not consume its
        # own last available slot; other admitted requests remain counted.
        reservation = _api_agent_request_reservation.get()
        if reservation and reservation["active"]:
            inflight -= 1
        if inflight >= limit:
            return self._concurrency_limit_response()
        return None

    def _reserve_agent_run(self) -> Optional[_APIServerRunReservation]:
        """Atomically check and reserve one event-loop admission slot."""
        limit = self._max_concurrent_runs
        if limit <= 0:
            return _APIServerRunReservation(self, counted=False)
        inflight = self.active_agent_work_count()
        admitted = _api_agent_request_reservation.get()
        if admitted and admitted["active"]:
            inflight -= 1
        if inflight >= limit:
            return None
        self._agent_run_reservations += 1
        return _APIServerRunReservation(self, counted=True)

    async def _run_agent_from_reservation(
        self,
        reservation: _APIServerRunReservation,
        **kwargs: Any,
    ) -> tuple:
        # No await occurs between releasing the temporary slot and entering
        # _run_agent, whose in-flight owner is installed before its first await.
        reservation.release()
        return await self._run_agent(**kwargs)

    @staticmethod
    def _bind_api_server_session(
        *,
        chat_id: str = "",
        session_key: str = "",
        session_id: str = "",
    ) -> _APIServerSessionBinding:
        """Bind session contextvars for an API-server agent run.

        This is the SINGLE structural chokepoint every API-server agent-entry
        path must use to seed session context — it hardwires
        ``platform="api_server"`` and ``async_delivery=False`` so a new route
        physically cannot reintroduce the silent-no-op bug (#10760) by
        forgetting to mark the channel as non-delivering. There is no
        ``async_delivery`` parameter to get wrong; the stateless HTTP path can
        never wake the agent after the turn ends, on ANY route.

        Returns reset tokens; pass them to ``clear_session_vars`` in a
        ``finally`` block (the binding is request-scoped and must not outlive
        the turn — a session resumed later on a delivering interface, e.g. the
        CLI or a gateway platform, re-binds fresh and is NOT blocked).
        """
        from gateway.session_context import set_session_vars

        # This epoch is gateway-owned, fresh for this one run, and never
        # accepted from an HTTP payload.  Only its digest reaches ContextVar
        # state; the random preimage is discarded immediately and is never
        # logged or persisted.
        capability_epoch_sha256 = hashlib.sha256(secrets.token_bytes(32)).hexdigest()

        return _APIServerSessionBinding(
            set_session_vars(
                platform="api_server",
                chat_id=chat_id,
                session_key=session_key,
                session_id=session_id,
                # The request middleware is the authority for multiplex
                # profile selection.  Carry that trusted label into the
                # detached delegation owner alongside the already scoped,
                # canonical HERMES_HOME so a later wake can prove both refer
                # to the same served profile.
                profile=str(
                    getattr(_api_request_authority.get(), "profile", "")
                    or _api_request_profile.get()
                    or ""
                ),
                capability_epoch_sha256=capability_epoch_sha256,
                async_delivery=False,
                cron_session="",
            ),
            capability_epoch_sha256,
        )

    def _admit_bound_api_server_run(
        self,
        *,
        session_id: str,
        capability_epoch_sha256: str,
    ) -> Mapping[str, Any] | None:
        """Run the optional canary barrier after binding and before the model."""

        callback = self._run_admission_callback
        if callback is None:
            return None
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id.encode("utf-8", errors="strict")) > 256
            or re.fullmatch(r"[0-9a-f]{64}", capability_epoch_sha256 or "")
            is None
        ):
            raise RuntimeError("api_server run admission binding is invalid")
        receipt = callback(session_id, capability_epoch_sha256)
        if not isinstance(receipt, Mapping):
            raise RuntimeError("api_server run admission returned no receipt")
        raw = dict(receipt)
        unsigned = {
            key: value for key, value in raw.items() if key != "receipt_sha256"
        }
        if (
            set(raw)
            != {
                "schema",
                "session_id",
                "capability_epoch_sha256",
                "challenge_sha256",
                "ready_receipt_sha256",
                "commit_receipt_sha256",
                "commit_ack_sha256",
                "finalization_sha256",
                "stage",
                "gateway_commit_acknowledged",
                "model_release_allowed",
                "model_callback_released",
                "receipt_sha256",
            }
            or raw["schema"] != API_RUN_ADMISSION_SCHEMA
            or raw["session_id"] != session_id
            or raw["capability_epoch_sha256"] != capability_epoch_sha256
            or re.fullmatch(r"[0-9a-f]{64}", raw["challenge_sha256"] or "")
            is None
            or any(
                re.fullmatch(r"[0-9a-f]{64}", str(raw[field] or "")) is None
                for field in (
                    "ready_receipt_sha256",
                    "commit_receipt_sha256",
                    "commit_ack_sha256",
                    "finalization_sha256",
                )
            )
            or raw["stage"] != "gateway_commit_acknowledged_pre_model"
            or raw["gateway_commit_acknowledged"] is not True
            or raw["model_release_allowed"] is not True
            or raw["model_callback_released"] is not False
            or raw["receipt_sha256"]
            != hashlib.sha256(
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8", errors="strict")
            ).hexdigest()
        ):
            raise RuntimeError("api_server run admission receipt is invalid")
        released_at = int(time.time() * 1000)
        release_unsigned = {
            "schema": API_MODEL_RELEASE_SCHEMA,
            "session_id": session_id,
            "capability_epoch_sha256": capability_epoch_sha256,
            "challenge_sha256": raw["challenge_sha256"],
            "admission_receipt_sha256": raw["receipt_sha256"],
            "finalization_sha256": raw["finalization_sha256"],
            "stage": "api_model_callback_released",
            "gateway_commit_acknowledged": True,
            "model_release_allowed": True,
            "model_callback_released": True,
            "released_at_unix_ms": released_at,
        }
        return {
            **release_unsigned,
            "receipt_sha256": hashlib.sha256(
                json.dumps(
                    release_unsigned,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8", errors="strict")
            ).hexdigest(),
        }

    @staticmethod
    def _revoke_api_server_run_capabilities(
        session_key: str,
        expected_capability_epoch_sha256: str,
    ) -> Dict[str, Any]:
        """Durably retire the exact bound per-run epoch and verify its receipt."""
        if not session_key:
            raise RuntimeError("api_server cleanup session key is empty")
        if (
            re.fullmatch(
                r"[0-9a-f]{64}", expected_capability_epoch_sha256 or ""
            )
            is None
        ):
            raise RuntimeError("api_server cleanup epoch is invalid")
        from tools.approval import revoke_session_capabilities_durably

        raw_receipt = revoke_session_capabilities_durably(
            session_key,
            reason="api_server_run_finished",
        )
        if not isinstance(raw_receipt, Mapping):
            raise RuntimeError("api_server durable revoke returned no receipt")
        receipt = dict(raw_receipt)

        # A stock/local Hermes runtime with no privileged writer has no
        # Canonical authority to retire.  The approval boundary explicitly
        # returns this shape only when writer enforcement is not required.
        if receipt.get("writer_required") is False:
            if (
                receipt.get("success") is not True
                or receipt.get("scope_revoked") is not False
            ):
                raise RuntimeError("api_server local cleanup receipt is invalid")
            return {
                **receipt,
                "session_key_sha256": hashlib.sha256(
                    session_key.encode()
                ).hexdigest(),
                "capability_epoch_sha256": expected_capability_epoch_sha256,
                "authority_active": False,
                "revocation_event_id": None,
                "inserted": False,
                "deduped": False,
            }

        expected_session_sha256 = hashlib.sha256(session_key.encode()).hexdigest()
        event_id = receipt.get("revocation_event_id")
        inserted = receipt.get("inserted")
        deduped = receipt.get("deduped")
        try:
            parsed_event_id = uuid.UUID(str(event_id))
            canonical_event_id = str(parsed_event_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                "api_server durable revoke event receipt is invalid"
            ) from exc
        if (
            receipt.get("success") is not True
            or receipt.get("session_key_sha256") != expected_session_sha256
            or receipt.get("capability_epoch_sha256")
            != expected_capability_epoch_sha256
            or receipt.get("scope_type") != "session"
            or receipt.get("scope_revoked") is not True
            or receipt.get("authority_active") is not False
            or parsed_event_id.int == 0
            or str(event_id) != canonical_event_id
            or type(inserted) is not bool
            or type(deduped) is not bool
            or inserted is deduped
        ):
            raise RuntimeError(
                "api_server durable revoke did not confirm the exact authority tombstone"
            )
        receipt.setdefault("writer_required", True)
        return receipt

    @staticmethod
    def _clear_api_server_run_local_authority(
        session_key: str,
        capability_epoch_sha256: str,
    ) -> None:
        """Fence every process-local grant from one completed API run."""
        if not session_key:
            return
        if re.fullmatch(r"[0-9a-f]{64}", capability_epoch_sha256) is None:
            raise RuntimeError("api_server run capability epoch is invalid")
        from tools.approval import clear_session_local

        clear_session_local(
            session_key,
            retire_capability_epoch_sha256=capability_epoch_sha256,
        )

    @staticmethod
    def _publish_api_cleanup_state(
        handle: _APIServerCleanupHandle,
        cleanup_ref: Optional[list],
        cleanup_state_callback: Any,
    ) -> Dict[str, Any]:
        state = handle.safe_state()
        if cleanup_ref is not None:
            if cleanup_ref:
                cleanup_ref[0] = state
            else:
                cleanup_ref.append(state)
        if cleanup_state_callback is not None:
            try:
                cleanup_state_callback(dict(state))
            except Exception:
                logger.exception("api_server cleanup state callback failed")
        return state

    @staticmethod
    def _api_cleanup_allows_terminal(cleanup_ref: Optional[list]) -> bool:
        state = cleanup_ref[0] if cleanup_ref else None
        if not isinstance(state, Mapping):
            return False
        if state.get("authority_created") is False:
            return bool(
                state.get("authority_active") is False
                and state.get("local_clear_succeeded") is True
            )
        if (
            state.get("durable_revoke_succeeded") is not True
            or state.get("authority_active") is not False
            or state.get("local_clear_succeeded") is not True
        ):
            return False
        if state.get("writer_required") is False:
            return True
        inserted = state.get("inserted")
        deduped = state.get("deduped")
        event_id = state.get("revocation_event_id")
        try:
            parsed_event_id = uuid.UUID(str(event_id))
            canonical_event_id = str(parsed_event_id)
        except (ValueError, TypeError, AttributeError):
            return False
        return bool(
            parsed_event_id.int != 0
            and str(event_id) == canonical_event_id
            and type(inserted) is bool
            and type(deduped) is bool
            and inserted is not deduped
        )

    @staticmethod
    def _publish_api_authority_not_created(
        cleanup_ref: Optional[list],
        cleanup_state_callback: Any,
    ) -> None:
        state = {
            "status": "authority_not_created",
            "authority_created": False,
            "authority_active": False,
            "durable_revoke_succeeded": False,
            "local_clear_succeeded": True,
            "terminal_safe": True,
        }
        if cleanup_ref is not None:
            if cleanup_ref:
                cleanup_ref[0] = state
            else:
                cleanup_ref.append(state)
        if cleanup_state_callback is not None:
            try:
                cleanup_state_callback(dict(state))
            except Exception:
                logger.exception("api_server no-authority callback failed")

    def _attempt_api_server_cleanup_once(
        self,
        handle: _APIServerCleanupHandle,
        *,
        use_copied_context: bool,
        cleanup_ref: Optional[list],
        cleanup_state_callback: Any,
    ) -> Dict[str, Any]:
        """Attempt one idempotent exact-epoch revoke plus local fence."""

        def _attempt() -> None:
            handle.attempts += 1
            errors: list[str] = []
            if not handle.durable_revoke_succeeded:
                try:
                    handle.receipt = self._revoke_api_server_run_capabilities(
                        handle._session_key,
                        handle.capability_epoch_sha256,
                    )
                    handle.durable_revoke_succeeded = True
                except Exception as exc:
                    errors.append(
                        "durable_revoke_unconfirmed: " + type(exc).__name__
                    )
            if not handle.local_clear_succeeded:
                try:
                    self._clear_api_server_run_local_authority(
                        handle._session_key,
                        handle.capability_epoch_sha256,
                    )
                    handle.local_clear_succeeded = True
                except Exception as exc:
                    errors.append(
                        "local_authority_clear_unconfirmed: "
                        + type(exc).__name__
                    )

            handle.last_error = "; ".join(part for part in errors if part)
            if (
                handle.durable_revoke_succeeded
                and handle.local_clear_succeeded
            ):
                handle.status = "confirmed"
            elif handle.durable_revoke_succeeded:
                handle.status = "cleanup_degraded"
            else:
                handle.status = "cleanup_blocked"

        if use_copied_context:
            trusted_context = handle._trusted_context
            if trusted_context is None or not handle._session_key:
                raise RuntimeError("api_server cleanup retry authority is unavailable")
            # Each attempt receives a fresh copy.  Mutations made while the
            # callback runs cannot erase the immutable base binding needed by
            # a later retry, and the executor thread's surrounding Context is
            # restored automatically by Context.run().
            trusted_context.copy().run(_attempt)
        else:
            _attempt()

        state = self._publish_api_cleanup_state(
            handle,
            cleanup_ref,
            cleanup_state_callback,
        )
        if handle.status == "confirmed":
            self._api_cleanup_handles.pop(handle.cleanup_id, None)
            handle.zeroize_retry_authority()
        else:
            self._api_cleanup_handles[handle.cleanup_id] = handle
        return state

    async def _confirm_api_server_cleanup(
        self,
        handle: _APIServerCleanupHandle,
        *,
        cleanup_ref: Optional[list],
        cleanup_state_callback: Any,
    ) -> Dict[str, Any]:
        """Retry one exact binding mechanically until both fences confirm.

        Every individual attempt and sleep is bounded.  The handle remains in
        ``_api_cleanup_handles`` between attempts, so cancellation of the HTTP
        writer cannot convert uncertain authority into terminal state.
        """
        loop = asyncio.get_running_loop()
        while handle.status != "confirmed":
            exponent = min(max(handle.attempts - 1, 0), 8)
            delay = min(
                API_CLEANUP_RETRY_BASE_SECONDS * (2**exponent),
                API_CLEANUP_RETRY_MAX_SECONDS,
            )
            await asyncio.sleep(delay)
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._attempt_api_server_cleanup_once(
                        handle,
                        use_copied_context=True,
                        cleanup_ref=cleanup_ref,
                        cleanup_state_callback=cleanup_state_callback,
                    ),
                )
            except Exception as exc:
                handle.attempts += 1
                handle.status = "cleanup_blocked"
                handle.last_error = (
                    "cleanup_retry_unconfirmed: " + type(exc).__name__
                )
                self._api_cleanup_handles[handle.cleanup_id] = handle
                self._publish_api_cleanup_state(
                    handle,
                    cleanup_ref,
                    cleanup_state_callback,
                )
        return handle.safe_state()

    def _track_api_background_task(self, task: "asyncio.Task") -> None:
        self._api_cleanup_tasks.add(task)
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._api_cleanup_tasks.discard)

    def _ensure_api_cleanup_retry(
        self,
        handle: _APIServerCleanupHandle,
        *,
        cleanup_ref: Optional[list],
        cleanup_state_callback: Any,
    ) -> "asyncio.Task":
        existing = self._api_cleanup_retry_tasks.get(handle.cleanup_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._confirm_api_server_cleanup(
                handle,
                cleanup_ref=cleanup_ref,
                cleanup_state_callback=cleanup_state_callback,
            )
        )
        self._api_cleanup_retry_tasks[handle.cleanup_id] = task

        def _forget(_task) -> None:
            if self._api_cleanup_retry_tasks.get(handle.cleanup_id) is _task:
                self._api_cleanup_retry_tasks.pop(handle.cleanup_id, None)

        task.add_done_callback(_forget)
        self._track_api_background_task(task)
        return task

    async def _interrupt_and_await_api_task(
        self,
        agent_task: "asyncio.Task",
        agent_ref: Optional[list],
        cleanup_ref: Optional[list],
        *,
        reason: str,
    ) -> bool:
        """Interrupt and wait briefly without cancelling cleanup ownership.

        Returns true only when the task finished and its exact cleanup state is
        confirmed (legacy test tasks with no cleanup reference are treated as
        self-contained once done).  A timed-out task is strongly tracked so it
        can finish execution and cleanup after the HTTP writer exits.
        """
        agent = agent_ref[0] if agent_ref else None
        if agent is not None:
            try:
                request_hard_interrupt(agent, reason)
            except Exception:
                pass
            # interrupt() cannot wake a thread blocked in Event.wait().  Cancel
            # the exact API clarify scope so the worker can unwind and perform
            # its mandatory capability cleanup.
            self._cancel_api_agent_clarifications(agent)
            self._clear_api_approval_scope(
                str(getattr(agent, "session_id", "") or ""),
                approval_session_key=str(
                    getattr(agent, "_api_approval_session_key", "") or ""
                ),
                cancel_core=True,
                request_authority=getattr(
                    agent,
                    "_api_request_authority",
                    None,
                ),
            )

        try:
            await asyncio.wait_for(
                asyncio.shield(agent_task),
                timeout=API_CLEANUP_SHIELD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._track_api_background_task(agent_task)
            return False
        except asyncio.CancelledError:
            if not agent_task.done():
                self._track_api_background_task(agent_task)
                return False
            return bool(
                not agent_task.cancelled()
                and self._api_cleanup_allows_terminal(cleanup_ref)
            )
        except Exception:
            # _run_agent releases execution exceptions only after its cleanup
            # receipt has confirmed.  The state check below remains the gate.
            pass

        if cleanup_ref is None:
            return agent_task.done() and not agent_task.cancelled()
        return self._api_cleanup_allows_terminal(cleanup_ref)

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        agent_ref: Optional[list] = None,
        cleanup_ref: Optional[list] = None,
        cleanup_state_callback=None,
        gateway_session_key: Optional[str] = None,
        requested_model: Optional[str] = None,
        requested_provider: Optional[str] = None,
        model_options: Optional[Dict[str, Any]] = None,
        route: Optional[Dict[str, Any]] = None,
        session_model: Optional[str] = None,
        requested_runtime: Optional[Dict[str, Any]] = None,
        route_source: str = "global",
        confirmed_runtime_lock: bool = False,
        clarify_notify_callback=None,
        approval_event_callback=None,
        runtime_effect: Optional[dict] = None,
        api_execution_context: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """
        Create an agent and run a conversation in a thread executor.

        Returns ``(result_dict, usage_dict)`` where *usage_dict* contains
        ``input_tokens``, ``output_tokens`` and ``total_tokens``.

        *route* is an optional ``model_routes`` entry (resolved from the
        request's ``model`` field) that overrides the global model/provider
        for this specific request.

        If *agent_ref* is a one-element list, the AIAgent instance is stored
        at ``agent_ref[0]`` before ``run_conversation`` begins.  This allows
        callers (e.g. the SSE writer) to call ``agent.interrupt()`` from
        another thread to stop in-progress LLM calls.
        """
        from agent.runtime_effects import normalize_optional_runtime_effect

        runtime_effect = normalize_optional_runtime_effect(runtime_effect)
        from gateway.api_execution_context import (
            normalize_api_execution_context,
        )

        api_execution_context = normalize_api_execution_context(
            api_execution_context
        )
        # Freeze the host-owned tenant authority before any background or
        # executor handoff.  ContextVars are copied as a defence in depth, but
        # all lifecycle keys below receive this exact immutable value.
        request_authority = (
            _api_request_authority.get()
            or self._api_request_scope("request")
        )
        bound_session_key = request_authority.bind(
            "capability-session",
            session_id or gateway_session_key or "",
        ).internal_key
        if cleanup_ref is not None:
            pending_state = {
                "status": "authority_creation_pending",
                "authority_created": None,
                "authority_active": None,
                "durable_revoke_succeeded": False,
                "local_clear_succeeded": False,
                "terminal_safe": False,
            }
            if cleanup_ref:
                cleanup_ref[0] = pending_state
            else:
                cleanup_ref.append(pending_state)
        loop = asyncio.get_running_loop()
        owned_agent_ref: list[Any] = [None]

        def _run():
            from gateway.session_context import clear_session_vars
            from tools.approval import (
                register_gateway_notify,
                reset_current_session_key,
                set_current_session_key,
                unregister_gateway_notify,
            )

            # Cached agents carry mutable callbacks/transcript state.  Serialize
            # exact-session turns from cache lookup through final cache fencing.
            with self._api_agent_run_lock_for(
                session_id,
                request_authority=request_authority,
            ):
                try:
                    # The long-term-memory key may span conversation rotations;
                    # dangerous-action authority must be exact-conversation
                    # scoped instead.
                    approval_token = None
                    approval_notify_registered = False
                    try:
                        tokens = self._bind_api_server_session(
                            chat_id=session_id or "",
                            session_key=bound_session_key,
                            session_id=session_id or "",
                        )
                    except Exception:
                        self._publish_api_authority_not_created(
                            cleanup_ref,
                            cleanup_state_callback,
                        )
                        raise
                    bound_capability_epoch_sha256 = tokens.capability_epoch_sha256
                    cleanup_handle = _APIServerCleanupHandle(
                        bound_session_key,
                        bound_capability_epoch_sha256,
                        contextvars.copy_context(),
                    )
                    self._publish_api_cleanup_state(
                        cleanup_handle,
                        cleanup_ref,
                        cleanup_state_callback,
                    )
                    result: Any = None
                    usage = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    }
                    execution_error: Optional[Exception] = None
                    agent = None
                    try:
                        try:
                            approval_token = set_current_session_key(
                                bound_session_key
                            )
                            model_release_receipt = self._admit_bound_api_server_run(
                                session_id=str(session_id or ""),
                                capability_epoch_sha256=(
                                    bound_capability_epoch_sha256
                                ),
                            )
                            if model_release_receipt is not None:
                                cleanup_handle.model_release_receipt = dict(
                                    model_release_receipt
                                )
                                self._publish_api_cleanup_state(
                                    cleanup_handle,
                                    cleanup_ref,
                                    cleanup_state_callback,
                                )
                            approval_notify = self._make_api_approval_notify(
                                session_id=str(session_id or ""),
                                approval_session_key=bound_session_key,
                                event_callback=approval_event_callback,
                                request_authority=request_authority,
                            )
                            register_gateway_notify(
                                bound_session_key,
                                approval_notify,
                            )
                            approval_notify_registered = True
                            agent = self._create_agent(
                                ephemeral_system_prompt=ephemeral_system_prompt,
                                session_id=session_id,
                                stream_delta_callback=stream_delta_callback,
                                tool_progress_callback=tool_progress_callback,
                                tool_start_callback=tool_start_callback,
                                tool_complete_callback=tool_complete_callback,
                                gateway_session_key=gateway_session_key,
                                requested_model=requested_model,
                                requested_provider=requested_provider,
                                model_options=model_options,
                                route=route,
                                session_model=session_model,
                                confirmed_runtime_lock=confirmed_runtime_lock,
                                clarify_notify_callback=clarify_notify_callback,
                                request_authority=request_authority,
                            )
                            self._assert_api_execution_context_matches_agent(
                                api_execution_context,
                                agent,
                            )
                            (
                                captured_api_execution_context,
                                detached_ineligible_reason,
                            ) = self._build_api_detached_execution_context(
                                agent=agent,
                                gateway_session_key=gateway_session_key,
                                ephemeral_system_prompt=ephemeral_system_prompt,
                                requested_model=requested_model,
                                requested_provider=requested_provider,
                                model_options=model_options,
                                route=route,
                                session_model=session_model,
                                requested_runtime=requested_runtime,
                                route_source=route_source,
                                confirmed_runtime_lock=confirmed_runtime_lock,
                            )
                            if (
                                api_execution_context is not None
                                and captured_api_execution_context
                                != api_execution_context
                            ):
                                raise _DetachedApiContinuityError(
                                    "detached API execution context changed "
                                    "before completion"
                                )
                            agent._api_detached_execution_context = (
                                captured_api_execution_context
                            )
                            agent._api_detached_ineligible_reason = (
                                detached_ineligible_reason
                            )
                            owned_agent_ref[0] = agent
                            agent._api_approval_session_key = bound_session_key
                            with self._api_agent_cache_lock:
                                self._api_active_agents[id(agent)] = agent
                            if agent_ref is not None:
                                agent_ref[0] = agent
                            effective_task_id = request_authority.bind(
                                "task",
                                session_id or str(uuid.uuid4()),
                            ).internal_key
                            # This API-server surface bypasses TurnRunner, so
                            # publish its exact task/baseline ownership before
                            # model execution. Disconnect/stop reapers consume
                            # only this baseline diff and are epoch-gated.
                            _publish_turn_process_ownership(
                                agent, effective_task_id
                            )
                            self._attest_capability_agent_policy(agent)
                            conversation_kwargs = {
                                "user_message": user_message,
                                "conversation_history": conversation_history,
                                "task_id": effective_task_id,
                            }
                            if runtime_effect is not None:
                                conversation_kwargs["runtime_effect"] = (
                                    runtime_effect
                                )
                            result = agent.run_conversation(
                                **conversation_kwargs
                            )
                            if isinstance(result, Mapping) and not isinstance(result, dict):
                                result = dict(result)
                            usage = {
                                "input_tokens": getattr(
                                    agent, "session_prompt_tokens", 0
                                )
                                or 0,
                                "output_tokens": getattr(
                                    agent, "session_completion_tokens", 0
                                )
                                or 0,
                                "total_tokens": getattr(
                                    agent, "session_total_tokens", 0
                                )
                                or 0,
                            }
                            # Include the effective session ID in the result so
                            # callers can track compression-triggered rotations.
                            _eff_sid = _validate_internal_api_session_id(
                                getattr(agent, "session_id", session_id),
                                source="agent.session_id",
                            )
                            if isinstance(result, dict):
                                result["session_id"] = _eff_sid
                            _compacted_in_place = bool(
                                getattr(agent, "_last_compaction_in_place", False)
                            )
                            _session_rotated = (
                                isinstance(_eff_sid, str)
                                and isinstance(session_id, str)
                                and _eff_sid != session_id
                            )
                            if (
                                isinstance(result, dict)
                                and (_compacted_in_place or _session_rotated)
                            ):
                                result["_compressed"] = True
                            include_runtime = bool(
                                requested_runtime
                                or route
                                or confirmed_runtime_lock
                                or (route_source and route_source != "global")
                            )
                            if include_runtime:
                                runtime = dict(
                                    getattr(agent, "_hermes_api_runtime", {}) or {}
                                )
                                raw_provider = getattr(agent, "provider", "")
                                raw_model = getattr(agent, "model", "")
                                actual_provider = (
                                    self._clean_runtime_id(
                                        raw_provider, max_len=80
                                    )
                                    if isinstance(raw_provider, str)
                                    else ""
                                )
                                actual_model = (
                                    self._clean_runtime_id(raw_model)
                                    if isinstance(raw_model, str)
                                    else ""
                                )
                                runtime["provider"] = actual_provider
                                runtime["model"] = actual_model
                                if confirmed_runtime_lock:
                                    expected_provider = self._clean_runtime_id(
                                        (route or {}).get("provider")
                                        or (requested_runtime or {}).get("provider"),
                                        max_len=80,
                                    )
                                    expected_model = self._clean_runtime_id(
                                        (route or {}).get("model")
                                        or (requested_runtime or {}).get("model")
                                    )
                                    if (
                                        (
                                            expected_provider
                                            and actual_provider
                                            != expected_provider
                                        )
                                        or (
                                            expected_model
                                            and actual_model != expected_model
                                        )
                                    ):
                                        raise RuntimeError(
                                            "confirmed model lock runtime mismatch: "
                                            f"expected provider={expected_provider or '<unspecified>'} "
                                            f"model={expected_model or '<unspecified>'}; "
                                            f"actual provider={actual_provider or '<unknown>'} "
                                            f"model={actual_model or '<unknown>'}"
                                        )
                                if requested_runtime:
                                    runtime["requested"] = {
                                        "provider": self._clean_runtime_id(
                                            requested_runtime.get("provider"),
                                            max_len=80,
                                        ),
                                        "model": self._clean_runtime_id(
                                            requested_runtime.get("model")
                                        ),
                                    }
                                runtime["route_source"] = (
                                    route_source
                                    or runtime.get("route_source")
                                    or "global"
                                )
                                runtime = self._sanitize_runtime_metadata(
                                    runtime=runtime,
                                    requested_runtime=requested_runtime,
                                    route_source=route_source or "global",
                                    model_lock=(
                                        "confirmed"
                                        if confirmed_runtime_lock
                                        else ""
                                    ),
                                )
                                if isinstance(result, dict):
                                    result["runtime"] = runtime
                                usage["runtime"] = runtime
                        except _ProviderAuthResolutionError as exc:
                            safe_error = _redact_api_error_text(exc, limit=500)
                            logger.warning(
                                "Provider authentication failed for session=%s: %s",
                                session_id or "",
                                safe_error,
                            )
                            error_msg = (
                                "⚠️ Provider authentication failed: "
                                f"{safe_error}"
                            )
                            result = {
                                "final_response": error_msg,
                                "error": error_msg,
                                "messages": [],
                                "api_calls": 0,
                                "tools": [],
                                "status": "failed",
                                **_terminal_failure_outcome_fields(
                                    "provider_auth_resolution_failed"
                                ),
                            }
                            usage = {
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "total_tokens": 0,
                            }
                        except Exception as exc:
                            execution_error = exc
                    finally:
                        # Once model execution has returned (success or
                        # failure), a later transport disconnect must not reap
                        # background work the completed turn intentionally
                        # left running.
                        if agent is not None:
                            _clear_turn_process_ownership(agent)
                            # This runs while the per-conversation execution
                            # lock is still held.  The old callback can no
                            # longer execute, and a queued next turn cannot yet
                            # attach its fresh authority.
                            self._retire_api_agent_clarifications(agent)
                        try:
                            self._attempt_api_server_cleanup_once(
                                cleanup_handle,
                                use_copied_context=False,
                                cleanup_ref=cleanup_ref,
                                cleanup_state_callback=cleanup_state_callback,
                            )
                        finally:
                            try:
                                if approval_notify_registered:
                                    unregister_gateway_notify(bound_session_key)
                            finally:
                                try:
                                    if approval_token is not None:
                                        reset_current_session_key(approval_token)
                                finally:
                                    self._clear_api_approval_scope(
                                        session_id,
                                        approval_session_key=bound_session_key,
                                        request_authority=request_authority,
                                    )
                                    clear_session_vars(tokens)

                    evicted_agent = self._finalize_api_agent_cache_after_turn(
                        requested_session_id=session_id,
                        agent=agent,
                        result=result,
                        execution_error=execution_error,
                        request_authority=request_authority,
                    )
                    return (
                        result,
                        usage,
                        execution_error,
                        cleanup_handle,
                        evicted_agent,
                    )
                except Exception:
                    # Binding/cleanup failures are terminal for cache purposes.
                    # Fence the exact instance before the serialized lock opens.
                    evicted = self._pop_cached_api_agent(
                        session_id,
                        expected_agent=owned_agent_ref[0],
                        request_authority=request_authority,
                    )
                    if evicted is not None:
                        self._release_api_cached_agent(evicted)
                    raise

        async def _own_execution_and_cleanup(executor_future):
            evicted_agent = None
            try:
                (
                    result,
                    usage,
                    execution_error,
                    cleanup_handle,
                    evicted_agent,
                ) = await asyncio.shield(executor_future)
                if cleanup_handle.status != "confirmed":
                    retry_task = self._ensure_api_cleanup_retry(
                        cleanup_handle,
                        cleanup_ref=cleanup_ref,
                        cleanup_state_callback=cleanup_state_callback,
                    )
                    await asyncio.shield(retry_task)
                return result, usage, execution_error
            finally:
                agent = owned_agent_ref[0]
                release_deferred = False
                if agent is not None:
                    with self._api_agent_cache_lock:
                        self._api_active_agents.pop(id(agent), None)
                        if id(agent) in self._api_deferred_agent_releases:
                            self._api_deferred_agent_releases.discard(id(agent))
                            release_deferred = True
                if evicted_agent is not None:
                    self._release_api_cached_agent(evicted_agent)
                elif release_deferred:
                    self._release_api_cached_agent(agent)
                self._inflight_agent_runs -= 1

        self._activate_admitted_request()
        self._inflight_agent_runs += 1
        # ``run_in_executor`` does not propagate ContextVars.  The API profile
        # middleware owns both profile-home and secret authority, so every
        # worker submission receives a fresh snapshot from this exact request.
        request_context = contextvars.copy_context()
        executor_future = loop.run_in_executor(
            None,
            request_context.run,
            _run,
        )
        completion_owner = asyncio.create_task(
            _own_execution_and_cleanup(executor_future)
        )
        self._track_api_background_task(completion_owner)
        result, usage, execution_error = await asyncio.shield(completion_owner)
        if execution_error is not None:
            raise execution_error
        return result, usage

    # ------------------------------------------------------------------
    # /v1/runs — structured event streaming
    # ------------------------------------------------------------------

    _RUN_STREAM_TTL = 300  # seconds before orphaned runs are swept
    _RUN_STATUS_TTL = 3600  # seconds to retain terminal run status for polling

    def _set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        run_scope: Optional[APIRequestScope] = None,
        request_authority: Optional[APIRequestScope] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        """Update pollable run status without exposing private agent objects."""
        run_scope = run_scope or self._api_request_scope(
            "run",
            run_id,
            authority=request_authority,
        )
        now = time.time()
        current = self._run_statuses.get(run_scope, {})
        current.update({
            "object": "hermes.run",
            "run_id": run_id,
            "status": status,
            "updated_at": now,
        })
        current.setdefault("created_at", fields.pop("created_at", now))
        current.update(fields)
        self._run_statuses[run_scope] = current
        return current

    def _make_run_event_callback(
        self,
        run_id: str,
        loop: "asyncio.AbstractEventLoop",
        *,
        run_scope: Optional[APIRequestScope] = None,
        request_authority: Optional[APIRequestScope] = None,
    ):
        """Return a tool_progress_callback that pushes structured events to the run's SSE queue."""
        run_scope = run_scope or self._api_request_scope(
            "run",
            run_id,
            authority=request_authority,
        )

        def _push(event: Dict[str, Any]) -> None:
            self._set_run_status(
                run_id,
                self._run_statuses.get(run_scope, {}).get("status", "running"),
                run_scope=run_scope,
                last_event=event.get("event"),
            )
            q = self._run_streams.get(run_scope)
            if q is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            elif event_type in {"subagent.start", "subagent.complete"}:
                event = {
                    "event": event_type,
                    "run_id": run_id,
                    "timestamp": ts,
                }
                if preview is not None:
                    event["preview"] = redact_sensitive_text(
                        str(preview), force=True
                    )
                for key in (
                    "goal",
                    "task_count",
                    "task_index",
                    "subagent_id",
                    "child_session_id",
                    "parent_id",
                    "depth",
                    "model",
                    "tool_count",
                    "status",
                    "summary",
                    "duration_seconds",
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "api_calls",
                    "cost_usd",
                    "files_read",
                    "files_written",
                    "output_tail",
                ):
                    value = kwargs.get(key)
                    if value is None:
                        continue
                    # Free-text fields can carry child terminal/tool output —
                    # force the same secret redaction the API applies to error
                    # text before it leaves the process on a public stream.
                    if key in ("goal", "summary", "output_tail") and isinstance(
                        value, str
                    ):
                        value = redact_sensitive_text(value, force=True)
                    event[key] = value
                _push(event)
            # _thinking, subagent.tool, and subagent_progress are intentionally
            # not forwarded on the /v1/runs stream: they are high-volume UI
            # noise. Lifecycle boundaries (start/complete) still need to land
            # so clients can observe delegate_task timeouts and failures.

        return _callback

    @_admit_api_agent_request
    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs — start an agent run, return run_id immediately."""
        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)
        if not isinstance(body, dict):
            return web.json_response(
                _openai_error("Request body must be an object"),
                status=400,
            )
        request_authority = (
            _api_request_authority.get()
            or self._api_request_scope("request")
        )

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = raw_input if isinstance(raw_input, str) else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # Accept explicit conversation_history from the request body.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store_for_request().get(previous_response_id)
            if stored:
                conversation_history = list(stored.get("conversation_history", []))
                if "session_id" in stored:
                    try:
                        stored_session_id = _validate_internal_api_session_id(
                            stored["session_id"],
                            source="runs_previous_response",
                        )
                    except _InvalidInternalAPISessionID:
                        return _invalid_internal_session_id_response()
                if instructions is None:
                    instructions = stored.get("instructions")

        # When input is a multi-message array, extract all but the last
        # message as conversation history (the last becomes user_message).
        # Only fires when no explicit history was provided.
        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        # Flatten multi-part content blocks to text
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append({"role": msg["role"], "content": str(content)})

        if "session_id" in body:
            session_id, session_err = self._validate_api_session_id_value(
                body.get("session_id"),
                required=True,
            )
            if session_err is not None:
                return session_err
        elif stored_session_id is not None:
            session_id = stored_session_id
        else:
            session_id = ""
        route = self._resolve_route(body.get("model"))
        try:
            agent_overrides = _request_agent_overrides(
                body,
                virtual_model=self._active_model_name(),
            )
        except ValueError as exc:
            return _invalid_runtime_request_response(exc)
        selection_error = self._request_route_conflict_error(
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            requested_model=agent_overrides.get("requested_model"),
            requested_provider=agent_overrides.get("requested_provider"),
            route=route,
        )
        if selection_error:
            return web.json_response(_openai_error(selection_error), status=400)

        reservation = self._reserve_agent_run()
        if reservation is None:
            return self._concurrency_limit_response()

        run_id = f"run_{uuid.uuid4().hex}"
        session_id = session_id or run_id
        run_scope = request_authority.bind("run", run_id)
        # Approval queues gate host-side tool execution and must be isolated
        # per API run.  Client-provided session IDs and memory session keys are
        # conversation/memory scopes, not authorization namespaces: multiple
        # concurrent runs can intentionally share them, and resolving an
        # approval for one run must not unblock another run's dangerous command.
        approval_session_key = request_authority.bind(
            "run-approval",
            run_id,
        ).internal_key
        ephemeral_system_prompt = instructions
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        created_at = time.time()
        self._run_streams[run_scope] = q
        self._run_streams_created[run_scope] = created_at
        self._run_approval_sessions[run_scope] = approval_session_key

        event_cb = self._make_run_event_callback(
            run_id,
            loop,
            run_scope=run_scope,
        )

        def _put_event_if_active(event: Optional[Dict]) -> None:
            """Enqueue only while this run still owns live transport state."""
            if self._run_streams.get(run_scope) is q:
                q.put_nowait(event)

        # Also wire stream_delta_callback so message.delta events flow through.
        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            if run_scope not in self._run_streams:
                return
            try:
                loop.call_soon_threadsafe(_put_event_if_active, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        def _clarify_notify(payload: Dict[str, Any]) -> None:
            clarify_id = str(payload.get("id", "") or "")
            if clarify_id:
                clarification_scope = request_authority.bind(
                    "clarification-id",
                    clarify_id,
                )
                with self._api_clarifications_lock:
                    state = self._api_pending_clarifications.get(
                        clarification_scope
                    )
                    if state is not None:
                        state["_run_scope"] = run_scope

            def _publish() -> None:
                self._set_run_status(
                    run_id,
                    "waiting_for_clarification",
                    run_scope=run_scope,
                    last_event="clarify.request",
                )
                try:
                    q.put_nowait({
                        "event": "clarify.request",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        **payload,
                    })
                except Exception:
                    pass

            try:
                loop.call_soon_threadsafe(_publish)
            except RuntimeError:
                pass

        self._set_run_status(
            run_id,
            "queued",
            run_scope=run_scope,
            created_at=created_at,
            session_id=session_id,
            model=body.get("model", self._active_model_name()),
        )

        async def _run_and_close():
            terminalized = False
            agent = None
            try:
                self._set_run_status(
                    run_id,
                    "running",
                    run_scope=run_scope,
                )
                if run_scope in self._stopping_run_ids:
                    _put_event_if_active(
                        {
                            "event": "run.cancelled",
                            "run_id": run_id,
                            "timestamp": time.time(),
                            "completed": False,
                            "partial": False,
                            "interrupted": True,
                            "failed": False,
                            "incomplete": True,
                            "turn_exit_reason": "stopped_before_start",
                        }
                    )
                    self._set_run_status(
                        run_id,
                        "cancelled",
                        run_scope=run_scope,
                        completed=False,
                        partial=False,
                        interrupted=True,
                        failed=False,
                        incomplete=True,
                        terminal=True,
                        turn_exit_reason="stopped_before_start",
                        last_event="run.cancelled",
                    )
                    terminalized = True
                    return
                def _approval_notify(approval_data: Dict[str, Any]) -> None:
                    approval_id = str(
                        (approval_data or {}).get("approval_id", "") or ""
                    )
                    from tools.approval import get_pending_gateway_approvals

                    core_state = next(
                        (
                            item
                            for item in get_pending_gateway_approvals(
                                approval_session_key,
                                include_authority_binding=True,
                            )
                            if item.get("approval_id") == approval_id
                        ),
                        {},
                    )
                    capability_epoch_sha256 = str(
                        core_state.get(
                            "_capability_epoch_sha256", ""
                        )
                        or ""
                    )
                    if (
                        re.fullmatch(r"[0-9a-f]{32}", approval_id) is None
                        or re.fullmatch(
                            r"[0-9a-f]{64}", capability_epoch_sha256
                        )
                        is None
                    ):
                        raise RuntimeError(
                            "run approval core returned invalid binding metadata"
                        )
                    allow_permanent = bool(
                        (approval_data or {}).get("allow_permanent", False)
                    )
                    allow_session = bool(
                        (approval_data or {}).get("allow_session", True)
                    )
                    choices = (
                        ["once", "deny"]
                        if not allow_session
                        else list(API_APPROVAL_CHOICES)
                    )
                    if allow_session and not allow_permanent:
                        choices.remove("always")
                    event = {
                        "event": "approval.request",
                        "run_id": run_id,
                        "session_id": session_id,
                        "approval_id": approval_id,
                        "timestamp": time.time(),
                        "command": str((approval_data or {}).get("command", "") or ""),
                        "description": str((approval_data or {}).get("description", "") or ""),
                        "pattern_keys": [
                            str(value)
                            for value in (
                                (approval_data or {}).get("pattern_keys") or []
                            )
                        ],
                        "allow_permanent": allow_permanent,
                        "allow_session": allow_session,
                        "choices": choices,
                        "owner_authority_required_for": [
                            value for value in choices if value != "deny"
                        ],
                        "owner_authority_schema": (
                            self._approval_authority_schema()
                        ),
                        "capability_epoch_sha256": (
                            capability_epoch_sha256
                        ),
                    }
                    self._set_run_status(
                        run_id,
                        "waiting_for_approval",
                        run_scope=run_scope,
                        last_event="approval.request",
                        pending_approval_id=approval_id,
                    )
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, event)
                    except Exception:
                        pass

                cleanup_ref: list[Any] = [None]
                last_cleanup_marker: list[Any] = [None]

                def _cleanup_state_callback(state: Dict[str, Any]) -> None:
                    if state.get("status") not in {
                        "cleanup_blocked",
                        "cleanup_degraded",
                    }:
                        return

                    def _publish() -> None:
                        marker = (
                            state.get("status"),
                            state.get("attempts"),
                            state.get("error"),
                        )
                        self._set_run_status(
                            run_id,
                            "cleanup_blocked",
                            run_scope=run_scope,
                            completed=False,
                            incomplete=True,
                            terminal=False,
                            cleanup=dict(state),
                            last_event="run.cleanup_blocked",
                        )
                        if marker == last_cleanup_marker[0]:
                            return
                        last_cleanup_marker[0] = marker
                        try:
                            q.put_nowait({
                                "event": "run.cleanup_blocked",
                                "run_id": run_id,
                                "timestamp": time.time(),
                                "status": "cleanup_blocked",
                                "completed": False,
                                "incomplete": True,
                                "terminal": False,
                                "cleanup": dict(state),
                            })
                        except Exception:
                            pass

                    try:
                        loop.call_soon_threadsafe(_publish)
                    except RuntimeError:
                        pass

                def _run_sync():
                    nonlocal agent
                    from gateway.session_context import clear_session_vars
                    from tools.approval import (
                        register_gateway_notify,
                        reset_current_session_key,
                        set_current_session_key,
                        unregister_gateway_notify,
                    )

                    effective_task_id = request_authority.bind(
                        "task",
                        session_id or run_id,
                    ).internal_key
                    approval_token = None
                    session_tokens = []
                    cleanup_handle: Optional[_APIServerCleanupHandle] = None
                    result: Any = None
                    execution_error: Optional[Exception] = None
                    stop_requested_before_execution_finished = False
                    from gateway.run import _profile_runtime_scope

                    with _profile_runtime_scope(
                        Path(request_authority.canonical_home)
                    ):
                        try:
                            try:
                                # Bind approval/session identity for this API run via
                                # contextvars so concurrent runs do not share process
                                # environment state.
                                approval_token = set_current_session_key(
                                    approval_session_key
                                )
                                session_tokens = self._bind_api_server_session(
                                    chat_id=session_id or "",
                                    session_key=approval_session_key,
                                    session_id=session_id or "",
                                )
                                cleanup_handle = _APIServerCleanupHandle(
                                    approval_session_key,
                                    session_tokens.capability_epoch_sha256,
                                    contextvars.copy_context(),
                                )
                                model_release_receipt = self._admit_bound_api_server_run(
                                    session_id=str(session_id or ""),
                                    capability_epoch_sha256=(
                                        session_tokens.capability_epoch_sha256
                                    ),
                                )
                                if model_release_receipt is not None:
                                    cleanup_handle.model_release_receipt = dict(
                                        model_release_receipt
                                    )
                                    self._publish_api_cleanup_state(
                                        cleanup_handle,
                                        cleanup_ref,
                                        _cleanup_state_callback,
                                    )
                                    self._set_run_status(
                                        run_id,
                                        "running",
                                        run_scope=run_scope,
                                        model_release_receipt=dict(
                                            model_release_receipt
                                        ),
                                    )
                                agent = self._create_agent(
                                    ephemeral_system_prompt=ephemeral_system_prompt,
                                    session_id=session_id,
                                    stream_delta_callback=_text_cb,
                                    tool_progress_callback=event_cb,
                                    gateway_session_key=gateway_session_key,
                                    requested_model=agent_overrides.get(
                                        "requested_model"
                                    ),
                                    requested_provider=agent_overrides.get(
                                        "requested_provider"
                                    ),
                                    model_options=agent_overrides.get(
                                        "model_options"
                                    ),
                                    route=route,
                                    clarify_notify_callback=_clarify_notify,
                                    reuse_cached_agent=False,
                                    request_authority=request_authority,
                                )
                                (
                                    agent._api_detached_execution_context,
                                    agent._api_detached_ineligible_reason,
                                ) = self._build_api_detached_execution_context(
                                    agent=agent,
                                    gateway_session_key=gateway_session_key,
                                    ephemeral_system_prompt=(
                                        ephemeral_system_prompt
                                    ),
                                    requested_model=agent_overrides.get(
                                        "requested_model"
                                    ),
                                    requested_provider=agent_overrides.get(
                                        "requested_provider"
                                    ),
                                    model_options=agent_overrides.get(
                                        "model_options"
                                    ),
                                    route=route,
                                    session_model=None,
                                    requested_runtime=None,
                                    route_source=(
                                        "model_routes"
                                        if route is not None
                                        else "raw_request"
                                        if agent_overrides
                                        else "global"
                                    ),
                                    confirmed_runtime_lock=False,
                                )
                                self._active_run_agents[run_scope] = agent
                                if run_scope in self._stopping_run_ids:
                                    agent.interrupt(
                                        "Stop requested via API before run start"
                                    )
                                register_gateway_notify(
                                    approval_session_key, _approval_notify
                                )
                                self._attest_capability_agent_policy(agent)
                                # /v1/runs owns a separate agent lifecycle and
                                # bypasses both TurnRunner and _run_agent.
                                _publish_turn_process_ownership(
                                    agent, effective_task_id
                                )
                                result = agent.run_conversation(
                                    user_message=user_message,
                                    conversation_history=conversation_history,
                                    task_id=effective_task_id,
                                )
                            except Exception as exc:
                                execution_error = exc
                                if cleanup_handle is None:
                                    self._publish_api_authority_not_created(
                                        cleanup_ref,
                                        _cleanup_state_callback,
                                    )
                        finally:
                            # Clear immediately at the model-execution boundary;
                            # a later stop during Canonical cleanup must not reap
                            # intentionally surviving background work.
                            if agent is not None:
                                _clear_turn_process_ownership(agent)
                                self._retire_api_agent_clarifications(agent)
                            # Linearize stop ownership at the execution/cleanup
                            # boundary. A stop received while the model is still
                            # executing cancels the run; a later stop received
                            # while exact capability revoke is blocking must not
                            # rewrite an already model-completed outcome.
                            stop_requested_before_execution_finished = (
                                run_scope in self._stopping_run_ids
                            )
                            try:
                                if cleanup_handle is not None:
                                    self._attempt_api_server_cleanup_once(
                                        cleanup_handle,
                                        use_copied_context=False,
                                        cleanup_ref=cleanup_ref,
                                        cleanup_state_callback=(
                                            _cleanup_state_callback
                                        ),
                                    )
                            finally:
                                try:
                                    unregister_gateway_notify(approval_session_key)
                                finally:
                                    if approval_token is not None:
                                        try:
                                            reset_current_session_key(approval_token)
                                        except Exception:
                                            pass
                                    if session_tokens:
                                        try:
                                            clear_session_vars(session_tokens)
                                        except Exception:
                                            pass
                        usage = {
                            "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                            "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                            "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                        }
                        return (
                            result,
                            usage,
                            execution_error,
                            cleanup_handle,
                            stop_requested_before_execution_finished,
                        )

                async def _own_run_sync(executor_future):
                    outcome = await asyncio.shield(executor_future)
                    cleanup_handle = outcome[3]
                    if (
                        cleanup_handle is not None
                        and cleanup_handle.status != "confirmed"
                    ):
                        retry_task = self._ensure_api_cleanup_retry(
                            cleanup_handle,
                            cleanup_ref=cleanup_ref,
                            cleanup_state_callback=_cleanup_state_callback,
                        )
                        await asyncio.shield(retry_task)
                    return outcome

                run_context = contextvars.copy_context()
                executor_future = loop.run_in_executor(
                    None,
                    run_context.run,
                    _run_sync,
                )
                completion_owner = asyncio.create_task(
                    _own_run_sync(executor_future)
                )
                self._track_api_background_task(completion_owner)
                (
                    result,
                    usage,
                    execution_error,
                    cleanup_handle,
                    stop_requested_before_execution_finished,
                ) = await asyncio.shield(completion_owner)
                if not self._api_cleanup_allows_terminal(cleanup_ref):
                    state = cleanup_ref[0] if cleanup_ref else None
                    self._set_run_status(
                        run_id,
                        "cleanup_blocked",
                        run_scope=run_scope,
                        completed=False,
                        incomplete=True,
                        terminal=False,
                        cleanup=dict(state)
                        if isinstance(state, Mapping)
                        else None,
                        last_event="run.cleanup_blocked",
                    )
                    return
                if execution_error is not None:
                    raise execution_error
                if stop_requested_before_execution_finished:
                    cancelled_fields = {
                        "completed": False,
                        "partial": False,
                        "interrupted": True,
                        "failed": False,
                        "incomplete": True,
                        "turn_exit_reason": "stop_requested",
                    }
                    _put_event_if_active(
                        {
                            "event": "run.cancelled",
                            "run_id": run_id,
                            "timestamp": time.time(),
                            **cancelled_fields,
                        }
                    )
                    self._set_run_status(
                        run_id,
                        "cancelled",
                        run_scope=run_scope,
                        terminal=True,
                        last_event="run.cancelled",
                        **cancelled_fields,
                    )
                    terminalized = True
                    return
                _effective_internal_api_session_id(
                    result,
                    fallback=session_id,
                    source="run_result",
                )
                outcome = _session_stream_outcome(result)
                result_mapping = result if isinstance(result, Mapping) else {}
                final_response = result_mapping.get("final_response", "") or ""
                outcome_fields = {
                    "completed": outcome["completed"],
                    "partial": outcome["partial"],
                    "interrupted": outcome["interrupted"],
                    "failed": outcome["failed"],
                    "incomplete": outcome["incomplete"],
                    "turn_exit_reason": outcome["turn_exit_reason"],
                }
                terminal_event = {
                    "event": outcome["run_event"],
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "status": outcome["status"],
                    "output": final_response,
                    "usage": usage,
                    **outcome_fields,
                }
                status_fields = {
                    "output": final_response,
                    "usage": usage,
                    "terminal": True,
                    "last_event": outcome["run_event"],
                    **outcome_fields,
                }
                if outcome["incomplete"]:
                    raw_error = result_mapping.get("error")
                    error_msg = _redact_api_error_text(
                        raw_error
                        or (
                            "agent returned an invalid result"
                            if outcome["turn_exit_reason"] == "invalid_agent_result"
                            else "agent run did not complete"
                        )
                    )
                    terminal_event["error"] = error_msg
                    status_fields["error"] = error_msg
                _put_event_if_active(terminal_event)
                self._set_run_status(
                    run_id,
                    outcome["status"],
                    run_scope=run_scope,
                    **status_fields,
                )
                terminalized = True
            except asyncio.CancelledError:
                self._set_run_status(
                    run_id,
                    "stopping",
                    run_scope=run_scope,
                    completed=False,
                    partial=False,
                    interrupted=True,
                    failed=False,
                    incomplete=True,
                    terminal=False,
                    last_event="run.stopping",
                )
                raise
            except _InvalidInternalAPISessionID:
                error_msg = "Internal session continuity state is invalid."
                failure_fields = {
                    "error": error_msg,
                    "error_code": "invalid_internal_session_id",
                    "terminal": True,
                    "completed": False,
                    "partial": False,
                    "interrupted": False,
                    "failed": True,
                    "incomplete": True,
                    "turn_exit_reason": "invalid_internal_session_id",
                    "last_event": "run.failed",
                }
                self._set_run_status(
                    run_id,
                    "failed",
                    run_scope=run_scope,
                    **failure_fields,
                )
                _put_event_if_active({
                    "event": "run.failed",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    **{
                        key: value
                        for key, value in failure_fields.items()
                        if key not in {"terminal", "last_event"}
                    },
                })
                terminalized = True
            except _ProviderAuthResolutionError as exc:
                # /v1/runs builds its own agent via _create_agent() and does
                # not route through _run_agent() (see that method's own
                # _ProviderAuthResolutionError branch), so it needs its own
                # handling to surface the same distinguished, controlled
                # message the other endpoints give a provider auth/credential
                # failure, instead of falling through to the generic
                # except-Exception branch below.
                safe_error = _redact_api_error_text(exc, limit=500)
                logger.warning(
                    "Provider authentication failed for run=%s: %s",
                    run_id,
                    safe_error,
                )
                error_msg = (
                    "⚠️ Provider authentication failed: "
                    f"{safe_error}"
                )
                failure_outcome = _terminal_failure_outcome_fields(
                    "provider_auth_resolution_failed"
                )
                failure_fields = {
                    "error": error_msg,
                    "terminal": True,
                    "last_event": "run.failed",
                    **failure_outcome,
                }
                self._set_run_status(
                    run_id,
                    "failed",
                    run_scope=run_scope,
                    **failure_fields,
                )
                try:
                    _put_event_if_active({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        **{
                            key: value
                            for key, value in failure_fields.items()
                            if key not in {"terminal", "last_event", "status"}
                        },
                    })
                except Exception:
                    pass
                terminalized = True
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                self._set_run_status(
                    run_id,
                    "failed",
                    run_scope=run_scope,
                    error=_redact_api_error_text(exc),
                    terminal=True,
                    completed=False,
                    partial=False,
                    interrupted=False,
                    failed=True,
                    incomplete=True,
                    turn_exit_reason="api_run_exception",
                    last_event="run.failed",
                )
                try:
                    _put_event_if_active({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": _redact_api_error_text(exc),
                        "completed": False,
                        "partial": False,
                        "interrupted": False,
                        "failed": True,
                        "incomplete": True,
                        "turn_exit_reason": "api_run_exception",
                    })
                except Exception:
                    pass
                terminalized = True
            finally:
                if terminalized:
                    # Sentinel: signal SSE stream to close only after the exact
                    # cleanup receipt and terminal status are both durable.
                    try:
                        _put_event_if_active(None)
                    except Exception:
                        pass
                    self._active_run_agents.pop(run_scope, None)
                    self._active_run_tasks.pop(run_scope, None)
                    self._run_approval_sessions.pop(run_scope, None)
                    self._release_api_cached_agent(agent)
                    self._stopping_run_ids.discard(run_scope)

        try:
            task = asyncio.create_task(_run_and_close())
        except Exception:
            reservation.release()
            raise
        task.add_done_callback(lambda _task: reservation.release())
        self._active_run_tasks[run_scope] = task
        # The registered task now owns the run's liveness.  Release the
        # pre-registration reservation synchronously so drain/concurrency
        # accounting never counts this one logical run twice while the event
        # loop has not scheduled ``_run_and_close`` yet.
        reservation.release()
        self._activate_admitted_request()
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        response_headers = (
            {"X-Hermes-Session-Key": gateway_session_key} if gateway_session_key else {}
        )
        return web.json_response(
            {"run_id": run_id, "status": "started"},
            status=202,
            headers=response_headers,
        )

    async def _handle_get_run(self, request: "web.Request") -> "web.Response":
        """GET /v1/runs/{run_id} — return pollable run status for external UIs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        run_scope = self._api_request_scope("run", run_id)
        status = self._run_statuses.get(run_scope)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )
        return web.json_response(status)

    async def _handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events — SSE stream of structured agent lifecycle events."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        run_scope = self._api_request_scope("run", run_id)

        # Allow subscribing slightly before the run is registered (race condition window)
        for _ in range(20):
            if run_scope in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        q = self._run_streams[run_scope]
        self._run_stream_subscribers.add(run_scope)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    # Run finished — send final SSE comment and close
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except Exception as exc:
            logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
        finally:
            self._run_stream_subscribers.discard(run_scope)
            self._run_streams.pop(run_scope, None)
            self._run_streams_created.pop(run_scope, None)

        return response


    async def _handle_run_approval(self, request: "web.Request") -> "web.Response":
        """Resolve one exact run approval without FIFO or blanket authority."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if not self._api_auth_configured():
            return web.json_response(
                _openai_error(
                    "Run approval control requires API key authentication",
                    code="approval_auth_required",
                ),
                status=403,
            )

        session_id, session_err = self._parse_api_control_session_id(
            request,
            required=True,
        )
        if session_err is not None:
            return session_err

        run_id = request.match_info["run_id"]
        request_authority = (
            _api_request_authority.get()
            or self._api_request_scope("request")
        )
        run_scope = request_authority.bind("run", run_id)
        status = self._run_statuses.get(run_scope)
        if status is None or str(status.get("session_id", "") or "") != session_id:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )

        body, body_err = await self._read_json_body(request)
        if body_err is not None:
            return body_err
        approval_id = body.get("approval_id")
        if (
            not isinstance(approval_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", approval_id) is None
        ):
            return web.json_response(
                _openai_error(
                    "Run approval requires one exact opaque approval_id",
                    code="invalid_approval_id",
                ),
                status=400,
            )
        choice = body.get("choice")
        if not isinstance(choice, str) or choice not in API_APPROVAL_CHOICES:
            return web.json_response(
                _openai_error(
                    "Invalid approval choice; expected one of: once, session, always, deny",
                    code="invalid_approval_choice",
                ),
                status=400,
            )
        if (
            choice == "deny"
            and set(body) != {"approval_id", "choice"}
        ) or (
            choice != "deny"
            and bool(
                set(body)
                - {"approval_id", "choice", "owner_authority"}
            )
        ):
            return web.json_response(
                _openai_error(
                    "Positive run approval accepts only owner_authority; deny "
                    "accepts no authority fields",
                    code="invalid_approval_response",
                ),
                status=400,
            )

        approval_session_key = self._run_approval_sessions.get(run_scope)
        expected_approval_key = request_authority.bind(
            "run-approval",
            run_id,
        ).internal_key
        if approval_session_key != expected_approval_key:
            return web.json_response(
                _openai_error(
                    f"Run has no active approval session: {run_id}",
                    code="approval_not_active",
                ),
                status=409,
            )

        from tools.approval import (
            get_pending_gateway_approvals,
            resolve_gateway_approval_by_id,
            session_authority_fence_is_current,
        )
        pending = next(
            (
                item
                for item in get_pending_gateway_approvals(
                    approval_session_key,
                    include_authority_binding=True,
                )
                if item.get("approval_id") == approval_id
            ),
            None,
        )
        if pending is None:
            return web.json_response(
                _openai_error(
                    "Run approval is not pending or has expired",
                    code="approval_not_pending",
                ),
                status=409,
            )

        capability_epoch_sha256 = str(
            pending.get("_capability_epoch_sha256", "") or ""
        )
        authority_generation = pending.get("_authority_generation")
        if (
            re.fullmatch(r"[0-9a-f]{64}", capability_epoch_sha256) is None
            or type(authority_generation) is not int
            or not session_authority_fence_is_current(
                approval_session_key,
                authority_generation,
                capability_epoch_sha256,
            )
        ):
            return web.json_response(
                _openai_error(
                    "Run approval authority epoch is stale",
                    code="approval_authority_stale",
                ),
                status=409,
            )
        if choice == "always" and not pending.get("allow_permanent", False):
            return web.json_response(
                _openai_error(
                    "Permanent approval is not offered for this action",
                    code="permanent_approval_not_allowed",
                ),
                status=400,
            )
        if (
            pending.get("exact_execution") is True
            and pending.get("allow_session") is False
            and choice not in {"once", "deny"}
        ):
            return web.json_response(
                _openai_error(
                    "Exact execution approvals allow only once or deny",
                    code="approval_choice_not_allowed",
                ),
                status=400,
            )
        if choice != "deny":
            authority_err = self._verify_and_consume_api_approval_authority(
                body.get("owner_authority"),
                session_id=session_id,
                run_id=run_id,
                approval_id=approval_id,
                choice=choice,
                capability_epoch_sha256=capability_epoch_sha256,
                request=request,
            )
            if authority_err is not None:
                return authority_err

        resolved = resolve_gateway_approval_by_id(
            approval_session_key,
            approval_id,
            choice,
        )

        if resolved != 1:
            return web.json_response(
                _openai_error(
                    "Run approval expired before the response was accepted",
                    code="approval_not_pending",
                ),
                status=409,
            )

        self._set_run_status(
            run_id,
            "running",
            run_scope=run_scope,
            last_event="approval.responded",
            pending_approval_id=None,
        )
        q = self._run_streams.get(run_scope)
        if q is not None:
            try:
                q.put_nowait({
                    "event": "approval.responded",
                    "run_id": run_id,
                    "session_id": session_id,
                    "approval_id": approval_id,
                    "timestamp": time.time(),
                    "choice": choice,
                    "resolved": resolved,
                })
            except Exception:
                pass

        return web.json_response({
            "object": "hermes.run.approval_response",
            "run_id": run_id,
            "session_id": session_id,
            "approval_id": approval_id,
            "choice": choice,
            "resolved": resolved,
        })

    async def _handle_stop_run(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/stop — interrupt a running agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        run_scope = self._api_request_scope("run", run_id)
        agent = self._active_run_agents.get(run_scope)
        task = self._active_run_tasks.get(run_scope)

        if agent is None and task is None:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        self._set_run_status(
            run_id,
            "stopping",
            run_scope=run_scope,
            completed=False,
            incomplete=True,
            terminal=False,
            last_event="run.stopping",
        )
        self._stopping_run_ids.add(run_scope)

        if agent is not None:
            try:
                request_hard_interrupt(agent, "Stop requested via API")
            except Exception:
                pass
            # Stop abandons model execution: reap only this run's process
            # baseline diff, with the upstream epoch gate protecting a newer
            # concurrent claimant of the same session/task id.
            _reap_disconnected_agent_processes(
                agent, source="api_server_run_stop"
            )
            self._cancel_api_agent_clarifications(agent)

        # Stop is cooperative and returns immediately.  The existing task
        # remains the sole owner of its executor thread and exact Canonical
        # cleanup receipt until the run really exits.
        return web.json_response({"run_id": run_id, "status": "stopping"})

    async def _sweep_orphaned_runs(self) -> None:
        """Periodically expire transport buffers and terminal status records."""
        while True:
            await asyncio.sleep(60)
            self._sweep_orphaned_runs_once(time.time())

    def _sweep_orphaned_runs_once(self, now: Optional[float] = None) -> None:
        """Expire old SSE buffers without treating transport age as run age."""
        if now is None:
            now = time.time()
        terminal_statuses = {
            "completed",
            "failed",
            "cancelled",
            "partial",
            "interrupted",
            "incomplete",
        }
        stale = [
            run_scope
            for run_scope, created_at in list(self._run_streams_created.items())
            if now - created_at > self._RUN_STREAM_TTL
            and run_scope not in self._run_stream_subscribers
        ]
        for run_scope in stale:
            logger.debug(
                "[api_server] sweeping expired run transport %s",
                run_scope.public_id,
            )
            task = self._active_run_tasks.get(run_scope)
            task_done = task is None or task.done()
            if task_done and run_scope not in self._active_run_agents:
                try:
                    from tools.approval import unregister_gateway_notify

                    approval_session_key = self._run_approval_sessions.get(
                        run_scope
                    )
                    if approval_session_key:
                        unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
                self._run_approval_sessions.pop(run_scope, None)
                self._stopping_run_ids.discard(run_scope)
            # Transport age only bounds buffering.  A live task, agent, epoch,
            # or cleanup handle remains owned by the exact terminal path.
            self._run_streams.pop(run_scope, None)
            self._run_streams_created.pop(run_scope, None)

        stale_statuses = [
            run_scope
            for run_scope, status in list(self._run_statuses.items())
            if status.get("status") in terminal_statuses
            and now - float(status.get("updated_at", 0) or 0) > self._RUN_STATUS_TTL
        ]
        for run_scope in stale_statuses:
            self._run_statuses.pop(run_scope, None)

    # ------------------------------------------------------------------
    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    def _api_key_passes_startup_guard(self) -> bool:
        """Return True when API_SERVER_KEY is present and strong enough to start."""
        if not self._api_auth_configured():
            logger.error(
                "[%s] Refusing to start: API_SERVER_KEY is required for the API server, "
                "including loopback-only binds on %s.",
                self.name, self._host,
            )
            return False

        if self._api_bearer_verifier is not None:
            return True
        try:
            from hermes_cli.auth import has_usable_secret
        except Exception as exc:
            # Fail CLOSED. This guard is the only thing between a guessable
            # key and a terminal-capable endpoint, so "the check could not be
            # run" must not resolve to "start anyway" — the same posture
            # tools/credential_files.py takes when its deny-list cannot be
            # consulted.
            logger.error(
                "[%s] Refusing to start: API_SERVER_KEY strength could not be "
                "verified (%s: %s), and this endpoint dispatches "
                "terminal-capable agent work. Repair the installation before "
                "starting the API server on %s.",
                self.name, type(exc).__name__, exc, self._host,
            )
            return False

        if not has_usable_secret(self._api_key, min_length=16):
            logger.error(
                "[%s] Refusing to start: API_SERVER_KEY is a "
                "placeholder or too short (<16 chars). This endpoint "
                "dispatches terminal-capable agent work — a guessable "
                "key is remote code execution. Generate a strong secret "
                "(e.g. `openssl rand -hex 32`) and set API_SERVER_KEY "
                "before starting the API server on %s.",
                self.name, self._host,
            )
            return False
        return True

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the aiohttp web server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            self._api_multiplex_enabled()
        except RuntimeError as exc:
            self._set_fatal_error(
                "api_server_multiplex_unsupported",
                str(exc),
                retryable=False,
            )
            logger.error("[%s] Refusing multiplex API server: %s", self.name, exc)
            return False

        if not self._api_key_passes_startup_guard():
            # A rejected API_SERVER_KEY is a configuration error, not a
            # transient blip — the key will not become valid on its own. A
            # bare ``return False`` makes the reconnect watcher in
            # gateway.run treat it as retryable and loop forever at the
            # backoff cap, re-instantiating the adapter (and its
            # ResponseStore sqlite connection) every retry (#38803: ~501
            # leaked connections / 1002 fds over 2.5 days until EMFILE took
            # the whole gateway down). Non-retryable drops it from the
            # reconnect queue — same treatment as the port-conflict guard
            # (api_server_port_in_use). The guard already logged the
            # specific rejection reason just above.
            self._set_fatal_error(
                "api_server_key_invalid",
                "API_SERVER_KEY was rejected by the startup guard (missing, "
                "placeholder/too short, or strength unverifiable — see the "
                "error logged above). Generate a strong secret (e.g. "
                "`openssl rand -hex 32`), set API_SERVER_KEY, then "
                "`/platform resume api_server`.",
                retryable=False,
            )
            return False

        try:
            # Capture profile path + directory generation before the listener
            # is reachable.  The snapshot is immutable for this adapter
            # lifetime; profile additions/replacements require a full gateway
            # restart and cannot inherit old in-memory or SQLite handles.
            self._freeze_api_profile_inventory()
            mws = [
                mw
                for mw in (
                    self._make_profile_prefix_middleware(),
                    cors_middleware,
                    body_limit_middleware,
                    security_headers_middleware,
                )
                if mw is not None
            ]
            self._app = web.Application(middlewares=mws, client_max_size=MAX_REQUEST_BYTES)
            assert self._app is not None
            # One process owns one profile and exposes only native routes.
            # Shared ingress must route to separate per-profile processes.
            for method, path, handler in self._http_route_table():
                self._app.router.add_route(method, path, handler)
            # Store the adapter after native routes are registered. Local Hermes-Relay
            # bootstrap shims use this key as a feature-detection hook; registering
            # native routes first lets those shims no-op instead of shadowing the
            # upstream session-control handlers.
            self._app["api_server_adapter"] = self
            if self.gateway_runner is not None:
                self._app["gateway_runner"] = self.gateway_runner

            # Start background sweep to clean up orphaned (unconsumed) run streams
            sweep_task = asyncio.create_task(self._sweep_orphaned_runs())
            try:
                self._background_tasks.add(sweep_task)
            except TypeError:
                pass
            if hasattr(sweep_task, "add_done_callback"):
                sweep_task.add_done_callback(self._background_tasks.discard)

            # Loud warning when a network-accessible API server runs against an
            # unsandboxed local terminal backend. The API server can drive the
            # agent's terminal/file tools as the host user; on a public bind
            # that is the exact surface the hermes-0day campaign abused to write
            # ~/.hermes/config.yaml and plant persistence. Sandboxing (Docker /
            # remote backend) contains the blast radius. Warn, don't refuse —
            # the operator may have an external firewall / strong key.
            if is_network_accessible(self._host):
                try:
                    from hermes_cli.config import load_config as _load_cfg
                    _backend = (
                        ((_load_cfg() or {}).get("terminal") or {}).get(
                            "backend", "local"
                        )
                    )
                except Exception:
                    _backend = "local"
                if str(_backend).lower() == "local":
                    logger.warning(
                        "[%s] API server is network-accessible (%s) AND the "
                        "terminal backend is 'local' (unsandboxed). Agent work "
                        "dispatched through this endpoint runs as the host user "
                        "with full terminal/file access. Strongly consider a "
                        "sandboxed backend (terminal.backend: docker) and "
                        "firewalling this port to trusted networks only.",
                        self.name, self._host,
                    )

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            # Bind directly instead of probing 127.0.0.1 first — the old
            # single-family pre-probe raced the real bind and reported a
            # TIME_WAIT socket as "in use" (#10297), failing gateway
            # restarts for up to ~60s.
            #
            # SO_REUSEADDR is platform-dependent (same rationale as the
            # webhook adapter, #65482):
            #   - macOS (BSD semantics): two sockets with SO_REUSEADDR can
            #     silently split traffic while both report success — disable.
            #   - Linux: SO_REUSEADDR only permits rebinding past TIME_WAIT
            #     (a second live listener needs SO_REUSEPORT, never set), so
            #     keep the default (enabled) for instant restart rebinds.
            self._site = web.TCPSite(
                self._runner,
                self._host,
                self._port,
                reuse_address=False if sys.platform == "darwin" else None,
            )
            try:
                await self._site.start()
            except OSError as exc:
                await self._runner.cleanup()
                self._runner = None
                self._site = None
                if getattr(exc, "errno", None) == errno.EADDRINUSE:
                    # A port conflict is a configuration error, not a
                    # transient blip — another process holds the port for
                    # its lifetime. A bare ``return False`` makes the
                    # reconnect watcher in gateway.run treat it as retryable
                    # and loop forever at the backoff cap (observed: 1568+
                    # retries over 5 days across multi-profile setups all
                    # defaulting to the same port, #52132), filling
                    # errors.log and leaking the adapter's ResponseStore
                    # fds each retry. Non-retryable drops it from the
                    # reconnect queue; the operator recovers with
                    # ``/platform resume api_server`` after changing the port.
                    self._set_fatal_error(
                        "api_server_port_in_use",
                        f"Port {self._port} already in use. Set "
                        f"platforms.api_server.port in config.yaml to a "
                        f"different value, then `/platform resume api_server`.",
                        retryable=False,
                    )
                logger.error(
                    "[%s] Could not bind %s:%d: %s. Set a different port in "
                    "config.yaml: platforms.api_server.port",
                    self.name, self._host, self._port, exc,
                )
                return False

            self._mark_connected()
            logger.info(
                "[%s] API server listening on http://%s:%d (model: %s)",
                self.name, self._host, self._port, self._model_name,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """Stop the aiohttp web server and release all owned resources.

        Closes the ResponseStore SQLite connection in addition to stopping
        the aiohttp web server. Without this, every adapter instance leaks
        2 file descriptors (the database file and its WAL sidecar) — the
        reconnect loop in ``gateway.run`` constructs a fresh adapter on
        every retry, so 2 fds/retry × 300s backoff cap ≈ 12 fds/hour, which
        exhausts the default 2560 fd limit after ~12h of failed reconnects
        and turns the whole gateway into a zombie
        (OSError: [Errno 24] Too many open files, #37011).
        """
        self._mark_disconnected()
        # Stop accepting new work before interrupting existing owners.
        if self._site:
            await self._site.stop()
            self._site = None
        # Close the adapter-specific SessionDB admission gate before any
        # cancellation-resistant aiohttp handler can enqueue another DB worker.
        self._seal_session_db_offload_admission()

        # Reconnect cleanup may receive an adapter whose construction failed
        # before the run/cleanup registries were initialized.  Treat absent
        # registries as empty so shutdown can still close the resources that
        # do exist (notably ResponseStore) without weakening the exact-cleanup
        # path for fully initialized adapters.
        active_run_agents = getattr(self, "_active_run_agents", {})
        api_active_agents = getattr(self, "_api_active_agents", {})
        active_run_tasks = getattr(self, "_active_run_tasks", {})
        api_cleanup_tasks = getattr(self, "_api_cleanup_tasks", set())
        api_cleanup_handles = getattr(self, "_api_cleanup_handles", {})
        agents_to_interrupt = {
            id(agent): agent
            for agent in (
                list(active_run_agents.values())
                + list(api_active_agents.values())
            )
        }
        for agent in agents_to_interrupt.values():
            try:
                agent.interrupt("API server shutting down")
            except Exception:
                pass
            self._cancel_api_agent_clarifications(agent)
            self._clear_api_approval_scope(
                str(getattr(agent, "session_id", "") or ""),
                approval_session_key=str(
                    getattr(agent, "_api_approval_session_key", "") or ""
                ),
                cancel_core=True,
                request_authority=getattr(
                    agent,
                    "_api_request_authority",
                    None,
                ),
            )

        async def _await_exact_api_cleanup() -> None:
            # Graceful shutdown is itself an authority boundary. Keep retrying
            # every retained exact binding and do not report a clean disconnect
            # merely because a short timeout elapsed. systemd's hard-kill path
            # is separately reconciled by the privileged writer's stop/start
            # hooks.
            while True:
                for handle in list(api_cleanup_handles.values()):
                    self._ensure_api_cleanup_retry(
                        handle,
                        cleanup_ref=None,
                        cleanup_state_callback=None,
                    )
                cleanup_tasks = {
                    task
                    for task in (
                        list(active_run_tasks.values())
                        + list(api_cleanup_tasks)
                    )
                    if task is not None and not task.done()
                }
                if not cleanup_tasks and not api_cleanup_handles:
                    return
                try:
                    _done, pending = await asyncio.wait(
                        cleanup_tasks,
                        timeout=30.0,
                    )
                    for task in _done:
                        try:
                            task.exception()
                        except (asyncio.CancelledError, Exception):
                            pass
                    if pending or api_cleanup_handles:
                        logger.warning(
                            "[%s] Waiting for exact API cleanup: %d task(s), "
                            "%d handle(s)",
                            self.name,
                            len(pending),
                            len(api_cleanup_handles),
                        )
                except asyncio.CancelledError:
                    logger.error(
                        "[%s] Graceful shutdown cancellation deferred until "
                        "exact API cleanup confirms",
                        self.name,
                    )
                    current = asyncio.current_task()
                    if current is not None and hasattr(current, "uncancel"):
                        current.uncancel()

        await _await_exact_api_cleanup()

        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        # aiohttp cleanup may let an already-admitted, cancellation-resistant
        # handler publish its run/cleanup owner after the first snapshot. Drain
        # the live registries again before releasing agents or closing DBs.
        await _await_exact_api_cleanup()

        # No worker owns these conversation agents now. Release every cached
        # provider client and cancel any clarification that raced shutdown.
        api_agent_cache = getattr(self, "_api_agent_cache", {})
        cache_lock = getattr(self, "_api_agent_cache_lock", threading.RLock())
        deferred_releases = getattr(self, "_api_deferred_agent_releases", set())
        with cache_lock:
            cached_agents = {
                id(entry.get("agent")): entry.get("agent")
                for entry in api_agent_cache.values()
                if entry.get("agent") is not None
            }
            api_agent_cache.clear()
            deferred_releases.clear()
        for agent in cached_agents.values():
            self._release_api_cached_agent(agent)
        clarifications_lock = getattr(
            self,
            "_api_clarifications_lock",
            threading.RLock(),
        )
        pending_clarifications = getattr(self, "_api_pending_clarifications", {})
        with clarifications_lock:
            clarify_scopes = {
                str(state.get("_scope", "") or "")
                for state in pending_clarifications.values()
            }
        for scope in clarify_scopes:
            self._clear_api_clarify_scope(scope)
        approvals_lock = getattr(
            self,
            "_api_approvals_lock",
            threading.RLock(),
        )
        pending_approvals = getattr(self, "_api_pending_approvals", {})
        with approvals_lock:
            approval_scopes = {
                (
                    str(state.get("session_id", "") or ""),
                    str(state.get("_approval_session_key", "") or ""),
                    state.get("_request_scope"),
                )
                for state in pending_approvals.values()
            }
        for session_id, approval_session_key, request_scope in approval_scopes:
            self._clear_api_approval_scope(
                session_id,
                approval_session_key=approval_session_key,
                cancel_core=True,
                request_authority=(
                    request_scope
                    if isinstance(request_scope, APIRequestScope)
                    else None
                ),
            )

        # Request-task cancellation can release an awaiting coroutine while its
        # real SQLite worker continues. Do not close the pool until every
        # operation admitted before the seal has exited. This wait is bounded
        # for non-process reconnect/disposal paths; timeout leaves handles open
        # rather than closing underneath a live worker.
        session_db_workers_idle = await self._seal_and_wait_session_db_offloads(
            timeout=5.0,
        )
        owned_session_dbs = {}
        if session_db_workers_idle:
            session_dbs_lock = getattr(
                self,
                "_session_dbs_lock",
                threading.RLock(),
            )
            with session_dbs_lock:
                session_dbs = getattr(self, "_session_dbs", {})
                owned_session_db_ids = set(
                    getattr(self, "_owned_session_db_ids", set())
                )
                owned_session_dbs = {
                    id(db): db
                    for db in session_dbs.values()
                    if db is not None and id(db) in owned_session_db_ids
                }
                injected_session_db = getattr(self, "_session_db", None)
                if (
                    injected_session_db is not None
                    and id(injected_session_db) in owned_session_db_ids
                ):
                    owned_session_dbs[id(injected_session_db)] = (
                        injected_session_db
                    )
                session_dbs.clear()
                owned_ids = getattr(self, "_owned_session_db_ids", None)
                if owned_ids is not None:
                    owned_ids.clear()
        else:
            logger.warning(
                "[%s] Leaving SessionDB pool open: admitted worker did not "
                "exit before the close barrier timeout",
                self.name,
            )
        for session_db in owned_session_dbs.values():
            try:
                session_db.close()
            except Exception:
                logger.debug(
                    "Failed to close SessionDB for %s",
                    self.name,
                    exc_info=True,
                )
        if not api_cleanup_handles:
            response_stores: Dict[int, ResponseStore] = {}
            default_response_store = getattr(self, "_response_store", None)
            if default_response_store is not None:
                response_stores[id(default_response_store)] = default_response_store
            response_stores_lock = getattr(
                self,
                "_response_stores_lock",
                threading.RLock(),
            )
            with response_stores_lock:
                profile_response_stores = getattr(
                    self,
                    "_response_stores_by_home",
                    {},
                )
                for response_store in profile_response_stores.values():
                    if response_store is not None:
                        response_stores[id(response_store)] = response_store
                profile_response_stores.clear()
            for response_store in response_stores.values():
                try:
                    response_store.close()
                except Exception:
                    logger.debug(
                        "Failed to close response store for %s",
                        self.name,
                        exc_info=True,
                    )
        elif api_cleanup_handles:
            logger.error(
                "[%s] ResponseStores retained because exact cleanup is unresolved",
                self.name,
            )
        self._app = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Not used — HTTP request/response cycle handles delivery directly.
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the API server."""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }
