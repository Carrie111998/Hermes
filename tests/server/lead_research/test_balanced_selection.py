"""One primary list, decided after all the research, balanced across markets.

The campaign used to materialize a lead the instant a candidate cleared its
verdict, under a per-country ceiling of 50. So "leads" was whatever the corpus
happened to hold — 91 in one real run, 173 in another — and because the markets
were processed in sequence the first one spent the budget while the other four
returned nothing.

Materialization is now deferred: every evaluated candidate is persisted with
its verdict and no lead, the strong fits are ranked once at the end, and only
the displayed ones become operational leads. Reviews stay visible and stay
unmaterialized.

These tests assert relationships — pool ≥ displayed, displayed == primary
leads, per-country counts, honest shortfall — rather than which internal call
happened.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from server.db import Database, json_dump, json_load, now
from server.lead_research.candidates import CandidateRepository
from server.lead_research.models import (
    CampaignConfig,
    DatasetDefinition,
    ProviderHealth,
    RawPage,
    RawRecord,
    SnapshotRef,
    VerificationBundle,
    VerificationSource,
)
from server.lead_research.quotes import spans_for_facts
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService

COUNTRIES = ("DE", "ES", "FR", "PL", "RO")
MANIFEST = {
    "purpose": "curated_buyers",
    "asserted_fields": ["company_identity", "target_presence",
                        "product_sector_relevance", "buyer_membership"],
    "sector_ids": ["household-appliances"],
    "product_terms": [],
    "publisher_label": "Curated appliance buyer list",
    "curated_at": 1787616000.0,
    "freshness_unknown": False,
    "curation_note": "Sanitized company-only buyer list.",
}


def _source(url, classification, facts, retrieved_via=None):
    content = json.dumps(facts, sort_keys=True, ensure_ascii=False)
    return VerificationSource(
        provenance_url=url,
        raw_hash=hashlib.sha256(content.encode()).hexdigest(),
        classification=classification,
        retrieved_via=retrieved_via or url,
        facts=facts,
        snapshot_content=content,
        fact_spans=spans_for_facts(content, facts),
    )


class TieredVerifier:
    """Full dimension evidence for `strong-*` rows, one thin fact for `review-*`.

    Two sources per strong candidate so the evidence is corroborated, which is
    what carries confidence over the band-A threshold. `review-*` rows get one
    thin product mention: real evidence, band C, and never promotable.
    """

    def __init__(self, definition: DatasetDefinition):
        self.definition = definition
        self.calls: list[str] = []

    def health(self):
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        self.calls.append(candidate.source_record_id)
        identity = {
            "company_name": [candidate.company_name],
            "country": [candidate.country.upper()],
        }
        if candidate.source_record_id.startswith("review"):
            facts = {**identity, "product_term": ["household-appliances"]}
            return VerificationBundle(
                candidate_source_record_id=candidate.source_record_id,
                sources=[_source(
                    f"https://directory.example/{candidate.source_record_id}",
                    "independent", facts, "https://search.example",
                )],
                independent_source_count=1,
            )
        facts = {
            **identity,
            "buyer_role": ["distributor"],
            "product_sector_fit": [90],
            "buyer_channel_fit": [85],
            "market_coverage": [80],
        }
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                _source(f"https://{candidate.domain}", "official", facts),
                _source(
                    f"https://registry.example/{candidate.source_record_id}",
                    "independent", facts, "https://search.example",
                ),
            ],
            independent_source_count=1,
        )


class DuplicateDiscovery(TieredVerifier):
    """A discovery source that names one company the corpus already holds."""

    def __init__(self, definition, domain: str, country: str = "DE"):
        super().__init__(definition)
        self.domain, self.country = domain, country

    def discover_candidates(self, query, cursor=None):
        del cursor
        records = [RawRecord(source_record_id="discovered-1", payload={
            "record_type": "organization",
            "company_name": "Duplicate Buyer",
            "country": self.country,
            "domain": self.domain,
            "categories": ["household-appliances"],
            "buyer_types": ["distributor"],
        })] if self.country in query.target_countries else []
        return RawPage(
            snapshot=SnapshotRef(snapshot_id="snap_dup", source_id=self.definition.source_id),
            records=records, source_reported_total=len(records), next_cursor=None,
        )


def _definition(source_id: str) -> DatasetDefinition:
    return DatasetDefinition(
        source_id=source_id, display_name=source_id, publisher="Tests",
        access_tier="public", entity_levels=["named_company"],
        capabilities=["candidate_verification"],
        emits=["company_name", "country", "domain", "buyer_role", "product_term",
               "product_sector_fit", "buyer_channel_fit", "market_coverage"],
        adapter_mode="live", default_enabled=True,
    )


class Harness:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.company_id = "cmp_1"
        self.campaign_id = "camp_1"
        self.db: Database | None = None

    def run(
        self,
        *,
        strong_per_country=3,
        countries=5,
        review_count=0,
        source_ids=("catalog-a",),
        duplicate_domain=None,
    ) -> dict:
        self.db = Database(self.tmp_path / f"run-{len(source_ids)}-{strong_per_country}-{countries}-{review_count}-{duplicate_domain}.db")
        stamp = now()
        self.db.execute(
            "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (self.company_id, "Tenant", "active", "{}", stamp, stamp),
        )
        markets = list(COUNTRIES[:countries])
        rows = [
            {
                "source_record_id": f"strong-{country.lower()}-{index}",
                "company_name": f"Buyer {country} {index}",
                "country": country,
                "domain": f"https://buyer-{country.lower()}-{index}.example",
                "categories": ["household-appliances"],
                "buyer_types": ["distributor"],
            }
            for country in markets
            for index in range(1, strong_per_country + 1)
        ] + [
            {
                "source_record_id": f"review-{index}",
                "company_name": f"Maybe {index}",
                # Spread across the markets, as a real corpus is. Piling them
                # into one market would test the shortlist ceiling instead of
                # the selection rule.
                "country": markets[(index - 1) % len(markets)],
                "domain": f"https://maybe-{index}.example",
                "categories": ["household-appliances"],
                "buyer_types": ["distributor"],
            }
            for index in range(1, review_count + 1)
        ]
        if duplicate_domain:
            rows.append({
                "source_record_id": "strong-de-dup",
                "company_name": "Duplicate Buyer",
                "country": "DE",
                "domain": f"https://{duplicate_domain}",
                "categories": ["household-appliances"],
                "buyer_types": ["distributor"],
            })
        CandidateRepository(self.db).import_file(
            "curated-buyers", "1", "buyers.jsonl",
            "\n".join(json.dumps(row) for row in rows).encode(),
            assertion_manifest=MANIFEST,
        )
        config = CampaignConfig(
            name="Appliance buyers",
            seller_countries=["TR"],
            target_countries=markets,
            sector_ids=["household-appliances"],
            buyer_types=["distributor"],
            enabled_source_ids=list(source_ids),
        )
        self.db.execute(
            "INSERT INTO research_campaigns(id,company_id,name,status,version,config,estimate,"
            "run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.campaign_id, self.company_id, config.name, "draft", 1,
             json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
        )
        definitions, providers = [], {}
        for source_id in source_ids:
            definition = _definition(source_id)
            definitions.append(definition)
            providers[source_id] = (
                DuplicateDiscovery(definition, duplicate_domain)
                if duplicate_domain and source_id == source_ids[-1]
                else TieredVerifier(definition)
            )
        service = LeadResearchService(
            self.db, registry=ProviderRegistry(definitions, providers),
        )
        return service.run(self.company_id, self.campaign_id)

    def results(self) -> list[dict]:
        return [
            {**dict(row), "selection": json_load(row["data"], {}).get("selection", {})}
            for row in self.db.all(
                "SELECT id,verdict,lead_id,data FROM research_results "
                "WHERE campaign_id=? ORDER BY id",
                (self.campaign_id,),
            )
        ]

    def displayed(self) -> list[dict]:
        return [row for row in self.results() if row["selection"].get("displayed")]


@pytest.fixture()
def harness(tmp_path):
    return Harness(tmp_path)


def test_only_balanced_displayed_strong_fits_become_primary_leads(harness):
    output = harness.run(strong_per_country=4, review_count=3)
    metrics = output["metrics"]
    rows = harness.results()
    displayed = harness.displayed()

    assert metrics["strong_fit_pool"] == 20
    assert metrics["qualified_leads"] == len(displayed) == 15
    assert metrics["review_candidates"] == metrics["review_leads"] == 3
    assert metrics["outside_result_limit"] == 5
    assert all(row["verdict"] == "strong_fit" and row["lead_id"] for row in displayed)
    assert all(row["lead_id"] is None for row in rows if row["verdict"] == "review")
    assert metrics["leads_by_country"] == {"DE": 3, "ES": 3, "FR": 3, "PL": 3, "RO": 3}
    assert metrics["countries_represented"] == 5
    assert metrics["result_target_min"] == 5
    assert metrics["result_limit"] == 15
    assert metrics["result_shortfall"] == 0


def test_overflow_strong_fits_are_recorded_without_a_lead(harness):
    harness.run(strong_per_country=4, review_count=0)
    overflow = [
        row for row in harness.results()
        if row["verdict"] == "strong_fit" and not row["selection"]["displayed"]
    ]

    assert len(overflow) == 5
    assert all(row["lead_id"] is None for row in overflow)
    assert all(row["selection"]["reason"] == "outside_result_limit" for row in overflow)
    assert all(row["selection"]["display_rank"] is None for row in overflow)


def test_four_strong_fits_report_honest_shortfall_without_reviews(harness):
    output = harness.run(strong_per_country=1, countries=4, review_count=20)
    metrics = output["metrics"]

    assert metrics["qualified_leads"] == 4
    assert metrics["result_shortfall"] == 1
    assert metrics["shortfall_reasons"]["review_candidates"] == 20
    assert all(row["lead_id"] is None for row in harness.results() if row["verdict"] == "review")


def test_source_order_cannot_change_the_shortlist_or_final_ranks(harness, tmp_path):
    forward = Harness(tmp_path / "forward")
    reverse = Harness(tmp_path / "reverse")
    (tmp_path / "forward").mkdir()
    (tmp_path / "reverse").mkdir()

    forward.run(strong_per_country=4, source_ids=("catalog-a", "catalog-b"))
    reverse.run(strong_per_country=4, source_ids=("catalog-b", "catalog-a"))

    def shape(item):
        return [
            (row["selection"]["display_rank"], json_load(row["data"], {})["score"]["fit_score"])
            for row in sorted(item.displayed(), key=lambda r: r["selection"]["display_rank"])
        ]

    assert shape(forward) == shape(reverse)
    assert len(forward.displayed()) == len(reverse.displayed()) == 15


def test_duplicate_identity_from_corpus_and_discovery_is_evaluated_once(harness):
    """One company named by two sources is one candidate and one lead.

    Charging a customer twice for the same buyer, and giving it two slots in a
    list of fifteen, is the failure. Identity is collapsed on the normalized
    domain before any verification is paid for.
    """
    output = harness.run(
        strong_per_country=1, countries=1,
        source_ids=("catalog-a", "catalog-b"),
        duplicate_domain="same-buyer.example",
    )

    assert output["metrics"]["candidate_supply_duplicates_collapsed"] == 1
    organizations = harness.db.all(
        "SELECT id,display_name FROM organizations WHERE company_id=?",
        (harness.company_id,),
    )
    assert [row["display_name"] for row in organizations].count("Duplicate Buyer") == 1
    # Two corpus rows in DE: the plain strong fit, and the one the discovery
    # source also named. Both qualify; neither is counted twice.
    assert output["metrics"]["strong_fit_pool"] == 2
    assert output["metrics"]["qualified_leads"] == 2
    assert harness.db.one(
        "SELECT COUNT(*) AS n FROM leads WHERE company_id=?", (harness.company_id,),
    )["n"] == 2
