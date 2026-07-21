"""Opt-in non-convergence tracking for delegated child agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

_DEFAULT_WARN_AFTER = 15
_DEFAULT_HALT_AFTER = 25


@dataclass(frozen=True)
class ProgressDecision:
    """Decision produced after one model/tool iteration."""

    action: Literal["none", "warn", "halt"] = "none"
    count: int = 0
    message: str = ""


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _opt_in_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value == 1


class ProgressTracker:
    """Count delegated-child iterations that produce no convergence signal."""

    def __init__(
        self,
        *,
        warn_after: int = _DEFAULT_WARN_AFTER,
        halt_after: int = _DEFAULT_HALT_AFTER,
        enabled: Any = False,
    ) -> None:
        self.warn_after = _positive_int(warn_after, _DEFAULT_WARN_AFTER)
        self.halt_after = max(
            self.warn_after,
            _positive_int(halt_after, _DEFAULT_HALT_AFTER),
        )
        self.enabled = _opt_in_bool(enabled)
        self._iterations_since_progress = 0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ProgressTracker":
        """Build a tracker from ``delegation.progress_tracker`` config."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            warn_after=data.get("warn_after", _DEFAULT_WARN_AFTER),
            halt_after=data.get("halt_after", _DEFAULT_HALT_AFTER),
        )

    @property
    def iterations_since_progress(self) -> int:
        return self._iterations_since_progress

    def finish_iteration(self, *, made_progress: bool) -> ProgressDecision:
        """Record one complete model/tool round and return its decision."""
        if not self.enabled:
            return ProgressDecision()
        if made_progress:
            self._iterations_since_progress = 0
            return ProgressDecision()

        self._iterations_since_progress += 1
        count = self._iterations_since_progress
        if count >= self.halt_after:
            return ProgressDecision(
                action="halt",
                count=count,
                message=(
                    f"Subagent stopped after {count} iterations without "
                    "user-visible text or a successful file change."
                ),
            )
        if count >= self.warn_after:
            return ProgressDecision(
                action="warn",
                count=count,
                message=(
                    f"[PROGRESS TRACKER: {count} iterations without user-visible "
                    "text or a successful file change. This subagent is not "
                    "converging; finish the task or return the useful findings now.]"
                ),
            )
        return ProgressDecision(count=count)

    def reset(self) -> None:
        """Clear state at the beginning of a new user turn."""
        self._iterations_since_progress = 0
