from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    BrokerOrderSnapshot,
    ExecutionFill,
    SubmissionAcknowledgement,
    SubmissionRequest,
)


class ExecutionAdapterError(RuntimeError):
    """Base exception for governed execution adapter failures."""


class SubmissionOutcomeUncertainError(ExecutionAdapterError):
    """Raised when a provider may have received an order but outcome is unknown."""


@runtime_checkable
class ExecutionAdapter(Protocol):
    @property
    def provider_name(self) -> str:
        """Return the normalized provider identifier."""

    def submit_order(
        self,
        request: SubmissionRequest,
    ) -> SubmissionAcknowledgement:
        """Submit one already-admitted governed order request."""

    def get_order(
        self,
        provider_order_id: str,
    ) -> BrokerOrderSnapshot:
        """Retrieve the latest broker-side order snapshot."""

    def list_fills(
        self,
        provider_order_id: str,
    ) -> tuple[ExecutionFill, ...]:
        """Return immutable execution fills for one provider order."""
