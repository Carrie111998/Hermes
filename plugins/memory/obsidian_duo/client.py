"""Service-ready broker client boundary with an embedded v1 implementation."""

from __future__ import annotations

from typing import Any, Protocol

from .contracts import (
    BrokerStatus,
    CandidateDecision,
    MemoryCandidate,
    MemoryEvent,
    MemoryPacket,
    RetrievalRequest,
)


class MemoryBrokerClient(Protocol):
    def retrieve(self, request: RetrievalRequest) -> MemoryPacket: ...
    def observe(self, event: MemoryEvent) -> None: ...
    def propose(self, candidate: MemoryCandidate) -> CandidateDecision: ...
    def flush(self, reason: str, timeout: float) -> bool: ...
    def status(self) -> BrokerStatus: ...
    def shutdown(self, timeout: float) -> None: ...


class EmbeddedMemoryBrokerClient:
    def __init__(self, broker: Any):
        self._broker = broker

    def retrieve(self, request: RetrievalRequest) -> MemoryPacket:
        return self._broker.retrieve(request)

    def observe(self, event: MemoryEvent) -> None:
        self._broker.observe(event)

    def propose(self, candidate: MemoryCandidate) -> CandidateDecision:
        return self._broker.propose(candidate)

    def flush(self, reason: str, timeout: float) -> bool:
        return self._broker.flush(reason, timeout)

    def status(self) -> BrokerStatus:
        return self._broker.status()

    def shutdown(self, timeout: float) -> None:
        self._broker.shutdown(timeout)
