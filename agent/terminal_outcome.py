"""Canonical terminal-outcome classification for agent turn results.

Agent results cross several runtime boundaries: gateway delivery, the
OpenAI-compatible API, and delegated child aggregation.  Those boundaries
must not independently guess whether the same result completed.  This module
is deliberately dependency-free so every boundary can share one precedence
contract without introducing import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class TerminalOutcomeKind(StrEnum):
    """Mutually exclusive terminal categories exposed by the runtime."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True)
class TerminalOutcome:
    """Canonicalized, mutually exclusive interpretation of an agent result."""

    kind: TerminalOutcomeKind
    completed: bool
    partial: bool
    interrupted: bool
    failed: bool
    incomplete: bool
    contradictory: bool = False
    valid: bool = True
    reason: str | None = None


_BOOLEAN_OUTCOME_FIELDS = (
    "completed",
    "partial",
    "interrupted",
    "failed",
    "incomplete",
)
_COMPLETED_STATUSES = frozenset(
    {"completed", "complete", "success", "succeeded", "ok"}
)
_PARTIAL_STATUSES = frozenset(
    {"partial", "incomplete", "blocked", "timeout", "timed_out"}
)
_INTERRUPTED_STATUSES = frozenset(
    {"interrupted", "cancelled", "canceled"}
)
_FAILED_STATUSES = frozenset(
    {"failed", "failure", "error"}
)
_KNOWN_STATUSES = (
    _COMPLETED_STATUSES
    | _PARTIAL_STATUSES
    | _INTERRUPTED_STATUSES
    | _FAILED_STATUSES
)


def _outcome(
    kind: TerminalOutcomeKind,
    *,
    contradictory: bool = False,
    valid: bool = True,
    reason: str | None = None,
) -> TerminalOutcome:
    completed = kind is TerminalOutcomeKind.COMPLETED
    return TerminalOutcome(
        kind=kind,
        completed=completed,
        partial=kind is TerminalOutcomeKind.PARTIAL,
        interrupted=kind is TerminalOutcomeKind.INTERRUPTED,
        failed=kind is TerminalOutcomeKind.FAILED,
        incomplete=not completed,
        contradictory=contradictory,
        valid=valid,
        reason=reason,
    )


def normalize_terminal_outcome(result: Any) -> TerminalOutcome:
    """Return one fail-closed terminal interpretation for ``result``.

    Precedence is mechanical and intentionally conservative:

    ``failed/error > interrupted > partial/incomplete > completed``.

    Contradictory but well-typed signals are normalized according to that
    precedence and marked for diagnostics.  Malformed or unknown explicit
    status values fail closed.  A legacy mapping with no terminal fields
    remains successful for compatibility with older agent implementations.
    """

    if not isinstance(result, Mapping):
        return _outcome(
            TerminalOutcomeKind.FAILED,
            valid=False,
            reason="invalid_agent_result",
        )

    if any(
        field in result and type(result[field]) is not bool
        for field in _BOOLEAN_OUTCOME_FIELDS
    ):
        return _outcome(
            TerminalOutcomeKind.FAILED,
            valid=False,
            reason="invalid_terminal_flag",
        )

    status_present = "status" in result
    raw_status = result.get("status")
    if status_present and not isinstance(raw_status, str):
        return _outcome(
            TerminalOutcomeKind.FAILED,
            valid=False,
            reason="invalid_terminal_status",
        )
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    if status_present and not status:
        return _outcome(
            TerminalOutcomeKind.FAILED,
            valid=False,
            reason="invalid_terminal_status",
        )
    if status and status not in _KNOWN_STATUSES:
        return _outcome(
            TerminalOutcomeKind.FAILED,
            valid=False,
            reason="unknown_terminal_status",
        )

    failed_signal = bool(
        result.get("failed", False)
        or result.get("error")
        or status in _FAILED_STATUSES
    )
    interrupted_signal = bool(
        result.get("interrupted", False)
        or status in _INTERRUPTED_STATUSES
    )
    explicit_partial_signal = bool(
        result.get("partial", False)
        or status in _PARTIAL_STATUSES
    )
    # ``incomplete`` and ``completed=False`` are broad, derived properties of
    # every non-successful outcome.  They must not manufacture a contradictory
    # PARTIAL signal when an otherwise coherent FAILED or INTERRUPTED result
    # carries the complete shared terminal envelope.
    derived_incomplete_signal = bool(
        result.get("incomplete", False)
        or result.get("completed") is False
    )
    partial_signal = bool(
        explicit_partial_signal
        or (
            derived_incomplete_signal
            and not failed_signal
            and not interrupted_signal
        )
    )
    completed_signal = bool(
        result.get("completed") is True
        or status in _COMPLETED_STATUSES
    )
    signals = (
        failed_signal,
        interrupted_signal,
        partial_signal,
        completed_signal,
    )
    contradictory = sum(bool(signal) for signal in signals) > 1

    if failed_signal:
        kind = TerminalOutcomeKind.FAILED
    elif interrupted_signal:
        kind = TerminalOutcomeKind.INTERRUPTED
    elif partial_signal:
        kind = TerminalOutcomeKind.PARTIAL
    elif completed_signal:
        kind = TerminalOutcomeKind.COMPLETED
    else:
        # Pre-structured-result compatibility: a mapping without any terminal
        # signal historically represented a successful agent return.
        kind = TerminalOutcomeKind.COMPLETED

    return _outcome(kind, contradictory=contradictory)


__all__ = [
    "TerminalOutcome",
    "TerminalOutcomeKind",
    "normalize_terminal_outcome",
]
