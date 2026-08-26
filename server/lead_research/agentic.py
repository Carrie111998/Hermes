"""Durable, budgeted agentic research for unresolved weighted lead gaps."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from ..db import json_load, new_id, now
from .facts import FIELD_TTL_DAYS, FactRepository, FreshnessPolicy
from .models import (
    AgenticResearchBudget,
    AgenticResearchRequest,
    AgenticResearchResult,
    AgentRunRef,
    CampaignConfig,
    EvidenceEnvelope,
    LeadCandidate,
    ProposedFact,
    ResearchFact,
    ResearchGapPlan,
    ResearchPage,
    StoredFact,
    VerificationSource,
)
from .quotes import EvidenceRejected, accept_fact
from .storage import EvidenceRepository
from .verdicts import terminal_value

if TYPE_CHECKING:
    from ..agent_service import AgentRunService


# Agent output is not allowed to expand the persistence schema. These are the
# application fields used by identity, eligibility, scoring, display, contact,
# and the fixed sector playbooks. Pages may contain other prose; it remains in
# the immutable snapshot but cannot become an arbitrary database fact.
SCHEMA_KNOWN_FACT_FIELDS = frozenset({
    *FIELD_TTL_DAYS,
    "company_size", "buyer_channel_fit", "product_sector_fit", "commercial_scale",
    "trade_activity", "contactability", "facility_event", "private_label_fit",
    "facilities", "address", "description", "market_position", "trade_fair_presence",
})


def _stable_hash(value) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _terminal_candidate(candidate: LeadCandidate) -> str | None:
    for item in candidate.qualifying_evidence:
        if isinstance(item, dict):
            field, value = item.get("field"), item.get("value")
            status = item.get("status", "observed")
        else:
            field, value = getattr(item, "field", None), getattr(item, "value", None)
            status = getattr(item, "status", "observed")
        if field and status in {"observed", "conflicted"}:
            reason = terminal_value(str(field), value)
            if reason:
                return reason
    return None


class AgenticResearchService:
    def __init__(
        self,
        db,
        *,
        runs: AgentRunService,
        facts: FactRepository | None = None,
        freshness: FreshnessPolicy | None = None,
    ) -> None:
        self.db = db
        self.runs = runs
        self.facts = facts or FactRepository(db)
        self.freshness = freshness or FreshnessPolicy()

    def _campaign(self, company_id: str, campaign_id: str) -> tuple[dict, CampaignConfig]:
        row = self.db.one(
            "SELECT * FROM research_campaigns WHERE id=? AND company_id=?",
            (campaign_id, company_id),
        )
        if row is None:
            raise ValueError("research campaign not found for tenant")
        return dict(row), CampaignConfig.model_validate(json_load(row["config"], {}))

    def _organization_name(self, company_id: str, candidate: LeadCandidate) -> str:
        if candidate.display_name:
            return candidate.display_name
        row = self.db.one(
            "SELECT display_name FROM organizations WHERE id=? AND company_id=?",
            (candidate.organization_id, company_id),
        )
        if row is None:
            raise ValueError("lead organization not found for tenant")
        return row["display_name"]

    def _request(
        self,
        company_id: str,
        campaign_id: str,
        candidate: LeadCandidate,
        plan: ResearchGapPlan,
        decision_model: str,
        extractor_model: str | None,
    ) -> AgenticResearchRequest:
        _, campaign = self._campaign(company_id, campaign_id)
        batches = [batch for batch in plan.batches if batch.route == "agentic"]
        if not batches:
            raise ValueError("research gap plan has no agentic batch")
        return AgenticResearchRequest(
            campaign_id=campaign_id,
            organization_id=candidate.organization_id or "",
            company_name=self._organization_name(company_id, candidate),
            canonical_domain=candidate.domain,
            batches=batches,
            market_terms={"canonical": campaign.product_terms},
            decision_model=decision_model,
            extractor_model=extractor_model,
            budget=AgenticResearchBudget(
                page_limit=campaign.enrichment.max_pages_per_company,
                request_limit=12,
                time_limit_seconds=campaign.enrichment.max_seconds_per_company,
                token_limit=campaign.enrichment.max_tokens,
            ),
        )

    def enqueue(
        self,
        company_id: str,
        campaign_id: str,
        candidate: LeadCandidate,
        plan: ResearchGapPlan,
        decision_model: str,
        extractor_model: str | None = None,
    ) -> AgentRunRef:
        request = self._request(
            company_id, campaign_id, candidate, plan, decision_model, extractor_model,
        )
        fingerprint = _stable_hash({
            "company_id": company_id,
            "request": request.model_dump(mode="json"),
        })
        run = self.runs.create(
            company_id,
            "lead_research_gap",
            request.model_dump(mode="json"),
            idempotency_key=f"lead-research-gap:{fingerprint}",
        )
        return AgentRunRef(run_id=run["id"], status=run["status"])

    def enqueue_if_needed(
        self,
        company_id: str,
        campaign_id: str,
        candidate: LeadCandidate,
        plan: ResearchGapPlan,
    ) -> AgentRunRef | None:
        row, campaign = self._campaign(company_id, campaign_id)
        if row["status"] in {"cancelled", "failed", "succeeded"}:
            return None
        if _terminal_candidate(candidate):
            return None
        agentic_batches = [batch for batch in plan.batches if batch.route == "agentic"]
        if not agentic_batches or not campaign.enrichment.enabled:
            return None
        required_fields = {
            field
            for gap in plan.gaps
            if gap.required
            for field in gap.agentic_fields
        }
        if candidate.priority_band == "A" and not required_fields:
            return None
        # Deliberately no fit-score threshold: missing weighted evidence is a
        # reason to research, not a reason to cement an early low estimate.
        return self.enqueue(
            company_id,
            campaign_id,
            candidate,
            plan,
            decision_model=campaign.enrichment.model_profile or "",
            extractor_model=campaign.enrichment.extractor_model_profile,
        )

    @staticmethod
    def _fact_visibility(page: ResearchPage) -> tuple[str, str]:
        if page.source_class == "licensed":
            return "licensed", "licensed"
        if page.source_class == "customer":
            return "customer", "private"
        return page.source_class, page.visibility

    def _save_page_evidence(
        self,
        company_id: str,
        run_id: str,
        campaign_id: str | None,
        organization_id: str,
        page: ResearchPage,
        page_facts: list[ProposedFact],
    ) -> EvidenceEnvelope:
        if hashlib.sha256(page.snapshot_content.encode()).hexdigest() != page.raw_hash:
            raise EvidenceRejected("research page hash differs from immutable snapshot")
        existing = self.db.one(
            "SELECT id FROM dataset_snapshots WHERE company_id=? AND source_id=? AND raw_hash=?",
            (company_id, page.source_id, page.raw_hash),
        )
        snapshot_id = existing["id"] if existing else "snap_" + _stable_hash({
            "company_id": company_id, "source_id": page.source_id, "raw_hash": page.raw_hash,
        })[:20]
        evidence_id = "ev_" + _stable_hash({
            "company_id": company_id,
            "run_id": run_id,
            "page_id": page.page_id,
            "raw_hash": page.raw_hash,
        })[:20]
        facts: dict[str, list[str]] = {}
        fact_spans: dict[str, list[dict]] = {}
        for fact in page_facts:
            facts.setdefault(fact.field, []).append(str(fact.value_en))
            fact_spans.setdefault(fact.field, []).append(fact.span.model_dump(mode="json"))
        envelope = EvidenceEnvelope(
            evidence_id=evidence_id,
            source_id=page.source_id,
            source_record_id=f"agentic:{run_id}:{page.page_id}",
            snapshot_id=snapshot_id,
            record_type="company_signal",
            observed_at=page.observed_at,
            retrieved_at=page.retrieved_at,
            provenance_url=page.canonical_url,
            raw_hash=page.raw_hash,
            method="observed",
            confidence=max((fact.confidence for fact in page_facts), default=.75),
            snapshot_content=page.snapshot_content,
            source_language=page.source_language,
            archive_snapshot_at=page.archive_snapshot_at,
            payload={
                "organization_id": organization_id,
                "facts": facts,
                "fact_spans": fact_spans,
                "classification": (
                    "official" if page.source_class == "official" else "independent"
                ),
                "snapshot_content": page.snapshot_content,
                "source_language": page.source_language,
                "archive_snapshot_at": (
                    page.archive_snapshot_at.isoformat() if page.archive_snapshot_at else None
                ),
            },
        )
        source = VerificationSource(
            provenance_url=page.canonical_url,
            raw_hash=page.raw_hash,
            classification="official" if page.source_class == "official" else "independent",
            retrieved_via=page.canonical_url,
            facts=facts,
            snapshot_content=page.snapshot_content,
            fact_spans={
                field: [fact.span for fact in page_facts if fact.field == field]
                for field in facts
            },
            source_language=page.source_language,
            archive_snapshot_at=page.archive_snapshot_at,
            retrieved_at=page.retrieved_at.timestamp(),
        )
        EvidenceRepository(self.db, company_id).save_verification(
            [{
                "evidence_id": evidence_id,
                "source_id": page.source_id,
                "source": source,
                "confidence": envelope.confidence,
                "envelope": envelope,
            }],
            campaign_id,
            organization_id,
        )
        return envelope

    def _record_rejection(
        self,
        company_id: str,
        campaign_id: str | None,
        organization_id: str,
        fact: ProposedFact,
        reason: str,
    ) -> None:
        if campaign_id is None:
            return
        stamp = now()
        self.db.execute(
            "INSERT INTO research_issues("
            "id,company_id,campaign_id,organization_id,issue_type,status,data,created_at,updated_at"
            ") VALUES(?,?,?,?,?,'open',?,?,?)",
            (
                new_id("issue"), company_id, campaign_id, organization_id,
                "agentic_fact_rejected",
                json.dumps({"field": fact.field, "page_id": fact.page_id, "reason": reason}),
                stamp, stamp,
            ),
        )

    def _accept_result_payload(
        self,
        company_id: str,
        run_id: str,
        campaign_id: str | None,
        organization_id: str,
        result: AgenticResearchResult,
        *,
        allowed_fields: set[str] | None = None,
        strict: bool = False,
    ) -> list[StoredFact]:
        facts_by_page: dict[str, list[ProposedFact]] = {}
        for proposed in result.facts:
            facts_by_page.setdefault(proposed.page_id, []).append(proposed)

        stored: list[StoredFact] = []
        for page in result.pages:
            proposals = facts_by_page.get(page.page_id, [])
            try:
                envelope = self._save_page_evidence(
                    company_id, run_id, campaign_id,
                    organization_id, page, proposals,
                )
            except EvidenceRejected as exc:
                if strict:
                    raise
                for proposed in proposals:
                    self._record_rejection(
                        company_id, campaign_id, organization_id,
                        proposed, str(exc),
                    )
                continue
            source_class, visibility = self._fact_visibility(page)
            for proposed in proposals:
                if (
                    proposed.field not in SCHEMA_KNOWN_FACT_FIELDS
                    or allowed_fields is not None
                    and proposed.field not in allowed_fields
                ):
                    if strict:
                        raise ValueError(
                            f"research output field is outside the allowed target: {proposed.field}"
                        )
                    self._record_rejection(
                        company_id, campaign_id, organization_id,
                        proposed, "field is outside the research fact schema",
                    )
                    continue
                candidate = ResearchFact(
                    organization_id=organization_id,
                    campaign_id=campaign_id,
                    field=proposed.field,
                    value_en=proposed.value_en,
                    original_text=proposed.original_text,
                    source_language=proposed.source_language,
                    derivation_kind=proposed.derivation_kind,
                    period=proposed.period,
                    unit=proposed.unit,
                    currency=proposed.currency,
                    status=proposed.status,
                    confidence=proposed.confidence,
                    validation_basis=proposed.validation_basis,
                    evidence_id=envelope.evidence_id,
                    span=proposed.span,
                    source_class=source_class,
                    visibility=visibility,
                    mechanically_validated=False,
                    observed_at=proposed.observed_at,
                    retrieved_at=page.retrieved_at.timestamp(),
                    expires_at=page.retrieved_at.timestamp(),
                )
                try:
                    accepted = accept_fact(envelope, candidate)
                except EvidenceRejected as exc:
                    if strict:
                        raise
                    self._record_rejection(
                        company_id, campaign_id, organization_id,
                        proposed, str(exc),
                    )
                    continue
                accepted = accepted.model_copy(update={
                    "expires_at": self.freshness.expires_at(
                        accepted.field,
                        accepted.source_class,
                        accepted.observed_at,
                        accepted.retrieved_at,
                    ),
                })
                stored.append(self.facts.accept(company_id, accepted))
        return stored

    def accept_result(self, company_id: str, run_id: str) -> list[StoredFact]:
        run = self.runs.get(company_id, run_id)
        if run["run_type"] != "lead_research_gap":
            raise ValueError("run is not a lead research gap")
        if run["status"] != "succeeded" or run["output"] is None:
            raise ValueError("lead research gap result is not complete")
        request = AgenticResearchRequest.model_validate(run["payload"])
        result = AgenticResearchResult.model_validate(run["output"])
        return self._accept_result_payload(
            company_id,
            run_id,
            request.campaign_id,
            request.organization_id,
            result,
        )

    def accept_refresh_output(
        self,
        company_id: str,
        run_id: str,
        payload: dict,
        output: dict,
    ) -> list[StoredFact]:
        """Validate and persist one scheduler-owned stale-field refresh."""
        organization_id = str(payload.get("organization_id") or "")
        if self.db.one(
            "SELECT id FROM organizations WHERE id=? AND company_id=?",
            (organization_id, company_id),
        ) is None:
            raise ValueError("research refresh organization is outside the tenant")
        field = str(payload.get("field") or "")
        if field not in SCHEMA_KNOWN_FACT_FIELDS:
            raise ValueError("research refresh field is outside the fact schema")
        result = AgenticResearchResult.model_validate(output)
        budget = payload.get("budget") or {}
        if len(result.pages) > int(budget.get("page_limit", 0) or 0):
            raise ValueError("research refresh exceeded its page budget")
        if result.requests_started > int(budget.get("request_limit", 0) or 0):
            raise ValueError("research refresh exceeded its request budget")
        if result.tokens_used > int(budget.get("token_limit", 0) or 0):
            raise ValueError("research refresh exceeded its token budget")
        return self._accept_result_payload(
            company_id,
            run_id,
            payload.get("campaign_id"),
            organization_id,
            result,
            allowed_fields={field},
            strict=True,
        )
