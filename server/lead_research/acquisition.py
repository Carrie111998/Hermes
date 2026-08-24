"""Bounded provider partition runner."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import DiscoveryQuery


CANDIDATE_STAGES = (
    "supplied", "gated", "identified", "eligible", "reused",
    "structured", "agentic", "scored", "materialized",
)


def stage_index(stage: str) -> int:
    """Stable monotonic ordering for persisted candidate checkpoints."""
    try:
        return CANDIDATE_STAGES.index(stage)
    except ValueError as exc:
        raise ValueError(f"unknown research candidate stage: {stage}") from exc


@dataclass(frozen=True)
class CheapVerification:
    matched: bool
    evidence_ids: list[str] = field(default_factory=list)
    requests: int = 0


class CandidateMetadataCheapVerifier:
    """Read a bounded discovery/lookup result already attached to a candidate.

    Provider candidate discovery is itself the cheap lookup. Its adapter puts
    the match and evidence identity on the candidate, so the gate can meter and
    consume that result without performing full company research early.
    """

    def verify(self, candidate, terms: list[str]) -> CheapVerification:
        del terms
        return CheapVerification(
            matched=bool(candidate.data.get("cheap_verification")),
            evidence_ids=list(candidate.data.get("cheap_verification_evidence_ids", [])),
            requests=int(candidate.data.get("cheap_verification_requests", 0)),
        )


class CampaignRunner:
    def __init__(self, registry, repository):
        self.registry, self.repository = registry, repository

    def run_partition(self, source_id: str, query: DiscoveryQuery):
        provider = self.registry.get(source_id)
        page = provider.fetch_page(query, cursor=None)
        snapshot = self.repository.save_snapshot(page, query.campaign_id)
        evidence = [item for record in page.records for item in provider.normalize(record, page.snapshot)]
        return {"page": page, "snapshot": snapshot, "evidence": evidence, "checkpoint": provider.checkpoint(page)}
