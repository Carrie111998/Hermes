"""Auto-resume restart-loop breaker (#30719, defense-3).

Defenses 1 and 2 (the ``_HERMES_GATEWAY`` guard on ``hermes gateway
stop|restart`` + ``terminal_tool``, and the cron-creation lifecycle
filter) stop the agent from scheduling its own restart via the cron and
CLI paths.  They do NOT cover every SIGTERM source: an agent running a
raw ``terminal("launchctl kickstart -k gui/<uid>/ai.hermes.gateway")``,
an external monitor with a bad trigger, or any other repeated crash can
still drive the supervisor (launchd ``KeepAlive`` / systemd ``Restart=``)
into a tight respawn loop.  On each boot the gateway auto-resumes the
restart-interrupted session, whose next turn re-runs the offending
logic — SIGTERM every ~10 seconds until manually broken.

This module is the last-resort circuit breaker: it records a timestamp
each time the gateway boots with restart-interrupted sessions pending,
keeps a rolling window of recent boots persisted across processes (each
boot is a fresh process, so in-memory state is useless), and reports the
loop as "tripped" once too many such boots happen inside a short window.
When tripped, the caller SKIPS auto-resume for that boot — the gateway
still starts and serves real inbound messages, it just stops replaying
the session that keeps killing it, which breaks the cycle and puts a
human back in the loop.

State lives in ``<HERMES_HOME>/gateway/restart_loop.json`` so it is
profile-scoped and survives process death.  It is intentionally tiny and
best-effort: any read/write failure fails OPEN (no false trip) because a
broken breaker must never wedge a healthy gateway.
"""

from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger("gateway.run")

# Defaults chosen so a legitimate operator restart (or two) never trips the
# breaker, but the documented ~10s respawn loop does within a few cycles.
DEFAULT_MAX_RESTARTS = 3
DEFAULT_WINDOW_SECONDS = 60


def _state_path():
    return get_hermes_home() / "gateway" / "restart_loop.json"


def _load_boots() -> List[float]:
    try:
        raw = _state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        boots = data.get("boots", [])
        return [float(t) for t in boots if isinstance(t, (int, float))]
    except (OSError, ValueError, TypeError):
        return []


def _save_boots(boots: List[float]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"boots": boots}), encoding="utf-8")
    except OSError:
        pass


def record_restart_interrupted_boot(
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    *,
    now: Optional[float] = None,
) -> List[float]:
    """Record that the gateway just booted with restart-interrupted sessions.

    Prunes boots older than ``window_seconds`` and appends the current time.
    Returns the pruned+appended list (most recent last).  Best-effort — a
    persistence failure returns the in-memory list without raising.
    """
    ts = time.time() if now is None else now
    cutoff = ts - max(1, window_seconds)
    boots = [t for t in _load_boots() if t >= cutoff]
    boots.append(ts)
    _save_boots(boots)
    return boots


def is_restart_loop_tripped(
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    *,
    now: Optional[float] = None,
) -> bool:
    """Return True if the gateway has restarted ``>= max_restarts`` times with
    restart-interrupted sessions inside the last ``window_seconds``.

    Reads the persisted boot log written by
    ``record_restart_interrupted_boot`` and counts boots within the window.
    Fails OPEN (returns False) on any error — a broken breaker must never
    wedge a healthy gateway.
    """
    if max_restarts <= 0:
        return False
    ts = time.time() if now is None else now
    cutoff = ts - max(1, window_seconds)
    try:
        recent = [t for t in _load_boots() if t >= cutoff]
    except Exception:  # pragma: no cover — _load_boots already guards
        return False
    return len(recent) >= max_restarts


def clear() -> None:
    """Remove the persisted boot log (used on clean shutdown / by tests)."""
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass


DEFAULT_NOTIFICATION_COOLDOWN_SECONDS = 60


def _notification_state_path():
    return get_hermes_home() / "gateway" / "shutdown_notifications.json"


def _load_notification_timestamps() -> dict:
    try:
        raw = _notification_state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        last_notified = data.get("last_notified", {})
        return {str(k): float(v) for k, v in last_notified.items() if isinstance(v, (int, float))}
    except (OSError, ValueError, TypeError):
        return {}


def _save_notification_timestamps(timestamps: dict) -> None:
    try:
        path = _notification_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_notified": timestamps}), encoding="utf-8")
    except OSError:
        pass


def should_suppress_shutdown_notification(
    destination_key: str,
    cooldown_seconds: float = DEFAULT_NOTIFICATION_COOLDOWN_SECONDS,
    *,
    now: Optional[float] = None,
) -> bool:
    """Return True when a shutdown notification for ``destination_key`` was
    already sent within ``cooldown_seconds`` (#71180).

    ``_notify_active_sessions_of_shutdown`` in ``gateway/run.py`` dedupes
    sends within a *single* shutdown via a local in-memory set, but that set
    starts empty on every fresh gateway process. When something outside
    Hermes repeatedly cycles the host (OS suspend/resume tearing a WSL
    distro down and back up, a flapping supervisor, ...), each new process
    is free to announce a "first" shutdown again seconds after the previous
    process already announced one, flooding the destination.

    This persists the last-notified timestamp per destination across
    process restarts (mirroring the restart-loop breaker's own persistence
    model) so a genuinely rapid re-cycle is suppressed while an isolated
    shutdown still notifies normally.

    When NOT suppressing, this call also records the current attempt as the
    new "last notified" timestamp — callers should call this immediately
    before sending, not after, so a failed send doesn't leave the window
    unrecorded and re-flood on retry.

    Best-effort / fails OPEN: any read/write error never suppresses a
    legitimate notification (``cooldown_seconds <= 0`` also disables it).
    """
    if cooldown_seconds <= 0:
        return False
    ts = time.time() if now is None else now
    try:
        timestamps = _load_notification_timestamps()
    except Exception:  # pragma: no cover — _load_notification_timestamps already guards
        return False
    last = timestamps.get(destination_key)
    if last is not None and (ts - last) < cooldown_seconds:
        return True
    timestamps[destination_key] = ts
    _save_notification_timestamps(timestamps)
    return False


def check_and_record(
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    *,
    now: Optional[float] = None,
) -> bool:
    """Record this restart-interrupted boot and report whether the loop is now
    tripped.

    This is the single entry point the gateway calls: it appends the current
    boot, then checks whether the (now-updated) window has reached the
    threshold.  Returns True when auto-resume should be SKIPPED to break the
    loop.
    """
    boots = record_restart_interrupted_boot(window_seconds, now=now)
    tripped = len(boots) >= max_restarts if max_restarts > 0 else False
    if tripped:
        logger.warning(
            "Restart-loop breaker TRIPPED: %d restart-interrupted gateway "
            "boots within %ds (threshold %d). Skipping auto-resume to break "
            "a suspected SIGTERM-respawn loop (#30719). Restart-interrupted "
            "sessions stay resume-pending and will continue on the next real "
            "user message. If this is a false positive, delete %s.",
            len(boots),
            window_seconds,
            max_restarts,
            _state_path(),
        )
    return tripped
