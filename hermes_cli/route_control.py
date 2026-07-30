"""Shared user-facing control commands for per-turn routing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent.turn_routing_runtime import (
    LIVE_AUTOMATIC_ROUTING_ENABLED,
    TurnRoutingSessionState,
)


_USAGE = "/route status|off|observe|auto|why|budget|reset"
_MODES = frozenset({"off", "observe", "auto"})


def _mode(config: Any) -> str:
    if not isinstance(config, Mapping):
        return "off"
    value = str(config.get("mode") or "off").strip().casefold()
    return value if value in _MODES else "off"


def _default_budget_status(config: Mapping[str, Any]) -> Any:
    raw_budget = config.get("budget")
    budget = raw_budget if isinstance(raw_budget, Mapping) else {}
    try:
        weekly_limit = max(0, int(budget.get("grok_weekly_limit", 0)))
    except (TypeError, ValueError):
        weekly_limit = 0
    if weekly_limit <= 0:
        return None

    from agent.turn_router_budget import TurnRouterBudgetLedger

    return TurnRouterBudgetLedger(weekly_limit=weekly_limit).status(
        cooldown_scope="grok"
    )


def _status_text(mode: str, state: TurnRoutingSessionState | None) -> str:
    mode_text = mode
    if mode == "auto" and not LIVE_AUTOMATIC_ROUTING_ENABLED:
        mode_text = "auto (locked; effective observe)"
    lines = [f"Route mode: {mode_text}"]
    if state is None:
        lines.append("Session routing state: unavailable")
        return "\n".join(lines)
    if state.affinity_route and state.affinity_remaining > 0:
        lines.append(
            f"Affinity: {state.affinity_route} "
            f"({state.affinity_remaining} turns remaining)"
        )
    else:
        lines.append("Affinity: none")
    if state.fail_off:
        lines.append(
            "Automatic routing fail-off: "
            f"{state.fail_off_reason or 'route_failure_limit'}"
        )
    else:
        lines.append("Automatic routing fail-off: inactive")
    return "\n".join(lines)


def _why_text(state: TurnRoutingSessionState | None) -> str:
    payload = state.latest_payload if state is not None else None
    if not isinstance(payload, Mapping):
        return "No route decision recorded for this session"
    route = str(payload.get("route") or "current")
    source = str(payload.get("source") or "unknown")
    reason = str(
        payload.get("selection_reason_code")
        or payload.get("reason_code")
        or "unknown"
    )
    event = str(state.latest_event or "route.unknown")
    turn_id = str(payload.get("turn_id") or "unknown")
    return f"Latest route: {route} via {source} ({reason}, {event}, turn {turn_id})"


def _budget_text(snapshot: Any, weekly_limit: int) -> str:
    if snapshot is None or weekly_limit <= 0:
        return "Grok automation disabled (0/week)"
    available = int(getattr(snapshot, "available_slots", 0))
    used = int(getattr(snapshot, "used_slots", 0))
    reserved = int(getattr(snapshot, "reserved_slots", 0))
    limit = int(getattr(snapshot, "weekly_limit", weekly_limit))
    text = (
        f"Grok budget: {available}/{limit} available; "
        f"{used} used; {reserved} reserved"
    )
    cooldown_reason = str(getattr(snapshot, "cooldown_reason", "") or "")
    cooldown_until = getattr(snapshot, "cooldown_until", None)
    if cooldown_reason and cooldown_until is not None:
        text += f"; cooldown {cooldown_reason} until {float(cooldown_until):g}"
    return text


def execute_route_command(
    argument: str,
    *,
    state: TurnRoutingSessionState | None,
    config_loader: Callable[[], Mapping[str, Any]],
    mode_writer: Callable[[str], None] | None = None,
    budget_status_loader: Callable[[], Any] | None = None,
) -> str:
    """Execute one validated route-control command without touching prompts."""

    parts = str(argument or "").strip().casefold().split()
    action = parts[0] if len(parts) == 1 else ""
    if not action:
        action = "status" if not parts else "invalid"
    if action not in {"status", "why", "budget", "reset", *_MODES}:
        return f"Usage: {_USAGE}"

    config = config_loader()
    config = config if isinstance(config, Mapping) else {}
    current_mode = _mode(config)

    if action in _MODES:
        if action == "auto" and not LIVE_AUTOMATIC_ROUTING_ENABLED:
            return "Automatic routing is locked; use /route observe"
        if not callable(mode_writer):
            return "Route mode is read-only on this surface"
        mode_writer(action)
        return f"Route mode set to {action}"
    if action == "status":
        return _status_text(current_mode, state)
    if action == "why":
        return _why_text(state)
    if action == "reset":
        if state is not None:
            state.reset()
        return f"Session routing state reset; route mode remains {current_mode}"

    raw_budget = config.get("budget")
    budget = raw_budget if isinstance(raw_budget, Mapping) else {}
    try:
        weekly_limit = max(0, int(budget.get("grok_weekly_limit", 0)))
    except (TypeError, ValueError):
        weekly_limit = 0
    try:
        snapshot = (
            budget_status_loader()
            if callable(budget_status_loader)
            else _default_budget_status(config)
        )
    except Exception:
        return "Grok budget status unavailable"
    return _budget_text(snapshot, weekly_limit)
