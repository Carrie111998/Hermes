from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..auth import Principal, company_scope, current_principal, require_admin
from ..db import json_dump, json_load, new_id, now


router = APIRouter(tags=["operations"])


class ExportRequest(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)
    format: str = "csv"


class DataSourceCreate(BaseModel):
    source_type: str
    name: str
    enabled: bool = True
    data: dict[str, Any] = Field(default_factory=dict)


def _scope(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


def _count(db, table: str, company_id: str, clause: str = "", params: tuple = ()) -> int:
    row = db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE company_id=? {clause}",
                 (company_id, *params))
    return int(row["n"])


@router.get("/analytics/overview")
def analytics_overview(request: Request, principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    metrics = request.app.state.runs.analytics(company_id)
    metrics.update({
        "campaigns": _count(request.app.state.db, "outreach_campaigns", company_id),
        "approved_brain": _count(request.app.state.db, "company_brain_snapshots", company_id,
                                 "AND status='approved'"),
        "reply_rate": round(metrics["replied"] / metrics["sent"] * 100, 2) if metrics["sent"] else 0,
    })
    return metrics


@router.get("/analytics/sales-pipeline")
def sales_pipeline(request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    rows = request.app.state.db.all(
        "SELECT status,COUNT(*) AS count FROM leads WHERE company_id=? GROUP BY status ORDER BY status",
        (company_id,),
    )
    return {"stages": [{"status": row["status"], "count": row["count"]} for row in rows],
            "total": sum(row["count"] for row in rows)}


def _country_metrics(request: Request, company_id: str) -> list[dict]:
    return [dict(row) for row in request.app.state.db.all(
        "SELECT COALESCE(country,'unknown') AS country,COUNT(*) AS leads,"
        "SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END) AS archived "
        "FROM leads WHERE company_id=? GROUP BY country ORDER BY leads DESC", (company_id,)
    )]


@router.get("/analytics/market-intelligence")
def market_intelligence(request: Request, principal: Principal = Depends(current_principal),
                        x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    markets = _country_metrics(request, company_id)
    for market in markets:
        sent = request.app.state.db.one(
            "SELECT COUNT(*) AS n FROM outreach_messages m JOIN leads l ON l.id=m.lead_id "
            "WHERE m.company_id=? AND l.country=? AND m.status IN ('sent','replied')",
            (company_id, market["country"]),
        )["n"]
        replies = request.app.state.db.one(
            "SELECT COUNT(*) AS n FROM outreach_messages m JOIN leads l ON l.id=m.lead_id "
            "WHERE m.company_id=? AND l.country=? AND m.replied_at IS NOT NULL",
            (company_id, market["country"]),
        )["n"]
        market.update({"sent": sent, "replies": replies,
                       "reply_rate": round(replies / sent * 100, 2) if sent else 0,
                       "opportunity_score": min(100, market["leads"] * 2 + replies * 10)})
    return {"markets": markets}


@router.get("/analytics/leads-by-country")
def leads_by_country(request: Request, principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    return _country_metrics(request, _scope(principal, x_company_id))


@router.get("/analytics/leads-by-industry")
def leads_by_industry(request: Request, principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    counts: dict[str, int] = {}
    for row in request.app.state.db.all("SELECT data FROM leads WHERE company_id=?",
                                        (_scope(principal, x_company_id),)):
        industry = str(json_load(row["data"], {}).get("industry") or "unknown")
        counts[industry] = counts.get(industry, 0) + 1
    return [{"industry": key, "count": value} for key, value in sorted(counts.items(),
                                                                         key=lambda item: -item[1])]


@router.get("/analytics/contactability")
def contactability(request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    total = _count(request.app.state.db, "leads", company_id)
    reachable = request.app.state.db.one(
        "SELECT COUNT(DISTINCT lead_id) AS n FROM contacts WHERE company_id=? AND do_not_contact=0 "
        "AND (email IS NOT NULL OR phone IS NOT NULL OR linkedin_url IS NOT NULL)", (company_id,),
    )["n"]
    return {"total_leads": total, "contactable_leads": reachable,
            "contactability_rate": round(reachable / total * 100, 2) if total else 0}


@router.get("/analytics/outreach")
def outreach_analytics(request: Request, principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return [dict(row) for row in request.app.state.db.all(
        "SELECT channel,status,COUNT(*) AS count FROM outreach_messages WHERE company_id=? "
        "GROUP BY channel,status ORDER BY channel,status", (company_id,)
    )]


@router.get("/analytics/source-performance")
def source_performance(request: Request, principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    counts: dict[str, int] = {}
    for row in request.app.state.db.all("SELECT data FROM leads WHERE company_id=?",
                                        (_scope(principal, x_company_id),)):
        source = str(json_load(row["data"], {}).get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return [{"source": source, "lead_count": count} for source, count in counts.items()]


@router.get("/analytics/product-market-fit")
def product_market_fit(request: Request, principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    brain = request.app.state.db.one(
        "SELECT content FROM company_brain_snapshots WHERE company_id=? AND status='approved' "
        "ORDER BY version DESC LIMIT 1", (company_id,),
    )
    return json_load(brain["content"], {}).get("market_assumptions", {}) if brain else {}


@router.get("/admin/analytics/overview")
def admin_overview(request: Request, _: Principal = Depends(require_admin)):
    db = request.app.state.db
    return {"companies": db.one("SELECT COUNT(*) AS n FROM companies")["n"],
            "users": db.one("SELECT COUNT(*) AS n FROM users")["n"],
            "agent_runs": db.one("SELECT COUNT(*) AS n FROM agent_runs")["n"],
            "sent_messages": db.one("SELECT COUNT(*) AS n FROM outreach_messages WHERE status='sent'")["n"]}


@router.get("/admin/analytics/companies")
def admin_companies(request: Request, _: Principal = Depends(require_admin)):
    return [dict(row) for row in request.app.state.db.all(
        "SELECT c.id,c.name,c.status,COUNT(DISTINCT l.id) AS leads,COUNT(DISTINCT r.id) AS runs "
        "FROM companies c LEFT JOIN leads l ON l.company_id=c.id LEFT JOIN agent_runs r ON r.company_id=c.id "
        "GROUP BY c.id ORDER BY c.name")]


@router.get("/admin/analytics/usage")
@router.get("/admin/analytics/agent-runs")
def admin_runs(request: Request, _: Principal = Depends(require_admin)):
    return [dict(row) for row in request.app.state.db.all(
        "SELECT company_id,status,run_type,COUNT(*) AS count,SUM(cost) AS cost FROM agent_runs "
        "GROUP BY company_id,status,run_type ORDER BY company_id,run_type")]


@router.get("/admin/analytics/errors")
def admin_errors(request: Request, _: Principal = Depends(require_admin)):
    return [dict(row) for row in request.app.state.db.all(
        "SELECT id,company_id,run_type,error,completed_at FROM agent_runs WHERE status='failed' "
        "ORDER BY completed_at DESC LIMIT 200")]


@router.get("/admin/analytics/integrations")
def admin_integrations(request: Request, _: Principal = Depends(require_admin)):
    return [dict(row) for row in request.app.state.db.all(
        "SELECT kind,provider,status,COUNT(*) AS count FROM integrations GROUP BY kind,provider,status")]


@router.get("/admin/analytics/costs")
def admin_costs(request: Request, _: Principal = Depends(require_admin)):
    return [dict(row) for row in request.app.state.db.all(
        "SELECT company_id,SUM(cost) AS total_cost FROM agent_runs GROUP BY company_id")]


EXPORT_TABLES = {
    "leads": ("leads", ["id", "company_name", "website", "country", "status", "created_at"]),
    "contacts": ("contacts", ["id", "lead_id", "email", "phone", "linkedin_url", "status"]),
    "research": ("research", ["id", "lead_id", "status", "insights", "created_at"]),
    "outreach": ("outreach_messages", ["id", "lead_id", "contact_id", "channel", "status", "sent_at"]),
}


def _create_export(kind: str, body: ExportRequest, request: Request,
                   principal: Principal, company_header: str | None):
    if body.format != "csv":
        raise HTTPException(422, "CSV is the only MVP export format")
    company_id = _scope(principal, company_header)
    export_id, stamp = new_id("exp"), now()
    directory = request.app.state.settings.upload_dir / company_id / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{export_id}-{kind}.csv"
    if kind == "analytics":
        rows = [{"metric": key, "value": value}
                for key, value in analytics_overview(request, principal, company_header).items()]
        fields = ["metric", "value"]
    else:
        table, fields = EXPORT_TABLES[kind]
        rows = [dict(row) for row in request.app.state.db.all(
            f"SELECT {','.join(fields)} FROM {table} WHERE company_id=?", (company_id,)
        )]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    request.app.state.db.execute("INSERT INTO exports VALUES(?,?,?,?,?,?,?,?)",
                                 (export_id, company_id, kind, "ready", str(path),
                                  json_dump({"format": "csv", "rows": len(rows)}), stamp, stamp))
    return {"id": export_id, "type": kind, "status": "ready", "format": "csv", "rows": len(rows)}


def _export_endpoint(kind: str):
    def endpoint(body: ExportRequest, request: Request,
                 principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
        return _create_export(kind, body, request, principal, x_company_id)
    return endpoint


for _export_type in ("leads", "contacts", "research", "outreach", "analytics"):
    router.add_api_route(f"/exports/{_export_type}", _export_endpoint(_export_type), methods=["POST"],
                         status_code=201, name=f"export_{_export_type}")


@router.get("/exports/{export_id}")
def get_export(export_id: str, request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM exports WHERE id=? AND company_id=?",
                                   (export_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Export not found")
    return {"id": row["id"], "type": row["export_type"], "status": row["status"],
            "data": json_load(row["data"], {}), "created_at": row["created_at"]}


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, request: Request, principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one("SELECT * FROM exports WHERE id=? AND company_id=?", (export_id, company_id))
    if not row or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(404, "Export file not found")
    return FileResponse(row["path"], media_type="text/csv", filename=Path(row["path"]).name)


def _source(row) -> dict:
    return {"id": row["id"], "source_type": row["source_type"], "name": row["name"],
            "enabled": bool(row["enabled"]), "data": json_load(row["data"], {}),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@router.get("/data-sources")
def data_sources(request: Request, principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    return [_source(row) for row in request.app.state.db.all(
        "SELECT * FROM data_sources WHERE company_id=? ORDER BY name", (_scope(principal, x_company_id),)
    )]


@router.post("/data-sources", status_code=201)
def create_data_source(body: DataSourceCreate, request: Request,
                       principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    source_id, company_id, stamp = new_id("src"), _scope(principal, x_company_id), now()
    request.app.state.db.execute("INSERT INTO data_sources VALUES(?,?,?,?,?,?,?,?)",
                                 (source_id, company_id, body.source_type, body.name, int(body.enabled),
                                  json_dump(body.data), stamp, stamp))
    return _source(request.app.state.db.one("SELECT * FROM data_sources WHERE id=?", (source_id,)))


@router.get("/data-sources/{source_id}")
def get_data_source(source_id: str, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM data_sources WHERE id=? AND company_id=?",
                                   (source_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Data source not found")
    return _source(row)


@router.patch("/data-sources/{source_id}")
def patch_data_source(source_id: str, body: DataSourceCreate, request: Request,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    get_data_source(source_id, request, principal, x_company_id)
    request.app.state.db.execute(
        "UPDATE data_sources SET source_type=?,name=?,enabled=?,data=?,updated_at=? WHERE id=?",
        (body.source_type, body.name, int(body.enabled), json_dump(body.data), now(), source_id),
    )
    return get_data_source(source_id, request, principal, x_company_id)


@router.delete("/data-sources/{source_id}", status_code=204)
def delete_data_source(source_id: str, request: Request,
                       principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("DELETE FROM data_sources WHERE id=? AND company_id=?",
                                        (source_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "Data source not found")


@router.post("/data-sources/{source_id}/test")
def test_data_source(source_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    source = get_data_source(source_id, request, principal, x_company_id)
    return {"ok": source["enabled"], "source_id": source_id,
            "detail": "Configuration present" if source["enabled"] else "Source is disabled"}


def _source_enabled(source_id: str, enabled: bool, request: Request,
                    principal: Principal, company_header: str | None):
    get_data_source(source_id, request, principal, company_header)
    request.app.state.db.execute("UPDATE data_sources SET enabled=?,updated_at=? WHERE id=?",
                                 (int(enabled), now(), source_id))
    return get_data_source(source_id, request, principal, company_header)


@router.post("/data-sources/{source_id}/enable")
def enable_source(source_id: str, request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return _source_enabled(source_id, True, request, principal, x_company_id)


@router.post("/data-sources/{source_id}/disable")
def disable_source(source_id: str, request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    return _source_enabled(source_id, False, request, principal, x_company_id)


def _activities(request: Request, company_id: str, clause: str = "", params: tuple = ()):
    return [{"id": row["id"], "actor_id": row["actor_id"], "action": row["action"],
             "entity_type": row["entity_type"], "entity_id": row["entity_id"],
             "data": json_load(row["data"], {}), "created_at": row["created_at"]}
            for row in request.app.state.db.all(
                f"SELECT * FROM activity_log WHERE company_id=? {clause} ORDER BY created_at DESC LIMIT 500",
                (company_id, *params),
            )]


@router.get("/activity")
def activity(request: Request, principal: Principal = Depends(current_principal),
             x_company_id: str | None = Header(default=None)):
    return _activities(request, _scope(principal, x_company_id))


@router.get("/activity/{activity_id}")
def activity_item(activity_id: str, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    items = _activities(request, company_id, "AND id=?", (activity_id,))
    if not items:
        raise HTTPException(404, "Activity not found")
    return items[0]


@router.get("/leads/{lead_id}/activity")
def lead_activity(lead_id: str, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return _activities(request, _scope(principal, x_company_id),
                       "AND entity_type='lead' AND entity_id=?", (lead_id,))


@router.get("/contacts/{contact_id}/activity")
def contact_activity(contact_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    return _activities(request, _scope(principal, x_company_id),
                       "AND entity_type='contact' AND entity_id=?", (contact_id,))


@router.get("/outreach/campaigns/{campaign_id}/activity")
def campaign_activity(campaign_id: str, request: Request,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    return _activities(request, _scope(principal, x_company_id),
                       "AND entity_type='campaign' AND entity_id=?", (campaign_id,))
