from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..auth import Principal, company_scope, current_principal, require_admin
from ..db import json_dump, json_load, new_id, now
from ..digest import KINDS, day_bounds, day_key, get_digest, write_digest


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


def _weekly_counts(timestamps: list[float | None]) -> dict:
    labels = [f"W-{weeks}" for weeks in range(7, 0, -1)] + ["Now"]
    values = [0] * len(labels)
    stamp, week = now(), 7 * 86400
    for value in timestamps:
        if value is None:
            continue
        age = int(max(0, stamp - float(value)) // week)
        if age <= 7:
            values[7 - age] += 1
    return {"labels": labels, "values": values}


def _market_fit(request: Request, company_id: str, markets: list[dict]) -> list[dict]:
    products = [dict(row) for row in request.app.state.db.all(
        "SELECT id,name FROM products WHERE company_id=? ORDER BY name", (company_id,),
    )]
    brain = request.app.state.db.one(
        "SELECT content FROM company_brain_snapshots WHERE company_id=? AND status='approved' "
        "ORDER BY version DESC LIMIT 1", (company_id,),
    )
    assumptions = json_load(brain["content"], {}).get("market_assumptions", {}) if brain else {}
    candidates = assumptions.get("product_market_fit", []) if isinstance(assumptions, dict) else []
    normalized = []
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict) or not item.get("country"):
            continue
        fits = item.get("products") if isinstance(item.get("products"), list) else []
        normalized.append({"country": item["country"], "products": fits})
    if normalized or not products:
        return normalized
    return [{
        "country": market["country"],
        "products": [{
            "product_id": product["id"],
            "name": product["name"],
            "score": max(1, int(market["opportunity_score"]) - index * 8),
        } for index, product in enumerate(products[:3])],
    } for market in markets]


@router.get("/analytics/sales-pipeline")
def sales_pipeline(request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    rows = request.app.state.db.all(
        "SELECT status,COUNT(*) AS count FROM leads WHERE company_id=? GROUP BY status ORDER BY status",
        (company_id,),
    )
    stages = [{"status": row["status"], "count": row["count"]} for row in rows]
    db = request.app.state.db
    messages = db.all(
        "SELECT sent_at,replied_at FROM outreach_messages WHERE company_id=?",
        (company_id,),
    )
    researched = _count(db, "research", company_id, "AND status='succeeded'")
    contacts = _count(db, "contacts", company_id)
    sent = sum(1 for row in messages if row["sent_at"] is not None)
    replied = sum(1 for row in messages if row["replied_at"] is not None)
    interested = _count(db, "leads", company_id, "AND status='interested'")
    return {
        "stages": stages,
        "total": sum(row["count"] for row in rows),
        "leads_by_status": stages,
        "emails_sent_weekly": _weekly_counts([row["sent_at"] for row in messages]),
        "replies_weekly": _weekly_counts([row["replied_at"] for row in messages]),
        "funnel": [
            {"stage": "Leads discovered", "value": sum(row["count"] for row in rows)},
            {"stage": "Researched", "value": researched},
            {"stage": "Contacts found", "value": contacts},
            {"stage": "Emails sent", "value": sent},
            {"stage": "Replies", "value": replied},
            {"stage": "Interested", "value": interested},
        ],
    }


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
    industries = leads_by_industry(request, principal, x_company_id)
    sources = source_performance(request, principal, x_company_id)
    return {
        "markets": markets,
        "country_scores": [
            {"country": market["country"], "score": market["opportunity_score"]}
            for market in markets
        ],
        "top_industries": [
            {"label": item["industry"].replace("_", " ").title(), "value": item["count"]}
            for item in industries
        ],
        "source_performance": [
            {"label": item["source"].replace("_", " ").title(), "value": item["lead_count"]}
            for item in sources
        ],
        "product_market_fit": _market_fit(request, company_id, markets),
    }


def _recommended_actions(request: Request, company_id: str) -> list[dict]:
    db = request.app.state.db
    actions = []
    awaiting = _count(db, "outreach_messages", company_id, "AND status='pending_approval'")
    if awaiting:
        actions.append({
            "icon": "mail",
            "title": f"Review {awaiting} generated messages awaiting approval",
            "sub": "Approve or revise them before delivery.",
            "href": "/app/outreach",
        })
    onboarding = db.one("SELECT status FROM onboarding WHERE company_id=?", (company_id,))
    if not onboarding or onboarding["status"] != "completed":
        actions.append({
            "icon": "upload",
            "title": "Finish workspace onboarding",
            "sub": "Complete the source data and Company Brain review.",
            "href": "/app/onboarding",
        })
    new_leads = _count(db, "leads", company_id, "AND status='new'")
    if new_leads:
        actions.append({
            "icon": "search",
            "title": f"Research {new_leads} new leads",
            "sub": "Turn raw companies into scored opportunities.",
            "href": "/app/leads?status=new",
        })
    if not actions:
        actions.append({
            "icon": "map",
            "title": "Review market opportunities",
            "sub": "Compare current country and source performance.",
            "href": "/app/analytics",
        })
    return actions[:4]


@router.get("/analytics/dashboard")
def analytics_dashboard(request: Request, principal: Principal = Depends(current_principal),
                        x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    db = request.app.state.db
    overview = analytics_overview(request, principal, x_company_id)
    market = market_intelligence(request, principal, x_company_id)
    messages = db.all(
        "SELECT channel,sent_at,replied_at FROM outreach_messages WHERE company_id=?",
        (company_id,),
    )
    activities = [dict(row) for row in db.all(
        "SELECT * FROM activity_log WHERE company_id=? ORDER BY created_at DESC LIMIT 8",
        (company_id,),
    )]
    selected = [row["country_code"] for row in db.all(
        "SELECT country_code FROM selected_countries WHERE company_id=? ORDER BY country_code",
        (company_id,),
    )]
    sent = sum(1 for row in messages if row["sent_at"] is not None)
    replied = sum(1 for row in messages if row["replied_at"] is not None)
    return {
        "sales": {
            "leads_found": overview["leads"],
            "contacts_found": overview["contacts"],
            "emails_sent": sent,
            "replies": replied,
            "interested": _count(db, "leads", company_id, "AND status='interested'"),
            "whatsapp_messages": sum(1 for row in messages if row["channel"] == "whatsapp"),
            "active_campaigns": _count(
                db, "outreach_campaigns", company_id,
                "AND status NOT IN ('completed','cancelled')",
            ),
        },
        "sparks": {
            "leads": _weekly_counts([
                row["created_at"] for row in db.all(
                    "SELECT created_at FROM leads WHERE company_id=?", (company_id,),
                )
            ])["values"],
            "emails": _weekly_counts([row["sent_at"] for row in messages])["values"],
        },
        "market": {
            "best_countries": market["country_scores"][:5],
            "top_industries": market["top_industries"][:5],
            "source_performance": market["source_performance"][:4],
        },
        "recent_activity": [{
            "id": row["id"],
            "kind": "reply" if "repl" in row["action"] else (
                "document" if row["entity_type"] == "document" else "agent"
            ),
            "label": row["action"].replace("_", " "),
            "at": row["created_at"],
            "ref": {
                f"{row['entity_type']}_id": row["entity_id"]
            } if row["entity_type"] and row["entity_id"] else {},
        } for row in activities],
        "recommended_actions": _recommended_actions(request, company_id),
        "country_scores": {
            item["country"]: item["score"] for item in market["country_scores"]
        },
        "selected_countries": selected,
    }


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
    """Per-tenant spend, in the two units this system actually spends in.

    `total_cost` is model spend and is still structurally 0: the run executor
    shells out to the hermes CLI and does not parse token accounting back out of
    it. `metering_enabled` says so explicitly, because a bare 0.0 reads as "this
    tenant cost nothing" rather than "not measured". Populate it in
    AgentRunService before flipping the flag.

    `provider_requests` is metered, and for lead research it is the bill that
    actually arrives: a Web Unlocker fetch is a paid request and one candidate
    costs several. It is deliberately a separate field rather than added into
    `total_cost` — requests and tokens are different units, and a single number
    summing both would mean nothing. It is a floor: a verify that raises after
    spending never reports what it spent.
    """
    # Summed in Python, not with json_extract: that function is spelled
    # differently on Postgres (`->>`) and this table holds one row per campaign
    # per dimension, so there is nothing to gain by pushing it into SQL.
    requests_by_company: dict[str, int] = {}
    for row in request.app.state.db.all(
        "SELECT company_id,metrics FROM campaign_metrics WHERE dimension='overall'"
    ):
        spent = json_load(row["metrics"], {}).get("provider_requests") or 0
        requests_by_company[row["company_id"]] = (
            requests_by_company.get(row["company_id"], 0) + int(spent)
        )
    return [
        {
            **dict(row),
            "metering_enabled": False,
            "provider_requests": requests_by_company.get(row["company_id"], 0),
            "provider_requests_metered": True,
        }
        for row in request.app.state.db.all(
            "SELECT company_id,SUM(cost) AS total_cost FROM agent_runs GROUP BY company_id"
        )
    ]


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
    # The provider catalog reuses the established enable/disable endpoints.
    # Catalog lifecycle is admin-only and retains historical evidence; legacy
    # tenant-created data sources keep their original behavior below.
    if source_id in request.app.state.lead_research.registry.definitions:
        if not principal.is_admin:
            raise HTTPException(403, "Administrator role required")
        definition = request.app.state.lead_research.registry.definitions[source_id]
        if enabled and definition.health == "retired":
            raise HTTPException(409, "Retired sources cannot be enabled for new campaigns")
        company_id = _scope(principal, company_header)
        request.app.state.lead_research.ensure_catalog(company_id)
        request.app.state.db.execute(
            "UPDATE dataset_definitions SET installed=1,enabled=?,updated_at=? "
            "WHERE company_id=? AND source_id=?",
            (int(enabled), now(), company_id, source_id),
        )
        return next(
            item for item in request.app.state.lead_research.catalog(company_id)
            if item["source_id"] == source_id
        )
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
             x_company_id: str | None = Header(default=None),
             since: float | None = Query(default=None, ge=0)):
    """`since` is an epoch seconds lower bound, so a briefing can ask for
    "what changed since yesterday" instead of pulling 500 rows and filtering
    client-side."""
    company_id = _scope(principal, x_company_id)
    if since is None:
        return _activities(request, company_id)
    return _activities(request, company_id, "AND created_at>=?", (float(since),))


# Declared before /activity/{activity_id}: FastAPI matches in declaration order,
# so a literal path registered after the parameterised one would never be hit.
@router.get("/activity/digest")
def activity_digest(request: Request, principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None),
                    date: str | None = Query(default=None),
                    refresh: bool = Query(default=False)):
    """The day's plan and report.

    Returns whatever the scheduler has written. `refresh=true` assembles a
    missing digest on demand so a workspace with the scheduler switched off can
    still show a briefing — it never overwrites one already written.
    """
    company_id = _scope(principal, x_company_id)
    day = date or day_key()
    try:
        day_bounds(day)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    db = request.app.state.db
    result = {"date": day, "scheduled": bool(request.app.state.settings.scheduler_enabled)}
    for kind in KINDS:
        digest = get_digest(db, company_id, day, kind)
        if digest is None and refresh:
            digest = write_digest(db, company_id, day, kind)
        result[kind] = digest
    return result


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
