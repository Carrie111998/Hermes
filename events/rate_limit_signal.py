"""Model rate-limit episode tracking and alerting.

Detectors across the agent runtime call :func:`record` when a model becomes
unavailable due to a rate limit, credit exhaustion, or usage cap. This module
coalesces those hits into *episodes* keyed ``(provider, model)`` and emits at
most one ``MODEL_RATE_LIMITED`` event per genuine change in the failure's
shape — so a two-hour outage produces one alert, not hundreds.

Design constraints (mirrors events/loop_fault.py):
* Best-effort: never raises, never blocks. Called from the agent's hot
  failover path. Every failure degrades to a debug log.
* Lazy bus access: the agent runtime has no events imports at module scope.
* Fail open: unreadable or malformed state means "no episode", never an
  exception and never a blocked model call.

State is deliberately FILE-backed rather than in-process: every cron spawns a
fresh process, so an in-memory cooldown (like ``agent._rate_limited_until``)
dies with it and each run rediscovers the same rate limit. Generalizes the
pattern already proven in agent/nous_rate_guard.py.

Cache coherence rule
--------------------
Two requirements pull in opposite directions:

* Losing persistence must never AMPLIFY alerts. If the cache were dropped
  whenever a write failed, every subsequent hit would re-read ``{}``,
  ``_should_alert(None, ...)`` would return True, and the anti-flood feature
  would itself become the flood — one HIGH-priority Telegram message (and, at
  ``chain_exhausted``, one WhatsApp page) per API call.
* A long-lived process must still SEE episodes other processes wrote. The
  gateway runs for days; crons are one-shot. A cache that is never refreshed
  makes every gateway write a whole-file replace built from a stale snapshot,
  silently deleting the crons' episodes and re-alerting them as brand new.

The rule that satisfies both: the cache is a snapshot ANCHORED to the state
file's identity marker ``(st_mtime_ns, st_size)`` as of the last moment this
process was in sync with the file.

  * no cache yet                      -> read the file, anchor the marker
  * marker == the file's current stat -> nobody has touched the file since we
                                         synced, so the cache IS the truth
                                         (this is what preserves a mutation the
                                         disk rejected: a failed write leaves
                                         the file — and therefore the marker —
                                         untouched)
  * marker != the file's current stat -> another process wrote; re-read and
                                         re-anchor, which is what makes foreign
                                         episodes visible

A FAILED write therefore publishes its mutated state into the cache but
deliberately does NOT advance the marker. A SUCCESSFUL write publishes and
re-anchors. A failed READ also anchors, so a permanently corrupt or unreadable
file degrades to a cached ``{}`` rather than re-reading (and re-alerting) on
every hit.

Known imperfections, stated rather than hidden:

1. If our write fails and a FOREIGN process then writes the file, the marker
   changes, we reload from disk, and our un-persisted mutation is discarded —
   which can permit one extra alert. That is bounded by foreign writes, not by
   hit rate, so it cannot flood.
2. ``(st_mtime_ns, st_size)`` can collide if a foreign write lands within the
   filesystem's timestamp granularity AND produces a byte-identical length. The
   consequence is one missed refresh — strictly better than today's "never
   refresh".
3. The marker is not a lock. Two processes writing concurrently still last-write-
   wins; this rule narrows the window from "the whole process lifetime" to "one
   read-modify-write", it does not close it.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Outcome severity ordering. Index = severity; higher wins and never downgrades
# within an episode. chain_exhausted and no_fallback are equally severe (both
# mean nothing absorbed the traffic); they are distinct only so the message can
# name the right remedy.
_SEVERITY = {"recovered": 0, "diverted": 1, "chain_exhausted": 2, "no_fallback": 2}

# Hard upper bound on how long an episode may sit in the state file.
#
# Two of the three detectors write into namespaces that hook D can NEVER clear:
# credential_pool records ``<provider>:pool`` and the Nous guard records
# ``nous/nous-portal``, while clear() is only ever called with the agent's real
# provider/model slugs. Without a TTL those keys are write-only — once such an
# episode reaches its top ``alerted_level``, ``_should_alert`` suppresses every
# future hit of that shape FOREVER, across process restarts, because the state
# file is global and this module is its only reader or writer. The same absence
# lets the file grow without bound.
#
# 6h is chosen to sit just above the longest provider window we actually meet:
# Nous RPH is 1h, and the Codex/Anthropic-style rolling quota is 5h. A limit
# that genuinely outlives 6h (a weekly cap) therefore re-alerts about four
# times a day, which is the honest behavior for an outage nothing has cleared —
# whereas a TTL below 5h would re-alert mid-window and re-create the flood this
# module exists to prevent.
_EPISODE_MAX_AGE_SECONDS = 6 * 60 * 60

_state_cache: Optional[Dict[str, Any]] = None
# (st_mtime_ns, st_size) of the state file as of the last time this process was
# in sync with it -- see the "Cache coherence rule" section of the module
# docstring. None means "the file did not exist / could not be stat'd".
_state_stat: Optional[Tuple[int, int]] = None


def reset_state_cache() -> None:
    """Test hook: drop the in-process state cache and its file anchor."""
    global _state_cache, _state_stat
    _state_cache = None
    _state_stat = None


def _state_path() -> Path:
    from events.paths import rate_limit_state_path
    return rate_limit_state_path()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _episode_key(provider: str, model: str) -> str:
    return f"{(provider or '').strip().lower()}/{(model or '').strip()}"


def _stat_marker() -> Optional[Tuple[int, int]]:
    """Identity of the state file right now, or None if it is not there."""
    try:
        st = os.stat(str(_state_path()))
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return None


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_expired(episode: Any, now: datetime) -> bool:
    """Whether an episode should be forgotten. See _EPISODE_MAX_AGE_SECONDS."""
    if not isinstance(episode, dict):
        return True
    resets_at = _parse_iso(episode.get("resets_at"))
    if resets_at is not None and resets_at <= now:
        return True
    opened_at = _parse_iso(episode.get("opened_at"))
    if opened_at is None:
        # No parseable age means the entry cannot be bounded. Forget it: the
        # failure mode of keeping it is PERMANENT alert suppression, which is
        # strictly worse than one extra alert.
        return True
    return (now - opened_at) >= timedelta(seconds=_EPISODE_MAX_AGE_SECONDS)


def _reap_expired(state: Dict[str, Any]) -> Dict[str, Any]:
    """Drop expired episodes. Returns ``state`` itself when nothing expired."""
    now = datetime.now(timezone.utc)
    live = {k: v for k, v in state.items() if not _is_expired(v, now)}
    return state if len(live) == len(state) else live


def _load_state() -> Dict[str, Any]:
    """Return the (reaped) episode map. Fails open to {} on any error.

    Refreshes from disk whenever the file's identity marker no longer matches
    the one we anchored on, and only then.
    """
    global _state_cache, _state_stat
    marker = _stat_marker()
    if _state_cache is None or marker != _state_stat:
        try:
            with open(str(_state_path()), "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        _state_cache = data
        # Anchor even on a failed read: a corrupt or unreadable file must
        # degrade to a cached {} rather than being re-read (and re-alerted on)
        # once per hit.
        _state_stat = marker
    reaped = _reap_expired(_state_cache)
    if reaped is not _state_cache:
        # The file still carries the expired entries; the next successful write
        # is what prunes them from disk. Leave the marker alone -- the FILE did
        # not change, only our view of which entries still count.
        _state_cache = reaped
    return _state_cache


def _publish_unsaved(state: Dict[str, Any]) -> None:
    """Adopt a mutation the disk rejected, WITHOUT re-anchoring the marker.

    This is the anti-flood half of the coherence rule: the write failed, so the
    file is unchanged and the marker still matches it, which means the next
    _load_state() hands this mutated state straight back instead of re-reading
    an empty/stale file and treating every subsequent hit as brand new.
    """
    global _state_cache
    _state_cache = state


def _save_state(state: Dict[str, Any]) -> bool:
    """Atomically persist the episode map. Returns True on success."""
    global _state_cache, _state_stat
    try:
        path = _state_path()
        state_dir = os.path.dirname(str(path))
        os.makedirs(state_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            from utils import atomic_replace
            atomic_replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _state_cache = state
        _state_stat = _stat_marker()
        return True
    except Exception:
        logger.debug("rate_limit_signal: state write failed (swallowed)", exc_info=True)
        return False


def _should_alert(episode: Optional[Dict[str, Any]], outcome: str,
                  fallback_key: str) -> bool:
    """Decide whether this hit is worth an alert.

    Fires on exactly three transitions:
      1. a brand-new episode;
      2. the outcome worsens past what was last alerted (diverted ->
         chain_exhausted / no_fallback), which is WARN becoming ACT;
      3. a NEW fallback target appears — the leading indicator that the
         previous absorber has also died, caught while still degrading
         rather than after it breaks.

    Everything else folds into diverted_calls silently.
    """
    if not episode:
        return True

    alerted = (episode.get("alerted_level") or "").strip().lower()
    if _SEVERITY.get(outcome, 0) > _SEVERITY.get(alerted, 0):
        return True

    if fallback_key and fallback_key not in (episode.get("fallbacks_seen") or []):
        return True

    return False


def _resolve_source(source_hint: Optional[str]) -> str:
    """Best-effort canonical identity of whoever hit the limit.

    Mirrors events/loop_fault.py::_resolve_source so both land in the same
    watchdog/alerts taxonomy.
    """
    raw = (
        os.environ.get("HERMES_CRON_JOB_NAME")
        or os.environ.get("HERMES_AGENT_SOURCE")
        or (source_hint or "")
    )
    raw = raw.strip().strip("[]").strip()
    if not raw:
        raw = "agent-loop"
    try:
        from events.producers.agent_source_mapping import canonical_agent_source
        return canonical_agent_source(raw)
    except Exception:
        return raw


def _alerts_enabled() -> bool:
    return (os.environ.get("HERMES_RATE_LIMIT_ALERTS", "1").strip() != "0")


def _emit(payload: Dict[str, Any], source: str, bus: Any) -> bool:
    from events.schema import EventType, Priority
    if bus is None:
        from events.bus import EventBus
        bus = EventBus()
    bus.emit(
        event_type=EventType.MODEL_RATE_LIMITED,
        source=source,
        payload=payload,
        priority=Priority.HIGH,
    )
    return True


def record(
    *,
    provider: str,
    model: str,
    reason: str,
    detector: str,
    outcome: str = "diverted",
    fallback_provider: str = "",
    fallback_model: str = "",
    resets_at: str = "",
    source_hint: Optional[str] = None,
    bus: Any = None,
) -> bool:
    """Record a rate-limit hit. Returns True if an alert was emitted.

    Never raises: this is called from the agent's hot failover path, and a
    telemetry defect must never take down a model call.
    """
    try:
        if not _alerts_enabled():
            return False

        key = _episode_key(provider, model)
        fallback_key = (
            _episode_key(fallback_provider, fallback_model)
            if fallback_provider and fallback_model else ""
        )
        # MUST be a deep copy, not dict(...). _load_state() returns the same
        # cached dict object on every call; a shallow copy shares the nested
        # per-episode dicts with that cache. Mutating episode[...] below
        # (diverted_calls, alerted_level, fallbacks_seen) would then corrupt
        # _state_cache in place BEFORE the outcome of this call is known. If
        # anything between here and _emit raises, the outer except swallows it
        # and returns False -- so nothing is persisted and nothing is emitted,
        # yet the cache would already read "already alerted at
        # chain_exhausted". Every later retry in this process would then be
        # silently suppressed by _should_alert, permanently losing an
        # escalation that should page the operator. A deep copy keeps mutations
        # private until this call REACHES A DECISION and republishes them --
        # via _save_state on success, or via _publish_unsaved once we know
        # whether the alert actually went out.
        state = copy.deepcopy(_load_state())
        episode = state.get(key)
        alert = _should_alert(episode, outcome, fallback_key)

        if episode is None:
            episode = {
                "provider": provider, "model": model,
                "opened_at": _now_iso(), "resets_at": resets_at,
                "worst_outcome": outcome, "alerted_level": "",
                "diverted_calls": 0, "fallbacks_seen": [],
            }

        episode["diverted_calls"] = int(episode.get("diverted_calls", 0)) + 1
        if resets_at:
            episode["resets_at"] = resets_at
        if _SEVERITY.get(outcome, 0) > _SEVERITY.get(
            episode.get("worst_outcome", ""), 0
        ):
            episode["worst_outcome"] = outcome
        if fallback_key and fallback_key not in episode["fallbacks_seen"]:
            episode["fallbacks_seen"].append(fallback_key)

        if alert:
            episode["alerted_level"] = (
                episode["worst_outcome"] if _SEVERITY.get(outcome, 0)
                >= _SEVERITY.get(episode.get("alerted_level", ""), 0)
                else episode.get("alerted_level", "")
            )

        state[key] = episode
        saved = _save_state(state)

        if not alert:
            if not saved:
                _publish_unsaved(state)
            return False

        payload = {
            "provider": provider, "model": model,
            "reason": reason, "detector": detector,
            "outcome": outcome,
            "fallback_provider": fallback_provider,
            "fallback_model": fallback_model,
            "resets_at": episode.get("resets_at", ""),
            "diverted_calls": episode["diverted_calls"],
            "episode_opened_at": episode["opened_at"],
        }
        emitted = _emit(payload, _resolve_source(source_hint), bus)
        if not saved:
            # The alert went out but the disk refused it. Adopt the mutation
            # anyway so the NEXT hit is coalesced against it: without this, a
            # write failure turns the anti-flood store into a flood generator
            # (one HIGH alert -- and one WhatsApp page at chain_exhausted --
            # per API call). Deliberately after _emit: an alert we did not
            # actually deliver must not be recorded as delivered, which is what
            # keeps a genuine escalation retryable once the disk recovers.
            _publish_unsaved(state)
        return emitted
    except Exception:
        logger.debug("rate_limit_signal.record failed (swallowed)", exc_info=True)
        return False


def clear(*, provider: str, model: str, bus: Any = None) -> bool:
    """Close an open episode after a confirmed success. Returns True if it
    emitted a RECOVERED event (i.e. there was an episode to close).

    This is the ONLY recovery signal. Every other hook fires on failure, so a
    passive "no hits for N minutes" heuristic would declare recovery during
    any quiet window where nothing was scheduled, then re-alert on the next
    cron run.
    """
    try:
        if not _alerts_enabled():
            return False
        key = _episode_key(provider, model)
        state = dict(_load_state())
        episode = state.pop(key, None)
        if episode is None:
            return False
        saved = _save_state(state)
        payload = {
            "provider": provider, "model": model,
            "reason": "recovered", "detector": "runtime",
            "outcome": "recovered",
            "fallback_provider": "", "fallback_model": "",
            "resets_at": "",
            "diverted_calls": int(episode.get("diverted_calls", 0)),
            "episode_opened_at": episode.get("opened_at", ""),
        }
        emitted = _emit(payload, _resolve_source(None), bus)
        if not saved:
            # Same reasoning as record(): hook D calls clear() after EVERY
            # successful API call, so a store that keeps handing back the
            # already-closed episode re-emits RECOVERED once per call.
            _publish_unsaved(state)
        return emitted
    except Exception:
        logger.debug("rate_limit_signal.clear failed (swallowed)", exc_info=True)
        return False
