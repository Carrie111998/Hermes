"""Typed authoritative reconciliation evidence for native sidebar delivery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import math


_LOWER_HEX = frozenset("0123456789abcdef")


class SidebarReconciliationState(StrEnum):
    RECOVERED = "recovered"
    ABSENCE_PROVEN = "absence_proven"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SidebarReconciliationEvidence:
    state: SidebarReconciliationState
    generation: str
    completed_at: float
    expires_at: float
    inventory_digest: str
    marker_digest: str
    match_count: int
    recovered_thread_id: str | None
    fixed_reason: str | None

    @classmethod
    def create(
        cls,
        *,
        state: SidebarReconciliationState,
        generation: str,
        completed_at: float,
        expires_at: float,
        inventory_digest: str,
        marker_digest: str,
        match_count: int,
        recovered_thread_id: str | None,
        fixed_reason: str | None,
    ) -> "SidebarReconciliationEvidence":
        evidence = cls(
            state=state,
            generation=generation,
            completed_at=completed_at,
            expires_at=expires_at,
            inventory_digest=inventory_digest,
            marker_digest=marker_digest,
            match_count=match_count,
            recovered_thread_id=recovered_thread_id,
            fixed_reason=fixed_reason,
        )
        evidence.validate()
        return evidence

    def validate(self) -> None:
        if not isinstance(self.state, SidebarReconciliationState):
            raise ValueError("sidebar reconciliation state is unsupported")
        _exact_text(self.generation, "sidebar reconciliation generation")
        completed_at = _finite_number(
            self.completed_at,
            "sidebar reconciliation completion time",
        )
        expires_at = _finite_number(
            self.expires_at,
            "sidebar reconciliation expiry time",
        )
        if completed_at > expires_at:
            raise ValueError("sidebar reconciliation generation is malformed")
        _lower_hex_digest(
            self.inventory_digest,
            "sidebar reconciliation inventory digest",
        )
        _lower_hex_digest(
            self.marker_digest,
            "sidebar reconciliation marker digest",
        )
        if type(self.match_count) is not int or self.match_count < 0:
            raise ValueError("sidebar reconciliation match count is malformed")
        thread_id = _optional_exact_text(
            self.recovered_thread_id,
            "sidebar reconciliation recovered thread ID",
        )
        reason = _optional_exact_text(
            self.fixed_reason,
            "sidebar reconciliation fixed reason",
        )
        if self.state is SidebarReconciliationState.RECOVERED:
            valid = self.match_count == 1 and thread_id is not None and reason is None
        elif self.state is SidebarReconciliationState.ABSENCE_PROVEN:
            valid = self.match_count == 0 and thread_id is None and reason is None
        else:
            valid = thread_id is None and reason is not None
        if not valid:
            raise ValueError("sidebar reconciliation state shape is malformed")


@dataclass(frozen=True)
class SidebarReconciliationProofInput:
    job_id: str
    source_session_id: str
    bridge_id: str
    marker_digest: str
    placement_generation: int
    delivery_generation: int
    reconciliation_generation: str
    completed_at: float
    expires_at: float
    inventory_digest: str
    state: SidebarReconciliationState
    match_count: int
    recovered_thread_id: str | None
    fixed_reason: str | None

    def validate(self) -> None:
        for value, label in (
            (self.job_id, "sidebar reconciliation job ID"),
            (self.source_session_id, "sidebar reconciliation source session ID"),
            (self.bridge_id, "sidebar reconciliation bridge ID"),
            (
                self.reconciliation_generation,
                "sidebar reconciliation generation",
            ),
        ):
            _exact_text(value, label)
        for value, label in (
            (self.placement_generation, "sidebar placement generation"),
            (self.delivery_generation, "sidebar delivery generation"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} is malformed")
        SidebarReconciliationEvidence.create(
            state=self.state,
            generation=self.reconciliation_generation,
            completed_at=self.completed_at,
            expires_at=self.expires_at,
            inventory_digest=self.inventory_digest,
            marker_digest=self.marker_digest,
            match_count=self.match_count,
            recovered_thread_id=self.recovered_thread_id,
            fixed_reason=self.fixed_reason,
        )


def sidebar_reconciliation_proof_digest(
    value: SidebarReconciliationProofInput,
) -> str:
    if not isinstance(value, SidebarReconciliationProofInput):
        raise TypeError("sidebar reconciliation proof input is malformed")
    value.validate()
    payload = asdict(value)
    payload["state"] = value.state.value
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} is malformed")
    return value


def _optional_exact_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _exact_text(value, label)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} is malformed")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} is malformed")
    return result


def _lower_hex_digest(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        raise ValueError(f"{label} is malformed")
    return value
