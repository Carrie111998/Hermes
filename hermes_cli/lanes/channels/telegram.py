"""Telegram approval notification through the CS-02b ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from hermes_cli.lanes.contracts import ApprovalRequest
from hermes_cli.side_effects import confirm, mark_in_flight, reserve


class TelegramApprovalChannel:
    def __init__(
        self,
        *,
        lane_id: str,
        db_path: str | Path,
        sender: Callable[[dict[str, Any]], str] | None,
    ) -> None:
        self.lane_id = lane_id
        self.db_path = db_path
        self.sender = sender

    def emit(
        self,
        *,
        request: ApprovalRequest,
        payload: dict[str, Any],
    ) -> int:
        if self.sender is None:
            raise RuntimeError(
                "Telegram approval delivery requires an injected sender"
            )
        key = (
            f"lane-approval:{self.lane_id}:"
            f"{request.lane_task_id}:{request.token}"
        )
        result = reserve(
            task_id=str(request.lane_task_id),
            lane=self.lane_id,
            action_type="telegram.send",
            payload=payload,
            idempotency_key=key,
            db_path=self.db_path,
        )
        if result.already_done is not None:
            return int(result.already_done["id"])
        if result.already_in_flight is not None:
            return int(result.already_in_flight["id"])
        reserved_id = int(result.reserved_id)
        mark_in_flight(reserved_id=reserved_id, db_path=self.db_path)
        external_ref = self.sender(payload)
        confirm(
            reserved_id=reserved_id,
            external_ref=external_ref,
            result_summary="lane approval delivered",
            db_path=self.db_path,
        )
        return reserved_id


__all__ = ["TelegramApprovalChannel"]
