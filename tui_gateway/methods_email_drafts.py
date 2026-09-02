"""P1 — Desktop/RPC approval surface for the outbound-email draft store (#99876).

While ``platforms.email.extra.draft_only`` is on, every outbound email lands in
the durable ``gateway.outbound_drafts`` store as a ``pending`` draft.  This
module is the *only* way a draft is ever transmitted: an explicit owner
approval over JSON-RPC (``email.drafts.approve``) claims the draft atomically
and hands it to a one-shot SMTP delivery.  Deny, cancel, list and detail round
out the surface the Desktop approval pane drives.

Authorization is owner-only and unforgeable: the request's bound transport
(``transport.auth_identity``) must carry a real human identity.  ``internal``
provider identities (cron, agent, gateway-internal) and unauthenticated
transports are rejected — inbound mail can never authorize a send.

Handlers are registered into ``server._methods`` *without* rebinding their
globals (unlike the ``HandlerRegistry`` split modules) so the module-level
``_smtp_deliver`` / ``_emit_event`` seams stay patchable by tests and by the
Desktop plugin's own seams.  Server helpers are imported at the bottom of this
module to avoid an import cycle (server.py imports this module at the end of
its own import).
"""

from __future__ import annotations

import os
import smtplib
import ssl as _ssl
import uuid
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, Optional

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method


# ── store resolution ─────────────────────────────────────────────────────
def _get_store():
    """Resolve the process draft store.

    Prefers the most recently constructed store (the adapter's, or the store a
    test created) so the RPC surface and the adapter share one durable file and
    one in-memory budget view.  Falls back to the canonical Hermes-home store.
    """
    from gateway.outbound_drafts import active_store, get_or_create_store

    store = active_store()
    if store is not None:
        return store
    return get_or_create_store()


# ── one-shot SMTP delivery (the only egress) ─────────────────────────────
def _smtp_deliver(draft) -> str:
    """Transmit one approved draft over SMTP.  Returns the SMTP Message-ID.

    Raises ``TimeoutError`` when the server accepted the connection but the
    send outcome is unknown (the caller records ``unknown_delivery`` and never
    auto-resends).  Raises any other exception on a permanent failure (the
    caller records ``failed``).  Exactly one call per approved draft.
    """
    address = os.environ.get("EMAIL_ADDRESS", "")
    password = os.environ.get("EMAIL_PASSWORD", "")
    smtp_host = os.environ.get("EMAIL_SMTP_HOST", "")
    try:
        smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "587") or "587")
    except (ValueError, TypeError):
        smtp_port = 587

    if not all([address, password, smtp_host]):
        raise RuntimeError(
            "Email not configured (EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_SMTP_HOST required)"
        )

    msg = MIMEMultipart()
    msg["From"] = address
    msg["To"] = draft.recipient
    msg["Subject"] = draft.subject or "Hermes Agent"
    if draft.in_reply_to:
        msg["In-Reply-To"] = draft.in_reply_to
    if draft.references:
        msg["References"] = draft.references
    msg["Date"] = formatdate(localtime=True)
    domain = address.rsplit("@", 1)[-1] if "@" in address else "localhost"
    msg["Message-ID"] = f"<hermes-{uuid.uuid4().hex[:12]}@{domain}>"

    if draft.body:
        msg.attach(MIMEText(draft.body, "plain", "utf-8"))

    for entry in draft.attachment_manifest or []:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            continue
        try:
            with open(p, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                name = entry.get("name") if isinstance(entry, dict) else p.name
                part.add_header("Content-Disposition", f"attachment; filename={name or p.name}")
                msg.attach(part)
        except Exception:
            continue

    server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
    try:
        server.starttls(context=_ssl.create_default_context())
        server.login(address, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            server.close()
    return str(msg["Message-ID"])


# ── event seam ───────────────────────────────────────────────────────────
def _emit_event(event: str, payload: Optional[dict] = None) -> None:
    """Fan a draft-lifecycle event to connected surfaces (best-effort)."""
    try:
        _broadcast_global_event(event, payload)
    except Exception:
        pass


# ── authz ────────────────────────────────────────────────────────────────
def _is_owner_identity(transport) -> bool:
    """True only for a real human (non-internal) identity on the transport."""
    identity = getattr(transport, "auth_identity", None)
    if not isinstance(identity, dict):
        return False
    provider = str(identity.get("provider") or "").lower()
    if not provider or provider == "internal":
        return False
    return bool(identity.get("user_id"))


def _principal(transport) -> str:
    identity = getattr(transport, "auth_identity", None) or {}
    provider = str(identity.get("provider") or "unknown")
    user = str(identity.get("user_id") or "unknown")
    return f"{provider}:{user}"


# ── handlers ──────────────────────────────────────────────────────────────
@method("email.drafts.list")
def _(rid, params: dict) -> dict:
    """List pending drafts (owner only)."""
    transport = current_transport()
    if not _is_owner_identity(transport):
        return _err(rid, 4403, "owner identity required")
    store = _get_store()
    drafts = store.list_drafts()
    return _ok(rid, {"drafts": [d.to_dict() for d in drafts]})


@method("email.drafts.detail")
def _(rid, params: dict) -> dict:
    """Return one draft by id (owner only)."""
    transport = current_transport()
    if not _is_owner_identity(transport):
        return _err(rid, 4403, "owner identity required")
    draft_id = (params or {}).get("draft_id")
    if not draft_id:
        return _err(rid, -32602, "draft_id required")
    store = _get_store()
    draft = store.get_draft(str(draft_id))
    if draft is None:
        return _err(rid, 4404, "draft not found")
    return _ok(rid, {"draft": draft.to_dict()})


@method("email.drafts.approve")
def _(rid, params: dict) -> dict:
    """Approve + claim a pending draft and deliver it exactly once (owner only)."""
    transport = current_transport()
    if not _is_owner_identity(transport):
        return _err(rid, 4403, "owner identity required")

    params = params or {}
    draft_id = params.get("draft_id")
    expected_hash = params.get("expected_content_hash")
    if not draft_id:
        return _err(rid, -32602, "draft_id required")

    store = _get_store()
    draft = store.get_draft(str(draft_id))
    if draft is None:
        return _err(rid, 4404, "draft not found")

    # Delivery budget / circuit gate before claiming — a blocked delivery
    # leaves the draft pending for a later approval.
    gate = store.check_delivery_allowed(session_key=draft.session_key)
    if not gate.allowed:
        return _ok(rid, {"claimed": False, "reason": gate.reason})

    claimed = store.approve_and_claim_draft(
        str(draft_id), str(expected_hash or draft.content_hash), actor=_principal(transport)
    )
    if not claimed.claimed:
        return _ok(rid, {"claimed": False, "reason": claimed.reason})

    # Surface the full lifecycle so a subscriber that missed the original
    # adapter event still renders the approval card, then the outcome.
    _emit_event("email.draft.created", draft.to_dict())
    _emit_event("email.draft.requires_approval", draft.to_dict())

    try:
        message_id = _smtp_deliver(draft)
    except TimeoutError:
        store.record_send_outcome(str(draft_id), "unknown_delivery")
        _emit_event("email.draft.unknown_delivery", {"draft_id": draft_id})
        return _ok(rid, {"claimed": True, "state": "unknown_delivery"})
    except Exception as exc:  # noqa: BLE001 - permanent failure, never auto-resend
        store.record_send_outcome(str(draft_id), "failed", error=str(exc))
        _emit_event("email.draft.failed", {"draft_id": draft_id, "error": str(exc)})
        return _ok(rid, {"claimed": True, "state": "failed", "error": str(exc)})

    store.record_send_outcome(str(draft_id), "sent", message_id=message_id)
    _emit_event("email.draft.sent", {"draft_id": draft_id, "message_id": message_id})
    return _ok(rid, {"claimed": True, "state": "sent", "message_id": message_id})


@method("email.drafts.deny")
def _(rid, params: dict) -> dict:
    """Deny a pending draft (owner only)."""
    transport = current_transport()
    if not _is_owner_identity(transport):
        return _err(rid, 4403, "owner identity required")
    draft_id = (params or {}).get("draft_id")
    if not draft_id:
        return _err(rid, -32602, "draft_id required")
    store = _get_store()
    denied = store.deny_draft(str(draft_id), actor=_principal(transport))
    _emit_event("email.draft.denied", {"draft_id": draft_id})
    return _ok(rid, {"denied": denied, "draft_id": draft_id})


@method("email.drafts.cancel")
def _(rid, params: dict) -> dict:
    """Cancel a pending draft (owner only)."""
    transport = current_transport()
    if not _is_owner_identity(transport):
        return _err(rid, 4403, "owner identity required")
    draft_id = (params or {}).get("draft_id")
    if not draft_id:
        return _err(rid, -32602, "draft_id required")
    store = _get_store()
    cancelled = store.cancel_draft(str(draft_id), actor=_principal(transport))
    _emit_event("email.draft.cancelled", {"draft_id": draft_id})
    return _ok(rid, {"cancelled": cancelled, "draft_id": draft_id})


def register(server) -> None:
    """Register the email.drafts.* handlers into server._methods.

    Unlike the ``HandlerRegistry.install`` split modules, handlers keep their
    original module globals so the ``_smtp_deliver`` / ``_emit_event`` seams
    remain patchable (tests, Desktop plugin).  Server helpers are resolved
    from this module's namespace, populated by the bottom-of-module import.
    """
    for name, fn in _registry._pending:
        server._methods[name] = fn


# Imported at the bottom so server.py (which imports this module at the end of
# its own import) has already defined every helper the handlers close over.
from tui_gateway.server import (  # noqa: E402
    _broadcast_global_event,
    _err,
    _ok,
    current_transport,
)
