"""A rerun must not pay twice for evidence it already holds.

Verifying one candidate costs three Web Unlocker fetches, and a rerun re-fetched
every page it had already bought. Evidence is immutable and content-addressed,
so the stored rows rebuild the bundle the provider would have returned.

These tests count provider calls, because "the same verdict" is not the claim —
"no request was made" is.
"""
from __future__ import annotations

import json

import pytest

from server.db import Database, json_dump, now
from server.lead_research.candidates import CandidateRepository
from server.lead_research.models import (
    CampaignConfig,
    DatasetDefinition,
    DiscoveryQuery,
    ProviderHealth,
    VerificationBundle,
    VerificationSource,
)
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService
from server.lead_research.storage import EvidenceRepository


class CountingVerifier:
    """Records every candidate it was actually asked about."""

    def __init__(self, definition, *, terms_in_facts=True):
        self.definition = definition
        self.calls: list[str] = []
        self.terms_in_facts = terms_in_facts

    def health(self):
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        self.calls.append(candidate.source_record_id)
        facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "domain": [candidate.domain] if candidate.domain else [],
            "buyer_role": ["distributor"],
        }
        if not facts["domain"]:
            facts.pop("domain")
        if self.terms_in_facts:
            # What a web verifier really does: the facts are the campaign's own
            # terms, matched against the page.
            facts["product_term"] = list(query.sector_ids)
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                VerificationSource(
                    provenance_url=f"https://{candidate.domain}/about",
                    raw_hash="a" * 64,
                    classification="official",
                    retrieved_via=f"https://{candidate.domain}",
                    facts=facts,
                ),
                VerificationSource(
                    provenance_url=f"https://registry.example/{candidate.source_record_id}",
                    raw_hash="b" * 64,
                    classification="independent",
                    retrieved_via="https://search.example",
                    facts=facts,
                ),
            ],
            independent_source_count=1,
        )


def _definition(freshness_days=30) -> DatasetDefinition:
    return DatasetDefinition(
        source_id="counting-source",
        display_name="Counting source",
        publisher="Tests",
        access_tier="public",
        entity_levels=["named_company"],
        capabilities=["candidate_verification"],
        emits=["company_name", "country", "domain", "buyer_role", "product_term"],
        freshness_days=freshness_days,
        adapter_mode="live",
        default_enabled=True,
    )


@pytest.fixture()
def harness(tmp_path):
    db = Database(tmp_path / "reuse.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_1", "Tenant", "active", "{}", stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "buyers", "1", "candidates.jsonl",
        b"\n".join(
            json.dumps({
                "source_record_id": f"buyer-de-{index}",
                "company_name": f"Buyer {index} DE",
                "country": "DE",
                "domain": f"https://buyer{index}.example",
                "categories": ["household-appliances"],
            }).encode()
            for index in (1, 2)
        ),
    )
    return db


def _campaign(db, campaign_id, definition, **config_overrides) -> CampaignConfig:
    config = CampaignConfig(**{
        "name": "German appliance distributors",
        "target_countries": ["DE"],
        "sector_ids": ["household-appliances"],
        "buyer_types": ["distributor"],
        "enabled_source_ids": [definition.source_id],
        **config_overrides,
    })
    stamp = now()
    db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, "cmp_1", config.name, "draft", 1,
         json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    return config


def _service(db, definition, verifier) -> LeadResearchService:
    return LeadResearchService(
        db, registry=ProviderRegistry([definition], {definition.source_id: verifier})
    )


# ── the saving ────────────────────────────────────────────────────────────────

def test_a_rerun_does_not_re_verify_what_it_already_holds(harness):
    """The regression this file exists for."""
    definition = _definition()
    verifier = CountingVerifier(definition)
    _campaign(harness, "camp_1", definition)
    service = _service(harness, definition, verifier)

    first = service.run("cmp_1", "camp_1")
    calls_after_first = list(verifier.calls)
    second = service.run("cmp_1", "camp_1")

    # Sorted: candidates in a batch are verified concurrently, so the order
    # they reach a provider in is not a contract. Which ones, and how many
    # times, is.
    assert sorted(calls_after_first) == ["buyer-de-1", "buyer-de-2"]
    assert verifier.calls == calls_after_first, "the rerun re-fetched pages it already had"
    assert second["metrics"]["reused_bundles"] == 2
    assert first["metrics"]["reused_bundles"] == 0


def test_a_reused_run_reaches_the_same_verdict(harness):
    """Reuse must change the cost, not the answer."""
    definition = _definition()
    _campaign(harness, "camp_1", definition)
    service = _service(harness, definition, CountingVerifier(definition))

    service.run("cmp_1", "camp_1")
    before = harness.all(
        "SELECT organization_id,verdict,fit_score,evidence_confidence FROM research_results "
        "WHERE campaign_id='camp_1' ORDER BY organization_id"
    )
    service.run("cmp_1", "camp_1")
    after = harness.all(
        "SELECT organization_id,verdict,fit_score,evidence_confidence FROM research_results "
        "WHERE campaign_id='camp_1' ORDER BY organization_id"
    )

    assert [dict(row) for row in before] == [dict(row) for row in after]
    assert before, "the fixture produced no results to compare"


def test_a_second_campaign_reuses_the_first_campaign_evidence(harness):
    """Evidence belongs to the tenant, not to the campaign that paid for it."""
    definition = _definition()
    verifier = CountingVerifier(definition)
    _campaign(harness, "camp_1", definition)
    _campaign(harness, "camp_2", definition)
    service = _service(harness, definition, verifier)

    service.run("cmp_1", "camp_1")
    service.run("cmp_1", "camp_2")

    assert sorted(verifier.calls) == ["buyer-de-1", "buyer-de-2"]


def test_reuse_is_reported_per_partition(harness):
    definition = _definition()
    _campaign(harness, "camp_1", definition)
    service = _service(harness, definition, CountingVerifier(definition))

    service.run("cmp_1", "camp_1")
    service.run("cmp_1", "camp_1")

    partition = harness.one(
        "SELECT metrics FROM campaign_partitions WHERE campaign_id='camp_1'"
    )
    assert json.loads(partition["metrics"])["reused_candidates"] == 2


# ── when reuse must not happen ────────────────────────────────────────────────

def test_changing_the_campaign_terms_re_verifies(harness):
    """The trap this would otherwise set.

    A web verifier emits `product_term` by matching the campaign's own terms
    against the page, and fit is scored on how many matched. Reusing evidence
    gathered under different terms would make an edited campaign silently ignore
    its own edit — and editing then rerunning is how this system gets tuned.
    """
    definition = _definition()
    verifier = CountingVerifier(definition)
    _campaign(harness, "camp_1", definition)
    # buyer_types, not sector_ids: sector ids also drive candidate selection,
    # so changing them would select nothing and the assertion would hold for the
    # wrong reason. This changes the question, not the shortlist.
    _campaign(harness, "camp_2", definition, buyer_types=["retailer"])
    service = _service(harness, definition, verifier)

    service.run("cmp_1", "camp_1")
    service.run("cmp_1", "camp_2")

    assert sorted(verifier.calls) == [
        "buyer-de-1", "buyer-de-1", "buyer-de-2", "buyer-de-2",
    ], "a campaign asking a different question reused the old answer"


def test_turning_the_cache_off_re_verifies(harness):
    definition = _definition()
    verifier = CountingVerifier(definition)
    _campaign(
        harness, "camp_1", definition,
        refresh={"schedule": "monthly", "reuse_public_cache": False},
    )
    service = _service(harness, definition, verifier)

    service.run("cmp_1", "camp_1")
    service.run("cmp_1", "camp_1")

    assert len(verifier.calls) == 4


def test_evidence_older_than_the_source_freshness_window_is_re_verified(harness):
    definition = _definition(freshness_days=7)
    verifier = CountingVerifier(definition)
    _campaign(harness, "camp_1", definition)
    service = _service(harness, definition, verifier)
    service.run("cmp_1", "camp_1")

    # Age the stored evidence past the window rather than mocking the clock.
    harness.execute(
        "UPDATE evidence_records SET retrieved_at=? WHERE company_id='cmp_1'",
        (now() - 8 * 86400,),
    )
    service.run("cmp_1", "camp_1")

    assert len(verifier.calls) == 4


def test_reuse_does_not_refresh_the_age_of_the_evidence(harness):
    """Otherwise cached evidence would never expire.

    A reused bundle that restamped `retrieved_at` would keep itself inside the
    freshness window for as long as anyone kept looking at it.
    """
    definition = _definition()
    _campaign(harness, "camp_1", definition)
    service = _service(harness, definition, CountingVerifier(definition))
    service.run("cmp_1", "camp_1")
    original = [
        row["retrieved_at"] for row in harness.all(
            "SELECT retrieved_at FROM evidence_records WHERE company_id='cmp_1' ORDER BY id"
        )
    ]

    service.run("cmp_1", "camp_1")

    assert [
        row["retrieved_at"] for row in harness.all(
            "SELECT retrieved_at FROM evidence_records WHERE company_id='cmp_1' ORDER BY id"
        )
    ] == original


def test_withdrawn_evidence_is_never_reused(harness):
    """Purging a source has to actually cost its evidence."""
    definition = _definition()
    verifier = CountingVerifier(definition)
    _campaign(harness, "camp_1", definition)
    service = _service(harness, definition, verifier)
    service.run("cmp_1", "camp_1")

    harness.execute(
        "UPDATE evidence_records SET withdrawn_at=? WHERE company_id='cmp_1'", (now(),)
    )
    service.run("cmp_1", "camp_1")

    assert len(verifier.calls) == 4


def test_another_tenants_evidence_is_never_reused(harness):
    definition = _definition()
    verifier = CountingVerifier(definition)
    _campaign(harness, "camp_1", definition)
    service = _service(harness, definition, verifier)
    service.run("cmp_1", "camp_1")
    stamp = now()
    harness.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_other", "Other tenant", "active", "{}", stamp, stamp),
    )
    harness.execute(
        "UPDATE evidence_records SET company_id='cmp_other',campaign_id=NULL "
        "WHERE company_id='cmp_1'"
    )

    service.run("cmp_1", "camp_1")

    assert len(verifier.calls) == 4


# ── the fingerprint itself ───────────────────────────────────────────────────

def _query(**overrides) -> DiscoveryQuery:
    return DiscoveryQuery(**{
        "campaign_id": "c", "seller_countries": ["TR"], "target_countries": ["DE"],
        "sector_ids": ["household-appliances"], "hs_codes": [], "buyer_types": ["distributor"],
        **overrides,
    })


def test_term_order_does_not_change_the_fingerprint():
    """Otherwise reordering a list in the editor would discard the whole cache."""
    assert EvidenceRepository.query_fingerprint(
        _query(buyer_types=["distributor", "importer"])
    ) == EvidenceRepository.query_fingerprint(
        _query(buyer_types=["importer", "distributor"])
    )


def test_target_country_does_not_fragment_the_fingerprint():
    """Extraction keys off the candidate's own country, which is immutable."""
    assert EvidenceRepository.query_fingerprint(
        _query(target_countries=["DE"])
    ) == EvidenceRepository.query_fingerprint(_query(target_countries=["AT"]))


@pytest.mark.parametrize("change", [
    {"sector_ids": ["industrial-machinery"]},
    {"hs_codes": ["8418"]},
    {"buyer_types": ["retailer"]},
])
def test_anything_that_changes_extraction_changes_the_fingerprint(change):
    assert (
        EvidenceRepository.query_fingerprint(_query(**change))
        != EvidenceRepository.query_fingerprint(_query())
    )
