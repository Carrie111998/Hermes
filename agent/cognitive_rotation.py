"""Session-scoped controller for opt-in cognitive rotation.

The controller is intentionally independent of the agent loop. Runtime callers
feed it completed tool outcomes and ask for a decision before direct source
mutation. It owns no I/O and keeps its small amount of session state behind a
lock because tool batches may complete on worker threads.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Mapping


DIRECT_MUTATOR_TOOLS = frozenset({"patch", "write_file", "execute_code"})


@dataclass(frozen=True)
class CognitiveRotationConfig:
    """Configuration for one session's cognitive-rotation controller."""

    enabled: bool = False
    mutation_budget: int = 20
    rotate_after_compaction: bool = True
    lock_after_delegation: bool = True

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None,
    ) -> "CognitiveRotationConfig":
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        return cls(
            enabled=_as_bool(data.get("enabled"), defaults.enabled),
            mutation_budget=_as_int(
                data.get("mutation_budget"), defaults.mutation_budget
            ),
            rotate_after_compaction=_as_bool(
                data.get("rotate_after_compaction"),
                defaults.rotate_after_compaction,
            ),
            lock_after_delegation=_as_bool(
                data.get("lock_after_delegation"),
                defaults.lock_after_delegation,
            ),
        )


@dataclass(frozen=True)
class CognitiveRotationDecision:
    """Pre-call decision for a direct mutator."""

    action: str = "allow"
    reason: str = ""
    message: str = ""
    reservation_id: int | None = None

    @property
    def allows_execution(self) -> bool:
        return self.action == "allow"


class CognitiveRotationController:
    """Thread-safe cognitive-rotation state for one agent session."""

    def __init__(self, config: CognitiveRotationConfig | None = None):
        self.config = config or CognitiveRotationConfig()
        self._lock = threading.RLock()
        self._active_reason = ""
        self._successful_mutations = 0
        self._pending_mutation_reservations: set[int] = set()
        self._next_mutation_reservation_id = 1

    @property
    def active_reason(self) -> str:
        with self._lock:
            return self._active_reason

    @property
    def successful_mutations(self) -> int:
        with self._lock:
            return self._successful_mutations

    @property
    def pending_mutation_reservations(self) -> int:
        with self._lock:
            return len(self._pending_mutation_reservations)

    def before_call(
        self,
        tool_name: str,
        *,
        batch_has_delegation: bool = False,
    ) -> CognitiveRotationDecision:
        """Return whether ``tool_name`` may execute in the current session."""
        with self._lock:
            if not self.config.enabled or tool_name not in DIRECT_MUTATOR_TOOLS:
                return CognitiveRotationDecision()
            if self._active_reason:
                return CognitiveRotationDecision(
                    action="block",
                    reason=self._active_reason,
                    message=_block_message(self._active_reason),
                )
            if batch_has_delegation:
                return CognitiveRotationDecision(
                    action="block",
                    reason="mixed_delegation_batch",
                    message=_block_message("mixed_delegation_batch"),
                )
            budget = self.config.mutation_budget
            if budget > 0:
                occupied = self._successful_mutations + len(
                    self._pending_mutation_reservations
                )
                if occupied >= budget:
                    return CognitiveRotationDecision(
                        action="block",
                        reason="mutation_budget",
                        message=_block_message("mutation_budget"),
                    )
                reservation_id = self._next_mutation_reservation_id
                self._next_mutation_reservation_id += 1
                self._pending_mutation_reservations.add(reservation_id)
                return CognitiveRotationDecision(reservation_id=reservation_id)
            return CognitiveRotationDecision()

    def observe_tool_result(
        self,
        tool_name: str,
        *,
        failed: bool,
        reservation_id: int | None = None,
    ) -> str | None:
        """Observe one completed tool and return a one-time activation notice."""
        with self._lock:
            if not self.config.enabled:
                return None
            if tool_name in DIRECT_MUTATOR_TOOLS:
                if reservation_id is not None:
                    if reservation_id not in self._pending_mutation_reservations:
                        return None
                    self._pending_mutation_reservations.remove(reservation_id)
                if failed:
                    return None
                self._successful_mutations += 1
                budget = self.config.mutation_budget
                if budget > 0 and self._successful_mutations >= budget:
                    return self._activate("mutation_budget")
                return None
            if failed:
                return None
            if tool_name == "delegate_task":
                if self.config.lock_after_delegation:
                    return self._activate("delegation")
                return None
            return None

    def cancel_mutation_reservation(self, reservation_id: int | None) -> None:
        """Release an admitted direct mutator that will not produce a result."""
        if reservation_id is None:
            return
        with self._lock:
            self._pending_mutation_reservations.discard(reservation_id)

    def observe_compaction(
        self,
        *,
        made_progress: bool,
        committed: bool,
    ) -> str | None:
        """Observe a completed compaction boundary."""
        with self._lock:
            if (
                not self.config.enabled
                or not self.config.rotate_after_compaction
                or not made_progress
                or not committed
                or self._successful_mutations == 0
            ):
                return None
            return self._activate("compaction")

    def _activate(self, reason: str) -> str | None:
        if self._active_reason:
            return None
        self._active_reason = reason
        return _activation_notice(reason)


def cognitive_rotation_block_result(
    decision: CognitiveRotationDecision,
) -> str:
    """Build a synthetic tool result without requesting a tool-loop halt."""
    return json.dumps(
        {
            "success": False,
            "error": decision.message,
            "error_type": "cognitive_rotation_required",
            "reason": decision.reason,
        },
        ensure_ascii=False,
    )


def _activation_notice(reason: str) -> str:
    return (
        f"[Cognitive rotation activated: {reason}] Direct source mutation is now "
        "reserved for a fresh builder. This session may continue reading, "
        "verifying, testing, inspecting diffs, delegating, and performing "
        "release operations."
    )


def _block_message(reason: str) -> str:
    return (
        f"Cognitive rotation is required ({reason}). Start or delegate to a "
        "fresh builder for further direct source mutation. Read, verification, "
        "test, diff, terminal, delegation, commit, and push operations remain "
        "available in this session."
    )


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default
