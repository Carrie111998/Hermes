"""Bounded provider partition runner."""
from __future__ import annotations

from .models import DiscoveryQuery


class CampaignRunner:
    def __init__(self, registry, repository):
        self.registry, self.repository = registry, repository

    def run_partition(self, source_id: str, query: DiscoveryQuery):
        provider = self.registry.get(source_id)
        page = provider.fetch_page(query, cursor=None)
        snapshot = self.repository.save_snapshot(page, query.campaign_id)
        evidence = [item for record in page.records for item in provider.normalize(record, page.snapshot)]
        return {"page": page, "snapshot": snapshot, "evidence": evidence, "checkpoint": provider.checkpoint(page)}
