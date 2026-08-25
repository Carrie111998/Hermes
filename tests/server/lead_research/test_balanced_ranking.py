"""The primary list is a global top-15, balanced across the requested markets.

Two failures this replaces. A per-country ceiling of 50 qualified leads meant
"leads" was whatever the corpus happened to contain — 91 in one run, 173 in
another — which is a list nobody reads. And the countries were processed in
order, so the first market consumed the budget and a five-market campaign
returned four markets' worth of nothing.

Selection is pure: it takes already-scored candidates and returns display
decisions. It cannot promote a review candidate, cannot lower a score, and
cannot reach a target it has no strong fits for.
"""
from __future__ import annotations

from collections import Counter

from server.lead_research.ranking import (
    RESULT_LIMIT,
    RESULT_TARGET_MIN,
    RankableResult,
    select_displayed_strong_fits,
)


def rankable(
    result_id: str,
    country: str,
    *,
    fit=90,
    confidence=.8,
    known_weight=60,
    freshness=.9,
    name=None,
) -> RankableResult:
    return RankableResult(
        result_id=result_id,
        organization_id=f"org_{result_id}",
        company_name=name or result_id,
        country=country,
        fit_score=fit,
        evidence_confidence=confidence,
        known_weight=known_weight,
        freshness=freshness,
    )


def test_the_target_and_limit_are_the_published_contract():
    assert (RESULT_TARGET_MIN, RESULT_LIMIT) == (5, 15)


def test_fifteen_countries_are_represented_before_any_second_pick():
    countries = [f"C{index:02d}" for index in range(15)]
    candidates = [
        rankable(f"{country}-1", country, fit=90) for country in countries
    ] + [rankable("C00-2", "C00", fit=100)]

    decisions = select_displayed_strong_fits(candidates, countries)

    displayed = [item for item in candidates if decisions[item.result_id].displayed]
    assert len(displayed) == 15
    assert Counter(item.country for item in displayed) == {country: 1 for country in countries}
    # C00 holds the two strongest candidates in the campaign and still gets one
    # slot; the weaker of its two is the one that loses it.
    assert decisions["C00-2"].displayed is True
    assert decisions["C00-1"].displayed is False
    assert decisions["C00-1"].reason == "outside_result_limit"


def test_five_countries_receive_three_balanced_rounds():
    countries = ["DE", "ES", "PL", "RO", "FR"]
    candidates = [
        rankable(f"{country}-{rank}", country, fit=100 - rank)
        for country in countries for rank in range(1, 5)
    ]

    decisions = select_displayed_strong_fits(candidates, countries)

    selected = [item for item in candidates if decisions[item.result_id].displayed]
    assert len(selected) == 15
    assert {
        country: sum(item.country == country for item in selected)
        for country in countries
    } == {country: 3 for country in countries}


def test_display_rank_follows_the_round_robin_in_requested_country_order():
    countries = ["DE", "ES"]
    candidates = [
        rankable(f"{country}-{rank}", country, fit=100 - rank)
        for country in countries for rank in range(1, 3)
    ]

    decisions = select_displayed_strong_fits(candidates, countries)

    assert [decisions[item].display_rank for item in ["DE-1", "ES-1", "DE-2", "ES-2"]] == [1, 2, 3, 4]
    assert [decisions[item].country_round for item in ["DE-1", "ES-1", "DE-2", "ES-2"]] == [1, 1, 2, 2]


def test_rank_is_source_independent_and_uses_stable_identity_for_ties():
    left = rankable("r_b", "DE", fit=90, confidence=.8, name="Beta")
    right = rankable("r_a", "DE", fit=90, confidence=.8, name="Alpha")

    forward = select_displayed_strong_fits([left, right], ["DE"])
    reverse = select_displayed_strong_fits([right, left], ["DE"])

    assert forward["r_a"].display_rank == reverse["r_a"].display_rank == 1
    assert forward["r_b"].display_rank == reverse["r_b"].display_rank == 2


def test_four_floor_clearing_candidates_are_not_padded():
    candidates = [rankable(f"r_{index}", "DE", fit=90) for index in range(4)]

    decisions = select_displayed_strong_fits(candidates, ["DE"])

    assert sum(decision.displayed for decision in decisions.values()) == 4


def test_a_country_nobody_asked_for_still_gets_a_deterministic_slot():
    """Evidence can place a company in a market the campaign did not list.

    Dropping it would silently lose a qualified lead; putting it first would let
    an unrequested market outrank a requested one. It goes last, in ISO order.
    """
    candidates = [
        rankable("req-1", "DE", fit=90),
        rankable("extra-nl", "NL", fit=100),
        rankable("extra-at", "AT", fit=100),
    ]

    decisions = select_displayed_strong_fits(candidates, ["DE"])

    assert [decisions[item].display_rank for item in ["req-1", "extra-at", "extra-nl"]] == [1, 2, 3]


def test_a_country_runs_dry_without_stalling_the_rounds():
    candidates = [
        rankable("de-1", "DE"), rankable("de-2", "DE"), rankable("de-3", "DE"),
        rankable("es-1", "ES"),
    ]

    decisions = select_displayed_strong_fits(candidates, ["DE", "ES"])

    assert all(decision.displayed for decision in decisions.values())
    assert decisions["es-1"].display_rank == 2
    assert [decisions[item].country_round for item in ["de-1", "es-1", "de-2", "de-3"]] == [1, 1, 2, 3]


def test_within_a_country_the_ordering_is_the_declared_key():
    candidates = [
        rankable("weakest", "DE", fit=80),
        rankable("strongest", "DE", fit=95),
        rankable("middle", "DE", fit=90),
    ]

    decisions = select_displayed_strong_fits(candidates, ["DE"])

    assert [decisions[item].display_rank for item in ["strongest", "middle", "weakest"]] == [1, 2, 3]


def test_no_country_takes_a_fourth_while_another_has_fewer_than_three():
    """The acceptance rule, stated directly.

    DE has enough candidates to fill the whole list on its own; ES and FR each
    have two. Balance means DE stops at three until they are exhausted.
    """
    candidates = (
        [rankable(f"de-{index}", "DE", fit=99) for index in range(10)]
        + [rankable(f"es-{index}", "ES", fit=81) for index in range(2)]
        + [rankable(f"fr-{index}", "FR", fit=81) for index in range(2)]
    )

    decisions = select_displayed_strong_fits(candidates, ["DE", "ES", "FR"])
    selected = Counter(
        item.country for item in candidates if decisions[item.result_id].displayed
    )

    assert sum(selected.values()) == 14
    assert selected == {"DE": 10, "ES": 2, "FR": 2}
    de_rounds = sorted(
        decisions[item.result_id].country_round
        for item in candidates
        if item.country == "DE" and decisions[item.result_id].displayed
    )
    assert de_rounds == list(range(1, 11))


def test_selection_is_pure_and_names_every_candidate():
    candidates = [rankable(f"r_{index}", "DE") for index in range(20)]

    decisions = select_displayed_strong_fits(candidates, ["DE"])

    assert set(decisions) == {item.result_id for item in candidates}
    assert sum(decision.displayed for decision in decisions.values()) == RESULT_LIMIT
    hidden = [decision for decision in decisions.values() if not decision.displayed]
    assert all(
        decision.display_rank is None and decision.country_round is None
        and decision.reason == "outside_result_limit"
        for decision in hidden
    )


def test_a_lower_limit_is_honoured_without_breaking_balance():
    candidates = [
        rankable(f"{country}-{rank}", country)
        for country in ("DE", "ES") for rank in range(1, 4)
    ]

    decisions = select_displayed_strong_fits(candidates, ["DE", "ES"], limit=4)

    selected = [item for item in candidates if decisions[item.result_id].displayed]
    assert len(selected) == 4
    assert Counter(item.country for item in selected) == {"DE": 2, "ES": 2}
