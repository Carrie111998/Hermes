"""Protocol for approval notification channels."""

from __future__ import annotations

from typing import Any, Protocol

from hermes_cli.lanes.contracts import ApprovalRequest


class ApprovalChannel(Protocol):
    def emit(
        self,
        *,
        request: ApprovalRequest,
        payload: dict[str, Any],
    ) -> int | None: ...


__all__ = ["ApprovalChannel"]
