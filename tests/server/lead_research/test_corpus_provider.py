from __future__ import annotations

import pytest

from server.lead_research.candidates import CandidateRecord
from server.lead_research.models import DiscoveryQuery
from server.lead_research.providers.corpus import CorpusProvider


@pytest.fixture()
def query() -> DiscoveryQuery:
    return DiscoveryQuery(
        campaign_id="campaign-1",
        seller_countries=["TR"],
        target_countries=["NL"],
        sector_ids=["kitchen-appliances"],
        hs_codes=[],
        buyer_types=["distributor"],
        max_records=10,
    )


def candidate(**data) -> CandidateRecord:
    name = data.pop("company_name", "Coolblue")
    return CandidateRecord(
        dataset_id="ted-appliances",
        version="1",
        source_record_id="rec-1",
        company_name=name,
        normalized_name=name.casefold(),
        country=data.pop("country", "NL"),
        domain=data.pop("domain", None),
        data=data,
    )


# The corpus is candidate supply. Letting an uncited row vouch for itself would
# qualify every name anyone ever imported.
def test_a_row_without_a_citation_produces_no_evidence(query):
    bundle = CorpusProvider().verify(query, candidate())
    assert bundle.sources == []
    assert bundle.independent_source_count == 0


@pytest.mark.parametrize("url", ["http://ted.europa.eu/x", "not-a-url", "", "https://"])
def test_a_citation_that_is_not_an_https_record_is_ignored(query, url):
    bundle = CorpusProvider().verify(query, candidate(provenance_url=url))
    assert bundle.sources == []


def test_a_cited_row_is_evidence_carrying_its_own_provenance(query):
    bundle = CorpusProvider().verify(query, candidate(
        provenance_url="https://ted.europa.eu/en/notice/-/detail/217894-2025",
        domain="coolblue.nl",
        buyer_types=["public procurement supplier"],
        categories=["kitchen-appliances"],
    ))
    (source,) = bundle.sources
    assert source.provenance_url == "https://ted.europa.eu/en/notice/-/detail/217894-2025"
    assert source.classification == "independent"
    assert bundle.independent_source_count == 1
    assert source.facts["country"] == ["NL"]
    assert source.facts["domain"] == ["coolblue.nl"]
    assert source.facts["buyer_role"] == ["public procurement supplier"]
    assert source.facts["product_term"] == ["kitchen-appliances"]


# A company's own site is not an independent check on itself, so it must not
# satisfy an eligibility rule that asks for one.
def test_a_citation_to_the_candidates_own_site_is_not_independent(query):
    bundle = CorpusProvider().verify(query, candidate(
        provenance_url="https://www.coolblue.nl/over-ons",
        domain="coolblue.nl",
    ))
    (source,) = bundle.sources
    assert source.classification == "official"
    assert bundle.independent_source_count == 0


def test_the_bundle_always_answers_for_the_candidate_it_was_asked_about(query):
    bundle = CorpusProvider().verify(query, candidate(provenance_url="https://x.example/a"))
    assert bundle.candidate_source_record_id == "rec-1"
