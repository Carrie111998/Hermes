"""Pre-dispatch usage refresh hook — throttled, timeout-bounded, coalescing-safe.

Ensures fresh usage evidence before every router/dispatch decision without
deadlocking the dispatcher or spamming the usage APIs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class UsageRefreshHook:
    """Throttled pre-dispatch usage refresh with concurrent-decision coalescing."""

    def __init__(
        self,
        *,
        refresh_fn: Callable[[], Any] | None = None,
        min_interval_seconds: float = 300.0,  # 5 minutes default
        timeout_seconds: float = 10.0,
        usage_path: Path | None = None,
    ) -> None:
        """Initialize the hook.

        Args:
            refresh_fn: Callable that performs the actual refresh
                (defaults to refresh_usage_document)
            min_interval_seconds: Minimum time between refreshes (default 5 min)
            timeout_seconds: Maximum time to wait for a refresh (default 10s)
            usage_path: Path to usage document (for checking staleness)
        """
        self.refresh_fn = refresh_fn
        self.min_interval_seconds = max(60.0, float(min_interval_seconds))
        self.timeout_seconds = float(timeout_seconds)
        self.usage_path = usage_path

        self._last_refresh_at: float | None = None
        self._refresh_in_progress: asyncio.Task[Any] | None = None
        self._refresh_lock = asyncio.Lock()

    async def refresh_if_needed(self) -> dict[str, Any]:
        """Refresh usage if needed, coalescing concurrent calls.

        Returns a status dict:
            {
                "ok": bool,
                "refreshed": bool,  # True if refresh was performed
                "cached": bool,     # True if result was from throttle cache
                "reason": str,      # "throttled", "timeout", "failed", "ok"
                "detail": str,      # Additional context
                "checked_at": str,  # ISO timestamp of refresh/check
            }
        """
        now = time.time()
        checked_at = datetime.now(timezone.utc).isoformat()

        # 1. Check if refresh is needed (throttle)
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
            if self._refresh_in_progress is not None:
                try:
                    result = await asyncio.wait_for(
                        self._refresh_in_progress,
                        timeout=self.timeout_seconds,
                    )
                    return {
                        "ok": result.get("ok", False),
                        "refreshed": False,
                        "cached": True,
                        "reason": "coalesced",
                        "detail": "Waited for concurrent refresh",
                        "checked_at": checked_at,
                    }
                except asyncio.TimeoutError:
                    return {
                        "ok": False,
                        "refreshed": False,
                        "cached": False,
                        "reason": "timeout",
                        "detail": f"Coalesced refresh timeout ({self.timeout_seconds}s)",
                        "checked_at": checked_at,
                    }
                except Exception as exc:
                    return {
                        "ok": False,
                        "refreshed": False,
                        "cached": False,
                        "reason": "failed",
                        "detail": f"Coalesced refresh failed: {type(exc).__name__}",
                        "checked_at": checked_at,
                    }

            # 3. No concurrent refresh; perform one
            if self.refresh_fn is None:
                from hermes_cli.fleet.usage_refresh import refresh_usage_document
                refresh_fn = refresh_usage_document
            else:
                refresh_fn = self.refresh_fn

            # Spawn the refresh as a task so concurrent calls can coalesce
            async def _do_refresh():
                try:
                    # refresh_fn is a sync function; run it in thread
                    result = await asyncio.to_thread(refresh_fn)
                    # Result has .ok attribute (UsageRefreshReport) or is dict/MagicMock
                    ok = result.ok if hasattr(result, "ok") else result.get("ok", True) if isinstance(result, dict) else True
                    return {"ok": ok}
                except Exception as exc:
                    logger.exception("Usage refresh failed: %s", exc)
                    return {"ok": False, "error": type(exc).__name__}

            self._refresh_in_progress = asyncio.create_task(_do_refresh())
            try:
                refresh_result = await asyncio.wait_for(
                    self._refresh_in_progress,
                    timeout=self.timeout_seconds,
                )
                self._last_refresh_at = now
                return {
                    "ok": refresh_result.get("ok", False),
                    "refreshed": True,
                    "cached": False,
                    "reason": "ok",
                    "detail": "Refresh completed successfully",
                    "checked_at": checked_at,
                }
            except asyncio.TimeoutError:
                # Refresh timed out, but let the task continue in the background
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
                logger.exception("Usage refresh failed: %s", exc)
                return {
                    "ok": False,
                    "refreshed": False,
                    "cached": False,
                    "reason": "failed",
                    "detail": f"Refresh failed: {type(exc).__name__}: {exc}",
                    "checked_at": checked_at,
                }
            finally:
                self._refresh_in_progress = None


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
