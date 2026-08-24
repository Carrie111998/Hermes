from __future__ import annotations

from server.db import Database, now
from server.lead_research.discovery import CandidateDiscoveryService
from server.lead_research.candidates import CandidateRepository
from server.lead_research.models import (
    DatasetDefinition,
    DiscoveryQuery,
    ProviderHealth,
    RawPage,
    RawRecord,
    SnapshotRef,
)
from server.lead_research.registry import ProviderRegistry


class PublicCandidateSource:
    def __init__(self, definition):
        self.definition = definition

    def health(self):
        return ProviderHealth(status="active")

    def discover_candidates(self, query, cursor=None):
        del cursor
        return RawPage(
            snapshot=SnapshotRef(snapshot_id="snap_public", source_id=self.definition.source_id),
            records=[RawRecord(source_record_id="public-1", payload={
                "record_type": "lead_candidate",
                "company_name": "Neue Ventil GmbH",
                "country": query.target_countries[0],
                "domain": "neue-ventil.example",
                "categories": query.product_terms,
            })],
        )


def _definition(source_id="public-source", *, adapter_mode="live", default_enabled=True):
    return DatasetDefinition(
        source_id=source_id,
        display_name=source_id,
        publisher="Tests",
        access_tier="public",
        entity_levels=["named_company"],
        capabilities=["candidate_discovery"],
        adapter_mode=adapter_mode,
        default_enabled=default_enabled,
    )


def test_public_source_can_supply_candidates_when_corpus_is_empty(tmp_path):
    db = Database(tmp_path / "discovery.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_a", "Acme", "active", "{}", stamp, stamp),
    )
    definition = _definition()
    registry = ProviderRegistry(
        [definition], {definition.source_id: PublicCandidateSource(definition)},
    )
    query = DiscoveryQuery(
        campaign_id="rc_1",
        seller_countries=["TR"],
        target_countries=["DE"],
        product_terms=["industrial valve"],
        max_records=20,
    )

    supply = CandidateDiscoveryService(db, registry).supply("cmp_a", query, 20)

    assert [candidate.company_name for candidate in supply.candidates] == ["Neue Ventil GmbH"]
    assert supply.counts["public-source_discovered"] == 1


def test_customer_catalog_exposes_only_executable_sources():
    runnable = _definition()
    setup_only = _definition(
        "customer-list-corpus", adapter_mode="manual_import", default_enabled=False,
    )
    registry = ProviderRegistry(
        [runnable, setup_only],
        {runnable.source_id: PublicCandidateSource(runnable)},
    )

    assert all(item["runnable"] for item in registry.customer_catalog())
    assert {item["id"] for item in registry.customer_catalog()} == {"public-source"}
    assert {item["id"] for item in registry.admin_setup_catalog()} >= {
        "customer-list-corpus", "public-source",
    }


def test_discovery_collapses_different_names_on_the_same_domain(tmp_path):
    db = Database(tmp_path / "discovery-dedupe.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_a", "Acme", "active", "{}", stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "public", "1", "public.csv",
        b"source_record_id,company_name,country,domain,categories\n"
        b"idx-1,Neue Ventil Holding,DE,https://neue-ventil.example,industrial valve\n",
    )
    definition = _definition()
    registry = ProviderRegistry(
        [definition], {definition.source_id: PublicCandidateSource(definition)},
    )
    supply = CandidateDiscoveryService(db, registry).supply(
        "cmp_a",
        DiscoveryQuery(
            campaign_id="rc_1", seller_countries=["TR"], target_countries=["DE"],
            product_terms=["industrial valve"], max_records=20,
        ),
        20,
    )

    assert len(supply.candidates) == 1
    assert supply.counts["duplicates_collapsed"] == 1
