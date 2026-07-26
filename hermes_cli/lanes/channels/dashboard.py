"""Queue-only dashboard approvals."""

from __future__ import annotations

from typing import Any

from hermes_cli.lanes.contracts import ApprovalRequest


class DashboardApprovalChannel:
    def emit(
        self,
        *,
        request: ApprovalRequest,
        payload: dict[str, Any],
    ) -> None:
        del request, payload
        return None


__all__ = ["DashboardApprovalChannel"]
