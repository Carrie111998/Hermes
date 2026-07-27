"""Per-agent iteration budget — thread-safe consume/refund counter.

Extracted from ``run_agent.py``.  Each ``AIAgent`` instance (parent or
subagent) holds an :class:`IterationBudget`; the parent's cap comes from
``max_iterations`` (default 500), each subagent's cap comes from
``delegation.max_iterations`` (default 50).

``run_agent`` re-exports ``IterationBudget`` so existing
``from run_agent import IterationBudget`` imports keep working unchanged.
"""

from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter for an agent.

    Each agent (parent or subagent) gets its own ``IterationBudget``.
    The parent's budget is capped at ``max_iterations`` (default 500).
    Each subagent gets an independent budget capped at
    ``delegation.max_iterations`` (default 50) — this means total
    iterations across parent + subagents can exceed the parent's cap.
    Users control the per-subagent limit via ``delegation.max_iterations``
    in config.yaml.

    ``execute_code`` (programmatic tool calling) iterations are refunded via
    :meth:`refund` so they don't eat into the budget.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._refunded = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration.  Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns).

        Also records that a refund happened. The loop is bounded by BOTH
        ``api_call_count < max_iterations`` and ``budget.remaining > 0``, and
        the budget is constructed as ``IterationBudget(max_iterations)`` — so
        the two counters advance together and, after any refund, the
        api_call_count half always binds first. Decrementing ``_used`` alone
        therefore bought nothing: the documented relief ("execute_code
        iterations are refunded so they don't eat into the budget") never
        actually happened. Callers read :attr:`refunded` to extend the other
        half of the condition too.

        Refunds are capped at half the budget. The relief has to be real, but
        an agent that only ever calls execute_code must not be able to extend
        its own turn without limit.
        """
        with self._lock:
            if self._used > 0 and self._refunded < self.max_refunds:
                self._used -= 1
                self._refunded += 1

    @property
    def max_refunds(self) -> int:
        """Upper bound on refunded iterations (half the budget)."""
        return max(0, self.max_total // 2)

    @property
    def refunded(self) -> int:
        """Iterations refunded so far — the allowance the loop may add."""
        with self._lock:
            return self._refunded

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget"]
