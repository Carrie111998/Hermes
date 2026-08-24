"""Session heartbeats — recurring re-entry prompts for the current session.

A heartbeat is one user-owned recurring instruction bound to a session
(`/heartbeat every 10m Check the deployment and report meaningful changes`).
When due AND the session is idle, the prompt is injected as a normal user
turn — same mechanism as a /goal continuation, so message-role alternation
and prompt caching are untouched. If the agent is busy at the due moment,
the tick coalesces: it fires once when the session next goes idle, never
stacking a backlog.

This is deliberately session-scoped and in-process (CLI process or gateway
process must be running) — the durable cross-process scheduling surface
remains ``hermes cron`` / the ``cronjob`` tool, which runs in isolated
sessions. A heartbeat is for "keep re-entering THIS conversation", the
cron system is for "run this job on a schedule". Distinct by design.

State is persisted in SessionDB ``state_meta`` keyed by
``heartbeat:<session_id>`` so ``/resume`` picks it up.

Invariants (mirrors goals.py):
- Injection is a plain user message. No system-prompt mutation, no toolset
  swap — prompt caching stays intact.
- A real user message always wins: heartbeats only fire into an idle
  session with an empty input queue.
- Failures are contained: any DB/import error degrades to "no heartbeat",
  never to a crashed input loop.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Floor: a heartbeat that re-enters the session more often than once a
# minute is a busy-loop, not a heartbeat. (Prime-Agent uses a similar floor.)
MIN_INTERVAL_SECONDS = 60
# How often drivers poll for due heartbeats. Not user-facing.
POLL_SECONDS = 5.0

# Claim-timeout fallback (seconds): how long a claimed-but-unconfirmed tick
# may stay in flight before it is abandoned with a warning, counted in
# missed_count, and left due for retry (issue #92837 expectation #3: a
# claimed tick that produces no turn must be loud, never silent). The
# authoritative default lives in the config defaults
# (hermes_cli.config_defaults.DEFAULT_CONFIG["heartbeat"][
# "claim_timeout_seconds"]); this constant only covers the degraded case
# where the config can't be read. Drivers can also pin a value per manager
# via HeartbeatManager(claim_timeout_seconds=...).
_CLAIM_TIMEOUT_FALLBACK_SECONDS = 300.0


def _default_claim_timeout_seconds() -> float:
    """Resolve the heartbeat claim timeout from config (best-effort).

    Reads ``heartbeat.claim_timeout_seconds`` from the user config; on any
    failure (unreadable config, missing key, bad value) falls back to
    :data:`_CLAIM_TIMEOUT_FALLBACK_SECONDS`. Never raises.
    """
    try:
        from hermes_cli.config import load_config_readonly

        hb_cfg = (load_config_readonly() or {}).get("heartbeat") or {}
        val = hb_cfg.get("claim_timeout_seconds")
        if val is not None:
            return float(val)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("HeartbeatManager: claim-timeout config read failed: %s", exc)
    return _CLAIM_TIMEOUT_FALLBACK_SECONDS


# Import time of this module — a proxy for "this process started". A
# persisted claim older than this timestamp was made by a previous process
# that died between claiming a tick and confirming/abandoning it, so the
# claim can never be resolved and must be treated as a missed delivery.
# Best-effort: wall clock only, so a backwards NTP step after import makes
# this process's own later claims (recorded on the stepped-back clock) look
# older than the start marker. Only claims older than start minus the skew
# tolerance below are treated as orphans; steps larger than the tolerance
# are out of scope for this proxy.
_PROCESS_START_TS = time.time()
# Wall-clock skew tolerance (seconds) for the previous-process orphan test
# above: a claim recorded up to this far before _PROCESS_START_TS still
# reads as a live-process claim, absorbing backwards NTP steps.
_PROCESS_START_SKEW_TOLERANCE_SECONDS = 120.0

HEARTBEAT_PROMPT_TEMPLATE = (
    "[Heartbeat — recurring instruction, fires every {interval}]\n"
    "{prompt}\n\n"
    "If there is nothing meaningful to do or report for this instruction "
    "right now, reply briefly that nothing has changed and stop — do not "
    "invent work."
)

_INTERVAL_RE = re.compile(
    r"^\s*(?:every\s+)?(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def parse_interval(text: str) -> Optional[int]:
    """Parse ``10m`` / ``every 2h`` / ``every 90 minutes`` into seconds.

    Returns None when the text is not an interval. Values below
    ``MIN_INTERVAL_SECONDS`` are rejected (returns -1 so callers can
    distinguish "not an interval" from "too small").
    """
    if not text:
        return None
    m = _INTERVAL_RE.match(text)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()
    seconds = int(value * _UNIT_SECONDS[unit])
    if seconds < MIN_INTERVAL_SECONDS:
        return -1
    return seconds


def format_interval(seconds: int) -> str:
    """Human-readable interval (``600`` → ``10m``)."""
    seconds = int(seconds)
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


@dataclass
class HeartbeatState:
    """Serializable per-session heartbeat.

    Firing is split into three phases so the persisted audit trail never
    claims a delivery that did not happen (issue #92837):

    - ``due_prompt()`` *claims* a due tick (``claimed_at``) without
      counting a fire.
    - ``confirm_delivery()`` records the fire *only* after the prompt was
      actually handed to a live input path / consumed by a turn.
    - ``abandon_claim()`` counts the claim as missed and leaves the tick
      due so the next poll retries.
    """

    prompt: str
    interval_seconds: int
    status: str = "active"          # active | paused | cleared
    created_at: float = 0.0
    last_fired_at: float = 0.0
    fire_count: int = 0
    # Set while a due tick is claimed but its delivery is unconfirmed.
    claimed_at: Optional[float] = None
    # When the last confirmed delivery actually happened (turn enqueued /
    # consumed), as opposed to when it was merely claimed.
    last_delivered_at: float = 0.0
    # Claims that were never confirmed delivered (dropped handoff, vanished
    # staged prompt, crash between claim and confirm).
    missed_count: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "HeartbeatState":
        data = json.loads(raw)
        return cls(
            prompt=str(data.get("prompt") or ""),
            interval_seconds=int(data.get("interval_seconds", 0) or 0),
            status=str(data.get("status") or "active"),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            last_fired_at=float(data.get("last_fired_at", 0.0) or 0.0),
            fire_count=int(data.get("fire_count", 0) or 0),
            claimed_at=(
                float(data["claimed_at"]) if data.get("claimed_at") is not None else None
            ),
            last_delivered_at=float(data.get("last_delivered_at", 0.0) or 0.0),
            missed_count=int(data.get("missed_count", 0) or 0),
        )

    def is_due(self, now: Optional[float] = None) -> bool:
        if self.status != "active" or not self.prompt or self.interval_seconds <= 0:
            return False
        now = now if now is not None else time.time()
        anchor = self.last_fired_at or self.created_at
        return (now - anchor) >= self.interval_seconds

    def render_prompt(self) -> str:
        return HEARTBEAT_PROMPT_TEMPLATE.format(
            interval=format_interval(self.interval_seconds),
            prompt=self.prompt,
        )


# ──────────────────────────────────────────────────────────────────────
# Persistence (SessionDB state_meta) — same pattern as goals.py
# ──────────────────────────────────────────────────────────────────────


def _meta_key(session_id: str) -> str:
    return f"heartbeat:{session_id}"


def _get_session_db() -> Optional[Any]:
    # Reuse the goals module's per-HERMES_HOME cached SessionDB so both
    # features share one connection instead of thrashing the file.
    try:
        from hermes_cli.goals import _get_session_db as _goals_db

        return _goals_db()
    except Exception as exc:  # pragma: no cover
        logger.debug("HeartbeatManager: SessionDB bootstrap failed (%s)", exc)
        return None


def load_heartbeat(session_id: str) -> Optional[HeartbeatState]:
    if not session_id:
        return None
    db = _get_session_db()
    if db is None:
        return None
    try:
        raw = db.get_meta(_meta_key(session_id))
    except Exception as exc:
        logger.debug("HeartbeatManager: get_meta failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        state = HeartbeatState.from_json(raw)
    except Exception as exc:
        logger.warning("HeartbeatManager: could not parse stored heartbeat for %s: %s", session_id, exc)
        return None
    return None if state.status == "cleared" else state


def save_heartbeat(session_id: str, state: HeartbeatState) -> None:
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        from hermes_cli.goals import _warn_dropped_write

        _warn_dropped_write("HeartbeatManager", "heartbeat", session_id)
        return
    try:
        db.set_meta(_meta_key(session_id), state.to_json())
    except Exception as exc:
        logger.debug("HeartbeatManager: set_meta failed: %s", exc)


# Sentinel for HeartbeatManager(..., state=...): "not provided, load from
# disk" must stay distinguishable from None ("no heartbeat persisted").
_UNSET_STATE = object()


class HeartbeatLoadCache:
    """mtime-checked cache of heartbeat states for poll-loop reuse.

    The gateway poll constructs a HeartbeatManager per watched session
    every ``POLL_SECONDS``; each construction otherwise re-reads the state
    from SessionDB on the event-loop thread. This cache keeps repeated
    polls O(1) in disk I/O: states are reloaded only when the SessionDB
    files (``state.db`` / ``state.db-wal``) changed since the last load,
    so a stable poll across N watches does zero DB reads, and a poll that
    follows a DB change re-reads each state at most once.

    Cached HeartbeatState objects are handed to managers by reference;
    mutations are persisted through save_heartbeat, which bumps the DB
    mtime and thereby invalidates the cache on the next load.
    """

    def __init__(self) -> None:
        self._fingerprint: Optional[tuple] = None
        self._states: Dict[str, Optional[HeartbeatState]] = {}

    def _db_fingerprint(self) -> Optional[tuple]:
        db = _get_session_db()
        if db is None:
            return None
        try:
            main = Path(str(db.db_path))
            wal = Path(str(db.db_path) + "-wal")
            parts: list = [main.stat().st_mtime_ns, main.stat().st_size]
            try:
                parts.append(wal.stat().st_mtime_ns)
                parts.append(wal.stat().st_size)
            except OSError:
                # No WAL file (yet): nothing to add.
                parts.append(0)
                parts.append(0)
            return tuple(parts)
        except (AttributeError, OSError, TypeError):  # pragma: no cover - defensive
            return None

    def load(self, session_id: str) -> Optional[HeartbeatState]:
        fingerprint = self._db_fingerprint()
        if fingerprint is None:
            # No SessionDB handle to fingerprint: the cache cannot stay
            # fresh, fall through to the uncached load.
            return load_heartbeat(session_id)
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._states = {}
        if session_id in self._states:
            return self._states[session_id]
        state = load_heartbeat(session_id)
        self._states[session_id] = state
        return state


# ──────────────────────────────────────────────────────────────────────
# Manager — the surface CLI + gateway talk to
# ──────────────────────────────────────────────────────────────────────


class HeartbeatManager:
    """Per-session heartbeat state + due-tick decisions.

    Drivers (CLI thread / gateway task) call :meth:`due_prompt` on a poll
    cadence while the session is idle; a non-None return is the user-role
    message to inject.

    A non-None return only *claims* the tick (``claimed_at``); it does not
    count a fire. The driver MUST call :meth:`confirm_delivery` once the
    prompt is actually handed to a live input path (CLI input queue) or
    consumed by a turn (gateway pending-slot drain), or
    :meth:`abandon_claim` when the handoff fails. This keeps the persisted
    ``fire_count``/``last_fired_at`` truthful: a claimed-but-undelivered
    tick is reported as missed instead of silently counted as fired
    (#92837). The in-flight claim also prevents overlapping polls from
    double-claiming the same tick, so no backlog can pile up.
    """

    def __init__(
        self,
        session_id: str,
        claim_timeout_seconds: Optional[float] = None,
        state: Optional[HeartbeatState] = _UNSET_STATE,
    ):
        self.session_id = session_id
        # How long a claimed tick may wait for a turn before it is
        # abandoned with a warning, counted missed, and re-claimed. None
        # resolves the config default (heartbeat.claim_timeout_seconds).
        self.claim_timeout_seconds = (
            float(claim_timeout_seconds)
            if claim_timeout_seconds is not None
            else _default_claim_timeout_seconds()
        )
        # state=... lets hot loops (gateway poll) inject a HeartbeatLoadCache
        # hit and skip the synchronous disk read; anything else loads fresh.
        if state is _UNSET_STATE:
            state = load_heartbeat(session_id)
        self._state: Optional[HeartbeatState] = state

    @property
    def state(self) -> Optional[HeartbeatState]:
        return self._state

    def has_heartbeat(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def status_line(self) -> str:
        s = self._state
        if s is None:
            return "No heartbeat. Set one with /heartbeat every <interval> <prompt>."
        every = format_interval(s.interval_seconds)
        fired = f", fired {s.fire_count}×" if s.fire_count else ""
        if s.missed_count:
            fired += f", missed {s.missed_count}×"
        if s.status == "active":
            anchor = s.last_fired_at or s.created_at
            next_in = max(0, int(anchor + s.interval_seconds - time.time()))
            return f"♥ Heartbeat (every {every}, next in ~{next_in}s{fired}): {s.prompt}"
        if s.status == "paused":
            return f"⏸ Heartbeat (paused, every {every}{fired}): {s.prompt}"
        return f"Heartbeat ({s.status}, every {every}{fired}): {s.prompt}"

    # --- mutation -----------------------------------------------------

    def set(self, prompt: str, interval_seconds: int) -> HeartbeatState:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("heartbeat prompt is empty")
        interval_seconds = int(interval_seconds)
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS}s")
        state = HeartbeatState(
            prompt=prompt,
            interval_seconds=interval_seconds,
            status="active",
            created_at=time.time(),
        )
        self._state = state
        save_heartbeat(self.session_id, state)
        return state

    def pause(self) -> Optional[HeartbeatState]:
        if not self._state:
            return None
        self._state.status = "paused"
        # Drop any in-flight claim: a paused heartbeat must not deliver,
        # and the claimed prompt will not be confirmed.
        self._state.claimed_at = None
        save_heartbeat(self.session_id, self._state)
        return self._state

    def resume(self) -> Optional[HeartbeatState]:
        if not self._state:
            return None
        self._state.status = "active"
        # Re-anchor so resuming doesn't instantly fire a stale tick.
        self._state.last_fired_at = time.time()
        self._state.claimed_at = None
        save_heartbeat(self.session_id, self._state)
        return self._state

    def clear(self) -> bool:
        if self._state is None:
            return False
        self._state.status = "cleared"
        save_heartbeat(self.session_id, self._state)
        self._state = None
        return True

    # --- driver entry points --------------------------------------------

    def due_prompt(self, now: Optional[float] = None) -> Optional[str]:
        """Return the injection prompt if the heartbeat is due, else None.

        A non-None return only *claims* the tick — ``fire_count`` and
        ``last_fired_at`` are deliberately NOT advanced here. The driver
        must call :meth:`confirm_delivery` once the prompt is accepted into
        a live input path (or consumed by a turn), or :meth:`abandon_claim`
        when the handoff fails.

        While a claim is in flight (claimed but unconfirmed), further polls
        return None so overlapping polls can never double-claim the same
        tick, and missed intervals coalesce into one delivery instead of a
        backlog. Two in-flight hazards are resolved here instead of
        stalling silently (issue #92837):

        - A claim left behind by a previous process (crash between claim
          and handoff) is logged as a missed delivery and cleared.
        - A claim from a LIVE process that produced no turn within
          ``claim_timeout_seconds`` (staged prompt stuck with no consumer:
          idle-evicted session, vanished drain, wedged input queue) is
          abandoned with a warning, counted in ``missed_count``, and the
          still-due tick is re-claimed in the same call.
        """
        s = self._state
        if s is None or not s.is_due(now):
            return None
        now = now if now is not None else time.time()
        if s.claimed_at is not None:
            if (
                s.claimed_at
                < _PROCESS_START_TS - _PROCESS_START_SKEW_TOLERANCE_SECONDS
            ):
                # The claiming process died before confirming/abandoning.
                # The tick never became a turn — count it missed, surface
                # the loss, and let the next poll re-claim the still-due
                # tick instead of silently stalling. (Claims inside the
                # skew tolerance stay live-process claims: wall-clock
                # claims recorded after a backwards NTP step can read
                # slightly older than _PROCESS_START_TS without being
                # orphans.)
                logger.warning(
                    "HeartbeatManager: session %s heartbeat tick was claimed at %.0f "
                    "but never confirmed delivered (previous process died mid-handoff); "
                    "counting as missed and keeping the tick due",
                    self.session_id,
                    s.claimed_at,
                )
                s.missed_count += 1
                s.claimed_at = None
                save_heartbeat(self.session_id, s)
                # The tick is still due: leave it for the next poll to
                # re-claim (mirrors the pre-claim-timeout contract).
                return None
            elif now - s.claimed_at >= self.claim_timeout_seconds:
                # Live-process claim that never became a turn within the
                # claim window. Abandon loudly (warning + missed_count)
                # and fall through: the tick is still due and gets a
                # fresh claim below, so the heartbeat keeps trying
                # instead of hanging forever with no signal.
                self.abandon_claim(
                    f"no turn consumed the claimed tick within "
                    f"{self.claim_timeout_seconds:.0f}s"
                )
            else:
                # In-flight claim from this process: the driver has not
                # resolved it yet — never claim the same tick twice.
                return None
        s.claimed_at = now
        save_heartbeat(self.session_id, s)
        return s.render_prompt()

    def confirm_delivery(self, now: Optional[float] = None) -> bool:
        """Record the fire for the in-flight claim after real delivery.

        The ONLY place ``fire_count`` and ``last_fired_at`` advance. Call
        this after the claimed prompt was accepted into a live input path
        (CLI input queue) or accepted into the live pipeline (gateway, at
        the turn's acceptance boundary in ``_handle_message`` — turn
        START, not completion; do NOT move this call post-turn or
        unconfirmed claims would stall again). Returns True when a claim
        was pending; False when there was nothing to confirm (e.g. the
        claim was already abandoned, or the heartbeat was paused in
        between — in which case the delivery is ignored).
        """
        s = self._state
        if s is None or s.claimed_at is None:
            return False
        ts = now if now is not None else time.time()
        s.last_fired_at = s.claimed_at
        s.fire_count += 1
        s.last_delivered_at = ts
        s.claimed_at = None
        save_heartbeat(self.session_id, s)
        return True

    def abandon_claim(self, reason: str = "") -> bool:
        """Give up on the in-flight claim and count it as a missed delivery.

        The fire counters are untouched, so the persisted audit trail
        reports the truth: the tick was due, was handed off, but never
        became a turn. The tick stays due and is re-claimed on the next
        poll. Returns True when a claim was pending.
        """
        s = self._state
        if s is None or s.claimed_at is None:
            return False
        s.missed_count += 1
        s.claimed_at = None
        save_heartbeat(self.session_id, s)
        logger.warning(
            "HeartbeatManager: session %s heartbeat tick claimed but never became "
            "a turn%s; counting as missed and keeping the tick due",
            self.session_id,
            f": {reason}" if reason else "",
        )
        return True


def migrate_heartbeat_to_session(old_session_id: str, new_session_id: str) -> bool:
    """Carry a heartbeat across a compression session rotation.

    Same shape as ``goals.migrate_goal_to_session`` — copy to the child,
    archive the parent row, never raise.
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        state = load_heartbeat(old_session_id)
        if state is None:
            return False
        if load_heartbeat(new_session_id) is not None:
            return False
        save_heartbeat(new_session_id, state)
        state.status = "cleared"
        save_heartbeat(old_session_id, state)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("HeartbeatManager: migration failed: %s", exc)
        return False


__all__ = [
    "HeartbeatState",
    "HeartbeatManager",
    "parse_interval",
    "format_interval",
    "load_heartbeat",
    "save_heartbeat",
    "migrate_heartbeat_to_session",
    "HEARTBEAT_PROMPT_TEMPLATE",
    "MIN_INTERVAL_SECONDS",
    "POLL_SECONDS",
]
