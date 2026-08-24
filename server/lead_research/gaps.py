"""Criterion-aware, page-batched planning for one resolved company."""
from __future__ import annotations

from collections import defaultdict
from time import time

from .enrichment import FeaturePlanner, PLAYBOOK_SATISFIED_BY
from .models import (
    CampaignConfig,
    CompanyProfileVersion,
    LeadCandidate,
    ResearchBatch,
    ResearchGap,
    ResearchGapPlan,
    ResearchQuery,
    ScoringWeights,
    SourceCapability,
    StoredFact,
)
from .scoring import DIMENSION_CLAIM_FIELDS
from .search_cache import query_scope, research_query_hash


def weighted_dimensions(weights: ScoringWeights) -> list[tuple[str, int]]:
    return [
        (name, int(value))
        for name, value in weights.model_dump().items()
        if int(value) > 0
    ]


def _accepted_fields(dimension: str, configured: dict) -> set[str]:
    return {
        dimension,
        *DIMENSION_CLAIM_FIELDS.get(dimension, ()),
        *configured.get("required", []),
        *configured.get("useful", []),
    }


def gap_query(
    profile_version: CompanyProfileVersion,
    campaign: CampaignConfig,
    candidate: LeadCandidate,
    field: str,
    source_id: str,
    *,
    licensed: bool = False,
) -> ResearchQuery:
    organization_key = candidate.domain or candidate.organization_id or ""
    query_class = "|".join([
        organization_key.casefold(),
        field,
        source_id,
        ",".join(sorted(campaign.target_countries)),
        ",".join(sorted(campaign.sector_ids)),
        ",".join(sorted(campaign.hs_codes)),
    ])
    return ResearchQuery(
        company_id=profile_version.company_id,
        organization_id=organization_key,
        field=field,
        normalized_query_class=query_class,
        customer_terms=campaign.product_terms,
        hidden_label_ids=profile_version.profile.hidden_label_ids,
        licensed_source_ids=[source_id] if licensed else [],
    )


class GapPlanner:
    def __init__(
        self,
        feature_planner: FeaturePlanner | None = None,
        *,
        search_attempts=None,
        at: float | None = None,
    ):
        self.feature_planner = feature_planner or FeaturePlanner()
        self.search_attempts = search_attempts
        self.at = at

    def _negative_cached(
        self,
        profile_version: CompanyProfileVersion | None,
        campaign: CampaignConfig,
        candidate: LeadCandidate,
        field: str,
        source_id: str,
        *,
        licensed: bool = False,
    ) -> bool:
        if self.search_attempts is None or profile_version is None:
            return False
        query = gap_query(
            profile_version, campaign, candidate, field, source_id, licensed=licensed,
        )
        return self.search_attempts.lookup(
            query_scope(query),
            research_query_hash(query, source_id),
            time() if self.at is None else self.at,
        ) is not None

    def _required_dimensions(self, campaign: CampaignConfig) -> set[str]:
        playbook_required: set[str] = set()
        for sector_id in campaign.sector_ids:
            playbook_required.update(
                (self.feature_planner.playbooks.get(sector_id) or {}).get("required", [])
            )
        required_fact_fields: set[str] = set()
        for playbook_field in playbook_required:
            required_fact_fields.update(
                PLAYBOOK_SATISFIED_BY.get(playbook_field, (playbook_field,))
            )
        return {
            dimension
            for dimension, configured in self.feature_planner.research_dimensions(
                campaign.sector_ids
            ).items()
            if _accepted_fields(dimension, configured).intersection(required_fact_fields)
        }

    def plan(
        self,
        profile_version: CompanyProfileVersion | None,
        campaign: CampaignConfig,
        candidate: LeadCandidate,
        reusable_facts: list[StoredFact],
        capabilities: list[SourceCapability],
        *,
        observed_fields: set[str] | None = None,
    ) -> ResearchGapPlan:
        configs = self.feature_planner.research_dimensions(campaign.sector_ids)
        required_dimensions = self._required_dimensions(campaign)
        fresh = [
            fact for fact in reusable_facts
            if fact.organization_id == candidate.organization_id
            and fact.status == "observed"
            and fact.expires_at > time()
        ]
        executable = [capability for capability in capabilities if capability.executable]
        gaps: list[ResearchGap] = []
        structured_batches: dict[str, set[str]] = defaultdict(set)
        structured_sources: dict[str, set[str]] = defaultdict(set)
        agentic_batches: dict[str, set[str]] = defaultdict(set)

        for dimension, weight in weighted_dimensions(campaign.scoring.weights):
            configured = configs.get(dimension, {})
            target_fields = list(dict.fromkeys([
                *configured.get("required", []), *configured.get("useful", []),
            ]))
            accepted = _accepted_fields(dimension, configured)
            reused = [fact for fact in fresh if fact.field in accepted]
            already_observed = accepted.intersection(observed_fields or set())
            if reused or already_observed:
                gaps.append(ResearchGap(
                    dimension=dimension,
                    weight=weight,
                    fields=[],
                    route="reuse",
                    required=dimension in required_dimensions,
                    reused_fact_ids=[fact.id for fact in reused],
                ))
                continue

            providers_by_field: dict[str, list[str]] = defaultdict(list)
            for capability in executable:
                for emitted in capability.emitted_fields.intersection(accepted):
                    if self._negative_cached(
                        profile_version,
                        campaign,
                        candidate,
                        emitted,
                        capability.source_id,
                        licensed=capability.access_class == "licensed",
                    ):
                        continue
                    providers_by_field[emitted].append(capability.source_id)
            structured_fields = sorted(providers_by_field)
            # Anything a structured source cannot establish still has an
            # agentic route. Direct dimension output can satisfy the score but
            # does not pretend the wider page facts were found.
            candidate_agentic_fields = [
                field for field in target_fields if field not in providers_by_field
            ]
            suppressed_fields = [
                field for field in candidate_agentic_fields
                if self._negative_cached(
                    profile_version, campaign, candidate, field, "agentic",
                )
            ]
            agentic_fields = [
                field for field in candidate_agentic_fields if field not in suppressed_fields
            ]
            route = "structured" if structured_fields else "agentic"
            gaps.append(ResearchGap(
                dimension=dimension,
                weight=weight,
                fields=[field for field in target_fields if field not in suppressed_fields],
                route=route,
                required=dimension in required_dimensions,
                structured_fields=structured_fields,
                agentic_fields=agentic_fields,
                suppressed_fields=suppressed_fields,
            ))
            for field, source_ids in providers_by_field.items():
                source_id = sorted(source_ids)[0]
                structured_batches[source_id].add(field)
                structured_sources[source_id].add(source_id)
            if agentic_fields:
                hint = configured.get("source_hint") or (
                    "official_site" if candidate.domain else "public_web"
                )
                agentic_batches[hint].update(agentic_fields)

        batches = [
            ResearchBatch(
                source_hint=source_id,
                fields=sorted(fields),
                route="structured",
                source_ids=sorted(structured_sources[source_id]),
            )
            for source_id, fields in sorted(structured_batches.items())
            if fields
        ]
        batches.extend(
            ResearchBatch(
                source_hint=hint,
                fields=sorted(fields),
                route="agentic",
            )
            for hint, fields in sorted(agentic_batches.items())
            if fields
        )
        return ResearchGapPlan(
            organization_id=candidate.organization_id or "",
            gaps=gaps,
            batches=batches,
        )
