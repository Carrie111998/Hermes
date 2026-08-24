"""Provider protocol and safe catalog-only adapter."""
from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from ..models import (
    DatasetDefinition,
    DiscoveryEstimate,
    DiscoveryQuery,
    EvidenceEnvelope,
    ProviderHealth,
    RawPage,
    RawRecord,
    VerificationBundle,
)
from ..candidates import CandidateRecord


class Provider(Protocol):
    definition: DatasetDefinition

    def discover(self, query: DiscoveryQuery) -> DiscoveryEstimate: ...
    def fetch_page(self, query: DiscoveryQuery, cursor: str | None) -> RawPage: ...
    def normalize(self, record: RawRecord, snapshot) -> list[EvidenceEnvelope]: ...
    def checkpoint(self, page: RawPage) -> str | None: ...
    def health(self) -> ProviderHealth: ...


@runtime_checkable
class CandidateSource(Protocol):
    definition: DatasetDefinition

    def discover_candidates(
        self, query: DiscoveryQuery, cursor: str | None = None,
    ) -> RawPage: ...


@runtime_checkable
class StructuredFactSource(Protocol):
    definition: DatasetDefinition

    def research_fields(
        self,
        company: CandidateRecord,
        fields: frozenset[str],
        query: DiscoveryQuery,
    ) -> VerificationBundle: ...


class CatalogProvider:
    """Represents a declared source whose live transport is not configured.

    It keeps access/licensing state visible without inventing a scraper or
    converting aggregate rows into named companies.
    """

    def __init__(self, definition: DatasetDefinition):
        self.definition = definition

    def discover(self, query: DiscoveryQuery) -> DiscoveryEstimate:
        return DiscoveryEstimate(
            kind="unavailable", basis="Source has no configured machine-access adapter", confidence="low"
        )

    def fetch_page(self, query: DiscoveryQuery, cursor: str | None) -> RawPage:
        raise RuntimeError(f"{self.definition.source_id} requires configured access or a permitted import")

    def normalize(self, record: RawRecord, snapshot) -> list[EvidenceEnvelope]:
        payload = record.payload
        record_type = payload.get("record_type")
        if record_type not in {"organization", "market_signal", "company_signal", "event", "opportunity"}:
            if self.definition.entity_levels == ["market"]:
                record_type = "market_signal"
            elif "opportunity" in self.definition.entity_levels:
                record_type = "opportunity"
            elif "event" in self.definition.entity_levels:
                record_type = "event"
            else:
                record_type = "organization"
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return [EvidenceEnvelope(
            evidence_id=f"ev_{hashlib.sha256(raw).hexdigest()[:20]}",
            source_id=self.definition.source_id,
            source_record_id=record.source_record_id,
            snapshot_id=snapshot.snapshot_id,
            record_type=record_type,
            jurisdiction=payload.get("country"),
            sector_ids=payload.get("sector_ids", []),
            provenance_url=payload.get("provenance_url"),
            raw_hash=hashlib.sha256(raw).hexdigest(),
            confidence=float(payload.get("confidence", .75)),
            payload=payload,
        )]

    def checkpoint(self, page: RawPage) -> str | None:
        return page.next_cursor

    def health(self) -> ProviderHealth:
        return ProviderHealth(status=self.definition.health)
