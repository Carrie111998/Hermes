"""Promotion-safe shared and tenant research fact storage."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from ..db import json_dump, json_load, new_id, now
from ..quality import normalize_name
from .models import CorrectionImpact, EvidenceSpan, ResearchFact, StoredFact


FactPool = Literal["shared", "tenant"]
DAY_SECONDS = 86_400.0


@dataclass(frozen=True)
class DueResearchFact:
    id: str
    pool: FactPool
    company_id: str
    organization_id: str
    organization_name: str
    canonical_domain: str | None
    field: str
    value_en: object
    evidence_id: str
    expires_at: float
    campaign_id: str | None = None

    @property
    def refresh_key(self) -> str:
        # Accepted facts are immutable.  Including the expiry distinguishes a
        # future replacement/version while making repeated scheduler ticks for
        # this exact stale observation idempotent.
        return f"{self.pool}:{self.id}:{self.expires_at:.6f}"

# A fact's shelf life belongs to the field, not to the page or bundle that
# happened to carry it. Stable identity facts can outlive volatile intent and
# hiring signals from the same snapshot.
FIELD_TTL_DAYS = {
    "company_name": 3650, "domain": 3650, "registry_id": 3650, "country": 3650,
    "founded_year": 3650, "website": 365,
    "sector_ids": 1095, "hs_code": 1095, "product_term": 730, "product_fit": 730,
    "brands_carried": 365, "certifications": 365, "locations": 365,
    "countries_served": 365, "market_coverage": 365, "buyer_role": 365,
    "buyer_type": 365, "employee_count": 365, "store_count": 365,
    "revenue": 550, "market_cap": 180, "relevant_import_value": 550,
    "relevant_export_value": 550, "lifecycle_status": 180, "legal_status": 30,
    "email": 365, "phone": 365, "linkedin_url": 365, "tender": 30,
    "procurement_intent": 60, "sourcing_intent": 60, "buying_intent": 60,
    "procurement_signal": 90, "recent_hiring": 30,
}


class FreshnessPolicy:
    def __init__(
        self,
        default_ttl_days: int = 180,
        *,
        field_ttl_days: dict[str, int] | None = None,
        source_ttl_days: dict[str, int] | None = None,
    ) -> None:
        if default_ttl_days < 1:
            raise ValueError("default freshness must be at least one day")
        self.default_ttl_days = default_ttl_days
        self.field_ttl_days = {**FIELD_TTL_DAYS, **(field_ttl_days or {})}
        self.source_ttl_days = dict(source_ttl_days or {})

    def expires_at(
        self,
        field: str,
        source_class: str,
        observed_at: float | None,
        retrieved_at: float,
    ) -> float:
        basis = observed_at if observed_at is not None else retrieved_at
        ttl_days = self.field_ttl_days.get(
            field,
            self.source_ttl_days.get(source_class, self.default_ttl_days),
        )
        return basis + ttl_days * DAY_SECONDS


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _pool_for(fact: ResearchFact) -> FactPool:
    if (
        fact.visibility == "public"
        and fact.source_class in {"official", "registry"}
        and fact.mechanically_validated
    ):
        return "shared"
    return "tenant"


class FactRepository:
    def __init__(self, db) -> None:
        self.db = db

    def _tenant_organization(self, company_id: str, organization_id: str):
        row = self.db.one(
            "SELECT * FROM organizations WHERE id=? AND company_id=?",
            (organization_id, company_id),
        )
        if row is None:
            raise ValueError("fact organization is outside the tenant")
        return row

    def _shared_organization(self, company_id: str, organization_id: str) -> str:
        organization = self._tenant_organization(company_id, organization_id)
        try:
            linked = organization["shared_organization_id"]
        except (KeyError, IndexError):  # pragma: no cover - pre-migration adapters
            linked = None
        if linked and self.db.one("SELECT id FROM shared_organizations WHERE id=?", (linked,)):
            return linked
        data = json_load(organization["data"], {})
        domain = (organization["domain"] or "").strip().casefold() or None
        registry_id = str(data.get("registry_id") or "").strip() or None
        country = str(organization["country"] or data.get("country") or "").upper() or None
        shared = None
        if domain:
            shared = self.db.one("SELECT id FROM shared_organizations WHERE domain=?", (domain,))
        if shared is None and registry_id:
            shared = self.db.one(
                "SELECT id FROM shared_organizations WHERE country=? AND registry_id=?",
                (country, registry_id),
            )
        normalized = organization["normalized_name"] or normalize_name(organization["display_name"])
        if shared is None:
            shared = self.db.one(
                "SELECT id FROM shared_organizations WHERE normalized_name=? "
                "AND COALESCE(country,'')=COALESCE(?, '')",
                (normalized, country),
            )
        stamp = now()
        if shared is None:
            identity = domain or f"{country or ''}:{registry_id or ''}:{normalized}"
            shared_id = "sorg_" + _hash(identity)[:20]
            self.db.execute(
                "INSERT INTO shared_organizations("
                "id,display_name,normalized_name,country,domain,registry_id,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (shared_id, organization["display_name"], normalized, country, domain,
                 registry_id, stamp, stamp),
            )
        else:
            shared_id = shared["id"]
        self.db.execute(
            "UPDATE organizations SET shared_organization_id=? WHERE id=? AND company_id=?",
            (shared_id, organization_id, company_id),
        )
        return shared_id

    def _consume(self, company_id: str, shared_fact_id: str, stamp: float) -> None:
        row = self.db.one(
            "SELECT shared_fact_id FROM research_fact_consumers "
            "WHERE company_id=? AND shared_fact_id=?",
            (company_id, shared_fact_id),
        )
        if row:
            self.db.execute(
                "UPDATE research_fact_consumers SET last_used_at=? "
                "WHERE company_id=? AND shared_fact_id=?",
                (stamp, company_id, shared_fact_id),
            )
        else:
            self.db.execute(
                "INSERT INTO research_fact_consumers("
                "company_id,shared_fact_id,first_used_at,last_used_at) VALUES(?,?,?,?)",
                (company_id, shared_fact_id, stamp, stamp),
            )

    def _shared_evidence(self, company_id: str, fact: ResearchFact) -> str:
        tenant_evidence = self.db.one(
            "SELECT source_id,provenance_url,raw_hash FROM evidence_records "
            "WHERE id=? AND company_id=?",
            (fact.evidence_id, company_id),
        )
        evidence_material = {
            "source_id": tenant_evidence["source_id"] if tenant_evidence else None,
            "provenance_url": tenant_evidence["provenance_url"] if tenant_evidence else None,
            "raw_hash": tenant_evidence["raw_hash"] if tenant_evidence else None,
            "source_class": fact.source_class,
            "visibility": fact.visibility,
            "source_language": fact.source_language,
            "original_text": fact.original_text,
            "span": fact.span.model_dump(mode="json"),
        }
        content_hash = _hash(evidence_material)
        existing = self.db.one(
            "SELECT id FROM shared_evidence_records WHERE content_hash=?", (content_hash,)
        )
        if existing:
            return existing["id"]
        evidence_id = "sev_" + content_hash[:20]
        stamp = now()
        self.db.execute(
            "INSERT INTO shared_evidence_records("
            "id,source_id,provenance_url,raw_hash,source_class,visibility,source_language,"
            "original_text,span_start,span_end,content_hash,retrieved_at,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                evidence_id,
                tenant_evidence["source_id"] if tenant_evidence else None,
                tenant_evidence["provenance_url"] if tenant_evidence else None,
                tenant_evidence["raw_hash"] if tenant_evidence else None,
                fact.source_class,
                fact.visibility,
                fact.source_language,
                fact.original_text,
                fact.span.start,
                fact.span.end,
                content_hash,
                fact.retrieved_at,
                stamp,
            ),
        )
        return evidence_id

    def accept(self, company_id: str, fact: ResearchFact) -> StoredFact:
        fact = ResearchFact.model_validate(fact)
        self._tenant_organization(company_id, fact.organization_id)
        if _pool_for(fact) == "shared":
            return self._accept_shared(company_id, fact)
        return self._accept_tenant(company_id, fact)

    def _accept_shared(self, company_id: str, fact: ResearchFact) -> StoredFact:
        shared_organization_id = self._shared_organization(company_id, fact.organization_id)
        shared_evidence_id = self._shared_evidence(company_id, fact)
        value_hash = _hash(fact.value_en)
        identity_hash = _hash({
            "organization_id": shared_organization_id,
            "field": fact.field,
            "value_hash": value_hash,
            "evidence_id": shared_evidence_id,
        })
        fact_id = "sf_" + identity_hash[:20]
        existing = self.db.one("SELECT id FROM shared_facts WHERE id=?", (fact_id,))
        stamp = now()
        if not existing:
            self.db.execute(
                "INSERT INTO shared_facts("
                "id,organization_id,field,value_en,value_hash,primary_evidence_id,"
                "derivation_kind,period,unit,currency,status,confidence,validation_basis,"
                "source_class,visibility,mechanically_validated,observed_at,retrieved_at,"
                "expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact_id, shared_organization_id, fact.field, json_dump(fact.value_en),
                    value_hash, shared_evidence_id, fact.derivation_kind, fact.period,
                    fact.unit, fact.currency, fact.status, fact.confidence,
                    fact.validation_basis, fact.source_class, fact.visibility,
                    int(fact.mechanically_validated), fact.observed_at, fact.retrieved_at,
                    fact.expires_at, stamp, stamp,
                ),
            )
            self.db.execute(
                "INSERT INTO shared_fact_evidence(fact_id,evidence_id) VALUES(?,?)",
                (fact_id, shared_evidence_id),
            )
        self._consume(company_id, fact_id, stamp)
        self.db.execute(
            "UPDATE evidence_records SET shared_evidence_id=? WHERE id=? AND company_id=?",
            (shared_evidence_id, fact.evidence_id, company_id),
        )
        return StoredFact.model_validate({
            **fact.model_dump(),
            "id": fact_id,
            "pool": "shared",
            "company_id": None,
            "shared_organization_id": shared_organization_id,
            "evidence_id": shared_evidence_id,
        })

    def _accept_tenant(self, company_id: str, fact: ResearchFact) -> StoredFact:
        value_hash = _hash(fact.value_en)
        existing = self.db.one(
            "SELECT id FROM tenant_facts WHERE company_id=? AND organization_id=? "
            "AND field=? AND value_hash=? AND evidence_id=?",
            (company_id, fact.organization_id, fact.field, value_hash, fact.evidence_id),
        )
        stamp = now()
        fact_id = existing["id"] if existing else new_id("tf")
        if not existing:
            self.db.execute(
                "INSERT INTO tenant_facts("
                "id,company_id,campaign_id,organization_id,field,value_en,value_hash,"
                "original_text,source_language,derivation_kind,period,unit,currency,status,"
                "confidence,validation_basis,evidence_id,span_start,span_end,source_class,"
                "visibility,mechanically_validated,observed_at,retrieved_at,expires_at,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact_id, company_id, fact.campaign_id, fact.organization_id, fact.field,
                    json_dump(fact.value_en), value_hash, fact.original_text,
                    fact.source_language, fact.derivation_kind, fact.period, fact.unit,
                    fact.currency, fact.status, fact.confidence, fact.validation_basis,
                    fact.evidence_id, fact.span.start, fact.span.end, fact.source_class,
                    fact.visibility, int(fact.mechanically_validated), fact.observed_at,
                    fact.retrieved_at, fact.expires_at, stamp, stamp,
                ),
            )
        return StoredFact.model_validate({
            **fact.model_dump(), "id": fact_id, "pool": "tenant", "company_id": company_id,
        })

    @staticmethod
    def _tenant_fact(row) -> StoredFact:
        return StoredFact(
            id=row["id"], pool="tenant", company_id=row["company_id"],
            organization_id=row["organization_id"], campaign_id=row["campaign_id"],
            field=row["field"], value_en=json_load(row["value_en"]),
            original_text=row["original_text"], source_language=row["source_language"],
            derivation_kind=row["derivation_kind"], period=row["period"], unit=row["unit"],
            currency=row["currency"], status=row["status"], confidence=row["confidence"],
            validation_basis=row["validation_basis"], evidence_id=row["evidence_id"],
            span=EvidenceSpan(original=row["original_text"], start=row["span_start"], end=row["span_end"]),
            source_class=row["source_class"], visibility=row["visibility"],
            mechanically_validated=bool(row["mechanically_validated"]),
            observed_at=row["observed_at"], retrieved_at=row["retrieved_at"],
            expires_at=row["expires_at"],
        )

    @staticmethod
    def _shared_fact(row, tenant_organization_id: str) -> StoredFact:
        return StoredFact(
            id=row["id"], pool="shared", company_id=None,
            organization_id=tenant_organization_id,
            shared_organization_id=row["organization_id"], campaign_id=None,
            field=row["field"], value_en=json_load(row["value_en"]),
            original_text=row["original_text"], source_language=row["source_language"],
            derivation_kind=row["derivation_kind"], period=row["period"], unit=row["unit"],
            currency=row["currency"], status=row["status"], confidence=row["confidence"],
            validation_basis=row["validation_basis"], evidence_id=row["evidence_id"],
            span=EvidenceSpan(original=row["original_text"], start=row["span_start"], end=row["span_end"]),
            source_class=row["source_class"], visibility=row["visibility"],
            mechanically_validated=bool(row["mechanically_validated"]),
            observed_at=row["observed_at"], retrieved_at=row["retrieved_at"],
            expires_at=row["expires_at"],
        )

    def reusable(
        self,
        company_id: str,
        organization_id: str,
        fields: set[str],
        at: float,
    ) -> list[StoredFact]:
        if not fields:
            return []
        organization = self._tenant_organization(company_id, organization_id)
        placeholders = ",".join("?" for _ in fields)
        field_values = tuple(sorted(fields))
        tenant_rows = self.db.all(
            "SELECT * FROM tenant_facts WHERE company_id=? AND organization_id=? "
            f"AND field IN ({placeholders}) AND status='observed' AND expires_at>? "
            "ORDER BY field,id",
            (company_id, organization_id, *field_values, at),
        )
        facts = [self._tenant_fact(row) for row in tenant_rows]
        shared_id = organization["shared_organization_id"]
        if not shared_id:
            # A second tenant may encounter the same public identity before it
            # has accepted a fact itself. Resolve its guarded domain/registry
            # mapping now so shared facts become visible without copying them.
            shared_id = self._shared_organization(company_id, organization_id)
        shared_rows = self.db.all(
            "SELECT f.*,e.id AS evidence_id,e.source_language,e.original_text,"
            "e.span_start,e.span_end FROM shared_facts f "
            "JOIN shared_evidence_records e ON e.id=f.primary_evidence_id "
            f"WHERE f.organization_id=? AND f.field IN ({placeholders}) "
            "AND f.status='observed' AND f.expires_at>? ORDER BY f.field,f.id",
            (shared_id, *field_values, at),
        )
        stamp = now()
        for row in shared_rows:
            facts.append(self._shared_fact(row, organization_id))
            self._consume(company_id, row["id"], stamp)
        return facts

    def due_for_refresh(self, at: float, limit: int) -> list[DueResearchFact]:
        """Return a bounded tenant-scoped queue of stale, previously used facts.

        Shared-fact consumption is explicit in ``research_fact_consumers``.
        Tenant facts count as consumed when they were accepted for a campaign;
        ad-hoc facts with no campaign never trigger background work.
        Each pool is limited before rows enter Python, keeping the scheduler's
        database work bounded even when a deployment has years of stale data.
        """
        if limit <= 0:
            return []
        shared_rows = self.db.all(
            "SELECT f.id,f.field,f.value_en,f.primary_evidence_id AS evidence_id,"
            "f.expires_at,c.company_id,o.id AS tenant_organization_id,"
            "o.display_name,o.domain "
            "FROM shared_facts f JOIN research_fact_consumers c ON c.shared_fact_id=f.id "
            "JOIN organizations o ON o.company_id=c.company_id "
            "AND o.shared_organization_id=f.organization_id "
            "WHERE f.status='observed' AND f.expires_at<=? "
            "ORDER BY f.expires_at,f.id,c.company_id LIMIT ?",
            (at, limit),
        )
        tenant_rows = self.db.all(
            "SELECT f.id,f.company_id,f.campaign_id,f.organization_id,f.field,f.value_en,"
            "f.evidence_id,f.expires_at,o.display_name,o.domain "
            "FROM tenant_facts f JOIN organizations o ON o.id=f.organization_id "
            "AND o.company_id=f.company_id "
            "WHERE f.status='observed' AND f.campaign_id IS NOT NULL AND f.expires_at<=? "
            "ORDER BY f.expires_at,f.id LIMIT ?",
            (at, limit),
        )
        due = [
            DueResearchFact(
                id=row["id"], pool="shared", company_id=row["company_id"],
                organization_id=row["tenant_organization_id"],
                organization_name=row["display_name"], canonical_domain=row["domain"],
                field=row["field"], value_en=json_load(row["value_en"]),
                evidence_id=row["evidence_id"], expires_at=row["expires_at"],
            )
            for row in shared_rows
        ]
        due.extend(
            DueResearchFact(
                id=row["id"], pool="tenant", company_id=row["company_id"],
                organization_id=row["organization_id"],
                organization_name=row["display_name"], canonical_domain=row["domain"],
                field=row["field"], value_en=json_load(row["value_en"]),
                evidence_id=row["evidence_id"], expires_at=row["expires_at"],
                campaign_id=row["campaign_id"],
            )
            for row in tenant_rows
        )
        return sorted(
            due, key=lambda fact: (fact.expires_at, fact.company_id, fact.id),
        )[:limit]

    def relevance(
        self,
        company_id: str,
        candidate,
        product_terms: list[str],
        at: float | None = None,
    ) -> list[str]:
        """Validated reusable evidence connecting a candidate to this scope.

        This is deliberately read-only and resolves identity by guarded domain
        or name+country lookup. Creating the tenant organization still happens
        only after the cheap gate passes.
        """
        from .candidates import matches_term, searchable_term

        terms = [searchable_term(term) for term in product_terms if str(term).strip()]
        if not terms:
            return []
        stamp = now() if at is None else at
        relevant_fields = (
            "product_term", "product_fit", "product_sector_fit", "sector_ids", "hs_code",
        )
        placeholders = ",".join("?" for _ in relevant_fields)

        def supported(value) -> bool:
            values = value if isinstance(value, list) else [value]
            haystack = searchable_term(" ".join(str(item) for item in values if item is not None))
            return any(
                matches_term(term, haystack) or matches_term(haystack, term)
                for term in terms
                if haystack
            )

        tenant_params: list = [company_id]
        tenant_identity = "normalized_name=? AND COALESCE(country,'')=?"
        tenant_params.extend([candidate.normalized_name, candidate.country.upper()])
        if candidate.domain:
            tenant_identity = "(domain=? OR (normalized_name=? AND COALESCE(country,'')=?))"
            tenant_params = [
                company_id, candidate.domain.casefold(), candidate.normalized_name,
                candidate.country.upper(),
            ]
        tenant_rows = self.db.all(
            "SELECT f.value_en,f.evidence_id FROM tenant_facts f JOIN organizations o "
            "ON o.id=f.organization_id AND o.company_id=f.company_id "
            f"WHERE f.company_id=? AND {tenant_identity} AND f.field IN ({placeholders}) "
            "AND f.status='observed' AND f.expires_at>?",
            (*tenant_params, *relevant_fields, stamp),
        )

        shared_params: list = []
        shared_identity = "normalized_name=? AND COALESCE(country,'')=?"
        shared_params.extend([candidate.normalized_name, candidate.country.upper()])
        if candidate.domain:
            shared_identity = "(domain=? OR (normalized_name=? AND COALESCE(country,'')=?))"
            shared_params = [
                candidate.domain.casefold(), candidate.normalized_name, candidate.country.upper(),
            ]
        shared_rows = self.db.all(
            "SELECT f.value_en,f.primary_evidence_id AS evidence_id FROM shared_facts f "
            "JOIN shared_organizations o ON o.id=f.organization_id "
            f"WHERE {shared_identity} AND f.field IN ({placeholders}) "
            "AND f.status='observed' AND f.expires_at>?",
            (*shared_params, *relevant_fields, stamp),
        )
        return list(dict.fromkeys(
            row["evidence_id"]
            for row in [*tenant_rows, *shared_rows]
            if supported(json_load(row["value_en"]))
        ))

    def consumers(self, fact_id: str) -> CorrectionImpact:
        if fact_id.startswith("sf_"):
            companies = [
                row["company_id"] for row in self.db.all(
                    "SELECT company_id FROM research_fact_consumers WHERE shared_fact_id=?",
                    (fact_id,),
                )
            ]
            exists = self.db.one("SELECT id FROM shared_facts WHERE id=?", (fact_id,))
        else:
            row = self.db.one(
                "SELECT company_id FROM tenant_facts WHERE id=?", (fact_id,),
            )
            companies = [row["company_id"]] if row else []
            exists = row
        if exists is None:
            raise ValueError("research fact not found")
        result_ids: set[str] = set()
        if companies:
            placeholders = ",".join("?" for _ in companies)
            rows = self.db.all(
                "SELECT result_id,snapshot_json FROM research_score_snapshots "
                f"WHERE company_id IN ({placeholders}) ORDER BY created_at",
                tuple(companies),
            )
            for row in rows:
                if fact_id in (json_load(row["snapshot_json"], {}).get("fact_ids") or []):
                    result_ids.add(row["result_id"])
        lead_ids = [
            row["lead_id"]
            for result_id in sorted(result_ids)
            if (row := self.db.one(
                "SELECT lead_id FROM research_results WHERE id=? AND lead_id IS NOT NULL",
                (result_id,),
            )) is not None
        ]
        return CorrectionImpact(
            fact_id=fact_id,
            result_ids=sorted(result_ids),
            lead_ids=list(dict.fromkeys(lead_ids)),
        )

    def _claims_for_snapshot_dimension(
        self,
        company_id: str,
        organization_id: str,
        fact_ids: list[str],
        dimension: str,
    ) -> list:
        """Rehydrate the accepted facts behind one frozen score dimension.

        Score snapshots deliberately keep fact ids rather than mutable claim
        rows.  A correction can therefore rebuild just the affected dimension
        from its accepted inputs while every prior snapshot remains untouched.
        """
        from .models import Claim
        from .scoring import DIMENSION_CLAIM_FIELDS

        relevant_fields = set(DIMENSION_CLAIM_FIELDS[dimension])
        claims: list[Claim] = []
        for fact_id in dict.fromkeys(fact_ids):
            if fact_id.startswith("sf_"):
                row = self.db.one(
                    "SELECT f.*,f.primary_evidence_id AS evidence_id "
                    "FROM shared_facts f JOIN organizations o "
                    "ON o.shared_organization_id=f.organization_id "
                    "WHERE f.id=? AND o.id=? AND o.company_id=?",
                    (fact_id, organization_id, company_id),
                )
            else:
                row = self.db.one(
                    "SELECT * FROM tenant_facts WHERE id=? AND organization_id=? "
                    "AND company_id=?",
                    (fact_id, organization_id, company_id),
                )
            if row is None or row["field"] not in relevant_fields:
                continue
            status = row["status"]
            if status not in {"observed", "calculated", "estimated_range", "conflicted"}:
                continue
            claims.append(Claim(
                field=row["field"],
                value=json_load(row["value_en"]),
                period=row["period"],
                unit=row["unit"],
                currency=row["currency"],
                status=status,
                confidence=row["confidence"],
                # Stored facts reached this pool through observed evidence;
                # derivation_kind describes translation/calculation lineage,
                # not a permission to detach the value from its cited page.
                method="observed",
                evidence_ids=[row["evidence_id"]],
                validated=(
                    bool(row["mechanically_validated"])
                    and row["source_class"] in {"official", "registry"}
                ),
                observed_at=row["observed_at"],
            ))
        return claims

    def _recompute_snapshot_score(self, result, snapshot: dict, fact_field: str):
        """Return a new current score when ``fact_field`` contributes to fit."""
        from .models import LeadScore, ScoringProfile, ScoringWeights
        from .scoring import (
            DIMENSION_CLAIM_FIELDS,
            derive_dimension_scores,
            dimension_evidence_ids,
        )

        current = LeadScore.model_validate(snapshot.get("score") or {})
        field_dimension = {
            field: dimension
            for dimension, fields in DIMENSION_CLAIM_FIELDS.items()
            for field in fields
        }
        dimension = field_dimension.get(fact_field)
        if dimension is None:
            return current
        claims = self._claims_for_snapshot_dimension(
            result["company_id"],
            result["organization_id"],
            list(snapshot.get("fact_ids") or []),
            dimension,
        )
        dimensions = {
            **current.dimensions,
            dimension: derive_dimension_scores(claims)[dimension],
        }
        weights = ScoringWeights.model_validate(
            snapshot.get("weights") or {},
        ).model_dump()
        not_applicable = set(current.not_applicable_dimensions)
        unknown_dimensions = {
            name: weight
            for name, weight in weights.items()
            if weight > 0 and name not in not_applicable and dimensions.get(name) is None
        }
        known_weight = sum(
            weight
            for name, weight in weights.items()
            if weight > 0 and name not in not_applicable and dimensions.get(name) is not None
        )
        numerator = sum(
            float(dimensions[name]) * weight
            for name, weight in weights.items()
            if weight > 0 and name not in not_applicable and dimensions.get(name) is not None
        )
        fit_score = int(round(numerator / known_weight)) if known_weight else 0
        campaign = self.db.one(
            "SELECT config FROM research_campaigns WHERE id=? AND company_id=?",
            (result["campaign_id"], result["company_id"]),
        )
        scoring_data = (
            json_load(campaign["config"], {}).get("scoring", {}) if campaign else {}
        )
        profile = ScoringProfile.model_validate(scoring_data).model_copy(
            update={"weights": ScoringWeights.model_validate(weights)},
        )
        priority_band = "Rejected"
        for name in ("A", "B", "C"):
            threshold = profile.bands[name]
            if (
                fit_score >= threshold.min_fit
                and current.evidence_confidence >= threshold.min_confidence
            ):
                priority_band = name
                break
        evidence_ids = {
            **current.dimension_evidence_ids,
            dimension: dimension_evidence_ids(claims)[dimension],
        }
        return current.model_copy(update={
            "fit_score": fit_score,
            "priority_band": priority_band,
            "known_weight": known_weight,
            "unknown_weight": sum(unknown_dimensions.values()),
            "unknown_dimensions": unknown_dimensions,
            "dimensions": dimensions,
            "dimension_evidence_ids": evidence_ids,
        })

    def correct(
        self,
        fact_id: str,
        corrected_value_en,
        actor_id: str,
        reason: str,
        apply: bool,
    ) -> CorrectionImpact:
        reason = str(reason).strip()
        if len(reason) < 3:
            raise ValueError("fact correction requires a reason")
        impact = self.consumers(fact_id)
        if not apply:
            return impact
        stamp = now()
        encoded = json_dump(corrected_value_en)
        value_hash = _hash(corrected_value_en)
        if fact_id.startswith("sf_"):
            fact_row = self.db.one("SELECT field FROM shared_facts WHERE id=?", (fact_id,))
            self.db.execute(
                "UPDATE shared_facts SET value_en=?,value_hash=?,updated_at=? WHERE id=?",
                (encoded, value_hash, stamp, fact_id),
            )
            company_id = None
        else:
            fact_row = self.db.one(
                "SELECT company_id,field FROM tenant_facts WHERE id=?", (fact_id,),
            )
            company_id = fact_row["company_id"] if fact_row else None
            self.db.execute(
                "UPDATE tenant_facts SET value_en=?,value_hash=?,updated_at=? WHERE id=?",
                (encoded, value_hash, stamp, fact_id),
            )
        fact_field = fact_row["field"]

        recomputed: list[str] = []
        for result_id in impact.result_ids:
            result = self.db.one(
                "SELECT * FROM research_results WHERE id=?", (result_id,),
            )
            if result is None:
                continue
            current = json_load(result["snapshot_json"], {})
            if not current:
                continue
            score = self._recompute_snapshot_score(result, current, fact_field)
            revised = {
                **current,
                "score": score.model_dump(mode="json"),
                "correction": {
                    "fact_id": fact_id,
                    "value_en": corrected_value_en,
                    "actor_id": actor_id,
                    "reason": reason,
                    "applied_at": stamp,
                },
            }
            snapshot_id = new_id("score")
            self.db.execute(
                "INSERT INTO research_score_snapshots("
                "id,company_id,result_id,campaign_id,profile_version_id,organization_id,"
                "snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    snapshot_id, result["company_id"], result_id, result["campaign_id"],
                    result["profile_version_id"], result["organization_id"],
                    json_dump(revised), stamp,
                ),
            )
            data = json_load(result["data"], {})
            corrections = [*(data.get("corrections") or []), revised["correction"]]
            self.db.execute(
                "UPDATE research_results SET fit_score=?,evidence_confidence=?,snapshot_json=?,"
                "data=?,updated_at=? WHERE id=?",
                (
                    score.fit_score,
                    score.evidence_confidence,
                    json_dump(revised),
                    json_dump({
                        **data,
                        "score": score.model_dump(mode="json"),
                        "score_dimensions": score.dimensions,
                        "confidence_factors": score.confidence_factors,
                        "corrections": corrections,
                    }),
                    stamp,
                    result_id,
                ),
            )
            recomputed.append(result_id)

        # One resolved organization can be reused by several campaigns and all
        # of their results can point at the same operational lead.  Sync that
        # lead once from its newest campaign result; loop order must not decide
        # which customer's weight snapshot appears in the lead row.
        for lead_id in impact.lead_ids:
            lead = self.db.one("SELECT company_id,data FROM leads WHERE id=?", (lead_id,))
            if lead is None:
                continue
            latest = self.db.one(
                "SELECT fit_score,evidence_confidence,snapshot_json "
                "FROM research_results WHERE lead_id=? AND company_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (lead_id, lead["company_id"]),
            )
            if latest is None:
                continue
            latest_snapshot = json_load(latest["snapshot_json"], {})
            latest_score = latest_snapshot.get("score") or {}
            lead_data = json_load(lead["data"], {})
            self.db.execute(
                "UPDATE leads SET data=?,updated_at=? WHERE id=? AND company_id=?",
                (
                    json_dump({
                        **lead_data,
                        "fit_score": latest["fit_score"],
                        "evidence_confidence": latest["evidence_confidence"],
                        "priority_band": latest_score.get(
                            "priority_band", lead_data.get("priority_band")
                        ),
                    }),
                    stamp,
                    lead_id,
                    lead["company_id"],
                ),
            )

        correction_id = new_id("correction")
        self.db.execute(
            "INSERT INTO research_fact_corrections("
            "id,company_id,fact_id,corrected_value_en,actor_id,reason,applied,impact,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                correction_id, company_id, fact_id, encoded, actor_id, reason, 1,
                json_dump(impact.model_dump(mode="json")), stamp,
            ),
        )
        return impact.model_copy(update={"recomputed_result_ids": recomputed})
