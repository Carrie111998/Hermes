"""Shared authoritative two-step human decisions for DDP lifecycle gates."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from devflow_delegation.lifecycle import emit_transition_event, transition

Decision = Literal["approve", "decline"]
TargetState = Literal["PLANNED", "DECLINED"]
ConfirmationResult = Literal["committed", "already_decided"]


class DdpDecisionError(ValueError):
    """Base class for fail-closed decision-service errors."""


class DdpDecisionUnauthorized(DdpDecisionError):
    """The requested decision is not authorized by a valid staged confirmation."""


class DdpDecisionExpired(DdpDecisionError):
    """The staged confirmation exceeded its fixed lifetime."""


class DdpDecisionConflict(DdpDecisionError):
    """The request cannot accept a newly staged decision."""


class DdpDecisionTelemetryError(DdpDecisionError):
    """The durable decision committed but post-commit telemetry failed."""


@dataclass(frozen=True)
class StagedDdpDecision:
    request_id: str
    decision: Decision
    target_state: TargetState
    immutable_summary: str
    confirmation_token: str
    expires_at_monotonic: float


@dataclass(frozen=True)
class _PendingDecision:
    staged: StagedDdpDecision
    actor: str
    rationale: str


class DdpDecisionService:
    """Stage and atomically commit one request-wide DDP human decision."""

    CONFIRM_TTL_SECONDS = 300.0

    def __init__(
        self,
        *,
        ledger,
        bus,
        monotonic: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
    ) -> None:
        self._ledger = ledger
        self._bus = bus
        self._monotonic = monotonic
        self._token_factory = token_factory
        self._pending: dict[str, _PendingDecision] = {}

    def stage(
        self,
        *,
        request_id: str,
        decision: str,
        actor: str,
        rationale: str,
    ) -> StagedDdpDecision:
        request_id = _required(request_id, "request_id")
        actor = _required(actor, "actor")
        rationale = _required(rationale, "rationale")
        if decision not in {"approve", "decline"}:
            raise DdpDecisionUnauthorized("decision must be approve or decline")

        row = self._ledger.get_request(request_id)
        if row is None:
            raise DdpDecisionUnauthorized(f"unknown DDP request: {request_id}")
        if row.get("state") != "TRIAGED":
            raise DdpDecisionConflict(
                f"DDP request {request_id} is {row.get('state')}, not TRIAGED"
            )
        if self._ledger.human_decision_for_request(request_id) is not None:
            raise DdpDecisionConflict(f"DDP request {request_id} was already decided")

        bound_decision: Decision = decision
        target_state: TargetState = "PLANNED" if decision == "approve" else "DECLINED"
        token = self._token_factory()
        expires_at = self._monotonic() + self.CONFIRM_TTL_SECONDS
        title = ""
        try:
            envelope = json.loads(str(row.get("envelope_json") or "{}"))
            title = str(envelope.get("title") or "").strip()[:200]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        summary = f"{request_id}: {title} -> {target_state}"
        staged = StagedDdpDecision(
            request_id=request_id,
            decision=bound_decision,
            target_state=target_state,
            immutable_summary=summary,
            confirmation_token=token,
            expires_at_monotonic=expires_at,
        )
        self._pending[token] = _PendingDecision(
            staged=staged,
            actor=actor,
            rationale=rationale[:500],
        )
        return staged

    def pending(self, confirmation_token: str) -> StagedDdpDecision | None:
        """Return the immutable staged decision for an unconsumed token."""
        pending = self._pending.get(confirmation_token)
        return pending.staged if pending is not None else None

    def has_pending(self) -> bool:
        """Report whether this process has any unconsumed staged decisions."""
        return bool(self._pending)

    def expire(self, confirmation_token: str) -> None:
        """Forget one process-local staged token without mutating authority."""
        self._pending.pop(confirmation_token, None)

    def confirm(self, *, staged: StagedDdpDecision, actor: str) -> ConfirmationResult:
        actor = _required(actor, "actor")
        pending = self._pending.get(staged.confirmation_token)
        if pending is None:
            if self._ledger.human_decision_for_request(staged.request_id) is not None:
                return "already_decided"
            raise DdpDecisionUnauthorized("confirmation token is unknown or already used")
        if pending.actor != actor:
            raise DdpDecisionUnauthorized("confirmation is bound to a different actor")
        if pending.staged != staged:
            if pending.staged.decision != staged.decision:
                raise DdpDecisionUnauthorized("confirmation is for a different decision")
            raise DdpDecisionUnauthorized("confirmation token payload does not match staged decision")
        if self._monotonic() > staged.expires_at_monotonic:
            self._pending.pop(staged.confirmation_token, None)
            raise DdpDecisionExpired("confirmation token has expired")

        with self._ledger.transaction():
            inserted = self._ledger.record_human_decision(
                staged.request_id,
                actor,
                staged.decision,
                pending.rationale,
                staged.confirmation_token,
            )
            if inserted:
                transition(
                    self._ledger,
                    None,
                    staged.request_id,
                    staged.target_state,
                    actor=actor,
                    evidence_ref=pending.rationale,
                    expected_from_state="TRIAGED",
                )

        self._pending.pop(staged.confirmation_token, None)
        if not inserted:
            return "already_decided"

        try:
            emit_transition_event(
                self._bus,
                request_id=staged.request_id,
                from_state="TRIAGED",
                to_state=staged.target_state,
                actor=actor,
            )
        except Exception as error:
            raise DdpDecisionTelemetryError(
                "durable decision committed but telemetry delivery failed"
            ) from error
        return "committed"


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DdpDecisionUnauthorized(f"{name} must be a non-empty string")
    return value.strip()
