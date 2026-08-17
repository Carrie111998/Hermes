"""Provider-neutral candidate verification contract."""
from __future__ import annotations

from typing import Protocol

from .candidates import CandidateRecord
from .models import (
    DatasetDefinition,
    DiscoveryQuery,
    ProviderHealth,
    VerificationBundle,
)
from .providers.base import CatalogProvider


class CandidateVerifier(Protocol):
    def verify(
        self,
        query: DiscoveryQuery,
        candidate: CandidateRecord,
    ) -> VerificationBundle: ...


class UnavailableCandidateVerifier(CatalogProvider):
    """Visible catalog entry for a verifier that is deliberately gated off."""

    def __init__(
        self,
        definition: DatasetDefinition,
        reason: str,
    ) -> None:
        super().__init__(definition)
        if reason not in {"credential_required", "disabled"}:
            raise ValueError("unsupported verifier unavailability reason")
        self.reason = reason

    def verify(
        self,
        query: DiscoveryQuery,
        candidate: CandidateRecord,
    ) -> VerificationBundle:
        del query, candidate
        raise RuntimeError(f"{self.definition.source_id} is unavailable: {self.reason}")

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            status="unavailable",
            reason=self.reason,
            message="Candidate verification is not configured",
        )
