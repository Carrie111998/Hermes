"""Defensible pre-run estimates and actual ordered funnel metrics."""
from __future__ import annotations

from .models import CampaignConfig, CampaignEstimate, DiscoveryQuery


FUNNEL_KEYS = (
    "raw_records", "named_candidates", "resolved_organizations",
    "eligible_companies", "qualified_leads", "contactable_leads",
)
CANDIDATE_STAGE_METRICS = tuple(f"stage_{stage}" for stage in (
    "supplied", "gated", "identified", "eligible", "reused",
    "structured", "agentic", "scored", "materialized",
))
CHEAP_GATE_REASONS = (
    "shared_relevance",
    "corpus_term",
    "cheap_verification",
    "excluded_by_range",
    "cheap_verification_no_scope_signal",
)


def zero_result_explanation(
    *,
    status: str,
    metrics: dict,
    failed_source_ids: list[str] | tuple[str, ...] | set[str],
    unmapped_markets: list[str] | tuple[str, ...] | set[str],
) -> str | None:
    """Name the terminal reason a campaign produced no actionable lead.

    These values are a stable product contract, not prose inferred by the UI.
    The precedence follows the funnel: an explicit cancellation or source
    failure outranks downstream counters; otherwise the first stage that
    eliminated the remaining supply explains the empty outcome.
    """
    if int(metrics.get("qualified_leads", 0) or 0) > 0:
        return None
    if status == "cancelled":
        return "campaign_cancelled"
    if failed_source_ids and status in {"failed", "partial"}:
        return "sources_failed"
    if unmapped_markets and int(metrics.get("named_candidates", 0) or 0) == 0:
        return "product_terms_missing_local_mapping"

    supplied = int(metrics.get("candidate_supply_supplied", 0) or 0)
    excluded = int(metrics.get("candidate_supply_excluded_by_range", 0) or 0)
    passed = int(metrics.get("candidate_supply_passed_cheap_gate", 0) or 0)
    if supplied and excluded == supplied and passed == 0:
        return "candidates_excluded_by_range"
    if int(metrics.get("named_candidates", 0) or 0) == 0:
        return "sources_named_no_candidate"
    if int(metrics.get("eligible_companies", 0) or 0) == 0:
        return "candidates_failed_eligibility"
    return "researched_below_threshold"


def count_cheap_gate(counts: dict[str, int], decision) -> None:
    if decision.reason not in CHEAP_GATE_REASONS:
        raise ValueError(f"unknown cheap-gate reason: {decision.reason}")
    counts[decision.reason] = counts.get(decision.reason, 0) + 1
    counts["cheap_verification_requests"] = (
        counts.get("cheap_verification_requests", 0) + decision.requests
    )
    if decision.passed:
        counts["passed_cheap_gate"] = counts.get("passed_cheap_gate", 0) + 1


def count_candidate_stage(metrics: dict, stage: str) -> None:
    key = f"stage_{stage}"
    if key not in CANDIDATE_STAGE_METRICS:
        raise ValueError(f"unknown candidate stage metric: {stage}")
    metrics[key] = metrics.get(key, 0) + 1


def estimate_campaign(config: CampaignConfig, providers, history=None) -> CampaignEstimate:
    estimates = []
    unavailable = []
    query = DiscoveryQuery(
        campaign_id="estimate", seller_countries=config.seller_countries,
        target_countries=config.target_countries, sector_ids=config.sector_ids,
        hs_codes=config.hs_codes, product_terms=config.product_terms,
        buyer_types=config.buyer_types,
    )
    for provider in providers:
        estimate = provider.discover(query)
        if estimate.kind == "unavailable" or estimate.low is None or estimate.high is None:
            unavailable.append(provider.definition.source_id)
        else:
            estimates.append(estimate)
    partitions = len(config.target_countries) * max(1, len(config.sector_ids)) * len(providers)
    if not estimates:
        return CampaignEstimate(
            status="unavailable", basis="No source count endpoint or comparable campaign history is available",
            confidence="low", unavailable_source_ids=unavailable, expected_partitions=partitions,
        )
    low, high = sum(item.low or 0 for item in estimates), sum(item.high or 0 for item in estimates)
    eligible = [round(low * .45), round(high * .65)]
    qualified = [round(eligible[0] * .35), round(eligible[1] * .55)]
    return CampaignEstimate(
        status="available", basis="Current source-reported counts and deterministic provider coverage",
        confidence="medium" if unavailable else "high", named_candidate_range=[low, high],
        eligible_range=eligible, qualified_range=qualified, unavailable_source_ids=unavailable,
        expected_partitions=partitions,
    )


class CampaignMetricsRecorder:
    def __init__(self, db, company_id: str, campaign_id: str):
        self.db, self.company_id, self.campaign_id = db, company_id, campaign_id

    def save(self, metrics: dict, stamp: float, dimension: str = "overall", value: str = "all") -> None:
        from ..db import json_dump
        existing = self.db.one(
            "SELECT campaign_id FROM campaign_metrics WHERE company_id=? AND campaign_id=? "
            "AND dimension=? AND dimension_value=?",
            (self.company_id, self.campaign_id, dimension, value),
        )
        if existing:
            self.db.execute(
                "UPDATE campaign_metrics SET metrics=?,updated_at=? WHERE company_id=? AND campaign_id=? "
                "AND dimension=? AND dimension_value=?",
                (json_dump(metrics), stamp, self.company_id, self.campaign_id, dimension, value),
            )
        else:
            self.db.execute(
                "INSERT INTO campaign_metrics VALUES(?,?,?,?,?,?)",
                (self.company_id, self.campaign_id, dimension, value, json_dump(metrics), stamp),
            )
