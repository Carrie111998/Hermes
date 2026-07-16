"""Tenant-scoped, evidence-first lead research application services."""

from .models import (
    CampaignConfig,
    Claim,
    DatasetDefinition,
    DiscoveryEstimate,
    DiscoveryQuery,
    EvidenceEnvelope,
    LeadScore,
    ScoringProfile,
)
__all__ = [
    "CampaignConfig",
    "Claim",
    "DatasetDefinition",
    "DiscoveryEstimate",
    "DiscoveryQuery",
    "EvidenceEnvelope",
    "LeadResearchService",
    "LeadScore",
    "ProviderRegistry",
    "ScoringProfile",
    "build_registry",
]


def __getattr__(name):
    # Keep server.db -> lead_research.schema imports cycle-free while retaining
    # convenient public imports for application consumers.
    if name == "LeadResearchService":
        from .service import LeadResearchService
        return LeadResearchService
    if name in {"ProviderRegistry", "build_registry"}:
        from .registry import ProviderRegistry, build_registry
        return {"ProviderRegistry": ProviderRegistry, "build_registry": build_registry}[name]
    raise AttributeError(name)
