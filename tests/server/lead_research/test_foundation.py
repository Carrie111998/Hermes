from pathlib import Path

import pytest
from pydantic import ValidationError

from server.lead_research.enrichment import FeaturePlanner
from server.lead_research.models import (
    Claim, LeadCandidate, MarketSignal, ScoringProfile,
)
from server.lead_research.scoring import score_lead
from server.lead_research.sectors import (
    SECTOR_CSV, SECTOR_MD, load_sectors, render_sector_csv, render_sector_markdown,
)


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


def test_fit_and_evidence_confidence_are_separate():
    weak = Claim(
        field="resolved_identity", value=True, status="observed", confidence=.25,
        method="observed", evidence_ids=["ev_weak"], applicability="required",
    )
    score = score_lead({"dimension_scores": {key: 90 for key in ScoringProfile().weights.model_dump()}}, [weak], ScoringProfile())
    assert score.fit_score >= 85
    assert score.evidence_confidence < .5
    assert score.priority_band != "A"


def test_sector_playbook_marks_store_count_not_applicable_to_industrial_machinery():
    fields = {item.field for item in FeaturePlanner().missing_claims({}, ["industrial-machinery"])}
    assert "store_count" not in fields
