"""Campaign orchestration over candidate, verification, and verdict contracts."""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from ..db import json_dump, json_load, new_id, now
from .candidates import CandidateRecord, CandidateRepository
from .identity import IdentityResolver
from .metrics import CampaignMetricsRecorder, FUNNEL_KEYS, estimate_campaign
from .models import CampaignConfig, Claim, DiscoveryQuery, ResearchResultData
from .qualification import EligibilityService
from .registry import ProviderRegistry, build_registry
from .scoring import score_lead
from .storage import EvidenceRepository
from .verdicts import SourceCoverage, evaluate_verdict


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.")


class LeadResearchService:
    def __init__(self, db, registry: ProviderRegistry | None = None):
        self.db = db
        self.registry = registry or build_registry()
        self.candidates = CandidateRepository(db)

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
            provider = self.registry.get(row["source_id"])
            health = provider.health()
            available = bool(row["installed"] and row["enabled"])
            reason = None
            if definition.get("health") == "retired" or health.status == "retired":
                available, reason = False, "retired"
            elif health.status == "unavailable":
                available, reason = False, health.reason or "unavailable"
            elif not callable(getattr(provider, "verify", None)):
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

    def estimate(self, config: CampaignConfig):
        providers = [self.registry.get(source_id) for source_id in config.enabled_source_ids]
        return estimate_campaign(config, providers)

    def _find_lead(self, company_id: str, organization_id: str):
        for row in self.db.all("SELECT * FROM leads WHERE company_id=?", (company_id,)):
            if json_load(row["data"], {}).get("organization_id") == organization_id:
                return row
        return None

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
    ) -> Claim:
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        claim = Claim(
            field=field,
            value=value,
            period="2025" if numeric else None,
            status=status,
            confidence=confidence,
            method="observed",
            evidence_ids=evidence_ids,
            applicability="useful",
        )
        self.db.execute(
            "INSERT INTO feature_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
                }),
                now(),
            ),
        )
        return claim

    def _claims_from_evidence(
        self,
        company_id: str,
        campaign_id: str,
        organization_id: str,
        stored_evidence: list[dict],
    ) -> list[Claim]:
        facts: dict[str, list[dict]] = defaultdict(list)
        for stored in stored_evidence:
            for field, values in stored["source"].facts.items():
                facts[field].append({**stored, "values": values})
        claims: list[Claim] = []
        scalar_fields = {"company_name", "country", "domain"}
        for field in sorted(facts):
            entries = facts[field]
            values: list[Any] = []
            for entry in entries:
                for value in entry["values"]:
                    if value not in values:
                        values.append(value)
            if not values:
                continue
            conflicting = field in scalar_fields and len(values) > 1
            value: Any = values[0] if len(values) == 1 else values
            claims.append(self._save_claim(
                company_id,
                campaign_id,
                organization_id,
                field,
                value,
                list(dict.fromkeys(entry["evidence_id"] for entry in entries)),
                list(dict.fromkeys(entry["source_id"] for entry in entries)),
                round(sum(entry["confidence"] for entry in entries) / len(entries), 3),
                "conflicted" if conflicting else "observed",
            ))
        return claims

    def _candidate_payload(self, candidate: CandidateRecord, config: CampaignConfig) -> dict:
        return {
            **candidate.data,
            "display_name": candidate.company_name,
            "country": candidate.country,
            "domain": candidate.domain,
            "buyer_types": candidate.data.get("buyer_types", []),
            "sector_ids": config.sector_ids,
        }

    def _product_terms(self, company_id: str, config: CampaignConfig) -> list[str]:
        terms = [*config.sector_ids, *config.hs_codes]
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
        candidate: CandidateRecord,
        config: CampaignConfig,
        score,
        gate,
        verdict,
        source_ids: list[str],
        evidence_domains: list[str],
        claim_count: int,
    ) -> str:
        lead_data = {
            "organization_id": organization_id,
            "research_campaign_id": campaign_id,
            "industry": config.sector_ids[0] if config.sector_ids else None,
            "buyer_type": (candidate.data.get("buyer_types") or [None])[0],
            "fit_score": score.fit_score,
            "evidence_confidence": score.evidence_confidence,
            "priority_band": score.priority_band,
            "verdict": verdict.kind,
            "score_dimensions": score.dimensions,
            "confidence_factors": score.confidence_factors,
            "eligibility": gate.gates,
            "applicable_feature_completeness": round(claim_count / max(1, len(config.features)) * 100),
            "source_ids": source_ids,
            "top_evidence_sources": evidence_domains,
        }
        existing = self._find_lead(company_id, organization_id)
        lead_status = "qualified" if verdict.kind == "strong_fit" else "review"
        if existing:
            self.db.execute(
                "UPDATE leads SET company_name=?,website=?,country=?,status=?,data=?,updated_at=? WHERE id=?",
                (
                    candidate.company_name,
                    candidate.domain,
                    candidate.country,
                    lead_status,
                    json_dump(lead_data),
                    now(),
                    existing["id"],
                ),
            )
            return existing["id"]
        lead_id = new_id("lead")
        stamp = now()
        self.db.execute(
            "INSERT INTO leads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                lead_id,
                company_id,
                None,
                candidate.company_name,
                candidate.domain,
                candidate.country,
                lead_status,
                0,
                json_dump(lead_data),
                stamp,
                stamp,
            ),
        )
        return lead_id

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

        repo = EvidenceRepository(self.db, company_id)
        resolver = IdentityResolver(self.db, company_id)
        eligibility = EligibilityService()
        metrics = {key: 0 for key in FUNNEL_KEYS}
        catalog = {item["source_id"]: item for item in self.catalog(company_id)}
        providers = {source_id: self.registry.get(source_id) for source_id in config.enabled_source_ids}
        partitions: dict[tuple[str, str], dict] = {}
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
                    "evidence": 0,
                    "errors": [],
                }
                self.db.execute(
                    "INSERT INTO campaign_partitions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        partition_id,
                        company_id,
                        campaign_id,
                        source_id,
                        country,
                        config.sector_ids[0] if config.sector_ids else None,
                        "running",
                        None,
                        json_dump({}),
                        None,
                        now(),
                    ),
                )

        product_terms = self._product_terms(company_id, config)
        for country in config.target_countries:
            candidates = self.candidates.select(
                countries=[country],
                product_terms=product_terms,
                limit=config.max_qualified_leads_per_country * 3,
            )
            metrics["raw_records"] += len(candidates)
            metrics["named_candidates"] += len(candidates)
            for partition in (
                partitions[(source_id, country)] for source_id in config.enabled_source_ids
            ):
                partition["selected"] = len(candidates)

            query = DiscoveryQuery(
                campaign_id=campaign_id,
                seller_countries=config.seller_countries,
                target_countries=[country],
                sector_ids=config.sector_ids,
                hs_codes=config.hs_codes,
                buyer_types=config.buyer_types,
                max_records=config.max_qualified_leads_per_country * 3,
            )
            for candidate in candidates:
                bundles: list[tuple[str, Any]] = []
                for source_id in config.enabled_source_ids:
                    partition = partitions[(source_id, country)]
                    if not partition["available"]:
                        continue
                    try:
                        bundle = providers[source_id].verify(query, candidate)
                        if bundle.candidate_source_record_id != candidate.source_record_id:
                            raise ValueError("verifier returned evidence for a different candidate")
                        bundles.append((source_id, bundle))
                        partition["verified"] += 1
                        partition["evidence"] += len(bundle.sources)
                    except Exception as exc:
                        partition["errors"].append({
                            "candidate_source_record_id": candidate.source_record_id,
                            "message": str(exc)[:240],
                        })

                payload = self._candidate_payload(candidate, config)
                identity_source = bundles[0][0] if bundles else candidate.dataset_id
                resolved = resolver.resolve(payload, identity_source)
                organization_id = resolved["organization_id"]
                metrics["resolved_organizations"] += 1
                stored_evidence = [
                    stored
                    for source_id, bundle in bundles
                    for stored in repo.save_verification(
                        bundle, source_id, campaign_id, organization_id
                    )
                ]
                claims = self._claims_from_evidence(
                    company_id, campaign_id, organization_id, stored_evidence
                )
                candidate_for_gate = {**payload, "organization_id": organization_id}
                gate = eligibility.evaluate(candidate_for_gate, config)
                if gate.eligible:
                    metrics["eligible_companies"] += 1
                else:
                    issue_stamp = now()
                    self.db.execute(
                        "INSERT INTO research_issues VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            new_id("issue"),
                            company_id,
                            campaign_id,
                            organization_id,
                            "eligibility_failed",
                            "open",
                            json_dump({"reasons": gate.reasons}),
                            issue_stamp,
                            issue_stamp,
                        ),
                    )
                score = score_lead(candidate_for_gate, claims, config.scoring)
                official_domains = {
                    _domain(source.provenance_url)
                    for _, bundle in bundles
                    for source in bundle.sources
                    if source.classification == "official" and _domain(source.provenance_url)
                }
                independent_domains = {
                    _domain(source.provenance_url)
                    for _, bundle in bundles
                    for source in bundle.sources
                    if source.classification == "independent" and _domain(source.provenance_url)
                }
                coverage = SourceCoverage(official_domains, independent_domains)
                verdict = evaluate_verdict(candidate, claims, score, gate, coverage)
                source_ids = list(dict.fromkeys(source_id for source_id, _ in bundles))
                result_data = ResearchResultData(
                    reasons=verdict.reasons,
                    missing_evidence=verdict.missing_evidence,
                    conflicting_claims=verdict.conflicting_claims,
                    source_ids=source_ids,
                    official_domains=sorted(official_domains),
                    independent_domains=sorted(independent_domains),
                    score_dimensions=score.dimensions,
                    confidence_factors=score.confidence_factors,
                ).model_dump(mode="json")
                repo.upsert_result(
                    campaign_id=campaign_id,
                    organization_id=organization_id,
                    lead_id=None,
                    verdict=verdict.kind,
                    fit_score=score.fit_score,
                    evidence_confidence=score.evidence_confidence,
                    data=result_data,
                )
                if verdict.kind in {"strong_fit", "review"}:
                    lead_id = self._upsert_lead(
                        company_id,
                        campaign_id,
                        organization_id,
                        candidate,
                        config,
                        score,
                        gate,
                        verdict,
                        source_ids,
                        sorted(official_domains | independent_domains),
                        len(claims),
                    )
                    metrics["qualified_leads"] += 1
                    repo.upsert_result(
                        campaign_id=campaign_id,
                        organization_id=organization_id,
                        lead_id=lead_id,
                        verdict=verdict.kind,
                        fit_score=score.fit_score,
                        evidence_confidence=score.evidence_confidence,
                        data=result_data,
                    )

        partition_statuses: list[str] = []
        failed_sources: set[str] = set()
        for (source_id, _country), partition in partitions.items():
            if not partition["available"]:
                status = "skipped"
                error_category = partition["reason"] or "unavailable"
                failed_sources.add(source_id)
            elif partition["errors"] and partition["verified"]:
                status = "partial"
                error_category = "verification_error"
                failed_sources.add(source_id)
            elif partition["errors"]:
                status = "failed"
                error_category = "verification_error"
                failed_sources.add(source_id)
            else:
                status = "succeeded"
                error_category = None
            partition_statuses.append(status)
            partition_metrics = {
                "selected_candidates": partition["selected"],
                "verified_candidates": partition["verified"],
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
        CampaignMetricsRecorder(self.db, company_id, campaign_id).save(metrics, now())
        if all(status == "succeeded" for status in partition_statuses):
            final_status = "succeeded"
        elif any(status in {"succeeded", "partial"} for status in partition_statuses):
            final_status = "partial"
        else:
            final_status = "failed"
        output = {
            "campaign_id": campaign_id,
            "metrics": metrics,
            "failed_source_ids": sorted(failed_sources),
        }
        self.db.execute(
            "UPDATE research_campaigns SET status=?,updated_at=? WHERE id=? AND company_id=?",
            (final_status, now(), campaign_id, company_id),
        )
        self.db.execute(
            "UPDATE agent_runs SET status=?,output=?,completed_at=?,updated_at=? WHERE id=? AND company_id=?",
            (
                "failed" if final_status == "failed" else "succeeded",
                json_dump(output),
                now(),
                now(),
                run_id,
                company_id,
            ),
        )
        return {"status": final_status, "run_id": run_id, **output}
