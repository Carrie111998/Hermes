from __future__ import annotations

import pytest

from server.lead_research.candidates import CandidateRecord
from server.lead_research.enrichment import satisfied_playbook_fields
from server.lead_research.models import CampaignConfig, DiscoveryQuery
from server.lead_research.service import LeadResearchService


def config(**over) -> CampaignConfig:
    base = dict(
        name="Enrichment",
        target_countries=["DE"],
        sector_ids=["household-appliances"],
        enabled_source_ids=["brightdata-web"],
    )
    base.update(over)
    cfg = CampaignConfig(**base)
    cfg.enrichment.research_each_lead = True
    return cfg


def query() -> DiscoveryQuery:
    return DiscoveryQuery(
        campaign_id="c1",
        seller_countries=["TR"],
        target_countries=["DE"],
        sector_ids=["household-appliances"],
        hs_codes=[],
        buyer_types=["distributor"],
        max_records=10,
    )


def candidate() -> CandidateRecord:
    return CandidateRecord(
        dataset_id="d", version="1", source_record_id="rec-1",
        company_name="Acme Handel", normalized_name="acme handel",
        country="DE", domain=None, data={},
    )


class Bundle:
    def __init__(self, sources, record_id="rec-1", requests=0):
        self.sources = sources
        self.candidate_source_record_id = record_id
        # What the bundle cost. A real verifier reports this so a run can meter
        # its own spend; the enrichment pass has to carry it back out.
        self.requests = requests

    def model_copy(self, update):
        return Bundle(
            update["sources"], self.candidate_source_record_id, self.requests,
        )


class Source:
    def __init__(self, url, facts):
        self.provenance_url = url
        self.facts = facts


class Provider:
    def __init__(self, bundle=None, error=None):
        self.bundle, self.error, self.queries = bundle, error, []

    def verify(self, query, candidate):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.bundle


def service() -> LeadResearchService:
    return LeadResearchService.__new__(LeadResearchService)


@pytest.fixture()
def svc():
    from server.lead_research.enrichment import FeaturePlanner
    from server.lead_research.registry import build_registry
    s = service()
    s._planner = FeaturePlanner()
    s.registry = build_registry()
    return s


# A source that retrieves by company name returns the same records however the
# terms are phrased, so re-querying it buys nothing and costs a request.
def test_a_source_that_does_not_search_terms_is_not_asked_twice(svc):
    provider = Provider(Bundle([Source("https://e.example/5", {"store_count": ["3"]})]))
    extra, missing, spent = svc._enrich_candidate(
        config(enabled_source_ids=["ted"]), query(), candidate(),
        {"ted": provider}, ["ted"],
        [("ted", Bundle([Source("https://a.example/1", {"company_name": ["Acme"]})]))],
    )
    assert provider.queries == []
    assert extra == []
    assert missing


# The vocabularies were written apart: playbooks ask for "product_fit", a
# verifier emits "product_term". Without the bridge every gap looks open.
def test_verifier_facts_close_the_playbook_fields_they_stand_for():
    satisfied = satisfied_playbook_fields({"company_name", "country", "product_term"})
    assert {"identity_scale", "market_coverage", "product_fit"} <= satisfied


def test_a_candidate_with_no_open_gaps_costs_no_second_request(svc):
    full = {f: ["x"] for f in (
        "company_name", "country", "product_term", "buyer_role",
        "store_count", "relevant_import_value", "brands_carried",
    )}
    provider = Provider(Bundle([Source("https://a.example/1", {})]))
    extra, missing, spent = svc._enrich_candidate(
        config(), query(), candidate(), {"brightdata-web": provider}, ["brightdata-web"],
        [("brightdata-web", Bundle([Source("https://a.example/0", full)]))],
    )
    assert provider.queries == []
    assert extra == []
    assert missing == []


def test_the_second_pass_searches_the_sector_vocabulary_not_the_first_query(svc):
    provider = Provider(Bundle([Source("https://b.example/2", {"store_count": ["12"]})]))
    extra, _, spent = svc._enrich_candidate(
        config(), query(), candidate(), {"brightdata-web": provider}, ["brightdata-web"],
        [("brightdata-web", Bundle([Source("https://a.example/1", {"company_name": ["Acme Handel"]})]))],
    )
    (sent,) = provider.queries
    assert "white goods" in sent.sector_ids
    assert "private label" in sent.sector_ids
    assert "household-appliances" not in sent.sector_ids
    assert "wholesaler" in sent.buyer_types
    assert len(extra) == 1


# Re-fetching a page the first pass already cited costs the same and proves
# nothing new, so a repeat citation is dropped rather than double-counted.
def test_evidence_already_cited_is_not_counted_twice(svc):
    repeated = Provider(Bundle([Source("https://a.example/1", {"store_count": ["9"]})]))
    extra, _, spent = svc._enrich_candidate(
        config(), query(), candidate(), {"brightdata-web": repeated}, ["brightdata-web"],
        [("brightdata-web", Bundle([Source("https://a.example/1", {"company_name": ["Acme"]})]))],
    )
    assert extra == []


def test_a_failing_enrichment_never_costs_the_first_pass_its_evidence(svc):
    broken = Provider(error=RuntimeError("upstream 500"))
    first = [("brightdata-web", Bundle([Source("https://a.example/1", {"company_name": ["Acme"]})]))]
    extra, missing, spent = svc._enrich_candidate(
        config(), query(), candidate(), {"brightdata-web": broken}, ["brightdata-web"], first,
    )
    assert extra == []
    assert missing  # still reported as open rather than silently satisfied
    assert len(first) == 1


def test_a_bundle_answering_for_a_different_candidate_is_discarded(svc):
    liar = Provider(Bundle([Source("https://c.example/3", {"store_count": ["4"]})], "somebody-else"))
    extra, _, spent = svc._enrich_candidate(
        config(), query(), candidate(), {"brightdata-web": liar}, ["brightdata-web"],
        [("brightdata-web", Bundle([Source("https://a.example/1", {"company_name": ["Acme"]})]))],
    )
    assert extra == []


def test_an_unknown_sector_has_no_vocabulary_so_no_second_request(svc):
    provider = Provider(Bundle([Source("https://d.example/4", {})]))
    extra, _, spent = svc._enrich_candidate(
        config(sector_ids=["kitchen-appliances"]), query(), candidate(),
        {"brightdata-web": provider}, ["brightdata-web"],
        [("brightdata-web", Bundle([Source("https://a.example/1", {"company_name": ["Acme"]})]))],
    )
    assert provider.queries == []
    assert extra == []
