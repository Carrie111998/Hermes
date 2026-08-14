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
