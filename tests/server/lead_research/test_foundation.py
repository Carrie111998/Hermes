from pathlib import Path

import pytest
from pydantic import ValidationError

from server.db import Database, now
from server.lead_research.enrichment import FeaturePlanner
from server.lead_research.models import (
    Claim, DatasetDefinition, LeadCandidate, MarketSignal, ScoringProfile, ScoringWeights,
)
from server.lead_research.scoring import derive_dimension_scores, score_lead
from server.lead_research.registry import ProviderRegistry, build_registry
from server.lead_research.service import LeadResearchService
from server.lead_research.sectors import (
    SECTOR_CSV, SECTOR_MD, load_sectors, render_sector_csv, render_sector_markdown,
)


def test_production_catalog_has_no_fixture_adapter():
    registry = build_registry()
    assert all(item.adapter_mode != "fixture" for item in registry.list())
    assert "fixture-directory" not in {item.source_id for item in registry.list()}


# Built here rather than taken from the shipped catalog: the guarantee is about
# any adapter-less definition, and pinning it to whichever source happens to be
# uninstalled today makes the test fail when the catalog is curated instead of
# when the rule breaks.
CATALOG_ONLY = DatasetDefinition(
    source_id="declared-only",
    display_name="Declared Only",
    publisher="Nobody",
    access_tier="credentialed_public",
    entity_levels=["named_company"],
    adapter_mode="credential_required",
)


def test_catalog_only_provider_cannot_be_enabled_as_a_candidate_verifier(tmp_path):
    db = Database(tmp_path / "catalog.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("company_one", "Company One", "active", "{}", stamp, stamp),
    )
    service = LeadResearchService(db, registry=ProviderRegistry([CATALOG_ONLY]))
    service.ensure_catalog("company_one")
    db.execute(
        "UPDATE dataset_definitions SET enabled=1 WHERE company_id=? AND source_id=?",
        ("company_one", "declared-only"),
    )

    source = next(
        item for item in service.catalog("company_one") if item["source_id"] == "declared-only"
    )

    assert source["available"] is False
    assert source["unavailable_reason"] == "credential_required"


def test_every_shipped_source_can_actually_verify_a_candidate():
    """A definition with no verifier renders as permanently unavailable.

    That is indistinguishable from a broken integration on the Data Sources
    page, so the catalog only carries sources that work.
    """
    registry = build_registry()
    missing = [
        item.source_id for item in registry.list()
        if not callable(getattr(registry.get(item.source_id), "verify", None))
    ]
    assert missing == []


def test_generated_sector_artifacts_are_current():
    sectors = load_sectors()
    assert SECTOR_MD.read_text(encoding="utf-8") == render_sector_markdown(sectors)
    assert SECTOR_CSV.read_text(encoding="utf-8") == render_sector_csv(sectors)
    assert len({sector.sector_id for sector in sectors}) == len(sectors)


def test_aggregate_market_signal_cannot_become_named_lead():
    signal = MarketSignal(metric="import_value", value=10, currency="USD", period="2025")
    with pytest.raises(ValidationError):
        LeadCandidate(organization_id=None, qualifying_evidence=[signal])


def test_time_varying_numeric_claim_requires_period_and_evidence():
    with pytest.raises(ValidationError):
        Claim(
            field="store_count", value=84, status="observed", confidence=.8,
            method="observed", evidence_ids=[], applicability="useful",
        )


def test_missing_claims_never_receive_default_dimension_scores():
    score = score_lead({}, [], ScoringProfile())

    assert set(score.dimensions.values()) == {None}
    assert score.fit_score == 0


def test_weight_values_are_multiples_of_five():
    with pytest.raises(ValidationError):
        ScoringWeights(product_sector_fit=24, buyer_channel_fit=21)


def test_fit_is_derived_from_supported_claims_not_candidate_hints():
    weak = Claim(
        field="product_sector_fit", value=90, status="observed", confidence=.25,
        method="observed", evidence_ids=["ev_weak"], applicability="required",
    )
    score = score_lead({"dimension_scores": {key: 100 for key in ScoringProfile().weights.model_dump()}}, [weak], ScoringProfile())

    assert score.fit_score == 90
    assert score.evidence_confidence < .5
    assert score.priority_band != "A"


def test_unknown_and_calculated_claims_leave_dimensions_unknown():
    claims = [
        Claim(
            field="buyer_channel_fit", value=90, status="unknown", confidence=.9,
            method="observed", evidence_ids=[], applicability="useful",
        ),
        Claim(
            field="product_sector_fit", value=90, status="observed", confidence=.9,
            method="calculated", evidence_ids=["ev_calculated"], applicability="useful",
        ),
    ]

    assert derive_dimension_scores(claims) == {
        key: None for key in ScoringProfile().weights.model_dump()
    }


def test_verified_web_facts_populate_only_their_supported_dimensions():
    claims = [
        Claim(field="product_term", value="oven", status="observed", confidence=.9,
              method="observed", evidence_ids=["ev_product"]),
        Claim(field="buyer_role", value="distributor", status="observed", confidence=.9,
              method="observed", evidence_ids=["ev_buyer"]),
        Claim(field="domain", value="example.test", status="observed", confidence=.9,
              method="observed", evidence_ids=["ev_domain"]),
    ]

    scores = derive_dimension_scores(claims)

    # Which dimensions a fact can speak for is what this pins. The scores are
    # deliberately not 100: one value from one source is the weakest thing the
    # evidence model can say, and how far it gets is pinned in
    # test_fit_scoring.py.
    assert {key for key, value in scores.items() if value is not None} == {
        "product_sector_fit", "buyer_channel_fit", "contactability",
    }
    assert all(0 < scores[key] < 60 for key in (
        "product_sector_fit", "buyer_channel_fit", "contactability",
    ))


def test_sector_playbook_marks_store_count_not_applicable_to_industrial_machinery():
    fields = {item.field for item in FeaturePlanner().missing_claims({}, ["industrial-machinery"])}
    assert "store_count" not in fields
