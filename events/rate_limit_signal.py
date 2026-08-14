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
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Outcome severity ordering. Index = severity; higher wins and never downgrades
# within an episode. chain_exhausted and no_fallback are equally severe (both
# mean nothing absorbed the traffic); they are distinct only so the message can
# name the right remedy.
_SEVERITY = {"recovered": 0, "diverted": 1, "chain_exhausted": 2, "no_fallback": 2}

_state_cache: Optional[Dict[str, Any]] = None


def reset_state_cache() -> None:
    """Test hook: drop the in-process state cache."""
    global _state_cache
    _state_cache = None


def _state_path() -> Path:
    from events.paths import rate_limit_state_path
    return rate_limit_state_path()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _episode_key(provider: str, model: str) -> str:
    return f"{(provider or '').strip().lower()}/{(model or '').strip()}"


def _load_state() -> Dict[str, Any]:
    """Return the episode map. Fails open to {} on any error."""
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    try:
        path = _state_path()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _state_cache = data
    return data


def _save_state(state: Dict[str, Any]) -> bool:
    """Atomically persist the episode map. Returns True on success."""
    global _state_cache
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
        # _state_cache in place BEFORE _save_state ever runs. If _save_state
        # then raises, the outer except swallows it and returns False before
        # reaching _emit -- so nothing is persisted and nothing is emitted,
        # yet the cache already reads "already alerted at chain_exhausted".
        # Every later retry in this process would then be silently
        # suppressed by _should_alert, permanently losing an escalation that
        # should page the operator. A deep copy keeps mutations private
        # until a successful _save_state explicitly republishes them via its
        # own `_state_cache = state` assignment.
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
        _save_state(state)

        if not alert:
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
        return _emit(payload, _resolve_source(source_hint), bus)
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
        _save_state(state)
        payload = {
            "provider": provider, "model": model,
            "reason": "recovered", "detector": "runtime",
            "outcome": "recovered",
            "fallback_provider": "", "fallback_model": "",
            "resets_at": "",
            "diverted_calls": int(episode.get("diverted_calls", 0)),
            "episode_opened_at": episode.get("opened_at", ""),
        }
        return _emit(payload, _resolve_source(None), bus)
    except Exception:
        logger.debug("rate_limit_signal.clear failed (swallowed)", exc_info=True)
        return False
