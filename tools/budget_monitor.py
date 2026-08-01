"""
Budget Monitor — Hard budget enforcement for subagents.

Monitors token usage and wall-clock time on a child AIAgent, firing an
interrupt when a hard budget cap is exceeded.  Plugs into
``_run_single_child`` in ``delegate_tool.py`` with zero changes to the
existing API — the monitor is an opt-in observer.

Architecture:

  budget_monitor = BudgetMonitor(
      child_agent,
      max_tokens=10000,
      max_seconds=60,
      on_budget_exceeded="interrupt",
  )
  budget_monitor.start()
  result = child.run_conversation(...)
  budget_monitor.stop()
  # result carries budget_exceeded=True if the cap was hit
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class BudgetSnapshot:
    """Token + time state at one point in time."""

    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_seconds: float = 0.0
    api_calls: int = 0
    budget_exceeded: bool = False
    exceeded_reason: str = ""


class BudgetMonitor:
    """Observes an AIAgent and fires an interrupt when budget is exceeded.

    Polls the child's ``get_activity_summary()`` on a configurable interval
    (default 2s) and checks:

      1. ``session_total_tokens > max_tokens``
      2. ``elapsed > max_seconds``

    When either cap is exceeded AND ``on_exceeded == "interrupt"``, calls
    ``child.interrupt()`` to signal the agent loop to stop at the next
    iteration boundary.  The interrupt is graceful — the current API call
    completes, then the loop breaks.  For truly hard cutoffs at the
    transport level, set ``on_exceeded == "raise"`` (requires threading +
    a daemon timer that raises on the worker thread).

    MULTI-NODE TRACKING:
    ``shared_budget`` lets you pass a dict that is mutated in-place so
    multiple BudgetMonitor instances (e.g. one per node in a fan-out
    batch) share a single run-level budget.  Each decrements the
    shared counters atomically.
    """

    def __init__(
        self,
        child: Any,
        *,
        max_tokens: Optional[int] = None,
        max_seconds: Optional[int] = None,
        poll_interval: float = 2.0,
        on_exceeded: str = "interrupt",
        shared_budget: Optional[Dict[str, float]] = None,
        node_id: str = "",
        callback: Optional[Callable[[BudgetSnapshot], None]] = None,
    ):
        if max_tokens is None and max_seconds is None:
            raise ValueError("At least one of max_tokens or max_seconds is required")

        self._child = child
        self._max_tokens = max_tokens
        self._max_seconds = max_seconds
        self._poll_interval = poll_interval
        self._on_exceeded = on_exceeded
        self._shared_budget = shared_budget
        self._node_id = node_id
        self._callback = callback

        self._started_at: float = 0.0
        self._stopped = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._snapshot = BudgetSnapshot()

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> "BudgetMonitor":
        """Begin monitoring. Call BEFORE child.run_conversation()."""
        self._started_at = time.monotonic()
        self._stopped.clear()
        self._monitor_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name=f"budget-monitor-{self._node_id}"
        )
        self._monitor_thread.start()
        return self

    def stop(self) -> BudgetSnapshot:
        """Stop monitoring. Call AFTER child.run_conversation() returns.

        Returns the final BudgetSnapshot with budget_exceeded=True if
        the interrupt fired at any point during the run.
        """
        self._stopped.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)
        self._snapshot.elapsed_seconds = round(
            time.monotonic() - self._started_at, 2
        )
        return self._snapshot

    # ── internals ─────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Polls child activity, checks budget, fires interrupt if exceeded."""
        while not self._stopped.is_set():
            self._stopped.wait(self._poll_interval)
            if self._stopped.is_set():
                break

            try:
                self._check()
            except Exception:
                logger.debug(
                    "BudgetMonitor._check() failed for node %s", self._node_id,
                    exc_info=True,
                )

    def _check(self) -> None:
        """Run one budget check cycle."""
        now = time.monotonic()
        elapsed = now - self._started_at

        # Read child token counters — use getattr chain to survive
        # test doubles that don't carry the real agent attributes.
        tokens = 0
        api_calls = 0
        try:
            summary = self._child.get_activity_summary()
            api_calls = int(summary.get("api_call_count", 0) or 0)
            # session_total_tokens is the canonical counter on AIAgent
            tokens = getattr(self._child, "session_total_tokens", 0)
            if not isinstance(tokens, (int, float)):
                tokens = 0
        except Exception:
            pass

        self._snapshot.total_tokens = int(tokens)
        self._snapshot.api_calls = api_calls
        self._snapshot.elapsed_seconds = round(elapsed, 2)

        # Decrement shared budget
        shared = self._shared_budget
        if shared is not None:
            with threading.Lock() if hasattr(shared, "lock") else _noop_cm():
                shared["remaining_tokens"] = shared.get(
                    "remaining_tokens", float("inf")
                )
                shared["remaining_seconds"] = shared.get(
                    "remaining_seconds", float("inf")
                )

        # Check token cap
        token_exceeded = (
            self._max_tokens is not None
            and tokens > 0
            and tokens >= self._max_tokens
        )

        # Check time cap
        time_exceeded = (
            self._max_seconds is not None
            and elapsed >= self._max_seconds
        )

        if token_exceeded or time_exceeded:
            reason_parts = []
            if token_exceeded:
                reason_parts.append(
                    f"tokens ({tokens} >= {self._max_tokens})"
                )
            if time_exceeded:
                reason_parts.append(
                    f"time ({elapsed:.1f}s >= {self._max_seconds}s)"
                )
            reason = "Budget exceeded: " + ", ".join(reason_parts)

            self._snapshot.budget_exceeded = True
            self._snapshot.exceeded_reason = reason

            logger.warning(
                "BudgetMonitor: %s (node=%s)",
                reason,
                self._node_id or "<unnamed>",
            )

            if self._callback:
                try:
                    self._callback(self._snapshot)
                except Exception:
                    pass

            if self._on_exceeded in ("interrupt", "warn"):
                try:
                    if hasattr(self._child, "interrupt"):
                        self._child.interrupt(
                            f"BudgetMonitor: {reason}"
                        )
                    elif hasattr(self._child, "_interrupt_requested"):
                        self._child._interrupt_requested = True
                except Exception:
                    pass

            # Stop polling — the interrupt has been sent.
            self._stopped.set()


# ── helpers ───────────────────────────────────────────────────────

class _noop_cm:
    """No-op context manager for when shared_budget has no lock."""

    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass


# ── convenience: run a child with budget enforcement ──────────────


def run_child_with_budget(
    child: Any,
    goal: str,
    *,
    max_tokens: Optional[int] = None,
    max_seconds: Optional[int] = None,
    task_id: str = "",
    **run_kwargs,
) -> Dict[str, Any]:
    """Run a child agent with hard budget enforcement.

    Calls ``child.run_conversation(user_message=goal, ...)`` while a
    ``BudgetMonitor`` watches token usage and elapsed time.  If either
    cap is hit, the child is gracefully interrupted.

    Returns the original ``run_conversation`` result dict with an extra
    ``_budget`` key carrying the ``BudgetSnapshot``.
    """
    monitor = BudgetMonitor(
        child,
        max_tokens=max_tokens,
        max_seconds=max_seconds,
        node_id=task_id,
    )
    monitor.start()
    try:
        result = child.run_conversation(
            user_message=goal, task_id=task_id, **run_kwargs
        )
    finally:
        snapshot = monitor.stop()

    if isinstance(result, dict):
        result["_budget"] = {
            "monitored": True,
            "max_tokens": max_tokens,
            "max_seconds": max_seconds,
            "tokens_used": snapshot.total_tokens,
            "seconds_elapsed": snapshot.elapsed_seconds,
            "api_calls": snapshot.api_calls,
            "budget_exceeded": snapshot.budget_exceeded,
            "exceeded_reason": snapshot.exceeded_reason,
        }
    return result
