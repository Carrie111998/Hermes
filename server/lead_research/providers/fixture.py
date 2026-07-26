"""Deterministic offline provider used for contract and end-to-end tests."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..models import (
    DatasetDefinition,
    DiscoveryEstimate,
    DiscoveryQuery,
    EvidenceEnvelope,
    ProviderHealth,
    RawPage,
    RawRecord,
    SnapshotRef,
)


class FixtureProvider:
    def __init__(self, definition: DatasetDefinition):
        self.definition = definition

    def discover(self, query: DiscoveryQuery) -> DiscoveryEstimate:
        countries = max(1, len(query.target_countries))
        return DiscoveryEstimate(
            kind="reported", low=2 * countries, high=3 * countries,
            basis="Deterministic named-company fixture records", confidence="high",
        )

    def fetch_page(self, query: DiscoveryQuery, cursor: str | None) -> RawPage:
        records: list[RawRecord] = []
        sectors = query.sector_ids or ["household-appliances"]
        for country in query.target_countries:
            slug = country.lower()
            records.extend([
                RawRecord(source_record_id=f"market-{slug}", payload={
                    "record_type": "market_signal", "country": country,
                    "metric": "addressable_market_value", "value": 125_000_000,
                    "currency": "EUR", "period": "2025", "sector_ids": sectors,
                    "provenance_url": f"https://data.example.test/markets/{slug}",
                }),
                RawRecord(source_record_id=f"buyer-{slug}-1", payload={
                    "record_type": "organization", "display_name": f"Atlas {country} Distribution",
                    "legal_name": f"Atlas {country} Distribution Ltd", "country": country,
                    "domain": f"atlas-{slug}.example.test", "registry_id": f"{country}-ATLAS-001",
                    "buyer_types": ["importer", "distributor"], "sector_ids": sectors,
                    "buying_intent": "active sourcing brief", "locations": 7,
                    "provenance_url": f"https://registry.example.test/{country}/ATLAS-001",
                }),
                RawRecord(source_record_id=f"buyer-{slug}-2", payload={
                    "record_type": "organization", "display_name": f"Northstar {country} Retail",
                    "legal_name": f"Northstar {country} Retail SA", "country": country,
                    "domain": f"northstar-{slug}.example.test", "registry_id": f"{country}-NORTH-002",
                    "buyer_types": ["retailer", "wholesaler"], "sector_ids": sectors,
                    "store_count": 24, "brands_carried": 18,
                    "provenance_url": f"https://registry.example.test/{country}/NORTH-002",
                }),
            ])
        snapshot_seed = f"{query.campaign_id}:{','.join(query.target_countries)}:{','.join(sectors)}"
        snapshot_id = f"snap_{hashlib.sha256(snapshot_seed.encode()).hexdigest()[:20]}"
        return RawPage(
            snapshot=SnapshotRef(snapshot_id=snapshot_id, source_id=self.definition.source_id),
            records=records[:query.max_records], source_reported_total=len(records), next_cursor=None,
        )

    def normalize(self, record: RawRecord, snapshot: SnapshotRef) -> list[EvidenceEnvelope]:
        raw = json.dumps(record.payload, sort_keys=True, ensure_ascii=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        return [EvidenceEnvelope(
            evidence_id=f"ev_{digest[:20]}", source_id=self.definition.source_id,
            source_record_id=record.source_record_id, snapshot_id=snapshot.snapshot_id,
            record_type=record.payload["record_type"], observed_at=datetime.now(timezone.utc),
            jurisdiction=record.payload.get("country"), sector_ids=record.payload.get("sector_ids", []),
            provenance_url=record.payload.get("provenance_url"), raw_hash=digest,
            method="observed", confidence=.92, payload=record.payload,
        )]

    def checkpoint(self, page: RawPage) -> str | None:
        return page.next_cursor

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="active", message="Offline deterministic contract fixture")
