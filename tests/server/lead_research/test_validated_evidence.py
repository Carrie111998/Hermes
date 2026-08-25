"""More evidence never lowers a score, and who published it never changes fit.

The first half is the original regression. `derive_dimension_scores` averaged
the claims in a dimension, so a corroborating fact *reduced* it — an official
product term scored 77.9 alone and 54.1 once a web mention agreed with it. The
system was penalised for looking harder, which is fatal to a design whose whole
plan is a web-search fallback that produces more claims than the validated
sources do. Combining fixes the direction: every claim is either the anchor or
bounded support, and neither role can subtract.

The second half is the correction to the first fix. `validated` — a publisher
with standing vouched for this fact — used to discount the anchor, which made
fit a function of who filed the evidence rather than of the company. Fit is
business fit. `validated` still decides what may be shared between customers,
still carries provenance and corrections, and claim confidence still feeds
evidence confidence; it is simply absent from the fit arithmetic.
"""
from __future__ import annotations

from server.lead_research.models import Claim, ScoringProfile
from server.lead_research.scoring import (
    FACT_TTL_DAYS, claim_freshness, derive_dimension_scores, score_lead,
)
from tests.server.lead_research.test_vertical_slice import (
    campaign_body, make_research_client, start_and_settle,
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


# ── the same fact scores the same whoever filed it ───────────────────────────

def test_the_same_fact_scores_the_same_validated_or_not():
    validated = [_claim("product_term", ["ovens"], validated=True, evidence=("ev_1", "ev_2"))]
    unvalidated = [_claim("product_term", ["ovens"], validated=False, evidence=("ev_1", "ev_2"))]

    assert _fit_dimension(validated) == _fit_dimension(unvalidated)


def test_the_strongest_claim_is_the_anchor_whoever_validated_it():
    """A better-evidenced fact is a better fact, whatever published it.

    What still separates a weak mention from a real one is the evidence behind
    it — confidence, corroboration, breadth — all of which this measures.
    """
    official_only = [
        _claim("product_term", ["ovens"], validated=True, confidence=.6, evidence=("ev_1",))
    ]
    perfect_web = [
        _claim(
            "hs_code", ["8516", "8514", "7321"], validated=False,
            confidence=1.0, evidence=("ev_2", "ev_3"),
        )
    ]

    # More evidence still only ever adds.
    assert _fit_dimension(official_only + perfect_web) > _fit_dimension(official_only)
    # And a corroborated, three-value claim outranks a single weak one.
    assert _fit_dimension(perfect_web) > _fit_dimension(official_only)


def test_a_thin_claim_still_scores_thin_however_it_was_published():
    thin = [_claim("product_term", ["ovens"], validated=True, confidence=.4, evidence=("ev_1",))]

    assert _fit_dimension(thin) < 50


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


def test_campaign_dual_writes_exact_facts_to_shared_and_tenant_pools():
    app, client, headers, _ = make_research_client()
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()

    _, settled = start_and_settle(app, client, headers, campaign["id"])

    assert settled["status"] == "succeeded"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM shared_facts")["n"] > 0
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM tenant_facts WHERE company_id=?",
        (campaign["company_id"],),
    )["n"] > 0
