"""Messaging pairing dashboard routes (extracted verbatim from web_server.py).

Handler bodies are byte-identical to their previous in-web_server form; the
helper they call (``_resolve_profile_dir``) still lives in web_server and is
reached via the late-binding seam in :mod:`hermes_cli.web_deps`, so
``monkeypatch.setattr(web_server, ...)`` keeps working.
"""

import logging
from typing import Optional  # noqa: F401

from fastapi import APIRouter, HTTPException  # noqa: F401

from hermes_cli.web_deps import late
from hermes_cli.web_models import PairingApprove, PairingRevoke  # noqa: F401

# Same logger the handlers used before extraction (identical logger object).
_log = logging.getLogger("hermes_cli.web_server")

router = APIRouter()

# Late-bound web_server helpers (resolved at call time; cycle-safe,
# monkeypatch-transparent).
_resolve_profile_dir = late("_resolve_profile_dir")


def _pairing_store(profile: Optional[str] = None):
    """Pairing store for ``profile`` — the dashboard's own when unspecified.

    Every other admin endpoint scopes by profile, and the gateway already
    keeps one store per served profile (``gateway/run.py``). Without this the
    dashboard and desktop always read the global store, so an operator on a
    named profile approves into a whitelist their gateway never consults.

    ``PairingStore`` resolves the profile's home itself (``default`` maps back
    to the global store), so this only needs to validate the name — no
    ``_profile_scope`` needed, and nothing process-global is swapped across
    the ``await`` boundary.
    """
    from gateway.pairing import PairingStore

    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return PairingStore()

    _resolve_profile_dir(requested)  # 400/404 on an unknown profile

    return PairingStore(profile=requested)


# ---------------------------------------------------------------------------
# Pairing endpoints — approve / revoke / list messaging pairing codes.
#
# These are how a remote admin onboards messaging users (Telegram, Discord, …)
# without shell access.  Wraps gateway.pairing.PairingStore directly.
# ---------------------------------------------------------------------------


@router.get("/api/pairing")
async def list_pairing(profile: Optional[str] = None):
    store = _pairing_store(profile)
    return {
        "pending": store.list_pending(),
        "approved": store.list_approved(),
    }


@router.post("/api/pairing/approve")
async def approve_pairing(body: PairingApprove):
    store = _pairing_store(body.profile)
    platform = (body.platform or "").lower().strip()
    # `request_id` is what an admin surface sends after listing pending
    # requests; `code` is the one-time code the user relays from their DM.
    # A GUI that only knows the older field name still works — a value with
    # request-id shape routes to the request path either way.
    target = (body.request_id or body.code or "").strip()
    if not platform or not target:
        raise HTTPException(
            status_code=400, detail="platform and request_id or code are required"
        )

    by_request_id = bool(body.request_id) or store.looks_like_request_id(target)
    if by_request_id:
        result = store.approve_request(platform, target)
    else:
        result = store.approve_code(platform, target.upper())

    if result:
        return {"ok": True, "user": result}
    # Lockout only gates the code path, so only report it there — otherwise a
    # stale request id would surface as a bogus 429 while the platform sat
    # locked out for an unrelated reason.
    if not by_request_id and store._is_locked_out(platform):
        raise HTTPException(
            status_code=429,
            detail=f"Platform '{platform}' is locked out after too many failed approvals.",
        )
    raise HTTPException(
        status_code=404,
        detail=f"Pairing request or code not found or expired for platform '{platform}'.",
    )


@router.post("/api/pairing/revoke")
async def revoke_pairing(body: PairingRevoke):
    store = _pairing_store(body.profile)
    platform = (body.platform or "").lower().strip()
    if not platform or not body.user_id:
        raise HTTPException(status_code=400, detail="platform and user_id are required")
    if store.revoke(platform, body.user_id):
        return {"ok": True}
    raise HTTPException(
        status_code=404,
        detail=f"User {body.user_id} not found in approved list for {platform}.",
    )


@router.post("/api/pairing/clear-pending")
async def clear_pending_pairing(profile: Optional[str] = None):
    store = _pairing_store(profile)
    count = store.clear_pending()
    return {"ok": True, "cleared": count}
