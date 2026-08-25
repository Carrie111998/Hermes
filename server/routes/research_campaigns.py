"""Tenant-scoped lead-research campaign, evidence, and source lifecycle API."""
from __future__ import annotations

import csv
import io
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from ..auth import Principal, company_scope, current_principal, require_admin
from ..db import json_dump, json_load, new_id, now
from ..lead_research.models import CampaignConfig
from ..lead_research.candidates import CandidateRepository
from ..lead_research.languages import FIXED_TR
from ..lead_research.profiles import ProfileRepository
from ..lead_research.sectors import load_sectors
from ..lead_research.service import CampaignAlreadyRunning
from ..lead_research.storage import EvidenceRepository


router = APIRouter(tags=["lead-research"])

MAX_CANDIDATE_UPLOAD_BYTES = 20 * 1024 * 1024


class CampaignPatch(BaseModel):
    version: int = Field(ge=1)
    config: dict[str, Any]


class PurgeRequest(BaseModel):
    confirmation: str


def _scope(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


@router.post("/candidate-datasets", status_code=201)
async def upload_candidate_dataset(
    file: UploadFile,
    request: Request,
    principal: Principal = Depends(current_principal),
    x_company_id: str | None = Header(default=None),
):
    company_id = _scope(principal, x_company_id)
    content = await file.read(MAX_CANDIDATE_UPLOAD_BYTES + 1)
    if len(content) > MAX_CANDIDATE_UPLOAD_BYTES:
        raise HTTPException(413, "Candidate dataset exceeds the 20 MB upload limit")
    filename = file.filename or "candidates.csv"
    report = CandidateRepository(request.app.state.db).import_file(
        f"tenant-{company_id}-{new_id('dataset')}",
        "1",
        filename,
        content,
        owner_company_id=company_id,
        visibility="tenant_private",
    )
    request.app.state.db.activity(
        company_id, principal.id, "candidate_dataset_uploaded", "candidate_dataset",
        report.dataset_id, {"records": report.record_count},
    )
    return {
        "dataset_id": report.dataset_id,
        "version": report.version,
        "record_count": report.record_count,
        "raw_hash": report.raw_hash,
        "visibility": "tenant_private",
    }


def _serialize(row) -> dict:
    return {
        "id": row["id"], "company_id": row["company_id"], "name": row["name"],
        "status": row["status"], "version": row["version"], "config": json_load(row["config"], {}),
        "estimate": json_load(row["estimate"], None), "run_id": row["run_id"],
        "profile_version_id": row["profile_version_id"],
        "scope_snapshot": json_load(row["scope_snapshot"], {}),
        "created_by": row["created_by"], "updated_by": row["updated_by"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _campaign_contract(request: Request, company_id: str, raw: dict, profile_id: str | None = None):
    profiles = ProfileRepository(request.app.state.db)
    profile = profiles.get(company_id, profile_id) if profile_id else profiles.current(company_id)
    if profile is None:
        raise HTTPException(409, detail={"message": "Confirm the company research profile first", "missing": ["confirmed_profile"]})
    values = dict(raw)
    values.setdefault("seller_countries", profile.profile.seller_countries)
    try:
        config = CampaignConfig.model_validate(values)
    except Exception as exc:
        raise HTTPException(422, detail={"path": "config", "message": str(exc)}) from exc
    product_by_id = {str(product.get("id")): product for product in profile.profile.products}
    missing_products = [product_id for product_id in config.product_ids if product_id not in product_by_id]
    if missing_products:
        raise HTTPException(422, detail={
            "path": "product_ids",
            "message": f"Products are not present in profile {profile.id}: {', '.join(missing_products)}",
        })
    resolved = [
        str(product_by_id[product_id].get("english_name") or product_by_id[product_id].get("name")).strip()
        for product_id in config.product_ids
    ]
    merged_terms: list[str] = []
    seen: set[str] = set()
    for term in [*config.product_terms, *resolved]:
        key = term.casefold()
        if term and key not in seen:
            merged_terms.append(term)
            seen.add(key)
    config = config.model_copy(update={"product_terms": merged_terms})
    snapshot = {
        "profile_version_id": profile.id,
        "seller_countries": list(config.seller_countries),
        "target_countries": list(config.target_countries),
        "sector_ids": list(config.sector_ids),
        "hs_codes": list(config.hs_codes),
        "product_ids": list(config.product_ids),
        "product_terms": list(config.product_terms),
        "buyer_types": list(config.buyer_types),
    }
    return profile, config, snapshot


def _insert_campaign(request: Request, company_id: str, actor_id: str, raw: dict,
                     profile_id: str | None = None):
    profile, config, snapshot = _campaign_contract(request, company_id, raw, profile_id)
    request.app.state.lead_research.ensure_catalog(company_id)
    unknown = sorted(set(config.enabled_source_ids) - set(request.app.state.lead_research.registry.definitions))
    if unknown:
        raise HTTPException(422, detail={"path": "enabled_source_ids", "message": f"Unknown sources: {', '.join(unknown)}"})
    readiness = request.app.state.lead_research.validate_readiness(company_id, config)
    if not readiness.ready:
        raise HTTPException(409, detail={
            "message": "Research campaign is not ready",
            "missing": readiness.missing,
            "zero_result_explanation": readiness.zero_result_explanation,
        })
    campaign_id, stamp = new_id("rc"), now()
    request.app.state.db.execute(
        "INSERT INTO research_campaigns("
        "id,company_id,name,status,version,config,estimate,run_id,profile_version_id,"
        "scope_snapshot,created_by,updated_by,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            campaign_id, company_id, config.name, "draft", 1,
            json_dump(config.model_dump(mode="json")), None, None, profile.id,
            json_dump(snapshot), actor_id, actor_id, stamp, stamp,
        ),
    )
    return _serialize(_row(request, company_id, campaign_id))


def _with_source_availability(campaign: dict, catalog: list[dict]) -> dict:
    enabled = set(campaign.get("config", {}).get("enabled_source_ids", []))
    credential_required = sorted(
        source["source_id"]
        for source in catalog
        if source.get("source_id") in enabled
        and source.get("unavailable_reason") == "credential_required"
    )
    return {**campaign, "credential_required_source_ids": credential_required}


def _row(request: Request, company_id: str, campaign_id: str):
    row = request.app.state.db.one(
        "SELECT * FROM research_campaigns WHERE id=? AND company_id=?", (campaign_id, company_id)
    )
    if not row:
        raise HTTPException(404, "Research campaign not found")
    return row


# `active` keeps its name for compatibility, but it now means exactly the
# customer's primary list: strong fits the ranker chose to display. What used to
# share that list — candidates that never cleared the floor — is `review`, and
# strong fits beyond the global limit are `outside_limit`. Three separate
# answers to three separate questions, instead of one list called "not
# rejected".
ResultView = Literal["active", "review", "outside_limit", "rejected"]

_VIEW_VERDICTS = {
    "active": ("strong_fit",),
    "outside_limit": ("strong_fit",),
    "review": ("review",),
    "rejected": ("reject",),
}


def _displayed(row) -> bool:
    return bool((json_load(row["data"], {}).get("selection") or {}).get("displayed"))


def _display_rank(row) -> int:
    rank = (json_load(row["data"], {}).get("selection") or {}).get("display_rank")
    return int(rank) if isinstance(rank, (int, float)) else 1_000_000


def _result_rows(request: Request, company_id: str, campaign_id: str, view: ResultView):
    _row(request, company_id, campaign_id)
    verdicts = _VIEW_VERDICTS[view]
    rows = request.app.state.db.all(
        "SELECT * FROM research_results "
        f"WHERE company_id=? AND campaign_id=? AND verdict IN ({','.join('?' for _ in verdicts)}) "
        "ORDER BY fit_score DESC,evidence_confidence DESC,created_at DESC",
        (company_id, campaign_id, *verdicts),
    )
    # The display flag lives inside a JSON column, and SQLite and Postgres do
    # not agree on how to read one. Filtering here keeps both backends
    # answering identically, which schema parity requires.
    if view == "active":
        return sorted((row for row in rows if _displayed(row)), key=_display_rank)
    if view == "outside_limit":
        return [row for row in rows if not _displayed(row)]
    return rows


_CUSTOMER_RESULT_FIELDS = (
    "reasons", "missing_evidence", "conflicting_claims", "source_ids",
    "official_domains", "independent_domains", "score_dimensions",
    "confidence_factors", "profile_version_id", "scope", "playbook_versions",
    "source_policy", "selection",
)
_CUSTOMER_SCORE_FIELDS = (
    "priority_band", "known_weight", "unknown_weight", "unknown_dimensions",
    "not_applicable_dimensions", "dimensions", "dimension_evidence_ids",
    "confidence_factors",
)


def _result_contract(row) -> tuple[dict, dict, dict]:
    """Return only customer-safe result, score, and verdict fields.

    Facts and labels can be reused across tenants, but the customer contract is
    a campaign decision rather than a view into that storage machinery.  An
    allow-list here prevents a future internal key from leaking merely because
    it was added to ``research_results.data``.
    """
    data = json_load(row["data"], {})
    snapshot = json_load(row["snapshot_json"], {}) if "snapshot_json" in row.keys() else {}
    score = snapshot.get("score") or data.get("score") or {}
    verdict = snapshot.get("verdict") or data.get("verdict_snapshot") or {}
    customer = {key: data[key] for key in _CUSTOMER_RESULT_FIELDS if key in data}
    for key in ("reasons", "missing_evidence", "conflicting_claims"):
        if key not in customer and key in verdict:
            customer[key] = verdict[key]
    customer.update({key: score[key] for key in _CUSTOMER_SCORE_FIELDS if key in score})
    return customer, score, snapshot


def _criteria_for_evidence(score: dict, snapshot: dict, evidence_id: str) -> list[dict]:
    weights = snapshot.get("weights") or {}
    result = []
    for dimension, evidence_ids in (score.get("dimension_evidence_ids") or {}).items():
        if evidence_id in (evidence_ids or []):
            result.append({"dimension": dimension, "weight": int(weights.get(dimension, 0))})
    return result


def _display_value(request: Request, company_id: str, fact_key: str, value, locale: str):
    if locale != "tr" or not isinstance(value, str):
        return value
    translated = request.app.state.db.one(
        "SELECT display_value FROM research_translations "
        "WHERE company_id=? AND fact_key=? AND value_en=? AND display_locale='tr' "
        "ORDER BY updated_at DESC LIMIT 1",
        (company_id, fact_key, value),
    )
    return translated["display_value"] if translated else FIXED_TR.get(value.casefold(), value)


def _fact_rows_for_evidence(
    request: Request,
    company_id: str,
    organization_id: str,
    field: str,
    evidence_id: str,
    shared_evidence_id: str | None,
):
    tenant = request.app.state.db.all(
        "SELECT id,value_en,original_text,source_language,observed_at,retrieved_at,"
        "span_start,span_end,mechanically_validated,source_class "
        "FROM tenant_facts WHERE company_id=? AND organization_id=? "
        "AND field=? AND evidence_id=? ORDER BY id",
        (company_id, organization_id, field, evidence_id),
    )
    shared_id = shared_evidence_id or (evidence_id if evidence_id.startswith("sev_") else None)
    shared = []
    if shared_id:
        shared = request.app.state.db.all(
            "SELECT f.id,f.value_en,e.original_text,e.source_language,f.observed_at,"
            "f.retrieved_at,e.span_start,e.span_end,f.mechanically_validated,f.source_class "
            "FROM shared_facts f "
            "JOIN shared_fact_evidence x ON x.fact_id=f.id "
            "JOIN shared_evidence_records e ON e.id=x.evidence_id "
            "JOIN research_fact_consumers c ON c.shared_fact_id=f.id AND c.company_id=? "
            "JOIN organizations o ON o.id=? AND o.company_id=? "
            "AND o.shared_organization_id=f.organization_id "
            "WHERE f.field=? AND e.id=? ORDER BY f.id",
            (company_id, organization_id, company_id, field, shared_id),
        )
    return [*tenant, *shared]


def _customer_evidence(
    request: Request,
    company_id: str,
    organization_id: str,
    field: str,
    evidence_id: str,
    *,
    score: dict | None = None,
    snapshot: dict | None = None,
    locale: str = "en",
) -> dict | None:
    stored = request.app.state.db.one(
        "SELECT source_id,provenance_url,retrieved_at,observed_at,snapshot_id,raw_hash,"
        "method,confidence,payload,shared_evidence_id FROM evidence_records "
        "WHERE id=? AND company_id=? AND organization_id=?",
        (evidence_id, company_id, organization_id),
    )
    shared_id = stored["shared_evidence_id"] if stored else None
    shared = None
    if not stored and evidence_id.startswith("sev_"):
        shared = request.app.state.db.one(
            "SELECT e.source_id,e.provenance_url,e.retrieved_at,e.raw_hash,e.source_language,"
            "e.original_text,e.span_start,e.span_end "
            "FROM shared_evidence_records e "
            "JOIN shared_fact_evidence x ON x.evidence_id=e.id "
            "JOIN shared_facts f ON f.id=x.fact_id "
            "JOIN research_fact_consumers c ON c.shared_fact_id=f.id AND c.company_id=? "
            "JOIN organizations o ON o.id=? AND o.company_id=? "
            "AND o.shared_organization_id=f.organization_id "
            "WHERE e.id=? AND f.field=? LIMIT 1",
            (company_id, organization_id, company_id, evidence_id, field),
        )
        if not shared:
            return None
        shared_id = evidence_id
    elif not stored:
        return None

    payload = json_load(stored["payload"], {}) if stored else {}
    rows = _fact_rows_for_evidence(
        request, company_id, organization_id, field, evidence_id, shared_id,
    )
    facts = []
    for row in rows:
        value = json_load(row["value_en"], None)
        facts.append({
            "value_en": value,
            "display_value": _display_value(request, company_id, row["id"], value, locale),
            "original_text": row["original_text"],
            "source_language": row["source_language"],
            "observed_at": row["observed_at"],
            "retrieved_at": row["retrieved_at"],
            "span_start": row["span_start"],
            "span_end": row["span_end"],
            "mechanically_validated": bool(row["mechanically_validated"]),
            "source_class": row["source_class"],
        })

    # Older evidence rows predate fact persistence.  Their verified payload is
    # still useful, but it is explicitly described as payload evidence rather
    # than silently pretending a shared/tenant fact exists.
    if not facts and stored:
        values = (payload.get("facts") or {}).get(field) or []
        spans = (payload.get("fact_spans") or {}).get(field) or []
        snapshot_content = payload.get("snapshot_content") or ""
        for index, value in enumerate(values):
            span = spans[index] if index < len(spans) else {}
            original = str(span.get("original") or value)
            start, end = int(span.get("start") or 0), int(span.get("end") or 0)
            exact = bool(
                snapshot_content and end > start
                and snapshot_content[start:end] == original
            )
            facts.append({
                "value_en": value,
                "display_value": FIXED_TR.get(str(value).casefold(), value) if locale == "tr" else value,
                "original_text": original,
                "source_language": payload.get("source_language") or "en",
                "observed_at": stored["observed_at"],
                "retrieved_at": stored["retrieved_at"],
                "span_start": start,
                "span_end": end,
                "mechanically_validated": exact,
                "source_class": payload.get("classification") or "public",
            })

    first = facts[0] if facts else {
        "value_en": None, "display_value": None, "original_text": None,
        "source_language": shared["source_language"] if shared else payload.get("source_language"),
        "observed_at": stored["observed_at"] if stored else None,
        "retrieved_at": stored["retrieved_at"] if stored else shared["retrieved_at"],
        "span_start": shared["span_start"] if shared else None,
        "span_end": shared["span_end"] if shared else None,
        "mechanically_validated": False,
        "source_class": payload.get("classification") or "public",
    }
    url = stored["provenance_url"] if stored else shared["provenance_url"]
    reference = payload.get("source_reference")
    return {
        "source_id": stored["source_id"] if stored else shared["source_id"],
        "provenance_url": url if str(url or "").startswith("https://") else None,
        # Evidence from a curated dataset has no public page to link. The
        # reference is the receipt: dataset, version and row, immutably. Shown
        # as a value, never as an href — a link the customer cannot follow is
        # worse than an identifier they can quote.
        "source_reference": reference if str(reference or "").startswith("dataset:") else None,
        "publisher_label": payload.get("publisher_label"),
        "retrieved_at": first["retrieved_at"],
        "observed_at": first["observed_at"],
        "archive_snapshot_at": payload.get("archive_snapshot_at"),
        "snapshot_id": stored["snapshot_id"] if stored else None,
        "raw_hash": stored["raw_hash"] if stored else shared["raw_hash"],
        "method": stored["method"] if stored else "observed",
        "confidence": stored["confidence"] if stored else None,
        "value_en": first["value_en"],
        "display_value": first["display_value"],
        "original_text": first["original_text"],
        "source_language": first["source_language"],
        "span_start": first["span_start"],
        "span_end": first["span_end"],
        "mechanically_validated": first["mechanically_validated"],
        "source_class": first["source_class"],
        "facts": facts,
        "criteria": _criteria_for_evidence(score or {}, snapshot or {}, evidence_id),
    }


def _result_evidence(request: Request, row, score: dict, snapshot: dict) -> list[dict]:
    """Flatten the result's customer-safe citations for ranked-list display.

    The claims endpoint remains the grouped detail view.  A result also needs
    enough receipts to honor the lead-list contract without making the client
    discover internal fact identifiers or issue one request per row.
    """
    citations: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for claim in request.app.state.db.all(
        "SELECT field,evidence_ids FROM feature_claims "
        "WHERE company_id=? AND campaign_id=? AND organization_id=? "
        "ORDER BY field,verified_at DESC",
        (row["company_id"], row["campaign_id"], row["organization_id"]),
    ):
        for evidence_id in json_load(claim["evidence_ids"], []):
            key = (claim["field"], evidence_id)
            if key in seen:
                continue
            citation = _customer_evidence(
                request,
                row["company_id"],
                row["organization_id"],
                claim["field"],
                evidence_id,
                score=score,
                snapshot=snapshot,
            )
            if citation is not None:
                citations.append({"field": claim["field"], **citation})
                seen.add(key)
    return citations


def _serialize_result(request: Request, row) -> dict:
    data, score, snapshot = _result_contract(row)
    organization = request.app.state.db.one(
        "SELECT display_name,domain,country FROM organizations WHERE id=? AND company_id=?",
        (row["organization_id"], row["company_id"]),
    )
    lead = None
    if row["lead_id"]:
        lead = request.app.state.db.one(
            "SELECT company_name,website,country,data FROM leads WHERE id=? AND company_id=?",
            (row["lead_id"], row["company_id"]),
        )
    lead_data = json_load(lead["data"], {}) if lead else {}
    buyer_role = lead_data.get("buyer_type")
    if not buyer_role:
        claim = request.app.state.db.one(
            "SELECT value FROM feature_claims "
            "WHERE company_id=? AND organization_id=? AND campaign_id=? AND field='buyer_role' "
            "ORDER BY verified_at DESC",
            (row["company_id"], row["organization_id"], row["campaign_id"]),
        )
        value = json_load(claim["value"], None) if claim else None
        buyer_role = value[0] if isinstance(value, list) and value else value
    source_ids = data.get("source_ids", [])
    return {
        **data,
        "id": row["id"],
        "company_id": row["company_id"],
        "campaign_id": row["campaign_id"],
        "organization_id": row["organization_id"],
        "lead_id": row["lead_id"],
        "company_name": (lead["company_name"] if lead else None)
        or (organization["display_name"] if organization else "Unknown company"),
        "website": (lead["website"] if lead else None)
        or (organization["domain"] if organization else None),
        "country": (lead["country"] if lead else None)
        or (organization["country"] if organization else None),
        "buyer_role": buyer_role,
        "verdict": row["verdict"],
        "fit_score": row["fit_score"],
        "evidence_confidence": row["evidence_confidence"],
        "source_ids": source_ids,
        "source_count": len(source_ids),
        "evidence": _result_evidence(request, row, score, snapshot),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/research-campaigns")
def list_campaigns(request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    catalog = request.app.state.lead_research.catalog(company_id)
    return [
        _with_source_availability(_serialize(row), catalog)
        for row in request.app.state.db.all(
            "SELECT * FROM research_campaigns WHERE company_id=? ORDER BY updated_at DESC", (company_id,)
        )
    ]


@router.post("/research-campaigns", status_code=201)
def create_campaign(body: dict[str, Any], request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return _insert_campaign(request, company_id, principal.id, body.get("config", body))


@router.get("/research-campaigns/{campaign_id}")
def get_campaign(campaign_id: str, request: Request,
                 principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return _with_source_availability(
        _serialize(_row(request, company_id, campaign_id)),
        request.app.state.lead_research.catalog(company_id),
    )


@router.patch("/research-campaigns/{campaign_id}")
def patch_campaign(campaign_id: str, body: CampaignPatch, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    current = _row(request, company_id, campaign_id)
    if current["version"] != body.version:
        raise HTTPException(409, detail={"message": "Campaign changed on the server", "current": _serialize(current)})
    if current["status"] not in {"draft", "failed", "cancelled"}:
        raise HTTPException(409, "Only draft, failed, or cancelled campaigns can be edited")
    _, config, snapshot = _campaign_contract(
        request, company_id, body.config, current["profile_version_id"],
    )
    self_version = current["version"] + 1
    request.app.state.db.execute(
        "UPDATE research_campaigns SET name=?,config=?,scope_snapshot=?,version=?,"
        "estimate=NULL,updated_by=?,updated_at=? "
        "WHERE id=? AND company_id=?",
        (config.name, json_dump(config.model_dump(mode="json")), json_dump(snapshot),
         self_version, principal.id, now(), campaign_id, company_id),
    )
    return get_campaign(campaign_id, request, principal, x_company_id)


@router.post("/research-campaigns/{campaign_id}/estimate")
def estimate_campaign(campaign_id: str, request: Request,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = _row(request, company_id, campaign_id)
    config = CampaignConfig.model_validate(json_load(row["config"], {}))
    estimate = request.app.state.lead_research.estimate(
        config, company_id,
    ).model_dump(mode="json")
    request.app.state.db.execute(
        "UPDATE research_campaigns SET estimate=?,updated_at=? WHERE id=? AND company_id=?",
        (json_dump(estimate), now(), campaign_id, company_id),
    )
    return estimate


@router.post("/research-campaigns/{campaign_id}/start", status_code=202)
def start_campaign(campaign_id: str, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = _row(request, company_id, campaign_id)
    if row["status"] not in {"draft", "failed", "cancelled", "partial", "completed", "succeeded"}:
        raise HTTPException(409, "Campaign cannot start from its current state")
    config = CampaignConfig.model_validate(json_load(row["config"], {}))
    readiness = request.app.state.lead_research.validate_readiness(company_id, config)
    if not readiness.ready:
        raise HTTPException(409, detail={
            "message": "Research campaign is not ready",
            "missing": readiness.missing,
            "zero_result_explanation": readiness.zero_result_explanation,
        })
    # Queued, not run: a campaign is hundreds of blocking HTTP fetches, so
    # running it here held the request open for the whole campaign and any proxy
    # timeout killed it mid-run, leaving the campaign `running` forever. Poll
    # GET /research-campaigns/{id} for status and run_id.
    try:
        return request.app.state.lead_research.start(company_id, campaign_id)
    except CampaignAlreadyRunning:
        raise HTTPException(409, "Campaign is already queued or running")


@router.post("/research-campaigns/{campaign_id}/cancel")
def cancel_campaign(campaign_id: str, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    _row(request, company_id, campaign_id)
    request.app.state.db.execute(
        "UPDATE research_campaigns SET status='cancelled',updated_at=? WHERE id=? AND company_id=?",
        (now(), campaign_id, company_id),
    )
    return get_campaign(campaign_id, request, principal, x_company_id)


@router.post("/research-campaigns/{campaign_id}/retry", status_code=202)
def retry_campaign(campaign_id: str, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    return start_campaign(campaign_id, request, principal, x_company_id)


@router.post("/research-campaigns/{campaign_id}/clone", status_code=201)
def clone_campaign(campaign_id: str, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    original = get_campaign(campaign_id, request, principal, x_company_id)
    config = {**original["config"], "name": f"{original['name']} copy"}
    return _insert_campaign(
        request, original["company_id"], principal.id, config, original["profile_version_id"],
    )


@router.delete("/research-campaigns/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: str, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = _row(request, company_id, campaign_id)
    if row["status"] != "draft":
        raise HTTPException(409, "Only drafts can be deleted")
    request.app.state.db.execute(
        "DELETE FROM research_campaigns WHERE id=? AND company_id=?", (campaign_id, company_id)
    )


@router.get("/research-campaigns/{campaign_id}/metrics")
def campaign_metrics(campaign_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    _row(request, company_id, campaign_id)
    rows = request.app.state.db.all(
        "SELECT * FROM campaign_metrics WHERE company_id=? AND campaign_id=? ORDER BY dimension,dimension_value",
        (company_id, campaign_id),
    )
    return [{"dimension": row["dimension"], "value": row["dimension_value"],
             **json_load(row["metrics"], {})} for row in rows]


@router.get("/research-campaigns/{campaign_id}/source-runs")
def campaign_sources(campaign_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    _row(request, company_id, campaign_id)
    return [{**dict(row), "metrics": json_load(row["metrics"], {})} for row in request.app.state.db.all(
        "SELECT * FROM campaign_partitions WHERE company_id=? AND campaign_id=? ORDER BY source_id,target_country",
        (company_id, campaign_id),
    )]


@router.get("/research-campaigns/{campaign_id}/issues")
def campaign_issues(campaign_id: str, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    _row(request, company_id, campaign_id)
    return [{**dict(row), "data": json_load(row["data"], {})} for row in request.app.state.db.all(
        "SELECT * FROM research_issues WHERE company_id=? AND campaign_id=? ORDER BY created_at DESC",
        (company_id, campaign_id),
    )]


# The bands a scoring profile defines, plus the leads a run rejected outright.
# Rejected is reported rather than dropped: it is the control group. A band that
# converts no better than the leads we threw away is the clearest evidence a
# scoring profile is not discriminating, and it is invisible if the comparison
# is only ever between A, B and C.
_OUTCOME_BANDS = ("A", "B", "C", "Rejected")


def _rate(numerator: int, denominator: int) -> float | None:
    """A share, or None when there is nothing to take a share of.

    Deliberately not 0.0 for an empty denominator: a band nobody contacted has
    no reply rate, and reporting 0% would read as "we tried and it failed"
    rather than "we have not tried". The whole point of this report is deciding
    whether to change a scoring profile, and those two lead to opposite
    decisions.
    """
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


@router.get("/research-campaigns/{campaign_id}/outcomes")
def campaign_outcomes(campaign_id: str, request: Request,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    """What each priority band actually produced once it was contacted.

    The only ground truth this product has is whether a lead replied. Fit and
    evidence confidence are our own opinion of a company, measured against
    evidence we chose to collect and weights the customer chose to set — nothing
    in that loop closes. This closes it: if band A replies at the same rate as
    band C, or as the leads the run rejected, then the weights or the criteria
    are wrong and no amount of further evidence rigour will fix it.

    It also carries a load the customer-facing product deliberately does not.
    Labels are not shown to customers, so a customer cannot tell us a label is
    wrong; conversion per band is the only channel through which a mislabelled
    profile ever surfaces.

    Reported, never applied. Weights are not tuned from this — a handful of
    replies is noise, and a scoring profile that moves on its own is one a
    customer cannot be given an explanation for.
    """
    company_id = _scope(principal, x_company_id)
    _row(request, company_id, campaign_id)
    # Leads first, messages second, aggregated here rather than in SQL: the band
    # lives in the lead's JSON payload, and `json_extract` is SQLite-only while
    # Postgres spells it `->>`. One tenant's campaign is hundreds of rows, so
    # this is cheaper than the portability problem it avoids.
    rows = request.app.state.db.all(
        "SELECT research_results.lead_id AS lead_id, research_results.verdict AS verdict, "
        "research_results.fit_score AS fit_score, "
        "research_results.evidence_confidence AS evidence_confidence, "
        "leads.data AS lead_data "
        "FROM research_results "
        "JOIN leads ON leads.id=research_results.lead_id "
        "AND leads.company_id=research_results.company_id "
        "WHERE research_results.company_id=? AND research_results.campaign_id=? "
        "AND research_results.lead_id IS NOT NULL",
        (company_id, campaign_id),
    )
    if not rows:
        return {"campaign_id": campaign_id, "bands": [], "totals": _empty_totals()}
    lead_bands = {}
    for row in rows:
        data = json_load(row["lead_data"], {})
        band = data.get("priority_band")
        lead_bands[row["lead_id"]] = {
            "band": band if band in _OUTCOME_BANDS else "Rejected",
            "fit_score": row["fit_score"],
            "evidence_confidence": row["evidence_confidence"],
        }
    messages = request.app.state.db.all(
        "SELECT lead_id, sent_at, replied_at, bounced_at FROM outreach_messages "
        "WHERE company_id=? AND lead_id IS NOT NULL",
        (company_id,),
    )
    per_lead: dict[str, dict] = {}
    for message in messages:
        if message["lead_id"] not in lead_bands:
            continue
        counters = per_lead.setdefault(
            message["lead_id"], {"sent": 0, "replied": 0, "bounced": 0}
        )
        # Counted on the timestamp, not on `status`: a message reaches several
        # statuses in its life and only these three say what happened to it.
        if message["sent_at"]:
            counters["sent"] += 1
        if message["replied_at"]:
            counters["replied"] += 1
        if message["bounced_at"]:
            counters["bounced"] += 1
    bands = []
    for name in _OUTCOME_BANDS:
        members = [key for key, value in lead_bands.items() if value["band"] == name]
        if not members:
            continue
        counted = [per_lead.get(key, {"sent": 0, "replied": 0, "bounced": 0}) for key in members]
        contacted = sum(1 for value in counted if value["sent"])
        replied = sum(1 for value in counted if value["replied"])
        bounced = sum(1 for value in counted if value["bounced"])
        fits = [lead_bands[key]["fit_score"] for key in members]
        confidences = [lead_bands[key]["evidence_confidence"] for key in members]
        bands.append({
            "band": name,
            "leads": len(members),
            "leads_contacted": contacted,
            "leads_replied": replied,
            "leads_bounced": bounced,
            "messages_sent": sum(value["sent"] for value in counted),
            # Per lead, not per message: three follow-ups to one company that
            # never answers is one company that never answered, and dividing by
            # messages would let a persistent sequence look like poor targeting.
            "reply_rate": _rate(replied, contacted),
            "bounce_rate": _rate(bounced, contacted),
            "mean_fit_score": round(sum(fits) / len(fits), 1),
            "mean_evidence_confidence": round(sum(confidences) / len(confidences), 3),
        })
    totals = {
        "leads": len(lead_bands),
        "leads_contacted": sum(band["leads_contacted"] for band in bands),
        "leads_replied": sum(band["leads_replied"] for band in bands),
        "leads_bounced": sum(band["leads_bounced"] for band in bands),
    }
    totals["reply_rate"] = _rate(totals["leads_replied"], totals["leads_contacted"])
    return {"campaign_id": campaign_id, "bands": bands, "totals": totals}


def _empty_totals() -> dict:
    return {
        "leads": 0, "leads_contacted": 0, "leads_replied": 0,
        "leads_bounced": 0, "reply_rate": None,
    }


@router.get("/research-campaigns/{campaign_id}/leads")
def campaign_leads(campaign_id: str, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    _row(request, company_id, campaign_id)
    result = []
    # The primary list, in the order the ranker saved. Not "every result with a
    # lead row": a rerun can leave an older lead attached to a result the new
    # ranking no longer displays, and the customer's list has to be exactly what
    # the run decided.
    rows = request.app.state.db.all(
        "SELECT leads.*,research_results.data AS result_data FROM research_results "
        "JOIN leads ON leads.id=research_results.lead_id AND leads.company_id=research_results.company_id "
        "WHERE research_results.company_id=? AND research_results.campaign_id=? "
        "AND research_results.verdict='strong_fit'",
        (company_id, campaign_id),
    )
    selected = [
        row for row in rows
        if (json_load(row["result_data"], {}).get("selection") or {}).get("displayed")
    ]
    selected.sort(key=lambda row: (
        (json_load(row["result_data"], {}).get("selection") or {}).get("display_rank")
        or 1_000_000,
        row["created_at"],
    ))
    for row in selected:
        data = json_load(row["data"], {})
        result.append({"id": row["id"], "company_name": row["company_name"], "website": row["website"],
                       "country": row["country"], "status": row["status"], **data})
    return result


@router.get("/research-campaigns/{campaign_id}/results")
def campaign_results(campaign_id: str, request: Request,
                     view: ResultView = Query(default="active"),
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return [
        _serialize_result(request, row)
        for row in _result_rows(request, company_id, campaign_id, view)
    ]


@router.get("/research/leads/{lead_id}/claims")
def lead_claims(lead_id: str, request: Request,
                locale: Literal["en", "tr"] = Query(default="en"),
                principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    lead = request.app.state.db.one("SELECT * FROM leads WHERE id=? AND company_id=?", (lead_id, company_id))
    if not lead:
        raise HTTPException(404, "Lead not found")
    organization_id = lead["resolved_organization_id"] or json_load(lead["data"], {}).get("organization_id")
    latest_result = request.app.state.db.one(
        "SELECT * FROM research_results WHERE company_id=? AND organization_id=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (company_id, organization_id),
    )
    _, score, snapshot = _result_contract(latest_result) if latest_result else ({}, {}, {})
    result = []
    for row in request.app.state.db.all(
        "SELECT * FROM feature_claims WHERE company_id=? AND organization_id=? ORDER BY field",
        (company_id, organization_id),
    ):
        data = json_load(row["data"], {})
        evidence_ids = json_load(row["evidence_ids"], [])
        evidence = [
            item for evidence_id in evidence_ids
            if (item := _customer_evidence(
                request, company_id, organization_id, row["field"], evidence_id,
                score=score, snapshot=snapshot, locale=locale,
            )) is not None
        ]
        result.append({
            "id": row["id"], "field": row["field"], "value": json_load(row["value"], None),
            "status": row["status"], "confidence": row["confidence"], "method": row["method"],
            "evidence": evidence, "verified_at": row["verified_at"],
            **{key: data.get(key) for key in (
                "source_ids", "period", "unit", "currency", "applicability", "validated", "observed_at"
            ) if key in data},
        })
    return result


@router.get("/research/results/{result_id}/claims")
def result_claims(result_id: str, request: Request,
                  locale: Literal["en", "tr"] = Query(default="en"),
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    result_row = request.app.state.db.one(
        "SELECT * FROM research_results "
        "WHERE id=? AND company_id=?",
        (result_id, company_id),
    )
    if not result_row:
        raise HTTPException(404, "Research result not found")
    _, score, snapshot = _result_contract(result_row)
    claims = []
    for row in request.app.state.db.all(
        "SELECT * FROM feature_claims "
        "WHERE company_id=? AND campaign_id=? AND organization_id=? ORDER BY field,verified_at DESC",
        (company_id, result_row["campaign_id"], result_row["organization_id"]),
    ):
        data = json_load(row["data"], {})
        evidence_ids = json_load(row["evidence_ids"], [])
        evidence = [
            item for evidence_id in evidence_ids
            if (item := _customer_evidence(
                request, company_id, result_row["organization_id"], row["field"],
                evidence_id, score=score, snapshot=snapshot, locale=locale,
            )) is not None
        ]
        claims.append({
            "id": row["id"],
            "field": row["field"],
            "value": json_load(row["value"], None),
            "status": row["status"],
            "confidence": row["confidence"],
            "method": row["method"],
            "evidence": evidence,
            "verified_at": row["verified_at"],
            **{key: data.get(key) for key in (
                "source_ids", "period", "unit", "currency", "applicability", "validated", "observed_at"
            ) if key in data},
        })
    return claims


@router.post("/research-campaigns/{campaign_id}/export")
def export_campaign(campaign_id: str, request: Request,
                    view: ResultView = Query(default="active"),
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    rows = [
        _serialize_result(request, row)
        for row in _result_rows(request, company_id, campaign_id, view)
    ]
    fields = [
        "id", "company_name", "website", "country", "buyer_role", "verdict", "fit_score",
        "evidence_confidence", "priority_band", "known_weight", "unknown_weight",
        "unknown_dimensions", "not_applicable_dimensions", "source_count", "reasons",
        "missing_evidence", "conflicting_claims", "source_ids",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({
            **row,
            "reasons": ";".join(row.get("reasons", [])),
            "missing_evidence": ";".join(row.get("missing_evidence", [])),
            "conflicting_claims": ";".join(row.get("conflicting_claims", [])),
            "source_ids": ";".join(row.get("source_ids", [])),
            "unknown_dimensions": ";".join(
                f"{key}:{value}" for key, value in row.get("unknown_dimensions", {}).items()
            ),
            "not_applicable_dimensions": ";".join(
                f"{key}:{value}" for key, value in row.get("not_applicable_dimensions", {}).items()
            ),
        })
    return Response(
        content="\ufeff" + stream.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="research-{campaign_id}-{view}.csv"'},
    )


@router.get("/research/configuration")
def research_configuration(request: Request, principal: Principal = Depends(current_principal),
                           x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    products = [dict(row) for row in request.app.state.db.all(
        "SELECT id,name FROM products WHERE company_id=? ORDER BY name", (company_id,)
    )]
    return {
        "origins": {"seller_countries": "system-safe default", "scoring": "tenant default"},
        "limits": {"target_countries": 25, "max_qualified_leads_per_country": 200},
        "buyer_types": ["importer", "distributor", "retailer", "brand", "wholesaler", "procurement_organization"],
        "products": products, "default_seller_countries": ["TR"],
        "refresh_schedules": ["none", "weekly", "monthly", "quarterly"],
    }


@router.get("/research/sectors")
def research_sectors(_: Principal = Depends(current_principal)):
    return [sector.model_dump(mode="json") for sector in load_sectors()]


@router.get("/research/scoring-profiles")
def scoring_profiles(_: Principal = Depends(current_principal)):
    return [CampaignConfig.model_fields["scoring"].default_factory().model_dump(mode="json")]


@router.get("/research/enrichment-profiles")
def enrichment_profiles(_: Principal = Depends(current_principal)):
    return [{"profile_id": "local-balanced", "name": "Local balanced", "local": True, "available": False}]


@router.get("/research/model-profiles")
def model_profiles(request: Request, _: Principal = Depends(current_principal)):
    model = request.app.state.settings.chat_model
    return ([{"id": model, "name": model, "local": True, "available": True}] if model else [])


@router.get("/data-sources/catalog")
def source_catalog(request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    return request.app.state.lead_research.catalog(_scope(principal, x_company_id))


def _admin_company(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


@router.get("/data-sources/{source_id}/impact")
def source_impact(source_id: str, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    if source_id not in request.app.state.lead_research.registry.definitions:
        raise HTTPException(404, "Data source not found")
    return EvidenceRepository(request.app.state.db, company_id).impact(source_id)


def _source_action(source_id: str, request: Request, company_id: str, *, installed=None, enabled=None):
    request.app.state.lead_research.ensure_catalog(company_id)
    row = request.app.state.db.one(
        "SELECT * FROM dataset_definitions WHERE company_id=? AND source_id=?", (company_id, source_id)
    )
    if not row:
        raise HTTPException(404, "Data source not found")
    next_installed = row["installed"] if installed is None else int(installed)
    next_enabled = row["enabled"] if enabled is None else int(enabled)
    if not next_installed:
        next_enabled = 0
    request.app.state.db.execute(
        "UPDATE dataset_definitions SET installed=?,enabled=?,updated_at=? WHERE company_id=? AND source_id=?",
        (next_installed, next_enabled, now(), company_id, source_id),
    )
    return next(item for item in request.app.state.lead_research.catalog(company_id) if item["source_id"] == source_id)


@router.post("/data-sources/{source_id}/install")
def install_source(source_id: str, request: Request, principal: Principal = Depends(require_admin),
                   x_company_id: str | None = Header(default=None)):
    return _source_action(source_id, request, _admin_company(principal, x_company_id), installed=True)


@router.post("/data-sources/{source_id}/uninstall")
def uninstall_source(source_id: str, request: Request, principal: Principal = Depends(require_admin),
                     x_company_id: str | None = Header(default=None)):
    return _source_action(source_id, request, _admin_company(principal, x_company_id), installed=False, enabled=False)


@router.post("/data-sources/{source_id}/purge")
def purge_source(source_id: str, body: PurgeRequest, request: Request,
                 principal: Principal = Depends(require_admin),
                 x_company_id: str | None = Header(default=None)):
    company_id = _admin_company(principal, x_company_id)
    definition = request.app.state.lead_research.registry.definitions.get(source_id)
    if not definition:
        raise HTTPException(404, "Data source not found")
    if body.confirmation != definition.display_name:
        raise HTTPException(422, detail={"path": "confirmation", "message": "Type the source name exactly"})
    impact = EvidenceRepository(request.app.state.db, company_id).withdraw_source(source_id, purge=True)
    return {"purged": True, "impact": impact,
            "message": "Raw and normalized evidence removed; affected leads require recalculation."}
