"""Defensible pre-run estimates and actual ordered funnel metrics."""
from __future__ import annotations

from .models import CampaignConfig, CampaignEstimate, DiscoveryQuery


FUNNEL_KEYS = (
    "raw_records", "named_candidates", "resolved_organizations",
    "eligible_companies", "qualified_leads", "contactable_leads",
)


def estimate_campaign(config: CampaignConfig, providers, history=None) -> CampaignEstimate:
    estimates = []
    unavailable = []
    query = DiscoveryQuery(
        campaign_id="estimate", seller_countries=config.seller_countries,
        target_countries=config.target_countries, sector_ids=config.sector_ids,
        hs_codes=config.hs_codes, buyer_types=config.buyer_types,
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
