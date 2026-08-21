"""Strict contracts shared by providers, storage, scoring, and the API."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ClaimStatus = Literal[
    "observed", "calculated", "estimated_range", "conflicted", "unknown", "not_applicable"
]
RecordType = Literal[
    "organization", "market_signal", "company_signal", "event", "opportunity", "lead_candidate"
]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetDefinition(ApiModel):
    source_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = "1"
    display_name: str
    publisher: str
    jurisdiction: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    homepage: str | None = None
    access_tier: Literal["public", "credentialed_public", "licensed", "customer_upload", "retired"]
    entity_levels: list[Literal["market", "named_company", "opportunity", "event"]]
    capabilities: list[str] = Field(default_factory=list)
    # The claim fields this source's verifier can actually produce. Coarse
    # capabilities cannot answer that: "candidate_verification" says a source
    # verifies companies, not that it can ever speak to their store count. A
    # scoring dimension no configured source can reach must not be counted
    # against a lead's completeness, and this is what makes that knowable.
    # Empty means undeclared, which is treated as "no information", not "none".
    emits: list[str] = Field(default_factory=list)
    # How many candidates may be verified against this source at once. Declared
    # per source because it is a property of the upstream, not of our appetite:
    # TED rate-limits hard enough that its adapter already carries a 429
    # backoff, so verifying it concurrently would turn a working source into a
    # failing one. A web unlocker is sold for concurrent use.
    max_concurrency: int = Field(default=4, ge=1)
    countries: list[str] = Field(default_factory=list)
    sector_ids: list[str] = Field(default_factory=list)
    freshness_days: int = Field(default=180, ge=1)
    adapter_mode: Literal["live", "fixture", "manual_import", "credential_required", "catalog_only"] = "catalog_only"
    default_enabled: bool = False
    health: Literal["active", "degraded", "retired"] = "active"
    last_verified_at: str | None = None
    license_note: str | None = None

    @model_validator(mode="after")
    def retired_is_not_enabled(self):
        if (self.health == "retired" or self.access_tier == "retired") and self.default_enabled:
            raise ValueError("retired sources cannot be enabled")
        return self


class DiscoveryQuery(ApiModel):
    campaign_id: str
    seller_countries: list[str]
    target_countries: list[str]
    sector_ids: list[str] = Field(default_factory=list)
    hs_codes: list[str] = Field(default_factory=list)
    buyer_types: list[str] = Field(default_factory=list)
    max_records: int = Field(default=100, ge=1, le=10_000)


class DiscoveryEstimate(ApiModel):
    kind: Literal["reported", "historical_range", "unavailable"]
    low: int | None = Field(default=None, ge=0)
    high: int | None = Field(default=None, ge=0)
    basis: str
    confidence: Literal["low", "medium", "high"] = "low"

    @model_validator(mode="after")
    def valid_range(self):
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("estimate low cannot exceed high")
        return self


class SnapshotRef(ApiModel):
    snapshot_id: str
    source_id: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RawRecord(ApiModel):
    source_record_id: str
    payload: dict[str, Any]


class RawPage(ApiModel):
    snapshot: SnapshotRef
    records: list[RawRecord]
    next_cursor: str | None = None
    source_reported_total: int | None = None


class ProviderHealth(ApiModel):
    status: Literal["active", "degraded", "retired", "unavailable"]
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    message: str | None = None
    reason: Literal["credential_required", "disabled"] | None = None


class VerificationSource(ApiModel):
    provenance_url: str
    raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification: Literal["official", "independent"]
    retrieved_via: str
    facts: dict[str, list[str]] = Field(default_factory=dict)
    # When this page was actually fetched. None for a bundle a provider just
    # returned, which is by definition now; a bundle rebuilt from the cache
    # carries the age of the evidence, which is the whole reason freshness can
    # be measured at all.
    retrieved_at: float | None = None

    @field_validator("provenance_url", "retrieved_via")
    @classmethod
    def require_https_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("verification sources must use https URLs")
        return value


class VerificationBundle(ApiModel):
    candidate_source_record_id: str
    sources: list[VerificationSource] = Field(default_factory=list)
    independent_source_count: int = Field(default=0, ge=0)
    # Outbound requests this bundle cost. Reported by the provider because only
    # it knows: one `verify` can be zero fetches for a local corpus or four for
    # a web verifier, and a caller cannot count what happens inside. A bundle
    # rebuilt from stored evidence therefore reports 0, which is exactly true.
    #
    # A `verify` that raises after spending is not counted — the bundle never
    # returns — so a run's total is a floor. Failures are recorded per
    # partition, so an unusually cheap run with errors is legible as one.
    requests: int = Field(default=0, ge=0)


class EvidenceEnvelope(ApiModel):
    evidence_id: str
    source_id: str
    source_record_id: str
    snapshot_id: str
    record_type: RecordType
    observed_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    jurisdiction: str | None = None
    sector_ids: list[str] = Field(default_factory=list)
    provenance_url: str | None = None
    raw_hash: str
    method: Literal["observed", "calculated", "estimated_range"] = "observed"
    confidence: float = Field(ge=0, le=1)
    payload: dict[str, Any]


class Organization(ApiModel):
    organization_id: str | None = None
    display_name: str
    legal_name: str | None = None
    country: str | None = None
    domain: str | None = None
    registry_id: str | None = None
    buyer_types: list[str] = Field(default_factory=list)


class MarketSignal(ApiModel):
    metric: str
    value: float
    currency: str | None = None
    unit: str | None = None
    period: str


class CompanySignal(ApiModel):
    organization_id: str
    signal: str
    value: Any


class Event(ApiModel):
    name: str
    organizer: str
    starts_at: datetime | None = None


class Opportunity(ApiModel):
    title: str
    organization_name: str | None = None
    intent: Literal["sourcing", "procurement", "partnership", "unknown"] = "unknown"


class Claim(ApiModel):
    field: str
    value: str | int | float | bool | list[str] | None = None
    low: float | None = None
    high: float | None = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    status: ClaimStatus
    confidence: float = Field(ge=0, le=1)
    method: Literal["observed", "calculated", "estimated_range"]
    evidence_ids: list[str] = Field(default_factory=list)
    applicability: Literal["required", "useful", "not_applicable"] = "useful"
    # Whether a publisher with standing vouched for this fact — the company's
    # own page, or an authoritative registry. An independent web page and an
    # agentic web-search result are real evidence and score, but they are not
    # validated, and only validated facts are safe to share between customers.
    validated: bool = False
    # When the newest evidence behind this claim was retrieved. Confidence reads
    # it to age the claim against its field's own shelf life. None means the
    # claim predates age tracking, which reads as "unknown", never as "fresh".
    observed_at: float | None = None

    @model_validator(mode="after")
    def validate_claim(self):
        if self.status == "estimated_range":
            if self.low is None or self.high is None or self.low > self.high:
                raise ValueError("estimated ranges require ordered low and high values")
        numeric_fields = {
            "store_count", "revenue", "market_cap", "reported_company_valuation",
            "estimated_company_value_range", "relevant_import_value", "employee_count",
        }
        if self.field in numeric_fields and self.status not in {"unknown", "not_applicable"} and not self.period:
            raise ValueError("time-varying numeric claims require a period")
        if self.status not in {"unknown", "not_applicable"} and not self.evidence_ids:
            raise ValueError("supported claims require evidence")
        return self


class LeadCandidate(ApiModel):
    organization_id: str | None
    qualifying_evidence: list[Any]

    @model_validator(mode="after")
    def named_company_required(self):
        if not self.organization_id:
            raise ValueError("a named organization is required")
        if any(isinstance(item, MarketSignal) for item in self.qualifying_evidence):
            raise ValueError("aggregate market signals cannot qualify a named lead")
        return self


class ScoringWeights(ApiModel):
    product_sector_fit: int = Field(default=25, ge=0, le=100)
    buyer_channel_fit: int = Field(default=20, ge=0, le=100)
    buying_intent: int = Field(default=15, ge=0, le=100)
    market_coverage: int = Field(default=15, ge=0, le=100)
    commercial_scale: int = Field(default=10, ge=0, le=100)
    trade_activity: int = Field(default=10, ge=0, le=100)
    contactability: int = Field(default=5, ge=0, le=100)

    @field_validator(
        "product_sector_fit", "buyer_channel_fit", "buying_intent", "market_coverage",
        "commercial_scale", "trade_activity", "contactability",
    )
    @classmethod
    def values_are_five_point_steps(cls, value: int) -> int:
        if value % 5:
            raise ValueError("scoring weights must be multiples of five")
        return value

    @model_validator(mode="after")
    def totals_one_hundred(self):
        if sum(self.model_dump().values()) != 100:
            raise ValueError("scoring weights must total 100")
        return self


class PriorityBand(ApiModel):
    min_fit: int = Field(ge=0, le=100)
    min_confidence: float = Field(ge=0, le=1)


class ScoringProfile(ApiModel):
    profile_id: str = "default-high-precision"
    name: str = "High precision"
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    bands: dict[str, PriorityBand] = Field(default_factory=lambda: {
        "A": PriorityBand(min_fit=80, min_confidence=.72),
        "B": PriorityBand(min_fit=60, min_confidence=.45),
        "C": PriorityBand(min_fit=35, min_confidence=.2),
    })

    @model_validator(mode="after")
    def monotonic_bands(self):
        values = [self.bands[key] for key in ("A", "B", "C") if key in self.bands]
        if len(values) != 3 or not (values[0].min_fit > values[1].min_fit > values[2].min_fit):
            raise ValueError("priority bands must be monotonic")
        return self


class EnrichmentProfile(ApiModel):
    profile_id: str = "local-balanced"
    # Two different mechanisms, deliberately not one flag. `enabled` is the
    # local-model fallback and needs a model profile. `research_each_lead` is
    # a second, gap-targeted pass over the sources already configured: it costs
    # requests, not tokens, and needs no model at all.
    enabled: bool = False
    research_each_lead: bool = False
    model_profile: str | None = None
    trigger: Literal["missing_required", "below_completeness", "manual"] = "missing_required"
    completeness_target: int = Field(default=80, ge=0, le=100)
    max_companies: int = Field(default=25, ge=1, le=500)
    max_pages_per_company: int = Field(default=8, ge=1, le=50)
    max_seconds_per_company: int = Field(default=120, ge=10, le=1800)
    max_tokens: int = Field(default=6000, ge=100, le=100_000)
    source_policy: Literal["official_only", "official_and_credible", "custom"] = "official_and_credible"

    @model_validator(mode="after")
    def model_required_when_enabled(self):
        if self.enabled and not self.model_profile:
            raise ValueError("an available model profile is required when local-AI fallback is enabled")
        return self


class CampaignConfig(ApiModel):
    name: str = Field(min_length=3, max_length=120)
    seller_countries: list[str] = Field(default_factory=lambda: ["TR"], min_length=1)
    target_countries: list[str] = Field(min_length=1, max_length=25)
    sector_ids: list[str] = Field(default_factory=list)
    hs_codes: list[str] = Field(default_factory=list)
    product_ids: list[str] = Field(default_factory=list)
    buyer_types: list[str] = Field(default_factory=lambda: ["importer", "distributor"])
    enabled_source_ids: list[str] = Field(min_length=1)
    precision_profile: Literal["high_precision", "balanced", "exploratory"] = "high_precision"
    max_qualified_leads_per_country: int = Field(default=50, ge=1, le=200)
    freshness_days: int = Field(default=180, ge=1, le=3650)
    exclusions: dict[str, Any] = Field(default_factory=lambda: {
        "company_ids": [], "domains": [], "seller_only": True, "sanctioned_entities": True,
    })
    eligibility: dict[str, Any] = Field(default_factory=lambda: {
        "require_resolved_identity": True, "require_official_domain": False,
        "require_target_presence": True, "require_buyer_role": True,
        "exclude_inactive": True, "minimum_independent_sources": 1,
    })
    scoring: ScoringProfile = Field(default_factory=ScoringProfile)
    enrichment: EnrichmentProfile = Field(default_factory=EnrichmentProfile)
    features: list[str] = Field(default_factory=lambda: [
        "identity_scale", "market_coverage", "trade_activity", "buying_intent", "product_fit",
    ])
    refresh: dict[str, Any] = Field(default_factory=lambda: {"schedule": "monthly", "reuse_public_cache": True})
    retention: dict[str, Any] = Field(default_factory=lambda: {
        "raw_snapshot_days": 365, "web_snapshot_days": 180, "export_days": 90,
    })
    source_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("seller_countries", "target_countries")
    @classmethod
    def normalize_countries(cls, value: list[str]):
        normalized = list(dict.fromkeys(str(item).upper() for item in value))
        if any(len(item) != 2 or not item.isalpha() for item in normalized):
            raise ValueError("countries must use ISO alpha-2 codes")
        return normalized

    @model_validator(mode="after")
    def scope_is_specific(self):
        if not (self.sector_ids or self.hs_codes or self.product_ids):
            raise ValueError("at least one sector, HS code, or product is required")
        return self


class LeadScore(ApiModel):
    fit_score: int = Field(ge=0, le=100)
    evidence_confidence: float = Field(ge=0, le=1)
    priority_band: Literal["A", "B", "C", "Rejected"]
    dimensions: dict[str, float | None]
    confidence_factors: dict[str, float]


class ResearchResultData(ApiModel):
    reasons: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    conflicting_claims: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    official_domains: list[str] = Field(default_factory=list)
    independent_domains: list[str] = Field(default_factory=list)
    score_dimensions: dict[str, float | None] = Field(default_factory=dict)
    confidence_factors: dict[str, float] = Field(default_factory=dict)


class CampaignEstimate(ApiModel):
    status: Literal["available", "unavailable"]
    basis: str
    confidence: Literal["low", "medium", "high"]
    named_candidate_range: list[int] | None = None
    eligible_range: list[int] | None = None
    qualified_range: list[int] | None = None
    unavailable_source_ids: list[str] = Field(default_factory=list)
    expected_partitions: int = 0
    # What the corpus can actually supply for these terms and markets. The
    # provider-reported ranges above describe how much a source knows in
    # general; they never consulted the candidate corpus, so an estimate could
    # promise leads for terms that select nothing at all. None means not
    # computed, rather than zero.
    corpus_candidates: int | None = None
    unmatched_terms: list[str] = Field(default_factory=list)
