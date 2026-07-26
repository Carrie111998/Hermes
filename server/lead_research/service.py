"""Campaign orchestration over deterministic provider and evidence contracts."""
from __future__ import annotations

from typing import Any

from ..db import json_dump, json_load, new_id, now
from ..quality import normalize_name
from .acquisition import CampaignRunner
from .identity import IdentityResolver
from .metrics import CampaignMetricsRecorder, FUNNEL_KEYS, estimate_campaign
from .models import CampaignConfig, Claim, DiscoveryQuery
from .qualification import EligibilityService
from .registry import ProviderRegistry, build_registry
from .scoring import score_lead
from .storage import EvidenceRepository


class LeadResearchService:
    def __init__(self, db, registry: ProviderRegistry | None = None):
        self.db = db
        self.registry = registry or build_registry()

    def ensure_catalog(self, company_id: str) -> None:
        self.registry.ensure_tenant(self.db, company_id, now())

    def catalog(self, company_id: str) -> list[dict]:
        self.ensure_catalog(company_id)
        rows = self.db.all(
            "SELECT * FROM dataset_definitions WHERE company_id=? ORDER BY source_id", (company_id,)
        )
        result = []
        for row in rows:
            definition = json_load(row["definition"], {})
            available = bool(row["installed"] and row["enabled"])
            reason = None
            if definition.get("health") == "retired":
                available, reason = False, "retired"
            elif definition.get("access_tier") in {"credentialed_public", "licensed"}:
                available, reason = False, "credential_required"
            elif definition.get("adapter_mode") in {"manual_import", "catalog_only"}:
                available, reason = False, "upload_or_adapter_required"
            result.append({
                **definition, "installed": bool(row["installed"]), "enabled": bool(row["enabled"]),
                "available": available, "unavailable_reason": reason, "health": row["health"],
                "last_checked_at": row["last_checked_at"],
            })
        return result

    def estimate(self, config: CampaignConfig):
        providers = [self.registry.get(source_id) for source_id in config.enabled_source_ids]
        return estimate_campaign(config, providers)

    def _find_lead(self, company_id: str, organization_id: str):
        for row in self.db.all("SELECT * FROM leads WHERE company_id=?", (company_id,)):
            if json_load(row["data"], {}).get("organization_id") == organization_id:
                return row
        return None

    def _save_claim(self, company_id: str, campaign_id: str, organization_id: str,
                    field: str, value: Any, evidence_id: str, source_id: str,
                    confidence: float = .9, period: str | None = "2025") -> Claim:
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        claim = Claim(
            field=field, value=value, period=period if numeric else None, status="observed",
            confidence=confidence, method="observed", evidence_ids=[evidence_id], applicability="useful",
        )
        self.db.execute(
            "INSERT INTO feature_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("claim"), company_id, campaign_id, organization_id, field, claim.status,
             json_dump(value), claim.confidence, claim.method, json_dump(claim.evidence_ids),
             json_dump({"source_ids": [source_id], "period": claim.period, "unit": claim.unit,
                        "currency": claim.currency, "applicability": claim.applicability}), now()),
        )
        return claim

    def run(self, company_id: str, campaign_id: str) -> dict:
        row = self.db.one(
            "SELECT * FROM research_campaigns WHERE id=? AND company_id=?", (campaign_id, company_id)
        )
        if not row:
            raise KeyError("campaign not found")
        config = CampaignConfig.model_validate(json_load(row["config"], {}))
        self.ensure_catalog(company_id)
        stamp = now()
        run_id = new_id("run")
        # Retry/refresh replaces campaign-owned derived state while immutable
        # snapshots and de-duplicated evidence remain reusable.
        for table in ("campaign_partitions", "campaign_metrics", "research_issues", "feature_claims"):
            self.db.execute(
                f"DELETE FROM {table} WHERE company_id=? AND campaign_id=?", (company_id, campaign_id)
            )
        self.db.execute(
            "UPDATE research_campaigns SET status='running',run_id=?,updated_at=? WHERE id=? AND company_id=?",
            (run_id, stamp, campaign_id, company_id),
        )
        self.db.execute(
            "INSERT INTO agent_runs(id,company_id,run_type,status,payload,output,error,output_ref,idempotency_key,"
            "cancellation_requested,cost,created_at,started_at,completed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, company_id, "lead_scan", "running", json_dump({"research_campaign_id": campaign_id}),
             None, None, campaign_id, None, 0, 0, stamp, stamp, None, stamp),
        )
        repo = EvidenceRepository(self.db, company_id)
        runner = CampaignRunner(self.registry, repo)
        resolver = IdentityResolver(self.db, company_id)
        eligibility = EligibilityService()
        metrics = {key: 0 for key in FUNNEL_KEYS}
        failed_sources: list[str] = []
        successful_sources: list[str] = []

        for source_id in config.enabled_source_ids:
            provider = self.registry.get(source_id)
            source_state = next(item for item in self.catalog(company_id) if item["source_id"] == source_id)
            for country in config.target_countries:
                partition_id = new_id("part")
                partition_stamp = now()
                self.db.execute(
                    "INSERT INTO campaign_partitions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (partition_id, company_id, campaign_id, source_id, country,
                     config.sector_ids[0] if config.sector_ids else None, "running", None,
                     json_dump({}), None, partition_stamp),
                )
                if not source_state["available"]:
                    self.db.execute(
                        "UPDATE campaign_partitions SET status='skipped',error_category=?,updated_at=? WHERE id=?",
                        (source_state["unavailable_reason"], now(), partition_id),
                    )
                    if source_id not in failed_sources:
                        failed_sources.append(source_id)
                    continue
                try:
                    query = DiscoveryQuery(
                        campaign_id=campaign_id, seller_countries=config.seller_countries,
                        target_countries=[country], sector_ids=config.sector_ids,
                        hs_codes=config.hs_codes, buyer_types=config.buyer_types,
                        max_records=config.max_qualified_leads_per_country * 3,
                    )
                    result = runner.run_partition(source_id, query)
                    evidence = result["evidence"]
                    metrics["raw_records"] += len(result["page"].records)
                    organizations: dict[str, str] = {}
                    claims_by_org: dict[str, list[Claim]] = {}
                    for item in evidence:
                        if item.record_type != "organization":
                            continue
                        metrics["named_candidates"] += 1
                        resolved = resolver.resolve(item.payload, source_id)
                        organization_id = resolved["organization_id"]
                        organizations[item.source_record_id] = organization_id
                        metrics["resolved_organizations"] += 1
                        candidate = {**item.payload, "organization_id": organization_id}
                        gate = eligibility.evaluate(candidate, config)
                        if not gate.eligible:
                            self.db.execute(
                                "INSERT INTO research_issues VALUES(?,?,?,?,?,?,?,?,?)",
                                (new_id("issue"), company_id, campaign_id, organization_id,
                                 "eligibility_failed", "open", json_dump({"reasons": gate.reasons}), now(), now()),
                            )
                            continue
                        metrics["eligible_companies"] += 1
                        claims = [self._save_claim(
                            company_id, campaign_id, organization_id, "resolved_identity", True,
                            item.evidence_id, source_id, item.confidence, None,
                        )]
                        for field in ("store_count", "locations", "brands_carried", "buying_intent"):
                            if item.payload.get(field) is not None:
                                claims.append(self._save_claim(
                                    company_id, campaign_id, organization_id, field, item.payload[field],
                                    item.evidence_id, source_id, item.confidence,
                                ))
                        claims_by_org[organization_id] = claims
                        score = score_lead(candidate, claims, config.scoring)
                        if score.priority_band == "Rejected":
                            continue
                        metrics["qualified_leads"] += 1
                        lead_data = {
                            "organization_id": organization_id, "research_campaign_id": campaign_id,
                            "industry": config.sector_ids[0] if config.sector_ids else None,
                            "buyer_type": (item.payload.get("buyer_types") or [None])[0],
                            "fit_score": score.fit_score, "evidence_confidence": score.evidence_confidence,
                            "priority_band": score.priority_band, "score_dimensions": score.dimensions,
                            "confidence_factors": score.confidence_factors, "eligibility": gate.gates,
                            "applicable_feature_completeness": round(len(claims) / max(1, len(config.features)) * 100),
                            "source_ids": [source_id], "top_evidence_sources": [provider.definition.display_name],
                        }
                        existing = self._find_lead(company_id, organization_id)
                        if existing:
                            self.db.execute(
                                "UPDATE leads SET company_name=?,website=?,country=?,status='qualified',data=?,updated_at=? WHERE id=?",
                                (item.payload["display_name"], item.payload.get("domain"), item.payload.get("country"),
                                 json_dump(lead_data), now(), existing["id"]),
                            )
                        else:
                            self.db.execute(
                                "INSERT INTO leads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (new_id("lead"), company_id, None, item.payload["display_name"],
                                 item.payload.get("domain"), item.payload.get("country"), "qualified", 0,
                                 json_dump(lead_data), now(), now()),
                            )
                    repo.save_evidence(evidence, campaign_id, organizations)
                    part_metrics = {
                        "records": len(result["page"].records), "normalized": len(evidence),
                        "named_candidates": len(organizations), "eligible": len(claims_by_org),
                    }
                    self.db.execute(
                        "UPDATE campaign_partitions SET status='succeeded',checkpoint=?,metrics=?,updated_at=? WHERE id=?",
                        (result["checkpoint"], json_dump(part_metrics), now(), partition_id),
                    )
                    if source_id not in successful_sources:
                        successful_sources.append(source_id)
                except Exception as exc:
                    self.db.execute(
                        "UPDATE campaign_partitions SET status='failed',error_category='provider_error',metrics=?,updated_at=? WHERE id=?",
                        (json_dump({"message": str(exc)[:240]}), now(), partition_id),
                    )
                    if source_id not in failed_sources:
                        failed_sources.append(source_id)

        campaign_lead_ids = {
            row["id"] for row in self.db.all("SELECT id,data FROM leads WHERE company_id=?", (company_id,))
            if json_load(row["data"], {}).get("research_campaign_id") == campaign_id
        }
        metrics["contactable_leads"] = len({
            row["lead_id"] for row in self.db.all(
                "SELECT lead_id,email,phone,linkedin_url,do_not_contact FROM contacts WHERE company_id=?",
                (company_id,),
            )
            if row["lead_id"] in campaign_lead_ids and not row["do_not_contact"]
            and (row["email"] or row["phone"] or row["linkedin_url"])
        })
        CampaignMetricsRecorder(self.db, company_id, campaign_id).save(metrics, now())
        final_status = "partial" if failed_sources and successful_sources else (
            "failed" if failed_sources and not successful_sources else "completed"
        )
        output = {"campaign_id": campaign_id, "metrics": metrics, "failed_source_ids": failed_sources}
        self.db.execute(
            "UPDATE research_campaigns SET status=?,updated_at=? WHERE id=? AND company_id=?",
            (final_status, now(), campaign_id, company_id),
        )
        self.db.execute(
            "UPDATE agent_runs SET status=?,output=?,completed_at=?,updated_at=? WHERE id=? AND company_id=?",
            ("succeeded" if successful_sources else "failed", json_dump(output), now(), now(), run_id, company_id),
        )
        return {"status": final_status, "run_id": run_id, **output}
