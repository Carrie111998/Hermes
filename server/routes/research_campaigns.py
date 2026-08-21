"""Tenant-scoped lead-research campaign, evidence, and source lifecycle API."""
from __future__ import annotations

import csv
import io
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from ..auth import Principal, company_scope, current_principal, require_admin
from ..db import json_dump, json_load, new_id, now
from ..lead_research.models import CampaignConfig
from ..lead_research.sectors import load_sectors
from ..lead_research.service import CampaignAlreadyRunning
from ..lead_research.storage import EvidenceRepository


router = APIRouter(tags=["lead-research"])


class CampaignPatch(BaseModel):
    version: int = Field(ge=1)
    config: dict[str, Any]


class PurgeRequest(BaseModel):
    confirmation: str


def _scope(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


def _serialize(row) -> dict:
    return {
        "id": row["id"], "company_id": row["company_id"], "name": row["name"],
        "status": row["status"], "version": row["version"], "config": json_load(row["config"], {}),
        "estimate": json_load(row["estimate"], None), "run_id": row["run_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


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


ResultView = Literal["active", "rejected"]


def _result_rows(request: Request, company_id: str, campaign_id: str, view: ResultView):
    _row(request, company_id, campaign_id)
    if view == "rejected":
        sql = (
            "SELECT * FROM research_results "
            "WHERE company_id=? AND campaign_id=? AND verdict='reject' "
            "ORDER BY fit_score DESC,evidence_confidence DESC,created_at DESC"
        )
    else:
        sql = (
            "SELECT * FROM research_results "
            "WHERE company_id=? AND campaign_id=? AND verdict IN ('strong_fit','review') "
            "ORDER BY fit_score DESC,evidence_confidence DESC,created_at DESC"
        )
    return request.app.state.db.all(sql, (company_id, campaign_id))


def _serialize_result(request: Request, row) -> dict:
    data = json_load(row["data"], {})
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
    raw = body.get("config", body)
    try:
        config = CampaignConfig.model_validate(raw)
    except Exception as exc:
        raise HTTPException(422, detail={"path": "config", "message": str(exc)}) from exc
    request.app.state.lead_research.ensure_catalog(company_id)
    unknown = sorted(set(config.enabled_source_ids) - set(request.app.state.lead_research.registry.definitions))
    if unknown:
        raise HTTPException(422, detail={"path": "enabled_source_ids", "message": f"Unknown sources: {', '.join(unknown)}"})
    campaign_id, stamp = new_id("rc"), now()
    request.app.state.db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, company_id, config.name, "draft", 1, json_dump(config.model_dump(mode="json")),
         None, None, stamp, stamp),
    )
    return _serialize(_row(request, company_id, campaign_id))


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
    try:
        config = CampaignConfig.model_validate(body.config)
    except Exception as exc:
        raise HTTPException(422, detail={"path": "config", "message": str(exc)}) from exc
    self_version = current["version"] + 1
    request.app.state.db.execute(
        "UPDATE research_campaigns SET name=?,config=?,version=?,estimate=NULL,updated_at=? "
        "WHERE id=? AND company_id=?",
        (config.name, json_dump(config.model_dump(mode="json")), self_version, now(), campaign_id, company_id),
    )
    return get_campaign(campaign_id, request, principal, x_company_id)


@router.post("/research-campaigns/{campaign_id}/estimate")
def estimate_campaign(campaign_id: str, request: Request,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = _row(request, company_id, campaign_id)
    config = CampaignConfig.model_validate(json_load(row["config"], {}))
    estimate = request.app.state.lead_research.estimate(config).model_dump(mode="json")
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
    return create_campaign(config, request, principal, x_company_id)


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


@router.get("/research-campaigns/{campaign_id}/leads")
def campaign_leads(campaign_id: str, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    _row(request, company_id, campaign_id)
    result = []
    rows = request.app.state.db.all(
        "SELECT leads.* FROM research_results "
        "JOIN leads ON leads.id=research_results.lead_id AND leads.company_id=research_results.company_id "
        "WHERE research_results.company_id=? AND research_results.campaign_id=? "
        "AND research_results.verdict IN ('strong_fit','review') "
        # By fit, not by insertion order. This is the list the customer works
        # down, and the brief page promises it is ranked by their weights —
        # ordering it by created_at handed them the corpus's arbitrary order.
        # created_at only breaks ties, so the order stays stable across reruns.
        "ORDER BY research_results.fit_score DESC,"
        "research_results.evidence_confidence DESC,leads.created_at DESC",
        (company_id, campaign_id),
    )
    for row in rows:
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
                principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    lead = request.app.state.db.one("SELECT * FROM leads WHERE id=? AND company_id=?", (lead_id, company_id))
    if not lead:
        raise HTTPException(404, "Lead not found")
    organization_id = json_load(lead["data"], {}).get("organization_id")
    result = []
    for row in request.app.state.db.all(
        "SELECT * FROM feature_claims WHERE company_id=? AND organization_id=? ORDER BY field",
        (company_id, organization_id),
    ):
        data = json_load(row["data"], {})
        evidence_ids = json_load(row["evidence_ids"], [])
        evidence = []
        for evidence_id in evidence_ids:
            ev = request.app.state.db.one(
                "SELECT source_id,provenance_url,retrieved_at,snapshot_id FROM evidence_records "
                "WHERE id=? AND company_id=?", (evidence_id, company_id),
            )
            if ev:
                evidence.append(dict(ev))
        result.append({
            "id": row["id"], "field": row["field"], "value": json_load(row["value"], None),
            "status": row["status"], "confidence": row["confidence"], "method": row["method"],
            "evidence_ids": evidence_ids, "evidence": evidence, "verified_at": row["verified_at"], **data,
        })
    return result


@router.get("/research/results/{result_id}/claims")
def result_claims(result_id: str, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    result_row = request.app.state.db.one(
        "SELECT company_id,campaign_id,organization_id FROM research_results "
        "WHERE id=? AND company_id=?",
        (result_id, company_id),
    )
    if not result_row:
        raise HTTPException(404, "Research result not found")
    claims = []
    for row in request.app.state.db.all(
        "SELECT * FROM feature_claims "
        "WHERE company_id=? AND campaign_id=? AND organization_id=? ORDER BY field,verified_at DESC",
        (company_id, result_row["campaign_id"], result_row["organization_id"]),
    ):
        data = json_load(row["data"], {})
        evidence_ids = json_load(row["evidence_ids"], [])
        evidence = []
        for evidence_id in evidence_ids:
            stored = request.app.state.db.one(
                "SELECT source_id,provenance_url,retrieved_at,snapshot_id,raw_hash,method,confidence "
                "FROM evidence_records WHERE id=? AND company_id=? AND organization_id=?",
                (evidence_id, company_id, result_row["organization_id"]),
            )
            if stored:
                item = dict(stored)
                url = item.get("provenance_url")
                item["provenance_url"] = url if str(url or "").startswith("https://") else None
                evidence.append(item)
        claims.append({
            **data,
            "id": row["id"],
            "field": row["field"],
            "value": json_load(row["value"], None),
            "status": row["status"],
            "confidence": row["confidence"],
            "method": row["method"],
            "evidence_ids": evidence_ids,
            "evidence": evidence,
            "verified_at": row["verified_at"],
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
        "evidence_confidence", "source_count", "reasons", "missing_evidence",
        "conflicting_claims", "source_ids",
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
