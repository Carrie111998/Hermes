"""Fit has to separate leads, not just confirm they exist.

`_claim_score` returned 100.0 for any truthy claim value, and `score_lead`
divides by the weight of the dimensions a lead actually has — so one product
term found in one search snippet scored the same as a company corroborated
across four sources, and nearly every lead came out at fit 100. Two real runs
produced 91 and 173 leads, every one of them fit 100 and `review`: a ranked
list with nothing to rank by. Ranking is the product.

These tests pin the ordering rather than the arithmetic. The exact anchors live
in one place — `scoring.py` — and only the relative order is a contract.
"""
from __future__ import annotations

import pytest

from server.lead_research.models import Claim, ScoringProfile, VerificationBundle
from server.lead_research.scoring import derive_dimension_scores, score_lead


def _claim(field, value, *, confidence=.9, evidence=("ev_1",), status="observed") -> Claim:
    return Claim(
        field=field, value=value, status=status, confidence=confidence,
        method="observed", evidence_ids=list(evidence),
    )


def _fit(claims) -> int:
    return score_lead({}, claims, ScoringProfile()).fit_score


def _dimension(claim: Claim) -> float:
    scores = derive_dimension_scores([claim])
    return next(value for value in scores.values() if value is not None)


# ── degree, not presence ──────────────────────────────────────────────────────

def test_a_second_source_scores_higher_than_one():
    """The regression this file exists for: corroboration used to be free."""
    alone = _claim("product_term", ["ovens"], evidence=("ev_1",))
    corroborated = _claim("product_term", ["ovens"], evidence=("ev_1", "ev_2"))

    assert _dimension(corroborated) > _dimension(alone)


def test_more_matched_terms_score_higher_than_one():
    narrow = _claim("product_term", ["ovens"], evidence=("ev_1", "ev_2"))
    broad = _claim("product_term", ["ovens", "white goods", "hobs"], evidence=("ev_1", "ev_2"))

    assert _dimension(broad) > _dimension(narrow)


def test_an_official_source_outscores_an_independent_one():
    """Authority reaches the score through the claim's own confidence."""
    independent = _claim("buyer_role", ["distributor"], confidence=.85)
    official = _claim("buyer_role", ["distributor"], confidence=.95)

    assert _dimension(official) > _dimension(independent)


def test_repeating_one_source_is_not_corroboration():
    """Evidence ids are deduplicated, so a source cannot vouch for itself twice."""
    once = _claim("product_term", ["ovens"], evidence=("ev_1",))
    twice = _claim("product_term", ["ovens"], evidence=("ev_1", "ev_1"))

    assert _dimension(twice) == _dimension(once)


def test_corroboration_stops_paying_after_a_second_source():
    """Two agreeing sources is the standard `evaluate_verdict` already uses.

    Paying for a fourth would let a company with many weak mentions outrank one
    with an official page and an independent registry agreeing.
    """
    two = _claim("product_term", ["ovens"], evidence=("ev_1", "ev_2"))
    four = _claim("product_term", ["ovens"], evidence=("ev_1", "ev_2", "ev_3", "ev_4"))

    assert _dimension(four) == _dimension(two)


def test_no_evidence_scores_nothing_at_all():
    """A dimension with no supported claim stays unknown, not zero."""
    scores = derive_dimension_scores([])

    assert set(scores.values()) == {None}


# ── the ordering the lead list depends on ─────────────────────────────────────

def _thin():
    return [
        _claim("product_term", ["household-appliances"], confidence=.85, evidence=("ev_i",)),
        _claim("buyer_role", ["public procurement supplier"], confidence=.85, evidence=("ev_i",)),
    ]


def _ordinary():
    return [
        _claim("product_term", ["household-appliances"], evidence=("ev_o", "ev_i")),
        _claim("buyer_role", ["distributor"], evidence=("ev_o", "ev_i")),
        _claim("domain", "atlas.test", confidence=.95, evidence=("ev_o",)),
    ]


def _strong():
    return [
        _claim(
            "product_term", ["white goods", "built-in ovens", "household-appliances"],
            confidence=.88, evidence=("ev_o", "ev_i", "ev_j"),
        ),
        _claim("buyer_role", ["distributor", "wholesaler"], confidence=.88, evidence=("ev_o", "ev_i")),
        _claim("domain", "atlas.test", confidence=.95, evidence=("ev_o",)),
    ]


def test_thinner_evidence_ranks_below_stronger_evidence():
    assert _fit(_thin()) < _fit(_ordinary()) < _fit(_strong())


def test_the_three_tiers_land_in_different_priority_bands():
    """Bands are what the customer sorts by, so the tiers must not collapse."""
    bands = [
        score_lead({}, claims, ScoringProfile()).priority_band
        for claims in (_thin(), _ordinary(), _strong())
    ]

    assert bands == ["C", "B", "A"]


def test_strong_evidence_can_still_reach_the_top_band():
    """Scoring by degree must not make the top band unreachable."""
    assert _fit(_strong()) >= ScoringProfile().bands["A"].min_fit


def test_one_mention_cannot_reach_the_top_band():
    single = [_claim("product_term", ["ovens"], evidence=("ev_1",))]

    assert score_lead({}, single, ScoringProfile()).priority_band != "A"


def test_a_lead_with_one_dimension_no_longer_scores_a_perfect_fit():
    """The exact shape of the old defect.

    One dimension known, divided by its own weight, used to be fit 100 — so a
    company with a single matched term outranked nothing and tied everything.
    """
    single = [_claim("product_term", ["ovens"], evidence=("ev_1", "ev_2"))]

    assert _fit(single) < 100


# ── contracts that must survive the change ───────────────────────────────────

def test_a_provider_stated_dimension_score_is_still_respected():
    """A claim whose field *is* a dimension is a stated score, not evidence of one."""
    stated = _claim("product_sector_fit", 90, confidence=.25)

    assert _fit([stated]) == 90


@pytest.mark.parametrize("value", [0, False, "", []])
def test_an_empty_or_negative_observation_scores_zero_not_a_bonus(value):
    assert _dimension(_claim("product_term", value)) == 0.0


def test_unsupported_claims_are_still_ignored_entirely():
    unsupported = Claim(
        field="product_term", value="ovens", status="unknown", confidence=.9,
        method="observed", evidence_ids=[],
    )

    assert set(derive_dimension_scores([unsupported]).values()) == {None}


def test_fit_and_confidence_remain_separate_signals():
    """Breadth lifts fit; it must not be laundered into evidence confidence."""
    narrow = score_lead({}, [_claim("product_term", ["ovens"], evidence=("ev_1", "ev_2"))], ScoringProfile())
    broad = score_lead(
        {}, [_claim("product_term", ["ovens", "hobs", "white goods"], evidence=("ev_1", "ev_2"))],
        ScoringProfile(),
    )

    assert broad.fit_score > narrow.fit_score
    assert broad.evidence_confidence == narrow.evidence_confidence


class TieredEvidenceVerifier:
    """Rich evidence for one candidate, thin for the others.

    The lead list has to be ordered by fit, and a fixture that gives every
    company identical evidence cannot show that: any order is sorted when every
    score is equal.
    """

    RICH = "buyer-de-1"

    def __init__(self, provider):
        self.provider = provider
        self.definition = provider.definition

    def discover(self, query):
        return self.provider.discover(query)

    def health(self):
        return self.provider.health()

    def verify(self, query, candidate):
        bundle = self.provider.verify(query, candidate)
        if candidate.source_record_id == self.RICH:
            return bundle
        # Thin: one independent source, one term, no domain.
        independent = bundle.sources[1]
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[independent.model_copy(update={"facts": {
                "company_name": [candidate.company_name],
                "buyer_role": ["distributor"],
                "product_term": ["household-appliances"],
            }})],
            independent_source_count=1,
        )


def test_the_customer_lead_list_is_ordered_by_fit(tmp_path):
    """Scoring only matters if the list the customer reads is sorted by it.

    `/leads` ordered by `leads.created_at DESC`, so the list arrived in the
    corpus's arbitrary insertion order while the brief page promised it was
    ranked by the customer's weights.
    """
    from server.lead_research.models import VerificationBundle as _bundle  # noqa: F401
    from server.lead_research.registry import ProviderRegistry
    from server.lead_research.service import LeadResearchService
    from tests.server.lead_research.fakes import deterministic_provider, fixture_definition
    from tests.server.lead_research.test_vertical_slice import (
        campaign_body, make_research_client, start_and_settle,
    )

    app, client, headers, _ = make_research_client()
    definition = fixture_definition()
    app.state.lead_research = LeadResearchService(
        app.state.db,
        registry=ProviderRegistry(
            [definition],
            {definition.source_id: TieredEvidenceVerifier(deterministic_provider(definition))},
        ),
    )
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()
    start_and_settle(app, client, headers, campaign["id"])

    leads = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/leads", headers=headers,
    ).json()

    assert len(leads) > 1
    scores = [lead["fit_score"] for lead in leads]
    assert scores == sorted(scores, reverse=True), scores
    assert len(set(scores)) > 1, "the fixture must produce distinguishable leads"
    assert leads[0]["company_name"] == "Atlas DE", "the best-evidenced lead comes first"


# ── completeness measures what the sources could supply ──────────────────────

ATTAINABLE = {"product_sector_fit", "buyer_channel_fit", "contactability"}


def test_a_dimension_no_source_can_reach_does_not_cost_confidence():
    """The regression: completeness divided by all seven dimensions.

    Four of the seven have no field any shipped verifier emits, so no company
    could exceed 3/7 and every lead's confidence was understated by the same
    fixed amount — enough that band A's threshold was only just clearable.
    """
    claims = _ordinary()

    fixed_denominator = score_lead({}, claims, ScoringProfile())
    attainable = score_lead({}, claims, ScoringProfile(), ATTAINABLE)

    assert attainable.evidence_confidence > fixed_denominator.evidence_confidence
    assert attainable.confidence_factors["completeness"] == 1.0
    assert fixed_denominator.confidence_factors["completeness"] < .5


def test_fit_is_untouched_by_the_completeness_denominator():
    """Coverage belongs to confidence. Fit must not move."""
    claims = _ordinary()

    assert (
        score_lead({}, claims, ScoringProfile(), ATTAINABLE).fit_score
        == score_lead({}, claims, ScoringProfile()).fit_score
    )


def test_a_missing_but_reachable_dimension_still_costs_confidence():
    """The fix must not excuse a gap the sources could actually have filled.

    A company with no website is missing `contactability`, and these sources can
    supply a domain — so it is a real gap, not an unreachable one.
    """
    without_domain = [claim for claim in _ordinary() if claim.field != "domain"]

    partial = score_lead({}, without_domain, ScoringProfile(), ATTAINABLE)

    assert partial.confidence_factors["completeness"] == round(2 / 3, 3)
    assert partial.evidence_confidence < score_lead(
        {}, _ordinary(), ScoringProfile(), ATTAINABLE
    ).evidence_confidence


def test_the_three_tiers_still_land_in_different_bands_under_the_new_denominator():
    bands = [
        score_lead({}, claims, ScoringProfile(), ATTAINABLE).priority_band
        for claims in (_thin(), _ordinary(), _strong())
    ]

    assert bands == ["C", "B", "A"]


def test_one_weak_mention_is_not_promoted_by_the_new_denominator():
    """Raising everyone's confidence must not let thin evidence reach the top."""
    single = [_claim("product_term", ["ovens"], evidence=("ev_1",))]

    assert score_lead({}, single, ScoringProfile(), ATTAINABLE).priority_band != "A"


def test_an_undeclared_fact_can_only_help():
    """A source emitting something it never declared must not be penalised.

    Dropping it from the numerator while it sat outside the denominator would
    make a lead score *worse* for carrying extra evidence.
    """
    # `locations` reaches market_coverage, which these sources never declared.
    with_extra = [*_ordinary(), _claim("locations", ["Cluj", "Iasi"], evidence=("ev_o", "ev_i"))]

    score = score_lead({}, with_extra, ScoringProfile(), ATTAINABLE)

    assert score.confidence_factors["completeness"] == 1.0


def test_no_declaration_falls_back_to_the_full_set():
    """Undeclared means "nobody said", not "nothing is reachable".

    Treating an empty declaration as the denominator would score a lead with one
    claim as fully complete.
    """
    single = [_claim("product_term", ["ovens"], evidence=("ev_1",))]

    score = score_lead({}, single, ScoringProfile(), set())

    assert score.confidence_factors["completeness"] < .2


def test_attainable_dimensions_are_derived_from_declared_fields():
    from server.lead_research.scoring import attainable_dimensions

    assert attainable_dimensions(["product_term"]) == {"product_sector_fit"}
    assert attainable_dimensions(["store_count"]) == {"commercial_scale"}
    assert attainable_dimensions([]) == set()
    # lifecycle_status is real evidence but speaks to no scoring dimension.
    assert attainable_dimensions(["lifecycle_status"]) == set()
