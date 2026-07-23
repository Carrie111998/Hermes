"""WS-upgrade auth credentials for gated mode.

Browsers cannot set ``Authorization`` on a WebSocket upgrade. In loopback
mode the legacy ``?token=<_SESSION_TOKEN>`` query param works because the
token is injected into the SPA bundle. In gated mode there is no injected
token — so this module provides two credential shapes:

1. **Single-use browser tickets** (``mint_ticket`` / ``consume_ticket``).
   The SPA gets a fresh ticket via the authenticated REST endpoint
   ``POST /api/auth/ws-ticket`` and passes it as ``?ticket=`` on the WS
   upgrade. Single-use, TTL = 30 seconds — a leaked ticket is uninteresting.

2. **A process-lifetime internal credential** (``internal_ws_credential`` /
   ``consume_internal_credential``). This authenticates *server-spawned*
   WS clients — specifically the embedded-TUI PTY child, which attaches to
   ``/api/ws`` (JSON-RPC gateway) and ``/api/pub`` (event sidecar) over
   loopback. A single-use 30s ticket is the wrong shape for that link: the
   child reads its attach URL once at startup and **reuses it on every
   reconnect**, and on a slow cold boot the child may not dial within 30s.
   The internal credential is minted once per process, never expires, is
   multi-use, and — critically — is **never injected into any HTML/SPA**:
   it only ever leaves the process via the spawned child's environment, so
   browser-side XSS cannot read it. A leaked internal credential grants no
   more than a single-use ticket already does (the same two internal WS
   endpoints), and the same Origin / host guards still apply downstream.

3. **Single-use phone-handoff tickets** (``mint_handoff_ticket`` /
   ``consume_handoff_ticket``). Separate store + ``hnd_`` prefix so a handoff
   ticket can never be consumed as a WS ticket (or vice-versa). TTL = 120 s.
   Payload binds a chat ``session_id`` + ``profile`` and mints a
   resume-scoped browser session cookie on first consume. Never confers
   ``API_SERVER_KEY`` / superuser / ``*`` scope.

In-memory; the dashboard is a single process so no distributed coordination
is needed. The module exposes a small functional API rather than a class so
tests can patch ``time.time`` cleanly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.scopes import (
    EXACT_HANDOFF_SCOPES,
    exact_handoff_scopes_or_none,
)

#: Time-to-live for newly-minted WS tickets in seconds. 30 s is long enough
#: that the SPA can call ``getWsTicket()`` and immediately open the WS,
#: short enough that a leaked ticket is uninteresting.
TTL_SECONDS = 30

#: Handoff (QR phone-path) ticket TTL. Longer than WS tickets so the
#: operator has time to open the camera and scan; still short enough that
#: a shoulder-surfed QR dies quickly.
HANDOFF_TTL_SECONDS = 120

#: Prefix that marks a handoff ticket. Distinct namespace from WS tickets
#: (raw ``token_urlsafe`` with no prefix) so the two kinds cannot cross-use
#: even if a caller confuses the query param names.
HANDOFF_TICKET_PREFIX = "hnd_"

#: Server-derived channel name for the structured event bridge of a
#: resume-scoped phone session. It is deterministic for the bound identity
#: and process, not client-selected entropy.
RESUME_EVENT_CHANNEL_PREFIX = "resume-"

#: Lifetime of the browser session cookie minted when a handoff ticket is
#: consumed. Longer than the ticket itself — the QR dies in 120 s, but the
#: phone session should last a normal browsing window. No refresh token is
#: issued for handoff sessions, so this is a hard upper bound.
HANDOFF_SESSION_TTL_SECONDS = 45 * 60  # F-04: shortened from 4h

#: Least-privilege scope attached to every handoff-minted browser session.
#: Explicitly excludes superuser / API_SERVER_KEY / wildcard / admin power.
#: Canonical value lives in scopes.EXACT_HANDOFF_SCOPES — single source.
HANDOFF_SCOPES: tuple[str, ...] = EXACT_HANDOFF_SCOPES

_lock = threading.Lock()
_tickets: Dict[str, Tuple[int, Dict[str, Any]]] = {}  # ticket -> (expires_at, info)
_handoff_tickets: Dict[str, Tuple[int, Dict[str, Any]]] = {}

#: Process-local HMAC key for handoff session access tokens (cookie ATs).
#: Lazily minted; never leaves the process.
_handoff_signing_key: Optional[bytes] = None

#: The process-lifetime internal credential (see module docstring). Lazily
#: minted on first ``internal_ws_credential()`` call and stable for the life
#: of the process. Guarded by ``_lock``.
_internal_credential: Optional[str] = None

#: Identity recorded for connections that authenticate via the internal
#: credential, so audit logs distinguish them from browser-initiated tickets.
INTERNAL_USER_ID = "server-internal"
INTERNAL_PROVIDER = "server-internal"

_SIG_LEN = hashlib.sha256().digest_size


class TicketInvalid(Exception):
    """Ticket missing, expired, or already consumed."""


def resume_event_channel(*, user_id: str, session_id: str, profile: str) -> str:
    """Derive the only structured-event channel for a resume ticket."""
    payload = json.dumps(
        {
            "profile": str(profile or ""),
            "session_id": str(session_id or ""),
            "user_id": str(user_id or ""),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    with _lock:
        key = _handoff_key_locked()
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"{RESUME_EVENT_CHANNEL_PREFIX}{encoded}"


def mint_ticket(
    *,
    user_id: str,
    provider: str,
    scopes: tuple[str, ...] | list[str] | None = None,
    bound_session_id: str = "",
    bound_profile: str = "",
    allowed_endpoints: tuple[str, ...] | list[str] | frozenset[str] | None = None,
) -> str:
    """Generate a one-shot ticket bound to this user identity.

    The returned token is base64url, 43 bytes of entropy (32-byte random
    seed). Stash returns the ``info`` dict to the caller on consume so the
    WS handler can carry the identity forward into its session log.

    Full-dashboard sessions omit ``scopes`` / ``allowed_endpoints``
    (unrestricted WS paths). Resume sessions pass both so consume can
    default-deny admin WS endpoints.
    """
    ticket = secrets.token_urlsafe(32)
    # Defence in depth: never mint a WS ticket that collides with the
    # handoff prefix (astronomically unlikely with token_urlsafe, but
    # cheap to guard).
    while ticket.startswith(HANDOFF_TICKET_PREFIX):
        ticket = secrets.token_urlsafe(32)
    scope_list = [str(s) for s in (scopes or ()) if s]
    if allowed_endpoints is None:
        endpoints = None
    else:
        endpoints = sorted({str(e) for e in allowed_endpoints if e})
    event_channel = ""
    if endpoints is not None:
        event_channel = resume_event_channel(
            user_id=user_id,
            session_id=str(bound_session_id or ""),
            profile=str(bound_profile or ""),
        )
    info = {
        "user_id": user_id,
        "provider": provider,
        "minted_at": int(time.time()),
        "kind": "ws",
        "scopes": scope_list,
        "bound_session_id": str(bound_session_id or ""),
        "bound_profile": str(bound_profile or ""),
        "allowed_endpoints": endpoints,
        "event_channel": event_channel,
    }
    with _lock:
        _tickets[ticket] = (int(time.time()) + TTL_SECONDS, info)
        _gc_expired_locked()
    return ticket


def consume_ticket(ticket: str) -> Dict[str, Any]:
    """Validate and consume. Raises :class:`TicketInvalid` on missing/expired/used.

    Single-use semantics: a successful consume immediately removes the
    ticket from the store, so a second call with the same value raises
    ``TicketInvalid("unknown ticket: …")``.

    Handoff tickets (``hnd_…`` prefix / separate store) are never accepted
    here — they must go through :func:`consume_handoff_ticket`.
    """
    if ticket and ticket.startswith(HANDOFF_TICKET_PREFIX):
        truncated = (ticket[:8] + "…") if len(ticket) > 8 else ticket
        raise TicketInvalid(f"handoff ticket not valid as ws ticket: {truncated}")

    now = int(time.time())
    with _lock:
        entry = _tickets.pop(ticket, None)
        if entry is None:
            # Truncate ticket value in the error so misuse never logs the
            # secret in full.
            truncated = (ticket[:8] + "…") if ticket else "<empty>"
            raise TicketInvalid(f"unknown ticket: {truncated}")
        expires_at, info = entry
        if expires_at < now:
            raise TicketInvalid("expired")
        return info


def mint_handoff_ticket(
    *,
    session_id: str,
    profile: str = "",
    user_id: str,
    email: str = "",
    display_name: str = "",
    org_id: str = "",
    provider: str,
) -> str:
    """Mint a single-use phone-handoff ticket bound to a chat session.

    Payload (server-side only; client receives the opaque ticket string):
    ``{session_id, profile, user_id, provider, scopes=("resume",), access_token}``.

    The embedded ``access_token`` is a process-local HMAC-signed handoff
    session token used to set the browser cookie on first consume. It
    carries only :data:`HANDOFF_SCOPES` — never superuser / ``*`` /
    ``API_SERVER_KEY``.
    """
    if not session_id or not str(session_id).strip():
        raise ValueError("session_id is required")
    if not user_id:
        raise ValueError("user_id is required")
    if not provider:
        raise ValueError("provider is required")

    now = int(time.time())
    session_exp = now + HANDOFF_SESSION_TTL_SECONDS
    access_token = _sign_handoff_session(
        {
            "sub": user_id,
            "email": email or "",
            "name": display_name or "",
            "org_id": org_id or "",
            "provider": provider,
            "session_id": str(session_id).strip(),
            "profile": profile or "",
            "scopes": list(HANDOFF_SCOPES),
            "kind": "handoff",
            "exp": session_exp,
        }
    )
    # Hard invariant at mint time — scopes constant must stay exact resume.
    if exact_handoff_scopes_or_none(HANDOFF_SCOPES) != EXACT_HANDOFF_SCOPES:
        raise RuntimeError("handoff scopes must be exactly ('resume',)")

    ticket = HANDOFF_TICKET_PREFIX + secrets.token_urlsafe(32)
    info: Dict[str, Any] = {
        "kind": "handoff",
        "session_id": str(session_id).strip(),
        "profile": profile or "",
        "user_id": user_id,
        "email": email or "",
        "display_name": display_name or "",
        "org_id": org_id or "",
        "provider": provider,
        "access_token": access_token,
        "access_token_expires_at": session_exp,
        "scopes": list(HANDOFF_SCOPES),
        "minted_at": now,
    }
    with _lock:
        _handoff_tickets[ticket] = (now + HANDOFF_TTL_SECONDS, info)
        _gc_handoff_expired_locked()
    return ticket


def consume_handoff_ticket(ticket: str) -> Dict[str, Any]:
    """Validate and consume a handoff ticket. Single-use; raises on failure.

    WS tickets (no ``hnd_`` prefix / other store) are never accepted here.
    """
    if not ticket or not ticket.startswith(HANDOFF_TICKET_PREFIX):
        truncated = (ticket[:8] + "…") if ticket else "<empty>"
        raise TicketInvalid(f"ws ticket not valid as handoff ticket: {truncated}")

    now = int(time.time())
    with _lock:
        entry = _handoff_tickets.pop(ticket, None)
        if entry is None:
            truncated = ticket[:8] + "…"
            raise TicketInvalid(f"unknown ticket: {truncated}")
        expires_at, info = entry
        if expires_at < now:
            raise TicketInvalid("expired")

    scopes = tuple(info.get("scopes") or ())
    if exact_handoff_scopes_or_none(scopes) is None:
        # Fail closed — never hand out non-exact resume handoff sessions.
        raise TicketInvalid("forbidden handoff scope")
    return info


def verify_handoff_session_token(access_token: str) -> Optional[Session]:
    """Verify a handoff-minted cookie access token.

    Returns a :class:`Session` with ``scopes=("resume",)`` on success, or
    ``None`` if the token is not a handoff session token (so the caller can
    fall through to normal provider verification). Expired / tampered
    handoff tokens also return ``None``.
    """
    payload = _unsign_handoff_session(access_token)
    if payload is None:
        return None
    if payload.get("kind") != "handoff":
        return None
    exp = int(payload.get("exp") or 0)
    if exp <= int(time.time()):
        return None
    scopes = tuple(payload.get("scopes") or ())
    exact = exact_handoff_scopes_or_none(scopes)
    if exact is None:
        return None
    scopes = exact
    return Session(
        user_id=str(payload.get("sub") or ""),
        email=str(payload.get("email") or ""),
        display_name=str(payload.get("name") or ""),
        org_id=str(payload.get("org_id") or ""),
        provider=str(payload.get("provider") or "handoff"),
        expires_at=exp,
        access_token=access_token,
        refresh_token="",  # handoff sessions never get a refresh token
        scopes=scopes,
        bound_session_id=str(payload.get("session_id") or ""),
        bound_profile=str(payload.get("profile") or ""),
    )


def _handoff_key_locked() -> bytes:
    """Return the process-local handoff HMAC key, minting once. Holds ``_lock``."""
    global _handoff_signing_key
    if _handoff_signing_key is None:
        _handoff_signing_key = secrets.token_bytes(32)
    return _handoff_signing_key


def _sign_handoff_session(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    with _lock:
        key = _handoff_key_locked()
    sig = hmac.new(key, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode()


def _unsign_handoff_session(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        with _lock:
            key = _handoff_signing_key
        if key is None:
            return None
        expected = hmac.new(key, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(raw)
    except Exception:
        return None


def _gc_expired_locked() -> None:
    """Drop expired WS tickets. Caller must hold ``_lock``."""
    now = int(time.time())
    expired = [t for t, (exp, _) in _tickets.items() if exp < now]
    for t in expired:
        _tickets.pop(t, None)


def _gc_handoff_expired_locked() -> None:
    """Drop expired handoff tickets. Caller must hold ``_lock``."""
    now = int(time.time())
    expired = [t for t, (exp, _) in _handoff_tickets.items() if exp < now]
    for t in expired:
        _handoff_tickets.pop(t, None)


def internal_ws_credential() -> str:
    """Return the process-lifetime internal WS credential, minting it once.

    Used by the server to authenticate WS clients it spawns itself (the
    embedded-TUI PTY child). The value is stable for the life of the process,
    multi-use, and never expires — so a server-spawned child can reconnect
    its ``/api/ws`` / ``/api/pub`` sockets indefinitely without re-minting.

    The credential is never injected into the SPA HTML or returned over any
    REST endpoint; it is only ever passed to a child process via its
    environment. See the module docstring for the threat-model rationale.
    """
    global _internal_credential
    with _lock:
        if _internal_credential is None:
            _internal_credential = secrets.token_urlsafe(32)
        return _internal_credential


def consume_internal_credential(value: str) -> Dict[str, Any]:
    """Validate an internal credential. Raises :class:`TicketInvalid` on mismatch.

    Unlike :func:`consume_ticket` this is **not** single-use — the value is
    not removed on success, so a server-spawned child can present it on every
    (re)connect. Returns the fixed server-internal identity ``info`` dict
    (``{user_id, provider}``), mirroring the ``info`` shape ``consume_ticket``
    returns, so a caller that wants to record the connecting identity can; the
    current ``_ws_auth_ok`` caller validates for the boolean outcome only and
    discards the dict.

    A constant-time compare against the (lazily-minted) credential avoids
    leaking length / prefix information on mismatch. If no internal
    credential has been minted yet, any value is rejected.
    """
    with _lock:
        expected = _internal_credential
    if not value or expected is None:
        raise TicketInvalid("no internal credential")
    if not secrets.compare_digest(value.encode(), expected.encode()):
        raise TicketInvalid("internal credential mismatch")
    return {
        "user_id": INTERNAL_USER_ID,
        "provider": INTERNAL_PROVIDER,
    }


def _reset_for_tests() -> None:
    """Test-only: drop all tickets, handoff tickets, and the internal credential."""
    global _internal_credential, _handoff_signing_key
    with _lock:
        _tickets.clear()
        _handoff_tickets.clear()
        _internal_credential = None
        _handoff_signing_key = None
