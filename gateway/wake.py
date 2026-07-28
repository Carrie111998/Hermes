"""Wake an existing agent session from a background completion event.

Two delivery strategies, selected by the target adapter's
``supports_async_delivery`` capability flag:

* Push-capable adapters (telegram, discord, plugin platforms, ...): inject a
  synthetic ``MessageEvent(internal=True)`` through ``adapter.handle_message``
  — the pre-existing wake path, preserved exactly.

* Stateless request/response adapters (the API server,
  ``supports_async_delivery = False``): ``handle_message`` would run the wake
  turn under a ``build_session_key()``-derived key
  (``agent:main:api_server:group:<sid>``) that NEVER matches the raw
  ``X-Hermes-Session-Id`` key real gateway/HQ turns run under
  (``_bind_api_server_session``), so the wake lands in a parallel, invisible
  session. Instead we self-POST ``/v1/chat/completions`` on the in-pod API
  server with the raw session id in the ``X-Hermes-Session-Id`` header — the
  exact entry point real turns use — so the wake turn resumes the REAL
  session, with full history, and its result is visible the next time the
  client polls/reopens the conversation.

Failures RAISE (after bounded retries on transient errors) so callers can
rewind cursors / retry instead of silently losing the event.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A wake self-post runs the entire agent turn synchronously (stream=false);
# generous ceiling so long tool-using turns aren't killed mid-flight.
WAKE_TURN_TIMEOUT_SECONDS = 600.0

# Backoff delays between retries on transient failures (429 concurrency cap,
# connection errors). The API server has no per-session lock — concurrent
# turns on one session are last-writer-wins — but it DOES enforce a global
# max_concurrent_runs cap via HTTP 429, which is worth waiting out.
_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)

INTERNAL_WAKE_TOKEN_HEADER = "X-Hermes-Internal-Wake-Token"
INTERNAL_WAKE_TOKEN_TTL_SECONDS = 900.0
_WAKE_TOKEN_LOCK = threading.Lock()
_WAKE_TOKENS: dict[str, dict[str, Any]] = {}


class InternalWakeTokenError(RuntimeError):
    """Raised when a loopback wake capability is absent, stale, or mismatched."""


class DurableWakeDeferredError(RuntimeError):
    """A durable wake was not accepted and may be retried without budget loss.

    The outer durable-delivery carrier uses this typed signal to release its
    claim without consuming an attempt.  Only explicit API responses that
    prove either zero execution or an already-live owner are classified this
    way; transport failures remain ambiguous and keep the historical failure
    path.
    """

    def __init__(
        self,
        reason: str,
        *,
        retry_after: Optional[float] = None,
        detail: str = "",
    ) -> None:
        self.reason = str(reason or "").strip()
        self.retry_after = retry_after
        message = detail or f"durable wake deferred: {self.reason}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _WakeProfileAuthority:
    """One fully verified profile identity carried across a wake attempt."""

    profile: Optional[str]
    source_home: str
    canonical_home: str
    profile_generation: str


def _wake_text_digest(text: str) -> str:
    if not isinstance(text, str):
        raise InternalWakeTokenError("internal wake text must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _internal_wake_idempotency_key(
    *,
    producer_id: str,
    session_id: str,
    text: str,
    runtime_effect: Optional[dict] = None,
    execution_context: Optional[dict] = None,
    profile: Optional[str] = None,
    delivery_home: Optional[str | Path] = None,
    profile_generation: Optional[str] = None,
    durable_wake_required: bool = False,
    durable_delegation_id: str = "",
    durable_execution_owner: str = "",
) -> str:
    """Build the stable, profile-bound retry key for one durable wake event."""

    from agent.runtime_effects import normalize_optional_runtime_effect
    from gateway.api_execution_context import execution_context_digest

    normalized = normalize_optional_runtime_effect(runtime_effect)
    authority = _resolve_wake_profile_authority(
        profile=profile,
        delivery_home=delivery_home,
        profile_generation=profile_generation,
    )
    producer = str(producer_id or "").strip()
    target = str(session_id or "").strip()
    if not producer:
        raise InternalWakeTokenError(
            "internal wake requires a producer id"
        )
    if not target:
        raise InternalWakeTokenError(
            "internal wake requires a session id"
        )
    durable_id = str(durable_delegation_id or "").strip()
    if bool(durable_id) != bool(durable_wake_required):
        raise InternalWakeTokenError(
            "durable wake flag and delegation id must be supplied together"
        )
    if durable_id and not secrets.compare_digest(durable_id, producer):
        raise InternalWakeTokenError(
            "durable wake delegation id must match its producer"
        )
    execution_owner = str(durable_execution_owner or "").strip()
    if durable_wake_required and execution_owner != "api":
        raise InternalWakeTokenError(
            "durable non-push wake execution owner must be api"
        )
    if not durable_wake_required and execution_owner:
        raise InternalWakeTokenError(
            "non-durable wake cannot name a durable execution owner"
        )
    canonical = json.dumps(
        {
            "producer_id": producer,
            "profile": authority.profile or "default",
            "source_home": authority.source_home,
            "canonical_home": authority.canonical_home,
            "profile_generation": authority.profile_generation,
            "session_id": target,
            "text_sha256": _wake_text_digest(text),
            "runtime_effect": normalized,
            "durable_wake_required": bool(durable_wake_required),
            "durable_delegation_id": durable_id,
            "durable_execution_owner": execution_owner,
            "execution_context_sha256": execution_context_digest(
                execution_context
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"hermes-internal-wake-v1-{digest}"


def _prune_wake_tokens_locked(now: float) -> None:
    expired = [
        token
        for token, record in _WAKE_TOKENS.items()
        if float(record.get("expires_at", 0.0) or 0.0) <= now
    ]
    for token in expired:
        _WAKE_TOKENS.pop(token, None)


def mint_internal_wake_token(
    *,
    session_id: str,
    origin_session_id: Optional[str] = None,
    text: str,
    runtime_effect: Optional[dict] = None,
    execution_context: Optional[dict] = None,
    producer_id: str = "",
    profile: Optional[str] = None,
    delivery_home: Optional[str | Path] = None,
    profile_generation: Optional[str] = None,
    durable_wake_required: bool = False,
    durable_delegation_id: str = "",
    durable_execution_owner: str = "",
    now: Optional[float] = None,
    ttl_seconds: float = INTERNAL_WAKE_TOKEN_TTL_SECONDS,
) -> str:
    """Mint one process-local, one-use capability for an internal HTTP wake."""

    from agent.runtime_effects import normalize_optional_runtime_effect
    from gateway.api_execution_context import normalize_api_execution_context

    normalized = normalize_optional_runtime_effect(runtime_effect)
    normalized_context = normalize_api_execution_context(execution_context)
    authority = _resolve_wake_profile_authority(
        profile=profile,
        delivery_home=delivery_home,
        profile_generation=profile_generation,
    )
    session_id = str(session_id or "").strip()
    if not session_id:
        raise InternalWakeTokenError(
            "internal wake token requires a session id"
        )
    origin_session_id = str(origin_session_id or session_id).strip()
    if not origin_session_id:
        raise InternalWakeTokenError(
            "internal wake token requires an origin session id"
        )
    producer_id = str(producer_id or "").strip()
    if not producer_id:
        raise InternalWakeTokenError(
            "internal wake token requires a producer id"
        )
    durable_delegation_id = str(durable_delegation_id or "").strip()
    if bool(durable_delegation_id) != bool(durable_wake_required):
        raise InternalWakeTokenError(
            "durable wake flag and delegation id must be supplied together"
        )
    if durable_delegation_id and not secrets.compare_digest(
        durable_delegation_id,
        producer_id,
    ):
        raise InternalWakeTokenError(
            "durable wake delegation id must match its producer"
        )
    durable_execution_owner = str(
        durable_execution_owner or ""
    ).strip()
    if durable_wake_required and durable_execution_owner != "api":
        raise InternalWakeTokenError(
            "durable non-push wake execution owner must be api"
        )
    if not durable_wake_required and durable_execution_owner:
        raise InternalWakeTokenError(
            "non-durable wake cannot name a durable execution owner"
        )
    issued_at = time.time() if now is None else float(now)
    ttl = float(ttl_seconds)
    if ttl <= 0:
        raise InternalWakeTokenError(
            "internal wake token TTL must be positive"
        )
    token = secrets.token_urlsafe(32)
    record = {
        "profile": authority.profile or "default",
        "source_home": authority.source_home,
        "canonical_home": authority.canonical_home,
        "profile_generation": authority.profile_generation,
        "session_id": session_id,
        "origin_session_id": origin_session_id,
        "text_sha256": _wake_text_digest(text),
        "runtime_effect": normalized,
        "execution_context": normalized_context,
        "producer_id": producer_id,
        "durable_wake_required": bool(durable_wake_required),
        "durable_delegation_id": durable_delegation_id,
        "durable_execution_owner": durable_execution_owner,
        "idempotency_key": _internal_wake_idempotency_key(
            producer_id=producer_id,
            session_id=origin_session_id,
            text=text,
            runtime_effect=normalized,
            execution_context=normalized_context,
            profile=authority.profile,
            delivery_home=authority.source_home,
            profile_generation=authority.profile_generation,
            durable_wake_required=durable_wake_required,
            durable_delegation_id=durable_delegation_id,
            durable_execution_owner=durable_execution_owner,
        ),
        "expires_at": issued_at + ttl,
    }
    with _WAKE_TOKEN_LOCK:
        _prune_wake_tokens_locked(issued_at)
        _WAKE_TOKENS[token] = record
    return token


def consume_internal_wake_token(
    token: str,
    *,
    session_id: str,
    text: str,
    idempotency_key: str,
    gateway_session_key: str = "",
    profile: Optional[str] = None,
    source_home: Optional[str | Path] = None,
    canonical_home: Optional[str | Path] = None,
    profile_generation: Optional[str] = None,
    now: Optional[float] = None,
    return_envelope: bool = False,
) -> Optional[dict]:
    """Atomically consume and validate one process-local wake capability."""

    from agent.runtime_effects import normalize_optional_runtime_effect
    from gateway.api_execution_context import normalize_api_execution_context

    token = str(token or "").strip()
    authority = _resolve_wake_profile_authority(
        profile=profile,
        delivery_home=source_home or canonical_home,
        profile_generation=profile_generation,
    )
    if canonical_home is not None and not secrets.compare_digest(
        authority.canonical_home,
        str(Path(canonical_home).expanduser().resolve(strict=True)),
    ):
        raise InternalWakeTokenError(
            "internal wake request profile-home mismatch"
        )
    current_time = time.time() if now is None else float(now)
    with _WAKE_TOKEN_LOCK:
        record = _WAKE_TOKENS.pop(token, None)
        _prune_wake_tokens_locked(current_time)
    if record is None:
        raise InternalWakeTokenError(
            "internal wake token is missing, expired, or already consumed"
        )
    if float(record.get("expires_at", 0.0) or 0.0) <= current_time:
        raise InternalWakeTokenError("internal wake token has expired")
    if not secrets.compare_digest(
        str(record.get("profile") or "default"),
        authority.profile or "default",
    ):
        raise InternalWakeTokenError(
            "internal wake token profile mismatch"
        )
    if not secrets.compare_digest(
        str(record.get("source_home") or ""),
        authority.source_home,
    ):
        raise InternalWakeTokenError(
            "internal wake token profile-source mismatch"
        )
    if not secrets.compare_digest(
        str(record.get("canonical_home") or ""),
        authority.canonical_home,
    ):
        raise InternalWakeTokenError(
            "internal wake token profile-home mismatch"
        )
    if not secrets.compare_digest(
        str(record.get("profile_generation") or ""),
        authority.profile_generation,
    ):
        raise InternalWakeTokenError(
            "internal wake token profile-generation mismatch"
        )
    if not secrets.compare_digest(
        str(record.get("session_id") or ""),
        str(session_id or ""),
    ):
        raise InternalWakeTokenError(
            "internal wake token session mismatch"
        )
    if not secrets.compare_digest(
        str(record.get("text_sha256") or ""),
        _wake_text_digest(text),
    ):
        raise InternalWakeTokenError(
            "internal wake token text mismatch"
        )
    if not secrets.compare_digest(
        str(record.get("idempotency_key") or ""),
        str(idempotency_key or "").strip(),
    ):
        raise InternalWakeTokenError(
            "internal wake token idempotency mismatch"
        )
    normalized = normalize_optional_runtime_effect(record.get("runtime_effect"))
    execution_context = normalize_api_execution_context(
        record.get("execution_context")
    )
    expected_gateway_session_key = str(
        (execution_context or {}).get("gateway_session_key") or ""
    )
    if not secrets.compare_digest(
        expected_gateway_session_key,
        str(gateway_session_key or ""),
    ):
        raise InternalWakeTokenError(
            "internal wake token memory-session mismatch"
        )
    if return_envelope:
        return {
            "runtime_effect": normalized,
            "execution_context": execution_context,
            "origin_session_id": str(
                record.get("origin_session_id") or ""
            ),
            "producer_id": str(record.get("producer_id") or ""),
            "durable_wake_required": bool(
                record.get("durable_wake_required")
            ),
            "durable_delegation_id": str(
                record.get("durable_delegation_id") or ""
            ),
            "durable_execution_owner": str(
                record.get("durable_execution_owner") or ""
            ),
            "profile_identity": {
                "profile": str(record.get("profile") or "default"),
                "source_home": str(record.get("source_home") or ""),
                "canonical_home": str(record.get("canonical_home") or ""),
                "profile_generation": str(
                    record.get("profile_generation") or ""
                ),
            },
        }
    # Historical callers used the token API only for runtime effects.
    return normalized


def adapter_supports_push(adapter: Any) -> bool:
    """Whether this adapter can push a message to the user after a turn ends.

    Mirrors ``gateway.session_context.async_delivery_supported`` but reads the
    capability off the adapter class (``supports_async_delivery``) instead of
    the request-scoped contextvar — background watchers run outside any bound
    session context. Adapters that don't declare the flag are push-capable.
    """
    return bool(getattr(adapter, "supports_async_delivery", True))


def _normalize_wake_profile(
    profile: Optional[str],
    *,
    require_registered: bool = True,
) -> Optional[str]:
    """Return a URL-safe named profile, or ``None`` for the default route."""

    raw = str(profile or "").strip()
    if not raw or raw.casefold() == "default":
        return None
    from hermes_cli.profiles import (
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )

    normalized = normalize_profile_name(raw)
    validate_profile_name(normalized)
    if require_registered and not profile_exists(normalized):
        raise ValueError(f"Hermes profile does not exist: {normalized!r}")
    return normalized


def _resolve_wake_profile_authority(
    *,
    profile: Optional[str],
    delivery_home: Optional[str | Path],
    profile_generation: Optional[str],
) -> _WakeProfileAuthority:
    """Resolve one exact profile directory identity for a wake capability.

    A trusted durable event supplies both ``delivery_home`` and
    ``profile_generation``.  In that path the profile registry is never
    consulted again: delete/recreate or symlink retargeting cannot redirect
    the completion to a new tenant.  Legacy/direct callers capture the
    currently active profile identity once at mint/key construction.
    """

    explicit_home = str(delivery_home or "").strip()
    explicit_generation = str(profile_generation or "").strip()
    if bool(explicit_home) != bool(explicit_generation):
        raise InternalWakeTokenError(
            "internal wake profile home and generation must be supplied together"
        )
    try:
        wake_profile = _normalize_wake_profile(
            profile,
            require_registered=not explicit_home,
        )
        profile_label = wake_profile or "default"
        if explicit_home:
            from gateway.api_request_scope import (
                APIProfileGenerationError,
                capture_api_profile_identity,
            )

            identity = capture_api_profile_identity(
                profile_label,
                Path(explicit_home),
                initialize_marker=False,
            )
            if not secrets.compare_digest(
                identity.profile_generation,
                explicit_generation,
            ):
                raise APIProfileGenerationError(
                    "internal wake profile generation changed"
                )
        elif wake_profile:
            from hermes_cli.profiles import get_profile_dir

            home = Path(get_profile_dir(wake_profile))
        else:
            from hermes_constants import get_hermes_home

            home = Path(get_hermes_home())

        if not explicit_home:
            from gateway.api_request_scope import capture_api_profile_identity

            identity = capture_api_profile_identity(profile_label, home)
    except InternalWakeTokenError:
        raise
    except Exception as exc:
        raise InternalWakeTokenError(
            "internal wake profile authority is unavailable"
        ) from exc
    return _WakeProfileAuthority(
        profile=wake_profile,
        source_home=identity.source_home,
        canonical_home=identity.canonical_home,
        profile_generation=identity.profile_generation,
    )


def _verify_wake_profile_authority(
    authority: _WakeProfileAuthority,
) -> None:
    """Reverify the original source path immediately before a side effect."""

    from gateway.api_request_scope import (
        APIProfileIdentity,
        verify_api_profile_identity,
    )

    verify_api_profile_identity(
        APIProfileIdentity(
            profile=authority.profile or "default",
            source_home=authority.source_home,
            canonical_home=authority.canonical_home,
            profile_generation=authority.profile_generation,
        )
    )


def _legacy_adapter_wake_profile_authority(
    adapter: Any,
    *,
    profile: Optional[str],
) -> _WakeProfileAuthority:
    """Read legacy/direct wake authority from one frozen API listener only.

    Older non-push callers do not carry the durable event's explicit home and
    generation.  They may continue only when the target adapter can prove one
    exact listener-lifetime profile identity.  Never rebuild this authority
    from the mutable profile registry or the process's current home.
    """

    try:
        freeze_inventory = getattr(
            adapter,
            "_freeze_api_profile_inventory",
            None,
        )
        if callable(freeze_inventory):
            inventory = freeze_inventory()
        else:
            inventory = getattr(adapter, "_api_profile_inventory", None)

        from gateway.api_request_scope import validate_api_profile_inventory

        inventory = validate_api_profile_inventory(inventory)
        if len(inventory) != 1:
            raise InternalWakeTokenError(
                "legacy internal wake requires exactly one frozen API "
                "profile identity"
            )
        identity = inventory[0]
        identity_profile = _normalize_wake_profile(
            identity.profile,
            require_registered=False,
        )
        raw_requested_profile = str(profile or "").strip()
        if raw_requested_profile:
            requested_profile = _normalize_wake_profile(
                raw_requested_profile,
                require_registered=False,
            )
            if (requested_profile or "default") != (
                identity_profile or "default"
            ):
                raise InternalWakeTokenError(
                    "legacy internal wake profile does not match the "
                    "adapter's frozen profile identity"
                )
        authority = _WakeProfileAuthority(
            profile=identity_profile,
            source_home=identity.source_home,
            canonical_home=identity.canonical_home,
            profile_generation=identity.profile_generation,
        )
        _verify_wake_profile_authority(authority)
        return authority
    except InternalWakeTokenError:
        raise
    except Exception as exc:
        raise InternalWakeTokenError(
            "legacy internal wake has no consistent frozen API profile "
            "authority"
        ) from exc


async def deliver_wake(
    adapter: Any,
    *,
    text: str,
    session_id: str = "",
    source: Any = None,
    runtime_effect: Optional[dict] = None,
    producer_id: str = "",
    profile: Optional[str] = None,
    execution_context: Optional[dict] = None,
    delivery_home: Optional[str | Path] = None,
    profile_generation: Optional[str] = None,
    durable_wake_required: bool = False,
    durable_delegation_id: str = "",
    durable_execution_owner: str = "",
) -> None:
    """Deliver a wake turn to the session behind ``adapter``.

    ``session_id`` is the RAW session id (the ``X-Hermes-Session-Id`` value /
    ``state.db`` key) — required for non-push adapters. ``source`` is the
    ``SessionSource`` used to build the synthetic event — required for
    push-capable adapters.

    Raises on failure (bad arguments, exhausted retries, HTTP error) so the
    caller can rewind/retry instead of treating the wake as delivered.
    """
    from gateway.api_execution_context import normalize_api_execution_context

    execution_context = normalize_api_execution_context(execution_context)
    if adapter_supports_push(adapter):
        # Push delivery uses the already-selected adapter/source pair.  The
        # optional label is syntax-only metadata here; consulting the mutable
        # profile registry could redirect or reject an otherwise frozen wake.
        _normalize_wake_profile(
            profile,
            require_registered=False,
        )
        if source is None:
            raise ValueError(
                "deliver_wake: push-capable adapter requires a SessionSource"
            )
        from gateway.platforms.base import MessageEvent, MessageType

        from agent.runtime_effects import normalize_optional_runtime_effect

        normalized_effect = normalize_optional_runtime_effect(runtime_effect)
        metadata = {}
        if normalized_effect is not None:
            metadata["runtime_effect"] = normalized_effect
        synth_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
            metadata=metadata,
        )
        await adapter.handle_message(synth_event)
        return

    if not session_id:
        raise ValueError(
            "deliver_wake: non-push adapter (supports_async_delivery=False) "
            "requires the raw session id to self-post the wake turn"
        )
    explicit_home = str(delivery_home or "").strip()
    explicit_generation = str(profile_generation or "").strip()
    if explicit_home or explicit_generation:
        wake_profile = _normalize_wake_profile(
            profile,
            require_registered=False,
        )
        authority = _resolve_wake_profile_authority(
            profile=wake_profile,
            delivery_home=delivery_home,
            profile_generation=profile_generation,
        )
    else:
        authority = _legacy_adapter_wake_profile_authority(
            adapter,
            profile=profile,
        )

    async def _deliver_non_push() -> None:
        normalized_effect = None
        if runtime_effect is not None:
            from agent.runtime_effects import normalize_optional_runtime_effect

            normalized_effect = normalize_optional_runtime_effect(runtime_effect)
        # Session continuity is independent of isolated-workspace authority.
        # Every non-push wake must follow a compression rotation to its live
        # tip and reject explicit conversation boundaries. The resolver keeps
        # the additional root-authority comparison conditional on an effect.
        resolved_session_id = await _resolve_wake_session_id(
            adapter,
            session_id=session_id,
            runtime_effect=normalized_effect,
            profile_authority=authority,
        )
        await _self_post_chat_completion(
            adapter,
            text=text,
            session_id=resolved_session_id,
            origin_session_id=session_id,
            runtime_effect=normalized_effect,
            producer_id=(
                str(producer_id or "").strip()
                or f"wake-{secrets.token_hex(16)}"
            ),
            profile=authority.profile,
            execution_context=execution_context,
            delivery_home=authority.source_home,
            profile_generation=authority.profile_generation,
            durable_wake_required=durable_wake_required,
            durable_delegation_id=durable_delegation_id,
            durable_execution_owner=durable_execution_owner,
        )

    from gateway.run import _profile_runtime_scope

    with _profile_runtime_scope(Path(authority.canonical_home)):
        await _deliver_non_push()


async def _resolve_wake_session_id(
    adapter: Any,
    *,
    session_id: str,
    runtime_effect: Optional[dict],
    profile_authority: Optional[_WakeProfileAuthority] = None,
) -> str:
    """Resolve only compression continuations; explicit new roots fail closed."""

    if profile_authority is not None:
        _verify_wake_profile_authority(profile_authority)
    db = await adapter._ensure_session_db_async()
    if db is None:
        raise RuntimeError("wake self-post session database is unavailable")
    current_id = str(session_id or "").strip()
    row = await asyncio.to_thread(db.get_session, current_id)
    if row is None:
        raise RuntimeError(
            f"wake self-post target session does not exist: {current_id}"
        )
    if row.get("ended_at"):
        if row.get("end_reason") != "compression":
            raise RuntimeError(
                "wake self-post target ended at an explicit conversation "
                "boundary"
            )
        tip_id = await asyncio.to_thread(
            db.get_compression_tip,
            current_id,
        )
        if not tip_id or str(tip_id) == current_id:
            raise RuntimeError(
                "wake self-post compression continuation is not available"
            )
        tip = await asyncio.to_thread(db.get_session, str(tip_id))
        if tip is None or tip.get("ended_at"):
            raise RuntimeError(
                "wake self-post compression continuation is not live"
            )
        current_id = str(tip_id)

    if runtime_effect is not None:
        authority = await asyncio.to_thread(
            db.get_conversation_root,
            current_id,
        )
        if str(authority or "") != str(
            runtime_effect.get("workspace_lease_authority") or ""
        ):
            raise RuntimeError(
                "wake self-post runtime effect authority does not match "
                "the target conversation root"
            )
    return current_id


async def _self_post_chat_completion(
    adapter: Any,
    *,
    text: str,
    session_id: str,
    origin_session_id: Optional[str] = None,
    runtime_effect: Optional[dict] = None,
    producer_id: str = "",
    profile: Optional[str] = None,
    execution_context: Optional[dict] = None,
    delivery_home: Optional[str | Path] = None,
    profile_generation: Optional[str] = None,
    durable_wake_required: bool = False,
    durable_delegation_id: str = "",
    durable_execution_owner: str = "",
) -> None:
    """POST the wake text to the in-pod API server as a normal session turn.

    Uses the adapter's own bind host/port/key (``ApiServerAdapter.__init__``).
    Session continuation via ``X-Hermes-Session-Id`` is 403-gated on
    ``API_SERVER_KEY`` being configured, so a missing key is a hard error —
    raise loudly rather than run the wake in a fresh fingerprint-derived
    session nobody is looking at.
    """
    import aiohttp

    host = str(getattr(adapter, "_host", "") or "127.0.0.1")
    if host in ("0.0.0.0", "::", "*"):
        # Wildcard bind address — connect over loopback.
        host = "127.0.0.1"
    port = int(getattr(adapter, "_port", 0) or 8642)
    api_key = str(getattr(adapter, "_api_key", "") or "")
    if not api_key:
        raise RuntimeError(
            "wake self-post requires API_SERVER_KEY: session continuation via "
            "X-Hermes-Session-Id is rejected (403) on an unauthenticated API "
            "server, so the wake cannot reach the target session"
        )

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6 literal
    authority = _resolve_wake_profile_authority(
        profile=profile,
        delivery_home=delivery_home,
        profile_generation=profile_generation,
    )
    # One gateway process owns one frozen profile. The profile remains part of
    # the wake capability/idempotency authority, but it is not an HTTP routing
    # prefix; shared ingress must select the correct isolated process first.
    url = f"http://{host}:{port}/v1/chat/completions"
    from agent.runtime_effects import normalize_optional_runtime_effect
    from gateway.api_execution_context import normalize_api_execution_context

    runtime_effect = normalize_optional_runtime_effect(runtime_effect)
    execution_context = normalize_api_execution_context(execution_context)
    origin_session_id = str(origin_session_id or session_id).strip()
    producer_id = str(producer_id or "").strip()
    if not producer_id:
        raise InternalWakeTokenError(
            "wake self-post requires a stable producer id"
        )
    request_model = str(
        (execution_context or {}).get("route_alias")
        or (execution_context or {}).get("request_model")
        or getattr(adapter, "_model_name", "")
        or "hermes-agent"
    )
    payload = {
        "model": request_model,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }
    request_provider = str(
        (execution_context or {}).get("request_provider") or ""
    )
    if request_provider:
        payload["provider"] = request_provider
    model_options = (execution_context or {}).get("model_options")
    if isinstance(model_options, dict) and model_options:
        payload["model_options"] = dict(model_options)
    idempotency_key = _internal_wake_idempotency_key(
        producer_id=producer_id,
        session_id=origin_session_id,
        text=text,
        runtime_effect=runtime_effect,
        execution_context=execution_context,
        profile=authority.profile,
        delivery_home=authority.source_home,
        profile_generation=authority.profile_generation,
        durable_wake_required=durable_wake_required,
        durable_delegation_id=durable_delegation_id,
        durable_execution_owner=durable_execution_owner,
    )

    last_err: Optional[BaseException] = None
    attempts = 1 + len(_RETRY_DELAYS_SECONDS)
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_RETRY_DELAYS_SECONDS[attempt - 1])
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "X-Hermes-Session-Id": session_id,
                "Idempotency-Key": idempotency_key,
            }
            stable_memory_key = str(
                (execution_context or {}).get("gateway_session_key") or ""
            )
            if stable_memory_key:
                headers["X-Hermes-Session-Key"] = stable_memory_key
            # Mint per attempt: a request that reached the server consumes its
            # token even when the HTTP response later fails.  This applies to
            # ordinary completions too, not only runtime-effect wakes.
            _verify_wake_profile_authority(authority)
            headers[INTERNAL_WAKE_TOKEN_HEADER] = (
                mint_internal_wake_token(
                    session_id=session_id,
                    origin_session_id=origin_session_id,
                    text=text,
                    runtime_effect=runtime_effect,
                    execution_context=execution_context,
                    producer_id=producer_id,
                    profile=authority.profile,
                    delivery_home=authority.source_home,
                    profile_generation=authority.profile_generation,
                    durable_wake_required=durable_wake_required,
                    durable_delegation_id=durable_delegation_id,
                    durable_execution_owner=durable_execution_owner,
                )
            )
            timeout = aiohttp.ClientTimeout(total=WAKE_TURN_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 429:
                        body_text = (await resp.text())[:300]
                        response_code = ""
                        try:
                            response_body = json.loads(body_text)
                            error_body = (
                                response_body.get("error")
                                if isinstance(response_body, dict)
                                else None
                            )
                            if isinstance(error_body, dict):
                                response_code = str(
                                    error_body.get("code") or ""
                                ).strip()
                        except (TypeError, ValueError):
                            pass
                        deferred_reason = {
                            "capacity_exceeded": "capacity_before_claim",
                            "rate_limit_exceeded": "capacity_before_claim",
                            "durable_wake_in_progress": (
                                "live_owner_in_progress"
                            ),
                            "durable_wake_claim_unavailable": (
                                "claim_unavailable"
                            ),
                            "durable_wake_settlement_unavailable": (
                                "settlement_unavailable"
                            ),
                        }.get(response_code)
                        retry_after: Optional[float] = None
                        try:
                            raw_retry_after = resp.headers.get("Retry-After")
                            if raw_retry_after is not None:
                                parsed_retry_after = float(raw_retry_after)
                                if parsed_retry_after >= 0:
                                    retry_after = parsed_retry_after
                        except (TypeError, ValueError):
                            pass
                        detail = (
                            f"wake self-post got HTTP 429"
                            f"{f' ({response_code})' if response_code else ''} "
                            f"for session {session_id}"
                        )
                        if deferred_reason:
                            last_err = DurableWakeDeferredError(
                                deferred_reason,
                                retry_after=retry_after,
                                detail=detail,
                            )
                        else:
                            last_err = RuntimeError(detail)
                        logger.warning(
                            "%s; attempt %d/%d", last_err, attempt + 1, attempts
                        )
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        # Non-transient (auth/validation) — fail immediately.
                        raise RuntimeError(
                            f"wake self-post failed for session {session_id}: "
                            f"HTTP {resp.status}: {body}"
                        )
                    await resp.read()
                    logger.info(
                        "wake self-post delivered for session %s (attempt %d)",
                        session_id,
                        attempt + 1,
                    )
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            last_err = exc
            logger.warning(
                "wake self-post transient failure for session %s "
                "(attempt %d/%d): %s",
                session_id,
                attempt + 1,
                attempts,
                exc,
            )
            continue
    if isinstance(last_err, DurableWakeDeferredError):
        raise last_err
    raise RuntimeError(
        f"wake self-post gave up for session {session_id} after "
        f"{attempts} attempts: {last_err}"
    ) from last_err
