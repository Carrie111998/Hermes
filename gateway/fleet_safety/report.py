"""Human-readable kill report for a tripped session.

Deterministic string builder — no clock, no I/O. The live wiring hands the
result to Telegram + the desktop/home channel so a kill is never silent.
"""

from __future__ import annotations

from gateway.fleet_safety.deadloop_guard import Trip


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def format_kill_report(trip: Trip) -> str:
    """Build the kill-and-report message for ``trip``.

    Includes the session id, what it was doing (provider/model/effort + last
    state), estimated spend, the trip reason, and that it was hard-stopped —
    everything an operator needs to understand the kill without digging.
    """
    runtime_min = trip.runtime_seconds / 60.0
    model_bits = " / ".join(b for b in (trip.provider, trip.model, trip.effort and f"effort={trip.effort}") if b)
    lines = [
        "🛑 Dead-loop guard: agent session HARD-STOPPED",
        f"• session: {trip.session_id}",
    ]
    if model_bits:
        lines.append(f"• running: {model_bits}")
    lines.append(f"• reason: {trip.reason.value} — {trip.detail}")
    lines.append(
        f"• spend (est): {_humanize_tokens(trip.estimated_tokens)} tokens "
        f"over {trip.estimated_calls} model calls, {runtime_min:.1f} min runtime"
    )
    if trip.last_state:
        lines.append(f"• last state: {trip.last_state}")
    lines.append("• action: turn aborted + lease released. No human action required.")
    return "\n".join(lines)
