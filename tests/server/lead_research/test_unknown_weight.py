from __future__ import annotations

from server.lead_research.models import Claim, ScoringProfile, ScoringWeights
from server.lead_research.scoring import score_lead


def claim(field: str, value, *, observed_at=None) -> Claim:
    return Claim(
        field=field,
        value=value,
        status="observed",
        method="observed",
        confidence=.9,
        evidence_ids=["ev_1"],
        validated=True,
        observed_at=observed_at,
    )


def profile() -> ScoringProfile:
    return ScoringProfile(weights=ScoringWeights(
        product_sector_fit=40,
        buyer_channel_fit=30,
        buying_intent=0,
        market_coverage=0,
        commercial_scale=0,
        trade_activity=30,
        contactability=0,
    ))


def test_unknown_weight_is_reported_and_not_silently_removed():
    score = score_lead({}, [claim("product_sector_fit", 1.0)], profile())

    assert score.fit_score == 100
    assert score.known_weight == 40
    assert score.unknown_weight == 60
    assert score.unknown_dimensions == {
        "buyer_channel_fit": 30,
        "trade_activity": 30,
    }


def test_not_applicable_weight_is_distinct_from_unknown_weight():
    score = score_lead(
        {}, [claim("product_sector_fit", .8)], profile(),
        not_applicable={"trade_activity"},
    )

    assert score.known_weight == 40
    assert score.unknown_weight == 30
    assert score.unknown_dimensions == {"buyer_channel_fit": 30}
    assert score.not_applicable_dimensions == {"trade_activity": 30}


def test_staleness_changes_confidence_without_changing_fit():
    now = 2_000_000_000.0
    fresh = score_lead(
        {}, [claim("product_sector_fit", .8, observed_at=now)], profile(), at=now,
    )
    stale = score_lead(
        {}, [claim("product_sector_fit", .8, observed_at=now - 730 * 86_400)],
        profile(), at=now,
    )

    assert stale.fit_score == fresh.fit_score
    assert stale.evidence_confidence < fresh.evidence_confidence
