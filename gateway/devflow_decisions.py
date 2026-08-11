"""Trusted HTTP adapters around the authoritative DDP decision service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from devflow_delegation.decision_service import (
    DdpDecisionError,
    DdpDecisionService,
    DdpDecisionTelemetryError,
    StagedDdpDecision,
)

_FORBIDDEN_BODY_FIELDS = frozenset({"actor", "decided_by", "confirmation_token"})
_ALLOWED_DECISIONS = frozenset({"approve", "decline"})


class DevflowDecisionRequestError(ValueError):
    """A trusted decision request failed validation."""


@dataclass(frozen=True)
class StagedDecisionResponse:
    request_id: str
    decision: str
    target_state: str
    immutable_summary: str
    staged_token: str

    def as_dict(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "decision": self.decision,
            "target_state": self.target_state,
            "immutable_summary": self.immutable_summary,
            "staged_token": self.staged_token,
        }


class DevflowDecisionAdapter:
    """Keep internal confirmation material server-side between HTTP calls."""

    def __init__(self, service: DdpDecisionService) -> None:
        self._service = service
        self._staged: dict[tuple[str, str, str], StagedDdpDecision] = {}

    async def stage(self, *, actor: str, body: dict[str, Any]) -> StagedDecisionResponse:
        _validate_body(body, required={"request_id", "decision", "rationale"})
        request_id = _required(body["request_id"])
        decision = _required(body["decision"])
        rationale = _required(body["rationale"])
        if decision not in _ALLOWED_DECISIONS:
            raise DevflowDecisionRequestError("invalid request")
        staged = await asyncio.to_thread(
            self._service.stage,
            request_id=request_id,
            decision=decision,
            actor=actor,
            rationale=rationale[:500],
        )
        self._staged[(actor, request_id, decision)] = staged
        return StagedDecisionResponse(
            request_id=staged.request_id,
            decision=staged.decision,
            target_state=staged.target_state,
            immutable_summary=staged.immutable_summary,
            staged_token=staged.confirmation_token,
        )

    async def confirm(self, *, actor: str, body: dict[str, Any]) -> tuple[str, StagedDdpDecision]:
        _validate_body(body, required={"request_id", "decision", "staged_token"})
        request_id = _required(body["request_id"])
        decision = _required(body["decision"])
        staged_token = _required(body["staged_token"])
        staged = self._staged.get((actor, request_id, decision))
        if (
            staged is None
            or staged.confirmation_token != staged_token
            or self._service.pending(staged_token) != staged
        ):
            raise DevflowDecisionRequestError("request unavailable")
        try:
            result = await asyncio.to_thread(self._service.confirm, staged=staged, actor=actor)
        except DdpDecisionTelemetryError:
            self._staged.pop((actor, request_id, decision), None)
            return "committed_telemetry_degraded", staged
        except DdpDecisionError:
            raise
        self._staged.pop((actor, request_id, decision), None)
        return result, staged


def _validate_body(body: dict[str, Any], *, required: set[str]) -> None:
    if not isinstance(body, dict) or _FORBIDDEN_BODY_FIELDS.intersection(body):
        raise DevflowDecisionRequestError("invalid request")
    if set(body) != required:
        raise DevflowDecisionRequestError("invalid request")


def _required(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DevflowDecisionRequestError("invalid request")
    return value.strip()
