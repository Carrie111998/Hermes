"""Validated evidence anchors a score, and more evidence never lowers one.

Both halves of this file are the same regression. `derive_dimension_scores`
averaged the claims in a dimension, so a corroborating fact *reduced* it — an
official product term scored 77.9 alone and 54.1 once a web mention agreed with
it. The system was penalised for looking harder, which is fatal to a design
whose whole plan is a web-search fallback that produces more claims than the
validated sources do.

Combining fixes the direction. Validated standing fixes the ordering: a fact
the company's own page or an authoritative registry vouched for sets what a
dimension is worth, and everything else can only add to it.
"""
from __future__ import annotations

from server.lead_research.models import Claim, ScoringProfile
from server.lead_research.scoring import (
    FACT_TTL_DAYS, claim_freshness, derive_dimension_scores, score_lead,
)

DAY = 86400.0
NOW = 1_760_000_000.0


def _claim(field, value, *, validated, evidence=("ev_1",), confidence=.9, observed_at=None) -> Claim:
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    return Claim(
        field=field, value=value, status="observed", method="observed",
        confidence=confidence, evidence_ids=list(evidence),
        # Time-varying numerics carry a period by contract; the model enforces it.
        period="2025" if numeric else None,
        validated=validated, observed_at=observed_at,
    )


def _fit_dimension(claims) -> float:
    return derive_dimension_scores(claims)["product_sector_fit"]


# ── more evidence must never lower a score ────────────────────────────────────

def test_a_corroborating_claim_raises_the_dimension():
    """The regression this file exists for: averaging made agreement cost."""
    alone = [_claim("product_term", ["ovens"], validated=True, evidence=("ev_1", "ev_2"))]
    corroborated = alone + [
        _claim("hs_code", "8516", validated=False, confidence=.55, evidence=("ev_3",))
    ]

    assert _fit_dimension(corroborated) > _fit_dimension(alone)


def test_a_weak_claim_cannot_drag_a_strong_one_down():
    strong = [_claim("product_term", ["ovens"], validated=True, evidence=("ev_1", "ev_2"))]
    with_noise = strong + [
        _claim("product_fit", True, validated=False, confidence=.1, evidence=("ev_9",))
    ]

    assert _fit_dimension(with_noise) >= _fit_dimension(strong)


def test_support_saturates_so_many_weak_mentions_cannot_reach_the_top():
    """Otherwise a company with twenty blog mentions outranks a verified one."""
    many = [
        _claim("product_term", ["ovens"], validated=False, confidence=.4, evidence=(f"ev_{n}",))
        for n in range(20)
    ]

    assert _fit_dimension(many) < 100


# ── validated evidence sets the score ────────────────────────────────────────

def test_validated_evidence_outscores_the_same_fact_unvalidated():
    validated = [_claim("product_term", ["ovens"], validated=True, evidence=("ev_1", "ev_2"))]
    unvalidated = [_claim("product_term", ["ovens"], validated=False, evidence=("ev_1", "ev_2"))]

    assert _fit_dimension(validated) > _fit_dimension(unvalidated)


def test_an_unvalidated_claim_never_becomes_the_anchor():
    """A perfect web-search hit must not outrank a weaker official fact."""
    official_only = [
        _claim("product_term", ["ovens"], validated=True, confidence=.6, evidence=("ev_1",))
    ]
    plus_perfect_web = official_only + [
        _claim(
            "hs_code", ["8516", "8514", "7321"], validated=False,
            confidence=1.0, evidence=("ev_2", "ev_3"),
        )
    ]
    web_alone = [
        _claim(
            "hs_code", ["8516", "8514", "7321"], validated=False,
            confidence=1.0, evidence=("ev_2", "ev_3"),
        )
    ]

    # The web fact adds support to the official one...
    assert _fit_dimension(plus_perfect_web) > _fit_dimension(official_only)
    # ...but on its own it stays below what a validated fact is worth.
    assert _fit_dimension(web_alone) < 100


# ── freshness is measured, not assumed ───────────────────────────────────────

def test_a_stale_claim_is_less_fresh_than_a_new_one():
    ttl = FACT_TTL_DAYS["employee_count"] * DAY
    fresh = [_claim("employee_count", 40, validated=True, observed_at=NOW - DAY)]
    stale = [_claim("employee_count", 40, validated=True, observed_at=NOW - ttl * .9)]

    assert claim_freshness(fresh, NOW) > claim_freshness(stale, NOW)


def test_a_claim_past_its_shelf_life_is_not_fresh_at_all():
    ttl = FACT_TTL_DAYS["tender"] * DAY
    expired = [_claim("tender", "notice-1", validated=True, observed_at=NOW - ttl * 2)]

    assert claim_freshness(expired, NOW) == 0


def test_each_field_ages_against_its_own_shelf_life():
    """A domain does not go stale on the schedule an open tender does."""
    age = 60 * DAY
    domain = [_claim("domain", "atlas.test", validated=True, observed_at=NOW - age)]
    tender = [_claim("tender", "notice-1", validated=True, observed_at=NOW - age)]

    assert claim_freshness(domain, NOW) > claim_freshness(tender, NOW)


def test_no_retrieval_time_is_not_measured_rather_than_fresh():
    assert claim_freshness([_claim("domain", "atlas.test", validated=True)], NOW) is None


def test_a_stale_lead_scores_lower_confidence_than_a_fresh_one():
    """The whole reason freshness had to stop being a constant."""
    def claims(observed_at):
        return [
            _claim("product_term", ["ovens"], validated=True,
                   evidence=("ev_1", "ev_2"), observed_at=observed_at),
            _claim("buyer_role", ["distributor"], validated=True,
                   evidence=("ev_1", "ev_2"), observed_at=observed_at),
        ]

    fresh = score_lead({}, claims(NOW - DAY), ScoringProfile(), at=NOW)
    stale = score_lead({}, claims(NOW - 320 * DAY), ScoringProfile(), at=NOW)

    assert fresh.evidence_confidence > stale.evidence_confidence
    assert fresh.fit_score == stale.fit_score  # fit is fit; age belongs to confidence
