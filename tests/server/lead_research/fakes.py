"""Deterministic lead-research providers for server contract tests."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from server.lead_research.models import (
    DatasetDefinition,
    DiscoveryEstimate,
    DiscoveryQuery,
    EvidenceEnvelope,
    ProviderHealth,
    RawPage,
    RawRecord,
    SnapshotRef,
    VerificationBundle,
    VerificationSource,
)


def fixture_definition() -> DatasetDefinition:
    return DatasetDefinition(
        source_id="fixture-directory",
        display_name="Verified buyer directory fixture",
        publisher="Interfaze test fixtures",
        jurisdiction=["global"],
        categories=["registry", "opportunity"],
        access_tier="public",
        entity_levels=["named_company", "opportunity"],
        capabilities=["organizations", "company_signals", "buying_requests"],
        freshness_days=30,
        adapter_mode="fixture",
        default_enabled=True,
    )


class DeterministicProvider:
    def __init__(self, definition: DatasetDefinition):
        self.definition = definition

    def discover(self, query: DiscoveryQuery) -> DiscoveryEstimate:
        countries = max(1, len(query.target_countries))
        return DiscoveryEstimate(
            kind="reported", low=2 * countries, high=3 * countries,
            basis="Deterministic named-company test records", confidence="high",
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
        return ProviderHealth(status="active", message="Offline deterministic contract fake")

    def verify(self, query, candidate) -> VerificationBundle:
        del query
        # The role this company turns out to have, not a fixed one. A real
        # verifier reads it off the page it fetched about this candidate, so
        # stamping every candidate "distributor" made the fake contradict any
        # corpus row that said otherwise — and eligibility reads observed roles.
        roles = [str(value) for value in candidate.data.get("buyer_types") or []] or ["distributor"]
        role_phrase = " and ".join(roles)
        official_markdown = (
            f"{candidate.company_name} is a {role_phrase} of household appliances in {candidate.country}."
        )
        independent_markdown = (
            f"Registry profile for {candidate.company_name}, a household-appliances {role_phrase}."
        )
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                VerificationSource(
                    provenance_url=f"https://{candidate.domain}",
                    raw_hash=hashlib.sha256(official_markdown.encode()).hexdigest(),
                    classification="official",
                    retrieved_via=f"https://{candidate.domain}",
                    facts={
                        "company_name": [candidate.company_name],
                        "country": [candidate.country],
                        "domain": [candidate.domain],
                        "buyer_role": roles,
                        "product_term": ["household-appliances"],
                    },
                ),
                VerificationSource(
                    provenance_url=f"https://registry.example.test/{candidate.source_record_id}",
                    raw_hash=hashlib.sha256(independent_markdown.encode()).hexdigest(),
                    classification="independent",
                    retrieved_via="https://search.example.test",
                    facts={
                        "company_name": [candidate.company_name],
                        "buyer_role": roles,
                        "product_term": ["household-appliances"],
                    },
                ),
            ],
            independent_source_count=1,
        )


def deterministic_provider(definition: DatasetDefinition) -> DeterministicProvider:
    return DeterministicProvider(definition)
