"""Which strong fits are shown, and in what order. Pure, so it is testable.

A campaign used to materialize a lead the moment a candidate cleared its
verdict, with a per-country ceiling of 50. "Leads" was therefore whatever the
corpus happened to contain — 91 in one run, 173 in another — and because the
countries were processed in sequence the first market consumed the budget while
four others returned nothing.

This module decides the list instead: a global cap of 15, filled by a
deterministic round-robin over the requested countries so every market is
represented before any market takes a second slot.

It selects; it never scores. Nothing here can promote a review candidate,
lower a score, or reach the target when there are not enough strong fits — a
short honest list is the product, and a padded one is a lie.
"""
from __future__ import annotations

from dataclasses import dataclass


# The list a customer is shown. Five is what "enough to act on" means, fifteen
# is what one person can actually work through; between them the run reports
# what it found rather than filling a quota.
RESULT_TARGET_MIN = 5
RESULT_LIMIT = 15


@dataclass(frozen=True)
class RankableResult:
    """One already-scored strong fit, with everything ordering needs and nothing else.

    Deliberately carries no source id, dataset id, provider or access tier: if
    ordering could read one, reordering `enabled_source_ids` would change the
    customer's list.
    """

    result_id: str
    organization_id: str
    company_name: str
    country: str
    fit_score: int
    evidence_confidence: float
    known_weight: int
    freshness: float


@dataclass(frozen=True)
class DisplayDecision:
    displayed: bool
    display_rank: int | None
    country_round: int | None
    reason: str


def rank_key(item: RankableResult):
    """Ordering within one country: best evidence first, then stable identity.

    The last two components exist so a tie is broken the same way on every run
    and on every machine. Without them Python's stable sort would preserve the
    order candidates happened to arrive in, which is source order.
    """
    return (
        -item.fit_score,
        -item.evidence_confidence,
        -item.known_weight,
        -item.freshness,
        item.company_name.casefold(),
        item.organization_id,
    )


def select_displayed_strong_fits(
    candidates: list[RankableResult],
    country_order: list[str],
    limit: int = RESULT_LIMIT,
) -> dict[str, DisplayDecision]:
    """Display decisions for every candidate, keyed by result id.

    `country_order` is the campaign's requested markets, in the order the
    customer listed them: round one takes the best candidate from each in turn,
    round two takes the second, and so on until the limit or exhaustion. A
    country that runs dry is skipped rather than stalling the round, so a market
    with one buyer costs the list nothing.

    A country the campaign did not request can still appear — evidence places a
    company where the evidence says it is — and goes after every requested
    market, in ISO-code order, so an unasked-for market can never outrank an
    asked-for one.
    """
    by_country: dict[str, list[RankableResult]] = {}
    for item in candidates:
        by_country.setdefault(item.country, []).append(item)
    for queue in by_country.values():
        queue.sort(key=rank_key)

    requested = list(dict.fromkeys(country_order))
    order = [country for country in requested if country in by_country]
    order.extend(sorted(set(by_country) - set(requested)))

    decisions: dict[str, DisplayDecision] = {
        item.result_id: DisplayDecision(False, None, None, "outside_result_limit")
        for item in candidates
    }
    taken = {country: 0 for country in order}
    rank = 0
    while rank < limit:
        progressed = False
        for country in order:
            if rank >= limit:
                break
            index = taken[country]
            queue = by_country[country]
            if index >= len(queue):
                continue
            rank += 1
            taken[country] = index + 1
            progressed = True
            decisions[queue[index].result_id] = DisplayDecision(
                displayed=True,
                display_rank=rank,
                country_round=index + 1,
                reason="displayed",
            )
        if not progressed:
            break
    return decisions
