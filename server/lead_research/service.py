"""Campaign orchestration over candidate, verification, and verdict contracts."""
from __future__ import annotations

import datetime as dt
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

from ..db import json_dump, json_load, new_id, now
from ..agent_service import AgentRunService
from .acquisition import CANDIDATE_STAGES, stage_index
from .agentic import AgenticResearchService, SCHEMA_KNOWN_FACT_FIELDS
from .candidates import CandidateRecord, CandidateRepository
from .discovery import CandidateDiscoveryService
from .facts import FactRepository, FreshnessPolicy
from .gaps import GapPlanner
from .identity import IdentityResolver
from .languages import build_market_terms
from .metrics import (
    CampaignMetricsRecorder,
    FUNNEL_KEYS,
    count_candidate_stage,
    estimate_campaign,
    zero_result_explanation,
)
from .models import (
    CampaignConfig, CampaignContext, Claim, DiscoveryQuery, LeadCandidate, LeadScore,
    PersistedOutcome, ResearchFact, ResearchReadiness, ResearchResultData,
    ResolvedIdentity,
)
from .profiles import ProfileRepository
from .qualification import EligibilityService
from .registry import ProviderRegistry, build_registry
from .quotes import EvidenceRejected, accept_fact
from .scoring import attainable_dimensions, score_lead
from .storage import EvidenceRepository
from .verdicts import SourceCoverage, evaluate_verdict, terminal_value


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.")


def _claimed_values(claims, field: str) -> list[str]:
    """Observed values for one claim field. A claim value is a scalar or a list."""
    values: list[str] = []
    for claim in claims:
        if claim.field != field or claim.status != "observed":
            continue
        raw = claim.value if isinstance(claim.value, list) else [claim.value]
        values.extend(str(item) for item in raw if str(item).strip())
    return list(dict.fromkeys(values))


class CampaignAlreadyRunning(RuntimeError):
    """A campaign cannot be queued twice; the in-flight run owns its results."""


ACTIONABLE_VERDICTS = frozenset({"strong_fit", "review"})


class ResearchRefreshService:
    """Create bounded durable refresh work for stale facts already in use."""

    def __init__(self, db, runs: AgentRunService, facts: FactRepository | None = None) -> None:
        self.db = db
        self.runs = runs
        self.facts = facts or FactRepository(db)

    def enqueue_due(self, at: dt.datetime, limit: int) -> int:
        if limit <= 0:
            return 0
        created = 0
        for fact in self.facts.due_for_refresh(at.timestamp(), limit):
            idempotency_key = f"lead-research-refresh:{fact.refresh_key}"
            if self.db.one(
                "SELECT id FROM agent_runs WHERE company_id=? AND idempotency_key=?",
                (fact.company_id, idempotency_key),
            ):
                continue
            run = self.runs.create(
                fact.company_id,
                "lead_research_refresh",
                {
                    "fact_id": fact.id,
                    "fact_pool": fact.pool,
                    "field": fact.field,
                    "dedupe_key": fact.refresh_key,
                    "organization_id": fact.organization_id,
                    "company_name": fact.organization_name,
                    "canonical_domain": fact.canonical_domain,
                    "campaign_id": fact.campaign_id,
                    "previous_value_en": fact.value_en,
                    "previous_evidence_id": fact.evidence_id,
                    "previous_expires_at": fact.expires_at,
                    # Refresh is deliberately much smaller than foreground
                    # company research. The durable run remains cancellable
                    # and cannot expand this budget from model output.
                    "budget": {
                        "page_limit": 2,
                        "request_limit": 3,
                        "time_limit_seconds": 45,
                        "token_limit": 2_000,
                    },
                },
                idempotency_key=idempotency_key,
            )
            self.runs.start(fact.company_id, run["id"])
            created += 1
        return created


def result_snapshot(
    context: CampaignContext,
    organization: ResolvedIdentity,
    score: LeadScore,
    fact_ids: list[str],
    verdict,
) -> dict:
    evidence_ids = list(dict.fromkeys(
        evidence_id
        for ids in score.dimension_evidence_ids.values()
        for evidence_id in ids
    ))
    return {
        "campaign_id": context.campaign_id,
        "profile_version_id": context.profile_version.id,
        "scope": context.scope,
        "playbook_versions": context.profile_version.profile.playbook_versions,
        "source_policy": context.config.enrichment.source_policy,
        "weights": context.config.scoring.weights.model_dump(mode="json"),
        "organization": organization.model_dump(mode="json"),
        "score": score.model_dump(mode="json"),
        "fact_ids": list(dict.fromkeys(fact_ids)),
        "evidence_ids": evidence_ids,
        "verdict": {
            "kind": verdict.kind,
            "reasons": verdict.reasons,
            "missing_evidence": verdict.missing_evidence,
            "conflicting_claims": verdict.conflicting_claims,
        },
    }


class LeadResearchService:
    def __init__(
        self,
        db,
        registry: ProviderRegistry | None = None,
        *,
        workers: int = 2,
        verify_workers: int = 4,
        agent_runs: AgentRunService | None = None,
    ):
        self.db = db
        self.registry = registry or build_registry()
        self.candidates = CandidateRepository(db)
        self.discovery = CandidateDiscoveryService(db, self.registry)
        self._gap_planner = GapPlanner()
        self._facts = FactRepository(db)
        self._agent_runs = agent_runs
        self._agentic: AgenticResearchService | None = None
        # A campaign is minutes-to-hours of blocking HTTP: three Web Unlocker
        # fetches per candidate, hundreds of candidates. It cannot run inside a
        # request handler, so it runs here — same shape as
        # DocumentProcessingService, including `wait_until_settled` so a caller
        # that genuinely needs the outcome can ask for it.
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(workers)), thread_name_prefix="lead-research"
        )
        self._lock = threading.Lock()
        self._settled: dict[tuple[str, str], dict] = {}
        self._events: dict[tuple[str, str], threading.Event] = {}
        self._closed = False
        # Candidates are verified concurrently; each source additionally caps
        # itself. 1 restores the old strictly-sequential behaviour.
        self.verify_workers = max(1, int(verify_workers))
        self._gates: dict[str, threading.Semaphore] = {}
        self._gate_lock = threading.Lock()

    def ensure_catalog(self, company_id: str) -> None:
        self.registry.ensure_tenant(self.db, company_id, now())

    def validate_readiness(self, company_id: str, config: CampaignConfig) -> ResearchReadiness:
        """Name every unmet precondition instead of failing one field at a time."""
        missing: list[str] = []
        profile = ProfileRepository(self.db).current(company_id)
        if profile is None:
            missing.append("confirmed_profile")
        else:
            identity = profile.profile.identity
            if not (
                identity.get("official_domain")
                or identity.get("website")
                or identity.get("admin_identity_exception") == "true"
            ):
                missing.append("identity_or_admin_exception")
            if not (config.seller_countries or profile.profile.seller_countries):
                missing.append("seller_country")
            if not (
                config.product_terms
                or config.product_ids
                or config.sector_ids
                or config.hs_codes
                or profile.profile.products
            ):
                missing.append("product_scope")
        if not config.target_countries:
            missing.append("target_market")
        self.ensure_catalog(company_id)
        available = {
            source["source_id"] for source in self.catalog(company_id) if source.get("available")
        }
        if not available.intersection(config.enabled_source_ids):
            missing.append("runnable_candidate_source")
        return ResearchReadiness(
            ready=not missing,
            missing=missing,
            zero_result_explanation=(
                "no_candidate_source_runnable"
                if "runnable_candidate_source" in missing else None
            ),
        )

    def discovery_query(self, campaign_id: str, company_id: str) -> DiscoveryQuery:
        row = self.db.one(
            "SELECT config,scope_snapshot,profile_version_id FROM research_campaigns "
            "WHERE id=? AND company_id=?",
            (campaign_id, company_id),
        )
        if not row:
            raise KeyError("campaign not found")
        config = CampaignConfig.model_validate(json_load(row["config"], {}))
        snapshot = json_load(row["scope_snapshot"], {})
        profile = (
            ProfileRepository(self.db).get(company_id, row["profile_version_id"])
            if row["profile_version_id"] else None
        )
        canonical = snapshot.get("product_terms", self._product_terms(company_id, config))
        market_terms = (
            build_market_terms({**snapshot, "product_terms": canonical}, profile.profile)
            if profile else None
        )
        return DiscoveryQuery(
            campaign_id=campaign_id,
            seller_countries=snapshot.get("seller_countries", config.seller_countries),
            target_countries=snapshot.get("target_countries", config.target_countries),
            sector_ids=snapshot.get("sector_ids", config.sector_ids),
            hs_codes=snapshot.get("hs_codes", config.hs_codes),
            product_terms=canonical,
            market_terms=market_terms.by_language if market_terms else {},
            buyer_types=snapshot.get("buyer_types", config.buyer_types),
            max_records=config.max_qualified_leads_per_country * 3,
        )

    def start(self, company_id: str, campaign_id: str) -> dict:
        """Queue a campaign and return without waiting for it.

        The status move to `queued` is the race guard: two rapid starts would
        otherwise both run, and a run deletes and rebuilds the campaign's own
        results, so the two would interleave deletes with inserts over the same
        rows. Whoever loses the compare-and-swap is told the campaign is
        already in flight.
        """
        if self._closed:
            raise RuntimeError("lead research service is shut down")
        changed = self.db.execute(
            "UPDATE research_campaigns SET status='queued',updated_at=? "
            "WHERE id=? AND company_id=? AND status NOT IN ('queued','running')",
            (now(), campaign_id, company_id),
        )
        if not changed:
            raise CampaignAlreadyRunning("campaign is already queued or running")
        key = (company_id, campaign_id)
        with self._lock:
            self._settled.pop(key, None)
            self._events.setdefault(key, threading.Event()).clear()
        self._pool.submit(self._run_settling, company_id, campaign_id)
        return {"status": "queued", "campaign_id": campaign_id, "run_id": None, "metrics": {}}

    def _run_settling(self, company_id: str, campaign_id: str) -> None:
        """Run a queued campaign and release anyone waiting on its outcome."""
        key = (company_id, campaign_id)
        try:
            result = self.run(company_id, campaign_id)
        except Exception as exc:
            # `run` terminalizes its own failures; reaching here means even that
            # failed. Nothing awaits this future, so record it or it is lost.
            diagnostic = {"stage": "dispatch", "message": str(exc)[:240]}
            result = {
                "status": "failed", "campaign_id": campaign_id,
                "metrics": {}, "failed_source_ids": [], "processing_error": diagnostic,
                "zero_result_explanation": "sources_failed",
            }
            self._try_save_processing_issue(
                company_id, campaign_id, None, "campaign_processing_failed", diagnostic,
            )
            self._finalize_terminal_state(company_id, campaign_id, None, "failed", result)
        with self._lock:
            self._settled[key] = result
            self._events.setdefault(key, threading.Event()).set()

    def wait_until_settled(
        self, company_id: str, campaign_id: str, timeout: float = 60
    ) -> dict | None:
        """Block until a queued campaign reaches a terminal state."""
        key = (company_id, campaign_id)
        with self._lock:
            event = self._events.setdefault(key, threading.Event())
        if not event.wait(timeout):
            return None
        with self._lock:
            return self._settled.get(key)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._agent_runs is not None:
            self._agent_runs.pool.shutdown(wait=False, cancel_futures=True)

    def _agentic_service(self) -> AgenticResearchService:
        if self._agent_runs is None:
            self._agent_runs = AgentRunService(self.db)
        if self._agentic is None:
            self._agentic = AgenticResearchService(
                self.db, runs=self._agent_runs, facts=self._facts,
            )
        return self._agentic

    def _checkpoint_candidate(
        self,
        company_id: str,
        campaign_id: str,
        partition: dict,
        candidate_id: str,
        stage: str,
        *,
        requests_started: int | None = None,
    ) -> None:
        """Persist monotonic per-candidate progress in the partition checkpoint."""
        stage_index(stage)
        checkpoints = partition.setdefault("checkpoints", {})
        previous = checkpoints.get(candidate_id, {})
        previous_stage = previous.get("stage")
        if previous_stage and stage_index(stage) < stage_index(previous_stage):
            raise ValueError("candidate stage cannot move backwards")
        checkpoints[candidate_id] = {
            "stage": stage,
            "requests_started": (
                previous.get("requests_started", 0)
                if requests_started is None else int(requests_started)
            ),
            "updated_at": now(),
        }
        self.db.execute(
            "UPDATE campaign_partitions SET checkpoint=?,updated_at=? "
            "WHERE id=? AND company_id=? AND campaign_id=?",
            (
                json_dump({"candidates": checkpoints}), now(), partition["id"],
                company_id, campaign_id,
            ),
        )

    def _cancellation_requested(self, company_id: str, campaign_id: str) -> bool:
        """Whether the tenant has asked this campaign to stop.

        `/cancel` writes the campaign row, so that row is the signal. Read once
        per candidate: one indexed SELECT against three HTTP fetches is free,
        and the alternative is a cancel that does nothing until the whole
        corpus has been paid for.
        """
        row = self.db.one(
            "SELECT status FROM research_campaigns WHERE id=? AND company_id=?",
            (campaign_id, company_id),
        )
        return bool(row) and row["status"] == "cancelled"

    # A claim field carrying "closed" retires a company for good; the corpus it
    # came from is immutable and shared, so the state has to live tenant-side.
    LIFECYCLE_FIELD = "lifecycle_status"

    def _settled_identities(self, company_id: str) -> tuple[set[tuple[str, str]], int]:
        """Identities no campaign of this tenant should spend requests on.

        Closure only. Skipping merely-validated companies used to live here too
        and was removed: a run rebuilds its own results from scratch, so any
        identity it skips is absent from its output, and the skip therefore
        emptied every campaign after the first instead of making reruns cheap.
        Per-candidate cost is bounded by `select(limit=...)` regardless.

        Returns the skip set and its size, so a run can report the exclusion
        instead of silently shrinking its own funnel.
        """
        organizations = self.db.all(
            "SELECT id,normalized_name,country FROM organizations WHERE company_id=?",
            (company_id,),
        )
        if not organizations:
            return set(), 0
        lifecycle: dict[str, tuple[float, Any]] = {}
        for row in self.db.all(
            "SELECT organization_id,field,value,verified_at FROM feature_claims "
            "WHERE company_id=? AND field=?",
            (company_id, self.LIFECYCLE_FIELD),
        ):
            organization_id = row["organization_id"]
            # Latest wins, so a later "operating" claim reopens a company
            # that was wrongly retired. Closure must not be a one-way door.
            stamp = row["verified_at"] or 0.0
            if organization_id not in lifecycle or stamp >= lifecycle[organization_id][0]:
                lifecycle[organization_id] = (stamp, json_load(row["value"], None))

        skip: set[tuple[str, str]] = set()
        for organization in organizations:
            state = lifecycle.get(organization["id"])
            value = state[1] if state else None
            if isinstance(value, list):
                value = value[0] if value else None
            if value == "closed":
                skip.add((
                    organization["normalized_name"],
                    (organization["country"] or "").upper(),
                ))
        return skip, len(skip)

    def catalog(self, company_id: str) -> list[dict]:
        self.ensure_catalog(company_id)
        rows = self.db.all(
            "SELECT * FROM dataset_definitions WHERE company_id=? ORDER BY source_id", (company_id,)
        )
        result = []
        for row in rows:
            definition = json_load(row["definition"], {})
            provider = self.registry.get(row["source_id"])
            health = provider.health()
            available = bool(row["installed"] and row["enabled"])
            reason = None
            if definition.get("health") == "retired" or health.status == "retired":
                available, reason = False, "retired"
            elif health.status == "unavailable":
                available, reason = False, health.reason or "unavailable"
            elif not (
                callable(getattr(provider, "verify", None))
                or callable(getattr(provider, "research_fields", None))
                or callable(getattr(provider, "discover_candidates", None))
            ):
                available = False
                reason = (
                    "credential_required"
                    if definition.get("adapter_mode") == "credential_required"
                    else "upload_or_adapter_required"
                )
            elif definition.get("adapter_mode") in {"manual_import", "catalog_only"}:
                available, reason = False, "upload_or_adapter_required"
            elif not row["enabled"]:
                reason = "disabled"
            result.append({
                **definition,
                "installed": bool(row["installed"]),
                "enabled": bool(row["enabled"]),
                "available": available,
                "unavailable_reason": reason,
                "health": health.status,
                "last_checked_at": row["last_checked_at"],
            })
        return result

    def estimate(self, config: CampaignConfig, company_id: str | None = None):
        """A pre-run estimate, including what the corpus can actually supply.

        The provider ranges say how much a source knows in general and never
        consulted the candidate corpus, so an estimate could read `available`
        with a healthy range while selection matched nothing — which is how a
        term spelled differently from the imported category became a campaign
        that ran, succeeded, and returned zero leads without explanation.
        """
        providers = [self.registry.get(source_id) for source_id in config.enabled_source_ids]
        estimate = estimate_campaign(config, providers)
        if company_id is None:
            return estimate
        terms = self._product_terms(company_id, config)
        profile = ProfileRepository(self.db).current(company_id)
        market_terms = build_market_terms({
            "product_terms": terms,
            "sector_ids": config.sector_ids,
            "target_countries": config.target_countries,
        }, profile.profile) if profile else None
        search_terms = (
            DiscoveryQuery(
                campaign_id="estimate",
                seller_countries=config.seller_countries,
                target_countries=config.target_countries,
                sector_ids=config.sector_ids,
                hs_codes=config.hs_codes,
                product_terms=terms,
                market_terms=market_terms.by_language if market_terms else {},
                buyer_types=config.buyer_types,
            ).search_product_terms
        )
        matches = self.candidates.term_match_counts(
            company_id=company_id, countries=config.target_countries, product_terms=search_terms,
        )
        selected = self.candidates.select(
            company_id=company_id,
            countries=config.target_countries,
            product_terms=search_terms,
            limit=config.max_qualified_leads_per_country * max(1, len(config.target_countries)) * 3,
        )
        return estimate.model_copy(update={
            "corpus_candidates": len(selected),
            "unmatched_terms": sorted(term for term, count in matches.items() if not count),
            "indexed_candidates": len(selected),
            "discoverable_candidates": estimate.named_candidate_range,
            "unavailable_sources": estimate.unavailable_source_ids,
            "unmapped_market_terms": market_terms.unmapped_markets if market_terms else [],
        })

    def _lead_ids_by_organization(self, company_id: str) -> dict[str, str]:
        """Existing leads, keyed by the organization they belong to.

        `organization_id` lives inside the lead's JSON payload, so finding one
        used to mean reading every lead row and decoding every payload — once
        per qualifying candidate. On the measured run that was 396 candidates
        against 173 leads, and it grew with the square of the tenant. Built once
        per run instead, the way `prior_results` already is.

        Not pushed into SQL: extracting from JSON is spelled differently on
        Postgres, and a tenant's lead table is small enough that one pass over
        it costs nothing next to a single Web Unlocker fetch.
        """
        index: dict[str, str] = {}
        for row in self.db.all(
            "SELECT id,data,resolved_organization_id FROM leads WHERE company_id=?",
            (company_id,),
        ):
            organization_id = (
                row["resolved_organization_id"]
                or json_load(row["data"], {}).get("organization_id")
            )
            if organization_id and organization_id not in index:
                index[organization_id] = row["id"]
        return index

    def _save_claim(
        self,
        company_id: str,
        campaign_id: str,
        organization_id: str,
        field: str,
        value: Any,
        evidence_ids: list[str],
        source_ids: list[str],
        confidence: float,
        status: str = "observed",
        validated: bool = False,
        observed_at: float | None = None,
        period: str | None = None,
        unit: str | None = None,
        currency: str | None = None,
    ) -> Claim:
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        claim = Claim(
            field=field,
            value=value,
            period=period or ("2025" if numeric else None),
            unit=unit,
            currency=currency,
            status=status,
            confidence=confidence,
            method="observed",
            evidence_ids=evidence_ids,
            applicability="useful",
            validated=validated,
            observed_at=observed_at,
        )
        self.db.execute(
            "INSERT INTO feature_claims("
            "id,company_id,campaign_id,organization_id,field,status,value,confidence,"
            "method,evidence_ids,data,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id("claim"),
                company_id,
                campaign_id,
                organization_id,
                field,
                claim.status,
                json_dump(value),
                claim.confidence,
                claim.method,
                json_dump(claim.evidence_ids),
                json_dump({
                    "source_ids": source_ids,
                    "period": claim.period,
                    "unit": claim.unit,
                    "currency": claim.currency,
                    "applicability": claim.applicability,
                    # Both ride in `data` rather than in new columns: the
                    # Postgres schema is guarded for parity, and a JSON field
                    # needs no migration to reach the deployed database.
                    "validated": claim.validated,
                    "observed_at": claim.observed_at,
                }),
                now(),
            ),
        )
        return claim

    SCALAR_CLAIM_FIELDS = frozenset({"company_name", "country", "domain"})

    def _validated(self, stored: dict) -> bool:
        """Whether a publisher with standing vouched for this evidence.

        The company's own page, or a source the catalog declares an
        authoritative registry. Deliberately the same test `SourceCoverage`
        uses for `has_authority`, so "validated" means one thing across
        scoring, verdicts and the shared cache rather than three.
        """
        return (
            stored["source"].classification == "official"
            or self._is_registry(stored["source_id"])
        )

    def _resolve_scalar(self, entries: list[dict], values: list[Any]) -> tuple[Any, bool]:
        """Pick one value for a single-valued field, or report a real conflict.

        Two sources disagreeing is information before it is a problem: a
        validated publisher outranks an unvalidated one, and on equal standing
        the newer page outranks the older. Only a disagreement between equally
        authoritative, equally recent sources is unresolvable — and only that
        one is worth penalising, which matters more once a long-lived cache
        makes stale-versus-fresh the ordinary case rather than the exception.
        """
        ranked: dict[Any, tuple[int, float]] = {}
        for entry in entries:
            standing = 1 if self._validated(entry) else 0
            age = entry["source"].retrieved_at or 0.0
            for value in entry["values"]:
                if value not in ranked or (standing, age) > ranked[value]:
                    ranked[value] = (standing, age)
        if not ranked:
            return (values[0] if values else None), False
        best = max(ranked.values())
        winners = [value for value, rank in ranked.items() if rank == best]
        if len(winners) > 1:
            # Equal standing, equal recency, still disagreeing. Nothing here can
            # break the tie, so the claim stays conflicted and carries every
            # candidate value rather than picking one arbitrarily.
            return sorted(winners, key=str), True
        return winners[0], False

    def _claim_plan(self, prepared_evidence: list[dict]) -> list[dict]:
        """Derive bounded claim writes before any tenant identity is created."""
        facts: dict[str, list[dict]] = defaultdict(list)
        for stored in prepared_evidence:
            for field, values in stored["source"].facts.items():
                facts[field].append({**stored, "values": values})
        plan: list[dict] = []
        stamp = now()
        for field in sorted(facts):
            entries = facts[field]
            values: list[Any] = []
            for entry in entries:
                for value in entry["values"]:
                    if value not in values:
                        values.append(value)
            if not values:
                continue
            if field in self.SCALAR_CLAIM_FIELDS and len(values) > 1:
                value, conflicting = self._resolve_scalar(entries, values)
            else:
                value, conflicting = (values[0] if len(values) == 1 else values), False
            plan.append({
                "field": field,
                "value": value,
                "evidence_ids": list(dict.fromkeys(entry["evidence_id"] for entry in entries)),
                "source_ids": list(dict.fromkeys(entry["source_id"] for entry in entries)),
                "confidence": round(
                    sum(entry["confidence"] for entry in entries) / len(entries), 3
                ),
                "status": "conflicted" if conflicting else "observed",
                "validated": any(self._validated(entry) for entry in entries),
                # The newest page behind the claim. A live provider result has
                # no timestamp because it is happening now; a reused one carries
                # the age of what it reused.
                "observed_at": max(
                    (entry["source"].retrieved_at or stamp) for entry in entries
                ),
            })
        return plan

    def _save_claim_plan(
        self,
        company_id: str,
        campaign_id: str,
        organization_id: str,
        plan: list[dict],
    ) -> list[Claim]:
        return [
            self._save_claim(
                company_id,
                campaign_id,
                organization_id,
                **item,
            )
            for item in plan
        ]

    def _persist_accepted_facts(
        self,
        company_id: str,
        campaign_id: str,
        organization_id: str,
        prepared_evidence: list[dict],
    ) -> list[str]:
        """Dual-write mechanically accepted facts into the durable fact pools."""
        facts = FactRepository(self.db)
        stored_ids: list[str] = []
        for stored in prepared_evidence:
            source = stored["source"]
            definition = self.registry.definitions.get(stored["source_id"])
            access_tier = definition.access_tier if definition else "public"
            if access_tier == "customer_upload":
                source_class, visibility = "customer", "private"
            elif access_tier == "licensed":
                source_class, visibility = "licensed", "licensed"
            elif source.classification == "official":
                source_class, visibility = "official", "public"
            elif self._is_registry(stored["source_id"]):
                source_class, visibility = "registry", "public"
            else:
                source_class, visibility = "public", "public"
            retrieved = stored["envelope"].retrieved_at.timestamp()
            observed = source.retrieved_at or retrieved
            freshness = FreshnessPolicy(
                default_ttl_days=definition.freshness_days if definition else 180
            )
            for field, values in source.facts.items():
                spans = source.fact_spans.get(field, [])
                for value, span in zip(values, spans):
                    proposed = ResearchFact(
                        organization_id=organization_id,
                        campaign_id=campaign_id,
                        field=field,
                        value_en=value,
                        original_text=span.original,
                        source_language=source.source_language,
                        derivation_kind="observed",
                        status="observed",
                        confidence=stored["confidence"],
                        validation_basis="pending exact-span validation",
                        evidence_id=stored["evidence_id"],
                        span=span,
                        source_class=source_class,
                        visibility=visibility,
                        mechanically_validated=False,
                        observed_at=observed,
                        retrieved_at=retrieved,
                        expires_at=freshness.expires_at(
                            field, source_class, observed, retrieved,
                        ),
                    )
                    try:
                        accepted = accept_fact(stored["envelope"], proposed)
                        accepted = accepted.model_copy(update={
                            "expires_at": freshness.expires_at(
                                field,
                                source_class,
                                accepted.observed_at,
                                accepted.retrieved_at,
                            )
                        })
                        stored_ids.append(facts.accept(company_id, accepted).id)
                    except (EvidenceRejected, ValueError) as exc:
                        self._try_save_processing_issue(
                            company_id,
                            campaign_id,
                            organization_id,
                            "evidence_rejected",
                            {
                                "field": field,
                                "source_id": stored["source_id"],
                                "message": str(exc)[:240],
                            },
                        )
        return stored_ids

    @staticmethod
    def _identity_payload(prepared_evidence: list[dict]) -> dict | None:
        by_field: dict[str, list[tuple[int, Any]]] = defaultdict(list)
        for stored in prepared_evidence:
            priority = 0 if stored["source"].classification == "official" else 1
            for field in ("company_name", "domain", "country", "registry_id"):
                for value in stored["source"].facts.get(field, []):
                    if value and all(existing != value for _, existing in by_field[field]):
                        by_field[field].append((priority, value))
        names = [value for _, value in sorted(by_field["company_name"])]
        domains = [value for _, value in sorted(by_field["domain"])]
        countries = [value for _, value in sorted(by_field["country"])]
        registry_ids = [value for _, value in sorted(by_field["registry_id"])]
        if not names and not domains:
            return None
        return {
            # A verified domain is a bounded display fallback when no source
            # supplies a name. Candidate-corpus names remain matching hints,
            # never stored organization facts.
            "display_name": names[0] if names else domains[0],
            "legal_name": names[0] if names else None,
            "domain": domains[0] if domains else None,
            "country": countries[0] if countries else None,
            "registry_id": registry_ids[0] if registry_ids else None,
            "evidence_backed_fields": sorted(
                field for field, values in by_field.items() if values
            ),
        }

    def _save_processing_issue(
        self,
        company_id: str,
        campaign_id: str,
        organization_id: str | None,
        issue_type: str,
        data: dict,
    ) -> None:
        stamp = now()
        self.db.execute(
            "INSERT INTO research_issues VALUES(?,?,?,?,?,?,?,?,?)",
            (
                new_id("issue"), company_id, campaign_id, organization_id,
                issue_type, "open", json_dump(data), stamp, stamp,
            ),
        )

    def _try_save_processing_issue(
        self,
        company_id: str,
        campaign_id: str,
        organization_id: str | None,
        issue_type: str,
        data: dict,
    ) -> bool:
        """Keep diagnostic storage from becoming a second orchestration failure."""
        try:
            self._save_processing_issue(
                company_id, campaign_id, organization_id, issue_type, data,
            )
            return True
        except Exception:
            return False

    def _finalize_terminal_state(
        self,
        company_id: str,
        campaign_id: str,
        run_id: str | None,
        final_status: str,
        output: dict,
    ) -> list[dict]:
        """Attempt campaign and run terminal writes independently."""
        errors: list[dict] = []
        try:
            self.db.execute(
                "UPDATE research_campaigns SET status=?,updated_at=? WHERE id=? AND company_id=?",
                (final_status, now(), campaign_id, company_id),
            )
        except Exception as exc:
            errors.append({"stage": "campaign_terminal_update", "message": str(exc)[:240]})

        if errors:
            output["terminal_errors"] = list(errors)
        if run_id:
            try:
                self.db.execute(
                    "UPDATE agent_runs SET status=?,output=?,completed_at=?,updated_at=? "
                    "WHERE id=? AND company_id=?",
                    (
                        "failed" if final_status == "failed" or errors
                        else "cancelled" if final_status == "cancelled"
                        else "succeeded",
                        json_dump(output), now(), now(), run_id, company_id,
                    ),
                )
            except Exception as exc:
                errors.append({"stage": "agent_run_terminal_update", "message": str(exc)[:240]})
                output["terminal_errors"] = list(errors)
        return errors

    def _reusable_bundles(
        self,
        company_id: str,
        config: CampaignConfig,
        repo: EvidenceRepository,
        query_fingerprints: set[str] | None = None,
    ) -> dict[tuple[str, str], Any]:
        """Cached evidence this run may stand on instead of re-fetching.

        Honours `refresh.reuse_public_cache`, which the campaign editor has
        always collected and nothing has ever read. Each source's own
        `freshness_days` sets its window — TED's 7 days and a customer corpus's
        365 are different claims about how fast the underlying record changes,
        and `freshness_days` is the field that already says so.
        """
        if not config.refresh.get("reuse_public_cache", True):
            return {}
        stamp = now()
        fingerprints = query_fingerprints or {repo.query_fingerprint(DiscoveryQuery(
            campaign_id="fingerprint",
            seller_countries=config.seller_countries,
            target_countries=config.target_countries,
            sector_ids=config.sector_ids,
            hs_codes=config.hs_codes,
            product_terms=config.product_terms,
            buyer_types=config.buyer_types,
        ))}
        cutoffs = {}
        for source_id in config.enabled_source_ids:
            definition = self.registry.definitions.get(source_id)
            if definition is None:
                continue
            cutoffs[source_id] = stamp - definition.freshness_days * 86400
        return repo.reusable_bundles(cutoffs, fingerprints)

    def _source_gate(self, source_id: str) -> threading.Semaphore:
        """How many candidates may hit one source at once.

        A property of the upstream, declared per source. Without it, adding
        concurrency would have made TED strictly worse: it answers 429 readily
        enough that its adapter already carries a backoff, so four workers
        hitting it at once converts a working free source into a failing one.
        Shared across campaigns, because the limit protects the upstream rather
        than any one run.
        """
        with self._gate_lock:
            gate = self._gates.get(source_id)
            if gate is None:
                definition = self.registry.definitions.get(source_id)
                gate = threading.Semaphore(definition.max_concurrency if definition else 1)
                self._gates[source_id] = gate
        return gate

    def _collect_bundles(
        self,
        candidate: CandidateRecord,
        query: DiscoveryQuery,
        source_ids: list[str],
        providers: dict,
        reusable: dict,
    ) -> dict:
        """Verify one candidate against every available source. Network only.

        This is the part that costs seconds, and it runs on a worker thread — so
        it deliberately touches nothing shared. Partition counters, issue rows
        and every other write are applied by the caller on the campaign's own
        thread, which is what keeps identity resolution and the funnel free of
        races and keeps all database work single-threaded.
        """
        outcome: dict = {
            "bundles": [], "reused": [], "abstained": [], "errors": [], "requests": {},
        }
        for source_id in source_ids:
            cached = reusable.get((source_id, candidate.source_record_id))
            if cached is not None:
                outcome["bundles"].append((source_id, cached))
                outcome["reused"].append(source_id)
                continue
            try:
                with self._source_gate(source_id):
                    provider = providers[source_id]
                    research = getattr(provider, "research_fields", None)
                    bundle = (
                        research(
                            candidate,
                            frozenset(self.registry.definitions[source_id].emits),
                            query,
                        )
                        if callable(research)
                        else provider.verify(query, candidate)
                    )
                if bundle.candidate_source_record_id != candidate.source_record_id:
                    raise ValueError("verifier returned evidence for a different candidate")
                # Counted before anything decides whether to keep the bundle: an
                # abstention still fetched its pages, and dropping the count with
                # the bundle would make the cheapest-looking runs the ones that
                # found nothing.
                outcome["requests"][source_id] = (
                    outcome["requests"].get(source_id, 0) + bundle.requests
                )
                if not bundle.sources:
                    # An abstention, not a verification. A provider with nothing
                    # to say returns an empty bundle (a corpus row without a
                    # citation, say), and counting it as a bundle used to carry
                    # the candidate to the identity stage to die there on "no
                    # evidence-backed identity" — an internal-sounding error in
                    # place of the true one, which is that no source could vouch
                    # for this company.
                    outcome["abstained"].append(source_id)
                    continue
                outcome["bundles"].append((source_id, bundle))
            except Exception as exc:
                outcome["errors"].append((source_id, str(exc)[:240]))
        return outcome

    def _verify_batch(
        self,
        batch: list[CandidateRecord],
        query: DiscoveryQuery,
        source_ids: list[str],
        providers: dict,
        reusable: dict,
        pool: ThreadPoolExecutor | None,
    ) -> list[tuple[CandidateRecord, dict]]:
        """Verify a batch concurrently, returned in candidate order.

        Order is restored deliberately. Concurrency must not reach the parts of
        a run that have to be reproducible: results are rebuilt in a stable
        order, and identity resolution reads before it writes, so two candidates
        resolving to one company have to arrive one after the other.
        """
        if pool is None or len(batch) == 1:
            return [
                (candidate, self._collect_bundles(
                    candidate, query, source_ids, providers, reusable,
                ))
                for candidate in batch
            ]
        futures = [
            pool.submit(
                self._collect_bundles, candidate, query, source_ids, providers, reusable,
            )
            for candidate in batch
        ]
        outcomes: list[tuple[CandidateRecord, dict]] = []
        for candidate, future in zip(batch, futures):
            try:
                outcomes.append((candidate, future.result()))
            except Exception as exc:
                # `_collect_bundles` handles a provider raising, so reaching here
                # is a fault in our own code. One candidate must not take the
                # batch down with it.
                outcomes.append((candidate, {
                    "bundles": [], "reused": [], "abstained": [], "requests": {},
                    "errors": [(source_ids[0] if source_ids else "unknown", str(exc)[:240])],
                }))
        return outcomes

    def _is_registry(self, source_id: str) -> bool:
        """Whether this source publishes with authority of its own.

        Declared in the provider catalog, not inferred: "the EU's Publications
        Office is authoritative" is a licensing and provenance judgement about a
        publisher, and guessing it from a domain would be exactly the kind of
        upgrade-a-hint-into-evidence this module refuses everywhere else.
        """
        definition = self.registry.definitions.get(source_id)
        return bool(definition and "authoritative_registry" in definition.capabilities)

    def _candidate_payload(self, candidate: CandidateRecord, config: CampaignConfig) -> dict:
        return {
            **candidate.data,
            "display_name": candidate.company_name,
            "country": candidate.country,
            "domain": candidate.domain,
            "buyer_types": candidate.data.get("buyer_types", []),
            "sector_ids": config.sector_ids,
        }

    def _enrich_candidate(
        self,
        config: CampaignConfig,
        query: DiscoveryQuery,
        candidate: CandidateRecord,
        providers: dict,
        available_source_ids: list[str],
        bundles: list,
        *,
        profile_version=None,
        organization_id: str | None = None,
        reusable_facts=None,
    ) -> tuple[list, list[str], int]:
        """Fill structured criterion gaps in one field-batched pass per source.

        Returns the extra bundles, criterion fields still missing after them,
        and the requests they cost — so a run can say what it looked for,
        whether it found it, and what looking was worth.
        """
        # Deep research is the most expensive thing a run does, so it does not
        # run on a company whose assessment is already over. A closed company
        # cannot be argued back into business by more evidence, and it is headed
        # for `reject` whatever else we learn — so every fetch spent here is
        # spent to confirm a rejection.
        #
        # Only terminal facts prune. A candidate that merely looks unpromising
        # is not skipped: combining means further evidence can only raise a
        # score, so there is no sound ceiling to rule anything out against, and
        # a prune on "probably will not qualify" would silently drop leads that
        # would have qualified.
        for _, bundle in bundles:
            for source in bundle.sources:
                for field, values in source.facts.items():
                    if terminal_value(field, values):
                        return [], [], 0
        fact_fields = {
            field
            for _, bundle in bundles
            for source in bundle.sources
            for field in source.facts
        }
        lead_candidate = LeadCandidate(
            organization_id=organization_id or candidate.source_record_id,
            display_name=candidate.company_name,
            domain=candidate.domain,
            country=candidate.country,
            qualifying_evidence=[],
        )
        capabilities = self.registry.source_capabilities(
            available_source_ids, provider_overrides=providers,
        )
        plan = self._gap_planner.plan(
            profile_version,
            config,
            lead_candidate,
            reusable_facts=list(reusable_facts or []),
            capabilities=capabilities,
            observed_fields=fact_fields,
        )
        missing = sorted({
            field for gap in plan.gaps if gap.route != "reuse" for field in gap.fields
        })
        structured_batches = [batch for batch in plan.batches if batch.route == "structured"]
        if not structured_batches:
            return [], missing, 0

        spent = 0
        seen = {
            source.provenance_url
            for _, bundle in bundles for source in bundle.sources
        }
        extra = []
        for batch in structured_batches:
            source_id = batch.source_ids[0] if batch.source_ids else batch.source_hint
            provider = providers.get(source_id)
            research_fields = getattr(provider, "research_fields", None)
            if not callable(research_fields):
                continue
            try:
                bundle = research_fields(candidate, frozenset(batch.fields), query)
            except Exception:
                # A failed enrichment must never lose the first pass's evidence.
                # The candidate keeps whatever it already had.
                continue
            # Spend first: the pages were fetched whether or not anything
            # in them turns out to be new.
            spent += bundle.requests
            if bundle.candidate_source_record_id != candidate.source_record_id:
                continue
            fresh = [source for source in bundle.sources if source.provenance_url not in seen]
            if not fresh:
                continue
            seen.update(source.provenance_url for source in fresh)
            extra.append((source_id, bundle.model_copy(update={"sources": fresh})))

        completed_fields = fact_fields | {
            field
            for _, bundle in extra
            for source in bundle.sources
            for field in source.facts
        }
        completed_plan = self._gap_planner.plan(
            profile_version,
            config,
            lead_candidate,
            reusable_facts=list(reusable_facts or []),
            capabilities=capabilities,
            observed_fields=completed_fields,
        )
        still_missing = sorted({
            field
            for gap in completed_plan.gaps
            if gap.route != "reuse"
            for field in gap.fields
        })
        return extra, still_missing, spent

    def _agentic_gap_plan(
        self,
        config: CampaignConfig,
        candidate: CandidateRecord,
        organization_id: str,
        available_source_ids: list[str],
        providers: dict,
        reusable_facts,
        observed_fields: set[str],
        profile_version,
        *,
        fit_score: int | None = None,
        priority_band: str | None = None,
        qualifying_evidence=None,
    ):
        lead_candidate = LeadCandidate(
            organization_id=organization_id,
            display_name=candidate.company_name,
            domain=candidate.domain,
            country=candidate.country,
            qualifying_evidence=list(qualifying_evidence or []),
            fit_score=fit_score,
            priority_band=priority_band,
        )
        plan = self._gap_planner.plan(
            profile_version,
            config,
            lead_candidate,
            reusable_facts=list(reusable_facts or []),
            capabilities=self.registry.source_capabilities(
                available_source_ids, provider_overrides=providers,
            ),
            observed_fields=observed_fields,
        )
        return lead_candidate, plan

    def _run_agentic_gap(
        self,
        company_id: str,
        campaign_id: str,
        candidate: LeadCandidate,
        plan,
        config: CampaignConfig,
    ):
        """Run one durable gap worker, honoring campaign cancellation and time."""
        agentic = self._agentic_service()
        ref = agentic.enqueue_if_needed(company_id, campaign_id, candidate, plan)
        if ref is None:
            return [], None
        run = self._agent_runs.start(company_id, ref.run_id)
        deadline = time.monotonic() + config.enrichment.max_seconds_per_company
        while run["status"] not in {"succeeded", "failed", "cancelled"}:
            if self._cancellation_requested(company_id, campaign_id):
                try:
                    self._agent_runs.cancel(company_id, ref.run_id)
                except Exception:
                    pass
                return [], ref
            if time.monotonic() >= deadline:
                try:
                    self._agent_runs.cancel(company_id, ref.run_id)
                except Exception:
                    pass
                return [], ref
            time.sleep(.02)
            run = self._agent_runs.get(company_id, ref.run_id)
        if run["status"] != "succeeded":
            return [], ref
        return agentic.accept_result(company_id, ref.run_id), ref

    def _claims_from_stored_facts(
        self,
        company_id: str,
        campaign_id: str,
        organization_id: str,
        stored_facts,
    ) -> list[Claim]:
        claims: list[Claim] = []
        for fact in stored_facts:
            claims.append(self._save_claim(
                company_id,
                campaign_id,
                organization_id,
                fact.field,
                fact.value_en,
                [fact.evidence_id],
                [fact.source_class],
                fact.confidence,
                status=fact.status,
                validated=(
                    fact.mechanically_validated
                    and fact.source_class in {"official", "registry"}
                ),
                observed_at=fact.observed_at,
                period=fact.period,
                unit=fact.unit,
                currency=fact.currency,
            ))
        return claims

    def _append_score_snapshot(
        self,
        context: CampaignContext,
        organization: ResolvedIdentity,
        result_id: str,
        score: LeadScore,
        verdict,
        fact_ids: list[str],
    ) -> tuple[str, dict]:
        snapshot = result_snapshot(context, organization, score, fact_ids, verdict)
        snapshot_id = new_id("score")
        self.db.execute(
            "INSERT INTO research_score_snapshots("
            "id,company_id,result_id,campaign_id,profile_version_id,organization_id,"
            "snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                snapshot_id, context.company_id, result_id, context.campaign_id,
                context.profile_version.id, organization.organization_id,
                json_dump(snapshot), now(),
            ),
        )
        self.db.execute(
            "UPDATE research_results SET profile_version_id=?,snapshot_json=?,updated_at=? "
            "WHERE id=? AND company_id=?",
            (
                context.profile_version.id, json_dump(snapshot), now(), result_id,
                context.company_id,
            ),
        )
        return snapshot_id, snapshot

    def persist_outcome(
        self,
        context: CampaignContext,
        organization: ResolvedIdentity,
        score: LeadScore,
        verdict,
        facts,
    ) -> PersistedOutcome:
        """Persist one decision; rejected results never become operational leads."""
        context = CampaignContext.model_validate(context)
        organization = ResolvedIdentity.model_validate(organization)
        score = LeadScore.model_validate(score)
        fact_ids = [fact.id for fact in facts]
        snapshot = result_snapshot(context, organization, score, fact_ids, verdict)
        repo = EvidenceRepository(self.db, context.company_id)
        result_id = repo.upsert_result(
            campaign_id=context.campaign_id,
            organization_id=organization.organization_id,
            lead_id=None,
            verdict=verdict.kind,
            fit_score=score.fit_score,
            evidence_confidence=score.evidence_confidence,
            data=snapshot,
        )
        lead_id = None
        if verdict.kind in ACTIONABLE_VERDICTS:
            existing = self.db.one(
                "SELECT id FROM leads WHERE company_id=? AND resolved_organization_id=?",
                (context.company_id, organization.organization_id),
            )
            lead_id = existing["id"] if existing else new_id("lead")
            stored_org = self.db.one(
                "SELECT display_name,domain,country FROM organizations "
                "WHERE id=? AND company_id=?",
                (organization.organization_id, context.company_id),
            )
            if stored_org is None:
                raise ValueError("resolved organization is outside the tenant")
            stamp = now()
            lead_data = {
                "organization_id": organization.organization_id,
                "research_campaign_id": context.campaign_id,
                "fit_score": score.fit_score,
                "evidence_confidence": score.evidence_confidence,
                "priority_band": score.priority_band,
                "verdict": verdict.kind,
            }
            if existing:
                self.db.execute(
                    "UPDATE leads SET company_name=?,website=?,country=?,status=?,data=?,"
                    "resolved_organization_id=?,updated_at=? WHERE id=? AND company_id=?",
                    (
                        stored_org["display_name"], stored_org["domain"], stored_org["country"],
                        "qualified" if verdict.kind == "strong_fit" else "review",
                        json_dump(lead_data), organization.organization_id, stamp,
                        lead_id, context.company_id,
                    ),
                )
            else:
                self.db.execute(
                    "INSERT INTO leads("
                    "id,company_id,scan_id,company_name,website,country,status,do_not_contact,"
                    "data,created_at,updated_at,resolved_organization_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        lead_id, context.company_id, None, stored_org["display_name"],
                        stored_org["domain"], stored_org["country"],
                        "qualified" if verdict.kind == "strong_fit" else "review",
                        0, json_dump(lead_data), stamp, stamp, organization.organization_id,
                    ),
                )
            repo.upsert_result(
                campaign_id=context.campaign_id,
                organization_id=organization.organization_id,
                lead_id=lead_id,
                verdict=verdict.kind,
                fit_score=score.fit_score,
                evidence_confidence=score.evidence_confidence,
                data=snapshot,
                result_id=result_id,
            )
        snapshot_id, snapshot = self._append_score_snapshot(
            context, organization, result_id, score, verdict, fact_ids,
        )
        return PersistedOutcome(
            result_id=result_id,
            lead_id=lead_id,
            score_snapshot_id=snapshot_id,
            snapshot=snapshot,
        )

    def _product_terms(self, company_id: str, config: CampaignConfig) -> list[str]:
        terms = [*config.product_terms, *config.sector_ids, *config.hs_codes]
        if config.product_ids:
            rows = self.db.all(
                "SELECT name FROM products WHERE company_id=? AND id IN ({})".format(
                    ",".join("?" for _ in config.product_ids)
                ),
                (company_id, *config.product_ids),
            )
            terms.extend(row["name"] for row in rows)
        return list(dict.fromkeys(terms))

    def _upsert_lead(
        self,
        company_id: str,
        campaign_id: str,
        organization_id: str,
        config: CampaignConfig,
        score,
        gate,
        verdict,
        source_ids: list[str],
        evidence_domains: list[str],
        claims: list[Claim],
        lead_ids: dict[str, str],
    ) -> str:
        organization = self.db.one(
            "SELECT display_name,domain,country FROM organizations WHERE id=? AND company_id=?",
            (organization_id, company_id),
        )
        if not organization:
            raise RuntimeError("resolved organization is missing")
        buyer_claim = next(
            (claim for claim in claims if claim.field == "buyer_role" and claim.status == "observed"),
            None,
        )
        buyer_value = buyer_claim.value if buyer_claim else None
        buyer_type = buyer_value[0] if isinstance(buyer_value, list) and buyer_value else buyer_value
        lead_data = {
            "organization_id": organization_id,
            "research_campaign_id": campaign_id,
            "industry": config.sector_ids[0] if config.sector_ids else None,
            "buyer_type": buyer_type,
            "fit_score": score.fit_score,
            "evidence_confidence": score.evidence_confidence,
            "priority_band": score.priority_band,
            "known_weight": score.known_weight,
            "unknown_weight": score.unknown_weight,
            "unknown_dimensions": score.unknown_dimensions,
            "not_applicable_dimensions": score.not_applicable_dimensions,
            "verdict": verdict.kind,
            "score_dimensions": score.dimensions,
            "confidence_factors": score.confidence_factors,
            "eligibility": gate.gates,
            "applicable_feature_completeness": round(len(claims) / max(1, len(config.features)) * 100),
            "source_ids": source_ids,
            "top_evidence_sources": evidence_domains,
        }
        existing_id = lead_ids.get(organization_id)
        lead_status = "qualified" if verdict.kind == "strong_fit" else "review"
        if existing_id:
            self.db.execute(
                "UPDATE leads SET company_name=?,website=?,country=?,status=?,data=?,"
                "resolved_organization_id=?,updated_at=? WHERE id=?",
                (
                    organization["display_name"],
                    organization["domain"],
                    organization["country"],
                    lead_status,
                    json_dump(lead_data),
                    organization_id,
                    now(),
                    existing_id,
                ),
            )
            return existing_id
        lead_id = new_id("lead")
        stamp = now()
        self.db.execute(
            "INSERT INTO leads("
            "id,company_id,scan_id,company_name,website,country,status,do_not_contact,"
            "data,created_at,updated_at,resolved_organization_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lead_id,
                company_id,
                None,
                organization["display_name"],
                organization["domain"],
                organization["country"],
                lead_status,
                0,
                json_dump(lead_data),
                stamp,
                stamp,
                organization_id,
            ),
        )
        # Kept current within the run: two candidates can resolve to one
        # organization, and the second must update the first's lead rather than
        # insert a duplicate.
        lead_ids[organization_id] = lead_id
        return lead_id

    def run(self, company_id: str, campaign_id: str) -> dict:
        """Guarantee a terminalization attempt around all started-run work."""
        result: dict | None = None
        outer_error: Exception | None = None
        fallback_run_id: str | None = None
        fallback_output: dict = {
            "campaign_id": campaign_id,
            "metrics": {},
            "failed_source_ids": [],
            "zero_result_explanation": "sources_failed",
        }
        try:
            result = self._run_campaign(company_id, campaign_id)
        except Exception as exc:
            outer_error = exc
            try:
                campaign = self.db.one(
                    "SELECT run_id FROM research_campaigns WHERE id=? AND company_id=?",
                    (campaign_id, company_id),
                )
                fallback_run_id = campaign["run_id"] if campaign else None
            except Exception:
                fallback_run_id = None
            diagnostic = {"stage": "campaign_processing", "message": str(exc)[:240]}
            fallback_output["processing_error"] = diagnostic
            self._try_save_processing_issue(
                company_id, campaign_id, None, "campaign_processing_failed", diagnostic,
            )
        finally:
            if outer_error is not None:
                self._finalize_terminal_state(
                    company_id, campaign_id, fallback_run_id, "failed", fallback_output,
                )
        if outer_error is not None:
            return {
                "status": "failed",
                "run_id": fallback_run_id,
                **fallback_output,
            }
        assert result is not None
        return result

    def _run_campaign(self, company_id: str, campaign_id: str) -> dict:
        row = self.db.one(
            "SELECT * FROM research_campaigns WHERE id=? AND company_id=?", (campaign_id, company_id)
        )
        if not row:
            raise KeyError("campaign not found")
        config = CampaignConfig.model_validate(json_load(row["config"], {}))
        scope_snapshot = json_load(row["scope_snapshot"], {})
        frozen_profile = (
            ProfileRepository(self.db).get(company_id, row["profile_version_id"])
            if row["profile_version_id"] else None
        )
        if row["status"] == "cancelled":
            # Cancelled between queueing and pickup. Claiming it as `running`
            # first would lose the cancellation and research the whole corpus.
            return {
                "status": "cancelled",
                "run_id": None,
                "campaign_id": campaign_id,
                "metrics": {},
                "failed_source_ids": [],
                "zero_result_explanation": "campaign_cancelled",
            }
        self.ensure_catalog(company_id)
        stamp = now()
        run_id = new_id("run")
        prior_results = {
            result["organization_id"]: dict(result)
            for result in self.db.all(
                "SELECT * FROM research_results WHERE company_id=? AND campaign_id=?",
                (company_id, campaign_id),
            )
        }
        lead_ids = self._lead_ids_by_organization(company_id)
        for table in ("campaign_partitions", "campaign_metrics", "research_issues", "feature_claims"):
            self.db.execute(
                f"DELETE FROM {table} WHERE company_id=? AND campaign_id=?", (company_id, campaign_id)
            )
        # Results are the current campaign view, not append-only history. Preserve
        # their identities for unchanged candidates, but remove stale visibility
        # before rebuilding this run.
        self.db.execute(
            "DELETE FROM research_results WHERE company_id=? AND campaign_id=?",
            (company_id, campaign_id),
        )
        self.db.execute(
            "UPDATE research_campaigns SET status='running',run_id=?,updated_at=? WHERE id=? AND company_id=?",
            (run_id, stamp, campaign_id, company_id),
        )
        self.db.execute(
            "INSERT INTO agent_runs(id,company_id,run_type,status,payload,output,error,output_ref,idempotency_key,"
            "cancellation_requested,cost,created_at,started_at,completed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                company_id,
                "lead_scan",
                "running",
                json_dump({"research_campaign_id": campaign_id}),
                None,
                None,
                campaign_id,
                None,
                0,
                0,
                stamp,
                stamp,
                None,
                stamp,
            ),
        )

        verify_pool = (
            ThreadPoolExecutor(
                max_workers=self.verify_workers, thread_name_prefix="lead-verify",
            )
            if self.verify_workers > 1 else None
        )
        repo = EvidenceRepository(self.db, company_id)
        resolver = IdentityResolver(self.db, company_id)
        eligibility = EligibilityService()
        metrics = {key: 0 for key in FUNNEL_KEYS}
        partitions: dict[tuple[str, str], dict] = {}
        processing_error: dict | None = None
        # Bound before the try so a campaign that fails early still reports
        # zero enrichment rather than blowing up on the way out.
        enriched = 0
        reused_bundles = 0
        provider_requests = 0
        unresolved_gaps: set[str] = set()
        unmapped_markets: set[str] = set()
        cancelled = False
        try:
            catalog = {item["source_id"]: item for item in self.catalog(company_id)}
            providers = {
                source_id: self.registry.get(source_id)
                for source_id in config.enabled_source_ids
            }
            for country in config.target_countries:
                for source_id in config.enabled_source_ids:
                    partition_id = new_id("part")
                    source_state = catalog[source_id]
                    partitions[(source_id, country)] = {
                        "id": partition_id,
                        "available": source_state["available"],
                        "reason": source_state["unavailable_reason"],
                        "selected": 0,
                        "verified": 0,
                        "completed": 0,
                        "evidence": 0,
                        "errors": [],
                        "checkpoints": {},
                    }
                    self.db.execute(
                        "INSERT INTO campaign_partitions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            partition_id, company_id, campaign_id, source_id, country,
                            config.sector_ids[0] if config.sector_ids else None,
                            "running", None, json_dump({}), None, now(),
                        ),
                    )

            # What this run's sources could establish at all, so a dimension
            # none of them can reach is not held against a lead's confidence.
            # Availability matters, not just enablement: a credential-gated
            # source emits nothing, so counting its fields would understate
            # completeness exactly as the fixed denominator used to.
            attainable = attainable_dimensions({
                field
                for source_id in config.enabled_source_ids
                if catalog.get(source_id, {}).get("available")
                for field in (
                    self.registry.definitions[source_id].emits
                    if source_id in self.registry.definitions else []
                )
            })
            metrics["attainable_dimensions"] = sorted(attainable)
            product_terms = scope_snapshot.get(
                "product_terms", self._product_terms(company_id, config)
            )
            queries: dict[str, DiscoveryQuery] = {}
            for country in config.target_countries:
                market_terms = (
                    build_market_terms({
                        **scope_snapshot,
                        "target_countries": [country],
                        "product_terms": product_terms,
                    }, frozen_profile.profile)
                    if frozen_profile else None
                )
                if market_terms is not None:
                    unmapped_markets.update(market_terms.unmapped_markets)
                queries[country] = DiscoveryQuery(
                    campaign_id=campaign_id,
                    seller_countries=config.seller_countries,
                    target_countries=[country],
                    sector_ids=config.sector_ids,
                    hs_codes=config.hs_codes,
                    product_terms=product_terms,
                    market_terms=market_terms.by_language if market_terms else {},
                    buyer_types=config.buyer_types,
                    max_records=config.max_qualified_leads_per_country * 3,
                )
            # Evidence already paid for, still fresh enough to stand. Read once
            # for the whole run, accepting the exact per-market query shapes.
            reusable = self._reusable_bundles(
                company_id,
                config,
                repo,
                {repo.query_fingerprint(query) for query in queries.values()},
            )
            settled, closed_count = self._settled_identities(company_id)
            # Outside FUNNEL_KEYS on purpose: the funnel is monotonic and this
            # describes work never started, not a stage that lost records.
            metrics["excluded_closed"] = closed_count
            for country in config.target_countries:
                if cancelled:
                    break
                query = queries[country]
                supply = self.discovery.supply(
                    company_id,
                    query,
                    config.max_qualified_leads_per_country * 3,
                    exclude=settled,
                    repository=self.candidates,
                )
                candidates = supply.candidates
                for key, value in supply.counts.items():
                    metrics[f"candidate_supply_{key}"] = (
                        metrics.get(f"candidate_supply_{key}", 0) + value
                    )
                provider_requests += (
                    supply.counts.get("cheap_verification_requests", 0)
                    + supply.counts.get("candidate_discovery_requests", 0)
                )
                if not candidates:
                    # A market that selected nothing is indistinguishable from a
                    # market with no buyers in it unless the run says which it
                    # was. Term matching is substring matching against a corpus
                    # category, so a term spelled differently from what was
                    # imported silently selects zero — the failure this names.
                    self._try_save_processing_issue(
                        company_id, campaign_id, None, "no_candidates_selected",
                        {
                            "country": country,
                            "terms": product_terms,
                            "term_matches": self.candidates.term_match_counts(
                                company_id=company_id,
                                countries=[country], product_terms=product_terms,
                            ),
                            "message": (
                                "No candidate matched these terms in this market. "
                                "A term matching 0 is spelled differently from the "
                                "imported corpus category."
                            ),
                        },
                    )
                metrics["raw_records"] += len(candidates)
                metrics["named_candidates"] += len(candidates)
                for source_id in config.enabled_source_ids:
                    partitions[(source_id, country)]["selected"] = len(candidates)

                available_source_ids = [
                    source_id for source_id in config.enabled_source_ids
                    if partitions[(source_id, country)]["available"]
                ]
                # Verified in bounded batches rather than one candidate at a
                # time: three fetches at a 45-second timeout, serially, made a
                # 150-candidate market ~450 sequential round trips. Each batch
                # is fetched concurrently and then consumed in candidate order,
                # so every database write — identity resolution above all —
                # still happens on this one thread, in a deterministic order.
                for start in range(0, len(candidates), self.verify_workers):
                    batch = candidates[start:start + self.verify_workers]
                    if self._cancellation_requested(company_id, campaign_id):
                        # Checked per batch, so a cancel costs at most the batch
                        # already in flight instead of the whole market.
                        cancelled = True
                        break
                    primary_source_id = (
                        available_source_ids[0]
                        if available_source_ids else config.enabled_source_ids[0]
                    )
                    primary_partition = partitions[(primary_source_id, country)]
                    for pending in batch:
                        self._checkpoint_candidate(
                            company_id, campaign_id, primary_partition,
                            pending.source_record_id, "supplied",
                        )
                        count_candidate_stage(metrics, "supplied")
                        self._checkpoint_candidate(
                            company_id, campaign_id, primary_partition,
                            pending.source_record_id, "gated",
                        )
                        count_candidate_stage(metrics, "gated")
                    outcomes = self._verify_batch(
                        batch, query, available_source_ids, providers, reusable, verify_pool,
                    )
                    for candidate, outcome in outcomes:
                        bundles: list[tuple[str, Any]] = list(outcome["bundles"])
                        verification_messages: list[str] = []
                        abstained: list[str] = list(outcome["abstained"])
                        reused_sources = set(outcome["reused"])
                        for source_id, bundle in bundles:
                            partition = partitions[(source_id, country)]
                            partition["verified"] += 1
                            partition["evidence"] += len(bundle.sources)
                            if source_id in reused_sources:
                                partition["reused"] = partition.get("reused", 0) + 1
                                reused_bundles += 1
                        for source_id, spent in outcome["requests"].items():
                            partition = partitions[(source_id, country)]
                            partition["requests"] = partition.get("requests", 0) + spent
                            provider_requests += spent
                        for source_id, message in outcome["errors"]:
                            verification_messages.append(message)
                            partitions[(source_id, country)]["errors"].append({
                                "candidate_source_record_id": candidate.source_record_id,
                                "stage": "verification",
                                "message": message,
                            })
                        if not bundles:
                            self._try_save_processing_issue(
                                company_id, campaign_id, None, "candidate_processing_failed",
                                {
                                    "candidate_source_record_id": candidate.source_record_id,
                                    "stage": "verification",
                                    "messages": verification_messages or [
                                        f"no evidence from {', '.join(abstained)}"
                                        if abstained else "no verifier was available"
                                    ],
                                },
                            )
                            continue

                        organization_id: str | None = None
                        evaluated_verdict = None
                        stage = "evidence"
                        try:
                            stage = "evidence"
                            prepared_evidence = [
                                stored
                                for source_id, bundle in bundles
                                for stored in repo.prepare_verification(
                                    bundle, source_id, repo.query_fingerprint(query),
                                )
                            ]
                            stage = "claims"
                            claim_plan = self._claim_plan(prepared_evidence)
                            stage = "identity"
                            identity_payload = self._identity_payload(prepared_evidence)
                            if not identity_payload:
                                raise RuntimeError("verification returned no evidence-backed identity")
                            resolved = resolver.resolve(
                                identity_payload,
                                bundles[0][0],
                                matching_hints={
                                    "display_name": candidate.company_name,
                                    "domain": candidate.domain,
                                    "country": candidate.country,
                                },
                            )
                            organization_id = resolved["organization_id"]
                            metrics["resolved_organizations"] += 1
                            self._checkpoint_candidate(
                                company_id, campaign_id, primary_partition,
                                candidate.source_record_id, "identified",
                            )
                            count_candidate_stage(metrics, "identified")
                            stage = "evidence"
                            repo.save_verification(prepared_evidence, campaign_id, organization_id)
                            self._persist_accepted_facts(
                                company_id, campaign_id, organization_id, prepared_evidence,
                            )
                            stage = "claims"
                            claims = self._save_claim_plan(
                                company_id, campaign_id, organization_id, claim_plan
                            )
                            stage = "eligibility"
                            # Coverage is computed here rather than at the verdict
                            # stage because the eligibility policy asks about it:
                            # `require_official_domain` and
                            # `minimum_independent_sources` are gates, and a gate
                            # cannot read a value produced two stages later.
                            official_domains = {
                                _domain(source.provenance_url)
                                for _, bundle in bundles for source in bundle.sources
                                if source.classification == "official" and _domain(source.provenance_url)
                            }
                            independent_domains = {
                                _domain(source.provenance_url)
                                for _, bundle in bundles for source in bundle.sources
                                if source.classification == "independent" and _domain(source.provenance_url)
                            }
                            # Standing is a property of the publisher, declared in
                            # the catalog, so it is read per source rather than per
                            # page. A registry domain is also whatever its pages
                            # classified as — these sets deliberately overlap.
                            registry_domains = {
                                _domain(source.provenance_url)
                                for source_id, bundle in bundles for source in bundle.sources
                                if self._is_registry(source_id) and _domain(source.provenance_url)
                            }
                            payload = self._candidate_payload(candidate, config)
                            # The corpus row is a starting guess about the role; the
                            # claims are what a source actually observed. Gating on
                            # the row alone rejected every company in a corpus that
                            # carries no buyer_types — which is most of them, since
                            # a contact list does not state one.
                            candidate_for_gate = {
                                **payload,
                                "buyer_types": list(dict.fromkeys([
                                    *(payload.get("buyer_types") or []),
                                    *_claimed_values(claims, "buyer_role"),
                                ])),
                                "organization_id": organization_id,
                                "official_domains": sorted(official_domains),
                                "independent_domain_count": len(independent_domains),
                                "lifecycle_status": next(
                                    iter(_claimed_values(claims, self.LIFECYCLE_FIELD)), None
                                ),
                            }
                            gate = eligibility.evaluate(candidate_for_gate, config)
                            self._checkpoint_candidate(
                                company_id, campaign_id, primary_partition,
                                candidate.source_record_id, "eligible",
                            )
                            count_candidate_stage(metrics, "eligible")

                            # Identity and a first eligibility assessment exist
                            # before deep work. Fresh accepted facts are reused
                            # before asking any structured or agentic source.
                            stage = "reuse"
                            reusable_facts = self._facts.reusable(
                                company_id,
                                organization_id,
                                set(SCHEMA_KNOWN_FACT_FIELDS),
                                now(),
                            )
                            self._checkpoint_candidate(
                                company_id, campaign_id, primary_partition,
                                candidate.source_record_id, "reused",
                            )
                            count_candidate_stage(metrics, "reused")

                            # Geography, closure, an explicit tenant exclusion,
                            # and unresolved identity are terminal for this run.
                            # Buyer role and source coverage are researchable
                            # gaps, so a preliminary fail on those does not prune.
                            hard_gate_reasons = {
                                "resolved_identity", "target_geography",
                                "lifecycle", "exclusion_list",
                            }
                            may_research = not hard_gate_reasons.intersection(gate.reasons)

                            stage = "structured"
                            if (
                                may_research
                                and config.enrichment.research_each_lead
                                and enriched < config.enrichment.max_companies
                                and not self._cancellation_requested(company_id, campaign_id)
                            ):
                                extra, still_missing, enrichment_requests = self._enrich_candidate(
                                    config,
                                    query,
                                    candidate,
                                    providers,
                                    available_source_ids,
                                    bundles,
                                    profile_version=frozen_profile,
                                    organization_id=organization_id,
                                    reusable_facts=reusable_facts,
                                )
                                provider_requests += enrichment_requests
                                if extra:
                                    enriched += 1
                                    bundles.extend(extra)
                                    for source_id, bundle in extra:
                                        partition = partitions[(source_id, country)]
                                        partition["enriched"] = partition.get("enriched", 0) + 1
                                        partition["evidence"] += len(bundle.sources)
                                    prepared_evidence = [
                                        stored
                                        for source_id, bundle in bundles
                                        for stored in repo.prepare_verification(
                                            bundle, source_id, repo.query_fingerprint(query),
                                        )
                                    ]
                                    repo.save_verification(
                                        prepared_evidence, campaign_id, organization_id,
                                    )
                                    self._persist_accepted_facts(
                                        company_id, campaign_id, organization_id,
                                        prepared_evidence,
                                    )
                                    # Claims are the current aggregate of all
                                    # accepted pages, not an append of stale
                                    # partial aggregates from the first pass.
                                    self.db.execute(
                                        "DELETE FROM feature_claims WHERE company_id=? "
                                        "AND campaign_id=? AND organization_id=?",
                                        (company_id, campaign_id, organization_id),
                                    )
                                    claims = self._save_claim_plan(
                                        company_id,
                                        campaign_id,
                                        organization_id,
                                        self._claim_plan(prepared_evidence),
                                    )
                                    reusable_facts = self._facts.reusable(
                                        company_id,
                                        organization_id,
                                        set(SCHEMA_KNOWN_FACT_FIELDS),
                                        now(),
                                    )
                                if still_missing:
                                    unresolved_gaps.update(still_missing)
                            self._checkpoint_candidate(
                                company_id, campaign_id, primary_partition,
                                candidate.source_record_id, "structured",
                                requests_started=provider_requests,
                            )
                            count_candidate_stage(metrics, "structured")

                            # Rebuild coverage and preliminary eligibility after
                            # structured research before deciding the remaining
                            # agentic gaps.
                            official_domains = {
                                _domain(source.provenance_url)
                                for _, bundle in bundles for source in bundle.sources
                                if source.classification == "official" and _domain(source.provenance_url)
                            }
                            independent_domains = {
                                _domain(source.provenance_url)
                                for _, bundle in bundles for source in bundle.sources
                                if source.classification == "independent" and _domain(source.provenance_url)
                            }
                            registry_domains = {
                                _domain(source.provenance_url)
                                for source_id, bundle in bundles for source in bundle.sources
                                if self._is_registry(source_id) and _domain(source.provenance_url)
                            }
                            candidate_for_gate.update({
                                "buyer_types": list(dict.fromkeys([
                                    *(payload.get("buyer_types") or []),
                                    *_claimed_values(claims, "buyer_role"),
                                ])),
                                "official_domains": sorted(official_domains),
                                "independent_domain_count": len(independent_domains),
                                "lifecycle_status": next(
                                    iter(_claimed_values(claims, self.LIFECYCLE_FIELD)), None
                                ),
                            })
                            gate = eligibility.evaluate(candidate_for_gate, config)
                            stage = "scoring"
                            preliminary_score = score_lead(
                                candidate_for_gate, claims, config.scoring, attainable,
                            )

                            stage = "agentic"
                            observed_fields = {claim.field for claim in claims if claim.status == "observed"}
                            lead_candidate, remaining_plan = self._agentic_gap_plan(
                                config,
                                candidate,
                                organization_id,
                                available_source_ids,
                                providers,
                                reusable_facts,
                                observed_fields,
                                frozen_profile,
                                fit_score=preliminary_score.fit_score,
                                priority_band=preliminary_score.priority_band,
                                qualifying_evidence=claims,
                            )
                            agentic_facts = []
                            agentic_ref = None
                            if (
                                may_research
                                and config.enrichment.enabled
                                and enriched < config.enrichment.max_companies
                                and not self._cancellation_requested(company_id, campaign_id)
                            ):
                                agentic_facts, agentic_ref = self._run_agentic_gap(
                                    company_id, campaign_id, lead_candidate,
                                    remaining_plan, config,
                                )
                                if agentic_ref is not None:
                                    enriched += 1
                                if agentic_facts:
                                    claims.extend(self._claims_from_stored_facts(
                                        company_id,
                                        campaign_id,
                                        organization_id,
                                        agentic_facts,
                                    ))
                            if self._cancellation_requested(company_id, campaign_id):
                                cancelled = True
                            self._checkpoint_candidate(
                                company_id, campaign_id, primary_partition,
                                candidate.source_record_id, "agentic",
                                requests_started=provider_requests,
                            )
                            count_candidate_stage(metrics, "agentic")

                            if agentic_facts:
                                for evidence in self.db.all(
                                    "SELECT source_id,provenance_url,payload FROM evidence_records "
                                    "WHERE company_id=? AND campaign_id=? AND organization_id=? "
                                    "AND source_record_id LIKE ?",
                                    (company_id, campaign_id, organization_id, "agentic:%"),
                                ):
                                    evidence_domain = _domain(evidence["provenance_url"] or "")
                                    if not evidence_domain:
                                        continue
                                    details = json_load(evidence["payload"], {})
                                    if details.get("classification") == "official":
                                        official_domains.add(evidence_domain)
                                    else:
                                        independent_domains.add(evidence_domain)
                                    if self._is_registry(evidence["source_id"]):
                                        registry_domains.add(evidence_domain)
                            candidate_for_gate.update({
                                "buyer_types": list(dict.fromkeys([
                                    *(payload.get("buyer_types") or []),
                                    *_claimed_values(claims, "buyer_role"),
                                ])),
                                "official_domains": sorted(official_domains),
                                "independent_domain_count": len(independent_domains),
                                "lifecycle_status": next(
                                    iter(_claimed_values(claims, self.LIFECYCLE_FIELD)), None
                                ),
                            })
                            gate = eligibility.evaluate(candidate_for_gate, config)
                            if gate.eligible:
                                metrics["eligible_companies"] += 1
                            else:
                                self._try_save_processing_issue(
                                    company_id, campaign_id, organization_id,
                                    "eligibility_failed", {"reasons": gate.reasons},
                                )
                            stage = "scoring"
                            score = score_lead(
                                candidate_for_gate, claims, config.scoring, attainable,
                            )
                            self._checkpoint_candidate(
                                company_id, campaign_id, primary_partition,
                                candidate.source_record_id, "scored",
                            )
                            count_candidate_stage(metrics, "scored")
                            stage = "verdict"
                            evaluated_verdict = evaluate_verdict(
                                candidate, claims, score, gate,
                                SourceCoverage(
                                    official_domains, independent_domains, registry_domains
                                ),
                            )
                            stage = "result"
                            source_ids = list(dict.fromkeys(source_id for source_id, _ in bundles))
                            fact_ids = list(dict.fromkeys([
                                *(fact.id for fact in reusable_facts),
                                *(fact.id for fact in agentic_facts),
                            ]))
                            evidence_ids = list(dict.fromkeys(
                                evidence_id
                                for claim in claims
                                for evidence_id in claim.evidence_ids
                            ))
                            snapshot_context = (
                                CampaignContext(
                                    company_id=company_id,
                                    campaign_id=campaign_id,
                                    profile_version=frozen_profile,
                                    config=config,
                                    scope=scope_snapshot,
                                )
                                if frozen_profile is not None else None
                            )
                            resolved_identity = ResolvedIdentity.model_validate(resolved)
                            frozen_snapshot = (
                                result_snapshot(
                                    snapshot_context,
                                    resolved_identity,
                                    score,
                                    fact_ids,
                                    evaluated_verdict,
                                )
                                if snapshot_context is not None else {}
                            )
                            result_data = ResearchResultData(
                                reasons=evaluated_verdict.reasons,
                                missing_evidence=evaluated_verdict.missing_evidence,
                                conflicting_claims=evaluated_verdict.conflicting_claims,
                                source_ids=source_ids,
                                official_domains=sorted(official_domains),
                                independent_domains=sorted(independent_domains),
                                score_dimensions=score.dimensions,
                                confidence_factors=score.confidence_factors,
                                profile_version_id=(
                                    frozen_profile.id if frozen_profile is not None else None
                                ),
                                scope=scope_snapshot,
                                playbook_versions=(
                                    frozen_profile.profile.playbook_versions
                                    if frozen_profile is not None else {}
                                ),
                                source_policy=config.enrichment.source_policy,
                                score=score.model_dump(mode="json"),
                                fact_ids=fact_ids,
                                evidence_ids=evidence_ids,
                                verdict_snapshot=frozen_snapshot.get("verdict", {}),
                            ).model_dump(mode="json")
                            previous = prior_results.get(organization_id)
                            result_identity = {
                                "result_id": previous["id"] if previous else None,
                                "created_at": previous["created_at"] if previous else None,
                            }
                            result_id = repo.upsert_result(
                                campaign_id=campaign_id,
                                organization_id=organization_id,
                                lead_id=None,
                                verdict=evaluated_verdict.kind,
                                fit_score=score.fit_score,
                                evidence_confidence=score.evidence_confidence,
                                data=result_data,
                                **result_identity,
                            )
                            if evaluated_verdict.kind in ACTIONABLE_VERDICTS:
                                stage = "lead"
                                lead_id = self._upsert_lead(
                                    company_id, campaign_id, organization_id, config,
                                    score, gate, evaluated_verdict, source_ids,
                                    sorted(official_domains | independent_domains), claims,
                                    lead_ids,
                                )
                                metrics["qualified_leads"] += 1
                                stage = "result"
                                result_id = repo.upsert_result(
                                    campaign_id=campaign_id,
                                    organization_id=organization_id,
                                    lead_id=lead_id,
                                    verdict=evaluated_verdict.kind,
                                    fit_score=score.fit_score,
                                    evidence_confidence=score.evidence_confidence,
                                    data=result_data,
                                    **result_identity,
                                )
                            if snapshot_context is not None:
                                self._append_score_snapshot(
                                    snapshot_context,
                                    resolved_identity,
                                    result_id,
                                    score,
                                    evaluated_verdict,
                                    fact_ids,
                                )
                            self._checkpoint_candidate(
                                company_id, campaign_id, primary_partition,
                                candidate.source_record_id, "materialized",
                            )
                            count_candidate_stage(metrics, "materialized")
                            for source_id, _ in bundles:
                                partitions[(source_id, country)]["completed"] += 1
                        except Exception as exc:
                            diagnostic = {
                                "candidate_source_record_id": candidate.source_record_id,
                                "stage": stage,
                                "message": str(exc)[:240],
                            }
                            if evaluated_verdict is not None:
                                diagnostic["evaluated_verdict"] = evaluated_verdict.kind
                            for source_id, _ in bundles:
                                partitions[(source_id, country)]["errors"].append(diagnostic)
                            self._try_save_processing_issue(
                                company_id, campaign_id, organization_id,
                                "candidate_processing_failed", diagnostic,
                            )
        except Exception as exc:
            processing_error = {
                "stage": "campaign_processing",
                "message": str(exc)[:240],
            }
            for partition in partitions.values():
                partition["errors"].append({
                    "candidate_source_record_id": None,
                    **processing_error,
                })
            self._try_save_processing_issue(
                company_id, campaign_id, None,
                "campaign_processing_failed", processing_error,
            )
        finally:
            # Before the metrics write, and on every path out including a
            # cancellation: a leaked pool holds threads for the process's life.
            if verify_pool is not None:
                verify_pool.shutdown(wait=True)

        # Outside FUNNEL_KEYS with excluded_closed:
        # enrichment adds evidence to records already counted, it never moves
        # one through a stage, and that funnel has to stay monotonic.
        metrics["enriched_companies"] = enriched
        # Outside FUNNEL_KEYS with the others: reuse changes what a stage cost,
        # never how many records passed through it. A run that says nothing
        # about reuse cannot be told apart from one that re-fetched everything.
        metrics["reused_bundles"] = reused_bundles
        # The number the 16,500-request estimate in the plan was guessing at.
        # A floor, not an exact figure: a verify that raises after spending
        # never returns its bundle, so its requests are not counted.
        metrics["provider_requests"] = provider_requests
        metrics["unresolved_gaps"] = sorted(unresolved_gaps)
        metrics["unmapped_markets"] = sorted(unmapped_markets)

        partition_statuses: list[str] = []
        failed_sources: set[str] = set()
        for (source_id, _country), partition in partitions.items():
            if not partition["available"]:
                status = "skipped"
                error_category = partition["reason"] or "unavailable"
                failed_sources.add(source_id)
            elif partition["errors"] and partition["completed"]:
                status = "partial"
                error_category = (
                    "verification_error"
                    if all(error.get("stage") == "verification" for error in partition["errors"])
                    else "candidate_processing_error"
                )
                failed_sources.add(source_id)
            elif partition["errors"]:
                status = "failed"
                error_category = (
                    "verification_error"
                    if all(error.get("stage") == "verification" for error in partition["errors"])
                    else "candidate_processing_error"
                )
                failed_sources.add(source_id)
            else:
                status = "succeeded"
                error_category = None
            partition_statuses.append(status)
            partition_metrics = {
                "selected_candidates": partition["selected"],
                "verified_candidates": partition["verified"],
                "enriched_candidates": partition.get("enriched", 0),
                "reused_candidates": partition.get("reused", 0),
                "provider_requests": partition.get("requests", 0),
                "completed_candidates": partition["completed"],
                "evidence_records": partition["evidence"],
                "errors": partition["errors"],
            }
            self.db.execute(
                "UPDATE campaign_partitions SET status=?,metrics=?,error_category=?,updated_at=? WHERE id=?",
                (status, json_dump(partition_metrics), error_category, now(), partition["id"]),
            )

        campaign_lead_ids = {
            result["lead_id"]
            for result in self.db.all(
                "SELECT lead_id FROM research_results WHERE company_id=? AND campaign_id=? AND lead_id IS NOT NULL",
                (company_id, campaign_id),
            )
        }
        metrics["contactable_leads"] = len({
            contact["lead_id"]
            for contact in self.db.all(
                "SELECT lead_id,email,phone,linkedin_url,do_not_contact FROM contacts WHERE company_id=?",
                (company_id,),
            )
            if contact["lead_id"] in campaign_lead_ids
            and not contact["do_not_contact"]
            and (contact["email"] or contact["phone"] or contact["linkedin_url"])
        })
        if cancelled:
            # Not "partial": a cancelled run stopped because it was told to,
            # and reporting a source failure would send someone to look for a
            # broken provider that was working fine.
            final_status = "cancelled"
        elif partition_statuses and all(status == "succeeded" for status in partition_statuses):
            final_status = "succeeded"
        elif any(status in {"succeeded", "partial"} for status in partition_statuses):
            final_status = "partial"
        else:
            final_status = "failed"
        explanation = zero_result_explanation(
            status=final_status,
            metrics=metrics,
            failed_source_ids=failed_sources,
            unmapped_markets=unmapped_markets,
        )
        metrics["zero_result_explanation"] = explanation
        CampaignMetricsRecorder(self.db, company_id, campaign_id).save(metrics, now())
        output = {
            "campaign_id": campaign_id,
            "metrics": metrics,
            "failed_source_ids": sorted(failed_sources),
            "zero_result_explanation": explanation,
        }
        self._finalize_terminal_state(
            company_id, campaign_id, run_id, final_status, output,
        )
        return {"status": final_status, "run_id": run_id, **output}
