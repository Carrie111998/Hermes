from __future__ import annotations

import pytest

from server.lead_research.candidates import CandidateRecord
from server.lead_research.models import DatasetAssertionManifest, DiscoveryQuery
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
        dataset_id=data.pop("dataset_id", "ted-appliances"),
        version=data.pop("version", "1"),
        source_record_id="rec-1",
        company_name=name,
        normalized_name=name.casefold(),
        country=data.pop("country", "NL"),
        domain=data.pop("domain", None),
        assertion_manifest=data.pop("assertion_manifest", None),
        data=data,
    )


def curated_manifest(**overrides) -> DatasetAssertionManifest:
    return DatasetAssertionManifest.model_validate({
        "purpose": "curated_buyers",
        "asserted_fields": [
            "company_identity", "target_presence",
            "product_sector_relevance", "buyer_membership",
        ],
        "sector_ids": ["household-appliances"],
        "product_terms": [],
        "publisher_label": "Kitchen appliance customer list",
        "curated_at": 1787616000.0,
        "freshness_unknown": False,
        "curation_note": "Sanitized company-only export buyer list.",
        **overrides,
    })


# A curated buyer list carries an assertion, so it can speak for its rows --
# with an internal, immutable reference instead of a public URL, because there
# is no public page to cite. Losing this makes a hand-checked customer list
# score identically to a scraped name.
def test_manifest_backed_row_emits_retrievable_dataset_evidence(query):
    bundle = CorpusProvider().verify(query, candidate(
        dataset_id="kitchen-appliances",
        version="3",
        categories=["household-appliances"],
        assertion_manifest=curated_manifest(),
    ))
    (source,) = bundle.sources
    assert source.provenance_url is None
    assert source.source_reference == "dataset:kitchen-appliances:3:rec-1"
    assert source.locator == "dataset:kitchen-appliances:3:rec-1"
    assert source.facts == {
        "company_name": ["Coolblue"],
        "country": ["NL"],
        "buyer_role": ["sector_buyer"],
        "product_sector_fit": [90],
        "buyer_channel_fit": [85],
        "market_coverage": [80],
    }
    assert bundle.independent_source_count == 1


def test_unmanifested_row_still_abstains(query):
    assert CorpusProvider().verify(query, candidate()).sources == []


def test_a_directory_manifest_asserts_nothing_a_buyer_list_does(query):
    bundle = CorpusProvider().verify(query, candidate(
        categories=["household-appliances"],
        assertion_manifest=curated_manifest(purpose="directory", sector_ids=[]),
    ))
    assert bundle.sources == []


def test_only_asserted_dimensions_are_emitted(query):
    bundle = CorpusProvider().verify(query, candidate(
        categories=["household-appliances"],
        assertion_manifest=curated_manifest(
            asserted_fields=["company_identity", "product_sector_relevance"],
        ),
    ))
    (source,) = bundle.sources
    assert set(source.facts) == {"company_name", "country", "product_sector_fit"}


def test_a_row_outside_the_manifests_sector_gets_no_product_claim(query):
    bundle = CorpusProvider().verify(query, candidate(
        categories=["industrial-valves"],
        assertion_manifest=curated_manifest(),
    ))
    (source,) = bundle.sources
    assert "product_sector_fit" not in source.facts


def test_manifest_evidence_never_reads_a_contact_column(query):
    bundle = CorpusProvider().verify(query, candidate(
        categories=["household-appliances"],
        assertion_manifest=curated_manifest(),
        primary_email="jane@buyer.example",
        telephone_numbers="+31 20 000 0000",
        name="Jane Roe",
    ))
    (source,) = bundle.sources
    for leaked in ("jane@buyer.example", "+31 20 000 0000", "Jane Roe"):
        assert leaked not in source.snapshot_content
        assert all(leaked not in str(values) for values in source.facts.values())


def test_every_manifest_fact_is_span_validated_against_its_snapshot(query):
    bundle = CorpusProvider().verify(query, candidate(
        categories=["household-appliances"],
        assertion_manifest=curated_manifest(),
    ))
    (source,) = bundle.sources
    assert set(source.fact_spans) == set(source.facts)


# The corpus is candidate supply unless an operator asserted something about
# it. Letting an unasserted, uncited row vouch for itself would qualify every
# name anyone ever imported.
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
