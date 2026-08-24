"""Promotion-safe shared and tenant research fact storage."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from ..db import json_dump, json_load, new_id, now
from ..quality import normalize_name
from .models import EvidenceSpan, ResearchFact, StoredFact


FactPool = Literal["shared", "tenant"]
DAY_SECONDS = 86_400.0

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
