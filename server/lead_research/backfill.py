"""Conservative, idempotent upgrade of pre-contract lead-research data.

Legacy rows are never promoted into the shared pool.  The backfill preserves
original payloads, adds compatibility snapshots beside them, and chooses the
least-trusting classification whenever old provenance cannot prove more.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..db import json_dump, json_load, now
from .contacts import verify_contact
from .models import CompanyResearchProfile


@dataclass(frozen=True)
class BackfillReport:
    profile_versions_created: int = 0
    datasets_classified: int = 0
    tenant_facts_created: int = 0
    results_snapshotted: int = 0
    contacts_classified: int = 0

    @property
    def total_changes(self) -> int:
        return sum((
            self.profile_versions_created,
            self.datasets_classified,
            self.tenant_facts_created,
            self.results_snapshotted,
            self.contacts_classified,
        ))


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _legacy_id(prefix: str, *parts) -> str:
    return f"{prefix}_legacy_{_hash(parts)[:20]}"


def _sections(db, company_id: str) -> dict[str, dict]:
    return {
        row["section"]: json_load(row["data"], {})
        for row in db.all("SELECT section,data FROM company_sections WHERE company_id=?", (company_id,))
    }


def _actor(db, company_id: str) -> str | None:
    row = db.one(
        "SELECT id FROM users WHERE company_id=? AND status='active' ORDER BY created_at,id LIMIT 1",
        (company_id,),
    )
    if row is None:
        row = db.one("SELECT id FROM users WHERE role='admin' AND status='active' ORDER BY created_at,id LIMIT 1")
    return row["id"] if row else None


def _profile_payload(db, company) -> CompanyResearchProfile | None:
    sections = _sections(db, company["id"])
    legacy = sections.get("profile") or {}
    products = []
    for row in db.all("SELECT * FROM products WHERE company_id=? ORDER BY created_at,id", (company["id"],)):
        data = json_load(row["data"], {})
        products.append({
            "id": row["id"],
            "name": row["name"],
            "english_name": data.get("english_name") or row["name"],
            "hs_codes": list(data.get("hs_codes") or []),
            "sector_ids": list(data.get("sector_ids") or []),
            **{key: value for key, value in data.items()
               if key not in {"id", "name", "english_name", "hs_codes", "sector_ids"}},
        })
    if not legacy and not products:
        return None
    # A profile cannot represent an empty product scope. Leave such tenants in
    # onboarding instead of inventing a product on their behalf.
    if not products:
        return None
    company_data = json_load(company["data"], {})
    website = legacy.get("website")
    if website:
        parsed = urlsplit(str(website))
        if parsed.scheme != "https" or not parsed.hostname:
            website = None
    identity = {
        "name": str(legacy.get("name") or company["name"]),
        "legal_name": legacy.get("legal_name") or company["legal_name"],
        "website": website,
    }
    countries = legacy.get("seller_countries") or company_data.get("seller_countries") or []
    countries = [str(value).upper() for value in countries if len(str(value)) == 2 and str(value).isalpha()]
    if not countries:
        country = str(legacy.get("country") or company_data.get("country") or "TR").upper()
        countries = [country if len(country) == 2 and country.isalpha() else "TR"]
    return CompanyResearchProfile(
        identity=identity,
        seller_countries=countries,
        products=products,
        market_preferences=sections.get("market_preferences") or {},
        research_exclusions=sections.get("research_exclusions") or {},
        confirmations={"legacy_contract_backfill_review_required": False},
    )


def _backfill_profiles(db) -> int:
    created = 0
    for company in db.all("SELECT * FROM companies ORDER BY id"):
        if db.one("SELECT id FROM company_profile_versions WHERE company_id=? LIMIT 1", (company["id"],)):
            continue
        actor = _actor(db, company["id"])
        profile = _profile_payload(db, company)
        if actor is None or profile is None:
            continue
        stamp = now()
        profile_id = _legacy_id("cpv", company["id"])
        db.execute(
            "INSERT INTO company_profile_versions("
            "id,company_id,version,status,profile_json,created_by,confirmed_by,created_at,confirmed_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (profile_id, company["id"], 1, "confirmed", json_dump(profile.model_dump(mode="json")),
             actor, actor, stamp, stamp),
        )
        db.execute(
            "UPDATE research_campaigns SET profile_version_id=?,"
            "scope_snapshot=COALESCE(scope_snapshot,?),created_by=COALESCE(created_by,?),"
            "updated_by=COALESCE(updated_by,?) WHERE company_id=? AND profile_version_id IS NULL",
            (profile_id, json_dump({
                "profile_version_id": profile_id,
                "seller_countries": profile.seller_countries,
                "compatibility": "legacy-contract-v1",
            }), actor, actor, company["id"]),
        )
        created += 1
    return created


def _backfill_datasets(db) -> int:
    count = db.one("SELECT COUNT(*) AS n FROM candidate_datasets WHERE visibility IS NULL")["n"]
    if count:
        db.execute(
            "UPDATE candidate_datasets SET visibility='service_public',owner_company_id=NULL "
            "WHERE visibility IS NULL"
        )
    return int(count)


def _claim_span(db, claim, value) -> tuple[str, str, int, int, str, float]:
    evidence_ids = json_load(claim["evidence_ids"], [])
    evidence_id = str(evidence_ids[0]) if evidence_ids else f"legacy:{claim['id']}"
    evidence = db.one(
        "SELECT * FROM evidence_records WHERE id=? AND company_id=?",
        (evidence_id, claim["company_id"]),
    )
    payload = json_load(evidence["payload"], {}) if evidence else {}
    spans = (payload.get("fact_spans") or {}).get(claim["field"]) or []
    span = spans[0] if spans and isinstance(spans[0], dict) else {}
    original = str(span.get("original") or payload.get("original_text") or "").strip()
    if not original:
        original = value if isinstance(value, str) else json_dump(value)
    start = max(0, int(span.get("start", 0) or 0))
    end = int(span.get("end", start + len(original)) or start + len(original))
    if end <= start:
        start, end = 0, len(original)
    language = str(payload.get("source_language") or "und")
    retrieved = float(evidence["retrieved_at"] if evidence else claim["verified_at"])
    return evidence_id, original, start, end, language, retrieved


def _backfill_claims(db) -> int:
    created = 0
    for claim in db.all("SELECT * FROM feature_claims ORDER BY company_id,id"):
        fact_id = _legacy_id("tf", claim["company_id"], claim["id"])
        if db.one("SELECT id FROM tenant_facts WHERE id=?", (fact_id,)):
            continue
        value = json_load(claim["value"], claim["value"])
        evidence_id, original, start, end, language, retrieved = _claim_span(db, claim, value)
        status = claim["status"] if claim["status"] in {
            "observed", "calculated", "estimated_range", "conflicted", "unknown", "not_applicable",
        } else "unknown"
        stamp = now()
        db.execute(
            "INSERT INTO tenant_facts("
            "id,company_id,campaign_id,organization_id,field,value_en,value_hash,original_text,"
            "source_language,derivation_kind,status,confidence,validation_basis,evidence_id,"
            "span_start,span_end,source_class,visibility,mechanically_validated,observed_at,"
            "retrieved_at,expires_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fact_id, claim["company_id"], claim["campaign_id"], claim["organization_id"],
             claim["field"], json_dump(value), _hash(value), original, language, "legacy_import",
             status, max(0.0, min(1.0, float(claim["confidence"] or 0))),
             "legacy claim retained tenant-private; revalidation required", evidence_id,
             start, end, "legacy", "private", 0, claim["verified_at"], retrieved,
             float(claim["verified_at"]), stamp, stamp),
        )
        created += 1
    return created


def _compatibility_snapshot(db, result) -> tuple[dict, str | None]:
    campaign = db.one(
        "SELECT profile_version_id,scope_snapshot FROM research_campaigns "
        "WHERE id=? AND company_id=?",
        (result["campaign_id"], result["company_id"]),
    )
    profile_id = campaign["profile_version_id"] if campaign else None
    fact_ids = [
        row["id"] for row in db.all(
            "SELECT id FROM tenant_facts WHERE company_id=? AND campaign_id=? AND organization_id=? ORDER BY id",
            (result["company_id"], result["campaign_id"], result["organization_id"]),
        )
    ]
    score = {
        "fit_score": int(result["fit_score"]),
        "evidence_confidence": float(result["evidence_confidence"]),
        "known_weight": 0,
        "unknown_weight": 100,
        "unknown_dimensions": ["legacy_unmapped"],
        "not_applicable_dimensions": {},
        "priority_band": "review",
        "dimensions": {},
        "dimension_evidence_ids": {},
    }
    return ({
        "contract_version": "legacy-compat-v1",
        "campaign_id": result["campaign_id"],
        "profile_version_id": profile_id,
        "scope": json_load(campaign["scope_snapshot"], {}) if campaign else {},
        "score": score,
        "verdict": {"verdict": result["verdict"], "reasons": ["legacy_result_preserved"]},
        "fact_ids": fact_ids,
        "evidence_ids": [],
        "legacy_payload": json_load(result["data"], {}),
    }, profile_id)


def _backfill_results(db) -> int:
    changed = 0
    for result in db.all("SELECT * FROM research_results ORDER BY company_id,id"):
        snapshot, profile_id = _compatibility_snapshot(db, result)
        snapshot_json = result["snapshot_json"]
        existing = db.one(
            "SELECT id FROM research_score_snapshots WHERE company_id=? AND result_id=? LIMIT 1",
            (result["company_id"], result["id"]),
        )
        touched = False
        if not snapshot_json:
            db.execute(
                "UPDATE research_results SET snapshot_json=?,profile_version_id=COALESCE(profile_version_id,?) "
                "WHERE id=? AND company_id=?",
                (json_dump(snapshot), profile_id, result["id"], result["company_id"]),
            )
            touched = True
        if not existing:
            db.execute(
                "INSERT INTO research_score_snapshots("
                "id,company_id,result_id,campaign_id,profile_version_id,organization_id,snapshot_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (_legacy_id("score", result["company_id"], result["id"]), result["company_id"],
                 result["id"], result["campaign_id"], profile_id, result["organization_id"],
                 snapshot_json or json_dump(snapshot), result["created_at"]),
            )
            touched = True
        changed += int(touched)
    return changed


def _backfill_contacts(db) -> int:
    changed = 0
    for row in db.all(
        "SELECT * FROM contacts WHERE verification_tier IS NULL OR verification_tier='' "
        "OR (verification_method='legacy_unverified' AND verification_checked_at IS NULL) "
        "ORDER BY company_id,id"
    ):
        data = json_load(row["data"], {})
        evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
        verification = verify_contact({**dict(row), "data": data}, evidence)
        if verification.tier != "green" or verification.method != "published_official_address":
            verification = verification.model_copy(update={
                "tier": "red", "method": "legacy_unverified", "evidence_ids": [],
            })
        db.execute(
            "UPDATE contacts SET verification_tier=?,contact_kind=?,verification_method=?,"
            "verification_evidence_ids=?,verification_checked_at=? WHERE id=? AND company_id=?",
            (verification.tier, verification.contact_kind, verification.method,
             json_dump(verification.evidence_ids), verification.checked_at,
             row["id"], row["company_id"]),
        )
        changed += 1
    return changed


def backfill_contract(db) -> BackfillReport:
    """Upgrade legacy rows without changing tenant ownership or shared state."""
    return BackfillReport(
        profile_versions_created=_backfill_profiles(db),
        datasets_classified=_backfill_datasets(db),
        tenant_facts_created=_backfill_claims(db),
        results_snapshotted=_backfill_results(db),
        contacts_classified=_backfill_contacts(db),
    )
