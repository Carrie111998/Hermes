"""Opt-in per-turn main-agent provider request budget."""

from __future__ import annotations

import threading
from collections.abc import Callable


class ProviderRequestBudgetExceeded(RuntimeError):
    """Raised before a covered provider request would exceed its turn limit."""


def parse_provider_request_limit(value: object) -> int:
    """Return a positive integer-like request limit, or zero when disabled."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return parsed if parsed > 0 else 0
    return 0


class ProviderRequestBudget:
    """Thread-safe counter for an optional provider request limit."""

    def __init__(self, max_total: object = 0):
        self.max_total = parse_provider_request_limit(max_total)
        self._used = 0
        self._exhausted = False
        self._lock = threading.Lock()

    def reserve(self, *, reason: str) -> int:
        """Reserve one request slot, or no-op when the budget is disabled."""
        if not self.enabled:
            return 0
        with self._lock:
            if self._used >= self.max_total:
                self._exhausted = True
                raise ProviderRequestBudgetExceeded(
                    "provider request budget exhausted "
                    f"({self._used}/{self.max_total}) before {reason}"
                )
            self._used += 1
            return self._used

    @property
    def enabled(self) -> bool:
        return self.max_total > 0

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._exhausted

    @property
    def remaining(self) -> int | None:
        if not self.enabled:
            return None
        with self._lock:
            return max(0, self.max_total - self._used)


def _is_background_execution(agent: object) -> bool:
    """Return whether this agent is running auxiliary/background model work."""
    if getattr(agent, "_provider_request_budget_exempt", False) is True:
        return True
    platform = str(getattr(agent, "platform", "") or "").lower()
    if platform == "curator":
        return True
    return any(
        getattr(agent, field, None) == "background_review"
        for field in ("_memory_write_context", "_memory_write_origin")
    )


def _is_covered_main_agent(agent: object) -> bool:
    """Return whether provider transports on this agent are in budget scope."""
    if str(getattr(agent, "provider", "") or "").lower() == "moa":
        return False
    if str(getattr(agent, "platform", "") or "").lower() == "subagent":
        return False
    if getattr(agent, "is_subagent", False) is True:
        return False
    if int(getattr(agent, "_delegate_depth", 0) or 0) > 0:
        return False
    if _is_background_execution(agent):
        return False
    if getattr(agent, "_persist_disabled", False) is True:
        return False
    return True


def capture_provider_request_reservation(
    agent: object,
) -> Callable[..., int]:
    """Capture this turn's budget so late workers cannot charge a later turn."""
    budget = getattr(agent, "provider_request_budget", None)
    covered = _is_covered_main_agent(agent)

    def reserve(*, reason: str) -> int:
        if not covered or budget is None:
            return 0
        return budget.reserve(reason=reason)

    return reserve


def reserve_provider_request(agent: object, *, reason: str) -> int:
    """Reserve against the current turn for a synchronous physical request."""
    return capture_provider_request_reservation(agent)(reason=reason)


def reset_provider_request_budget(agent: object) -> ProviderRequestBudget:
    """Replace an agent's per-turn request budget with a fresh instance."""
    budget = ProviderRequestBudget(
        getattr(agent, "max_provider_requests_per_turn", 0)
    )
    setattr(agent, "provider_request_budget", budget)
    return budget


__all__ = [
    "ProviderRequestBudget",
    "ProviderRequestBudgetExceeded",
    "capture_provider_request_reservation",
    "parse_provider_request_limit",
    "reserve_provider_request",
    "reset_provider_request_budget",
]
