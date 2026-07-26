"""Frozen value objects for compact execution verdicts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal[
    "success",
    "failure",
    "partial",
    "aborted",
    "killed_by_cap",
    "killed_by_operator",
]
FailureClass = Literal[
    "infra",
    "quality",
    "capability",
    "budget",
    "ambiguous",
    "cost_cap",
    "operator",
]
Mode = Literal["single", "single_with_critic", "moa", "panel", "decompose"]

ALLOWED_RUNG_IDS = (
    "r0_baseline",
    "r1_decompose",
    "r2_critic",
    "r3_moa",
    "r4_opus5_single",
    "r5_fusion",
)


def canonical_strategy_hash(payload: dict[str, Any]) -> str:
    """Return the stable SHA-256 hash of a canonical JSON strategy payload."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DispatchEnvelope:
    task_id: str
    attempt_number: int
    rung_id: str
    model_slug: str
    mode: Mode
    strategy_payload: dict[str, Any]
    task_run_id: int | None = None
    parent_verdict_id: int | None = None
    expected_cost_aud: float | None = None
    issued_by: str = "atlas"

    @property
    def strategy_hash(self) -> str:
        return canonical_strategy_hash(self.strategy_payload)


@dataclass(frozen=True)
class LeafVerdict:
    task_id: str
    attempt_number: int
    rung_id: str
    model_used: str
    outcome: Outcome
    confidence: float
    strategy_hash: str
    task_run_id: int | None = None
    dispatch_envelope_id: int | None = None
    failure_class: FailureClass | None = None
    failure_signals: list[str] = field(default_factory=list)
    cost_aud: float = 0.0
    side_effects: list[int] = field(default_factory=list)
    escalation_recommended: bool = False
    recommendation_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    wall_ms: int | None = None
    error_class: str | None = None
    error_message: str | None = None
    raw_meta: dict[str, Any] | None = None


__all__ = [
    "ALLOWED_RUNG_IDS",
    "DispatchEnvelope",
    "FailureClass",
    "LeafVerdict",
    "Mode",
    "Outcome",
    "canonical_strategy_hash",
]
