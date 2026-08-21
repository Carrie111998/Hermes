"""Campaign orchestration over candidate, verification, and verdict contracts."""
from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

from ..db import json_dump, json_load, new_id, now
from .candidates import CandidateRecord, CandidateRepository
from .enrichment import FeaturePlanner, satisfied_playbook_fields
from .identity import IdentityResolver
from .metrics import CampaignMetricsRecorder, FUNNEL_KEYS, estimate_campaign
from .models import CampaignConfig, Claim, DiscoveryQuery, ResearchResultData
from .qualification import EligibilityService
from .registry import ProviderRegistry, build_registry
from .scoring import score_lead
from .sectors import load_sectors
from .storage import EvidenceRepository
from .verdicts import SourceCoverage, evaluate_verdict


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


class LeadResearchService:
    def __init__(self, db, registry: ProviderRegistry | None = None, *, workers: int = 2):
        self.db = db
        self.registry = registry or build_registry()
        self.candidates = CandidateRepository(db)
        self._planner = FeaturePlanner()
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

    def ensure_catalog(self, company_id: str) -> None:
        self.registry.ensure_tenant(self.db, company_id, now())

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

    def _claim_plan(self, prepared_evidence: list[dict]) -> list[dict]:
        """Derive bounded claim writes before any tenant identity is created."""
        facts: dict[str, list[dict]] = defaultdict(list)
        for stored in prepared_evidence:
            for field, values in stored["source"].facts.items():
                facts[field].append({**stored, "values": values})
        plan: list[dict] = []
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
            plan.append({
                "field": field,
                "value": value,
                "evidence_ids": list(dict.fromkeys(entry["evidence_id"] for entry in entries)),
                "source_ids": list(dict.fromkeys(entry["source_id"] for entry in entries)),
                "confidence": round(
                    sum(entry["confidence"] for entry in entries) / len(entries), 3
                ),
                "status": "conflicted" if conflicting else "observed",
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

    def _candidate_payload(self, candidate: CandidateRecord, config: CampaignConfig) -> dict:
        return {
            **candidate.data,
            "display_name": candidate.company_name,
            "country": candidate.country,
            "domain": candidate.domain,
            "buyer_types": candidate.data.get("buyer_types", []),
            "sector_ids": config.sector_ids,
        }

    def _enrichment_query(
        self, query: DiscoveryQuery, config: CampaignConfig
    ) -> DiscoveryQuery | None:
        """A second query aimed at what the first pass did not establish.

        The first pass searches the candidate's name against the campaign's own
        terms. This one searches it against the sector's vocabulary — the words
        a distributor actually puts on a page ("white goods", "private label",
        "distributor wanted") — so it reaches different pages rather than
        re-fetching the same ones at extra cost.
        """
        sectors = {sector.sector_id: sector for sector in load_sectors()}
        product: list[str] = []
        buyer: list[str] = []
        for sector_id in config.sector_ids:
            sector = sectors.get(sector_id)
            if sector is None:
                continue
            product.extend(sector.aliases)
            product.extend(sector.sourcing_terms)
            buyer.extend(sector.buyer_types)
        product = [term for term in dict.fromkeys(product) if term not in query.sector_ids]
        buyer = [term for term in dict.fromkeys(buyer) if term not in query.buyer_types]
        if not product and not buyer:
            # No sector vocabulary means no new search to run. Repeating the
            # first query would cost the same and return the same pages.
            return None
        return query.model_copy(update={
            "sector_ids": product or query.sector_ids,
            "buyer_types": buyer or query.buyer_types,
        })

    def _enrich_candidate(
        self,
        config: CampaignConfig,
        query: DiscoveryQuery,
        candidate: CandidateRecord,
        providers: dict,
        available_source_ids: list[str],
        bundles: list,
    ) -> tuple[list, list[str]]:
        """Re-verify a candidate against the gaps its first pass left open.

        Returns the extra bundles plus the playbook fields still missing after
        them, so a run can say what it looked for and whether it found it.
        """
        fact_fields = {
            field
            for _, bundle in bundles
            for source in bundle.sources
            for field in source.facts
        }
        satisfied = satisfied_playbook_fields(fact_fields)
        missing = [
            request.field
            for request in self._planner.missing_claims(
                {"claims": {field: True for field in satisfied}}, config.sector_ids
            )
        ]
        if not missing:
            return [], []

        gap_query = self._enrichment_query(query, config)
        if gap_query is None:
            return [], missing

        # Only ask sources whose answer can actually change with the terms.
        # TED retrieves by winner name and country, so a re-query returns the
        # same notices and costs a request to learn nothing; a web verifier
        # searches the terms and reaches different pages. `web_evidence` is
        # what separates the two, and it is already declared in the catalog.
        searchable = [
            source_id for source_id in available_source_ids
            if "web_evidence" in (self.registry.definitions[source_id].capabilities
                                  if source_id in self.registry.definitions else [])
        ]
        if not searchable:
            return [], missing

        seen = {
            source.provenance_url
            for _, bundle in bundles for source in bundle.sources
        }
        extra = []
        for source_id in searchable:
            try:
                bundle = providers[source_id].verify(gap_query, candidate)
            except Exception:
                # A failed enrichment must never lose the first pass's evidence.
                # The candidate keeps whatever it already had.
                continue
            if bundle.candidate_source_record_id != candidate.source_record_id:
                continue
            fresh = [source for source in bundle.sources if source.provenance_url not in seen]
            if not fresh:
                continue
            seen.update(source.provenance_url for source in fresh)
            extra.append((source_id, bundle.model_copy(update={"sources": fresh})))

        still_missing = [
            request.field
            for request in self._planner.missing_claims(
                {"claims": {
                    field: True for field in satisfied_playbook_fields(fact_fields | {
                        field
                        for _, bundle in extra
                        for source in bundle.sources
                        for field in source.facts
                    })
                }},
                config.sector_ids,
            )
        ]
        return extra, still_missing

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
        config: CampaignConfig,
        score,
        gate,
        verdict,
        source_ids: list[str],
        evidence_domains: list[str],
        claims: list[Claim],
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
            "verdict": verdict.kind,
            "score_dimensions": score.dimensions,
            "confidence_factors": score.confidence_factors,
            "eligibility": gate.gates,
            "applicable_feature_completeness": round(len(claims) / max(1, len(config.features)) * 100),
            "source_ids": source_ids,
            "top_evidence_sources": evidence_domains,
        }
        existing = self._find_lead(company_id, organization_id)
        lead_status = "qualified" if verdict.kind == "strong_fit" else "review"
        if existing:
            self.db.execute(
                "UPDATE leads SET company_name=?,website=?,country=?,status=?,data=?,updated_at=? WHERE id=?",
                (
                    organization["display_name"],
                    organization["domain"],
                    organization["country"],
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
                organization["display_name"],
                organization["domain"],
                organization["country"],
                lead_status,
                0,
                json_dump(lead_data),
                stamp,
                stamp,
            ),
        )
        return lead_id

    def run(self, company_id: str, campaign_id: str) -> dict:
        """Guarantee a terminalization attempt around all started-run work."""
        result: dict | None = None
        outer_error: Exception | None = None
        fallback_run_id: str | None = None
        fallback_output: dict = {"campaign_id": campaign_id, "metrics": {}, "failed_source_ids": []}
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
        if row["status"] == "cancelled":
            # Cancelled between queueing and pickup. Claiming it as `running`
            # first would lose the cancellation and research the whole corpus.
            return {"status": "cancelled", "run_id": None, "campaign_id": campaign_id,
                    "metrics": {}, "failed_source_ids": []}
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

        repo = EvidenceRepository(self.db, company_id)
        resolver = IdentityResolver(self.db, company_id)
        eligibility = EligibilityService()
        metrics = {key: 0 for key in FUNNEL_KEYS}
        partitions: dict[tuple[str, str], dict] = {}
        processing_error: dict | None = None
        # Bound before the try so a campaign that fails early still reports
        # zero enrichment rather than blowing up on the way out.
        enriched = 0
        unresolved_gaps: set[str] = set()
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
                    }
                    self.db.execute(
                        "INSERT INTO campaign_partitions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            partition_id, company_id, campaign_id, source_id, country,
                            config.sector_ids[0] if config.sector_ids else None,
                            "running", None, json_dump({}), None, now(),
                        ),
                    )

            product_terms = self._product_terms(company_id, config)
            settled, closed_count = self._settled_identities(company_id)
            # Outside FUNNEL_KEYS on purpose: the funnel is monotonic and this
            # describes work never started, not a stage that lost records.
            metrics["excluded_closed"] = closed_count
            for country in config.target_countries:
                if cancelled:
                    break
                candidates = self.candidates.select(
                    countries=[country],
                    product_terms=product_terms,
                    limit=config.max_qualified_leads_per_country * 3,
                    exclude=settled,
                )
                metrics["raw_records"] += len(candidates)
                metrics["named_candidates"] += len(candidates)
                for source_id in config.enabled_source_ids:
                    partitions[(source_id, country)]["selected"] = len(candidates)

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
                    if self._cancellation_requested(company_id, campaign_id):
                        # Before verification, so a cancel stops spending on the
                        # next candidate rather than the one after it.
                        cancelled = True
                        break
                    bundles: list[tuple[str, Any]] = []
                    verification_messages: list[str] = []
                    available_source_ids: list[str] = []
                    abstained: list[str] = []
                    for source_id in config.enabled_source_ids:
                        partition = partitions[(source_id, country)]
                        if not partition["available"]:
                            continue
                        available_source_ids.append(source_id)
                        try:
                            bundle = providers[source_id].verify(query, candidate)
                            if bundle.candidate_source_record_id != candidate.source_record_id:
                                raise ValueError("verifier returned evidence for a different candidate")
                            if not bundle.sources:
                                # An abstention, not a verification. A provider
                                # with nothing to say returns an empty bundle
                                # (a corpus row without a citation, say), and
                                # counting it as a bundle used to carry the
                                # candidate to the identity stage to die there
                                # on "no evidence-backed identity" — an
                                # internal-sounding error in place of the true
                                # one, which is that no source could vouch for
                                # this company.
                                abstained.append(source_id)
                                continue
                            bundles.append((source_id, bundle))
                            partition["verified"] += 1
                            partition["evidence"] += len(bundle.sources)
                        except Exception as exc:
                            message = str(exc)[:240]
                            verification_messages.append(message)
                            partition["errors"].append({
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

                    # A source match says the company exists and is roughly
                    # right. It does not say whether it is worth contacting, and
                    # the sector playbook knows what else to look for — so ask
                    # again, aimed at what is still unknown, before scoring.
                    if (config.enrichment.research_each_lead
                            and enriched < config.enrichment.max_companies):
                        extra, still_missing = self._enrich_candidate(
                            config, query, candidate, providers, available_source_ids, bundles,
                        )
                        if extra:
                            enriched += 1
                            bundles.extend(extra)
                            for source_id, bundle in extra:
                                partition = partitions[(source_id, country)]
                                partition["enriched"] = partition.get("enriched", 0) + 1
                                partition["evidence"] += len(bundle.sources)
                        if still_missing:
                            unresolved_gaps.update(still_missing)

                    organization_id: str | None = None
                    evaluated_verdict = None
                    stage = "evidence"
                    try:
                        stage = "evidence"
                        prepared_evidence = [
                            stored
                            for source_id, bundle in bundles
                            for stored in repo.prepare_verification(bundle, source_id)
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
                        stage = "evidence"
                        repo.save_verification(prepared_evidence, campaign_id, organization_id)
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
                        if gate.eligible:
                            metrics["eligible_companies"] += 1
                        else:
                            self._try_save_processing_issue(
                                company_id, campaign_id, organization_id,
                                "eligibility_failed", {"reasons": gate.reasons},
                            )
                        stage = "scoring"
                        score = score_lead(candidate_for_gate, claims, config.scoring)
                        stage = "verdict"
                        evaluated_verdict = evaluate_verdict(
                            candidate, claims, score, gate,
                            SourceCoverage(official_domains, independent_domains),
                        )
                        stage = "result"
                        source_ids = list(dict.fromkeys(source_id for source_id, _ in bundles))
                        result_data = ResearchResultData(
                            reasons=evaluated_verdict.reasons,
                            missing_evidence=evaluated_verdict.missing_evidence,
                            conflicting_claims=evaluated_verdict.conflicting_claims,
                            source_ids=source_ids,
                            official_domains=sorted(official_domains),
                            independent_domains=sorted(independent_domains),
                            score_dimensions=score.dimensions,
                            confidence_factors=score.confidence_factors,
                        ).model_dump(mode="json")
                        previous = prior_results.get(organization_id)
                        result_identity = {
                            "result_id": previous["id"] if previous else None,
                            "created_at": previous["created_at"] if previous else None,
                        }
                        repo.upsert_result(
                            campaign_id=campaign_id,
                            organization_id=organization_id,
                            lead_id=None,
                            verdict=evaluated_verdict.kind,
                            fit_score=score.fit_score,
                            evidence_confidence=score.evidence_confidence,
                            data=result_data,
                            **result_identity,
                        )
                        if evaluated_verdict.kind in {"strong_fit", "review"}:
                            stage = "lead"
                            lead_id = self._upsert_lead(
                                company_id, campaign_id, organization_id, config,
                                score, gate, evaluated_verdict, source_ids,
                                sorted(official_domains | independent_domains), claims,
                            )
                            metrics["qualified_leads"] += 1
                            stage = "result"
                            repo.upsert_result(
                                campaign_id=campaign_id,
                                organization_id=organization_id,
                                lead_id=lead_id,
                                verdict=evaluated_verdict.kind,
                                fit_score=score.fit_score,
                                evidence_confidence=score.evidence_confidence,
                                data=result_data,
                                **result_identity,
                            )
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

        # Outside FUNNEL_KEYS with excluded_closed:
        # enrichment adds evidence to records already counted, it never moves
        # one through a stage, and that funnel has to stay monotonic.
        metrics["enriched_companies"] = enriched
        metrics["unresolved_gaps"] = sorted(unresolved_gaps)

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
        CampaignMetricsRecorder(self.db, company_id, campaign_id).save(metrics, now())
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
        output = {
            "campaign_id": campaign_id,
            "metrics": metrics,
            "failed_source_ids": sorted(failed_sources),
        }
        self._finalize_terminal_state(
            company_id, campaign_id, run_id, final_status, output,
        )
        return {"status": final_status, "run_id": run_id, **output}
