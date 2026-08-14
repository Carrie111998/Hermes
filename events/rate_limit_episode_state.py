"""File-backed episode state store for model rate-limit incidents.

Persists (provider, model) rate-limit episode metadata across gateway
restarts so later tasks can coalesce rate-limit hits into semantic
episodes instead of emitting one alert per 429 response.

An episode is a contiguous period where a specific (provider, model)
pair is rate-limited. The store records:

  episode_opened_at: ISO8601 when the (provider, model) first hit the limit
  last_hit_at: ISO8601 when this provider/model was last rate-limited
  hit_count: number of rate-limit hits in this episode so far
  outcome: current outcome (diverted | chain_exhausted | no_fallback | recovered)
  reason: why the limit was hit (rate_limit | upstream_rate_limit | billing | ...)
  resets_at: ISO8601 when the provider's quota resets (if known)

Load/save are fail-open by design: a corrupt state file must never
block a model call, so _load_state() returns {} on ANY error (missing
file, malformed JSON, wrong type, etc.).

State is GLOBAL and NEVER profile-scoped, anchored at the canonical
~/.hermes root — when HERMES_HOME points to a profile directory, all
agents must see the same rate-limit episode state.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from events import paths
from utils import atomic_replace

logger = logging.getLogger(__name__)


def _load_state() -> Dict[str, Any]:
    """Load episode state from disk, returning {} on any error.

    Parses the JSON state file. On ANY error (missing file, JSON decode
    failure, wrong top-level type, unreadable), logs at debug level and
    returns {}. This is intentional: the telemetry store must never be
    able to block a model call.

    Returns:
        dict keyed by (provider, model) tuples (as strings), or {} if load fails.
    """
    state_path = paths.rate_limit_state_path()
    try:
        if not state_path.exists():
            return {}
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            logger.debug(
                "rate_limit_episode_state: top level is %s, not dict; returning {}",
                type(state).__name__,
            )
            return {}
        return state
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.debug("rate_limit_episode_state: JSON decode error: %s", e)
        return {}
    except (OSError, IOError) as e:
        logger.debug("rate_limit_episode_state: read error: %s", e)
        return {}
    except Exception as e:
        logger.debug("rate_limit_episode_state: unexpected error loading state: %s", e)
        return {}


def _save_state(state: Dict[str, Any]) -> bool:
    """Atomically save episode state to disk.

    Writes to a temp file then atomically replaces the target, so a
    crash mid-write does not corrupt the state. On any error, logs at
    debug level and returns False; the state file is optional.

    Args:
        state: dict keyed by (provider, model) tuples (as strings).

    Returns:
        True if write succeeded, False otherwise.
    """
    state_path = paths.rate_limit_state_path()
    try:
        # Ensure parent directory exists
        state_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file in the same directory
        fd, tmp_path = tempfile.mkstemp(
            dir=str(state_path.parent), suffix=".tmp", text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            # Atomically move temp file to target
            atomic_replace(tmp_path, state_path)
            return True
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("rate_limit_episode_state: write error: %s", e)
        return False
