"""User-owned, session-scoped delegation routes.

Authorization lives in trusted session ``model_config`` — never in
model-facing ``delegate_task`` arguments. The agent can only consume a
route the user already approved.
"""

from __future__ import annotations

import secrets
import shlex
from typing import Any, Optional

from hermes_constants import parse_reasoning_effort

ROUTE_KEY = "_delegation_route"
SCOPES = ("next", "session")
USAGE = (
    "Usage: /delegate-route --provider <name> --model <id> "
    "[--reasoning-effort <level>] [--scope next|session]\n"
    "       /delegate-route clear\n"
    "       /delegate-route"
)


class DelegationRouteError(ValueError):
    """User-facing command or consume error."""


def parse_delegate_route_args(raw: str) -> dict[str, Any]:
    """Parse slash-command arguments. Bare args inspect; ``clear`` clears."""
    text = (raw or "").strip()
    if not text:
        return {"action": "inspect"}

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise DelegationRouteError(f"Could not parse arguments: {exc}") from exc

    if tokens and tokens[0].lower() in {"clear", "reset", "off"}:
        if len(tokens) > 1:
            raise DelegationRouteError("Usage: /delegate-route clear")
        return {"action": "clear"}

    provider = ""
    model = ""
    effort = ""
    scope = "next"
    i = 0
    while i < len(tokens):
        token = tokens[i]
        key = token.lower()
        if key in {"--provider", "-p"}:
            i += 1
            if i >= len(tokens):
                raise DelegationRouteError("--provider requires a value")
            provider = tokens[i].strip()
        elif key in {"--model", "-m"}:
            i += 1
            if i >= len(tokens):
                raise DelegationRouteError("--model requires a value")
            model = tokens[i].strip()
        elif key in {"--reasoning-effort", "--effort", "-e"}:
            i += 1
            if i >= len(tokens):
                raise DelegationRouteError("--reasoning-effort requires a value")
            effort = tokens[i].strip()
        elif key == "--scope":
            i += 1
            if i >= len(tokens):
                raise DelegationRouteError("--scope requires next or session")
            scope = tokens[i].strip().lower()
        else:
            raise DelegationRouteError(f"Unknown argument: {token}\n{USAGE}")
        i += 1

    if scope not in SCOPES:
        raise DelegationRouteError("--scope must be 'next' or 'session'")
    if not provider or not model:
        raise DelegationRouteError(
            "Provider and model are required to authorize a route.\n" + USAGE
        )
    requested_effort, effective_effort = normalize_effort(effort)
    return {
        "action": "approve",
        "provider": provider,
        "model": model,
        "reasoning_effort": requested_effort,
        "effective_reasoning_effort": effective_effort,
        "scope": scope,
    }


def normalize_effort(raw: Any) -> tuple[Optional[str], Optional[str]]:
    """Return (requested, effective). Unknown levels clamp to ``high``."""
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    requested = text.lower()
    parsed = parse_reasoning_effort(requested)
    if parsed is None:
        return requested, "high"
    if parsed.get("enabled") is False:
        return "none", "none"
    return requested, str(parsed.get("effort") or requested)


def format_route(route: Optional[dict[str, Any]]) -> str:
    if not route:
        return "No user-authorized delegation route on this session."
    requested = route.get("requested") or {}
    effective = route.get("effective") or {}
    lines = [
        "Authorized delegation route (user-owned):",
        f"  route_id: {route.get('route_id')}",
        f"  scope: {route.get('scope')}",
        (
            "  requested: "
            f"provider={requested.get('provider') or '-'} "
            f"model={requested.get('model') or '-'} "
            f"reasoning_effort={requested.get('reasoning_effort') or '(inherit)'}"
        ),
        (
            "  effective: "
            f"provider={effective.get('provider') or requested.get('provider') or '-'} "
            f"model={effective.get('model') or requested.get('model') or '-'} "
            f"reasoning_effort={effective.get('reasoning_effort') or requested.get('reasoning_effort') or '(inherit)'}"
        ),
    ]
    if route.get("clamped"):
        lines.append(
            "  note: requested reasoning effort is not supported; "
            f"clamped to {effective.get('reasoning_effort')}."
        )
    return "\n".join(lines)


def _unwrap_session_db(db):
    inner = getattr(db, "_db", None)
    return inner if inner is not None else db


def live_session_row_id(session: Optional[dict] = None, *, parent=None) -> str:
    """SessionDB row id that ``delegate_task`` will peek.

    Prefer the live agent id. ``session_key`` and gateway platform keys can
    diverge from that row (compression, messaging store keys).
    """
    if parent is not None:
        return str(getattr(parent, "session_id", "") or "")
    session = session or {}
    agent = session.get("agent")
    return str(
        getattr(agent, "session_id", None)
        or session.get("session_id")
        or session.get("session_key")
        or ""
    )


def _session_row_exists(db, session_id: str) -> bool:
    getter = getattr(db, "get_session", None)
    if getter is None:
        return True
    return bool(getter(session_id))


def _session_parts(parent_or_db, session_id: Optional[str] = None):
    if session_id is None:
        db = _unwrap_session_db(getattr(parent_or_db, "_session_db", None))
        sid = live_session_row_id(parent=parent_or_db)
        return db, sid
    return _unwrap_session_db(parent_or_db), str(session_id or "")


def peek_approved_route(parent_or_db, session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    db, sid = _session_parts(parent_or_db, session_id)
    if db is None or not sid:
        return None
    getter = getattr(db, "get_session_model_config_value", None)
    if getter is None:
        return None
    raw = getter(sid, ROUTE_KEY)
    if not isinstance(raw, dict):
        return None
    if not raw.get("route_id") or not isinstance(raw.get("requested"), dict):
        return None
    return raw


def approve_route(
    db,
    session_id: str,
    *,
    provider: str,
    model: str,
    reasoning_effort: Optional[str],
    effective_reasoning_effort: Optional[str],
    scope: str,
) -> dict[str, Any]:
    if db is None or not session_id:
        raise DelegationRouteError("No live session to store a delegation route.")
    if not _session_row_exists(db, session_id):
        raise DelegationRouteError("No live session to store a delegation route.")
    requested_effort, clamped_effort = normalize_effort(reasoning_effort)
    effective_effort = effective_reasoning_effort or clamped_effort
    route = {
        "route_id": secrets.token_hex(8),
        "scope": scope if scope in SCOPES else "next",
        "requested": {
            "provider": provider,
            "model": model,
            "reasoning_effort": requested_effort,
        },
        "effective": {
            "provider": provider,
            "model": model,
            "reasoning_effort": effective_effort,
        },
        "clamped": bool(
            requested_effort
            and effective_effort
            and requested_effort != effective_effort
        ),
    }
    db.patch_session_model_config(session_id, {ROUTE_KEY: route})
    stored = peek_approved_route(db, session_id)
    if not stored:
        raise DelegationRouteError("No live session to store a delegation route.")
    return stored


def clear_route(db, session_id: str) -> bool:
    existing = peek_approved_route(db, session_id)
    if db is None or not session_id or not existing:
        return False
    db.patch_session_model_config(session_id, {ROUTE_KEY: None})
    return True


def consume_route(db, session_id: str, route_id: str) -> bool:
    """Atomically drop a one-shot route after a successful spawn."""
    current = peek_approved_route(db, session_id)
    if not current or current.get("route_id") != route_id:
        return False
    if current.get("scope") != "next":
        return False
    db.patch_session_model_config(session_id, {ROUTE_KEY: None})
    return True


def overlay_delegation_cfg(cfg: dict, route: Optional[dict[str, Any]]) -> dict:
    """Copy ``cfg`` and apply a user-approved route without mutating globals."""
    overlaid = dict(cfg or {})
    if not route:
        return overlaid
    requested = route.get("requested") or {}
    effective = route.get("effective") or {}
    provider = str(effective.get("provider") or requested.get("provider") or "").strip()
    model = str(effective.get("model") or requested.get("model") or "").strip()
    effort = effective.get("reasoning_effort")
    if provider:
        overlaid["provider"] = provider
    if model:
        overlaid["model"] = model
    if effort:
        overlaid["reasoning_effort"] = effort
    return overlaid


def route_metadata(route: Optional[dict[str, Any]], creds: Optional[dict] = None) -> Optional[dict[str, Any]]:
    if not route:
        return None
    requested = dict(route.get("requested") or {})
    effective = dict(route.get("effective") or {})
    if creds:
        if creds.get("provider"):
            effective["provider"] = creds.get("provider")
        if creds.get("model"):
            effective["model"] = creds.get("model")
    return {
        "route_id": route.get("route_id"),
        "scope": route.get("scope"),
        "requested": requested,
        "effective": effective,
        "clamped": bool(route.get("clamped")),
    }


def handle_delegate_route_command(raw_args: str, db, session_id: str) -> str:
    """Shared slash-command handler for CLI, gateway, and desktop/TUI."""
    parsed = parse_delegate_route_args(raw_args)
    action = parsed.get("action")
    if action == "inspect":
        return format_route(peek_approved_route(db, session_id))
    if action == "clear":
        if clear_route(db, session_id):
            return "Cleared the authorized delegation route for this session."
        return "No authorized delegation route to clear."
    route = approve_route(
        db,
        session_id,
        provider=parsed["provider"],
        model=parsed["model"],
        reasoning_effort=parsed.get("reasoning_effort"),
        effective_reasoning_effort=parsed.get("effective_reasoning_effort"),
        scope=parsed["scope"],
    )
    prefix = (
        "Authorized. The next delegate_task will consume this route."
        if route.get("scope") == "next"
        else "Authorized for this session until you run /delegate-route clear."
    )
    return prefix + "\n" + format_route(route)
