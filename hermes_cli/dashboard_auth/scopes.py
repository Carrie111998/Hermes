"""Resume-scoped dashboard session authorization (default-deny).

Handoff/resume sessions carry a non-empty ``scopes`` tuple (today: ``(\"resume\",)``).
Full dashboard sessions keep empty scopes and retain unrestricted access.

Rules:
- Non-empty scopes ⇒ restricted. Unknown / missing scopes do **not** grant power.
- Allowlist only the minimal phone-resume surface; everything else is 403.
- Bound ``session_id`` / ``profile`` on the Session limit which session row a
  resume token may read.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional
from urllib.parse import unquote

from starlette.requests import Request

# Scope names that must never appear on handoff tokens.
# Canonical set — do not duplicate elsewhere (ws_tickets / middleware).
FORBIDDEN_HANDOFF_SCOPES: frozenset[str] = frozenset({
    "*",
    "superuser",
    "API_SERVER_KEY",
    "admin",
})

RESUME_SCOPE = "resume"

# Exact effective scopes for every handoff mint / verify / consume.
EXACT_HANDOFF_SCOPES: tuple[str, ...] = (RESUME_SCOPE,)

# WebSocket paths a resume session may mint tickets for (and connect to).
# Slice 2 opens chat PTY + events only. Admin WS (/api/ws JSON-RPC gateway,
# /api/console) stays closed. Destination bind is enforced at /api/pty
# (ticket bound_session_id / bound_profile win over client query params).
RESUME_WS_ENDPOINTS: frozenset[str] = frozenset({
    "/api/pty",
    "/api/events",
})

# Exact REST paths always allowed for resume (method checked separately).
_RESUME_REST_EXACT: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/ws-ticket"),
    ("POST", "/auth/logout"),
    ("POST", "/api/chat/image-upload"),
})

# GET /api/sessions/{bound_id} and a few read-only suffixes.
_BOUND_SESSION_SUFFIXES: frozenset[str] = frozenset({
    "",
    "/messages",
    "/latest-descendant",
})

_SESSION_PATH_RE = re.compile(r"^/api/sessions/(?P<sid>[^/]+)(?P<rest>/.*)?$")

_RESUME_DOCUMENT_PATHS: frozenset[str] = frozenset({"/", "/chat"})
_RESUME_STATIC_PATHS: frozenset[str] = frozenset({
    "/favicon.ico",
    "/manifest.webmanifest",
    "/pwa-icon-180.png",
    "/pwa-icon-192.png",
    "/pwa-icon-512.png",
    "/pwa-icon.svg",
    "/sw.js",
})
_RESUME_STATIC_PREFIXES: tuple[str, ...] = (
    "/assets/",
    "/ds-assets/",
    "/fonts/",
    "/fonts-terminal/",
)


def session_scopes(sess: Any) -> tuple[str, ...]:
    raw = getattr(sess, "scopes", ()) or ()
    return tuple(str(s) for s in raw if s)


def session_is_restricted(sess: Any) -> bool:
    """True when the session has non-empty scopes (resume / handoff)."""
    return bool(session_scopes(sess))


def session_is_full_dashboard(sess: Any) -> bool:
    """True when the session is a normal full-dashboard identity."""
    return not session_is_restricted(sess)


def exact_handoff_scopes_or_none(
    scopes: Iterable[str] | None,
) -> Optional[tuple[str, ...]]:
    """Canonical handoff scope validator (mint / verify / consume).

    - Empty / missing → ``(\"resume\",)`` (restricted, never full dashboard).
    - Exact ``resume`` only (dupes collapsed) → ``(\"resume\",)``.
    - ``admin``, unknown, forbidden, or mixed extras → ``None`` (reject).
    """
    names = [str(s).strip() for s in (scopes or ()) if str(s).strip()]
    if not names:
        return EXACT_HANDOFF_SCOPES
    uniq: list[str] = []
    for n in names:
        if n not in uniq:
            uniq.append(n)
    if uniq == [RESUME_SCOPE]:
        return EXACT_HANDOFF_SCOPES
    return None


def sanitize_handoff_scopes(scopes: Iterable[str] | None) -> tuple[str, ...]:
    """Return exact ``(\"resume\",)`` or ``()`` when scopes are invalid.

    Prefer :func:`exact_handoff_scopes_or_none` at security boundaries so
    callers can distinguish reject from empty without ambiguity.
    """
    exact = exact_handoff_scopes_or_none(scopes)
    return exact if exact is not None else ()


def bound_session_id(sess: Any) -> str:
    return str(getattr(sess, "bound_session_id", "") or "").strip()


def bound_profile(sess: Any) -> str:
    return str(getattr(sess, "bound_profile", "") or "").strip()


def _path_only(request: Request) -> str:
    # Starlette path is already decoded; keep no trailing slash except root.
    path = request.url.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path


def _is_spa_or_static_get(method: str, path: str) -> bool:
    """Allow only the Chat document and its static/PWA assets."""
    if method != "GET":
        return False
    if path in _RESUME_DOCUMENT_PATHS or path in _RESUME_STATIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _RESUME_STATIC_PREFIXES)


def _bound_session_get_allowed(path: str, sess: Any) -> bool:
    bid = bound_session_id(sess)
    if not bid:
        return False
    m = _SESSION_PATH_RE.match(path)
    if not m:
        return False
    path_sid = unquote(m.group("sid") or "")
    if path_sid != bid:
        return False
    rest = m.group("rest") or ""
    return rest in _BOUND_SESSION_SUFFIXES


def _profile_query_ok(request: Request, sess: Any) -> bool:
    """If the token is bound to a profile, query profile must match when set."""
    bp = bound_profile(sess)
    if not bp:
        return True
    qp = (request.query_params.get("profile") or "").strip()
    if not qp:
        # Unspecified profile → process default; allow only when bound is default-ish
        # or empty. Prefer deny when bound is a non-default named profile and
        # client omitted profile (avoids reading wrong HERMES_HOME).
        return bp in ("", "default")
    return qp == bp


def resume_request_allowed(request: Request, sess: Any) -> bool:
    """Default-deny allowlist for restricted (scoped) sessions."""
    if not session_is_restricted(sess):
        return True

    # Exact resume only — admin / unknown / mixed extras get nothing.
    if exact_handoff_scopes_or_none(session_scopes(sess)) is None:
        return False

    method = (request.method or "GET").upper()
    path = _path_only(request)

    if (method, path) in _RESUME_REST_EXACT:
        if path.startswith("/api/sessions"):
            return _profile_query_ok(request, sess)
        return True

    if method == "GET" and _bound_session_get_allowed(path, sess):
        return _profile_query_ok(request, sess)

    if _is_spa_or_static_get(method, path):
        return True

    return False


def require_full_dashboard_session(sess: Any) -> None:
    """Raise HTTP 403 if ``sess`` is resume-scoped.

    Used by mint-handoff and other full-dashboard-only handlers.
    """
    from fastapi import HTTPException

    if sess is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if session_is_restricted(sess):
        raise HTTPException(
            status_code=403,
            detail="Insufficient scope for this operation",
        )


def scope_denial_detail(request: Request, sess: Any) -> str:
    return "Insufficient scope for this path"


def is_handoff_consume_request(request: Request) -> bool:
    """True only for exact canonical GET ``/chat`` at the ASGI boundary.

    F-02 / Oscar M2 / F-01 (slice 1.3) + F-02 slice 1.4: no last-segment
    match and no client-controlled ``X-Forwarded-Prefix`` in the
    authorisation decision. Reject trailing slash, nested paths, API-shaped
    paths, doubled slashes, and non-canonical encodings. Rejected attempts
    must not consume the ticket.

    ASGI ``raw_path`` must be present as bytes/bytearray exactly ``b"/chat"``.
    Missing, None, wrong type, or non-canonical wire bytes fail closed.

    After a successful consume, callers may still use a normalised prefix for
    external redirect Location and cookie Path only.
    """
    if (request.method or "GET").upper() != "GET":
        return False

    path = request.url.path or ""
    if path != "/chat":
        return False

    # Slice 1.4 F-02: fail closed when raw_path is absent or non-canonical.
    # Optional ASGI key must not default-authorise decoded path lookalikes.
    raw = request.scope.get("raw_path")
    if not isinstance(raw, (bytes, bytearray)):
        return False
    return bytes(raw) == b"/chat"


def handoff_redirect_location(info: dict, *, prefix: str = "") -> str:
    """Build post-consume Location from ticket-bound session_id/profile only.

    Ticket wins over any client query params (F-02).
    """
    from urllib.parse import urlencode

    sid = str(info.get("session_id") or "").strip()
    profile = str(info.get("profile") or "").strip()
    pairs: list[tuple[str, str]] = []
    if sid:
        pairs.append(("resume", sid))
    if profile:
        pairs.append(("profile", profile))
    qs = urlencode(pairs)
    base = (prefix or "").rstrip("/")
    path = f"{base}/chat" if base else "/chat"
    return f"{path}?{qs}" if qs else path


def validate_handoff_target(
    session_id: str,
    profile: str = "",
) -> tuple[str, str]:
    """Validate mint target exists; return (canonical_session_id, canonical_profile).

    Raises ``ValueError`` with a safe message on failure (mapped to 400/404).

    Profile resolution:
    - empty / ``default`` → current process :func:`get_hermes_home` (respects
      ``HERMES_HOME`` / test isolation)
    - named profile → ``profiles.get_profile_dir`` after existence check
    """
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")

    from pathlib import Path

    from hermes_cli import profiles as profiles_mod
    from hermes_constants import get_hermes_home

    raw_profile = (profile or "").strip()
    if raw_profile:
        try:
            canon_profile = profiles_mod.normalize_profile_name(raw_profile)
            profiles_mod.validate_profile_name(canon_profile)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if canon_profile == "default":
            home = get_hermes_home()
            canon_profile = "default"
        else:
            if not profiles_mod.profile_exists(canon_profile):
                raise ValueError(f"Profile '{canon_profile}' does not exist")
            home = profiles_mod.get_profile_dir(canon_profile)
    else:
        home = get_hermes_home()
        canon_profile = ""

    from hermes_state import SessionDB

    home = Path(home)
    db_path = home / "state.db"
    # SessionDB creates the file if missing — we still need the row.
    db = SessionDB(db_path=db_path)
    try:
        resolved = db.resolve_session_id(sid)
        if not resolved:
            raise ValueError("session not found")
        row = db.get_session(resolved)
        if not row:
            raise ValueError("session not found")
        return str(resolved), str(canon_profile or "")
    finally:
        try:
            db.close()
        except Exception:
            pass


__all__ = [
    "EXACT_HANDOFF_SCOPES",
    "FORBIDDEN_HANDOFF_SCOPES",
    "RESUME_SCOPE",
    "RESUME_WS_ENDPOINTS",
    "bound_profile",
    "bound_session_id",
    "exact_handoff_scopes_or_none",
    "handoff_redirect_location",
    "is_handoff_consume_request",
    "require_full_dashboard_session",
    "resume_request_allowed",
    "sanitize_handoff_scopes",
    "scope_denial_detail",
    "session_is_full_dashboard",
    "session_is_restricted",
    "session_scopes",
    "validate_handoff_target",
]
