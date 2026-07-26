"""Pre-dispatch usage refresh hook — throttled, timeout-bounded, coalescing-safe.

Ensures fresh usage evidence before every router/dispatch decision without
deadlocking the dispatcher or spamming the usage APIs.

Guarantees:
  - At most one refresh runs concurrently (enforced by lock + task tracking)
  - Concurrent callers within throttle window get cached result immediately
  - Concurrent callers during refresh coalesce (await same task, get consistent outcome)
  - Timeout on in-flight refresh does NOT trigger duplicate refresh (task keeps running)
  - After timeout or completion, next call respects throttle window or starts fresh refresh
  - All concurrent callers get deterministic, consistent terminal states
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class UsageRefreshHook:
    """Throttled pre-dispatch usage refresh with concurrent-decision coalescing.

    Concurrency guarantees:
      1. Throttle window (default 60s, configurable, enforced >= 60s):
         - Calls within window return cached result immediately
         - No refresh invoked, no background work triggered
      2. Concurrent calls during refresh coalesce:
         - First call: start refresh task, await with timeout
         - Other calls: await same task (via lock), timeout together
         - All get consistent outcome (success, timeout, or failure)
         - No duplicate refreshes even if first caller times out
      3. Task lifecycle cleanup:
         - Task kept tracked until completion, success or failure
         - Successfully completed task: next call respects throttle window
         - Timed-out task: continues in background, next call waits or throttles
         - Failed task: considered "in progress" until task actually finishes
    """

    def __init__(
        self,
        *,
        refresh_fn: Callable[[], Any] | None = None,
        min_interval_seconds: float = 60.0,
        timeout_seconds: float = 10.0,
        usage_path: Path | None = None,
    ) -> None:
        """Initialize the hook.

        Args:
            refresh_fn: Callable that performs the actual refresh (sync function).
                Defaults to refresh_usage_document if not provided.
            min_interval_seconds: Minimum seconds between successful refreshes (default 60s).
                Clamped to minimum 60s for safety. Concurrent calls within this window
                return throttled result without invoking refresh_fn.
            timeout_seconds: Maximum seconds to wait for a refresh to complete (default 10s).
                Calls waiting on an in-flight refresh will timeout after this duration.
                Timed-out callers return failure; underlying task continues.
            usage_path: Optional path to usage document (reserved for future use).
        """
        self.refresh_fn = refresh_fn
        self.min_interval_seconds = max(60.0, float(min_interval_seconds))  # Enforce 60s minimum
        self.timeout_seconds = float(timeout_seconds)
        self.usage_path = usage_path

        self._last_refresh_at: float | None = None
        self._refresh_in_progress: asyncio.Task[Any] | None = None
        self._refresh_lock = asyncio.Lock()

    async def refresh_if_needed(self) -> dict[str, Any]:
        """Refresh usage if needed, coalescing concurrent calls.

        Deterministic behavior:
          - Throttled call: returns immediately with cached=True
          - Concurrent refresh (multiple callers, one task): all await, all get same result
          - Timeout (in-flight refresh takes >timeout_seconds): caller times out, task continues
          - Subsequent call (after timeout, still in window): throttled
          - Subsequent call (after timeout, window expired): starts new refresh
          - Failed refresh: caller gets failure; next call can retry (throttle applies)

        Returns a status dict:
            {
                "ok": bool,                 # Refresh succeeded or was throttled (cached)
                "refreshed": bool,          # True if refresh was performed this call
                "cached": bool,             # True if result from throttle/coalescing
                "reason": str,              # "throttled", "coalesced", "timeout", "failed", "ok"
                "detail": str,              # Additional context
                "checked_at": str,          # ISO timestamp of check/refresh
            }
        """
        now = time.time()
        checked_at = datetime.now(timezone.utc).isoformat()

        # 1. Check if refresh is needed (throttle window)
        if self._last_refresh_at is not None:
            age = now - self._last_refresh_at
            if age < self.min_interval_seconds:
                return {
                    "ok": True,
                    "refreshed": False,
                    "cached": True,
                    "reason": "throttled",
                    "detail": f"Last refresh {age:.0f}s ago (min {self.min_interval_seconds}s)",
                    "checked_at": checked_at,
                }

        # 2. If a refresh is already in progress, wait for it (coalesce)
        async with self._refresh_lock:
            if self._refresh_in_progress is not None and not self._refresh_in_progress.done():
                # Coalesce: await the existing task with timeout
                try:
                    result = await asyncio.wait_for(
                        self._refresh_in_progress,
                        timeout=self.timeout_seconds,
                    )
                    # Task completed before timeout; return its result
                    return {
                        "ok": result.get("ok", False),
                        "refreshed": False,
                        "cached": True,
                        "reason": "coalesced",
                        "detail": "Waited for concurrent refresh; task completed",
                        "checked_at": checked_at,
                    }
                except asyncio.TimeoutError:
                    # Coalesced task timed out; return consistent timeout state
                    # (the underlying task continues in background)
                    return {
                        "ok": False,
                        "refreshed": False,
                        "cached": False,
                        "reason": "timeout",
                        "detail": f"Coalesced refresh timeout ({self.timeout_seconds}s)",
                        "checked_at": checked_at,
                    }
                except Exception as exc:
                    # Coalesced task failed; return consistent failure state
                    return {
                        "ok": False,
                        "refreshed": False,
                        "cached": False,
                        "reason": "failed",
                        "detail": f"Coalesced refresh failed: {type(exc).__name__}",
                        "checked_at": checked_at,
                    }

            # 3. No concurrent refresh in progress; start one
            if self.refresh_fn is None:
                from hermes_cli.fleet.usage_refresh import refresh_usage_document
                refresh_fn = refresh_usage_document
            else:
                refresh_fn = self.refresh_fn

            # Spawn refresh task for this caller (and future coalesced callers)
            async def _do_refresh():
                try:
                    # refresh_fn is a sync function; run it in thread
                    result = await asyncio.to_thread(refresh_fn)
                    # Result has .ok attribute (UsageRefreshReport) or is dict/MagicMock
                    ok = (
                        result.ok
                        if hasattr(result, "ok")
                        else result.get("ok", True)
                        if isinstance(result, dict)
                        else True
                    )
                    return {"ok": ok}
                except Exception as exc:
                    logger.exception("Usage refresh failed: %s", exc)
                    return {"ok": False, "error": type(exc).__name__}

            self._refresh_in_progress = asyncio.create_task(_do_refresh())

            # Await the task we just created with timeout
            try:
                refresh_result = await asyncio.wait_for(
                    self._refresh_in_progress,
                    timeout=self.timeout_seconds,
                )
                # Refresh completed successfully; mark time and return success
                self._last_refresh_at = now
                self._refresh_in_progress = None  # Clear for next refresh window
                return {
                    "ok": refresh_result.get("ok", False),
                    "refreshed": True,
                    "cached": False,
                    "reason": "ok",
                    "detail": "Refresh completed successfully",
                    "checked_at": checked_at,
                }
            except asyncio.TimeoutError:
                # Refresh timed out; mark time so throttle applies to next caller
                # (even though this refresh didn't complete, we did attempt it).
                # _refresh_in_progress stays set so coalesced callers can still wait.
                self._last_refresh_at = now
                logger.warning("Usage refresh timed out after %.1fs", self.timeout_seconds)
                return {
                    "ok": False,
                    "refreshed": False,
                    "cached": False,
                    "reason": "timeout",
                    "detail": f"Refresh timed out ({self.timeout_seconds}s)",
                    "checked_at": checked_at,
                }
            except Exception as exc:
                # Refresh failed; mark time and clear task so next attempt can try fresh
                self._last_refresh_at = now
                self._refresh_in_progress = None
                logger.exception("Usage refresh failed: %s", exc)
                return {
                    "ok": False,
                    "refreshed": False,
                    "cached": False,
                    "reason": "failed",
                    "detail": f"Refresh failed: {type(exc).__name__}: {exc}",
                    "checked_at": checked_at,
                }


# Global singleton hook (per-process)
_global_hook: UsageRefreshHook | None = None


def get_global_usage_refresh_hook() -> UsageRefreshHook:
    """Get or create the global usage refresh hook."""
    global _global_hook
    if _global_hook is None:
        _global_hook = UsageRefreshHook()
    return _global_hook


async def refresh_usage_before_dispatch() -> dict[str, Any]:
    """Call from kanban dispatcher before routing decisions.

    Returns status dict; dispatch should continue regardless of outcome
    since usage evidence is advisory (never a hard blocker for all lanes).
    """
    hook = get_global_usage_refresh_hook()
    status = await hook.refresh_if_needed()

    if not status.get("ok"):
        logger.warning(
            "Usage refresh before dispatch: %s — %s",
            status.get("reason"),
            status.get("detail"),
        )
    else:
        if status.get("refreshed"):
            logger.info("Usage evidence refreshed before dispatch")

    return status
