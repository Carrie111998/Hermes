"""Frozen lane values and the protocol future TA implementations must satisfy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.lanes.harness import LaneHarness


@dataclass(frozen=True)
class LaneTask:
    lane_id: str
    external_id: str
    payload: dict[str, Any]
    id: int | None = None
    task_id: str | None = None
    status: str = "ingested"


@dataclass(frozen=True)
class LaneDraft:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalRequest:
    token: str
    lane_task_id: int
    status: str
    expires_at: str


@dataclass(frozen=True)
class ApprovalStatus:
    token: str
    status: str
    expires_at: str
    grant_note: str | None = None
    reject_reason: str | None = None


@dataclass(frozen=True)
class ApprovalGrant:
    token: str
    note: str | None = None


@dataclass(frozen=True)
class PublishResult:
    outcome: str
    log_id: int | None = None
    side_effect_id: int | None = None


@dataclass(frozen=True)
class AdmitResult:
    admitted: bool
    dry_run: bool


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    decision_row_id: int | None
    verdict_id: int | None
    cost_ledger_id: int | None


@runtime_checkable
class BusinessLane(Protocol):
    lane_id: str
    name: str
    version: str

    def ingest(self, *, harness: "LaneHarness") -> list[LaneTask]: ...
    def draft(
        self, *, task: LaneTask, harness: "LaneHarness"
    ) -> LaneDraft: ...
    def approve(
        self, *, task: LaneTask, draft: LaneDraft, harness: "LaneHarness"
    ) -> ApprovalRequest: ...
    def publish(
        self,
        *,
        task: LaneTask,
        draft: LaneDraft,
        approval: ApprovalGrant,
        harness: "LaneHarness",
    ) -> PublishResult: ...
    def cleanup(self, *, task: LaneTask, harness: "LaneHarness") -> None: ...


__all__ = [
    "AdmitResult",
    "ApprovalGrant",
    "ApprovalRequest",
    "ApprovalStatus",
    "BusinessLane",
    "LLMResult",
    "LaneDraft",
    "LaneTask",
    "PublishResult",
]
