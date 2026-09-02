"""Principal-aware policy for creating and linking Kanban tasks.

The database layer deliberately stays policy-free: fixtures, migrations, and
trusted recovery code need to build task graphs without impersonating an
interactive caller.  Every user-facing creation surface calls this module
before opening a write transaction instead.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping, Optional


PROFILE_PRINCIPAL = "profile"
DASHBOARD_HUMAN_PRINCIPAL = "dashboard-human"


@dataclass(frozen=True)
class KanbanPrincipal:
    """Identity class used by the Kanban creation policy."""

    kind: str
    name: str
    authenticated: bool = True


@dataclass(frozen=True)
class KanbanCreationDecision:
    """Policy verdict with a stable, user-facing denial reason."""

    allowed: bool
    reason: str


class KanbanCreationDenied(PermissionError):
    """Raised before a creation/link surface reaches the database."""


def current_profile_principal() -> KanbanPrincipal:
    """Return the profile that owns the current CLI/worker process."""

    name = (os.environ.get("HERMES_PROFILE") or "").strip()
    if not name:
        try:
            from hermes_cli.profiles import get_active_profile_name

            name = (get_active_profile_name() or "default").strip()
        except Exception:
            name = "default"
    return KanbanPrincipal(PROFILE_PRINCIPAL, name or "default")


def dashboard_human_principal(name: Optional[str] = None) -> KanbanPrincipal:
    """Return an authenticated interactive-dashboard principal.

    HTTP authentication remains owned by the dashboard middleware.  The
    plugin route calls this only after that middleware has admitted the
    request (cookie/native bearer in gated mode, ephemeral session token in
    loopback mode).
    """

    return KanbanPrincipal(
        DASHBOARD_HUMAN_PRINCIPAL,
        (name or "dashboard-session").strip() or "dashboard-session",
        authenticated=True,
    )


def _load_policy_config() -> Mapping[str, Any]:
    from hermes_cli.config import load_config

    config = load_config()
    if not isinstance(config, Mapping):
        raise TypeError("Kanban policy config must be a mapping")
    return config


def creation_decision(
    principal: KanbanPrincipal,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> KanbanCreationDecision:
    """Evaluate task creation/link permission for ``principal``.

    Profile callers use ``kanban.can_create`` (backward-compatible default:
    true).  Interactive dashboard humans use the independent
    ``kanban.dashboard_create_policy`` setting.  Unknown principals, invalid
    policy values, and unreadable configuration all fail closed.
    """

    if config is None:
        try:
            config = _load_policy_config()
        except Exception:
            return KanbanCreationDecision(
                False,
                "Kanban creation policy is unavailable; refusing fail-closed.",
            )
    if not isinstance(config, Mapping):
        return KanbanCreationDecision(
            False,
            "Kanban creation policy is invalid; refusing fail-closed.",
        )
    raw_kanban = config.get("kanban") or {}
    if not isinstance(raw_kanban, Mapping):
        return KanbanCreationDecision(
            False,
            "Kanban creation policy is invalid; refusing fail-closed.",
        )

    if principal.kind == PROFILE_PRINCIPAL:
        if bool(raw_kanban.get("can_create", True)):
            return KanbanCreationDecision(True, "")
        return KanbanCreationDecision(
            False,
            f"profile {principal.name!r} has kanban.can_create=false",
        )

    if principal.kind == DASHBOARD_HUMAN_PRINCIPAL:
        policy = str(
            raw_kanban.get("dashboard_create_policy", "authenticated")
        ).strip().lower()
        if policy == "authenticated" and principal.authenticated:
            return KanbanCreationDecision(True, "")
        if policy == "disabled":
            return KanbanCreationDecision(
                False,
                "kanban.dashboard_create_policy is disabled",
            )
        return KanbanCreationDecision(
            False,
            "kanban.dashboard_create_policy must be 'authenticated' or "
            "'disabled'; refusing fail-closed",
        )

    return KanbanCreationDecision(
        False,
        f"unknown Kanban principal kind {principal.kind!r}; refusing fail-closed",
    )


def require_creation_allowed(
    principal: KanbanPrincipal,
    *,
    operation: str,
    config: Optional[Mapping[str, Any]] = None,
) -> None:
    """Raise :class:`KanbanCreationDenied` when policy denies ``operation``."""

    decision = creation_decision(principal, config=config)
    if not decision.allowed:
        raise KanbanCreationDenied(
            f"{operation} refused for {principal.kind} {principal.name!r}: "
            f"{decision.reason}"
        )
